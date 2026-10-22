"""Transcript tests — sync, codex parsing, and viewer spawn.

Rendering and Python indexer tests removed — served by Bun rewind tool (rewind/).
"""
import json
import os
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

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


def test_sync_session_transcript_targets_single_session():
    import bridge as teleport

    # Capture rsync commands
    rsync_cmds = []

    class FakeRunner:
        def run(self, cmd, **kw):
            rsync_cmds.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        def Popen(self, *a, **kw):
            return types.SimpleNamespace(pid=1, communicate=lambda: ('', ''))

    # Sync a specific session — patch teleport's _subprocess_runner
    sid = 'd61370de-61b2-467b-ac92-d3c5a1e4cfca'
    with patch.object(teleport, '_subprocess_runner', FakeRunner()):
        teleport.sync_session_transcript(
            sid,
            source_cwd='/home/claude/mira-nex',
            target_cwd='/Users/agent/mira-nex',
            source_host=None,
            target_host='mac-mini',
        )

    # Filter to only rsync commands (skip the mkdir ssh command)
    rsync_only = [c for c in rsync_cmds if c[0] == 'rsync']

    # Should have exactly 2 rsync calls: one for .jsonl, one for subdir
    assert len(rsync_only) == 2, f"expected 2 rsync calls, got {len(rsync_only)}"

    # First: the JSONL file itself
    cmd1 = ' '.join(rsync_only[0])
    assert f'{sid}.jsonl' in cmd1, f"first rsync should target session JSONL, got {cmd1!r}"

    # Second: the session subdirectory
    cmd2 = ' '.join(rsync_only[1])
    assert f'{sid}/' in cmd2, f"second rsync should target session subdir, got {cmd2!r}"

    # Neither should glob all .jsonl files
    for cmd in rsync_only:
        cmd_str = ' '.join(cmd)
        assert '*.jsonl' not in cmd_str, f"should NOT sync all jsonl files, got {cmd_str!r}"


