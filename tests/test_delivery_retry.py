"""Tests for message delivery retry and failure alerting."""
import json
import threading
import time
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
    import bridge
    saved = {
        "NODE_DIR": bridge.NODE_DIR,
        "SESSIONS_DIR": bridge.SESSIONS_DIR,
        "WORKER_REGISTRY_FILE": bridge.WORKER_REGISTRY_FILE,
        "wm_scan": bridge.worker_manager.scan_tmux_sessions,
        "wm_get_reg": bridge.worker_manager.get_registered_sessions,
    }
    yield
    bridge.NODE_DIR = saved["NODE_DIR"]
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.worker_manager.scan_tmux_sessions = saved["wm_scan"]
    bridge.worker_manager.get_registered_sessions = saved["wm_get_reg"]
    bridge.worker_manager.invalidate_sessions_cache()


# ---------------------------------------------------------------------------
# 1. Reaction removed
# ---------------------------------------------------------------------------

def test_no_reaction_on_successful_send():
    """The 👀 reaction should NOT be sent after successful message delivery."""
    import bridge
    src = open(bridge.__file__).read()
    # Find route_message method which handles dispatch
    idx = src.index("def route_message(")
    section = src[idx:]
    # Get just this method (up to next def at same indent level)
    lines = section.split("\n")
    method_lines = [lines[0]]
    for line in lines[1:]:
        if line and not line[0].isspace() and line.strip():
            break
        method_lines.append(line)
    method_text = "\n".join(method_lines)
    assert "set_reaction" not in method_text, (
        "set_reaction should be removed from route_message"
    )


# ---------------------------------------------------------------------------
# 2. Retry logic in WorkerManager.send()
# ---------------------------------------------------------------------------

def test_send_succeeds_first_attempt_no_retry():
    """When send succeeds on first try, no retry happens."""
    import bridge
    import claudecode
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", return_value=True) as mock_send:
        result = bridge.worker_manager.send("testworker", "hello")
        assert result is True
        assert mock_send.call_count == 1


def test_send_retries_on_failure():
    """When first send fails but session is alive, retry up to max attempts."""
    import bridge
    import claudecode
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", side_effect=[False, False, True]) as mock_send, \
         patch("claudecode.tmux_exists", return_value=True), \
         patch("bridge.get_worker_host", return_value=None), \
         patch("time.sleep"):
        result = bridge.worker_manager.send("testworker", "hello")
        assert result is True
        assert mock_send.call_count == 3


def test_send_fails_after_max_retries():
    """After all retry attempts fail, send returns False."""
    import bridge
    import claudecode
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", return_value=False) as mock_send, \
         patch("claudecode.tmux_exists", return_value=True), \
         patch("bridge.get_worker_host", return_value=None), \
         patch("time.sleep"):
        result = bridge.worker_manager.send("testworker", "hello")
        assert result is False
        assert mock_send.call_count == 3


def test_send_skips_retry_when_session_dead():
    """If tmux session is gone, don't retry — fail immediately."""
    import bridge
    import claudecode
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", return_value=False) as mock_send, \
         patch("claudecode.tmux_exists", return_value=False), \
         patch("bridge.get_worker_host", return_value=None), \
         patch("time.sleep"):
        result = bridge.worker_manager.send("testworker", "hello")
        assert result is False
        assert mock_send.call_count == 1


def test_retry_uses_backoff():
    """Retry delays should increase (backoff pattern)."""
    import bridge
    import claudecode
    sleep_calls = []
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", return_value=False) as mock_send, \
         patch("claudecode.tmux_exists", return_value=True), \
         patch("bridge.get_worker_host", return_value=None), \
         patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        bridge.worker_manager.send("testworker", "hello")
        assert len(sleep_calls) == 2  # 2 retries = 2 sleeps (not before first attempt)
        assert sleep_calls[0] < sleep_calls[1], "Backoff should increase"


