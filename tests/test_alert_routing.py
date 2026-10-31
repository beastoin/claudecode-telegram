"""Tests for alert routing and consolidation.

TDD tests for the unified alert system:
- AlertRouter: pattern-matching routing table from config file
- Routed delivery: health alerts go to the right workers
- External watchdog simplification: liveness-only, no health re-check
"""

import json
import threading
import time
import pytest
from unittest.mock import patch, MagicMock, call
from pathlib import Path


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
# 1. AlertRouter — config loading
# ---------------------------------------------------------------------------


def test_load_routing_config_from_file(tmp_path):
    """Routing config loads from JSON file with correct routes."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni", "lee"]},
            {"match": "github", "targets": ["geni"]},
            {"match": "*", "targets": ["lee"]},
        ]
    }
    config_file = tmp_path / "alert-routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert len(router.routes) == 3
    assert router.routes[0]["match"] == "gmail"
    assert router.routes[0]["targets"] == ["geni", "lee"]


def test_missing_routing_config_uses_fallback():
    """When config file doesn't exist, route all alerts to fallback workers."""
    import bridge

    router = bridge.AlertRouter.load(
        path=Path("/nonexistent/alert-routing.json"),
        fallback_workers=["lee"],
    )
    assert router.route("gmail") == ["lee"]
    assert router.route("anything") == ["lee"]


# ---------------------------------------------------------------------------
# 2. AlertRouter — pattern matching
# ---------------------------------------------------------------------------


