"""Tests migrated from test.sh — transcript category."""
import base64
import json
import os
import re
import sqlite3
import sys
import tempfile
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


def test_transcript_renders_html():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Hello world'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'test-sid', 'version': '2.1.85', 'gitBranch': 'main'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi there! How can I help?'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'test-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='test-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker')

        assert '<!DOCTYPE html>' in html, 'Missing DOCTYPE'
        assert 'testworker' in html, 'Missing worker name'
        assert 'Hello world' in html, 'Missing user message'
        # Assistant text is base64-encoded in data-md attr (rendered client-side by marked.js)
        md_vals = [base64.b64decode(m).decode() for m in re.findall(r'data-md="([^"]+)"', html)]
        assert any('Hi there!' in v for v in md_vals), f'Missing assistant reply in data-md: {md_vals}'
        assert 'claude-opus-4-6' in html, 'Missing model name'
        assert 'user-msg' in html, 'Missing user CSS class'
        assert 'a-text' in html, 'Missing assistant CSS class'


def test_transcript_missing_session():
    import bridge

    sys.path.insert(0, os.getcwd())

    with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/x'), \
         patch.object(bridge, 'get_claude_session_id', return_value=''):
        html = bridge._render_transcript_html('nobody')

    assert 'No session found' in html, f'Expected error message, got: {html[:200]}'


def test_transcript_with_tool_calls():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Read my file'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'tool-sid', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_abc123', 'name': 'Read', 'input': {'file_path': '/tmp/hello.txt'}}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_abc123', 'content': 'file contents here', 'is_error': False}]}, 'timestamp': '2026-04-05T10:00:02Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'Let me analyze this file...'}, {'type': 'text', 'text': 'The file contains hello.'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:03Z'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd2')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'tool-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd2'), \
             patch.object(bridge, 'get_claude_session_id', return_value='tool-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('toolworker')

        assert 'hello.txt' in html, 'Missing file path'
        # Result merged into tool block (no separate t-result)
        assert 't-result' not in html, 'Separate t-result should not exist (merged into tool block)'
        assert 'file contents here' in html, 'Missing tool output merged into tool block'
        assert 'act' in html, 'Read with result should become expandable act block'
        assert 't-name' not in html, 'Tool name text should not appear (icon-only per AmpCode)'
        assert 'Thinking' in html, 'Missing thinking block'
        md_vals = [base64.b64decode(m).decode() for m in re.findall(r'data-md="([^"]+)"', html)]
        assert any('The file contains hello.' in v for v in md_vals), f'Missing final text in data-md: {md_vals}'
        assert '1 tool call' in html, 'Missing tool call count'


def test_transcript_default_last_page():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Create 120 entries so we get multiple pages at per_page=50
    entries = []
    for i in range(120):
        entries.append({'type': 'user', 'message': {'role': 'user', 'content': f'Message {i}'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'page-sid', 'version': '2.1.85'})

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'page-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='page-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            # page=None = default = last page
            html = bridge._render_transcript_html('testworker', per_page=50)

        # 120 entries / 50 = 3 pages. Default should show page 3
        assert 'Message 119' in html, 'Last message not on default page'
        assert 'Message 0' not in html, 'First message should NOT be on last page'
        assert 'pg-cur' in html, 'Missing pagination current marker'


def test_transcript_bm25_search():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'general conversation about weather'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'bm25-sid', 'version': '2.1.85'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'teleport teleport teleport worker to mac'}, 'timestamp': '2026-04-05T10:00:01Z', 'sessionId': 'bm25-sid'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'one mention of teleport here'}, 'timestamp': '2026-04-05T10:00:02Z', 'sessionId': 'bm25-sid'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'bm25-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='bm25-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker', page=1, search_query='teleport')

        assert 'sorted by relevance' in html, 'Missing relevance info'
        assert 'Found 2 matching' in html, 'Expected 2 results'
        # The entry with 3x teleport should rank higher (appear first)
        pos_3x = html.find('teleport teleport teleport')
        pos_1x = html.find('one mention of teleport')
        assert pos_3x < pos_1x, f'Higher TF entry should rank first: {pos_3x} vs {pos_1x}'


