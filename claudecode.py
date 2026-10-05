"""Claude Code worker management, backends, tmux, and sessions.

Manages Claude Code worker lifecycle: tmux sessions, backends (Claude CLI,
Codex), session state, machine registry, and worker health monitoring.
"""

from __future__ import annotations

import bridge as _bt
from bridge import *  # noqa: F401,F403
# Underscore names excluded from * import — import explicitly
from bridge import (
    _subprocess_runner, _clock,
    _log, _log_best_effort,
    _str_field, _int_field, _dict_field, _bool_field,
    _RealSubprocessRunner, _RealClock,
    _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _build_app_context,
    _wd_cfg, _res_cfg, _urlopen,
    _DEFAULT_PORTS, _bridge_url_env, _mounts_env, _node_name,
    _CHECKIN_NOTE_PATH, _LEARNING_REMINDER_PATH,
)

import collections
from dataclasses import dataclass, field
import fcntl
import hashlib
import http.client
import os
import json
import mimetypes
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import IO, Any, Callable, Iterator, Literal, NamedTuple, Protocol, TypedDict, cast, runtime_checkable
from urllib.parse import urlparse



# ── WorkerRecord: normalized worker data model ──

@dataclass
class WorkerRecord:
    """Normalized worker representation — single typed object for all sources."""
    name: str
    backend: str = "claude"
    host: str | None = None
    tmux_name: str = ""
    callback_url: str = ""
    protocol: str = ""  # "http", "tmux", "pipe", "adapter", ""
    version: str = ""
    tools: dict[str, object] | None = None  # plugin config, shape varies per tool
    chat_id: int | None = None
    cwd: str = ""
    home_host: str = ""
    home_cwd: str = ""

    @property
    def is_remote(self) -> bool:
        """Worker lives on a different machine from the bridge."""
        return bool(self.host)

    @property
    def is_callback(self) -> bool:
        """Worker uses HTTP callback protocol (e.g., forge/packaged workers)."""
        return bool(self.callback_url)

    @property
    def is_interactive(self) -> bool:
        """Worker uses an interactive CLI (tmux-based send)."""
        # Defer to Backend for the canonical answer
        return self.backend == "claude"

    def to_session_dict(self) -> WorkerSessionDict:
        """Convert back to legacy session dict for backward compatibility."""
        result: WorkerSessionDict = {"backend": self.backend}
        if self.tmux_name:
            result["tmux"] = self.tmux_name
        if self.host:
            result["host"] = self.host
        if self.callback_url:
            result["callback_url"] = self.callback_url
            result["protocol"] = "http"
        if self.version:
            result["version"] = self.version
        return result

    @classmethod
    def from_session_dict(cls: type["WorkerRecord"], name: str, session: WorkerSessionDict, tmux_prefix: str = "") -> "WorkerRecord":
        """Create from legacy session dict (as returned by get_registered_sessions)."""
        return cls(
            name=name,
            backend=session.get("backend", "claude"),
            host=session.get("host"),
            tmux_name=session.get("tmux", f"{tmux_prefix}{name}" if tmux_prefix else ""),
            callback_url=session.get("callback_url", ""),
            protocol=session.get("protocol", ""),
            version=session.get("version", ""),
        )



# ============================================================
# CORE: Backend Protocol + implementations

def build_claude_start_cmd(resume_id: str = "") -> str:
    """Build the shell command to start a Claude Code interactive session."""
    cmd = ["claude"]
    if resume_id:
        cmd.extend(["--resume", resume_id])
    cmd.append("--dangerously-skip-permissions")
    return " ".join(shlex.quote(part) for part in cmd)



class Backend(Protocol):
    """Backend interface — start, send, and health-check a CLI worker."""
    name: str
    binary: str
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

class RemoteCache:
    """Caches for remote host operations (tools, machines, home dirs).

    Thread safety: all reads/writes to mutable dicts must be under self.lock.
    The lock is fine-grained (not held during network I/O).
    """

    def __init__(self) -> None:
        """Initialize caches for SSH host resolution and machine config."""
        self.lock: threading.Lock = threading.Lock()
        self.tools: dict[str, str] = {}     # host:tool -> absolute path
        self.machines: dict[str, "Machine"] | None = None
        self.machines_path: Path | None = None
        self.home_dirs: dict[str, str] = {}  # host -> remote $HOME path



remote_cache = RemoteCache()



def _resolve_remote_tool(tool: str, host: str) -> str:
    """Discover absolute path of a tool on a remote host. Cached per host."""
    key = f"{host}:{tool}"
    with remote_cache.lock:
        cached = remote_cache.tools.get(key)
    if cached:
        return cached
    probe = (
        f'command -v {shlex.quote(tool)} 2>/dev/null || '
        f'for p in /opt/homebrew/bin/{tool} /usr/local/bin/{tool} /usr/bin/{tool} /bin/{tool}; '
        f'do [ -x "$p" ] && echo "$p" && break; done'
    )
    try:
        r = _subprocess_runner.run(
            ["ssh", "-o", "ConnectTimeout=3", host, probe],
            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND,
        )
        found = r.stdout.strip()
        if found:
            with remote_cache.lock:
                remote_cache.tools[key] = found
            _log(_LOG_INFO, "bridge", f"discovered {tool} on {host}: {found}")
            return found
    except (subprocess.SubprocessError, OSError) as exc:
        _log(_LOG_DEBUG, "probe:_resolve_remote_tool", f"{type(exc).__name__}: {exc}")
    return tool  # fallback to bare name



