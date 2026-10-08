from __future__ import annotations
import http.client
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, runtime_checkable

# ── Version ────────────────────────────────────────────────────────────
VERSION = "0.47.0"

# ── Safe JSON field accessors ──────────────────────────────────────────

def _str_field(d: Mapping[str, object], key: str, default: str = "") -> str:
    val = d.get(key, default)
    return str(val) if val is not None else default

def _int_field(d: Mapping[str, object], key: str, default: int = 0) -> int:
    val = d.get(key, default)
    if isinstance(val, int): return val
    if isinstance(val, str):
        try: return int(val)
        except ValueError: return default
    return default

def _dict_field(d: Mapping[str, object], key: str) -> Mapping[str, object]:
    val = d.get(key)
    return val if isinstance(val, dict) else {}

def _bool_field(d: Mapping[str, object], key: str, default: bool = False) -> bool:
    val = d.get(key, default)
    return bool(val)

# ── Structured logging ─────────────────────────────────────────────────
_LOG_ERROR: str = "ERROR"
_LOG_WARN: str = "WARN"
_LOG_INFO: str = "INFO"
_LOG_DEBUG: str = "DEBUG"

def _log(level: str, component: str, msg: str | Path, *,
         exc: BaseException | None = None) -> None:
    print(f"[{level}:{component}] {msg}", file=sys.stderr, flush=True)
    if exc is not None:
        import traceback as _tb
        _tb.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)

def _log_best_effort(label: str, func: Callable[..., object], *args: object, **kwargs: object) -> object | None:  # type: ignore[explicit-any]
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        _log(_LOG_DEBUG, label, f"{type(exc).__name__}: {exc}")
        return None

# ── DI seams (injectable for testing) ──────────────────────────────────

class MarkdownToken(Protocol):
    type: str
    content: str
    children: list['MarkdownToken'] | None
    attrs: dict[str, str] | None

class SubprocessRunner(Protocol):
    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ...

    def popen(self, args: list[str], **kwargs: object) -> subprocess.Popen[str]:
        ...

class Clock(Protocol):
    def time(self) -> float:
        ...

    def sleep(self, seconds: float) -> None:
        ...

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

# ── HTTP client infrastructure ────────────────────────────────────────
import http.cookiejar
import threading
import urllib.error

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
        self._rate = rate; self._burst = burst; self._clock = clock or _RealClock(); self._tokens = float(burst)
        self._last_refill = self._clock.time(); self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = self._clock.time() + timeout
        while True:
            with self._lock:
                now = self._clock.time(); elapsed = now - self._last_refill
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate); self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            wait = min(1.0 / self._rate, deadline - self._clock.time())
            if wait <= 0: return False
            self._clock.sleep(wait)

