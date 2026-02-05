#!/usr/bin/env python3
"""Codex adapter - uses codex exec --json with session resume."""

import json
import os
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def get_session_id_file(worker_name: str, sessions_dir: str) -> Path:
    """Get path to file storing codex session ID."""
    return Path(sessions_dir) / worker_name / "codex_session_id"


def get_session_lock_file(worker_name: str, sessions_dir: str) -> Path:
    """Get path to lock file for codex session ID."""
    return Path(sessions_dir) / worker_name / "codex_session_id.lock"


@contextmanager
def session_lock(lock_path: Path):
    """Serialize codex session access per worker."""
    if fcntl is None:
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_session_id(worker_name: str, sessions_dir: str) -> str:
    """Load saved codex session ID for worker."""
    f = get_session_id_file(worker_name, sessions_dir)
    if f.exists():
        return f.read_text().strip()
    return ""


def save_session_id(worker_name: str, sessions_dir: str, session_id: str):
    """Save codex session ID for worker."""
    f = get_session_id_file(worker_name, sessions_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(session_id)


def parse_jsonl_response(output: str) -> tuple[str, str]:
    """Parse JSONL output from codex exec.

    Returns (response_text, thread_id).

    Codex JSONL format:
    {"type":"thread.started","thread_id":"..."}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Hello!"}}
    {"type":"turn.completed","usage":{...}}
    """
    response_parts = []
    thread_id = ""

    for line in output.strip().split('\n'):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            event_type = event.get("type", "")

            # Capture thread ID from thread.started event
            if event_type == "thread.started":
                thread_id = event.get("thread_id", "")

            # Capture text from item.completed events
            elif event_type == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    text = item.get("text", "")
                    if text:
                        response_parts.append(text)

        except json.JSONDecodeError:
            continue

    return '\n'.join(response_parts).strip(), thread_id


def run_codex(message: str, session_id: str = "", workdir: str = "") -> tuple[str, str, int]:
    """Run codex exec and return (response, session_id, returncode)."""
    cmd = ["codex", "exec", "--json", "--yolo"]

    if workdir:
        cmd.extend(["-C", workdir])

    if session_id:
        # Resume existing session
        cmd.extend(["resume", session_id, message])
    else:
        # New session
        cmd.append(message)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
            # No timeout - let codex run as long as needed
        )
        response, new_session_id = parse_jsonl_response(result.stdout)
        if result.returncode != 0 and not response:
            stderr = (result.stderr or "").strip()
            response = stderr or "Codex exec failed."
        return response, new_session_id or session_id, result.returncode
    except Exception as e:
        return f"Error: {e}", session_id, 1


def send_to_bridge(session_name: str, text: str, bridge_url: str, extra: Optional[dict] = None) -> bool:
    """Send response to bridge (raw text, no escaping)."""
    try:
        payload = {"session": session_name, "text": text}
        if extra:
            payload.update(extra)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{bridge_url}/response",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception as e:
        print(f"Failed to send to bridge: {e}", file=sys.stderr)
        return False


def main() -> int:
    if len(sys.argv) < 4:
        print("Usage: codex-tmux-adapter.py <worker-name> <message> <bridge-url> [sessions-dir] [workdir]", file=sys.stderr)
        return 1

    worker_name = sys.argv[1]
    message = sys.argv[2]
    bridge_url = sys.argv[3]
    sessions_dir = sys.argv[4] if len(sys.argv) > 4 else os.environ.get("SESSIONS_DIR", str(Path.home() / ".claude/telegram/sessions"))
    workdir = sys.argv[5] if len(sys.argv) > 5 else ""

    lock_path = get_session_lock_file(worker_name, sessions_dir)
    with session_lock(lock_path):
        # Load existing session ID if any
        session_id = load_session_id(worker_name, sessions_dir)

        # Run codex
        response, new_session_id, returncode = run_codex(message, session_id, workdir)

        # Save session ID for next time
        if new_session_id:
            save_session_id(worker_name, sessions_dir, new_session_id)

        # Send response to bridge
        if response:
            send_to_bridge(worker_name, response, bridge_url, extra={"source": "codex", "escape": True})

    return returncode


if __name__ == "__main__":
    sys.exit(main())
