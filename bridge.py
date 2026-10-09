#!/usr/bin/env python3
import collections
from dataclasses import dataclass, field
import os
import json
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import types
import time
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, ParseResult
from pathlib import Path
from collections.abc import Mapping
from typing import IO, Any, Callable, NamedTuple, TypedDict, cast
from core import *  # noqa: F401,F403
from core import (
    _str_field, _bool_field,
    _log,
    _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _subprocess_runner, _clock, _urlopen,
    _RealClock,
    _build_app_context,
    _wd_cfg, _res_cfg,
    _DEFAULT_PORTS, _bridge_url_env, _node_name,
    _CHECKIN_NOTE_PATH, _LEARNING_REMINDER_PATH,
    _app_context, )
from telegram import *  # noqa: F401,F403
from telegram import (
    _TelegramHTMLSanitizer,
    _init_transport,
    _prepare_photo_for_telegram,
    _split_protected_segments, _collapse_excess_newlines,
    _parse_media_tags,
    _sanitize_telegram_html,
    _render_md_inline_plain, _render_md_inline_html,
    _render_table_as_pre, _wrap_plain_tables,
    _pipe_tables_to_html,
    _MEDIA_GROUP_WAIT,
    _extract_msg_text,
    _build_cwd_change_notice, )
import tunnel as _tunnel_mod
from core import _build_tunnel_config
from claudecode import *  # noqa: F401,F403
from claudecode import (
    _acquire_flock, _cache_session_id, _capture_pane_text,
    _codex_load_session_id, _codex_save_session_id,
    _codex_session_id_path, _detect_os_family,
    _ensure_workspace_trusted, _find_codex_transcript, _forward_pipe_message,
    _get_remote_home, _get_tmux_send_lock, _remap_path,
    _INTERACTIVE_CONTENT, _INTERACTIVE_FOOTERS,
    _is_git_repo, _LEARNING_REMINDER_TEXT, _log_session_event, _project_slug,
    _release_flock, _remote_run, _resolve_remote_tool,
    _scan_latest_session_id, _tmux_pane_pids,
    _which_binary, )
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TRANSCRIPT_CSS = (_TEMPLATE_DIR / "transcript.css").read_text() if (_TEMPLATE_DIR / "transcript.css").exists() else ""
_TRANSCRIPT_JS = (_TEMPLATE_DIR / "transcript.js").read_text() if (_TEMPLATE_DIR / "transcript.js").exists() else ""
_CONNECTOR_CSS = (_TEMPLATE_DIR / "connector.css").read_text() if (_TEMPLATE_DIR / "connector.css").exists() else ""
class WorkerEndpointInfo(TypedDict, total=False):
    name: str; backend: str; status: str; host: str; tmux: str; protocol: str; send_example: str; machine: str; address: str; note: str
class MachinePublicDict(TypedDict, total=False):
    id: str; display_name: str; ssh_target: str | None; bridge_base_url: str; home_root: str; os_family: str; tailscale_ip: str; role: str
    configured: bool; access: str; workers: list[dict[str, str]]; worker_count: int; health: MachineHealthDict
class MachinesCatalogResponse(TypedDict):
    version: int; config_path: str; caller: str | None; machines: list[MachinePublicDict]
class ConnectorMessageLogEntry(TypedDict):
    ts: float; html: str; plain: str; targets: list[str]
class ConnectorMetadataDict(TypedDict, total=False):
    number: int; repo: str
class ConnectorAttachmentDict(TypedDict, total=False):
    path: str; filename: str; mimeType: str
class ConnectorStatusDict(TypedDict, total=False):
    name: str; running: bool; error: str; enabled: bool
class MentionRouteResult(TypedDict):
    name: str; status: str
class HookResponseBody(TypedDict, total=False):
    session: str; text: str; source: str; backend: str; escape: bool; session_id: str; name: str; worker: str; to: str; target: str
    message: str
class HealthAlertBody(TypedDict, total=False):
    worker: str; issue: str; transcript_age: int; node: str
class ForgeRegisterBody(TypedDict, total=False):
    Name: str; name: str; Host: str; host: str; Version: str; version: str; CallbackURL: str; callback_url: str; callbackUrl: str
    Tools: dict[str, object]; tools: dict[str, object]; note: str; address: str; machine: str
class PrActionBody(TypedDict, total=False):
    token: str; owner: str; repo: str; pr_num: int; body: str; path: str; line: int; commit_id: str; merge_method: str
class TelegramWebhookBody(TypedDict, total=False):
    update_id: int; message: TelegramMessageDict; callback_query: TelegramCallbackQuery; edited_message: TelegramMessageDict
class NodeConfigDict(TypedDict, total=False):
    admin_chat_id: int; tunnel: str; host: str; port: int; webhook_secret: str; connectors: dict[str, object]
class _MentionSnapshot(NamedTuple):
    target: str | None; mention_count: int; ts: float
class MentionTracker:
    def __init__(self) -> None:
        self.target: str | None = None; self.count: int = 0; self.ts: float = 0.0
    def snapshot(self) -> _MentionSnapshot:
        return _MentionSnapshot(target=self.target, mention_count=self.count, ts=self.ts)
    def restore(self, snap: _MentionSnapshot) -> None:
        self.target = snap.target; self.count = snap.mention_count; self.ts = snap.ts
class _StateSnapshot(NamedTuple):
    active: str | None; startup_notified: bool
class BridgeRuntimeState:
    def __init__(self) -> None:
        self.active: str | None = None; self.startup_notified: bool = False; self.mention: MentionTracker = MentionTracker()
    def snapshot(self) -> _StateSnapshot:
        return _StateSnapshot( active=self.active, startup_notified=self.startup_notified, )
    def restore(self, snap: _StateSnapshot) -> None:
        self.active = snap.active; self.startup_notified = snap.startup_notified
state = BridgeRuntimeState(); _last_mention = state.mention; BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NODE_NAME = os.environ.get("NODE_NAME", ""); _DEFAULT_PORTS = {"prod": 8271, "dev": 8272, "test": 8295}
if NODE_NAME and not os.environ.get("PORT"): PORT = _DEFAULT_PORTS.get(NODE_NAME, 8270)
else: PORT = int(os.environ.get("PORT", "8270"))
BRIDGE_BIND = os.environ.get("BRIDGE_BIND", "127.0.0.1"); WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
if NODE_NAME and not os.environ.get("SESSIONS_DIR"): SESSIONS_DIR = Path.home() / ".claude" / "telegram" / "nodes" / NODE_NAME / "sessions"
else: SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))
if NODE_NAME and not os.environ.get("TMUX_PREFIX"): TMUX_PREFIX = f"claude-{NODE_NAME}-"
else: TMUX_PREFIX = os.environ.get("TMUX_PREFIX", "claude-")
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
CLAUDE_SETTINGS_FILE = Path(os.environ.get("CLAUDE_SETTINGS_FILE", CLAUDE_DIR / "settings.json"))
_bridge_url_env = os.environ.get("BRIDGE_URL", "").rstrip("/")
if _bridge_url_env and not _bridge_url_env.startswith(("http://localhost", "http://127.0.0.1")): BRIDGE_URL = _bridge_url_env
else: BRIDGE_URL = f"http://localhost:{PORT}"
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/")
if BRIDGE_PUBLIC_URL and not os.environ.get("BRIDGE_BIND"):
    _pub_host = urlparse(BRIDGE_PUBLIC_URL).hostname or ""
    BRIDGE_BIND = _pub_host if _pub_host and _pub_host not in ("localhost",) else "0.0.0.0"
    if BRIDGE_BIND != "0.0.0.0": BRIDGE_URL = f"http://{BRIDGE_BIND}:{PORT}"
BRIDGE_SSH_TARGET = os.environ.get("BRIDGE_SSH_TARGET", "vps")
MACHINES_CONFIG_FILE = Path(os.environ.get(
    "MACHINES_CONFIG_FILE",
    Path.home() / ".config" / "claudecode-telegram" / "machines.json"
))
PERSISTENCE_NOTE = "They'll stay on your team."; STT_ENDPOINT = os.environ.get("STT_ENDPOINT", "http://100.126.187.125:10110/transcribe")
STT_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "10"))
API_ENDPOINTS = {
    "GET /": "API index — lists all endpoints",
    "GET /machines": "List configured machines, access hints, workers, and health",
    "GET /workers": "List active workers with send commands",
    "GET /checkin?name=<name>": "Refresh worker instructions (optional: &cwd=/path)",
    "GET /health/workers": "Watchdog state for all workers",
    "GET /health/tunnel": "Tunnel and poll fallback state",
    "GET /transcript/<name>": "Polished HTML transcript viewer for a worker",
    "GET /transcript/<name>/updates": "Poll for new transcript entries (returns {total, new})",
    "GET /tools/review/<pr_num>": "PR review viewer with diff, search, file navigation",
    "POST /messages": "Send a prompt to a worker: {worker, message, from}",
    "POST /outputs": "Hook only: publish this worker's own response to Telegram",
    "POST /notifications": "Send notification to all admin chats",
    "POST /alerts": "Hook: JSONL health alert (stale transcript detection)",
    "POST /workers": "Worker registration (name, host, version, tools, callback_url)",
    "GET /connectors": "Connector status (gmail, github — running, failures, config)",
    "POST /connectors/restarts": "Restart a connector: {name: 'gmail'|'github'}",
    "POST /tools/review/comments": "Post a PR comment (inline if path+line in body, else general)",
    "GET /tools/review/files": "Fetch file content from GitHub for diff expansion",
    "POST /tools/review/merges": "Merge a PR via GitHub API",
    "GET /guests": "List active guest sessions",
    "POST /guests": "Register a temporary guest agent session",
    "DELETE /guests?token=<token>": "Disconnect a guest session",
    "GET /relay/<channel_id>": "Relay channel guide, messages, status",
    "POST /relay/<channel_id>/send": "Guest sends message via relay channel",
    "POST /relay/<channel_id>/reply": "Worker replies via relay channel", }
_node_name = TMUX_PREFIX.strip("-").removeprefix("claude-") or "default"; FILE_INBOX_ROOT = Path(f"/tmp/claudecode-telegram/{_node_name}")
WORKER_PIPE_ROOT = Path(f"/tmp/claudecode-telegram/{_node_name}"); DEFAULT_BACKEND = "claude"; DEFAULT_WORKER_BACKEND = DEFAULT_BACKEND
PENDING_TIMEOUT = 600; TIMEOUT_TMUX_CHECK = 3; TIMEOUT_TMUX_SEND = 5; TIMEOUT_REMOTE_CMD = 10; TIMEOUT_FILE_TRANSFER = 15
TIMEOUT_GIT_OP = 30; TIMEOUT_LARGE_TRANSFER = 60; TIMEOUT_RSYNC = 120; TIMEOUT_FULL_SYNC = 600; TIMEOUT_HTTP_API = 10
TIMEOUT_HTTP_DOWNLOAD = 30; TIMEOUT_HTTP_UPLOAD = 60; TIMEOUT_PROCESS_WAIT = 3; TIMEOUT_THREAD_JOIN = 1.0; DELAY_TMUX_SEND = 0.3
DELAY_PIPE_POLL = 0.5; DELAY_STARTUP = 1.0; DELAY_STARTUP_LONG = 1.5; DELAY_RETRY = 0.5; DELAY_BRIEF = 0.05; DELAY_SHORT = 0.2
DELAY_RESPONSE_GAP = 2; DELAY_PROCESS_SETTLE = 3; DELAY_CLAUDE_LOAD = 4; TEAM_DIR = os.path.expanduser(os.environ.get("TEAM_DIR", "~/team"))
_CHECKIN_NOTE_PATH = os.path.join(TEAM_DIR, "checkin-note.txt"); _LEARNING_REMINDER_PATH = os.path.join(TEAM_DIR, "learning-reminder.txt")
DISK_WARN_THRESHOLD_PCT = _res_cfg.disk_warn_pct; DISK_ALERT_THRESHOLD_PCT = _res_cfg.disk_alert_pct
DISK_ALERT_THRESHOLD_GB = _res_cfg.disk_alert_gb; DISK_ALERT_COOLDOWN = _res_cfg.disk_cooldown; CPU_HOG_THRESHOLD_PCT = _res_cfg.cpu_hog_pct
CPU_HOG_DURATION_MIN = _res_cfg.cpu_hog_duration_min; CPU_HOG_ALERT_COOLDOWN = _res_cfg.cpu_hog_cooldown
WORKTREE_ALERT_THRESHOLD_GB = _res_cfg.worktree_threshold_gb; WORKTREE_ALERT_COOLDOWN = _res_cfg.worktree_cooldown
MEM_ALERT_THRESHOLD_PCT = _res_cfg.mem_threshold_pct; MEM_ALERT_THRESHOLD_GB = _res_cfg.mem_threshold_gb
MEM_ALERT_COOLDOWN = _res_cfg.mem_cooldown; IO_ALERT_IOWAIT_PCT = _res_cfg.io_iowait_pct; IO_ALERT_COOLDOWN = _res_cfg.io_cooldown
INFRA_ALERT_COOLDOWN = _res_cfg.infra_cooldown
@dataclass
class WorkerRegistryEntry:
    backend: str = "claude"; protocol: str = ""; callback_url: str = ""; host: str | None = None; version: str = ""
    chat_id: int | None = None; hire_time: int = 0; tools: dict[str, object] | None = None; home_host: str = ""; home_cwd: str = ""
    def to_dict(self) -> RegistryWorkerDict:
        result: RegistryWorkerDict = {"backend": self.backend}
        for field_name in ("protocol", "callback_url", "host", "version", "chat_id",
                           "hire_time", "tools", "home_host", "home_cwd"):
            value = getattr(self, field_name)
            if value is not None and value != "" and value != 0: result[field_name] = value
        return result
    @classmethod
    def from_dict(cls: type["WorkerRegistryEntry"], data: RegistryWorkerDict) -> "WorkerRegistryEntry":
        return cls(backend=data.get("backend", "claude"), protocol=data.get("protocol", ""),
            callback_url=data.get("callback_url", ""), host=data.get("host"), version=data.get("version", ""),
            chat_id=data.get("chat_id"), hire_time=data.get("hire_time", 0), tools=data.get("tools"),
            home_host=data.get("home_host") or "", home_cwd=data.get("home_cwd") or "")
@dataclass
class WorkerRecord:
    name: str; backend: str = "claude"; host: str | None = None; tmux_name: str = ""; callback_url: str = ""; protocol: str = ""
    version: str = ""; tools: dict[str, object] | None = None; chat_id: int | None = None; cwd: str = ""; home_host: str = ""
    home_cwd: str = ""
    @property
    def is_remote(self) -> bool:
        return bool(self.host)
    @property
    def is_interactive(self) -> bool:
        return self.backend == "claude"
