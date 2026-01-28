#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.8.0"

import os
import json
import signal
import subprocess
import sys
import threading
import time
import re
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")  # Optional webhook verification
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))
TMUX_PREFIX = os.environ.get("TMUX_PREFIX", "claude-")  # tmux session prefix for isolation
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
PLAYBOOK_FILE = SESSIONS_DIR.parent / "team_playbook.md"
PERSISTENCE_NOTE = "Workers are long-lived and keep context across restarts."

# In-memory state (RAM only, no persistence - tmux IS the persistence)
state = {
    "active": None,  # Currently active session name
    "pending_registration": None,  # Unregistered tmux session awaiting name
    "startup_notified": False,  # Whether we've sent the startup message
}

# Security: Pre-set admin or auto-learn first user (RAM only, re-learns on restart)
ADMIN_CHAT_ID_ENV = os.environ.get("ADMIN_CHAT_ID", "")
admin_chat_id = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV else None

BOT_COMMANDS = [
    # Daily commands (frequency-first, natural workflow order)
    {"command": "team", "description": "Show your team"},
    {"command": "focus", "description": "Focus a worker: /focus <name>"},
    {"command": "progress", "description": "Check focused worker status"},
    {"command": "learn", "description": "Ask focused worker what they learned"},
    {"command": "pause", "description": "Pause focused worker"},
    {"command": "relaunch", "description": "Relaunch focused worker"},
    # Occasional
    {"command": "settings", "description": "Show settings"},
    # Rare (onboarding/offboarding)
    {"command": "hire", "description": "Hire a worker: /hire <name>"},
    {"command": "end", "description": "Offboard a worker: /end <name>"},
]

BLOCKED_COMMANDS = [
    "/mcp", "/help", "/config", "/model", "/compact", "/cost",
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


def format_response_text(session_name, text):
    """Format response with session prefix. No escaping - Claude Code handles safety."""
    return f"<b>{session_name}:</b>\n{text}"


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
    """Check if session has a pending request. Auto-clears after 10 min timeout."""
    pending = get_pending_file(name)
    if not pending.exists():
        return False
    try:
        ts = int(pending.read_text().strip())
        if (time.time() - ts) > 600:  # 10 min timeout - auto-clear stale pending
            pending.unlink()
            return False
        return True
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


def get_registered_sessions(registered=None):
    """Get registered sessions from tmux and reconcile active state."""
    if registered is None:
        registered, _ = scan_tmux_sessions()

    if state["active"] and state["active"] not in registered:
        state["active"] = None
    if registered and not state["active"]:
        state["active"] = list(registered.keys())[0]

    return registered


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
    """Send Enter key to tmux session."""
    result = subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])
    return result.returncode == 0


def tmux_send_escape(tmux_name):
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Escape"])


def export_hook_env(tmux_name):
    """Export env vars for hook inside tmux session."""
    subprocess.run([
        "tmux", "send-keys", "-t", tmux_name,
        f"export PORT={PORT} TMUX_PREFIX='{TMUX_PREFIX}' SESSIONS_DIR='{SESSIONS_DIR}'", "Enter"
    ])


def create_session(name):
    """Create a new Claude instance.

    SECURITY: Token is NOT exported to Claude session. Hook forwards responses
    to bridge via localhost HTTP, bridge sends to Telegram. Token isolation.
    """
    tmux_name = f"{TMUX_PREFIX}{name}"

    if tmux_exists(tmux_name):
        return False, f"Worker '{name}' already exists"

    # Create tmux session
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
        capture_output=True
    )
    if result.returncode != 0:
        return False, "Could not start the worker workspace"

    time.sleep(0.5)

    # Export env vars for hook (PORT for bridge endpoint, TMUX_PREFIX for session detection, SESSIONS_DIR for pending files)
    # SECURITY: Token is NOT exported - hook forwards to bridge via localhost HTTP
    export_hook_env(tmux_name)
    time.sleep(0.3)

    # Start claude
    subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                   "claude --dangerously-skip-permissions", "Enter"])

    # Wait for confirmation dialog and accept it (select option 2: "Yes, I accept")
    time.sleep(1.5)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "2"])  # Select option 2
    time.sleep(0.3)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])  # Confirm

    state["active"] = name
    ensure_session_dir(name)

    return True, None


