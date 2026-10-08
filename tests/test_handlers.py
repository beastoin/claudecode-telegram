import json
import os
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from urllib.parse import ParseResult, urlencode

import pytest


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "12345")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))
    import bridge
    saved = {
        "SESSIONS_DIR": bridge.SESSIONS_DIR,
        "WORKER_REGISTRY_FILE": bridge.WORKER_REGISTRY_FILE,
        "NODE_DIR": bridge.NODE_DIR,
        "WORKER_PIPE_ROOT": getattr(bridge, "WORKER_PIPE_ROOT", None),
        "admin_chat_id": bridge.admin_chat_id,
        "transport": bridge.transport,
    }
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = tmp_path / "node"
    bridge.NODE_DIR.mkdir(exist_ok=True)
    bridge.WORKER_REGISTRY_FILE = tmp_path / "workers.json"
    bridge.admin_chat_id = 12345
    saved_active = bridge.state.active
    saved_scan = bridge.worker_manager.scan_tmux_sessions
    saved_get_reg = bridge.worker_manager.get_registered_sessions
    yield
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.NODE_DIR = saved["NODE_DIR"]
    if saved["WORKER_PIPE_ROOT"] is not None:
        bridge.WORKER_PIPE_ROOT = saved["WORKER_PIPE_ROOT"]
    bridge.admin_chat_id = saved["admin_chat_id"]
    bridge.transport = saved["transport"]
    bridge.state.active = saved_active
    bridge.worker_manager.scan_tmux_sessions = saved_scan
    bridge.worker_manager.get_registered_sessions = saved_get_reg
    bridge.worker_manager.invalidate_sessions_cache()


def _make_handler():
    import bridge
    handler = MagicMock(spec=bridge.Handler)
    handler.client_address = ("127.0.0.1", 9999)
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler._send_json = bridge.Handler._send_json.__get__(handler, bridge.Handler)
    handler._send_text = bridge.Handler._send_text.__get__(handler, bridge.Handler)
    handler._validate_response_source = bridge.Handler._validate_response_source.__get__(handler, bridge.Handler)
    return handler


# ── POST /notify ──────────────────────────────────────────