def _remote_run(cmd: list[str], host: str | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a command, optionally on a remote host via SSH.

    When host is None, runs locally. When set, builds a single shell command
    string with proper quoting so the remote shell doesn't eat special chars
    like # (which starts a comment in bash).
    Resolves tool paths automatically on remote hosts (e.g. tmux -> /opt/homebrew/bin/tmux).
    SSH ControlMaster keeps overhead to ~10ms per call.
    """
    if host:
        cmd = list(cmd)
        tool = str(cmd[0])
        if tool in ("tmux", "claude"):
            cmd[0] = _resolve_remote_tool(tool, host)
        remote_cmd = " ".join(shlex.quote(str(a)) for a in cmd)
        _tv = kwargs.get("timeout", 10)
        timeout_val = int(_tv) if isinstance(_tv, (int, float, str)) else 10  # type: ignore[call-overload]
        cmd = ["ssh", "-o", f"ConnectTimeout={min(timeout_val, 5)}", host, remote_cmd]
    # Default timeout: prevent unbounded subprocess hangs that block bridge threads.
    # Hot-path callers should pass explicit shorter timeouts (3s probes, 5s sends).
    kwargs.setdefault("timeout", 10)
    return _subprocess_runner.run(cmd, **kwargs)



# Compatibility shim: _extract_msg_text lives in telegram.py; prefer importing from there.
from telegram import _extract_msg_text as _extract_msg_text  # noqa: F401



def get_worker_host(name: str) -> str | None:
    """Get the SSH host for a worker from the persistent registry, or None if local."""
    import bridge
    registry = bridge._load_registry()
    worker = registry.get("workers", {}).get(name, {})
    return worker.get("host")



class MachineConfigError(ValueError):
    """Invalid machines.json configuration."""



@dataclass(frozen=True)
class Machine:
    """Static machine catalog entry.

    This matches SDD-host-awareness.md Phase 0. Optional metadata is read-only
    decoration for operators and does not affect worker routing yet.
    """
    id: str
    ssh_target: str | None
    bridge_base_url: str
    home_root: str
    os_family: str
    display_name: str = ""
    tailscale_ip: str = ""
    role: str = ""
    configured: bool = True

    @property
    def is_local(self) -> bool:
        """Check whether this machine is the local bridge host."""
        return self.ssh_target is None

    def public_dict(self) -> MachinePublicDict:
        """Return a sanitized dictionary safe for API responses."""
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "ssh_target": self.ssh_target,
            "bridge_base_url": self.bridge_base_url,
            "home_root": self.home_root,
            "os_family": self.os_family,
            "tailscale_ip": self.tailscale_ip,
            "role": self.role,
            "configured": self.configured,
        }



# (machines cache moved to remote_cache.machines / remote_cache.machines_path)


def _detect_os_family() -> str:
    """Detect the OS family: 'darwin' on macOS, 'linux' everywhere else."""
    if sys.platform == "darwin":
        return "darwin"
    return "linux"



def _validate_machine_id(machine_id: str) -> str:
    """Validate and return a machine id (alphanumeric + dash/underscore)."""
    if not isinstance(machine_id, str) or not machine_id:
        raise MachineConfigError("machine id must be a non-empty string")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", machine_id):
        raise MachineConfigError(f"invalid machine id: {machine_id!r}")
    return machine_id



def _coerce_optional_str(value: object, field: str, machine_id: str) -> str:
    """Coerce a config value to str, allowing None (→ empty string)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MachineConfigError(f"machine {machine_id!r} field {field!r} must be a string")
    return value



def _implicit_local_machine() -> Machine:
    """Create a Machine for the local host when no machines.json exists."""
    return Machine(
        id=BRIDGE_SSH_TARGET or "vps",
        ssh_target=None,
        bridge_base_url=BRIDGE_URL,
        home_root=str(Path.home()),
        os_family=_detect_os_family(),
        display_name=(BRIDGE_SSH_TARGET or "vps").upper(),
        role="bridge",
        configured=False,
    )



def load_machines_config(path: Path | None = None) -> dict[str, Machine]:
    """Load the static machine catalog.

    Missing file is allowed and yields one implicit local bridge machine for
    backward compatibility. Malformed operator config fails loudly.
    """
    config_path = Path(path) if path is not None else MACHINES_CONFIG_FILE
    if not config_path.exists():
        return {_implicit_local_machine().id: _implicit_local_machine()}

    try:
        data = cast(NodeConfigDict, json.loads(config_path.read_text()))
    except json.JSONDecodeError as e:
        raise MachineConfigError(f"{config_path}: invalid JSON: {e}") from e
    except OSError as e:
        raise MachineConfigError(f"{config_path}: cannot read: {e}") from e

    if not isinstance(data, dict):
        raise MachineConfigError(f"{config_path}: root must be an object")
    if data.get("version") != 1:
        raise MachineConfigError(f"{config_path}: version must be 1")
    raw_machines = data.get("machines")
    if not isinstance(raw_machines, dict) or not raw_machines:
        raise MachineConfigError(f"{config_path}: machines must be a non-empty object")

    machines: dict[str, Machine] = {}
    ssh_targets: dict[str, str] = {}
    local_count = 0
    for raw_id, raw in raw_machines.items():
        machine_id = _validate_machine_id(raw_id)
        if not isinstance(raw, dict):
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} must be an object")

        missing = [k for k in ("ssh_target", "bridge_base_url", "home_root", "os_family") if k not in raw]
        if missing:
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} missing {', '.join(missing)}")

        ssh_target = raw.get("ssh_target")
        if ssh_target is not None and not isinstance(ssh_target, str):
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} ssh_target must be string or null")
        if ssh_target == "":
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} ssh_target cannot be empty")
        if ssh_target is None:
            local_count += 1
        elif ssh_target in ssh_targets:
            raise MachineConfigError(
                f"{config_path}: ssh_target {ssh_target!r} used by both "
                f"{ssh_targets[ssh_target]!r} and {machine_id!r}"
            )
        else:
            ssh_targets[ssh_target] = machine_id

        bridge_base_url = _coerce_optional_str(raw.get("bridge_base_url"), "bridge_base_url", machine_id).rstrip("/")
        home_root = _coerce_optional_str(raw.get("home_root"), "home_root", machine_id).rstrip("/")
        os_family = _coerce_optional_str(raw.get("os_family"), "os_family", machine_id)
        if not bridge_base_url:
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} bridge_base_url cannot be empty")
        if not home_root.startswith("/"):
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} home_root must be absolute")
        if os_family not in ("linux", "darwin"):
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} os_family must be linux or darwin")

        machines[machine_id] = Machine(
            id=machine_id,
            ssh_target=ssh_target,
            bridge_base_url=bridge_base_url,
            home_root=home_root,
            os_family=os_family,
            display_name=_coerce_optional_str(raw.get("display_name", machine_id), "display_name", machine_id),
            tailscale_ip=_coerce_optional_str(raw.get("tailscale_ip", ""), "tailscale_ip", machine_id),
            role=_coerce_optional_str(raw.get("role", ""), "role", machine_id),
        )

    if local_count != 1:
        raise MachineConfigError(f"{config_path}: exactly one local machine with ssh_target=null is required")
    return machines



def get_machine_catalog(force_reload: bool = False) -> dict[str, Machine]:
    """Return the startup machine catalog."""
    with remote_cache.lock:
        if force_reload or remote_cache.machines is None or remote_cache.machines_path != MACHINES_CONFIG_FILE:
            remote_cache.machines = load_machines_config(MACHINES_CONFIG_FILE)
            remote_cache.machines_path = MACHINES_CONFIG_FILE
        return dict(remote_cache.machines)



def _machine_for_worker_host(host: str | None, machines: dict[str, Machine]) -> Machine:
    """Look up the Machine for a worker's ssh_target, creating an ad-hoc entry if needed."""
    if host is None:
        for machine in machines.values():
            if machine.is_local:
                return machine
        return _implicit_local_machine()
    for machine in machines.values():
        if machine.ssh_target == host:
            return machine
    machine_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", host).strip("-") or "unknown"
    return Machine(
        id=machine_id,
        ssh_target=host,
        bridge_base_url=BRIDGE_PUBLIC_URL or "",
        home_root="",
        os_family="",
        display_name=host,
        role="worker-host",
        configured=False,
    )



def _machine_health(machine: Machine) -> MachineHealthDict:
    """Build a health status dict for a machine (disk, memory, IO, up/down)."""
    host_label = machine.ssh_target or "VPS"
    with watchdog.lock:
        health: MachineHealthDict = {
            "status": "up" if machine.is_local else "unknown",
            "down_since": None,
            "last_error": None,
            "disk": host_health.disk_usage.get(host_label),
            "memory": host_health.mem_usage.get(host_label),
            "io": host_health.io_usage.get(host_label),
        }
        if machine.ssh_target:
            if host_health.down.get(machine.ssh_target, False):
                health["status"] = "down"
                health["down_since"] = host_health.down_since.get(machine.ssh_target)
                health["last_error"] = host_health.last_error.get(machine.ssh_target)
            elif (
                machine.ssh_target in host_health.ssh_failures
                or machine.ssh_target in host_health.disk_usage
                or machine.ssh_target in host_health.mem_usage
                or machine.ssh_target in host_health.io_usage
            ):
                health["status"] = "up"
    return cast(MachineHealthDict, health)



def _machine_access(machine: Machine, caller_host: str | None) -> str:
    """Return the access command for reaching a machine ('local' or 'ssh <host>')."""
    target_host = machine.ssh_target
    if caller_host == target_host:
        return "local"
    if target_host is None:
        return f"ssh {BRIDGE_SSH_TARGET}"
    return f"ssh {target_host}"



def get_machines(caller_from: str | None = None) -> MachinesCatalogResponse:
    """Return configured machines plus derived workers, access, and health."""
    machines = get_machine_catalog()
    import bridge
    registered = bridge.get_registered_sessions()
    caller_info = registered.get(caller_from, {}) if caller_from else {}
    caller_host = caller_info.get("host") if caller_info else (get_worker_host(caller_from) if caller_from else None)

    rows: dict[str, MachinePublicDict] = {}
    for machine in machines.values():
        row = machine.public_dict()
        row["access"] = _machine_access(machine, caller_host)
        row["workers"] = []
        row["worker_count"] = 0
        row["health"] = _machine_health(machine)
        rows[machine.id] = row

    for name, info in registered.items():
        host = info.get("host") if "host" in info else get_worker_host(name)
        machine = _machine_for_worker_host(host, machines)
        if machine.id not in rows:
            row = machine.public_dict()
            row["access"] = _machine_access(machine, caller_host)
            row["workers"] = []
            row["worker_count"] = 0
            row["health"] = _machine_health(machine)
            rows[machine.id] = row

        worker_entry = {
            "name": name,
            "backend": info.get("backend", DEFAULT_BACKEND),
            "status": "online" if info.get("tmux") or info.get("callback_url") else "exited",
        }
        rows[machine.id]["workers"].append(worker_entry)
        rows[machine.id]["worker_count"] += 1

    return {
        "version": 1,
        "config_path": str(MACHINES_CONFIG_FILE),
        "caller": caller_from or None,
        "machines": list(rows.values()),
    }



def _project_slug(cwd: str) -> str:
    """Convert absolute path to Claude Code's project directory slug.

    Claude Code stores sessions at ~/.claude/projects/<slug>/<session-id>.jsonl
    where slug is the CWD with / replaced by -.
    """
    return cwd.replace("/", "-")


# ============================================================
# GIT-BASED TELEPORT SYNC
# ============================================================
# VPS hosts bare repos at ~/git-server/<project>.git.
# Workers push WIP state (via git stash create) to per-worker branches,
# target fetches deltas. ~0-50s vs 600s+ for rsync over Tailscale.

