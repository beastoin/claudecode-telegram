#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

import os
import json
import subprocess
import threading
import time
import re
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
SESSIONS_DIR = Path.home() / ".claude" / "telegram" / "sessions"
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"

# In-memory state (RAM only, no persistence - tmux IS the persistence)
state = {
    "active": None,  # Currently active session name
    "sessions": {},  # name -> {"tmux": "claude-<name>"}
    "pending_registration": None,  # Unregistered tmux session awaiting name
}

BOT_COMMANDS = [
    {"command": "new", "description": "Create new Claude: /new <name>"},
    {"command": "use", "description": "Switch active Claude: /use <name>"},
    {"command": "list", "description": "List all Claude instances"},
    {"command": "kill", "description": "Stop Claude: /kill <name>"},
    {"command": "status", "description": "Detailed status of active Claude"},
    {"command": "stop", "description": "Interrupt active Claude (Escape)"},
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
    """Create session directory if needed."""
    d = get_session_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_pending_file(name):
    return get_session_dir(name) / "pending"


def get_chat_id_file(name):
    return get_session_dir(name) / "chat_id"


def set_pending(name, chat_id):
    """Mark session as having a pending request."""
    d = ensure_session_dir(name)
    (d / "pending").write_text(str(int(time.time())))
    (d / "chat_id").write_text(str(chat_id))


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

            if session_name.startswith("claude-"):
                # Registered session
                name = session_name[7:]  # Remove "claude-" prefix
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


def tmux_send(tmux_name, text, literal=True):
    """Send text to tmux session."""
    cmd = ["tmux", "send-keys", "-t", tmux_name]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd)


def tmux_send_enter(tmux_name):
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])


def tmux_send_escape(tmux_name):
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Escape"])


def create_session(name):
    """Create a new Claude instance."""
    tmux_name = f"claude-{name}"

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

    # Export token and start claude
    subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                   f"export TELEGRAM_BOT_TOKEN='{BOT_TOKEN}'", "Enter"])
    time.sleep(0.2)
    subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                   "claude --dangerously-skip-permissions", "Enter"])

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


def switch_session(name):
    """Switch active session."""
    if name not in state["sessions"]:
        return False, f"Session '{name}' not found"

    state["active"] = name
    return True, None


def register_session(name, tmux_session):
    """Register an unregistered tmux session under a name."""
    new_tmux_name = f"claude-{name}"

    # Rename the tmux session
    result = subprocess.run(
        ["tmux", "rename-session", "-t", tmux_session, new_tmux_name],
        capture_output=True
    )
    if result.returncode != 0:
        return False, "Failed to rename tmux session"

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


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
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

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Claude-Telegram Multi-Session Bridge")

    def handle_message(self, update):
        msg = update.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        msg_id = msg.get("message_id")

        if not text or not chat_id:
            return

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
        status.append(f"Running: {'yes' if exists else 'no'}")
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

        # Add reaction
        if msg_id:
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "\u2705"}]
            })

        # Start typing indicator
        threading.Thread(
            target=send_typing_loop,
            args=(chat_id, session_name),
            daemon=True
        ).start()

        # Send to tmux
        tmux_send(tmux_name, text)
        tmux_send_enter(tmux_name)

    def reply(self, chat_id, text):
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text})

    def log_message(self, *args):
        pass


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        return

    # Create sessions directory
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Discover existing sessions
    init_sessions()

    setup_bot_commands()
    print(f"Multi-Session Bridge on :{PORT}")
    print(f"Active: {state['active'] or 'none'}")
    print(f"Sessions: {list(state['sessions'].keys()) or 'none'}")

    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
