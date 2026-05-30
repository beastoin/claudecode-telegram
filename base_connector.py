"""Base class for polling connectors (GitHub, Gmail, etc.)."""

import re
import threading
from typing import Callable, Optional

CONSECUTIVE_FAIL_WARN = 3


class BaseConnector:

    connector_name = "base"

    def __init__(
        self,
        sender_filter: str,
        poll_interval: int,
        on_message: Callable,
        get_registered_workers: Callable,
        on_alert: Optional[Callable] = None,
    ):
        if not sender_filter or not sender_filter.strip():
            raise ValueError(f"{self.connector_name}: sender_filter is required (security: cannot be empty)")
        self.sender_filter = sender_filter.lower()
        self.poll_interval = poll_interval
        self.on_message = on_message
        self.get_registered_workers = get_registered_workers
        self.on_alert = on_alert
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._alert_sent = False

    def preflight_check(self) -> tuple:
        raise NotImplementedError

    def poll_once(self):
        raise NotImplementedError

    def _on_preflight_ok(self):
        """Called after successful preflight, before poll loop starts."""
        pass

    def parse_mentions(self, text: str) -> tuple:
        if not text:
            return [], ""
        registered = self.get_registered_workers()
        found = []
        for match in re.finditer(r'@([a-zA-Z0-9_-]+)', text):
            name = match.group(1).lower()
            if name in registered and name not in found:
                found.append(name)
        if not found:
            return [], text
        cleaned = re.sub(
            r'@([a-zA-Z0-9_-]+)',
            lambda m: '' if m.group(1).lower() in set(found) else m.group(0),
            text
        )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return found, cleaned

    def _send_alert(self, text: str):
        tag = f"[{self.connector_name}]"
        print(f"{tag} ALERT: {text}")
        if self.on_alert and not self._alert_sent:
            try:
                self.on_alert(f"{tag} {text}")
                self._alert_sent = True
            except Exception as e:
                print(f"{tag} Failed to send alert: {e}")

    def _clear_alert(self):
        if self._alert_sent:
            self._alert_sent = False
            tag = f"[{self.connector_name}]"
            if self.on_alert:
                try:
                    self.on_alert(f"{tag} Recovered — polling resumed")
                except Exception:
                    pass

    def track_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= CONSECUTIVE_FAIL_WARN and not self._alert_sent:
            self._send_alert(f"Polling failing ({self._consecutive_failures} consecutive errors)")

    def track_success(self):
        if self._consecutive_failures > 0:
            self._clear_alert()
            self._consecutive_failures = 0

    def _poll_loop(self):
        tag = f"[{self.connector_name}]"
        ok, msg = self.preflight_check()
        if not ok:
            print(f"{tag} Preflight FAILED: {msg}")
            print(f"{tag} Retrying in 60s...")
            if self._stop_event.wait(60):
                return
            ok, msg = self.preflight_check()
            if not ok:
                self._send_alert(f"{self.connector_name} disabled — {msg}")
                return
        print(f"{tag} Preflight OK: {msg}")
        self._on_preflight_ok()
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                print(f"{tag} Poll error: {e}")
            self._stop_event.wait(self.poll_interval)

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name=f"{self.connector_name}-poller",
        )
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
