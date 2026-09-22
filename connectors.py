"""Polling connectors for claudecode-telegram bridge.

Gmail and GitHub connectors that poll external services for new messages,
parse @worker mentions, and deliver to workers via bridge callbacks.

Flat module — no subpackages. Import directly:
    from connectors import GmailConnector, GitHubConnector
"""

from __future__ import annotations

import abc
import base64
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional, Protocol, TypedDict

# ---------------------------------------------------------------------------
# Types — concrete callback signatures and typed data structures
# ---------------------------------------------------------------------------


class Attachment(TypedDict, total=False):
    """A downloaded file attachment."""
    path: str
    filename: str
    mimeType: str


class ConnectorMetadata(TypedDict, total=False):
    """Metadata passed alongside connector messages."""
    number: Optional[str]
    repo: str
    comment_id: int


class IssueContext(TypedDict):
    """Context extracted from a GitHub comment."""
    number: str
    kind: str
    url: str


class ConnectorStatus(TypedDict):
    """Status dict returned by BaseConnector.status()."""
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool


class MessageCallback(Protocol):
    """on_message(targets, html_text, plain_text, attachments, metadata=None)."""
    def __call__(
        self,
        targets: list[str],
        html_text: str,
        plain_text: str,
        attachments: list[Attachment],
        metadata: Optional[ConnectorMetadata] = None,
    ) -> None: ...


class AlertCallback(Protocol):
    """Callback for sending alert notifications."""
    def __call__(self, text: str) -> None: ...


class WorkerListCallback(Protocol):
    """Callback that returns the set of registered worker names."""
    def __call__(self) -> set[str]: ...

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONSECUTIVE_FAIL_WARN = 3
CONSECUTIVE_FAIL_REBOOTSTRAP = 5
_MAX_POLL_INTERVAL = 3600
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB
_MAX_SEEN_IDS = 500
_PRUNE_KEEP = 200


# ---------------------------------------------------------------------------
# BaseConnector
# ---------------------------------------------------------------------------

