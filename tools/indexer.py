#!/usr/bin/env python3
"""JSONL transcript indexer with SQLite FTS5.

Standalone CLI — indexes append-only JSONL transcript files into SQLite
for fast pagination, BM25 search, and domain-specific queries.
Designed to run locally or via SSH from the bridge.

Usage:
    python3 tools/indexer.py transcript --jsonl <path> --db <path> --query entries

Python 3.9+ compatible. No external dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Optional, Sequence


# ── Shared types ─────────────────────────────────────────────────────────────

@dataclass
class PaginatedResult:
    """Base result for paginated queries."""
    total: int = 0
    total_pages: int = 0
    page: int = 1
    per_page: int = 50


@dataclass
class EntriesResult(PaginatedResult):
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SearchResult(PaginatedResult):
    query: str = ""
    total_results: int = 0


@dataclass
class TranscriptSearchResult(SearchResult):
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TranscriptStats:
    n_user: int = 0
    n_tool: int = 0
    n_edit: int = 0
    lines_add: int = 0
    lines_del: int = 0
    lines_mod: int = 0
    n_files: int = 0
    model: str = ""
    version: str = ""
    git_branch: str = ""
    first_ts: str = ""
    last_ts: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration: str = ""



# ── Shared indexing logic ────────────────────────────────────────────────────

def _sanitize_fts5_query(term: str) -> str:
    """Sanitize a user search term for FTS5 MATCH.

    FTS5 query syntax treats characters like . * - : ^ as operators.
    If the term contains any of these, wrap each token in double quotes
    so FTS5 treats them as literal strings.
    """
    # If already looks like an intentional FTS5 query (AND/OR/NOT/NEAR), pass through
    if re.search(r'\b(AND|OR|NOT|NEAR)\b', term):
        return term
    # If it contains FTS5 special chars, quote each whitespace-separated token
    if re.search(r'[.*:^(){}"\-]', term):
        tokens = term.split()
        quoted = []
        for t in tokens:
            t = t.strip('"')
            if t:
                quoted.append(f'"{t}"')
        return " ".join(quoted)
    return term


def _setup_db(db_path: str) -> sqlite3.Connection:
    """Open SQLite with WAL mode and busy timeout."""
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _check_incremental(
    db: sqlite3.Connection,
    jsonl_path: str,
    table: str,
    meta_table: str = "meta",
) -> tuple[int, bool]:
    """Check whether incremental indexing is possible.

    Returns (old_byte_offset, needs_full_reindex).
    Side-effect: clears tables if full reindex needed.
    """
    file_size = os.path.getsize(jsonl_path)
    if file_size == 0:
        return 0, False

    row = db.execute(f"SELECT value FROM {meta_table} WHERE key='file_size'").fetchone()
    old_size = int(row[0]) if row else 0

    path_row = db.execute(f"SELECT value FROM {meta_table} WHERE key='jsonl_path'").fetchone()
    old_path = path_row[0] if path_row else ""
    abs_path = os.path.abspath(jsonl_path)

    fts_table = f"{table}_fts"

    if old_path and old_path != abs_path:
        db.execute(f"DELETE FROM {table}")
        db.execute(f"DELETE FROM {fts_table}")
        db.execute(f"DELETE FROM {meta_table}")
        return 0, True

    if old_size == file_size:
        return old_size, False  # No changes

    if old_size > file_size:
        db.execute(f"DELETE FROM {table}")
        db.execute(f"DELETE FROM {fts_table}")
        db.execute(f"DELETE FROM {meta_table}")
        return 0, True

    return old_size, True


def _update_meta(
    db: sqlite3.Connection,
    jsonl_path: str,
    next_idx: int,
    meta_table: str = "meta",
) -> None:
    """Update meta table after indexing."""
    file_size = os.path.getsize(jsonl_path)
    abs_path = os.path.abspath(jsonl_path)
    db.execute(f"INSERT OR REPLACE INTO {meta_table} VALUES ('file_size', ?)", (str(file_size),))
    db.execute(f"INSERT OR REPLACE INTO {meta_table} VALUES ('entry_count', ?)", (str(next_idx),))
    db.execute(f"INSERT OR REPLACE INTO {meta_table} VALUES ('jsonl_path', ?)", (abs_path,))
    db.commit()


def _paginate(total: int, page: Optional[int], per_page: int) -> tuple[int, int, int]:
    """Compute pagination: returns (page, total_pages, offset)."""
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 0
    if page is None:
        page = total_pages  # Default to last page
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    return page, total_pages, offset


# ── Transcript indexer ───────────────────────────────────────────────────────

_SKIP_TYPES = frozenset(("progress", "queue-operation", "file-history-snapshot", "system"))


def _transcript_create_tables(db: sqlite3.Connection) -> None:
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
    has_fts = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone()
    if not has_fts:
        db.execute("""CREATE VIRTUAL TABLE entries_fts USING fts5(
            plain_text, content=entries, content_rowid=idx,
            tokenize='unicode61'
        )""")
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def _extract_plain_text(entry: dict[str, Any]) -> str:
    """Extract searchable plain text from a transcript entry."""
    msg = entry.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
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


def _transcript_index(db: sqlite3.Connection, jsonl_path: str) -> int:
    """Index transcript JSONL incrementally. Returns new entry count."""
    old_size, needs_work = _check_incremental(db, jsonl_path, "entries")
    if not needs_work and old_size == os.path.getsize(jsonl_path):
        return 0

    row = db.execute("SELECT MAX(idx) FROM entries").fetchone()
    next_idx: int = (row[0] + 1) if row[0] is not None else 0
    new_count = 0

    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        if old_size > 0:
            f.seek(old_size)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype: str = entry.get("type", "")
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
            output_tokens: int = usage.get("output_tokens", 0)

            db.execute(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    next_idx, old_size, etype,
                    msg.get("role", ""), entry.get("timestamp", ""),
                    msg.get("model", ""), entry.get("version", ""),
                    entry.get("gitBranch", ""),
                    json.dumps(entry, ensure_ascii=False),
                    plain_text, input_tokens, output_tokens,
                ),
            )
            db.execute(
                "INSERT INTO entries_fts(rowid, plain_text) VALUES (?, ?)",
                (next_idx, plain_text),
            )
            next_idx += 1
            new_count += 1

    _update_meta(db, jsonl_path, next_idx)
    return new_count


def _transcript_query_entries(
    db: sqlite3.Connection,
    page: Optional[int] = None,
    per_page: int = 50,
    filter_mode: str = "",
) -> dict[str, Any]:
    where = ""
    if filter_mode == "prompts":
        where = (
            "WHERE type='user' AND role='user' "
            "AND plain_text != '' AND plain_text NOT LIKE '<%' "
            "AND json_type(raw_json, '$.message.content') = 'text'"
        )

    total: int = db.execute(f"SELECT COUNT(*) FROM entries {where}").fetchone()[0]
    if total == 0:
        return asdict(EntriesResult(per_page=per_page))

    page_num, total_pages, offset = _paginate(total, page, per_page)
    rows = db.execute(
        f"SELECT idx, type, role, timestamp, raw_json FROM entries {where} "
        "ORDER BY idx LIMIT ? OFFSET ?",
        (per_page, offset),
    ).fetchall()

    entries = [
        {"idx": r[0], "type": r[1], "role": r[2], "timestamp": r[3], "raw_json": r[4]}
        for r in rows
    ]
    return asdict(EntriesResult(
        entries=entries, total=total, total_pages=total_pages,
        page=page_num, per_page=per_page,
    ))


def _transcript_query_search(
    db: sqlite3.Connection,
    search_term: str,
    page: int = 1,
    per_page: int = 50,
    sort: str = "relevance",
) -> dict[str, Any]:
    if not search_term:
        return asdict(TranscriptSearchResult(per_page=per_page))

    safe_term = _sanitize_fts5_query(search_term)
    try:
        total_results: int = db.execute(
            "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH ?",
            (safe_term,),
        ).fetchone()[0]
    except Exception:
        safe_term = f'"{search_term}"'
        total_results = db.execute(
            "SELECT COUNT(*) FROM entries_fts WHERE entries_fts MATCH ?",
            (safe_term,),
        ).fetchone()[0]

    if total_results == 0:
        return asdict(TranscriptSearchResult(query=search_term, per_page=per_page))

    page_num, total_pages, offset = _paginate(total_results, page, per_page)
    order = "ORDER BY e.idx" if sort == "time" else "ORDER BY f.rank"
    rows = db.execute(
        f"""SELECT e.idx, e.type, e.role, e.timestamp, e.raw_json, f.rank
           FROM entries_fts f JOIN entries e ON f.rowid = e.idx
           WHERE entries_fts MATCH ? {order} LIMIT ? OFFSET ?""",
        (safe_term, per_page, offset),
    ).fetchall()

    entries = [
        {"idx": r[0], "type": r[1], "role": r[2], "timestamp": r[3],
         "raw_json": r[4], "rank": r[5]}
        for r in rows
    ]
    return asdict(TranscriptSearchResult(
        entries=entries, total_results=total_results, total_pages=total_pages,
        page=page_num, per_page=per_page, query=search_term,
    ))


def _transcript_query_stats(db: sqlite3.Connection) -> dict[str, Any]:
    agg = db.execute("""SELECT
        SUM(input_tokens), SUM(output_tokens),
        MIN(timestamp), MAX(timestamp),
        (SELECT model FROM entries WHERE type='assistant' AND model != '' LIMIT 1),
        (SELECT version FROM entries WHERE version != '' LIMIT 1),
        (SELECT git_branch FROM entries WHERE git_branch != '' LIMIT 1)
    FROM entries""").fetchone()

    if not agg or agg[0] is None:
        return asdict(TranscriptStats())

    stats = TranscriptStats(
        input_tokens=agg[0] or 0, output_tokens=agg[1] or 0,
        first_ts=agg[2] or "", last_ts=agg[3] or "",
        model=agg[4] or "", version=agg[5] or "", git_branch=agg[6] or "",
    )

    stats.n_user = db.execute(
        "SELECT COUNT(*) FROM entries WHERE type='user' AND role='user' "
        "AND plain_text != '' AND json_type(raw_json, '$.message.content') = 'text'"
    ).fetchone()[0]

    files_modified: set[str] = set()
    for (raw,) in db.execute("SELECT raw_json FROM entries WHERE type='assistant'").fetchall():
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                stats.n_tool += 1
                name = item.get("name", "")
                ti = item.get("input", {})
                fp = ti.get("file_path", "") or ti.get("path", "")
                if fp:
                    files_modified.add(fp)
                if name == "Edit":
                    stats.n_edit += 1
                    n_old = len(ti.get("old_string", "").splitlines(True))
                    n_new = len(ti.get("new_string", "").splitlines(True))
                    overlap = min(n_old, n_new)
                    stats.lines_mod += overlap
                    stats.lines_add += n_new - overlap
                    stats.lines_del += n_old - overlap

    stats.n_files = len(files_modified)

    # Duration
    if stats.first_ts and stats.last_ts:
        try:
            fmt = "%Y-%m-%dT%H:%M:%S"
            t0 = datetime.strptime(stats.first_ts[:19], fmt)
            t1 = datetime.strptime(stats.last_ts[:19], fmt)
            delta = int((t1 - t0).total_seconds())
            if delta > 0:
                days, rem = divmod(delta, 86400)
                hours, rem = divmod(rem, 3600)
                mins = rem // 60
                if days > 0:
                    stats.duration = f"{days}d {hours}h"
                elif hours > 0:
                    stats.duration = f"{hours}h {mins}m"
                else:
                    stats.duration = f"{mins}m"
        except (ValueError, TypeError):
            pass

    return asdict(stats)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _run_transcript(args: argparse.Namespace) -> None:
    """Handle transcript subcommand."""
    if not os.path.exists(args.jsonl):
        empty: dict[str, Any]
        if args.query == "entries":
            empty = asdict(EntriesResult(per_page=args.per_page))
        elif args.query == "search":
            empty = asdict(TranscriptSearchResult(query=args.search, per_page=args.per_page))
        elif args.query == "stats":
            empty = asdict(TranscriptStats())
        elif args.query in ("entries+stats", "search+stats"):
            empty = asdict(TranscriptStats())
        else:
            empty = {}
        json.dump(empty, sys.stdout)
        return

    db = _setup_db(args.db)
    _transcript_create_tables(db)
    if os.path.getsize(args.jsonl) > 0:
        _transcript_index(db, args.jsonl)

    result: dict[str, Any]
    if args.query == "entries":
        result = _transcript_query_entries(db, page=args.page, per_page=args.per_page,
                                           filter_mode=args.filter_mode)
    elif args.query == "search":
        result = _transcript_query_search(db, args.search, page=args.page or 1,
                                          per_page=args.per_page, sort=args.sort)
    elif args.query == "stats":
        result = _transcript_query_stats(db)
    elif args.query == "entries+stats":
        result = _transcript_query_entries(db, page=args.page, per_page=args.per_page,
                                           filter_mode=args.filter_mode)
        result["stats"] = _transcript_query_stats(db)
    elif args.query == "search+stats":
        result = _transcript_query_search(db, args.search, page=args.page or 1,
                                          per_page=args.per_page, sort=args.sort)
        result["stats"] = _transcript_query_stats(db)
    else:
        result = {}

    json.dump(result, sys.stdout, ensure_ascii=False)
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JSONL transcript indexer with SQLite FTS5",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # Shared args
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--jsonl", required=True, help="Path to JSONL file")
    shared.add_argument("--db", required=True, help="Path to SQLite database")
    shared.add_argument("--page", type=int, default=None, help="Page number (default: last)")
    shared.add_argument("--per-page", type=int, default=50, help="Items per page")
    shared.add_argument("--search", type=str, default="", help="Search term")

    # Transcript subcommand
    t = sub.add_parser("transcript", parents=[shared], help="Index Claude transcripts")
    t.add_argument("--query", required=True,
                   choices=["entries", "search", "stats", "entries+stats", "search+stats"])
    t.add_argument("--filter", type=str, default="", dest="filter_mode",
                   help="Filter mode (e.g., 'prompts')")
    t.add_argument("--sort", type=str, default="relevance", choices=["relevance", "time"])

    args = parser.parse_args()

    if args.mode == "transcript":
        _run_transcript(args)


if __name__ == "__main__":
    main()
