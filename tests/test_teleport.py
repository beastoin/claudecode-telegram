"""Tests migrated from test.sh — teleport category."""
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
        "NODE_DIR": bridge.NODE_DIR,
        "SESSIONS_DIR": bridge.SESSIONS_DIR,
        "WORKER_REGISTRY_FILE": bridge.WORKER_REGISTRY_FILE,
        "WORKER_PIPE_ROOT": getattr(bridge, "WORKER_PIPE_ROOT", None),
        "wm_scan": bridge.worker_manager.scan_tmux_sessions,
        "wm_get_reg": bridge.worker_manager.get_registered_sessions,
    }
    yield
    bridge.NODE_DIR = saved["NODE_DIR"]
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    if saved["WORKER_PIPE_ROOT"] is not None:
        bridge.WORKER_PIPE_ROOT = saved["WORKER_PIPE_ROOT"]
    bridge.worker_manager.scan_tmux_sessions = saved["wm_scan"]
    bridge.worker_manager.get_registered_sessions = saved["wm_get_reg"]
    bridge.worker_manager.invalidate_sessions_cache()


def test_remote_run_local():
    from unittest.mock import patch, MagicMock
    import bridge

    # _remote_run with host=None should call subprocess.run without ssh prefix
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
        bridge._remote_run(['tmux', 'has-session', '-t', 'test'], host=None, capture_output=True)
        args = mock_run.call_args[0][0]
        assert args == ['tmux', 'has-session', '-t', 'test'], f'Local: expected raw cmd, got {args}'


def test_remote_run_ssh():
    from unittest.mock import patch, MagicMock
    import bridge

    # Pre-populate tool cache so _resolve_remote_tool doesn't SSH
    bridge.remote_cache.tools['mac:tmux'] = '/usr/bin/tmux'

    # _remote_run with host='mac' should prefix with ssh and shell-quote args
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')
        bridge._remote_run(['tmux', 'has-session', '-t', 'test'], host='mac', capture_output=True)
        args = mock_run.call_args[0][0]
        # Format: ['ssh', '-o', 'ConnectTimeout=N', host, 'shell-quoted-command']
        assert args[0] == 'ssh', f'Expected ssh, got {args[0]}'
        assert 'mac' in args, f'Expected mac in args, got {args}'
        assert 'has-session' in args[-1], f'Expected has-session in cmd string, got {args[-1]}'
        # Tool path should be resolved from cache
        assert '/usr/bin/tmux' in args[-1], f'Expected resolved tmux path, got {args[-1]}'

    # Clean up cache
    del bridge.remote_cache.tools['mac:tmux']


def test_remote_run_stdin():
    from unittest.mock import patch, MagicMock
    import bridge

    # Verify kwargs (input, capture_output, etc.) are forwarded
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        bridge._remote_run(['tmux', 'load-buffer', '-b', 'buf1', '-'], host='mac', input=b'hello', capture_output=True)
        kwargs = mock_run.call_args[1]
        assert kwargs.get('input') == b'hello', f'input not forwarded: {kwargs}'
        assert kwargs.get('capture_output') == True, f'capture_output not forwarded: {kwargs}'


def test_remote_copy():
    from unittest.mock import patch, MagicMock
    import shutil
    import bridge

    # Local copy (no host) — should use shutil.copy2
    with patch('shutil.copy2') as mock_copy:
        bridge._remote_copy('/tmp/a.txt', '/tmp/b.txt', host=None)
        mock_copy.assert_called_once_with('/tmp/a.txt', '/tmp/b.txt')

    # Push to remote — scp local remote:path
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        bridge._remote_copy('/tmp/a.txt', '/remote/b.txt', host='mac', direction='push')
        args = mock_run.call_args[0][0]
        assert args == ['scp', '-q', '/tmp/a.txt', 'mac:/remote/b.txt'], f'Push: got {args}'

    # Pull from remote — scp remote:path local
    with patch('subprocess.run') as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        bridge._remote_copy('/remote/a.txt', '/tmp/b.txt', host='mac', direction='pull')
        args = mock_run.call_args[0][0]
        assert args == ['scp', '-q', 'mac:/remote/a.txt', '/tmp/b.txt'], f'Pull: got {args}'


def test_registry_teleport_fields():
    import json
    import tempfile
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'

    # Add a worker
    bridge._registry_add('lee', 'claude', 123)

    # Update with teleport info
    bridge._registry_update_teleport('lee', host='mac', home_host=None, home_cwd='/home/claude/proj')
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data['workers']['lee']
    assert w['host'] == 'mac', f'host should be mac, got {w.get("host")}'
    assert w['home_host'] is None, f'home_host should be None'
    assert w['home_cwd'] == '/home/claude/proj', f'home_cwd wrong'

    # Clear teleport info (teleback)
    bridge._registry_clear_teleport('lee')
    data = json.loads(bridge.WORKER_REGISTRY_FILE.read_text())
    w = data['workers']['lee']
    assert w.get('host') is None, f'host should be cleared'
    assert w.get('home_host') is None, f'home_host should be cleared'
    assert w.get('home_cwd') is None, f'home_cwd should be cleared'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    import shutil
    shutil.rmtree(tmpdir)


def test_is_git_repo_with_remote_host():
    import subprocess
    from unittest.mock import patch
    import bridge

    # Mock _remote_run to simulate remote git rev-parse succeeding
    def mock_remote_run(cmd, host=None, **kwargs):
        if host == 'remote-mac' and 'rev-parse' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='true\n', stderr='')
        return subprocess.CompletedProcess(cmd, 1)

    with patch.object(bridge, '_remote_run', side_effect=mock_remote_run):
        assert bridge._is_git_repo('/remote/path', host='remote-mac') == True

    # Also test failure case
    def mock_fail(cmd, host=None, **kwargs):
        return subprocess.CompletedProcess(cmd, 128, stderr='not a git repo')

    with patch.object(bridge, '_remote_run', side_effect=mock_fail):
        assert bridge._is_git_repo('/remote/path', host='remote-mac') == False


def test_git_push_state_remote_host():
    import subprocess
    from unittest.mock import patch
    import bridge

    calls = []

    def mock_remote_run(cmd, host=None, **kwargs):
        calls.append((cmd, host))
        if 'rev-parse' in cmd and 'HEAD' in cmd and '--abbrev-ref' not in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='abc123\n', stderr='')
        if '--abbrev-ref' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='main\n', stderr='')
        if 'diff' in cmd and '--cached' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
        if 'add' in cmd and '-A' in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        if 'stash' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
        if 'reset' in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        if 'push' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')
        return subprocess.CompletedProcess(cmd, 0)

    with patch.object(bridge, '_remote_run', side_effect=mock_remote_run):
        meta = bridge._git_push_state('/remote/src', 'w1', '/home/claude/git-server/test.git',
                                       host='remote-mac')

    assert meta is not None, f'push should succeed: {meta}'
    # Verify all git commands were dispatched to remote host
    git_calls = [(c, h) for c, h in calls if c[0] == 'git']
    for cmd, host in git_calls:
        assert host == 'remote-mac', f'expected remote-mac, got {host} for {cmd}'

    # Verify push targets VPS bare repo via SSH
    push_calls = [c for c, h in calls if 'push' in c]
    assert len(push_calls) > 0, 'should have a push call'
    push_cmd = push_calls[0]
    assert 'claude@100.125.36.102:' in str(push_cmd), f'push should target VPS: {push_cmd}'


def test_git_pull_state_remote_host():
    import subprocess
    from unittest.mock import patch
    import bridge

    calls = []

    def mock_remote_run(cmd, host=None, **kwargs):
        calls.append((cmd, host))
        if 'rev-parse' in cmd and '--git-dir' in cmd:
            return subprocess.CompletedProcess(cmd, 128, stderr='not a git repo')
        if 'clone' in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        if 'config' in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        if 'checkout' in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    meta = {'orig_sha': 'abc123', 'orig_branch': 'main', 'staged_files': [], 'stash_sha': None}

    with patch.object(bridge, '_remote_run', side_effect=mock_remote_run):
        ok = bridge._git_pull_state('/remote/target', 'w1',
                                     'claude@100.125.36.102:/home/claude/git-server/test.git',
                                     meta, host='remote-mac')

    assert ok, 'pull should succeed'
    # Verify all commands dispatched to remote host
    git_calls = [(c, h) for c, h in calls if c[0] == 'git']
    for cmd, host in git_calls:
        assert host == 'remote-mac', f'expected remote-mac, got {host} for {cmd}'