class BaseConnector(abc.ABC):
    """Abstract polling connector with preflight, failure tracking, and alerts."""

    connector_name: str = "base"

    def __init__(
        self,
        sender_filter: str,
        poll_interval: int,
        on_message: MessageCallback,
        get_registered_workers: WorkerListCallback,
        on_alert: Optional[AlertCallback] = None,
    ) -> None:
        if not sender_filter or not sender_filter.strip():
            raise ValueError(f"{self.connector_name}: sender_filter is required (security: cannot be empty)")
        if poll_interval < 1 or poll_interval > _MAX_POLL_INTERVAL:
            raise ValueError(f"{self.connector_name}: poll_interval must be 1–{_MAX_POLL_INTERVAL}, got {poll_interval}")
        self.sender_filter: str = sender_filter.strip().lower()
        self.poll_interval: int = poll_interval
        self.on_message: MessageCallback = on_message
        self.get_registered_workers: WorkerListCallback = get_registered_workers
        self.on_alert: Optional[AlertCallback] = on_alert
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures: int = 0
        self._alert_sent: bool = False
        self._lock: threading.RLock = threading.RLock()

    # -- Abstract interface (subclasses MUST override) --

    @abc.abstractmethod
    def preflight_check(self) -> tuple[bool, str]:
        """Check if the connector can reach its external service."""

    @abc.abstractmethod
    def poll_once(self) -> None:
        """Run one polling cycle."""

    @abc.abstractmethod
    def extract_sender(self, message: dict[str, Any]) -> str:
        """Return the lowercased sender identity from a message."""

    # -- Lifecycle --

    def _on_preflight_ok(self) -> None:
        """Called after successful preflight, before poll loop starts."""

    def start(self) -> threading.Thread:
        """Spawn the polling thread. Returns the thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(f"{self.connector_name}: already running (call stop() first)")
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True,
                name=f"{self.connector_name}-poller",
            )
            self._thread.start()
            return self._thread

    def stop(self) -> None:
        """Signal the polling thread to stop and wait up to 5s."""
        self._stop_event.set()
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=5)

    def restart(self) -> tuple[bool, str]:
        """Stop, reset, re-preflight, start. Returns (ok, message)."""
        self.stop()
        with self._lock:
            # Wait for old thread to actually die before starting new one
            if self._thread is not None and self._thread.is_alive():
                return False, f"{self.connector_name}: old thread did not exit in time"
            self._stop_event.clear()
            self._consecutive_failures = 0
            self._alert_sent = False
        ok, msg = self.preflight_check()
        if not ok:
            return False, msg
        with self._lock:
            self._thread = threading.Thread(
                target=self._poll_loop, kwargs={"skip_preflight": True},
                daemon=True, name=f"{self.connector_name}-poller",
            )
            self._thread.start()
        return True, msg

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> ConnectorStatus:
        """Return a status dict for API responses."""
        with self._lock:
            return ConnectorStatus(
                name=self.connector_name,
                running=self.running,
                sender_filter=self.sender_filter,
                poll_interval=self.poll_interval,
                consecutive_failures=self._consecutive_failures,
                alert_sent=self._alert_sent,
            )

    # -- Sender filtering --

    def is_allowed_sender(self, message: dict[str, Any]) -> bool:
        return self.extract_sender(message) == self.sender_filter

    # -- Mention parsing --

    def parse_mentions(self, text: str) -> tuple[list[str], str]:
        """Parse @worker mentions from text, return (targets, cleaned_text)."""
        if not text:
            return [], ""
        registered = self.get_registered_workers()
        found: list[str] = []
        for match in re.finditer(r'@([a-zA-Z0-9_-]+)', text):
            name = match.group(1).lower()
            if name in registered and name not in found:
                found.append(name)
        if not found:
            return [], text
        found_set = set(found)
        cleaned = re.sub(
            r'@([a-zA-Z0-9_-]+)',
            lambda m: '' if m.group(1).lower() in found_set else m.group(0),
            text,
        )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return found, cleaned

    # -- Alert lifecycle --

    def _send_alert(self, text: str) -> None:
        tag = f"[{self.connector_name}]"
        print(f"{tag} ALERT: {text}")
        alert_fn = self.on_alert  # Capture ref outside lock
        if alert_fn is None:
            return
        with self._lock:
            if self._alert_sent:
                return
            # Mark sent BEFORE calling out (prevents re-entry on slow callback)
            self._alert_sent = True
        try:
            alert_fn(f"{tag} {text}")
        except Exception as e:
            print(f"{tag} Failed to send alert: {e}")
            with self._lock:
                self._alert_sent = False  # Rollback — allow retry

    def _clear_alert(self) -> None:
        with self._lock:
            was_alerted = self._alert_sent
            self._alert_sent = False
        if was_alerted:
            tag = f"[{self.connector_name}]"
            if self.on_alert:
                try:
                    self.on_alert(f"{tag} Recovered — polling resumed")
                except Exception as e:
                    print(f"{tag} Failed to send recovery alert: {e}")

    # -- Failure tracking --

    def track_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            should_alert = self._consecutive_failures >= CONSECUTIVE_FAIL_WARN and not self._alert_sent
        if should_alert:
            self._send_alert(f"Polling failing ({self._consecutive_failures} consecutive errors)")

    def track_success(self) -> None:
        with self._lock:
            was_failing = self._consecutive_failures > 0
            self._consecutive_failures = 0
        if was_failing:
            self._clear_alert()

    # -- Poll loop --

    def _poll_loop(self, skip_preflight: bool = False) -> None:
        """Main polling loop. skip_preflight=True when called from restart()."""
        tag = f"[{self.connector_name}]"
        if not skip_preflight:
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
        else:
            print(f"{tag} Preflight OK (restart)")
        self._on_preflight_ok()
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                print(f"{tag} Poll error: {e}")
                self.track_failure()
            self._stop_event.wait(self.poll_interval)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _escape_html(text: str) -> str:
    """Escape all HTML-significant characters including quotes."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _sanitize_filename(name: str) -> str:
    """Strip path separators and traversal from a filename."""
    name = os.path.basename(name)  # strip directory components
    name = name.replace("..", "_").replace("/", "_").replace("\\", "_")
    name = re.sub(r'[^\w.\-]', '_', name)  # keep only safe chars
    return name or "attachment"


