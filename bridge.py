#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.19.0"

import os
import json
import mimetypes
import signal
import subprocess
import sys
import threading
import time
import re
import urllib.request
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, Optional, Protocol


# ============================================================
# CONFIGURATION
# ============================================================

class ReuseAddrServer(ThreadingHTTPServer):
    """HTTP server with SO_REUSEADDR to avoid 'Address already in use' on restart."""
    allow_reuse_address = True

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")  # Optional webhook verification
SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))
TMUX_PREFIX = os.environ.get("TMUX_PREFIX", "claude-")  # tmux session prefix for isolation

# BRIDGE_URL: primary hook target for remote workers, falls back to localhost:PORT
# User can set BRIDGE_URL=https://remote-bridge.example.com for distributed setups
_bridge_url_env = os.environ.get("BRIDGE_URL", "")
BRIDGE_URL = _bridge_url_env.rstrip("/") if _bridge_url_env else f"http://localhost:{PORT}"
PERSISTENCE_NOTE = "They'll stay on your team."

# Sandbox mode: run Claude Code in Docker container for isolation
# CLI flags: --sandbox, --sandbox-image, --mount, --mount-ro
# Default: mounts ~ to /workspace (rw)
SANDBOX_ENABLED = os.environ.get("SANDBOX_ENABLED", "0") == "1"
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "claudecode-telegram:latest")
# Extra mounts from CLI: list of (host_path, container_path, readonly)
# Parsed from SANDBOX_MOUNTS env var: "/host:/container,/path,ro:/secrets:/secrets"
SANDBOX_EXTRA_MOUNTS = []
_mounts_env = os.environ.get("SANDBOX_MOUNTS", "")
if _mounts_env:
    for mount_spec in _mounts_env.split(","):
        mount_spec = mount_spec.strip()
        if not mount_spec:
            continue
        readonly = mount_spec.startswith("ro:")
        if readonly:
            mount_spec = mount_spec[3:]
        if ":" in mount_spec:
            host, container = mount_spec.split(":", 1)
        else:
            host = container = mount_spec
        SANDBOX_EXTRA_MOUNTS.append((host, container, readonly))

# Derive node name from TMUX_PREFIX for per-node isolation in /tmp
# "claude-test-" -> "test", "claude-" -> "default"
_node_name = TMUX_PREFIX.strip("-").removeprefix("claude-") or "default"

# Temporary file inbox (session-isolated, auto-cleaned)
FILE_INBOX_ROOT = Path(f"/tmp/claudecode-telegram/{_node_name}")

# Worker pipe root for inter-worker communication
# Each worker gets a named pipe at WORKER_PIPE_ROOT/<name>/in.pipe
WORKER_PIPE_ROOT = Path(f"/tmp/claudecode-telegram/{_node_name}")

DEFAULT_BACKEND = "claude"
DEFAULT_WORKER_BACKEND = DEFAULT_BACKEND


# ============================================================
# CORE: Backend Protocol + implementations
# ============================================================

class Backend(Protocol):
    """Minimal backend interface. 3 methods, no more."""
    name: str
    is_exec: bool

    def start_cmd(self) -> str:
        """Return the shell command to start this CLI in tmux."""
        ...

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        """Send a message to the worker. Returns True if sent."""
        ...

    def is_online(self, tmux_name: str) -> bool:
        """Check if worker is alive and ready to receive messages."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Shared tmux helpers (used by multiple backends)
# ─────────────────────────────────────────────────────────────────────────────

# Per-session locks to prevent concurrent tmux sends from interleaving
_tmux_send_locks = {}
_tmux_send_locks_guard = threading.Lock()


def _get_tmux_send_lock(tmux_name: str):
    """Get or create a lock for a specific tmux session."""
    with _tmux_send_locks_guard:
        if tmux_name not in _tmux_send_locks:
            _tmux_send_locks[tmux_name] = threading.Lock()
        return _tmux_send_locks[tmux_name]


def tmux_exists(tmux_name: str) -> bool:
    """Check if tmux session exists."""
    return subprocess.run(
        ["tmux", "has-session", "-t", tmux_name],
        capture_output=True
    ).returncode == 0


def tmux_send_message(tmux_name: str, text: str) -> bool:
    """Send text + Enter to tmux session with locking."""
    lock = _get_tmux_send_lock(tmux_name)
    with lock:
        result = subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-l", text])
        if result.returncode != 0:
            return False
        time.sleep(0.2)  # Delay to let terminal process text before Enter
        result = subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])
        return result.returncode == 0


def get_pane_command(tmux_name: str) -> str:
    """Get the current command running in tmux pane."""
    result = subprocess.run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_command}"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def is_process_running(tmux_name: str, process_name: str) -> bool:
    """Check if a process is running in tmux session."""
    cmd = get_pane_command(tmux_name)
    if process_name.lower() in cmd.lower():
        return True

    result = subprocess.run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False

    pane_pid = result.stdout.strip()
    if not pane_pid:
        return False

    result = subprocess.run(
        ["pgrep", "-P", pane_pid, process_name],
        capture_output=True
    )
    return result.returncode == 0


def tmux_send_escape(tmux_name: str):
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Escape"])


class ClaudeBackend:
    """Claude Code CLI - interactive mode with hook for responses."""
    name = "claude"
    is_exec = False

    def start_cmd(self) -> str:
        return "claude --dangerously-skip-permissions"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        if not tmux_exists(tmux_name):
            return False
        return tmux_send_message(tmux_name, text)

    def is_online(self, tmux_name: str) -> bool:
        if not tmux_exists(tmux_name):
            return False
        return is_process_running(tmux_name, "claude")


class CodexBackend:
    """OpenAI Codex CLI - non-interactive exec mode."""
    name = "codex"
    is_exec = True

    def start_cmd(self) -> str:
        return "echo 'Codex worker ready (exec mode)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        adapter = Path(__file__).parent / "hooks" / "codex-tmux-adapter.py"
        if not adapter.exists():
            print(f"Codex adapter not found: {adapter}")
            return False

        subprocess.Popen(
            ["python3", str(adapter), worker_name, text, bridge_url, str(sessions_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True

    def is_online(self, tmux_name: str) -> bool:
        return True


class GeminiBackend:
    """Google Gemini CLI - non-interactive prompt mode (stub)."""
    name = "gemini"
    is_exec = True

    def start_cmd(self) -> str:
        return "echo 'Gemini worker ready (exec mode)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        adapter = Path(__file__).parent / "hooks" / "gemini-adapter.py"
        if not adapter.exists():
            print(f"Gemini adapter not found: {adapter}")
            return False

        subprocess.Popen(
            ["python3", str(adapter), worker_name, text, bridge_url, str(sessions_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True

    def is_online(self, tmux_name: str) -> bool:
        return True


class OpenCodeBackend:
    """OpenCode CLI - non-interactive run mode (stub)."""
    name = "opencode"
    is_exec = True

    def start_cmd(self) -> str:
        return "echo 'OpenCode worker ready (exec mode)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        adapter = Path(__file__).parent / "hooks" / "opencode-adapter.py"
        if not adapter.exists():
            print(f"OpenCode adapter not found: {adapter}")
            return False

        subprocess.Popen(
            ["python3", str(adapter), worker_name, text, bridge_url, str(sessions_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True

    def is_online(self, tmux_name: str) -> bool:
        return True


BACKENDS = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
    "gemini": GeminiBackend(),
    "opencode": OpenCodeBackend(),
}


def get_backend(name: str) -> Backend:
    return BACKENDS.get(name, BACKENDS[DEFAULT_BACKEND])


def is_valid_backend(name: str) -> bool:
    return name in BACKENDS


def list_backends() -> list[str]:
    return list(BACKENDS.keys())


def is_claude_running(tmux_name: str) -> bool:
    return is_process_running(tmux_name, "claude")


# In-memory state (RAM only, no persistence - tmux IS the persistence)
state = {
    "active": None,  # Currently active session name
    "startup_notified": False,  # Whether we've sent the startup message
}

# Security: Pre-set admin or auto-learn first user (RAM only, re-learns on restart)
ADMIN_CHAT_ID_ENV = os.environ.get("ADMIN_CHAT_ID", "")
admin_chat_id = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV else None

# Persistence files (in node directory, survives restart)
NODE_DIR = SESSIONS_DIR.parent  # ~/.claude/telegram/nodes/<node>
LAST_CHAT_ID_FILE = NODE_DIR / "last_chat_id"
LAST_ACTIVE_FILE = NODE_DIR / "last_active"

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


# ============================================================
# FILE PERSISTENCE
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Persistence (last chat ID and last active worker survive restart)
# ─────────────────────────────────────────────────────────────────────────────

def save_last_chat_id(chat_id):
    """Save last known chat ID to file for auto-notification on restart."""
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        LAST_CHAT_ID_FILE.write_text(str(chat_id))
        LAST_CHAT_ID_FILE.chmod(0o600)
    except Exception as e:
        print(f"Failed to save last_chat_id: {e}")


def load_last_chat_id():
    """Load last known chat ID from file."""
    try:
        if LAST_CHAT_ID_FILE.exists():
            chat_id = LAST_CHAT_ID_FILE.read_text().strip()
            if chat_id:
                return int(chat_id)
    except Exception as e:
        print(f"Failed to load last_chat_id: {e}")
    return None


def save_last_active(name):
    """Save last active worker name to file for auto-focus on restart."""
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        LAST_ACTIVE_FILE.write_text(name)
        LAST_ACTIVE_FILE.chmod(0o600)
    except Exception as e:
        print(f"Failed to save last_active: {e}")


def load_last_active():
    """Load last active worker name from file."""
    try:
        if LAST_ACTIVE_FILE.exists():
            name = LAST_ACTIVE_FILE.read_text().strip()
            if name:
                return name
    except Exception as e:
        print(f"Failed to load last_active: {e}")
    return None

# Reserved names that cannot be used as worker names (would clash with commands)
RESERVED_NAMES = {
    # Bridge commands
    "team", "focus", "progress", "learn", "pause", "relaunch", "settings", "hire", "end",
    # Special
    "all", "start", "help",
}


# ============================================================
# TELEGRAM API
# ============================================================

class TelegramAPI:
    def __init__(self, token: str):
        self.token = token

    def api(self, method: str, data: dict):
        if not self.token:
            return None
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"Telegram API error: {e}")
            return None

    def send_message(self, chat_id: int, text: str, **kwargs):
        payload = {"chat_id": chat_id, "text": text}
        payload.update(kwargs)
        return self.api("sendMessage", payload)

    def send_photo(self, chat_id: int, photo, **kwargs):
        payload = {"chat_id": chat_id, "photo": photo}
        payload.update(kwargs)
        return self.api("sendPhoto", payload)

    def send_document(self, chat_id: int, document, **kwargs):
        payload = {"chat_id": chat_id, "document": document}
        payload.update(kwargs)
        return self.api("sendDocument", payload)

    def set_reaction(self, chat_id: int, message_id: int, reaction: list[dict]):
        payload = {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}
        return self.api("setMessageReaction", payload)

    def send_chat_action(self, chat_id: int, action: str):
        return self.api("sendChatAction", {"chat_id": chat_id, "action": action})


telegram = TelegramAPI(BOT_TOKEN)


def telegram_api(method, data):
    return telegram.api(method, data)


# ============================================================
# MEDIA HANDLING
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Image Handling
# ─────────────────────────────────────────────────────────────────────────────

# Max file size: 20MB (Telegram limit)
MAX_FILE_SIZE = 20 * 1024 * 1024

# Allowed image extensions for outgoing
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# Allowed document extensions for outgoing (common code, docs, data files)
ALLOWED_DOC_EXTENSIONS = {
    # Docs
    ".md", ".txt", ".rst", ".pdf",
    # Data
    ".json", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    ".log", ".sql", ".patch", ".diff",
    # Code
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
    ".sh", ".html", ".css", ".scss",
    # Archives
    ".zip", ".tar", ".gz",
}

# Blocked extensions (secrets, keys, certificates)
BLOCKED_DOC_EXTENSIONS = {
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
    ".jks", ".keystore", ".kdb", ".pgp", ".gpg", ".asc",
}

# Blocked filenames (case-insensitive)
BLOCKED_FILENAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_dsa", "credentials", "kubeconfig",
}


def format_file_size(size_bytes):
    """Format file size in human-readable form."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def get_inbox_dir(session_name):
    """Get inbox directory for incoming files (images, documents, etc.).

    Uses /tmp for ephemeral storage, session-namespaced to prevent cross-session access.
    """
    return FILE_INBOX_ROOT / session_name / "inbox"


