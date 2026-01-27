#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.5.4"

import os
import json
import signal
import subprocess
import sys
import threading
import time
import re
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")  # Optional webhook verification
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))
TMUX_PREFIX = os.environ.get("TMUX_PREFIX", "claude-")  # tmux session prefix for isolation
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"

# In-memory state (RAM only, no persistence - tmux IS the persistence)
state = {
    "active": None,  # Currently active session name
    "sessions": {},  # name -> {"tmux": "claude-<name>"}
    "pending_registration": None,  # Unregistered tmux session awaiting name
    "startup_notified": False,  # Whether we've sent the startup message
}

# Security: Pre-set admin or auto-learn first user (RAM only, re-learns on restart)
ADMIN_CHAT_ID_ENV = os.environ.get("ADMIN_CHAT_ID", "")
admin_chat_id = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV else None

BOT_COMMANDS = [
    {"command": "new", "description": "Create new Claude: /new <name>"},
    {"command": "use", "description": "Switch active Claude: /use <name>"},
    {"command": "list", "description": "List all Claude instances"},
    {"command": "kill", "description": "Stop Claude: /kill <name>"},
    {"command": "status", "description": "Detailed status of active Claude"},
    {"command": "stop", "description": "Interrupt active Claude (Escape)"},
    {"command": "restart", "description": "Restart Claude in active session"},
    {"command": "system", "description": "Show system config (secrets redacted)"},
]

BLOCKED_COMMANDS = [
    "/mcp", "/help", "/settings", "/config", "/model", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen"
]


def telegram_api(method, data):
    if not BOT_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Telegram API error: {e}")
        return None


def setup_bot_commands():
    result = telegram_api("setMyCommands", {"commands": BOT_COMMANDS})
    if result and result.get("ok"):
        print("Bot commands registered")


# ─────────────────────────────────────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────────────────────────────────────

def get_session_dir(name):
    """Get per-session directory path."""
    return SESSIONS_DIR / name


def ensure_session_dir(name):
    """Create session directory if needed with secure permissions (0o700)."""
    d = get_session_dir(name)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Ensure parent directories also have secure permissions
    SESSIONS_DIR.chmod(0o700)
    d.chmod(0o700)
    return d


def get_pending_file(name):
    return get_session_dir(name) / "pending"


def get_chat_id_file(name):
    return get_session_dir(name) / "chat_id"


def set_pending(name, chat_id):
    """Mark session as having a pending request with secure permissions (0o600)."""
    d = ensure_session_dir(name)
    pending = d / "pending"
    chat_id_file = d / "chat_id"
    pending.write_text(str(int(time.time())))
    pending.chmod(0o600)
    chat_id_file.write_text(str(chat_id))
    chat_id_file.chmod(0o600)


def clear_pending(name):
    """Clear pending status for session."""
    d = get_session_dir(name)
    pending = d / "pending"
    if pending.exists():
        pending.unlink()


def is_pending(name):
    """Check if session has a pending request."""
    pending = get_pending_file(name)
    if not pending.exists():
        return False
    try:
        ts = int(pending.read_text().strip())
        return (time.time() - ts) < 600  # 10 min timeout
    except:
        return False


def scan_tmux_sessions():
    """Scan tmux for claude-* sessions (registered) and other claude sessions (unregistered)."""
    registered = {}
    unregistered = []

    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return registered, unregistered

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            session_name = line.strip()

            # Check if this session is running claude
            pane_cmd = subprocess.run(
                ["tmux", "display-message", "-t", session_name, "-p", "#{pane_current_command}"],
                capture_output=True, text=True
            )
            cmd = pane_cmd.stdout.strip() if pane_cmd.returncode == 0 else ""

            if session_name.startswith(TMUX_PREFIX):
                # Registered session
                name = session_name[len(TMUX_PREFIX):]  # Remove prefix
                registered[name] = {"tmux": session_name}
            elif "claude" in cmd.lower() or session_name == "claude":
                # Unregistered session running claude
                unregistered.append(session_name)
    except Exception as e:
        print(f"Error scanning tmux: {e}")

    return registered, unregistered


