#!/usr/bin/env python3
import collections
from dataclasses import dataclass, field
import fcntl
import hashlib
import http.client
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
import types
import time
import re
import urllib.error
import urllib.request
import shlex
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, ParseResult
import uuid
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import IO, Callable, Iterator, Literal, NamedTuple, Protocol, TypedDict, cast, runtime_checkable
from core import *  # noqa: F401,F403
from core import (
    _str_field, _int_field, _dict_field, _bool_field,
    _log, _log_best_effort,
    _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _subprocess_runner, _clock, _urlopen,
    _RealSubprocessRunner, _RealClock,
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
    _build_cwd_change_notice,
    _normalize_activity,
    _team_attention_summary,
    _format_watchdog_status as _format_watchdog_status_pure,
    format_team_lines as _format_team_lines_pure, )
import telegram as _tunnel_mod
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
class TranscriptSyncState(TypedDict, total=False):
    status: str; progress: str; error: str | None; path: str | None; started: float; pct: int; remote_size: int
class TranscriptMessageUsage(TypedDict, total=False):
    input_tokens: int; output_tokens: int; cache_read_input_tokens: int; cache_creation_input_tokens: int
class TranscriptMessageContent(TypedDict, total=False):
    type: str; text: str; name: str; id: str; input: dict[str, str]; content: str | list[object]; is_error: bool
class TranscriptMessage(TypedDict, total=False):
    role: str; content: str | list[TranscriptMessageContent]; model: str; usage: TranscriptMessageUsage
class TranscriptEntry(TypedDict, total=False):
    type: str; message: TranscriptMessage; timestamp: str; version: str; gitBranch: str; _idx: int
class TranscriptStatsDict(TypedDict):
    n_user: int; n_tool: int; n_edit: int; lines_add: int; lines_del: int; lines_mod: int; n_files: int; model: str; version: str
    git_branch: str; first_ts: str; last_ts: str; input_tokens: int; output_tokens: int; duration: str
class ToolResultDict(TypedDict, total=False):
    content: str; is_error: bool
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
def _ensure_bare_repo(project_name: str) -> str:
    bare_path = os.path.join(GIT_SERVER_DIR, f"{project_name}.git")
    if not os.path.isdir(bare_path):
        os.makedirs(GIT_SERVER_DIR, exist_ok=True)
        _subprocess_runner.run(
            ["git", "init", "--bare", bare_path],
            capture_output=True, text=True, check=True, timeout=TIMEOUT_REMOTE_CMD)
    return bare_path
