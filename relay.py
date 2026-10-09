"""Guest, channel, and relay data model — extracted from bridge.py."""
from __future__ import annotations
import hashlib, json, os, secrets, time, threading
from pathlib import Path
from typing import cast, TYPE_CHECKING

from core import _log, _LOG_INFO, _LOG_WARN, _clock, NODE_DIR

if TYPE_CHECKING:
    from bridge import (
        GuestSessionDict, GuestInboxMessageDict,
        ChannelDict, ChannelMemberDict, ChannelMessageDict,
        RelayChannelDict, RelayMessageDict,
    )

# ---------------------------------------------------------------------------
# Shared state-file helpers
# ---------------------------------------------------------------------------
def _save_state_json(path: Path, data: object, label: str) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f: json.dump(data, f, indent=2)
        tmp.rename(path); os.chmod(path, 0o600)
    except OSError as e: _log(_LOG_WARN, label, f"Failed to save state: {e}")

def _load_state_json(path: Path, label: str) -> dict[str, object] | None:
    if not path.exists(): return None
    try:
        with open(path) as f:
            result: object = json.load(f)
            return cast(dict[str, object], result) if isinstance(result, dict) else None
    except (json.JSONDecodeError, OSError, KeyError) as e:
        _log(_LOG_WARN, label, f"Failed to load state: {e}"); return None

# ---------------------------------------------------------------------------
# Guest store
# ---------------------------------------------------------------------------
class GuestStore:
    def __init__(self) -> None:
        self.guests: dict[str, GuestSessionDict] = {}
        self.inboxes: dict[str, list[GuestInboxMessageDict]] = {}
        self.lock: threading.Lock = threading.Lock()
guest_store = GuestStore()
GUEST_TTL = 86400
GUEST_INBOX_CAP = 200

def _guest_state_path() -> Path:
    return NODE_DIR / "guest_state.json"
def _guest_save() -> None:
    now = _clock.time(); active_guests = {}
    for k, v in guest_store.guests.items():
        if now <= v.get("expires_at_unix", 0):
            guest = dict(v)
            if isinstance(guest.get("notified_workers"), set):
                guest["notified_workers"] = list(cast("set[str] | list[str]", guest.get("notified_workers", [])))
            active_guests[k] = guest
    active_names = {guest["name"] for guest in active_guests.values()}
    active_inboxes = {k: v for k, v in guest_store.inboxes.items() if k in active_names}
    _save_state_json(_guest_state_path(), {"guests": active_guests, "inboxes": active_inboxes}, "guest")
def _guest_load() -> None:
    data = _load_state_json(_guest_state_path(), "guest")
    if not data: return
    now = _clock.time(); restored = 0; raw_guests = data.get("guests", {})
    if isinstance(raw_guests, dict):
        for k, v in raw_guests.items():
            if isinstance(v, dict) and now <= v.get("expires_at_unix", 0):
                if isinstance(v.get("notified_workers"), list): v["notified_workers"] = set(v["notified_workers"])
                guest_store.guests[k] = cast("GuestSessionDict", v); restored += 1
    raw_inboxes = data.get("inboxes", {})
    if isinstance(raw_inboxes, dict): guest_store.inboxes.update(cast("dict[str, list[GuestInboxMessageDict]]", raw_inboxes))
    if restored: _log(_LOG_INFO, "guest", f"Restored {restored} active guest(s) from disk")
def guest_create_token() -> tuple[str, str]:
    token = f"gt_{secrets.token_urlsafe(32)}"; token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash
def guest_generate_name(existing_names: set[str] | None = None) -> str:
    if existing_names is None: existing_names = set()
    for _ in range(100):
        name = secrets.token_urlsafe(3).rstrip("=").lower()[:5]
        if len(name) >= 3 and name not in existing_names: return name
    return secrets.token_urlsafe(4).rstrip("=").lower()[:6]
def guest_validate_name(name: str, team_workers: set[str], existing_guests: set[str]) -> tuple[bool, str]:
    if not name or not name.strip(): return False, "name is required"
    name = name.strip().lower()
    if len(name) > 20: return False, "name too long (max 20 chars)"
    if name in team_workers: return False, f"name '{name}' conflicts with a team worker"
    if name in existing_guests: return False, f"name '{name}' already taken by another guest"
    return True, ""
def guest_is_expired(expires_at_unix: float) -> bool:
    return _clock.time() >= expires_at_unix
def guest_inbox_filter(messages: list[GuestInboxMessageDict], after: str | None = None) -> list[GuestInboxMessageDict]:
    if not after: return list(messages)
    found = False; result = []
    for m in messages:
        if found: result.append(m)
        elif m.get("id") == after: found = True
    if not found: return list(messages)
    return result
