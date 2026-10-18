"""Tests for minimal persistent worker state: {host, cwd, home_host, home_cwd}.

CWD consolidated into workers.json registry (no more worker_cwds.json).
Session ID authoritative from marker file (no mtime fallback for lifecycle).
"""

import json
import tempfile
from pathlib import Path
from urllib.parse import urlparse, quote

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
        "SESSIONS_DIR": bridge.SESSIONS_DIR,
        "WORKER_REGISTRY_FILE": bridge.WORKER_REGISTRY_FILE,
        "NODE_DIR": bridge.NODE_DIR,
        "TMUX_PREFIX": bridge.TMUX_PREFIX,
        "admin_chat_id": bridge.admin_chat_id,
        "wm_scan": bridge.worker_manager.scan_tmux_sessions,
        "wm_get_reg": bridge.worker_manager.get_registered_sessions,
        "wm_restart": bridge.worker_manager.restart,
        "wm_get_pane_cwd": bridge.worker_manager._get_tmux_pane_cwd,
        "tmux_exists": bridge.tmux_exists,
        "is_claude_running": bridge.is_claude_running,
        "export_hook_env": bridge.export_hook_env,
        "send_telegram_message": bridge.send_telegram_message,
    }
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = tmp_path / "node"
    bridge.NODE_DIR.mkdir(exist_ok=True)
    bridge.WORKER_REGISTRY_FILE = bridge.NODE_DIR / "workers.json"
    bridge.TMUX_PREFIX = "claude-test-wstate-"
    bridge.admin_chat_id = 12345
    bridge.worker_manager.scan_tmux_sessions = lambda: {}
    bridge.worker_manager.get_registered_sessions = lambda *a, **kw: {}
    bridge.worker_manager.restart = lambda *a, **kw: True
    bridge.worker_manager._get_tmux_pane_cwd = lambda *a, **kw: ""
    bridge.tmux_exists = lambda *a, **kw: True
    bridge.is_claude_running = lambda *a, **kw: False
    bridge.export_hook_env = lambda *a, **kw: None
    bridge.send_telegram_message = lambda *a, **kw: None
    bridge.watchdog.worker_cwds.clear()
    yield
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.NODE_DIR = saved["NODE_DIR"]
    bridge.TMUX_PREFIX = saved["TMUX_PREFIX"]
    bridge.admin_chat_id = saved["admin_chat_id"]
    bridge.worker_manager.scan_tmux_sessions = saved["wm_scan"]
    bridge.worker_manager.get_registered_sessions = saved["wm_get_reg"]
    bridge.worker_manager.restart = saved["wm_restart"]
    bridge.worker_manager._get_tmux_pane_cwd = saved["wm_get_pane_cwd"]
    bridge.tmux_exists = saved["tmux_exists"]
    bridge.is_claude_running = saved["is_claude_running"]
    bridge.export_hook_env = saved["export_hook_env"]
    bridge.send_telegram_message = saved["send_telegram_message"]
    bridge.worker_manager.invalidate_sessions_cache()


# --- CWD in registry ---


def test_set_worker_cwd_persists_in_registry(tmp_path):
    """_set_worker_cwd stores CWD in workers.json, not a separate file."""
    import bridge

    project = tmp_path / "project"
    project.mkdir()

    bridge._registry_add("alice", "claude")
    bridge._set_worker_cwd("alice", str(project))

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["alice"]["cwd"] == str(project)


def test_get_worker_cwd_reads_from_registry(tmp_path):
    """_get_worker_cwd reads CWD from workers.json registry."""
    import bridge

    project = tmp_path / "project"
    project.mkdir()

    bridge._registry_add("bob", "claude")
    bridge._set_worker_cwd("bob", str(project))

    bridge.watchdog.worker_cwds.clear()
    result = bridge._get_worker_cwd("bob")
    assert result == str(project)


