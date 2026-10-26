"""Tests migrated from test.sh — health/watchdog category."""
import pytest

import bridge
import shared
from shared import _res_cfg


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

    # Test _format_watchdog_status_pure with WAITING_INPUT
    now = time.time()
    bridge.watchdog.worker_states['testbot'] = ('WAITING_INPUT', 'question=Pick color', now - 120)
    snapshot = dict(bridge.watchdog.worker_states)
    result = bridge._format_watchdog_status_pure('testbot', pending_lookup=bridge.is_pending, state_snapshot=snapshot, clock_now=now)
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
    result = bridge._format_watchdog_status_pure('ready_worker', pending_lookup=lambda n: False, state_snapshot=snapshot, clock_now=now)
    assert result == 'Ready', f'READY: expected Ready, got {result}'

    # BUSY_TOOL -> 'Working'
    result = bridge._format_watchdog_status_pure('busy_tool_worker', pending_lookup=lambda n: True, state_snapshot=snapshot, clock_now=now)
    assert result == 'Working', f'BUSY_TOOL: expected Working, got {result}'

    # BUSY_THINKING -> 'Thinking'
    result = bridge._format_watchdog_status_pure('busy_thinking_worker', pending_lookup=lambda n: True, state_snapshot=snapshot, clock_now=now)
    assert result == 'Thinking', f'BUSY_THINKING: expected Thinking, got {result}'

    # WAITING -> 'Working'
    result = bridge._format_watchdog_status_pure('waiting_worker', pending_lookup=lambda n: True, state_snapshot=snapshot, clock_now=now)
    assert result == 'Working', f'WAITING: expected Working, got {result}'

    # STUCK -> 'No progress (Xm)'
    result = bridge._format_watchdog_status_pure('stuck_worker', pending_lookup=lambda n: True, state_snapshot=snapshot, clock_now=now)
    assert result.startswith('No progress'), f'STUCK: expected No progress (Xm), got {result}'
    assert 'm)' in result, f'STUCK: expected minutes in parens, got {result}'

    # DEAD -> 'Not responding'
    result = bridge._format_watchdog_status_pure('dead_worker', pending_lookup=lambda n: False, state_snapshot=snapshot, clock_now=now)
    assert result == 'Not responding', f'DEAD: expected Not responding, got {result}'

    # OFFLINE -> 'Offline'
    result = bridge._format_watchdog_status_pure('offline_worker', pending_lookup=lambda n: False, state_snapshot=snapshot, clock_now=now)
    assert result == 'Offline', f'OFFLINE: expected Offline, got {result}'

    # POISONED -> 'Error loop (Xm)'
    result = bridge._format_watchdog_status_pure('poisoned_worker', pending_lookup=lambda n: True, state_snapshot=snapshot, clock_now=now)
    assert result.startswith('Error loop'), f'POISONED: expected Error loop (Xm), got {result}'

    # Unknown worker -> fallback based on pending
    result = bridge._format_watchdog_status_pure('nonexistent_worker', pending_lookup=lambda n: True, state_snapshot=snapshot, clock_now=now)
    assert result == 'Working', f'Unknown+pending: expected Working, got {result}'
    result = bridge._format_watchdog_status_pure('nonexistent_worker', pending_lookup=lambda n: False, state_snapshot=snapshot, clock_now=now)
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

    # Verify _format_watchdog_status_pure returns 'exited'
    import time as _time
    _now = _time.time()
    _snap = dict(bridge.watchdog.worker_states)
    status = bridge._format_watchdog_status_pure('deadworker', pending_lookup=bridge.is_pending, state_snapshot=_snap, clock_now=_now)
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


# ── /status command tests ──────────────────────────────────────────────


