#!/usr/bin/env python3
"""Team chat JSONL indexer with SQLite FTS5.

Standalone CLI script — indexes parsed Telegram team chat JSONL into SQLite
for fast pagination, BM25 search, and message-to-page lookup.
Designed to be called by the bridge via subprocess.

Python 3.9+ compatible. No external dependencies.
"""
import argparse
import json
import os
import sqlite3
import sys

AGENT_NAMES = frozenset({
    "chen", "finn", "geni", "hiro", "jin", "kai", "kelvin", "kenji",
    "lee", "luck", "mon", "noa", "ren", "ryo", "sora", "taro", "x", "yuki",
})


def _create_tables(db):
    """Create schema if not exists."""
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        idx INTEGER PRIMARY KEY,
        msg_id INTEGER UNIQUE,
        timestamp TEXT,
        timestamp_unix INTEGER,
        sender TEXT,
        display_sender TEXT,
        text TEXT,
        target_agents TEXT,
        has_command INTEGER DEFAULT 0,
        reply_to INTEGER,
        photo TEXT,
        file TEXT,
        file_name TEXT,
        reply_text TEXT DEFAULT ''
    )""")
    # Migrate: add reply_text column if missing (existing DBs)
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)").fetchall()}
    if "reply_text" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN reply_text TEXT DEFAULT ''")
    # FTS5 must cover: text, display_sender, file_name, reply_text
    has_fts = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if has_fts:
        # Check if FTS schema includes file_name (v2). If not, rebuild.
        fts_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()
        if fts_sql and "file_name" not in (fts_sql[0] or ""):
            db.execute("DROP TABLE messages_fts")
            has_fts = None
    if not has_fts:
        db.execute("""CREATE VIRTUAL TABLE messages_fts USING fts5(
            text, display_sender, file_name, reply_text,
            content=messages, content_rowid=idx,
            tokenize='unicode61'
        )""")
        # Backfill FTS from existing messages
        db.execute("""INSERT INTO messages_fts(rowid, text, display_sender, file_name, reply_text)
            SELECT idx, text, display_sender, COALESCE(file_name,''), COALESCE(reply_text,'')
            FROM messages""")
        db.commit()
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def _resolve_sender(sender, text):
    """Resolve display sender and clean text.

    - "Thinh" → "manager"
    - "beasts" with "agent:" prefix → agent name, strip prefix from text
    - Others → lowercase
    """
    if sender.lower() == "thinh":
        return "manager", text

    if sender.lower() == "beasts":
        first_line = text.split("\n")[0]
        if ":" in first_line[:30]:
            prefix = first_line.split(":")[0].strip().lower()
            if prefix in AGENT_NAMES:
                if "\n" in text:
                    cleaned = text[len(first_line) + 1:].strip()
                else:
                    cleaned = text[len(first_line.split(":")[0]) + 1:].strip()
                return prefix, cleaned

    return sender.lower(), text


def _index_jsonl(db, jsonl_path):
    """Index JSONL into SQLite, incrementally if possible.

    Returns number of new messages indexed.
    """
    file_size = os.path.getsize(jsonl_path)
    if file_size == 0:
        return 0

    row = db.execute("SELECT value FROM meta WHERE key='file_size'").fetchone()
    old_size = int(row[0]) if row else 0

    path_row = db.execute("SELECT value FROM meta WHERE key='jsonl_path'").fetchone()
    old_path = path_row[0] if path_row else ""
    abs_path = os.path.abspath(jsonl_path)
    if old_path and old_path != abs_path:
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM messages_fts")
        db.execute("DELETE FROM meta")
        old_size = 0

    if old_size == file_size:
        return 0

    if old_size > file_size:
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM messages_fts")
        db.execute("DELETE FROM meta")
        old_size = 0

    row = db.execute("SELECT MAX(idx) FROM messages").fetchone()
    next_idx = (row[0] + 1) if row[0] is not None else 0

    # First pass: collect all messages for reply text lookup
    all_new = []
    with open(jsonl_path, encoding="utf-8", errors="replace") as f:
        if old_size > 0:
            f.seek(old_size)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            all_new.append(msg)

    if not all_new:
        return 0

    # Build msg_id → text lookup for reply resolution (batch from DB + new messages)
    reply_ids = {m.get("reply_to") for m in all_new if m.get("reply_to")}
    reply_ids.discard(None)
    reply_text_map = {}
    if reply_ids:
        # Lookup from already-indexed messages
        placeholders = ",".join("?" * len(reply_ids))
        for row in db.execute(
            f"SELECT msg_id, text FROM messages WHERE msg_id IN ({placeholders})",
            list(reply_ids),
        ).fetchall():
            reply_text_map[row[0]] = (row[1] or "")[:200]
    # Also index new messages for intra-batch replies
    new_text_map = {}
    for m in all_new:
        mid = m.get("id")
        if mid:
            _, dt = _resolve_sender(m.get("from", ""), m.get("text", "").strip())
            new_text_map[mid] = dt[:200]

    new_count = 0
    for msg in all_new:
        sender = msg.get("from", "")
        text = msg.get("text", "").strip()
        display_sender, display_text = _resolve_sender(sender, text)
        target_agents = ",".join(msg.get("target_agents", []))
        reply_to = msg.get("reply_to")
        reply_text = ""
        if reply_to:
            reply_text = reply_text_map.get(reply_to, "") or new_text_map.get(reply_to, "")
        file_name = msg.get("file_name", "")

        db.execute(
            "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                next_idx,
                msg.get("id"),
                msg.get("timestamp", ""),
                msg.get("timestamp_unix", 0),
                sender,
                display_sender,
                display_text,
                target_agents,
                1 if msg.get("has_command") else 0,
                reply_to,
                msg.get("photo", ""),
                msg.get("file", ""),
                file_name,
                reply_text,
            ),
        )
        db.execute(
            "INSERT INTO messages_fts(rowid, text, display_sender, file_name, reply_text) VALUES (?, ?, ?, ?, ?)",
            (next_idx, display_text, display_sender, file_name, reply_text),
        )
        next_idx += 1
        new_count += 1

    db.execute("INSERT OR REPLACE INTO meta VALUES ('file_size', ?)", (str(file_size),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('entry_count', ?)", (str(next_idx),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('jsonl_path', ?)", (abs_path,))
    db.commit()
    return new_count


def _query_entries(db, page=None, per_page=50):
    """Paginated messages query."""
    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_pages = max(1, (total + per_page - 1) // per_page) if total > 0 else 0

    if total == 0:
        return {"messages": [], "total": 0, "total_pages": 0, "page": 1, "per_page": per_page}

    if page is None:
        page = total_pages
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = db.execute(
        """SELECT idx, msg_id, timestamp, timestamp_unix, sender, display_sender,
                  text, target_agents, has_command, reply_to, photo, file, file_name
           FROM messages ORDER BY idx LIMIT ? OFFSET ?""",
        (per_page, offset),
    ).fetchall()

    messages = []
    for r in rows:
        messages.append({
            "idx": r[0], "msg_id": r[1], "timestamp": r[2],
            "timestamp_unix": r[3], "sender": r[4], "display_sender": r[5],
            "text": r[6], "target_agents": r[7], "has_command": r[8],
            "reply_to": r[9], "photo": r[10] or "", "file": r[11] or "",
            "file_name": r[12] or "",
        })

    return {
        "messages": messages, "total": total,
        "total_pages": total_pages, "page": page, "per_page": per_page,
    }


def _sanitize_fts5_query(term):
    """Sanitize a user search term for FTS5 MATCH.

    FTS5 query syntax treats characters like . * - : ^ as operators.
    If the term contains any of these, wrap each token in double quotes
    so FTS5 treats them as literal strings.
    """
    import re
    # If already looks like an intentional FTS5 query (AND/OR/NOT/NEAR), pass through
    if re.search(r'\b(AND|OR|NOT|NEAR)\b', term):
        return term
    # If it contains FTS5 special chars, quote each whitespace-separated token
    if re.search(r'[.*:^(){}"\-]', term):
        tokens = term.split()
        quoted = []
        for t in tokens:
            # Strip existing quotes, re-wrap
            t = t.strip('"')
            if t:
                quoted.append(f'"{t}"')
        return " ".join(quoted)
    return term


def _query_search(db, search_term, page=1, per_page=50):
    """FTS5 BM25 search query."""
    if not search_term:
        return {"messages": [], "total_results": 0, "total_pages": 0, "page": 1, "query": ""}

    safe_term = _sanitize_fts5_query(search_term)
    try:
        count_row = db.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            (safe_term,),
        ).fetchone()
    except Exception:
        # Last resort: quote the entire term
        safe_term = f'"{search_term}"'
        count_row = db.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            (safe_term,),
        ).fetchone()
    total_results = count_row[0]
    total_pages = max(1, (total_results + per_page - 1) // per_page) if total_results > 0 else 0

    if total_results == 0:
        return {"messages": [], "total_results": 0, "total_pages": 0, "page": 1, "query": search_term}

    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = db.execute(
        """SELECT m.idx, m.msg_id, m.timestamp, m.timestamp_unix, m.sender,
                  m.display_sender, m.text, m.target_agents, m.has_command,
                  m.reply_to, m.photo, m.file, m.file_name, f.rank
           FROM messages_fts f
           JOIN messages m ON f.rowid = m.idx
           WHERE messages_fts MATCH ?
           ORDER BY f.rank
           LIMIT ? OFFSET ?""",
        (safe_term, per_page, offset),
    ).fetchall()

    messages = []
    for r in rows:
        messages.append({
            "idx": r[0], "msg_id": r[1], "timestamp": r[2],
            "timestamp_unix": r[3], "sender": r[4], "display_sender": r[5],
            "text": r[6], "target_agents": r[7], "has_command": r[8],
            "reply_to": r[9], "photo": r[10] or "", "file": r[11] or "",
            "file_name": r[12] or "", "rank": r[13],
        })

    return {
        "messages": messages, "total_results": total_results,
        "total_pages": total_pages, "page": page, "query": search_term,
    }


def _query_page_for_msg(db, msg_id, per_page=50):
    """Find which page a message ID falls on."""
    row = db.execute("SELECT idx FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
    if not row:
        return {"msg_id": msg_id, "idx": None, "page": None, "per_page": per_page}

    idx = row[0]
    page = (idx // per_page) + 1
    return {"msg_id": msg_id, "idx": idx, "page": page, "per_page": per_page}


def _query_msg_by_id(db, msg_id, per_page=50):
    """Fetch a single message by ID, including its page number."""
    row = db.execute(
        """SELECT idx, msg_id, timestamp, timestamp_unix, sender, display_sender,
                  text, target_agents, has_command, reply_to, photo, file, file_name
           FROM messages WHERE msg_id = ?""",
        (msg_id,),
    ).fetchone()
    if not row:
        return None
    idx = row[0]
    return {
        "idx": idx, "msg_id": row[1], "timestamp": row[2],
        "timestamp_unix": row[3], "sender": row[4], "display_sender": row[5],
        "text": row[6], "target_agents": row[7], "has_command": row[8],
        "reply_to": row[9], "photo": row[10] or "", "file": row[11] or "",
        "file_name": row[12] or "", "page": (idx // per_page) + 1,
    }


def _empty_entries(per_page):
    return {"messages": [], "total": 0, "total_pages": 0, "page": 1, "per_page": per_page}


def _empty_search(search_term):
    return {"messages": [], "total_results": 0, "total_pages": 0, "page": 1, "query": search_term}


def main():
    parser = argparse.ArgumentParser(description="Team chat JSONL indexer with SQLite FTS5")
    parser.add_argument("--jsonl", required=True, help="Path to parsed team chat JSONL")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument("--query", required=True,
                        choices=["entries", "search", "page-for-msg", "msg-by-id"],
                        help="Query type")
    parser.add_argument("--page", type=int, default=None, help="Page number (default: last page)")
    parser.add_argument("--per-page", type=int, default=50, help="Messages per page (default: 50)")
    parser.add_argument("--search", type=str, default="", help="Search term")
    parser.add_argument("--msg-id", type=int, default=None, help="Message ID (for page-for-msg)")
    args = parser.parse_args()

    # Handle missing JSONL
    if not os.path.exists(args.jsonl):
        if args.query == "entries":
            json.dump(_empty_entries(args.per_page), sys.stdout)
        elif args.query == "search":
            json.dump(_empty_search(args.search), sys.stdout)
        elif args.query in ("page-for-msg", "msg-by-id"):
            json.dump({"msg_id": args.msg_id, "idx": None, "page": None, "per_page": args.per_page}, sys.stdout)
        return

    # Ensure db directory exists
    db_dir = os.path.dirname(args.db)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    _create_tables(db)

    # Index new messages if needed
    if os.path.getsize(args.jsonl) > 0:
        _index_jsonl(db, args.jsonl)

    # Run query
    if args.query == "entries":
        result = _query_entries(db, page=args.page, per_page=args.per_page)
    elif args.query == "search":
        result = _query_search(db, args.search, page=args.page or 1,
                               per_page=args.per_page)
    elif args.query == "page-for-msg":
        result = _query_page_for_msg(db, args.msg_id, per_page=args.per_page)
    elif args.query == "msg-by-id":
        result = _query_msg_by_id(db, args.msg_id, per_page=args.per_page)
        if result is None:
            result = {"msg_id": args.msg_id, "idx": None, "page": None}

    json.dump(result, sys.stdout, ensure_ascii=False)
    db.close()


if __name__ == "__main__":
    main()
