"""Tests for teleport unification — removing teleback, safe sync, checkin-triggered teleport.

TDD RED phase: these tests describe the target behavior.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path
from contextlib import ExitStack


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


def _teleport_mocks(stack, **overrides):
    """Set up common mocks for do_teleport tests. Returns dict of mocks."""
    import bridge

    defaults = {
        "get_registered": {"hal": {"tmux": "claude-test-hal", "backend": "claude"}},
        "source_host": None,
        "source_cwd": "/home/claude/project",
        "stop_session_id": "sess-123",
        "sync_wd_ok": True,
        "sync_shared_warnings": [],
        "hook_warnings": [],
        "start_ok": True,
    }
    defaults.update(overrides)

    stack.enter_context(patch.object(
        bridge.worker_manager, "get_registered_sessions",
        return_value=defaults["get_registered"],
    ))
    stack.enter_context(patch("bridge.get_worker_host", return_value=defaults["source_host"]))
    stack.enter_context(patch("bridge.get_claude_session_cwd", return_value=defaults["source_cwd"]))
    stack.enter_context(patch("bridge.stop_worker_for_teleport", return_value=defaults["stop_session_id"]))
    stack.enter_context(patch("bridge.sync_working_directory", return_value=defaults["sync_wd_ok"]))
    stack.enter_context(patch("bridge.sync_session_transcript"))
    m_shared = stack.enter_context(patch("bridge.sync_shared_repos", return_value=defaults["sync_shared_warnings"]))
    m_hooks = stack.enter_context(patch("bridge.install_hooks_on_target", return_value=defaults["hook_warnings"]))
    stack.enter_context(patch("bridge.start_worker_on_target", return_value=defaults["start_ok"]))
    stack.enter_context(patch("bridge._remote_run"))
    stack.enter_context(patch("bridge.save_claude_session_cwd"))
    stack.enter_context(patch("bridge.clear_claude_session_id"))
    stack.enter_context(patch("bridge._ensure_workspace_trusted_remote"))
    stack.enter_context(patch("bridge.ensure_session_dir"))
    stack.enter_context(patch("bridge._atomic_write"))
    stack.enter_context(patch("bridge.teleport_notify"))
    stack.enter_context(patch("bridge.get_worker_backend", return_value="claude"))
    stack.enter_context(patch("bridge._registry_update_host"))
    stack.enter_context(patch.object(bridge._clock, "sleep"))
    m_send = stack.enter_context(patch.object(bridge.worker_manager, "send"))
    stack.enter_context(patch("bridge._build_teleport_context", return_value="ctx"))
    stack.enter_context(patch.object(bridge.worker_manager, "_build_welcome", return_value="welcome msg"))

    return {"sync_shared": m_shared, "hooks": m_hooks, "send": m_send}


def _checkin_mocks(stack, *, current_host=None, registered=None, **overrides):
    """Set up common mocks for checkin tests."""
    import bridge

    if registered is None:
        registered = {"hal": {"tmux": "claude-test-hal", "backend": "claude", "host": current_host}}

    stack.enter_context(patch.object(
        bridge.worker_manager, "get_registered_sessions", return_value=registered,
    ))
    stack.enter_context(patch("bridge.get_worker_host", return_value=current_host))
    stack.enter_context(patch("bridge.get_worker_backend", return_value="claude"))
    stack.enter_context(patch("bridge.get_backend", return_value=MagicMock()))
    stack.enter_context(patch.object(bridge.worker_manager, "_build_welcome", return_value="welcome"))
    stack.enter_context(patch.object(bridge, "claudecode", MagicMock()))


class FakeCheckinHandler:
    def __init__(self):
        self.response_code = None
        self.response_body = ""

    def _send_text(self, code, text):
        self.response_code = code
        self.response_body = text

    def _send_json(self, code, data):
        self.response_code = code
        self.response_body = data


# ---------------------------------------------------------------------------
# 1. Teleback removed
# ---------------------------------------------------------------------------


def test_teleback_command_removed():
    """cmd_teleback should no longer exist after unification."""
    import bridge

    assert not hasattr(bridge, "cmd_teleback"), "cmd_teleback should be removed"


def test_teleback_references_removed():
    """No cmd_teleback references should remain in bridge.py."""
    src = (Path(__file__).resolve().parent.parent / "bridge.py").read_text()
    assert "cmd_teleback" not in src, "cmd_teleback references should be removed"


# ---------------------------------------------------------------------------
# 2. Unified do_teleport — no is_teleback flag
# ---------------------------------------------------------------------------


def test_do_teleport_no_is_teleback_param():
    """do_teleport should not accept is_teleback parameter."""
    import inspect
    import bridge

    sig = inspect.signature(bridge.do_teleport)
    assert "is_teleback" not in sig.parameters, "is_teleback param should be removed"


def test_do_teleport_always_syncs_shared_repos():
    """Teleport always syncs team config (no teleback skip)."""
    import bridge

    with ExitStack() as stack:
        mocks = _teleport_mocks(stack)
        bridge.do_teleport("hal", "macmini", "/Users/beastoinagents/project", False, 123, bridge.worker_manager)
        mocks["sync_shared"].assert_called_once()
        mocks["hooks"].assert_called_once()


def test_do_teleport_always_sends_welcome():
    """Teleport always sends welcome + context."""
    import bridge

    with ExitStack() as stack:
        mocks = _teleport_mocks(stack)
        bridge.do_teleport("hal", "macmini", "/Users/beastoinagents/project", False, 123, bridge.worker_manager)
        assert mocks["send"].call_count >= 1, "Must send welcome"


# ---------------------------------------------------------------------------
# 3. Registry — no home_host/home_cwd, just host
# ---------------------------------------------------------------------------


def test_registry_update_host_replaces_teleport_fields(tmp_path):
    """_registry_update_host sets host, removes home_host/home_cwd."""
    import bridge

    reg_file = tmp_path / "workers.json"
    reg_file.write_text(json.dumps({
        "workers": {
            "hal": {
                "tmux": "claude-test-hal",
                "host": None,
                "home_host": "old-home",
                "home_cwd": "/old/cwd",
            }
        }
    }))

    with patch.object(bridge, "WORKER_REGISTRY_FILE", reg_file), \
         patch.object(bridge, "NODE_DIR", tmp_path):
        bridge._registry_update_host("hal", host="macmini")

    data = json.loads(reg_file.read_text())
    assert data["workers"]["hal"]["host"] == "macmini"
    assert "home_host" not in data["workers"]["hal"]
    assert "home_cwd" not in data["workers"]["hal"]


# ---------------------------------------------------------------------------
# 4. Safe rsync — --update instead of --delete
# ---------------------------------------------------------------------------


def test_rsync_uses_update_not_delete():
    """rsync_working_directory uses --update, not --delete."""
    import bridge

    with ExitStack() as stack:
        stack.enter_context(patch("bridge._remote_run"))
        mock_run = stack.enter_context(patch.object(
            bridge._subprocess_runner, "run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ))
        bridge.rsync_working_directory("/src", "/dst", source_host=None, target_host="mac")

    rsync_args = mock_run.call_args[0][0]
    assert "--update" in rsync_args, f"rsync must use --update: {rsync_args}"
    assert "--delete" not in rsync_args, f"rsync must NOT use --delete: {rsync_args}"


def test_transcript_rsync_uses_update():
    """Session transcript sync uses --update."""
    import bridge

    rsync_calls = []

    def capture_run(cmd, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "rsync":
            rsync_calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    with ExitStack() as stack:
        stack.enter_context(patch.object(bridge._subprocess_runner, "run", side_effect=capture_run))
        stack.enter_context(patch("os.makedirs"))
        bridge.sync_session_transcript("sess-123", "/src/proj", "/dst/proj", source_host=None, target_host="mac")

    assert len(rsync_calls) >= 1, "Should have rsync calls"
    for cmd in rsync_calls:
        assert "--update" in cmd, f"Transcript rsync must use --update: {cmd}"


# ---------------------------------------------------------------------------
# 5. Checkin with host param — breadcrumbs + confirm teleport
# ---------------------------------------------------------------------------


def test_checkin_host_mismatch_returns_breadcrumbs():
    """GET /checkin?name=hal&host=macmini returns breadcrumbs with confirm param."""
    from urllib.parse import urlparse
    import bridge

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host=None)
        stack.enter_context(patch("bridge.get_claude_session_cwd", return_value="/home/claude/project"))
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={}))
        stack.enter_context(patch("bridge._remap_path", return_value="/remapped/path"))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert handler.response_code == 200
    assert "HOST MISMATCH" in handler.response_body
    assert "confirm=1" in handler.response_body
    assert "macmini" in handler.response_body


def test_checkin_same_host_no_teleport():
    """GET /checkin?name=hal&host=macmini when already on resolved macmini host — no teleport."""
    from urllib.parse import urlparse
    import bridge

    teleport_calls = []
    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini")

    mock_machine = MagicMock()
    mock_machine.ssh_target = "beastoin-agents-f1-mac-mini"

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host="beastoin-agents-f1-mac-mini")
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={"macmini": mock_machine}))
        stack.enter_context(patch("bridge.do_teleport", side_effect=lambda *a, **kw: teleport_calls.append(1)))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert len(teleport_calls) == 0
    assert handler.response_code == 200


def test_checkin_host_mismatch_breadcrumb_is_checkin_url():
    """Breadcrumbs include a checkin URL with confirm=1, not POST /teleport."""
    from urllib.parse import urlparse
    import bridge

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host="vps-local")
        stack.enter_context(patch("bridge.get_claude_session_cwd", return_value="/home/claude/project"))
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={}))
        stack.enter_context(patch("bridge._remap_path", return_value="/Users/beastoinagents/project"))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert handler.response_code == 200
    assert "/checkin?" in handler.response_body
    assert "confirm=1" in handler.response_body
    assert "host=macmini" in handler.response_body
    assert "vps-local" in handler.response_body


def test_checkin_confirm_teleports_then_checkin():
    """GET /checkin?name=hal&host=macmini&confirm=1 teleports then returns welcome."""
    from urllib.parse import urlparse
    import bridge

    teleport_calls = []

    def mock_teleport(name, target_host, target_cwd, full, chat_id, workers):
        teleport_calls.append({"name": name, "target": target_host})

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini&confirm=1")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host=None)
        stack.enter_context(patch("bridge.get_claude_session_cwd", return_value="/home/claude/project"))
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={}))
        stack.enter_context(patch("bridge.run_teleport_preflight", return_value=[]))
        stack.enter_context(patch("bridge._remote_run", return_value=MagicMock(returncode=0)))
        stack.enter_context(patch("bridge.get_manager_chat_id", return_value=123))
        stack.enter_context(patch("bridge.send_telegram_message"))
        stack.enter_context(patch("bridge.do_teleport", side_effect=mock_teleport))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert len(teleport_calls) == 1
    assert teleport_calls[0]["name"] == "hal"
    assert teleport_calls[0]["target"] == "macmini"
    assert handler.response_code == 200


def test_checkin_confirm_preflight_failure():
    """GET /checkin with confirm=1 returns error when preflight fails."""
    from urllib.parse import urlparse
    import bridge

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini&confirm=1")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host=None)
        stack.enter_context(patch("bridge.get_claude_session_cwd", return_value="/home/claude/project"))
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={}))
        stack.enter_context(patch("bridge.run_teleport_preflight", return_value=["rsync not found"]))
        stack.enter_context(patch("bridge._remote_run", return_value=MagicMock(returncode=0)))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert handler.response_code == 412


def test_checkin_confirm_unreachable():
    """GET /checkin with confirm=1 returns error when target unreachable."""
    from urllib.parse import urlparse
    import bridge

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini&confirm=1")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host=None)
        stack.enter_context(patch("bridge.get_claude_session_cwd", return_value="/home/claude/project"))
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={}))
        stack.enter_context(patch("bridge._remote_run", return_value=MagicMock(returncode=1)))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert handler.response_code == 502


def test_checkin_without_host_unchanged():
    """GET /checkin?name=hal (no host) works as before."""
    from urllib.parse import urlparse
    import bridge

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host=None)
        bridge.handle_checkin_endpoint(handler, parsed)

    assert handler.response_code == 200
    assert handler.response_body == "welcome"


def test_checkin_host_resolves_machine_catalog():
    """Checkin host param resolves friendly names via machine catalog in breadcrumbs."""
    from urllib.parse import urlparse
    import bridge

    mock_machine = MagicMock()
    mock_machine.ssh_target = "beastoin-agents-f1-mac-mini"

    handler = FakeCheckinHandler()
    parsed = urlparse("/checkin?name=hal&host=macmini")

    with ExitStack() as stack:
        _checkin_mocks(stack, current_host=None)
        stack.enter_context(patch("bridge.get_claude_session_cwd", return_value="/home/claude/project"))
        stack.enter_context(patch("bridge.get_machine_catalog", return_value={"macmini": mock_machine}))
        stack.enter_context(patch("bridge._remap_path", return_value="/Users/beastoinagents/project"))
        bridge.handle_checkin_endpoint(handler, parsed)

    assert handler.response_code == 200
    assert "HOST MISMATCH" in handler.response_body
    assert "beastoin-agents-f1-mac-mini" in handler.response_body
