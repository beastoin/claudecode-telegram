"""Tests migrated from test.sh — channel category."""
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


def test_channel_create_id():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    cid = bridge.channel_create_id('review')
    assert cid.startswith('ch_'), f'Expected ch_ prefix, got {cid}'
    assert len(cid) <= 10, f'ID too long: {cid}'
    # Uniqueness
    ids = {bridge.channel_create_id() for _ in range(50)}
    assert len(ids) >= 45, f'Too many collisions: {len(ids)} unique out of 50'


def test_channel_new():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch = bridge.channel_new('ch_abc123', 'review', 'guest:alice',
        ['worker:geni', 'guest:alice', 'manager'])
    assert ch['id'] == 'ch_abc123'
    assert ch['label'] == 'review'
    assert ch['seq'] == 0
    assert len(ch['members']) == 3
    assert ch['members']['manager']['type'] == 'manager'
    assert ch['members']['worker:geni']['name'] == 'geni'
    assert ch['members']['guest:alice']['name'] == 'alice'
    assert ch['messages'] == []


def test_channel_add_remove_members():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch = bridge.channel_new('ch_test', 'test', 'manager', ['manager'])
    assert len(ch['members']) == 1

    # Add
    added = bridge.channel_add_members(ch, ['worker:kai', 'guest:bob'])
    assert len(added) == 2
    assert len(ch['members']) == 3

    # Add duplicate (no-op)
    added = bridge.channel_add_members(ch, ['worker:kai'])
    assert len(added) == 0
    assert len(ch['members']) == 3

    # Remove
    removed = bridge.channel_remove_members(ch, ['guest:bob'])
    assert len(removed) == 1
    assert len(ch['members']) == 2
    assert 'guest:bob' not in ch['members']

    # Remove non-existent
    removed = bridge.channel_remove_members(ch, ['guest:nobody'])
    assert len(removed) == 0


def test_channel_append_message():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch = bridge.channel_new('ch_test', 'test', 'manager', ['manager', 'worker:geni'])
    m1 = bridge.channel_append_message(ch, 'worker:geni', 'hello')
    assert m1['id'] == 'cm_000001'
    assert m1['seq'] == 1
    assert m1['from'] == 'worker:geni'
    assert m1['text'] == 'hello'

    m2 = bridge.channel_append_message(ch, 'manager', 'hi back')
    assert m2['id'] == 'cm_000002'
    assert m2['seq'] == 2
    assert len(ch['messages']) == 2


def test_channel_message_cap():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch = bridge.channel_new('ch_test', 'test', 'manager', ['manager'])
    for i in range(250):
        bridge.channel_append_message(ch, 'manager', f'msg {i}')
    assert len(ch['messages']) == 200
    assert ch['messages'][0]['seq'] == 51
    assert ch['messages'][-1]['seq'] == 250
    assert ch['seq'] == 250


def test_channel_get_messages_after():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch = bridge.channel_new('ch_test', 'test', 'manager', ['manager'])
    for i in range(5):
        bridge.channel_append_message(ch, 'manager', f'msg {i}')

    # No filter
    msgs, trunc = bridge.channel_get_messages(ch)
    assert len(msgs) == 5
    assert not trunc

    # After cm_000003
    msgs, trunc = bridge.channel_get_messages(ch, after='cm_000003')
    assert len(msgs) == 2
    assert msgs[0]['id'] == 'cm_000004'
    assert not trunc

    # After last message
    msgs, trunc = bridge.channel_get_messages(ch, after='cm_000005')
    assert len(msgs) == 0
    assert not trunc

    # After unknown (truncated)
    msgs, trunc = bridge.channel_get_messages(ch, after='cm_999999')
    assert trunc
    assert len(msgs) == 5  # returns all current


def test_channel_expiry():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch = bridge.channel_new('ch_test', 'test', 'manager', ['manager'])
    assert not bridge.channel_is_expired(ch)

    # Expired
    ch['expires_at_unix'] = time.time() - 1
    assert bridge.channel_is_expired(ch)


def test_relay_channel_has_tokens():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch, guest_token, reply_token = bridge.relay_channel_create('testworker', 'relay-testworker')
    assert 'guest_token_hash' in ch, 'missing guest_token_hash'
    assert 'reply_token_hash' in ch, 'missing reply_token_hash'
    assert 'reply_token' in ch, 'missing reply_token (plaintext for envelope)'
    assert ch['guest_token_hash'] == hashlib.sha256(guest_token.encode()).hexdigest()
    assert ch['reply_token_hash'] == hashlib.sha256(reply_token.encode()).hexdigest()
    assert ch['reply_token'] == reply_token
    assert 'worker' in ch, 'missing worker field'
    assert ch['worker'] == 'testworker'

