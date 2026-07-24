"""Unit tests for gmail_connector.py — TDD inner loop."""
import base64
import json
import pytest
from unittest.mock import patch, MagicMock
from gmail_connector import GmailConnector


def make_connector(**overrides):
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

class TestConstruct:
    def test_import_and_construct(self):
        gc = make_connector()
        assert gc.gws_bin == "/usr/bin/gws"
        assert gc.sender_filter == "manager@example.com"
        assert gc.poll_interval == 30
        assert gc._history_id is None

    def test_from_filter_lowered(self):
        gc = make_connector(from_filter="Manager@Example.COM")
        assert gc.sender_filter == "manager@example.com"


# --- Increment 2: _run_gws subprocess wrapper ---

class TestRunGws:
    def test_success(self):
        gc = make_connector()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"historyId": "123"}'
        with patch("gmail_connector.subprocess.run", return_value=mock_result) as mock_run:
            result = gc._run_gws("messages", "list", "--params", '{"userId":"me"}')
        assert result == {"historyId": "123"}
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/gws"
        assert "gmail" in cmd
        assert "messages" in cmd

    def test_failure_returns_none(self):
        gc = make_connector()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "auth error"
        with patch("gmail_connector.subprocess.run", return_value=mock_result):
            assert gc._run_gws("messages", "list") is None

    def test_timeout_returns_none(self):
        gc = make_connector()
        import subprocess
        with patch("gmail_connector.subprocess.run", side_effect=subprocess.TimeoutExpired("gws", 30)):
            assert gc._run_gws("messages", "list") is None

    def test_json_body_passed(self):
        gc = make_connector()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"id": "m1"}'
        with patch("gmail_connector.subprocess.run", return_value=mock_result) as mock_run:
            gc._run_gws("messages", "modify", "--params", '{}', json_body='{"removeLabelIds":["UNREAD"]}')
        cmd = mock_run.call_args[0][0]
        assert "--json" in cmd


# --- Increment 3: Bootstrap historyId ---

class TestBootstrap:
    def test_bootstrap_history_id(self):
        gc = make_connector()
        with patch.object(gc, "_load_history_id", return_value=None), \
             patch.object(gc, "_save_history_id"), \
             patch.object(gc, "_run_gws", return_value={"historyId": "202400"}):
            assert gc._bootstrap_history_id() == "202400"

    def test_bootstrap_failure(self):
        gc = make_connector()
        with patch.object(gc, "_load_history_id", return_value=None), \
             patch.object(gc, "_run_gws", return_value=None):
            assert gc._bootstrap_history_id() is None

    def test_bootstrap_missing_key(self):
        gc = make_connector()
        with patch.object(gc, "_load_history_id", return_value=None), \
             patch.object(gc, "_run_gws", return_value={"emailAddress": "a@b.com"}):
            assert gc._bootstrap_history_id() is None

    def test_bootstrap_resumes_from_disk(self):
        gc = make_connector()
        with patch.object(gc, "_load_history_id", return_value="199999"):
            assert gc._bootstrap_history_id() == "199999"

    def test_bootstrap_skip_disk(self):
        gc = make_connector()
        with patch.object(gc, "_load_history_id", return_value="199999"), \
             patch.object(gc, "_save_history_id"), \
             patch.object(gc, "_run_gws", return_value={"historyId": "202500"}):
            assert gc._bootstrap_history_id(skip_disk=True) == "202500"


# --- Increment 4: Extract sender ---

class TestExtractSender:
    def test_angle_brackets(self):
        gc = make_connector()
        msg = make_message(from_addr="boss@example.com")
        assert gc.extract_sender(msg) == "boss@example.com"

    def test_bare_email(self):
        gc = make_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": "boss@example.com"}]}}
        assert gc.extract_sender(msg) == "boss@example.com"

    def test_missing_from(self):
        gc = make_connector()
        msg = {"payload": {"headers": [{"name": "To", "value": "me@x.com"}]}}
        assert gc.extract_sender(msg) == ""

    def test_case_insensitive(self):
        gc = make_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": "Boss@Example.COM"}]}}
        assert gc.extract_sender(msg) == "boss@example.com"


# --- Increment 5: Extract body text ---

