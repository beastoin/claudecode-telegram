"""Tests for agent-friendly API: ENDPOINT_REGISTRY, ?describe, /doctor, errors, approval."""
import json
import re
import time
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
# Phase 1: ENDPOINT_REGISTRY
# ---------------------------------------------------------------------------

def test_registry_exists_and_has_entries():
    import bridge
    assert hasattr(bridge, "ENDPOINT_REGISTRY")
    assert len(bridge.ENDPOINT_REGISTRY) > 0


def test_api_endpoints_derived_from_registry():
    import bridge
    derived = {k: v["description"] for k, v in bridge.ENDPOINT_REGISTRY.items()}
    assert bridge.API_ENDPOINTS == derived


def test_registry_entries_have_required_fields():
    import bridge
    for key, schema in bridge.ENDPOINT_REGISTRY.items():
        assert "description" in schema, f"{key} missing description"
        assert "tier" in schema, f"{key} missing tier"


def test_registry_tier_values_valid():
    import bridge
    valid_tiers = {"worker", "hook", "guest", "channel", "relay", "admin", "read"}
    for key, schema in bridge.ENDPOINT_REGISTRY.items():
        assert schema["tier"] in valid_tiers, f"{key} has invalid tier: {schema['tier']}"


def test_worker_tier_includes_core_endpoints():
    import bridge
    worker_keys = {k for k, v in bridge.ENDPOINT_REGISTRY.items() if v["tier"] == "worker"}
    assert "GET /workers" in worker_keys
    assert "GET /checkin" in worker_keys
    assert "POST /teleport" in worker_keys
    assert "POST /messages" in worker_keys
    assert "GET /health" in worker_keys


def test_registry_post_endpoints_have_body_field():
    import bridge
    for key, schema in bridge.ENDPOINT_REGISTRY.items():
        if key.startswith("POST ") and schema["tier"] not in ("hook",):
            assert "body" in schema or "notes" in schema, (
                f"{key} is POST but has no body or notes field"
            )


# ---------------------------------------------------------------------------
# Phase 2: ?describe
# ---------------------------------------------------------------------------

class FakeWfile:
    def __init__(self):
        self.data = b""
    def write(self, b):
        self.data += b


class FakeHandler:
    """Minimal handler for testing _handle_describe."""
    def __init__(self):
        self.wfile = FakeWfile()
        self._code = None
        self._headers = {}

    def send_response(self, code):
        self._code = code

    def send_header(self, k, v):
        self._headers[k] = v

    def end_headers(self):
        pass

    def _send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code, message, *, error_code="", suggestion=""):
        import bridge
        bridge.Handler._send_error_json(self, code, message, error_code=error_code, suggestion=suggestion)

    def response_json(self):
        return json.loads(self.wfile.data.decode())


def test_describe_root_returns_full_registry():
    import bridge
    h = FakeHandler()
    bridge.Handler._handle_describe(h, "GET", "/")
    assert h._code == 200
    data = h.response_json()
    assert "endpoints" in data
    assert len(data["endpoints"]) == len(bridge.ENDPOINT_REGISTRY)


def test_describe_root_with_tier_filter():
    import bridge
    h = FakeHandler()
    h.path = "/?describe&tier=worker"
    bridge.Handler._handle_describe(h, "GET", "/", tier_filter="worker")
    assert h._code == 200
    data = h.response_json()
    for key, schema in data["endpoints"].items():
        assert schema["tier"] == "worker", f"{key} is not worker tier"


def test_describe_exact_endpoint():
    import bridge
    h = FakeHandler()
    bridge.Handler._handle_describe(h, "POST", "/teleport")
    assert h._code == 200
    data = h.response_json()
    assert data["endpoint"] == "POST /teleport"
    assert "body" in data
    assert "tier" in data


def test_describe_unknown_returns_404():
    import bridge
    h = FakeHandler()
    bridge.Handler._handle_describe(h, "GET", "/nonexistent-endpoint")
    assert h._code == 404
    data = h.response_json()
    assert "error" in data


def test_describe_pattern_endpoint():
    import bridge
    h = FakeHandler()
    bridge.Handler._handle_describe(h, "POST", "/relay/abc123/send")
    assert h._code == 200
    data = h.response_json()
    assert "relay" in data["endpoint"].lower()


