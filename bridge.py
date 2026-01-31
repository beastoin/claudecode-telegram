#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.16.0"

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

# Temporary file inbox (session-isolated, auto-cleaned)
FILE_INBOX_ROOT = Path("/tmp/claudecode-telegram")

# In-memory state (RAM only, no persistence - tmux IS the persistence)
state = {
    "active": None,  # Currently active session name
    "pending_registration": None,  # Unregistered tmux session awaiting name
    "startup_notified": False,  # Whether we've sent the startup message
}

# Per-session locks to prevent concurrent tmux sends from interleaving
# Without this, rapid messages from multiple threads can corrupt each other
_tmux_send_locks = {}
_tmux_send_locks_guard = threading.Lock()

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

# Reserved names that cannot be used as worker names (would clash with commands)
RESERVED_NAMES = {
    # Bridge commands
    "team", "focus", "progress", "learn", "pause", "relaunch", "settings", "hire", "end",
    # Aliases
    "new", "use", "list", "kill", "status", "stop", "restart", "system",
    # Special
    "all", "start", "help",
}


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
    registered, _ = scan_tmux_sessions()
    for name in sorted(registered.keys()):
        commands.append({"command": name, "description": f"Message {name}"})

    result = telegram_api("setMyCommands", {"commands": commands})
    if result and result.get("ok"):
        worker_count = len(registered)
        print(f"Bot commands updated ({len(BOT_COMMANDS)} + {worker_count} workers)")


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
    # First try pane_current_command (fast path)
    cmd = get_pane_command(tmux_name)
    if "claude" in cmd.lower():
        return True

    # Fallback: check if claude is a child process of the pane
    # This handles cases where pane_current_command returns unexpected values
    result = subprocess.run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False

    pane_pid = result.stdout.strip()
    if not pane_pid:
        return False

    # Check for claude as child process using pgrep
    result = subprocess.run(
        ["pgrep", "-P", pane_pid, "claude"],
        capture_output=True
    )
    return result.returncode == 0


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


def _get_tmux_send_lock(tmux_name):
    """Get or create a lock for a specific tmux session.

    Prevents concurrent sends to the same session from interleaving,
    which causes messages to corrupt each other.
    """
    with _tmux_send_locks_guard:
        if tmux_name not in _tmux_send_locks:
            _tmux_send_locks[tmux_name] = threading.Lock()
        return _tmux_send_locks[tmux_name]


def tmux_send_message(tmux_name, text):
    """Send text + Enter to tmux session with locking.

    Uses per-session lock to prevent concurrent sends from interleaving.
    Sends text with -l flag (literal), then Enter key separately.
    """
    lock = _get_tmux_send_lock(tmux_name)
    with lock:
        send_ok = tmux_send(tmux_name, text, literal=True)
        enter_ok = tmux_send_enter(tmux_name)
        return send_ok and enter_ok


def tmux_send_escape(tmux_name):
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Escape"])


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


def export_hook_env(tmux_name):
    """Export env vars for hook inside tmux session.

    Uses tmux set-environment which persists in session and survives restarts.
    Hook reads these via `tmux show-environment -t $SESSION_NAME`.
    """
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)])
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX])
    subprocess.run(["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", str(SESSIONS_DIR)])
    # Only set BRIDGE_URL if user-provided (not default localhost)
    # This makes override semantics clear: set = override, unset = use PORT
    if _bridge_url_env:
        subprocess.run(["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", BRIDGE_URL])
    else:
        # Unset to clear any stale value from previous config
        subprocess.run(["tmux", "set-environment", "-u", "-t", tmux_name, "BRIDGE_URL"], capture_output=True)


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
        f"-e=BRIDGE_SESSION={name}",  # Session name for hook (no tmux inside container)
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