def ensure_inbox_dir(session_name):
    """Create inbox directory with secure permissions."""
    inbox = get_inbox_dir(session_name)
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    inbox.chmod(0o700)
    return inbox


def cleanup_inbox(session_name):
    """Clean up all files in a session's inbox."""
    inbox = get_inbox_dir(session_name)
    if inbox.exists():
        for f in inbox.iterdir():
            try:
                f.unlink()
            except Exception as e:
                print(f"Failed to delete {f}: {e}")


# ============================================================
# INTER-WORKER PIPES
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Worker Pipe Functions (inter-worker communication)
# ─────────────────────────────────────────────────────────────────────────────

def get_worker_pipe_path(name):
    """Get the named pipe path for a worker.

    Path: /tmp/claudecode-telegram/<node>/<worker>/in.pipe
    """
    return WORKER_PIPE_ROOT / name / "in.pipe"


def ensure_worker_pipe(name):
    """Create the named pipe for a worker if it doesn't exist.

    Creates: /tmp/claudecode-telegram/<node>/<worker>/in.pipe
    Also starts a reader thread to forward messages to the worker.
    """
    pipe_path = get_worker_pipe_path(name)
    pipe_dir = pipe_path.parent

    # Create directory with secure permissions
    pipe_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pipe_dir.chmod(0o700)

    # Create FIFO (named pipe) if it doesn't exist
    if not pipe_path.exists():
        os.mkfifo(str(pipe_path), mode=0o600)
        print(f"Created worker pipe: {pipe_path}")

    # Start the pipe reader thread to forward messages to worker
    start_pipe_reader(name)

    return pipe_path


def cleanup_worker_pipe(name):
    """Remove the named pipe for a worker."""
    # Stop the pipe reader thread first
    stop_pipe_reader(name)

    pipe_path = get_worker_pipe_path(name)

    if pipe_path.exists():
        try:
            pipe_path.unlink()
            print(f"Removed worker pipe: {pipe_path}")
        except Exception as e:
            print(f"Failed to remove worker pipe {pipe_path}: {e}")

    # Also try to remove parent directory if empty
    pipe_dir = pipe_path.parent
    if pipe_dir.exists():
        try:
            pipe_dir.rmdir()
        except OSError:
            pass  # Directory not empty, that's OK


# ─────────────────────────────────────────────────────────────────────────────
# Pipe Reader Threads (for inter-worker communication)
# ─────────────────────────────────────────────────────────────────────────────

# Dict to track pipe reader threads: name -> (thread, stop_event)
_pipe_reader_threads: Dict[str, tuple] = {}


def pipe_reader_loop(name: str, stop_event: threading.Event):
    """Background thread that reads messages from a worker's input pipe.

    When another worker writes to this worker's pipe:
      echo "message" > /tmp/claudecode-telegram/<node>/bob/in.pipe

    This thread reads the message and forwards it to the worker's backend.

    The reader uses blocking open() - this means the thread will block until
    a writer opens the pipe. This is correct behavior for FIFOs. When the
    writer closes, we get EOF, close our end, and re-open to wait for the
    next writer.
    """
    pipe_path = get_worker_pipe_path(name)
    print(f"Pipe reader started for worker '{name}' at {pipe_path}")

    while not stop_event.is_set():
        try:
            # Check if we should stop before blocking on open
            if stop_event.is_set():
                break

            # Open pipe for reading (blocks until a writer connects)
            # Use regular open() which blocks - this is the correct way to read FIFOs
            with open(str(pipe_path), 'r') as pipe:
                # Read until EOF (writer closes their end)
                while not stop_event.is_set():
                    line = pipe.readline()
                    if not line:
                        # EOF - writer closed, break to re-open
                        break

                    message = line.strip()
                    if message:
                        print(f"Pipe message for '{name}': {message[:100]}{'...' if len(message) > 100 else ''}")
                        # Forward to worker using backend routing
                        try:
                            _forward_pipe_message(name, message)
                        except Exception as e:
                            print(f"Error forwarding pipe message to '{name}': {e}")

        except FileNotFoundError:
            # Pipe was removed, stop the reader
            print(f"Pipe for '{name}' no longer exists, stopping reader")
            break
        except OSError as e:
            if stop_event.is_set():
                break
            print(f"Pipe reader error for '{name}': {e}")
            # Wait a bit before retrying
            stop_event.wait(0.5)

    # Clean up registry so start_pipe_reader can restart if needed
    if name in _pipe_reader_threads:
        _pipe_reader_threads.pop(name, None)
    print(f"Pipe reader stopped for worker '{name}'")


def _forward_pipe_message(name: str, message: str):
    """Forward a message from the pipe to the worker's session.

    Uses backend routing for tmux or exec-mode workers.
    """
    if not worker_manager.send(name, message):
        print(f"Warning: Cannot forward pipe message to '{name}' - worker not found")