def get_worker_host(name: str) -> str | None:
    registry = _load_registry(); worker = registry.get("workers", {}).get(name, {}); return worker.get("host")
class MachineConfigError(ValueError):
    pass
@dataclass(frozen=True)
class Machine:
    id: str; ssh_target: str | None; bridge_base_url: str; home_root: str; os_family: str; display_name: str = ""; tailscale_ip: str = ""
    role: str = ""; configured: bool = True
    @property
    def is_local(self) -> bool:
        return self.ssh_target is None
    def public_dict(self) -> MachinePublicDict:
        return {"id": self.id, "display_name": self.display_name or self.id, "ssh_target": self.ssh_target,
            "bridge_base_url": self.bridge_base_url, "home_root": self.home_root, "os_family": self.os_family,
            "tailscale_ip": self.tailscale_ip, "role": self.role, "configured": self.configured}
def _validate_machine_id(machine_id: str) -> str:
    if not isinstance(machine_id, str) or not machine_id: raise MachineConfigError("machine id must be a non-empty string")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", machine_id): raise MachineConfigError(f"invalid machine id: {machine_id!r}")
    return machine_id
def _coerce_optional_str(value: object, field: str, machine_id: str) -> str:
    if value is None: return ""
    if not isinstance(value, str): raise MachineConfigError(f"machine {machine_id!r} field {field!r} must be a string")
    return value
def _implicit_local_machine() -> Machine:
    _id = BRIDGE_SSH_TARGET or "vps"
    return Machine(id=_id, ssh_target=None, bridge_base_url=BRIDGE_URL, home_root=str(Path.home()),
        os_family=_detect_os_family(), display_name=_id.upper(), role="bridge", configured=False)
def load_machines_config(path: Path | None = None) -> dict[str, Machine]:
    config_path = Path(path) if path is not None else MACHINES_CONFIG_FILE
    if not config_path.exists(): return {_implicit_local_machine().id: _implicit_local_machine()}
    try: data = cast(NodeConfigDict, json.loads(config_path.read_text()))
    except json.JSONDecodeError as e: raise MachineConfigError(f"{config_path}: invalid JSON: {e}") from e
    except OSError as e: raise MachineConfigError(f"{config_path}: cannot read: {e}") from e
    if not isinstance(data, dict): raise MachineConfigError(f"{config_path}: root must be an object")
    if data.get("version") != 1: raise MachineConfigError(f"{config_path}: version must be 1")
    raw_machines = data.get("machines")
    if not isinstance(raw_machines, dict) or not raw_machines:
        raise MachineConfigError(f"{config_path}: machines must be a non-empty object")
    machines: dict[str, Machine] = {}; ssh_targets: dict[str, str] = {}; local_count = 0
    for raw_id, raw in raw_machines.items():
        machine_id = _validate_machine_id(raw_id)
        if not isinstance(raw, dict): raise MachineConfigError(f"{config_path}: machine {machine_id!r} must be an object")
        missing = [k for k in ("ssh_target", "bridge_base_url", "home_root", "os_family") if k not in raw]
        if missing: raise MachineConfigError(f"{config_path}: machine {machine_id!r} missing {', '.join(missing)}")
        ssh_target = raw.get("ssh_target")
        if ssh_target is not None and not isinstance(ssh_target, str):
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} ssh_target must be string or null")
        if ssh_target == "": raise MachineConfigError(f"{config_path}: machine {machine_id!r} ssh_target cannot be empty")
        if ssh_target is None: local_count += 1
        elif ssh_target in ssh_targets:
            raise MachineConfigError(
                f"{config_path}: ssh_target {ssh_target!r} used by both "
                f"{ssh_targets[ssh_target]!r} and {machine_id!r}" )
        else: ssh_targets[ssh_target] = machine_id
        bridge_base_url = _coerce_optional_str(raw.get("bridge_base_url"), "bridge_base_url", machine_id).rstrip("/")
        home_root = _coerce_optional_str(raw.get("home_root"), "home_root", machine_id).rstrip("/")
        os_family = _coerce_optional_str(raw.get("os_family"), "os_family", machine_id)
        if not bridge_base_url: raise MachineConfigError(f"{config_path}: machine {machine_id!r} bridge_base_url cannot be empty")
        if not home_root.startswith("/"): raise MachineConfigError(f"{config_path}: machine {machine_id!r} home_root must be absolute")
        if os_family not in ("linux", "darwin"):
            raise MachineConfigError(f"{config_path}: machine {machine_id!r} os_family must be linux or darwin")
        machines[machine_id] = Machine(id=machine_id, ssh_target=ssh_target, bridge_base_url=bridge_base_url,
            home_root=home_root, os_family=os_family,
            display_name=_coerce_optional_str(raw.get("display_name", machine_id), "display_name", machine_id),
            tailscale_ip=_coerce_optional_str(raw.get("tailscale_ip", ""), "tailscale_ip", machine_id),
            role=_coerce_optional_str(raw.get("role", ""), "role", machine_id))
    if local_count != 1: raise MachineConfigError(f"{config_path}: exactly one local machine with ssh_target=null is required")
    return machines
def get_machine_catalog(force_reload: bool = False) -> dict[str, Machine]:
    with remote_cache.lock:
        if force_reload or remote_cache.machines is None or remote_cache.machines_path != MACHINES_CONFIG_FILE:
            remote_cache.machines = load_machines_config(MACHINES_CONFIG_FILE); remote_cache.machines_path = MACHINES_CONFIG_FILE
        return dict(remote_cache.machines)
def _machine_for_worker_host(host: str | None, machines: dict[str, Machine]) -> Machine:
    if host is None:
        for machine in machines.values():
            if machine.is_local: return machine
        return _implicit_local_machine()
    for machine in machines.values():
        if machine.ssh_target == host: return machine
    machine_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", host).strip("-") or "unknown"
    return Machine(
        id=machine_id,
        ssh_target=host,
        bridge_base_url=BRIDGE_PUBLIC_URL or "",
        home_root="",
        os_family="",
        display_name=host,
        role="worker-host",
        configured=False, )
def _machine_health(machine: Machine) -> MachineHealthDict:
    host_label = machine.ssh_target or "VPS"
    with watchdog.lock:
        health: MachineHealthDict = {"status": "up" if machine.is_local else "unknown", "down_since": None, "last_error": None,
            "disk": host_health.disk_usage.get(host_label), "memory": host_health.mem_usage.get(host_label), "io": host_health.io_usage.get(host_label)}
        if machine.ssh_target:
            if host_health.down.get(machine.ssh_target, False):
                health["status"] = "down"; health["down_since"] = host_health.down_since.get(machine.ssh_target)
                health["last_error"] = host_health.last_error.get(machine.ssh_target)
            elif (
                machine.ssh_target in host_health.ssh_failures
                or machine.ssh_target in host_health.disk_usage
                or machine.ssh_target in host_health.mem_usage
                or machine.ssh_target in host_health.io_usage ):
                health["status"] = "up"
    return health
def _machine_access(machine: Machine, caller_host: str | None) -> str:
    target_host = machine.ssh_target
    if caller_host == target_host: return "local"
    if target_host is None: return f"ssh {BRIDGE_SSH_TARGET}"
    return f"ssh {target_host}"
def get_machines(caller_from: str | None = None) -> MachinesCatalogResponse:
    machines = get_machine_catalog(); registered = get_registered_sessions()
    caller_info = registered.get(caller_from, {}) if caller_from else {}
    caller_host = caller_info.get("host") if caller_info else (get_worker_host(caller_from) if caller_from else None)
    rows: dict[str, MachinePublicDict] = {}
    for machine in machines.values():
        row = machine.public_dict(); row["access"] = _machine_access(machine, caller_host); row["workers"] = []; row["worker_count"] = 0
        row["health"] = _machine_health(machine); rows[machine.id] = row
    for name, info in registered.items():
        host = info.get("host") if "host" in info else get_worker_host(name); machine = _machine_for_worker_host(host, machines)
        if machine.id not in rows:
            row = machine.public_dict(); row["access"] = _machine_access(machine, caller_host); row["workers"] = []; row["worker_count"] = 0
            row["health"] = _machine_health(machine); rows[machine.id] = row
        rows[machine.id]["workers"].append({"name": name, "backend": info.get("backend", DEFAULT_BACKEND),
            "status": "online" if info.get("tmux") or info.get("callback_url") else "exited"})
        rows[machine.id]["worker_count"] += 1
    return {"version": 1, "config_path": str(MACHINES_CONFIG_FILE), "caller": caller_from or None, "machines": list(rows.values())}
def parse_hire_args(raw: str) -> tuple[str, str]:
    parts = [p for p in (raw or "").split() if p]; backend = DEFAULT_BACKEND; name_parts = []; i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--backend" and i + 1 < len(parts): backend = parts[i + 1]; i += 2; continue
        elif part == "--codex": backend = "codex"
        elif part.startswith("--"): pass
        else: name_parts.append(part)
        i += 1
    if len(name_parts) != 1: return "", backend
    name = name_parts[0]
    for backend_name in list_backends():
        prefix = f"{backend_name}-"
        if name.startswith(prefix): backend = backend_name; name = name[len(prefix):]; break
    if not is_valid_backend(backend): return name, backend
    return name, backend
def _remote_copy(src: str, dst: str, host: str | None = None, direction: str = "push") -> None:
    if not host: shutil.copy2(src, dst)
    elif direction == "push":
        _subprocess_runner.run(["scp", "-q", src, f"{host}:{dst}"], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
    else: _subprocess_runner.run(["scp", "-q", f"{host}:{src}", dst], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
def parse_worker_target(target: str) -> ParsedWorkerTarget:
    if "@" in target:
        name, host = target.rsplit("@", 1)
        return ParsedWorkerTarget(name, host)
    return ParsedWorkerTarget(target, None)
def _registry_update_teleport(name: str, host: str, home_host: str | None, home_cwd: str | None) -> None:
    with watchdog.lock:
        data = _load_registry(); worker = data.get("workers", {}).get(name, {})
        worker["host"] = host
        worker["home_host"] = home_host
        worker["home_cwd"] = home_cwd
        data.setdefault("workers", {})[name] = worker
        _save_registry(data)
def _registry_clear_teleport(name: str) -> None:
    with watchdog.lock:
        data = _load_registry(); worker = data.get("workers", {}).get(name, {})
        worker.pop("host", None)
        worker.pop("home_host", None)
        worker.pop("home_cwd", None)
        data.setdefault("workers", {})[name] = worker
        _save_registry(data)
def mark_hook_event(session_name: str) -> None:
    with watchdog.lock: watchdog.last_hook_ts[session_name] = _clock.time()
def kill_adapter(name: str) -> None:
    with processes.adapter_pids_lock: entry = processes.adapter_pids.pop(name, None)
    if entry is None: return
    proc, stderr_fh = entry
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=TIMEOUT_PROCESS_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=TIMEOUT_THREAD_JOIN)
    if stderr_fh:
        try: stderr_fh.close()
        except OSError as exc: _log(_LOG_DEBUG, "io:kill_adapter", f"{type(exc).__name__}: {exc}")
def _read_learning_reminder(name: str) -> str:
    try:
        if os.path.isfile(_LEARNING_REMINDER_PATH):
            text = Path(_LEARNING_REMINDER_PATH).read_text().strip()
            if text: return text.replace("{name}", name)
    except OSError as e: _log(_LOG_WARN, "bridge", f"Failed to read learning reminder from {_LEARNING_REMINDER_PATH}: {e}")
    return _LEARNING_REMINDER_TEXT.replace("{name}", name)
def _new_reminder_state() -> ReminderState:
    now = _clock.time()
    return { "response_count": 0, "last_reminder_ts": now, "last_response_ts": now, "reminder_pending": False, }
def _reset_learning_reminder(name: str) -> None:
    with learning_reminders.lock: learning_reminders.state[name] = _new_reminder_state()
def _fire_reminder(name: str, st: ReminderState) -> None:
    st["response_count"] = 0; st["last_reminder_ts"] = _clock.time(); st["reminder_pending"] = True
    _task_pool.submit(_send_learning_reminder, name, _read_learning_reminder(name))
def _check_learning_reminder(name: str) -> None:
    with learning_reminders.lock:
        st = learning_reminders.state.get(name)
        if st is None: st = _new_reminder_state(); learning_reminders.state[name] = st
        st["last_response_ts"] = _clock.time()
        if st.get("reminder_pending"): st["reminder_pending"] = False; st["response_count"] = 1; return
        st["response_count"] = st.get("response_count", 0) + 1
        if st["response_count"] >= LEARNING_REMINDER_RESPONSE_THRESHOLD: _fire_reminder(name, st)
def _scan_idle_workers() -> None:
    try:
        now = _clock.time(); idle_threshold = LEARNING_REMINDER_IDLE_HOURS * 3600; to_fire = []
        with learning_reminders.lock:
            for name, st in learning_reminders.state.items():
                if st.get("reminder_pending"): continue
                if st.get("response_count", 0) <= 1: continue
                idle_seconds = now - st.get("last_response_ts", now)
                since_reminder = now - st.get("last_reminder_ts", now)
                if idle_seconds >= idle_threshold and since_reminder >= idle_threshold: to_fire.append(name)
            for name in to_fire: _fire_reminder(name, learning_reminders.state[name])
    except KeyError as e: _log(_LOG_ERROR, "bridge", f"Learning reminder idle scan error: {e}")
    finally:
        _schedule_idle_scan()
def _seed_learning_reminder_state(worker_names: Iterable[str]) -> None:
    with learning_reminders.lock:
        for name in worker_names:
            if name not in learning_reminders.state: learning_reminders.state[name] = _new_reminder_state()
def _schedule_idle_scan() -> None:
    learning_reminders.idle_scan_timer = threading.Timer(1800, _scan_idle_workers)
    learning_reminders.idle_scan_timer.name = "idle-scan"
    learning_reminders.idle_scan_timer.daemon = True
    learning_reminders.idle_scan_timer.start()
def _send_learning_reminder(name: str, text: str) -> None:
    try:
        _clock.sleep(DELAY_RESPONSE_GAP)
        if send_to_worker(name, text): _log(_LOG_INFO, "worker", f"Learning reminder sent to {name}")
        else: _log(_LOG_WARN, "bridge", f"Learning reminder: failed to send to {name}")
    except (ConnectionError, OSError, TimeoutError) as e: _log(_LOG_ERROR, "bridge", f"Learning reminder error for {name}: {e}")
def _load_registry() -> RegistryFileDict:
    try:
        if not WORKER_REGISTRY_FILE.exists(): return {}
        raw = WORKER_REGISTRY_FILE.read_text(); data = cast(RegistryFileDict, json.loads(raw))
        if not isinstance(data, dict) or "workers" not in data: raise ValueError("invalid registry format")
        return data
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        if WORKER_REGISTRY_FILE.exists():
            corrupt_path = WORKER_REGISTRY_FILE.with_suffix(f".corrupt.{int(_clock.time())}")
            _log(_LOG_INFO, "worker", f"Corrupt worker registry, renaming to {corrupt_path}: {e}")
            try: WORKER_REGISTRY_FILE.rename(corrupt_path)
            except OSError as exc: _log(_LOG_DEBUG, "io:_load_registry", f"{type(exc).__name__}: {exc}")
        return {}
def _save_registry(data: RegistryFileDict) -> None:
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(NODE_DIR), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f: json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(WORKER_REGISTRY_FILE))
        except OSError:
            try: os.unlink(tmp_path)
            except OSError as exc: _log(_LOG_DEBUG, "io:_save_registry", f"{type(exc).__name__}: {exc}")
            raise
    except OSError as e: _log(_LOG_WARN, "worker", f"Failed to save worker registry: {e}")
