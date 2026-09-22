"""Polling connectors for claudecode-telegram bridge.

Gmail and GitHub connectors that poll external services for new messages,
parse @worker mentions, and deliver to workers via bridge callbacks.

Flat module — no subpackages. Import directly:
    from connectors import GmailConnector, GitHubConnector
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONSECUTIVE_FAIL_WARN = 3
CONSECUTIVE_FAIL_REBOOTSTRAP = 5


# ---------------------------------------------------------------------------
# BaseConnector
# ---------------------------------------------------------------------------

class BaseConnector:
    """Abstract polling connector with preflight, failure tracking, and alerts."""

    connector_name: str = "base"

    def __init__(
        self,
        sender_filter: str,
        poll_interval: int,
        on_message: Callable[..., None],
        get_registered_workers: Callable[[], set[str]],
        on_alert: Optional[Callable[[str], None]] = None,
    ) -> None:
        if not sender_filter or not sender_filter.strip():
            raise ValueError(f"{self.connector_name}: sender_filter is required (security: cannot be empty)")
        self.sender_filter: str = sender_filter.lower()
        self.poll_interval: int = poll_interval
        self.on_message = on_message
        self.get_registered_workers = get_registered_workers
        self.on_alert = on_alert
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures: int = 0
        self._alert_sent: bool = False

    # -- Abstract interface (subclasses MUST override) --

    def preflight_check(self) -> tuple[bool, str]:
        raise NotImplementedError

    def poll_once(self) -> None:
        raise NotImplementedError

    def extract_sender(self, message: Any) -> str:
        raise NotImplementedError

    # -- Lifecycle --

    def _on_preflight_ok(self) -> None:
        """Called after successful preflight, before poll loop starts."""

    def start(self) -> threading.Thread:
        """Spawn the polling thread. Returns the thread."""
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
        if self._thread:
            self._thread.join(timeout=5)

    def restart(self) -> tuple[bool, str]:
        """Stop if running, reset state, re-run preflight, and start polling.

        Returns (ok, message) — ok=True if connector restarted successfully.
        """
        self.stop()
        self._stop_event.clear()
        self._consecutive_failures = 0
        self._alert_sent = False
        ok, msg = self.preflight_check()
        if not ok:
            return False, msg
        self._thread = threading.Thread(
            target=self._restart_loop, daemon=True,
            name=f"{self.connector_name}-poller",
        )
        self._thread.start()
        return True, msg

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        """Return a status dict for API responses."""
        return {
            "name": self.connector_name,
            "running": self.running,
            "sender_filter": self.sender_filter,
            "poll_interval": self.poll_interval,
            "consecutive_failures": self._consecutive_failures,
            "alert_sent": self._alert_sent,
        }

    # -- Sender filtering --

    def is_allowed_sender(self, message: Any) -> bool:
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
        if self.on_alert and not self._alert_sent:
            try:
                self.on_alert(f"{tag} {text}")
                self._alert_sent = True
            except Exception as e:
                print(f"{tag} Failed to send alert: {e}")

    def _clear_alert(self) -> None:
        if self._alert_sent:
            self._alert_sent = False
            tag = f"[{self.connector_name}]"
            if self.on_alert:
                try:
                    self.on_alert(f"{tag} Recovered — polling resumed")
                except Exception:
                    pass

    # -- Failure tracking --

    def track_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= CONSECUTIVE_FAIL_WARN and not self._alert_sent:
            self._send_alert(f"Polling failing ({self._consecutive_failures} consecutive errors)")

    def track_success(self) -> None:
        if self._consecutive_failures > 0:
            self._clear_alert()
            self._consecutive_failures = 0

    # -- Poll loop --

    def _poll_loop(self) -> None:
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

    def _restart_loop(self) -> None:
        """Poll loop for restart() — preflight already passed, skip it."""
        tag = f"[{self.connector_name}]"
        print(f"{tag} Preflight OK (restart)")
        self._on_preflight_ok()
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                print(f"{tag} Poll error: {e}")
            self._stop_event.wait(self.poll_interval)


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
        on_message: Callable[..., None],
        get_registered_workers: Callable[[], set[str]],
        on_alert: Optional[Callable[[str], None]] = None,
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

    def preflight_check(self) -> tuple[bool, str]:
        if not os.path.isfile(self.gws_bin):
            return False, f"gws binary not found at {self.gws_bin}"
        if not os.access(self.gws_bin, os.X_OK):
            return False, f"gws binary not executable: {self.gws_bin}"
        result = self._run_gws("getProfile", "--params", '{"userId":"me"}')
        if result is None:
            return False, "gws auth failed — token may be expired (run: gws gmail users getProfile)"
        return True, f"OK (email={result.get('emailAddress', '?')})"

    def status(self) -> dict[str, Any]:
        s = super().status()
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
            return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
            print(f"[gmail] gws call failed: {e}")
            return None

    # -- History ID persistence --

    def _history_file(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".cache", "beast", "email", "gmail_history_id")

    def _save_history_id(self) -> None:
        if not self._history_id:
            return
        path = self._history_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w") as f:
                f.write(self._history_id)
        except Exception as e:
            print(f"[gmail] Failed to save historyId: {e}")

    def _load_history_id(self) -> Optional[str]:
        path = self._history_file()
        try:
            with open(path, "r") as f:
                hid = f.read().strip()
                if hid:
                    return hid
        except FileNotFoundError:
            pass
        except Exception as e:
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
            hid: str = profile["historyId"]
            self._history_id = hid
            self._save_history_id()
            return hid
        return None

    # -- Message extraction --

    def extract_sender(self, message: dict[str, Any]) -> str:
        headers: list[dict[str, str]] = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "from":
                match = re.search(r'<([^>]+)>', h["value"])
                if match:
                    return match.group(1).lower()
                return h["value"].strip().lower()
        return ""

    def extract_sender_name(self, message: dict[str, Any]) -> str:
        headers: list[dict[str, str]] = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "from":
                match = re.match(r'^([^<]+)\s*<', h["value"])
                if match:
                    return match.group(1).strip().strip('"')
                return h["value"].split("@")[0]
        return ""

    def extract_subject(self, message: dict[str, Any]) -> str:
        headers: list[dict[str, str]] = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "subject":
                return h["value"]
        return ""

    def extract_message_id(self, message: dict[str, Any]) -> str:
        headers: list[dict[str, str]] = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "message-id":
                return h["value"]
        return ""

    def is_sent_message(self, message: dict[str, Any]) -> bool:
        label_ids: list[str] = message.get("labelIds", [])
        return "SENT" in label_ids

    def extract_body_text(self, message: dict[str, Any]) -> str:
        payload: dict[str, Any] = message.get("payload", {})
        return self._find_text_part(payload)

    def _find_text_part(self, part: dict[str, Any]) -> str:
        mime_type: str = part.get("mimeType", "")
        if mime_type == "text/plain":
            data: str = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return ""
        for sub in part.get("parts", []):
            text = self._find_text_part(sub)
            if text:
                return text
        return ""

    def extract_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        self._find_attachments(message.get("payload", {}), attachments)
        return attachments

    def _find_attachments(self, part: dict[str, Any], result: list[dict[str, Any]]) -> None:
        filename: str = part.get("filename", "")
        body: dict[str, Any] = part.get("body", {})
        if filename and body.get("attachmentId"):
            result.append({
                "filename": filename,
                "mimeType": part.get("mimeType", ""),
                "size": body.get("size", 0),
                "attachmentId": body["attachmentId"],
            })
        for sub in part.get("parts", []):
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

    def _escape_html(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _truncate(self, text: str, limit: int = 1500) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rsplit('\n', 1)[0] + "\n… (truncated)"

    def format_email_message(self, body: str, subject: str, thread_id: str = "") -> tuple[str, str]:
        """Returns (html_text, plain_text) for Telegram and worker delivery."""
        body = self._clean_body(body)
        manager_text, forwarded_content = self._detect_forward_split(body)

        thread_tag = f" [thread:{thread_id}]" if thread_id else ""

        if forwarded_content is not None:
            if manager_text:
                manager_text = self._strip_reply_chain(manager_text)
            forwarded_content = self._clean_body(forwarded_content)
            forwarded_content = self._truncate(forwarded_content, 1200)

            fwd_subject = subject if re.match(r'(?i)^(fwd?|forwarded):', subject) else f"Fwd: {subject}"
            html_parts = [f"📧 <b>{self._escape_html(fwd_subject)}</b>{thread_tag}"]
            plain_parts = [f"manager (via email):{thread_tag}"]
            if manager_text:
                html_parts.append(f"\n{self._escape_html(manager_text)}")
                plain_parts.append(manager_text)
            html_parts.append(f"<blockquote>{self._escape_html(forwarded_content)}</blockquote>")
            plain_parts.append(f"--- Forwarded: {subject} ---")
            plain_parts.append(forwarded_content)
            return "\n".join(html_parts), "\n".join(plain_parts)

        body = self._strip_reply_chain(body)
        body = self._truncate(body)
        html = f"📧 <b>{self._escape_html(subject)}</b>{thread_tag}\n\n{self._escape_html(body)}"
        plain = f"manager (via email):{thread_tag} {body}"
        return html, plain

    def _format_sent_reply(self, body: str, subject: str, thread_id: str, message: dict[str, Any]) -> tuple[str, str]:
        body = self._clean_body(body)
        body = self._strip_reply_chain(body)
        body = self._truncate(body)
        sender_name = self.extract_sender_name(message)
        thread_tag = f" [thread:{thread_id}]" if thread_id else ""
        html = f"✉️ <b>Sent:</b> {self._escape_html(subject)}{thread_tag}\n\n{self._escape_html(body)}"
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

    def _get_new_message_ids(self) -> Optional[list[str]]:
        if not self._history_id:
            return []
        params = json.dumps({"userId": "me", "startHistoryId": self._history_id})
        data = self._run_gws("history", "list", "--params", params)
        if data is None:
            return None
        if "historyId" in data:
            self._history_id = data["historyId"]
            self._save_history_id()
        msg_ids: list[str] = []
        seen: set[str] = set()
        for entry in data.get("history", []):
            for added in entry.get("messagesAdded", []):
                msg = added.get("message", {})
                msg_id: str = msg.get("id", "")
                label_ids: list[str] = msg.get("labelIds", [])
                if msg_id and msg_id not in seen:
                    if self.is_inbox_unread(label_ids) or "SENT" in label_ids:
                        seen.add(msg_id)
                        msg_ids.append(msg_id)
        return msg_ids

    def _format_attachment_line(self, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return ""
        names = [a["filename"] for a in attachments]
        return f"\n[{len(attachments)} attachment(s): {', '.join(names)}]"

    def _download_attachment(self, msg_id: str, att: dict[str, Any]) -> Optional[str]:
        params = json.dumps({"userId": "me", "messageId": msg_id, "id": att["attachmentId"]})
        data = self._run_gws("messages", "attachments", "get", "--params", params)
        if not data or "data" not in data:
            return None
        raw: str = data["data"].replace("-", "+").replace("_", "/")
        padding = 4 - len(raw) % 4
        if padding < 4:
            raw += "=" * padding
        try:
            decoded = base64.b64decode(raw)
        except Exception as e:
            print(f"[gmail] decode attachment failed: {e}")
            return None
        att_dir = os.path.join(os.path.expanduser("~"), ".cache", "beast", "email", "attachments")
        os.makedirs(att_dir, exist_ok=True)
        path = os.path.join(att_dir, att["filename"])
        try:
            with open(path, "wb") as f:
                f.write(decoded)
            return path
        except Exception as e:
            print(f"[gmail] save attachment failed: {e}")
            return None

    # -- Message processing --

    def _process_message(self, msg_id: str) -> None:
        message = self.get_message(msg_id)
        if not message:
            return

        is_sent = self.is_sent_message(message)
        if not is_sent and not self.is_allowed_sender(message):
            return

        body = self.extract_body_text(message)
        subject = self.extract_subject(message)
        thread_id: str = message.get("threadId", "")
        message_id = self.extract_message_id(message)
        attachments = self.extract_attachments(message)
        if not body.strip():
            return

        if is_sent:
            html_text, plain_text = self._format_sent_reply(body, subject, thread_id, message)
            downloaded: list[dict[str, str]] = []
            for att in attachments:
                path = self._download_attachment(msg_id, att)
                if path:
                    downloaded.append({"path": path, "filename": att["filename"], "mimeType": att.get("mimeType", "")})
            self.on_message([], html_text, plain_text, downloaded)
            return

        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_email_message(cleaned, subject, thread_id)
        else:
            html_text, plain_text = self.format_email_message(body, subject, thread_id)
        att_line = self._format_attachment_line(attachments)
        html_text += att_line
        plain_text += att_line

        if targets and message_id:
            reply_hint = f"\n\nReply (prefer HTML): beast email send -s \"Re: {subject}\" --thread-id {thread_id} --in-reply-to \"{message_id}\" --html-file /tmp/reply.html"
            reply_hint += f"\nReply (plain text): beast email send -s \"Re: {subject}\" --thread-id {thread_id} --in-reply-to \"{message_id}\" --body \"your reply\""
            plain_text += reply_hint

        downloaded = []
        for att in attachments:
            path = self._download_attachment(msg_id, att)
            if path:
                downloaded.append({"path": path, "filename": att["filename"], "mimeType": att.get("mimeType", "")})
                print(f"[gmail] attachment: {att['filename']} -> {path}")

        self.on_message(targets, html_text, plain_text, downloaded)
        self.mark_as_read(msg_id)

    # -- Polling --

    def poll_once(self) -> None:
        msg_ids = self._get_new_message_ids()
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
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                print(f"[gmail] Error processing message {msg_id}: {e}")

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
        msg_ids: list[str] = [m["id"] for m in data["messages"]]
        if not msg_ids:
            return
        print(f"[gmail] Catch-up: {len(msg_ids)} unread from {self.sender_filter}")
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                print(f"[gmail] Catch-up error {msg_id}: {e}")

    def stop(self) -> None:
        self._save_history_id()
        super().stop()


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
        on_message: Callable[..., None],
        get_registered_workers: Callable[[], set[str]],
        on_alert: Optional[Callable[[str], None]] = None,
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
        except FileNotFoundError:
            return False, f"gh binary not found: {self.gh_bin}"
        except Exception as e:
            return False, f"gh error: {e}"

    def status(self) -> dict[str, Any]:
        s = super().status()
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
                state: dict[str, Any] = json.load(f)
            if state.get("last_poll_time"):
                self._last_poll_time = state["last_poll_time"]
            for sid in state.get("seen_ids", []):
                self._seen_ids.add(sid)
            return bool(self._last_poll_time)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return False

    def _save_state(self) -> None:
        if not self._state_file:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            state = {
                "last_poll_time": self._last_poll_time,
                "seen_ids": list(self._seen_ids)[-500:],
            }
            tmp = self._state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self._state_file)
        except Exception as e:
            print(f"[github] Failed to save state: {e}")

    def _on_preflight_ok(self) -> None:
        restored = self._load_state()
        if restored:
            print(f"[github] Restart — restored state: {len(self._seen_ids)} seen IDs, since={self._last_poll_time}")
        else:
            self._last_poll_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            seed = self._get_all_comments(self._last_poll_time)
            if seed:
                for c in seed:
                    self._seen_ids.add(c.get("id"))
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
        except Exception as e:
            print(f"[github] gh api exception: {e}")
            return None

    def _get_all_comments(self, since: str) -> Optional[list[dict[str, Any]]]:
        issue_raw = self._gh_api(
            f"/repos/{self.repo}/issues/comments?since={since}&sort=updated&direction=asc&per_page=100",
        )
        pr_raw = self._gh_api(
            f"/repos/{self.repo}/pulls/comments?since={since}&sort=updated&direction=asc&per_page=100",
        )
        if issue_raw is None and pr_raw is None:
            return None
        seen: set[int] = set()
        merged: list[dict[str, Any]] = []
        for raw in [issue_raw, pr_raw]:
            if not raw:
                continue
            try:
                comments: list[dict[str, Any]] = json.loads(raw)
                for c in comments:
                    cid: int = c.get("id", 0)
                    if cid not in seen:
                        seen.add(cid)
                        merged.append(c)
            except json.JSONDecodeError as e:
                print(f"[github] JSON error: {e}")
        return merged

    # -- Message extraction --

    def extract_sender(self, comment: dict[str, Any]) -> str:
        return comment.get("user", {}).get("login", "").lower()

    def extract_issue_context(self, comment: dict[str, Any]) -> dict[str, str]:
        if comment.get("issue_num") and comment.get("kind"):
            return {
                "number": str(comment["issue_num"]),
                "kind": comment["kind"],
                "url": comment.get("html_url", ""),
            }
        issue_url: str = comment.get("issue_url", "") or comment.get("pull_request_url", "")
        html_url: str = comment.get("html_url", "")
        number = ""
        kind = "comment"
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
        return {"number": number, "kind": kind, "url": html_url}

    # -- Formatting --

    def _escape_html(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _md_to_telegram_html(self, md: str) -> str:
        text = self._escape_html(md)
        text = re.sub(r'```[a-zA-Z]*\n(.*?)```', lambda m: f'<pre>{m.group(1).rstrip()}</pre>', text, flags=re.DOTALL)
        text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
        text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', lambda m: m.group(1) or '(image)', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
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

    def _truncate(self, text: str, limit: int = 1500) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rsplit('\n', 1)[0] + "\n… (truncated)"

    def format_comment(self, body: str, context: dict[str, str]) -> tuple[str, str]:
        body = self._truncate(body)
        kind = context.get("kind", "comment")
        number = context.get("number", "")
        url = context.get("url", "")
        tag = f"#{number}" if number else ""
        link = f'<a href="{url}">#{number}</a>' if url and number else tag
        html = f"🔔 <b>GitHub {kind} {link}</b>\n\n{self._md_to_telegram_html(body)}"
        plain = f"manager (via GitHub {kind} {tag}): {body}"
        return html, plain

    # -- Comment processing --

    def _process_comment(self, comment: dict[str, Any]) -> None:
        comment_id: int = comment.get("id", 0)
        if comment_id in self._seen_ids:
            return
        self._seen_ids.add(comment_id)
        if not self.is_allowed_sender(comment):
            return
        body: str = comment.get("body", "").strip()
        if not body:
            return
        context = self.extract_issue_context(comment)
        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_comment(cleaned, context)
        else:
            html_text, plain_text = self.format_comment(body, context)
        if targets and context.get("number"):
            num = context["number"]
            reply_hint = f"\n\nView: beast github show {num} --repo {self.repo}"
            reply_hint += f"\nReply: beast github comment {num} --repo {self.repo} --body \"your reply\""
            plain_text += reply_hint
        metadata: dict[str, Any] = {"number": context.get("number"), "repo": self.repo, "comment_id": comment_id}
        self.on_message(targets, html_text, plain_text, [], metadata=metadata)
        print(f"[github] {context['kind']} #{context['number']}: -> {targets or 'Telegram only'}")

    # -- Polling --

    def poll_once(self) -> None:
        if not self._last_poll_time:
            return
        all_comments = self._get_all_comments(self._last_poll_time)
        if all_comments is None:
            self.track_failure()
            return
        self.track_success()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for comment in all_comments:
            try:
                self._process_comment(comment)
            except Exception as e:
                print(f"[github] Error processing comment {comment.get('id')}: {e}")
        self._last_poll_time = now
        self._prune_seen_ids()
        self._save_state()

    def _prune_seen_ids(self) -> None:
        if len(self._seen_ids) > 500:
            self._seen_ids = set(list(self._seen_ids)[-200:])

    def stop(self) -> None:
        self._save_state()
        super().stop()