class TestCmdStatus:
    """Tests for the /status command — verifies output correctness and no crashes."""

    @pytest.fixture(autouse=True)
    def _import_bridge(self):
        import bridge as _b
        self.bridge = _b

    def _make_router(self):
        """Create a CommandRouter with a mock transport that captures sent messages."""
        sent: list[dict[str, object]] = []

        class FakeLegacy:
            def send_message(self, chat_id, text, **kw):
                sent.append({'chat_id': chat_id, 'text': text, **kw})
                return {'ok': True, 'result': {'message_id': 1}}
            def set_reaction(self, *a, **kw):
                pass

        router = self.bridge.CommandRouter(FakeLegacy(), self.bridge.worker_manager)
        return router, sent

    def test_cmd_status_no_workers(self, tmp_path):
        """cmd_status works with zero workers registered."""
        self.bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
        self.bridge.NODE_DIR = tmp_path
        self.bridge.worker_manager._sessions_cache = None
        orig_scan = self.bridge.worker_manager.scan_tmux_sessions
        self.bridge.worker_manager.scan_tmux_sessions = lambda: {}

        try:
            router, sent = self._make_router()
            result = router.cmd_status(chat_id=123)

            assert result is True
            assert len(sent) == 1
            text = str(sent[0]['text'])
            assert f'v{self.bridge.VERSION}' in text
            assert '0' in text and 'workers' in text
        finally:
            self.bridge.worker_manager.scan_tmux_sessions = orig_scan

    def test_cmd_status_with_busy_worker(self, tmp_path):
        """cmd_status correctly reports BUSY workers using wd.status (not wd.state)."""
        import json as _json
        from claudecode import WorkerStateEntry

        self.bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
        (tmp_path / 'workers.json').write_text(_json.dumps({
            'workers': {
                'alice': {'session': 'test-alice', 'backend': 'claude', 'host': None},
            }
        }))
        self.bridge.worker_manager._sessions_cache = None

        orig_online = self.bridge.worker_manager.is_online
        self.bridge.worker_manager.is_online = lambda name, session=None: True

        self.bridge.watchdog.worker_states['alice'] = WorkerStateEntry(
            status='BUSY', reason='working', since=1000.0
        )

        try:
            router, sent = self._make_router()
            result = router.cmd_status(chat_id=123)

            assert result is True
            text = str(sent[0]['text'])
            assert '1 busy' in text
            assert 'alice' in text
        finally:
            self.bridge.worker_manager.is_online = orig_online
            self.bridge.watchdog.worker_states.pop('alice', None)

    def test_cmd_status_worker_state_uses_status_not_state(self):
        """WorkerStateEntry field is 'status', not 'state' — regression test."""
        from claudecode import WorkerStateEntry

        entry = WorkerStateEntry(status='BUSY', reason='test', since=1.0)
        assert entry.status == 'BUSY'
        assert not hasattr(entry, 'state'), \
            "WorkerStateEntry should not have a 'state' attribute — use 'status'"

    def test_cmd_status_no_emoji(self, tmp_path):
        """Status output should be clean — no emoji characters."""
        import re

        self.bridge.WORKER_REGISTRY_FILE = tmp_path / 'workers.json'
        self.bridge.worker_manager._sessions_cache = None

        router, sent = self._make_router()
        router.cmd_status(chat_id=123)

        text = str(sent[0]['text'])
        emoji_pattern = re.compile(
            r'[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA6F]'
        )
        assert not emoji_pattern.search(text), \
            f"/status output contains emoji: {text}"


# --- JSONL alert log tests ---

def test_alert_log_append_writes_jsonl(tmp_path):
    import json
    import bridge
    import shared
    orig_node = bridge.NODE_DIR
    orig_fh = shared._log_fh
    shared._log_fh = None
    bridge.NODE_DIR = tmp_path
    try:
        eid = bridge._alert_log_append("fire", "gmail", desc="token expired")
        assert len(eid) == 16
        log_file = tmp_path / "bridge.log"
        assert log_file.exists()
        entries = [json.loads(l) for l in log_file.read_text().strip().split("\n")]
        alert_entries = [e for e in entries if e.get("category") == "alert"]
        assert len(alert_entries) == 1
        entry = alert_entries[0]
        assert entry["id"] == eid
        assert entry["event"] == "fire"
        assert entry["key"] == "gmail"
        assert entry["desc"] == "token expired"
        assert "ts" in entry
    finally:
        bridge.NODE_DIR = orig_node
        if shared._log_fh:
            shared._log_fh.close()
        shared._log_fh = orig_fh


