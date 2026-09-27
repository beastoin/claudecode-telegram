"""Behavior tests for worker-to-worker messaging guardrails.

Covers: /response source validation rejects misuse,
welcome message warns against /response for w2w.
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


# ── _validate_response_source ────────────────────────────────────────

class TestValidateResponseSource:
    """The /response endpoint must reject worker-to-worker misuse."""

    def _validate(self, data, session_name):
        import bridge
        handler = MagicMock(spec=bridge.Handler)
        handler._validate_response_source = bridge.Handler._validate_response_source.__get__(handler)
        return handler._validate_response_source(data, session_name)

    def test_valid_hook_payload_passes(self):
        """Legitimate hook payload: source matches session."""
        result = self._validate(
            {"session": "finn", "text": "hello", "source": "finn"},
            "finn",
        )
        assert result == ""

    def test_missing_source_rejected(self):
        """Payload without source field is rejected."""
        result = self._validate(
            {"session": "sui", "text": "hello"},
            "sui",
        )
        assert "Missing source" in result

    def test_source_session_mismatch_rejected(self):
        """The exact finn→sui misroute: source=finn, session=sui."""
        result = self._validate(
            {"session": "sui", "text": "hey sui", "source": "finn"},
            "sui",
        )
        assert "mismatch" in result
        assert "finn" in result
        assert "sui" in result

    def test_messaging_fields_rejected(self):
        """Payload with worker/to/from fields is rejected."""
        for field in ("worker", "to", "target", "message", "from"):
            result = self._validate(
                {"session": "lee", "text": "hello", "source": "lee", field: "value"},
                "lee",
            )
            assert "cannot address workers" in result
            assert field in result

    def test_messaging_fields_checked_before_source(self):
        """Messaging field check fires before source check."""
        result = self._validate(
            {"session": "sui", "text": "hi", "worker": "sui", "from": "finn"},
            "sui",
        )
        assert "cannot address workers" in result
        assert "worker" in result
        assert "from" in result


# ── Welcome message routing instructions ─────────────────────────────

class TestWelcomeMessageRouting:
    """Checkin/welcome message steers workers to p2p send_example."""

    def _get_welcome(self, name="finn"):
        import bridge
        mgr = MagicMock(spec=bridge.WorkerManager)
        mgr._build_welcome = bridge.WorkerManager._build_welcome.__get__(mgr)
        backend_obj = MagicMock()
        backend_obj.is_interactive = True
        with patch.object(bridge, "read_checkin_note", return_value=""), \
             patch.object(bridge, "SANDBOX_ENABLED", False):
            return mgr._build_welcome(name, backend_obj)

    def test_welcome_mentions_send_example(self):
        """Welcome text tells workers about send_example from /workers."""
        welcome = self._get_welcome()
        assert "send_example" in welcome

    def test_welcome_warns_against_response_misuse(self):
        """Welcome text warns workers not to use /response for w2w."""
        welcome = self._get_welcome()
        assert "/response" in welcome
        assert "Never use POST /response to message another worker" in welcome

    def test_welcome_directs_to_workers_endpoint(self):
        """Welcome tells workers to call /workers?from=<name> before messaging."""
        welcome = self._get_welcome("finn")
        assert "/workers?from=finn" in welcome
