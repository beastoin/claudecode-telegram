"""
iMessage integration modules.

- db: Read chat.db
- sender: Send via AppleScript
- receiver: Watch for incoming messages
- watch: File system watcher
"""

from .db import Message, get_new_messages, get_latest_rowid, check_permissions as check_db_permissions
from .sender import send_message, send_chunked, check_permissions as check_sender_permissions
from .receiver import MessageReceiver, get_allowed_handles

__all__ = [
    'Message',
    'get_new_messages',
    'get_latest_rowid',
    'check_db_permissions',
    'send_message',
    'send_chunked',
    'check_sender_permissions',
    'MessageReceiver',
    'get_allowed_handles',
]