def test_transcript_search_assistant_ctx_link():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'tell me about deployments'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'ctx-sid', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Here is the deployment status for your cluster'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'what about the database?'}, 'timestamp': '2026-04-05T10:00:02Z', 'sessionId': 'ctx-sid'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'The database deployment is running normally'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:03Z'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'ctx-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='ctx-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker', page=1, search_query='deployment')

        # Both assistant entries mention deployment — they should have ctx-wrap links
        assert 'ctx-wrap' in html, 'Missing ctx-wrap links in search results'
        # Assistant entries have class 'a-text' — verify they are inside ctx-wrap anchors
        # ctx-wrap links that contain assistant content (a-text divs)
        assistant_ctx = re.findall(r'<a class="ctx-wrap"[^>]*>.*?class="a-text', html, re.DOTALL)
        assert len(assistant_ctx) >= 1, f'Expected assistant entries wrapped in ctx-wrap, got {len(assistant_ctx)}: search should make assistant results clickable'


def test_transcript_search_sort_toggle():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'first deploy question'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'sort-sid', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'deploy info here'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'general conversation no match'}, 'timestamp': '2026-04-05T10:00:02Z', 'sessionId': 'sort-sid'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'deploy deploy deploy many mentions'}, 'timestamp': '2026-04-05T10:00:03Z', 'sessionId': 'sort-sid'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'sort-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='sort-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            # Default (relevance): high-TF entry should come first
            html_rel = bridge._render_transcript_html('testworker', page=1, search_query='deploy', search_sort='relevance')
            # Time sort: entries in chronological order
            html_time = bridge._render_transcript_html('testworker', page=1, search_query='deploy', search_sort='time')

        # Relevance: 'deploy deploy deploy' (idx 3, more TF) before 'first deploy' (idx 0)
        pos_many_rel = html_rel.find('deploy deploy deploy')
        pos_first_rel = html_rel.find('first deploy')
        assert pos_many_rel < pos_first_rel, f'Relevance sort should put high-TF first: {pos_many_rel} vs {pos_first_rel}'

        # Time: 'first deploy' (idx 0) before 'deploy deploy deploy' (idx 3)
        pos_first_time = html_time.find('first deploy')
        pos_many_time = html_time.find('deploy deploy deploy')
        assert pos_first_time < pos_many_time, f'Time sort should put earlier entry first: {pos_first_time} vs {pos_many_time}'

        # Both HTML pages should have sort toggle links
        assert 'sort=time' in html_rel, 'Relevance page should have link to switch to time sort'
        assert 'sort=relevance' in html_time, 'Time page should have link to switch to relevance sort'


def test_transcript_unicode():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'こんにちは世界 🌍 café résumé naïve'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'utf8-sid', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Héllo! 你好 🎉 Ñoño über Straße'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'utf8-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='utf8-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker')

        # User message should have unicode rendered directly (HTML-escaped)
        assert 'こんにちは世界' in html, 'Missing Japanese text in user message'
        assert '🌍' in html, 'Missing emoji in user message'
        assert 'café' in html, 'Missing accented text'
        # Assistant text is base64 — verify the data-md roundtrips UTF-8
        md_vals = [base64.b64decode(m).decode('utf-8') for m in re.findall(r'data-md="([^"]+)"', html)]
        assert any('你好' in v for v in md_vals), f'Missing Chinese text in data-md: {md_vals}'
        assert any('🎉' in v for v in md_vals), f'Missing emoji in data-md: {md_vals}'
        assert any('Straße' in v for v in md_vals), f'Missing German text in data-md: {md_vals}'
        # Verify decodeB64Utf8 function is in the JS
        assert 'decodeB64Utf8' in html, 'Missing UTF-8 base64 decoder function'


