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
    assert '/relay/' in url, f'URL must contain /relay/: {url}'
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


def test_cmd_relay_multi_worker_create():
    """Creating a relay with multiple workers: /relay mio sui geni."""
    import bridge

    # Mock router and registered sessions
    router = MagicMock()
    router.reply = MagicMock()
    chat_id = 12345

    # Register fake workers
    with patch.object(bridge, 'get_registered_sessions', return_value={'mio': {}, 'sui': {}, 'geni': {}}):
        result = bridge.cmd_relay(router, "mio sui geni", chat_id)

    assert result is True
    # Should have created a relay
    router.reply.assert_called_once()
    reply_text = router.reply.call_args[0][1]
    assert "mio" in reply_text
    assert "sui" in reply_text
    assert "geni" in reply_text
    assert "📡" in reply_text

    # Verify channel has all workers
    with bridge.relay_store.lock:
        channels = list(bridge.relay_store.channels.values())
    last_ch = channels[-1]
    assert set(last_ch["workers"]) == {"mio", "sui", "geni"}


def test_cmd_relay_multi_worker_not_found():
    """Multi-worker relay creation rejects unknown workers."""
    import bridge

    router = MagicMock()
    chat_id = 12345

    with patch.object(bridge, 'get_registered_sessions', return_value={'mio': {}}):
        bridge.cmd_relay(router, "mio unknown_worker", chat_id)

    reply_text = router.reply.call_args[0][1]
    assert "not found" in reply_text.lower()
    assert "unknown_worker" in reply_text


def test_cmd_relay_add_multi_worker():
    """Adding multiple workers at once: /relay add <id> worker1 worker2."""
    import bridge

    ch, guest_token, _ = bridge.relay_channel_create('mio', 'relay-mio')
    cid = ch['id']
    bridge.relay_store.channels[cid] = ch

    router = MagicMock()
    chat_id = 12345

    with patch.object(bridge, 'get_registered_sessions', return_value={'mio': {}, 'sui': {}, 'geni': {}}):
        bridge.cmd_relay_add(router, ["add", cid, "sui", "geni"], chat_id)

    reply_text = router.reply.call_args[0][1]
    assert "sui" in reply_text
    assert "geni" in reply_text

    # Verify channel now has all 3
    assert set(ch["workers"]) == {"mio", "sui", "geni"}


def test_cmd_relay_add_skips_duplicates():
    """Adding workers that are already in the relay doesn't duplicate."""
    import bridge

    ch, _, _ = bridge.relay_channel_create('mio', 'relay-mio')
    cid = ch['id']
    bridge.relay_store.channels[cid] = ch

    router = MagicMock()
    chat_id = 12345

    with patch.object(bridge, 'get_registered_sessions', return_value={'mio': {}, 'sui': {}}):
        bridge.cmd_relay_add(router, ["add", cid, "mio", "sui"], chat_id)

    # mio was already there, sui is new — should add only sui
    reply_text = router.reply.call_args[0][1]
    assert "sui" in reply_text
    assert ch["workers"].count("mio") == 1  # no duplicates


def test_relay_guide_is_bash_script():
    """Guide text is a ready-to-run bash script with functions."""
    import bridge

    ch, guest_token, _ = bridge.relay_channel_create('mio', 'relay-mio')
    ch["workers"] = ["mio", "sui"]
    guide = bridge.relay_guide_text(ch, guest_token)

    assert guide.startswith("#!/usr/bin/env bash"), "guide must be a bash script"
    assert "relay_send()" in guide, "guide must define relay_send function"
    assert "relay_messages()" in guide, "guide must define relay_messages function"
    assert "relay_poll()" in guide, "guide must define relay_poll function"
    assert "mio" in guide and "sui" in guide, "guide must list all workers"
    assert guest_token in guide, "guide must contain the token"