def test_alert_log_multiple_appends(tmp_path):
    import json
    import bridge
    import shared
    orig_node = bridge.NODE_DIR
    orig_fh = shared._log_fh
    shared._log_fh = None
    bridge.NODE_DIR = tmp_path
    try:
        bridge._alert_log_append("fire", "poll", desc="timeout")
        bridge._alert_log_append("resolve", "poll", duration_s=120)
        bridge._alert_log_append("fire", "gmail", desc="expired")
        log_file = tmp_path / "bridge.log"
        all_entries = [json.loads(l) for l in log_file.read_text().strip().split("\n")]
        alert_entries = [e for e in all_entries if e.get("category") == "alert"]
        assert len(alert_entries) == 3
        events = [e["event"] for e in alert_entries]
        assert events == ["fire", "resolve", "fire"]
    finally:
        bridge.NODE_DIR = orig_node
        if shared._log_fh:
            shared._log_fh.close()
        shared._log_fh = orig_fh


def test_alerts_active_save_and_load(tmp_path):
    import json
    import time
    import bridge
    orig_node = bridge.NODE_DIR
    bridge.NODE_DIR = tmp_path
    now = time.time()
    with bridge._alert_lock:
        orig_active = dict(bridge._alert_active)
        orig_snoozed = dict(bridge._alert_snoozed)
        bridge._alert_active.clear()
        bridge._alert_snoozed.clear()
        bridge._alert_active["gmail"] = now - 60
        bridge._alert_active["poll"] = now - 300
    try:
        bridge._alerts_active_save()
        active_file = tmp_path / "alerts_active.jsonl"
        assert active_file.exists()
        lines = active_file.read_text().strip().split("\n")
        assert len(lines) == 2
        keys = {json.loads(l)["key"] for l in lines}
        assert keys == {"gmail", "poll"}

        with bridge._alert_lock:
            bridge._alert_active.clear()
        bridge._alerts_active_load()
        with bridge._alert_lock:
            assert "gmail" in bridge._alert_active
            assert "poll" in bridge._alert_active
    finally:
        with bridge._alert_lock:
            bridge._alert_active.clear()
            bridge._alert_active.update(orig_active)
            bridge._alert_snoozed.clear()
            bridge._alert_snoozed.update(orig_snoozed)
        bridge.NODE_DIR = orig_node


def test_health_alert_check_logs_fire_and_resolve(tmp_path):
    import json
    import time
    import bridge
    import shared
    from unittest.mock import patch
    orig_node = bridge.NODE_DIR
    orig_fh = shared._log_fh
    shared._log_fh = None
    bridge.NODE_DIR = tmp_path
    with bridge._alert_lock:
        orig_active = dict(bridge._alert_active)
        orig_sent = dict(bridge._alert_last_sent)
        orig_snoozed = dict(bridge._alert_snoozed)
        bridge._alert_active.clear()
        bridge._alert_last_sent.clear()
        bridge._alert_snoozed.clear()
    orig_workers = bridge.ALERT_WORKERS[:]
    orig_hcid = bridge._health_channel_id
    bridge.ALERT_WORKERS = ["lee"]
    bridge._health_channel_id = "test-ch"
    try:
        bad_snap = {
            "signals": {"gmail": {"ok": False, "last_ok_ago_s": 600, "errors": 0, "last_error": "token expired"}},
            "threads": {},
            "pools": {},
        }
        with patch.object(bridge.health, "snapshot", return_value=bad_snap), \
             patch("bridge._send_health_alert"):
            bridge._health_alert_check()
        log_file = tmp_path / "bridge.log"
        assert log_file.exists()
        all_entries = [json.loads(l) for l in log_file.read_text().strip().split("\n")]
        alert_entries = [e for e in all_entries if e.get("category") == "alert"]
        assert len(alert_entries) >= 1
        assert alert_entries[0]["event"] == "fire"
        assert alert_entries[0]["key"] == "gmail"

        good_snap = {"signals": {"gmail": {"ok": True, "last_ok_ago_s": 1, "errors": 0}}, "threads": {}, "pools": {}}
        with patch.object(bridge.health, "snapshot", return_value=good_snap), \
             patch("bridge._send_health_alert"):
            bridge._health_alert_check()
        all_entries = [json.loads(l) for l in log_file.read_text().strip().split("\n")]
        resolve_events = [e for e in all_entries if e.get("category") == "alert" and e["event"] == "resolve"]
        assert len(resolve_events) >= 1
        assert resolve_events[0]["key"] == "gmail"
    finally:
        with bridge._alert_lock:
            bridge._alert_active.clear()
            bridge._alert_active.update(orig_active)
            bridge._alert_last_sent.clear()
            bridge._alert_last_sent.update(orig_sent)
            bridge._alert_snoozed.clear()
            bridge._alert_snoozed.update(orig_snoozed)
        bridge.ALERT_WORKERS[:] = orig_workers
        bridge._health_channel_id = orig_hcid
        bridge.NODE_DIR = orig_node
        if shared._log_fh:
            shared._log_fh.close()
        shared._log_fh = orig_fh


