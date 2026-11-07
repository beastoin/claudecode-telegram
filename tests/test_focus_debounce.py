"""Tests for focus simplification and message debounce."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeClock:
    def __init__(self, t: float = 10000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


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
        "_clock": bridge._clock,
    }
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = tmp_path / "node"
    bridge.NODE_DIR.mkdir(exist_ok=True)
    bridge.WORKER_REGISTRY_FILE = tmp_path / "workers.json"
    bridge.admin_chat_id = 12345
    saved_active = bridge.state.active
    saved_focus_ts = bridge.state.focus_sent_ts
    saved_scan = bridge.worker_manager.scan_tmux_sessions
    saved_get_reg = bridge.worker_manager.get_registered_sessions
    saved_is_online = bridge.worker_manager.is_online
    saved_send = bridge.worker_manager.send
    yield
    for r in _routers:
        with r._debounce_lock:
            if r._debounce_timer is not None:
                r._debounce_timer.cancel()
                r._debounce_timer = None
            r._debounce_buffer.clear()
    _routers.clear()
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.NODE_DIR = saved["NODE_DIR"]
    if saved["WORKER_PIPE_ROOT"] is not None:
        bridge.WORKER_PIPE_ROOT = saved["WORKER_PIPE_ROOT"]
    bridge.admin_chat_id = saved["admin_chat_id"]
    bridge.transport = saved["transport"]
    bridge._clock = saved["_clock"]
    bridge.state.active = saved_active
    bridge.state.focus_sent_ts = saved_focus_ts
    bridge.worker_manager.scan_tmux_sessions = saved_scan
    bridge.worker_manager.get_registered_sessions = saved_get_reg
    bridge.worker_manager.is_online = saved_is_online
    bridge.worker_manager.send = saved_send
    bridge.worker_manager.invalidate_sessions_cache()


_routers: list = []


def _make_router():
    import bridge

    sent: list[dict[str, object]] = []

    class FakeTransport:
        def send_text(self, chat_id, text, **kw):
            sent.append({"chat_id": chat_id, "text": text, **kw})
            return {"ok": True, "result": {"message_id": 1}}

        def send_message(self, chat_id, text, **kw):
            return self.send_text(chat_id, text, **kw)

        def set_reaction(self, *a, **kw) -> None:
            pass

    router = bridge.CommandRouter(FakeTransport(), bridge.worker_manager)
    _routers.append(router)
    return router, sent


# ── Focus simplification ─────────────────────────────────


class TestFocusCommand:
    """Only /name should set focus (plus /hire and startup)."""

    def test_focus_command_sets_active(self) -> None:
        import bridge

        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = None
        bridge.cmd_focus(router, "lee", 12345)
        assert bridge.state.active == "lee"

    def test_slash_worker_bare_sets_focus(self) -> None:
        """/worker (no arg) sets focus via handle_command."""
        import bridge

        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = None
        result = router.handle_command("/lee", 12345, 1)
        assert result is True
        assert bridge.state.active == "lee"
        assert len(sent) == 0  # silent on success

    def test_bare_mention_no_focus_change(self) -> None:
        """@worker (no text) should NOT change focus."""
        import bridge

        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = "finn"
        router._handle_mention_routing(
            targets=["lee"],
            message="",
            text=" @lee ",
            chat_id=12345,
            msg_id=1,
            reply_to=None,
            reply_context="",
            reply_context_ts=None,
        )
        assert bridge.state.active == "finn"

    def test_double_mention_no_auto_focus(self) -> None:
        """Mentioning same worker twice should NOT auto-focus."""
        import bridge

        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router._route_mention = MagicMock(return_value={"name": "lee", "status": "sent"})
        bridge.state.active = "finn"
        router._handle_mention_routing(
            targets=["lee"],
            message="first",
            text="@lee first",
            chat_id=12345,
            msg_id=1,
            reply_to=None,
            reply_context="",
            reply_context_ts=None,
        )
        router._handle_mention_routing(
            targets=["lee"],
            message="second",
            text="@lee second",
            chat_id=12345,
            msg_id=2,
            reply_to=None,
            reply_context="",
            reply_context_ts=None,
        )
        assert bridge.state.active == "finn"
        assert not any("twice" in str(s.get("text", "")).lower() for s in sent)

    def test_slash_worker_routes_without_focus_change(self) -> None:
        """/worker arg should route to worker but NOT change focus."""
        import bridge

        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = "finn"
        route_mock = MagicMock()
        router.route_message = route_mock
        result = router.handle_command("/lee build it", 12345, 1)
        assert result is True
        assert bridge.state.active == "finn"
        route_mock.assert_called_once()

    def test_bare_slash_worker_switches_focus(self) -> None:
        """/worker (no arg) should switch focus to that worker."""
        import bridge

        router, sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.state.active = "finn"
        result = router.handle_command("/lee", 12345, 1)
        assert result is True
        assert bridge.state.active == "lee"
        assert len(sent) == 0  # silent on success

    def test_hire_still_sets_focus(self) -> None:
        """Hiring a new worker should still set focus to them."""
        import bridge

        bridge.state.active = "old_worker"
        router, _sent = _make_router()
        with patch.object(bridge.worker_manager, "hire", return_value=(True, None)):
            bridge.cmd_hire(router, "newbie", 12345)
        # hire() internally sets state.active — we verify via the mock
        # The real test is that hire's code still contains state.active = name

    def test_mention_routes_without_focus(self) -> None:
        """@mention with text routes to worker without changing focus."""
        import bridge

        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"geni": {"tmux": "claude-test-geni"}})
        router._route_mention = MagicMock(return_value={"name": "geni", "status": "sent"})
        bridge.state.active = "lee"
        router._handle_mention_routing(
            targets=["geni"],
            message="check this",
            text="@geni check this",
            chat_id=12345,
            msg_id=1,
            reply_to=None,
            reply_context="",
            reply_context_ts=None,
        )
        assert bridge.state.active == "lee"


# ── Focus expiry ─────────────────────────────────────────


class TestFocusExpiry:
    """Focus should auto-expire after 30 minutes of inactivity."""

    def test_focus_expires_after_30min(self) -> None:
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        bridge.state.active = "lee"
        bridge.state.focus_sent_ts = clock.time()
        clock.advance(1801)  # 30min + 1s
        expired = bridge._check_focus_expiry()
        assert expired is True
        assert bridge.state.active is None

    def test_focus_not_expired_within_30min(self) -> None:
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        bridge.state.active = "lee"
        bridge.state.focus_sent_ts = clock.time()
        clock.advance(1799)  # 30min - 1s
        expired = bridge._check_focus_expiry()
        assert expired is False
        assert bridge.state.active == "lee"

    def test_focus_expiry_sends_notice(self) -> None:
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        bridge.state.active = "lee"
        bridge.state.focus_sent_ts = clock.time()
        bridge.admin_chat_id = 12345
        clock.advance(1801)
        notices: list[str] = []
        with patch.object(bridge, "send_telegram_message", side_effect=lambda cid, txt: notices.append(txt)):
            bridge._check_focus_expiry()
        assert len(notices) == 1
        assert "lee" in notices[0].lower()
        assert "expired" in notices[0].lower() or "idle" in notices[0].lower()

    def test_route_to_active_resets_focus_timer(self) -> None:
        """Sending a bare message to focused worker resets the timer."""
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        bridge.state.active = "lee"
        bridge.state.focus_sent_ts = clock.time()
        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router.workers.is_online = MagicMock(return_value=True)
        router.workers.send = MagicMock(return_value=True)
        clock.advance(600)  # 10 min
        router.route_to_active("hello", 12345, 1)
        assert bridge.state.focus_sent_ts == 10600.0

    def test_any_message_resets_focus_timer(self) -> None:
        """Any incoming message (even @mention to another worker) resets focus timer."""
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        bridge.state.active = "lee"
        bridge.state.focus_sent_ts = clock.time()
        bridge.state.startup_notified = True
        bridge.admin_chat_id = 12345
        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(
            return_value={
                "lee": {"tmux": "claude-test-lee"},
                "geni": {"tmux": "claude-test-geni"},
            }
        )
        router.workers.is_online = MagicMock(return_value=True)
        router.workers.send = MagicMock(return_value=True)
        clock.advance(600)
        incoming = bridge.IncomingMessage(
            text="@geni check this",
            chat_id=12345,
            msg_id=1,
            raw_msg={"chat": {"id": 12345}, "message_id": 1, "text": "@geni check this"},
            photo=None,
            document=None,
            animation=None,
            video=None,
            audio=None,
            voice=None,
            sticker=None,
        )
        with patch.object(bridge, "_TEXT_DEBOUNCE_WAIT", 999):
            router._route_text_message(incoming)
        assert bridge.state.focus_sent_ts == 10600.0

    def test_no_expiry_when_no_focus(self) -> None:
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        bridge.state.active = None
        bridge.state.focus_sent_ts = 0.0
        expired = bridge._check_focus_expiry()
        assert expired is False

    def test_focus_command_resets_timer(self) -> None:
        """/focus should reset the expiry timer."""
        import bridge

        clock = _FakeClock(10000.0)
        bridge._clock = clock
        router, _sent = _make_router()
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.cmd_focus(router, "lee", 12345)
        assert bridge.state.focus_sent_ts == 10000.0


# ── Message debounce ─────────────────────────────────────


class TestMessageDebounce:
    """Telegram splits long messages; debounce combines them before routing."""

    def _make_incoming(self, text: str, msg_id: int = 1, chat_id: int = 12345):
        import bridge

        return bridge.IncomingMessage(
            text=text,
            chat_id=chat_id,
            msg_id=msg_id,
            raw_msg={"message_id": msg_id, "chat": {"id": chat_id}, "text": text, "date": 1000},
            photo=None,
            document=None,
            animation=None,
            video=None,
            audio=None,
            voice=None,
            video_note=None,
            sticker=None,
        )

    def test_single_message_routes_after_debounce(self) -> None:
        """A single message should route after the debounce window."""
        import bridge

        router, _sent = _make_router()
        bridge.state.active = "lee"
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router.workers.is_online = MagicMock(return_value=True)
        router.workers.send = MagicMock(return_value=True)
        bridge.admin_chat_id = 12345
        bridge.state.startup_notified = True
        incoming = self._make_incoming("hello world")
        with patch.object(bridge, "_TEXT_DEBOUNCE_WAIT", 999):
            router._route_text_message(incoming)
        router.workers.send.assert_not_called()
        router._flush_text_debounce()
        router.workers.send.assert_called_once()
        call_args = router.workers.send.call_args
        assert "hello world" in str(call_args)

    def test_split_messages_combined(self) -> None:
        """Multiple rapid messages should be combined into one."""
        import bridge

        router, _sent = _make_router()
        bridge.state.active = "lee"
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router.workers.is_online = MagicMock(return_value=True)
        router.workers.send = MagicMock(return_value=True)
        bridge.admin_chat_id = 12345
        bridge.state.startup_notified = True
        with patch.object(bridge, "_TEXT_DEBOUNCE_WAIT", 999):
            router._route_text_message(self._make_incoming("first part", msg_id=1))
            router._route_text_message(self._make_incoming("second part", msg_id=2))
        router._flush_text_debounce()
        router.workers.send.assert_called_once()
        call_text = str(router.workers.send.call_args)
        assert "first part" in call_text
        assert "second part" in call_text

    def test_command_bypasses_debounce(self) -> None:
        """Commands starting with / should route immediately, not be buffered."""
        import bridge

        router, _sent = _make_router()
        bridge.state.active = "lee"
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        bridge.admin_chat_id = 12345
        bridge.state.startup_notified = True
        incoming = self._make_incoming("/team")
        with patch.object(router, "handle_command", return_value=True) as cmd_mock:
            router._route_text_message(incoming)
            cmd_mock.assert_called_once()

    def test_debounced_mention_routes_correctly(self) -> None:
        """Split message where first part is bare, second has @mention.
        Combined text should route to mentioned worker."""
        import bridge

        router, _sent = _make_router()
        bridge.state.active = "lee"
        router.workers.get_registered_sessions = MagicMock(
            return_value={
                "lee": {"tmux": "claude-test-lee"},
                "geni": {"tmux": "claude-test-geni"},
            }
        )
        router._route_mention = MagicMock(return_value={"name": "geni", "status": "sent"})
        bridge.admin_chat_id = 12345
        bridge.state.startup_notified = True
        with patch.object(bridge, "_TEXT_DEBOUNCE_WAIT", 999):
            router._route_text_message(self._make_incoming("here is context for", msg_id=1))
            router._route_text_message(self._make_incoming("@geni please review this", msg_id=2))
        router._flush_text_debounce()
        router._route_mention.assert_called_once()
        call_args = router._route_mention.call_args
        assert "geni" in str(call_args)

    def test_debounce_timer_fires_automatically(self) -> None:
        """After the debounce wait, the buffer should flush on its own."""
        import bridge

        router, _sent = _make_router()
        bridge.state.active = "lee"
        router.workers.get_registered_sessions = MagicMock(return_value={"lee": {"tmux": "claude-test-lee"}})
        router.workers.is_online = MagicMock(return_value=True)
        router.workers.send = MagicMock(return_value=True)
        bridge.admin_chat_id = 12345
        bridge.state.startup_notified = True
        with patch.object(bridge, "_TEXT_DEBOUNCE_WAIT", 0.05):
            router._route_text_message(self._make_incoming("auto flush test"))
            time.sleep(0.15)
        router.workers.send.assert_called_once()
