#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.31.0"

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

try:
    from bridge_grpc import BridgeGRPCServer
    BRIDGE_GRPC_IMPORT_ERROR = None
except ImportError as e:
    BridgeGRPCServer = None
    BRIDGE_GRPC_IMPORT_ERROR = e

try:
    from gmail_connector import GmailConnector
    GMAIL_IMPORT_ERROR = None
except ImportError as e:
    GmailConnector = None
    GMAIL_IMPORT_ERROR = e

try:
    from github_connector import GitHubConnector
    GITHUB_IMPORT_ERROR = None
except ImportError as e:
    GitHubConnector = None
    GITHUB_IMPORT_ERROR = e


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

GRPC_PORT = int(os.environ.get("BRIDGE_GRPC_PORT", str(PORT + 1)))
grpc_server = None  # initialized in main()
gmail_connector_instance = None  # initialized in main()
github_connector_instance = None  # initialized in main()

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
# BRIDGE_SSH_TARGET: ssh alias that remote machines use to reach the bridge host.
# Used by /workers?from= when a remote caller needs to address a bridge-local peer.
# Default "vps" matches team convention; override per deployment if needed.
BRIDGE_SSH_TARGET = os.environ.get("BRIDGE_SSH_TARGET", "vps")
PERSISTENCE_NOTE = "They'll stay on your team."

# Voice mode: STT (speech-to-text) and TTS (text-to-speech) endpoints
# STT: transcribe incoming voice messages so workers can read them
# TTS: generate voice from worker text responses (explicit [[speak]] tag)
STT_ENDPOINT = os.environ.get("STT_ENDPOINT", "http://100.126.187.125:10110/transcribe")
TTS_ENDPOINT = os.environ.get("TTS_ENDPOINT", "http://100.126.187.125:10111/synthesize")
TTS_VOICE = os.environ.get("TTS_VOICE", "Serena")
STT_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "10"))  # seconds, fail-open
TTS_TIMEOUT = int(os.environ.get("TTS_TIMEOUT", "60"))  # seconds, TTS runs in background thread
TTS_CHUNKED_THRESHOLD = 200  # chars: above this, use /synthesize/chunked endpoint

# API endpoint registry — used by index, 404 handler, and worker instructions.
# Update this when adding new endpoints.
API_ENDPOINTS = {
    "GET /": "API index — lists all endpoints",
    "GET /workers": "List active workers with send commands",
    "GET /checkin?name=<name>": "Refresh worker instructions (optional: &cwd=/path)",
    "GET /health/workers": "Watchdog state for all workers",
    "GET /transcript/<name>": "Polished HTML transcript viewer for a worker",
    "GET /team-chat": "Team Telegram chat viewer (requires rewind token)",
    "GET /pr-review/<pr_num>": "PR review viewer with diff, search, file navigation",
    "POST /send": "Send a prompt to a worker: {worker, message, from (default: system)}",
    "POST /response": "Hook: send Claude response to Telegram",
    "POST /notify": "Send notification to all admin chats",
    "POST /health-alert": "Hook: JSONL health alert (stale transcript detection)",
    "POST /register": "Forge/callback worker registration (name, host, version, tools, callback_url)",
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

# Gmail connector: poll Gmail for manager emails with @worker mentions
GMAIL_ENABLED = os.environ.get("GMAIL_ENABLED", "0") == "1"
GMAIL_POLL_INTERVAL = int(os.environ.get("GMAIL_POLL_INTERVAL", "45"))
GMAIL_FROM_FILTER = os.environ.get("GMAIL_FROM_FILTER", "ngocthinhdp@gmail.com")
GMAIL_GWS_BIN = os.environ.get("GMAIL_GWS_BIN", os.path.expanduser("~/bin/gws"))
if GMAIL_ENABLED and not GMAIL_FROM_FILTER.strip():
    raise RuntimeError("GMAIL_FROM_FILTER must be set when GMAIL_ENABLED=1 (security: sender filter required)")

GITHUB_ENABLED = os.environ.get("BRIDGE_GHPOLL_ENABLED", "0") == "1"
GITHUB_POLL_INTERVAL = int(os.environ.get("BRIDGE_GHPOLL_INTERVAL", "60"))
GITHUB_REPO = os.environ.get("BRIDGE_GHPOLL_REPO", "BasedHardware/omi")
GITHUB_FROM_USER = os.environ.get("BRIDGE_GHPOLL_USER", "beastoin")
if GITHUB_ENABLED and not GITHUB_FROM_USER.strip():
    raise RuntimeError("BRIDGE_GHPOLL_USER must be set when BRIDGE_GHPOLL_ENABLED=1 (security: sender filter required)")

# Team directory: shared knowledge base (soul docs, kanban, playbook, etc.)
TEAM_DIR = os.path.expanduser(os.environ.get("TEAM_DIR", "~/team"))
# Checkin note: read from TEAM_DIR/checkin-note.txt on each checkin/hire/restart.
# Supports {name} placeholder for per-worker substitution.
_CHECKIN_NOTE_PATH = os.path.join(TEAM_DIR, "checkin-note.txt")
# Learning reminder: read from TEAM_DIR/learning-reminder.txt on each fire.
_LEARNING_REMINDER_PATH = os.path.join(TEAM_DIR, "learning-reminder.txt")
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

def build_claude_start_cmd(resume_id: str = "") -> str:
    cmd = ["claude"]
    if resume_id:
        cmd.extend(["--resume", resume_id])
    cmd.append("--dangerously-skip-permissions")
    return " ".join(shlex.quote(part) for part in cmd)


class Backend(Protocol):
    """Minimal backend interface. 3 methods, no more."""
    name: str
    binary: str  # CLI binary name (e.g. "claude", "codex")
    is_interactive: bool

    def start_cmd(self, resume_id: str = "") -> str:
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

# ============================================================
# GIT-BASED TELEPORT SYNC
# ============================================================
# VPS hosts bare repos at ~/git-server/<project>.git.
# Workers push WIP state (via git stash create) to per-worker branches,
# target fetches deltas. ~0-50s vs 600s+ for rsync over Tailscale.

GIT_SERVER_DIR = os.path.expanduser("~/git-server")


def _bare_repo_url(bare_repo_path: str, target_host: str = None) -> str:
    """Return the URL to access the bare repo from target_host.

    Local targets get the direct path. Remote targets get an SSH URL to VPS.
    """
    if target_host:
        return f"claude@100.125.36.102:{bare_repo_path}"
    return bare_repo_path


def _ensure_bare_repo(project_name: str) -> str:
    """Create bare repo at GIT_SERVER_DIR/<project>.git if missing. Returns path."""
    bare_path = os.path.join(GIT_SERVER_DIR, f"{project_name}.git")
    if not os.path.isdir(bare_path):
        os.makedirs(GIT_SERVER_DIR, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", bare_path],
            capture_output=True, text=True, check=True)
    return bare_path


def _git_push_state(source_cwd: str, worker_name: str, bare_repo: str,
                    host: str = None) -> Optional[dict]:
    """Push working state to bare repo without mutating source.

    Approach: temporarily `git add -A` to capture untracked files in the index,
    run `git stash create` (non-mutating — creates commit without moving HEAD),
    then `git reset` to restore original index. Working tree is never modified.

    Returns metadata dict {orig_sha, orig_branch, staged_files, stash_sha}
    or None on failure.
    """
    try:
        # Get current HEAD
        r = _remote_run(["git", "-C", source_cwd, "rev-parse", "HEAD"],
                        host=host, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f"[git-sync] rev-parse HEAD failed: {r.stderr[:200]}")
            return None
        orig_sha = r.stdout.strip()

        # Get current branch name (or "HEAD" if detached)
        r = _remote_run(["git", "-C", source_cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                        host=host, capture_output=True, text=True, timeout=10)
        orig_branch = r.stdout.strip() if r.returncode == 0 else "HEAD"

        # Get originally staged files (before we touch the index)
        r = _remote_run(["git", "-C", source_cwd, "diff", "--cached", "--name-only"],
                        host=host, capture_output=True, text=True, timeout=15)
        staged_files = [f for f in r.stdout.strip().split("\n") if f] if r.returncode == 0 else []

        # Stage everything (including untracked) temporarily to capture in stash
        _remote_run(["git", "-C", source_cwd, "add", "-A"],
                    host=host, capture_output=True, text=True, timeout=30)

        # Create stash commit (non-mutating — working tree untouched)
        r = _remote_run(["git", "-C", source_cwd, "stash", "create"],
                        host=host, capture_output=True, text=True, timeout=30)
        stash_sha = r.stdout.strip() if r.returncode == 0 else ""

        # Restore original index: reset, then re-stage originally staged files
        _remote_run(["git", "-C", source_cwd, "reset", "HEAD"],
                    host=host, capture_output=True, text=True, timeout=15)
        if staged_files:
            _remote_run(["git", "-C", source_cwd, "add", "--"] + staged_files,
                        host=host, capture_output=True, text=True, timeout=15)

        # Determine what to push: stash commit if dirty, HEAD if clean
        push_sha = stash_sha if stash_sha else orig_sha
        ref = f"refs/heads/teleport/{worker_name}"

        # Push to bare repo
        if host:
            # Remote source → push to VPS bare repo via SSH
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force",
                 f"claude@100.125.36.102:{bare_repo}", f"{push_sha}:{ref}"],
                host=host, capture_output=True, text=True, timeout=60)
        else:
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force",
                 bare_repo, f"{push_sha}:{ref}"],
                host=host, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"[git-sync] push failed: {r.stderr[:200]}")
            return None

        return {
            "orig_sha": orig_sha,
            "orig_branch": orig_branch,
            "staged_files": staged_files,
            "stash_sha": stash_sha or None,
        }
    except Exception as e:
        print(f"[git-sync] push state error: {e}")
        return None


def _git_pull_state(target_cwd: str, worker_name: str, bare_repo_url: str,
                    metadata: dict, host: str = None) -> bool:
    """Pull and apply working state on target. Returns success.

    For fresh targets: clones from bare repo.
    For existing targets: fetches and applies.
    Restores branch, working tree changes, and staged files.
    """
    try:
        orig_sha = metadata["orig_sha"]
        orig_branch = metadata["orig_branch"]
        staged_files = metadata.get("staged_files", [])
        stash_sha = metadata.get("stash_sha")
        ref = f"teleport/{worker_name}"

        is_existing = False
        try:
            r = _remote_run(["git", "-C", target_cwd, "rev-parse", "--git-dir"],
                            host=host, capture_output=True, text=True, timeout=10)
            is_existing = r.returncode == 0
        except Exception:
            pass

        if not is_existing:
            # Fresh clone from bare repo
            r = _remote_run(
                ["git", "clone", "--no-checkout", bare_repo_url, target_cwd],
                host=host, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"[git-sync] clone failed: {r.stderr[:200]}")
                return False
            # Configure user for the clone
            _remote_run(["git", "-C", target_cwd, "config", "user.email", "teleport@bridge"],
                        host=host, capture_output=True)
            _remote_run(["git", "-C", target_cwd, "config", "user.name", "teleport"],
                        host=host, capture_output=True)
        else:
            # Add/update remote pointing to bare repo
            _remote_run(["git", "-C", target_cwd, "remote", "remove", "vps"],
                        host=host, capture_output=True)
            _remote_run(
                ["git", "-C", target_cwd, "remote", "add", "vps", bare_repo_url],
                host=host, capture_output=True, text=True, timeout=10)
            # Fetch the teleport branch
            r = _remote_run(
                ["git", "-C", target_cwd, "fetch", "vps", ref],
                host=host, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"[git-sync] fetch failed: {r.stderr[:200]}")
                return False

        # Checkout the original branch at the original commit
        if orig_branch and orig_branch != "HEAD":
            _remote_run(
                ["git", "-C", target_cwd, "checkout", "-B", orig_branch, orig_sha],
                host=host, capture_output=True, text=True, timeout=30)
        else:
            _remote_run(
                ["git", "-C", target_cwd, "checkout", orig_sha],
                host=host, capture_output=True, text=True, timeout=30)

        # Apply the stash if there were uncommitted changes
        if stash_sha:
            # Fetch the stash commit (it's on the teleport branch)
            # For fresh clones, it's already available. For existing, we fetched it.
            # Use FETCH_HEAD or the ref directly
            fetch_ref = f"vps/{ref}" if is_existing else f"origin/{ref}"

            # Apply stash: the teleport branch tip IS the stash commit
            r = _remote_run(
                ["git", "-C", target_cwd, "stash", "apply", fetch_ref],
                host=host, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                # Fallback: try direct SHA if ref doesn't resolve
                # The stash SHA was pushed as the branch tip
                _remote_run(
                    ["git", "-C", target_cwd, "read-tree", "-u", "--reset", orig_sha],
                    host=host, capture_output=True, text=True, timeout=15)
                r = _remote_run(
                    ["git", "-C", target_cwd, "cherry-pick", "--no-commit", fetch_ref],
                    host=host, capture_output=True, text=True, timeout=30)

            # Re-stage originally staged files
            if staged_files:
                # First reset index to HEAD (stash apply may have staged everything)
                _remote_run(["git", "-C", target_cwd, "reset", "HEAD"],
                            host=host, capture_output=True, text=True, timeout=15)
                _remote_run(["git", "-C", target_cwd, "add", "--"] + staged_files,
                            host=host, capture_output=True, text=True, timeout=15)

        return True
    except Exception as e:
        print(f"[git-sync] pull state error: {e}")
        return False


def _is_git_repo(cwd: str, host: str = None) -> bool:
    """Check if cwd is inside a git repository."""
    try:
        r = _remote_run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            host=host, capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _get_project_name(cwd: str, host: str = None) -> Optional[str]:
    """Derive project name from git remote.origin.url.

    Returns short name (e.g., 'omi' from 'https://github.com/BasedHardware/omi.git')
    or None if no origin remote.
    """
    try:
        r = _remote_run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            host=host, capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        url = r.stdout.strip()
        # Strip trailing .git
        if url.endswith(".git"):
            url = url[:-4]
        # Handle SSH (git@host:org/repo) and HTTPS (https://host/org/repo)
        if ":" in url and not url.startswith("http"):
            # SSH format: git@github.com:Org/repo
            name = url.rsplit("/", 1)[-1] if "/" in url.split(":")[-1] else url.split(":")[-1]
        else:
            # HTTPS format
            name = url.rsplit("/", 1)[-1]
        return name if name else None
    except Exception:
        return None


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


def tmux_send_message(tmux_name: str, text: str, host: str = None, literal: bool = False) -> bool:
    """Send text + Enter to tmux session via paste-buffer (reliable for long messages).

    Uses tmux load-buffer/paste-buffer instead of send-keys -l to avoid
    character-by-character terminal injection which causes input batching
    on long messages or rapid sends.

    When literal=True, uses send-keys -l instead of paste-buffer.
    This is needed for TUI dialogs (e.g. Claude's OAuth "Paste code here")
    that don't support bracketed paste mode.

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
            if literal:
                r = _remote_run(
                    ["tmux", "send-keys", "-t", tmux_name, "-l", text],
                    host=host, capture_output=True,
                )
                if r.returncode != 0:
                    return False
                time.sleep(0.5)
                r = _remote_run(["tmux", "send-keys", "-t", tmux_name, "Enter"], host=host)
                return r.returncode == 0

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

    def start_cmd(self, resume_id: str = "") -> str:
        return build_claude_start_cmd(resume_id)

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        host = get_worker_host(worker_name)
        if not tmux_exists(tmux_name, host=host):
            return False
        # Claude's OAuth login dialog doesn't support bracketed paste.
        # Detect login state and use literal send-keys instead.
        literal = False
        try:
            r = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host, capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and "Paste code here" in r.stdout:
                literal = True
        except Exception:
            pass
        return tmux_send_message(tmux_name, text, host=host, literal=literal)

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

    def start_cmd(self, resume_id: str = "") -> str:
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

    def start_cmd(self, resume_id: str = "") -> str:
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

    def start_cmd(self, resume_id: str = "") -> str:
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
    # Teleported workers can't run adapters locally
    host = get_worker_host(worker_name)
    if host:
        print(f"Cannot spawn adapter for teleported worker '{worker_name}' on {host} (not supported yet)")
        return False
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
    "tts_enabled": False,  # Auto-TTS for worker responses (toggle with /voice)
}

# Consecutive @mention tracking (auto-focus after 2 in a row to same worker)
_last_mention = {"target": None, "count": 0}

# Watchdog state
_worker_states = {}  # name -> (state, reason, since)
_last_child_ts = {}
_last_seen_claude = {}
_last_hook_ts = {}
_last_alert_ts = {}
_alert_msg_ids = {}  # name -> message_id of last bad-state alert (for edit on recovery)
_idle_streak = {}
_prev_worker_states = {}
_consecutive_probe_failures = {}
_consecutive_good_probes = {}  # name -> int (consecutive good states after bad)
_consecutive_bad_probes = {}  # name -> int (consecutive bad states for remote workers)
_idle_child_baseline = {}  # name -> int (MCP server child count at idle)
_prev_children = {}  # name -> int (previous active children count, for activity detection)
_last_activity_ts = {}  # name -> float (last time children count changed)
_worker_cwds = {}  # name -> cwd (RAM-only startup cwd hints)
_recent_restarts = {}  # name -> timestamp (suppress watchdog resolved alert after restart)
RESTART_COOLDOWN = 60  # seconds: reject checkin-triggered restarts within this window
_restart_in_progress = {}  # name -> timestamp: set BEFORE restart, cleared after completion
_restart_lock = threading.Lock()  # protects _restart_in_progress
_force_restart_pending_cwd = {}  # name -> True: force restart completed, allow one post-restart CWD fix
_waiting_input_details = {}  # name -> dict (question details for WAITING_INPUT alert)
_watchdog_lock = threading.Lock()

# Host-level health tracking: detect when machines (Mac Mini, etc.) go offline
HOST_DOWN_THRESHOLD = 3  # consecutive SSH failures before declaring host DOWN
_host_ssh_failures = {}  # host -> int (consecutive SSH probe failures)
_host_down = {}  # host -> bool (True = host is DOWN)
_host_down_since = {}  # host -> float (timestamp when host went DOWN)
_host_last_error = {}  # host -> str (last SSH error message)

# Disk space monitoring: alert when machines run low on disk
DISK_ALERT_THRESHOLD_PCT = 90  # alert when usage exceeds this percentage
DISK_ALERT_THRESHOLD_GB = 10  # alert when free space drops below this (GB)
DISK_ALERT_COOLDOWN = 3600  # seconds between repeated disk alerts per host
_host_disk_usage = {}  # host -> {pct: int, free_gb: float, total_gb: float, ts: float}
_host_disk_alert_ts = {}  # host -> float (last disk alert timestamp)
_host_disk_alerted = {}  # host -> bool (currently in alert state, for recovery detection)

# Learning reminders: periodic self-learning nudges per worker
# Two triggers: response count threshold, and idle timeout (checked by timer).
# Anti-annoyance: after any reminder fires, all triggers suppressed until worker responds.
# State is persisted to disk so bridge restarts don't reset progress.
LEARNING_REMINDER_RESPONSE_THRESHOLD = 15  # fire after N worker responses
LEARNING_REMINDER_IDLE_HOURS = 6  # fire if no worker response in N hours
_learning_reminder_state = {}  # name -> {response_count, last_reminder_ts, last_response_ts, reminder_pending}
_learning_reminder_lock = threading.Lock()
_idle_scan_timer = None

_LEARNING_REMINDER_TEXT = (
    "system: Self-Learning Protocol reminder — time to check your learnings.\n\n"
    "You own your learning. Do not wait for approval to update your playbook.\n\n"
    "**What to capture:**\n"
    "Decisions that surprised you, corrections from manager or teammates, "
    "patterns you will use again, mistakes you will not repeat, "
    "tool/API behaviors that were not obvious.\n\n"
    "**What NOT to capture:**\n"
    "Routine task notes, things already in the code or git history, "
    "one-off fixes with no reuse value, debugging steps that only apply to a specific bug.\n\n"
    "**Format:**\n"
    'Write every rule as: "When X, do Y, because Z." '
    'The "because Z" is the most important part — without it the rule has no context '
    "and cannot be judged in edge cases.\n\n"
    "**Cap:**\n"
    "Maximum 20 active rules. When you hit 20, replace your weakest rule. "
    "A tight playbook of battle-tested rules beats a long list nobody reads.\n\n"
    "**Where to write:**\n"
    "~/team/{name}/playbook.md for rules specific to your role/tools/project.\n"
    "~/team/learnings.md for lessons that help other workers (cross-team value). "
    "Include date, description, tags, and your name.\n"
    "Do NOT duplicate between personal playbook and shared learnings — pick one home.\n\n"
    "**Steps:**\n"
    "1. Reflect — scan your recent work. Did you hit a surprise, get corrected, "
    "or discover a reusable pattern?\n"
    "2. If yes — read your ~/team/{name}/playbook.md, check if the lesson already exists. "
    "Update an existing rule or add a new one.\n"
    "3. If cross-team value — add a one-liner to ~/team/learnings.md.\n"
    "4. If nothing worth keeping — carry on. Not every session produces a learning.\n"
    "5. Clean — if over 20 rules, archive your weakest one.\n\n"
    "**Quality check:**\n"
    'Good: "When backend returns 500 on auth-token, check if Firebase emulator is running first, '
    'because the error message says connection refused which misleads you into checking network config."\n'
    'Bad: "Fixed auth-token bug." (no When/because, no reuse value, will rot)'
)


def _read_learning_reminder(name: str) -> str:
    """Read learning reminder from file, substitute {name}. Falls back to hardcoded constant."""
    try:
        if os.path.isfile(_LEARNING_REMINDER_PATH):
            text = open(_LEARNING_REMINDER_PATH).read().strip()
            if text:
                return text.replace("{name}", name)
    except Exception as e:
        print(f"Failed to read learning reminder from {_LEARNING_REMINDER_PATH}: {e}")
    return _LEARNING_REMINDER_TEXT.replace("{name}", name)


def _new_reminder_state():
    now = time.time()
    return {
        "response_count": 0,
        "last_reminder_ts": now,
        "last_response_ts": now,
        "reminder_pending": False,
    }


def _learning_reminder_state_file():
    """Path to persistent state file (NODE_DIR/learning_reminders.json)."""
    try:
        return os.path.join(str(NODE_DIR), "learning_reminders.json")
    except NameError:
        return None


def _save_learning_reminder_state():
    """Persist state to disk. Caller should hold _learning_reminder_lock."""
    path = _learning_reminder_state_file()
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_learning_reminder_state, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"Learning reminder state save error: {e}")


def _load_learning_reminder_state():
    """Load persisted state from disk into _learning_reminder_state."""
    path = _learning_reminder_state_file()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _learning_reminder_lock:
                for name, st in data.items():
                    if isinstance(st, dict) and "response_count" in st:
                        _learning_reminder_state[name] = st
            print(f"Learning reminder state loaded: {len(data)} workers")
    except Exception as e:
        print(f"Learning reminder state load error: {e}")


def _reset_learning_reminder(name: str):
    """Reset learning reminder state for a worker (on hire/restart/SessionStart)."""
    with _learning_reminder_lock:
        _learning_reminder_state[name] = _new_reminder_state()
        _save_learning_reminder_state()


def _fire_reminder(name: str, st: dict):
    """Mark state as fired and send reminder in background. Caller holds _learning_reminder_lock."""
    st["response_count"] = 0
    st["last_reminder_ts"] = time.time()
    st["reminder_pending"] = True
    _save_learning_reminder_state()
    reminder = _read_learning_reminder(name)
    threading.Thread(
        target=_send_learning_reminder,
        args=(name, reminder),
        daemon=True,
    ).start()


def _check_learning_reminder(name: str):
    """Increment response count and fire learning reminder if threshold met."""
    with _learning_reminder_lock:
        st = _learning_reminder_state.get(name)
        if st is None:
            st = _new_reminder_state()
            _learning_reminder_state[name] = st

        st["last_response_ts"] = time.time()

        if st.get("reminder_pending"):
            st["reminder_pending"] = False
            st["response_count"] = 1
            _save_learning_reminder_state()
            return

        st["response_count"] = st.get("response_count", 0) + 1

        if st["response_count"] >= LEARNING_REMINDER_RESPONSE_THRESHOLD:
            _fire_reminder(name, st)
        else:
            _save_learning_reminder_state()


def _scan_idle_workers():
    """Check all tracked workers for idle timeout. Called periodically by timer."""
    try:
        now = time.time()
        idle_threshold = LEARNING_REMINDER_IDLE_HOURS * 3600
        to_fire = []

        with _learning_reminder_lock:
            for name, st in _learning_reminder_state.items():
                if st.get("reminder_pending"):
                    continue
                if st.get("response_count", 0) <= 1:
                    continue
                idle_seconds = now - st.get("last_response_ts", now)
                since_reminder = now - st.get("last_reminder_ts", now)
                if idle_seconds >= idle_threshold and since_reminder >= idle_threshold:
                    to_fire.append(name)

            for name in to_fire:
                _fire_reminder(name, _learning_reminder_state[name])
    except Exception as e:
        print(f"Learning reminder idle scan error: {e}")
    finally:
        _schedule_idle_scan()


def _seed_learning_reminder_state(worker_names):
    """Initialize state for workers not already tracked (from disk or previous session)."""
    with _learning_reminder_lock:
        changed = False
        for name in worker_names:
            if name not in _learning_reminder_state:
                _learning_reminder_state[name] = _new_reminder_state()
                changed = True
        if changed:
            _save_learning_reminder_state()


def _schedule_idle_scan():
    """Schedule next idle scan (every 30 minutes)."""
    global _idle_scan_timer
    _idle_scan_timer = threading.Timer(1800, _scan_idle_workers)
    _idle_scan_timer.daemon = True
    _idle_scan_timer.start()


def _send_learning_reminder(name: str, text: str):
    """Send learning reminder to worker (runs in background thread)."""
    try:
        time.sleep(2)  # brief delay so it doesn't collide with the response
        if send_to_worker(name, text):
            print(f"Learning reminder sent to {name}")
        else:
            print(f"Learning reminder: failed to send to {name}")
    except Exception as e:
        print(f"Learning reminder error for {name}: {e}")


# Security: Pre-set admin or auto-learn first user (RAM only, re-learns on restart)
ADMIN_CHAT_ID_ENV = os.environ.get("ADMIN_CHAT_ID", "")
admin_chat_id = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV else None

# Persistence files (in node directory, survives restart)
NODE_DIR = SESSIONS_DIR.parent  # ~/.claude/telegram/nodes/<node>
LAST_CHAT_ID_FILE = NODE_DIR / "last_chat_id"
LAST_ACTIVE_FILE = NODE_DIR / "last_active"

# Claude Code stores transcripts at ~/.claude/projects/<slug>/<uuid>.jsonl.
# Overridable in tests.
CLAUDE_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))

# Rewind tokens: {token_str: {"name": worker, "expires_at": timestamp}}
REWIND_TOKENS = {}
PR_REVIEW_TOKENS = {}
REWIND_TIMEOUT = 5 * 60  # 5 minutes

BOT_COMMANDS = [
    # Daily commands (frequency-first, natural workflow order)
    {"command": "team", "description": "Show your team"},
    {"command": "focus", "description": "Focus a worker: /focus <name>"},
    {"command": "progress", "description": "Check focused worker status"},
    {"command": "pause", "description": "Pause focused worker"},
    {"command": "restart", "description": "Restart worker (--clean for fresh)"},
    # Occasional
    {"command": "voice", "description": "Toggle voice replies: /voice on|off"},
    {"command": "settings", "description": "Show settings"},
    {"command": "pilot", "description": "Toggle pilot access: /pilot <name>"},
    {"command": "rewind", "description": "Transcript viewer: /rewind <name>"},
    {"command": "pr", "description": "PR review viewer: /pr <github_pr_url>"},
    {"command": "memory", "description": "Search team chat memory: /memory <query>"},
    # Rare (onboarding/offboarding)
    {"command": "hire", "description": "Hire a worker: /hire <name>"},
    {"command": "end", "description": "Offboard a worker: /end <name>"},
]

BLOCKED_COMMANDS = [
    "/mcp", "/help", "/config", "/model", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/permissions",
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


def _registry_add_callback(name: str, callback_url: str, host: str = "", version: str = "", tools: dict = None):
    """Add an HTTP callback worker to the persistent registry."""
    with _watchdog_lock:
        data = _load_registry()
        if "workers" not in data:
            data = {"version": 1, "workers": {}}
        entry = {
            "backend": DEFAULT_BACKEND,
            "protocol": "http",
            "callback_url": callback_url.rstrip("/"),
            "chat_id": None,
            "hire_time": int(time.time()),
        }
        if host:
            entry["host"] = host
        if version:
            entry["version"] = version
        if isinstance(tools, dict):
            entry["tools"] = tools
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
# MESSAGE TRANSPORT ABSTRACTION
# ============================================================

TRANSPORT_MODE = os.environ.get("TRANSPORT", "telegram")


class MessageTransport:
    """Interface for all outbound messaging from bridge to manager."""

    @property
    def name(self) -> str:
        raise NotImplementedError

    def send_text(self, chat_id, text, parse_mode=None, reply_to=None) -> dict | None:
        raise NotImplementedError

    def send_photo(self, chat_id, photo_path, caption=None) -> bool:
        raise NotImplementedError

    def send_document(self, chat_id, doc_path, caption=None) -> bool:
        raise NotImplementedError

    def send_animation(self, chat_id, animation_path, caption=None) -> bool:
        raise NotImplementedError

    def send_video(self, chat_id, video_path, caption=None) -> bool:
        raise NotImplementedError

    def send_audio(self, chat_id, audio_path, caption=None) -> bool:
        raise NotImplementedError

    def send_voice(self, chat_id, voice_path, caption=None) -> bool:
        raise NotImplementedError

    def send_sticker(self, chat_id, sticker_path) -> bool:
        raise NotImplementedError

    def send_chat_action(self, chat_id, action) -> None:
        raise NotImplementedError

    def set_reaction(self, chat_id, message_id, reaction) -> None:
        raise NotImplementedError

    def edit_message(self, chat_id, message_id, text, parse_mode=None) -> dict | None:
        raise NotImplementedError

    def setup_commands(self, commands) -> None:
        raise NotImplementedError

    def download_file(self, file_id, session_name) -> str | None:
        raise NotImplementedError


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


class TelegramTransport(MessageTransport):
    """Transport that sends messages via Telegram Bot API."""

    def __init__(self, token: str):
        self._api = TelegramAPI(token)

    @property
    def name(self) -> str:
        return "telegram"

    def send_text(self, chat_id, text, parse_mode=None, reply_to=None) -> dict | None:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        # Use module-level telegram_api so tests can mock bridge.telegram_api
        return telegram_api("sendMessage", payload)

    def send_photo(self, chat_id, photo_path, caption=None) -> bool:
        if not BOT_TOKEN:
            return False
        ok, validated = validate_photo_path(photo_path)
        if not ok:
            print(validated)
            return False
        photo_path = validated
        boundary = uuid.uuid4().hex
        content_type = mimetypes.guess_type(str(photo_path))[0] or "image/jpeg"
        body_parts = []
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
        body_parts.append(b"")
        body_parts.append(str(chat_id).encode())
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"'.encode())
        body_parts.append(f"Content-Type: {content_type}".encode())
        body_parts.append(b"")
        body_parts.append(photo_path.read_bytes())
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

    def send_animation(self, chat_id, animation_path, caption=None) -> bool:
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
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
        body_parts.append(b"")
        body_parts.append(str(chat_id).encode())
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(f'Content-Disposition: form-data; name="animation"; filename="{animation_path.name}"'.encode())
        body_parts.append(f"Content-Type: {content_type}".encode())
        body_parts.append(b"")
        body_parts.append(animation_path.read_bytes())
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

    def send_document(self, chat_id, doc_path, caption=None) -> bool:
        if not BOT_TOKEN:
            return False
        ok, validated = validate_document_path(doc_path)
        if not ok:
            print(validated)
            return False
        doc_path = validated
        boundary = uuid.uuid4().hex
        content_type = mimetypes.guess_type(str(doc_path))[0] or "application/octet-stream"
        body_parts = []
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(b'Content-Disposition: form-data; name="chat_id"')
        body_parts.append(b"")
        body_parts.append(str(chat_id).encode())
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(f'Content-Disposition: form-data; name="document"; filename="{doc_path.name}"'.encode())
        body_parts.append(f"Content-Type: {content_type}".encode())
        body_parts.append(b"")
        body_parts.append(doc_path.read_bytes())
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

    def _send_media_multipart(self, chat_id, file_path, field_name, api_method, caption=None) -> bool:
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

    def send_video(self, chat_id, video_path, caption=None) -> bool:
        ok, validated = validate_document_path(video_path)
        if not ok:
            print(validated)
            return False
        return self._send_media_multipart(chat_id, validated, "video", "sendVideo", caption)

    def send_audio(self, chat_id, audio_path, caption=None) -> bool:
        ok, validated = validate_document_path(audio_path)
        if not ok:
            print(validated)
            return False
        return self._send_media_multipart(chat_id, validated, "audio", "sendAudio", caption)

    def send_voice(self, chat_id, voice_path, caption=None) -> bool:
        ok, validated = validate_document_path(voice_path)
        if not ok:
            print(validated)
            return False
        return self._send_media_multipart(chat_id, validated, "voice", "sendVoice", caption)

    def send_sticker(self, chat_id, sticker_path) -> bool:
        sticker_path = Path(sticker_path)
        if not sticker_path.exists() or not sticker_path.is_file():
            print(f"Sticker not found: {sticker_path}")
            return False
        return self._send_media_multipart(chat_id, sticker_path, "sticker", "sendSticker")

    def send_chat_action(self, chat_id, action) -> None:
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_reaction(self, chat_id, message_id, reaction) -> None:
        telegram_api("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": reaction})

    def edit_message(self, chat_id, message_id, text, parse_mode=None) -> dict | None:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return telegram_api("editMessageText", payload)

    def setup_commands(self, commands) -> None:
        telegram_api("setMyCommands", {"commands": commands})

    def download_file(self, file_id, session_name) -> str | None:
        if not BOT_TOKEN:
            return None
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
        if file_size > MAX_FILE_SIZE:
            print(f"File too large: {file_size} > {MAX_FILE_SIZE}")
            return None
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        inbox = ensure_inbox_dir(session_name)
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
            host = get_worker_host(session_name)
            if host:
                remote_inbox = str(inbox)
                _remote_run(["mkdir", "-p", remote_inbox], host=host, capture_output=True)
                _remote_run(["chmod", "700", remote_inbox], host=host, capture_output=True)
                subprocess.run(
                    ["rsync", "-az", str(local_path), f"{host}:{remote_inbox}/"],
                    capture_output=True, timeout=15)
            return str(local_path)
        except Exception as e:
            print(f"Download error: {e}")
            return None


class LocalTransport(MessageTransport):
    """Transport that logs messages to stdout. For testing without Telegram."""

    def __init__(self):
        self._log_file = os.environ.get("TRANSPORT_LOG", "")

    @property
    def name(self) -> str:
        return "local"

    def _log(self, method, chat_id, **kwargs):
        msg = f"[local-transport] {method} chat_id={chat_id}"
        for k, v in kwargs.items():
            if v is not None:
                msg += f" {k}={v}"
        print(msg)
        if self._log_file:
            with open(self._log_file, "a") as f:
                f.write(msg + "\n")

    def send_text(self, chat_id, text, parse_mode=None, reply_to=None) -> dict | None:
        self._log("send_text", chat_id, text=text[:200], parse_mode=parse_mode)
        return {"ok": True, "result": {"message_id": 1}}

    def send_photo(self, chat_id, photo_path, caption=None) -> bool:
        self._log("send_photo", chat_id, path=photo_path, caption=caption)
        return True

    def send_document(self, chat_id, doc_path, caption=None) -> bool:
        self._log("send_document", chat_id, path=doc_path, caption=caption)
        return True

    def send_animation(self, chat_id, animation_path, caption=None) -> bool:
        self._log("send_animation", chat_id, path=animation_path, caption=caption)
        return True

    def send_video(self, chat_id, video_path, caption=None) -> bool:
        self._log("send_video", chat_id, path=video_path, caption=caption)
        return True

    def send_audio(self, chat_id, audio_path, caption=None) -> bool:
        self._log("send_audio", chat_id, path=audio_path, caption=caption)
        return True

    def send_voice(self, chat_id, voice_path, caption=None) -> bool:
        self._log("send_voice", chat_id, path=voice_path, caption=caption)
        return True

    def send_sticker(self, chat_id, sticker_path) -> bool:
        self._log("send_sticker", chat_id, path=sticker_path)
        return True

    def send_chat_action(self, chat_id, action) -> None:
        self._log("send_chat_action", chat_id, action=action)

    def set_reaction(self, chat_id, message_id, reaction) -> None:
        self._log("set_reaction", chat_id, message_id=message_id)

    def edit_message(self, chat_id, message_id, text, parse_mode=None) -> dict | None:
        self._log("edit_message", chat_id, message_id=message_id, text=text[:200])
        return {"ok": True, "result": {"message_id": message_id}}

    def setup_commands(self, commands) -> None:
        self._log("setup_commands", 0, count=len(commands))

    def download_file(self, file_id, session_name) -> str | None:
        self._log("download_file", 0, file_id=file_id, session=session_name)
        return None


def _init_transport() -> MessageTransport:
    if TRANSPORT_MODE == "local":
        return LocalTransport()
    return TelegramTransport(BOT_TOKEN)


transport = _init_transport()


def telegram_api(method, data):
    """Low-level Telegram API call. Tests can mock this to intercept all outbound calls."""
    if TRANSPORT_MODE == "local":
        print(f"[local-transport] telegram_api {method} {str(data)[:100]}")
        return {"ok": True, "result": {"message_id": 1}}
    if isinstance(transport, TelegramTransport):
        return transport._api.api(method, data)
    return None


def send_telegram_message(chat_id: int, text: str, parse_mode=None):
    """Send a Telegram message, optionally with parse_mode (HTML or MarkdownV2)."""
    return transport.send_text(chat_id, text, parse_mode=parse_mode)


def download_telegram_file(file_id, session_name):
    """Download a Telegram file to the session inbox.
    Tests can patch bridge.download_telegram_file to intercept file downloads.
    Delegates to transport.download_file() internally.
    """
    return transport.download_file(file_id, session_name)


# Backward-compat module-level media stubs.
# Tests patch these (e.g. patch.object(bridge, 'send_voice', ...)).
# Production code routes through transport.*; these stubs allow test mocking.
def send_voice(chat_id, voice_path, caption=None):
    return transport.send_voice(chat_id, voice_path, caption)


def send_photo(chat_id, photo_path, caption=None):
    return transport.send_photo(chat_id, photo_path, caption)


def send_animation(chat_id, animation_path, caption=None):
    return transport.send_animation(chat_id, animation_path, caption)


def send_document(chat_id, doc_path, caption=None):
    return transport.send_document(chat_id, doc_path, caption)


def send_video(chat_id, video_path, caption=None):
    return transport.send_video(chat_id, video_path, caption)


def send_audio(chat_id, audio_path, caption=None):
    return transport.send_audio(chat_id, audio_path, caption)


def send_sticker(chat_id, sticker_path):
    return transport.send_sticker(chat_id, sticker_path)


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


def get_workers(caller_from: str = None):
    """Get all active workers with their communication details.

    If ``caller_from`` is set to a worker name, ``send_example`` for each peer
    is rendered from that caller's machine perspective.
    """
    _sync_worker_manager()
    return _merge_grpc_workers(worker_manager.get_workers(caller_from=caller_from))


# download_telegram_file removed — use download_telegram_file() instead


def transcribe_voice(file_path: str, timeout: int = None) -> Optional[str]:
    """Transcribe a voice file via STT endpoint. Returns text or None on failure.

    Fail-open: any error (timeout, bad response, unreachable) returns None
    so the caller can fall back to delivering the raw audio file.
    """
    if not STT_ENDPOINT:
        return None
    if timeout is None:
        timeout = STT_TIMEOUT

    try:
        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            return None

        boundary = uuid.uuid4().hex
        body_parts = []
        body_parts.append(f"--{boundary}".encode())
        content_type = mimetypes.guess_type(str(file_path_obj))[0] or "audio/ogg"
        body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_path_obj.name}"'.encode())
        body_parts.append(f"Content-Type: {content_type}".encode())
        body_parts.append(b"")
        body_parts.append(file_path_obj.read_bytes())
        body_parts.append(f"--{boundary}--".encode())
        body_parts.append(b"")
        body = b"\r\n".join(body_parts)

        req = urllib.request.Request(
            STT_ENDPOINT,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
            text = result.get("text", "").strip()
            if text:
                duration = result.get("audio_duration_s", "?")
                print(f"STT transcribed: {len(text)} chars from {duration}s audio")
                return text
            return None
    except Exception as e:
        print(f"STT error (fail-open): {e}")
        return None


def synthesize_speech(text: str, voice: str = None, language: str = "en") -> Optional[str]:
    """Synthesize speech from text via TTS endpoint. Returns OGG file path or None.

    Fail-open: any error returns None so caller can skip voice and send text only.
    """
    if not TTS_ENDPOINT:
        return None
    if not text or not text.strip():
        return None
    if voice is None:
        voice = TTS_VOICE

    try:
        # Strip HTML tags for clean speech
        clean = re.sub(r'<[^>]+>', '', text)
        clean = clean.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        clean = clean.strip()
        if not clean:
            return None

        payload = json.dumps({
            "text": clean[:5000],  # API limit
            "voice": voice,
            "language": language,
            "format": "ogg",
        }).encode()

        # Use chunked endpoint for longer text (splits into sentences server-side)
        endpoint = TTS_ENDPOINT
        if len(clean) > TTS_CHUNKED_THRESHOLD and TTS_ENDPOINT:
            chunked_url = TTS_ENDPOINT.rstrip('/') + '/chunked'
            # Only use chunked if it looks like /synthesize base
            if '/synthesize' in TTS_ENDPOINT:
                endpoint = chunked_url

        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TTS_TIMEOUT) as r:
            audio_data = r.read()
            if not audio_data:
                return None

            # Write to temp file
            tmp_path = Path(tempfile.gettempdir()) / f"tts_{uuid.uuid4().hex}.ogg"
            tmp_path.write_bytes(audio_data)
            tmp_path.chmod(0o600)

            duration = r.headers.get("X-Audio-Duration", "?")
            proc_time = r.headers.get("X-Processing-Time", "?")
            mode = "chunked" if endpoint != TTS_ENDPOINT else "single"
            print(f"TTS synthesized ({mode}): {len(clean)} chars -> {duration}s audio in {proc_time}s")
            return str(tmp_path)
    except Exception as e:
        print(f"TTS error (fail-open): {e}")
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


# Media extensions routed to specialized Telegram API methods
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".aac", ".wav"}
VOICE_EXTENSIONS = {".ogg", ".opus", ".oga"}
STICKER_EXTENSIONS = {".tgs"}  # animated stickers; static .webp handled by sendPhoto


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

    transport.setup_commands(commands)
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


def _read_session_file(name, filename):
    """Read a session file, routing to remote host for teleported workers.

    Tries local cache first (fast), falls back to SSH for remote workers.
    Local cache is populated by this function and by save_claude_session_*.
    """
    # Try local first (works for local workers, fast cache for remote)
    f = get_session_dir(name) / filename
    if f.exists():
        val = f.read_text().strip()
        if val:
            return val
    # For remote workers, try SSH if local is missing
    host = get_worker_host(name)
    if host:
        try:
            remote_home = _get_remote_home(host) or ""
            local_home = str(Path.home())
            session_path = str(get_session_dir(name) / filename)
            if remote_home and remote_home != local_home and session_path.startswith(local_home):
                session_path = remote_home + session_path[len(local_home):]
            r = _remote_run(["cat", session_path], host=host,
                            capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                val = r.stdout.strip()
                # Cache locally for next read
                try:
                    ensure_session_dir(name)
                    f.write_text(val)
                    f.chmod(0o600)
                except Exception:
                    pass
                return val
        except Exception:
            pass
    return ""


def _scan_latest_session_id(cwd: str, host: str = None) -> str:
    """Return the UUID of the most-recently-modified JSONL in <slug>/ on `host`.

    Source of truth for the "current" session Claude Code is writing to.
    host=None → scan local filesystem. host set → SSH + `ls -1t`.
    Returns "" if the slug dir is missing/empty or the scan errors out.
    """
    if not cwd:
        return ""
    slug = _project_slug(cwd)
    if host:
        try:
            cmd = [
                "bash", "-c",
                f'ls -1t "$HOME/.claude/projects/{slug}"/*.jsonl 2>/dev/null | head -1',
            ]
            r = _remote_run(cmd, host=host, capture_output=True,
                            text=True, timeout=10)
            if r.returncode != 0:
                return ""
            path = (r.stdout or "").strip()
            if not path:
                return ""
            return os.path.basename(path).removesuffix(".jsonl")
        except Exception:
            return ""
    # Local scan
    slug_dir = CLAUDE_PROJECTS_DIR / slug
    if not slug_dir.is_dir():
        return ""
    try:
        jsonls = [p for p in slug_dir.iterdir()
                  if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return ""
    if not jsonls:
        return ""
    latest = max(jsonls, key=lambda p: p.stat().st_mtime)
    return latest.stem


def _log_session_event(name: str, session_id: str, cwd: str, event: str) -> None:
    """Append a session event to the worker's audit log (best effort)."""
    if not session_id:
        return
    try:
        d = ensure_session_dir(name)
        f = d / "session_history.jsonl"
        entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
            "cwd": cwd or "",
            "event": event,
        })
        with open(f, "a") as fh:
            fh.write(entry + "\n")
        f.chmod(0o600)
    except Exception:
        pass


