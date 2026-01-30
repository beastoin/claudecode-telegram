#!/usr/bin/env python3
"""
claudecode-imessage bridge.

Local HTTP server that:
- Receives Claude stop hook responses and sends via iMessage
- Watches chat.db for incoming messages and routes to tmux sessions
- Manages tmux sessions (create, kill, route)
"""

import os
import sys
import json
import time
import signal
import logging
import threading
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from dataclasses import dataclass

# Add parent to path for imessage module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from imessage import (
    Message,
    MessageReceiver,
    get_allowed_handles,
    send_chunked,
    check_db_permissions,
    check_sender_permissions,
)

# Version
VERSION = "0.1.0"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration from environment
PORT = int(os.environ.get('IMESSAGE_BRIDGE_PORT', '8083'))
SESSIONS_DIR = os.path.expanduser(
    os.environ.get('SESSIONS_DIR', '~/.claude/imessage/sessions')
)
TMUX_PREFIX = os.environ.get('TMUX_PREFIX', 'claude-imessage-')
POLL_INTERVAL = float(os.environ.get('IMESSAGE_POLL_INTERVAL', '2'))
AUTO_LEARN_ADMIN = os.environ.get('IMESSAGE_AUTO_LEARN_ADMIN', '0') == '1'

# RAM state
state = {
    "active": None,  # Currently focused worker name
    "admin_handle": None,  # Admin's iMessage handle (auto-learned or set)
    "startup_notified": False,
}

# Per-session locks to prevent tmux send interleaving
session_locks: dict[str, threading.Lock] = {}


@dataclass
class Session:
    """Worker session info."""
    name: str
    handle: str
    last_rowid: int = 0


def get_session_dir(name: str) -> Path:
    """Get session directory path."""
    return Path(SESSIONS_DIR) / name


def ensure_dirs():
    """Create necessary directories."""
    Path(SESSIONS_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)


def get_session_lock(name: str) -> threading.Lock:
    """Get or create a lock for a session."""
    if name not in session_locks:
        session_locks[name] = threading.Lock()
    return session_locks[name]


# --- Session Management ---

