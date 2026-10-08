"""Tests migrated from test.sh — checkin category."""
import io
import json
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, quote

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
    import bridge
    saved = {
        "SESSIONS_DIR": bridge.SESSIONS_DIR,
        "WORKER_REGISTRY_FILE": bridge.WORKER_REGISTRY_FILE,
        "NODE_DIR": bridge.NODE_DIR,
        "TMUX_PREFIX": bridge.TMUX_PREFIX,
        "admin_chat_id": bridge.admin_chat_id,
        "wm_scan": bridge.worker_manager.scan_tmux_sessions,
        "wm_get_reg": bridge.worker_manager.get_registered_sessions,
        "wm_restart": bridge.worker_manager.restart,
        "wm_get_pane_cwd": bridge.worker_manager._get_tmux_pane_cwd,
        "tmux_exists": bridge.tmux_exists,
        "is_claude_running": bridge.is_claude_running,
        "export_hook_env": bridge.export_hook_env,
        "_wait_for_restart_ready": bridge._wait_for_restart_ready,
        "send_telegram_message": bridge.send_telegram_message,
    }
    yield
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.NODE_DIR = saved["NODE_DIR"]
    bridge.TMUX_PREFIX = saved["TMUX_PREFIX"]
    bridge.admin_chat_id = saved["admin_chat_id"]
    bridge.worker_manager.scan_tmux_sessions = saved["wm_scan"]
    bridge.worker_manager.get_registered_sessions = saved["wm_get_reg"]
    bridge.worker_manager.restart = saved["wm_restart"]
    bridge.worker_manager._get_tmux_pane_cwd = saved["wm_get_pane_cwd"]
    bridge.tmux_exists = saved["tmux_exists"]
    bridge.is_claude_running = saved["is_claude_running"]
    bridge.export_hook_env = saved["export_hook_env"]
    bridge._wait_for_restart_ready = saved["_wait_for_restart_ready"]
    bridge.send_telegram_message = saved["send_telegram_message"]
    bridge.worker_manager.invalidate_sessions_cache()


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def _send_text(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode() if isinstance(text, str) else text)


def test_checkin_instructions_warn_against_response_misuse():
    import bridge

    welcome = bridge.worker_manager._build_welcome('finn', bridge.get_backend('claude'))
    assert 'send_example' in welcome, welcome
    assert 'Never use POST /outputs to message another worker' in welcome, welcome
    assert 'http_send_example' not in welcome, 'should not advertise http_send_example — maximize p2p'