def create_session(name):
    """Create a new Claude instance.

    SECURITY: Token is NOT exported to Claude session. Hook forwards responses
    to bridge via localhost HTTP, bridge sends to Telegram. Token isolation.

    Args:
        name: Worker name
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

    if SANDBOX_ENABLED:
        # Sandbox mode: run Claude in Docker container
        docker_cmd = get_docker_run_cmd(name)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        print(f"Started worker '{name}' in sandbox mode")
    else:
        # Legacy mode: run Claude directly
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

    # Send welcome message with feature info (after Claude starts)
    time.sleep(2.0 if not SANDBOX_ENABLED else 5.0)  # Container startup takes longer
    welcome = (
        "You are connected to Telegram via claudecode-telegram bridge. "
        "Manager can send you files (images, PDFs, documents) - they'll appear as local paths. "
        "To send files back: [[file:/path/to/doc.pdf|caption]] or [[image:/path/to/img.png|caption]]. "
        "Allowed paths: /tmp, current directory."
    )
    if SANDBOX_ENABLED:
        welcome += " Running in sandbox mode (Docker container)."
    tmux_send_message(tmux_name, welcome)

    state["active"] = name
    ensure_session_dir(name)

    return True, None


def kill_session(name):
    """Kill a Claude instance."""
    registered = get_registered_sessions()
    if name not in registered:
        return False, f"Worker '{name}' not found"

    tmux_name = registered[name]["tmux"]

    # Stop docker container if in sandbox mode
    if SANDBOX_ENABLED:
        stop_docker_container(name)

    subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)

    # Clean up inbox images
    cleanup_inbox(name)

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

    if SANDBOX_ENABLED:
        # Stop any existing container first
        stop_docker_container(name)
        time.sleep(0.5)

        # Start new container
        docker_cmd = get_docker_run_cmd(name)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
    else:
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
            "text": "Going offline briefly. Your team stays the same."
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

            # Parse image and file tags from text
            clean_text, images = parse_image_tags(text)
            clean_text, files = parse_file_tags(clean_text)

            # Send text message if there's text content
            if clean_text:
                # Split long messages to fit Telegram's 4096 char limit
                # Reserve space for prefix: "<b>name (xx/xx):</b>\n" ≈ 30 chars + session name
                prefix_reserve = len(session_name) + 30
                chunks = split_message(clean_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
                formatted_parts = format_multipart_messages(session_name, chunks)

                prev_msg_id = None
                for i, part in enumerate(formatted_parts):
                    msg_data = {
                        "chat_id": chat_id,
                        "text": part,
                        "parse_mode": "HTML"
                    }
                    # Chain messages with reply_to for visual grouping
                    if prev_msg_id:
                        msg_data["reply_to_message_id"] = prev_msg_id

                    result = telegram_api("sendMessage", msg_data)
                    if result and result.get("ok"):
                        prev_msg_id = result.get("result", {}).get("message_id")
                        if len(formatted_parts) > 1:
                            print(f"Response sent: {session_name} part {i+1}/{len(formatted_parts)} -> Telegram OK")
                        else:
                            print(f"Response sent: {session_name} -> Telegram OK")
                    else:
                        print(f"Response failed: {session_name} part {i+1} -> {result}")

                    # Small delay between parts to ensure order
                    if i < len(formatted_parts) - 1:
                        time.sleep(0.05)

            # Send images
            for img_path, img_caption in images:
                # Add session prefix to caption
                full_caption = f"{session_name}: {img_caption}" if img_caption else f"{session_name}:"
                if send_photo(chat_id, img_path, full_caption):
                    print(f"Image sent: {session_name} -> {img_path}")
                else:
                    # Notify about failed image
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": f"<b>{session_name}:</b> [Image failed: {img_path}]",
                        "parse_mode": "HTML"
                    })

            # Send files/documents
            for file_path, file_caption in files:
                # Add session prefix to caption
                full_caption = f"{session_name}: {file_caption}" if file_caption else f"{session_name}:"
                if send_document(chat_id, file_path, full_caption):
                    print(f"File sent: {session_name} -> {file_path}")
                else:
                    # Notify about failed file
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": f"<b>{session_name}:</b> [File failed: {file_path}]",
                        "parse_mode": "HTML"
                    })

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
        text = msg.get("text", "") or msg.get("caption", "")
        chat_id = msg.get("chat", {}).get("id")
        msg_id = msg.get("message_id")

        # Handle photo messages
        photo = msg.get("photo")
        document = msg.get("document")

        # Check if document is an image
        doc_is_image = False
        if document:
            mime_type = document.get("mime_type", "")
            doc_is_image = mime_type.startswith("image/")

        if (photo or doc_is_image) and chat_id:
            # Get file_id from photo or document
            if photo:
                largest = max(photo, key=lambda p: p.get("file_size", 0))
                file_id = largest.get("file_id")
            else:
                file_id = document.get("file_id")

            if file_id:
                # Admin check first
                if admin_chat_id is None:
                    admin_chat_id = chat_id
                elif chat_id != admin_chat_id:
                    return  # Silent rejection

                # Download to focused worker's inbox
                if not state["active"]:
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Needs decision - No focused worker. Use /focus <name> first."
                    })
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    # Build message with image path
                    image_text = f"Manager sent image: {local_path}"
                    if text:
                        image_text = f"{text}\n\n{image_text}"
                    # Route to active session
                    self.route_to_active(image_text, chat_id, msg_id)
                else:
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Needs decision - Could not download image. Try again or send as file."
                    })
                return

        # Handle non-image document attachments (PDF, txt, code files, etc.)
        if document and not doc_is_image and chat_id:
            file_id = document.get("file_id")
            if file_id:
                # Admin check first
                if admin_chat_id is None:
                    admin_chat_id = chat_id
                elif chat_id != admin_chat_id:
                    return  # Silent rejection

                # Download to focused worker's inbox
                if not state["active"]:
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Needs decision - No focused worker. Use /focus <name> first."
                    })
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    # Build message with file metadata
                    file_name = document.get("file_name", "unknown")
                    file_size = document.get("file_size", 0)
                    mime_type = document.get("mime_type", "unknown")
                    size_str = format_file_size(file_size)
                    file_text = f"Manager sent file: {file_name} ({size_str}, {mime_type})\nPath: {local_path}"
                    if text:
                        file_text = f"{text}\n\n{file_text}"
                    # Route to active session
                    self.route_to_active(file_text, chat_id, msg_id)
                else:
                    telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Needs decision - Could not download file. Try again."
                    })
                return

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
                # Reply to worker message → route to that worker
                routed_text = self.format_reply_context(text, reply_context)
                self.route_message(reply_target, routed_text, chat_id, msg_id, one_off=True)
                return
            else:
                # Reply to non-worker message → route to focused with context
                routed_text = self.format_reply_context(text, reply_context)
                self.route_to_active(routed_text, chat_id, msg_id)
                return

        # Route to active session (no reply)
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

                # Validate: name cannot clash with reserved commands
                if name in RESERVED_NAMES:
                    self.reply(chat_id, f"Cannot use \"{name}\" - reserved command. Choose another name.", outcome="Needs decision")
                    return True

                registered = get_registered_sessions()
                if name in registered:
                    self.reply(chat_id, f"Worker name \"{name}\" is already on the team. Choose another.", outcome="Needs decision")
                    return True

                ok, err = register_session(name, state["pending_registration"])
                if ok:
                    self.reply(chat_id, f"{name.capitalize()} is now on your team and assigned.")
                    # Update bot commands to include new worker
                    update_bot_commands()
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
        """Extract worker target and context from a replied-to message.

        Always returns context (the replied message text).
        Only returns worker target if it's a bot message with worker prefix.
        """
        if not reply_msg:
            return None, ""
        reply_text = reply_msg.get("text") or reply_msg.get("caption") or ""

        # Check if it's a bot message with worker prefix
        reply_from = reply_msg.get("from", {})
        if reply_from and reply_from.get("is_bot"):
            worker, _ = self.parse_worker_prefix(reply_text)
            if worker:
                return worker, reply_text

        # Not a worker message, but still return context
        return None, reply_text

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

        # Check if command is a worker shortcut: /lee hello -> route to lee AND switch focus
        # Slash commands are "strong" actions - they imply intent to talk to that person
        worker_name = cmd[1:]  # Remove leading /
        registered = get_registered_sessions()
        if worker_name in registered:
            # Always switch focus when using /name (strong intent)
            prev_focus = state["active"]
            state["active"] = worker_name
            if not arg:
                # Just /lee with no message - switch focus only
                self.reply(chat_id, f"Now talking to {worker_name.capitalize()}.")
                return True
            # /lee hello -> route message to lee AND switch focus
            # Notify focus change if switching from different worker
            if prev_focus != worker_name:
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"Now talking to {worker_name.capitalize()}."
                })
            self.route_message(worker_name, arg, chat_id, msg_id, one_off=False)
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

        # Validate: name cannot clash with reserved commands
        if name in RESERVED_NAMES:
            self.reply(chat_id, f"Cannot use \"{name}\" - reserved command. Choose another name.", outcome="Needs decision")
            return True

        ok, err = create_session(name)
        if ok:
            self.reply(chat_id, f"{name.capitalize()} is added and assigned. {PERSISTENCE_NOTE}")
            # Update bot commands to include new worker
            update_bot_commands()
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
            self.reply(chat_id, f"Now talking to {name.capitalize()}.")
        else:
            self.reply(chat_id, f"Could not focus \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_team(self, chat_id):
        """List all Claude instances."""
        # Refresh from tmux
        registered, unregistered = scan_tmux_sessions()
        registered = get_registered_sessions(registered)

        if not registered and not unregistered:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True

        lines = []
        lines.append("Your team:")
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
            self.reply(chat_id, f"{name.capitalize()} removed from your team.")
            # Update bot commands to remove worker
            update_bot_commands()
        else:
            self.reply(chat_id, f"Could not offboard \"{name}\". {err}", outcome="Needs decision")
        return True

    def cmd_progress(self, chat_id):
        """Show detailed status of focused Claude."""
        if not state["active"]:
            self.reply(chat_id, "No one assigned. Who should I talk to? Use /team or /focus <name>.")
            return True

        name = state["active"]
        registered = get_registered_sessions()
        session = registered.get(name)
        if not session:
            self.reply(chat_id, "Can't find them. Check /team for who's available.")
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
            self.reply(chat_id, "No one assigned.")
            return True

        name = state["active"]
        registered = get_registered_sessions()
        session = registered.get(name)
        if session:
            tmux_send_escape(session["tmux"])
            clear_pending(name)

        self.reply(chat_id, f"{name.capitalize()} is paused. I'll pick up where we left off.")
        return True

    def cmd_relaunch(self, chat_id):
        """Restart Claude in active session."""
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

        # Sandbox details
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
        """Ask the focused worker what they learned today, optionally about a topic."""
        if not state["active"]:
            self.reply(chat_id, "No one assigned. Who should I talk to?")
            return True

        name = state["active"]
        registered = get_registered_sessions()
        session = registered.get(name)
        if not session:
            self.reply(chat_id, "Can't find them. Check /team.")
            return True

        tmux_name = session["tmux"]
        if not tmux_exists(tmux_name) or not is_claude_running(tmux_name):
            self.reply(chat_id, f"{name.capitalize()} is offline. Try /relaunch.")
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

        # Send prompt to worker (with lock to prevent interleaving)
        send_ok = tmux_send_message(tmux_name, prompt)

        # 👀 reaction only if Claude accepted the message (prompt is empty)
        if msg_id and send_ok and tmux_prompt_empty(tmux_name):
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "👀"}]
            })
        return True

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
                self.reply(chat_id, f"No one assigned. Your team: {names}\nWho should I talk to?")
                return
            else:
                # No sessions at all
                self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
                return

        self.route_message(state["active"], text, chat_id, msg_id, one_off=False)

    def route_to_all(self, text, chat_id, msg_id):
        """Broadcast message to all running sessions."""
        registered = get_registered_sessions()
        sessions = list(registered.keys())
        if not sessions:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
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
            self.reply(chat_id, "No one's online to share with.")

    def route_message(self, session_name, text, chat_id, msg_id, one_off=False):
        """Route a message to a specific session."""
        registered = get_registered_sessions()
        session = registered.get(session_name)
        if not session:
            self.reply(chat_id, f"Can't find {session_name}. Check /team for who's available.")
            return

        tmux_name = session["tmux"]

        if not tmux_exists(tmux_name):
            self.reply(chat_id, f"{session_name.capitalize()} is offline. Try /relaunch.")
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

        # Send to tmux (with lock to prevent interleaving)
        send_ok = tmux_send_message(tmux_name, text)

        # Add 👀 reaction only if Claude accepted the message (prompt is empty)
        if msg_id and send_ok and tmux_prompt_empty(tmux_name):
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "👀"}]
            })

    def reply(self, chat_id, text, outcome=None):
        # No prefix - manager-friendly direct messages
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text})

    def send_startup_message(self, chat_id):
        """Send bridge startup notification."""
        registered = get_registered_sessions()
        sessions = list(registered.keys())
        active = state["active"]

        lines = ["I'm online and ready."]
        if sessions:
            lines.append(f"Team: {', '.join(sessions)}")
            if active:
                lines.append(f"Focused: {active}")
        else:
            lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")

        # Short sandbox note
        if SANDBOX_ENABLED:
            lines.append(f"Sandbox: {Path.home()} → /workspace")

        self.reply(chat_id, "\n".join(lines))


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

    try:
        ReuseAddrServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