def test_workers_send_example_ssh_for_teleported():
    import tempfile
    from pathlib import Path
    import bridge

    tmp = Path(tempfile.mkdtemp())
    bridge.NODE_DIR = tmp
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.TMUX_PREFIX = 'claude-test-wkr-'
    bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
    bridge.worker_manager.sessions_dir = bridge.SESSIONS_DIR
    bridge.worker_manager.tmux_prefix = bridge.TMUX_PREFIX
    bridge.worker_manager.scan_tmux_sessions = lambda: {
        'ren': {'tmux': 'claude-test-wkr-ren', 'chat_id': 123}
    }
    bridge.worker_manager.invalidate_sessions_cache()

    # Register ren as teleported to mac mini
    bridge._registry_add('ren', 'claude', 123)
    bridge._registry_update_teleport('ren', host='beastoin-agents-f1-mac-mini',
                                      home_host=None, home_cwd='/home/claude/omi')

    workers = bridge.worker_manager.get_workers()
    ren = next((w for w in workers if w['name'] == 'ren'), None)
    assert ren is not None, f'ren not in workers: {[w["name"] for w in workers]}'
    assert ren['protocol'] == 'tmux', f'expected tmux protocol: {ren}'
    assert 'ssh' in ren['send_example'], f'teleported worker should have ssh in send_example: {ren["send_example"]}'
    assert 'beastoin-agents-f1-mac-mini' in ren['send_example'], f'should reference mac mini host: {ren["send_example"]}'

    import shutil
    shutil.rmtree(tmp)


def test_workers_from_caller_remote_to_local_peer():
    import tempfile
    from pathlib import Path
    import bridge

    tmp = Path(tempfile.mkdtemp())
    bridge.NODE_DIR = tmp
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.TMUX_PREFIX = 'claude-test-wkr-'
    bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
    bridge.BRIDGE_SSH_TARGET = 'vps'
    bridge.worker_manager.sessions_dir = bridge.SESSIONS_DIR
    bridge.worker_manager.tmux_prefix = bridge.TMUX_PREFIX
    bridge.worker_manager.scan_tmux_sessions = lambda: {
        'kai': {'tmux': 'claude-test-wkr-kai', 'chat_id': 1},
        'mon': {'tmux': 'claude-test-wkr-mon', 'chat_id': 2},
    }
    bridge.worker_manager.invalidate_sessions_cache()

    bridge._registry_add('kai', 'claude', 1)
    bridge._registry_add('mon', 'claude', 2)
    # kai lives on mac-mini, mon lives on bridge host (None)
    bridge._registry_update_teleport('kai', host='beastoin-agents-f1-mac-mini',
                                      home_host=None, home_cwd='/home/claude/project')

    workers = bridge.worker_manager.get_workers(caller_from='kai')
    mon = next((w for w in workers if w['name'] == 'mon'), None)
    assert mon is not None, f'mon missing: {[w["name"] for w in workers]}'
    assert mon['protocol'] == 'tmux', f'expected tmux protocol: {mon}'
    assert 'ssh vps' in mon['send_example'], \
        f'expected ssh vps wrap for remote caller to bridge-local peer, got: {mon["send_example"]}'
    assert 'paste-buffer -p -r' in mon['send_example'], \
        f'ssh wrap should still use paste-buffer -p -r: {mon["send_example"]}'

    import shutil
    shutil.rmtree(tmp)


def test_workers_from_caller_same_remote_machine_bare():
    import tempfile
    from pathlib import Path
    import bridge

    tmp = Path(tempfile.mkdtemp())
    bridge.NODE_DIR = tmp
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.TMUX_PREFIX = 'claude-test-wkr-'
    bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
    bridge.worker_manager.sessions_dir = bridge.SESSIONS_DIR
    bridge.worker_manager.tmux_prefix = bridge.TMUX_PREFIX
    bridge.worker_manager.scan_tmux_sessions = lambda: {
        'kai': {'tmux': 'claude-test-wkr-kai', 'chat_id': 1},
        'luck': {'tmux': 'claude-test-wkr-luck', 'chat_id': 2},
    }
    bridge.worker_manager.invalidate_sessions_cache()

    bridge._registry_add('kai', 'claude', 1)
    bridge._registry_add('luck', 'claude', 2)
    # Both on mac-mini
    bridge._registry_update_teleport('kai', host='beastoin-agents-f1-mac-mini',
                                      home_host=None, home_cwd='/home/claude/project')
    bridge._registry_update_teleport('luck', host='beastoin-agents-f1-mac-mini',
                                      home_host=None, home_cwd='/home/claude/other')

    workers = bridge.worker_manager.get_workers(caller_from='kai')
    luck = next((w for w in workers if w['name'] == 'luck'), None)
    assert luck is not None, f'luck missing: {[w["name"] for w in workers]}'
    assert 'ssh' not in luck['send_example'], \
        f'same-machine peers should not have ssh wrap, got: {luck["send_example"]}'
    assert 'paste-buffer -p -r' in luck['send_example'], \
        f'bare tmux should use paste-buffer: {luck["send_example"]}'

    import shutil
    shutil.rmtree(tmp)


def test_workers_from_caller_local_to_remote_peer():
    import tempfile
    from pathlib import Path
    import bridge

    tmp = Path(tempfile.mkdtemp())
    bridge.NODE_DIR = tmp
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.TMUX_PREFIX = 'claude-test-wkr-'
    bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
    bridge.worker_manager.sessions_dir = bridge.SESSIONS_DIR
    bridge.worker_manager.tmux_prefix = bridge.TMUX_PREFIX
    bridge.worker_manager.scan_tmux_sessions = lambda: {
        'mon': {'tmux': 'claude-test-wkr-mon', 'chat_id': 1},
        'kai': {'tmux': 'claude-test-wkr-kai', 'chat_id': 2},
    }
    bridge.worker_manager.invalidate_sessions_cache()

    bridge._registry_add('mon', 'claude', 1)
    bridge._registry_add('kai', 'claude', 2)
    # mon on bridge (None), kai on mac-mini
    bridge._registry_update_teleport('kai', host='beastoin-agents-f1-mac-mini',
                                      home_host=None, home_cwd='/home/claude/project')

    workers = bridge.worker_manager.get_workers(caller_from='mon')
    kai = next((w for w in workers if w['name'] == 'kai'), None)
    assert kai is not None
    assert 'ssh beastoin-agents-f1-mac-mini' in kai['send_example'], \
        f'expected ssh to mac-mini for bridge-local caller to remote peer: {kai["send_example"]}'

    import shutil
    shutil.rmtree(tmp)


def test_teleport_preflight_checks_rsync_and_backend():
    import inspect
    import teleport

    src = inspect.getsource(teleport.cmd_teleport)

    # Must check rsync on target via _resolve_remote_tool (inline in cmd_teleport)
    assert '_resolve_remote_tool' in src, \
        'cmd_teleport should check tools via _resolve_remote_tool'

    # Must check backend-specific binary (not just claude)
    assert 'backend_name' in src and 'backend_name != "claude"' in src, \
        'cmd_teleport should check backend-specific binary when not claude'

    # Must check tmux session collision
    assert 'has-session' in src or 'tmux_name' in src, \
        'cmd_teleport should detect tmux session collision on target'


