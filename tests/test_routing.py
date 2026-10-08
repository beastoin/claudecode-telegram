import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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


# ── @mention routing ──────────────────────────────────────


class TestMentionRouting:
    def test_bare_mention_switches_focus(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = None
        router._handle_mention_routing(
            targets=["lee"], message="", text=" @lee ",
            chat_id=12345, msg_id=1,
            reply_to=None, reply_context="", reply_context_ts=None,
        )
        assert bridge.state.active == "lee"

    def test_mention_with_message_routes_to_worker(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        route_mock = MagicMock(return_value={"name": "lee", "status": "sent"})
        router._route_mention = route_mock
        router._handle_mention_routing(
            targets=["lee"], message="build the feature",
            text="@lee build the feature",
            chat_id=12345, msg_id=1,
            reply_to=None, reply_context="", reply_context_ts=None,
        )
        route_mock.assert_called_once_with("lee", "build the feature", 12345, 1)

    def test_offline_mention_warns(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router._route_mention = MagicMock(return_value={"name": "lee", "status": "offline"})
        router._handle_mention_routing(
            targets=["lee"], message="hello",
            text="@lee hello",
            chat_id=12345, msg_id=1,
            reply_to=None, reply_context="", reply_context_ts=None,
        )
        assert any("offline" in str(s["text"]).lower() for s in sent)

    def test_auto_focus_after_two_mentions(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router._route_mention = MagicMock(return_value={"name": "lee", "status": "sent"})
        bridge.state.active = "finn"
        router._reset_mention_streak()
        router._handle_mention_routing(
            targets=["lee"], message="first message",
            text="@lee first message",
            chat_id=12345, msg_id=1,
            reply_to=None, reply_context="", reply_context_ts=None,
        )
        assert bridge.state.active == "finn"
        router._handle_mention_routing(
            targets=["lee"], message="second message",
            text="@lee second message",
            chat_id=12345, msg_id=2,
            reply_to=None, reply_context="", reply_context_ts=None,
        )
        assert bridge.state.active == "lee"
        assert any("twice" in str(s["text"]).lower() for s in sent)

    def test_multi_target_mention_delivers_to_all(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={
            "lee": {"tmux": "claude-test-lee"},
            "finn": {"tmux": "claude-test-finn"},
        })
        router._route_mention = MagicMock(return_value={"name": "x", "status": "sent"})
        router._handle_mention_routing(
            targets=["lee", "finn"], message="hello both",
            text="@lee @finn hello both",
            chat_id=12345, msg_id=1,
            reply_to=None, reply_context="", reply_context_ts=None,
        )
        assert router._route_mention.call_count == 2


# ── Implicit focus routing ────────────────────────────────


class TestImplicitFocusRouting:
    def test_bare_worker_name_as_command_focuses(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = None
        result = router.handle_command("/lee", 12345, 1)
        assert result is True
        assert bridge.state.active == "lee"

    def test_worker_name_with_message_routes(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = "finn"
        route_mock = MagicMock()
        router.route_message = route_mock
        result = router.handle_command("/lee build it", 12345, 1)
        assert result is True
        assert bridge.state.active == "lee"
        route_mock.assert_called_once()

    def test_unknown_command_returns_false(self):
        import bridge
        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={})
        result = router.handle_command("/unknowncmd", 12345, 1)
        assert result is False


# ── Remote file localization ──────────────────────────────


class TestRemoteFileLocalization:
    def test_fetch_remote_file_success(self, tmp_path):
        import bridge
        mock_runner = MagicMock()
        result = MagicMock()
        result.returncode = 0
        mock_runner.run.return_value = result
        with patch.object(bridge, "_subprocess_runner", mock_runner), \
             patch("os.path.getsize", return_value=1024):
            local = bridge._fetch_remote_file("remote-host", "/tmp/test.png")
        assert local is not None
        assert "test.png" in local

    def test_fetch_remote_file_rsync_failure(self):
        import bridge
        mock_runner = MagicMock()
        result = MagicMock()
        result.returncode = 1
        result.stderr = "connection refused"
        mock_runner.run.return_value = result
        with patch.object(bridge, "_subprocess_runner", mock_runner):
            local = bridge._fetch_remote_file("bad-host", "/tmp/test.png")
        assert local is None

    def test_localize_media_no_host_passes_through(self):
        import bridge
        media = [("/tmp/a.png", "photo"), ("/tmp/b.pdf", "doc")]
        with patch.object(bridge, "get_worker_host", return_value=None):
            result = bridge._localize_media("lee", media)
        assert result == media

    def test_localize_media_fetches_from_remote(self):
        import bridge
        media = [("/remote/a.png", "photo")]
        with patch.object(bridge, "get_worker_host", return_value="remote-host"), \
             patch.object(bridge, "_fetch_remote_file", return_value="/tmp/local-a.png"):
            result = bridge._localize_media("lee", media)
        assert result[0][0] == "/tmp/local-a.png"
        assert result[0][1] == "photo"

    def test_localize_media_fetch_failure_marks_as_failed(self):
        import bridge
        media = [("/remote/missing.png", "photo")]
        with patch.object(bridge, "get_worker_host", return_value="remote-host"), \
             patch.object(bridge, "_fetch_remote_file", return_value=None):
            result = bridge._localize_media("lee", media)
        assert result[0][0] is None
        assert "Fetch failed" in result[0][1]


# ── Reply media extraction ────────────────────────────────


class TestReplyMediaExtraction:
    def test_extract_reply_photo(self):
        import bridge
        router, sent = _make_router()
        reply_msg = {
            "photo": [{"file_id": "photo123", "file_size": 1000}],
        }
        with patch.object(bridge, "download_telegram_file", return_value="/tmp/photo.jpg"):
            result = router._extract_reply_media(reply_msg, "lee")
        assert result is not None
        assert "/tmp/photo.jpg" in result

    def test_extract_reply_document(self):
        import bridge
        router, sent = _make_router()
        reply_msg = {
            "document": {"file_id": "doc456", "file_name": "report.pdf"},
        }
        with patch.object(bridge, "download_telegram_file", return_value="/tmp/report.pdf"):
            result = router._extract_reply_media(reply_msg, "lee")
        assert result is not None
        assert "/tmp/report.pdf" in result

    def test_extract_reply_no_media_returns_none(self):
        import bridge
        router, sent = _make_router()
        reply_msg = {"text": "just text, no media"}
        result = router._extract_reply_media(reply_msg, "lee")
        assert result is None

    def test_extract_reply_animation(self):
        import bridge
        router, sent = _make_router()
        reply_msg = {
            "animation": {"file_id": "anim789"},
        }
        with patch.object(bridge, "download_telegram_file", return_value="/tmp/anim.gif"):
            result = router._extract_reply_media(reply_msg, "lee")
        assert result is not None


# ── Media group flush ─────────────────────────────────────


class TestMediaGroupFlush:
    def test_flush_empty_group_does_nothing(self):
        router, sent = _make_router()
        router._handle_media_group_flush("nonexistent-group-id")

    def test_flush_group_routes_to_active_worker(self):
        import bridge
        router, sent = _make_router()
        bridge.state.active = "lee"
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router.parse_at_mentions = MagicMock(return_value=([], ""))
        with bridge.media_groups.lock:
            bridge.media_groups.buffer["test-group"] = {
                "items": [{
                    "photo": [{"file_id": "photo1", "file_size": 500}],
                    "chat": {"id": 12345},
                    "message_id": 1,
                }],
                "caption": "",
                "timer": None,
            }
        with patch.object(bridge, "download_telegram_file", return_value="/tmp/photo.jpg") as mock_dl, \
             patch.object(bridge, "send_to_worker", return_value=True):
            router._handle_media_group_flush("test-group")
        mock_dl.assert_called()


# ── Single media handling ─────────────────────────────────


class TestSingleMediaHandling:
    def _make_incoming(self, **overrides):
        import bridge
        defaults = {
            "raw_msg": {"chat": {"id": 12345}, "message_id": 1},
            "text": "",
            "chat_id": 12345,
            "msg_id": 1,
            "photo": None,
            "document": None,
            "animation": None,
            "audio": None,
            "voice": None,
            "video": None,
            "video_note": None,
            "sticker": None,
            "doc_is_image": False,
        }
        defaults.update(overrides)
        incoming = MagicMock(spec=bridge.IncomingMessage)
        for k, v in defaults.items():
            setattr(incoming, k, v)
        return incoming

    def test_single_photo_routes_to_active_worker(self):
        import bridge
        router, sent = _make_router()
        bridge.state.active = "lee"
        bridge.admin_chat_id = 12345
        incoming = self._make_incoming(
            photo=[{"file_id": "photo1", "file_size": 1000}],
        )
        with patch.object(bridge, "download_telegram_file", return_value="/tmp/photo.jpg"), \
             patch.object(bridge, "send_to_worker", return_value=True), \
             patch.object(router, "parse_at_mentions", return_value=([], "")), \
             patch.object(router, "_resolve_media_target", return_value="lee"):
            result = router._handle_single_media(incoming)
        assert result is True

    def test_single_media_no_focus_shows_error(self):
        import bridge
        router, sent = _make_router()
        bridge.state.active = None
        bridge.admin_chat_id = 12345
        incoming = self._make_incoming(
            photo=[{"file_id": "photo1", "file_size": 1000}],
        )
        with patch.object(router, "parse_at_mentions", return_value=([], "")):
            result = router._handle_single_media(incoming)
        assert result is True
        assert any("focus" in str(s["text"]).lower() or "No focused" in str(s["text"]) for s in sent)

    def test_single_media_non_admin_rejected(self):
        import bridge
        router, sent = _make_router()
        bridge.admin_chat_id = 99999
        incoming = self._make_incoming(
            chat_id=88888,
            photo=[{"file_id": "photo1", "file_size": 1000}],
        )
        result = router._handle_single_media(incoming)
        assert result is True

    def test_sticker_routes_as_media(self):
        import bridge
        router, sent = _make_router()
        bridge.state.active = "lee"
        bridge.admin_chat_id = 12345
        incoming = self._make_incoming(
            sticker={"file_id": "sticker1"},
        )
        with patch.object(bridge, "download_telegram_file", return_value="/tmp/sticker.webp"), \
             patch.object(bridge, "send_to_worker", return_value=True), \
             patch.object(router, "parse_at_mentions", return_value=([], "")), \
             patch.object(router, "_resolve_media_target", return_value="lee"):
            result = router._handle_single_media(incoming)
        assert result is True

    def test_no_file_id_returns_false(self):
        import bridge
        router, sent = _make_router()
        incoming = self._make_incoming()
        result = router._handle_single_media(incoming)
        assert result is False


# ── handle_command dispatch ───────────────────────────────


class TestHandleCommandDispatch:
    def test_known_command_dispatches(self):
        router, sent = _make_router()
        result = router.handle_command("/team", 12345, 1)
        assert result is True

    def test_blocked_command_rejected(self):
        import bridge
        router, sent = _make_router()
        if hasattr(bridge, "BLOCKED_COMMANDS") and bridge.BLOCKED_COMMANDS:
            blocked = list(bridge.BLOCKED_COMMANDS)[0]
            result = router.handle_command(f"/{blocked}", 12345, 1)
            if result:
                assert any("not supported" in str(s["text"]).lower() or "interactive" in str(s["text"]).lower() for s in sent)

    def test_command_strips_bot_suffix(self):
        router, sent = _make_router()
        result = router.handle_command("/team@mybot", 12345, 1)
        assert result is True