GIT_SERVER_DIR = os.path.expanduser("~/git-server")



def _ensure_bare_repo(project_name: str) -> str:
    """Create bare repo at GIT_SERVER_DIR/<project>.git if missing. Returns path."""
    bare_path = os.path.join(GIT_SERVER_DIR, f"{project_name}.git")
    if not os.path.isdir(bare_path):
        os.makedirs(GIT_SERVER_DIR, exist_ok=True)
        _subprocess_runner.run(
            ["git", "init", "--bare", bare_path],
            capture_output=True, text=True, check=True, timeout=TIMEOUT_REMOTE_CMD)
    return bare_path



def _is_git_repo(cwd: str, host: str | None = None) -> bool:
    """Check if cwd is inside a git repository."""
    try:
        r = _remote_run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False



# ─────────────────────────────────────────────────────────────────────────────
# Shared tmux helpers (used by multiple backends)
# ─────────────────────────────────────────────────────────────────────────────

class TmuxSendState:
    """Thread locks and file descriptors for serialized tmux send operations."""

    def __init__(self) -> None:
        """Initialize per-session send locks and flock file descriptors."""
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard: threading.Lock = threading.Lock()
        self.flock_fds: dict[str, int] = {}



tmux_send = TmuxSendState()



def _get_tmux_send_lock(tmux_name: str) -> threading.Lock:
    """Get or create a lock for a specific tmux session."""
    with tmux_send.locks_guard:
        if tmux_name not in tmux_send.locks:
            tmux_send.locks[tmux_name] = threading.Lock()
        return tmux_send.locks[tmux_name]



def tmux_send_lock_path(tmux_name: str) -> Path:
    """Return the flock file path for a tmux session. Node-namespaced."""
    return Path(f"/tmp/claudecode-telegram/{_node_name}/locks/{tmux_name}.lock")



def _acquire_flock(tmux_name: str) -> int:
    """Acquire a cross-process flock for a tmux session. Returns fd."""
    lock_file = tmux_send_lock_path(tmux_name)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd



def _release_flock(fd: int) -> None:
    """Release a cross-process flock."""
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)



def tmux_exists(tmux_name: str, host: str | None = None, timeout: int = 3) -> bool:
    """Check if tmux session exists (locally or on remote host via SSH)."""
    return _remote_run(
        ["tmux", "has-session", "-t", tmux_name],
        host=host, capture_output=True, timeout=timeout
    ).returncode == 0



def tmux_send_message(tmux_name: str, text: str, host: str | None = None, literal: bool = False) -> bool:
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
                    host=host, capture_output=True, timeout=TIMEOUT_TMUX_SEND,
                )
                if r.returncode != 0:
                    return False
                _clock.sleep(DELAY_RETRY)
                r = _remote_run(["tmux", "send-keys", "-t", tmux_name, "Enter"], host=host, timeout=TIMEOUT_TMUX_SEND)
                return r.returncode == 0

            buf_name = f"msg-{uuid.uuid4().hex[:8]}"

            if host:
                # Remote: pipe text via stdin to avoid shared filesystem
                r = _remote_run(
                    ["tmux", "load-buffer", "-b", buf_name, "-"],
                    host=host, input=text.encode(), capture_output=True, timeout=TIMEOUT_TMUX_SEND,
                )
            else:
                # Local: write to temp file for tmux load-buffer
                fd, tmpfile = tempfile.mkstemp(suffix=".msg", prefix="tmux-send-")
                try:
                    try:
                        os.write(fd, text.encode())
                    finally:
                        os.close(fd)
                    r = _subprocess_runner.run(
                        ["tmux", "load-buffer", "-b", buf_name, tmpfile],
                        capture_output=True, timeout=TIMEOUT_TMUX_SEND,
                    )
                finally:
                    try:
                        os.unlink(tmpfile)
                    except OSError as exc:
                        _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")

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
                host=host, capture_output=True, timeout=TIMEOUT_TMUX_SEND,
            )
            if r.returncode != 0:
                return False
            # Delay after paste: TUI needs time to process paste-end marker
            # and re-render. At low context (1%), Claude Code TUI can take
            # 300-1000ms to render pasted text. Enter sent before render
            # completes hits an empty prompt and the message is silently lost.
            # 50ms → 150ms → 1s: increased after observing silent message
            # loss on prod sessions with heavy context load.
            _clock.sleep(DELAY_STARTUP)
            # Send Enter to submit the pasted text
            r = _remote_run(["tmux", "send-keys", "-t", tmux_name, "Enter"], host=host, timeout=TIMEOUT_TMUX_SEND)
            return r.returncode == 0
        finally:
            _release_flock(flock_fd)



def get_pane_command(tmux_name: str, host: str | None = None) -> str:
    """Get the current command running in tmux pane."""
    result = _remote_run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_command}"],
        host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
    )
    return result.stdout.strip() if result.returncode == 0 else ""



def is_process_running(tmux_name: str, process_name: str, host: str | None = None) -> bool:
    """Check if a process is running in tmux session."""
    cmd = get_pane_command(tmux_name, host=host)
    if process_name.lower() in cmd.lower():
        return True

    result = _remote_run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
        host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
    )
    if result.returncode != 0:
        return False

    pane_pid = result.stdout.strip()
    if not pane_pid:
        return False

    result = _remote_run(
        ["pgrep", "-P", pane_pid, process_name],
        host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK
    )
    return result.returncode == 0



def tmux_send_escape(tmux_name: str, host: str | None = None) -> None:
    """Send an escape key sequence to a tmux pane."""
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)



def _tmux_pane_pids(host: str | None = None) -> dict[str, str]:
    """Return a map of tmux session_name -> pane_pid for all panes."""
    try:
        result = _remote_run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
        )
    except (subprocess.SubprocessError, OSError):
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



class ClaudeBackend:
    """Claude Code CLI - interactive mode with hook for responses."""
    name = "claude"
    binary = "claude"
    is_interactive = True

    def start_cmd(self, resume_id: str = "") -> str:
        """Start cmd."""
        return build_claude_start_cmd(resume_id)

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        """Send."""
        host = get_worker_host(worker_name)
        try:
            if not tmux_exists(tmux_name, host=host, timeout=TIMEOUT_TMUX_CHECK):
                return False
        except (subprocess.SubprocessError, OSError):
            if not host:
                return False
            # Remote probe failure — don't block send, attempt delivery anyway
        # Claude's OAuth login dialog doesn't support bracketed paste.
        # Detect login state and use literal send-keys instead.
        literal = False
        try:
            r = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND,
            )
            if r.returncode == 0 and "Paste code here" in r.stdout:
                literal = True
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:send", f"{type(exc).__name__}: {exc}")
        return tmux_send_message(tmux_name, text, host=host, literal=literal)

    def is_online(self, tmux_name: str) -> bool:
        # Note: is_online doesn't have worker_name, so can't look up host.
        # For remote workers, the watchdog uses different detection.
        """Is online."""
        if not tmux_exists(tmux_name):
            return False
        return is_process_running(tmux_name, "claude")



# ─────────────────────────────────────────────────────────────────────────────
# Codex adapter (was hooks/codex-tmux-adapter.py — merged into bridge)
# ─────────────────────────────────────────────────────────────────────────────


def _codex_session_id_path(worker_name: str, sessions_dir: Path) -> Path:
    """Path to the file storing codex session ID."""
    return sessions_dir / worker_name / "codex_session_id"



def _codex_load_session_id(worker_name: str, sessions_dir: Path) -> str:
    """Load saved codex session ID for worker. Returns '' if none."""
    p = _codex_session_id_path(worker_name, sessions_dir)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""



def _codex_save_session_id(worker_name: str, sessions_dir: Path, session_id: str) -> None:
    """Atomically save codex session ID for worker (tmp + rename)."""
    target = _codex_session_id_path(worker_name, sessions_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd: int = -1
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".session_id_")
        os.write(fd, session_id.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(target))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise



def _codex_parse_jsonl(output: str) -> tuple[str, str]:
    """Parse JSONL output from codex exec --json.

    Returns (response_text, thread_id).
    """
    response_parts: list[str] = []
    thread_id: str = ""

    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            event: dict[str, object] = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = _str_field(event, "type")
        if event_type == "thread.started":
            thread_id = _str_field(event, "thread_id")
        elif event_type == "item.completed":
            item = _dict_field(event, "item")
            if _str_field(item, "type") == "agent_message":
                text = _str_field(item, "text")
                if text:
                    response_parts.append(text)

    return "\n".join(response_parts).strip(), thread_id



