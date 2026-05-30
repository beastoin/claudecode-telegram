"""Gmail polling connector for claudecode-telegram bridge.

Polls Gmail via gws CLI for new emails from a whitelisted sender,
parses @worker mentions, and delivers messages to workers.
"""

import base64
import json
import os
import re
import subprocess
import time
from typing import Callable, Optional

from base_connector import BaseConnector

CONSECUTIVE_FAIL_REBOOTSTRAP = 5


class GmailConnector(BaseConnector):

    connector_name = "gmail"

    def __init__(
        self,
        gws_bin: str,
        from_filter: str,
        poll_interval: int,
        on_message: Callable,
        get_registered_workers: Callable,
        on_alert: Optional[Callable] = None,
    ):
        super().__init__(
            sender_filter=from_filter,
            poll_interval=poll_interval,
            on_message=on_message,
            get_registered_workers=get_registered_workers,
            on_alert=on_alert,
        )
        self.gws_bin = gws_bin
        self.from_filter = self.sender_filter
        self._history_id: Optional[str] = None

    def preflight_check(self) -> tuple:
        if not os.path.isfile(self.gws_bin):
            return False, f"gws binary not found at {self.gws_bin}"
        if not os.access(self.gws_bin, os.X_OK):
            return False, f"gws binary not executable: {self.gws_bin}"
        result = self._run_gws("getProfile", "--params", '{"userId":"me"}')
        if result is None:
            return False, "gws auth failed — token may be expired (run: gws gmail users getProfile)"
        return True, f"OK (email={result.get('emailAddress', '?')})"

    def _run_gws(self, *args, json_body: str = None) -> Optional[dict]:
        cmd = [self.gws_bin, "gmail", "users"] + list(args)
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

    def _history_file(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".cache", "beast", "email", "gmail_history_id")

    def _save_history_id(self):
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

    def _bootstrap_history_id(self, skip_disk=False) -> Optional[str]:
        if not skip_disk:
            saved = self._load_history_id()
            if saved:
                print(f"[gmail] Resumed historyId={saved} from disk")
                return saved
        profile = self._run_gws("getProfile", "--params", '{"userId":"me"}')
        if profile and "historyId" in profile:
            hid = profile["historyId"]
            self._history_id = hid
            self._save_history_id()
            return hid
        return None

    def extract_sender(self, message: dict) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "from":
                match = re.search(r'<([^>]+)>', h["value"])
                if match:
                    return match.group(1).lower()
                return h["value"].strip().lower()
        return ""

    def extract_sender_name(self, message: dict) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "from":
                match = re.match(r'^([^<]+)\s*<', h["value"])
                if match:
                    return match.group(1).strip().strip('"')
                return h["value"].split("@")[0]
        return ""

    def extract_subject(self, message: dict) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "subject":
                return h["value"]
        return ""

    def extract_message_id(self, message: dict) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"].lower() == "message-id":
                return h["value"]
        return ""

    def is_sent_message(self, message: dict) -> bool:
        label_ids = message.get("labelIds", [])
        return "SENT" in label_ids

    def extract_body_text(self, message: dict) -> str:
        payload = message.get("payload", {})
        return self._find_text_part(payload)

    def _find_text_part(self, part: dict) -> str:
        mime_type = part.get("mimeType", "")
        if mime_type == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return ""
        for sub in part.get("parts", []):
            text = self._find_text_part(sub)
            if text:
                return text
        return ""

    def extract_attachments(self, message: dict) -> list:
        attachments = []
        self._find_attachments(message.get("payload", {}), attachments)
        return attachments

    def _find_attachments(self, part: dict, result: list):
        filename = part.get("filename", "")
        body = part.get("body", {})
        if filename and body.get("attachmentId"):
            result.append({
                "filename": filename,
                "mimeType": part.get("mimeType", ""),
                "size": body.get("size", 0),
                "attachmentId": body["attachmentId"],
            })
        for sub in part.get("parts", []):
            self._find_attachments(sub, result)

    def is_from_allowed_sender(self, message: dict) -> bool:
        return self.extract_sender(message) == self.from_filter

    def is_inbox_unread(self, label_ids: list) -> bool:
        return "INBOX" in label_ids and "UNREAD" in label_ids

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

    def _detect_forward_split(self, body: str) -> tuple:
        gmail_marker = "---------- Forwarded message ---------"
        if gmail_marker in body:
            parts = body.split(gmail_marker, 1)
            return parts[0].strip(), parts[1].strip()
        outlook_match = re.search(
            r'\n_{3,}\n\s*From:.*?\nSent:.*?\nTo:.*?\nSubject:',
            body, re.DOTALL
        )
        if outlook_match:
            return body[:outlook_match.start()].strip(), body[outlook_match.start():].strip()
        generic_match = re.search(
            r'\n-{2,}\s*(?:Original Message|Forwarded)\s*-{2,}',
            body, re.IGNORECASE
        )
        if generic_match:
            return body[:generic_match.start()].strip(), body[generic_match.start():].strip()
        return None, None

    def _escape_html(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _truncate(self, text: str, limit: int = 1500) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rsplit('\n', 1)[0] + "\n… (truncated)"

    def format_email_message(self, body: str, subject: str, thread_id: str = "") -> tuple:
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

    def _format_sent_reply(self, body: str, subject: str, thread_id: str, message: dict) -> tuple:
        body = self._clean_body(body)
        body = self._strip_reply_chain(body)
        body = self._truncate(body)
        sender_name = self.extract_sender_name(message)
        thread_tag = f" [thread:{thread_id}]" if thread_id else ""
        html = f"✉️ <b>Sent:</b> {self._escape_html(subject)}{thread_tag}\n\n{self._escape_html(body)}"
        plain = f"{sender_name} (sent via email):{thread_tag} {body}"
        return html, plain

    def get_message(self, msg_id: str) -> Optional[dict]:
        params = json.dumps({"userId": "me", "id": msg_id, "format": "full"})
        return self._run_gws("messages", "get", "--params", params)

    def mark_as_read(self, msg_id: str) -> bool:
        params = json.dumps({"userId": "me", "id": msg_id})
        body = json.dumps({"removeLabelIds": ["UNREAD"]})
        result = self._run_gws("messages", "modify", "--params", params, json_body=body)
        return result is not None

    def _get_new_message_ids(self) -> Optional[list]:
        if not self._history_id:
            return []
        params = json.dumps({"userId": "me", "startHistoryId": self._history_id})
        data = self._run_gws("history", "list", "--params", params)
        if data is None:
            return None
        if "historyId" in data:
            self._history_id = data["historyId"]
            self._save_history_id()
        msg_ids = []
        seen = set()
        for entry in data.get("history", []):
            for added in entry.get("messagesAdded", []):
                msg = added.get("message", {})
                msg_id = msg.get("id")
                label_ids = msg.get("labelIds", [])
                if msg_id and msg_id not in seen:
                    if self.is_inbox_unread(label_ids) or "SENT" in label_ids:
                        seen.add(msg_id)
                        msg_ids.append(msg_id)
        return msg_ids

    def _format_attachment_line(self, attachments: list) -> str:
        if not attachments:
            return ""
        names = [a["filename"] for a in attachments]
        return f"\n[{len(attachments)} attachment(s): {', '.join(names)}]"

    def _download_attachment(self, msg_id: str, att: dict) -> Optional[str]:
        params = json.dumps({"userId": "me", "messageId": msg_id, "id": att["attachmentId"]})
        data = self._run_gws("messages", "attachments", "get", "--params", params)
        if not data or "data" not in data:
            return None
        raw = data["data"].replace("-", "+").replace("_", "/")
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

    def _process_message(self, msg_id: str):
        message = self.get_message(msg_id)
        if not message:
            return

        is_sent = self.is_sent_message(message)
        if not is_sent and not self.is_from_allowed_sender(message):
            return

        body = self.extract_body_text(message)
        subject = self.extract_subject(message)
        thread_id = message.get("threadId", "")
        message_id = self.extract_message_id(message)
        attachments = self.extract_attachments(message)
        if not body.strip():
            return

        if is_sent:
            html_text, plain_text = self._format_sent_reply(body, subject, thread_id, message)
            downloaded = []
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
            reply_hint = f"\n\nReply: beast email send -s \"Re: {subject}\" --thread-id {thread_id} --in-reply-to \"{message_id}\" --body \"your reply\""
            plain_text += reply_hint

        downloaded = []
        for att in attachments:
            path = self._download_attachment(msg_id, att)
            if path:
                downloaded.append({"path": path, "filename": att["filename"], "mimeType": att.get("mimeType", "")})
                print(f"[gmail] attachment: {att['filename']} -> {path}")

        self.on_message(targets, html_text, plain_text, downloaded)
        self.mark_as_read(msg_id)

    def poll_once(self):
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

    def _on_preflight_ok(self):
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
        print(f"[gmail] Started (interval={self.poll_interval}s, from={self.from_filter}, historyId={self._history_id})")
        self._catchup_unread()

    def _catchup_unread(self):
        q = f"from:{self.from_filter} is:unread in:inbox newer_than:1d"
        params = json.dumps({"userId": "me", "maxResults": 5, "q": q})
        data = self._run_gws("messages", "list", "--params", params)
        if not data or "messages" not in data:
            return
        msg_ids = [m["id"] for m in data["messages"]]
        if not msg_ids:
            return
        print(f"[gmail] Catch-up: {len(msg_ids)} unread from {self.from_filter}")
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                print(f"[gmail] Catch-up error {msg_id}: {e}")

    def stop(self):
        self._save_history_id()
        super().stop()
