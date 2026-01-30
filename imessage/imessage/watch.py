"""
File system watcher for chat.db changes.

Uses FSEvents on macOS for near-real-time detection,
with polling fallback.
"""

import os
import time
import threading
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

class ChatDBWatcher:
    """Watch chat.db for changes using FSEvents or polling."""

    def __init__(
        self,
        db_path: str,
        callback: Callable[[], None],
        poll_interval: float = 2.0
    ):
        """
        Initialize watcher.

        Args:
            db_path: Path to chat.db
            callback: Function to call when changes detected
            poll_interval: Seconds between polls (fallback)
        """
        self.db_path = os.path.expanduser(db_path)
        self.callback = callback
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_mtime: float = 0
        self._use_fsevents = False

        # Check for FSEvents support (macOS only)
        try:
            import fsevents  # type: ignore
            self._use_fsevents = True
            logger.info("Using FSEvents for file watching")
        except ImportError:
            logger.info("FSEvents not available, using polling")

    def start(self):
        """Start watching in a background thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()

        if self._use_fsevents:
            self._thread = threading.Thread(target=self._watch_fsevents, daemon=True)
        else:
            self._thread = threading.Thread(target=self._watch_poll, daemon=True)

        self._thread.start()
        logger.info(f"Started watching {self.db_path}")

    def stop(self):
        """Stop watching."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Stopped watching")

    def _watch_fsevents(self):
        """Watch using macOS FSEvents."""
        try:
            import fsevents  # type: ignore

            # Watch the directory containing chat.db
            watch_dir = os.path.dirname(self.db_path)
            db_name = os.path.basename(self.db_path)
            wal_name = db_name + "-wal"

            def callback(path, mask):
                # Only trigger on chat.db or chat.db-wal changes
                filename = os.path.basename(path)
                if filename in (db_name, wal_name):
                    logger.debug(f"FSEvents: change detected in {filename}")
                    self.callback()

            observer = fsevents.Observer()
            observer.schedule(callback, watch_dir)
            observer.start()

            try:
                while not self._stop_event.is_set():
                    time.sleep(0.5)
            finally:
                observer.stop()
                observer.join()

        except Exception as e:
            logger.error(f"FSEvents error, falling back to polling: {e}")
            self._watch_poll()

    def _watch_poll(self):
        """Watch using polling fallback."""
        # Also watch the WAL file
        wal_path = self.db_path + "-wal"

        # Initialize mtimes
        self._last_mtime = self._get_mtime(self.db_path)
        last_wal_mtime = self._get_mtime(wal_path)

        while not self._stop_event.is_set():
            try:
                current_mtime = self._get_mtime(self.db_path)
                current_wal_mtime = self._get_mtime(wal_path)

                if current_mtime > self._last_mtime or current_wal_mtime > last_wal_mtime:
                    logger.debug("Poll: change detected")
                    self._last_mtime = current_mtime
                    last_wal_mtime = current_wal_mtime
                    self.callback()

            except Exception as e:
                logger.warning(f"Poll error: {e}")

            # Wait for next poll
            self._stop_event.wait(self.poll_interval)

    def _get_mtime(self, path: str) -> float:
        """Get file modification time, or 0 if not exists."""
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0


class DebounceCallback:
    """Debounce rapid callbacks into a single call."""

    def __init__(self, callback: Callable[[], None], delay: float = 0.5):
        """
        Initialize debouncer.

        Args:
            callback: Function to call after debounce
            delay: Seconds to wait before calling
        """
        self.callback = callback
        self.delay = delay
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def __call__(self):
        """Trigger a debounced callback."""
        with self._lock:
            if self._timer:
                self._timer.cancel()

            self._timer = threading.Timer(self.delay, self._execute)
            self._timer.start()

    def _execute(self):
        """Execute the callback."""
        with self._lock:
            self._timer = None
        self.callback()

    def cancel(self):
        """Cancel any pending callback."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
