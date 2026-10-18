from __future__ import annotations

import collections
import fcntl
import glob
import hashlib
import http.client
import json
import mimetypes
import os
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
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    IO,
    TYPE_CHECKING,
    Literal,
    NamedTuple,
    Protocol,
    TypedDict,
    cast,
    runtime_checkable,
)
from urllib.parse import urlparse

from shared import (
    _CHECKIN_NOTE_PATH,
    _DEFAULT_PORTS,
    _LEARNING_REMINDER_PATH,
    _LOG_DEBUG,
    _LOG_ERROR,
    _LOG_INFO,
    _LOG_WARN,
    ADMIN_CHAT_ID_ENV,
    ALERT_COOLDOWN,
    BOT_TOKEN,
    BRIDGE_BIND,
    BRIDGE_PUBLIC_URL,
    BRIDGE_SSH_TARGET,
    BRIDGE_URL,
    CLAUDE_DIR,
    CLAUDE_PROJECTS_DIR,
    CLAUDE_SETTINGS_FILE,
    CPU_ACTIVE,
    CPU_HOG_COOLDOWN,
    CPU_HOG_DURATION_MIN,
    CPU_HOG_PCT,
    CPU_IDLE,
    DEFAULT_BACKEND,
    DEFAULT_WORKER_BACKEND,
    DELAY_BRIEF,
    DELAY_CLAUDE_LOAD,
    DELAY_PIPE_POLL,
    DELAY_PROCESS_SETTLE,
    DELAY_RESPONSE_GAP,
    DELAY_RETRY,
    DELAY_SHORT,
    DELAY_STARTUP,
    DELAY_STARTUP_LONG,
    DELAY_TMUX_SEND,
    DISK_ALERT_GB,
    DISK_ALERT_PCT,
    DISK_COOLDOWN,
    DISK_WARN_PCT,
    FILE_INBOX_ROOT,
    HOST_DOWN_THRESHOLD,
    IDLE_STREAK_STUCK,
    INFRA_COOLDOWN,
    IO_COOLDOWN,
    IO_IOWAIT_PCT,
    MACHINES_CONFIG_FILE,
    MEM_COOLDOWN,
    MEM_THRESHOLD_GB,
    MEM_THRESHOLD_PCT,
    NODE_DIR,
    NODE_NAME,
    PENDING_TIMEOUT,
    PERSISTENCE_NOTE,
    PORT,
    RESTART_COOLDOWN,
    SESSIONS_DIR,
    STALE_PENDING,
    START_GRACE,
    STT_ENDPOINT,
    STT_TIMEOUT,
    TEAM_DIR,
    THINK_GRACE,
    TIMEOUT_FILE_TRANSFER,
    TIMEOUT_FULL_SYNC,
    TIMEOUT_GIT_OP,
    TIMEOUT_HTTP_API,
    TIMEOUT_HTTP_DOWNLOAD,
    TIMEOUT_HTTP_UPLOAD,
    TIMEOUT_LARGE_TRANSFER,
    TIMEOUT_PROCESS_WAIT,
    TIMEOUT_REMOTE_CMD,
    TIMEOUT_RSYNC,
    TIMEOUT_THREAD_JOIN,
    TIMEOUT_TMUX_CHECK,
    TIMEOUT_TMUX_SEND,
    TMUX_PREFIX,
    TOOL_GAP_GRACE,
    VERSION,
    WATCHDOG_INTERVAL,
    WEBHOOK_SECRET,
    WORKER_PIPE_ROOT,
    WORKTREE_COOLDOWN,
    WORKTREE_THRESHOLD_GB,
    AppContext,
    Clock,
    CodexTranscriptEntry,
    CpuHogEntry,
    DiskUsageDict,
    GitPushStateResult,
    IoUsageDict,
    MachineHealthDict,
    MarkdownToken,
    MemUsageDict,
    ProcStatsEntry,
    PrReviewTokenEntry,
    QuestionDetails,
    QuestionOption,
    RegistryFileDict,
    RegistryWorkerDict,
    ReminderState,
    ResourceAlertConfig,
    RewindTokenEntry,
    SubprocessRunner,
    TmuxActivityResult,
    TmuxSessionDict,
    WatchdogConfig,
    WorkerStateEntry,
    WorktreeItemDict,
    WorktreeUsageDict,
    _bool_field,
    _bridge_url_env,
    _build_app_context,
    _clock,
    _dict_field,
    _int_field,
    _log,
    _log_best_effort,
    _log_suppress,
    _node_name,
    _RealClock,
    _RealSubprocessRunner,
    _res_cfg,
    _str_field,
    _subprocess_runner,
    _urlopen,
    _wd_cfg,
    admin_chat_id,
    get_app_context,
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
        self.machines: dict[str, Machine] | None = None
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
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
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
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    return fd


def _release_flock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def tmux_exists(tmux_name: str, host: str | None = None, timeout: int = 3) -> bool:
    return _remote_run(["tmux", "has-session", "-t", tmux_name], host=host, timeout=timeout).returncode == 0


def tmux_send_message(tmux_name: str, text: str, host: str | None = None, literal: bool = False) -> bool:
    lock = _get_tmux_send_lock(tmux_name)
    with lock:
        flock_fd = _acquire_flock(tmux_name)
        try:
            if literal:
                r = _remote_run(
                    ["tmux", "send-keys", "-t", tmux_name, "-l", text],
                    host=host,
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
                    input=text,
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
                    with suppress(OSError):
                        os.unlink(tmpfile)
            if r.returncode != 0:
                return False
            r = _remote_run(
                ["tmux", "paste-buffer", "-p", "-r", "-t", tmux_name, "-b", buf_name, "-d"],
                host=host,
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
        timeout=TIMEOUT_TMUX_CHECK,
    )
    if result.returncode != 0:
        return False
    pane_pid = result.stdout.strip()
    if not pane_pid:
        return False
    result = _remote_run(["pgrep", "-P", pane_pid, process_name], host=host, timeout=TIMEOUT_TMUX_CHECK)
    return result.returncode == 0


def tmux_send_escape(tmux_name: str, host: str | None = None) -> None:
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)


def _tmux_pane_pids(host: str | None = None) -> dict[str, str]:
    try:
        result = _remote_run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
            host=host,
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
        import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

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


# ---------------------------------------------------------------------------
# Codex backend, pipes, session helpers (was _claudecode_impl.py)
# ---------------------------------------------------------------------------


def _codex_session_id_path(worker_name: str, sessions_dir: Path) -> Path:
    return sessions_dir / worker_name / "codex_session_id"


def _codex_load_session_id(worker_name: str, sessions_dir: Path) -> str:
    p = _codex_session_id_path(worker_name, sessions_dir)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _codex_save_session_id(worker_name: str, sessions_dir: Path, session_id: str) -> None:
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
    try:
        data = json.dumps(
            {"session": session_name, "text": text, "source": session_name, "backend": "codex", "escape": True}
        ).encode()
        req = urllib.request.Request(f"{bridge_url}/outputs", data=data, headers={"Content-Type": "application/json"})
        with _urlopen(req, timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError) as e:
        _log(_LOG_WARN, "codex", f"Failed to send to bridge: {e}")
        return False


def _codex_adapter_thread(worker_name: str, text: str, bridge_url: str, sessions_dir: Path) -> None:
    with _log_suppress(Exception, level=_LOG_ERROR, tag="codex", msg=f"Adapter thread for '{worker_name}' failed: "):
        session_id = _codex_load_session_id(worker_name, sessions_dir)
        response, new_session_id, _rc = _codex_run(text, session_id)
        if new_session_id:
            _codex_save_session_id(worker_name, sessions_dir, new_session_id)
        if response:
            _codex_send_to_bridge(worker_name, response, bridge_url)


def _codex_adapter_remote(worker_name: str, text: str, bridge_url: str, sessions_dir: Path, host: str) -> None:
    with _log_suppress(
        Exception, level=_LOG_ERROR, tag="codex", msg=f"Remote adapter for '{worker_name}' on {host} failed: "
    ):
        import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

        remote_sessions = bridge._remap_sessions_dir(host)
        sid_file = f"{remote_sessions}/{worker_name}/codex_session_id"
        session_id = ""
        with suppress(OSError, subprocess.TimeoutExpired):
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", host, "cat", sid_file],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0:
                session_id = r.stdout.strip()
        cmd_parts = ["codex", "exec", "--json", "--yolo"]
        if session_id and not session_id.startswith("-"):
            cmd_parts.extend(["resume", session_id, "-"])
        else:
            cmd_parts.append("-")
        ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", host, *cmd_parts]
        result = subprocess.run(ssh_cmd, input=text, capture_output=True, text=True)
        response, new_session_id = _codex_parse_jsonl(result.stdout)
        if result.returncode != 0 and not response:
            response = (result.stderr or "").strip() or "Codex exec failed."
        if new_session_id:
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    [
                        "ssh",
                        "-o",
                        "ConnectTimeout=5",
                        host,
                        "mkdir",
                        "-p",
                        f"{remote_sessions}/{worker_name}",
                        "&&",
                        "echo",
                        shlex.quote(new_session_id),
                        ">",
                        sid_file,
                    ],
                    timeout=10,
                    capture_output=True,
                )
        if response:
            target_url = BRIDGE_PUBLIC_URL or bridge_url
            _codex_send_to_bridge(worker_name, response, target_url)


