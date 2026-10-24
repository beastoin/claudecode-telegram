"""Behavior tests for the RuntimeSession protocol and concrete implementations.

Tests verify: session creation, send receipt types, observation caching,
diagnostics collection, output binding, batch probing, and provider discovery.
"""
import subprocess
import time

import pytest
from unittest.mock import MagicMock, patch

from shared import (
    OutputBinding,
    ProbeOutcome,
    RuntimeActivity,
    RuntimeDiagnostics,
    RuntimeObservation,
    RuntimePresence,
    SendOutcome,
    SendReceipt,
)
from claudecode import (
    TmuxClaudeSession,
    TmuxCodexSession,
    TmuxProvider,
    create_session,
    runtime_provider,
)


class TestRuntimeTypes:
    def test_observation_is_frozen(self):
        obs = RuntimeObservation(RuntimePresence.PRESENT, RuntimeActivity.BUSY, ProbeOutcome.OK, 1.0)
        with pytest.raises(AttributeError):
            obs.presence = RuntimePresence.ABSENT  # type: ignore[misc]

    def test_diagnostics_is_frozen(self):
        diag = RuntimeDiagnostics(pane_text="hello")
        with pytest.raises(AttributeError):
            diag.pane_text = "bye"  # type: ignore[misc]

    def test_send_receipt_outcomes(self):
        submitted = SendReceipt(SendOutcome.SUBMITTED)
        queued = SendReceipt(SendOutcome.QUEUED, "codex dispatched")
        rejected = SendReceipt(SendOutcome.REJECTED, "session absent")
        assert submitted.outcome == SendOutcome.SUBMITTED
        assert queued.detail == "codex dispatched"
        assert rejected.outcome == SendOutcome.REJECTED

    def test_output_binding_fields(self):
        binding = OutputBinding("http://localhost:8271", "/tmp/sessions", 8271, "claude-test-", "claude")
        assert binding.bridge_url == "http://localhost:8271"
        assert binding.backend_name == "claude"


class TestCreateSession:
    def test_creates_claude_session(self):
        s = create_session("lee", "claude-prod-lee", "claude")
        assert isinstance(s, TmuxClaudeSession)
        assert s.worker_name == "lee"
        assert s.tmux_name == "claude-prod-lee"
        assert s.backend_name == "claude"
        assert s.is_interactive is True
        assert s.host is None

    def test_creates_codex_session(self):
        s = create_session("mon", "claude-prod-mon", "codex", host="mac-mini")
        assert isinstance(s, TmuxCodexSession)
        assert s.worker_name == "mon"
        assert s.backend_name == "codex"
        assert s.is_interactive is False
        assert s.host == "mac-mini"

    def test_unknown_backend_defaults_to_claude(self):
        s = create_session("x", "claude-prod-x", "opencode")
        assert isinstance(s, TmuxClaudeSession)


