#!/usr/bin/env python3
"""Panic Relay — public channel server for external agents.

Serves channel-scoped endpoints only: /v1/<channel_id>/*
Channels are created by the bridge via /relay Telegram command (writes JSON files).
This server has NO admin API — it just serves whatever channels exist on disk.

Usage:
    python3 panic_relay.py
"""

import hashlib
import json
import os
import queue
import secrets
import sys
import threading
import time
import uuid
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

VERSION = "0.3.0"

PORT = int(os.environ.get("RELAY_PORT", "8787"))
BIND = os.environ.get("RELAY_BIND", "0.0.0.0")
BRIDGE_URL = os.environ.get("RELAY_BRIDGE_URL", "http://127.0.0.1:8271")
PUBLIC_HOST = os.environ.get("RELAY_PUBLIC_HOST", "157.180.48.254")
CHANNELS_DIR = Path(os.environ.get("RELAY_CHANNELS_DIR",
    Path.home() / ".local" / "share" / "claudecode-telegram" / "panic-relay" / "channels"))
LOG_DIR = Path(os.environ.get("RELAY_LOG_DIR",
    Path.home() / ".local" / "share" / "claudecode-telegram" / "panic-relay"))

MAX_MSG_SIZE = 16384
MAX_BODY_SIZE = 65536
HEARTBEAT_INTERVAL = 30
RATE_WINDOW = 60
RATE_SEND_MAX = 30
RATE_REPLY_MAX = 60

# --- In-memory state ---
channels = {}           # channel_id -> channel dict (loaded from disk)
sse_subscribers = {}    # channel_id -> [queue.Queue]
rate_counters = {}      # key -> (count, window_start)
messages = {}           # channel_id -> [msg dict]
state_lock = threading.Lock()
ledger_file = None


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _rate_check(key, limit):
    now = time.time()
    with state_lock:
        count, start = rate_counters.get(key, (0, now))
        if now - start > RATE_WINDOW:
            rate_counters[key] = (1, now)
            return True
        if count >= limit:
            return False
        rate_counters[key] = (count + 1, start)
        return True

def _log(event):
    if ledger_file:
        try:
            event["ts"] = _now_iso()
            ledger_file.write(json.dumps(event) + "\n")
            ledger_file.flush()
        except Exception:
            pass


# --- Channel loading from disk ---

def _load_channel(path):
    try:
        data = json.loads(path.read_text())
        cid = data["channel_id"]
        with state_lock:
            channels[cid] = data
            if cid not in messages:
                messages[cid] = []
            if cid not in sse_subscribers:
                sse_subscribers[cid] = []
        return cid
    except Exception as e:
        print(f"[relay] failed to load {path}: {e}")
        return None

def _scan_channels():
    if not CHANNELS_DIR.exists():
        return
    loaded = set()
    for f in CHANNELS_DIR.glob("*.json"):
        cid = _load_channel(f)
        if cid:
            loaded.add(cid)
    # Remove channels whose files were deleted
    with state_lock:
        gone = set(channels.keys()) - loaded
        for cid in gone:
            channels.pop(cid, None)
            messages.pop(cid, None)
            sse_subscribers.pop(cid, None)

def _cleanup_expired():
    now = time.time()
    with state_lock:
        expired = [cid for cid, ch in channels.items()
                   if ch.get("expires_at_unix", 0) < now]
    for cid in expired:
        fpath = CHANNELS_DIR / f"{cid}.json"
        fpath.unlink(missing_ok=True)
        with state_lock:
            channels.pop(cid, None)
            messages.pop(cid, None)
            sse_subscribers.pop(cid, None)
        _log({"type": "channel_expired", "channel_id": cid})
        print(f"[relay] expired: {cid}")

def _scan_loop():
    while True:
        time.sleep(5)
        _scan_channels()
        _cleanup_expired()


# --- Auth ---

def _auth_guest(channel_id, token):
    ch = channels.get(channel_id)
    if not ch:
        return None
    if time.time() > ch.get("expires_at_unix", 0):
        return None
    if _hash(token) != ch.get("guest_token_hash"):
        return None
    return ch

def _auth_reply(channel_id, token):
    ch = channels.get(channel_id)
    if not ch:
        return None
    if time.time() > ch.get("expires_at_unix", 0):
        return None
    if _hash(token) != ch.get("reply_token_hash"):
        return None
    return ch


# --- Operations ---

def _bridge_send(worker, message, from_label):
    payload = json.dumps({
        "worker": worker,
        "message": message,
        "from": f"relay:{from_label}",
    }).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/send",
        data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception as e:
        print(f"[relay] bridge send error: {e}")
        return False

def _sse_push(channel_id, event_type, data):
    with state_lock:
        subs = sse_subscribers.get(channel_id, [])
        dead = []
        for q in subs:
            try:
                q.put_nowait((event_type, data))
            except queue.Full:
                dead.append(q)
        for q in dead:
            subs.remove(q)

