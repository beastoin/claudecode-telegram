from __future__ import annotations

import http.client
import http.cookiejar
import os
import re
import subprocess
import sys
import threading
import time
import traceback as _tb
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, Optional, Protocol, TypedDict, runtime_checkable
from urllib.parse import ParseResult, urlparse

# --- Types ---


class ReminderState(TypedDict):
    response_count: int
    last_reminder_ts: float
    last_response_ts: float
    reminder_pending: bool


class WorkerStateEntry(NamedTuple):
    status: str
    reason: str
    since: float


class TmuxActivityResult(NamedTuple):
    activity: str
    context_pct: str | None
    raw_lines: list[str] | None


class TmuxSessionDict(TypedDict, total=False):
    tmux: str
    backend: str
    host: str
    protocol: str
    callback_url: str
    version: str
    activity: str
    context_pct: str


class RegistryWorkerDict(TypedDict, total=False):
    backend: str
    chat_id: int | None
    hire_time: int
    host: str
    home_host: str | None
    home_cwd: str | None
    protocol: str
    callback_url: str
    version: str
    tools: dict[str, object]


class RegistryFileDict(TypedDict, total=False):
    version: int
    workers: dict[str, RegistryWorkerDict]


class RewindTokenEntry(TypedDict):
    name: str
    expires_at: float


class PrReviewTokenEntry(TypedDict):
    pr_num: int
    owner: str
    repo: str
    expires_at: float


class ProcStatsEntry(TypedDict):
    cpu: float
    state: str


class QuestionOption(TypedDict):
    num: int
    label: str
    selected: bool


class QuestionDetails(TypedDict):
    header: str
    options: list[QuestionOption]
    selected_num: int


class DiskUsageDict(TypedDict, total=False):
    pct: float
    free_gb: float
    total_gb: float
    ts: float


class MemUsageDict(TypedDict, total=False):
    pct: int | float
    used_gb: float
    total_gb: float
    avail_gb: float
    top_procs: list[dict[str, object]]
    ts: float


class IoUsageDict(TypedDict, total=False):
    iowait_pct: float
    read_iops: int
    write_iops: int
    util_pct: float
    ts: float


class WorktreeItemDict(TypedDict):
    path: str
    size_gb: float


class WorktreeUsageDict(TypedDict):
    total_gb: float
    items: list[WorktreeItemDict]
    ts: float


class CpuHogEntry(TypedDict, total=False):
    pid: int
    cpu: float
    etime_min: int
    cmd: str


class MachineHealthDict(TypedDict, total=False):
    status: str
    down_since: float | None
    last_error: str | None
    disk: DiskUsageDict | None
    memory: MemUsageDict | None
    io: IoUsageDict | None


class CodexTranscriptEntry(TypedDict, total=False):
    role: str
    text: str
    timestamp: str


class GitPushStateResult(TypedDict, total=False):
    orig_sha: str
    orig_branch: str
    staged_files: list[str]
    stash_sha: str | None


class TelegramApiResponseDict(TypedDict, total=False):
    ok: bool
    result: object
    description: str
    error_code: int


class TelegramUser(TypedDict, total=False):
    id: int
    is_bot: bool
    first_name: str
    last_name: str
    username: str


class TelegramChat(TypedDict, total=False):
    id: int
    type: str
    title: str
    username: str


