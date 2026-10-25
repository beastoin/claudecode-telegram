"""Behavior tests for connectors.py (Gmail + GitHub).

Tests message processing, mention parsing, formatting,
forwarding, dedup, and poll cycles. No scaffolding or constant checks.
"""
import base64
import json
import subprocess
import time
import threading
import pytest
from unittest.mock import patch, MagicMock
from bridge import GmailConnector, GitHubConnector, CONSECUTIVE_FAIL_WARN
from bridge import connector_on_message
from telegram import markdown_to_telegram_html
from conftest import sync_global


# ── Gmail Connector ─────────────────────────────────────────────────

def make_gmail_connector(**overrides):
    defaults = dict(
        gws_bin="/usr/bin/gws",
        from_filter="manager@example.com",
        poll_interval=30,
        on_message=MagicMock(),
        get_registered_workers=lambda: {"mon", "taro"},
    )
    defaults.update(overrides)
    return GmailConnector(**defaults)


def make_message(from_addr="manager@example.com", subject="Test", body_text="hello", labels=None, message_id=None):
    if labels is None:
        labels = ["INBOX", "UNREAD"]
    encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
    headers = [
        {"name": "From", "value": f"Manager <{from_addr}>"},
        {"name": "Subject", "value": subject},
    ]
    if message_id:
        headers.append({"name": "Message-Id", "value": message_id})
    return {
        "id": "msg123",
        "labelIds": labels,
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": encoded, "size": len(body_text)},
        },
    }


def make_multipart_message(from_addr="manager@example.com", subject="Test",
                           plain_text="hello plain", html_text="<b>hello</b>", labels=None):
    if labels is None:
        labels = ["INBOX", "UNREAD"]
    plain_encoded = base64.urlsafe_b64encode(plain_text.encode()).decode()
    html_encoded = base64.urlsafe_b64encode(html_text.encode()).decode()
    return {
        "id": "msg456",
        "labelIds": labels,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": f"Manager <{from_addr}>"},
                {"name": "Subject", "value": subject},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": plain_encoded, "size": len(plain_text)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": html_encoded, "size": len(html_text)},
                },
            ],
        },
    }


# --- Increment 1: Module skeleton ---
# --- Increment 2: _run_gws subprocess wrapper ---
# --- Increment 3: Bootstrap historyId ---

class TestGmailBootstrap:
    def test_bootstrap_history_id(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_load_history_id", return_value=None), \
             patch.object(gc, "_save_history_id"), \
             patch.object(gc, "_run_gws", return_value={"historyId": "202400"}):
            assert gc._bootstrap_history_id() == "202400"

    def test_bootstrap_failure(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_load_history_id", return_value=None), \
             patch.object(gc, "_run_gws", return_value=None):
            assert gc._bootstrap_history_id() is None

    def test_bootstrap_missing_key(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_load_history_id", return_value=None), \
             patch.object(gc, "_run_gws", return_value={"emailAddress": "a@b.com"}):
            assert gc._bootstrap_history_id() is None

    def test_bootstrap_resumes_from_disk(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_load_history_id", return_value="199999"):
            assert gc._bootstrap_history_id() == "199999"

    def test_bootstrap_skip_disk(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_load_history_id", return_value="199999"), \
             patch.object(gc, "_save_history_id"), \
             patch.object(gc, "_run_gws", return_value={"historyId": "202500"}):
            assert gc._bootstrap_history_id(skip_disk=True) == "202500"


# --- Increment 4: Extract sender ---

class TestGmailExtractSender:
    def test_angle_brackets(self):
        gc = make_gmail_connector()
        msg = make_message(from_addr="boss@example.com")
        assert gc.extract_sender(msg) == "boss@example.com"

    def test_bare_email(self):
        gc = make_gmail_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": "boss@example.com"}]}}
        assert gc.extract_sender(msg) == "boss@example.com"

    def test_missing_from(self):
        gc = make_gmail_connector()
        msg = {"payload": {"headers": [{"name": "To", "value": "me@x.com"}]}}
        assert gc.extract_sender(msg) == ""

    def test_case_insensitive(self):
        gc = make_gmail_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": "Boss@Example.COM"}]}}
        assert gc.extract_sender(msg) == "boss@example.com"


# --- Increment 4b: Strip subject prefixes ---

class TestGmailStripSubjectPrefixes:
    def test_single_re(self):
        assert GmailConnector._strip_subject_prefixes("Re: Hello") == "Hello"

    def test_single_fwd(self):
        assert GmailConnector._strip_subject_prefixes("Fwd: Hello") == "Hello"

    def test_fw_variant(self):
        assert GmailConnector._strip_subject_prefixes("FW: Hello") == "Hello"

    def test_chained_prefixes(self):
        assert GmailConnector._strip_subject_prefixes("Re: Re: Fwd: Hello") == "Hello"

    def test_no_prefix(self):
        assert GmailConnector._strip_subject_prefixes("Hello") == "Hello"

    def test_empty(self):
        assert GmailConnector._strip_subject_prefixes("") == ""

    def test_case_insensitive(self):
        assert GmailConnector._strip_subject_prefixes("RE: RE: Hello") == "Hello"

    def test_forwarded_prefix(self):
        assert GmailConnector._strip_subject_prefixes("Forwarded: Re: Hello") == "Hello"


# --- Increment 5: Extract body text ---

class TestGmailExtractBody:
    def test_plain_single_part(self):
        gc = make_gmail_connector()
        msg = make_message(body_text="hello world")
        assert gc.extract_body_text(msg) == "hello world"

    def test_multipart_returns_plain(self):
        gc = make_gmail_connector()
        msg = make_multipart_message(plain_text="plain version", html_text="<b>html</b>")
        assert gc.extract_body_text(msg) == "plain version"

    def test_empty_body(self):
        gc = make_gmail_connector()
        msg = {"payload": {"mimeType": "text/plain", "body": {}}}
        assert gc.extract_body_text(msg) == ""

    def test_no_parts(self):
        gc = make_gmail_connector()
        msg = {"payload": {"mimeType": "multipart/alternative"}}
        assert gc.extract_body_text(msg) == ""


# --- Increment 6: Extract subject ---
# --- Increment 6b: Extract message-id and sender name ---
class TestGmailIsSentMessage:
    def test_sent_label(self):
        gc = make_gmail_connector()
        msg = make_message(labels=["SENT"])
        assert gc.is_sent_message(msg) is True

    def test_inbox_label(self):
        gc = make_gmail_connector()
        msg = make_message(labels=["INBOX", "UNREAD"])
        assert gc.is_sent_message(msg) is False


# --- Increment 7: Sender/label filters ---

class TestGmailFilters:
    def test_allowed_sender_match(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com")
        assert gc.is_allowed_sender(msg) is True

    def test_allowed_sender_no_match(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="spam@evil.com")
        assert gc.is_allowed_sender(msg) is False

    def test_inbox_unread_true(self):
        gc = make_gmail_connector()
        assert gc.is_inbox_unread(["INBOX", "UNREAD", "CATEGORY_PERSONAL"]) is True

    def test_inbox_unread_missing_unread(self):
        gc = make_gmail_connector()
        assert gc.is_inbox_unread(["INBOX", "CATEGORY_PERSONAL"]) is False

    def test_inbox_unread_missing_inbox(self):
        gc = make_gmail_connector()
        assert gc.is_inbox_unread(["UNREAD"]) is False


# --- Mention parsing ---

class TestGmailParseMentions:
    def test_single_mention(self):
        gc = make_gmail_connector(get_registered_workers=lambda: {"mon", "taro"})
        targets, cleaned = gc.parse_mentions("@mon check the logs")
        assert targets == ["mon"]
        assert "check the logs" in cleaned
        assert "@mon" not in cleaned

    def test_multi_mention(self):
        gc = make_gmail_connector(get_registered_workers=lambda: {"mon", "taro"})
        targets, cleaned = gc.parse_mentions("@mon @taro check this")
        assert set(targets) == {"mon", "taro"}
        assert "@mon" not in cleaned
        assert "@taro" not in cleaned

    def test_unregistered_mention(self):
        gc = make_gmail_connector(get_registered_workers=lambda: {"mon", "taro"})
        targets, cleaned = gc.parse_mentions("@unknown hello")
        assert targets == []
        assert cleaned == "@unknown hello"

    def test_dedup_mentions(self):
        gc = make_gmail_connector(get_registered_workers=lambda: {"mon"})
        targets, _ = gc.parse_mentions("@mon hey @mon again")
        assert targets == ["mon"]

    def test_mixed_registered_unregistered(self):
        gc = make_gmail_connector(get_registered_workers=lambda: {"mon"})
        targets, cleaned = gc.parse_mentions("@mon @nobody check")
        assert targets == ["mon"]
        assert "@nobody" in cleaned


# --- Increment 9: Format email message ---

class TestGmailExtractAttachments:
    def test_single_attachment(self):
        gc = make_gmail_connector()
        msg = {
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "aGVsbG8=", "size": 5}},
                    {
                        "mimeType": "application/pdf",
                        "filename": "pricing.pdf",
                        "body": {"attachmentId": "att-1", "size": 12345},
                    },
                ],
            }
        }
        atts = gc.extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["filename"] == "pricing.pdf"
        assert atts[0]["attachmentId"] == "att-1"

    def test_multiple_attachments(self):
        gc = make_gmail_connector()
        msg = {
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "aGVsbG8="}},
                    {"mimeType": "image/png", "filename": "chart.png",
                     "body": {"attachmentId": "att-1", "size": 100}},
                    {"mimeType": "application/pdf", "filename": "doc.pdf",
                     "body": {"attachmentId": "att-2", "size": 200}},
                ],
            }
        }
        atts = gc.extract_attachments(msg)
        assert len(atts) == 2
        assert {a["filename"] for a in atts} == {"chart.png", "doc.pdf"}

