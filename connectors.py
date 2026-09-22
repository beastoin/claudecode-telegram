"""Polling connectors for claudecode-telegram bridge.

Gmail and GitHub connectors that poll external services for new messages,
parse @worker mentions, and deliver to workers via bridge callbacks.

Flat module — no subpackages. Import directly:
    from connectors import GmailConnector, GitHubConnector
"""

from __future__ import annotations

import abc
import base64
import binascii
import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from typing import Generic, Optional, Protocol, TypedDict, TypeVar, cast

# ---------------------------------------------------------------------------
# Types — strongly typed data structures for all external API shapes
# ---------------------------------------------------------------------------

# -- Gmail API types --

class GmailHeader(TypedDict, total=False):
    """A single Gmail message header (name/value pair)."""
    name: str
    value: str


class GmailBody(TypedDict, total=False):
    """Body of a Gmail MIME part."""
    data: str
    size: int
    attachmentId: str


class GmailPayloadPart(TypedDict, total=False):
    """A MIME part in a Gmail message payload (recursive via 'parts')."""
    mimeType: str
    filename: str
    body: GmailBody
    headers: list[GmailHeader]
    parts: list[GmailPayloadPart]


class GmailMessage(TypedDict, total=False):
    """A Gmail message as returned by messages.get (format=full)."""
    id: str
    threadId: str
    labelIds: list[str]
    payload: GmailPayloadPart
    historyId: str


class GmailMessageRef(TypedDict, total=False):
    """Lightweight message reference inside history entries."""
    message: GmailMessage


class GmailHistoryEntry(TypedDict, total=False):
    """One entry in a Gmail history list response."""
    messagesAdded: list[GmailMessageRef]


class GmailHistoryResponse(TypedDict, total=False):
    """Response from Gmail history.list API."""
    history: list[GmailHistoryEntry]
    historyId: str


class GmailProfile(TypedDict, total=False):
    """Response from Gmail users.getProfile."""
    emailAddress: str
    historyId: str


class GmailAttachmentData(TypedDict, total=False):
    """Response from Gmail messages.attachments.get."""
    data: str


class GmailMessageListResponse(TypedDict, total=False):
    """Response from Gmail messages.list."""
    messages: list[GmailMessage]


# -- Gmail internal types --

class GmailAttachmentInfo(TypedDict):
    """Parsed attachment metadata extracted from a Gmail message."""
    filename: str
    mimeType: str
    size: int
    attachmentId: str


# -- GitHub API types --

class GithubUser(TypedDict, total=False):
    """GitHub user object (only login used)."""
    login: str


class GithubComment(TypedDict, total=False):
    """A GitHub issue or PR review comment."""
    id: int
    body: str
    user: GithubUser
    html_url: str
    issue_url: str
    pull_request_url: str
    # Pre-shaped fields (set by bridge when forwarding)
    issue_num: str
    kind: str


# -- GitHub internal types --

class GithubState(TypedDict):
    """Persisted state for GitHubConnector."""
    last_poll_time: Optional[str]
    seen_ids: list[int]


# -- Connector output types --

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


class GmailStatus(TypedDict):
    """Status dict returned by GmailConnector.status()."""
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool
    history_id: Optional[str]


class GithubStatus(TypedDict):
    """Status dict returned by GitHubConnector.status()."""
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool
    repo: str
    last_poll_time: Optional[str]
    seen_ids_count: int


# -- Callback protocols --

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