def guest_send(channel_id, text):
    ch = channels.get(channel_id)
    if not ch:
        return {"error": "channel not found"}
    if len(text) > MAX_MSG_SIZE:
        return {"error": f"message too large (max {MAX_MSG_SIZE} bytes)"}

    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    worker = ch["worker"]
    base = f"http://{PUBLIC_HOST}:{PORT}/v1/{channel_id}"

    reply_token = ch['reply_token']
    envelope = (
        f"[RELAY from {ch['label']}]\n"
        f"channel: {channel_id}\n"
        f"message_id: {msg_id}\n"
        f"reply: curl -fsS {base}/reply "
        f"-H 'Authorization: Bearer {reply_token}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"text\":\"YOUR_REPLY\"}}'\n"
        f"\n"
        f"{text}\n"
        f"[/RELAY]"
    )

    ok = _bridge_send(worker, envelope, ch["label"])

    msg = {
        "message_id": msg_id,
        "direction": "guest_to_worker",
        "from": ch["label"],
        "to": worker,
        "text": text,
        "delivered": ok,
        "ts": _now_iso(),
    }
    with state_lock:
        messages.setdefault(channel_id, []).append(msg)

    _log({"type": "send", "channel_id": channel_id, "message_id": msg_id, "delivered": ok})
    return {"message_id": msg_id, "delivered": ok, "worker": worker}

def do_reply(channel_id, text):
    ch = channels.get(channel_id)
    if not ch:
        return {"error": "channel not found"}

    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    msg = {
        "message_id": msg_id,
        "direction": "worker_to_guest",
        "from": ch["worker"],
        "to": ch["label"],
        "text": text,
        "ts": _now_iso(),
    }
    with state_lock:
        messages.setdefault(channel_id, []).append(msg)

    _sse_push(channel_id, "message", {
        "message_id": msg_id,
        "from": ch["worker"],
        "text": text,
        "ts": msg["ts"],
    })

    _log({"type": "reply", "channel_id": channel_id, "message_id": msg_id})
    return {"message_id": msg_id, "delivered": True}


# --- Guide & Members ---

def _guide(ch, token):
    base = f"http://{PUBLIC_HOST}:{PORT}/v1/{ch['channel_id']}"
    w = ch["worker"]
    return f"""# Chat Channel to {w}

Direct chat channel to **{w}** (a Claude Code agent).

## Setup

```bash
export RELAY_TOKEN="{token}"
export RELAY="{base}"
```

## Step 1: Start the listener

Run this in the background FIRST. It polls for new messages every 3 seconds and auto-exits when the channel expires:

```bash
python3 -c "
import json,time,urllib.request as u,sys
TOKEN,URL='{token}','{base}'
last=''
print('[listener] connected — waiting for messages',flush=True)
while True:
    try:
        q=URL+'/messages'+('?after='+last if last else '')
        req=u.Request(q,headers={{'Authorization':'Bearer '+TOKEN}})
        d=json.loads(u.urlopen(req,timeout=10).read())
        for m in d.get('messages',[]):
            print(m.get('from','?')+': '+m['text'],flush=True)
            last=m['message_id']
    except Exception as e:
        if '403' in str(e) or '404' in str(e):
            print('[listener] channel expired or invalid — closing',flush=True)
            break
    time.sleep(3)
"
```

## Step 2: Send messages

```bash
curl -fsS $RELAY/send -H "Authorization: Bearer $RELAY_TOKEN" -H "Content-Type: application/json" -d '{{"text":"your message here"}}'
```

Replies appear automatically in the listener output. No polling needed after sending.

## How to use

1. Start the listener in the background (Step 1)
2. Send a message (Step 2)
3. Watch the listener output for the reply — it arrives automatically
4. Send more messages as needed
5. The listener auto-exits when the channel expires (`{ch['expires_at']}`)

## Limits

- Max message: 16 KiB
- Rate: 30 msg/min
- Scoped to **{w}** only
"""

def _members(ch, token):
    base = f"http://{PUBLIC_HOST}:{PORT}/v1/{ch['channel_id']}"
    rt = ch["reply_token"]
    return {
        "channel_id": ch["channel_id"],
        "members": [
            {
                "name": ch["label"],
                "role": "guest",
                "send_example": (
                    f"curl -fsS {base}/send "
                    f"-H 'Authorization: Bearer {token}' "
                    f"-H 'Content-Type: application/json' "
                    f"-d '{{\"text\":\"your message\"}}'"
                ),
            },
            {
                "name": ch["worker"],
                "role": "worker",
                "reply_example": (
                    f"curl -fsS {base}/reply "
                    f"-H 'Authorization: Bearer {rt}' "
                    f"-H 'Content-Type: application/json' "
                    f"-d '{{\"text\":\"your reply\"}}'"
                ),
            },
        ],
        "events_example": (
            f"curl -fsS -N {base}/events "
            f"-H 'Authorization: Bearer {token}' "
            f"-H 'Accept: text/event-stream'"
        ),
    }