def _read_noninteractive_activity(worker_name: str) -> str:
    with processes.adapter_pids_lock: entry = processes.adapter_pids.get(worker_name)
    if entry:
        proc, _ = entry
        if proc.poll() is None: return "adapter running"
    host = get_worker_host(worker_name); path = _find_codex_transcript(worker_name, host=host)
    if path:
        try:
            if host:
                r = _remote_run(["stat", "-c", "%Y", path], host=host,
                                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r.returncode == 0: mtime = float(r.stdout.strip()); age = int(_clock.time() - mtime)
                else: age = -1
            else: mtime = os.path.getmtime(path); age = int(_clock.time() - mtime)
            if age >= 0:
                if age < 60: return f"idle (last response {age}s ago)"
                elif age < 3600: return f"idle (last response {age // 60}m ago)"
                else: return f"idle (last response {age // 3600}h ago)"
        except (subprocess.SubprocessError, OSError, ValueError) as exc: _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    return "idle"
def _check_hook_failure_signal(name: str) -> str | None:
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"; host = get_worker_host(name)
    if host:
        try:
            r = _remote_run(["cat", signal_path], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            raw = r.stdout.strip()
        except (subprocess.SubprocessError, OSError): return None
    else:
        signal_file = Path(signal_path)
        if not signal_file.exists(): return None
        try: raw = signal_file.read_text().strip()
        except OSError: return None
    if not raw: return None
    lines = raw.splitlines(); cutoff = int(_clock.time()) - HOOK_FAILURE_WINDOW; recent = 0
    for line in lines:
        parts = line.split(None, 1)
        if not parts: continue
        try: ts = int(parts[0])
        except ValueError: continue
        if ts >= cutoff: recent += 1
    if recent >= HOOK_FAILURE_THRESHOLD: return f"hook failure signal: {recent} tool failures in {HOOK_FAILURE_WINDOW}s"
    return None
def _clear_hook_failures(name: str) -> None:
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"; host = get_worker_host(name)
    if host:
        try:
            _remote_run(["rm", "-f", signal_path], host=host, capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        except (subprocess.SubprocessError, OSError) as exc: _log(_LOG_DEBUG, "probe:_clear_hook_failures", f"{type(exc).__name__}: {exc}")
    else:
        try: Path(signal_path).unlink(missing_ok=True)
        except OSError as exc: _log(_LOG_DEBUG, "io:_clear_hook_failures", f"{type(exc).__name__}: {exc}")
def _detect_poisoned(name: str, tmux_name: str) -> str | None:
    hook_reason = _check_hook_failure_signal(name)
    if hook_reason: return hook_reason
    backend_name = get_worker_backend(name); backend = get_backend(backend_name); host = get_worker_host(name); text_parts = []
    if backend.is_interactive: text_parts.append(_capture_pane_text(tmux_name, host=host))
    else: text_parts.append(_check_adapter_log(name))
    combined = "\n".join([part for part in text_parts if part])
    if not combined: return None
    for pattern in POISON_PATTERNS:
        if len(pattern.findall(combined)) >= 3: return pattern.pattern
    return None
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
def _activity_from_spinner(stripped: list[str]) -> str | None:
    _ACTIVE_SPINNER_CHARS = {"·", "*", "✢", "✦", "✧", "✹", "✵", "∙", "•", "✻"}
    for raw in reversed(stripped):
        first = raw[0] if raw else ""
        if first == "✻" and "…" not in raw and "..." not in raw: continue
        if first not in _ACTIVE_SPINNER_CHARS: continue
        match = re.match(r'^.\s+(.+?)(?:…|\.{3})\s*\(([^()]+)\)\s*$', raw)
        if match: verb = match.group(1).strip(); dur = match.group(2).split('·')[0].strip(); return f"{verb} ({dur})"
        verb_match = re.match(r'^.\s+(.+?)(?:…|\.{3})?\s*$', raw)
        if verb_match:
            verb = verb_match.group(1).strip(); dur_match = re.search(r'(\d+m?\s*\d*\.?\d*s)', raw)
            return f"{verb} ({dur_match.group(1).strip()})" if dur_match else verb
    return None
def _activity_from_tool(stripped: list[str]) -> str | None:
    last_running_tool = None
    for i, raw in enumerate(stripped):
        tool_match = re.match(r'^●\s*([A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*)\(', raw)
        if not tool_match: continue
        tool = tool_match.group(1)
        for j in range(i + 1, min(i + 6, len(stripped))):
            line = stripped[j]
            if not line: continue
            if line.startswith("⎿"):
                if "Running" in line and "background" not in line: last_running_tool = tool
                break
    if last_running_tool:
        if last_running_tool.startswith("mcp__"):
            parts = last_running_tool.split("__")
            last_running_tool = ".".join(parts[1:]) if len(parts) > 1 else last_running_tool
        return f"Running {last_running_tool}"
    return None
def _activity_from_rate_limit(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        lower = raw.lower()
        if "rate limit" in lower: return "Rate limited — waiting to retry"
        if "connection error" in lower and "retrying" in lower: return "Connection error — retrying"
        if lower.startswith("retrying") or "retrying in" in lower: return "Retrying API request"
    return None
def _activity_from_interactive(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                for question_line in stripped:
                    if "☐" in question_line:
                        question = question_line.replace("☐", "").strip()
                        if question: return f"Waiting for input: {question}"
                return "Waiting for user input"
    for raw in stripped:
        for pattern in _INTERACTIVE_CONTENT:
            if pattern in raw:
                if "plan" in raw.lower() and ("proceed" in raw.lower() or "execute" in raw.lower()): return "Waiting for plan approval"
                if "plan mode" in raw.lower(): return "Waiting for plan mode decision"
                if raw.startswith("Allow "): return "Waiting for tool permission"
                return "Waiting for user input"
    return None
def _activity_from_prompt(stripped: list[str]) -> str | None:
    last_prompt_idx = None; last_plan_bar_idx = None
    for i, raw in enumerate(stripped):
        if raw.startswith("❯"): last_prompt_idx = i
        if raw.startswith("⏸"): last_plan_bar_idx = i
    if last_plan_bar_idx is not None:
        if last_prompt_idx is None or last_prompt_idx < last_plan_bar_idx: return "In plan mode"
    if last_prompt_idx is not None: return "Ready"
    return None
_REVERSE_SCAN_PATTERNS: list[tuple[str, str]] = [
    ("Save and close editor to continue", "Waiting for external editor"),
    ("Running SessionStart", "Running SessionStart hooks"),
    ("Running PreCompact", "Running PreCompact hooks"),
    ("Do you want to proceed?", "Waiting for plan approval"),
    ("Would you like to proceed?", "Waiting for plan approval"),
    ("Exit plan mode?", "In plan mode"), ("Entering plan mode", "In plan mode"),
    ("Waiting for team lead", "Waiting for team lead approval"), ]
def _activity_from_reverse_scan(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        for needle, result in _REVERSE_SCAN_PATTERNS:
            if needle in raw: return result
    return None
def _activity_from_tasks(stripped: list[str]) -> str | None:
    done = 0; total = 0
    for raw in stripped:
        line = raw.lstrip()
        if line.startswith("✔") or line.startswith("✅"): done += 1; total += 1
        elif line.startswith("◻"): total += 1
    if total >= 2: return f"Tasks ({done}/{total} done)"
    return None
def _is_output_block_end(text: str) -> bool:
    trimmed = text.lstrip()
    if trimmed.startswith("Context left until auto-compact:"): return True
    return trimmed.startswith(("●", "·", "*", "✻", "─", "❯", "⏵", "⏸"))
def _activity_from_output_block(stripped: list[str]) -> str | None:
    for i in range(len(stripped) - 1, -1, -1):
        raw = stripped[i]
        if not raw.startswith("●"): continue
        if re.match(r'^●\s*[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*\(', raw): continue
        parts: list[str] = []
        head = re.sub(r'^●\s*', '', raw).strip()
        if head and not head.startswith("⎿") and not head.startswith("(ctrl+"): parts.append(head)
        j = i + 1
        while j < len(stripped):
            nxt = stripped[j]
            if _is_output_block_end(nxt): break
            text = nxt.strip()
            if text and not text.startswith("⎿") and not text.startswith("(ctrl+"): parts.append(text)
            j += 1
        if parts:
            msg = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
            if len(msg) > 120: msg = msg[:117].rstrip() + "..."
            return msg
    return None
def _activity_from_error(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        if re.match(r'^(FAIL|ERROR|Error|Traceback|Fail)\b', raw, re.IGNORECASE):
            lower = raw.lower()
            if lower.startswith("error"):
                tail = raw[len("Error"):].lstrip(": ").strip()
                return f"Error: {tail}" if tail else "Error"
            return f"Error: {raw[:60]}"
    return None
def _extract_activity(lines: list[str]) -> str:
    if not lines: return "Active"
    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped: return "Idle"
    _CHECKS: list[Callable[[list[str]], str | None]] = [
        _activity_from_spinner, _activity_from_tool, _activity_from_rate_limit,
        _activity_from_interactive, _activity_from_prompt, _activity_from_reverse_scan,
        _activity_from_tasks, _activity_from_output_block, _activity_from_error]
    for check in _CHECKS:
        result: str | None = check(stripped)
        if result is not None: return result
    return "Active"
def _extract_context_pct(lines: list[str]) -> str | None:
    for line in reversed(lines):
        m = re.search(r'Context left.*?(\d+)%', line)
        if m: return f"{m.group(1)}%"
    return None
def _read_tmux_activity(tmux_name: str, host: str | None = None) -> TmuxActivityResult:
    try:
        if host:
            proc = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
        else:
            proc = _subprocess_runner.run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK )
        if proc.returncode != 0: return TmuxActivityResult("Unknown", None, None)
        lines = proc.stdout.split("\n"); tail = lines[-40:]
        return TmuxActivityResult(_extract_activity(tail), _extract_context_pct(tail), tail)
    except (subprocess.SubprocessError, OSError): return TmuxActivityResult("Unknown", None, None)
def _extract_question_details(lines: list[str]) -> QuestionDetails | None:
    if not lines: return None
    stripped = [l.strip() for l in lines if l.strip()]
    if not stripped: return None
    tail = stripped[-5:]
    if any(line == "❯" for line in tail): return None
    has_interactive = False
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                has_interactive = True
                break
        if has_interactive: break
    if not has_interactive:
        for raw in stripped:
            for pattern in _INTERACTIVE_CONTENT:
                if pattern in raw:
                    has_interactive = True
                    break
            if has_interactive: break
    if not has_interactive: return None
    header = ""
    for raw in stripped:
        if "☐" in raw:
            header = raw.replace("☐", "").strip()
            break
    options = []; selected_num = 0; opt_re = re.compile(r'^(❯)?\s*(\d+)\.\s+(.+)')
    for raw in stripped:
        m = opt_re.match(raw)
        if m:
            is_selected = m.group(1) == "❯"; num = int(m.group(2)); label = m.group(3).strip()
            options.append({"num": num, "label": label, "selected": is_selected})
            if is_selected: selected_num = num
    if not options: return None
    return cast(QuestionDetails, { "header": header, "options": options, "selected_num": selected_num,
    })
def _send_interactive_reply(tmux_name: str, reply: str, details: QuestionDetails, host: str | None = None) -> bool:
    reply = reply.strip().lower()
    if reply in ("skip", "cancel", "esc"):
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)
        return True
    if reply.isdigit():
        target_num = int(reply); option_nums = [o["num"] for o in details["options"]]
        if target_num not in option_nums: return False
        target_idx = option_nums.index(target_num); current_idx = 0
        for i, o in enumerate(details["options"]):
            if o["selected"]:
                current_idx = i
                break
        diff = target_idx - current_idx; keys = []
        if diff > 0: keys = ["Down"] * diff
        elif diff < 0: keys = ["Up"] * abs(diff)
        keys.append("Enter")
        for key in keys:
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key], host=host, timeout=TIMEOUT_TMUX_SEND)
            _clock.sleep(DELAY_BRIEF)
        return True
    return False
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
def _bare_repo_url(bare_repo_path: str, target_host: str | None = None) -> str:
    if target_host: return f"claude@100.125.36.102:{bare_repo_path}"
    return bare_repo_path
def _git_push_state(source_cwd: str, worker_name: str, bare_repo: str,
                    host: str | None = None) -> GitPushStateResult | None:
    try:
        r = _remote_run(["git", "-C", source_cwd, "rev-parse", "HEAD"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"rev-parse HEAD failed: {r.stderr[:200]}")
            return None
        orig_sha = r.stdout.strip()
        r = _remote_run(["git", "-C", source_cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        orig_branch = r.stdout.strip() if r.returncode == 0 else "HEAD"
        r = _remote_run(["git", "-C", source_cwd, "diff", "--cached", "--name-only"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        staged_files = [f for f in r.stdout.strip().split("\n") if f] if r.returncode == 0 else []
        _remote_run(["git", "-C", source_cwd, "add", "-A"],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        try:
            r = _remote_run(["git", "-C", source_cwd, "stash", "create"],
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            stash_sha = r.stdout.strip() if r.returncode == 0 else ""
        finally:
            _remote_run(["git", "-C", source_cwd, "reset", "HEAD"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
            if staged_files:
                _remote_run(["git", "-C", source_cwd, "add", "--"] + staged_files,
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        push_sha = stash_sha if stash_sha else orig_sha; ref = f"refs/heads/teleport/{worker_name}"
        if host:
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force", f"claude@100.125.36.102:{bare_repo}", f"{push_sha}:{ref}"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_LARGE_TRANSFER)
        else:
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force", bare_repo, f"{push_sha}:{ref}"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_LARGE_TRANSFER)
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"push failed: {r.stderr[:200]}")
            return None
        return {"orig_sha": orig_sha, "orig_branch": orig_branch, "staged_files": staged_files, "stash_sha": stash_sha or None}
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"push state error: {e}")
        return None
def _git_pull_state(target_cwd: str, worker_name: str, bare_repo_url: str,
                    metadata: GitPushStateResult, host: str | None = None) -> bool:
    try:
        orig_sha = metadata["orig_sha"]; orig_branch = metadata["orig_branch"]
        staged_files = metadata.get("staged_files", []); stash_sha = metadata.get("stash_sha")
        ref = f"teleport/{worker_name}"; is_existing = False
        try:
            r = _remote_run(["git", "-C", target_cwd, "rev-parse", "--git-dir"],
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            is_existing = r.returncode == 0
        except (subprocess.SubprocessError, OSError) as exc: _log(_LOG_DEBUG, "probe:_git_pull_state", f"{type(exc).__name__}: {exc}")
        if not is_existing:
            r = _remote_run(
                ["git", "clone", "--no-checkout", bare_repo_url, target_cwd],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"clone failed: {r.stderr[:200]}")
                return False
            _remote_run(["git", "-C", target_cwd, "config", "user.email", "teleport@bridge"],
                        host=host, capture_output=True)
            _remote_run(["git", "-C", target_cwd, "config", "user.name", "teleport"], host=host, capture_output=True)
        else:
            _remote_run(["git", "-C", target_cwd, "remote", "remove", "vps"], host=host, capture_output=True)
            _remote_run(
                ["git", "-C", target_cwd, "remote", "add", "vps", bare_repo_url],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            r = _remote_run(
                ["git", "-C", target_cwd, "fetch", "vps", ref],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"fetch failed: {r.stderr[:200]}")
                return False
        if orig_branch and orig_branch != "HEAD":
            _remote_run(
                ["git", "-C", target_cwd, "checkout", "-B", orig_branch, orig_sha],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        else:
            _remote_run(
                ["git", "-C", target_cwd, "checkout", orig_sha],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if stash_sha:
            fetch_ref = f"vps/{ref}" if is_existing else f"origin/{ref}"
            r = _remote_run(
                ["git", "-C", target_cwd, "stash", "apply", fetch_ref],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0:
                _remote_run(
                    ["git", "-C", target_cwd, "read-tree", "-u", "--reset", orig_sha],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                r = _remote_run(
                    ["git", "-C", target_cwd, "cherry-pick", "--no-commit", fetch_ref],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if staged_files:
                _remote_run(["git", "-C", target_cwd, "reset", "HEAD"],
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                _remote_run(["git", "-C", target_cwd, "add", "--"] + staged_files,
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"pull state error: {e}")
        return False
def _get_project_name(cwd: str, host: str | None = None) -> str | None:
    try:
        r = _remote_run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0 or not r.stdout.strip(): return None
        url = r.stdout.strip()
        if url.endswith(".git"): url = url[:-4]
        if ":" in url and not url.startswith("http"): name = url.rsplit("/", 1)[-1] if "/" in url.split(":")[-1] else url.split(":")[-1]
        else: name = url.rsplit("/", 1)[-1]
        return name if name else None
    except (subprocess.SubprocessError, OSError): return None
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
def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pane_pid), "-f", "claude"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return None
    if result.returncode != 0: return None
    output = result.stdout.strip().splitlines()
    if not output: return None
    return output[0].strip()
def _child_count(pid: str, host: str | None = None) -> int:
    if not pid: return 0
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pid)],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return 0
    if result.returncode != 0: return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])
def _ps_stats(pids: list[str], host: str | None = None) -> dict[str, ProcStatsEntry]:
    pid_list = [str(pid) for pid in pids if pid]
    if not pid_list: return {}
    try:
        result = _remote_run(
            ["ps", "-o", "pid=,%cpu=,state=", "-p", ",".join(pid_list)],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return {}
    if result.returncode != 0: return {}
    stats = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 3: continue
        pid = parts[0]
        try: cpu = float(parts[1])
        except ValueError: cpu = 0.0
        state = parts[2]
        stats[pid] = cast(ProcStatsEntry, {"cpu": cpu, "state": state})
    return stats
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
def _learning_reminder_state_file() -> str | None:
    return None
def _save_learning_reminder_state() -> None:
    path = _learning_reminder_state_file()
    if not path: return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f: json.dump(learning_reminders.state, f)
        os.replace(tmp, path)
    except OSError as e: _log(_LOG_ERROR, "bridge", f"Learning reminder state save error: {e}")
def _reset_learning_reminder(name: str) -> None:
    with learning_reminders.lock:
        learning_reminders.state[name] = _new_reminder_state()
        _save_learning_reminder_state()
def _fire_reminder(name: str, st: ReminderState) -> None:
    st["response_count"] = 0
    st["last_reminder_ts"] = _clock.time()
    st["reminder_pending"] = True
    _save_learning_reminder_state()
    reminder = _read_learning_reminder(name)
    _task_pool.submit(_send_learning_reminder, name, reminder)
def _check_learning_reminder(name: str) -> None:
    with learning_reminders.lock:
        st = learning_reminders.state.get(name)
        if st is None:
            st = _new_reminder_state()
            learning_reminders.state[name] = st
        st["last_response_ts"] = _clock.time()
        if st.get("reminder_pending"):
            st["reminder_pending"] = False
            st["response_count"] = 1
            _save_learning_reminder_state()
            return
        st["response_count"] = st.get("response_count", 0) + 1
        if st["response_count"] >= LEARNING_REMINDER_RESPONSE_THRESHOLD: _fire_reminder(name, st)
        else: _save_learning_reminder_state()
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
        changed = False
        for name in worker_names:
            if name not in learning_reminders.state:
                learning_reminders.state[name] = _new_reminder_state()
                changed = True
        if changed: _save_learning_reminder_state()
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
def _ensure_workspace_trusted_remote( cwd: str | None, host: str | None,
) -> None:
    if not cwd: return
    if not host:
        _ensure_workspace_trusted(cwd)
        return
    script = (
        "import json, pathlib, os, fcntl; "
        "p = pathlib.Path(os.path.expanduser('~/.claude.json')); "
        "lk = open(str(p) + '.lock', 'w'); "
        "fcntl.flock(lk, fcntl.LOCK_EX); "
        "d = json.loads(p.read_text()) if p.exists() else {}; "
        "proj = d.setdefault('projects', {}); "
        f"e = proj.get({cwd!r}, {{}}); "
        "changed = e.get('hasTrustDialogAccepted') is not True; "
        f"proj[{cwd!r}] = {{**e, 'hasTrustDialogAccepted': True}} if changed else e; "
        "p.write_text(json.dumps(d, indent=2)) if changed else None; "
        "fcntl.flock(lk, fcntl.LOCK_UN); lk.close(); "
        "print('trusted' if changed else 'already')" )
    try:
        r = _remote_run(
            ["python3", "-c", script],
            host=host, capture_output=True, text=True,
            timeout=TIMEOUT_TMUX_SEND)
        if r.returncode == 0: _log(_LOG_INFO, "trust", f"remote pre-trust {cwd} on {host}: {r.stdout.strip()}")
        else: _log(_LOG_WARN, "trust", f"remote pre-trust failed on {host}: {r.stderr[:200]}")
    except (OSError, TimeoutError) as exc:
        _log(_LOG_WARN, "trust",
             f"remote pre-trust {cwd} on {host}: {exc}")
def _build_teleport_context(
    name: str,
    source_host: str | None,
    target_host: str,
    source_cwd: str | None,
    session_id: str | None,
) -> str:
    src_label = source_host or "VPS (local)"
    lines = [ f"📦 You were teleported from {src_label} to {target_host}.", f"Previous workspace: {source_cwd}", ]
    if session_id:
        lines.append(f"Previous session: {session_id}")
        lines.append("")
        lines.append(
            "To retrieve your previous work context, run:\n"
            f"  beast transcript search --session {session_id} --last 20 --full" )
    else: lines.append("No previous session was active.")
    return "\n".join(lines)
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
def compute_state(
    tmux_exists: bool,
    claude_pid: str | None,
    pending: bool,
    pending_ts: int | None,
    pending_age: float,
    children: int,
    last_child_ts: float,
    cpu: float,
    last_hook_ts: float | None,
    last_seen_claude: float | None,
    now: float,
    is_interactive: bool = True,
    adapter_alive: bool = False,
    poisoned_reason: str | None = None,
) -> tuple[str, str]:
    if not tmux_exists: return "OFFLINE", "tmux missing"
    if not is_interactive:
        if adapter_alive: return "BUSY_TOOL", "adapter running"
        if pending:
            if pending_age < STALE_PENDING: return "WAITING", f"age={int(pending_age)}s"
            hook_since_pending = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
            if pending_age >= STALE_PENDING and not hook_since_pending:
                if poisoned_reason is not None: return "POISONED", f"{poisoned_reason}"
                return "STUCK", f"age={int(pending_age)}s"
            return "WAITING", f"age={int(pending_age)}s"
        return "READY", "idle"
    if not claude_pid and last_seen_claude is not None:
        if (now - last_seen_claude) > START_GRACE: return "DEAD", f"claude missing {int(now - last_seen_claude)}s"
    if pending and children > 0: return "BUSY_TOOL", f"children={children}"
    if pending and children == 0:
        if (pending_age <= THINK_GRACE) or ((now - last_child_ts) <= TOOL_GAP_GRACE) or (cpu >= CPU_ACTIVE):
            return "BUSY_THINKING", f"age={int(pending_age)}s cpu={cpu:.1f}"
        if pending_age < STALE_PENDING: return "WAITING", f"age={int(pending_age)}s"
        hook_since_pending = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
        if pending_age >= STALE_PENDING and cpu < CPU_IDLE and not hook_since_pending:
            if poisoned_reason is not None: return "POISONED", f"{poisoned_reason}"
            return "STUCK", f"age={int(pending_age)}s cpu={cpu:.1f}"
        return "WAITING", f"age={int(pending_age)}s"
    if not pending and children > 0: return "UNTRACKED_BUSY", f"children={children}"
    if claude_pid and not pending: return "READY", "idle"
    return "OFFLINE", "tmux alive, claude missing"
def _check_adapter_log(name: str, tail_lines: int = 20) -> str:
    if tail_lines <= 0: return ""
    host = get_worker_host(name)
    if host:
        try:
            remote_log = _remap_path(str(get_session_dir(name) / "adapter.log"), host)
            r = _remote_run(["tail", "-n", str(tail_lines), remote_log], host=host,
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            return r.stdout if r.returncode == 0 else ""
        except (subprocess.SubprocessError, OSError): return ""
    log_path = get_session_dir(name) / "adapter.log"
    if not log_path.exists(): return ""
    try:
        with log_path.open("r", errors="ignore") as fh: lines = fh.readlines()
        return "".join(lines[-tail_lines:])
    except OSError: return ""
def _send_watchdog_alert(name: str, state: str, reason: str) -> None:
    if admin_chat_id is None: return
    now = _clock.time()
    with watchdog.lock: last = watchdog.last_alert_ts.get(name)
    if last and (now - last) < ALERT_COOLDOWN:
        _log(_LOG_WARN, "watchdog", f"Alert suppressed for {name} ({state}): cooldown {now - last:.0f}s < {ALERT_COOLDOWN}s")
        return
    if state == "WAITING_INPUT":
        with watchdog.lock: details = watchdog.waiting_input_details.get(name)
        header = details.get("header", "") if details else ""; title = f"🟡 {name} needs your reply"
        if header: title += f": {header}"
        parts = [title]
        if details and details.get("options"):
            for o in details["options"]:
                marker = "\u2794 " if o.get("selected") else "  "
                parts.append(f"{marker}{o['num']}. {o['label']}")
            max_num = max(o["num"] for o in details["options"])
            parts.append(f"\nReply 1-{max_num} to choose, or \"skip\" to cancel.")
        text = "\n".join(parts)
    elif state == "STUCK":
        age_match = re.search(r"age=(\d+)s", reason); age_min = int(age_match.group(1)) // 60 if age_match else 0
        age_str = f"{age_min}min" if age_min > 0 else reason.split()[0]
        text = f"🔴 {name} has made no progress for {age_str}.\n/restart --clean {name} (starts fresh)"
    elif state == "POISONED": text = f"🔴 {name} is stuck in an error loop.\n/restart --clean {name} (starts fresh)"
    elif state == "DEAD": text = f"🔴 {name} stopped unexpectedly.\n/restart --clean {name} (starts fresh)"
    elif state == "EXITED": text = f"🟡 {name}'s session ended.\n/restart {name}"
    elif state == "OFFLINE": text = f"🔴 {name} is not running.\n/hire {name}"
    else: text = f"{name}: {state} ({reason}). Check /team"
    try:
        import telegram as _tg
        result = _tg.transport.send_text(admin_chat_id, text)
        if result and result.get("ok"):
            _log(_LOG_WARN, "watchdog", f"Alert sent for {name} ({state}): {text[:80]}")
            _res = result.get("result", {}); msg_id = _res.get("message_id") if isinstance(_res, dict) else None
            with watchdog.lock:
                watchdog.last_alert_ts[name] = now
                if msg_id: watchdog.alert_msg_ids[name] = (msg_id, text)
        else: _log(_LOG_WARN, "watchdog", f"Alert FAILED for {name} ({state}): {result}")
    except KeyError as e: _log(_LOG_ERROR, "watchdog", f"Watchdog alert error: {e}")
def _format_watchdog_status(name: str,
                            pending_lookup: Callable[[str], bool] | None = None,
                            state_snapshot: dict[str, WorkerStateEntry] | None = None) -> str:
    if pending_lookup is None: pending_lookup = is_pending
    if state_snapshot is None:
        with watchdog.lock: state_snapshot = dict(watchdog.worker_states)
    return _format_watchdog_status_pure(
        name, pending_lookup=pending_lookup,
        state_snapshot=state_snapshot, clock_now=_clock.time(), )
def format_team_lines(  # type: ignore[no-redef]
    registered: dict[str, TmuxSessionDict],
    active: str | None,
    pending_lookup: Callable[[str], bool] | None = None,
    worker_live: dict[str, TmuxSessionDict] | dict[str, dict[str, str | None]] | None = None
) -> list[str]:
    if pending_lookup is None: pending_lookup = is_pending
    with watchdog.lock: state_snapshot = dict(watchdog.worker_states)
    return _format_team_lines_pure(
        registered, active,
        pending_lookup=pending_lookup, worker_live=worker_live,
        state_snapshot=state_snapshot, clock_now=_clock.time(),
        normalize_backend_fn=normalize_backend, )
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
class WorkerManager:
    def __init__(self, sessions_dir: Path, tmux_prefix: str,
                 runner: SubprocessRunner | None = None,
                 clock: Clock | None = None) -> None:
        self.sessions_dir = sessions_dir; self.tmux_prefix = tmux_prefix
        self._runner: SubprocessRunner = runner or _subprocess_runner
        self._clock: Clock = clock or _clock
    def _sync_paths(self) -> None:
        if self.sessions_dir != SESSIONS_DIR: self.sessions_dir = SESSIONS_DIR
        if self.tmux_prefix != TMUX_PREFIX: self.tmux_prefix = TMUX_PREFIX
    def _get_startup_cwd(self, name: str, requested_cwd: str = "", fallback_cwd: str = "") -> str:
        candidate = normalize_cwd(requested_cwd)
        if not candidate: candidate = normalize_cwd(_get_worker_cwd(name))
        if candidate:
            if os.path.isdir(candidate): return candidate
            _log(_LOG_WARN, "bridge", f"Ignoring invalid startup cwd for {name}: {candidate}")
        fallback = normalize_cwd(fallback_cwd)
        if fallback and os.path.isdir(fallback): return fallback
        return ""
    def _get_tmux_pane_cwd(self, tmux_name: str, host: str | None = None) -> str:
        result = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_path}"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK )
        if result.returncode == 0: return result.stdout.strip()
        return ""
    def _cd_tmux_to_cwd(self, tmux_name: str, cwd: str) -> None:
        if not cwd: return
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"cd {shlex.quote(cwd)}", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_SHORT)
    def scan_tmux_sessions(self) -> dict[str, TmuxSessionDict]:
        self._sync_paths()
        registered: dict[str, TmuxSessionDict] = {}
        try:
            result = self._runner.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line: continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix):]
                        backend = normalize_backend(get_tmux_env_value(session_name, "WORKER_BACKEND"))
                        registered[name] = {"tmux": session_name, "backend": backend}
        except (subprocess.SubprocessError, KeyError) as e: _log(_LOG_ERROR, "bridge", f"Error scanning local tmux: {e}")
        try: machines = get_machine_catalog()
        except (subprocess.SubprocessError, OSError): machines = {}
        for machine in machines.values():
            if machine.is_local or not machine.ssh_target: continue
            if machine.role not in ("worker-host", ""): continue
            try:
                r = _remote_run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    host=machine.ssh_target,
                    capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD )
                if r.returncode != 0: continue
                for line in r.stdout.strip().split("\n"):
                    if not line: continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix):]
                        if name not in registered:
                            registered[name] = {
                                "tmux": session_name,
                                "backend": DEFAULT_BACKEND,
                                "host": machine.ssh_target, }
                            _registry_add(name, DEFAULT_BACKEND, host=machine.ssh_target)
            except (subprocess.SubprocessError, KeyError) as e:
                _log(_LOG_ERROR, "bridge", f"Error scanning tmux on {machine.ssh_target}: {e}")
        return registered
    _sessions_cache = None
    _sessions_cache_ts: float = 0
    _sessions_cache_lock = threading.Lock(); _SESSIONS_CACHE_TTL = 15
    def invalidate_sessions_cache(self) -> None:
        with self._sessions_cache_lock: self._sessions_cache = None; self._sessions_cache_ts = 0
    def get_registered_sessions(self, registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
        self._sync_paths()
        if registered is None:
            now = self._clock.time()
            with self._sessions_cache_lock:
                if self._sessions_cache is not None and (now - self._sessions_cache_ts) < self._SESSIONS_CACHE_TTL:
                    return dict(self._sessions_cache)
            registered = self.scan_tmux_sessions()
        registry = _load_registry()
        for rname, rentry in registry.get("workers", {}).items():
            if rname not in registered and rentry.get("backend") != "claude":
                registered[rname] = {"backend": rentry.get("backend", "codex")}
        _registry_bootstrap(registered)
        registry = _load_registry()
        for name, info in registry.get("workers", {}).items():
            if name not in registered:
                entry: TmuxSessionDict = {"backend": str(info.get("backend", DEFAULT_BACKEND))}
                for key in ("protocol", "callback_url", "host", "version"):
                    val = info.get(key)
                    if val: entry[key] = str(val)
                if info.get("host") and not info.get("callback_url"): entry["tmux"] = f"{self.tmux_prefix}{name}"
                registered[name] = entry
            else:
                for key in ("protocol", "callback_url", "host", "version"):
                    if info.get(key): registered[name][key] = info.get(key)  # type: ignore[literal-required]
        if state.active and state.active not in registered: state.active = None
        if registered and not state.active: state.active = list(registered.keys())[0]
        with self._sessions_cache_lock:
            self._sessions_cache = dict(registered); self._sessions_cache_ts = self._clock.time()
        return registered
    def is_online(self, name: str, session: TmuxSessionDict | None = None) -> bool:
        self._sync_paths()
        if not session: sessions = self.get_registered_sessions(); session = sessions.get(name)
        if not session: return False
        if session.get("callback_url"): return True
        backend_name = normalize_backend(session.get("backend")); backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}"); host = get_worker_host(name)
        if host:
            try:
                if not tmux_exists(tmux_name, host=host, timeout=TIMEOUT_TMUX_CHECK): return False
                if backend.is_interactive:
                    try: return is_claude_running(tmux_name, host=host)
                    except (subprocess.SubprocessError, OSError): return True
                return True
            except (subprocess.SubprocessError, OSError): return True
        return backend.is_online(tmux_name)
    def send(self, name: str, message: str, chat_id: ChatId | None = None, session: TmuxSessionDict | None = None) -> bool:
        self._sync_paths()
        if not session: sessions = self.get_registered_sessions(); session = sessions.get(name)
        if not session: return False
        if session.get("callback_url"): return _send_to_callback_worker(name, message, "manager", session)
        backend_name = normalize_backend(session.get("backend")); backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        return backend.send(name, tmux_name, message, BRIDGE_URL, self.sessions_dir)
    def _pipe_worker_entry(self, name: str, peer_host: str | None, caller_host: str | None) -> WorkerEndpointInfo:
        pipe_path = ensure_worker_pipe(name)
        pipe_cmd = f"echo 'YOUR_NAME: your message here' > {pipe_path} &"
        return cast(WorkerEndpointInfo, {
            "name": name, "machine": peer_host or BRIDGE_SSH_TARGET, "protocol": "pipe",
            "address": str(pipe_path), "send_example": self._wrap_for_caller(pipe_cmd, peer_host, caller_host),
            "note": "Non-interactive. IMPORTANT: Always prefix your name (e.g., 'kenji: hello'). Always use & (background) when writing to pipe — it BLOCKS until read. Never use cat/echo without & or your session will freeze.",
        })
    def get_workers(self, caller_from: str | None = None) -> list[WorkerEndpointInfo]:
        self._sync_paths()
        workers: list[WorkerEndpointInfo] = []
        registered = self.get_registered_sessions(); caller_host = get_worker_host(caller_from) if caller_from else None
        for name, info in registered.items():
            callback_url = info.get("callback_url", "")
            if callback_url:
                msg_url = callback_url.rstrip("/")
                if not msg_url.endswith("/msg"): msg_url = f"{msg_url}/msg"
                payload = json.dumps({"from": "YOUR_NAME", "text": "your message here"})
                workers.append(cast(WorkerEndpointInfo, {"name": name, "machine": info.get("host", "") or BRIDGE_SSH_TARGET,
                    "protocol": "http", "address": msg_url,
                    "send_example": f"curl -sS -X POST {shlex.quote(msg_url)} -H 'Content-Type: application/json' --data-raw {shlex.quote(payload)}",
                    "note": "HTTP callback worker. POST JSON with from/text. Always set from to your worker name."}))
                continue
            backend_name = get_worker_backend(name, info); backend = get_backend(backend_name)
            peer_host = get_worker_host(name)
            if "tmux" not in info:
                if not backend.is_interactive: workers.append(self._pipe_worker_entry(name, peer_host, caller_host))
                else:
                    workers.append(cast(WorkerEndpointInfo, {"name": name, "machine": peer_host or BRIDGE_SSH_TARGET,
                        "protocol": "none", "address": "", "status": "exited", "note": f"Worker exited. Use /restart {name} to bring back."}))
                continue
            if not backend.is_interactive:
                if peer_host:
                    workers.append(cast(WorkerEndpointInfo, {"name": name, "machine": peer_host, "protocol": "adapter",
                        "address": f"{peer_host}:{info.get('tmux', '')}",
                        "note": f"Non-interactive ({backend_name}) on {peer_host}. Use @{name} from Telegram or bridge API."}))
                else: workers.append(self._pipe_worker_entry(name, peer_host, caller_host))
            else:
                tmux_name = info.get("tmux")
                tmux_cmd = (
                    f"echo 'YOUR_NAME: your message here' | "
                    f"tmux load-buffer - && "
                    f"tmux paste-buffer -p -r -t {tmux_name} && "
                    f"sleep 1 && tmux send-keys -t {tmux_name} Enter" )
                send_example = self._wrap_for_caller(tmux_cmd, peer_host, caller_host)
                if peer_host and caller_host == peer_host:
                    note = f"On {peer_host} (same machine as caller). Uses paste-buffer -p. Always prefix your name."
                elif peer_host: note = f"On {peer_host}. Uses SSH + paste-buffer -p (bracketed paste). Always prefix your name."
                elif caller_host: note = f"On bridge host (cross-machine from caller). Uses SSH + paste-buffer -p. Always prefix your name."
                else:
                    note = "Uses paste-buffer -p (bracketed paste) for reliable delivery. Sleep 1s before Enter — TUI needs time to render. Always prefix your name."
                workers.append(cast(WorkerEndpointInfo, {"name": name, "machine": peer_host or BRIDGE_SSH_TARGET, "protocol": "tmux",
                    "address": f"{peer_host}:{tmux_name}" if peer_host else tmux_name, "send_example": send_example, "note": note}))
        return workers
    def _wrap_for_caller(self, cmd: str, peer_host: str | None, caller_host: str | None) -> str:
        if caller_host == peer_host: return cmd
        if peer_host is None: ssh_target = BRIDGE_SSH_TARGET
        else: ssh_target = peer_host
        escaped = cmd.replace('"', '\\"')
        return f'ssh {ssh_target} "{escaped}"'
    def _build_welcome(self, name: str, backend_obj: Backend) -> str:
        welcome = (
            "You are connected to Telegram via claudecode-telegram bridge. "
            "RECEIVING FILES: Manager sends files (images, PDFs, documents) — they appear as local paths you can read directly. "
            "SENDING FILES: Use [[image:/path/to/photo.png|caption]] for images (jpg/png/webp/bmp) and animations (gif/mp4), or [[file:/path/to/file|caption]] for documents, video (mp4/mov/avi — shows player), audio (mp3/m4a/flac — shows player), and voice (ogg/opus — voice bubble). "
            f"MESSAGING WORKERS: Run `curl -s \"$BRIDGE_URL/workers?from={name}\"` to discover other workers — returns JSON with a `send_example` field containing ready-to-use send commands wrapped correctly for your machine (auto-adds ssh when a peer lives elsewhere). Always call /workers?from={name} before messaging, never guess addresses. Never use POST /outputs to message another worker; /outputs only publishes your own worker output to Telegram. "
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
                "Use nohup/& if calling CLI directly." )
        note = read_checkin_note()
        if note:
            rendered = note.replace("{name}", name); host = get_worker_host(name)
            if host: machine = f"Mac Mini ({host})"
            else: machine = "VPS (100.125.36.102)"
            rendered = rendered.replace("{machine}", machine); welcome += f"\n\nMANAGER NOTE:\n{rendered}"
            _log(_LOG_INFO, "checkin", f"Checkin note included for {name}")
        return welcome
    def hire(self, name: str, backend: str = DEFAULT_BACKEND, chat_id: ChatId | None = None) -> tuple[bool, str | None]:
        self._sync_paths()
        if not is_valid_backend(backend): return False, f"Unknown backend '{backend}'. Available: {', '.join(list_backends())}"
        backend_obj = get_backend(backend)
        if not _which_binary(backend_obj.binary): return False, f"'{backend_obj.binary}' not found in PATH. Install it first."
        tmux_name = f"{self.tmux_prefix}{name}"
        if tmux_exists(tmux_name): return False, f"Worker '{name}' already exists"
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env, timeout=TIMEOUT_REMOTE_CMD )
        if result.returncode != 0: return False, "Could not start the worker workspace"
        self._runner.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        startup_cwd = self._get_startup_cwd(name)
        if startup_cwd: self._cd_tmux_to_cwd(tmux_name, startup_cwd)
        export_hook_env(tmux_name, backend)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)
        ensure_session_dir(name)
        if chat_id:
            chat_id_file = get_chat_id_file(name); _tmp = chat_id_file.with_suffix('.tmp')
            _tmp.write_text(str(chat_id))
            _tmp.chmod(0o600)
            os.replace(str(_tmp), str(chat_id_file))
        if not backend_obj.is_interactive: ensure_worker_pipe(name)
        start_cmd = f'unset CLAUDECODE && {backend_obj.start_cmd()}'
        if startup_cwd: start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        if backend_obj.is_interactive:
            self._clock.sleep(DELAY_STARTUP_LONG)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        if backend_obj.is_interactive: self._clock.sleep(DELAY_RESPONSE_GAP)
        welcome = self._build_welcome(name, backend_obj)
        if not backend_obj.is_interactive:
            if chat_id: set_pending(name, chat_id)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        else: self.send(name, welcome)
        state.active = name
        import telegram as _tg
        _tg.save_last_active(name)
        _registry_add(name, backend, chat_id)
        _reset_learning_reminder(name)
        if not backend_obj.is_interactive: _log(_LOG_INFO, "worker", f"Created {backend} worker '{name}' (non-interactive mode)")
        self.invalidate_sessions_cache()
        return True, None
    def end(self, name: str) -> tuple[bool, str | None]:
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered: return False, f"Worker '{name}' not found"
        session = registered[name]; backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name); tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        if not backend.is_interactive:
            kill_adapter(name)
            session_dir = self.sessions_dir / name
            try:
                for session_id_file in session_dir.glob("*_session_id"): session_id_file.unlink()
            except OSError as e: return False, f"Failed to clean non-interactive metadata: {e}"
        clear_pending(name)
        _set_worker_cwd(name, "")
        host = get_worker_host(name)
        _remote_run(["tmux", "kill-session", "-t", tmux_name], host=host, capture_output=True)
        cleanup_inbox(name)
        cleanup_worker_pipe(name)
        _registry_remove(name)
        self.invalidate_sessions_cache()
        if state.active == name:
            state.active = None
            self.get_registered_sessions()
        return True, None
    def restart(self, name: str, mode: str = "relaunch") -> tuple[bool, str | None]:
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered: return False, f"Worker '{name}' not found"
        host = get_worker_host(name)
        if host: return False, "use_remote_restart"
        session = registered[name]; backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name); tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        if not tmux_exists(tmux_name): return self._restart_dead_worker(name, backend_name, backend, tmux_name, mode)
        if not _which_binary(backend.binary): return False, f"'{backend.binary}' not found in PATH. Install it first."
        resume_id, startup_cwd = self._prepare_restart_state(name, mode)
        if mode != "resume": _clear_hook_failures(name)
        session_dir = self.sessions_dir / name
        if not backend.is_interactive:
            session_dir.mkdir(parents=True, exist_ok=True)
            ensure_worker_pipe(name)
            clear_pending(name)
        elif is_claude_running(tmux_name): self._stop_running_claude(name, tmux_name)
        if backend.is_interactive and not is_claude_running(tmux_name): self._kill_stray_children(name, tmux_name)
        export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._send_start_command(name, tmux_name, backend, resume_id, startup_cwd)
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = self._wait_for_startup(name, tmux_name, backend, resume_id, startup_cwd)
            if started: self.send(name, welcome)
            else: _log(_LOG_WARN, "restart", f"{name}: Claude did not start within 10s, skipping welcome")
        else: self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        _reset_learning_reminder(name)
        self.invalidate_sessions_cache()
        return True, None
    def _prepare_restart_state(self, name: str, mode: str) -> tuple[str, str]:
        resume_id = ""; resume_cwd = ""; session_dir = self.sessions_dir / name
        if mode == "resume":
            resume_id = (get_claude_session_id(name, authoritative=False) or
                         get_claude_session_id(name, authoritative=True) or "")
            resume_cwd = ""
        else:
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"): session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd: _ensure_workspace_trusted(startup_cwd)
        return resume_id, startup_cwd
    def _stop_running_claude(self, name: str, tmux_name: str) -> None:
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_STARTUP)
        if is_claude_running(tmux_name):
            pane_pid = _tmux_pane_pids().get(tmux_name)
            if pane_pid:
                claude_pid = _get_claude_pid(pane_pid)
                if claude_pid: self._runner.run(["kill", claude_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        for _ in range(20):
            if not is_claude_running(tmux_name): break
            self._clock.sleep(DELAY_SHORT)
        else: _log(_LOG_WARN, "restart", f"{name}: Claude still running after 5s kill wait")
    def _kill_stray_children(self, name: str, tmux_name: str) -> None:
        pane_pids = _tmux_pane_pids(); pane_pid = pane_pids.get(tmux_name)
        if pane_pid:
            stray = self._runner.run(
                ["pgrep", "-P", str(pane_pid)],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
            if stray.returncode == 0:
                for child_pid in stray.stdout.strip().splitlines():
                    child_pid = child_pid.strip()
                    if child_pid and child_pid.isdigit():
                        _log(_LOG_INFO, "restart", f"{name}: killing stray child pid {child_pid}")
                        self._runner.run(["kill", child_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
                self._clock.sleep(DELAY_RETRY)
    def _send_start_command(self, name: str, tmux_name: str, backend: Backend,
                            resume_id: str, startup_cwd: str) -> None:
        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)
        start_cmd = backend.start_cmd(resume_id); start_cmd = f'unset CLAUDECODE && {start_cmd}'
        if startup_cwd: start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
    def _wait_for_startup(self, name: str, tmux_name: str, backend: Backend,
                          resume_id: str, startup_cwd: str) -> bool:
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if is_claude_running(tmux_name):
                started = True
                break
        if not started and resume_id: started = self._retry_after_stale_resume(name, tmux_name, backend, resume_id, startup_cwd)
        return started
    def _retry_after_stale_resume(self, name: str, tmux_name: str, backend: Backend,
                                  resume_id: str, startup_cwd: str) -> bool:
        _log(_LOG_WARN, "restart", f"{name}: resume failed (stale session {resume_id[:8]}), auto-retrying fresh")
        clear_claude_session_id(name)
        _clear_hook_failures(name)
        start_cmd = backend.start_cmd(""); start_cmd = f'unset CLAUDECODE && {start_cmd}'
        if startup_cwd: start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if is_claude_running(tmux_name):
                started = True
                break
        if started:
            _log(_LOG_INFO, "restart", f"{name}: fresh start succeeded after stale resume")
            _notify_admin(f"⚠️ {name}: stale session ID {resume_id[:8]}… — auto-restarted fresh ✓")
        else:
            _log(_LOG_WARN, "restart", f"{name}: fresh start also failed after stale resume")
            _notify_admin(f"🔴 {name}: resume failed (stale {resume_id[:8]}…) AND fresh start failed.\n/restart --clean {name} to try manually.")
        return started
    def _restart_dead_worker(self, name: str, backend_name: str, backend: Backend, tmux_name: str, mode: str) -> tuple[bool, str | None]:
        if not _which_binary(backend.binary): return False, f"'{backend.binary}' not found in PATH. Install it first."
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env, timeout=TIMEOUT_REMOTE_CMD)
        if result.returncode != 0: return False, "Could not create worker workspace"
        self._runner.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        ensure_session_dir(name)
        if not backend.is_interactive: ensure_worker_pipe(name)
        resume_id, startup_cwd = self._prepare_restart_state(name, mode)
        export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._send_start_command(name, tmux_name, backend, resume_id, startup_cwd)
        if backend.is_interactive:
            self._clock.sleep(DELAY_STARTUP_LONG)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = self._wait_for_startup(name, tmux_name, backend, resume_id, startup_cwd)
            if started: self.send(name, welcome)
            else: _log(_LOG_WARN, "restart", f"{name}: dead worker did not start within 10s, skipping welcome")
        else: self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        _log(_LOG_INFO, "worker", f"Dead worker '{name}' recovered from registry (mode={mode})")
        self.invalidate_sessions_cache()
        return True, None
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
class _GuestSessionDictRequired(TypedDict):
    name: str
    created_at: str
    expires_at_unix: float
    notified_workers: set[str] | list[str]
class GuestSessionDict(_GuestSessionDictRequired, total=False):
    token_hash: str
    expires_at: str
GuestInboxMessageDict = TypedDict("GuestInboxMessageDict", {
    "id": str,
    "from": str,
    "sender": str,
    "text": str,
    "ts": int,
    "to": str,
    "channel": str,
}, total=False)
"""Shape of a guest inbox message in storage."""
class ChannelMemberDict(TypedDict, total=False):
    type: str
    name: str
ChannelMessageDict = TypedDict("ChannelMessageDict", { "id": str, "seq": int, "from": str, "text": str, "ts": int,
})
"""Shape of a message inside a channel's messages list."""
class ChannelDict(TypedDict):
    id: str
    label: str
    created_at: str
    expires_at_unix: float
    seq: int
    created_by: str
    members: dict[str, ChannelMemberDict]
    messages: list[ChannelMessageDict]
_RelayMessageDictRequired = TypedDict("_RelayMessageDictRequired", {
    "message_id": str,
    "direction": str,
    "from": str,
    "to": str,
    "text": str,
    "ts": str,
})
class RelayMessageDict(_RelayMessageDictRequired, total=False):
    sender_name: str
class RelayChannelDict(TypedDict):
    id: str
    label: str
    worker: str
    workers: list[str]
    created_at: str
    expires_at: str
    expires_at_unix: float
    guest_token_hash: str
    reply_token_hash: str
    reply_token: str
    messages: list[RelayMessageDict]
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
@dataclass
class GuestSession:
    name: str
    token_hash: str
    created_at: str
    expires_at_unix: float
    notified_workers: set[str]
    @classmethod
    def from_dict(cls: type["GuestSession"], token_hash: str, data: GuestSessionDict) -> "GuestSession":
        notified = data.get("notified_workers", set())
        if isinstance(notified, list): notified = set(notified)
        return cls(name=data["name"], token_hash=token_hash, created_at=data.get("created_at", ""),
            expires_at_unix=data["expires_at_unix"], notified_workers=notified)
    def to_dict(self) -> GuestSessionDict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "expires_at_unix": self.expires_at_unix,
            "notified_workers": self.notified_workers, }
    @property
    def is_expired(self) -> bool:
        return _clock.time() >= self.expires_at_unix
@dataclass
class GuestInboxMessage:
    id: str
    sender: str
    text: str
    ts: int
    @classmethod
    def from_dict(cls: type["GuestInboxMessage"], data: GuestInboxMessageDict) -> "GuestInboxMessage":
        return cls(
            id=data.get("id", ""),
            sender=data.get("from", data.get("sender", "")),
            text=data.get("text", ""),
            ts=data.get("ts", 0), )
@dataclass
class ChannelMember:
    key: str
    type: str         # "manager", "worker", "guest"  # noqa: A003 — shadows builtin
    name: str = ""
    @classmethod
    def from_dict(cls, key: str, data: ChannelMemberDict) -> "ChannelMember":
        return cls(key=key, type=data["type"], name=data.get("name", ""))
@dataclass
class ChannelMessage:
    id: str
    seq: int
    sender: str
    text: str
    ts: int
    @classmethod
    def from_dict(cls: type["ChannelMessage"], data: ChannelMessageDict) -> "ChannelMessage":
        return cls( id=data["id"], seq=data["seq"], sender=data["from"], text=data["text"], ts=data["ts"], )
@dataclass
class RelayMessage:
    id: str
    sender: str
    text: str
    ts: int
    sender_name: str = ""
    @classmethod
    def from_dict(cls: type["RelayMessage"], d: RelayMessageDict) -> "RelayMessage":
        return cls(id=d.get("message_id", ""), sender=d.get("from", d.get("sender", "")),
            text=d.get("text", ""), ts=int(d.get("ts", 0)), sender_name=d.get("sender_name", ""))
class GuestStore:
    def __init__(self) -> None:
        self.guests: dict[str, GuestSessionDict] = {}
        self.inboxes: dict[str, list[GuestInboxMessageDict]] = {}
        self.lock: threading.Lock = threading.Lock()
guest_store = GuestStore()
GUEST_TTL = 86400
GUEST_INBOX_CAP = 200
def _save_state_json(path: Path, data: object, label: str) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f: json.dump(data, f, indent=2)
        tmp.rename(path); os.chmod(path, 0o600)
    except OSError as e: _log(_LOG_WARN, label, f"Failed to save state: {e}")
def _load_state_json(path: Path, label: str) -> dict[str, object] | None:
    if not path.exists(): return None
    try:
        with open(path) as f:
            result: object = json.load(f)
            return cast(dict[str, object], result) if isinstance(result, dict) else None
    except (json.JSONDecodeError, OSError, KeyError) as e:
        _log(_LOG_WARN, label, f"Failed to load state: {e}"); return None
def _guest_state_path() -> Path:
    return NODE_DIR / "guest_state.json"
def _guest_save() -> None:
    now = _clock.time(); active_guests = {}
    for k, v in guest_store.guests.items():
        if now <= v.get("expires_at_unix", 0):
            guest = dict(v)
            if isinstance(guest.get("notified_workers"), set):
                guest["notified_workers"] = list(cast("set[str] | list[str]", guest.get("notified_workers", [])))
            active_guests[k] = guest
    active_names = {guest["name"] for guest in active_guests.values()}
    active_inboxes = {k: v for k, v in guest_store.inboxes.items() if k in active_names}
    _save_state_json(_guest_state_path(), {"guests": active_guests, "inboxes": active_inboxes}, "guest")
def _guest_load() -> None:
    data = _load_state_json(_guest_state_path(), "guest")
    if not data: return
    now = _clock.time(); restored = 0; raw_guests = data.get("guests", {})
    if isinstance(raw_guests, dict):
        for k, v in raw_guests.items():
            if isinstance(v, dict) and now <= v.get("expires_at_unix", 0):
                if isinstance(v.get("notified_workers"), list): v["notified_workers"] = set(v["notified_workers"])
                guest_store.guests[k] = cast("GuestSessionDict", v); restored += 1
    raw_inboxes = data.get("inboxes", {})
    if isinstance(raw_inboxes, dict): guest_store.inboxes.update(cast("dict[str, list[GuestInboxMessageDict]]", raw_inboxes))
    if restored: _log(_LOG_INFO, "guest", f"Restored {restored} active guest(s) from disk")
def guest_create_token() -> tuple[str, str]:
    token = f"gt_{secrets.token_urlsafe(32)}"; token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash
def guest_generate_name(existing_names: set[str] | None = None) -> str:
    if existing_names is None: existing_names = set()
    for _ in range(100):
        name = secrets.token_urlsafe(3).rstrip("=").lower()[:5]
        if len(name) >= 3 and name not in existing_names: return name
    return secrets.token_urlsafe(4).rstrip("=").lower()[:6]
def guest_validate_name(name: str, team_workers: set[str], existing_guests: set[str]) -> tuple[bool, str]:
    if not name or not name.strip(): return False, "name is required"
    name = name.strip().lower()
    if len(name) > 20: return False, "name too long (max 20 chars)"
    if name in team_workers: return False, f"name '{name}' conflicts with a team worker"
    if name in existing_guests: return False, f"name '{name}' already taken by another guest"
    return True, ""
def guest_is_expired(expires_at_unix: float) -> bool:
    return _clock.time() >= expires_at_unix
def guest_inbox_filter(messages: list[GuestInboxMessageDict], after: str | None = None) -> list[GuestInboxMessageDict]:
    if not after: return list(messages)
    found = False; result = []
    for m in messages:
        if found: result.append(m)
        elif m.get("id") == after: found = True
    if not found: return list(messages)
    return result
def guest_inbox_append(inbox: list[GuestInboxMessageDict], msg: GuestInboxMessageDict) -> list[GuestInboxMessageDict]:
    inbox.append(msg)
    if len(inbox) > GUEST_INBOX_CAP: inbox = inbox[-GUEST_INBOX_CAP:]
    return inbox
class ChannelStore:
    def __init__(self) -> None:
        self.channels: dict[str, ChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()
channel_store = ChannelStore()
CHANNEL_TTL = 86400
CHANNEL_MSG_CAP = 200
def _channel_state_path() -> Path:
    return NODE_DIR / "channel_state.json"
def _channel_save() -> None:
    active = {cid: ch for cid, ch in channel_store.channels.items() if not channel_is_expired(ch)}
    _save_state_json(_channel_state_path(), active, "channel")
def _channel_load() -> None:
    data = _load_state_json(_channel_state_path(), "channel")
    if not data: return
    restored = 0
    for cid, ch_raw in data.items():
        ch = cast("ChannelDict", ch_raw)
        if not channel_is_expired(ch): channel_store.channels[cid] = ch; restored += 1
    if restored: _log(_LOG_INFO, "channel", f"Restored {restored} active channel(s) from disk")
def channel_create_id(label: str = "") -> str:
    short = secrets.token_urlsafe(4).rstrip("=").lower()[:6]
    return f"ch_{short}"
def _channel_member_entry(m: str) -> tuple[str, ChannelMemberDict] | None:
    if m == "manager": return "manager", cast(ChannelMemberDict, {"type": "manager"})
    if ":" in m:
        mtype, name = m.split(":", 1)
        if mtype in ("worker", "guest"): return m, cast(ChannelMemberDict, {"type": mtype, "name": name})
    return None
def channel_new(channel_id: str, label: str, created_by: str,
                members: list[str], ttl: int = CHANNEL_TTL) -> ChannelDict:
    now = _clock.time()
    member_dict: dict[str, ChannelMemberDict] = {k: v for m in members for pair in [_channel_member_entry(m)] if pair for k, v in [pair]}
    return {"id": channel_id, "label": label, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "expires_at_unix": now + ttl, "seq": 0, "created_by": created_by,
            "members": member_dict, "messages": []}
def channel_add_members(channel: ChannelDict, members: list[str]) -> list[str]:
    added = []
    for m in members:
        if m in channel["members"]: continue
        entry = _channel_member_entry(m)
        if entry: channel["members"][entry[0]] = entry[1]; added.append(m)
    return added
def channel_remove_members(channel: ChannelDict, members: list[str]) -> list[str]:
    removed = []
    for m in members:
        if m in channel["members"]:
            del channel["members"][m]
            removed.append(m)
    return removed
def channel_append_message(channel: ChannelDict, from_member: str, text: str) -> ChannelMessageDict:
    channel["seq"] += 1
    msg = {
        "id": f"cm_{channel['seq']:06d}",
        "seq": channel["seq"],
        "from": from_member,
        "text": text,
        "ts": int(_clock.time()), }
    channel["messages"].append(cast(ChannelMessageDict, msg))
    if len(channel["messages"]) > CHANNEL_MSG_CAP: channel["messages"] = channel["messages"][-CHANNEL_MSG_CAP:]
    return cast(ChannelMessageDict, msg)
def channel_get_messages(channel: ChannelDict, after: str | None = None) -> tuple[list[ChannelMessageDict], bool]:
    if not after: return list(channel["messages"]), False
    found_idx = -1
    for i, m in enumerate(channel["messages"]):
        if m["id"] == after:
            found_idx = i
            break
    if found_idx == -1: return list(channel["messages"]), True
    return channel["messages"][found_idx + 1:], False
def channel_is_expired(channel: ChannelDict) -> bool:
    return _clock.time() > channel["expires_at_unix"]
class RelayStore:
    def __init__(self) -> None:
        self.channels: dict[str, RelayChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()
relay_store = RelayStore()
RELAY_PUBLIC_HOST = os.environ.get("RELAY_PUBLIC_HOST", "157.180.48.254")
def _relay_state_path() -> Path:
    return NODE_DIR / "relay_state.json"
def _relay_save() -> None:
    now = _clock.time()
    active = {cid: ch for cid, ch in relay_store.channels.items() if now <= ch["expires_at_unix"]}
    _save_state_json(_relay_state_path(), active, "relay")
def _relay_load() -> None:
    data = _load_state_json(_relay_state_path(), "relay")
    if not data: return
    now = _clock.time(); restored = 0
    for cid, ch_raw in data.items():
        ch = cast("RelayChannelDict", ch_raw)
        if now <= ch.get("expires_at_unix", 0): relay_store.channels[cid] = ch; restored += 1
    if restored: _log(_LOG_INFO, "relay", f"Restored {restored} active relay(s) from disk")
def relay_channel_create(worker: str, label: str, ttl: int = 86400) -> tuple[RelayChannelDict, str, str]:
    channel_id = channel_create_id(label); guest_token = f"gt_{secrets.token_urlsafe(32)}"
    reply_token = f"rt_{secrets.token_urlsafe(32)}"; now = _clock.time()
    channel = {
        "id": channel_id,
        "label": label,
        "worker": worker,
        "workers": [worker],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + ttl)),
        "expires_at_unix": now + ttl,
        "guest_token_hash": hashlib.sha256(guest_token.encode()).hexdigest(),
        "reply_token_hash": hashlib.sha256(reply_token.encode()).hexdigest(),
        "reply_token": reply_token,
        "messages": [], }
    return cast(RelayChannelDict, channel), guest_token, reply_token
def _relay_base_url() -> str:
    if BRIDGE_PUBLIC_URL: return BRIDGE_PUBLIC_URL
    return f"http://{RELAY_PUBLIC_HOST}:{PORT}"
def relay_guide_url(channel_id: str, guest_token: str) -> str:
    return f"{_relay_base_url()}/relay/{channel_id}?token={guest_token}"
def relay_guide_text(channel: RelayChannelDict, guest_token: str) -> str:
    base = f"{_relay_base_url()}/relay/{channel['id']}"; w = channel["worker"]; exp = channel['expires_at']
    return (f"# Chat Channel to {w}\nDirect chat channel to **{w}** (a Claude Code agent).\n"
            f'```bash\nexport RELAY_TOKEN="{guest_token}"\nexport RELAY="{base}"\n```\n'
            f"```bash\ncurl -fsS $RELAY/send -H \"Authorization: Bearer $RELAY_TOKEN\" -H \"Content-Type: application/json\" -d '{{\"text\":\"your message here\"}}'\n```\n"
            f"```bash\ncurl -fsS $RELAY/messages -H \"Authorization: Bearer $RELAY_TOKEN\"\n```\n"
            f"1. Send a message using the curl command above\n2. Poll `/messages` to see replies (add `?after=<message_id>` for new messages only)\n3. The channel expires at `{exp}`\n")
def _relay_auth(channel_id: str, token: str, hash_key: str) -> RelayChannelDict | None:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel or _clock.time() > channel.get("expires_at_unix", 0): return None
    if hashlib.sha256(token.encode()).hexdigest() != channel.get(hash_key): return None
    return channel
def relay_auth_guest(channel_id: str, token: str) -> RelayChannelDict | None:
    return _relay_auth(channel_id, token, "guest_token_hash")
def relay_auth_reply(channel_id: str, token: str) -> RelayChannelDict | None:
    return _relay_auth(channel_id, token, "reply_token_hash")
def _relay_append_msg(channel: RelayChannelDict, direction: str, from_: str, to: str, text: str) -> RelayMessageDict:
    msg_id = f"msg_{secrets.token_urlsafe(4)}"
    relay_msg = cast(RelayMessageDict, {"message_id": msg_id, "direction": direction, "from": from_,
                     "to": to, "text": text, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time()))})
    with relay_store.lock: channel["messages"].append(relay_msg); _relay_save()
    return relay_msg
def relay_guest_send(channel_id: str, text: str) -> tuple[str | None, RelayMessageDict | None]:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel: return None, None
    relay_msg = _relay_append_msg(channel, "guest_to_worker", channel["label"], channel["worker"], text)
    base = f"{_relay_base_url()}/relay/{channel_id}"; rt = channel["reply_token"]
    envelope = (f"[RELAY from {channel['label']}]\nchannel: {channel_id}\nmessage_id: {relay_msg['message_id']}\n"
                f"reply: curl -fsS {base}/reply -H 'Authorization: Bearer {rt}' "
                f"-H 'Content-Type: application/json' -d '{{\"text\":\"YOUR_REPLY\"}}'\n\n{text}\n[/RELAY]")
    return envelope, relay_msg
def relay_worker_reply(channel_id: str, text: str) -> RelayMessageDict | None:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel: return None
    return _relay_append_msg(channel, "worker_to_guest", channel["worker"], channel["label"], text)
def relay_get_messages(channel_id: str, after: str | None = None) -> list[RelayMessageDict]:
    with relay_store.lock: channel = relay_store.channels.get(channel_id)
    if not channel: return []
    msgs = channel["messages"]
    if not after: return list(msgs)
    for i, m in enumerate(msgs):
        if m["message_id"] == after: return list(msgs[i + 1:])
    return list(msgs)
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
def _load_learning_reminder_state() -> None:
    path = _learning_reminder_state_file()
    if not path or not os.path.exists(path): return
    try:
        with open(path) as f: data = json.load(f)
        if isinstance(data, dict):
            with learning_reminders.lock:
                for name, st in data.items():
                    if isinstance(st, dict) and "response_count" in st: learning_reminders.state[name] = cast(ReminderState, st)
            _log(_LOG_INFO, "worker", f"Learning reminder state loaded: {len(data)} workers")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_ERROR, "bridge", f"Learning reminder state load error: {e}")
media_groups = MediaGroupState()
@dataclass
class RewindToken:
    name: str
    expires_at: float
    def is_expired(self) -> bool:
        return _clock.time() >= self.expires_at
    def to_dict(self) -> RewindTokenEntry:
        return {"name": self.name, "expires_at": self.expires_at}
    @classmethod
    def from_dict(cls: type["RewindToken"], d: RewindTokenEntry) -> "RewindToken":
        return cls(name=d["name"], expires_at=d["expires_at"])
@dataclass
class PrReviewToken:
    pr_num: int
    owner: str
    repo: str
    expires_at: float
    def is_expired(self) -> bool:
        return _clock.time() >= self.expires_at
    def to_dict(self) -> PrReviewTokenEntry:
        return {"pr_num": self.pr_num, "owner": self.owner,
                "repo": self.repo, "expires_at": self.expires_at}
    @classmethod
    def from_dict(cls: type["PrReviewToken"], d: PrReviewTokenEntry) -> "PrReviewToken":
        return cls(pr_num=d["pr_num"], owner=d["owner"], repo=d["repo"], expires_at=d["expires_at"])
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
            if token not in PR_REVIEW_TOKENS: return None
            entry = PR_REVIEW_TOKENS[token]
            if entry.get("expires_at", 0) <= now: return None
            if extend: entry["expires_at"] = now + PR_REVIEW_EXTEND
            return entry
tokens = TokenStore()
def _watchdog_alert(category: str, text: str | None) -> None:
    if not text or not admin_chat_id: return
    try:
        transport.send_text(admin_chat_id, text)
        _log(_LOG_WARN, "watchdog", f"{category}: {text.splitlines()[0]}")
    except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_ERROR, "watchdog", f"{category} error: {e}")
def _record_host_probe(host: str, ok: bool, error: str | None = None) -> None:
    now = _clock.time()
    with watchdog.lock:
        was_down = host_health.down.get(host, False)
        if ok:
            host_health.ssh_failures[host] = 0; host_health.last_error.pop(host, None)
            if was_down:
                host_health.down[host] = False
                down_since = host_health.down_since.pop(host, now); duration = int(now - down_since)
                workers_on_host = [n for n, s in get_registered_sessions().items() if get_worker_host(n) == host]
                _do_send = True; alert_text = f"✅ Host BACK UP: {host}\nWas down for {duration // 60}m {duration % 60}s\nWorkers affected: {', '.join(workers_on_host) or 'none'}"
            else: _do_send = False; alert_text = None
        else:
            failures = host_health.ssh_failures.get(host, 0) + 1
            host_health.ssh_failures[host] = failures; host_health.last_error[host] = error or "ssh probe failed"
            if not was_down and failures >= HOST_DOWN_THRESHOLD:
                host_health.down[host] = True; host_health.down_since[host] = now
                workers_on_host = [n for n, s in get_registered_sessions().items() if get_worker_host(n) == host]
                _do_send = True; alert_text = f"🔴 Host DOWN: {host}\nAfter {failures} consecutive SSH failures\nError: {error or 'unknown'}\nWorkers affected: {', '.join(workers_on_host) or 'none'}"
            else: _do_send = False; alert_text = None
    if _do_send: _watchdog_alert("Host", alert_text)
def _is_host_down(host: str) -> bool:
    with watchdog.lock: return host_health.down.get(host, False)
def _check_disk_usage(host: str | None = None) -> DiskUsageDict | None:
    is_mac = bool(host and "mac" in host.lower())
    try:
        cmd = ["df", "-g", "/"] if is_mac else ["df", "-BG", "--output=size,used,avail,pcent", "/"]
        r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2: return None
        parts = lines[1].split()
        if is_mac:
            if len(parts) < 6: return None
            return {"pct": int(parts[4].rstrip("%")), "free_gb": float(parts[3]), "total_gb": float(parts[1])}
        if len(parts) < 4: return None
        return {"pct": int(parts[3].rstrip("%")), "free_gb": float(parts[2].rstrip("G")), "total_gb": float(parts[0].rstrip("G"))}
    except (ValueError, KeyError): return None
def _check_mem_usage(host: str | None = None) -> MemUsageDict | None:
    is_mac = bool(host and "mac" in host.lower())
    try:
        if is_mac:
            r = _remote_run(["bash", "-c", "sysctl -n hw.memsize && vm_stat"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            lines = r.stdout.strip().splitlines()
            if len(lines) < 2: return None
            total = int(lines[0]); page_size = 16384; free_p = inactive_p = spec_p = 0
            for line in lines[1:]:
                if "page size of" in line:
                    try: page_size = int(line.split("page size of")[1].strip().rstrip("."))
                    except (ValueError, IndexError): pass
                elif "Pages free:" in line: free_p = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages inactive:" in line: inactive_p = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages speculative:" in line: spec_p = int(line.split(":")[1].strip().rstrip("."))
            avail = (free_p + inactive_p + spec_p) * page_size; used = total - avail
        else:
            r = _remote_run(["free", "-b"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            mem_line = next((l for l in r.stdout.strip().splitlines() if l.startswith("Mem:")), None)
            if not mem_line: return None
            parts = mem_line.split(); total = int(parts[1]); used = int(parts[2])
            avail = int(parts[6]) if len(parts) >= 7 else total - used
        total_gb = total / (1024**3); used_gb = used / (1024**3); avail_gb = avail / (1024**3)
        pct = int((used / total) * 100) if total > 0 else 0
        top_procs = _get_top_mem_procs(host)
        return {"pct": pct, "used_gb": used_gb, "total_gb": total_gb, "avail_gb": avail_gb, "top_procs": top_procs}
    except (ValueError, KeyError): return None
def _get_top_mem_procs(host: str | None = None) -> list[dict[str, object]]:
    try:
        r = _remote_run(["ps", "aux", "--sort=-rss"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return []
        procs = []
        for line in r.stdout.strip().splitlines()[1:6]:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                procs.append(cast(dict[str, object], {"pid": parts[1], "rss_gb": round(int(parts[5]) / (1024 * 1024), 1), "pct": parts[3], "cmd": parts[10][:80]}))
        return procs
    except (ValueError, KeyError): return []
def _check_io_usage(host: str | None = None) -> IoUsageDict | None:
    is_mac = bool(host and "mac" in host.lower())
    if is_mac:
        try:
            r = _remote_run(["iostat", "-c", "2", "-w", "1"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            lines = r.stdout.strip().splitlines()
            if len(lines) < 3: return None
            parts = lines[-1].split()
            return {"iowait_pct": 0, "read_iops": int(float(parts[0])), "write_iops": int(float(parts[1])), "util_pct": 0} if len(parts) >= 6 else None
        except (ValueError, KeyError): return None
    try:
        r = _remote_run(["iostat", "-x", "-d", "1", "2", "-o", "JSON"], host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode == 0 and r.stdout.strip():
            data = cast(dict[str, object], json.loads(r.stdout)); _sysstat = data.get("sysstat", {})
            _hosts = _sysstat.get("hosts", [{}]) if isinstance(_sysstat, dict) else [{}]
            _host0 = _hosts[0] if isinstance(_hosts, list) and _hosts else {}
            stats = _host0.get("statistics", []) if isinstance(_host0, dict) else []
            if isinstance(stats, list) and len(stats) >= 2:
                disks = stats[-1].get("disk", []) if isinstance(stats[-1], dict) else []
                r_iops = sum(d.get("r/s", 0) for d in disks); w_iops = sum(d.get("w/s", 0) for d in disks)
                m_util = max((d.get("util", d.get("%util", 0)) for d in disks), default=0); iowait = 0.0
                r2 = _remote_run(["vmstat", "1", "2"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r2.returncode == 0:
                    vlines = r2.stdout.strip().splitlines()
                    if len(vlines) >= 3:
                        vp = vlines[-1].split()
                        if len(vp) >= 16: iowait = float(vp[15])
                return {"iowait_pct": round(iowait, 1), "read_iops": round(r_iops), "write_iops": round(w_iops), "util_pct": round(m_util, 1)}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        _log(_LOG_DEBUG, "parse:io", f"{type(exc).__name__}: {exc}")
    try:
        r = _remote_run(["vmstat", "1", "2"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 3: return None
        parts = lines[-1].split()
        if len(parts) < 16: return None
        return {"iowait_pct": round(float(parts[15]), 1), "read_iops": int(parts[8]), "write_iops": int(parts[9]), "util_pct": 0}
    except (ValueError, KeyError): return None
def _probe_metric(remote_hosts: set[str], *, check_fn: Callable[..., dict[str, object] | None],
                  usage_store: dict[str, object], alerted_store: dict[str, object],
                  alert_ts_store: dict[str, float], cooldown: int | float,
                  is_critical_fn: Callable[[dict[str, object]], bool],
                  alert_fmt: Callable[[str, dict[str, object]], str],
                  recovery_fmt: Callable[[str, dict[str, object]], str], category: str) -> None:
    """Generic host metric probe: check -> threshold -> alert with cooldown."""
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host): continue
        host_label = host or "VPS"
        usage = check_fn(host)
        if usage is None: continue
        is_crit = is_critical_fn(usage); alert_text = None
        with watchdog.lock:
            usage_store[host_label] = {**usage, "ts": now}
            was_alerted = alerted_store.get(host_label, False)
            if is_crit and not was_alerted:
                if now - alert_ts_store.get(host_label, 0) >= cooldown:
                    alert_text = alert_fmt(host_label, usage)
                    alert_ts_store[host_label] = now; alerted_store[host_label] = True
            elif not is_crit and was_alerted:
                alerted_store[host_label] = False; alert_text = recovery_fmt(host_label, usage)
        _watchdog_alert(category, alert_text)
def _probe_disk_all_hosts(remote_hosts: set[str]) -> None:
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host): continue
        host_label = host or "VPS"
        usage = _check_disk_usage(host)
        if usage is None: continue
        is_critical = usage["pct"] >= DISK_ALERT_THRESHOLD_PCT or usage["free_gb"] < DISK_ALERT_THRESHOLD_GB
        is_warning = usage["pct"] >= DISK_WARN_THRESHOLD_PCT
        level = "critical" if is_critical else ("warning" if is_warning else False)
        alert_text: str | None = None
        with watchdog.lock:
            host_health.disk_usage[host_label] = {**usage, "ts": now}
            prev = host_health.disk_alerted.get(host_label, False)
            if level and level != prev:
                if level == "critical" or not prev:
                    if now - host_health.disk_alert_ts.get(host_label, 0) >= DISK_ALERT_COOLDOWN:
                        emoji = "🔴 Disk space CRITICAL" if level == "critical" else "⚠️ Disk space warning"
                        alert_text = f"{emoji}: {host_label}\nUsage: {usage['pct']}% ({usage['free_gb']:.1f}GB free of {usage['total_gb']:.0f}GB)"
                        if level == "critical": alert_text += "\nAction needed: clean up old files, worktrees, or logs"
                        host_health.disk_alert_ts[host_label] = now; host_health.disk_alerted[host_label] = level
                else: host_health.disk_alerted[host_label] = level
            elif not level and prev:
                host_health.disk_alerted[host_label] = False
                alert_text = f"✅ Disk space recovered: {host_label} — {usage['pct']}% ({usage['free_gb']:.1f}GB free)"
        _watchdog_alert("Disk", alert_text)
def _probe_mem_all_hosts(remote_hosts: set[str]) -> None:
    def _fmt(h: str, u: dict[str, object]) -> str:
        text = f"🧠 Memory critical: {h}\nUsage: {u['pct']}% ({u['avail_gb']:.1f}GB available of {u['total_gb']:.0f}GB)"
        for p in u.get("top_procs", [])[:3]: text += f"\n  {p['pid']} {p['rss_gb']}GB {p['cmd']}"
        return text
    _probe_metric(remote_hosts, check_fn=_check_mem_usage, usage_store=host_health.mem_usage,
        alerted_store=host_health.mem_alerted, alert_ts_store=host_health.mem_alert_ts,
        cooldown=MEM_ALERT_COOLDOWN, is_critical_fn=lambda u: u["pct"] >= MEM_ALERT_THRESHOLD_PCT or u["avail_gb"] < MEM_ALERT_THRESHOLD_GB,
        alert_fmt=_fmt, recovery_fmt=lambda h, u: f"✅ Memory recovered: {h} — {u['pct']}% ({u['avail_gb']:.1f}GB available)", category="Memory")
def _probe_io_all_hosts(remote_hosts: set[str]) -> None:
    _probe_metric(remote_hosts, check_fn=_check_io_usage, usage_store=host_health.io_usage,
        alerted_store=host_health.io_alerted, alert_ts_store=host_health.io_alert_ts,
        cooldown=IO_ALERT_COOLDOWN, is_critical_fn=lambda u: u["iowait_pct"] >= IO_ALERT_IOWAIT_PCT,
        alert_fmt=lambda h, u: f"⚡ IO critical: {h}\nIO wait: {u['iowait_pct']}%\nIOPS: {u['read_iops']}r + {u['write_iops']}w" + (f" | disk util: {u['util_pct']}%" if u["util_pct"] else ""),
        recovery_fmt=lambda h, u: f"✅ IO recovered: {h} — iowait {u['iowait_pct']}%, IOPS {u['read_iops']}r+{u['write_iops']}w", category="IO")
def _get_cpu_hogs(host: str | None = None) -> list[CpuHogEntry]:
    is_mac = bool(host and "mac" in host.lower())
    try:
        cmd = ["ps", "-eo", "pid,pcpu,etime,comm", "-r"] if is_mac else ["ps", "-eo", "pid,pcpu,etime,comm", "--sort=-pcpu"]
        r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return []
        hogs: list[CpuHogEntry] = []
        for line in r.stdout.strip().splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4: continue
            try: pid = int(parts[0]); cpu = float(parts[1])
            except (ValueError, IndexError): continue
            if cpu < CPU_HOG_THRESHOLD_PCT: break
            etime_str = parts[2]; days = 0
            if "-" in etime_str: day_part, etime_str = etime_str.split("-", 1); days = int(day_part)
            tp = etime_str.split(":")
            try:
                mins = days * 1440 + (int(tp[0]) * 60 + int(tp[1]) if len(tp) == 3 else int(tp[0])) if len(tp) >= 2 else None
            except (ValueError, IndexError): mins = None
            if mins is None: continue
            hogs.append({"pid": pid, "cpu": cpu, "etime_min": mins, "cmd": parts[3][:80] if len(parts) > 3 else "?"})
        return hogs
    except (ValueError, KeyError): return []
def _probe_cpu_hogs(remote_hosts: set[str]) -> None:
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host): continue
        host_label = host or "VPS"
        real_hogs = [h for h in _get_cpu_hogs(host) if h["etime_min"] >= CPU_HOG_DURATION_MIN]
        with watchdog.lock: host_health.cpu_hogs[host_label] = real_hogs
        if real_hogs and now - host_health.cpu_hog_alert_ts.get(host_label, 0) >= CPU_HOG_ALERT_COOLDOWN:
            lines = []
            for h in real_hogs[:5]:
                elapsed = f"{h['etime_min'] // 60}h{h['etime_min'] % 60}m" if h["etime_min"] >= 60 else f"{h['etime_min']}m"
                lines.append(f"  PID {h['pid']}: {h['cpu']}% CPU for {elapsed} — {h['cmd']}")
            host_health.cpu_hog_alert_ts[host_label] = now
            _watchdog_alert("CPU hog", f"🔥 Runaway process{'es' if len(real_hogs) > 1 else ''} on {host_label}:\n" + "\n".join(lines))
def _probe_worktree_sizes(remote_hosts: set[str]) -> None:
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host): continue
        host_label = host or "VPS"; is_mac = bool(host and "mac" in host.lower())
        try:
            du_flag = "-sk" if is_mac else "-sb"
            cmd = ["bash", "-c", f"find $HOME -maxdepth 4 -type d -name worktrees 2>/dev/null | while read d; do du {du_flag} \"$d\" 2>/dev/null; done"]
            r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0 or not r.stdout.strip(): continue
            items: list[WorktreeItemDict] = []; total_bytes = 0
            for line in r.stdout.strip().splitlines():
                parts = line.split(None, 1)
                if len(parts) < 2: continue
                try: size_val = int(parts[0])
                except (ValueError, IndexError): continue
                size_bytes = size_val * 1024 if is_mac else size_val; total_bytes += size_bytes
                items.append({"path": parts[1], "size_gb": round(size_bytes / (1024**3), 1)})
            total_gb = total_bytes / (1024**3)
            with watchdog.lock: host_health.worktree_usage[host_label] = {"total_gb": round(total_gb, 1), "items": items, "ts": now}
            was_alerted = host_health.worktree_alerted.get(host_label, False)
            if total_gb >= WORKTREE_ALERT_THRESHOLD_GB and not was_alerted:
                if now - host_health.worktree_alert_ts.get(host_label, 0) >= WORKTREE_ALERT_COOLDOWN:
                    top = sorted(items, key=lambda x: x["size_gb"], reverse=True)[:5]
                    host_health.worktree_alert_ts[host_label] = now; host_health.worktree_alerted[host_label] = True
                    _watchdog_alert("Worktree", f"📁 Worktree bloat on {host_label}: {total_gb:.1f}GB total (threshold: {WORKTREE_ALERT_THRESHOLD_GB}GB)\nTop directories:\n" + "\n".join(f"  {it['size_gb']}GB — {it['path']}" for it in top))
            elif total_gb < WORKTREE_ALERT_THRESHOLD_GB and was_alerted:
                host_health.worktree_alerted[host_label] = False
                _watchdog_alert("Worktree", f"✅ Worktree size recovered: {host_label} — {total_gb:.1f}GB (below {WORKTREE_ALERT_THRESHOLD_GB}GB)")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "watchdog", f"Worktree check error for {host_label}: {e}")
def _probe_tailscale() -> None:
    now = _clock.time()
    try:
        r = _subprocess_runner.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        is_up = r.returncode == 0 and json.loads(r.stdout).get("BackendState") == "Running"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError): is_up = False
    alert_text = None
    with watchdog.lock:
        if not is_up and not host_health.tailscale_down:
            if now - host_health.tailscale_alert_ts >= INFRA_ALERT_COOLDOWN:
                host_health.tailscale_down = True; host_health.tailscale_alert_ts = now
                alert_text = "🚨 Tailscale is DOWN on VPS — 100.125.36.102 unreachable from external network.\nRun: sudo tailscale up"
        elif is_up and host_health.tailscale_down:
            host_health.tailscale_down = False; alert_text = "✅ Tailscale recovered — VPS reachable at 100.125.36.102"
    _watchdog_alert("Tailscale", alert_text)
def _send_resolved_alert(name: str, new_state: str) -> None:
    if admin_chat_id is None: return
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    with watchdog.lock: prev_state = watchdog.prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states: return
    restart_ts = watchdog.recent_restarts.get(name)
    if restart_ts and _clock.time() - restart_ts < 30: return
    now = _clock.time(); last_resolved = watchdog.last_resolved_ts.get(name, 0)
    if now - last_resolved < 180: return
    watchdog.last_resolved_ts[name] = now
    with watchdog.lock: alert_info = watchdog.alert_msg_ids.pop(name, None)
    if alert_info:
        old_msg_id, old_text = alert_info
        resolved_text = f"✅ {name} resolved (was: {old_text.splitlines()[0]})"
        try:
            transport.edit_message(admin_chat_id, old_msg_id, resolved_text)
            _log(_LOG_WARN, "watchdog", f"Edited alert for {name} -> resolved")
            return
        except (urllib.error.URLError, OSError, TimeoutError): pass
    text = f"✅ {name} is back to normal."
    try:
        transport.send_text(admin_chat_id, text)
    except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_ERROR, "watchdog", f"Watchdog resolved alert error: {e}")
def _handle_watchdog_transition( name: str, state: str, reason: str, since: float, now: float | None = None,
) -> None:
    if now is None: now = _clock.time()
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with watchdog.lock: prev_state = watchdog.prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state
    if state == "HOST_OFFLINE":
        with watchdog.lock: watchdog.prev_worker_states[name] = state
        return
    teleport_state_file = SESSIONS_DIR / name / "teleport_state"
    if state in {"OFFLINE", "DEAD", "EXITED"} and teleport_state_file.exists():
        with watchdog.lock: watchdog.prev_worker_states[name] = state
        return
    def eligible_for_alert() -> bool:
        if state in {"OFFLINE", "DEAD", "EXITED"}: return since is not None and (now - since) >= START_GRACE
        return True
    GOOD_PROBE_THRESHOLD = 3; BAD_PROBE_THRESHOLD = 3; is_remote = bool(get_worker_host(name))
    if state in bad_states:
        with watchdog.lock: watchdog.consecutive_good_probes[name] = 0
        if is_remote and state in {"OFFLINE", "DEAD"}:
            with watchdog.lock:
                watchdog.consecutive_bad_probes[name] = watchdog.consecutive_bad_probes.get(name, 0) + 1
                bad_count = watchdog.consecutive_bad_probes[name]
            if bad_count < BAD_PROBE_THRESHOLD: return
        if state_changed or prev_state is None:
            if eligible_for_alert():
                _log(_LOG_WARN, "watchdog", f"State change {name}: {prev_state} -> {state} ({reason}), sending alert")
                _send_watchdog_alert(name, state, reason)
        elif state in {"OFFLINE", "DEAD", "EXITED"} and eligible_for_alert(): _send_watchdog_alert(name, state, reason)
        with watchdog.lock: watchdog.prev_worker_states[name] = state
        return
    if state in good_states and prev_state in bad_states:
        with watchdog.lock:
            watchdog.consecutive_good_probes[name] = watchdog.consecutive_good_probes.get(name, 0) + 1
            watchdog.consecutive_bad_probes[name] = 0
            good_count = watchdog.consecutive_good_probes[name]
        if good_count >= GOOD_PROBE_THRESHOLD:
            _send_resolved_alert(name, state)
            with watchdog.lock:
                watchdog.consecutive_good_probes[name] = 0
                watchdog.prev_worker_states[name] = state
        return
    with watchdog.lock:
        watchdog.consecutive_good_probes[name] = 0
        watchdog.consecutive_bad_probes[name] = 0
        watchdog.prev_worker_states[name] = state
def _record_worker_state(name: str, state: str, reason: str, now: float) -> float:
    with watchdog.lock:
        prev = watchdog.worker_states.get(name)
        if prev and prev[0] == state: since = prev[2]
        else: since = now
        watchdog.worker_states[name] = WorkerStateEntry(state, reason, since)
    return since
def watchdog_loop() -> None:
    _disk_check_counter = 0
    while not watchdog.stop_event.is_set():
        try:
            now = _clock.time(); registered = get_registered_sessions(); pane_pids = _tmux_pane_pids()
            registered_names = set(registered.keys()); probe_failed = bool(registered_names) and not pane_pids
            _watchdog_update_probe_failures(registered_names, probe_failed)
            remote_workers, remote_pane_pids, failed_hosts = _watchdog_probe_remote_hosts(registered)
            claude_pids, tmux_present, backend_info = _watchdog_collect_worker_pids(
                registered, pane_pids, remote_pane_pids, now)
            stats = _watchdog_gather_cpu_stats(claude_pids)
            _watchdog_evaluate_workers(
                registered, tmux_present, claude_pids, backend_info, stats,
                probe_failed, failed_hosts, now)
            _watchdog_cleanup_stale(registered_names)
            _disk_check_counter += 1
            if _disk_check_counter >= 20:
                _disk_check_counter = 0
                _watchdog_resource_checks(set(remote_workers.keys()))
        except (subprocess.SubprocessError, ValueError, KeyError) as e: _log(_LOG_ERROR, "watchdog", f"Watchdog error: {e}")
        watchdog.stop_event.wait(WATCHDOG_INTERVAL)
def _watchdog_update_probe_failures(registered_names: set[str], probe_failed: bool) -> None:
    if probe_failed:
        for name in registered_names: watchdog.consecutive_probe_failures[name] = watchdog.consecutive_probe_failures.get(name, 0) + 1
    else:
        for name in registered_names: watchdog.consecutive_probe_failures[name] = 0
def _watchdog_probe_remote_hosts( registered: dict[str, TmuxSessionDict]
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str], set[str]]:
    remote_workers: dict[str, list[tuple[str, str]]] = {}
    for name, session in registered.items():
        host = get_worker_host(name)
        if host:
            tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
            remote_workers.setdefault(host, []).append((name, tmux_name))
    remote_pane_pids: dict[str, str] = {}
    failed_hosts: set[str] = set()
    for host, workers in remote_workers.items():
        try:
            r = _remote_run(
                ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1].isdigit(): remote_pane_pids[parts[0]] = parts[1]
                _record_host_probe(host, ok=True)
            else:
                failed_hosts.add(host)
                _record_host_probe(host, ok=False, error=f"tmux list-panes exit {r.returncode}")
        except (subprocess.SubprocessError, KeyError) as e:
            failed_hosts.add(host)
            _record_host_probe(host, ok=False, error=str(e)[:200])
    return remote_workers, remote_pane_pids, failed_hosts
def _watchdog_collect_worker_pids(
    registered: dict[str, TmuxSessionDict],
    pane_pids: dict[str, str],
    remote_pane_pids: dict[str, str],
    now: float
) -> tuple[dict[str, str], dict[str, bool], dict[str, Backend]]:
    claude_pids: dict[str, str] = {}
    tmux_present: dict[str, bool] = {}
    backend_info: dict[str, Backend] = {}
    for name, session in registered.items():
        backend_name = get_worker_backend(name, session); backend = get_backend(backend_name)
        backend_info[name] = backend
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}"); host = get_worker_host(name)
        pane_pid = remote_pane_pids.get(tmux_name) if host else pane_pids.get(tmux_name); tmux_exists = bool(pane_pid)
        tmux_present[name] = tmux_exists
        if not tmux_exists: continue
        if backend.is_interactive and pane_pid:
            claude_pid = _get_claude_pid(pane_pid, host=host)
            if claude_pid:
                claude_pids[name] = claude_pid
                with watchdog.lock: watchdog.last_seen_claude[name] = now
            else:
                with watchdog.lock:
                    if name not in watchdog.last_seen_claude: watchdog.last_seen_claude[name] = now
    return claude_pids, tmux_present, backend_info
def _watchdog_gather_cpu_stats(claude_pids: dict[str, str]) -> dict[str, ProcStatsEntry]:
    pids_by_host: dict[str | None, list[str]] = {}
    for name, pid in claude_pids.items():
        host = get_worker_host(name)
        pids_by_host.setdefault(host, []).append(pid)
    stats: dict[str, ProcStatsEntry] = {}
    for host, pids in pids_by_host.items(): stats.update(_ps_stats(pids, host=host))
    return stats
def _watchdog_evaluate_workers(
    registered: dict[str, TmuxSessionDict],
    tmux_present: dict[str, bool],
    claude_pids: dict[str, str],
    backend_info: dict[str, Backend],
    stats: dict[str, ProcStatsEntry],
    probe_failed: bool,
    failed_hosts: set[str],
    now: float
) -> None:
    for name, session in registered.items():
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}"); tmux_exists = tmux_present.get(name, False)
        if not tmux_exists and "tmux" not in session:
            since = _record_worker_state(name, "EXITED", "session gone", now)
            _handle_watchdog_transition(name, "EXITED", "session gone", since, now=now)
            continue
        if probe_failed and not tmux_exists and watchdog.consecutive_probe_failures.get(name, 0) < 3: continue
        host = get_worker_host(name)
        if host and _is_host_down(host):
            reason = f"host {host} offline"; since = _record_worker_state(name, "HOST_OFFLINE", reason, now)
            _handle_watchdog_transition(name, "HOST_OFFLINE", reason, since, now=now)
            continue
        if host and host in failed_hosts and not tmux_exists: continue
        backend = backend_info.get(name)
        if backend is None: backend_name = get_worker_backend(name, session); backend = get_backend(backend_name)
        is_interactive = backend.is_interactive; adapter_alive = False
        if not is_interactive:
            with processes.adapter_pids_lock: entry = processes.adapter_pids.get(name)
            if entry:
                proc, _stderr = entry
                adapter_alive = proc.poll() is None
        host = get_worker_host(name); claude_pid = claude_pids.get(name) if is_interactive else None; cpu = 0.0
        if claude_pid and claude_pid in stats: cpu = stats[claude_pid].get("cpu", 0.0)
        children_total = _child_count(claude_pid, host=host) if claude_pid else 0
        children = _watchdog_compute_children(name, children_total, is_interactive, claude_pid, now)
        if children > 0:
            with watchdog.lock: watchdog.last_child_ts[name] = now
        _watchdog_track_activity(name, children, cpu, now)
        pending_ts = _pending_timestamp(name); pending = pending_ts is not None
        with watchdog.lock: last_activity = watchdog.last_activity_ts.get(name, 0.0)
        if pending_ts:
            effective_start = max(pending_ts, last_activity) if last_activity > pending_ts else pending_ts
            pending_age = now - effective_start
        else: pending_age = 0.0
        with watchdog.lock:
            last_child_ts = watchdog.last_child_ts.get(name, 0.0); last_hook_ts = watchdog.last_hook_ts.get(name)
            last_seen_claude = watchdog.last_seen_claude.get(name)
        if not is_interactive: last_seen_claude = None
        state_kw: dict[str, object] = dict(tmux_exists=tmux_exists, claude_pid=claude_pid,
            pending=pending, pending_ts=pending_ts, pending_age=pending_age, children=children,
            last_child_ts=last_child_ts, cpu=cpu, last_hook_ts=last_hook_ts,
            last_seen_claude=last_seen_claude, now=now, is_interactive=is_interactive, adapter_alive=adapter_alive)
        worker_state, reason = compute_state(**state_kw)  # type: ignore[arg-type]
        worker_state, reason = _watchdog_refine_state(
            name, tmux_name, worker_state, reason, state_kw, host)
        since = _record_worker_state(name, worker_state, reason, now)
        _handle_watchdog_transition(name, worker_state, reason, since, now=now)
def _watchdog_compute_children(name: str, children_total: int,
                                is_interactive: bool, claude_pid: str | None,
                                now: float) -> int:
    pending_ts = _pending_timestamp(name); pending = pending_ts is not None
    if is_interactive and claude_pid:
        with watchdog.lock:
            baseline = watchdog.idle_child_baseline.get(name)
            if baseline is None:
                watchdog.idle_child_baseline[name] = children_total
                baseline = children_total
            elif not pending:
                baseline = min(baseline, children_total)
                watchdog.idle_child_baseline[name] = baseline
        return max(0, children_total - baseline)
    return children_total
def _watchdog_track_activity(name: str, children: int, cpu: float, now: float) -> None:
    with watchdog.lock:
        prev_children = watchdog.prev_children.get(name)
        activity_increased = (prev_children is not None and children > prev_children)
        if activity_increased or cpu >= CPU_ACTIVE: watchdog.last_activity_ts[name] = now
        watchdog.prev_children[name] = children
def _watchdog_refine_state(
    name: str, tmux_name: str,
    worker_state: str, reason: str,
    state_kw: dict[str, object],
    host: str | None,
) -> tuple[str, str]:
    is_interactive = bool(state_kw.get("is_interactive", True)); pending = bool(state_kw.get("pending"))
    pending_age = float(state_kw.get("pending_age", 0.0)); now = float(state_kw.get("now", 0.0))  # type: ignore[arg-type]
    if worker_state == "STUCK":
        watchdog.idle_streak[name] = watchdog.idle_streak.get(name, 0) + 1
        streak = watchdog.idle_streak[name]
        if streak < IDLE_STREAK_STUCK: worker_state = "WAITING"
        else:
            if is_interactive and pending:
                pane_text = _capture_pane_text(tmux_name, lines=15, host=host)
                if pane_text:
                    activity = _extract_activity(pane_text.splitlines())
                    if activity == "Idle at prompt":
                        _log(_LOG_WARN, "watchdog", f"Auto-clearing stale pending for {name} (idle at prompt, age={int(pending_age)}s)")
                        clear_pending(name)
                        watchdog.idle_streak[name] = 0
                        since = _record_worker_state(name, "READY", "idle (auto-cleared stale pending)", now)
                        _handle_watchdog_transition(name, "READY", "idle (auto-cleared stale pending)", since, now=now)
                        return "READY", "idle (auto-cleared stale pending)"
            poisoned_reason = _detect_poisoned(name, tmux_name)
            worker_state, reason = compute_state(**state_kw, poisoned_reason=poisoned_reason)  # type: ignore[arg-type]
        reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    elif worker_state == "POISONED":
        streak = watchdog.idle_streak.get(name, 0)
        if streak: reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    else: watchdog.idle_streak[name] = 0
    if worker_state == "READY" and is_interactive:
        pane_text = _capture_pane_text(tmux_name, lines=30, host=host)
        if pane_text:
            pane_lines = pane_text.splitlines(); details = _extract_question_details(pane_lines)
            if details:
                with watchdog.lock: watchdog.waiting_input_details[name] = details
                worker_state = "WAITING_INPUT"; header = details.get("header", "")
                reason = f"question={header}" if header else "interactive prompt"
    return worker_state, reason
def _watchdog_cleanup_stale(registered_names: set[str]) -> None:
    with watchdog.lock:
        stale_dicts: list[dict[str, object]] = cast(list[dict[str, object]], [
            watchdog.worker_states, watchdog.last_child_ts,
            watchdog.last_seen_claude, watchdog.last_hook_ts,
            watchdog.prev_worker_states, watchdog.last_alert_ts,
            watchdog.idle_streak, watchdog.idle_child_baseline,
            watchdog.prev_children, watchdog.last_activity_ts,
        ])
        for d in stale_dicts:
            for name in list(d.keys()):
                if name not in registered_names: d.pop(name, None)
    for name in list(watchdog.consecutive_probe_failures.keys()):
        if name not in registered_names: watchdog.consecutive_probe_failures.pop(name, None)
def _watchdog_resource_checks(remote_hosts: set[str]) -> None:
    checks: list[tuple[str, Callable[[], None]]] = [
        ("Disk", lambda: _probe_disk_all_hosts(remote_hosts)),
        ("Memory", lambda: _probe_mem_all_hosts(remote_hosts)),
        ("IO", lambda: _probe_io_all_hosts(remote_hosts)),
        ("CPU hog", lambda: _probe_cpu_hogs(remote_hosts)),
        ("Worktree", lambda: _probe_worktree_sizes(remote_hosts)),
        ("Tailscale", lambda: _probe_tailscale()), ]
    for label, check_fn in checks:
        try: check_fn()
        except (subprocess.SubprocessError, OSError) as e: _log(_LOG_ERROR, "watchdog", f"{label} check error: {e}")
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
        prev_msg_id = _send_html_fallback_chunks(
            name, rich_chunks[rich_failed_at:], chat_id, log_prefix,
            prev_msg_id, rich_failed_at, len(rich_chunks))
        rich_sent = True
    if not rich_sent: _send_text_as_html(name, clean_text, chat_id, log_prefix)
def _send_html_fallback_chunks(
    name: str, remaining_chunks: list[str], chat_id: int,
    log_prefix: str, prev_msg_id: int | None,
    start_index: int, total_chunks: int
) -> int | None:
    remaining_md = '\n'.join(remaining_chunks); remaining_html = markdown_to_telegram_html(remaining_md)
    prefix_reserve = len(name) + 30; chunks = split_message(remaining_html, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)
    for i, part in enumerate(formatted_parts):
        result = transport.send_text( chat_id, part, parse_mode="HTML", reply_to=prev_msg_id if prev_msg_id else None )
        if result and result.get("ok"):
            _rr2 = result.get("result", {}); prev_msg_id = _rr2.get("message_id") if isinstance(_rr2, dict) else None
            _log(_LOG_INFO, "telegram", f"{log_prefix} sent (html fallback): {name} part {start_index + i + 1}/{total_chunks} -> Telegram OK")
        else:
            plain_text = re.sub(r'<[^>]+>', '', part)
            plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            transport.send_text(chat_id, plain_text, reply_to=prev_msg_id if prev_msg_id else None)
        if i < len(formatted_parts) - 1: _clock.sleep(DELAY_BRIEF)
    return prev_msg_id
def _send_text_as_html(name: str, clean_text: str, chat_id: int, log_prefix: str) -> None:
    html_text = markdown_to_telegram_html(clean_text); prefix_reserve = len(name) + 30
    chunks = split_message(html_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)
    prev_msg_id: int | None = None
    for i, part in enumerate(formatted_parts):
        result = transport.send_text( chat_id, part, parse_mode="HTML", reply_to=prev_msg_id if prev_msg_id else None )
        if result and result.get("ok"):
            _rr3 = result.get("result", {}); prev_msg_id = _rr3.get("message_id") if isinstance(_rr3, dict) else None
            if len(formatted_parts) > 1:
                _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} part {i+1}/{len(formatted_parts)} -> Telegram OK")
            else: _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} -> Telegram OK")
        else:
            desc = str((result or {}).get("description", "")); error_code = int((result or {}).get("error_code", 0))
            if error_code == 400:
                _log(_LOG_WARN, "bridge", f"{log_prefix} HTML send failed (400: {desc}), retrying as plain text")
                plain_text = re.sub(r'<[^>]+>', '', part)
                plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                result = transport.send_text( chat_id, plain_text, reply_to=prev_msg_id if prev_msg_id else None )
                if result and result.get("ok"):
                    prev_msg_id = cast(int | None, _dict_field(result, "result").get("message_id"))
                    _log(_LOG_INFO, "telegram", f"{log_prefix} sent (plain): {name} -> Telegram OK")
                else: _log(_LOG_WARN, "bridge", f"{log_prefix} failed (plain): {name} -> {result}")
            else: _log(_LOG_WARN, "bridge", f"{log_prefix} failed: {name} -> {result}")
        if i < len(formatted_parts) - 1: _clock.sleep(DELAY_BRIEF)
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
class _LegacyTransportProto(Protocol):
    def send_message(self, chat_id: ChatId, text: str, **kwargs: object) -> TelegramApiResponse: ...
class _LegacyTransportAdapter(MessageTransport):
    def __init__(self, legacy: _LegacyTransportProto) -> None: self._legacy: _LegacyTransportProto = legacy
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
def _fanout_channel_message(channel_id: str, from_member: str,
                            text: str, msg: ChannelMessageDict,
                            members_snapshot: dict[str, ChannelMemberDict],
                            registered: dict[str, TmuxSessionDict]) -> None:
    tagged = f"[{channel_id} from {from_member}] {text}"
    for member_key, minfo in members_snapshot.items():
        if member_key == from_member: continue
        if minfo["type"] == "worker":
            wname = minfo.get("name", "")
            if wname and wname in registered:
                winfo = registered[wname]; backend_name = get_worker_backend(wname, winfo)
                backend = get_backend(backend_name)
                try:
                    backend.send(wname, f"{TMUX_PREFIX}{wname}", tagged, f"http://localhost:{PORT}", SESSIONS_DIR)
                except (ConnectionError, TimeoutError) as e: _log(_LOG_WARN, "bridge", f"Channel fan-out to {wname} failed: {e}")
        elif minfo["type"] == "guest":
            gname = minfo.get("name", "")
            if gname:
                with guest_store.lock:
                    ginbox = guest_store.inboxes.get(gname, [])
                    guest_store.inboxes[gname] = guest_inbox_append(ginbox, {
                        "id": msg["id"], "from": from_member,
                        "channel": channel_id, "text": text, "ts": msg["ts"],
                    })
        elif minfo["type"] == "manager": _notify_admin(f"[{channel_id}] {from_member}: {text}")
class CommandRouter:
    def _check_worker_transfer_ready(self, worker_name: str, chat_id: ChatId, action: str) -> str | None:
        """Shared validation for teleport/teleback. Returns error message or None if ready."""
        with watchdog.lock: worker_state = watchdog.worker_states.get(worker_name, ("UNKNOWN", "", 0))
        if worker_state[0] in ("BUSY_TOOL", "BUSY_THINKING"):
            return (f"{worker_name} is busy. Must be idle to {action}.\n"
                    f"Wait for it to finish or /pause {worker_name} first.")
        teleport_file = SESSIONS_DIR / worker_name / "teleport_state"
        if teleport_file.exists(): return f"{worker_name} has a teleport in progress."
        return None
    def _check_remote_host_ready(self, target_host: str, chat_id: ChatId,
                                  tools: tuple[str, ...] = ("claude", "tmux", "rsync")) -> str | None:
        """Check SSH reachability and tool availability. Returns error or None."""
        r = _remote_run(["echo", "ok"], host=target_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0: return f"Cannot reach {target_host} via SSH."
        for tool in tools:
            if _resolve_remote_tool(tool, target_host) == tool:
                return f"{tool} not found on {target_host}. Install it first."
        return None
    def cmd_teleport(self, arg: str, chat_id: ChatId, check_only: bool = False) -> bool:
        if not arg:
            cmd_name = "/teleport-check" if check_only else "/teleport"
            self.reply(chat_id, f"Usage: {cmd_name} <worker> <host>[:/path]")
            return True
        parts = arg.split(); worker_name = parts[0].lower(); target_spec = " ".join(parts[1:]) if len(parts) > 1 else ""
        if not target_spec:
            self.reply(chat_id, "Usage: /teleport <worker> <host>[:/path] [--full]")
            return True
        full_sync = "--full" in target_spec; target_spec = target_spec.replace("--full", "").strip()
        if ":" in target_spec and not target_spec.startswith("/"): target_host, target_cwd = target_spec.split(":", 1)
        else: target_host = target_spec; target_cwd = ""
        machines = get_machine_catalog()
        if target_host in machines:
            machine = machines[target_host]
            if machine.ssh_target: target_host = machine.ssh_target
        registry = _load_registry(); worker_entry = registry.get("workers", {}).get(worker_name)
        if not worker_entry:
            self.reply(chat_id, f"Worker '{worker_name}' not found in registry.")
            return True
        backend_name = worker_entry.get("backend", "claude")
        err = self._check_worker_transfer_ready(worker_name, chat_id, "teleport")
        if err: self.reply(chat_id, err); return True
        required_tools = ("claude", "tmux", "rsync") + ((backend_name,) if backend_name != "claude" else ())
        err = self._check_remote_host_ready(target_host, chat_id, required_tools)
        if err: self.reply(chat_id, err); return True
        tmux_name = f"{self.workers.tmux_prefix}{worker_name}"
        r = _remote_run(
            ["bash", "-c", f"tmux has-session -t {tmux_name} 2>/dev/null && echo exists || echo none"],
            host=target_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode == 0 and "exists" in r.stdout:
            self._teleport_notify(chat_id,
                f"⚠️ tmux session '{tmux_name}' already exists on {target_host} — will be replaced.")
        target_bridge_url = BRIDGE_PUBLIC_URL or BRIDGE_URL
        if "localhost" in target_bridge_url or "127.0.0.1" in target_bridge_url:
            self.reply(chat_id,
                "Cannot teleport: no reachable bridge URL. "
                "Set BRIDGE_PUBLIC_URL to this machine's network IP "
                "(e.g., BRIDGE_PUBLIC_URL=http://100.125.36.102:8271).")
            return True
        r = _remote_run(["curl", "-sf", "--connect-timeout", "5",
                         f"{target_bridge_url}/"],
                        host=target_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            self.reply(chat_id,
                f"Target {target_host} cannot reach {target_bridge_url}. "
                f"Ensure BRIDGE_BIND=0.0.0.0 and network connectivity.")
            return True
        r = _remote_run(["test", "-f", ".claude/.credentials.json"],
                        host=target_host, capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            local_creds = os.path.expanduser("~/.claude/.credentials.json")
            if os.path.exists(local_creds):
                _remote_run(["mkdir", "-p", ".claude"], host=target_host, capture_output=True)
                _subprocess_runner.run(
                    ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json"],
                    capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host, capture_output=True)
                self._teleport_notify(chat_id, "Synced credentials to target.")
            else:
                self.reply(chat_id,
                    f"No Claude credentials on {target_host} or locally. "
                    f"Run: ssh {target_host} claude login")
                return True
        r = _remote_run(["test", "-f", ".claude/hooks/claudecode.sh"],
                        host=target_host, capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: self._teleport_notify(chat_id, "Hooks missing on target — will install during teleport.")
        preflight_fails = self._run_teleport_preflight( target_host, worker_name, backend_name)
        if preflight_fails:
            self.reply(chat_id, f"Preflight failed:\n" + "\n".join(f" - {f}" for f in preflight_fails))
            return True
        if check_only:
            self.reply(chat_id, f"Preflight OK — {worker_name} is clear to teleport to {target_host}.")
            return True
        self.reply(chat_id, f"Teleporting {worker_name} to {target_host}...")
        threading.Thread(
            target=self._do_teleport,
            args=(worker_name, target_host, target_cwd, full_sync, chat_id),
            name=f"teleport-{worker_name}",
            daemon=True
        ).start()
        return True
    def cmd_teleback(self, arg: str, chat_id: ChatId) -> bool:
        parts = arg.split(); worker_name = parts[0].lower() if parts else ""; full_sync = "--full" in parts
        if not worker_name:
            self.reply(chat_id, "Usage: /teleback <worker> [--full]")
            return True
        registry = _load_registry(); worker = registry.get("workers", {}).get(worker_name)
        if not worker:
            self.reply(chat_id, f"Worker '{worker_name}' not in registry.")
            return True
        current_host = worker.get("host"); home_host = worker.get("home_host"); home_cwd = worker.get("home_cwd")
        if current_host is None and home_cwd is None:
            self.reply(chat_id, f"{worker_name} hasn't been teleported.")
            return True
        err = self._check_worker_transfer_ready(worker_name, chat_id, "teleback")
        if err: self.reply(chat_id, err); return True
        target_host = home_host; target_cwd = home_cwd or get_claude_session_cwd(worker_name)
        if target_host:
            err = self._check_remote_host_ready(target_host, chat_id)
            if err: self.reply(chat_id, err); return True
        if current_host:
            r = _remote_run(["echo", "ok"], host=current_host,
                            capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode != 0:
                self.reply(chat_id, f"Cannot reach {current_host} where {worker_name} currently is.")
                return True
        if not full_sync:
            conflicts = self._check_teleback_conflicts( worker_name, current_host, target_cwd)
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
            name=f"teleback-{worker_name}",
            daemon=True
        ).start()
        return True
    def _check_teleback_conflicts(self, name: str, remote_host: str | None, local_cwd: str | None) -> list[str]:
        conflicts: list[str] = []
        if not local_cwd or not remote_host: return conflicts
        local_is_git = os.path.isdir(os.path.join(local_cwd, ".git"))
        if not local_is_git: return conflicts
        local_status = _subprocess_runner.run(
            ["git", "-C", local_cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        local_changed = bool(local_status.stdout.strip()) if local_status.returncode == 0 else False
        local_head = _subprocess_runner.run(
            ["git", "-C", local_cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        local_commit = local_head.stdout.strip() if local_head.returncode == 0 else ""
        remote_cwd = _remap_path(local_cwd, remote_host)
        r_status = _remote_run(
            ["git", "-C", remote_cwd, "status", "--porcelain"],
            host=remote_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        remote_changed = bool(r_status.stdout.strip()) if r_status.returncode == 0 else False
        r_head = _remote_run(
            ["git", "-C", remote_cwd, "rev-parse", "HEAD"],
            host=remote_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        remote_commit = r_head.stdout.strip() if r_head.returncode == 0 else ""
        if local_changed and remote_changed:
            local_files = [l.strip().split(None, 1)[-1]
                          for l in local_status.stdout.strip().splitlines()[:5]]
            remote_files = [l.strip().split(None, 1)[-1]
                           for l in r_status.stdout.strip().splitlines()[:5]]
            conflicts.append( f"VPS has uncommitted changes: {', '.join(local_files)}")
            conflicts.append(
                f"Remote has uncommitted changes: {', '.join(remote_files)}")
        elif local_commit and remote_commit and local_commit != remote_commit:
            if local_changed: conflicts.append( f"VPS has uncommitted changes AND different commit than remote")
            elif remote_changed:
                conflicts.append( f"VPS has new commits since teleport (HEAD: {local_commit[:8]})")
                conflicts.append( f"Remote has uncommitted changes (HEAD: {remote_commit[:8]})")
        return conflicts
    def _do_teleport(self, name: str, target_host: str, target_cwd: str, full_sync: bool,
                     chat_id: int | str, is_teleback: bool=False) -> None:
        try:
            registered = self.workers.get_registered_sessions(); session = registered.get(name, {})
            tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}"); backend_name = get_worker_backend(name, session)
            source_host = get_worker_host(name); source_cwd = get_claude_session_cwd(name) or ""
            _log(_LOG_INFO, "teleport", f"{name}: source_host={source_host}, source_cwd={source_cwd}, target_host={target_host}, target_cwd={target_cwd}")
            if not target_cwd: target_cwd = _remap_path(source_cwd, target_host) if target_host else source_cwd
            if target_cwd and target_cwd.startswith("~"):
                if target_host:
                    rh = _get_remote_home(target_host)
                    if rh: target_cwd = rh + target_cwd[1:]
                else: target_cwd = os.path.expanduser(target_cwd)
            ensure_session_dir(name)
            state_file = SESSIONS_DIR / name / "teleport_state"; _tmp_ts = state_file.with_suffix('.tmp')
            _tmp_ts.write_text(json.dumps({"phase": 1, "source_host": source_host, "target_host": target_host,
                "target_cwd": target_cwd, "started_at": int(_clock.time())}))
            os.replace(str(_tmp_ts), str(state_file))
            self._teleport_notify(chat_id, f"Stopping {name}...")
            session_id = self._stop_worker_for_teleport(name, tmux_name, source_host)
            _log(_LOG_INFO, "teleport", f"{name}: stopped, session_id={session_id}")
            if source_cwd and target_cwd:
                self._teleport_notify(chat_id, f"Syncing working directory...")
                _log(_LOG_INFO, "teleport", f"{name}: syncing {source_cwd} → {target_cwd}")
                ok = self._sync_working_directory( source_cwd, target_cwd, source_host, target_host, full_sync)
                _log(_LOG_INFO, "teleport", f"{name}: working dir sync ok={ok}")
                if not ok:
                    self._teleport_rollback(name, tmux_name, source_host, source_cwd,
                                            session_id, backend_name, chat_id,
                                            "working directory sync failed")
                    return
            if session_id:
                self._teleport_notify(chat_id, "Syncing session transcript...")
                self._sync_session_transcript( session_id, source_cwd, target_cwd, source_host, target_host)
                _log(_LOG_INFO, "teleport", f"{name}: transcript sync done")
            if not is_teleback:
                self._teleport_notify(chat_id, "Syncing team config and hooks...")
                sync_warnings = self._sync_shared_repos(target_host, chat_id)
                hook_warnings = self._install_hooks_on_target(target_host); all_warnings = sync_warnings + hook_warnings
                if all_warnings:
                    self._teleport_notify(
                        chat_id,
                        f"Config sync completed with warnings:\n" +
                        "\n".join(f"- {w}" for w in all_warnings))
                _log(_LOG_WARN, "teleport", f"{name}: team config + hooks synced ({len(all_warnings)} warnings)")
            _tmp_ts2 = state_file.with_suffix('.tmp')
            _tmp_ts2.write_text(json.dumps({"phase": 2, "source_host": source_host, "target_host": target_host,
                "target_cwd": target_cwd, "started_at": int(_clock.time())}))
            os.replace(str(_tmp_ts2), str(state_file))
            save_claude_session_cwd(name, target_cwd)
            clear_claude_session_id(name)
            _ensure_workspace_trusted_remote(target_cwd, target_host)
            self._teleport_notify(chat_id, f"Starting {name} on {target_host or 'local'}...")
            resume_id = session_id
            _log(_LOG_INFO, "teleport", f"{name}: calling _start_worker_on_target(target_cwd={target_cwd}, session_id={resume_id}, backend={backend_name})")
            ok = self._start_worker_on_target( name, target_host, target_cwd, resume_id, backend_name)
            _log(_LOG_INFO, "teleport", f"{name}: _start_worker_on_target returned {ok}")
            if not ok:
                _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host, capture_output=True)
                self._teleport_rollback(name, tmux_name, source_host, source_cwd,
                                        session_id, backend_name, chat_id,
                                        "failed to start on target")
                return
            if is_teleback: _registry_clear_teleport(name)
            else: _registry_update_teleport( name, host=target_host, home_host=source_host, home_cwd=source_cwd)
            _remote_run(["tmux", "kill-session", "-t", tmux_name],
                        host=source_host, capture_output=True)
            if is_teleback: self._sync_worker_data_back(name, source_host)
            if not is_teleback:
                try:
                    backend_obj = get_backend(backend_name); welcome = self.workers._build_welcome(name, backend_obj)
                    _clock.sleep(DELAY_PROCESS_SETTLE)
                    self.workers.send(name, welcome)
                    if source_host != target_host:
                        ctx = _build_teleport_context(
                            name=name,
                            source_host=source_host,
                            target_host=target_host,
                            source_cwd=source_cwd,
                            session_id=session_id, )
                        _clock.sleep(0.5)
                        self.workers.send(name, ctx)
                except (ConnectionError, TimeoutError, AttributeError, OSError) as e:
                    _log(_LOG_WARN, "teleport", f"Warning: failed to send welcome to {name}: {e}")
            state_file.unlink(missing_ok=True)
            dest_label = target_host or "local"; action = "teleported back" if is_teleback else "teleported"
            msg = f"{name} {action} to {dest_label}:{target_cwd}"
            if session_id: msg += f"\nSession resumed ({session_id[:8]}...)."
            if not is_teleback: msg += f"\nUse /teleback {name} to bring it back."
            self._teleport_notify(chat_id, msg)
        except (subprocess.SubprocessError, ConnectionError, TimeoutError, TypeError, AttributeError, OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "teleport", f"Teleport failed: {e}", exc=e)
            self._teleport_notify(chat_id, f"Teleport failed: {e}")
            try:
                state_file = SESSIONS_DIR / name / "teleport_state"
                state_file.unlink(missing_ok=True)
            except OSError as exc: _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
    def _stop_worker_for_teleport(self, name: str, tmux_name: str, host: str | None=None) -> str | None:
        session_id = get_claude_session_id(name, authoritative=True)
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"],
                     host=host, capture_output=True)
        for _ in range(20):
            _clock.sleep(DELAY_RETRY)
            r = _remote_run(
                ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
                host=host, capture_output=True, text=True)
            if r.returncode != 0: break
            pane_pid = r.stdout.strip()
            if pane_pid:
                claude_pid = _get_claude_pid(pane_pid, host=host)
                if not claude_pid: break
        else:
            _remote_run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], host=host, capture_output=True)
            _clock.sleep(DELAY_STARTUP)
        return get_claude_session_id(name, authoritative=True) or session_id
    def _sync_working_directory(self, source_cwd: str, target_cwd: str,
                                 source_host: str | None=None, target_host: str | None=None,
                                 full: bool=False) -> bool:
        if not full and _is_git_repo(source_cwd, host=source_host):
            project = _get_project_name(source_cwd, host=source_host)
            if project:
                try:
                    bare_repo = _ensure_bare_repo(project)
                    meta = _git_push_state(source_cwd, project, bare_repo, host=source_host)
                    if meta:
                        bare_url = _bare_repo_url(bare_repo, target_host=target_host)
                        if _git_pull_state(target_cwd, project, bare_url, meta,
                                           host=target_host):
                            _log(_LOG_INFO, "teleport", f"git sync succeeded for {project}")
                            return True
                        _log(_LOG_WARN, "teleport", f"git pull failed, falling back to rsync")
                    else: _log(_LOG_WARN, "teleport", f"git push failed, falling back to rsync")
                except (subprocess.SubprocessError, OSError, KeyError) as e:
                    _log(_LOG_ERROR, "teleport", f"git sync error, falling back to rsync: {e}")
        return self._rsync_working_directory(
            source_cwd, target_cwd, source_host, target_host, full)
    def _rsync_working_directory(self, source_cwd: str, target_cwd: str,
                                  source_host: str | None=None, target_host: str | None=None,
                                  full: bool=False) -> bool:
        _remote_run(["mkdir", "-p", target_cwd], host=target_host, capture_output=True)
        cmd = ["rsync", "-az", "--delete"]; gitignore_tmpfile = None
        if not full:
            try:
                gi_result = _remote_run(
                    ["git", "-C", source_cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
                    host=source_host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                if gi_result.returncode == 0 and gi_result.stdout.strip():
                    fd, gitignore_tmpfile = tempfile.mkstemp( prefix="rsync-gitignore-", suffix=".txt")
                    try:
                        os.write(fd, gi_result.stdout.encode())
                    finally:
                        os.close(fd)
                    cmd.extend(["--exclude-from", gitignore_tmpfile])
            except (ConnectionError, TimeoutError, OSError) as e:
                _log(_LOG_WARN, "teleport", f"git ls-files failed, skipping gitignore excludes: {e}")
            for excl in TELEPORT_RSYNC_EXCLUDES: cmd.extend(["--exclude", excl])
        src = source_cwd.rstrip("/") + "/"; dst = target_cwd.rstrip("/") + "/"
        if source_host: cmd.extend([f"{source_host}:{src}", dst])
        elif target_host: cmd.extend([src, f"{target_host}:{dst}"])
        else: cmd.extend([src, dst])
        try:
            r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_FULL_SYNC)
            if r.returncode != 0: _log(_LOG_WARN, "teleport", f"rsync failed: cmd={cmd} rc={r.returncode} stderr={r.stderr[:500]}")
            return r.returncode == 0
        finally:
            if gitignore_tmpfile and os.path.exists(gitignore_tmpfile): os.unlink(gitignore_tmpfile)
    def _sync_session_transcript(self, session_id: str, source_cwd: str, target_cwd: str,
                                  source_host: str | None=None, target_host: str | None=None) -> None:
        if not session_id: return
        source_slug = _project_slug(source_cwd); target_slug = _project_slug(target_cwd)
        source_dir = f".claude/projects/{source_slug}"; target_dir = f".claude/projects/{target_slug}"
        if target_host: _remote_run(["bash", "-c", f"mkdir -p $HOME/{target_dir}"], host=target_host, capture_output=True)
        else: os.makedirs(os.path.expanduser(f"~/{target_dir}"), exist_ok=True)
        jsonl = f"{session_id}.jsonl"
        for item in [jsonl, f"{session_id}/"]:
            if source_host:
                src_path = f"~/{source_dir}/{item}"; local_dst = os.path.expanduser(f"~/{target_dir}/")
                cmd = ["rsync", "-az", f"{source_host}:{src_path}", local_dst]
            elif target_host:
                local_src = os.path.expanduser(f"~/{source_dir}/{item}"); dst_path = f"~/{target_dir}/"
                cmd = ["rsync", "-az", local_src, f"{target_host}:{dst_path}"]
            else:
                local_src = os.path.expanduser(f"~/{source_dir}/{item}")
                local_dst = os.path.expanduser(f"~/{target_dir}/"); cmd = ["rsync", "-az", local_src, local_dst]
            r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0: _log(_LOG_WARN, "teleport", f"transcript sync failed for {item}: {r.stderr[:200]}")
    def _sync_shared_repos(self, target_host: str, chat_id: int | str | None = None) -> list[str]:
        if not target_host: return []
        warnings: list[str] = []
        def _warn(msg: str) -> None: warnings.append(msg); _log(_LOG_INFO, "teleport", msg)
        try:
            home = os.path.expanduser("~")
            git_repos = {"team": os.path.join(home, "team"), "agent-config": os.path.join(home, "agent-config")}
            for rn, rp in git_repos.items():
                if os.path.isdir(os.path.join(rp, ".git")):
                    try:
                        for cmd in [["git", "-C", rp, "add", "-A"], ["git", "-C", rp, "commit", "-m", f"teleport sync: {rn}"]]:
                            _subprocess_runner.run(cmd, capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                        _subprocess_runner.run(["git", "-C", rp, "push", "origin", "master"], capture_output=True, timeout=TIMEOUT_LARGE_TRANSFER)
                    except (subprocess.SubprocessError, OSError) as e: _warn(f"git push {rn}: {e}")
            for rn in git_repos:
                try: _remote_run(["bash", "-c", f"cd ~/{rn} 2>/dev/null && git pull origin master 2>/dev/null || true"], host=target_host, capture_output=True, timeout=TIMEOUT_LARGE_TRANSFER)
                except (subprocess.SubprocessError, OSError) as e: _warn(f"git pull {rn} on {target_host}: {e}")
            for subdir in ("skills", "hooks", "scripts"):
                try: _remote_run(["bash", "-c", f"[ -d ~/agent-config/.claude/{subdir} ] && rsync -az --checksum ~/agent-config/.claude/{subdir}/ ~/.claude/{subdir}/"], host=target_host, capture_output=True, timeout=TIMEOUT_LARGE_TRANSFER)
                except (subprocess.SubprocessError, OSError) as e: _warn(f"agent-config deploy {subdir}: {e}")
            remote_home = _get_remote_home(target_host)
            if remote_home and remote_home != home:
                settings_src = os.path.expanduser("~/.claude/settings.json")
                if os.path.exists(settings_src):
                    with open(settings_src) as f: settings_text = f.read().replace(home, remote_home)
                    fd, tmp = tempfile.mkstemp(suffix=".json")
                    try: os.write(fd, settings_text.encode())
                    finally: os.close(fd)
                    try: _subprocess_runner.run(["rsync", "-az", tmp, f"{target_host}:.claude/settings.json"], capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                    finally: os.unlink(tmp)
        except (subprocess.SubprocessError, OSError) as e: _warn(f"shared repo sync: {e}")
        return warnings
    def _sync_worker_data_back(self, name: str, source_host: str | None) -> None:
        if not source_host: return
        home = os.path.expanduser("~"); worker_team_dir = os.path.join(home, "team", name)
        if os.path.isdir(worker_team_dir):
            _subprocess_runner.run(
                ["rsync", "-az", f"{source_host}:team/{name}/", f"{worker_team_dir}/"],
                capture_output=True, timeout=TIMEOUT_GIT_OP)
        r = _remote_run(
            ["bash", "-c", "find ~/.claude/projects/*/memory -name '*.md' 2>/dev/null | head -50"],
            host=source_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode == 0 and r.stdout.strip():
            for remote_file in r.stdout.strip().splitlines():
                remote_home = _get_remote_home(source_host)
                if remote_home and remote_file.startswith(remote_home):
                    local_file = home + remote_file[len(remote_home):]; local_dir = os.path.dirname(local_file)
                    os.makedirs(local_dir, exist_ok=True)
                    _subprocess_runner.run(
                        ["rsync", "-az", f"{source_host}:{remote_file}", local_file],
                        capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
    def _run_teleport_preflight(self, target_host: str, worker_name: str, backend_name: str) -> list[str]:
        fails = []
        preflight_dirs = [
            os.path.expanduser("~/agent-config/teleport-preflight.d"),
            os.path.expanduser("~/.config/claudecode-telegram/teleport-preflight.d"), ]
        env = os.environ.copy()
        env["TARGET_HOST"] = target_host or ""
        env["WORKER_NAME"] = worker_name
        env["BACKEND"] = backend_name
        env["BRIDGE_URL"] = BRIDGE_PUBLIC_URL or BRIDGE_URL
        seen_scripts = set()
        for pdir in preflight_dirs:
            if not os.path.isdir(pdir): continue
            scripts = sorted(
                f for f in os.listdir(pdir)
                if f.endswith(".sh") and os.access(os.path.join(pdir, f), os.X_OK))
            for script in scripts:
                script_path = os.path.join(pdir, script); real_path = os.path.realpath(script_path)
                if real_path in seen_scripts: continue
                seen_scripts.add(real_path)
                try:
                    r = _subprocess_runner.run(
                        [script_path], env=env,
                        capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
                    if r.returncode != 0:
                        reason = r.stdout.strip().split("\n")[0] if r.stdout.strip() else f"{script} failed"
                        fails.append(reason)
                except subprocess.TimeoutExpired: fails.append(f"{script} timed out")
                except (subprocess.SubprocessError, OSError) as e: fails.append(f"{script} error: {e}")
        return fails
    def _install_hooks_on_target(self, target_host: str) -> list[str]:
        if not target_host: return []
        warnings = []
        try:
            _remote_run(["chmod", "-R", "700", ".claude/hooks"], host=target_host, capture_output=True)
            claude_json = os.path.expanduser("~/.claude.json")
            if os.path.exists(claude_json):
                _subprocess_runner.run(
                    ["rsync", "-az", claude_json, f"{target_host}:.claude.json"],
                    capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
            else:
                _remote_run(
                    ["python3", "-c",
                     'import json,os,pathlib;'
                     'p=pathlib.Path(os.path.expanduser("~/.claude.json"));'
                     'd=json.loads(p.read_text()) if p.exists() else {};'
                     'd["hasCompletedOnboarding"]=True;'
                     'd.setdefault("numStartups",1);'
                     'p.write_text(json.dumps(d))'],
                    host=target_host, capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
        except (subprocess.SubprocessError, OSError) as e:
            w = f"hook install: {e}"
            warnings.append(w)
            _log(_LOG_INFO, "teleport", f"{w}")
        return warnings
    def _sync_session_files_to_target(self, name: str, target_sessions_dir: str, target_host: str) -> None:
        local_session_dir = SESSIONS_DIR / name
        if not local_session_dir.is_dir(): return
        remote_session_dir = f"{target_sessions_dir}/{name}"
        _remote_run(["mkdir", "-p", remote_session_dir], host=target_host, capture_output=True)
        for fname in ["chat_id", "claude_session_id"]:
            local_file = local_session_dir / fname
            if local_file.exists():
                _subprocess_runner.run(
                    ["rsync", "-az", str(local_file), f"{target_host}:{remote_session_dir}/{fname}"],
                    capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
    def _sync_credentials_to_target(self, target_host: str) -> None:
        local_creds = os.path.expanduser("~/.claude/.credentials.json")
        if not os.path.exists(local_creds): return
        try:
            r = _remote_run(["cat", ".claude/.credentials.json"],
                             host=target_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode == 0 and r.stdout.strip():
                remote_data = cast(dict[str, object], json.loads(r.stdout))
                local_data = cast(dict[str, object], json.loads(Path(local_creds).read_text()))
                _ro = remote_data.get("claudeAiOauth", {}); remote_oauth = _ro if isinstance(_ro, dict) else {}
                _lo = local_data.get("claudeAiOauth", {}); local_oauth = _lo if isinstance(_lo, dict) else {}
                remote_refresh = str(remote_oauth.get("refreshToken", ""))
                local_refresh = str(local_oauth.get("refreshToken", ""))
                remote_exp = int(remote_oauth.get("expiresAt", 0)); now_ms = int(_clock.time() * 1000)
                if remote_refresh and remote_refresh != local_refresh and remote_exp > now_ms:
                    _log(_LOG_INFO, "creds", f"Target {target_host} has independent valid credentials, skipping sync")
                    return
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, subprocess.SubprocessError, OSError): pass
        _remote_run(["mkdir", "-p", ".claude"], host=target_host, capture_output=True)
        _subprocess_runner.run(
            ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json.tmp"],
            capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
        _remote_run(["mv", ".claude/.credentials.json.tmp",
                      ".claude/.credentials.json"],
                     host=target_host, capture_output=True)
        _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host, capture_output=True)
        _log(_LOG_INFO, "creds", f"Synced credentials to {target_host}")
    def _start_worker_on_target(self, name: str, target_host: str, target_cwd: str | None,
                                 session_id: str | None, backend_name: str, skip_session_sync: bool=False) -> bool:
        tmux_name = f"{TMUX_PREFIX}{name}"
        _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host, capture_output=True)
        _clock.sleep(DELAY_TMUX_SEND)
        r = _remote_run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            host=target_host, capture_output=True, text=True)
        if r.returncode != 0:
            _log(_LOG_WARN, "teleport", f"tmux new-session failed: rc={r.returncode} stderr={r.stderr[:200] if r.stderr else ''}")
            return False
        _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
                    host=target_host, capture_output=True)
        _clock.sleep(DELAY_RETRY)
        target_sessions_dir = _remap_path(str(SESSIONS_DIR), target_host)
        if target_host and not skip_session_sync: self._sync_session_files_to_target(name, target_sessions_dir, target_host)
        if target_host: self._sync_credentials_to_target(target_host)
        for key, value in {
            "PORT": str(PORT),
            "TMUX_PREFIX": TMUX_PREFIX,
            "SESSIONS_DIR": target_sessions_dir,
            "WORKER_BACKEND": normalize_backend(backend_name),
            "BRIDGE_URL": BRIDGE_PUBLIC_URL or BRIDGE_URL,
        }.items():
            _remote_run(["tmux", "set-environment", "-t", tmux_name, key, value], host=target_host, capture_output=True)
        _clock.sleep(DELAY_TMUX_SEND)
        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name, 'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
            host=target_host, capture_output=True)
        _clock.sleep(DELAY_TMUX_SEND)
        backend = get_backend(backend_name); cli_cmd = backend.start_cmd(session_id or "")
        if target_host:
            id_result = _remote_run(["id", "-u"], host=target_host, capture_output=True, text=True)
            if id_result.returncode == 0 and id_result.stdout.strip() == "0":
                cli_cmd = cli_cmd.replace(" --dangerously-skip-permissions", "")
        start_cmd = f'unset CLAUDECODE && {cli_cmd}'
        if target_cwd: start_cmd = f'cd {shlex.quote(target_cwd)} && {start_cmd}'
        _log(_LOG_INFO, "teleport", f"start_cmd={start_cmd}")
        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
            host=target_host, capture_output=True)
        if backend.is_interactive:
            for delay, key in [(3.0, "Enter"), (2.0, "Enter"),
                               (1.0, "Enter"), (1.0, "Enter")]:
                _clock.sleep(delay)
                _remote_run(["tmux", "send-keys", "-t", tmux_name, key],
                             host=target_host, capture_output=True)
        for attempt in range(30):
            _clock.sleep(DELAY_STARTUP)
            r = _remote_run(
                ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
                host=target_host, capture_output=True, text=True)
            if r.returncode != 0:
                if attempt % 10 == 0:
                    _log(_LOG_WARN, "teleport", f"verify attempt {attempt}: tmux display-message failed rc={r.returncode}")
                continue
            pane_pid = r.stdout.strip(); claude_pid = _get_claude_pid(pane_pid, host=target_host) if pane_pid else None
            if claude_pid:
                _log(_LOG_INFO, "teleport", f"verified: claude running as pid {claude_pid} (pane {pane_pid})")
                return True
            if attempt % 10 == 0:
                _log(_LOG_INFO, "teleport", f"verify attempt {attempt}: pane_pid={pane_pid}, no claude yet")
                cap = _remote_run(
                    ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                    host=target_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if cap.returncode == 0: _log(_LOG_INFO, "teleport", f"pane content: {cap.stdout[:300]}")
        _log(_LOG_WARN, "teleport", f"verify FAILED after 30 attempts")
        return False
    def _teleport_rollback(self, name: str, tmux_name: str, source_host: str | None, source_cwd: str | None,
                            session_id: str | None, backend_name: str, chat_id: ChatId | None, reason: str) -> None:
        self._teleport_notify(chat_id, f"Teleport failed: {reason}. Rolling back...")
        try:
            if source_cwd: save_claude_session_cwd(name, source_cwd)
            if not tmux_exists(tmux_name, host=source_host):
                _remote_run(
                    ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
                    host=source_host, capture_output=True)
                _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
                            host=source_host, capture_output=True)
            backend = get_backend(backend_name)
            start_cmd = f'unset CLAUDECODE && {backend.start_cmd(session_id or "")}'
            if source_cwd: start_cmd = f'cd {shlex.quote(source_cwd)} && {start_cmd}'
            _remote_run(
                ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
                host=source_host, capture_output=True)
            self._teleport_notify(chat_id, f"{name} restarted on source. Teleport cancelled.")
        except (subprocess.SubprocessError, OSError) as e: self._teleport_notify(chat_id, f"Rollback also failed: {e}")
        try:
            state_file = SESSIONS_DIR / name / "teleport_state"
            state_file.unlink(missing_ok=True)
        except OSError as exc: _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
    def _teleport_notify(self, chat_id: ChatId | None, text: str) -> None:
        if chat_id is None: return
        try:
            transport.send_text(chat_id, text)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:_teleport_notify", f"{type(exc).__name__}: {exc}")
    def cmd_hire(self, name: str, chat_id: ChatId) -> bool:
        if not name:
            self.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
            return True
        parsed_name, backend = parse_hire_args(name)
        if not parsed_name:
            self.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
            return True
        name = parsed_name.lower().strip(); name = re.sub(r'[^a-z0-9-]', '', name)
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
        else: self.reply(chat_id, f"Could not hire \"{name}\". {err}", outcome="Needs decision")
        return True
    def cmd_end(self, name: str, chat_id: ChatId) -> bool:
        if not name:
            self.reply(chat_id, "This is permanent. Usage: /end <name>", outcome="Needs decision")
            return True
        name = name.lower().strip()
        ok, err = kill_session(name)
        if ok:
            self.reply(chat_id, f"{name.capitalize()} removed from your team.")
            update_bot_commands()
        else: self.reply(chat_id, f"Could not remove \"{name}\". {err}", outcome="Needs decision")
        return True
    def cmd_restart(self, chat_id: ChatId, args: str = "") -> bool:
        args = (args or "").strip(); clean = False; force = False; tokens = args.split()
        remaining = []
        for t in tokens:
            if t == "--clean": clean = True
            elif t == "--force": force = True
            else: remaining.append(t)
        name_arg = remaining[0].lower() if remaining else ""
        if name_arg == "cancel": return self._cmd_restart_cancel(chat_id)
        if name_arg == "all": return self._cmd_restart_all(chat_id, clean)
        if len(remaining) > 1:
            names = [n.lower() for n in remaining]
            self.reply(chat_id, f"Restarting {len(names)} workers: {', '.join(names)}...")
            for n in names: self.cmd_restart(chat_id, f"{'--clean ' if clean else ''}{'--force ' if force else ''}{n}")
            return True
        if name_arg: name = name_arg
        else:
            if not state.active:
                registered = self.workers.get_registered_sessions()
                if len(registered) == 1:
                    name = next(iter(registered)); state.active = name
                    save_last_active(name)
                else:
                    self.reply(chat_id, "No one assigned.")
                    return True
            else: name = state.active
        registered = self.workers.get_registered_sessions(); session = registered.get(name)
        if name not in registered:
            if registered:
                names_str = ", ".join(registered.keys())
                self.reply(chat_id, f"Can't find \"{name}\". Available workers: {names_str}")
            else: self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True
        if name_arg:
            state.active = name
            save_last_active(name)
        host = get_worker_host(name)
        tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}") if session else f"{self.workers.tmux_prefix}{name}"
        _log(_LOG_INFO, "cmd_restart", f"{name}: force={force}, clean={clean}, host={host}, tmux={tmux_name}")
        tmux_alive = tmux_exists(tmux_name, host=host)
        claude_running = is_claude_running(tmux_name, host=host) if tmux_alive else False
        _log(_LOG_INFO, "cmd_restart", f"{name}: tmux_alive={tmux_alive}, claude_running={claude_running}")
        if not force and tmux_alive and claude_running:
            _log(_LOG_INFO, "cmd_restart", f"{name}: BLOCKED (already running)")
            self.reply(chat_id, f"{name.capitalize()} is already running. Use /restart --force {name} to force.")
            return True
        with watchdog.restart_lock:
            inflight_ts = watchdog.restart_in_progress.get(name)
            if inflight_ts and _clock.time() - inflight_ts < 120:
                _log(_LOG_INFO, "cmd_restart", f"{name}: BLOCKED (restart in progress since {_clock.time() - inflight_ts:.0f}s ago)")
                self.reply(chat_id, f"{name.capitalize()} restart already in progress. Wait for it to finish.")
                return True
            watchdog.restart_in_progress[name] = _clock.time()
        try:
            result = self._do_restart(name, session, chat_id, host, tmux_name, force, clean)
            if force: watchdog.force_restart_pending_cwd[name] = True
            return result
        finally:
            with watchdog.restart_lock: watchdog.restart_in_progress.pop(name, None)
    def _do_restart(self, name: str, session: TmuxSessionDict | None, chat_id: ChatId, host: str | None, tmux_name: str, force: bool, clean: bool) -> bool:
        if host:
            mode = "relaunch" if clean else "resume"
            backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)
            _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart mode={mode}, host={host}")
            self.reply(chat_id, f"Restarting {name.capitalize()} on remote host...")
            ok, err = self._restart_remote_worker(name, backend_name, backend_obj, tmux_name, host, mode)
            watchdog.recent_restarts[name] = _clock.time()
            _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart result ok={ok}, err={err}")
            return self._restart_reply(chat_id, name, ok, err, host)
        backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
        backend = get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}") if session else f"{self.workers.tmux_prefix}{name}"
        if not clean and not backend.is_interactive and session and "tmux" in session and tmux_exists(tmux_name):
            session_id, source = get_any_session_id(name)
            if session_id: self.reply(chat_id, f"{name.capitalize()} is still active. Next message continues where you left off.")
            else: self.reply(chat_id, f"No active session for {name.capitalize()}. Next message starts fresh.")
            return True
        mode = "relaunch" if clean else "resume"
        if mode == "resume":
            session_dir = get_session_dir(name)
            if not session_dir.exists() or not any(session_dir.glob("*_session_id")): mode = "relaunch"
        label = "Resuming" if mode == "resume" else ("Bringing" if clean else "Restarting")
        self.reply(chat_id, f"{label} {name.capitalize()}...")
        ok, err = restart_claude(name, mode=mode)
        if ok: watchdog.recent_restarts[name] = _clock.time()
        return self._restart_reply(chat_id, name, ok, err)
    def _restart_reply(self, chat_id: ChatId, name: str, ok: bool, err: str | None, host: str | None = None) -> bool:
        if ok: self.reply(chat_id, f"{name.capitalize()} is back and ready.")
        else:
            loc = f" on {host}" if host else ""
            self.reply(chat_id, f"Could not restart \"{name}\"{loc}. {err}", outcome="Needs decision")
        return True
    def _restart_remote_worker(self, name: str, backend_name: str, backend: Backend, tmux_name: str, host: str, mode: str) -> tuple[bool, str | None]:
        resume_id = ""; target_cwd = get_claude_session_cwd(name)
        if target_cwd and host: target_cwd = _remap_path(target_cwd, host)
        _log(_LOG_INFO, "_restart_remote", f"{name}: mode={mode}, host={host}, tmux={tmux_name}, cwd={target_cwd}")
        if mode == "resume":
            resume_id = get_claude_session_id(name, authoritative=False)
            if not resume_id: resume_id = get_claude_session_id(name, authoritative=True)
            _log(_LOG_INFO, "_restart_remote", f"{name}: resume_id={resume_id}")
        else:
            session_dir = SESSIONS_DIR / name
            session_dir.mkdir(parents=True, exist_ok=True)
            cleared = list(session_dir.glob("*_session_id"))
            for f in cleared: f.unlink()
            _clear_hook_failures(name)
            _log(_LOG_INFO, "_restart_remote", f"{name}: cleared {len(cleared)} session files for relaunch")
        if tmux_exists(tmux_name, host=host):
            _log(_LOG_INFO, "_restart_remote", f"{name}: stopping remote tmux {tmux_name}")
            self._stop_worker_for_teleport(name, tmux_name, host=host)
            _remote_run(["tmux", "kill-session", "-t", tmux_name], host=host, capture_output=True)
            _clock.sleep(DELAY_RETRY)
        else: _log(_LOG_INFO, "_restart_remote", f"{name}: tmux {tmux_name} not found on {host}")
        if mode == "resume":
            cached = get_claude_session_id(name, authoritative=False)
            if cached: resume_id = cached
            else: resume_id = get_claude_session_id(name, authoritative=True) or resume_id
            _log(_LOG_INFO, "_restart_remote", f"{name}: post-stop resume_id={resume_id}")
        if resume_id and target_cwd and host:
            remote_home = _get_remote_home(host)
            if remote_home:
                cwd_slug = target_cwd.replace("/", "-")
                session_file = f"{remote_home}/.claude/projects/{cwd_slug}/{resume_id}.jsonl"
                check = _remote_run(["test", "-f", session_file], host=host,
                                     capture_output=True, timeout=TIMEOUT_TMUX_SEND)
                if check.returncode != 0:
                    _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} NOT found at {session_file}, starting fresh")
                    resume_id = ""; session_dir = SESSIONS_DIR / name
                    session_dir.mkdir(parents=True, exist_ok=True)
                    for f in session_dir.glob("*_session_id"): f.unlink()
                else: _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} validated at {session_file}")
        _ensure_workspace_trusted_remote(target_cwd, host)
        _log(_LOG_INFO, "_restart_remote", f"{name}: calling _start_worker_on_target(cwd={target_cwd}, resume={resume_id}, backend={backend_name})")
        ok = self._start_worker_on_target( name, host, target_cwd, resume_id, backend_name, skip_session_sync=True)
        if not ok:
            _log(_LOG_WARN, "_restart_remote", f"{name}: _start_worker_on_target FAILED")
            return False, f"Failed to restart {name} on {host}"
        welcome = self.workers._build_welcome(name, backend)
        if backend.is_interactive:
            started = False
            for _ in range(10):
                _clock.sleep(DELAY_STARTUP)
                if is_claude_running(tmux_name, host=host):
                    started = True
                    break
            if started: self.workers.send(name, welcome)
            else: _log(_LOG_WARN, "_restart_remote", f"{name}: Claude did not start within 10s, skipping welcome")
        _log(_LOG_INFO, "_restart_remote", f"{name}: restarted successfully (mode={mode})")
        return True, None
    def _cmd_restart_all(self, chat_id: int | str, clean: bool) -> bool:
        registered = self.workers.get_registered_sessions()
        if not registered:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True
        with self._restart_all_lock:
            if self._restart_all_running:  # type: ignore[has-type]
                self.reply(chat_id, "A /restart all is already running. Use /restart cancel to stop it.")
                return True
            self._restart_all_running = True
            self._restart_all_abort.clear()
        names = sorted(registered.keys()); active = state.active
        if active and active in names:
            names.remove(active)
            names.append(active)
        mode = "relaunch" if clean else "resume"
        self.reply(chat_id, f"Restarting {len(names)} workers sequentially ({mode})...")
        self._restart_all_thread: threading.Thread | None = threading.Thread(
            target=self._run_restart_all_sequence,
            args=(chat_id, names, mode),
            name="restart-all",
            daemon=True, )
        self._restart_all_thread.start()
        return True
    def _run_restart_all_sequence(self, chat_id: int | str, names: list[str], mode: str) -> None:
        delay_s = 7; failed = []
        try:
            total = len(names)
            for i, name in enumerate(names, 1):
                if self._restart_all_abort.is_set():
                    self.reply(chat_id, f"Restart sequence aborted at {i-1}/{total}.")
                    return
                host = get_worker_host(name)
                if host:
                    _sync_worker_manager()
                    assert worker_manager is not None
                    reg = worker_manager.get_registered_sessions(); session = reg.get(name, {})
                    backend_name = get_worker_backend(name, session); backend_obj = get_backend(backend_name)
                    tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}")
                    ok, err = self._restart_remote_worker( name, backend_name, backend_obj, tmux_name, host, mode)
                else: ok, err = restart_claude(name, mode=mode)
                if ok: self.reply(chat_id, f"[{i}/{total}] {name.capitalize()} restarted.")
                else:
                    failed.append((name, err))
                    self.reply(chat_id, f"[{i}/{total}] {name.capitalize()} failed: {err}")
                if i < total:
                    for _ in range(5):
                        if self._restart_all_abort.is_set(): break
                        _clock.sleep(delay_s / 5)
            if failed:
                summary = ", ".join(n for n, _ in failed)
                self.reply(chat_id, f"Restart all done. {len(failed)} failed: {summary}")
            else: self.reply(chat_id, f"Restart all done. All {total} workers restarted.")
        finally:
            with self._restart_all_lock:
                self._restart_all_running = False
                self._restart_all_abort.clear()
                self._restart_all_thread = None
    def _cmd_restart_cancel(self, chat_id: int | str) -> bool:
        with self._restart_all_lock:
            if not self._restart_all_running:
                self.reply(chat_id, "No restart-all sequence is running.")
                return True
            self._restart_all_abort.set()
        self.reply(chat_id, "Stopping restart-all sequence...")
        return True
    def _cmd_relay_list(self, chat_id: ChatId) -> bool:
        with relay_store.lock:
            active = [(cid, ch) for cid, ch in relay_store.channels.items()
                      if _clock.time() <= ch["expires_at_unix"]]
        if active:
            lines = []
            for cid, ch in active:
                msg_count = len(ch.get("messages", [])); workers = ch.get("workers", [ch["worker"]])
                worker_str = ", ".join(workers)
                lines.append(f"• {cid} → {worker_str} ({msg_count} msgs, expires {ch['expires_at']})")
            self.reply(chat_id, "\U0001f4e1 Active relays:\n" + "\n".join(lines))
        else: self.reply(chat_id, "No active relays.")
        return True
    def _cmd_relay_add(self, parts: list[str], chat_id: ChatId) -> bool:
        if len(parts) < 3:
            self.reply(chat_id, "Usage: /relay add <channel_id> <worker>")
            return True
        channel_id = parts[1]; new_worker = parts[2].lower(); registered = get_registered_sessions()
        if new_worker not in registered:
            self.reply(chat_id, f"Worker \"{new_worker}\" not found.")
            return True
        with relay_store.lock:
            found = relay_store.channels.get(channel_id)
            if not found or _clock.time() > found["expires_at_unix"]: found = None
        if not found:
            self.reply(chat_id, f"Relay \"{channel_id}\" not found. Use /relay list to see active channels.")
            return True
        workers = found.get("workers", [found["worker"]])
        if new_worker in workers:
            self.reply(chat_id, f"{new_worker} is already in {channel_id}.")
            return True
        with relay_store.lock:
            found.setdefault("workers", [found["worker"]]).append(new_worker)
            _relay_save()
        self.reply(chat_id, f"\U0001f4e1 Added {new_worker} to {channel_id}. Workers: {', '.join(found['workers'])}")
        return True
    def _cmd_relay_remove(self, parts: list[str], chat_id: ChatId) -> bool:
        if len(parts) < 3:
            self.reply(chat_id, "Usage: /relay remove <channel_id> <worker>")
            return True
        channel_id = parts[1]; rm_worker = parts[2].lower()
        with relay_store.lock:
            found = relay_store.channels.get(channel_id)
            if not found or _clock.time() > found["expires_at_unix"]: found = None
        if not found:
            self.reply(chat_id, f"Relay \"{channel_id}\" not found. Use /relay list to see active channels.")
            return True
        workers = found.get("workers", [found["worker"]])
        if rm_worker not in workers:
            self.reply(chat_id, f"{rm_worker} is not in {channel_id}.")
            return True
        if len(workers) <= 1:
            self.reply(chat_id, f"Can't remove the last worker. Use /relay stop {channel_id} to close it.")
            return True
        with relay_store.lock:
            found["workers"].remove(rm_worker)
            _relay_save()
        self.reply(chat_id, f"\U0001f4e1 Removed {rm_worker} from {channel_id}. Workers: {', '.join(found['workers'])}")
        return True
    def _cmd_relay_status(self, chat_id: ChatId) -> bool:
        now = _clock.time()
        with relay_store.lock:
            relay_active = [(cid, ch) for cid, ch in relay_store.channels.items()
                            if now <= ch["expires_at_unix"]]
        with channel_store.lock:
            ch_active = [(cid, ch) for cid, ch in channel_store.channels.items()
                         if not channel_is_expired(ch)]
        with guest_store.lock:
            guest_active = [g for g in guest_store.guests.values()
                            if now <= g.get("expires_at_unix", 0)]
        lines = ["\U0001f4e1 System Status\n"]
        lines.append(f"Relays: {len(relay_active)}")
        for cid, ch in relay_active:
            msg_count = len(ch.get("messages", [])); workers = ch.get("workers", [ch["worker"]])
            lines.append(f"  • {ch['label']} → {', '.join(workers)} ({msg_count} msgs)")
        lines.append(f"\nChannels: {len(ch_active)}")
        for cid, chan in ch_active:
            members_str = ", ".join(chan["members"].keys()); msg_count = len(chan.get("messages", []))
            lines.append(f"  • {cid} ({chan['label']}) — {members_str} ({msg_count} msgs)")
        lines.append(f"\nGuests: {len(guest_active)}")
        for g in guest_active:
            inbox_count = len(guest_store.inboxes.get(g["name"], []))
            lines.append(f"  • {g['name']} (inbox: {inbox_count} msgs)")
        self.reply(chat_id, "\n".join(lines))
        return True
    def _cmd_relay_stop(self, parts: list[str], chat_id: ChatId) -> bool:
        if len(parts) < 2:
            self.reply(chat_id, "Usage: /relay stop <label>")
            return True
        target = parts[1]; removed = False
        with relay_store.lock:
            to_remove = None
            if target in relay_store.channels: to_remove = target
            else:
                target_lower = target.lower()
                for cid, ch in relay_store.channels.items():
                    if ch["label"] == target_lower or ch["worker"] == target_lower:
                        to_remove = cid
                        break
            if to_remove:
                del relay_store.channels[to_remove]
                _relay_save()
                removed = True
        if removed: self.reply(chat_id, f"\U0001f4e1 Relay \"{target}\" closed.")
        else: self.reply(chat_id, f"Relay \"{target}\" not found.")
        return True
    def cmd_relay(self, arg: str, chat_id: ChatId) -> bool:
        if not arg:
            with relay_store.lock:
                active = [ch for ch in relay_store.channels.values()
                          if _clock.time() <= ch["expires_at_unix"]]
            lines = ["\U0001f4e1 Relay — connect any agent to the team\n"]
            lines.append("/relay <worker> — create relay, get a guideline link")
            lines.append("/relay add <channel_id> <worker> — add worker to relay")
            lines.append("/relay remove <channel_id> <worker> — remove worker")
            lines.append("/relay list — active relays")
            lines.append("/relay status — counts for relays, channels, guests")
            lines.append("/relay stop <name> — close relay")
            if active: lines.append(f"\nActive: {', '.join(ch['label'] for ch in active)}")
            self.reply(chat_id, "\n".join(lines))
            return True
        parts = arg.strip().split(); sub = parts[0].lower()
        subcommands: dict[str, Callable[[], bool]] = {
            "list": lambda: self._cmd_relay_list(chat_id),
            "add": lambda: self._cmd_relay_add(parts, chat_id),
            "remove": lambda: self._cmd_relay_remove(parts, chat_id),
            "status": lambda: self._cmd_relay_status(chat_id),
            "stop": lambda: self._cmd_relay_stop(parts, chat_id), }
        if sub in subcommands: return subcommands[sub]()
        worker = sub; registered = get_registered_sessions()
        if worker not in registered:
            self.reply(chat_id, f"Worker \"{worker}\" not found.")
            return True
        label = f"relay-{worker}"
        ch, guest_token, reply_token = relay_channel_create(worker, label)
        with relay_store.lock:
            relay_store.channels[ch["id"]] = ch
            _relay_save()
        url = relay_guide_url(ch["id"], guest_token); lines = [f"\U0001f4e1 Relay to {worker} (24h) — {ch['id']}"]
        lines.append(f"\nManage: /relay add {ch['id']} <worker> to add more workers")
        lines.append(f"\nPaste this to the external agent:\n")
        lines.append(f"---")
        lines.append(f"You have a direct channel to {worker}. Open this link to see the API guide (setup + curl commands):")
        lines.append(f"{url}")
        lines.append(f"Steps: (1) open the link, (2) copy the env vars and curl commands from the page, (3) send your message via the /send endpoint, (4) poll /messages for replies. Channel expires in 24h.")
        lines.append(f"---")
        self.reply(chat_id, "\n".join(lines))
        return True
    def _extract_reply_media(self, reply_to: TelegramMessageDict, target_worker: str) -> str | None:
        animation = reply_to.get("animation"); photo = reply_to.get("photo"); document = reply_to.get("document")
        audio = reply_to.get("audio"); voice = reply_to.get("voice"); video = reply_to.get("video")
        sticker = reply_to.get("sticker"); file_id = None; media_label = "media"
        if animation: file_id = animation.get("file_id"); media_label = "GIF"
        elif photo:
            largest = max(photo, key=lambda p: p.get("file_size", 0)); file_id = largest.get("file_id")
            media_label = "image"
        elif video: file_id = video.get("file_id"); media_label = "video"
        elif document: file_id = document.get("file_id"); media_label = f"file: {document.get('file_name', 'unknown')}"
        elif audio: file_id = audio.get("file_id"); media_label = "audio"
        elif voice: file_id = voice.get("file_id"); media_label = "voice message"
        elif sticker: file_id = sticker.get("file_id"); media_label = f"sticker: {sticker.get('emoji', '')}"
        if not file_id: return None
        local_path = download_telegram_file(file_id, target_worker)
        if not local_path: return None
        if voice:
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
            photo = msg.get("photo"); document = msg.get("document"); animation = msg.get("animation")
            video = msg.get("video"); audio = msg.get("audio"); voice = msg.get("voice")
            video_note = msg.get("video_note"); sticker = msg.get("sticker"); doc_is_image = False
            if document: mime_type = document.get("mime_type", ""); doc_is_image = mime_type.startswith("image/")
            file_id = None; media_type = "media"
            if animation: file_id = animation.get("file_id"); media_type = "GIF"
            elif photo:
                largest = max(photo, key=lambda p: p.get("file_size", 0)); file_id = largest.get("file_id")
                media_type = "image"
            elif doc_is_image and document: file_id = document.get("file_id"); media_type = "image"
            elif document: file_id = document.get("file_id"); media_type = "file"
            elif video: file_id = video.get("file_id"); media_type = "video"
            elif audio: file_id = audio.get("file_id"); media_type = "audio"
            elif voice: file_id = voice.get("file_id"); media_type = "voice"
            elif video_note: file_id = video_note.get("file_id"); media_type = "video note"
            elif sticker: file_id = sticker.get("file_id"); media_type = "sticker"
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
                statuses = []
                for name in targets: statuses.append(self._route_mention(name, media_text, chat_id, msg_id))
                sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
                offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
                if offline:
                    parts = []
                    parts.append(f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline.")
                    if sent_to: parts.append(f"Delivered to {', '.join(sent_to)}.")
                    self.reply(chat_id, " ".join(parts))
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
        sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
        offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
        if offline:
            parts = [f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline."]
            if sent_to: parts.append(f"Delivered to {', '.join(sent_to)}.")
            self.reply(chat_id, " ".join(parts))
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
                guest_store.inboxes[name] = guest_inbox_append(inbox, cast(GuestInboxMessageDict, msg_obj))
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
        file_id: str | None = None; media_label = "media"
        if incoming.animation: file_id = incoming.animation.get("file_id"); media_label = "GIF"
        elif incoming.photo or incoming.doc_is_image:
            if incoming.photo: file_id = max(incoming.photo, key=lambda p: p.get("file_size", 0)).get("file_id")
            else: file_id = incoming.document.get("file_id") if incoming.document else None
            media_label = "image"
        elif incoming.document and not incoming.doc_is_image: file_id = incoming.document.get("file_id"); media_label = "file"
        else:
            for attr, label in [("audio", "audio"), ("voice", "voice"), ("video", "video"), ("video_note", "video note"), ("sticker", "sticker")]:
                item = getattr(incoming, attr, None)
                if item: file_id = item.get("file_id"); media_label = label; break
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
        if not name:
            self.reply(chat_id, "Usage: /pilot <name> [name2 ...]", outcome="Needs decision")
            return True
        names = name.lower().strip().split(); prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
        pilot_port = os.environ.get("PILOT_PORT", "10170")
        import urllib.request, json as _json
        from urllib.parse import urlparse, quote as _urlquote
        if "all" in names:
            assert worker_manager is not None
            registered = worker_manager.scan_tmux_sessions(); registry = _load_registry()
            for rname, rinfo in registry.get("workers", {}).items():
                if rinfo.get("host") and rname not in registered: registered[rname] = {"tmux": f"{prefix}{rname}", "host": rinfo["host"]}
            if not registered:
                self.reply(chat_id, "No active workers found", outcome="Needs decision")
                return True
            names = sorted(registered.keys())
        enabled = []; session_names = []; errors = []
        for n in names:
            session_name = f"{prefix}{n}" if not n.startswith("claude-") else n
            try:
                worker_host = get_worker_host(n)
                url = f"http://localhost:{pilot_port}/api/pilot?session={session_name}"
                if worker_host: url += f"&host={_urlquote(worker_host)}"
                req = urllib.request.Request(url, method="POST")
                with _urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp: cast(dict[str, object], _json.loads(resp.read()))
                enabled.append(n)
                session_names.append(session_name)
            except (urllib.error.URLError, OSError, TimeoutError) as e: errors.append(f"{n}: {e}")
        if not enabled:
            self.reply(chat_id, f"Pilot error: {'; '.join(errors)}", outcome="Needs decision")
            return True
        ts = time.strftime("%m%d-%H%M", time.gmtime(_clock.time()))
        if len(enabled) <= 3: slug = "-".join(enabled) + "-" + ts
        else: slug = f"team{len(enabled)}-{ts}"
        try:
            payload = _json.dumps({"slug": slug, "sessions": session_names, "ttl": 1800}).encode()
            req = urllib.request.Request(
                f"http://localhost:{pilot_port}/api/grid-session",
                data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            with _urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp: cast(dict[str, object], _json.loads(resp.read()))
        except (urllib.error.URLError, OSError, TimeoutError) as exc: _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")
        host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "localhost"
        pilot_url = f"http://{host}:{pilot_port}/grid/{_urlquote(slug)}"; names_str = ", ".join(enabled)
        msg = f"✈️ Pilot: {names_str} (30min)\n{pilot_url}"
        if errors: msg += f"\n⚠️ Failed: {'; '.join(errors)}"
        self.reply(chat_id, msg)
        return True
    def cmd_rewind(self, name: str, chat_id: ChatId) -> bool:
        if not name:
            self.reply(chat_id, "Usage: /rewind <name>", outcome="Needs decision")
            return True
        name = name.lower().strip()
        import secrets
        token = secrets.token_urlsafe(32); base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
        tokens.add_rewind(token, name)
        url = f"{base_url}/transcript/{name}?token={token}"
        try:
            html_content = _render_transcript_html(
                name, per_page=200, token=token,
                live_base_url=f"{base_url}/transcript/{name}")
            snap_path = f"/tmp/rewind-{name}.html"
            with open(snap_path, "w") as f: f.write(html_content)
            serve_url = _beast_serve_deploy(snap_path, f"rewind-{name}")
            if serve_url:
                self.reply(chat_id, f"⏪ Rewind for {name}\n{serve_url}")
                return True
        except OSError as e: _log(_LOG_WARN, "bridge", f"Rewind snapshot deploy failed for {name}: {e}")
        self.reply(chat_id, f"⏪ Rewind for {name}\n{url}")
        return True
    def cmd_pr_review(self, arg: str, chat_id: ChatId) -> bool:
        if not arg:
            self.reply(chat_id, "Usage: /pr <github_pr_url>\nExample: /pr https://github.com/BasedHardware/omi/pull/6426", outcome="Needs decision")
            return True
        arg = arg.strip(); clean_url = arg.split('#')[0]
        m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', clean_url)
        if not m:
            try:
                pr_num = int(clean_url)
                owner, repo = 'BasedHardware', 'omi'
            except ValueError:
                self.reply(chat_id, "Invalid PR URL. Example: /pr https://github.com/BasedHardware/omi/pull/6426", outcome="Needs decision")
                return True
        else: owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
        self.reply(chat_id, f"Generating PR review for {owner}/{repo}#{pr_num}...")
        script_path = Path(__file__).parent / "review.py"; out_path = f"/tmp/pr-review-{pr_num}.html"
        try:
            r = _subprocess_runner.run(
                [sys.executable, str(script_path), arg, "--no-serve"],
                capture_output=True, text=True, timeout=PENDING_TIMEOUT)
            if r.returncode != 0 or not os.path.exists(out_path):
                self.reply(chat_id, f"Failed to generate PR review:\n{r.stderr[:500]}", outcome="Needs decision")
                return True
        except subprocess.TimeoutExpired:
            self.reply(chat_id, "PR review generation timed out (>300s).", outcome="Needs decision")
            return True
        slug = f"pr-{pr_num}"; serve_url = _beast_serve_deploy(out_path, slug)
        if serve_url: self.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{serve_url}")
        else:
            import secrets
            token = secrets.token_urlsafe(32)
            tokens.add_pr_review(token, pr_num, owner, repo)
            base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
            url = f"{base_url}/tools/review/{pr_num}?token={token}"
            self.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{url}")
        return True
    def cmd_focus(self, name: str, chat_id: ChatId) -> bool:
        if not name:
            self.reply(chat_id, "Usage: /focus <name>", outcome="Needs decision")
            return True
        name = name.lower().strip()
        ok, err = switch_session(name)
        if ok: self.reply(chat_id, f"Now talking to {name.capitalize()}.")
        else: self.reply(chat_id, f"Could not focus \"{name}\". {err}", outcome="Needs decision")
        return True
    def cmd_team(self, chat_id: ChatId) -> bool:
        registered = self.workers.scan_tmux_sessions(); registered = self.workers.get_registered_sessions(registered)
        if not registered:
            self.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
            return True
        worker_live: dict[str, dict[str, str | None]] = {}
        for name, session in registered.items():
            backend_name = get_worker_backend(name, session)
            activity: str | None = None
            context_pct: str | None = None
            tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{name}"); host = get_worker_host(name)
            tmux_alive = "tmux" in session and tmux_exists(tmux_name, host=host)
            if tmux_alive:
                backend = get_backend(backend_name)
                if backend.is_interactive:
                    if is_claude_running(tmux_name, host=host): activity, context_pct, _ = _read_tmux_activity(tmux_name, host=host)
                    else: activity = "worker app not running"
                else: activity = _read_noninteractive_activity(name)
            worker_live[name] = { "backend": backend_name, "activity": activity, "context_pct": context_pct, }
        lines = format_team_lines(registered, state.active, worker_live=worker_live)
        self.reply(chat_id, "\n".join(lines))
        return True
    def cmd_status(self, chat_id: ChatId) -> bool:
        import time as _time
        registered = self.workers.get_registered_sessions()
        try:
            uptime_sec = int(_time.time() - os.stat(f"/proc/{os.getpid()}").st_mtime)
            d, rem = divmod(uptime_sec, 86400); h, rem = divmod(rem, 3600); m, _ = divmod(rem, 60)
            uptime_str = f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m" if h > 0 else f"{m}m"
        except (OSError, ValueError): uptime_str = "unknown"
        tm_status = tunnel_manager.status() if tunnel_manager else {}
        tunnel_url = tm_status.get("tunnel_url", "")
        inbound = f"webhook ({tunnel_url})" if tunnel_url else "poll" if tm_status.get("polling_active", False) else "DOWN"
        busy = 0; idle = 0; offline = 0; busy_names: list[str] = []
        for name, session in registered.items():
            if self.workers.is_online(name, session):
                wd = watchdog.worker_states.get(name)
                if wd and wd.status == "BUSY": busy += 1; busy_names.append(name)
                else: idle += 1
            else: offline += 1
        total = busy + idle + offline; machine_workers: dict[str, list[str]] = {}
        ssh_to_display: dict[str | None, str] = {None: "local"}
        for mach in get_machine_catalog().values():
            if mach.ssh_target: ssh_to_display[mach.ssh_target] = mach.display_name or mach.id
        for name in registered:
            host = get_worker_host(name); machine_workers.setdefault(ssh_to_display.get(host, host or "local"), []).append(name)
        conn_parts = [f"{cn} ({info.get('consecutive_failures', 0)} err)" if info.get("consecutive_failures", 0) else cn
            for cn, info in _get_connectors_status().items() if info.get("running", False)]
        lines = [f"<b>v{VERSION}</b> | up {uptime_str} | {inbound}", "",
            f"<b>{total}</b> workers: {busy} busy, {idle} idle, {offline} off", f"focus: <b>{state.active or 'none'}</b>"]
        if busy_names: lines.append(f"busy: {', '.join(sorted(busy_names))}")
        if len(machine_workers) > 1:
            lines.append(" / ".join(f"{label} {len(ws)}" for label, ws in sorted(machine_workers.items())))
        if conn_parts: lines.append(", ".join(conn_parts))
        if self.transport is not None and chat_id is not None: self.transport.send_text(chat_id, "\n".join(lines), parse_mode="HTML")
        return True
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
class TranscriptSyncRegistry:
    def __init__(self) -> None:
        self._state: dict[str, TranscriptSyncState] = {}
        self._lock: threading.Lock = threading.Lock()
    def get(self, key: str) -> TranscriptSyncState | None:
        with self._lock: return self._state.get(key)
    def set(self, key: str, value: TranscriptSyncState) -> None:
        with self._lock: self._state[key] = value
    def update(self, key: str, **fields: object) -> None:
        with self._lock:
            if key in self._state: self._state[key].update(fields)
    def get_started(self, key: str) -> float:
        with self._lock:
            entry = self._state.get(key, {})
            return entry.get("started", 0)
_transcript_sync = TranscriptSyncRegistry()
INDEXER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indexer.py")
def _run_transcript_query(jsonl_path: str, sid: str, query: str,
                          host: str | None = None, *,
                          page: int | None = None,
                          per_page: int | None = None,
                          search: str | None = None,
                          filter_mode: str | None = None,
                          sort: str | None = None) -> dict[str, object] | None:
    db_path = f"/tmp/transcript-cache/{sid}.db"; script_path = INDEXER_SCRIPT
    if host:
        remote_home = _get_remote_home(host) or ""
        if remote_home: script_path = f"{remote_home}/claudecode-telegram/indexer.py"
    cmd = ["python3", script_path, "transcript", "--jsonl", str(jsonl_path), "--db", db_path, "--query", query]
    if page is not None: cmd.extend(["--page", str(page)])
    if per_page is not None: cmd.extend(["--per-page", str(per_page)])
    if search: cmd.extend(["--search", search])
    if filter_mode: cmd.extend(["--filter", filter_mode])
    if sort and sort != "relevance": cmd.extend(["--sort", sort])
    try:
        if host: r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_LARGE_TRANSFER)
        else: r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0 and r.stdout.strip(): return cast(dict[str, object], json.loads(r.stdout))
    except (subprocess.SubprocessError, OSError) as e: _log(_LOG_ERROR, "transcript", f"Transcript query error: {e}")
    return None
def _start_transcript_sync(name: str, host: str, remote_path: str, local_tmp: Path, key: str) -> None:
    try:
        _transcript_sync.set(key, {"status": "syncing", "progress": "Connecting to remote host...",
                                   "started": _clock.time(), "path": None, "error": None})
        r = _subprocess_runner.run(["ssh", host, f"stat -f%z '{remote_path}' 2>/dev/null || stat -c%s '{remote_path}' 2>/dev/null"],
                           capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        remote_size = 0
        if r.returncode == 0 and r.stdout.strip().isdigit(): remote_size = int(r.stdout.strip())
        if remote_size > 0:
            size_mb = remote_size / 1_048_576
            _transcript_sync.update(key, progress=f"Syncing transcript ({size_mb:.1f} MB)...", remote_size=remote_size)
        else: _transcript_sync.update(key, progress="Syncing transcript...")
        proc = _subprocess_runner.popen(
            ["rsync", "-az", f"{host}:{remote_path}", str(local_tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while proc.poll() is None:
                _clock.sleep(DELAY_STARTUP)
                try:
                    if local_tmp.exists() and remote_size > 0:
                        local_size = local_tmp.stat().st_size; pct = min(99, int(local_size * 100 / remote_size))
                        _transcript_sync.update(key,
                            progress=f"Syncing... {pct}% ({local_size / 1_048_576:.1f} / {remote_size / 1_048_576:.1f} MB)",
                            pct=pct)
                except (OSError, ValueError) as exc: _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
            if proc.returncode == 0 and local_tmp.exists() and local_tmp.stat().st_size > 0:
                _transcript_sync.set(key, {"status": "done", "progress": "Ready", "path": str(local_tmp),
                                           "started": _transcript_sync.get_started(key), "error": None})
            else:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                _transcript_sync.set(key, {"status": "error", "progress": "Sync failed",
                                           "started": _transcript_sync.get_started(key),
                                           "path": None, "error": stderr[:200] or "rsync failed"})
        finally:
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        _transcript_sync.set(key, {"status": "error", "progress": "Sync failed",
                                   "started": _transcript_sync.get_started(key),
                                   "path": None, "error": str(e)[:200]})
def _resolve_transcript_path(name: str, session_id: str | None = None) -> tuple[str | None, str | None, str]:
    cwd = get_claude_session_cwd(name)
    if not cwd:
        host = get_worker_host(name); tmux_name = f"{TMUX_PREFIX}{name}"
        try:
            assert worker_manager is not None
            cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
        except Exception: pass
    cwd = cwd or os.path.expanduser("~"); sid = session_id or get_claude_session_id(name, authoritative=True)
    if not sid: return None, "", cwd
    slug = _project_slug(cwd); transcript_path = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"
    if not transcript_path.exists():
        reg = _load_registry().get("workers", {}); entry = reg.get(name, {}); host = entry.get("host")
        if host:
            try:
                remote_home = _get_remote_home(host)
                if remote_home:
                    remote_cwd = _remap_path(cwd, host)
                    remote_slug = _project_slug(remote_cwd)
                    remote_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                    local_tmp = Path(f"/tmp/transcript-{name}-{sid}.jsonl"); sync_key = f"{name}:{sid}"
                    sync_info = _transcript_sync.get(sync_key)
                    if sync_info and sync_info["status"] == "done" and local_tmp.exists(): transcript_path = local_tmp
                    elif sync_info and sync_info["status"] == "syncing": return "syncing", sid, cwd
                    elif sync_info and sync_info["status"] == "error":
                        err = sync_info.get("error", "unknown error")
                        _log(_LOG_WARN, "transcript", f"sync failed for {name}:{sid}: {err}")
                        return None, sid, cwd
                    else:
                        _task_pool.submit(_start_transcript_sync, name, host, remote_path, local_tmp, sync_key)
                        return "syncing", sid, cwd
            except (OSError, subprocess.SubprocessError) as exc: _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    if transcript_path.exists(): return str(transcript_path), sid, cwd
    return None, sid, cwd
def _parse_transcript_entries(transcript_path: str) -> list[TranscriptEntry]:
    entries = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: entry = cast(TranscriptEntry, json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError): continue
            etype = entry.get("type", "")
            if etype in ("progress", "queue-operation", "file-history-snapshot"): continue
            if etype == "system": continue
            entries.append(entry)
    return entries
def _generate_member_avatar(name: str) -> str:
    name_hash = hash(name) & 0xFFFFFFFF; hue = (name_hash % 36) * 10; sat = 55 + (name_hash >> 6 & 1) * 15
    shape_idx = (name_hash >> 7) % 6; accent_idx = (name_hash >> 10) % 5
    initials = name[:2].upper() if len(name) >= 2 else name.upper(); bg = f"hsl({hue},{sat}%,42%)"
    fg = f"hsl({hue},{max(sat-20,30)}%,75%)"
    shapes = [
        '<circle cx="14" cy="14" r="14"/>',
        '<rect x="1" y="1" width="26" height="26" rx="6"/>',
        '<polygon points="14,0 28,7 28,21 14,28 0,21 0,7"/>',
        '<polygon points="14,1 27,14 14,27 1,14"/>',
        '<polygon points="14,0 28,10 22,28 6,28 0,10"/>',
        '<rect x="0" y="0" width="28" height="28" rx="10"/>', ]
    accents = [
        '',
        f'<circle cx="14" cy="14" r="8" fill="none" stroke="{fg}" stroke-width="1.5" opacity=".3"/>',
        f'<circle cx="7" cy="7" r="2" fill="{fg}" opacity=".2"/><circle cx="21" cy="7" r="2" fill="{fg}" opacity=".2"/>',
        f'<line x1="4" y1="4" x2="24" y2="24" stroke="{fg}" stroke-width="1" opacity=".15"/><line x1="4" y1="24" x2="24" y2="4" stroke="{fg}" stroke-width="1" opacity=".15"/>',
        f'<rect x="4" y="12" width="20" height="4" rx="2" fill="{fg}" opacity=".15"/>', ]
    return (f'<div class="u-av"><svg viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">'
            f'<g fill="{bg}">{shapes[shape_idx]}</g>'
            f'{accents[accent_idx]}'
            f'<text x="14" y="14" text-anchor="middle" dominant-baseline="central" '
            f'fill="#fff" font-family="Inter,system-ui,sans-serif" font-size="11" font-weight="600" '
            f'opacity=".9">{initials}</text></svg></div>')
_TEAM_MEMBERS = {
    "chen", "geni", "hiro", "jin", "kai", "kelvin", "kenji",
    "lee", "luck", "mon", "noa", "ren", "ryo", "sora", "taro",
    "x", "yuki", "finn", }
_MANAGER_AV = '<div class="u-av"><img src="https://avatars.githubusercontent.com/u/4256921" alt="manager"></div>'
def _detect_message_author(text: str) -> AuthorDetection:
    stripped = text.strip(); colon_pos = stripped.find(":")
    if 0 < colon_pos <= 10:
        prefix = stripped[:colon_pos].lower().strip(); rest = stripped[colon_pos + 1:].strip()
        if prefix == "manager": return AuthorDetection("manager", _MANAGER_AV, rest or stripped)
        if prefix in _TEAM_MEMBERS: return AuthorDetection(prefix, _generate_member_avatar(prefix), rest or stripped)
    return AuthorDetection("manager", _MANAGER_AV, stripped)
_TRANSCRIPT_CHEVRON_SVG = '<svg class="chev" viewBox="0 0 16 16" fill="currentColor"><path d="M6.22 3.22a.75.75 0 011.06 0l4.25 4.25a.75.75 0 010 1.06l-4.25 4.25a.75.75 0 01-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 010-1.06z"/></svg>'
_TRANSCRIPT_CLAUDE_AVATAR = '<div class="cl-av"><svg viewBox="0 0 24 24" fill="none"><path d="M16.98 5.35L12 2L7.02 5.35L1.28 6.35L3.28 12.1L1.28 17.85L7.02 18.85L12 22.2L16.98 18.85L22.72 17.85L20.72 12.1L22.72 6.35L16.98 5.35Z" fill="currentColor"/></svg></div>'
_TRANSCRIPT_TOOL_SVGS: dict[str, str] = {
    "Read": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 1.5a.25.25 0 00-.25.25v11.5c0 .138.112.25.25.25h8.5a.25.25 0 00.25-.25V6H9.75A1.75 1.75 0 018 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.086c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v8.086A1.75 1.75 0 0112.25 15h-8.5A1.75 1.75 0 012 13.25V1.75z"/></svg>',
    "Write": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 1.5a.25.25 0 00-.25.25v11.5c0 .138.112.25.25.25h8.5a.25.25 0 00.25-.25V6H9.75A1.75 1.75 0 018 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.086c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v8.086A1.75 1.75 0 0112.25 15h-8.5A1.75 1.75 0 012 13.25V1.75z"/></svg>',
    "Edit": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M11.013 1.427a1.75 1.75 0 012.474 0l1.086 1.086a1.75 1.75 0 010 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 01-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61zm1.414 1.06a.25.25 0 00-.354 0L10.811 3.75l1.439 1.44 1.263-1.263a.25.25 0 000-.354l-1.086-1.086zM11.189 6.25L9.75 4.811l-6.286 6.287a.25.25 0 00-.064.108l-.558 1.953 1.953-.558a.249.249 0 00.108-.064l6.286-6.287z"/></svg>',
    "Bash": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0114.25 15H1.75A1.75 1.75 0 010 13.25V2.75zm1.75-.25a.25.25 0 00-.25.25v10.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25V2.75a.25.25 0 00-.25-.25H1.75zM7.25 8a.75.75 0 01-.22.53l-2.25 2.25a.75.75 0 11-1.06-1.06L5.44 8 3.72 6.28a.75.75 0 111.06-1.06l2.25 2.25c.141.14.22.331.22.53zm1.5 1.5a.75.75 0 000 1.5h3a.75.75 0 000-1.5h-3z"/></svg>',
    "Grep": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M10.68 11.74a6 6 0 01-7.922-8.982 6 6 0 018.982 7.922l3.04 3.04a.749.749 0 01-.326 1.275.749.749 0 01-.734-.215l-3.04-3.04zM11.5 7a4.499 4.499 0 10-8.997 0A4.499 4.499 0 0011.5 7z"/></svg>',
    "Glob": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1A1.75 1.75 0 000 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0016 13.25v-8.5A1.75 1.75 0 0014.25 3H7.5a.25.25 0 01-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1H1.75z"/></svg>',
    "Agent": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M6.5.75a.75.75 0 00-1.5 0V2H3.75A1.75 1.75 0 002 3.75V5h-.25a.75.75 0 000 1.5H2v3h-.25a.75.75 0 000 1.5H2v1.25c0 .966.784 1.75 1.75 1.75h8.5A1.75 1.75 0 0014 12.25V11h.25a.75.75 0 000-1.5H14v-3h.25a.75.75 0 000-1.5H14V3.75A1.75 1.75 0 0012.25 2H11V.75a.75.75 0 00-1.5 0V2h-3V.75z"/></svg>',
}
_TRANSCRIPT_DEFAULT_TOOL_SVG = '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304zM10.567 2.304A4.49 4.49 0 0112.5 6c0 1.598-.832 3.002-2.09 3.802-.518.328-.929.923-.902 1.64v.008l.164 3.337a.75.75 0 101.498-.073l-.163-3.34c-.007-.14.1-.313.357-.476A5.994 5.994 0 0114 6c0-2.033-1.01-3.83-2.555-4.916a1.89 1.89 0 00-.878 1.22z"/></svg>'
def _transcript_entry_to_html(entry: TranscriptEntry, esc: Callable[[str], str], tool_results: dict[str, ToolResultDict] | None = None) -> str:
    import base64 as _b64
    if tool_results is None: tool_results = {}
    etype = entry.get("type", ""); msg = entry.get("message", {}); role = msg.get("role", "")
    content = msg.get("content", ""); _chev = _TRANSCRIPT_CHEVRON_SVG; _claude_av = _TRANSCRIPT_CLAUDE_AVATAR
    _tool_svgs = _TRANSCRIPT_TOOL_SVGS; _default_tool_svg = _TRANSCRIPT_DEFAULT_TOOL_SVG
    parts: list[str] = []
    ts_raw = entry.get("timestamp", ""); ts_html = ""
    if ts_raw: ts_html = f'<span class="ts" data-ts="{esc(ts_raw)}">{esc(ts_raw[:16].replace("T"," "))}</span>'
    if etype == "user" and role == "user":
        if isinstance(content, str) and content.strip():
            raw = content.strip()
            if raw.startswith("<task-notification>") or raw.startswith("<system-reminder>"): return ""
            _author, _avatar, _display = _detect_message_author(raw)
            text = esc(_display)
            if len(text) > 2000: text = text[:2000] + "\n\n<em>… truncated</em>"
            _name_html = f'<span class="u-name">{esc(_author)}</span>' if _author != "manager" else ""
            parts.append(f'<div class="user-msg">{_avatar}<div class="u-body">{_name_html}<div class="u-text">{text}{ts_html}</div></div></div>')
        elif isinstance(content, list): pass
    elif etype == "assistant" and role == "assistant":
        if isinstance(content, list):
            for item in content:
                ct = str(item.get("type", ""))
                if ct == "thinking":
                    raw_thinking = str(item.get("thinking", ""))
                    if raw_thinking and raw_thinking.strip():
                        tt = esc(raw_thinking[:3000])
                        if len(raw_thinking) > 3000: tt += "\n… truncated"
                        parts.append(f'<details class="think"><summary class="think-h">{_chev} Thinking</summary><div class="think-t">{tt}</div></details>')
                elif ct == "text":
                    text = item.get("text", "")
                    if text and text != "(no content)":
                        b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
                        parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
                elif ct == "tool_use":
                    tn = item.get("name", "?"); ti = item.get("input", {})
                    tool_svg = _tool_svgs.get(tn, _default_tool_svg)
                    _INPUT_KEYS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path",
                                   "Glob": "pattern", "Bash": "command", "Grep": "pattern", "Search": "pattern"}
                    key = _INPUT_KEYS.get(tn)
                    if key: inp = ti.get(key, "")
                    elif tn == "Agent": inp = ti.get("description", "") or str(ti.get("prompt", ""))[:80]
                    else: inp = json.dumps(ti, ensure_ascii=False)[:200]
                    is_fp = tn in ("Read", "Write", "Edit") and inp and "/" in inp
                    inp = str(inp)[:300]; tool_id = item.get("id", ""); tr = tool_results.get(tool_id, {})
                    tr_text = tr.get("content", ""); tr_err = tr.get("is_error", False)
                    has_result = bool(tr_text and tr_text.strip() and tr_text != "Bash completed with no output")
                    tr_esc = ""
                    if has_result:
                        tr_str = str(tr_text)[:5000]; tr_esc = esc(tr_str)
                        if len(str(tr_text)) > 5000: tr_esc += "\n… truncated"
                    err_cls = " act-err" if tr_err else ""
                    if tn == "Edit" and ti.get("old_string") is not None:
                        fp = esc(ti.get("file_path", "?")); old = ti.get("old_string", "").splitlines(True); new = ti.get("new_string", "").splitlines(True)
                        dl = [f'<div class="diff-del"><span class="diff-ln">{i+1}</span><span class="diff-sign">-</span>{esc(l.rstrip())}</div>' for i, l in enumerate(old[:60])]
                        dl += [f'<div class="diff-add"><span class="diff-ln">{i+1}</span><span class="diff-sign">+</span>{esc(l.rstrip())}</div>' for i, l in enumerate(new[:60])]
                        if len(old) > 60 or len(new) > 60: dl.append('<div class="diff-ctx"><span class="diff-ln"></span><span class="diff-sign"> </span>… truncated</div>')
                        no, na, nm = len(old), len(new), min(len(old), len(new))
                        stats = f'<span class="diff-stat"><span class="diff-plus">+{na-nm}</span> <span class="diff-minus">-{no-nm}</span> <span class="diff-mod">~{nm}</span></span>'
                        pp = fp.rsplit("/", 1); fp_h = f'<span class="fp-dir">{esc(pp[0])}/</span>{esc(pp[1])}' if len(pp) > 1 else fp
                        parts.append(f'<details class="act diff-act{err_cls}"><summary class="act-h">{tool_svg}<span class="fp">{fp_h}</span>{stats}{_chev}</summary><div class="diff-body">{chr(10).join(dl)}</div></details>')
                    elif tn == "Bash" and inp:
                        body_html = f'<div class="act-cmd">{esc(inp)}</div>'
                        if tr_esc: body_html += f'<div class="act-out{" act-out-err" if tr_err else ""}">{tr_esc}</div>'
                        parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="t-det">{esc(inp[:80])}</span>{_chev}</summary><div class="act-body">{body_html}</div></details>')
                    else:
                        if is_fp:
                            pp = inp.rsplit("/", 1); dp = esc(pp[0]) if len(pp) > 1 else ""; bp = esc(pp[-1])
                            inner = f'<span class="fp-dir">{dp}/</span>{bp}' if dp else bp
                            label = f'<span class="fp">{inner}</span>'
                        else: label = f'<span class="t-det">{esc(inp[:80])}</span>'
                        if tr_esc and has_result:
                            parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}{label}{_chev}</summary><div class="act-body"><pre class="t-out">{tr_esc}</pre></div></details>')
                        else: parts.append(f'<div class="chip">{tool_svg}{label}</div>')
        elif isinstance(content, str) and content.strip():
            b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
            parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
    return "\n".join(parts)
def _format_model_name(model_name: str) -> str:
    import re as _re
    s = model_name.replace("claude-", ""); s = _re.sub(r"-\d{8}$", "", s); s = _re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", s)
    s = _re.sub(r"-(\d+)\.(\d+)$", r" \1.\2", s); s = s.replace("-", " ").title()
    return s
def _transcript_stats(entries: list[TranscriptEntry]) -> TranscriptStatsDict:
    n_user = sum(1 for e in entries if e.get("type") == "user"
                 and e.get("message", {}).get("role") == "user"
                 and isinstance(e.get("message", {}).get("content"), str))
    n_tool = 0; n_edit = 0; lines_add = 0; lines_del = 0; lines_mod = 0; files_modified: set[str] = set()
    for e in entries:
        if e.get("type") != "assistant": continue
        for c in (e.get("message", {}).get("content") or []):
            if not isinstance(c, dict) or c.get("type") != "tool_use": continue
            n_tool += 1; ti = c.get("input", {}); cname = c.get("name")
            fp = ti.get("file_path", "")
            if cname == "Edit" and ti.get("old_string") is not None:
                n_edit += 1; n_old = len(ti.get("old_string", "").splitlines(True)); n_new = len(ti.get("new_string", "").splitlines(True))
                overlap = min(n_old, n_new); lines_mod += overlap; lines_del += n_old - overlap; lines_add += n_new - overlap
            if cname in ("Write", "Read", "Edit") and fp: files_modified.add(fp)
    model = version = git_branch = first_ts = last_ts = ""; input_tokens = output_tokens = 0
    for e in entries:
        if not model: model = e.get("message", {}).get("model", "")
        if not version: version = e.get("version", "")
        if not git_branch: git_branch = e.get("gitBranch", "")
        ts = e.get("timestamp", "")
        if ts:
            if not first_ts: first_ts = ts
            last_ts = ts
        usage = e.get("message", {}).get("usage", {})
        input_tokens += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        output_tokens += usage.get("output_tokens", 0)
    duration_str = ""
    if first_ts and last_ts:
        try:
            from datetime import datetime
            total_s = max(0, int((datetime.fromisoformat(last_ts.replace("Z", "+00:00")) - datetime.fromisoformat(first_ts.replace("Z", "+00:00"))).total_seconds()))
            d, rem = divmod(total_s, 86400); h = rem // 3600; m = (rem % 3600) // 60
            duration_str = f"{d}d {h}h" if d > 0 else f"{h}h {m}m" if h > 0 else f"{m}m"
        except (ValueError, TypeError, KeyError) as exc: _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
    return {"n_user": n_user, "n_tool": n_tool, "n_edit": n_edit, "lines_add": lines_add, "lines_del": lines_del, "lines_mod": lines_mod, "n_files": len(files_modified), "model": model, "version": version, "git_branch": git_branch, "first_ts": first_ts, "last_ts": last_ts, "input_tokens": input_tokens, "output_tokens": output_tokens, "duration": duration_str}
_LOADING_CSS = "body{font-family:-apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0b0d0b;color:#e5e5e0}.card{text-align:center;max-width:420px;padding:40px;width:100%}h1{font-size:1.3rem;margin-bottom:8px;font-weight:600}.sub{color:#878b86;font-size:.9rem;margin-bottom:24px}.bar{background:#1a1c1a;border-radius:6px;height:8px;overflow:hidden;margin:16px 0}.bar-fill{background:#22c55e;height:100%;border-radius:6px;transition:width .5s ease}.bar-fill.err{background:#ef4444}.progress{color:#a0a4a0;font-size:.85rem;margin:8px 0}.elapsed{color:#5a5e5a;font-size:.8rem;margin-top:4px}.err-msg{color:#ef4444;font-size:.85rem;margin-top:12px}.spinner{display:inline-block;width:20px;height:20px;border:2px solid #2a2c2a;border-top-color:#22c55e;border-radius:50%;animation:spin 1s linear infinite;vertical-align:middle;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}"
def _render_transcript_loading(name: str, sid: str | None, token: str, sync_key: str) -> str:
    import html as html_mod
    esc = html_mod.escape; info = _transcript_sync.get(sync_key) or {}; status = info.get("status", "syncing")
    progress = esc(info.get("progress", "Starting sync...")); pct = info.get("pct", 0)
    elapsed = int(_clock.time() - info.get("started", _clock.time())); error = info.get("error")
    if status == "error": bar_html = '<div class="bar-fill err" style="width:100%"></div>'; msg = f'<p class="err-msg">Error: {esc(error or "Unknown error")}</p>'; meta_js = ""
    else: bar_html = f'<div class="bar-fill" style="width:{pct}%"></div>'; msg = ""; meta_js = '<meta http-equiv="refresh" content="2">'
    spin = '<span class="spinner"></span>' if status == "syncing" else ""
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Loading {esc(name)}</title>{meta_js}<style>{_LOADING_CSS}</style></head><body><div class="card"><h1>Preparing Transcript</h1><p class="sub">{esc(name)}</p><div class="bar">{bar_html}</div><p class="progress">{spin}{progress}</p><p class="elapsed">{elapsed}s elapsed</p>{msg}</div></body></html>'
def _transcript_html_head(name: str, esc: Callable[[str], str]) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(name)} — Transcript</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">'
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" media="(prefers-color-scheme: dark)">'
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css" media="(prefers-color-scheme: light)">'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.1/marked.min.js"></script>'
            '<style>' + _TRANSCRIPT_CSS + '</style></head><body><div class="wrap"><div class="main"><div class="content">')
def _transcript_html_nav(name: str, stats: TranscriptStatsDict,
                         prompts_filter_url: str,
                         esc: Callable[[str], str]) -> str:
    _info_svg = '<svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16"><path d="M0 8a8 8 0 1116 0A8 8 0 010 8zm8-6.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM6.5 7.75A.75.75 0 017.25 7h1a.75.75 0 01.75.75v2.75h.25a.75.75 0 010 1.5h-2a.75.75 0 010-1.5h.25v-2h-.25a.75.75 0 01-.75-.75zM8 6a1 1 0 110-2 1 1 0 010 2z"/></svg>'
    _clock_svg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 16A8 8 0 108 0a8 8 0 000 16zm.25-11.75v4l3 1.5-.5 1-3.5-1.75v-4.75h1z"/></svg>'
    _model_svg = "<svg viewBox='0 0 16 16' fill='currentColor'><path d='M8 1.5c-2.363 0-4 1.69-4 3.75 0 .984.424 1.625.984 2.304l.214.253c.223.264.47.556.673.848.284.411.537.896.621 1.49a.75.75 0 01-1.484.211c-.04-.282-.163-.547-.37-.847a8.456 8.456 0 00-.542-.68c-.084-.1-.173-.205-.268-.32C3.201 7.75 2.5 6.766 2.5 5.25 2.5 2.31 4.863 0 8 0s5.5 2.31 5.5 5.25c0 1.516-.701 2.5-1.328 3.259-.095.115-.184.22-.268.319-.207.245-.383.453-.541.681-.208.3-.33.565-.37.847a.75.75 0 01-1.485-.212c.084-.593.337-1.078.621-1.489.203-.292.45-.584.673-.848l.213-.253c.561-.679.985-1.32.985-2.304 0-2.06-1.637-3.75-4-3.75zM6 15.25a.75.75 0 01.75-.75h2.5a.75.75 0 010 1.5h-2.5a.75.75 0 01-.75-.75zM5.75 12a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-4.5z'/></svg>"
    _branch_svg = "<svg viewBox='0 0 16 16' fill='currentColor'><path d='M11.93 8.5a4.002 4.002 0 01-7.86 0H.75a.75.75 0 010-1.5h3.32a4.002 4.002 0 017.86 0h3.32a.75.75 0 010 1.5h-3.32zm-1.43-.75a2.5 2.5 0 10-5 0 2.5 2.5 0 005 0z'/></svg>"
    _chat_svg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0110.25 10H7.061l-2.574 2.573A1.458 1.458 0 012 11.543V10h-.25A1.75 1.75 0 010 8.25v-5.5C0 1.784.784 1 1.75 1z"/></svg>'
    _tool_svg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304z"/></svg>'
    meta = []
    if stats["first_ts"]: meta.append(f"<span class='mi'>{_clock_svg}{esc(stats['first_ts'][:10])}</span>")
    if stats["model"]: meta.append(f"<span class='mi'>{_model_svg}{esc(stats['model'])}</span>")
    if stats["git_branch"]: meta.append(f"<span class='mi'>{_branch_svg}{esc(stats['git_branch'])}</span>")
    meta.append(f'<a class="mi" href="{prompts_filter_url}" style="cursor:pointer" title="Filter to prompts only">{_chat_svg}{stats["n_user"]} prompts</a>')
    meta.append(f'<span class="mi">{_tool_svg}{stats["n_tool"]} tool call{"s" if stats["n_tool"] != 1 else ""}</span>')
    return (f'<header><div class="h-row"><h1>{esc(name)}</h1><button class="sb-toggle" onclick="document.querySelector(\'.sidebar\').classList.toggle(\'sb-open\')" title="Session info">{_info_svg}</button></div>'
            f'<div class="meta">{"".join(meta)}</div></header>')
def _transcript_html_entries(page_entries: list[TranscriptEntry],
                             tool_results: dict[str, ToolResultDict],
                             search_val: str,
                             filter_banner: str, search_result: str,
                             nav_html: str, live_base_url: str,
                             name: str, token: str, search_query: str,
                             total: int, per_page: int, page: int,
                             total_pages: int,
                             esc: Callable[[str], str],
                             session_id: str | None,
                             filter_mode: str) -> str:
    _tool_results = tool_results; blocks = []; in_assistant_turn = False
    _url_prefix = live_base_url + "?" if live_base_url else "?"
    def _ctx_url(entry: TranscriptEntry) -> str:
        if not search_query: return ""
        idx = entry.get("_idx", -1)
        if idx < 0: return ""
        ctx_page = (idx // per_page) + 1; ctx_qs = []
        if token: ctx_qs.append(f"token={esc(token)}")
        if session_id: ctx_qs.append(f"sid={esc(session_id)}")
        if per_page != 50: ctx_qs.append(f"per_page={per_page}")
        ctx_qs.append(f"page={ctx_page}")
        return f'{_url_prefix}{"&".join(ctx_qs)}#e-{idx}'
    for entry in page_entries:
        etype = entry.get("type", ""); role = entry.get("message", {}).get("role", "")
        is_tool_result = (etype == "user" and role == "user" and
                          isinstance(entry.get("message", {}).get("content"), list) and
                          any(c.get("type") == "tool_result" for c in entry.get("message", {}).get("content", []) if isinstance(c, dict)))
        if is_tool_result: continue
        entry_html = _transcript_entry_to_html(entry, esc, tool_results=_tool_results)
        if not entry_html: continue
        eidx = entry.get("_idx", -1); anchor = f' id="e-{eidx}"' if eidx >= 0 else ""; curl = _ctx_url(entry)
        is_assistant = (etype == "assistant" and role == "assistant")
        if is_assistant:
            if curl:
                if in_assistant_turn:
                    blocks.append('</div>')
                    in_assistant_turn = False
                entry_html = f'<a class="ctx-wrap" href="{curl}"{anchor}>{entry_html}</a>'
                blocks.append(entry_html)
            else:
                if not in_assistant_turn:
                    blocks.append(f'<div class="turn-body"{anchor}>')
                    in_assistant_turn = True
                blocks.append(entry_html)
        else:
            if in_assistant_turn:
                blocks.append('</div>')
                in_assistant_turn = False
            if curl: entry_html = f'<a class="ctx-wrap" href="{curl}"{anchor}>{entry_html}</a>'
            elif anchor: entry_html = f'<div{anchor}>{entry_html}</div>'
            blocks.append(entry_html)
    if in_assistant_turn: blocks.append('</div>')
    hidden = "".join(f"<input type='hidden' name='{k}' value='{esc(str(v))}'>" for k, v in [("token", token), ("sid", session_id), ("per_page", per_page if per_page != 50 else ""), ("filter", "prompts" if filter_mode == "prompts" else "")] if v)
    action = f' action="{esc(live_base_url)}"' if live_base_url else ''
    upd_url = esc(live_base_url) + '/updates' if live_base_url else '/transcript/' + esc(name) + '/updates'
    pg_url = esc(live_base_url) if live_base_url else '/transcript/' + esc(name)
    search_attr = f' data-search="{esc(search_query)}"' if search_query else ''
    return (f'<form class="search-bar" method="get"{action}><input type="text" name="q" placeholder="Search transcript…" value="{search_val}"><button type="submit">Search</button>{hidden}</form>'
            f'{filter_banner}{search_result}{nav_html}'
            f'<div id="live-banner" class="live-banner" style="display:none"></div>'
            f'<div class="thread" id="thread"{search_attr} data-total="{total}" data-name="{esc(name)}" data-token="{esc(token)}" data-updates-url="{upd_url}" data-page-url="{pg_url}" data-per-page="{per_page}" data-page="{page}" data-total-pages="{total_pages}">'
            f'{"".join(blocks)}</div>{nav_html}')
def _transcript_html_footer(sid: str, stats: TranscriptStatsDict,
                              file_size_str: str, total: int,
                              page: int, total_pages: int,
                              esc: Callable[[str], str]) -> str:
    def _row(label: str, val: str) -> str: return f"<div class='sb-row'><span class='sb-label'>{label}</span><span class='sb-val'>{val}</span></div>"
    _d = '<div class="sb-divider"></div>'
    rows = [_row("Session", esc(sid[:12]))]
    if stats["model"]: rows.append(_row("Model", esc(_format_model_name(stats["model"]))))
    if stats["version"]: rows.append(_row("Version", esc(stats["version"])))
    if stats["git_branch"]: rows.append(_row("Branch", esc(stats["git_branch"])))
    rows.append(_d)
    rows += [_row("Prompts", str(stats["n_user"])), _row("Tool calls", str(stats["n_tool"]))]
    if stats["n_edit"]: rows.append(_row("Edits", str(stats["n_edit"])))
    if stats["n_files"]: rows.append(_row("Files touched", str(stats["n_files"])))
    if stats["lines_add"] or stats["lines_del"] or stats["lines_mod"]:
        rows.append(_d + f'<div class="sb-lines"><span class="plus">+{stats["lines_add"]}</span><span class="minus">-{stats["lines_del"]}</span><span class="mod">~{stats["lines_mod"]}</span></div>')
    if stats["duration"] or file_size_str or stats["input_tokens"]: rows.append(_d)
    if stats["duration"]: rows.append(_row("Duration", esc(stats["duration"])))
    if file_size_str: rows.append(_row("File size", esc(file_size_str)))
    if stats["input_tokens"]: rows.append(_row("Input tokens", f'{stats["input_tokens"]:,}'))
    if stats["output_tokens"]: rows.append(_row("Output tokens", f'{stats["output_tokens"]:,}'))
    rows += [_d, _row("Total entries", str(total)), _row("Page", f"{page}/{total_pages}")]
    sb = "\n".join(rows)
    return f'</div><aside class="sidebar"><div class="sidebar-inner"><div class="sb-title">Session Info</div>{sb}</div></aside></div></div><div class="jump"><a href="#" title="Top" onclick="window.scrollTo(0,0);return false">↑</a><a href="#" title="Bottom" onclick="window.scrollTo(0,document.body.scrollHeight);return false">↓</a></div>'
def _transcript_html_search_js() -> str:
    return '<script>\n' + _TRANSCRIPT_JS + '\n</script>\n</body>\n</html>\n'
def _err_page(title: str, detail: str = "") -> str:
    d = f"<p>{detail}</p>" if detail else ""
    return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>{title}</h1>{d}</body></html>"
def _render_transcript_html(name: str, session_id: str | None = None,
                            page: int | None = None, per_page: int = 50,
                            search_query: str = "", token: str = "",
                            filter_mode: str = "", search_sort: str = "relevance",
                            live_base_url: str = "") -> str:
    import html as html_mod
    esc = html_mod.escape; host = get_worker_host(name)
    if host:
        cwd = get_claude_session_cwd(name) or ""
        sid: str | None = session_id or get_claude_session_id(name, authoritative=True)
        if not sid:
            return _err_page(f"No session found for {esc(name)}")
        remote_home = _get_remote_home(host) or ""
        if not remote_home:
            return _err_page(f"Cannot resolve remote home for {esc(name)}")
        remote_cwd = _remap_path(cwd, host)
        remote_slug = _project_slug(remote_cwd)
        jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"; transcript_path = None
    else:
        transcript_path, sid, cwd = _resolve_transcript_path(name, session_id)
        if not sid:
            return _err_page(f"No session found for {esc(name)}")
        if not transcript_path or transcript_path == "syncing":
            return _err_page("Transcript not found", f"Worker: {esc(name)}, Session: {esc(sid)}")
        jsonl_path = str(transcript_path)
    query_result = (_run_transcript_query(jsonl_path, sid, "search+stats", host=host,
        search=search_query, page=page or 1, per_page=per_page, sort=search_sort) if search_query else
        _run_transcript_query(jsonl_path, sid, "entries+stats", host=host,
        page=page, per_page=per_page, filter_mode=filter_mode))
    stats_result = query_result.pop("stats", None) if query_result else None
    if not query_result:
        if not transcript_path or not Path(str(transcript_path)).exists():
            return _err_page("Transcript not available", f"Worker: {esc(name)}, Session: {esc(sid)}")
        all_entries = _parse_transcript_entries(transcript_path); total = len(all_entries)
        for _i, _e in enumerate(all_entries): _e["_idx"] = _i
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page is None: page = total_pages
        page = max(1, min(page, total_pages)); start = (page - 1) * per_page
        page_entries = all_entries[start:start + per_page]; stats = _transcript_stats(all_entries); file_size_str = ""
    else:
        page_entries = []; _entries_raw = query_result.get("entries", [])
        for e in (_entries_raw if isinstance(_entries_raw, list) else []):
            try:
                entry = cast(TranscriptEntry, json.loads(e["raw_json"]))
                entry["_idx"] = e.get("idx", -1)
                page_entries.append(entry)
            except (json.JSONDecodeError, KeyError): continue
        if search_query: total = _int_field(query_result, "total_results")
        else: total = _int_field(query_result, "total")
        total_pages = _int_field(query_result, "total_pages", 1); page = _int_field(query_result, "page", 1)
        _empty_stats: TranscriptStatsDict = {"n_user": 0, "n_tool": 0, "n_edit": 0, "lines_add": 0, "lines_del": 0, "lines_mod": 0, "n_files": 0, "model": "", "version": "", "git_branch": "", "first_ts": "", "last_ts": "", "input_tokens": 0, "output_tokens": 0, "duration": ""}
        stats = cast(TranscriptStatsDict, stats_result) if stats_result else _empty_stats
    file_size_str = ""
    if not host:
        try:
            file_size_bytes = os.path.getsize(transcript_path) if transcript_path else 0
            if file_size_bytes >= 1_048_576: file_size_str = f"{file_size_bytes / 1_048_576:.1f} MB"
            elif file_size_bytes >= 1024: file_size_str = f"{file_size_bytes / 1024:.0f} KB"
            else: file_size_str = f"{file_size_bytes} B"
        except OSError as exc: _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
    _tool_results: dict[str, ToolResultDict] = {}
    for entry in page_entries:
        ct = entry.get("message", {}).get("content", []) if entry.get("type") == "user" else []
        if not isinstance(ct, list): continue
        for item in ct:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                rt = item.get("content", ""); rt = "\n".join(str(r.get("text", "")) for r in rt if isinstance(r, dict) and r.get("type") == "text") if isinstance(rt, list) else str(rt)
                _tool_results[str(item.get("tool_use_id", ""))] = cast(ToolResultDict, {"content": rt, "is_error": bool(item.get("is_error"))})
    _url_prefix = live_base_url + "?" if live_base_url else "?"
    def _qs(*extras: str) -> str:
        return "&".join([p for p in [f"token={esc(token)}" if token else "", f"sid={esc(session_id)}" if session_id else "", f"per_page={per_page}" if per_page != 50 else ""] if p] + list(extras))
    qs_base = _qs(*(([f"q={esc(search_query)}"] if search_query else []) + ([f"filter={esc(filter_mode)}"] if filter_mode else [])))
    def page_url(p: int | str) -> str:
        return _url_prefix + (f"page={p}&{qs_base}" if qs_base else f"page={p}")
    nav_html = ""
    if total_pages > 1:
        def _pg(label: str, p: int, dis: bool = False, cur: bool = False) -> str:
            cls = " pg-dis" if dis else (" pg-cur" if cur else "")
            return f'<a class="pg-btn{cls}" href="{page_url(p)}">{label}</a>'
        start_p = max(1, min(page - 3, total_pages - 6)); end_p = min(total_pages, start_p + 6)
        nav_items = [_pg("First", 1, page <= 1), _pg("Prev", page - 1, page <= 1)]
        nav_items += [_pg(str(p), p, cur=(p == page)) for p in range(start_p, end_p + 1)]
        nav_items += [_pg("Next", page + 1, page >= total_pages), _pg("Last", total_pages, page >= total_pages)]
        nav_html = f'<nav class="pg">{"".join(nav_items)}<span class="pg-info">Page {page}/{total_pages} ({total} entries)</span></nav>'
    search_val = esc(search_query) if search_query else ""; search_result = ""
    if search_query:
        sort_label = "by time" if search_sort == "time" else "by relevance"
        alt_sort = "time" if search_sort == "relevance" else "relevance"
        sort_url = _url_prefix + _qs(f"q={esc(search_query)}", f"sort={alt_sort}")
        search_result = (
            f'<div class="search-info">Found {total} matching entries for "<strong>{esc(search_query)}</strong>" '
            f'(sorted {sort_label}) &middot; <a href="{sort_url}">sort by {alt_sort}</a></div>' )
    filt_qs = _qs("filter=prompts") if filter_mode != "prompts" else _qs()
    prompts_filter_url = _url_prefix + filt_qs if filt_qs else _url_prefix.rstrip("?"); filter_banner = ""
    if filter_mode == "prompts":
        clear_url = _url_prefix + _qs() if _qs() else _url_prefix.rstrip("?")
        filter_banner = f'<div class="search-info">Showing prompts only — <a href="{clear_url}">show all</a></div>'
    return (_transcript_html_head(name, esc)
            + _transcript_html_nav(name, stats, prompts_filter_url, esc)
            + _transcript_html_entries(
                page_entries, _tool_results, search_val,
                filter_banner, search_result, nav_html,
                live_base_url, name, token, search_query, total, per_page,
                page, total_pages, esc, session_id, filter_mode)
            + _transcript_html_footer(sid, stats, file_size_str, total, page, total_pages, esc)
            + _transcript_html_search_js())
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
    def _guest_auth(self, parsed: ParseResult | None = None) -> GuestSessionDict | None:
        query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(self.path).query)
        token = query_params.get("token", [""])[0]
        if not token: self._send_error_json(403, "token required"); return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with guest_store.lock: guest = guest_store.guests.get(token_hash)
        if not guest or guest_is_expired(guest["expires_at_unix"]):
            if guest:
                with guest_store.lock:
                    guest_store.guests.pop(token_hash, None)
                    _guest_save()
            self._send_error_json(403, "invalid or expired guest session")
            return None
        return guest
    def _channel_guest_auth(self, query_params: dict[str, list[str]]) -> str | None:
        token = query_params.get("token", [None])[0]
        if not token: return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with guest_store.lock: guest = guest_store.guests.get(token_hash)
        if not guest or guest_is_expired(guest["expires_at_unix"]):
            self._send_error_json(403, "invalid or expired token"); return ""
        return f"guest:{guest['name']}"
    def handle_guest_register(self, body: bytes = b"") -> None:
        try: data = cast(dict[str, object], json.loads(body)) if body else {}
        except (json.JSONDecodeError, ValueError): data = {}
        requested_name = _str_field(data, "name").strip().lower(); team_workers = set(get_registered_sessions().keys())
        with guest_store.lock: existing_guests = {g["name"] for g in guest_store.guests.values()}
        if requested_name:
            ok, err = guest_validate_name(requested_name, team_workers, existing_guests)
            if not ok:
                status = 409 if "conflicts" in err or "already taken" in err else 400
                self._send_json(status, {"ok": False, "error": err})
                return
            name = requested_name
        else: name = guest_generate_name(existing_names=team_workers | existing_guests)
        token, token_hash = guest_create_token()
        now = _clock.time(); expires_at_unix = now + GUEST_TTL
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_unix))
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        guest = cast(GuestSessionDict, {"name": name, "token_hash": token_hash, "created_at": created_at,
            "expires_at": expires_at, "expires_at_unix": expires_at_unix, "notified_workers": set()})
        with guest_store.lock: guest_store.guests[token_hash] = guest; guest_store.inboxes[name] = []; _guest_save()
        base_url = _relay_base_url()
        listen_script = (f'python3 -c "import json,time,urllib.request as u;TOKEN,URL=\'{token}\',\'{base_url}\';last=\'\';'
                         f'print(\'[listener] connected\',flush=True)\nwhile True:\n try:\n  q=URL+\'/guests/inbox?token=\'+TOKEN+('
                         f'\'&after=\'+last if last else \'\');d=json.loads(u.urlopen(u.Request(q),timeout=10).read())\n  for m in d.get(\'messages\',[]):'
                         f'\n   print(m.get(\'from\',\'?\')+\': \'+m[\'text\'],flush=True);last=m[\'id\']\n except Exception as e:\n  '
                         f'if \'403\' in str(e) or \'404\' in str(e):print(\'[listener] expired\',flush=True);break\n time.sleep(3)"')
        _notify_admin(f"\U0001f514 Guest \"{name}\" connected"); _log(_LOG_INFO, "guest", f"Guest registered: {name} (expires {expires_at})")
        self._send_json(200, {"ok": True, "name": name, "token": token, "expires": expires_at,
            "inbox_url": f"/guests/inbox?token={token}", "send_url": f"/guests/send?token={token}",
            "channels_url": f"/channels?token={token}", "channel_create_url": f"/channels?token={token}",
            "listen_script": listen_script})
    def handle_guest_send(self, body: bytes = b"") -> None:
        parsed = urlparse(self.path); guest = self._guest_auth(parsed)
        if not guest: return
        data = self._parse_body(body)
        if data is None: return
        text = _str_field(data, "text").strip()
        if not text: self._send_error_json(400, "text required"); return
        raw_to = data.get("to", _str_field(data, "worker"))
        if isinstance(raw_to, str): targets = [raw_to.strip()] if raw_to.strip() else []
        elif isinstance(raw_to, list): targets = [t.strip() for t in raw_to if isinstance(t, str) and t.strip()]
        else: targets = []
        if not targets: self._send_error_json(400, "to (or worker) required"); return
        guest_name = guest["name"]; from_member = f"guest:{guest_name}"; tagged_text = f"[guest:{guest_name}] {text}"
        registered = get_registered_sessions(); results = []
        for target in targets:
            if target.startswith("#") or target.startswith("ch_"):
                with channel_store.lock:
                    if target.startswith("#"):
                        ch_id = next((cid for cid, ch in channel_store.channels.items()
                                      if ch["label"] == target[1:] and not channel_is_expired(ch)), None)
                    else: ch_id = target
                    channel = channel_store.channels.get(ch_id) if ch_id else None
                    if not channel or channel_is_expired(channel):
                        results.append({"target": target, "ok": False, "error": "channel not found"})
                        continue
                    if from_member not in channel["members"]:
                        results.append({"target": target, "ok": False, "error": "not a member"})
                        continue
                    msg = channel_append_message(channel, from_member, text)
                    members_snapshot = dict(channel["members"])
                _fanout_channel_message(ch_id, from_member, text, msg, members_snapshot, registered)
                results.append({"target": target, "ok": True, "channel": ch_id, "message_id": msg["id"]})
            elif target.startswith("guest:"):
                target_guest = target.split(":", 1)[1]; msg_id = f"gm_{secrets.token_hex(4)}"
                with guest_store.lock:
                    ginbox = guest_store.inboxes.get(target_guest, [])
                    guest_store.inboxes[target_guest] = guest_inbox_append(ginbox, {
                        "id": msg_id, "from": from_member,
                        "text": text, "ts": int(_clock.time()),
                    })
                results.append({"target": target, "ok": True, "message_id": msg_id})
            else:
                worker = target
                if worker not in registered:
                    results.append({"target": worker, "ok": False, "error": f"worker '{worker}' not found"})
                    continue
                info = registered[worker]; backend_name = get_worker_backend(worker, info)
                backend = get_backend(backend_name); tmux_name = f"{TMUX_PREFIX}{worker}"
                delivered = backend.send(worker, tmux_name, tagged_text, f"http://localhost:{PORT}", SESSIONS_DIR)
                msg_id = f"gm_{secrets.token_hex(4)}"
                with guest_store.lock:
                    inbox = guest_store.inboxes.get(guest_name, [])
                    guest_store.inboxes[guest_name] = guest_inbox_append(inbox, {
                        "id": msg_id, "from": guest_name, "to": worker,
                        "text": text, "ts": int(_clock.time()),
                    })
                    notified = guest.get("notified_workers", set())
                    if worker not in notified:
                        if isinstance(notified, set): notified.add(worker)
                        guest["notified_workers"] = notified
                        _notify_admin(f"\U0001f514 Guest \"{guest_name}\" → {worker}")
                results.append({"target": worker, "ok": True, "delivered": delivered, "message_id": msg_id})
        if len(results) == 1: self._send_json(200, {**results[0], "ok": results[0].get("ok", True)})
        else: self._send_json(200, {"ok": all(r.get("ok") for r in results), "results": results})
    def handle_guest_reply(self, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None: return
        guest_name = _str_field(data, "guest").strip(); from_worker = _str_field(data, "from").strip()
        text = _str_field(data, "text").strip()
        if not guest_name or not text: self._send_error_json(400, "guest and text required"); return
        msg_id = f"gm_{secrets.token_hex(4)}"
        with guest_store.lock:
            if guest_name not in guest_store.inboxes: guest_store.inboxes[guest_name] = []
            guest_store.inboxes[guest_name] = guest_inbox_append(
                guest_store.inboxes[guest_name],
                {"id": msg_id, "from": from_worker or "worker", "text": text, "ts": int(_clock.time())}, )
        self._send_json(200, {"ok": True, "message_id": msg_id})
    def handle_guest_inbox(self, parsed: ParseResult) -> None:
        guest = self._guest_auth(parsed)
        if not guest: return
        query_params = parse_qs(parsed.query); after = query_params.get("after", [None])[0]; guest_name = guest["name"]
        with guest_store.lock: msgs = list(guest_store.inboxes.get(guest_name, []))
        filtered = guest_inbox_filter(msgs, after=after)
        self._send_json(200, { "ok": True, "name": guest_name, "messages": filtered,
        })
    def handle_guest_status(self, parsed: ParseResult) -> None:
        guest = self._guest_auth(parsed)
        if not guest: return
        with guest_store.lock: workers = list(guest.get("notified_workers", set()))
        self._send_json(200, {"ok": True, "name": guest["name"], "expires": guest["expires_at"], "connected_workers": workers})
    def handle_guests_list(self) -> None:
        with guest_store.lock:
            guests_list = []; expired = []
            for th, g in guest_store.guests.items():
                if guest_is_expired(g["expires_at_unix"]): expired.append(th)
                else: guests_list.append({"name": g["name"], "expires": g["expires_at"], "connected_workers": list(g.get("notified_workers", set()))})
            for th in expired: guest_store.inboxes.pop(guest_store.guests.pop(th, {}).get("name", ""), None)
            if expired: _guest_save()
        self._send_json(200, {"ok": True, "guests": guests_list})
    def handle_guest_disconnect(self, parsed: ParseResult) -> None:
        query_params = parse_qs(parsed.query); token = query_params.get("token", [""])[0]
        if not token: self._send_error_json(403, "token required"); return
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with guest_store.lock:
            guest = guest_store.guests.pop(token_hash, None)
            if guest: guest_store.inboxes.pop(guest["name"], None); _guest_save()
        if not guest: self._send_error_json(403, "invalid token"); return
        _notify_admin(f"\U0001f514 Guest \"{guest['name']}\" disconnected"); _log(_LOG_INFO, "guest", f"Guest disconnected: {guest['name']}")
        self._send_json(200, {"ok": True, "name": guest["name"]})
    def handle_channel_create(self, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None: return
        label = _str_field(data, "label").strip(); members = data.get("members", [])
        include_manager = _bool_field(data, "include_manager", True)
        ttl = min(_int_field(data, "ttl_seconds", CHANNEL_TTL), CHANNEL_TTL)
        if not isinstance(members, list): self._send_error_json(400, "members must be a list"); return
        valid_members = []
        for m in members:
            if isinstance(m, str) and (m == "manager" or ":" in m): valid_members.append(m)
        if include_manager and "manager" not in valid_members: valid_members.append("manager")
        parsed = urlparse(self.path); query_params = parse_qs(parsed.query)
        guest_member = self._channel_guest_auth(query_params)
        if guest_member == "": return
        created_by = guest_member or "manager"
        if guest_member and guest_member not in valid_members: valid_members.append(guest_member)
        channel_id = channel_create_id(label); channel = channel_new(channel_id, label, created_by, valid_members, ttl)
        with channel_store.lock:
            channel_store.channels[channel_id] = channel
            _channel_save()
        member_str = ", ".join(valid_members)
        _notify_admin(f"\U0001f4e2 Channel {channel_id} created by {created_by}\nMembers: {member_str}")
        _log(_LOG_INFO, "channel", f"Channel created: {channel_id} by {created_by} members=[{member_str}]")
        self._send_json(200, {"ok": True, "channel": channel_id, "label": label, "members": valid_members,
            "send_url": f"/channels/{channel_id}/send", "messages_url": f"/channels/{channel_id}/messages"})
    def handle_channel_members(self, channel_id: str, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None: return
        with channel_store.lock:
            channel = channel_store.channels.get(channel_id)
            if not channel or channel_is_expired(channel): self._send_error_json(404, "channel not found"); return
            added = channel_add_members(channel, data.get("add", []) if isinstance(data.get("add", []), list) else [])
            removed = channel_remove_members(channel, data.get("remove", []) if isinstance(data.get("remove", []), list) else [])
            current = list(channel["members"].keys())
        if added or removed:
            _notify_admin(f"\U0001f4e2 Channel {channel_id}: {'; '.join((['added ' + ', '.join(added)] if added else []) + (['removed ' + ', '.join(removed)] if removed else []))}")
        self._send_json(200, {"ok": True, "channel": channel_id, "added": added, "removed": removed, "members": current})
    def handle_channel_send(self, channel_id: str, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None: return
        text = _str_field(data, "text").strip()
        if not text: self._send_error_json(400, "text required"); return
        parsed = urlparse(self.path); query_params = parse_qs(parsed.query)
        guest_member = self._channel_guest_auth(query_params)
        if guest_member == "": return
        from_member = guest_member or _str_field(data, "from") or "manager"
        with channel_store.lock:
            channel = channel_store.channels.get(channel_id)
            if not channel or channel_is_expired(channel): self._send_error_json(404, "channel not found"); return
            if from_member not in channel["members"] and from_member != "manager": self._send_error_json(403, f"{from_member} not a member"); return
            msg = channel_append_message(channel, from_member, text); members_snapshot = dict(channel["members"])
        _fanout_channel_message(channel_id, from_member, text, msg, members_snapshot, get_registered_sessions())
        self._send_json(200, {"ok": True, "channel": channel_id, "message_id": msg["id"], "seq": msg["seq"]})
    def handle_channel_messages(self, channel_id: str, parsed: ParseResult) -> None:
        query_params = parse_qs(parsed.query); after = query_params.get("after", [None])[0]
        from_member = self._channel_guest_auth(query_params)
        if from_member == "": return
        with channel_store.lock:
            channel = channel_store.channels.get(channel_id)
            if not channel or channel_is_expired(channel): self._send_error_json(404, "channel not found"); return
            if from_member and from_member not in channel["members"]: self._send_error_json(403, "not a member of this channel"); return
            msgs, truncated = channel_get_messages(channel, after)
        resp: dict[str, object] = {"ok": True, "channel": channel_id, "messages": msgs}
        if truncated: resp["truncated"] = True
        self._send_json(200, resp)
    def handle_channels_list(self, parsed: ParseResult | None = None) -> None:
        query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(self.path).query)
        filter_member = self._channel_guest_auth(query_params)
        if filter_member == "": return
        with channel_store.lock:
            active = []; expired_ids = []
            for cid, ch in channel_store.channels.items():
                if channel_is_expired(ch): expired_ids.append(cid)
                else:
                    if filter_member and filter_member not in ch["members"]: continue
                    active.append({"id": ch["id"], "label": ch["label"], "members": list(ch["members"].keys()),
                        "message_count": len(ch["messages"]), "created_by": ch["created_by"],
                        "send_url": f"/channels/{ch['id']}/send", "messages_url": f"/channels/{ch['id']}/messages"})
            for cid in expired_ids: del channel_store.channels[cid]
            if expired_ids: _channel_save()
        self._send_json(200, {"ok": True, "channels": active})
    def handle_channel_delete(self, channel_id: str) -> None:
        with channel_store.lock:
            channel = channel_store.channels.pop(channel_id, None)
            if channel: _channel_save()
        if not channel: self._send_error_json(404, "channel not found"); return
        _notify_admin(f"\U0001f4e2 Channel {channel_id} closed"); _log(_LOG_INFO, "channel", f"Channel deleted: {channel_id}")
        self._send_json(200, {"ok": True, "channel": channel_id})
    def _relay_get_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "): return auth[7:].strip()
        parsed = urlparse(self.path); params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
        return params.get("token", "")
    def handle_relay_get(self, channel_id: str, action: str | None, parsed: ParseResult) -> None:
        token = self._relay_get_token()
        if not token: self._send_json(401, {"error": "missing token"}); return
        channel = relay_auth_guest(channel_id, token)
        if not channel: self._send_json(403, {"error": "invalid or expired channel/token"}); return
        if action is None: self._send_text(200, relay_guide_text(channel, token)); return
        if action == "messages":
            params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
            self._send_json(200, {"messages": relay_get_messages(channel_id, after=params.get("after"))}); return
        if action == "status":
            self._send_json(200, {"channel_id": channel_id, "worker": channel["worker"], "label": channel["label"],
                "expires_at": channel["expires_at"], "message_count": len(channel["messages"])}); return
        self._send_json(404, {"error": f"unknown action: {action}"})
    def _relay_auth_and_parse(self, channel_id: str, auth_fn: Callable[..., object], body: bytes) -> tuple[dict[str, object] | None, str, object]:
        token = self._relay_get_token()
        if not token: self._send_json(401, {"error": "missing token"}); return None, "", None
        channel = auth_fn(channel_id, token)
        if not channel: self._send_json(403, {"error": "invalid or expired channel/token"}); return None, "", None
        try: data = cast(dict[str, object], json.loads(body)) if body else {}
        except (json.JSONDecodeError, ValueError): self._send_json(400, {"error": "invalid JSON"}); return None, "", None
        text = _str_field(data, "text").strip()
        if not text: self._send_json(400, {"error": "missing text"}); return None, "", None
        return data, text, channel
    def handle_relay_send(self, channel_id: str, body: bytes = b"") -> None:
        data, text, channel = self._relay_auth_and_parse(channel_id, relay_auth_guest, body)
        if not data: return
        envelope, msg = relay_guest_send(channel_id, text)
        if not envelope: self._send_json(500, {"error": "channel not found"}); return
        workers = channel.get("workers", [channel["worker"]]); delivered = {w: send_to_worker(w, envelope) for w in workers}
        self._send_json(200, {"message_id": msg["message_id"], "delivered": all(delivered.values()), "workers": delivered})
    def handle_relay_reply(self, channel_id: str, body: bytes = b"") -> None:
        data, text, channel = self._relay_auth_and_parse(channel_id, relay_auth_reply, body)
        if not data: return
        msg = relay_worker_reply(channel_id, text)
        if not msg: self._send_json(500, {"error": "channel not found"}); return
        self._send_json(200, {"message_id": msg["message_id"], "delivered": True})
    def handle_pr_file_content(self, parsed: ParseResult) -> None:
        import base64 as _b64
        params = dict(parse_qs(parsed.query)); token = params.get("token", [None])[0]
        if not tokens.validate_pr_review(token): self._send_status(403); return
        owner = params.get("owner", [None])[0]; repo = params.get("repo", [None])[0]
        path = params.get("path", [None])[0]; ref = params.get("ref", [None])[0]
        if not all([owner, repo, path, ref]): self._send_status(400); return
        try:
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}", "--jq", ".content"],
                capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
            if r.returncode != 0: self._send_status(404); return
            raw = _b64.b64decode(r.stdout.strip()).decode('utf-8', errors='replace')
            self._send_json(200, raw.splitlines())
        except subprocess.TimeoutExpired: self._send_status(504)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError): self._send_status(500)
    def handle_pr_keepalive(self, parsed: ParseResult) -> None:
        params = dict(parse_qs(parsed.query)); token = params.get("token", [None])[0]
        if not tokens.validate_pr_review(token): self._send_status(403); return
        self._send_status(204)
    def handle_pr_review_comments(self, body: bytes) -> None:
        try: data = cast(dict[str, object], json.loads(body))
        except (json.JSONDecodeError, ValueError): self._send_text(400, "Invalid JSON"); return
        if _str_field(data, "path") and _int_field(data, "line"): self.handle_pr_comment(body)
        else: self.handle_pr_general_comment(body)
    def handle_pr_general_comment(self, body: bytes) -> None:
        data = self._pr_guard(body)
        if not data: return
        owner = _str_field(data, "owner"); repo = _str_field(data, "repo"); pr_num = _int_field(data, "pr_num")
        comment_body = _str_field(data, "body").strip()
        if not all([owner, repo, pr_num, comment_body]): self._send_text(400, "Missing required fields"); return
        try:
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/issues/{pr_num}/comments", "--method", "POST", "-f", f"body={comment_body}"],
                capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
            if r.returncode != 0: self._send_text(502, f"GitHub API error: {r.stderr[:200]}"); return
        except subprocess.TimeoutExpired: self._send_text(504, "GitHub API timeout"); return
        self._pr_notify(f"\U0001f4ac PR #{pr_num} comment:\n{comment_body[:500]}")
        self._pr_fan_out(pr_num, comment_body)
        self._send_json(200, {"ok": True})
    def handle_pr_merge(self, body: bytes) -> None:
        data = self._pr_guard(body)
        if not data: return
        owner = _str_field(data, "owner"); repo = _str_field(data, "repo"); pr_num = _int_field(data, "pr_num")
        merge_method = _str_field(data, "merge_method", "merge")
        if merge_method not in ("merge", "squash", "rebase"): merge_method = "merge"
        if not all([owner, repo, pr_num]): self._send_text(400, "Missing required fields"); return
        try:
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/merge", "--method", "PUT", "-f", f"merge_method={merge_method}"],
                capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0:
                err = r.stderr.strip()[:300] or r.stdout.strip()[:300]
                self._send_text(502, f"Merge failed: {err}"); return
        except subprocess.TimeoutExpired: self._send_text(504, "Merge API timeout"); return
        self._pr_notify(f"\u2705 PR #{pr_num} merged ({merge_method}) via review page")
        self._send_json(200, {"ok": True})
    def handle_pr_review_endpoint(self, parsed: ParseResult) -> None:
        params = dict(parse_qs(parsed.query)); token = params.get("token", [None])[0]
        info = tokens.validate_pr_review(token)
        if not info: self._send_html(b"<h2>Link expired</h2><p>Send <code>/pr &lt;url&gt;</code> in Telegram to get a fresh 5-minute link.</p>", 403); return
        pr_num = info["pr_num"]; html_path = f"/tmp/pr-review-{pr_num}.html"
        if not os.path.exists(html_path): self._send_html(f"<h2>PR review not found</h2><p>File {html_path} missing. Re-run /pr command.</p>".encode(), 404); return
        with open(html_path, "rb") as f: self._send_html(f.read())
    def handle_pr_comment(self, body: bytes) -> None:
        data = self._pr_guard(body)
        if not data: return
        owner = _str_field(data, "owner"); repo = _str_field(data, "repo"); pr_num = _int_field(data, "pr_num")
        path = _str_field(data, "path"); line = _int_field(data, "line"); side = _str_field(data, "side", "RIGHT")
        comment_body = _str_field(data, "body").strip(); head_sha = _str_field(data, "head_sha")
        if not all([owner, repo, pr_num, path, line, comment_body, head_sha]): self._send_text(400, "Missing required fields"); return
        try:
            gh_payload = json.dumps({"body": comment_body, "commit_id": head_sha, "path": path, "line": line, "side": side})
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/comments", "--method", "POST", "--input", "-"],
                input=gh_payload, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
            if r.returncode != 0:
                err = r.stderr.strip() or r.stdout.strip()
                _log(_LOG_ERROR, "pr-comment", f"GitHub API error: {err}"); self._send_text(502, f"GitHub API error: {err}"); return
        except subprocess.TimeoutExpired: self._send_text(504, "GitHub API timeout"); return
        if admin_chat_id: transport.send_text(admin_chat_id, f"\U0001f4ac PR #{pr_num} comment\n{path}:{line}\n\n{comment_body}")
        self._pr_fan_out(pr_num, comment_body, f" on {path}:{line}")
        self._send_json(200, {"ok": True})
    def handle_transcript_endpoint(self, parsed: ParseResult) -> None:
        try:
            query_params = parse_qs(parsed.query); token = query_params.get("token", [None])[0]
            if not tokens.validate_rewind(token):
                self._send_html(_err_page("Session Expired", "This link has expired or is invalid. Send <code>/rewind &lt;name&gt;</code> in Telegram to get a fresh 5-minute link.").encode("utf-8"), 403); return
            parts = parsed.path.rstrip("/").split("/")
            if len(parts) < 3 or not parts[2]:
                self._send_json(400, {"error": "Usage: /transcript/<worker_name>"})
                return
            name = parts[2]
            if len(parts) >= 4 and parts[3] == "updates":
                query_params = parse_qs(parsed.query); since = int(query_params.get("since", [0])[0])
                session_id = query_params.get("sid", [None])[0]; host = get_worker_host(name)
                if host:
                    cwd = get_claude_session_cwd(name) or ""
                    sid: str | None = session_id or get_claude_session_id(name)
                    if not sid:
                        self._send_json(200, {"total": 0, "new": 0})
                        return
                    remote_home = _get_remote_home(host) or ""
                    remote_cwd = _remap_path(cwd, host); remote_slug = _project_slug(remote_cwd)
                    jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                else:
                    _tp, sid, _cwd = _resolve_transcript_path(name, session_id)
                    if not _tp or not sid:
                        self._send_json(200, {"total": 0, "new": 0})
                        return
                    jsonl_path = str(_tp)
                result = _run_transcript_query(jsonl_path, sid, "stats", host=host); total = 0
                if result:
                    total = _int_field(result, "n_user") + _int_field(result, "n_tool")
                    count_result = _run_transcript_query( jsonl_path, sid, "entries", host=host, page=1, per_page=1)
                    if count_result: total = _int_field(count_result, "total", total)
                new_count = max(0, total - since)
                self._send_json(200, {"total": total, "new": new_count})
                return
            query_params = parse_qs(parsed.query); session_id = query_params.get("sid", [None])[0]
            page_raw = query_params.get("page", [None])[0]
            try: page = max(1, int(page_raw)) if page_raw is not None else None
            except (ValueError, TypeError): page = None
            try: per_page = max(1, min(500, int(query_params.get("per_page", [50])[0])))
            except (ValueError, TypeError): per_page = 50
            search_query = query_params.get("q", [""])[0].strip()
            search_sort = query_params.get("sort", ["relevance"])[0].strip()
            if search_sort not in ("relevance", "time"): search_sort = "relevance"
            filter_mode = query_params.get("filter", [""])[0].strip(); host = get_worker_host(name)
            if not host:
                _tp, _sid, _cwd = _resolve_transcript_path(name, session_id)
                if _tp == "syncing":
                    sync_key = f"{name}:{_sid}"
                    self._send_html(_render_transcript_loading(name, _sid, token or "", sync_key).encode("utf-8")); return
            html_content = _render_transcript_html(
                    name, session_id=session_id,
                    page=page, per_page=per_page, search_query=search_query,
                    token=token or "", filter_mode=filter_mode, search_sort=search_sort)
            self._send_html(html_content.encode("utf-8"))
        except (OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "transcript", f"Transcript endpoint error: {e}", exc=e)
            self._send_text(500, str(e))
    def _send_status(self, code: int) -> None:
        self.send_response(code); self.end_headers()
    def _send_json(self, code: int, data: Mapping[str, object]) -> None:
        body = json.dumps(data).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def _send_error_json(self, code: int, message: str) -> None:
        self._send_json(code, {"ok": False, "error": message})
    def _send_html(self, body: bytes, status: int = 200) -> None:
        accept = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept and len(body) > 1024:
            import gzip as _gzip; compressed = _gzip.compress(body, compresslevel=6)
            self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip"); self.send_header("Content-Length", str(len(compressed)))
            self.end_headers(); self.wfile.write(compressed)
        else:
            self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _send_text(self, code: int, text: str) -> None:
        body = text.encode(); self.send_response(code)
        self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(body)
    def _pr_guard(self, body: bytes) -> PrActionBody | None:
        try: data = cast(PrActionBody, json.loads(body))
        except (json.JSONDecodeError, ValueError): self._send_text(400, "Invalid JSON"); return None
        if not tokens.validate_pr_review(_str_field(data, "token")): self._send_text(403, "Token expired"); return None
        return data
    def _pr_notify(self, text: str) -> None:
        try:
            import urllib.request; req = urllib.request.Request(
                f"{BRIDGE_PUBLIC_URL or f'http://localhost:{PORT}'}/notifications",
                data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
            _urlopen(req, timeout=TIMEOUT_TMUX_SEND)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")
    def _pr_fan_out(self, pr_num: int, comment_body: str, prefix: str = "") -> None:
        targets, _ = command_router.parse_at_mentions(comment_body)
        if targets:
            msg = f"manager: PR #{pr_num} review comment{prefix}\n\n{comment_body}"
            for t in targets: send_to_worker(t, msg)
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
        try:
            data = cast(dict[str, object], json.loads(body)); text = _str_field(data, "text")
            name = _str_field(data, "name")
            if not text:
                self._send_text(400, "Missing text")
                return
            host = get_worker_host(name) if name else None
            if host:
                _accept_all: Callable[[str | Path], FileValidation] = lambda p: FileValidation(True, Path(p))
                clean_text, images = _parse_media_tags(text, "image", _accept_all)
                clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
            else:
                clean_text, images = parse_image_tags(text)
                clean_text, files = parse_file_tags(clean_text)
            if name and (images or files): images = _localize_media(name, images); files = _localize_media(name, files)
            chat_ids = get_all_chat_ids(); sent = 0; label = name or "notify"
            for chat_id in chat_ids:
                if clean_text:
                    result = transport.send_text(chat_id, clean_text)
                    if result and result.get("ok"): sent += 1
                for img_path, caption in images:
                    if img_path is None:
                        transport.send_text(chat_id, f"{label}: {caption}")
                        continue
                    full_caption = f"{label}: {caption}" if caption else f"{label}:"
                    if Path(img_path).suffix.lower() in (".gif", ".mp4"): send_animation(chat_id, img_path, full_caption)
                    else: send_photo(chat_id, img_path, full_caption)
                for fpath, caption in files:
                    if fpath is None:
                        transport.send_text(chat_id, f"{label}: {caption}")
                        continue
                    full_caption = f"{label}: {caption}" if caption else f"{label}:"; ext = Path(fpath).suffix.lower()
                    if ext in VIDEO_EXTENSIONS: send_video(chat_id, fpath, full_caption)
                    elif ext in AUDIO_EXTENSIONS: send_audio(chat_id, fpath, full_caption)
                    elif ext in VOICE_EXTENSIONS: send_voice(chat_id, fpath, full_caption)
                    else: send_document(chat_id, fpath, full_caption)
            has_media = len(images) + len(files)
            _log(_LOG_INFO, "notify", f"sent to {sent}/{len(chat_ids)} chats: {text[:50]}..."
                 f"{f' ({has_media} media)' if has_media else ''}")
            self._send_text(200, f"Sent to {sent} chats")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"Notify error: {e}")
            self._send_text(500, str(e))
    def handle_health_alert(self, body: bytes = b"") -> None:
        try:
            data = cast(HealthAlertBody, json.loads(body)) if body else {}
            worker = _str_field(data, "worker", "unknown"); issue = _str_field(data, "issue", "unknown")
            age = _int_field(data, "transcript_age")
            age_human = f"{age // 3600}h{(age % 3600) // 60}m" if age >= 3600 else f"{age // 60}m"
            alert_text = f"🔴 {worker}: JSONL transcript stale ({age_human}). Session active but not recording. `/restart {worker}` to fix."
            _log(_LOG_WARN, "health", f"Health alert: {worker} — {issue} (age={age}s)")
            chat_ids = get_all_chat_ids()
            for chat_id in chat_ids: transport.send_text(chat_id, alert_text)
            self._send_json(200, {"ok": True})
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"Health alert error: {e}")
            self._send_json(500, {"ok": False, "error": str(e)})
    def handle_forge_register(self, body: bytes = b"") -> None:
        try:
            data = cast(ForgeRegisterBody, json.loads(body)) if body else {}
            name = _str_field(data, "Name") or _str_field(data, "name")
            host = _str_field(data, "Host") or _str_field(data, "host")
            version = _str_field(data, "Version") or _str_field(data, "version")
            callback_url = _str_field(data, "CallbackURL") or _str_field(data, "callback_url") or _str_field(data, "callbackUrl")
            tools = data.get("Tools", data.get("tools", {}))
            if name:
                if callback_url:
                    _registry_add_callback(name, callback_url, host=host, version=version, tools=tools)
                    _log(_LOG_INFO, "worker", f"Callback worker registered: {name} (host={host}, url={callback_url}, version={version})")
                else:
                    _registry_add(name, DEFAULT_BACKEND, host=host)
                    _log(_LOG_INFO, "worker", f"Forge worker registered: {name} (host={host}, version={version})")
                backend_name = get_worker_backend(name, {"host": host}); tmux_name = f"{TMUX_PREFIX}{name}"
                reg_host = host or None
                if tmux_exists(tmux_name, host=reg_host): export_hook_env(tmux_name, backend_name, host=reg_host)
                ensure_session_dir(name)
                if admin_chat_id is not None:
                    cid_file = get_chat_id_file(name)
                    if not cid_file.exists():
                        _tmp_cid = cid_file.with_suffix('.tmp'); _tmp_cid.write_text(str(admin_chat_id)); _tmp_cid.chmod(0o600); os.replace(str(_tmp_cid), str(cid_file))
            tmux_session = f"{TMUX_PREFIX}{name}" if name else ""; conflict = False; active_workers = []
            try:
                r = _subprocess_runner.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                                           capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r.returncode == 0:
                    active_workers = [s.removeprefix(TMUX_PREFIX)
                                      for s in r.stdout.strip().split("\n")
                                      if s.startswith(TMUX_PREFIX)]
                    conflict = name in active_workers if name else False
            except (subprocess.SubprocessError, OSError) as exc: _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
            worker_manager.invalidate_sessions_cache()
            self._send_json(200, {"ok": True, "settings": {"tmux_prefix": TMUX_PREFIX, "node_name": NODE_NAME or "", "tmux_session": tmux_session},
                "conflict": conflict, "active_workers": active_workers})
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Register error: {e}")
            self._send_json(500, {"ok": False, "error": str(e)})
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
        try:
            params = parse_qs(parsed.query); name = params.get("name", ["worker"])[0]
            raw_cwd = params.get("cwd", [None])[0]; requested_cwd = ""
            if raw_cwd is not None:
                worker_host = get_worker_host(name)
                requested_cwd, cwd_err = validate_cwd(raw_cwd, host=worker_host)
                if cwd_err: self._send_text(400, f"Invalid cwd: {cwd_err}"); return
            _sync_worker_manager()
            registered = worker_manager.get_registered_sessions(); tmux_name = ""
            host: str | None = None
            if name in registered:
                backend_name = get_worker_backend(name, registered[name])
                tmux_name = registered[name].get("tmux", f"{TMUX_PREFIX}{name}"); host = get_worker_host(name)
                if tmux_exists(tmux_name, host=host): export_hook_env(tmux_name, backend_name, host=host)
            else: backend_name = DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)
            if requested_cwd:
                _set_worker_cwd(name, requested_cwd)
                old_cwd = get_claude_session_cwd(name)
                save_claude_session_cwd(name, requested_cwd)
                _ensure_workspace_trusted(requested_cwd)
                if old_cwd and old_cwd.rstrip("/") != requested_cwd.rstrip("/"):
                    old_sid = get_claude_session_id(name)
                    _log_session_event(name, old_sid or "(none)", requested_cwd, "cwd_change")
                    _log(_LOG_WARN, "checkin", f"{name}: CWD changed ({old_cwd} -> {requested_cwd}), stale sessions will self-invalidate")
                    notice = _build_cwd_change_notice(name, old_cwd, requested_cwd, old_sid or "")
                    notify_chat_id = get_manager_chat_id(name)
                    if notify_chat_id is not None: send_telegram_message(notify_chat_id, notice, parse_mode="HTML")
                _log(_LOG_INFO, "checkin", f"{name}: requested_cwd={requested_cwd}, tmux={tmux_name}, host={host}")
                if tmux_name and tmux_exists(tmux_name, host=host):
                    pane_cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
                    same_cwd = pane_cwd and pane_cwd.rstrip("/") == requested_cwd.rstrip("/")
                    _log(_LOG_INFO, "checkin", f"{name}: pane_cwd={pane_cwd}, same_cwd={same_cwd}")
                    if not same_cwd:
                        allowed, block_msg = _checkin_can_restart( name, tmux_name, host, pane_cwd or "", requested_cwd)
                        if not allowed:
                            notify_chat_id = get_manager_chat_id(name)
                            if notify_chat_id is not None: send_telegram_message(notify_chat_id, block_msg)
                            self._send_text(200, block_msg); return
                        _log(_LOG_INFO, "checkin", f"{name}: triggering restart (cwd mismatch: pane={pane_cwd} vs requested={requested_cwd})")
                        ok, err = _checkin_do_restart( name, backend_name, tmux_name, host, requested_cwd)
                        if not ok: self._send_text(500, f"Failed to restart in {requested_cwd}: {err}")
                        else: self._send_text(200, f"Restarting in {requested_cwd}...")
                        return
            self._send_text(200, worker_manager._build_welcome(name, backend_obj))
        except (subprocess.SubprocessError, OSError, KeyError) as exc:
            _log(_LOG_ERROR, "bridge", f"Checkin endpoint error: {exc}")
            self._send_text(500, str(exc))
    def handle_health_workers_endpoint(self) -> None:
        try:
            now = _clock.time(); registered = get_registered_sessions()
            with watchdog.lock: state_snapshot = dict(watchdog.worker_states)
            workers = {}
            for name in sorted(registered.keys()):
                entry = state_snapshot.get(name)
                if entry: workers[name] = {"state": entry.status, "reason": entry.reason, "since": entry.since, "age_sec": int(now - entry.since) if entry.since else None}
                else: workers[name] = {"state": "unknown"}
            self._send_json(200, {"workers": workers})
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "worker", f"Health workers endpoint error: {e}"); self._send_text(500, str(e))
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
    setup_bot_commands(); print(f"Multi-Session Bridge on {BRIDGE_BIND}:{PORT}")
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
def _connector_log_message(tag: str, html_text: str, plain_text: str, targets: list[str]) -> None:
    connectors.log_message(tag, html_text, plain_text, targets)
def _connector_render_html(tag: str, current_html: str) -> str:
    import html as html_mod
    esc = html_mod.escape; msgs = connectors.get_log(tag); icon = "🔔" if tag == "github" else "📧"
    title = f"{tag.title()} Feed"; blocks = []
    av_letter = tag[0].upper(); av_color = "#8b5cf6" if tag == "github" else "#f59e0b"
    av_svg = f'<div class="u-av"><svg viewBox="0 0 28 28"><rect width="28" height="28" rx="14" fill="{av_color}"/><text x="14" y="18" text-anchor="middle" fill="#fff" font-size="12" font-weight="600">{av_letter}</text></svg></div>'
    tag_title = esc(tag.title())
    for i, m in enumerate(msgs):
        ts = time.strftime("%b %d, %H:%M", time.gmtime(m["ts"]))
        who = ", ".join(m["targets"]) if m["targets"] else "all"
        content = re.sub(r'(https?://\S+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', m["html"]).replace("\n", "<br>")
        is_latest = (i == len(msgs) - 1); cls = "chat-msg latest" if is_latest else "chat-msg"
        badge = '<span class="badge">Latest</span>' if is_latest else ""
        blocks.append(f'<div class="{cls}">{av_svg}<div class="chat-body"><span class="u-name">{tag_title}</span><span class="ts">{ts}</span><span class="target">→ {esc(who)}</span>{badge}<div class="chat-text">{content}</div></div></div>')
    blocks_html = "\n".join(blocks); updated = time.strftime("%b %d, %H:%M UTC", time.gmtime(_clock.time()))
    count_text = f'{len(msgs)} recent message{"s" if len(msgs) != 1 else ""}'
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{icon} {title}</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">'
            '<style>' + _CONNECTOR_CSS + f'</style></head><body><div class="wrap"><header><h1>{icon} {title}</h1>'
            f'<div class="meta">{count_text} &middot; Updated {updated}</div></header>'
            f'<div class="thread">{blocks_html}</div></div></body></html>')
def _connector_short_summary(tag: str, plain_text: str, serve_url: str | None = None, metadata: ConnectorMetadataDict | None = None) -> str:
    import html as _html
    icon = "🔔" if tag == "github" else "📧"; body = plain_text.strip()
    body = re.sub(r'^manager\s*\(via\s+\w+[^)]*\):\s*', '', body); body = re.sub(r'\[thread:[^\]]+\]\s*', '', body)
    body = " ".join(body.split())
    if len(body) > 200: body = body[:197] + "…"
    ref_match = re.search(r'#(\d+)', plain_text); thread_match = re.search(r'\[thread:([^\]]+)\]', plain_text)
    header = f"{icon} <b>{tag.title()}</b>"
    if ref_match:
        num = ref_match.group(1); repo = (metadata or {}).get("repo", "BasedHardware/omi")
        gh_url = f"https://github.com/{repo}/issues/{num}"; header += f' <a href="{gh_url}">#{num}</a>'
    elif thread_match: header += f" {_html.escape('thread:' + thread_match.group(1))}"
    parts = [header, _html.escape(body)]
    if serve_url: parts.append(f'<a href="{_html.escape(serve_url)}">View full →</a>')
    return "\n".join(parts)
def _connector_export_github(number: int, repo: str) -> str | None:
    try:
        r = _subprocess_runner.run(
            ["beast", "github", "export", str(number), "--format", "print", "--serve", "--repo", repo, "--fresh"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0:
            for line in r.stderr.splitlines() + r.stdout.splitlines():
                if "http" in line and ("localhost" in line or "serve" in line.lower()):
                    url = line.strip().split()[-1].rstrip("/")
                    if "localhost" in url:
                        host = (urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else None) or "157.180.48.254"
                        url = url.replace("localhost", host)
                    return url
    except subprocess.SubprocessError as e: _log(_LOG_WARN, "github", f"export failed for #{number}: {e}")
    return None
def _connector_on_message(tag: str) -> Callable[[list[str], str, str | None, list[ConnectorAttachmentDict] | None, ConnectorMetadataDict | None], None]:
    def handler(targets: list[str], html_text: str, plain_text: str | None = None, attachments: list[ConnectorAttachmentDict] | None = None, metadata: ConnectorMetadataDict | None = None) -> None:
        if plain_text is None: plain_text = html_text
        _connector_log_message(tag, html_text, plain_text, targets)
        if admin_chat_id:
            serve_url: str | None = None
            if tag == "github" and metadata and metadata.get("number"):
                try:
                    serve_url = _connector_export_github( metadata["number"], metadata.get("repo", "BasedHardware/omi"))
                except KeyError as e: _log(_LOG_WARN, tag, f"github export failed: {e}")
            if not serve_url:
                try:
                    page_html = _connector_render_html(tag, html_text); tmp_path = f"/tmp/connector-{tag}.html"
                    with open(tmp_path, "w") as f: f.write(page_html)
                    serve_url = _beast_serve_deploy(tmp_path, f"connector-{tag}")
                except OSError as e: _log(_LOG_WARN, tag, f"beast serve failed: {e}")
            summary = _connector_short_summary(tag, plain_text, serve_url, metadata)
            try:
                send_telegram_message(admin_chat_id, summary, parse_mode="HTML")
            except OSError:
                try:
                    send_telegram_message(admin_chat_id, plain_text[:300])
                except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_WARN, tag, f"Telegram send failed: {e}")
            for att in (attachments or []):
                fpath = att.get("path", ""); fname = att.get("filename", "")
                if not fpath or not os.path.isfile(fpath): continue
                ext = os.path.splitext(fname)[1].lower(); caption = f"📧 {fname}"
                if ext in ALLOWED_IMAGE_EXTENSIONS: send_photo(admin_chat_id, fpath, caption)
                elif ext in VIDEO_EXTENSIONS: send_video(admin_chat_id, fpath, caption)
                else: send_document(admin_chat_id, fpath, caption)
                _log(_LOG_INFO, tag, f"attachment -> Telegram: {fname}")
        if targets:
            for name in targets:
                send_to_worker(name, plain_text)
                _log(_LOG_INFO, tag, f"-> {name}: {plain_text[:80]}...")
        else: _log(_LOG_INFO, tag, f"-> Telegram only (no mentions): {plain_text[:80]}...")
    return handler
def _connector_get_workers() -> set[str]:
    return set(get_registered_sessions().keys())
def _connector_on_alert(tag: str) -> Callable[[str], None]:
    def handler(text: str) -> None:
        if admin_chat_id:
            try:
                send_telegram_message(admin_chat_id, text)
            except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_WARN, tag, f"Failed to send Telegram alert: {e}")
    return handler
def _new_gmail() -> "GmailConnector":
    return GmailConnector(gws_bin=GMAIL_GWS_BIN, from_filter=GMAIL_FROM_FILTER, poll_interval=GMAIL_POLL_INTERVAL,
        on_message=_connector_on_message("gmail"), get_registered_workers=_connector_get_workers, on_alert=_connector_on_alert("gmail"))
def _new_github() -> "GitHubConnector":
    return GitHubConnector(repo=GITHUB_REPOS, from_user=GITHUB_FROM_USER, poll_interval=GITHUB_POLL_INTERVAL,
        on_message=_connector_on_message("github"), get_registered_workers=_connector_get_workers,
        on_alert=_connector_on_alert("github"), state_file=str(NODE_DIR / "github_state.json"))
def _start_connectors() -> tuple[object, object]:
    gmail_inst = None
    if GMAIL_ENABLED and GmailConnector is not None:
        gmail_inst = _new_gmail(); gmail_inst.start()
        print(f"Gmail connector: polling every {GMAIL_POLL_INTERVAL}s for {GMAIL_FROM_FILTER}")
    elif GMAIL_ENABLED and GmailConnector is None: _log(_LOG_ERROR, "bridge", f"Gmail connector disabled: {GMAIL_IMPORT_ERROR}")
    github_inst = None
    if GITHUB_ENABLED and GitHubConnector is not None:
        github_inst = _new_github(); github_inst.start()
        print(f"GitHub connector: polling every {GITHUB_POLL_INTERVAL}s for {GITHUB_FROM_USER} on {', '.join(GITHUB_REPOS)}")
    elif GITHUB_ENABLED and GitHubConnector is None: _log(_LOG_ERROR, "bridge", f"GitHub connector disabled: {GITHUB_IMPORT_ERROR}")
    return gmail_inst, github_inst
def _restart_connector(name: str) -> tuple[bool, str]:
    _cfg = {"gmail": (GMAIL_ENABLED, GmailConnector, GMAIL_IMPORT_ERROR, "GMAIL_ENABLED=0", _new_gmail),
            "github": (GITHUB_ENABLED, GitHubConnector, GITHUB_IMPORT_ERROR, "BRIDGE_GHPOLL_ENABLED=0", _new_github)}
    entry = _cfg.get(name)
    if not entry: return False, f"Unknown connector: {name} (valid: gmail, github)"
    enabled, cls, import_err, env_name, factory = entry
    if not enabled: return False, f"{name.title()} connector not enabled ({env_name})"
    if cls is None: return False, f"{name.title()} connector import failed: {import_err}"
    old = getattr(connectors, name, None)
    if old is not None: old.stop()
    inst = factory(); setattr(connectors, name, inst)
    ok, msg = inst.restart()
    print(f"{name.title()} connector {'restarted' if ok else 'restart failed'}: {msg}"); return ok, msg
def _get_connectors_status() -> dict[str, ConnectorStatusDict]:
    result: dict[str, ConnectorStatusDict] = {}
    for cname, enabled in [("gmail", GMAIL_ENABLED), ("github", GITHUB_ENABLED)]:
        if not enabled: result[cname] = {"name": cname, "running": False, "enabled": False}
        else:
            inst = getattr(connectors, cname, None)
            result[cname] = cast(ConnectorStatusDict, inst.status()) if inst else {"name": cname, "running": False, "error": "not initialized"}
    return result
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
    _load_learning_reminder_state()
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
import telegram as _tg_mod
import claudecode as _cc_mod
_this_module = sys.modules[__name__]
_this_module.__class__ = _BridgeModule
_this_module._sources = (_tg_mod, _cc_mod)
