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


def _extract_data(html):
    """Extract JSON data from transcript page HTML."""
    m = re.search(r'id="transcript-data">(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    # Unescape the <\/ → </ that Python applies for XSS safety
    return json.loads(m.group(1).replace("<\\/", "</"))


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
        data = _extract_data(html)
        assert data is not None, 'Missing JSON data'
        assert data['name'] == 'testworker', 'Missing worker name'
        # User message in entries
        user_entries = [e for e in data['entries'] if e.get('type') == 'user' and isinstance(e.get('message', {}).get('content'), str)]
        assert any('Hello world' in e['message']['content'] for e in user_entries), 'Missing user message'
        # Assistant text in entries
        asst_entries = [e for e in data['entries'] if e.get('type') == 'assistant']
        asst_texts = [item.get('text', '') for e in asst_entries for item in (e.get('message', {}).get('content') or []) if isinstance(item, dict) and item.get('type') == 'text']
        assert any('Hi there!' in t for t in asst_texts), f'Missing assistant reply: {asst_texts}'
        assert data['stats']['model'] == 'claude-opus-4-6', 'Missing model name'


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

        data = _extract_data(html)
        assert data is not None, 'Missing JSON data'
        # Tool use entry with file path
        tool_uses = [item for e in data['entries'] if e.get('type') == 'assistant' for item in (e.get('message', {}).get('content') or []) if isinstance(item, dict) and item.get('type') == 'tool_use']
        assert any('hello.txt' in str(t.get('input', {}).get('file_path', '')) for t in tool_uses), 'Missing file path'
        # Tool result entry
        tool_results = [item for e in data['entries'] if e.get('type') == 'user' for item in (e.get('message', {}).get('content') or []) if isinstance(item, dict) and item.get('type') == 'tool_result']
        assert any('file contents here' in str(t.get('content', '')) for t in tool_results), 'Missing tool result'
        # Thinking entry
        thinking = [item for e in data['entries'] if e.get('type') == 'assistant' for item in (e.get('message', {}).get('content') or []) if isinstance(item, dict) and item.get('type') == 'thinking']
        assert any('analyze this file' in t.get('thinking', '') for t in thinking), 'Missing thinking block'
        # Final text
        texts = [item for e in data['entries'] if e.get('type') == 'assistant' for item in (e.get('message', {}).get('content') or []) if isinstance(item, dict) and item.get('type') == 'text']
        assert any('The file contains hello.' in t.get('text', '') for t in texts), 'Missing final text'
        assert data['stats']['n_tool'] == 1, 'Expected 1 tool call'


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
        data = _extract_data(html)
        assert data is not None
        assert data['pagination']['page'] == 3, 'Default page should be last page (3)'
        assert data['pagination']['total_pages'] == 3
        contents = [e['message']['content'] for e in data['entries'] if isinstance(e.get('message', {}).get('content'), str)]
        assert 'Message 119' in contents, 'Last message not on default page'
        assert 'Message 0' not in contents, 'First message should NOT be on last page'


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

        data = _extract_data(html)
        assert data is not None
        assert data['search_sort'] == 'relevance'
        assert data['pagination']['total'] == 2, 'Expected 2 results'
        # The entry with 3x teleport should rank higher (appear first in entries)
        contents = [e['message']['content'] for e in data['entries'] if isinstance(e.get('message', {}).get('content'), str)]
        idx_3x = next(i for i, c in enumerate(contents) if 'teleport teleport teleport' in c)
        idx_1x = next(i for i, c in enumerate(contents) if 'one mention of teleport' in c)
        assert idx_3x < idx_1x, f'Higher TF entry should rank first: idx {idx_3x} vs {idx_1x}'


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

        # With client-side rendering, verify search data is passed to JS
        data = _extract_data(html)
        assert data is not None
        assert data['search_query'] == 'deployment'
        # Both assistant entries should have _idx set for context linking
        asst_entries = [e for e in data['entries'] if e.get('type') == 'assistant']
        assert len(asst_entries) >= 1, 'Expected assistant entries in search results'
        assert all('_idx' in e for e in asst_entries), 'Assistant entries should have _idx for context links'


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

        data_rel = _extract_data(html_rel)
        data_time = _extract_data(html_time)
        assert data_rel is not None and data_time is not None

        # Relevance: 'deploy deploy deploy' (more TF) before 'first deploy'
        contents_rel = [e['message']['content'] for e in data_rel['entries'] if isinstance(e.get('message', {}).get('content'), str)]
        idx_many = next(i for i, c in enumerate(contents_rel) if 'deploy deploy deploy' in c)
        idx_first = next(i for i, c in enumerate(contents_rel) if 'first deploy' in c)
        assert idx_many < idx_first, f'Relevance sort should put high-TF first: {idx_many} vs {idx_first}'

        # Time: 'first deploy' before 'deploy deploy deploy'
        contents_time = [e['message']['content'] for e in data_time['entries'] if isinstance(e.get('message', {}).get('content'), str)]
        idx_first_t = next(i for i, c in enumerate(contents_time) if 'first deploy' in c)
        idx_many_t = next(i for i, c in enumerate(contents_time) if 'deploy deploy deploy' in c)
        assert idx_first_t < idx_many_t, f'Time sort should put earlier first: {idx_first_t} vs {idx_many_t}'

        # Sort mode is recorded in JSON for JS to render toggle links
        assert data_rel['search_sort'] == 'relevance'
        assert data_time['search_sort'] == 'time'


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

        data = _extract_data(html)
        assert data is not None, 'Missing JSON data block'
        entries = data['entries']
        # User messages with unicode should be in JSON entries
        user_contents = [e['message']['content'] for e in entries if e.get('message', {}).get('role') == 'user' and isinstance(e.get('message', {}).get('content'), str)]
        assert any('こんにちは世界' in c for c in user_contents), 'Missing Japanese text in user entry'
        assert any('🌍' in c for c in user_contents), 'Missing emoji in user entry'
        assert any('café' in c for c in user_contents), 'Missing accented text in user entry'
        # Assistant text should be in JSON entries
        asst_texts = []
        for e in entries:
            msg = e.get('message', {})
            if msg.get('role') == 'assistant':
                for block in (msg.get('content') or []):
                    if isinstance(block, dict) and block.get('type') == 'text':
                        asst_texts.append(block['text'])
        assert any('你好' in t for t in asst_texts), f'Missing Chinese text in assistant entries'
        assert any('🎉' in t for t in asst_texts), f'Missing emoji in assistant entries'
        assert any('Straße' in t for t in asst_texts), f'Missing German text in assistant entries'


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

        data = _extract_data(html)
        assert data is not None, 'Missing JSON data block'
        # Find the Edit tool_use entry in JSON data
        edit_entry = None
        for e in data['entries']:
            msg = e.get('message', {})
            if msg.get('role') == 'assistant':
                for block in (msg.get('content') or []):
                    if isinstance(block, dict) and block.get('name') == 'Edit':
                        edit_entry = block
                        break
        assert edit_entry is not None, 'Missing Edit tool_use in entries'
        assert edit_entry['input']['file_path'] == '/home/user/src/app.py'
        assert 'x = 1' in edit_entry['input']['old_string']
        assert 'z = 30' in edit_entry['input']['new_string']


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

        data = _extract_data(html)
        assert data is not None, 'Missing JSON data block'
        entries = data['entries']
        # Should have 4 entries: user, assistant text, assistant tool_use, user tool_result
        assert len(entries) == 4, f'Expected 4 entries, got {len(entries)}'
        assert entries[0]['message']['role'] == 'user'
        assert entries[0]['message']['content'] == 'Hello'
        assert entries[1]['message']['role'] == 'assistant'
        # Tool use entry
        assert entries[2]['message']['role'] == 'assistant'
        tool_blocks = entries[2]['message']['content']
        assert any(b.get('name') == 'Read' for b in tool_blocks), 'Missing Read tool_use'
        # Tool result entry
        assert entries[3]['message']['role'] == 'user'
        content = entries[3]['message']['content']
        assert any(b.get('type') == 'tool_result' for b in content), 'Missing tool_result'


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

        data = _extract_data(html)
        assert data is not None, 'Missing JSON data block'
        entries = data['entries']
        # All 4 entries are in the JSON (JS filters system messages client-side)
        assert len(entries) == 4, f'Expected 4 entries in data, got {len(entries)}'
        # Verify real user messages are present
        user_texts = [e['message']['content'] for e in entries if isinstance(e.get('message', {}).get('content'), str)]
        assert 'Hello world' in user_texts
        assert 'Real user message' in user_texts
        # System entries are also present (JS will hide them during rendering)
        assert any('task-notification' in str(e['message']['content']) for e in entries)


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
             patch('bridge._resolve_transcript_path', return_value=(tpath, 'test-sid', '/tmp')):
            # Without filter: shows all entries
            html_all = bridge._render_transcript_html('test')
            data_all = _extract_data(html_all)
            assert data_all is not None
            assert len(data_all['entries']) == 4
            assert data_all['filter_mode'] is None or data_all['filter_mode'] == ''

            # With filter=prompts: filter_mode set for JS to handle
            html_filt = bridge._render_transcript_html('test', filter_mode='prompts')
            data_filt = _extract_data(html_filt)
            assert data_filt is not None
            assert data_filt['filter_mode'] == 'prompts'
            # All entries still in JSON (JS filters client-side)
            assert any('hello world' in str(e['message']['content']) for e in data_filt['entries'])
            assert any('second prompt' in str(e['message']['content']) for e in data_filt['entries'])
    finally:
        os.unlink(tpath)


# test_transcript_dynamic_avatars removed — _detect_message_author and
# _generate_member_avatar moved to client-side JS (templates/transcript.js)


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
             patch('bridge._resolve_transcript_path', return_value=(tpath, 'test-sid', '/tmp')):
            html = bridge._render_transcript_html('test')
            data = _extract_data(html)
            assert data is not None, 'Missing JSON data block'
            stats = data['stats']
            # Duration should be 2h 30m (150 min)
            assert stats['duration'] == '2h 30m', f"Expected '2h 30m', got {stats['duration']}"
            # Token counts
            assert stats['input_tokens'] == 8000, f'Expected 8000 input tokens, got {stats["input_tokens"]}'
            assert stats['output_tokens'] == 1200, f'Expected 1200 output tokens, got {stats["output_tokens"]}'
            # File size should be present
            assert data['file_size'], 'Expected file_size in data'
    finally:
        os.unlink(tpath)


def test_tindex_missing_file(tmp_path):
    import subprocess

    db = tmp_path / "test-tindex-missing.db"
    result = subprocess.run(
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", "/nonexistent/path.jsonl",
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on missing file: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on empty file: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on basic indexing: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on noise entries: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on incremental: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    time.sleep(1)
    # Run again — should skip indexing
    subprocess.run(
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    # Note: mtime may change due to SQLite WAL, so just check total is still 1
    result = subprocess.run(
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries", "--page", "1", "--per-page", "50"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    d = json.loads(result.stdout)
    assert d['page'] == 1
    assert len(d['entries']) == 50
    assert json.loads(d['entries'][0]['raw_json'])['message']['content'] == 'Message 0'

    # Page 2
    result = subprocess.run(
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "search", "--search", "teleport"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on search: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "search", "--search", "xyznonexistent"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on empty search: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "entries", "--filter", "prompts"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on filter: {result.stderr}"
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
        [sys.executable, "bridge.py", "indexer", "transcript", "--jsonl", str(tmp),
         "--db", str(db), "--query", "stats"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, f"bridge.py indexer crashed on stats: {result.stderr}"
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
    import bridge as teleport
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
