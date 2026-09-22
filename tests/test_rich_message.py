"""Tests for sendRichMessage integration in bridge.py.

Verifies:
1. Rich message is tried first, falls back to HTML on failure
2. Markdown passed directly (no HTML conversion) in rich path
3. 32K char limit used for rich, 4K for fallback
4. Name prefix formatting in rich vs HTML mode
5. Multi-chunk splitting respects rich limit
6. LocalTransport supports send_rich_text
"""
import os
import re
import pytest
from unittest.mock import patch, MagicMock, call


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


class TestRichMessageSend:
    """Test sendRichMessage path in send_response_to_telegram."""

    def test_rich_message_tried_first(self, monkeypatch):
        """When transport has send_rich_text, it's called before send_text."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "hello world", chat_id=123)

        mock_transport.send_rich_text.assert_called_once()
        mock_transport.send_text.assert_not_called()

    def test_rich_message_contains_raw_markdown(self, monkeypatch):
        """Rich path sends raw markdown, not HTML-converted text."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "**bold** and `code`", chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[1].get("markdown") or mock_transport.send_rich_text.call_args[0][1]
        assert "**bold**" in sent_md
        assert "`code`" in sent_md
        assert "<b>" not in sent_md

    def test_rich_message_has_name_prefix(self, monkeypatch):
        """Rich path prefixes message with bold worker name."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "test message", chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert sent_md.startswith("**lee:**")

    def test_rich_message_strips_duplicate_name_prefix(self, monkeypatch):
        """When text already starts with 'lee:', don't double-prefix."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "lee: test message", chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert "**lee:**\ntest message" == sent_md
        assert "lee: lee:" not in sent_md

    def test_fallback_to_html_on_rich_failure(self, monkeypatch):
        """When sendRichMessage returns 400, falls back to sendMessage+HTML."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: method not found"}
        mock_transport.send_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "test fallback", chat_id=123)

        mock_transport.send_rich_text.assert_called_once()
        mock_transport.send_text.assert_called_once()
        html_text = mock_transport.send_text.call_args[0][1]
        assert "<b>lee:</b>" in html_text

    def test_fallback_on_rich_none_response(self, monkeypatch):
        """When sendRichMessage returns None (network error), falls back."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = None
        mock_transport.send_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "test fallback", chat_id=123)

        mock_transport.send_rich_text.assert_called_once()
        mock_transport.send_text.assert_called_once()

    def test_rich_message_preserves_headings(self, monkeypatch):
        """Headings in markdown are passed through (not converted to bold)."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        text = "# Deploy Report\n\n## Status\nAll green."
        bridge.send_response_to_telegram("lee", text, chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert "# Deploy Report" in sent_md
        assert "## Status" in sent_md

    def test_rich_message_converts_pipe_tables_to_html(self, monkeypatch):
        """Pipe tables are converted to HTML <table> for sendRichMessage."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        text = "| Service | Status |\n|---------|--------|\n| API | OK |\n| DB | OK |"
        bridge.send_response_to_telegram("lee", text, chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert "<table>" in sent_md
        assert "<th>Service</th>" in sent_md
        assert "<th>Status</th>" in sent_md
        assert "<td>API</td>" in sent_md
        assert "<td>DB</td>" in sent_md
        assert "</table>" in sent_md
        assert "| Service |" not in sent_md

    def test_rich_message_preserves_details(self, monkeypatch):
        """Collapsible <details> blocks pass through."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        text = "<details>\n<summary>Full logs</summary>\n\nPod started OK.\n</details>"
        bridge.send_response_to_telegram("lee", text, chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert "<details>" in sent_md
        assert "<summary>Full logs</summary>" in sent_md


class TestRichMessageChunking:
    """Test message splitting with 32K rich limit vs 4K HTML limit."""

    def test_short_message_single_chunk(self, monkeypatch):
        """Short messages are sent as a single rich message."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "short msg", chat_id=123)

        assert mock_transport.send_rich_text.call_count == 1

    def test_long_message_fewer_chunks_with_rich(self, monkeypatch):
        """A 10K message needs 3 HTML chunks but only 1 rich chunk."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        text = "word " * 2000  # ~10K chars
        bridge.send_response_to_telegram("lee", text, chat_id=123)

        # With 32K limit, 10K text fits in 1 chunk
        assert mock_transport.send_rich_text.call_count == 1

    def test_very_long_message_splits_at_32k(self, monkeypatch):
        """Messages over 32K are split into multiple rich chunks."""
        import bridge

        call_count = 0

        def mock_rich(chat_id, markdown, reply_to=None):
            nonlocal call_count
            call_count += 1
            return {"ok": True, "result": {"message_id": call_count}}

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.side_effect = mock_rich
        monkeypatch.setattr(bridge, "transport", mock_transport)

        text = "x" * 40000  # Over 32K
        bridge.send_response_to_telegram("lee", text, chat_id=123)

        assert mock_transport.send_rich_text.call_count >= 2

    def test_reply_chaining_on_multi_chunk(self, monkeypatch):
        """Multi-chunk rich messages chain via reply_to."""
        import bridge

        msg_id = [0]

        def mock_rich(chat_id, markdown, reply_to=None):
            msg_id[0] += 1
            return {"ok": True, "result": {"message_id": msg_id[0]}}

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.side_effect = mock_rich
        monkeypatch.setattr(bridge, "transport", mock_transport)

        text = "x" * 40000
        bridge.send_response_to_telegram("lee", text, chat_id=123)

        calls = mock_transport.send_rich_text.call_args_list
        assert len(calls) >= 2
        # First chunk: no reply_to
        assert calls[0][1].get("reply_to") is None
        # Second chunk: replies to first
        assert calls[1][1].get("reply_to") == 1


class TestRichMessageEdgeCases:
    """Edge cases for the rich message path."""

    def test_empty_text_no_send(self, monkeypatch):
        """Empty text after tag parsing sends nothing."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "", chat_id=123)

        mock_transport.send_rich_text.assert_not_called()
        mock_transport.send_text.assert_not_called()

    def test_image_tags_extracted_before_rich_send(self, monkeypatch, tmp_path):
        """[[image:...]] tags are parsed out before the text goes to rich send."""
        import bridge

        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)
        monkeypatch.setattr(bridge, "send_photo", lambda *a, **kw: True)

        bridge.send_response_to_telegram("lee", f"hello [[image:{img}|cap]]", chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert "[[image:" not in sent_md
        assert "hello" in sent_md

    def test_fallback_html_still_works_after_rich_fail(self, monkeypatch):
        """After rich fails, HTML fallback uses markdown_to_telegram_html correctly."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": False, "error_code": 400, "description": "not found"}
        mock_transport.send_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)

        bridge.send_response_to_telegram("lee", "**bold text**", chat_id=123)

        html_text = mock_transport.send_text.call_args[0][1]
        assert "<b>" in html_text
        assert "bold text" in html_text

    def test_speak_tag_extracted_before_rich_send(self, monkeypatch):
        """[[speak:...]] tags are removed before sending to Telegram."""
        import bridge

        mock_transport = MagicMock()
        mock_transport.name = "telegram"
        mock_transport.send_rich_text.return_value = {"ok": True, "result": {"message_id": 42}}
        monkeypatch.setattr(bridge, "transport", mock_transport)
        monkeypatch.setattr(bridge, "TTS_ENDPOINT", "")

        bridge.send_response_to_telegram("lee", "hello [[speak:custom voice]]", chat_id=123)

        sent_md = mock_transport.send_rich_text.call_args[0][1]
        assert "[[speak:" not in sent_md
        assert "hello" in sent_md


class TestLocalTransportRich:
    """Verify LocalTransport supports rich message method."""

    def test_local_transport_has_send_rich_text(self):
        import bridge
        t = bridge.LocalTransport()
        assert hasattr(t, "send_rich_text")

    def test_local_transport_send_rich_text_returns_ok(self):
        import bridge
        t = bridge.LocalTransport()
        result = t.send_rich_text(123, "**test**")
        assert result["ok"] is True
        assert "message_id" in result["result"]


class TestTelegramAPIRich:
    """Verify TelegramAPI.send_rich_message constructs correct payload."""

    def test_send_rich_message_payload(self, monkeypatch):
        """send_rich_message sends correct JSON to sendRichMessage endpoint."""
        import bridge

        captured = {}

        def mock_api(method, data):
            captured["method"] = method
            captured["data"] = data
            return {"ok": True, "result": {"message_id": 1}}

        api = bridge.TelegramAPI("fake:token")
        monkeypatch.setattr(api, "api", mock_api)

        api.send_rich_message(123, "# Hello\nWorld")

        assert captured["method"] == "sendRichMessage"
        assert captured["data"]["chat_id"] == 123
        assert captured["data"]["rich_message"] == {"markdown": "# Hello\nWorld"}

    def test_send_rich_message_with_reply(self, monkeypatch):
        """send_rich_message passes reply_to_message_id."""
        import bridge

        captured = {}

        def mock_api(method, data):
            captured["data"] = data
            return {"ok": True, "result": {"message_id": 1}}

        api = bridge.TelegramAPI("fake:token")
        monkeypatch.setattr(api, "api", mock_api)

        api.send_rich_message(123, "test", reply_to_message_id=42)

        assert captured["data"]["reply_to_message_id"] == 42


class TestPipeTableToHtml:
    """Test _pipe_tables_to_html conversion."""

    def test_basic_table(self):
        import bridge
        text = "| Check | Status |\n|-------|--------|\n| API | OK |"
        result = bridge._pipe_tables_to_html(text)
        assert "<table>" in result
        assert "<th>Check</th>" in result
        assert "<td>API</td>" in result
        assert "<td>OK</td>" in result
        assert "</table>" in result

    def test_multi_row_table(self):
        import bridge
        text = "| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        result = bridge._pipe_tables_to_html(text)
        assert result.count("<tr>") == 3  # 1 header + 2 data rows
        assert "<td>1</td>" in result
        assert "<td>4</td>" in result

    def test_text_around_table_preserved(self):
        import bridge
        text = "Before text\n\n| X | Y |\n|---|---|\n| a | b |\n\nAfter text"
        result = bridge._pipe_tables_to_html(text)
        assert "Before text" in result
        assert "After text" in result
        assert "<table>" in result

    def test_no_table_passthrough(self):
        import bridge
        text = "Just regular text\nNo tables here"
        result = bridge._pipe_tables_to_html(text)
        assert result == text

    def test_alignment(self):
        import bridge
        text = "| L | C | R |\n|:---|:---:|---:|\n| a | b | c |"
        result = bridge._pipe_tables_to_html(text)
        assert 'style="text-align:center"' in result
        assert 'style="text-align:right"' in result

    def test_table_inside_code_block_not_converted(self):
        import bridge
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        result = bridge._pipe_tables_to_html(text)
        assert "<table>" not in result
        assert "| A | B |" in result

    def test_table_after_code_block_converted(self):
        import bridge
        text = "```\nsome code\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        result = bridge._pipe_tables_to_html(text)
        assert "<table>" in result
        assert "<td>1</td>" in result

    def test_html_chars_escaped_in_cells(self):
        import bridge
        text = "| Name | Value |\n|---|---|\n| x < y | a & b |"
        result = bridge._pipe_tables_to_html(text)
        assert "&lt;" in result
        assert "&amp;" in result
        assert "<td>x &lt; y</td>" in result
