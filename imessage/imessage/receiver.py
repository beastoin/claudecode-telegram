"""
iMessage receiver.

Watches chat.db for new incoming messages and routes them to tmux sessions.
"""

import os
import logging
import threading
from typing import Callable, Optional

from .db import get_db_path, get_new_messages, get_latest_rowid, Message
from .watch import ChatDBWatcher, DebounceCallback

logger = logging.getLogger(__name__)

class MessageReceiver:
    """
    Receive and route incoming iMessages.

    Watches chat.db for new messages and calls a handler function
    for each incoming message (excluding messages from self).
    """

    def __init__(
        self,
        on_message: Callable[[Message], None],
        db_path: Optional[str] = None,
        poll_interval: float = 2.0,
        allowed_handles: Optional[set[str]] = None
    ):
        """
        Initialize receiver.

        Args:
            on_message: Callback for each new incoming message
            db_path: Path to chat.db (default from env)
            poll_interval: Seconds between polls
            allowed_handles: Set of allowed sender handles (None = all)
        """
        self.on_message = on_message
        self.db_path = db_path or get_db_path()
        self.poll_interval = poll_interval
        self.allowed_handles = allowed_handles

        # Track last processed message
        self._last_rowid = 0
        self._lock = threading.Lock()

        # File watcher with debounce
        self._debounce = DebounceCallback(self._check_new_messages, delay=0.3)
        self._watcher = ChatDBWatcher(
            self.db_path,
            self._debounce,
            poll_interval=poll_interval
        )

    def start(self, from_latest: bool = True):
        """
        Start receiving messages.

        Args:
            from_latest: If True, only process new messages from now.
                        If False, process all messages in DB.
        """
        if from_latest:
            # Start from current latest message
            self._last_rowid = get_latest_rowid(self.db_path)
            logger.info(f"Starting receiver from rowid {self._last_rowid}")
        else:
            self._last_rowid = 0
            logger.info("Starting receiver from beginning")

        self._watcher.start()

    def stop(self):
        """Stop receiving messages."""
        self._watcher.stop()
        self._debounce.cancel()
        logger.info("Receiver stopped")

    def _check_new_messages(self):
        """Check for and process new messages."""
        with self._lock:
            try:
                messages = get_new_messages(
                    since_rowid=self._last_rowid,
                    db_path=self.db_path
                )

                for msg in messages:
                    # Update last rowid
                    if msg.rowid > self._last_rowid:
                        self._last_rowid = msg.rowid

                    # Skip messages from self
                    if msg.is_from_me:
                        logger.debug(f"Skipping self-authored message {msg.rowid}")
                        continue

                    # Check allowlist
                    if self.allowed_handles and msg.handle not in self.allowed_handles:
                        logger.debug(f"Skipping message from non-allowed handle: {msg.handle}")
                        continue

                    # Route message
                    logger.info(f"Received message from {msg.handle}: {msg.text[:50]}...")
                    try:
                        self.on_message(msg)
                    except Exception as e:
                        logger.error(f"Error handling message {msg.rowid}: {e}")

            except Exception as e:
                logger.error(f"Error checking new messages: {e}")

    @property
    def last_rowid(self) -> int:
        """Get last processed message rowid."""
        return self._last_rowid

    @last_rowid.setter
    def last_rowid(self, value: int):
        """Set last processed message rowid."""
        with self._lock:
            self._last_rowid = value


def parse_allowed_handles(env_value: Optional[str]) -> Optional[set[str]]:
    """
    Parse IMESSAGE_ALLOWED_HANDLES env var.

    Format: comma-separated phone numbers or emails
    Example: "+1234567890,user@icloud.com"

    Returns:
        Set of allowed handles, or None if no restriction
    """
    if not env_value:
        return None

    handles = set()
    for h in env_value.split(','):
        h = h.strip()
        if h:
            handles.add(h)

    return handles if handles else None


def get_allowed_handles() -> Optional[set[str]]:
    """Get allowed handles from environment."""
    return parse_allowed_handles(os.environ.get('IMESSAGE_ALLOWED_HANDLES'))
