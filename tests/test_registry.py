"""Tests migrated from test.sh — registry category."""
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

    # These tests point bridge's module-level registry/session globals at
    # scratch dirs and monkeypatch the shared worker_manager singleton
    # in-process (the original bash tests ran each as a separate python3
    # subprocess, so there was nothing to leak). Since bridge is a single
    # module object shared across every test file in this pytest session,
    # snapshot the mutable bits here and restore them after each test so
    # this file can't bleed state into test_workers.py, test_checkin.py, etc.
    import bridge
    saved_node_dir = bridge.NODE_DIR
    saved_registry_file = bridge.WORKER_REGISTRY_FILE
    saved_sessions_dir = bridge.SESSIONS_DIR
    saved_wm_dict = dict(bridge.worker_manager.__dict__)
    saved_backends = dict(bridge.BACKENDS)
    saved_codex_send = bridge.CodexBackend.send

    yield

    bridge.NODE_DIR = saved_node_dir
    bridge.WORKER_REGISTRY_FILE = saved_registry_file
    bridge.SESSIONS_DIR = saved_sessions_dir
    bridge.worker_manager.__dict__.clear()
    bridge.worker_manager.__dict__.update(saved_wm_dict)
    bridge.BACKENDS.clear()
    bridge.BACKENDS.update(saved_backends)
    bridge.CodexBackend.send = saved_codex_send
    bridge.worker_manager.invalidate_sessions_cache()


def test_registry_add_remove():
    """Registry add/remove CRUD works."""
    import json
    import tempfile
    from pathlib import Path
    import bridge

    # Use temp dir to avoid touching real registry
    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"

    # Add a worker
    bridge._registry_add("alice", "claude", 12345)
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert "alice" in data["workers"], "alice not in registry"
    assert data["workers"]["alice"]["backend"] == "claude"
    assert data["workers"]["alice"]["chat_id"] == 12345
    assert data["version"] == 1

    # Add another
    bridge._registry_add("bob", "codex", 67890)
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert "alice" in data["workers"] and "bob" in data["workers"], "both should exist"

    # Remove alice
    bridge._registry_remove("alice")
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert "alice" not in data["workers"], "alice should be removed"
    assert "bob" in data["workers"], "bob should remain"

    # Remove nonexistent (no crash)
    bridge._registry_remove("charlie")

    # Verify file permissions
    perms = oct(bridge.WORKER_REGISTRY_FILE.stat().st_mode)[-3:]
    assert perms == "600", f"registry file should be 600, got {perms}"

    import shutil
    shutil.rmtree(tmpdir)


def test_registry_bootstrap():
    """Registry bootstrap from tmux sessions works."""
    import json
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"

    # Simulate existing tmux sessions
    registered = {
        "alice": {"tmux": "claude-test-alice", "backend": "claude"},
        "bob": {"tmux": "claude-test-bob", "backend": "codex"},
    }

    # Registry file doesn't exist yet -> bootstrap should create it from
    # the currently running tmux sessions.
    bridge._registry_bootstrap(registered)
    assert bridge.WORKER_REGISTRY_FILE.exists(), "registry file should be created by bootstrap"
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert "alice" in data["workers"] and "bob" in data["workers"], "both workers should be bootstrapped"
    assert data["workers"]["alice"]["backend"] == "claude"
    assert data["workers"]["bob"]["backend"] == "codex"

    # Once the registry file exists, bootstrap is a no-op (first-run only)
    bridge._registry_add("carol", "claude", 999)
    bridge._registry_bootstrap({"dave": {"tmux": "claude-test-dave", "backend": "claude"}})
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert "dave" not in data["workers"], "bootstrap should not re-run once registry file exists"
    assert "carol" in data["workers"], "existing registry entries should be untouched"

    import shutil
    shutil.rmtree(tmpdir)