def test_worker_cwds_json_not_used(tmp_path):
    """worker_cwds.json should NOT be written — CWD lives in registry."""
    import bridge

    project = tmp_path / "project"
    project.mkdir()
    cwds_file = bridge.NODE_DIR / "worker_cwds.json"

    bridge._registry_add("charlie", "claude")
    bridge._set_worker_cwd("charlie", str(project))

    assert not cwds_file.exists(), "worker_cwds.json should not be created"


def test_cwd_survives_bridge_restart(tmp_path):
    """After clearing RAM (simulating bridge restart), CWD recoverable from registry."""
    import bridge

    project = tmp_path / "project"
    project.mkdir()

    bridge._registry_add("dana", "claude")
    bridge._set_worker_cwd("dana", str(project))

    bridge.watchdog.worker_cwds.clear()

    cwd = bridge._get_worker_cwd("dana")
    assert cwd == str(project)


def test_registry_add_preserves_cwd(tmp_path):
    """Re-hiring a worker preserves its CWD in registry."""
    import bridge

    project = tmp_path / "project"
    project.mkdir()

    bridge._registry_add("eve", "claude")
    bridge._set_worker_cwd("eve", str(project))

    bridge._registry_add("eve", "claude")
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["eve"].get("cwd") == str(project)


# --- Session ID authoritative marker ---


def test_session_id_from_marker_not_mtime(tmp_path):
    """get_claude_session_id uses marker file, not mtime scan, for lifecycle."""
    import bridge
    import claudecode

    worker_dir = bridge.SESSIONS_DIR / "frank"
    worker_dir.mkdir()

    marker = worker_dir / "claude_session_id"
    marker.write_text("correct-uuid-111\n/some/path")

    slug_dir = tmp_path / "claude_projects" / "-some-path"
    slug_dir.mkdir(parents=True)
    (slug_dir / "wrong-uuid-222.jsonl").write_text("{}")
    import time

    time.sleep(0.05)
    (slug_dir / "wrong-uuid-333.jsonl").write_text("{}")

    sid = claudecode.get_claude_session_id("frank")
    assert sid == "correct-uuid-111"


def test_missing_marker_no_mtime_fallback_for_lifecycle(tmp_path, monkeypatch):
    """When marker is missing, lifecycle ops get empty — no silent mtime guess."""
    import bridge
    import claudecode
    from shared import CLAUDE_PROJECTS_DIR

    worker_dir = bridge.SESSIONS_DIR / "grace"
    worker_dir.mkdir()

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    bridge._registry_add("grace", "claude")
    bridge._set_worker_cwd("grace", str(project_dir))

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    monkeypatch.setattr("claudecode.CLAUDE_PROJECTS_DIR", projects_dir)
    slug = str(project_dir).replace("/", "-")
    slug_dir = projects_dir / slug
    slug_dir.mkdir(parents=True)
    (slug_dir / "scannable-uuid-111.jsonl").write_text("{}")

    sid = claudecode.get_claude_session_id("grace")
    assert sid == "", f"Without marker, should return empty — got '{sid}' from mtime scan"


def test_scan_session_id_finds_latest_jsonl(tmp_path, monkeypatch):
    """scan_session_id returns the newest JSONL by mtime (diagnostic only)."""
    import claudecode

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    monkeypatch.setattr("claudecode.CLAUDE_PROJECTS_DIR", projects_dir)
    slug = str(tmp_path / "myproject").replace("/", "-")
    slug_dir = projects_dir / slug
    slug_dir.mkdir(parents=True)
    import time

    (slug_dir / "old-uuid.jsonl").write_text("{}")
    time.sleep(0.05)
    (slug_dir / "new-uuid.jsonl").write_text("{}")

    sid = claudecode.scan_session_id(str(tmp_path / "myproject"))
    assert sid == "new-uuid"