def kill_session(name):
    """Kill a Claude instance."""
    registered = get_registered_sessions()
    if name not in registered:
        return False, f"Worker '{name}' not found"

    tmux_name = registered[name]["tmux"]
    subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)

    # Clear active if it was the killed session
    if state["active"] == name:
        state["active"] = None
        get_registered_sessions()

    return True, None


def restart_claude(name):
    """Restart claude in an existing tmux session."""
    registered = get_registered_sessions()
    if name not in registered:
        return False, f"Worker '{name}' not found"

    tmux_name = registered[name]["tmux"]

    if not tmux_exists(tmux_name):
        return False, "Worker workspace is not running"

    if is_claude_running(tmux_name):
        return False, "Worker is already running"

    # Re-export env vars for hook (in case session was created by older version or bridge restarted)
    export_hook_env(tmux_name)
    time.sleep(0.3)

    # Start claude in the existing tmux session
    subprocess.run([
        "tmux", "send-keys", "-t", tmux_name,
        "claude --dangerously-skip-permissions", "Enter"
    ])

    return True, None


def switch_session(name):
    """Switch active session."""
    registered = get_registered_sessions()
    if name not in registered:
        return False, f"Worker '{name}' not found"

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
        return False, "Could not claim the running worker"

    # Export env vars for hook (same as create_session)
    export_hook_env(new_tmux_name)

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
        telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": "Working - Team hub going offline. Your workers stay intact and keep context."
        })
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

            # Format with session prefix and HTML escaping
            response_text = format_response_text(session_name, text)

            # Bridge sends to Telegram (only place with token)
            result = telegram_api("sendMessage", {"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})
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

        # Handle @all broadcast
        if text.lower().startswith("@all "):
            message = text[5:]  # Remove "@all "
            self.route_to_all(message, chat_id, msg_id)
            return

        # Check for reply context (used by both @mention and reply routing)
        reply_to = msg.get("reply_to_message")
        reply_context = ""
        if reply_to:
            _, reply_context = self.parse_reply_target(reply_to)

        # Handle @name prefix for one-off routing (priority over reply target)
        target_session, message = self.parse_at_mention(text)

        if target_session:
            # Include reply context if replying to a message
            if reply_context:
                message = self.format_reply_context(message, reply_context)
            self.route_message(target_session, message, chat_id, msg_id, one_off=True)
            return

        # Handle reply-to-worker routing (when no @mention)
        if reply_to and reply_context:
            reply_target, _ = self.parse_reply_target(reply_to)
            if reply_target:
                routed_text = self.format_reply_context(text, reply_context)
                self.route_message(reply_target, routed_text, chat_id, msg_id, one_off=True)
                return

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
                    self.reply(chat_id, "Name must use letters, numbers, and hyphens only.", outcome="Needs decision")
                    return True

                registered = get_registered_sessions()
                if name in registered:
                    self.reply(chat_id, f"Worker name \"{name}\" is already on the team. Choose another.", outcome="Needs decision")
                    return True

                ok, err = register_session(name, state["pending_registration"])
                if ok:
                    self.reply(chat_id, f"Claimed and focused \"{name}\". {PERSISTENCE_NOTE}")
                else:
                    self.reply(chat_id, f"Could not claim that worker. {err}", outcome="Needs decision")
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
            registered = get_registered_sessions()
            if name in registered:
                return name, message
        return None, text

    def parse_worker_prefix(self, text):
        """Parse worker_name: prefix from a message."""
        if not text:
            return None, ""
        match = re.match(r'^\s*([a-zA-Z0-9-]+):\s*(.*)$', text, re.DOTALL)
        if not match:
            return None, ""
        name = match.group(1).lower()
        message = match.group(2).strip()
        registered = get_registered_sessions()
        if name not in registered:
            return None, ""
        return name, message

    def parse_reply_target(self, reply_msg):
        """Extract worker target and context from a replied-to message."""
        if not reply_msg:
            return None, ""
        reply_from = reply_msg.get("from", {})
        if reply_from and reply_from.get("is_bot") is False:
            return None, ""
        reply_text = reply_msg.get("text") or reply_msg.get("caption") or ""
        return self.parse_worker_prefix(reply_text)

    def format_reply_context(self, reply_text, context_text):
        """Format a manager reply with context for the worker."""
        reply_text = (reply_text or "").strip()
        context_text = (context_text or "").strip()
        if context_text:
            return (
                "Manager reply:\n"
                f"{reply_text}\n\n"
                "Context (your previous message):\n"
                f"{context_text}"
            )
        return f"Manager reply:\n{reply_text}"

    def handle_command(self, text, chat_id, msg_id):
        """Handle /commands. Returns True if handled."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        # Strip @botname suffix (Telegram appends this in groups/autocomplete)
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/hire", "/new"):
            return self.cmd_hire(arg, chat_id)
        elif cmd in ("/focus", "/use"):
            return self.cmd_focus(arg, chat_id)
        elif cmd in ("/team", "/list"):
            return self.cmd_team(chat_id)
        elif cmd in ("/end", "/kill"):
            return self.cmd_end(arg, chat_id)
        elif cmd in ("/progress", "/status"):
            return self.cmd_progress(chat_id)
        elif cmd in ("/pause", "/stop"):
            return self.cmd_pause(chat_id)
        elif cmd in ("/relaunch", "/restart"):
            return self.cmd_relaunch(chat_id)
        elif cmd in ("/settings", "/system"):
            return self.cmd_settings(chat_id)
        elif cmd == "/learn":
            return self.cmd_learn(arg, chat_id, msg_id)
        elif cmd in BLOCKED_COMMANDS:
            self.reply(chat_id, f"{cmd} is interactive and not supported here.", outcome="Needs decision")
            return True

        # Not a control command - might be a Claude command, pass through
        return False

    def cmd_hire(self, name, chat_id):
        """Create new Claude instance."""
        if not name:
            self.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
            return True

        name = name.lower().strip()
        name = re.sub(r'[^a-z0-9-]', '', name)

        if not name:
            self.reply(chat_id, "Name must use letters, numbers, and hyphens only.", outcome="Needs decision")
            return True

        ok, err = create_session(name)
        if ok:
            self.reply(chat_id, f"Hired \"{name}\" and focused them. {PERSISTENCE_NOTE}")
        else:
            self.reply(chat_id, f"Could not hire \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_focus(self, name, chat_id):
        """Switch focused Claude."""
        if not name:
            self.reply(chat_id, "Usage: /focus <name>", outcome="Needs decision")
            return True

        name = name.lower().strip()
        ok, err = switch_session(name)
        if ok:
            self.reply(chat_id, f"Focused \"{name}\". They keep their context.")
        else:
            self.reply(chat_id, f"Could not focus \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_team(self, chat_id):
        """List all Claude instances."""
        # Refresh from tmux
        registered, unregistered = scan_tmux_sessions()
        registered = get_registered_sessions(registered)

        if not registered and not unregistered:
            self.reply(chat_id, "No workers yet. Hire your first long-lived worker with /hire <name>.", outcome="Needs decision")
            return True

        lines = []
        lines.append(f"Team overview. {PERSISTENCE_NOTE}")
        lines.append(f"Focused: {state['active'] or '(none)'}")
        lines.append("Workers:")
        for name in sorted(registered.keys()):
            status = []
            if name == state["active"]:
                status.append("focused")
            status.append("working" if is_pending(name) else "available")
            lines.append(f"- {name} ({', '.join(status)})")

        if unregistered:
            lines.append("")
            lines.append("Unclaimed running Claude (needs a name):")
            for tmux in unregistered:
                lines.append(f"- {tmux}")

        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_end(self, name, chat_id):
        """Kill a Claude instance."""
        if not name:
            self.reply(chat_id, "Offboarding is permanent. Usage: /end <name>", outcome="Needs decision")
            return True

        name = name.lower().strip()
        ok, err = kill_session(name)
        if ok:
            self.reply(chat_id, f"Offboarded \"{name}\". This is for long-lived team changes.")
        else:
            self.reply(chat_id, f"Could not offboard \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_progress(self, chat_id):
        """Show detailed status of focused Claude."""
        if not state["active"]:
            self.reply(chat_id, "No focused worker. Use /team or /focus <name>.", outcome="Needs decision")
            return True

        name = state["active"]
        registered = get_registered_sessions()
        session = registered.get(name)
        if not session:
            self.reply(chat_id, "Focused worker not found. Use /team to refocus.", outcome="Needs decision")
            return True

        tmux_name = session["tmux"]
        exists = tmux_exists(tmux_name)
        pending = is_pending(name)

        status = []
        status.append(f"Progress for focused worker: {name}")
        status.append("Focused: yes")
        status.append(f"Working: {'yes' if pending else 'no'}")
        status.append(f"Online: {'yes' if exists else 'no'}")

        if exists:
            claude_running = is_claude_running(tmux_name)
            status.append(f"Ready: {'yes' if claude_running else 'no'}")
            if not claude_running:
                status.append("Needs attention: worker app is not running. Use /relaunch.")

        self.reply(chat_id, "\n".join(status))
        return True

    def cmd_pause(self, chat_id):
        """Interrupt active Claude."""
        if not state["active"]:
            self.reply(chat_id, "No focused worker.", outcome="Needs decision")
            return True

        name = state["active"]
        registered = get_registered_sessions()
        session = registered.get(name)
        if session:
            tmux_send_escape(session["tmux"])
            clear_pending(name)

        self.reply(chat_id, f"Paused \"{name}\". They stay focused and keep context.")
        return True

    def cmd_relaunch(self, chat_id):
        """Restart Claude in active session."""
        if not state["active"]:
            self.reply(chat_id, "No focused worker.", outcome="Needs decision")
            return True

        name = state["active"]
        ok, err = restart_claude(name)
        if ok:
            self.reply(chat_id, f"Relaunching \"{name}\" now. Their context remains intact.", outcome="Working")
        else:
            self.reply(chat_id, f"Could not relaunch \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_settings(self, chat_id):
        """Show system configuration (secrets redacted)."""
        def redact(s):
            if not s:
                return "(not set)"
            if len(s) <= 8:
                return "***"
            return s[:4] + "..." + s[-4:]

        registered = get_registered_sessions()
        team_list = ", ".join(registered.keys()) if registered else "(none)"
        lines = [
            f"claudecode-telegram v{VERSION}",
            PERSISTENCE_NOTE,
            "",
            f"Bot token: {redact(BOT_TOKEN)}",
            f"Admin: {admin_chat_id or '(auto-learn)'}",
            f"Webhook verification: {redact(WEBHOOK_SECRET) if WEBHOOK_SECRET else '(disabled)'}",
            f"Team storage: {SESSIONS_DIR.parent}",
            "",
            "Team state",
            f"Focused worker: {state['active'] or '(none)'}",
            f"Workers: {team_list}",
            f"Pending claim: {state['pending_registration'] or '(none)'}",
        ]
        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_learn(self, topic, chat_id, msg_id=None):
        """Ask the focused worker what they learned today, optionally about a topic."""
        if not state["active"]:
            self.reply(chat_id, "No focused worker. Use /focus <name> first.", outcome="Needs decision")
            return True

        name = state["active"]
        registered = get_registered_sessions()
        session = registered.get(name)
        if not session:
            self.reply(chat_id, "Focused worker not found.", outcome="Needs decision")
            return True

        tmux_name = session["tmux"]
        if not tmux_exists(tmux_name) or not is_claude_running(tmux_name):
            self.reply(chat_id, f"Worker \"{name}\" is not online. Use /relaunch first.", outcome="Needs decision")
            return True

        # Build prompt based on whether topic is provided
        topic = topic.strip() if topic else ""
        if topic:
            prompt = (
                f"What did you learn about {topic} today? Please answer in Problem / Fix / Why format:\n"
                "Problem: <what went wrong or was inefficient>\n"
                "Fix: <the better approach>\n"
                "Why: <root cause or insight>"
            )
        else:
            prompt = (
                "What did you learn today? Please answer in Problem / Fix / Why format:\n"
                "Problem: <what went wrong or was inefficient>\n"
                "Fix: <the better approach>\n"
                "Why: <root cause or insight>"
            )

        set_pending(name, chat_id)

        # Start typing indicator
        threading.Thread(
            target=send_typing_loop,
            args=(chat_id, name),
            daemon=True
        ).start()

        # Send prompt to worker
        send_ok = tmux_send(tmux_name, prompt)
        enter_ok = tmux_send_enter(tmux_name)

        # 👀 reaction confirms delivery - no text reply needed (worker will respond)
        if msg_id and send_ok and enter_ok:
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "👀"}]
            })
        return True

    def parse_learning(self, text):
        """Parse Problem/Fix/Why sections from text."""
        sections = {"problem": "", "fix": "", "why": ""}
        for line in text.splitlines():
            match = re.match(r'^\s*(problem|fix|why)\s*[:\-]\s*(.+)\s*$', line, re.IGNORECASE)
            if match:
                key = match.group(1).lower()
                sections[key] = match.group(2).strip()

        if all(sections.values()):
            return sections["problem"], sections["fix"], sections["why"]

        if "|" in text:
            parts = [p.strip() for p in text.split("|")]
        elif " / " in text:
            parts = [p.strip() for p in text.split(" / ")]
        elif text.count("/") >= 2:
            parts = [p.strip() for p in text.split("/")]
        else:
            parts = []

        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]

        return sections["problem"], sections["fix"], sections["why"]

    def append_learning_to_playbook(self, problem, fix, why):
        """Append a learning entry to the team playbook."""
        PLAYBOOK_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not PLAYBOOK_FILE.exists():
            PLAYBOOK_FILE.write_text("# Team Playbook\n\n", encoding="utf-8")
            PLAYBOOK_FILE.chmod(0o600)

        timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        focused = state["active"] or "none"
        entry = (
            f"## {timestamp}\n"
            f"- Problem: {problem}\n"
            f"- Fix: {fix}\n"
            f"- Why: {why}\n"
            f"- Focused worker: {focused}\n\n"
        )

        with PLAYBOOK_FILE.open("a", encoding="utf-8") as handle:
            handle.write(entry)

    def share_learning_with_team(self, problem, fix, why):
        """Share the learning with online workers."""
        registered = get_registered_sessions()
        shared_with = []
        note = (
            "Team learning (long-lived context):\n"
            f"Problem: {problem}\n"
            f"Fix: {fix}\n"
            f"Why: {why}\n"
            "Please add this to your context."
        )

        for name, session in registered.items():
            tmux_name = session["tmux"]
            if tmux_exists(tmux_name) and is_claude_running(tmux_name):
                if tmux_send(tmux_name, note) and tmux_send_enter(tmux_name):
                    shared_with.append(name)

        return shared_with

    def route_to_active(self, text, chat_id, msg_id):
        """Route message to active session or handle no-session cases."""
        # Check for unregistered sessions
        registered, unregistered = scan_tmux_sessions()
        registered = get_registered_sessions(registered)

        if not state["active"]:
            if unregistered:
                # Prompt for registration
                state["pending_registration"] = unregistered[0]
                self.reply(
                    chat_id,
                    "Found a running Claude not yet on your team.\n"
                    "Claim it to make it a long-lived worker by replying with:\n"
                    '{"name": "your-worker-name"}',
                    outcome="Needs decision"
                )
                return
            elif registered:
                # Sessions exist but none active
                names = ", ".join(registered.keys())
                self.reply(chat_id, f"No focused worker. Team: {names}\nUse: /focus <name>", outcome="Needs decision")
                return
            else:
                # No sessions at all
                self.reply(chat_id, "No workers yet. Hire your first long-lived worker with /hire <name>.", outcome="Needs decision")
                return

        self.route_message(state["active"], text, chat_id, msg_id, one_off=False)

    def route_to_all(self, text, chat_id, msg_id):
        """Broadcast message to all running sessions."""
        registered = get_registered_sessions()
        sessions = list(registered.keys())
        if not sessions:
            self.reply(chat_id, "No workers yet. Hire your first long-lived worker with /hire <name>.", outcome="Needs decision")
            return

        sent_to = []
        for name in sessions:
            session = registered[name]
            tmux_name = session["tmux"]
            if tmux_exists(tmux_name) and is_claude_running(tmux_name):
                # Route without setting as active
                self.route_message(name, text, chat_id, msg_id, one_off=True)
                sent_to.append(name)

        if not sent_to:
            self.reply(chat_id, "No online workers to share with right now.", outcome="Needs decision")

    def route_message(self, session_name, text, chat_id, msg_id, one_off=False):
        """Route a message to a specific session."""
        registered = get_registered_sessions()
        session = registered.get(session_name)
        if not session:
            self.reply(chat_id, f"Worker \"{session_name}\" not found.", outcome="Needs decision")
            return

        tmux_name = session["tmux"]

        if not tmux_exists(tmux_name):
            self.reply(chat_id, f"Worker \"{session_name}\" is not online right now.", outcome="Needs decision")
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

    def reply(self, chat_id, text, outcome="Done"):
        prefix = f"{outcome} - " if text else f"{outcome}"
        message = f"{prefix}{text}" if text else prefix
        telegram_api("sendMessage", {"chat_id": chat_id, "text": message})

    def send_startup_message(self, chat_id):
        """Send bridge startup notification."""
        registered = get_registered_sessions()
        sessions = list(registered.keys())
        active = state["active"]

        lines = [f"Team hub online. {PERSISTENCE_NOTE}"]
        if sessions:
            lines.append(f"Team: {', '.join(sessions)}")
            if active:
                lines.append(f"Focused: {active}")
        else:
            lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")

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

    # Write port file for hook to read (solves port mismatch on bridge restart)
    port_file = SESSIONS_DIR.parent / "port"
    port_file.write_text(str(PORT))
    port_file.chmod(0o600)

    # Discover existing sessions
    registered, unregistered = scan_tmux_sessions()
    registered = get_registered_sessions(registered)
    if registered:
        print(f"Discovered sessions: {list(registered.keys())}")
    if unregistered:
        print(f"Unregistered sessions: {unregistered}")

    setup_bot_commands()
    print(f"Multi-Session Bridge on :{PORT}")
    print(f"Hook endpoint: http://localhost:{PORT}/response")
    print(f"Active: {state['active'] or 'none'}")
    print(f"Sessions: {list(registered.keys()) or 'none'}")
    if WEBHOOK_SECRET:
        print("Webhook verification: enabled")
    else:
        print("Webhook verification: disabled (set TELEGRAM_WEBHOOK_SECRET to enable)")
    if admin_chat_id:
        print(f"Admin: {admin_chat_id} (pre-configured)")
    else:
        print("Admin: auto-learn (first user to message becomes admin)")

    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
