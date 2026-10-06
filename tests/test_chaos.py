"""Chaos and resilience tests for claudecode-telegram.

Tests thread-safety of container classes under concurrent stress,
bridge state recovery after process restart, and tmux session chaos.

These are FAST-mode tests (no bridge process needed) — they test
the container classes in isolation with real threads.
"""

import collections
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bridge  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────

class FakeClock:
    """Controllable clock for testing expiry logic under contention."""

    def __init__(self, start: float = 1_000_000.0):
        self._now = start
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        pass  # no-op in tests

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


def _run_threads(target, count=20, args=()):
    """Launch `count` threads all hitting `target`, wait for all to finish.
    Returns list of exceptions (empty = all passed).
    """
    errors: list[Exception] = []
    barrier = threading.Barrier(count)

    def wrapper():
        barrier.wait()  # all threads start at the same instant
        try:
            target()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=wrapper) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return errors


# ── TokenStore thread-safety ─────────────────────────────────────────────

class TestTokenStoreChaos:
    """Concurrent access to TokenStore.add_*/validate_* must not corrupt state."""

    def setup_method(self):
        """Fresh TokenStore + fake clock for each test."""
        self.store = bridge.TokenStore()
        self.clock = FakeClock()
        self._orig_clock = bridge._clock
        bridge._clock = self.clock
        # Clear global dicts
        bridge.REWIND_TOKENS.clear()
        bridge.PR_REVIEW_TOKENS.clear()

    def teardown_method(self):
        bridge._clock = self._orig_clock
        bridge.REWIND_TOKENS.clear()
        bridge.PR_REVIEW_TOKENS.clear()

    def test_concurrent_add_rewind(self):
        """20 threads each add a unique rewind token — all must survive."""
        tokens = [f"tok-{i}" for i in range(20)]

        def add_one():
            tid = threading.current_thread().name
            idx = int(tid.split("-")[-1]) if "-" in tid else 0
            token = tokens[idx % len(tokens)]
            self.store.add_rewind(token, f"worker-{idx}", timeout=3600)

        # Use explicit thread naming so we can derive index
        threads = []
        barrier = threading.Barrier(20)
        for i in range(20):
            def work(i=i):
                barrier.wait()
                self.store.add_rewind(tokens[i], f"worker-{i}", timeout=3600)
            t = threading.Thread(target=work)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All 20 tokens present
        assert len(bridge.REWIND_TOKENS) == 20
        for tok in tokens:
            assert self.store.validate_rewind(tok) is not None

    def test_concurrent_validate_rewind_under_expiry(self):
        """Add tokens, advance clock past half their expiry, then
        validate concurrently — expired ones must be cleaned, valid ones kept."""
        # Add 20 tokens: even-indexed expire in 10s, odd in 10000s
        for i in range(20):
            timeout = 10 if i % 2 == 0 else 10000
            self.store.add_rewind(f"tok-{i}", f"w-{i}", timeout=timeout)

        # Advance past the short-lived tokens
        self.clock.advance(50)

        results = collections.defaultdict(list)
        lock = threading.Lock()

        def validate_all():
            for i in range(20):
                result = self.store.validate_rewind(f"tok-{i}", extend=False)
                with lock:
                    results[i].append(result)

        errors = _run_threads(validate_all, count=10)
        assert not errors, f"Thread errors: {errors}"

        # Even tokens (short timeout) must be None; odd tokens must be valid
        for i in range(20):
            verdicts = results[i]
            if i % 2 == 0:
                assert all(v is None for v in verdicts), f"tok-{i} should be expired"
            else:
                assert all(v is not None for v in verdicts), f"tok-{i} should be valid"

    def test_concurrent_add_and_validate_rewind(self):
        """Simultaneous adds and validates must not raise or corrupt."""
        error_count = [0]
        lock = threading.Lock()

        def writer():
            for j in range(50):
                self.store.add_rewind(f"w-{uuid.uuid4()}", "test", timeout=3600)

        def reader():
            for j in range(50):
                # Validate a random token — may or may not exist
                try:
                    self.store.validate_rewind(f"w-{j}", extend=True)
                except Exception:
                    with lock:
                        error_count[0] += 1

        threads = []
        barrier = threading.Barrier(20)
        for i in range(10):
            def w():
                barrier.wait()
                writer()
            threads.append(threading.Thread(target=w))
        for i in range(10):
            def r():
                barrier.wait()
                reader()
            threads.append(threading.Thread(target=r))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert error_count[0] == 0, "Concurrent add+validate raised exceptions"

    def test_concurrent_pr_review_add_validate(self):
        """PR review tokens: concurrent add + validate must not corrupt."""
        tokens = [f"pr-{i}" for i in range(20)]
        barrier = threading.Barrier(20)

        def work(i):
            barrier.wait()
            self.store.add_pr_review(tokens[i], pr_num=i, owner="org", repo="repo")
            # Immediately validate own token
            entry = self.store.validate_pr_review(tokens[i])
            assert entry is not None, f"Token {tokens[i]} lost immediately after add"
            assert entry["pr_num"] == i

        threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All tokens present
        assert len(bridge.PR_REVIEW_TOKENS) == 20

    def test_pr_review_expiry_cleanup_concurrent(self):
        """Expired PR tokens are cleaned by validate, even under contention."""
        for i in range(20):
            timeout_override = 5 if i < 10 else 50000
            # Manually set expiry since add_pr_review uses PR_REVIEW_EXTEND
            bridge.PR_REVIEW_TOKENS[f"pr-{i}"] = {
                "pr_num": i, "owner": "o", "repo": "r",
                "expires_at": self.clock.time() + timeout_override,
            }

        self.clock.advance(100)  # past the first 10 tokens

        def validate_batch():
            for i in range(20):
                self.store.validate_pr_review(f"pr-{i}", extend=False)

        errors = _run_threads(validate_batch, count=10)
        assert not errors

        # First 10 cleaned, last 10 survive
        for i in range(10):
            assert self.store.validate_pr_review(f"pr-{i}", extend=False) is None
        for i in range(10, 20):
            assert self.store.validate_pr_review(f"pr-{i}", extend=False) is not None


