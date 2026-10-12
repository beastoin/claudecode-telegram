from __future__ import annotations
from core import (
    _subprocess_runner,
    _clock,
    _log,
    _log_best_effort,
    _str_field,
    _int_field,
    _dict_field,
    _bool_field,
    _RealSubprocessRunner,
    _RealClock,
    _LOG_ERROR,
    _LOG_WARN,
    _LOG_INFO,
    _LOG_DEBUG,
    _build_app_context,
    _wd_cfg,
    _res_cfg,
    _urlopen,
    _DEFAULT_PORTS,
    _bridge_url_env,
    _node_name,
    _CHECKIN_NOTE_PATH,
    _LEARNING_REMINDER_PATH,
    SubprocessRunner,
    Clock,
    MarkdownToken,
    AppContext,
    get_app_context,
    VERSION,
    BOT_TOKEN,
    NODE_NAME,
    NODE_DIR,
    PORT,
    BRIDGE_BIND,
    BRIDGE_URL,
    BRIDGE_PUBLIC_URL,
    BRIDGE_SSH_TARGET,
    SESSIONS_DIR,
    TMUX_PREFIX,
    CLAUDE_DIR,
    CLAUDE_SETTINGS_FILE,
    TIMEOUT_TMUX_CHECK,
    TIMEOUT_TMUX_SEND,
    TIMEOUT_REMOTE_CMD,
    TIMEOUT_FILE_TRANSFER,
    TIMEOUT_GIT_OP,
    TIMEOUT_LARGE_TRANSFER,
    TIMEOUT_RSYNC,
    TIMEOUT_FULL_SYNC,
    TIMEOUT_HTTP_API,
    TIMEOUT_HTTP_DOWNLOAD,
    TIMEOUT_HTTP_UPLOAD,
    TIMEOUT_PROCESS_WAIT,
    TIMEOUT_THREAD_JOIN,
    DELAY_TMUX_SEND,
    DELAY_PIPE_POLL,
    DELAY_STARTUP,
    DELAY_STARTUP_LONG,
    DELAY_RETRY,
    DELAY_BRIEF,
    DELAY_SHORT,
    DELAY_RESPONSE_GAP,
    DELAY_PROCESS_SETTLE,
    DELAY_CLAUDE_LOAD,
    DEFAULT_BACKEND,
    DEFAULT_WORKER_BACKEND,
    PENDING_TIMEOUT,
    FILE_INBOX_ROOT,
    WORKER_PIPE_ROOT,
    TEAM_DIR,
    MACHINES_CONFIG_FILE,
    WEBHOOK_SECRET,
    WatchdogConfig,
    ResourceAlertConfig,
    WATCHDOG_INTERVAL,
    START_GRACE,
    THINK_GRACE,
    TOOL_GAP_GRACE,
    STALE_PENDING,
    CPU_ACTIVE,
    CPU_IDLE,
    IDLE_STREAK_STUCK,
    ALERT_COOLDOWN,
    RESTART_COOLDOWN,
    DISK_WARN_PCT,
    DISK_ALERT_PCT,
    DISK_ALERT_GB,
    DISK_COOLDOWN,
    CPU_HOG_PCT,
    CPU_HOG_DURATION_MIN,
    CPU_HOG_COOLDOWN,
    WORKTREE_THRESHOLD_GB,
    WORKTREE_COOLDOWN,
    MEM_THRESHOLD_PCT,
    MEM_THRESHOLD_GB,
    MEM_COOLDOWN,
    IO_IOWAIT_PCT,
    IO_COOLDOWN,
    INFRA_COOLDOWN,
    HOST_DOWN_THRESHOLD,
    PERSISTENCE_NOTE,
    STT_ENDPOINT,
    STT_TIMEOUT,
    ADMIN_CHAT_ID_ENV,
    admin_chat_id,
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
from typing import (
    IO,
    Callable,
    Iterator,
    Literal,
    NamedTuple,
    Protocol,
    TypedDict,
    TYPE_CHECKING,
    cast,
    runtime_checkable,
)
from urllib.parse import urlparse
from _types import (
    ReminderState,
    WorkerStateEntry,
    ParsedWorkerTarget,
    TmuxActivityResult,
    AuthorDetection,
    TmuxSessionDict,
    RegistryWorkerDict,
    RegistryFileDict,
    RewindTokenEntry,
    PrReviewTokenEntry,
    ProcStatsEntry,
    QuestionOption,
    QuestionDetails,
    DiskUsageDict,
    MemUsageDict,
    IoUsageDict,
    WorktreeItemDict,
    WorktreeUsageDict,
    CpuHogEntry,
    MachineHealthDict,
    CodexTranscriptEntry,
    GitPushStateResult,
)

if TYPE_CHECKING:
    from bridge import Machine


class ProcessRegistry:
    def __init__(self) -> None:
        self.adapter_pids: dict[str, tuple[subprocess.Popen[str], IO[str] | None]] = {}
        self.adapter_pids_lock: threading.Lock = threading.Lock()
        self.pipe_readers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.pipe_readers_lock: threading.Lock = threading.Lock()
        self.pending_locks: dict[str, threading.Lock] = {}
        self.pending_locks_guard: threading.Lock = threading.Lock()


class WorkerWatchdogState:
    def __init__(self) -> None:
        self.worker_states: dict[str, WorkerStateEntry] = {}
        self.last_child_ts: dict[str, float] = {}
        self.last_seen_claude: dict[str, float] = {}
        self.last_hook_ts: dict[str, float] = {}
        self.last_alert_ts: dict[str, float] = {}
        self.alert_msg_ids: dict[str, tuple[int, str]] = {}
        self.idle_streak: dict[str, int] = {}
        self.prev_worker_states: dict[str, str] = {}
        self.consecutive_probe_failures: dict[str, int] = {}
        self.consecutive_good_probes: dict[str, int] = {}
        self.consecutive_bad_probes: dict[str, int] = {}
        self.idle_child_baseline: dict[str, int] = {}
        self.prev_children: dict[str, int] = {}
        self.last_activity_ts: dict[str, float] = {}
        self.worker_cwds: dict[str, str] = {}
        self.recent_restarts: dict[str, float] = {}
        self.restart_in_progress: dict[str, float] = {}
        self.restart_lock = threading.Lock()
        self.force_restart_pending_cwd: dict[str, bool] = {}
        self.waiting_input_details: dict[str, QuestionDetails] = {}
        self.last_resolved_ts: dict[str, float] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


class LearningReminderState:
    def __init__(self) -> None:
        self.state: dict[str, ReminderState] = {}
        self.lock: threading.Lock = threading.Lock()
        self.idle_scan_timer: threading.Timer | None = None


class HostHealthState:
    def __init__(self) -> None:
        self.ssh_failures: dict[str, int] = {}
        self.down: dict[str, bool] = {}
        self.down_since: dict[str, float] = {}
        self.last_error: dict[str, str] = {}
        self.disk_usage: dict[str, DiskUsageDict] = {}
        self.disk_alert_ts: dict[str, float] = {}
        self.disk_alerted: dict[str, str | bool] = {}
        self.cpu_hogs: dict[str, list[CpuHogEntry]] = {}
        self.cpu_hog_alert_ts: dict[str, float] = {}
        self.worktree_usage: dict[str, WorktreeUsageDict] = {}
        self.worktree_alert_ts: dict[str, float] = {}
        self.worktree_alerted: dict[str, bool] = {}
        self.mem_usage: dict[str, MemUsageDict] = {}
        self.mem_alert_ts: dict[str, float] = {}
        self.mem_alerted: dict[str, bool] = {}
        self.io_usage: dict[str, IoUsageDict] = {}
        self.io_alert_ts: dict[str, float] = {}
        self.io_alerted: dict[str, bool] = {}
        self.tailscale_down: bool = False
        self.tailscale_alert_ts: float = 0.0

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


def build_claude_start_cmd(resume_id: str = "") -> str:
    cmd = ["claude"]
    if resume_id:
        cmd.extend(["--resume", resume_id])
    cmd.append("--dangerously-skip-permissions")
    return " ".join(shlex.quote(part) for part in cmd)


class Backend(Protocol):
    name: str
    binary: str
    is_interactive: bool

    def start_cmd(self, resume_id: str = "") -> str: ...
    def send(self, worker_name: str, tmux_name: str, text: str, bridge_url: str, sessions_dir: Path) -> bool: ...
    def is_online(self, tmux_name: str) -> bool: ...
class RemoteCache:
    def __init__(self) -> None:
        self.lock: threading.Lock = threading.Lock()
        self.tools: dict[str, str] = {}
        self.machines: dict[str, "Machine"] | None = None
        self.machines_path: Path | None = None
        self.home_dirs: dict[str, str] = {}


remote_cache = RemoteCache()


def _resolve_remote_tool(tool: str, host: str) -> str:
    key = f"{host}:{tool}"
    with remote_cache.lock:
        cached = remote_cache.tools.get(key)
    if cached:
        return cached
    probe = (
        f"command -v {shlex.quote(tool)} 2>/dev/null || "
        f"for p in /opt/homebrew/bin/{tool} /usr/local/bin/{tool} /usr/bin/{tool} /bin/{tool}; "
        f'do [ -x "$p" ] && echo "$p" && break; done'
    )
    try:
        r = _subprocess_runner.run(
            ["ssh", "-o", "ConnectTimeout=3", host, probe],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_TMUX_SEND,
        )
        found = r.stdout.strip()
        if found:
            with remote_cache.lock:
                remote_cache.tools[key] = found
            _log(_LOG_INFO, "bridge", f"discovered {tool} on {host}: {found}")
            return found
    except (subprocess.SubprocessError, OSError) as exc:
        _log(_LOG_DEBUG, "probe:_resolve_remote_tool", f"{type(exc).__name__}: {exc}")
    return tool


def _remote_run(cmd: list[str], host: str | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
    if host:
        cmd = list(cmd)
        tool = str(cmd[0])
        if tool in ("tmux", "claude"):
            cmd[0] = _resolve_remote_tool(tool, host)
        remote_cmd = " ".join(shlex.quote(str(a)) for a in cmd)
        _tv = kwargs.get("timeout", 10)
        timeout_val = int(_tv) if isinstance(_tv, (int, float, str)) else 10
        cmd = ["ssh", "-o", f"ConnectTimeout={min(timeout_val, 5)}", host, remote_cmd]
    kwargs.setdefault("timeout", 10)
    return _subprocess_runner.run(cmd, **kwargs)


def _detect_os_family() -> str:
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _project_slug(cwd: str) -> str:
    return cwd.replace("/", "-")


GIT_SERVER_DIR = os.path.expanduser("~/git-server")


def _is_git_repo(cwd: str, host: str | None = None) -> bool:
    try:
        r = _remote_run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            host=host,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_REMOTE_CMD,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


class TmuxSendState:
    def __init__(self) -> None:
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard: threading.Lock = threading.Lock()
        self.flock_fds: dict[str, int] = {}


tmux_send = TmuxSendState()


def _get_tmux_send_lock(tmux_name: str) -> threading.Lock:
    with tmux_send.locks_guard:
        if tmux_name not in tmux_send.locks:
            tmux_send.locks[tmux_name] = threading.Lock()
        return tmux_send.locks[tmux_name]


def tmux_send_lock_path(tmux_name: str) -> Path:
    return Path(f"/tmp/claudecode-telegram/{_node_name}/locks/{tmux_name}.lock")


def _acquire_flock(tmux_name: str) -> int:
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
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def tmux_exists(tmux_name: str, host: str | None = None, timeout: int = 3) -> bool:
    return (
        _remote_run(
            ["tmux", "has-session", "-t", tmux_name], host=host, capture_output=True, timeout=timeout
        ).returncode
        == 0
    )


def tmux_send_message(tmux_name: str, text: str, host: str | None = None, literal: bool = False) -> bool:
    lock = _get_tmux_send_lock(tmux_name)
    with lock:
        flock_fd = _acquire_flock(tmux_name)
        try:
            if literal:
                r = _remote_run(
                    ["tmux", "send-keys", "-t", tmux_name, "-l", text],
                    host=host,
                    capture_output=True,
                    timeout=TIMEOUT_TMUX_SEND,
                )
                if r.returncode != 0:
                    return False
                _clock.sleep(DELAY_RETRY)
                r = _remote_run(["tmux", "send-keys", "-t", tmux_name, "Enter"], host=host, timeout=TIMEOUT_TMUX_SEND)
                return r.returncode == 0
            buf_name = f"msg-{uuid.uuid4().hex[:8]}"
            if host:
                r = _remote_run(
                    ["tmux", "load-buffer", "-b", buf_name, "-"],
                    host=host,
                    input=text.encode(),
                    capture_output=True,
                    timeout=TIMEOUT_TMUX_SEND,
                )
            else:
                fd, tmpfile = tempfile.mkstemp(suffix=".msg", prefix="tmux-send-")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(text.encode())
                    r = _subprocess_runner.run(
                        ["tmux", "load-buffer", "-b", buf_name, tmpfile], capture_output=True, timeout=TIMEOUT_TMUX_SEND
                    )
                finally:
                    try:
                        os.unlink(tmpfile)
                    except OSError:
                        pass
            if r.returncode != 0:
                return False
            r = _remote_run(
                ["tmux", "paste-buffer", "-p", "-r", "-t", tmux_name, "-b", buf_name, "-d"],
                host=host,
                capture_output=True,
                timeout=TIMEOUT_TMUX_SEND,
            )
            if r.returncode != 0:
                return False
            _clock.sleep(DELAY_STARTUP)
            r = _remote_run(["tmux", "send-keys", "-t", tmux_name, "Enter"], host=host, timeout=TIMEOUT_TMUX_SEND)
            return r.returncode == 0
        finally:
            _release_flock(flock_fd)


def get_pane_command(tmux_name: str, host: str | None = None) -> str:
    result = _remote_run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_command}"],
        host=host,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_TMUX_CHECK,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def is_process_running(tmux_name: str, process_name: str, host: str | None = None) -> bool:
    cmd = get_pane_command(tmux_name, host=host)
    if process_name.lower() in cmd.lower():
        return True
    result = _remote_run(
        ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
        host=host,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_TMUX_CHECK,
    )
    if result.returncode != 0:
        return False
    pane_pid = result.stdout.strip()
    if not pane_pid:
        return False
    result = _remote_run(
        ["pgrep", "-P", pane_pid, process_name], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK
    )
    return result.returncode == 0


def tmux_send_escape(tmux_name: str, host: str | None = None) -> None:
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)