def test_transcript_edit_diff_rendering():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Fix the bug'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'diff-sid', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_abc', 'name': 'Edit', 'input': {'file_path': '/home/user/src/app.py', 'old_string': 'x = 1\ny = 2', 'new_string': 'x = 10\ny = 20\nz = 30'}}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'diff-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='diff-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker')

        assert 'diff-add' in html, 'Missing diff-add class'
        assert 'diff-del' in html, 'Missing diff-del class'
        assert 'diff-stat' in html, 'Missing diff stats'
        # Per-edit: old=2, new=3 → overlap=2, pure_add=1, pure_del=0
        assert '+1' in html, 'Missing add count (+1 pure additions)'
        assert '~2' in html, 'Missing mod count (~2 modified lines)'
        assert 'app.py' in html, 'Missing filename in diff header'


def test_transcript_turn_grouping():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Hello'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'grp-sid', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi!'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': '/tmp/test.txt'}}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:02Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'file content'}]}, 'timestamp': '2026-04-05T10:00:03Z'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'grp-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='grp-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker')

        thread_html = html[html.find('id="thread"'):]
        # Assistant content grouped in turn-body (no avatar — AmpCode style)
        assert 'turn-body' in thread_html, 'Missing turn-body grouping'
        # No Claude avatar (removed per AmpCode match)
        assert 'cl-av' not in thread_html, 'Should not have Claude avatar'
        # User message should have image avatar
        assert 'class="u-av"' in thread_html, 'Missing user avatar'
        assert 'u-label' not in html, 'Should not have Human label (AmpCode: no labels)'
        # Tool result merged into tool block inside turn-body
        assert 't-result' not in thread_html, 'No separate t-result (merged into tool block)'
        tb_start = thread_html.find('turn-body')
        act_pos = thread_html.find('class="act"', tb_start)
        assert tb_start < act_pos, 'Tool block (with merged result) should be inside turn-body'
        assert 'file content' in thread_html, 'Merged result content should appear in tool block'


def test_transcript_hides_system_messages():
    import bridge

    sys.path.insert(0, os.getcwd())

    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Hello world'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'sys-sid', 'version': '2.1.85'},
        {'type': 'user', 'message': {'role': 'user', 'content': '<task-notification>\n<task-id>abc123</task-id>\n<status>completed</status>\n<summary>Background command done</summary>\n</task-notification>'}, 'timestamp': '2026-04-05T10:00:01Z', 'sessionId': 'sys-sid'},
        {'type': 'user', 'message': {'role': 'user', 'content': '<system-reminder>\nSome internal reminder\n</system-reminder>'}, 'timestamp': '2026-04-05T10:00:02Z', 'sessionId': 'sys-sid'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'Real user message'}, 'timestamp': '2026-04-05T10:00:03Z', 'sessionId': 'sys-sid'},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        slug = bridge._project_slug('/tmp/testcwd')
        project_dir = Path(tmpdir) / '.claude' / 'projects' / slug
        project_dir.mkdir(parents=True)
        transcript = project_dir / 'sys-sid.jsonl'
        with open(transcript, 'w') as f:
            for e in entries:
                f.write(json.dumps(e) + '\n')

        with patch.object(bridge, 'get_claude_session_cwd', return_value='/tmp/testcwd'), \
             patch.object(bridge, 'get_claude_session_id', return_value='sys-sid'), \
             patch('pathlib.Path.home', return_value=Path(tmpdir)):
            html = bridge._render_transcript_html('testworker')

        assert 'Hello world' in html, 'Real user message should appear'
        assert 'Real user message' in html, 'Second real user message should appear'
        assert 'task-notification' not in html, 'task-notification should be hidden'
        assert 'system-reminder' not in html, 'system-reminder should be hidden'
        assert 'Background command done' not in html, 'Task summary should be hidden'
        # Count user-msg divs - should be exactly 2 (the real messages)
        count = html.count('class="user-msg"')
        assert count == 2, f'Expected 2 user messages, got {count}'