def scan_tmux_sessions() -> list[str]:
    """Find all tmux sessions with our prefix."""
    try:
        result = subprocess.run(
            ['tmux', 'list-sessions', '-F', '#{session_name}'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return []

        sessions = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith(TMUX_PREFIX):
                name = line[len(TMUX_PREFIX):]
                sessions.append(name)
        return sessions
    except Exception as e:
        logger.error(f"Error scanning tmux sessions: {e}")
        return []


def session_exists(name: str) -> bool:
    """Check if a tmux session exists."""
    return name in scan_tmux_sessions()


def create_session(name: str, handle: str) -> tuple[bool, str]:
    """
    Create a new worker session.

    Args:
        name: Worker name
        handle: iMessage handle for this worker

    Returns:
        (success, message) tuple
    """
    if session_exists(name):
        return False, f"Session '{name}' already exists"

    session_name = f"{TMUX_PREFIX}{name}"
    session_dir = get_session_dir(name)

    # Create session directory
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Save handle
    (session_dir / 'chat_handle').write_text(handle)

    # Create tmux session
    try:
        # Set environment variables for the session
        env_exports = (
            f"export PORT={PORT} "
            f"TMUX_PREFIX='{TMUX_PREFIX}' "
            f"SESSIONS_DIR='{SESSIONS_DIR}'"
        )

        subprocess.run(
            ['tmux', 'new-session', '-d', '-s', session_name],
            check=True,
            timeout=10
        )

        # Send environment setup
        subprocess.run(
            ['tmux', 'send-keys', '-t', session_name, env_exports, 'Enter'],
            check=True,
            timeout=5
        )
        time.sleep(0.5)

        # Start Claude Code
        subprocess.run(
            ['tmux', 'send-keys', '-t', session_name,
             'claude --dangerously-skip-permissions', 'Enter'],
            check=True,
            timeout=5
        )

        logger.info(f"Created session '{name}' for handle {handle}")
        return True, f"Created worker '{name}'"

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to create session: {e}")
        return False, f"Failed to create session: {e}"
    except Exception as e:
        logger.error(f"Error creating session: {e}")
        return False, str(e)


def kill_session(name: str) -> tuple[bool, str]:
    """Kill a worker session."""
    if not session_exists(name):
        return False, f"Session '{name}' not found"

    session_name = f"{TMUX_PREFIX}{name}"

    try:
        subprocess.run(
            ['tmux', 'kill-session', '-t', session_name],
            check=True,
            timeout=10
        )

        # Clean up session directory
        session_dir = get_session_dir(name)
        if session_dir.exists():
            import shutil
            shutil.rmtree(session_dir)

        # Remove lock
        if name in session_locks:
            del session_locks[name]

        logger.info(f"Killed session '{name}'")
        return True, f"Removed worker '{name}'"

    except Exception as e:
        logger.error(f"Error killing session: {e}")
        return False, str(e)


def tmux_send_message(name: str, text: str) -> bool:
    """
    Send a message to a tmux session.

    Uses per-session locking to prevent interleaving.
    """
    if not session_exists(name):
        return False

    session_name = f"{TMUX_PREFIX}{name}"
    lock = get_session_lock(name)

    with lock:
        try:
            # Send text (literal mode to preserve special chars)
            subprocess.run(
                ['tmux', 'send-keys', '-t', session_name, '-l', text],
                check=True,
                timeout=10
            )
            time.sleep(0.1)

            # Send Enter
            subprocess.run(
                ['tmux', 'send-keys', '-t', session_name, 'Enter'],
                check=True,
                timeout=5
            )

            return True

        except Exception as e:
            logger.error(f"Error sending to tmux: {e}")
            return False


def set_pending(name: str, handle: str):
    """Mark a session as having a pending request."""
    session_dir = get_session_dir(name)
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    (session_dir / 'pending').write_text(str(int(time.time())))
    (session_dir / 'chat_handle').write_text(handle)


def clear_pending(name: str):
    """Clear pending flag for a session."""
    pending_file = get_session_dir(name) / 'pending'
    if pending_file.exists():
        pending_file.unlink()


def get_session_handle(name: str) -> Optional[str]:
    """Get the iMessage handle for a session."""
    handle_file = get_session_dir(name) / 'chat_handle'
    if handle_file.exists():
        return handle_file.read_text().strip()
    return None


def is_session_busy(name: str, timeout_minutes: int = 10) -> bool:
    """Check if a session has a pending request."""
    pending_file = get_session_dir(name) / 'pending'
    if not pending_file.exists():
        return False

    try:
        timestamp = int(pending_file.read_text().strip())
        age_minutes = (time.time() - timestamp) / 60
        if age_minutes > timeout_minutes:
            # Auto-clear stale pending
            clear_pending(name)
            return False
        return True
    except Exception:
        return False


# --- Message Routing ---

def route_message(name: str, text: str, handle: str) -> bool:
    """Route a message to a specific worker."""
    if not session_exists(name):
        return False

    set_pending(name, handle)

    if tmux_send_message(name, text):
        logger.info(f"Routed message to '{name}'")
        return True
    else:
        clear_pending(name)
        return False


def route_to_active(text: str, handle: str) -> tuple[bool, str]:
    """Route a message to the active worker."""
    if not state["active"]:
        return False, "No active worker. Use /focus <name> first."

    if not session_exists(state["active"]):
        state["active"] = None
        return False, "Active worker no longer exists."

    if route_message(state["active"], text, handle):
        return True, ""
    else:
        return False, f"Failed to send to '{state['active']}'"


# --- Command Handlers ---

def cmd_hire(args: str, handle: str) -> str:
    """Create a new worker."""
    name = args.strip()
    if not name:
        return "Usage: /hire <name>"

    if not name.isalnum():
        return "Worker name must be alphanumeric"

    success, msg = create_session(name, handle)
    if success:
        state["active"] = name
        return f"{msg}\nFocused on '{name}'"
    return msg


def cmd_focus(args: str, handle: str) -> str:
    """Set active worker."""
    name = args.strip()
    if not name:
        sessions = scan_tmux_sessions()
        if not sessions:
            return "No workers available"
        return f"Available: {', '.join(sessions)}\nUsage: /focus <name>"

    if not session_exists(name):
        return f"Worker '{name}' not found"

    state["active"] = name
    return f"Focused on '{name}'"


def cmd_team(args: str, handle: str) -> str:
    """List all workers."""
    sessions = scan_tmux_sessions()
    if not sessions:
        return "No workers. Use /hire <name> to create one."

    lines = ["Workers:"]
    for name in sessions:
        status = "busy" if is_session_busy(name) else "idle"
        active = " *" if name == state["active"] else ""
        lines.append(f"  {name}{active} ({status})")

    return '\n'.join(lines)


def cmd_progress(args: str, handle: str) -> str:
    """Check active worker status."""
    if not state["active"]:
        return "No active worker"

    name = state["active"]
    if not session_exists(name):
        state["active"] = None
        return "Active worker no longer exists"

    if is_session_busy(name):
        return f"'{name}' is working..."
    return f"'{name}' is idle"


def cmd_end(args: str, handle: str) -> str:
    """Remove a worker."""
    name = args.strip()
    if not name:
        return "Usage: /end <name>"

    success, msg = kill_session(name)
    if success and state["active"] == name:
        state["active"] = None
    return msg


def cmd_pause(args: str, handle: str) -> str:
    """Send Escape to active worker."""
    if not state["active"]:
        return "No active worker"

    session_name = f"{TMUX_PREFIX}{state['active']}"
    try:
        subprocess.run(
            ['tmux', 'send-keys', '-t', session_name, 'Escape'],
            check=True,
            timeout=5
        )
        return f"Sent Escape to '{state['active']}'"
    except Exception as e:
        return f"Failed: {e}"


def cmd_settings(args: str, handle: str) -> str:
    """Show current settings."""
    return (
        f"Version: {VERSION}\n"
        f"Port: {PORT}\n"
        f"Sessions: {SESSIONS_DIR}\n"
        f"Prefix: {TMUX_PREFIX}\n"
        f"Active: {state['active'] or '(none)'}\n"
        f"Admin: {state['admin_handle'] or '(auto-learn)'}"
    )


COMMANDS = {
    '/hire': cmd_hire,
    '/new': cmd_hire,
    '/focus': cmd_focus,
    '/use': cmd_focus,
    '/team': cmd_team,
    '/list': cmd_team,
    '/progress': cmd_progress,
    '/status': cmd_progress,
    '/end': cmd_end,
    '/kill': cmd_end,
    '/pause': cmd_pause,
    '/stop': cmd_pause,
    '/settings': cmd_settings,
    '/system': cmd_settings,
}


def handle_command(text: str, handle: str) -> str:
    """Handle a /command."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ''

    if cmd in COMMANDS:
        return COMMANDS[cmd](args, handle)

    return f"Unknown command: {cmd}"


def handle_incoming_message(msg: Message):
    """Handle an incoming iMessage."""
    text = msg.text.strip()
    handle = msg.handle

    logger.info(f"Incoming from {handle}: {text[:50]}...")

    # Auto-learn admin
    if AUTO_LEARN_ADMIN and not state["admin_handle"]:
        state["admin_handle"] = handle
        logger.info(f"Auto-learned admin: {handle}")

    # Admin check (if admin is set)
    if state["admin_handle"] and handle != state["admin_handle"]:
        logger.warning(f"Rejected message from non-admin: {handle}")
        return  # Silent rejection

    # Handle commands
    if text.startswith('/'):
        response = handle_command(text, handle)
        send_response(handle, response)
        return

    # Handle @mention
    if text.startswith('@'):
        parts = text.split(maxsplit=1)
        target = parts[0][1:]  # Remove @
        message = parts[1] if len(parts) > 1 else ''

        if target == 'all':
            # Broadcast to all
            sessions = scan_tmux_sessions()
            for name in sessions:
                route_message(name, message, handle)
            send_response(handle, f"Sent to {len(sessions)} workers")
        elif session_exists(target):
            if route_message(target, message, handle):
                pass  # Response will come from hook
            else:
                send_response(handle, f"Failed to send to '{target}'")
        else:
            send_response(handle, f"Worker '{target}' not found")
        return

    # Route to active worker
    success, error = route_to_active(text, handle)
    if not success:
        send_response(handle, error)


def send_response(handle: str, text: str):
    """Send a response message."""
    success, msg = send_chunked(handle, text)
    if not success:
        logger.error(f"Failed to send response: {msg}")


# --- HTTP Server ---

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server."""
    allow_reuse_address = True


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'version': VERSION}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handle POST requests."""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        if self.path == '/response':
            self._handle_response(body)
        elif self.path == '/notify':
            self._handle_notify(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_response(self, body: str):
        """Handle response from Claude hook."""
        try:
            data = json.loads(body)
            session_name = data.get('session')
            text = data.get('text', '')

            if not session_name:
                self.send_response(400)
                self.end_headers()
                return

            # Get handle for this session
            handle = get_session_handle(session_name)
            if not handle:
                logger.warning(f"No handle for session '{session_name}'")
                self.send_response(404)
                self.end_headers()
                return

            # Clear pending
            clear_pending(session_name)

            # Send response
            if text:
                prefix = f"{session_name}: "
                send_response(handle, prefix + text)

            self.send_response(200)
            self.end_headers()

        except Exception as e:
            logger.error(f"Error handling response: {e}")
            self.send_response(500)
            self.end_headers()

    def _handle_notify(self, body: str):
        """Handle admin notification."""
        try:
            data = json.loads(body)
            text = data.get('text', '')

            if state["admin_handle"] and text:
                send_response(state["admin_handle"], text)

            self.send_response(200)
            self.end_headers()

        except Exception as e:
            logger.error(f"Error handling notify: {e}")
            self.send_response(500)
            self.end_headers()


# --- Main ---

def check_all_permissions() -> bool:
    """Check all required permissions."""
    # Check database read permission
    ok, msg = check_db_permissions()
    if not ok:
        logger.error(f"Database permission check failed:\n{msg}")
        return False

    # Check AppleScript permission
    ok, msg = check_sender_permissions()
    if not ok:
        logger.error(f"AppleScript permission check failed:\n{msg}")
        return False

    return True


def shutdown_handler(signum, frame):
    """Handle shutdown signal."""
    logger.info("Shutting down...")
    sys.exit(0)


def main():
    """Main entry point."""
    # Check permissions first
    if not check_all_permissions():
        sys.exit(1)

    ensure_dirs()

    # Setup signal handlers
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Start message receiver
    receiver = MessageReceiver(
        on_message=handle_incoming_message,
        poll_interval=POLL_INTERVAL,
        allowed_handles=get_allowed_handles()
    )
    receiver.start(from_latest=True)

    # Start HTTP server
    server = ThreadedHTTPServer(('127.0.0.1', PORT), RequestHandler)
    logger.info(f"claudecode-imessage v{VERSION} started on 127.0.0.1:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        receiver.stop()
        server.shutdown()


if __name__ == '__main__':
    main()