def test_teleport_cross_machine_passes_session_for_resume():
    import bridge
    import teleport
    import inspect

    # v0.44.5+: do_teleport no longer blanket-skips --resume for cross-machine.
    # Instead, sync_session_transcript copies the JSONL to the target first,
    # and start_worker_on_target validates the file exists before --resume.
    #
    # Verify do_teleport passes session_id straight through (no cross-machine skip)

    src = inspect.getsource(teleport.do_teleport)

    # Should NOT have the old blanket skip logic
    assert 'skipping --resume' not in src, \
        'do_teleport should not blanket-skip --resume for cross-machine'

    # Should still assign resume_id from session_id
    assert 'resume_id = session_id' in src, \
        'do_teleport should pass session_id through as resume_id'

    # Should call sync_session_transcript before start_worker_on_target
    sync_pos = src.find('sync_session_transcript')
    start_pos = src.find('start_worker_on_target')
    assert sync_pos > 0, 'do_teleport must call sync_session_transcript'
    assert start_pos > 0, 'do_teleport must call start_worker_on_target'
    assert sync_pos < start_pos, \
        'sync_session_transcript must run before start_worker_on_target'

    # Verify backend.start_cmd with session_id produces --resume
    backend = bridge.get_backend('claude')
    cmd = backend.start_cmd('d61370de-61b2-467b-ac92-d3c5a1e4cfca')
    assert '--resume d61370de' in cmd, f'session_id should produce --resume, got {cmd!r}'

    # Verify backend.start_cmd with empty session_id produces no --resume
    cmd = backend.start_cmd('')
    assert '--resume' not in cmd, f'empty session_id should not produce --resume, got {cmd!r}'


def test_teleport_context_message_has_session_and_search_cmd():
    import bridge

    # Build teleport context for a cross-machine move with a known session
    msg = bridge._build_teleport_context(
        name='nex',
        source_host=None,
        target_host='beastoin-agents-f1-mac-mini',
        source_cwd='/home/claude/mira-nex',
        session_id='d61370de-61b2-467b-ac92-d3c5a1e4cfca',
    )

    # Must tell worker they were teleported
    assert 'teleport' in msg.lower(), f'should mention teleport, got {msg!r}'
    # Must include source machine info
    assert 'VPS' in msg or '100.125' in msg or 'local' in msg, f'should mention source, got {msg!r}'
    # Must include the session_id (or prefix) so worker can look it up
    assert 'd61370de' in msg, f'should include session_id prefix, got {msg!r}'
    # Must include a beast transcript search command
    assert 'beast transcript search' in msg, f'should include search command, got {msg!r}'
    assert '--session' in msg, f'should include --session flag, got {msg!r}'
    # Must include source CWD so worker knows where they were
    assert '/home/claude/mira-nex' in msg or 'mira-nex' in msg, f'should include source cwd, got {msg!r}'


def test_teleport_context_wired_into_do_teleport():
    import inspect
    import teleport

    # Verify the wiring: do_teleport code calls _build_teleport_context
    # when source_host != target_host
    src = inspect.getsource(teleport.do_teleport)

    # Must call _build_teleport_context
    assert '_build_teleport_context' in src, \
        'do_teleport should call _build_teleport_context'

    # Must guard on cross-machine (source_host != target_host)
    assert 'source_host != target_host' in src, \
        'do_teleport should check source_host != target_host'

    # Must pass session_id to the builder
    assert 'session_id' in src, \
        'do_teleport should pass session_id to context builder'

    # Must send the context via workers.send
    assert 'workers.send' in src, \
        'do_teleport should send context via workers.send'


def test_ensure_workspace_trusted_remote_runs_on_target():
    import bridge
    import types

    # Capture remote commands
    remote_cmds = []
    orig_remote_run = bridge._remote_run

    def fake_remote_run(cmd, host=None, **kw):
        remote_cmds.append((cmd, host))
        return types.SimpleNamespace(returncode=0, stdout='trusted', stderr='')

    bridge._remote_run = fake_remote_run

    bridge._ensure_workspace_trusted_remote(
        cwd='/Users/beastoinagents/mira-nex',
        host='beastoin-agents-f1-mac-mini',
    )

    bridge._remote_run = orig_remote_run

    # Should have run a python3 command on the remote host
    assert len(remote_cmds) == 1, f'expected 1 remote command, got {len(remote_cmds)}'
    cmd, host = remote_cmds[0]
    assert host == 'beastoin-agents-f1-mac-mini', f'wrong host: {host}'
    assert cmd[0] == 'python3', f'should run python3 on remote, got {cmd[0]}'
    script = cmd[2]  # -c argument
    assert 'hasTrustDialogAccepted' in script, f'script should set trust flag'
    assert '/Users/beastoinagents/mira-nex' in script, f'script should include target cwd'
    assert 'fcntl' in script, f'remote script should use file locking (fcntl)'
    assert 'LOCK_EX' in script, f'remote script should use exclusive lock'


def test_restart_remote_trusts_cwd_before_start():
    import inspect
    import teleport

    src = inspect.getsource(teleport.restart_remote_worker)

    # Must call _ensure_workspace_trusted_remote before start_worker_on_target
    assert '_ensure_workspace_trusted_remote' in src, \
        'restart_remote_worker should call _ensure_workspace_trusted_remote'

    trust_pos = src.find('_ensure_workspace_trusted_remote(')
    start_pos = src.find('start_worker_on_target(')
    assert trust_pos > 0 and start_pos > 0, 'both calls must exist'
    assert trust_pos < start_pos, \
        f'trust ({trust_pos}) must run before start ({start_pos})'


def test_teleport_trusts_target_cwd_before_start():
    import inspect
    import teleport

    src = inspect.getsource(teleport.do_teleport)

    # Must call _ensure_workspace_trusted_remote
    assert '_ensure_workspace_trusted_remote' in src, \
        'do_teleport should call _ensure_workspace_trusted_remote'

    # Trust must happen before start_worker_on_target
    trust_pos = src.find('_ensure_workspace_trusted_remote(')
    start_pos = src.find('start_worker_on_target(')
    assert trust_pos > 0 and start_pos > 0, 'both function calls must appear in source'
    assert trust_pos < start_pos, \
        f'trust ({trust_pos}) must run before start ({start_pos})'


def test_ensure_workspace_trusted_remote_delegates_local():
    import bridge
    import json
    import tempfile
    import pathlib

    # Use a temp file as the config
    tmp = tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w')
    tmp.write('{}')
    tmp.close()
    orig_path = bridge._CLAUDE_JSON_PATH
    bridge._CLAUDE_JSON_PATH = pathlib.Path(tmp.name)

    # host=None should use local _ensure_workspace_trusted
    bridge._ensure_workspace_trusted_remote('/some/local/path', host=None)

    data = json.loads(pathlib.Path(tmp.name).read_text())
    trusted = data.get('projects', {}).get('/some/local/path', {}).get('hasTrustDialogAccepted')
    assert trusted is True, f'local trust should be set, got {data}'

    bridge._CLAUDE_JSON_PATH = orig_path
    pathlib.Path(tmp.name).unlink()


def test_teleport_context_message_no_session():
    import bridge

    # No previous session
    msg = bridge._build_teleport_context(
        name='nex',
        source_host=None,
        target_host='mac-mini',
        source_cwd='/home/claude/mira-nex',
        session_id='',
    )

    # Should still mention teleport
    assert 'teleport' in msg.lower(), f'should mention teleport, got {msg!r}'
    # Should NOT include beast transcript search (no session to search)
    assert '--session' not in msg, f'should not include --session when no session, got {msg!r}'


