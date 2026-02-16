#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.24.0"

import os
import json
import mimetypes
import shutil
import signal
import subprocess
import sys
import threading
import time
import re
import urllib.error
import urllib.request
import shlex
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs
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
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
CLAUDE_SETTINGS_FILE = Path(os.environ.get("CLAUDE_SETTINGS_FILE", CLAUDE_DIR / "settings.json"))

# MCP tool inventory (system prompt injection)
MCP_INVENTORY_ENABLED = os.environ.get("MCP_INVENTORY_ENABLED", "1") != "0"
MCP_CONFIG_PATHS = [
    Path(p.strip()).expanduser()
    for p in os.environ.get("MCP_CONFIG_PATHS", "").split(",")
    if p.strip()
]
MCP_PROJECT_FILES = [
    name.strip()
    for name in os.environ.get("MCP_PROJECT_FILES", ".mcp.json,.mcp.jsonc").split(",")
    if name.strip()
]
MCP_PROJECT_ROOT = os.environ.get("MCP_PROJECT_ROOT", "").strip()
MCP_PROJECT_SEARCH_DEPTH = int(os.environ.get("MCP_PROJECT_SEARCH_DEPTH", "6"))
MCP_INVENTORY_MAX_CHARS = int(os.environ.get("MCP_INVENTORY_MAX_CHARS", "2000"))
MCP_INVENTORY_INCLUDE_COMMAND = os.environ.get("MCP_INVENTORY_INCLUDE_COMMAND", "0") == "1"
MCP_INVENTORY_INCLUDE_ENV_KEYS = os.environ.get("MCP_INVENTORY_INCLUDE_ENV_KEYS", "1") != "0"

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
PENDING_TIMEOUT = 600

# Team directory: shared knowledge base (soul docs, kanban, playbook, etc.)
TEAM_DIR = os.path.expanduser(os.environ.get("TEAM_DIR", "~/team"))
# Checkin note: read from TEAM_DIR/checkin-note.txt on each checkin/hire/restart.
# Supports {name} placeholder for per-worker substitution.
_CHECKIN_NOTE_PATH = os.path.join(TEAM_DIR, "checkin-note.txt")
WATCHDOG_INTERVAL = 4
START_GRACE = 30
THINK_GRACE = 30
TOOL_GAP_GRACE = 12
STALE_PENDING = 300
CPU_ACTIVE = 15.0
CPU_IDLE = 7.0
IDLE_STREAK_STUCK = 3
ALERT_COOLDOWN = 180


# ============================================================
# CORE: Backend Protocol + implementations
# ============================================================

def _strip_json_comments(text: str) -> str:
    """Remove // and /* */ comments for jsonc-like files."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(^|\s)//.*$", r"\1", text, flags=re.MULTILINE)
    return text


def _read_json_file(path: Path) -> Optional[dict]:
    try:
        raw = path.read_text()
    except Exception:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(_strip_json_comments(raw))
        except json.JSONDecodeError:
            return None


def _extract_mcp_servers(config: dict) -> dict:
    if not isinstance(config, dict):
        return {}

    candidates = []
    if isinstance(config.get("mcpServers"), dict):
        candidates.append(config["mcpServers"])
    if isinstance(config.get("mcp"), dict):
        mcp = config["mcp"]
        if isinstance(mcp.get("servers"), dict):
            candidates.append(mcp["servers"])
        if isinstance(mcp.get("mcpServers"), dict):
            candidates.append(mcp["mcpServers"])
    if isinstance(config.get("servers"), dict):
        candidates.append(config["servers"])

    servers = {}
    for candidate in candidates:
        for name, data in candidate.items():
            if isinstance(data, dict):
                servers[name] = data
            else:
                servers[name] = {"_raw": data}
    return servers


def _find_project_mcp_files(start_dir: Path) -> list[Path]:
    if not start_dir:
        return []
    try:
        current = start_dir.resolve()
    except Exception:
        current = start_dir

    found = []
    depth = max(MCP_PROJECT_SEARCH_DEPTH, 0)
    for _ in range(depth + 1):
        for name in MCP_PROJECT_FILES:
            candidate = current / name
            if candidate.exists():
                found.append(candidate)
        if current.parent == current:
            break
        current = current.parent
    return found


def _format_mcp_source(path: Path) -> str:
    try:
        if path.resolve() == CLAUDE_SETTINGS_FILE.resolve():
            return "settings.json"
    except Exception:
        pass
    return path.name


def _normalize_server_info(name: str, data: dict, source: str) -> dict:
    info = {"name": name, "source": source}
    if isinstance(data, dict):
        description = data.get("description") or data.get("desc")
        if isinstance(description, str) and description.strip():
            info["description"] = description.strip()
        env = data.get("env") or data.get("environment") or {}
        if MCP_INVENTORY_INCLUDE_ENV_KEYS and isinstance(env, dict):
            env_keys = sorted(k for k in env.keys() if isinstance(k, str))
            if env_keys:
                info["env_keys"] = env_keys
        if MCP_INVENTORY_INCLUDE_COMMAND:
            command = data.get("command")
            if isinstance(command, str) and command.strip():
                info["command"] = command.strip()
    return info


def build_mcp_inventory_prompt(cwd: Optional[str] = None) -> str:
    if not MCP_INVENTORY_ENABLED:
        return ""

    paths = []
    if MCP_CONFIG_PATHS:
        paths.extend(MCP_CONFIG_PATHS)
    else:
        paths.append(CLAUDE_SETTINGS_FILE)

    project_root = Path(MCP_PROJECT_ROOT).expanduser() if MCP_PROJECT_ROOT else None
    if not project_root and cwd:
        project_root = Path(cwd).expanduser()
    if project_root:
        paths.extend(_find_project_mcp_files(project_root))

    # De-dupe while preserving order
    seen = set()
    unique_paths = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)

    servers = {}
    for path in unique_paths:
        if not path.exists():
            continue
        config = _read_json_file(path)
        if not config:
            continue
        for name, data in _extract_mcp_servers(config).items():
            servers[name] = _normalize_server_info(name, data, _format_mcp_source(path))

    if not servers:
        return ""

    lines = [
        "MCP tool inventory (auto-loaded at startup).",
        "Use only tools listed here. If tools change, restart this worker to reload MCP.",
        "To see exact tool names/params, run `/mcp list` in Claude Code.",
        "Servers:",
    ]
    for name in sorted(servers.keys()):
        info = servers[name]
        line = f"- {name} (source: {info.get('source', 'unknown')})"
        description = info.get("description")
        if description:
            line += f" — {description}"
        env_keys = info.get("env_keys") if MCP_INVENTORY_INCLUDE_ENV_KEYS else None
        if env_keys:
            line += f" [env: {', '.join(env_keys)}]"
        command = info.get("command") if MCP_INVENTORY_INCLUDE_COMMAND else None
        if command:
            line += f" [cmd: {command}]"
        lines.append(line)

    prompt = "\n".join(lines)
    if len(prompt) <= MCP_INVENTORY_MAX_CHARS:
        return prompt

    short_lines = lines[:4] + [f"- {name}" for name in sorted(servers.keys())]
    prompt = "\n".join(short_lines)
    if len(prompt) > MCP_INVENTORY_MAX_CHARS:
        prompt = prompt[: max(MCP_INVENTORY_MAX_CHARS - 3, 0)] + "..."
    return prompt


def build_claude_start_cmd(resume_id: str = "", append_system_prompt: str = "") -> str:
    cmd = ["claude"]
    if resume_id:
        cmd.extend(["--resume", resume_id])
    if append_system_prompt:
        cmd.extend(["--append-system-prompt", append_system_prompt])
    cmd.append("--dangerously-skip-permissions")
    return " ".join(shlex.quote(part) for part in cmd)


class Backend(Protocol):
    """Minimal backend interface. 3 methods, no more."""
    name: str
    binary: str  # CLI binary name (e.g. "claude", "codex")
    is_interactive: bool

    def start_cmd(self, resume_id: str = "", append_system_prompt: str = "") -> str:
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


def _tmux_pane_pids() -> dict:
    """Return a map of tmux session_name -> pane_pid for all panes."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return {}

    if result.returncode != 0:
        return {}

    pane_map = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        session_name, pane_pid = parts[0], parts[1]
        if pane_pid.isdigit():
            pane_map[session_name] = pane_pid
    return pane_map


def _get_claude_pid(pane_pid: str) -> Optional[str]:
    """Return Claude PID for a pane, or None if not found."""
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pane_pid), "-f", "claude"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip().splitlines()
    if not output:
        return None
    return output[0].strip()


def _child_count(pid: str) -> int:
    """Return child process count for pid."""
    if not pid:
        return 0
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return 0

    if result.returncode != 0:
        return 0

    return len([line for line in result.stdout.splitlines() if line.strip()])


def _ps_stats(pids) -> dict:
    """Return {pid: {'cpu': float, 'state': str}} for given pids."""
    pid_list = [str(pid) for pid in pids if pid]
    if not pid_list:
        return {}

    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,%cpu=,state=", "-p", ",".join(pid_list)],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return {}

    if result.returncode != 0:
        return {}

    stats = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        pid = parts[0]
        try:
            cpu = float(parts[1])
        except ValueError:
            cpu = 0.0
        state = parts[2]
        stats[pid] = {"cpu": cpu, "state": state}
    return stats