def _registry_add(name: str, backend: str, chat_id: ChatId | None = None,
                   host: str | None = None) -> None:
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data: data = {"version": 1, "workers": {}}
        existing = data.get("workers", {}).get(name, {}); preserved_keys = {"host", "home_host", "home_cwd"}
        entry = {k: v for k, v in existing.items() if k in preserved_keys}
        entry.update({ "backend": backend, "chat_id": chat_id, "hire_time": int(_clock.time()),
        })
        if host: entry["host"] = host
        data["workers"][name] = cast(RegistryWorkerDict, entry)
        _save_registry(data)
def _registry_add_callback(name: str, callback_url: str, host: str = "",
                           version: str = "", tools: dict[str, object] | None = None) -> None:
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data: data = {"version": 1, "workers": {}}
        entry: dict[str, object] = {"backend": DEFAULT_BACKEND, "protocol": "http",
            "callback_url": callback_url.rstrip("/"), "chat_id": None, "hire_time": int(_clock.time())}
        if host: entry["host"] = host
        if version: entry["version"] = version
        if isinstance(tools, dict): entry["tools"] = tools
        data["workers"][name] = cast(RegistryWorkerDict, entry)
        _save_registry(data)
def _registry_remove(name: str) -> None:
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data: return
        data["workers"].pop(name, None)
        _save_registry(data)
def _set_worker_cwd(name: str, cwd: str) -> None:
    normalized = normalize_cwd(cwd)
    with watchdog.lock:
        if normalized: watchdog.worker_cwds[name] = normalized
        else: watchdog.worker_cwds.pop(name, None)
def _get_worker_cwd(name: str) -> str:
    with watchdog.lock: cwd = watchdog.worker_cwds.get(name)
    return cwd if isinstance(cwd, str) else ""
def _registry_bootstrap(registered: dict[str, TmuxSessionDict]) -> None:
    if WORKER_REGISTRY_FILE.exists(): return
    if not registered: return
    data: RegistryFileDict = {"version": 1, "workers": {}}
    for name, session in registered.items():
        backend = normalize_backend(session.get("backend"))
        data["workers"][name] = { "backend": backend, "chat_id": None, "hire_time": int(_clock.time()), }
    _save_registry(data)
    _log(_LOG_INFO, "registry", f"Registry bootstrapped with {len(registered)} workers: {', '.join(registered.keys())}")
def read_checkin_note() -> str:
    try:
        path = _CHECKIN_NOTE_PATH
        if os.path.isfile(path):
            text = Path(path).read_text().strip()
            if text: return text
    except OSError as e: _log(_LOG_WARN, "bridge", f"Failed to read checkin note from {_CHECKIN_NOTE_PATH}: {e}")
    return ""
def get_inbox_dir(session_name: str) -> Path:
    return FILE_INBOX_ROOT / session_name / "inbox"
def ensure_inbox_dir(session_name: str) -> Path:
    inbox = get_inbox_dir(session_name)
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    inbox.chmod(0o700)
    return inbox
def cleanup_inbox(session_name: str) -> None:
    inbox = get_inbox_dir(session_name)
    if inbox.exists():
        for f in inbox.iterdir():
            try: f.unlink()
            except OSError as e: _log(_LOG_WARN, "bridge", f"Failed to delete {f}: {e}")
def get_workers(caller_from: str | None = None) -> list[WorkerEndpointInfo]:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.get_workers(caller_from=caller_from)
def get_pending_file(name: str) -> Path:
    return get_session_dir(name) / "pending"
def _get_pending_lock(name: str) -> threading.Lock:
    with processes.pending_locks_guard:
        if name not in processes.pending_locks: processes.pending_locks[name] = threading.Lock()
        return processes.pending_locks[name]
def set_pending(name: str, chat_id: ChatId) -> None:
    session_dir = ensure_session_dir(name); pending = session_dir / "pending"; chat_id_file = session_dir / "chat_id"
    _tmp_p = pending.with_suffix('.tmp')
    _tmp_p.write_text(str(int(_clock.time())))
    _tmp_p.chmod(0o600)
    os.replace(str(_tmp_p), str(pending))
    _tmp_c = chat_id_file.with_suffix('.tmp')
    _tmp_c.write_text(str(chat_id))
    _tmp_c.chmod(0o600)
    os.replace(str(_tmp_c), str(chat_id_file))
    _sync_chat_id_to_remote(name, str(chat_id_file))
def _remap_sessions_dir(host: str | None) -> str:
    return _remap_path(str(SESSIONS_DIR), host)
def _sync_chat_id_to_remote(name: str, local_chat_id_path: str) -> None:
    host = get_worker_host(name)
    if not host: return
    try:
        remote_sessions_dir = _remap_sessions_dir(host)
        _remote_run(["mkdir", "-p", f"{remote_sessions_dir}/{name}"],
                     host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK)
        _remote_copy(local_chat_id_path, f"{remote_sessions_dir}/{name}/chat_id", host=host, direction="push")
    except (subprocess.SubprocessError, OSError) as e: _log(_LOG_WARN, "set_pending", f"Failed to sync chat_id to {host} for {name}: {e}")
def clear_pending(name: str) -> None:
    session_dir = get_session_dir(name); pending = session_dir / "pending"
    try: pending.unlink()
    except OSError as exc: _log(_LOG_DEBUG, "cleanup:clear_pending", f"{type(exc).__name__}: {exc}")
def is_pending(name: str) -> bool:
    pending = get_pending_file(name)
    if not pending.exists(): return False
    try:
        ts = int(pending.read_text().strip())
        if (_clock.time() - ts) > PENDING_TIMEOUT: return False
        return True
    except (OSError, ValueError): return False
def try_set_pending(name: str, chat_id: ChatId) -> bool:
    with _get_pending_lock(name):
        if is_pending(name): return False
        set_pending(name, chat_id)
        return True
def _pending_timestamp(name: str) -> int | None:
    pending = get_pending_file(name)
    if not pending.exists(): return None
    try: return int(pending.read_text().strip())
    except OSError: return None
def _wait_for_restart_ready(tmux_name: str, backend_name: str, timeout: float = 45.0, host: str | None = None) -> bool:
    backend = get_backend(backend_name)
    if not backend.is_interactive: return tmux_exists(tmux_name, host=host)
    deadline = _clock.time() + timeout
    while _clock.time() < deadline:
        if not tmux_exists(tmux_name, host=host): return False
        activity, _, _ = _read_tmux_activity(tmux_name, host=host)
        if activity == "Idle at prompt": return True
        _clock.sleep(DELAY_RETRY)
    return False
def _send_to_callback_worker(name: str, message: str, from_name: str = "manager", session: TmuxSessionDict | None = None) -> bool:
    callback_url = (session or {}).get("callback_url", "")
    if not callback_url: callback_url = _load_registry().get("workers", {}).get(name, {}).get("callback_url", "")
    if not callback_url: return False
    msg_url = callback_url.rstrip("/")
    if not msg_url.endswith("/msg"): msg_url = f"{msg_url}/msg"
    body = json.dumps({"from": from_name, "text": message}).encode()
    req = urllib.request.Request( msg_url, data=body, headers={"Content-Type": "application/json"}, method="POST", )
    try:
        with _urlopen(req, timeout=TIMEOUT_HTTP_API) as resp: return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_WARN, "bridge", f"Callback send failed for '{name}' at {msg_url}: {e}")
        return False
from worker_manager import WorkerManager
def _sync_worker_manager() -> None:
    assert worker_manager is not None, "worker_manager not initialized"
    worker_manager.sessions_dir = SESSIONS_DIR; worker_manager.tmux_prefix = TMUX_PREFIX
def worker_set_pending(name: str, chat_id: ChatId) -> None:
    set_pending(name, chat_id)
def worker_send(name: str, message: str, chat_id: int | None = None, session: TmuxSessionDict | None = None) -> bool:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.send(name, message, chat_id, session)
def scan_tmux_sessions() -> dict[str, TmuxSessionDict]:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.scan_tmux_sessions()
def get_registered_sessions(registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.get_registered_sessions(registered)
def send_to_worker(name: str, message: str, chat_id: int | None = None) -> bool:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.send(name, message, chat_id)
worker_manager = WorkerManager(SESSIONS_DIR, TMUX_PREFIX)  # type: ignore[assignment]
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from html.parser import HTMLParser
PostRouteHandler = Callable[["Handler", bytes, re.Match[str] | None], None]
GetRouteHandler = Callable[["Handler", ParseResult, re.Match[str] | None], None]
class PostRouteResolution(NamedTuple):
    handler: PostRouteHandler | None
    match: re.Match[str] | None
class GetRouteResolution(NamedTuple):
    handler: GetRouteHandler | None
    match: re.Match[str] | None
try:
    from connectors import GmailConnector, GitHubConnector
    GMAIL_IMPORT_ERROR: ImportError | None = None
    GITHUB_IMPORT_ERROR: ImportError | None = None
except ImportError as e:
    GmailConnector = None  # type: ignore[assignment,misc]
    GitHubConnector = None  # type: ignore[assignment,misc]
    GMAIL_IMPORT_ERROR = e; GITHUB_IMPORT_ERROR = e
_message_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="msg")
_task_pool    = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task")
class ReuseAddrServer(ThreadingHTTPServer):
    allow_reuse_address = True
class ConnectorRegistry:
    def __init__(self) -> None:
        from typing import Any as _Any
        self.gmail: _Any = None  # type: ignore[explicit-any]
        self.github: _Any = None  # type: ignore[explicit-any]
        self._log: dict[str, collections.deque[ConnectorMessageLogEntry]] = {}
        self._lock: threading.Lock = threading.Lock()
    def stop_all(self) -> None:
        for name, inst in [("gmail", self.gmail), ("github", self.github)]:
            if inst is not None:
                try:
                    inst.stop()
                    print(f"{name.title()} connector stopped")
                except (RuntimeError, OSError) as exc: _log(_LOG_DEBUG, f"shutdown:{name}", f"{type(exc).__name__}: {exc}")
    def log_message(self, tag: str, html_text: str, plain_text: str, targets: list[str]) -> None:
        with self._lock:
            if tag not in self._log: self._log[tag] = collections.deque(maxlen=20)
            self._log[tag].append({
                "ts": _clock.time(),
                "html": html_text,
                "plain": plain_text,
                "targets": targets or [],
            })
    def get_log(self, tag: str) -> list[ConnectorMessageLogEntry]:
        with self._lock: return list(self._log.get(tag, []))
connectors = ConnectorRegistry()
tunnel_manager: _tunnel_mod.TunnelManager | None = None
if not BRIDGE_PUBLIC_URL:
    try:
        _ts_ip = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True,
            text=True, timeout=3).stdout.strip()  # noqa: direct call — runs before constants init
        if _ts_ip: BRIDGE_PUBLIC_URL = f"http://{_ts_ip}:{PORT}"
    except (subprocess.SubprocessError, OSError): pass
GMAIL_ENABLED = os.environ.get("GMAIL_ENABLED", "0") == "1"
GMAIL_POLL_INTERVAL = int(os.environ.get("GMAIL_POLL_INTERVAL", "45"))
GMAIL_FROM_FILTER = os.environ.get("GMAIL_FROM_FILTER", "ngocthinhdp@gmail.com")
GMAIL_GWS_BIN = os.environ.get("GMAIL_GWS_BIN", os.path.expanduser("~/bin/gws"))
if GMAIL_ENABLED and not GMAIL_FROM_FILTER.strip():
    raise RuntimeError("GMAIL_FROM_FILTER must be set when GMAIL_ENABLED=1 (security: sender filter required)")
GITHUB_ENABLED = os.environ.get("BRIDGE_GHPOLL_ENABLED", "0") == "1"
GITHUB_POLL_INTERVAL = int(os.environ.get("BRIDGE_GHPOLL_INTERVAL", "60"))
_GITHUB_REPO_RAW = os.environ.get("BRIDGE_GHPOLL_REPO", "BasedHardware/omi")
GITHUB_REPOS: list[str] = [r.strip() for r in _GITHUB_REPO_RAW.split(",") if r.strip()]
GITHUB_FROM_USER = os.environ.get("BRIDGE_GHPOLL_USER", "beastoin")
if GITHUB_ENABLED and not GITHUB_FROM_USER.strip():
    raise RuntimeError("BRIDGE_GHPOLL_USER must be set when BRIDGE_GHPOLL_ENABLED=1 (security: sender filter required)")
TELEPORT_RSYNC_EXCLUDES = [
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".next", "build", "dist", "target", ".gradle", ".cache",
    ".tox", ".mypy_cache", ".pytest_cache", "*.pyc",
    ".build", ".claude/worktrees", ]
def _parse_codex_transcript(path: str, host: str | None = None) -> list[CodexTranscriptEntry]:
    try:
        if host:
            r = _remote_run(["cat", path], host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0: return []
            content = r.stdout
        else: content = Path(path).read_text()
    except (subprocess.SubprocessError, OSError): return []
    messages = []
    for line in content.strip().split("\n"):
        if not line.strip(): continue
        try: ev = cast(dict[str, object], json.loads(line))
        except json.JSONDecodeError: continue
        ev_type = str(ev.get("type", "")); raw_payload = ev.get("payload", {})
        payload = cast(dict[str, object], raw_payload) if isinstance(raw_payload, dict) else {}
        ts = str(ev.get("timestamp", ""))
        if ev_type == "response_item":
            item_type = str(payload.get("type", ""))
            if item_type == "message":
                role = str(payload.get("role", "assistant"))
                text_parts: list[str] = []
                raw_content = payload.get("content", [])
                for c in cast(list[dict[str, object]], raw_content) if isinstance(raw_content, list) else []:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "input_text") and c.get("text"):
                        text_parts.append(str(c["text"]))
                text = "\n".join(text_parts).strip()
                if text and role != "developer": messages.append({"role": role, "text": text, "timestamp": ts})
            elif item_type == "function_call":
                name = str(payload.get("name", ""))
                messages.append({"role": "tool_use", "text": f"[{name}]", "timestamp": ts})
            elif item_type == "function_call_output":
                output = str(payload.get("output", ""))
                if output: messages.append({"role": "tool_result", "text": output[:500], "timestamp": ts})
    return cast(list[CodexTranscriptEntry], messages)