def _truncate(text: str, limit: int = 1500) -> str:
    """Truncate text at a line boundary, with marker."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Find last newline to avoid mid-line cut
    nl = cut.rfind('\n')
    if nl > limit // 2:
        cut = cut[:nl]
    return cut + "\n… (truncated)"


def _get_header(headers: list[dict[str, Any]], name: str) -> str:
    """Safely extract a header value from Gmail headers list."""
    target = name.lower()
    for h in headers:
        if not isinstance(h, dict):
            continue
        h_name = h.get("name")
        if not isinstance(h_name, str):
            continue
        if h_name.lower() == target:
            h_value = h.get("value")
            return str(h_value) if isinstance(h_value, str) else ""
    return ""


def _safe_json_loads_list(raw: str) -> list[dict[str, Any]]:
    """Parse JSON that might be a single array or multiple concatenated arrays
    (gh --paginate emits one JSON array per page)."""
    raw = raw.strip()
    if not raw:
        return []
    # Try single parse first (common case)
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []  # unexpected type
    except json.JSONDecodeError:
        pass
    # gh --paginate: concatenated arrays like ][
    # Split on ][ boundary and parse each
    items: list[dict[str, Any]] = []
    for chunk in re.split(r'\]\s*\[', raw):
        chunk = chunk.strip()
        if not chunk.startswith('['):
            chunk = '[' + chunk
        if not chunk.endswith(']'):
            chunk = chunk + ']'
        try:
            parsed = json.loads(chunk)
            if isinstance(parsed, list):
                items.extend(item for item in parsed if isinstance(item, dict))
        except json.JSONDecodeError:
            continue
    return items


# ---------------------------------------------------------------------------
# GmailConnector
# ---------------------------------------------------------------------------

class GmailConnector(BaseConnector):
    """Polls Gmail via gws CLI for new emails from a whitelisted sender."""

    connector_name: str = "gmail"

    def __init__(
        self,
        gws_bin: str,
        from_filter: str,
        poll_interval: int,
        on_message: MessageCallback,
        get_registered_workers: WorkerListCallback,
        on_alert: Optional[AlertCallback] = None,
        history_file: str = "",
    ) -> None:
        super().__init__(
            sender_filter=from_filter,
            poll_interval=poll_interval,
            on_message=on_message,
            get_registered_workers=get_registered_workers,
            on_alert=on_alert,
        )
        self.gws_bin: str = gws_bin
        self._history_id: Optional[str] = None
        self._history_file_path: str = history_file or os.path.join(
            os.path.expanduser("~"), ".cache", "beast", "email", "gmail_history_id",
        )

    def preflight_check(self) -> tuple[bool, str]:
        if not os.path.isfile(self.gws_bin):
            return False, f"gws binary not found at {self.gws_bin}"
        if not os.access(self.gws_bin, os.X_OK):
            return False, f"gws binary not executable: {self.gws_bin}"
        result = self._run_gws("getProfile", "--params", '{"userId":"me"}')
        if result is None:
            return False, "gws auth failed — token may be expired (run: gws gmail users getProfile)"
        return True, f"OK (email={result.get('emailAddress', '?')})"

    def status(self) -> dict[str, Any]:  # extends ConnectorStatus with history_id
        with self._lock:
            s: dict[str, Any] = dict(super().status())
            s["history_id"] = self._history_id
        return s

    # -- gws CLI wrapper --

    def _run_gws(self, *args: str, json_body: Optional[str] = None) -> Optional[dict[str, Any]]:
        cmd: list[str] = [self.gws_bin, "gmail", "users"] + list(args)
        if json_body:
            cmd.extend(["--json", json_body])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                stderr = result.stderr[:200]
                print(f"[gmail] gws error: {stderr}")
                if "token" in stderr.lower() or "auth" in stderr.lower() or "expired" in stderr.lower():
                    print("[gmail] ⚠ TOKEN MAY BE EXPIRED — run: gws gmail users getProfile --params '{\"userId\":\"me\"}'")
                return None
            parsed = json.loads(result.stdout)
            if not isinstance(parsed, dict):
                print(f"[gmail] gws returned non-dict: {type(parsed).__name__}")
                return None
            return parsed
        except subprocess.TimeoutExpired:
            print("[gmail] gws call timed out")
            return None
        except json.JSONDecodeError as e:
            print(f"[gmail] gws JSON parse failed: {e}")
            return None
        except OSError as e:
            print(f"[gmail] gws call failed: {e}")
            return None

    # -- History ID persistence (atomic write) --

    def _save_history_id(self) -> None:
        if not self._history_id:
            return
        path = self._history_file_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(self._history_id)
            os.replace(tmp, path)
        except OSError as e:
            print(f"[gmail] Failed to save historyId: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _load_history_id(self) -> Optional[str]:
        try:
            with open(self._history_file_path, "r") as f:
                hid = f.read().strip()
                if hid:
                    return hid
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[gmail] Failed to load historyId: {e}")
        return None

    def _bootstrap_history_id(self, skip_disk: bool = False) -> Optional[str]:
        if not skip_disk:
            saved = self._load_history_id()
            if saved:
                print(f"[gmail] Resumed historyId={saved} from disk")
                return saved
        profile = self._run_gws("getProfile", "--params", '{"userId":"me"}')
        if profile and "historyId" in profile:
            hid = str(profile["historyId"])
            self._history_id = hid
            self._save_history_id()
            return hid
        return None

    # -- Message extraction (safe header access) --

    def extract_sender(self, message: dict[str, Any]) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return ""
        from_val = _get_header(headers, "from")
        if not from_val:
            return ""
        match = re.search(r'<([^>]+)>', from_val)
        if match:
            return match.group(1).lower()
        return from_val.strip().lower()

    def extract_sender_name(self, message: dict[str, Any]) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return ""
        from_val = _get_header(headers, "from")
        if not from_val:
            return ""
        match = re.match(r'^([^<]+)\s*<', from_val)
        if match:
            return match.group(1).strip().strip('"')
        return from_val.split("@")[0]

    def extract_subject(self, message: dict[str, Any]) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return ""
        return _get_header(headers, "subject")

    def extract_message_id(self, message: dict[str, Any]) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return ""
        return _get_header(headers, "message-id")

    def is_sent_message(self, message: dict[str, Any]) -> bool:
        label_ids = message.get("labelIds", [])
        if not isinstance(label_ids, list):
            return False
        return "SENT" in label_ids

    def extract_body_text(self, message: dict[str, Any]) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        return self._find_text_part(payload)

    def _find_text_part(self, part: dict[str, Any]) -> str:
        raw_mime = part.get("mimeType")
        mime_type: str = str(raw_mime) if isinstance(raw_mime, str) else ""
        if mime_type == "text/plain":
            body = part.get("body")
            if not isinstance(body, dict):
                return ""
            raw_data = body.get("data")
            data: str = str(raw_data) if isinstance(raw_data, str) else ""
            if data:
                try:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                except Exception:
                    return ""
            return ""
        parts = part.get("parts")
        if isinstance(parts, list):
            for sub in parts:
                if isinstance(sub, dict):
                    text = self._find_text_part(sub)
                    if text:
                        return text
        return ""

    def extract_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        payload = message.get("payload")
        if isinstance(payload, dict):
            self._find_attachments(payload, attachments)
        return attachments

    def _find_attachments(self, part: dict[str, Any], result: list[dict[str, Any]]) -> None:
        raw_fn = part.get("filename")
        filename: str = str(raw_fn) if isinstance(raw_fn, str) else ""
        body = part.get("body")
        if not isinstance(body, dict):
            return
        raw_att_id = body.get("attachmentId")
        att_id: str = str(raw_att_id) if isinstance(raw_att_id, str) else ""
        raw_size = body.get("size")
        size: int = int(raw_size) if isinstance(raw_size, int) else 0
        if filename and att_id:
            if size > _MAX_ATTACHMENT_BYTES:
                print(f"[gmail] Skipping oversized attachment: {filename} ({size} bytes)")
                return
            raw_mime = part.get("mimeType")
            mime: str = str(raw_mime) if isinstance(raw_mime, str) else ""
            result.append({
                "filename": _sanitize_filename(filename),
                "mimeType": mime,
                "size": size,
                "attachmentId": att_id,
            })
        parts = part.get("parts")
        if isinstance(parts, list):
            for sub in parts:
                if isinstance(sub, dict):
                    self._find_attachments(sub, result)

    def is_inbox_unread(self, label_ids: list[str]) -> bool:
        return "INBOX" in label_ids and "UNREAD" in label_ids

    # -- Body cleaning --

    def _clean_body(self, text: str) -> str:
        text = text.replace('\r\n', '\n')
        text = re.sub(r'<https?://[^>]+>', '', text)
        text = re.sub(r'\[image:[^\]]*\]', '', text)
        text = re.sub(r'Get Outlook for iOS\s*', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _strip_reply_chain(self, text: str) -> str:
        patterns = [
            r'\s+On \w{3}, \w{3,9} \d{1,2}, \d{4}[ ,].{5,80} wrote:',
            r'\s+On \d{1,2} \w{3,9} \d{4}[ ,].{5,80} wrote:',
            r'\n>[ >].*(?:\n>[ >].*)*',
            r'\n-{2,}\s*Reply above this line\s*-{2,}',
            r'\n_{2,}\nFrom:.*?\nSent:',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.DOTALL)
            if m:
                text = text[:m.start()]
        return text.strip()

    def _detect_forward_split(self, body: str) -> tuple[Optional[str], Optional[str]]:
        gmail_marker = "---------- Forwarded message ---------"
        if gmail_marker in body:
            parts = body.split(gmail_marker, 1)
            return parts[0].strip(), parts[1].strip()
        outlook_match = re.search(
            r'\n_{3,}\n\s*From:.*?\nSent:.*?\nTo:.*?\nSubject:',
            body, re.DOTALL,
        )
        if outlook_match:
            return body[:outlook_match.start()].strip(), body[outlook_match.start():].strip()
        generic_match = re.search(
            r'\n-{2,}\s*(?:Original Message|Forwarded)\s*-{2,}',
            body, re.IGNORECASE,
        )
        if generic_match:
            return body[:generic_match.start()].strip(), body[generic_match.start():].strip()
        return None, None

    # -- Formatting --

    def format_email_message(self, body: str, subject: str, thread_id: str = "") -> tuple[str, str]:
        """Returns (html_text, plain_text) for Telegram and worker delivery."""
        body = self._clean_body(body)
        manager_text, forwarded_content = self._detect_forward_split(body)

        thread_tag = f" [thread:{thread_id}]" if thread_id else ""

        if forwarded_content is not None:
            if manager_text:
                manager_text = self._strip_reply_chain(manager_text)
            forwarded_content = self._clean_body(forwarded_content)
            forwarded_content = _truncate(forwarded_content, 1200)

            fwd_subject = subject if re.match(r'(?i)^(fwd?|forwarded):', subject) else f"Fwd: {subject}"
            html_parts = [f"📧 <b>{_escape_html(fwd_subject)}</b>{thread_tag}"]
            plain_parts = [f"manager (via email):{thread_tag}"]
            if manager_text:
                html_parts.append(f"\n{_escape_html(manager_text)}")
                plain_parts.append(manager_text)
            html_parts.append(f"<blockquote>{_escape_html(forwarded_content)}</blockquote>")
            plain_parts.append(f"--- Forwarded: {subject} ---")
            plain_parts.append(forwarded_content)
            return "\n".join(html_parts), "\n".join(plain_parts)

        body = self._strip_reply_chain(body)
        body = _truncate(body)
        html = f"📧 <b>{_escape_html(subject)}</b>{thread_tag}\n\n{_escape_html(body)}"
        plain = f"manager (via email):{thread_tag} {body}"
        return html, plain

    def _format_sent_reply(self, body: str, subject: str, thread_id: str, message: dict[str, Any]) -> tuple[str, str]:
        body = self._clean_body(body)
        body = self._strip_reply_chain(body)
        body = _truncate(body)
        sender_name = self.extract_sender_name(message)
        thread_tag = f" [thread:{thread_id}]" if thread_id else ""
        html = f"✉️ <b>Sent:</b> {_escape_html(subject)}{thread_tag}\n\n{_escape_html(body)}"
        plain = f"{sender_name} (sent via email):{thread_tag} {body}"
        return html, plain

    # -- Gmail API operations --

    def get_message(self, msg_id: str) -> Optional[dict[str, Any]]:
        params = json.dumps({"userId": "me", "id": msg_id, "format": "full"})
        return self._run_gws("messages", "get", "--params", params)

    def mark_as_read(self, msg_id: str) -> bool:
        params = json.dumps({"userId": "me", "id": msg_id})
        body = json.dumps({"removeLabelIds": ["UNREAD"]})
        result = self._run_gws("messages", "modify", "--params", params, json_body=body)
        return result is not None

    def _get_new_message_ids(self) -> tuple[Optional[list[str]], Optional[str]]:
        """Return (message_ids, new_history_id). Caller advances historyId after processing."""
        if not self._history_id:
            return [], None
        params = json.dumps({"userId": "me", "startHistoryId": self._history_id})
        data = self._run_gws("history", "list", "--params", params)
        if data is None:
            return None, None
        msg_ids: list[str] = []
        seen: set[str] = set()
        for entry in data.get("history", []):
            if not isinstance(entry, dict):
                continue
            for added in entry.get("messagesAdded", []):
                if not isinstance(added, dict):
                    continue
                msg = added.get("message", {})
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id", ""))
                label_ids = msg.get("labelIds", [])
                if not isinstance(label_ids, list):
                    label_ids = []
                if msg_id and msg_id not in seen:
                    if self.is_inbox_unread(label_ids) or "SENT" in label_ids:
                        seen.add(msg_id)
                        msg_ids.append(msg_id)
        new_hid = str(data["historyId"]) if "historyId" in data else None
        return msg_ids, new_hid

    def _format_attachment_line(self, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return ""
        names: list[str] = []
        for a in attachments:
            raw = a.get("filename")
            names.append(str(raw) if isinstance(raw, str) and raw else "?")
        return f"\n[{len(attachments)} attachment(s): {', '.join(names)}]"

    def _download_attachment(self, msg_id: str, att: dict[str, Any]) -> Optional[str]:
        raw_att_id = att.get("attachmentId")
        att_id: str = str(raw_att_id) if isinstance(raw_att_id, str) else ""
        if not att_id:
            return None
        params = json.dumps({"userId": "me", "messageId": msg_id, "id": att_id})
        data = self._run_gws("messages", "attachments", "get", "--params", params)
        if not data:
            return None
        raw_b64 = data.get("data")
        if not isinstance(raw_b64, str) or not raw_b64:
            return None
        try:
            decoded = base64.urlsafe_b64decode(raw_b64)
        except Exception as e:
            print(f"[gmail] decode attachment failed: {e}")
            return None
        if len(decoded) > _MAX_ATTACHMENT_BYTES:
            print(f"[gmail] Attachment too large: {len(decoded)} bytes")
            return None
        raw_fn = att.get("filename")
        raw_name = _sanitize_filename(str(raw_fn) if isinstance(raw_fn, str) else "attachment")
        # Prefix with msg_id to prevent clobbering across messages
        filename = f"{msg_id[:8]}_{raw_name}" if msg_id else raw_name
        att_dir = os.path.join(os.path.expanduser("~"), ".cache", "beast", "email", "attachments")
        os.makedirs(att_dir, exist_ok=True)
        path = os.path.join(att_dir, filename)
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(decoded)
            os.replace(tmp, path)
            return path
        except OSError as e:
            print(f"[gmail] save attachment failed: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return None

    # -- Message processing --

    def _process_message(self, msg_id: str) -> None:
        message = self.get_message(msg_id)
        if not message:
            raise RuntimeError(f"get_message({msg_id}) returned None — transient API failure")

        is_sent = self.is_sent_message(message)
        if not is_sent and not self.is_allowed_sender(message):
            return

        body = self.extract_body_text(message)
        subject = self.extract_subject(message)
        raw_tid = message.get("threadId")
        thread_id: str = str(raw_tid) if isinstance(raw_tid, str) else ""
        message_id = self.extract_message_id(message)
        attachments = self.extract_attachments(message)
        if not body.strip():
            return

        if is_sent:
            html_text, plain_text = self._format_sent_reply(body, subject, thread_id, message)
            downloaded: list[Attachment] = []
            for att in attachments:
                path = self._download_attachment(msg_id, att)
                if path:
                    downloaded.append(Attachment(path=path, filename=att.get("filename", ""), mimeType=att.get("mimeType", "")))
            self.on_message([], html_text, plain_text, downloaded)
            return

        # Strip reply chain BEFORE mention parsing to avoid routing on quoted @mentions
        body = self._strip_reply_chain(body)
        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_email_message(cleaned, subject, thread_id)
        else:
            html_text, plain_text = self.format_email_message(body, subject, thread_id)
        att_line = self._format_attachment_line(attachments)
        html_text += att_line
        plain_text += att_line

        if targets and message_id:
            # Shell-safe: quote all interpolated values
            safe_subject = subject.replace("'", "'\\''")
            safe_thread = thread_id.replace("'", "'\\''")
            safe_msgid = message_id.replace("'", "'\\''")
            reply_hint = f"\n\nReply (prefer HTML): beast email send -s 'Re: {safe_subject}' --thread-id '{safe_thread}' --in-reply-to '{safe_msgid}' --html-file /tmp/reply.html"
            reply_hint += f"\nReply (plain text): beast email send -s 'Re: {safe_subject}' --thread-id '{safe_thread}' --in-reply-to '{safe_msgid}' --body 'your reply'"
            plain_text += reply_hint

        downloaded: list[Attachment] = []
        for att in attachments:
            path = self._download_attachment(msg_id, att)
            if path:
                downloaded.append(Attachment(path=path, filename=att.get("filename", ""), mimeType=att.get("mimeType", "")))
                print(f"[gmail] attachment: {att.get('filename', '?')} -> {path}")

        self.on_message(targets, html_text, plain_text, downloaded)
        if not self.mark_as_read(msg_id):
            print(f"[gmail] Warning: failed to mark message {msg_id} as read")

    # -- Polling --

    def poll_once(self) -> None:
        msg_ids, new_hid = self._get_new_message_ids()
        if msg_ids is None:
            self.track_failure()
            if self._consecutive_failures >= CONSECUTIVE_FAIL_REBOOTSTRAP:
                print(f"[gmail] ⚠ {self._consecutive_failures} failures — attempting re-bootstrap")
                new_id = self._bootstrap_history_id(skip_disk=True)
                if new_id:
                    self._history_id = new_id
                    self.track_success()
                    print(f"[gmail] Re-bootstrap OK (historyId={new_id})")
                else:
                    self._send_alert("Gmail re-bootstrap failed — token expired, needs manual renewal (ask geni to run gws renewal)")
            return
        self.track_success()
        had_errors: bool = False
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                print(f"[gmail] Error processing message {msg_id}: {e}")
                had_errors = True
        # Advance historyId AFTER all messages processed (skip on errors to allow retry)
        if new_hid and not had_errors:
            self._history_id = new_hid
            self._save_history_id()
        elif had_errors:
            print("[gmail] historyId NOT advanced — some messages failed, will retry next poll")

    def _on_preflight_ok(self) -> None:
        self._history_id = self._bootstrap_history_id()
        if not self._history_id:
            print("[gmail] Bootstrap failed, retrying in 60s...")
            if self._stop_event.wait(60):
                return
            self._history_id = self._bootstrap_history_id()
            if not self._history_id:
                self._send_alert("Gmail connector disabled — cannot get historyId")
                self._stop_event.set()
                return
        print(f"[gmail] Started (interval={self.poll_interval}s, from={self.sender_filter}, historyId={self._history_id})")
        self._catchup_unread()

    def _catchup_unread(self) -> None:
        q = f"from:{self.sender_filter} is:unread in:inbox newer_than:1d"
        params = json.dumps({"userId": "me", "maxResults": 5, "q": q})
        data = self._run_gws("messages", "list", "--params", params)
        if not data or "messages" not in data:
            return
        msg_ids: list[str] = [m["id"] for m in data["messages"] if isinstance(m, dict) and "id" in m]
        if not msg_ids:
            return
        print(f"[gmail] Catch-up: {len(msg_ids)} unread from {self.sender_filter}")
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                print(f"[gmail] Catch-up error {msg_id}: {e}")

    def stop(self) -> None:
        super().stop()  # Wait for poll thread to exit before saving
        self._save_history_id()


# ---------------------------------------------------------------------------
# GitHubConnector
# ---------------------------------------------------------------------------

class GitHubConnector(BaseConnector):
    """Polls GitHub issue/PR comments via gh CLI for a configured user."""

    connector_name: str = "github"

    def __init__(
        self,
        repo: str,
        from_user: str,
        poll_interval: int,
        on_message: MessageCallback,
        get_registered_workers: WorkerListCallback,
        on_alert: Optional[AlertCallback] = None,
        state_file: str = "",
        gh_bin: str = "",
    ) -> None:
        super().__init__(
            sender_filter=from_user,
            poll_interval=poll_interval,
            on_message=on_message,
            get_registered_workers=get_registered_workers,
            on_alert=on_alert,
        )
        self.gh_bin: str = gh_bin or "gh"
        self.repo: str = repo
        self._state_file: str = state_file
        self._last_poll_time: Optional[str] = None
        self._seen_ids: set[int] = set()

    def preflight_check(self) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                [self.gh_bin, "api", f"/repos/{self.repo}", "--jq", ".id"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return False, f"gh api failed: {result.stderr.strip()}"
            return True, "OK"
        except subprocess.TimeoutExpired:
            return False, "gh api timed out during preflight"
        except FileNotFoundError:
            return False, f"gh binary not found: {self.gh_bin}"
        except OSError as e:
            return False, f"gh error: {e}"

    def status(self) -> dict[str, Any]:  # extends ConnectorStatus with repo fields
        with self._lock:
            s: dict[str, Any] = dict(super().status())
            s["repo"] = self.repo
            s["last_poll_time"] = self._last_poll_time
            s["seen_ids_count"] = len(self._seen_ids)
        return s

    # -- State persistence --

    def _load_state(self) -> bool:
        if not self._state_file:
            return False
        try:
            with open(self._state_file) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False
            lpt = data.get("last_poll_time")
            if isinstance(lpt, str) and lpt:
                self._last_poll_time = lpt
            for sid in data.get("seen_ids", []):
                if isinstance(sid, int):
                    self._seen_ids.add(sid)
            return bool(self._last_poll_time)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return False

    def _save_state(self) -> None:
        if not self._state_file:
            return
        dirname = os.path.dirname(self._state_file)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        state: dict[str, Any] = {
            "last_poll_time": self._last_poll_time,
            "seen_ids": sorted(self._seen_ids)[-_MAX_SEEN_IDS:],
        }
        tmp = self._state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self._state_file)
        except OSError as e:
            print(f"[github] Failed to save state: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _on_preflight_ok(self) -> None:
        restored = self._load_state()
        if restored:
            print(f"[github] Restart — restored state: {len(self._seen_ids)} seen IDs, since={self._last_poll_time}")
        else:
            self._last_poll_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            seed, _ = self._get_all_comments(self._last_poll_time)
            if seed:
                for c in seed:
                    cid = c.get("id")
                    if isinstance(cid, int):
                        self._seen_ids.add(cid)
                print(f"[github] First run — seeded {len(seed)} existing comments as seen")
            else:
                print(f"[github] First run — no comments to seed")
            self._save_state()
        print(f"[github] Started (interval={self.poll_interval}s, repo={self.repo}, user={self.sender_filter}, since={self._last_poll_time})")

    # -- GitHub API --

    def _gh_api(self, endpoint: str, timeout: int = 30) -> Optional[str]:
        try:
            result = subprocess.run(
                [self.gh_bin, "api", endpoint, "--paginate"],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[github] gh api error: {result.stderr.strip()}")
                return None
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            print(f"[github] gh api timed out: {endpoint}")
            return None
        except OSError as e:
            print(f"[github] gh api exception: {e}")
            return None

    def _get_all_comments(self, since: str) -> tuple[Optional[list[dict[str, Any]]], bool]:
        """Returns (comments, is_complete). is_complete=False means partial fetch."""
        issue_raw = self._gh_api(
            f"/repos/{self.repo}/issues/comments?since={since}&sort=updated&direction=asc&per_page=100",
        )
        pr_raw = self._gh_api(
            f"/repos/{self.repo}/pulls/comments?since={since}&sort=updated&direction=asc&per_page=100",
        )
        if issue_raw is None and pr_raw is None:
            return None, False
        is_complete: bool = issue_raw is not None and pr_raw is not None
        if not is_complete:
            failed = "issues" if issue_raw is None else "pulls"
            print(f"[github] Partial fetch — {failed} comments failed, processing available data")
        seen: set[int] = set()
        merged: list[dict[str, Any]] = []
        for raw in [issue_raw, pr_raw]:
            if not raw:
                continue
            comments = _safe_json_loads_list(raw)
            for c in comments:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id", 0)
                if isinstance(cid, int) and cid not in seen:
                    seen.add(cid)
                    merged.append(c)
        return merged, is_complete

    # -- Message extraction --

    def extract_sender(self, comment: dict[str, Any]) -> str:
        user = comment.get("user")
        if not isinstance(user, dict):
            return ""
        login = user.get("login", "")
        return str(login).lower() if login else ""

    def extract_issue_context(self, comment: dict[str, Any]) -> IssueContext:
        if comment.get("issue_num") and comment.get("kind"):
            return IssueContext(
                number=str(comment["issue_num"]),
                kind=str(comment["kind"]),
                url=str(comment.get("html_url", "")),
            )
        issue_url: str = str(comment.get("issue_url", "") or comment.get("pull_request_url", ""))
        html_url: str = str(comment.get("html_url", ""))
        number: str = ""
        kind: str = "comment"
        if "/issues/" in issue_url:
            number = issue_url.rsplit("/", 1)[-1]
            kind = "issue"
        elif "/pulls/" in issue_url or "/pull/" in html_url:
            if "/pulls/" in issue_url:
                number = issue_url.rsplit("/", 1)[-1]
            elif "/pull/" in html_url:
                parts = html_url.split("/pull/")
                if len(parts) > 1:
                    number = parts[1].split("/")[0]
                    if "#" in number:
                        number = number.split("#")[0]
            kind = "PR"
        return IssueContext(number=number, kind=kind, url=html_url)

    # -- Formatting --

    def _md_to_telegram_html(self, md: str) -> str:
        text = _escape_html(md)
        text = re.sub(r'```[a-zA-Z]*\n(.*?)```', lambda m: f'<pre>{m.group(1).rstrip()}</pre>', text, flags=re.DOTALL)
        text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
        text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', lambda m: m.group(1) or '(image)', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', lambda m: f'<a href="{_escape_html(m.group(2))}">{m.group(1)}</a>', text)
        text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
        text = re.sub(r'(?<!\w)\*([^*\n]+)\*(?!\w)', r'<i>\1</i>', text)
        text = re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)', r'<i>\1</i>', text)
        lines = text.split('\n')
        result: list[str] = []
        in_quote = False
        quote_lines: list[str] = []
        for line in lines:
            if re.match(r'&gt;\s?', line):
                in_quote = True
                quote_lines.append(re.sub(r'^&gt;\s?', '', line))
            else:
                if in_quote:
                    result.append(f'<blockquote>{chr(10).join(quote_lines)}</blockquote>')
                    quote_lines = []
                    in_quote = False
                result.append(line)
        if in_quote:
            result.append(f'<blockquote>{chr(10).join(quote_lines)}</blockquote>')
        text = '\n'.join(result)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def format_comment(self, body: str, context: IssueContext) -> tuple[str, str]:
        body = _truncate(body)
        kind = context.get("kind", "comment")
        number = context.get("number", "")
        url = context.get("url", "")
        tag = f"#{number}" if number else ""
        link = f'<a href="{_escape_html(url)}">#{number}</a>' if url and number else tag
        html = f"🔔 <b>GitHub {kind} {link}</b>\n\n{self._md_to_telegram_html(body)}"
        plain = f"manager (via GitHub {kind} {tag}): {body}"
        return html, plain

    # -- Comment processing --

    def _process_comment(self, comment: dict[str, Any]) -> None:
        cid = comment.get("id", 0)
        if not isinstance(cid, int) or cid == 0:
            return
        if cid in self._seen_ids:
            return
        # Check sender BEFORE marking as seen — failed delivery won't permanently skip
        if not self.is_allowed_sender(comment):
            self._seen_ids.add(cid)  # Non-target sender: mark seen, no retry needed
            return
        body: str = str(comment.get("body", "")).strip()
        if not body:
            self._seen_ids.add(cid)
            return
        context = self.extract_issue_context(comment)
        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_comment(cleaned, context)
        else:
            html_text, plain_text = self.format_comment(body, context)
        if targets and context.get("number"):
            num = context["number"]
            # Shell-safe: quote interpolated values
            safe_repo = self.repo.replace("'", "'\\''")
            reply_hint = f"\n\nView: beast github show {num} --repo '{safe_repo}'"
            reply_hint += f"\nReply: beast github comment {num} --repo '{safe_repo}' --body 'your reply'"
            plain_text += reply_hint
        metadata = ConnectorMetadata(number=context["number"], repo=self.repo, comment_id=cid)
        self.on_message(targets, html_text, plain_text, [], metadata=metadata)
        # Mark seen AFTER successful delivery
        self._seen_ids.add(cid)
        print(f"[github] {context['kind']} #{context['number']}: -> {targets or 'Telegram only'}")

    # -- Polling --

    def poll_once(self) -> None:
        if not self._last_poll_time:
            return
        all_comments, fetch_complete = self._get_all_comments(self._last_poll_time)
        if all_comments is None:
            self.track_failure()
            return
        self.track_success()
        # Process all comments BEFORE advancing time
        had_errors: bool = False
        for comment in all_comments:
            try:
                self._process_comment(comment)
            except Exception as e:
                print(f"[github] Error processing comment {comment.get('id')}: {e}")
                had_errors = True
        # Advance time only on full success (complete fetch + no processing errors)
        if not had_errors and fetch_complete:
            self._last_poll_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            reasons: list[str] = []
            if not fetch_complete:
                reasons.append("partial fetch")
            if had_errors:
                reasons.append("processing errors")
            print(f"[github] poll_time NOT advanced — {', '.join(reasons)}, will retry")
        self._prune_seen_ids()
        self._save_state()

    def _prune_seen_ids(self) -> None:
        if len(self._seen_ids) > _MAX_SEEN_IDS:
            # sorted() preserves newest (highest) IDs
            self._seen_ids = set(sorted(self._seen_ids)[-_PRUNE_KEEP:])

    def stop(self) -> None:
        super().stop()  # Wait for poll thread to exit before saving
        self._save_state()