def test_transcript_prompts_filter():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Create transcript with user + assistant entries
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        entries = [
            {'type': 'user', 'message': {'role': 'user', 'content': 'hello world'}, 'timestamp': '2026-04-05T10:00:00Z'},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi!'}]}, 'timestamp': '2026-04-05T10:00:01Z'},
            {'type': 'user', 'message': {'role': 'user', 'content': 'second prompt'}, 'timestamp': '2026-04-05T10:01:00Z'},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Ok'}]}, 'timestamp': '2026-04-05T10:01:01Z'},
        ]
        for e in entries:
            f.write(json.dumps(e) + '\n')
        tpath = f.name

    try:
        with patch.object(bridge, '_resolve_transcript_path', return_value=(tpath, 'test-sid', '/tmp')), \
             patch('transcript._resolve_transcript_path', return_value=(tpath, 'test-sid', '/tmp')):
            # Without filter: shows all entries
            html_all = bridge._render_transcript_html('test')
            assert 'hello world' in html_all
            assert 'a-text markdown' in html_all  # assistant blocks visible

            # With filter=prompts: only user messages
            html_filt = bridge._render_transcript_html('test', filter_mode='prompts')
            assert 'hello world' in html_filt
            assert 'second prompt' in html_filt
            assert 'Showing prompts only' in html_filt
            # No assistant text blocks in the content area (before script tag)
            assert 'a-text markdown' not in html_filt.split('<script>')[0]
    finally:
        os.unlink(tpath)


def test_transcript_dynamic_avatars():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Test _detect_message_author
    author, av, display = bridge._detect_message_author('ryo: fix the bug')
    assert author == 'ryo', f'Expected ryo, got {author}'
    assert 'viewBox' in av, 'Team member should get SVG avatar'
    assert display == 'fix the bug', f'Display should strip prefix, got: {display}'

    # Manager prefix
    author2, av2, display2 = bridge._detect_message_author('manager: check this')
    assert author2 == 'manager', f'Expected manager, got {author2}'
    assert 'github' in av2.lower() or 'img' in av2, 'Manager should get GitHub avatar'
    assert display2 == 'check this'

    # No prefix → default manager
    author3, av3, display3 = bridge._detect_message_author('just a plain message')
    assert author3 == 'manager'
    assert 'img' in av3

    # Test avatar uniqueness
    av_ryo = bridge._generate_member_avatar('ryo')
    av_mon = bridge._generate_member_avatar('mon')
    av_lee = bridge._generate_member_avatar('lee')
    assert av_ryo != av_mon, 'Different members should have different avatars'
    assert av_ryo != av_lee
    assert av_mon != av_lee

    # Verify determinism
    assert bridge._generate_member_avatar('ryo') == av_ryo, 'Same name should give same avatar'

    # Verify 100+ unique variants
    avatars = set()
    for name in list(bridge._TEAM_MEMBERS) + ['alice', 'bob', 'carol', 'dave', 'eve', 'frank', 'grace',
        'heidi', 'ivan', 'judy', 'karl', 'larry', 'mallory', 'nancy', 'oscar', 'peggy',
        'quinn', 'romeo', 'steve', 'trudy', 'ursula', 'victor', 'wendy', 'xavier', 'yvonne', 'zach',
        'alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta', 'iota', 'kappa',
        'lambda', 'mu', 'nu', 'xi', 'omicron', 'pi', 'rho', 'sigma', 'tau', 'upsilon', 'phi',
        'chi', 'psi', 'omega', 'ant', 'bee', 'cat', 'dog', 'elk', 'fox', 'gnu', 'hen', 'ibis',
        'jay', 'koi', 'lynx', 'moth', 'newt', 'owl', 'pig', 'quail', 'ram', 'seal', 'toad',
        'urchin', 'vole', 'wolf', 'yak', 'zebu', 'atom', 'bolt', 'cog', 'dart', 'edge',
        'flux', 'grit', 'haze', 'icon', 'jest', 'knot', 'lens', 'mist', 'node', 'opus',
        'pulse', 'rift', 'shard', 'tide', 'unit', 'vex', 'warp', 'xray', 'yield', 'zinc']:
        avatars.add(bridge._generate_member_avatar(name))
    assert len(avatars) >= 100, f'Expected 100+ unique avatars, got {len(avatars)}'