class CodexBackend:
    name = "codex"
    binary = "codex"
    is_interactive = False
    is_exec = True

    def start_cmd(self, resume_id: str = "") -> str:
        return "echo 'Codex worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str, bridge_url: str, sessions_dir: Path) -> bool:
        import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

        host = bridge.get_worker_host(worker_name)
        if host:
            threading.Thread(
                target=_codex_adapter_remote, args=(worker_name, text, bridge_url, sessions_dir, host), daemon=True
            ).start()
        else:
            threading.Thread(
                target=_codex_adapter_thread, args=(worker_name, text, bridge_url, sessions_dir), daemon=True
            ).start()
        return True

    def is_online(self, tmux_name: str) -> bool:
        return tmux_exists(tmux_name)


def _find_codex_transcript(worker_name: str, host: str | None = None) -> str | None:
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
                host=host,
                timeout=TIMEOUT_REMOTE_CMD,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().split("\n")[0]
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:_find_codex_transcript", f"{type(exc).__name__}: {exc}")
        return None
    matches = glob.glob(os.path.join(codex_dir, "**", f"*{session_id}*"), recursive=True)
    jsonl_matches = [m for m in matches if m.endswith(".jsonl")]
    if jsonl_matches:
        return max(jsonl_matches, key=os.path.getmtime)
    return None


