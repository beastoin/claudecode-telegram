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
