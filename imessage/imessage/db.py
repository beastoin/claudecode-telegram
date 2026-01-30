"""
iMessage chat.db reader.

Read-only SQLite access to ~/Library/Messages/chat.db.
Queries new messages since a given rowid.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Apple's epoch starts 2001-01-01 (978307200 seconds after Unix epoch)
APPLE_EPOCH_OFFSET = 978307200

@dataclass
class Message:
    """Parsed iMessage from chat.db."""
    rowid: int
    guid: str
    text: str
    handle: str  # Phone number or email
    is_from_me: bool
    date: datetime
    chat_guid: Optional[str] = None

def get_db_path() -> str:
    """Get Messages database path."""
    return os.environ.get(
        'IMESSAGE_DB_PATH',
        os.path.expanduser('~/Library/Messages/chat.db')
    )

def apple_time_to_datetime(apple_time: int) -> datetime:
    """Convert Apple's timestamp to datetime.

    chat.db timestamps are nanoseconds since 2001-01-01.
    We detect the format by checking if the result is reasonable (2001-2100).
    """
    # Try nanoseconds first (modern macOS)
    unix_time_ns = apple_time / 1e9 + APPLE_EPOCH_OFFSET
    if 978307200 < unix_time_ns < 4102444800:  # 2001-01-01 to 2100-01-01
        return datetime.fromtimestamp(unix_time_ns)

    # Try seconds (very old format)
    unix_time_s = apple_time + APPLE_EPOCH_OFFSET
    if 978307200 < unix_time_s < 4102444800:
        return datetime.fromtimestamp(unix_time_s)

    # Fallback to nanoseconds interpretation
    return datetime.fromtimestamp(unix_time_ns)

def connect_readonly(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Connect to chat.db in read-only mode."""
    path = db_path or get_db_path()

    if not os.path.exists(path):
        raise FileNotFoundError(f"Messages database not found: {path}")

    # Read-only connection with immutable flag
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn

def get_new_messages(
    since_rowid: int = 0,
    db_path: Optional[str] = None,
    limit: int = 100
) -> list[Message]:
    """
    Get new messages since a given rowid.

    Args:
        since_rowid: Only return messages with rowid > this value
        db_path: Optional path to chat.db
        limit: Max messages to return

    Returns:
        List of Message objects, oldest first
    """
    conn = connect_readonly(db_path)

    try:
        cursor = conn.execute("""
            SELECT
                m.ROWID,
                m.guid,
                m.text,
                m.is_from_me,
                m.date,
                h.id as handle_id,
                c.chat_identifier
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.ROWID > ?
              AND m.text IS NOT NULL
              AND m.text != ''
            ORDER BY m.ROWID ASC
            LIMIT ?
        """, (since_rowid, limit))

        messages = []
        for row in cursor:
            messages.append(Message(
                rowid=row['ROWID'],
                guid=row['guid'],
                text=row['text'],
                handle=row['handle_id'] or row['chat_identifier'] or 'unknown',
                is_from_me=bool(row['is_from_me']),
                date=apple_time_to_datetime(row['date']),
                chat_guid=row['chat_identifier']
            ))

        return messages

    finally:
        conn.close()

def get_latest_rowid(db_path: Optional[str] = None) -> int:
    """Get the latest message rowid."""
    conn = connect_readonly(db_path)

    try:
        cursor = conn.execute("SELECT MAX(ROWID) FROM message")
        row = cursor.fetchone()
        return row[0] or 0
    finally:
        conn.close()

def check_permissions(db_path: Optional[str] = None) -> tuple[bool, str]:
    """
    Check if we have permission to read chat.db.

    Returns:
        (success, message) tuple
    """
    path = db_path or get_db_path()

    if not os.path.exists(path):
        return False, f"Database not found: {path}"

    try:
        conn = connect_readonly(path)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        conn.close()
        return True, "OK"
    except sqlite3.OperationalError as e:
        if "unable to open database" in str(e).lower():
            return False, (
                "Full Disk Access required.\n"
                "Go to System Settings > Privacy & Security > Full Disk Access\n"
                "and enable access for Terminal (or your terminal app)."
            )
        return False, str(e)
    except Exception as e:
        return False, str(e)