def test_route_exact_match(tmp_path):
    """Exact signal name matches its route."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni"]},
            {"match": "*", "targets": ["lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("gmail") == ["geni"]


def test_route_wildcard_prefix(tmp_path):
    """Wildcard pattern 'thread:*' matches 'thread:worker-watchdog'."""
    import bridge

    config = {
        "routes": [
            {"match": "thread:*", "targets": ["lee"]},
            {"match": "*", "targets": ["fallback"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("thread:worker-watchdog") == ["lee"]
    assert router.route("thread:tunnel-watchdog") == ["lee"]


def test_route_fallback_star(tmp_path):
    """Unmatched signals fall through to '*' default route."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni"]},
            {"match": "*", "targets": ["lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("poll") == ["lee"]
    assert router.route("dispatch") == ["lee"]
    assert router.route("unknown_signal") == ["lee"]


def test_route_no_matching_rule_returns_empty(tmp_path):
    """If no route matches and no fallback, returns empty list."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("poll") == []


def test_route_worker_variable_expansion(tmp_path):
    """'$worker' in targets expands to the named worker."""
    import bridge

    config = {
        "routes": [
            {"match": "*", "targets": ["$worker", "lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("worker:hal", worker_name="hal") == ["hal", "lee"]


def test_route_worker_variable_without_name_skipped(tmp_path):
    """'$worker' without worker_name is dropped from targets."""
    import bridge

    config = {
        "routes": [
            {"match": "*", "targets": ["$worker", "lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("gmail") == ["lee"]


def test_route_deduplicates_targets(tmp_path):
    """If expansion produces duplicates, they are removed."""
    import bridge

    config = {
        "routes": [
            {"match": "*", "targets": ["$worker", "lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    result = router.route("worker:lee", worker_name="lee")
    assert result == ["lee"]


# ---------------------------------------------------------------------------
# 3. Routed health alert delivery
# ---------------------------------------------------------------------------


def test_health_alert_delivers_to_routed_workers(tmp_path):
    """Health alert for 'gmail' signal delivers to routing targets, not flat ALERT_WORKERS."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni"]},
            {"match": "*", "targets": ["lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    delivered_to = []
    original_send = bridge.worker_manager.send

    def capture_send(name, msg, **kw):
        delivered_to.append(name)
        return True

    with (
        patch.object(bridge, "alert_router", router),
        patch.object(bridge, "_health_channel_id", "fake-channel"),
        patch.object(bridge, "relay_store", MagicMock()),
        patch("bridge._relay_append_msg"),
        patch.object(bridge.worker_manager, "send", side_effect=capture_send),
    ):
        bridge._send_health_alert("test alert", signal="gmail")
    assert "geni" in delivered_to
    assert "lee" not in delivered_to


def test_health_alert_fallback_when_no_signal(tmp_path):
    """When signal is empty, falls back to ALERT_WORKERS list."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni"]},
            {"match": "*", "targets": ["lee"]},
        ]
    }
    config_file = tmp_path / "routing.json"
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    delivered_to = []
    with (
        patch.object(bridge, "alert_router", router),
        patch.object(bridge, "ALERT_WORKERS", ["lee", "mon"]),
        patch.object(bridge, "_health_channel_id", "fake-channel"),
        patch.object(bridge, "relay_store", MagicMock()),
        patch("bridge._relay_append_msg"),
        patch.object(bridge.worker_manager, "send", side_effect=lambda n, m, **kw: delivered_to.append(n) or True),
    ):
        bridge._send_health_alert("test alert", signal="")
    assert "lee" in delivered_to
    assert "mon" in delivered_to


def test_emit_alert_passes_signal_to_health_alert():
    """_emit_alert with signal= parameter routes through to _send_health_alert."""
    import bridge

    calls = []

    def capture_health_alert(text, *, signal=""):
        calls.append({"text": text, "signal": signal})

    with (
        patch.object(bridge, "admin_chat_id", "12345"),
        patch.object(bridge, "transport", MagicMock()),
        patch("bridge._send_health_alert", side_effect=capture_health_alert),
    ):
        bridge._emit_alert("gmail is down", tag="health", signal="gmail")
    assert len(calls) == 1
    assert calls[0]["signal"] == "gmail"


def test_health_alert_check_passes_signal_keys():
    """_health_alert_check passes the signal key through to _emit_alert."""
    import bridge

    emit_calls = []
    original_emit = bridge._emit_alert

    def capture_emit(text, *, tag="alert", edit_msg_id=None, relay=True, signal=""):
        emit_calls.append({"text": text, "signal": signal, "tag": tag})

    snap = {
        "ok": False,
        "signals": {
            "gmail": {"ok": False, "last_ok_ago_s": 600, "errors": 5, "last_error": "token expired"},
        },
        "threads": {},
        "pools": {},
    }
    with (
        patch.object(bridge, "ALERT_WORKERS", ["lee"]),
        patch.object(bridge, "_health_channel_id", "fake-channel"),
        patch.object(bridge.health, "snapshot", return_value=snap),
        patch("bridge._emit_alert", side_effect=capture_emit),
        patch("bridge._alert_log_append", return_value="abc123"),
        patch("bridge._alerts_active_save"),
    ):
        # Clear any prior state
        with bridge._alert_lock:
            bridge._alert_active.clear()
            bridge._alert_last_sent.clear()
            bridge._alert_snoozed.clear()
            bridge._alert_refs.clear()
        bridge._health_alert_check()
    health_emits = [c for c in emit_calls if c["tag"] == "health"]
    assert len(health_emits) >= 1
    assert health_emits[0]["signal"] == "gmail"


# ---------------------------------------------------------------------------
# 4. External watchdog — liveness only
# ---------------------------------------------------------------------------


def test_external_watchdog_no_collect_problems():
    """External watchdog should not have collect_problems function (removed)."""
    src = Path("/home/claude/claudecode-telegram/bridge-watchdog.py").read_text()
    assert (
        "def collect_problems" not in src
    ), "collect_problems should be removed — health signal checking is internal-only"


def test_external_watchdog_no_reconcile_alerts():
    """External watchdog should not have reconcile_alerts function (removed)."""
    src = Path("/home/claude/claudecode-telegram/bridge-watchdog.py").read_text()
    assert "def reconcile_alerts" not in src, "reconcile_alerts should be removed — dedup is internal-only"


def test_external_watchdog_bridge_dead_still_alerts():
    """When bridge process is dead, external watchdog still fires Telegram + tmux alerts."""
    src = Path("/home/claude/claudecode-telegram/bridge-watchdog.py").read_text()
    assert "Bridge process not running" in src
    assert "telegram_send" in src
    assert "deliver_to_workers" in src


def test_external_watchdog_http_dead_still_alerts():
    """When bridge HTTP is dead, external watchdog still fires Telegram + tmux alerts."""
    src = Path("/home/claude/claudecode-telegram/bridge-watchdog.py").read_text()
    assert "Bridge HTTP not responding" in src
    assert "telegram_send" in src


def test_external_watchdog_no_health_signal_loop():
    """External watchdog main() should not iterate over health signals."""
    src = Path("/home/claude/claudecode-telegram/bridge-watchdog.py").read_text()
    assert (
        "collect_problems" not in src
    ), "main() should not call collect_problems — health evaluation moved to internal"


# ---------------------------------------------------------------------------
# 5. Config file location
# ---------------------------------------------------------------------------


def test_default_routing_config_path():
    """Default routing config path is in standard config directory."""
    import bridge

    assert hasattr(bridge, "AlertRouter")
    assert hasattr(bridge, "_ALERT_ROUTING_FILE")
    path = bridge._ALERT_ROUTING_FILE
    assert "claudecode-telegram" in str(path)
    assert "alert-routing.json" in str(path)


# ---------------------------------------------------------------------------
# 6. Signal wiring — all _emit_alert call sites pass signal=
# ---------------------------------------------------------------------------


def test_connector_on_alert_passes_signal_tag():
    """connector_on_alert handler passes signal=tag to _emit_alert."""
    import bridge

    calls = []

    def capture_emit(text, *, tag="alert", edit_msg_id=None, relay=True, signal=""):
        calls.append({"text": text, "tag": tag, "signal": signal})

    handler = bridge.connector_on_alert("gmail")
    with patch("bridge._emit_alert", side_effect=capture_emit):
        handler("Gmail auth token expired")
    assert len(calls) == 1
    assert calls[0]["signal"] == "gmail"
    assert calls[0]["tag"] == "gmail"


def test_watchdog_alert_passes_signal_key():
    """_watchdog_alert passes signal=key to _emit_alert."""
    import bridge

    calls = []

    def capture_emit(text, *, tag="alert", edit_msg_id=None, relay=True, signal=""):
        calls.append({"signal": signal, "tag": tag})

    with (
        patch("bridge._emit_alert", side_effect=capture_emit),
        patch("bridge._alert_log_append", return_value="abc123"),
    ):
        bridge._watchdog_alert("host", "Mac Mini unreachable", key="host:macmini")
    assert len(calls) == 1
    assert calls[0]["signal"] == "host:macmini"
    assert calls[0]["tag"] == "watchdog"


def test_send_watchdog_alert_passes_worker_signal():
    """_send_watchdog_alert passes signal=f'worker:{name}' to _emit_alert."""
    import bridge

    calls = []

    def capture_emit(text, *, tag="alert", edit_msg_id=None, relay=True, signal=""):
        calls.append({"signal": signal})

    with (
        patch.object(bridge, "admin_chat_id", "12345"),
        patch.object(bridge, "transport", MagicMock()),
        patch("bridge._emit_alert", side_effect=capture_emit),
        patch("bridge._alert_log_append", return_value="abc123"),
    ):
        with bridge.watchdog.lock:
            bridge.watchdog.last_alert_ts.pop("hal", None)
        bridge._send_watchdog_alert("hal", "DEAD", "process exited")
    assert len(calls) >= 1
    assert calls[0]["signal"] == "worker:hal"


def test_send_resolved_alert_passes_worker_signal():
    """_send_resolved_alert passes signal=f'worker:{name}' to _emit_alert."""
    import bridge

    calls = []

    def capture_emit(text, *, tag="alert", edit_msg_id=None, relay=True, signal=""):
        calls.append({"signal": signal, "text": text})

    with (
        patch.object(bridge, "admin_chat_id", "12345"),
        patch("bridge._emit_alert", side_effect=capture_emit),
        patch("bridge._alert_log_append", return_value="abc123"),
    ):
        with bridge.watchdog.lock:
            bridge.watchdog.last_resolved_ts.pop("hal", None)
            bridge.watchdog.alert_msg_ids.pop("hal", None)
            bridge.watchdog.prev_worker_states["hal"] = "DEAD"
            bridge.watchdog.recent_restarts.pop("hal", None)
        bridge._send_resolved_alert("hal", "READY")
    assert len(calls) >= 1
    assert calls[0]["signal"] == "worker:hal"
    assert "back to normal" in calls[0]["text"]


def test_no_emit_alert_without_signal():
    """Every _emit_alert() invocation in bridge.py passes signal= explicitly."""
    import re

    src = Path("/home/claude/claudecode-telegram/bridge.py").read_text()
    # Match call sites only — skip the function definition (which has "def _emit_alert(")
    call_pattern = re.compile(r"(?<!def )_emit_alert\(")
    emit_calls = list(call_pattern.finditer(src))
    assert len(emit_calls) > 0, "Should find at least one _emit_alert call"
    for m in emit_calls:
        start = m.start()
        depth = 0
        i = m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call_text = src[start : i + 1]
        assert "signal=" in call_text, f"_emit_alert call missing signal=: {call_text[:100]}"


def test_worker_state_alerts_route_to_empty_targets():
    """Worker state alerts (worker:*) route to empty target list — admin DM only."""
    import bridge

    config = {
        "routes": [
            {"match": "gmail", "targets": ["geni"]},
            {"match": "worker:*", "targets": []},
            {"match": "*", "targets": ["lee"]},
        ]
    }
    config_file = Path("/tmp/test-routing-worker.json")
    config_file.write_text(json.dumps(config))
    router = bridge.AlertRouter.load(path=config_file)
    assert router.route("worker:hal") == []
    assert router.route("worker:mon") == []
    assert router.route("gmail") == ["geni"]
    assert router.route("host:macmini") == ["lee"]
    config_file.unlink()


def test_health_endpoint_passes_signal():
    """POST /health alert endpoint passes signal=f'health:{worker}'."""
    import bridge

    calls = []

    def capture_emit(text, *, tag="alert", edit_msg_id=None, relay=True, signal=""):
        calls.append({"signal": signal, "tag": tag})

    handler = MagicMock()
    handler._send_json = MagicMock()
    body = json.dumps({
        "worker": "hal",
        "issue": "jsonl_stale",
        "age": 300,
    }).encode()

    with (
        patch("bridge._emit_alert", side_effect=capture_emit),
        patch("bridge._log_event", return_value="evt123"),
        patch("bridge._log"),
    ):
        bridge.handle_health_alert(handler, body)
    assert len(calls) == 1
    assert calls[0]["signal"] == "health:hal"