def get_worker_pipe_path(name: str) -> Path:
    return WORKER_PIPE_ROOT / name / "in.pipe"


def ensure_worker_pipe(name: str) -> Path:
    pipe_path = get_worker_pipe_path(name)
    pipe_dir = pipe_path.parent
    pipe_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pipe_dir.chmod(0o700)
    if not pipe_path.exists():
        os.mkfifo(str(pipe_path), mode=0o600)
        _log(_LOG_INFO, "pipe", f"Created worker pipe: {pipe_path}")
    start_pipe_reader(name)
    return pipe_path


def cleanup_worker_pipe(name: str) -> None:
    stop_pipe_reader(name)
    pipe_path = get_worker_pipe_path(name)
    if pipe_path.exists():
        try:
            pipe_path.unlink()
            _log(_LOG_INFO, "pipe", f"Removed worker pipe: {pipe_path}")
        except OSError as e:
            _log(_LOG_WARN, "worker", f"Failed to remove worker pipe {pipe_path}: {e}")
    pipe_dir = pipe_path.parent
    if pipe_dir.exists():
        with suppress(OSError):
            pipe_dir.rmdir()


def pipe_reader_loop(name: str, stop_event: threading.Event) -> None:
    pipe_path = get_worker_pipe_path(name)
    _log(_LOG_INFO, "pipe", f"Pipe reader started for worker '{name}' at {pipe_path}")
    while not stop_event.is_set():
        try:
            if stop_event.is_set():
                break
            with open(str(pipe_path)) as pipe:
                while not stop_event.is_set():
                    line = pipe.readline()
                    if not line:
                        break
                    message = line.strip()
                    if message:
                        _log(
                            _LOG_INFO,
                            "pipe",
                            f"Pipe message for '{name}': {message[:100]}{'...' if len(message) > 100 else ''}",
                        )
                        with _log_suppress(
                            OSError,
                            ValueError,
                            level=_LOG_ERROR,
                            tag="bridge",
                            msg=f"Error forwarding pipe message to '{name}': ",
                        ):
                            _forward_pipe_message(name, message)
        except FileNotFoundError:
            _log(_LOG_WARN, "pipe", f"Pipe for '{name}' no longer exists, stopping reader")
            break
        except OSError as e:
            if stop_event.is_set():
                break
            _log(_LOG_ERROR, "bridge", f"Pipe reader error for '{name}': {e}")
            stop_event.wait(0.5)
    with processes.pipe_readers_lock:
        if name in processes.pipe_readers:
            processes.pipe_readers.pop(name, None)
    _log(_LOG_INFO, "pipe", f"Pipe reader stopped for worker '{name}'")


