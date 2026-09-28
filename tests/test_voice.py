"""Tests migrated from test.sh — voice category."""
import os
import pytest
from unittest.mock import MagicMock, patch

from unittest.mock import patch
from unittest.mock import patch, MagicMock
from unittest.mock import patch, MagicMock, call
import sys, os
import sys, os, json, tempfile
import sys, os, tempfile
import sys, os, time
import urllib.error


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

    sys.path.insert(0, os.getcwd())

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
        assert result == 'hello world', f'Expected \"hello world\", got {result!r}'
    finally:
        os.unlink(tmp.name)


def test_transcribe_voice_timeout_returns_none():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Simulate timeout
    with patch('bridge._urlopen', side_effect=TimeoutError('timeout')):
        result = bridge.transcribe_voice('/tmp/test.ogg')

    assert result is None, f'Expected None, got {result!r}'


def test_transcribe_voice_bad_json_returns_none():
    import bridge

    sys.path.insert(0, os.getcwd())

    mock_response = MagicMock()
    mock_response.read.return_value = b'not json'
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch('bridge._urlopen', return_value=mock_response):
        result = bridge.transcribe_voice('/tmp/test.ogg')

    assert result is None, f'Expected None, got {result!r}'


def test_synthesize_speech_success():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Mock urllib to return audio bytes
    mock_response = MagicMock()
    mock_response.read.return_value = b'OggS fake audio data'
    mock_response.headers = {'X-Audio-Duration': '3.5', 'X-Processing-Time': '2.1'}
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch('bridge._urlopen', return_value=mock_response):
        result = bridge.synthesize_speech('Hello world')

    assert result is not None, 'Expected file path, got None'
    assert os.path.exists(result), f'File does not exist: {result}'
    assert result.endswith('.ogg'), f'Expected .ogg file, got: {result}'

    # Verify content
    with open(result, 'rb') as f:
        content = f.read()
    assert content == b'OggS fake audio data', f'Unexpected content'

    os.unlink(result)


def test_synthesize_speech_timeout_returns_none():
    import bridge

    sys.path.insert(0, os.getcwd())

    with patch('bridge._urlopen', side_effect=TimeoutError('timeout')):
        result = bridge.synthesize_speech('Hello world')

    assert result is None, f'Expected None, got {result!r}'


def test_synthesize_speech_uses_chunked_for_long_text():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Save original
    orig_threshold = bridge.TTS_CHUNKED_THRESHOLD

    # Mock urllib to capture which URL is called
    mock_response = MagicMock()
    mock_response.read.return_value = b'OggS fake audio data'
    mock_response.headers = {'X-Audio-Duration': '10.0', 'X-Processing-Time': '5.0'}
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    # Short text: should use base /synthesize endpoint
    bridge.TTS_CHUNKED_THRESHOLD = 200
    with patch('bridge._urlopen', return_value=mock_response) as mock_url:
        result = bridge.synthesize_speech('Short text')
        called_url = mock_url.call_args[0][0].full_url
        assert '/synthesize/chunked' not in called_url, f'Short text used chunked: {called_url}'
        os.unlink(result)

    # Long text: should use /synthesize/chunked endpoint
    long_text = 'This is a test sentence. ' * 20  # ~500 chars
    with patch('bridge._urlopen', return_value=mock_response) as mock_url:
        result = bridge.synthesize_speech(long_text)
        called_url = mock_url.call_args[0][0].full_url
        assert '/synthesize/chunked' in called_url, f'Long text did not use chunked: {called_url}'
        os.unlink(result)

    bridge.TTS_CHUNKED_THRESHOLD = orig_threshold


def test_auto_tts_sends_voice_with_response():
    import bridge

    sys.path.insert(0, os.getcwd())

    bridge.BOT_TOKEN = 'fake'
    bridge.admin_chat_id = 12345
    bridge.state.tts_enabled = True

    voice_sent = []
    api_calls = []

    def mock_send_voice(chat_id, path, caption=None):
        voice_sent.append((chat_id, path, caption))
        return True

    def mock_telegram_api(method, data):
        api_calls.append(method)
        return {'ok': True, 'result': {'message_id': 1}}

    response_text = 'Here is my answer to your question.'

    with patch.object(bridge, 'send_voice', side_effect=mock_send_voice), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'synthesize_speech', return_value='/tmp/voice.ogg') as mock_tts, \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', response_text, 12345)
        time.sleep(0.5)  # TTS runs in background thread

    # Text should be sent (via sendRichMessage or sendMessage)
    assert len(api_calls) >= 1, f'Expected at least 1 API call, got {len(api_calls)}'

    # Voice should be auto-synthesized (no [[speak]] needed)
    mock_tts.assert_called_once()
    assert len(voice_sent) == 1, f'Expected 1 voice sent, got {len(voice_sent)}'


