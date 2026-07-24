"""Tests for github_connector.py"""

import json
import time
import threading
from unittest.mock import patch, MagicMock

import pytest
from github_connector import GitHubConnector
from base_connector import CONSECUTIVE_FAIL_WARN


def make_connector(**overrides):
    defaults = {
        "gh_bin": "/usr/bin/gh",
        "repo": "BasedHardware/omi",
        "from_user": "beastoin",
        "poll_interval": 60,
        "on_message": MagicMock(),
        "get_registered_workers": lambda: {"mon", "taro", "lee"},
    }
    defaults.update(overrides)
    return GitHubConnector(**defaults)


def make_comment(user="beastoin", body="check this", comment_id=1,
                 issue_url="https://api.github.com/repos/BasedHardware/omi/issues/100",
                 html_url="https://github.com/BasedHardware/omi/issues/100#issuecomment-1"):
    return {
        "id": comment_id,
        "user": {"login": user},
        "body": body,
        "issue_url": issue_url,
        "html_url": html_url,
        "created_at": "2026-05-26T10:00:00Z",
    }


def make_pr_comment(user="beastoin", body="fix this", comment_id=2,
                    html_url="https://github.com/BasedHardware/omi/pull/200#discussion_r2"):
    return {
        "id": comment_id,
        "user": {"login": user},
        "body": body,
        "pull_request_url": "https://api.github.com/repos/BasedHardware/omi/pulls/200",
        "html_url": html_url,
        "created_at": "2026-05-26T10:00:00Z",
    }


# --- Construction ---

class TestConstruct:
    def test_import_and_construct(self):
        gc = make_connector()
        assert gc.repo == "BasedHardware/omi"
        assert gc.sender_filter == "beastoin"
        assert gc.poll_interval == 60

    def test_from_user_lowered(self):
        gc = make_connector(from_user="Beastoin")
        assert gc.sender_filter == "beastoin"


# --- User filter ---

class TestUserFilter:
    def test_target_user_match(self):
        gc = make_connector(from_user="beastoin")
        assert gc.is_allowed_sender(make_comment(user="beastoin")) is True

    def test_target_user_case_insensitive(self):
        gc = make_connector(from_user="beastoin")
        assert gc.is_allowed_sender(make_comment(user="Beastoin")) is True

    def test_other_user_rejected(self):
        gc = make_connector(from_user="beastoin")
        assert gc.is_allowed_sender(make_comment(user="someone")) is False


# --- Issue context extraction ---

class TestExtractContext:
    def test_issue_comment(self):
        gc = make_connector()
        comment = make_comment(
            issue_url="https://api.github.com/repos/BasedHardware/omi/issues/100",
            html_url="https://github.com/BasedHardware/omi/issues/100#issuecomment-1",
        )
        ctx = gc.extract_issue_context(comment)
        assert ctx["number"] == "100"
        assert ctx["kind"] == "issue"
        assert "issues/100" in ctx["url"]

    def test_pr_comment_from_html_url(self):
        gc = make_connector()
        comment = make_pr_comment(
            html_url="https://github.com/BasedHardware/omi/pull/200#discussion_r2"
        )
        ctx = gc.extract_issue_context(comment)
        assert ctx["number"] == "200"
        assert ctx["kind"] == "PR"


# --- Mention parsing ---