def _read_codex_transcript(worker_name: str) -> list[CodexTranscriptEntry]:
    host = get_worker_host(worker_name); path = _find_codex_transcript(worker_name, host=host)
    if not path: return []
    return _parse_codex_transcript(path, host=host)
media_groups = MediaGroupState()
REWIND_TIMEOUT: int = 24 * 60 * 60
PR_REVIEW_EXTEND: int = 300
class TokenStore:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
    def add_rewind(self, token: str, name: str, timeout: int = REWIND_TIMEOUT) -> None:
        with self._lock: REWIND_TOKENS[token] = {"name": name, "expires_at": _clock.time() + timeout}
    def validate_rewind(self, token: str | None, extend: bool = True) -> RewindTokenEntry | None:
        if not token: return None
        now = _clock.time()
        with self._lock:
            expired = [k for k, v in REWIND_TOKENS.items() if v["expires_at"] <= now]
            for k in expired: del REWIND_TOKENS[k]
            entry = REWIND_TOKENS.get(token)
            if entry and extend: entry["expires_at"] = now + REWIND_TIMEOUT
            return entry
    def add_pr_review(self, token: str, pr_num: int, owner: str, repo: str) -> None:
        with self._lock:
            PR_REVIEW_TOKENS[token] = {"pr_num": pr_num, "owner": owner, "repo": repo,
                                       "expires_at": _clock.time() + PR_REVIEW_EXTEND}
    def validate_pr_review(self, token: str | None, extend: bool = True) -> PrReviewTokenEntry | None:
        if not token: return None
        now = _clock.time()
        with self._lock:
            expired = [k for k, v in PR_REVIEW_TOKENS.items() if v["expires_at"] <= now]
            for k in expired: del PR_REVIEW_TOKENS[k]
            entry = PR_REVIEW_TOKENS.get(token)
            if entry and extend: entry["expires_at"] = now + PR_REVIEW_EXTEND
            return entry
tokens = TokenStore()
def _extract_file_id(msg: TelegramMessageDict) -> tuple[str | None, str]:
    """Extract (file_id, media_label) from a Telegram message dict."""
    animation = msg.get("animation"); photo = msg.get("photo")
    document = msg.get("document"); video = msg.get("video")
    audio = msg.get("audio"); voice = msg.get("voice")
    video_note = msg.get("video_note"); sticker = msg.get("sticker")
    if animation: return animation.get("file_id"), "GIF"
    if photo: return max(photo, key=lambda p: p.get("file_size", 0)).get("file_id"), "image"
    if document:
        if document.get("mime_type", "").startswith("image/"): return document.get("file_id"), "image"
        return document.get("file_id"), "file"
    if video: return video.get("file_id"), "video"
    if audio: return audio.get("file_id"), "audio"
    if voice: return voice.get("file_id"), "voice"
    if video_note: return video_note.get("file_id"), "video note"
    if sticker: return sticker.get("file_id"), "sticker"
    return None, "media"
def _format_offline_warning(statuses: list[MentionRouteResult | None]) -> str | None:
    """Build offline warning from route results. Returns None if no offline workers."""
    sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
    offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
    if not offline: return None
    parts = [f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline."]
    if sent_to: parts.append(f"Delivered to {', '.join(sent_to)}.")
    return " ".join(parts)
def _gh_api_call(args: list[str], *, input_data: str | None = None,
                 timeout: int = TIMEOUT_FILE_TRANSFER) -> tuple[bool, str]:
    """Run gh API command. Returns (success, stdout_or_stderr)."""
    try:
        kw: dict[str, object] = {"capture_output": True, "text": True, "timeout": timeout}
        if input_data is not None: kw["input"] = input_data
        r = _subprocess_runner.run(["gh", "api"] + args, **kw)
        if r.returncode != 0: return False, r.stderr.strip() or r.stdout.strip()
        return True, r.stdout
    except subprocess.TimeoutExpired: return False, "timeout"
def _fetch_remote_file(host: str, remote_path: str) -> str | None:
    original_name = Path(remote_path).name; tmp_dir = tempfile.mkdtemp(prefix="remote-file-")
    local_path = os.path.join(tmp_dir, original_name)
    try:
        r = _subprocess_runner.run(
            ["rsync", "-az", f"{host}:{remote_path}", local_path],
            capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
        if r.returncode == 0 and os.path.getsize(local_path) > 0: return local_path
        if r.returncode != 0: _log(_LOG_ERROR, "bridge", f"rsync failed (exit {r.returncode}): {host}:{remote_path} -> {r.stderr.strip()}")
    except (subprocess.SubprocessError, OSError) as e: _log(_LOG_WARN, "bridge", f"Remote file fetch failed: {host}:{remote_path} -> {e}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None
def _localize_media(name: str, media_list: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    host = get_worker_host(name)
    if not host: return media_list
    result: list[tuple[str | None, str]] = []
    for file_path, caption in media_list:
        if not file_path:
            result.append((file_path, caption))
            continue
        local = _fetch_remote_file(host, file_path)
        if local: result.append((local, caption))
        else:
            _log(_LOG_WARN, "bridge", f"Cannot fetch remote file {host}:{file_path} for {name}")
            result.append((None, f"[Fetch failed: {file_path}]"))
    return result
def _parse_response_media(name: str, text: str) -> tuple[str, list[tuple[str | None, str]], list[tuple[str | None, str]]]:
    host = get_worker_host(name)
    if host:
        _accept_all: Callable[[str | Path], FileValidation] = lambda p: FileValidation(True, Path(p))
        clean_text, images = _parse_media_tags(text, "image", _accept_all)
        clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
    else:
        clean_text, images = parse_image_tags(text)
        clean_text, files = parse_file_tags(clean_text)
    images = _localize_media(name, images); files = _localize_media(name, files)
    return clean_text, images, files
def _send_text_via_telegram(name: str, clean_text: str, chat_id: int, log_prefix: str) -> None:
    rich_sent = False; rich_failed_at = -1
    rich_chunks: list[str] = []
    prev_msg_id: int | None = None
    if hasattr(transport, 'send_rich_text'):
        rich_text = clean_text.lstrip(); prefix_lower = f"{name}:".lower()
        if rich_text.lower().startswith(prefix_lower): rich_text = rich_text[len(prefix_lower):].lstrip()
        rich_text = _pipe_tables_to_html(rich_text); rich_md = f"**{name}:**\n{rich_text}"
        prefix_reserve = len(name) + 30; rich_chunks = split_message(rich_md, TELEGRAM_RICH_MAX_LENGTH - prefix_reserve)
        rich_sent = True
        for i, chunk in enumerate(rich_chunks):
            if i > 0: chunk = f"**{name}:** _(continued)_\n{chunk}"
            result = transport.send_rich_text( chat_id, chunk, reply_to=prev_msg_id if prev_msg_id else None )
            if result and result.get("ok"):
                _rr = result.get("result", {}); prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
                if len(rich_chunks) > 1:
                    _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} part {i+1}/{len(rich_chunks)} -> Telegram OK")
                else: _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} -> Telegram OK")
            else:
                error_code = (result or {}).get("error_code", 0); desc = (result or {}).get("description", "")
                _log(_LOG_ERROR, "bridge", f"{log_prefix} sendRichMessage failed ({error_code}: {desc}), falling back to HTML")
                rich_sent = False; rich_failed_at = i
                break
            if i < len(rich_chunks) - 1: _clock.sleep(DELAY_BRIEF)
    if not rich_sent and rich_failed_at > 0:
        _send_as_html(name, '\n'.join(rich_chunks[rich_failed_at:]), chat_id, log_prefix, prev_msg_id)
        rich_sent = True
    if not rich_sent: _send_as_html(name, clean_text, chat_id, log_prefix)
def _send_as_html(name: str, text: str, chat_id: int, log_prefix: str,
                  prev_msg_id: int | None = None) -> int | None:
    html_text = markdown_to_telegram_html(text); prefix_reserve = len(name) + 30
    chunks = split_message(html_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)
    for i, part in enumerate(formatted_parts):
        result = transport.send_text( chat_id, part, parse_mode="HTML", reply_to=prev_msg_id if prev_msg_id else None )
        if result and result.get("ok"):
            _rr = result.get("result", {}); prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
            if len(formatted_parts) > 1:
                _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} part {i+1}/{len(formatted_parts)} -> Telegram OK")
            else: _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} -> Telegram OK")
        else:
            desc = str((result or {}).get("description", ""))
            _log(_LOG_WARN, "bridge", f"{log_prefix} HTML send failed ({desc}), retrying as plain text")
            plain_text = re.sub(r'<[^>]+>', '', part)
            plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            result = transport.send_text( chat_id, plain_text, reply_to=prev_msg_id if prev_msg_id else None )
            if result and result.get("ok"):
                _rr = result.get("result", {}); prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
        if i < len(formatted_parts) - 1: _clock.sleep(DELAY_BRIEF)
    return prev_msg_id
def _send_response_media(name: str, images: list[tuple[str | None, str]], files: list[tuple[str | None, str]], chat_id: int) -> None:
    for img_path, img_caption in images:
        if img_path is None:
            transport.send_text(chat_id, f"{name}: {img_caption}")
            continue
        full_caption = f"{name}: {img_caption}" if img_caption else f"{name}:"
        if Path(img_path).suffix.lower() in (".gif", ".mp4"): sent = send_animation(chat_id, img_path, full_caption)
        else: sent = send_photo(chat_id, img_path, full_caption)
        if sent: _log(_LOG_INFO, "telegram", f"Image sent: {name} -> {img_path}")
        else: transport.send_text(chat_id, f"{name}: [Image failed: {img_path}]")
    for file_path, file_caption in files:
        if file_path is None:
            transport.send_text(chat_id, f"{name}: {file_caption}")
            continue
        full_caption = f"{name}: {file_caption}" if file_caption else f"{name}:"; ext = Path(file_path).suffix.lower()
        if ext in VIDEO_EXTENSIONS: sent = send_video(chat_id, file_path, full_caption)
        elif ext in AUDIO_EXTENSIONS: sent = send_audio(chat_id, file_path, full_caption)
        elif ext in VOICE_EXTENSIONS: sent = send_voice(chat_id, file_path, full_caption)
        elif ext in STICKER_EXTENSIONS: sent = send_sticker(chat_id, file_path)
        else: sent = send_document(chat_id, file_path, full_caption)
        if sent: _log(_LOG_INFO, "telegram", f"File sent: {name} -> {file_path}")
        else: transport.send_text(chat_id, f"{name}: [File failed: {file_path}]")
def send_response_to_telegram(name: str, text: str, chat_id: int, log_prefix: str = "Response") -> None:
    clean_text, images, files = _parse_response_media(name, text)
    if clean_text and len(clean_text.strip()) <= 5:
        _log(_LOG_DEBUG, "telegram", f"{log_prefix} short msg: {name}, text={repr(clean_text)}, "
             f"images={len(images)}, files={len(files)}")
    if clean_text: _send_text_via_telegram(name, clean_text, chat_id, log_prefix)
    _send_response_media(name, images, files, chat_id)
def _beast_serve_deploy(html_path: str, slug: str) -> str | None:
    try:
        r = _subprocess_runner.run(
            ["beast", "serve", "deploy", html_path, "--slug", slug, "--output-json"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0:
            data = cast(dict[str, object], json.loads(r.stdout)); url = str(data.get("url", ""))
            if url and "localhost" in url:
                host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "157.180.48.254"
                url = url.replace("localhost", str(host))
            return url or None
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_WARN, "bridge", f"beast serve deploy failed for {slug}: {e}")
    return None
def create_session(name: str, backend: str = DEFAULT_BACKEND, chat_id: ChatId | None = None) -> tuple[bool, str | None]:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.hire(name, backend, chat_id=chat_id)
def kill_session(name: str) -> tuple[bool, str | None]:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.end(name)
def restart_claude(name: str, mode: str = "relaunch") -> tuple[bool, str | None]:
    _sync_worker_manager()
    assert worker_manager is not None
    return worker_manager.restart(name, mode=mode)
def switch_session(name: str) -> tuple[bool, str | None]:
    registered = get_registered_sessions()
    if name not in registered: return False, f"Worker '{name}' not found"
    state.active = name
    save_last_active(name)
    return True, None
def _notify_admin(msg: str) -> None:
    try:
        if admin_chat_id: send_telegram_message(admin_chat_id, msg)
    except (urllib.error.URLError, OSError, TimeoutError): pass
def send_typing_loop(chat_id: int | str, session_name: str) -> None:
    while is_pending(session_name):
        transport.send_chat_action(chat_id, "typing")
        _clock.sleep(DELAY_CLAUDE_LOAD)
def get_all_chat_ids() -> list[ChatId]:
    chat_ids: set[ChatId] = set()
    if SESSIONS_DIR.exists():
        for session_dir in SESSIONS_DIR.iterdir():
            if session_dir.is_dir():
                chat_id_file = session_dir / "chat_id"
                if chat_id_file.exists():
                    try:
                        raw = chat_id_file.read_text().strip()
                        if raw: chat_ids.add(int(raw))
                    except (OSError, ValueError) as exc: _log(_LOG_DEBUG, "parse:get_all_chat_ids", f"{type(exc).__name__}: {exc}")
    if admin_chat_id: chat_ids.add(admin_chat_id)
    return list(chat_ids)
def send_shutdown_message() -> None:
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        _log(_LOG_WARN, "notify", "No chat_ids to notify")
        return
    _log(_LOG_INFO, "notify", f"Sending shutdown to {len(chat_ids)} chat(s)...")
    for chat_id in chat_ids: transport.send_text(chat_id, "Going offline briefly. Your team stays the same.")
    _log(_LOG_INFO, "notify", "Shutdown notifications sent")
class _LegacyTransportAdapter(MessageTransport):
    def __init__(self, legacy: Any) -> None: self._legacy = legacy
    @property
    def name(self) -> str: return "legacy-adapter"
    def send_text(self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None) -> TelegramApiResponse:
        result = self._legacy.send_message(chat_id, text); return result if result else {"ok": True, "result": {"message_id": 1}}
    def send_photo(self, chat_id: ChatId, photo_path: str | Path, caption: str | None = None) -> bool: return False
    def send_document(self, chat_id: ChatId, doc_path: str | Path, caption: str | None = None) -> bool: return False
    def send_animation(self, chat_id: ChatId, animation_path: str | Path, caption: str | None = None) -> bool: return False
    def send_video(self, chat_id: ChatId, video_path: str | Path, caption: str | None = None) -> bool: return False
    def send_audio(self, chat_id: ChatId, audio_path: str | Path, caption: str | None = None) -> bool: return False
    def send_voice(self, chat_id: ChatId, voice_path: str | Path, caption: str | None = None) -> bool: return False
    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool: return False
    def send_chat_action(self, chat_id: ChatId, action: str) -> None: pass
    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None:
        if hasattr(self._legacy, 'set_reaction'): self._legacy.set_reaction(chat_id, message_id, reaction)
    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None) -> TelegramApiResponse:
        return {"ok": True, "result": {"message_id": message_id}}
    def setup_commands(self, commands: list[dict[str, str]]) -> None: pass
    def download_file(self, file_id: str, session_name: str) -> str | None: return None