def test_checkin_cwd_change_logs_event():
    import bridge

    tmpdir = Path(tempfile.mkdtemp())
    sessions = tmpdir / 'sessions'
    sessions.mkdir()
    worker_dir = sessions / 'cwdworker'
    worker_dir.mkdir()
    (worker_dir / 'claude_session_id').write_text('old-sid-123')

    orig = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = sessions
    # Seed RAM CWD cache (no more claude_session_cwd file)
    bridge._set_worker_cwd('cwdworker', '/home/claude/old-project')
    try:
        # Simulate the checkin CWD change logic from the handler
        old_cwd = bridge.get_claude_session_cwd('cwdworker')
        new_cwd = '/home/claude/new-project'
        bridge.save_claude_session_cwd('cwdworker', new_cwd)
        if old_cwd and old_cwd.rstrip('/') != new_cwd.rstrip('/'):
            bridge._log_session_event('cwdworker', 'old-sid-123', new_cwd, 'cwd_change')
            bridge.clear_claude_session_id('cwdworker')

        history = worker_dir / 'session_history.jsonl'
        assert history.exists(), 'No history after CWD change'
        lines = history.read_text().strip().splitlines()
        assert len(lines) == 1
        e = json.loads(lines[0])
        assert e['event'] == 'cwd_change', f"Wrong event: {e['event']}"
        assert e['cwd'] == '/home/claude/new-project'
        assert e['session_id'] == 'old-sid-123'

        # session_id should be stale (get_claude_session_id returns empty because
        # CWD changed — the file may still exist but self-validates at read time)
        got = bridge.get_claude_session_id('cwdworker')
        assert got == '', f'stale session_id should self-invalidate after CWD change, got {got!r}'
    finally:
        bridge.SESSIONS_DIR = orig
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_change_trusts_new_dir():
    import bridge

    tmpdir = Path(tempfile.mkdtemp())
    fake_claude_json = tmpdir / 'claude.json'
    fake_claude_json.write_text(json.dumps({'projects': {}}))

    orig_sessions = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = tmpdir / 'sessions'
    bridge.SESSIONS_DIR.mkdir()

    worker_dir = bridge.SESSIONS_DIR / 'trustworker'
    worker_dir.mkdir()

    try:
        # Worker starts in project-a
        (worker_dir / 'claude_session_cwd').write_text('/home/claude/project-a')

        # Simulate what checkin does on CWD change: save new CWD + trust it
        bridge.save_claude_session_cwd('trustworker', '/home/claude/project-b')
        bridge._ensure_workspace_trusted('/home/claude/project-b', config_path=fake_claude_json)

        # Verify the new directory is trusted
        data = json.loads(fake_claude_json.read_text())
        proj = data.get('projects', {}).get('/home/claude/project-b', {})
        assert proj.get('hasTrustDialogAccepted') is True, f'new CWD should be auto-trusted, got {proj!r}'
    finally:
        bridge.SESSIONS_DIR = orig_sessions
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_phase2_checkin_passes_host():
    from unittest.mock import patch
    import bridge

    tmpdir = tempfile.mkdtemp()
    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()
    sessions = Path(tmpdir) / 'sessions'
    sessions.mkdir()
    (sessions / 'ren').mkdir()
    (sessions / 'ren' / 'chat_id').write_text('123')

    reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
    (node_dir / 'workers.json').write_text(json.dumps(reg))

    orig_sessions = bridge.SESSIONS_DIR
    orig_node_dir = bridge.NODE_DIR
    orig_registry = bridge.WORKER_REGISTRY_FILE
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = node_dir
    bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'

    ehe_calls = []

    def mock_ehe(tmux_name, backend=None, host=None):
        ehe_calls.append({'tmux': tmux_name, 'host': host})

    te_calls = []

    def mock_te(tmux_name, host=None):
        te_calls.append({'tmux': tmux_name, 'host': host})
        return True

    class MockWorkerManager:
        tmux_prefix = bridge.TMUX_PREFIX
        sessions_dir = sessions

        def _sync_paths(self):
            pass

        def get_registered_sessions(self, *a):
            return {'ren': {'tmux': 'claude-prod-ren'}}

        def _get_tmux_pane_cwd(self, tmux_name, host=None):
            return '/Users/beastoinagents/omi'

    orig_wm = bridge.worker_manager
    bridge.worker_manager = MockWorkerManager()

    try:
        with patch('bridge.export_hook_env', side_effect=mock_ehe), \
             patch('bridge.tmux_exists', side_effect=mock_te):

            # Simulate check-in: just verify export_hook_env is called with host
            name = 'ren'
            registered = bridge.worker_manager.get_registered_sessions()
            tmux_name = registered[name].get('tmux', f'{bridge.TMUX_PREFIX}{name}')
            host = bridge.get_worker_host(name)
            if bridge.tmux_exists(tmux_name, host=host):
                bridge.export_hook_env(tmux_name, 'claude', host=host)

        assert len(ehe_calls) == 1, f'Expected 1 export_hook_env call, got {ehe_calls}'
        assert ehe_calls[0]['host'] == 'mac-mini', f"Expected host=mac-mini, got {ehe_calls[0]['host']}"
        assert te_calls[0]['host'] == 'mac-mini', f"tmux_exists should get host=mac-mini, got {te_calls[0]['host']}"
    finally:
        bridge.SESSIONS_DIR = orig_sessions
        bridge.NODE_DIR = orig_node_dir
        bridge.WORKER_REGISTRY_FILE = orig_registry
        bridge.worker_manager = orig_wm
        shutil.rmtree(tmpdir)