def init_sessions():
    """Initialize state by scanning tmux on startup."""
    global state
    registered, unregistered = scan_tmux_sessions()
    state["sessions"] = registered

    # Set active to first session if any exist
    if registered and not state["active"]:
        state["active"] = list(registered.keys())[0]

    print(f"Discovered sessions: {list(registered.keys())}")
    if unregistered:
        print(f"Unregistered sessions: {unregistered}")


def tmux_exists(tmux_name):
    """Check if tmux session exists."""
    return subprocess.run(
        ["tmux", "has-session", "-t", tmux_name],
        capture_output=True
    ).returncode == 0


def get_pane_command(tmux_name):
    """Get the current command running in tmux pane."""
    result = subprocess.run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_command}"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def is_claude_running(tmux_name):
    """Check if claude process is running in tmux session."""
    cmd = get_pane_command(tmux_name)
    return "claude" in cmd.lower()


def tmux_send(tmux_name, text, literal=True):
    """Send text to tmux session. Returns True if successful."""
    cmd = ["tmux", "send-keys", "-t", tmux_name]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    result = subprocess.run(cmd)
    return result.returncode == 0


def tmux_send_enter(tmux_name):
    result = subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])
    return result.returncode == 0


def tmux_send_escape(tmux_name):
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Escape"])


def create_session(name):
    """Create a new Claude instance.

    SECURITY: Token is NOT exported to Claude session. Hook forwards responses
    to bridge via localhost HTTP, bridge sends to Telegram. Token isolation.
    """
    tmux_name = f"{TMUX_PREFIX}{name}"

    if tmux_exists(tmux_name):
        return False, f"Session '{name}' already exists"

    # Create tmux session
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
        capture_output=True
    )
    if result.returncode != 0:
        return False, "Failed to create tmux session"

    time.sleep(0.5)

    # Export env vars for hook (PORT for bridge endpoint, TMUX_PREFIX for session detection, SESSIONS_DIR for pending files)
    # SECURITY: Token is NOT exported - hook forwards to bridge via localhost HTTP
    subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                   f"export PORT={PORT} TMUX_PREFIX='{TMUX_PREFIX}' SESSIONS_DIR='{SESSIONS_DIR}'", "Enter"])
    time.sleep(0.3)

    # Start claude
    subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                   "claude --dangerously-skip-permissions", "Enter"])

    # Wait for confirmation dialog and accept it (select option 2: "Yes, I accept")
    time.sleep(1.5)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "2"])  # Select option 2
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])  # Confirm

    # Register in state
    state["sessions"][name] = {"tmux": tmux_name}
    state["active"] = name
    ensure_session_dir(name)

    return True, None


def kill_session(name):
    """Kill a Claude instance."""
    if name not in state["sessions"]:
        return False, f"Session '{name}' not found"

    tmux_name = state["sessions"][name]["tmux"]
    subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)

    del state["sessions"][name]

    # Clear active if it was the killed session
    if state["active"] == name:
        state["active"] = list(state["sessions"].keys())[0] if state["sessions"] else None

    return True, None


def restart_claude(name):
    """Restart claude in an existing tmux session."""
    if name not in state["sessions"]:
        return False, f"Session '{name}' not found"

    tmux_name = state["sessions"][name]["tmux"]

    if not tmux_exists(tmux_name):
        return False, f"tmux session '{tmux_name}' not running"

    if is_claude_running(tmux_name):
        return False, "Claude is already running"

    # Re-export env vars for hook (in case session was created by older version or bridge restarted)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                   f"export PORT={PORT} TMUX_PREFIX='{TMUX_PREFIX}' SESSIONS_DIR='{SESSIONS_DIR}'", "Enter"])
    time.sleep(0.3)

    # Start claude in the existing tmux session
    subprocess.run([
        "tmux", "send-keys", "-t", tmux_name,
        "claude --dangerously-skip-permissions", "Enter"
    ])

    return True, None


def switch_session(name):
    """Switch active session."""
    if name not in state["sessions"]:
        return False, f"Session '{name}' not found"

    state["active"] = name
    return True, None