def get_session_history(name: str, event: str = None) -> list:
    """Read the session audit log for a worker. Optional event filter."""
    f = get_session_dir(name) / "session_history.jsonl"
    if not f.exists():
        return []
    entries = []
    for line in f.read_text().strip().splitlines():
        if not line:
            continue
        try:
            e = json.loads(line)
            if event and e.get("event") != event:
                continue
            entries.append(e)
        except json.JSONDecodeError:
            continue
    return entries


def _cache_session_id(name: str, sid: str) -> None:
    """Write session_id to local VPS cache file (best effort, 0o600)."""
    if not sid:
        return
    try:
        d = ensure_session_dir(name)
        f = d / "claude_session_id"
        old = f.read_text().strip() if f.exists() else ""
        if old != sid:
            cwd = get_claude_session_cwd(name)
            _log_session_event(name, sid, cwd, "cache")
        f.write_text(sid)
        f.chmod(0o600)
    except Exception:
        pass


def get_claude_session_id(name: str, authoritative: bool = False) -> str:
    """Return the Claude Code session UUID for a worker.

    The local `claude_session_id` file is a cache/hint populated by
    (a) the Stop hook POST to /response and (b) this function's scan fallback.
    The *authoritative* source is the latest-mtime JSONL under
    `~/.claude/projects/<slug>/` on the host where the worker is running.

    authoritative=False (default): return the cached value if present.
        If the cache is empty, scan the transcript dir and cache the result.
    authoritative=True: always scan the transcript dir and refresh the cache.
        Use this for correctness-critical call sites — /rewind, /restart,
        memory source deep-links — where a stale UUID leads to "transcript
        not available" or a failed `--resume`. Falls back to cached value
        if the scan itself fails (SSH down, dir missing).
    """
    cache_file = get_session_dir(name) / "claude_session_id"

    def _read_cache():
        if cache_file.exists():
            val = cache_file.read_text().strip()
            if val:
                return val
        return ""

    if not authoritative:
        val = _read_cache()
        if val:
            return val
        # Self-heal: cache empty, try to populate via scan
        cwd = get_claude_session_cwd(name)
        if cwd:
            host = get_worker_host(name)
            scanned = _scan_latest_session_id(cwd, host=host)
            if scanned:
                _cache_session_id(name, scanned)
                return scanned
        return ""

    # Authoritative: always scan
    cwd = get_claude_session_cwd(name)
    if cwd:
        host = get_worker_host(name)
        scanned = _scan_latest_session_id(cwd, host=host)
        if scanned:
            _cache_session_id(name, scanned)
            return scanned
    # Scan failed — better to return stale cache than nothing
    return _read_cache()


def get_claude_session_cwd(name):
    cwd = _read_session_file(name, "claude_session_cwd")
    if cwd:
        cwd = os.path.expanduser(cwd)
    return cwd


def save_claude_session_cwd(name, cwd):
    if cwd:
        cwd = os.path.expanduser(cwd)
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
    # Sync chat_id to remote host if worker is teleported.
    # The Stop hook reads chat_id locally — without this, responses from
    # teleported workers never reach Telegram.
    _sync_chat_id_to_remote(name, str(chat_id_file))


_remote_home_cache = {}  # host -> remote $HOME path

def _get_remote_home(host):
    """Get remote $HOME with caching (avoids SSH per message)."""
    if host in _remote_home_cache:
        return _remote_home_cache[host]
    try:
        r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                        capture_output=True, text=True, timeout=5)
        home = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        home = ""
    _remote_home_cache[host] = home
    return home


def _remap_sessions_dir(host):
    """Remap SESSIONS_DIR to use remote host's $HOME prefix."""
    remote_sessions_dir = str(SESSIONS_DIR)
    local_home = os.path.expanduser("~")
    remote_home = _get_remote_home(host)
    if remote_home and remote_home != local_home and remote_sessions_dir.startswith(local_home):
        remote_sessions_dir = remote_home + remote_sessions_dir[len(local_home):]
    return remote_sessions_dir


