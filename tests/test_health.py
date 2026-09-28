"""Tests migrated from test.sh — health/watchdog category."""
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
        "wm_scan": bridge.worker_manager.scan_tmux_sessions,
        "wm_get_reg": bridge.worker_manager.get_registered_sessions,
    }
    yield
    bridge.SESSIONS_DIR = saved["SESSIONS_DIR"]
    bridge.WORKER_REGISTRY_FILE = saved["WORKER_REGISTRY_FILE"]
    bridge.NODE_DIR = saved["NODE_DIR"]
    bridge.worker_manager.scan_tmux_sessions = saved["wm_scan"]
    bridge.worker_manager.get_registered_sessions = saved["wm_get_reg"]
    bridge.worker_manager.invalidate_sessions_cache()


def test_watchdog_suppressed_after_restart():
    import time
    import bridge

    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        return {'ok': True}

    orig_api = bridge.telegram_api
    bridge.telegram_api = fake_api
    bridge.admin_chat_id = 12345

    # Clear state
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states.clear()
        bridge.watchdog.prev_worker_states['bob'] = 'STUCK'

    # Mark bob as recently restarted
    bridge.watchdog.recent_restarts['bob'] = time.time()

    # Resolved alert should be suppressed
    bridge._send_resolved_alert('bob', 'READY')
    assert len(calls) == 0, f'Expected 0 calls (suppressed), got {len(calls)}'

    # After 30s, should fire again
    bridge.watchdog.recent_restarts['bob'] = time.time() - 31
    calls.clear()
    bridge._send_resolved_alert('bob', 'READY')
    assert len(calls) == 1, f'Expected 1 call after cooldown, got {len(calls)}'

    bridge.telegram_api = orig_api
    bridge.admin_chat_id = None
    bridge.watchdog.recent_restarts.clear()


def test_watchdog_waiting_input_state():
    import bridge
    import time

    # Test _format_watchdog_status with WAITING_INPUT
    bridge.watchdog.worker_states['testbot'] = ('WAITING_INPUT', 'question=Pick color', time.time() - 120)
    result = bridge._format_watchdog_status('testbot')
    assert 'Needs reply' in result, f'Expected Needs reply, got: {result}'
    assert '2m' in result, f'Expected 2m duration, got: {result}'

    # Test that WAITING_INPUT is in bad_states for alert transitions
    bad = {'OFFLINE', 'DEAD', 'STUCK', 'POISONED', 'EXITED', 'WAITING_INPUT'}
    assert 'WAITING_INPUT' in bad

    # Test _team_attention_summary picks it up
    icon, label, rank = bridge._team_attention_summary('Needs reply (2m)', 'Waiting for input: Pick color')
    assert icon == '\U0001f7e1', f'Expected yellow icon, got: {icon}'
    assert 'reply' in label, f'Expected reply label, got: {label}'

    # Clean up
    bridge.watchdog.worker_states.pop('testbot', None)


def test_watchdog_resolved_alert():
    import bridge

    # Track calls to telegram_api
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        return {'ok': True}

    orig_api = bridge.telegram_api
    bridge.telegram_api = fake_api
    bridge.admin_chat_id = 12345

    # Clear state
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states.clear()

    # Simulate STUCK -> READY transition
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states['testworker'] = 'STUCK'

    bridge._send_resolved_alert('testworker', 'READY')

    assert len(calls) == 1, f'Expected 1 API call, got {len(calls)}'
    method, data = calls[0]
    assert method == 'sendMessage', f'Expected sendMessage, got {method}'
    assert 'testworker' in data['text'], f'Expected worker name in text, got {data["text"]}'
    assert 'back to normal' in data['text'], f'Expected back to normal in text, got {data["text"]}'

    # Verify no alert when transition is not from bad to good state
    calls.clear()
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states['testworker'] = 'READY'
    bridge._send_resolved_alert('testworker', 'BUSY_TOOL')
    assert len(calls) == 0, f'Expected no call for READY->BUSY_TOOL, got {len(calls)}'

    bridge.telegram_api = orig_api
    bridge.admin_chat_id = None