class TelegramPhotoSize(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int


class TelegramDocument(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    file_name: str
    mime_type: str
    file_size: int


class TelegramVoice(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    duration: int
    mime_type: str
    file_size: int


class TelegramVideo(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    duration: int
    file_name: str
    mime_type: str
    file_size: int


class TelegramAudio(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    duration: int
    performer: str
    title: str
    file_name: str
    mime_type: str
    file_size: int


class TelegramSticker(TypedDict, total=False):
    file_id: str
    file_unique_id: str
    width: int
    height: int
    emoji: str
    type: str
    is_animated: bool
    is_video: bool


class TelegramMessageDict(TypedDict, total=False):
    message_id: int
    chat: TelegramChat
    date: int
    text: str
    photo: list[TelegramPhotoSize]
    document: TelegramDocument
    voice: TelegramVoice
    video: TelegramVideo
    video_note: TelegramVideo
    animation: TelegramDocument
    audio: TelegramAudio
    sticker: TelegramSticker
    reply_to_message: TelegramMessageDict
    caption: str
    media_group_id: str
    rich_message: dict[str, str]


TelegramCallbackQuery = TypedDict(
    "TelegramCallbackQuery",
    {
        "id": str,
        "from": TelegramUser,
        "message": TelegramMessageDict,
        "data": str,
    },
    total=False,
)


class TelegramUpdate(TypedDict, total=False):
    update_id: int
    message: TelegramMessageDict
    edited_message: TelegramMessageDict
    callback_query: TelegramCallbackQuery


class FileValidation(NamedTuple):
    ok: bool
    detail: Path | str


class MediaGroupEntry(TypedDict):
    items: list[TelegramMessageDict]
    caption: str
    timer: threading.Timer | None


class WorkerEndpointInfo(TypedDict, total=False):
    name: str
    backend: str
    status: str
    host: str
    tmux: str
    protocol: str
    send_example: str
    machine: str
    address: str
    note: str


class MachinePublicDict(TypedDict, total=False):
    id: str
    display_name: str
    ssh_target: str | None
    bridge_base_url: str
    home_root: str
    os_family: str
    tailscale_ip: str
    role: str
    configured: bool
    access: str
    workers: list[dict[str, str]]
    worker_count: int
    health: MachineHealthDict


class MachinesCatalogResponse(TypedDict):
    version: int
    config_path: str
    caller: str | None
    machines: list[MachinePublicDict]


class ConnectorMessageLogEntry(TypedDict):
    ts: float
    html: str
    plain: str
    targets: list[str]


class ConnectorMetadataDict(TypedDict, total=False):
    number: int
    repo: str


class ConnectorStatusDict(TypedDict, total=False):
    name: str
    running: bool
    error: str
    enabled: bool


class MentionRouteResult(TypedDict):
    name: str
    status: str


class HookResponseBody(TypedDict, total=False):
    session: str
    text: str
    source: str
    backend: str
    escape: bool
    session_id: str
    name: str
    worker: str
    to: str
    target: str
    message: str


class TelegramWebhookBody(TypedDict, total=False):
    update_id: int
    message: TelegramMessageDict
    callback_query: TelegramCallbackQuery
    edited_message: TelegramMessageDict


class NodeConfigDict(TypedDict, total=False):
    admin_chat_id: int
    tunnel: str
    host: str
    port: int
    webhook_secret: str
    connectors: dict[str, object]


class _MentionSnapshot(NamedTuple):
    target: str | None
    mention_count: int
    ts: float


class _StateSnapshot(NamedTuple):
    active: str | None
    startup_notified: bool


PostRouteHandler = Callable[..., None]  # (handler, body, match) -> None
GetRouteHandler = Callable[..., None]  # (handler, parsed, match) -> None


class GmailHeader(TypedDict, total=False):
    name: str
    value: str


class GmailBody(TypedDict, total=False):
    data: str
    size: int
    attachmentId: str


class GmailPayloadPart(TypedDict, total=False):
    mimeType: str
    filename: str
    body: GmailBody
    headers: list[GmailHeader]
    parts: list[GmailPayloadPart]


class GmailMessage(TypedDict, total=False):
    id: str
    threadId: str
    labelIds: list[str]
    payload: GmailPayloadPart
    historyId: str


class GmailMessageRef(TypedDict, total=False):
    message: GmailMessage


class GmailHistoryEntry(TypedDict, total=False):
    messagesAdded: list[GmailMessageRef]


class GmailHistoryResponse(TypedDict, total=False):
    history: list[GmailHistoryEntry]
    historyId: str


class GmailProfile(TypedDict, total=False):
    emailAddress: str
    historyId: str


class GmailAttachmentData(TypedDict, total=False):
    data: str


class GmailMessageListResponse(TypedDict, total=False):
    messages: list[GmailMessage]


class GmailAttachmentInfo(TypedDict):
    filename: str
    mimeType: str
    size: int
    attachmentId: str


class GithubUser(TypedDict, total=False):
    login: str


class GithubComment(TypedDict, total=False):
    id: int
    body: str
    user: GithubUser
    html_url: str
    issue_url: str
    pull_request_url: str
    issue_num: str
    kind: str


class GithubState(TypedDict):
    last_poll_time: str | None
    seen_ids: list[int]


class Attachment(TypedDict, total=False):
    path: str
    filename: str
    mimeType: str


class ConnectorMetadata(TypedDict, total=False):
    number: str | None
    repo: str
    comment_id: int


class IssueContext(TypedDict):
    number: str
    kind: str
    url: str


class ConnectorStatus(TypedDict):
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool


class GmailStatus(TypedDict):
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool
    history_id: str | None


class GithubStatus(TypedDict):
    name: str
    running: bool
    sender_filter: str
    poll_interval: int
    consecutive_failures: int
    alert_sent: bool
    repos: list[str]
    last_poll_time: str | None
    seen_ids_count: int


class TranscriptSyncState(TypedDict, total=False):
    status: str
    progress: str
    error: str | None
    path: str | None
    started: float
    pct: int
    remote_size: int


class TranscriptEntry(TypedDict, total=False):
    type: str
    message: dict[str, object]
    timestamp: str
    version: str
    gitBranch: str
    _idx: int


class TranscriptStatsDict(TypedDict):
    n_user: int
    n_tool: int
    n_edit: int
    lines_add: int
    lines_del: int
    lines_mod: int
    n_files: int
    model: str
    version: str
    git_branch: str
    first_ts: str
    last_ts: str
    input_tokens: int
    output_tokens: int
    duration: str


class _GuestSessionDictRequired(TypedDict):
    name: str
    created_at: str
    expires_at_unix: float
    notified_workers: set[str] | list[str]


class ChannelMemberDict(TypedDict, total=False):
    type: str
    name: str


ChannelMessageDict = TypedDict(
    "ChannelMessageDict",
    {
        "id": str,
        "seq": int,
        "from": str,
        "text": str,
        "ts": int,
    },
)


class ChannelDict(TypedDict):
    id: str
    label: str
    created_at: str
    expires_at_unix: float
    seq: int
    created_by: str
    members: dict[str, ChannelMemberDict]
    messages: list[ChannelMessageDict]


_RelayMessageDictRequired = TypedDict(
    "_RelayMessageDictRequired",
    {
        "message_id": str,
        "direction": str,
        "from": str,
        "to": str,
        "text": str,
        "ts": str,
    },
)


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


# --- Core ---

VERSION = "0.51.0"


def _str_field(d: Mapping[str, object], key: str, default: str = "") -> str:
    val = d.get(key, default)
    return str(val) if val is not None else default


def _int_field(d: Mapping[str, object], key: str, default: int = 0) -> int:
    val = d.get(key, default)
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return default
    return default


def _dict_field(d: Mapping[str, object], key: str) -> Mapping[str, object]:
    val = d.get(key)
    return val if isinstance(val, dict) else {}


def _bool_field(d: Mapping[str, object], key: str, default: bool = False) -> bool:
    val = d.get(key, default)
    return bool(val)


_LOG_ERROR: str = "ERROR"
_LOG_WARN: str = "WARN"
_LOG_INFO: str = "INFO"
_LOG_DEBUG: str = "DEBUG"


def _log(level: str, component: str, msg: str | Path, *, exc: BaseException | None = None) -> None:
    print(f"[{level}:{component}] {msg}", file=sys.stderr, flush=True)
    if exc is not None:
        _tb.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def _log_best_effort(label: str, func: Callable[..., object], *args: object, **kwargs: object) -> object | None:  # type: ignore[explicit-any]
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        _log(_LOG_DEBUG, label, f"{type(exc).__name__}: {exc}")
        return None


@contextmanager
def _log_suppress(*exc_types, level=_LOG_WARN, tag="bridge", msg="", exc_info=False):
    try:
        yield
    except exc_types as e:
        _log(level, tag, f"{msg}{e}" if msg else f"{type(e).__name__}: {e}", exc=e if exc_info else None)


class MarkdownToken(Protocol):
    type: str
    content: str
    children: list[MarkdownToken] | None
    attrs: dict[str, str] | None


class SubprocessRunner(Protocol):
    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]: ...
    def popen(self, args: list[str], **kwargs: object) -> subprocess.Popen[str]: ...
class Clock(Protocol):
    def time(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
class _RealSubprocessRunner:
    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, **kwargs)  # type: ignore[call-overload,no-any-return]

    def popen(self, args: list[str], **kwargs: object) -> subprocess.Popen[str]:
        return subprocess.Popen(args, **kwargs)  # type: ignore[call-overload,no-any-return]


class _RealClock:
    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_subprocess_runner: SubprocessRunner = _RealSubprocessRunner()
_clock: Clock = _RealClock()
_urlopen: Callable[..., http.client.HTTPResponse] = urllib.request.urlopen  # type: ignore[explicit-any]


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0
    retryable_status: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RateLimitConfig:
    requests_per_second: float = 30.0
    burst: int = 30


class _TokenBucket:
    def __init__(self, rate: float, burst: int, clock: Clock | None = None) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock or _RealClock()
        self._tokens = float(burst)
        self._last_refill = self._clock.time()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = self._clock.time() + timeout
        while True:
            with self._lock:
                now = self._clock.time()
                elapsed = now - self._last_refill
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            wait = min(1.0 / self._rate, deadline - self._clock.time())
            if wait <= 0:
                return False
            self._clock.sleep(wait)


class HttpClient:
    def __init__(  # type: ignore[explicit-any]
        self,
        retry: RetryConfig | None = None,
        rate_limit: RateLimitConfig | None = None,
        clock: Clock | None = None,
        urlopen: Callable[..., http.client.HTTPResponse] | None = None,
    ) -> None:
        self._retry = retry or RetryConfig()
        self._rate_config = rate_limit or RateLimitConfig()
        self._clock = clock or _RealClock()
        self._urlopen_fn = urlopen or urllib.request.urlopen
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self._limiters: dict[str, _TokenBucket] = {}
        self._limiters_lock = threading.Lock()

    def _get_limiter(self, host: str) -> _TokenBucket:
        with self._limiters_lock:
            if host not in self._limiters:
                self._limiters[host] = _TokenBucket(
                    rate=self._rate_config.requests_per_second,
                    burst=self._rate_config.burst,
                    clock=self._clock,
                )
            return self._limiters[host]

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        retry: RetryConfig | None = None,
    ) -> http.client.HTTPResponse:
        host = urlparse(url).hostname or "localhost"
        cfg = retry or self._retry
        limiter = self._get_limiter(host)
        if not limiter.acquire(timeout=timeout):
            raise TimeoutError(f"Rate limit timeout for {host}")
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        last_exc: BaseException | None = None
        delay = cfg.initial_delay
        for attempt in range(cfg.max_retries + 1):
            try:
                return self._urlopen_fn(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code not in cfg.retryable_status:
                    raise
                last_exc = e
                _log(_LOG_WARN, "http", f"{method} {url} -> {e.code} (attempt {attempt+1}/{cfg.max_retries+1})")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_exc = e
                _log(
                    _LOG_WARN, "http", f"{method} {url} -> {type(e).__name__} (attempt {attempt+1}/{cfg.max_retries+1})"
                )
            if attempt < cfg.max_retries:
                self._clock.sleep(min(delay, cfg.max_delay))
                delay *= cfg.backoff_factor
        raise last_exc  # type: ignore[misc]

    def get(self, url: str, *, timeout: float = 30.0, retry: RetryConfig | None = None) -> http.client.HTTPResponse:
        return self.request(url, method="GET", timeout=timeout, retry=retry)

    def post(
        self,
        url: str,
        data: bytes,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        retry: RetryConfig | None = None,
    ) -> http.client.HTTPResponse:
        return self.request(url, method="POST", data=data, headers=headers, timeout=timeout, retry=retry)


_tg_http_client: HttpClient = HttpClient(
    rate_limit=RateLimitConfig(requests_per_second=25.0, burst=30),
    retry=RetryConfig(max_retries=3, initial_delay=0.5, retryable_status=frozenset({429, 500, 502, 503})),
)
NODE_NAME = os.environ.get("NODE_NAME", "")
_DEFAULT_PORTS = {"prod": 8271, "dev": 8272, "test": 8295}
if NODE_NAME and not os.environ.get("PORT"):
    PORT = _DEFAULT_PORTS.get(NODE_NAME, 8270)
else:
    PORT = int(os.environ.get("PORT", "8270"))
BRIDGE_BIND = os.environ.get("BRIDGE_BIND", "127.0.0.1")
if NODE_NAME and not os.environ.get("SESSIONS_DIR"):
    SESSIONS_DIR = Path.home() / ".claude" / "telegram" / "nodes" / NODE_NAME / "sessions"
else:
    SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))
if NODE_NAME and not os.environ.get("TMUX_PREFIX"):
    TMUX_PREFIX = f"claude-{NODE_NAME}-"
else:
    TMUX_PREFIX = os.environ.get("TMUX_PREFIX", "claude-")
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
CLAUDE_SETTINGS_FILE = Path(os.environ.get("CLAUDE_SETTINGS_FILE", CLAUDE_DIR / "settings.json"))
_bridge_url_env = os.environ.get("BRIDGE_URL", "").rstrip("/")
if _bridge_url_env and not _bridge_url_env.startswith(("http://localhost", "http://127.0.0.1")):
    BRIDGE_URL = _bridge_url_env
else:
    BRIDGE_URL = f"http://localhost:{PORT}"
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/")
if BRIDGE_PUBLIC_URL and not os.environ.get("BRIDGE_BIND"):
    from urllib.parse import urlparse as _urlparse_pub

    _pub_host = _urlparse_pub(BRIDGE_PUBLIC_URL).hostname or ""
    BRIDGE_BIND = _pub_host if _pub_host and _pub_host not in ("localhost",) else "0.0.0.0"
    if BRIDGE_BIND != "0.0.0.0":
        BRIDGE_URL = f"http://{BRIDGE_BIND}:{PORT}"
BRIDGE_SSH_TARGET = os.environ.get("BRIDGE_SSH_TARGET", "vps")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NODE_DIR = SESSIONS_DIR.parent if NODE_NAME else SESSIONS_DIR.parent
_node_name = TMUX_PREFIX.strip("-").removeprefix("claude-") or "default"
MACHINES_CONFIG_FILE = Path(
    os.environ.get("MACHINES_CONFIG_FILE", Path.home() / ".config" / "claudecode-telegram" / "machines.json")
)
TIMEOUT_TMUX_CHECK = 3
TIMEOUT_TMUX_SEND = 5
TIMEOUT_REMOTE_CMD = 10
TIMEOUT_FILE_TRANSFER = 15
TIMEOUT_GIT_OP = 30
TIMEOUT_LARGE_TRANSFER = 60
TIMEOUT_RSYNC = 120
TIMEOUT_FULL_SYNC = 600
TIMEOUT_HTTP_API = 10
TIMEOUT_HTTP_DOWNLOAD = 30
TIMEOUT_HTTP_UPLOAD = 60
TIMEOUT_PROCESS_WAIT = 3
TIMEOUT_THREAD_JOIN = 1.0
DELAY_TMUX_SEND = 0.3
DELAY_PIPE_POLL = 0.5
DELAY_STARTUP = 1.0
DELAY_STARTUP_LONG = 1.5
DELAY_RETRY = 0.5
DELAY_BRIEF = 0.05
DELAY_SHORT = 0.2
DELAY_RESPONSE_GAP = 2
DELAY_PROCESS_SETTLE = 3
DELAY_CLAUDE_LOAD = 4
DEFAULT_BACKEND = "claude"
DEFAULT_WORKER_BACKEND = DEFAULT_BACKEND
PENDING_TIMEOUT = 600
FILE_INBOX_ROOT = Path(f"/tmp/claudecode-telegram/{_node_name}")
WORKER_PIPE_ROOT = Path(f"/tmp/claudecode-telegram/{_node_name}")
TEAM_DIR = os.path.expanduser(os.environ.get("TEAM_DIR", "~/team"))
_CHECKIN_NOTE_PATH = os.path.join(TEAM_DIR, "checkin-note.txt")
_LEARNING_REMINDER_PATH = os.path.join(TEAM_DIR, "learning-reminder.txt")
PERSISTENCE_NOTE = "They'll stay on your team."
STT_ENDPOINT = os.environ.get("STT_ENDPOINT", "http://100.126.187.125:10110/transcribe")
STT_TIMEOUT = int(os.environ.get("STT_TIMEOUT", "10"))
ADMIN_CHAT_ID_ENV = os.environ.get("ADMIN_CHAT_ID", "")
admin_chat_id: int | None = int(ADMIN_CHAT_ID_ENV) if ADMIN_CHAT_ID_ENV else None


@dataclass(frozen=True)
class WatchdogConfig:
    interval: int = 4
    start_grace: int = 30
    think_grace: int = 30
    tool_gap_grace: int = 12
    stale_pending: int = 900
    cpu_active: float = 15.0
    cpu_idle: float = 7.0
    idle_streak_stuck: int = 3
    alert_cooldown: int = 180
    restart_cooldown: int = 60
    host_down_threshold: int = 3


@dataclass(frozen=True)
class ResourceAlertConfig:
    disk_warn_pct: int = 85
    disk_alert_pct: int = 95
    disk_alert_gb: int = 5
    disk_cooldown: int = 3600
    cpu_hog_pct: int = 90
    cpu_hog_duration_min: int = 60
    cpu_hog_cooldown: int = 3600
    worktree_threshold_gb: int = 30
    worktree_cooldown: int = 3600
    mem_threshold_pct: int = 90
    mem_threshold_gb: int = 4
    mem_cooldown: int = 3600
    io_iowait_pct: int = 30
    io_cooldown: int = 3600
    infra_cooldown: int = 300


@dataclass(frozen=True)
class TunnelConfig:
    mode: Literal["auto", "poll", "provided", "none"] = "poll"
    provided_url: str = ""
    cloudflared_binary: str = "cloudflared"
    startup_timeout: int = 60
    max_restart_attempts: int = 5
    initial_backoff: int = 10
    watchdog_interval: int = 10
    reachability_timeout: int = 10
    webhook_retry_delays: tuple[int, ...] = (0, 5, 10, 20, 40, 80)
    webhook_check_cycles: int = 6
    poll_timeout: int = 30
    poll_error_delay: int = 2
    port_wait_timeout: int = 30


TUNNEL_MODE: str = os.environ.get("TUNNEL_MODE", "poll")
TUNNEL_URL: str = os.environ.get("TUNNEL_URL", "")


def _build_tunnel_config() -> TunnelConfig:
    mode: Literal["auto", "poll", "provided", "none"]
    tunnel_url = TUNNEL_URL
    raw = TUNNEL_MODE.lower()
    if raw == "none":
        mode = "none"
    elif raw == "poll":
        mode = "poll"
    elif tunnel_url:
        mode = "provided"
    elif raw == "provided":
        mode = "provided"
    elif raw == "auto":
        mode = "auto"
    else:
        mode = "poll"
    return TunnelConfig(mode=mode, provided_url=tunnel_url)


_wd_cfg = WatchdogConfig()
_res_cfg = ResourceAlertConfig()
WATCHDOG_INTERVAL = _wd_cfg.interval
START_GRACE = _wd_cfg.start_grace
THINK_GRACE = _wd_cfg.think_grace
TOOL_GAP_GRACE = _wd_cfg.tool_gap_grace
STALE_PENDING = _wd_cfg.stale_pending
CPU_ACTIVE = _wd_cfg.cpu_active
CPU_IDLE = _wd_cfg.cpu_idle
IDLE_STREAK_STUCK = _wd_cfg.idle_streak_stuck
ALERT_COOLDOWN = _wd_cfg.alert_cooldown
RESTART_COOLDOWN = _res_cfg.disk_cooldown
DISK_WARN_PCT = _res_cfg.disk_warn_pct
DISK_ALERT_PCT = _res_cfg.disk_alert_pct
DISK_ALERT_GB = _res_cfg.disk_alert_gb
DISK_COOLDOWN = _res_cfg.disk_cooldown
CPU_HOG_PCT = _res_cfg.cpu_hog_pct
CPU_HOG_DURATION_MIN = _res_cfg.cpu_hog_duration_min
CPU_HOG_COOLDOWN = _res_cfg.cpu_hog_cooldown
WORKTREE_THRESHOLD_GB = _res_cfg.worktree_threshold_gb
WORKTREE_COOLDOWN = _res_cfg.worktree_cooldown
MEM_THRESHOLD_PCT = _res_cfg.mem_threshold_pct
MEM_THRESHOLD_GB = _res_cfg.mem_threshold_gb
MEM_COOLDOWN = _res_cfg.mem_cooldown
IO_IOWAIT_PCT = _res_cfg.io_iowait_pct
IO_COOLDOWN = _res_cfg.io_cooldown
INFRA_COOLDOWN = _res_cfg.infra_cooldown
HOST_DOWN_THRESHOLD = _wd_cfg.host_down_threshold
TRANSPORT_MODE = os.environ.get("TRANSPORT_MODE", "telegram")


@dataclass
class AppContext:
    bot_token: str = ""
    port: int = 8270
    bridge_bind: str = "127.0.0.1"
    bridge_url: str = ""
    bridge_public_url: str = ""
    bridge_ssh_target: str = "vps"
    sessions_dir: Path | None = None
    tmux_prefix: str = "claude-"
    node_name: str = ""
    claude_dir: Path | None = None
    default_backend: str = "claude"
    team_dir: str = ""
    watchdog_interval: int = 4
    webhook_secret: str = ""
    transport_mode: str = "telegram"

    def __post_init__(self) -> None:
        if self.sessions_dir is None:
            self.sessions_dir = Path.home() / ".claude" / "telegram" / "sessions"
        if self.claude_dir is None:
            self.claude_dir = Path.home() / ".claude"
        if not self.bridge_url:
            self.bridge_url = f"http://localhost:{self.port}"


def _build_app_context() -> AppContext:
    return AppContext(
        bot_token=BOT_TOKEN,
        port=PORT,
        bridge_bind=BRIDGE_BIND,
        bridge_url=BRIDGE_URL,
        bridge_public_url=BRIDGE_PUBLIC_URL,
        bridge_ssh_target=BRIDGE_SSH_TARGET,
        sessions_dir=SESSIONS_DIR,
        tmux_prefix=TMUX_PREFIX,
        node_name=NODE_NAME,
        claude_dir=CLAUDE_DIR,
        default_backend=DEFAULT_BACKEND,
        team_dir=TEAM_DIR,
        watchdog_interval=WATCHDOG_INTERVAL,
        webhook_secret=WEBHOOK_SECRET,
        transport_mode=TRANSPORT_MODE,
    )


_CLAUDE_JSON_PATH = Path.home() / ".claude.json"
CLAUDE_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
_app_context: AppContext | None = None


def get_app_context() -> AppContext:
    global _app_context
    if _app_context is None:
        _app_context = _build_app_context()
    return _app_context