def _codex_run(message: str, session_id: str = "", workdir: str = "") -> tuple[str, str, int]:
    """Run codex exec and return (response, session_id, returncode)."""
    cmd: list[str] = ["codex", "exec", "--json", "--yolo"]
    if workdir:
        cmd.extend(["-C", workdir])
    if session_id and not session_id.startswith("-"):
        cmd.extend(["resume", session_id, "-"])
    else:
        cmd.append("-")

    try:
        result = subprocess.run(cmd, input=message, capture_output=True, text=True)
        response, new_session_id = _codex_parse_jsonl(result.stdout)
        if result.returncode != 0 and not response:
            stderr = (result.stderr or "").strip()
            response = stderr or "Codex exec failed."
        return response, new_session_id or session_id, result.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error: {e}", session_id, 1



def _codex_send_to_bridge(session_name: str, text: str, bridge_url: str) -> bool:
    """Send codex response to bridge (raw text, no escaping)."""
    try:
        payload: dict[str, str | bool] = {
            "session": session_name, "text": text,
            "source": session_name, "backend": "codex", "escape": True,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{bridge_url}/response", data=data,
            headers={"Content-Type": "application/json"},
        )
        with _urlopen(req, timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError) as e:
        _log(_LOG_WARN, "codex", f"Failed to send to bridge: {e}")
        return False



def _codex_adapter_thread(worker_name: str, text: str,
                          bridge_url: str, sessions_dir: Path) -> None:
    """Thread target: run codex for a worker and forward response to bridge."""
    try:
        session_id = _codex_load_session_id(worker_name, sessions_dir)
        response, new_session_id, _rc = _codex_run(text, session_id)

        if new_session_id:
            _codex_save_session_id(worker_name, sessions_dir, new_session_id)

        if response:
            _codex_send_to_bridge(worker_name, response, bridge_url)
    except Exception as e:
        _log(_LOG_ERROR, "codex", f"Adapter thread for '{worker_name}' failed: {e}")



def _codex_adapter_remote(worker_name: str, text: str,
                          bridge_url: str, sessions_dir: Path, host: str) -> None:
    """Thread target: run codex on a remote host via SSH, parse output locally."""
    try:
        # Load session_id from remote
        import bridge
        remote_sessions = bridge._remap_sessions_dir(host)
        sid_file = f"{remote_sessions}/{worker_name}/codex_session_id"
        session_id = ""
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", host, "cat", sid_file],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                session_id = r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

        # Build codex command
        cmd_parts = ["codex", "exec", "--json", "--yolo"]
        if session_id and not session_id.startswith("-"):
            cmd_parts.extend(["resume", session_id, "-"])
        else:
            cmd_parts.append("-")

        # SSH + run codex with message on stdin
        ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", host] + cmd_parts
        result = subprocess.run(ssh_cmd, input=text, capture_output=True, text=True)
        response, new_session_id = _codex_parse_jsonl(result.stdout)

        if result.returncode != 0 and not response:
            response = (result.stderr or "").strip() or "Codex exec failed."

        # Save session_id on remote
        if new_session_id:
            try:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", host,
                     "mkdir", "-p", f"{remote_sessions}/{worker_name}",
                     "&&", "echo", shlex.quote(new_session_id), ">", sid_file],
                    timeout=10, capture_output=True,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        # POST response to bridge (use public URL for remote)
        if response:
            target_url = BRIDGE_PUBLIC_URL or bridge_url
            _codex_send_to_bridge(worker_name, response, target_url)

    except Exception as e:
        _log(_LOG_ERROR, "codex", f"Remote adapter for '{worker_name}' on {host} failed: {e}")