class TestParseMentions:
    def test_single_mention(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("@mon check the deploy")
        assert targets == ["mon"]
        assert "mon" not in cleaned
        assert "check the deploy" in cleaned

    def test_multi_mention(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("@mon @taro both look at this")
        assert "mon" in targets
        assert "taro" in targets

    def test_unregistered_mention_kept(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("@unknown check this")
        assert targets == []
        assert "@unknown" in cleaned

    def test_no_mentions(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("general comment")
        assert targets == []

    def test_empty_text(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("")
        assert targets == []


# --- Format comment ---

class TestFormatComment:
    def test_issue_comment(self):
        gc = make_connector()
        ctx = {"kind": "issue", "number": "100", "url": "https://github.com/BasedHardware/omi/issues/100"}
        html, plain = gc.format_comment("check the logs", ctx)
        assert "🔔" in html
        assert "#100" in html
        assert "GitHub issue" in html
        assert "check the logs" in html
        assert "manager (via GitHub issue #100)" in plain

    def test_pr_comment(self):
        gc = make_connector()
        ctx = {"kind": "PR", "number": "200", "url": "https://github.com/BasedHardware/omi/pull/200"}
        html, plain = gc.format_comment("fix the types", ctx)
        assert "GitHub PR" in html
        assert "#200" in html

    def test_html_escaping(self):
        gc = make_connector()
        ctx = {"kind": "issue", "number": "1", "url": ""}
        html, plain = gc.format_comment("a < b & c > d", ctx)
        assert "&lt;" in html
        assert "&amp;" in html

    def test_long_body_truncated(self):
        gc = make_connector()
        ctx = {"kind": "issue", "number": "1", "url": ""}
        long_body = "line\n" * 500
        html, plain = gc.format_comment(long_body, ctx)
        assert "truncated" in plain


# --- Markdown to Telegram HTML ---

class TestMdToTelegramHtml:
    def test_headers_to_bold(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("## CP9 Live Test Evidence")
        assert "<b>CP9 Live Test Evidence</b>" in result

    def test_code_fences_to_pre(self):
        gc = make_connector()
        md = "```bash\necho hello\n```"
        result = gc._md_to_telegram_html(md)
        assert "<pre>" in result
        assert "echo hello" in result
        assert "```" not in result

    def test_inline_code(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("run `beast github show 100`")
        assert "<code>beast github show 100</code>" in result

    def test_bold_stars(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("this is **important**")
        assert "<b>important</b>" in result

    def test_italic(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("this is *italic* text")
        assert "<i>italic</i>" in result

    def test_image_links_stripped(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("![App launch](https://storage.example.com/img.png)")
        assert "https://storage" not in result
        assert "App launch" in result

    def test_links_converted(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("[click here](https://example.com)")
        assert '<a href="https://example.com">click here</a>' in result

    def test_blockquotes(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("> quoted text\n> more quote")
        assert "<blockquote>" in result
        assert "quoted text" in result

    def test_html_still_escaped(self):
        gc = make_connector()
        result = gc._md_to_telegram_html("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result

    def test_real_github_comment(self):
        gc = make_connector()
        md = """## CP9 — Live Test Evidence

### CP9A — Build and run

**Build:**
```
$ swift build -c debug
Build complete! (11.34s)
```

**Unit tests (8/8 pass):**
```
Executed 8 tests, with 0 failures
```

![screenshot](https://storage.example.com/img.png)"""
        result = gc._md_to_telegram_html(md)
        assert "<b>CP9 — Live Test Evidence</b>" in result
        assert "<pre>" in result
        assert "Build complete!" in result
        assert "screenshot" in result
        assert "```" not in result
        assert "##" not in result


# --- Process comment ---

class TestProcessComment:
    def test_delivers_comment_with_mentions(self):
        gc = make_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        comment = make_comment(user="beastoin", body="@mon check deploy")
        gc._process_comment(comment)
        gc.on_message.assert_called_once()
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert "mon" in targets
        assert "check deploy" in plain
        assert "beast github show 100" in plain
        assert "beast github comment 100" in plain
        assert "--body" in plain

    def test_wrong_user_skipped(self):
        gc = make_connector(from_user="beastoin")
        comment = make_comment(user="someone-else", body="hello")
        gc._process_comment(comment)
        gc.on_message.assert_not_called()

    def test_empty_body_skipped(self):
        gc = make_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="   ")
        gc._process_comment(comment)
        gc.on_message.assert_not_called()

    def test_no_mentions_still_delivers(self):
        gc = make_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="general note")
        gc._process_comment(comment)
        gc.on_message.assert_called_once()
        targets, _, _, _ = gc.on_message.call_args[0]
        assert targets == []

    def test_pr_comment_reply_hint(self):
        gc = make_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        comment = make_pr_comment(user="beastoin", body="@mon review this fix")
        gc._process_comment(comment)
        gc.on_message.assert_called_once()
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert "mon" in targets
        assert "beast github show 200" in plain
        assert "beast github comment 200" in plain

    def test_no_reply_hint_without_mentions(self):
        gc = make_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="general note")
        gc._process_comment(comment)
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert targets == []
        assert "beast github comment" not in plain

    def test_dedup_same_comment(self):
        gc = make_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="hello", comment_id=42)
        gc._process_comment(comment)
        gc._process_comment(comment)
        assert gc.on_message.call_count == 1


# --- Poll cycle ---

class TestPollOnce:
    def test_processes_issue_and_pr_comments(self):
        gc = make_connector(from_user="beastoin")
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        issue_comment = make_comment(user="beastoin", body="issue note", comment_id=1)
        pr_comment = make_pr_comment(user="beastoin", body="pr note", comment_id=2)
        with patch.object(gc, "_get_all_comments", return_value=[issue_comment, pr_comment]):
            gc.poll_once()
        assert gc.on_message.call_count == 2

    def test_skips_without_last_poll_time(self):
        gc = make_connector()
        gc._last_poll_time = None
        gc.poll_once()
        gc.on_message.assert_not_called()

    def test_failure_increments_counter(self):
        gc = make_connector()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        with patch.object(gc, "_get_all_comments", return_value=None):
            gc.poll_once()
        assert gc._consecutive_failures == 1

    def test_success_resets_counter(self):
        gc = make_connector()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_all_comments", return_value=[]):
            gc.poll_once()
        assert gc._consecutive_failures == 0

    def test_individual_failure_doesnt_block(self):
        gc = make_connector(from_user="beastoin")
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        bad_comment = {"id": 1, "user": {"login": "beastoin"}, "body": "ok"}
        good_comment = make_comment(user="beastoin", body="good", comment_id=2)
        with patch.object(gc, "_get_all_comments", return_value=[bad_comment, good_comment]), \
             patch.object(gc, "extract_issue_context", side_effect=[Exception("bad"), {"kind": "issue", "number": "1", "url": ""}]):
            gc.poll_once()
        assert gc.on_message.call_count == 1


# --- Alerts ---

class TestAlerts:
    def test_alert_on_consecutive_failures(self):
        gc = make_connector()
        gc.on_alert = MagicMock()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        with patch.object(gc, "_get_all_comments", return_value=None):
            for _ in range(CONSECUTIVE_FAIL_WARN):
                gc.poll_once()
        gc.on_alert.assert_called_once()

    def test_recovery_clears_alert(self):
        gc = make_connector()
        gc.on_alert = MagicMock()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        gc._consecutive_failures = 3
        gc._alert_sent = True
        with patch.object(gc, "_get_all_comments", return_value=[]):
            gc.poll_once()
        gc.on_alert.assert_called_once()
        assert "Recovered" in gc.on_alert.call_args[0][0]


# --- Seen ID pruning ---

class TestPrune:
    def test_prune_keeps_recent(self):
        gc = make_connector()
        gc._seen_ids = set(range(600))
        gc._prune_seen_ids()
        assert len(gc._seen_ids) == 200


# --- Lifecycle ---

class TestLifecycle:
    def test_start_creates_daemon_thread(self):
        gc = make_connector()
        with patch.object(gc, "_poll_loop"):
            t = gc.start()
            assert t.daemon is True
            assert t.name == "github-poller"
            gc.stop()

    def test_stop_terminates(self):
        gc = make_connector()
        gc._stop_event = threading.Event()
        gc._thread = threading.Thread(target=lambda: gc._stop_event.wait(10))
        gc._thread.daemon = True
        gc._thread.start()
        gc.stop()
        assert not gc._thread.is_alive()
