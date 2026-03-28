#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.24.0"

import hashlib
import os
import json
import mimetypes
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
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

# Node-derived config: NODE_NAME drives defaults for PORT, TMUX_PREFIX, SESSIONS_DIR.
# Explicit env vars always override. No NODE_NAME = original defaults.
NODE_NAME = os.environ.get("NODE_NAME", "")
_DEFAULT_PORTS = {"prod": 8271, "dev": 8272, "test": 8295}

if NODE_NAME and not os.environ.get("PORT"):
    PORT = _DEFAULT_PORTS.get(NODE_NAME, 8270)
else:
    PORT = int(os.environ.get("PORT", "8270"))

BRIDGE_BIND = os.environ.get("BRIDGE_BIND", "127.0.0.1")  # Bind address (localhost-only by default)
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")  # Optional webhook verification

if NODE_NAME and not os.environ.get("SESSIONS_DIR"):
    SESSIONS_DIR = Path.home() / ".claude" / "telegram" / "nodes" / NODE_NAME / "sessions"
else:
    SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))

if NODE_NAME and not os.environ.get("TMUX_PREFIX"):
    TMUX_PREFIX = f"claude-{NODE_NAME}-"
else:
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

# BRIDGE_URL: hook callback target. Localhost URLs are always derived from PORT to
# prevent stale-port inheritance when restarting. Only non-localhost URLs (for
# distributed setups, e.g. https://remote-bridge.example.com) are honored from env.
_bridge_url_env = os.environ.get("BRIDGE_URL", "").rstrip("/")
if _bridge_url_env and not _bridge_url_env.startswith(("http://localhost", "http://127.0.0.1")):
    BRIDGE_URL = _bridge_url_env
else:
    BRIDGE_URL = f"http://localhost:{PORT}"
# BRIDGE_PUBLIC_URL: reachable URL for teleported workers (e.g., http://100.125.36.102:8271)
# When set and BRIDGE_BIND is not explicitly set, auto-bind to 0.0.0.0
# Auto-detect from Tailscale IP if not explicitly set.
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/")
if not BRIDGE_PUBLIC_URL:
    try:
        _ts_ip = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True,
            text=True, timeout=3).stdout.strip()
        if _ts_ip:
            BRIDGE_PUBLIC_URL = f"http://{_ts_ip}:{PORT}"
    except Exception:
        pass
if BRIDGE_PUBLIC_URL and not os.environ.get("BRIDGE_BIND"):
    BRIDGE_BIND = "0.0.0.0"
PERSISTENCE_NOTE = "They'll stay on your team."

# API endpoint registry — used by index, 404 handler, and worker instructions.
# Update this when adding new endpoints.
API_ENDPOINTS = {
    "GET /": "API index — lists all endpoints",
    "GET /workers": "List active workers with send commands",
    "GET /checkin?name=<name>": "Refresh worker instructions (optional: &cwd=/path)",
    "GET /health/workers": "Watchdog state for all workers",
    "POST /response": "Hook: send Claude response to Telegram",
    "POST /notify": "Send notification to all admin chats",
}

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
STALE_PENDING = 900  # 15 minutes
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
# SSH Teleport helpers (remote worker support)
# ─────────────────────────────────────────────────────────────────────────────