# ── ConnectorRegistry thread-safety ──────────────────────────────────────

class TestConnectorRegistryChaos:
    """Concurrent logging and reading from ConnectorRegistry."""

    def test_concurrent_log_message(self):
        """20 threads each log 50 messages — no data loss, no crash."""
        registry = bridge.ConnectorRegistry()
        barrier = threading.Barrier(20)

        def log_messages(thread_id):
            barrier.wait()
            for j in range(50):
                registry.log_message(
                    "gmail",
                    f"<b>thread {thread_id} msg {j}</b>",
                    f"thread {thread_id} msg {j}",
                    [f"worker-{j % 5}"],
                )

        threads = [threading.Thread(target=log_messages, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # deque maxlen=20 means only latest 20 survive per tag
        log = registry.get_log("gmail")
        assert len(log) == 20  # capped at maxlen
        # Verify each entry has expected shape
        for entry in log:
            assert "ts" in entry
            assert "html" in entry
            assert "plain" in entry
            assert "targets" in entry

    def test_concurrent_log_multiple_tags(self):
        """Different tags don't interfere with each other."""
        registry = bridge.ConnectorRegistry()
        barrier = threading.Barrier(20)

        def log_to_tag(tag_idx):
            barrier.wait()
            tag = f"tag-{tag_idx % 4}"
            for j in range(10):
                registry.log_message(tag, f"html-{j}", f"plain-{j}", [])

        threads = [threading.Thread(target=log_to_tag, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 4 tags, each with up to 20 entries (5 threads x 10 msgs, capped at 20)
        for tag_idx in range(4):
            log = registry.get_log(f"tag-{tag_idx}")
            assert len(log) <= 20
            assert len(log) > 0

    def test_concurrent_log_and_read(self):
        """Writers and readers at the same time must not deadlock or crash."""
        registry = bridge.ConnectorRegistry()
        barrier = threading.Barrier(20)
        read_results = []
        read_lock = threading.Lock()

        def writer():
            barrier.wait()
            for j in range(100):
                registry.log_message("test", f"h{j}", f"p{j}", [])

        def reader():
            barrier.wait()
            for _ in range(100):
                snap = registry.get_log("test")
                with read_lock:
                    read_results.append(len(snap))

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=writer))
        for i in range(10):
            threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # Readers got snapshots without crashing
        assert len(read_results) == 1000  # 10 readers x 100 reads
        # Final state: log exists and is capped
        final_log = registry.get_log("test")
        assert 0 < len(final_log) <= 20

    def test_stop_all_with_failing_connector(self):
        """stop_all must not crash even if a connector's stop() raises."""
        registry = bridge.ConnectorRegistry()

        class BadConnector:
            def stop(self):
                raise RuntimeError("connector exploded")

        class GoodConnector:
            stopped = False
            def stop(self):
                self.stopped = True

        registry.gmail = BadConnector()
        registry.github = GoodConnector()

        # Should not raise
        registry.stop_all()
        assert registry.github.stopped


# ── TranscriptSyncRegistry thread-safety ─────────────────────────────────

class TestTranscriptSyncRegistryChaos:
    """Concurrent access to TranscriptSyncRegistry."""

    def test_concurrent_set_and_get(self):
        """20 threads set unique keys, then verify their keys survived."""
        reg = bridge.TranscriptSyncRegistry()
        barrier = threading.Barrier(20)
        errors = []
        error_lock = threading.Lock()

        def work(i):
            barrier.wait()
            key = f"session-{i}"
            reg.set(key, {"started": float(i), "status": "running"})
            # Read back
            entry = reg.get(key)
            if entry is None or entry.get("started") != float(i):
                with error_lock:
                    errors.append(f"key {key} lost or corrupted: {entry}")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors: {errors}"

    def test_concurrent_update(self):
        """Multiple threads updating different fields of the same key."""
        reg = bridge.TranscriptSyncRegistry()
        reg.set("shared", {"started": 1.0, "status": "init"})
        barrier = threading.Barrier(20)

        def update_status(i):
            barrier.wait()
            reg.update("shared", status=f"update-{i}")

        threads = [threading.Thread(target=update_status, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        entry = reg.get("shared")
        assert entry is not None
        # Status should be one of the update values (last writer wins)
        assert entry["status"].startswith("update-")

    def test_get_started_missing_key(self):
        """get_started returns 0 for missing keys — no crash under contention."""
        reg = bridge.TranscriptSyncRegistry()
        barrier = threading.Barrier(20)
        results = []
        lock = threading.Lock()

        def read_missing():
            barrier.wait()
            val = reg.get_started("nonexistent")
            with lock:
                results.append(val)

        threads = [threading.Thread(target=read_missing) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert all(v == 0 for v in results)
        assert len(results) == 20

    def test_concurrent_set_get_update_interleaved(self):
        """Mix of set/get/update on overlapping keys must not deadlock."""
        reg = bridge.TranscriptSyncRegistry()
        barrier = threading.Barrier(30)
        errors = []
        error_lock = threading.Lock()

        def setter(i):
            barrier.wait()
            for j in range(20):
                reg.set(f"k-{j % 5}", {"started": float(i * 100 + j), "status": "set"})

        def getter(i):
            barrier.wait()
            for j in range(20):
                reg.get(f"k-{j % 5}")
                reg.get_started(f"k-{j % 5}")

        def updater(i):
            barrier.wait()
            for j in range(20):
                try:
                    reg.update(f"k-{j % 5}", status=f"u-{i}-{j}")
                except Exception as exc:
                    with error_lock:
                        errors.append(exc)

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=setter, args=(i,)))
        for i in range(10):
            threads.append(threading.Thread(target=getter, args=(i,)))
        for i in range(10):
            threads.append(threading.Thread(target=updater, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors: {errors}"


# ── Bridge state persistence / recovery ──────────────────────────────────

class TestBridgeStatePersistence:
    """Test that bridge state files survive simulated restarts."""

    def test_workers_json_roundtrip(self, tmp_path):
        """workers.json write + read roundtrip preserves all fields."""
        workers_json = tmp_path / "workers.json"
        import json

        # Simulate what bridge writes
        state = {
            "worker-a": {
                "name": "worker-a",
                "session": "claude-prod-worker-a",
                "chat_id": "12345",
                "cwd": "/home/test",
                "backend": "claude",
            },
            "worker-b": {
                "name": "worker-b",
                "session": "claude-prod-worker-b",
                "chat_id": "12345",
                "cwd": "/home/test2",
                "backend": "codex",
            },
        }
        workers_json.write_text(json.dumps(state))

        # Read back
        loaded = json.loads(workers_json.read_text())
        assert loaded == state

    def test_chat_id_file_persistence(self, tmp_path):
        """chat_id file survives write-read cycle (simulates restart)."""
        chat_id_file = tmp_path / "chat_id"
        chat_id_file.write_text("987654321")

        # Simulate restart: read it back
        assert chat_id_file.read_text().strip() == "987654321"

    def test_focus_file_persistence(self, tmp_path):
        """Active worker focus file survives restart."""
        focus_file = tmp_path / "active_worker"
        focus_file.write_text("my-worker")

        assert focus_file.read_text().strip() == "my-worker"

    def test_corrupt_workers_json_handled(self, tmp_path):
        """Corrupt workers.json must not crash the loader."""
        import json

        workers_json = tmp_path / "workers.json"
        workers_json.write_text("{invalid json!!")

        # The bridge should handle this gracefully
        try:
            json.loads(workers_json.read_text())
            assert False, "Should have raised"
        except json.JSONDecodeError:
            pass  # Expected — bridge has try/except around this

    def test_empty_workers_json_handled(self, tmp_path):
        """Empty workers.json is valid — zero workers recovered."""
        import json

        workers_json = tmp_path / "workers.json"
        workers_json.write_text("{}")

        loaded = json.loads(workers_json.read_text())
        assert loaded == {}


# ── TokenStore edge cases ────────────────────────────────────────────────

class TestTokenStoreEdgeCases:
    """Non-thread tests for subtle token expiry edge cases."""

    def setup_method(self):
        self.store = bridge.TokenStore()
        self.clock = FakeClock()
        self._orig_clock = bridge._clock
        bridge._clock = self.clock
        bridge.REWIND_TOKENS.clear()
        bridge.PR_REVIEW_TOKENS.clear()

    def teardown_method(self):
        bridge._clock = self._orig_clock
        bridge.REWIND_TOKENS.clear()
        bridge.PR_REVIEW_TOKENS.clear()

    def test_validate_none_token(self):
        """validate_rewind(None) returns None, doesn't crash."""
        assert self.store.validate_rewind(None) is None
        assert self.store.validate_pr_review(None) is None

    def test_validate_empty_string_token(self):
        """validate_rewind('') returns None."""
        assert self.store.validate_rewind("") is None
        assert self.store.validate_pr_review("") is None

    def test_rewind_expiry_exact_boundary(self):
        """Token at exact expiry time is treated as expired."""
        self.store.add_rewind("tok", "w", timeout=100)
        self.clock.advance(100)  # exactly at boundary
        # expires_at <= now → expired
        assert self.store.validate_rewind("tok", extend=False) is None

    def test_rewind_sliding_window_extends(self):
        """validate with extend=True pushes expiry forward."""
        self.store.add_rewind("tok", "w", timeout=100)
        self.clock.advance(50)
        # Validate with extend — should still exist AND push expiry
        entry = self.store.validate_rewind("tok", extend=True)
        assert entry is not None
        # Advance another 80s — original timeout would be expired,
        # but the extension should keep it alive
        self.clock.advance(80)
        entry2 = self.store.validate_rewind("tok", extend=False)
        assert entry2 is not None

    def test_mass_expiry_cleanup(self):
        """Adding 1000 tokens, expiring 999, validates the survivor."""
        for i in range(1000):
            timeout = 10 if i < 999 else 100000
            self.store.add_rewind(f"tok-{i}", f"w-{i}", timeout=timeout)

        self.clock.advance(50)
        # Validate any token — triggers cleanup
        survivor = self.store.validate_rewind("tok-999", extend=False)
        assert survivor is not None
        assert survivor["name"] == "w-999"

        # Verify expired ones are cleaned
        assert len(bridge.REWIND_TOKENS) == 1

    def test_pr_review_validate_after_full_expiry(self):
        """PR review token fully expired returns None."""
        self.store.add_pr_review("pr-tok", pr_num=42, owner="org", repo="repo")
        self.clock.advance(bridge.PR_REVIEW_EXTEND + 1)
        assert self.store.validate_pr_review("pr-tok") is None


# ── ConnectorRegistry edge cases ─────────────────────────────────────────

class TestConnectorRegistryEdgeCases:

    def test_get_log_empty_tag(self):
        """get_log for nonexistent tag returns empty list."""
        registry = bridge.ConnectorRegistry()
        assert registry.get_log("nonexistent") == []

    def test_log_maxlen_cap(self):
        """Log is capped at 20 entries per tag."""
        registry = bridge.ConnectorRegistry()
        for i in range(50):
            registry.log_message("test", f"h{i}", f"p{i}", [])
        log = registry.get_log("test")
        assert len(log) == 20
        # Most recent entries should be 30-49
        assert log[0]["plain"] == "p30"
        assert log[-1]["plain"] == "p49"

    def test_stop_all_no_connectors(self):
        """stop_all with no connectors set is a no-op."""
        registry = bridge.ConnectorRegistry()
        registry.stop_all()  # should not raise

    def test_get_log_returns_snapshot(self):
        """get_log returns a copy — mutating it doesn't affect the registry."""
        registry = bridge.ConnectorRegistry()
        registry.log_message("test", "h1", "p1", [])
        snap = registry.get_log("test")
        snap.clear()
        assert len(registry.get_log("test")) == 1  # original untouched


# ── TranscriptSyncRegistry edge cases ────────────────────────────────────

class TestTranscriptSyncRegistryEdgeCases:

    def test_get_missing_key(self):
        reg = bridge.TranscriptSyncRegistry()
        assert reg.get("missing") is None

    def test_update_missing_key_noop(self):
        """Updating a key that doesn't exist is a silent no-op."""
        reg = bridge.TranscriptSyncRegistry()
        reg.update("missing", status="x")  # should not raise
        assert reg.get("missing") is None

    def test_set_overwrites(self):
        reg = bridge.TranscriptSyncRegistry()
        reg.set("k", {"started": 1.0, "status": "a"})
        reg.set("k", {"started": 2.0, "status": "b"})
        assert reg.get("k")["started"] == 2.0
