#!/usr/bin/env python3
"""Minimal MCP server for sending messages to claudecode-telegram workers."""

import json
import os
from typing import Any
from urllib import error, parse, request

from mcp.server.fastmcp import FastMCP


BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8271").rstrip("/")
TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "15"))

mcp = FastMCP("claudecode-telegram")


def _bridge_url(path: str) -> str:
    return f"{BRIDGE_URL}{path}"


def _http_json(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(_bridge_url(path), data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw}
        payload.setdefault("ok", False)
        payload.setdefault("status", exc.code)
        return payload
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def list_workers() -> dict[str, Any]:
    """List active bridge workers and their status."""
    data = _http_json("GET", "/workers")
    workers = data.get("workers", [])
    return {
        "ok": "workers" in data,
        "bridge_url": BRIDGE_URL,
        "workers": [
            {
                "name": worker.get("name"),
                "status": worker.get("status", "unknown"),
                "protocol": worker.get("protocol"),
                "machine": worker.get("machine"),
                "address": worker.get("address"),
            }
            for worker in workers
        ],
    }


@mcp.tool()
def send_message(worker: str, text: str) -> dict[str, Any]:
    """Send text to a worker's Claude Code tmux session."""
    worker = worker.strip()
    if not worker:
        return {"ok": False, "error": "worker is required"}
    if not text.strip():
        return {"ok": False, "error": "text is required"}
    return _http_json("POST", "/send", {"worker": worker, "message": text})


@mcp.tool()
def get_response(worker: str) -> dict[str, Any]:
    """Return the worker transcript URL; the bridge does not store pollable replies."""
    worker = worker.strip()
    if not worker:
        return {"ok": False, "error": "worker is required"}
    quoted = parse.quote(worker, safe="")
    return {
        "ok": False,
        "worker": worker,
        "error": "No response polling endpoint is available on the bridge.",
        "transcript_url": _bridge_url(f"/transcript/{quoted}"),
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
