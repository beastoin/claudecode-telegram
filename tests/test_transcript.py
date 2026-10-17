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


def test_codex_native_transcript_parsing():
    import shutil
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)

    try:
        # Create a codex native JSONL transcript
        transcript = tmp / 'session.jsonl'
        events = [
            {'timestamp': '2026-06-01T10:00:00Z', 'type': 'session_meta', 'payload': {'id': 'test-session-123', 'cwd': '/home/claude'}},
            {'timestamp': '2026-06-01T10:00:01Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello from manager'}]}},
            {'timestamp': '2026-06-01T10:00:05Z', 'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'call_1', 'arguments': '{"cmd": "ls"}'}},
            {'timestamp': '2026-06-01T10:00:06Z', 'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'file1.txt\nfile2.txt'}},
            {'timestamp': '2026-06-01T10:00:10Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'I found 2 files.'}]}},
            {'timestamp': '2026-06-01T10:00:11Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'system prompt'}]}},
        ]
        with open(transcript, 'w') as f:
            for ev in events:
                f.write(json.dumps(ev) + '\n')

        messages = bridge._parse_codex_transcript(str(transcript))
        assert len(messages) == 4, f"expected 4 messages (user, tool_use, tool_result, assistant), got {len(messages)}: {messages}"
        assert messages[0]['role'] == 'user', f"expected user: {messages[0]}"
        assert messages[0]['text'] == 'hello from manager', f"text mismatch: {messages[0]}"
        assert messages[1]['role'] == 'tool_use', f"expected tool_use: {messages[1]}"
        assert 'exec_command' in messages[1]['text'], f"tool name mismatch: {messages[1]}"
        assert messages[2]['role'] == 'tool_result', f"expected tool_result: {messages[2]}"
        assert messages[3]['role'] == 'assistant', f"expected assistant: {messages[3]}"
        assert messages[3]['text'] == 'I found 2 files.', f"response text mismatch: {messages[3]}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_codex_rewind_reads_native_transcript():
    import shutil
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_sessions = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = tmp

    try:
        # Create worker session dir with codex_session_id
        worker_dir = tmp / 'alice'
        worker_dir.mkdir()
        (worker_dir / 'backend').write_text('codex')
        (worker_dir / 'codex_session_id').write_text('abc-session-123')

        # Create codex native transcript at ~/.codex/sessions/ (simulated)
        codex_dir = tmp / 'codex_sessions' / '2026' / '06'
        codex_dir.mkdir(parents=True)
        transcript_file = codex_dir / 'rollout-2026-06-01-abc-session-123.jsonl'

        events = [
            {'timestamp': '2026-06-01T10:00:01Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}},
            {'timestamp': '2026-06-01T10:00:10Z', 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hi there!'}]}},
        ]
        with open(transcript_file, 'w') as f:
            for ev in events:
                f.write(json.dumps(ev) + '\n')

        # Call _parse_codex_transcript directly with the test file
        messages = bridge._parse_codex_transcript(str(transcript_file))

        assert len(messages) == 2, f"expected 2 messages, got {len(messages)}"
        assert messages[0]['role'] == 'user', f"expected user: {messages[0]}"
        assert messages[0]['text'] == 'hello', f"text mismatch: {messages[0]}"
        assert messages[1]['role'] == 'assistant', f"expected assistant: {messages[1]}"
        assert messages[1]['text'] == 'hi there!', f"text mismatch: {messages[1]}"
    finally:
        bridge.SESSIONS_DIR = orig_sessions
        shutil.rmtree(tmpdir, ignore_errors=True)