def guest_inbox_append(inbox: list[GuestInboxMessageDict], msg: GuestInboxMessageDict) -> list[GuestInboxMessageDict]:
    inbox.append(msg)
    if len(inbox) > GUEST_INBOX_CAP: inbox = inbox[-GUEST_INBOX_CAP:]
    return inbox

# ---------------------------------------------------------------------------
# Channel store
# ---------------------------------------------------------------------------
class ChannelStore:
    def __init__(self) -> None:
        self.channels: dict[str, ChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()
channel_store = ChannelStore()
CHANNEL_TTL = 86400
CHANNEL_MSG_CAP = 200

def _channel_state_path() -> Path:
    return NODE_DIR / "channel_state.json"
def _channel_save() -> None:
    active = {cid: ch for cid, ch in channel_store.channels.items() if not channel_is_expired(ch)}
    _save_state_json(_channel_state_path(), active, "channel")
def _channel_load() -> None:
    data = _load_state_json(_channel_state_path(), "channel")
    if not data: return
    restored = 0
    for cid, ch_raw in data.items():
        ch = cast("ChannelDict", ch_raw)
        if not channel_is_expired(ch): channel_store.channels[cid] = ch; restored += 1
    if restored: _log(_LOG_INFO, "channel", f"Restored {restored} active channel(s) from disk")
def channel_create_id(label: str = "") -> str:
    short = secrets.token_urlsafe(4).rstrip("=").lower()[:6]
    return f"ch_{short}"
def _channel_member_entry(m: str) -> tuple[str, ChannelMemberDict] | None:
    if m == "manager": return "manager", cast("ChannelMemberDict", {"type": "manager"})
    if ":" in m:
        mtype, name = m.split(":", 1)
        if mtype in ("worker", "guest"): return m, cast("ChannelMemberDict", {"type": mtype, "name": name})
    return None
def channel_new(channel_id: str, label: str, created_by: str,
                members: list[str], ttl: int = CHANNEL_TTL) -> ChannelDict:
    now = _clock.time()
    member_dict: dict[str, ChannelMemberDict] = {k: v for m in members for pair in [_channel_member_entry(m)] if pair for k, v in [pair]}
    return {"id": channel_id, "label": label, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "expires_at_unix": now + ttl, "seq": 0, "created_by": created_by,
            "members": member_dict, "messages": []}
def channel_add_members(channel: ChannelDict, members: list[str]) -> list[str]:
    added = []
    for m in members:
        if m in channel["members"]: continue
        entry = _channel_member_entry(m)
        if entry: channel["members"][entry[0]] = entry[1]; added.append(m)
    return added
def channel_remove_members(channel: ChannelDict, members: list[str]) -> list[str]:
    removed = []
    for m in members:
        if m in channel["members"]:
            del channel["members"][m]
            removed.append(m)
    return removed
def channel_append_message(channel: ChannelDict, from_member: str, text: str) -> ChannelMessageDict:
    channel["seq"] += 1
    msg = {
        "id": f"cm_{channel['seq']:06d}",
        "seq": channel["seq"],
        "from": from_member,
        "text": text,
        "ts": int(_clock.time()), }
    channel["messages"].append(cast("ChannelMessageDict", msg))
    if len(channel["messages"]) > CHANNEL_MSG_CAP: channel["messages"] = channel["messages"][-CHANNEL_MSG_CAP:]
    return cast("ChannelMessageDict", msg)
def channel_get_messages(channel: ChannelDict, after: str | None = None) -> tuple[list[ChannelMessageDict], bool]:
    if not after: return list(channel["messages"]), False
    found_idx = -1
    for i, m in enumerate(channel["messages"]):
        if m["id"] == after:
            found_idx = i
            break
    if found_idx == -1: return list(channel["messages"]), True
    return channel["messages"][found_idx + 1:], False
def channel_is_expired(channel: ChannelDict) -> bool:
    return _clock.time() > channel["expires_at_unix"]

# ---------------------------------------------------------------------------
# Relay store
# ---------------------------------------------------------------------------
class RelayStore:
    def __init__(self) -> None:
        self.channels: dict[str, RelayChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()
relay_store = RelayStore()
RELAY_PUBLIC_HOST = os.environ.get("RELAY_PUBLIC_HOST", "157.180.48.254")

def _bridge_public_url() -> str:
    import bridge as _b; return _b.BRIDGE_PUBLIC_URL
def _bridge_port() -> int:
    import bridge as _b; return _b.PORT

def _relay_state_path() -> Path:
    return NODE_DIR / "relay_state.json"
def _relay_save() -> None:
    now = _clock.time()
    active = {cid: ch for cid, ch in relay_store.channels.items() if now <= ch["expires_at_unix"]}
    _save_state_json(_relay_state_path(), active, "relay")
def _relay_load() -> None:
    data = _load_state_json(_relay_state_path(), "relay")
    if not data: return
    now = _clock.time(); restored = 0
    for cid, ch_raw in data.items():
        ch = cast("RelayChannelDict", ch_raw)
        if now <= ch.get("expires_at_unix", 0): relay_store.channels[cid] = ch; restored += 1
    if restored: _log(_LOG_INFO, "relay", f"Restored {restored} active relay(s) from disk")
def relay_channel_create(worker: str, label: str, ttl: int = 86400) -> tuple[RelayChannelDict, str, str]:
    channel_id = channel_create_id(label); guest_token = f"gt_{secrets.token_urlsafe(32)}"
    reply_token = f"rt_{secrets.token_urlsafe(32)}"; now = _clock.time()
    channel = {
        "id": channel_id,
        "label": label,
        "worker": worker,
        "workers": [worker],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + ttl)),
        "expires_at_unix": now + ttl,
        "guest_token_hash": hashlib.sha256(guest_token.encode()).hexdigest(),
        "reply_token_hash": hashlib.sha256(reply_token.encode()).hexdigest(),
        "reply_token": reply_token,
        "messages": [], }
    return cast("RelayChannelDict", channel), guest_token, reply_token