def test_registry_corrupt_recovery():
    """Registry corrupt file recovery works."""
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"

    # Write garbage to registry file
    bridge.WORKER_REGISTRY_FILE.write_text("not valid json{{{")

    # Load should return empty dict, not crash
    data = bridge._load_registry()
    assert data == {}, f"corrupt file should return empty dict, got {data}"

    # Corrupt file should be renamed
    corrupt_files = list(Path(tmpdir).glob("workers.corrupt.*"))
    assert len(corrupt_files) == 1, f"expected 1 corrupt backup, got {len(corrupt_files)}"

    # Now add a worker — should work fine after corrupt recovery
    bridge._registry_add("alice", "claude", 12345)
    data = bridge._load_registry()
    assert "alice" in data.get("workers", {}), "should work after corrupt recovery"

    import shutil
    shutil.rmtree(tmpdir)


def test_registry_add_preserves_host():
    """_registry_add preserves existing host/teleport fields."""
    import json
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"

    # Add a worker with host (simulating forge registration)
    bridge._registry_add("ivy", "claude", 123, host="beastoin-agents-f1-mac-mini")
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["ivy"]["host"] == "beastoin-agents-f1-mac-mini", "host should be set"

    # Re-register the same worker (simulating re-hire) — host must survive
    bridge._registry_add("ivy", "claude", 456)
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data["workers"]["ivy"]
    assert w.get("host") == "beastoin-agents-f1-mac-mini", f"host should survive re-add, got {w.get('host')}"
    assert w["chat_id"] == 456, f"chat_id should update to 456, got {w['chat_id']}"

    # Also test: teleport fields survive re-add
    bridge._registry_update_teleport("ivy", host="mac", home_host="vps", home_cwd="/home/proj")
    bridge._registry_add("ivy", "claude", 789)
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data["workers"]["ivy"]
    assert w.get("host") == "mac", f"teleport host should survive, got {w.get('host')}"
    assert w.get("home_host") == "vps", f"home_host should survive, got {w.get('home_host')}"
    assert w.get("home_cwd") == "/home/proj", f"home_cwd should survive, got {w.get('home_cwd')}"

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    import shutil
    shutil.rmtree(tmpdir)


def test_registry_add_clears_stale_callback():
    """_registry_add clears stale callback_url on non-callback re-registration."""
    import json
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"

    # First: register as callback worker (simulating forge registration)
    bridge._registry_add_callback("bot1", "http://old-host:9000/callback", host="old-host", version="1.0")
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["bot1"].get("callback_url") == "http://old-host:9000/callback", "callback_url should be set"

    # Second: re-register as regular worker (simulating /hire)
    bridge._registry_add("bot1", "claude", 456)
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data["workers"]["bot1"]

    # callback_url must NOT survive — it would route messages to a dead endpoint
    assert "callback_url" not in w, f"stale callback_url should be cleared, got {w.get('callback_url')}"
    assert "protocol" not in w, f"stale protocol should be cleared, got {w.get('protocol')}"

    # But host/teleport fields SHOULD survive
    bridge._registry_add("bot1", "claude", 456, host="new-host")
    bridge._registry_add("bot1", "claude", 789)
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    assert data["workers"]["bot1"].get("host") == "new-host", "host should survive"

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    import shutil
    shutil.rmtree(tmpdir)


def test_get_registered_includes_registry():
    """get_registered_sessions includes registry workers."""
    import json
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"
    bridge.SESSIONS_DIR = Path(tmpdir) / "sessions"
    bridge.SESSIONS_DIR.mkdir()

    # Pre-create registry with a dead worker (no live tmux session)
    data = {"version": 1, "workers": {
        "deadworker": {"backend": "claude", "chat_id": 123, "hire_time": 1000},
    }}
    bridge.WORKER_REGISTRY_FILE.write_text(json.dumps(data))

    # No live tmux sessions — the worker should still surface from the registry
    bridge._sync_worker_manager()
    bridge.worker_manager.invalidate_sessions_cache()
    bridge.worker_manager.scan_tmux_sessions = lambda: {}

    result = bridge.get_registered_sessions()
    assert "deadworker" in result, f"registry worker should appear in get_registered_sessions, got {result}"
    assert result["deadworker"]["backend"] == "claude"
    assert "tmux" not in result["deadworker"], "dead worker with no host should have no tmux key"

    import shutil
    shutil.rmtree(tmpdir)