class TestHandleNotify:
    def test_notify_missing_text_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_notify = bridge.Handler.handle_notify.__get__(handler, bridge.Handler)
        body = json.dumps({"name": "lee"}).encode()
        handler.handle_notify(body)
        handler.send_response.assert_called_with(400)

    def test_notify_sends_to_all_chat_ids(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_notify = bridge.Handler.handle_notify.__get__(handler, bridge.Handler)
        mock_transport = MagicMock()
        mock_transport.send_text.return_value = {"ok": True}
        bridge.transport = mock_transport
        cid_file = bridge.SESSIONS_DIR / "worker1" / "chat_id"
        cid_file.parent.mkdir(parents=True)
        cid_file.write_text("12345")
        body = json.dumps({"text": "hello world", "name": "bot"}).encode()
        handler.handle_notify(body)
        handler.send_response.assert_called_with(200)
        assert mock_transport.send_text.called

    def test_notify_parses_media_tags_in_text(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_notify = bridge.Handler.handle_notify.__get__(handler, bridge.Handler)
        mock_transport = MagicMock()
        mock_transport.send_text.return_value = {"ok": True}
        bridge.transport = mock_transport
        with patch.object(bridge, "get_all_chat_ids", return_value=["12345"]), \
             patch.object(bridge, "send_photo") as mock_photo:
            img_path = tmp_path / "test.png"
            img_path.write_bytes(b"\x89PNG")
            body = json.dumps({"text": f"check this [[image:{img_path}|screenshot]]", "name": ""}).encode()
            handler.handle_notify(body)
            handler.send_response.assert_called_with(200)


# ── POST /alerts ──────────────────────────────────────────


class TestHandleHealthAlert:
    def test_health_alert_sends_to_all_chats(self):
        import bridge
        handler = _make_handler()
        handler.handle_health_alert = bridge.Handler.handle_health_alert.__get__(handler, bridge.Handler)
        mock_transport = MagicMock()
        bridge.transport = mock_transport
        with patch.object(bridge, "get_all_chat_ids", return_value=["12345"]):
            body = json.dumps({"worker": "lee", "issue": "stale", "transcript_age": 7200}).encode()
            handler.handle_health_alert(body)
        assert mock_transport.send_text.called
        alert_text = mock_transport.send_text.call_args[0][1]
        assert "lee" in alert_text
        assert "2h0m" in alert_text

    def test_health_alert_formats_minutes_only(self):
        import bridge
        handler = _make_handler()
        handler.handle_health_alert = bridge.Handler.handle_health_alert.__get__(handler, bridge.Handler)
        mock_transport = MagicMock()
        bridge.transport = mock_transport
        with patch.object(bridge, "get_all_chat_ids", return_value=["12345"]):
            body = json.dumps({"worker": "finn", "issue": "stale", "transcript_age": 900}).encode()
            handler.handle_health_alert(body)
        alert_text = mock_transport.send_text.call_args[0][1]
        assert "15m" in alert_text

    def test_health_alert_returns_ok_json(self):
        import bridge
        handler = _make_handler()
        handler.handle_health_alert = bridge.Handler.handle_health_alert.__get__(handler, bridge.Handler)
        bridge.transport = MagicMock()
        with patch.object(bridge, "get_all_chat_ids", return_value=[]):
            body = json.dumps({"worker": "x", "issue": "stale", "transcript_age": 60}).encode()
            handler.handle_health_alert(body)
        handler.send_response.assert_called_with(200)


# ── POST /workers (forge register) ───────────────────────


class TestHandleForgeRegister:
    def test_forge_register_creates_worker(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_forge_register = bridge.Handler.handle_forge_register.__get__(handler, bridge.Handler)
        bridge.WORKER_REGISTRY_FILE = tmp_path / "workers.json"
        (bridge.SESSIONS_DIR / "forgebot").mkdir(parents=True, exist_ok=True)
        with patch.object(bridge, "tmux_exists", return_value=False), \
             patch.object(bridge, "export_hook_env"), \
             patch.object(bridge, "ensure_session_dir"):
            body = json.dumps({"name": "forgebot", "host": "remote-host"}).encode()
            handler.handle_forge_register(body)
        handler.send_response.assert_called_with(200)
        reg = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
        assert "forgebot" in reg.get("workers", {})

    def test_forge_register_callback_worker(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_forge_register = bridge.Handler.handle_forge_register.__get__(handler, bridge.Handler)
        bridge.WORKER_REGISTRY_FILE = tmp_path / "workers.json"
        (bridge.SESSIONS_DIR / "callbackbot").mkdir(parents=True, exist_ok=True)
        with patch.object(bridge, "tmux_exists", return_value=False), \
             patch.object(bridge, "export_hook_env"), \
             patch.object(bridge, "ensure_session_dir"):
            body = json.dumps({
                "name": "callbackbot",
                "host": "remote-host",
                "callback_url": "http://remote:8080/callback",
            }).encode()
            handler.handle_forge_register(body)
        handler.send_response.assert_called_with(200)
        reg = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
        worker = reg.get("workers", {}).get("callbackbot", {})
        assert worker.get("callback_url") == "http://remote:8080/callback"


# ── GET /connectors + POST /connectors/restart ────────────


class TestConnectorEndpoints:
    def test_connectors_status_returns_json(self):
        import bridge
        handler = _make_handler()
        handler.handle_connectors_status = bridge.Handler.handle_connectors_status.__get__(handler, bridge.Handler)
        with patch.object(bridge, "_get_connectors_status", return_value={"gmail": "off", "github": "off"}):
            handler.handle_connectors_status()
        handler.send_response.assert_called_with(200)

    def test_connectors_restart_missing_name_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_connectors_restart = bridge.Handler.handle_connectors_restart.__get__(handler, bridge.Handler)
        body = json.dumps({}).encode()
        handler.handle_connectors_restart(body)
        handler.send_response.assert_called_with(400)

    def test_connectors_restart_valid_name(self):
        import bridge
        handler = _make_handler()
        handler.handle_connectors_restart = bridge.Handler.handle_connectors_restart.__get__(handler, bridge.Handler)
        with patch.object(bridge, "_restart_connector", return_value=(True, "restarted")):
            body = json.dumps({"name": "gmail"}).encode()
            handler.handle_connectors_restart(body)
        handler.send_response.assert_called_with(200)

    def test_connectors_restart_invalid_json_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_connectors_restart = bridge.Handler.handle_connectors_restart.__get__(handler, bridge.Handler)
        handler.handle_connectors_restart(b"not json{{{")
        handler.send_response.assert_called_with(400)


# ── POST /messages ────────────────────────────────────────


class TestHandleSendEndpoint:
    def test_send_missing_worker_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_send_endpoint = bridge.Handler.handle_send_endpoint.__get__(handler, bridge.Handler)
        body = json.dumps({"message": "hello"}).encode()
        handler.handle_send_endpoint(body)
        handler.send_response.assert_called_with(400)

    def test_send_missing_message_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_send_endpoint = bridge.Handler.handle_send_endpoint.__get__(handler, bridge.Handler)
        body = json.dumps({"worker": "lee"}).encode()
        handler.handle_send_endpoint(body)
        handler.send_response.assert_called_with(400)

    def test_send_delivers_to_worker(self):
        import bridge
        handler = _make_handler()
        handler.handle_send_endpoint = bridge.Handler.handle_send_endpoint.__get__(handler, bridge.Handler)
        with patch.object(bridge, "send_to_worker", return_value=True) as mock_send:
            body = json.dumps({"worker": "lee", "message": "build the feature", "from": "manager"}).encode()
            handler.handle_send_endpoint(body)
        handler.send_response.assert_called_with(200)
        mock_send.assert_called_once_with("lee", "manager: build the feature")

    def test_send_worker_not_found_returns_404(self):
        import bridge
        handler = _make_handler()
        handler.handle_send_endpoint = bridge.Handler.handle_send_endpoint.__get__(handler, bridge.Handler)
        with patch.object(bridge, "send_to_worker", return_value=False):
            body = json.dumps({"worker": "nonexistent", "message": "hello"}).encode()
            handler.handle_send_endpoint(body)
        handler.send_response.assert_called_with(404)

    def test_send_uses_text_field_as_fallback(self):
        import bridge
        handler = _make_handler()
        handler.handle_send_endpoint = bridge.Handler.handle_send_endpoint.__get__(handler, bridge.Handler)
        with patch.object(bridge, "send_to_worker", return_value=True) as mock_send:
            body = json.dumps({"worker": "lee", "text": "fallback text"}).encode()
            handler.handle_send_endpoint(body)
        mock_send.assert_called_once_with("lee", "system: fallback text")

    def test_send_invalid_json_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_send_endpoint = bridge.Handler.handle_send_endpoint.__get__(handler, bridge.Handler)
        handler.handle_send_endpoint(b"not json")
        handler.send_response.assert_called_with(400)


# ── POST /outputs (hook response) ────────────────────────


class TestHandleHookResponse:
    def test_hook_response_missing_session_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_hook_response = bridge.Handler.handle_hook_response.__get__(handler, bridge.Handler)
        body = json.dumps({"text": "hello"}).encode()
        handler.handle_hook_response(body)
        handler.send_response.assert_called_with(400)

    def test_hook_response_missing_text_returns_400(self):
        import bridge
        handler = _make_handler()
        handler.handle_hook_response = bridge.Handler.handle_hook_response.__get__(handler, bridge.Handler)
        body = json.dumps({"session": "claude-test-lee"}).encode()
        handler.handle_hook_response(body)
        handler.send_response.assert_called_with(400)

    def test_hook_response_no_chat_id_returns_404(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_hook_response = bridge.Handler.handle_hook_response.__get__(handler, bridge.Handler)
        bridge.admin_chat_id = None
        body = json.dumps({
            "session": "claude-test-lee",
            "text": "done",
            "source": "claude-test-lee",
        }).encode()
        handler.handle_hook_response(body)
        handler.send_response.assert_called_with(404)

    def test_hook_response_sends_to_telegram(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_hook_response = bridge.Handler.handle_hook_response.__get__(handler, bridge.Handler)
        cid_file = bridge.SESSIONS_DIR / "lee" / "chat_id"
        cid_file.parent.mkdir(parents=True)
        cid_file.write_text("12345")
        with patch.object(bridge, "send_response_to_telegram") as mock_send, \
             patch.object(bridge, "clear_pending"), \
             patch.object(bridge, "mark_hook_event"), \
             patch.object(bridge, "_check_learning_reminder"), \
             patch.object(bridge, "get_chat_id_file", return_value=cid_file):
            body = json.dumps({
                "session": "lee",
                "text": "feature built",
                "source": "lee",
            }).encode()
            handler.handle_hook_response(body)
        handler.send_response.assert_called_with(200)
        mock_send.assert_called_once_with("lee", "feature built", 12345, log_prefix="Response")

    def test_hook_response_caches_session_id(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_hook_response = bridge.Handler.handle_hook_response.__get__(handler, bridge.Handler)
        cid_file = bridge.SESSIONS_DIR / "lee" / "chat_id"
        cid_file.parent.mkdir(parents=True)
        cid_file.write_text("12345")
        with patch.object(bridge, "send_response_to_telegram"), \
             patch.object(bridge, "clear_pending"), \
             patch.object(bridge, "mark_hook_event"), \
             patch.object(bridge, "_check_learning_reminder"), \
             patch.object(bridge, "_cache_session_id") as mock_cache, \
             patch.object(bridge, "get_chat_id_file", return_value=cid_file):
            body = json.dumps({
                "session": "lee",
                "text": "done",
                "source": "lee",
                "session_id": "abc123def456",
            }).encode()
            handler.handle_hook_response(body)
        mock_cache.assert_called_once_with("lee", "abc123def456")

    def test_hook_response_auto_creates_chat_id_from_admin(self, tmp_path):
        import bridge
        handler = _make_handler()
        handler.handle_hook_response = bridge.Handler.handle_hook_response.__get__(handler, bridge.Handler)
        session_dir = bridge.SESSIONS_DIR / "newworker"
        session_dir.mkdir(parents=True)
        cid_file = session_dir / "chat_id"
        bridge.admin_chat_id = 99999
        with patch.object(bridge, "send_response_to_telegram"), \
             patch.object(bridge, "clear_pending"), \
             patch.object(bridge, "mark_hook_event"), \
             patch.object(bridge, "_check_learning_reminder"), \
             patch.object(bridge, "get_chat_id_file", return_value=cid_file), \
             patch.object(bridge, "ensure_session_dir"):
            body = json.dumps({
                "session": "newworker",
                "text": "hello",
                "source": "newworker",
            }).encode()
            handler.handle_hook_response(body)
        handler.send_response.assert_called_with(200)
        assert cid_file.exists()
        assert cid_file.read_text().strip() == "99999"


# ── GET /machines ─────────────────────────────────────────


class TestHandleMachinesEndpoint:
    def test_machines_returns_json(self):
        import bridge
        handler = _make_handler()
        handler.handle_machines_endpoint = bridge.Handler.handle_machines_endpoint.__get__(handler, bridge.Handler)
        fake_machines = {"local": {"workers": [], "health": "ok"}}
        with patch.object(bridge, "get_machines", return_value=fake_machines):
            parsed = ParseResult(scheme="", netloc="", path="/machines", params="", query="", fragment="")
            handler.handle_machines_endpoint(parsed)
        handler.send_response.assert_called_with(200)

    def test_machines_passes_from_param(self):
        import bridge
        handler = _make_handler()
        handler.handle_machines_endpoint = bridge.Handler.handle_machines_endpoint.__get__(handler, bridge.Handler)
        with patch.object(bridge, "get_machines", return_value={}) as mock_gm:
            parsed = ParseResult(scheme="", netloc="", path="/machines", params="", query="from=lee", fragment="")
            handler.handle_machines_endpoint(parsed)
        mock_gm.assert_called_once_with(caller_from="lee")

    def test_machines_config_error_returns_500(self):
        import bridge
        handler = _make_handler()
        handler.handle_machines_endpoint = bridge.Handler.handle_machines_endpoint.__get__(handler, bridge.Handler)
        with patch.object(bridge, "get_machines", side_effect=bridge.MachineConfigError("bad config")):
            handler.handle_machines_endpoint(None)
        handler.send_response.assert_called_with(500)


def _make_router():
    import bridge
    sent: list[dict[str, object]] = []

    class FakeTransport:
        def send_text(self, chat_id, text, **kw):
            sent.append({"chat_id": chat_id, "text": text, **kw})
            return {"ok": True, "result": {"message_id": 1}}
        def send_message(self, chat_id, text, **kw):
            return self.send_text(chat_id, text, **kw)
        def set_reaction(self, *a, **kw):
            pass

    router = bridge.CommandRouter(FakeTransport(), bridge.worker_manager)
    return router, sent


# ── /pilot command ────────────────────────────────────────


class TestCmdPilot:
    def test_pilot_no_args_shows_usage(self):
        router, sent = _make_router()
        router.cmd_pilot("", 12345)
        assert len(sent) == 1
        assert "Usage" in sent[0]["text"]

    def test_pilot_calls_pilot_api(self):
        import bridge
        router, sent = _make_router()
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"ok": true}'
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch.object(bridge, "_urlopen", return_value=fake_resp), \
             patch.object(bridge, "get_worker_host", return_value=None):
            router.cmd_pilot("lee", 12345)
        assert len(sent) == 1
        msg = sent[0]["text"]
        assert "Pilot" in msg
        assert "lee" in msg

    def test_pilot_api_failure_reports_error(self):
        import bridge
        import urllib.error
        router, sent = _make_router()
        with patch.object(bridge, "_urlopen", side_effect=urllib.error.URLError("refused")), \
             patch.object(bridge, "get_worker_host", return_value=None):
            router.cmd_pilot("lee", 12345)
        assert len(sent) == 1
        msg = sent[0]["text"]
        assert "error" in msg.lower() or "refused" in msg


# ── /rewind command ───────────────────────────────────────


class TestCmdRewind:
    def test_rewind_no_args_shows_usage(self):
        router, sent = _make_router()
        router.cmd_rewind("", 12345)
        assert len(sent) == 1
        assert "Usage" in sent[0]["text"]

    def test_rewind_generates_token_and_url(self):
        import bridge
        router, sent = _make_router()
        with patch.object(bridge, "_render_transcript_html", return_value="<html>test</html>"), \
             patch.object(bridge, "_beast_serve_deploy", return_value=None):
            router.cmd_rewind("lee", 12345)
        assert len(sent) == 1
        msg = sent[0]["text"]
        assert "Rewind" in msg
        assert "lee" in msg
        assert "token=" in msg


# ── /pr command ───────────────────────────────────────────


class TestCmdPrReview:
    def test_pr_no_args_shows_usage(self):
        router, sent = _make_router()
        router.cmd_pr_review("", 12345)
        assert len(sent) == 1
        assert "Usage" in sent[0]["text"]

    def test_pr_invalid_url_shows_error(self):
        router, sent = _make_router()
        router.cmd_pr_review("not-a-url", 12345)
        assert len(sent) == 1
        assert "Invalid" in sent[0]["text"]

    def test_pr_valid_url_generates_review(self, tmp_path):
        import bridge
        router, sent = _make_router()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        out_path = "/tmp/pr-review-123.html"
        Path(out_path).write_text("<html>review</html>")
        try:
            with patch.object(bridge, "_subprocess_runner") as mock_runner, \
                 patch.object(bridge, "_beast_serve_deploy", return_value="https://serve.example.com/pr-123"):
                mock_runner.run.return_value = mock_result
                router.cmd_pr_review("https://github.com/BasedHardware/omi/pull/123", 12345)
            assert len(sent) >= 1
            last_msg = sent[-1]["text"]
            assert "123" in last_msg
        finally:
            Path(out_path).unlink(missing_ok=True)