def _remote_run(cmd: list, host: str = None, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, optionally on a remote host via SSH.

    When host is None, runs locally. When set, builds a single shell command
    string with proper quoting so the remote shell doesn't eat special chars
    like # (which starts a comment in bash).
    SSH ControlMaster keeps overhead to ~10ms per call.
    """
    if host:
        # SSH concatenates args and runs them through the remote shell,
        # so we must shell-quote each arg to preserve special characters.
        remote_cmd = " ".join(shlex.quote(str(a)) for a in cmd)
        cmd = ["ssh", host, remote_cmd]
    return subprocess.run(cmd, **kwargs)


def _remote_copy(src: str, dst: str, host: str = None, direction: str = "push"):
    """Copy a file, optionally to/from a remote host via scp.

    direction='push': local src -> remote dst
    direction='pull': remote src -> local dst
    host=None: local copy via shutil.copy2
    """
    if not host:
        shutil.copy2(src, dst)
    elif direction == "push":
        subprocess.run(["scp", "-q", src, f"{host}:{dst}"], capture_output=True)
    else:  # pull
        subprocess.run(["scp", "-q", f"{host}:{src}", dst], capture_output=True)


def parse_worker_target(target: str) -> tuple:
    """Parse 'name@host' or 'name' into (name, host).

    Returns (name, None) for local workers, (name, host) for remote.
    """
    if "@" in target:
        name, host = target.rsplit("@", 1)
        return name, host
    return target, None


def get_worker_host(name: str) -> Optional[str]:
    """Get the SSH host for a worker from the persistent registry, or None if local."""
    registry = _load_registry()
    worker = registry.get("workers", {}).get(name, {})
    return worker.get("host")


def _project_slug(cwd: str) -> str:
    """Convert absolute path to Claude Code's project directory slug.

    Claude Code stores sessions at ~/.claude/projects/<slug>/<session-id>.jsonl
    where slug is the CWD with / replaced by -.
    """
    return cwd.replace("/", "-")


# Default rsync excludes for teleport directory sync
TELEPORT_RSYNC_EXCLUDES = [
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".next", "build", "dist", "target", ".gradle", ".cache",
    ".tox", ".mypy_cache", ".pytest_cache", "*.pyc",
    ".build", ".claude/worktrees",
]


def _registry_update_teleport(name: str, host: str, home_host: str, home_cwd: str):
    """Update registry with teleport location info."""
    with _watchdog_lock:
        data = _load_registry()
        worker = data.get("workers", {}).get(name, {})
        worker["host"] = host
        worker["home_host"] = home_host
        worker["home_cwd"] = home_cwd
        data.setdefault("workers", {})[name] = worker
        _save_registry(data)


def _registry_clear_teleport(name: str):
    """Clear teleport location info from registry (after teleback)."""
    with _watchdog_lock:
        data = _load_registry()
        worker = data.get("workers", {}).get(name, {})
        worker.pop("host", None)
        worker.pop("home_host", None)
        worker.pop("home_cwd", None)
        data.setdefault("workers", {})[name] = worker
        _save_registry(data)


# ─────────────────────────────────────────────────────────────────────────────
# Shared tmux helpers (used by multiple backends)
# ─────────────────────────────────────────────────────────────────────────────

# Per-session locks to prevent concurrent tmux sends from interleaving
_tmux_send_locks = {}
_tmux_send_locks_guard = threading.Lock()

# Per-session flock file descriptors (kept open for the process lifetime)
_tmux_send_flock_fds = {}


def _get_tmux_send_lock(tmux_name: str):
    """Get or create a lock for a specific tmux session."""
    with _tmux_send_locks_guard:
        if tmux_name not in _tmux_send_locks:
            _tmux_send_locks[tmux_name] = threading.Lock()
        return _tmux_send_locks[tmux_name]


def tmux_send_lock_path(tmux_name: str) -> Path:
    """Return the flock file path for a tmux session. Node-namespaced."""
    return Path(f"/tmp/claudecode-telegram/{_node_name}/locks/{tmux_name}.lock")


def _acquire_flock(tmux_name: str) -> int:
    """Acquire a cross-process flock for a tmux session. Returns fd."""
    lock_file = tmux_send_lock_path(tmux_name)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_flock(fd: int):
    """Release a cross-process flock."""
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def tmux_exists(tmux_name: str, host: str = None) -> bool:
    """Check if tmux session exists (locally or on remote host via SSH)."""
    return _remote_run(
        ["tmux", "has-session", "-t", tmux_name],
        host=host, capture_output=True
    ).returncode == 0


def tmux_send_message(tmux_name: str, text: str, host: str = None) -> bool:
    """Send text + Enter to tmux session via paste-buffer (reliable for long messages).

    Uses tmux load-buffer/paste-buffer instead of send-keys -l to avoid
    character-by-character terminal injection which causes input batching
    on long messages or rapid sends.

    When host is set, uses SSH and pipes text via stdin (no shared filesystem needed).

    Two-layer locking:
    1. Python threading.Lock — serializes sends within this process
    2. flock on a per-session file — serializes sends across processes
       (workers sending via tmux directly use the same lock file)
    """
    lock = _get_tmux_send_lock(tmux_name)
    with lock:
        flock_fd = _acquire_flock(tmux_name)
        try:
            buf_name = f"msg-{uuid.uuid4().hex[:8]}"

            if host:
                # Remote: pipe text via stdin to avoid shared filesystem
                r = _remote_run(
                    ["tmux", "load-buffer", "-b", buf_name, "-"],
                    host=host, input=text.encode(), capture_output=True,
                )
            else:
                # Local: write to temp file for tmux load-buffer
                fd, tmpfile = tempfile.mkstemp(suffix=".msg", prefix="tmux-send-")
                try:
                    os.write(fd, text.encode())
                    os.close(fd)
                    r = subprocess.run(
                        ["tmux", "load-buffer", "-b", buf_name, tmpfile],
                        capture_output=True,
                    )
                finally:
                    try:
                        os.unlink(tmpfile)
                    except OSError:
                        pass

            if r.returncode != 0:
                return False
            # Paste buffer into the target pane with proper bracketed paste
            # -p: send bracketed paste control codes (\e[200~ ... \e[201~)
            #     so TUI apps (Claude Code) know exactly where paste ends.
            #     Without -p, Enter sent after paste can be swallowed into
            #     the TUI's time-based paste detection window.
            # -r: preserve LF as LF (don't convert to CR). Keeps multi-line
            #     text as multi-line input, not line-by-line Enter presses.
            # -d: delete buffer after pasting
            r = _remote_run(
                ["tmux", "paste-buffer", "-p", "-r", "-t", tmux_name, "-b", buf_name, "-d"],
                host=host, capture_output=True,
            )
            if r.returncode != 0:
                return False
            # Delay after paste: TUI needs time to process paste-end marker
            # and re-render. At low context (1%), Claude Code TUI can take
            # 300-1000ms to render pasted text. Enter sent before render
            # completes hits an empty prompt and the message is silently lost.
            # 50ms → 150ms → 1s: increased after observing silent message
            # loss on prod sessions with heavy context load.
            time.sleep(1.0)
            # Send Enter to submit the pasted text
            r = _remote_run(["tmux", "send-keys", "-t", tmux_name, "Enter"], host=host)
            return r.returncode == 0
        finally:
            _release_flock(flock_fd)


def get_pane_command(tmux_name: str, host: str = None) -> str:
    """Get the current command running in tmux pane."""
    result = _remote_run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_command}"],
        host=host, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def is_process_running(tmux_name: str, process_name: str, host: str = None) -> bool:
    """Check if a process is running in tmux session."""
    cmd = get_pane_command(tmux_name, host=host)
    if process_name.lower() in cmd.lower():
        return True

    result = _remote_run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
        host=host, capture_output=True, text=True
    )
    if result.returncode != 0:
        return False

    pane_pid = result.stdout.strip()
    if not pane_pid:
        return False

    result = _remote_run(
        ["pgrep", "-P", pane_pid, process_name],
        host=host, capture_output=True
    )
    return result.returncode == 0


def tmux_send_escape(tmux_name: str, host: str = None):
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host)


def _tmux_pane_pids(host: str = None) -> dict:
    """Return a map of tmux session_name -> pane_pid for all panes."""
    try:
        result = _remote_run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
            host=host, capture_output=True, text=True, timeout=5
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


def _get_claude_pid(pane_pid: str, host: str = None) -> Optional[str]:
    """Return Claude PID for a pane, or None if not found."""
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pane_pid), "-f", "claude"],
            host=host, capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip().splitlines()
    if not output:
        return None
    return output[0].strip()


def _child_count(pid: str, host: str = None) -> int:
    """Return child process count for pid."""
    if not pid:
        return 0
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pid)],
            host=host, capture_output=True, text=True, timeout=5
        )
    except Exception:
        return 0

    if result.returncode != 0:
        return 0

    return len([line for line in result.stdout.splitlines() if line.strip()])


def _ps_stats(pids, host: str = None) -> dict:
    """Return {pid: {'cpu': float, 'state': str}} for given pids."""
    pid_list = [str(pid) for pid in pids if pid]
    if not pid_list:
        return {}

    try:
        result = _remote_run(
            ["ps", "-o", "pid=,%cpu=,state=", "-p", ",".join(pid_list)],
            host=host, capture_output=True, text=True, timeout=5
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
        host = get_worker_host(worker_name)
        if not tmux_exists(tmux_name, host=host):
            return False
        return tmux_send_message(tmux_name, text, host=host)

    def is_online(self, tmux_name: str) -> bool:
        # Note: is_online doesn't have worker_name, so can't look up host.
        # For remote workers, the watchdog uses different detection.
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


def _which_binary(binary: str) -> str | None:
    """Find binary in PATH, including common user install locations.

    The bridge may run with a minimal PATH (e.g. via env -i), missing
    ~/.local/bin or ~/bin where claude/codex are typically installed.
    """
    found = shutil.which(binary)
    if found:
        return found
    home = os.environ.get("HOME", "")
    if home:
        for extra_dir in [os.path.join(home, ".local", "bin"), os.path.join(home, "bin")]:
            candidate = os.path.join(extra_dir, binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def is_claude_running(tmux_name: str, host: str = None) -> bool:
    return is_process_running(tmux_name, "claude", host=host)


# In-memory state (RAM only, no persistence - tmux IS the persistence)
state = {
    "active": None,  # Currently active session name
    "startup_notified": False,  # Whether we've sent the startup message
}

# Consecutive @mention tracking (auto-focus after 2 in a row to same worker)
_last_mention = {"target": None, "count": 0}

# Watchdog state
_worker_states = {}  # name -> (state, reason, since)
_last_child_ts = {}
_last_seen_claude = {}
_last_hook_ts = {}
_last_alert_ts = {}
_idle_streak = {}
_prev_worker_states = {}
_consecutive_probe_failures = {}
_idle_child_baseline = {}  # name -> int (MCP server child count at idle)
_prev_children = {}  # name -> int (previous active children count, for activity detection)
_last_activity_ts = {}  # name -> float (last time children count changed)
_worker_cwds = {}  # name -> cwd (RAM-only startup cwd hints)
_recent_restarts = {}  # name -> timestamp (suppress watchdog resolved alert after restart)
_waiting_input_details = {}  # name -> dict (question details for WAITING_INPUT alert)
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
    {"command": "pilot", "description": "Toggle pilot access: /pilot <name>"},
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


# ─────────────────────────────────────────────────────────────────────────────
# Persistent Worker Registry
# ─────────────────────────────────────────────────────────────────────────────

WORKER_REGISTRY_FILE = NODE_DIR / "workers.json"


def _load_registry() -> dict:
    """Load worker registry from disk. Returns {} on missing/corrupt."""
    try:
        if not WORKER_REGISTRY_FILE.exists():
            return {}
        raw = WORKER_REGISTRY_FILE.read_text()
        data = json.loads(raw)
        if not isinstance(data, dict) or "workers" not in data:
            raise ValueError("invalid registry format")
        return data
    except Exception as e:
        if WORKER_REGISTRY_FILE.exists():
            corrupt_path = WORKER_REGISTRY_FILE.with_suffix(f".corrupt.{int(time.time())}")
            print(f"Corrupt worker registry, renaming to {corrupt_path}: {e}")
            try:
                WORKER_REGISTRY_FILE.rename(corrupt_path)
            except Exception:
                pass
        return {}


def _save_registry(data: dict):
    """Atomic write of registry to disk."""
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(NODE_DIR), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(WORKER_REGISTRY_FILE))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        print(f"Failed to save worker registry: {e}")


def _registry_add(name: str, backend: str, chat_id: int = None, host: str = None):
    """Add a worker to the persistent registry."""
    with _watchdog_lock:
        data = _load_registry()
        if "workers" not in data:
            data = {"version": 1, "workers": {}}
        entry = {
            "backend": backend,
            "chat_id": chat_id,
            "hire_time": int(time.time()),
        }
        if host:
            entry["host"] = host
        data["workers"][name] = entry
        _save_registry(data)


def _registry_remove(name: str):
    """Remove a worker from the persistent registry."""
    with _watchdog_lock:
        data = _load_registry()
        if "workers" not in data:
            return
        data["workers"].pop(name, None)
        _save_registry(data)


def _set_worker_cwd(name: str, cwd: str):
    """Set startup cwd hint for a worker in RAM."""
    normalized = normalize_cwd(cwd)
    with _watchdog_lock:
        if normalized:
            _worker_cwds[name] = normalized
        else:
            _worker_cwds.pop(name, None)


def _get_worker_cwd(name: str) -> str:
    """Get startup cwd hint for a worker from RAM."""
    with _watchdog_lock:
        cwd = _worker_cwds.get(name)
    return cwd if isinstance(cwd, str) else ""


def _registry_bootstrap(registered: dict):
    """First-run: create registry from currently running tmux sessions."""
    if WORKER_REGISTRY_FILE.exists():
        return
    if not registered:
        return
    data = {"version": 1, "workers": {}}
    for name, session in registered.items():
        backend = normalize_backend(session.get("backend"))
        data["workers"][name] = {
            "backend": backend,
            "chat_id": None,
            "hire_time": int(time.time()),
        }
    _save_registry(data)
    print(f"Registry bootstrapped with {len(registered)} workers: {', '.join(registered.keys())}")


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
    "all", "cancel", "start", "help",
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


def send_telegram_message(chat_id: int, text: str):
    """Send a plain Telegram message."""
    return telegram.send_message(chat_id, text)



# ============================================================
# MEDIA HANDLING
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Image Handling
# ─────────────────────────────────────────────────────────────────────────────

# Max file size: 50MB (Telegram Bot API limit for uploads)
MAX_FILE_SIZE = 50 * 1024 * 1024

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


def get_manager_chat_id(name: str) -> Optional[int]:
    """Resolve manager chat ID for worker notifications.

    Priority:
      1) ADMIN_CHAT_ID (if configured)
      2) Session chat_id file
    """
    if admin_chat_id is not None:
        return admin_chat_id

    chat_id_file = get_chat_id_file(name)
    if not chat_id_file.exists():
        return None

    try:
        value = chat_id_file.read_text().strip()
        return int(value) if value else None
    except Exception as e:
        print(f"Failed to read chat_id for {name}: {e}")
        return None


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
    """Check if session has a pending request within the timeout window.

    Non-mutating: does NOT delete the pending file. The file is preserved
    so the watchdog can detect STALE_PENDING at 15 minutes. Cleanup happens
    only via clear_pending() when a response arrives.
    """
    pending = get_pending_file(name)
    if not pending.exists():
        return False
    try:
        ts = int(pending.read_text().strip())
        if (time.time() - ts) > PENDING_TIMEOUT:
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


def _capture_pane_text(tmux_name: str, lines: int = 50, host: str = None) -> str:
    """Return the last N lines of a tmux pane, or empty string on error."""
    if lines <= 0:
        return ""
    try:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", f"-{lines}"],
            host=host, capture_output=True, text=True, timeout=5
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


HOOK_FAILURE_THRESHOLD = 3   # failures in window → POISONED
HOOK_FAILURE_WINDOW = 120    # seconds


def _check_hook_failure_signal(name: str) -> Optional[str]:
    """Check hook-written failure signal file for recent tool failures.

    PostToolUseFailure hook appends lines: "<epoch> <tool_name>"
    Returns reason string if >= HOOK_FAILURE_THRESHOLD recent failures, else None.
    """
    signal_file = Path(f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures")
    if not signal_file.exists():
        return None
    try:
        lines = signal_file.read_text().strip().splitlines()
    except Exception:
        return None
    if not lines:
        return None

    cutoff = int(time.time()) - HOOK_FAILURE_WINDOW
    recent = 0
    for line in lines:
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            ts = int(parts[0])
        except ValueError:
            continue
        if ts >= cutoff:
            recent += 1

    if recent >= HOOK_FAILURE_THRESHOLD:
        return f"hook failure signal: {recent} tool failures in {HOOK_FAILURE_WINDOW}s"
    return None


def _clear_hook_failures(name: str) -> None:
    """Remove hook failure signal file for a worker (on restart/clean)."""
    signal_file = Path(f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures")
    try:
        signal_file.unlink(missing_ok=True)
    except Exception:
        pass


def _detect_poisoned(name: str, tmux_name: str) -> Optional[str]:
    # Primary: check hook-written failure signal file
    hook_reason = _check_hook_failure_signal(name)
    if hook_reason:
        return hook_reason

    # Fallback: regex-based pane/log scanning
    backend_name = get_worker_backend(name)
    backend = get_backend(backend_name)
    host = get_worker_host(name)
    text_parts = []
    if backend.is_interactive:
        text_parts.append(_capture_pane_text(tmux_name, host=host))
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
        print(f"[watchdog] Alert suppressed for {name} ({state}): cooldown {now - last:.0f}s < {ALERT_COOLDOWN}s")
        return

    # Human-friendly alert messages for manager
    if state == "WAITING_INPUT":
        with _watchdog_lock:
            details = _waiting_input_details.get(name)
        header = details.get("header", "") if details else ""
        title = f"🟡 {name} needs your reply"
        if header:
            title += f": {header}"
        parts = [title]
        if details and details.get("options"):
            for o in details["options"]:
                marker = "\u2794 " if o.get("selected") else "  "
                parts.append(f"{marker}{o['num']}. {o['label']}")
            max_num = max(o["num"] for o in details["options"])
            parts.append(f"\nReply 1-{max_num} to choose, or \"skip\" to cancel.")
        text = "\n".join(parts)
    elif state == "STUCK":
        # Parse age from reason like "age=909s cpu=6.3 streak=3/3"
        age_match = re.search(r"age=(\d+)s", reason)
        age_min = int(age_match.group(1)) // 60 if age_match else 0
        age_str = f"{age_min}min" if age_min > 0 else reason.split()[0]
        text = f"🔴 {name} has made no progress for {age_str}.\n/restart --clean {name} (starts fresh)"
    elif state == "POISONED":
        text = f"🔴 {name} is stuck in an error loop.\n/restart --clean {name} (starts fresh)"
    elif state == "DEAD":
        text = f"🔴 {name} stopped unexpectedly.\n/restart --clean {name} (starts fresh)"
    elif state == "EXITED":
        text = f"🟡 {name}'s session ended.\n/restart {name}"
    elif state == "OFFLINE":
        text = f"🔴 {name} is not running.\n/hire {name}"
    else:
        text = f"{name}: {state} ({reason}). Check /team"
    try:
        result = telegram_api("sendMessage", {"chat_id": admin_chat_id, "text": text})
        if result and result.get("ok"):
            print(f"[watchdog] Alert sent for {name} ({state}): {text[:80]}")
            with _watchdog_lock:
                _last_alert_ts[name] = now
        else:
            print(f"[watchdog] Alert FAILED for {name} ({state}): {result}")
    except Exception as e:
        print(f"Watchdog alert error: {e}")


def _send_resolved_alert(name: str, new_state: str) -> None:
    if admin_chat_id is None:
        return

    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT"}
    with _watchdog_lock:
        prev_state = _prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states:
        return

    # Suppress if worker was recently restarted (cmd_restart sends its own confirmation)
    restart_ts = _recent_restarts.get(name)
    if restart_ts and time.time() - restart_ts < 30:
        return

    text = f"✅ {name} is back to normal."
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

    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with _watchdog_lock:
        prev_state = _prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state

    # Suppress alerts for workers being teleported (teleport takes 30-60s,
    # during which the worker appears DEAD/OFFLINE but is expected)
    teleport_state_file = SESSIONS_DIR / name / "teleport_state"
    if state in {"OFFLINE", "DEAD", "EXITED"} and teleport_state_file.exists():
        with _watchdog_lock:
            _prev_worker_states[name] = state
        return

    def eligible_for_alert() -> bool:
        if state in {"OFFLINE", "DEAD", "EXITED"}:
            return since is not None and (now - since) >= START_GRACE
        return True

    if state in bad_states:
        if state_changed:
            if eligible_for_alert():
                print(f"[watchdog] State change {name}: {prev_state} -> {state} ({reason}), sending alert")
                _send_watchdog_alert(name, state, reason)
        elif state in {"OFFLINE", "DEAD", "EXITED"} and eligible_for_alert():
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

            # Collect remote hosts and probe their tmux sessions in bulk
            remote_workers = {}  # host -> [(name, tmux_name)]
            for name, session in registered.items():
                host = get_worker_host(name)
                if host:
                    tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
                    remote_workers.setdefault(host, []).append((name, tmux_name))

            remote_pane_pids = {}  # tmux_name -> pane_pid (across all hosts)
            for host, workers in remote_workers.items():
                try:
                    r = _remote_run(
                        ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
                        host=host, capture_output=True, text=True, timeout=5)
                    if r.returncode == 0:
                        for line in r.stdout.splitlines():
                            parts = line.strip().split()
                            if len(parts) >= 2 and parts[1].isdigit():
                                remote_pane_pids[parts[0]] = parts[1]
                except Exception:
                    pass

            for name, session in registered.items():
                backend_name = get_worker_backend(name, session)
                backend = get_backend(backend_name)
                backend_info[name] = backend
                tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
                host = get_worker_host(name)
                if host:
                    pane_pid = remote_pane_pids.get(tmux_name)
                else:
                    pane_pid = pane_pids.get(tmux_name)
                tmux_exists = bool(pane_pid)
                tmux_present[name] = tmux_exists

                if not tmux_exists:
                    continue

                if backend.is_interactive:
                    claude_pid = _get_claude_pid(pane_pid, host=host)
                    if claude_pid:
                        claude_pids[name] = claude_pid
                        with _watchdog_lock:
                            _last_seen_claude[name] = now
                    else:
                        with _watchdog_lock:
                            if name not in _last_seen_claude:
                                _last_seen_claude[name] = now

            # Group PIDs by host for remote ps stats
            pids_by_host = {}  # host (None=local) -> [pid, ...]
            pid_to_host = {}   # pid -> host
            for name, pid in claude_pids.items():
                host = get_worker_host(name)
                pids_by_host.setdefault(host, []).append(pid)
                pid_to_host[pid] = host
            stats = {}
            for host, pids in pids_by_host.items():
                stats.update(_ps_stats(pids, host=host))

            for name, session in registered.items():
                tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
                tmux_exists = tmux_present.get(name, False)

                # Registry-only worker (tmux gone): mark EXITED directly
                if not tmux_exists and "tmux" not in session:
                    since = _record_worker_state(name, "EXITED", "session gone", now)
                    _handle_watchdog_transition(name, "EXITED", "session gone", since, now=now)
                    continue

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

                host = get_worker_host(name)
                claude_pid = claude_pids.get(name) if is_interactive else None
                cpu = 0.0
                if claude_pid and claude_pid in stats:
                    cpu = stats[claude_pid].get("cpu", 0.0)

                children_total = _child_count(claude_pid, host=host) if claude_pid else 0

                # Dynamic baseline: MCP servers are persistent children.
                # Track idle child count so only EXTRA children count as work.
                pending_ts = _pending_timestamp(name)
                pending = pending_ts is not None
                if is_interactive and claude_pid:
                    with _watchdog_lock:
                        baseline = _idle_child_baseline.get(name)
                        if baseline is None:
                            # First observation — assume current count is baseline
                            _idle_child_baseline[name] = children_total
                            baseline = children_total
                        elif not pending:
                            # When idle, learn the true floor (MCP servers may start late)
                            baseline = min(baseline, children_total)
                            _idle_child_baseline[name] = baseline
                    children = max(0, children_total - baseline)
                else:
                    children = children_total

                if children > 0:
                    with _watchdog_lock:
                        _last_child_ts[name] = now

                # Activity detection: if children count changed or CPU is active,
                # worker is doing something. Reset the stale-pending timer so
                # long autonomous work doesn't trigger false STUCK alerts.
                with _watchdog_lock:
                    prev_children = _prev_children.get(name)
                    activity_changed = (prev_children is not None and prev_children != children)
                    if activity_changed or cpu >= CPU_ACTIVE:
                        _last_activity_ts[name] = now
                    _prev_children[name] = children
                    last_activity = _last_activity_ts.get(name, 0.0)

                # pending_age counts from the LATER of: message arrival or last activity
                if pending_ts:
                    effective_start = max(pending_ts, last_activity) if last_activity > pending_ts else pending_ts
                    pending_age = now - effective_start
                else:
                    pending_age = 0.0
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

                # Detect interactive prompt (WAITING_INPUT): worker is READY
                # but TUI is at a selection/question prompt needing manager action
                if state == "READY" and is_interactive:
                    pane_text = _capture_pane_text(tmux_name, lines=30, host=host)
                    if pane_text:
                        pane_lines = pane_text.splitlines()
                        details = _extract_question_details(pane_lines)
                        if details:
                            # Store details for the alert message
                            with _watchdog_lock:
                                _waiting_input_details[name] = details
                            state = "WAITING_INPUT"
                            header = details.get("header", "")
                            reason = f"question={header}" if header else "interactive prompt"

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
                for name in list(_idle_child_baseline.keys()):
                    if name not in registered_names:
                        _idle_child_baseline.pop(name, None)
                for name in list(_prev_children.keys()):
                    if name not in registered_names:
                        _prev_children.pop(name, None)
                for name in list(_last_activity_ts.keys()):
                    if name not in registered_names:
                        _last_activity_ts.pop(name, None)
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


def normalize_cwd(cwd: Optional[str]) -> str:
    """Expand ~ and return absolute path; empty string for unset/blank."""
    if cwd is None:
        return ""
    raw = cwd.strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(raw))


def validate_cwd(cwd: Optional[str]) -> tuple[str, str]:
    """Validate cwd path. Returns (normalized_path, error_message)."""
    normalized = normalize_cwd(cwd)
    if not normalized:
        return "", "cwd is empty"
    if not os.path.exists(normalized):
        return "", f"cwd does not exist: {normalized}"
    if not os.path.isdir(normalized):
        return "", f"cwd is not a directory: {normalized}"
    return normalized, ""


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
        return "Working" if pending_lookup(name) else "Ready"

    state, _reason, since = entry
    now = time.time()

    if state == "READY":
        return "Ready"
    if state == "BUSY_TOOL":
        return "Working"
    if state == "BUSY_THINKING":
        return "Thinking"
    if state == "WAITING":
        return "Working"
    if state == "WAITING_INPUT":
        minutes = max(0, int((now - since) / 60)) if since else 0
        return f"Needs reply ({minutes}m)"
    if state == "STUCK":
        minutes = max(0, int((now - since) / 60)) if since else 0
        return f"No progress ({minutes}m)"
    if state == "POISONED":
        minutes = max(0, int((now - since) / 60)) if since else 0
        return f"Error loop ({minutes}m)"
    if state == "DEAD":
        return "Not responding"
    if state == "OFFLINE":
        return "Offline"
    if state == "EXITED":
        return "Session ended"
    if state == "UNTRACKED_BUSY":
        return "Working"
    return state.lower()


def _team_attention_summary(watchdog_status: str, activity: str) -> tuple[str, str, int]:
    """Return (icon, blocker_label, sort_rank) for /team rows."""
    status = (watchdog_status or "").lower()
    act = (activity or "").lower()

    if "rate limit" in act:
        return "🔴", "rate limit", 0
    if "error" in act or "traceback" in act or "not running" in act or "failed" in act:
        return "🔴", "error", 0
    if "needs input" in status or "needs reply" in status:
        return "🟡", "needs reply", 1
    if "stuck" in status or "no progress" in status:
        return "🔴", "stuck", 0
    if "poisoned" in status or "error loop" in status:
        return "🔴", "error loop", 0
    if "dead" in status or "not responding" in status:
        return "🔴", "stopped", 0
    if "offline" in status:
        return "🔴", "offline", 0
    if "exited" in status or "session ended" in status:
        return "🔴", "session ended", 0

    waiting_signals = (
        "waiting for",
        "awaiting",
        "approval",
        "accept edits",
        "confirm",
        "in plan mode",
    )
    if "working (waiting)" in status or any(sig in act for sig in waiting_signals):
        return "🟡", "needs reply", 1

    return "🟢", "ok", 2


def format_team_lines(
    registered: dict,
    active: Optional[str],
    pending_lookup=None,
    worker_live: Optional[dict] = None
) -> list[str]:
    """Format /team response lines with attention, activity, and context."""
    if pending_lookup is None:
        pending_lookup = is_pending
    if worker_live is None:
        worker_live = {}

    with _watchdog_lock:
        state_snapshot = dict(_worker_states)

    backend_values = set()
    for name, session in registered.items():
        live = worker_live.get(name, {})
        backend = normalize_backend(live.get("backend") or session.get("backend"))
        backend_values.add(backend)
    show_backend = len(backend_values) > 1

    rows = []
    counts = {"🔴": 0, "🟡": 0, "🟢": 0}
    for name in sorted(registered.keys()):
        session = registered[name]
        watchdog_status = _format_watchdog_status(name, pending_lookup, state_snapshot=state_snapshot)
        live = worker_live.get(name, {})
        backend = normalize_backend(live.get("backend") or session.get("backend"))

        raw_activity = (live.get("activity") or "").strip()
        if not raw_activity or raw_activity == "Unknown":
            raw_activity = watchdog_status
        activity = _normalize_activity(raw_activity)
        if len(activity) > 42:
            activity = activity[:39].rstrip() + "..."

        context_pct = (live.get("context_pct") or "").strip()
        icon, blocker, severity_rank = _team_attention_summary(watchdog_status, raw_activity)
        counts[icon] += 1

        name_cell = f"{name} 🎯" if name == active else name
        ctx_part = f" | ctx {context_pct}" if context_pct and context_pct != "--" else ""
        row = f"{icon} {name_cell} — {activity}{ctx_part}"
        if show_backend:
            row += f" | backend={backend}"

        focus_rank = 0 if name == active else 1
        rows.append((severity_rank, focus_rank, name, blocker, row))

    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    attention_rows = [f"{name} ({blocker})" for rank, _focus, name, blocker, _row in rows if rank < 2]

    lines = []
    focused = active or "(none)"
    lines.append(
        f"Team: {len(registered)} agents · focused: {focused} | "
        f"🟢 {counts['🟢']} ok · 🟡 {counts['🟡']} need reply · 🔴 {counts['🔴']} blocked"
    )
    if attention_rows:
        lines.append("Needs your reply: " + ", ".join(attention_rows))
    lines.extend(row for _rank, _focus, _name, _blocker, row in rows)
    return lines


def _normalize_activity(raw: str) -> str:
    """Normalize Claude Code spinner verbs to human-friendly text.

    Claude Code TUI shows random verbs like "Ionizing", "Hullaballooing",
    "Schlepping" as thinking spinner text. These are meaningless to managers.
    Normalize single-word spinner verbs to "Thinking (duration)".

    Multi-word activities like "Running Bash", "Compacting conversation",
    "In plan mode", etc. pass through unchanged.
    """
    if not raw:
        return raw
    # Pattern: single capitalized gerund word optionally followed by (duration)
    m = re.match(r'^([A-Z][a-z]+ing)\s*(?:\((.+)\))?\s*$', raw)
    if m:
        verb = m.group(1)
        dur = m.group(2)
        # Known multi-word prefixes that happen to start with a gerund are handled
        # by the regex requiring the FULL string to be one word + optional duration.
        # "Running Bash" won't match because "Bash" follows after a space.
        # "Compacting conversation (5m)" won't match because "conversation" follows.
        # Only single-word verbs like "Ionizing", "Whirring" match.
        if dur:
            return f"Thinking ({dur})"
        return "Thinking"
    return raw


def _extract_activity(lines: list[str]) -> str:
    """Extract a 1-line activity summary from tmux pane output.

    Based on Claude Code v2.1.59 (repo d6ab0ea, 2026-02-26).
    Scans for Claude Code UI signals. Priority:
    1.  Active thinking spinner (· Verb… / * Verb…) — NOT ✻ (past tense)
    2.  Tool actively running (● ToolName( + ⎿ Running…)
    3.  Rate limiting / connection errors (blockers — before prompt check)
    4.  Mode bars (⏵⏵ permission/accept-edits, ⏸ plan mode) vs idle (❯)
    5.  Editor mode ("Save and close editor to continue...")
    6.  Hook execution ("Running SessionStart/PreCompact hooks…")
    7.  Confirmation prompts (plan approval, accept edits, team lead, etc.)
    8.  Task progress (✔/◻)
    9.  Last ● output block (non-tool)
    10. Standalone error line
    """
    if not lines:
        return "Active"

    stripped = [l.strip() for l in lines if l.strip()]
    if not stripped:
        return "Idle"

    # 1. Active thinking spinner — "· Verb…" or "* Verb…" or "✢ Verb…" etc.
    #    Claude Code cycles through various Unicode chars as spinner frames.
    #    ✻ is ALSO a spinner frame (not just past tense) — distinguish by "…" presence.
    #    "✻ Verbing… (49m)" = active; "✻ Thought for 5s" = completed (no "…").
    _ACTIVE_SPINNER_CHARS = {"·", "*", "✢", "✦", "✧", "✹", "✵", "∙", "•", "✻"}
    for raw in reversed(stripped):
        first = raw[0] if raw else ""
        if first == "✻" and "…" not in raw and "..." not in raw:
            continue  # Past tense completed thinking (no ellipsis = done)
        if first not in _ACTIVE_SPINNER_CHARS:
            continue
        # "· Compacting conversation… (5m 26s · thought for 5s)" → verb + duration
        m = re.match(r'^.\s+(.+?)(?:…|\.{3})\s*\(([^()]+)\)\s*$', raw)
        if m:
            verb = m.group(1).strip()
            dur = m.group(2).split('·')[0].strip()
            return f"{verb} ({dur})"
        # Fallback: "· Verb…" or "· Verb" without duration ($ anchor fixes greedy)
        vm = re.match(r'^.\s+(.+?)(?:…|\.{3})?\s*$', raw)
        if vm:
            verb = vm.group(1).strip()
            dm = re.search(r'(\d+m?\s*\d*\.?\d*s)', raw)
            return f"{verb} ({dm.group(1).strip()})" if dm else verb

    # 2. Tool actively running: "● ToolName(...)" followed by "⎿ Running…"
    #    Also handles MCP tools: "● mcp__server__tool("
    last_running_tool = None
    for i, raw in enumerate(stripped):
        m_tool = re.match(r'^●\s*([A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*)\(', raw)
        if not m_tool:
            continue
        tool = m_tool.group(1)
        # Look ahead up to 5 lines for "⎿ Running…" (tolerant of intermediate lines)
        for j in range(i + 1, min(i + 6, len(stripped))):
            s = stripped[j]
            if not s:
                continue
            if s.startswith("⎿"):
                if "Running" in s and "background" not in s:
                    last_running_tool = tool
                break
    if last_running_tool:
        # Shorten MCP tool names: mcp__figma__get_file → figma.get_file
        if last_running_tool.startswith("mcp__"):
            parts = last_running_tool.split("__")
            last_running_tool = ".".join(parts[1:]) if len(parts) > 1 else last_running_tool
        return f"Running {last_running_tool}"

    # 3. Rate limiting / connection errors (BEFORE prompt — blockers override idle)
    for raw in reversed(stripped):
        ll = raw.lower()
        if "rate limit" in ll:
            return "Rate limited — waiting to retry"
        if "connection error" in ll and "retrying" in ll:
            return "Connection error — retrying"
        if ll.startswith("retrying") or "retrying in" in ll:
            return "Retrying API request"

    # 3b. Interactive prompts — Claude Code TUI has taken over input.
    #      These footer lines appear at the bottom when a selection/question UI
    #      is active. The ❯ symbol in these states is a SELECTION CURSOR, not
    #      the text input prompt. Must check BEFORE the ❯ prompt check below.
    #      Uses module-level _INTERACTIVE_FOOTERS list (shared with _extract_question_details).
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                # Try to extract question header (☐ line)
                for q_raw in stripped:
                    if "☐" in q_raw:
                        q = q_raw.replace("☐", "").strip()
                        if q:
                            return f"Waiting for input: {q}"
                return "Waiting for user input"

    # 3c. Content-based interactive detection — prompts without standard footers.
    #      ExitPlanMode, EnterPlanMode, and tool permission prompts may not render
    #      any _INTERACTIVE_FOOTERS pattern (e.g. plan approval only shows "ctrl-g to edit"
    #      when an editor is configured, and nothing at all otherwise).
    #      Must check BEFORE the ❯ prompt check to avoid misclassifying ❯ selection cursor.
    #      Uses module-level _INTERACTIVE_CONTENT list.
    for raw in stripped:
        for pattern in _INTERACTIVE_CONTENT:
            if pattern in raw:
                # Check if this is a plan approval specifically
                if "plan" in raw.lower() and ("proceed" in raw.lower() or "execute" in raw.lower()):
                    return "Waiting for plan approval"
                if "plan mode" in raw.lower():
                    return "Waiting for plan mode decision"
                if raw.startswith("Allow "):
                    return "Waiting for tool permission"
                return "Waiting for user input"

    # 4. Prompt + mode bars — all bottom-bar elements are informational, not blocking
    #    ⏵⏵ bypass permissions on · 1 bash    → mode bar (bypass ON, 1 bash auto-approved)
    #    ⏵⏵ bypass permissions on (shift+tab)  → mode bar (bypass ON, no recent actions)
    #    ⏵⏵ accept edits on (shift+tab)       → mode bar (accept edits mode)
    #    ⏸ plan mode on (shift+tab to cycle)  → plan mode indicator
    #    ❯                                     → idle prompt
    #    ❯ some text                           → idle (auto-suggestion hint, not a message)
    #
    #  "bypass permissions on" means permissions ARE being bypassed — worker is NOT blocked.
    #  The · N action count shows what was auto-approved (informational).
    last_prompt_idx = None
    last_plan_bar_idx = None  # "⏸ plan mode on"
    for i, raw in enumerate(stripped):
        if raw.startswith("❯"):
            last_prompt_idx = i
        if raw.startswith("⏸"):
            last_plan_bar_idx = i

    # ⏸ plan mode bar (persistent at bottom, only if no prompt after it)
    if last_plan_bar_idx is not None:
        if last_prompt_idx is None or last_prompt_idx < last_plan_bar_idx:
            return "In plan mode"

    # Prompt present = ready (text after ❯ is auto-suggestion hint)
    if last_prompt_idx is not None:
        return "Ready"

    # 5. Editor mode — worker waiting for external editor
    for raw in reversed(stripped):
        if "Save and close editor to continue" in raw:
            return "Waiting for external editor"

    # 6. Hook execution — system hooks running
    for raw in reversed(stripped):
        if "Running SessionStart" in raw:
            return "Running SessionStart hooks"
        if "Running PreCompact" in raw:
            return "Running PreCompact hooks"

    # 7. Confirmation prompts (plan approval, team lead, etc.)
    for raw in reversed(stripped):
        if "Do you want to proceed?" in raw or "Would you like to proceed?" in raw:
            return "Waiting for plan approval"
        if "Exit plan mode?" in raw or "Entering plan mode" in raw:
            return "In plan mode"
        if "Waiting for team lead" in raw:
            return "Waiting for team lead approval"

    # 8. Task progress
    done = 0
    total = 0
    for raw in stripped:
        s = raw.lstrip()
        if s.startswith("✔") or s.startswith("✅"):
            done += 1
            total += 1
        elif s.startswith("◻"):
            total += 1
    if total >= 2:
        return f"Tasks ({done}/{total} done)"

    # 9. Last non-tool ● output block
    def _is_block_end(text):
        t = text.lstrip()
        if t.startswith("Context left until auto-compact:"):
            return True
        return t.startswith(("●", "·", "*", "✻", "─", "❯", "⏵", "⏸"))

    for i in range(len(stripped) - 1, -1, -1):
        raw = stripped[i]
        if not raw.startswith("●"):
            continue
        # Skip tool calls (● CapitalWord( or ● mcp__server__tool()
        if re.match(r'^●\s*[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*\(', raw):
            continue
        parts = []
        head = re.sub(r'^●\s*', '', raw).strip()
        if head and not head.startswith("⎿") and not head.startswith("(ctrl+"):
            parts.append(head)
        j = i + 1
        while j < len(stripped):
            nxt = stripped[j]
            if _is_block_end(nxt):
                break
            t = nxt.strip()
            if t and not t.startswith("⎿") and not t.startswith("(ctrl+"):
                parts.append(t)
            j += 1
        if parts:
            msg = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
            if len(msg) > 120:
                msg = msg[:117].rstrip() + "..."
            return msg

    # 10. Standalone error (case-insensitive for broader coverage)
    for raw in reversed(stripped):
        if re.match(r'^(FAIL|ERROR|Error|Traceback|Fail)\b', raw, re.IGNORECASE):
            ll = raw.lower()
            if ll.startswith("error"):
                tail = raw[len("Error"):].lstrip(": ").strip()
                return f"Error: {tail}" if tail else "Error"
            return f"Error: {raw[:60]}"

    return "Active"


def _extract_context_pct(lines: list[str]) -> Optional[str]:
    """Extract context % from tmux output if present."""
    for line in reversed(lines):
        m = re.search(r'Context left.*?(\d+)%', line)
        if m:
            return f"{m.group(1)}%"
    return None


def _read_tmux_activity(tmux_name: str, host: str = None) -> tuple:
    """Read tmux pane and extract activity summary + context% + raw lines.

    Returns (activity_str, context_pct_str_or_None, raw_lines_or_None).
    When host is set, reads from a remote tmux session via SSH.
    """
    try:
        if host:
            result = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host, capture_output=True, text=True, timeout=5
            )
        else:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                capture_output=True, text=True, timeout=3
            )
        if result.returncode != 0:
            return "Unknown", None, None
        lines = result.stdout.split("\n")
        tail = lines[-40:]
        return _extract_activity(tail), _extract_context_pct(tail), tail
    except Exception:
        return "Unknown", None, None


def _wait_for_restart_ready(tmux_name: str, backend_name: str, timeout: float = 45.0, host: str = None) -> bool:
    """Wait until restarted worker is actually back at the prompt."""
    backend = get_backend(backend_name)
    if not backend.is_interactive:
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not tmux_exists(tmux_name, host=host):
            return False
        activity, _, _ = _read_tmux_activity(tmux_name, host=host)
        if activity == "Idle at prompt":
            return True
        time.sleep(0.5)
    return False


# Interactive footer patterns (kept in sync with _extract_activity step 3b)
_INTERACTIVE_FOOTERS = [
    "Enter to select",      # AskUserQuestion single-select
    "Space to toggle",      # AskUserQuestion multi-select
    "Tab to toggle",        # Toggle confirm
    "Type to search",       # Searchable list
    "Enter to submit",      # Text submission prompt
    "Enter to add",         # Autocomplete
    "Enter to retry",       # Retry prompt
    "Enter to continue",    # Continue/proceed prompt
    "Enter to try again",   # Retry variant
    "Enter to confirm",     # Selection confirm variant
    "ctrl-g to edit",       # ExitPlanMode plan approval (editor configured)
    "Auto-approving in",    # ExitPlanMode auto-approve countdown
    "Press any key to intervene",  # ExitPlanMode auto-approve variant
]

# Content patterns that indicate an interactive prompt even without a matching footer.
# These are checked BEFORE the ❯ idle-prompt detection (step 3c) to avoid misclassifying
# the ❯ selection cursor as the text input prompt.
_INTERACTIVE_CONTENT = [
    # ExitPlanMode "Ready to code?" prompt
    "Would you like to proceed?",
    "written up a plan and is ready to execute",
    # EnterPlanMode prompt
    "wants to enter plan mode",
    "No code changes will be made until you approve",
    # Tool permission prompts
    "Allow Bash",
    "Allow Read",
    "Allow Write",
    "Allow Edit",
    "Allow Glob",
    "Allow Grep",
    "Allow Agent",
    "Allow Notebook",
]


def _extract_question_details(lines: list[str]) -> Optional[dict]:
    """Extract interactive question details from tmux pane output.

    Returns dict with:
      header: str — question title from ☐ line (or "")
      options: list of {num: int, label: str, selected: bool}
      selected_num: int — currently selected option number (or 0)
    Returns None if no interactive prompt detected.
    """
    if not lines:
        return None

    stripped = [l.strip() for l in lines if l.strip()]
    if not stripped:
        return None

    # If the idle ❯ prompt appears in the last few lines, the dialog was
    # already dismissed — it's just still visible in scrollback above.
    tail = stripped[-5:]
    if any(line == "❯" for line in tail):
        return None

    # Check for interactive footer or content patterns
    has_interactive = False
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                has_interactive = True
                break
        if has_interactive:
            break
    if not has_interactive:
        for raw in stripped:
            for pattern in _INTERACTIVE_CONTENT:
                if pattern in raw:
                    has_interactive = True
                    break
            if has_interactive:
                break
    if not has_interactive:
        return None

    # Extract header (☐ line)
    header = ""
    for raw in stripped:
        if "☐" in raw:
            header = raw.replace("☐", "").strip()
            break

    # Extract options: lines matching "❯? N. Label" or "  N. Label"
    # Option lines start with optional ❯, then number + dot
    options = []
    selected_num = 0
    opt_re = re.compile(r'^(❯)?\s*(\d+)\.\s+(.+)')
    for raw in stripped:
        m = opt_re.match(raw)
        if m:
            is_selected = m.group(1) == "❯"
            num = int(m.group(2))
            label = m.group(3).strip()
            options.append({"num": num, "label": label, "selected": is_selected})
            if is_selected:
                selected_num = num

    if not options:
        return None

    return {
        "header": header,
        "options": options,
        "selected_num": selected_num,
    }


def _send_interactive_reply(tmux_name: str, reply: str, details: dict, host: str = None) -> bool:
    """Handle manager's reply to an interactive prompt via keystroke navigation.

    reply: "1"-"9" for option selection, "skip"/"cancel" for Escape.
    details: from _extract_question_details().
    Returns True if handled, False if not applicable.
    """
    reply = reply.strip().lower()

    if reply in ("skip", "cancel", "esc"):
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host)
        return True

    if reply.isdigit():
        target_num = int(reply)
        # Find target option index and current selected index
        option_nums = [o["num"] for o in details["options"]]
        if target_num not in option_nums:
            return False

        target_idx = option_nums.index(target_num)
        current_idx = 0
        for i, o in enumerate(details["options"]):
            if o["selected"]:
                current_idx = i
                break

        diff = target_idx - current_idx
        keys = []
        if diff > 0:
            keys = ["Down"] * diff
        elif diff < 0:
            keys = ["Up"] * abs(diff)
        keys.append("Enter")

        for key in keys:
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key], host=host)
            time.sleep(0.05)
        return True

    return False


def format_progress_lines(
    name: str,
    pending: bool,
    backend: str,
    online: bool,
    ready: bool,
    mode: str,
    resume_line: Optional[str] = None,
    continuity_line: Optional[str] = None,
    needs_attention: Optional[str] = None,
    activity: Optional[str] = None,
    context_pct: Optional[str] = None,
    question_details: Optional[dict] = None
) -> list[str]:
    """Format /progress response lines (manager-friendly)."""
    status = []

    # Header with name + context%
    watchdog_status = _format_watchdog_status(name)
    ctx = f" · context {context_pct}" if context_pct else ""
    status.append(f"{name.capitalize()} ({backend}) — {watchdog_status}{ctx}")

    # Activity line (what they're doing right now)
    if activity:
        status.append(f"Doing: {activity}")
    elif not online:
        status.append("Doing: Offline")
    elif pending:
        status.append("Doing: Working on a request")

    # Rich question details (when at interactive prompt)
    if question_details and question_details.get("options"):
        opts = question_details["options"]
        if question_details.get("header"):
            status.append(f"\n{question_details['header']}")
        for o in opts:
            marker = "\u2794 " if o.get("selected") else "  "
            status.append(f"{marker}{o['num']}. {o['label']}")
        max_num = max(o["num"] for o in opts)
        status.append(f"\nReply 1-{max_num} to pick, \"skip\" to cancel")

    # Blockers / attention
    if needs_attention:
        status.append(f"Blocker: {needs_attention}")

    # Session info (non-interactive backends only)
    if continuity_line:
        status.append(continuity_line)
    elif resume_line:
        status.append(resume_line)

    return status


def get_worker_backend(name: str, session: Optional[dict] = None) -> str:
    """Get backend for a worker.

    Priority: backend file (canonical) > session dict (cache) > default.
    The backend file in SESSIONS_DIR/<name>/backend is the single source of
    truth, written at hire time. Session dict may drift if registry or RAM
    state gets stale.
    """
    # Backend file is canonical — check it first
    backend_file = SESSIONS_DIR / name / "backend"
    if backend_file.exists():
        return normalize_backend(backend_file.read_text().strip())
    # Fall back to session dict (cache from registry/tmux)
    if session and session.get("backend"):
        return normalize_backend(session.get("backend"))
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

    def _get_startup_cwd(self, name: str, requested_cwd: str = "", fallback_cwd: str = "") -> str:
        """Resolve startup cwd with priority: explicit > RAM hint > fallback."""
        candidate = normalize_cwd(requested_cwd)
        if not candidate:
            candidate = normalize_cwd(_get_worker_cwd(name))
        if candidate:
            if os.path.isdir(candidate):
                return candidate
            print(f"Ignoring invalid startup cwd for {name}: {candidate}")

        fallback = normalize_cwd(fallback_cwd)
        if fallback and os.path.isdir(fallback):
            return fallback
        return ""

    def _get_tmux_pane_cwd(self, tmux_name: str, host: str = None) -> str:
        """Read current pane cwd for a tmux session."""
        result = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_path}"],
            host=host, capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _cd_tmux_to_cwd(self, tmux_name: str, cwd: str):
        """Change tmux shell cwd before starting backend process."""
        if not cwd:
            return
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, f"cd {shlex.quote(cwd)}", "Enter"])
        time.sleep(0.2)

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

        # Merge persistent registry: workers in registry but not in tmux
        # appear with no "tmux" key (same pattern as non-interactive fallback above).
        # On first run, bootstrap registry from current tmux sessions.
        _registry_bootstrap(registered)
        registry = _load_registry()
        for name, info in registry.get("workers", {}).items():
            if name not in registered:
                entry = {"backend": info.get("backend", DEFAULT_BACKEND)}
                # Teleported workers: inject tmux name so they don't appear as "exited"
                if info.get("host"):
                    entry["tmux"] = f"{self.tmux_prefix}{name}"
                registered[name] = entry

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

        # For teleported workers, check remote tmux session
        host = get_worker_host(name)
        if host:
            return tmux_exists(tmux_name, host=host)

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

            # Registry-only workers (tmux gone): non-interactive can still serve via pipe
            if "tmux" not in info:
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
                    workers.append({
                        "name": name,
                        "protocol": "none",
                        "address": "",
                        "status": "exited",
                        "note": f"Worker exited. Use /restart {name} to bring back.",
                    })
                continue

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
                workers.append({
                    "name": name,
                    "protocol": "tmux",
                    "address": tmux_name,
                    "send_example": f"echo 'YOUR_NAME: your message here' | tmux load-buffer - && tmux paste-buffer -p -r -t {tmux_name} && sleep 1 && tmux send-keys -t {tmux_name} Enter",
                    "note": "Uses paste-buffer -p (bracketed paste) for reliable delivery. Sleep 1s before Enter — TUI needs time to render. Always prefix your name."
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
            f"WORKING DIRECTORY: To switch project directory (reloads CLAUDE.md), run `curl -s \"$BRIDGE_URL/checkin?name={name}&cwd=/path/to/project\"`. "
            "BRIDGE API: Available endpoints: GET /workers, GET /checkin. Messages from manager arrive as prompts — there is NO polling endpoint. "
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
        if not _which_binary(backend_obj.binary):
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
        startup_cwd = self._get_startup_cwd(name)
        if startup_cwd:
            self._cd_tmux_to_cwd(tmux_name, startup_cwd)

        # After tmux new-session succeeds, capture the pane's cwd
        pane_cwd = self._get_tmux_pane_cwd(tmux_name) or startup_cwd
        if pane_cwd:
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
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, append_system_prompt=append_prompt)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
            print(f"Started worker '{name}' in sandbox mode")
        else:
            start_cmd = f'unset CLAUDECODE && {backend_obj.start_cmd(append_system_prompt=append_prompt)}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
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
        _registry_add(name, backend, chat_id)

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
        _set_worker_cwd(name, "")
        # Kill tmux session if it exists (may already be gone for registry-only workers)
        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        cleanup_inbox(name)
        cleanup_worker_pipe(name)
        _registry_remove(name)

        if state["active"] == name:
            state["active"] = None
            self.get_registered_sessions()

        return True, None

    def restart(self, name: str, mode: str = "relaunch"):
        """Restart a worker in its existing tmux session.

        If tmux session is gone but worker is in the persistent registry,
        re-creates the tmux session and restarts the backend (dead worker recovery).
        """
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered:
            return False, f"Worker '{name}' not found"

        session = registered[name]
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        if not tmux_exists(tmux_name):
            # Dead worker recovery: re-create tmux session if worker is in registry
            return self._restart_dead_worker(name, backend_name, backend, tmux_name, mode)

        # Check binary still exists before restarting
        if not _which_binary(backend.binary):
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
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd:
            save_claude_session_cwd(name, startup_cwd)

        # Clear hook failure signal on clean restart
        if mode != "resume":
            _clear_hook_failures(name)

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
            inventory_cwd = startup_cwd or get_claude_session_cwd(name)
            append_prompt = build_mcp_inventory_prompt(inventory_cwd)

        if SANDBOX_ENABLED and backend.is_interactive:
            stop_docker_container(name)
            time.sleep(0.5)
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id, append_system_prompt=append_prompt)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        else:
            start_cmd = backend.start_cmd(resume_id, append_system_prompt=append_prompt)
            start_cmd = f'unset CLAUDECODE && {start_cmd}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])

        # Re-send welcome/instructions so worker gets fresh context after restart
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            time.sleep(2.0 if not SANDBOX_ENABLED else 5.0)
            self.send(name, welcome)
        else:
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"])

        return True, None

    def _restart_dead_worker(self, name: str, backend_name: str, backend, tmux_name: str, mode: str):
        """Re-create a dead worker (tmux gone) from registry.

        Creates a new tmux session, exports env, starts backend, sends welcome.
        Preserves session files (session_id, cwd) for resume capability.
        """
        if not _which_binary(backend.binary):
            return False, f"'{backend.binary}' not found in PATH. Install it first."

        # Create new tmux session
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env
        )
        if result.returncode != 0:
            return False, "Could not create worker workspace"

        time.sleep(0.5)
        export_hook_env(tmux_name, backend_name)
        time.sleep(0.3)

        subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"])
        time.sleep(0.3)

        ensure_session_dir(name)
        if not backend.is_interactive:
            ensure_worker_pipe(name)

        resume_id = ""
        resume_cwd = ""
        if mode == "resume":
            resume_id = get_claude_session_id(name)
            resume_cwd = get_claude_session_cwd(name)
        else:
            session_dir = self.sessions_dir / name
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd:
            save_claude_session_cwd(name, startup_cwd)

        append_prompt = ""
        if backend.name == "claude":
            inventory_cwd = startup_cwd or get_claude_session_cwd(name)
            append_prompt = build_mcp_inventory_prompt(inventory_cwd)

        if SANDBOX_ENABLED and backend.is_interactive:
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id, append_system_prompt=append_prompt)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        else:
            start_cmd = backend.start_cmd(resume_id, append_system_prompt=append_prompt)
            start_cmd = f'unset CLAUDECODE && {start_cmd}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])
            if backend.is_interactive:
                time.sleep(1.5)
                subprocess.run(["tmux", "send-keys", "-t", tmux_name, "2"])
                time.sleep(0.3)
                subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"])

        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            time.sleep(2.0 if not SANDBOX_ENABLED else 5.0)
            self.send(name, welcome)
        else:
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"])

        print(f"Dead worker '{name}' recovered from registry (mode={mode})")
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


def tmux_prompt_empty(tmux_name, timeout=0.5, host: str = None):
    """Check if Claude Code's input prompt is empty (message was accepted).

    After sending a message, polls the tmux pane to verify the prompt
    line (❯) is empty, indicating Claude accepted the input.

    Returns True if prompt is empty within timeout, False otherwise.
    """
    import re
    start = time.time()
    while time.time() - start < timeout:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p"],
            host=host, capture_output=True, text=True
        )
        if result.returncode == 0:
            # Check for empty prompt: line starting with ❯ followed by only whitespace
            if re.search(r'^❯\s*$', result.stdout, re.MULTILINE):
                return True
        time.sleep(0.1)
    return False


def export_hook_env(tmux_name, backend: str = DEFAULT_WORKER_BACKEND, host: str = None):
    """Export env vars for hook inside tmux session.

    Uses tmux set-environment which persists in session and survives restarts.
    Hook reads these via `tmux show-environment -t $SESSION_NAME`.
    """
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)], host=host)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX], host=host)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", str(SESSIONS_DIR)], host=host)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "WORKER_BACKEND", normalize_backend(backend)], host=host)
    # Always export BRIDGE_URL so workers know where their bridge is
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", BRIDGE_URL], host=host)


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


def _fetch_remote_file(host: str, remote_path: str) -> Optional[str]:
    """Fetch a file from a remote host via rsync to a local temp path.

    Returns local temp path on success, None on failure.
    """
    suffix = Path(remote_path).suffix
    fd, local_path = tempfile.mkstemp(suffix=suffix, prefix="remote-file-")
    os.close(fd)
    try:
        r = subprocess.run(
            ["rsync", "-az", f"{host}:{remote_path}", local_path],
            capture_output=True, timeout=15)
        if r.returncode == 0 and os.path.getsize(local_path) > 0:
            return local_path
    except Exception as e:
        print(f"Remote file fetch failed: {host}:{remote_path} -> {e}")
    os.unlink(local_path)
    return None


def _localize_media(name: str, media_list: list) -> list:
    """For teleported workers, fetch remote files to local temp paths."""
    host = get_worker_host(name)
    if not host:
        return media_list
    result = []
    for file_path, caption in media_list:
        if not os.path.exists(file_path):
            local = _fetch_remote_file(host, file_path)
            if local:
                result.append((local, caption))
            else:
                print(f"Cannot fetch remote file {host}:{file_path} for {name}")
        else:
            result.append((file_path, caption))
    return result


def send_response_to_telegram(name: str, text: str, chat_id: int, log_prefix: str = "Response"):
    """Send a response to Telegram. Shared by hook responses.

    Args:
        name: Session/worker name for message prefix
        text: Response text (may contain image/file tags)
        chat_id: Telegram chat ID
        log_prefix: Prefix for log messages (e.g., "Response", "Hook response")
    """
    # Parse image and file tags from text (before converting to preserve tag syntax)
    # For teleported workers, skip local file existence check during parsing
    # (files are on the remote host, not local) — validate after fetching
    host = get_worker_host(name)
    if host:
        _accept_all = lambda p: (True, Path(p))
        clean_text, images = _parse_media_tags(text, "image", _accept_all)
        clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
    else:
        clean_text, images = parse_image_tags(text)
        clean_text, files = parse_file_tags(clean_text)

    # For teleported workers, fetch remote files to local temp paths
    images = _localize_media(name, images)
    files = _localize_media(name, files)
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
        # Restart-all state
        self._restart_all_lock = threading.Lock()
        self._restart_all_running = False
        self._restart_all_abort = threading.Event()
        self._restart_all_thread = None

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
        audio = msg.get("audio")
        voice = msg.get("voice")
        video = msg.get("video")
        video_note = msg.get("video_note")
        sticker = msg.get("sticker")

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
                    self.reply(chat_id, "No focused worker. Use /focus <name> first.")
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    gif_text = f"Manager sent GIF: {local_path}"
                    if text:
                        gif_text = f"{text}\n\n{gif_text}"
                    self._route_media_message(gif_text, text, chat_id, msg_id, msg=msg)
                else:
                    self.reply(chat_id, "Could not download GIF. Try again.")
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
                    self.reply(chat_id, "No focused worker. Use /focus <name> first.")
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    image_text = f"Manager sent image: {local_path}"
                    if text:
                        image_text = f"{text}\n\n{image_text}"
                    self._route_media_message(image_text, text, chat_id, msg_id, msg=msg)
                else:
                    self.reply(chat_id, "Could not download image. Try again or send as file.")
                return

        if document and not doc_is_image and chat_id:
            file_id = document.get("file_id")
            if file_id:
                if admin_chat_id is None:
                    admin_chat_id = chat_id
                elif chat_id != admin_chat_id:
                    return

                if not state["active"]:
                    self.reply(chat_id, "No focused worker. Use /focus <name> first.")
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
                    self._route_media_message(file_text, text, chat_id, msg_id, msg=msg)
                else:
                    self.reply(chat_id, "Could not download file. Try again.")
                return

        # Handle audio, voice, video, video_note, sticker — all have file_id
        media_item = audio or voice or video or video_note or sticker
        if media_item and chat_id:
            file_id = media_item.get("file_id")
            if file_id:
                if admin_chat_id is None:
                    admin_chat_id = chat_id
                elif chat_id != admin_chat_id:
                    return

                if not state["active"]:
                    self.reply(chat_id, "No focused worker. Use /focus <name> first.")
                    return

                local_path = download_telegram_file(file_id, state["active"])
                if local_path:
                    if audio:
                        title = audio.get("title", audio.get("file_name", "audio"))
                        duration = audio.get("duration", 0)
                        media_text = f"Manager sent audio: {title} ({duration}s)\nPath: {local_path}"
                    elif voice:
                        duration = voice.get("duration", 0)
                        media_text = f"Manager sent voice message: ({duration}s)\nPath: {local_path}"
                    elif video:
                        duration = video.get("duration", 0)
                        file_name = video.get("file_name", "video")
                        media_text = f"Manager sent video: {file_name} ({duration}s)\nPath: {local_path}"
                    elif video_note:
                        duration = video_note.get("duration", 0)
                        media_text = f"Manager sent video note: ({duration}s)\nPath: {local_path}"
                    elif sticker:
                        emoji = sticker.get("emoji", "")
                        media_text = f"Manager sent sticker: {emoji}\nPath: {local_path}"
                    else:
                        media_text = f"Manager sent media: {local_path}"
                    if text:
                        media_text = f"{text}\n\n{media_text}"
                    self._route_media_message(media_text, text, chat_id, msg_id, msg=msg)
                else:
                    media_type = "audio" if audio else "voice" if voice else "video" if video else "media"
                    self.reply(chat_id, f"Could not download {media_type}. Try again.")
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
                _last_mention["target"] = None
                _last_mention["count"] = 0
                return

        if text.lower().startswith("@all "):
            message = text[5:]
            self.route_to_all(message, chat_id, msg_id)
            _last_mention["target"] = None
            _last_mention["count"] = 0
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
            # If reply-to message contains media, download and forward it to targets
            if reply_to:
                reply_media = self._extract_reply_media(reply_to, targets[0])
                if reply_media:
                    media_text = reply_media
                    if message:
                        media_text = f"{message}\n\n{reply_media}"
                    for name in targets:
                        self.route_message(name, media_text, chat_id, msg_id, one_off=True)
                else:
                    for name in targets:
                        self.route_message(name, message, chat_id, msg_id, one_off=True)
            else:
                for name in targets:
                    self.route_message(name, message, chat_id, msg_id, one_off=True)

            # Auto-focus: if same single worker mentioned 2+ consecutive times, switch focus
            if len(targets) == 1:
                target = targets[0]
                if _last_mention["target"] == target:
                    _last_mention["count"] += 1
                else:
                    _last_mention["target"] = target
                    _last_mention["count"] = 1
                if _last_mention["count"] >= 2 and state["active"] != target:
                    state["active"] = target
                    save_last_active(target)
                    self.reply(chat_id, f"Switched to {target} (you mentioned them twice).")
            else:
                # Multi-mention resets streak
                _last_mention["target"] = None
                _last_mention["count"] = 0
            return

        # No @mentions → route to focused worker (resets mention streak)
        _last_mention["target"] = None
        _last_mention["count"] = 0
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
        elif cmd == "/pilot":
            return self.cmd_pilot(arg, chat_id)
        elif cmd == "/teleport":
            return self.cmd_teleport(arg, chat_id)
        elif cmd == "/teleport-check":
            return self.cmd_teleport(arg, chat_id, check_only=True)
        elif cmd == "/teleback":
            return self.cmd_teleback(arg, chat_id)
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

    def cmd_pilot(self, name, chat_id):
        if not name:
            self.reply(chat_id, "Usage: /pilot <name>", outcome="Needs decision")
            return True
        name = name.lower().strip()
        # Resolve to tmux session name
        prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
        session_name = f"{prefix}{name}" if not name.startswith("claude-") else name
        pilot_port = os.environ.get("PILOT_PORT", "10170")
        try:
            import urllib.request
            import json as _json
            # Pass remote host to pilot if worker is teleported
            worker_host = get_worker_host(name)
            url = f"http://localhost:{pilot_port}/api/pilot?session={session_name}"
            if worker_host:
                url += f"&host={urllib.parse.quote(worker_host)}"
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            # Derive host from BRIDGE_PUBLIC_URL (auto-detected at startup)
            from urllib.parse import urlparse
            host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "localhost"
            pilot_url = f"http://{host}:{pilot_port}/session/{session_name}"
            self.reply(chat_id, f"✈️ Pilot on for {name} (5min)\n{pilot_url}")
        except Exception as e:
            self.reply(chat_id, f"Pilot error: {e}", outcome="Needs decision")
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

        worker_live = {}
        for name, session in registered.items():
            backend_name = get_worker_backend(name, session)
            activity = None
            context_pct = None

            tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}")
            host = get_worker_host(name)
            tmux_alive = "tmux" in session and tmux_exists(tmux_name, host=host)
            if tmux_alive:
                backend = get_backend(backend_name)
                if backend.is_interactive:
                    if is_claude_running(tmux_name, host=host):
                        activity, context_pct, _ = _read_tmux_activity(tmux_name, host=host)
                    else:
                        activity = "worker app not running"
                else:
                    activity = "handling async requests"

            worker_live[name] = {
                "backend": backend_name,
                "activity": activity,
                "context_pct": context_pct,
            }

        lines = format_team_lines(registered, state["active"], worker_live=worker_live)
        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_end(self, name, chat_id):
        if not name:
            self.reply(chat_id, "This is permanent. Usage: /end <name>", outcome="Needs decision")
            return True

        name = name.lower().strip()
        ok, err = kill_session(name)
        if ok:
            self.reply(chat_id, f"{name.capitalize()} removed from your team.")
            update_bot_commands()
        else:
            self.reply(chat_id, f"Could not remove \"{name}\". {err}", outcome="Needs decision")
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
        host = get_worker_host(name)
        is_tmux_alive = "tmux" in session and tmux_exists(tmux_name, host=host)
        if not is_tmux_alive:
            # Worker exited (tmux gone, in registry only)
            online = False
            ready = False
            mode = f"{backend_name} (exited)"
            needs_attention = "Session exited. Use /restart to bring back."
        elif not backend.is_interactive:
            # Non-interactive: online = tmux exists, ready = always (stateless)
            online = True
            ready = True
            mode = f"{backend_name} (non-interactive)"
        else:
            online = True
            claude_running = is_claude_running(tmux_name, host=host)
            ready = claude_running
            if not claude_running:
                needs_attention = "Not running. Use /restart."

        resume_line = None
        continuity_line = None

        if not backend.is_interactive:
            # Non-interactive: show Continuity (thread) + In-flight
            session_id, source = get_any_session_id(name)
            if session_id:
                continuity_line = "Continuity: on"
            else:
                continuity_line = "Continuity: off (next message starts new thread)"
        else:
            # Interactive: show Resume
            resume_id = get_claude_session_id(name)
            if resume_id:
                resume_line = "Resume: available"
            else:
                resume_line = "Resume: not available"

        # Read live activity from tmux pane
        activity = None
        context_pct = None
        raw_lines = None
        if is_tmux_alive and ready:
            activity, context_pct, raw_lines = _read_tmux_activity(tmux_name, host=host)

        # Extract question details if at interactive prompt
        question_details = None
        if raw_lines and activity and "Waiting for" in activity:
            question_details = _extract_question_details(raw_lines)

        status = format_progress_lines(
            name=name,
            pending=pending,
            backend=backend_name,
            online=online,
            ready=ready,
            mode=mode,
            resume_line=resume_line,
            continuity_line=continuity_line,
            needs_attention=needs_attention,
            activity=activity,
            context_pct=context_pct,
            question_details=question_details
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
            host = get_worker_host(name)
            tmux_send_escape(session["tmux"], host=host)
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

        # Branch: /restart cancel
        if name_arg == "cancel":
            return self._cmd_restart_cancel(chat_id)

        # Branch: /restart all [--clean]
        if name_arg == "all":
            return self._cmd_restart_all(chat_id, clean)

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

        # Teleported worker: delegate to remote restart
        host = get_worker_host(name)
        if host:
            mode = "relaunch" if clean else "resume"
            backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)
            tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}") if session else f"{self.workers.tmux_prefix}{name}"
            self.reply(chat_id, f"Restarting {name.capitalize()} on remote host...")
            ok, err = self._restart_remote_worker(
                name, backend_name, backend_obj, tmux_name, host, mode)
            if ok:
                _recent_restarts[name] = time.time()
                self.reply(chat_id, f"{name.capitalize()} is back and ready.")
            else:
                self.reply(chat_id, f"Could not restart \"{name}\" on {host}. {err}",
                           outcome="Needs decision")
            return True

        # --clean: fresh start (clear session IDs)
        if clean:
            ok, err = restart_claude(name, mode="relaunch")
            if ok:
                _recent_restarts[name] = time.time()
                self.reply(chat_id, f"Bringing {name.capitalize()} back online...")
                self.reply(chat_id, f"{name.capitalize()} is back and ready.")
            else:
                self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
            return True

        # Default: resume behavior
        backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
        backend = get_backend(backend_name)

        # Non-interactive backends: resume is automatic via saved thread ID
        # (but only if tmux is still alive — dead workers need full restart)
        tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}") if session else f"{self.workers.tmux_prefix}{name}"
        worker_alive = session and "tmux" in session and tmux_exists(tmux_name)
        if not backend.is_interactive and worker_alive:
            session_id, source = get_any_session_id(name)
            if session_id:
                self.reply(chat_id, f"{name.capitalize()} is still active. Next message continues where you left off.")
            else:
                self.reply(chat_id, f"No active session for {name.capitalize()}. Next message starts fresh.")
            return True

        # Interactive backends: restart with --resume
        session_dir = get_session_dir(name)
        has_session_id = False
        if session_dir.exists():
            has_session_id = any(session_dir.glob("*_session_id"))

        if not has_session_id:
            ok, err = restart_claude(name, mode="relaunch")
            if ok:
                _recent_restarts[name] = time.time()
                self.reply(chat_id, f"Restarting {name.capitalize()} fresh...")
                self.reply(chat_id, f"{name.capitalize()} is back and ready.")
            else:
                self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
            return True

        ok, err = restart_claude(name, mode="resume")
        if ok:
            _recent_restarts[name] = time.time()
            self.reply(chat_id, f"Resuming {name.capitalize()}...")
            self.reply(chat_id, f"{name.capitalize()} is back and ready.")
        else:
            self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
        return True

    # ── Remote Restart ──────────────────────────────────────────────

    def _restart_remote_worker(self, name, backend_name, backend, tmux_name, host, mode):
        """Restart a teleported worker on its remote host.

        Reuses _stop_worker_for_teleport + _start_worker_on_target which
        already handle remote tmux, $HOME remapping, credential sync, etc.
        """
        resume_id = ""
        target_cwd = get_claude_session_cwd(name)
        if mode == "resume":
            resume_id = get_claude_session_id(name)
        else:
            # Clear session IDs for relaunch
            session_dir = SESSIONS_DIR / name
            session_dir.mkdir(parents=True, exist_ok=True)
            for f in session_dir.glob("*_session_id"):
                f.unlink()
            _clear_hook_failures(name)

        # Stop the remote Claude process if tmux is still alive
        if tmux_exists(tmux_name, host=host):
            self._stop_worker_for_teleport(name, tmux_name, host=host)
            # Kill tmux — _start_worker_on_target creates a fresh one
            _remote_run(["tmux", "kill-session", "-t", tmux_name],
                         host=host, capture_output=True)
            time.sleep(0.5)

        # Re-read session_id (hook may have updated during /exit)
        if mode == "resume":
            resume_id = get_claude_session_id(name) or resume_id

        # Delegate to existing remote start flow
        ok = self._start_worker_on_target(
            name, host, target_cwd, resume_id, backend_name)
        if not ok:
            return False, f"Failed to restart {name} on {host}"

        # Send welcome
        welcome = self.workers._build_welcome(name, backend)
        if backend.is_interactive:
            time.sleep(3.0)
            self.workers.send(name, welcome)

        print(f"Remote worker '{name}' restarted on {host} (mode={mode})")
        return True, None

    # ── Restart All (sequential) ──────────────────────────────────

    def _cmd_restart_all(self, chat_id, clean: bool):
        registered = self.workers.get_registered_sessions()
        if not registered:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True

        with self._restart_all_lock:
            if self._restart_all_running:
                self.reply(chat_id, "A /restart all is already running. Use /restart cancel to stop it.")
                return True
            self._restart_all_running = True
            self._restart_all_abort.clear()

        # Snapshot worker names now; sort alphabetically, focused worker last
        names = sorted(registered.keys())
        active = state.get("active")
        if active and active in names:
            names.remove(active)
            names.append(active)

        mode = "relaunch" if clean else "resume"
        self.reply(chat_id, f"Restarting {len(names)} workers sequentially ({mode})...")

        self._restart_all_thread = threading.Thread(
            target=self._run_restart_all_sequence,
            args=(chat_id, names, mode),
            daemon=True,
        )
        self._restart_all_thread.start()
        return True

    def _run_restart_all_sequence(self, chat_id, names, mode):
        delay_s = 2.5
        failed = []
        try:
            total = len(names)
            for i, name in enumerate(names, 1):
                if self._restart_all_abort.is_set():
                    self.reply(chat_id, f"Restart sequence aborted at {i-1}/{total}.")
                    return

                host = get_worker_host(name)
                if host:
                    _sync_worker_manager()
                    reg = worker_manager.get_registered_sessions()
                    session = reg.get(name, {})
                    backend_name = get_worker_backend(name, session)
                    backend_obj = get_backend(backend_name)
                    tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}")
                    ok, err = self._restart_remote_worker(
                        name, backend_name, backend_obj, tmux_name, host, mode)
                else:
                    ok, err = restart_claude(name, mode=mode)
                if ok:
                    self.reply(chat_id, f"[{i}/{total}] {name.capitalize()} restarted.")
                else:
                    failed.append((name, err))
                    self.reply(chat_id, f"[{i}/{total}] {name.capitalize()} failed: {err}")

                if i < total:
                    # Interruptible sleep
                    for _ in range(5):
                        if self._restart_all_abort.is_set():
                            break
                        time.sleep(delay_s / 5)

            if failed:
                summary = ", ".join(n for n, _ in failed)
                self.reply(chat_id, f"Restart all done. {len(failed)} failed: {summary}")
            else:
                self.reply(chat_id, f"Restart all done. All {total} workers restarted.")
        finally:
            with self._restart_all_lock:
                self._restart_all_running = False
                self._restart_all_abort.clear()
                self._restart_all_thread = None

    def _cmd_restart_cancel(self, chat_id):
        with self._restart_all_lock:
            if not self._restart_all_running:
                self.reply(chat_id, "No restart-all sequence is running.")
                return True
            self._restart_all_abort.set()
        self.reply(chat_id, "Stopping restart-all sequence...")
        return True

    # ── Teleport commands ──────────────────────────────────────────────────

    def cmd_teleport(self, arg, chat_id, check_only=False):
        """Teleport a worker to a remote machine."""
        if not arg:
            cmd_name = "/teleport-check" if check_only else "/teleport"
            self.reply(chat_id, f"Usage: {cmd_name} <worker> <host>[:/path]")
            return True

        parts = arg.split()
        worker_name = parts[0].lower()
        target_spec = " ".join(parts[1:]) if len(parts) > 1 else ""

        if not target_spec:
            self.reply(chat_id, "Usage: /teleport <worker> <host>[:/path] [--full]")
            return True

        full_sync = "--full" in target_spec
        target_spec = target_spec.replace("--full", "").strip()

        # Parse target_host:target_cwd
        if ":" in target_spec and not target_spec.startswith("/"):
            target_host, target_cwd = target_spec.split(":", 1)
        else:
            target_host = target_spec
            target_cwd = ""

        # 1. Worker exists?
        registry = _load_registry()
        worker_entry = registry.get("workers", {}).get(worker_name)
        if not worker_entry:
            self.reply(chat_id, f"Worker '{worker_name}' not found in registry.")
            return True
        backend_name = worker_entry.get("backend", "claude")

        # 2. Worker not actively busy? (EXITED/OFFLINE/UNKNOWN are all fine)
        with _watchdog_lock:
            ws = _worker_states.get(worker_name, ("UNKNOWN", "", 0))
        current_state = ws[0]
        if current_state in ("BUSY_TOOL", "BUSY_THINKING"):
            self.reply(chat_id,
                f"{worker_name} is busy. Must be idle to teleport.\n"
                f"Wait for it to finish or /pause {worker_name} first.")
            return True

        # 3. No teleport in progress?
        teleport_file = SESSIONS_DIR / worker_name / "teleport_state"
        if teleport_file.exists():
            self.reply(chat_id, f"{worker_name} has a teleport in progress.")
            return True

        # 4. Target reachable?
        r = _remote_run(["echo", "ok"], host=target_host,
                        capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            self.reply(chat_id, f"Cannot reach {target_host} via SSH.")
            return True

        # 5. Claude Code on target?
        r = _remote_run(["which", "claude"], host=target_host,
                        capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            self.reply(chat_id, f"claude not found on {target_host}. Install it first.")
            return True

        # 6. tmux on target?
        r = _remote_run(["which", "tmux"], host=target_host,
                        capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            self.reply(chat_id, f"tmux not found on {target_host}. Install it first.")
            return True

        # 7. Need a reachable URL for remote workers
        target_bridge_url = BRIDGE_PUBLIC_URL or BRIDGE_URL
        if "localhost" in target_bridge_url or "127.0.0.1" in target_bridge_url:
            self.reply(chat_id,
                "Cannot teleport: no reachable bridge URL. "
                "Set BRIDGE_PUBLIC_URL to this machine's network IP "
                "(e.g., BRIDGE_PUBLIC_URL=http://100.125.36.102:8271).")
            return True

        # 8. Bridge must be reachable from target
        r = _remote_run(["curl", "-sf", "--connect-timeout", "5",
                         f"{target_bridge_url}/health"],
                        host=target_host, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            self.reply(chat_id,
                f"Target {target_host} cannot reach {target_bridge_url}. "
                f"Ensure BRIDGE_BIND=0.0.0.0 and network connectivity.")
            return True

        # 9. Claude credentials on target?
        r = _remote_run(["test", "-f", ".claude/.credentials.json"],
                        host=target_host, capture_output=True, timeout=5)
        if r.returncode != 0:
            # Try to sync credentials from source
            local_creds = os.path.expanduser("~/.claude/.credentials.json")
            if os.path.exists(local_creds):
                _remote_run(["mkdir", "-p", ".claude"],
                             host=target_host, capture_output=True)
                subprocess.run(
                    ["rsync", "-az", local_creds,
                     f"{target_host}:.claude/.credentials.json"],
                    capture_output=True, timeout=10)
                _remote_run(["chmod", "600", ".claude/.credentials.json"],
                             host=target_host, capture_output=True)
                self._teleport_notify(chat_id, "Synced credentials to target.")
            else:
                self.reply(chat_id,
                    f"No Claude credentials on {target_host} or locally. "
                    f"Run: ssh {target_host} claude login")
                return True

        # 10. Hooks installed on target?
        r = _remote_run(["test", "-f", ".claude/hooks/send-to-telegram.sh"],
                        host=target_host, capture_output=True, timeout=5)
        if r.returncode != 0:
            self._teleport_notify(chat_id, "Hooks missing on target — will install during teleport.")

        # 11. Team-defined preflight checks
        preflight_fails = self._run_teleport_preflight(
            target_host, worker_name, backend_name)
        if preflight_fails:
            self.reply(chat_id,
                f"Preflight failed:\n" + "\n".join(f"  - {f}" for f in preflight_fails))
            return True

        # All checks pass
        if check_only:
            self.reply(chat_id,
                f"Preflight OK — {worker_name} is clear to teleport to {target_host}.")
            return True

        self.reply(chat_id, f"Teleporting {worker_name} to {target_host}...")
        threading.Thread(
            target=self._do_teleport,
            args=(worker_name, target_host, target_cwd, full_sync, chat_id),
            daemon=True
        ).start()
        return True

    def cmd_teleback(self, arg, chat_id):
        """Bring a teleported worker back to its previous machine."""
        parts = arg.split()
        worker_name = parts[0].lower() if parts else ""
        full_sync = "--full" in parts

        if not worker_name:
            self.reply(chat_id, "Usage: /teleback <worker> [--full]")
            return True

        registry = _load_registry()
        worker = registry.get("workers", {}).get(worker_name)
        if not worker:
            self.reply(chat_id, f"Worker '{worker_name}' not in registry.")
            return True

        current_host = worker.get("host")
        home_host = worker.get("home_host")
        home_cwd = worker.get("home_cwd")

        if current_host is None and home_cwd is None:
            self.reply(chat_id, f"{worker_name} hasn't been teleported.")
            return True

        # Worker must not be actively busy
        with _watchdog_lock:
            ws = _worker_states.get(worker_name, ("UNKNOWN", "", 0))
        if ws[0] in ("BUSY_TOOL", "BUSY_THINKING"):
            self.reply(chat_id,
                f"{worker_name} is busy. Must be idle to teleback.")
            return True

        target_host = home_host  # Where we're going back to (None = local)
        target_cwd = home_cwd or get_claude_session_cwd(worker_name)

        # If going back to local, verify current remote host is reachable
        if current_host:
            r = _remote_run(["echo", "ok"], host=current_host,
                            capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                self.reply(chat_id,
                    f"Cannot reach {current_host} where {worker_name} currently is.")
                return True

        # Conflict check: detect if both sides changed the working directory
        if not full_sync:
            conflicts = self._check_teleback_conflicts(
                worker_name, current_host, target_cwd)
            if conflicts:
                self.reply(chat_id,
                    f"Teleback conflict detected for {worker_name}:\n"
                    + "\n".join(f"  {c}" for c in conflicts)
                    + "\n\nUse /teleback " + worker_name + " --full to force sync "
                    "(remote overwrites local).")
                return True

        dest_label = home_host or "local"
        self.reply(chat_id, f"Bringing {worker_name} back to {dest_label}...")
        threading.Thread(
            target=self._do_teleport,
            args=(worker_name, target_host, target_cwd, full_sync, chat_id, True),
            daemon=True
        ).start()
        return True

    def _check_teleback_conflicts(self, name, remote_host, local_cwd):
        """Check for working directory conflicts before teleback.

        Compares git status on both remote (where worker is) and local
        (VPS, where worker is coming back to). If both sides have
        uncommitted changes or new commits, report conflicts.

        Returns list of conflict descriptions, or empty list if clean.
        """
        conflicts = []
        if not local_cwd or not remote_host:
            return conflicts

        # Check if it's a git repo locally
        local_is_git = os.path.isdir(os.path.join(local_cwd, ".git"))
        if not local_is_git:
            return conflicts  # Not a git repo — rsync is the only option

        # Get local (VPS) git status — uncommitted changes + recent commits
        local_status = subprocess.run(
            ["git", "-C", local_cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10)
        local_changed = bool(local_status.stdout.strip()) if local_status.returncode == 0 else False

        local_head = subprocess.run(
            ["git", "-C", local_cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
        local_commit = local_head.stdout.strip() if local_head.returncode == 0 else ""

        # Get remote git status
        # Detect remote CWD (may have different $HOME prefix)
        r_home = _remote_run(
            ["bash", "-c", "echo $HOME"], host=remote_host,
            capture_output=True, text=True, timeout=5)
        remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
        local_home = os.path.expanduser("~")

        remote_cwd = local_cwd
        if remote_home and remote_home != local_home and local_cwd.startswith(local_home):
            remote_cwd = remote_home + local_cwd[len(local_home):]

        r_status = _remote_run(
            ["git", "-C", remote_cwd, "status", "--porcelain"],
            host=remote_host, capture_output=True, text=True, timeout=10)
        remote_changed = bool(r_status.stdout.strip()) if r_status.returncode == 0 else False

        r_head = _remote_run(
            ["git", "-C", remote_cwd, "rev-parse", "HEAD"],
            host=remote_host, capture_output=True, text=True, timeout=5)
        remote_commit = r_head.stdout.strip() if r_head.returncode == 0 else ""

        # Conflict: both sides have uncommitted changes
        if local_changed and remote_changed:
            local_files = [l.strip().split(None, 1)[-1]
                          for l in local_status.stdout.strip().splitlines()[:5]]
            remote_files = [l.strip().split(None, 1)[-1]
                           for l in r_status.stdout.strip().splitlines()[:5]]
            conflicts.append(
                f"VPS has uncommitted changes: {', '.join(local_files)}")
            conflicts.append(
                f"Remote has uncommitted changes: {', '.join(remote_files)}")

        # Conflict: commits diverged
        elif local_commit and remote_commit and local_commit != remote_commit:
            if local_changed:
                conflicts.append(
                    f"VPS has uncommitted changes AND different commit than remote")
            elif remote_changed:
                # Remote changed, local has new commits — this is the normal case
                # (VPS got new commits while worker was away, worker made changes)
                conflicts.append(
                    f"VPS has new commits since teleport (HEAD: {local_commit[:8]})")
                conflicts.append(
                    f"Remote has uncommitted changes (HEAD: {remote_commit[:8]})")

        return conflicts

    def _do_teleport(self, name, target_host, target_cwd, full_sync,
                     chat_id, is_teleback=False):
        """Run the full teleport flow in a background thread."""
        try:
            registered = self.workers.get_registered_sessions()
            session = registered.get(name, {})
            tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
            backend_name = get_worker_backend(name, session)
            source_host = get_worker_host(name)

            source_cwd = get_claude_session_cwd(name)
            if not target_cwd:
                target_cwd = source_cwd
                # Remap home directory when source and target have different $HOME
                # e.g., /home/claude/project → /Users/beastoinagents/project
                if target_cwd and target_host:
                    local_home = os.path.expanduser("~")
                    r_home = _remote_run(
                        ["bash", "-c", "echo $HOME"], host=target_host,
                        capture_output=True, text=True, timeout=5)
                    remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
                    if remote_home and remote_home != local_home and target_cwd.startswith(local_home):
                        target_cwd = remote_home + target_cwd[len(local_home):]

            # Write teleport state for crash recovery
            ensure_session_dir(name)
            state_file = SESSIONS_DIR / name / "teleport_state"
            state_file.write_text(json.dumps({
                "phase": 1, "source_host": source_host,
                "target_host": target_host, "target_cwd": target_cwd,
                "started_at": int(time.time()),
            }))

            # ── PHASE 1: Stop and sync (reversible) ──

            self._teleport_notify(chat_id, f"Stopping {name}...")
            session_id = self._stop_worker_for_teleport(name, tmux_name, source_host)

            if source_cwd and target_cwd:
                self._teleport_notify(chat_id, f"Syncing working directory...")
                ok = self._sync_working_directory(
                    source_cwd, target_cwd, source_host, target_host, full_sync)
                if not ok:
                    self._teleport_rollback(name, tmux_name, source_host, source_cwd,
                                            session_id, backend_name, chat_id,
                                            "working directory sync failed")
                    return

            if session_id:
                self._teleport_notify(chat_id, "Syncing session transcript...")
                self._sync_session_transcript(
                    session_id, source_cwd, target_cwd, source_host, target_host)

            # On teleport out: push team configs + install hooks on target
            # On teleback: skip — VPS is source of truth for team-scope config
            if not is_teleback:
                self._teleport_notify(chat_id, "Syncing team config and hooks...")
                self._sync_shared_repos(target_host, chat_id)
                self._install_hooks_on_target(target_host)

            # ── PHASE 2: Commit ──

            state_file.write_text(json.dumps({
                "phase": 2, "source_host": source_host,
                "target_host": target_host, "target_cwd": target_cwd,
                "started_at": int(time.time()),
            }))

            self._teleport_notify(chat_id,
                f"Starting {name} on {target_host or 'local'}...")
            ok = self._start_worker_on_target(
                name, target_host, target_cwd, session_id, backend_name)
            if not ok:
                # Clean up target, restart source
                _remote_run(["tmux", "kill-session", "-t", tmux_name],
                            host=target_host, capture_output=True)
                self._teleport_rollback(name, tmux_name, source_host, source_cwd,
                                        session_id, backend_name, chat_id,
                                        "failed to start on target")
                return

            # Update registry BEFORE killing source (crash-safe: if we crash
            # between here and kill, bridge still knows where the worker is)
            if is_teleback:
                _registry_clear_teleport(name)
            else:
                _registry_update_teleport(
                    name, host=target_host,
                    home_host=source_host, home_cwd=source_cwd)

            # Point of no return: kill source
            _remote_run(["tmux", "kill-session", "-t", tmux_name],
                        host=source_host, capture_output=True)

            # On teleback: sync only worker-scoped data back
            # VPS is source of truth — workers don't override team-scope config
            if is_teleback:
                self._sync_worker_data_back(name, source_host)

            save_claude_session_cwd(name, target_cwd)

            # Auto-inject worker context so teleported worker knows about
            # the Telegram bridge (without waiting for next SessionStart event)
            if not is_teleback:
                try:
                    backend_obj = get_backend(backend_name)
                    welcome = self.workers._build_welcome(name, backend_obj)
                    time.sleep(3)  # Let Claude finish loading
                    self.workers.send(name, welcome)
                except Exception as e:
                    print(f"[teleport] Warning: failed to send welcome to {name}: {e}")

            state_file.unlink(missing_ok=True)

            dest_label = target_host or "local"
            action = "teleported back" if is_teleback else "teleported"
            msg = f"{name} {action} to {dest_label}:{target_cwd}"
            if session_id:
                msg += f"\nSession resumed ({session_id[:8]}...)."
            if not is_teleback:
                msg += f"\nUse /teleback {name} to bring it back."
            self._teleport_notify(chat_id, msg)

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._teleport_notify(chat_id, f"Teleport failed: {e}")
            try:
                state_file = SESSIONS_DIR / name / "teleport_state"
                state_file.unlink(missing_ok=True)
            except Exception:
                pass

    def _stop_worker_for_teleport(self, name, tmux_name, host=None):
        """Gracefully stop Claude Code and return session_id."""
        session_id = get_claude_session_id(name)

        # Send /exit for graceful shutdown
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"],
                     host=host, capture_output=True)

        # Wait for process to exit (up to 10s)
        for _ in range(20):
            time.sleep(0.5)
            r = _remote_run(
                ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
                host=host, capture_output=True, text=True)
            if r.returncode != 0:
                break
            pane_pid = r.stdout.strip()
            if pane_pid:
                claude_pid = _get_claude_pid(pane_pid, host=host)
                if not claude_pid:
                    break
        else:
            # Force stop
            _remote_run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""],
                         host=host, capture_output=True)
            time.sleep(1)

        # Re-read session ID (hook may have updated it during /exit)
        return get_claude_session_id(name) or session_id

    def _sync_working_directory(self, source_cwd, target_cwd,
                                 source_host=None, target_host=None,
                                 full=False):
        """rsync working directory from source to target."""
        _remote_run(["mkdir", "-p", target_cwd],
                     host=target_host, capture_output=True)

        cmd = ["rsync", "-az", "--delete"]
        gitignore_tmpfile = None
        if not full:
            # Build exclude list from git ls-files on the source side.
            # --filter ':- .gitignore' breaks on macOS openrsync, so we
            # generate an exclude file from git instead (portable).
            try:
                gi_result = _remote_run(
                    ["git", "-C", source_cwd, "ls-files",
                     "--others", "--ignored", "--exclude-standard",
                     "--directory"],
                    host=source_host, capture_output=True, text=True, timeout=15)
                if gi_result.returncode == 0 and gi_result.stdout.strip():
                    fd, gitignore_tmpfile = tempfile.mkstemp(
                        prefix="rsync-gitignore-", suffix=".txt")
                    os.write(fd, gi_result.stdout.encode())
                    os.close(fd)
                    if source_host:
                        # Excludes are relative paths — works locally with --exclude-from
                        cmd.extend(["--exclude-from", gitignore_tmpfile])
                    else:
                        cmd.extend(["--exclude-from", gitignore_tmpfile])
            except Exception as e:
                print(f"[teleport] git ls-files failed, skipping gitignore excludes: {e}")

            for excl in TELEPORT_RSYNC_EXCLUDES:
                cmd.extend(["--exclude", excl])

        src = source_cwd.rstrip("/") + "/"
        dst = target_cwd.rstrip("/") + "/"

        if source_host:
            cmd.extend([f"{source_host}:{src}", dst])
        elif target_host:
            cmd.extend([src, f"{target_host}:{dst}"])
        else:
            cmd.extend([src, dst])

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                print(f"[teleport] rsync failed: cmd={cmd} rc={r.returncode} stderr={r.stderr[:500]}")
            return r.returncode == 0
        finally:
            if gitignore_tmpfile and os.path.exists(gitignore_tmpfile):
                os.unlink(gitignore_tmpfile)

    def _sync_session_transcript(self, session_id, source_cwd, target_cwd,
                                  source_host=None, target_host=None):
        """Sync Claude Code session transcript between machines."""
        if not session_id:
            return

        source_slug = _project_slug(source_cwd)
        target_slug = _project_slug(target_cwd)

        # _remote_run shell-quotes args, so ~ won't expand. Use $HOME instead
        # for remote commands, and os.path.expanduser for local paths.
        source_dir = f".claude/projects/{source_slug}"
        target_dir = f".claude/projects/{target_slug}"

        # Ensure target directory exists (use bash -c for $HOME expansion)
        if target_host:
            _remote_run(["bash", "-c", f"mkdir -p $HOME/{target_dir}"],
                         host=target_host, capture_output=True)
        else:
            os.makedirs(os.path.expanduser(f"~/{target_dir}"), exist_ok=True)

        # Sync session JSONL and subdirectory
        jsonl = f"{session_id}.jsonl"
        for item in [jsonl, f"{session_id}/"]:
            if source_host:
                # rsync handles ~ in remote paths (not shell-quoted by _remote_run)
                src_path = f"~/{source_dir}/{item}"
                local_dst = os.path.expanduser(f"~/{target_dir}/")
                cmd = ["rsync", "-az", f"{source_host}:{src_path}", local_dst]
            elif target_host:
                local_src = os.path.expanduser(f"~/{source_dir}/{item}")
                dst_path = f"~/{target_dir}/"
                cmd = ["rsync", "-az", local_src, f"{target_host}:{dst_path}"]
            else:
                local_src = os.path.expanduser(f"~/{source_dir}/{item}")
                local_dst = os.path.expanduser(f"~/{target_dir}/")
                cmd = ["rsync", "-az", local_src, local_dst]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"[teleport] transcript sync failed for {item}: {r.stderr[:200]}")

    def _sync_shared_repos(self, target_host, chat_id=None):
        """Sync team and agent-config git repos between VPS and target.

        VPS hosts bare repos at ~/git/{team,agent-config}.git.
        Both VPS working copies and target clones use these as origin.
        Push from source, pull on target.
        """
        if not target_host:
            return  # Local — already in sync

        home = os.path.expanduser("~")
        git_repos = {
            "team": os.path.join(home, "team"),
            "agent-config": os.path.join(home, "agent-config"),
        }

        # Push local changes to bare repo (VPS side)
        for repo_name, repo_path in git_repos.items():
            if os.path.isdir(os.path.join(repo_path, ".git")):
                subprocess.run(
                    ["git", "-C", repo_path, "add", "-A"],
                    capture_output=True, timeout=10)
                subprocess.run(
                    ["git", "-C", repo_path, "commit", "-m",
                     f"teleport sync: {repo_name}"],
                    capture_output=True, timeout=10)
                subprocess.run(
                    ["git", "-C", repo_path, "push", "origin", "master"],
                    capture_output=True, timeout=15)

        # Pull on target
        for repo_name in git_repos:
            _remote_run(
                ["bash", "-c",
                 f"cd ~/{repo_name} 2>/dev/null && git pull origin master 2>/dev/null || true"],
                host=target_host, capture_output=True, timeout=30)

        # Deploy agent-config to ~/.claude/ on target (skills, hooks, scripts)
        # This replaces the old symlink approach which broke macOS find.
        for subdir in ["skills", "hooks", "scripts"]:
            _remote_run(
                ["bash", "-c",
                 f"[ -d ~/agent-config/.claude/{subdir} ] && "
                 f"rsync -az --checksum ~/agent-config/.claude/{subdir}/ ~/.claude/{subdir}/"],
                host=target_host, capture_output=True, timeout=30)

        # Adapt settings.json paths for target $HOME
        r_home = _remote_run(["bash", "-c", "echo $HOME"], host=target_host,
                              capture_output=True, text=True, timeout=5)
        remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
        local_home = home
        if remote_home and remote_home != local_home:
            settings_src = os.path.expanduser("~/.claude/settings.json")
            if os.path.exists(settings_src):
                with open(settings_src) as f:
                    settings_text = f.read()
                settings_text = settings_text.replace(local_home, remote_home)
                fd, tmp = tempfile.mkstemp(suffix=".json")
                os.write(fd, settings_text.encode())
                os.close(fd)
                subprocess.run(
                    ["rsync", "-az", tmp, f"{target_host}:.claude/settings.json"],
                    capture_output=True, timeout=10)
                os.unlink(tmp)

    def _sync_worker_data_back(self, name, source_host):
        """Sync worker-scoped data back from remote after teleback.

        Only syncs:
        - ~/team/<worker>/ — worker's own team dir (kanban, playbook, etc.)
        - ~/.claude/projects/*/memory/ — worker's auto-memory
        Working directory and session transcript are already synced
        by _sync_working_directory and _sync_session_transcript.

        VPS is source of truth for team-scope config — workers don't
        override ~/team/playbook.md, ~/agent-config/, etc.
        """
        if not source_host:
            return

        home = os.path.expanduser("~")

        # 1. Sync worker's team dir (~/team/<name>/)
        worker_team_dir = os.path.join(home, "team", name)
        if os.path.isdir(worker_team_dir):
            subprocess.run(
                ["rsync", "-az",
                 f"{source_host}:team/{name}/",
                 f"{worker_team_dir}/"],
                capture_output=True, timeout=30)

        # 2. Sync auto-memory files back
        # Memory lives in ~/.claude/projects/<slug>/memory/
        r = _remote_run(
            ["bash", "-c",
             "find ~/.claude/projects/*/memory -name '*.md' 2>/dev/null | head -50"],
            host=source_host, capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            for remote_file in r.stdout.strip().splitlines():
                # Convert remote path to local: replace remote $HOME with local
                r_home = _remote_run(
                    ["bash", "-c", "echo $HOME"], host=source_host,
                    capture_output=True, text=True, timeout=5)
                remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
                if remote_home and remote_file.startswith(remote_home):
                    local_file = home + remote_file[len(remote_home):]
                    local_dir = os.path.dirname(local_file)
                    os.makedirs(local_dir, exist_ok=True)
                    subprocess.run(
                        ["rsync", "-az",
                         f"{source_host}:{remote_file}", local_file],
                        capture_output=True, timeout=10)

    def _run_teleport_preflight(self, target_host, worker_name, backend_name):
        """Run team-defined preflight check scripts against target.

        Scripts live in agent-config/teleport-preflight.d/*.sh (team-managed)
        and ~/.config/claudecode-telegram/teleport-preflight.d/*.sh (local).

        Each script receives env vars: TARGET_HOST, WORKER_NAME, BACKEND,
        BRIDGE_URL. Exit 0 = pass, exit 1 = fail (stdout = reason).
        """
        fails = []
        preflight_dirs = [
            os.path.expanduser("~/agent-config/teleport-preflight.d"),
            os.path.expanduser("~/.config/claudecode-telegram/teleport-preflight.d"),
        ]

        env = os.environ.copy()
        env["TARGET_HOST"] = target_host or ""
        env["WORKER_NAME"] = worker_name
        env["BACKEND"] = backend_name
        env["BRIDGE_URL"] = BRIDGE_PUBLIC_URL or BRIDGE_URL

        seen_scripts = set()  # Deduplicate symlinked scripts
        for pdir in preflight_dirs:
            if not os.path.isdir(pdir):
                continue
            scripts = sorted(
                f for f in os.listdir(pdir)
                if f.endswith(".sh") and os.access(os.path.join(pdir, f), os.X_OK))
            for script in scripts:
                script_path = os.path.join(pdir, script)
                real_path = os.path.realpath(script_path)
                if real_path in seen_scripts:
                    continue
                seen_scripts.add(real_path)
                try:
                    r = subprocess.run(
                        [script_path], env=env,
                        capture_output=True, text=True, timeout=10)
                    if r.returncode != 0:
                        reason = r.stdout.strip().split("\n")[0] if r.stdout.strip() else f"{script} failed"
                        fails.append(reason)
                except subprocess.TimeoutExpired:
                    fails.append(f"{script} timed out")
                except Exception as e:
                    fails.append(f"{script} error: {e}")

        return fails

    def _install_hooks_on_target(self, target_host):
        """Install Claude Code hooks and settings on target machine.

        With git-synced agent-config, this is a lightweight fallback
        for any files not covered by the repo (e.g., .claude.json).
        Hooks/skills/settings are synced via _sync_shared_repos.
        """
        if not target_host:
            return  # Local — hooks already installed

        # Ensure hooks dir has correct permissions
        _remote_run(["chmod", "-R", "700", ".claude/hooks"],
                     host=target_host, capture_output=True)

        # Sync .claude.json (onboarding, trust dialogs, project config)
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            subprocess.run(
                ["rsync", "-az", claude_json, f"{target_host}:.claude.json"],
                capture_output=True, timeout=10)
        else:
            # Create minimal .claude.json to skip first-time prompts
            _remote_run(
                ["python3", "-c",
                 'import json,os,pathlib;'
                 'p=pathlib.Path(os.path.expanduser("~/.claude.json"));'
                 'd=json.loads(p.read_text()) if p.exists() else {};'
                 'd["hasCompletedOnboarding"]=True;'
                 'd.setdefault("numStartups",1);'
                 'p.write_text(json.dumps(d))'],
                host=target_host, capture_output=True, timeout=10)

    def _sync_session_files_to_target(self, name, target_sessions_dir, target_host):
        """Copy session files (chat_id, session_id, cwd) to target machine.

        The Stop hook reads chat_id from SESSIONS_DIR/<worker>/chat_id to
        route responses to Telegram. Without these files, the hook exits
        silently and responses never reach Telegram.
        """
        local_session_dir = SESSIONS_DIR / name
        if not local_session_dir.is_dir():
            return
        remote_session_dir = f"{target_sessions_dir}/{name}"
        _remote_run(["mkdir", "-p", remote_session_dir],
                     host=target_host, capture_output=True)
        for fname in ["chat_id", "claude_session_id", "claude_session_cwd"]:
            local_file = local_session_dir / fname
            if local_file.exists():
                subprocess.run(
                    ["rsync", "-az", str(local_file),
                     f"{target_host}:{remote_session_dir}/{fname}"],
                    capture_output=True, timeout=10)

    def _sync_credentials_to_target(self, target_host):
        """Copy Claude credentials to target (always overwrite — tokens expire).

        ~/.claude/.credentials.json has the actual access/refresh tokens.
        Without it, Claude starts unauthenticated on the target machine.
        Always overwrite: target may have expired tokens from a previous teleport.
        """
        local_creds = os.path.expanduser("~/.claude/.credentials.json")
        if not os.path.exists(local_creds):
            return
        _remote_run(["mkdir", "-p", ".claude"],
                     host=target_host, capture_output=True)
        # Atomic: rsync to tmp, then mv (avoids truncated file on crash)
        subprocess.run(
            ["rsync", "-az", local_creds,
             f"{target_host}:.claude/.credentials.json.tmp"],
            capture_output=True, timeout=10)
        _remote_run(["mv", ".claude/.credentials.json.tmp",
                      ".claude/.credentials.json"],
                     host=target_host, capture_output=True)
        _remote_run(["chmod", "600", ".claude/.credentials.json"],
                     host=target_host, capture_output=True)

    def _start_worker_on_target(self, name, target_host, target_cwd,
                                 session_id, backend_name):
        """Create tmux session on target and start Claude Code with --resume."""
        tmux_name = f"{TMUX_PREFIX}{name}"

        # Clean up any leftover session
        _remote_run(["tmux", "kill-session", "-t", tmux_name],
                     host=target_host, capture_output=True)
        time.sleep(0.3)

        # Create new session
        r = _remote_run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            host=target_host, capture_output=True)
        if r.returncode != 0:
            return False

        time.sleep(0.5)

        # Remap SESSIONS_DIR for target $HOME (e.g. /home/claude → /Users/user)
        target_sessions_dir = str(SESSIONS_DIR)
        local_home = os.path.expanduser("~")
        if target_host:
            r_home = _remote_run(
                ["bash", "-c", "echo $HOME"], host=target_host,
                capture_output=True, text=True, timeout=5)
            remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
            if remote_home and remote_home != local_home and target_sessions_dir.startswith(local_home):
                target_sessions_dir = remote_home + target_sessions_dir[len(local_home):]

        # Sync session files (chat_id, session_id, cwd) to target
        if target_host:
            self._sync_session_files_to_target(name, target_sessions_dir, target_host)

        # Sync credentials if target lacks them
        if target_host:
            self._sync_credentials_to_target(target_host)

        # Export hook env vars (BRIDGE_URL points back to bridge)
        for key, value in {
            "PORT": str(PORT),
            "TMUX_PREFIX": TMUX_PREFIX,
            "SESSIONS_DIR": target_sessions_dir,  # Remapped for target $HOME
            "WORKER_BACKEND": normalize_backend(backend_name),
            "BRIDGE_URL": BRIDGE_PUBLIC_URL or BRIDGE_URL,
        }.items():
            _remote_run(["tmux", "set-environment", "-t", tmux_name, key, value],
                         host=target_host, capture_output=True)

        time.sleep(0.3)

        # Source env and unset CLAUDECODE
        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name,
             'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
            host=target_host, capture_output=True)
        time.sleep(0.3)

        # Build and send start command
        backend = get_backend(backend_name)
        cli_cmd = backend.start_cmd(session_id)

        # Claude Code refuses --dangerously-skip-permissions as root
        if target_host:
            r_id = _remote_run(["id", "-u"], host=target_host,
                               capture_output=True, text=True)
            if r_id.returncode == 0 and r_id.stdout.strip() == "0":
                cli_cmd = cli_cmd.replace(" --dangerously-skip-permissions", "")

        start_cmd = f'unset CLAUDECODE && {cli_cmd}'
        if target_cwd:
            start_cmd = f'cd {shlex.quote(target_cwd)} && {start_cmd}'

        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
            host=target_host, capture_output=True)

        if backend.is_interactive:
            # Claude may show first-time prompts (theme picker, permission
            # mode). Navigate them: Enter accepts defaults, "2" selects
            # auto-accept permission mode. Multiple Enter presses are safe.
            for delay, key in [(3.0, "Enter"), (2.0, "Enter"),
                               (1.0, "Enter"), (1.0, "Enter")]:
                time.sleep(delay)
                _remote_run(["tmux", "send-keys", "-t", tmux_name, key],
                             host=target_host, capture_output=True)

        # Verify Claude is running (retry up to 30s for startup)
        for _ in range(30):
            time.sleep(1)
            r = _remote_run(
                ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
                host=target_host, capture_output=True, text=True)
            if r.returncode != 0:
                continue
            pane_pid = r.stdout.strip()
            if pane_pid and _get_claude_pid(pane_pid, host=target_host):
                return True
        return False

    def _teleport_rollback(self, name, tmux_name, source_host, source_cwd,
                            session_id, backend_name, chat_id, reason):
        """Roll back a failed teleport by restarting on source."""
        self._teleport_notify(chat_id, f"Teleport failed: {reason}. Rolling back...")
        try:
            # Ensure tmux session exists on source
            if not tmux_exists(tmux_name, host=source_host):
                _remote_run(
                    ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
                    host=source_host, capture_output=True)

            # Restart Claude Code on source
            backend = get_backend(backend_name)
            start_cmd = f'unset CLAUDECODE && {backend.start_cmd(session_id)}'
            if source_cwd:
                start_cmd = f'cd {shlex.quote(source_cwd)} && {start_cmd}'
            _remote_run(
                ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
                host=source_host, capture_output=True)

            self._teleport_notify(chat_id, f"{name} restarted on source. Teleport cancelled.")
        except Exception as e:
            self._teleport_notify(chat_id, f"Rollback also failed: {e}")

        try:
            state_file = SESSIONS_DIR / name / "teleport_state"
            state_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _teleport_notify(self, chat_id, text):
        """Send progress notification during teleport."""
        try:
            telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
        except Exception:
            pass

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

    def _extract_reply_media(self, reply_to, target_worker):
        """Download media from a reply-to message. Returns media text or None."""
        # Check for media types in priority order
        animation = reply_to.get("animation")
        photo = reply_to.get("photo")
        document = reply_to.get("document")
        audio = reply_to.get("audio")
        voice = reply_to.get("voice")
        video = reply_to.get("video")
        sticker = reply_to.get("sticker")

        file_id = None
        media_label = "media"

        if animation:
            file_id = animation.get("file_id")
            media_label = "GIF"
        elif photo:
            largest = max(photo, key=lambda p: p.get("file_size", 0))
            file_id = largest.get("file_id")
            media_label = "image"
        elif video:
            file_id = video.get("file_id")
            media_label = "video"
        elif document:
            file_id = document.get("file_id")
            media_label = f"file: {document.get('file_name', 'unknown')}"
        elif audio:
            file_id = audio.get("file_id")
            media_label = "audio"
        elif voice:
            file_id = voice.get("file_id")
            media_label = "voice message"
        elif sticker:
            file_id = sticker.get("file_id")
            media_label = f"sticker: {sticker.get('emoji', '')}"

        if not file_id:
            return None

        local_path = download_telegram_file(file_id, target_worker)
        if not local_path:
            return None

        return f"Manager forwarded {media_label}: {local_path}"

    def _worker_from_reply(self, msg):
        """Extract worker name from a reply-to message's text prefix (e.g. 'bob:\\n...')."""
        reply_to = msg.get("reply_to_message") if msg else None
        if not reply_to:
            return None
        reply_text = reply_to.get("text") or reply_to.get("caption") or ""
        if not reply_text:
            return None
        # Worker messages are formatted as "name:\n..." — extract the name
        first_line = reply_text.split("\n", 1)[0]
        if first_line.endswith(":"):
            candidate = first_line[:-1].strip().lower()
            registered = self.workers.get_registered_sessions()
            if candidate in registered:
                return candidate
        return None

    def _route_media_message(self, media_text, caption, chat_id, msg_id, msg=None):
        """Route a media message, honoring @mentions in caption or reply-to context."""
        if caption:
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                for name in targets:
                    self.route_message(name, media_text, chat_id, msg_id, one_off=True)
                return
        # Check reply-to: if replying to a worker's message, route to that worker
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            self.route_message(reply_worker, media_text, chat_id, msg_id, one_off=True)
            return
        self.route_to_active(media_text, chat_id, msg_id)

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
            # Check if worker is being teleported before reporting offline
            teleport_state_file = SESSIONS_DIR / session_name / "teleport_state"
            if teleport_state_file.exists():
                self.reply(chat_id, f"{session_name.capitalize()} is being teleported. Please wait.")
                return
            self.reply(chat_id, f"{session_name.capitalize()} is offline. Try /restart.")
            return

        backend_name = get_worker_backend(session_name, session)
        backend = get_backend(backend_name)

        # Non-interactive backpressure: reject if already processing
        if not backend.is_interactive and is_pending(session_name):
            self.reply(chat_id, f"{session_name.capitalize()} is still working on the previous request. Wait for a response or use /pause.")
            return

        # Interactive prompt shortcut: if worker is at a selection prompt and
        # manager sends a single digit or "skip", translate to keystrokes
        shortcut = text.strip().lower()
        if backend.is_interactive and shortcut in (
            "1", "2", "3", "4", "5", "6", "7", "8", "9", "skip", "cancel"
        ):
            tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{session_name}")
            host = get_worker_host(session_name)
            _, _, raw_lines = _read_tmux_activity(tmux_name, host=host)
            if raw_lines:
                details = _extract_question_details(raw_lines)
                if details:
                    if _send_interactive_reply(tmux_name, shortcut, details, host=host):
                        action = f"Skipped" if shortcut in ("skip", "cancel") else f"Picked option {shortcut}"
                        self.reply(chat_id, f"{action}.")
                        return

        # Prefix manager messages so workers can distinguish from inter-worker messages.
        # Skip if text already has a "Manager sent ..." prefix (media messages).
        if not text.startswith("Manager sent "):
            text = f"manager: {text}"

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
            host = get_worker_host(session_name)
            if not backend.is_interactive or tmux_prompt_empty(session.get("tmux", ""), host=host):
                self.telegram.set_reaction(chat_id, msg_id, [{"type": "emoji", "emoji": "👀"}])


