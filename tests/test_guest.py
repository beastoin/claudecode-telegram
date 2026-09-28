"""Tests migrated from test.sh — guest category."""
import os
import pytest
from unittest.mock import MagicMock, patch

import sys, os
import sys, os, hashlib
import sys, os, time


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


def test_guest_token_generation():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    token, token_hash = bridge.guest_create_token()
    assert token.startswith('gt_'), f'Token must start with gt_, got {token!r}'
    assert len(token) > 10, f'Token too short: {token!r}'
    expected_hash = hashlib.sha256(token.encode()).hexdigest()
    assert token_hash == expected_hash, f'Hash mismatch: {token_hash} != {expected_hash}'


def test_guest_name_generation():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    names = set()
    for _ in range(20):
        name = bridge.guest_generate_name(existing_names=names)
        assert len(name) <= 6, f'Name too long: {name!r}'
        assert len(name) >= 3, f'Name too short: {name!r}'
        assert name not in names, f'Duplicate name: {name!r}'
        names.add(name)
    assert len(names) == 20, f'Expected 20 unique names, got {len(names)}'


def test_guest_name_validation():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    team_workers = {'lee', 'geni', 'kai'}
    # Should reject team worker names
    ok, err = bridge.guest_validate_name('lee', team_workers, set())
    assert not ok, f'Should reject team name lee: {err}'
    # Should reject empty
    ok, err = bridge.guest_validate_name('', team_workers, set())
    assert not ok, f'Should reject empty: {err}'
    # Should reject too long (>20 chars)
    ok, err = bridge.guest_validate_name('a' * 21, team_workers, set())
    assert not ok, f'Should reject too long: {err}'
    # Should reject existing guest name
    ok, err = bridge.guest_validate_name('fox', team_workers, {'fox'})
    assert not ok, f'Should reject existing guest: {err}'
    # Should accept valid name
    ok, err = bridge.guest_validate_name('fox3k', team_workers, set())
    assert ok, f'Should accept valid name: {err}'


def test_guest_expiry_check():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    # Expired session
    assert bridge.guest_is_expired(time.time() - 100) == True
    # Valid session
    assert bridge.guest_is_expired(time.time() + 3600) == False
    # Edge: exactly now
    assert bridge.guest_is_expired(time.time()) == True


def test_guest_inbox_after_filter():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    msgs = [
        {'id': 'gm_001', 'from': 'lee', 'text': 'hello', 'ts': 1000},
        {'id': 'gm_002', 'from': 'lee', 'text': 'world', 'ts': 1001},
        {'id': 'gm_003', 'from': 'guest', 'text': 'thanks', 'ts': 1002},
    ]
    # No filter — all messages
    result = bridge.guest_inbox_filter(msgs, after=None)
    assert len(result) == 3

    # After gm_001 — should get 2
    result = bridge.guest_inbox_filter(msgs, after='gm_001')
    assert len(result) == 2
    assert result[0]['id'] == 'gm_002'

    # After gm_003 — should get 0
    result = bridge.guest_inbox_filter(msgs, after='gm_003')
    assert len(result) == 0

    # After nonexistent — return all
    result = bridge.guest_inbox_filter(msgs, after='gm_999')
    assert len(result) == 3


def test_guest_inbox_cap():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    inbox = []
    for i in range(250):
        inbox = bridge.guest_inbox_append(inbox, {'id': f'gm_{i:03d}', 'text': f'msg {i}'})
    assert len(inbox) == 200, f'Expected 200, got {len(inbox)}'
    # Oldest should be gm_050 (first 50 dropped)
    assert inbox[0]['id'] == 'gm_050', f'Expected gm_050, got {inbox[0]["id"]}'
    assert inbox[-1]['id'] == 'gm_249'