def _tmux_pane_pids(host: str | None = None) -> dict[str, str]:
    try:
        result = _remote_run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
            host=host,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_TMUX_SEND,
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
    name = "claude"
    binary = "claude"
    is_interactive = True

    def start_cmd(self, resume_id: str = "") -> str:
        return build_claude_start_cmd(resume_id)

    def send(self, worker_name: str, tmux_name: str, text: str, bridge_url: str, sessions_dir: Path) -> bool:
        import bridge

        host = bridge.get_worker_host(worker_name)
        try:
            if not tmux_exists(tmux_name, host=host, timeout=TIMEOUT_TMUX_CHECK):
                return False
        except (subprocess.SubprocessError, OSError):
            if not host:
                return False
        literal = False
        try:
            r = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_TMUX_SEND,
            )
            if r.returncode == 0 and "Paste code here" in r.stdout:
                literal = True
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:send", f"{type(exc).__name__}: {exc}")
        return tmux_send_message(tmux_name, text, host=host, literal=literal)

    def is_online(self, tmux_name: str) -> bool:
        if not tmux_exists(tmux_name):
            return False
        return is_process_running(tmux_name, "claude")


processes = ProcessRegistry()
watchdog = WorkerWatchdogState()
learning_reminders = LearningReminderState()
host_health = HostHealthState()
worker_manager = None
LEARNING_REMINDER_RESPONSE_THRESHOLD = 15
LEARNING_REMINDER_IDLE_HOURS = 6