def test_restart_teleported_worker():
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    sessions = Path(tmpdir) / 'sessions'
    sessions.mkdir()
    (sessions / 'ren').mkdir()
    (sessions / 'ren' / 'chat_id').write_text('123')
    (sessions / 'ren' / 'claude_session_id').write_text('sess-uuid-123')
    (sessions / 'ren' / 'cwd').write_text('/home/claude/omi')

    # Write registry with host (teleported)
    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()
    (node_dir / 'workers.json').write_text(json.dumps({
        'ren': {'host': 'mac-mini', 'tmux': 'claude-prod-ren'}
    }))

    # Track calls
    remote_calls = []
    start_worker_calls = []
    stop_calls = []
    send_calls = []
    claude_started = [False]

    def mock_remote_run(cmd, **kwargs):
        remote_calls.append((' '.join(str(c) for c in cmd), kwargs.get('host')))
        r = MagicMock(returncode=0, stdout='', stderr='')
        if 'has-session' in str(cmd):
            r.returncode = 0  # tmux exists on remote
        if 'display-message' in str(cmd):
            r.stdout = '12345'
        if 'pgrep' in str(cmd):
            r.returncode = 0 if claude_started[0] else 1
        if 'echo' in str(cmd) and 'HOME' in str(cmd):
            r.stdout = '/Users/beastoinagents'
            r = MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
        return r

    def mock_start_worker(name, target_host, target_cwd, session_id, backend_name, skip_session_sync=False):
        start_worker_calls.append({
            'name': name, 'host': target_host, 'cwd': target_cwd,
            'session_id': session_id, 'backend': backend_name,
            'skip_session_sync': skip_session_sync
        })
        claude_started[0] = True
        return True

    def mock_stop(name, tmux_name, host=None):
        stop_calls.append({'name': name, 'tmux': tmux_name, 'host': host})
        return 'sess-uuid-123'

    class MockWorkers:
        tmux_prefix = 'claude-prod-'
        sessions_dir = sessions

        def _sync_paths(self):
            pass

        def get_registered_sessions(self):
            return {'ren': {'tmux': 'claude-prod-ren'}}

        def _build_welcome(self, name, backend):
            return 'Welcome ren!'

        def send(self, name, text):
            send_calls.append({'name': name, 'text': text[:20]})

    class MockTelegramAPI:
        def send_message(self, *a, **k):
            pass

    orig_sessions = bridge.SESSIONS_DIR
    orig_node_dir = bridge.NODE_DIR
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = node_dir

    router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
    router.workers = MockWorkers()

    with patch('bridge._remote_run', side_effect=mock_remote_run), \
         patch('teleport._remote_run', side_effect=mock_remote_run), \
         patch('claudecode._remote_run', side_effect=mock_remote_run), \
         patch('teleport.start_worker_on_target', side_effect=mock_start_worker), \
         patch('teleport.stop_worker_for_teleport', side_effect=mock_stop), \
         patch('time.sleep'):

        # Call _restart_remote_worker directly on CommandRouter
        ok, err = router._restart_remote_worker(
            'ren', 'claude', bridge.get_backend('claude'),
            'claude-prod-ren', 'mac-mini', 'resume')

    assert ok, f'restart should succeed, got err={err}'

    # Should have stopped the worker on mac-mini
    assert len(stop_calls) == 1, f'Expected 1 stop call, got {stop_calls}'
    assert stop_calls[0]['host'] == 'mac-mini', f'Stop should target mac-mini'

    # Should have called _start_worker_on_target with session_id for resume
    assert len(start_worker_calls) == 1, f'Expected 1 start call, got {start_worker_calls}'
    assert start_worker_calls[0]['host'] == 'mac-mini'
    assert start_worker_calls[0]['session_id'] == 'sess-uuid-123', \
        f'Should pass session_id for resume, got {start_worker_calls[0]["session_id"]}'
    assert start_worker_calls[0]['skip_session_sync'] == True, \
        f'Remote restart should skip session sync (target files are authoritative)'

    # Should have sent welcome
    assert len(send_calls) == 1, f'Expected welcome, got {send_calls}'

    bridge.SESSIONS_DIR = orig_sessions
    bridge.NODE_DIR = orig_node_dir
    import shutil
    shutil.rmtree(tmpdir)


def test_restart_remote_validates_session_exists():
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    sessions = Path(tmpdir) / 'sessions'
    sessions.mkdir()
    (sessions / 'ren').mkdir()
    (sessions / 'ren' / 'claude_session_id').write_text('stale-session-id')
    # Seed RAM CWD cache (no more claude_session_cwd file)
    bridge._set_worker_cwd('ren', '/Users/beastoinagents/omi/omi-ren')

    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()
    (node_dir / 'workers.json').write_text(json.dumps({
        'ren': {'host': 'mac-mini', 'tmux': 'claude-prod-ren'}
    }))

    start_calls = []

    def mock_remote_run(cmd, **kwargs):
        r = MagicMock(returncode=0, stdout='', stderr='')
        cmd_str = ' '.join(str(c) for c in cmd)
        if 'echo' in cmd_str and 'HOME' in cmd_str:
            r.stdout = '/Users/beastoinagents\n'
        # Session file does NOT exist on target
        if 'test' in cmd_str and '-f' in cmd_str:
            r.returncode = 1  # file not found
        if 'has-session' in cmd_str:
            r.returncode = 1  # no tmux session
        return r

    def mock_start(name, host, cwd, session_id, backend, skip_session_sync=False):
        start_calls.append({'session_id': session_id})
        return True

    class MockWorkers:
        tmux_prefix = 'claude-prod-'
        sessions_dir = sessions

        def _build_welcome(self, name, backend):
            return 'Welcome'

        def send(self, name, text):
            pass

    orig_sessions = bridge.SESSIONS_DIR
    orig_node_dir = bridge.NODE_DIR
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = node_dir

    router = bridge.CommandRouter(MagicMock(), MockWorkers())
    router.workers = MockWorkers()

    with patch('bridge._remote_run', side_effect=mock_remote_run), \
         patch('teleport._remote_run', side_effect=mock_remote_run), \
         patch('claudecode._remote_run', side_effect=mock_remote_run), \
         patch('teleport.start_worker_on_target', side_effect=mock_start), \
         patch('time.sleep'):
        ok, err = router._restart_remote_worker(
            'ren', 'claude', bridge.get_backend('claude'),
            'claude-prod-ren', 'mac-mini', 'resume')

    assert ok, f'restart should succeed, got err={err}'
    # Session ID should be empty (fell back to fresh) since file doesn't exist
    assert start_calls[0]['session_id'] == '', \
        f'Should fall back to fresh start, but got session_id={start_calls[0]["session_id"]!r}'
    # Stale session ID file should be cleared
    assert not (sessions / 'ren' / 'claude_session_id').exists(), \
        'Stale session ID file should be deleted'

    bridge.SESSIONS_DIR = orig_sessions
    bridge.NODE_DIR = orig_node_dir
    import shutil
    shutil.rmtree(tmpdir)


def test_restart_remote_remaps_cwd_home():
    import json
    import tempfile
    import os
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    local_home = os.path.expanduser('~')
    tmpdir = tempfile.mkdtemp()
    sessions = Path(tmpdir) / 'sessions'
    sessions.mkdir()
    (sessions / 'x').mkdir()
    # Seed RAM CWD cache — CWD has local home path (the bug: stale local path synced to remote)
    bridge._set_worker_cwd('x', f'{local_home}/claudecode-telegram')
    (sessions / 'x' / 'claude_session_id').write_text('test-session-id')

    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()
    (node_dir / 'workers.json').write_text(json.dumps({
        'x': {'host': 'mac-mini', 'tmux': 'claude-prod-x'}
    }))

    start_calls = []

    def mock_remote_run(cmd, **kwargs):
        r = MagicMock(returncode=0, stdout='', stderr='')
        cmd_str = ' '.join(str(c) for c in cmd)
        if 'echo' in cmd_str and 'HOME' in cmd_str:
            r.stdout = '/Users/beastoinagents\n'
        if 'test' in cmd_str and '-f' in cmd_str:
            r.returncode = 0  # session file exists
        if 'has-session' in cmd_str:
            r.returncode = 1  # no tmux
        return r

    def mock_start(name, host, cwd, session_id, backend, skip_session_sync=False):
        start_calls.append({'cwd': cwd})
        return True

    class MockWorkers:
        tmux_prefix = 'claude-prod-'
        sessions_dir = sessions

        def _build_welcome(self, name, backend):
            return 'Welcome'

        def send(self, name, text):
            pass

    orig_sessions = bridge.SESSIONS_DIR
    orig_node_dir = bridge.NODE_DIR
    orig_home = os.path.expanduser('~')
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = node_dir

    router = bridge.CommandRouter(MagicMock(), MockWorkers())
    router.workers = MockWorkers()

    with patch('bridge._remote_run', side_effect=mock_remote_run), \
         patch('teleport._remote_run', side_effect=mock_remote_run), \
         patch('claudecode._remote_run', side_effect=mock_remote_run), \
         patch('teleport.start_worker_on_target', side_effect=mock_start), \
         patch('time.sleep'):
        ok, err = router._restart_remote_worker(
            'x', 'claude', bridge.get_backend('claude'),
            'claude-prod-x', 'mac-mini', 'resume')

    assert ok, f'restart should succeed, got err={err}'
    # CWD should be remapped from local home to remote home
    actual_cwd = start_calls[0]['cwd']
    assert actual_cwd == '/Users/beastoinagents/claudecode-telegram', \
        f'CWD should be remapped to Mac Mini path, got: {actual_cwd!r}'

    bridge.SESSIONS_DIR = orig_sessions
    bridge.NODE_DIR = orig_node_dir
    import shutil
    shutil.rmtree(tmpdir)


