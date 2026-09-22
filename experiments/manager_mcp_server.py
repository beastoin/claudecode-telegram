#!/usr/bin/env python3
"""Minimal MCP peer for claudecode-telegram workers."""

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8271").rstrip("/")
NAME = os.environ.get("MANAGER_NAME", "manager")
HOST = os.environ.get("MANAGER_HOST", socket.gethostname())
BIND = os.environ.get("MCP_BIND", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8787"))
PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", f"http://{HOST}:{PORT}").rstrip("/")
TIMEOUT = float(os.environ.get("MCP_TIMEOUT", "20"))
INBOX = []
LOCK = threading.Lock()

def http_json(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}

def register():
    http_json("POST", f"{BRIDGE_URL}/register", {
        "name": NAME,
        "host": HOST,
        "version": "manager-mcp-0.1",
        "protocol": "http",
        "callback_url": PUBLIC_URL,
        "tools": {"list_workers": True, "send": True, "inbox": True},
    })

class MsgHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        return

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"OK" if self.path == "/health" else b"Not found")

    def do_POST(self):
        if self.path != "/msg":
            self.send_error(404)
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(raw or b"{}")
            item = {"at": int(time.time()), "from": data.get("from", ""), "text": data.get("text", "")}
            with LOCK:
                INBOX.append(item)
                del INBOX[:-200]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as exc:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(str(exc).encode())

def start_http():
    ThreadingHTTPServer((BIND, PORT), MsgHandler).serve_forever()

def workers():
    query = urllib.parse.urlencode({"from": NAME})
    return http_json("GET", f"{BRIDGE_URL}/workers?{query}").get("workers", [])

def render_command(example, text):
    prefixed = f"{NAME}: {text}"
    old = shlex.quote(json.dumps({"from": "YOUR_NAME", "text": "your message here"})); new = shlex.quote(json.dumps({"from": NAME, "text": text}))
    cmd = example.replace(old, new).replace("'YOUR_NAME: your message here'", shlex.quote(prefixed))
    cmd = cmd.replace('"YOUR_NAME"', json.dumps(NAME))
    cmd = cmd.replace('"your message here"', json.dumps(text))
    return cmd

def tool_list_workers(_args):
    return {"bridge_url": BRIDGE_URL, "manager": NAME, "workers": workers()}


def tool_send(args):
    worker = str(args.get("worker", "")).strip()
    text = str(args.get("text", "")).strip()
    if not worker or not text:
        return {"ok": False, "error": "worker and text are required"}
    peer = next((w for w in workers() if w.get("name") == worker), None)
    if not peer or not peer.get("send_example"):
        return {"ok": False, "error": f"worker not found or not sendable: {worker}"}
    cmd = render_command(peer["send_example"], text)
    proc = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=TIMEOUT)
    return {"ok": proc.returncode == 0, "worker": worker, "cmd": cmd, "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:]}


def tool_inbox(args):
    limit = int(args.get("limit", 20))
    clear = bool(args.get("clear", False))
    with LOCK:
        items = INBOX[-limit:]
        if clear:
            INBOX.clear()
    return {"ok": True, "messages": items}


TOOLS = {
    "list_workers": ("List workers from bridge /workers?from=<manager>.", {}, tool_list_workers),
    "send": ("Send text to a worker using its /workers send_example.",
             {"worker": {"type": "string"}, "text": {"type": "string"}}, tool_send),
    "inbox": ("Read messages posted by workers to this MCP peer.",
              {"limit": {"type": "integer"}, "clear": {"type": "boolean"}}, tool_inbox),
}


def rpc_result(id_, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    threading.Thread(target=start_http, daemon=True).start()
    register()
    for line in sys.stdin:
        msg = json.loads(line)
        method, id_ = msg.get("method"), msg.get("id")
        if method == "initialize":
            rpc_result(id_, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {
                "name": "claudecode-telegram-manager", "version": "0.1"}})
        elif method == "tools/list":
            rpc_result(id_, {"tools": [{"name": n, "description": d, "inputSchema": {"type": "object",
                "properties": p}} for n, (d, p, _) in TOOLS.items()]})
        elif method == "tools/call":
            name = msg.get("params", {}).get("name")
            args = msg.get("params", {}).get("arguments", {})
            out = TOOLS[name][2](args) if name in TOOLS else {"ok": False, "error": "unknown tool"}
            rpc_result(id_, {"content": [{"type": "text", "text": json.dumps(out, indent=2)}]})

if __name__ == "__main__":
    main()