def test_speak_tag_custom_text():
    import bridge

    sys.path.insert(0, os.getcwd())

    bridge.BOT_TOKEN = 'fake'
    bridge.admin_chat_id = 12345

    tts_calls = []

    def mock_tts(text, **kwargs):
        tts_calls.append(text)
        return '/tmp/voice.ogg'

    def mock_telegram_api(method, data):
        return {'ok': True, 'result': {'message_id': 1}}

    response_text = 'Complex technical explanation with code.\n\n[[speak:Here is the short summary]]'

    with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
         patch.object(bridge, 'send_voice', return_value=True), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', response_text, 12345)
        time.sleep(0.3)

    assert len(tts_calls) == 1, f'Expected 1 TTS call, got {len(tts_calls)}'
    assert tts_calls[0] == 'Here is the short summary', f'TTS called with wrong text: {tts_calls[0]!r}'


def test_auto_tts_skips_long_messages():
    import bridge

    sys.path.insert(0, os.getcwd())

    bridge.BOT_TOKEN = 'fake'
    bridge.admin_chat_id = 12345
    bridge.state.tts_enabled = True

    tts_calls = []

    def mock_tts(text, **kwargs):
        tts_calls.append(text)
        return '/tmp/voice.ogg'

    def mock_telegram_api(method, data):
        return {'ok': True, 'result': {'message_id': 1}}

    # Multi-paragraph text under 1000 chars — should split into separate TTS calls
    tts_calls.clear()
    multi_para = 'First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph.'
    with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
         patch.object(bridge, 'send_voice', return_value=True), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', multi_para, 12345)
        time.sleep(0.5)

    assert len(tts_calls) == 3, f'Expected 3 TTS calls (one per paragraph), got {len(tts_calls)}: {tts_calls}'
    assert tts_calls[0] == 'First paragraph here.', f'Wrong para 1: {tts_calls[0]!r}'
    assert tts_calls[1] == 'Second paragraph here.', f'Wrong para 2: {tts_calls[1]!r}'
    assert tts_calls[2] == 'Third paragraph.', f'Wrong para 3: {tts_calls[2]!r}'

    # Long text (>1000 chars) — TTS should be skipped entirely
    tts_calls.clear()
    long_text = 'A' * 1001
    with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
         patch.object(bridge, 'send_voice', return_value=True), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', long_text, 12345)
        time.sleep(0.3)

    assert len(tts_calls) == 0, f'Expected 0 TTS calls for >1000 char text, got {len(tts_calls)}'


def test_auto_tts_failure_still_sends_text():
    import bridge

    sys.path.insert(0, os.getcwd())

    bridge.BOT_TOKEN = 'fake'
    bridge.admin_chat_id = 12345
    bridge.state.tts_enabled = True

    api_calls = []

    def mock_telegram_api(method, data):
        api_calls.append(method)
        return {'ok': True, 'result': {'message_id': 1}}

    response_text = 'Important information.'

    with patch.object(bridge, 'synthesize_speech', return_value=None), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', response_text, 12345)
        time.sleep(0.3)

    # Text should still be sent even when TTS fails (via sendRichMessage or sendMessage)
    assert len(api_calls) >= 1, f'Expected text to be sent, got {len(api_calls)}'


def test_voice_toggle_command():
    import bridge

    sys.path.insert(0, os.getcwd())

    bridge.BOT_TOKEN = 'fake'
    bridge.admin_chat_id = 12345
    bridge.state.tts_enabled = True

    # Test /voice off disables TTS
    tts_calls = []

    def mock_tts(text, **kwargs):
        tts_calls.append(text)
        return '/tmp/voice.ogg'

    def mock_telegram_api(method, data):
        return {'ok': True, 'result': {'message_id': 1}}

    # Disable TTS
    bridge.state.tts_enabled = False

    with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
         patch.object(bridge, 'send_voice', return_value=True), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', 'Hello text only', 12345)
        time.sleep(0.3)

    assert len(tts_calls) == 0, f'TTS should not be called when disabled, got {len(tts_calls)} calls'

    # Re-enable TTS
    bridge.state.tts_enabled = True

    with patch.object(bridge, 'synthesize_speech', side_effect=mock_tts), \
         patch.object(bridge, 'send_voice', return_value=True), \
         patch.object(bridge, 'telegram_api', side_effect=mock_telegram_api), \
         patch.object(bridge, 'get_worker_host', return_value=None):
        bridge.send_response_to_telegram('testworker', 'Hello with voice', 12345)
        time.sleep(0.3)

    assert len(tts_calls) == 1, f'TTS should be called when enabled, got {len(tts_calls)} calls'

