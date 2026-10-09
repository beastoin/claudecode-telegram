"""Guest and channel HTTP handler implementations.

Extracted from bridge.py Handler class to reduce core LOC.
All functions take (handler, ...) where handler is the BaseHTTPRequestHandler.
"""
from __future__ import annotations
import hashlib
import json
import secrets
import time
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse, parse_qs, ParseResult
import claudecode
import health
from core import (
    _str_field, _int_field, _bool_field, _clock, _log, _LOG_INFO, _LOG_WARN,
    BRIDGE_URL, SESSIONS_DIR, TMUX_PREFIX,
)
from relay import (
    guest_store, channel_store,
    guest_is_expired, guest_validate_name, guest_generate_name,
    guest_create_token, guest_inbox_append, guest_inbox_filter, _guest_save,
    channel_is_expired, channel_create_id, channel_new,
    channel_add_members, channel_remove_members,
    channel_append_message, channel_get_messages, _channel_save,
    GUEST_TTL, CHANNEL_TTL, GuestSessionDict,
)

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

def _fanout_channel_message(channel_id: str, from_member: str,
                            text: str, msg: dict,
                            members_snapshot: dict,
                            registered: dict) -> None:
    tagged = f"[{channel_id} from {from_member}] {text}"
    for member_key, minfo in members_snapshot.items():
        if member_key == from_member: continue
        if minfo["type"] == "worker":
            wname = minfo.get("name", "")
            if wname and wname in registered:
                winfo = registered[wname]; backend_name = claudecode.get_worker_backend(wname, winfo)
                backend = claudecode.get_backend(backend_name)
                try:
                    backend.send(wname, f"{TMUX_PREFIX}{wname}", tagged, BRIDGE_URL, SESSIONS_DIR)
                except (ConnectionError, TimeoutError) as e: _log(_LOG_WARN, "bridge", f"Channel fan-out to {wname} failed: {e}")
        elif minfo["type"] == "guest":
            gname = minfo.get("name", "")
            if gname:
                with guest_store.lock:
                    ginbox = guest_store.inboxes.get(gname, [])
                    guest_store.inboxes[gname] = guest_inbox_append(ginbox, {
                        "id": msg["id"], "from": from_member,
                        "channel": channel_id, "text": text, "ts": msg["ts"],
                    })
        elif minfo["type"] == "manager":
            from bridge import _notify_admin
            _notify_admin(f"[{channel_id}] {from_member}: {text}")


def _guest_auth(handler: "BaseHTTPRequestHandler", parsed: ParseResult | None = None) -> dict | None:
    """Authenticate guest via token. Returns guest dict or None (sends error)."""
    query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(handler.path).query)
    token = query_params.get("token", [""])[0]
    if not token: handler._send_error_json(403, "token required"); return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with guest_store.lock: guest = guest_store.guests.get(token_hash)
    if not guest or guest_is_expired(guest["expires_at_unix"]):
        if guest:
            with guest_store.lock:
                guest_store.guests.pop(token_hash, None)
                _guest_save()
        handler._send_error_json(403, "invalid or expired guest session")
        return None
    return guest


def _channel_guest_auth(handler: "BaseHTTPRequestHandler", query_params: dict) -> str | None:
    """Authenticate guest for channel context. Returns member string, '' on auth failure, None if no token."""
    token = query_params.get("token", [None])[0]
    if not token: return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with guest_store.lock: guest = guest_store.guests.get(token_hash)
    if not guest or guest_is_expired(guest["expires_at_unix"]):
        handler._send_error_json(403, "invalid or expired token"); return ""
    return f"guest:{guest['name']}"