class TestExtractBody:
    def test_plain_single_part(self):
        gc = make_connector()
        msg = make_message(body_text="hello world")
        assert gc.extract_body_text(msg) == "hello world"

    def test_multipart_returns_plain(self):
        gc = make_connector()
        msg = make_multipart_message(plain_text="plain version", html_text="<b>html</b>")
        assert gc.extract_body_text(msg) == "plain version"

    def test_empty_body(self):
        gc = make_connector()
        msg = {"payload": {"mimeType": "text/plain", "body": {}}}
        assert gc.extract_body_text(msg) == ""

    def test_no_parts(self):
        gc = make_connector()
        msg = {"payload": {"mimeType": "multipart/alternative"}}
        assert gc.extract_body_text(msg) == ""


# --- Increment 6: Extract subject ---

class TestExtractSubject:
    def test_subject_present(self):
        gc = make_connector()
        msg = make_message(subject="Fwd: GCP Alert")
        assert gc.extract_subject(msg) == "Fwd: GCP Alert"

    def test_subject_missing(self):
        gc = make_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": "a@b.com"}]}}
        assert gc.extract_subject(msg) == ""


# --- Increment 6b: Extract message-id and sender name ---

class TestExtractMessageId:
    def test_message_id_present(self):
        gc = make_connector()
        msg = make_message(message_id="<abc123@mail.gmail.com>")
        assert gc.extract_message_id(msg) == "<abc123@mail.gmail.com>"

    def test_message_id_missing(self):
        gc = make_connector()
        msg = make_message()
        assert gc.extract_message_id(msg) == ""


class TestExtractSenderName:
    def test_name_with_angle_brackets(self):
        gc = make_connector()
        msg = make_message(from_addr="alice@example.com")
        assert gc.extract_sender_name(msg) == "Manager"

    def test_quoted_name(self):
        gc = make_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": '"Alice B" <alice@x.com>'}]}}
        assert gc.extract_sender_name(msg) == "Alice B"

    def test_bare_email(self):
        gc = make_connector()
        msg = {"payload": {"headers": [{"name": "From", "value": "alice@x.com"}]}}
        assert gc.extract_sender_name(msg) == "alice"

    def test_no_from(self):
        gc = make_connector()
        msg = {"payload": {"headers": []}}
        assert gc.extract_sender_name(msg) == ""


class TestIsSentMessage:
    def test_sent_label(self):
        gc = make_connector()
        msg = make_message(labels=["SENT"])
        assert gc.is_sent_message(msg) is True

    def test_inbox_label(self):
        gc = make_connector()
        msg = make_message(labels=["INBOX", "UNREAD"])
        assert gc.is_sent_message(msg) is False


# --- Increment 7: Sender/label filters ---

class TestFilters:
    def test_allowed_sender_match(self):
        gc = make_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com")
        assert gc.is_allowed_sender(msg) is True

    def test_allowed_sender_no_match(self):
        gc = make_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="spam@evil.com")
        assert gc.is_allowed_sender(msg) is False

    def test_inbox_unread_true(self):
        gc = make_connector()
        assert gc.is_inbox_unread(["INBOX", "UNREAD", "CATEGORY_PERSONAL"]) is True

    def test_inbox_unread_missing_unread(self):
        gc = make_connector()
        assert gc.is_inbox_unread(["INBOX", "CATEGORY_PERSONAL"]) is False

    def test_inbox_unread_missing_inbox(self):
        gc = make_connector()
        assert gc.is_inbox_unread(["UNREAD"]) is False


# --- Increment 8: Parse @mentions ---