def register_session(name, tmux_session):
    """Register an unregistered tmux session under a name."""
    new_tmux_name = f"{TMUX_PREFIX}{name}"

    # Rename the tmux session
    result = subprocess.run(
        ["tmux", "rename-session", "-t", tmux_session, new_tmux_name],
        capture_output=True
    )
    if result.returncode != 0:
        return False, "Failed to rename tmux session"

    # Export env vars for hook (same as create_session)
    subprocess.run(["tmux", "send-keys", "-t", new_tmux_name,
                   f"export PORT={PORT} TMUX_PREFIX='{TMUX_PREFIX}' SESSIONS_DIR='{SESSIONS_DIR}'", "Enter"])

    # Register in state
    state["sessions"][name] = {"tmux": new_tmux_name}
    state["active"] = name
    state["pending_registration"] = None
    ensure_session_dir(name)

    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Typing indicator
# ─────────────────────────────────────────────────────────────────────────────

def send_typing_loop(chat_id, session_name):
    """Send typing indicator while request is pending."""
    while is_pending(session_name):
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        time.sleep(4)


def get_all_chat_ids():
    """Get all unique chat_ids from session files."""
    chat_ids = set()
    if SESSIONS_DIR.exists():
        for session_dir in SESSIONS_DIR.iterdir():
            if session_dir.is_dir():
                chat_id_file = session_dir / "chat_id"
                if chat_id_file.exists():
                    try:
                        chat_id = chat_id_file.read_text().strip()
                        if chat_id:
                            chat_ids.add(chat_id)
                    except Exception:
                        pass
    # Also include current admin if known
    if admin_chat_id:
        chat_ids.add(str(admin_chat_id))
    return chat_ids