def test_callback_worker_no_retry():
    """Callback workers use HTTP — retry logic doesn't apply to them."""
    import bridge
    registry = {"cbworker": {"callback_url": "http://localhost:9999/cb", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch("bridge._send_to_callback_worker", return_value=True) as mock_cb:
        result = bridge.worker_manager.send("cbworker", "hello")
        assert result is True
        assert mock_cb.call_count == 1


def test_codex_backend_no_retry():
    """Codex backend fires async thread and returns True immediately — no retry needed."""
    import bridge
    import claudecode
    registry = {"codexworker": {"tmux_name": "claude-test-codexworker", "backend": "codex"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.CodexBackend, "send", return_value=True) as mock_send:
        result = bridge.worker_manager.send("codexworker", "hello")
        assert result is True
        assert mock_send.call_count == 1


# ---------------------------------------------------------------------------
# 3. Failure alert after retries exhausted
# ---------------------------------------------------------------------------

def test_route_message_alerts_on_delivery_failure(tmp_path):
    """After all retries fail, route_message should fire health.error('dispatch')."""
    import bridge

    health_signals = []
    original_error = bridge.health.error
    def capture_error(signal, msg="", **kw):
        health_signals.append(("error", signal, msg))
        return original_error(signal, msg, **kw)

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    worker_dir = sessions_dir / "testworker"
    worker_dir.mkdir()
    (worker_dir / "chat_id").write_text("12345")
    bridge.SESSIONS_DIR = sessions_dir

    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(bridge.worker_manager, "is_online", return_value=True), \
         patch.object(bridge.worker_manager, "send", return_value=False), \
         patch.object(bridge.health, "error", side_effect=capture_error), \
         patch.object(bridge, "send_telegram_message"), \
         patch("bridge.send_typing_loop"), \
         patch("bridge.clear_pending"):
        router = MagicMock()
        router.workers = bridge.worker_manager
        router.transport = None
        router.reply = MagicMock()
        bridge.CommandRouter.route_message(router, "testworker", "hello", 12345, None)
        dispatch_errors = [(sig, msg) for kind, sig, msg in health_signals if sig == "dispatch"]
        assert len(dispatch_errors) > 0, "Should fire health.error('dispatch') on failure"
        assert any("testworker" in msg and "failed" in msg for _, msg in dispatch_errors)


def test_route_message_no_alert_on_success(tmp_path):
    """Successful delivery should fire health.ok('dispatch'), not error."""
    import bridge

    health_signals = []
    original_ok = bridge.health.ok
    def capture_ok(signal, **kw):
        health_signals.append(("ok", signal))
        return original_ok(signal, **kw)

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    worker_dir = sessions_dir / "testworker"
    worker_dir.mkdir()
    (worker_dir / "chat_id").write_text("12345")
    bridge.SESSIONS_DIR = sessions_dir

    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(bridge.worker_manager, "is_online", return_value=True), \
         patch.object(bridge.worker_manager, "send", return_value=True), \
         patch.object(bridge.health, "ok", side_effect=capture_ok), \
         patch("bridge.send_typing_loop"):
        router = MagicMock()
        router.workers = bridge.worker_manager
        router.transport = MagicMock()
        router.reply = MagicMock()
        bridge.CommandRouter.route_message(router, "testworker", "hello", 12345, None)
        assert ("ok", "dispatch") in health_signals


# ---------------------------------------------------------------------------
# 4. Delivery logging
# ---------------------------------------------------------------------------

def test_send_logs_delivery_attempts(capsys):
    """Each delivery attempt should be logged for traceability."""
    import bridge
    import claudecode
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", side_effect=[False, True]) as mock_send, \
         patch("claudecode.tmux_exists", return_value=True), \
         patch("bridge.get_worker_host", return_value=None), \
         patch("time.sleep"):
        bridge.worker_manager.send("testworker", "hello")
    captured = capsys.readouterr()
    assert "attempt 2/3" in captured.err, f"Should log retry attempt in stderr, got: {captured.err[:200]}"


# ---------------------------------------------------------------------------
# 5. Concurrency: retries don't block other deliveries
# ---------------------------------------------------------------------------

def test_concurrent_sends_independent():
    """Multiple sends to different workers should not block each other."""
    import bridge
    import claudecode

    def slow_send(worker_name, tmux_name, text, bridge_url, sessions_dir):
        time.sleep(0.05)
        return True

    registry = {
        "worker1": {"tmux_name": "claude-test-worker1", "backend": "claude"},
        "worker2": {"tmux_name": "claude-test-worker2", "backend": "claude"},
    }
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch.object(claudecode.ClaudeBackend, "send", side_effect=slow_send):
        t1 = threading.Thread(target=bridge.worker_manager.send, args=("worker1", "hello"))
        t2 = threading.Thread(target=bridge.worker_manager.send, args=("worker2", "hello"))
        start = time.monotonic()
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        elapsed = time.monotonic() - start
        assert elapsed < 0.15, f"Concurrent sends took {elapsed:.2f}s — should be parallel, not serial"