def _which_binary(binary: str) -> str | None:
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
    return is_process_running(tmux_name, "claude", host=host)


import _claudecode_impl as _cci_mod  # noqa: E402
from _claudecode_impl import (  # noqa: E402
    CodexBackend,
    _cache_session_id,
    _capture_pane_text,
    _codex_adapter_remote,
    _codex_adapter_thread,
    _codex_load_session_id,
    _codex_parse_jsonl,
    _codex_run,
    _codex_save_session_id,
    _codex_send_to_bridge,
    _codex_session_id_path,
    _ensure_workspace_trusted,
    _find_codex_transcript,
    _forward_pipe_message,
    _get_remote_home,
    _log_session_event,
    _remap_path,
    _scan_latest_session_id,
    cleanup_worker_pipe,
    clear_claude_session_id,
    ensure_session_dir,
    ensure_worker_pipe,
    export_hook_env,
    get_any_session_id,
    get_chat_id_file,
    get_claude_session_cwd,
    get_claude_session_id,
    get_session_dir,
    get_tmux_env_value,
    get_worker_backend,
    get_worker_pipe_path,
    normalize_backend,
    normalize_cwd,
    pipe_reader_loop,
    save_claude_session_cwd,
    start_pipe_reader,
    stop_pipe_reader,
    tmux_prompt_empty,
    validate_cwd,
)

