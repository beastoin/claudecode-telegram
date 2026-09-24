#!/usr/bin/env python3
"""Codex adapter — non-interactive mode via codex exec --json with session resume.

Blocking: each call blocks until codex finishes. Bridge spawns via Popen so it
doesn't block. Prompts piped via stdin ('-') to handle long messages and avoid
CLI flag parsing issues with messages starting with '-'.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import TypedDict

try:
    import fcntl  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────


class CodexEvent(TypedDict, total=False):
    """One line of codex exec --json JSONL output.

    Relevant event types:
        thread.started  → thread_id
        item.completed  → item.type == "agent_message", item.text
        turn.completed  → usage (ignored)
    """

    type: str
    thread_id: str
    item: CodexItem


class CodexItem(TypedDict, total=False):
    """Item payload inside an item.completed event."""

    id: str
    type: str
    text: str


class BridgePayload(TypedDict, total=False):
    """JSON body sent to POST /response."""

    session: str       # required
    text: str          # required
    session_id: str
    source: str        # "codex"
    escape: bool       # True — bridge should not parse media tags


# ─────────────────────────────────────────────────────────────────────────────
# Signal handling
# ─────────────────────────────────────────────────────────────────────────────


def _handle_sigterm(_signum: int, _frame: FrameType | None) -> None:
    """Exit cleanly on SIGTERM (bridge /pause or /end)."""
    sys.exit(130)


signal.signal(signal.SIGTERM, _handle_sigterm)


# ─────────────────────────────────────────────────────────────────────────────
# Session file helpers
# ─────────────────────────────────────────────────────────────────────────────


def _session_id_path(worker_name: str, sessions_dir: str) -> Path:
    """Path to the file storing codex session ID."""
    return Path(sessions_dir) / worker_name / "codex_session_id"


def _session_lock_path(worker_name: str, sessions_dir: str) -> Path:
    """Path to lock file for codex session ID."""
    return Path(sessions_dir) / worker_name / "codex_session_id.lock"


@contextmanager
def session_lock(lock_path: Path) -> Generator[None, None, None]:
    """Serialize codex session access per worker via flock."""
    if fcntl is None:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd: int = -1
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def load_session_id(worker_name: str, sessions_dir: str) -> str:
    """Load saved codex session ID for worker. Returns '' if none."""
    p: Path = _session_id_path(worker_name, sessions_dir)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def save_session_id(worker_name: str, sessions_dir: str, session_id: str) -> None:
    """Atomically save codex session ID for worker (tmp + rename)."""
    target: Path = _session_id_path(worker_name, sessions_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd: int = -1
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".session_id_")
        os.write(fd, session_id.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(target))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Field extraction — safe access to untyped JSON from codex output
# ─────────────────────────────────────────────────────────────────────────────


def _str_field(d: dict[str, object], key: str, default: str = "") -> str:
    """Extract a string field from a dict, returning default if missing or wrong type."""
    val: object = d.get(key, default)
    return val if isinstance(val, str) else default


def _dict_field(d: dict[str, object], key: str) -> dict[str, object]:
    """Extract a dict field, returning empty dict if missing or wrong type."""
    val: object = d.get(key)
    return val if isinstance(val, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# JSONL parsing
# ─────────────────────────────────────────────────────────────────────────────


def parse_jsonl_response(output: str) -> tuple[str, str]:
    """Parse JSONL output from codex exec --json.

    Returns (response_text, thread_id).

    Codex JSONL format::

        {"type":"thread.started","thread_id":"..."}
        {"type":"turn.started"}
        {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hello!"}}
        {"type":"turn.completed","usage":{...}}
    """
    response_parts: list[str] = []
    thread_id: str = ""

    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event: dict[str, object] = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type: str = _str_field(event, "type")

        if event_type == "thread.started":
            thread_id = _str_field(event, "thread_id")
        elif event_type == "item.completed":
            item: dict[str, object] = _dict_field(event, "item")
            if _str_field(item, "type") == "agent_message":
                text: str = _str_field(item, "text")
                if text:
                    response_parts.append(text)

    return "\n".join(response_parts).strip(), thread_id


# ─────────────────────────────────────────────────────────────────────────────
# Codex execution
# ─────────────────────────────────────────────────────────────────────────────


def run_codex(message: str, session_id: str = "", workdir: str = "") -> tuple[str, str, int]:
    """Run codex exec and return (response, session_id, returncode)."""
    cmd: list[str] = ["codex", "exec", "--json", "--yolo"]

    if workdir:
        cmd.extend(["-C", workdir])

    if session_id and not session_id.startswith("-"):
        # Resume existing session; read prompt from stdin via '-'
        cmd.extend(["resume", session_id, "-"])
    else:
        # New session; read prompt from stdin via '-'
        cmd.append("-")

    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            cmd,
            input=message,
            capture_output=True,
            text=True,
            # No timeout — let codex run as long as needed
        )
        response: str
        new_session_id: str
        response, new_session_id = parse_jsonl_response(result.stdout)
        if result.returncode != 0 and not response:
            stderr: str = (result.stderr or "").strip()
            response = stderr or "Codex exec failed."
        return response, new_session_id or session_id, result.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error: {e}", session_id, 1


# ─────────────────────────────────────────────────────────────────────────────
# Bridge communication
# ─────────────────────────────────────────────────────────────────────────────


def send_to_bridge(
    session_name: str,
    text: str,
    bridge_url: str,
    extra: dict[str, str | bool] | None = None,
) -> bool:
    """Send response to bridge (raw text, no escaping)."""
    try:
        payload: dict[str, str | bool] = {"session": session_name, "text": text}
        if extra:
            payload.update(extra)
        data: bytes = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{bridge_url}/response",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError) as e:
        print(f"Failed to send to bridge: {e}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    """Run codex for a worker message and forward the response to bridge."""
    if len(sys.argv) < 4:
        print(
            "Usage: codex-tmux-adapter.py <worker-name> <message> <bridge-url>"
            " [sessions-dir] [workdir]",
            file=sys.stderr,
        )
        return 1

    worker_name: str = sys.argv[1]
    message: str = sys.argv[2]
    bridge_url: str = sys.argv[3]
    sessions_dir: str = (
        sys.argv[4]
        if len(sys.argv) > 4
        else os.environ.get("SESSIONS_DIR", str(Path.home() / ".claude/telegram/sessions"))
    )
    workdir: str = sys.argv[5] if len(sys.argv) > 5 else ""

    lock_path: Path = _session_lock_path(worker_name, sessions_dir)
    with session_lock(lock_path):
        session_id: str = load_session_id(worker_name, sessions_dir)

        response: str
        new_session_id: str
        returncode: int
        response, new_session_id, returncode = run_codex(message, session_id, workdir)

        if new_session_id:
            save_session_id(worker_name, sessions_dir, new_session_id)

        if response:
            send_to_bridge(
                worker_name,
                response,
                bridge_url,
                extra={"source": "codex", "escape": True},
            )

    return returncode


if __name__ == "__main__":
    sys.exit(main())