def handle_guest_register(handler: "BaseHTTPRequestHandler", body: bytes = b"") -> None:
    from bridge import _notify_admin, _relay_base_url
    try: data = cast(dict, json.loads(body)) if body else {}
    except (json.JSONDecodeError, ValueError): data = {}
    requested_name = _str_field(data, "name").strip().lower(); team_workers = set(health.get_registered_sessions().keys())
    with guest_store.lock: existing_guests = {g["name"] for g in guest_store.guests.values()}
    if requested_name:
        ok, err = guest_validate_name(requested_name, team_workers, existing_guests)
        if not ok:
            status = 409 if "conflicts" in err or "already taken" in err else 400
            handler._send_json(status, {"ok": False, "error": err})
            return
        name = requested_name
    else: name = guest_generate_name(existing_names=team_workers | existing_guests)
    token, token_hash = guest_create_token()
    now = _clock.time(); expires_at_unix = now + GUEST_TTL
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_unix))
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    guest = cast(GuestSessionDict, {"name": name, "token_hash": token_hash, "created_at": created_at,
        "expires_at": expires_at, "expires_at_unix": expires_at_unix, "notified_workers": set()})
    with guest_store.lock: guest_store.guests[token_hash] = guest; guest_store.inboxes[name] = []; _guest_save()
    base_url = _relay_base_url()
    listen_script = (f'python3 -c "import json,time,urllib.request as u;TOKEN,URL=\'{token}\',\'{base_url}\';last=\'\';'
                     f'print(\'[listener] connected\',flush=True)\nwhile True:\n try:\n  q=URL+\'/guests/inbox?token=\'+TOKEN+('
                     f'\'&after=\'+last if last else \'\');d=json.loads(u.urlopen(u.Request(q),timeout=10).read())\n  for m in d.get(\'messages\',[]):'
                     f'\n   print(m.get(\'from\',\'?\')+\': \'+m[\'text\'],flush=True);last=m[\'id\']\n except Exception as e:\n  '
                     f'if \'403\' in str(e) or \'404\' in str(e):print(\'[listener] expired\',flush=True);break\n time.sleep(3)"')
    _notify_admin(f"\U0001f514 Guest \"{name}\" connected"); _log(_LOG_INFO, "guest", f"Guest registered: {name} (expires {expires_at})")
    handler._send_json(200, {"ok": True, "name": name, "token": token, "expires": expires_at,
        "inbox_url": f"/guests/inbox?token={token}", "send_url": f"/guests/send?token={token}",
        "channels_url": f"/channels?token={token}", "channel_create_url": f"/channels?token={token}",
        "listen_script": listen_script})


def handle_guest_send(handler: "BaseHTTPRequestHandler", body: bytes = b"") -> None:
    from bridge import _notify_admin
    parsed = urlparse(handler.path); guest = _guest_auth(handler, parsed)
    if not guest: return
    data = handler._parse_body(body)
    if data is None: return
    text = _str_field(data, "text").strip()
    if not text: handler._send_error_json(400, "text required"); return
    raw_to = data.get("to", _str_field(data, "worker"))
    if isinstance(raw_to, str): targets = [raw_to.strip()] if raw_to.strip() else []
    elif isinstance(raw_to, list): targets = [t.strip() for t in raw_to if isinstance(t, str) and t.strip()]
    else: targets = []
    if not targets: handler._send_error_json(400, "to (or worker) required"); return
    guest_name = guest["name"]; from_member = f"guest:{guest_name}"; tagged_text = f"[guest:{guest_name}] {text}"
    registered = health.get_registered_sessions(); results = []
    for target in targets:
        if target.startswith("#") or target.startswith("ch_"):
            with channel_store.lock:
                if target.startswith("#"):
                    ch_id = next((cid for cid, ch in channel_store.channels.items()
                                  if ch["label"] == target[1:] and not channel_is_expired(ch)), None)
                else: ch_id = target
                channel = channel_store.channels.get(ch_id) if ch_id else None
                if not channel or channel_is_expired(channel):
                    results.append({"target": target, "ok": False, "error": "channel not found"})
                    continue
                if from_member not in channel["members"]:
                    results.append({"target": target, "ok": False, "error": "not a member"})
                    continue
                msg = channel_append_message(channel, from_member, text)
                members_snapshot = dict(channel["members"])
            _fanout_channel_message(ch_id, from_member, text, msg, members_snapshot, registered)
            results.append({"target": target, "ok": True, "channel": ch_id, "message_id": msg["id"]})
        elif target.startswith("guest:"):
            target_guest = target.split(":", 1)[1]; msg_id = f"gm_{secrets.token_hex(4)}"
            with guest_store.lock:
                ginbox = guest_store.inboxes.get(target_guest, [])
                guest_store.inboxes[target_guest] = guest_inbox_append(ginbox, {
                    "id": msg_id, "from": from_member,
                    "text": text, "ts": int(_clock.time()),
                })
            results.append({"target": target, "ok": True, "message_id": msg_id})
        else:
            worker = target
            if worker not in registered:
                results.append({"target": worker, "ok": False, "error": f"worker '{worker}' not found"})
                continue
            info = registered[worker]; backend_name = claudecode.get_worker_backend(worker, info)
            backend = claudecode.get_backend(backend_name); tmux_name = f"{TMUX_PREFIX}{worker}"
            delivered = backend.send(worker, tmux_name, tagged_text, BRIDGE_URL, SESSIONS_DIR)
            msg_id = f"gm_{secrets.token_hex(4)}"
            with guest_store.lock:
                inbox = guest_store.inboxes.get(guest_name, [])
                guest_store.inboxes[guest_name] = guest_inbox_append(inbox, {
                    "id": msg_id, "from": guest_name, "to": worker,
                    "text": text, "ts": int(_clock.time()),
                })
                notified = guest.get("notified_workers", set())
                if worker not in notified:
                    if isinstance(notified, set): notified.add(worker)
                    guest["notified_workers"] = notified
                    _notify_admin(f"\U0001f514 Guest \"{guest_name}\" → {worker}")
            results.append({"target": worker, "ok": True, "delivered": delivered, "message_id": msg_id})
    if len(results) == 1: handler._send_json(200, {**results[0], "ok": results[0].get("ok", True)})
    else: handler._send_json(200, {"ok": all(r.get("ok") for r in results), "results": results})