class TestTmuxClaudeSession:
    def test_send_returns_receipt_on_success(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        with patch("claudecode.tmux_exists", return_value=True), \
             patch("claudecode._remote_run") as mock_run, \
             patch("claudecode.tmux_send_message", return_value=True):
            mock_run.return_value = MagicMock(returncode=0, stdout="$ ")
            receipt = s.send("hello")
            assert receipt.outcome == SendOutcome.SUBMITTED

    def test_send_returns_rejected_when_session_absent(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        with patch("claudecode.tmux_exists", return_value=False):
            receipt = s.send("hello")
            assert receipt.outcome == SendOutcome.REJECTED
            assert "absent" in receipt.detail

    def test_snapshot_returns_unknown_without_probe(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        obs = s.snapshot()
        assert obs.presence == RuntimePresence.UNKNOWN
        assert obs.probe == ProbeOutcome.ERROR

    def test_snapshot_returns_cached_after_update(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        fresh = RuntimeObservation(RuntimePresence.PRESENT, RuntimeActivity.BUSY, ProbeOutcome.OK, time.time())
        s.update_observation(fresh)
        assert s.snapshot() is fresh
        assert s.snapshot().presence == RuntimePresence.PRESENT

    def test_diagnose_collects_pane_and_pid(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        with patch("claudecode._capture_pane_text", return_value="❯ claude\nThinking..."), \
             patch("claudecode.get_pane_command", return_value="claude"), \
             patch("claudecode._remote_run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            diag = s.diagnose(lines=20)
            assert "Thinking" in diag.pane_text
            assert diag.process_name == "claude"
            assert diag.pane_pid == "12345"

    def test_bind_output_calls_export_hook_env(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        binding = OutputBinding("http://localhost:8271", "/tmp/sessions", 8271, "claude-test-", "claude")
        with patch("claudecode.export_hook_env") as mock_export:
            s.bind_output(binding)
            mock_export.assert_called_once_with("claude-test-lee", backend="claude", host=None)

    def test_stop_kills_tmux_session(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        with patch("claudecode._remote_run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert s.stop() is True
            args = mock_run.call_args[0][0]
            assert args == ["tmux", "kill-session", "-t", "claude-test-lee"]


class TestTmuxCodexSession:
    def test_send_returns_queued_receipt(self):
        s = TmuxCodexSession("mon", "claude-test-mon")
        with patch("claudecode._codex_adapter_thread"):
            receipt = s.send("analyze this")
            assert receipt.outcome == SendOutcome.QUEUED
            assert "codex" in receipt.detail

    def test_send_dispatches_remote_thread_for_remote_host(self):
        s = TmuxCodexSession("mon", "claude-test-mon", host="mac-mini")
        with patch("claudecode._codex_adapter_remote") as mock_remote, \
             patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            s.send("hello")
            mock_thread.assert_called_once()
            call_kwargs = mock_thread.call_args
            assert call_kwargs[1]["target"] == mock_remote


class TestTmuxProvider:
    def test_probe_many_batches_by_host(self):
        local_s = TmuxClaudeSession("lee", "claude-test-lee")
        remote_s = TmuxClaudeSession("x", "claude-test-x", host="mac-mini")
        provider = TmuxProvider()
        with patch("claudecode._tmux_pane_pids") as mock_pids, \
             patch("claudecode.is_process_running", return_value=True):
            mock_pids.side_effect = [
                {"claude-test-lee": "111"},
                {"claude-test-x": "222"},
            ]
            results = provider.probe_many([local_s, remote_s])
            assert mock_pids.call_count == 2
            assert results["lee"].presence == RuntimePresence.PRESENT
            assert results["lee"].activity == RuntimeActivity.BUSY
            assert results["x"].presence == RuntimePresence.PRESENT

    def test_probe_many_marks_absent_when_not_found(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        provider = TmuxProvider()
        with patch("claudecode._tmux_pane_pids", return_value={}):
            results = provider.probe_many([s])
            assert results["lee"].presence == RuntimePresence.ABSENT
            assert results["lee"].probe == ProbeOutcome.OK

    def test_probe_updates_session_observation(self):
        s = TmuxClaudeSession("lee", "claude-test-lee")
        provider = TmuxProvider()
        with patch("claudecode._tmux_pane_pids", return_value={"claude-test-lee": "111"}), \
             patch("claudecode.is_process_running", return_value=False):
            provider.probe_many([s])
            obs = s.snapshot()
            assert obs.presence == RuntimePresence.PRESENT
            assert obs.activity == RuntimeActivity.IDLE

    def test_discover_sessions_delegates_to_tmux_pane_pids(self):
        provider = TmuxProvider()
        with patch("claudecode._tmux_pane_pids", return_value={"s1": "111", "s2": "222"}) as mock:
            result = provider.discover_sessions()
            assert result == {"s1": "111", "s2": "222"}
            mock.assert_called_once_with(host=None)

    def test_codex_session_probe_marks_idle(self):
        s = TmuxCodexSession("mon", "claude-test-mon")
        provider = TmuxProvider()
        with patch("claudecode._tmux_pane_pids", return_value={"claude-test-mon": "333"}):
            results = provider.probe_many([s])
            assert results["mon"].activity == RuntimeActivity.IDLE


class TestRuntimeProviderSingleton:
    def test_runtime_provider_is_tmux_provider(self):
        assert isinstance(runtime_provider, TmuxProvider)