def _relay_base_url() -> str:
    pub = _bridge_public_url()
    if pub: return pub
    return f"http://{RELAY_PUBLIC_HOST}:{_bridge_port()}"
def relay_guide_url(channel_id: str, guest_token: str) -> str:
    return f"{_relay_base_url()}/relay/{channel_id}?token={guest_token}"
def relay_guide_text(channel: RelayChannelDict, guest_token: str) -> str:
    base = f"{_relay_base_url()}/relay/{channel['id']}"; w = channel["worker"]; exp = channel['expires_at']
    return (f"# Chat Channel to {w}\nDirect chat channel to **{w}** (a Claude Code agent).\n"
            f'```bash\nexport RELAY_TOKEN="{guest_token}"\nexport RELAY="{base}"\n```\n'
            f"```bash\ncurl -fsS $RELAY/send -H \"Authorization: Bearer $RELAY_TOKEN\" -H \"Content-Type: application/json\" -d '{{\"text\":\"your message here\"}}'\n```\n"
            f"```bash\ncurl -fsS $RELAY/messages -H \"Authorization: Bearer $RELAY_TOKEN\"\n```\n"
            f"1. Send a message using the curl command above\n2. Poll `/messages` to see replies (add `?after=<message_id>` for new messages only)\n3. The channel expires at `{exp}`\n")
def _relay_auth(channel_id: str, token: str, hash_key: str) -> RelayChannelDict | None:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel or _clock.time() > channel.get("expires_at_unix", 0): return None
    if hashlib.sha256(token.encode()).hexdigest() != channel.get(hash_key): return None
    return channel
def relay_auth_guest(channel_id: str, token: str) -> RelayChannelDict | None:
    return _relay_auth(channel_id, token, "guest_token_hash")
def relay_auth_reply(channel_id: str, token: str) -> RelayChannelDict | None:
    return _relay_auth(channel_id, token, "reply_token_hash")
def _relay_append_msg(channel: RelayChannelDict, direction: str, from_: str, to: str, text: str) -> RelayMessageDict:
    msg_id = f"msg_{secrets.token_urlsafe(4)}"
    relay_msg = cast("RelayMessageDict", {"message_id": msg_id, "direction": direction, "from": from_,
                     "to": to, "text": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time()))})
    with relay_store.lock: channel["messages"].append(relay_msg); _relay_save()
    return relay_msg
def relay_guest_send(channel_id: str, text: str) -> tuple[str | None, RelayMessageDict | None]:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel: return None, None
    relay_msg = _relay_append_msg(channel, "guest_to_worker", channel["label"], channel["worker"], text)
    base = f"{_relay_base_url()}/relay/{channel_id}"; rt = channel["reply_token"]
    envelope = (f"[RELAY from {channel['label']}]\nchannel: {channel_id}\nmessage_id: {relay_msg['message_id']}\n"
                f"reply: curl -fsS {base}/reply -H 'Authorization: Bearer {rt}' "
                f"-H 'Content-Type: application/json' -d '{{\"text\":\"YOUR_REPLY\"}}'\n\n{text}\n[/RELAY]")
    return envelope, relay_msg
def relay_worker_reply(channel_id: str, text: str) -> RelayMessageDict | None:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel: return None
    return _relay_append_msg(channel, "worker_to_guest", channel["worker"], channel["label"], text)
def relay_get_messages(channel_id: str, after: str | None = None) -> list[RelayMessageDict]:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel: return []
    msgs = channel["messages"]
    if not after: return list(msgs)
    for i, m in enumerate(msgs):
        if m["message_id"] == after: return list(msgs[i + 1:])
    return list(msgs)
