#!/usr/bin/env python3
"""Transcript JSONL indexer with SQLite FTS5.

Standalone CLI script — indexes append-only JSONL transcripts into SQLite
for fast pagination, BM25 search, and stats queries. Designed to run
locally or via SSH from the bridge.

Python 3.9+ compatible. No external dependencies.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

# Entry types to skip (noise)
_SKIP_TYPES = frozenset(("progress", "queue-operation", "file-history-snapshot", "system"))


def _create_tables(db):
    """Create schema if not exists."""
    db.execute("""CREATE TABLE IF NOT EXISTS entries (
        idx INTEGER PRIMARY KEY,
        byte_offset INTEGER,
        type TEXT,
        role TEXT,
        timestamp TEXT,
        model TEXT,
        version TEXT,
        git_branch TEXT,
        raw_json TEXT,
        plain_text TEXT,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0
    )""")
    # Check if FTS table exists (can't use IF NOT EXISTS with virtual tables portably)
    has_fts = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone()
    if not has_fts:
        db.execute("""CREATE VIRTUAL TABLE entries_fts USING fts5(
            plain_text, content=entries, content_rowid=idx,
            tokenize='unicode61'
        )""")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def _extract_plain_text(entry):
    """Extract searchable plain text from an entry."""
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("text"):
                parts.append(c["text"])
            if c.get("thinking"):
                parts.append(c["thinking"])
            if isinstance(c.get("content"), str):
                parts.append(c["content"])
            if c.get("type") == "tool_use":
                parts.append(json.dumps(c.get("input", {}), ensure_ascii=False))
    return " ".join(parts)


def _index_jsonl(db, jsonl_path):
    """Index JSONL into SQLite, incrementally if possible.

    Returns number of new entries indexed.
    """
    file_size = os.path.getsize(jsonl_path)
    if file_size == 0:
        return 0

    # Check if we can do incremental indexing
    row = db.execute("SELECT value FROM meta WHERE key='file_size'").fetchone()
    old_size = int(row[0]) if row else 0

    # Detect source file change (different JSONL path → full reindex)
    path_row = db.execute("SELECT value FROM meta WHERE key='jsonl_path'").fetchone()
    old_path = path_row[0] if path_row else ""
    abs_path = os.path.abspath(jsonl_path)
    if old_path and old_path != abs_path:
        db.execute("DELETE FROM entries")
        db.execute("DELETE FROM entries_fts")
        db.execute("DELETE FROM meta")
        old_size = 0

    if old_size == file_size:
        return 0  # No changes

    if old_size > file_size:
        # File shrank (truncated/replaced) — full reindex
        db.execute("DELETE FROM entries")
        db.execute("DELETE FROM entries_fts")
        db.execute("DELETE FROM meta")
        old_size = 0

    # Get current entry count for idx continuity
    row = db.execute("SELECT MAX(idx) FROM entries").fetchone()
    next_idx = (row[0] + 1) if row[0] is not None else 0

    # Parse only new bytes
    new_count = 0
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        if old_size > 0:
            f.seek(old_size)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype = entry.get("type", "")
            if etype in _SKIP_TYPES:
                continue

            msg = entry.get("message", {})
            plain_text = _extract_plain_text(entry)
            usage = msg.get("usage", {})
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
            output_tokens = usage.get("output_tokens", 0)

            db.execute(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    next_idx,
                    old_size,  # byte_offset of the start of new section
                    etype,
                    msg.get("role", ""),
                    entry.get("timestamp", ""),
                    msg.get("model", ""),
                    entry.get("version", ""),
                    entry.get("gitBranch", ""),
                    json.dumps(entry, ensure_ascii=False),
                    plain_text,
                    input_tokens,
                    output_tokens,
                ),
            )
            db.execute("INSERT INTO entries_fts(rowid, plain_text) VALUES (?, ?)",
                        (next_idx, plain_text))
            next_idx += 1
            new_count += 1

    # Update meta
    db.execute("INSERT OR REPLACE INTO meta VALUES ('file_size', ?)", (str(file_size),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('entry_count', ?)", (str(next_idx),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('jsonl_path', ?)", (abs_path,))
    db.commit()
    return new_count


def _query_entries(db, page=None, per_page=50, filter_mode=""):
    """Paginated entries query."""
    where = ""
    params = []
    if filter_mode == "prompts":
        where = """WHERE type='user' AND role='user'
                   AND plain_text != ''
                   AND plain_text NOT LIKE '<%'
                   AND json_type(raw_json, '$.message.content') = 'text'"""

    total = db.execute("SELECT COUNT(*) FROM entries " + where, params).fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 0

    if total == 0:
        return {"entries": [], "total": 0, "total_pages": 0, "page": 1, "per_page": per_page}

    if page is None:
        page = total_pages  # Default to last page
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = db.execute(
        "SELECT idx, type, role, timestamp, raw_json FROM entries "
        + where
        + " ORDER BY idx LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    entries = []
    for r in rows:
        entries.append({
            "idx": r[0], "type": r[1], "role": r[2],
            "timestamp": r[3], "raw_json": r[4],
        })

    return {
        "entries": entries, "total": total,
        "total_pages": total_pages, "page": page, "per_page": per_page,
    }


def _query_search(db, search_term, page=1, per_page=50):
    """FTS5 BM25 search query."""
    if not search_term:
        return {"entries": [], "total_results": 0, "total_pages": 0, "page": 1, "query": ""}

    # FTS5 match query with BM25 ranking
    count_row = db.execute(
        "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH ?",
        (search_term,),
    ).fetchone()
    total_results = count_row[0]
    total_pages = max(1, (total_results + per_page - 1) // per_page) if total_results > 0 else 0

    if total_results == 0:
        return {"entries": [], "total_results": 0, "total_pages": 0, "page": 1, "query": search_term}

    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = db.execute(
        """SELECT e.idx, e.type, e.role, e.timestamp, e.raw_json, f.rank
           FROM entries_fts f
           JOIN entries e ON f.rowid = e.idx
           WHERE entries_fts MATCH ?
           ORDER BY f.rank
           LIMIT ? OFFSET ?""",
        (search_term, per_page, offset),
    ).fetchall()

    entries = []
    for r in rows:
        entries.append({
            "idx": r[0], "type": r[1], "role": r[2],
            "timestamp": r[3], "raw_json": r[4], "rank": r[5],
        })

    return {
        "entries": entries, "total_results": total_results,
        "total_pages": total_pages, "page": page, "query": search_term,
    }


def _query_stats(db):
    """Compute transcript stats from indexed entries.

    Uses SQL aggregates for simple counts (fast), only parses raw_json
    for assistant entries that contain tool_use blocks (slower but fewer rows).
    """
    # Fast SQL aggregates — no JSON parsing needed
    agg = db.execute("""SELECT
        SUM(input_tokens), SUM(output_tokens),
        MIN(timestamp), MAX(timestamp),
        (SELECT model FROM entries WHERE type='assistant' AND model != '' LIMIT 1),
        (SELECT version FROM entries WHERE version != '' LIMIT 1),
        (SELECT git_branch FROM entries WHERE git_branch != '' LIMIT 1)
    FROM entries""").fetchone()
    if not agg or agg[0] is None:
        return {
            "n_user": 0, "n_tool": 0, "n_edit": 0,
            "lines_add": 0, "lines_del": 0, "lines_mod": 0,
            "n_files": 0, "model": "", "version": "", "git_branch": "",
            "first_ts": "", "last_ts": "",
            "input_tokens": 0, "output_tokens": 0, "duration": "",
        }

    input_tokens = agg[0] or 0
    output_tokens = agg[1] or 0
    first_ts = agg[2] or ""
    last_ts = agg[3] or ""
    model = agg[4] or ""
    version = agg[5] or ""
    git_branch = agg[6] or ""

    # Count user prompts via SQL
    n_user = db.execute(
        "SELECT COUNT(*) FROM entries WHERE type='user' AND role='user' "
        "AND plain_text != '' AND json_type(raw_json, '$.message.content') = 'text'"
    ).fetchone()[0]

    # Tool stats — only parse assistant entries (much fewer than total)
    n_tool = 0
    n_edit = 0
    lines_add = 0
    lines_del = 0
    lines_mod = 0
    files_modified = set()

    rows = db.execute("SELECT raw_json FROM entries WHERE type='assistant'").fetchall()
    for (raw,) in rows:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    n_tool += 1
                    if item.get("name") == "Edit":
                        n_edit += 1
                        ti = item.get("input", {})
                        fp = ti.get("file_path", "")
                        if fp:
                            files_modified.add(fp)
                        n_old = len(ti.get("old_string", "").splitlines(True))
                        n_new = len(ti.get("new_string", "").splitlines(True))
                        overlap = min(n_old, n_new)
                        lines_mod += overlap
                        lines_add += n_new - overlap
                        lines_del += n_old - overlap
                    elif item.get("name") in ("Write", "Read", "Glob", "Grep"):
                        fp = item.get("input", {}).get("file_path", "") or item.get("input", {}).get("path", "")
                        if fp:
                            files_modified.add(fp)

    # Duration
    duration = ""
    if first_ts and last_ts:
        try:
            fmt = "%Y-%m-%dT%H:%M:%S"
            t0 = datetime.strptime(first_ts[:19], fmt)
            t1 = datetime.strptime(last_ts[:19], fmt)
            delta = int((t1 - t0).total_seconds())
            if delta > 0:
                days = delta // 86400
                hours = (delta % 86400) // 3600
                mins = (delta % 3600) // 60
                if days > 0:
                    duration = "%dd %dh" % (days, hours)
                elif hours > 0:
                    duration = "%dh %dm" % (hours, mins)
                else:
                    duration = "%dm" % mins
        except (ValueError, TypeError):
            pass

    return {
        "n_user": n_user, "n_tool": n_tool, "n_edit": n_edit,
        "lines_add": lines_add, "lines_del": lines_del, "lines_mod": lines_mod,
        "n_files": len(files_modified), "model": model,
        "version": version, "git_branch": git_branch,
        "first_ts": first_ts, "last_ts": last_ts,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "duration": duration,
    }


def main():
    parser = argparse.ArgumentParser(description="Transcript JSONL indexer with SQLite FTS5")
    parser.add_argument("--jsonl", required=True, help="Path to JSONL transcript file")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument("--query", required=True, choices=["entries", "search", "stats", "entries+stats", "search+stats"],
                        help="Query type")
    parser.add_argument("--page", type=int, default=None, help="Page number (default: last page)")
    parser.add_argument("--per-page", type=int, default=50, help="Entries per page (default: 50)")
    parser.add_argument("--search", type=str, default="", help="Search term (for search query)")
    parser.add_argument("--filter", type=str, default="", dest="filter_mode",
                        help="Filter mode (e.g., 'prompts')")
    args = parser.parse_args()

    # Handle missing/nonexistent JSONL
    if not os.path.exists(args.jsonl):
        if args.query == "entries":
            json.dump({"entries": [], "total": 0, "total_pages": 0, "page": 1, "per_page": args.per_page}, sys.stdout)
        elif args.query == "search":
            json.dump({"entries": [], "total_results": 0, "total_pages": 0, "page": 1, "query": args.search}, sys.stdout)
        elif args.query == "stats":
            json.dump({"n_user": 0, "n_tool": 0, "n_edit": 0, "lines_add": 0, "lines_del": 0,
                        "lines_mod": 0, "n_files": 0, "model": "", "version": "", "git_branch": "",
                        "first_ts": "", "last_ts": "", "input_tokens": 0, "output_tokens": 0,
                        "duration": ""}, sys.stdout)
        return

    # Ensure db directory exists
    db_dir = os.path.dirname(args.db)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    _create_tables(db)

    # Index new entries if needed
    if os.path.getsize(args.jsonl) > 0:
        _index_jsonl(db, args.jsonl)

    # Run query
    if args.query == "entries":
        result = _query_entries(db, page=args.page, per_page=args.per_page,
                                filter_mode=args.filter_mode)
    elif args.query == "search":
        result = _query_search(db, args.search, page=args.page or 1,
                               per_page=args.per_page)
    elif args.query == "stats":
        result = _query_stats(db)
    elif args.query == "entries+stats":
        entries = _query_entries(db, page=args.page, per_page=args.per_page,
                                filter_mode=args.filter_mode)
        entries["stats"] = _query_stats(db)
        result = entries
    elif args.query == "search+stats":
        search = _query_search(db, args.search, page=args.page or 1,
                               per_page=args.per_page)
        search["stats"] = _query_stats(db)
        result = search

    json.dump(result, sys.stdout, ensure_ascii=False)
    db.close()


if __name__ == "__main__":
    main()