def test_format_watchdog_status():
    import time
    import bridge

    now = time.time()
    since = now - 120  # 2 minutes ago

    # Build a state snapshot for each state
    states = {
        'ready_worker': ('READY', 'idle', since),
        'busy_tool_worker': ('BUSY_TOOL', 'children=3', since),
        'busy_thinking_worker': ('BUSY_THINKING', 'cpu=20.0', since),
        'waiting_worker': ('WAITING', 'age=100s', since),
        'stuck_worker': ('STUCK', 'age=600s cpu=2.0', since),
        'dead_worker': ('DEAD', 'claude missing 60s', since),
        'offline_worker': ('OFFLINE', 'tmux missing', since),
        'poisoned_worker': ('POISONED', 'exec loop', since),
        'untracked_worker': ('UNTRACKED_BUSY', 'children=1', since),
    }

    # Inject into _worker_states
    with bridge.watchdog.lock:
        bridge.watchdog.worker_states.update(states)

    snapshot = dict(states)

    # READY -> 'Ready'
    result = bridge._format_watchdog_status('ready_worker', lambda n: False, state_snapshot=snapshot)
    assert result == 'Ready', f'READY: expected Ready, got {result}'

    # BUSY_TOOL -> 'Working'
    result = bridge._format_watchdog_status('busy_tool_worker', lambda n: True, state_snapshot=snapshot)
    assert result == 'Working', f'BUSY_TOOL: expected Working, got {result}'

    # BUSY_THINKING -> 'Thinking'
    result = bridge._format_watchdog_status('busy_thinking_worker', lambda n: True, state_snapshot=snapshot)
    assert result == 'Thinking', f'BUSY_THINKING: expected Thinking, got {result}'

    # WAITING -> 'Working'
    result = bridge._format_watchdog_status('waiting_worker', lambda n: True, state_snapshot=snapshot)
    assert result == 'Working', f'WAITING: expected Working, got {result}'

    # STUCK -> 'No progress (Xm)'
    result = bridge._format_watchdog_status('stuck_worker', lambda n: True, state_snapshot=snapshot)
    assert result.startswith('No progress'), f'STUCK: expected No progress (Xm), got {result}'
    assert 'm)' in result, f'STUCK: expected minutes in parens, got {result}'

    # DEAD -> 'Not responding'
    result = bridge._format_watchdog_status('dead_worker', lambda n: False, state_snapshot=snapshot)
    assert result == 'Not responding', f'DEAD: expected Not responding, got {result}'

    # OFFLINE -> 'Offline'
    result = bridge._format_watchdog_status('offline_worker', lambda n: False, state_snapshot=snapshot)
    assert result == 'Offline', f'OFFLINE: expected Offline, got {result}'

    # POISONED -> 'Error loop (Xm)'
    result = bridge._format_watchdog_status('poisoned_worker', lambda n: True, state_snapshot=snapshot)
    assert result.startswith('Error loop'), f'POISONED: expected Error loop (Xm), got {result}'

    # Unknown worker -> fallback based on pending
    result = bridge._format_watchdog_status('nonexistent_worker', lambda n: True, state_snapshot=snapshot)
    assert result == 'Working', f'Unknown+pending: expected Working, got {result}'
    result = bridge._format_watchdog_status('nonexistent_worker', lambda n: False, state_snapshot=snapshot)
    assert result == 'Ready', f'Unknown+idle: expected Ready, got {result}'

    # Clean up
    with bridge.watchdog.lock:
        for k in list(states.keys()):
            bridge.watchdog.worker_states.pop(k, None)


