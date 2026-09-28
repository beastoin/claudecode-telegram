"""Tests migrated from test_data/workers.txt (bash acceptance tests with
embedded python3 -c snippets), converted to pytest.

Tests that required a truncated source snippet (the original bash test body
was cut off mid-block in the source data, with no closing python3 -c quote,
assertions, or success/fail handling) are marked skip with the reason
"truncated source". Tests that require a live bridge HTTP server / tmux
sessions / the claude CLI (integration-only scenarios such as `/workers`
endpoint checks against `http://localhost:$PORT` or direct-mode pipe
delivery) are marked skip with the reason "requires live bridge/tmux".
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

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
    # Save/restore mutable bridge globals so tests don't leak state
    import bridge
    saved = {
        "SESSIONS_DIR": bridge.SESSIONS_DIR,
        "WORKER_REGISTRY_FILE": bridge.WORKER_REGISTRY_FILE,
        "NODE_DIR": bridge.NODE_DIR,
        "WORKER_PIPE_ROOT": getattr(bridge, "WORKER_PIPE_ROOT", None),
        "wm_scan": bridge.worker_manager.scan_tmux_sessions,
        "wm_get_reg": bridge.worker_manager.get_registered_sessions,
    }
    yield
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.NODE_DIR = saved["NODE_DIR"]
    if saved["WORKER_PIPE_ROOT"] is not None:
        bridge.WORKER_PIPE_ROOT = saved["WORKER_PIPE_ROOT"]
    bridge.worker_manager.scan_tmux_sessions = saved["wm_scan"]
    bridge.worker_manager.get_registered_sessions = saved["wm_get_reg"]
    bridge.worker_manager.invalidate_sessions_cache()


def test_send_to_worker_missing():
    from bridge import send_to_worker

    # Non-existent worker returns False
    result = send_to_worker('nonexistent_worker_12345', 'test message')
    assert result == False, f'Expected False, got {result}'

    # Another non-existent tmux worker also returns False
    result2 = send_to_worker('nonexistent_tmux_worker_xyz', 'test message')
    assert result2 == False, f'Expected False, got {result2}'


def test_rewind_team_token():
    import bridge

    bridge.REWIND_TOKENS.clear()
    router = bridge.CommandRouter.__new__(bridge.CommandRouter)
    router.workers = MagicMock()
    replies = []
    router.reply = lambda cid, msg, **kw: replies.append(msg)

    with patch.object(bridge, 'BRIDGE_PUBLIC_URL', 'http://100.125.36.102:8271'):
        router.cmd_rewind('team', 12345)

    assert len(replies) == 1
    reply = replies[0]
    # Reply contains either a beast serve snapshot URL or the bridge team-chat URL
    assert 'rewind-team' in reply or '/team-chat?' in reply, f'Should contain team rewind URL: {reply}'
    assert 'Team chat' in reply

    # Verify token stored with __team__ name
    token = list(bridge.REWIND_TOKENS.keys())[0]
    entry = bridge.REWIND_TOKENS[token]
    assert entry['name'] == '__team__', f'Token name should be __team__: {entry}'
    assert entry['expires_at'] > time.time()

    # Also test --team variant
    bridge.REWIND_TOKENS.clear()
    replies.clear()
    with patch.object(bridge, 'BRIDGE_PUBLIC_URL', 'http://100.125.36.102:8271'):
        router.cmd_rewind('--team', 12345)
    assert 'rewind-team' in replies[0] or '/team-chat?' in replies[0]


def test_team_chat_403_no_token():
    import bridge

    bridge.REWIND_TOKENS.clear()

    handler = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.wfile.write = MagicMock()

    # No token -> 403
    parsed = urlparse('/team-chat')
    bridge.Handler.handle_team_chat_endpoint(handler, parsed)
    handler.send_response.assert_called_with(403)


def test_team_chat_renders_html(tmp_path):
    import bridge

    tmp = tmp_path / "tchat-render.jsonl"
    msgs = [
        {'id': 800, 'timestamp': '2026-04-05T10:00:00', 'timestamp_unix': 1775120400, 'from': 'Thinh', 'text': 'Hello team good morning', 'target_agents': [], 'has_command': False, 'reply_to': None},
        {'id': 801, 'timestamp': '2026-04-05T10:01:00', 'timestamp_unix': 1775120460, 'from': 'beasts', 'text': 'lee:\nGood morning', 'target_agents': ['lee'], 'has_command': False, 'reply_to': None},
    ]
    with open(tmp, 'w') as f:
        for m in msgs:
            f.write(json.dumps(m) + '\n')

    # Set up test data paths
    bridge.TEAM_CHAT_JSONL = str(tmp)
    bridge.TEAM_CHAT_DB = str(tmp_path / "test-tchat-render.db")

    # Add a valid token
    token = 'test-token-123'
    bridge.REWIND_TOKENS[token] = {'name': '__team__', 'expires_at': time.time() + 300}

    # Call the renderer directly
    html = bridge._render_team_chat_html(page=1, per_page=50, token=token)
    assert 'Team Chat' in html, 'Should contain title'
    assert 'msg-800' in html, 'Should contain msg-800 anchor'
    assert 'msg-801' in html, 'Should contain msg-801 anchor'
    assert 'manager' in html, 'Thinh should be resolved to manager'
    assert 'lee' in html, 'beasts+lee: should show as lee'
    assert 'Good morning' in html, 'Should contain message text'

    # Verify handler returns 200
    handler = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    buf = bytearray()
    handler.wfile = MagicMock()
    handler.wfile.write = lambda d: buf.extend(d)
    handler.headers = {'Accept-Encoding': ''}
    handler._send_html = bridge.Handler._send_html.__get__(handler)
    parsed = urlparse(f'/team-chat?token={token}&page=1')
    bridge.Handler.handle_team_chat_endpoint(handler, parsed)
    handler.send_response.assert_called_with(200)
    body = buf.decode('utf-8')
    assert 'msg-800' in body

    bridge.REWIND_TOKENS.clear()


def test_team_chat_search(tmp_path):
    import bridge

    tmp = tmp_path / "tchat-search.jsonl"
    msgs = [
        {'id': 900, 'timestamp': '2026-04-05T10:00:00', 'timestamp_unix': 1775120400, 'from': 'Thinh', 'text': 'Check the gemini costs analysis', 'target_agents': [], 'has_command': False, 'reply_to': None},
        {'id': 901, 'timestamp': '2026-04-05T10:01:00', 'timestamp_unix': 1775120460, 'from': 'Thinh', 'text': 'Flutter build is ready now', 'target_agents': [], 'has_command': False, 'reply_to': None},
    ]
    with open(tmp, 'w') as f:
        for m in msgs:
            f.write(json.dumps(m) + '\n')

    bridge.TEAM_CHAT_JSONL = str(tmp)
    bridge.TEAM_CHAT_DB = str(tmp_path / "test-tchat-search.db")

    html = bridge._render_team_chat_html(page=1, per_page=50, search_query='gemini', token='t')
    assert 'gemini' in html.lower(), 'Should contain search term'
    assert 'msg-900' in html, 'Should show matching msg 900'
    assert 'Found' in html, 'Should show result count'


def test_team_chat_anchor(tmp_path):
    import bridge

    tmp = tmp_path / "tchat-anchor.jsonl"
    msgs = [
        {'id': 3167866, 'timestamp': '2026-04-06T10:04:31', 'timestamp_unix': 1775206271, 'from': 'Thinh', 'text': '@ryo check with mon about gemini costs', 'target_agents': ['ryo'], 'has_command': False, 'reply_to': None},
    ]
    with open(tmp, 'w') as f:
        for m in msgs:
            f.write(json.dumps(m) + '\n')

    bridge.TEAM_CHAT_JSONL = str(tmp)
    bridge.TEAM_CHAT_DB = str(tmp_path / "test-tchat-anchor.db")

    html = bridge._render_team_chat_html(page=1, per_page=50, token='t')
    assert 'id="msg-3167866"' in html, 'Should have anchor for msg 3167866'
    # Verify the JS scroll-to-hash code is present
    assert 'location.hash' in html, 'Should have hash-scroll JS'


def test_team_chat_search_context_link(tmp_path):
    import bridge

    tmp = tmp_path / "tchat-ctx.jsonl"
    msgs = []
    for i in range(60):
        msgs.append({
            'id': 9000 + i,
            'timestamp': f'2026-04-06T10:{i:02d}:00',
            'timestamp_unix': 1775206000 + i * 60,
            'from': 'Thinh' if i % 2 == 0 else 'beasts',
            'text': f'message number {i} about deployment' if i == 55 else f'message {i}',
            'target_agents': [],
            'has_command': False,
            'reply_to': None,
        })
    with open(tmp, 'w') as f:
        for m in msgs:
            f.write(json.dumps(m) + '\n')

    bridge.TEAM_CHAT_JSONL = str(tmp)
    bridge.TEAM_CHAT_DB = str(tmp_path / "test-tchat-ctx.db")

    # Search for 'deployment' -- msg 55 is on page 2 (idx 55, per_page 50)
    html = bridge._render_team_chat_html(page=1, per_page=50, search_query='deployment', token='t')
    # Should contain context link arrow pointing to page 2
    assert 'ctx-link' in html, 'Should have context link class'
    assert 'page=2' in html, 'Should link to page 2 where msg lives'
    assert '#msg-9055' in html, 'Should anchor to the message'


def test_team_chat_reply_context(tmp_path):
    import bridge

    tmp = tmp_path / "tchat-reply.jsonl"
    msgs = [
        {'id': 8001, 'timestamp': '2026-04-06T10:00:00', 'timestamp_unix': 1775206000, 'from': 'Thinh', 'text': 'what is the status of PR 123?', 'target_agents': [], 'has_command': False, 'reply_to': None},
        {'id': 8002, 'timestamp': '2026-04-06T10:01:00', 'timestamp_unix': 1775206060, 'from': 'beasts', 'text': 'lee: PR 123 is merged and deployed', 'target_agents': [], 'has_command': False, 'reply_to': 8001},
    ]
    with open(tmp, 'w') as f:
        for m in msgs:
            f.write(json.dumps(m) + '\n')

    bridge.TEAM_CHAT_JSONL = str(tmp)
    bridge.TEAM_CHAT_DB = str(tmp_path / "test-tchat-reply.db")

    html = bridge._render_team_chat_html(page=1, per_page=50, token='t')
    # Should have reply context block
    assert 'reply-ctx' in html, 'Should have reply context class'
    assert '#msg-8001' in html, 'Reply should link to original message'
    # Should show the replied-to message sender/text
    assert 'manager' in html, 'Reply context should show sender'
    assert 'status of PR 123' in html, 'Reply context should show text snippet'


def test_hire_binary_check():
    import bridge

    # Save originals
    orig_which = shutil.which

    # Make 'fakecli' not found
    def mock_which(name, path=None):
        if name == 'fakecli':
            return None
        return orig_which(name, path=path)

    shutil.which = mock_which

    # Create a backend with a missing binary
    class FakeBackend:
        name = 'fake'
        binary = 'fakecli'
        is_interactive = False

        def start_cmd(self, resume_id='', append_system_prompt=''):
            return 'echo hi'

        def send(self, *a, **kw):
            return True

        def is_online(self, *a):
            return True

    bridge.BACKENDS['fake'] = FakeBackend()

    try:
        # hire should fail with binary-not-found error
        ok, err = bridge.worker_manager.hire('testbincheck', 'fake')
        assert ok is False, f'expected hire to fail, got ok={ok}'
        assert 'fakecli' in err, f'expected binary name in error: {err}'
        assert 'not found' in err, f'expected not-found message: {err}'

        # Verify no tmux session was created
        result = subprocess.run(
            ['tmux', 'has-session', '-t', f'{bridge.TMUX_PREFIX}testbincheck'],
            capture_output=True,
        )
        assert result.returncode != 0, 'tmux session should NOT have been created'
    finally:
        # Cleanup
        bridge.BACKENDS.pop('fake', None)
        shutil.which = orig_which


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_restart_all_sequential():
    pass


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_restart_all_clean():
    pass


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_restart_all_handles_failure():
    pass


def test_restart_stale_session_auto_recovery(tmp_path):
    import bridge

    # Create a temp session dir with a stale session ID
    tmpdir = tmp_path
    sessions_dir = tmpdir / 'sessions2'
    (sessions_dir / 'testbot').mkdir(parents=True, exist_ok=True)

    # Write a stale session ID
    (sessions_dir / 'testbot' / 'claude_session_id').write_text(
        'stale-id-00000000-0000-0000-0000-000000000000'
    )

    # Verify get_claude_session_id reads the stale ID
    orig_sessions_dir = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = type(bridge.SESSIONS_DIR)(str(sessions_dir))

    try:
        sid = bridge.get_claude_session_id('testbot')
        assert sid == 'stale-id-00000000-0000-0000-0000-000000000000', f'Expected stale ID, got {sid}'

        # clear_claude_session_id should remove it
        bridge.clear_claude_session_id('testbot')
        sid_after = bridge.get_claude_session_id('testbot')
        assert not sid_after, f'Expected empty after clear, got {sid_after}'

        # Verify the file is gone
        assert not (sessions_dir / 'testbot' / 'claude_session_id').exists()
    finally:
        bridge.SESSIONS_DIR = orig_sessions_dir


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_restart_all_rejects_duplicate():
    pass


def test_extract_worker_activity():
    from bridge import _extract_activity

    # Spinner shows actual verb + duration (not just 'Thinking')
    lines = ['some output', '· Razzle-dazzling… (6m 21s · thought for 7s)', '───']
    result = _extract_activity(lines)
    assert 'razzle-dazzling' in result.lower(), f'Expected verb in: {result}'
    assert '6m 21s' in result, f'Expected duration in: {result}'

    # Compacting shows as compacting, not thinking
    lines = ['some output', '* Compacting conversation… (5m 26s · thought for 5s)', '───']
    result = _extract_activity(lines)
    assert 'compacting' in result.lower(), f'Expected compacting in: {result}'
    assert '5m 26s' in result, f'Expected duration in: {result}'

    # Thinking with * prefix
    lines = ['some output', '* Stewing… (44s)', 'more stuff']
    result = _extract_activity(lines)
    assert 'stewing' in result.lower(), f'Expected verb in: {result}'
    assert '44s' in result, f'Expected duration in: {result}'

    # Tool actively running (● prefix + ⎿ Running...)
    lines = ['● Bash(python3 -u test.py)', '  ⎿  Running...']
    result = _extract_activity(lines)
    assert 'bash' in result.lower() or 'running' in result.lower(), f'Expected running bash in: {result}'

    # Finished tool call should NOT report as running
    lines = ['● Bash(python3 -u test.py)', '  ⎿  file1.txt', '     file2.txt']
    result = _extract_activity(lines)
    assert 'running' not in result.lower(), f'Finished tool should NOT show as running: {result}'

    # Backgrounded tool should NOT report as running
    lines = ['● Bash(python3 -u test.py 2>&1)', '  ⎿  Running in the background (↓ to manage)', '✻ Compacting conversation… (5m 26s)', '❯']
    result = _extract_activity(lines)
    assert 'running bash' not in result.lower(), f'Backgrounded tool should NOT show as running: {result}'

    # Task list with progress
    lines = ['  ✔ Task A', '  ✔ Task B', '  ◻ Task C', '  ◻ Task D']
    result = _extract_activity(lines)
    assert '2' in result and '4' in result, f'Expected 2/4 progress in: {result}'

    # Error in discussion should NOT trigger error (false positive)
    lines = ['● 300ms gap gives best result: 0.77% with 53.4%', 'savings (only 16 errors). Close to target.']
    result = _extract_activity(lines)
    assert 'error:' not in result.lower(), f'Should not false-positive on discussion error: {result}'

    # Standalone error line SHOULD trigger
    lines = ['FAIL: test_something', 'exit code 1']
    result = _extract_activity(lines)
    assert 'error' in result.lower() or 'fail' in result.lower(), f'Expected error/fail in: {result}'

    # Plan approval prompt
    lines = ['Plan:', '1. Do X', '2. Do Y', 'Do you want to proceed?']
    result = _extract_activity(lines)
    assert 'waiting' in result.lower() or 'input' in result.lower() or 'approval' in result.lower(), f'Expected waiting/input in: {result}'

    # ✻ Churned = PAST tense, NOT active thinking -- with mode bar at bottom
    lines = ['● Skill looks solid.', '✻ Churned for 2m 45s', '───', '❯ save reports', '───', '  ⏵⏵ bypass permissions on · 1 bash']
    result = _extract_activity(lines)
    assert 'thinking' not in result.lower(), f'✻ Churned should NOT be thinking: {result}'
    assert result == 'Ready', f'Prompt with bypass mode bar should be idle: {result}'

    # ✻ with ellipsis = ACTIVE spinner frame, not past tense (kelvin bug 2026-03-08)
    lines = ['● Bash(ssh host "adb screencap")', '  ⎿  Running… (7s · timeout 15s)', '✻ Discombobulating… (49m 13s · thinking)', '───', '❯', '───', '⏵⏵ bypass permissions on (shift+tab to cycle) · es…']
    result = _extract_activity(lines)
    assert 'discombobulating' in result.lower(), f'✻ with … should be active spinner: {result}'
    assert '49m 13s' in result, f'Expected duration in: {result}'

    # Prompt with hint text = idle (auto-suggestion, not queued message)
    lines = ['● Done with task.', '───', '❯ do the next thing', '───']
    result = _extract_activity(lines)
    assert result == 'Ready', f'Prompt hint text should be idle: {result}'

    # Idle at bare prompt
    lines = ['● Done with task.', '───', '❯', '───']
    result = _extract_activity(lines)
    assert result == 'Ready', f'Expected idle at prompt: {result}'

    # bypass permissions mode bar is NEVER a blocking prompt -- bypass = auto-approved
    lines = ['● Some output', '  ⏵⏵ bypass permissions on · 1 bash']
    result = _extract_activity(lines)
    assert 'permission' not in result.lower(), f'Bypass mode bar should NOT be permission: {result}'

    # bypass mode bar with prompt = idle
    lines = ['● Some output', '───', '  ⏵⏵ bypass permissions on · 1 bash', '───', '❯', '───']
    result = _extract_activity(lines)
    assert result == 'Ready', f'Prompt with bypass mode bar should be idle: {result}'
    assert 'permission' not in result.lower(), f'Should NOT show permission: {result}'

    # mode bar WITHOUT pending actions = NOT a permission prompt
    # bypass-permissions-on with shift+tab hint is just persistent mode bar
    lines = ['● Done!', '✻ Cooked for 21m', '───', '❯ merge the PR', '───', '⏵⏵ bypass permissions on (shift+tab to cycle)']
    result = _extract_activity(lines)
    assert 'permission' not in result.lower(), f'Mode bar without actions should NOT be permission: {result}'
    assert result == 'Ready', f'Prompt with hint text should be idle: {result}'

    # ⏵⏵ accept edits on = mode bar, NOT permission prompt (Codex fix #3)
    lines = ['● Finished edits', '⏵⏵ accept edits on (shift+tab to cycle)']
    result = _extract_activity(lines)
    assert 'permission' not in result.lower(), f'accept edits should NOT be permission: {result}'

    # ⏸ plan mode bar detection (Codex fix #4)
    lines = ['● Some output', '⏸ plan mode on (shift+tab to cycle)']
    result = _extract_activity(lines)
    assert 'plan mode' in result.lower(), f'Expected plan mode: {result}'

    # ⏸ plan mode with prompt AFTER = idle (worker moved past plan mode bar)
    lines = ['⏸ plan mode on (shift+tab to cycle)', '❯']
    result = _extract_activity(lines)
    assert result == 'Ready', f'Prompt after plan bar should be idle: {result}'

    # Rate limit ABOVE prompt should still detect rate limit (Codex fix #2)
    lines = ['Rate limit exceeded. Retrying in 30s...', '❯']
    result = _extract_activity(lines)
    assert 'rate limit' in result.lower(), f'Rate limit should beat idle prompt: {result}'

    # Case-insensitive error matching (Codex fix #6)
    lines = ['error: something went wrong']
    result = _extract_activity(lines)
    assert 'error' in result.lower(), f'Lowercase error should be caught: {result}'

    lines = ['ERROR: fatal crash']
    result = _extract_activity(lines)
    assert 'error' in result.lower(), f'Uppercase ERROR should be caught: {result}'

    # Active spinner beats mode bar (spinner is real status)
    lines = ['· Ruminating… (20m 2s · thinking)', '───', '❯', '───', '  ⏵⏵ bypass permissions on · 1 bash']
    result = _extract_activity(lines)
    assert 'ruminating' in result.lower(), f'Active spinner should beat mode bar: {result}'
    assert '20m 2s' in result, f'Expected duration in: {result}'

    # Last ● block concat (multi-line) -- no idle prompt after
    lines = ['● Found 3 bugs in the', '  auth module. All critical.', '  Context left until auto-compact: 42%']
    result = _extract_activity(lines)
    assert 'found 3 bugs' in result.lower() and 'auth module' in result.lower(), f'Expected concat ● block: {result}'

    # Rate limiting detection
    lines = ['● Some output', 'Rate limit exceeded. Retrying in 30s...']
    result = _extract_activity(lines)
    assert 'rate limit' in result.lower(), f'Expected rate limit detection: {result}'

    # Connection error detection
    lines = ['● Output', 'Connection error, retrying in 5s']
    result = _extract_activity(lines)
    assert 'connection' in result.lower(), f'Expected connection error: {result}'

    # Editor mode detection
    lines = ['some code', 'Save and close editor to continue...']
    result = _extract_activity(lines)
    assert 'editor' in result.lower(), f'Expected editor mode: {result}'

    # Hook execution -- SessionStart
    lines = ['Loading hooks', 'Running SessionStart hooks…']
    result = _extract_activity(lines)
    assert 'sessionstart' in result.lower() or 'hooks' in result.lower(), f'Expected hook execution: {result}'

    # Hook execution -- PreCompact
    lines = ['Auto-compacting', 'Running PreCompact hooks…']
    result = _extract_activity(lines)
    assert 'precompact' in result.lower() or 'hooks' in result.lower(), f'Expected hook execution: {result}'

    # Plan mode entry
    lines = ['● Plan:', '1. Do X', '2. Do Y', 'Entering plan mode']
    result = _extract_activity(lines)
    assert 'plan' in result.lower(), f'Expected plan mode: {result}'

    # Team lead approval
    lines = ['Changes ready', 'Waiting for team lead to review and approve...']
    result = _extract_activity(lines)
    assert 'team lead' in result.lower(), f'Expected team lead: {result}'

    # ✢ spinner character (Unicode cross spinner frame)
    lines = ['some output', '✢ Befuddling… (14m 23s · thought for 3s)']
    result = _extract_activity(lines)
    assert 'befuddling' in result.lower(), f'Expected ✢ spinner verb: {result}'
    assert '14m 23s' in result, f'Expected duration for ✢ spinner: {result}'

    # Idle / empty
    lines = ['', '  >', '']
    result = _extract_activity(lines)
    assert result, f'Should return something even for idle, got empty'


def test_clear_hook_failures_on_restart():
    import bridge

    # Set up signal file
    node = os.environ.get('TMUX_PREFIX', 'claude-test-').rstrip('-').removeprefix('claude-') or 'default'
    name = 'cleartest'
    hook_dir = Path(f'/tmp/claudecode-telegram/{node}/{name}/hooks')
    hook_dir.mkdir(parents=True, exist_ok=True)
    failures_file = hook_dir / 'failures'
    now = int(time.time())
    failures_file.write_text(f'{now} Bash\n{now} Edit\n{now} Read\n')

    assert failures_file.exists(), 'setup failed: signal file not created'

    # Clear
    bridge._clear_hook_failures(name)

    assert not failures_file.exists(), 'signal file should be deleted after clear'


def test_worker_host_field():
    import bridge

    # Use temp dir
    tmpdir = tempfile.mkdtemp()
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

    try:
        # Add worker with host
        bridge._registry_add('kenji', 'claude', 123, host='mac')
        data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
        assert data['workers']['kenji']['host'] == 'mac', f"host not stored: {data['workers']['kenji']}"

        # Add worker without host (local)
        bridge._registry_add('lee', 'claude', 123)
        data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
        assert data['workers']['lee'].get('host') is None, 'local worker should have no host'

        # get_worker_host reads back correctly
        assert bridge.get_worker_host('kenji') == 'mac', 'get_worker_host should return mac'
        assert bridge.get_worker_host('lee') is None, 'get_worker_host should return None for local'
        assert bridge.get_worker_host('nobody') is None, 'get_worker_host should return None for missing'
    finally:
        # Restore
        bridge.NODE_DIR = orig_node
        bridge.WORKER_REGISTRY_FILE = orig_reg
        shutil.rmtree(tmpdir)


def test_hire_parses_host():
    import bridge

    # Parse name@host
    name, host = bridge.parse_worker_target('kenji@mac')
    assert name == 'kenji', f'Expected kenji, got {name}'
    assert host == 'mac', f'Expected mac, got {host}'

    # Parse plain name (no host)
    name, host = bridge.parse_worker_target('lee')
    assert name == 'lee', f'Expected lee, got {name}'
    assert host is None, f'Expected None, got {host}'

    # Parse name@host with dashes
    name, host = bridge.parse_worker_target('my-worker@mac-mini')
    assert name == 'my-worker', f'Expected my-worker, got {name}'
    assert host == 'mac-mini', f'Expected mac-mini, got {host}'


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_workers_no_from_backward_compat():
    pass


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_workers_from_includes_machine_id():
    pass


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_workers_callback_worker_send_example():
    pass


def test_restart_clears_session_id_on_cwd_mismatch(tmp_path):
    import bridge

    tmpdir = tmp_path
    orig = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = tmpdir / 'sessions3'
    bridge.SESSIONS_DIR.mkdir()

    try:
        worker_dir = bridge.SESSIONS_DIR / 'cwdmismatch'
        worker_dir.mkdir()

        # Session_id bound to project-a
        (worker_dir / 'claude_session_cwd').write_text('/home/claude/project-a')
        bridge._cache_session_id('cwdmismatch', 'sid-proj-a')

        # CWD changes
        bridge.save_claude_session_cwd('cwdmismatch', '/home/claude/project-b')

        # has_session_id check (what _do_restart does):
        # Even though the file exists, get_claude_session_id returns empty
        has_session_id = False
        session_dir = bridge.SESSIONS_DIR / 'cwdmismatch'
        if session_dir.exists():
            has_session_id = any(session_dir.glob('*_session_id'))

        # File exists on disk...
        assert has_session_id, 'session_id file should exist on disk'

        # ...but get_claude_session_id returns empty (stale)
        sid = bridge.get_claude_session_id('cwdmismatch')
        assert sid == '', f'stale session should return empty, got {sid!r}'

        # This means _do_restart's has_session_id check (file existence) would
        # find the file and try to resume. But _prepare_restart_state calls
        # get_claude_session_id which returns empty -- so resume_id is empty.
        # Empty resume_id means backend.start_cmd('') -> fresh start. Safe.
        resume_id = bridge.get_claude_session_id('cwdmismatch', authoritative=False) or \
            bridge.get_claude_session_id('cwdmismatch', authoritative=True)
        assert resume_id == '', f'stale session should give empty resume_id, got {resume_id!r}'
    finally:
        bridge.SESSIONS_DIR = orig


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_restart_dead_worker():
    pass


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_team_shows_exited():
    pass


def test_team_ready_replaces_idle():
    import bridge

    # Simulate pane with idle prompt
    pane_lines = [
        '--- some output ---',
        '❯ ',
        '-------------------------------------------',
        '  ⏵⏵ bypass permissions on · 5 bashes',
    ]
    result = bridge._extract_activity(pane_lines)
    assert result == 'Ready', f'Expected Ready, got {result!r}'


@pytest.mark.skip(reason="truncated source: bash test body cut off mid python3 -c block, no assertions to convert")
def test_team_header_human_friendly():
    pass


def test_team_attention_needs_reply():
    import bridge

    # WAITING_INPUT status should return 'needs reply' not 'needs input'
    icon, label, rank = bridge._team_attention_summary('Needs reply (5m)', 'some activity')
    assert label == 'needs reply', f'Expected needs reply, got {label!r}'
    assert icon == chr(0x1F7E1), f'Expected yellow, got {icon!r}'

    # Green worker should return 'ok' not 'no blocker'
    icon2, label2, rank2 = bridge._team_attention_summary('Ready', 'Ready')
    assert label2 == 'ok', f'Expected ok, got {label2!r}'


@pytest.mark.skip(reason="requires live bridge HTTP server on $PORT")
def test_workers_endpoint_json_structure():
    pass


@pytest.mark.skip(reason="requires live bridge HTTP server on $PORT plus tmux session cleanup")
def test_workers_endpoint_empty_when_no_workers():
    pass


@pytest.mark.skip(reason="requires live direct-mode bridge process (DIRECT_MODE_BRIDGE_PID)")
def test_workers_endpoint_shows_direct_workers():
    pass


def test_worker_pipe_creation_on_startup():
    from bridge import get_worker_pipe_path, ensure_worker_pipe

    # Test ensure_worker_pipe function creates pipe
    test_name = 'pipetest'
    pipe_path = get_worker_pipe_path(test_name)

    # Ensure parent directory exists
    pipe_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
        # Create the pipe
        ensure_worker_pipe(test_name)

        # Verify pipe exists and is a FIFO
        assert pipe_path.exists(), f'Pipe should exist at {pipe_path}'
        assert os.path.exists(str(pipe_path)), 'Pipe should exist'

        # Check if it's a FIFO (named pipe)
        mode = os.stat(str(pipe_path)).st_mode
        assert stat.S_ISFIFO(mode), 'Should be a FIFO (named pipe)'
    finally:
        # Cleanup
        if pipe_path.exists():
            pipe_path.unlink()
        if pipe_path.parent.exists():
            try:
                pipe_path.parent.rmdir()
            except OSError:
                pass


def test_worker_pipe_cleanup_on_end():
    from bridge import get_worker_pipe_path, ensure_worker_pipe, cleanup_worker_pipe

    test_name = 'pipecleanuptest'
    pipe_path = get_worker_pipe_path(test_name)

    # Create the pipe
    pipe_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_worker_pipe(test_name)

    # Verify pipe exists
    assert pipe_path.exists(), 'Pipe should exist before cleanup'

    # Clean up
    cleanup_worker_pipe(test_name)

    # Verify pipe is removed
    assert not pipe_path.exists(), 'Pipe should be removed after cleanup'

    # Also cleanup parent dir if exists
    if pipe_path.parent.exists():
        try:
            pipe_path.parent.rmdir()
        except OSError:
            pass


@pytest.mark.skip(reason="requires live direct-mode bridge process, claude CLI, and tmux/pipe I/O")
def test_worker_to_worker_pipe_direct():
    pass