class HttpClient:
    def __init__(  # type: ignore[explicit-any]
        self,
        retry: RetryConfig | None = None,
        rate_limit: RateLimitConfig | None = None,
        clock: Clock | None = None,
        urlopen: Callable[..., http.client.HTTPResponse] | None = None,
    ) -> None:
        self._retry = retry or RetryConfig(); self._rate_config = rate_limit or RateLimitConfig()
        self._clock = clock or _RealClock(); self._urlopen_fn = urlopen or urllib.request.urlopen
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), )
        self._limiters: dict[str, _TokenBucket] = {}
        self._limiters_lock = threading.Lock()

    def _get_limiter(self, host: str) -> _TokenBucket:
        with self._limiters_lock:
            if host not in self._limiters:
                self._limiters[host] = _TokenBucket(
                    rate=self._rate_config.requests_per_second,
                    burst=self._rate_config.burst,
                    clock=self._clock, )
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
        from urllib.parse import urlparse
        host = urlparse(url).hostname or "localhost"; cfg = retry or self._retry; limiter = self._get_limiter(host)
        if not limiter.acquire(timeout=timeout): raise TimeoutError(f"Rate limit timeout for {host}")

        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for k, v in headers.items(): req.add_header(k, v)
        last_exc: BaseException | None = None
        delay = cfg.initial_delay

        for attempt in range(cfg.max_retries + 1):
            try:
                return self._urlopen_fn(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code not in cfg.retryable_status: raise
                last_exc = e
                _log(_LOG_WARN, "http", f"{method} {url} -> {e.code} (attempt {attempt+1}/{cfg.max_retries+1})")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_exc = e
                _log(_LOG_WARN, "http", f"{method} {url} -> {type(e).__name__} (attempt {attempt+1}/{cfg.max_retries+1})")

            if attempt < cfg.max_retries:
                self._clock.sleep(min(delay, cfg.max_delay))
                delay *= cfg.backoff_factor
        raise last_exc  # type: ignore[misc]

    def get(self, url: str, *, timeout: float = 30.0,
            retry: RetryConfig | None = None) -> http.client.HTTPResponse:
        return self.request(url, method="GET", timeout=timeout, retry=retry)

    def post(self, url: str, data: bytes, *, headers: dict[str, str] | None = None,
             timeout: float = 30.0,
             retry: RetryConfig | None = None) -> http.client.HTTPResponse:
        return self.request(url, method="POST", data=data, headers=headers,
                            timeout=timeout, retry=retry)

    def head(self, url: str, *, timeout: float = 30.0,
             retry: RetryConfig | None = None) -> http.client.HTTPResponse:
        return self.request(url, method="HEAD", timeout=timeout, retry=retry)

_http_client: HttpClient = HttpClient()
_tg_http_client: HttpClient = HttpClient(
    rate_limit=RateLimitConfig(requests_per_second=25.0, burst=30),
    retry=RetryConfig(max_retries=3, initial_delay=0.5, retryable_status=frozenset({429, 500, 502, 503})), )

# ── Node-derived configuration ─────────────────────────────────────────
NODE_NAME = os.environ.get("NODE_NAME", "")
_DEFAULT_PORTS = {"prod": 8271, "dev": 8272, "test": 8295}

if NODE_NAME and not os.environ.get("PORT"): PORT = _DEFAULT_PORTS.get(NODE_NAME, 8270)
else: PORT = int(os.environ.get("PORT", "8270"))
BRIDGE_BIND = os.environ.get("BRIDGE_BIND", "127.0.0.1")

if NODE_NAME and not os.environ.get("SESSIONS_DIR"):
    SESSIONS_DIR = Path.home() / ".claude" / "telegram" / "nodes" / NODE_NAME / "sessions"
else: SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", Path.home() / ".claude" / "telegram" / "sessions"))

if NODE_NAME and not os.environ.get("TMUX_PREFIX"): TMUX_PREFIX = f"claude-{NODE_NAME}-"
else: TMUX_PREFIX = os.environ.get("TMUX_PREFIX", "claude-")
CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
CLAUDE_SETTINGS_FILE = Path(os.environ.get("CLAUDE_SETTINGS_FILE", CLAUDE_DIR / "settings.json"))
_bridge_url_env = os.environ.get("BRIDGE_URL", "").rstrip("/")
if _bridge_url_env and not _bridge_url_env.startswith(("http://localhost", "http://127.0.0.1")):
    BRIDGE_URL = _bridge_url_env
else: BRIDGE_URL = f"http://localhost:{PORT}"
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "").rstrip("/")
if BRIDGE_PUBLIC_URL and not os.environ.get("BRIDGE_BIND"):
    from urllib.parse import urlparse as _urlparse_pub
    _pub_host = _urlparse_pub(BRIDGE_PUBLIC_URL).hostname or ""
    BRIDGE_BIND = _pub_host if _pub_host and _pub_host not in ("localhost",) else "0.0.0.0"
    if BRIDGE_BIND != "0.0.0.0": BRIDGE_URL = f"http://{BRIDGE_BIND}:{PORT}"
BRIDGE_SSH_TARGET = os.environ.get("BRIDGE_SSH_TARGET", "vps")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NODE_DIR = SESSIONS_DIR.parent if NODE_NAME else SESSIONS_DIR.parent
_node_name = TMUX_PREFIX.strip("-").removeprefix("claude-") or "default"
MACHINES_CONFIG_FILE = Path(os.environ.get(
    "MACHINES_CONFIG_FILE",
    Path.home() / ".config" / "claudecode-telegram" / "machines.json"
))

# ── Timeouts (seconds) ─────────────────────────────────────────────────
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

# ── Worker/session defaults ────────────────────────────────────────────
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

# ── Config dataclasses ──────────────────────────────────────────────────

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
class MediaConfig:
    max_file_size: int = 50 * 1024 * 1024
    photo_max_sum: int = 10000
    photo_max_dim: int = 5000

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

# ── Tunnel env vars ────────────────────────────────────────────────────
TUNNEL_MODE: str = os.environ.get("TUNNEL_MODE", "poll")
TUNNEL_URL: str = os.environ.get("TUNNEL_URL", "")

def _build_tunnel_config() -> TunnelConfig:
    mode: Literal["auto", "poll", "provided", "none"]
    tunnel_url = TUNNEL_URL; raw = TUNNEL_MODE.lower()
    if raw == "none": mode = "none"
    elif raw == "poll": mode = "poll"
    elif tunnel_url: mode = "provided"
    elif raw == "provided": mode = "provided"
    elif raw == "auto": mode = "auto"
    else: mode = "poll"
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

# ── Derived resource constants ──────────────────────────────────────────
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

# ── AppContext: injectable configuration ────────────────────────────────
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
        if self.sessions_dir is None: self.sessions_dir = Path.home() / ".claude" / "telegram" / "sessions"
        if self.claude_dir is None: self.claude_dir = Path.home() / ".claude"
        if not self.bridge_url: self.bridge_url = f"http://localhost:{self.port}"

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
        transport_mode=TRANSPORT_MODE, )
_app_context: AppContext | None = None

def get_app_context() -> AppContext:
    global _app_context
    if _app_context is None: _app_context = _build_app_context()
    return _app_context
