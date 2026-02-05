#!/usr/bin/env python3
"""OpenCode CLI adapter - runs opencode run with JSON output.

Usage: opencode-adapter.py <worker-name> <message> <bridge-url> [sessions-dir]

Runs opencode CLI in non-interactive mode, parses JSON response, and POSTs to bridge.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import fcntl
except Exception:
    fcntl = None


def get_session_file(worker_name: str, sessions_dir: str, filename: str) -> Path:
    """Get path to session file."""
    return Path(sessions_dir) / worker_name / filename


def get_lock_file(worker_name: str, sessions_dir: str) -> Path:
    """Get path to lock file for session."""
    return Path(sessions_dir) / worker_name / "opencode.lock"


class SessionLock:
    """Serialize opencode session access per worker."""
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.lock_file = None

    def __enter__(self):
        if fcntl is None:
            return self
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.lock_path, "w")
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        if self.lock_file and fcntl:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()


def run_opencode(message: str) -> tuple[str, int]:
    """Run opencode CLI and return (output, returncode)."""
    cmd = ["opencode", "run", message, "--format", "json"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 min timeout
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "OpenCode request timed out.", 1
    except FileNotFoundError:
        return "OpenCode CLI not installed. Install from: https://opencode.ai", 1
    except Exception as e:
        return f"Error running opencode: {e}", 1


def extract_response(raw: str) -> str:
    """Extract response text from opencode JSON output.

    OpenCode JSON format (JSONL events):
    {"type": "message", "role": "assistant", "content": "..."}
    """
    if not raw.strip():
        return ""

    parts = []

    # Try parsing as JSONL (multiple JSON objects per line)
    for line in raw.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            # Collect assistant messages
            if event.get("type") == "message" and event.get("role") == "assistant":
                content = event.get("content", "")
                if content:
                    parts.append(content)
            # Also check for text/output fields
            elif "text" in event:
                parts.append(event["text"])
            elif "output" in event:
                parts.append(event["output"])
        except json.JSONDecodeError:
            continue

    if parts:
        return "\n".join(parts)

    # Fallback: try parsing as single JSON object
    try:
        data = json.loads(raw)
        return data.get("response", "") or data.get("text", "") or data.get("output", "")
    except json.JSONDecodeError:
        pass

    # Final fallback: return raw text
    return raw.strip()


def send_to_bridge(session_name: str, text: str, bridge_url: str) -> bool:
    """Send response to bridge."""
    try:
        payload = {
            "session": session_name,
            "text": text,
            "source": "opencode",
            "escape": True
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{bridge_url}/response",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"Failed to send to bridge: {e}", file=sys.stderr)
        return False


def main() -> int:
    if len(sys.argv) < 4:
        print("Usage: opencode-adapter.py <worker-name> <message> <bridge-url> [sessions-dir]",
              file=sys.stderr)
        return 1

    worker_name = sys.argv[1]
    message = sys.argv[2]
    bridge_url = sys.argv[3]
    sessions_dir = sys.argv[4] if len(sys.argv) > 4 else os.environ.get(
        "SESSIONS_DIR", str(Path.home() / ".claude/telegram/sessions"))

    lock_path = get_lock_file(worker_name, sessions_dir)
    with SessionLock(lock_path):
        # Run opencode
        output, returncode = run_opencode(message)

        # Extract response
        if returncode != 0 and not output.strip():
            response = "OpenCode request failed."
        else:
            response = extract_response(output)
            if not response:
                response = output.strip() or "No response from OpenCode."

        # Send to bridge
        if response:
            send_to_bridge(worker_name, response, bridge_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