def _atomic_write_text(path: str, content: str) -> None:
    """Write content atomically: tmp (0o600) + fsync + rename + dir fsync."""
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".tmp_", suffix=".tmp")
    renamed: bool = False
    try:
        os.fchmod(fd, 0o600)  # Restrictive permissions before writing
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        renamed = True
        # fsync parent directory for rename durability (best-effort)
        try:
            dir_fd = os.open(dirname, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # Rename succeeded; dir fsync failure is non-fatal
    except BaseException:
        if not renamed:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write bytes atomically: tmp (0o600) + fsync + rename + dir fsync."""
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".tmp_", suffix=".tmp")
    renamed: bool = False
    try:
        os.fchmod(fd, 0o600)  # Restrictive permissions before writing
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        renamed = True
        # fsync parent directory for rename durability (best-effort)
        try:
            dir_fd = os.open(dirname, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass  # Rename succeeded; dir fsync failure is non-fatal
    except BaseException:
        if not renamed:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


# Repo name pattern: owner/name (GitHub: start/end with alnum, no ".." or "." runs)
_REPO_SEGMENT = r'[a-zA-Z0-9](?:[a-zA-Z0-9_-]|\.(?!\.))*[a-zA-Z0-9]'
_REPO_PATTERN = re.compile(rf'^(?:{_REPO_SEGMENT}|[a-zA-Z0-9])/(?:{_REPO_SEGMENT}|[a-zA-Z0-9])$')
_MAX_FILENAME_LEN = 200  # Limit attachment filename length
# Urlsafe base64: A-Z a-z 0-9 _ - followed by 0-2 padding chars
_B64_URLSAFE = re.compile(r'^[A-Za-z0-9_-]+={0,2}$')
_MAX_MIME_DEPTH = 10  # Max recursion depth for MIME part traversal
_MAX_MIME_PARTS = 100  # Max total MIME parts to process

CONSECUTIVE_FAIL_WARN = 3
CONSECUTIVE_FAIL_REBOOTSTRAP = 5
_MAX_POLL_INTERVAL = 3600
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB
_MAX_SEEN_IDS = 500
_PRUNE_KEEP = 200

# Type variable for the message type each connector handles
M = TypeVar("M")

# ---------------------------------------------------------------------------
# BaseConnector
# ---------------------------------------------------------------------------

class BaseConnector(abc.ABC, Generic[M]):
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
        if isinstance(poll_interval, bool) or poll_interval < 1 or poll_interval > _MAX_POLL_INTERVAL:
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
    def extract_sender(self, message: M) -> str:
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

    def is_allowed_sender(self, message: M) -> bool:
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
    """Strip path separators, traversal, and limit length."""
    name = os.path.basename(name)  # strip directory components
    name = name.replace("..", "_").replace("/", "_").replace("\\", "_")
    name = re.sub(r'[^\w.\-]', '_', name)  # keep only safe chars
    name = name or "attachment"
    if len(name) > _MAX_FILENAME_LEN:
        root, ext = os.path.splitext(name)
        if len(ext) >= _MAX_FILENAME_LEN:
            ext = ext[:10]  # Truncate absurdly long extensions
        name = root[:_MAX_FILENAME_LEN - len(ext)] + ext
    return name


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


def _get_header(headers: list[GmailHeader], name: str) -> str:
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


def _safe_json_loads_list(raw: str) -> list[GithubComment]:
    """Parse JSON that might be a single array or multiple concatenated arrays
    (gh --paginate emits one JSON array per page). Validates each item."""
    raw = raw.strip()
    if not raw:
        return []
    # Try single parse first (common case)
    raw_items: list[object] = []
    try:
        result: object = json.loads(raw)
        if isinstance(result, list):
            raw_items = result
        else:
            return []  # unexpected type
    except json.JSONDecodeError:
        # gh --paginate: concatenated arrays like ][
        # Split on ][ boundary and parse each
        for chunk in re.split(r'\]\s*\[', raw):
            chunk = chunk.strip()
            if not chunk.startswith('['):
                chunk = '[' + chunk
            if not chunk.endswith(']'):
                chunk = chunk + ']'
            try:
                parsed: object = json.loads(chunk)
                if isinstance(parsed, list):
                    raw_items.extend(parsed)
            except json.JSONDecodeError:
                continue
    # Validate each item into a typed GithubComment
    comments: list[GithubComment] = []
    for item in raw_items:
        if isinstance(item, dict):
            comments.append(_validate_github_comment(cast(dict[str, object], item)))
    return comments


# ---------------------------------------------------------------------------
# Validation — narrow dict[str, object] to concrete TypedDicts at boundaries
# ---------------------------------------------------------------------------

def _str_val(raw: dict[str, object], key: str) -> str:
    """Extract a string value from a raw dict, or return empty string."""
    val = raw.get(key)
    return str(val) if isinstance(val, str) else ""


def _int_val(raw: dict[str, object], key: str) -> int:
    """Extract an int value from a raw dict, or return 0. Rejects bool."""
    val = raw.get(key)
    return int(val) if isinstance(val, int) and not isinstance(val, bool) else 0


def _str_list_val(raw: dict[str, object], key: str) -> list[str]:
    """Extract a list[str] from a raw dict, validating each element."""
    val = raw.get(key)
    if not isinstance(val, list):
        return []
    return [str(item) for item in val if isinstance(item, str)]


def _validate_gmail_body(raw: dict[str, object]) -> GmailBody:
    """Validate and narrow a raw dict into a GmailBody."""
    body = GmailBody()
    data = _str_val(raw, "data")
    if data:
        body["data"] = data
    size = _int_val(raw, "size")
    if size:
        body["size"] = size
    att_id = _str_val(raw, "attachmentId")
    if att_id:
        body["attachmentId"] = att_id
    return body


def _validate_gmail_header(raw: dict[str, object]) -> GmailHeader:
    """Validate and narrow a raw dict into a GmailHeader."""
    header = GmailHeader()
    name = _str_val(raw, "name")
    if name:
        header["name"] = name
    value = _str_val(raw, "value")
    if value:
        header["value"] = value
    return header


def _validate_payload_part(
    raw: dict[str, object], depth: int = 0, counter: Optional[list[int]] = None,
) -> GmailPayloadPart:
    """Validate and narrow a raw dict into a GmailPayloadPart. Depth+count limited."""
    if counter is None:
        counter = [0]  # Root call: start fresh counter
    counter[0] += 1
    result = GmailPayloadPart()
    if depth > _MAX_MIME_DEPTH or counter[0] > _MAX_MIME_PARTS:
        return result  # Truncate overly deep/wide MIME trees at validation boundary
    mime = _str_val(raw, "mimeType")
    if mime:
        result["mimeType"] = mime
    filename = _str_val(raw, "filename")
    if filename:
        result["filename"] = filename
    body_raw = raw.get("body")
    if isinstance(body_raw, dict):
        result["body"] = _validate_gmail_body(cast(dict[str, object], body_raw))
    headers_raw = raw.get("headers")
    if isinstance(headers_raw, list):
        headers: list[GmailHeader] = []
        for h in headers_raw:
            if isinstance(h, dict):
                headers.append(_validate_gmail_header(cast(dict[str, object], h)))
        result["headers"] = headers
    parts_raw = raw.get("parts")
    if isinstance(parts_raw, list):
        parts: list[GmailPayloadPart] = []
        for p in parts_raw:
            if isinstance(p, dict):
                parts.append(_validate_payload_part(cast(dict[str, object], p), depth + 1, counter))
        result["parts"] = parts
    return result


def _validate_gmail_message(raw: dict[str, object]) -> GmailMessage:
    """Validate and narrow a raw API dict into a GmailMessage TypedDict."""
    result = GmailMessage()
    msg_id = _str_val(raw, "id")
    if msg_id:
        result["id"] = msg_id
    thread_id = _str_val(raw, "threadId")
    if thread_id:
        result["threadId"] = thread_id
    label_ids = _str_list_val(raw, "labelIds")
    if label_ids:
        result["labelIds"] = label_ids
    history_id = _str_val(raw, "historyId")
    if history_id:
        result["historyId"] = history_id
    payload_raw = raw.get("payload")
    if isinstance(payload_raw, dict):
        result["payload"] = _validate_payload_part(cast(dict[str, object], payload_raw))
    return result


def _validate_gmail_history_response(raw: dict[str, object]) -> GmailHistoryResponse:
    """Validate and narrow a raw dict into a GmailHistoryResponse."""
    result = GmailHistoryResponse()
    hid = _str_val(raw, "historyId")
    if hid:
        result["historyId"] = hid
    history_raw = raw.get("history")
    if isinstance(history_raw, list):
        entries: list[GmailHistoryEntry] = []
        for entry_raw in history_raw:
            if not isinstance(entry_raw, dict):
                continue
            entry = GmailHistoryEntry()
            added_raw = cast(dict[str, object], entry_raw).get("messagesAdded")
            if isinstance(added_raw, list):
                refs: list[GmailMessageRef] = []
                for added in added_raw:
                    if not isinstance(added, dict):
                        continue
                    msg_raw = cast(dict[str, object], added).get("message")
                    if isinstance(msg_raw, dict):
                        ref = GmailMessageRef()
                        ref["message"] = _validate_gmail_message(cast(dict[str, object], msg_raw))
                        refs.append(ref)
                entry["messagesAdded"] = refs
            entries.append(entry)
        result["history"] = entries
    return result


def _validate_gmail_attachment_data(raw: dict[str, object]) -> GmailAttachmentData:
    """Validate and narrow a raw dict into a GmailAttachmentData."""
    result = GmailAttachmentData()
    data = _str_val(raw, "data")
    if data:
        result["data"] = data
    return result


def _validate_gmail_message_list_response(raw: dict[str, object]) -> GmailMessageListResponse:
    """Validate and narrow a raw dict into a GmailMessageListResponse."""
    result = GmailMessageListResponse()
    msgs_raw = raw.get("messages")
    if isinstance(msgs_raw, list):
        msgs: list[GmailMessage] = []
        for m in msgs_raw:
            if isinstance(m, dict):
                msgs.append(_validate_gmail_message(cast(dict[str, object], m)))
        result["messages"] = msgs
    return result


def _validate_gmail_profile(raw: dict[str, object]) -> GmailProfile:
    """Validate and narrow a raw dict into a GmailProfile."""
    return GmailProfile(
        emailAddress=_str_val(raw, "emailAddress"),
        historyId=_str_val(raw, "historyId"),
    )


def _validate_github_state(raw: dict[str, object]) -> GithubState:
    """Validate and narrow a raw dict into a GithubState."""
    lpt_raw = raw.get("last_poll_time")
    lpt: Optional[str] = str(lpt_raw) if isinstance(lpt_raw, str) and lpt_raw else None
    seen_raw = raw.get("seen_ids")
    seen: list[int] = []
    if isinstance(seen_raw, list):
        seen = [int(sid) for sid in seen_raw if isinstance(sid, int) and not isinstance(sid, bool)]
    return GithubState(last_poll_time=lpt, seen_ids=seen)


def _validate_github_comment(raw: dict[str, object]) -> GithubComment:
    """Validate and narrow a raw API dict into a GithubComment TypedDict."""
    result = GithubComment()
    cid = _int_val(raw, "id")
    if cid:
        result["id"] = cid
    body = _str_val(raw, "body")
    if body:
        result["body"] = body
    user_raw = raw.get("user")
    if isinstance(user_raw, dict):
        user = GithubUser()
        login = _str_val(cast(dict[str, object], user_raw), "login")
        if login:
            user["login"] = login
        result["user"] = user
    html_url = _str_val(raw, "html_url")
    if html_url:
        result["html_url"] = html_url
    issue_url = _str_val(raw, "issue_url")
    if issue_url:
        result["issue_url"] = issue_url
    pr_url = _str_val(raw, "pull_request_url")
    if pr_url:
        result["pull_request_url"] = pr_url
    issue_num = _str_val(raw, "issue_num")
    if issue_num:
        result["issue_num"] = issue_num
    kind = _str_val(raw, "kind")
    if kind:
        result["kind"] = kind
    return result


# ---------------------------------------------------------------------------
# GmailConnector
# ---------------------------------------------------------------------------

class GmailConnector(BaseConnector[GmailMessage]):
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
        profile = self._get_profile()
        if profile is None:
            return False, "gws auth failed — token may be expired (run: gws gmail users getProfile)"
        email = profile.get("emailAddress")
        email_str: str = str(email) if isinstance(email, str) else "?"
        return True, f"OK (email={email_str})"

    def status(self) -> GmailStatus:
        with self._lock:
            return GmailStatus(
                name=self.connector_name,
                running=self.running,
                sender_filter=self.sender_filter,
                poll_interval=self.poll_interval,
                consecutive_failures=self._consecutive_failures,
                alert_sent=self._alert_sent,
                history_id=self._history_id,
            )

    # -- gws CLI wrapper --

    def _run_gws(self, *args: str, json_body: Optional[str] = None) -> Optional[dict[str, object]]:
        """Run a gws CLI command and return parsed JSON dict, or None on failure."""
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
            parsed: object = json.loads(result.stdout)
            if not isinstance(parsed, dict):
                print(f"[gmail] gws returned non-dict: {type(parsed).__name__}")
                return None
            return cast(dict[str, object], parsed)
        except subprocess.TimeoutExpired:
            print("[gmail] gws call timed out")
            return None
        except json.JSONDecodeError as e:
            print(f"[gmail] gws JSON parse failed: {e}")
            return None
        except OSError as e:
            print(f"[gmail] gws call failed: {e}")
            return None

    # -- Typed API wrappers (narrow _run_gws output to concrete types) --

    def _get_profile(self) -> Optional[GmailProfile]:
        raw = self._run_gws("getProfile", "--params", '{"userId":"me"}')
        if raw is None:
            return None
        return _validate_gmail_profile(raw)

    def get_message(self, msg_id: str) -> Optional[GmailMessage]:
        params = json.dumps({"userId": "me", "id": msg_id, "format": "full"})
        raw = self._run_gws("messages", "get", "--params", params)
        if raw is None:
            return None
        return _validate_gmail_message(raw)

    def _get_history(self, start_history_id: str) -> Optional[GmailHistoryResponse]:
        params = json.dumps({"userId": "me", "startHistoryId": start_history_id})
        raw = self._run_gws("history", "list", "--params", params)
        if raw is None:
            return None
        return _validate_gmail_history_response(raw)

    def _get_attachment_data(self, msg_id: str, att_id: str) -> Optional[GmailAttachmentData]:
        params = json.dumps({"userId": "me", "messageId": msg_id, "id": att_id})
        raw = self._run_gws("messages", "attachments", "get", "--params", params)
        if raw is None:
            return None
        return _validate_gmail_attachment_data(raw)

    def _list_messages(self, query: str, max_results: int = 5) -> Optional[GmailMessageListResponse]:
        params = json.dumps({"userId": "me", "maxResults": max_results, "q": query})
        raw = self._run_gws("messages", "list", "--params", params)
        if raw is None:
            return None
        return _validate_gmail_message_list_response(raw)

    def mark_as_read(self, msg_id: str) -> bool:
        params = json.dumps({"userId": "me", "id": msg_id})
        body = json.dumps({"removeLabelIds": ["UNREAD"]})
        result = self._run_gws("messages", "modify", "--params", params, json_body=body)
        return result is not None

    # -- History ID persistence (atomic write) --

    def _save_history_id(self) -> None:
        with self._lock:
            hid = self._history_id
        if not hid:
            return
        try:
            _atomic_write_text(self._history_file_path, hid)
        except OSError as e:
            print(f"[gmail] Failed to save historyId: {e}")

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
        profile = self._get_profile()
        if profile and profile.get("historyId"):
            hid = str(profile["historyId"])
            with self._lock:
                self._history_id = hid
            self._save_history_id()
            return hid
        return None

    # -- Message extraction (safe header access) --

    def extract_sender(self, message: GmailMessage) -> str:
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

    def extract_sender_name(self, message: GmailMessage) -> str:
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

    def extract_subject(self, message: GmailMessage) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return ""
        return _get_header(headers, "subject")

    def extract_message_id(self, message: GmailMessage) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        headers = payload.get("headers")
        if not isinstance(headers, list):
            return ""
        return _get_header(headers, "message-id")

    def is_sent_message(self, message: GmailMessage) -> bool:
        label_ids = message.get("labelIds")
        if not isinstance(label_ids, list):
            return False
        return "SENT" in label_ids

    def extract_body_text(self, message: GmailMessage) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        return self._find_text_part(payload)

    def _find_text_part(
        self, part: GmailPayloadPart, depth: int = 0, counter: Optional[list[int]] = None,
    ) -> str:
        if counter is None:
            counter = [0]
        counter[0] += 1
        if depth > _MAX_MIME_DEPTH or counter[0] > _MAX_MIME_PARTS:
            return ""
        raw_mime = part.get("mimeType")
        mime_type: str = str(raw_mime) if isinstance(raw_mime, str) else ""
        if mime_type == "text/plain":
            body = part.get("body")
            if not isinstance(body, dict):
                return ""
            raw_data = body.get("data")
            data: str = str(raw_data) if isinstance(raw_data, str) else ""
            if data:
                # Validate base64 alphabet and padding position
                padded = data + "=" * (-len(data) % 4)
                if not _B64_URLSAFE.match(padded):
                    return ""
                try:
                    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
                except (ValueError, binascii.Error):
                    return ""
            return ""
        parts = part.get("parts")
        if isinstance(parts, list):
            for sub in parts:
                if counter[0] > _MAX_MIME_PARTS:
                    break
                if isinstance(sub, dict):
                    text = self._find_text_part(sub, depth + 1, counter)
                    if text:
                        return text
        return ""

    def extract_attachments(self, message: GmailMessage) -> list[GmailAttachmentInfo]:
        attachments: list[GmailAttachmentInfo] = []
        payload = message.get("payload")
        if isinstance(payload, dict):
            self._find_attachments(payload, attachments)
        return attachments

    def _find_attachments(
        self, part: GmailPayloadPart, result: list[GmailAttachmentInfo],
        depth: int = 0, counter: Optional[list[int]] = None,
    ) -> None:
        if counter is None:
            counter = [0]
        counter[0] += 1
        if depth > _MAX_MIME_DEPTH or counter[0] > _MAX_MIME_PARTS:
            return
        raw_fn = part.get("filename")
        filename: str = str(raw_fn) if isinstance(raw_fn, str) else ""
        body = part.get("body")
        if isinstance(body, dict):
            raw_att_id = body.get("attachmentId")
            att_id: str = str(raw_att_id) if isinstance(raw_att_id, str) else ""
            raw_size = body.get("size")
            size: int = int(raw_size) if isinstance(raw_size, int) and not isinstance(raw_size, bool) else 0
            if filename and att_id:
                if size > _MAX_ATTACHMENT_BYTES:
                    print(f"[gmail] Skipping oversized attachment: {filename} ({size} bytes)")
                else:
                    raw_mime = part.get("mimeType")
                    mime: str = str(raw_mime) if isinstance(raw_mime, str) else ""
                    result.append(GmailAttachmentInfo(
                        filename=_sanitize_filename(filename),
                        mimeType=mime,
                        size=size,
                        attachmentId=att_id,
                    ))
        parts = part.get("parts")
        if isinstance(parts, list):
            for sub in parts:
                if counter[0] > _MAX_MIME_PARTS:
                    break
                if isinstance(sub, dict):
                    self._find_attachments(sub, result, depth + 1, counter)

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

    def _format_sent_reply(self, body: str, subject: str, thread_id: str, message: GmailMessage) -> tuple[str, str]:
        body = self._clean_body(body)
        body = self._strip_reply_chain(body)
        body = _truncate(body)
        sender_name = self.extract_sender_name(message)
        thread_tag = f" [thread:{thread_id}]" if thread_id else ""
        html = f"✉️ <b>Sent:</b> {_escape_html(subject)}{thread_tag}\n\n{_escape_html(body)}"
        plain = f"{sender_name} (sent via email):{thread_tag} {body}"
        return html, plain

    # -- Gmail API operations --

    def _get_new_message_ids(self) -> tuple[Optional[list[str]], Optional[str]]:
        """Return (message_ids, new_history_id). Caller advances historyId after processing."""
        with self._lock:
            current_hid = self._history_id
        if not current_hid:
            return [], None
        data = self._get_history(current_hid)
        if data is None:
            return None, None
        msg_ids: list[str] = []
        seen: set[str] = set()
        history_entries = data.get("history")
        if not isinstance(history_entries, list):
            history_entries = []
        for entry in history_entries:
            if not isinstance(entry, dict):
                continue
            messages_added = entry.get("messagesAdded")
            if not isinstance(messages_added, list):
                continue
            for added in messages_added:
                if not isinstance(added, dict):
                    continue
                msg = added.get("message")
                if not isinstance(msg, dict):
                    continue
                raw_id = msg.get("id")
                msg_id: str = str(raw_id) if isinstance(raw_id, str) else ""
                label_ids = msg.get("labelIds")
                if not isinstance(label_ids, list):
                    label_ids = []
                if msg_id and msg_id not in seen:
                    if self.is_inbox_unread(label_ids) or "SENT" in label_ids:
                        seen.add(msg_id)
                        msg_ids.append(msg_id)
        raw_hid = data.get("historyId")
        new_hid: Optional[str] = str(raw_hid) if raw_hid is not None else None
        return msg_ids, new_hid

    def _format_attachment_line(self, attachments: list[GmailAttachmentInfo]) -> str:
        if not attachments:
            return ""
        names: list[str] = []
        for a in attachments:
            fn = a.get("filename")
            names.append(str(fn) if isinstance(fn, str) and fn else "?")
        return f"\n[{len(attachments)} attachment(s): {', '.join(names)}]"

    def _download_attachment(self, msg_id: str, att: GmailAttachmentInfo) -> Optional[str]:
        raw_att_id = att.get("attachmentId")
        att_id: str = str(raw_att_id) if isinstance(raw_att_id, str) else ""
        if not att_id:
            return None
        data = self._get_attachment_data(msg_id, att_id)
        if not data:
            return None
        raw_b64 = data.get("data")
        if not isinstance(raw_b64, str) or not raw_b64:
            return None
        # Validate base64 alphabet and padding position
        padded_b64 = raw_b64 + "=" * (-len(raw_b64) % 4)
        if not _B64_URLSAFE.match(padded_b64):
            print(f"[gmail] Invalid base64 characters in attachment data")
            return None
        try:
            decoded = base64.urlsafe_b64decode(padded_b64)
        except (ValueError, binascii.Error) as e:
            print(f"[gmail] decode attachment failed: {e}")
            return None
        if len(decoded) > _MAX_ATTACHMENT_BYTES:
            print(f"[gmail] Attachment too large: {len(decoded)} bytes")
            return None
        raw_fn = att.get("filename")
        raw_name = _sanitize_filename(str(raw_fn) if isinstance(raw_fn, str) else "attachment")
        # Prefix with sanitized msg_id to prevent clobbering across messages
        safe_prefix = re.sub(r'[^\w]', '', msg_id[:8]) if msg_id else ""
        filename = f"{safe_prefix}_{raw_name}" if safe_prefix else raw_name
        # Enforce total filename length after prefixing
        if len(filename) > _MAX_FILENAME_LEN:
            root, ext = os.path.splitext(filename)
            if len(ext) > 10:
                ext = ext[:10]
            filename = root[:_MAX_FILENAME_LEN - len(ext)] + ext
        att_dir = os.path.join(os.path.expanduser("~"), ".cache", "beast", "email", "attachments")
        try:
            _atomic_write_bytes(os.path.join(att_dir, filename), decoded)
            return os.path.join(att_dir, filename)
        except OSError as e:
            print(f"[gmail] save attachment failed: {e}")
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
                    downloaded.append(Attachment(path=path, filename=att["filename"], mimeType=att["mimeType"]))
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
                downloaded.append(Attachment(path=path, filename=att["filename"], mimeType=att["mimeType"]))
                print(f"[gmail] attachment: {att['filename']} -> {path}")

        self.on_message(targets, html_text, plain_text, downloaded)
        if not self.mark_as_read(msg_id):
            print(f"[gmail] Warning: failed to mark message {msg_id} as read")

    # -- Polling --

    def poll_once(self) -> None:
        msg_ids, new_hid = self._get_new_message_ids()
        if msg_ids is None:
            self.track_failure()
            with self._lock:
                fail_count = self._consecutive_failures
            if fail_count >= CONSECUTIVE_FAIL_REBOOTSTRAP:
                print(f"[gmail] ⚠ {fail_count} failures — attempting re-bootstrap")
                new_id = self._bootstrap_history_id(skip_disk=True)
                if new_id:
                    with self._lock:
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
            with self._lock:
                self._history_id = new_hid
            self._save_history_id()
        elif had_errors:
            print("[gmail] historyId NOT advanced — some messages failed, will retry next poll")

    def _on_preflight_ok(self) -> None:
        hid = self._bootstrap_history_id()
        with self._lock:
            self._history_id = hid
        if not hid:
            print("[gmail] Bootstrap failed, retrying in 60s...")
            if self._stop_event.wait(60):
                return
            hid = self._bootstrap_history_id()
            with self._lock:
                self._history_id = hid
            if not hid:
                self._send_alert("Gmail connector disabled — cannot get historyId")
                self._stop_event.set()
                return
        print(f"[gmail] Started (interval={self.poll_interval}s, from={self.sender_filter}, historyId={hid})")
        self._catchup_unread()

    def _catchup_unread(self) -> None:
        q = f"from:{self.sender_filter} is:unread in:inbox newer_than:1d"
        data = self._list_messages(q, max_results=5)
        if not data:
            return
        messages_list = data.get("messages")
        if not isinstance(messages_list, list):
            return
        msg_ids: list[str] = []
        for m in messages_list:
            if isinstance(m, dict):
                raw_id = m.get("id")
                if isinstance(raw_id, str):
                    msg_ids.append(raw_id)
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

class GitHubConnector(BaseConnector[GithubComment]):
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
        if not _REPO_PATTERN.match(repo):
            raise ValueError(f"github: repo must be 'owner/name' format, got: {repo!r}")
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

    def status(self) -> GithubStatus:
        with self._lock:
            return GithubStatus(
                name=self.connector_name,
                running=self.running,
                sender_filter=self.sender_filter,
                poll_interval=self.poll_interval,
                consecutive_failures=self._consecutive_failures,
                alert_sent=self._alert_sent,
                repo=self.repo,
                last_poll_time=self._last_poll_time,
                seen_ids_count=len(self._seen_ids),
            )

    # -- State persistence --

    def _load_state(self) -> bool:
        if not self._state_file:
            return False
        try:
            with open(self._state_file) as f:
                data: object = json.load(f)
            if not isinstance(data, dict):
                return False
            state = _validate_github_state(cast(dict[str, object], data))
            with self._lock:
                if state["last_poll_time"]:
                    self._last_poll_time = state["last_poll_time"]
                for sid in state["seen_ids"]:
                    self._seen_ids.add(sid)
                return bool(self._last_poll_time)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return False

    def _save_state(self) -> None:
        if not self._state_file:
            return
        with self._lock:
            state = GithubState(
                last_poll_time=self._last_poll_time,
                seen_ids=sorted(self._seen_ids)[-_MAX_SEEN_IDS:],
            )
        try:
            _atomic_write_text(self._state_file, json.dumps(state))
        except OSError as e:
            print(f"[github] Failed to save state: {e}")

    def _on_preflight_ok(self) -> None:
        restored = self._load_state()
        if restored:
            with self._lock:
                seen_count = len(self._seen_ids)
                poll_time = self._last_poll_time
            print(f"[github] Restart — restored state: {seen_count} seen IDs, since={poll_time}")
        else:
            initial_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with self._lock:
                self._last_poll_time = initial_time
            seed, _ = self._get_all_comments(initial_time)
            if seed:
                with self._lock:
                    for c in seed:
                        cid = c.get("id")
                        if isinstance(cid, int) and not isinstance(cid, bool):
                            self._seen_ids.add(cid)
                print(f"[github] First run — seeded {len(seed)} existing comments as seen")
            else:
                print(f"[github] First run — no comments to seed")
            self._save_state()
        with self._lock:
            poll_time = self._last_poll_time
        print(f"[github] Started (interval={self.poll_interval}s, repo={self.repo}, user={self.sender_filter}, since={poll_time})")

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

    def _get_all_comments(self, since: str) -> tuple[Optional[list[GithubComment]], bool]:
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
        merged: list[GithubComment] = []
        for raw in [issue_raw, pr_raw]:
            if not raw:
                continue
            comments = _safe_json_loads_list(raw)
            for c in comments:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id", 0)
                if isinstance(cid, int) and not isinstance(cid, bool) and cid not in seen:
                    seen.add(cid)
                    merged.append(c)
        return merged, is_complete

    # -- Message extraction --

    def extract_sender(self, comment: GithubComment) -> str:
        user = comment.get("user")
        if not isinstance(user, dict):
            return ""
        login = user.get("login")
        return str(login).lower() if isinstance(login, str) and login else ""

    def extract_issue_context(self, comment: GithubComment) -> IssueContext:
        if comment.get("issue_num") and comment.get("kind"):
            return IssueContext(
                number=str(comment["issue_num"]),
                kind=str(comment["kind"]),
                url=str(comment.get("html_url", "")),
            )
        raw_issue_url = comment.get("issue_url")
        raw_pr_url = comment.get("pull_request_url")
        issue_url: str = str(raw_issue_url) if isinstance(raw_issue_url, str) else ""
        if not issue_url and isinstance(raw_pr_url, str):
            issue_url = str(raw_pr_url)
        raw_html_url = comment.get("html_url")
        html_url: str = str(raw_html_url) if isinstance(raw_html_url, str) else ""
        number: str = ""
        kind: str = "comment"
        if "/issues/" in issue_url:
            candidate = issue_url.rsplit("/", 1)[-1]
            if candidate.isdigit():
                number = candidate
            kind = "issue"
        elif "/pulls/" in issue_url or "/pull/" in html_url:
            if "/pulls/" in issue_url:
                candidate = issue_url.rsplit("/", 1)[-1]
                if candidate.isdigit():
                    number = candidate
            elif "/pull/" in html_url:
                parts = html_url.split("/pull/")
                if len(parts) > 1:
                    candidate = parts[1].split("/")[0]
                    if "#" in candidate:
                        candidate = candidate.split("#")[0]
                    if candidate.isdigit():
                        number = candidate
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
        kind = context["kind"]
        number = context["number"]
        url = context["url"]
        tag = f"#{number}" if number else ""
        link = f'<a href="{_escape_html(url)}">#{number}</a>' if url and number else tag
        html = f"🔔 <b>GitHub {kind} {link}</b>\n\n{self._md_to_telegram_html(body)}"
        plain = f"manager (via GitHub {kind} {tag}): {body}"
        return html, plain

    # -- Comment processing --

    def _process_comment(self, comment: GithubComment) -> None:
        cid = comment.get("id", 0)
        if not isinstance(cid, int) or isinstance(cid, bool) or cid == 0:
            return
        with self._lock:
            if cid in self._seen_ids:
                return
        # Check sender BEFORE marking as seen — failed delivery won't permanently skip
        if not self.is_allowed_sender(comment):
            with self._lock:
                self._seen_ids.add(cid)  # Non-target sender: mark seen, no retry needed
            return
        raw_body = comment.get("body")
        body: str = str(raw_body).strip() if isinstance(raw_body, str) else ""
        if not body:
            with self._lock:
                self._seen_ids.add(cid)
            return
        context = self.extract_issue_context(comment)
        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_comment(cleaned, context)
        else:
            html_text, plain_text = self.format_comment(body, context)
        if targets and context["number"]:
            num = context["number"]
            # Shell-safe: quote interpolated values
            safe_repo = self.repo.replace("'", "'\\''")
            reply_hint = f"\n\nView: beast github show {num} --repo '{safe_repo}'"
            reply_hint += f"\nReply: beast github comment {num} --repo '{safe_repo}' --body 'your reply'"
            plain_text += reply_hint
        metadata = ConnectorMetadata(number=context["number"], repo=self.repo, comment_id=cid)
        self.on_message(targets, html_text, plain_text, [], metadata=metadata)
        # Mark seen AFTER successful delivery
        with self._lock:
            self._seen_ids.add(cid)
        print(f"[github] {context['kind']} #{context['number']}: -> {targets or 'Telegram only'}")

    # -- Polling --

    def poll_once(self) -> None:
        with self._lock:
            poll_time = self._last_poll_time
        if not poll_time:
            return
        all_comments, fetch_complete = self._get_all_comments(poll_time)
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
            with self._lock:
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
        with self._lock:
            if len(self._seen_ids) > _MAX_SEEN_IDS:
                self._seen_ids = set(sorted(self._seen_ids)[-_PRUNE_KEEP:])

    def stop(self) -> None:
        super().stop()  # Wait for poll thread to exit before saving
        self._save_state()
