"""TDD tests for Periodic Learning Reminders feature in bridge.py.

Two triggers: response count (15) and idle timeout (6h).
Anti-annoyance: reminder_pending blocks all triggers until worker responds.
State persisted to disk across bridge restarts.
"""

import json
import os
import sys
import time
import threading
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    """Set minimal env so bridge.py can be imported without crashing."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    _wait_for_bg_threads()
    yield
    _wait_for_bg_threads()


def _import_bridge():
    if "bridge" in sys.modules:
        return sys.modules["bridge"]
    import bridge
    return bridge


def _wait_for_bg_threads(timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name != "MainThread" and "_send_learning_reminder" in str(t._target or "")]
        if not alive:
            return
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Response count trigger
# ---------------------------------------------------------------------------

class TestCheckLearningReminder:

    def test_first_call_initializes_state(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            bridge._check_learning_reminder("worker_a")

        st = bridge._learning_reminder_state["worker_a"]
        assert st["response_count"] == 1
        assert st["last_reminder_ts"] > 0
        assert st["last_response_ts"] > 0
        assert st["reminder_pending"] is False

    def test_increments_count_each_call(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(5):
                bridge._check_learning_reminder("worker_b")

        assert bridge._learning_reminder_state["worker_b"]["response_count"] == 5

    def test_fires_at_response_threshold(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD):
                bridge._check_learning_reminder("worker_c")

            _wait_for_bg_threads()
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == "worker_c"
            assert "Self-learning check" in mock_send.call_args[0][1]

    def test_does_not_fire_below_threshold(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD - 1):
                bridge._check_learning_reminder("worker_d")

            time.sleep(0.5)
            mock_send.assert_not_called()

    def test_resets_count_after_firing(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD):
                bridge._check_learning_reminder("worker_e")

        assert bridge._learning_reminder_state["worker_e"]["response_count"] == 0

    def test_updates_last_response_ts(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            bridge._check_learning_reminder("ts_worker")
            ts1 = bridge._learning_reminder_state["ts_worker"]["last_response_ts"]
            time.sleep(0.05)
            bridge._check_learning_reminder("ts_worker")
            ts2 = bridge._learning_reminder_state["ts_worker"]["last_response_ts"]

        assert ts2 >= ts1


# ---------------------------------------------------------------------------
# Anti-annoyance: reminder_pending
# ---------------------------------------------------------------------------

class TestReminderPending:

    def test_pending_set_on_fire(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD):
                bridge._check_learning_reminder("pending_worker")

        assert bridge._learning_reminder_state["pending_worker"]["reminder_pending"] is True

    def test_pending_blocks_second_fire(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        threshold = bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(threshold):
                bridge._check_learning_reminder("block_worker")
            _wait_for_bg_threads()
            assert mock_send.call_count == 1

            # First clears pending (count=1), then threshold-2 more = count threshold-1. No fire.
            for _ in range(threshold - 1):
                bridge._check_learning_reminder("block_worker")
            _wait_for_bg_threads()
            assert mock_send.call_count == 1

    def test_pending_cleared_on_response_after_fire(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        threshold = bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(threshold):
                bridge._check_learning_reminder("clear_worker")
            _wait_for_bg_threads()
            assert mock_send.call_count == 1
            assert bridge._learning_reminder_state["clear_worker"]["reminder_pending"] is True

            # One response clears pending
            bridge._check_learning_reminder("clear_worker")
            assert bridge._learning_reminder_state["clear_worker"]["reminder_pending"] is False

            # Reach threshold again — should fire
            for _ in range(threshold - 1):
                bridge._check_learning_reminder("clear_worker")
            _wait_for_bg_threads()
            assert mock_send.call_count == 2

    def test_pending_cleared_on_reset(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD):
                bridge._check_learning_reminder("reset_pending")

        assert bridge._learning_reminder_state["reset_pending"]["reminder_pending"] is True

        with patch("bridge._save_learning_reminder_state"):
            bridge._reset_learning_reminder("reset_pending")

        assert bridge._learning_reminder_state["reset_pending"]["reminder_pending"] is False


# ---------------------------------------------------------------------------
# Idle detection (timer-based)
# ---------------------------------------------------------------------------

class TestIdleDetection:

    def test_idle_constant_exists(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        assert bridge.LEARNING_REMINDER_IDLE_HOURS == 6

    def test_idle_scan_fires_for_idle_worker(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["idle_worker"] = {
            "response_count": 5,
            "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600),
            "reminder_pending": False,
        }

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == "idle_worker"

    def test_idle_scan_skips_recent_worker(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["active_worker"] = {
            "response_count": 5,
            "last_reminder_ts": now - (8 * 3600),
            "last_response_ts": now - (1 * 3600),
            "reminder_pending": False,
        }

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()
            mock_send.assert_not_called()

    def test_idle_scan_skips_recently_reminded(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["reminded_worker"] = {
            "response_count": 0,
            "last_reminder_ts": now - (2 * 3600),
            "last_response_ts": now - (7 * 3600),
            "reminder_pending": False,
        }

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()
            mock_send.assert_not_called()

    def test_idle_scan_skips_low_response_count(self, tmp_path, monkeypatch):
        """Workers with response_count <= 1 (only acknowledged reminder, no real work) are skipped."""
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["ack_only"] = {
            "response_count": 1,
            "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600),
            "reminder_pending": False,
        }
        bridge._learning_reminder_state["fresh_seed"] = {
            "response_count": 0,
            "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600),
            "reminder_pending": False,
        }

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()
            mock_send.assert_not_called()

    def test_idle_scan_skips_pending(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["pending_idle"] = {
            "response_count": 0,
            "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600),
            "reminder_pending": True,
        }

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()
            mock_send.assert_not_called()

    def test_idle_scan_sets_pending(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["idle_pending"] = {
            "response_count": 3,
            "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600),
            "reminder_pending": False,
        }

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()

        assert bridge._learning_reminder_state["idle_pending"]["reminder_pending"] is True

    def test_idle_scan_multiple_workers(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        now = time.time()
        bridge._learning_reminder_state["idle_a"] = {
            "response_count": 4, "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600), "reminder_pending": False,
        }
        bridge._learning_reminder_state["idle_b"] = {
            "response_count": 2, "last_reminder_ts": now - (7 * 3600),
            "last_response_ts": now - (7 * 3600), "reminder_pending": False,
        }
        bridge._learning_reminder_state["active_c"] = {
            "response_count": 3, "last_reminder_ts": now - (1 * 3600),
            "last_response_ts": now - (0.5 * 3600), "reminder_pending": False,
        }

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            _wait_for_bg_threads()
            assert mock_send.call_count == 2
            names = {c[0][0] for c in mock_send.call_args_list}
            assert names == {"idle_a", "idle_b"}


# ---------------------------------------------------------------------------
# Idle scan resilience
# ---------------------------------------------------------------------------

class TestIdleScanResilience:

    def test_scan_reschedules_after_exception(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch.object(bridge, "_learning_reminder_lock") as mock_lock:
            mock_lock.__enter__ = MagicMock(side_effect=RuntimeError("boom"))
            mock_lock.__exit__ = MagicMock(return_value=False)
            with patch("bridge._schedule_idle_scan") as mock_schedule:
                bridge._scan_idle_workers()
                mock_schedule.assert_called_once()

    def test_scan_reschedules_on_success(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._schedule_idle_scan") as mock_schedule, \
             patch("bridge._save_learning_reminder_state"):
            bridge._scan_idle_workers()
            mock_schedule.assert_called_once()


# ---------------------------------------------------------------------------
# State reset
# ---------------------------------------------------------------------------

class TestResetLearningReminder:

    def test_reset_clears_count(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state["worker_x"] = {
            "response_count": 12, "last_reminder_ts": 1000.0,
            "last_response_ts": 1000.0, "reminder_pending": True,
        }

        with patch("bridge._save_learning_reminder_state"):
            bridge._reset_learning_reminder("worker_x")

        st = bridge._learning_reminder_state["worker_x"]
        assert st["response_count"] == 0
        assert st["last_reminder_ts"] > 1000.0
        assert st["last_response_ts"] > 1000.0
        assert st["reminder_pending"] is False

    def test_reset_on_new_worker(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge._save_learning_reminder_state"):
            bridge._reset_learning_reminder("new_worker")

        st = bridge._learning_reminder_state["new_worker"]
        assert st["response_count"] == 0
        assert st["reminder_pending"] is False
        assert "last_response_ts" in st

    def test_reset_after_partial_count(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(10):
                bridge._check_learning_reminder("partial_worker")
            bridge._reset_learning_reminder("partial_worker")
            assert bridge._learning_reminder_state["partial_worker"]["response_count"] == 0

            for _ in range(10):
                bridge._check_learning_reminder("partial_worker")
            time.sleep(0.5)
            mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Seed state for existing workers
# ---------------------------------------------------------------------------

class TestSeedState:

    def test_seeds_new_workers(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge._save_learning_reminder_state"):
            bridge._seed_learning_reminder_state(["alpha", "beta", "gamma"])

        assert len(bridge._learning_reminder_state) == 3
        for name in ["alpha", "beta", "gamma"]:
            st = bridge._learning_reminder_state[name]
            assert st["response_count"] == 0
            assert st["reminder_pending"] is False

    def test_seed_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        bridge._learning_reminder_state["existing"] = {
            "response_count": 7, "last_reminder_ts": 1000.0,
            "last_response_ts": 2000.0, "reminder_pending": True,
        }

        with patch("bridge._save_learning_reminder_state"):
            bridge._seed_learning_reminder_state(["existing", "new_one"])

        assert bridge._learning_reminder_state["existing"]["response_count"] == 7
        assert bridge._learning_reminder_state["existing"]["reminder_pending"] is True
        assert bridge._learning_reminder_state["new_one"]["response_count"] == 0

    def test_seed_empty_list(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge._save_learning_reminder_state"):
            bridge._seed_learning_reminder_state([])

        assert len(bridge._learning_reminder_state) == 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        monkeypatch.setattr(bridge, "NODE_DIR", tmp_path)

        now = time.time()
        bridge._learning_reminder_state["worker_a"] = {
            "response_count": 7, "last_reminder_ts": now,
            "last_response_ts": now, "reminder_pending": True,
        }
        bridge._learning_reminder_state["worker_b"] = {
            "response_count": 3, "last_reminder_ts": now - 3600,
            "last_response_ts": now - 1800, "reminder_pending": False,
        }

        bridge._save_learning_reminder_state()

        state_file = tmp_path / "learning_reminders.json"
        assert state_file.exists()

        bridge._learning_reminder_state.clear()
        bridge._load_learning_reminder_state()

        assert bridge._learning_reminder_state["worker_a"]["response_count"] == 7
        assert bridge._learning_reminder_state["worker_a"]["reminder_pending"] is True
        assert bridge._learning_reminder_state["worker_b"]["response_count"] == 3
        assert bridge._learning_reminder_state["worker_b"]["reminder_pending"] is False

    def test_load_missing_file_no_crash(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        monkeypatch.setattr(bridge, "NODE_DIR", tmp_path)

        bridge._load_learning_reminder_state()
        assert bridge._learning_reminder_state == {}

    def test_load_corrupt_file_no_crash(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        monkeypatch.setattr(bridge, "NODE_DIR", tmp_path)

        (tmp_path / "learning_reminders.json").write_text("not valid json{{{")
        bridge._load_learning_reminder_state()
        assert bridge._learning_reminder_state == {}

    def test_save_called_on_fire(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state") as mock_save:
            for _ in range(bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD):
                bridge._check_learning_reminder("save_worker")

            # save called on each increment + on fire
            assert mock_save.call_count == bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD

    def test_save_called_on_reset(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()

        with patch("bridge._save_learning_reminder_state") as mock_save:
            bridge._reset_learning_reminder("reset_save")
            mock_save.assert_called_once()

    def test_state_survives_simulated_restart(self, tmp_path, monkeypatch):
        """Simulate: set state, save, clear (= restart), load, verify."""
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        monkeypatch.setattr(bridge, "NODE_DIR", tmp_path)

        now = time.time()
        bridge._learning_reminder_state["survivor"] = {
            "response_count": 12, "last_reminder_ts": now - 7200,
            "last_response_ts": now - 3600, "reminder_pending": False,
        }
        bridge._save_learning_reminder_state()

        # Simulate restart
        bridge._learning_reminder_state.clear()
        assert "survivor" not in bridge._learning_reminder_state

        bridge._load_learning_reminder_state()
        assert bridge._learning_reminder_state["survivor"]["response_count"] == 12
        assert bridge._learning_reminder_state["survivor"]["last_response_ts"] == pytest.approx(now - 3600, abs=1)


# ---------------------------------------------------------------------------
# Reminder text
# ---------------------------------------------------------------------------

class TestReminderText:

    def test_text_contains_worker_name(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        text = bridge._LEARNING_REMINDER_TEXT.replace("{name}", "myworker")
        assert "myworker" in text
        assert "Self-learning check" in text
        assert "playbook.md" in text

    def test_text_mentions_learnings(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        assert "learnings.md" in bridge._LEARNING_REMINDER_TEXT

    def test_text_has_name_placeholder(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        assert "{name}" in bridge._LEARNING_REMINDER_TEXT


# ---------------------------------------------------------------------------
# send helper
# ---------------------------------------------------------------------------

class TestSendLearningReminder:

    def test_calls_send_to_worker(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        with patch("bridge.send_to_worker", return_value=True) as mock_send:
            bridge._send_learning_reminder("test_worker", "reminder text")
            mock_send.assert_called_once_with("test_worker", "reminder text")

    def test_handles_send_failure(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        with patch("bridge.send_to_worker", return_value=False) as mock_send:
            bridge._send_learning_reminder("test_worker", "reminder text")
            mock_send.assert_called_once()

    def test_handles_exception(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        with patch("bridge.send_to_worker", side_effect=Exception("connection error")):
            bridge._send_learning_reminder("test_worker", "reminder text")


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestConcurrency:

    def test_concurrent_checks_no_crash(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        errors = []

        def worker_thread(name):
            try:
                for _ in range(20):
                    bridge._check_learning_reminder(name)
            except Exception as e:
                errors.append(e)

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            threads = [threading.Thread(target=worker_thread, args=(f"t{i}",)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            _wait_for_bg_threads()

        assert errors == []

    def test_concurrent_reset_and_check(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        errors = []

        def checker():
            try:
                for _ in range(30):
                    bridge._check_learning_reminder("race_worker")
            except Exception as e:
                errors.append(e)

        def resetter():
            try:
                for _ in range(10):
                    bridge._reset_learning_reminder("race_worker")
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        with patch("bridge.send_to_worker", return_value=True), \
             patch("bridge._save_learning_reminder_state"):
            t1 = threading.Thread(target=checker)
            t2 = threading.Thread(target=resetter)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            _wait_for_bg_threads()

        assert errors == []


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:

    def test_response_threshold(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        assert bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD == 15

    def test_idle_hours(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        assert bridge.LEARNING_REMINDER_IDLE_HOURS == 6

    def test_no_stale_playbook_constant(self, tmp_path, monkeypatch):
        """Playbook staleness trigger was removed — constant should not exist."""
        bridge = _import_bridge()
        assert not hasattr(bridge, "LEARNING_REMINDER_PLAYBOOK_STALE_DAYS")


# ---------------------------------------------------------------------------
# Fire cycle
# ---------------------------------------------------------------------------

class TestFireCycleRepeats:

    def test_fires_again_after_pending_cleared(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        threshold = bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(threshold):
                bridge._check_learning_reminder("cycle_worker")
            _wait_for_bg_threads()
            assert mock_send.call_count == 1

            bridge._check_learning_reminder("cycle_worker")  # clears pending

            for _ in range(threshold - 1):
                bridge._check_learning_reminder("cycle_worker")
            _wait_for_bg_threads()
            assert mock_send.call_count == 2

    def test_reset_breaks_cycle(self, tmp_path, monkeypatch):
        bridge = _import_bridge()
        bridge._learning_reminder_state.clear()
        threshold = bridge.LEARNING_REMINDER_RESPONSE_THRESHOLD

        with patch("bridge.send_to_worker", return_value=True) as mock_send, \
             patch("bridge._save_learning_reminder_state"):
            for _ in range(threshold - 1):
                bridge._check_learning_reminder("reset_cycle")
            bridge._reset_learning_reminder("reset_cycle")
            for _ in range(threshold - 1):
                bridge._check_learning_reminder("reset_cycle")
            time.sleep(0.5)
            mock_send.assert_not_called()

            bridge._check_learning_reminder("reset_cycle")
            _wait_for_bg_threads()
            mock_send.assert_called_once()