def test_transcript_sidebar_stats():
    import bridge

    sys.path.insert(0, os.getcwd())

    # Create transcript with usage data and timestamps spread over 2 hours
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        entries = [
            {'type': 'user', 'message': {'role': 'user', 'content': 'test'}, 'timestamp': '2026-04-05T10:00:00Z'},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'reply'}],
             'usage': {'input_tokens': 5000, 'cache_read_input_tokens': 3000, 'output_tokens': 1200}},
             'timestamp': '2026-04-05T12:30:00Z'},
        ]
        for e in entries:
            f.write(json.dumps(e) + '\n')
        tpath = f.name

    try:
        with patch.object(bridge, '_resolve_transcript_path', return_value=(tpath, 'test-sid', '/tmp')), \
             patch('transcript._resolve_transcript_path', return_value=(tpath, 'test-sid', '/tmp')):
            html = bridge._render_transcript_html('test')
            # Duration should show 2h 30m
            assert '2h 30m' in html, 'Expected 2h 30m duration in sidebar'
            # Token counts
            assert '8,000' in html, 'Expected 8,000 input tokens (5000+3000)'
            assert '1,200' in html, 'Expected 1,200 output tokens'
            # File size should be present (small file)
            assert 'KB' in html or ' B' in html, 'Expected file size in sidebar'
    finally:
        os.unlink(tpath)


def test_tindex_missing_file(tmp_path):
    import subprocess

    db = tmp_path / "test-tindex-missing.db"
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", "/nonexistent/path.jsonl",
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on missing file: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['entries'] == [], f"Expected empty entries, got {d['entries']}"
    assert d['total'] == 0, f"Expected total=0, got {d['total']}"
    assert d['total_pages'] == 0
    assert d['page'] == 1


def test_tindex_empty_file(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-empty.jsonl"
    tmp.write_text("")
    db = tmp_path / "test-tindex-empty.db"
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on empty file: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['entries'] == [], 'Expected empty entries'
    assert d['total'] == 0


def test_tindex_basic_indexing(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-basic.jsonl"
    db = tmp_path / "test-tindex-basic.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Hello world'}, 'timestamp': '2026-04-05T10:00:00Z', 'sessionId': 'test', 'version': '2.1.85'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi there'}], 'model': 'claude-opus-4-6'}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'Thanks'}, 'timestamp': '2026-04-05T10:00:02Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on basic indexing: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['total'] == 3, f"Expected 3 entries, got {d['total']}"
    assert len(d['entries']) == 3
    assert d['entries'][0]['type'] == 'user'
    assert d['entries'][1]['type'] == 'assistant'
    # Verify SQLite db was created with correct rows
    conn = sqlite3.connect(str(db))
    count = conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
    assert count == 3, f"SQLite has {count} rows, expected 3"
    # Verify raw_json is valid JSON
    parsed = json.loads(d['entries'][0]['raw_json'])
    assert parsed['message']['content'] == 'Hello world'
    conn.close()


def test_tindex_skips_noise(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-noise.jsonl"
    db = tmp_path / "test-tindex-noise.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Hello'}, 'timestamp': '2026-04-05T10:00:00Z'},
        {'type': 'progress', 'data': {}},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi'}]}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'system', 'message': {'role': 'system', 'content': 'sys msg'}},
        {'type': 'queue-operation', 'data': {}},
        {'type': 'file-history-snapshot', 'data': {}},
        {'type': 'user', 'message': {'role': 'user', 'content': 'Bye'}, 'timestamp': '2026-04-05T10:00:02Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on noise entries: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['total'] == 3, f"Expected 3 (skipping noise), got {d['total']}"
    types = [e['type'] for e in d['entries']]
    assert 'progress' not in types
    assert 'system' not in types
    assert 'queue-operation' not in types


def test_tindex_plain_text_extraction(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-text.jsonl"
    db = tmp_path / "test-tindex-text.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Hello world'}, 'timestamp': '2026-04-05T10:00:00Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi there friend'}]}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'ls -la /tmp'}}]}, 'timestamp': '2026-04-05T10:00:02Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    conn = sqlite3.connect(str(db))
    rows = conn.execute('SELECT plain_text FROM entries ORDER BY idx').fetchall()
    assert 'Hello world' in rows[0][0], f"User text not extracted: {rows[0][0]}"
    assert 'Hi there friend' in rows[1][0], f"Assistant text not extracted: {rows[1][0]}"
    assert 'ls -la /tmp' in rows[2][0], f"Tool input not extracted: {rows[2][0]}"
    conn.close()


def test_tindex_incremental(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-incr.jsonl"
    db = tmp_path / "test-tindex-incr.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'First'}, 'timestamp': '2026-04-05T10:00:00Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Reply1'}]}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'Second'}, 'timestamp': '2026-04-05T10:00:02Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    # First index
    subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    # Append 2 more entries
    new = [
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Reply2'}]}, 'timestamp': '2026-04-05T10:00:03Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'Third'}, 'timestamp': '2026-04-05T10:00:04Z'},
    ]
    with open(tmp, 'a') as f:
        for e in new:
            f.write(json.dumps(e) + '\n')

    # Re-index (incremental)
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on incremental: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['total'] == 5, f"Expected 5 after incremental, got {d['total']}"
    assert d['entries'][0]['type'] == 'user'
    assert d['entries'][4]['type'] == 'user'


def test_tindex_no_reindex_unchanged(tmp_path):
    import subprocess
    import time

    tmp = tmp_path / "tindex-noop.jsonl"
    db = tmp_path / "test-tindex-noop.db"
    tmp.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'test'}, 'timestamp': '2026-04-05T10:00:00Z'}) + '\n')

    subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    time.sleep(1)
    # Run again — should skip indexing
    subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    # Note: mtime may change due to SQLite WAL, so just check total is still 1
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0
    d = json.loads(result.stdout)
    assert d['total'] == 1, f"Expected 1, got {d['total']}"