class CodexBackend:
    """OpenAI Codex CLI - non-interactive mode."""
    name = "codex"
    binary = "codex"
    is_interactive = False
    is_exec = True

    def start_cmd(self, resume_id: str = "") -> str:
        """Start cmd."""
        return "echo 'Codex worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        """Send message to codex worker — runs in a background thread."""
        host = get_worker_host(worker_name)
        if host:
            t = threading.Thread(
                target=_codex_adapter_remote,
                args=(worker_name, text, bridge_url, sessions_dir, host),
                daemon=True,
            )
        else:
            t = threading.Thread(
                target=_codex_adapter_thread,
                args=(worker_name, text, bridge_url, sessions_dir),
                daemon=True,
            )
        t.start()
        return True

    def is_online(self, tmux_name: str) -> bool:
        """Is online."""
        return tmux_exists(tmux_name)



BACKENDS: dict[str, Backend] = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
}



# _spawn_adapter and _spawn_adapter_remote removed — codex adapter is now
# inline in bridge.py (see _codex_adapter_thread / _codex_adapter_remote above CodexBackend).



def _find_codex_transcript(worker_name: str, host: str | None = None) -> str | None:
    """Find the codex native JSONL transcript file for a worker.

    Uses codex_session_id to locate the file under ~/.codex/sessions/.
    Returns the file path or None if not found.
    """
    sid_file = SESSIONS_DIR / worker_name / "codex_session_id"
    if not sid_file.exists():
        return None
    session_id = sid_file.read_text().strip()
    if not session_id:
        return None

    home = os.path.expanduser("~")
    if host:
        home = _get_remote_home(host) or home
    codex_dir = os.path.join(home, ".codex", "sessions")

    if host:
        try:
            r = _remote_run(
                ["find", codex_dir, "-name", f"*{session_id}*", "-name", "*.jsonl"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().split("\n")[0]
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:_find_codex_transcript", f"{type(exc).__name__}: {exc}")
        return None

    import glob
    matches = glob.glob(os.path.join(codex_dir, "**", f"*{session_id}*"), recursive=True)
    jsonl_matches = [m for m in matches if m.endswith(".jsonl")]
    if jsonl_matches:
        return max(jsonl_matches, key=os.path.getmtime)
    return None



def _read_noninteractive_activity(worker_name: str) -> str:
    """Return human-readable activity string for a non-interactive worker."""
    with processes.adapter_pids_lock:
        entry = processes.adapter_pids.get(worker_name)
    if entry:
        proc, _ = entry
        if proc.poll() is None:
            return "adapter running"

    host = get_worker_host(worker_name)
    path = _find_codex_transcript(worker_name, host=host)
    if path:
        try:
            if host:
                r = _remote_run(["stat", "-c", "%Y", path], host=host,
                                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r.returncode == 0:
                    mtime = float(r.stdout.strip())
                    age = int(_clock.time() - mtime)
                else:
                    age = -1
            else:
                mtime = os.path.getmtime(path)
                age = int(_clock.time() - mtime)
            if age >= 0:
                if age < 60:
                    return f"idle (last response {age}s ago)"
                elif age < 3600:
                    return f"idle (last response {age // 60}m ago)"
                else:
                    return f"idle (last response {age // 3600}h ago)"
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    return "idle"



def get_backend(name: str) -> Backend:
    """Look up a backend class by name."""
    return cast(Backend, BACKENDS.get(name, BACKENDS[DEFAULT_BACKEND]))



def is_valid_backend(name: str) -> bool:
    """Check whether a backend name is registered."""
    return name in BACKENDS



def list_backends() -> list[str]:
    """Return list of all registered backend names."""
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



def is_claude_running(tmux_name: str, host: str | None = None) -> bool:
    """Check if an interactive backend process is running in the given tmux pane."""
    return is_process_running(tmux_name, "claude", host=host)



# ── Classes from bridge.py needed for singleton instantiation ──

from bridge import (  # noqa: E402
    ProcessRegistry,
    WorkerWatchdogState,
    LearningReminderState,
)

# Singletons: instantiated here, shared with bridge.py via _BridgeModule propagation.
processes = ProcessRegistry()
watchdog = WorkerWatchdogState()
learning_reminders = LearningReminderState()
# worker_manager is set by bridge.py after WorkerManager class loads (defined after import).
# Stub None so _BridgeModule.__setattr__ propagation can fill it.
worker_manager = None



class HostHealthState:
    """Tracks health metrics for all remote hosts (SSH, disk, CPU, memory, IO, worktrees, Tailscale).

    Thread safety: all reads/writes to mutable dicts must be under watchdog.lock
    (the canonical lock for all watchdog + host_health state).
    """

    def __init__(self) -> None:
        """Initialize per-host health metrics (SSH, disk, memory, IO, CPU, Tailscale)."""
        # SSH connectivity
        self.ssh_failures: dict[str, int] = {}
        self.down: dict[str, bool] = {}
        self.down_since: dict[str, float] = {}
        self.last_error: dict[str, str] = {}
        # Disk
        self.disk_usage: dict[str, DiskUsageDict] = {}
        self.disk_alert_ts: dict[str, float] = {}
        self.disk_alerted: dict[str, str | bool] = {}
        # CPU hogs
        self.cpu_hogs: dict[str, list[CpuHogEntry]] = {}
        self.cpu_hog_alert_ts: dict[str, float] = {}
        # Worktrees
        self.worktree_usage: dict[str, WorktreeUsageDict] = {}
        self.worktree_alert_ts: dict[str, float] = {}
        self.worktree_alerted: dict[str, bool] = {}
        # Memory
        self.mem_usage: dict[str, MemUsageDict] = {}
        self.mem_alert_ts: dict[str, float] = {}
        self.mem_alerted: dict[str, bool] = {}
        # IO
        self.io_usage: dict[str, IoUsageDict] = {}
        self.io_alert_ts: dict[str, float] = {}
        self.io_alerted: dict[str, bool] = {}
        # Infra / Tailscale
        self.tailscale_down: bool = False
        self.tailscale_alert_ts: float = 0.0

    def reset(self) -> None:
        """Reset all state (useful for testing)."""
        self.__init__()  # type: ignore[misc]

    def to_health_summary(self, host: str) -> HealthSummaryDict:
        """Return a typed summary dict for a single host."""
        return HealthSummaryDict(
            ssh_down=self.down.get(host, False),
            ssh_down_since=self.down_since.get(host),
            disk=self.disk_usage.get(host),
            mem=self.mem_usage.get(host),
            io=self.io_usage.get(host),
            cpu_hogs=self.cpu_hogs.get(host, []),
            worktrees=self.worktree_usage.get(host),
        )



host_health = HostHealthState()


# Learning reminders: periodic self-learning nudges per worker
# Two triggers: response count threshold, and idle timeout (checked by timer).
# Anti-annoyance: after any reminder fires, all triggers suppressed until worker responds.
# State is persisted to disk so bridge restarts don't reset progress.
LEARNING_REMINDER_RESPONSE_THRESHOLD = 15  # fire after N worker responses

LEARNING_REMINDER_IDLE_HOURS = 6  # fire if no worker response in N hours



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



# Claude Code stores transcripts at ~/.claude/projects/<slug>/<uuid>.jsonl.
# Overridable in tests.
CLAUDE_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))



# Token stores — still use raw dicts internally for backward compat,
# but the dataclasses above define the canonical shape.
REWIND_TOKENS: dict[str, RewindTokenEntry] = {}

PR_REVIEW_TOKENS: dict[str, PrReviewTokenEntry] = {}



# ─────────────────────────────────────────────────────────────────────────────
# Persistent Worker Registry
# ─────────────────────────────────────────────────────────────────────────────

WORKER_REGISTRY_FILE = NODE_DIR / "workers.json"



# Reserved names that cannot be used as worker names (would clash with commands)
RESERVED_NAMES = {
    # Bridge commands
    "team", "focus", "restart", "settings", "hire", "end",
    # Special
    "all", "cancel", "start", "help",
}



# ============================================================
# INTER-WORKER PIPES
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Worker Pipe Functions (inter-worker communication)
# ─────────────────────────────────────────────────────────────────────────────

def get_worker_pipe_path(name: str) -> Path:
    """Get the named pipe path for a worker.

    Path: /tmp/claudecode-telegram/<node>/<worker>/in.pipe
    """
    return WORKER_PIPE_ROOT / name / "in.pipe"



def ensure_worker_pipe(name: str) -> Path:
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
        _log(_LOG_INFO, "pipe", f"Created worker pipe: {pipe_path}")

    # Start the pipe reader thread to forward messages to worker
    start_pipe_reader(name)

    return pipe_path



def cleanup_worker_pipe(name: str) -> None:
    """Remove the named pipe for a worker."""
    # Stop the pipe reader thread first
    stop_pipe_reader(name)

    pipe_path = get_worker_pipe_path(name)

    if pipe_path.exists():
        try:
            pipe_path.unlink()
            _log(_LOG_INFO, "pipe", f"Removed worker pipe: {pipe_path}")
        except OSError as e:
            _log(_LOG_WARN, "worker", f"Failed to remove worker pipe {pipe_path}: {e}")

    # Also try to remove parent directory if empty
    pipe_dir = pipe_path.parent
    if pipe_dir.exists():
        try:
            pipe_dir.rmdir()
        except OSError:
            pass  # intentional no-op: directory not empty is expected (other workers' pipes)



# ─────────────────────────────────────────────────────────────────────────────
# Pipe Reader Threads (for inter-worker communication)
# ─────────────────────────────────────────────────────────────────────────────

# Dict to track pipe reader threads: name -> (thread, stop_event)
# (pipe reader threads moved to processes.pipe_readers)


def pipe_reader_loop(name: str, stop_event: threading.Event) -> None:
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
    _log(_LOG_INFO, "pipe", f"Pipe reader started for worker '{name}' at {pipe_path}")

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
                        _log(_LOG_INFO, "pipe", f"Pipe message for '{name}': {message[:100]}{'...' if len(message) > 100 else ''}")
                        # Forward to worker using backend routing
                        try:
                            _forward_pipe_message(name, message)
                        except (OSError, ValueError) as e:
                            _log(_LOG_ERROR, "bridge", f"Error forwarding pipe message to '{name}': {e}")

        except FileNotFoundError:
            # Pipe was removed, stop the reader
            _log(_LOG_WARN, "pipe", f"Pipe for '{name}' no longer exists, stopping reader")
            break
        except OSError as e:
            if stop_event.is_set():
                break
            _log(_LOG_ERROR, "bridge", f"Pipe reader error for '{name}': {e}")
            # Wait a bit before retrying
            stop_event.wait(0.5)

    # Clean up registry so start_pipe_reader can restart if needed
    with processes.pipe_readers_lock:
        if name in processes.pipe_readers:
            processes.pipe_readers.pop(name, None)
    _log(_LOG_INFO, "pipe", f"Pipe reader stopped for worker '{name}'")



def _forward_pipe_message(name: str, message: str) -> None:
    """Forward a message from the pipe to the worker's session.

    Uses backend routing for tmux or non-interactive workers.
    """
    import bridge
    if not bridge.worker_manager.send(name, message):
        _log(_LOG_WARN, "worker", f"Warning: Cannot forward pipe message to '{name}' - worker not found")



def start_pipe_reader(name: str) -> None:
    """Start a background thread to read from the worker's input pipe."""
    with processes.pipe_readers_lock:
        if name in processes.pipe_readers:
            thread, _stop = processes.pipe_readers[name]
            if thread.is_alive():
                # Already running
                return
            # Thread crashed or exited — clean up stale entry and restart
            _log(_LOG_WARN, "pipe", f"Pipe reader thread for '{name}' is dead, restarting")
            processes.pipe_readers.pop(name, None)

    pipe_path = get_worker_pipe_path(name)
    if not pipe_path.exists():
        _log(_LOG_WARN, "bridge", f"Cannot start pipe reader: pipe does not exist for '{name}'")
        return

    stop_event = threading.Event()
    thread = threading.Thread(
        target=pipe_reader_loop,
        args=(name, stop_event),
        daemon=True,
        name=f"pipe-reader-{name}"
    )
    with processes.pipe_readers_lock:
        processes.pipe_readers[name] = (thread, stop_event)
    thread.start()
    _log(_LOG_INFO, "pipe", f"Started pipe reader thread for '{name}'")



def stop_pipe_reader(name: str) -> None:
    """Stop the pipe reader thread for a worker."""
    with processes.pipe_readers_lock:
        if name not in processes.pipe_readers:
            return
        thread, stop_event = processes.pipe_readers.pop(name)
    stop_event.set()

    # Write a dummy byte to unblock the reader if it's waiting
    pipe_path = get_worker_pipe_path(name)
    if pipe_path.exists():
        try:
            # Open in non-blocking write mode to unblock reader
            fd = os.open(str(pipe_path), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, b"\n")
            finally:
                os.close(fd)
        except OSError:
            pass  # intentional no-op: pipe may already be closed by reader

    # Wait for thread to finish (with timeout)
    thread.join(timeout=TIMEOUT_THREAD_JOIN)
    if thread.is_alive():
        _log(_LOG_WARN, "bridge", f"Warning: pipe reader thread for '{name}' did not stop gracefully")



# ============================================================
# TMUX SESSION MANAGEMENT
# ============================================================

# ─────────────────────────────────────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────────────────────────────────────

def get_session_dir(name: str) -> Path:
    """Get per-session directory path."""
    return SESSIONS_DIR / name



def ensure_session_dir(name: str) -> Path:
    """Create session directory if needed with secure permissions (0o700)."""
    session_dir = get_session_dir(name)
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Ensure parent directories also have secure permissions
    SESSIONS_DIR.chmod(0o700)
    session_dir.chmod(0o700)
    return session_dir