class TestParseMentions:
    def test_single_mention(self):
        gc = make_connector(get_registered_workers=lambda: {"mon", "taro"})
        targets, cleaned = gc.parse_mentions("@mon check the logs")
        assert targets == ["mon"]
        assert "check the logs" in cleaned
        assert "@mon" not in cleaned

    def test_multi_mention(self):
        gc = make_connector(get_registered_workers=lambda: {"mon", "taro"})
        targets, cleaned = gc.parse_mentions("@mon @taro check this")
        assert set(targets) == {"mon", "taro"}
        assert "@mon" not in cleaned
        assert "@taro" not in cleaned

    def test_unregistered_mention(self):
        gc = make_connector(get_registered_workers=lambda: {"mon", "taro"})
        targets, cleaned = gc.parse_mentions("@unknown hello")
        assert targets == []
        assert cleaned == "@unknown hello"

    def test_no_mentions(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("just a plain message")
        assert targets == []
        assert cleaned == "just a plain message"

    def test_empty_text(self):
        gc = make_connector()
        targets, cleaned = gc.parse_mentions("")
        assert targets == []
        assert cleaned == ""

    def test_dedup_mentions(self):
        gc = make_connector(get_registered_workers=lambda: {"mon"})
        targets, _ = gc.parse_mentions("@mon hey @mon again")
        assert targets == ["mon"]

    def test_mixed_registered_unregistered(self):
        gc = make_connector(get_registered_workers=lambda: {"mon"})
        targets, cleaned = gc.parse_mentions("@mon @nobody check")
        assert targets == ["mon"]
        assert "@nobody" in cleaned


# --- Increment 9: Format email message ---

class TestExtractAttachments:
    def test_no_attachments(self):
        gc = make_connector()
        msg = make_message()
        assert gc.extract_attachments(msg) == []

    def test_single_attachment(self):
        gc = make_connector()
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
        gc = make_connector()
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


class TestFormatAttachmentLine:
    def test_no_attachments(self):
        gc = make_connector()
        assert gc._format_attachment_line([]) == ""

    def test_with_attachments(self):
        gc = make_connector()
        atts = [{"filename": "pricing.pdf"}, {"filename": "chart.png"}]
        line = gc._format_attachment_line(atts)
        assert "2 attachment(s)" in line
        assert "pricing.pdf" in line
        assert "chart.png" in line
        assert "attachment(s)" in line


class TestCleanBody:
    def test_strips_urls(self):
        gc = make_connector()
        assert gc._clean_body("click here<https://example.com> now") == "click here now"

    def test_strips_image_markers(self):
        gc = make_connector()
        assert gc._clean_body("[image: Google Cloud] VIEW") == "VIEW"

    def test_strips_outlook_footer(self):
        gc = make_connector()
        assert gc._clean_body("hello\n\nGet Outlook for iOS\nmore") == "hello\n\nmore"

    def test_normalizes_crlf(self):
        gc = make_connector()
        assert gc._clean_body("a\r\nb\r\n\r\n\r\n\r\nc") == "a\nb\n\nc"

    def test_collapses_blank_lines(self):
        gc = make_connector()
        assert gc._clean_body("a\n\n\n\n\nb") == "a\n\nb"


class TestFormatMessage:
    def test_plain_email(self):
        gc = make_connector()
        html, plain = gc.format_email_message("check the deploy", "Deploy Alert")
        assert "manager (via email):" in plain
        assert "check the deploy" in plain
        assert "<b>Deploy Alert</b>" in html

    def test_plain_email_with_thread(self):
        gc = make_connector()
        html, plain = gc.format_email_message("check the deploy", "Deploy Alert", "abc123")
        assert "[thread:abc123]" in plain
        assert "check the deploy" in plain
        assert "[thread:abc123]" in html
        assert "<b>Deploy Alert</b>" in html

    def test_forwarded_gmail(self):
        gc = make_connector()
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
        gc = make_connector()
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
        gc = make_connector()
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
        gc = make_connector()
        body = (
            "---------- Forwarded message ---------\n"
            "From: alerts@google.com\n"
            "Alert content here"
        )
        html, plain = gc.format_email_message(body, "Alert")
        assert "manager (via email):" in plain
        assert "--- Forwarded: Alert ---" in plain

    def test_whitespace_trimmed(self):
        gc = make_connector()
        html, plain = gc.format_email_message("  hello world  ", "Test")
        assert "hello world" in plain
        assert "hello world" in html

    def test_real_outlook_forward(self):
        gc = make_connector()
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
        gc = make_connector()
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
        gc = make_connector()
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
        gc = make_connector()
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
        gc = make_connector()
        html, plain = gc.format_email_message("a < b & c > d", "Test <script>")
        assert "&lt;" in html
        assert "&amp;" in html
        assert "&gt;" in html
        assert "<script>" not in html

    def test_long_body_truncated(self):
        gc = make_connector()
        long_body = "line\n" * 500
        html, plain = gc.format_email_message(long_body, "Long")
        assert "truncated" in plain
        assert len(plain) < 2000


class TestFormatSentReply:
    def test_basic_sent_reply(self):
        gc = make_connector()
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
        gc = make_connector()
        body = "My reply\n\nOn Mon, May 26, 2026 Someone wrote:\n> original text"
        msg = make_message(from_addr="beastoin@gmail.com", labels=["SENT"])
        html, plain = gc._format_sent_reply(body, "Re: Test", "t2", msg)
        assert "My reply" in html
        assert "original text" not in html


# --- Increment 10: Get new message IDs ---

class TestGetNewMessageIds:
    def test_returns_new_ids(self):
        gc = make_connector()
        gc._history_id = "100"
        history_data = {
            "history": [
                {
                    "id": "101",
                    "messagesAdded": [
                        {"message": {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"]}}
                    ],
                },
                {
                    "id": "102",
                    "messagesAdded": [
                        {"message": {"id": "m2", "threadId": "t2", "labelIds": ["INBOX", "UNREAD"]}}
                    ],
                },
            ],
            "historyId": "103",
        }
        with patch.object(gc, "_run_gws", return_value=history_data):
            ids = gc._get_new_message_ids()
        assert ids == ["m1", "m2"]
        assert gc._history_id == "103"

    def test_empty_history(self):
        gc = make_connector()
        gc._history_id = "100"
        with patch.object(gc, "_run_gws", return_value={"historyId": "100"}):
            ids = gc._get_new_message_ids()
        assert ids == []

    def test_filters_non_inbox(self):
        gc = make_connector()
        gc._history_id = "100"
        history_data = {
            "history": [
                {
                    "id": "101",
                    "messagesAdded": [
                        {"message": {"id": "m1", "labelIds": ["SPAM", "UNREAD"]}},
                        {"message": {"id": "m2", "labelIds": ["INBOX", "UNREAD"]}},
                    ],
                }
            ],
            "historyId": "102",
        }
        with patch.object(gc, "_run_gws", return_value=history_data):
            ids = gc._get_new_message_ids()
        assert ids == ["m2"]

    def test_picks_up_sent_messages(self):
        gc = make_connector()
        gc._history_id = "100"
        history_data = {
            "history": [
                {
                    "id": "101",
                    "messagesAdded": [
                        {"message": {"id": "m1", "labelIds": ["SENT"]}},
                        {"message": {"id": "m2", "labelIds": ["INBOX", "UNREAD"]}},
                        {"message": {"id": "m3", "labelIds": ["SPAM"]}},
                    ],
                }
            ],
            "historyId": "102",
        }
        with patch.object(gc, "_run_gws", return_value=history_data):
            ids = gc._get_new_message_ids()
        assert ids == ["m1", "m2"]

    def test_no_history_id_returns_empty(self):
        gc = make_connector()
        gc._history_id = None
        assert gc._get_new_message_ids() == []

    def test_api_failure_returns_none(self):
        gc = make_connector()
        gc._history_id = "100"
        with patch.object(gc, "_run_gws", return_value=None):
            assert gc._get_new_message_ids() is None

    def test_dedup_message_ids(self):
        gc = make_connector()
        gc._history_id = "100"
        history_data = {
            "history": [
                {"id": "101", "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX", "UNREAD"]}}]},
                {"id": "102", "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX", "UNREAD"]}}]},
            ],
            "historyId": "103",
        }
        with patch.object(gc, "_run_gws", return_value=history_data):
            ids = gc._get_new_message_ids()
        assert ids == ["m1"]