BACKENDS: dict[str, Backend] = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
}


def get_backend(name: str) -> Backend:
    return BACKENDS.get(name, BACKENDS[DEFAULT_BACKEND])


def is_valid_backend(name: str) -> bool:
    return name in BACKENDS


def list_backends() -> list[str]:
    return list(BACKENDS.keys())


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
CLAUDE_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
REWIND_TOKENS: dict[str, RewindTokenEntry] = {}
PR_REVIEW_TOKENS: dict[str, PrReviewTokenEntry] = {}
WORKER_REGISTRY_FILE = NODE_DIR / "workers.json"
RESERVED_NAMES = {
    "team",
    "focus",
    "restart",
    "settings",
    "hire",
    "end",
    "all",
    "cancel",
    "start",
    "help",
}
_CLAUDE_JSON_PATH = Path.home() / ".claude.json"
POISON_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"error.*overloaded",
        r"error.*40[139]",
        r"error.*429",
        r"error.*5[02][39]",
        r"image.*dimensions.*exceed",
        r"context.*(length|window).*exceed",
        r"context_length_exceeded",
        r"rate.?limit",
        r"invalid.*api.?key",
        r"invalid_request_error",
        r"insufficient_quota",
        r"model.*not.*found",
        r"APIError",
        r"connection.*reset",
        r"timeout.*error",
    )
]
HOOK_FAILURE_THRESHOLD = 3
HOOK_FAILURE_WINDOW = 120
_ActivityCheck = Callable[[list[str]], str | None]
_INTERACTIVE_FOOTERS = [
    "Enter to select",
    "Space to toggle",
    "Tab to toggle",
    "Type to search",
    "Enter to submit",
    "Enter to add",
    "Enter to retry",
    "Enter to continue",
    "Enter to try again",
    "Enter to confirm",
    "ctrl-g to edit",
    "Auto-approving in",
    "Press any key to intervene",
]
_INTERACTIVE_CONTENT = [
    "Would you like to proceed?",
    "written up a plan and is ready to execute",
    "wants to enter plan mode",
    "No code changes will be made until you approve",
    "Allow Bash",
    "Allow Read",
    "Allow Write",
    "Allow Edit",
    "Allow Glob",
    "Allow Grep",
    "Allow Agent",
    "Allow Notebook",
]
# Propagate test patches to _claudecode_impl
import types as _types_mod


class _ClaudeCodeModule(_types_mod.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_cci_mod, name):
            setattr(_cci_mod, name, value)


import sys as _sys

_sys.modules[__name__].__class__ = _ClaudeCodeModule