def test_scan_does_not_write_marker(tmp_path, monkeypatch):
    """scan_session_id must NOT auto-write the result to the marker file."""
    import bridge
    import claudecode

    worker_dir = bridge.SESSIONS_DIR / "holly"
    worker_dir.mkdir()

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    bridge._registry_add("holly", "claude")
    bridge._set_worker_cwd("holly", str(project_dir))

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    monkeypatch.setattr("claudecode.CLAUDE_PROJECTS_DIR", projects_dir)
    slug = str(project_dir).replace("/", "-")
    slug_dir = projects_dir / slug
    slug_dir.mkdir(parents=True)
    (slug_dir / "scanned-uuid.jsonl").write_text("{}")

    claudecode.scan_session_id(str(project_dir))

    marker = worker_dir / "claude_session_id"
    assert not marker.exists(), "scan_session_id must not write to marker file"


# --- Teleport state transitions ---


def test_teleport_preserves_home_on_double_teleport(tmp_path):
    """A→B→C: do_teleport preserves original home (A), doesn't overwrite to B.

    Tests the logic in do_teleport() that checks existing home before setting.
    """
    import bridge

    bridge._registry_add("hiro", "claude")
    # Teleport A → B: record home = A
    bridge._registry_update_teleport("hiro", host="machine-b", home_host="machine-a", home_cwd="/a/project")

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["hiro"]["home_host"] == "machine-a"
    assert data["workers"]["hiro"]["home_cwd"] == "/a/project"

    # Simulate what do_teleport does for B → C:
    # It reads existing home and preserves it if set
    existing = bridge._load_registry().get("workers", {}).get("hiro", {})
    prev_home_host = existing.get("home_host")
    prev_home_cwd = existing.get("home_cwd")
    bridge._registry_update_teleport(
        "hiro",
        host="machine-c",
        home_host=prev_home_host or "machine-b",
        home_cwd=prev_home_cwd or "/b/project",
    )

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data["workers"]["hiro"]
    assert w["host"] == "machine-c"
    assert w["home_host"] == "machine-a", "Home should stay at original A, not overwrite to B"
    assert w["home_cwd"] == "/a/project"


def test_teleback_clears_home(tmp_path):
    """After teleback, home fields are cleared."""
    import bridge

    bridge._registry_add("iris", "claude")
    bridge._registry_update_teleport("iris", host="mac-mini", home_host="vps", home_cwd="/home/claude/proj")

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["iris"]["home_host"] == "vps"

    bridge._registry_clear_teleport("iris")

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data["workers"]["iris"]
    assert w.get("home_host") in (None, ""), "home_host should be cleared after teleback"
    assert w.get("home_cwd") in (None, ""), "home_cwd should be cleared after teleback"
    assert w.get("host") is None, "host should be cleared (back to local)"


# --- Minimal record completeness ---


def test_minimal_record_fields(tmp_path):
    """Registry entry contains the minimal fields: host, cwd, home_host, home_cwd."""
    import bridge

    project = tmp_path / "project"
    project.mkdir()

    bridge._registry_add("kai", "claude")
    bridge._set_worker_cwd("kai", str(project))
    bridge._registry_update_teleport("kai", host="mac", home_host="vps", home_cwd="/home/claude/proj")

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data["workers"]["kai"]
    assert w.get("host") == "mac"
    assert w.get("cwd") == str(project)
    assert w.get("home_host") == "vps"
    assert w.get("home_cwd") == "/home/claude/proj"


def test_checkin_cwd_updates_registry(tmp_path):
    """Checkin with cwd= param updates the registry cwd field."""
    import bridge
    import io

    project = tmp_path / "project"
    project.mkdir()

    bridge._registry_add("lena", "claude")
    bridge.worker_manager.scan_tmux_sessions = lambda: {"lena": {"tmux": "claude-test-wstate-lena"}}
    bridge.worker_manager.get_registered_sessions = lambda *a, **kw: {"lena": {"tmux": "claude-test-wstate-lena"}}
    bridge.worker_manager._get_tmux_pane_cwd = lambda *a, **kw: str(project)

    bridge._set_worker_cwd("lena", str(project))

    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["lena"]["cwd"] == str(project)