CommandFn = Callable[[str, ChatId, MessageId], bool]
class CommandRouter:
    def cmd_teleport(self, arg: str, chat_id: ChatId, check_only: bool = False) -> bool:
        from teleport import cmd_teleport; return cmd_teleport(self, arg, chat_id, check_only)
    def cmd_teleback(self, arg: str, chat_id: ChatId) -> bool:
        from teleport import cmd_teleback; return cmd_teleback(self, arg, chat_id)
    def _teleport_notify(self, chat_id: ChatId | None, text: str) -> None:
        from teleport import teleport_notify; teleport_notify(chat_id, text)
    def cmd_hire(self, name: str, chat_id: ChatId) -> bool:
        from command_handlers import cmd_hire; return cmd_hire(self, name, chat_id)
    def cmd_end(self, name: str, chat_id: ChatId) -> bool:
        from command_handlers import cmd_end; return cmd_end(self, name, chat_id)
    def cmd_restart(self, chat_id: ChatId, args: str = "") -> bool:
        from command_handlers import cmd_restart; return cmd_restart(self, chat_id, args)
    def _restart_remote_worker(self, name: str, backend_name: str, backend: Backend, tmux_name: str, host: str, mode: str) -> tuple[bool, str | None]:
        from teleport import restart_remote_worker; return restart_remote_worker(name, backend_name, backend, tmux_name, host, mode, self.workers)
    def _cmd_relay_list(self, chat_id: ChatId) -> bool:
        from relay import cmd_relay_list; return cmd_relay_list(self, chat_id)
    def _cmd_relay_add(self, parts: list[str], chat_id: ChatId) -> bool:
        from relay import cmd_relay_add; return cmd_relay_add(self, parts, chat_id)
    def _cmd_relay_remove(self, parts: list[str], chat_id: ChatId) -> bool:
        from relay import cmd_relay_remove; return cmd_relay_remove(self, parts, chat_id)
    def _cmd_relay_status(self, chat_id: ChatId) -> bool:
        from relay import cmd_relay_status; return cmd_relay_status(self, chat_id)
    def _cmd_relay_stop(self, parts: list[str], chat_id: ChatId) -> bool:
        from relay import cmd_relay_stop; return cmd_relay_stop(self, parts, chat_id)
    def cmd_relay(self, arg: str, chat_id: ChatId) -> bool:
        from relay import cmd_relay; return cmd_relay(self, arg, chat_id)
    def _extract_reply_media(self, reply_to: TelegramMessageDict, target_worker: str) -> str | None:
        file_id, media_label = _extract_file_id(reply_to)
        if reply_to.get("document") and media_label == "file": media_label = f"file: {reply_to['document'].get('file_name', 'unknown')}"
        elif reply_to.get("sticker"): media_label = f"sticker: {reply_to['sticker'].get('emoji', '')}"
        elif reply_to.get("voice"): media_label = "voice message"
        if not file_id: return None
        local_path = download_telegram_file(file_id, target_worker)
        if not local_path: return None
        if reply_to.get("voice"):
            transcript = transcribe_voice(local_path)
            if transcript: return transcript
        return f"Manager forwarded {media_label}: {local_path}"
    def _worker_from_reply(self, msg: TelegramMessageDict | None) -> str | None:
        reply_to = msg.get("reply_to_message") if msg else None
        if not reply_to: return None
        reply_text = _extract_msg_text(reply_to)
        if not reply_text: return None
        first_line = reply_text.split("\n", 1)[0]
        if first_line.endswith(":"):
            candidate = first_line[:-1].strip().lower(); registered = self.workers.get_registered_sessions()
            if candidate in registered: return candidate
        return None
    def _handle_media_group_flush(self, group_id: str) -> None:
        with media_groups.lock: group = media_groups.buffer.pop(group_id, None)
        if not group: return
        items = group["items"]; caption = group.get("caption", ""); first_msg = items[0] if items else {}
        chat_id = first_msg.get("chat", {}).get("id"); msg_id = first_msg.get("message_id"); target = None
        if caption:
            targets, _ = self.parse_at_mentions(caption)
            if targets: target = targets[0]
        if not target:
            reply_worker = self._worker_from_reply(first_msg)
            if reply_worker: target = reply_worker
        if not target: target = state.active
        all_paths = []
        for msg in items:
            file_id, media_type = _extract_file_id(msg)
            if file_id:
                local_path = download_telegram_file(file_id, target)
                if local_path: all_paths.append((local_path, media_type))
        if not all_paths: return
        path_lines = "\n".join(f"Manager sent {mt}: `{p}`" for p, mt in all_paths)
        if caption: full_text = f"{caption}\n\n{path_lines}"
        else: full_text = path_lines
        self._route_media_message(full_text, caption, chat_id, msg_id, msg=first_msg)
    def _resolve_media_target(self, caption: str, msg: TelegramMessageDict) -> str | None:
        if caption:
            targets, _ = self.parse_at_mentions(caption)
            if targets: return targets[0]
        reply_worker = self._worker_from_reply(msg)
        if reply_worker: return reply_worker
        return state.active
    def _route_media_message(self, media_text: str, caption: str, chat_id: ChatId | None, msg_id: int | None, msg: TelegramMessageDict | None=None) -> None:
        if caption:
            unknown_mentions = self.unknown_at_mentions(caption)
            if unknown_mentions:
                self.reply(chat_id, self.format_unknown_mentions_warning(unknown_mentions))
                return
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                statuses = [self._route_mention(name, media_text, chat_id, msg_id) for name in targets]
                warning = _format_offline_warning(statuses)
                if warning: self.reply(chat_id, warning)
                return
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            self.route_message(reply_worker, media_text, chat_id, msg_id, one_off=True)
            return
        self.route_to_active(media_text, chat_id, msg_id)
    def _reset_mention_streak(self) -> None:
        _last_mention.target = None; _last_mention.count = 0; _last_mention.ts = 0
    def _handle_mention_routing(self, targets: list[str], message: str,
                                text: str, chat_id: ChatId | None, msg_id: int | None,
                                reply_to: TelegramMessageDict | None,
                                reply_context: str,
                                reply_context_ts: int | None) -> None:
        if len(targets) == 1 and re.fullmatch(r'\s*@[a-zA-Z0-9-]+\s*', text):
            target = targets[0]; registered = self.workers.get_registered_sessions()
            if target in registered:
                state.active = target
                save_last_active(target)
            else: self.reply(chat_id, f"Can't focus guest {target}.")
            self._reset_mention_streak()
            return
        if reply_context: message = self.format_reply_context(message, reply_context, reply_context_ts)
        final_msg = message
        if reply_to:
            reply_media = self._extract_reply_media(reply_to, targets[0])
            if reply_media: final_msg = f"{message}\n\n{reply_media}" if message else reply_media
        statuses = [self._route_mention(name, final_msg, chat_id, msg_id) for name in targets]
        warning = _format_offline_warning(statuses)
        if warning: self.reply(chat_id, warning)
        registered = self.workers.get_registered_sessions(); now = _clock.time()
        if len(targets) == 1 and targets[0] in registered:
            target = targets[0]
            if _last_mention.target == target and now - _last_mention.ts <= 60: _last_mention.count += 1
            else: _last_mention.target = target; _last_mention.count = 1
            _last_mention.ts = now
            if _last_mention.count >= 2 and state.active != target:
                state.active = target
                save_last_active(target)
                self.reply(chat_id, f"Switched to {target} (you mentioned them twice).")
        else: self._reset_mention_streak()
    _mention_re = re.compile(r'(?<![a-zA-Z0-9._+\-])@([a-zA-Z0-9-]+)')
    def _known_names(self) -> set[str]:
        registered = self.workers.get_registered_sessions()
        with guest_store.lock: guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        return set(registered.keys()) | guest_names
    def parse_at_mentions(self, text: str) -> tuple[list[str], str]:
        if not text: return [], ""
        known = self._known_names(); found = []
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name in known and name not in found: found.append(name)
        return found, text
    def unknown_at_mentions(self, text: str) -> list[str]:
        if not text: return []
        known = self._known_names() | {"all"}; unknown = []
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name not in known and name not in unknown: unknown.append(name)
        return unknown
    def format_unknown_mentions_warning(self, unknown_mentions: list[str]) -> str:
        import difflib
        known = sorted(self._known_names()); parts = []
        for name in unknown_mentions:
            suggestions = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
            if suggestions: parts.append(f"@{name}. Did you mean {', '.join('@' + s for s in suggestions)}?")
            else: parts.append(f"@{name}.")
        return f"⚠️ Unknown: {' '.join(parts)}"
    def _route_mention(self, name: str, message: str, chat_id: ChatId | None, msg_id: int | None) -> MentionRouteResult | None:
        registered = self.workers.get_registered_sessions(); session = registered.get(name)
        if session:
            if not self.workers.is_online(name, session): return {"name": name, "status": "offline"}
            self.route_message(name, message, chat_id, msg_id, one_off=True)
            return {"name": name, "status": "sent"}
        with guest_store.lock:
            guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        if name in guest_names:
            msg_obj = {
                "id": f"gm_{secrets.token_hex(4)}",
                "from": "manager",
                "text": message,
                "ts": int(_clock.time()), }
            with guest_store.lock:
                inbox = guest_store.inboxes.get(name, [])
                guest_store.inboxes[name] = guest_inbox_append(inbox, cast("GuestInboxMessageDict", msg_obj))
            return {"name": name, "status": "sent"}
        return {"name": name, "status": "unknown"}
    def get_reply_context(self, reply_msg: TelegramMessageDict) -> tuple[str, int | None]:
        if not reply_msg: return "", None
        text = _extract_msg_text(reply_msg); ts = reply_msg.get("date")
        return text, ts
    def format_reply_context(self, reply_text: str, context_text: str, context_ts: int | None = None) -> str:
        reply_text = (reply_text or "").strip(); context_text = (context_text or "").strip()
        if context_text:
            ts_str = ""
            if context_ts: ts_str = f" at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(context_ts))}"
            return (
                "Manager reply:\n"
                f"{reply_text}\n\n"
                f"Context (your previous message{ts_str}):\n"
                f"{context_text}" )
        return f"Manager reply:\n{reply_text}"
    def __init__(self, transport: MessageTransport | None,
                 workers: "WorkerManager") -> None:
        if transport is not None and not isinstance(transport, MessageTransport): transport = _LegacyTransportAdapter(transport)
        self.transport: MessageTransport | None = transport
        self.workers: "WorkerManager" = workers
        self._restart_all_lock: threading.Lock = threading.Lock()
        self._restart_all_running: bool = False  # type: ignore[no-redef]
        self._restart_all_abort: threading.Event = threading.Event()
        self._restart_all_thread: threading.Thread | None = None  # type: ignore[no-redef]
        self._commands: dict[str, CommandFn] = {
            "/hire": lambda arg, cid, mid: self.cmd_hire(arg, cid),
            "/focus": lambda arg, cid, mid: self.cmd_focus(arg, cid),
            "/team": lambda arg, cid, mid: self.cmd_team(cid),
            "/end": lambda arg, cid, mid: self.cmd_end(arg, cid),
            "/restart": lambda arg, cid, mid: self.cmd_restart(cid, arg),
            "/status": lambda arg, cid, mid: self.cmd_status(cid),
            "/settings": lambda arg, cid, mid: self.cmd_status(cid),
            "/pilot": lambda arg, cid, mid: self.cmd_pilot(arg, cid),
            "/relay": lambda arg, cid, mid: self.cmd_relay(arg, cid),
            "/rewind": lambda arg, cid, mid: self.cmd_rewind(arg, cid),
            "/pr": lambda arg, cid, mid: self.cmd_pr_review(arg, cid),
            "/teleport": lambda arg, cid, mid: self.cmd_teleport(arg, cid),
            "/teleport-check": lambda arg, cid, mid: self.cmd_teleport(arg, cid, check_only=True),
            "/teleback": lambda arg, cid, mid: self.cmd_teleback(arg, cid), }
    def reply(self, chat_id: ChatId | None, text: str, outcome: str | None = None) -> None:
        if self.transport is not None and chat_id is not None: self.transport.send_text(chat_id, text)
    def send_startup_message(self, chat_id: ChatId | None) -> None:
        registered = self.workers.get_registered_sessions(); sessions = list(registered.keys()); active = state.active
        lines = ["I'm online and ready."]
        if sessions:
            lines.append(f"Team: {', '.join(sessions)}")
            if active: lines.append(f"Focused: {active}")
        else: lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")
        self.reply(chat_id, "\n".join(lines))
    def handle_message(self, update: TelegramUpdate) -> None:
        global admin_chat_id
        _log(_LOG_INFO, "handle_message", f"ENTER update_id={update.get('update_id')}")
        incoming = IncomingMessage.from_update(update); msg = incoming.raw_msg; text = incoming.text
        chat_id = incoming.chat_id; msg_id = incoming.msg_id
        _log(_LOG_INFO, "handle_message", f"chat_id={chat_id} admin={admin_chat_id} text={repr(text[:40])}")
        media_group_id = msg.get("media_group_id")
        has_media = (incoming.photo or incoming.document or incoming.animation
                     or incoming.video or incoming.audio or incoming.voice
                     or incoming.video_note or incoming.sticker)
        if media_group_id and has_media and chat_id:
            if not self._check_admin(chat_id): return
            self._buffer_media_group(media_group_id, msg, text)
            return
        if has_media and chat_id:
            if self._handle_single_media(incoming): return
        if text and chat_id: self._route_text_message(incoming)
    def _check_admin(self, chat_id: ChatId | None) -> bool:
        global admin_chat_id
        if chat_id is None: return False
        if admin_chat_id is None:
            admin_chat_id = chat_id  # type: ignore[assignment]
            return True
        return chat_id == admin_chat_id
    def _buffer_media_group(self, media_group_id: str, msg: TelegramMessageDict, text: str) -> None:
        with media_groups.lock:
            if media_group_id not in media_groups.buffer:
                media_groups.buffer[media_group_id] = { "items": [], "caption": "", "timer": None, }
            group = media_groups.buffer[media_group_id]
            group["items"].append(msg)
            if text: group["caption"] = text
            if group["timer"]: group["timer"].cancel()
            t = threading.Timer(_MEDIA_GROUP_WAIT, self._handle_media_group_flush, args=[media_group_id])
            t.name = f"media-flush-{media_group_id}"; t.daemon = True
            group["timer"] = t
            t.start()
    def _handle_single_media(self, incoming: 'IncomingMessage') -> bool:
        global admin_chat_id
        msg = incoming.raw_msg; text = incoming.text; chat_id = incoming.chat_id; msg_id = incoming.msg_id
        media_dict: dict[str, object] = {}
        for attr in ("animation", "photo", "document", "video", "audio", "voice", "video_note", "sticker"):
            val = getattr(incoming, attr, None)
            if val: media_dict[attr] = val
        if incoming.doc_is_image and incoming.document: media_dict["document"] = incoming.document
        file_id, media_label = _extract_file_id(media_dict)
        if not file_id: return False
        if not self._check_admin(chat_id): return True
        if not state.active and not self.parse_at_mentions(text)[0]:
            self.reply(chat_id, "No focused worker. Use /focus <name> or @worker in caption.")
            return True
        download_target = self._resolve_media_target(text, msg)
        local_path = download_telegram_file(file_id, download_target)
        if not local_path:
            self.reply(chat_id, f"Could not download {media_label}. Try again.")
            return True
        media_text = self._format_media_text(incoming, local_path, media_label)
        if media_text is None: return True
        if text: media_text = f"{text}\n\n{media_text}"
        self._route_media_message(media_text, text, chat_id, msg_id, msg=msg)
        return True
    def _format_media_text(self, incoming: 'IncomingMessage', local_path: str, media_label: str) -> str | None:
        text = incoming.text; chat_id = incoming.chat_id; msg_id = incoming.msg_id; msg = incoming.raw_msg
        if media_label in ("GIF", "image"): return f"Manager sent {media_label}: `{local_path}`"
        if media_label == "file" and incoming.document:
            d = incoming.document; return f"Manager sent file: {d.get('file_name', 'unknown')} ({format_file_size(d.get('file_size', 0))}, {d.get('mime_type', 'unknown')})\nPath: `{local_path}`"
        if incoming.audio:
            return f"Manager sent audio: {incoming.audio.get('title', incoming.audio.get('file_name', 'audio'))} ({incoming.audio.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.voice:
            transcript = transcribe_voice(local_path)
            if transcript:
                self.reply(chat_id, f"🎤 _{transcript}_")
                self._route_media_message(f"{text}\n\n{transcript}" if text else transcript, text or transcript, chat_id, msg_id, msg=msg)
                return None
            return f"Manager sent voice message: ({incoming.voice.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.video: return f"Manager sent video: {incoming.video.get('file_name', 'video')} ({incoming.video.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.video_note: return f"Manager sent video note: ({incoming.video_note.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.sticker: return f"Manager sent sticker: {incoming.sticker.get('emoji', '')}\nPath: {local_path}"
        return f"Manager sent media: {local_path}"
    def _route_text_message(self, incoming: 'IncomingMessage') -> None:
        global admin_chat_id
        text = incoming.text; chat_id = incoming.chat_id; msg_id = incoming.msg_id; msg = incoming.raw_msg
        if admin_chat_id is None:
            admin_chat_id = chat_id
            save_last_chat_id(chat_id)
            _log(_LOG_INFO, "admin", f"Admin registered: {chat_id}")
        if not state.startup_notified:
            state.startup_notified = True
            self.send_startup_message(chat_id)
        if chat_id != admin_chat_id:
            _log(_LOG_WARN, "bridge", f"Rejected non-admin: {chat_id}")
            return
        save_last_chat_id(chat_id)
        if text.startswith("/"):
            if self.handle_command(text, chat_id, msg_id):
                _last_mention.target = None; _last_mention.count = 0
                return
        if re.match(r'^\s*@all(?:\s|[:,]|$)', text, re.IGNORECASE):
            self.route_to_all(text, chat_id, msg_id)
            self._reset_mention_streak()
            return
        reply_context = ""; reply_context_ts = None; reply_to = msg.get("reply_to_message")
        if reply_to: reply_context, reply_context_ts = self.get_reply_context(reply_to)
        unknown_mentions = self.unknown_at_mentions(text)
        if unknown_mentions:
            self.reply(chat_id, self.format_unknown_mentions_warning(unknown_mentions))
            self._reset_mention_streak()
            return
        targets, message = self.parse_at_mentions(text)
        if targets:
            self._handle_mention_routing(targets, message, text, chat_id, msg_id,
                                         reply_to, reply_context, reply_context_ts)
            return
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            routed_text = text
            if reply_context: routed_text = self.format_reply_context(text, reply_context, reply_context_ts)
            self.route_message(reply_worker, routed_text, chat_id, msg_id, one_off=True)
            self._reset_mention_streak()
            return
        self._reset_mention_streak()
        routed_text = text
        if reply_context: routed_text = self.format_reply_context(text, reply_context, reply_context_ts)
        self.route_to_active(routed_text, chat_id, msg_id)
    def handle_command(self, text: str, chat_id: ChatId | None, msg_id: int | None) -> bool:
        parts = text.split(maxsplit=1); cmd = parts[0].lower()
        if "@" in cmd: cmd = cmd.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""; handler = self._commands.get(cmd)
        if handler: return handler(arg, chat_id or 0, msg_id or 0)
        if cmd in BLOCKED_COMMANDS:
            self.reply(chat_id, f"{cmd} is interactive and not supported here.", outcome="Needs decision")
            return True
        worker_name = cmd[1:]; registered = self.workers.get_registered_sessions()
        if worker_name in registered:
            prev_focus = state.active; state.active = worker_name
            save_last_active(worker_name)
            if not arg: return True
            if prev_focus != worker_name and self.transport and chat_id is not None:
                self.transport.send_text(chat_id, f"Now talking to {worker_name.capitalize()}.")
            self.route_message(worker_name, arg, chat_id, msg_id, one_off=False)
            return True
        return False
    def cmd_pilot(self, name: str, chat_id: ChatId) -> bool:
        from command_handlers import cmd_pilot; return cmd_pilot(self, name, chat_id)
    def cmd_rewind(self, name: str, chat_id: ChatId) -> bool:
        from command_handlers import cmd_rewind; return cmd_rewind(self, name, chat_id)
    def cmd_pr_review(self, arg: str, chat_id: ChatId) -> bool:
        from command_handlers import cmd_pr_review; return cmd_pr_review(self, arg, chat_id)
    def cmd_focus(self, name: str, chat_id: ChatId) -> bool:
        from command_handlers import cmd_focus; return cmd_focus(self, name, chat_id)
    def cmd_team(self, chat_id: ChatId) -> bool:
        from command_handlers import cmd_team; return cmd_team(self, chat_id)
    def cmd_status(self, chat_id: ChatId) -> bool:
        from command_handlers import cmd_status; return cmd_status(self, chat_id)
    def route_to_active(self, text: str, chat_id: ChatId | None, msg_id: int | None) -> None:
        registered = self.workers.get_registered_sessions()
        if not state.active:
            if registered:
                names = ", ".join(registered.keys())
                self.reply(chat_id, f"No one assigned. Your team: {names}\nWho should I talk to?")
                return
            else:
                self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
                return
        self.route_message(state.active, text, chat_id, msg_id, one_off=False)
    def route_to_all(self, text: str, chat_id: ChatId | None, msg_id: int | None) -> None:
        registered = self.workers.get_registered_sessions(); sessions = list(registered.keys())
        if not sessions:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return
        sent_to = []
        for name in sessions:
            session = registered[name]
            if self.workers.is_online(name, session):
                self.route_message(name, text, chat_id, msg_id, one_off=True)
                sent_to.append(name)
        if not sent_to: self.reply(chat_id, "No one's online to share with.")
    def route_message(self, session_name: str, text: str, chat_id: ChatId | None, msg_id: int | None, one_off: bool=False) -> None:
        registered = self.workers.get_registered_sessions(); session = registered.get(session_name)
        if not session:
            self.reply(chat_id, f"Can't find {session_name}. Check /team for who's available.")
            return
        if not self.workers.is_online(session_name, session):
            teleport_state_file = SESSIONS_DIR / session_name / "teleport_state"
            if teleport_state_file.exists():
                self.reply(chat_id, f"{session_name.capitalize()} is being teleported. Please wait.")
                return
            self.reply(chat_id, f"{session_name.capitalize()} is offline. Try /restart.")
            return
        backend_name = get_worker_backend(session_name, session); backend = get_backend(backend_name)
        shortcut = text.strip().lower()
        if backend.is_interactive and shortcut in ( "1", "2", "3", "4", "5", "6", "7", "8", "9", "skip", "cancel" ):
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
        if not backend.is_interactive and chat_id is not None and not try_set_pending(session_name, chat_id):
            self.reply(chat_id, f"{session_name.capitalize()} is still working on the previous request. Wait for a response or use /pause.")
            return
        if not text.startswith("Manager sent "): text = f"manager: {text}"
        _log(_LOG_INFO, "dispatch", f"chat_id={chat_id} -> {session_name}: {text[:50]}...")
        if backend.is_interactive and chat_id is not None: worker_set_pending(session_name, chat_id)
        if chat_id is not None: _task_pool.submit(send_typing_loop, chat_id, session_name)
        send_ok = self.workers.send(session_name, text, chat_id, session)
        if not send_ok:
            clear_pending(session_name)
            self.reply(
                chat_id,
                f"Could not send to {session_name.capitalize()}. Try /restart.",
                outcome="Needs decision" )
            return
        if msg_id and send_ok:
            host = get_worker_host(session_name)
            if not backend.is_interactive or tmux_prompt_empty(session.get("tmux", ""), host=host):
                if self.transport and chat_id is not None: self.transport.set_reaction(chat_id, msg_id, [{"type": "emoji", "emoji": "👀"}])
assert worker_manager is not None
command_router = CommandRouter(transport, worker_manager)
from transcript import (  # noqa: E402
    TranscriptSyncState, TranscriptEntry, TranscriptStatsDict, ToolResultDict,
    TranscriptSyncRegistry, _TEAM_MEMBERS, _MANAGER_AV,
    _run_transcript_query, _start_transcript_sync, _resolve_transcript_path,
    _parse_transcript_entries, _generate_member_avatar, _detect_message_author,
    _transcript_entry_to_html, _format_model_name, _transcript_stats,
    _render_transcript_loading, _transcript_html_head, _transcript_html_nav,
    _transcript_html_entries, _transcript_html_footer, _transcript_html_search_js,
    _err_page, _render_transcript_html,
)
class EndpointRouter:
    def __init__(self) -> None:
        self._exact: dict[str, dict[str, object]] = {"POST": {}, "GET": {}, "DELETE": {}}
        self._pats: dict[str, list[tuple[re.Pattern[str], object]]] = {"POST": [], "GET": [], "DELETE": []}
    def _add(self, method: str, path: str, handler: object) -> None: self._exact[method][path] = handler
    def _add_pat(self, method: str, pattern: str, handler: object) -> None: self._pats[method].append((re.compile(pattern), handler))
    def post(self, path: str, handler: PostRouteHandler) -> None: self._add("POST", path, handler)
    def post_pattern(self, pattern: str, handler: PostRouteHandler) -> None: self._add_pat("POST", pattern, handler)
    def get(self, path: str, handler: GetRouteHandler) -> None: self._add("GET", path, handler)
    def get_pattern(self, pattern: str, handler: GetRouteHandler) -> None: self._add_pat("GET", pattern, handler)
    def delete(self, path: str, handler: GetRouteHandler) -> None: self._add("DELETE", path, handler)
    def delete_pattern(self, pattern: str, handler: GetRouteHandler) -> None: self._add_pat("DELETE", pattern, handler)
    def _resolve(self, method: str, path: str) -> tuple[object | None, re.Match[str] | None]:
        h = self._exact[method].get(path)
        if h: return h, None
        for regex, ph in self._pats[method]:
            m = regex.match(path)
            if m: return ph, m
        return None, None
    def resolve_post(self, path: str) -> PostRouteResolution: return cast(PostRouteResolution, self._resolve("POST", path))
    def resolve_get(self, path: str) -> GetRouteResolution: return cast(GetRouteResolution, self._resolve("GET", path))
    def resolve_delete(self, path: str) -> GetRouteResolution: return cast(GetRouteResolution, self._resolve("DELETE", path))
_endpoint_router = EndpointRouter()
def _checkin_can_restart(name: str, tmux_name: str,
                         host: str | None, pane_cwd: str,
                         requested_cwd: str) -> tuple[bool, str]:
    last_restart = watchdog.recent_restarts.get(name, 0); elapsed = _clock.time() - last_restart
    if elapsed < RESTART_COOLDOWN:
        if watchdog.force_restart_pending_cwd.pop(name, False):
            _log(_LOG_WARN, "checkin", f"{name}: cooldown bypassed (post-force CWD repair)")
        else:
            _log(_LOG_WARN, "checkin", f"{name}: BLOCKED restart (cooldown {elapsed:.0f}s < {RESTART_COOLDOWN}s)")
            return False, (f"Checkin restart blocked: {name} was restarted {elapsed:.0f}s ago "
                           f"(cooldown {RESTART_COOLDOWN}s). CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")
    if is_claude_running(tmux_name, host=host):
        _log(_LOG_INFO, "checkin", f"{name}: BLOCKED restart (Claude already running in tmux)")
        return False, (f"Checkin restart skipped: {name} has Claude running. "
                       f"CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")
    with watchdog.restart_lock:
        inflight_ts = watchdog.restart_in_progress.get(name)
        if inflight_ts and _clock.time() - inflight_ts < 120:
            _log(_LOG_INFO, "checkin", f"{name}: BLOCKED restart (in-flight since {_clock.time() - inflight_ts:.0f}s ago)")
            return False, f"Checkin restart blocked: {name} restart already in progress ({_clock.time() - inflight_ts:.0f}s)."
        watchdog.restart_in_progress[name] = _clock.time()
    return True, ""
def _checkin_do_restart(name: str, backend_name: str,
                        tmux_name: str, host: str | None,
                        requested_cwd: str) -> tuple[bool, str]:
    notify_chat_id = get_manager_chat_id(name)
    try:
        if notify_chat_id is not None:
            send_telegram_message(
                notify_chat_id,
                f"{name} is restarting in a new directory. "
                "Messages during restart may be lost.", )
        if host:
            restart_backend = get_backend(backend_name)
            ok, err = command_router._restart_remote_worker(
                name, backend_name, restart_backend, tmux_name, host, "relaunch")
        else: ok, err = worker_manager.restart(name, mode="relaunch")
        watchdog.recent_restarts[name] = _clock.time()
        _log(_LOG_INFO, "checkin", f"{name}: restart result ok={ok}, err={err}")
        if not ok:
            if notify_chat_id is not None:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} could not restart. "
                    f"Run /restart {name} before sending new messages.", )
            return False, err or "restart failed"
        if notify_chat_id is not None:
            if _wait_for_restart_ready(tmux_name, backend_name, host=host):
                send_telegram_message(notify_chat_id, f"{name} is ready. Safe to send messages now.")
            else:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} restarted but is not ready yet. "
                    f"Hold messages for now. If this continues, run /restart {name}.", )
        return True, ""
    finally:
        with watchdog.restart_lock: watchdog.restart_in_progress.pop(name, None)