def mark_hook_event(session_name: str) -> None:
    """Record timestamp of last hook response for a session."""
    with _watchdog_lock:
        _last_hook_ts[session_name] = time.time()


class ClaudeBackend:
    """Claude Code CLI - interactive mode with hook for responses."""
    name = "claude"
    binary = "claude"
    is_interactive = True

    def start_cmd(self, resume_id: str = "", append_system_prompt: str = "") -> str:
        return build_claude_start_cmd(resume_id, append_system_prompt)

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
    """OpenAI Codex CLI - non-interactive mode."""
    name = "codex"
    binary = "codex"
    is_interactive = False

    def start_cmd(self, resume_id: str = "", append_system_prompt: str = "") -> str:
        return "echo 'Codex worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        adapter = Path(__file__).parent / "hooks" / "codex-tmux-adapter.py"
        return _spawn_adapter(adapter, worker_name, text, bridge_url, sessions_dir)

    def is_online(self, tmux_name: str) -> bool:
        return tmux_exists(tmux_name)


class GeminiBackend:
    """Google Gemini CLI - non-interactive mode (stub)."""
    name = "gemini"
    binary = "gemini"
    is_interactive = False

    def start_cmd(self, resume_id: str = "", append_system_prompt: str = "") -> str:
        return "echo 'Gemini worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        adapter = Path(__file__).parent / "hooks" / "gemini-adapter.py"
        return _spawn_adapter(adapter, worker_name, text, bridge_url, sessions_dir)

    def is_online(self, tmux_name: str) -> bool:
        return tmux_exists(tmux_name)


class OpenCodeBackend:
    """OpenCode CLI - non-interactive mode (stub)."""
    name = "opencode"
    binary = "opencode"
    is_interactive = False

    def start_cmd(self, resume_id: str = "", append_system_prompt: str = "") -> str:
        return "echo 'OpenCode worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        adapter = Path(__file__).parent / "hooks" / "opencode-adapter.py"
        return _spawn_adapter(adapter, worker_name, text, bridge_url, sessions_dir)

    def is_online(self, tmux_name: str) -> bool:
        return tmux_exists(tmux_name)


BACKENDS = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
    "gemini": GeminiBackend(),
    "opencode": OpenCodeBackend(),
}

# Track inflight adapter processes per worker (non-interactive backends only)
# Each entry: (Popen, stderr_file_handle_or_None)
_adapter_pids: dict[str, tuple[subprocess.Popen, object]] = {}


def _spawn_adapter(adapter_path: Path, worker_name: str, text: str,
                   bridge_url: str, sessions_dir: Path) -> bool:
    """Spawn an adapter process with stderr logged to per-worker file."""
    if not adapter_path.exists():
        print(f"Adapter not found: {adapter_path}")
        return False

    # Open per-worker log file for adapter stderr (append mode)
    log_file = sessions_dir / worker_name / "adapter.log"
    try:
        stderr_fh = open(log_file, "a")
    except OSError:
        stderr_fh = None  # Fall back to DEVNULL if dir doesn't exist yet

    proc = subprocess.Popen(
        ["python3", str(adapter_path), worker_name, text, bridge_url, str(sessions_dir)],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh if stderr_fh else subprocess.DEVNULL
    )
    _adapter_pids[worker_name] = (proc, stderr_fh)
    return True


def kill_adapter(name: str):
    """Kill inflight adapter process for a worker."""
    entry = _adapter_pids.pop(name, None)
    if entry is None:
        return
    proc, stderr_fh = entry
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
    if stderr_fh:
        try:
            stderr_fh.close()
        except OSError:
            pass


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

# Watchdog state
_worker_states = {}  # name -> (state, reason, since)
_last_child_ts = {}
_last_seen_claude = {}
_last_hook_ts = {}
_last_alert_ts = {}
_idle_streak = {}
_prev_worker_states = {}
_consecutive_probe_failures = {}
_watchdog_lock = threading.Lock()

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
    {"command": "pause", "description": "Pause focused worker"},
    {"command": "restart", "description": "Restart worker (--clean for fresh)"},
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

def read_checkin_note():
    """Read checkin note from file. Returns empty string if file missing."""
    try:
        path = _CHECKIN_NOTE_PATH
        if os.path.isfile(path):
            text = open(path).read().strip()
            if text:
                return text
    except Exception as e:
        print(f"Failed to read checkin note from {_CHECKIN_NOTE_PATH}: {e}")
    return ""


# Reserved names that cannot be used as worker names (would clash with commands)
RESERVED_NAMES = {
    # Bridge commands
    "team", "focus", "progress", "pause", "restart", "settings", "hire", "end",
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
        except urllib.error.HTTPError as e:
            print(f"Telegram API error: {e}")
            try:
                raw = e.read()
                body = json.loads(raw)
                return body  # Return error response so callers can inspect description
            except Exception:
                # Non-JSON error body (proxy, middlebox, empty) — return structured error
                return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code} (non-JSON body)"}
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

    def send_animation(self, chat_id: int, animation, **kwargs):
        payload = {"chat_id": chat_id, "animation": animation}
        payload.update(kwargs)
        return self.api("sendAnimation", payload)

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

# Allowed image extensions for outgoing (sendPhoto + sendAnimation + sendVideo)
ALLOWED_IMAGE_EXTENSIONS = {
    # Photos (sendPhoto)
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
    # Animations (sendAnimation) - autoplay, loop, silent
    ".gif", ".mp4",
}

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
    # Audio (sendAudio — shows player UI)
    ".mp3", ".m4a", ".flac", ".aac", ".wav",
    # Voice (sendVoice — shows voice bubble)
    ".ogg", ".opus", ".oga",
    # Video (sendVideo — shows video player)
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    # Stickers (sendSticker)
    ".tgs",
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

    Uses backend routing for tmux or non-interactive workers.
    """
    if not worker_manager.send(name, message):
        print(f"Warning: Cannot forward pipe message to '{name}' - worker not found")


def start_pipe_reader(name: str):
    """Start a background thread to read from the worker's input pipe."""
    if name in _pipe_reader_threads:
        thread, _stop = _pipe_reader_threads[name]
        if thread.is_alive():
            # Already running
            return
        # Thread crashed or exited — clean up stale entry and restart
        print(f"Pipe reader thread for '{name}' is dead, restarting")
        _pipe_reader_threads.pop(name, None)

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


def send_animation(chat_id, animation_path, caption=None):
    """Send an animation (GIF) to Telegram using multipart/form-data.

    SECURITY: Path is validated before sending.
    Returns True on success, False on failure.
    """
    if not BOT_TOKEN:
        return False

    ok, validated = validate_photo_path(animation_path)
    if not ok:
        print(validated)
        return False

    animation_path = validated

    boundary = uuid.uuid4().hex
    content_type = "video/mp4" if animation_path.suffix.lower() == ".mp4" else "image/gif"

    body_parts = []

    # chat_id field
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
    body_parts.append(b"")
    body_parts.append(str(chat_id).encode())

    # animation field
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(f'Content-Disposition: form-data; name="animation"; filename="{animation_path.name}"'.encode())
    body_parts.append(f"Content-Type: {content_type}".encode())
    body_parts.append(b"")
    body_parts.append(animation_path.read_bytes())

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
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendAnimation",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                print(f"Animation sent: {animation_path.name}")
                return True
            else:
                print(f"sendAnimation failed: {result}")
                return False
    except Exception as e:
        print(f"sendAnimation error: {e}")
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


def _send_media_multipart(chat_id, file_path, field_name, api_method, caption=None):
    """Generic multipart upload for any Telegram media method.

    Args:
        chat_id: Telegram chat ID
        file_path: Path object to the file
        field_name: API field name (e.g. "video", "audio", "voice")
        api_method: API method (e.g. "sendVideo", "sendAudio", "sendVoice")
        caption: Optional caption
    Returns True on success.
    """
    if not BOT_TOKEN:
        return False

    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

    body_parts = []
    body_parts.append(f"--{boundary}".encode())
    body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
    body_parts.append(b"")
    body_parts.append(str(chat_id).encode())

    body_parts.append(f"--{boundary}".encode())
    body_parts.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"'.encode())
    body_parts.append(f"Content-Type: {content_type}".encode())
    body_parts.append(b"")
    body_parts.append(file_path.read_bytes())

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
            f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
            if result.get("ok"):
                print(f"{api_method} sent: {file_path.name}")
                return True
            else:
                print(f"{api_method} failed: {result}")
                return False
    except Exception as e:
        print(f"{api_method} error: {e}")
        return False


# Media extensions routed to specialized Telegram API methods
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".aac", ".wav"}
VOICE_EXTENSIONS = {".ogg", ".opus", ".oga"}
STICKER_EXTENSIONS = {".tgs"}  # animated stickers; static .webp handled by sendPhoto


def send_video(chat_id, video_path, caption=None):
    """Send video via sendVideo (shows player with controls)."""
    ok, validated = validate_document_path(video_path)
    if not ok:
        print(validated)
        return False
    return _send_media_multipart(chat_id, validated, "video", "sendVideo", caption)


def send_audio(chat_id, audio_path, caption=None):
    """Send audio via sendAudio (shows audio player UI)."""
    ok, validated = validate_document_path(audio_path)
    if not ok:
        print(validated)
        return False
    return _send_media_multipart(chat_id, validated, "audio", "sendAudio", caption)