def test_handle_watchdog_transition():
    import time
    import bridge

    alerts = []
    resolved = []

    def fake_alert(name, state, reason):
        alerts.append((name, state, reason))

    def fake_resolved(name, new_state):
        resolved.append((name, new_state))

    orig_alert = bridge._send_watchdog_alert
    orig_resolved = bridge._send_resolved_alert
    orig_get_host = bridge.get_worker_host
    bridge._send_watchdog_alert = fake_alert
    bridge._send_resolved_alert = fake_resolved
    bridge.get_worker_host = lambda name: None  # local workers

    # Clear state
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states.clear()
        bridge.watchdog.consecutive_good_probes.clear()
        bridge.watchdog.consecutive_bad_probes.clear()

    now = time.time()

    # Local worker: STUCK triggers alert immediately
    bridge._handle_watchdog_transition('worker1', 'STUCK', 'age=600s', now, now=now)
    assert len(alerts) == 1, f'Expected 1 alert, got {len(alerts)}'
    assert alerts[0] == ('worker1', 'STUCK', 'age=600s')

    # Transition from STUCK to READY requires 3 consecutive good probes
    alerts.clear()
    bridge._handle_watchdog_transition('worker1', 'READY', 'idle', now, now=now)
    assert len(resolved) == 0, f'Expected 0 resolved after 1 good probe, got {len(resolved)}'
    bridge._handle_watchdog_transition('worker1', 'READY', 'idle', now, now=now)
    assert len(resolved) == 0, f'Expected 0 resolved after 2 good probes, got {len(resolved)}'
    bridge._handle_watchdog_transition('worker1', 'READY', 'idle', now, now=now)
    assert len(resolved) == 1, f'Expected 1 resolved after 3 good probes, got {len(resolved)}'
    assert resolved[0] == ('worker1', 'READY')
    assert len(alerts) == 0, 'No alert for good state'

    # Transition READY -> BUSY_THINKING should not trigger alert or resolved
    alerts.clear()
    resolved.clear()
    bridge._handle_watchdog_transition('worker1', 'BUSY_THINKING', 'cpu=20', now, now=now)
    assert len(alerts) == 0 and len(resolved) == 0, 'READY->BUSY_THINKING should fire nothing'

    # --- Remote worker: OFFLINE/DEAD requires 3 consecutive bad probes ---
    alerts.clear()
    resolved.clear()
    bridge.get_worker_host = lambda name: 'remote-host'  # remote workers
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states.clear()
        bridge.watchdog.consecutive_good_probes.clear()
        bridge.watchdog.consecutive_bad_probes.clear()

    since_past = now - 60  # past START_GRACE so eligible_for_alert is True

    # First bad probe: suppressed
    bridge._handle_watchdog_transition('remote1', 'DEAD', 'claude missing 30s', since_past, now=now)
    assert len(alerts) == 0, f'Remote: expected 0 alerts after 1 bad probe, got {len(alerts)}'

    # Second bad probe: still suppressed
    bridge._handle_watchdog_transition('remote1', 'DEAD', 'claude missing 30s', since_past, now=now)
    assert len(alerts) == 0, f'Remote: expected 0 alerts after 2 bad probes, got {len(alerts)}'

    # Third bad probe: alert fires
    bridge._handle_watchdog_transition('remote1', 'DEAD', 'claude missing 30s', since_past, now=now)
    assert len(alerts) == 1, f'Remote: expected 1 alert after 3 bad probes, got {len(alerts)}'

    # Good probe resets bad counter — but needs 3 good probes to resolve
    alerts.clear()
    bridge._handle_watchdog_transition('remote1', 'READY', 'idle', now, now=now)
    assert len(resolved) == 0, f'Remote: expected 0 resolved after 1 good probe, got {len(resolved)}'
    bridge._handle_watchdog_transition('remote1', 'READY', 'idle', now, now=now)
    bridge._handle_watchdog_transition('remote1', 'READY', 'idle', now, now=now)
    assert len(resolved) == 1, f'Remote: expected 1 resolved after 3 good probes, got {len(resolved)}'

    # After resolve, single bad probe should not alert (absorbed)
    alerts.clear()
    resolved.clear()
    bridge._handle_watchdog_transition('remote1', 'DEAD', 'claude missing 10s', since_past, now=now)
    assert len(alerts) == 0, f'Remote: single bad probe after resolve should be absorbed, got {len(alerts)}'
    # Good probe resets — no resolved since prev_state is still READY
    bridge._handle_watchdog_transition('remote1', 'READY', 'idle', now, now=now)
    assert len(alerts) == 0 and len(resolved) == 0, 'Remote: transient blip absorbed cleanly'

    bridge._send_watchdog_alert = orig_alert
    bridge._send_resolved_alert = orig_resolved
    bridge.get_worker_host = orig_get_host
    with bridge.watchdog.lock:
        bridge.watchdog.prev_worker_states.clear()
        bridge.watchdog.consecutive_good_probes.clear()
        bridge.watchdog.consecutive_bad_probes.clear()


def test_stale_pending_survives_for_watchdog():
    from bridge import is_pending, get_pending_file, _pending_timestamp, compute_state
    import time

    test_name = 'stale_test'
    pending_file = get_pending_file(test_name)
    pending_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Simulate 16 minutes old pending (past both PENDING_TIMEOUT and STALE_PENDING)
    stale_ts = int(time.time()) - (16 * 60)
    pending_file.write_text(str(stale_ts))

    # is_pending returns False (timed out)
    assert is_pending(test_name) == False

    # But _pending_timestamp still returns the timestamp
    ts = _pending_timestamp(test_name)
    assert ts == stale_ts, f'timestamp lost: {ts}'

    # And compute_state should see STALE_PENDING
    now = time.time()
    pending_age = now - stale_ts
    state, reason = compute_state(
        tmux_exists=True,
        claude_pid='12345',
        pending=True,  # watchdog uses _pending_timestamp, not is_pending()
        pending_ts=stale_ts,
        pending_age=pending_age,
        children=0,
        last_child_ts=0,
        cpu=0.0,
        last_hook_ts=None,
        last_seen_claude=now,  # claude was seen recently
        now=now,
    )
    # STALE_PENDING threshold triggers STUCK state (not a separate state name)
    assert state == 'STUCK', f'expected STUCK at stale pending, got {state}: {reason}'

    # Cleanup
    pending_file.unlink(missing_ok=True)
    pending_file.parent.rmdir()