command_router = CommandRouter(telegram, worker_manager)

# ============================================================
# NON-CORE: HTTP Handler
# ============================================================

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict):
        """Send a JSON response with proper Content-Type."""
        body = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _send_unknown_endpoint(self, method: str, path: str):
        """Return 404 JSON for unrecognized endpoints with available alternatives."""
        self._send_json(404, {
            "error": f"Unknown endpoint: {method} {path}",
            "available_endpoints": API_ENDPOINTS,
            "hint": "Messages from manager arrive as prompts. There is no polling endpoint.",
        })

    def do_POST(self):
        # Route based on path
        if self.path == "/response":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_hook_response(body)
            return

        if self.path == "/notify":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_notify(body)
            return

        # Only accept Telegram webhook on root path — 404 for unknown POST paths
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self._send_unknown_endpoint("POST", parsed.path)
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

    def handle_notify(self, body: bytes = b""):
        """Handle system notification request (internal, HMAC-authenticated).

        SECURITY: This endpoint allows the shell script to trigger
        notifications without having access to the bot token.
        Used for tunnel watchdog alerts.
        """
        try:
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

    def handle_hook_response(self, body: bytes = b""):
        """Handle response forwarded from Claude hook.

        SECURITY: This is how Claude responses get to Telegram without
        Claude ever having access to the bot token. Hook POSTs here,
        bridge sends to Telegram. HMAC-authenticated.

        FILE SUPPORT: Parses [[image:/path|caption]] (photos, animations) and [[file:/path|caption]] (documents, video, audio, voice, stickers) tags.
        """
        try:
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

        # API index (also serves as health check — returns 200)
        if parsed.path == "/":
            self._send_json(200, {
                "name": "claudecode-telegram bridge",
                "endpoints": API_ENDPOINTS,
                "note": "Messages from manager arrive as prompts. There is no polling endpoint.",
            })
            return

        # Unknown GET endpoint
        self._send_unknown_endpoint("GET", parsed.path)

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

        GET /checkin                    — generic instructions (uses default backend)
        GET /checkin?name=lee           — personalized instructions for worker 'lee'
        GET /checkin?name=lee&cwd=/dir  — set startup cwd (RAM); restart worker if cwd changed
        """
        try:
            params = parse_qs(parsed.query)
            name = params.get("name", ["worker"])[0]
            raw_cwd = params.get("cwd", [None])[0]
            requested_cwd = ""
            if raw_cwd is not None:
                requested_cwd, cwd_err = validate_cwd(raw_cwd)
                if cwd_err:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(f"Invalid cwd: {cwd_err}".encode())
                    return

            # If worker exists, use their actual backend; otherwise default
            _sync_worker_manager()
            registered = worker_manager.get_registered_sessions()
            tmux_name = ""
            if name in registered:
                backend_name = get_worker_backend(name, registered[name])
                # Re-export hook env on checkin (refreshes BRIDGE_URL after restart)
                tmux_name = registered[name].get("tmux", f"{TMUX_PREFIX}{name}")
                host = get_worker_host(name)
                if tmux_exists(tmux_name, host=host):
                    export_hook_env(tmux_name, backend_name, host=host)
            else:
                backend_name = DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)

            if requested_cwd:
                _set_worker_cwd(name, requested_cwd)
                save_claude_session_cwd(name, requested_cwd)
                if tmux_name and tmux_exists(tmux_name, host=host):
                    pane_cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
                    same_cwd = pane_cwd and os.path.realpath(pane_cwd) == os.path.realpath(requested_cwd)
                    if not same_cwd:
                        notify_chat_id = get_manager_chat_id(name)
                        if notify_chat_id is not None:
                            send_telegram_message(
                                notify_chat_id,
                                f"{name} is restarting in a new directory. "
                                "Messages during restart may be lost.",
                            )

                        ok, err = worker_manager.restart(name, mode="relaunch")
                        if not ok:
                            if notify_chat_id is not None:
                                send_telegram_message(
                                    notify_chat_id,
                                    f"{name} could not restart. "
                                    f"Run /restart {name} before sending new messages.",
                                )
                            self.send_response(500)
                            self.send_header("Content-Type", "text/plain")
                            self.end_headers()
                            self.wfile.write(f"Failed to restart in {requested_cwd}: {err}".encode())
                            return

                        if notify_chat_id is not None:
                            if _wait_for_restart_ready(tmux_name, backend_name, host=host):
                                send_telegram_message(
                                    notify_chat_id,
                                    f"{name} is ready. Safe to send messages now.",
                                )
                            else:
                                send_telegram_message(
                                    notify_chat_id,
                                    f"{name} restarted but is not ready yet. "
                                    f"Hold messages for now. If this continues, run /restart {name}.",
                                )

                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain")
                        self.end_headers()
                        self.wfile.write(f"Restarting in {requested_cwd}...".encode())
                        return

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
            # SAFETY: only touch sessions that match OUR prefix to avoid
            # overwriting env vars of workers belonging to other nodes
            tmux_name = info.get("tmux", f"{TMUX_PREFIX}{name}")
            if not tmux_name.startswith(TMUX_PREFIX):
                print(f"  SKIP {name}: tmux '{tmux_name}' doesn't match prefix '{TMUX_PREFIX}'")
                continue
            backend_name = get_worker_backend(name, info)
            backend_obj = get_backend(backend_name)
            if not backend_obj.is_interactive:
                ensure_worker_pipe(name)
            # Re-export hook env so workers get the current BRIDGE_URL
            host = get_worker_host(name)
            if tmux_exists(tmux_name, host=host):
                export_hook_env(tmux_name, backend_name, host=host)

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
    print(f"Multi-Session Bridge on {BRIDGE_BIND}:{PORT}")
    print(f"Hook endpoint: http://localhost:{PORT}/response")
    print(f"Active: {state['active'] or 'none'}")
    print(f"Sessions: {list(registered.keys()) or 'none'}")
    if WEBHOOK_SECRET:
        print("Webhook verification: enabled")
    else:
        print("Webhook verification: disabled (set TELEGRAM_WEBHOOK_SECRET to enable)")
    print(f"Hook endpoint auth: disabled (localhost-only)")
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
        ReuseAddrServer((BRIDGE_BIND, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