def start_pipe_reader(name: str):
    """Start a background thread to read from the worker's input pipe."""
    if name in _pipe_reader_threads:
        # Already running
        return

    pipe_path = get_worker_pipe_path(name)
    if not pipe_path.exists():
        print(f"Cannot start pipe reader: pipe does not exist for '{name}'")
        return

    stop_event = threading.Event()
    thread = threading.Thread(
        target=pipe_reader_loop,
        args=(name, stop_event),
        daemon=True,
        name=f"pipe-reader-{name}"
    )
    _pipe_reader_threads[name] = (thread, stop_event)
    thread.start()
    print(f"Started pipe reader thread for '{name}'")


def stop_pipe_reader(name: str):
    """Stop the pipe reader thread for a worker."""
    if name not in _pipe_reader_threads:
        return

    thread, stop_event = _pipe_reader_threads.pop(name)
    stop_event.set()

    # Write a dummy byte to unblock the reader if it's waiting
    pipe_path = get_worker_pipe_path(name)
    if pipe_path.exists():
        try:
            # Open in non-blocking write mode to unblock reader
            fd = os.open(str(pipe_path), os.O_WRONLY | os.O_NONBLOCK)
            os.write(fd, b"\n")
            os.close(fd)
        except OSError:
            pass  # Pipe may already be closed

    # Wait for thread to finish (with timeout)
    thread.join(timeout=1.0)
    if thread.is_alive():
        print(f"Warning: pipe reader thread for '{name}' did not stop gracefully")


def get_workers():
    """Get all active workers with their communication details."""
    _sync_worker_manager()
    return worker_manager.get_workers()


def download_telegram_file(file_id, session_name):
    """Download a file from Telegram to the session's inbox.

    Returns the local file path or None on failure.
    SECURITY: Files are sandboxed in session's inbox directory.
    """
    if not BOT_TOKEN:
        return None

    # Get file info from Telegram
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            data=json.dumps({"file_id": file_id}).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
            if not result.get("ok"):
                print(f"getFile failed: {result}")
                return None
            file_info = result.get("result", {})
    except Exception as e:
        print(f"getFile error: {e}")
        return None

    file_path = file_info.get("file_path")
    file_size = file_info.get("file_size", 0)

    if not file_path:
        print("No file_path in response")
        return None

    # Check file size
    if file_size > MAX_FILE_SIZE:
        print(f"File too large: {file_size} > {MAX_FILE_SIZE}")
        return None

    # Download the file
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    inbox = ensure_inbox_dir(session_name)

    # Generate unique filename with original extension
    ext = Path(file_path).suffix or ""
    local_filename = f"{uuid.uuid4().hex}{ext}"
    local_path = inbox / local_filename

    try:
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, timeout=60) as r:
            content = r.read()
            if len(content) > MAX_FILE_SIZE:
                print(f"Downloaded file too large: {len(content)}")
                return None
            local_path.write_bytes(content)
            local_path.chmod(0o600)
        print(f"Downloaded file: {local_path}")
        return str(local_path)
    except Exception as e:
        print(f"Download error: {e}")
        return None


def validate_photo_path(photo_path):
    """Validate a photo path. Returns (ok, Path or error string)."""
    photo_path = Path(photo_path)

    if not photo_path.exists():
        return False, f"Photo not found: {photo_path}"

    if not photo_path.is_file():
        return False, f"Not a file: {photo_path}"

    # Check extension
    if photo_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
        return False, f"Invalid image extension: {photo_path.suffix}"

    # Check size
    file_size = photo_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        return False, f"Photo too large: {file_size} > {MAX_FILE_SIZE}"

    # Security: path must be within allowed directories
    # Allow: /tmp (includes image inbox), sessions dir, and current working directory
    allowed_roots = [
        Path("/tmp"),
        SESSIONS_DIR,
        Path.cwd(),
    ]
    photo_resolved = photo_path.resolve()
    is_allowed = any(
        str(photo_resolved).startswith(str(root.resolve()))
        for root in allowed_roots
    )
    if not is_allowed:
        return False, f"Photo path not in allowed directory: {photo_path}"

    return True, photo_path


def send_photo(chat_id, photo_path, caption=None):
    """Send a photo to Telegram using multipart/form-data.

    SECURITY: Path is validated before sending.
    Returns True on success, False on failure.
    """
    if not BOT_TOKEN:
        return False

    ok, validated = validate_photo_path(photo_path)
    if not ok:
        print(validated)
        return False

    photo_path = validated

    # Build multipart form data
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(str(photo_path))[0] or "image/jpeg"

    body_parts = []

    # chat_id field
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
    body_parts.append(b"")
    body_parts.append(str(chat_id).encode())

    # photo field
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"'.encode())
    body_parts.append(f"Content-Type: {content_type}".encode())
    body_parts.append(b"")
    body_parts.append(photo_path.read_bytes())

    # caption field (optional)
    if caption:
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="caption"')
        body_parts.append(b"")
        body_parts.append(caption.encode())

    body_parts.append(f"--{boundary}--".encode())
    body_parts.append(b"")

    body = b"\r\n".join(body_parts)

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                print(f"Photo sent: {photo_path.name}")
                return True
            else:
                print(f"sendPhoto failed: {result}")
                return False
    except Exception as e:
        print(f"sendPhoto error: {e}")
        return False


def is_blocked_filename(filename):
    """Check if filename matches blocked patterns (secrets, credentials, etc.)."""
    name_lower = filename.lower()
    # Check exact filename matches
    if name_lower in BLOCKED_FILENAMES:
        return True
    # Check .env.* pattern
    if name_lower.startswith(".env"):
        return True
    return False


def validate_document_path(doc_path):
    """Validate a document path. Returns (ok, Path or error string)."""
    doc_path = Path(doc_path)

    # Security: validate path exists and is regular file
    if not doc_path.exists():
        return False, f"Document not found: {doc_path}"

    if not doc_path.is_file():
        return False, f"Not a file: {doc_path}"

    # Security: check for blocked extensions (sensitive)
    ext_lower = doc_path.suffix.lower()
    if ext_lower in BLOCKED_DOC_EXTENSIONS:
        return False, f"Blocked extension (sensitive): {doc_path.suffix}"

    # Check extension is in allowlist
    if ext_lower not in ALLOWED_DOC_EXTENSIONS:
        return False, f"Extension not allowed: {doc_path.suffix}"

    # Security: check for blocked filenames
    if is_blocked_filename(doc_path.name):
        return False, f"Blocked filename (sensitive): {doc_path.name}"

    # Check size
    file_size = doc_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        return False, f"Document too large: {file_size} > {MAX_FILE_SIZE}"

    # Note: No path restriction - workers can send from anywhere
    # Security is enforced via extension allowlist and filename blocklist

    return True, doc_path


def send_document(chat_id, doc_path, caption=None):
    """Send a document to Telegram using multipart/form-data.

    SECURITY: Path and filename are validated before sending.
    Returns True on success, False on failure.
    """
    if not BOT_TOKEN:
        return False

    ok, validated = validate_document_path(doc_path)
    if not ok:
        print(validated)
        return False

    doc_path = validated

    # Build multipart form data
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(str(doc_path))[0] or "application/octet-stream"

    body_parts = []

    # chat_id field
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
    body_parts.append(b"")
    body_parts.append(str(chat_id).encode())

    # document field
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(f'Content-Disposition: form-data; name="document"; filename="{doc_path.name}"'.encode())
    body_parts.append(f"Content-Type: {content_type}".encode())
    body_parts.append(b"")
    body_parts.append(doc_path.read_bytes())

    # caption field (optional)
    if caption:
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="caption"')
        body_parts.append(b"")
        body_parts.append(caption.encode())

    body_parts.append(f"--{boundary}--".encode())
    body_parts.append(b"")

    body = b"\r\n".join(body_parts)

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                print(f"Document sent: {doc_path.name}")
                return True
            else:
                print(f"sendDocument failed: {result}")
                return False
    except Exception as e:
        print(f"sendDocument error: {e}")
        return False


# ============================================================
# MESSAGE FORMATTING
# ============================================================

CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _split_protected_segments(text, pattern):
    """Split text into (segment, is_protected) based on regex matches."""
    segments = []
    last = 0
    for match in pattern.finditer(text):
        if match.start() > last:
            segments.append((text[last:match.start()], False))
        segments.append((match.group(0), True))
        last = match.end()
    if last < len(text):
        segments.append((text[last:], False))
    return segments


def _collapse_excess_newlines(text):
    """Collapse 3+ newlines to 2, but avoid touching code blocks and inline code."""
    output = []
    for segment, protected in _split_protected_segments(text, CODE_FENCE_RE):
        if protected:
            output.append(segment)
            continue
        for inline_segment, inline_protected in _split_protected_segments(segment, INLINE_CODE_RE):
            if inline_protected:
                output.append(inline_segment)
            else:
                output.append(re.sub(r"\n{3,}", "\n\n", inline_segment))
    return "".join(output)