class Handler(BaseHTTPRequestHandler):
    def _parse_body(self, body: bytes) -> dict[str, object] | None:
        try: return cast(dict[str, object], json.loads(body)) if body else {}
        except (json.JSONDecodeError, ValueError): self._send_error_json(400, "invalid JSON"); return None
    def _guest_auth(self, parsed: ParseResult | None = None) -> "GuestSessionDict | None":
        from guest_handlers import _guest_auth; return _guest_auth(self, parsed)
    def _channel_guest_auth(self, query_params: dict[str, list[str]]) -> str | None:
        from guest_handlers import _channel_guest_auth; return _channel_guest_auth(self, query_params)
    def handle_guest_register(self, body: bytes = b"") -> None:
        from guest_handlers import handle_guest_register; handle_guest_register(self, body)
    def handle_guest_send(self, body: bytes = b"") -> None:
        from guest_handlers import handle_guest_send; handle_guest_send(self, body)
    def handle_guest_reply(self, body: bytes = b"") -> None:
        from guest_handlers import handle_guest_reply; handle_guest_reply(self, body)
    def handle_guest_inbox(self, parsed: ParseResult) -> None:
        from guest_handlers import handle_guest_inbox; handle_guest_inbox(self, parsed)
    def handle_guest_status(self, parsed: ParseResult) -> None:
        from guest_handlers import handle_guest_status; handle_guest_status(self, parsed)
    def handle_guests_list(self) -> None:
        from guest_handlers import handle_guests_list; handle_guests_list(self)
    def handle_guest_disconnect(self, parsed: ParseResult) -> None:
        from guest_handlers import handle_guest_disconnect; handle_guest_disconnect(self, parsed)
    def handle_channel_create(self, body: bytes = b"") -> None:
        from guest_handlers import handle_channel_create; handle_channel_create(self, body)
    def handle_channel_members(self, channel_id: str, body: bytes = b"") -> None:
        from guest_handlers import handle_channel_members; handle_channel_members(self, channel_id, body)
    def handle_channel_send(self, channel_id: str, body: bytes = b"") -> None:
        from guest_handlers import handle_channel_send; handle_channel_send(self, channel_id, body)
    def handle_channel_messages(self, channel_id: str, parsed: ParseResult) -> None:
        from guest_handlers import handle_channel_messages; handle_channel_messages(self, channel_id, parsed)
    def handle_channels_list(self, parsed: ParseResult | None = None) -> None:
        from guest_handlers import handle_channels_list; handle_channels_list(self, parsed)
    def handle_channel_delete(self, channel_id: str) -> None:
        from guest_handlers import handle_channel_delete; handle_channel_delete(self, channel_id)
    def _relay_get_token(self) -> str | None:
        from relay import _relay_get_token; return _relay_get_token(self)
    def handle_relay_get(self, channel_id: str, action: str | None, parsed: ParseResult) -> None:
        from relay import handle_relay_get; handle_relay_get(self, channel_id, action, parsed)
    def _relay_auth_and_parse(self, channel_id: str, auth_fn: Callable[..., object], body: bytes) -> tuple[dict[str, object] | None, str, object]:
        from relay import _relay_auth_and_parse; return _relay_auth_and_parse(self, channel_id, auth_fn, body)
    def handle_relay_send(self, channel_id: str, body: bytes = b"") -> None:
        from relay import handle_relay_send; handle_relay_send(self, channel_id, body)
    def handle_relay_reply(self, channel_id: str, body: bytes = b"") -> None:
        from relay import handle_relay_reply; handle_relay_reply(self, channel_id, body)
    def handle_pr_file_content(self, parsed: ParseResult) -> None:
        from pr_handlers import handle_pr_file_content; handle_pr_file_content(self, parsed)
    def handle_pr_keepalive(self, parsed: ParseResult) -> None:
        from pr_handlers import handle_pr_keepalive; handle_pr_keepalive(self, parsed)
    def handle_pr_review_comments(self, body: bytes) -> None:
        from pr_handlers import handle_pr_review_comments; handle_pr_review_comments(self, body)
    def handle_pr_general_comment(self, body: bytes) -> None:
        from pr_handlers import handle_pr_general_comment; handle_pr_general_comment(self, body)
    def handle_pr_merge(self, body: bytes) -> None:
        from pr_handlers import handle_pr_merge; handle_pr_merge(self, body)
    def handle_pr_review_endpoint(self, parsed: ParseResult) -> None:
        from pr_handlers import handle_pr_review_endpoint; handle_pr_review_endpoint(self, parsed)
    def handle_pr_comment(self, body: bytes) -> None:
        from pr_handlers import handle_pr_comment; handle_pr_comment(self, body)
    def handle_transcript_endpoint(self, parsed: ParseResult) -> None:
        from transcript import handle_transcript_endpoint; handle_transcript_endpoint(self, parsed)
    def _send_status(self, code: int) -> None:
        self.send_response(code); self.end_headers()
    def _send_json(self, code: int, data: Mapping[str, object]) -> None:
        body = json.dumps(data).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def _send_error_json(self, code: int, message: str) -> None:
        self._send_json(code, {"ok": False, "error": message})
    def _send_html(self, body: bytes, status: int = 200) -> None:
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024
        if use_gzip:
            import gzip as _gzip; body = _gzip.compress(body, compresslevel=6)
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8")
        if use_gzip: self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _send_text(self, code: int, text: str) -> None:
        body = text.encode(); self.send_response(code)
        self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(body)
    def _pr_guard(self, body: bytes) -> PrActionBody | None:
        from pr_handlers import _pr_guard; return _pr_guard(self, body)
    def _pr_notify(self, text: str) -> None:
        from pr_handlers import _pr_notify; _pr_notify(text)
    def _pr_fan_out(self, pr_num: int, comment_body: str, prefix: str = "") -> None:
        from pr_handlers import _pr_fan_out; _pr_fan_out(pr_num, comment_body, prefix)
    def _send_unknown_endpoint(self, method: str, path: str) -> None:
        self._send_json(404, {
            "error": f"Unknown endpoint: {method} {path}",
            "available_endpoints": API_ENDPOINTS,
            "hint": "Messages from manager arrive as prompts. There is no polling endpoint.",
        })
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve_post(parsed.path)
        if handler:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            handler(self, body, match)
            return
        if parsed.path != "/":
            self._send_unknown_endpoint("POST", parsed.path)
            return
        if WEBHOOK_SECRET:
            header_token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if header_token != WEBHOOK_SECRET:
                _log(_LOG_WARN, "webhook", f"Webhook rejected: invalid secret token")
                self._send_text(403, "Forbidden")
                return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._send_text(200, "OK")
        try:
            update = cast(TelegramWebhookBody, json.loads(body))
            update_types = [k for k in update.keys() if k != "update_id"]; msg = update.get("message", {})
            text = msg.get("text", "") or msg.get("caption", "")
            _log(_LOG_INFO, "webhook", f"update_id={update.get('update_id')}, types={update_types}, text={repr(text[:50]) if text else '(none)'}")
            if "message" in update:
                def _safe_handle(upd: TelegramUpdate) -> None:
                    try:
                        command_router.handle_message(upd)
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                        _log(_LOG_ERROR, "webhook", f"handle_message CRASH: {exc}", exc=exc)
                _message_pool.submit(_safe_handle, update)
        except (json.JSONDecodeError, KeyError) as e: _log(_LOG_ERROR, "webhook", f"parse error: {e}", exc=e)
    def handle_notify(self, body: bytes = b"") -> None:
        from api_handlers import handle_notify; handle_notify(self, body)
    def handle_health_alert(self, body: bytes = b"") -> None:
        from api_handlers import handle_health_alert; handle_health_alert(self, body)
    def handle_forge_register(self, body: bytes = b"") -> None:
        from api_handlers import handle_forge_register; handle_forge_register(self, body)
    def handle_connectors_status(self) -> None:
        self._send_json(200, _get_connectors_status())
    def handle_connectors_restart(self, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None: return
        name = str(data.get("name", "")).strip().lower()
        if not name:
            self._send_error_json(400, "Missing 'name' (gmail or github)")
            return
        ok, msg = _restart_connector(name)
        self._send_json(200 if ok else 500, {"ok": ok, "name": name, "message": msg})
    def handle_send_endpoint(self, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None: return
        worker = _str_field(data, "worker").strip(); message = _str_field(data, "message") or _str_field(data, "text")
        sender = _str_field(data, "from", "system").strip() or "system"
        if not worker:
            self._send_error_json(400, "Missing worker")
            return
        if not isinstance(message, str) or not message.strip(): self._send_error_json(400, "Missing message"); return
        prefixed = f"{sender}: {message}"; delivered = send_to_worker(worker, prefixed)
        status = 200 if delivered else 404
        self._send_json(status, {"ok": delivered, "worker": worker, "delivered": delivered,
            "error": None if delivered else "Worker not found or not reachable"})
    def _validate_response_source(self, data: HookResponseBody, session_name: str) -> str:
        raw = cast(dict[str, object], data)
        messaging_fields = [key for key in ("worker", "to", "target", "message", "from") if key in raw]
        if messaging_fields:
            return f"POST /outputs is hook-only and cannot address workers. Unexpected messaging fields: {', '.join(messaging_fields)}. Use POST /messages with {{worker, from, message}}."
        source = _str_field(data, "source").strip()
        if not source:
            return "Missing source. POST /outputs is hook-only; worker output must identify its own source. To message another worker, use POST /messages."
        if source != session_name:
            return f"Source/session mismatch: source={source!r}, session={session_name!r}. POST /outputs only accepts a worker's own output. To message another worker, use POST /messages."
        return ""
    def handle_hook_response(self, body: bytes = b"") -> None:
        try:
            data = cast(HookResponseBody, json.loads(body)); session_name = _str_field(data, "session")
            text = _str_field(data, "text")
            if not session_name or not text: self._send_text(400, "Missing session or text"); return
            source_error = self._validate_response_source(data, session_name)
            if source_error:
                _log(_LOG_WARN, "hook", f"Rejected /outputs for {session_name}: {source_error}"); self._send_text(403, source_error); return
            chat_id_file = get_chat_id_file(session_name)
            if chat_id_file.exists(): chat_id = chat_id_file.read_text().strip()
            elif admin_chat_id is not None:
                chat_id = str(admin_chat_id); ensure_session_dir(session_name)
                _tmp_hk = chat_id_file.with_suffix('.tmp'); _tmp_hk.write_text(chat_id); _tmp_hk.chmod(0o600)
                os.replace(str(_tmp_hk), str(chat_id_file))
                _log(_LOG_INFO, "hook", f"Hook response: auto-created chat_id for session '{session_name}' from admin_chat_id")
            else: _log(_LOG_WARN, "hook", f"Hook response: no chat_id for session '{session_name}'"); self._send_text(404, "No chat_id for session"); return
            if len(text.strip()) <= 5:
                source_ip = self.client_address[0] if self.client_address else "unknown"
                hook_sid = _str_field(data, "session_id"); escape_flag = _bool_field(data, "escape")
                _log(_LOG_DEBUG, "hook", f"Hook response DEBUG: {session_name} -> chat {chat_id}, "
                     f"text={repr(text)}, len={len(text)}, "
                     f"source={_str_field(data, 'source', 'hook')}, "
                     f"session_id={hook_sid[:12] if hook_sid else 'none'}, "
                     f"escape={escape_flag}, ip={source_ip}")
            _log(_LOG_INFO, "hook", f"Hook response: {session_name} -> chat {chat_id} ({len(text)} chars)")
            hook_sid = _str_field(data, "session_id")
            if hook_sid: _cache_session_id(session_name, hook_sid)
            send_response_to_telegram(session_name, text, int(chat_id), log_prefix="Response")
            _check_learning_reminder(session_name)
            clear_pending(session_name)
            mark_hook_event(session_name)
            self._send_text(200, "OK")
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Hook response error: {e}")
            self._send_text(500, str(e))
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve_get(parsed.path)
        if handler:
            handler(self, parsed, match)
            return
        if parsed.path == "/":
            self._send_json(200, {"name": "claudecode-telegram bridge", "endpoints": API_ENDPOINTS,
                "note": "Messages from manager arrive as prompts. There is no polling endpoint."})
            return
        self._send_unknown_endpoint("GET", parsed.path)
    def do_HEAD(self) -> None:
        if urlparse(self.path).path == "/": self._send_status(200)
        else: self._send_status(404)
    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve_delete(parsed.path)
        if handler:
            handler(self, parsed, match)
            return
        self._send_unknown_endpoint("DELETE", parsed.path)
    def handle_workers_endpoint(self, parsed: ParseResult | None = None) -> None:
        try:
            caller_from = None
            if parsed is not None: caller_from = parse_qs(parsed.query).get("from", [None])[0]
            workers = get_workers(caller_from=caller_from)
            self._send_json(200, {"workers": workers})
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "worker", f"Workers endpoint error: {e}")
            self._send_text(500, str(e))
    def handle_machines_endpoint(self, parsed: ParseResult | None = None) -> None:
        try:
            caller_from = parse_qs(parsed.query).get("from", [None])[0] if parsed is not None else None
            self._send_json(200, get_machines(caller_from=caller_from))
        except (MachineConfigError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Machines endpoint error: {e}"); self._send_json(500, {"error": str(e)})
    def handle_checkin_endpoint(self, parsed: ParseResult) -> None:
        from api_handlers import handle_checkin_endpoint; handle_checkin_endpoint(self, parsed)
    def handle_health_workers_endpoint(self) -> None:
        from api_handlers import handle_health_workers_endpoint; handle_health_workers_endpoint(self)
    def handle_health_tunnel_endpoint(self) -> None:
        if tunnel_manager is not None: data = tunnel_manager.status()
        else: data = {"mode": "none", "state": "disabled", "tunnel_url": "", "polling_active": False}
        self._send_json(200, data)
def _mg(m: re.Match[str] | None, n: int = 1) -> str:
    return m.group(n) if m else ""
def _mg2(m: re.Match[str] | None) -> str:
    return m.group(2) if m else None
def _setup_endpoint_routes() -> None:
    r = _endpoint_router
    r.post("/outputs", lambda h, b, _m: h.handle_hook_response(b))
    r.post("/notifications", lambda h, b, _m: h.handle_notify(b))
    r.post("/messages", lambda h, b, _m: h.handle_send_endpoint(b))
    r.post("/tools/review/comments", lambda h, b, _m: h.handle_pr_review_comments(b))
    r.post("/tools/review/merges", lambda h, b, _m: h.handle_pr_merge(b))
    r.post("/workers", lambda h, b, _m: h.handle_forge_register(b))
    r.post("/guests", lambda h, b, _m: h.handle_guest_register(b))
    r.post("/channels", lambda h, b, _m: h.handle_channel_create(b))
    r.post("/alerts", lambda h, b, _m: h.handle_health_alert(b))
    r.post("/connectors/restarts", lambda h, b, _m: h.handle_connectors_restart(b))
    r.post_pattern(r'^/guests/send', lambda h, b, m: h.handle_guest_send(b))
    r.post_pattern(r'^/guests/reply', lambda h, b, m: h.handle_guest_reply(b))
    r.post_pattern( r'^/channels/([^/]+)/members$', lambda h, b, m: h.handle_channel_members(_mg(m), b) )
    r.post_pattern( r'^/channels/([^/]+)/send$', lambda h, b, m: h.handle_channel_send(_mg(m), b) )
    r.post_pattern( r'^/relay/([^/]+)/send$', lambda h, b, m: h.handle_relay_send(_mg(m), b) )
    r.post_pattern( r'^/relay/([^/]+)/reply$', lambda h, b, m: h.handle_relay_reply(_mg(m), b) )
    r.get("/guests", lambda h, p, _m: h.handle_guests_list())
    r.get("/workers", lambda h, p, _m: h.handle_workers_endpoint(p))
    r.get("/machines", lambda h, p, _m: h.handle_machines_endpoint(p))
    r.get("/checkin", lambda h, p, _m: h.handle_checkin_endpoint(p))
    r.get("/health/workers", lambda h, p, _m: h.handle_health_workers_endpoint())
    r.get("/health/tunnel", lambda h, p, _m: h.handle_health_tunnel_endpoint())
    r.get("/connectors", lambda h, p, _m: h.handle_connectors_status())
    r.get("/channels", lambda h, p, _m: h.handle_channels_list(p))
    r.get("/tools/review/files", lambda h, p, _m: h.handle_pr_file_content(p))
    r.get("/pr-keepalive", lambda h, p, _m: h.handle_pr_keepalive(p))
    r.get_pattern(r'^/guests/inbox', lambda h, p, m: h.handle_guest_inbox(p))
    r.get_pattern(r'^/guests/status', lambda h, p, m: h.handle_guest_status(p))
    r.get_pattern( r'^/relay/([^/]+)(?:/(.+))?$', lambda h, p, m: h.handle_relay_get(_mg(m), _mg2(m), p) )
    r.get_pattern( r'^/channels/([^/]+)/messages$', lambda h, p, m: h.handle_channel_messages(_mg(m), p) )
    r.get_pattern(r'^/transcript/', lambda h, p, m: h.handle_transcript_endpoint(p))
    r.get_pattern(r'^/tools/review/', lambda h, p, m: h.handle_pr_review_endpoint(p))
    r.delete("/guests", lambda h, p, _m: h.handle_guest_disconnect(p))
    r.delete_pattern( r'^/channels/([^/]+)$', lambda h, p, m: h.handle_channel_delete(_mg(m)) )
_setup_endpoint_routes()
def graceful_shutdown(signum: int, frame: types.FrameType | None) -> None:
    from datetime import datetime
    sig_name = signal.Signals(signum).name if signum else "unknown"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S"); ppid = os.getppid(); parent_info = f"ppid={ppid}"
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            cmdline = f.read().decode().replace("\x00", " ").strip(); parent_info = f"ppid={ppid} cmd={cmdline[:100]}"
    except (OSError, UnicodeDecodeError) as exc: _log(_LOG_DEBUG, "io:graceful_shutdown", f"{type(exc).__name__}: {exc}")
    print(f"\n[{timestamp}] Received {sig_name} ({parent_info}), shutting down...")
    connectors.stop_all()
    if tunnel_manager is not None:
        try: tunnel_manager.stop(); print("Tunnel manager stopped")
        except (RuntimeError, OSError) as exc: _log(_LOG_DEBUG, "shutdown:tunnel", f"{type(exc).__name__}: {exc}")
    watchdog.stop_event.set()
    for cancelable in [learning_reminders.idle_scan_timer]:
        if cancelable is not None:
            try: cancelable.cancel()
            except (RuntimeError, OSError) as exc: _log(_LOG_DEBUG, "shutdown:timer", f"{type(exc).__name__}: {exc}")
    with media_groups.lock:
        for _mg_entry in media_groups.buffer.values():
            t = _mg_entry.get("timer")
            if t is not None:
                try: t.cancel()
                except (RuntimeError, OSError): pass
        media_groups.buffer.clear()
    with processes.adapter_pids_lock: adapter_names = list(processes.adapter_pids.keys())
    for name in adapter_names:
        try: kill_adapter(name)
        except (OSError, ProcessLookupError): pass
    with processes.pipe_readers_lock: pipe_names = list(processes.pipe_readers.keys())
    for name in pipe_names:
        try: stop_pipe_reader(name)
        except OSError: pass
    _message_pool.shutdown(wait=False, cancel_futures=True); _task_pool.shutdown(wait=False, cancel_futures=True)
    send_shutdown_message(); sys.exit(0)
def _discover_and_configure_sessions() -> dict[str, TmuxSessionDict]:
    registered = scan_tmux_sessions(); registered = get_registered_sessions(registered)
    if registered:
        print(f"Discovered sessions: {list(registered.keys())}")
        for name, info in registered.items():
            tmux_name = info.get("tmux", f"{TMUX_PREFIX}{name}")
            if not tmux_name.startswith(TMUX_PREFIX):
                print(f"  SKIP {name}: tmux '{tmux_name}' doesn't match prefix '{TMUX_PREFIX}'")
                continue
            backend_name = get_worker_backend(name, info); backend_obj = get_backend(backend_name)
            if not backend_obj.is_interactive: ensure_worker_pipe(name)
            host = info.get("host") or get_worker_host(name)
            if tmux_exists(tmux_name, host=host): export_hook_env(tmux_name, backend_name, host=host)
    return registered
def _restore_bridge_state(registered: dict[str, TmuxSessionDict]) -> int | None:
    global admin_chat_id
    last_active = load_last_active()
    if last_active and last_active in registered: state.active = last_active; print(f"Restored last active worker: {last_active}")
    elif last_active: print(f"Last active worker '{last_active}' no longer exists")
    if os.path.isdir(TEAM_DIR):
        _startup_note = read_checkin_note()
        print(f"Team dir: {TEAM_DIR}" + (f"  Checkin note: {len(_startup_note)} chars" if _startup_note else ""))
    else: print(f"Team dir not found: {TEAM_DIR} (checkin note disabled)")
    last_chat_id = load_last_chat_id()
    if last_chat_id and admin_chat_id is None: admin_chat_id = last_chat_id; print(f"Restored admin from last_chat_id: {admin_chat_id}")
    _relay_load(); _channel_load(); _guest_load()
    return last_chat_id
def _log_startup_info(registered: dict[str, TmuxSessionDict]) -> None:
    update_bot_commands(); print(f"Multi-Session Bridge on {BRIDGE_BIND}:{PORT}")
    if BRIDGE_BIND == "0.0.0.0":
        print("  ⚠️  WARNING: Bound to 0.0.0.0 — exposed on ALL interfaces. Use BRIDGE_BIND=<tailscale-ip>")
    print(f"Hook: http://{BRIDGE_BIND}:{PORT}/outputs | Active: {state.active or 'none'} | Sessions: {list(registered.keys()) or 'none'}")
    print(f"Webhook: {'enabled' if WEBHOOK_SECRET else 'disabled'} | Admin: {admin_chat_id or 'auto-learn'} | Exec: direct")
def _send_startup_notification(last_chat_id: int, registered: dict[str, TmuxSessionDict]) -> None:
    state.startup_notified = True; sessions = list(registered.keys()); active = state.active
    lines = ["I'm online and ready."]
    if sessions:
        lines.append(f"Team: {', '.join(sessions)}")
        if active: lines.append(f"Focused: {active}")
    else: lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")
    result = transport.send_text(last_chat_id, "\n".join(lines))
    if result and result.get("ok"): print(f"Sent startup notification to chat {last_chat_id}")
    else: _log(_LOG_WARN, "bridge", f"Failed to send startup notification: {result}")
from connector_glue import (  # noqa: E402
    connector_on_message as _connector_on_message,
    connector_get_workers as _connector_get_workers,
    connector_on_alert as _connector_on_alert,
    start_connectors as _start_connectors,
    restart_connector as _restart_connector,
    get_connectors_status as _get_connectors_status,
)
def main() -> None:
    global admin_chat_id, tunnel_manager
    if TRANSPORT_MODE == "telegram" and not BOT_TOKEN:
        _log(_LOG_ERROR, "telegram", "Error: TELEGRAM_BOT_TOKEN not set")
        return
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SESSIONS_DIR.chmod(0o700)
    try:
        machines = get_machine_catalog(force_reload=True)
        print(f"Machine catalog: {list(machines.keys())} ({MACHINES_CONFIG_FILE})")
    except MachineConfigError as e:
        _log(_LOG_ERROR, "bridge", f"Error: {e}")
        sys.exit(1)
    registered = _discover_and_configure_sessions(); last_chat_id = _restore_bridge_state(registered)
    _log_startup_info(registered)
    if last_chat_id: _send_startup_notification(last_chat_id, registered)
    watchdog = threading.Thread(target=watchdog_loop, name="worker-watchdog", daemon=True)
    watchdog.start()
    _seed_learning_reminder_state(registered.keys())
    _schedule_idle_scan()
    print(f"Learning reminder idle scan: started (every 30 min, {len(learning_reminders.state)} workers tracked)")
    connectors.gmail, connectors.github = _start_connectors()
    server = ReuseAddrServer((BRIDGE_BIND, PORT), Handler); tunnel_config = _build_tunnel_config()
    if tunnel_config.mode != "none" and BOT_TOKEN:
        def _tunnel_on_update(update: dict[str, object]) -> None:
            if "message" in update:
                def _safe_handle(upd: dict[str, object]) -> None:
                    try:
                        command_router.handle_message(upd)
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
                        _log(_LOG_ERROR, "tunnel:poll", f"handle_message CRASH: {exc}", exc=exc)
                _message_pool.submit(_safe_handle, update)
        def _tunnel_on_notify(msg: str) -> None:
            if admin_chat_id:
                try: transport.send_text(admin_chat_id, msg)
                except Exception: pass
        tunnel_manager = _tunnel_mod.TunnelManager(config=tunnel_config, bot_token=BOT_TOKEN, port=PORT, node_dir=NODE_DIR,
            webhook_secret=WEBHOOK_SECRET, bind_host=BRIDGE_BIND, on_update=_tunnel_on_update, on_notify=_tunnel_on_notify)
        tunnel_manager.start()
        _log(_LOG_INFO, "tunnel", f"Tunnel manager started (mode={tunnel_config.mode})")
    try: server.serve_forever()
    except KeyboardInterrupt: graceful_shutdown(signal.SIGINT, None)
if __name__ == "__main__": main()
import types as _types_mod
class _BridgeModule(_types_mod.ModuleType):
    _sources: tuple[_types_mod.ModuleType, ...] = ()
    def __setattr__(self, name: str, value: object) -> None:
        if name == "_sources":
            super().__setattr__(name, value)
            return
        for mod in self._sources:
            if hasattr(mod, name): setattr(mod, name, value)
        super().__setattr__(name, value)
    def __getattr__(self, name: str) -> object:
        for mod in self._sources:
            try: return getattr(mod, name)
            except AttributeError: pass
        raise AttributeError(f"module 'bridge' has no attribute {name}")
import core as _core_mod
import telegram as _tg_mod
import claudecode as _cc_mod
import health as _health_mod
import relay as _relay_mod
# Re-export health functions used by bridge-internal LOAD_GLOBAL lookups
from health import (  # noqa: E402
    _clear_hook_failures, _extract_question_details, _get_claude_pid,
    _read_noninteractive_activity, _read_tmux_activity, _send_interactive_reply,
    compute_state, format_team_lines, _format_watchdog_status,
    _normalize_activity, _team_attention_summary,
    _format_watchdog_status_pure, _format_team_lines_pure,
    _check_adapter_log, _detect_poisoned, _extract_activity,
    watchdog_loop,
)
# Re-export relay/guest/channel for LOAD_GLOBAL lookups
from relay import (  # noqa: E402
    GuestSessionDict, GuestInboxMessageDict,
    ChannelMemberDict, ChannelMessageDict, ChannelDict,
    RelayMessageDict, RelayChannelDict,
    _save_state_json, _load_state_json,
    GuestStore, guest_store, GUEST_TTL, GUEST_INBOX_CAP,
    _guest_state_path, _guest_save, _guest_load,
    guest_create_token, guest_generate_name, guest_validate_name,
    guest_is_expired, guest_inbox_filter, guest_inbox_append,
    ChannelStore, channel_store, CHANNEL_TTL, CHANNEL_MSG_CAP,
    _channel_state_path, _channel_save, _channel_load,
    channel_create_id, _channel_member_entry, channel_new,
    channel_add_members, channel_remove_members, channel_append_message,
    channel_get_messages, channel_is_expired,
    RelayStore, relay_store, RELAY_PUBLIC_HOST,
    _relay_state_path, _relay_save, _relay_load,
    relay_channel_create, _relay_base_url, relay_guide_url, relay_guide_text,
    _relay_auth, relay_auth_guest, relay_auth_reply,
    _relay_append_msg, relay_guest_send, relay_worker_reply, relay_get_messages,
)
_this_module = sys.modules[__name__]
_this_module.__class__ = _BridgeModule
_this_module._sources = (_core_mod, _tg_mod, _cc_mod, _health_mod, _relay_mod)