def send_shutdown_message():
    """Send shutdown notification to all known chat_ids."""
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        print("No chat_ids to notify")
        return

    print(f"Sending shutdown to {len(chat_ids)} chat(s)...")
    for chat_id in chat_ids:
        telegram_api("sendMessage", {"chat_id": chat_id, "text": "Bridge going offline"})
    print("Shutdown notifications sent")


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Route based on path
        if self.path == "/response":
            # Hook forwarding response (internal, no auth needed - localhost only)
            self.handle_hook_response()
            return

        if self.path == "/notify":
            # Internal endpoint for system notifications (localhost only)
            self.handle_notify()
            return

        if self.path == "/broadcast":
            # Internal endpoint for session-initiated messages (localhost only)
            self.handle_broadcast()
            return

        # Telegram webhook - optional secret verification
        if WEBHOOK_SECRET:
            header_token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if header_token != WEBHOOK_SECRET:
                print(f"Webhook rejected: invalid secret token")
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return

        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            update = json.loads(body)
            if "message" in update:
                self.handle_message(update)
        except Exception as e:
            print(f"Error: {e}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def handle_notify(self):
        """Handle system notification request (internal, localhost only).

        SECURITY: This endpoint allows the shell script to trigger
        notifications without having access to the bot token.
        Used for tunnel watchdog alerts.
        """
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
            text = data.get("text", "")

            if not text:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing text")
                return

            # Send to all known chat_ids
            chat_ids = get_all_chat_ids()
            sent = 0
            for chat_id in chat_ids:
                result = telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
                if result and result.get("ok"):
                    sent += 1

            print(f"Notify: sent to {sent}/{len(chat_ids)} chats: {text[:50]}...")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Sent to {sent} chats".encode())
        except Exception as e:
            print(f"Notify error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_broadcast(self):
        """Handle broadcast message from sessions (internal, localhost only).

        SECURITY: Allows Claude sessions to send messages to Telegram
        without needing a pending file. Used for unprompted notifications.
        Token stays in bridge.
        """
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
            sender = data.get("from", "unknown")
            text = data.get("text", "")

            if not text:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing text")
                return

            # Format message with sender
            formatted = f"[{sender}] {text}"

            # Send to admin (primary recipient for broadcasts)
            if admin_chat_id:
                result = telegram_api("sendMessage", {
                    "chat_id": admin_chat_id,
                    "text": formatted,
                    "parse_mode": "HTML"
                })
                if result and result.get("ok"):
                    print(f"Broadcast from {sender}: {text[:50]}...")
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                    return

            # Fallback: no admin set
            print(f"Broadcast failed: no admin_chat_id")
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"No admin configured")
        except Exception as e:
            print(f"Broadcast error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_hook_response(self):
        """Handle response forwarded from Claude hook.

        SECURITY: This is how Claude responses get to Telegram without
        Claude ever having access to the bot token. Hook POSTs here,
        bridge sends to Telegram.
        """
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
            session_name = data.get("session")
            text = data.get("text", "")

            if not session_name or not text:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing session or text")
                return

            # Get chat_id from session's file
            chat_id_file = get_chat_id_file(session_name)
            if not chat_id_file.exists():
                print(f"Hook response: no chat_id for session '{session_name}'")
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"No chat_id for session")
                return

            chat_id = chat_id_file.read_text().strip()
            print(f"Hook response: {session_name} -> chat {chat_id} ({len(text)} chars)")

            # Bridge sends to Telegram (only place with token)
            result = telegram_api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
            if result and result.get("ok"):
                print(f"Response sent: {session_name} -> Telegram OK")

            # Clear pending
            clear_pending(session_name)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print(f"Hook response error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Claude-Telegram Multi-Session Bridge")

    def handle_message(self, update):
        global admin_chat_id

        msg = update.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        msg_id = msg.get("message_id")

        if not text or not chat_id:
            return

        # SECURITY: Auto-learn first user as admin
        if admin_chat_id is None:
            admin_chat_id = chat_id
            print(f"Admin registered: {chat_id}")

        # Send startup notification on first admin interaction
        if not state["startup_notified"]:
            state["startup_notified"] = True
            self.send_startup_message(chat_id)

        # SECURITY: Reject non-admin users (silent - don't reveal bot exists)
        if chat_id != admin_chat_id:
            print(f"Rejected non-admin: {chat_id}")
            return  # Silent rejection

        # Check for JSON registration response
        if state["pending_registration"]:
            if self.try_registration(text, chat_id):
                return

        # Handle commands
        if text.startswith("/"):
            if self.handle_command(text, chat_id, msg_id):
                return

        # Handle @name prefix for one-off routing
        target_session, message = self.parse_at_mention(text)

        if target_session:
            self.route_message(target_session, message, chat_id, msg_id, one_off=True)
        else:
            # Route to active session
            self.route_to_active(text, chat_id, msg_id)

    def try_registration(self, text, chat_id):
        """Try to parse JSON registration."""
        try:
            data = json.loads(text.strip())
            if "name" in data:
                name = data["name"].strip().lower()
                name = re.sub(r'[^a-z0-9-]', '', name)

                if not name:
                    self.reply(chat_id, "Invalid name. Use alphanumeric and hyphens only.")
                    return True

                if name in state["sessions"]:
                    self.reply(chat_id, f"Name '{name}' already in use. Choose another.")
                    return True

                ok, err = register_session(name, state["pending_registration"])
                if ok:
                    self.reply(chat_id, f"Registered \"{name}\" (now active)")
                else:
                    self.reply(chat_id, f"Failed: {err}")
                return True
        except json.JSONDecodeError:
            pass
        return False

    def parse_at_mention(self, text):
        """Parse @name prefix from message."""
        match = re.match(r'^@([a-zA-Z0-9-]+)\s+(.+)$', text, re.DOTALL)
        if match:
            name = match.group(1).lower()
            message = match.group(2)
            if name in state["sessions"]:
                return name, message
        return None, text

    def handle_command(self, text, chat_id, msg_id):
        """Handle /commands. Returns True if handled."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        # Strip @botname suffix (Telegram appends this in groups/autocomplete)
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/new":
            return self.cmd_new(arg, chat_id)
        elif cmd == "/use":
            return self.cmd_use(arg, chat_id)
        elif cmd == "/list":
            return self.cmd_list(chat_id)
        elif cmd == "/kill":
            return self.cmd_kill(arg, chat_id)
        elif cmd == "/status":
            return self.cmd_status(chat_id)
        elif cmd == "/stop":
            return self.cmd_stop(chat_id)
        elif cmd == "/restart":
            return self.cmd_restart(chat_id)
        elif cmd == "/system":
            return self.cmd_system(chat_id)
        elif cmd in BLOCKED_COMMANDS:
            self.reply(chat_id, f"'{cmd}' not supported (interactive)")
            return True

        # Not a control command - might be a Claude command, pass through
        return False

    def cmd_new(self, name, chat_id):
        """Create new Claude instance."""
        if not name:
            self.reply(chat_id, "Usage: /new <name>")
            return True

        name = name.lower().strip()
        name = re.sub(r'[^a-z0-9-]', '', name)

        if not name:
            self.reply(chat_id, "Invalid name. Use alphanumeric and hyphens only.")
            return True

        ok, err = create_session(name)
        if ok:
            self.reply(chat_id, f"Created \"{name}\" (now active)")
        else:
            self.reply(chat_id, f"Failed: {err}")
        return True

    def cmd_use(self, name, chat_id):
        """Switch active Claude."""
        if not name:
            self.reply(chat_id, "Usage: /use <name>")
            return True

        name = name.lower().strip()
        ok, err = switch_session(name)
        if ok:
            self.reply(chat_id, f"Switched to \"{name}\"")
        else:
            self.reply(chat_id, f"Failed: {err}")
        return True

    def cmd_list(self, chat_id):
        """List all Claude instances."""
        # Refresh from tmux
        registered, unregistered = scan_tmux_sessions()
        state["sessions"] = registered

        if not registered and not unregistered:
            self.reply(chat_id, "No sessions. Create with: /new <name>")
            return True

        lines = []
        for name in sorted(registered.keys()):
            marker = " <- active" if name == state["active"] else ""
            pending = " (busy)" if is_pending(name) else ""
            lines.append(f"  {name}{marker}{pending}")

        if unregistered:
            lines.append("\nUnregistered:")
            for tmux in unregistered:
                lines.append(f"  {tmux}")

        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_kill(self, name, chat_id):
        """Kill a Claude instance."""
        if not name:
            self.reply(chat_id, "Usage: /kill <name>")
            return True

        name = name.lower().strip()
        ok, err = kill_session(name)
        if ok:
            self.reply(chat_id, f"Killed \"{name}\"")
        else:
            self.reply(chat_id, f"Failed: {err}")
        return True

    def cmd_status(self, chat_id):
        """Show detailed status of active Claude."""
        if not state["active"]:
            self.reply(chat_id, "No active session. Use /list or /new <name>")
            return True

        name = state["active"]
        session = state["sessions"].get(name)
        if not session:
            self.reply(chat_id, "Active session not found")
            return True

        tmux_name = session["tmux"]
        exists = tmux_exists(tmux_name)
        pending = is_pending(name)

        status = []
        status.append(f"Session: {name}")
        status.append(f"tmux: {tmux_name}")
        status.append(f"tmux running: {'yes' if exists else 'no'}")

        if exists:
            claude_running = is_claude_running(tmux_name)
            pane_cmd = get_pane_command(tmux_name)
            status.append(f"Claude running: {'yes' if claude_running else 'no'}")
            status.append(f"Process: {pane_cmd or '(none)'}")
            if not claude_running:
                status.append("\nClaude exited. Use /restart to restart.")

        status.append(f"Busy: {'yes' if pending else 'no'}")

        self.reply(chat_id, "\n".join(status))
        return True

    def cmd_stop(self, chat_id):
        """Interrupt active Claude."""
        if not state["active"]:
            self.reply(chat_id, "No active session")
            return True

        name = state["active"]
        session = state["sessions"].get(name)
        if session:
            tmux_send_escape(session["tmux"])
            clear_pending(name)

        self.reply(chat_id, f"Interrupted \"{name}\"")
        return True

    def cmd_restart(self, chat_id):
        """Restart Claude in active session."""
        if not state["active"]:
            self.reply(chat_id, "No active session")
            return True

        name = state["active"]
        ok, err = restart_claude(name)
        if ok:
            self.reply(chat_id, f"Restarting Claude in \"{name}\"...")
        else:
            self.reply(chat_id, f"Failed: {err}")
        return True

    def cmd_system(self, chat_id):
        """Show system configuration (secrets redacted)."""
        def redact(s):
            if not s:
                return "(not set)"
            if len(s) <= 8:
                return "***"
            return s[:4] + "..." + s[-4:]

        lines = [
            f"claudecode-telegram v{VERSION} (beastoin)",
            "─" * 20,
            f"Bot Token: {redact(BOT_TOKEN)}",
            f"Port: {PORT}",
            f"Admin: {admin_chat_id or '(auto-learn)'}",
            f"Webhook Secret: {redact(WEBHOOK_SECRET) if WEBHOOK_SECRET else '(disabled)'}",
            f"Sessions Dir: {SESSIONS_DIR}",
            f"tmux Prefix: {TMUX_PREFIX}",
            "",
            "State",
            "─" * 20,
            f"Active: {state['active'] or '(none)'}",
            f"Sessions: {list(state['sessions'].keys()) or '(none)'}",
            f"Pending Registration: {state['pending_registration'] or '(none)'}",
        ]
        self.reply(chat_id, "\n".join(lines))
        return True

    def route_to_active(self, text, chat_id, msg_id):
        """Route message to active session or handle no-session cases."""
        # Check for unregistered sessions
        _, unregistered = scan_tmux_sessions()

        if not state["active"]:
            if unregistered:
                # Prompt for registration
                state["pending_registration"] = unregistered[0]
                self.reply(chat_id,
                    f"Unregistered session detected: {unregistered[0]}\n"
                    f"Register with:\n"
                    f'  {{"name": "your-session-name"}}'
                )
                return
            elif state["sessions"]:
                # Sessions exist but none active
                names = ", ".join(state["sessions"].keys())
                self.reply(chat_id, f"No active session. Found: {names}\nUse: /use <name>")
                return
            else:
                # No sessions at all
                self.reply(chat_id, "No sessions. Create with: /new <name>")
                return

        self.route_message(state["active"], text, chat_id, msg_id, one_off=False)

    def route_message(self, session_name, text, chat_id, msg_id, one_off=False):
        """Route a message to a specific session."""
        session = state["sessions"].get(session_name)
        if not session:
            self.reply(chat_id, f"Session '{session_name}' not found")
            return

        tmux_name = session["tmux"]

        if not tmux_exists(tmux_name):
            self.reply(chat_id, f"Session '{session_name}' tmux not running")
            return

        print(f"[{chat_id}] -> {session_name}: {text[:50]}...")

        # Set pending
        set_pending(session_name, chat_id)

        # Start typing indicator
        threading.Thread(
            target=send_typing_loop,
            args=(chat_id, session_name),
            daemon=True
        ).start()

        # Send to tmux
        send_ok = tmux_send(tmux_name, text)
        enter_ok = tmux_send_enter(tmux_name)

        # Add 👀 reaction only after successful send to Claude
        if msg_id and send_ok and enter_ok:
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "👀"}]
            })

    def reply(self, chat_id, text):
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text})

    def send_startup_message(self, chat_id):
        """Send bridge startup notification."""
        sessions = list(state["sessions"].keys())
        active = state["active"]

        lines = ["Bridge online"]
        if sessions:
            lines.append(f"Sessions: {', '.join(sessions)}")
            if active:
                lines.append(f"Active: {active}")
        else:
            lines.append("No sessions. Create with: /new <name>")

        self.reply(chat_id, "\n".join(lines))

    def log_message(self, *args):
        pass


def graceful_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    sig_name = signal.Signals(signum).name if signum else "unknown"
    print(f"\nReceived {sig_name}, shutting down...")
    send_shutdown_message()
    sys.exit(0)


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        return

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    # Create sessions directory with secure permissions (0o700)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SESSIONS_DIR.chmod(0o700)

    # Discover existing sessions
    init_sessions()

    setup_bot_commands()
    print(f"Multi-Session Bridge on :{PORT}")
    print(f"Hook endpoint: http://localhost:{PORT}/response")
    print(f"Active: {state['active'] or 'none'}")
    print(f"Sessions: {list(state['sessions'].keys()) or 'none'}")
    if WEBHOOK_SECRET:
        print("Webhook verification: enabled")
    else:
        print("Webhook verification: disabled (set TELEGRAM_WEBHOOK_SECRET to enable)")
    if admin_chat_id:
        print(f"Admin: {admin_chat_id} (pre-configured)")
    else:
        print("Admin: auto-learn (first user to message becomes admin)")

    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