def get_chat_id_file(name: str) -> Path:
    """Return the path to a worker's chat ID file."""
    return get_session_dir(name) / "chat_id"



def _scan_latest_session_id(cwd: str, host: str | None = None) -> str:
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
                            text=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode != 0:
                return ""
            path = (r.stdout or "").strip()
            if not path:
                return ""
            return os.path.basename(path).removesuffix(".jsonl")
        except (subprocess.SubprocessError, OSError):
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
        session_dir = ensure_session_dir(name)
        history_file = session_dir / "session_history.jsonl"
        entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time())),
            "session_id": session_id,
            "cwd": cwd or "",
            "event": event,
        })
        with open(history_file, "a") as fh:
            fh.write(entry + "\n")
        history_file.chmod(0o600)
    except OSError as exc:
        _log(_LOG_DEBUG, "io:_log_session_event", f"{type(exc).__name__}: {exc}")



def get_session_history(name: str, event: str | None = None) -> list[dict[str, object]]:
    """Read the session audit log for a worker. Optional event filter."""
    f = get_session_dir(name) / "session_history.jsonl"
    if not f.exists():
        return []
    entries = []
    for line in f.read_text().strip().splitlines():
        if not line:
            continue
        try:
            e = cast(dict[str, object], json.loads(line))  # shape varies per event type
            if event and e.get("event") != event:
                continue
            entries.append(e)
        except json.JSONDecodeError:
            continue
    return entries



# Default path for Claude Code's workspace trust config.
_CLAUDE_JSON_PATH = Path.home() / ".claude.json"



def _ensure_workspace_trusted(
    cwd: str,
    config_path: Path | None = None,
) -> None:
    """Add *cwd* to Claude Code's trusted-workspace list (best effort).

    Claude Code stores workspace trust in ``~/.claude.json`` under
    ``projects.<path>.hasTrustDialogAccepted``.  When a worker restarts
    in a directory that hasn't been trusted yet, Claude shows an
    interactive "trust this folder?" prompt that blocks non-interactive
    sessions.  This function pre-trusts the directory so the prompt
    never appears.

    Uses file locking to prevent concurrent writes from corrupting the
    JSON (e.g., two workers being hired/teleported simultaneously).

    Skips the write if the directory is already trusted.
    """
    if not cwd:
        return
    target = config_path or _CLAUDE_JSON_PATH
    try:
        import fcntl
        lock_path = target.with_suffix(".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if target.exists():
                    data = cast(dict[str, object], json.loads(target.read_text()))  # body: {projects}
                else:
                    data = {}
                _projects_raw = data.setdefault("projects", {})
                projects = _projects_raw if isinstance(_projects_raw, dict) else {}
                _entry_raw = projects.get(cwd, {})
                entry = _entry_raw if isinstance(_entry_raw, dict) else {}
                if entry.get("hasTrustDialogAccepted") is True:
                    return  # already trusted — skip rewrite
                projects[cwd] = {**entry, "hasTrustDialogAccepted": True}
                _tmp = target.with_suffix('.tmp')
                _tmp.write_text(json.dumps(data, indent=2))
                os.replace(str(_tmp), str(target))
                _log(_LOG_INFO, "trust", f"pre-trusted workspace: {cwd}")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError) as exc:
        _log(_LOG_WARN, "trust", f"could not pre-trust {cwd}: {exc}")



# Compatibility shim: _build_cwd_change_notice lives in telegram.py; prefer importing from there.
from telegram import _build_cwd_change_notice as _build_cwd_change_notice  # noqa: F401



def _cache_session_id(name: str, sid: str) -> None:
    """Write session_id + its CWD to local cache file (best effort, 0o600).

    The file stores two lines:
        line 1: session UUID
        line 2: CWD path the session was created in

    get_claude_session_id() validates that line 2 matches the worker's
    current CWD. If the CWD has changed, the session_id is stale and
    ignored — no separate "clear on CWD change" step needed.

    Race guard: if the incoming session_id matches what's already cached
    but the cached CWD doesn't match the current CWD, this is a stale
    write from a pre-CWD-change hook — reject it. This prevents the Stop
    hook from re-polluting a session_id that was invalidated by a CWD
    change.
    """
    if not sid:
        return
    try:
        session_dir = ensure_session_dir(name)
        id_file = session_dir / "claude_session_id"
        cwd = get_claude_session_cwd(name) or ""
        old_content = id_file.read_text().strip() if id_file.exists() else ""
        old_lines = old_content.split("\n", 1) if old_content else []
        old_sid = old_lines[0].strip() if old_lines else ""
        old_cwd = old_lines[1].strip() if len(old_lines) > 1 else ""

        # Race guard: same session_id being rewritten after CWD changed.
        # The hook is still sending the old session_id from the previous
        # directory — don't let it rebind to the new CWD.
        if (sid == old_sid and old_cwd and cwd and
                old_cwd.rstrip("/") != cwd.rstrip("/")):
            _log(_LOG_WARN, "session",
                 f"{name}: rejecting stale session_id write "
                 f"(sid={sid[:12]}, old_cwd={old_cwd}, current_cwd={cwd})")
            return

        if old_sid != sid:
            _log_session_event(name, sid, cwd, "cache")
        _tmp = id_file.with_suffix('.tmp')
        _tmp.write_text(f"{sid}\n{cwd}")
        _tmp.chmod(0o600)
        os.replace(str(_tmp), str(id_file))
    except OSError as exc:
        _log(_LOG_DEBUG, "io:_cache_session_id", f"{type(exc).__name__}: {exc}")



def get_claude_session_id(name: str, authoritative: bool = False) -> str:
    """Return the Claude Code session UUID for a worker.

    The local `claude_session_id` file stores two lines:
        line 1: session UUID
        line 2: CWD path the session was created in

    Self-validating: if the stored CWD doesn't match the worker's current
    CWD, the session_id is stale (from a previous directory) and ignored.
    This prevents --resume with a wrong session after CWD changes, even if
    the Stop hook wrote back the old session_id in a race window.

    Old files with only a UUID (no line 2) are treated as valid for
    backwards compatibility.

    The per-worker cache is ALWAYS preferred over _scan_latest_session_id()
    because the scan picks the newest JSONL by mtime in the project dir,
    which is ambiguous when multiple workers share the same CWD.
    """
    cache_file = get_session_dir(name) / "claude_session_id"
    current_cwd = get_claude_session_cwd(name)

    def _read_cache() -> str:
        """Read cached session ID, validating CWD if present."""
        if not cache_file.exists():
            return ""
        content = cache_file.read_text().strip()
        if not content:
            return ""
        lines = content.split("\n", 1)
        sid = lines[0].strip()
        if not sid:
            return ""
        # Validate CWD binding (line 2) against current CWD
        if len(lines) > 1:
            cached_cwd = lines[1].strip()
            if (cached_cwd and current_cwd and
                    cached_cwd.rstrip("/") != current_cwd.rstrip("/")):
                _log(_LOG_INFO, "session",
                     f"{name}: stale session_id (cached_cwd={cached_cwd}, "
                     f"current_cwd={current_cwd})")
                return ""
        return sid

    # Both modes: prefer per-worker cache (worker-specific, set by Stop hook).
    # Only fall back to CWD-based scan when cache is empty or stale.
    val = _read_cache()
    if val:
        return val
    # Cache empty or stale — scan as fallback to self-heal
    if current_cwd:
        host = get_worker_host(name)
        scanned = _scan_latest_session_id(current_cwd, host=host)
        if scanned:
            _cache_session_id(name, scanned)
            return scanned
    return ""



def get_claude_session_cwd(name: str) -> str | None:
    """Read and return the current working directory for a worker session."""
    import bridge
    cwd = bridge._read_session_file(name, "claude_session_cwd")
    if cwd:
        cwd = os.path.expanduser(cwd)
    return cwd



def save_claude_session_cwd(name: str, cwd: str) -> None:
    """Persist a worker's current working directory to disk."""
    if cwd:
        cwd = os.path.expanduser(cwd)
    session_dir = ensure_session_dir(name)
    cwd_file = session_dir / "claude_session_cwd"
    _tmp_cwd = cwd_file.with_suffix('.tmp')
    _tmp_cwd.write_text(cwd)
    _tmp_cwd.chmod(0o600)
    os.replace(str(_tmp_cwd), str(cwd_file))



def clear_claude_session_id(name: str) -> None:
    """Remove the cached session ID for a worker."""
    id_file = get_session_dir(name) / "claude_session_id"
    if id_file.exists():
        id_file.unlink()