def test_tindex_pagination(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-page.jsonl"
    db = tmp_path / "test-tindex-page.db"
    with open(tmp, 'w') as f:
        for i in range(120):
            e = {'type': 'user', 'message': {'role': 'user', 'content': f'Message {i}'}, 'timestamp': f'2026-04-05T10:{i//60:02d}:{i%60:02d}Z'}
            f.write(json.dumps(e) + '\n')

    # Default (no --page) should be last page
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries", "--per-page", "50"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    d = json.loads(result.stdout)
    assert d['total'] == 120
    assert d['total_pages'] == 3
    assert d['page'] == 3, f"Default page should be 3 (last), got {d['page']}"
    assert len(d['entries']) == 20, f"Last page should have 20 entries, got {len(d['entries'])}"

    # Explicit page 1
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries", "--page", "1", "--per-page", "50"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    d = json.loads(result.stdout)
    assert d['page'] == 1
    assert len(d['entries']) == 50
    assert json.loads(d['entries'][0]['raw_json'])['message']['content'] == 'Message 0'

    # Page 2
    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries", "--page", "2", "--per-page", "50"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    d = json.loads(result.stdout)
    assert d['page'] == 2
    assert len(d['entries']) == 50
    assert json.loads(d['entries'][0]['raw_json'])['message']['content'] == 'Message 50'


def test_tindex_fts5_search(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-search.jsonl"
    db = tmp_path / "test-tindex-search.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'general conversation about weather'}, 'timestamp': '2026-04-05T10:00:00Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'teleport teleport teleport worker to mac'}]}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'one mention of teleport here'}, 'timestamp': '2026-04-05T10:00:02Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "search", "--search", "teleport"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on search: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['total_results'] == 2, f"Expected 2 results, got {d['total_results']}"
    # 3x teleport should rank higher (first)
    first_text = json.loads(d['entries'][0]['raw_json'])
    assert 'teleport teleport teleport' in str(first_text), '3x teleport should be first'


def test_tindex_search_no_results(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-nores.jsonl"
    db = tmp_path / "test-tindex-nores.db"
    tmp.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'Hello world'}, 'timestamp': '2026-04-05T10:00:00Z'}) + '\n')

    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "search", "--search", "xyznonexistent"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on empty search: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['total_results'] == 0
    assert d['entries'] == []