def _parse_media_tags(text, tag_name, validate_func):
    """Parse media tags, skipping escaped tags and code spans.

    Returns (clean_text, [(path, caption), ...]).
    """
    pattern = re.compile(rf"(\\)?\[\[{tag_name}:([^\]|]+)(?:\|([^\]]*))?\]\]")
    items = []
    removed = 0

    def replace_tag(match):
        nonlocal removed
        if match.group(1):
            # Escaped tag, return without the escape slash.
            return match.group(0)[1:]
        path = match.group(2).strip()
        caption = (match.group(3) or "").strip()
        ok, _ = validate_func(path)
        if ok:
            items.append((path, caption))
            removed += 1
            return ""
        return match.group(0)

    output = []
    for segment, protected in _split_protected_segments(text, CODE_FENCE_RE):
        if protected:
            output.append(segment)
            continue
        for inline_segment, inline_protected in _split_protected_segments(segment, INLINE_CODE_RE):
            if inline_protected:
                output.append(inline_segment)
            else:
                output.append(pattern.sub(replace_tag, inline_segment))

    clean_text = "".join(output)
    if removed:
        clean_text = _collapse_excess_newlines(clean_text).strip()
    return clean_text, items


def parse_image_tags(text):
    """Parse [[image:/path|caption]] tags from text.

    Returns (clean_text, [(path, caption), ...])
    """
    return _parse_media_tags(text, "image", validate_photo_path)


def parse_file_tags(text):
    """Parse [[file:/path|caption]] tags from text.

    Returns (clean_text, [(path, caption), ...])
    """
    return _parse_media_tags(text, "file", validate_document_path)


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram's HTML parse mode.

    Must escape &, <, > to prevent Telegram from interpreting them as HTML tags.
    """
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def format_response_text(session_name, text):
    """Format response with session prefix. No escaping - Claude Code handles safety."""
    return f"<b>{session_name}:</b>\n{text}"


# ─────────────────────────────────────────────────────────────────────────────
# Message Splitting (Telegram 4096 char limit)
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_MAX_LENGTH = 4096


def split_message(text, max_len=TELEGRAM_MAX_LENGTH):
    """Split text into chunks that fit within Telegram's message limit.

    Splits on safe boundaries: blank lines → newlines → spaces → hard cut.
    Returns list of text chunks.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Find best split point within max_len
        split_at = find_split_point(remaining, max_len)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


def find_split_point(text, max_len):
    """Find the best point to split text within max_len.

    Priority: blank line → newline → space → hard cut.
    """
    search_area = text[:max_len]

    # Try blank line (paragraph break)
    pos = search_area.rfind('\n\n')
    if pos > max_len // 2:  # Only use if reasonably far into text
        return pos + 1  # Include one newline in current chunk

    # Try newline
    pos = search_area.rfind('\n')
    if pos > max_len // 2:
        return pos + 1

    # Try space
    pos = search_area.rfind(' ')
    if pos > max_len // 2:
        return pos + 1

    # Hard cut at max_len
    return max_len


def format_multipart_messages(session_name, chunks):
    """Format chunks with session prefix (all chunks get prefix, no part numbers).

    Single chunk: "<b>name:</b>\ntext"
    Multiple chunks: "<b>name:</b>\ntext" (same format, no 1/3, 2/3 etc)
    """
    return [format_response_text(session_name, chunk) for chunk in chunks]


def setup_bot_commands():
    """Initial bot commands setup."""
    update_bot_commands()


def update_bot_commands():
    """Update bot commands including dynamic worker shortcuts."""
    commands = list(BOT_COMMANDS)  # Copy static commands

    # Add worker shortcuts (e.g., /lee, /chen)
    registered = get_registered_sessions()
    for name in sorted(registered.keys()):
        commands.append({"command": name, "description": f"Message {name}"})

    result = telegram_api("setMyCommands", {"commands": commands})
    if result and result.get("ok"):
        worker_count = len(registered)
        print(f"Bot commands updated ({len(BOT_COMMANDS)} + {worker_count} workers)")


# ============================================================
# TMUX SESSION MANAGEMENT
# ============================================================

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


# ─────────────────────────────────────────────────────────────────────────────
# Worker Backend Helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalize_backend(backend: Optional[str]) -> str:
    """Return a normalized backend name with a safe default."""
    return backend or DEFAULT_BACKEND


def parse_hire_args(raw: str) -> tuple[str, str]:
    """Parse /hire arguments and return (name, backend).

    Supports:
    - /hire alice                    -> (alice, claude)
    - /hire alice --backend codex    -> (alice, codex)
    - /hire alice --codex            -> (alice, codex)  [legacy]
    - /hire codex-alice              -> (alice, codex)  [prefix syntax]
    """
    parts = [p for p in (raw or "").split() if p]
    backend = DEFAULT_BACKEND
    name_parts = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--backend" and i + 1 < len(parts):
            backend = parts[i + 1]
            i += 2
            continue
        elif part == "--codex":
            # Legacy support
            backend = "codex"
        elif part.startswith("--"):
            # Skip unknown flags
            pass
        else:
            name_parts.append(part)
        i += 1

    if len(name_parts) != 1:
        return "", backend

    name = name_parts[0]

    # Check for backend prefix syntax (e.g., codex-alice, gemini-bob)
    for backend_name in list_backends():
        prefix = f"{backend_name}-"
        if name.startswith(prefix):
            backend = backend_name
            name = name[len(prefix):]
            break

    # Validate backend
    if not is_valid_backend(backend):
        # Return invalid backend so caller can show error
        return name, backend

    return name, backend


def format_team_lines(registered: dict, active: Optional[str], pending_lookup=None) -> list[str]:
    """Format /team response lines (backend-aware)."""
    if pending_lookup is None:
        pending_lookup = is_pending

    lines = []
    lines.append("Your team:")
    lines.append(f"Focused: {active or '(none)'}")
    lines.append("Workers:")
    for name in sorted(registered.keys()):
        session = registered[name]
        backend = normalize_backend(session.get("backend"))
        status = []
        if name == active:
            status.append("focused")
        status.append("working" if pending_lookup(name) else "available")
        status.append(f"backend={backend}")
        lines.append(f"- {name} ({', '.join(status)})")
    return lines


def format_progress_lines(
    name: str,
    pending: bool,
    backend: str,
    online: bool,
    ready: bool,
    mode: str,
    needs_attention: Optional[str] = None
) -> list[str]:
    """Format /progress response lines (backend-aware)."""
    status = []
    status.append(f"Progress for focused worker: {name}")
    status.append("Focused: yes")
    status.append(f"Working: {'yes' if pending else 'no'}")
    status.append(f"Backend: {backend}")
    status.append(f"Online: {'yes' if online else 'no'}")
    status.append(f"Ready: {'yes' if ready else 'no'}")
    if needs_attention:
        status.append(f"Needs attention: {needs_attention}")
    status.append(f"Mode: {mode}")
    return status


def get_worker_backend(name: str, session: Optional[dict] = None) -> str:
    """Get backend for a worker."""
    # Check session dict first
    if session and session.get("backend"):
        return normalize_backend(session.get("backend"))
    # Check backend file in session dir (for exec mode workers)
    backend_file = SESSIONS_DIR / name / "backend"
    if backend_file.exists():
        return normalize_backend(backend_file.read_text().strip())
    return DEFAULT_BACKEND


# ─────────────────────────────────────────────────────────────────────────────
# CORE: WorkerManager
# ─────────────────────────────────────────────────────────────────────────────

