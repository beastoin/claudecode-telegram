#!/usr/bin/env python3
"""Gemini CLI adapter - runs gemini -p with JSON output.

Usage: gemini-adapter.py <worker-name> <message> <bridge-url> [sessions-dir]

Runs gemini CLI in headless mode, parses JSON response, and POSTs to bridge.
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
    return Path(sessions_dir) / worker_name / "gemini.lock"


class SessionLock:
    """Serialize gemini session access per worker."""
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


def run_gemini(message: str) -> tuple[str, int]:
    """Run gemini CLI and return (output, returncode)."""
    cmd = ["gemini", "-p", message, "--output-format", "json"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 min timeout
        )
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        return "Gemini request timed out.", 1
    except FileNotFoundError:
        return "Gemini CLI not installed. Install with: npm install -g @google/gemini-cli", 1
    except Exception as e:
        return f"Error running gemini: {e}", 1


def extract_response(raw: str) -> str:
    """Extract response text from gemini JSON output.

    Gemini JSON format:
    {
      "response": "...",
      "stats": {...}
    }
    """
    if not raw.strip():
        return ""

    try:
        data = json.loads(raw)
        # Primary: response field
        if "response" in data:
            return data["response"]
        # Fallback: text or output field
        return data.get("text", "") or data.get("output", "")
    except json.JSONDecodeError:
        # If not JSON, return raw text
        return raw.strip()


def send_to_bridge(session_name: str, text: str, bridge_url: str) -> bool:
    """Send response to bridge."""
    try:
        payload = {
            "session": session_name,
            "text": text,
            "source": "gemini",
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
        print("Usage: gemini-adapter.py <worker-name> <message> <bridge-url> [sessions-dir]",
              file=sys.stderr)
        return 1

    worker_name = sys.argv[1]
    message = sys.argv[2]
    bridge_url = sys.argv[3]
    sessions_dir = sys.argv[4] if len(sys.argv) > 4 else os.environ.get(
        "SESSIONS_DIR", str(Path.home() / ".claude/telegram/sessions"))

    lock_path = get_lock_file(worker_name, sessions_dir)
    with SessionLock(lock_path):
        # Run gemini
        output, returncode = run_gemini(message)

        # Extract response
        if returncode != 0 and not output.strip():
            response = "Gemini request failed."
        else:
            response = extract_response(output)
            if not response:
                response = output.strip() or "No response from Gemini."

        # Send to bridge
        if response:
            send_to_bridge(worker_name, response, bridge_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