def test_checkin_cwd_stores_in_memory():
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
    bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'regcheckin-'
    bridge.watchdog.worker_cwds.clear()

    try:
        bridge._registry_add('alice', 'claude', 123)
        project_dir = Path(tmpdir) / 'project'
        project_dir.mkdir()

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(project_dir)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        assert handler.status == 200, f'expected 200, got {handler.status}'
        assert bridge._get_worker_cwd('alice') == str(project_dir), bridge._get_worker_cwd('alice')
        data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
        assert 'cwd' not in data['workers']['alice'], data['workers']['alice']
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_invalid_path():
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
    bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'regcheckin-'

    try:
        bridge._registry_add('alice', 'claude', 123)
        missing = Path(tmpdir) / 'does-not-exist'

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(missing)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        assert handler.status == 400, f'expected 400, got {handler.status}'
        data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
        assert 'cwd' not in data['workers']['alice'], data['workers']['alice']
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_restart_notifies_manager():
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir)
    bridge.NODE_DIR = tmp_path
    bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
    bridge.SESSIONS_DIR = tmp_path / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'checkinnotify-'
    bridge.admin_chat_id = None
    bridge.watchdog.worker_cwds.clear()

    try:
        bridge._registry_add('alice', 'claude', 123)
        session_dir = bridge.ensure_session_dir('alice')
        chat_file = session_dir / 'chat_id'
        chat_file.write_text('777')
        chat_file.chmod(0o600)

        old_dir = tmp_path / 'old-project'
        new_dir = tmp_path / 'new-project'
        old_dir.mkdir()
        new_dir.mkdir()

        bridge.worker_manager.get_registered_sessions = lambda registered=None: {
            'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice', 'backend': 'claude'}
        }
        bridge.tmux_exists = lambda _name, host=None: True
        bridge.is_claude_running = lambda _name, host=None: False  # Allow checkin restart
        bridge.export_hook_env = lambda *_args, **_kwargs: None
        bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
        bridge.worker_manager.restart = lambda name, mode='relaunch': (True, None)
        bridge._wait_for_restart_ready = lambda *_args, **_kwargs: True
        bridge.watchdog.recent_restarts.pop('alice', None)  # Clear cooldown

        sent = []

        def fake_send(chat_id, text):
            sent.append((chat_id, text))
            return {'ok': True}
        bridge.send_telegram_message = fake_send

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(new_dir)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        body = handler.wfile.getvalue().decode()
        assert handler.status == 200, f'expected 200, got {handler.status}: {body}'
        assert 'Restarting in' in body, body
        assert len(sent) == 2, f'expected 2 notifications, got {len(sent)}: {sent}'
        assert sent[0][0] == 777 and sent[1][0] == 777, sent
        first = sent[0][1].lower()
        second = sent[1][1].lower()
        assert 'alice' in first and 'restart' in first, first
        assert 'lost' in first or 'hold' in first, first
        assert 'alice' in second and 'ready' in second, second
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_restart_prefers_admin_chat_id():
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir)
    bridge.NODE_DIR = tmp_path
    bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
    bridge.SESSIONS_DIR = tmp_path / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'checkinnotify-'
    bridge.admin_chat_id = 999
    bridge.watchdog.worker_cwds.clear()

    try:
        bridge._registry_add('alice', 'claude', 123)
        session_dir = bridge.ensure_session_dir('alice')
        chat_file = session_dir / 'chat_id'
        chat_file.write_text('777')
        chat_file.chmod(0o600)

        old_dir = tmp_path / 'old-project'
        new_dir = tmp_path / 'new-project'
        old_dir.mkdir()
        new_dir.mkdir()

        bridge.worker_manager.get_registered_sessions = lambda registered=None: {
            'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice', 'backend': 'claude'}
        }
        bridge.tmux_exists = lambda _name, host=None: True
        bridge.is_claude_running = lambda _name, host=None: False  # Allow checkin restart
        bridge.export_hook_env = lambda *_args, **_kwargs: None
        bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
        bridge.worker_manager.restart = lambda name, mode='relaunch': (True, None)
        bridge._wait_for_restart_ready = lambda *_args, **_kwargs: True
        bridge.watchdog.recent_restarts.pop('alice', None)  # Clear cooldown

        sent = []

        def fake_send(chat_id, text):
            sent.append((chat_id, text))
            return {'ok': True}
        bridge.send_telegram_message = fake_send

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(new_dir)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        body = handler.wfile.getvalue().decode()
        assert handler.status == 200, f'expected 200, got {handler.status}: {body}'
        assert len(sent) == 2, f'expected 2 notifications, got {len(sent)}: {sent}'
        assert sent[0][0] == 999 and sent[1][0] == 999, sent
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_restart_failure_notifies_manager():
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir)
    bridge.NODE_DIR = tmp_path
    bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
    bridge.SESSIONS_DIR = tmp_path / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'checkinnotify-'
    bridge.admin_chat_id = None
    bridge.watchdog.worker_cwds.clear()

    try:
        bridge._registry_add('alice', 'claude', 123)
        session_dir = bridge.ensure_session_dir('alice')
        chat_file = session_dir / 'chat_id'
        chat_file.write_text('777')
        chat_file.chmod(0o600)

        old_dir = tmp_path / 'old-project'
        new_dir = tmp_path / 'new-project'
        old_dir.mkdir()
        new_dir.mkdir()

        bridge.worker_manager.get_registered_sessions = lambda registered=None: {
            'alice': {'tmux': f'{bridge.TMUX_PREFIX}alice', 'backend': 'claude'}
        }
        bridge.tmux_exists = lambda _name, host=None: True
        bridge.is_claude_running = lambda _name, host=None: False  # Allow checkin restart
        bridge.export_hook_env = lambda *_args, **_kwargs: None
        bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
        bridge.worker_manager.restart = lambda name, mode='relaunch': (False, 'boom')
        bridge.watchdog.recent_restarts.pop('alice', None)  # Clear cooldown

        sent = []

        def fake_send(chat_id, text):
            sent.append((chat_id, text))
            return {'ok': True}
        bridge.send_telegram_message = fake_send

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=alice&cwd=' + quote(str(new_dir)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        body = handler.wfile.getvalue().decode()
        assert handler.status == 500, f'expected 500, got {handler.status}: {body}'
        assert 'Failed to restart' in body, body
        assert len(sent) == 2, f'expected 2 notifications, got {len(sent)}: {sent}'
        assert sent[0][0] == 777 and sent[1][0] == 777, sent
        assert 'restart' in sent[1][1].lower() and ('fail' in sent[1][1].lower() or 'could not' in sent[1][1].lower()), sent[1][1]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_restart_blocked_by_cooldown():
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir)
    bridge.NODE_DIR = tmp_path
    bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
    bridge.SESSIONS_DIR = tmp_path / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'cooldown-'
    bridge.admin_chat_id = None
    bridge.watchdog.worker_cwds.clear()

    try:
        bridge._registry_add('bob', 'claude', 123)
        bridge.ensure_session_dir('bob')

        old_dir = tmp_path / 'old-project'
        new_dir = tmp_path / 'new-project'
        old_dir.mkdir()
        new_dir.mkdir()

        bridge.worker_manager.get_registered_sessions = lambda registered=None: {
            'bob': {'tmux': f'{bridge.TMUX_PREFIX}bob', 'backend': 'claude'}
        }
        bridge.tmux_exists = lambda _name, host=None: True
        bridge.is_claude_running = lambda _name, host=None: False
        bridge.export_hook_env = lambda *_args, **_kwargs: None
        bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)

        restart_calls = []
        bridge.worker_manager.restart = lambda name, mode='relaunch': (restart_calls.append(1), (True, None))[1]
        bridge._wait_for_restart_ready = lambda *_args, **_kwargs: True
        bridge.send_telegram_message = lambda *a, **kw: {'ok': True}

        # Set recent restart to NOW — should trigger cooldown
        bridge.watchdog.recent_restarts['bob'] = time.time()

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=bob&cwd=' + quote(str(new_dir)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        body = handler.wfile.getvalue().decode()
        assert handler.status == 200, f'expected 200, got {handler.status}: {body}'
        assert 'blocked' in body.lower(), f'Expected cooldown block message: {body}'
        assert len(restart_calls) == 0, f'Should NOT have restarted: {restart_calls}'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_restart_blocked_by_running_claude():
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir)
    bridge.NODE_DIR = tmp_path
    bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
    bridge.SESSIONS_DIR = tmp_path / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'guard-'
    bridge.admin_chat_id = None
    bridge.watchdog.worker_cwds.clear()

    try:
        bridge._registry_add('bob', 'claude', 123)

        old_dir = tmp_path / 'old-project'
        new_dir = tmp_path / 'new-project'
        old_dir.mkdir()
        new_dir.mkdir()

        bridge.worker_manager.get_registered_sessions = lambda registered=None: {
            'bob': {'tmux': f'{bridge.TMUX_PREFIX}bob', 'backend': 'claude'}
        }
        bridge.tmux_exists = lambda _name, host=None: True
        bridge.is_claude_running = lambda _name, host=None: True  # Claude IS running
        bridge.export_hook_env = lambda *_args, **_kwargs: None
        bridge.worker_manager._get_tmux_pane_cwd = lambda _tmux, host=None: str(old_dir)
        bridge.watchdog.recent_restarts.pop('bob', None)  # No cooldown

        restart_calls = []
        bridge.worker_manager.restart = lambda name, mode='relaunch': (restart_calls.append(1), (True, None))[1]

        handler = FakeHandler()
        parsed = urlparse('/checkin?name=bob&cwd=' + quote(str(new_dir)))
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

        body = handler.wfile.getvalue().decode()
        assert handler.status == 200, f'expected 200, got {handler.status}: {body}'
        assert 'skipped' in body.lower() or 'running' in body.lower(), f'Expected running guard message: {body}'
        assert len(restart_calls) == 0, f'Should NOT have restarted: {restart_calls}'
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_note_machine_substitution():
    import bridge

    tmpdir = tempfile.mkdtemp()
    team_dir = Path(tmpdir) / 'team'
    team_dir.mkdir()
    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()

    # Write checkin note with {machine} placeholder
    (team_dir / 'checkin-note.txt').write_text('You are {name}. You are on: {machine}.')

    # Write registry with a remote worker
    (node_dir / 'workers.json').write_text(json.dumps(
        {'workers': {'remotetest': {'backend': 'claude', 'host': 'beastoin-agents-f1-mac-mini'}}}
    ))

    old_team_dir = bridge.TEAM_DIR
    old_node_dir = bridge.NODE_DIR
    old_checkin = bridge._CHECKIN_NOTE_PATH
    old_registry = bridge.WORKER_REGISTRY_FILE
    try:
        bridge.TEAM_DIR = str(team_dir)
        bridge._CHECKIN_NOTE_PATH = str(team_dir / 'checkin-note.txt')
        bridge.NODE_DIR = node_dir
        bridge.WORKER_REGISTRY_FILE = bridge.NODE_DIR / 'workers.json'

        note = bridge.read_checkin_note()
        assert '{machine}' in note, f'note missing placeholder: {note}'

        # Local worker: no host → VPS
        rendered_local = note.replace('{name}', 'localtest')
        host = bridge.get_worker_host('localtest')
        if host:
            rendered_local = rendered_local.replace('{machine}', f'Mac Mini ({host})')
        else:
            rendered_local = rendered_local.replace('{machine}', 'VPS (100.125.36.102)')
        assert 'VPS' in rendered_local, f'local should get VPS: {rendered_local}'
        assert '{machine}' not in rendered_local

        # Remote worker: has host → Mac Mini
        rendered_remote = note.replace('{name}', 'remotetest')
        host = bridge.get_worker_host('remotetest')
        if host:
            rendered_remote = rendered_remote.replace('{machine}', f'Mac Mini ({host})')
        else:
            rendered_remote = rendered_remote.replace('{machine}', 'VPS (100.125.36.102)')
        assert 'Mac Mini' in rendered_remote, f'remote should get Mac Mini: {rendered_remote}'
        assert '{machine}' not in rendered_remote
    finally:
        bridge.TEAM_DIR = old_team_dir
        bridge._CHECKIN_NOTE_PATH = old_checkin
        bridge.NODE_DIR = old_node_dir
        bridge.WORKER_REGISTRY_FILE = old_registry
        shutil.rmtree(tmpdir, ignore_errors=True)