class WorkerManager:
    def __init__(self, sessions_dir: Path, tmux_prefix: str):
        self.sessions_dir = sessions_dir
        self.tmux_prefix = tmux_prefix

    def _sync_paths(self):
        if self.sessions_dir != SESSIONS_DIR:
            self.sessions_dir = SESSIONS_DIR
        if self.tmux_prefix != TMUX_PREFIX:
            self.tmux_prefix = TMUX_PREFIX

    def scan_tmux_sessions(self):
        """Scan tmux for claude-* sessions (registered)."""
        self._sync_paths()
        registered = {}

        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                return registered

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                session_name = line.strip()

                if session_name.startswith(self.tmux_prefix):
                    name = session_name[len(self.tmux_prefix):]
                    backend = normalize_backend(get_tmux_env_value(session_name, "WORKER_BACKEND"))
                    registered[name] = {"tmux": session_name, "backend": backend}
        except Exception as e:
            print(f"Error scanning tmux: {e}")

        return registered

    def get_registered_sessions(self, registered=None):
        """Get registered sessions from tmux and exec-mode workers."""
        self._sync_paths()
        if registered is None:
            registered = self.scan_tmux_sessions()

        # Add exec-mode workers (have backend file but no tmux session)
        if self.sessions_dir.exists():
            for session_dir in self.sessions_dir.iterdir():
                if session_dir.is_dir():
                    backend_file = session_dir / "backend"
                    if backend_file.exists():
                        name = session_dir.name
                        if name not in registered:
                            backend = backend_file.read_text().strip()
                            registered[name] = {"backend": backend, "mode": "exec"}

        if state["active"] and state["active"] not in registered:
            state["active"] = None
        if registered and not state["active"]:
            state["active"] = list(registered.keys())[0]

        return registered

    def is_online(self, name: str, session: dict = None) -> bool:
        """Check if worker is online and ready."""
        self._sync_paths()
        if not session:
            sessions = self.get_registered_sessions()
            session = sessions.get(name)
        if not session:
            return False

        backend_name = normalize_backend(session.get("backend"))
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        return backend.is_online(tmux_name)

    def send(self, name: str, message: str, chat_id: int = None, session: dict = None) -> bool:
        """Send message to worker using backend registry."""
        self._sync_paths()
        if not session:
            sessions = self.get_registered_sessions()
            session = sessions.get(name)
        if not session:
            return False

        backend_name = normalize_backend(session.get("backend"))
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        return backend.send(name, tmux_name, message, BRIDGE_URL, self.sessions_dir)

    def get_workers(self):
        """Get all active workers with their communication details."""
        self._sync_paths()
        workers = []
        registered = self.get_registered_sessions()
        for name, info in registered.items():
            backend_name = get_worker_backend(name, info)
            backend = get_backend(backend_name)
            if backend.is_exec:
                pipe_path = ensure_worker_pipe(name)
                workers.append({
                    "name": name,
                    "protocol": "pipe",
                    "address": str(pipe_path),
                    "send_example": f"echo 'your message here' > {pipe_path}"
                })
            else:
                tmux_name = info.get("tmux")
                if not tmux_name:
                    continue
                workers.append({
                    "name": name,
                    "protocol": "tmux",
                    "address": tmux_name,
                    "send_example": f"tmux send-keys -t {tmux_name} 'your message here' Enter"
                })
        return workers

    def hire(self, name: str, backend: str = DEFAULT_BACKEND, chat_id: int = None):
        """Create a new worker instance."""
        self._sync_paths()
        if not is_valid_backend(backend):
            return False, f"Unknown backend '{backend}'. Available: {', '.join(list_backends())}"

        backend_obj = get_backend(backend)

        # Exec-mode backends: stateless, no tmux
        if backend_obj.is_exec:
            state["active"] = name
            save_last_active(name)
            ensure_session_dir(name)
            ensure_worker_pipe(name)
            backend_file = self.sessions_dir / name / "backend"
            backend_file.write_text(backend)
            # Write chat_id BEFORE sending welcome so /response can find it
            if chat_id:
                set_pending(name, chat_id)
            # Send welcome message as first message to exec worker
            welcome = (
                "You are connected to Telegram via claudecode-telegram bridge. "
                f"Your bridge URL is {BRIDGE_URL}. "
                f"To message other workers: curl {BRIDGE_URL}/workers to discover workers and their protocols. "
                "Use their protocol directly (tmux send-keys or pipe) - do NOT output normally or it goes to Telegram."
            )
            self.send(name, welcome)
            print(f"Created {backend} worker '{name}' (exec mode)")
            return True, None

        tmux_name = f"{self.tmux_prefix}{name}"
        if tmux_exists(tmux_name):
            return False, f"Worker '{name}' already exists"

        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True
        )
        if result.returncode != 0:
            return False, "Could not start the worker workspace"

        time.sleep(0.5)

        export_hook_env(tmux_name, backend)
        time.sleep(0.3)

        if SANDBOX_ENABLED:
            docker_cmd = get_docker_run_cmd(name)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
            print(f"Started worker '{name}' in sandbox mode")
        else:
            start_cmd = backend_obj.start_cmd()
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])
            time.sleep(1.5)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "2"])
            time.sleep(0.3)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])

        time.sleep(2.0 if not SANDBOX_ENABLED else 5.0)
        welcome = (
            "You are connected to Telegram via claudecode-telegram bridge. "
            "Manager can send you files (images, PDFs, documents) - they'll appear as local paths. "
            "To send files back: [[file:/path/to/doc.pdf|caption]] or [[image:/path/to/img.png|caption]]. "
            "Allowed paths: /tmp, current directory. "
            f"To message other workers: curl {BRIDGE_URL}/workers to discover workers and their protocols. "
            "Use their protocol directly (tmux send-keys or pipe) - do NOT output normally or it goes to Telegram."
        )
        if SANDBOX_ENABLED:
            welcome += " Running in sandbox mode (Docker container)."
        self.send(name, welcome)

        state["active"] = name
        save_last_active(name)
        ensure_session_dir(name)
        ensure_worker_pipe(name)

        return True, None

    def end(self, name: str):
        """Kill a worker instance."""
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered:
            return False, f"Worker '{name}' not found"

        session = registered[name]
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)

        if backend.is_exec:
            session_dir = self.sessions_dir / name
            backend_file = session_dir / "backend"
            try:
                if backend_file.exists():
                    backend_file.unlink()
                for session_id_file in session_dir.glob("*_session_id"):
                    session_id_file.unlink()
            except Exception as e:
                return False, f"Failed to clean exec metadata: {e}"

            cleanup_inbox(name)
            cleanup_worker_pipe(name)
            clear_pending(name)

            if state["active"] == name:
                state["active"] = None
                self.get_registered_sessions()

            return True, None

        tmux_name = session["tmux"]

        if SANDBOX_ENABLED:
            stop_docker_container(name)

        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        cleanup_inbox(name)
        cleanup_worker_pipe(name)

        if state["active"] == name:
            state["active"] = None
            self.get_registered_sessions()

        return True, None

    def restart(self, name: str):
        """Restart claude in an existing tmux session."""
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered:
            return False, f"Worker '{name}' not found"

        session = registered[name]
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)

        if backend.is_exec:
            session_dir = self.sessions_dir / name
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()
            ensure_worker_pipe(name)
            clear_pending(name)
            return True, None

        tmux_name = session["tmux"]
        if not tmux_exists(tmux_name):
            return False, "Worker workspace is not running"
        if is_claude_running(tmux_name):
            return False, "Worker is already running"

        export_hook_env(tmux_name, backend_name)
        time.sleep(0.3)

        if SANDBOX_ENABLED:
            stop_docker_container(name)
            time.sleep(0.5)
            docker_cmd = get_docker_run_cmd(name)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        else:
            start_cmd = backend.start_cmd()
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])

        return True, None


worker_manager = WorkerManager(SESSIONS_DIR, TMUX_PREFIX)


def _sync_worker_manager():
    worker_manager.sessions_dir = SESSIONS_DIR
    worker_manager.tmux_prefix = TMUX_PREFIX

# ─────────────────────────────────────────────────────────────────────────────
# grug say: one place for backend branching. no scatter.
# Worker Helpers (centralize backend switching)
# ─────────────────────────────────────────────────────────────────────────────

def worker_is_online(name: str, session: dict = None) -> bool:
    """Check if worker is online and ready.

    Args:
        name: Worker name
        session: Session dict from get_registered_sessions() (optional, avoids re-lookup)
    """
    _sync_worker_manager()
    return worker_manager.is_online(name, session)


def worker_set_pending(name: str, chat_id: int):
    """Set pending state for worker."""
    set_pending(name, chat_id)


def worker_send(name: str, message: str, chat_id: int = None, session: dict = None) -> bool:
    """Send message to worker using backend registry.

    Args:
        name: Worker name
        message: Message text to send
        chat_id: Chat ID (unused, kept for compatibility)
        session: Session dict (optional, avoids re-lookup)

    Returns:
        True if send succeeded
    """
    _sync_worker_manager()
    return worker_manager.send(name, message, chat_id, session)