def test_restart_delegates_to_remote_for_teleported():
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    sessions = Path(tmpdir) / 'sessions'
    sessions.mkdir()
    (sessions / 'ren').mkdir()
    (sessions / 'ren' / 'chat_id').write_text('123')
    (sessions / 'ren' / 'claude_session_id').write_text('sess-abc')
    (sessions / 'ren' / 'cwd').write_text('/home/claude/omi')

    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()
    (node_dir / 'workers.json').write_text(json.dumps({
        'workers': {'ren': {'host': 'mac-mini', 'tmux': 'claude-prod-ren'}}
    }))

    remote_restart_calls = []
    replies = []

    def mock_restart_remote(self, name, backend_name, backend, tmux_name, host, mode):
        remote_restart_calls.append({
            'name': name, 'host': host, 'mode': mode
        })
        return True, None

    class MockWorkers:
        tmux_prefix = 'claude-prod-'
        sessions_dir = sessions

        def _sync_paths(self):
            pass

        def get_registered_sessions(self):
            return {'ren': {'tmux': 'claude-prod-ren'}}

    class MockTelegramAPI:
        def send_message(self, *a, **k):
            pass

    orig_sessions = bridge.SESSIONS_DIR
    orig_node_dir = bridge.NODE_DIR
    orig_registry = bridge.WORKER_REGISTRY_FILE
    orig_state = bridge.state.snapshot()
    orig_admin = bridge.admin_chat_id
    bridge.SESSIONS_DIR = sessions
    bridge.NODE_DIR = node_dir
    bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'
    bridge.admin_chat_id = 123
    bridge.state.active = 'ren'

    router = bridge.CommandRouter(MockTelegramAPI(), MockWorkers())
    router.workers = MockWorkers()

    def mock_reply(self, chat_id, text, **kwargs):
        replies.append(text)

    with patch.object(bridge.CommandRouter, '_restart_remote_worker', mock_restart_remote), \
         patch.object(bridge.CommandRouter, 'reply', mock_reply):
        router.cmd_restart(123, 'ren')

    assert len(remote_restart_calls) == 1, f'Should delegate to _restart_remote_worker, got {remote_restart_calls}'
    assert remote_restart_calls[0]['host'] == 'mac-mini'
    assert remote_restart_calls[0]['mode'] == 'resume', f'Default mode should be resume, got {remote_restart_calls[0]["mode"]}'
    assert any('back and ready' in r for r in replies), f'Should confirm restart, got {replies}'

    bridge.SESSIONS_DIR = orig_sessions
    bridge.NODE_DIR = orig_node_dir
    bridge.WORKER_REGISTRY_FILE = orig_registry
    bridge.state.restore(orig_state)
    bridge.admin_chat_id = orig_admin
    import shutil
    shutil.rmtree(tmpdir)


def test_end_worker_teleported_uses_remote_tmux():
    import json
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    orig_sessions = bridge.SESSIONS_DIR
    orig_state = bridge.state.snapshot()

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.state.active = None

    bridge._registry_add('ren', 'claude', 123, host='mac-mini')
    wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
    wm.get_registered_sessions = lambda registered=None: {
        'ren': {'tmux': 'claude-test-ren', 'backend': 'claude'}
    }

    remote_calls = []
    local_calls = []

    def mock_remote(cmd, host=None, **kwargs):
        remote_calls.append((cmd, host))
        return MagicMock(returncode=0, stdout='', stderr='')

    def mock_run(cmd, **kwargs):
        local_calls.append(cmd)
        return MagicMock(returncode=0, stdout='', stderr='')

    with patch('bridge._remote_run', side_effect=mock_remote), \
         patch('subprocess.run', side_effect=mock_run), \
         patch('bridge.cleanup_inbox'), \
         patch('bridge.cleanup_worker_pipe'), \
         patch('bridge.clear_pending'), \
         patch('bridge._set_worker_cwd'), \
         patch('bridge._registry_remove'):
        ok, err = wm.end('ren')

    assert ok, f'remote end should succeed: {err}'
    assert any(cmd[:3] == ['tmux', 'kill-session', '-t'] and host == 'mac-mini'
               for cmd, host in remote_calls), f'remote tmux kill missing: {remote_calls}'
    assert not any(cmd[:3] == ['tmux', 'kill-session', '-t'] for cmd in local_calls), \
        f'remote worker should not use local tmux kill: {local_calls}'

    bridge.WORKER_REGISTRY_FILE.write_text(json.dumps({'version': 1, 'workers': {}}))
    bridge._registry_add('lee', 'claude', 123)
    wm.get_registered_sessions = lambda registered=None: {
        'lee': {'tmux': 'claude-test-lee', 'backend': 'claude'}
    }
    remote_calls.clear()
    local_calls.clear()

    with patch('bridge._remote_run', side_effect=mock_remote), \
         patch('subprocess.run', side_effect=mock_run), \
         patch('bridge.cleanup_inbox'), \
         patch('bridge.cleanup_worker_pipe'), \
         patch('bridge.clear_pending'), \
         patch('bridge._set_worker_cwd'), \
         patch('bridge._registry_remove'):
        ok, err = wm.end('lee')

    assert ok, f'local end should succeed: {err}'
    # _remote_run is called with host=None for local workers (routes to subprocess.run internally)
    assert any(cmd[:3] == ['tmux', 'kill-session', '-t'] and host is None
               for cmd, host in remote_calls), \
        f'local worker should use _remote_run(host=None) for tmux kill: {remote_calls}'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    bridge.SESSIONS_DIR = orig_sessions
    bridge.state.restore(orig_state)
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_restart_teleported_requires_remote_dispatch():
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    orig_sessions = bridge.SESSIONS_DIR

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()

    bridge._registry_add('ren', 'claude', 123, host='mac-mini')
    wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
    wm.get_registered_sessions = lambda registered=None: {
        'ren': {'tmux': 'claude-test-ren', 'backend': 'claude'}
    }

    with patch('bridge.tmux_exists', side_effect=AssertionError('tmux_exists should not run locally for teleported worker')):
        ok, err = wm.restart('ren', mode='resume')

    assert ok is False, f'teleported restart should reject local path: {(ok, err)}'
    assert err == 'use_remote_restart', f'expected sentinel, got {err!r}'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    bridge.SESSIONS_DIR = orig_sessions
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_hook_env_remaps_remote_sessions_dir():
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    orig_sessions = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = Path(str(Path.home() / '.claude' / 'telegram' / 'sessions'))

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append((cmd, host))
        # Match the echo HOME call (literal dollar-HOME in the cmd list)
        if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
            return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
        return MagicMock(returncode=0, stdout='', stderr='')

    with patch('bridge._remote_run', side_effect=mock_remote):
        bridge.export_hook_env('claude-test-ren', backend='claude', host='mac-mini')

    session_exports = [cmd for cmd, host in calls if host == 'mac-mini' and len(cmd) >= 6 and cmd[4] == 'SESSIONS_DIR']
    assert len(session_exports) == 1, f'expected one SESSIONS_DIR export, got {calls}'
    assert session_exports[0][5] == '/Users/beastoinagents/.claude/telegram/sessions', f'wrong path: {session_exports[0]}'

    calls.clear()
    with patch('bridge._remote_run', side_effect=mock_remote):
        bridge.export_hook_env('claude-test-lee', backend='claude')

    local_exports = [cmd for cmd, host in calls if host is None and len(cmd) >= 6 and cmd[4] == 'SESSIONS_DIR']
    assert len(local_exports) == 1, f'expected local SESSIONS_DIR export, got {calls}'
    assert local_exports[0][5] == str(bridge.SESSIONS_DIR), f'wrong local path: {local_exports[0]}'

    bridge.SESSIONS_DIR = orig_sessions