# ---------------------------------------------------------------------------
# Phase 3: GET /doctor
# ---------------------------------------------------------------------------

def test_doctor_missing_name_returns_400():
    import bridge
    h = FakeHandler()
    bridge.handle_doctor_endpoint(h, {})
    assert h._code == 400
    data = h.response_json()
    assert "name" in data["error"].lower()


def test_doctor_unknown_worker():
    import bridge
    from unittest.mock import patch
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value={}):
        h = FakeHandler()
        bridge.handle_doctor_endpoint(h, {"name": "nonexistent"})
        assert h._code == 404
        data = h.response_json()
        assert "not found" in data["error"].lower()


def test_doctor_healthy_worker(tmp_path):
    import bridge
    from unittest.mock import patch
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    worker_dir = sessions_dir / "testworker"
    worker_dir.mkdir()
    (worker_dir / "chat_id").write_text("12345")
    bridge.SESSIONS_DIR = sessions_dir
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch("bridge.tmux_exists", return_value=True), \
         patch("bridge.get_claude_session_cwd", return_value=str(tmp_path)), \
         patch("bridge.get_worker_host", return_value=None):
        h = FakeHandler()
        bridge.handle_doctor_endpoint(h, {"name": "testworker"})
        assert h._code == 200
        data = h.response_json()
        assert data["ok"] is True
        assert data["failed"] == 0


def test_doctor_missing_session_dir(tmp_path):
    import bridge
    from unittest.mock import patch
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    bridge.SESSIONS_DIR = sessions_dir
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch("bridge.tmux_exists", return_value=True), \
         patch("bridge.get_claude_session_cwd", return_value=""), \
         patch("bridge.get_worker_host", return_value=None):
        h = FakeHandler()
        bridge.handle_doctor_endpoint(h, {"name": "testworker"})
        data = h.response_json()
        assert data["failed"] > 0
        assert data["ok"] is False


def test_doctor_remote_worker_skips_local_checks(tmp_path):
    import bridge
    from unittest.mock import patch
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    worker_dir = sessions_dir / "testworker"
    worker_dir.mkdir()
    (worker_dir / "chat_id").write_text("12345")
    bridge.SESSIONS_DIR = sessions_dir
    registry = {"testworker": {"tmux_name": "claude-test-testworker", "backend": "claude", "host": "macmini"}}
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value=registry), \
         patch("bridge.get_claude_session_cwd", return_value="/Users/beastoinagents/work"), \
         patch("bridge.get_worker_host", return_value="macmini"):
        h = FakeHandler()
        bridge.handle_doctor_endpoint(h, {"name": "testworker"})
        data = h.response_json()
        skipped = [c for c in data["checks"] if c["status"] == "skipped"]
        assert len(skipped) > 0, "Remote worker should skip local-only checks"


# ---------------------------------------------------------------------------
# Phase 4: Enhanced errors
# ---------------------------------------------------------------------------

def test_error_json_backward_compat():
    import bridge
    h = FakeHandler()
    bridge.Handler._send_error_json(h, 400, "bad request")
    data = h.response_json()
    assert data == {"ok": False, "error": "bad request"}


def test_error_json_with_code_and_suggestion():
    import bridge
    h = FakeHandler()
    bridge.Handler._send_error_json(h, 404, "Worker 'foo' not found",
                                     error_code="WORKER_NOT_FOUND",
                                     suggestion="Run GET /workers to see active workers")
    data = h.response_json()
    assert data["ok"] is False
    assert data["code"] == "WORKER_NOT_FOUND"
    assert "suggestion" in data


def test_404_handler_includes_describe_hint():
    import bridge
    h = FakeHandler()
    bridge.Handler._send_unknown_endpoint(h, "GET", "/nonexistent")
    data = h.response_json()
    assert "describe" in data.get("hint", "").lower()


# ---------------------------------------------------------------------------
# Phase 5: Auto-generated checkin note
# ---------------------------------------------------------------------------

def test_worker_api_summary_includes_core_endpoints():
    import bridge
    summary = bridge._worker_api_summary()
    assert "GET /workers" in summary
    assert "POST /teleport" in summary
    assert "GET /doctor" in summary


