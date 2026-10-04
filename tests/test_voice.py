"""Tests for voice transcription (STT)."""
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


def test_transcribe_voice_success():
    import bridge

    # Create a temp file to simulate audio
    tmp = tempfile.NamedTemporaryFile(suffix='.ogg', delete=False)
    tmp.write(b'fake audio data')
    tmp.close()

    # Mock urllib to return a successful transcription
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({'text': 'hello world', 'audio_duration_s': 2.5}).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    try:
        with patch('bridge._urlopen', return_value=mock_response):
            result = bridge.transcribe_voice(tmp.name)
        assert result == 'hello world', f'Expected "hello world", got {result!r}'
    finally:
        os.unlink(tmp.name)


def test_transcribe_voice_timeout_returns_none():
    import bridge

    # Simulate timeout
    with patch('bridge._urlopen', side_effect=TimeoutError('timeout')):
        result = bridge.transcribe_voice('/tmp/test.ogg')

    assert result is None, f'Expected None, got {result!r}'


def test_transcribe_voice_bad_json_returns_none():
    import bridge

    mock_response = MagicMock()
    mock_response.read.return_value = b'not json'
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch('bridge._urlopen', return_value=mock_response):
        result = bridge.transcribe_voice('/tmp/test.ogg')

    assert result is None, f'Expected None, got {result!r}'


# ── Voice transcript confirmation tests ────────────────────────────


def test_pending_voice_store_and_pop():
    """Store a pending voice transcript and pop it back."""
    import bridge

    bridge._pending_voice.clear()
    bridge._store_pending_voice(42, "hello world", "alice", 123, "")
    entry = bridge._pop_pending_voice(42)
    assert entry is not None
    assert entry["transcript"] == "hello world"
    assert entry["target_worker"] == "alice"
    assert entry["chat_id"] == 123
    # Should be gone after pop
    assert bridge._pop_pending_voice(42) is None


def test_pending_voice_expires():
    """Pending voice transcripts expire after TTL."""
    import bridge

    bridge._pending_voice.clear()
    # Store with a timestamp far in the past
    bridge._pending_voice[99] = {
        "transcript": "old",
        "target_worker": "bob",
        "chat_id": 123,
        "caption": "",
        "created": bridge._clock.time() - bridge._VOICE_CONFIRM_TTL - 1,
    }
    assert bridge._pop_pending_voice(99) is None


def test_voice_preview_sends_buttons():
    """_send_voice_preview sends transcript with inline keyboard."""
    import bridge

    bridge._pending_voice.clear()

    def mock_api(method: str, data: dict) -> dict:
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 777}}
        return {"ok": True, "result": {}}

    with patch.object(bridge, "telegram_api", side_effect=mock_api):
        router = bridge.CommandRouter.__new__(bridge.CommandRouter)
        router._send_voice_preview(123, "test transcript", "alice", "")

    # Should have stored the pending transcript
    assert 777 in bridge._pending_voice
    assert bridge._pending_voice[777]["transcript"] == "test transcript"


def test_callback_voice_send_routes_to_worker():
    """Pressing ✅ Send routes the transcript to the focused worker."""
    import bridge

    bridge._pending_voice.clear()
    bridge._store_pending_voice(100, "go fix the bug", "alice", 123, "")

    routed: list[tuple[str, str]] = []

    def mock_api(method: str, data: dict) -> dict:
        return {"ok": True, "result": {}}

    class FakeRouter(bridge.CommandRouter):
        def __init__(self) -> None:
            pass
        def route_message(self, name: str, text: str, chat_id, msg_id, one_off: bool = False) -> None:
            routed.append((name, text))

    router = FakeRouter()

    callback: bridge.TelegramCallbackQuery = {
        "id": "cb1",
        "from": {"id": 123, "is_bot": False, "first_name": "Test"},
        "message": {"message_id": 100, "date": 0, "chat": {"id": 123, "type": "private"}},
        "data": "voice_send",
    }

    with patch.object(bridge, "telegram_api", side_effect=mock_api):
        router.handle_callback_query(callback)

    assert len(routed) == 1
    assert routed[0] == ("alice", "go fix the bug")


def test_callback_voice_drop():
    """Pressing ❌ removes the keyboard and marks as dropped."""
    import bridge

    bridge._pending_voice.clear()
    bridge._store_pending_voice(200, "never mind", "bob", 123, "")

    api_calls: list[tuple[str, dict]] = []

    def mock_api(method: str, data: dict) -> dict:
        api_calls.append((method, dict(data)))
        return {"ok": True, "result": {}}

    router = bridge.CommandRouter.__new__(bridge.CommandRouter)

    callback: bridge.TelegramCallbackQuery = {
        "id": "cb2",
        "from": {"id": 123, "is_bot": False, "first_name": "Test"},
        "message": {"message_id": 200, "date": 0, "chat": {"id": 123, "type": "private"}},
        "data": "voice_drop",
    }

    with patch.object(bridge, "telegram_api", side_effect=mock_api):
        router.handle_callback_query(callback)

    edit_calls = [c for c in api_calls if c[0] == "editMessageText"]
    assert len(edit_calls) == 1
    assert "Dropped" in edit_calls[0][1]["text"]
    assert bridge._pop_pending_voice(200) is None


def test_voice_reply_edit_routes_edited_text():
    """Replying to a voice preview routes the edited text to the worker."""
    import bridge

    bridge._pending_voice.clear()
    bridge._store_pending_voice(300, "original voice text", "charlie", 123, "")

    routed: list[tuple[str, str]] = []

    def mock_api(method: str, data: dict) -> dict:
        return {"ok": True, "result": {}}

    class FakeRouter(bridge.CommandRouter):
        def __init__(self) -> None:
            pass
        def route_message(self, name: str, text: str, chat_id, msg_id, one_off: bool = False) -> None:
            routed.append((name, text))

    router = FakeRouter()
    msg: bridge.TelegramMessageDict = {
        "message_id": 301,
        "date": 0,
        "chat": {"id": 123, "type": "private"},
        "reply_to_message": {"message_id": 300, "date": 0, "chat": {"id": 123, "type": "private"}},
    }

    with patch.object(bridge, "telegram_api", side_effect=mock_api):
        result = router._check_voice_reply(msg, "my edited text", 123)

    assert result is True
    assert len(routed) == 1
    assert routed[0] == ("charlie", "my edited text")