def _forward_pipe_message(name: str, message: str) -> None:
    import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

    if bridge.worker_manager is None or not bridge.worker_manager.send(name, message):
        _log(_LOG_WARN, "worker", f"Warning: Cannot forward pipe message to '{name}' - worker not found")


def start_pipe_reader(name: str) -> None:
    with processes.pipe_readers_lock:
        if name in processes.pipe_readers:
            thread, _stop = processes.pipe_readers[name]
            if thread.is_alive():
                return
            _log(_LOG_WARN, "pipe", f"Pipe reader thread for '{name}' is dead, restarting")
            processes.pipe_readers.pop(name, None)
    pipe_path = get_worker_pipe_path(name)
    if not pipe_path.exists():
        _log(_LOG_WARN, "bridge", f"Cannot start pipe reader: pipe does not exist for '{name}'")
        return
    stop_event = threading.Event()
    thread = threading.Thread(target=pipe_reader_loop, args=(name, stop_event), daemon=True, name=f"pipe-reader-{name}")
    with processes.pipe_readers_lock:
        processes.pipe_readers[name] = (thread, stop_event)
    thread.start()
    _log(_LOG_INFO, "pipe", f"Started pipe reader thread for '{name}'")


def stop_pipe_reader(name: str) -> None:
    with processes.pipe_readers_lock:
        if name not in processes.pipe_readers:
            return
        thread, stop_event = processes.pipe_readers.pop(name)
    stop_event.set()
    pipe_path = get_worker_pipe_path(name)
    if pipe_path.exists():
        with suppress(OSError):
            fd = os.open(str(pipe_path), os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, b"\n")
            finally:
                os.close(fd)
    thread.join(timeout=TIMEOUT_THREAD_JOIN)
    if thread.is_alive():
        _log(_LOG_WARN, "bridge", f"Warning: pipe reader thread for '{name}' did not stop gracefully")


def get_session_dir(name: str) -> Path:
    return SESSIONS_DIR / name


def ensure_session_dir(name: str) -> Path:
    session_dir = get_session_dir(name)
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    SESSIONS_DIR.chmod(0o700)
    session_dir.chmod(0o700)
    return session_dir


def get_chat_id_file(name: str) -> Path:
    return get_session_dir(name) / "chat_id"


def _scan_latest_session_id(cwd: str, host: str | None = None) -> str:
    if not cwd:
        return ""
    slug = _project_slug(cwd)
    if host:
        try:
            cmd = [
                "bash",
                "-c",
                f'ls -1t "$HOME/.claude/projects/{slug}"/*.jsonl 2>/dev/null | head -1',
            ]
            r = _remote_run(cmd, host=host, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode != 0:
                return ""
            path = (r.stdout or "").strip()
            if not path:
                return ""
            return os.path.basename(path).removesuffix(".jsonl")
        except (subprocess.SubprocessError, OSError):
            return ""
    slug_dir = CLAUDE_PROJECTS_DIR / slug
    if not slug_dir.is_dir():
        return ""
    try:
        jsonls = [p for p in slug_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return ""
    if not jsonls:
        return ""
    latest = max(jsonls, key=lambda p: p.stat().st_mtime)
    return latest.stem


def _log_session_event(name: str, session_id: str, cwd: str, event: str) -> None:
    if not session_id:
        return
    try:
        session_dir = ensure_session_dir(name)
        history_file = session_dir / "session_history.jsonl"
        entry = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time())),
                "session_id": session_id,
                "cwd": cwd or "",
                "event": event,
            }
        )
        with open(history_file, "a") as fh:
            fh.write(entry + "\n")
        history_file.chmod(0o600)
    except OSError as exc:
        _log(_LOG_DEBUG, "io:_log_session_event", f"{type(exc).__name__}: {exc}")


