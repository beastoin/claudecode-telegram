"""Tests migrated from test.sh — relay category."""
import os
import pytest
from unittest.mock import MagicMock, patch

import sys, os
import time


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


def test_relay_guide_url_format():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch, guest_token, reply_token = bridge.relay_channel_create('testworker', 'relay-testworker')
    url = bridge.relay_guide_url(ch['id'], guest_token)
    assert '/v1/' in url, f'URL must contain /v1/: {url}'
    assert ch['id'] in url, f'URL must contain channel ID: {url}'
    assert 'token=' in url, f'URL must contain token param: {url}'
    assert guest_token in url, f'URL must contain actual token: {url}'


def test_relay_auth_validates_token():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch, guest_token, reply_token = bridge.relay_channel_create('testworker', 'relay-testworker')
    cid = ch['id']
    bridge.relay_store.channels[cid] = ch

    # Good token works
    assert bridge.relay_auth_guest(cid, guest_token) is not None, 'valid guest token rejected'
    # Bad token fails
    assert bridge.relay_auth_guest(cid, 'bad-token') is None, 'bad token accepted'
    # Wrong channel fails
    assert bridge.relay_auth_guest('ch_nonexistent', guest_token) is None, 'nonexistent channel accepted'

    # Reply token auth
    assert bridge.relay_auth_reply(cid, reply_token) is not None, 'valid reply token rejected'
    assert bridge.relay_auth_reply(cid, 'bad-reply') is None, 'bad reply token accepted'

    # Expired channel
    ch['expires_at_unix'] = time.time() - 10
    assert bridge.relay_auth_guest(cid, guest_token) is None, 'expired channel accepted'


def test_relay_reply_records_message():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch, guest_token, reply_token = bridge.relay_channel_create('testworker', 'relay-testworker')
    cid = ch['id']
    bridge.relay_store.channels[cid] = ch

    msg = bridge.relay_worker_reply(cid, 'Here is my answer')
    assert msg is not None, 'reply returned None'
    assert msg['text'] == 'Here is my answer'
    assert msg['direction'] == 'worker_to_guest'
    assert msg['from'] == 'testworker'

    # Message should be in channel messages
    msgs = bridge.relay_get_messages(cid)
    assert len(msgs) == 1
    assert msgs[0]['text'] == 'Here is my answer'


def test_relay_messages_poll():
    import bridge

    sys.path.insert(0, os.getcwd())
    os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test:token')

    ch, guest_token, reply_token = bridge.relay_channel_create('testworker', 'relay-testworker')
    cid = ch['id']
    bridge.relay_store.channels[cid] = ch

    # Send then reply
    _, msg1 = bridge.relay_guest_send(cid, 'msg1')
    msg2 = bridge.relay_worker_reply(cid, 'reply1')
    _, msg3 = bridge.relay_guest_send(cid, 'msg2')

    # All messages
    all_msgs = bridge.relay_get_messages(cid)
    assert len(all_msgs) == 3, f'expected 3 msgs, got {len(all_msgs)}'

    # After filter
    after_msgs = bridge.relay_get_messages(cid, after=msg1['message_id'])
    assert len(after_msgs) == 2, f'expected 2 msgs after msg1, got {len(after_msgs)}'
    assert after_msgs[0]['text'] == 'reply1'

    after_msgs2 = bridge.relay_get_messages(cid, after=msg2['message_id'])
    assert len(after_msgs2) == 1, f'expected 1 msg after msg2, got {len(after_msgs2)}'

