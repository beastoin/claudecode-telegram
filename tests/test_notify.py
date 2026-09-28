"""Tests migrated from test.sh — notify/escape category."""
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


def test_html_escape():
    from bridge import escape_html

    assert escape_html('hello') == 'hello', 'Plain text unchanged'
    assert escape_html('<script>') == '&lt;script&gt;', 'Angle brackets escaped'
    assert escape_html('a & b') == 'a &amp; b', 'Ampersand escaped'
    assert escape_html('1 < 2 > 0') == '1 &lt; 2 &gt; 0', 'Mixed escaping'

    # Real-world case (code content)
    code = 'if (x < 10 && y > 5)'
    expected = 'if (x &lt; 10 &amp;&amp; y &gt; 5)'
    assert escape_html(code) == expected, f'Code escaping failed: {escape_html(code)}'

    # Already-escaped content (should double-escape)
    assert escape_html('&lt;') == '&amp;lt;', 'Already escaped gets re-escaped'