def test_is_online_teleported_ssh_failure_assumes_online():
    from unittest.mock import patch
    import bridge

    wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
    session = {'tmux': 'claude-test-ren', 'backend': 'claude'}

    # SSH failure (exception) should return True (assume online)
    def mock_tmux_exists(name, host=None, **kwargs):
        if host:
            raise OSError('SSH connection refused')
        return True

    with patch('bridge.get_worker_host', return_value='mac-mini'), \
         patch('bridge.tmux_exists', side_effect=mock_tmux_exists):
        result = wm.is_online('ren', session)
        assert result is True, f'SSH failure should assume online, got {result}'


def test_is_online_teleported_checks_claude_process():
    from unittest.mock import patch
    import bridge

    wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
    session = {'tmux': 'claude-test-ren', 'backend': 'claude'}

    with patch('bridge.get_worker_host', return_value='mac-mini'), \
         patch('bridge.tmux_exists', return_value=True), \
         patch('bridge.is_claude_running', return_value=False):
        assert wm.is_online('ren', session) is False, 'remote worker with dead claude should be offline'

    with patch('bridge.get_worker_host', return_value='mac-mini'), \
         patch('bridge.tmux_exists', return_value=True), \
         patch('bridge.is_claude_running', return_value=True):
        assert wm.is_online('ren', session) is True, 'remote worker with live claude should be online'

    local_calls = []

    class FakeBackend:
        is_interactive = True

        def is_online(self, tmux_name):
            local_calls.append(tmux_name)
            return True

    with patch('bridge.get_worker_host', return_value=None), \
         patch('bridge.get_backend', return_value=FakeBackend()):
        assert wm.is_online('lee', {'tmux': 'claude-test-lee', 'backend': 'claude'}) is True

    assert local_calls == ['claude-test-lee'], f'local path should delegate to backend.is_online: {local_calls}'