def _ensure_workspace_trusted(
    cwd: str,
    config_path: Path | None = None,
) -> None:
    if not cwd:
        return
    target = config_path or _CLAUDE_JSON_PATH
    try:
        lock_path = target.with_suffix(".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                data = cast(dict[str, object], json.loads(target.read_text())) if target.exists() else {}
                _projects_raw = data.setdefault("projects", {})
                projects = _projects_raw if isinstance(_projects_raw, dict) else {}
                _entry_raw = projects.get(cwd, {})
                entry = _entry_raw if isinstance(_entry_raw, dict) else {}
                if entry.get("hasTrustDialogAccepted") is True:
                    return
                projects[cwd] = {**entry, "hasTrustDialogAccepted": True}
                _tmp = target.with_suffix(".tmp")
                _tmp.write_text(json.dumps(data, indent=2))
                os.replace(str(_tmp), str(target))
                _log(_LOG_INFO, "trust", f"pre-trusted workspace: {cwd}")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError) as exc:
        _log(_LOG_WARN, "trust", f"could not pre-trust {cwd}: {exc}")


def _cache_session_id(name: str, sid: str) -> None:
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
        if sid == old_sid and old_cwd and cwd and old_cwd.rstrip("/") != cwd.rstrip("/"):
            _log(
                _LOG_WARN,
                "session",
                f"{name}: rejecting stale session_id write " f"(sid={sid[:12]}, old_cwd={old_cwd}, current_cwd={cwd})",
            )
            return
        if old_sid != sid:
            _log_session_event(name, sid, cwd, "cache")
        _tmp = id_file.with_suffix(".tmp")
        _tmp.write_text(f"{sid}\n{cwd}")
        _tmp.chmod(0o600)
        os.replace(str(_tmp), str(id_file))
    except OSError as exc:
        _log(_LOG_DEBUG, "io:_cache_session_id", f"{type(exc).__name__}: {exc}")


def get_claude_session_id(name: str) -> str:
    """Read session ID from marker file only. No mtime scan fallback."""
    cache_file = get_session_dir(name) / "claude_session_id"
    current_cwd = get_claude_session_cwd(name)
    if not cache_file.exists():
        return ""
    content = cache_file.read_text().strip()
    if not content:
        return ""
    lines = content.split("\n", 1)
    sid = lines[0].strip()
    if not sid:
        return ""
    if len(lines) > 1:
        cached_cwd = lines[1].strip()
        if cached_cwd and current_cwd and cached_cwd.rstrip("/") != current_cwd.rstrip("/"):
            _log(
                _LOG_INFO,
                "session",
                f"{name}: stale session_id (cached_cwd={cached_cwd}, " f"current_cwd={current_cwd})",
            )
            return ""
    return sid


def scan_session_id(cwd: str, host: str | None = None) -> str:
    """Diagnostic: find newest JSONL by mtime. Does NOT write to marker."""
    return _scan_latest_session_id(cwd, host=host)


def get_claude_session_cwd(name: str) -> str | None:
    import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

    cwd = bridge._get_worker_cwd(name)
    if cwd:
        return os.path.expanduser(cwd)
    return None


def save_claude_session_cwd(name: str, cwd: str) -> None:
    import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

    if cwd:
        cwd = os.path.expanduser(cwd)
    bridge._set_worker_cwd(name, cwd)


def clear_claude_session_id(name: str) -> None:
    id_file = get_session_dir(name) / "claude_session_id"
    if id_file.exists():
        id_file.unlink()


def get_any_session_id(name: str) -> tuple[str, str]:
    session_dir = get_session_dir(name)
    if not session_dir.exists():
        return "", ""
    for f in sorted(session_dir.glob("*_session_id")):
        val = f.read_text().strip()
        if val:
            source = f.name.replace("_session_id", "")
            return val, source
    return "", ""


def _remap_path(local_path: str, host: str | None) -> str:
    if not host or not local_path:
        return local_path
    remote_home = _get_remote_home(host)
    local_home = os.path.expanduser("~")
    if remote_home and remote_home != local_home and local_path.startswith(local_home):
        return remote_home + local_path[len(local_home) :]
    return local_path