# --- HTTP Handler ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"PanicRelay/{VERSION}"

    def log_message(self, fmt, *args):
        print(f"[relay] {self.client_address[0]} - {fmt % args}")

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, text, ct="text/plain"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ct}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY_SIZE:
            return None
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        if "?" in self.path:
            for part in self.path.split("?")[1].split("&"):
                if part.startswith("token="):
                    return part[6:]
        return ""

    def _parse(self):
        """Returns (channel_id, action) from /v1/<channel_id>[/<action>]."""
        raw = self.path.split("?")[0]
        parts = [p for p in raw.split("/") if p]
        if len(parts) >= 2 and parts[0] == "v1":
            return parts[1], (parts[2] if len(parts) >= 3 else None)
        return None, None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def do_GET(self):
        channel_id, action = self._parse()
        if not channel_id:
            self._json(404, {"error": "not found"})
            return

        token = self._token()
        ch = _auth_guest(channel_id, token)
        if not ch:
            self._json(403, {"error": "invalid or expired channel/token"})
            return

        # GET /v1/<channel_id> — guide
        if action is None:
            self._text(200, _guide(ch, token), "text/markdown")

        # GET /v1/<channel_id>/members
        elif action == "members":
            self._json(200, _members(ch, token))

        # GET /v1/<channel_id>/status
        elif action == "status":
            self._json(200, {
                "channel_id": channel_id,
                "worker": ch["worker"],
                "label": ch["label"],
                "expires_at": ch["expires_at"],
                "message_count": len(messages.get(channel_id, [])),
            })

        # GET /v1/<channel_id>/messages[?after=msg_id]
        elif action == "messages":
            after = None
            if "?" in self.path:
                for part in self.path.split("?")[1].split("&"):
                    if part.startswith("after="):
                        after = part[6:]
            all_msgs = messages.get(channel_id, [])
            if after:
                found = False
                filtered = []
                for m in all_msgs:
                    if found:
                        filtered.append(m)
                    elif m.get("message_id") == after:
                        found = True
                result = filtered
            else:
                result = all_msgs
            self._json(200, {
                "channel_id": channel_id,
                "messages": result,
            })

        # GET /v1/<channel_id>/events — SSE
        elif action == "events":
            self._sse(ch)

        else:
            self._json(404, {"error": f"unknown: {action}"})

    def do_POST(self):
        channel_id, action = self._parse()
        if not channel_id:
            self._json(404, {"error": "not found"})
            return

        # POST /v1/<channel_id>/send — guest sends
        if action == "send":
            token = self._token()
            ch = _auth_guest(channel_id, token)
            if not ch:
                self._json(403, {"error": "invalid or expired channel/token"})
                return
            if not _rate_check(f"send:{channel_id}", RATE_SEND_MAX):
                self._json(429, {"error": "rate limit exceeded"})
                return
            body = self._body()
            if body is None:
                self._json(413, {"error": "body too large"})
                return
            text = body.get("text", "")
            if not text:
                self._json(400, {"error": "text is required"})
                return
            result = guest_send(channel_id, text)
            code = 200 if "error" not in result else 400
            self._json(code, result)

        # POST /v1/<channel_id>/reply — worker replies
        elif action == "reply":
            token = self._token()
            ch = _auth_reply(channel_id, token)
            if not ch:
                self._json(403, {"error": "invalid reply token"})
                return
            if not _rate_check(f"reply:{channel_id}", RATE_REPLY_MAX):
                self._json(429, {"error": "rate limit exceeded"})
                return
            body = self._body()
            if body is None:
                self._json(413, {"error": "body too large"})
                return
            text = body.get("text", "")
            if not text:
                self._json(400, {"error": "text is required"})
                return
            result = do_reply(channel_id, text)
            code = 200 if "error" not in result else 400
            self._json(code, result)

        else:
            self._json(404, {"error": f"unknown: {action}"})

    def _sse(self, ch):
        channel_id = ch["channel_id"]
        q = queue.Queue(maxsize=100)
        with state_lock:
            sse_subscribers.setdefault(channel_id, []).append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(f"event: connected\ndata: {{\"channel_id\":\"{channel_id}\",\"worker\":\"{ch['worker']}\"}}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    et, data = q.get(timeout=HEARTBEAT_INTERVAL)
                    self.wfile.write(f"event: {et}\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() > ch.get("expires_at_unix", 0):
                        self.wfile.write(b"event: expired\ndata: {}\n\n")
                        self.wfile.flush()
                        break
                    self.wfile.write(f"event: heartbeat\ndata: {{\"ts\":\"{_now_iso()}\"}}\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with state_lock:
                subs = sse_subscribers.get(channel_id, [])
                if q in subs:
                    subs.remove(q)


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


def main():
    global ledger_file

    CHANNELS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    lpath = LOG_DIR / "events.jsonl"
    ledger_file = open(lpath, "a")
    lpath.chmod(0o600)

    _scan_channels()
    threading.Thread(target=_scan_loop, daemon=True).start()

    server = Server((BIND, PORT), Handler)
    print(f"[relay] Panic Relay v{VERSION}")
    print(f"[relay] {BIND}:{PORT}")
    print(f"[relay] Bridge: {BRIDGE_URL}")
    print(f"[relay] Channels: {CHANNELS_DIR} ({len(channels)} loaded)")

    _log({"type": "started", "version": VERSION})

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[relay] stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