def send_voice(chat_id, voice_path, caption=None):
    """Send voice message via sendVoice (shows voice bubble)."""
    ok, validated = validate_document_path(voice_path)
    if not ok:
        print(validated)
        return False
    return _send_media_multipart(chat_id, validated, "voice", "sendVoice", caption)


def send_sticker(chat_id, sticker_path):
    """Send sticker via sendSticker."""
    sticker_path = Path(sticker_path)
    if not sticker_path.exists() or not sticker_path.is_file():
        print(f"Sticker not found: {sticker_path}")
        return False
    return _send_media_multipart(chat_id, sticker_path, "sticker", "sendSticker")


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


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML using markdown-it-py.

    Handles: bold, italic, strikethrough, code, code blocks, links,
    blockquotes, headings (as bold), lists, tables (as bullet lists), hr.
    Unrecognized tokens degrade to plain text.
    """
    import re
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable("strikethrough").enable("table")
    tokens = md.parse(text)

    result = []
    list_depth = 0
    ordered_counter = []  # stack of counters for ordered lists
    in_table = False
    table_row = []  # current row cells
    table_headers = []  # header cells
    table_rows = []  # all data rows
    in_thead = False
    _rejected_open_tags = []

    class _TelegramHTMLSanitizer(HTMLParser):
        SAFE_TAGS = frozenset({
            "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
            "code", "pre", "a", "blockquote", "span", "tg-emoji", "tg-spoiler",
        })
        SAFE_ATTRS = {
            "a": frozenset({"href"}),
            "code": frozenset({"class"}),
            "blockquote": frozenset({"expandable"}),
            "span": frozenset({"class"}),
            "tg-emoji": frozenset({"emoji-id"}),
        }

        def __init__(self, rejected_open_tags):
            super().__init__(convert_charrefs=False)
            self._out = []
            self._rejected_open_tags = rejected_open_tags

        def _escape_attr(self, value):
            return escape_html(value).replace('"', "&quot;")

        def _attrs_are_safe(self, tag, attrs):
            allowed = self.SAFE_ATTRS.get(tag, frozenset())
            seen = set()
            for name, value in attrs:
                if name in seen or name not in allowed:
                    return False
                seen.add(name)
                if tag == "a" and name == "href":
                    if value is None:
                        return False
                elif tag == "code" and name == "class":
                    if value is None or not value.startswith("language-"):
                        return False
                elif tag == "blockquote" and name == "expandable":
                    if value not in (None, "", "expandable"):
                        return False
                elif tag == "span" and name == "class":
                    if value != "tg-spoiler":
                        return False
                elif tag == "tg-emoji" and name == "emoji-id":
                    if value is None:
                        return False
            return True

        def _render_start_tag(self, tag, attrs):
            if not attrs:
                return f"<{tag}>"
            rendered = []
            for name, value in attrs:
                if value is None:
                    rendered.append(name)
                else:
                    rendered.append(f'{name}="{self._escape_attr(value)}"')
            return f"<{tag} {' '.join(rendered)}>"

        def handle_starttag(self, tag, attrs):
            accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
            if accepted:
                self._out.append(self._render_start_tag(tag, attrs))
            else:
                self._out.append(escape_html(self.get_starttag_text() or f"<{tag}>"))
                # Track rejected start tags so matching closing tags are escaped too.
                self._rejected_open_tags.append(tag)

        def handle_endtag(self, tag):
            rejected_match = False
            for idx in range(len(self._rejected_open_tags) - 1, -1, -1):
                if self._rejected_open_tags[idx] == tag:
                    rejected_match = True
                    del self._rejected_open_tags[idx]
                    break
            if tag in self.SAFE_TAGS and not rejected_match:
                self._out.append(f"</{tag}>")
            else:
                self._out.append(escape_html(f"</{tag}>"))

        def handle_startendtag(self, tag, attrs):
            accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
            if accepted:
                start = self._render_start_tag(tag, attrs)
                self._out.append(f"{start[:-1]}/>")
            else:
                self._out.append(escape_html(self.get_starttag_text() or f"<{tag}/>"))

        def handle_data(self, data):
            self._out.append(escape_html(data))

        def handle_entityref(self, name):
            self._out.append(f"&{name};")

        def handle_charref(self, name):
            self._out.append(f"&#{name};")

        def handle_comment(self, data):
            self._out.append(escape_html(f"<!--{data}-->"))

        def html(self):
            return "".join(self._out)

    def _sanitize_html(raw):
        if not raw:
            return ""
        sanitizer = _TelegramHTMLSanitizer(_rejected_open_tags)
        sanitizer.feed(raw)
        sanitizer.close()
        return sanitizer.html()

    def _render_inline_plain(children):
        """Render inline token children to plain text (for use inside <pre>)."""
        out = []
        for tok in children:
            if tok.type in ("text", "code_inline"):
                out.append(tok.content)
            elif tok.type in ("softbreak", "hardbreak"):
                out.append(" ")
            elif tok.type == "image":
                out.append(tok.content or "image")
            elif tok.type in ("strong_open", "strong_close", "em_open", "em_close",
                              "s_open", "s_close", "link_open", "link_close",
                              "html_inline"):
                pass
            else:
                if tok.content:
                    out.append(tok.content)
        return "".join(out)

    def _render_inline(children):
        """Render inline token children to HTML string."""
        out = []
        for tok in children:
            if tok.type == "text":
                out.append(escape_html(tok.content))
            elif tok.type == "code_inline":
                out.append(f"<code>{escape_html(tok.content)}</code>")
            elif tok.type == "strong_open":
                out.append("<b>")
            elif tok.type == "strong_close":
                out.append("</b>")
            elif tok.type == "em_open":
                out.append("<i>")
            elif tok.type == "em_close":
                out.append("</i>")
            elif tok.type == "s_open":
                out.append("<s>")
            elif tok.type == "s_close":
                out.append("</s>")
            elif tok.type == "link_open":
                href = escape_html((tok.attrs or {}).get("href", ""))
                out.append(f'<a href="{href}">')
            elif tok.type == "link_close":
                out.append("</a>")
            elif tok.type == "softbreak":
                out.append("\n")
            elif tok.type == "hardbreak":
                out.append("\n")
            elif tok.type == "image":
                alt = escape_html(tok.content or "image")
                src = escape_html((tok.attrs or {}).get("src", ""))
                out.append(f'[{alt}]({src})')
            elif tok.type == "html_inline":
                out.append(_sanitize_html(tok.content))
            else:
                if tok.content:
                    out.append(escape_html(tok.content))
        return "".join(out)

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "paragraph_open":
            pass
        elif tok.type == "paragraph_close":
            if not in_table:
                result.append("\n")
        elif tok.type == "inline":
            if in_table:
                table_row.append(_render_inline_plain(tok.children or []))
            else:
                result.append(_render_inline(tok.children or []))

        # Headings -> bold
        elif tok.type == "heading_open":
            result.append("<b>")
        elif tok.type == "heading_close":
            result.append("</b>\n")

        # Code blocks
        elif tok.type == "fence":
            lang = tok.info.strip() if tok.info else ""
            code = escape_html(tok.content.rstrip("\n"))
            if lang:
                result.append(f'<pre><code class="language-{escape_html(lang)}">{code}</code></pre>\n')
            else:
                result.append(f"<pre>{code}</pre>\n")
        elif tok.type == "code_block":
            code = escape_html(tok.content.rstrip("\n"))
            result.append(f"<pre>{code}</pre>\n")

        # Blockquotes
        elif tok.type == "blockquote_open":
            result.append("<blockquote>")
        elif tok.type == "blockquote_close":
            if result and result[-1].endswith("\n"):
                result[-1] = result[-1][:-1]
            result.append("</blockquote>\n")

        # Bullet lists
        elif tok.type == "bullet_list_open":
            list_depth += 1
        elif tok.type == "bullet_list_close":
            list_depth -= 1
            if list_depth == 0:
                result.append("\n")

        # Ordered lists
        elif tok.type == "ordered_list_open":
            list_depth += 1
            ordered_counter.append(0)
        elif tok.type == "ordered_list_close":
            list_depth -= 1
            ordered_counter.pop()
            if list_depth == 0:
                result.append("\n")

        # List items
        elif tok.type == "list_item_open":
            indent = "  " * (list_depth - 1)
            if ordered_counter:
                ordered_counter[-1] += 1
                result.append(f"{indent}{ordered_counter[-1]}. ")
            else:
                result.append(f"{indent}\u2022 ")
        elif tok.type == "list_item_close":
            if result and not result[-1].endswith("\n"):
                result.append("\n")

        # Tables -> <pre> aligned columns
        elif tok.type == "table_open":
            in_table = True
            table_headers = []
            table_rows = []
        elif tok.type == "table_close":
            in_table = False
            # Render as <pre> with aligned columns
            all_rows = [table_headers] + table_rows
            if all_rows and all_rows[0]:
                num_cols = max(len(r) for r in all_rows)
                col_widths = [0] * num_cols
                for row in all_rows:
                    for ci, cell in enumerate(row):
                        if ci < num_cols:
                            col_widths[ci] = max(col_widths[ci], len(cell))
                lines = []
                for ri, row in enumerate(all_rows):
                    cols = []
                    for ci in range(num_cols):
                        cell = row[ci] if ci < len(row) else ""
                        cols.append(escape_html(cell.ljust(col_widths[ci])))
                    lines.append("  ".join(cols).rstrip())
                    if ri == 0:
                        lines.append("\u2550" * (sum(col_widths) + 2 * (num_cols - 1)))
                result.append(f"<pre>{''.join(chr(10).join(lines))}</pre>\n")
            table_headers = []
            table_rows = []
        elif tok.type == "thead_open":
            in_thead = True
        elif tok.type == "thead_close":
            in_thead = False
        elif tok.type in ("tbody_open", "tbody_close"):
            pass
        elif tok.type == "tr_open":
            table_row = []
        elif tok.type == "tr_close":
            if in_thead:
                table_headers = table_row[:]
            else:
                table_rows.append(table_row[:])
            table_row = []
        elif tok.type in ("th_open", "th_close", "td_open", "td_close"):
            pass

        # Horizontal rule
        elif tok.type == "hr":
            result.append("\u2014\u2014\u2014\u2014\n")

        # HTML blocks
        elif tok.type == "html_block":
            result.append(_sanitize_html(tok.content))

        else:
            if tok.content:
                result.append(escape_html(tok.content))

        i += 1

    output = "".join(result).strip()
    while "\n\n\n" in output:
        output = output.replace("\n\n\n", "\n\n")

    # Post-process: wrap runs of plain-text tabular lines in <pre>.
    # Detects lines with 2+ internal multi-space gaps (column alignment)
    # that are NOT already inside <pre> tags.
    _MULTI_SPACE = re.compile(r'\S  +\S.*\S  +\S')  # 2+ columns with 2+ space gaps

    def _wrap_plain_tables(text):
        """Find consecutive tabular lines outside <pre> and wrap in <pre>."""
        parts = re.split(r'(<pre>.*?</pre>)', text, flags=re.DOTALL)
        out = []
        for part in parts:
            if part.startswith('<pre>'):
                out.append(part)
                continue
            lines = part.split('\n')
            i = 0
            while i < len(lines):
                if _MULTI_SPACE.search(lines[i]):
                    # Start of tabular run
                    run = [lines[i]]
                    j = i + 1
                    while j < len(lines) and (_MULTI_SPACE.search(lines[j]) or lines[j].strip() == ''):
                        run.append(lines[j])
                        j += 1
                    # Only wrap if 2+ tabular lines
                    tabular_count = sum(1 for l in run if _MULTI_SPACE.search(l))
                    if tabular_count >= 2:
                        # Strip trailing empty lines from run
                        while run and run[-1].strip() == '':
                            j -= 1
                            run.pop()
                        content = escape_html('\n'.join(run))
                        out.append(f'<pre>{content}</pre>')
                        i = j
                    else:
                        out.append(lines[i])
                        i += 1
                else:
                    out.append(lines[i])
                    i += 1
            # Rejoin non-pre parts with newlines (but parts list alternates)
            if out and not out[-1].startswith('<pre>') and not part.startswith('<pre>'):
                pass  # already appended line by line
        # Reconstruct: join lines that aren't pre blocks
        # Actually, simpler approach: rebuild from parts
        return '\n'.join(out) if out else text

    output = _wrap_plain_tables(output)
    return output


def format_response_text(session_name, text):
    """Format response with session prefix. No escaping - Claude Code handles safety."""
    # Strip redundant worker name prefix to avoid "lee:\nlee: message" double prefix
    stripped = text.lstrip()
    prefix = f"{session_name}:"
    if stripped.lower().startswith(prefix.lower()):
        text = stripped[len(prefix):].lstrip()
    return f"<b>{session_name}:</b>\n{text}"


# ─────────────────────────────────────────────────────────────────────────────
# Message Splitting (Telegram 4096 char limit)
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_MAX_LENGTH = 4096


def split_message(text, max_len=TELEGRAM_MAX_LENGTH):
    """Split HTML text into chunks that fit within Telegram's message limit.

    HTML-aware: tracks open tags and closes/reopens them at split boundaries.
    Splits on safe boundaries: blank lines → newlines → spaces → hard cut.
    Returns list of valid HTML text chunks.
    """
    import re
    if len(text) <= max_len:
        return [text]

    # Regex for Telegram-supported HTML tags
    TAG_RE = re.compile(r'<(/?)(\w+)([^>]*)>')
    TRACKED_TAGS = frozenset(('b', 'i', 's', 'u', 'code', 'pre', 'a',
                              'strong', 'em', 'del', 'ins', 'strike', 'blockquote'))

    def _closing_tags(stack):
        """Generate closing tags for all open tags (reverse order)."""
        return "".join(f"</{tag}>" for tag, _ in reversed(stack))

    def _opening_tags(stack):
        """Generate opening tags for all open tags (original order)."""
        return "".join(full for _, full in stack)

    def _scan_tags(text):
        """Return the tag stack state after scanning text."""
        stack = []
        for m in TAG_RE.finditer(text):
            is_close = m.group(1) == '/'
            tag_name = m.group(2).lower()
            if tag_name not in TRACKED_TAGS:
                continue
            if is_close:
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j][0] == tag_name:
                        stack.pop(j)
                        break
            else:
                stack.append((tag_name, m.group(0)))
        return stack

    def _find_split(text, budget):
        """Find best split point within budget chars.

        Priority: blank line → newline → space → hard cut.
        Avoids splitting inside HTML tags. Always returns >= 1.
        """
        if budget <= 0:
            budget = 1
        search = text[:budget]

        # Don't split inside a tag — find last '>' before budget
        last_tag_start = search.rfind('<')
        last_tag_end = search.rfind('>')
        if last_tag_start > last_tag_end:
            search = text[:last_tag_start]
            budget = last_tag_start

        for sep in ('\n\n', '\n', ' '):
            pos = search.rfind(sep)
            if pos > budget // 3:
                return pos + 1

        return max(budget, 1)  # Guarantee forward progress

    chunks = []
    remaining = text
    carry_stack = []  # Tags open from previous chunk

    while remaining:
        prefix = _opening_tags(carry_stack)
        available = max_len - len(prefix)

        # Close carry tags in final chunk too
        if len(prefix) + len(remaining) + len(_closing_tags(carry_stack)) <= max_len:
            suffix = _closing_tags(_scan_tags(prefix + remaining))
            chunks.append(prefix + remaining + suffix)
            break

        # Find split point with iterative backoff to guarantee max_len
        budget = available - 100  # Initial conservative reserve
        if budget < 100:
            budget = 100

        for _attempt in range(5):
            split_at = _find_split(remaining, budget)
            chunk_text = remaining[:split_at].rstrip()
            full_chunk = prefix + chunk_text
            open_stack = _scan_tags(full_chunk)
            suffix = _closing_tags(open_stack)

            if len(full_chunk) + len(suffix) <= max_len:
                break
            # Shrink budget and retry
            overshoot = len(full_chunk) + len(suffix) - max_len
            budget = max(budget - overshoot - 20, 100)
        else:
            # Last resort: hard cut to fit
            hard_limit = max_len - len(prefix) - len(suffix) - 10
            if hard_limit < 1:
                hard_limit = 1
            chunk_text = remaining[:hard_limit].rstrip()
            full_chunk = prefix + chunk_text
            open_stack = _scan_tags(full_chunk)
            suffix = _closing_tags(open_stack)
            split_at = hard_limit

        chunks.append(full_chunk + suffix)
        carry_stack = open_stack
        remaining = remaining[split_at:].lstrip()

        # Safety: prevent infinite loop
        if split_at == 0:
            # Force progress by consuming at least 1 char
            remaining = remaining[1:]

    return chunks


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


def get_claude_session_id(name):
    f = get_session_dir(name) / "claude_session_id"
    if f.exists():
        return f.read_text().strip()
    return ""


def get_claude_session_cwd(name):
    f = get_session_dir(name) / "claude_session_cwd"
    if f.exists():
        return f.read_text().strip()
    return ""


def save_claude_session_cwd(name, cwd):
    d = ensure_session_dir(name)
    f = d / "claude_session_cwd"
    f.write_text(cwd)
    f.chmod(0o600)


def clear_claude_session_id(name):
    f = get_session_dir(name) / "claude_session_id"
    if f.exists():
        f.unlink()


def get_any_session_id(name):
    """Get any *_session_id value for a worker (backend-agnostic).

    Returns (session_id, source) tuple where source is the prefix (e.g. 'claude', 'codex').
    """
    session_dir = get_session_dir(name)
    if not session_dir.exists():
        return "", ""
    for f in sorted(session_dir.glob("*_session_id")):
        val = f.read_text().strip()
        if val:
            source = f.name.replace("_session_id", "")
            return val, source
    return "", ""


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
        if (time.time() - ts) > PENDING_TIMEOUT:  # 10 min timeout - auto-clear stale pending
            pending.unlink()
            return False
        return True
    except:
        return False


def _pending_timestamp(name: str) -> Optional[int]:
    pending = get_pending_file(name)
    if not pending.exists():
        return None
    try:
        return int(pending.read_text().strip())
    except Exception:
        return None


def compute_state(
    tmux_exists: bool,
    claude_pid: Optional[str],
    pending: bool,
    pending_ts: Optional[int],
    pending_age: float,
    children: int,
    last_child_ts: float,
    cpu: float,
    last_hook_ts: Optional[float],
    last_seen_claude: Optional[float],
    now: float,
    is_interactive: bool = True,
    adapter_alive: bool = False,
    poisoned_reason: Optional[str] = None,
) -> tuple[str, str]:
    if not tmux_exists:
        return "OFFLINE", "tmux missing"

    if not is_interactive:
        if adapter_alive:
            return "BUSY_TOOL", "adapter running"
        if pending:
            if pending_age < STALE_PENDING:
                return "WAITING", f"age={int(pending_age)}s"
            hook_since_pending = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
            if pending_age >= STALE_PENDING and not hook_since_pending:
                if poisoned_reason is not None:
                    return "POISONED", f"{poisoned_reason}"
                return "STUCK", f"age={int(pending_age)}s"
            return "WAITING", f"age={int(pending_age)}s"
        return "READY", "idle"

    if not claude_pid and last_seen_claude is not None:
        if (now - last_seen_claude) > START_GRACE:
            return "DEAD", f"claude missing {int(now - last_seen_claude)}s"

    if pending and children > 0:
        return "BUSY_TOOL", f"children={children}"

    if pending and children == 0:
        if (pending_age <= THINK_GRACE) or ((now - last_child_ts) <= TOOL_GAP_GRACE) or (cpu >= CPU_ACTIVE):
            return "BUSY_THINKING", f"age={int(pending_age)}s cpu={cpu:.1f}"
        if pending_age < STALE_PENDING:
            return "WAITING", f"age={int(pending_age)}s"
        hook_since_pending = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
        if pending_age >= STALE_PENDING and cpu < CPU_IDLE and not hook_since_pending:
            if poisoned_reason is not None:
                return "POISONED", f"{poisoned_reason}"
            return "STUCK", f"age={int(pending_age)}s cpu={cpu:.1f}"
        return "WAITING", f"age={int(pending_age)}s"

    if not pending and children > 0:
        return "UNTRACKED_BUSY", f"children={children}"

    if claude_pid and not pending:
        return "READY", "idle"

    return "OFFLINE", "tmux alive, claude missing"


POISON_PATTERNS = [
    re.compile(r"error.*overloaded", re.IGNORECASE),
    re.compile(r"error.*401", re.IGNORECASE),
    re.compile(r"error.*403", re.IGNORECASE),
    re.compile(r"error.*429", re.IGNORECASE),
    re.compile(r"image.*dimensions.*exceed", re.IGNORECASE),
    re.compile(r"context.*(length|window).*exceed", re.IGNORECASE),
    re.compile(r"context_length_exceeded", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"invalid.*api.?key", re.IGNORECASE),
    re.compile(r"invalid_request_error", re.IGNORECASE),
    re.compile(r"insufficient_quota", re.IGNORECASE),
    re.compile(r"model.*not.*found", re.IGNORECASE),
    re.compile(r"APIError", re.IGNORECASE),
    re.compile(r"connection.*reset", re.IGNORECASE),
    re.compile(r"timeout.*error", re.IGNORECASE),
    re.compile(r"error.*529", re.IGNORECASE),
    re.compile(r"error.*503", re.IGNORECASE),
]


def _capture_pane_text(tmux_name: str, lines: int = 50) -> str:
    """Return the last N lines of a tmux pane, or empty string on error."""
    if lines <= 0:
        return ""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _check_adapter_log(name: str, tail_lines: int = 20) -> str:
    """Read the last N lines of adapter.log for a worker, or empty string."""
    if tail_lines <= 0:
        return ""
    log_path = get_session_dir(name) / "adapter.log"
    if not log_path.exists():
        return ""
    try:
        with log_path.open("r", errors="ignore") as fh:
            lines = fh.readlines()
        return "".join(lines[-tail_lines:])
    except Exception:
        return ""


def _detect_poisoned(name: str, tmux_name: str) -> Optional[str]:
    backend_name = get_worker_backend(name)
    backend = get_backend(backend_name)
    text_parts = []
    if backend.is_interactive:
        text_parts.append(_capture_pane_text(tmux_name))
    else:
        text_parts.append(_check_adapter_log(name))
    combined = "\n".join([part for part in text_parts if part])
    if not combined:
        return None
    for pattern in POISON_PATTERNS:
        if len(pattern.findall(combined)) >= 3:
            return pattern.pattern
    return None


def _send_watchdog_alert(name: str, state: str, reason: str) -> None:
    if admin_chat_id is None:
        return

    now = time.time()
    with _watchdog_lock:
        last = _last_alert_ts.get(name)
    if last and (now - last) < ALERT_COOLDOWN:
        return

    actions = {
        "OFFLINE": f"/hire {name}",
        "DEAD": f"/restart --clean {name}",
        "STUCK": f"/restart --clean {name}",
        "POISONED": f"/restart --clean {name} (context may be poisoned)",
    }
    action = actions.get(state, "check /team")
    text = f"[watchdog] {name}: {state} ({reason}). Suggested: {action}"
    try:
        telegram_api("sendMessage", {"chat_id": admin_chat_id, "text": text})
        with _watchdog_lock:
            _last_alert_ts[name] = now
    except Exception as e:
        print(f"Watchdog alert error: {e}")


def _send_resolved_alert(name: str, new_state: str) -> None:
    if admin_chat_id is None:
        return

    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED"}
    with _watchdog_lock:
        prev_state = _prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states:
        return

    text = f"[watchdog] {name}: resolved -> {new_state}"
    try:
        telegram_api("sendMessage", {"chat_id": admin_chat_id, "text": text})
    except Exception as e:
        print(f"Watchdog resolved alert error: {e}")


def _handle_watchdog_transition(
    name: str,
    state: str,
    reason: str,
    since: float,
    now: Optional[float] = None,
) -> None:
    if now is None:
        now = time.time()

    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with _watchdog_lock:
        prev_state = _prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state

    def eligible_for_alert() -> bool:
        if state in {"OFFLINE", "DEAD"}:
            return since is not None and (now - since) >= START_GRACE
        return True

    if state in bad_states:
        if state_changed:
            if eligible_for_alert():
                _send_watchdog_alert(name, state, reason)
        elif state in {"OFFLINE", "DEAD"} and eligible_for_alert():
            _send_watchdog_alert(name, state, reason)

    if state_changed and prev_state in bad_states and state in good_states:
        _send_resolved_alert(name, state)

    with _watchdog_lock:
        _prev_worker_states[name] = state


def _record_worker_state(name: str, state: str, reason: str, now: float) -> float:
    """Update worker state and preserve since for unchanged states."""
    with _watchdog_lock:
        prev = _worker_states.get(name)
        if prev and prev[0] == state:
            since = prev[2]
        else:
            since = now
        _worker_states[name] = (state, reason, since)
    return since


def watchdog_loop():
    while True:
        try:
            now = time.time()
            registered = get_registered_sessions()
            pane_pids = _tmux_pane_pids()

            registered_names = set(registered.keys())
            probe_failed = bool(registered_names) and not pane_pids
            if probe_failed:
                for name in registered_names:
                    _consecutive_probe_failures[name] = _consecutive_probe_failures.get(name, 0) + 1
            else:
                for name in registered_names:
                    _consecutive_probe_failures[name] = 0

            claude_pids = {}
            tmux_present = {}
            backend_info = {}

            for name, session in registered.items():
                backend_name = get_worker_backend(name, session)
                backend = get_backend(backend_name)
                backend_info[name] = backend
                tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
                pane_pid = pane_pids.get(tmux_name)
                tmux_exists = bool(pane_pid)
                tmux_present[name] = tmux_exists

                if not tmux_exists:
                    continue

                if backend.is_interactive:
                    claude_pid = _get_claude_pid(pane_pid)
                    if claude_pid:
                        claude_pids[name] = claude_pid
                        with _watchdog_lock:
                            _last_seen_claude[name] = now
                    else:
                        with _watchdog_lock:
                            if name not in _last_seen_claude:
                                _last_seen_claude[name] = now

            stats = _ps_stats(claude_pids.values())

            for name, session in registered.items():
                tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
                tmux_exists = tmux_present.get(name, False)
                if probe_failed and not tmux_exists and _consecutive_probe_failures.get(name, 0) < 3:
                    continue

                backend = backend_info.get(name)
                if backend is None:
                    backend_name = get_worker_backend(name, session)
                    backend = get_backend(backend_name)
                is_interactive = backend.is_interactive

                adapter_alive = False
                if not is_interactive:
                    entry = _adapter_pids.get(name)
                    if entry:
                        proc, _stderr = entry
                        adapter_alive = proc.poll() is None

                claude_pid = claude_pids.get(name) if is_interactive else None
                cpu = 0.0
                if claude_pid and claude_pid in stats:
                    cpu = stats[claude_pid].get("cpu", 0.0)

                children = _child_count(claude_pid) if claude_pid else 0
                if children > 0:
                    with _watchdog_lock:
                        _last_child_ts[name] = now

                pending_ts = _pending_timestamp(name)
                pending = pending_ts is not None
                pending_age = now - pending_ts if pending_ts else 0.0
                with _watchdog_lock:
                    last_child_ts = _last_child_ts.get(name, 0.0)
                    last_hook_ts = _last_hook_ts.get(name)
                    last_seen_claude = _last_seen_claude.get(name)
                if not is_interactive:
                    last_seen_claude = None

                state_args = dict(
                    tmux_exists=tmux_exists,
                    claude_pid=claude_pid,
                    pending=pending,
                    pending_ts=pending_ts,
                    pending_age=pending_age,
                    children=children,
                    last_child_ts=last_child_ts,
                    cpu=cpu,
                    last_hook_ts=last_hook_ts,
                    last_seen_claude=last_seen_claude,
                    now=now,
                    is_interactive=is_interactive,
                    adapter_alive=adapter_alive,
                )
                state, reason = compute_state(**state_args)

                if state == "STUCK":
                    _idle_streak[name] = _idle_streak.get(name, 0) + 1
                    streak = _idle_streak[name]
                    if streak < IDLE_STREAK_STUCK:
                        state = "WAITING"
                    else:
                        poisoned_reason = _detect_poisoned(name, tmux_name)
                        state, reason = compute_state(
                            **state_args,
                            poisoned_reason=poisoned_reason
                        )
                    reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
                elif state == "POISONED":
                    streak = _idle_streak.get(name, 0)
                    if streak:
                        reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
                else:
                    _idle_streak[name] = 0

                since = _record_worker_state(name, state, reason, now)
                _handle_watchdog_transition(name, state, reason, since, now=now)

            with _watchdog_lock:
                for name in list(_worker_states.keys()):
                    if name not in registered_names:
                        _worker_states.pop(name, None)
                for name in list(_last_child_ts.keys()):
                    if name not in registered_names:
                        _last_child_ts.pop(name, None)
                for name in list(_last_seen_claude.keys()):
                    if name not in registered_names:
                        _last_seen_claude.pop(name, None)
                for name in list(_last_hook_ts.keys()):
                    if name not in registered_names:
                        _last_hook_ts.pop(name, None)
                for name in list(_prev_worker_states.keys()):
                    if name not in registered_names:
                        _prev_worker_states.pop(name, None)
                for name in list(_last_alert_ts.keys()):
                    if name not in registered_names:
                        _last_alert_ts.pop(name, None)
                for name in list(_idle_streak.keys()):
                    if name not in registered_names:
                        _idle_streak.pop(name, None)
            for name in list(_consecutive_probe_failures.keys()):
                if name not in registered_names:
                    _consecutive_probe_failures.pop(name, None)
        except Exception as e:
            print(f"Watchdog error: {e}")

        time.sleep(WATCHDOG_INTERVAL)


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


def _format_watchdog_status(name: str, pending_lookup=None, state_snapshot: Optional[dict] = None) -> str:
    if pending_lookup is None:
        pending_lookup = is_pending

    if state_snapshot is None:
        with _watchdog_lock:
            entry = _worker_states.get(name)
    else:
        entry = state_snapshot.get(name)
    if not entry:
        return "working" if pending_lookup(name) else "available"

    state, _reason, since = entry
    now = time.time()

    if state == "READY":
        return "ready"
    if state == "BUSY_TOOL":
        return "working (tools)"
    if state == "BUSY_THINKING":
        return "working (thinking)"
    if state == "WAITING":
        return "working (waiting)"
    if state == "STUCK":
        minutes = max(0, int((now - since) / 60)) if since else 0
        return f"STUCK {minutes}m"
    if state == "POISONED":
        minutes = max(0, int((now - since) / 60)) if since else 0
        return f"POISONED {minutes}m"
    if state == "DEAD":
        return "DEAD"
    if state == "OFFLINE":
        return "offline"
    if state == "UNTRACKED_BUSY":
        return "working (untracked)"
    return state.lower()


def format_team_lines(registered: dict, active: Optional[str], pending_lookup=None) -> list[str]:
    """Format /team response lines (backend-aware)."""
    if pending_lookup is None:
        pending_lookup = is_pending

    lines = []
    lines.append("Your team:")
    lines.append(f"Focused: {active or '(none)'}")
    lines.append("Workers:")
    with _watchdog_lock:
        state_snapshot = dict(_worker_states)
    for name in sorted(registered.keys()):
        session = registered[name]
        backend = normalize_backend(session.get("backend"))
        status = []
        if name == active:
            status.append("focused")
        watchdog_status = _format_watchdog_status(name, pending_lookup, state_snapshot=state_snapshot)
        status.append(watchdog_status)
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
    resume_line: Optional[str] = None,
    continuity_line: Optional[str] = None,
    needs_attention: Optional[str] = None
) -> list[str]:
    """Format /progress response lines (backend-aware)."""
    status = []
    status.append(f"Progress for focused worker: {name}")
    status.append("Focused: yes")
    status.append(f"Working: {'yes' if pending else 'no'}")
    watchdog_status = _format_watchdog_status(name)
    status.append(f"State: {watchdog_status}")
    status.append(f"Backend: {backend}")
    status.append(f"Online: {'yes' if online else 'no'}")
    status.append(f"Ready: {'yes' if ready else 'no'}")
    if continuity_line:
        status.append(continuity_line)
    elif resume_line:
        status.append(resume_line)
    if needs_attention:
        status.append(f"Needs attention: {needs_attention}")
    status.append(f"Mode: {mode}")
    return status


def get_worker_backend(name: str, session: Optional[dict] = None) -> str:
    """Get backend for a worker."""
    # Check session dict first
    if session and session.get("backend"):
        return normalize_backend(session.get("backend"))
    # Check backend file in session dir (for non-interactive mode workers)
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
        """Get registered sessions from tmux (all backends have tmux now)."""
        self._sync_paths()
        if registered is None:
            registered = self.scan_tmux_sessions()

        # Fallback: pick up non-interactive workers with backend file but orphaned tmux
        if self.sessions_dir.exists():
            for session_dir in self.sessions_dir.iterdir():
                if session_dir.is_dir():
                    backend_file = session_dir / "backend"
                    if backend_file.exists():
                        name = session_dir.name
                        if name not in registered:
                            backend = backend_file.read_text().strip()
                            registered[name] = {"backend": backend}

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
            if not backend.is_interactive:
                pipe_path = ensure_worker_pipe(name)
                workers.append({
                    "name": name,
                    "protocol": "pipe",
                    "address": str(pipe_path),
                    "send_example": f"echo 'YOUR_NAME: your message here' > {pipe_path} &",
                    "note": "Non-interactive. IMPORTANT: Always prefix your name (e.g., 'kenji: hello'). Always use & (background) when writing to pipe — it BLOCKS until read. Never use cat/echo without & or your session will freeze."
                })
            else:
                tmux_name = info.get("tmux")
                if not tmux_name:
                    continue
                workers.append({
                    "name": name,
                    "protocol": "tmux",
                    "address": tmux_name,
                    "send_example": f"tmux send-keys -t {tmux_name} 'YOUR_NAME: your message here' Enter && sleep 1"
                })
        return workers

    def _build_welcome(self, name: str, backend_obj) -> str:
        """Build welcome/instructions message for a worker."""
        welcome = (
            "You are connected to Telegram via claudecode-telegram bridge. "
            "RECEIVING FILES: Manager sends files (images, PDFs, documents) — they appear as local paths you can read directly. "
            "SENDING FILES: Use [[image:/path/to/photo.png|caption]] for images (jpg/png/webp/bmp) and animations (gif/mp4), or [[file:/path/to/file|caption]] for documents, video (mp4/mov/avi — shows player), audio (mp3/m4a/flac — shows player), and voice (ogg/opus — voice bubble). "
            "MESSAGING WORKERS: Run `curl -s $BRIDGE_URL/workers` to discover other workers — returns names, protocols, and ready-to-use send commands. Always call /workers before messaging, never guess addresses. "
            f"NAME PREFIX: Always prefix your name in messages (e.g., '{name}: your message'). "
            f"REFRESH INSTRUCTIONS: Run `curl -s $BRIDGE_URL/checkin?name={name}` to re-read these instructions anytime. "
            "WARNING: Do NOT output worker messages normally — they go to Telegram. Use the send commands from /workers instead."
        )
        if not backend_obj.is_interactive:
            welcome += (
                " NON-INTERACTIVE MODE: Your bridge URL is in $BRIDGE_URL env var. "
                "Each message triggers a blocking CLI call, responses arrive async in Telegram. "
                "Use nohup/& if calling CLI directly."
            )
        if SANDBOX_ENABLED and backend_obj.is_interactive:
            welcome += " Running in sandbox mode (Docker container)."

        # Append manager note if set (with {name} substitution)
        note = read_checkin_note()
        if note:
            rendered = note.replace("{name}", name)
            welcome += f"\n\nMANAGER NOTE:\n{rendered}"
            print(f"Checkin note included for {name}")

        return welcome

    def hire(self, name: str, backend: str = DEFAULT_BACKEND, chat_id: int = None):
        """Create a new worker instance."""
        self._sync_paths()
        if not is_valid_backend(backend):
            return False, f"Unknown backend '{backend}'. Available: {', '.join(list_backends())}"

        backend_obj = get_backend(backend)

        # Check binary exists before creating tmux session
        if not shutil.which(backend_obj.binary):
            return False, f"'{backend_obj.binary}' not found in PATH. Install it first."

        tmux_name = f"{self.tmux_prefix}{name}"
        if tmux_exists(tmux_name):
            return False, f"Worker '{name}' already exists"

        # Strip CLAUDECODE from env so new tmux shell doesn't inherit it
        # (Claude Code refuses to start if it detects a parent session)
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env
        )
        if result.returncode != 0:
            return False, "Could not start the worker workspace"

        time.sleep(0.5)

        # After tmux new-session succeeds, capture the pane's cwd
        pane_cwd = ""
        result = subprocess.run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_path}"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            pane_cwd = result.stdout.strip()
            save_claude_session_cwd(name, pane_cwd)

        export_hook_env(tmux_name, backend)
        time.sleep(0.3)

        # Inject tmux env vars then unset CLAUDECODE (prevents nested-session error)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"])
        time.sleep(0.3)

        ensure_session_dir(name)
        if not backend_obj.is_interactive:
            ensure_worker_pipe(name)

        if not backend_obj.is_interactive:
            backend_file = self.sessions_dir / name / "backend"
            backend_file.write_text(backend)

        append_prompt = ""
        if backend_obj.name == "claude":
            append_prompt = build_mcp_inventory_prompt(pane_cwd)

        if SANDBOX_ENABLED and backend_obj.is_interactive:
            docker_cmd = get_docker_run_cmd(name, append_system_prompt=append_prompt)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
            print(f"Started worker '{name}' in sandbox mode")
        else:
            start_cmd = f'unset CLAUDECODE && {backend_obj.start_cmd(append_system_prompt=append_prompt)}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])
            if backend_obj.is_interactive:
                time.sleep(1.5)
                subprocess.run(["tmux", "send-keys", "-t", tmux_name, "2"])
                time.sleep(0.3)
                subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])

        if backend_obj.is_interactive:
            time.sleep(2.0 if not SANDBOX_ENABLED else 5.0)

        welcome = self._build_welcome(name, backend_obj)
        if not backend_obj.is_interactive:
            if chat_id:
                set_pending(name, chat_id)
            # Echo welcome to tmux (visible for debugging) but don't call backend
            # to avoid triggering a codex API call on hire
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"])
        else:
            self.send(name, welcome)

        state["active"] = name
        save_last_active(name)

        if not backend_obj.is_interactive:
            print(f"Created {backend} worker '{name}' (non-interactive mode)")

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
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        # Clean non-interactive metadata (backend file, session IDs, pending)
        if not backend.is_interactive:
            kill_adapter(name)
            session_dir = self.sessions_dir / name
            backend_file = session_dir / "backend"
            try:
                if backend_file.exists():
                    backend_file.unlink()
                for session_id_file in session_dir.glob("*_session_id"):
                    session_id_file.unlink()
            except Exception as e:
                return False, f"Failed to clean non-interactive metadata: {e}"

        if SANDBOX_ENABLED and backend.is_interactive:
            stop_docker_container(name)

        clear_pending(name)
        # All backends have tmux sessions now
        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        cleanup_inbox(name)
        cleanup_worker_pipe(name)

        if state["active"] == name:
            state["active"] = None
            self.get_registered_sessions()

        return True, None

    def restart(self, name: str, mode: str = "relaunch"):
        """Restart a worker in its existing tmux session."""
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered:
            return False, f"Worker '{name}' not found"

        session = registered[name]
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        if not tmux_exists(tmux_name):
            return False, "Worker workspace is not running"

        # Check binary still exists before restarting
        if not shutil.which(backend.binary):
            return False, f"'{backend.binary}' not found in PATH. Install it first."

        resume_id = ""
        resume_cwd = ""
        session_dir = self.sessions_dir / name
        if mode == "resume":
            resume_id = get_claude_session_id(name)
            resume_cwd = get_claude_session_cwd(name)
        else:
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()

        # Clean non-interactive state on restart
        if not backend.is_interactive:
            session_dir.mkdir(parents=True, exist_ok=True)
            ensure_worker_pipe(name)
            clear_pending(name)
        elif is_claude_running(tmux_name):
            # Kill running claude first, then restart (resume keeps session ID, relaunch clears it)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""])
            time.sleep(0.5)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"])
            time.sleep(1.0)
            # If still running, force kill
            if is_claude_running(tmux_name):
                pane_pid = _tmux_pane_pids().get(tmux_name)
                if pane_pid:
                    claude_pid = _get_claude_pid(pane_pid)
                    if claude_pid:
                        subprocess.run(["kill", claude_pid], capture_output=True)
                        time.sleep(0.5)

        export_hook_env(tmux_name, backend_name)
        time.sleep(0.3)

        # Inject tmux env vars then unset CLAUDECODE (prevents nested-session error)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"])
        time.sleep(0.3)

        append_prompt = ""
        if backend.name == "claude":
            inventory_cwd = resume_cwd or get_claude_session_cwd(name)
            append_prompt = build_mcp_inventory_prompt(inventory_cwd)

        if SANDBOX_ENABLED and backend.is_interactive:
            stop_docker_container(name)
            time.sleep(0.5)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id, append_system_prompt=append_prompt)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        else:
            start_cmd = backend.start_cmd(resume_id, append_system_prompt=append_prompt)
            if resume_cwd:
                start_cmd = f'cd "{resume_cwd}" && unset CLAUDECODE && {start_cmd}'
            else:
                start_cmd = f'unset CLAUDECODE && {start_cmd}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])

        # Re-send welcome/instructions so worker gets fresh context after restart
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            time.sleep(2.0 if not SANDBOX_ENABLED else 5.0)
            self.send(name, welcome)
        else:
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"])

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
    """Get registered sessions from tmux (all backends have tmux now)."""
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


def get_docker_run_cmd(name, resume_id: str = "", append_system_prompt: str = ""):
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
    cmd_parts.append(build_claude_start_cmd(resume_id, append_system_prompt))

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


def send_response_to_telegram(name: str, text: str, chat_id: int, log_prefix: str = "Response"):
    """Send a response to Telegram. Shared by hook responses.

    Args:
        name: Session/worker name for message prefix
        text: Response text (may contain image/file tags)
        chat_id: Telegram chat ID
        log_prefix: Prefix for log messages (e.g., "Response", "Hook response")
    """
    # Parse image and file tags from text (before converting to preserve tag syntax)
    clean_text, images = parse_image_tags(text)
    clean_text, files = parse_file_tags(clean_text)
    clean_text = markdown_to_telegram_html(clean_text)

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
                # Fallback: retry as plain text on HTTP 400 (HTML parse error)
                desc = (result or {}).get("description", "")
                error_code = (result or {}).get("error_code", 0)
                is_400 = error_code == 400
                if is_400:
                    print(f"{log_prefix} HTML send failed (400: {desc}), retrying as plain text")
                    # Strip HTML tags and decode entities for readable plain text
                    plain_text = re.sub(r'<[^>]+>', '', part)
                    plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                    plain_data = {k: v for k, v in msg_data.items() if k != "parse_mode"}
                    plain_data["text"] = plain_text
                    result = telegram_api("sendMessage", plain_data)
                    if result and result.get("ok"):
                        prev_msg_id = result.get("result", {}).get("message_id")
                        print(f"{log_prefix} sent (plain): {name} -> Telegram OK")
                    else:
                        print(f"{log_prefix} failed (plain): {name} -> {result}")
                else:
                    print(f"{log_prefix} failed: {name} -> {result}")

            if i < len(formatted_parts) - 1:
                time.sleep(0.05)

    # Send images
    for img_path, img_caption in images:
        full_caption = f"{name}: {img_caption}" if img_caption else f"{name}:"
        # Use sendAnimation for GIFs and MP4s to preserve animation
        if Path(img_path).suffix.lower() in (".gif", ".mp4"):
            sent = send_animation(chat_id, img_path, full_caption)
        else:
            sent = send_photo(chat_id, img_path, full_caption)
        if sent:
            print(f"Image sent: {name} -> {img_path}")
        else:
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"{name}: [Image failed: {img_path}]"
            })

    # Send files — route to specialized API method by extension
    for file_path, file_caption in files:
        full_caption = f"{name}: {file_caption}" if file_caption else f"{name}:"
        ext = Path(file_path).suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            sent = send_video(chat_id, file_path, full_caption)
        elif ext in AUDIO_EXTENSIONS:
            sent = send_audio(chat_id, file_path, full_caption)
        elif ext in VOICE_EXTENSIONS:
            sent = send_voice(chat_id, file_path, full_caption)
        elif ext in STICKER_EXTENSIONS:
            sent = send_sticker(chat_id, file_path)
        else:
            sent = send_document(chat_id, file_path, full_caption)
        if sent:
            print(f"File sent: {name} -> {file_path}")
        else:
            telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"{name}: [File failed: {file_path}]"
            })




 


def create_session(name, backend: str = DEFAULT_BACKEND, chat_id: int = None):
    """Create a new worker instance."""
    _sync_worker_manager()
    return worker_manager.hire(name, backend, chat_id=chat_id)


def kill_session(name):
    """Kill a worker instance."""
    _sync_worker_manager()
    return worker_manager.end(name)


def restart_claude(name, mode: str = "relaunch"):
    """Restart claude in an existing tmux session."""
    _sync_worker_manager()
    return worker_manager.restart(name, mode=mode)


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
        animation = msg.get("animation")

        doc_is_image = False
        if document:
            mime_type = document.get("mime_type", "")
            doc_is_image = mime_type.startswith("image/")

        # Handle GIF/animation (Telegram sends these separately from photos)
        if animation and chat_id:
            file_id = animation.get("file_id")
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
                    gif_text = f"Manager sent GIF: {local_path}"
                    if text:
                        gif_text = f"{text}\n\n{gif_text}"
                    self.route_to_active(gif_text, chat_id, msg_id)
                else:
                    self.reply(chat_id, "Needs decision - Could not download GIF. Try again.")
                return

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

        # Extract reply context (quote-reply = context only, never routing)
        reply_context = ""
        reply_to = msg.get("reply_to_message")
        if reply_to:
            reply_context = self.get_reply_context(reply_to)

        # Parse @mentions anywhere in text
        targets, clean_text = self.parse_at_mentions(text)

        if targets:
            message = clean_text or text
            if reply_context:
                message = self.format_reply_context(message, reply_context)
            for name in targets:
                self.route_message(name, message, chat_id, msg_id, one_off=True)
            return

        # No @mentions → route to focused worker
        routed_text = text
        if reply_context:
            routed_text = self.format_reply_context(text, reply_context)
        self.route_to_active(routed_text, chat_id, msg_id)

    def parse_at_mentions(self, text):
        """Extract all @mentions from anywhere in text. Returns (targets, cleaned_text)."""
        if not text:
            return [], ""
        registered = self.workers.get_registered_sessions()
        found = []
        for match in re.finditer(r'@([a-zA-Z0-9-]+)', text):
            name = match.group(1).lower()
            if name in registered and name not in found:
                found.append(name)
        if not found:
            return [], text
        # Remove matched @mentions from text
        cleaned = re.sub(r'@([a-zA-Z0-9-]+)', lambda m: '' if m.group(1).lower() in found else m.group(0), text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return found, cleaned

    def parse_worker_prefix(self, text):
        """Parse 'name: message' prefix from bot-sent messages."""
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

    def get_reply_context(self, reply_msg):
        """Extract text from a replied-to message (context only, no routing)."""
        if not reply_msg:
            return ""
        return reply_msg.get("text") or reply_msg.get("caption") or ""

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
        elif cmd == "/restart":
            return self.cmd_restart(chat_id, arg)
        elif cmd == "/settings":
            return self.cmd_settings(chat_id)
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

        tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}")
        if not backend.is_interactive:
            # Non-interactive: online = tmux exists, ready = always (stateless)
            online = tmux_exists(tmux_name)
            ready = online  # Ready if tmux exists
            mode = f"{backend_name} (non-interactive)"
            if not online:
                needs_attention = "tmux session missing. Use /end then /hire to recreate."
        else:
            exists = tmux_exists(tmux_name)
            online = exists
            if exists:
                claude_running = is_claude_running(tmux_name)
                ready = claude_running
                if not claude_running:
                    needs_attention = "worker app is not running. Use /restart."

        resume_line = None
        continuity_line = None

        if not backend.is_interactive:
            # Non-interactive: show Continuity (thread) + In-flight
            session_id, source = get_any_session_id(name)
            if session_id:
                continuity_line = f"Continuity: on ({source} thread {session_id[:8]}...)"
            else:
                continuity_line = "Continuity: off (next message starts new thread)"
        else:
            # Interactive: show Resume
            resume_id = get_claude_session_id(name)
            if resume_id:
                resume_line = f"Resume: available (session {resume_id[:8]}...)"
            else:
                resume_line = "Resume: not available"

        status = format_progress_lines(
            name=name,
            pending=pending,
            backend=backend_name,
            online=online,
            ready=ready,
            mode=mode,
            resume_line=resume_line,
            continuity_line=continuity_line,
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
            if not backend.is_interactive:
                kill_adapter(name)
                clear_pending(name)
                self.reply(chat_id, f"{name.capitalize()} is paused. I'll pick up where we left off.")
                return True
            tmux_send_escape(session["tmux"])
            clear_pending(name)

        self.reply(chat_id, f"{name.capitalize()} is paused. I'll pick up where we left off.")
        return True

    def cmd_restart(self, chat_id, args=""):
        args = (args or "").strip()

        # Parse --clean flag
        clean = False
        tokens = args.split()
        remaining = []
        for t in tokens:
            if t == "--clean":
                clean = True
            else:
                remaining.append(t)
        name_arg = remaining[0].lower() if remaining else ""

        if name_arg:
            name = name_arg
        else:
            if not state["active"]:
                registered = self.workers.get_registered_sessions()
                if len(registered) == 1:
                    name = next(iter(registered))
                    state["active"] = name
                    save_last_active(name)
                else:
                    self.reply(chat_id, "No one assigned.")
                    return True
            else:
                name = state["active"]

        registered = self.workers.get_registered_sessions()
        session = registered.get(name)
        if name not in registered:
            if registered:
                names = ", ".join(registered.keys())
                self.reply(chat_id, f"Can't find \"{name}\". Available workers: {names}")
            else:
                self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True

        if name_arg:
            state["active"] = name
            save_last_active(name)

        # --clean: fresh start (clear session IDs)
        if clean:
            ok, err = restart_claude(name, mode="relaunch")
            if ok:
                self.reply(chat_id, f"Bringing {name.capitalize()} back online...")
            else:
                self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
            return True

        # Default: resume behavior
        backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
        backend = get_backend(backend_name)

        # Non-interactive backends: resume is automatic via saved thread ID
        if not backend.is_interactive:
            session_id, source = get_any_session_id(name)
            if session_id:
                self.reply(chat_id, f"Resume is automatic for {backend_name}. Thread {session_id[:8]}... is active — next message continues it.")
            else:
                self.reply(chat_id, f"No thread found for {name.capitalize()}. Next message starts a new {backend_name} thread.")
            return True

        # Interactive backends: restart with --resume
        session_dir = get_session_dir(name)
        has_session_id = False
        if session_dir.exists():
            has_session_id = any(session_dir.glob("*_session_id"))

        if not has_session_id:
            ok, err = restart_claude(name, mode="relaunch")
            if ok:
                self.reply(chat_id, f"No resume info found. Restarting {name.capitalize()} fresh...")
            else:
                self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
            return True

        ok, err = restart_claude(name, mode="resume")
        if ok:
            self.reply(chat_id, f"Resuming {name.capitalize()}...")
        else:
            self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
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
            self.reply(chat_id, f"{session_name.capitalize()} is offline. Try /restart.")
            return

        backend_name = get_worker_backend(session_name, session)
        backend = get_backend(backend_name)

        # Non-interactive backpressure: reject if already processing
        if not backend.is_interactive and is_pending(session_name):
            self.reply(chat_id, f"{session_name.capitalize()} is still working on the previous request. Wait for a response or use /pause.")
            return

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
                f"Could not send to {session_name.capitalize()}. Try /restart.",
                outcome="Needs decision"
            )
            return

        if msg_id and send_ok:
            if not backend.is_interactive or tmux_prompt_empty(session.get("tmux", "")):
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

        FILE SUPPORT: Parses [[image:/path|caption]] (photos, animations) and [[file:/path|caption]] (documents, video, audio, voice, stickers) tags.
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
            send_response_to_telegram(session_name, text, int(chat_id), log_prefix="Response")

            # Clear pending
            clear_pending(session_name)
            mark_hook_event(session_name)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print(f"Hook response error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())


    def do_GET(self):
        parsed = urlparse(self.path)

        # Handle /workers endpoint for inter-worker discovery
        if parsed.path == "/workers":
            self.handle_workers_endpoint()
            return

        # Handle /checkin endpoint for worker instruction refresh
        if parsed.path == "/checkin":
            self.handle_checkin_endpoint(parsed)
            return
        if parsed.path == "/health/workers":
            self.handle_health_workers_endpoint()
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

    def handle_checkin_endpoint(self, parsed):
        """Return worker instructions as plain text.

        GET /checkin           — generic instructions (uses default backend)
        GET /checkin?name=lee  — personalized instructions for worker 'lee'
        """
        try:
            params = parse_qs(parsed.query)
            name = params.get("name", ["worker"])[0]

            # If worker exists, use their actual backend; otherwise default
            _sync_worker_manager()
            registered = worker_manager.get_registered_sessions()
            if name in registered:
                backend_name = get_worker_backend(name, registered[name])
            else:
                backend_name = DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)

            welcome = worker_manager._build_welcome(name, backend_obj)

            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(welcome.encode())
        except Exception as e:
            print(f"Checkin endpoint error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_health_workers_endpoint(self):
        """Return watchdog worker states as JSON (debug endpoint)."""
        try:
            now = time.time()
            registered = get_registered_sessions()
            with _watchdog_lock:
                state_snapshot = dict(_worker_states)
            workers = {}
            for name in sorted(registered.keys()):
                entry = state_snapshot.get(name)
                if entry:
                    state, reason, since = entry
                    workers[name] = {
                        "state": state,
                        "reason": reason,
                        "since": since,
                        "age_sec": int(now - since) if since else None,
                    }
                else:
                    workers[name] = {"state": "unknown"}

            response = {"workers": workers}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        except Exception as e:
            print(f"Health workers endpoint error: {e}")
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
        for name, info in registered.items():
            backend_name = get_worker_backend(name, info)
            backend_obj = get_backend(backend_name)
            if not backend_obj.is_interactive:
                ensure_worker_pipe(name)

    # Load last active worker from file (if still exists)
    last_active = load_last_active()
    if last_active and last_active in registered:
        state["active"] = last_active
        print(f"Restored last active worker: {last_active}")
    elif last_active:
        print(f"Last active worker '{last_active}' no longer exists")

    # Log team dir and checkin note status
    if os.path.isdir(TEAM_DIR):
        print(f"Team dir: {TEAM_DIR}")
        _startup_note = read_checkin_note()
        if _startup_note:
            print(f"  Checkin note: {_CHECKIN_NOTE_PATH} ({len(_startup_note)} chars)")
        else:
            print(f"  No checkin note at {_CHECKIN_NOTE_PATH}")
    else:
        print(f"Team dir not found: {TEAM_DIR} (checkin note disabled)")

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

    watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog.start()

    try:
        ReuseAddrServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