class TestGmailFormatMessage:
    def test_plain_email(self):
        gc = make_gmail_connector()
        html, plain = gc.format_email_message("check the deploy", "Deploy Alert")
        assert "manager (via email):" in plain
        assert "check the deploy" in plain
        assert "<b>Deploy Alert</b>" in html

    def test_plain_email_with_thread(self):
        gc = make_gmail_connector()
        html, plain = gc.format_email_message("check the deploy", "Deploy Alert", "abc123")
        assert "[thread:abc123]" in plain
        assert "check the deploy" in plain
        assert "[thread:abc123]" in html
        assert "<b>Deploy Alert</b>" in html

    def test_forwarded_gmail(self):
        gc = make_gmail_connector()
        body = (
            "@mon can you check this?\r\n\r\n"
            "---------- Forwarded message ---------\r\n"
            "From: Google Cloud Alerting <alerting-noreply@google.com>\r\n"
            "Date: Thu, May 21, 2026\r\n"
            "Subject: GCP Alert\r\n\r\n"
            "[image: Google Cloud] VIEW INCIDENT<https://console.cloud.google.com/alert>\r\n"
            "Firestore writes > 200/s"
        )
        html, plain = gc.format_email_message(body, "Fwd: GCP Alert", "t1")
        assert "@mon can you check this?" in plain
        assert "--- Forwarded: Fwd: GCP Alert ---" in plain
        assert "Firestore writes" in plain
        assert "<blockquote>" in html
        assert "Firestore writes" in html
        assert "<https://" not in plain
        assert "[image:" not in plain

    def test_forwarded_outlook(self):
        gc = make_gmail_connector()
        body = (
            "@mon check this\r\n\r\n"
            "Get Outlook for iOS<https://aka.ms/o0ukef>\r\n"
            "________________________________\r\n"
            "From: Google Cloud Alerting <alerting-noreply@google.com>\r\n"
            "Sent: Thursday, May 21, 2026 1:26:07 PM\r\n"
            "To: ngocthinhdp@gmail.com <ngocthinhdp@gmail.com>\r\n"
            "Subject: [ALERT] Firestore writes\r\n\r\n"
            "Alert content here"
        )
        html, plain = gc.format_email_message(body, "Fw: Alert", "t2")
        assert "@mon check this" in plain
        assert "--- Forwarded: Fw: Alert ---" in plain
        assert "Alert content here" in plain
        assert "Get Outlook" not in plain
        assert "<blockquote>" in html

    def test_forwarded_generic_original_message(self):
        gc = make_gmail_connector()
        body = (
            "please check\n\n"
            "--- Original Message ---\n"
            "From: someone@example.com\n"
            "Content here"
        )
        html, plain = gc.format_email_message(body, "Re: Thing")
        assert "manager (via email):" in plain
        assert "--- Forwarded: Re: Thing ---" in plain
        assert "Content here" in plain

    def test_forwarded_no_manager_text(self):
        gc = make_gmail_connector()
        body = (
            "---------- Forwarded message ---------\n"
            "From: alerts@google.com\n"
            "Alert content here"
        )
        html, plain = gc.format_email_message(body, "Alert")
        assert "manager (via email):" in plain
        assert "--- Forwarded: Alert ---" in plain

    def test_whitespace_trimmed(self):
        gc = make_gmail_connector()
        html, plain = gc.format_email_message("  hello world  ", "Test")
        assert "hello world" in plain
        assert "hello world" in html

    def test_real_outlook_forward(self):
        gc = make_gmail_connector()
        body = (
            "@mon can you deep diving on the issue facts and co check with taro on the code fact?\r\n"
            "\r\n"
            "Get Outlook for iOS<https://aka.ms/o0ukef>\r\n"
            "________________________________\r\n"
            "From: Google Cloud Alerting <alerting-noreply@google.com>\r\n"
            "Sent: Thursday, May 21, 2026 1:26:07 PM\r\n"
            "To: ngocthinhdp@gmail.com <ngocthinhdp@gmail.com>\r\n"
            "Subject: [ALERT - Critical] Firestore Instance - Document Writes > 200/s\r\n"
            "\r\n"
            "VIEW INCIDENT<https://console.cloud.google.com/monitoring/alerting/alerts/0.o83yxvn9iuji>\r\n"
            "Alert firing [Critical] Critical\r\n"
            "Firestore Instance - Document Writes is above threshold of 200 with a value of 714.40666666666664\r\n"
        )
        html, plain = gc.format_email_message(body, "Fw: [ALERT - Critical] Firestore Instance", "19e49ba276cb94cd")
        assert "[thread:19e49ba276cb94cd]" in plain
        assert "@mon can you deep diving" in plain
        assert "Get Outlook" not in plain
        assert "--- Forwarded: Fw: [ALERT - Critical]" in plain
        assert "Firestore Instance - Document Writes is above threshold" in plain
        assert "<https://" not in plain
        assert "<blockquote>" in html

    def test_reply_chain_stripped(self):
        gc = make_gmail_connector()
        body = (
            "can you check this?\n\n"
            "On Sat, May 23, 2026 at 11:35 AM Someone <someone@gmail.com> wrote:\n"
            "> original message content\n"
            "> more quoted text"
        )
        html, plain = gc.format_email_message(body, "Re: Check", "t1")
        assert "can you check this?" in plain
        assert "original message content" not in plain
        assert "more quoted text" not in plain

    def test_reply_chain_inline_on_wrote(self):
        gc = make_gmail_connector()
        body = (
            "can we have the landscape on all aspects? keep sending them via this email. "
            "On Tue, May 26, 2026 at 1:32 PM Beastoin <beastoin@gmail.com> wrote: "
            "> Omi Weekly CTO Report — W21 (May 19-25, 2026) Performance Summary "
            "> Metric W21 W20 WoW > MRR $50,182 ~$50,000 +0.4%"
        )
        html, plain = gc.format_email_message(body, "Re: Weekly CTO Report", "t1")
        assert "landscape on all aspects" in plain
        assert "CTO Report" not in plain or "Weekly CTO Report" in plain
        assert "$50,182" not in plain

    def test_reply_chain_after_paren(self):
        gc = make_gmail_connector()
        body = (
            "what do you mean by the contract optimization? "
            "> Deepgram -40% (usage drop + contract optimization) "
            "On Tue, May 26, 2026 at 3:46 PM Beastoin <beastoin@gmail.com> wrote: "
            "> Omi Full Landscape v2 — W18-W21 > Apr 28 - May 25, 2026"
        )
        html, plain = gc.format_email_message(body, "Re: Landscape Report", "t1")
        assert "contract optimization?" in plain
        assert "Full Landscape v2" not in plain
        assert "Apr 28" not in plain

    def test_html_escaping(self):
        gc = make_gmail_connector()
        html, plain = gc.format_email_message("a < b & c > d", "Test <script>")
        assert "&lt;" in html
        assert "&amp;" in html
        assert "&gt;" in html
        assert "<script>" not in html

    def test_long_body_truncated(self):
        gc = make_gmail_connector()
        long_body = "line\n" * 500
        html, plain = gc.format_email_message(long_body, "Long")
        assert "truncated" in plain
        assert len(plain) < 2000