def _sync_chat_id_to_remote(name, local_chat_id_path):
    """Sync chat_id file to remote host if worker is teleported there.

    The Stop hook reads SESSIONS_DIR/<worker>/chat_id locally on the machine
    where Claude runs. For teleported workers, the hook is on the remote host
    but chat_id is only written on VPS. This bridges the gap by pushing the
    file after each write.
    """
    host = get_worker_host(name)
    if not host:
        return
    try:
        remote_sessions_dir = _remap_sessions_dir(host)
        _remote_run(["mkdir", "-p", f"{remote_sessions_dir}/{name}"],
                     host=host, capture_output=True)
        _remote_copy(local_chat_id_path, f"{remote_sessions_dir}/{name}/chat_id",
                      host=host, direction="push")
    except Exception as e:
        print(f"[set_pending] Failed to sync chat_id to {host} for {name}: {e}")


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
    """Read the last N lines of adapter.log for a worker, or empty string.
    For teleported workers, reads via SSH from the remote host.
    """
    if tail_lines <= 0:
        return ""
    host = get_worker_host(name)
    if host:
        try:
            # Remap path for remote $HOME
            r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                            capture_output=True, text=True, timeout=5)
            remote_home = r.stdout.strip() if r.returncode == 0 else ""
            local_home = str(Path.home())
            remote_log = str(get_session_dir(name) / "adapter.log")
            if remote_home and remote_home != local_home and remote_log.startswith(local_home):
                remote_log = remote_home + remote_log[len(local_home):]
            r = _remote_run(["tail", "-n", str(tail_lines), remote_log], host=host,
                            capture_output=True, text=True, timeout=5)
            return r.stdout if r.returncode == 0 else ""
        except Exception:
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
    For teleported workers, reads the file from the remote host.
    """
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"
    host = get_worker_host(name)

    if host:
        try:
            r = _remote_run(["cat", signal_path], host=host,
                            capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return None
            raw = r.stdout.strip()
        except Exception:
            return None
    else:
        signal_file = Path(signal_path)
        if not signal_file.exists():
            return None
        try:
            raw = signal_file.read_text().strip()
        except Exception:
            return None

    if not raw:
        return None
    lines = raw.splitlines()

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
    """Remove hook failure signal file for a worker (on restart/clean).
    For teleported workers, removes the file on the remote host.
    """
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"
    host = get_worker_host(name)
    if host:
        try:
            _remote_run(["rm", "-f", signal_path], host=host,
                        capture_output=True, timeout=5)
        except Exception:
            pass
    else:
        try:
            Path(signal_path).unlink(missing_ok=True)
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
        result = transport.send_text(admin_chat_id, text)
        if result and result.get("ok"):
            print(f"[watchdog] Alert sent for {name} ({state}): {text[:80]}")
            msg_id = result.get("result", {}).get("message_id")
            with _watchdog_lock:
                _last_alert_ts[name] = now
                if msg_id:
                    _alert_msg_ids[name] = (msg_id, text)
        else:
            print(f"[watchdog] Alert FAILED for {name} ({state}): {result}")
    except Exception as e:
        print(f"Watchdog alert error: {e}")


def _record_host_probe(host: str, ok: bool, error: str | None = None) -> None:
    """Track host SSH probe results; alert on DOWN/BACK UP transitions."""
    now = time.time()
    with _watchdog_lock:
        was_down = _host_down.get(host, False)
        if ok:
            _host_ssh_failures[host] = 0
            _host_last_error.pop(host, None)
            if was_down:
                _host_down[host] = False
                down_since = _host_down_since.pop(host, now)
                duration = int(now - down_since)
                workers_on_host = [n for n, s in get_registered_sessions().items() if get_worker_host(n) == host]
                alert_text = (f"✅ Host BACK UP: {host}\n"
                              f"Was down for {duration // 60}m {duration % 60}s\n"
                              f"Workers affected: {', '.join(workers_on_host) or 'none'}")
                _do_send = True
            else:
                _do_send = False
                alert_text = None
        else:
            failures = _host_ssh_failures.get(host, 0) + 1
            _host_ssh_failures[host] = failures
            _host_last_error[host] = error or "ssh probe failed"
            if not was_down and failures >= HOST_DOWN_THRESHOLD:
                _host_down[host] = True
                _host_down_since[host] = now
                workers_on_host = [n for n, s in get_registered_sessions().items() if get_worker_host(n) == host]
                alert_text = (f"🔴 Host DOWN: {host}\n"
                              f"After {failures} consecutive SSH failures\n"
                              f"Error: {error or 'unknown'}\n"
                              f"Workers affected: {', '.join(workers_on_host) or 'none'}")
                _do_send = True
            else:
                _do_send = False
                alert_text = None

    if _do_send and alert_text and admin_chat_id:
        try:
            transport.send_text(admin_chat_id, alert_text)
            print(f"[watchdog] Host alert: {alert_text.splitlines()[0]}")
        except Exception as e:
            print(f"[watchdog] Host alert error: {e}")


def _is_host_down(host: str) -> bool:
    with _watchdog_lock:
        return _host_down.get(host, False)


def _check_disk_usage(host: str | None = None) -> dict | None:
    """Check disk usage on a host (None = local). Returns {pct, free_gb, total_gb} or None."""
    try:
        r = _remote_run(
            ["df", "-BG", "--output=size,used,avail,pcent", "/"],
            host=host, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            return None
        parts = lines[1].split()
        if len(parts) < 4:
            return None
        total_gb = float(parts[0].rstrip("G"))
        free_gb = float(parts[2].rstrip("G"))
        pct = int(parts[3].rstrip("%"))
        return {"pct": pct, "free_gb": free_gb, "total_gb": total_gb}
    except Exception:
        return None


def _check_disk_usage_macos(host: str) -> dict | None:
    """Check disk usage on macOS host (df output differs from Linux)."""
    try:
        r = _remote_run(
            ["df", "-g", "/"],
            host=host, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            return None
        parts = lines[1].split()
        if len(parts) < 6:
            return None
        total_gb = float(parts[1])
        free_gb = float(parts[3])
        pct = int(parts[4].rstrip("%"))
        return {"pct": pct, "free_gb": free_gb, "total_gb": total_gb}
    except Exception:
        return None


def _probe_disk_all_hosts(remote_hosts: set[str]) -> None:
    """Probe disk usage on local + remote hosts, alert on threshold breaches."""
    now = time.time()
    hosts_to_check = [None] + list(remote_hosts)  # None = local (VPS)

    for host in hosts_to_check:
        if host and _is_host_down(host):
            continue

        host_label = host or "VPS"
        is_mac = host and "mac" in host.lower()
        usage = _check_disk_usage_macos(host) if is_mac else _check_disk_usage(host)

        if usage is None:
            continue

        with _watchdog_lock:
            _host_disk_usage[host_label] = {**usage, "ts": now}

        is_critical = usage["pct"] >= DISK_ALERT_THRESHOLD_PCT or usage["free_gb"] < DISK_ALERT_THRESHOLD_GB
        was_alerted = _host_disk_alerted.get(host_label, False)

        if is_critical and not was_alerted:
            last_alert = _host_disk_alert_ts.get(host_label, 0)
            if now - last_alert >= DISK_ALERT_COOLDOWN:
                alert_text = (
                    f"💾 Disk space critical: {host_label}\n"
                    f"Usage: {usage['pct']}% ({usage['free_gb']:.1f}GB free of {usage['total_gb']:.0f}GB)"
                )
                _host_disk_alert_ts[host_label] = now
                _host_disk_alerted[host_label] = True
                if admin_chat_id:
                    try:
                        transport.send_text(admin_chat_id, alert_text)
                        print(f"[watchdog] Disk alert: {alert_text.splitlines()[0]}")
                    except Exception as e:
                        print(f"[watchdog] Disk alert error: {e}")
        elif not is_critical and was_alerted:
            _host_disk_alerted[host_label] = False
            if admin_chat_id:
                try:
                    transport.send_text(
                        admin_chat_id,
                        f"✅ Disk space recovered: {host_label} — {usage['pct']}% ({usage['free_gb']:.1f}GB free)"
                    )
                except Exception:
                    pass


_last_resolved_ts: dict[str, float] = {}  # Per-worker resolved alert cooldown

def _send_resolved_alert(name: str, new_state: str) -> None:
    if admin_chat_id is None:
        return

    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    with _watchdog_lock:
        prev_state = _prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states:
        return

    # Suppress if worker was recently restarted (cmd_restart sends its own confirmation)
    restart_ts = _recent_restarts.get(name)
    if restart_ts and time.time() - restart_ts < 30:
        return

    # Cooldown: don't spam "back to normal" for flapping workers
    now = time.time()
    last_resolved = _last_resolved_ts.get(name, 0)
    if now - last_resolved < 180:
        return

    _last_resolved_ts[name] = now

    # Edit the old alert to show resolved
    with _watchdog_lock:
        alert_info = _alert_msg_ids.pop(name, None)
    if alert_info:
        old_msg_id, old_text = alert_info
        resolved_text = f"✅ {name} resolved (was: {old_text.splitlines()[0]})"
        try:
            transport.edit_message(admin_chat_id, old_msg_id, resolved_text)
            print(f"[watchdog] Edited alert for {name} -> resolved")
            return
        except Exception:
            pass  # Fall through to send new message

    text = f"✅ {name} is back to normal."
    try:
        transport.send_text(admin_chat_id, text)
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

    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with _watchdog_lock:
        prev_state = _prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state

    # Host-level alerts are sent by _record_host_probe; suppress per-worker spam
    if state == "HOST_OFFLINE":
        with _watchdog_lock:
            _prev_worker_states[name] = state
        return

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

    GOOD_PROBE_THRESHOLD = 3
    BAD_PROBE_THRESHOLD = 3

    is_remote = bool(get_worker_host(name))

    if state in bad_states:
        with _watchdog_lock:
            _consecutive_good_probes[name] = 0

        if is_remote and state in {"OFFLINE", "DEAD"}:
            with _watchdog_lock:
                _consecutive_bad_probes[name] = _consecutive_bad_probes.get(name, 0) + 1
                bad_count = _consecutive_bad_probes[name]
            if bad_count < BAD_PROBE_THRESHOLD:
                return

        if state_changed or prev_state is None:
            if eligible_for_alert():
                print(f"[watchdog] State change {name}: {prev_state} -> {state} ({reason}), sending alert")
                _send_watchdog_alert(name, state, reason)
        elif state in {"OFFLINE", "DEAD", "EXITED"} and eligible_for_alert():
            _send_watchdog_alert(name, state, reason)
        with _watchdog_lock:
            _prev_worker_states[name] = state
        return

    if state in good_states and prev_state in bad_states:
        with _watchdog_lock:
            _consecutive_good_probes[name] = _consecutive_good_probes.get(name, 0) + 1
            _consecutive_bad_probes[name] = 0
            good_count = _consecutive_good_probes[name]
        if good_count >= GOOD_PROBE_THRESHOLD:
            _send_resolved_alert(name, state)
            with _watchdog_lock:
                _consecutive_good_probes[name] = 0
                _prev_worker_states[name] = state
        return

    with _watchdog_lock:
        _consecutive_good_probes[name] = 0
        _consecutive_bad_probes[name] = 0
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
    _disk_check_counter = 0
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
            failed_hosts = set()  # hosts where SSH probe failed this cycle
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
                        _record_host_probe(host, ok=True)
                    else:
                        failed_hosts.add(host)
                        _record_host_probe(host, ok=False, error=f"tmux list-panes exit {r.returncode}")
                except Exception as e:
                    failed_hosts.add(host)
                    _record_host_probe(host, ok=False, error=str(e)[:200])

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

                # Mark workers on down hosts as HOST_OFFLINE instead of silently skipping
                host = get_worker_host(name)
                if host and _is_host_down(host):
                    reason = f"host {host} offline"
                    since = _record_worker_state(name, "HOST_OFFLINE", reason, now)
                    _handle_watchdog_transition(name, "HOST_OFFLINE", reason, since, now=now)
                    continue
                if host and host in failed_hosts and not tmux_exists:
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

                # Activity detection: if children count increased or CPU is active,
                # worker is doing something. Reset the stale-pending timer so
                # long autonomous work doesn't trigger false STUCK alerts.
                # Only increases count — background sleep cycling (exit+restart)
                # causes ±1 flicker that shouldn't reset the timer.
                with _watchdog_lock:
                    prev_children = _prev_children.get(name)
                    activity_increased = (prev_children is not None and children > prev_children)
                    if activity_increased or cpu >= CPU_ACTIVE:
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
            # Disk space check every ~5 min (every 20th cycle)
            _disk_check_counter += 1
            if _disk_check_counter >= 20:
                _disk_check_counter = 0
                try:
                    _probe_disk_all_hosts(set(remote_workers.keys()))
                except Exception as de:
                    print(f"[watchdog] Disk check error: {de}")

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


def validate_cwd(cwd: Optional[str], host: str = None) -> tuple[str, str]:
    """Validate cwd path. Returns (normalized_path, error_message).

    When host is set, validates via SSH on the remote machine instead of locally.
    """
    normalized = normalize_cwd(cwd)
    if not normalized:
        return "", "cwd is empty"
    if host:
        # Remote validation: check directory exists on the remote host
        try:
            r = _remote_run(["test", "-d", normalized], host=host,
                            capture_output=True, timeout=10)
            if r.returncode != 0:
                return "", f"cwd does not exist on {host}: {normalized}"
        except Exception as e:
            return "", f"cwd check failed on {host}: {e}"
    else:
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
    if state == "HOST_OFFLINE":
        return "Host offline"
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


def _send_to_grpc_worker(name: str, message: str, from_name: str = "manager") -> bool:
    if grpc_server is None:
        return False

    try:
        if not grpc_server.is_worker_connected(name):
            return False
        if grpc_server.send_to_worker(name, message, from_name):
            return True
        print(f"gRPC send failed for '{name}', falling back to tmux backend")
    except Exception as e:
        print(f"gRPC send error for '{name}', falling back to tmux backend: {e}")
    return False


def _send_to_callback_worker(name: str, message: str, from_name: str = "manager", session: dict = None) -> bool:
    callback_url = (session or {}).get("callback_url", "")
    if not callback_url:
        callback_url = _load_registry().get("workers", {}).get(name, {}).get("callback_url", "")
    if not callback_url:
        return False
    msg_url = callback_url.rstrip("/")
    if not msg_url.endswith("/msg"):
        msg_url = f"{msg_url}/msg"
    body = json.dumps({"from": from_name, "text": message}).encode()
    req = urllib.request.Request(
        msg_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"Callback send failed for '{name}' at {msg_url}: {e}")
        return False


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
        """Resolve startup cwd with priority: explicit > RAM hint > disk > fallback."""
        candidate = normalize_cwd(requested_cwd)
        if not candidate:
            candidate = normalize_cwd(_get_worker_cwd(name))
        if not candidate:
            candidate = normalize_cwd(get_claude_session_cwd(name))
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
                for key in ("protocol", "callback_url", "host", "version"):
                    if info.get(key):
                        entry[key] = info.get(key)
                # Teleported workers: inject tmux name so they don't appear as "exited"
                if info.get("host") and not info.get("callback_url"):
                    entry["tmux"] = f"{self.tmux_prefix}{name}"
                registered[name] = entry
            else:
                for key in ("protocol", "callback_url", "host", "version"):
                    if info.get(key):
                        registered[name][key] = info.get(key)

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

        if session.get("callback_url"):
            return True

        backend_name = normalize_backend(session.get("backend"))
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        # For teleported workers, check remote tmux AND claude process
        # Treat SSH failures as "online" to avoid false OFFLINE from transient network issues
        host = get_worker_host(name)
        if host:
            try:
                if not tmux_exists(tmux_name, host=host):
                    return False
                if backend.is_interactive:
                    return is_claude_running(tmux_name, host=host)
                return True
            except Exception:
                return True  # SSH failure — assume still online

        return backend.is_online(tmux_name)

    def send(self, name: str, message: str, chat_id: int = None, session: dict = None) -> bool:
        """Send message to worker using backend registry."""
        self._sync_paths()
        if _send_to_grpc_worker(name, message, "manager"):
            return True
        if not session:
            sessions = self.get_registered_sessions()
            session = sessions.get(name)
        if not session:
            return False

        if session.get("callback_url"):
            return _send_to_callback_worker(name, message, "manager", session)

        backend_name = normalize_backend(session.get("backend"))
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")

        return backend.send(name, tmux_name, message, BRIDGE_URL, self.sessions_dir)

    def get_workers(self, caller_from: str = None):
        """Get all active workers with their communication details.

        If ``caller_from`` is the name of a registered worker, each ``send_example``
        is rendered from that caller's perspective: bare tmux/pipe when caller
        and peer share a machine, ssh-wrapped when they don't. When ``caller_from``
        is None, the bridge's own perspective is used (legacy behavior).
        """
        self._sync_paths()
        workers = []
        registered = self.get_registered_sessions()
        caller_host = get_worker_host(caller_from) if caller_from else None
        for name, info in registered.items():
            callback_url = info.get("callback_url", "")
            if callback_url:
                msg_url = callback_url.rstrip("/")
                if not msg_url.endswith("/msg"):
                    msg_url = f"{msg_url}/msg"
                payload = json.dumps({"from": "YOUR_NAME", "text": "your message here"})
                send_example = (
                    f"curl -sS -X POST {shlex.quote(msg_url)} "
                    f"-H 'Content-Type: application/json' "
                    f"--data-raw {shlex.quote(payload)}"
                )
                workers.append({
                    "name": name,
                    "machine": info.get("host", "") or BRIDGE_SSH_TARGET,
                    "protocol": "http",
                    "address": msg_url,
                    "send_example": send_example,
                    "note": "HTTP callback worker. POST JSON with from/text. Always set from to your worker name.",
                })
                continue

            backend_name = get_worker_backend(name, info)
            backend = get_backend(backend_name)
            peer_host = get_worker_host(name)

            # Registry-only workers (tmux gone): non-interactive can still serve via pipe
            if "tmux" not in info:
                if not backend.is_interactive:
                    pipe_path = ensure_worker_pipe(name)
                    pipe_cmd = f"echo 'YOUR_NAME: your message here' > {pipe_path} &"
                    send_example = self._wrap_for_caller(pipe_cmd, peer_host, caller_host)
                    workers.append({
                        "name": name,
                        "machine": peer_host or BRIDGE_SSH_TARGET,
                        "protocol": "pipe",
                        "address": str(pipe_path),
                        "send_example": send_example,
                        "note": "Non-interactive. IMPORTANT: Always prefix your name (e.g., 'kenji: hello'). Always use & (background) when writing to pipe — it BLOCKS until read. Never use cat/echo without & or your session will freeze."
                    })
                else:
                    workers.append({
                        "name": name,
                        "machine": peer_host or BRIDGE_SSH_TARGET,
                        "protocol": "none",
                        "address": "",
                        "status": "exited",
                        "note": f"Worker exited. Use /restart {name} to bring back.",
                    })
                continue

            if not backend.is_interactive:
                if peer_host:
                    # Non-interactive remote workers can't use local pipes
                    workers.append({
                        "name": name,
                        "machine": peer_host,
                        "protocol": "none",
                        "address": f"{peer_host}:{info.get('tmux', '')}",
                        "note": f"Non-interactive ({backend_name}) on {peer_host}. Remote pipe not supported yet.",
                    })
                else:
                    pipe_path = ensure_worker_pipe(name)
                    pipe_cmd = f"echo 'YOUR_NAME: your message here' > {pipe_path} &"
                    send_example = self._wrap_for_caller(pipe_cmd, peer_host, caller_host)
                    workers.append({
                        "name": name,
                        "machine": peer_host or BRIDGE_SSH_TARGET,
                        "protocol": "pipe",
                        "address": str(pipe_path),
                        "send_example": send_example,
                        "note": "Non-interactive. IMPORTANT: Always prefix your name (e.g., 'kenji: hello'). Always use & (background) when writing to pipe — it BLOCKS until read. Never use cat/echo without & or your session will freeze."
                    })
            else:
                tmux_name = info.get("tmux")
                tmux_cmd = (
                    f"echo 'YOUR_NAME: your message here' | "
                    f"tmux load-buffer - && "
                    f"tmux paste-buffer -p -r -t {tmux_name} && "
                    f"sleep 1 && tmux send-keys -t {tmux_name} Enter"
                )
                send_example = self._wrap_for_caller(tmux_cmd, peer_host, caller_host)
                if peer_host and caller_host == peer_host:
                    note = f"On {peer_host} (same machine as caller). Uses paste-buffer -p. Always prefix your name."
                elif peer_host:
                    note = f"On {peer_host}. Uses SSH + paste-buffer -p (bracketed paste). Always prefix your name."
                elif caller_host:
                    note = f"On bridge host (cross-machine from caller). Uses SSH + paste-buffer -p. Always prefix your name."
                else:
                    note = "Uses paste-buffer -p (bracketed paste) for reliable delivery. Sleep 1s before Enter — TUI needs time to render. Always prefix your name."
                workers.append({
                    "name": name,
                    "machine": peer_host or BRIDGE_SSH_TARGET,
                    "protocol": "tmux",
                    "address": f"{peer_host}:{tmux_name}" if peer_host else tmux_name,
                    "send_example": send_example,
                    "note": note,
                })
        return workers

    def _wrap_for_caller(self, cmd: str, peer_host: str, caller_host: str) -> str:
        """Wrap a shell command so it executes on the peer's machine from the caller's POV.

        - Same machine (incl. both None): bare command, no ssh.
        - Caller on bridge, peer remote: ssh to peer's host (legacy behavior).
        - Caller remote, peer on bridge: ssh to BRIDGE_SSH_TARGET.
        - Caller and peer on different remotes: ssh directly to peer's host.
        """
        if caller_host == peer_host:
            return cmd
        if peer_host is None:
            ssh_target = BRIDGE_SSH_TARGET
        else:
            ssh_target = peer_host
        escaped = cmd.replace('"', '\\"')
        return f'ssh {ssh_target} "{escaped}"'

    def _build_welcome(self, name: str, backend_obj) -> str:
        """Build welcome/instructions message for a worker."""
        welcome = (
            "You are connected to Telegram via claudecode-telegram bridge. "
            "RECEIVING FILES: Manager sends files (images, PDFs, documents) — they appear as local paths you can read directly. "
            "SENDING FILES: Use [[image:/path/to/photo.png|caption]] for images (jpg/png/webp/bmp) and animations (gif/mp4), or [[file:/path/to/file|caption]] for documents, video (mp4/mov/avi — shows player), audio (mp3/m4a/flac — shows player), and voice (ogg/opus — voice bubble). "
            f"MESSAGING WORKERS: Run `curl -s \"$BRIDGE_URL/workers?from={name}\"` to discover other workers — returns JSON with a `send_example` field containing ready-to-use send commands wrapped correctly for your machine (auto-adds ssh when a peer lives elsewhere). Always call /workers?from={name} before messaging, never guess addresses. "
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

        # Append manager note if set (with {name} and {machine} substitution)
        note = read_checkin_note()
        if note:
            rendered = note.replace("{name}", name)
            host = get_worker_host(name)
            if host:
                machine = f"Mac Mini ({host})"
            else:
                machine = "VPS (100.125.36.102)"
            rendered = rendered.replace("{machine}", machine)
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
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True)

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

        if SANDBOX_ENABLED and backend_obj.is_interactive:
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
            print(f"Started worker '{name}' in sandbox mode")
        else:
            start_cmd = f'unset CLAUDECODE && {backend_obj.start_cmd()}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])
            if backend_obj.is_interactive:
                time.sleep(1.5)
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
        _reset_learning_reminder(name)

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
        host = get_worker_host(name)
        _remote_run(["tmux", "kill-session", "-t", tmux_name], host=host, capture_output=True)
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

        For teleported workers, returns sentinel (False, "use_remote_restart") —
        callers should route to CommandRouter._restart_remote_worker() instead.
        """
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered:
            return False, f"Worker '{name}' not found"

        # Teleported workers must be restarted via CommandRouter._restart_remote_worker
        host = get_worker_host(name)
        if host:
            return False, "use_remote_restart"

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
            resume_id = get_claude_session_id(name, authoritative=True)
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
            # Poll until Claude has actually exited (fixed sleep races with slow exits)
            for _ in range(20):
                if not is_claude_running(tmux_name):
                    break
                time.sleep(0.25)
            else:
                print(f"[restart] {name}: Claude still running after 5s kill wait")

        # Kill any stray child process (e.g. SSH, vim) before sending start command
        if backend.is_interactive and not is_claude_running(tmux_name):
            pane_pids = _tmux_pane_pids()
            pane_pid = pane_pids.get(tmux_name)
            if pane_pid:
                stray = subprocess.run(
                    ["pgrep", "-P", str(pane_pid)],
                    capture_output=True, text=True, timeout=5
                )
                if stray.returncode == 0:
                    for child_pid in stray.stdout.strip().splitlines():
                        child_pid = child_pid.strip()
                        if child_pid and child_pid.isdigit():
                            print(f"[restart] {name}: killing stray child pid {child_pid}")
                            subprocess.run(["kill", child_pid], capture_output=True)
                    time.sleep(0.5)

        export_hook_env(tmux_name, backend_name)
        time.sleep(0.3)

        # Inject tmux env vars then unset CLAUDECODE (prevents nested-session error)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"])
        time.sleep(0.3)

        if SANDBOX_ENABLED and backend.is_interactive:
            stop_docker_container(name)
            time.sleep(0.5)
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        else:
            start_cmd = backend.start_cmd(resume_id)
            start_cmd = f'unset CLAUDECODE && {start_cmd}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])

        # Wait for Claude to actually start before sending welcome
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = False
            for _ in range(10):
                time.sleep(1.0)
                if is_claude_running(tmux_name):
                    started = True
                    break
            if not started and resume_id:
                print(f"[restart] {name}: resume failed (stale session {resume_id[:8]}), alerting admin")
                session_id_path = os.path.join(
                    os.path.expanduser("~"), ".claude", "telegram", "nodes",
                    _node_name(), "sessions", name, "claude_session_id"
                )
                try:
                    os.remove(session_id_path)
                    print(f"[restart] {name}: cleared stale session_id file")
                except FileNotFoundError:
                    pass
                if admin_chat_id:
                    alert = (
                        f"⚠️ {name}: resume failed (stale session ID {resume_id[:8]}…)\n"
                        f"Session ID cleared. Use /restart --clean {name} to start fresh."
                    )
                    send_telegram_message(admin_chat_id, alert)
            if started:
                self.send(name, welcome)
            else:
                print(f"[restart] {name}: Claude did not start within 10s, skipping welcome")
        else:
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"])

        _reset_learning_reminder(name)
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
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True)

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
            resume_id = get_claude_session_id(name, authoritative=True)
            resume_cwd = get_claude_session_cwd(name)
        else:
            session_dir = self.sessions_dir / name
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd:
            save_claude_session_cwd(name, startup_cwd)

        if SANDBOX_ENABLED and backend.is_interactive:
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id)
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"])
        else:
            start_cmd = backend.start_cmd(resume_id)
            start_cmd = f'unset CLAUDECODE && {start_cmd}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            subprocess.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"])
            if backend.is_interactive:
                time.sleep(1.5)
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

    For remote hosts, remaps SESSIONS_DIR to use the remote $HOME prefix
    (e.g., /home/claude/... → /Users/beastoinagents/...).
    """
    # Guard: don't overwrite env if session belongs to another live bridge.
    # Prevents test/dev bridges from clobbering prod workers.
    our_url = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    try:
        r = _remote_run(["tmux", "show-environment", "-t", tmux_name, "BRIDGE_URL"],
                        host=host, capture_output=True, text=True, timeout=3)
        existing = r.stdout.strip().split("=", 1)[-1] if r.returncode == 0 else ""
        if existing and existing != our_url:
            import urllib.request
            urllib.request.urlopen(existing, timeout=1).read()
            print(f"  SKIP export_hook_env({tmux_name}): owned by live bridge at {existing}")
            return
    except Exception:
        pass  # other bridge dead or unreachable — safe to claim

    _remote_run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)], host=host)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX], host=host)
    # Remap SESSIONS_DIR for remote hosts (different $HOME path)
    sessions_dir_val = str(SESSIONS_DIR)
    if host:
        try:
            r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                            capture_output=True, text=True, timeout=5)
            remote_home = r.stdout.strip() if r.returncode == 0 else ""
            local_home = str(Path.home())
            if remote_home and remote_home != local_home and sessions_dir_val.startswith(local_home):
                sessions_dir_val = remote_home + sessions_dir_val[len(local_home):]
        except Exception:
            pass
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", sessions_dir_val], host=host)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "WORKER_BACKEND", normalize_backend(backend)], host=host)
    # Always export BRIDGE_URL so workers know where their bridge is
    # Remote workers need BRIDGE_PUBLIC_URL (reachable IP), not localhost
    bridge_url_val = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", bridge_url_val], host=host)


def get_docker_run_cmd(name, resume_id: str = ""):
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
    cmd_parts.append(build_claude_start_cmd(resume_id))

    return " ".join(cmd_parts)


def stop_docker_container(name):
    """Stop and remove a docker container."""
    container_name = f"claude-worker-{name}"
    subprocess.run(["docker", "stop", container_name], capture_output=True)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)


def send_to_worker(name: str, message: str, chat_id: Optional[int] = None) -> bool:
    """Send a message to a worker using the appropriate backend."""
    if _send_to_grpc_worker(name, message, "manager"):
        return True
    _sync_worker_manager()
    return worker_manager.send(name, message, chat_id)


def _fetch_remote_file(host: str, remote_path: str) -> Optional[str]:
    """Fetch a file from a remote host via rsync to a local temp path.

    Returns local temp path on success, None on failure.
    Preserves the original filename so Telegram displays it correctly.
    """
    original_name = Path(remote_path).name
    tmp_dir = tempfile.mkdtemp(prefix="remote-file-")
    local_path = os.path.join(tmp_dir, original_name)
    try:
        r = subprocess.run(
            ["rsync", "-az", f"{host}:{remote_path}", local_path],
            capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.getsize(local_path) > 0:
            return local_path
    except Exception as e:
        print(f"Remote file fetch failed: {host}:{remote_path} -> {e}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _localize_media(name: str, media_list: list) -> list:
    """For teleported workers, fetch remote files to local temp paths.

    Always fetches from remote for teleported workers, even if a local file
    with the same path exists (e.g., /tmp/raw.png) — the remote file is the
    correct one.
    """
    host = get_worker_host(name)
    if not host:
        return media_list
    result = []
    for file_path, caption in media_list:
        local = _fetch_remote_file(host, file_path)
        if local:
            result.append((local, caption))
        else:
            print(f"Cannot fetch remote file {host}:{file_path} for {name}")
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

    # Still support explicit [[speak:custom text]] tag for custom voice text
    speak_text = None
    speak_match = re.search(r'\[\[speak(?::([^\]]*))?\]\]', clean_text)
    if speak_match:
        custom = speak_match.group(1)
        clean_text = clean_text[:speak_match.start()] + clean_text[speak_match.end():]
        clean_text = clean_text.strip()
        if custom is not None and custom.strip():
            speak_text = custom.strip()

    # For teleported workers, fetch remote files to local temp paths
    images = _localize_media(name, images)
    files = _localize_media(name, files)

    # Auto-TTS: synthesize voice for every response when enabled (/voice on|off)
    # Use explicit [[speak:text]] if provided, otherwise use the clean response text
    if speak_text is None and TTS_ENDPOINT and state.get("tts_enabled", True):
        speak_text = clean_text  # raw text before HTML conversion

    clean_text = markdown_to_telegram_html(clean_text)

    # Debug: log when sending very short text (helps trace empty "name:" messages)
    if clean_text and len(clean_text.strip()) <= 5:
        print(f"{log_prefix} DEBUG short msg: {name}, text={repr(clean_text)}, "
              f"images={len(images)}, files={len(files)}")

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

            result = transport.send_text(
                chat_id, part, parse_mode="HTML",
                reply_to=prev_msg_id if prev_msg_id else None
            )
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
                    result = transport.send_text(
                        chat_id, plain_text,
                        reply_to=prev_msg_id if prev_msg_id else None
                    )
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
            transport.send_text(chat_id, f"{name}: [Image failed: {img_path}]")

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
            transport.send_text(chat_id, f"{name}: [File failed: {file_path}]")

    # Auto-TTS: synthesize and send voice alongside text
    # Skip TTS for messages >1000 chars. Split into paragraphs for separate voice messages.
    if speak_text is not None and speak_text and len(speak_text) <= 1000:
        # Split into paragraphs (double newline), filter empty
        paragraphs = [p.strip() for p in speak_text.split('\n\n') if p.strip()]
        if not paragraphs:
            paragraphs = [speak_text]
        def _tts_and_send():
            try:
                for i, para in enumerate(paragraphs):
                    print(f"TTS starting: {len(para)} chars for {name} (part {i+1}/{len(paragraphs)})")
                    voice_path = synthesize_speech(para)
                    if voice_path:
                        send_voice(chat_id, voice_path, caption=f"{name}:")
                        try:
                            os.unlink(voice_path)
                        except OSError:
                            pass
            except Exception as e:
                print(f"TTS thread error: {e}")
        # Run TTS in background thread to not block /response return
        threading.Thread(target=_tts_and_send, daemon=True).start()


def handle_grpc_worker_response(name: str, text: str, payload: bytes = b""):
    """Route a gRPC worker response through the same Telegram path as hooks."""
    try:
        if not name or not text:
            print(f"gRPC response ignored: missing worker name or text")
            return

        chat_id_file = get_chat_id_file(name)
        if not chat_id_file.exists():
            print(f"gRPC response: no chat_id for session '{name}'")
            return

        chat_id = chat_id_file.read_text().strip()
        print(f"gRPC response: {name} -> chat {chat_id} ({len(text)} chars)")

        if payload:
            try:
                data = json.loads(payload.decode("utf-8"))
                session_id = data.get("session_id", "")
                if session_id:
                    sid_file = ensure_session_dir(name) / "claude_session_id"
                    old_sid = sid_file.read_text().strip() if sid_file.exists() else ""
                    if old_sid != session_id:
                        sid_file.write_text(session_id)
                        sid_file.chmod(0o600)
            except Exception as e:
                print(f"gRPC response payload ignored for '{name}': {e}")

        send_response_to_telegram(name, text, int(chat_id), log_prefix="gRPC response")
        _check_learning_reminder(name)
        clear_pending(name)
        mark_hook_event(name)
    except Exception as e:
        print(f"gRPC response error for '{name}': {e}")


def handle_grpc_worker_register(name: str, host: str, version: str, tools: dict):
    tool_names = ", ".join(sorted(tools.keys())) if tools else "none"
    host_label = host or "unknown-host"
    version_label = version or "unknown-version"
    print(f"gRPC worker registered: {name} ({host_label}, {version_label}, tools: {tool_names})")


def _beast_serve_deploy(html_path: str, slug: str) -> str | None:
    """Deploy an HTML file via beast serve and return the public URL, or None on failure."""
    try:
        r = subprocess.run(
            ["beast", "serve", "deploy", html_path, "--slug", slug, "--output-json"],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            url = data.get("url", "")
            if url and "localhost" in url:
                host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "157.180.48.254"
                url = url.replace("localhost", host)
            return url or None
    except Exception as e:
        print(f"beast serve deploy failed for {slug}: {e}")
    return None


def handle_grpc_worker_disconnect(name: str):
    print(f"gRPC worker disconnected: {name}")


def handle_grpc_jsonl_received(stream_id: str, data: bytes):
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", stream_id or "stream")
    jsonl_dir = SESSIONS_DIR / "grpc-jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    jsonl_dir.chmod(0o700)
    path = jsonl_dir / f"{safe_id}.jsonl"
    path.write_bytes(data)
    path.chmod(0o600)
    print(f"gRPC JSONL received: {stream_id or safe_id} -> {path} ({len(data)} bytes)")


def _merge_grpc_workers(workers: list) -> list:
    """Include connected gRPC workers in /workers without changing tmux entries."""
    if grpc_server is None:
        return workers

    try:
        connected = grpc_server.get_connected_workers()
    except Exception as e:
        print(f"gRPC worker list unavailable: {e}")
        return workers

    existing = {worker.get("name") for worker in workers}
    for worker in workers:
        if worker.get("name") in connected:
            worker["grpc_connected"] = True

    for name in connected:
        if name in existing:
            continue
        workers.append({
            "name": name,
            "machine": "",
            "protocol": "grpc",
            "address": f"{BRIDGE_BIND}:{GRPC_PORT}",
            "grpc_connected": True,
            "note": "Connected over gRPC MessageStream. Manager messages route through the gRPC stream.",
        })

    return workers


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
        transport.send_chat_action(chat_id, "typing")
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
        transport.send_text(chat_id, "Going offline briefly. Your team stays the same.")
    print("Shutdown notifications sent")


# ============================================================
# NON-CORE: CommandRouter
# ============================================================

class _LegacyTransportAdapter(MessageTransport):
    """Wraps legacy TelegramAPI-style objects (with send_message/set_reaction)
    for backward compat with tests that pass FakeTelegram to CommandRouter."""

    def __init__(self, legacy):
        self._legacy = legacy

    @property
    def name(self) -> str:
        return "legacy-adapter"

    def send_text(self, chat_id, text, parse_mode=None, reply_to=None) -> dict | None:
        result = self._legacy.send_message(chat_id, text)
        return result if result else {"ok": True, "result": {"message_id": 1}}

    def send_photo(self, chat_id, photo_path, caption=None) -> bool:
        return False

    def send_document(self, chat_id, doc_path, caption=None) -> bool:
        return False

    def send_animation(self, chat_id, animation_path, caption=None) -> bool:
        return False

    def send_video(self, chat_id, video_path, caption=None) -> bool:
        return False

    def send_audio(self, chat_id, audio_path, caption=None) -> bool:
        return False

    def send_voice(self, chat_id, voice_path, caption=None) -> bool:
        return False

    def send_sticker(self, chat_id, sticker_path) -> bool:
        return False

    def send_chat_action(self, chat_id, action) -> None:
        pass

    def set_reaction(self, chat_id, message_id, reaction) -> None:
        if hasattr(self._legacy, 'set_reaction'):
            self._legacy.set_reaction(chat_id, message_id, reaction)

    def edit_message(self, chat_id, message_id, text, parse_mode=None) -> dict | None:
        return {"ok": True, "result": {"message_id": message_id}}

    def setup_commands(self, commands) -> None:
        pass

    def download_file(self, file_id, session_name) -> str | None:
        return None


class CommandRouter:
    def __init__(self, transport, workers: WorkerManager):
        # Accept MessageTransport or legacy TelegramAPI-style objects (for test compat)
        if transport is not None and not isinstance(transport, MessageTransport):
            transport = _LegacyTransportAdapter(transport)
        self.transport = transport
        self.workers = workers
        # Restart-all state
        self._restart_all_lock = threading.Lock()
        self._restart_all_running = False
        self._restart_all_abort = threading.Event()
        self._restart_all_thread = None

    def reply(self, chat_id, text, outcome=None):
        if self.transport is not None:
            self.transport.send_text(chat_id, text)

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

                download_target = self._resolve_media_target(text, msg)
                local_path = download_telegram_file(file_id, download_target)
                if local_path:
                    gif_text = f"Manager sent GIF: `{local_path}`"
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

                download_target = self._resolve_media_target(text, msg)
                local_path = download_telegram_file(file_id, download_target)
                if local_path:
                    image_text = f"Manager sent image: `{local_path}`"
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

                download_target = self._resolve_media_target(text, msg)
                local_path = download_telegram_file(file_id, download_target)
                if local_path:
                    file_name = document.get("file_name", "unknown")
                    file_size = document.get("file_size", 0)
                    mime_type = document.get("mime_type", "unknown")
                    size_str = format_file_size(file_size)
                    file_text = f"Manager sent file: {file_name} ({size_str}, {mime_type})\nPath: `{local_path}`"
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

                download_target = self._resolve_media_target(text, msg)
                local_path = download_telegram_file(file_id, download_target)
                if local_path:
                    if audio:
                        title = audio.get("title", audio.get("file_name", "audio"))
                        duration = audio.get("duration", 0)
                        media_text = f"Manager sent audio: {title} ({duration}s)\nPath: `{local_path}`"
                    elif voice:
                        duration = voice.get("duration", 0)
                        transcript = transcribe_voice(local_path)
                        if transcript:
                            self.reply(chat_id, f"🎤 _{transcript}_", msg_id)
                            if text:
                                self._route_media_message(f"{text}\n\n{transcript}", text, chat_id, msg_id, msg=msg)
                            else:
                                self._route_media_message(transcript, transcript, chat_id, msg_id, msg=msg)
                            return
                        else:
                            media_text = f"Manager sent voice message: ({duration}s)\nPath: `{local_path}`"
                    elif video:
                        duration = video.get("duration", 0)
                        file_name = video.get("file_name", "video")
                        media_text = f"Manager sent video: {file_name} ({duration}s)\nPath: `{local_path}`"
                    elif video_note:
                        duration = video_note.get("duration", 0)
                        media_text = f"Manager sent video note: ({duration}s)\nPath: `{local_path}`"
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
        elif cmd == "/voice":
            return self.cmd_voice(arg, chat_id)
        elif cmd == "/pilot":
            return self.cmd_pilot(arg, chat_id)
        elif cmd == "/rewind":
            return self.cmd_rewind(arg, chat_id)
        elif cmd == "/pr":
            return self.cmd_pr_review(arg, chat_id)
        elif cmd == "/memory":
            return self.cmd_memory(arg, chat_id)
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
                self.transport.send_text(chat_id, f"Now talking to {worker_name.capitalize()}.")
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
            self.reply(chat_id, "Usage: /pilot <name> [name2 ...]", outcome="Needs decision")
            return True
        names = name.lower().strip().split()
        prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
        pilot_port = os.environ.get("PILOT_PORT", "10170")
        import urllib.request, json as _json, time as _time
        from urllib.parse import urlparse, quote as _urlquote
        if "all" in names:
            registered = worker_manager.scan_tmux_sessions()
            registry = _load_registry()
            for rname, rinfo in registry.get("workers", {}).items():
                if rinfo.get("host") and rname not in registered:
                    registered[rname] = {"tmux": f"{prefix}{rname}", "host": rinfo["host"]}
            if not registered:
                self.reply(chat_id, "No active workers found", outcome="Needs decision")
                return True
            names = sorted(registered.keys())
        enabled = []
        session_names = []
        errors = []
        for n in names:
            session_name = f"{prefix}{n}" if not n.startswith("claude-") else n
            try:
                worker_host = get_worker_host(n)
                url = f"http://localhost:{pilot_port}/api/pilot?session={session_name}"
                if worker_host:
                    url += f"&host={_urlquote(worker_host)}"
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    _json.loads(resp.read())
                enabled.append(n)
                session_names.append(session_name)
            except Exception as e:
                errors.append(f"{n}: {e}")
        if not enabled:
            self.reply(chat_id, f"Pilot error: {'; '.join(errors)}", outcome="Needs decision")
            return True
        ts = _time.strftime("%m%d-%H%M")
        if len(enabled) <= 3:
            slug = "-".join(enabled) + "-" + ts
        else:
            slug = f"team{len(enabled)}-{ts}"
        try:
            payload = _json.dumps({"slug": slug, "sessions": session_names, "ttl": 300}).encode()
            req = urllib.request.Request(
                f"http://localhost:{pilot_port}/api/grid-session",
                data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                _json.loads(resp.read())
        except Exception:
            pass
        host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "localhost"
        pilot_url = f"http://{host}:{pilot_port}/grid/{_urlquote(slug)}"
        names_str = ", ".join(enabled)
        msg = f"✈️ Pilot: {names_str} (5min)\n{pilot_url}"
        if errors:
            msg += f"\n⚠️ Failed: {'; '.join(errors)}"
        self.reply(chat_id, msg)
        return True

    def cmd_rewind(self, name, chat_id):
        if not name:
            self.reply(chat_id, "Usage: /rewind <name>\n/rewind team — view team chat", outcome="Needs decision")
            return True
        name = name.lower().strip()
        import secrets, time as _time
        token = secrets.token_urlsafe(32)
        base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
        # Team chat viewer
        if name in ("team", "--team"):
            REWIND_TOKENS[token] = {"name": "__team__", "expires_at": _time.time() + REWIND_TIMEOUT}
            url = f"{base_url}/team-chat?token={token}"
            try:
                html_content = _render_team_chat_html(per_page=200, token=token)
                snap_path = "/tmp/rewind-team.html"
                with open(snap_path, "w") as f:
                    f.write(html_content)
                serve_url = _beast_serve_deploy(snap_path, "rewind-team")
                if serve_url:
                    self.reply(chat_id, f"\U0001f4ac Team chat\n{serve_url}\n\nLive (5min): {url}")
                    return True
            except Exception as e:
                print(f"Team chat snapshot deploy failed: {e}")
            self.reply(chat_id, f"\U0001f4ac Team chat (5min)\n{url}")
            return True
        REWIND_TOKENS[token] = {"name": name, "expires_at": _time.time() + REWIND_TIMEOUT}
        url = f"{base_url}/transcript/{name}?token={token}"
        try:
            html_content = _render_transcript_html(name, per_page=200, token=token)
            snap_path = f"/tmp/rewind-{name}.html"
            with open(snap_path, "w") as f:
                f.write(html_content)
            serve_url = _beast_serve_deploy(snap_path, f"rewind-{name}")
            if serve_url:
                self.reply(chat_id, f"⏪ Rewind for {name}\n{serve_url}\n\nLive (5min): {url}")
                return True
        except Exception as e:
            print(f"Rewind snapshot deploy failed for {name}: {e}")
        self.reply(chat_id, f"⏪ Rewind for {name} (5min)\n{url}")
        return True

    def cmd_pr_review(self, arg, chat_id):
        if not arg:
            self.reply(chat_id, "Usage: /pr <github_pr_url>\nExample: /pr https://github.com/BasedHardware/omi/pull/6426", outcome="Needs decision")
            return True
        arg = arg.strip()
        # Parse PR URL (supports #issuecomment-XXXXX fragments)
        clean_url = arg.split('#')[0]
        m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', clean_url)
        if not m:
            # Try bare number (assume BasedHardware/omi)
            try:
                pr_num = int(clean_url)
                owner, repo = 'BasedHardware', 'omi'
            except ValueError:
                self.reply(chat_id, "Invalid PR URL. Example: /pr https://github.com/BasedHardware/omi/pull/6426", outcome="Needs decision")
                return True
        else:
            owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))

        self.reply(chat_id, f"Generating PR review for {owner}/{repo}#{pr_num}...")

        # Run pr-review.py — pass full URL (with fragment) so it can highlight linked comment
        script_path = Path(__file__).parent / "pr-review.py"
        out_path = f"/tmp/pr-review-{pr_num}.html"
        try:
            r = subprocess.run(
                [sys.executable, str(script_path), arg, "--no-serve"],
                capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(out_path):
                self.reply(chat_id, f"Failed to generate PR review:\n{r.stderr[:500]}", outcome="Needs decision")
                return True
        except subprocess.TimeoutExpired:
            self.reply(chat_id, "PR review generation timed out (>300s).", outcome="Needs decision")
            return True

        slug = f"pr-{pr_num}"
        serve_url = _beast_serve_deploy(out_path, slug)
        if serve_url:
            self.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{serve_url}")
        else:
            import secrets, time as _time
            token = secrets.token_urlsafe(32)
            PR_REVIEW_TOKENS[token] = {"pr_num": pr_num, "owner": owner, "repo": repo, "expires_at": _time.time() + 300}
            base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
            url = f"{base_url}/pr-review/{pr_num}?token={token}"
            self.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{url}")
        return True

    def cmd_memory(self, query, chat_id):
        """Search team chat memory. /memory <query> [--agent X] [--days N] [--from X]"""
        if not query:
            self.reply(chat_id,
                       "Usage: /memory <query>\n"
                       "Examples:\n"
                       "  /memory what did I tell kai about auth\n"
                       "  /memory PR 6377 --agent taro\n"
                       "  /memory OTP problem --days 30\n"
                       "  /memory update  (re-index latest export)\n"
                       "  /memory status  (stack health)\n"
                       "  /memory wake-up [wing]  (L0+L1 context)\n"
                       "  /memory recall --wing=X [--room=Y]",
                       outcome="Needs decision")
            return True

        # Subcommand: /memory update — trigger incremental ingest
        if query.strip().lower() == "update":
            return self._memory_update(chat_id)

        # Subcommand: /memory status — memory stack health
        if query.strip().lower() == "status":
            try:
                from team_memory.memory_stack import MemoryStack
                stack = MemoryStack()
                info = stack.status()
                wings = info.get("wing_distribution", {})
                wing_str = ", ".join(f"{w}: {n}" for w, n in sorted(wings.items(), key=lambda x: -x[1]))
                lines = [
                    "Memory Stack Status:",
                    f"  Chunks: {info.get('total_chunks', 0)} ({wing_str})",
                    f"  Messages: {info.get('total_messages', 0)}",
                    f"  Summaries: {info.get('total_summaries', 0)}",
                    f"  L0 identity: {info['L0_identity']['tokens']} tokens ({info['L0_identity']['agents']} agents, {info['L0_identity']['projects']} projects, {info['L0_identity']['wings']} wings)",
                    f"  L1 essential: last 7 days, top 15 items",
                ]
                self.reply(chat_id, "\n".join(lines))
            except Exception as e:
                self.reply(chat_id, f"Memory status failed: {e}")
            return True

        # Subcommand: /memory wake-up [wing] — L0+L1 wake-up text
        if query.strip().lower().startswith("wake-up"):
            try:
                from team_memory.memory_stack import MemoryStack
                stack = MemoryStack()
                parts = query.strip().split()
                wing = parts[1] if len(parts) > 1 else None
                text = stack.wake_up(wing=wing)
                if len(text) > 4000:
                    text = text[:3997] + "..."
                self.reply(chat_id, text)
            except Exception as e:
                self.reply(chat_id, f"Memory wake-up failed: {e}")
            return True

        # Subcommand: /memory recall --wing=X --room=Y — L2 on-demand
        if query.strip().lower().startswith("recall"):
            try:
                from team_memory.memory_stack import MemoryStack
                stack = MemoryStack()
                wing = room = None
                for part in query.split():
                    if part.startswith("--wing="):
                        wing = part.split("=", 1)[1]
                    elif part.startswith("--room="):
                        room = part.split("=", 1)[1]
                text = stack.recall(wing=wing, room=room)
                if len(text) > 4000:
                    text = text[:3997] + "..."
                self.reply(chat_id, text)
            except Exception as e:
                self.reply(chat_id, f"Memory recall failed: {e}")
            return True

        self.reply(chat_id, "Searching memory...")

        try:
            from team_memory.search import search_memory
            result = search_memory(query)
        except Exception as e:
            self.reply(chat_id, f"Memory search failed: {e}", outcome="Needs decision")
            return True

        answer = result.get("answer", "")
        sources = result.get("results", [])

        if not answer and not sources:
            self.reply(chat_id, "No results found.")
            return True

        # Format response per spec
        lines = []
        if answer:
            lines.append(f"\U0001f9e0 {answer}")
        else:
            lines.append("\U0001f9e0 No direct answer found.")

        if sources:
            lines.append("")
            # Generate a single rewind token for all source links
            import secrets, time as _time
            tc_token = secrets.token_urlsafe(32)
            REWIND_TOKENS[tc_token] = {"name": "__team__", "expires_at": _time.time() + REWIND_TIMEOUT}
            base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"

            lines.append("\U0001f4ce Sources:")
            for i, r in enumerate(sources[:3], 1):
                # Show first 2 lines of chunk, truncated
                chunk_lines = r.get("text", "").split("\n")
                preview = "\n".join(chunk_lines[:2])
                if len(preview) > 200:
                    preview = preview[:197] + "..."
                # Deep link to team chat page
                source_link = ""
                chunk_id = r.get("_id", "")
                if chunk_id.startswith("tg_"):
                    parts = chunk_id.split("_")
                    if len(parts) >= 2:
                        try:
                            first_msg_id = int(parts[1])
                            page_info = _run_team_chat_query("page-for-msg", msg_id=first_msg_id)
                            if page_info and page_info.get("page"):
                                source_link = f"\n{base_url}/team-chat?token={tc_token}&page={page_info['page']}#msg-{first_msg_id}"
                        except (ValueError, IndexError):
                            pass
                lines.append(f"{i}. {preview}{source_link}")

        self.reply(chat_id, "\n".join(lines))
        return True

    def _memory_update(self, chat_id):
        """Run incremental ingest from latest export in ~/team/exports/."""
        import glob as _glob
        exports_dir = os.path.expanduser("~/team/exports")
        zips = sorted(_glob.glob(os.path.join(exports_dir, "ChatExport_*.json.zip")))
        if not zips:
            self.reply(chat_id, f"No exports found in {exports_dir}/", outcome="Needs decision")
            return True

        latest = zips[-1]
        self.reply(chat_id, f"Indexing from {os.path.basename(latest)}...")

        try:
            # Parse
            parse_script = str(Path(__file__).parent / "team_memory" / "parse.py")
            parsed_path = "/tmp/team-memory-parsed-full.jsonl"
            r = subprocess.run(
                [sys.executable, parse_script, "--zip", latest, "--out", parsed_path],
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                self.reply(chat_id, f"Parse failed: {r.stderr[:300]}", outcome="Needs decision")
                return True

            # Ingest (incremental)
            ingest_script = str(Path(__file__).parent / "team_memory" / "ingest.py")
            r = subprocess.run(
                [sys.executable, ingest_script, parsed_path, "--incremental"],
                capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                self.reply(chat_id, f"Ingest failed: {r.stderr[:300]}", outcome="Needs decision")
                return True

            # Extract stats from output
            lines = r.stdout.strip().split("\n")
            summary = lines[-1] if lines else "Done"
            self.reply(chat_id, f"Memory updated.\n{summary}")

        except subprocess.TimeoutExpired:
            self.reply(chat_id, "Memory update timed out.", outcome="Needs decision")
        except Exception as e:
            self.reply(chat_id, f"Memory update failed: {e}", outcome="Needs decision")
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

        # Parse flags
        clean = False
        force = False
        tokens = args.split()
        remaining = []
        for t in tokens:
            if t == "--clean":
                clean = True
            elif t == "--force":
                force = True
            else:
                remaining.append(t)
        name_arg = remaining[0].lower() if remaining else ""

        # Branch: /restart cancel
        if name_arg == "cancel":
            return self._cmd_restart_cancel(chat_id)

        # Branch: /restart all [--clean]
        if name_arg == "all":
            return self._cmd_restart_all(chat_id, clean)

        # Branch: /restart name1 name2 name3 ... (multi-worker restart)
        if len(remaining) > 1:
            names = [n.lower() for n in remaining]
            self.reply(chat_id, f"Restarting {len(names)} workers: {', '.join(names)}...")
            for n in names:
                self.cmd_restart(chat_id, f"{'--clean ' if clean else ''}{'--force ' if force else ''}{n}")
            return True

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

        # Guard: skip restart if worker is already running (unless --force)
        host = get_worker_host(name)
        tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}") if session else f"{self.workers.tmux_prefix}{name}"
        print(f"[cmd_restart] {name}: force={force}, clean={clean}, host={host}, tmux={tmux_name}")
        tmux_alive = tmux_exists(tmux_name, host=host)
        claude_running = is_claude_running(tmux_name, host=host) if tmux_alive else False
        print(f"[cmd_restart] {name}: tmux_alive={tmux_alive}, claude_running={claude_running}")
        if not force and tmux_alive and claude_running:
            print(f"[cmd_restart] {name}: BLOCKED (already running)")
            self.reply(chat_id, f"{name.capitalize()} is already running. Use /restart --force {name} to force.")
            return True

        # In-flight dedupe: block if a restart is already in progress (even with --force)
        with _restart_lock:
            inflight_ts = _restart_in_progress.get(name)
            if inflight_ts and time.time() - inflight_ts < 120:
                print(f"[cmd_restart] {name}: BLOCKED (restart in progress since {time.time() - inflight_ts:.0f}s ago)")
                self.reply(chat_id, f"{name.capitalize()} restart already in progress. Wait for it to finish.")
                return True
            _restart_in_progress[name] = time.time()

        try:
            result = self._do_restart(name, session, chat_id, host, tmux_name, force, clean)
            if force:
                _force_restart_pending_cwd[name] = True
            return result
        finally:
            with _restart_lock:
                _restart_in_progress.pop(name, None)

    def _do_restart(self, name, session, chat_id, host, tmux_name, force, clean):
        """Execute restart after in-flight guard. Called from cmd_restart."""
        # Teleported worker: delegate to remote restart
        if host:
            mode = "relaunch" if clean else "resume"
            backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)
            resume_id = get_claude_session_id(name, authoritative=True) if mode == "resume" else ""
            target_cwd = get_claude_session_cwd(name)
            print(f"[cmd_restart] {name}: remote restart mode={mode}, resume_id={resume_id}, cwd={target_cwd}")
            self.reply(chat_id, f"Restarting {name.capitalize()} on remote host...")
            ok, err = self._restart_remote_worker(
                name, backend_name, backend_obj, tmux_name, host, mode)
            _recent_restarts[name] = time.time()
            print(f"[cmd_restart] {name}: remote restart result ok={ok}, err={err}")
            if ok:
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

        # Remap $HOME if CWD still has the VPS path (e.g., /home/claude/...)
        # This happens when remote session files have a stale VPS CWD.
        if target_cwd and host:
            local_home = os.path.expanduser("~")
            if target_cwd.startswith(local_home):
                r_home = _remote_run(
                    ["bash", "-c", "echo $HOME"], host=host,
                    capture_output=True, text=True, timeout=5)
                remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
                if remote_home and remote_home != local_home:
                    target_cwd = remote_home + target_cwd[len(local_home):]

        print(f"[_restart_remote] {name}: mode={mode}, host={host}, tmux={tmux_name}, cwd={target_cwd}")
        if mode == "resume":
            resume_id = get_claude_session_id(name, authoritative=True)
            print(f"[_restart_remote] {name}: resume_id={resume_id}")
        else:
            # Clear session IDs for relaunch
            session_dir = SESSIONS_DIR / name
            session_dir.mkdir(parents=True, exist_ok=True)
            cleared = list(session_dir.glob("*_session_id"))
            for f in cleared:
                f.unlink()
            _clear_hook_failures(name)
            print(f"[_restart_remote] {name}: cleared {len(cleared)} session files for relaunch")

        # Stop the remote Claude process if tmux is still alive
        if tmux_exists(tmux_name, host=host):
            print(f"[_restart_remote] {name}: stopping remote tmux {tmux_name}")
            self._stop_worker_for_teleport(name, tmux_name, host=host)
            # Kill tmux — _start_worker_on_target creates a fresh one
            _remote_run(["tmux", "kill-session", "-t", tmux_name],
                         host=host, capture_output=True)
            time.sleep(0.5)
        else:
            print(f"[_restart_remote] {name}: tmux {tmux_name} not found on {host}")

        # Re-read session_id (hook may have updated during /exit)
        if mode == "resume":
            resume_id = get_claude_session_id(name, authoritative=True) or resume_id
            print(f"[_restart_remote] {name}: post-stop resume_id={resume_id}")

        # Validate session is resumable on target before attempting --resume
        # Claude stores sessions under ~/.claude/projects/-<cwd-dashes>/<session_id>.jsonl
        # If the file doesn't exist on the target, --resume will fail immediately
        if resume_id and target_cwd and host:
            # Build the project dir path on the remote host
            r_home = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                                  capture_output=True, text=True, timeout=5)
            remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
            if remote_home:
                # Claude Code project dir: ~/.claude/projects/-<cwd with / replaced by->
                cwd_slug = target_cwd.replace("/", "-")
                session_file = f"{remote_home}/.claude/projects/{cwd_slug}/{resume_id}.jsonl"
                check = _remote_run(["test", "-f", session_file], host=host,
                                     capture_output=True, timeout=5)
                if check.returncode != 0:
                    print(f"[_restart_remote] {name}: session {resume_id} NOT found at {session_file}, starting fresh")
                    resume_id = ""
                    # Clear stale session ID
                    session_dir = SESSIONS_DIR / name
                    session_dir.mkdir(parents=True, exist_ok=True)
                    for f in session_dir.glob("*_session_id"):
                        f.unlink()
                else:
                    print(f"[_restart_remote] {name}: session {resume_id} validated at {session_file}")

        # Delegate to existing remote start flow
        # skip_session_sync=True: worker was already on this host, target session files are authoritative
        print(f"[_restart_remote] {name}: calling _start_worker_on_target(cwd={target_cwd}, resume={resume_id}, backend={backend_name})")
        ok = self._start_worker_on_target(
            name, host, target_cwd, resume_id, backend_name, skip_session_sync=True)
        if not ok:
            print(f"[_restart_remote] {name}: _start_worker_on_target FAILED")
            return False, f"Failed to restart {name} on {host}"

        # Wait for Claude to actually start before sending welcome
        welcome = self.workers._build_welcome(name, backend)
        if backend.is_interactive:
            started = False
            for _ in range(10):
                time.sleep(1.0)
                if is_claude_running(tmux_name, host=host):
                    started = True
                    break
            if started:
                self.workers.send(name, welcome)
            else:
                print(f"[_restart_remote] {name}: Claude did not start within 10s, skipping welcome")

        print(f"[_restart_remote] {name}: restarted successfully (mode={mode})")
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
        delay_s = 7
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
            print(f"[teleport] {name}: source_host={source_host}, source_cwd={source_cwd}, target_host={target_host}, target_cwd={target_cwd}")
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

            # Expand ~ in target_cwd to remote $HOME
            if target_cwd and target_cwd.startswith("~") and target_host:
                r_home = _remote_run(
                    ["bash", "-c", "echo $HOME"], host=target_host,
                    capture_output=True, text=True, timeout=5)
                remote_home = r_home.stdout.strip() if r_home.returncode == 0 else ""
                if remote_home:
                    target_cwd = remote_home + target_cwd[1:]
            elif target_cwd and target_cwd.startswith("~"):
                target_cwd = os.path.expanduser(target_cwd)

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
            print(f"[teleport] {name}: stopped, session_id={session_id}")

            if source_cwd and target_cwd:
                self._teleport_notify(chat_id, f"Syncing working directory...")
                print(f"[teleport] {name}: syncing {source_cwd} → {target_cwd}")
                ok = self._sync_working_directory(
                    source_cwd, target_cwd, source_host, target_host, full_sync)
                print(f"[teleport] {name}: working dir sync ok={ok}")
                if not ok:
                    self._teleport_rollback(name, tmux_name, source_host, source_cwd,
                                            session_id, backend_name, chat_id,
                                            "working directory sync failed")
                    return

            if session_id:
                self._teleport_notify(chat_id, "Syncing session transcript...")
                self._sync_session_transcript(
                    session_id, source_cwd, target_cwd, source_host, target_host)
                print(f"[teleport] {name}: transcript sync done")

            # On teleport out: push team configs + install hooks on target
            # On teleback: skip — VPS is source of truth for team-scope config
            if not is_teleback:
                self._teleport_notify(chat_id, "Syncing team config and hooks...")
                self._sync_shared_repos(target_host, chat_id)
                self._install_hooks_on_target(target_host)
                print(f"[teleport] {name}: team config + hooks synced")

            # ── PHASE 2: Commit ──

            state_file.write_text(json.dumps({
                "phase": 2, "source_host": source_host,
                "target_host": target_host, "target_cwd": target_cwd,
                "started_at": int(time.time()),
            }))

            # Save remapped CWD BEFORE _start_worker_on_target so that
            # _sync_session_files_to_target copies the correct (remapped) path
            # to the target machine, not the stale VPS path.
            save_claude_session_cwd(name, target_cwd)

            # Clear local session ID cache — the target may create a new session
            # (e.g., different project, expired session). Without clearing, VPS
            # returns the stale ID instead of SSH-fetching the real one from target.
            # The hook's /response POST will repopulate it on first response.
            clear_claude_session_id(name)

            self._teleport_notify(chat_id,
                f"Starting {name} on {target_host or 'local'}...")
            print(f"[teleport] {name}: calling _start_worker_on_target(target_cwd={target_cwd}, session_id={session_id}, backend={backend_name})")
            ok = self._start_worker_on_target(
                name, target_host, target_cwd, session_id, backend_name)
            print(f"[teleport] {name}: _start_worker_on_target returned {ok}")
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
        session_id = get_claude_session_id(name, authoritative=True)

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
        return get_claude_session_id(name, authoritative=True) or session_id

    def _sync_working_directory(self, source_cwd, target_cwd,
                                 source_host=None, target_host=None,
                                 full=False):
        """Sync working directory from source to target.

        Prefers git-based sync (fast, delta-only) for git repos.
        Falls back to rsync for non-git dirs or on git failure.
        Use full=True to force rsync (skip git entirely).
        """
        if not full and _is_git_repo(source_cwd, host=source_host):
            project = _get_project_name(source_cwd, host=source_host)
            if project:
                try:
                    bare_repo = _ensure_bare_repo(project)
                    meta = _git_push_state(source_cwd, project, bare_repo,
                                           host=source_host)
                    if meta:
                        bare_url = _bare_repo_url(bare_repo, target_host=target_host)
                        if _git_pull_state(target_cwd, project, bare_url, meta,
                                           host=target_host):
                            print(f"[teleport] git sync succeeded for {project}")
                            return True
                        print(f"[teleport] git pull failed, falling back to rsync")
                    else:
                        print(f"[teleport] git push failed, falling back to rsync")
                except Exception as e:
                    print(f"[teleport] git sync error, falling back to rsync: {e}")

        return self._rsync_working_directory(
            source_cwd, target_cwd, source_host, target_host, full)

    def _rsync_working_directory(self, source_cwd, target_cwd,
                                  source_host=None, target_host=None,
                                  full=False):
        """rsync working directory from source to target (fallback path)."""
        _remote_run(["mkdir", "-p", target_cwd],
                     host=target_host, capture_output=True)

        cmd = ["rsync", "-az", "--delete"]
        gitignore_tmpfile = None
        if not full:
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
                try:
                    subprocess.run(
                        ["git", "-C", repo_path, "add", "-A"],
                        capture_output=True, timeout=10)
                    subprocess.run(
                        ["git", "-C", repo_path, "commit", "-m",
                         f"teleport sync: {repo_name}"],
                        capture_output=True, timeout=10)
                    subprocess.run(
                        ["git", "-C", repo_path, "push", "origin", "master"],
                        capture_output=True, timeout=60)
                    print(f"[teleport] git sync succeeded for {repo_name}")
                except subprocess.TimeoutExpired:
                    print(f"[teleport] git sync timed out for {repo_name}, continuing")

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
        """Copy Claude credentials to target if target has no valid token.

        ~/.claude/.credentials.json has the actual access/refresh tokens.
        Without it, Claude starts unauthenticated on the target machine.
        Skip if target already has a valid (non-expired) token with a DIFFERENT
        refresh token — means target was logged in independently.
        """
        local_creds = os.path.expanduser("~/.claude/.credentials.json")
        if not os.path.exists(local_creds):
            return

        # Check if target already has valid credentials with a different token
        try:
            r = _remote_run(["cat", ".claude/.credentials.json"],
                             host=target_host, capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                remote_data = json.loads(r.stdout)
                local_data = json.loads(open(local_creds).read())
                remote_oauth = remote_data.get("claudeAiOauth", {})
                local_oauth = local_data.get("claudeAiOauth", {})
                remote_refresh = remote_oauth.get("refreshToken", "")
                local_refresh = local_oauth.get("refreshToken", "")
                remote_exp = remote_oauth.get("expiresAt", 0)
                now_ms = int(time.time() * 1000)
                # Skip if target has a DIFFERENT refresh token that hasn't expired
                if remote_refresh and remote_refresh != local_refresh and remote_exp > now_ms:
                    print(f"[creds] Target {target_host} has independent valid credentials, skipping sync")
                    return
        except Exception:
            pass  # Can't check — fall through to sync

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
        print(f"[creds] Synced credentials to {target_host}")

    def _start_worker_on_target(self, name, target_host, target_cwd,
                                 session_id, backend_name, skip_session_sync=False):
        """Create tmux session on target and start Claude Code with --resume."""
        tmux_name = f"{TMUX_PREFIX}{name}"

        # Clean up any leftover session
        _remote_run(["tmux", "kill-session", "-t", tmux_name],
                     host=target_host, capture_output=True)
        time.sleep(0.3)

        # Create new session
        r = _remote_run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            host=target_host, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[teleport] tmux new-session failed: rc={r.returncode} stderr={r.stderr[:200] if r.stderr else ''}")
            return False
        _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
                    host=target_host, capture_output=True)

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
        # Skip for remote restarts — worker was already on target, target files are authoritative
        if target_host and not skip_session_sync:
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

        print(f"[teleport] start_cmd={start_cmd}")
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
        for attempt in range(30):
            time.sleep(1)
            r = _remote_run(
                ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
                host=target_host, capture_output=True, text=True)
            if r.returncode != 0:
                if attempt % 10 == 0:
                    print(f"[teleport] verify attempt {attempt}: tmux display-message failed rc={r.returncode}")
                continue
            pane_pid = r.stdout.strip()
            claude_pid = _get_claude_pid(pane_pid, host=target_host) if pane_pid else None
            if claude_pid:
                print(f"[teleport] verified: claude running as pid {claude_pid} (pane {pane_pid})")
                return True
            if attempt % 10 == 0:
                print(f"[teleport] verify attempt {attempt}: pane_pid={pane_pid}, no claude yet")
                # Capture pane to see what's happening
                cap = _remote_run(
                    ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                    host=target_host, capture_output=True, text=True, timeout=5)
                if cap.returncode == 0:
                    print(f"[teleport] pane content: {cap.stdout[:300]}")
        print(f"[teleport] verify FAILED after 30 attempts")
        return False

    def _teleport_rollback(self, name, tmux_name, source_host, source_cwd,
                            session_id, backend_name, chat_id, reason):
        """Roll back a failed teleport by restarting on source."""
        self._teleport_notify(chat_id, f"Teleport failed: {reason}. Rolling back...")
        try:
            # Restore source CWD (may have been overwritten with target path)
            if source_cwd:
                save_claude_session_cwd(name, source_cwd)

            # Ensure tmux session exists on source
            if not tmux_exists(tmux_name, host=source_host):
                _remote_run(
                    ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
                    host=source_host, capture_output=True)
                _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
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
            transport.send_text(chat_id, text)
        except Exception:
            pass

    def cmd_voice(self, arg, chat_id):
        """Toggle auto-TTS for worker responses. /voice on|off or /voice to show status."""
        arg = arg.strip().lower()
        if arg == "on":
            state["tts_enabled"] = True
            self.reply(chat_id, "Voice mode ON — responses include voice messages.")
        elif arg == "off":
            state["tts_enabled"] = False
            self.reply(chat_id, "Voice mode OFF — text only.")
        else:
            status = "ON" if state["tts_enabled"] else "OFF"
            self.reply(chat_id, f"Voice mode: {status}\n/voice on — responses include voice\n/voice off — text only")
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
            # Will attempt transcription after download below
        elif sticker:
            file_id = sticker.get("file_id")
            media_label = f"sticker: {sticker.get('emoji', '')}"

        if not file_id:
            return None

        local_path = download_telegram_file(file_id, target_worker)
        if not local_path:
            return None

        if voice:
            transcript = transcribe_voice(local_path)
            if transcript:
                return transcript

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

    def _resolve_media_target(self, caption, msg):
        """Determine which worker's inbox to download media into.

        Uses same priority as _route_media_message: @mentions > reply-to > active.
        Returns the target worker name (or state["active"] as fallback).
        """
        if caption:
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                return targets[0]
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            return reply_worker
        return state["active"]

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
                self.transport.set_reaction(chat_id, msg_id, [{"type": "emoji", "emoji": "👀"}])


command_router = CommandRouter(transport, worker_manager)

# ============================================================
# TRANSCRIPT VIEWER
# ============================================================

# Background transcript sync tracking: {key: {status, progress, error, path, started}}
_TRANSCRIPT_SYNC = {}
_TRANSCRIPT_SYNC_LOCK = threading.Lock()

# Path to transcript-index.py script (same directory as bridge.py)
TRANSCRIPT_INDEX_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcript-index.py")
TEAM_CHAT_INDEX_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team-chat-index.py")
TEAM_CHAT_JSONL = "/tmp/team-memory-parsed-full.jsonl"
TEAM_CHAT_DB = "/tmp/team-chat-cache/team.db"
TEAM_CHAT_MEDIA_DIR = os.path.expanduser("~/team/exports/chat-full")


def _render_md_to_html(md_text):
    """Simple markdown to HTML renderer for file previews."""
    import re as _re, html as _html
    h = _html.escape(md_text)
    # Headers
    h = _re.sub(r'^######\s+(.+)$', r'<h6>\1</h6>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^#####\s+(.+)$', r'<h5>\1</h5>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^####\s+(.+)$', r'<h4>\1</h4>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^###\s+(.+)$', r'<h3>\1</h3>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^##\s+(.+)$', r'<h2>\1</h2>', h, flags=_re.MULTILINE)
    h = _re.sub(r'^#\s+(.+)$', r'<h1>\1</h1>', h, flags=_re.MULTILINE)
    # Code blocks (fenced)
    h = _re.sub(r'```[a-z]*\n(.*?)```', r'<pre class="md-code"><code>\1</code></pre>', h, flags=_re.DOTALL)
    # Inline code
    h = _re.sub(r'`([^`]+)`', r'<code class="md-inline">\1</code>', h)
    # Bold + italic
    h = _re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', h)
    h = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', h)
    h = _re.sub(r'\*(.+?)\*', r'<em>\1</em>', h)
    # Horizontal rule
    h = _re.sub(r'^---+$', r'<hr>', h, flags=_re.MULTILINE)
    # Unordered lists
    h = _re.sub(r'^[-*]\s+(.+)$', r'<li>\1</li>', h, flags=_re.MULTILINE)
    # Links
    h = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank">\1</a>', h)
    # Line breaks → paragraphs (double newline)
    h = _re.sub(r'\n{2,}', '</p><p>', h)
    # Single newlines → <br>
    h = h.replace('\n', '<br>')
    # Clean up br inside pre
    h = _re.sub(r'(<pre[^>]*>.*?</pre>)', lambda m: m.group(0).replace('<br>', '\n'), h, flags=_re.DOTALL)
    # Clean up br inside headers
    for tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
        h = _re.sub(rf'(<{tag}>.*?</{tag}>)', lambda m: m.group(0).replace('<br>', ''), h, flags=_re.DOTALL)
    return f'<p>{h}</p>'


def _render_csv_to_html(csv_text):
    """Render CSV as an HTML table."""
    import csv as _csv, io as _io, html as _html
    esc = _html.escape
    reader = _csv.reader(_io.StringIO(csv_text))
    rows = []
    for i, row in enumerate(reader):
        if i > 200:  # cap rows
            break
        cells = ''.join(f'<td>{esc(c)}</td>' for c in row)
        if i == 0:
            cells = ''.join(f'<th>{esc(c)}</th>' for c in row)
        rows.append(f'<tr>{cells}</tr>')
    header = rows[0] if rows else ''
    body = ''.join(rows[1:]) if len(rows) > 1 else ''
    return f'<div class="file-body file-body-csv"><table><thead>{header}</thead><tbody>{body}</tbody></table></div>'


def _run_team_chat_query(query_type, **kwargs):
    """Run team-chat-index.py via subprocess. Returns parsed JSON dict."""
    cmd = ["python3", TEAM_CHAT_INDEX_SCRIPT,
           "--jsonl", TEAM_CHAT_JSONL,
           "--db", TEAM_CHAT_DB,
           "--query", query_type]
    if kwargs.get("page") is not None:
        cmd.extend(["--page", str(kwargs["page"])])
    if kwargs.get("per_page") is not None:
        cmd.extend(["--per-page", str(kwargs["per_page"])])
    if kwargs.get("search"):
        cmd.extend(["--search", kwargs["search"]])
    if kwargs.get("msg_id") is not None:
        cmd.extend(["--msg-id", str(kwargs["msg_id"])])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        print(f"Team chat query error: {e}")
    return None


def _run_transcript_query(jsonl_path, sid, query, host=None, **kwargs):
    """Run transcript-index.py locally or via SSH. Returns parsed JSON dict."""
    db_path = f"/tmp/transcript-cache/{sid}.db"
    script_path = TRANSCRIPT_INDEX_SCRIPT
    if host:
        # Use script on remote host (deployed via scp/rsync)
        remote_home = _get_remote_home(host) or ""
        if remote_home:
            script_path = f"{remote_home}/claudecode-telegram/transcript-index.py"
    cmd = ["python3", script_path, "--jsonl", str(jsonl_path),
           "--db", db_path, "--query", query]
    if kwargs.get("page") is not None:
        cmd.extend(["--page", str(kwargs["page"])])
    if kwargs.get("per_page") is not None:
        cmd.extend(["--per-page", str(kwargs["per_page"])])
    if kwargs.get("search"):
        cmd.extend(["--search", kwargs["search"]])
    if kwargs.get("filter_mode"):
        cmd.extend(["--filter", kwargs["filter_mode"]])
    if kwargs.get("sort") and kwargs["sort"] != "relevance":
        cmd.extend(["--sort", kwargs["sort"]])
    try:
        if host:
            # For remote workers, use the script on the remote host
            r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=60)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception as e:
        print(f"Transcript query error: {e}")
    return None


def _start_transcript_sync(name: str, host: str, remote_path: str, local_tmp: Path, key: str):
    """Background thread: rsync transcript from remote host with progress tracking."""
    import time as _time
    try:
        with _TRANSCRIPT_SYNC_LOCK:
            _TRANSCRIPT_SYNC[key] = {"status": "syncing", "progress": "Connecting to remote host...",
                                     "started": _time.time(), "path": None, "error": None}
        # First get remote file size for progress
        r = subprocess.run(["ssh", host, f"stat -f%z '{remote_path}' 2>/dev/null || stat -c%s '{remote_path}' 2>/dev/null"],
                           capture_output=True, text=True, timeout=10)
        remote_size = 0
        if r.returncode == 0 and r.stdout.strip().isdigit():
            remote_size = int(r.stdout.strip())

        with _TRANSCRIPT_SYNC_LOCK:
            if remote_size > 0:
                size_mb = remote_size / 1_048_576
                _TRANSCRIPT_SYNC[key]["progress"] = f"Syncing transcript ({size_mb:.1f} MB)..."
                _TRANSCRIPT_SYNC[key]["remote_size"] = remote_size
            else:
                _TRANSCRIPT_SYNC[key]["progress"] = "Syncing transcript..."

        # Run rsync with --progress (we poll local file size for progress)
        proc = subprocess.Popen(
            ["rsync", "-az", f"{host}:{remote_path}", str(local_tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Poll local file size while rsync runs
        while proc.poll() is None:
            _time.sleep(1)
            try:
                if local_tmp.exists() and remote_size > 0:
                    local_size = local_tmp.stat().st_size
                    pct = min(99, int(local_size * 100 / remote_size))
                    with _TRANSCRIPT_SYNC_LOCK:
                        _TRANSCRIPT_SYNC[key]["progress"] = f"Syncing... {pct}% ({local_size / 1_048_576:.1f} / {remote_size / 1_048_576:.1f} MB)"
                        _TRANSCRIPT_SYNC[key]["pct"] = pct
            except Exception:
                pass

        if proc.returncode == 0 and local_tmp.exists() and local_tmp.stat().st_size > 0:
            with _TRANSCRIPT_SYNC_LOCK:
                _TRANSCRIPT_SYNC[key] = {"status": "done", "progress": "Ready", "path": str(local_tmp),
                                         "started": _TRANSCRIPT_SYNC[key]["started"], "error": None}
        else:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            with _TRANSCRIPT_SYNC_LOCK:
                _TRANSCRIPT_SYNC[key] = {"status": "error", "progress": "Sync failed",
                                         "started": _TRANSCRIPT_SYNC[key]["started"],
                                         "path": None, "error": stderr[:200] or "rsync failed"}
    except Exception as e:
        with _TRANSCRIPT_SYNC_LOCK:
            _TRANSCRIPT_SYNC[key] = {"status": "error", "progress": "Sync failed",
                                     "started": _TRANSCRIPT_SYNC.get(key, {}).get("started", 0),
                                     "path": None, "error": str(e)[:200]}


def _resolve_transcript_path(name: str, session_id: str = None):
    """Resolve transcript JSONL path for a worker (local or remote).

    Returns (transcript_path, sid, cwd) or (None, sid, cwd) if not found.
    For remote workers, returns ("syncing", sid, cwd) if sync is in progress.
    """
    cwd = get_claude_session_cwd(name) or os.path.expanduser("~")
    sid = session_id or get_claude_session_id(name, authoritative=True)
    if not sid:
        return None, "", cwd

    slug = _project_slug(cwd)
    transcript_path = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"

    if not transcript_path.exists():
        reg = _load_registry().get("workers", {})
        entry = reg.get(name, {})
        host = entry.get("host")
        if host:
            try:
                remote_home = _get_remote_home(host)
                if remote_home:
                    remote_cwd = cwd
                    local_home = os.path.expanduser("~")
                    if remote_cwd.startswith(local_home) and remote_home != local_home:
                        remote_cwd = remote_home + remote_cwd[len(local_home):]
                    remote_slug = _project_slug(remote_cwd)
                    remote_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                    local_tmp = Path(f"/tmp/transcript-{name}-{sid}.jsonl")
                    sync_key = f"{name}:{sid}"

                    # Check if sync already completed
                    with _TRANSCRIPT_SYNC_LOCK:
                        sync_info = _TRANSCRIPT_SYNC.get(sync_key)
                    if sync_info and sync_info["status"] == "done" and local_tmp.exists():
                        transcript_path = local_tmp
                    elif sync_info and sync_info["status"] == "syncing":
                        return "syncing", sid, cwd
                    elif sync_info and sync_info["status"] == "error":
                        # Don't retry forever — return None so caller shows error
                        err = sync_info.get("error", "unknown error")
                        print(f"[transcript] sync failed for {name}:{sid}: {err}")
                        return None, sid, cwd
                    else:
                        # Start background sync
                        t = threading.Thread(target=_start_transcript_sync,
                                             args=(name, host, remote_path, local_tmp, sync_key),
                                             daemon=True)
                        t.start()
                        return "syncing", sid, cwd
            except Exception:
                pass

    if transcript_path.exists():
        return transcript_path, sid, cwd
    return None, sid, cwd


def _parse_transcript_entries(transcript_path) -> list:
    """Parse JSONL transcript into a list of visible entries (skip noise)."""
    entries = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype = entry.get("type", "")
            if etype in ("progress", "queue-operation", "file-history-snapshot"):
                continue
            if etype == "system":
                continue
            entries.append(entry)
    return entries


def _generate_member_avatar(name: str) -> str:
    """Generate a unique SVG avatar for a team member based on name hash.

    Produces 1000+ unique variants via:
    - Hue (36 steps) × shape (6) × accent (5) × saturation (2) = 2160 combos
    """
    h = hash(name) & 0xFFFFFFFF
    hue = (h % 36) * 10  # 0-350 in steps of 10
    sat = 55 + (h >> 6 & 1) * 15  # 55 or 70
    shape_idx = (h >> 7) % 6
    accent_idx = (h >> 10) % 5
    initials = name[:2].upper() if len(name) >= 2 else name.upper()

    bg = f"hsl({hue},{sat}%,42%)"
    fg = f"hsl({hue},{max(sat-20,30)}%,75%)"

    # Base shapes for the background
    shapes = [
        '<circle cx="14" cy="14" r="14"/>',  # circle
        '<rect x="1" y="1" width="26" height="26" rx="6"/>',  # rounded rect
        '<polygon points="14,0 28,7 28,21 14,28 0,21 0,7"/>',  # hexagon
        '<polygon points="14,1 27,14 14,27 1,14"/>',  # diamond
        '<polygon points="14,0 28,10 22,28 6,28 0,10"/>',  # pentagon
        '<rect x="0" y="0" width="28" height="28" rx="10"/>',  # squircle
    ]
    # Accent overlays
    accents = [
        '',  # none
        f'<circle cx="14" cy="14" r="8" fill="none" stroke="{fg}" stroke-width="1.5" opacity=".3"/>',  # ring
        f'<circle cx="7" cy="7" r="2" fill="{fg}" opacity=".2"/><circle cx="21" cy="7" r="2" fill="{fg}" opacity=".2"/>',  # dots
        f'<line x1="4" y1="4" x2="24" y2="24" stroke="{fg}" stroke-width="1" opacity=".15"/><line x1="4" y1="24" x2="24" y2="4" stroke="{fg}" stroke-width="1" opacity=".15"/>',  # cross
        f'<rect x="4" y="12" width="20" height="4" rx="2" fill="{fg}" opacity=".15"/>',  # bar
    ]

    return (f'<div class="u-av"><svg viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">'
            f'<g fill="{bg}">{shapes[shape_idx]}</g>'
            f'{accents[accent_idx]}'
            f'<text x="14" y="14" text-anchor="middle" dominant-baseline="central" '
            f'fill="#fff" font-family="Inter,system-ui,sans-serif" font-size="11" font-weight="600" '
            f'opacity=".9">{initials}</text></svg></div>')


# Known team member prefixes for avatar detection
_TEAM_MEMBERS = {
    "chen", "geni", "hiro", "jin", "kai", "kelvin", "kenji",
    "lee", "luck", "mon", "noa", "ren", "ryo", "sora", "taro",
    "x", "yuki", "finn",
}

# Manager GitHub avatar
_MANAGER_AV = '<div class="u-av"><img src="https://avatars.githubusercontent.com/u/4256921" alt="manager"></div>'


def _detect_message_author(text: str) -> tuple:
    """Detect author from message prefix like 'ryo: message'.

    Returns (author_name, avatar_html, display_text).
    - Team member prefix → generated avatar, text without prefix
    - 'manager:' prefix → GitHub avatar, text without prefix
    - No prefix → GitHub avatar (default = manager), original text
    """
    stripped = text.strip()
    # Check for "name: " prefix (1-10 chars before colon)
    colon_pos = stripped.find(":")
    if 0 < colon_pos <= 10:
        prefix = stripped[:colon_pos].lower().strip()
        rest = stripped[colon_pos + 1:].strip()
        if prefix == "manager":
            return "manager", _MANAGER_AV, rest or stripped
        if prefix in _TEAM_MEMBERS:
            return prefix, _generate_member_avatar(prefix), rest or stripped
    # Default: manager avatar, full text
    return "manager", _MANAGER_AV, stripped


def _transcript_entry_to_html(entry: dict, esc, tool_results: dict = None) -> str:
    """Convert a single transcript entry to HTML block(s).

    Matches ampcode.com visual style: tool results merged into tool_use blocks,
    Edit diffs with +/- coloring, collapsible thinking.
    tool_results: map of tool_use_id → {content: str, is_error: bool}
    """
    import base64 as _b64
    if tool_results is None:
        tool_results = {}
    etype = entry.get("type", "")
    msg = entry.get("message", {})
    role = msg.get("role", "")
    content = msg.get("content", "")
    # Chevron SVG for expand/collapse
    _chev = '<svg class="chev" viewBox="0 0 16 16" fill="currentColor"><path d="M6.22 3.22a.75.75 0 011.06 0l4.25 4.25a.75.75 0 010 1.06l-4.25 4.25a.75.75 0 01-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 010-1.06z"/></svg>'
    # Claude sparkle avatar (Anthropic brand icon)
    _claude_av = '<div class="cl-av"><svg viewBox="0 0 24 24" fill="none"><path d="M16.98 5.35L12 2L7.02 5.35L1.28 6.35L3.28 12.1L1.28 17.85L7.02 18.85L12 22.2L16.98 18.85L22.72 17.85L20.72 12.1L22.72 6.35L16.98 5.35Z" fill="currentColor"/></svg></div>'
    # Tool-specific SVG icons
    _tool_svgs = {
        "Read": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 1.5a.25.25 0 00-.25.25v11.5c0 .138.112.25.25.25h8.5a.25.25 0 00.25-.25V6H9.75A1.75 1.75 0 018 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.086c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v8.086A1.75 1.75 0 0112.25 15h-8.5A1.75 1.75 0 012 13.25V1.75z"/></svg>',
        "Write": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 1.5a.25.25 0 00-.25.25v11.5c0 .138.112.25.25.25h8.5a.25.25 0 00.25-.25V6H9.75A1.75 1.75 0 018 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.086c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v8.086A1.75 1.75 0 0112.25 15h-8.5A1.75 1.75 0 012 13.25V1.75z"/></svg>',
        "Edit": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M11.013 1.427a1.75 1.75 0 012.474 0l1.086 1.086a1.75 1.75 0 010 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 01-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61zm1.414 1.06a.25.25 0 00-.354 0L10.811 3.75l1.439 1.44 1.263-1.263a.25.25 0 000-.354l-1.086-1.086zM11.189 6.25L9.75 4.811l-6.286 6.287a.25.25 0 00-.064.108l-.558 1.953 1.953-.558a.249.249 0 00.108-.064l6.286-6.287z"/></svg>',
        "Bash": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0114.25 15H1.75A1.75 1.75 0 010 13.25V2.75zm1.75-.25a.25.25 0 00-.25.25v10.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25V2.75a.25.25 0 00-.25-.25H1.75zM7.25 8a.75.75 0 01-.22.53l-2.25 2.25a.75.75 0 11-1.06-1.06L5.44 8 3.72 6.28a.75.75 0 111.06-1.06l2.25 2.25c.141.14.22.331.22.53zm1.5 1.5a.75.75 0 000 1.5h3a.75.75 0 000-1.5h-3z"/></svg>',
        "Grep": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M10.68 11.74a6 6 0 01-7.922-8.982 6 6 0 018.982 7.922l3.04 3.04a.749.749 0 01-.326 1.275.749.749 0 01-.734-.215l-3.04-3.04zM11.5 7a4.499 4.499 0 10-8.997 0A4.499 4.499 0 0011.5 7z"/></svg>',
        "Glob": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1A1.75 1.75 0 000 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0016 13.25v-8.5A1.75 1.75 0 0014.25 3H7.5a.25.25 0 01-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1H1.75z"/></svg>',
        "Agent": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M6.5.75a.75.75 0 00-1.5 0V2H3.75A1.75 1.75 0 002 3.75V5h-.25a.75.75 0 000 1.5H2v3h-.25a.75.75 0 000 1.5H2v1.25c0 .966.784 1.75 1.75 1.75h8.5A1.75 1.75 0 0014 12.25V11h.25a.75.75 0 000-1.5H14v-3h.25a.75.75 0 000-1.5H14V3.75A1.75 1.75 0 0012.25 2H11V.75a.75.75 0 00-1.5 0V2h-3V.75z"/></svg>',
    }
    _default_tool_svg = '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304zM10.567 2.304A4.49 4.49 0 0112.5 6c0 1.598-.832 3.002-2.09 3.802-.518.328-.929.923-.902 1.64v.008l.164 3.337a.75.75 0 101.498-.073l-.163-3.34c-.007-.14.1-.313.357-.476A5.994 5.994 0 0114 6c0-2.033-1.01-3.83-2.555-4.916a1.89 1.89 0 00-.878 1.22z"/></svg>'
    parts = []

    # Timestamp for the entry
    ts_raw = entry.get("timestamp", "")
    ts_html = ""
    if ts_raw:
        # Store raw ISO timestamp; JS renders in browser timezone
        ts_html = f'<span class="ts" data-ts="{esc(ts_raw)}">{esc(ts_raw[:16].replace("T"," "))}</span>'

    if etype == "user" and role == "user":
        if isinstance(content, str) and content.strip():
            raw = content.strip()
            # Skip system/internal messages (task notifications, hook file/image syntax)
            if raw.startswith("<task-notification>") or raw.startswith("<system-reminder>"):
                return ""
            # Detect author from prefix for avatar selection
            _author, _avatar, _display = _detect_message_author(raw)
            text = esc(_display)
            if len(text) > 2000:
                text = text[:2000] + "\n\n<em>… truncated</em>"
            # Show author name label for team members
            _name_html = f'<span class="u-name">{esc(_author)}</span>' if _author != "manager" else ""
            parts.append(f'<div class="user-msg">{_avatar}<div class="u-body">{_name_html}<div class="u-text">{text}{ts_html}</div></div></div>')
        elif isinstance(content, list):
            # tool_result entries are merged into their tool_use blocks — skip here
            pass

    elif etype == "assistant" and role == "assistant":
        if isinstance(content, list):
            for item in content:
                ct = item.get("type", "")
                if ct == "thinking":
                    raw_thinking = item.get("thinking", "")
                    if raw_thinking and raw_thinking.strip():
                        tt = esc(raw_thinking[:3000])
                        if len(raw_thinking) > 3000:
                            tt += "\n… truncated"
                        parts.append(f'<details class="think"><summary class="think-h">{_chev} Thinking</summary><div class="think-t">{tt}</div></details>')
                elif ct == "text":
                    text = item.get("text", "")
                    if text and text != "(no content)":
                        b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
                        parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
                elif ct == "tool_use":
                    tn = item.get("name", "?")
                    ti = item.get("input", {})
                    tool_svg = _tool_svgs.get(tn, _default_tool_svg)
                    # Extract compact display info
                    inp = ""
                    is_fp = False
                    if tn in ("Read", "Write"):
                        inp = ti.get("file_path", "")
                        is_fp = bool(inp and "/" in inp)
                    elif tn == "Edit":
                        inp = ti.get("file_path", "")
                        is_fp = bool(inp and "/" in inp)
                    elif tn == "Glob":
                        inp = ti.get("pattern", "")
                    elif tn == "Bash":
                        inp = ti.get("command", "")
                    elif tn in ("Grep", "Search"):
                        inp = ti.get("pattern", "")
                    elif tn == "Agent":
                        inp = ti.get("description", "") or str(ti.get("prompt", ""))[:80]
                    else:
                        inp = json.dumps(ti, ensure_ascii=False)[:200]
                    inp = str(inp)[:300]

                    # Look up merged result for this tool call
                    tool_id = item.get("id", "")
                    tr = tool_results.get(tool_id, {})
                    tr_text = tr.get("content", "")
                    tr_err = tr.get("is_error", False)
                    # Skip empty/noise results
                    _skip_result = (not tr_text or tr_text == "Bash completed with no output"
                                    or tr_text.strip() == "")
                    tr_esc = ""
                    if not _skip_result:
                        tr_str = str(tr_text)[:5000]
                        tr_esc = esc(tr_str)
                        if len(str(tr_text)) > 5000:
                            tr_esc += "\n… truncated"

                    # Edit tool with old_string/new_string → render as diff
                    if tn == "Edit" and ti.get("old_string") is not None:
                        fp = esc(ti.get("file_path", "?"))
                        old_s = ti.get("old_string", "")
                        new_s = ti.get("new_string", "")
                        old_lines = old_s.splitlines(True)
                        new_lines = new_s.splitlines(True)
                        n_del = len(old_lines)
                        n_add = len(new_lines)
                        diff_html_lines = []
                        ln_old = 1
                        for ln in old_lines[:60]:
                            diff_html_lines.append(f'<div class="diff-del"><span class="diff-ln">{ln_old}</span><span class="diff-sign">-</span>{esc(ln.rstrip())}</div>')
                            ln_old += 1
                        ln_new = 1
                        for ln in new_lines[:60]:
                            diff_html_lines.append(f'<div class="diff-add"><span class="diff-ln">{ln_new}</span><span class="diff-sign">+</span>{esc(ln.rstrip())}</div>')
                            ln_new += 1
                        if len(old_lines) > 60 or len(new_lines) > 60:
                            diff_html_lines.append('<div class="diff-ctx"><span class="diff-ln"></span><span class="diff-sign"> </span>… truncated</div>')
                        diff_body = "\n".join(diff_html_lines)
                        n_overlap = min(n_del, n_add)
                        n_pure_add = n_add - n_overlap
                        n_pure_del = n_del - n_overlap
                        stats_html = f'<span class="diff-stat"><span class="diff-plus">+{n_pure_add}</span> <span class="diff-minus">-{n_pure_del}</span> <span class="diff-mod">~{n_overlap}</span></span>'
                        pp = fp.rsplit("/", 1)
                        fp_html = f'<span class="fp-dir">{esc(pp[0])}/</span>{esc(pp[1])}' if len(pp) > 1 else esc(fp)
                        err_cls = " act-err" if tr_err else ""
                        parts.append(f'<details class="act diff-act{err_cls}"><summary class="act-h">{tool_svg}<span class="fp">{fp_html}</span>{stats_html}{_chev}</summary><div class="diff-body">{diff_body}</div></details>')
                    elif tn == "Bash" and inp:
                        # Bash: single block with command + output merged
                        body_parts = [f'<div class="act-cmd">{esc(inp)}</div>']
                        if tr_esc:
                            body_parts.append(f'<div class="act-out{" act-out-err" if tr_err else ""}">{tr_esc}</div>')
                        err_cls = " act-err" if tr_err else ""
                        parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="t-det">{esc(inp[:80])}</span>{_chev}</summary><div class="act-body">{"".join(body_parts)}</div></details>')
                    elif is_fp:
                        pp = inp.rsplit("/", 1)
                        dp = esc(pp[0]) if len(pp) > 1 else ""
                        bp = esc(pp[-1])
                        fp = f'<span class="fp-dir">{dp}/</span>{bp}' if dp else bp
                        if tr_esc and not _skip_result:
                            # File tool with result → expandable
                            err_cls = " act-err" if tr_err else ""
                            parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="fp">{fp}</span>{_chev}</summary><div class="act-body"><pre class="t-out">{tr_esc}</pre></div></details>')
                        else:
                            parts.append(f'<div class="chip">{tool_svg}<span class="fp">{fp}</span></div>')
                    else:
                        if tr_esc and not _skip_result:
                            err_cls = " act-err" if tr_err else ""
                            parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="t-det">{esc(inp[:80])}</span>{_chev}</summary><div class="act-body"><pre class="t-out">{tr_esc}</pre></div></details>')
                        else:
                            parts.append(f'<div class="chip">{tool_svg}<span class="t-det">{esc(inp)}</span></div>')
        elif isinstance(content, str) and content.strip():
            b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
            parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')

    return "\n".join(parts)


def _format_model_name(model_name: str) -> str:
    """Format model ID into display name: 'claude-opus-4-6' → 'Opus 4.6'."""
    import re as _re
    s = model_name.replace("claude-", "")
    # Strip date suffixes like -20251001
    s = _re.sub(r"-\d{8}$", "", s)
    # Convert version numbers: "opus-4-6" → "opus-4.6" (last dash before final digit = dot)
    s = _re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", s)
    # Also handle "haiku-4-5" pattern
    s = _re.sub(r"-(\d+)\.(\d+)$", r" \1.\2", s)
    # Remaining dashes to spaces
    s = s.replace("-", " ").title()
    return s


def _transcript_stats(entries: list) -> dict:
    """Extract metadata stats from transcript entries."""
    n_user = sum(1 for e in entries if e.get("type") == "user"
                 and e.get("message", {}).get("role") == "user"
                 and isinstance(e.get("message", {}).get("content"), str))
    n_tool = 0
    n_edit = 0
    lines_add = 0
    lines_del = 0
    lines_mod = 0
    files_modified = set()
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for c in (e.get("message", {}).get("content") or []):
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            n_tool += 1
            ti = c.get("input", {})
            if c.get("name") == "Edit" and ti.get("old_string") is not None:
                n_edit += 1
                fp = ti.get("file_path", "")
                if fp:
                    files_modified.add(fp)
                n_old = len(ti.get("old_string", "").splitlines(True))
                n_new = len(ti.get("new_string", "").splitlines(True))
                overlap = min(n_old, n_new)
                lines_mod += overlap
                lines_del += n_old - overlap
                lines_add += n_new - overlap
            elif c.get("name") in ("Write", "Read", "Edit"):
                fp = ti.get("file_path", "")
                if fp:
                    files_modified.add(fp)
    model = version = git_branch = ""
    first_ts = last_ts = ""
    input_tokens = output_tokens = 0
    for e in entries:
        if not model:
            model = e.get("message", {}).get("model", "")
        if not version:
            version = e.get("version", "")
        if not git_branch:
            git_branch = e.get("gitBranch", "")
        ts = e.get("timestamp", "")
        if ts and not first_ts:
            first_ts = ts
        if ts:
            last_ts = ts
        # Token usage from Claude response entries (in message.usage)
        usage = e.get("message", {}).get("usage", {})
        turn_in = (usage.get("input_tokens", 0)
                   + usage.get("cache_read_input_tokens", 0)
                   + usage.get("cache_creation_input_tokens", 0))
        turn_out = usage.get("output_tokens", 0)
        input_tokens += turn_in
        output_tokens += turn_out
    # Compute duration
    duration_str = ""
    if first_ts and last_ts:
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            delta = t1 - t0
            total_s = int(delta.total_seconds())
            if total_s < 0:
                total_s = 0
            days = total_s // 86400
            hours = (total_s % 86400) // 3600
            mins = (total_s % 3600) // 60
            if days > 0:
                duration_str = f"{days}d {hours}h"
            elif hours > 0:
                duration_str = f"{hours}h {mins}m"
            else:
                duration_str = f"{mins}m"
        except Exception:
            pass
    return {"n_user": n_user, "n_tool": n_tool, "n_edit": n_edit,
            "lines_add": lines_add, "lines_del": lines_del,
            "lines_mod": lines_mod, "n_files": len(files_modified), "model": model,
            "version": version, "git_branch": git_branch,
            "first_ts": first_ts, "last_ts": last_ts,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration": duration_str}



def _render_transcript_loading(name: str, sid: str, token: str, sync_key: str) -> str:
    """Render a loading page while transcript syncs from remote host."""
    import html as html_mod, time as _time
    esc = html_mod.escape
    with _TRANSCRIPT_SYNC_LOCK:
        info = _TRANSCRIPT_SYNC.get(sync_key, {})
    status = info.get("status", "syncing")
    progress = esc(info.get("progress", "Starting sync..."))
    pct = info.get("pct", 0)
    elapsed = int(_time.time() - info.get("started", _time.time()))
    error = info.get("error")

    if status == "error":
        bar_html = f'<div class="bar-fill err" style="width:100%"></div>'
        msg = f'<p class="err-msg">Error: {esc(error or "Unknown error")}</p>'
        meta_js = ""
    else:
        bar_html = f'<div class="bar-fill" style="width:{pct}%"></div>'
        msg = ""
        meta_js = '<meta http-equiv="refresh" content="2">'

    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loading {esc(name)}</title>
{meta_js}
<style>
body{{font-family:-apple-system,system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;
background:#0b0d0b;color:#e5e5e0}}
.card{{text-align:center;max-width:420px;padding:40px;width:100%}}
h1{{font-size:1.3rem;margin-bottom:8px;font-weight:600}}
.sub{{color:#878b86;font-size:.9rem;margin-bottom:24px}}
.bar{{background:#1a1c1a;border-radius:6px;height:8px;overflow:hidden;margin:16px 0}}
.bar-fill{{background:#22c55e;height:100%;border-radius:6px;transition:width .5s ease}}
.bar-fill.err{{background:#ef4444}}
.progress{{color:#a0a4a0;font-size:.85rem;margin:8px 0}}
.elapsed{{color:#5a5e5a;font-size:.8rem;margin-top:4px}}
.err-msg{{color:#ef4444;font-size:.85rem;margin-top:12px}}
.spinner{{display:inline-block;width:20px;height:20px;border:2px solid #2a2c2a;
border-top-color:#22c55e;border-radius:50%;animation:spin 1s linear infinite;
vertical-align:middle;margin-right:8px}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style></head><body><div class="card">
<h1>Preparing Transcript</h1>
<p class="sub">{esc(name)}</p>
<div class="bar">{bar_html}</div>
<p class="progress">{('<span class="spinner"></span>' if status == "syncing" else "")}{progress}</p>
<p class="elapsed">{elapsed}s elapsed</p>
{msg}
</div></body></html>'''


def _render_team_chat_html(page: int = None, per_page: int = 50,
                           search_query: str = "", token: str = "") -> str:
    """Render team Telegram chat as paginated HTML page."""
    import html as html_mod
    esc = html_mod.escape

    if search_query:
        query_result = _run_team_chat_query("search", search=search_query,
                                            page=page or 1, per_page=per_page)
    else:
        query_result = _run_team_chat_query("entries", page=page, per_page=per_page)

    if not query_result:
        return ('<html><body style="background:#0b0d0b;color:#e5e5e0;font-family:system-ui;padding:40px">'
                '<h1>Team chat not available</h1>'
                '<p>Run <code>/memory update</code> to index the latest export.</p></body></html>')

    messages = query_result.get("messages", [])
    if search_query:
        total = query_result.get("total_results", 0)
    else:
        total = query_result.get("total", 0)
    total_pages = query_result.get("total_pages", 1)
    page = query_result.get("page", 1)

    # Pagination query string (needed by message blocks for reply/context links)
    qs_parts = []
    if token:
        qs_parts.append(f"token={esc(token)}")
    if per_page != 50:
        qs_parts.append(f"per_page={per_page}")
    if search_query:
        qs_parts.append(f"q={esc(search_query)}")
    qs_base = "&".join(qs_parts)

    def page_url(p):
        parts = [f"page={p}"]
        if qs_base:
            parts.append(qs_base)
        return "?" + "&".join(parts)

    # Build msg_id → message lookup for reply context
    msg_by_id = {m.get("msg_id"): m for m in messages if m.get("msg_id")}

    # For reply-to messages not on this page, batch-fetch from DB
    missing_reply_ids = set()
    for msg in messages:
        rid = msg.get("reply_to")
        if rid and rid not in msg_by_id:
            missing_reply_ids.add(rid)
    reply_cache = {}
    if missing_reply_ids:
        for rid in missing_reply_ids:
            r = _run_team_chat_query("msg-by-id", msg_id=rid)
            if r and r.get("idx") is not None:
                reply_cache[rid] = r

    # Build message blocks
    blocks = []
    for msg in messages:
        msg_id = msg.get("msg_id", 0)
        sender = msg.get("display_sender", "")
        text = esc(msg.get("text", ""))
        ts = msg.get("timestamp", "")
        reply_to = msg.get("reply_to")
        # Auto-link URLs
        text = re.sub(r'(https?://\S+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)
        # Newlines to <br>
        text = text.replace("\n", "<br>")

        # Reply context
        reply_html = ""
        if reply_to:
            replied = msg_by_id.get(reply_to)
            if replied:
                rsender = esc(replied.get("display_sender", ""))
                rtext = esc(replied.get("text", "")[:120])
                if len(replied.get("text", "")) > 120:
                    rtext += "..."
                reply_html = (f'<a class="reply-ctx" href="#msg-{reply_to}">'
                              f'<span class="reply-name">{rsender}</span> {rtext}</a>')
            else:
                # Reply to message on another page — fetch text and link to its page
                rinfo = reply_cache.get(reply_to)
                if rinfo and rinfo.get("page"):
                    rpage = rinfo["page"]
                    rurl = f"?page={rpage}&{qs_base}#msg-{reply_to}" if qs_base else f"?page={rpage}#msg-{reply_to}"
                    rsender = esc(rinfo.get("display_sender", ""))
                    rtext_raw = rinfo.get("text", "")
                    rtext = esc(rtext_raw[:120])
                    if len(rtext_raw) > 120:
                        rtext += "..."
                    reply_html = (f'<a class="reply-ctx" href="{rurl}">'
                                  f'<span class="reply-name">{rsender}</span> {rtext}</a>')

        # Avatar
        if sender == "manager":
            av = _MANAGER_AV
        elif sender in _TEAM_MEMBERS:
            av = _generate_member_avatar(sender)
        else:
            av = _generate_member_avatar(sender or "system")

        name_html = f'<span class="u-name">{esc(sender)}</span>'
        ts_html = f'<span class="ts" data-ts="{esc(ts)}">{esc(ts[11:16]) if len(ts) > 16 else ""}</span>'

        # In search mode, add "view in context" link
        ctx_link = ""
        if search_query and msg.get("idx") is not None:
            ctx_page = (msg["idx"] // per_page) + 1
            ctx_qs = f"page={ctx_page}"
            if token:
                ctx_qs += f"&token={esc(token)}"
            if per_page != 50:
                ctx_qs += f"&per_page={per_page}"
            ctx_link = f' <a class="ctx-link" href="?{ctx_qs}#msg-{msg_id}" title="View in context">\u2197</a>'

        # Media rendering (photo / file)
        media_html = ""
        photo = msg.get("photo", "")
        file_path_val = msg.get("file", "")
        file_name = msg.get("file_name", "")
        media_token = f"token={esc(token)}" if token else ""
        if photo:
            from urllib.parse import quote as _url_quote
            media_url = f"/team-chat-media/{_url_quote(photo, safe='/')}?{media_token}"
            media_html = (f'<div class="media-wrap">'
                          f'<a href="{media_url}" target="_blank">'
                          f'<img class="chat-photo" src="{media_url}" loading="lazy" alt=""></a>'
                          f'</div>')
        elif file_path_val and not file_path_val.startswith("(File not included"):
            from urllib.parse import quote as _url_quote
            media_url = f"/team-chat-media/{_url_quote(file_path_val, safe='/')}?{media_token}"
            display_name = esc(file_name) if file_name else esc(file_path_val.split("/")[-1])
            ext = os.path.splitext(file_path_val)[1].lower()
            # Image files — render inline like photos
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
                media_html = (f'<div class="media-wrap">'
                              f'<a href="{media_url}" target="_blank">'
                              f'<img class="chat-photo" src="{media_url}" loading="lazy" alt=""></a>'
                              f'<a class="file-dl" href="{media_url}" download="{esc(file_name or "")}">{display_name}</a>'
                              f'</div>')
            # Previewable text/code files — render inline, open by default
            elif ext in (".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".py",
                         ".sh", ".js", ".ts", ".go", ".rs", ".toml", ".log"):
                _chev = '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>'
                # Read file content for inline rendering
                _file_full = os.path.join(TEAM_CHAT_MEDIA_DIR, file_path_val)
                _file_content = ""
                try:
                    with open(_file_full, "r", encoding="utf-8", errors="replace") as _ff:
                        _file_content = _ff.read(64_000)  # cap at 64KB
                except Exception:
                    pass
                if ext == ".md":
                    # Render markdown to HTML
                    _rendered = _render_md_to_html(_file_content)
                    _body = f'<div class="file-body file-body-md">{_rendered}</div>'
                elif ext in (".py", ".sh", ".js", ".ts", ".go", ".rs"):
                    _body = f'<pre class="file-body file-body-code"><code>{esc(_file_content)}</code></pre>'
                elif ext in (".json", ".yaml", ".yml", ".toml"):
                    _body = f'<pre class="file-body file-body-code"><code>{esc(_file_content)}</code></pre>'
                elif ext == ".csv":
                    _body = _render_csv_to_html(_file_content)
                else:
                    # .txt, .log — plain preformatted
                    _body = f'<pre class="file-body file-body-text">{esc(_file_content)}</pre>'
                media_html = (f'<div class="media-wrap">'
                              f'<details class="file-card" open>'
                              f'<summary class="file-header">'
                              f'<span class="file-icon">&#128196;</span> {display_name}'
                              f'<a class="file-dl-btn" href="{media_url}" download="{esc(file_name or "")}" onclick="event.stopPropagation()">&#8615;</a>'
                              f'{_chev}'
                              f'</summary>'
                              f'{_body}'
                              f'</details></div>')
            # PDF — collapsible viewer (iframe, open by default)
            elif ext == ".pdf":
                _chev = '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>'
                media_html = (f'<div class="media-wrap">'
                              f'<details class="file-card" open>'
                              f'<summary class="file-header">'
                              f'<span class="file-icon">&#128195;</span> {display_name}'
                              f'<a class="file-dl-btn" href="{media_url}" download="{esc(file_name or "")}" onclick="event.stopPropagation()">&#8615;</a>'
                              f'{_chev}'
                              f'</summary>'
                              f'<iframe class="file-preview file-preview-pdf" src="{media_url}"></iframe>'
                              f'</details></div>')
            # Other files — download card
            else:
                media_html = (f'<div class="media-wrap">'
                              f'<a class="file-card-link" href="{media_url}" download="{esc(file_name or "")}">'
                              f'<span class="file-icon">&#128206;</span> {display_name}'
                              f'</a></div>')

        blocks.append(
            f'<div class="chat-msg" id="msg-{msg_id}">'
            f'{av}<div class="chat-body">'
            f'{name_html} {ts_html}{ctx_link}'
            f'{reply_html}'
            f'{media_html}'
            f'<div class="chat-text">{text}</div>'
            f'</div></div>'
        )

    # Pagination nav
    nav_html = ""
    if total_pages > 1:
        nav_items = []
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page <= 1 else ""}" href="{page_url(1)}">First</a>')
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page <= 1 else ""}" href="{page_url(page-1)}">Prev</a>')
        start_p = max(1, page - 3)
        end_p = min(total_pages, start_p + 6)
        start_p = max(1, end_p - 6)
        for p in range(start_p, end_p + 1):
            cls = " pg-cur" if p == page else ""
            nav_items.append(f'<a class="pg-btn{cls}" href="{page_url(p)}">{p}</a>')
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page >= total_pages else ""}" href="{page_url(page+1)}">Next</a>')
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page >= total_pages else ""}" href="{page_url(total_pages)}">Last</a>')
        nav_html = f'<nav class="pg">{"".join(nav_items)}<span class="pg-info">Page {page}/{total_pages} ({total} messages)</span></nav>'

    search_val = esc(search_query) if search_query else ""
    search_result = ""
    if search_query:
        search_result = f'<div class="search-info">Found {total} matching messages for "<strong>{esc(search_query)}</strong>"</div>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team Chat</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
:root {{
  --bg:#0b0d0b; --fg:#e5e5e0; --border:rgba(135,139,134,.12); --muted:#9ca49c;
  --user-bg:rgba(255,255,255,.04); --code-bg:#1a1c1a; --link:#75dbf0; --radius:6px;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
}}
@media(prefers-color-scheme:light){{
  :root{{--bg:#fafaf8;--fg:#1a1a1a;--muted:#595959;--border:rgba(135,139,134,.2);
    --user-bg:rgba(0,0,0,.03);--code-bg:#f4f4f0;--link:#0969da;}}
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html{{font-size:14px}}
body{{font-family:var(--sans);background:var(--bg);color:var(--fg);line-height:1.6;
  -webkit-font-smoothing:antialiased}}
.wrap{{max-width:48rem;margin:0 auto;padding:24px 16px 80px}}
header{{border-bottom:1px solid var(--border);padding-bottom:16px;margin-bottom:20px}}
h1{{font-size:1.3rem;font-weight:700}}
.meta{{color:var(--muted);font-size:.85rem;margin-top:4px}}
/* Search */
.search-bar{{display:flex;gap:8px;margin-bottom:16px}}
.search-bar input{{flex:1;background:var(--code-bg);border:1px solid var(--border);
  border-radius:var(--radius);padding:8px 12px;color:var(--fg);font-size:.9rem;
  font-family:var(--sans);outline:none}}
.search-bar input:focus{{border-color:var(--link)}}
.search-bar button{{background:var(--code-bg);border:1px solid var(--border);
  border-radius:var(--radius);padding:8px 16px;color:var(--fg);cursor:pointer;
  font-family:var(--sans);font-size:.9rem}}
.search-bar button:hover{{border-color:var(--muted)}}
.search-info{{background:rgba(117,219,240,.06);border:1px solid rgba(117,219,240,.15);
  border-radius:var(--radius);padding:8px 12px;margin-bottom:16px;font-size:.85rem;color:var(--link)}}
/* Pagination */
.pg{{display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin:16px 0;font-size:.85rem}}
.pg-btn{{padding:4px 10px;border:1px solid var(--border);border-radius:var(--radius);
  color:var(--fg);text-decoration:none;transition:border-color .15s}}
.pg-btn:hover{{border-color:var(--muted)}}
.pg-cur{{background:var(--link);color:#000;border-color:var(--link);font-weight:600}}
.pg-dis{{opacity:.3;pointer-events:none}}
.pg-info{{margin-left:8px;color:var(--muted)}}
/* Chat messages */
.thread{{display:flex;flex-direction:column;gap:4px}}
.chat-msg{{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:8px 8px;
  border-radius:8px;transition:background .2s}}
.chat-msg:target{{background:rgba(117,219,240,.1)}}
.chat-msg:hover{{background:var(--user-bg)}}
.chat-body{{min-width:0}}
.u-av{{width:28px;height:28px;border-radius:50%;overflow:hidden;flex-shrink:0;margin-top:2px}}
.u-av img{{width:100%;height:100%;object-fit:cover;border-radius:50%}}
.u-av svg{{width:100%;height:100%}}
.u-name{{font-weight:600;font-size:.85rem;margin-right:6px}}
.ts{{font-size:.75rem;color:var(--muted)}}
.chat-text{{white-space:pre-wrap;word-break:break-word;font-size:.9rem;line-height:1.6;margin-top:2px}}
.chat-text a{{color:var(--link)}}
/* Media */
.media-wrap{{margin:4px 0 6px}}
.chat-photo{{max-width:100%;border-radius:8px;display:block;cursor:pointer;
  border:1px solid var(--border)}}
.chat-photo:hover{{opacity:.9}}
.file-dl{{display:block;font-size:.75rem;color:var(--muted);margin-top:2px;text-decoration:none}}
.file-dl:hover{{color:var(--link)}}
.file-card{{background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden}}
.file-header{{display:flex;align-items:center;gap:6px;padding:8px 10px;font-size:.85rem;
  color:var(--fg);cursor:pointer;list-style:none}}
.file-header::-webkit-details-marker{{display:none}}
.file-header:hover{{background:var(--user-bg)}}
.file-header .chev{{margin-left:auto;width:14px;height:14px;color:var(--muted);
  transition:transform .15s;flex-shrink:0}}
details.file-card[open] .chev{{transform:rotate(90deg)}}
details.file-card[open] .file-header{{border-bottom:1px solid var(--border)}}
.file-icon{{font-size:1rem}}
.file-dl-btn{{margin-left:auto;color:var(--muted);text-decoration:none;font-size:1rem;
  padding:0 4px;border-radius:4px}}
.file-dl-btn:hover{{color:var(--link);background:rgba(117,219,240,.08)}}
.file-preview{{width:100%;height:300px;border:0;background:var(--bg);color:var(--fg)}}
.file-preview-pdf{{height:500px}}
/* Inline file content */
.file-body{{padding:12px 14px;font-size:.85rem;line-height:1.6;color:var(--fg);
  max-height:500px;overflow:auto;border-top:1px solid var(--border)}}
.file-body-md{{font-family:var(--sans)}}
.file-body-md h1,.file-body-md h2,.file-body-md h3,.file-body-md h4{{margin:12px 0 6px;font-weight:600}}
.file-body-md h1{{font-size:1.3rem}}.file-body-md h2{{font-size:1.1rem}}.file-body-md h3{{font-size:.95rem}}
.file-body-md p{{margin:4px 0}}.file-body-md li{{margin:2px 0 2px 16px;list-style:disc}}
.file-body-md hr{{border:0;border-top:1px solid var(--border);margin:10px 0}}
.file-body-md a{{color:var(--link)}}.file-body-md strong{{font-weight:600}}
.file-body-md .md-code{{background:var(--code-bg);padding:8px 10px;border-radius:4px;display:block;
  font-family:var(--mono);font-size:.8rem;overflow-x:auto;white-space:pre;margin:6px 0}}
.file-body-md .md-inline{{background:var(--code-bg);padding:1px 4px;border-radius:3px;font-family:var(--mono);font-size:.8rem}}
.file-body-code{{font-family:var(--mono);white-space:pre;overflow-x:auto;margin:0;
  background:var(--code-bg);border-top:1px solid var(--border)}}
.file-body-code code{{font-size:.8rem;line-height:1.5}}
.file-body-text{{font-family:var(--mono);white-space:pre-wrap;word-break:break-word;margin:0;
  background:var(--code-bg);border-top:1px solid var(--border)}}
.file-body-csv{{overflow-x:auto;border-top:1px solid var(--border)}}
.file-body-csv table{{width:100%;border-collapse:collapse;font-size:.8rem}}
.file-body-csv th,.file-body-csv td{{padding:4px 8px;border:1px solid var(--border);text-align:left}}
.file-body-csv th{{background:var(--card);font-weight:600;position:sticky;top:0}}
.file-card-link{{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;
  background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--fg);text-decoration:none;font-size:.85rem}}
.file-card-link:hover{{border-color:var(--link);color:var(--link)}}
/* Reply context */
.reply-ctx{{display:block;border-left:3px solid var(--link);padding:2px 8px;margin:2px 0 4px;
  font-size:.8rem;color:var(--muted);text-decoration:none;border-radius:2px;
  background:rgba(117,219,240,.04);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}}
.reply-ctx:hover{{background:rgba(117,219,240,.1);color:var(--fg)}}
.reply-name{{font-weight:600;color:var(--link);margin-right:4px}}
/* Context link (search → full view) */
.ctx-link{{font-size:.75rem;color:var(--muted);text-decoration:none;margin-left:4px;
  opacity:.6;transition:opacity .15s}}
.ctx-link:hover{{opacity:1;color:var(--link)}}
/* Jump buttons */
.jump{{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:6px}}
.jump a{{width:32px;height:32px;border-radius:50%;background:var(--code-bg);border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;color:var(--muted);text-decoration:none;font-size:1rem;
  transition:border-color .15s}}
.jump a:hover{{border-color:var(--muted);color:var(--fg)}}
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>Team Chat</h1>
<div class="meta">Telegram chat history</div>
</header>

<form class="search-bar" method="get">
<input type="text" name="q" value="{search_val}" placeholder="Search messages...">
<button type="submit">Search</button>
<input type="hidden" name="token" value="{esc(token)}">
{"" if per_page == 50 else f'<input type="hidden" name="per_page" value="{per_page}">'}
</form>

{search_result}
{nav_html}

<div class="thread" id="thread">
{"".join(blocks)}
</div>

{nav_html}
</div>

<div class="jump">
<a href="#" onclick="window.scrollTo(0,0);return false">&uarr;</a>
<a href="#" onclick="window.scrollTo(0,document.body.scrollHeight);return false">&darr;</a>
</div>

<script>
// Render timestamps in browser timezone
document.querySelectorAll('.ts[data-ts]').forEach(el => {{
  try {{
    const d = new Date(el.dataset.ts);
    if (!isNaN(d)) el.textContent = d.toLocaleString(undefined, {{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}});
  }} catch(e) {{}}
}});
// Scroll to target anchor if present
if (location.hash) {{
  const el = document.querySelector(location.hash);
  if (el) setTimeout(() => el.scrollIntoView({{behavior:'smooth',block:'center'}}), 100);
}}
</script>
</body>
</html>'''


def _render_transcript_html(name: str, session_id: str = None,
                            page: int = None, per_page: int = 50,
                            search_query: str = "", token: str = "",
                            filter_mode: str = "", search_sort: str = "relevance") -> str:
    """Render a worker's transcript as polished HTML (ampcode.com style).

    Supports pagination (?page=N&per_page=50) and search (?q=term).
    filter_mode="prompts" shows only user messages.
    page=None means "show last page" (most recent entries).
    Uses marked.js for markdown and highlight.js for syntax highlighting.
    """
    import html as html_mod
    esc = html_mod.escape

    # For remote workers, bypass local path resolution and query via SSH directly
    host = get_worker_host(name)
    if host:
        cwd = get_claude_session_cwd(name) or ""
        sid = session_id or get_claude_session_id(name, authoritative=True)
        if not sid:
            return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>No session found for {esc(name)}</h1></body></html>"
        remote_home = _get_remote_home(host) or ""
        if not remote_home:
            return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>Cannot resolve remote home for {esc(name)}</h1></body></html>"
        remote_cwd = cwd
        local_home = os.path.expanduser("~")
        if remote_cwd.startswith(local_home) and remote_home != local_home:
            remote_cwd = remote_home + remote_cwd[len(local_home):]
        remote_slug = _project_slug(remote_cwd)
        jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
        transcript_path = None  # No local file for remote workers
    else:
        transcript_path, sid, cwd = _resolve_transcript_path(name, session_id)
        if not sid:
            return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>No session found for {esc(name)}</h1></body></html>"
        if not transcript_path or transcript_path == "syncing":
            return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>Transcript not found</h1><p>Worker: {esc(name)}</p><p>Session: {esc(sid)}</p></body></html>"
        jsonl_path = str(transcript_path)

    # Query transcript-index.py — combined entries+stats in single call (saves SSH round-trip)
    if search_query:
        query_result = _run_transcript_query(
            jsonl_path, sid, "search+stats", host=host,
            search=search_query, page=page or 1, per_page=per_page, sort=search_sort)
    else:
        query_result = _run_transcript_query(
            jsonl_path, sid, "entries+stats", host=host,
            page=page, per_page=per_page, filter_mode=filter_mode)
    stats_result = query_result.pop("stats", None) if query_result else None

    # Fallback to old parsing if script fails
    if not query_result:
        if not transcript_path or not Path(str(transcript_path)).exists():
            return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>Transcript not available</h1><p>Worker: {esc(name)}</p><p>Session: {esc(sid)}</p></body></html>"
        all_entries = _parse_transcript_entries(transcript_path)
        total = len(all_entries)
        for _i, _e in enumerate(all_entries):
            _e["_idx"] = _i
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page is None:
            page = total_pages
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        page_entries = all_entries[start:start + per_page]
        stats = _transcript_stats(all_entries)
        file_size_str = ""
    else:
        # Reconstruct entry dicts from raw_json
        page_entries = []
        for e in query_result.get("entries", []):
            try:
                entry = json.loads(e["raw_json"])
                entry["_idx"] = e.get("idx", -1)
                page_entries.append(entry)
            except (json.JSONDecodeError, KeyError):
                continue

        if search_query:
            total = query_result.get("total_results", 0)
        else:
            total = query_result.get("total", 0)
        total_pages = query_result.get("total_pages", 1)
        page = query_result.get("page", 1)

        _empty_stats = {"n_user": 0, "n_tool": 0, "n_edit": 0, "lines_add": 0,
                        "lines_del": 0, "lines_mod": 0, "n_files": 0, "model": "",
                        "version": "", "git_branch": "", "first_ts": "", "last_ts": "",
                        "input_tokens": 0, "output_tokens": 0, "duration": ""}
        stats = stats_result if stats_result else _empty_stats

    # File size of the transcript JSONL (local only)
    file_size_str = ""
    if not host:
        try:
            file_size_bytes = os.path.getsize(transcript_path)
            if file_size_bytes >= 1_048_576:
                file_size_str = f"{file_size_bytes / 1_048_576:.1f} MB"
            elif file_size_bytes >= 1024:
                file_size_str = f"{file_size_bytes / 1024:.0f} KB"
            else:
                file_size_str = f"{file_size_bytes} B"
        except OSError:
            pass

    # Pre-index tool results by tool_use_id for merging into tool_use blocks
    _tool_results = {}
    for entry in page_entries:
        if entry.get("type") == "user":
            ct = entry.get("message", {}).get("content", [])
            if isinstance(ct, list):
                for item in ct:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tuid = item.get("tool_use_id", "")
                        rt = item.get("content", "")
                        if isinstance(rt, list):
                            rt = "\n".join(r.get("text", "") for r in rt if isinstance(r, dict) and r.get("type") == "text")
                        _tool_results[tuid] = {"content": str(rt), "is_error": bool(item.get("is_error"))}

    # Render blocks — group consecutive assistant entries into a turn-body
    # No avatar/label on assistant turns (matches AmpCode: only user has avatar)
    blocks = []
    in_assistant_turn = False

    # Build context URL for search mode (click message → jump to full transcript)
    def _ctx_url(entry):
        if not search_query:
            return ""
        idx = entry.get("_idx", -1)
        if idx < 0:
            return ""
        ctx_page = (idx // per_page) + 1
        ctx_qs = []
        if token:
            ctx_qs.append(f"token={esc(token)}")
        if session_id:
            ctx_qs.append(f"sid={esc(session_id)}")
        if per_page != 50:
            ctx_qs.append(f"per_page={per_page}")
        ctx_qs.append(f"page={ctx_page}")
        return f'?{"&".join(ctx_qs)}#e-{idx}'

    for entry in page_entries:
        etype = entry.get("type", "")
        role = entry.get("message", {}).get("role", "")
        # Skip tool_result entries — they're merged into tool_use blocks
        is_tool_result = (etype == "user" and role == "user" and
                          isinstance(entry.get("message", {}).get("content"), list) and
                          any(c.get("type") == "tool_result" for c in entry.get("message", {}).get("content", []) if isinstance(c, dict)))
        if is_tool_result:
            continue
        h = _transcript_entry_to_html(entry, esc, tool_results=_tool_results)
        if not h:
            continue
        eidx = entry.get("_idx", -1)
        anchor = f' id="e-{eidx}"' if eidx >= 0 else ""
        curl = _ctx_url(entry)
        is_assistant = (etype == "assistant" and role == "assistant")
        if is_assistant:
            if curl:
                if in_assistant_turn:
                    blocks.append('</div>')
                    in_assistant_turn = False
                h = f'<a class="ctx-wrap" href="{curl}"{anchor}>{h}</a>'
                blocks.append(h)
            else:
                if not in_assistant_turn:
                    blocks.append(f'<div class="turn-body"{anchor}>')
                    in_assistant_turn = True
                blocks.append(h)
        else:
            if in_assistant_turn:
                blocks.append('</div>')
                in_assistant_turn = False
            if curl:
                h = f'<a class="ctx-wrap" href="{curl}"{anchor}>{h}</a>'
            elif anchor:
                h = f'<div{anchor}>{h}</div>'
            blocks.append(h)
    if in_assistant_turn:
        blocks.append('</div>')

    # Build query string for pagination links (token first to preserve auth)
    qs_parts = []
    if token:
        qs_parts.append(f"token={esc(token)}")
    if session_id:
        qs_parts.append(f"sid={esc(session_id)}")
    if per_page != 50:
        qs_parts.append(f"per_page={per_page}")
    if search_query:
        qs_parts.append(f"q={esc(search_query)}")
    if filter_mode:
        qs_parts.append(f"filter={esc(filter_mode)}")
    qs_base = "&".join(qs_parts)

    def page_url(p):
        parts = [f"page={p}"]
        if qs_base:
            parts.append(qs_base)
        return "?" + "&".join(parts)

    # Pagination nav
    nav_html = ""
    if total_pages > 1:
        nav_items = []
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page <= 1 else ""}" href="{page_url(1)}">First</a>')
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page <= 1 else ""}" href="{page_url(page-1)}">Prev</a>')
        # Page numbers: show up to 7 centered on current
        start_p = max(1, page - 3)
        end_p = min(total_pages, start_p + 6)
        start_p = max(1, end_p - 6)
        for p in range(start_p, end_p + 1):
            cls = " pg-cur" if p == page else ""
            nav_items.append(f'<a class="pg-btn{cls}" href="{page_url(p)}">{p}</a>')
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page >= total_pages else ""}" href="{page_url(page+1)}">Next</a>')
        nav_items.append(f'<a class="pg-btn{" pg-dis" if page >= total_pages else ""}" href="{page_url(total_pages)}">Last</a>')
        nav_html = f'<nav class="pg">{"".join(nav_items)}<span class="pg-info">Page {page}/{total_pages} ({total} entries)</span></nav>'

    search_val = esc(search_query) if search_query else ""
    search_result = ""
    if search_query:
        sort_label = "by time" if search_sort == "time" else "by relevance"
        alt_sort = "time" if search_sort == "relevance" else "relevance"
        alt_label = "time" if search_sort == "relevance" else "relevance"
        sort_qs = []
        if token:
            sort_qs.append(f"token={esc(token)}")
        if session_id:
            sort_qs.append(f"sid={esc(session_id)}")
        sort_qs.append(f"q={esc(search_query)}")
        if per_page != 50:
            sort_qs.append(f"per_page={per_page}")
        sort_qs.append(f"sort={alt_sort}")
        sort_url = f'?{"&".join(sort_qs)}'
        search_result = (
            f'<div class="search-info">Found {total} matching entries for "<strong>{esc(search_query)}</strong>" '
            f'(sorted {sort_label}) &middot; <a href="{sort_url}">sort by {alt_label}</a></div>'
        )

    # Build prompts filter URL (toggle on/off)
    _filt_qs = []
    if token:
        _filt_qs.append(f"token={esc(token)}")
    if session_id:
        _filt_qs.append(f"sid={esc(session_id)}")
    if per_page != 50:
        _filt_qs.append(f"per_page={per_page}")
    if filter_mode != "prompts":
        _filt_qs.append("filter=prompts")
    prompts_filter_url = "?" + "&".join(_filt_qs) if _filt_qs else "?"
    filter_banner = ""
    if filter_mode == "prompts":
        _clear_qs = [p for p in _filt_qs]  # already excludes filter=prompts
        _clear_url = "?" + "&".join(_clear_qs) if _clear_qs else "?"
        filter_banner = f'<div class="search-info">Showing prompts only — <a href="{_clear_url}">show all</a></div>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(name)} — Transcript</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" media="(prefers-color-scheme: dark)">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css" media="(prefers-color-scheme: light)">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.1/marked.min.js"></script>
<style>
:root {{
  --bg:#0b0d0b; --fg:#e5e5e0; --border:rgba(135,139,134,.12); --muted:#9ca49c;
  --card:rgba(11,13,11,.02); --user-bg:rgba(255,255,255,.04);
  --user-border:rgba(135,139,134,.12); --code-bg:#1a1c1a;
  --green:#22c55e; --red:#bd2b2b; --link:#75dbf0; --radius:6px;
  --claude:#d4a574; --claude-bg:rgba(212,165,116,.08);
  --mono:"JetBrains Mono","Berkeley Mono","Fira Code","SF Mono",monospace;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
  --diff-add-bg:rgba(34,197,94,.1); --diff-add-fg:#22c55e;
  --diff-del-bg:rgba(239,68,68,.1); --diff-del-fg:#ef4444;
}}
@media(prefers-color-scheme:light){{
  :root{{--bg:#fafaf8;--fg:#1a1a1a;--muted:#595959;--border:rgba(135,139,134,.2);
    --card:rgba(246,255,245,.03);--user-bg:rgba(0,0,0,.03);--user-border:rgba(135,139,134,.2);
    --code-bg:#f4f4f0;--green:#16a34a;--red:#d44444;--link:#0969da;
    --claude:#b07d4f;--claude-bg:rgba(176,125,79,.06);
    --diff-add-bg:rgba(34,197,94,.1);--diff-add-fg:#16a34a;
    --diff-del-bg:rgba(239,68,68,.1);--diff-del-fg:#dc2626;}}
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html{{font-size:14px}}
body{{font-family:var(--sans);background:var(--bg);color:var(--fg);line-height:1.6;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
.wrap{{display:flex;flex-direction:column;min-height:100vh}}
.main{{flex:1;display:flex;justify-content:center;padding:24px 16px 80px;gap:24px}}
.content{{flex:1;min-width:0;max-width:42rem}}
/* Sidebar */
.sidebar{{width:240px;flex-shrink:0;position:sticky;top:24px;align-self:flex-start;
  font-size:.8rem;color:var(--muted)}}
.sidebar-inner{{border:1px solid var(--border);border-radius:10px;padding:16px;
  background:var(--card);display:flex;flex-direction:column;gap:12px}}
.sb-title{{font-weight:600;color:var(--fg);font-size:.85rem;margin-bottom:4px}}
.sb-row{{display:flex;justify-content:space-between;align-items:center}}
.sb-label{{color:var(--muted)}}
.sb-val{{color:var(--fg);font-weight:500;font-family:var(--mono);font-size:.75rem}}
.sb-divider{{border-top:1px solid var(--border);margin:4px 0}}
.sb-lines{{display:flex;gap:10px;font-family:var(--mono);font-size:.75rem;font-weight:600}}
.sb-lines .plus{{color:var(--green)}}.sb-lines .minus{{color:var(--red)}}.sb-lines .mod{{color:#f59e0b}}
/* Mobile sidebar toggle (in header) */
.sb-toggle{{display:none;background:none;border:none;color:var(--muted);cursor:pointer;
  padding:2px;line-height:0;transition:color .15s}}
.sb-toggle:hover{{color:var(--fg)}}
@media(max-width:900px){{
  .sidebar{{display:none;position:fixed;top:0;right:0;bottom:0;width:260px;z-index:100;
    padding:16px;background:var(--bg);border-left:1px solid var(--border);
    overflow-y:auto;box-shadow:-4px 0 20px rgba(0,0,0,.3)}}
  .sidebar.sb-open{{display:block}}
  .sb-toggle{{display:inline-flex}}
  .main{{justify-content:center}}
}}
/* Header */
header{{border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:24px}}
.h-row{{display:flex;align-items:center;gap:8px}}
h1{{font-size:1.5rem;font-weight:600;letter-spacing:-.02em}}
.meta{{display:flex;flex-wrap:wrap;gap:16px;margin-top:10px;color:var(--muted);font-size:.8rem}}
.mi{{display:inline-flex;align-items:center;gap:5px}}
.mi svg{{width:14px;height:14px;opacity:.7;flex-shrink:0}}
/* Search */
.search-bar{{margin-bottom:16px;display:flex;gap:8px}}
.search-bar input{{flex:1;padding:8px 12px;border:1px solid var(--border);border-radius:8px;
  background:var(--card);color:var(--fg);font-size:.875rem;font-family:var(--sans);outline:none;
  transition:border-color .15s}}
.search-bar input:focus{{border-color:var(--link)}}
.search-bar button{{padding:8px 16px;border:1px solid var(--border);border-radius:8px;
  background:var(--card);color:var(--fg);cursor:pointer;font-size:.8rem;transition:all .15s}}
.search-bar button:hover{{border-color:var(--link);color:var(--link)}}
.search-info{{color:var(--muted);font-size:.8rem;margin-bottom:12px;padding:8px 12px;
  border:1px dashed var(--border);border-radius:8px}}
.ctx-wrap{{display:block;text-decoration:none;color:inherit;border-radius:8px;
  padding:4px;margin:-4px;transition:background .15s;cursor:pointer}}
.ctx-wrap:hover{{background:var(--user-bg);text-decoration:none}}
mark{{background:rgba(250,204,21,.25);color:inherit;border-radius:2px;padding:0 1px}}
/* Pagination */
.pg{{display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin:16px 0;font-size:.8rem}}
.pg-btn{{padding:5px 12px;border:1px solid var(--border);border-radius:6px;color:var(--fg);
  text-decoration:none;transition:all .15s}}
.pg-btn:hover{{border-color:var(--link);color:var(--link);text-decoration:none}}
.pg-cur{{background:var(--link);color:var(--bg);border-color:var(--link);font-weight:600}}
.pg-cur:hover{{color:var(--bg)}}
.pg-dis{{opacity:.3;pointer-events:none}}
.pg-info{{margin-left:auto;color:var(--muted)}}
/* Thread */
.thread{{display:flex;flex-direction:column;gap:20px}}
/* Assistant turn body (no avatar — matches AmpCode) */
.turn-body{{display:flex;flex-direction:column;gap:8px;min-width:0}}
/* User messages */
.user-msg{{display:grid;grid-template-columns:28px 1fr;gap:10px;align-items:start}}
.u-av{{width:28px;height:28px;border-radius:50%;overflow:hidden;flex-shrink:0;margin-top:2px;
  border:1px solid var(--border)}}
.u-av img{{width:100%;height:100%;object-fit:cover;display:block}}
.ts{{font-size:.65rem;font-weight:400;color:var(--muted);float:right;margin-left:8px;margin-top:4px}}
.u-body{{min-width:0}}
.u-name{{display:block;font-size:.7rem;font-weight:600;color:var(--muted);margin-bottom:2px;text-transform:capitalize}}
.u-text{{white-space:pre-wrap;word-break:break-word;font-size:1rem;line-height:1.6}}
/* Assistant text (rendered by marked.js) */
.a-text{{font-size:1rem;line-height:1.6;word-break:break-word}}
.a-text p{{margin:.5em 0}}
.a-text ul,.a-text ol{{padding-left:1.5rem;margin:.5em 0}}
.a-text li{{margin:.3em 0}}
.a-text strong{{font-weight:600}}
.a-text h1{{font-size:1.4em;font-weight:600;margin:.8em 0 .4em}}
.a-text h2{{font-size:1.2em;font-weight:600;margin:.7em 0 .3em}}
.a-text h3{{font-size:1.1em;font-weight:600;margin:.6em 0 .2em}}
.a-text blockquote{{border-left:3px solid var(--border);padding-left:12px;color:var(--muted);margin:.5em 0}}
.a-text .table-wrap{{overflow-x:auto;margin:.75em 0}}
.a-text .table-wrap table{{margin:0}}
.a-text table{{border-collapse:collapse;box-shadow:0 0 0 1px var(--border);border-radius:.25rem;overflow:hidden;margin:.75em 0;font-size:.93em}}
.a-text thead{{background:color-mix(in srgb,var(--muted) 20%,transparent)}}
.a-text th{{text-align:left;font-weight:600;border-bottom:1px solid var(--border);border-right:1px solid var(--border);padding:.375rem .5rem;white-space:nowrap}}
.a-text th:last-child{{border-right:none}}
.a-text td{{border-bottom:1px solid var(--border);border-right:1px solid var(--border);padding:.375rem .5rem;white-space:nowrap}}
.a-text td:last-child{{border-right:none}}
.a-text tbody tr:last-child td{{border-bottom:none}}
.a-text tbody tr:hover{{background:color-mix(in srgb,var(--muted) 15%,transparent)}}
.a-text a{{color:var(--link)}}
.a-text img{{max-width:100%;border-radius:8px}}
/* Code (hljs themed) */
.a-text pre{{background:var(--code-bg);border:1px solid var(--border);border-radius:6px;
  padding:12px 14px;overflow-x:auto;font-family:var(--mono);font-size:.8rem;line-height:1.6;margin:.6em 0}}
.a-text pre code{{background:none!important;padding:0!important;font-size:inherit}}
.a-text code{{background:var(--code-bg);padding:2px 6px;border-radius:4px;
  font-family:var(--mono);font-size:.85em}}
.a-text pre code{{background:none;padding:0;border-radius:0}}
.hljs{{background:transparent!important;padding:0!important}}
/* Copy button on code blocks */
.a-text pre{{position:relative}}
.copy-btn{{position:absolute;top:6px;right:6px;padding:3px 8px;border:1px solid var(--border);
  border-radius:4px;background:var(--bg);color:var(--muted);cursor:pointer;font-size:.65rem;
  opacity:0;transition:opacity .15s;font-family:var(--sans)}}
.a-text pre:hover .copy-btn{{opacity:1}}
.copy-btn:hover{{color:var(--fg);border-color:var(--muted)}}
.copy-btn.copied{{color:var(--green);border-color:var(--green)}}
/* Tool chips */
.chip{{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:6px;
  border:1px solid var(--border);background:var(--card);font-size:.875rem;font-weight:400;overflow:hidden;
  transition:border-color .15s;width:fit-content}}
.chip:hover{{border-color:var(--muted)}}
.t-icon{{flex-shrink:0;width:14px;height:14px;color:var(--muted);opacity:.8}}
.t-det{{color:var(--fg);font-family:var(--mono);font-size:.8rem;font-weight:400;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}}
.fp{{font-family:var(--mono);font-size:.8rem;font-weight:400}}
.fp-dir{{opacity:.6}}
/* Action blocks (Bash, expandable tools) */
.act{{border-radius:6px;border:1px solid var(--border);overflow:hidden}}
.act-h{{display:flex;align-items:center;gap:6px;padding:6px 8px;background:var(--card);
  font-size:.875rem;font-weight:400;cursor:pointer;user-select:none;list-style:none;transition:background .1s}}
.act-h::-webkit-details-marker{{display:none}}
.act-h:hover{{background:var(--user-bg)}}
.act-h svg{{width:14px;height:14px;color:var(--muted);flex-shrink:0}}
.act-body{{border-top:1px solid var(--border);padding:0;font-family:var(--mono);
  font-size:.75rem;line-height:1.6;white-space:pre-wrap;word-break:break-all;
  color:var(--muted);background:var(--code-bg)}}
.act-cmd{{padding:8px 12px;color:var(--fg)}}
.act-out{{padding:8px 12px;border-top:1px solid var(--border);color:var(--muted)}}
.act-out-err{{color:var(--red)}}
.act-err>.act-h .chev{{color:#bd2b2b}}
.act-body .t-out{{padding:8px 12px;white-space:pre-wrap;word-break:break-word;font-size:.75rem;
  color:var(--muted);font-family:var(--mono);line-height:1.6;border:0}}
/* Diff display */
.diff-act .act-h{{gap:8px}}
.diff-body{{border-top:1px solid var(--border);padding:0;font-family:var(--mono);
  font-size:.75rem;line-height:1.7;overflow-x:auto;background:var(--code-bg)}}
.diff-add,.diff-del,.diff-ctx{{padding:0 12px 0 0;white-space:pre;display:flex}}
.diff-add{{background:var(--diff-add-bg);color:var(--diff-add-fg)}}
.diff-del{{background:var(--diff-del-bg);color:var(--diff-del-fg)}}
.diff-ctx{{color:var(--muted)}}
.diff-ln{{display:inline-block;width:36px;text-align:right;padding-right:8px;color:var(--muted);
  opacity:.5;user-select:none;flex-shrink:0}}
.diff-sign{{display:inline-block;width:16px;text-align:center;flex-shrink:0;font-weight:600}}
.diff-stat{{display:inline-flex;gap:6px;margin-left:auto;font-family:var(--mono);font-size:.7rem}}
.diff-plus{{color:var(--green)}}.diff-minus{{color:var(--red)}}.diff-mod{{color:#f59e0b}}
/* Thinking */
.think{{border-radius:6px;border:1px solid transparent;background:var(--card)}}
.think-h{{display:flex;align-items:center;gap:4px;padding:6px 10px;cursor:pointer;
  user-select:none;color:var(--muted);font-size:.8rem;list-style:none;transition:color .1s}}
.think-h::-webkit-details-marker{{display:none}}
.think-h:hover{{color:var(--fg)}}
.think-t{{padding:10px 12px;white-space:pre-wrap;word-break:break-word;font-size:.8rem;
  color:var(--muted);font-family:var(--sans);font-style:italic;line-height:1.6}}
/* (tool outputs merged into tool_use blocks) */
/* Chevrons */
.chev{{width:14px;height:14px;transition:transform .15s ease;flex-shrink:0}}
.act-h .chev{{margin-left:auto}}
details[open] .chev{{transform:rotate(90deg)}}
/* Jump buttons */
.jump{{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:6px;z-index:50}}
.jump a{{width:36px;height:36px;border-radius:50%;border:1px solid var(--border);
  background:var(--bg);display:flex;align-items:center;justify-content:center;
  color:var(--muted);text-decoration:none;font-size:1.1rem;transition:all .15s;
  box-shadow:0 2px 8px rgba(0,0,0,.15)}}
.jump a:hover{{border-color:var(--link);color:var(--link)}}
a{{color:var(--link);text-decoration:none}}
a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="wrap">
<div class="main">
<div class="content">
<header>
<div class="h-row"><h1>{esc(name)}</h1><button class="sb-toggle" onclick="document.querySelector('.sidebar').classList.toggle('sb-open')" title="Session info"><svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16"><path d="M0 8a8 8 0 1116 0A8 8 0 010 8zm8-6.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM6.5 7.75A.75.75 0 017.25 7h1a.75.75 0 01.75.75v2.75h.25a.75.75 0 010 1.5h-2a.75.75 0 010-1.5h.25v-2h-.25a.75.75 0 01-.75-.75zM8 6a1 1 0 110-2 1 1 0 010 2z"/></svg></button></div>
<div class="meta">
{"<span class='mi'><svg viewBox=\"0 0 16 16\" fill=\"currentColor\"><path d=\"M8 16A8 8 0 108 0a8 8 0 000 16zm.25-11.75v4l3 1.5-.5 1-3.5-1.75v-4.75h1z\"/></svg>" + esc(stats["first_ts"][:10]) + "</span>" if stats["first_ts"] else ""}
{"<span class='mi'><svg viewBox='0 0 16 16' fill='currentColor'><path d='M8 1.5c-2.363 0-4 1.69-4 3.75 0 .984.424 1.625.984 2.304l.214.253c.223.264.47.556.673.848.284.411.537.896.621 1.49a.75.75 0 01-1.484.211c-.04-.282-.163-.547-.37-.847a8.456 8.456 0 00-.542-.68c-.084-.1-.173-.205-.268-.32C3.201 7.75 2.5 6.766 2.5 5.25 2.5 2.31 4.863 0 8 0s5.5 2.31 5.5 5.25c0 1.516-.701 2.5-1.328 3.259-.095.115-.184.22-.268.319-.207.245-.383.453-.541.681-.208.3-.33.565-.37.847a.75.75 0 01-1.485-.212c.084-.593.337-1.078.621-1.489.203-.292.45-.584.673-.848l.213-.253c.561-.679.985-1.32.985-2.304 0-2.06-1.637-3.75-4-3.75zM6 15.25a.75.75 0 01.75-.75h2.5a.75.75 0 010 1.5h-2.5a.75.75 0 01-.75-.75zM5.75 12a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-4.5z'/></svg>" + esc(stats["model"]) + "</span>" if stats["model"] else ""}
{"<span class='mi'><svg viewBox='0 0 16 16' fill='currentColor'><path d='M11.93 8.5a4.002 4.002 0 01-7.86 0H.75a.75.75 0 010-1.5h3.32a4.002 4.002 0 017.86 0h3.32a.75.75 0 010 1.5h-3.32zm-1.43-.75a2.5 2.5 0 10-5 0 2.5 2.5 0 005 0z'/></svg>" + esc(stats["git_branch"]) + "</span>" if stats["git_branch"] else ""}
<a class="mi" href="{prompts_filter_url}" style="cursor:pointer" title="Filter to prompts only"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0110.25 10H7.061l-2.574 2.573A1.458 1.458 0 012 11.543V10h-.25A1.75 1.75 0 010 8.25v-5.5C0 1.784.784 1 1.75 1z"/></svg>{stats["n_user"]} prompts</a>
<span class="mi"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304z"/></svg>{stats["n_tool"]} tool call{"s" if stats["n_tool"] != 1 else ""}</span>
</div>
</header>
<form class="search-bar" method="get">
<input type="text" name="q" placeholder="Search transcript…" value="{search_val}">
<button type="submit">Search</button>
{"<input type='hidden' name='token' value='" + esc(token) + "'>" if token else ""}
{"<input type='hidden' name='sid' value='" + esc(session_id) + "'>" if session_id else ""}
{"<input type='hidden' name='per_page' value='" + str(per_page) + "'>" if per_page != 50 else ""}
{"<input type='hidden' name='filter' value='prompts'>" if filter_mode == "prompts" else ""}
</form>
{filter_banner}
{search_result}
{nav_html}
<div class="thread" id="thread"{' data-search="' + esc(search_query) + '"' if search_query else ''}>
{"".join(blocks)}
</div>
{nav_html}
</div>
<aside class="sidebar"><div class="sidebar-inner">
<div class="sb-title">Session Info</div>
<div class="sb-row"><span class="sb-label">Session</span><span class="sb-val">{esc(sid[:12])}</span></div>
{"<div class='sb-row'><span class='sb-label'>Model</span><span class='sb-val'>" + esc(_format_model_name(stats["model"])) + "</span></div>" if stats["model"] else ""}
{"<div class='sb-row'><span class='sb-label'>Version</span><span class='sb-val'>" + esc(stats["version"]) + "</span></div>" if stats["version"] else ""}
{"<div class='sb-row'><span class='sb-label'>Branch</span><span class='sb-val'>" + esc(stats["git_branch"]) + "</span></div>" if stats["git_branch"] else ""}
<div class="sb-divider"></div>
<div class="sb-row"><span class="sb-label">Prompts</span><span class="sb-val">{stats["n_user"]}</span></div>
<div class="sb-row"><span class="sb-label">Tool calls</span><span class="sb-val">{stats["n_tool"]}</span></div>
{"<div class='sb-row'><span class='sb-label'>Edits</span><span class='sb-val'>" + str(stats["n_edit"]) + "</span></div>" if stats["n_edit"] else ""}
{"<div class='sb-row'><span class='sb-label'>Files touched</span><span class='sb-val'>" + str(stats["n_files"]) + "</span></div>" if stats["n_files"] else ""}
{"<div class='sb-divider'></div><div class='sb-lines'><span class='plus'>+" + str(stats["lines_add"]) + "</span><span class='minus'>-" + str(stats["lines_del"]) + "</span><span class='mod'>~" + str(stats["lines_mod"]) + "</span></div>" if stats["lines_add"] or stats["lines_del"] or stats["lines_mod"] else ""}
{"<div class='sb-divider'></div>" if stats["duration"] or file_size_str or stats["input_tokens"] else ""}
{"<div class='sb-row'><span class='sb-label'>Duration</span><span class='sb-val'>" + esc(stats["duration"]) + "</span></div>" if stats["duration"] else ""}
{"<div class='sb-row'><span class='sb-label'>File size</span><span class='sb-val'>" + esc(file_size_str) + "</span></div>" if file_size_str else ""}
{"<div class='sb-row'><span class='sb-label'>Input tokens</span><span class='sb-val'>" + f'{stats["input_tokens"]:,}' + "</span></div>" if stats["input_tokens"] else ""}
{"<div class='sb-row'><span class='sb-label'>Output tokens</span><span class='sb-val'>" + f'{stats["output_tokens"]:,}' + "</span></div>" if stats["output_tokens"] else ""}
<div class="sb-divider"></div>
<div class="sb-row"><span class="sb-label">Total entries</span><span class="sb-val">{total}</span></div>
<div class="sb-row"><span class="sb-label">Page</span><span class="sb-val">{page}/{total_pages}</span></div>
</div></aside>
</div>
</div>
<div class="jump">
<a href="#" title="Top" onclick="window.scrollTo(0,0);return false">↑</a>
<a href="#" title="Bottom" onclick="window.scrollTo(0,document.body.scrollHeight);return false">↓</a>
</div>
<script>
// Render markdown blocks with marked.js + highlight.js
marked.setOptions({{
  highlight: function(code, lang) {{
    if (lang && hljs.getLanguage(lang)) {{
      return hljs.highlight(code, {{language: lang}}).value;
    }}
    return hljs.highlightAuto(code).value;
  }},
  breaks: true,
  gfm: true
}});
function decodeB64Utf8(b64) {{
  var bin = atob(b64);
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder('utf-8').decode(bytes);
}}
document.querySelectorAll('.markdown[data-md]').forEach(function(el) {{
  try {{
    var md = decodeB64Utf8(el.getAttribute('data-md'));
    el.innerHTML = marked.parse(md);
  }} catch(e) {{
    el.textContent = 'Error rendering markdown: ' + e.message;
  }}
}});
// Wrap tables in scroll containers
document.querySelectorAll('.a-text table').forEach(function(table) {{
  var wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  table.parentNode.insertBefore(wrap, table);
  wrap.appendChild(table);
}});
// Add copy buttons to code blocks
document.querySelectorAll('.a-text pre').forEach(function(pre) {{
  var btn = document.createElement('button');
  btn.className = 'copy-btn';
  btn.textContent = 'Copy';
  btn.onclick = function() {{
    var code = pre.querySelector('code');
    navigator.clipboard.writeText(code ? code.textContent : pre.textContent).then(function() {{
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(function() {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 2000);
    }});
  }};
  pre.appendChild(btn);
}});
// Keyboard shortcuts
document.addEventListener('keydown', function(e) {{
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '/') {{ e.preventDefault(); document.querySelector('.search-bar input').focus(); }}
  if (e.key === 'Home') {{ window.scrollTo(0,0); }}
  if (e.key === 'End') {{ window.scrollTo(0,document.body.scrollHeight); }}
}});
// Highlight search terms in thread content
(function() {{
  var thread = document.getElementById('thread');
  var q = thread && thread.getAttribute('data-search');
  if (!q) return;
  var terms = q.split(/\s+/).filter(function(t) {{ return t.length > 0; }});
  if (!terms.length) return;
  var pattern = new RegExp('(' + terms.map(function(t) {{
    return t.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
  }}).join('|') + ')', 'gi');
  function walk(node) {{
    if (node.nodeType === 3) {{
      var text = node.textContent;
      if (!pattern.test(text)) return;
      pattern.lastIndex = 0;
      var frag = document.createDocumentFragment();
      var last = 0;
      var match;
      while ((match = pattern.exec(text)) !== null) {{
        if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
        var mark = document.createElement('mark');
        mark.textContent = match[0];
        frag.appendChild(mark);
        last = pattern.lastIndex;
      }}
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    }} else if (node.nodeType === 1 && !/^(script|style|mark|code|pre)$/i.test(node.tagName)) {{
      var children = Array.from(node.childNodes);
      for (var i = 0; i < children.length; i++) walk(children[i]);
    }}
  }}
  // Highlight in user messages and assistant text
  thread.querySelectorAll('.u-text, .a-text').forEach(function(el) {{ walk(el); }});
}})();
// Render timestamps in browser timezone
var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
document.querySelectorAll('.ts[data-ts]').forEach(function(el) {{
  try {{
    var d = new Date(el.getAttribute('data-ts'));
    if (!isNaN(d)) {{
      var mon = months[d.getMonth()];
      var day = d.getDate();
      var h = String(d.getHours()).padStart(2,'0');
      var m = String(d.getMinutes()).padStart(2,'0');
      el.textContent = mon + ' ' + day + ' ' + h + ':' + m;
    }}
  }} catch(e) {{}}
}});
</script>
</body>
</html>'''
    return page_html


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

    def _send_html(self, body: bytes, status: int = 200):
        """Send HTML response, gzip-compressed if client supports it."""
        accept = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept and len(body) > 1024:
            import gzip as _gzip
            compressed = _gzip.compress(body, compresslevel=6)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)
        else:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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

        if self.path == "/send":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_send_endpoint(body)
            return

        # PR comment endpoint
        if self.path == "/pr-comment":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_pr_comment(body)
            return

        if self.path == "/pr-general-comment":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_pr_general_comment(body)
            return

        if self.path == "/pr-merge":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_pr_merge(body)
            return

        if self.path == "/register":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_forge_register(body)
            return

        if self.path == "/health-alert":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.handle_health_alert(body)
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
        # Respond 200 immediately so Telegram gets the ACK fast
        # (prevents missing read receipts and webhook retries during
        # slow operations like remote restart which blocks 30-60s).
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
        try:
            update = json.loads(body)
            # Debug: show what update type we received
            update_types = [k for k in update.keys() if k != "update_id"]
            if update_types and update_types[0] != "message":
                print(f"Received update type: {update_types}")
            if "message" in update:
                threading.Thread(
                    target=command_router.handle_message,
                    args=(update,),
                    daemon=True,
                ).start()
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

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
                result = transport.send_text(chat_id, text)
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

    def handle_health_alert(self, body: bytes = b""):
        """Handle JSONL health alerts from stop hook.

        POST /health-alert — hook reports a worker's JSONL transcript is stale.
        Body: {"worker": "name", "issue": "jsonl_stale", "transcript_age": 3600, ...}
        Sends a one-time Telegram alert to admin so they can restart the worker.
        """
        try:
            data = json.loads(body) if body else {}
            worker = data.get("worker", "unknown")
            issue = data.get("issue", "unknown")
            age = data.get("transcript_age", 0)

            age_human = f"{age // 3600}h{(age % 3600) // 60}m" if age >= 3600 else f"{age // 60}m"
            alert_text = f"🔴 {worker}: JSONL transcript stale ({age_human}). Session active but not recording. `/restart {worker}` to fix."
            print(f"Health alert: {worker} — {issue} (age={age}s)")

            chat_ids = get_all_chat_ids()
            for chat_id in chat_ids:
                transport.send_text(chat_id, alert_text)

            self._send_json(200, {"ok": True})
        except Exception as e:
            print(f"Health alert error: {e}")
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_forge_register(self, body: bytes = b""):
        """Accept registration from forge-built worker binaries.

        POST /register — worker announces itself to the bridge.
        Body: {"Name": "workerName", "Host": "hostname", "Version": "1.0.0", "Tools": {...}}
        Callback workers may include {"callback_url": "http://host:port"}.
        Response: {"ok": true}
        """
        try:
            data = json.loads(body) if body else {}
            name = data.get("Name", data.get("name", ""))
            host = data.get("Host", data.get("host", ""))
            version = data.get("Version", data.get("version", ""))
            callback_url = data.get("CallbackURL", data.get("callback_url", data.get("callbackUrl", "")))
            tools = data.get("Tools", data.get("tools", {}))
            if name:
                if callback_url:
                    _registry_add_callback(name, callback_url, host=host, version=version, tools=tools)
                    print(f"Callback worker registered: {name} (host={host}, url={callback_url}, version={version})")
                else:
                    _registry_add(name, DEFAULT_BACKEND, host=host)
                    print(f"Forge worker registered: {name} (host={host}, version={version})")
            tmux_session = f"{TMUX_PREFIX}{name}" if name else ""
            import subprocess as _sp
            conflict = False
            active_workers = []
            try:
                r = _sp.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    active_workers = [s.removeprefix(TMUX_PREFIX)
                                      for s in r.stdout.strip().split("\n")
                                      if s.startswith(TMUX_PREFIX)]
                    conflict = name in active_workers if name else False
            except Exception:
                pass
            self._send_json(200, {
                "ok": True,
                "settings": {
                    "tmux_prefix": TMUX_PREFIX,
                    "node_name": NODE_NAME or "",
                    "tmux_session": tmux_session,
                },
                "conflict": conflict,
                "active_workers": active_workers,
            })
        except Exception as e:
            print(f"Register error: {e}")
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_send_endpoint(self, body: bytes = b""):
        """Send a prompt to a worker.

        POST /send
        Body: {"worker": "name", "message": "text", "from": "system"}
        The "from" field (default "system") is prefixed to the message.
        """
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        worker = str(data.get("worker", "")).strip()
        message = data.get("message", data.get("text", ""))
        sender = str(data.get("from", "system")).strip() or "system"
        if not worker:
            self._send_json(400, {"ok": False, "error": "Missing worker"})
            return
        if not isinstance(message, str) or not message.strip():
            self._send_json(400, {"ok": False, "error": "Missing message"})
            return

        prefixed = f"{sender}: {message}"
        delivered = send_to_worker(worker, prefixed)
        status = 200 if delivered else 404
        self._send_json(status, {
            "ok": delivered,
            "worker": worker,
            "delivered": delivered,
            "error": None if delivered else "Worker not found or not reachable",
        })

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

            # Debug: log short messages to trace source of empty "name:" messages
            if len(text.strip()) <= 5:
                source_ip = self.client_address[0] if self.client_address else "unknown"
                hook_sid = data.get("session_id", "")
                escape_flag = data.get("escape", False)
                print(f"Hook response DEBUG: {session_name} -> chat {chat_id}, "
                      f"text={repr(text)}, len={len(text)}, "
                      f"source={data.get('source', 'hook')}, "
                      f"session_id={hook_sid[:12] if hook_sid else 'none'}, "
                      f"escape={escape_flag}, ip={source_ip}")

            print(f"Hook response: {session_name} -> chat {chat_id} ({len(text)} chars)")

            # Update session ID cache if provided (keeps VPS in sync with remote workers)
            hook_sid = data.get("session_id", "")
            if hook_sid:
                try:
                    sid_file = ensure_session_dir(session_name) / "claude_session_id"
                    old_sid = sid_file.read_text().strip() if sid_file.exists() else ""
                    if old_sid != hook_sid:
                        cwd = get_claude_session_cwd(session_name)
                        _log_session_event(session_name, hook_sid, cwd, "hook")
                        sid_file.write_text(hook_sid)
                        sid_file.chmod(0o600)
                except Exception:
                    pass

            # Send response using shared helper
            send_response_to_telegram(session_name, text, int(chat_id), log_prefix="Response")

            # Learning reminder check (non-blocking)
            _check_learning_reminder(session_name)

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
            self.handle_workers_endpoint(parsed)
            return

        # Handle /checkin endpoint for worker instruction refresh
        if parsed.path == "/checkin":
            self.handle_checkin_endpoint(parsed)
            return
        if parsed.path == "/health/workers":
            self.handle_health_workers_endpoint()
            return

        # Handle /transcript/<name> endpoint
        if parsed.path.startswith("/transcript/"):
            self.handle_transcript_endpoint(parsed)
            return

        # Handle /team-chat-media/<path> endpoint (photos/files from chat export)
        # Must be checked BEFORE /team-chat to avoid prefix collision
        if parsed.path.startswith("/team-chat-media/"):
            self.handle_team_chat_media(parsed)
            return

        # Handle /team-chat endpoint
        if parsed.path.startswith("/team-chat"):
            self.handle_team_chat_endpoint(parsed)
            return

        # Handle /pr-file-content (lazy fetch for expand context)
        if parsed.path == "/pr-file-content":
            self.handle_pr_file_content(parsed)
            return

        if parsed.path == "/pr-keepalive":
            self.handle_pr_keepalive(parsed)
            return

        # Handle /pr-review/<pr_num> endpoint
        if parsed.path.startswith("/pr-review/"):
            self.handle_pr_review_endpoint(parsed)
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

    def handle_workers_endpoint(self, parsed=None):
        """Return list of active workers with communication details.

        GET /workers                 — bridge-POV send_example (legacy)
        GET /workers?from=<name>     — caller-POV send_example, wraps ssh if cross-machine
        Response: {"workers": [{"name": ..., "machine": ..., "protocol": ..., "address": ..., "send_example": ...}, ...]}
        """
        try:
            caller_from = None
            if parsed is not None:
                caller_from = parse_qs(parsed.query).get("from", [None])[0]
            workers = get_workers(caller_from=caller_from)
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
                # Teleported workers have remote cwds — validate on their host
                worker_host = get_worker_host(name)
                requested_cwd, cwd_err = validate_cwd(raw_cwd, host=worker_host)
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
                old_cwd = get_claude_session_cwd(name)
                save_claude_session_cwd(name, requested_cwd)
                if old_cwd and old_cwd.rstrip("/") != requested_cwd.rstrip("/"):
                    old_sid = get_claude_session_id(name)
                    _log_session_event(name, old_sid or "(none)", requested_cwd, "cwd_change")
                    clear_claude_session_id(name)
                    print(f"[checkin] {name}: CWD changed ({old_cwd} -> {requested_cwd}), cleared stale session_id")
                print(f"[checkin] {name}: requested_cwd={requested_cwd}, tmux={tmux_name}, host={host}")
                if tmux_name and tmux_exists(tmux_name, host=host):
                    pane_cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
                    # Compare normalized paths (don't use os.path.realpath — it resolves on VPS, not remote)
                    same_cwd = pane_cwd and pane_cwd.rstrip("/") == requested_cwd.rstrip("/")
                    print(f"[checkin] {name}: pane_cwd={pane_cwd}, same_cwd={same_cwd}")
                    if not same_cwd:
                        notify_chat_id = get_manager_chat_id(name)

                        # Cooldown: prevent restart loops from repeated checkins
                        last_restart = _recent_restarts.get(name, 0)
                        elapsed = time.time() - last_restart
                        if elapsed < RESTART_COOLDOWN:
                            # Narrow exemption: allow one CWD repair after force restart
                            if _force_restart_pending_cwd.pop(name, False):
                                print(f"[checkin] {name}: cooldown bypassed (post-force CWD repair)")
                            else:
                                print(f"[checkin] {name}: BLOCKED restart (cooldown {elapsed:.0f}s < {RESTART_COOLDOWN}s)")
                                msg = (f"Checkin restart blocked: {name} was restarted {elapsed:.0f}s ago "
                                       f"(cooldown {RESTART_COOLDOWN}s). CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")
                                if notify_chat_id is not None:
                                    send_telegram_message(notify_chat_id, msg)
                                self.send_response(200)
                                self.send_header("Content-Type", "text/plain")
                                self.end_headers()
                                self.wfile.write(msg.encode())
                                return

                        # Guard: skip if worker is already running Claude
                        if is_claude_running(tmux_name, host=host):
                            print(f"[checkin] {name}: BLOCKED restart (Claude already running in tmux)")
                            msg = (f"Checkin restart skipped: {name} has Claude running. "
                                   f"CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")
                            if notify_chat_id is not None:
                                send_telegram_message(notify_chat_id, msg)
                            self.send_response(200)
                            self.send_header("Content-Type", "text/plain")
                            self.end_headers()
                            self.wfile.write(msg.encode())
                            return

                        # In-flight dedupe: skip if restart already in progress
                        with _restart_lock:
                            inflight_ts = _restart_in_progress.get(name)
                            if inflight_ts and time.time() - inflight_ts < 120:
                                print(f"[checkin] {name}: BLOCKED restart (in-flight since {time.time() - inflight_ts:.0f}s ago)")
                                msg = f"Checkin restart blocked: {name} restart already in progress ({time.time() - inflight_ts:.0f}s)."
                                if notify_chat_id is not None:
                                    send_telegram_message(notify_chat_id, msg)
                                self.send_response(200)
                                self.send_header("Content-Type", "text/plain")
                                self.end_headers()
                                self.wfile.write(msg.encode())
                                return
                            _restart_in_progress[name] = time.time()

                        print(f"[checkin] {name}: triggering restart (cwd mismatch: pane={pane_cwd} vs requested={requested_cwd})")
                        try:
                            notify_chat_id = get_manager_chat_id(name)
                            if notify_chat_id is not None:
                                send_telegram_message(
                                    notify_chat_id,
                                    f"{name} is restarting in a new directory. "
                                    "Messages during restart may be lost.",
                                )

                            if host:
                                # Teleported worker: use remote restart
                                backend_obj_r = get_backend(backend_name)
                                ok, err = command_router._restart_remote_worker(
                                    name, backend_name, backend_obj_r, tmux_name, host, "relaunch")
                            else:
                                ok, err = worker_manager.restart(name, mode="relaunch")

                            _recent_restarts[name] = time.time()
                            print(f"[checkin] {name}: restart result ok={ok}, err={err}")

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
                        finally:
                            with _restart_lock:
                                _restart_in_progress.pop(name, None)

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

    def handle_pr_file_content(self, parsed):
        """Fetch file content from GitHub for diff context expansion."""
        import time as _time
        import base64 as _b64
        params = dict(parse_qs(parsed.query))
        token = params.get("token", [None])[0]

        now = _time.time()
        if not token or token not in PR_REVIEW_TOKENS or PR_REVIEW_TOKENS[token]["expires_at"] <= now:
            self.send_response(403)
            self.end_headers()
            return

        PR_REVIEW_TOKENS[token]["expires_at"] = now + 300

        owner = params.get("owner", [None])[0]
        repo = params.get("repo", [None])[0]
        path = params.get("path", [None])[0]
        ref = params.get("ref", [None])[0]

        if not all([owner, repo, path, ref]):
            self.send_response(400)
            self.end_headers()
            return

        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}",
                 "--jq", ".content"],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                self.send_response(404)
                self.end_headers()
                return
            raw = _b64.b64decode(r.stdout.strip()).decode('utf-8', errors='replace')
            lines = raw.splitlines()
            body = json.dumps(lines, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except subprocess.TimeoutExpired:
            self.send_response(504)
            self.end_headers()
        except Exception:
            self.send_response(500)
            self.end_headers()

    def handle_pr_keepalive(self, parsed):
        """Extend PR review token expiry on client activity."""
        import time as _time
        params = dict(parse_qs(parsed.query))
        token = params.get("token", [None])[0]
        now = _time.time()
        if not token or token not in PR_REVIEW_TOKENS or PR_REVIEW_TOKENS[token]["expires_at"] <= now:
            self.send_response(403)
            self.end_headers()
            return
        PR_REVIEW_TOKENS[token]["expires_at"] = now + 300
        self.send_response(204)
        self.end_headers()

    def handle_pr_general_comment(self, body: bytes):
        """Post a general (non-inline) comment on a PR via GitHub API."""
        import time as _time
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        token = data.get("token", "")
        now = _time.time()
        if not token or token not in PR_REVIEW_TOKENS or PR_REVIEW_TOKENS[token].get("expires_at", 0) <= now:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Token expired")
            return
        PR_REVIEW_TOKENS[token]["expires_at"] = now + 300

        owner = data.get("owner", "")
        repo = data.get("repo", "")
        pr_num = data.get("pr_num", 0)
        comment_body = data.get("body", "").strip()
        if not all([owner, repo, pr_num, comment_body]):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing required fields")
            return

        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/issues/{pr_num}/comments",
                 "--method", "POST", "-f", f"body={comment_body}"],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                self.send_response(502)
                self.end_headers()
                self.wfile.write(f"GitHub API error: {r.stderr[:200]}".encode())
                return
        except subprocess.TimeoutExpired:
            self.send_response(504)
            self.end_headers()
            self.wfile.write(b"GitHub API timeout")
            return

        # Notify Telegram
        try:
            notify_text = f"\U0001f4ac PR #{pr_num} comment:\n{comment_body[:500]}"
            import urllib.request
            req = urllib.request.Request(
                f"{BRIDGE_PUBLIC_URL or f'http://localhost:{PORT}'}/notify",
                data=json.dumps({"text": notify_text}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

        # Route to workers via @mentions
        targets, _ = command_router.parse_at_mentions(comment_body)
        if targets:
            worker_msg = (
                f"manager: PR #{pr_num} review comment\n\n"
                f"{comment_body}"
            )
            for t in targets:
                send_to_worker(t, worker_msg)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def handle_pr_merge(self, body: bytes):
        """Merge a PR via GitHub API."""
        import time as _time
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        token = data.get("token", "")
        now = _time.time()
        if not token or token not in PR_REVIEW_TOKENS or PR_REVIEW_TOKENS[token].get("expires_at", 0) <= now:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Token expired")
            return
        PR_REVIEW_TOKENS[token]["expires_at"] = now + 300

        owner = data.get("owner", "")
        repo = data.get("repo", "")
        pr_num = data.get("pr_num", 0)
        merge_method = data.get("merge_method", "merge")
        if merge_method not in ("merge", "squash", "rebase"):
            merge_method = "merge"

        if not all([owner, repo, pr_num]):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing required fields")
            return

        try:
            r = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/merge",
                 "--method", "PUT", "-f", f"merge_method={merge_method}"],
                capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                err = r.stderr.strip()[:300] or r.stdout.strip()[:300]
                self.send_response(502)
                self.end_headers()
                self.wfile.write(f"Merge failed: {err}".encode())
                return
        except subprocess.TimeoutExpired:
            self.send_response(504)
            self.end_headers()
            self.wfile.write(b"Merge API timeout")
            return

        # Notify Telegram
        try:
            notify_text = f"\u2705 PR #{pr_num} merged ({merge_method}) via review page"
            import urllib.request
            req = urllib.request.Request(
                f"{BRIDGE_PUBLIC_URL or f'http://localhost:{PORT}'}/notify",
                data=json.dumps({"text": notify_text}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def handle_pr_review_endpoint(self, parsed):
        """Serve generated PR review HTML.

        Requires a valid token (?token=...) generated by /pr command.
        Token expires after 5 minutes (same as rewind).
        """
        import time as _time
        params = dict(parse_qs(parsed.query))
        token = params.get("token", [None])[0]

        # Cleanup expired tokens
        now = _time.time()
        expired = [k for k, v in PR_REVIEW_TOKENS.items() if v["expires_at"] <= now]
        for k in expired:
            del PR_REVIEW_TOKENS[k]

        if not token or token not in PR_REVIEW_TOKENS:
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Link expired</h2><p>Send <code>/pr &lt;url&gt;</code> in Telegram to get a fresh 5-minute link.</p>")
            return

        info = PR_REVIEW_TOKENS[token]
        pr_num = info["pr_num"]
        html_path = f"/tmp/pr-review-{pr_num}.html"

        if not os.path.exists(html_path):
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<h2>PR review not found</h2><p>File {html_path} missing. Re-run /pr command.</p>".encode())
            return

        with open(html_path, "rb") as f:
            self._send_html(f.read())

    def handle_pr_comment(self, body: bytes):
        """Post an inline comment on a PR via GitHub API + notify Telegram."""
        import time as _time
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        token = data.get("token", "")
        now = _time.time()
        expired = [k for k, v in PR_REVIEW_TOKENS.items() if v["expires_at"] <= now]
        for k in expired:
            del PR_REVIEW_TOKENS[k]
        if not token or token not in PR_REVIEW_TOKENS:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Token expired - reload the PR review page")
            return

        # Extend token expiry on use
        PR_REVIEW_TOKENS[token]["expires_at"] = now + 300

        owner = data.get("owner", "")
        repo = data.get("repo", "")
        pr_num = data.get("pr_num", 0)
        path = data.get("path", "")
        line = data.get("line", 0)
        side = data.get("side", "RIGHT")
        comment_body = data.get("body", "").strip()
        head_sha = data.get("head_sha", "")

        if not all([owner, repo, pr_num, path, line, comment_body, head_sha]):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing required fields")
            return

        # Post to GitHub via gh api
        try:
            gh_payload = json.dumps({
                "body": comment_body,
                "commit_id": head_sha,
                "path": path,
                "line": line,
                "side": side,
            })
            r = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/comments",
                 "--method", "POST", "--input", "-"],
                input=gh_payload, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                err = r.stderr.strip() or r.stdout.strip()
                print(f"[pr-comment] GitHub API error: {err}")
                self.send_response(502)
                self.end_headers()
                self.wfile.write(f"GitHub API error: {err}".encode())
                return
        except subprocess.TimeoutExpired:
            self.send_response(504)
            self.end_headers()
            self.wfile.write(b"GitHub API timeout")
            return

        # Send to Telegram as manager notification
        if admin_chat_id:
            tg_text = (
                f"\U0001f4ac PR #{pr_num} comment\n"
                f"{path}:{line}\n\n"
                f"{comment_body}"
            )
            transport.send_text(admin_chat_id, tg_text)

        # Route to workers via @mentions (same rule as Telegram messages)
        targets, _ = command_router.parse_at_mentions(comment_body)
        if targets:
            worker_msg = (
                f"manager: PR #{pr_num} review comment on {path}:{line}\n\n"
                f"{comment_body}"
            )
            for t in targets:
                send_to_worker(t, worker_msg)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())

    def handle_transcript_endpoint(self, parsed):
        """Serve polished HTML transcript for a worker.

        Requires a valid rewind token (?token=...) generated by /rewind command.
        GET /transcript/<name>?token=...      — required auth
        GET /transcript/<name>?token=...&sid=...        — specific session ID
        GET /transcript/<name>?token=...&page=2         — pagination
        GET /transcript/<name>?token=...&per_page=100   — entries per page (default 50)
        GET /transcript/<name>?token=...&q=search+term  — full-text search
        """
        try:
            import time as _time
            qs = parse_qs(parsed.query)
            # Token auth — clean up expired tokens first
            now = _time.time()
            expired = [k for k, v in REWIND_TOKENS.items() if v["expires_at"] <= now]
            for k in expired:
                del REWIND_TOKENS[k]
            token = qs.get("token", [None])[0]
            if not token or token not in REWIND_TOKENS:
                body = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session Expired</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;
background:#0b0d0b;color:#e5e5e0}
.card{text-align:center;max-width:400px;padding:40px}
h1{font-size:1.5rem;margin-bottom:12px}
p{color:#878b86;line-height:1.6;margin:8px 0}
code{background:#1a1c1a;padding:3px 8px;border-radius:4px;font-size:.9em}
</style></head><body><div class="card">
<h1>Session Expired</h1>
<p>This link has expired or is invalid.</p>
<p>Send <code>/rewind &lt;name&gt;</code> in Telegram to get a fresh 5-minute link.</p>
</div></body></html>""".encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # Refresh token expiry on each valid interaction (sliding window)
            REWIND_TOKENS[token]["expires_at"] = now + REWIND_TIMEOUT

            parts = parsed.path.rstrip("/").split("/")
            # /transcript/<name>
            if len(parts) < 3 or not parts[2]:
                self._send_json(400, {"error": "Usage: /transcript/<worker_name>"})
                return
            name = parts[2]
            qs = parse_qs(parsed.query)
            session_id = qs.get("sid", [None])[0]
            page_raw = qs.get("page", [None])[0]
            try:
                page = max(1, int(page_raw)) if page_raw is not None else None
            except (ValueError, TypeError):
                page = None
            try:
                per_page = max(1, min(500, int(qs.get("per_page", [50])[0])))
            except (ValueError, TypeError):
                per_page = 50
            search_query = qs.get("q", [""])[0].strip()
            search_sort = qs.get("sort", ["relevance"])[0].strip()
            if search_sort not in ("relevance", "time"):
                search_sort = "relevance"
            filter_mode = qs.get("filter", [""])[0].strip()
            # Remote workers use SSH via transcript-index.py — skip rsync loading page
            host = get_worker_host(name)
            if not host:
                # Local workers: check if transcript needs remote sync
                _tp, _sid, _cwd = _resolve_transcript_path(name, session_id)
                if _tp == "syncing":
                    sync_key = f"{name}:{_sid}"
                    html_content = _render_transcript_loading(name, _sid, token or "", sync_key)
                    body = html_content.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            html_content = _render_transcript_html(
                    name, session_id=session_id,
                    page=page, per_page=per_page, search_query=search_query,
                    token=token or "", filter_mode=filter_mode, search_sort=search_sort)
            self._send_html(html_content.encode("utf-8"))
        except Exception as e:
            print(f"Transcript endpoint error: {e}")
            import traceback
            traceback.print_exc()
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_team_chat_endpoint(self, parsed):
        """Serve team Telegram chat viewer.

        GET /team-chat?token=...
        GET /team-chat?token=...&page=2
        GET /team-chat?token=...&q=search+term
        """
        try:
            import time as _time
            qs = parse_qs(parsed.query)
            # Token auth — same as transcript endpoint
            now = _time.time()
            expired = [k for k, v in REWIND_TOKENS.items() if v["expires_at"] <= now]
            for k in expired:
                del REWIND_TOKENS[k]
            token = qs.get("token", [None])[0]
            if not token or token not in REWIND_TOKENS:
                body = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session Expired</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;