# --- ResourceAlertState unit tests ---


class TestResourceAlertState:
    def test_initial_state(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        assert not s.alerted
        assert s.first_alert_ts == 0.0
        assert s.last_alert_ts == 0.0
        assert s.re_alert_count == 0

    def test_fire_first_time(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        s.fire(1000.0)
        assert s.alerted
        assert s.first_alert_ts == 1000.0
        assert s.last_alert_ts == 1000.0
        assert s.re_alert_count == 0

    def test_fire_subsequent_increments_count(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        s.fire(1000.0)
        s.fire(1900.0)
        assert s.re_alert_count == 1
        assert s.first_alert_ts == 1000.0
        assert s.last_alert_ts == 1900.0
        s.fire(2800.0)
        assert s.re_alert_count == 2

    def test_recover_resets_all(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        s.fire(1000.0)
        s.fire(1900.0)
        s.recover()
        assert not s.alerted
        assert s.first_alert_ts == 0.0
        assert s.re_alert_count == 0
        assert s.last_alert_ts == 1900.0  # preserved for cooldown

    def test_should_re_alert(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        s.fire(1000.0)
        assert not s.should_re_alert(1000.0, 900)
        assert not s.should_re_alert(1899.0, 900)
        assert s.should_re_alert(1900.0, 900)
        assert s.should_re_alert(2000.0, 900)

    def test_should_re_alert_not_alerted(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        assert not s.should_re_alert(5000.0, 900)

    def test_duration(self):
        from claudecode import ResourceAlertState
        s = ResourceAlertState()
        assert s.duration(5000.0) == 0.0
        s.fire(1000.0)
        assert s.duration(1500.0) == 500.0


# --- _probe_metric re-alerting integration tests ---


class _FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t
    def time(self) -> float:
        return self.t
    def sleep(self, s: float) -> None:
        self.t += s
    def advance(self, s: float) -> None:
        self.t += s


class TestProbeMetricReAlert:
    """Tests _probe_metric re-alerting behavior end-to-end."""

    def setup_method(self):
        self.alerts: list[tuple[str, str]] = []
        self.orig_alert = bridge._watchdog_alert
        bridge._watchdog_alert = lambda cat, txt: self.alerts.append((cat, txt)) if txt else None

        self.clock = _FakeClock(10000.0)
        self.orig_clock_shared = shared._clock
        self.orig_clock_bridge = bridge._clock
        shared._clock = self.clock
        bridge._clock = self.clock

        self.orig_run_metric = bridge._run_metric
        self.metric_value: dict | None = None
        bridge._run_metric = lambda metric, host=None: self.metric_value

        bridge.host_health.reset()

    def teardown_method(self):
        bridge._watchdog_alert = self.orig_alert
        shared._clock = self.orig_clock_shared
        bridge._clock = self.orig_clock_bridge
        bridge._run_metric = self.orig_run_metric
        bridge.host_health.reset()

    def _probe_disk(self):
        bridge._probe_disk_all_hosts(set())

    def test_first_alert_fires(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 1
        assert "critical" in self.alerts[0][1].lower()

    def test_no_repeat_before_interval(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 1
        self.clock.advance(800)
        self._probe_disk()
        assert len(self.alerts) == 1

    def test_re_alert_after_interval(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 1
        self.clock.advance(_res_cfg.disk_re_alert)
        self._probe_disk()
        assert len(self.alerts) == 2
        assert "STILL" in self.alerts[1][1]
        assert "alert #2" in self.alerts[1][1]

    def test_multiple_re_alerts(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        for i in range(3):
            self.clock.advance(_res_cfg.disk_re_alert)
            self._probe_disk()
        assert len(self.alerts) == 4
        assert "alert #4" in self.alerts[3][1]

    def test_recovery_after_re_alerts(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        self.clock.advance(_res_cfg.disk_re_alert)
        self._probe_disk()
        assert len(self.alerts) == 2
        self.metric_value = {"pct": 50, "free_gb": 50.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 3
        assert "recovered" in self.alerts[2][1].lower()

    def test_re_alert_shows_duration(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        self.clock.advance(3600)
        self._probe_disk()
        assert "1h0m" in self.alerts[1][1]

    def test_no_alert_when_below_threshold(self):
        self.metric_value = {"pct": 50, "free_gb": 50.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 0

    def test_cooldown_prevents_rapid_first_alert(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 1
        # Recover
        self.metric_value = {"pct": 50, "free_gb": 50.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 2
        # Go critical again immediately — cooldown blocks
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 2
        # After cooldown — fires
        self.clock.advance(_res_cfg.disk_cooldown)
        self._probe_disk()
        assert len(self.alerts) == 3

    def test_state_isolated_per_host(self):
        self.metric_value = {"pct": 98, "free_gb": 2.0, "total_gb": 100}
        self._probe_disk()
        assert len(self.alerts) == 1
        state_vps = bridge.host_health.disk_alert.get("VPS")
        assert state_vps is not None
        assert state_vps.alerted


class TestFmtDuration:
    def test_seconds(self):
        assert bridge._fmt_duration(30) == "30s"

    def test_minutes(self):
        assert bridge._fmt_duration(300) == "5m0s"
        assert bridge._fmt_duration(330) == "5m30s"

    def test_hours(self):
        assert bridge._fmt_duration(3600) == "1h0m"
        assert bridge._fmt_duration(7200) == "2h0m"

    def test_hours_and_minutes(self):
        assert bridge._fmt_duration(3660) == "1h1m"
        assert bridge._fmt_duration(5400) == "1h30m"

    def test_zero(self):
        assert bridge._fmt_duration(0) == "0s"


# ── _emit_alert unified delivery ─────────────────────────────────────


class TestEmitAlert:
    """Verify _emit_alert fans out to both Telegram and relay/worker delivery."""

    def test_sends_to_telegram_and_relay(self):
        from unittest.mock import MagicMock, patch

        mock_transport = MagicMock()
        mock_transport.send_text.return_value = {"ok": True, "result": {"message_id": 42}}
        orig_transport = bridge.transport
        orig_chat = bridge.admin_chat_id
        bridge.transport = mock_transport
        bridge.admin_chat_id = "12345"
        try:
            with patch.object(bridge, "_send_health_alert") as mock_relay:
                result = bridge._emit_alert("test alert", tag="test")
            mock_transport.send_text.assert_called_once_with("12345", "test alert")
            mock_relay.assert_called_once_with("test alert")
            assert result == {"ok": True, "result": {"message_id": 42}}
        finally:
            bridge.transport = orig_transport
            bridge.admin_chat_id = orig_chat

    def test_edit_message_path(self):
        from unittest.mock import MagicMock, patch

        mock_transport = MagicMock()
        orig_transport = bridge.transport
        orig_chat = bridge.admin_chat_id
        bridge.transport = mock_transport
        bridge.admin_chat_id = "12345"
        try:
            with patch.object(bridge, "_send_health_alert") as mock_relay:
                bridge._emit_alert("resolved", tag="test", edit_msg_id=99)
            mock_transport.edit_message.assert_called_once_with("12345", 99, "resolved")
            mock_transport.send_text.assert_not_called()
            mock_relay.assert_called_once_with("resolved")
        finally:
            bridge.transport = orig_transport
            bridge.admin_chat_id = orig_chat

    def test_no_admin_chat_still_relays(self):
        from unittest.mock import MagicMock, patch

        mock_transport = MagicMock()
        orig_transport = bridge.transport
        orig_chat = bridge.admin_chat_id
        bridge.transport = mock_transport
        bridge.admin_chat_id = None
        try:
            with patch.object(bridge, "_send_health_alert") as mock_relay:
                result = bridge._emit_alert("alert text", tag="test")
            mock_transport.send_text.assert_not_called()
            mock_relay.assert_called_once_with("alert text")
            assert result is None
        finally:
            bridge.transport = orig_transport
            bridge.admin_chat_id = orig_chat

    def test_watchdog_alert_uses_emit(self):
        """_watchdog_alert flows through _emit_alert."""
        from unittest.mock import patch

        with patch.object(bridge, "_emit_alert") as mock_emit:
            bridge._watchdog_alert("Disk", "disk full")
        mock_emit.assert_called_once_with("disk full", tag="watchdog")

    def test_watchdog_alert_skips_none(self):
        from unittest.mock import patch

        with patch.object(bridge, "_emit_alert") as mock_emit:
            bridge._watchdog_alert("Disk", None)
        mock_emit.assert_not_called()

    def test_connector_on_alert_uses_emit(self):
        from unittest.mock import patch

        handler = bridge.connector_on_alert("github")
        with patch.object(bridge, "_emit_alert") as mock_emit:
            handler("GitHub poll failed")
        mock_emit.assert_called_once_with("GitHub poll failed", tag="github")


# --- JSONL staleness detection tests ---

class TestJsonlStalenessCheck:

    def _setup_busy_worker(self, name, busy_since, monkeypatch):
        """Set up a worker that appears BUSY in watchdog state."""
        import threading
        if not hasattr(bridge, 'watchdog') or not hasattr(bridge.watchdog, 'lock'):
            bridge.watchdog = threading.current_thread()
            bridge.watchdog.lock = threading.Lock()
            bridge.watchdog.worker_states = {}
        with bridge.watchdog.lock:
            bridge.watchdog.worker_states[name] = ("BUSY_TOOL", "running tool", busy_since)

    def _cleanup_jsonl_state(self):
        bridge._jsonl_stale_last_alert.clear()
        bridge._jsonl_stale_alerted.clear()

    def test_jsonl_stale_alert_fires_for_busy_worker(self, monkeypatch, tmp_path):
        """Alert fires when BUSY worker's JSONL hasn't updated in >5 minutes."""
        from unittest.mock import patch, MagicMock
        self._cleanup_jsonl_state()
        now = 1000000.0
        busy_since = now - 400  # busy for 400s
        self._setup_busy_worker("testw", busy_since, monkeypatch)
        monkeypatch.setattr(bridge, "get_worker_host", lambda name: None)
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("{}")
        import os
        os.utime(str(jsonl_file), (now - 600, now - 600))  # 10 min old
        monkeypatch.setattr(bridge, "_resolve_transcript_path", lambda name: (str(jsonl_file), "sid1", "/tmp"))
        monkeypatch.setattr(bridge._clock, "time", lambda: now)
        with patch.object(bridge, "_emit_alert", return_value={"ok": True}) as mock_emit, \
             patch.object(bridge, "_alert_log_append"):
            bridge._watchdog_jsonl_staleness_check({"testw": {}})
        assert mock_emit.called
        alert_text = mock_emit.call_args[0][0]
        assert "testw" in alert_text
        assert "transcript not recording" in alert_text
        assert "inotify" in alert_text
        self._cleanup_jsonl_state()

    def test_jsonl_stale_skips_ready_worker(self, monkeypatch, tmp_path):
        """No alert for READY (non-busy) workers even if JSONL is old."""
        from unittest.mock import patch
        self._cleanup_jsonl_state()
        import threading
        if not hasattr(bridge, 'watchdog') or not hasattr(bridge.watchdog, 'lock'):
            bridge.watchdog = threading.current_thread()
            bridge.watchdog.lock = threading.Lock()
            bridge.watchdog.worker_states = {}
        with bridge.watchdog.lock:
            bridge.watchdog.worker_states["readyw"] = ("READY", "idle", 0.0)
        monkeypatch.setattr(bridge._clock, "time", lambda: 1000000.0)
        with patch.object(bridge, "_emit_alert") as mock_emit:
            bridge._watchdog_jsonl_staleness_check({"readyw": {}})
        mock_emit.assert_not_called()
        self._cleanup_jsonl_state()

    def test_jsonl_stale_skips_remote_worker(self, monkeypatch, tmp_path):
        """Remote workers are skipped (can't stat their JSONL locally)."""
        from unittest.mock import patch
        self._cleanup_jsonl_state()
        now = 1000000.0
        self._setup_busy_worker("remotew", now - 400, monkeypatch)
        monkeypatch.setattr(bridge, "get_worker_host", lambda name: "macmini")
        monkeypatch.setattr(bridge._clock, "time", lambda: now)
        with patch.object(bridge, "_emit_alert") as mock_emit:
            bridge._watchdog_jsonl_staleness_check({"remotew": {}})
        mock_emit.assert_not_called()
        self._cleanup_jsonl_state()

    def test_jsonl_stale_no_alert_when_fresh(self, monkeypatch, tmp_path):
        """No alert when JSONL was recently updated (even if worker is busy)."""
        from unittest.mock import patch
        self._cleanup_jsonl_state()
        now = 1000000.0
        self._setup_busy_worker("freshw", now - 400, monkeypatch)
        monkeypatch.setattr(bridge, "get_worker_host", lambda name: None)
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("{}")
        import os
        os.utime(str(jsonl_file), (now - 30, now - 30))  # 30s old — fresh
        monkeypatch.setattr(bridge, "_resolve_transcript_path", lambda name: (str(jsonl_file), "sid1", "/tmp"))
        monkeypatch.setattr(bridge._clock, "time", lambda: now)
        with patch.object(bridge, "_emit_alert") as mock_emit:
            bridge._watchdog_jsonl_staleness_check({"freshw": {}})
        mock_emit.assert_not_called()
        self._cleanup_jsonl_state()

    def test_jsonl_stale_cooldown_prevents_spam(self, monkeypatch, tmp_path):
        """Second alert within cooldown window is suppressed."""
        from unittest.mock import patch
        self._cleanup_jsonl_state()
        now = 1000000.0
        self._setup_busy_worker("spamw", now - 400, monkeypatch)
        monkeypatch.setattr(bridge, "get_worker_host", lambda name: None)
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("{}")
        import os
        os.utime(str(jsonl_file), (now - 600, now - 600))
        monkeypatch.setattr(bridge, "_resolve_transcript_path", lambda name: (str(jsonl_file), "sid1", "/tmp"))
        monkeypatch.setattr(bridge._clock, "time", lambda: now)
        # First alert fires
        with patch.object(bridge, "_emit_alert", return_value={"ok": True}) as mock_emit, \
             patch.object(bridge, "_alert_log_append"):
            bridge._watchdog_jsonl_staleness_check({"spamw": {}})
        assert mock_emit.called
        # Second call — should be suppressed (in _jsonl_stale_alerted set)
        with patch.object(bridge, "_emit_alert") as mock_emit2:
            bridge._watchdog_jsonl_staleness_check({"spamw": {}})
        mock_emit2.assert_not_called()
        self._cleanup_jsonl_state()

    def test_jsonl_stale_clears_on_fresh_update(self, monkeypatch, tmp_path):
        """Alert state clears when JSONL becomes fresh again."""
        from unittest.mock import patch
        self._cleanup_jsonl_state()
        now = 1000000.0
        self._setup_busy_worker("clearw", now - 400, monkeypatch)
        monkeypatch.setattr(bridge, "get_worker_host", lambda name: None)
        jsonl_file = tmp_path / "transcript.jsonl"
        jsonl_file.write_text("{}")
        import os
        os.utime(str(jsonl_file), (now - 600, now - 600))
        monkeypatch.setattr(bridge, "_resolve_transcript_path", lambda name: (str(jsonl_file), "sid1", "/tmp"))
        monkeypatch.setattr(bridge._clock, "time", lambda: now)
        # Fire initial alert
        with patch.object(bridge, "_emit_alert", return_value={"ok": True}), \
             patch.object(bridge, "_alert_log_append"):
            bridge._watchdog_jsonl_staleness_check({"clearw": {}})
        assert "clearw" in bridge._jsonl_stale_alerted
        # Now JSONL becomes fresh
        os.utime(str(jsonl_file), (now - 10, now - 10))
        with patch.object(bridge, "_emit_alert") as mock_emit:
            bridge._watchdog_jsonl_staleness_check({"clearw": {}})
        assert "clearw" not in bridge._jsonl_stale_alerted
        self._cleanup_jsonl_state()