def test_session_helpers_read_remote_files():
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    orig_sessions = bridge.SESSIONS_DIR

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    # Teleported worker with NO local cwd cache — must SSH fetch
    session_dir = bridge.SESSIONS_DIR / 'ren'
    session_dir.mkdir()
    bridge._registry_add('ren', 'claude', 123, host='mac-mini')

    calls = []

    # CWD is now RAM-only — test save/get round-trip
    bridge.save_claude_session_cwd('ren', '/Users/beastoinagents/omi')
    scwd = bridge.get_claude_session_cwd('ren')
    assert scwd == '/Users/beastoinagents/omi', f'expected RAM cwd, got {scwd!r}'

    # Local worker: RAM cache works the same way
    bridge.save_claude_session_cwd('lee', '/tmp/local')
    assert bridge.get_claude_session_cwd('lee') == '/tmp/local'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    bridge.SESSIONS_DIR = orig_sessions
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_hook_failures_teleported_use_remote_files():
    import json
    import tempfile
    import shutil
    import time
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge._registry_add('ren', 'claude', 123, host='mac-mini')

    now = int(time.time())
    remote_calls = []

    def mock_remote(cmd, host=None, **kwargs):
        remote_calls.append((cmd, host))
        if cmd[:1] == ['cat']:
            lines = '\n'.join([f'{now} Bash', f'{now - 1} Bash', f'{now - 2} Bash']) + '\n'
            return MagicMock(returncode=0, stdout=lines, stderr='')
        return MagicMock(returncode=0, stdout='', stderr='')

    with patch('bridge._remote_run', side_effect=mock_remote):
        reason = bridge._check_hook_failure_signal('ren')
        bridge._clear_hook_failures('ren')

    assert reason is not None and 'hook failure signal' in reason, f'unexpected reason: {reason}'
    assert any(cmd[:1] == ['cat'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote read missing: {remote_calls}'
    assert any(cmd[:2] == ['rm', '-f'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote clear missing: {remote_calls}'

    hook_dir = Path(f'/tmp/claudecode-telegram/{bridge._node_name}/lee/hooks')
    hook_dir.mkdir(parents=True, exist_ok=True)
    failures = hook_dir / 'failures'
    failures.write_text('\n'.join([f'{now} Bash', f'{now - 1} Bash', f'{now - 2} Bash']) + '\n')
    assert bridge._check_hook_failure_signal('lee') is not None
    bridge._clear_hook_failures('lee')
    assert not failures.exists(), 'local hook failure file should be cleared'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    shutil.rmtree(tmpdir, ignore_errors=True)
    shutil.rmtree(hook_dir.parent, ignore_errors=True)


def test_localize_media_teleported_fetches_remote_even_when_local_exists():
    from unittest.mock import patch
    import bridge

    fetch_calls = []

    def mock_fetch(host, file_path):
        fetch_calls.append((host, file_path))
        return '/tmp/fetched.png'

    with patch('bridge.get_worker_host', return_value='mac-mini'), \
         patch('bridge._fetch_remote_file', side_effect=mock_fetch), \
         patch('os.path.exists', return_value=True):
        result = bridge._localize_media('ren', [('/tmp/raw.png', 'caption')])

    assert result == [('/tmp/fetched.png', 'caption')], f'expected fetched remote path, got {result}'
    assert fetch_calls == [('mac-mini', '/tmp/raw.png')], f'should fetch remote file regardless of local collision: {fetch_calls}'

    fetch_calls.clear()
    with patch('bridge.get_worker_host', return_value=None), \
         patch('bridge._fetch_remote_file', side_effect=mock_fetch):
        result = bridge._localize_media('lee', [('/tmp/raw.png', 'caption')])

    assert result == [('/tmp/raw.png', 'caption')], f'local worker should keep local path: {result}'
    assert not fetch_calls, f'local worker should not fetch remote media: {fetch_calls}'


def test_download_telegram_file_syncs_to_remote_inbox():
    import io
    import json
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    orig_token = bridge.BOT_TOKEN
    orig_inbox = bridge.FILE_INBOX_ROOT

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.FILE_INBOX_ROOT = tmp / 'inbox-root'
    bridge.BOT_TOKEN = 'test-token'
    bridge._registry_add('ren', 'claude', 123, host='mac-mini')

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    url_calls = []

    def mock_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, 'full_url') else req
        url_calls.append(url)
        if 'getFile' in url:
            payload = json.dumps({'ok': True, 'result': {'file_path': 'photos/test.png', 'file_size': 4}}).encode()
            return FakeResponse(payload)
        return FakeResponse(b'data')

    remote_calls = []

    def mock_remote(cmd, host=None, **kwargs):
        remote_calls.append((cmd, host))
        return MagicMock(returncode=0, stdout='', stderr='')

    sync_calls = []

    def mock_run(cmd, **kwargs):
        sync_calls.append(cmd)
        return MagicMock(returncode=0, stdout='', stderr='')

    with patch('bridge._urlopen', side_effect=mock_urlopen), \
         patch('bridge._remote_run', side_effect=mock_remote), \
         patch('subprocess.run', side_effect=mock_run):
        remote_path = bridge.download_telegram_file('file-1', 'ren')

    assert remote_path.startswith(str(bridge.FILE_INBOX_ROOT / 'ren' / 'inbox')), remote_path
    assert remote_path.endswith('.png'), remote_path
    assert any(cmd[:2] == ['mkdir', '-p'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote inbox mkdir missing: {remote_calls}'
    assert any(cmd[:2] == ['chmod', '700'] and host == 'mac-mini' for cmd, host in remote_calls), f'remote inbox chmod missing: {remote_calls}'
    assert any(cmd[0] == 'rsync' and 'mac-mini:' in cmd[-1] for cmd in sync_calls), f'remote inbox rsync missing: {sync_calls}'

    bridge.WORKER_REGISTRY_FILE.write_text(json.dumps({'version': 1, 'workers': {}}))
    sync_calls.clear()
    remote_calls.clear()
    with patch('bridge._urlopen', side_effect=mock_urlopen), \
         patch('bridge._remote_run', side_effect=mock_remote), \
         patch('subprocess.run', side_effect=mock_run):
        local_path = bridge.download_telegram_file('file-2', 'lee')

    assert local_path.startswith(str(bridge.FILE_INBOX_ROOT / 'lee' / 'inbox')), local_path
    assert not any(cmd[0] == 'rsync' for cmd in sync_calls), f'local inbox should not rsync: {sync_calls}'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    bridge.BOT_TOKEN = orig_token
    bridge.FILE_INBOX_ROOT = orig_inbox
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_workers_remote_noninteractive_warns():
    import tempfile
    import shutil
    from pathlib import Path
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    orig_sessions = bridge.SESSIONS_DIR
    orig_pipe_root = bridge.WORKER_PIPE_ROOT

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.WORKER_PIPE_ROOT = tmp / 'pipes'
    bridge._registry_add('ren', 'codex', 123, host='mac-mini')

    wm = bridge.WorkerManager(bridge.SESSIONS_DIR, 'claude-test-')
    wm.scan_tmux_sessions = lambda: {'ren': {'tmux': 'claude-test-ren', 'backend': 'codex'}}

    workers = wm.get_workers()
    ren = workers[0]
    assert ren['name'] == 'ren', workers
    assert 'non-interactive' in ren['note'].lower(), f'expected non-interactive note: {ren}'
    assert 'mac-mini' in ren['address'], ren
    assert ren['protocol'] == 'adapter', f'expected adapter protocol: {ren}'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    bridge.SESSIONS_DIR = orig_sessions
    bridge.WORKER_PIPE_ROOT = orig_pipe_root
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_check_adapter_log_teleported_reads_remote():
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    tmp = Path(tmpdir)
    orig_node = bridge.NODE_DIR
    orig_reg = bridge.WORKER_REGISTRY_FILE
    orig_sessions = bridge.SESSIONS_DIR

    bridge.NODE_DIR = tmp
    bridge.WORKER_REGISTRY_FILE = tmp / 'workers.json'
    bridge.SESSIONS_DIR = tmp / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge._registry_add('ren', 'codex', 123, host='mac-mini')

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append((cmd, host))
        if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
            return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
        if cmd[:2] == ['tail', '-n']:
            return MagicMock(returncode=0, stdout='remote adapter log\n', stderr='')
        return MagicMock(returncode=1, stdout='', stderr='missing')

    with patch('bridge._remote_run', side_effect=mock_remote):
        text = bridge._check_adapter_log('ren', tail_lines=5)

    assert text == 'remote adapter log\n', f'expected remote log text, got {text!r}'
    assert any(cmd[:2] == ['tail', '-n'] and host == 'mac-mini' for cmd, host in calls), f'remote tail missing: {calls}'

    local_dir = bridge.SESSIONS_DIR / 'lee'
    local_dir.mkdir()
    (local_dir / 'adapter.log').write_text('local adapter log\n')
    assert bridge._check_adapter_log('lee', tail_lines=5) == 'local adapter log\n'

    bridge.NODE_DIR = orig_node
    bridge.WORKER_REGISTRY_FILE = orig_reg
    bridge.SESSIONS_DIR = orig_sessions
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_remote_dispatch_get_pane_command():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = 'claude'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge.get_pane_command('claude-prod-ren', host='mac-mini')

    assert len(calls) == 1, f'Expected 1 _remote_run call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {calls[0]["host"]}'
    assert 'tmux' in calls[0]['cmd'][0], f'Expected tmux command, got {calls[0]["cmd"]}'
    assert result == 'claude', f'Expected claude, got {result}'

    # host=None should also work (backward compat)
    calls.clear()
    with patch('bridge._remote_run', side_effect=mock_remote):
        bridge.get_pane_command('claude-prod-ren')
    assert calls[0]['host'] is None, f'Default host should be None'


def test_remote_dispatch_is_process_running():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = 'claude'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge.is_process_running('claude-prod-ren', 'claude', host='mac-mini')

    # Should have called _remote_run for tmux display-message (via get_pane_command)
    assert any(c['host'] == 'mac-mini' for c in calls), f'All calls should have host=mac-mini: {calls}'
    assert result == True, f'Process name in pane command should match'


def test_remote_dispatch_is_claude_running():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = 'claude'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge.is_claude_running('claude-prod-ren', host='mac-mini')

    assert any(c['host'] == 'mac-mini' for c in calls), f'Should route through _remote_run with host'
    assert result == True


def test_remote_dispatch_tmux_send_escape():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        return MagicMock(returncode=0)

    with patch('bridge._remote_run', side_effect=mock_remote):
        bridge.tmux_send_escape('claude-prod-ren', host='mac-mini')

    assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'
    assert 'Escape' in calls[0]['cmd'], f'Should send Escape key: {calls[0]["cmd"]}'


def test_remote_dispatch_tmux_pane_pids():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = 'claude-prod-ren 12345\nclaude-prod-lee 67890'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge._tmux_pane_pids(host='mac-mini')

    assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'
    assert result == {'claude-prod-ren': '12345', 'claude-prod-lee': '67890'}, f'Got {result}'


def test_remote_dispatch_ps_stats():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = '12345  2.5 S'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge._ps_stats(['12345'], host='mac-mini')

    assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'
    assert '12345' in result, f'Got {result}'


def test_remote_dispatch_capture_pane_text():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = 'some pane text'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge._capture_pane_text('claude-prod-ren', host='mac-mini')

    assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'
    assert result == 'some pane text', f'Got {result}'


def test_remote_dispatch_export_hook_env():
    from unittest.mock import patch, MagicMock
    import bridge, claudecode

    # Clear remote home cache so _remap_path always calls _remote_run
    with claudecode.remote_cache.lock:
        saved_home = dict(claudecode.remote_cache.home_dirs)
        claudecode.remote_cache.home_dirs.clear()

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        # Handle echo HOME call for SESSIONS_DIR remapping
        if len(cmd) == 3 and cmd[0] == 'bash' and 'HOME' in cmd[2]:
            return MagicMock(returncode=0, stdout='/Users/beastoinagents\n', stderr='')
        # Guard reads current BRIDGE_URL via show-environment (return empty = no prior owner)
        if 'show-environment' in cmd and 'BRIDGE_URL' in cmd:
            return MagicMock(returncode=1, stdout='', stderr='')
        return MagicMock(returncode=0, stdout='', stderr='')

    try:
        with patch('bridge._remote_run', side_effect=mock_remote):
            bridge.export_hook_env('claude-prod-ren', host='mac-mini')
    finally:
        with claudecode.remote_cache.lock:
            claudecode.remote_cache.home_dirs.update(saved_home)

    # 7 calls: 1 show-environment guard + 5 set-environment + 1 echo HOME
    assert len(calls) == 7, f'Expected 7 calls (1 guard + 5 set-env + 1 echo HOME), got {len(calls)}'
    assert all(c['host'] == 'mac-mini' for c in calls), f'All calls should target mac-mini'
    set_env_calls = [c for c in calls if 'set-environment' in ' '.join(c['cmd']) and 'show' not in ' '.join(c['cmd'])]
    assert len(set_env_calls) == 5, f'Expected 5 set-environment calls, got {len(set_env_calls)}'


def test_remote_dispatch_tmux_prompt_empty():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = 'some output\n❯ \n'
        return r

    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge.tmux_prompt_empty('claude-prod-ren', host='mac-mini')

    assert len(calls) >= 1, f'Expected at least 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'
    assert result == True, f'Should detect empty prompt'


def test_remote_dispatch_send_interactive_reply():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        return MagicMock(returncode=0)

    details = {'options': [{'num': 1, 'selected': True}, {'num': 2, 'selected': False}]}
    with patch('bridge._remote_run', side_effect=mock_remote):
        result = bridge._send_interactive_reply('claude-prod-ren', 'cancel', details, host='mac-mini')

    assert result == True, f'cancel should be handled'
    assert len(calls) >= 1, f'Expected at least 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'


def test_remote_dispatch_wait_for_restart_ready():
    from unittest.mock import patch, MagicMock
    import bridge

    tmux_exists_calls = []
    activity_calls = []

    def mock_tmux_exists(name, host=None):
        tmux_exists_calls.append({'name': name, 'host': host})
        return True

    def mock_activity(name, host=None):
        activity_calls.append({'name': name, 'host': host})
        return ('Idle at prompt', None, None)

    with patch('bridge.tmux_exists', side_effect=mock_tmux_exists), \
         patch('bridge._read_tmux_activity', side_effect=mock_activity):
        result = bridge._wait_for_restart_ready('claude-prod-ren', 'claude', host='mac-mini')

    assert result == True, f'Should detect idle prompt'
    assert any(c['host'] == 'mac-mini' for c in tmux_exists_calls), f'tmux_exists should get host: {tmux_exists_calls}'
    assert any(c['host'] == 'mac-mini' for c in activity_calls), f'_read_tmux_activity should get host: {activity_calls}'


def test_remote_dispatch_get_tmux_pane_cwd():
    from unittest.mock import patch, MagicMock
    import bridge

    calls = []

    def mock_remote(cmd, host=None, **kwargs):
        calls.append({'cmd': cmd, 'host': host})
        r = MagicMock()
        r.returncode = 0
        r.stdout = '/Users/beastoinagents/omi'
        return r

    wm = bridge.WorkerManager.__new__(bridge.WorkerManager)
    with patch('bridge._remote_run', side_effect=mock_remote):
        result = wm._get_tmux_pane_cwd('claude-prod-ren', host='mac-mini')

    assert len(calls) == 1, f'Expected 1 call, got {len(calls)}'
    assert calls[0]['host'] == 'mac-mini'
    assert result == '/Users/beastoinagents/omi', f'Got {result}'


def test_fetch_remote_file_logs_stderr():
    from unittest.mock import patch, MagicMock
    import bridge

    # Mock _subprocess_runner.run to simulate rsync failure with stderr
    mock_result = MagicMock()
    mock_result.returncode = 23  # rsync partial transfer error
    mock_result.stderr = b'rsync: connection unexpectedly closed'

    with patch.object(bridge._subprocess_runner, 'run', return_value=mock_result), \
         patch('sys.stderr', new_callable=__import__('io').StringIO) as captured:
        result = bridge._fetch_remote_file('fake-host', '/remote/photo.png')

    output = captured.getvalue()

    # Should have logged the rsync stderr
    assert 'rsync' in output.lower() or 'connection' in output.lower(), \
        f'Should log rsync stderr on failure, got: {repr(output)}'
    assert result is None, 'Should return None on failure'


def test_checkin_cwd_accepts_remote_path_for_teleported_worker():
    import io
    import shutil
    import tempfile
    import subprocess
    from pathlib import Path
    from urllib.parse import urlparse, quote
    from unittest.mock import patch
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
    bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'claude-test-regcheckin-'

    bridge._registry_add('ren', 'claude', 123)
    # Mark ren as teleported to a remote host
    bridge._registry_update_teleport('ren', 'remote-host', None, '/home/claude/omi')
    bridge.worker_manager.invalidate_sessions_cache()

    # Remote path that does NOT exist on VPS
    remote_cwd = '/Users/beastoinagents/omi/omi-ren'

    # Mock _remote_run to succeed for 'test -d' on the remote path
    orig_remote_run = bridge._remote_run

    def mock_remote_run(cmd, host=None, **kwargs):
        # Intercept the remote directory check
        if host == 'remote-host' and cmd[:2] == ['test', '-d']:
            return subprocess.CompletedProcess(cmd, 0)
        return orig_remote_run(cmd, host=host, **kwargs)

    # Mock tmux_exists so we don't need a real session
    # Mock worker_manager methods
    class FakeHandler:
        def __init__(self):
            self.status = None
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.status = code

        def send_header(self, *_args, **_kwargs):
            pass

        def end_headers(self):
            pass

        def _send_text(self, code, text):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(text.encode() if isinstance(text, str) else text)

    handler = FakeHandler()
    parsed = urlparse('/checkin?name=ren&cwd=' + quote(remote_cwd))

    with patch.object(bridge, '_remote_run', side_effect=mock_remote_run), \
         patch.object(bridge, 'tmux_exists', return_value=False):
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

    # Should NOT be 400 — remote path is valid on the remote host
    assert handler.status != 400, f'expected non-400, got {handler.status}: {handler.wfile.getvalue()}'

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_checkin_cwd_restart_uses_remote_for_teleported():
    import io
    import shutil
    import tempfile
    import subprocess
    from pathlib import Path
    from urllib.parse import urlparse, quote
    from unittest.mock import patch, MagicMock
    import bridge

    tmpdir = tempfile.mkdtemp()
    bridge.NODE_DIR = Path(tmpdir)
    bridge.WORKER_REGISTRY_FILE = Path(tmpdir) / 'workers.json'
    bridge.SESSIONS_DIR = Path(tmpdir) / 'sessions'
    bridge.SESSIONS_DIR.mkdir()
    bridge.TMUX_PREFIX = 'claude-test-checkin-'
    (bridge.SESSIONS_DIR / 'ren').mkdir()
    (bridge.SESSIONS_DIR / 'ren' / 'chat_id').write_text('123')
    (bridge.SESSIONS_DIR / 'ren' / 'cwd').write_text('/Users/beastoinagents/old-dir')
    bridge.admin_chat_id = 123

    bridge._registry_add('ren', 'claude', 123)
    bridge._registry_update_teleport('ren', host='mac-mini', home_host=None, home_cwd='/home/claude/omi')
    bridge.worker_manager.invalidate_sessions_cache()

    remote_restart_calls = []
    tg_messages = []

    def mock_remote_run(cmd, host=None, **kwargs):
        # validate_cwd SSH check
        if cmd[:2] == ['test', '-d']:
            return subprocess.CompletedProcess(cmd, 0)
        # tmux_exists
        if 'has-session' in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        # _get_tmux_pane_cwd
        if 'display-message' in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='/Users/beastoinagents/old-dir', stderr='')
        return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

    def mock_restart_remote(self, name, backend_name, backend, tmux_name, host, mode):
        remote_restart_calls.append({'name': name, 'host': host, 'mode': mode})
        return True, None

    def mock_send_tg(chat_id, text, **kwargs):
        tg_messages.append(text)

    new_cwd = '/Users/beastoinagents/omi/omi-ren'

    # Clear cooldown so checkin-triggered restart proceeds
    bridge.watchdog.recent_restarts.pop('ren', None)

    class FakeHandler:
        def __init__(self):
            self.status = None
            self.headers_sent = []
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.status = code

        def send_header(self, *a):
            self.headers_sent.append(a)

        def end_headers(self):
            pass

        def _send_text(self, code, text):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(text.encode() if isinstance(text, str) else text)

    handler = FakeHandler()
    parsed = urlparse('/checkin?name=ren&cwd=' + quote(new_cwd))

    with patch.object(bridge, '_remote_run', side_effect=mock_remote_run), \
         patch.object(bridge, 'send_telegram_message', side_effect=mock_send_tg), \
         patch.object(bridge.CommandRouter, '_restart_remote_worker', mock_restart_remote), \
         patch.object(bridge, '_wait_for_restart_ready', return_value=True), \
         patch.object(bridge, 'is_claude_running', return_value=False):
        bridge.Handler.handle_checkin_endpoint(handler, parsed)

    # Key assertion: should use _restart_remote_worker, not worker_manager.restart
    assert len(remote_restart_calls) == 1, f'Should call _restart_remote_worker, got {remote_restart_calls}'
    assert remote_restart_calls[0]['host'] == 'mac-mini', f'Should target mac-mini: {remote_restart_calls[0]}'

    shutil.rmtree(tmpdir, ignore_errors=True)