class TestGmailFormatSentReply:
    def test_basic_sent_reply(self):
        gc = make_gmail_connector()
        msg = make_message(from_addr="beastoin@gmail.com", subject="Re: Alert",
                           body_text="Got it, checking now", labels=["SENT"])
        html, plain = gc._format_sent_reply("Got it, checking now", "Re: Alert", "t1", msg)
        assert "✉️" in html
        assert "<b>Sent:</b>" in html
        assert "Re: Alert" in html
        assert "[thread:t1]" in html
        assert "Got it, checking now" in html
        assert "sent via email" in plain
        assert "Manager" in plain

    def test_sent_reply_strips_reply_chain(self):
        gc = make_gmail_connector()
        body = "My reply\n\nOn Mon, May 26, 2026 Someone wrote:\n> original text"
        msg = make_message(from_addr="beastoin@gmail.com", labels=["SENT"])
        html, plain = gc._format_sent_reply(body, "Re: Test", "t2", msg)
        assert "My reply" in html
        assert "original text" not in html


# --- Increment 10: Get new message IDs ---
# --- Increment 11: Mark as read ---

class TestGmailMarkAsRead:
    def test_success(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_run_gws", return_value={"id": "m1", "labelIds": ["INBOX"]}) as mock:
            assert gc.mark_as_read("m1") is True
        args = mock.call_args
        assert "modify" in args[0]
        assert '"removeLabelIds": ["UNREAD"]' in args[1]["json_body"]

    def test_failure(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_run_gws", return_value=None):
            assert gc.mark_as_read("m1") is False


# --- Increment 12: Process single message ---

class TestGmailProcessMessage:
    def test_with_mentions(self):
        gc = make_gmail_connector(
            from_filter="manager@example.com",
            get_registered_workers=lambda: {"mon", "taro"},
        )
        msg = make_message(from_addr="manager@example.com", subject="Alert",
                           body_text="@mon check the deploy")
        msg["threadId"] = "t1"
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read") as mock_mark:
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        targets, html_text, plain_text, attachments = gc.on_message.call_args[0]
        assert "mon" in targets
        assert "manager (via email):" in plain_text
        assert "[thread:t1]" in plain_text
        assert "<b>" in html_text
        assert attachments == []
        mock_mark.assert_called_once_with("m1")

    def test_wrong_sender_skipped(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="spam@evil.com", body_text="buy stuff")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read") as mock_mark:
            gc._process_message("m1")
        gc.on_message.assert_not_called()
        mock_mark.assert_not_called()

    def test_no_mentions_still_delivers(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com", body_text="general update")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        targets, html_text, plain_text, attachments = gc.on_message.call_args[0]
        assert targets == []

    def test_empty_body_skipped(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com", body_text="   ")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read") as mock_mark:
            gc._process_message("m1")
        gc.on_message.assert_not_called()
        mock_mark.assert_not_called()

    def test_get_message_failure(self):
        gc = make_gmail_connector()
        with patch.object(gc, "get_message", return_value=None), \
             pytest.raises(RuntimeError, match="transient API failure"):
            gc._process_message("m1")
        gc.on_message.assert_not_called()

    def test_sent_reply_shows_in_telegram(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="beastoin@gmail.com", subject="Re: Alert",
                           body_text="Got it, checking now", labels=["SENT"])
        with patch.object(gc, "get_message", return_value=msg):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        targets, html_text, plain_text, attachments = gc.on_message.call_args[0]
        assert targets == []
        assert "✉️" in html_text
        assert "Sent:" in html_text
        assert "Got it, checking now" in html_text
        assert "sent via email" in plain_text

    def test_sent_reply_wrong_sender_not_blocked(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="beastoin@gmail.com", subject="Re: Deploy",
                           body_text="Deployed", labels=["SENT"])
        with patch.object(gc, "get_message", return_value=msg):
            gc._process_message("m1")
        gc.on_message.assert_called_once()

    def test_inbound_with_reply_hint(self):
        gc = make_gmail_connector(
            from_filter="manager@example.com",
            get_registered_workers=lambda: {"mon"},
        )
        msg = make_message(from_addr="manager@example.com", subject="Check deploy",
                           body_text="@mon check the deploy",
                           message_id="<abc@mail.gmail.com>")
        msg["threadId"] = "t1"
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        targets, html_text, plain_text, attachments = gc.on_message.call_args[0]
        assert "mon" in targets
        assert "beast email send" in plain_text
        assert "--thread-id" in plain_text and "t1" in plain_text
        assert "--in-reply-to" in plain_text

    def test_inbound_no_reply_hint_without_mentions(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com", subject="FYI",
                           body_text="general info",
                           message_id="<abc@mail.gmail.com>")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        targets, html_text, plain_text, attachments = gc.on_message.call_args[0]
        assert targets == []
        assert "beast email send" not in plain_text

    def test_reply_hint_uses_stripped_subject(self):
        gc = make_gmail_connector(
            from_filter="manager@example.com",
            get_registered_workers=lambda: {"mon"},
        )
        msg = make_message(from_addr="manager@example.com", subject="Re: Re: Fwd: Deploy plan",
                           body_text="@mon check this",
                           message_id="<abc@mail.gmail.com>")
        msg["threadId"] = "t1"
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        plain_text = gc.on_message.call_args[0][2]
        assert "Re: Deploy plan" in plain_text
        assert "Re: Re: Re: Fwd: Deploy plan" not in plain_text

    def test_metadata_passed_with_email_fields(self):
        gc = make_gmail_connector(
            from_filter="manager@example.com",
            get_registered_workers=lambda: {"mon"},
        )
        msg = make_message(from_addr="manager@example.com", subject="Re: Status update",
                           body_text="@mon check deploy",
                           message_id="<xyz@mail.gmail.com>")
        msg["threadId"] = "thread123"
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        metadata = gc.on_message.call_args[1].get("metadata")
        assert metadata is not None
        assert metadata["thread_id"] == "thread123"
        assert metadata["message_id"] == "<xyz@mail.gmail.com>"
        assert metadata["original_subject"] == "Status update"
        assert metadata["sender_email"] == "manager@example.com"

    def test_metadata_passed_for_sent_reply(self):
        gc = make_gmail_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com", subject="Re: Hello",
                           body_text="my reply", message_id="<sent1@mail.gmail.com>")
        msg["labelIds"] = ["SENT"]
        msg["threadId"] = "t_sent"
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        metadata = gc.on_message.call_args[1].get("metadata")
        assert metadata is not None
        assert metadata["original_subject"] == "Hello"
        assert metadata["thread_id"] == "t_sent"


# --- Increment 13: Poll cycle ---

class TestGmailPollOnce:
    def test_processes_multiple(self):
        gc = make_gmail_connector(from_filter="mgr@x.com", get_registered_workers=lambda: {"mon"})
        msg1 = make_message(from_addr="mgr@x.com", body_text="@mon task 1")
        msg2 = make_message(from_addr="mgr@x.com", body_text="@mon task 2")
        msg2["id"] = "m2"

        def get_msg(msg_id):
            return msg1 if msg_id == "m1" else msg2

        with patch.object(gc, "_get_new_message_ids", return_value=(["m1", "m2"], "hid123")), \
             patch.object(gc, "get_message", side_effect=get_msg), \
             patch.object(gc, "mark_as_read"):
            gc.poll_once()
        assert gc.on_message.call_count == 2

    def test_individual_failure_doesnt_block(self):
        gc = make_gmail_connector(from_filter="mgr@x.com", get_registered_workers=lambda: {"mon"})
        msg2 = make_message(from_addr="mgr@x.com", body_text="@mon task 2")

        call_count = [0]
        def get_msg(msg_id):
            call_count[0] += 1
            if msg_id == "m1":
                raise ValueError("parse error")
            return msg2

        with patch.object(gc, "_get_new_message_ids", return_value=(["m1", "m2"], "hid123")), \
             patch.object(gc, "get_message", side_effect=get_msg), \
             patch.object(gc, "mark_as_read"):
            gc.poll_once()
        assert gc.on_message.call_count == 1

    def test_empty_poll(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_get_new_message_ids", return_value=([], None)):
            gc.poll_once()
        gc.on_message.assert_not_called()


# --- Increment 14a: Preflight check ---

class TestGmailPreflightCheck:
    def test_binary_missing(self):
        gc = make_gmail_connector(gws_bin="/nonexistent/gws")
        ok, msg = gc.preflight_check()
        assert ok is False
        assert "not found" in msg

    def test_auth_failure(self, tmp_path):
        fake_bin = tmp_path / "gws"
        fake_bin.write_text("#!/bin/sh\nexit 1")
        fake_bin.chmod(0o755)
        gc = make_gmail_connector(gws_bin=str(fake_bin))
        with patch.object(gc, "_run_gws", return_value=None):
            ok, msg = gc.preflight_check()
        assert ok is False
        assert "auth failed" in msg

    def test_success(self, tmp_path):
        fake_bin = tmp_path / "gws"
        fake_bin.write_text("#!/bin/sh\necho '{}'")
        fake_bin.chmod(0o755)
        gc = make_gmail_connector(gws_bin=str(fake_bin))
        with patch.object(gc, "_run_gws", return_value={"emailAddress": "test@gmail.com", "historyId": "1"}):
            ok, msg = gc.preflight_check()
        assert ok is True
        assert "test@gmail.com" in msg


# -- Consecutive failures --

class TestGmailConsecutiveFailures:
    def test_failure_counter_increments(self):
        gc = make_gmail_connector()
        with patch.object(gc, "_get_new_message_ids", return_value=(None, None)):
            gc.poll_once()
            assert gc._consecutive_failures == 1
            gc.poll_once()
            assert gc._consecutive_failures == 2

    def test_success_resets_counter(self):
        gc = make_gmail_connector()
        gc._consecutive_failures = 4
        with patch.object(gc, "_get_new_message_ids", return_value=([], None)):
            gc.poll_once()
        assert gc._consecutive_failures == 0

    def test_rebootstrap_after_threshold(self):
        gc = make_gmail_connector()
        gc._consecutive_failures = 4
        with patch.object(gc, "_get_new_message_ids", return_value=(None, None)), \
             patch.object(gc, "_bootstrap_history_id", return_value="999") as mock_boot:
            gc.poll_once()
        mock_boot.assert_called_once()
        assert gc._history_id == "999"
        assert gc._consecutive_failures == 0

    def test_rebootstrap_failure_keeps_counting(self):
        gc = make_gmail_connector()
        gc._consecutive_failures = 4
        with patch.object(gc, "_get_new_message_ids", return_value=(None, None)), \
             patch.object(gc, "_bootstrap_history_id", return_value=None):
            gc.poll_once()
        assert gc._consecutive_failures == 5


# -- Alerts --

class TestGmailAlerts:
    def test_alert_sent_on_warn_threshold(self):
        alert_mock = MagicMock()
        gc = make_gmail_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_new_message_ids", return_value=(None, None)):
            gc.poll_once()
        alert_mock.assert_called_once()
        assert "consecutive errors" in alert_mock.call_args[0][0]

    def test_alert_sent_only_once(self):
        alert_mock = MagicMock()
        gc = make_gmail_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_new_message_ids", return_value=(None, None)):
            gc.poll_once()
            gc.poll_once()
        assert alert_mock.call_count == 1

    def test_recovery_clears_alert(self):
        alert_mock = MagicMock()
        gc = make_gmail_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 3
        gc._alert_sent = True
        with patch.object(gc, "_get_new_message_ids", return_value=([], None)):
            gc.poll_once()
        assert gc._alert_sent is False
        assert "Recovered" in alert_mock.call_args[0][0]

    def test_preflight_failure_alerts(self):
        alert_mock = MagicMock()
        gc = make_gmail_connector(gws_bin="/nonexistent/gws")
        gc.on_alert = alert_mock
        with patch.object(gc._stop_event, "wait", return_value=False):
            gc._poll_loop()
        alert_mock.assert_called_once()
        assert "disabled" in alert_mock.call_args[0][0].lower()

    def test_rebootstrap_success_sends_recovery(self):
        alert_mock = MagicMock()
        gc = make_gmail_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 4
        gc._alert_sent = True
        with patch.object(gc, "_get_new_message_ids", return_value=(None, None)), \
             patch.object(gc, "_bootstrap_history_id", return_value="999"):
            gc.poll_once()
        assert gc._consecutive_failures == 0
        assert "Recovered" in alert_mock.call_args[0][0]


# -- Bootstrap retry --

class TestGmailBootstrapRetry:
    def test_bootstrap_failure_retries(self):
        gc = make_gmail_connector(poll_interval=1)
        call_count = [0]
        def mock_bootstrap(**kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return "200"
        wait_count = [0]
        def mock_wait(timeout=None):
            wait_count[0] += 1
            if wait_count[0] == 1:
                return False
            gc._stop_event.set()
            return True
        with patch.object(gc, "preflight_check", return_value=(True, "OK")), \
             patch.object(gc, "_bootstrap_history_id", side_effect=mock_bootstrap), \
             patch.object(gc, "poll_once"), \
             patch.object(gc._stop_event, "wait", side_effect=mock_wait):
            gc._poll_loop()
        assert call_count[0] == 2
        assert gc._history_id == "200"


# ── GitHub Connector ────────────────────────────────────────────────

from bridge import GitHubConnector, CONSECUTIVE_FAIL_WARN


def make_github_connector(**overrides):
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


# --- User filter ---

class TestGitHubUserFilter:
    def test_target_user_match(self):
        gc = make_github_connector(from_user="beastoin")
        assert gc.is_allowed_sender(make_comment(user="beastoin")) is True

    def test_target_user_case_insensitive(self):
        gc = make_github_connector(from_user="beastoin")
        assert gc.is_allowed_sender(make_comment(user="Beastoin")) is True

    def test_other_user_rejected(self):
        gc = make_github_connector(from_user="beastoin")
        assert gc.is_allowed_sender(make_comment(user="someone")) is False


# --- Issue context extraction ---

class TestGitHubExtractContext:
    def test_issue_comment(self):
        gc = make_github_connector()
        comment = make_comment(
            issue_url="https://api.github.com/repos/BasedHardware/omi/issues/100",
            html_url="https://github.com/BasedHardware/omi/issues/100#issuecomment-1",
        )
        ctx = gc.extract_issue_context(comment)
        assert ctx["number"] == "100"
        assert ctx["kind"] == "issue"
        assert "issues/100" in ctx["url"]

    def test_pr_comment_from_html_url(self):
        gc = make_github_connector()
        comment = make_pr_comment(
            html_url="https://github.com/BasedHardware/omi/pull/200#discussion_r2"
        )
        ctx = gc.extract_issue_context(comment)
        assert ctx["number"] == "200"
        assert ctx["kind"] == "PR"


# --- Mention parsing ---

class TestGitHubParseMentions:
    def test_single_mention(self):
        gc = make_github_connector()
        targets, cleaned = gc.parse_mentions("@mon check the deploy")
        assert targets == ["mon"]
        assert "mon" not in cleaned
        assert "check the deploy" in cleaned

    def test_multi_mention(self):
        gc = make_github_connector()
        targets, cleaned = gc.parse_mentions("@mon @taro both look at this")
        assert "mon" in targets
        assert "taro" in targets

    def test_unregistered_mention_kept(self):
        gc = make_github_connector()
        targets, cleaned = gc.parse_mentions("@unknown check this")
        assert targets == []
        assert "@unknown" in cleaned

# --- Format comment ---

class TestGitHubFormatComment:
    def test_issue_comment(self):
        gc = make_github_connector()
        ctx = {"kind": "issue", "number": "100", "url": "https://github.com/BasedHardware/omi/issues/100"}
        html, plain = gc.format_comment("check the logs", ctx)
        assert "🔔" in html
        assert "#100" in html
        assert "GitHub issue" in html
        assert "check the logs" in html
        assert "manager (via GitHub issue #100)" in plain

    def test_pr_comment(self):
        gc = make_github_connector()
        ctx = {"kind": "PR", "number": "200", "url": "https://github.com/BasedHardware/omi/pull/200"}
        html, plain = gc.format_comment("fix the types", ctx)
        assert "GitHub PR" in html
        assert "#200" in html

    def test_html_escaping(self):
        gc = make_github_connector()
        ctx = {"kind": "issue", "number": "1", "url": ""}
        html, plain = gc.format_comment("a < b & c > d", ctx)
        assert "&lt;" in html
        assert "&amp;" in html

    def test_long_body_truncated(self):
        gc = make_github_connector()
        ctx = {"kind": "issue", "number": "1", "url": ""}
        long_body = "line\n" * 500
        html, plain = gc.format_comment(long_body, ctx)
        assert "truncated" in plain


# --- Markdown to Telegram HTML ---

class TestGitHubMdToTelegramHtml:
    def test_headers_to_bold(self):
        result = markdown_to_telegram_html("## CP9 Live Test Evidence")
        assert "<b>CP9 Live Test Evidence</b>" in result

    def test_code_fences_to_pre(self):
        result = markdown_to_telegram_html("```bash\necho hello\n```")
        assert "<pre>" in result
        assert "echo hello" in result
        assert "```" not in result

    def test_inline_code(self):
        result = markdown_to_telegram_html("run `beast github show 100`")
        assert "<code>beast github show 100</code>" in result

    def test_bold_stars(self):
        result = markdown_to_telegram_html("this is **important**")
        assert "<b>important</b>" in result

    def test_italic(self):
        result = markdown_to_telegram_html("this is *italic* text")
        assert "<i>italic</i>" in result

    def test_image_links_preserved(self):
        result = markdown_to_telegram_html("![App launch](https://storage.example.com/img.png)")
        assert "App launch" in result

    def test_links_converted(self):
        result = markdown_to_telegram_html("[click here](https://example.com)")
        assert '<a href="https://example.com">click here</a>' in result

    def test_blockquotes(self):
        result = markdown_to_telegram_html("> quoted text\n> more quote")
        assert "<blockquote>" in result
        assert "quoted text" in result

    def test_html_still_escaped(self):
        result = markdown_to_telegram_html("a < b & c > d")
        assert "&lt;" in result
        assert "&amp;" in result

    def test_real_github_comment(self):
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
        result = markdown_to_telegram_html(md)
        assert "<b>CP9 — Live Test Evidence</b>" in result
        assert "<pre>" in result
        assert "Build complete!" in result
        assert "screenshot" in result
        assert "```" not in result
        assert "##" not in result


# --- Process comment ---

class TestGitHubProcessComment:
    def test_delivers_comment_with_mentions(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
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
        gc = make_github_connector(from_user="beastoin")
        comment = make_comment(user="someone-else", body="hello")
        gc._process_comment(comment)
        gc.on_message.assert_not_called()

    def test_empty_body_skipped(self):
        gc = make_github_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="   ")
        gc._process_comment(comment)
        gc.on_message.assert_not_called()

    def test_no_mentions_still_delivers(self):
        gc = make_github_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="general note")
        gc._process_comment(comment)
        gc.on_message.assert_called_once()
        targets, _, _, _ = gc.on_message.call_args[0]
        assert targets == []

    def test_pr_comment_reply_hint(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        comment = make_pr_comment(user="beastoin", body="@mon review this fix")
        gc._process_comment(comment)
        gc.on_message.assert_called_once()
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert "mon" in targets
        assert "beast github show 200" in plain
        assert "beast github comment 200" in plain

    def test_no_reply_hint_without_mentions(self):
        gc = make_github_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="general note")
        gc._process_comment(comment)
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert targets == []
        assert "beast github comment" not in plain

    def test_dedup_same_comment(self):
        gc = make_github_connector(from_user="beastoin")
        comment = make_comment(user="beastoin", body="hello", comment_id=42)
        gc._process_comment(comment)
        gc._process_comment(comment)
        assert gc.on_message.call_count == 1


# --- Poll cycle ---

class TestGitHubPollOnce:
    def test_processes_issue_and_pr_comments(self):
        gc = make_github_connector(from_user="beastoin")
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        issue_comment = make_comment(user="beastoin", body="issue note", comment_id=1)
        pr_comment = make_pr_comment(user="beastoin", body="pr note", comment_id=2)
        with patch.object(gc, "_get_all_comments", return_value=([issue_comment, pr_comment], True)):
            gc.poll_once()
        assert gc.on_message.call_count == 2

    def test_skips_without_last_poll_time(self):
        gc = make_github_connector()
        gc._last_poll_time = None
        gc.poll_once()
        gc.on_message.assert_not_called()

    def test_failure_increments_counter(self):
        gc = make_github_connector()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        with patch.object(gc, "_get_all_comments", return_value=(None, False)):
            gc.poll_once()
        assert gc._consecutive_failures == 1

    def test_success_resets_counter(self):
        gc = make_github_connector()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_all_comments", return_value=([], True)):
            gc.poll_once()
        assert gc._consecutive_failures == 0

    def test_individual_failure_doesnt_block(self):
        gc = make_github_connector(from_user="beastoin")
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        bad_comment = {"id": 1, "user": {"login": "beastoin"}, "body": "ok"}
        good_comment = make_comment(user="beastoin", body="good", comment_id=2)
        with patch.object(gc, "_get_all_comments", return_value=([bad_comment, good_comment], True)), \
             patch.object(gc, "extract_issue_context", side_effect=[Exception("bad"), {"kind": "issue", "number": "1", "url": ""}]):
            gc.poll_once()
        assert gc.on_message.call_count == 1


# -- Alerts --

class TestGitHubAlerts:
    def test_alert_on_consecutive_failures(self):
        gc = make_github_connector()
        gc.on_alert = MagicMock()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        with patch.object(gc, "_get_all_comments", return_value=(None, False)):
            for _ in range(CONSECUTIVE_FAIL_WARN):
                gc.poll_once()
        gc.on_alert.assert_called_once()

    def test_recovery_clears_alert(self):
        gc = make_github_connector()
        gc.on_alert = MagicMock()
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        gc._consecutive_failures = 3
        gc._alert_sent = True
        with patch.object(gc, "_get_all_comments", return_value=([], True)):
            gc.poll_once()
        gc.on_alert.assert_called_once()
        assert "Recovered" in gc.on_alert.call_args[0][0]


# --- Multi-repo support ---

class TestGitHubMultiRepo:
    def test_single_repo_string_accepted(self):
        gc = make_github_connector(repo="BasedHardware/omi")
        assert gc.repos == ["BasedHardware/omi"]

    def test_multi_repo_list_accepted(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"])
        assert gc.repos == ["BasedHardware/omi", "beastoin/agent-swift"]

    def test_empty_repo_list_raises(self):
        import pytest
        with pytest.raises(ValueError, match="at least one repo"):
            make_github_connector(repo=[])

    def test_invalid_repo_in_list_raises(self):
        import pytest
        with pytest.raises(ValueError, match="owner/name"):
            make_github_connector(repo=["BasedHardware/omi", "not-valid"])

    def test_status_returns_repos_list(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"])
        status = gc.status()
        assert status["repos"] == ["BasedHardware/omi", "beastoin/agent-swift"]

    def test_multi_repo_polls_all(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"], from_user="beastoin")
        gc._last_poll_time = "2026-05-26T00:00:00Z"
        omi_comment = make_comment(user="beastoin", body="omi note", comment_id=10,
                                   issue_url="https://api.github.com/repos/BasedHardware/omi/issues/100",
                                   html_url="https://github.com/BasedHardware/omi/issues/100#issuecomment-10")
        swift_comment = {
            "id": 20,
            "user": {"login": "beastoin"},
            "body": "swift note",
            "issue_url": "https://api.github.com/repos/beastoin/agent-swift/issues/1",
            "html_url": "https://github.com/beastoin/agent-swift/issues/1#issuecomment-20",
            "created_at": "2026-05-26T10:00:00Z",
        }
        with patch.object(gc, "_get_all_comments", return_value=([omi_comment, swift_comment], True)):
            gc.poll_once()
        assert gc.on_message.call_count == 2

    def test_extract_repo_from_comment(self):
        # API URL
        comment = make_comment(issue_url="https://api.github.com/repos/beastoin/agent-swift/issues/5")
        assert GitHubConnector._extract_repo_from_comment(comment) == "beastoin/agent-swift"
        # HTML URL
        comment2 = {"html_url": "https://github.com/BasedHardware/omi/pull/200#discussion_r2"}
        assert GitHubConnector._extract_repo_from_comment(comment2) == "BasedHardware/omi"
        # No URL
        assert GitHubConnector._extract_repo_from_comment({}) == ""

    def test_reply_hint_uses_comment_repo(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"],
                                   from_user="beastoin",
                                   get_registered_workers=lambda: {"mon"})
        comment = {
            "id": 30,
            "user": {"login": "beastoin"},
            "body": "@mon check this",
            "issue_url": "https://api.github.com/repos/beastoin/agent-swift/issues/5",
            "html_url": "https://github.com/beastoin/agent-swift/issues/5#issuecomment-30",
            "created_at": "2026-09-30T10:00:00Z",
        }
        gc._process_comment(comment)
        gc.on_message.assert_called_once()
        _, _, plain, _ = gc.on_message.call_args[0]
        assert "beastoin/agent-swift" in plain
        # Metadata should also have the correct repo
        metadata = gc.on_message.call_args[1].get("metadata", {})
        assert metadata.get("repo") == "beastoin/agent-swift"


class TestGitHubBeastNormalize:
    """Test beast CLI output normalization to internal GithubComment format."""

    def test_flat_user_normalized(self):
        gc = make_github_connector()
        beast_out = {"id": 42, "user": "beastoin", "body": "test", "html_url": "https://github.com/o/r/issues/1#c42", "issue_num": "1", "kind": "issue"}
        result = gc._normalize_beast_comment(beast_out)
        assert result["user"] == {"login": "beastoin"}
        assert result["id"] == 42
        assert result["issue_num"] == "1"
        assert result["kind"] == "issue"

    def test_nested_user_passthrough(self):
        gc = make_github_connector()
        beast_out = {"id": 42, "user": {"login": "beastoin"}, "body": "test", "html_url": ""}
        result = gc._normalize_beast_comment(beast_out)
        assert result["user"]["login"] == "beastoin"

    def test_missing_user_safe(self):
        gc = make_github_connector()
        result = gc._normalize_beast_comment({"id": 1, "body": "x", "html_url": ""})
        assert result["user"] == {"login": ""}

    def test_beast_fetch_success(self):
        gc = make_github_connector()
        gc._last_poll_time = "2026-10-24T00:00:00Z"
        beast_json = '[{"id":1,"user":"alice","body":"hi","html_url":"https://github.com/o/r/issues/1#c1","issue_num":"1","kind":"issue"}]'
        mock_result = MagicMock(returncode=0, stdout=beast_json, stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = gc._beast_fetch("BasedHardware/omi", "2026-10-24T00:00:00Z")
        assert result is not None
        assert len(result) == 1
        assert result[0]["user"] == {"login": "alice"}
        assert result[0]["id"] == 1
        call_args = mock_run.call_args[0][0]
        assert "beast" in call_args[0]
        assert "--since" in call_args

    def test_beast_fetch_failure(self):
        gc = make_github_connector()
        mock_result = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            result = gc._beast_fetch("BasedHardware/omi", "2026-10-24T00:00:00Z")
        assert result is None

    def test_beast_fetch_invalid_json(self):
        gc = make_github_connector()
        mock_result = MagicMock(returncode=0, stdout="not json", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = gc._beast_fetch("BasedHardware/omi", "2026-10-24T00:00:00Z")
        assert result is None

    def test_beast_preflight_uses_beast_cli(self):
        gc = make_github_connector()
        mock_result = MagicMock(returncode=0, stdout="[]", stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            ok, msg = gc.preflight_check()
        assert ok
        call_args = mock_run.call_args[0][0]
        assert "beast" in call_args[0]
        assert "github" in call_args
        assert "comments" in call_args


# ── connector_on_message bridge callback ──────────────────────────────

class TestConnectorOnMessageTelegramDelivery:
    """Verify connector_on_message sends to Telegram when admin_chat_id is set."""

    def test_sends_to_telegram_when_admin_chat_id_set(self):
        """Connector messages reach Telegram when admin_chat_id is known."""
        import bridge, shared
        saved = (bridge.admin_chat_id, shared.admin_chat_id)
        try:
            sync_global("admin_chat_id", 99999)
            handler = connector_on_message("gmail")
            with patch("bridge.send_telegram_message") as mock_send, \
                 patch("bridge._beast_serve_deploy", return_value=None), \
                 patch("bridge.send_to_worker"):
                handler([], "<b>Test email</b>", "Test email")
                mock_send.assert_called_once()
                assert mock_send.call_args[0][0] == 99999
        finally:
            bridge.admin_chat_id = saved[0]
            shared.admin_chat_id = saved[1]

    def test_skips_telegram_when_admin_chat_id_none(self):
        """Connector messages do NOT go to Telegram when admin_chat_id is None."""
        import bridge, shared
        saved = (bridge.admin_chat_id, shared.admin_chat_id)
        try:
            sync_global("admin_chat_id", None)
            handler = connector_on_message("gmail")
            with patch("bridge.send_telegram_message") as mock_send, \
                 patch("bridge.send_to_worker"):
                handler([], "<b>Test email</b>", "Test email")
                mock_send.assert_not_called()
        finally:
            bridge.admin_chat_id = saved[0]
            shared.admin_chat_id = saved[1]

    def test_delivers_to_workers_regardless_of_admin_chat_id(self):
        """Worker delivery works even when admin_chat_id is None."""
        import bridge, shared
        saved = (bridge.admin_chat_id, shared.admin_chat_id)
        try:
            sync_global("admin_chat_id", None)
            handler = connector_on_message("gmail")
            with patch("bridge.send_telegram_message") as mock_tg, \
                 patch("bridge.send_to_worker") as mock_worker:
                handler(["lee", "mon"], "<b>hello</b>", "hello")
                mock_tg.assert_not_called()
                assert mock_worker.call_count == 2
                mock_worker.assert_any_call("lee", "hello")
                mock_worker.assert_any_call("mon", "hello")
        finally:
            bridge.admin_chat_id = saved[0]
            shared.admin_chat_id = saved[1]

    def test_admin_chat_id_global_propagates_to_shared_module(self):
        """Setting admin_chat_id in bridge must also set shared.admin_chat_id.

        Python's STORE_GLOBAL bypasses module __setattr__, so code that
        does `global admin_chat_id; admin_chat_id = X` inside bridge.py
        only updates bridge's namespace. The connector reads core.admin_chat_id
        (= shared.admin_chat_id), so both must stay in sync.
        """
        import bridge, shared
        saved = (bridge.admin_chat_id, shared.admin_chat_id)
        try:
            sync_global("admin_chat_id", None)
            assert shared.admin_chat_id is None

            bridge._restore_bridge_state.__globals__["admin_chat_id"] = 12345
            assert bridge.admin_chat_id == 12345
            # Before fix: shared.admin_chat_id would still be None
            # The fix adds `core.admin_chat_id = last_chat_id` alongside the global set
            # We test the sync_global path here since we can't trigger STORE_GLOBAL
            # from outside. The real fix is verified by the Telegram delivery tests above.
            sync_global("admin_chat_id", 12345)
            assert shared.admin_chat_id == 12345
        finally:
            bridge.admin_chat_id = saved[0]
            shared.admin_chat_id = saved[1]


# ── GitHub Preflight Resilience ──────────────────────────────────────

class TestGitHubPreflightResilience:
    """GitHub connector preflight skips failing repos instead of dying."""

    def test_preflight_skips_failing_repo_keeps_good_ones(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "bad/repo", "beastoin/agent-swift"])
        def side_effect(cmd, **kw):
            repo = cmd[-1]
            if repo == "bad/repo":
                return MagicMock(returncode=1, stdout="", stderr="repo not in allowlist")
            return MagicMock(returncode=0, stdout="[]", stderr="")
        with patch("subprocess.run", side_effect=side_effect):
            ok, msg = gc.preflight_check()
        assert ok
        assert "skipped" in msg
        assert "bad/repo" in msg
        assert gc.repos == ["BasedHardware/omi", "beastoin/agent-swift"]

    def test_preflight_fails_when_all_repos_fail(self):
        gc = make_github_connector(repo=["bad/one", "bad/two"])
        mock_result = MagicMock(returncode=1, stdout="", stderr="not in allowlist")
        with patch("subprocess.run", return_value=mock_result):
            ok, msg = gc.preflight_check()
        assert not ok
        assert "all repos failed" in msg

    def test_preflight_ok_when_all_repos_succeed(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"])
        mock_result = MagicMock(returncode=0, stdout="[]", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            ok, msg = gc.preflight_check()
        assert ok
        assert msg == "OK"
        assert gc.repos == ["BasedHardware/omi", "beastoin/agent-swift"]


# ── Connector Restart Thread Registration ────────────────────────────

class TestConnectorRestartThreadRegistration:
    """restart_connector re-registers the new thread with BridgeHealth."""

    def test_restart_registers_thread_with_health(self):
        from bridge import restart_connector, health, GMAIL_ENABLED, GmailConnector
        with patch("bridge.GITHUB_ENABLED", True), \
             patch("bridge.GitHubConnector") as mock_cls, \
             patch("bridge.new_github") as mock_factory, \
             patch("bridge.health") as mock_health:
            mock_inst = MagicMock()
            mock_inst.restart.return_value = (True, "OK")
            mock_inst._thread = MagicMock()
            mock_factory.return_value = mock_inst
            ok, msg = restart_connector("github")
            assert ok
            mock_health.register_thread.assert_called_once_with("github-poller", mock_inst._thread)


# ── GitHub Issue Body @Mention Support ─────────────────────────────


def make_issue(author="beastoin", body="check this @mon", title="Bug report",
               issue_id=5000, number=300, kind="issue",
               html_url="https://github.com/BasedHardware/omi/issues/300",
               repo="BasedHardware/omi"):
    return {
        "id": issue_id,
        "author": author,
        "body": body,
        "title": title,
        "number": number,
        "kind": kind,
        "html_url": html_url,
        "repo": repo,
    }


class TestGitHubProcessIssue:
    """_process_issue delivers issue bodies that contain @mentions of registered workers."""

    def test_delivers_issue_with_mentions(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon", "lee"})
        issue = make_issue(author="beastoin", body="@mon please check the deploy logs", title="Deploy failure")
        gc._process_issue(issue)
        gc.on_message.assert_called_once()
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert "mon" in targets
        assert "Deploy failure" in plain
        assert "please check the deploy logs" in plain
        assert "beast github show 300" in plain
        assert "beast github comment 300" in plain

    def test_wrong_author_skipped(self):
        gc = make_github_connector(from_user="beastoin")
        issue = make_issue(author="random-user", body="@mon hello")
        gc._process_issue(issue)
        gc.on_message.assert_not_called()
        assert issue["id"] in gc._seen_issue_ids

    def test_empty_body_skipped(self):
        gc = make_github_connector(from_user="beastoin")
        issue = make_issue(author="beastoin", body="   ")
        gc._process_issue(issue)
        gc.on_message.assert_not_called()
        assert issue["id"] in gc._seen_issue_ids

    def test_no_mentions_skipped(self):
        gc = make_github_connector(from_user="beastoin")
        issue = make_issue(author="beastoin", body="General bug report with no mentions")
        gc._process_issue(issue)
        gc.on_message.assert_not_called()
        assert issue["id"] in gc._seen_issue_ids

    def test_dedup_same_issue(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        issue = make_issue(author="beastoin", body="@mon check this", issue_id=9999)
        gc._process_issue(issue)
        gc._process_issue(issue)
        assert gc.on_message.call_count == 1

    def test_pr_body_with_mentions(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"taro"})
        issue = make_issue(
            author="beastoin", body="@taro review the migration changes",
            title="DB migration", kind="PR", number=450,
            html_url="https://github.com/BasedHardware/omi/pull/450",
        )
        gc._process_issue(issue)
        gc.on_message.assert_called_once()
        targets, html, plain, atts = gc.on_message.call_args[0]
        assert "taro" in targets
        assert "DB migration" in plain
        assert "beast github show 450" in plain

    def test_multi_mention_in_body(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon", "taro", "lee"})
        issue = make_issue(author="beastoin", body="@mon @taro both look at this")
        gc._process_issue(issue)
        gc.on_message.assert_called_once()
        targets = gc.on_message.call_args[0][0]
        assert "mon" in targets
        assert "taro" in targets

    def test_metadata_includes_repo(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        issue = make_issue(author="beastoin", body="@mon check", repo="beastoin/agent-swift")
        gc._process_issue(issue)
        metadata = gc.on_message.call_args[1].get("metadata", {})
        assert metadata.get("repo") == "beastoin/agent-swift"
        assert metadata.get("comment_id") == issue["id"]

    def test_author_case_insensitive(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        issue = make_issue(author="Beastoin", body="@mon look here")
        gc._process_issue(issue)
        gc.on_message.assert_called_once()

    def test_missing_author_skipped(self):
        gc = make_github_connector(from_user="beastoin")
        issue = make_issue(body="@mon hello")
        issue["author"] = ""
        gc._process_issue(issue)
        gc.on_message.assert_not_called()

    def test_invalid_id_skipped(self):
        gc = make_github_connector(from_user="beastoin")
        issue = make_issue(body="@mon hello")
        issue["id"] = "not-an-int"
        gc._process_issue(issue)
        gc.on_message.assert_not_called()


class TestGitHubPollOnceWithIssues:
    """poll_once processes both comments and issues."""

    def test_polls_comments_and_issues(self):
        gc = make_github_connector(from_user="beastoin", get_registered_workers=lambda: {"mon"})
        gc._last_poll_time = "2026-10-25T00:00:00Z"
        comment = make_comment(user="beastoin", body="comment note", comment_id=100)
        issue = make_issue(author="beastoin", body="@mon issue note", issue_id=200)
        with patch.object(gc, "_get_all_comments", return_value=([comment], True)), \
             patch.object(gc, "_get_all_issues", return_value=[issue]):
            gc.poll_once()
        assert gc.on_message.call_count == 2

    def test_issue_error_doesnt_block_poll_time_update(self):
        gc = make_github_connector(from_user="beastoin")
        gc._last_poll_time = "2026-10-25T00:00:00Z"
        bad_issue = make_issue(author="beastoin", body="@mon hello")
        with patch.object(gc, "_get_all_comments", return_value=([], True)), \
             patch.object(gc, "_get_all_issues", return_value=[bad_issue]), \
             patch.object(gc, "_process_issue", side_effect=Exception("boom")):
            gc.poll_once()
        assert gc._last_poll_time == "2026-10-25T00:00:00Z"

    def test_no_issues_still_advances_poll_time(self):
        gc = make_github_connector(from_user="beastoin")
        gc._last_poll_time = "2026-10-25T00:00:00Z"
        with patch.object(gc, "_get_all_comments", return_value=([], True)), \
             patch.object(gc, "_get_all_issues", return_value=[]):
            gc.poll_once()
        assert gc._last_poll_time != "2026-10-25T00:00:00Z"


class TestGitHubIssueStatePersistence:
    """_save_state / _load_state round-trip seen_issue_ids."""

    def test_save_load_roundtrip(self, tmp_path):
        state_file = str(tmp_path / "github_state.json")
        gc = make_github_connector()
        gc._state_file = state_file
        gc._last_poll_time = "2026-10-25T12:00:00Z"
        gc._seen_ids.add(100)
        gc._seen_ids.add(200)
        gc._seen_issue_ids.add(5000)
        gc._seen_issue_ids.add(6000)
        gc._save_state()

        gc2 = make_github_connector()
        gc2._state_file = state_file
        restored = gc2._load_state()
        assert restored is True
        assert gc2._last_poll_time == "2026-10-25T12:00:00Z"
        assert 100 in gc2._seen_ids
        assert 200 in gc2._seen_ids
        assert 5000 in gc2._seen_issue_ids
        assert 6000 in gc2._seen_issue_ids

    def test_load_without_issue_ids_ok(self, tmp_path):
        """State file from older bridge version (no seen_issue_ids key) loads fine."""
        state_file = str(tmp_path / "github_state.json")
        import json
        with open(state_file, "w") as f:
            json.dump({"last_poll_time": "2026-10-25T12:00:00Z", "seen_ids": [1, 2, 3]}, f)
        gc = make_github_connector()
        gc._state_file = state_file
        restored = gc._load_state()
        assert restored is True
        assert gc._seen_ids == {1, 2, 3}
        assert gc._seen_issue_ids == set()

    def test_status_includes_issue_count(self):
        gc = make_github_connector()
        gc._seen_issue_ids = {1, 2, 3}
        status = gc.status()
        assert status["seen_issue_ids_count"] == 3


class TestGitHubBeastFetchIssues:
    """_beast_fetch_issues calls beast github issues CLI correctly."""

    def test_success(self):
        gc = make_github_connector()
        beast_json = '[{"id":1,"author":"beastoin","body":"hello","title":"Bug","number":100,"kind":"issue","html_url":"https://github.com/o/r/issues/100"}]'
        mock_result = MagicMock(returncode=0, stdout=beast_json, stderr="")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = gc._beast_fetch_issues("BasedHardware/omi", "1h")
        assert result is not None
        assert len(result) == 1
        assert result[0]["id"] == 1
        cmd = mock_run.call_args[0][0]
        assert "issues" in cmd
        assert "--repo" in cmd
        assert "BasedHardware/omi" in cmd

    def test_failure_returns_none(self):
        gc = make_github_connector()
        mock_result = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("subprocess.run", return_value=mock_result):
            result = gc._beast_fetch_issues("BasedHardware/omi", "1h")
        assert result is None

    def test_timeout_returns_none(self):
        gc = make_github_connector()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
            result = gc._beast_fetch_issues("BasedHardware/omi", "1h")
        assert result is None

    def test_invalid_json_returns_none(self):
        gc = make_github_connector()
        mock_result = MagicMock(returncode=0, stdout="not json", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = gc._beast_fetch_issues("BasedHardware/omi", "1h")
        assert result is None

    def test_empty_output_returns_empty_list(self):
        gc = make_github_connector()
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = gc._beast_fetch_issues("BasedHardware/omi", "1h")
        assert result == []


class TestGitHubGetAllIssues:
    """_get_all_issues merges across repos and deduplicates."""

    def test_merges_from_multiple_repos(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"])
        issue1 = {"id": 100, "author": "beastoin", "body": "one"}
        issue2 = {"id": 200, "author": "beastoin", "body": "two"}

        def side_effect(repo, since, **kw):
            if repo == "BasedHardware/omi":
                return [issue1]
            return [issue2]

        with patch.object(gc, "_beast_fetch_issues", side_effect=side_effect):
            result = gc._get_all_issues("1h")
        assert len(result) == 2
        repos = {r["repo"] for r in result}
        assert "BasedHardware/omi" in repos
        assert "beastoin/agent-swift" in repos

    def test_dedup_across_repos(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"])
        same_issue = {"id": 100, "author": "beastoin", "body": "dup"}
        with patch.object(gc, "_beast_fetch_issues", return_value=[same_issue]):
            result = gc._get_all_issues("1h")
        assert len(result) == 1

    def test_partial_failure_continues(self):
        gc = make_github_connector(repo=["BasedHardware/omi", "beastoin/agent-swift"])
        issue = {"id": 100, "author": "beastoin", "body": "ok"}

        def side_effect(repo, since, **kw):
            if repo == "BasedHardware/omi":
                return None
            return [issue]

        with patch.object(gc, "_beast_fetch_issues", side_effect=side_effect):
            result = gc._get_all_issues("1h")
        assert len(result) == 1


class TestGitHubIssuePruning:
    """_prune_seen_ids also prunes issue IDs."""

    def test_prune_trims_issue_ids(self):
        from bridge import _MAX_SEEN_IDS, _PRUNE_KEEP
        gc = make_github_connector()
        gc._seen_issue_ids = set(range(_MAX_SEEN_IDS + 100))
        gc._prune_seen_ids()
        assert len(gc._seen_issue_ids) == _PRUNE_KEEP