def handle_guest_reply(handler: "BaseHTTPRequestHandler", body: bytes = b"") -> None:
    data = handler._parse_body(body)
    if data is None: return
    guest_name = _str_field(data, "guest").strip(); from_worker = _str_field(data, "from").strip()
    text = _str_field(data, "text").strip()
    if not guest_name or not text: handler._send_error_json(400, "guest and text required"); return
    msg_id = f"gm_{secrets.token_hex(4)}"
    with guest_store.lock:
        if guest_name not in guest_store.inboxes: guest_store.inboxes[guest_name] = []
        guest_store.inboxes[guest_name] = guest_inbox_append(
            guest_store.inboxes[guest_name],
            {"id": msg_id, "from": from_worker or "worker", "text": text, "ts": int(_clock.time())}, )
    handler._send_json(200, {"ok": True, "message_id": msg_id})


def handle_guest_inbox(handler: "BaseHTTPRequestHandler", parsed: ParseResult) -> None:
    guest = _guest_auth(handler, parsed)
    if not guest: return
    query_params = parse_qs(parsed.query); after = query_params.get("after", [None])[0]; guest_name = guest["name"]
    with guest_store.lock: msgs = list(guest_store.inboxes.get(guest_name, []))
    filtered = guest_inbox_filter(msgs, after=after)
    handler._send_json(200, { "ok": True, "name": guest_name, "messages": filtered, })


def handle_guest_status(handler: "BaseHTTPRequestHandler", parsed: ParseResult) -> None:
    guest = _guest_auth(handler, parsed)
    if not guest: return
    with guest_store.lock: workers = list(guest.get("notified_workers", set()))
    handler._send_json(200, {"ok": True, "name": guest["name"], "expires": guest["expires_at"], "connected_workers": workers})


def handle_guests_list(handler: "BaseHTTPRequestHandler") -> None:
    with guest_store.lock:
        guests_list = []; expired = []
        for th, g in guest_store.guests.items():
            if guest_is_expired(g["expires_at_unix"]): expired.append(th)
            else: guests_list.append({"name": g["name"], "expires": g["expires_at"], "connected_workers": list(g.get("notified_workers", set()))})
        for th in expired: guest_store.inboxes.pop(guest_store.guests.pop(th, {}).get("name", ""), None)
        if expired: _guest_save()
    handler._send_json(200, {"ok": True, "guests": guests_list})


def handle_guest_disconnect(handler: "BaseHTTPRequestHandler", parsed: ParseResult) -> None:
    from bridge import _notify_admin
    query_params = parse_qs(parsed.query); token = query_params.get("token", [""])[0]
    if not token: handler._send_error_json(403, "token required"); return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with guest_store.lock:
        guest = guest_store.guests.pop(token_hash, None)
        if guest: guest_store.inboxes.pop(guest["name"], None); _guest_save()
    if not guest: handler._send_error_json(403, "invalid token"); return
    _notify_admin(f"\U0001f514 Guest \"{guest['name']}\" disconnected"); _log(_LOG_INFO, "guest", f"Guest disconnected: {guest['name']}")
    handler._send_json(200, {"ok": True, "name": guest["name"]})


def handle_channel_create(handler: "BaseHTTPRequestHandler", body: bytes = b"") -> None:
    from bridge import _notify_admin
    data = handler._parse_body(body)
    if data is None: return
    label = _str_field(data, "label").strip(); members = data.get("members", [])
    include_manager = _bool_field(data, "include_manager", True)
    ttl = min(_int_field(data, "ttl_seconds", CHANNEL_TTL), CHANNEL_TTL)
    if not isinstance(members, list): handler._send_error_json(400, "members must be a list"); return
    valid_members = []
    for m in members:
        if isinstance(m, str) and (m == "manager" or ":" in m): valid_members.append(m)
    if include_manager and "manager" not in valid_members: valid_members.append("manager")
    parsed = urlparse(handler.path); query_params = parse_qs(parsed.query)
    guest_member = _channel_guest_auth(handler, query_params)
    if guest_member == "": return
    created_by = guest_member or "manager"
    if guest_member and guest_member not in valid_members: valid_members.append(guest_member)
    channel_id = channel_create_id(label); channel = channel_new(channel_id, label, created_by, valid_members, ttl)
    with channel_store.lock:
        channel_store.channels[channel_id] = channel
        _channel_save()
    member_str = ", ".join(valid_members)
    _notify_admin(f"\U0001f4e2 Channel {channel_id} created by {created_by}\nMembers: {member_str}")
    _log(_LOG_INFO, "channel", f"Channel created: {channel_id} by {created_by} members=[{member_str}]")
    handler._send_json(200, {"ok": True, "channel": channel_id, "label": label, "members": valid_members,
        "send_url": f"/channels/{channel_id}/send", "messages_url": f"/channels/{channel_id}/messages"})