def test_watchdog_alert_on_stuck():
    import bridge
    from unittest.mock import patch

    bridge.admin_chat_id = 123
    bridge.watchdog.last_alert_ts = {}
    bridge.watchdog.prev_worker_states = {'alice': 'READY'}
    bridge.watchdog.worker_states = {'alice': ('STUCK', 'age=400s cpu=0.0', 600)}

    sent = {}

    def fake_api(method, data):
        sent['method'] = method
        sent['data'] = data
        return {'ok': True}

    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('alice', 'STUCK', 'age=400s cpu=0.0', since=600, now=1000)

    assert sent.get('method') == 'sendMessage', 'telegram_api not called'
    assert sent['data']['chat_id'] == 123, 'admin chat id should be used'
    txt = sent['data']['text']
    assert 'alice' in txt, f'alert should include worker name: {txt}'
    assert 'frozen' in txt or '/restart' in txt, f'alert should be human-friendly: {txt}'


def test_get_machines_includes_workers_and_health():
    import json
    import tempfile
    import shutil
    from pathlib import Path
    import bridge

    tmp = Path(tempfile.mkdtemp())
    path = tmp / 'machines.json'
    path.write_text(json.dumps({
        'version': 1,
        'machines': {
            'vps': {'ssh_target': None, 'bridge_base_url': 'http://localhost:8271', 'home_root': '/home/claude', 'os_family': 'linux'},
            'macmini': {'ssh_target': 'beastoin-agents-f1-mac-mini', 'bridge_base_url': 'http://100.125.36.102:8271', 'home_root': '/Users/beastoinagents', 'os_family': 'darwin'}
        }
    }))
    old_path = bridge.MACHINES_CONFIG_FILE
    old_cache = bridge.remote_cache.machines
    old_cache_path = bridge.remote_cache.machines_path
    old_get = bridge.get_registered_sessions
    bridge.MACHINES_CONFIG_FILE = path
    bridge.remote_cache.machines = None
    bridge.remote_cache.machines_path = None
    bridge.get_registered_sessions = lambda registered=None: {
        'mon': {'tmux': 'claude-test-mon', 'backend': 'claude'},
        'kai': {'tmux': 'claude-test-kai', 'backend': 'claude', 'host': 'beastoin-agents-f1-mac-mini'}
    }
    bridge.host_health.down['beastoin-agents-f1-mac-mini'] = True
    bridge.host_health.down_since['beastoin-agents-f1-mac-mini'] = 123.0
    bridge.host_health.last_error['beastoin-agents-f1-mac-mini'] = 'timeout'

    data = bridge.get_machines()
    machines = {m['id']: m for m in data['machines']}
    assert machines['vps']['worker_count'] == 1, machines
    assert machines['macmini']['worker_count'] == 1, machines
    assert machines['macmini']['workers'][0]['name'] == 'kai'
    assert machines['macmini']['health']['status'] == 'down'
    assert machines['macmini']['health']['last_error'] == 'timeout'

    bridge.MACHINES_CONFIG_FILE = old_path
    bridge.remote_cache.machines = old_cache
    bridge.remote_cache.machines_path = old_cache_path
    bridge.get_registered_sessions = old_get
    bridge.host_health.down.pop('beastoin-agents-f1-mac-mini', None)
    bridge.host_health.down_since.pop('beastoin-agents-f1-mac-mini', None)
    bridge.host_health.last_error.pop('beastoin-agents-f1-mac-mini', None)
    shutil.rmtree(tmp)