def get_tmux_env_value(tmux_name: str, key: str) -> str:
    """Get a tmux session environment variable value."""
    result = subprocess.run(
        ["tmux", "show-environment", "-t", tmux_name, key],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if "=" not in value:
        return ""
    return value.split("=", 1)[1]


def scan_tmux_sessions():
    """Scan tmux for registered sessions."""
    _sync_worker_manager()
    return worker_manager.scan_tmux_sessions()


def get_registered_sessions(registered=None):
    """Get registered sessions from tmux and exec-mode workers."""
    _sync_worker_manager()
    return worker_manager.get_registered_sessions(registered)


def tmux_prompt_empty(tmux_name, timeout=0.5):
    """Check if Claude Code's input prompt is empty (message was accepted).

    After sending a message, polls the tmux pane to verify the prompt
    line (❯) is empty, indicating Claude accepted the input.

    Returns True if prompt is empty within timeout, False otherwise.
    """
    import re
    start = time.time()
    while time.time() - start < timeout:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            # Check for empty prompt: line starting with ❯ followed by only whitespace
            if re.search(r'^❯\s*$', result.stdout, re.MULTILINE):
                return True
        time.sleep(0.1)
    return False


def export_hook_env(tmux_name, backend: str = DEFAULT_WORKER_BACKEND):
    """Export env vars for hook inside tmux session.

    Uses tmux set-environment which persists in session and survives restarts.
    Hook reads these via `tmux show-environment -t $SESSION_NAME`.
    """
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)])
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX])
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", str(SESSIONS_DIR)])
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "WORKER_BACKEND", normalize_backend(backend)])
    # Always export BRIDGE_URL so workers know where their bridge is
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", BRIDGE_URL])


def get_docker_run_cmd(name):
    """Build docker run command for sandbox mode.

    Default: mounts ~ to /workspace (rw)
    Extra mounts via SANDBOX_EXTRA_MOUNTS (from --mount/--mount-ro flags)

    Args:
        name: Worker name (used for container name)

    Returns:
        Command string to run in tmux
    """
    import platform
    container_name = f"claude-worker-{name}"
    home = Path.home()

    # Base command
    cmd_parts = [
        "docker", "run", "-it",
        f"--name={container_name}",
        "--rm",  # Clean up on exit
    ]

    # Host gateway for bridge communication
    if platform.system() == "Linux":
        cmd_parts.append("--add-host=host.docker.internal:host-gateway")

    # Default mount: ~ → /workspace (rw)
    cmd_parts.append(f"-v={home}:/workspace")

    # Extra mounts from --mount/--mount-ro flags
    for host_path, container_path, readonly in SANDBOX_EXTRA_MOUNTS:
        if readonly:
            cmd_parts.append(f"-v={host_path}:{container_path}:ro")
        else:
            cmd_parts.append(f"-v={host_path}:{container_path}")

    # Mount session files for hook coordination
    cmd_parts.append(f"-v={SESSIONS_DIR}:{SESSIONS_DIR}")

    # Mount temp for file inbox
    FILE_INBOX_ROOT.mkdir(parents=True, exist_ok=True)
    cmd_parts.append(f"-v={FILE_INBOX_ROOT}:{FILE_INBOX_ROOT}")

    # Environment variables for hook
    # Use global BRIDGE_URL if user-provided, otherwise default to host.docker.internal for Docker
    if _bridge_url_env:
        docker_bridge_url = BRIDGE_URL  # User-provided takes precedence
    else:
        docker_bridge_url = f"http://host.docker.internal:{PORT}"
    cmd_parts.extend([
        f"-e=BRIDGE_URL={docker_bridge_url}",
        f"-e=PORT={PORT}",
        f"-e=TMUX_PREFIX={TMUX_PREFIX}",
        f"-e=SESSIONS_DIR={SESSIONS_DIR}",
        f"-e=BRIDGE_SESSION={name}",  # Session name for hook (tmux unavailable inside container)
        "-e=TMUX_FALLBACK=1",
    ])

    # Working directory
    cmd_parts.extend(["-w", "/workspace"])

    # Image
    cmd_parts.append(SANDBOX_IMAGE)

    # Run claude with --dangerously-skip-permissions (same as non-sandbox)
    cmd_parts.append("claude --dangerously-skip-permissions")

    return " ".join(cmd_parts)


def stop_docker_container(name):
    """Stop and remove a docker container."""
    container_name = f"claude-worker-{name}"
    subprocess.run(["docker", "stop", container_name], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


def send_to_worker(name: str, message: str, chat_id: Optional[int] = None) -> bool:
    """Send a message to a worker using the appropriate backend."""
    _sync_worker_manager()
    return worker_manager.send(name, message, chat_id)


def send_response_to_telegram(name: str, text: str, chat_id: int, escape: bool = True, log_prefix: str = "Response"):
    """Send a response to Telegram. Shared by hook responses.

    Args:
        name: Session/worker name for message prefix
        text: Response text (may contain image/file tags)
        chat_id: Telegram chat ID
        escape: If True, escape HTML special chars. If False, text is pre-escaped.
        log_prefix: Prefix for log messages (e.g., "Response", "Hook response")
    """
    # Parse image and file tags from text (before escaping to preserve tag syntax)
    clean_text, images = parse_image_tags(text)
    clean_text, files = parse_file_tags(clean_text)
    if escape:
        clean_text = escape_html(clean_text)

    # Send text message if there's text content
    if clean_text:
        prefix_reserve = len(name) + 30
        chunks = split_message(clean_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
        formatted_parts = format_multipart_messages(name, chunks)

        prev_msg_id = None
        for i, part in enumerate(formatted_parts):
            msg_data = {
                "chat_id": chat_id,
                "text": part,
                "parse_mode": "HTML"
            }
            if prev_msg_id:
                msg_data["reply_to_message_id"] = prev_msg_id

            result = telegram_api("sendMessage", msg_data)
            if result and result.get("ok"):
                prev_msg_id = result.get("result", {}).get("message_id")
                if len(formatted_parts) > 1:
                    print(f"{log_prefix} sent: {name} part {i+1}/{len(formatted_parts)} -> Telegram OK")
                else:
                    print(f"{log_prefix} sent: {name} -> Telegram OK")
            else:
                print(f"{log_prefix} failed: {name} -> {result}")

            if i < len(formatted_parts) - 1:
                time.sleep(0.05)

    # Send images
    for img_path, img_caption in images:
        full_caption = f"{name}: {img_caption}" if img_caption else f"{name}:"
        if send_photo(chat_id, img_path, full_caption):
            print(f"Image sent: {name} -> {img_path}")
        else:
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"{name}: [Image failed: {img_path}]"
            })

    # Send files/documents
    for file_path, file_caption in files:
        full_caption = f"{name}: {file_caption}" if file_caption else f"{name}:"
        if send_document(chat_id, file_path, full_caption):
            print(f"File sent: {name} -> {file_path}")
        else:
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"{name}: [File failed: {file_path}]"
            })


def should_escape_response(data: dict) -> bool:
    """Determine whether a response payload should be HTML-escaped."""
    if data.get("escape") is True:
        return True
    if data.get("source") == "codex":
        return True
    return False


 


def create_session(name, backend: str = DEFAULT_BACKEND, chat_id: int = None):
    """Create a new worker instance."""
    _sync_worker_manager()
    return worker_manager.hire(name, backend, chat_id=chat_id)


def kill_session(name):
    """Kill a worker instance."""
    _sync_worker_manager()
    return worker_manager.end(name)


def restart_claude(name):
    """Restart claude in an existing tmux session."""
    _sync_worker_manager()
    return worker_manager.restart(name)


def switch_session(name):
    """Switch active session."""
    registered = get_registered_sessions()
    if name not in registered:
        return False, f"Worker '{name}' not found"

    state["active"] = name
    save_last_active(name)
    return True, None




# ============================================================
# MESSAGE ROUTING
# ============================================================

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
            "text": "Going offline briefly. Your team stays the same."
        })
    print("Shutdown notifications sent")


# ============================================================
# NON-CORE: CommandRouter
# ============================================================