# --- Increment 11: Mark as read ---

class TestMarkAsRead:
    def test_success(self):
        gc = make_connector()
        with patch.object(gc, "_run_gws", return_value={"id": "m1", "labelIds": ["INBOX"]}) as mock:
            assert gc.mark_as_read("m1") is True
        args = mock.call_args
        assert "modify" in args[0]
        assert '"removeLabelIds": ["UNREAD"]' in args[1]["json_body"]

    def test_failure(self):
        gc = make_connector()
        with patch.object(gc, "_run_gws", return_value=None):
            assert gc.mark_as_read("m1") is False


# --- Increment 12: Process single message ---

class TestProcessMessage:
    def test_with_mentions(self):
        gc = make_connector(
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
        gc = make_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="spam@evil.com", body_text="buy stuff")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read") as mock_mark:
            gc._process_message("m1")
        gc.on_message.assert_not_called()
        mock_mark.assert_not_called()

    def test_no_mentions_still_delivers(self):
        gc = make_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com", body_text="general update")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read"):
            gc._process_message("m1")
        gc.on_message.assert_called_once()
        targets, html_text, plain_text, attachments = gc.on_message.call_args[0]
        assert targets == []

    def test_empty_body_skipped(self):
        gc = make_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="manager@example.com", body_text="   ")
        with patch.object(gc, "get_message", return_value=msg), \
             patch.object(gc, "mark_as_read") as mock_mark:
            gc._process_message("m1")
        gc.on_message.assert_not_called()
        mock_mark.assert_not_called()

    def test_get_message_failure(self):
        gc = make_connector()
        with patch.object(gc, "get_message", return_value=None):
            gc._process_message("m1")
        gc.on_message.assert_not_called()

    def test_sent_reply_shows_in_telegram(self):
        gc = make_connector(from_filter="manager@example.com")
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
        gc = make_connector(from_filter="manager@example.com")
        msg = make_message(from_addr="beastoin@gmail.com", subject="Re: Deploy",
                           body_text="Deployed", labels=["SENT"])
        with patch.object(gc, "get_message", return_value=msg):
            gc._process_message("m1")
        gc.on_message.assert_called_once()

    def test_inbound_with_reply_hint(self):
        gc = make_connector(
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
        assert "--thread-id t1" in plain_text
        assert "--in-reply-to" in plain_text

    def test_inbound_no_reply_hint_without_mentions(self):
        gc = make_connector(from_filter="manager@example.com")
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


# --- Increment 13: Poll cycle ---

class TestPollOnce:
    def test_processes_multiple(self):
        gc = make_connector(from_filter="mgr@x.com", get_registered_workers=lambda: {"mon"})
        msg1 = make_message(from_addr="mgr@x.com", body_text="@mon task 1")
        msg2 = make_message(from_addr="mgr@x.com", body_text="@mon task 2")
        msg2["id"] = "m2"

        def get_msg(msg_id):
            return msg1 if msg_id == "m1" else msg2

        with patch.object(gc, "_get_new_message_ids", return_value=["m1", "m2"]), \
             patch.object(gc, "get_message", side_effect=get_msg), \
             patch.object(gc, "mark_as_read"):
            gc.poll_once()
        assert gc.on_message.call_count == 2

    def test_individual_failure_doesnt_block(self):
        gc = make_connector(from_filter="mgr@x.com", get_registered_workers=lambda: {"mon"})
        msg2 = make_message(from_addr="mgr@x.com", body_text="@mon task 2")

        call_count = [0]
        def get_msg(msg_id):
            call_count[0] += 1
            if msg_id == "m1":
                raise ValueError("parse error")
            return msg2

        with patch.object(gc, "_get_new_message_ids", return_value=["m1", "m2"]), \
             patch.object(gc, "get_message", side_effect=get_msg), \
             patch.object(gc, "mark_as_read"):
            gc.poll_once()
        assert gc.on_message.call_count == 1

    def test_empty_poll(self):
        gc = make_connector()
        with patch.object(gc, "_get_new_message_ids", return_value=[]):
            gc.poll_once()
        gc.on_message.assert_not_called()


# --- Increment 14a: Preflight check ---

class TestPreflightCheck:
    def test_binary_missing(self):
        gc = make_connector(gws_bin="/nonexistent/gws")
        ok, msg = gc.preflight_check()
        assert ok is False
        assert "not found" in msg

    def test_auth_failure(self, tmp_path):
        fake_bin = tmp_path / "gws"
        fake_bin.write_text("#!/bin/sh\nexit 1")
        fake_bin.chmod(0o755)
        gc = make_connector(gws_bin=str(fake_bin))
        with patch.object(gc, "_run_gws", return_value=None):
            ok, msg = gc.preflight_check()
        assert ok is False
        assert "auth failed" in msg

    def test_success(self, tmp_path):
        fake_bin = tmp_path / "gws"
        fake_bin.write_text("#!/bin/sh\necho '{}'")
        fake_bin.chmod(0o755)
        gc = make_connector(gws_bin=str(fake_bin))
        with patch.object(gc, "_run_gws", return_value={"emailAddress": "test@gmail.com", "historyId": "1"}):
            ok, msg = gc.preflight_check()
        assert ok is True
        assert "test@gmail.com" in msg


# --- Increment 14b: Consecutive failure recovery ---

class TestConsecutiveFailures:
    def test_failure_counter_increments(self):
        gc = make_connector()
        with patch.object(gc, "_get_new_message_ids", return_value=None):
            gc.poll_once()
            assert gc._consecutive_failures == 1
            gc.poll_once()
            assert gc._consecutive_failures == 2

    def test_success_resets_counter(self):
        gc = make_connector()
        gc._consecutive_failures = 4
        with patch.object(gc, "_get_new_message_ids", return_value=[]):
            gc.poll_once()
        assert gc._consecutive_failures == 0

    def test_rebootstrap_after_threshold(self):
        gc = make_connector()
        gc._consecutive_failures = 4
        with patch.object(gc, "_get_new_message_ids", return_value=None), \
             patch.object(gc, "_bootstrap_history_id", return_value="999") as mock_boot:
            gc.poll_once()
        mock_boot.assert_called_once()
        assert gc._history_id == "999"
        assert gc._consecutive_failures == 0

    def test_rebootstrap_failure_keeps_counting(self):
        gc = make_connector()
        gc._consecutive_failures = 4
        with patch.object(gc, "_get_new_message_ids", return_value=None), \
             patch.object(gc, "_bootstrap_history_id", return_value=None):
            gc.poll_once()
        assert gc._consecutive_failures == 5


# --- Increment 14c: Telegram alerts on failure ---

class TestAlerts:
    def test_alert_sent_on_warn_threshold(self):
        alert_mock = MagicMock()
        gc = make_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_new_message_ids", return_value=None):
            gc.poll_once()
        alert_mock.assert_called_once()
        assert "consecutive errors" in alert_mock.call_args[0][0]

    def test_alert_sent_only_once(self):
        alert_mock = MagicMock()
        gc = make_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_new_message_ids", return_value=None):
            gc.poll_once()  # triggers at 3
            gc.poll_once()  # 4 — no duplicate
        assert alert_mock.call_count == 1

    def test_recovery_clears_alert(self):
        alert_mock = MagicMock()
        gc = make_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 3
        gc._alert_sent = True
        with patch.object(gc, "_get_new_message_ids", return_value=[]):
            gc.poll_once()
        assert gc._alert_sent is False
        assert alert_mock.call_count == 1
        assert "Recovered" in alert_mock.call_args[0][0]

    def test_no_alert_without_callback(self):
        gc = make_connector()
        gc.on_alert = None
        gc._consecutive_failures = 2
        with patch.object(gc, "_get_new_message_ids", return_value=None):
            gc.poll_once()
        assert gc._consecutive_failures == 3

    def test_preflight_failure_alerts(self):
        alert_mock = MagicMock()
        gc = make_connector(gws_bin="/nonexistent/gws")
        gc.on_alert = alert_mock
        with patch.object(gc._stop_event, "wait", return_value=False):
            gc._poll_loop()
        alert_mock.assert_called_once()
        assert "disabled" in alert_mock.call_args[0][0].lower()

    def test_rebootstrap_success_sends_recovery(self):
        alert_mock = MagicMock()
        gc = make_connector()
        gc.on_alert = alert_mock
        gc._consecutive_failures = 4
        gc._alert_sent = True
        with patch.object(gc, "_get_new_message_ids", return_value=None), \
             patch.object(gc, "_bootstrap_history_id", return_value="999"):
            gc.poll_once()
        assert gc._consecutive_failures == 0
        assert "Recovered" in alert_mock.call_args[0][0]


# --- Increment 15: Thread lifecycle ---

class TestLifecycle:
    def test_start_creates_daemon_thread(self):
        gc = make_connector(poll_interval=1)
        with patch.object(gc, "preflight_check", return_value=(True, "OK")), \
             patch.object(gc, "_bootstrap_history_id", return_value="100"), \
             patch.object(gc, "poll_once"):
            thread = gc.start()
            import time
            time.sleep(0.1)
            assert thread.is_alive()
            assert thread.daemon is True
            assert thread.name == "gmail-poller"
            gc.stop()

    def test_stop_terminates(self):
        gc = make_connector(poll_interval=1)
        with patch.object(gc, "preflight_check", return_value=(True, "OK")), \
             patch.object(gc, "_bootstrap_history_id", return_value="100"), \
             patch.object(gc, "poll_once"):
            gc.start()
            import time
            time.sleep(0.1)
            gc.stop()
            time.sleep(0.2)
            assert not gc._thread.is_alive()

    def test_bootstrap_failure_retries(self):
        gc = make_connector(poll_interval=1)
        call_count = [0]
        def mock_bootstrap():
            call_count[0] += 1
            if call_count[0] == 1:
                return None
            return "200"
        wait_count = [0]
        def mock_wait(timeout=None):
            wait_count[0] += 1
            if wait_count[0] == 1:
                return False  # retry bootstrap
            gc._stop_event.set()  # stop the loop
            return True
        with patch.object(gc, "preflight_check", return_value=(True, "OK")), \
             patch.object(gc, "_bootstrap_history_id", side_effect=mock_bootstrap), \
             patch.object(gc, "poll_once"), \
             patch.object(gc._stop_event, "wait", side_effect=mock_wait):
            gc._poll_loop()
        assert call_count[0] == 2
        assert gc._history_id == "200"