background:#0b0d0b;color:#e5e5e0}
.card{text-align:center;max-width:400px;padding:40px}
h1{font-size:1.5rem;margin-bottom:12px}
p{color:#878b86;line-height:1.6;margin:8px 0}
code{background:#1a1c1a;padding:3px 8px;border-radius:4px;font-size:.9em}
</style></head><body><div class="card">
<h1>Session Expired</h1>
<p>This link has expired or is invalid.</p>
<p>Send <code>/rewind team</code> in Telegram to get a fresh 5-minute link.</p>
</div></body></html>""".encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            REWIND_TOKENS[token]["expires_at"] = now + REWIND_TIMEOUT

            page_raw = qs.get("page", [None])[0]
            try:
                page = max(1, int(page_raw)) if page_raw is not None else None
            except (ValueError, TypeError):
                page = None
            try:
                per_page = max(1, min(500, int(qs.get("per_page", [50])[0])))
            except (ValueError, TypeError):
                per_page = 50
            search_query = qs.get("q", [""])[0].strip()

            html_content = _render_team_chat_html(
                page=page, per_page=per_page,
                search_query=search_query, token=token)
            self._send_html(html_content.encode("utf-8"))
        except Exception as e:
            print(f"Team chat endpoint error: {e}")
            import traceback
            traceback.print_exc()
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_team_chat_media(self, parsed):
        """Serve media files (photos/files) from team chat export.

        GET /team-chat-media/photos/photo_1@28-01-2026_18-09-13.jpg?token=...
        GET /team-chat-media/files/somefile.pdf?token=...

        Requires valid rewind token (same as /team-chat).
        Only serves files under TEAM_CHAT_MEDIA_DIR (no path traversal).
        """
        import time as _time
        qs = parse_qs(parsed.query)

        # Token auth
        now = _time.time()
        token = qs.get("token", [None])[0]
        if not token or token not in REWIND_TOKENS or REWIND_TOKENS[token]["expires_at"] <= now:
            self.send_response(403)
            self.end_headers()
            return

        # Refresh token expiry on access
        REWIND_TOKENS[token]["expires_at"] = now + REWIND_TIMEOUT

        # Extract relative path (after /team-chat-media/)
        from urllib.parse import unquote
        rel_path = unquote(parsed.path[len("/team-chat-media/"):])
        # Security: prevent path traversal
        rel_path = os.path.normpath(rel_path)
        if rel_path.startswith("..") or rel_path.startswith("/"):
            self.send_response(403)
            self.end_headers()
            return

        full_path = os.path.join(TEAM_CHAT_MEDIA_DIR, rel_path)
        # Double-check it's still under the media dir
        if not os.path.abspath(full_path).startswith(os.path.abspath(TEAM_CHAT_MEDIA_DIR)):
            self.send_response(403)
            self.end_headers()
            return

        if not os.path.isfile(full_path):
            self.send_response(404)
            self.end_headers()
            return

        # Determine content type
        ext = os.path.splitext(full_path)[1].lower()
        content_types = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
            ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
            ".pdf": "application/pdf", ".txt": "text/plain",
            ".md": "text/plain", ".json": "application/json",
            ".csv": "text/csv", ".zip": "application/zip",
        }
        ctype = content_types.get(ext, "application/octet-stream")

        try:
            with open(full_path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(500)
            self.end_headers()


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

    if grpc_server is not None:
        try:
            grpc_server.stop()
            print("gRPC server stopped")
        except Exception as e:
            print(f"gRPC server stop failed: {e}")

    if gmail_connector_instance is not None:
        try:
            gmail_connector_instance.stop()
            print("Gmail connector stopped")
        except Exception:
            pass

    if github_connector_instance is not None:
        try:
            github_connector_instance.stop()
            print("GitHub connector stopped")
        except Exception:
            pass

    send_shutdown_message()
    sys.exit(0)


def main():
    global admin_chat_id, grpc_server, gmail_connector_instance, github_connector_instance

    if TRANSPORT_MODE == "telegram" and not BOT_TOKEN:
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

        result = transport.send_text(last_chat_id, "\n".join(lines))
        if result and result.get("ok"):
            print(f"Sent startup notification to chat {last_chat_id}")
        else:
            print(f"Failed to send startup notification: {result}")

    watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog.start()

    _load_learning_reminder_state()
    _seed_learning_reminder_state(registered.keys())
    _schedule_idle_scan()
    print(f"Learning reminder idle scan: started (every 30 min, {len(_learning_reminder_state)} workers tracked)")

    if BridgeGRPCServer is not None:
        try:
            grpc_server = BridgeGRPCServer(
                on_worker_response=handle_grpc_worker_response,
                on_worker_register=handle_grpc_worker_register,
                on_worker_disconnect=handle_grpc_worker_disconnect,
                on_jsonl_received=handle_grpc_jsonl_received,
            )
            grpc_server.start(GRPC_PORT)
            print(f"gRPC server on {BRIDGE_BIND}:{GRPC_PORT}")
        except Exception as e:
            grpc_server = None
            print(f"gRPC server disabled: {e}")
    elif BRIDGE_GRPC_IMPORT_ERROR is not None:
        print(f"gRPC server disabled: {BRIDGE_GRPC_IMPORT_ERROR}")

    _connector_message_log = {}  # tag -> deque of {ts, html, plain, targets}

    def _connector_log_message(tag, html_text, plain_text, targets):
        from collections import deque
        if tag not in _connector_message_log:
            _connector_message_log[tag] = deque(maxlen=20)
        _connector_message_log[tag].append({
            "ts": time.time(),
            "html": html_text,
            "plain": plain_text,
            "targets": targets or [],
        })

    def _connector_render_html(tag, current_html):
        """Render HTML page with current message + recent history."""
        import html as html_mod
        msgs = list(_connector_message_log.get(tag, []))
        icon = "🔔" if tag == "github" else "📧"
        title = f"{icon} {tag.title()} Messages"
        rows = []
        for m in msgs:
            ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(m["ts"]))
            who = ", ".join(m["targets"]) if m["targets"] else "broadcast"
            content = m["html"]
            is_current = (m == msgs[-1]) if msgs else False
            cls = "msg current" if is_current else "msg"
            rows.append(f'<div class="{cls}"><div class="meta">{ts} → {html_mod.escape(who)}</div><div class="body">{content}</div></div>')
        rows_html = "\n".join(rows)
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 700px; margin: 2em auto; padding: 0 1em; background: #1a1a2e; color: #e0e0e0; }}
h1 {{ font-size: 1.3em; color: #8be9fd; }}
.msg {{ border-left: 3px solid #444; padding: 0.5em 1em; margin: 1em 0; background: #16213e; border-radius: 4px; }}
.msg.current {{ border-left-color: #50fa7b; background: #1a2a4a; }}
.meta {{ font-size: 0.8em; color: #888; margin-bottom: 0.3em; }}
.body {{ line-height: 1.5; }}
blockquote {{ border-left: 2px solid #555; padding-left: 0.8em; color: #aaa; margin: 0.5em 0; }}
a {{ color: #8be9fd; }}
</style></head><body>
<h1>{title}</h1>
<p style="color:#888;font-size:0.85em">{len(msgs)} recent message{"s" if len(msgs) != 1 else ""}</p>
{rows_html}
</body></html>"""

    def _connector_short_summary(tag, plain_text, serve_url=None):
        """Create concise Telegram HTML summary (max 4 lines, clickable link)."""
        import html as _html
        icon = "🔔" if tag == "github" else "📧"
        body = plain_text.strip()
        # Strip verbose prefix like "manager (via GitHub issue #1234):"
        import re as _re
        body = _re.sub(r'^manager\s*\(via\s+\w+[^)]*\):\s*', '', body)
        body = _re.sub(r'\[thread:[^\]]+\]\s*', '', body)
        # Collapse to single line, trim
        body = " ".join(body.split())
        if len(body) > 200:
            body = body[:197] + "…"
        # Build: icon + tag ref on line 1, body on line 2, link on line 3
        # Extract reference (issue/PR number, subject, thread)
        ref_match = _re.search(r'#(\d+)', plain_text)
        thread_match = _re.search(r'\[thread:([^\]]+)\]', plain_text)
        if ref_match:
            ref = f"#{ref_match.group(1)}"
        elif thread_match:
            ref = f"thread:{thread_match.group(1)}"
        else:
            ref = ""
        header = f"{icon} <b>{tag.title()}</b>"
        if ref:
            header += f" {_html.escape(ref)}"
        parts = [header, _html.escape(body)]
        if serve_url:
            parts.append(f'<a href="{_html.escape(serve_url)}">View full →</a>')
        return "\n".join(parts)

    def _connector_on_message(tag):
        def handler(targets, html_text, plain_text=None, attachments=None):
            if plain_text is None:
                plain_text = html_text
            _connector_log_message(tag, html_text, plain_text, targets)
            if admin_chat_id:
                serve_url = None
                try:
                    page_html = _connector_render_html(tag, html_text)
                    slug = f"connector-{tag}"
                    tmp_path = f"/tmp/connector-{tag}.html"
                    with open(tmp_path, "w") as f:
                        f.write(page_html)
                    serve_url = _beast_serve_deploy(tmp_path, slug)
                except Exception as e:
                    print(f"[{tag}] beast serve failed: {e}")
                summary = _connector_short_summary(tag, plain_text, serve_url)
                try:
                    send_telegram_message(admin_chat_id, summary, parse_mode="HTML")
                except Exception:
                    try:
                        send_telegram_message(admin_chat_id, plain_text[:300])
                    except Exception as e:
                        print(f"[{tag}] Telegram send failed: {e}")
                for att in (attachments or []):
                    fpath = att.get("path", "")
                    fname = att.get("filename", "")
                    if not fpath or not os.path.isfile(fpath):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    caption = f"📧 {fname}"
                    if ext in ALLOWED_IMAGE_EXTENSIONS:
                        send_photo(admin_chat_id, fpath, caption)
                    elif ext in VIDEO_EXTENSIONS:
                        send_video(admin_chat_id, fpath, caption)
                    else:
                        send_document(admin_chat_id, fpath, caption)
                    print(f"[{tag}] attachment -> Telegram: {fname}")
            if targets:
                for name in targets:
                    send_to_worker(name, plain_text)
                    print(f"[{tag}] -> {name}: {plain_text[:80]}...")
            else:
                print(f"[{tag}] -> Telegram only (no mentions): {plain_text[:80]}...")
        return handler

    def _connector_get_workers():
        return set(get_registered_sessions().keys())

    def _connector_on_alert(tag):
        def handler(text):
            if admin_chat_id:
                try:
                    send_telegram_message(admin_chat_id, text)
                except Exception as e:
                    print(f"[{tag}] Failed to send Telegram alert: {e}")
        return handler

    gmail_connector_instance = None
    if GMAIL_ENABLED and GmailConnector is not None:
        gmail_connector_instance = GmailConnector(
            gws_bin=GMAIL_GWS_BIN,
            from_filter=GMAIL_FROM_FILTER,
            poll_interval=GMAIL_POLL_INTERVAL,
            on_message=_connector_on_message("gmail"),
            get_registered_workers=_connector_get_workers,
            on_alert=_connector_on_alert("gmail"),
        )
        gmail_connector_instance.start()
        print(f"Gmail connector: polling every {GMAIL_POLL_INTERVAL}s for {GMAIL_FROM_FILTER}")
    elif GMAIL_ENABLED and GmailConnector is None:
        print(f"Gmail connector disabled: {GMAIL_IMPORT_ERROR}")

    github_connector_instance = None
    if GITHUB_ENABLED and GitHubConnector is not None:
        github_connector_instance = GitHubConnector(
            repo=GITHUB_REPO,
            from_user=GITHUB_FROM_USER,
            poll_interval=GITHUB_POLL_INTERVAL,
            on_message=_connector_on_message("github"),
            get_registered_workers=_connector_get_workers,
            on_alert=_connector_on_alert("github"),
            state_file=str(NODE_DIR / "github_state.json"),
        )
        github_connector_instance.start()
        print(f"GitHub connector: polling every {GITHUB_POLL_INTERVAL}s for {GITHUB_FROM_USER} on {GITHUB_REPO}")
    elif GITHUB_ENABLED and GitHubConnector is None:
        print(f"GitHub connector disabled: {GITHUB_IMPORT_ERROR}")

    try:
        ReuseAddrServer((BRIDGE_BIND, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