def handle_channel_members(handler: "BaseHTTPRequestHandler", channel_id: str, body: bytes = b"") -> None:
    from bridge import _notify_admin
    data = handler._parse_body(body)
    if data is None: return
    with channel_store.lock:
        channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel): handler._send_error_json(404, "channel not found"); return
        added = channel_add_members(channel, data.get("add", []) if isinstance(data.get("add", []), list) else [])
        removed = channel_remove_members(channel, data.get("remove", []) if isinstance(data.get("remove", []), list) else [])
        current = list(channel["members"].keys())
    if added or removed:
        _notify_admin(f"\U0001f4e2 Channel {channel_id}: {'; '.join((['added ' + ', '.join(added)] if added else []) + (['removed ' + ', '.join(removed)] if removed else []))}")
    handler._send_json(200, {"ok": True, "channel": channel_id, "added": added, "removed": removed, "members": current})


def handle_channel_send(handler: "BaseHTTPRequestHandler", channel_id: str, body: bytes = b"") -> None:
    data = handler._parse_body(body)
    if data is None: return
    text = _str_field(data, "text").strip()
    if not text: handler._send_error_json(400, "text required"); return
    parsed = urlparse(handler.path); query_params = parse_qs(parsed.query)
    guest_member = _channel_guest_auth(handler, query_params)
    if guest_member == "": return
    from_member = guest_member or _str_field(data, "from") or "manager"
    with channel_store.lock:
        channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel): handler._send_error_json(404, "channel not found"); return
        if from_member not in channel["members"] and from_member != "manager": handler._send_error_json(403, f"{from_member} not a member"); return
        msg = channel_append_message(channel, from_member, text); members_snapshot = dict(channel["members"])
    _fanout_channel_message(channel_id, from_member, text, msg, members_snapshot, health.get_registered_sessions())
    handler._send_json(200, {"ok": True, "channel": channel_id, "message_id": msg["id"], "seq": msg["seq"]})


def handle_channel_messages(handler: "BaseHTTPRequestHandler", channel_id: str, parsed: ParseResult) -> None:
    query_params = parse_qs(parsed.query); after = query_params.get("after", [None])[0]
    from_member = _channel_guest_auth(handler, query_params)
    if from_member == "": return
    with channel_store.lock:
        channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel): handler._send_error_json(404, "channel not found"); return
        if from_member and from_member not in channel["members"]: handler._send_error_json(403, "not a member of this channel"); return
        msgs, truncated = channel_get_messages(channel, after)
    resp: dict = {"ok": True, "channel": channel_id, "messages": msgs}
    if truncated: resp["truncated"] = True
    handler._send_json(200, resp)


def handle_channels_list(handler: "BaseHTTPRequestHandler", parsed: ParseResult | None = None) -> None:
    query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(handler.path).query)
    filter_member = _channel_guest_auth(handler, query_params)
    if filter_member == "": return
    with channel_store.lock:
        active = []; expired_ids = []
        for cid, ch in channel_store.channels.items():
            if channel_is_expired(ch): expired_ids.append(cid)
            else:
                if filter_member and filter_member not in ch["members"]: continue
                active.append({"id": ch["id"], "label": ch["label"], "members": list(ch["members"].keys()),
                    "message_count": len(ch["messages"]), "created_by": ch["created_by"],
                    "send_url": f"/channels/{ch['id']}/send", "messages_url": f"/channels/{ch['id']}/messages"})
        for cid in expired_ids: del channel_store.channels[cid]
        if expired_ids: _channel_save()
    handler._send_json(200, {"ok": True, "channels": active})


def handle_channel_delete(handler: "BaseHTTPRequestHandler", channel_id: str) -> None:
    from bridge import _notify_admin
    with channel_store.lock:
        channel = channel_store.channels.pop(channel_id, None)
        if channel: _channel_save()
    if not channel: handler._send_error_json(404, "channel not found"); return
    _notify_admin(f"\U0001f4e2 Channel {channel_id} closed"); _log(_LOG_INFO, "channel", f"Channel deleted: {channel_id}")
    handler._send_json(200, {"ok": True, "channel": channel_id})