def test_end_removes_from_registry():
    """/end removes worker from registry."""
    import subprocess
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / "workers.json"
    bridge.SESSIONS_DIR = Path(tmpdir) / "sessions"
    bridge.SESSIONS_DIR.mkdir()

    prefix = "claude-regtest-"
    wm = bridge.WorkerManager(bridge.SESSIONS_DIR, prefix)

    # Create a tmux session to simulate a live worker
    tmux_name = f"{prefix}endtest"
    subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name], capture_output=True)

    # Add to registry
    bridge._registry_add("endtest", "claude", 123)
    data = bridge._load_registry()
    assert "endtest" in data["workers"], "worker should be in registry before end"

    # End the worker
    ok, err = wm.end("endtest")
    assert ok, f"end should succeed, got err: {err}"

    # Verify removed from registry
    data = bridge._load_registry()
    assert "endtest" not in data.get("workers", {}), "worker should be removed from registry after end"

    import shutil
    shutil.rmtree(tmpdir)


def test_send_to_worker_uses_backend_registry():
    """send_to_worker uses backend registry correctly."""
    import tempfile
    from pathlib import Path
    import bridge

    # Track calls
    calls = {"codex": 0}

    # Mock codex backend send
    def fake_codex_send(self, name, tmux, text, url, dir):
        calls["codex"] += 1
        return True

    # Save original and replace
    original_send = bridge.CodexBackend.send
    bridge.CodexBackend.send = fake_codex_send
    bridge.BACKENDS["codex"] = bridge.CodexBackend()

    # Create temp sessions dir with a codex worker
    tmp = Path(tempfile.mkdtemp())
    bridge.SESSIONS_DIR = tmp
    bridge.worker_manager.scan_tmux_sessions = lambda: {}
    bridge._sync_worker_manager()
    bridge.worker_manager.invalidate_sessions_cache()

    session_dir = tmp / "testcodex"
    session_dir.mkdir()
    (session_dir / "backend").write_text("codex")

    try:
        # Call send_to_worker
        result = bridge.send_to_worker("testcodex", "hello from test")

        # Should return True and have called codex send
        assert result == True, f"Expected True, got {result}"
        assert calls["codex"] == 1, f"Expected 1 codex call, got {calls}"
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(tmp)
        bridge.CodexBackend.send = original_send
        bridge.BACKENDS["codex"] = bridge.CodexBackend()


def test_backend_registry_exists():
    """Backend registry exists and contains expected backends."""
    import bridge

    # Check BACKENDS registry exists
    assert hasattr(bridge, "BACKENDS"), "BACKENDS registry should exist"

    # Check expected backends are registered
    expected = ["claude", "codex"]
    for name in expected:
        assert name in bridge.BACKENDS, f"{name} should be in BACKENDS"

    # Check get_backend helper
    for name in expected:
        backend = bridge.get_backend(name)
        assert backend is not None, f"get_backend({name}) should return backend"
        assert hasattr(backend, "send"), f"{name} backend should have send method"
        assert hasattr(backend, "is_online"), f"{name} backend should have is_online method"
        assert hasattr(backend, "start_cmd"), f"{name} backend should have start_cmd method"

    # Check is_valid_backend helper
    assert bridge.is_valid_backend("claude") == True
    assert bridge.is_valid_backend("codex") == True
    assert bridge.is_valid_backend("invalid") == False

    # Check list_backends helper
    available = bridge.list_backends()
    assert set(available) == set(expected), f"list_backends should return {expected}"


def test_get_registered_sessions_includes_noninteractive_workers():
    """get_registered_sessions includes non-interactive workers."""
    import tempfile
    from pathlib import Path
    import bridge

    # Create temp sessions dir
    tmp = Path(tempfile.mkdtemp())
    bridge.SESSIONS_DIR = tmp
    bridge.worker_manager.scan_tmux_sessions = lambda: {}  # No tmux sessions
    bridge._sync_worker_manager()
    bridge.worker_manager.invalidate_sessions_cache()

    # Create a non-interactive worker (like codex)
    session_dir = tmp / "myworker"
    session_dir.mkdir()
    (session_dir / "backend").write_text("codex")

    # get_registered_sessions should include non-interactive worker
    result = bridge.get_registered_sessions()
    assert "myworker" in result, f"Should contain myworker, got {result}"
    assert result["myworker"]["backend"] == "codex", "Backend should be codex"

    # Cleanup
    import shutil
    shutil.rmtree(tmp)