def test_build_welcome_uses_auto_generated_endpoints():
    import bridge
    import claudecode
    from unittest.mock import patch
    backend = claudecode.get_backend("claude")
    with patch.object(bridge.worker_manager, "get_registered_sessions", return_value={"lee": {}}):
        welcome = bridge.worker_manager._build_welcome("lee", backend)
        assert "/?describe" in welcome
        assert "GET /workers" not in welcome or "GET /workers" in bridge._worker_api_summary()


# ---------------------------------------------------------------------------
# Phase 6: Approval UI
# ---------------------------------------------------------------------------

def test_approval_token_created():
    import bridge
    details = {"header": "ENDPOINT_REGISTRY", "options": [
        {"num": 1, "label": "Yes, bypass", "selected": True},
        {"num": 2, "label": "No", "selected": False},
    ], "selected_num": 1}
    token, pin = bridge._create_approval_token("lee", "claude-prod-lee", details, None)
    assert len(token) == 8
    assert 1000 <= int(pin) <= 9999
    assert token in bridge._approval_tokens
    stored = bridge._approval_tokens[token]
    assert stored["worker"] == "lee"
    assert stored["pin"] == pin
    # cleanup
    del bridge._approval_tokens[token]


def test_approval_get_renders_html():
    import bridge
    details = {"header": "Test question", "options": [
        {"num": 1, "label": "Yes", "selected": True},
        {"num": 2, "label": "No", "selected": False},
    ], "selected_num": 1}
    token, pin = bridge._create_approval_token("lee", "claude-prod-lee", details, None)
    h = FakeHandler()
    bridge.handle_approval_get(h, token)
    assert h._code == 200
    html = h.wfile.data.decode()
    assert "Test question" in html
    assert "Yes" in html
    assert "No" in html
    assert "pin" in html.lower()
    del bridge._approval_tokens[token]


def test_approval_post_correct_pin():
    import bridge
    from unittest.mock import patch
    details = {"header": "Test", "options": [
        {"num": 1, "label": "Yes", "selected": True},
        {"num": 2, "label": "No", "selected": False},
    ], "selected_num": 1}
    token, pin = bridge._create_approval_token("lee", "claude-prod-lee", details, None)
    h = FakeHandler()
    with patch("bridge._send_interactive_reply", return_value=True) as mock_reply:
        bridge.handle_approval_post(h, token, {"choice": 1, "pin": pin})
        mock_reply.assert_called_once()
    assert h._code == 200
    data = h.response_json()
    assert data["ok"] is True
    assert bridge._approval_tokens[token]["used"] is True


def test_approval_wrong_pin_rejected():
    import bridge
    details = {"header": "Test", "options": [
        {"num": 1, "label": "Yes", "selected": True},
    ], "selected_num": 1}
    token, pin = bridge._create_approval_token("lee", "claude-prod-lee", details, None)
    h = FakeHandler()
    bridge.handle_approval_post(h, token, {"choice": 1, "pin": "0000"})
    assert h._code == 403
    # 3 wrong attempts should invalidate
    bridge.handle_approval_post(h, token, {"choice": 1, "pin": "0001"})
    bridge.handle_approval_post(h, token, {"choice": 1, "pin": "0002"})
    h2 = FakeHandler()
    bridge.handle_approval_post(h2, token, {"choice": 1, "pin": pin})
    assert h2._code in (403, 410), "Token should be dead after 3 wrong attempts"
    if token in bridge._approval_tokens:
        del bridge._approval_tokens[token]


def test_approval_token_expires():
    import bridge
    details = {"header": "Test", "options": [
        {"num": 1, "label": "Yes", "selected": True},
    ], "selected_num": 1}
    token, pin = bridge._create_approval_token("lee", "claude-prod-lee", details, None)
    bridge._approval_tokens[token]["created"] = time.time() - 600  # 10 min ago
    h = FakeHandler()
    bridge.handle_approval_post(h, token, {"choice": 1, "pin": pin})
    assert h._code == 410
    data = h.response_json()
    assert "expired" in data["error"].lower()
    if token in bridge._approval_tokens:
        del bridge._approval_tokens[token]