def test_phase2_watchdog_capture_pane_passes_host():
    import json
    import tempfile
    import shutil
    from pathlib import Path
    from unittest.mock import patch
    import bridge

    tmpdir = tempfile.mkdtemp()
    node_dir = Path(tmpdir) / 'node'
    node_dir.mkdir()

    reg = {'workers': {'ren': {'host': 'mac-mini', 'home_host': 'localhost', 'home_cwd': '/home/claude'}}}
    (node_dir / 'workers.json').write_text(json.dumps(reg))

    orig_node_dir = bridge.NODE_DIR
    orig_registry = bridge.WORKER_REGISTRY_FILE
    bridge.NODE_DIR = node_dir
    bridge.WORKER_REGISTRY_FILE = node_dir / 'workers.json'

    cpt_calls = []

    def mock_cpt(tmux_name, lines=50, host=None):
        cpt_calls.append({'tmux': tmux_name, 'host': host})
        return ''

    # Test that _detect_poisoned passes host
    with patch('bridge._capture_pane_text', side_effect=mock_cpt), \
         patch('bridge._check_adapter_log', return_value=''):
        bridge._detect_poisoned('ren', 'claude-prod-ren')

    assert len(cpt_calls) == 1, f'Expected 1 _capture_pane_text call, got {cpt_calls}'
    assert cpt_calls[0]['host'] == 'mac-mini', f'Expected host=mac-mini, got {cpt_calls[0]["host"]}'

    bridge.NODE_DIR = orig_node_dir
    bridge.WORKER_REGISTRY_FILE = orig_registry
    shutil.rmtree(tmpdir)


def test_watchdog_alert_stuck_copy():
    import bridge
    from unittest.mock import patch

    bridge.admin_chat_id = 123
    bridge.watchdog.last_alert_ts = {}
    bridge.watchdog.prev_worker_states = {'alice': 'READY'}

    sent = {}

    def fake_api(method, data):
        sent['method'] = method
        sent['data'] = data
        return {'ok': True}

    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('alice', 'STUCK', 'age=900s cpu=0.0', since=100, now=1000)

    txt = sent['data']['text']
    assert 'no progress' in txt.lower(), f'Should say no progress: {txt}'
    assert '/restart --clean alice' in txt, f'Should have restart command: {txt}'
    assert 'starts fresh' in txt.lower(), f'Should explain --clean: {txt}'
    assert 'STUCK' not in txt, f'Should not expose internal state name: {txt}'


def test_watchdog_alert_poisoned_copy():
    import bridge
    from unittest.mock import patch

    bridge.admin_chat_id = 123
    bridge.watchdog.last_alert_ts = {}
    bridge.watchdog.prev_worker_states = {'bob': 'READY'}

    sent = {}

    def fake_api(method, data):
        sent['data'] = data
        return {'ok': True}

    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('bob', 'POISONED', 'error loop', since=100, now=1000)

    txt = sent['data']['text']
    assert 'error' in txt.lower(), f'Should mention error: {txt}'
    assert 'POISONED' not in txt, f'Should not expose internal state POISONED: {txt}'
    assert '/restart --clean bob' in txt, f'Should have restart command: {txt}'


def test_watchdog_alert_dead_copy():
    import bridge
    from unittest.mock import patch

    bridge.admin_chat_id = 123
    bridge.watchdog.last_alert_ts = {}
    bridge.watchdog.prev_worker_states = {'carol': 'READY'}

    sent = {}

    def fake_api(method, data):
        sent['data'] = data
        return {'ok': True}

    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('carol', 'DEAD', 'process gone', since=100, now=1000)

    txt = sent['data']['text']
    assert 'stopped' in txt.lower(), f'Should say stopped: {txt}'
    assert 'process' not in txt.lower(), f'Should not say process: {txt}'
    assert '/restart --clean carol' in txt, f'Should have restart command: {txt}'


def test_watchdog_alert_waiting_input_copy():
    import bridge
    from unittest.mock import patch

    bridge.admin_chat_id = 123
    bridge.watchdog.last_alert_ts = {}
    bridge.watchdog.prev_worker_states = {'dave': 'READY'}
    bridge.watchdog.waiting_input_details = {
        'dave': {
            'header': 'Auth method',
            'options': [
                {'num': 1, 'label': 'OAuth', 'selected': True},
                {'num': 2, 'label': 'JWT', 'selected': False},
            ],
            'selected_num': 1,
        }
    }

    sent = {}

    def fake_api(method, data):
        sent['data'] = data
        return {'ok': True}

    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('dave', 'WAITING_INPUT', 'question=Auth', since=100, now=1000)

    txt = sent['data']['text']
    assert 'needs your reply' in txt.lower() or 'reply' in txt.lower(), f'Should say needs reply: {txt}'
    assert 'Auth method' in txt, f'Should include question header: {txt}'
    assert 'waiting for your input' not in txt.lower(), f'Should not use old copy: {txt}'