def get_any_session_id(name: str) -> tuple[str, str]:
    """Get any *_session_id value for a worker (backend-agnostic).

    Returns (session_id, source) tuple where source is the prefix (e.g. 'claude', 'codex').
    Returns ('', '') when no session ID is found.
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



# (pending locks moved to processes.pending_locks)
# (pending locks guard moved to processes.pending_locks_guard)



# (remote home cache moved to remote_cache.home_dirs)

def _get_remote_home(host: str | None) -> str:
    """Get remote $HOME with caching (avoids SSH per message)."""
    with remote_cache.lock:
        cached = remote_cache.home_dirs.get(host or "")
    if cached is not None:
        return cached
    try:
        r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                        capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        home = r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        home = ""
    with remote_cache.lock:
        remote_cache.home_dirs[host or ""] = home
    return home



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



def _capture_pane_text(tmux_name: str, lines: int = 50, host: str | None = None) -> str:
    """Return the last N lines of a tmux pane, or empty string on error."""
    if lines <= 0:
        return ""
    try:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", f"-{lines}"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout



HOOK_FAILURE_THRESHOLD = 3   # failures in window → POISONED

HOOK_FAILURE_WINDOW = 120    # seconds



def _check_hook_failure_signal(name: str) -> str | None:
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
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0:
                return None
            raw = r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return None
    else:
        signal_file = Path(signal_path)
        if not signal_file.exists():
            return None
        try:
            raw = signal_file.read_text().strip()
        except OSError:
            return None

    if not raw:
        return None
    lines = raw.splitlines()

    cutoff = int(_clock.time()) - HOOK_FAILURE_WINDOW
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
                        capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:_clear_hook_failures", f"{type(exc).__name__}: {exc}")
    else:
        try:
            Path(signal_path).unlink(missing_ok=True)
        except OSError as exc:
            _log(_LOG_DEBUG, "io:_clear_hook_failures", f"{type(exc).__name__}: {exc}")



def _detect_poisoned(name: str, tmux_name: str) -> str | None:
    """Check if a worker session is poisoned (crashed/stuck). Returns reason or None."""
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
        import bridge
        text_parts.append(bridge._check_adapter_log(name))
    combined = "\n".join([part for part in text_parts if part])
    if not combined:
        return None
    for pattern in POISON_PATTERNS:
        if len(pattern.findall(combined)) >= 3:
            return pattern.pattern
    return None



# ─────────────────────────────────────────────────────────────────────────────
# Worker Backend Helpers
# ─────────────────────────────────────────────────────────────────────────────

def normalize_backend(backend: str | None) -> str:
    """Return a normalized backend name with a safe default."""
    return backend or DEFAULT_BACKEND



def normalize_cwd(cwd: str | None) -> str:
    """Expand ~ and return absolute path; empty string for unset/blank."""
    if cwd is None:
        return ""
    raw = cwd.strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(raw))



def validate_cwd(cwd: str | None, host: str | None = None) -> tuple[str, str]:
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
                            capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode != 0:
                return "", f"cwd does not exist on {host}: {normalized}"
        except (subprocess.SubprocessError, OSError) as e:
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
            pass  # intentional no-op: skip unknown flag
        else:
            name_parts.append(part)
        i += 1

    if len(name_parts) != 1:
        return "", backend

    name = name_parts[0]

    # Check for backend prefix syntax (e.g., codex-alice)
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



# Type alias for activity-check functions used by _extract_activity cascade.
_ActivityCheck = Callable[[list[str]], str | None]



def _activity_from_spinner(stripped: list[str]) -> str | None:
    """Detect active thinking spinner (priority 1).

    Claude Code cycles through various Unicode chars as spinner frames.
    ✻ is ALSO a spinner frame — distinguish by "…" presence.
    "✻ Verbing… (49m)" = active; "✻ Thought for 5s" = completed (no "…").
    """
    _ACTIVE_SPINNER_CHARS = {"·", "*", "✢", "✦", "✧", "✹", "✵", "∙", "•", "✻"}
    for raw in reversed(stripped):
        first = raw[0] if raw else ""
        if first == "✻" and "…" not in raw and "..." not in raw:
            continue  # Past tense completed thinking (no ellipsis = done)
        if first not in _ACTIVE_SPINNER_CHARS:
            continue
        # "· Compacting conversation… (5m 26s · thought for 5s)" → verb + duration
        match = re.match(r'^.\s+(.+?)(?:…|\.{3})\s*\(([^()]+)\)\s*$', raw)
        if match:
            verb = match.group(1).strip()
            dur = match.group(2).split('·')[0].strip()
            return f"{verb} ({dur})"
        # Fallback: "· Verb…" or "· Verb" without duration
        verb_match = re.match(r'^.\s+(.+?)(?:…|\.{3})?\s*$', raw)
        if verb_match:
            verb = verb_match.group(1).strip()
            dur_match = re.search(r'(\d+m?\s*\d*\.?\d*s)', raw)
            return f"{verb} ({dur_match.group(1).strip()})" if dur_match else verb
    return None



def _activity_from_tool(stripped: list[str]) -> str | None:
    """Detect actively running tool (priority 2).

    Matches "● ToolName(...)" followed by "⎿ Running…" within 5 lines.
    Also handles MCP tools: "● mcp__server__tool(".
    """
    last_running_tool = None
    for i, raw in enumerate(stripped):
        tool_match = re.match(r'^●\s*([A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*)\(', raw)
        if not tool_match:
            continue
        tool = tool_match.group(1)
        for j in range(i + 1, min(i + 6, len(stripped))):
            line = stripped[j]
            if not line:
                continue
            if line.startswith("⎿"):
                if "Running" in line and "background" not in line:
                    last_running_tool = tool
                break
    if last_running_tool:
        # Shorten MCP tool names: mcp__figma__get_file → figma.get_file
        if last_running_tool.startswith("mcp__"):
            parts = last_running_tool.split("__")
            last_running_tool = ".".join(parts[1:]) if len(parts) > 1 else last_running_tool
        return f"Running {last_running_tool}"
    return None



def _activity_from_rate_limit(stripped: list[str]) -> str | None:
    """Detect rate limiting or connection errors (priority 3)."""
    for raw in reversed(stripped):
        lower = raw.lower()
        if "rate limit" in lower:
            return "Rate limited — waiting to retry"
        if "connection error" in lower and "retrying" in lower:
            return "Connection error — retrying"
        if lower.startswith("retrying") or "retrying in" in lower:
            return "Retrying API request"
    return None



def _activity_from_interactive(stripped: list[str]) -> str | None:
    """Detect interactive prompts — TUI selection/question UI (priority 3b/3c).

    Checks footer lines (shared with _extract_question_details) and content
    patterns. Must run BEFORE the ❯ prompt check because ❯ in these states
    is a SELECTION CURSOR, not the text input prompt.
    """
    # 3b. Footer-based detection
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                for question_line in stripped:
                    if "☐" in question_line:
                        question = question_line.replace("☐", "").strip()
                        if question:
                            return f"Waiting for input: {question}"
                return "Waiting for user input"

    # 3c. Content-based detection (plan approval, tool permission)
    for raw in stripped:
        for pattern in _INTERACTIVE_CONTENT:
            if pattern in raw:
                if "plan" in raw.lower() and ("proceed" in raw.lower() or "execute" in raw.lower()):
                    return "Waiting for plan approval"
                if "plan mode" in raw.lower():
                    return "Waiting for plan mode decision"
                if raw.startswith("Allow "):
                    return "Waiting for tool permission"
                return "Waiting for user input"
    return None



def _activity_from_prompt(stripped: list[str]) -> str | None:
    """Detect prompt/mode bars — ❯ idle, ⏸ plan mode (priority 4).

    Bottom-bar elements are informational, not blocking.
    "bypass permissions on" means permissions ARE being bypassed.
    """
    last_prompt_idx = None
    last_plan_bar_idx = None
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
    return None



def _activity_from_editor(stripped: list[str]) -> str | None:
    """Detect external editor mode (priority 5)."""
    for raw in reversed(stripped):
        if "Save and close editor to continue" in raw:
            return "Waiting for external editor"
    return None



def _activity_from_hooks(stripped: list[str]) -> str | None:
    """Detect system hook execution (priority 6)."""
    for raw in reversed(stripped):
        if "Running SessionStart" in raw:
            return "Running SessionStart hooks"
        if "Running PreCompact" in raw:
            return "Running PreCompact hooks"
    return None



def _activity_from_confirmation(stripped: list[str]) -> str | None:
    """Detect confirmation prompts — plan approval, team lead (priority 7)."""
    for raw in reversed(stripped):
        if "Do you want to proceed?" in raw or "Would you like to proceed?" in raw:
            return "Waiting for plan approval"
        if "Exit plan mode?" in raw or "Entering plan mode" in raw:
            return "In plan mode"
        if "Waiting for team lead" in raw:
            return "Waiting for team lead approval"
    return None



def _activity_from_tasks(stripped: list[str]) -> str | None:
    """Detect task progress checklist (priority 8)."""
    done = 0
    total = 0
    for raw in stripped:
        line = raw.lstrip()
        if line.startswith("✔") or line.startswith("✅"):
            done += 1
            total += 1
        elif line.startswith("◻"):
            total += 1
    if total >= 2:
        return f"Tasks ({done}/{total} done)"
    return None



def _is_output_block_end(text: str) -> bool:
    """Detect if a tmux line signals the end of a Claude output block."""
    trimmed = text.lstrip()
    if trimmed.startswith("Context left until auto-compact:"):
        return True
    return trimmed.startswith(("●", "·", "*", "✻", "─", "❯", "⏵", "⏸"))



def _activity_from_output_block(stripped: list[str]) -> str | None:
    """Extract last non-tool ● output block summary (priority 9)."""
    for i in range(len(stripped) - 1, -1, -1):
        raw = stripped[i]
        if not raw.startswith("●"):
            continue
        # Skip tool calls (● CapitalWord( or ● mcp__server__tool()
        if re.match(r'^●\s*[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*\(', raw):
            continue
        parts: list[str] = []
        head = re.sub(r'^●\s*', '', raw).strip()
        if head and not head.startswith("⎿") and not head.startswith("(ctrl+"):
            parts.append(head)
        j = i + 1
        while j < len(stripped):
            nxt = stripped[j]
            if _is_output_block_end(nxt):
                break
            text = nxt.strip()
            if text and not text.startswith("⎿") and not text.startswith("(ctrl+"):
                parts.append(text)
            j += 1
        if parts:
            msg = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
            if len(msg) > 120:
                msg = msg[:117].rstrip() + "..."
            return msg
    return None



def _activity_from_error(stripped: list[str]) -> str | None:
    """Detect standalone error lines (priority 10)."""
    for raw in reversed(stripped):
        if re.match(r'^(FAIL|ERROR|Error|Traceback|Fail)\b', raw, re.IGNORECASE):
            lower = raw.lower()
            if lower.startswith("error"):
                tail = raw[len("Error"):].lstrip(": ").strip()
                return f"Error: {tail}" if tail else "Error"
            return f"Error: {raw[:60]}"
    return None



def _extract_activity(lines: list[str]) -> str:
    """Extract a 1-line activity summary from tmux pane output.

    Based on Claude Code v2.1.59 (repo d6ab0ea, 2026-02-26).
    Scans for Claude Code UI signals in priority order.
    Each check is a focused helper returning str | None.
    """
    if not lines:
        return "Active"

    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped:
        return "Idle"

    # Priority cascade — first match wins
    _CHECKS: list[_ActivityCheck] = [
        _activity_from_spinner,       # 1. Active thinking spinner
        _activity_from_tool,          # 2. Tool actively running
        _activity_from_rate_limit,    # 3. Rate limiting / connection errors
        _activity_from_interactive,   # 3b/3c. Interactive prompts
        _activity_from_prompt,        # 4. Prompt + mode bars
        _activity_from_editor,        # 5. Editor mode
        _activity_from_hooks,         # 6. Hook execution
        _activity_from_confirmation,  # 7. Confirmation prompts
        _activity_from_tasks,         # 8. Task progress
        _activity_from_output_block,  # 9. Last ● output block
        _activity_from_error,         # 10. Standalone error
    ]
    for check in _CHECKS:
        result = check(stripped)
        if result is not None:
            return result

    return "Active"



def _extract_context_pct(lines: list[str]) -> str | None:
    """Extract context % from tmux output if present."""
    for line in reversed(lines):
        m = re.search(r'Context left.*?(\d+)%', line)
        if m:
            return f"{m.group(1)}%"
    return None



def _read_tmux_activity(tmux_name: str, host: str | None = None) -> TmuxActivityResult:
    """Read tmux pane and extract activity summary + context% + raw lines.

    Returns TmuxActivityResult(activity, context_pct, raw_lines).
    When host is set, reads from a remote tmux session via SSH.
    """
    try:
        if host:
            proc = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
            )
        else:
            proc = _subprocess_runner.run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
            )
        if proc.returncode != 0:
            return TmuxActivityResult("Unknown", None, None)
        lines = proc.stdout.split("\n")
        tail = lines[-40:]
        return TmuxActivityResult(_extract_activity(tail), _extract_context_pct(tail), tail)
    except (subprocess.SubprocessError, OSError):
        return TmuxActivityResult("Unknown", None, None)



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



def _extract_question_details(lines: list[str]) -> QuestionDetails | None:
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

    return cast(QuestionDetails, {
        "header": header,
        "options": options,
        "selected_num": selected_num,
    })



def _send_interactive_reply(tmux_name: str, reply: str, details: QuestionDetails, host: str | None = None) -> bool:
    """Handle manager's reply to an interactive prompt via keystroke navigation.

    reply: "1"-"9" for option selection, "skip"/"cancel" for Escape.
    details: from _extract_question_details().
    Returns True if handled, False if not applicable.
    """
    reply = reply.strip().lower()

    if reply in ("skip", "cancel", "esc"):
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)
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
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key], host=host, timeout=TIMEOUT_TMUX_SEND)
            _clock.sleep(DELAY_BRIEF)
        return True

    return False



def get_worker_backend(name: str, session: RegistryWorkerDict | TmuxSessionDict | None = None) -> str:
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
        return normalize_backend(str(session.get("backend")))
    return DEFAULT_BACKEND



# ─────────────────────────────────────────────────────────────────────────────
# CORE: WorkerManager
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# grug say: one place for backend branching. no scatter.
# Worker Helpers (centralize backend switching)
# ─────────────────────────────────────────────────────────────────────────────



def get_tmux_env_value(tmux_name: str, key: str) -> str:
    """Get a tmux session environment variable value."""
    result = _subprocess_runner.run(
        ["tmux", "show-environment", "-t", tmux_name, key],
        capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
    )
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if "=" not in value:
        return ""
    return value.split("=", 1)[1]



def tmux_prompt_empty(tmux_name: str, timeout: float=0.5, host: str | None = None) -> bool:
    """Check if Claude Code's input prompt is empty (message was accepted).

    After sending a message, polls the tmux pane to verify the prompt
    line (❯) is empty, indicating Claude accepted the input.

    Returns True if prompt is empty within timeout, False otherwise.
    """
    import re
    start = _clock.time()
    while _clock.time() - start < timeout:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
        )
        if result.returncode == 0:
            # Check for empty prompt: line starting with ❯ followed by only whitespace
            if re.search(r'^❯\s*$', result.stdout, re.MULTILINE):
                return True
        _clock.sleep(DELAY_BRIEF * 2)
    return False



def export_hook_env(tmux_name: str, backend: str = DEFAULT_WORKER_BACKEND, host: str | None = None) -> None:
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
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK)
        existing = r.stdout.strip().split("=", 1)[-1] if r.returncode == 0 else ""
        if existing and existing != our_url:
            import urllib.request
            _urlopen(existing, timeout=TIMEOUT_THREAD_JOIN).read()
            _log(_LOG_DEBUG, "worker", f"  SKIP export_hook_env({tmux_name}): owned by live bridge at {existing}")
            return
    except (urllib.error.URLError, OSError, TimeoutError):
        pass  # intentional no-op: other bridge dead or unreachable — safe to claim port

    _remote_run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)], host=host, timeout=TIMEOUT_TMUX_CHECK)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX], host=host, timeout=TIMEOUT_TMUX_CHECK)
    # Remap SESSIONS_DIR for remote hosts (different $HOME path)
    sessions_dir_val = str(SESSIONS_DIR)
    if host:
        try:
            r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            remote_home = r.stdout.strip() if r.returncode == 0 else ""
            local_home = str(Path.home())
            if remote_home and remote_home != local_home and sessions_dir_val.startswith(local_home):
                sessions_dir_val = remote_home + sessions_dir_val[len(local_home):]
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", sessions_dir_val], host=host, timeout=TIMEOUT_TMUX_CHECK)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "WORKER_BACKEND", normalize_backend(backend)], host=host, timeout=TIMEOUT_TMUX_CHECK)
    # Always export BRIDGE_URL so workers know where their bridge is
    # Remote workers need BRIDGE_PUBLIC_URL (reachable IP), not localhost
    bridge_url_val = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", bridge_url_val], host=host, timeout=TIMEOUT_TMUX_CHECK)


