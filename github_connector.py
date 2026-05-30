"""GitHub polling connector for claudecode-telegram bridge.

Polls GitHub issue/PR comments via beast CLI for new comments from a
configured user, parses @worker mentions, and delivers to workers.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from base_connector import BaseConnector


class GitHubConnector(BaseConnector):

    connector_name = "github"

    def __init__(
        self,
        gh_bin: str = "",
        repo: str = "",
        from_user: str = "",
        poll_interval: int = 60,
        on_message: Callable = None,
        get_registered_workers: Callable = None,
        on_alert: Optional[Callable] = None,
        beast_bin: str = "",
    ):
        super().__init__(
            sender_filter=from_user,
            poll_interval=poll_interval,
            on_message=on_message,
            get_registered_workers=get_registered_workers,
            on_alert=on_alert,
        )
        self.beast_bin = beast_bin or os.path.expanduser("~/bin/beast")
        self.repo = repo
        self.from_user = self.sender_filter
        self._last_poll_time: Optional[str] = None
        self._seen_ids: set = set()

    def preflight_check(self) -> tuple:
        if not os.path.isfile(self.beast_bin):
            return False, f"beast binary not found: {self.beast_bin}"
        raw = self._run_beast("github", "cache-status", "--json", timeout=10)
        if raw is None:
            return False, "beast github not responding"
        return True, "OK"

    def _on_preflight_ok(self):
        self._last_poll_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Only seed on first-ever run (no seen_ids in DB).
        # On restart, skip seed so comments posted during downtime get delivered.
        if not self._has_seen_ids():
            seed = self._get_all_comments(self._last_poll_time)
            if seed:
                print(f"[github] First run — seeded {len(seed)} existing events as seen")
            else:
                print(f"[github] First run — no events to seed")
        else:
            print(f"[github] Restart — {self._seen_id_count()} events already tracked, skipping seed")
        print(f"[github] Started (interval={self.poll_interval}s, repo={self.repo}, user={self.from_user}, since={self._last_poll_time})")

    def _has_seen_ids(self) -> bool:
        raw = self._run_beast("github", "cache-status", "--json", timeout=10)
        if not raw:
            return False
        try:
            import sqlite3
            db_path = json.loads(raw).get("db_path", "")
            if not db_path:
                return False
            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM seen_ids WHERE repo='_events'").fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def _seen_id_count(self) -> int:
        try:
            import sqlite3
            raw = self._run_beast("github", "cache-status", "--json", timeout=10)
            if not raw:
                return 0
            db_path = json.loads(raw).get("db_path", "")
            conn = sqlite3.connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM seen_ids WHERE repo='_events'").fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def _run_beast(self, *args, timeout=30) -> Optional[str]:
        try:
            result = subprocess.run(
                [self.beast_bin] + list(args),
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                print(f"[github] beast error: {result.stderr.strip()}")
                return None
            return result.stdout.strip()
        except Exception as e:
            print(f"[github] beast exception: {e}")
            return None

    def _get_all_comments(self, since: str) -> Optional[list]:
        raw = self._run_beast(
            "github", "events",
            "--user", self.from_user,
            "--count", "50",
            "--json", "--new-only",
            timeout=30,
        )
        if raw is None:
            return None
        try:
            comments = json.loads(raw)
            for c in comments:
                if isinstance(c.get("user"), str):
                    c["user"] = {"login": c["user"]}
            return comments
        except json.JSONDecodeError as e:
            print(f"[github] beast JSON error: {e}")
            return None

    def is_from_target_user(self, comment: dict) -> bool:
        user = comment.get("user", {}).get("login", "").lower()
        return user == self.from_user

    def extract_issue_context(self, comment: dict) -> dict:
        if comment.get("issue_num") and comment.get("kind"):
            return {
                "number": str(comment["issue_num"]),
                "kind": comment["kind"],
                "url": comment.get("html_url", ""),
            }
        issue_url = comment.get("issue_url", "") or comment.get("pull_request_url", "")
        html_url = comment.get("html_url", "")
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
        result = []
        in_quote = False
        quote_lines = []
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

    def format_comment(self, body: str, context: dict) -> tuple:
        body = self._truncate(body)
        kind = context.get("kind", "comment")
        number = context.get("number", "")
        url = context.get("url", "")
        tag = f"#{number}" if number else ""
        link = f'<a href="{url}">#{number}</a>' if url and number else tag
        html = f"🔔 <b>GitHub {kind} {link}</b>\n\n{self._md_to_telegram_html(body)}"
        plain = f"manager (via GitHub {kind} {tag}): {body}"
        return html, plain

    def _process_comment(self, comment: dict):
        comment_id = comment.get("id")
        if comment_id in self._seen_ids:
            return
        self._seen_ids.add(comment_id)
        if not self.is_from_target_user(comment):
            return
        body = comment.get("body", "").strip()
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
        self.on_message(targets, html_text, plain_text, [])
        print(f"[github] {context['kind']} #{context['number']}: -> {targets or 'Telegram only'}")

    def poll_once(self):
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

    def _prune_seen_ids(self):
        if len(self._seen_ids) > 500:
            self._seen_ids = set(list(self._seen_ids)[-200:])
