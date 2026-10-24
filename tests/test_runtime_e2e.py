"""End-to-end tests for the RuntimeSession protocol.

These tests create REAL tmux sessions, send REAL messages,
probe with the REAL TmuxProvider, and verify actual lifecycle behavior.
Full flow against real tmux. Codex adapter threads are blocked from
reaching the prod bridge (they'd POST to BRIDGE_URL/outputs → Telegram).
"""
import os
import subprocess
import time
import uuid
from unittest.mock import patch

import pytest

from shared import (
    OutputBinding,
    ProbeOutcome,
    RuntimeActivity,
    RuntimePresence,
    SendOutcome,
)
from claudecode import (
    TmuxClaudeSession,
    TmuxCodexSession,
    TmuxProvider,
    create_session,
    tmux_exists,
    _capture_pane_text,
)

TMUX_PREFIX = f"rt-e2e-{uuid.uuid4().hex[:6]}-"


def _tmux_new(name: str) -> str:
    """Create a real tmux session running bash, return full session name."""
    full = f"{TMUX_PREFIX}{name}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", full, "-x", "120", "-y", "30"],
        capture_output=True, check=True,
    )
    time.sleep(0.3)
    return full


def _tmux_kill(full_name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", full_name], capture_output=True)


def _tmux_read(full_name: str) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", full_name, "-p"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""


@pytest.fixture()
def tmux_session():
    """Create a real tmux session, yield the name, kill it after."""
    name = _tmux_new("worker")
    yield name
    _tmux_kill(name)


@pytest.fixture()
def two_tmux_sessions():
    """Create two real tmux sessions for batch probe tests."""
    s1 = _tmux_new("alice")
    s2 = _tmux_new("bob")
    yield s1, s2
    _tmux_kill(s1)
    _tmux_kill(s2)


class TestClaudeSessionE2E:
    """Full lifecycle: create session → send → snapshot → diagnose → stop."""

    def test_send_delivers_text_to_real_tmux(self, tmux_session):
        session = TmuxClaudeSession("e2e-worker", tmux_session)
        receipt = session.send("echo RUNTIME_PROTOCOL_TEST_MARKER")
        assert receipt.outcome == SendOutcome.SUBMITTED
        time.sleep(0.5)
        pane = _tmux_read(tmux_session)
        assert "RUNTIME_PROTOCOL_TEST_MARKER" in pane

    def test_send_rejected_when_session_does_not_exist(self):
        session = TmuxClaudeSession("ghost", f"{TMUX_PREFIX}nonexistent-{uuid.uuid4().hex[:8]}")
        receipt = session.send("hello")
        assert receipt.outcome == SendOutcome.REJECTED

    def test_diagnose_captures_real_pane_text(self, tmux_session):
        subprocess.run(
            ["tmux", "send-keys", "-t", tmux_session, "echo DIAG_MARKER_42", "Enter"],
            capture_output=True,
        )
        time.sleep(0.3)
        session = TmuxClaudeSession("e2e-worker", tmux_session)
        diag = session.diagnose(lines=30)
        assert "DIAG_MARKER_42" in diag.pane_text
        assert diag.pane_pid != ""
        assert diag.process_name != ""

    def test_stop_kills_real_tmux_session(self):
        name = _tmux_new("stopme")
        session = TmuxClaudeSession("stopme", name)
        assert tmux_exists(name) is True
        result = session.stop()
        assert result is True
        time.sleep(0.3)
        assert tmux_exists(name) is False

    def test_bind_output_sets_tmux_environment(self, tmux_session):
        session = TmuxClaudeSession("e2e-worker", tmux_session)
        binding = OutputBinding(
            bridge_url="http://127.0.0.1:9999",
            sessions_dir="/tmp/test-sessions",
            port=9999,
            tmux_prefix="rt-e2e-",
            backend_name="claude",
        )
        session.bind_output(binding)
        for var in ("BRIDGE_URL", "WORKER_BACKEND", "PORT"):
            r = subprocess.run(
                ["tmux", "show-environment", "-t", tmux_session, var],
                capture_output=True, text=True,
            )
            assert r.returncode == 0, f"{var} should be set in tmux env"
            assert "=" in r.stdout, f"{var} should have a value"


class TestCodexSessionE2E:
    """Codex session: send dispatches async, tmux stays alive."""

    @patch("claudecode._codex_adapter_remote")
    @patch("claudecode._codex_adapter_thread")
    def test_send_returns_queued_for_real_session(self, _mock_thread, _mock_remote, tmux_session):
        session = TmuxCodexSession("e2e-codex", tmux_session)
        receipt = session.send("test query")
        assert receipt.outcome == SendOutcome.QUEUED

    def test_stop_kills_codex_tmux(self):
        name = _tmux_new("codex-stop")
        session = TmuxCodexSession("codex-stop", name)
        assert tmux_exists(name) is True
        session.stop()
        time.sleep(0.3)
        assert tmux_exists(name) is False


class TestProviderE2E:
    """TmuxProvider.probe_many against real tmux sessions."""

    def test_probe_detects_present_session(self, tmux_session):
        session = TmuxClaudeSession("e2e-probe", tmux_session)
        provider = TmuxProvider()
        results = provider.probe_many([session])
        assert "e2e-probe" in results
        obs = results["e2e-probe"]
        assert obs.presence == RuntimePresence.PRESENT
        assert obs.probe == ProbeOutcome.OK

    def test_probe_detects_absent_session(self):
        fake_name = f"{TMUX_PREFIX}gone-{uuid.uuid4().hex[:8]}"
        session = TmuxClaudeSession("e2e-gone", fake_name)
        provider = TmuxProvider()
        results = provider.probe_many([session])
        assert results["e2e-gone"].presence == RuntimePresence.ABSENT

    def test_probe_many_batches_two_real_sessions(self, two_tmux_sessions):
        s1_name, s2_name = two_tmux_sessions
        sess_a = TmuxClaudeSession("alice", s1_name)
        sess_b = TmuxClaudeSession("bob", s2_name)
        provider = TmuxProvider()
        results = provider.probe_many([sess_a, sess_b])
        assert len(results) == 2
        assert results["alice"].presence == RuntimePresence.PRESENT
        assert results["bob"].presence == RuntimePresence.PRESENT

    def test_probe_updates_session_snapshot(self, tmux_session):
        session = TmuxClaudeSession("e2e-snap", tmux_session)
        assert session.snapshot().presence == RuntimePresence.UNKNOWN
        provider = TmuxProvider()
        provider.probe_many([session])
        obs = session.snapshot()
        assert obs.presence == RuntimePresence.PRESENT
        assert obs.timestamp > 0

    def test_discover_finds_real_sessions(self, tmux_session):
        provider = TmuxProvider()
        discovered = provider.discover_sessions()
        assert tmux_session in discovered
        assert discovered[tmux_session].isdigit()

    def test_mixed_present_and_absent_in_single_probe(self, tmux_session):
        live = TmuxClaudeSession("live-worker", tmux_session)
        dead = TmuxClaudeSession("dead-worker", f"{TMUX_PREFIX}dead-{uuid.uuid4().hex[:8]}")
        provider = TmuxProvider()
        results = provider.probe_many([live, dead])
        assert results["live-worker"].presence == RuntimePresence.PRESENT
        assert results["dead-worker"].presence == RuntimePresence.ABSENT


class TestFullLifecycleE2E:
    """Complete worker lifecycle: create → bind → send → probe → diagnose → stop → verify gone."""

    def test_full_claude_lifecycle(self):
        tmux_name = _tmux_new("lifecycle")
        try:
            session = create_session("lifecycle-worker", tmux_name, "claude")
            assert session.is_interactive is True

            binding = OutputBinding("http://127.0.0.1:9999", "/tmp/sess", 9999, "rt-e2e-", "claude")
            session.bind_output(binding)

            receipt = session.send("echo LIFECYCLE_OK")
            assert receipt.outcome == SendOutcome.SUBMITTED
            time.sleep(0.5)

            provider = TmuxProvider()
            results = provider.probe_many([session])
            assert results["lifecycle-worker"].presence == RuntimePresence.PRESENT

            diag = session.diagnose()
            assert "LIFECYCLE_OK" in diag.pane_text

            obs = session.snapshot()
            assert obs.presence == RuntimePresence.PRESENT

            session.stop()
            time.sleep(0.3)
            assert tmux_exists(tmux_name) is False

            results = provider.probe_many([session])
            assert results["lifecycle-worker"].presence == RuntimePresence.ABSENT
        finally:
            _tmux_kill(tmux_name)

    def test_full_codex_lifecycle(self):
        tmux_name = _tmux_new("codex-life")
        try:
            session = create_session("codex-life", tmux_name, "codex")
            assert session.is_interactive is False

            with patch("claudecode._codex_adapter_thread"), \
                 patch("claudecode._codex_adapter_remote"):
                receipt = session.send("test codex query")
                assert receipt.outcome == SendOutcome.QUEUED

            provider = TmuxProvider()
            results = provider.probe_many([session])
            assert results["codex-life"].presence == RuntimePresence.PRESENT

            session.stop()
            time.sleep(0.3)
            assert tmux_exists(tmux_name) is False
        finally:
            _tmux_kill(tmux_name)

    def test_create_probe_stop_three_workers(self):
        names = []
        sessions = []
        try:
            for i in range(3):
                n = _tmux_new(f"batch-{i}")
                names.append(n)
                sessions.append(create_session(f"w{i}", n, "claude"))

            provider = TmuxProvider()
            results = provider.probe_many(sessions)
            assert len(results) == 3
            for i in range(3):
                assert results[f"w{i}"].presence == RuntimePresence.PRESENT

            sessions[1].stop()
            time.sleep(0.3)

            results = provider.probe_many(sessions)
            assert results["w0"].presence == RuntimePresence.PRESENT
            assert results["w1"].presence == RuntimePresence.ABSENT
            assert results["w2"].presence == RuntimePresence.PRESENT
        finally:
            for n in names:
                _tmux_kill(n)