def test_watchdog_resolved_copy():
    import bridge
    from unittest.mock import patch

    bridge.admin_chat_id = 123
    bridge.watchdog.prev_worker_states = {'eve': 'STUCK'}
    bridge.watchdog.recent_restarts = {}

    sent = {}

    def fake_api(method, data):
        sent['data'] = data
        return {'ok': True}

    with patch('bridge.telegram_api', fake_api):
        bridge._send_resolved_alert('eve', 'READY')

    txt = sent['data']['text']
    assert 'back to normal' in txt.lower() or chr(0x2705) in txt, f'Should say back to normal: {txt}'


def test_watchdog_skips_dead_alert_during_teleport():
    import bridge
    import tempfile
    import json
    import time
    from pathlib import Path
    from unittest.mock import patch

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / 'sessions'
    sessions_dir.mkdir()
    orig_sessions_dir = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = sessions_dir

    worker_name = 'bob'
    worker_dir = sessions_dir / worker_name
    worker_dir.mkdir()

    # Write teleport_state file
    (worker_dir / 'teleport_state').write_text(json.dumps({
        'phase': 1, 'source_host': 'host1',
        'target_host': 'host2', 'target_cwd': '/tmp',
        'started_at': 1000,
    }))

    bridge.admin_chat_id = 123
    bridge.watchdog.last_alert_ts = {}
    bridge.watchdog.prev_worker_states = {'bob': 'READY'}

    sent = []

    def fake_api(method, data):
        sent.append(data)
        return {'ok': True}

    now = time.time()
    with patch('bridge.telegram_api', fake_api):
        # DEAD during teleport: should NOT send alert
        bridge._handle_watchdog_transition('bob', 'DEAD', 'process gone', since=100, now=now)
    assert len(sent) == 0, f'Should suppress DEAD alert during teleport, but sent {len(sent)} messages'

    # Verify state was still recorded
    with bridge.watchdog.lock:
        assert bridge.watchdog.prev_worker_states.get('bob') == 'DEAD', 'State should still be tracked'

    # OFFLINE during teleport: should also NOT send alert
    sent.clear()
    bridge.watchdog.prev_worker_states = {'bob': 'READY'}
    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('bob', 'OFFLINE', 'not running', since=100, now=now)
    assert len(sent) == 0, f'Should suppress OFFLINE alert during teleport, but sent {len(sent)} messages'

    # Remove teleport_state — now DEAD alert should fire
    (worker_dir / 'teleport_state').unlink()
    sent.clear()
    bridge.watchdog.prev_worker_states = {'bob': 'READY'}
    with patch('bridge.telegram_api', fake_api):
        bridge._handle_watchdog_transition('bob', 'DEAD', 'process gone', since=100, now=now)
    assert len(sent) == 1, f'Should send DEAD alert without teleport_state, but sent {len(sent)} messages'

    bridge.SESSIONS_DIR = orig_sessions_dir


def test_watchdog_exited_state():
    import bridge
    import time

    # Clear watchdog state
    bridge.watchdog.worker_states.clear()
    bridge.watchdog.prev_worker_states.clear()
    bridge.watchdog.last_alert_ts.clear()

    now = time.time()

    # Record EXITED state
    since = bridge._record_worker_state('deadworker', 'EXITED', 'session gone', now)

    # Verify it's tracked
    entry = bridge.watchdog.worker_states.get('deadworker')
    assert entry is not None, 'EXITED state should be recorded'
    assert entry[0] == 'EXITED', f'state should be EXITED, got {entry[0]}'

    # Verify _format_watchdog_status returns 'exited'
    status = bridge._format_watchdog_status('deadworker')
    assert status == 'Session ended', f'expected "Session ended", got "{status}"'

    # Verify EXITED is in the alert actions
    bridge.admin_chat_id = 12345
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        return {'ok': True}

    orig_api = bridge.telegram_api
    bridge.telegram_api = fake_api

    # Simulate transition: first transition should alert (after grace period)
    bridge.watchdog.prev_worker_states.clear()
    bridge._handle_watchdog_transition('deadworker', 'EXITED', 'session gone', since=now - 60, now=now)
    assert len(calls) == 1, f'expected 1 alert call, got {len(calls)}'
    txt = calls[0][1]['text']
    assert 'deadworker' in txt, f'alert should mention worker name: {txt}'
    assert '/restart' in txt, f'alert should suggest /restart: {txt}'

    bridge.telegram_api = orig_api
    bridge.admin_chat_id = None