def test_tindex_filter_prompts(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-filter.jsonl"
    db = tmp_path / "test-tindex-filter.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Real prompt'}, 'timestamp': '2026-04-05T10:00:00Z'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Reply'}]}, 'timestamp': '2026-04-05T10:00:01Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': '<task-notification>system stuff</task-notification>'}, 'timestamp': '2026-04-05T10:00:02Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': '<system-reminder>internal</system-reminder>'}, 'timestamp': '2026-04-05T10:00:03Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'result'}]}, 'timestamp': '2026-04-05T10:00:04Z'},
        {'type': 'user', 'message': {'role': 'user', 'content': 'Another real prompt'}, 'timestamp': '2026-04-05T10:00:05Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries", "--filter", "prompts"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on filter: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['total'] == 2, f"Expected 2 prompts, got {d['total']}"
    texts = [json.loads(e['raw_json'])['message']['content'] for e in d['entries']]
    assert 'Real prompt' in texts
    assert 'Another real prompt' in texts


def test_tindex_stats(tmp_path):
    import subprocess

    tmp = tmp_path / "tindex-stats.jsonl"
    db = tmp_path / "test-tindex-stats.db"
    entries = [
        {'type': 'user', 'message': {'role': 'user', 'content': 'Fix the bug'}, 'timestamp': '2026-04-05T10:00:00Z', 'version': '2.1.85', 'gitBranch': 'main'},
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 't1', 'name': 'Edit', 'input': {'file_path': '/tmp/app.py', 'old_string': 'x = 1\ny = 2', 'new_string': 'x = 10\ny = 20\nz = 30'}},
        ], 'model': 'claude-opus-4-6', 'usage': {'input_tokens': 5000, 'cache_read_input_tokens': 3000, 'output_tokens': 1200}}, 'timestamp': '2026-04-05T12:30:00Z'},
    ]
    with open(tmp, 'w') as f:
        for e in entries:
            f.write(json.dumps(e) + '\n')

    result = subprocess.run(
        [sys.executable, "indexer.py", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "stats"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"indexer.py transcript crashed on stats: {result.stderr}"
    d = json.loads(result.stdout)
    assert d['n_user'] == 1, f"n_user={d['n_user']}"
    assert d['n_tool'] == 1, f"n_tool={d['n_tool']}"
    assert d['n_edit'] == 1, f"n_edit={d['n_edit']}"
    assert d['input_tokens'] == 8000, f"input_tokens={d['input_tokens']}"
    assert d['output_tokens'] == 1200, f"output_tokens={d['output_tokens']}"
    assert d['model'] == 'claude-opus-4-6', f"model={d['model']}"
    assert d['version'] == '2.1.85', f"version={d['version']}"
    assert d['duration'] == '2h 30m', f"duration={d['duration']}"
    assert d['n_files'] == 1
    # Per-edit: old=2, new=3 → overlap=2, add=1, del=0
    assert d['lines_add'] == 1, f"lines_add={d['lines_add']}"
    assert d['lines_mod'] == 2, f"lines_mod={d['lines_mod']}"
    assert d['lines_del'] == 0, f"lines_del={d['lines_del']}"


def test_sync_session_transcript_targets_single_session():
    import teleport
    import types
    from unittest.mock import patch

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

        # Mock _find_codex_transcript to return our test file
        with patch.object(bridge, '_find_codex_transcript', return_value=str(transcript_file)):
            messages = bridge._read_codex_transcript('alice')

        assert len(messages) == 2, f"expected 2 messages, got {len(messages)}"
        assert messages[0]['role'] == 'user', f"expected user: {messages[0]}"
        assert messages[0]['text'] == 'hello', f"text mismatch: {messages[0]}"
        assert messages[1]['role'] == 'assistant', f"expected assistant: {messages[1]}"
        assert messages[1]['text'] == 'hi there!', f"text mismatch: {messages[1]}"
    finally:
        bridge.SESSIONS_DIR = orig_sessions
        shutil.rmtree(tmpdir, ignore_errors=True)