class CommandRouter:
    def __init__(self, telegram_api: TelegramAPI, workers: WorkerManager):
        self.telegram = telegram_api
        self.workers = workers

    def reply(self, chat_id, text, outcome=None):
        self.telegram.send_message(chat_id, text)

    def send_startup_message(self, chat_id):
        registered = self.workers.get_registered_sessions()
        sessions = list(registered.keys())
        active = state["active"]

        lines = ["I'm online and ready."]
        if sessions:
            lines.append(f"Team: {', '.join(sessions)}")
            if active:
                lines.append(f"Focused: {active}")
        else:
            lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")

        if SANDBOX_ENABLED:
            lines.append(f"Sandbox: {Path.home()} → /workspace")

        self.reply(chat_id, "\n".join(lines))

    def handle_message(self, update):
        global admin_chat_id

        msg = update.get("message", {})
        text = msg.get("text", "") or msg.get("caption", "")
        chat_id = msg.get("chat", {}).get("id")
        msg_id = msg.get("message_id")

        photo = msg.get("photo")
        document = msg.get("document")

        doc_is_image = False
        if document:
            mime_type = document.get("mime_type", "")
            doc_is_image = mime_type.startswith("image/")

        if (photo or doc_is_image) and chat_id:
            if photo:
                largest = max(photo, key=lambda p: p.get("file_size", 0))
                file_id = largest.get("file_id")
            else:
                file_id = document.get("file_id")

            if file_id:
                if admin_chat_id is None:
                    admin_chat_id = chat_id
                elif chat_id != admin_chat_id:
                    return

                if not state["active"]:
                    self.reply(chat_id, "Needs decision - No focused worker. Use /focus <name> first.")
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    image_text = f"Manager sent image: {local_path}"
                    if text:
                        image_text = f"{text}\n\n{image_text}"
                    self.route_to_active(image_text, chat_id, msg_id)
                else:
                    self.reply(chat_id, "Needs decision - Could not download image. Try again or send as file.")
                return

        if document and not doc_is_image and chat_id:
            file_id = document.get("file_id")
            if file_id:
                if admin_chat_id is None:
                    admin_chat_id = chat_id
                elif chat_id != admin_chat_id:
                    return

                if not state["active"]:
                    self.reply(chat_id, "Needs decision - No focused worker. Use /focus <name> first.")
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    file_name = document.get("file_name", "unknown")
                    file_size = document.get("file_size", 0)
                    mime_type = document.get("mime_type", "unknown")
                    size_str = format_file_size(file_size)
                    file_text = f"Manager sent file: {file_name} ({size_str}, {mime_type})\nPath: {local_path}"
                    if text:
                        file_text = f"{text}\n\n{file_text}"
                    self.route_to_active(file_text, chat_id, msg_id)
                else:
                    self.reply(chat_id, "Needs decision - Could not download file. Try again.")
                return

        if not text or not chat_id:
            return

        if admin_chat_id is None:
            admin_chat_id = chat_id
            save_last_chat_id(chat_id)
            print(f"Admin registered: {chat_id}")

        if not state["startup_notified"]:
            state["startup_notified"] = True
            self.send_startup_message(chat_id)

        if chat_id != admin_chat_id:
            print(f"Rejected non-admin: {chat_id}")
            return

        save_last_chat_id(chat_id)

        if text.startswith("/"):
            if self.handle_command(text, chat_id, msg_id):
                return

        if text.lower().startswith("@all "):
            message = text[5:]
            self.route_to_all(message, chat_id, msg_id)
            return

        reply_to = msg.get("reply_to_message")
        reply_context = ""
        if reply_to:
            _, reply_context = self.parse_reply_target(reply_to)

        target_session, message = self.parse_at_mention(text)
        if target_session:
            if reply_context:
                message = self.format_reply_context(message, reply_context)
            self.route_message(target_session, message, chat_id, msg_id, one_off=True)
            return

        if reply_to and reply_context:
            reply_target, _ = self.parse_reply_target(reply_to)
            if reply_target:
                routed_text = self.format_reply_context(text, reply_context)
                self.route_message(reply_target, routed_text, chat_id, msg_id, one_off=True)
                return
            else:
                routed_text = self.format_reply_context(text, reply_context)
                self.route_to_active(routed_text, chat_id, msg_id)
                return

        self.route_to_active(text, chat_id, msg_id)

    def parse_at_mention(self, text):
        match = re.match(r'^@([a-zA-Z0-9-]+)\s+(.+)$', text, re.DOTALL)
        if match:
            name = match.group(1).lower()
            message = match.group(2)
            registered = self.workers.get_registered_sessions()
            if name in registered:
                return name, message
        return None, text

    def parse_worker_prefix(self, text):
        if not text:
            return None, ""
        match = re.match(r'^\s*([a-zA-Z0-9-]+):\s*(.*)$', text, re.DOTALL)
        if not match:
            return None, ""
        name = match.group(1).lower()
        message = match.group(2).strip()
        registered = self.workers.get_registered_sessions()
        if name not in registered:
            return None, ""
        return name, message

    def parse_reply_target(self, reply_msg):
        if not reply_msg:
            return None, ""
        reply_text = reply_msg.get("text") or reply_msg.get("caption") or ""

        reply_from = reply_msg.get("from", {})
        if reply_from and reply_from.get("is_bot"):
            worker, _ = self.parse_worker_prefix(reply_text)
            if worker:
                return worker, reply_text

        return None, reply_text

    def format_reply_context(self, reply_text, context_text):
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
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/hire":
            return self.cmd_hire(arg, chat_id)
        elif cmd == "/focus":
            return self.cmd_focus(arg, chat_id)
        elif cmd == "/team":
            return self.cmd_team(chat_id)
        elif cmd == "/end":
            return self.cmd_end(arg, chat_id)
        elif cmd == "/progress":
            return self.cmd_progress(chat_id)
        elif cmd == "/pause":
            return self.cmd_pause(chat_id)
        elif cmd == "/relaunch":
            return self.cmd_relaunch(chat_id)
        elif cmd == "/settings":
            return self.cmd_settings(chat_id)
        elif cmd == "/learn":
            return self.cmd_learn(arg, chat_id, msg_id)
        elif cmd in BLOCKED_COMMANDS:
            self.reply(chat_id, f"{cmd} is interactive and not supported here.", outcome="Needs decision")
            return True

        worker_name = cmd[1:]
        registered = self.workers.get_registered_sessions()
        if worker_name in registered:
            prev_focus = state["active"]
            state["active"] = worker_name
            save_last_active(worker_name)
            if not arg:
                self.reply(chat_id, f"Now talking to {worker_name.capitalize()}.")
                return True
            if prev_focus != worker_name:
                self.telegram.send_message(chat_id, f"Now talking to {worker_name.capitalize()}.")
            self.route_message(worker_name, arg, chat_id, msg_id, one_off=False)
            return True

        return False

    def cmd_hire(self, name, chat_id):
        if not name:
            self.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
            return True

        parsed_name, backend = parse_hire_args(name)
        if not parsed_name:
            self.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
            return True

        name = parsed_name.lower().strip()
        name = re.sub(r'[^a-z0-9-]', '', name)

        if not name:
            self.reply(chat_id, "Name must use letters, numbers, and hyphens only.", outcome="Needs decision")
            return True

        if name in RESERVED_NAMES:
            self.reply(chat_id, f"Cannot use \"{name}\" - reserved command. Choose another name.", outcome="Needs decision")
            return True

        ok, err = create_session(name, backend, chat_id=chat_id)
        if ok:
            self.reply(chat_id, f"{name.capitalize()} is added and assigned. {PERSISTENCE_NOTE}")
            update_bot_commands()
        else:
            self.reply(chat_id, f"Could not hire \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_focus(self, name, chat_id):
        if not name:
            self.reply(chat_id, "Usage: /focus <name>", outcome="Needs decision")
            return True

        name = name.lower().strip()
        ok, err = switch_session(name)
        if ok:
            self.reply(chat_id, f"Now talking to {name.capitalize()}.")
        else:
            self.reply(chat_id, f"Could not focus \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_team(self, chat_id):
        registered = self.workers.scan_tmux_sessions()
        registered = self.workers.get_registered_sessions(registered)

        if not registered:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True

        lines = format_team_lines(registered, state["active"])
        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_end(self, name, chat_id):
        if not name:
            self.reply(chat_id, "Offboarding is permanent. Usage: /end <name>", outcome="Needs decision")
            return True

        name = name.lower().strip()
        ok, err = kill_session(name)
        if ok:
            self.reply(chat_id, f"{name.capitalize()} removed from your team.")
            update_bot_commands()
        else:
            self.reply(chat_id, f"Could not offboard \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_progress(self, chat_id):
        if not state["active"]:
            self.reply(chat_id, "No one assigned. Who should I talk to? Use /team or /focus <name>.")
            return True

        name = state["active"]
        registered = self.workers.get_registered_sessions()
        session = registered.get(name)
        if not session:
            self.reply(chat_id, "Can't find them. Check /team for who's available.")
            return True

        pending = is_pending(name)
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)
        online = False
        ready = False
        needs_attention = None
        mode = "tmux"

        if backend.is_exec:
            online = True
            ready = True
            mode = f"{backend_name} exec (stateless)"
        else:
            tmux_name = session.get("tmux")
            if tmux_name:
                exists = tmux_exists(tmux_name)
                online = exists
                if exists:
                    claude_running = is_claude_running(tmux_name)
                    ready = claude_running
                    if not claude_running:
                        needs_attention = "worker app is not running. Use /relaunch."

        status = format_progress_lines(
            name=name,
            pending=pending,
            backend=backend_name,
            online=online,
            ready=ready,
            mode=mode,
            needs_attention=needs_attention
        )

        self.reply(chat_id, "\n".join(status))
        return True

    def cmd_pause(self, chat_id):
        if not state["active"]:
            self.reply(chat_id, "No one assigned.")
            return True

        name = state["active"]
        registered = self.workers.get_registered_sessions()
        session = registered.get(name)
        if session:
            backend_name = get_worker_backend(name, session)
            backend = get_backend(backend_name)
            if backend.is_exec:
                clear_pending(name)
                self.reply(chat_id, f"{name.capitalize()} is paused. I'll pick up where we left off.")
                return True
            tmux_send_escape(session["tmux"])
            clear_pending(name)

        self.reply(chat_id, f"{name.capitalize()} is paused. I'll pick up where we left off.")
        return True

    def cmd_relaunch(self, chat_id):
        if not state["active"]:
            self.reply(chat_id, "No one assigned.")
            return True

        name = state["active"]
        ok, err = restart_claude(name)
        if ok:
            self.reply(chat_id, f"Bringing {name.capitalize()} back online...")
        else:
            self.reply(chat_id, f"Could not relaunch \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_settings(self, chat_id):
        def redact(s):
            if not s:
                return "(not set)"
            if len(s) <= 8:
                return "***"
            return s[:4] + "..." + s[-4:]

        registered = self.workers.get_registered_sessions()
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
        ]

        lines.append("")
        if SANDBOX_ENABLED:
            lines.append("Sandbox: enabled (Docker isolation)")
            lines.append(f"Image: {SANDBOX_IMAGE}")
            lines.append(f"Default mount: {Path.home()} → /workspace")
            if SANDBOX_EXTRA_MOUNTS:
                lines.append("Extra mounts:")
                for host, container, ro in SANDBOX_EXTRA_MOUNTS:
                    ro_flag = " (ro)" if ro else ""
                    lines.append(f"  {host} → {container}{ro_flag}")
            lines.append("")
            lines.append("Note: Workers run in containers with access")
            lines.append("only to mounted directories. System paths")
            lines.append("outside mounts are not accessible.")
        else:
            lines.append("Sandbox: disabled (direct execution)")
            lines.append("Workers run with full system access.")

        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_learn(self, topic, chat_id, msg_id=None):
        if not state["active"]:
            self.reply(chat_id, "No one assigned. Who should I talk to?")
            return True

        name = state["active"]
        registered = self.workers.get_registered_sessions()
        session = registered.get(name)
        if not session:
            self.reply(chat_id, "Can't find them. Check /team.")
            return True
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)

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

        if not self.workers.is_online(name, session):
            self.reply(chat_id, f"{name.capitalize()} is offline. Try /relaunch.")
            return True

        worker_set_pending(name, chat_id)
        threading.Thread(
            target=send_typing_loop,
            args=(chat_id, name),
            daemon=True
        ).start()

        send_ok = self.workers.send(name, prompt, chat_id, session)
        if not send_ok:
            clear_pending(name)
            self.reply(chat_id, f"Could not send to {name.capitalize()}. Try /relaunch.", outcome="Needs decision")
            return True

        if msg_id and send_ok:
            if backend.is_exec or tmux_prompt_empty(session.get("tmux", "")):
                self.telegram.set_reaction(chat_id, msg_id, [{"type": "emoji", "emoji": "👀"}])
        return True

    def route_to_active(self, text, chat_id, msg_id):
        registered = self.workers.get_registered_sessions()

        if not state["active"]:
            if registered:
                names = ", ".join(registered.keys())
                self.reply(chat_id, f"No one assigned. Your team: {names}\nWho should I talk to?")
                return
            else:
                self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
                return

        self.route_message(state["active"], text, chat_id, msg_id, one_off=False)

    def route_to_all(self, text, chat_id, msg_id):
        registered = self.workers.get_registered_sessions()
        sessions = list(registered.keys())
        if not sessions:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return

        sent_to = []
        for name in sessions:
            session = registered[name]
            if self.workers.is_online(name, session):
                self.route_message(name, text, chat_id, msg_id, one_off=True)
                sent_to.append(name)

        if not sent_to:
            self.reply(chat_id, "No one's online to share with.")

    def route_message(self, session_name, text, chat_id, msg_id, one_off=False):
        registered = self.workers.get_registered_sessions()
        session = registered.get(session_name)
        if not session:
            self.reply(chat_id, f"Can't find {session_name}. Check /team for who's available.")
            return

        if not self.workers.is_online(session_name, session):
            self.reply(chat_id, f"{session_name.capitalize()} is offline. Try /relaunch.")
            return

        backend_name = get_worker_backend(session_name, session)
        backend = get_backend(backend_name)

        print(f"[{chat_id}] -> {session_name}: {text[:50]}...")

        worker_set_pending(session_name, chat_id)
        threading.Thread(
            target=send_typing_loop,
            args=(chat_id, session_name),
            daemon=True
        ).start()

        send_ok = self.workers.send(session_name, text, chat_id, session)
        if not send_ok:
            clear_pending(session_name)
            self.reply(
                chat_id,
                f"Could not send to {session_name.capitalize()}. Try /relaunch.",
                outcome="Needs decision"
            )
            return

        if msg_id and send_ok:
            if backend.is_exec or tmux_prompt_empty(session.get("tmux", "")):
                self.telegram.set_reaction(chat_id, msg_id, [{"type": "emoji", "emoji": "👀"}])


command_router = CommandRouter(telegram, worker_manager)

# ============================================================
# NON-CORE: HTTP Handler
# ============================================================

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
            # Debug: show what update type we received
            update_types = [k for k in update.keys() if k != "update_id"]
            if update_types and update_types[0] != "message":
                print(f"Received update type: {update_types}")
            if "message" in update:
                command_router.handle_message(update)
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
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

        FILE SUPPORT: Parses [[image:/path|caption]] and [[file:/path|caption]] tags.
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

            # Send response using shared helper
            escape = should_escape_response(data)
            send_response_to_telegram(session_name, text, int(chat_id), escape=escape, log_prefix="Response")

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
        # Handle /workers endpoint for inter-worker discovery
        if self.path == "/workers":
            self.handle_workers_endpoint()
            return

        # Default health check endpoint
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Claude-Telegram Multi-Session Bridge")

    def handle_workers_endpoint(self):
        """Return list of active workers with communication details.

        GET /workers
        Response: {"workers": [{"name": ..., "protocol": ..., "address": ..., "send_example": ...}, ...]}
        """
        try:
            workers = get_workers()
            response = {"workers": workers}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            print(f"Workers endpoint error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())


# ============================================================
# MAIN
# ============================================================

def graceful_shutdown(signum, frame):
    """Handle shutdown signals gracefully with diagnostic info."""
    from datetime import datetime
    sig_name = signal.Signals(signum).name if signum else "unknown"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ppid = os.getppid()

    # Try to get parent process info
    parent_info = f"ppid={ppid}"
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            cmdline = f.read().decode().replace("\x00", " ").strip()
            parent_info = f"ppid={ppid} cmd={cmdline[:100]}"
    except Exception:
        pass

    print(f"\n[{timestamp}] Received {sig_name} ({parent_info}), shutting down...")

    send_shutdown_message()
    sys.exit(0)


def main():
    global admin_chat_id

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
    registered = scan_tmux_sessions()
    registered = get_registered_sessions(registered)
    if registered:
        print(f"Discovered sessions: {list(registered.keys())}")

    # Load last active worker from file (if still exists)
    last_active = load_last_active()
    if last_active and last_active in registered:
        state["active"] = last_active
        print(f"Restored last active worker: {last_active}")
    elif last_active:
        print(f"Last active worker '{last_active}' no longer exists")

    # Load last chat ID for auto-notification
    last_chat_id = load_last_chat_id()
    if last_chat_id:
        if admin_chat_id is None:
            admin_chat_id = last_chat_id
            print(f"Restored admin from last_chat_id: {admin_chat_id}")

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

    # Sandbox status
    if SANDBOX_ENABLED:
        print(f"Sandbox mode: Workers run in Docker containers")
        print(f"Mounted: {Path.home()} → /workspace")
        if SANDBOX_EXTRA_MOUNTS:
            for host, container, ro in SANDBOX_EXTRA_MOUNTS:
                ro_flag = " (ro)" if ro else ""
                print(f"Mounted: {host} → {container}{ro_flag}")
        print("Workers can only access mounted directories")
    else:
        print("Sandbox mode: disabled (direct execution)")

    # Send startup notification if we have a last known chat ID
    if last_chat_id:
        state["startup_notified"] = True
        sessions = list(registered.keys())
        active = state["active"]

        lines = ["I'm online and ready."]
        if sessions:
            lines.append(f"Team: {', '.join(sessions)}")
            if active:
                lines.append(f"Focused: {active}")
        else:
            lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")

        if SANDBOX_ENABLED:
            lines.append(f"Sandbox: {Path.home()} → /workspace")

        result = telegram_api("sendMessage", {"chat_id": last_chat_id, "text": "\n".join(lines)})
        if result and result.get("ok"):
            print(f"Sent startup notification to chat {last_chat_id}")
        else:
            print(f"Failed to send startup notification: {result}")

    try:
        ReuseAddrServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