def _get_remote_home(host: str | None) -> str:
    with remote_cache.lock:
        cached = remote_cache.home_dirs.get(host or "")
    if cached is not None:
        return cached
    try:
        r = _remote_run(["bash", "-c", "echo $HOME"], host=host, timeout=TIMEOUT_TMUX_SEND)
        home = r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        home = ""
    with remote_cache.lock:
        remote_cache.home_dirs[host or ""] = home
    return home


def _capture_pane_text(tmux_name: str, lines: int = 50, host: str | None = None) -> str:
    if lines <= 0:
        return ""
    try:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", f"-{lines}"],
            host=host,
            timeout=TIMEOUT_TMUX_SEND,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def normalize_backend(backend: str | None) -> str:
    return backend or DEFAULT_BACKEND


def normalize_cwd(cwd: str | None) -> str:
    if cwd is None:
        return ""
    raw = cwd.strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(raw))


def validate_cwd(cwd: str | None, host: str | None = None) -> tuple[str, str]:
    normalized = normalize_cwd(cwd)
    if not normalized:
        return "", "cwd is empty"
    if host:
        try:
            r = _remote_run(["test", "-d", normalized], host=host, timeout=TIMEOUT_REMOTE_CMD)
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


def get_worker_backend(name: str, session: RegistryWorkerDict | TmuxSessionDict | None = None) -> str:
    if session and session.get("backend"):
        return normalize_backend(str(session.get("backend")))
    import bridge  # noqa: PLC0415  # circular: bridge imports claudecode

    registry = bridge._load_registry()
    entry = registry.get("workers", {}).get(name, {})
    if entry.get("backend"):
        return normalize_backend(str(entry["backend"]))
    return DEFAULT_BACKEND


def get_tmux_env_value(tmux_name: str, key: str) -> str:
    result = _subprocess_runner.run(
        ["tmux", "show-environment", "-t", tmux_name, key], capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
    )
    if result.returncode != 0:
        return ""
    value = result.stdout.strip()
    if "=" not in value:
        return ""
    return value.split("=", 1)[1]


def tmux_prompt_empty(tmux_name: str, timeout: float = 0.5, host: str | None = None) -> bool:
    start = _clock.time()
    while _clock.time() - start < timeout:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p"],
            host=host,
            timeout=TIMEOUT_TMUX_CHECK,
        )
        if result.returncode == 0 and re.search(r"^❯\s*$", result.stdout, re.MULTILINE):
            return True
        _clock.sleep(DELAY_BRIEF * 2)
    return False


def export_hook_env(tmux_name: str, backend: str = DEFAULT_WORKER_BACKEND, host: str | None = None) -> None:
    our_url = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    with suppress(urllib.error.URLError, OSError, TimeoutError):
        r = _remote_run(
            ["tmux", "show-environment", "-t", tmux_name, "BRIDGE_URL"],
            host=host,
            timeout=TIMEOUT_TMUX_CHECK,
        )
        existing = r.stdout.strip().split("=", 1)[-1] if r.returncode == 0 else ""
        if existing and existing != our_url:
            _urlopen(existing, timeout=TIMEOUT_THREAD_JOIN).read()
            _log(_LOG_DEBUG, "worker", f"  SKIP export_hook_env({tmux_name}): owned by live bridge at {existing}")
            return
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)], host=host, timeout=TIMEOUT_TMUX_CHECK)
    _remote_run(
        ["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX], host=host, timeout=TIMEOUT_TMUX_CHECK
    )
    sessions_dir_val = _remap_path(str(SESSIONS_DIR), host) if host else str(SESSIONS_DIR)
    _remote_run(
        ["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", sessions_dir_val],
        host=host,
        timeout=TIMEOUT_TMUX_CHECK,
    )
    _remote_run(
        ["tmux", "set-environment", "-t", tmux_name, "WORKER_BACKEND", normalize_backend(backend)],
        host=host,
        timeout=TIMEOUT_TMUX_CHECK,
    )
    bridge_url_val = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    _remote_run(
        ["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", bridge_url_val],
        host=host,
        timeout=TIMEOUT_TMUX_CHECK,
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
