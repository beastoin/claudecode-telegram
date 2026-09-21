#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge - Multi-Session Control Panel"""

VERSION = "0.44.0"

from dataclasses import dataclass, field
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
import types
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
from typing import IO, Any, Callable, Iterator, Literal, NamedTuple, Protocol, TypedDict, runtime_checkable

# ── Type aliases for clarity ────────────────────────────────────────────
ChatId = int
MessageId = int
ParseMode = Literal["HTML", "MarkdownV2"] | None
TelegramApiResponse = dict[str, Any] | None

# ── TypedDict models for raw JSON shapes ────────────────────────────────


class TelegramUser(TypedDict, total=False):
    """Telegram User object fields."""
    id: int
    is_bot: bool
    first_name: str
    last_name: str
    username: str


class TelegramChat(TypedDict, total=False):
    """Telegram Chat object fields."""
    id: int
    type: str
    title: str
    username: str


class TelegramPhotoSize(TypedDict, total=False):
    """Telegram PhotoSize object fields."""
    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int


class TelegramDocument(TypedDict, total=False):
    """Telegram Document object fields."""
    file_id: str
    file_unique_id: str
    file_name: str
    mime_type: str
    file_size: int


class TelegramVoice(TypedDict, total=False):
    """Telegram Voice object fields."""
    file_id: str
    file_unique_id: str
    duration: int
    mime_type: str
    file_size: int


class TelegramVideo(TypedDict, total=False):
    """Telegram Video object fields."""
    file_id: str
    file_unique_id: str
    width: int
    height: int
    duration: int
    file_size: int


class TelegramAudio(TypedDict, total=False):
    """Telegram Audio object fields."""
    file_id: str
    file_unique_id: str
    duration: int
    performer: str
    title: str
    file_size: int


class TelegramSticker(TypedDict, total=False):
    """Telegram Sticker object fields."""
    file_id: str
    file_unique_id: str
    width: int
    height: int


class TelegramMessageDict(TypedDict, total=False):
    """Telegram Message object fields (raw JSON from API)."""
    message_id: int
    chat: TelegramChat
    date: int
    text: str
    photo: list[TelegramPhotoSize]
    document: TelegramDocument
    voice: TelegramVoice
    video: TelegramVideo
    animation: TelegramDocument
    audio: TelegramAudio
    sticker: TelegramSticker
    reply_to_message: dict[str, Any]
    caption: str
    media_group_id: str


class TelegramCallbackQuery(TypedDict, total=False):
    """Telegram CallbackQuery object fields."""
    id: str
    from_user: TelegramUser
    message: TelegramMessageDict
    data: str


class TelegramUpdate(TypedDict, total=False):
    """Telegram Update object fields (raw webhook payload)."""
    update_id: int
    message: TelegramMessageDict
    edited_message: TelegramMessageDict
    callback_query: TelegramCallbackQuery


class RegistryData(TypedDict):
    """Worker registry JSON shape."""
    version: int
    workers: dict[str, dict[str, Any]]


class WorkerEndpointInfo(TypedDict, total=False):
    """Worker info returned by /workers endpoint."""
    name: str
    backend: str
    status: str
    host: str
    tmux: str
    protocol: str
    send_example: str


# ── Probe / token / state TypedDicts ────────────────────────────────

class DiskUsageDict(TypedDict, total=False):
    """Disk usage probe result from _check_disk_usage."""
    pct: int
    free_gb: float
    total_gb: float
    ts: float  # added when stored in HostHealthState


class MemUsageDict(TypedDict, total=False):
    """Memory usage probe result from _check_mem_usage."""
    pct: int
    used_gb: float
    total_gb: float
    avail_gb: float
    top_procs: list[str]
    ts: float


class IoUsageDict(TypedDict, total=False):
    """IO probe result from _check_io_usage."""
    iowait_pct: float
    read_iops: int
    write_iops: int
    util_pct: float
    ts: float


class RewindTokenEntry(TypedDict):
    """Shape of entries in REWIND_TOKENS."""
    name: str
    expires_at: float


class PrReviewTokenEntry(TypedDict):
    """Shape of entries in PR_REVIEW_TOKENS."""
    pr_num: int
    owner: str
    repo: str
    expires_at: float


class ProcStatsEntry(TypedDict):
    """Per-process stats from ps (used by _ps_stats)."""
    cpu: float
    state: str


class QuestionOption(TypedDict):
    """A single option in an interactive prompt."""
    num: int
    label: str
    selected: bool


class QuestionDetails(TypedDict):
    """Interactive question details extracted from tmux pane output."""
    header: str
    options: list[QuestionOption]
    selected_num: int


class MediaGroupEntry(TypedDict):
    """Buffered media group state during collection."""
    items: list[TelegramMessageDict]
    caption: str
    timer: threading.Timer | None


class ReminderState(TypedDict):
    """Per-worker learning-reminder state."""
    response_count: int
    last_reminder_ts: float
    last_response_ts: float
    reminder_pending: bool


class HealthSummaryDict(TypedDict, total=False):
    """Per-host health summary returned by HostHealthState.to_health_summary."""
    ssh_down: bool
    ssh_down_since: float | None
    disk: DiskUsageDict | None
    mem: MemUsageDict | None
    io: IoUsageDict | None
    cpu_hogs: list[dict[str, str | float]]
    worktrees: dict[str, float] | None


# ── Guest / Channel / Relay TypedDicts ──────────────────────────────────

class GuestSessionDict(TypedDict):
    """Shape of a guest session stored in GuestStore.guests."""
    name: str
    token_hash: str
    created_at: str
    expires_at: str
    expires_at_unix: float
    notified_workers: set[str]


class ChannelMemberDict(TypedDict, total=False):
    """Shape of a member entry inside a channel's members dict."""
    type: str         # "worker", "guest", or "manager"
    name: str         # omitted for manager members


# Note: channel messages and relay messages use the JSON key "from" (a Python keyword).
# TypedDict functional syntax allows reserved-word keys.

ChannelMessageDict = TypedDict("ChannelMessageDict", {
    "id": str,
    "seq": int,
    "from": str,
    "text": str,
    "ts": int,
})
"""Shape of a message inside a channel's messages list."""


class ChannelDict(TypedDict):
    """Shape of a channel stored in ChannelStore.channels."""
    id: str
    label: str
    created_at: str
    expires_at_unix: float
    seq: int
    created_by: str
    members: dict[str, ChannelMemberDict]
    messages: list[ChannelMessageDict]


RelayMessageDict = TypedDict("RelayMessageDict", {
    "message_id": str,
    "direction": str,      # "guest_to_worker" or "worker_to_guest"
    "from": str,
    "to": str,
    "text": str,
    "ts": str,
})
"""Shape of a message in a relay channel."""


class RelayChannelDict(TypedDict):
    """Shape of a relay channel stored in RelayStore.channels."""
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


class TmuxSessionDict(TypedDict, total=False):
    """Shape of a scanned tmux session entry."""
    tmux: str
    backend: str
    host: str           # optional — present only for remote sessions


class RegistryWorkerDict(TypedDict, total=False):
    """Raw dict shape of a single worker entry in workers.json.

    The dataclass WorkerRegistryEntry (below) is the rich model equivalent.
    """
    backend: str
    chat_id: int | None
    hire_time: int
    host: str           # optional
    home_host: str      # optional — preserved across re-registrations
    home_cwd: str       # optional — preserved across re-registrations
    protocol: str       # optional — "http" for callback workers
    callback_url: str   # optional — for callback workers
    version: str        # optional — for callback workers
    tools: dict[str, Any]  # optional — for callback workers


class RegistryFileDict(TypedDict, total=False):
    """Raw dict shape of the top-level workers.json file."""
    version: int
    workers: dict[str, RegistryWorkerDict]


# ── NamedTuple models for structured returns ──────────────────────────

class ParsedWorkerTarget(NamedTuple):
    """Result of parsing 'name@host' or 'name' worker target."""
    name: str
    host: str | None


class FileValidation(NamedTuple):
    """Result of validating a file path (photo or document)."""
    ok: bool
    detail: Path | str  # Path on success, error message on failure


class TmuxActivityResult(NamedTuple):
    """Result of reading tmux pane activity."""
    activity: str
    context_pct: str | None
    raw_lines: list[str] | None


class AuthorDetection(NamedTuple):
    """Result of detecting message author from text prefix."""
    author: str
    avatar_html: str
    display_text: str


class RouteResolution(NamedTuple):
    """Result of resolving an HTTP route to its handler."""
    handler: Callable[..., None] | None
    match: re.Match[str] | None


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


# ── File map ───────────────────────────────────────────────────────────
#
#   L1-380      Imports, type aliases, 33 TypedDict/5 NamedTuple/1 type alias models
#   L~436       Configuration: ReuseAddrServer, dataclasses (WatchdogConfig,
#               ResourceAlertConfig, MediaConfig), AppContext
#   L~820       Guest/channel/relay subsystem (GuestSession, RelayStore)
#   L~1570      Backend Protocol + registry (Backend, SubprocessRunner, Clock, get_backend)
#   L~1838      OS detection, worker health: _detect_os_family, _machine_health,
#               normalize_cwd, validate_cwd, parse_hire_args
#   L~2396      Tmux interaction: tmux_exists, tmux_send_message,
#               _read_tmux_activity, _wait_for_restart_ready
#   L~3904      Transport + Telegram API: MessageTransport, TelegramAPI,
#               send_message, send_photo, send_document, split_message
#   L~5100      Text/media processing: parse_image_tags, escape_html,
#               _TelegramHTMLSanitizer, markdown_to_telegram_html
#   L~8461      WorkerManager class (~820 lines): hire, fire, restart, status
#               (DI: accepts SubprocessRunner/Clock for testability)
#   L~10076     TeleportCommandsMixin (~1064 lines)
#   L~12155     CommandRouter class (~1239 lines)
#   L~14009     Transcript HTML rendering
#   L~15247     EndpointRouter, Handler class + endpoint mixins (~2500 lines)
#   L~17947     main() function, signal handlers, startup
#
#   DI coverage (v0.42.0): 68 subprocess + 136 time calls → injectable seams
#   Only _RealSubprocessRunner/_RealClock and 1 module-init call use direct stdlib
#   Structured logging: all diagnostics via _log(LEVEL, component, msg)
#
# ======================================================================
# CONFIGURATION
# ======================================================================

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
            text=True, timeout=3).stdout.strip()  # noqa: direct call — runs before constants init
        if _ts_ip:
            BRIDGE_PUBLIC_URL = f"http://{_ts_ip}:{PORT}"
    except (subprocess.SubprocessError, OSError) as exc:
        _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
if BRIDGE_PUBLIC_URL and not os.environ.get("BRIDGE_BIND"):
    BRIDGE_BIND = "0.0.0.0"
# BRIDGE_SSH_TARGET: ssh alias that remote machines use to reach the bridge host.
# Used by /workers?from= when a remote caller needs to address a bridge-local peer.
# Default "vps" matches team convention; override per deployment if needed.
BRIDGE_SSH_TARGET = os.environ.get("BRIDGE_SSH_TARGET", "vps")
MACHINES_CONFIG_FILE = Path(os.environ.get(
    "MACHINES_CONFIG_FILE",
    Path.home() / ".config" / "claudecode-telegram" / "machines.json"
))
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
    "GET /machines": "List configured machines, access hints, workers, and health",
    "GET /workers": "List active workers with send commands",
    "GET /checkin?name=<name>": "Refresh worker instructions (optional: &cwd=/path)",
    "GET /health/workers": "Watchdog state for all workers",
    "GET /transcript/<name>": "Polished HTML transcript viewer for a worker",
    "GET /transcript/<name>/updates": "Poll for new transcript entries (returns {total, new})",
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

# ── Timeout constants (seconds) ──────────────────────────────────────
# Subprocess timeouts — grouped by operation weight
TIMEOUT_TMUX_CHECK = 3       # has-session, capture-pane, tmux list-sessions
TIMEOUT_TMUX_SEND = 5        # send-keys, short tmux / remote commands
TIMEOUT_REMOTE_CMD = 10      # standard ssh remote commands (git status, cat, etc.)
TIMEOUT_FILE_TRANSFER = 15   # scp, small file transfers
TIMEOUT_GIT_OP = 30          # git push/pull/clone, medium remote operations
TIMEOUT_LARGE_TRANSFER = 60  # rsync medium dirs, large uploads
TIMEOUT_RSYNC = 120          # rsync full working directories
TIMEOUT_FULL_SYNC = 600      # full working-directory git stash/apply

# HTTP timeouts
TIMEOUT_HTTP_API = 10        # Telegram Bot API calls (sendMessage, getUpdates)
TIMEOUT_HTTP_DOWNLOAD = 30   # file downloads from Telegram servers
TIMEOUT_HTTP_UPLOAD = 60     # media uploads (photos, documents, video)

# Process lifecycle
TIMEOUT_PROCESS_WAIT = 3     # proc.wait() for adapter/child cleanup
TIMEOUT_THREAD_JOIN = 1.0    # thread.join() for pipe readers

# Sleep/delay constants
DELAY_TMUX_SEND = 0.3        # between tmux send-keys calls (avoid interleaving)
DELAY_PIPE_POLL = 0.5        # pipe reader poll interval
DELAY_STARTUP = 1.0          # waiting for process startup confirmation
DELAY_STARTUP_LONG = 1.5     # longer startup wait (backend initialization)
DELAY_RETRY = 0.5            # between retry attempts
DELAY_BRIEF = 0.05           # minimal delay (lock-file poll, tmux propagation)
DELAY_SHORT = 0.2            # short pause (UI updates, pipe flush)
DELAY_RESPONSE_GAP = 2       # gap to avoid colliding with concurrent responses
DELAY_PROCESS_SETTLE = 3     # let a newly-started process settle (tmux, claude)
DELAY_CLAUDE_LOAD = 4        # wait for Claude Code to finish loading after start

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


# ── Config dataclasses (frozen, immutable defaults) ──────────────────

@dataclass(frozen=True)
class WatchdogConfig:
    """Watchdog timing and threshold configuration (immutable)."""
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
    """Resource monitoring thresholds and cooldowns (immutable)."""
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
    """Media handling limits and extension sets (immutable)."""
    max_file_size: int = 50 * 1024 * 1024
    photo_max_sum: int = 10000
    photo_max_dim: int = 5000


_wd_cfg = WatchdogConfig()
WATCHDOG_INTERVAL = _wd_cfg.interval
START_GRACE = _wd_cfg.start_grace
THINK_GRACE = _wd_cfg.think_grace
TOOL_GAP_GRACE = _wd_cfg.tool_gap_grace
STALE_PENDING = _wd_cfg.stale_pending
CPU_ACTIVE = _wd_cfg.cpu_active
CPU_IDLE = _wd_cfg.cpu_idle
IDLE_STREAK_STUCK = _wd_cfg.idle_streak_stuck
ALERT_COOLDOWN = _wd_cfg.alert_cooldown


# ── AppContext: injectable configuration ──

@dataclass
class AppContext:
    """Injectable application configuration — replaces scattered module globals."""
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
    sandbox_enabled: bool = False
    sandbox_image: str = ""
    team_dir: str = ""
    watchdog_interval: int = 4
    webhook_secret: str = ""
    transport_mode: str = "telegram"

    def __post_init__(self) -> None:
        """Validate and normalize fields after dataclass initialization."""
        if self.sessions_dir is None:
            self.sessions_dir = Path.home() / ".claude" / "telegram" / "sessions"
        if self.claude_dir is None:
            self.claude_dir = Path.home() / ".claude"
        if not self.bridge_url:
            self.bridge_url = f"http://localhost:{self.port}"


def _log_best_effort(label: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call func(*args, **kwargs) and log on failure instead of crashing.

    Use for fire-and-forget operations where failure is acceptable but
    should not be silent (aligns with 'fail loudly' philosophy).
    Returns the function result on success, None on failure.
    """
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        _log(_LOG_DEBUG, label, f"{type(exc).__name__}: {exc}")
        return None


def _build_app_context() -> AppContext:
    """Build AppContext from current module globals (bridge between old and new)."""
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
        sandbox_enabled=SANDBOX_ENABLED,
        sandbox_image=SANDBOX_IMAGE,
        team_dir=TEAM_DIR,
        watchdog_interval=WATCHDOG_INTERVAL,
        webhook_secret=WEBHOOK_SECRET,
        transport_mode=TRANSPORT_MODE if 'TRANSPORT_MODE' in dir() else "telegram",
    )


# Singleton context — built lazily after all globals are initialized
_app_context: AppContext | None = None


def get_app_context() -> AppContext:
    """Get the singleton AppContext. Built on first call from module globals."""
    global _app_context
    if _app_context is None:
        _app_context = _build_app_context()
    return _app_context


# ── IncomingMessage: parse Telegram update once ──

@dataclass
class IncomingMessage:
    """Parsed Telegram update — created once, read everywhere."""
    update_id: int = 0
    chat_id: int | None = None
    msg_id: int | None = None
    text: str = ""
    # Media fields (Telegram API JSON shapes)
    photo: list[dict[str, Any]] | None = None
    document: dict[str, Any] | None = None
    animation: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    video: dict[str, Any] | None = None
    video_note: dict[str, Any] | None = None
    sticker: dict[str, Any] | None = None
    # Derived
    has_media: bool = False
    doc_is_image: bool = False
    media_group_id: str | None = None
    # Reply context (Telegram API reply_to_message JSON)
    reply_to: dict[str, Any] | None = None
    # Raw message dict (for edge cases during migration)
    raw_msg: dict[str, Any] | None = None

    @classmethod
    def from_update(cls: type["IncomingMessage"], update: dict[str, Any]) -> "IncomingMessage":
        """Factory: parse a Telegram update dict into an IncomingMessage."""
        msg = update.get("message", {})
        text = msg.get("text", "") or msg.get("caption", "")
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

        has_media = bool(
            photo or document or animation or video
            or audio or voice or video_note or sticker
        )

        return cls(
            update_id=update.get("update_id", 0),
            chat_id=msg.get("chat", {}).get("id"),
            msg_id=msg.get("message_id"),
            text=text,
            photo=photo,
            document=document,
            animation=animation,
            audio=audio,
            voice=voice,
            video=video,
            video_note=video_note,
            sticker=sticker,
            has_media=has_media,
            doc_is_image=doc_is_image,
            media_group_id=msg.get("media_group_id"),
            reply_to=msg.get("reply_to_message"),
            raw_msg=msg,
        )


# ============================================================
# GUEST SYSTEM: temporary external agent sessions
# ============================================================

# ── Typed dataclasses for conversation state ─────────────────
#
# These provide typed access to the guest, channel, and relay
# structures. The underlying storage is still dict-based for
# JSON serialization compat, but new code should prefer these.

@dataclass
class GuestSession:
    """A temporary external agent session."""
    name: str
    token_hash: str
    created_at: str
    expires_at_unix: float
    notified_workers: set[str]

    @classmethod
    def from_dict(cls: type["GuestSession"], token_hash: str, data: dict[str, Any]) -> "GuestSession":
        """Construct an instance from a plain dictionary."""
        notified = data.get("notified_workers", set())
        if isinstance(notified, list):
            notified = set(notified)
        return cls(
            name=data["name"],
            token_hash=token_hash,
            created_at=data.get("created_at", ""),
            expires_at_unix=data["expires_at_unix"],
            notified_workers=notified,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        return {
            "name": self.name,
            "created_at": self.created_at,
            "expires_at_unix": self.expires_at_unix,
            "notified_workers": self.notified_workers,
        }

    @property
    def is_expired(self) -> bool:
        """Check whether this entry has passed its expiration time."""
        return _clock.time() >= self.expires_at_unix


@dataclass
class GuestInboxMessage:
    """A message in a guest's inbox."""
    id: str
    sender: str
    text: str
    ts: int

    @classmethod
    def from_dict(cls: type["GuestInboxMessage"], data: dict[str, Any]) -> "GuestInboxMessage":
        """Construct an instance from a plain dictionary."""
        return cls(
            id=data.get("id", ""),
            sender=data.get("from", data.get("sender", "")),
            text=data.get("text", ""),
            ts=data.get("ts", 0),
        )


@dataclass
class ChannelMember:
    """A member of a group channel."""
    key: str          # "manager", "worker:name", "guest:name"
    type: str         # "manager", "worker", "guest"
    name: str = ""    # display name (empty for manager)

    @classmethod
    def from_dict(cls: type["ChannelMember"], key: str, data: dict[str, Any]) -> "ChannelMember":
        """Construct an instance from a plain dictionary."""
        return cls(key=key, type=data["type"], name=data.get("name", ""))


@dataclass
class ChannelMessage:
    """A message in a group channel."""
    id: str
    seq: int
    sender: str  # member key
    text: str
    ts: int

    @classmethod
    def from_dict(cls: type["ChannelMessage"], data: dict[str, Any]) -> "ChannelMessage":
        """Construct an instance from a plain dictionary."""
        return cls(
            id=data["id"],
            seq=data["seq"],
            sender=data["from"],
            text=data["text"],
            ts=data["ts"],
        )


@dataclass
class RelayMessage:
    """A message in a relay channel."""
    id: str
    sender: str       # "guest" or "worker"
    text: str
    ts: int
    sender_name: str = ""

    @classmethod
    def from_dict(cls: type["RelayMessage"], d: dict[str, Any]) -> "RelayMessage":
        """Construct an instance from a plain dictionary."""
        return cls(
            id=d.get("id", ""),
            sender=d.get("from", d.get("sender", "")),
            text=d.get("text", ""),
            ts=d.get("ts", 0),
            sender_name=d.get("sender_name", ""),
        )


class GuestStore:
    """Thread-safe store for guest sessions and inboxes."""

    def __init__(self) -> None:
        """Initialize guest sessions store with thread-safe locks."""
        self.guests: dict[str, GuestSessionDict] = {}
        self.inboxes: dict[str, list[dict[str, Any]]] = {}
        self.lock: threading.Lock = threading.Lock()


guest_store = GuestStore()
GUEST_TTL = 86400           # 24 hours
GUEST_INBOX_CAP = 200


def _guest_state_path() -> Path:
    """Path to persisted guest state file."""
    return NODE_DIR / "guest_state.json"


def _guest_save() -> None:
    """Persist guest state to disk. Call with guest_store.lock held."""
    try:
        path = _guest_state_path()
        now = _clock.time()
        active_guests = {}
        for k, v in guest_store.guests.items():
            if now <= v.get("expires_at_unix", 0):
                guest = dict(v)
                # Convert set to list for JSON
                if isinstance(guest.get("notified_workers"), set):
                    guest["notified_workers"] = list(guest["notified_workers"])
                active_guests[k] = guest
        active_names = {guest["name"] for guest in active_guests.values()}
        active_inboxes = {k: v for k, v in guest_store.inboxes.items() if k in active_names}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"guests": active_guests, "inboxes": active_inboxes}, f, indent=2)
        tmp.rename(path)
        os.chmod(path, 0o600)
    except OSError as e:
        _log(_LOG_WARN, "guest", f"Failed to save state: {e}")


def _guest_load() -> None:
    """Load guest state from disk on startup."""
    path = _guest_state_path()
    if not path.exists():
        return
    try:
        with open(path) as f:
            data = json.load(f)
        now = _clock.time()
        restored = 0
        for k, v in data.get("guests", {}).items():
            if now <= v.get("expires_at_unix", 0):
                # Convert notified_workers list back to set
                if isinstance(v.get("notified_workers"), list):
                    v["notified_workers"] = set(v["notified_workers"])
                guest_store.guests[k] = v
                restored += 1
        guest_store.inboxes.update(data.get("inboxes", {}))
        if restored:
            _log(_LOG_INFO, "guest", f"Restored {restored} active guest(s) from disk")
    except (json.JSONDecodeError, OSError, KeyError) as e:
        _log(_LOG_WARN, "guest", f"Failed to load state: {e}")


def guest_create_token() -> tuple[str, str]:
    """Create a guest token and its hash. Returns (token, token_hash)."""
    token = f"gt_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


def guest_generate_name(existing_names: set[str] | None = None) -> str:
    """Generate a short random guest name (3-6 chars), unique vs existing."""
    if existing_names is None:
        existing_names = set()
    for _ in range(100):
        name = secrets.token_urlsafe(3).rstrip("=").lower()[:5]
        if len(name) >= 3 and name not in existing_names:
            return name
    return secrets.token_urlsafe(4).rstrip("=").lower()[:6]


def guest_validate_name(name: str, team_workers: set[str], existing_guests: set[str]) -> tuple[bool, str]:
    """Validate a guest name. Returns (ok, error_message)."""
    if not name or not name.strip():
        return False, "name is required"
    name = name.strip().lower()
    if len(name) > 20:
        return False, "name too long (max 20 chars)"
    if name in team_workers:
        return False, f"name '{name}' conflicts with a team worker"
    if name in existing_guests:
        return False, f"name '{name}' already taken by another guest"
    return True, ""


def guest_is_expired(expires_at_unix: float) -> bool:
    """Check if a guest session has expired."""
    return _clock.time() >= expires_at_unix


def guest_inbox_filter(messages: list[dict[str, Any]], after: str | None = None) -> list[dict[str, Any]]:
    """Filter messages, returning only those after the given message ID."""
    if not after:
        return list(messages)
    found = False
    result = []
    for m in messages:
        if found:
            result.append(m)
        elif m.get("id") == after:
            found = True
    if not found:
        return list(messages)
    return result


def guest_inbox_append(inbox: list[dict[str, Any]], msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Append a message to inbox, capping at GUEST_INBOX_CAP."""
    inbox.append(msg)
    if len(inbox) > GUEST_INBOX_CAP:
        inbox = inbox[-GUEST_INBOX_CAP:]
    return inbox


# ============================================================
# GROUP CHANNELS: data + pure functions
# ============================================================

class ChannelStore:
    """Thread-safe store for group channels."""

    def __init__(self) -> None:
        """Initialize channel store with thread-safe locks."""
        self.channels: dict[str, ChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()


channel_store = ChannelStore()
CHANNEL_TTL = 86400           # 24 hours
CHANNEL_MSG_CAP = 200


def _channel_state_path() -> Path:
    """Path to persisted channel state file."""
    return NODE_DIR / "channel_state.json"


def _channel_save() -> None:
    """Persist channel state to disk. Call with channel_store.lock held."""
    try:
        path = _channel_state_path()
        active = {cid: ch for cid, ch in channel_store.channels.items()
                  if not channel_is_expired(ch)}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(active, f, indent=2)
        tmp.rename(path)
        os.chmod(path, 0o600)
    except OSError as e:
        _log(_LOG_WARN, "channel", f"Failed to save state: {e}")


def _channel_load() -> None:
    """Load channel state from disk on startup."""
    path = _channel_state_path()
    if not path.exists():
        return
    try:
        with open(path) as f:
            data = json.load(f)
        restored = 0
        for cid, ch in data.items():
            if not channel_is_expired(ch):
                channel_store.channels[cid] = ch
                restored += 1
        if restored:
            _log(_LOG_INFO, "channel", f"Restored {restored} active channel(s) from disk")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_WARN, "channel", f"Failed to load state: {e}")


def channel_create_id(label: str = "") -> str:
    """Generate a stable channel ID: ch_<6-char random>."""
    short = secrets.token_urlsafe(4).rstrip("=").lower()[:6]
    return f"ch_{short}"


def channel_new(channel_id: str, label: str, created_by: str,
                members: list[str], ttl: int = CHANNEL_TTL) -> ChannelDict:
    """Create a channel dict. Members are typed strings like 'worker:geni', 'guest:alice', 'manager'."""
    now = _clock.time()
    member_dict = {}
    for m in members:
        if m == "manager":
            member_dict["manager"] = {"type": "manager"}
        elif m.startswith("worker:"):
            name = m.split(":", 1)[1]
            member_dict[m] = {"type": "worker", "name": name}
        elif m.startswith("guest:"):
            name = m.split(":", 1)[1]
            member_dict[m] = {"type": "guest", "name": name}
    return {
        "id": channel_id,
        "label": label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "expires_at_unix": now + ttl,
        "seq": 0,
        "created_by": created_by,
        "members": member_dict,
        "messages": [],
    }


def channel_add_members(channel: ChannelDict, members: list[str]) -> list[str]:
    """Add members to channel. Returns list of actually added members."""
    added = []
    for m in members:
        if m in channel["members"]:
            continue
        if m == "manager":
            channel["members"]["manager"] = {"type": "manager"}
        elif m.startswith("worker:"):
            channel["members"][m] = {"type": "worker", "name": m.split(":", 1)[1]}
        elif m.startswith("guest:"):
            channel["members"][m] = {"type": "guest", "name": m.split(":", 1)[1]}
        else:
            continue
        added.append(m)
    return added


def channel_remove_members(channel: ChannelDict, members: list[str]) -> list[str]:
    """Remove members from channel. Returns list of actually removed members."""
    removed = []
    for m in members:
        if m in channel["members"]:
            del channel["members"][m]
            removed.append(m)
    return removed


def channel_append_message(channel: ChannelDict, from_member: str, text: str) -> ChannelMessageDict:
    """Append a message to the channel. Returns the message dict."""
    channel["seq"] += 1
    msg = {
        "id": f"cm_{channel['seq']:06d}",
        "seq": channel["seq"],
        "from": from_member,
        "text": text,
        "ts": int(_clock.time()),
    }
    channel["messages"].append(msg)
    if len(channel["messages"]) > CHANNEL_MSG_CAP:
        channel["messages"] = channel["messages"][-CHANNEL_MSG_CAP:]
    return msg


def channel_get_messages(channel: ChannelDict, after: str | None = None) -> tuple[list[ChannelMessageDict], bool]:
    """Get channel messages, optionally after a given message ID. Returns (messages, truncated)."""
    if not after:
        return list(channel["messages"]), False
    found_idx = -1
    for i, m in enumerate(channel["messages"]):
        if m["id"] == after:
            found_idx = i
            break
    if found_idx == -1:
        return list(channel["messages"]), True
    return channel["messages"][found_idx + 1:], False


def channel_is_expired(channel: ChannelDict) -> bool:
    """Check if a channel has expired."""
    return _clock.time() > channel["expires_at_unix"]


def channel_get_member_names(channel: ChannelDict, member_type: str) -> list[str]:
    """Get names of members of a specific type (worker, guest, manager)."""
    return [info["name"] for info in channel["members"].values()
            if info["type"] == member_type and "name" in info]


# ============================================================
# RELAY GUIDELINE LINK
# ============================================================

class RelayStore:
    """Thread-safe store for relay channels."""

    def __init__(self) -> None:
        """Initialize relay channel store with thread-safe locks."""
        self.channels: dict[str, RelayChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()


relay_store = RelayStore()

RELAY_PUBLIC_HOST = os.environ.get("RELAY_PUBLIC_HOST", "157.180.48.254")


def _relay_state_path() -> Path:
    """Path to persisted relay state file."""
    return NODE_DIR / "relay_state.json"


def _relay_save() -> None:
    """Persist relay channels to disk. Call with relay_store.lock held."""
    try:
        path = _relay_state_path()
        # Only save non-expired channels
        now = _clock.time()
        active = {cid: ch for cid, ch in relay_store.channels.items()
                  if now <= ch["expires_at_unix"]}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(active, f, indent=2)
        tmp.rename(path)
        os.chmod(path, 0o600)
    except OSError as e:
        _log(_LOG_WARN, "relay", f"Failed to save state: {e}")


def _relay_load() -> None:
    """Load relay channels from disk on startup."""
    path = _relay_state_path()
    if not path.exists():
        return
    try:
        with open(path) as f:
            data = json.load(f)
        now = _clock.time()
        restored = 0
        for cid, ch in data.items():
            if now <= ch.get("expires_at_unix", 0):
                relay_store.channels[cid] = ch
                restored += 1
        if restored:
            _log(_LOG_INFO, "relay", f"Restored {restored} active relay(s) from disk")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_WARN, "relay", f"Failed to load state: {e}")


def relay_channel_create(worker: str, label: str, ttl: int = 86400) -> tuple[RelayChannelDict, str, str]:
    """Create a relay channel with guest and reply tokens. Returns (channel, guest_token, reply_token)."""
    channel_id = channel_create_id(label)
    guest_token = f"gt_{secrets.token_urlsafe(32)}"
    reply_token = f"rt_{secrets.token_urlsafe(32)}"
    now = _clock.time()
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
        "messages": [],
    }
    return channel, guest_token, reply_token


def _relay_base_url() -> str:
    """Base URL for relay endpoints. Prefers BRIDGE_PUBLIC_URL (Tailscale/tunnel), falls back to RELAY_PUBLIC_HOST."""
    if BRIDGE_PUBLIC_URL:
        return BRIDGE_PUBLIC_URL
    return f"http://{RELAY_PUBLIC_HOST}:{PORT}"


def relay_guide_url(channel_id: str, guest_token: str) -> str:
    """Generate the guideline link URL for a relay channel."""
    return f"{_relay_base_url()}/v1/{channel_id}?token={guest_token}"


def relay_guide_text(channel: dict[str, Any], guest_token: str) -> str:
    """Generate markdown guide for a relay channel."""
    base = f"{_relay_base_url()}/v1/{channel['id']}"
    worker_name = channel["worker"]
    return f"""# Chat Channel to {worker_name}

Direct chat channel to **{worker_name}** (a Claude Code agent).

## Setup

```bash
export RELAY_TOKEN="{guest_token}"
export RELAY="{base}"
```

## Send a message

```bash
curl -fsS $RELAY/send -H "Authorization: Bearer $RELAY_TOKEN" -H "Content-Type: application/json" -d '{{"text":"your message here"}}'
```

## Poll for replies

```bash
curl -fsS $RELAY/messages -H "Authorization: Bearer $RELAY_TOKEN"
```

## How to use

1. Send a message using the curl command above
2. Poll `/messages` to see replies (add `?after=<message_id>` for new messages only)
3. The channel expires at `{channel['expires_at']}`
"""


def relay_auth_guest(channel_id: str, token: str) -> RelayChannelDict | None:
    """Authenticate a guest token for a relay channel. Returns channel or None."""
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return None
    if _clock.time() > channel.get("expires_at_unix", 0):
        return None
    if hashlib.sha256(token.encode()).hexdigest() != channel.get("guest_token_hash"):
        return None
    return channel


def relay_auth_reply(channel_id: str, token: str) -> RelayChannelDict | None:
    """Authenticate a reply token for a relay channel. Returns channel or None."""
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return None
    if _clock.time() > channel.get("expires_at_unix", 0):
        return None
    if hashlib.sha256(token.encode()).hexdigest() != channel.get("reply_token_hash"):
        return None
    return channel


def relay_guest_send(channel_id: str, text: str) -> tuple[str | None, RelayMessageDict | None]:
    """Guest sends a message to the worker. Returns (envelope_text, message_dict)."""
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return None, None

    msg_id = f"msg_{secrets.token_urlsafe(4)}"
    base = f"{_relay_base_url()}/v1/{channel_id}"
    reply_token = channel["reply_token"]

    envelope = (
        f"[RELAY from {channel['label']}]\n"
        f"channel: {channel_id}\n"
        f"message_id: {msg_id}\n"
        f"reply: curl -fsS {base}/reply "
        f"-H 'Authorization: Bearer {reply_token}' "
        f"-H 'Content-Type: application/json' "
        f"-d '{{\"text\":\"YOUR_REPLY\"}}'\n"
        f"\n"
        f"{text}\n"
        f"[/RELAY]"
    )

    msg = {
        "message_id": msg_id,
        "direction": "guest_to_worker",
        "from": channel["label"],
        "to": channel["worker"],
        "text": text,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time())),
    }
    with relay_store.lock:
        channel["messages"].append(msg)
        _relay_save()

    return envelope, msg


def relay_worker_reply(channel_id: str, text: str) -> RelayMessageDict | None:
    """Worker replies to the guest. Returns message dict."""
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return None

    msg_id = f"msg_{secrets.token_urlsafe(4)}"
    msg = {
        "message_id": msg_id,
        "direction": "worker_to_guest",
        "from": channel["worker"],
        "to": channel["label"],
        "text": text,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time())),
    }
    with relay_store.lock:
        channel["messages"].append(msg)
        _relay_save()
    return msg


def relay_get_messages(channel_id: str, after: str | None = None) -> list[dict[str, Any]]:
    """Get messages for a relay channel, optionally after a message ID."""
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return []
    msgs = channel["messages"]
    if not after:
        return list(msgs)
    for i, m in enumerate(msgs):
        if m["message_id"] == after:
            return list(msgs[i + 1:])
    return list(msgs)


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
    tools: dict[str, Any] | None = None
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

    def to_session_dict(self) -> dict[str, Any]:
        """Convert back to legacy session dict for backward compatibility."""
        result: dict[str, Any] = {"backend": self.backend}
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
    def from_session_dict(cls: type["WorkerRecord"], name: str, session: dict[str, Any], tmux_prefix: str = "") -> "WorkerRecord":
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


@dataclass
class WorkerRegistryEntry:
    """Typed representation of a single entry in the worker registry JSON.

    Maps to the shape ``{"backend": ..., "protocol": ..., "callback_url": ..., ...}``
    stored under ``registry["workers"][name]``.
    """
    backend: str = "claude"
    protocol: str = ""
    callback_url: str = ""
    host: str | None = None
    version: str = ""
    chat_id: int | None = None
    hire_time: int = 0
    tools: dict[str, Any] | None = None
    home_host: str = ""
    home_cwd: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        result: dict[str, Any] = {"backend": self.backend}
        for field_name in ("protocol", "callback_url", "host", "version", "chat_id",
                           "hire_time", "tools", "home_host", "home_cwd"):
            value = getattr(self, field_name)
            if value is not None and value != "" and value != 0:
                result[field_name] = value
        return result

    @classmethod
    def from_dict(cls: type["WorkerRegistryEntry"], data: dict[str, Any]) -> "WorkerRegistryEntry":
        """Construct an instance from a plain dictionary."""
        return cls(
            backend=data.get("backend", "claude"),
            protocol=data.get("protocol", ""),
            callback_url=data.get("callback_url", ""),
            host=data.get("host"),
            version=data.get("version", ""),
            chat_id=data.get("chat_id"),
            hire_time=data.get("hire_time", 0),
            tools=data.get("tools"),
            home_host=data.get("home_host", ""),
            home_cwd=data.get("home_cwd", ""),
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


# ── Injectable testing seams ──

class SubprocessRunner(Protocol):
    """Abstraction over subprocess.run and subprocess.Popen for test injection."""

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Execute a subprocess command and wait for completion."""
        ...

    def popen(self, args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        """Spawn a subprocess without waiting for completion."""
        ...


class Clock(Protocol):
    """Abstraction over time for deterministic testing."""

    def time(self) -> float:
        """Return the current time in seconds since epoch."""
        ...

    def sleep(self, seconds: float) -> None:
        """Sleep for the given number of seconds."""
        ...


class _RealSubprocessRunner:
    """Production subprocess runner — delegates to subprocess.run/Popen."""

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Execute a subprocess command via the real subprocess module."""
        return subprocess.run(args, **kwargs)

    def popen(self, args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        """Spawn a subprocess without waiting, via the real subprocess module."""
        return subprocess.Popen(args, **kwargs)


class _RealClock:
    """Production clock — delegates to the time module."""

    def time(self) -> float:
        """Return current wall-clock time."""
        return time.time()

    def sleep(self, seconds: float) -> None:
        """Sleep for real wall-clock duration."""
        time.sleep(seconds)


# Module-level defaults (overridable in tests by replacing these singletons)
_subprocess_runner: SubprocessRunner = _RealSubprocessRunner()
_clock: Clock = _RealClock()
_urlopen: Callable[..., Any] = urllib.request.urlopen  # Injectable for testing


# ─────────────────────────────────────────────────────────────────────────────
# Structured logging
# ─────────────────────────────────────────────────────────────────────────────

# Severity levels for structured log output.
_LOG_ERROR: str = "ERROR"
_LOG_WARN: str = "WARN"
_LOG_INFO: str = "INFO"
_LOG_DEBUG: str = "DEBUG"


def _log(level: str, component: str, msg: str, *,
         exc: BaseException | None = None) -> None:
    """Emit a structured log line to stderr.

    Format: [LEVEL:component] message
    Optionally appends a traceback if exc is provided.
    All bridge error/warning output should route through this function.
    """
    print(f"[{level}:{component}] {msg}", file=sys.stderr, flush=True)
    if exc is not None:
        import traceback as _tb
        _tb.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# SSH Teleport helpers (remote worker support)
# ─────────────────────────────────────────────────────────────────────────────

class RemoteCache:
    """Caches for remote host operations (tools, machines, home dirs)."""

    def __init__(self) -> None:
        """Initialize caches for SSH host resolution and machine config."""
        self.tools: dict[str, str] = {}     # host:tool -> absolute path
        self.machines: dict[str, "Machine"] | None = None
        self.machines_path: Path | None = None
        self.home_dirs: dict[str, str] = {}  # host -> remote $HOME path


remote_cache = RemoteCache()


def _resolve_remote_tool(tool: str, host: str) -> str:
    """Discover absolute path of a tool on a remote host. Cached per host."""
    key = f"{host}:{tool}"
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
            remote_cache.tools[key] = found
            _log(_LOG_INFO, "bridge", f"discovered {tool} on {host}: {found}")
            return found
    except (subprocess.SubprocessError, OSError) as exc:
        _log(_LOG_DEBUG, "probe:_resolve_remote_tool", f"{type(exc).__name__}: {exc}")
    return tool  # fallback to bare name


def _remote_run(cmd: list[str], host: str | None = None, **kwargs: Any) -> subprocess.CompletedProcess:
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
        timeout_val = kwargs.get("timeout", 10)
        cmd = ["ssh", "-o", f"ConnectTimeout={min(timeout_val, 5)}", host, remote_cmd]
    # Default timeout: prevent unbounded subprocess hangs that block bridge threads.
    # Hot-path callers should pass explicit shorter timeouts (3s probes, 5s sends).
    kwargs.setdefault("timeout", 10)
    return _subprocess_runner.run(cmd, **kwargs)


def _remote_copy(src: str, dst: str, host: str | None = None, direction: str = "push") -> None:
    """Copy a file, optionally to/from a remote host via scp.

    direction='push': local src -> remote dst
    direction='pull': remote src -> local dst
    host=None: local copy via shutil.copy2
    """
    if not host:
        shutil.copy2(src, dst)
    elif direction == "push":
        _subprocess_runner.run(["scp", "-q", src, f"{host}:{dst}"], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
    else:  # pull
        _subprocess_runner.run(["scp", "-q", f"{host}:{src}", dst], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)


def _extract_msg_text(msg: TelegramMessageDict) -> str:
    """Extract plain text from a Telegram message, including rich_message blocks."""
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        rich = msg.get("rich_message")
        if rich and isinstance(rich.get("blocks"), list):
            parts = []
            for b in rich["blocks"]:
                bt = b.get("text")
                if not bt:
                    continue
                if isinstance(bt, str):
                    parts.append(bt)
                elif isinstance(bt, list):
                    parts.append("".join(
                        chunk if isinstance(chunk, str) else chunk.get("text", "")
                        for chunk in bt
                    ))
            text = "\n".join(parts)
    return text


def parse_worker_target(target: str) -> ParsedWorkerTarget:
    """Parse 'name@host' or 'name' into a ParsedWorkerTarget.

    Returns ParsedWorkerTarget(name, None) for local workers,
    ParsedWorkerTarget(name, host) for remote.
    """
    if "@" in target:
        name, host = target.rsplit("@", 1)
        return ParsedWorkerTarget(name, host)
    return ParsedWorkerTarget(target, None)


def get_worker_host(name: str) -> str | None:
    """Get the SSH host for a worker from the persistent registry, or None if local."""
    registry = _load_registry()
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

    def public_dict(self) -> dict[str, Any]:
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


def _coerce_optional_str(value: Any, field: str, machine_id: str) -> str:
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
        data = json.loads(config_path.read_text())
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


def _machine_health(machine: Machine) -> dict[str, Any]:
    """Build a health status dict for a machine (disk, memory, IO, up/down)."""
    host_label = machine.ssh_target or "VPS"
    health = {
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
    return health


def _machine_access(machine: Machine, caller_host: str | None) -> str:
    """Return the access command for reaching a machine ('local' or 'ssh <host>')."""
    target_host = machine.ssh_target
    if caller_host == target_host:
        return "local"
    if target_host is None:
        return f"ssh {BRIDGE_SSH_TARGET}"
    return f"ssh {target_host}"


def get_machines(caller_from: str | None = None) -> dict[str, Any]:
    """Return configured machines plus derived workers, access, and health."""
    machines = get_machine_catalog()
    registered = get_registered_sessions()
    caller_info = registered.get(caller_from, {}) if caller_from else {}
    caller_host = caller_info.get("host") if caller_info else (get_worker_host(caller_from) if caller_from else None)

    rows: dict[str, dict[str, Any]] = {}
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


def _bare_repo_url(bare_repo_path: str, target_host: str | None = None) -> str:
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
        _subprocess_runner.run(
            ["git", "init", "--bare", bare_path],
            capture_output=True, text=True, check=True, timeout=TIMEOUT_REMOTE_CMD)
    return bare_path


def _git_push_state(source_cwd: str, worker_name: str, bare_repo: str,
                    host: str | None = None) -> dict[str, Any] | None:
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
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"rev-parse HEAD failed: {r.stderr[:200]}")
            return None
        orig_sha = r.stdout.strip()

        # Get current branch name (or "HEAD" if detached)
        r = _remote_run(["git", "-C", source_cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        orig_branch = r.stdout.strip() if r.returncode == 0 else "HEAD"

        # Get originally staged files (before we touch the index)
        r = _remote_run(["git", "-C", source_cwd, "diff", "--cached", "--name-only"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        staged_files = [f for f in r.stdout.strip().split("\n") if f] if r.returncode == 0 else []

        # Stage everything (including untracked) temporarily to capture in stash
        _remote_run(["git", "-C", source_cwd, "add", "-A"],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)

        # Create stash commit (non-mutating — working tree untouched)
        r = _remote_run(["git", "-C", source_cwd, "stash", "create"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        stash_sha = r.stdout.strip() if r.returncode == 0 else ""

        # Restore original index: reset, then re-stage originally staged files
        _remote_run(["git", "-C", source_cwd, "reset", "HEAD"],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
        if staged_files:
            _remote_run(["git", "-C", source_cwd, "add", "--"] + staged_files,
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)

        # Determine what to push: stash commit if dirty, HEAD if clean
        push_sha = stash_sha if stash_sha else orig_sha
        ref = f"refs/heads/teleport/{worker_name}"

        # Push to bare repo
        if host:
            # Remote source → push to VPS bare repo via SSH
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force",
                 f"claude@100.125.36.102:{bare_repo}", f"{push_sha}:{ref}"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_LARGE_TRANSFER)
        else:
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force",
                 bare_repo, f"{push_sha}:{ref}"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_LARGE_TRANSFER)
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"push failed: {r.stderr[:200]}")
            return None

        return {
            "orig_sha": orig_sha,
            "orig_branch": orig_branch,
            "staged_files": staged_files,
            "stash_sha": stash_sha or None,
        }
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"push state error: {e}")
        return None


def _git_pull_state(target_cwd: str, worker_name: str, bare_repo_url: str,
                    metadata: dict[str, Any], host: str | None = None) -> bool:
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
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            is_existing = r.returncode == 0
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:_git_pull_state", f"{type(exc).__name__}: {exc}")

        if not is_existing:
            # Fresh clone from bare repo
            r = _remote_run(
                ["git", "clone", "--no-checkout", bare_repo_url, target_cwd],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"clone failed: {r.stderr[:200]}")
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
                host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            # Fetch the teleport branch
            r = _remote_run(
                ["git", "-C", target_cwd, "fetch", "vps", ref],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"fetch failed: {r.stderr[:200]}")
                return False

        # Checkout the original branch at the original commit
        if orig_branch and orig_branch != "HEAD":
            _remote_run(
                ["git", "-C", target_cwd, "checkout", "-B", orig_branch, orig_sha],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        else:
            _remote_run(
                ["git", "-C", target_cwd, "checkout", orig_sha],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)

        # Apply the stash if there were uncommitted changes
        if stash_sha:
            # Fetch the stash commit (it's on the teleport branch)
            # For fresh clones, it's already available. For existing, we fetched it.
            # Use FETCH_HEAD or the ref directly
            fetch_ref = f"vps/{ref}" if is_existing else f"origin/{ref}"

            # Apply stash: the teleport branch tip IS the stash commit
            r = _remote_run(
                ["git", "-C", target_cwd, "stash", "apply", fetch_ref],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0:
                # Fallback: try direct SHA if ref doesn't resolve
                # The stash SHA was pushed as the branch tip
                _remote_run(
                    ["git", "-C", target_cwd, "read-tree", "-u", "--reset", orig_sha],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                r = _remote_run(
                    ["git", "-C", target_cwd, "cherry-pick", "--no-commit", fetch_ref],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)

            # Re-stage originally staged files
            if staged_files:
                # First reset index to HEAD (stash apply may have staged everything)
                _remote_run(["git", "-C", target_cwd, "reset", "HEAD"],
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                _remote_run(["git", "-C", target_cwd, "add", "--"] + staged_files,
                            host=host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)

        return True
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"pull state error: {e}")
        return False


def _is_git_repo(cwd: str, host: str | None = None) -> bool:
    """Check if cwd is inside a git repository."""
    try:
        r = _remote_run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _get_project_name(cwd: str, host: str | None = None) -> str | None:
    """Derive project name from git remote.origin.url.

    Returns short name (e.g., 'omi' from 'https://github.com/BasedHardware/omi.git')
    or None if no origin remote.
    """
    try:
        r = _remote_run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
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
    except (subprocess.SubprocessError, OSError):
        return None


def _registry_update_teleport(name: str, host: str, home_host: str, home_cwd: str) -> None:
    """Update registry with teleport location info."""
    with watchdog.lock:
        data = _load_registry()
        worker = data.get("workers", {}).get(name, {})
        worker["host"] = host
        worker["home_host"] = home_host
        worker["home_cwd"] = home_cwd
        data.setdefault("workers", {})[name] = worker
        _save_registry(data)


def _registry_clear_teleport(name: str) -> None:
    """Clear teleport location info from registry (after teleback)."""
    with watchdog.lock:
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
    fcntl.flock(fd, fcntl.LOCK_EX)
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
                    os.write(fd, text.encode())
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


def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    """Return Claude PID for a pane, or None if not found."""
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pane_pid), "-f", "claude"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
        )
    except (subprocess.SubprocessError, OSError):
        return None

    if result.returncode != 0:
        return None

    output = result.stdout.strip().splitlines()
    if not output:
        return None
    return output[0].strip()


def _child_count(pid: str, host: str | None = None) -> int:
    """Return child process count for pid."""
    if not pid:
        return 0
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pid)],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
        )
    except (subprocess.SubprocessError, OSError):
        return 0

    if result.returncode != 0:
        return 0

    return len([line for line in result.stdout.splitlines() if line.strip()])


def _ps_stats(pids: list[str], host: str | None = None) -> dict[str, ProcStatsEntry]:
    """Return {pid: {'cpu': float, 'state': str}} for given pids."""
    pid_list = [str(pid) for pid in pids if pid]
    if not pid_list:
        return {}

    try:
        result = _remote_run(
            ["ps", "-o", "pid=,%cpu=,state=", "-p", ",".join(pid_list)],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
        )
    except (subprocess.SubprocessError, OSError):
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
    with watchdog.lock:
        watchdog.last_hook_ts[session_name] = _clock.time()


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


class CodexBackend:
    """OpenAI Codex CLI - non-interactive mode."""
    name = "codex"
    binary = "codex"
    is_interactive = False

    def start_cmd(self, resume_id: str = "") -> str:
        """Start cmd."""
        return "echo 'Codex worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        """Send."""
        adapter = Path(__file__).parent / "hooks" / "codex-tmux-adapter.py"
        return _spawn_adapter(adapter, worker_name, text, bridge_url, sessions_dir)

    def is_online(self, tmux_name: str) -> bool:
        """Is online."""
        return tmux_exists(tmux_name)


class GeminiBackend:
    """Google Gemini CLI - non-interactive mode (stub)."""
    name = "gemini"
    binary = "gemini"
    is_interactive = False

    def start_cmd(self, resume_id: str = "") -> str:
        """Start cmd."""
        return "echo 'Gemini worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        """Send."""
        adapter = Path(__file__).parent / "hooks" / "gemini-adapter.py"
        return _spawn_adapter(adapter, worker_name, text, bridge_url, sessions_dir)

    def is_online(self, tmux_name: str) -> bool:
        """Is online."""
        return tmux_exists(tmux_name)


class OpenCodeBackend:
    """OpenCode CLI - non-interactive mode (stub)."""
    name = "opencode"
    binary = "opencode"
    is_interactive = False

    def start_cmd(self, resume_id: str = "") -> str:
        """Start cmd."""
        return "echo 'OpenCode worker ready (non-interactive)'"

    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        """Send."""
        adapter = Path(__file__).parent / "hooks" / "opencode-adapter.py"
        return _spawn_adapter(adapter, worker_name, text, bridge_url, sessions_dir)

    def is_online(self, tmux_name: str) -> bool:
        """Is online."""
        return tmux_exists(tmux_name)


BACKENDS = {
    "claude": ClaudeBackend(),
    "codex": CodexBackend(),
    "gemini": GeminiBackend(),
    "opencode": OpenCodeBackend(),
}

class ProcessRegistry:
    """Tracks background process PIDs, pipe reader threads, and pending locks."""

    def __init__(self) -> None:
        """Initialize adapter process tracking and bridge PID references."""
        self.adapter_pids: dict[str, tuple[subprocess.Popen[Any], IO[str] | None]] = {}
        self.pipe_readers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.pending_locks: dict[str, threading.Lock] = {}
        self.pending_locks_guard: threading.Lock = threading.Lock()


processes = ProcessRegistry()


def _spawn_adapter(adapter_path: Path, worker_name: str, text: str,
                   bridge_url: str, sessions_dir: Path) -> bool:
    """Spawn an adapter process with stderr logged to per-worker file."""
    host = get_worker_host(worker_name)
    if host:
        return _spawn_adapter_remote(adapter_path, worker_name, text, bridge_url, sessions_dir, host)
    if not adapter_path.exists():
        _log(_LOG_ERROR, "adapter", f"Adapter not found: {adapter_path}")
        return False

    # Open per-worker log file for adapter stderr (append mode)
    log_file = sessions_dir / worker_name / "adapter.log"
    try:
        stderr_fh = open(log_file, "a")
    except OSError:
        stderr_fh = None  # Fall back to DEVNULL if dir doesn't exist yet

    proc = _subprocess_runner.popen(
        ["python3", str(adapter_path), worker_name, text, bridge_url, str(sessions_dir)],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh if stderr_fh else subprocess.DEVNULL
    )
    processes.adapter_pids[worker_name] = (proc, stderr_fh)
    return True


def _spawn_adapter_remote(adapter_path: Path, worker_name: str, text: str,
                          bridge_url: str, sessions_dir: Path, host: str) -> bool:
    """Spawn an adapter on a remote host via SSH for teleported workers."""
    remote_home = _get_remote_home(host)
    if not remote_home:
        _log(_LOG_WARN, "bridge", f"Cannot determine remote $HOME for {host}, adapter spawn failed")
        return False

    local_home = os.path.expanduser("~")
    remote_adapter = str(adapter_path)
    if remote_adapter.startswith(local_home):
        remote_adapter = remote_home + remote_adapter[len(local_home):]

    remote_sessions = _remap_sessions_dir(host)
    remote_bridge_url = BRIDGE_PUBLIC_URL or bridge_url

    remote_cmd = (
        f"python3 {shlex.quote(remote_adapter)} "
        f"{shlex.quote(worker_name)} {shlex.quote(text)} "
        f"{shlex.quote(remote_bridge_url)} {shlex.quote(remote_sessions)}"
    )

    log_file = sessions_dir / worker_name / "adapter.log"
    try:
        stderr_fh = open(log_file, "a")
    except OSError:
        stderr_fh = None

    proc = _subprocess_runner.popen(
        ["ssh", "-o", "ConnectTimeout=5", host, remote_cmd],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh if stderr_fh else subprocess.DEVNULL
    )
    processes.adapter_pids[worker_name] = (proc, stderr_fh)
    _log(_LOG_INFO, "adapter", f"Spawned remote adapter for '{worker_name}' on {host}")
    return True


def kill_adapter(name: str) -> None:
    """Kill inflight adapter process for a worker."""
    entry = processes.adapter_pids.pop(name, None)
    if entry is None:
        return
    proc, stderr_fh = entry
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=TIMEOUT_PROCESS_WAIT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=TIMEOUT_THREAD_JOIN)
    if stderr_fh:
        try:
            stderr_fh.close()
        except OSError as exc:
            _log(_LOG_DEBUG, "io:kill_adapter", f"{type(exc).__name__}: {exc}")


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


def _parse_codex_transcript(path: str, host: str | None = None) -> list[dict[str, Any]]:
    """Parse a codex native JSONL file into a list of messages.

    Reads the same format beast hours ParseCodexFile() handles:
    event types: session_meta, turn_context, response_item (message/function_call/function_call_output).
    """
    try:
        if host:
            r = _remote_run(["cat", path], host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0:
                return []
            content = r.stdout
        else:
            content = Path(path).read_text()
    except (subprocess.SubprocessError, OSError):
        return []

    messages = []
    for line in content.strip().split("\n"):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        ev_type = ev.get("type", "")
        payload = ev.get("payload", {})
        ts = ev.get("timestamp", "")

        if ev_type == "response_item":
            item_type = payload.get("type", "")
            if item_type == "message":
                role = payload.get("role", "assistant")
                text_parts = []
                for c in payload.get("content", []):
                    if c.get("type") in ("output_text", "input_text") and c.get("text"):
                        text_parts.append(c["text"])
                text = "\n".join(text_parts).strip()
                if text and role != "developer":
                    messages.append({"role": role, "text": text, "timestamp": ts})
            elif item_type == "function_call":
                name = payload.get("name", "")
                messages.append({"role": "tool_use", "text": f"[{name}]", "timestamp": ts})
            elif item_type == "function_call_output":
                output = payload.get("output", "")
                if output:
                    messages.append({"role": "tool_result", "text": output[:500], "timestamp": ts})
    return messages


def _read_codex_transcript(worker_name: str) -> list[dict[str, Any]]:
    """Read the codex native transcript for a worker."""
    host = get_worker_host(worker_name)
    path = _find_codex_transcript(worker_name, host=host)
    if not path:
        return []
    return _parse_codex_transcript(path, host=host)


def _read_noninteractive_activity(worker_name: str) -> str:
    """Return human-readable activity string for a non-interactive worker."""
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
    return BACKENDS.get(name, BACKENDS[DEFAULT_BACKEND])


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


# ── BridgeRuntimeState: typed in-memory state ──


class MentionTracker:
    """Tracks consecutive @mentions for auto-focus.

    Supports dict-style access for backward compatibility with code that
    does ``_last_mention["target"]``, ``_last_mention["count"] += 1``, etc.
    """
    _KEYS = ("target", "count", "ts")

    def __init__(self) -> None:
        """Initialize mention streak tracking (count and last timestamp)."""
        self.target: str | None = None
        self.count: int = 0
        self.ts: float = 0.0

    def __getitem__(self, key: str) -> Any:
        """Get a value by key (dict-like access)."""
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a value by key (dict-like access)."""
        setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value with optional default (dict-like access)."""
        return getattr(self, key, default)

    def keys(self) -> tuple[str, ...]:
        """Return all keys (dict-like access)."""
        return self._KEYS

    def __iter__(self) -> Iterator[str]:
        """Iterate over keys (dict-like access)."""
        return iter(self._KEYS)

    def update(self, other: dict[str, Any]) -> None:
        """Update from a mapping (dict-like access)."""
        for k, v in other.items():
            if k in self._KEYS:
                setattr(self, k, v)


class BridgeRuntimeState:
    """Typed in-memory state (RAM only — tmux IS persistence)."""
    def __init__(self) -> None:
        """Initialize bridge runtime state (focus, TTS, admin, notifications)."""
        self.active: str | None = None
        self.startup_notified: bool = False
        self.tts_enabled: bool = False
        self.mention: MentionTracker = MentionTracker()

    # ── dict-compat layer (for state["active"] etc.) ──
    _KEY_MAP = {"active": "active", "startup_notified": "startup_notified",
                "tts_enabled": "tts_enabled"}

    def __getitem__(self, key: str) -> Any:
        """Get a value by key (dict-like access)."""
        attr = self._KEY_MAP.get(key)
        if attr:
            return getattr(self, attr)
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a value by key (dict-like access)."""
        attr = self._KEY_MAP.get(key)
        if attr:
            setattr(self, attr, value)
        else:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value with optional default (dict-like access)."""
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> list[str]:
        """Return all keys (dict-like access)."""
        return list(self._KEY_MAP.keys())

    def __iter__(self) -> Iterator[str]:
        """Iterate over keys (dict-like access)."""
        return iter(self._KEY_MAP)

    def update(self, other: dict[str, Any]) -> None:
        """Update from a mapping (dict-like access)."""
        for k, v in other.items():
            if k in self._KEY_MAP:
                setattr(self, self._KEY_MAP[k], v)


state = BridgeRuntimeState()

# Backward-compat alias for code reading _last_mention directly
_last_mention = state.mention


# ── Typed dataclasses for health/resource probes ─────────────

@dataclass
class DiskUsage:
    """Result of a disk usage probe."""
    pct: float          # usage percentage (0-100)
    free_gb: float      # free space in GB
    total_gb: float     # total space in GB
    ts: float = 0.0     # timestamp of probe

    @classmethod
    def from_dict(cls: type["DiskUsage"], d: dict[str, Any]) -> "DiskUsage":
        """Construct an instance from a plain dictionary."""
        return cls(
            pct=d.get("pct", 0.0),
            free_gb=d.get("free_gb", 0.0),
            total_gb=d.get("total_gb", 0.0),
            ts=d.get("ts", 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        return {"pct": self.pct, "free_gb": self.free_gb,
                "total_gb": self.total_gb, "ts": self.ts}


@dataclass
class MemoryUsage:
    """Result of a memory usage probe."""
    pct: float          # usage percentage (0-100)
    used_gb: float      # used memory in GB
    total_gb: float     # total memory in GB
    available_gb: float = 0.0
    ts: float = 0.0

    @classmethod
    def from_dict(cls: type["MemoryUsage"], d: dict[str, Any]) -> "MemoryUsage":
        """Construct an instance from a plain dictionary."""
        return cls(
            pct=d.get("pct", 0.0),
            used_gb=d.get("used_gb", 0.0),
            total_gb=d.get("total_gb", 0.0),
            available_gb=d.get("available_gb", 0.0),
            ts=d.get("ts", 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        return {"pct": self.pct, "used_gb": self.used_gb,
                "total_gb": self.total_gb, "available_gb": self.available_gb,
                "ts": self.ts}


@dataclass
class IoUsage:
    """Result of an I/O usage probe."""
    read_mb_s: float = 0.0
    write_mb_s: float = 0.0
    iops: int = 0
    util_pct: float = 0.0
    ts: float = 0.0

    @classmethod
    def from_dict(cls: type["IoUsage"], d: dict[str, Any]) -> "IoUsage":
        """Construct an instance from a plain dictionary."""
        return cls(
            read_mb_s=d.get("read_mb_s", 0.0),
            write_mb_s=d.get("write_mb_s", 0.0),
            iops=d.get("iops", 0),
            util_pct=d.get("util_pct", 0.0),
            ts=d.get("ts", 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        return {"read_mb_s": self.read_mb_s, "write_mb_s": self.write_mb_s,
                "iops": self.iops, "util_pct": self.util_pct, "ts": self.ts}


@dataclass
class CpuHog:
    """A process consuming high CPU."""
    pid: int
    cpu_pct: float
    command: str
    user: str = ""

    @classmethod
    def from_dict(cls: type["CpuHog"], d: dict[str, Any]) -> "CpuHog":
        """Construct an instance from a plain dictionary."""
        return cls(
            pid=d.get("pid", 0),
            cpu_pct=d.get("cpu_pct", d.get("cpu", 0.0)),
            command=d.get("command", d.get("cmd", "")),
            user=d.get("user", ""),
        )


@dataclass
class WorktreeItem:
    """A git worktree and its disk usage."""
    path: str
    size_mb: float
    worker: str = ""

    @classmethod
    def from_dict(cls: type["WorktreeItem"], d: dict[str, Any]) -> "WorktreeItem":
        """Construct an instance from a plain dictionary."""
        return cls(
            path=d.get("path", ""),
            size_mb=d.get("size_mb", 0.0),
            worker=d.get("worker", ""),
        )



# ── Watchdog & health monitoring constants ────────────────────────────
_res_cfg = ResourceAlertConfig()
RESTART_COOLDOWN: int = _wd_cfg.restart_cooldown
HOST_DOWN_THRESHOLD: int = _wd_cfg.host_down_threshold

DISK_WARN_THRESHOLD_PCT = _res_cfg.disk_warn_pct
DISK_ALERT_THRESHOLD_PCT = _res_cfg.disk_alert_pct
DISK_ALERT_THRESHOLD_GB = _res_cfg.disk_alert_gb
DISK_ALERT_COOLDOWN = _res_cfg.disk_cooldown

CPU_HOG_THRESHOLD_PCT = _res_cfg.cpu_hog_pct
CPU_HOG_DURATION_MIN = _res_cfg.cpu_hog_duration_min
CPU_HOG_ALERT_COOLDOWN = _res_cfg.cpu_hog_cooldown

WORKTREE_ALERT_THRESHOLD_GB = _res_cfg.worktree_threshold_gb
WORKTREE_ALERT_COOLDOWN = _res_cfg.worktree_cooldown

MEM_ALERT_THRESHOLD_PCT = _res_cfg.mem_threshold_pct
MEM_ALERT_THRESHOLD_GB = _res_cfg.mem_threshold_gb
MEM_ALERT_COOLDOWN = _res_cfg.mem_cooldown

IO_ALERT_IOWAIT_PCT = _res_cfg.io_iowait_pct
IO_ALERT_COOLDOWN = _res_cfg.io_cooldown

INFRA_ALERT_COOLDOWN = _res_cfg.infra_cooldown


class WorkerWatchdogState:
    """Tracks per-worker health, probe results, restart coordination, and watchdog locks."""

    def __init__(self) -> None:
        # Worker probe state
        """Initialize worker watchdog counters, locks, and health maps."""
        self.worker_states: dict[str, tuple] = {}
        self.last_child_ts: dict[str, float] = {}
        self.last_seen_claude: dict[str, float] = {}
        self.last_hook_ts: dict[str, float] = {}
        self.last_alert_ts: dict[str, float] = {}
        self.alert_msg_ids: dict[str, int] = {}
        self.idle_streak: dict[str, int] = {}
        self.prev_worker_states: dict[str, str] = {}
        self.consecutive_probe_failures: dict[str, int] = {}
        self.consecutive_good_probes: dict[str, int] = {}
        self.consecutive_bad_probes: dict[str, int] = {}
        self.idle_child_baseline: dict[str, int] = {}
        self.prev_children: dict[str, int] = {}
        self.last_activity_ts: dict[str, float] = {}
        self.worker_cwds: dict[str, str] = {}
        # Restart coordination
        self.recent_restarts: dict[str, float] = {}
        self.restart_in_progress: dict[str, float] = {}
        self.restart_lock: threading.Lock = threading.Lock()
        self.force_restart_pending_cwd: dict[str, bool] = {}
        self.waiting_input_details: dict[str, QuestionDetails] = {}
        # Alert cooldowns
        self.last_resolved_ts: dict[str, float] = {}
        # Global watchdog lock
        self.lock: threading.Lock = threading.Lock()

    def reset(self) -> None:
        """Reset all state (useful for testing)."""
        self.__init__()

    def clear_worker(self, name: str) -> None:
        """Remove all tracking state for a worker."""
        for store in (
            self.worker_states, self.last_child_ts, self.last_seen_claude,
            self.last_hook_ts, self.last_alert_ts, self.alert_msg_ids,
            self.idle_streak, self.prev_worker_states,
            self.consecutive_probe_failures, self.consecutive_good_probes,
            self.consecutive_bad_probes, self.idle_child_baseline,
            self.prev_children, self.last_activity_ts, self.worker_cwds,
            self.recent_restarts, self.restart_in_progress,
            self.force_restart_pending_cwd, self.waiting_input_details,
            self.last_resolved_ts,
        ):
            store.pop(name, None)


watchdog = WorkerWatchdogState()


class HostHealthState:
    """Tracks health metrics for all remote hosts (SSH, disk, CPU, memory, IO, worktrees, Tailscale)."""

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
        self.cpu_hogs: dict[str, list[dict[str, str | float]]] = {}
        self.cpu_hog_alert_ts: dict[str, float] = {}
        # Worktrees
        self.worktree_usage: dict[str, dict[str, float]] = {}
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
        self.__init__()

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
class LearningReminderState:
    """Tracks per-worker learning reminder counters, timers, and persistence."""

    def __init__(self) -> None:
        """Initialize per-worker reminder counters, lock, and idle timer."""
        self.state: dict[str, ReminderState] = {}
        self.lock: threading.Lock = threading.Lock()
        self.idle_scan_timer: threading.Timer | None = None


learning_reminders = LearningReminderState()

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
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to read learning reminder from {_LEARNING_REMINDER_PATH}: {e}")
    return _LEARNING_REMINDER_TEXT.replace("{name}", name)


def _new_reminder_state() -> ReminderState:
    """Create a fresh learning-reminder state dict with zero counters."""
    now = _clock.time()
    return {
        "response_count": 0,
        "last_reminder_ts": now,
        "last_response_ts": now,
        "reminder_pending": False,
    }


def _learning_reminder_state_file() -> Path:
    """Path to persistent state file (NODE_DIR/learning_reminders.json)."""
    try:
        return os.path.join(str(NODE_DIR), "learning_reminders.json")
    except NameError:
        return None


def _save_learning_reminder_state() -> None:
    """Persist state to disk. Caller should hold learning_reminders.lock."""
    path = _learning_reminder_state_file()
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(learning_reminders.state, f)
        os.replace(tmp, path)
    except OSError as e:
        _log(_LOG_ERROR, "bridge", f"Learning reminder state save error: {e}")


def _load_learning_reminder_state() -> None:
    """Load persisted state from disk into learning_reminders.state."""
    path = _learning_reminder_state_file()
    if not path or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            with learning_reminders.lock:
                for name, st in data.items():
                    if isinstance(st, dict) and "response_count" in st:
                        learning_reminders.state[name] = st
            _log(_LOG_INFO, "worker", f"Learning reminder state loaded: {len(data)} workers")
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_ERROR, "bridge", f"Learning reminder state load error: {e}")


def _reset_learning_reminder(name: str) -> None:
    """Reset learning reminder state for a worker (on hire/restart/SessionStart)."""
    with learning_reminders.lock:
        learning_reminders.state[name] = _new_reminder_state()
        _save_learning_reminder_state()


def _fire_reminder(name: str, st: ReminderState) -> None:
    """Mark state as fired and send reminder in background. Caller holds learning_reminders.lock."""
    st["response_count"] = 0
    st["last_reminder_ts"] = _clock.time()
    st["reminder_pending"] = True
    _save_learning_reminder_state()
    reminder = _read_learning_reminder(name)
    threading.Thread(
        target=_send_learning_reminder,
        args=(name, reminder),
        daemon=True,
    ).start()


def _check_learning_reminder(name: str) -> None:
    """Increment response count and fire learning reminder if threshold met."""
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

        if st["response_count"] >= LEARNING_REMINDER_RESPONSE_THRESHOLD:
            _fire_reminder(name, st)
        else:
            _save_learning_reminder_state()


def _scan_idle_workers() -> None:
    """Check all tracked workers for idle timeout. Called periodically by timer."""
    try:
        now = _clock.time()
        idle_threshold = LEARNING_REMINDER_IDLE_HOURS * 3600
        to_fire = []

        with learning_reminders.lock:
            for name, st in learning_reminders.state.items():
                if st.get("reminder_pending"):
                    continue
                if st.get("response_count", 0) <= 1:
                    continue
                idle_seconds = now - st.get("last_response_ts", now)
                since_reminder = now - st.get("last_reminder_ts", now)
                if idle_seconds >= idle_threshold and since_reminder >= idle_threshold:
                    to_fire.append(name)

            for name in to_fire:
                _fire_reminder(name, learning_reminders.state[name])
    except KeyError as e:
        _log(_LOG_ERROR, "bridge", f"Learning reminder idle scan error: {e}")
    finally:
        _schedule_idle_scan()


def _seed_learning_reminder_state(worker_names: list[str]) -> None:
    """Initialize state for workers not already tracked (from disk or previous session)."""
    with learning_reminders.lock:
        changed = False
        for name in worker_names:
            if name not in learning_reminders.state:
                learning_reminders.state[name] = _new_reminder_state()
                changed = True
        if changed:
            _save_learning_reminder_state()


def _schedule_idle_scan() -> None:
    """Schedule next idle scan (every 30 minutes)."""
    learning_reminders.idle_scan_timer = threading.Timer(1800, _scan_idle_workers)
    learning_reminders.idle_scan_timer.daemon = True
    learning_reminders.idle_scan_timer.start()


def _send_learning_reminder(name: str, text: str) -> None:
    """Send learning reminder to worker (runs in background thread)."""
    try:
        _clock.sleep(DELAY_RESPONSE_GAP)  # avoid colliding with the response
        if send_to_worker(name, text):
            _log(_LOG_INFO, "worker", f"Learning reminder sent to {name}")
        else:
            _log(_LOG_WARN, "bridge", f"Learning reminder: failed to send to {name}")
    except (ConnectionError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"Learning reminder error for {name}: {e}")


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


class MediaGroupState:
    """Buffer for Telegram media groups — collects items before routing."""

    def __init__(self) -> None:
        """Initialize media group buffer and collection lock."""
        self.buffer: dict[str, MediaGroupEntry] = {}
        self.lock: threading.Lock = threading.Lock()


media_groups = MediaGroupState()
_MEDIA_GROUP_WAIT: float = 0.8  # seconds to wait for all items in a group

# ── Typed token stores ───────────────────────────────────────

@dataclass
class RewindToken:
    """A rewind (transcript viewer) token."""
    name: str            # worker name or "__team__"
    expires_at: float    # unix timestamp

    def is_expired(self) -> bool:
        """Check whether this entry has passed its expiration time."""
        return _clock.time() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        return {"name": self.name, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls: type["RewindToken"], d: dict[str, Any]) -> "RewindToken":
        """Construct an instance from a plain dictionary."""
        return cls(name=d["name"], expires_at=d["expires_at"])


@dataclass
class PrReviewToken:
    """A PR review viewer token."""
    pr_num: int
    owner: str
    repo: str
    expires_at: float

    def is_expired(self) -> bool:
        """Check whether this entry has passed its expiration time."""
        return _clock.time() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize this instance to a plain dictionary."""
        return {"pr_num": self.pr_num, "owner": self.owner,
                "repo": self.repo, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls: type["PrReviewToken"], d: dict[str, Any]) -> "PrReviewToken":
        """Construct an instance from a plain dictionary."""
        return cls(pr_num=d["pr_num"], owner=d["owner"],
                   repo=d["repo"], expires_at=d["expires_at"])


# Token stores — still use raw dicts internally for backward compat,
# but the dataclasses above define the canonical shape.
REWIND_TOKENS: dict[str, RewindTokenEntry] = {}
PR_REVIEW_TOKENS: dict[str, PrReviewTokenEntry] = {}
REWIND_TIMEOUT: int = 24 * 60 * 60  # 24 hours (sliding window)

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
    {"command": "relay", "description": "Open public channel: /relay <worker>"},
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

def save_last_chat_id(chat_id: int | str) -> None:
    """Save last known chat ID to file for auto-notification on restart."""
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        LAST_CHAT_ID_FILE.write_text(str(chat_id))
        LAST_CHAT_ID_FILE.chmod(0o600)
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to save last_chat_id: {e}")


def load_last_chat_id() -> int | None:
    """Load last known chat ID from file."""
    try:
        if LAST_CHAT_ID_FILE.exists():
            chat_id = LAST_CHAT_ID_FILE.read_text().strip()
            if chat_id:
                return int(chat_id)
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to load last_chat_id: {e}")
    return None


def save_last_active(name: str) -> None:
    """Save last active worker name to file for auto-focus on restart."""
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        LAST_ACTIVE_FILE.write_text(name)
        LAST_ACTIVE_FILE.chmod(0o600)
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to save last_active: {e}")


def load_last_active() -> str | None:
    """Load last active worker name from file."""
    try:
        if LAST_ACTIVE_FILE.exists():
            name = LAST_ACTIVE_FILE.read_text().strip()
            if name:
                return name
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to load last_active: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Persistent Worker Registry
# ─────────────────────────────────────────────────────────────────────────────

WORKER_REGISTRY_FILE = NODE_DIR / "workers.json"


def _load_registry() -> RegistryFileDict:
    """Load worker registry from disk. Returns {} on missing/corrupt."""
    try:
        if not WORKER_REGISTRY_FILE.exists():
            return {}
        raw = WORKER_REGISTRY_FILE.read_text()
        data = json.loads(raw)
        if not isinstance(data, dict) or "workers" not in data:
            raise ValueError("invalid registry format")
        return data
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        if WORKER_REGISTRY_FILE.exists():
            corrupt_path = WORKER_REGISTRY_FILE.with_suffix(f".corrupt.{int(_clock.time())}")
            _log(_LOG_INFO, "worker", f"Corrupt worker registry, renaming to {corrupt_path}: {e}")
            try:
                WORKER_REGISTRY_FILE.rename(corrupt_path)
            except OSError as exc:
                _log(_LOG_DEBUG, "io:_load_registry", f"{type(exc).__name__}: {exc}")
        return {}


def _save_registry(data: RegistryFileDict) -> None:
    """Atomic write of registry to disk."""
    try:
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(NODE_DIR), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(WORKER_REGISTRY_FILE))
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                _log(_LOG_DEBUG, "io:_save_registry", f"{type(exc).__name__}: {exc}")
            raise
    except OSError as e:
        _log(_LOG_WARN, "worker", f"Failed to save worker registry: {e}")


def _registry_add(name: str, backend: str, chat_id: int | None = None,
                   host: str | None = None) -> None:
    """Add a worker to the persistent registry (merge with existing entry)."""
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data:
            data = {"version": 1, "workers": {}}
        existing = data.get("workers", {}).get(name, {})
        preserved_keys = {"host", "home_host", "home_cwd"}
        entry = {k: v for k, v in existing.items() if k in preserved_keys}
        entry.update({
            "backend": backend,
            "chat_id": chat_id,
            "hire_time": int(_clock.time()),
        })
        if host:
            entry["host"] = host
        data["workers"][name] = entry
        _save_registry(data)


def _registry_add_callback(name: str, callback_url: str, host: str = "",
                           version: str = "", tools: dict[str, Any] | None = None) -> None:
    """Add an HTTP callback worker to the persistent registry."""
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data:
            data = {"version": 1, "workers": {}}
        entry = {
            "backend": DEFAULT_BACKEND,
            "protocol": "http",
            "callback_url": callback_url.rstrip("/"),
            "chat_id": None,
            "hire_time": int(_clock.time()),
        }
        if host:
            entry["host"] = host
        if version:
            entry["version"] = version
        if isinstance(tools, dict):
            entry["tools"] = tools
        data["workers"][name] = entry
        _save_registry(data)


def _registry_remove(name: str) -> None:
    """Remove a worker from the persistent registry."""
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data:
            return
        data["workers"].pop(name, None)
        _save_registry(data)


def _set_worker_cwd(name: str, cwd: str) -> None:
    """Set startup cwd hint for a worker in RAM."""
    normalized = normalize_cwd(cwd)
    with watchdog.lock:
        if normalized:
            watchdog.worker_cwds[name] = normalized
        else:
            watchdog.worker_cwds.pop(name, None)


def _get_worker_cwd(name: str) -> str:
    """Get startup cwd hint for a worker from RAM."""
    with watchdog.lock:
        cwd = watchdog.worker_cwds.get(name)
    return cwd if isinstance(cwd, str) else ""


def _registry_bootstrap(registered: dict[str, TmuxSessionDict]) -> None:
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
            "hire_time": int(_clock.time()),
        }
    _save_registry(data)
    _log(_LOG_INFO, "registry", f"Registry bootstrapped with {len(registered)} workers: {', '.join(registered.keys())}")


def read_checkin_note() -> str:
    """Read checkin note from file. Returns empty string if file missing."""
    try:
        path = _CHECKIN_NOTE_PATH
        if os.path.isfile(path):
            text = open(path).read().strip()
            if text:
                return text
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to read checkin note from {_CHECKIN_NOTE_PATH}: {e}")
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
    """Interface for all outbound messaging from bridge to manager.

    All methods are fully type-annotated so static checkers can verify
    that transports and callers agree on argument/return types.
    """

    @property
    def name(self) -> str:
        """Return the transport name identifier."""
        raise NotImplementedError

    def send_text(self, chat_id: ChatId, text: str,
                  parse_mode: ParseMode | None = None,
                  reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a plain text message."""
        raise NotImplementedError

    def send_rich_text(self, chat_id: ChatId, markdown: str,
                       reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a rich-formatted (markdown) text message."""
        raise NotImplementedError

    def send_photo(self, chat_id: ChatId, photo_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a photo to a Telegram chat."""
        raise NotImplementedError

    def send_document(self, chat_id: ChatId, doc_path: str | Path,
                      caption: str | None = None) -> bool:
        """Send a document file to a Telegram chat."""
        raise NotImplementedError

    def send_animation(self, chat_id: ChatId, animation_path: str | Path,
                       caption: str | None = None) -> bool:
        """Send an animation (GIF/MP4) to a Telegram chat."""
        raise NotImplementedError

    def send_video(self, chat_id: ChatId, video_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a video to a Telegram chat."""
        raise NotImplementedError

    def send_audio(self, chat_id: ChatId, audio_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send an audio file to a Telegram chat."""
        raise NotImplementedError

    def send_voice(self, chat_id: ChatId, voice_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a voice message to a Telegram chat."""
        raise NotImplementedError

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        """Send a sticker to a Telegram chat."""
        raise NotImplementedError

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        """Send a chat action indicator (typing, uploading, etc.)."""
        raise NotImplementedError

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> None:
        """Set an emoji reaction on a message."""
        raise NotImplementedError

    def edit_message(self, chat_id: ChatId, message_id: MessageId,
                     text: str, parse_mode: ParseMode | None = None) -> TelegramApiResponse:
        """Edit an existing message by its ID."""
        raise NotImplementedError

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        """Register bot command suggestions with Telegram."""
        raise NotImplementedError

    def download_file(self, file_id: str, session_name: str) -> str | None:
        """Download a file from Telegram by file ID."""
        raise NotImplementedError


# ============================================================
# TELEGRAM API
# ============================================================

class TelegramAPI:
    """Low-level Telegram Bot API caller. Fully type-annotated."""

    def __init__(self, token: str) -> None:
        """Initialize Telegram Bot API client with the given token."""
        self.token: str = token

    def api(self, method: str, data: dict[str, Any]) -> TelegramApiResponse:
        """Make a raw Telegram Bot API call and return the response."""
        if not self.token:
            return None
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}
        )
        try:
            with _urlopen(req, timeout=TIMEOUT_HTTP_API) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            _log(_LOG_ERROR, "telegram", f"Telegram API error: {e}")
            try:
                raw = e.read()
                body = json.loads(raw)
                return body  # Return error response so callers can inspect description
            except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
                # Non-JSON error body (proxy, middlebox, empty) — return structured error
                return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code} (non-JSON body)"}
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "telegram", f"Telegram API error: {e}")
            return None

    def send_message(self, chat_id: ChatId, text: str, **kwargs: Any) -> TelegramApiResponse:
        """Send a text message via the Telegram Bot API."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        payload.update(kwargs)
        return self.api("sendMessage", payload)

    def send_rich_message(self, chat_id: ChatId, markdown: str, **kwargs: Any) -> TelegramApiResponse:
        """Send a rich-formatted message via the Telegram Bot API."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        payload.update(kwargs)
        return self.api("sendRichMessage", payload)

    def send_photo(self, chat_id: ChatId, photo: str, **kwargs: Any) -> TelegramApiResponse:
        """Send a photo to a Telegram chat."""
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
        payload.update(kwargs)
        return self.api("sendPhoto", payload)

    def send_document(self, chat_id: ChatId, document: str, **kwargs: Any) -> TelegramApiResponse:
        """Send a document file to a Telegram chat."""
        payload: dict[str, Any] = {"chat_id": chat_id, "document": document}
        payload.update(kwargs)
        return self.api("sendDocument", payload)

    def send_animation(self, chat_id: ChatId, animation: str, **kwargs: Any) -> TelegramApiResponse:
        """Send an animation (GIF/MP4) to a Telegram chat."""
        payload: dict[str, Any] = {"chat_id": chat_id, "animation": animation}
        payload.update(kwargs)
        return self.api("sendAnimation", payload)

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> TelegramApiResponse:
        """Set an emoji reaction on a message."""
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}
        return self.api("setMessageReaction", payload)

    def send_chat_action(self, chat_id: ChatId, action: str) -> TelegramApiResponse:
        """Send a chat action indicator (typing, uploading, etc.)."""
        return self.api("sendChatAction", {"chat_id": chat_id, "action": action})


class TelegramTransport(MessageTransport):
    """Transport that sends messages via Telegram Bot API."""

    def __init__(self, token: str) -> None:
        """Initialize Telegram transport wrapping a TelegramAPI instance."""
        self._api: TelegramAPI = TelegramAPI(token)

    @property
    def name(self) -> str:
        """Return the transport name identifier."""
        return "telegram"

    def send_text(self, chat_id: ChatId, text: str,
                  parse_mode: ParseMode | None = None,
                  reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a plain text message."""
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        # Use module-level telegram_api so tests can mock bridge.telegram_api
        return telegram_api("sendMessage", payload)

    def send_rich_text(self, chat_id: ChatId, markdown: str,
                       reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a rich-formatted (markdown) text message."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        return telegram_api("sendRichMessage", payload)

    def send_photo(self, chat_id: ChatId, photo_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a photo to a Telegram chat.

        Validates and auto-resizes the photo if oversized before sending.
        """
        if not BOT_TOKEN:
            return False
        ok, validated = validate_photo_path(photo_path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        photo_data, filename = _prepare_photo_for_telegram(validated)
        mime = mimetypes.guess_type(str(validated))[0] or "image/jpeg"
        return self._send_media_multipart(
            chat_id, validated, "photo", "sendPhoto", caption,
            file_data=photo_data, filename=filename, mime_type=mime,
        )

    def send_animation(self, chat_id: ChatId, animation_path: str | Path,
                       caption: str | None = None) -> bool:
        """Send an animation (GIF/MP4) to a Telegram chat."""
        if not BOT_TOKEN:
            return False
        ok, validated = validate_photo_path(animation_path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        mime = "video/mp4" if validated.suffix.lower() == ".mp4" else "image/gif"
        return self._send_media_multipart(
            chat_id, validated, "animation", "sendAnimation", caption,
            mime_type=mime,
        )

    def send_document(self, chat_id: ChatId, doc_path: str | Path,
                      caption: str | None = None) -> bool:
        """Send a document file to a Telegram chat."""
        if not BOT_TOKEN:
            return False
        ok, validated = validate_document_path(doc_path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        return self._send_media_multipart(
            chat_id, validated, "document", "sendDocument", caption,
        )

    def _send_media_multipart(self, chat_id: ChatId, file_path: Path,
                              field_name: str, api_method: str,
                              caption: str | None = None,
                              file_data: bytes | None = None,
                              filename: str | None = None,
                              mime_type: str | None = None) -> bool:
        """Send a file to Telegram using multipart/form-data.

        Args:
            chat_id: Target chat.
            file_path: Path to the file (used for data, filename, and MIME
                type unless overridden by the optional parameters).
            field_name: Telegram API field name (e.g. "photo", "document").
            api_method: Telegram Bot API method (e.g. "sendPhoto").
            caption: Optional caption text.
            file_data: Pre-loaded bytes (skips reading file_path if provided).
            filename: Override for the filename in Content-Disposition.
            mime_type: Override for the Content-Type of the file part.

        Returns:
            True on success, False on failure.
        """
        if not BOT_TOKEN:
            return False
        data = file_data if file_data is not None else file_path.read_bytes()
        fname = filename or file_path.name
        ctype = mime_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        boundary = uuid.uuid4().hex
        body_parts: list[bytes] = [
            f"--{boundary}".encode(),
            b'Content-Disposition: form-data; name="chat_id"',
            b"",
            str(chat_id).encode(),
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{fname}"'.encode(),
            f"Content-Type: {ctype}".encode(),
            b"",
            data,
        ]
        if caption:
            body_parts.extend([
                f"--{boundary}".encode(),
                b'Content-Disposition: form-data; name="caption"',
                b"",
                caption.encode(),
            ])
        body_parts.append(f"--{boundary}--".encode())
        body_parts.append(b"")
        body = b"\r\n".join(body_parts)
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
            with _urlopen(req, timeout=TIMEOUT_HTTP_UPLOAD) as r:
                result = json.loads(r.read())
                if result.get("ok"):
                    _log(_LOG_INFO, "telegram", f"{api_method} sent: {fname}")
                    return True
                else:
                    _log(_LOG_WARN, "bridge", f"{api_method} failed: {result}")
                    return False
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"{api_method} error: {e}")
            return False

    def send_video(self, chat_id: ChatId, video_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a video to a Telegram chat."""
        ok, validated = validate_document_path(video_path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        return self._send_media_multipart(chat_id, validated, "video", "sendVideo", caption)

    def send_audio(self, chat_id: ChatId, audio_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send an audio file to a Telegram chat."""
        ok, validated = validate_document_path(audio_path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        return self._send_media_multipart(chat_id, validated, "audio", "sendAudio", caption)

    def send_voice(self, chat_id: ChatId, voice_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a voice message to a Telegram chat."""
        ok, validated = validate_document_path(voice_path)
        if not ok:
            _log(_LOG_WARN, "telegram", validated)
            return False
        return self._send_media_multipart(chat_id, validated, "voice", "sendVoice", caption)

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        """Send a sticker to a Telegram chat."""
        sticker_path = Path(sticker_path)
        if not sticker_path.exists() or not sticker_path.is_file():
            _log(_LOG_WARN, "telegram", f"Sticker not found: {sticker_path}")
            return False
        return self._send_media_multipart(chat_id, sticker_path, "sticker", "sendSticker")

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        """Send a chat action indicator (typing, uploading, etc.)."""
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": action})

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> None:
        """Set an emoji reaction on a message."""
        telegram_api("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": reaction})

    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str,
                     parse_mode: ParseMode | None = None) -> TelegramApiResponse:
        """Edit an existing message by its ID."""
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return telegram_api("editMessageText", payload)

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        """Register bot command suggestions with Telegram."""
        telegram_api("setMyCommands", {"commands": commands})

    def download_file(self, file_id: str, session_name: str) -> str | None:
        """Download a file from Telegram by file ID."""
        if not BOT_TOKEN:
            return None
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                data=json.dumps({"file_id": file_id}).encode(),
                headers={"Content-Type": "application/json"}
            )
            with _urlopen(req, timeout=TIMEOUT_HTTP_DOWNLOAD) as r:
                result = json.loads(r.read())
                if not result.get("ok"):
                    _log(_LOG_WARN, "bridge", f"getFile failed: {result}")
                    return None
                file_info = result.get("result", {})
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"getFile error: {e}")
            return None
        file_path = file_info.get("file_path")
        file_size = file_info.get("file_size", 0)
        if not file_path:
            _log(_LOG_WARN, "telegram", "No file_path in response")
            return None
        if file_size > MAX_FILE_SIZE:
            _log(_LOG_WARN, "telegram", f"File too large: {file_size} > {MAX_FILE_SIZE}")
            return None
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        inbox = ensure_inbox_dir(session_name)
        ext = Path(file_path).suffix or ""
        local_filename = f"{uuid.uuid4().hex}{ext}"
        local_path = inbox / local_filename
        try:
            req = urllib.request.Request(download_url)
            with _urlopen(req, timeout=TIMEOUT_HTTP_UPLOAD) as r:
                content = r.read()
                if len(content) > MAX_FILE_SIZE:
                    _log(_LOG_WARN, "telegram", f"Downloaded file too large: {len(content)}")
                    return None
                local_path.write_bytes(content)
                local_path.chmod(0o600)
            _log(_LOG_INFO, "telegram", f"Downloaded file: {local_path}")
            host = get_worker_host(session_name)
            if host:
                remote_inbox = str(inbox)
                _remote_run(["mkdir", "-p", remote_inbox], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK)
                _remote_run(["chmod", "700", remote_inbox], host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK)
                r = _subprocess_runner.run(
                    ["rsync", "-az", str(local_path), f"{host}:{remote_inbox}/"],
                    capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
                if r.returncode != 0:
                    _log(_LOG_ERROR, "bridge", f"rsync inbound failed (exit {r.returncode}): {host}:{remote_inbox}/ -> {r.stderr.decode(errors='replace').strip()}")
            return str(local_path)
        except (subprocess.SubprocessError, OSError) as e:
            _log(_LOG_ERROR, "bridge", f"Download error: {e}")
            return None


class LocalTransport(MessageTransport):
    """Transport that logs messages to stdout. For testing without Telegram."""

    def __init__(self) -> None:
        """Initialize local (in-process) transport with a message log."""
        self._log_file: str = os.environ.get("TRANSPORT_LOG", "")

    @property
    def name(self) -> str:
        """Return the transport name identifier."""
        return "local"

    def _log(self, method: str, chat_id: ChatId, **kwargs: Any) -> None:
        """Log a transport method call with optional kwargs for debugging."""
        msg = f"{method} chat_id={chat_id}"
        for k, v in kwargs.items():
            if v is not None:
                msg += f" {k}={v}"
        _log(_LOG_DEBUG, "local-transport", msg)
        if self._log_file:
            with open(self._log_file, "a") as f:
                f.write(msg + "\n")

    def send_text(self, chat_id: ChatId, text: str,
                  parse_mode: ParseMode | None = None,
                  reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a plain text message."""
        self._log("send_text", chat_id, text=text[:200], parse_mode=parse_mode)
        return {"ok": True, "result": {"message_id": 1}}

    def send_rich_text(self, chat_id: ChatId, markdown: str,
                       reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a rich-formatted (markdown) text message."""
        self._log("send_rich_text", chat_id, text=markdown[:200])
        return {"ok": True, "result": {"message_id": 1}}

    def send_photo(self, chat_id: ChatId, photo_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a photo to a Telegram chat."""
        self._log("send_photo", chat_id, path=photo_path, caption=caption)
        return True

    def send_document(self, chat_id: ChatId, doc_path: str | Path,
                      caption: str | None = None) -> bool:
        """Send a document file to a Telegram chat."""
        self._log("send_document", chat_id, path=doc_path, caption=caption)
        return True

    def send_animation(self, chat_id: ChatId, animation_path: str | Path,
                       caption: str | None = None) -> bool:
        """Send an animation (GIF/MP4) to a Telegram chat."""
        self._log("send_animation", chat_id, path=animation_path, caption=caption)
        return True

    def send_video(self, chat_id: ChatId, video_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a video to a Telegram chat."""
        self._log("send_video", chat_id, path=video_path, caption=caption)
        return True

    def send_audio(self, chat_id: ChatId, audio_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send an audio file to a Telegram chat."""
        self._log("send_audio", chat_id, path=audio_path, caption=caption)
        return True

    def send_voice(self, chat_id: ChatId, voice_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a voice message to a Telegram chat."""
        self._log("send_voice", chat_id, path=voice_path, caption=caption)
        return True

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        """Send a sticker to a Telegram chat."""
        self._log("send_sticker", chat_id, path=sticker_path)
        return True

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        """Send a chat action indicator (typing, uploading, etc.)."""
        self._log("send_chat_action", chat_id, action=action)

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> None:
        """Set an emoji reaction on a message."""
        self._log("set_reaction", chat_id, message_id=message_id)

    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str,
                     parse_mode: ParseMode | None = None) -> TelegramApiResponse:
        """Edit an existing message by its ID."""
        self._log("edit_message", chat_id, message_id=message_id, text=text[:200])
        return {"ok": True, "result": {"message_id": message_id}}

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        """Register bot command suggestions with Telegram."""
        self._log("setup_commands", 0, count=len(commands))

    def download_file(self, file_id: str, session_name: str) -> str | None:
        """Download a file from Telegram by file ID."""
        self._log("download_file", 0, file_id=file_id, session=session_name)
        return None


def _init_transport() -> MessageTransport:
    """Create the appropriate MessageTransport based on TRANSPORT_MODE."""
    if TRANSPORT_MODE == "local":
        return LocalTransport()
    return TelegramTransport(BOT_TOKEN)


transport = _init_transport()


def telegram_api(method: str, data: dict[str, Any]) -> TelegramApiResponse:
    """Low-level Telegram API call. Tests can mock this to intercept all outbound calls."""
    if TRANSPORT_MODE == "local":
        _log(_LOG_INFO, "local-transport", f"telegram_api {method} {str(data)[:100]}")
        return {"ok": True, "result": {"message_id": 1}}
    if isinstance(transport, TelegramTransport):
        return transport._api.api(method, data)
    return None


def send_telegram_message(chat_id: ChatId, text: str,
                          parse_mode: ParseMode | None = None) -> TelegramApiResponse:
    """Send a Telegram message, optionally with parse_mode (HTML or MarkdownV2)."""
    return transport.send_text(chat_id, text, parse_mode=parse_mode)


def download_telegram_file(file_id: str, session_name: str) -> str | None:
    """Download a Telegram file to the session inbox.
    Tests can patch bridge.download_telegram_file to intercept file downloads.
    Delegates to transport.download_file() internally.
    """
    return transport.download_file(file_id, session_name)


# Backward-compat module-level media stubs.
# Tests patch these (e.g. patch.object(bridge, 'send_voice', ...)).
# Production code routes through transport.*; these stubs allow test mocking.
def send_voice(chat_id: int | str, voice_path: str, caption: str | None = None) -> bool:
    """Send a voice message to a Telegram chat."""
    return transport.send_voice(chat_id, voice_path, caption)


def send_photo(chat_id: int | str, photo_path: str, caption: str | None = None) -> bool:
    """Send a photo to a Telegram chat."""
    return transport.send_photo(chat_id, photo_path, caption)


def send_animation(chat_id: int | str, animation_path: str, caption: str | None = None) -> bool:
    """Send an animation (GIF/MP4) to a Telegram chat."""
    return transport.send_animation(chat_id, animation_path, caption)


def send_document(chat_id: int | str, doc_path: str, caption: str | None = None) -> bool:
    """Send a document file to a Telegram chat."""
    return transport.send_document(chat_id, doc_path, caption)


def send_video(chat_id: int | str, video_path: str, caption: str | None = None) -> bool:
    """Send a video to a Telegram chat."""
    return transport.send_video(chat_id, video_path, caption)


def send_audio(chat_id: int | str, audio_path: str, caption: str | None = None) -> bool:
    """Send an audio file to a Telegram chat."""
    return transport.send_audio(chat_id, audio_path, caption)


def send_sticker(chat_id: int | str, sticker_path: str) -> bool:
    """Send a sticker to a Telegram chat."""
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


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable form."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def get_inbox_dir(session_name: str) -> Path:
    """Get inbox directory for incoming files (images, documents, etc.).

    Uses /tmp for ephemeral storage, session-namespaced to prevent cross-session access.
    """
    return FILE_INBOX_ROOT / session_name / "inbox"


def ensure_inbox_dir(session_name: str) -> Path:
    """Create inbox directory with secure permissions."""
    inbox = get_inbox_dir(session_name)
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    inbox.chmod(0o700)
    return inbox


def cleanup_inbox(session_name: str) -> None:
    """Clean up all files in a session's inbox."""
    inbox = get_inbox_dir(session_name)
    if inbox.exists():
        for f in inbox.iterdir():
            try:
                f.unlink()
            except OSError as e:
                _log(_LOG_WARN, "bridge", f"Failed to delete {f}: {e}")


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
    if name in processes.pipe_readers:
        processes.pipe_readers.pop(name, None)
    _log(_LOG_INFO, "pipe", f"Pipe reader stopped for worker '{name}'")


def _forward_pipe_message(name: str, message: str) -> None:
    """Forward a message from the pipe to the worker's session.

    Uses backend routing for tmux or non-interactive workers.
    """
    if not worker_manager.send(name, message):
        _log(_LOG_WARN, "worker", f"Warning: Cannot forward pipe message to '{name}' - worker not found")


def start_pipe_reader(name: str) -> None:
    """Start a background thread to read from the worker's input pipe."""
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
    processes.pipe_readers[name] = (thread, stop_event)
    thread.start()
    _log(_LOG_INFO, "pipe", f"Started pipe reader thread for '{name}'")


def stop_pipe_reader(name: str) -> None:
    """Stop the pipe reader thread for a worker."""
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
            os.write(fd, b"\n")
            os.close(fd)
        except OSError:
            pass  # intentional no-op: pipe may already be closed by reader

    # Wait for thread to finish (with timeout)
    thread.join(timeout=TIMEOUT_THREAD_JOIN)
    if thread.is_alive():
        _log(_LOG_WARN, "bridge", f"Warning: pipe reader thread for '{name}' did not stop gracefully")


def get_workers(caller_from: str | None = None) -> list[dict[str, Any]]:
    """Get all active workers with their communication details.

    If ``caller_from`` is set to a worker name, ``send_example`` for each peer
    is rendered from that caller's machine perspective.
    """
    _sync_worker_manager()
    return _merge_grpc_workers(worker_manager.get_workers(caller_from=caller_from))


# download_telegram_file removed — use download_telegram_file() instead


def transcribe_voice(file_path: str, timeout: int | None = None) -> str | None:
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
        with _urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read())
            text = result.get("text", "").strip()
            if text:
                duration = result.get("audio_duration_s", "?")
                _log(_LOG_INFO, "stt", f"STT transcribed: {len(text)} chars from {duration}s audio")
                return text
            return None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"STT error (fail-open): {e}")
        return None


def synthesize_speech(text: str, voice: str | None = None, language: str = "en") -> str | None:
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
        with _urlopen(req, timeout=TTS_TIMEOUT) as r:
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
            _log(_LOG_INFO, "tts", f"TTS synthesized ({mode}): {len(clean)} chars -> {duration}s audio in {proc_time}s")
            return str(tmp_path)
    except (urllib.error.URLError, TimeoutError, TypeError, OSError, KeyError) as e:
        _log(_LOG_ERROR, "bridge", f"TTS error (fail-open): {e}")
        return None


TELEGRAM_PHOTO_MAX_SUM = 10000  # width + height must not exceed this
TELEGRAM_PHOTO_MAX_DIM = 5000   # neither dimension may exceed this


def _prepare_photo_for_telegram(photo_path: str) -> tuple[bytes, str]:
    """Auto-resize a photo if it exceeds Telegram's sendPhoto limits.

    Telegram returns 400 Bad Request when width+height > 10000 or either
    dimension > 5000.  This function scales down proportionally when needed
    and returns (bytes, filename).  If no resize is needed, returns the
    original file bytes unchanged.
    """
    try:
        from PIL import Image
        img = Image.open(photo_path)
        w, h = img.size
        needs_resize = (
            w + h > TELEGRAM_PHOTO_MAX_SUM or
            w > TELEGRAM_PHOTO_MAX_DIM or
            h > TELEGRAM_PHOTO_MAX_DIM
        )
        if needs_resize:
            # Scale proportionally so both constraints are satisfied
            scale = min(
                TELEGRAM_PHOTO_MAX_DIM / max(w, 1),
                TELEGRAM_PHOTO_MAX_DIM / max(h, 1),
                TELEGRAM_PHOTO_MAX_SUM / max(w + h, 1),
            )
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            import io
            buf = io.BytesIO()
            fmt = img.format or ("PNG" if photo_path.suffix.lower() == ".png" else "JPEG")
            if fmt == "PNG" and img.mode not in ("RGBA", "P", "L", "LA"):
                img = img.convert("RGBA")
            elif fmt == "JPEG" and img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buf, format=fmt)
            _log(_LOG_INFO, "telegram", f"Photo auto-resized: {w}x{h} -> {new_w}x{new_h} for Telegram")
            return buf.getvalue(), photo_path.name
        return photo_path.read_bytes(), photo_path.name
    except ImportError:
        return photo_path.read_bytes(), photo_path.name


def validate_photo_path(photo_path: str) -> FileValidation:
    """Validate a photo path. Returns FileValidation(ok, Path_or_error_str)."""
    photo_path = Path(photo_path)

    if not photo_path.exists():
        return FileValidation(False, f"Photo not found: {photo_path}")

    if not photo_path.is_file():
        return FileValidation(False, f"Not a file: {photo_path}")

    # Check extension
    if photo_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
        return FileValidation(False, f"Invalid image extension: {photo_path.suffix}")

    # Check size
    file_size = photo_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        return FileValidation(False, f"Photo too large: {file_size} > {MAX_FILE_SIZE}")

    return FileValidation(True, photo_path)


def is_blocked_filename(filename: str) -> bool:
    """Check if filename matches blocked patterns (secrets, credentials, etc.)."""
    name_lower = filename.lower()
    # Check exact filename matches
    if name_lower in BLOCKED_FILENAMES:
        return True
    # Check .env.* pattern
    if name_lower.startswith(".env"):
        return True
    return False


def validate_document_path(doc_path: str) -> FileValidation:
    """Validate a document path. Returns FileValidation(ok, Path_or_error_str)."""
    doc_path = Path(doc_path)

    # Security: validate path exists and is regular file
    if not doc_path.exists():
        return FileValidation(False, f"Document not found: {doc_path}")

    if not doc_path.is_file():
        return FileValidation(False, f"Not a file: {doc_path}")

    # Security: check for blocked extensions (sensitive)
    ext_lower = doc_path.suffix.lower()
    if ext_lower in BLOCKED_DOC_EXTENSIONS:
        return FileValidation(False, f"Blocked extension (sensitive): {doc_path.suffix}")

    # Security: check for blocked filenames
    if is_blocked_filename(doc_path.name):
        return FileValidation(False, f"Blocked filename (sensitive): {doc_path.name}")

    # Check size
    file_size = doc_path.stat().st_size
    if file_size > MAX_FILE_SIZE:
        return FileValidation(False, f"Document too large: {file_size} > {MAX_FILE_SIZE}")

    # Note: No path restriction - workers can send from anywhere
    # Security is enforced via extension allowlist and filename blocklist

    return FileValidation(True, doc_path)


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


def _split_protected_segments(text: str, pattern: str) -> list[tuple]:
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


def _collapse_excess_newlines(text: str) -> str:
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


def _parse_media_tags(text: str, tag_name: str, validate_func: Callable[[str], FileValidation]) -> tuple[str, list[tuple[str, str]]]:
    """Parse media tags, skipping escaped tags and code spans.

    Returns (clean_text, [(path, caption), ...]).
    """
    pattern = re.compile(rf"(\\)?\[\[{tag_name}:([^\]|]+)(?:\|([^\]]*))?\]\]")
    items = []
    removed = 0

    def replace_tag(match: re.Match[str]) -> str:
        """Replace HTML tag names in a sanitizer context."""
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


def parse_image_tags(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse [[image:/path|caption]] tags from text.

    Returns (clean_text, [(path, caption), ...]).
    """
    return _parse_media_tags(text, "image", validate_photo_path)


def parse_file_tags(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Parse [[file:/path|caption]] tags from text.

    Returns (clean_text, [(path, caption), ...]).
    """
    return _parse_media_tags(text, "file", validate_document_path)


def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram's HTML parse mode.

    Must escape &, <, > to prevent Telegram from interpreting them as HTML tags.
    """
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


class _TelegramHTMLSanitizer(HTMLParser):
    """Sanitize HTML to only allow Telegram-safe tags and attributes."""

    SAFE_TAGS: frozenset[str] = frozenset({
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "code", "pre", "a", "blockquote", "span", "tg-emoji", "tg-spoiler",
    })
    SAFE_ATTRS: dict[str, frozenset[str]] = {
        "a": frozenset({"href"}),
        "code": frozenset({"class"}),
        "blockquote": frozenset({"expandable"}),
        "span": frozenset({"class"}),
        "tg-emoji": frozenset({"emoji-id"}),
    }

    def __init__(self, rejected_open_tags: list[str]) -> None:
        """Initialize sanitizer with shared rejected-tag tracker."""
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._rejected_open_tags = rejected_open_tags

    def _escape_attr(self, value: str) -> str:
        """Escape an HTML attribute value (entities + quotes)."""
        return escape_html(value).replace('"', "&quot;")

    def _attrs_are_safe(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        """Check if all attributes on a tag are in the allowed set."""
        allowed = self.SAFE_ATTRS.get(tag, frozenset())
        seen: set[str] = set()
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

    def _render_start_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        """Render a safe opening tag with escaped attributes."""
        if not attrs:
            return f"<{tag}>"
        rendered = []
        for name, value in attrs:
            if value is None:
                rendered.append(name)
            else:
                rendered.append(f'{name}="{self._escape_attr(value)}"')
        return f"<{tag} {' '.join(rendered)}>"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Process an opening HTML tag during sanitization."""
        accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
        if accepted:
            self._out.append(self._render_start_tag(tag, attrs))
        else:
            self._out.append(escape_html(self.get_starttag_text() or f"<{tag}>"))
            self._rejected_open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        """Process a closing HTML tag during sanitization."""
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

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Process a self-closing HTML tag during sanitization."""
        accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
        if accepted:
            start = self._render_start_tag(tag, attrs)
            self._out.append(f"{start[:-1]}/>")
        else:
            self._out.append(escape_html(self.get_starttag_text() or f"<{tag}/>"))

    def handle_data(self, data: str) -> None:
        """Process raw text content during sanitization."""
        self._out.append(escape_html(data))

    def handle_entityref(self, name: str) -> None:
        """Process a named HTML entity during sanitization."""
        self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        """Process a numeric HTML character reference during sanitization."""
        self._out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        """Discard HTML comments during sanitization."""
        self._out.append(escape_html(f"<!--{data}-->"))

    def html(self) -> str:
        """Return the sanitized HTML output."""
        return "".join(self._out)


def _sanitize_telegram_html(raw: str, rejected_open_tags: list[str]) -> str:
    """Sanitize HTML to only allow Telegram-safe tags and attributes."""
    if not raw:
        return ""
    sanitizer = _TelegramHTMLSanitizer(rejected_open_tags)
    sanitizer.feed(raw)
    sanitizer.close()
    return sanitizer.html()


def _render_md_inline_plain(children: list[Any]) -> str:
    """Render inline markdown-it token children to plain text (for <pre> content)."""
    out: list[str] = []
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
            pass  # intentional no-op: skip formatting tokens
        else:
            if tok.content:
                out.append(tok.content)
    return "".join(out)


def _render_md_inline_html(children: list[Any], rejected_open_tags: list[str]) -> str:
    """Render inline markdown-it token children to Telegram HTML."""
    out: list[str] = []
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
            out.append(_sanitize_telegram_html(tok.content, rejected_open_tags))
        else:
            if tok.content:
                out.append(escape_html(tok.content))
    return "".join(out)


def _render_table_as_pre(headers: list[str], rows: list[list[str]]) -> str:
    """Render markdown table rows as a <pre>-aligned column block."""
    all_rows = [headers] + rows
    if not all_rows or not all_rows[0]:
        return ""
    num_cols = max(len(r) for r in all_rows)
    col_widths = [0] * num_cols
    for row in all_rows:
        for ci, cell in enumerate(row):
            if ci < num_cols:
                col_widths[ci] = max(col_widths[ci], len(cell))
    lines: list[str] = []
    for ri, row in enumerate(all_rows):
        cols = []
        for ci in range(num_cols):
            cell = row[ci] if ci < len(row) else ""
            cols.append(escape_html(cell.ljust(col_widths[ci])))
        lines.append("  ".join(cols).rstrip())
        if ri == 0:
            lines.append("\u2550" * (sum(col_widths) + 2 * (num_cols - 1)))
    return f"<pre>{chr(10).join(lines)}</pre>\n"


# Pattern for detecting tabular lines (2+ columns separated by 2+ spaces)
_MULTI_SPACE_RE = re.compile(r'\S  +\S.*\S  +\S')


def _wrap_plain_tables(text: str) -> str:
    """Find consecutive tabular lines outside <pre> and wrap in <pre>."""
    parts = re.split(r'(<pre>.*?</pre>)', text, flags=re.DOTALL)
    out: list[str] = []
    for part in parts:
        if part.startswith('<pre>'):
            out.append(part)
            continue
        lines = part.split('\n')
        i = 0
        while i < len(lines):
            if _MULTI_SPACE_RE.search(lines[i]):
                run = [lines[i]]
                j = i + 1
                while j < len(lines) and (_MULTI_SPACE_RE.search(lines[j]) or lines[j].strip() == ''):
                    run.append(lines[j])
                    j += 1
                tabular_count = sum(1 for line in run if _MULTI_SPACE_RE.search(line))
                if tabular_count >= 2:
                    while run and run[-1].strip() == '':
                        j -= 1
                        run.pop()
                    raw = []
                    for rl in run:
                        plain = re.sub(r'<[^>]+>', '', rl)
                        plain = plain.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                        raw.append(plain)
                    content = escape_html('\n'.join(raw))
                    out.append(f'<pre>{content}</pre>')
                    i = j
                else:
                    out.append(lines[i])
                    i += 1
            else:
                out.append(lines[i])
                i += 1
    return '\n'.join(out) if out else text


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML using markdown-it-py.

    Handles: bold, italic, strikethrough, code, code blocks, links,
    blockquotes, headings (as bold), lists, tables (as pre), hr.
    Unrecognized tokens degrade to plain text.
    """
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable("strikethrough").enable("table")
    tokens = md.parse(text)

    result: list[str] = []
    list_depth = 0
    ordered_counter: list[int] = []
    in_table = False
    table_row: list[str] = []
    table_headers: list[str] = []
    table_rows: list[list[str]] = []
    in_thead = False
    rejected_open_tags: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok.type == "paragraph_open":
            pass  # intentional no-op: skip token
        elif tok.type == "paragraph_close":
            if not in_table:
                result.append("\n")
        elif tok.type == "inline":
            if in_table:
                table_row.append(_render_md_inline_plain(tok.children or []))
            else:
                result.append(_render_md_inline_html(tok.children or [], rejected_open_tags))

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
            pre_block = _render_table_as_pre(table_headers, table_rows)
            if pre_block:
                result.append(pre_block)
            table_headers = []
            table_rows = []
        elif tok.type == "thead_open":
            in_thead = True
        elif tok.type == "thead_close":
            in_thead = False
        elif tok.type in ("tbody_open", "tbody_close"):
            pass  # intentional no-op: skip token
        elif tok.type == "tr_open":
            table_row = []
        elif tok.type == "tr_close":
            if in_thead:
                table_headers = table_row[:]
            else:
                table_rows.append(table_row[:])
            table_row = []
        elif tok.type in ("th_open", "th_close", "td_open", "td_close"):
            pass  # intentional no-op: skip token

        # Horizontal rule
        elif tok.type == "hr":
            result.append("\u2014\u2014\u2014\u2014\n")

        # HTML blocks
        elif tok.type == "html_block":
            result.append(_sanitize_telegram_html(tok.content, rejected_open_tags))

        else:
            if tok.content:
                result.append(escape_html(tok.content))

        i += 1

    output = "".join(result).strip()
    while "\n\n\n" in output:
        output = output.replace("\n\n\n", "\n\n")

    return _wrap_plain_tables(output)


def _pipe_tables_to_html(text: str) -> str:
    """Convert GFM pipe tables to HTML <table> for sendRichMessage.

    Telegram's sendRichMessage markdown parser doesn't recognize GFM pipe
    table syntax — it collapses table rows into a single paragraph.
    This converts pipe tables to HTML tables which sendRichMessage renders
    natively via RichBlockTable.
    """
    import re

    def _parse_row(line: str) -> list[str] | None:
        """Parse a pipe-delimited table row into cells, or None if not a table row."""
        line = line.strip()
        if line.startswith('|'):
            line = line[1:]
        if line.endswith('|'):
            line = line[:-1]
        return [cell.strip() for cell in line.split('|')]

    def _esc(s: str) -> str:
        """Escape HTML special characters in table cell text."""
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _cell_md(s: str) -> str:
        """Escape HTML then convert inline markdown in a table cell."""
        s = _esc(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
        s = re.sub(r'__(.+?)__', r'<b>\1</b>', s)
        s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', s)
        s = re.sub(r'~~(.+?)~~', r'<s>\1</s>', s)
        s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
        return s

    lines = text.split('\n')
    result = []
    i = 0
    in_code_fence = False
    while i < len(lines):
        # Track fenced code blocks — don't convert tables inside them
        if re.match(r'^\s*(`{3,}|~{3,})', lines[i]):
            in_code_fence = not in_code_fence
            result.append(lines[i])
            i += 1
            continue

        # Detect pipe table: header row, separator row, then data rows
        if (not in_code_fence
                and i + 1 < len(lines)
                and '|' in lines[i]
                and re.match(r'^\s*\|[\s:]*-+[\s:]*(\|[\s:]*-+[\s:]*)*\|?\s*$', lines[i + 1])):
            headers = _parse_row(lines[i])

            # Parse alignment from separator
            aligns = []
            for cell in _parse_row(lines[i + 1]):
                cell = cell.strip()
                if cell.startswith(':') and cell.endswith(':'):
                    aligns.append(' style="text-align:center"')
                elif cell.endswith(':'):
                    aligns.append(' style="text-align:right"')
                else:
                    aligns.append('')

            html = ['<table>']
            html.append('<tr>')
            for j, h in enumerate(headers):
                align = aligns[j] if j < len(aligns) else ''
                html.append(f'<th{align}>{_cell_md(h)}</th>')
            html.append('</tr>')

            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip().startswith('|'):
                cells = _parse_row(lines[i])
                html.append('<tr>')
                for j, cell in enumerate(cells):
                    align = aligns[j] if j < len(aligns) else ''
                    html.append(f'<td{align}>{_cell_md(cell)}</td>')
                html.append('</tr>')
                i += 1

            html.append('</table>')
            result.append('\n'.join(html))
        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def format_response_text(session_name: str, text: str) -> str:
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
TELEGRAM_RICH_MAX_LENGTH = 32768


def split_message(text: str, max_len: int=TELEGRAM_MAX_LENGTH) -> list[str]:
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

    def _closing_tags(stack: list[str]) -> str:
        """Generate closing tags for all open tags (reverse order)."""
        return "".join(f"</{tag}>" for tag, _ in reversed(stack))

    def _opening_tags(stack: list[str]) -> str:
        """Generate opening tags for all open tags (original order)."""
        return "".join(full for _, full in stack)

    def _scan_tags(text: str) -> list[str]:
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

    def _find_split(text: str, budget: int) -> str:
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


def format_multipart_messages(session_name: str, chunks: list[str]) -> list[str]:
    """Format chunks with session prefix (all chunks get prefix, no part numbers).

    Single chunk: "<b>name:</b>\ntext"
    Multiple chunks: "<b>name:</b>\ntext" (same format, no 1/3, 2/3 etc)
    """
    return [format_response_text(session_name, chunk) for chunk in chunks]


def setup_bot_commands() -> None:
    """Initial bot commands setup."""
    update_bot_commands()


def update_bot_commands() -> None:
    """Update bot commands including dynamic worker shortcuts."""
    commands = list(BOT_COMMANDS)  # Copy static commands

    # Add worker shortcuts (e.g., /lee, /chen)
    registered = get_registered_sessions()
    for name in sorted(registered.keys()):
        commands.append({"command": name, "description": f"Message {name}"})

    transport.setup_commands(commands)
    worker_count = len(registered)
    _log(_LOG_INFO, "telegram", f"Bot commands updated ({len(BOT_COMMANDS)} + {worker_count} workers)")


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


def get_pending_file(name: str) -> Path:
    """Return the path to a worker's pending-message file."""
    return get_session_dir(name) / "pending"


def get_chat_id_file(name: str) -> Path:
    """Return the path to a worker's chat ID file."""
    return get_session_dir(name) / "chat_id"


def get_manager_chat_id(name: str) -> int | None:
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
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to read chat_id for {name}: {e}")
        return None


def _read_session_file(name: str, filename: str) -> str | None:
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
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode == 0:
                val = r.stdout.strip()
                # Cache locally for next read
                try:
                    ensure_session_dir(name)
                    f.write_text(val)
                    f.chmod(0o600)
                except OSError as exc:
                    _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
                return val
        except (OSError, ValueError) as exc:
            _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
    return ""


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


def get_session_history(name: str, event: str | None = None) -> list[dict[str, Any]]:
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

    Skips the write if the directory is already trusted.
    """
    if not cwd:
        return
    target = config_path or _CLAUDE_JSON_PATH
    try:
        if target.exists():
            data = json.loads(target.read_text())
        else:
            data = {}
        projects = data.setdefault("projects", {})
        entry = projects.get(cwd, {})
        if entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted — skip rewrite
        projects[cwd] = {**entry, "hasTrustDialogAccepted": True}
        target.write_text(json.dumps(data, indent=2))
        _log(_LOG_INFO, "trust", f"pre-trusted workspace: {cwd}")
    except (OSError, json.JSONDecodeError) as exc:
        _log(_LOG_WARN, "trust", f"could not pre-trust {cwd}: {exc}")


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
        id_file.write_text(f"{sid}\n{cwd}")
        id_file.chmod(0o600)
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
    cwd = _read_session_file(name, "claude_session_cwd")
    if cwd:
        cwd = os.path.expanduser(cwd)
    return cwd



def save_claude_session_cwd(name: str, cwd: str) -> None:
    """Persist a worker's current working directory to disk."""
    if cwd:
        cwd = os.path.expanduser(cwd)
    session_dir = ensure_session_dir(name)
    cwd_file = session_dir / "claude_session_cwd"
    cwd_file.write_text(cwd)
    cwd_file.chmod(0o600)



def clear_claude_session_id(name: str) -> None:
    """Remove the cached session ID for a worker."""
    id_file = get_session_dir(name) / "claude_session_id"
    if id_file.exists():
        id_file.unlink()



def get_any_session_id(name: str) -> str | None:
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


# (pending locks moved to processes.pending_locks)
# (pending locks guard moved to processes.pending_locks_guard)


def _get_pending_lock(name: str) -> threading.Lock:
    """Get or create a per-worker lock for atomic pending check+set."""
    with processes.pending_locks_guard:
        if name not in processes.pending_locks:
            processes.pending_locks[name] = threading.Lock()
        return processes.pending_locks[name]


def set_pending(name: str, chat_id: int | str) -> None:
    """Mark session as having a pending request with secure permissions (0o600)."""
    session_dir = ensure_session_dir(name)
    pending = session_dir / "pending"
    chat_id_file = session_dir / "chat_id"
    pending.write_text(str(int(_clock.time())))
    pending.chmod(0o600)
    chat_id_file.write_text(str(chat_id))
    chat_id_file.chmod(0o600)
    # Sync chat_id to remote host if worker is teleported.
    # The Stop hook reads chat_id locally — without this, responses from
    # teleported workers never reach Telegram.
    _sync_chat_id_to_remote(name, str(chat_id_file))


# (remote home cache moved to remote_cache.home_dirs)

def _get_remote_home(host: str | None) -> str:
    """Get remote $HOME with caching (avoids SSH per message)."""
    if host in remote_cache.home_dirs:
        return remote_cache.home_dirs[host]
    try:
        r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                        capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        home = r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        home = ""
    remote_cache.home_dirs[host] = home
    return home


def _remap_sessions_dir(host: str | None) -> str:
    """Remap SESSIONS_DIR to use remote host's $HOME prefix."""
    remote_sessions_dir = str(SESSIONS_DIR)
    local_home = os.path.expanduser("~")
    remote_home = _get_remote_home(host)
    if remote_home and remote_home != local_home and remote_sessions_dir.startswith(local_home):
        remote_sessions_dir = remote_home + remote_sessions_dir[len(local_home):]
    return remote_sessions_dir


def _sync_chat_id_to_remote(name: str, local_chat_id_path: str) -> None:
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
                     host=host, capture_output=True, timeout=TIMEOUT_TMUX_CHECK)
        _remote_copy(local_chat_id_path, f"{remote_sessions_dir}/{name}/chat_id",
                      host=host, direction="push")
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_WARN, "set_pending", f"Failed to sync chat_id to {host} for {name}: {e}")


def clear_pending(name: str) -> None:
    """Clear pending status for session."""
    session_dir = get_session_dir(name)
    pending = session_dir / "pending"
    try:
        pending.unlink()
    except FileNotFoundError as exc:
        _log(_LOG_DEBUG, "cleanup:clear_pending", f"{type(exc).__name__}: {exc}")


def is_pending(name: str) -> bool:
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
        if (_clock.time() - ts) > PENDING_TIMEOUT:
            return False
        return True
    except (OSError, ValueError):
        return False


def try_set_pending(name: str, chat_id: int | str) -> bool:
    """Atomic check+set: returns True if pending was set, False if already pending."""
    with _get_pending_lock(name):
        if is_pending(name):
            return False
        set_pending(name, chat_id)
        return True


def _pending_timestamp(name: str) -> int | None:
    """Read the mtime of a worker's pending-response file, or None if absent."""
    pending = get_pending_file(name)
    if not pending.exists():
        return None
    try:
        return int(pending.read_text().strip())
    except OSError:
        return None


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
    """Compute the current watchdog state for a worker (READY, BUSY, STUCK, etc.)."""
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
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            remote_home = r.stdout.strip() if r.returncode == 0 else ""
            local_home = str(Path.home())
            remote_log = str(get_session_dir(name) / "adapter.log")
            if remote_home and remote_home != local_home and remote_log.startswith(local_home):
                remote_log = remote_home + remote_log[len(local_home):]
            r = _remote_run(["tail", "-n", str(tail_lines), remote_log], host=host,
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            return r.stdout if r.returncode == 0 else ""
        except (subprocess.SubprocessError, OSError):
            return ""
    log_path = get_session_dir(name) / "adapter.log"
    if not log_path.exists():
        return ""
    try:
        with log_path.open("r", errors="ignore") as fh:
            lines = fh.readlines()
        return "".join(lines[-tail_lines:])
    except OSError:
        return ""


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
        text_parts.append(_check_adapter_log(name))
    combined = "\n".join([part for part in text_parts if part])
    if not combined:
        return None
    for pattern in POISON_PATTERNS:
        if len(pattern.findall(combined)) >= 3:
            return pattern.pattern
    return None


def _send_watchdog_alert(name: str, state: str, reason: str) -> None:
    """Send a watchdog alert to Telegram admin when a worker changes state."""
    if admin_chat_id is None:
        return

    now = _clock.time()
    with watchdog.lock:
        last = watchdog.last_alert_ts.get(name)
    if last and (now - last) < ALERT_COOLDOWN:
        _log(_LOG_WARN, "watchdog", f"Alert suppressed for {name} ({state}): cooldown {now - last:.0f}s < {ALERT_COOLDOWN}s")
        return

    # Human-friendly alert messages for manager
    if state == "WAITING_INPUT":
        with watchdog.lock:
            details = watchdog.waiting_input_details.get(name)
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
            _log(_LOG_WARN, "watchdog", f"Alert sent for {name} ({state}): {text[:80]}")
            msg_id = result.get("result", {}).get("message_id")
            with watchdog.lock:
                watchdog.last_alert_ts[name] = now
                if msg_id:
                    watchdog.alert_msg_ids[name] = (msg_id, text)
        else:
            _log(_LOG_WARN, "watchdog", f"Alert FAILED for {name} ({state}): {result}")
    except KeyError as e:
        _log(_LOG_ERROR, "watchdog", f"Watchdog alert error: {e}")


def _record_host_probe(host: str, ok: bool, error: str | None = None) -> None:
    """Track host SSH probe results; alert on DOWN/BACK UP transitions."""
    now = _clock.time()
    with watchdog.lock:
        was_down = host_health.down.get(host, False)
        if ok:
            host_health.ssh_failures[host] = 0
            host_health.last_error.pop(host, None)
            if was_down:
                host_health.down[host] = False
                down_since = host_health.down_since.pop(host, now)
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
            failures = host_health.ssh_failures.get(host, 0) + 1
            host_health.ssh_failures[host] = failures
            host_health.last_error[host] = error or "ssh probe failed"
            if not was_down and failures >= HOST_DOWN_THRESHOLD:
                host_health.down[host] = True
                host_health.down_since[host] = now
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
            _log(_LOG_WARN, "watchdog", f"Host alert: {alert_text.splitlines()[0]}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "watchdog", f"Host alert error: {e}")


def _is_host_down(host: str) -> bool:
    """Check if a remote host is marked as down by the watchdog."""
    with watchdog.lock:
        return host_health.down.get(host, False)


def _check_disk_usage(host: str | None = None) -> DiskUsageDict | None:
    """Check disk usage on a host (None = local). Returns {pct, free_gb, total_gb} or None."""
    try:
        r = _remote_run(
            ["df", "-BG", "--output=size,used,avail,pcent", "/"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
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
    except (ValueError, KeyError):
        return None


def _check_disk_usage_macos(host: str) -> DiskUsageDict | None:
    """Check disk usage on macOS host (df output differs from Linux)."""
    try:
        r = _remote_run(
            ["df", "-g", "/"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
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
    except (ValueError, KeyError):
        return None


def _probe_disk_all_hosts(remote_hosts: set[str]) -> None:
    """Probe disk usage on local + remote hosts, alert on threshold breaches."""
    now = _clock.time()
    hosts_to_check = [None] + list(remote_hosts)  # None = local (VPS)

    for host in hosts_to_check:
        if host and _is_host_down(host):
            continue

        host_label = host or "VPS"
        is_mac = host and "mac" in host.lower()
        usage = _check_disk_usage_macos(host) if is_mac else _check_disk_usage(host)

        if usage is None:
            continue

        with watchdog.lock:
            host_health.disk_usage[host_label] = {**usage, "ts": now}

        # Two-tier: critical (95% or <5GB free), warning (85%), ok (below 85%)
        is_critical = usage["pct"] >= DISK_ALERT_THRESHOLD_PCT or usage["free_gb"] < DISK_ALERT_THRESHOLD_GB
        is_warning = usage["pct"] >= DISK_WARN_THRESHOLD_PCT
        prev_level = host_health.disk_alerted.get(host_label, False)  # False, "warning", "critical"

        if is_critical:
            current_level = "critical"
        elif is_warning:
            current_level = "warning"
        else:
            current_level = False

        # Escalation or new alert
        if current_level and current_level != prev_level:
            # Don't re-alert if downgrading from critical→warning (that's partial recovery)
            if current_level == "critical" or not prev_level:
                last_alert = host_health.disk_alert_ts.get(host_label, 0)
                if now - last_alert >= DISK_ALERT_COOLDOWN:
                    if current_level == "critical":
                        alert_text = (
                            f"🔴 Disk space CRITICAL: {host_label}\n"
                            f"Usage: {usage['pct']}% ({usage['free_gb']:.1f}GB free of {usage['total_gb']:.0f}GB)\n"
                            f"Action needed: clean up old files, worktrees, or logs"
                        )
                    else:
                        alert_text = (
                            f"⚠️ Disk space warning: {host_label}\n"
                            f"Usage: {usage['pct']}% ({usage['free_gb']:.1f}GB free of {usage['total_gb']:.0f}GB)"
                        )
                    host_health.disk_alert_ts[host_label] = now
                    host_health.disk_alerted[host_label] = current_level
                    if admin_chat_id:
                        try:
                            transport.send_text(admin_chat_id, alert_text)
                            _log(_LOG_WARN, "watchdog", f"Disk alert: {alert_text.splitlines()[0]}")
                        except (urllib.error.URLError, OSError, TimeoutError) as e:
                            _log(_LOG_ERROR, "watchdog", f"Disk alert error: {e}")
            else:
                # Downgrade from critical to warning — just update state
                host_health.disk_alerted[host_label] = current_level
        elif not current_level and prev_level:
            host_health.disk_alerted[host_label] = False
            if admin_chat_id:
                try:
                    transport.send_text(
                        admin_chat_id,
                        f"✅ Disk space recovered: {host_label} — {usage['pct']}% ({usage['free_gb']:.1f}GB free)"
                    )
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")


def _check_mem_usage(host: str | None = None) -> MemUsageDict | None:
    """Check memory usage on a host (None = local). Returns {pct, used_gb, total_gb, avail_gb, top_procs} or None."""
    try:
        r = _remote_run(
            ["free", "-b"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().splitlines()
        mem_line = None
        for line in lines:
            if line.startswith("Mem:"):
                mem_line = line
                break
        if not mem_line:
            return None
        parts = mem_line.split()
        total = int(parts[1])
        used = int(parts[2])
        avail = int(parts[6]) if len(parts) >= 7 else total - used
        total_gb = total / (1024**3)
        used_gb = used / (1024**3)
        avail_gb = avail / (1024**3)
        pct = int((used / total) * 100) if total > 0 else 0
        top_procs = _get_top_mem_procs(host)
        return {"pct": pct, "used_gb": used_gb, "total_gb": total_gb, "avail_gb": avail_gb, "top_procs": top_procs}
    except (ValueError, KeyError):
        return None


def _check_mem_usage_macos(host: str) -> MemUsageDict | None:
    """Check memory usage on macOS host via vm_stat."""
    try:
        r = _remote_run(
            ["bash", "-c", "sysctl -n hw.memsize && vm_stat"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            return None
        total = int(lines[0])
        page_size = 16384
        free_pages = 0
        inactive_pages = 0
        speculative_pages = 0
        for line in lines[1:]:
            if "page size of" in line:
                try:
                    page_size = int(line.split("page size of")[1].strip().rstrip("."))
                except (ValueError, IndexError) as exc:
                    _log(_LOG_DEBUG, "parse:_check_mem_usage_macos", f"{type(exc).__name__}: {exc}")
            elif "Pages free:" in line:
                free_pages = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages inactive:" in line:
                inactive_pages = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages speculative:" in line:
                speculative_pages = int(line.split(":")[1].strip().rstrip("."))
        avail = (free_pages + inactive_pages + speculative_pages) * page_size
        used = total - avail
        total_gb = total / (1024**3)
        used_gb = used / (1024**3)
        avail_gb = avail / (1024**3)
        pct = int((used / total) * 100) if total > 0 else 0
        top_procs = _get_top_mem_procs(host)
        return {"pct": pct, "used_gb": used_gb, "total_gb": total_gb, "avail_gb": avail_gb, "top_procs": top_procs}
    except (ValueError, KeyError):
        return None


def _get_top_mem_procs(host: str | None = None) -> list[dict[str, Any]]:
    """Get top 5 memory-consuming processes on a host."""
    try:
        r = _remote_run(
            ["ps", "aux", "--sort=-rss"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            return []
        lines = r.stdout.strip().splitlines()
        procs = []
        for line in lines[1:6]:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                rss_kb = int(parts[5])
                procs.append({
                    "pid": parts[1],
                    "rss_gb": round(rss_kb / (1024 * 1024), 1),
                    "pct": parts[3],
                    "cmd": parts[10][:80],
                })
        return procs
    except (ValueError, KeyError):
        return []


def _probe_mem_all_hosts(remote_hosts: set[str]) -> None:
    """Probe memory usage on local + remote hosts, alert on threshold breaches."""
    now = _clock.time()
    hosts_to_check = [None] + list(remote_hosts)

    for host in hosts_to_check:
        if host and _is_host_down(host):
            continue

        host_label = host or "VPS"
        is_mac = host and "mac" in host.lower()
        usage = _check_mem_usage_macos(host) if is_mac else _check_mem_usage(host)

        if usage is None:
            continue

        with watchdog.lock:
            host_health.mem_usage[host_label] = {**usage, "ts": now}

        is_critical = usage["pct"] >= MEM_ALERT_THRESHOLD_PCT or usage["avail_gb"] < MEM_ALERT_THRESHOLD_GB
        was_alerted = host_health.mem_alerted.get(host_label, False)

        if is_critical and not was_alerted:
            last_alert = host_health.mem_alert_ts.get(host_label, 0)
            if now - last_alert >= MEM_ALERT_COOLDOWN:
                top_lines = ""
                for p in usage.get("top_procs", [])[:3]:
                    top_lines += f"\n  {p['pid']} {p['rss_gb']}GB {p['cmd']}"
                alert_text = (
                    f"🧠 Memory critical: {host_label}\n"
                    f"Usage: {usage['pct']}% ({usage['avail_gb']:.1f}GB available of {usage['total_gb']:.0f}GB)"
                )
                if top_lines:
                    alert_text += f"\nTop consumers:{top_lines}"
                host_health.mem_alert_ts[host_label] = now
                host_health.mem_alerted[host_label] = True
                if admin_chat_id:
                    try:
                        transport.send_text(admin_chat_id, alert_text)
                        _log(_LOG_WARN, "watchdog", f"Memory alert: {alert_text.splitlines()[0]}")
                    except (urllib.error.URLError, OSError, TimeoutError) as e:
                        _log(_LOG_ERROR, "watchdog", f"Memory alert error: {e}")
        elif not is_critical and was_alerted:
            host_health.mem_alerted[host_label] = False
            if admin_chat_id:
                try:
                    transport.send_text(
                        admin_chat_id,
                        f"✅ Memory recovered: {host_label} — {usage['pct']}% ({usage['avail_gb']:.1f}GB available)"
                    )
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")


def _check_io_usage(host: str | None = None) -> IoUsageDict | None:
    """Check IO stats on a host (None = local). Returns {iowait_pct, read_iops, write_iops, util_pct} or None."""
    try:
        r = _remote_run(
            ["iostat", "-x", "-d", "1", "2", "-o", "JSON"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode == 0 and r.stdout.strip():
            import json as _json
            data = _json.loads(r.stdout)
            stats = data.get("sysstat", {}).get("hosts", [{}])[0].get("statistics", [])
            if len(stats) >= 2:
                disks = stats[-1].get("disk", [])
                total_r_iops = sum(d.get("r/s", 0) for d in disks)
                total_w_iops = sum(d.get("w/s", 0) for d in disks)
                max_util = max((d.get("util", d.get("%util", 0)) for d in disks), default=0)
                cpu_r = _remote_run(
                    ["bash", "-c", "awk '{print $5}' /proc/stat | head -1"],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK)
                iowait = 0.0
                r2 = _remote_run(
                    ["vmstat", "1", "2"],
                    host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r2.returncode == 0:
                    lines = r2.stdout.strip().splitlines()
                    if len(lines) >= 3:
                        parts = lines[-1].split()
                        if len(parts) >= 16:
                            iowait = float(parts[15])
                return {
                    "iowait_pct": round(iowait, 1),
                    "read_iops": round(total_r_iops),
                    "write_iops": round(total_w_iops),
                    "util_pct": round(max_util, 1),
                }
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
    try:
        r = _remote_run(
            ["vmstat", "1", "2"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 3:
            return None
        parts = lines[-1].split()
        if len(parts) < 16:
            return None
        iowait = float(parts[15])
        bi = int(parts[8])
        bo = int(parts[9])
        return {
            "iowait_pct": round(iowait, 1),
            "read_iops": bi,
            "write_iops": bo,
            "util_pct": 0,
        }
    except (ValueError, KeyError):
        return None


def _check_io_usage_macos(host: str) -> IoUsageDict | None:
    """Check IO stats on macOS host via iostat."""
    try:
        r = _remote_run(
            ["iostat", "-c", "2", "-w", "1"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 3:
            return None
        parts = lines[-1].split()
        if len(parts) < 6:
            return None
        return {
            "iowait_pct": 0,
            "read_iops": int(float(parts[0])),
            "write_iops": int(float(parts[1])),
            "util_pct": 0,
        }
    except (ValueError, KeyError):
        return None


def _probe_io_all_hosts(remote_hosts: set[str]) -> None:
    """Probe IO usage on local + remote hosts, alert on high IO wait."""
    now = _clock.time()
    hosts_to_check = [None] + list(remote_hosts)

    for host in hosts_to_check:
        if host and _is_host_down(host):
            continue

        host_label = host or "VPS"
        is_mac = host and "mac" in host.lower()
        usage = _check_io_usage_macos(host) if is_mac else _check_io_usage(host)

        if usage is None:
            continue

        with watchdog.lock:
            host_health.io_usage[host_label] = {**usage, "ts": now}

        is_critical = usage["iowait_pct"] >= IO_ALERT_IOWAIT_PCT
        was_alerted = host_health.io_alerted.get(host_label, False)

        if is_critical and not was_alerted:
            last_alert = host_health.io_alert_ts.get(host_label, 0)
            if now - last_alert >= IO_ALERT_COOLDOWN:
                alert_text = (
                    f"⚡ IO critical: {host_label}\n"
                    f"IO wait: {usage['iowait_pct']}%\n"
                    f"IOPS: {usage['read_iops']}r + {usage['write_iops']}w"
                )
                if usage["util_pct"]:
                    alert_text += f" | disk util: {usage['util_pct']}%"
                host_health.io_alert_ts[host_label] = now
                host_health.io_alerted[host_label] = True
                if admin_chat_id:
                    try:
                        transport.send_text(admin_chat_id, alert_text)
                        _log(_LOG_WARN, "watchdog", f"IO alert: {alert_text.splitlines()[0]}")
                    except (urllib.error.URLError, OSError, TimeoutError) as e:
                        _log(_LOG_ERROR, "watchdog", f"IO alert error: {e}")
        elif not is_critical and was_alerted:
            host_health.io_alerted[host_label] = False
            if admin_chat_id:
                try:
                    transport.send_text(
                        admin_chat_id,
                        f"✅ IO recovered: {host_label} — iowait {usage['iowait_pct']}%, IOPS {usage['read_iops']}r+{usage['write_iops']}w"
                    )
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")


def _get_cpu_hogs(host: str | None = None, is_mac: bool = False) -> list[dict[str, Any]]:
    """Get processes using >CPU_HOG_THRESHOLD_PCT CPU on a host. Returns [{pid, cpu, etime_min, cmd}]."""
    try:
        if is_mac:
            cmd = ["ps", "-eo", "pid,pcpu,etime,comm", "-r"]
        else:
            cmd = ["ps", "-eo", "pid,pcpu,etime,comm", "--sort=-pcpu"]
        r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            return []
        hogs = []
        for line in r.stdout.strip().splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                cpu = float(parts[1])
            except (ValueError, IndexError):
                continue
            if cpu < CPU_HOG_THRESHOLD_PCT:
                break  # sorted by CPU desc, no point continuing
            # Parse etime: [[dd-]hh:]mm:ss
            etime_str = parts[2]
            etime_min = _parse_etime(etime_str)
            if etime_min is None:
                continue
            cmd_name = parts[3][:80] if len(parts) > 3 else "?"
            hogs.append({"pid": pid, "cpu": cpu, "etime_min": etime_min, "cmd": cmd_name})
        return hogs
    except (ValueError, KeyError):
        return []


def _parse_etime(etime: str) -> int | None:
    """Parse ps etime format [[dd-]hh:]mm:ss into total minutes."""
    try:
        days = 0
        if "-" in etime:
            day_part, rest = etime.split("-", 1)
            days = int(day_part)
            etime = rest
        parts = etime.split(":")
        if len(parts) == 3:
            hours, mins, _secs = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            hours, mins, _secs = 0, int(parts[0]), int(parts[1])
        else:
            return None
        return days * 24 * 60 + hours * 60 + mins
    except (ValueError, IndexError):
        return None


def _probe_cpu_hogs(remote_hosts: set[str]) -> None:
    """Detect runaway processes: >90% CPU for >1 hour. Alert admin."""
    now = _clock.time()
    hosts_to_check = [None] + list(remote_hosts)

    for host in hosts_to_check:
        if host and _is_host_down(host):
            continue

        host_label = host or "VPS"
        is_mac = host and "mac" in host.lower()
        hogs = _get_cpu_hogs(host, is_mac=is_mac)

        # Filter: only processes running > CPU_HOG_DURATION_MIN minutes
        real_hogs = [h for h in hogs if h["etime_min"] >= CPU_HOG_DURATION_MIN]

        with watchdog.lock:
            host_health.cpu_hogs[host_label] = real_hogs

        if real_hogs:
            last_alert = host_health.cpu_hog_alert_ts.get(host_label, 0)
            if now - last_alert >= CPU_HOG_ALERT_COOLDOWN:
                lines = []
                for h in real_hogs[:5]:
                    elapsed = f"{h['etime_min'] // 60}h{h['etime_min'] % 60}m" if h["etime_min"] >= 60 else f"{h['etime_min']}m"
                    lines.append(f"  PID {h['pid']}: {h['cpu']}% CPU for {elapsed} — {h['cmd']}")
                alert_text = (
                    f"🔥 Runaway process{'es' if len(real_hogs) > 1 else ''} on {host_label}:\n"
                    + "\n".join(lines)
                )
                host_health.cpu_hog_alert_ts[host_label] = now
                if admin_chat_id:
                    try:
                        transport.send_text(admin_chat_id, alert_text)
                        _log(_LOG_WARN, "watchdog", f"CPU hog alert: {host_label} ({len(real_hogs)} process{'es' if len(real_hogs) > 1 else ''})")
                    except (urllib.error.URLError, OSError, TimeoutError) as e:
                        _log(_LOG_ERROR, "watchdog", f"CPU hog alert error: {e}")


def _probe_worktree_sizes(remote_hosts: set[str]) -> None:
    """Check worktree directories and alert when total exceeds threshold."""
    now = _clock.time()
    hosts_to_check = [None] + list(remote_hosts)

    for host in hosts_to_check:
        if host and _is_host_down(host):
            continue

        host_label = host or "VPS"
        is_mac = host and "mac" in host.lower()

        # Find worktree directories: common patterns
        # - ~/.claude/worktrees/
        # - ~/omi-*/.claude/worktrees/
        # - ~/*/worktrees/  (any project)
        try:
            if is_mac:
                # macOS: check common locations, du -sk for KB
                cmd = ["bash", "-c",
                       "find $HOME -maxdepth 4 -type d -name worktrees 2>/dev/null | "
                       "while read d; do du -sk \"$d\" 2>/dev/null; done"]
            else:
                # Linux: du -sb for bytes
                cmd = ["bash", "-c",
                       "find $HOME -maxdepth 4 -type d -name worktrees 2>/dev/null | "
                       "while read d; do du -sb \"$d\" 2>/dev/null; done"]
            r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
            if r.returncode != 0 or not r.stdout.strip():
                continue

            items = []
            total_bytes = 0
            for line in r.stdout.strip().splitlines():
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                try:
                    size_val = int(parts[0])
                    path = parts[1]
                except (ValueError, IndexError):
                    continue
                # macOS du -sk gives KB, Linux du -sb gives bytes
                size_bytes = size_val * 1024 if is_mac else size_val
                size_gb = size_bytes / (1024**3)
                total_bytes += size_bytes
                items.append({"path": path, "size_gb": round(size_gb, 1)})

            total_gb = total_bytes / (1024**3)
            with watchdog.lock:
                host_health.worktree_usage[host_label] = {"total_gb": round(total_gb, 1), "items": items, "ts": now}

            was_alerted = host_health.worktree_alerted.get(host_label, False)

            if total_gb >= WORKTREE_ALERT_THRESHOLD_GB and not was_alerted:
                last_alert = host_health.worktree_alert_ts.get(host_label, 0)
                if now - last_alert >= WORKTREE_ALERT_COOLDOWN:
                    top_items = sorted(items, key=lambda x: x["size_gb"], reverse=True)[:5]
                    lines = [f"  {it['size_gb']}GB — {it['path']}" for it in top_items]
                    alert_text = (
                        f"📁 Worktree bloat on {host_label}: {total_gb:.1f}GB total (threshold: {WORKTREE_ALERT_THRESHOLD_GB}GB)\n"
                        f"Top directories:\n" + "\n".join(lines)
                    )
                    host_health.worktree_alert_ts[host_label] = now
                    host_health.worktree_alerted[host_label] = True
                    if admin_chat_id:
                        try:
                            transport.send_text(admin_chat_id, alert_text)
                            _log(_LOG_WARN, "watchdog", f"Worktree alert: {host_label} {total_gb:.1f}GB")
                        except (urllib.error.URLError, OSError, TimeoutError) as e:
                            _log(_LOG_ERROR, "watchdog", f"Worktree alert error: {e}")
            elif total_gb < WORKTREE_ALERT_THRESHOLD_GB and was_alerted:
                host_health.worktree_alerted[host_label] = False
                if admin_chat_id:
                    try:
                        transport.send_text(
                            admin_chat_id,
                            f"✅ Worktree size recovered: {host_label} — {total_gb:.1f}GB (below {WORKTREE_ALERT_THRESHOLD_GB}GB)"
                        )
                    except (urllib.error.URLError, OSError, TimeoutError) as exc:
                        _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "watchdog", f"Worktree check error for {host_label}: {e}")


def _probe_tailscale() -> None:
    """Check Tailscale connectivity, alert on disconnect/recovery."""
    now = _clock.time()
    try:
        r = _subprocess_runner.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode == 0:
            import json as _json
            data = _json.loads(r.stdout)
            is_up = data.get("BackendState") == "Running"
        else:
            is_up = False
    except (subprocess.SubprocessError, OSError):
        is_up = False

    if not is_up and not host_health.tailscale_down:
        if now - host_health.tailscale_alert_ts >= INFRA_ALERT_COOLDOWN:
            host_health.tailscale_down = True
            host_health.tailscale_alert_ts = now
            if admin_chat_id:
                try:
                    transport.send_text(
                        admin_chat_id,
                        "🚨 Tailscale is DOWN on VPS — 100.125.36.102 unreachable from external network.\n"
                        "Run: sudo tailscale up"
                    )
                    _log(_LOG_INFO, "watchdog", "[watchdog] Tailscale DOWN alert sent")
                except (urllib.error.URLError, OSError, TimeoutError) as e:
                    _log(_LOG_ERROR, "watchdog", f"Tailscale alert error: {e}")
    elif is_up and host_health.tailscale_down:
        host_health.tailscale_down = False
        if admin_chat_id:
            try:
                transport.send_text(
                    admin_chat_id,
                    "✅ Tailscale recovered — VPS reachable at 100.125.36.102"
                )
                _log(_LOG_INFO, "watchdog", "[watchdog] Tailscale recovery alert sent")
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")


# (last_resolved_ts moved to watchdog.last_resolved_ts — added to WorkerWatchdogState)

def _send_resolved_alert(name: str, new_state: str) -> None:
    """Send a 'resolved' alert to admin when a worker recovers from a problem state."""
    if admin_chat_id is None:
        return

    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    with watchdog.lock:
        prev_state = watchdog.prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states:
        return

    # Suppress if worker was recently restarted (cmd_restart sends its own confirmation)
    restart_ts = watchdog.recent_restarts.get(name)
    if restart_ts and _clock.time() - restart_ts < 30:
        return

    # Cooldown: don't spam "back to normal" for flapping workers
    now = _clock.time()
    last_resolved = watchdog.last_resolved_ts.get(name, 0)
    if now - last_resolved < 180:
        return

    watchdog.last_resolved_ts[name] = now

    # Edit the old alert to show resolved
    with watchdog.lock:
        alert_info = watchdog.alert_msg_ids.pop(name, None)
    if alert_info:
        old_msg_id, old_text = alert_info
        resolved_text = f"✅ {name} resolved (was: {old_text.splitlines()[0]})"
        try:
            transport.edit_message(admin_chat_id, old_msg_id, resolved_text)
            _log(_LOG_WARN, "watchdog", f"Edited alert for {name} -> resolved")
            return
        except (urllib.error.URLError, OSError, TimeoutError):
            pass  # intentional no-op: edit failed, fall through to send new message

    text = f"✅ {name} is back to normal."
    try:
        transport.send_text(admin_chat_id, text)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "watchdog", f"Watchdog resolved alert error: {e}")


def _handle_watchdog_transition(
    name: str,
    state: str,
    reason: str,
    since: float,
    now: float | None = None,
) -> None:
    """Process a watchdog state transition: alert on bad states, clear on recovery."""
    if now is None:
        now = _clock.time()

    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with watchdog.lock:
        prev_state = watchdog.prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state

    # Host-level alerts are sent by _record_host_probe; suppress per-worker spam
    if state == "HOST_OFFLINE":
        with watchdog.lock:
            watchdog.prev_worker_states[name] = state
        return

    # Suppress alerts for workers being teleported (teleport takes 30-60s,
    # during which the worker appears DEAD/OFFLINE but is expected)
    teleport_state_file = SESSIONS_DIR / name / "teleport_state"
    if state in {"OFFLINE", "DEAD", "EXITED"} and teleport_state_file.exists():
        with watchdog.lock:
            watchdog.prev_worker_states[name] = state
        return

    def eligible_for_alert() -> bool:
        """Check whether enough time has passed to send another alert."""
        if state in {"OFFLINE", "DEAD", "EXITED"}:
            return since is not None and (now - since) >= START_GRACE
        return True

    GOOD_PROBE_THRESHOLD = 3
    BAD_PROBE_THRESHOLD = 3

    is_remote = bool(get_worker_host(name))

    if state in bad_states:
        with watchdog.lock:
            watchdog.consecutive_good_probes[name] = 0

        if is_remote and state in {"OFFLINE", "DEAD"}:
            with watchdog.lock:
                watchdog.consecutive_bad_probes[name] = watchdog.consecutive_bad_probes.get(name, 0) + 1
                bad_count = watchdog.consecutive_bad_probes[name]
            if bad_count < BAD_PROBE_THRESHOLD:
                return

        if state_changed or prev_state is None:
            if eligible_for_alert():
                _log(_LOG_WARN, "watchdog", f"State change {name}: {prev_state} -> {state} ({reason}), sending alert")
                _send_watchdog_alert(name, state, reason)
        elif state in {"OFFLINE", "DEAD", "EXITED"} and eligible_for_alert():
            _send_watchdog_alert(name, state, reason)
        with watchdog.lock:
            watchdog.prev_worker_states[name] = state
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
    """Update worker state and preserve since for unchanged states."""
    with watchdog.lock:
        prev = watchdog.worker_states.get(name)
        if prev and prev[0] == state:
            since = prev[2]
        else:
            since = now
        watchdog.worker_states[name] = (state, reason, since)
    return since


def watchdog_loop() -> None:
    """Main watchdog loop — periodically probes all workers and fires alerts."""
    _disk_check_counter = 0
    while True:
        try:
            now = _clock.time()
            registered = get_registered_sessions()
            pane_pids = _tmux_pane_pids()
            registered_names = set(registered.keys())

            probe_failed = bool(registered_names) and not pane_pids
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

        except (subprocess.SubprocessError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "watchdog", f"Watchdog error: {e}")

        _clock.sleep(WATCHDOG_INTERVAL)


def _watchdog_update_probe_failures(registered_names: set[str], probe_failed: bool) -> None:
    """Update consecutive probe failure counters for all registered workers."""
    if probe_failed:
        for name in registered_names:
            watchdog.consecutive_probe_failures[name] = watchdog.consecutive_probe_failures.get(name, 0) + 1
    else:
        for name in registered_names:
            watchdog.consecutive_probe_failures[name] = 0


def _watchdog_probe_remote_hosts(
    registered: dict[str, TmuxSessionDict]
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str], set[str]]:
    """Probe remote hosts for tmux sessions in bulk.

    Returns (remote_workers, remote_pane_pids, failed_hosts).
    """
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
                    if len(parts) >= 2 and parts[1].isdigit():
                        remote_pane_pids[parts[0]] = parts[1]
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
    """Collect claude PIDs, tmux presence, and backend info for all workers.

    Returns (claude_pids, tmux_present, backend_info).
    """
    claude_pids: dict[str, str] = {}
    tmux_present: dict[str, bool] = {}
    backend_info: dict[str, Any] = {}

    for name, session in registered.items():
        backend_name = get_worker_backend(name, session)
        backend = get_backend(backend_name)
        backend_info[name] = backend
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
        host = get_worker_host(name)
        pane_pid = remote_pane_pids.get(tmux_name) if host else pane_pids.get(tmux_name)
        tmux_exists = bool(pane_pid)
        tmux_present[name] = tmux_exists

        if not tmux_exists:
            continue

        if backend.is_interactive:
            claude_pid = _get_claude_pid(pane_pid, host=host)
            if claude_pid:
                claude_pids[name] = claude_pid
                with watchdog.lock:
                    watchdog.last_seen_claude[name] = now
            else:
                with watchdog.lock:
                    if name not in watchdog.last_seen_claude:
                        watchdog.last_seen_claude[name] = now

    return claude_pids, tmux_present, backend_info


def _watchdog_gather_cpu_stats(claude_pids: dict[str, str]) -> dict[str, dict[str, float]]:
    """Gather CPU stats for all claude PIDs, grouped by host."""
    pids_by_host: dict[str | None, list[str]] = {}
    for name, pid in claude_pids.items():
        host = get_worker_host(name)
        pids_by_host.setdefault(host, []).append(pid)
    stats: dict[str, dict[str, float]] = {}
    for host, pids in pids_by_host.items():
        stats.update(_ps_stats(pids, host=host))
    return stats


def _watchdog_evaluate_workers(
    registered: dict[str, TmuxSessionDict],
    tmux_present: dict[str, bool],
    claude_pids: dict[str, str],
    backend_info: dict[str, Backend],
    stats: dict[str, dict[str, float]],
    probe_failed: bool,
    failed_hosts: set[str],
    now: float
) -> None:
    """Evaluate state for each registered worker and handle transitions."""
    for name, session in registered.items():
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
        tmux_exists = tmux_present.get(name, False)

        # Registry-only worker (tmux gone): mark EXITED directly
        if not tmux_exists and "tmux" not in session:
            since = _record_worker_state(name, "EXITED", "session gone", now)
            _handle_watchdog_transition(name, "EXITED", "session gone", since, now=now)
            continue

        if probe_failed and not tmux_exists and watchdog.consecutive_probe_failures.get(name, 0) < 3:
            continue

        # Mark workers on down hosts as HOST_OFFLINE
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
            entry = processes.adapter_pids.get(name)
            if entry:
                proc, _stderr = entry
                adapter_alive = proc.poll() is None

        host = get_worker_host(name)
        claude_pid = claude_pids.get(name) if is_interactive else None
        cpu = 0.0
        if claude_pid and claude_pid in stats:
            cpu = stats[claude_pid].get("cpu", 0.0)

        children_total = _child_count(claude_pid, host=host) if claude_pid else 0
        children = _watchdog_compute_children(name, children_total, is_interactive, claude_pid, now)

        if children > 0:
            with watchdog.lock:
                watchdog.last_child_ts[name] = now

        _watchdog_track_activity(name, children, cpu, now)

        pending_ts = _pending_timestamp(name)
        pending = pending_ts is not None
        with watchdog.lock:
            last_activity = watchdog.last_activity_ts.get(name, 0.0)

        if pending_ts:
            effective_start = max(pending_ts, last_activity) if last_activity > pending_ts else pending_ts
            pending_age = now - effective_start
        else:
            pending_age = 0.0
        with watchdog.lock:
            last_child_ts = watchdog.last_child_ts.get(name, 0.0)
            last_hook_ts = watchdog.last_hook_ts.get(name)
            last_seen_claude = watchdog.last_seen_claude.get(name)
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
        worker_state, reason = compute_state(**state_args)

        worker_state, reason = _watchdog_refine_state(
            name, tmux_name, worker_state, reason, state_args,
            is_interactive, pending, pending_age, host, now)

        since = _record_worker_state(name, worker_state, reason, now)
        _handle_watchdog_transition(name, worker_state, reason, since, now=now)


def _watchdog_compute_children(name: str, children_total: int,
                                is_interactive: bool, claude_pid: str | None,
                                now: float) -> int:
    """Apply dynamic baseline to child count (MCP servers are persistent)."""
    pending_ts = _pending_timestamp(name)
    pending = pending_ts is not None
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
    """Track activity based on child count increases and CPU usage."""
    with watchdog.lock:
        prev_children = watchdog.prev_children.get(name)
        activity_increased = (prev_children is not None and children > prev_children)
        if activity_increased or cpu >= CPU_ACTIVE:
            watchdog.last_activity_ts[name] = now
        watchdog.prev_children[name] = children


def _watchdog_refine_state(
    name: str, tmux_name: str,
    worker_state: str, reason: str,
    state_args: dict[str, Any],
    is_interactive: bool, pending: bool, pending_age: float,
    host: str | None, now: float
) -> tuple[str, str]:
    """Refine STUCK/READY states with streak tracking and interactive prompt detection."""
    if worker_state == "STUCK":
        watchdog.idle_streak[name] = watchdog.idle_streak.get(name, 0) + 1
        streak = watchdog.idle_streak[name]
        if streak < IDLE_STREAK_STUCK:
            worker_state = "WAITING"
        else:
            # Auto-clear stale pending if worker is at idle prompt
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
            worker_state, reason = compute_state(**state_args, poisoned_reason=poisoned_reason)
        reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    elif worker_state == "POISONED":
        streak = watchdog.idle_streak.get(name, 0)
        if streak:
            reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    else:
        watchdog.idle_streak[name] = 0

    # Detect WAITING_INPUT: worker is READY but at interactive prompt
    if worker_state == "READY" and is_interactive:
        pane_text = _capture_pane_text(tmux_name, lines=30, host=host)
        if pane_text:
            pane_lines = pane_text.splitlines()
            details = _extract_question_details(pane_lines)
            if details:
                with watchdog.lock:
                    watchdog.waiting_input_details[name] = details
                worker_state = "WAITING_INPUT"
                header = details.get("header", "")
                reason = f"question={header}" if header else "interactive prompt"

    return worker_state, reason


def _watchdog_cleanup_stale(registered_names: set[str]) -> None:
    """Remove watchdog state for workers no longer in the registry."""
    with watchdog.lock:
        stale_dicts = [
            watchdog.worker_states, watchdog.last_child_ts,
            watchdog.last_seen_claude, watchdog.last_hook_ts,
            watchdog.prev_worker_states, watchdog.last_alert_ts,
            watchdog.idle_streak, watchdog.idle_child_baseline,
            watchdog.prev_children, watchdog.last_activity_ts,
        ]
        for d in stale_dicts:
            for name in list(d.keys()):
                if name not in registered_names:
                    d.pop(name, None)
    for name in list(watchdog.consecutive_probe_failures.keys()):
        if name not in registered_names:
            watchdog.consecutive_probe_failures.pop(name, None)


def _watchdog_resource_checks(remote_hosts: set[str]) -> None:
    """Run periodic resource checks (disk, memory, IO, CPU, worktrees, Tailscale)."""
    checks = [
        ("Disk", lambda: _probe_disk_all_hosts(remote_hosts)),
        ("Memory", lambda: _probe_mem_all_hosts(remote_hosts)),
        ("IO", lambda: _probe_io_all_hosts(remote_hosts)),
        ("CPU hog", lambda: _probe_cpu_hogs(remote_hosts)),
        ("Worktree", lambda: _probe_worktree_sizes(remote_hosts)),
        ("Tailscale", lambda: _probe_tailscale()),
    ]
    for label, check_fn in checks:
        try:
            check_fn()
        except (subprocess.SubprocessError, OSError) as e:
            _log(_LOG_ERROR, "watchdog", f"{label} check error: {e}")


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


def _format_watchdog_status(name: str,
                            pending_lookup: dict[str, Any] | None = None,
                            state_snapshot: dict[str, Any] | None = None) -> str:
    """Format a human-readable watchdog status line for one worker."""
    if pending_lookup is None:
        pending_lookup = is_pending

    if state_snapshot is None:
        with watchdog.lock:
            entry = watchdog.worker_states.get(name)
    else:
        entry = state_snapshot.get(name)
    if not entry:
        return "Working" if pending_lookup(name) else "Ready"

    state, _reason, since = entry
    now = _clock.time()

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
        # Use age from reason (derived from pending file timestamp on disk,
        # survives bridge restarts) rather than since (resets on restart).
        age_match = re.search(r"age=(\d+)s", _reason) if _reason else None
        if age_match:
            minutes = int(age_match.group(1)) // 60
        else:
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
    registered: dict[str, Any],
    active: str | None,
    pending_lookup: dict[str, bool] | None = None,
    worker_live: dict[str, Any] | None = None
) -> list[str]:
    """Format /team response lines with attention, activity, and context."""
    if pending_lookup is None:
        pending_lookup = is_pending
    if worker_live is None:
        worker_live = {}

    with watchdog.lock:
        state_snapshot = dict(watchdog.worker_states)

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


def _wait_for_restart_ready(tmux_name: str, backend_name: str, timeout: float = 45.0, host: str | None = None) -> bool:
    """Wait until restarted worker is actually back at the prompt."""
    backend = get_backend(backend_name)
    if not backend.is_interactive:
        return tmux_exists(tmux_name, host=host)

    deadline = _clock.time() + timeout
    while _clock.time() < deadline:
        if not tmux_exists(tmux_name, host=host):
            return False
        activity, _, _ = _read_tmux_activity(tmux_name, host=host)
        if activity == "Idle at prompt":
            return True
        _clock.sleep(DELAY_RETRY)
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

    return {
        "header": header,
        "options": options,
        "selected_num": selected_num,
    }


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


def format_progress_lines(
    name: str,
    pending: bool,
    backend: str,
    online: bool,
    ready: bool,
    mode: str,
    resume_line: str | None = None,
    continuity_line: str | None = None,
    needs_attention: str | None = None,
    activity: str | None = None,
    context_pct: str | None = None,
    question_details: dict[str, Any] | None = None
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


def get_worker_backend(name: str, session: dict[str, Any] | None = None) -> str:
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
    """Send a message to a gRPC-connected worker. Returns True on success."""
    if grpc_server is None:
        return False

    try:
        if not grpc_server.is_worker_connected(name):
            return False
        if grpc_server.send_to_worker(name, message, from_name):
            return True
        _log(_LOG_WARN, "bridge", f"gRPC send failed for '{name}', falling back to tmux backend")
    except subprocess.SubprocessError as e:
        _log(_LOG_ERROR, "bridge", f"gRPC send error for '{name}', falling back to tmux backend: {e}")
    return False


def _send_to_callback_worker(name: str, message: str, from_name: str = "manager", session: TmuxSessionDict | None = None) -> bool:
    """Send a message to a callback-URL worker (HTTP POST). Returns True on success."""
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
        with _urlopen(req, timeout=TIMEOUT_HTTP_API) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_WARN, "bridge", f"Callback send failed for '{name}' at {msg_url}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CORE: WorkerManager
# ─────────────────────────────────────────────────────────────────────────────

class WorkerManager:
    """Manages worker lifecycle: hire, end, restart, and tmux session orchestration.

    Tracks active workers via tmux session discovery and the persistent
    registry file. Handles session creation, welcome message injection,
    backend selection, and dead-worker restart logic.
    """

    def __init__(self, sessions_dir: Path, tmux_prefix: str,
                 runner: SubprocessRunner | None = None,
                 clock: Clock | None = None) -> None:
        """Initialize worker manager with tmux prefix, session paths, and DI seams.

        Args:
            sessions_dir: Root directory for per-worker session files.
            tmux_prefix: Prefix for tmux session names (e.g. 'claude-prod-').
            runner: Subprocess runner for test injection (defaults to real subprocess).
            clock: Clock for test injection (defaults to real wall-clock time).
        """
        self.sessions_dir = sessions_dir
        self.tmux_prefix = tmux_prefix
        self._runner: SubprocessRunner = runner or _subprocess_runner
        self._clock: Clock = clock or _clock

    def _sync_paths(self) -> None:
        """Sync instance paths with current module globals (for runtime reconfiguration)."""
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
            _log(_LOG_WARN, "bridge", f"Ignoring invalid startup cwd for {name}: {candidate}")

        fallback = normalize_cwd(fallback_cwd)
        if fallback and os.path.isdir(fallback):
            return fallback
        return ""

    def _get_tmux_pane_cwd(self, tmux_name: str, host: str | None = None) -> str:
        """Read current pane cwd for a tmux session."""
        result = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_path}"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _cd_tmux_to_cwd(self, tmux_name: str, cwd: str) -> None:
        """Change tmux shell cwd before starting backend process."""
        if not cwd:
            return
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"cd {shlex.quote(cwd)}", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_SHORT)

    def scan_tmux_sessions(self) -> dict[str, TmuxSessionDict]:
        """Scan tmux for claude-* sessions (local + remote machines)."""
        self._sync_paths()
        registered = {}

        # Scan local tmux
        try:
            result = self._runner.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix):]
                        backend = normalize_backend(get_tmux_env_value(session_name, "WORKER_BACKEND"))
                        registered[name] = {"tmux": session_name, "backend": backend}
        except (subprocess.SubprocessError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Error scanning local tmux: {e}")

        # Scan remote machines for tmux sessions
        try:
            machines = get_machine_catalog()
        except (subprocess.SubprocessError, OSError):
            machines = {}
        for machine in machines.values():
            if machine.is_local or not machine.ssh_target:
                continue
            if machine.role not in ("worker-host", ""):
                continue
            try:
                r = _remote_run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    host=machine.ssh_target,
                    capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD
                )
                if r.returncode != 0:
                    continue
                for line in r.stdout.strip().split("\n"):
                    if not line:
                        continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix):]
                        if name not in registered:
                            registered[name] = {
                                "tmux": session_name,
                                "backend": DEFAULT_BACKEND,
                                "host": machine.ssh_target,
                            }
                            _registry_add(name, DEFAULT_BACKEND, host=machine.ssh_target)
            except (subprocess.SubprocessError, KeyError) as e:
                _log(_LOG_ERROR, "bridge", f"Error scanning tmux on {machine.ssh_target}: {e}")

        return registered

    # TTL cache for get_registered_sessions: avoids SSH calls on every message.
    # When a remote host is down, each scan_tmux_sessions() blocks for 10s per
    # host on SSH timeout.  With 4+ calls per message routing path, this delays
    # routing by 40+ seconds and effectively drops messages.
    _sessions_cache = None
    _sessions_cache_ts = 0
    _sessions_cache_lock = threading.Lock()
    _SESSIONS_CACHE_TTL = 15  # seconds

    def invalidate_sessions_cache(self) -> None:
        """Force next get_registered_sessions() to re-scan.  Call after hire/fire/restart."""
        with self._sessions_cache_lock:
            self._sessions_cache = None
            self._sessions_cache_ts = 0

    def get_registered_sessions(self, registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
        """Get registered sessions from tmux. Cached for _SESSIONS_CACHE_TTL seconds."""
        self._sync_paths()
        if registered is None:
            now = self._clock.time()
            with self._sessions_cache_lock:
                if self._sessions_cache is not None and (now - self._sessions_cache_ts) < self._SESSIONS_CACHE_TTL:
                    # Return a shallow copy so callers can mutate without poisoning cache
                    return dict(self._sessions_cache)
            # Cache miss or expired — do the full scan (outside the lock)
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

        # Update cache
        with self._sessions_cache_lock:
            self._sessions_cache = dict(registered)
            self._sessions_cache_ts = self._clock.time()

        return registered

    def is_online(self, name: str, session: TmuxSessionDict | None = None) -> bool:
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
                if not tmux_exists(tmux_name, host=host, timeout=TIMEOUT_TMUX_CHECK):
                    return False
                if backend.is_interactive:
                    try:
                        return is_claude_running(tmux_name, host=host)
                    except (subprocess.SubprocessError, OSError):
                        return True  # Probe failure on interactive check — assume online
                return True
            except (subprocess.SubprocessError, OSError):
                return True  # SSH failure — assume still online (broad catch intentional)

        return backend.is_online(tmux_name)

    def send(self, name: str, message: str, chat_id: int | None = None, session: TmuxSessionDict | None = None) -> bool:
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

    def get_workers(self, caller_from: str | None = None) -> list[dict[str, Any]]:
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
                    # Non-interactive remote workers use SSH-based adapter spawning
                    # Worker-to-worker pipes don't work remotely, but bridge send does
                    workers.append({
                        "name": name,
                        "machine": peer_host,
                        "protocol": "adapter",
                        "address": f"{peer_host}:{info.get('tmux', '')}",
                        "note": f"Non-interactive ({backend_name}) on {peer_host}. Use @{name} from Telegram or bridge API.",
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

    def _wrap_for_caller(self, cmd: str, peer_host: str | None, caller_host: str | None) -> str:
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

    def _build_welcome(self, name: str, backend_obj: Backend) -> str:
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
            _log(_LOG_INFO, "checkin", f"Checkin note included for {name}")

        return welcome

    def hire(self, name: str, backend: str = DEFAULT_BACKEND, chat_id: int | None = None) -> tuple[bool, str | None]:
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
        result = self._runner.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env, timeout=TIMEOUT_REMOTE_CMD
        )
        if result.returncode != 0:
            return False, "Could not start the worker workspace"
        self._runner.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True, timeout=TIMEOUT_TMUX_SEND)

        self._clock.sleep(DELAY_RETRY)
        startup_cwd = self._get_startup_cwd(name)
        if startup_cwd:
            self._cd_tmux_to_cwd(tmux_name, startup_cwd)

        # After tmux new-session succeeds, capture the pane's cwd
        pane_cwd = self._get_tmux_pane_cwd(tmux_name) or startup_cwd
        if pane_cwd:
            save_claude_session_cwd(name, pane_cwd)

        export_hook_env(tmux_name, backend)
        self._clock.sleep(DELAY_TMUX_SEND)

        # Inject tmux env vars then unset CLAUDECODE (prevents nested-session error)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)

        ensure_session_dir(name)
        if chat_id:
            chat_id_file = get_chat_id_file(name)
            chat_id_file.write_text(str(chat_id))
            chat_id_file.chmod(0o600)
        if not backend_obj.is_interactive:
            ensure_worker_pipe(name)

        if not backend_obj.is_interactive:
            backend_file = self.sessions_dir / name / "backend"
            backend_file.write_text(backend)

        if SANDBOX_ENABLED and backend_obj.is_interactive:
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
            _log(_LOG_INFO, "worker", f"Started worker '{name}' in sandbox mode")
        else:
            start_cmd = f'unset CLAUDECODE && {backend_obj.start_cmd()}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
            if backend_obj.is_interactive:
                self._clock.sleep(DELAY_STARTUP_LONG)
                self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)

        if backend_obj.is_interactive:
            self._clock.sleep(DELAY_RESPONSE_GAP if not SANDBOX_ENABLED else DELAY_CLAUDE_LOAD + 1)

        welcome = self._build_welcome(name, backend_obj)
        if not backend_obj.is_interactive:
            if chat_id:
                set_pending(name, chat_id)
            # Echo welcome to tmux (visible for debugging) but don't call backend
            # to avoid triggering a codex API call on hire
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        else:
            self.send(name, welcome)

        state["active"] = name
        save_last_active(name)
        _registry_add(name, backend, chat_id)
        _reset_learning_reminder(name)

        if not backend_obj.is_interactive:
            _log(_LOG_INFO, "worker", f"Created {backend} worker '{name}' (non-interactive mode)")

        self.invalidate_sessions_cache()
        return True, None

    def end(self, name: str) -> tuple[bool, str | None]:
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
            except OSError as e:
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
        self.invalidate_sessions_cache()

        if state["active"] == name:
            state["active"] = None
            self.get_registered_sessions()

        return True, None

    def restart(self, name: str, mode: str = "relaunch") -> tuple[bool, str | None]:
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

        resume_id, startup_cwd = self._prepare_restart_state(name, mode)

        # Clear hook failure signal on clean restart
        if mode != "resume":
            _clear_hook_failures(name)

        # Clean non-interactive state or stop existing Claude
        session_dir = self.sessions_dir / name
        if not backend.is_interactive:
            session_dir.mkdir(parents=True, exist_ok=True)
            ensure_worker_pipe(name)
            clear_pending(name)
        elif is_claude_running(tmux_name):
            self._stop_running_claude(name, tmux_name)

        # Kill any stray child process (e.g. SSH, vim) before sending start command
        if backend.is_interactive and not is_claude_running(tmux_name):
            self._kill_stray_children(name, tmux_name)

        export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)

        self._send_start_command(name, tmux_name, backend, resume_id, startup_cwd)

        # Wait for Claude to actually start before sending welcome
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = self._wait_for_startup(name, tmux_name, backend, resume_id, startup_cwd)
            if started:
                self.send(name, welcome)
            else:
                _log(_LOG_WARN, "restart", f"{name}: Claude did not start within 10s, skipping welcome")
        else:
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)

        _reset_learning_reminder(name)
        self.invalidate_sessions_cache()
        return True, None

    def _prepare_restart_state(self, name: str, mode: str) -> tuple[str, str]:
        """Prepare resume ID and startup CWD for a restart.

        For resume mode, preserves session IDs. For relaunch, clears them.
        Returns (resume_id, startup_cwd).
        """
        resume_id = ""
        resume_cwd = ""
        session_dir = self.sessions_dir / name
        if mode == "resume":
            resume_id = (get_claude_session_id(name, authoritative=False) or
                         get_claude_session_id(name, authoritative=True))
            resume_cwd = get_claude_session_cwd(name)
        else:
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd:
            save_claude_session_cwd(name, startup_cwd)
            _ensure_workspace_trusted(startup_cwd)
        return resume_id, startup_cwd

    def _stop_running_claude(self, name: str, tmux_name: str) -> None:
        """Gracefully stop a running Claude instance in a tmux session.

        Sends C-c and /exit, then force-kills if still running after 5s.
        """
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_STARTUP)
        # If still running, force kill
        if is_claude_running(tmux_name):
            pane_pid = _tmux_pane_pids().get(tmux_name)
            if pane_pid:
                claude_pid = _get_claude_pid(pane_pid)
                if claude_pid:
                    self._runner.run(["kill", claude_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        # Poll until Claude has actually exited (fixed sleep races with slow exits)
        for _ in range(20):
            if not is_claude_running(tmux_name):
                break
            self._clock.sleep(DELAY_SHORT)
        else:
            _log(_LOG_WARN, "restart", f"{name}: Claude still running after 5s kill wait")

    def _kill_stray_children(self, name: str, tmux_name: str) -> None:
        """Kill stray child processes (SSH, vim, etc.) in a tmux pane."""
        pane_pids = _tmux_pane_pids()
        pane_pid = pane_pids.get(tmux_name)
        if pane_pid:
            stray = self._runner.run(
                ["pgrep", "-P", str(pane_pid)],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
            )
            if stray.returncode == 0:
                for child_pid in stray.stdout.strip().splitlines():
                    child_pid = child_pid.strip()
                    if child_pid and child_pid.isdigit():
                        _log(_LOG_INFO, "restart", f"{name}: killing stray child pid {child_pid}")
                        self._runner.run(["kill", child_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
                self._clock.sleep(DELAY_RETRY)

    def _send_start_command(self, name: str, tmux_name: str, backend: Backend,
                            resume_id: str, startup_cwd: str) -> None:
        """Inject tmux env vars and send the backend start command."""
        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)

        if SANDBOX_ENABLED and backend.is_interactive:
            stop_docker_container(name)
            self._clock.sleep(DELAY_RETRY)
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        else:
            start_cmd = backend.start_cmd(resume_id)
            start_cmd = f'unset CLAUDECODE && {start_cmd}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)

    def _wait_for_startup(self, name: str, tmux_name: str, backend: Backend,
                          resume_id: str, startup_cwd: str) -> bool:
        """Wait for Claude to start, with stale-resume auto-retry.

        Returns True if Claude started successfully.
        """
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if is_claude_running(tmux_name):
                started = True
                break
        if not started and resume_id:
            started = self._retry_after_stale_resume(name, tmux_name, backend, resume_id, startup_cwd)
        return started

    def _retry_after_stale_resume(self, name: str, tmux_name: str, backend: Backend,
                                  resume_id: str, startup_cwd: str) -> bool:
        """Retry a fresh start after a stale resume fails.

        Clears session ID and hooks, sends a fresh start command, and notifies admin.
        Returns True if the fresh start succeeded.
        """
        _log(_LOG_WARN, "restart", f"{name}: resume failed (stale session {resume_id[:8]}), auto-retrying fresh")
        clear_claude_session_id(name)
        _clear_hook_failures(name)
        start_cmd = backend.start_cmd("")
        start_cmd = f'unset CLAUDECODE && {start_cmd}'
        if startup_cwd:
            start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if is_claude_running(tmux_name):
                started = True
                break
        if started:
            _log(_LOG_INFO, "restart", f"{name}: fresh start succeeded after stale resume")
            if admin_chat_id:
                send_telegram_message(
                    admin_chat_id,
                    f"⚠️ {name}: stale session ID {resume_id[:8]}… — auto-restarted fresh ✓"
                )
        else:
            _log(_LOG_WARN, "restart", f"{name}: fresh start also failed after stale resume")
            if admin_chat_id:
                send_telegram_message(
                    admin_chat_id,
                    f"🔴 {name}: resume failed (stale {resume_id[:8]}…) AND fresh start failed.\n"
                    f"/restart --clean {name} to try manually."
                )
        return started

    def _restart_dead_worker(self, name: str, backend_name: str, backend: Backend, tmux_name: str, mode: str) -> tuple[bool, str | None]:
        """Re-create a dead worker (tmux gone) from registry.

        Creates a new tmux session, exports env, starts backend, sends welcome.
        Preserves session files (session_id, cwd) for resume capability.
        """
        if not _which_binary(backend.binary):
            return False, f"'{backend.binary}' not found in PATH. Install it first."

        # Create new tmux session
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env, timeout=TIMEOUT_REMOTE_CMD
        )
        if result.returncode != 0:
            return False, "Could not create worker workspace"
        self._runner.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True, timeout=TIMEOUT_TMUX_SEND)

        self._clock.sleep(DELAY_RETRY)
        export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)

        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)

        ensure_session_dir(name)
        if not backend.is_interactive:
            ensure_worker_pipe(name)

        resume_id = ""
        resume_cwd = ""
        if mode == "resume":
            resume_id = (get_claude_session_id(name, authoritative=False) or
                         get_claude_session_id(name, authoritative=True))
            resume_cwd = get_claude_session_cwd(name)
        else:
            session_dir = self.sessions_dir / name
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd:
            save_claude_session_cwd(name, startup_cwd)
            _ensure_workspace_trusted(startup_cwd)

        if SANDBOX_ENABLED and backend.is_interactive:
            if startup_cwd:
                self._cd_tmux_to_cwd(tmux_name, startup_cwd)
            docker_cmd = get_docker_run_cmd(name, resume_id=resume_id)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, docker_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        else:
            start_cmd = backend.start_cmd(resume_id)
            start_cmd = f'unset CLAUDECODE && {start_cmd}'
            if startup_cwd:
                start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
            if backend.is_interactive:
                self._clock.sleep(DELAY_STARTUP_LONG)
                self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)

        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = False
            for _ in range(10):
                self._clock.sleep(DELAY_STARTUP)
                if is_claude_running(tmux_name):
                    started = True
                    break
            if not started and resume_id:
                _log(_LOG_WARN, "restart", f"{name}: dead worker resume failed (stale session {resume_id[:8]}), auto-retrying fresh")
                clear_claude_session_id(name)
                start_cmd = backend.start_cmd("")
                start_cmd = f'unset CLAUDECODE && {start_cmd}'
                if startup_cwd:
                    start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
                self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
                for _ in range(10):
                    self._clock.sleep(DELAY_STARTUP)
                    if is_claude_running(tmux_name):
                        started = True
                        break
                if started:
                    _log(_LOG_WARN, "restart", f"{name}: dead worker fresh start succeeded after stale resume")
                    if admin_chat_id:
                        send_telegram_message(
                            admin_chat_id,
                            f"⚠️ {name}: stale session ID {resume_id[:8]}… — auto-restarted fresh ✓"
                        )
            if started:
                self.send(name, welcome)
            else:
                _log(_LOG_WARN, "restart", f"{name}: dead worker did not start within 10s, skipping welcome")
        else:
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)

        _log(_LOG_INFO, "worker", f"Dead worker '{name}' recovered from registry (mode={mode})")
        self.invalidate_sessions_cache()
        return True, None


worker_manager = WorkerManager(SESSIONS_DIR, TMUX_PREFIX)


def _sync_worker_manager() -> None:
    """Sync the global worker_manager instance with current module globals."""
    worker_manager.sessions_dir = SESSIONS_DIR
    worker_manager.tmux_prefix = TMUX_PREFIX


# ─────────────────────────────────────────────────────────────────────────────
# grug say: one place for backend branching. no scatter.
# Worker Helpers (centralize backend switching)
# ─────────────────────────────────────────────────────────────────────────────

def worker_is_online(name: str, session: TmuxSessionDict | None = None) -> bool:
    """Check if worker is online and ready.

    Args:
        name: Worker name
        session: Session dict from get_registered_sessions() (optional, avoids re-lookup)
    """
    _sync_worker_manager()
    return worker_manager.is_online(name, session)


def worker_set_pending(name: str, chat_id: int) -> None:
    """Set pending state for worker."""
    set_pending(name, chat_id)


def worker_send(name: str, message: str, chat_id: int | None = None, session: TmuxSessionDict | None = None) -> bool:
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


def scan_tmux_sessions() -> dict[str, TmuxSessionDict]:
    """Scan tmux for registered sessions."""
    _sync_worker_manager()
    return worker_manager.scan_tmux_sessions()


def get_registered_sessions(registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
    """Get registered sessions from tmux (all backends have tmux now)."""
    _sync_worker_manager()
    return worker_manager.get_registered_sessions(registered)


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


def get_docker_run_cmd(name: str, resume_id: str = "") -> list[str]:
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


def stop_docker_container(name: str) -> None:
    """Stop and remove a docker container."""
    container_name = f"claude-worker-{name}"
    _subprocess_runner.run(["docker", "stop", container_name], capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
    _subprocess_runner.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=TIMEOUT_REMOTE_CMD)


def send_to_worker(name: str, message: str, chat_id: int | None = None) -> bool:
    """Send a message to a worker using the appropriate backend."""
    if _send_to_grpc_worker(name, message, "manager"):
        return True
    _sync_worker_manager()
    return worker_manager.send(name, message, chat_id)


def _fetch_remote_file(host: str, remote_path: str) -> str | None:
    """Fetch a file from a remote host via rsync to a local temp path.

    Returns local temp path on success, None on failure.
    Preserves the original filename so Telegram displays it correctly.
    """
    original_name = Path(remote_path).name
    tmp_dir = tempfile.mkdtemp(prefix="remote-file-")
    local_path = os.path.join(tmp_dir, original_name)
    try:
        r = _subprocess_runner.run(
            ["rsync", "-az", f"{host}:{remote_path}", local_path],
            capture_output=True, timeout=TIMEOUT_RSYNC)
        if r.returncode == 0 and os.path.getsize(local_path) > 0:
            return local_path
        if r.returncode != 0:
            _log(_LOG_ERROR, "bridge", f"rsync failed (exit {r.returncode}): {host}:{remote_path} -> {r.stderr.decode(errors='replace').strip()}")
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_WARN, "bridge", f"Remote file fetch failed: {host}:{remote_path} -> {e}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _localize_media(name: str, media_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            _log(_LOG_WARN, "bridge", f"Cannot fetch remote file {host}:{file_path} for {name}")
            result.append((None, f"[Fetch failed: {file_path}]"))
    return result


def _parse_response_media(name: str, text: str) -> tuple[str, list[tuple[str | None, str]], list[tuple[str | None, str]], str | None]:
    """Parse media tags and speak tag from response text.

    Returns (clean_text, images, files, speak_text).
    For teleported workers, skips local file existence checks during parsing.
    """
    host = get_worker_host(name)
    if host:
        _accept_all = lambda p: (True, Path(p))
        clean_text, images = _parse_media_tags(text, "image", _accept_all)
        clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
    else:
        clean_text, images = parse_image_tags(text)
        clean_text, files = parse_file_tags(clean_text)

    # Extract explicit [[speak:custom text]] tag
    speak_text: str | None = None
    speak_match = re.search(r'\[\[speak(?::([^\]]*))?\]\]', clean_text)
    if speak_match:
        custom = speak_match.group(1)
        clean_text = clean_text[:speak_match.start()] + clean_text[speak_match.end():]
        clean_text = clean_text.strip()
        if custom is not None and custom.strip():
            speak_text = custom.strip()

    # Fetch remote files to local temp paths for teleported workers
    images = _localize_media(name, images)
    files = _localize_media(name, files)

    return clean_text, images, files, speak_text


def _send_text_via_telegram(name: str, clean_text: str, chat_id: int, log_prefix: str) -> None:
    """Send text content to Telegram, trying rich → HTML → plain fallback chain."""
    # Try sendRichMessage first (Bot API 10.1+: native headings, tables, 32K limit)
    rich_sent = False
    rich_failed_at = -1
    rich_chunks: list[str] = []
    prev_msg_id: int | None = None

    if hasattr(transport, 'send_rich_text'):
        # Strip redundant worker name prefix
        rich_text = clean_text.lstrip()
        prefix_lower = f"{name}:".lower()
        if rich_text.lower().startswith(prefix_lower):
            rich_text = rich_text[len(prefix_lower):].lstrip()
        rich_text = _pipe_tables_to_html(rich_text)
        rich_md = f"**{name}:**\n{rich_text}"

        prefix_reserve = len(name) + 30
        rich_chunks = split_message(rich_md, TELEGRAM_RICH_MAX_LENGTH - prefix_reserve)

        rich_sent = True
        for i, chunk in enumerate(rich_chunks):
            if i > 0:
                chunk = f"**{name}:** _(continued)_\n{chunk}"
            result = transport.send_rich_text(
                chat_id, chunk,
                reply_to=prev_msg_id if prev_msg_id else None
            )
            if result and result.get("ok"):
                prev_msg_id = result.get("result", {}).get("message_id")
                if len(rich_chunks) > 1:
                    _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} part {i+1}/{len(rich_chunks)} -> Telegram OK")
                else:
                    _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} -> Telegram OK")
            else:
                error_code = (result or {}).get("error_code", 0)
                desc = (result or {}).get("description", "")
                _log(_LOG_ERROR, "bridge", f"{log_prefix} sendRichMessage failed ({error_code}: {desc}), falling back to HTML")
                rich_sent = False
                rich_failed_at = i
                break
            if i < len(rich_chunks) - 1:
                _clock.sleep(DELAY_BRIEF)

    # Partial rich failure: send remaining chunks as HTML
    if not rich_sent and rich_failed_at > 0:
        prev_msg_id = _send_html_fallback_chunks(
            name, rich_chunks[rich_failed_at:], chat_id, log_prefix,
            prev_msg_id, rich_failed_at, len(rich_chunks))
        rich_sent = True  # handled

    # Full HTML path (no rich support or rich never tried)
    if not rich_sent:
        _send_text_as_html(name, clean_text, chat_id, log_prefix)


def _send_html_fallback_chunks(
    name: str, remaining_chunks: list[str], chat_id: int,
    log_prefix: str, prev_msg_id: int | None,
    start_index: int, total_chunks: int
) -> int | None:
    """Send remaining rich chunks as HTML after partial rich failure."""
    remaining_md = '\n'.join(remaining_chunks)
    remaining_html = markdown_to_telegram_html(remaining_md)
    prefix_reserve = len(name) + 30
    chunks = split_message(remaining_html, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)
    for i, part in enumerate(formatted_parts):
        result = transport.send_text(
            chat_id, part, parse_mode="HTML",
            reply_to=prev_msg_id if prev_msg_id else None
        )
        if result and result.get("ok"):
            prev_msg_id = result.get("result", {}).get("message_id")
            _log(_LOG_INFO, "telegram", f"{log_prefix} sent (html fallback): {name} part {start_index + i + 1}/{total_chunks} -> Telegram OK")
        else:
            plain_text = re.sub(r'<[^>]+>', '', part)
            plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            transport.send_text(chat_id, plain_text, reply_to=prev_msg_id if prev_msg_id else None)
        if i < len(formatted_parts) - 1:
            _clock.sleep(DELAY_BRIEF)
    return prev_msg_id


def _send_text_as_html(name: str, clean_text: str, chat_id: int, log_prefix: str) -> None:
    """Send text as HTML with plain-text fallback on 400 errors."""
    html_text = markdown_to_telegram_html(clean_text)
    prefix_reserve = len(name) + 30
    chunks = split_message(html_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)

    prev_msg_id: int | None = None
    for i, part in enumerate(formatted_parts):
        result = transport.send_text(
            chat_id, part, parse_mode="HTML",
            reply_to=prev_msg_id if prev_msg_id else None
        )
        if result and result.get("ok"):
            prev_msg_id = result.get("result", {}).get("message_id")
            if len(formatted_parts) > 1:
                _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} part {i+1}/{len(formatted_parts)} -> Telegram OK")
            else:
                _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} -> Telegram OK")
        else:
            desc = (result or {}).get("description", "")
            error_code = (result or {}).get("error_code", 0)
            if error_code == 400:
                _log(_LOG_WARN, "bridge", f"{log_prefix} HTML send failed (400: {desc}), retrying as plain text")
                plain_text = re.sub(r'<[^>]+>', '', part)
                plain_text = plain_text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
                result = transport.send_text(
                    chat_id, plain_text,
                    reply_to=prev_msg_id if prev_msg_id else None
                )
                if result and result.get("ok"):
                    prev_msg_id = result.get("result", {}).get("message_id")
                    _log(_LOG_INFO, "telegram", f"{log_prefix} sent (plain): {name} -> Telegram OK")
                else:
                    _log(_LOG_WARN, "bridge", f"{log_prefix} failed (plain): {name} -> {result}")
            else:
                _log(_LOG_WARN, "bridge", f"{log_prefix} failed: {name} -> {result}")

        if i < len(formatted_parts) - 1:
            _clock.sleep(DELAY_BRIEF)


def _send_response_media(name: str, images: list[tuple[str | None, str]], files: list[tuple[str | None, str]], chat_id: int) -> None:
    """Send image and file attachments to Telegram with type-based routing."""
    # Send images
    for img_path, img_caption in images:
        if img_path is None:
            transport.send_text(chat_id, f"{name}: {img_caption}")
            continue
        full_caption = f"{name}: {img_caption}" if img_caption else f"{name}:"
        if Path(img_path).suffix.lower() in (".gif", ".mp4"):
            sent = send_animation(chat_id, img_path, full_caption)
        else:
            sent = send_photo(chat_id, img_path, full_caption)
        if sent:
            _log(_LOG_INFO, "telegram", f"Image sent: {name} -> {img_path}")
        else:
            transport.send_text(chat_id, f"{name}: [Image failed: {img_path}]")

    # Send files — route to specialized API method by extension
    for file_path, file_caption in files:
        if file_path is None:
            transport.send_text(chat_id, f"{name}: {file_caption}")
            continue
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
            _log(_LOG_INFO, "telegram", f"File sent: {name} -> {file_path}")
        else:
            transport.send_text(chat_id, f"{name}: [File failed: {file_path}]")


def _send_response_tts(name: str, speak_text: str, chat_id: int) -> None:
    """Synthesize TTS audio and send voice messages in a background thread."""
    paragraphs = [p.strip() for p in speak_text.split('\n\n') if p.strip()]
    if not paragraphs:
        paragraphs = [speak_text]

    def _tts_worker() -> None:
        """Synthesize and send TTS voice messages for each paragraph."""
        try:
            for i, para in enumerate(paragraphs):
                _log(_LOG_INFO, "tts", f"TTS starting: {len(para)} chars for {name} (part {i+1}/{len(paragraphs)})")
                voice_path = synthesize_speech(para)
                if voice_path:
                    send_voice(chat_id, voice_path, caption=f"{name}:")
                    try:
                        os.unlink(voice_path)
                    except OSError as exc:
                        _log(_LOG_DEBUG, "cleanup:_tts_worker", f"{type(exc).__name__}: {exc}")
        except OSError as e:
            _log(_LOG_ERROR, "bridge", f"TTS thread error: {e}")

    threading.Thread(target=_tts_worker, daemon=True).start()


def send_response_to_telegram(name: str, text: str, chat_id: int, log_prefix: str = "Response") -> None:
    """Send a worker response to Telegram — text, media, and TTS.

    Orchestrates: media parsing → text sending (rich/HTML/plain fallback) →
    image/file delivery → optional TTS synthesis.
    """
    clean_text, images, files, speak_text = _parse_response_media(name, text)

    # Auto-TTS: use clean text when no explicit [[speak:...]] tag
    if speak_text is None and TTS_ENDPOINT and state.get("tts_enabled", True):
        speak_text = clean_text

    # Debug: log very short text (helps trace empty "name:" messages)
    if clean_text and len(clean_text.strip()) <= 5:
        _log(_LOG_DEBUG, "telegram", f"{log_prefix} short msg: {name}, text={repr(clean_text)}, "
             f"images={len(images)}, files={len(files)}")

    if clean_text:
        _send_text_via_telegram(name, clean_text, chat_id, log_prefix)

    _send_response_media(name, images, files, chat_id)

    # TTS: skip for messages >1000 chars
    if speak_text is not None and speak_text and len(speak_text) <= 1000:
        _send_response_tts(name, speak_text, chat_id)


def handle_grpc_worker_response(name: str, text: str, payload: bytes = b"") -> None:
    """Route a gRPC worker response through the same Telegram path as hooks."""
    try:
        if not name or not text:
            _log(_LOG_WARN, "grpc", f"gRPC response ignored: missing worker name or text")
            return

        chat_id_file = get_chat_id_file(name)
        if not chat_id_file.exists():
            _log(_LOG_INFO, "grpc", f"gRPC response: no chat_id for session '{name}'")
            return

        chat_id = chat_id_file.read_text().strip()
        _log(_LOG_INFO, "grpc", f"gRPC response: {name} -> chat {chat_id} ({len(text)} chars)")

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
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                _log(_LOG_WARN, "grpc", f"gRPC response payload ignored for '{name}': {e}")

        send_response_to_telegram(name, text, int(chat_id), log_prefix="gRPC response")
        _check_learning_reminder(name)
        clear_pending(name)
        mark_hook_event(name)
    except OSError as e:
        _log(_LOG_ERROR, "bridge", f"gRPC response error for '{name}': {e}")


def handle_grpc_worker_register(name: str, host: str, version: str, tools: dict[str, Any]) -> None:
    """Handle a gRPC worker registration event."""
    tool_names = ", ".join(sorted(tools.keys())) if tools else "none"
    host_label = host or "unknown-host"
    version_label = version or "unknown-version"
    _log(_LOG_INFO, "grpc", f"gRPC worker registered: {name} ({host_label}, {version_label}, tools: {tool_names})")


def _beast_serve_deploy(html_path: str, slug: str) -> str | None:
    """Deploy an HTML file via beast serve and return the public URL, or None on failure."""
    try:
        r = _subprocess_runner.run(
            ["beast", "serve", "deploy", html_path, "--slug", slug, "--output-json"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            url = data.get("url", "")
            if url and "localhost" in url:
                host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "157.180.48.254"
                url = url.replace("localhost", host)
            return url or None
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_WARN, "bridge", f"beast serve deploy failed for {slug}: {e}")
    return None


def handle_grpc_worker_disconnect(name: str) -> None:
    """Handle a gRPC worker disconnection event."""
    _log(_LOG_INFO, "grpc", f"gRPC worker disconnected: {name}")


def handle_grpc_jsonl_received(stream_id: str, data: bytes) -> None:
    """Handle an incoming JSONL message from a gRPC worker."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", stream_id or "stream")
    jsonl_dir = SESSIONS_DIR / "grpc-jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    jsonl_dir.chmod(0o700)
    path = jsonl_dir / f"{safe_id}.jsonl"
    path.write_bytes(data)
    path.chmod(0o600)
    _log(_LOG_INFO, "grpc", f"gRPC JSONL received: {stream_id or safe_id} -> {path} ({len(data)} bytes)")


def _merge_grpc_workers(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Include connected gRPC workers in /workers without changing tmux entries."""
    if grpc_server is None:
        return workers

    try:
        connected = grpc_server.get_connected_workers()
    except (ConnectionError, OSError, TimeoutError) as e:
        _log(_LOG_WARN, "grpc", f"gRPC worker list unavailable: {e}")
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


def create_session(name: str, backend: str = DEFAULT_BACKEND, chat_id: int | None = None) -> bool:
    """Create a new worker instance."""
    _sync_worker_manager()
    return worker_manager.hire(name, backend, chat_id=chat_id)


def kill_session(name: str) -> bool:
    """Kill a worker instance."""
    _sync_worker_manager()
    return worker_manager.end(name)


def restart_claude(name: str, mode: str = "relaunch") -> bool:
    """Restart claude in an existing tmux session."""
    _sync_worker_manager()
    return worker_manager.restart(name, mode=mode)


def switch_session(name: str) -> bool:
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

def send_typing_loop(chat_id: int | str, session_name: str) -> None:
    """Send typing indicator while request is pending."""
    while is_pending(session_name):
        transport.send_chat_action(chat_id, "typing")
        _clock.sleep(DELAY_CLAUDE_LOAD)


def get_all_chat_ids() -> list[int]:
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
                    except (OSError, ValueError) as exc:
                        _log(_LOG_DEBUG, "parse:get_all_chat_ids", f"{type(exc).__name__}: {exc}")
    # Also include current admin if known
    if admin_chat_id:
        chat_ids.add(str(admin_chat_id))
    return chat_ids


def send_shutdown_message() -> None:
    """Send shutdown notification to all known chat_ids."""
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        _log(_LOG_WARN, "notify", "No chat_ids to notify")
        return

    _log(_LOG_INFO, "notify", f"Sending shutdown to {len(chat_ids)} chat(s)...")
    for chat_id in chat_ids:
        transport.send_text(chat_id, "Going offline briefly. Your team stays the same.")
    _log(_LOG_INFO, "notify", "Shutdown notifications sent")


# ============================================================
# NON-CORE: CommandRouter
# ============================================================

class _LegacyTransportAdapter(MessageTransport):
    """Wraps legacy TelegramAPI-style objects (with send_message/set_reaction)
    for backward compat with tests that pass FakeTelegram to CommandRouter."""

    def __init__(self, legacy: Any) -> None:
        """Initialize with TelegramAPI or duck-typed test double (must have send_message)."""
        self._legacy: Any = legacy  # Any: accepts FakeTelegram test doubles without Protocol

    @property
    def name(self) -> str:
        """Return the transport name identifier."""
        return "legacy-adapter"

    def send_text(self, chat_id: ChatId, text: str,
                  parse_mode: ParseMode | None = None,
                  reply_to: MessageId | None = None) -> TelegramApiResponse:
        """Send a plain text message."""
        result = self._legacy.send_message(chat_id, text)
        return result if result else {"ok": True, "result": {"message_id": 1}}

    def send_photo(self, chat_id: ChatId, photo_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a photo to a Telegram chat."""
        return False

    def send_document(self, chat_id: ChatId, doc_path: str | Path,
                      caption: str | None = None) -> bool:
        """Send a document file to a Telegram chat."""
        return False

    def send_animation(self, chat_id: ChatId, animation_path: str | Path,
                       caption: str | None = None) -> bool:
        """Send an animation (GIF/MP4) to a Telegram chat."""
        return False

    def send_video(self, chat_id: ChatId, video_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a video to a Telegram chat."""
        return False

    def send_audio(self, chat_id: ChatId, audio_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send an audio file to a Telegram chat."""
        return False

    def send_voice(self, chat_id: ChatId, voice_path: str | Path,
                   caption: str | None = None) -> bool:
        """Send a voice message to a Telegram chat."""
        return False

    def send_sticker(self, chat_id: ChatId, sticker_path: str | Path) -> bool:
        """Send a sticker to a Telegram chat."""
        return False

    def send_chat_action(self, chat_id: ChatId, action: str) -> None:
        """Send a chat action indicator (typing, uploading, etc.)."""
        pass  # intentional no-op: abstract method

    def set_reaction(self, chat_id: ChatId, message_id: MessageId,
                     reaction: list[dict[str, str]]) -> None:
        """Set an emoji reaction on a message."""
        if hasattr(self._legacy, 'set_reaction'):
            self._legacy.set_reaction(chat_id, message_id, reaction)

    def edit_message(self, chat_id: ChatId, message_id: MessageId, text: str,
                     parse_mode: ParseMode | None = None) -> TelegramApiResponse:
        """Edit an existing message by its ID."""
        return {"ok": True, "result": {"message_id": message_id}}

    def setup_commands(self, commands: list[dict[str, str]]) -> None:
        """Register bot command suggestions with Telegram."""
        pass  # intentional no-op: abstract method

    def download_file(self, file_id: str, session_name: str) -> str | None:
        """Download a file from Telegram by file ID."""
        return None


# ── CommandRouter: dict-based dispatch ──

# Type alias for command handler functions
CommandFn = Callable[[str, ChatId, MessageId], bool]




def _fanout_channel_message(channel_id: str, from_member: str,
                            text: str, msg: ChannelMessageDict,
                            members_snapshot: dict[str, ChannelMemberDict],
                            registered: dict[str, TmuxSessionDict]) -> None:
    """Deliver a channel message to all members except the sender.

    Used by both CommandRouter (manager sends via /ch) and GuestEndpointsMixin
    (guest sends via POST /guest/send).
    """
    tagged = f"[{channel_id} from {from_member}] {text}"
    for member_key, minfo in members_snapshot.items():
        if member_key == from_member:
            continue
        if minfo["type"] == "worker":
            wname = minfo.get("name", "")
            if wname and wname in registered:
                winfo = registered[wname]
                backend_name = get_worker_backend(wname, winfo)
                backend = get_backend(backend_name)
                try:
                    backend.send(wname, f"{TMUX_PREFIX}{wname}", tagged,
                                 f"http://localhost:{PORT}", SESSIONS_DIR)
                except (ConnectionError, TimeoutError) as e:
                    _log(_LOG_WARN, "bridge", f"Channel fan-out to {wname} failed: {e}")
        elif minfo["type"] == "guest":
            gname = minfo.get("name", "")
            if gname:
                with guest_store.lock:
                    ginbox = guest_store.inboxes.get(gname, [])
                    guest_store.inboxes[gname] = guest_inbox_append(ginbox, {
                        "id": msg["id"], "from": from_member,
                        "channel": channel_id, "text": text, "ts": msg["ts"],
                    })
        elif minfo["type"] == "manager":
            try:
                if admin_chat_id:
                    send_telegram_message(admin_chat_id,
                        f"[{channel_id}] {from_member}: {text}")
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")


class CommandRouter:
    """Dispatches Telegram commands and messages to worker handlers."""

    # ── Teleport Commands ──────────────────────────────────────────────

    def cmd_teleport(self, arg: str, chat_id: ChatId, check_only: bool = False) -> bool:
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

        # Resolve machine catalog name to ssh_target
        machines = get_machine_catalog()
        if target_host in machines:
            machine = machines[target_host]
            if machine.ssh_target:
                target_host = machine.ssh_target

        # 1. Worker exists?
        registry = _load_registry()
        worker_entry = registry.get("workers", {}).get(worker_name)
        if not worker_entry:
            self.reply(chat_id, f"Worker '{worker_name}' not found in registry.")
            return True
        backend_name = worker_entry.get("backend", "claude")

        # 2. Worker not actively busy? (EXITED/OFFLINE/UNKNOWN are all fine)
        with watchdog.lock:
            worker_state = watchdog.worker_states.get(worker_name, ("UNKNOWN", "", 0))
        current_state = worker_state[0]
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
                        capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            self.reply(chat_id, f"Cannot reach {target_host} via SSH.")
            return True

        # 5. Claude Code on target?
        claude_path = _resolve_remote_tool("claude", target_host)
        if claude_path == "claude":
            self.reply(chat_id, f"claude not found on {target_host}. Install it first.")
            return True

        # 6. tmux on target?
        tmux_path = _resolve_remote_tool("tmux", target_host)
        if tmux_path == "tmux":
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
                         f"{target_bridge_url}/"],
                        host=target_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            self.reply(chat_id,
                f"Target {target_host} cannot reach {target_bridge_url}. "
                f"Ensure BRIDGE_BIND=0.0.0.0 and network connectivity.")
            return True

        # 9. Claude credentials on target?
        r = _remote_run(["test", "-f", ".claude/.credentials.json"],
                        host=target_host, capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0:
            # Try to sync credentials from source
            local_creds = os.path.expanduser("~/.claude/.credentials.json")
            if os.path.exists(local_creds):
                _remote_run(["mkdir", "-p", ".claude"],
                             host=target_host, capture_output=True)
                _subprocess_runner.run(
                    ["rsync", "-az", local_creds,
                     f"{target_host}:.claude/.credentials.json"],
                    capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
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
                        host=target_host, capture_output=True, timeout=TIMEOUT_TMUX_SEND)
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

    def cmd_teleback(self, arg: str, chat_id: ChatId) -> bool:
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
        with watchdog.lock:
            worker_state = watchdog.worker_states.get(worker_name, ("UNKNOWN", "", 0))
        if worker_state[0] in ("BUSY_TOOL", "BUSY_THINKING"):
            self.reply(chat_id,
                f"{worker_name} is busy. Must be idle to teleback.")
            return True

        target_host = home_host  # Where we're going back to (None = local)
        target_cwd = home_cwd or get_claude_session_cwd(worker_name)

        # If going back to local, verify current remote host is reachable
        if current_host:
            r = _remote_run(["echo", "ok"], host=current_host,
                            capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
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

    def _check_teleback_conflicts(self, name: str, remote_host: str, local_cwd: str) -> list[str]:
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
        local_status = _subprocess_runner.run(
            ["git", "-C", local_cwd, "status", "--porcelain"],
            capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        local_changed = bool(local_status.stdout.strip()) if local_status.returncode == 0 else False

        local_head = _subprocess_runner.run(
            ["git", "-C", local_cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        local_commit = local_head.stdout.strip() if local_head.returncode == 0 else ""

        # Get remote git status
        # Detect remote CWD (may have different $HOME prefix)
        home_result = _remote_run(
            ["bash", "-c", "echo $HOME"], host=remote_host,
            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
        local_home = os.path.expanduser("~")

        remote_cwd = local_cwd
        if remote_home and remote_home != local_home and local_cwd.startswith(local_home):
            remote_cwd = remote_home + local_cwd[len(local_home):]

        r_status = _remote_run(
            ["git", "-C", remote_cwd, "status", "--porcelain"],
            host=remote_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        remote_changed = bool(r_status.stdout.strip()) if r_status.returncode == 0 else False

        r_head = _remote_run(
            ["git", "-C", remote_cwd, "rev-parse", "HEAD"],
            host=remote_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
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

    def _do_teleport(self, name: str, target_host: str, target_cwd: str, full_sync: bool,
                     chat_id: int | str, is_teleback: bool=False) -> None:
        """Run the full teleport flow in a background thread."""
        try:
            registered = self.workers.get_registered_sessions()
            session = registered.get(name, {})
            tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
            backend_name = get_worker_backend(name, session)
            source_host = get_worker_host(name)

            source_cwd = get_claude_session_cwd(name)
            _log(_LOG_INFO, "teleport", f"{name}: source_host={source_host}, source_cwd={source_cwd}, target_host={target_host}, target_cwd={target_cwd}")
            if not target_cwd:
                target_cwd = source_cwd
                # Remap home directory when source and target have different $HOME
                # e.g., /home/claude/project → /Users/beastoinagents/project
                if target_cwd and target_host:
                    local_home = os.path.expanduser("~")
                    home_result = _remote_run(
                        ["bash", "-c", "echo $HOME"], host=target_host,
                        capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                    remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
                    if remote_home and remote_home != local_home and target_cwd.startswith(local_home):
                        target_cwd = remote_home + target_cwd[len(local_home):]

            # Expand ~ in target_cwd to remote $HOME
            if target_cwd and target_cwd.startswith("~") and target_host:
                home_result = _remote_run(
                    ["bash", "-c", "echo $HOME"], host=target_host,
                    capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
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
                "started_at": int(_clock.time()),
            }))

            # ── PHASE 1: Stop and sync (reversible) ──

            self._teleport_notify(chat_id, f"Stopping {name}...")
            session_id = self._stop_worker_for_teleport(name, tmux_name, source_host)
            _log(_LOG_INFO, "teleport", f"{name}: stopped, session_id={session_id}")

            if source_cwd and target_cwd:
                self._teleport_notify(chat_id, f"Syncing working directory...")
                _log(_LOG_INFO, "teleport", f"{name}: syncing {source_cwd} → {target_cwd}")
                ok = self._sync_working_directory(
                    source_cwd, target_cwd, source_host, target_host, full_sync)
                _log(_LOG_INFO, "teleport", f"{name}: working dir sync ok={ok}")
                if not ok:
                    self._teleport_rollback(name, tmux_name, source_host, source_cwd,
                                            session_id, backend_name, chat_id,
                                            "working directory sync failed")
                    return

            if session_id:
                self._teleport_notify(chat_id, "Syncing session transcript...")
                self._sync_session_transcript(
                    session_id, source_cwd, target_cwd, source_host, target_host)
                _log(_LOG_INFO, "teleport", f"{name}: transcript sync done")

            # On teleport out: push team configs + install hooks on target
            # On teleback: skip — VPS is source of truth for team-scope config
            if not is_teleback:
                self._teleport_notify(chat_id, "Syncing team config and hooks...")
                sync_warnings = self._sync_shared_repos(target_host, chat_id)
                hook_warnings = self._install_hooks_on_target(target_host)
                all_warnings = sync_warnings + hook_warnings
                if all_warnings:
                    self._teleport_notify(
                        chat_id,
                        f"Config sync completed with warnings:\n" +
                        "\n".join(f"- {w}" for w in all_warnings))
                _log(_LOG_WARN, "teleport", f"{name}: team config + hooks synced ({len(all_warnings)} warnings)")

            # ── PHASE 2: Commit ──

            state_file.write_text(json.dumps({
                "phase": 2, "source_host": source_host,
                "target_host": target_host, "target_cwd": target_cwd,
                "started_at": int(_clock.time()),
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
            _log(_LOG_INFO, "teleport", f"{name}: calling _start_worker_on_target(target_cwd={target_cwd}, session_id={session_id}, backend={backend_name})")
            ok = self._start_worker_on_target(
                name, target_host, target_cwd, session_id, backend_name)
            _log(_LOG_INFO, "teleport", f"{name}: _start_worker_on_target returned {ok}")
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
                    _clock.sleep(DELAY_PROCESS_SETTLE)  # Let Claude finish loading
                    self.workers.send(name, welcome)
                except (ConnectionError, TimeoutError, AttributeError, OSError) as e:
                    _log(_LOG_WARN, "teleport", f"Warning: failed to send welcome to {name}: {e}")

            state_file.unlink(missing_ok=True)

            dest_label = target_host or "local"
            action = "teleported back" if is_teleback else "teleported"
            msg = f"{name} {action} to {dest_label}:{target_cwd}"
            if session_id:
                msg += f"\nSession resumed ({session_id[:8]}...)."
            if not is_teleback:
                msg += f"\nUse /teleback {name} to bring it back."
            self._teleport_notify(chat_id, msg)

        except (subprocess.SubprocessError, ConnectionError, TimeoutError, TypeError, AttributeError, OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "teleport", f"Teleport failed: {e}", exc=e)
            self._teleport_notify(chat_id, f"Teleport failed: {e}")
            try:
                state_file = SESSIONS_DIR / name / "teleport_state"
                state_file.unlink(missing_ok=True)
            except OSError as exc:
                _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")

    def _stop_worker_for_teleport(self, name: str, tmux_name: str, host: str | None=None) -> str | None:
        """Gracefully stop Claude Code and return session_id."""
        session_id = get_claude_session_id(name, authoritative=True)

        # Send /exit for graceful shutdown
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"],
                     host=host, capture_output=True)

        # Wait for process to exit (up to 10s)
        for _ in range(20):
            _clock.sleep(DELAY_RETRY)
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
            _clock.sleep(DELAY_STARTUP)

        # Re-read session ID (hook may have updated it during /exit)
        return get_claude_session_id(name, authoritative=True) or session_id

    def _sync_working_directory(self, source_cwd: str, target_cwd: str,
                                 source_host: str | None=None, target_host: str=None,
                                 full: bool=False) -> bool:
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
                            _log(_LOG_INFO, "teleport", f"git sync succeeded for {project}")
                            return True
                        _log(_LOG_WARN, "teleport", f"git pull failed, falling back to rsync")
                    else:
                        _log(_LOG_WARN, "teleport", f"git push failed, falling back to rsync")
                except (subprocess.SubprocessError, OSError, KeyError) as e:
                    _log(_LOG_ERROR, "teleport", f"git sync error, falling back to rsync: {e}")

        return self._rsync_working_directory(
            source_cwd, target_cwd, source_host, target_host, full)

    def _rsync_working_directory(self, source_cwd: str, target_cwd: str,
                                  source_host: str | None=None, target_host: str=None,
                                  full: bool=False) -> bool:
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
                    host=source_host, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
                if gi_result.returncode == 0 and gi_result.stdout.strip():
                    fd, gitignore_tmpfile = tempfile.mkstemp(
                        prefix="rsync-gitignore-", suffix=".txt")
                    os.write(fd, gi_result.stdout.encode())
                    os.close(fd)
                    cmd.extend(["--exclude-from", gitignore_tmpfile])
            except (ConnectionError, TimeoutError, OSError) as e:
                _log(_LOG_WARN, "teleport", f"git ls-files failed, skipping gitignore excludes: {e}")

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
            r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_FULL_SYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "teleport", f"rsync failed: cmd={cmd} rc={r.returncode} stderr={r.stderr[:500]}")
            return r.returncode == 0
        finally:
            if gitignore_tmpfile and os.path.exists(gitignore_tmpfile):
                os.unlink(gitignore_tmpfile)

    def _sync_session_transcript(self, session_id: str, source_cwd: str, target_cwd: str,
                                  source_host: str | None=None, target_host: str=None) -> None:
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
            r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "teleport", f"transcript sync failed for {item}: {r.stderr[:200]}")

    def _sync_shared_repos(self, target_host: str, chat_id: int | str | None = None) -> list[str]:
        """Sync team and agent-config git repos between VPS and target.

        Best-effort: never raises.  Returns a list of warning strings
        for any operations that failed.  The caller can report them but
        a failure here must never abort the teleport.

        VPS hosts bare repos at ~/git/{team,agent-config}.git.
        Both VPS working copies and target clones use these as origin.
        Push from source, pull on target.
        """
        if not target_host:
            return []

        warnings = []
        try:
            home = os.path.expanduser("~")
            git_repos = {
                "team": os.path.join(home, "team"),
                "agent-config": os.path.join(home, "agent-config"),
            }

            # Push local changes to bare repo (VPS side)
            for repo_name, repo_path in git_repos.items():
                if os.path.isdir(os.path.join(repo_path, ".git")):
                    try:
                        _subprocess_runner.run(
                            ["git", "-C", repo_path, "add", "-A"],
                            capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                        _subprocess_runner.run(
                            ["git", "-C", repo_path, "commit", "-m",
                             f"teleport sync: {repo_name}"],
                            capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                        _subprocess_runner.run(
                            ["git", "-C", repo_path, "push", "origin", "master"],
                            capture_output=True, timeout=TIMEOUT_LARGE_TRANSFER)
                        _log(_LOG_INFO, "teleport", f"git sync succeeded for {repo_name}")
                    except (subprocess.SubprocessError, OSError) as e:
                        w = f"git push {repo_name}: {e}"
                        warnings.append(w)
                        _log(_LOG_INFO, "teleport", f"{w}")

            # Pull on target
            for repo_name in git_repos:
                try:
                    _remote_run(
                        ["bash", "-c",
                         f"cd ~/{repo_name} 2>/dev/null && git pull origin master 2>/dev/null || true"],
                        host=target_host, capture_output=True, timeout=TIMEOUT_LARGE_TRANSFER)
                except (subprocess.SubprocessError, OSError) as e:
                    w = f"git pull {repo_name} on {target_host}: {e}"
                    warnings.append(w)
                    _log(_LOG_INFO, "teleport", f"{w}")

            # Deploy agent-config to ~/.claude/ on target (skills, hooks, scripts)
            for subdir in ["skills", "hooks", "scripts"]:
                try:
                    _remote_run(
                        ["bash", "-c",
                         f"[ -d ~/agent-config/.claude/{subdir} ] && "
                         f"rsync -az --checksum ~/agent-config/.claude/{subdir}/ ~/.claude/{subdir}/"],
                        host=target_host, capture_output=True, timeout=TIMEOUT_LARGE_TRANSFER)
                except (subprocess.SubprocessError, OSError) as e:
                    w = f"agent-config deploy {subdir}: {e}"
                    warnings.append(w)
                    _log(_LOG_INFO, "teleport", f"{w}")

            # Adapt settings.json paths for target $HOME
            home_result = _remote_run(["bash", "-c", "echo $HOME"], host=target_host,
                                  capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
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
                    _subprocess_runner.run(
                        ["rsync", "-az", tmp, f"{target_host}:.claude/settings.json"],
                        capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                    os.unlink(tmp)
        except (subprocess.SubprocessError, OSError) as e:
            w = f"shared repo sync: {e}"
            warnings.append(w)
            _log(_LOG_INFO, "teleport", f"{w}")

        return warnings

    def _sync_worker_data_back(self, name: str, source_host: str | None) -> None:
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
            _subprocess_runner.run(
                ["rsync", "-az",
                 f"{source_host}:team/{name}/",
                 f"{worker_team_dir}/"],
                capture_output=True, timeout=TIMEOUT_GIT_OP)

        # 2. Sync auto-memory files back
        # Memory lives in ~/.claude/projects/<slug>/memory/
        r = _remote_run(
            ["bash", "-c",
             "find ~/.claude/projects/*/memory -name '*.md' 2>/dev/null | head -50"],
            host=source_host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode == 0 and r.stdout.strip():
            for remote_file in r.stdout.strip().splitlines():
                # Convert remote path to local: replace remote $HOME with local
                home_result = _remote_run(
                    ["bash", "-c", "echo $HOME"], host=source_host,
                    capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
                if remote_home and remote_file.startswith(remote_home):
                    local_file = home + remote_file[len(remote_home):]
                    local_dir = os.path.dirname(local_file)
                    os.makedirs(local_dir, exist_ok=True)
                    _subprocess_runner.run(
                        ["rsync", "-az",
                         f"{source_host}:{remote_file}", local_file],
                        capture_output=True, timeout=TIMEOUT_REMOTE_CMD)

    def _run_teleport_preflight(self, target_host: str, worker_name: str, backend_name: str) -> tuple[bool, list[str]]:
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
                    r = _subprocess_runner.run(
                        [script_path], env=env,
                        capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
                    if r.returncode != 0:
                        reason = r.stdout.strip().split("\n")[0] if r.stdout.strip() else f"{script} failed"
                        fails.append(reason)
                except subprocess.TimeoutExpired:
                    fails.append(f"{script} timed out")
                except (subprocess.SubprocessError, OSError) as e:
                    fails.append(f"{script} error: {e}")

        return fails

    def _install_hooks_on_target(self, target_host: str) -> list[str]:
        """Install Claude Code hooks and settings on target machine.

        Best-effort: never raises.  Returns a list of warning strings.

        With git-synced agent-config, this is a lightweight fallback
        for any files not covered by the repo (e.g., .claude.json).
        Hooks/skills/settings are synced via _sync_shared_repos.
        """
        if not target_host:
            return []

        warnings = []
        try:
            # Ensure hooks dir has correct permissions
            _remote_run(["chmod", "-R", "700", ".claude/hooks"],
                         host=target_host, capture_output=True)

            # Sync .claude.json (onboarding, trust dialogs, project config)
            claude_json = os.path.expanduser("~/.claude.json")
            if os.path.exists(claude_json):
                _subprocess_runner.run(
                    ["rsync", "-az", claude_json, f"{target_host}:.claude.json"],
                    capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
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
                    host=target_host, capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
        except (subprocess.SubprocessError, OSError) as e:
            w = f"hook install: {e}"
            warnings.append(w)
            _log(_LOG_INFO, "teleport", f"{w}")

        return warnings

    def _sync_session_files_to_target(self, name: str, target_sessions_dir: str, target_host: str) -> None:
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
                _subprocess_runner.run(
                    ["rsync", "-az", str(local_file),
                     f"{target_host}:{remote_session_dir}/{fname}"],
                    capture_output=True, timeout=TIMEOUT_REMOTE_CMD)

    def _sync_credentials_to_target(self, target_host: str) -> None:
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
                             host=target_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode == 0 and r.stdout.strip():
                remote_data = json.loads(r.stdout)
                local_data = json.loads(open(local_creds).read())
                remote_oauth = remote_data.get("claudeAiOauth", {})
                local_oauth = local_data.get("claudeAiOauth", {})
                remote_refresh = remote_oauth.get("refreshToken", "")
                local_refresh = local_oauth.get("refreshToken", "")
                remote_exp = remote_oauth.get("expiresAt", 0)
                now_ms = int(_clock.time() * 1000)
                # Skip if target has a DIFFERENT refresh token that hasn't expired
                if remote_refresh and remote_refresh != local_refresh and remote_exp > now_ms:
                    _log(_LOG_INFO, "creds", f"Target {target_host} has independent valid credentials, skipping sync")
                    return
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, subprocess.SubprocessError, OSError):
            pass  # intentional no-op: can't verify remote — fall through to sync anyway

        _remote_run(["mkdir", "-p", ".claude"],
                     host=target_host, capture_output=True)
        # Atomic: rsync to tmp, then mv (avoids truncated file on crash)
        _subprocess_runner.run(
            ["rsync", "-az", local_creds,
             f"{target_host}:.claude/.credentials.json.tmp"],
            capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
        _remote_run(["mv", ".claude/.credentials.json.tmp",
                      ".claude/.credentials.json"],
                     host=target_host, capture_output=True)
        _remote_run(["chmod", "600", ".claude/.credentials.json"],
                     host=target_host, capture_output=True)
        _log(_LOG_INFO, "creds", f"Synced credentials to {target_host}")

    def _start_worker_on_target(self, name: str, target_host: str, target_cwd: str,
                                 session_id: str, backend_name: str, skip_session_sync: bool=False) -> bool:
        """Create tmux session on target and start Claude Code with --resume."""
        tmux_name = f"{TMUX_PREFIX}{name}"

        # Clean up any leftover session
        _remote_run(["tmux", "kill-session", "-t", tmux_name],
                     host=target_host, capture_output=True)
        _clock.sleep(DELAY_TMUX_SEND)

        # Create new session
        r = _remote_run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            host=target_host, capture_output=True, text=True)
        if r.returncode != 0:
            _log(_LOG_WARN, "teleport", f"tmux new-session failed: rc={r.returncode} stderr={r.stderr[:200] if r.stderr else ''}")
            return False
        _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
                    host=target_host, capture_output=True)

        _clock.sleep(DELAY_RETRY)

        # Remap SESSIONS_DIR for target $HOME (e.g. /home/claude → /Users/user)
        target_sessions_dir = str(SESSIONS_DIR)
        local_home = os.path.expanduser("~")
        if target_host:
            home_result = _remote_run(
                ["bash", "-c", "echo $HOME"], host=target_host,
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
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

        _clock.sleep(DELAY_TMUX_SEND)

        # Source env and unset CLAUDECODE
        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name,
             'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
            host=target_host, capture_output=True)
        _clock.sleep(DELAY_TMUX_SEND)

        # Build and send start command
        backend = get_backend(backend_name)
        cli_cmd = backend.start_cmd(session_id)

        # Claude Code refuses --dangerously-skip-permissions as root
        if target_host:
            id_result = _remote_run(["id", "-u"], host=target_host,
                               capture_output=True, text=True)
            if id_result.returncode == 0 and id_result.stdout.strip() == "0":
                cli_cmd = cli_cmd.replace(" --dangerously-skip-permissions", "")

        start_cmd = f'unset CLAUDECODE && {cli_cmd}'
        if target_cwd:
            start_cmd = f'cd {shlex.quote(target_cwd)} && {start_cmd}'

        _log(_LOG_INFO, "teleport", f"start_cmd={start_cmd}")
        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
            host=target_host, capture_output=True)

        if backend.is_interactive:
            # Claude may show first-time prompts (theme picker, permission
            # mode). Navigate them: Enter accepts defaults, "2" selects
            # auto-accept permission mode. Multiple Enter presses are safe.
            for delay, key in [(3.0, "Enter"), (2.0, "Enter"),
                               (1.0, "Enter"), (1.0, "Enter")]:
                _clock.sleep(delay)
                _remote_run(["tmux", "send-keys", "-t", tmux_name, key],
                             host=target_host, capture_output=True)

        # Verify Claude is running (retry up to 30s for startup)
        for attempt in range(30):
            _clock.sleep(DELAY_STARTUP)
            r = _remote_run(
                ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
                host=target_host, capture_output=True, text=True)
            if r.returncode != 0:
                if attempt % 10 == 0:
                    _log(_LOG_WARN, "teleport", f"verify attempt {attempt}: tmux display-message failed rc={r.returncode}")
                continue
            pane_pid = r.stdout.strip()
            claude_pid = _get_claude_pid(pane_pid, host=target_host) if pane_pid else None
            if claude_pid:
                _log(_LOG_INFO, "teleport", f"verified: claude running as pid {claude_pid} (pane {pane_pid})")
                return True
            if attempt % 10 == 0:
                _log(_LOG_INFO, "teleport", f"verify attempt {attempt}: pane_pid={pane_pid}, no claude yet")
                # Capture pane to see what's happening
                cap = _remote_run(
                    ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                    host=target_host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if cap.returncode == 0:
                    _log(_LOG_INFO, "teleport", f"pane content: {cap.stdout[:300]}")
        _log(_LOG_WARN, "teleport", f"verify FAILED after 30 attempts")
        return False

    def _teleport_rollback(self, name: str, tmux_name: str, source_host: str | None, source_cwd: str,
                            session_id: str, backend_name: str, chat_id: int | str, reason: str) -> None:
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
        except (subprocess.SubprocessError, OSError) as e:
            self._teleport_notify(chat_id, f"Rollback also failed: {e}")

        try:
            state_file = SESSIONS_DIR / name / "teleport_state"
            state_file.unlink(missing_ok=True)
        except OSError as exc:
            _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")

    def _teleport_notify(self, chat_id: int | str, text: str) -> None:
        """Send progress notification during teleport."""
        try:
            transport.send_text(chat_id, text)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:_teleport_notify", f"{type(exc).__name__}: {exc}")



    # ── Worker Lifecycle ──────────────────────────────────────────────

    def cmd_hire(self, name: str, chat_id: ChatId) -> bool:
        """Handle the /hire command — create a new worker."""
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

    def cmd_end(self, name: str, chat_id: ChatId) -> bool:
        """Handle the /end command — terminate a worker."""
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

    def cmd_pause(self, chat_id: ChatId) -> bool:
        """Handle the /pause command — send Ctrl-C to a worker."""
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

    def cmd_restart(self, chat_id: ChatId, args: str = "") -> bool:
        """Handle the /restart command — restart a worker session."""
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
        _log(_LOG_INFO, "cmd_restart", f"{name}: force={force}, clean={clean}, host={host}, tmux={tmux_name}")
        tmux_alive = tmux_exists(tmux_name, host=host)
        claude_running = is_claude_running(tmux_name, host=host) if tmux_alive else False
        _log(_LOG_INFO, "cmd_restart", f"{name}: tmux_alive={tmux_alive}, claude_running={claude_running}")
        if not force and tmux_alive and claude_running:
            _log(_LOG_INFO, "cmd_restart", f"{name}: BLOCKED (already running)")
            self.reply(chat_id, f"{name.capitalize()} is already running. Use /restart --force {name} to force.")
            return True

        # In-flight dedupe: block if a restart is already in progress (even with --force)
        with watchdog.restart_lock:
            inflight_ts = watchdog.restart_in_progress.get(name)
            if inflight_ts and _clock.time() - inflight_ts < 120:
                _log(_LOG_INFO, "cmd_restart", f"{name}: BLOCKED (restart in progress since {_clock.time() - inflight_ts:.0f}s ago)")
                self.reply(chat_id, f"{name.capitalize()} restart already in progress. Wait for it to finish.")
                return True
            watchdog.restart_in_progress[name] = _clock.time()

        try:
            result = self._do_restart(name, session, chat_id, host, tmux_name, force, clean)
            if force:
                watchdog.force_restart_pending_cwd[name] = True
            return result
        finally:
            with watchdog.restart_lock:
                watchdog.restart_in_progress.pop(name, None)

    def _do_restart(self, name: str, session: TmuxSessionDict, chat_id: ChatId, host: str | None, tmux_name: str, force: bool, clean: bool) -> bool:
        """Execute restart after in-flight guard. Called from cmd_restart."""
        # Teleported worker: delegate to remote restart
        if host:
            mode = "relaunch" if clean else "resume"
            backend_name = get_worker_backend(name, session) if session else DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)
            resume_id = (get_claude_session_id(name, authoritative=False) or
                         get_claude_session_id(name, authoritative=True)) if mode == "resume" else ""
            target_cwd = get_claude_session_cwd(name)
            _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart mode={mode}, resume_id={resume_id}, cwd={target_cwd}")
            self.reply(chat_id, f"Restarting {name.capitalize()} on remote host...")
            ok, err = self._restart_remote_worker(
                name, backend_name, backend_obj, tmux_name, host, mode)
            watchdog.recent_restarts[name] = _clock.time()
            _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart result ok={ok}, err={err}")
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
                watchdog.recent_restarts[name] = _clock.time()
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
                watchdog.recent_restarts[name] = _clock.time()
                self.reply(chat_id, f"Restarting {name.capitalize()} fresh...")
                self.reply(chat_id, f"{name.capitalize()} is back and ready.")
            else:
                self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
            return True

        ok, err = restart_claude(name, mode="resume")
        if ok:
            watchdog.recent_restarts[name] = _clock.time()
            self.reply(chat_id, f"Resuming {name.capitalize()}...")
            self.reply(chat_id, f"{name.capitalize()} is back and ready.")
        else:
            self.reply(chat_id, f"Could not restart \"{name}\". {err}", outcome="Needs decision")
        return True

    def _restart_remote_worker(self, name: str, backend_name: str, backend: Backend, tmux_name: str, host: str, mode: str) -> tuple[bool, str | None]:
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
                home_result = _remote_run(
                    ["bash", "-c", "echo $HOME"], host=host,
                    capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
                if remote_home and remote_home != local_home:
                    target_cwd = remote_home + target_cwd[len(local_home):]

        _log(_LOG_INFO, "_restart_remote", f"{name}: mode={mode}, host={host}, tmux={tmux_name}, cwd={target_cwd}")
        if mode == "resume":
            # Respect cached session ID first (may have been set explicitly).
            # Only fall back to authoritative scan if cache is empty.
            resume_id = get_claude_session_id(name, authoritative=False)
            if not resume_id:
                resume_id = get_claude_session_id(name, authoritative=True)
            _log(_LOG_INFO, "_restart_remote", f"{name}: resume_id={resume_id}")
        else:
            # Clear session IDs for relaunch
            session_dir = SESSIONS_DIR / name
            session_dir.mkdir(parents=True, exist_ok=True)
            cleared = list(session_dir.glob("*_session_id"))
            for f in cleared:
                f.unlink()
            _clear_hook_failures(name)
            _log(_LOG_INFO, "_restart_remote", f"{name}: cleared {len(cleared)} session files for relaunch")

        # Stop the remote Claude process if tmux is still alive
        if tmux_exists(tmux_name, host=host):
            _log(_LOG_INFO, "_restart_remote", f"{name}: stopping remote tmux {tmux_name}")
            self._stop_worker_for_teleport(name, tmux_name, host=host)
            # Kill tmux — _start_worker_on_target creates a fresh one
            _remote_run(["tmux", "kill-session", "-t", tmux_name],
                         host=host, capture_output=True)
            _clock.sleep(DELAY_RETRY)
        else:
            _log(_LOG_INFO, "_restart_remote", f"{name}: tmux {tmux_name} not found on {host}")

        # Re-read session_id (hook may have updated during /exit)
        # Prefer cached value — only scan if cache was cleared by the stop hook
        if mode == "resume":
            cached = get_claude_session_id(name, authoritative=False)
            if cached:
                resume_id = cached
            else:
                resume_id = get_claude_session_id(name, authoritative=True) or resume_id
            _log(_LOG_INFO, "_restart_remote", f"{name}: post-stop resume_id={resume_id}")

        # Validate session is resumable on target before attempting --resume
        # Claude stores sessions under ~/.claude/projects/-<cwd-dashes>/<session_id>.jsonl
        # If the file doesn't exist on the target, --resume will fail immediately
        if resume_id and target_cwd and host:
            # Build the project dir path on the remote host
            home_result = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                                  capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            remote_home = home_result.stdout.strip() if home_result.returncode == 0 else ""
            if remote_home:
                # Claude Code project dir: ~/.claude/projects/-<cwd with / replaced by->
                cwd_slug = target_cwd.replace("/", "-")
                session_file = f"{remote_home}/.claude/projects/{cwd_slug}/{resume_id}.jsonl"
                check = _remote_run(["test", "-f", session_file], host=host,
                                     capture_output=True, timeout=TIMEOUT_TMUX_SEND)
                if check.returncode != 0:
                    _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} NOT found at {session_file}, starting fresh")
                    resume_id = ""
                    # Clear stale session ID
                    session_dir = SESSIONS_DIR / name
                    session_dir.mkdir(parents=True, exist_ok=True)
                    for f in session_dir.glob("*_session_id"):
                        f.unlink()
                else:
                    _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} validated at {session_file}")

        # Delegate to existing remote start flow
        # skip_session_sync=True: worker was already on this host, target session files are authoritative
        _log(_LOG_INFO, "_restart_remote", f"{name}: calling _start_worker_on_target(cwd={target_cwd}, resume={resume_id}, backend={backend_name})")
        ok = self._start_worker_on_target(
            name, host, target_cwd, resume_id, backend_name, skip_session_sync=True)
        if not ok:
            _log(_LOG_WARN, "_restart_remote", f"{name}: _start_worker_on_target FAILED")
            return False, f"Failed to restart {name} on {host}"

        # Wait for Claude to actually start before sending welcome
        welcome = self.workers._build_welcome(name, backend)
        if backend.is_interactive:
            started = False
            for _ in range(10):
                _clock.sleep(DELAY_STARTUP)
                if is_claude_running(tmux_name, host=host):
                    started = True
                    break
            if started:
                self.workers.send(name, welcome)
            else:
                _log(_LOG_WARN, "_restart_remote", f"{name}: Claude did not start within 10s, skipping welcome")

        _log(_LOG_INFO, "_restart_remote", f"{name}: restarted successfully (mode={mode})")
        return True, None

    def _cmd_restart_all(self, chat_id: int | str, clean: bool) -> bool:
        """Handle /restart-all command — sequentially restart all workers."""
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

    def _run_restart_all_sequence(self, chat_id: int | str, names: list[str], mode: str) -> None:
        """Background thread: restart workers one at a time with delay between."""
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
                        _clock.sleep(delay_s / 5)

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

    def _cmd_restart_cancel(self, chat_id: int | str) -> bool:
        """Cancel a running restart-all sequence."""
        with self._restart_all_lock:
            if not self._restart_all_running:
                self.reply(chat_id, "No restart-all sequence is running.")
                return True
            self._restart_all_abort.set()
        self.reply(chat_id, "Stopping restart-all sequence...")
        return True

    # ── Channel & Relay ─────────────────────────────────────────────

    def _cmd_relay_list(self, chat_id: ChatId) -> bool:
        """Handle /relay list — show active relay channels."""
        with relay_store.lock:
            active = [(cid, ch) for cid, ch in relay_store.channels.items()
                      if _clock.time() <= ch["expires_at_unix"]]
        if active:
            lines = []
            for cid, ch in active:
                msg_count = len(ch.get("messages", []))
                workers = ch.get("workers", [ch["worker"]])
                worker_str = ", ".join(workers)
                lines.append(f"• {cid} → {worker_str} ({msg_count} msgs, expires {ch['expires_at']})")
            self.reply(chat_id, "\U0001f4e1 Active relays:\n" + "\n".join(lines))
        else:
            self.reply(chat_id, "No active relays.")
        return True

    def _cmd_relay_add(self, parts: list[str], chat_id: ChatId) -> bool:
        """Handle /relay add <channel_id> <worker>."""
        if len(parts) < 3:
            self.reply(chat_id, "Usage: /relay add <channel_id> <worker>")
            return True
        channel_id = parts[1]
        new_worker = parts[2].lower()
        registered = get_registered_sessions()
        if new_worker not in registered:
            self.reply(chat_id, f"Worker \"{new_worker}\" not found.")
            return True
        with relay_store.lock:
            found = relay_store.channels.get(channel_id)
            if not found or _clock.time() > found["expires_at_unix"]:
                found = None
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
        """Handle /relay remove <channel_id> <worker>."""
        if len(parts) < 3:
            self.reply(chat_id, "Usage: /relay remove <channel_id> <worker>")
            return True
        channel_id = parts[1]
        rm_worker = parts[2].lower()
        with relay_store.lock:
            found = relay_store.channels.get(channel_id)
            if not found or _clock.time() > found["expires_at_unix"]:
                found = None
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
        """Handle /relay status — counts for relays, channels, guests."""
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
            msg_count = len(ch.get("messages", []))
            workers = ch.get("workers", [ch["worker"]])
            lines.append(f"  • {ch['label']} → {', '.join(workers)} ({msg_count} msgs)")
        lines.append(f"\nChannels: {len(ch_active)}")
        for cid, ch in ch_active:
            members = ", ".join(ch["members"].keys())
            msg_count = len(ch.get("messages", []))
            lines.append(f"  • {cid} ({ch['label']}) — {members} ({msg_count} msgs)")
        lines.append(f"\nGuests: {len(guest_active)}")
        for g in guest_active:
            inbox_count = len(guest_store.inboxes.get(g["name"], []))
            lines.append(f"  • {g['name']} (inbox: {inbox_count} msgs)")
        self.reply(chat_id, "\n".join(lines))
        return True

    def _cmd_relay_stop(self, parts: list[str], chat_id: ChatId) -> bool:
        """Handle /relay stop <label> — close a relay channel."""
        if len(parts) < 2:
            self.reply(chat_id, "Usage: /relay stop <label>")
            return True
        target = parts[1]
        removed = False
        with relay_store.lock:
            to_remove = None
            if target in relay_store.channels:
                to_remove = target
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
        if removed:
            self.reply(chat_id, f"\U0001f4e1 Relay \"{target}\" closed.")
        else:
            self.reply(chat_id, f"Relay \"{target}\" not found.")
        return True

    def cmd_relay(self, arg: str, chat_id: ChatId) -> bool:
        """Relay: connect an external agent to a worker via guideline link.

        Dispatches to subcommand helpers:
        /relay <worker> — open relay, returns guideline link URL
        /relay add/remove/list/status/stop — manage relay channels
        """
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
            if active:
                lines.append(f"\nActive: {', '.join(ch['label'] for ch in active)}")
            self.reply(chat_id, "\n".join(lines))
            return True

        parts = arg.strip().split()
        sub = parts[0].lower()

        subcommands: dict[str, Callable[..., bool]] = {
            "list": lambda: self._cmd_relay_list(chat_id),
            "add": lambda: self._cmd_relay_add(parts, chat_id),
            "remove": lambda: self._cmd_relay_remove(parts, chat_id),
            "status": lambda: self._cmd_relay_status(chat_id),
            "stop": lambda: self._cmd_relay_stop(parts, chat_id),
        }
        if sub in subcommands:
            return subcommands[sub]()

        # /relay <worker> — main flow
        worker = sub
        registered = get_registered_sessions()
        if worker not in registered:
            self.reply(chat_id, f"Worker \"{worker}\" not found.")
            return True

        label = f"relay-{worker}"
        ch, guest_token, reply_token = relay_channel_create(worker, label)
        with relay_store.lock:
            relay_store.channels[ch["id"]] = ch
            _relay_save()

        url = relay_guide_url(ch["id"], guest_token)
        lines = [f"\U0001f4e1 Relay to {worker} (24h) — {ch['id']}"]
        lines.append(f"\nManage: /relay add {ch['id']} <worker> to add more workers")
        lines.append(f"\nPaste this to the external agent:\n")
        lines.append(f"---")
        lines.append(f"You have a direct channel to {worker}. Open this link to see the API guide (setup + curl commands):")
        lines.append(f"{url}")
        lines.append(f"Steps: (1) open the link, (2) copy the env vars and curl commands from the page, (3) send your message via the /send endpoint, (4) poll /messages for replies. Channel expires in 24h.")
        lines.append(f"---")
        self.reply(chat_id, "\n".join(lines))
        return True

    def cmd_channel(self, arg: str, chat_id: ChatId) -> bool:
        """Handle /ch and /channel commands.

        /ch create <label> <member1> <member2> ...
        /ch <id> <message>
        /ch <id> add <member1> ...
        /ch <id> remove <member1> ...
        /ch <id> members
        /ch list
        /ch <id> close
        """
        if not arg:
            self.reply(chat_id,
                "Usage:\n"
                "/ch create <label> <members...>\n"
                "/ch <id> <message>\n"
                "/ch <id> add <member...>\n"
                "/ch <id> remove <member...>\n"
                "/ch <id> members\n"
                "/ch <id> close\n"
                "/ch list",
                outcome="Needs decision")
            return True

        parts = arg.strip().split()
        subcmd = parts[0].lower()

        if subcmd == "create":
            if len(parts) < 2:
                self.reply(chat_id, "Usage: /ch create <label> [member1 member2 ...]", outcome="Needs decision")
                return True
            label = parts[1]
            members = parts[2:] if len(parts) > 2 else []
            members.append("manager")
            channel_id = channel_create_id(label)
            channel = channel_new(channel_id, label, "manager", members)
            with channel_store.lock:
                channel_store.channels[channel_id] = channel
                _channel_save()
            member_str = ", ".join(channel["members"].keys())
            self.reply(chat_id,
                f"\U0001f4e2 Channel {channel_id} ({label})\nMembers: {member_str}")
            return True

        if subcmd == "list":
            with channel_store.lock:
                active = [(cid, channel) for cid, channel in channel_store.channels.items()
                          if not channel_is_expired(channel)]
            if not active:
                self.reply(chat_id, "No active channels.")
            else:
                lines = []
                for cid, channel in active:
                    members = ", ".join(channel["members"].keys())
                    lines.append(f"{cid} ({channel['label']}) — {members}")
                self.reply(chat_id, "\n".join(lines))
            return True

        # Remaining commands: /channel <channel_id> <action>
        channel_id = subcmd
        with channel_store.lock:
            channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel):
            self.reply(chat_id, f"Channel {channel_id} not found.", outcome="Needs decision")
            return True

        if len(parts) < 2:
            # Just show channel info
            members = ", ".join(channel["members"].keys())
            self.reply(chat_id, f"{channel_id} ({channel['label']})\nMembers: {members}\nMessages: {len(channel['messages'])}")
            return True

        action = parts[1].lower()

        if action == "close":
            with channel_store.lock:
                channel_store.channels.pop(channel_id, None)
                _channel_save()
            self.reply(chat_id, f"\U0001f4e2 Channel {channel_id} closed.")
            return True

        if action == "members":
            members = ", ".join(channel["members"].keys())
            self.reply(chat_id, f"{channel_id} members: {members}")
            return True

        if action == "add":
            to_add = parts[2:]
            if not to_add:
                self.reply(chat_id, "Usage: /channel <id> add <member...>", outcome="Needs decision")
                return True
            with channel_store.lock:
                added = channel_add_members(channel, to_add)
            if added:
                self.reply(chat_id, f"{channel_id}: added {', '.join(added)}")
            else:
                self.reply(chat_id, f"No new members to add.")
            return True

        if action == "remove":
            to_remove = parts[2:]
            if not to_remove:
                self.reply(chat_id, "Usage: /channel <id> remove <member...>", outcome="Needs decision")
                return True
            with channel_store.lock:
                removed = channel_remove_members(channel, to_remove)
            if removed:
                self.reply(chat_id, f"{channel_id}: removed {', '.join(removed)}")
            else:
                self.reply(chat_id, f"No members to remove.")
            return True

        # Default: send message to channel
        message_text = " ".join(parts[1:])
        with channel_store.lock:
            msg = channel_append_message(channel, "manager", message_text)
            members_snapshot = dict(channel["members"])
        registered = get_registered_sessions()
        _fanout_channel_message(channel_id, "manager", message_text, msg,
                                     members_snapshot, registered)
        self.reply(chat_id, f"[{channel_id}] Sent.")
        return True



    # ── Media Routing ──────────────────────────────────────────────────

    def _extract_reply_media(self, reply_to: dict[str, Any], target_worker: str) -> str | None:
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

    def _worker_from_reply(self, msg: dict[str, Any]) -> str | None:
        """Extract worker name from a reply-to message's text prefix (e.g. 'bob:\\n...')."""
        reply_to = msg.get("reply_to_message") if msg else None
        if not reply_to:
            return None
        reply_text = _extract_msg_text(reply_to)
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

    def _handle_media_group_flush(self, group_id: str) -> None:
        """Flush a buffered media group: route all items using the group's caption."""
        with media_groups.lock:
            group = media_groups.buffer.pop(group_id, None)
        if not group:
            return
        items = group["items"]
        caption = group.get("caption", "")
        # Find the first msg dict (for reply-to context)
        first_msg = items[0] if items else {}
        chat_id = first_msg.get("chat", {}).get("id")
        msg_id = first_msg.get("message_id")
        # Determine target worker from caption (same logic as _resolve_media_target)
        target = None
        if caption:
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                target = targets[0]
        if not target:
            reply_worker = self._worker_from_reply(first_msg)
            if reply_worker:
                target = reply_worker
        if not target:
            target = state["active"]

        # Download and route each item to the same target
        all_paths = []
        for msg in items:
            photo = msg.get("photo")
            document = msg.get("document")
            animation = msg.get("animation")
            video = msg.get("video")
            audio = msg.get("audio")
            voice = msg.get("voice")
            video_note = msg.get("video_note")
            sticker = msg.get("sticker")

            doc_is_image = False
            if document:
                mime_type = document.get("mime_type", "")
                doc_is_image = mime_type.startswith("image/")

            file_id = None
            media_type = "media"
            if animation:
                file_id = animation.get("file_id")
                media_type = "GIF"
            elif photo:
                largest = max(photo, key=lambda p: p.get("file_size", 0))
                file_id = largest.get("file_id")
                media_type = "image"
            elif doc_is_image:
                file_id = document.get("file_id")
                media_type = "image"
            elif document:
                file_id = document.get("file_id")
                media_type = "file"
            elif video:
                file_id = video.get("file_id")
                media_type = "video"
            elif audio:
                file_id = audio.get("file_id")
                media_type = "audio"
            elif voice:
                file_id = voice.get("file_id")
                media_type = "voice"
            elif video_note:
                file_id = video_note.get("file_id")
                media_type = "video note"
            elif sticker:
                file_id = sticker.get("file_id")
                media_type = "sticker"

            if file_id:
                local_path = download_telegram_file(file_id, target)
                if local_path:
                    all_paths.append((local_path, media_type))

        if not all_paths:
            return

        # Build combined message text
        path_lines = "\n".join(f"Manager sent {mt}: `{p}`" for p, mt in all_paths)
        if caption:
            full_text = f"{caption}\n\n{path_lines}"
        else:
            full_text = path_lines

        self._route_media_message(full_text, caption, chat_id, msg_id, msg=first_msg)

    def _resolve_media_target(self, caption: str, msg: dict[str, Any]) -> tuple[str | None, str | None]:
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

    def _route_media_message(self, media_text: str, caption: str, chat_id: int | str, msg_id: int, msg: dict[str, Any]=None) -> None:
        """Route a media message, honoring @mentions in caption or reply-to context."""
        if caption:
            unknown_mentions = self.unknown_at_mentions(caption)
            if unknown_mentions:
                self.reply(chat_id, self.format_unknown_mentions_warning(unknown_mentions))
                return
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                statuses = []
                for name in targets:
                    statuses.append(self._route_mention(name, media_text, chat_id, msg_id))
                sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
                offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
                # Only reply on failure — success is silent
                if offline:
                    parts = []
                    parts.append(f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline.")
                    if sent_to:
                        parts.append(f"Delivered to {', '.join(sent_to)}.")
                    self.reply(chat_id, " ".join(parts))
                return
        # Check reply-to: if replying to a worker's message, route to that worker
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            self.route_message(reply_worker, media_text, chat_id, msg_id, one_off=True)
            return
        self.route_to_active(media_text, chat_id, msg_id)



    # ── Mention Routing ──────────────────────────────────────────────

    def _reset_mention_streak(self) -> None:
        """Reset the @mention auto-focus streak tracker."""
        _last_mention["target"] = None
        _last_mention["count"] = 0
        _last_mention["ts"] = 0

    def _handle_mention_routing(self, targets: list[str], message: str,
                                text: str, chat_id: int, msg_id: int,
                                reply_to: dict[str, Any] | None,
                                reply_context: str,
                                reply_context_ts: int | None) -> None:
        """Route a message with @mentions to the targeted workers."""
        # Bare @mention means focus switch only (silent on success)
        if len(targets) == 1 and re.fullmatch(r'\s*@[a-zA-Z0-9-]+\s*', text):
            target = targets[0]
            registered = self.workers.get_registered_sessions()
            if target in registered:
                state["active"] = target
                save_last_active(target)
            else:
                self.reply(chat_id, f"Can't focus guest {target}.")
            self._reset_mention_streak()
            return

        if reply_context:
            message = self.format_reply_context(message, reply_context, reply_context_ts)

        statuses = []
        if reply_to:
            reply_media = self._extract_reply_media(reply_to, targets[0])
            if reply_media:
                media_text = reply_media
                if message:
                    media_text = f"{message}\n\n{reply_media}"
                for name in targets:
                    statuses.append(self._route_mention(name, media_text, chat_id, msg_id))
            else:
                for name in targets:
                    statuses.append(self._route_mention(name, message, chat_id, msg_id))
        else:
            for name in targets:
                statuses.append(self._route_mention(name, message, chat_id, msg_id))

        sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
        offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
        if offline:
            parts = [f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline."]
            if sent_to:
                parts.append(f"Delivered to {', '.join(sent_to)}.")
            self.reply(chat_id, " ".join(parts))

        # Auto-focus: same single registered worker mentioned 2+ times within 60s
        registered = self.workers.get_registered_sessions()
        now = _clock.time()
        if len(targets) == 1 and targets[0] in registered:
            target = targets[0]
            if _last_mention["target"] == target and now - _last_mention.get("ts", 0) <= 60:
                _last_mention["count"] += 1
            else:
                _last_mention["target"] = target
                _last_mention["count"] = 1
            _last_mention["ts"] = now
            if _last_mention["count"] >= 2 and state["active"] != target:
                state["active"] = target
                save_last_active(target)
                self.reply(chat_id, f"Switched to {target} (you mentioned them twice).")
        else:
            self._reset_mention_streak()

    # @mention regex: negative lookbehind skips @ inside email addresses
    # (e.g., user@gmail.com → @gmail NOT matched; "@geni hello" → @geni matched)
    _mention_re = re.compile(r'(?<![a-zA-Z0-9._+\-])@([a-zA-Z0-9-]+)')

    def parse_at_mentions(self, text: str) -> tuple[list[str], str]:
        """Extract known @mentions from anywhere in text. Returns (targets, original_text).
        Matches registered workers first, then active guests. Full message preserved."""
        if not text:
            return [], ""
        registered = self.workers.get_registered_sessions()
        with guest_store.lock:
            guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        known = set(registered.keys()) | guest_names
        found = []
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name in known and name not in found:
                found.append(name)
        return found, text

    def unknown_at_mentions(self, text: str) -> list[str]:
        """Return @mentions that do not match a known worker/guest."""
        if not text:
            return []
        registered = self.workers.get_registered_sessions()
        with guest_store.lock:
            guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        known = set(registered.keys()) | guest_names | {"all"}
        unknown = []
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name not in known and name not in unknown:
                unknown.append(name)
        return unknown

    def format_unknown_mentions_warning(self, unknown_mentions: list[str]) -> str:
        """Format a warning message for unrecognized @mentions."""
        import difflib

        registered = self.workers.get_registered_sessions()
        with guest_store.lock:
            guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        known = sorted(set(registered.keys()) | guest_names)
        parts = []
        for name in unknown_mentions:
            suggestions = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
            if suggestions:
                parts.append(f"@{name}. Did you mean {', '.join('@' + s for s in suggestions)}?")
            else:
                parts.append(f"@{name}.")
        return f"⚠️ Unknown: {' '.join(parts)}"

    def _route_mention(self, name: str, message: str, chat_id: int | str, msg_id: int) -> dict[str, Any] | None:
        """Route a @mention to either a worker or a guest inbox. Workers win name collisions."""
        registered = self.workers.get_registered_sessions()
        session = registered.get(name)
        if session:
            if not self.workers.is_online(name, session):
                return {"name": name, "status": "offline"}
            self.route_message(name, message, chat_id, msg_id, one_off=True)
            return {"name": name, "status": "sent"}

        with guest_store.lock:
            guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        if name in guest_names:
            msg_obj = {
                "id": f"gm_{secrets.token_hex(4)}",
                "from": "manager",
                "text": message,
                "ts": int(_clock.time()),
            }
            with guest_store.lock:
                inbox = guest_store.inboxes.get(name, [])
                guest_store.inboxes[name] = guest_inbox_append(inbox, msg_obj)
            return {"name": name, "status": "sent"}

        return {"name": name, "status": "unknown"}

    def parse_worker_prefix(self, text: str) -> tuple[str | None, str]:
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

    def get_reply_context(self, reply_msg: dict[str, Any]) -> tuple[str, int | None]:
        """Extract text and timestamp from a replied-to message."""
        if not reply_msg:
            return "", None
        text = _extract_msg_text(reply_msg)
        ts = reply_msg.get("date")
        return text, ts

    def format_reply_context(self, reply_text: str, context_text: str, context_ts: str | None = None) -> str:
        """Format reply-to context for prepending to forwarded messages."""
        reply_text = (reply_text or "").strip()
        context_text = (context_text or "").strip()
        if context_text:
            ts_str = ""
            if context_ts:
                ts_str = f" at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(context_ts))}"
            return (
                "Manager reply:\n"
                f"{reply_text}\n\n"
                f"Context (your previous message{ts_str}):\n"
                f"{context_text}"
            )
        return f"Manager reply:\n{reply_text}"


    # ── Memory Commands ─────────────────────────────────────────────

    def cmd_memory(self, query: str, chat_id: ChatId) -> bool:
        """Dispatch /memory subcommands. /memory <query|subcommand>"""
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

        subcmd = query.strip().lower().split()[0]
        dispatch: dict[str, Callable[[str, ChatId], bool]] = {
            "update": lambda q, c: self._memory_update(c),
            "status": lambda q, c: self._memory_status(c),
            "wake-up": self._memory_wakeup,
            "recall": self._memory_recall,
        }
        handler = dispatch.get(subcmd)
        if handler:
            return handler(query, chat_id)
        return self._memory_search(query, chat_id)

    def _memory_status(self, chat_id: ChatId) -> bool:
        """Show memory stack health summary."""
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
        except (KeyError, RuntimeError, OSError, ImportError) as e:
            self.reply(chat_id, f"Memory status failed: {e}")
        return True

    def _memory_wakeup(self, query: str, chat_id: ChatId) -> bool:
        """Generate L0+L1 wake-up context for a wing."""
        try:
            from team_memory.memory_stack import MemoryStack
            stack = MemoryStack()
            parts = query.strip().split()
            wing = parts[1] if len(parts) > 1 else None
            text = stack.wake_up(wing=wing)
            if len(text) > 4000:
                text = text[:3997] + "..."
            self.reply(chat_id, text)
        except (KeyError, RuntimeError, OSError, ImportError) as e:
            self.reply(chat_id, f"Memory wake-up failed: {e}")
        return True

    def _memory_recall(self, query: str, chat_id: ChatId) -> bool:
        """On-demand L2 recall with optional --wing and --room filters."""
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
        except (KeyError, RuntimeError, OSError, ImportError) as e:
            self.reply(chat_id, f"Memory recall failed: {e}")
        return True

    def _memory_search(self, query: str, chat_id: ChatId) -> bool:
        """Full-text search across team chat memory with source links."""
        self.reply(chat_id, "Searching memory...")

        try:
            from team_memory.search import search_memory
            result = search_memory(query)
        except (OSError, json.JSONDecodeError, KeyError) as e:
            self.reply(chat_id, f"Memory search failed: {e}", outcome="Needs decision")
            return True

        answer = result.get("answer", "")
        sources = result.get("results", [])

        if not answer and not sources:
            self.reply(chat_id, "No results found.")
            return True

        lines = [f"\U0001f9e0 {answer}" if answer else "\U0001f9e0 No direct answer found."]

        if sources:
            lines.append("")
            import secrets
            tc_token = secrets.token_urlsafe(32)
            REWIND_TOKENS[tc_token] = {"name": "__team__", "expires_at": _clock.time() + REWIND_TIMEOUT}
            base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"

            lines.append("\U0001f4ce Sources:")
            for i, r in enumerate(sources[:3], 1):
                chunk_lines = r.get("text", "").split("\n")
                preview = "\n".join(chunk_lines[:2])
                if len(preview) > 200:
                    preview = preview[:197] + "..."
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
                        except (ValueError, IndexError) as exc:
                            _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
                lines.append(f"{i}. {preview}{source_link}")

        self.reply(chat_id, "\n".join(lines))
        return True

    def _memory_update(self, chat_id: int | str) -> bool:
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
            r = _subprocess_runner.run(
                [sys.executable, parse_script, "--zip", latest, "--out", parsed_path],
                capture_output=True, text=True, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                self.reply(chat_id, f"Parse failed: {r.stderr[:300]}", outcome="Needs decision")
                return True

            # Ingest (incremental)
            ingest_script = str(Path(__file__).parent / "team_memory" / "ingest.py")
            r = _subprocess_runner.run(
                [sys.executable, ingest_script, parsed_path, "--incremental"],
                capture_output=True, text=True, timeout=PENDING_TIMEOUT)
            if r.returncode != 0:
                self.reply(chat_id, f"Ingest failed: {r.stderr[:300]}", outcome="Needs decision")
                return True

            # Extract stats from output
            lines = r.stdout.strip().split("\n")
            summary = lines[-1] if lines else "Done"
            self.reply(chat_id, f"Memory updated.\n{summary}")

        except subprocess.TimeoutExpired:
            self.reply(chat_id, "Memory update timed out.", outcome="Needs decision")
        except (subprocess.SubprocessError, OSError) as e:
            self.reply(chat_id, f"Memory update failed: {e}", outcome="Needs decision")
        return True


    # ── Core Router ─────────────────────────────────────────────────

    def __init__(self, transport: MessageTransport | None,
                 workers: "WorkerManager") -> None:
        # Accept MessageTransport or legacy TelegramAPI-style objects (for test compat)
        """Initialize command router with worker manager, transport, and dispatch table."""
        if transport is not None and not isinstance(transport, MessageTransport):
            transport = _LegacyTransportAdapter(transport)
        self.transport: MessageTransport | None = transport
        self.workers: "WorkerManager" = workers
        # Restart-all state
        self._restart_all_lock: threading.Lock = threading.Lock()
        self._restart_all_running: bool = False
        self._restart_all_abort: threading.Event = threading.Event()
        self._restart_all_thread: threading.Thread | None = None

        # Each handler takes (arg, chat_id, message_id) and returns True if handled.
        # To add a new command, add one line here. No dispatch changes needed.
        self._commands: dict[str, CommandFn] = {
            "/hire": lambda arg, cid, mid: self.cmd_hire(arg, cid),
            "/focus": lambda arg, cid, mid: self.cmd_focus(arg, cid),
            "/team": lambda arg, cid, mid: self.cmd_team(cid),
            "/end": lambda arg, cid, mid: self.cmd_end(arg, cid),
            "/progress": lambda arg, cid, mid: self.cmd_progress(cid, arg),
            "/pause": lambda arg, cid, mid: self.cmd_pause(cid),
            "/restart": lambda arg, cid, mid: self.cmd_restart(cid, arg),
            "/settings": lambda arg, cid, mid: self.cmd_settings(cid),
            "/voice": lambda arg, cid, mid: self.cmd_voice(arg, cid),
            "/pilot": lambda arg, cid, mid: self.cmd_pilot(arg, cid),
            "/relay": lambda arg, cid, mid: self.cmd_relay(arg, cid),
            "/rewind": lambda arg, cid, mid: self.cmd_rewind(arg, cid),
            "/pr": lambda arg, cid, mid: self.cmd_pr_review(arg, cid),
            "/memory": lambda arg, cid, mid: self.cmd_memory(arg, cid),
            "/teleport": lambda arg, cid, mid: self.cmd_teleport(arg, cid),
            "/teleport-check": lambda arg, cid, mid: self.cmd_teleport(arg, cid, check_only=True),
            "/teleback": lambda arg, cid, mid: self.cmd_teleback(arg, cid),
            "/ch": lambda arg, cid, mid: self.cmd_channel(arg, cid),
            "/channel": lambda arg, cid, mid: self.cmd_channel(arg, cid),
        }

    def reply(self, chat_id: ChatId, text: str, outcome: str | None = None) -> None:
        """Send a reply message to a chat via the transport."""
        if self.transport is not None:
            self.transport.send_text(chat_id, text)

    def send_startup_message(self, chat_id: ChatId) -> None:
        """Send the bridge startup notification to the admin chat."""
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

    def handle_message(self, update: dict[str, Any]) -> None:
        """Route an incoming Telegram update to the appropriate handler."""
        global admin_chat_id
        _log(_LOG_INFO, "handle_message", f"ENTER update_id={update.get('update_id')}")

        # Parse update once into IncomingMessage
        incoming = IncomingMessage.from_update(update)
        msg = incoming.raw_msg
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        _log(_LOG_INFO, "handle_message", f"chat_id={chat_id} admin={admin_chat_id} text={repr(text[:40])}")

        # Media group handling: buffer multi-photo messages
        media_group_id = msg.get("media_group_id")
        has_media = (incoming.photo or incoming.document or incoming.animation
                     or incoming.video or incoming.audio or incoming.voice
                     or incoming.video_note or incoming.sticker)
        if media_group_id and has_media and chat_id:
            if not self._check_admin(chat_id):
                return
            self._buffer_media_group(media_group_id, msg, text)
            return

        # Single media message
        if has_media and chat_id:
            if self._handle_single_media(incoming):
                return

        # Text-only message routing
        if text and chat_id:
            self._route_text_message(incoming)

    def _check_admin(self, chat_id: int) -> bool:
        """Verify chat_id is admin, auto-learning if first contact. Returns True if allowed."""
        global admin_chat_id
        if admin_chat_id is None:
            admin_chat_id = chat_id
            return True
        return chat_id == admin_chat_id

    def _buffer_media_group(self, media_group_id: str, msg: dict[str, Any], text: str) -> None:
        """Buffer a media group item and schedule flush when group is complete."""
        with media_groups.lock:
            if media_group_id not in media_groups.buffer:
                media_groups.buffer[media_group_id] = {
                    "items": [],
                    "caption": "",
                    "timer": None,
                }
            group = media_groups.buffer[media_group_id]
            group["items"].append(msg)
            if text:
                group["caption"] = text
            if group["timer"]:
                group["timer"].cancel()
            t = threading.Timer(_MEDIA_GROUP_WAIT,
                                self._handle_media_group_flush, args=[media_group_id])
            t.daemon = True
            group["timer"] = t
            t.start()

    def _handle_single_media(self, incoming: 'IncomingMessage') -> bool:
        """Handle a single media message (animation, photo, document, audio, etc.).

        Returns True if the message was handled, False otherwise.
        """
        global admin_chat_id
        msg = incoming.raw_msg
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id

        # Determine media type and file_id
        file_id: str | None = None
        media_label = "media"

        if incoming.animation:
            file_id = incoming.animation.get("file_id")
            media_label = "GIF"
        elif incoming.photo or incoming.doc_is_image:
            if incoming.photo:
                largest = max(incoming.photo, key=lambda p: p.get("file_size", 0))
                file_id = largest.get("file_id")
            else:
                file_id = incoming.document.get("file_id") if incoming.document else None
            media_label = "image"
        elif incoming.document and not incoming.doc_is_image:
            file_id = incoming.document.get("file_id")
            media_label = "file"
        elif incoming.audio or incoming.voice or incoming.video or incoming.video_note or incoming.sticker:
            media_item = (incoming.audio or incoming.voice or incoming.video
                          or incoming.video_note or incoming.sticker)
            file_id = media_item.get("file_id") if media_item else None
            if incoming.audio:
                media_label = "audio"
            elif incoming.voice:
                media_label = "voice"
            elif incoming.video:
                media_label = "video"
            elif incoming.video_note:
                media_label = "video note"
            elif incoming.sticker:
                media_label = "sticker"

        if not file_id:
            return False

        if not self._check_admin(chat_id):
            return True

        if not state["active"] and not self.parse_at_mentions(text)[0]:
            self.reply(chat_id, "No focused worker. Use /focus <name> or @worker in caption.")
            return True

        download_target = self._resolve_media_target(text, msg)
        local_path = download_telegram_file(file_id, download_target)
        if not local_path:
            self.reply(chat_id, f"Could not download {media_label}. Try again.")
            return True

        # Build the media text based on type
        media_text = self._format_media_text(incoming, local_path, media_label)
        if media_text is None:
            return True  # Voice with transcript was already handled

        if text:
            media_text = f"{text}\n\n{media_text}"
        self._route_media_message(media_text, text, chat_id, msg_id, msg=msg)
        return True

    def _format_media_text(self, incoming: 'IncomingMessage',
                           local_path: str, media_label: str) -> str | None:
        """Format the text description for a downloaded media file.

        Returns the formatted text, or None if the message was already handled
        (e.g., voice with successful transcription).
        """
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        msg = incoming.raw_msg

        if media_label == "GIF":
            return f"Manager sent GIF: `{local_path}`"

        if media_label == "image":
            return f"Manager sent image: `{local_path}`"

        if media_label == "file" and incoming.document:
            file_name = incoming.document.get("file_name", "unknown")
            file_size = incoming.document.get("file_size", 0)
            mime_type = incoming.document.get("mime_type", "unknown")
            size_str = format_file_size(file_size)
            return f"Manager sent file: {file_name} ({size_str}, {mime_type})\nPath: `{local_path}`"

        if incoming.audio:
            title = incoming.audio.get("title", incoming.audio.get("file_name", "audio"))
            duration = incoming.audio.get("duration", 0)
            return f"Manager sent audio: {title} ({duration}s)\nPath: `{local_path}`"

        if incoming.voice:
            duration = incoming.voice.get("duration", 0)
            transcript = transcribe_voice(local_path)
            if transcript:
                self.reply(chat_id, f"🎤 _{transcript}_", msg_id)
                routed = f"{text}\n\n{transcript}" if text else transcript
                self._route_media_message(routed, text or transcript, chat_id, msg_id, msg=msg)
                return None  # Already handled
            return f"Manager sent voice message: ({duration}s)\nPath: `{local_path}`"

        if incoming.video:
            duration = incoming.video.get("duration", 0)
            file_name = incoming.video.get("file_name", "video")
            return f"Manager sent video: {file_name} ({duration}s)\nPath: `{local_path}`"

        if incoming.video_note:
            duration = incoming.video_note.get("duration", 0)
            return f"Manager sent video note: ({duration}s)\nPath: `{local_path}`"

        if incoming.sticker:
            emoji = incoming.sticker.get("emoji", "")
            return f"Manager sent sticker: {emoji}\nPath: {local_path}"

        return f"Manager sent media: {local_path}"

    def _route_text_message(self, incoming: 'IncomingMessage') -> None:
        """Route a text-only message: commands, @mentions, reply-to, or active worker."""
        global admin_chat_id
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        msg = incoming.raw_msg

        if admin_chat_id is None:
            admin_chat_id = chat_id
            save_last_chat_id(chat_id)
            _log(_LOG_INFO, "admin", f"Admin registered: {chat_id}")

        if not state["startup_notified"]:
            state["startup_notified"] = True
            self.send_startup_message(chat_id)

        if chat_id != admin_chat_id:
            _log(_LOG_WARN, "bridge", f"Rejected non-admin: {chat_id}")
            return

        save_last_chat_id(chat_id)

        if text.startswith("/"):
            if self.handle_command(text, chat_id, msg_id):
                _last_mention["target"] = None
                _last_mention["count"] = 0
                return

        if re.match(r'^\s*@all(?:\s|[:,]|$)', text, re.IGNORECASE):
            self.route_to_all(text, chat_id, msg_id)
            self._reset_mention_streak()
            return

        # Extract reply context
        reply_context = ""
        reply_context_ts = None
        reply_to = msg.get("reply_to_message")
        if reply_to:
            reply_context, reply_context_ts = self.get_reply_context(reply_to)

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

        # Reply-to worker message without @mention routes to that worker
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            routed_text = text
            if reply_context:
                routed_text = self.format_reply_context(text, reply_context, reply_context_ts)
            self.route_message(reply_worker, routed_text, chat_id, msg_id, one_off=True)
            self._reset_mention_streak()
            return

        # No @mentions → route to focused worker
        self._reset_mention_streak()
        routed_text = text
        if reply_context:
            routed_text = self.format_reply_context(text, reply_context, reply_context_ts)
        self.route_to_active(routed_text, chat_id, msg_id)

    def handle_command(self, text: str, chat_id: int | str, msg_id: int) -> bool:
        """Parse and dispatch a slash command."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Adding a new command = adding one entry to self._commands in __init__.
        handler = self._commands.get(cmd)
        if handler:
            return handler(arg, chat_id, msg_id)

        if cmd in BLOCKED_COMMANDS:
            self.reply(chat_id, f"{cmd} is interactive and not supported here.", outcome="Needs decision")
            return True

        # Implicit worker focus: /name routes to worker
        worker_name = cmd[1:]
        registered = self.workers.get_registered_sessions()
        if worker_name in registered:
            prev_focus = state["active"]
            state["active"] = worker_name
            save_last_active(worker_name)
            if not arg:
                return True
            if prev_focus != worker_name:
                self.transport.send_text(chat_id, f"Now talking to {worker_name.capitalize()}.")
            self.route_message(worker_name, arg, chat_id, msg_id, one_off=False)
            return True

        return False


    def cmd_pilot(self, name: str, chat_id: ChatId) -> bool:
        """Handle the /pilot command — open a terminal viewer session."""
        if not name:
            self.reply(chat_id, "Usage: /pilot <name> [name2 ...]", outcome="Needs decision")
            return True
        names = name.lower().strip().split()
        prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
        pilot_port = os.environ.get("PILOT_PORT", "10170")
        import urllib.request, json as _json
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
                with _urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp:
                    _json.loads(resp.read())
                enabled.append(n)
                session_names.append(session_name)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                errors.append(f"{n}: {e}")
        if not enabled:
            self.reply(chat_id, f"Pilot error: {'; '.join(errors)}", outcome="Needs decision")
            return True
        ts = time.strftime("%m%d-%H%M", time.gmtime(_clock.time()))
        if len(enabled) <= 3:
            slug = "-".join(enabled) + "-" + ts
        else:
            slug = f"team{len(enabled)}-{ts}"
        try:
            payload = _json.dumps({"slug": slug, "sessions": session_names, "ttl": 1800}).encode()
            req = urllib.request.Request(
                f"http://localhost:{pilot_port}/api/grid-session",
                data=payload, method="POST",
                headers={"Content-Type": "application/json"})
            with _urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp:
                _json.loads(resp.read())
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")
        host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "localhost"
        pilot_url = f"http://{host}:{pilot_port}/grid/{_urlquote(slug)}"
        names_str = ", ".join(enabled)
        msg = f"✈️ Pilot: {names_str} (30min)\n{pilot_url}"
        if errors:
            msg += f"\n⚠️ Failed: {'; '.join(errors)}"
        self.reply(chat_id, msg)
        return True



    def cmd_rewind(self, name: str, chat_id: ChatId) -> bool:
        """Handle the /rewind command — generate a transcript viewer link."""
        if not name:
            self.reply(chat_id, "Usage: /rewind <name>\n/rewind team — view team chat", outcome="Needs decision")
            return True
        name = name.lower().strip()
        import secrets
        token = secrets.token_urlsafe(32)
        base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
        # Team chat viewer
        if name in ("team", "--team"):
            REWIND_TOKENS[token] = {"name": "__team__", "expires_at": _clock.time() + REWIND_TIMEOUT}
            url = f"{base_url}/team-chat?token={token}"
            try:
                html_content = _render_team_chat_html(
                    per_page=200, token=token,
                    live_base_url=f"{base_url}/team-chat")
                snap_path = "/tmp/rewind-team.html"
                with open(snap_path, "w") as f:
                    f.write(html_content)
                serve_url = _beast_serve_deploy(snap_path, "rewind-team")
                if serve_url:
                    self.reply(chat_id, f"\U0001f4ac Team chat\n{serve_url}")
                    return True
            except OSError as e:
                _log(_LOG_WARN, "bridge", f"Team chat snapshot deploy failed: {e}")
            self.reply(chat_id, f"\U0001f4ac Team chat\n{url}")
            return True
        REWIND_TOKENS[token] = {"name": name, "expires_at": _clock.time() + REWIND_TIMEOUT}
        url = f"{base_url}/transcript/{name}?token={token}"
        try:
            # Pass live_base_url so pagination/search links point to the bridge
            # endpoint (the static snapshot can't handle query params itself)
            html_content = _render_transcript_html(
                name, per_page=200, token=token,
                live_base_url=f"{base_url}/transcript/{name}")
            snap_path = f"/tmp/rewind-{name}.html"
            with open(snap_path, "w") as f:
                f.write(html_content)
            serve_url = _beast_serve_deploy(snap_path, f"rewind-{name}")
            if serve_url:
                self.reply(chat_id, f"⏪ Rewind for {name}\n{serve_url}")
                return True
        except OSError as e:
            _log(_LOG_WARN, "bridge", f"Rewind snapshot deploy failed for {name}: {e}")
        self.reply(chat_id, f"⏪ Rewind for {name}\n{url}")
        return True

    def cmd_pr_review(self, arg: str, chat_id: ChatId) -> bool:
        """Handle the /pr command — open a PR review page."""
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
            r = _subprocess_runner.run(
                [sys.executable, str(script_path), arg, "--no-serve"],
                capture_output=True, text=True, timeout=PENDING_TIMEOUT)
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
            import secrets
            token = secrets.token_urlsafe(32)
            PR_REVIEW_TOKENS[token] = {"pr_num": pr_num, "owner": owner, "repo": repo, "expires_at": _clock.time() + 300}
            base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
            url = f"{base_url}/pr-review/{pr_num}?token={token}"
            self.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{url}")
        return True

    def cmd_focus(self, name: str, chat_id: ChatId) -> bool:
        """Handle the /focus command — switch the active worker."""
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

    def cmd_team(self, chat_id: ChatId) -> bool:
        """Handle the /team command — show all workers and their states."""
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
                    activity = _read_noninteractive_activity(name)

            worker_live[name] = {
                "backend": backend_name,
                "activity": activity,
                "context_pct": context_pct,
            }

        lines = format_team_lines(registered, state["active"], worker_live=worker_live)
        self.reply(chat_id, "\n".join(lines))
        return True


    def cmd_progress(self, chat_id: ChatId, arg: str = "") -> bool:
        # If a name is given, show that worker's progress
        """Handle the /progress command — show current worker activity."""
        if arg:
            target = arg.strip().lower().lstrip("@")
            registered = self.workers.get_registered_sessions()
            if target not in registered:
                self.reply(chat_id, f"Unknown worker: {target}. Check /team for who's available.")
                return True
            name = target
        elif not state["active"]:
            self.reply(chat_id, "No one assigned. Who should I talk to? Use /team or /focus <name>.")
            return True
        else:
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

        # Read live activity from tmux pane (or adapter status for non-interactive)
        activity = None
        context_pct = None
        raw_lines = None
        if is_tmux_alive and ready:
            if backend.is_interactive:
                activity, context_pct, raw_lines = _read_tmux_activity(tmux_name, host=host)
            else:
                activity = _read_noninteractive_activity(name)

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




    # ── Remote Restart ──────────────────────────────────────────────


    # ── Restart All (sequential) ──────────────────────────────────




    # ── Teleport commands ──────────────────────────────────────────────────


















    def cmd_voice(self, arg: str, chat_id: ChatId) -> bool:
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

    def cmd_settings(self, chat_id: ChatId) -> bool:
        """Handle the /settings command — show bridge configuration."""
        def redact(s: str) -> str:
            """Redact sensitive tokens/keys from a string for display."""
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






    def route_to_active(self, text: str, chat_id: int | str, msg_id: int) -> None:
        """Route a text message to the currently focused worker."""
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

    def route_to_all(self, text: str, chat_id: int | str, msg_id: int) -> None:
        """Broadcast a text message to all active workers."""
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

    def route_message(self, session_name: str, text: str, chat_id: int | str, msg_id: int, one_off: bool=False) -> None:
        """Deliver a text message to a specific worker via tmux or pipe."""
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

        # Non-interactive backpressure: atomic check+set to prevent races
        if not backend.is_interactive and not try_set_pending(session_name, chat_id):
            self.reply(chat_id, f"{session_name.capitalize()} is still working on the previous request. Wait for a response or use /pause.")
            return

        # Prefix manager messages so workers can distinguish from inter-worker messages.
        # Skip if text already has a "Manager sent ..." prefix (media messages).
        if not text.startswith("Manager sent "):
            text = f"manager: {text}"

        _log(_LOG_INFO, "dispatch", f"chat_id={chat_id} -> {session_name}: {text[:50]}...")

        if backend.is_interactive:
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
_TRANSCRIPT_SYNC: dict[str, Any] = {}
_TRANSCRIPT_SYNC_LOCK: threading.Lock = threading.Lock()

# Path to transcript-index.py script (same directory as bridge.py)
TRANSCRIPT_INDEX_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcript-index.py")
TEAM_CHAT_INDEX_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team-chat-index.py")
TEAM_CHAT_JSONL = "/tmp/team-memory-parsed-full.jsonl"
TEAM_CHAT_DB = "/tmp/team-chat-cache/team.db"
TEAM_CHAT_MEDIA_DIR = os.path.expanduser("~/team/exports/chat-full")


def _render_md_to_html(md_text: str) -> str:
    """Simple markdown to HTML renderer for file previews."""
    import re as _re, html as _html
    html_out = _html.escape(md_text)
    # Headers
    html_out = _re.sub(r'^######\s+(.+)$', r'<h6>\1</h6>', html_out, flags=_re.MULTILINE)
    html_out = _re.sub(r'^#####\s+(.+)$', r'<h5>\1</h5>', html_out, flags=_re.MULTILINE)
    html_out = _re.sub(r'^####\s+(.+)$', r'<h4>\1</h4>', html_out, flags=_re.MULTILINE)
    html_out = _re.sub(r'^###\s+(.+)$', r'<h3>\1</h3>', html_out, flags=_re.MULTILINE)
    html_out = _re.sub(r'^##\s+(.+)$', r'<h2>\1</h2>', html_out, flags=_re.MULTILINE)
    html_out = _re.sub(r'^#\s+(.+)$', r'<h1>\1</h1>', html_out, flags=_re.MULTILINE)
    # Code blocks (fenced)
    html_out = _re.sub(r'```[a-z]*\n(.*?)```', r'<pre class="md-code"><code>\1</code></pre>', html_out, flags=_re.DOTALL)
    # Inline code
    html_out = _re.sub(r'`([^`]+)`', r'<code class="md-inline">\1</code>', html_out)
    # Bold + italic
    html_out = _re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', html_out)
    html_out = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_out)
    html_out = _re.sub(r'\*(.+?)\*', r'<em>\1</em>', html_out)
    # Horizontal rule
    html_out = _re.sub(r'^---+$', r'<hr>', html_out, flags=_re.MULTILINE)
    # Unordered lists
    html_out = _re.sub(r'^[-*]\s+(.+)$', r'<li>\1</li>', html_out, flags=_re.MULTILINE)
    # Links
    html_out = _re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank">\1</a>', html_out)
    # Line breaks → paragraphs (double newline)
    html_out = _re.sub(r'\n{2,}', '</p><p>', html_out)
    # Single newlines → <br>
    html_out = html_out.replace('\n', '<br>')
    # Clean up br inside pre
    html_out = _re.sub(r'(<pre[^>]*>.*?</pre>)', lambda m: m.group(0).replace('<br>', '\n'), html_out, flags=_re.DOTALL)
    # Clean up br inside headers
    for tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
        html_out = _re.sub(rf'(<{tag}>.*?</{tag}>)', lambda m: m.group(0).replace('<br>', ''), html_out, flags=_re.DOTALL)
    return f'<p>{html_out}</p>'


def _render_csv_to_html(csv_text: str) -> str:
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


def _run_team_chat_query(query_type: str, **kwargs: Any) -> dict[str, Any] | None:
    """Run team-chat-index.py via subprocess. Returns parsed JSON dict or None on failure."""
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
        r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "bridge", f"Team chat query error: {e}")
    return None


def _run_transcript_query(jsonl_path: str, sid: str, query: str, host: str | None=None, **kwargs: Any) -> dict[str, Any] | None:
    """Run transcript-index.py locally or via SSH. Returns parsed JSON dict or None on failure."""
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
            r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_LARGE_TRANSFER)
        else:
            r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "transcript", f"Transcript query error: {e}")
    return None


def _start_transcript_sync(name: str, host: str, remote_path: str, local_tmp: Path, key: str) -> None:
    """Background thread: rsync transcript from remote host with progress tracking."""
    try:
        with _TRANSCRIPT_SYNC_LOCK:
            _TRANSCRIPT_SYNC[key] = {"status": "syncing", "progress": "Connecting to remote host...",
                                     "started": _clock.time(), "path": None, "error": None}
        # First get remote file size for progress
        r = _subprocess_runner.run(["ssh", host, f"stat -f%z '{remote_path}' 2>/dev/null || stat -c%s '{remote_path}' 2>/dev/null"],
                           capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
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
        proc = _subprocess_runner.popen(
            ["rsync", "-az", f"{host}:{remote_path}", str(local_tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Poll local file size while rsync runs
        while proc.poll() is None:
            _clock.sleep(DELAY_STARTUP)
            try:
                if local_tmp.exists() and remote_size > 0:
                    local_size = local_tmp.stat().st_size
                    pct = min(99, int(local_size * 100 / remote_size))
                    with _TRANSCRIPT_SYNC_LOCK:
                        _TRANSCRIPT_SYNC[key]["progress"] = f"Syncing... {pct}% ({local_size / 1_048_576:.1f} / {remote_size / 1_048_576:.1f} MB)"
                        _TRANSCRIPT_SYNC[key]["pct"] = pct
            except (OSError, ValueError) as exc:
                _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")

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
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        with _TRANSCRIPT_SYNC_LOCK:
            _TRANSCRIPT_SYNC[key] = {"status": "error", "progress": "Sync failed",
                                     "started": _TRANSCRIPT_SYNC.get(key, {}).get("started", 0),
                                     "path": None, "error": str(e)[:200]}


def _resolve_transcript_path(name: str, session_id: str | None = None) -> tuple[str | None, str | None]:
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
                        _log(_LOG_WARN, "transcript", f"sync failed for {name}:{sid}: {err}")
                        return None, sid, cwd
                    else:
                        # Start background sync
                        t = threading.Thread(target=_start_transcript_sync,
                                             args=(name, host, remote_path, local_tmp, sync_key),
                                             daemon=True)
                        t.start()
                        return "syncing", sid, cwd
            except (OSError, subprocess.SubprocessError) as exc:
                _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")

    if transcript_path.exists():
        return transcript_path, sid, cwd
    return None, sid, cwd


def _parse_transcript_entries(transcript_path: str) -> list[dict[str, Any]]:
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
    name_hash = hash(name) & 0xFFFFFFFF
    hue = (name_hash % 36) * 10  # 0-350 in steps of 10
    sat = 55 + (name_hash >> 6 & 1) * 15  # 55 or 70
    shape_idx = (name_hash >> 7) % 6
    accent_idx = (name_hash >> 10) % 5
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


def _detect_message_author(text: str) -> AuthorDetection:
    """Detect author from message prefix like 'ryo: message'.

    Returns AuthorDetection(author, avatar_html, display_text).
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
            return AuthorDetection("manager", _MANAGER_AV, rest or stripped)
        if prefix in _TEAM_MEMBERS:
            return AuthorDetection(prefix, _generate_member_avatar(prefix), rest or stripped)
    # Default: manager avatar, full text
    return AuthorDetection("manager", _MANAGER_AV, stripped)


# ── Transcript rendering SVG constants ──────────────────────────────────
# Module-level to avoid re-creation on every _transcript_entry_to_html call.

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


def _transcript_entry_to_html(entry: dict[str, Any], esc: Callable[[str], str], tool_results: dict[str, Any] | None = None) -> str:
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
    _chev = _TRANSCRIPT_CHEVRON_SVG
    _claude_av = _TRANSCRIPT_CLAUDE_AVATAR
    _tool_svgs = _TRANSCRIPT_TOOL_SVGS
    _default_tool_svg = _TRANSCRIPT_DEFAULT_TOOL_SVG
    parts: list[str] = []

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
            pass  # intentional no-op: tool_result merged into tool_use

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
                        file_path_esc = esc(ti.get("file_path", "?"))
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
                        pp = file_path_esc.rsplit("/", 1)
                        fp_html = f'<span class="fp-dir">{esc(pp[0])}/</span>{esc(pp[1])}' if len(pp) > 1 else esc(file_path_esc)
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
                        file_path_html = f'<span class="fp-dir">{dp}/</span>{bp}' if dp else bp
                        if tr_esc and not _skip_result:
                            # File tool with result → expandable
                            err_cls = " act-err" if tr_err else ""
                            parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="fp">{file_path_html}</span>{_chev}</summary><div class="act-body"><pre class="t-out">{tr_esc}</pre></div></details>')
                        else:
                            parts.append(f'<div class="chip">{tool_svg}<span class="fp">{file_path_html}</span></div>')
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


def _transcript_stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
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
                file_path = ti.get("file_path", "")
                if file_path:
                    files_modified.add(file_path)
                n_old = len(ti.get("old_string", "").splitlines(True))
                n_new = len(ti.get("new_string", "").splitlines(True))
                overlap = min(n_old, n_new)
                lines_mod += overlap
                lines_del += n_old - overlap
                lines_add += n_new - overlap
            elif c.get("name") in ("Write", "Read", "Edit"):
                file_path = ti.get("file_path", "")
                if file_path:
                    files_modified.add(file_path)
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
        except (ValueError, TypeError, KeyError) as exc:
            _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
    return {"n_user": n_user, "n_tool": n_tool, "n_edit": n_edit,
            "lines_add": lines_add, "lines_del": lines_del,
            "lines_mod": lines_mod, "n_files": len(files_modified), "model": model,
            "version": version, "git_branch": git_branch,
            "first_ts": first_ts, "last_ts": last_ts,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration": duration_str}


def _render_transcript_loading(name: str, sid: str, token: str, sync_key: str) -> str:
    """Render a loading page while transcript syncs from remote host."""
    import html as html_mod
    esc = html_mod.escape
    with _TRANSCRIPT_SYNC_LOCK:
        info = _TRANSCRIPT_SYNC.get(sync_key, {})
    status = info.get("status", "syncing")
    progress = esc(info.get("progress", "Starting sync..."))
    pct = info.get("pct", 0)
    elapsed = int(_clock.time() - info.get("started", _clock.time()))
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



# ── Team chat HTML helpers (decomposed from _render_team_chat_html) ──


_TEAM_CHAT_CSS = """<style>
:root {
  --bg:#0b0d0b; --fg:#e5e5e0; --border:rgba(135,139,134,.12); --muted:#9ca49c;
  --user-bg:rgba(255,255,255,.04); --code-bg:#1a1c1a; --link:#75dbf0; --radius:6px;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
}
@media(prefers-color-scheme:light){
  :root{--bg:#fafaf8;--fg:#1a1a1a;--muted:#595959;--border:rgba(135,139,134,.2);
    --user-bg:rgba(0,0,0,.03);--code-bg:#f4f4f0;--link:#0969da;}
}
*{margin:0;padding:0;box-sizing:border-box}
html{font-size:14px}
body{font-family:var(--sans);background:var(--bg);color:var(--fg);line-height:1.6;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:48rem;margin:0 auto;padding:24px 16px 80px}
header{border-bottom:1px solid var(--border);padding-bottom:16px;margin-bottom:20px}
h1{font-size:1.3rem;font-weight:700}
.meta{color:var(--muted);font-size:.85rem;margin-top:4px}
/* Search */
.search-bar{display:flex;gap:8px;margin-bottom:16px}
.search-bar input{flex:1;background:var(--code-bg);border:1px solid var(--border);
  border-radius:var(--radius);padding:8px 12px;color:var(--fg);font-size:.9rem;
  font-family:var(--sans);outline:none}
.search-bar input:focus{border-color:var(--link)}
.search-bar button{background:var(--code-bg);border:1px solid var(--border);
  border-radius:var(--radius);padding:8px 16px;color:var(--fg);cursor:pointer;
  font-family:var(--sans);font-size:.9rem}
.search-bar button:hover{border-color:var(--muted)}
.search-info{background:rgba(117,219,240,.06);border:1px solid rgba(117,219,240,.15);
  border-radius:var(--radius);padding:8px 12px;margin-bottom:16px;font-size:.85rem;color:var(--link)}
/* Pagination */
.pg{display:flex;flex-wrap:wrap;align-items:center;gap:4px;margin:16px 0;font-size:.85rem}
.pg-btn{padding:4px 10px;border:1px solid var(--border);border-radius:var(--radius);
  color:var(--fg);text-decoration:none;transition:border-color .15s}
.pg-btn:hover{border-color:var(--muted)}
.pg-cur{background:var(--link);color:#000;border-color:var(--link);font-weight:600}
.pg-dis{opacity:.3;pointer-events:none}
.pg-info{margin-left:8px;color:var(--muted)}
/* Chat messages */
.thread{display:flex;flex-direction:column;gap:4px}
.chat-msg{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:8px 8px;
  border-radius:8px;transition:background .2s}
.chat-msg:target{background:rgba(117,219,240,.1)}
.chat-msg:hover{background:var(--user-bg)}
.chat-body{min-width:0}
.u-av{width:28px;height:28px;border-radius:50%;overflow:hidden;flex-shrink:0;margin-top:2px}
.u-av img{width:100%;height:100%;object-fit:cover;border-radius:50%}
.u-av svg{width:100%;height:100%}
.u-name{font-weight:600;font-size:.85rem;margin-right:6px}
.ts{font-size:.75rem;color:var(--muted)}
.chat-text{white-space:pre-wrap;word-break:break-word;font-size:.9rem;line-height:1.6;margin-top:2px}
.chat-text a{color:var(--link)}
/* Media */
.media-wrap{margin:4px 0 6px}
.chat-photo{max-width:100%;border-radius:8px;display:block;cursor:pointer;
  border:1px solid var(--border)}
.chat-photo:hover{opacity:.9}
.file-dl{display:block;font-size:.75rem;color:var(--muted);margin-top:2px;text-decoration:none}
.file-dl:hover{color:var(--link)}
.file-card{background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden}
.file-header{display:flex;align-items:center;gap:6px;padding:8px 10px;font-size:.85rem;
  color:var(--fg);cursor:pointer;list-style:none}
.file-header::-webkit-details-marker{display:none}
.file-header:hover{background:var(--user-bg)}
.file-header .chev{margin-left:auto;width:14px;height:14px;color:var(--muted);
  transition:transform .15s;flex-shrink:0}
details.file-card[open] .chev{transform:rotate(90deg)}
details.file-card[open] .file-header{border-bottom:1px solid var(--border)}
.file-icon{font-size:1rem}
.file-dl-btn{margin-left:auto;color:var(--muted);text-decoration:none;font-size:1rem;
  padding:0 4px;border-radius:4px}
.file-dl-btn:hover{color:var(--link);background:rgba(117,219,240,.08)}
.file-preview{width:100%;height:300px;border:0;background:var(--bg);color:var(--fg)}
.file-preview-pdf{height:500px}
/* Inline file content */
.file-body{padding:12px 14px;font-size:.85rem;line-height:1.6;color:var(--fg);
  max-height:500px;overflow:auto;border-top:1px solid var(--border)}
.file-body-md{font-family:var(--sans)}
.file-body-md h1,.file-body-md h2,.file-body-md h3,.file-body-md h4{margin:12px 0 6px;font-weight:600}
.file-body-md h1{font-size:1.3rem}.file-body-md h2{font-size:1.1rem}.file-body-md h3{font-size:.95rem}
.file-body-md p{margin:4px 0}.file-body-md li{margin:2px 0 2px 16px;list-style:disc}
.file-body-md hr{border:0;border-top:1px solid var(--border);margin:10px 0}
.file-body-md a{color:var(--link)}.file-body-md strong{font-weight:600}
.file-body-md .md-code{background:var(--code-bg);padding:8px 10px;border-radius:4px;display:block;
  font-family:var(--mono);font-size:.8rem;overflow-x:auto;white-space:pre;margin:6px 0}
.file-body-md .md-inline{background:var(--code-bg);padding:1px 4px;border-radius:3px;font-family:var(--mono);font-size:.8rem}
.file-body-code{font-family:var(--mono);white-space:pre;overflow-x:auto;margin:0;
  background:var(--code-bg);border-top:1px solid var(--border)}
.file-body-code code{font-size:.8rem;line-height:1.5}
.file-body-text{font-family:var(--mono);white-space:pre-wrap;word-break:break-word;margin:0;
  background:var(--code-bg);border-top:1px solid var(--border)}
.file-body-csv{overflow-x:auto;border-top:1px solid var(--border)}
.file-body-csv table{width:100%;border-collapse:collapse;font-size:.8rem}
.file-body-csv th,.file-body-csv td{padding:4px 8px;border:1px solid var(--border);text-align:left}
.file-body-csv th{background:var(--card);font-weight:600;position:sticky;top:0}
.file-card-link{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;
  background:var(--code-bg);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--fg);text-decoration:none;font-size:.85rem}
.file-card-link:hover{border-color:var(--link);color:var(--link)}
/* Reply context */
.reply-ctx{display:block;border-left:3px solid var(--link);padding:2px 8px;margin:2px 0 4px;
  font-size:.8rem;color:var(--muted);text-decoration:none;border-radius:2px;
  background:rgba(117,219,240,.04);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.reply-ctx:hover{background:rgba(117,219,240,.1);color:var(--fg)}
.reply-name{font-weight:600;color:var(--link);margin-right:4px}
/* Context link (search → full view) */
.ctx-link{font-size:.75rem;color:var(--muted);text-decoration:none;margin-left:4px;
  opacity:.6;transition:opacity .15s}
.ctx-link:hover{opacity:1;color:var(--link)}
/* Jump buttons */
.jump{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:6px}
.jump a{width:32px;height:32px;border-radius:50%;background:var(--code-bg);border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;color:var(--muted);text-decoration:none;font-size:1rem;
  transition:border-color .15s}
.jump a:hover{border-color:var(--muted);color:var(--fg)}
</style>"""


_TEAM_CHAT_JS = """<script>
// Render timestamps in browser timezone
document.querySelectorAll('.ts[data-ts]').forEach(el => {
  try {
    const d = new Date(el.dataset.ts);
    if (!isNaN(d)) el.textContent = d.toLocaleString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  } catch(e) {}
});
// Scroll to target anchor if present
if (location.hash) {
  const el = document.querySelector(location.hash);
  if (el) setTimeout(() => el.scrollIntoView({behavior:'smooth',block:'center'}), 100);
}
</script>"""


def _team_chat_html_head(title: str) -> str:
    """Return the HTML head section with CSS for the team chat page."""
    import html as html_mod
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>' + html_mod.escape(title) + '</title>\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">\n'
        + _TEAM_CHAT_CSS
        + '\n</head>'
    )


def _team_chat_html_messages(messages: list[dict[str, Any]], msg_by_id: dict[str, Any], reply_cache: dict[str, Any],
                             search_query: str, per_page: int, token: str,
                             url_prefix: str, qs_base: str,
                             esc: "Callable[[str], str]") -> str:
    """Return rendered HTML for all team chat message blocks."""
    _url_prefix = url_prefix
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
                    rurl = f"{_url_prefix}page={rpage}&{qs_base}#msg-{reply_to}" if qs_base else f"{_url_prefix}page={rpage}#msg-{reply_to}"
                    rsender = esc(rinfo.get("display_sender", ""))
                    rtext_raw = rinfo.get("text", "")
                    rtext = esc(rtext_raw[:120])
                    if len(rtext_raw) > 120:
                        rtext += "..."
                    reply_html = (f'<a class="reply-ctx" href="{rurl}">'
                                  f'<span class="reply-name">{rsender}</span> {rtext}</a>')

        # Avatar
        if sender == "manager":
            avatar = _MANAGER_AV
        elif sender in _TEAM_MEMBERS:
            avatar = _generate_member_avatar(sender)
        else:
            avatar = _generate_member_avatar(sender or "system")

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
            ctx_link = f' <a class="ctx-link" href="{_url_prefix}{ctx_qs}#msg-{msg_id}" title="View in context">\u2197</a>'

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
                except OSError as exc:
                    _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
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
            f'{avatar}<div class="chat-body">'
            f'{name_html} {ts_html}{ctx_link}'
            f'{reply_html}'
            f'{media_html}'
            f'<div class="chat-text">{text}</div>'
            f'</div></div>'
        )


    return "".join(blocks)


def _team_chat_html_search_panel(search_val: str, search_result: str,
                                 live_base_url: str, token: str,
                                 per_page: int,
                                 esc: "Callable[[str], str]") -> str:
    """Return the search form and result info HTML for team chat."""
    action_attr = f' action="{esc(live_base_url)}"' if live_base_url else ''
    per_page_input = (
        f'<input type="hidden" name="per_page" value="{per_page}">'
        if per_page != 50 else ''
    )
    return (
        f'<form class="search-bar" method="get"{action_attr}>\n'
        f'<input type="text" name="q" value="{search_val}" placeholder="Search messages...">\n'
        f'<button type="submit">Search</button>\n'
        f'<input type="hidden" name="token" value="{esc(token)}">\n'
        f'{per_page_input}\n'
        f'</form>\n\n'
        f'{search_result}'
    )


def _team_chat_html_footer_js() -> str:
    """Return the JavaScript section for the team chat page."""
    return _TEAM_CHAT_JS


def _render_team_chat_html(page: int | None = None, per_page: int = 50,
                           search_query: str = "", token: str = "",
                           live_base_url: str = "") -> str:
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
    _url_prefix = live_base_url + "?" if live_base_url else "?"

    def page_url(p: str) -> str:
        """Build a full URL for a transcript/PR page path."""
        parts = [f"page={p}"]
        if qs_base:
            parts.append(qs_base)
        return _url_prefix + "&".join(parts)

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

    blocks_html = _team_chat_html_messages(
        messages, msg_by_id, reply_cache, search_query,
        per_page, token, _url_prefix, qs_base, esc)
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

    head = _team_chat_html_head("Team Chat")
    search_panel = _team_chat_html_search_panel(
        search_val, search_result, live_base_url, token, per_page, esc)
    footer_js = _team_chat_html_footer_js()

    return (head
            + '\n<body>\n<div class="wrap">\n<header>\n'
            '<h1>Team Chat</h1>\n'
            '<div class="meta">Telegram chat history</div>\n'
            '</header>\n\n'
            + search_panel + '\n'
            + nav_html + '\n\n'
            '<div class="thread" id="thread">\n'
            + blocks_html + '\n</div>\n\n'
            + nav_html + '\n</div>\n\n'
            '<div class="jump">\n'
            '<a href="#" onclick="window.scrollTo(0,0);return false">&uarr;</a>\n'
            '<a href="#" onclick="window.scrollTo(0,document.body.scrollHeight);return false">&darr;</a>\n'
            '</div>\n\n'
            + footer_js + '\n</body>\n</html>')


def _transcript_html_head(name: str, esc: Callable[[str], str]) -> str:
    """Return the DOCTYPE, head, CSS and opening body/layout tags."""
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
/* Live updates banner */
.live-banner{{position:sticky;top:0;z-index:40;padding:10px 16px;
  background:var(--link);color:#fff;text-align:center;cursor:pointer;
  font-size:.85rem;font-weight:500;border-radius:0 0 var(--radius) var(--radius);
  box-shadow:0 2px 8px rgba(0,0,0,.2);transition:opacity .2s}}
.live-banner:hover{{opacity:.9}}
</style>
</head>
<body>
<div class="wrap">
<div class="main">
<div class="content">
'''


def _transcript_html_nav(name: str, stats: dict[str, Any],
                         prompts_filter_url: str,
                         esc: Callable[[str], str]) -> str:
    """Return the header bar with worker name and session metadata."""
    return f'''<header>
<div class="h-row"><h1>{esc(name)}</h1><button class="sb-toggle" onclick="document.querySelector('.sidebar').classList.toggle('sb-open')" title="Session info"><svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16"><path d="M0 8a8 8 0 1116 0A8 8 0 010 8zm8-6.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM6.5 7.75A.75.75 0 017.25 7h1a.75.75 0 01.75.75v2.75h.25a.75.75 0 010 1.5h-2a.75.75 0 010-1.5h.25v-2h-.25a.75.75 0 01-.75-.75zM8 6a1 1 0 110-2 1 1 0 010 2z"/></svg></button></div>
<div class="meta">
{"<span class='mi'><svg viewBox=\"0 0 16 16\" fill=\"currentColor\"><path d=\"M8 16A8 8 0 108 0a8 8 0 000 16zm.25-11.75v4l3 1.5-.5 1-3.5-1.75v-4.75h1z\"/></svg>" + esc(stats["first_ts"][:10]) + "</span>" if stats["first_ts"] else ""}
{"<span class='mi'><svg viewBox='0 0 16 16' fill='currentColor'><path d='M8 1.5c-2.363 0-4 1.69-4 3.75 0 .984.424 1.625.984 2.304l.214.253c.223.264.47.556.673.848.284.411.537.896.621 1.49a.75.75 0 01-1.484.211c-.04-.282-.163-.547-.37-.847a8.456 8.456 0 00-.542-.68c-.084-.1-.173-.205-.268-.32C3.201 7.75 2.5 6.766 2.5 5.25 2.5 2.31 4.863 0 8 0s5.5 2.31 5.5 5.25c0 1.516-.701 2.5-1.328 3.259-.095.115-.184.22-.268.319-.207.245-.383.453-.541.681-.208.3-.33.565-.37.847a.75.75 0 01-1.485-.212c.084-.593.337-1.078.621-1.489.203-.292.45-.584.673-.848l.213-.253c.561-.679.985-1.32.985-2.304 0-2.06-1.637-3.75-4-3.75zM6 15.25a.75.75 0 01.75-.75h2.5a.75.75 0 010 1.5h-2.5a.75.75 0 01-.75-.75zM5.75 12a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-4.5z'/></svg>" + esc(stats["model"]) + "</span>" if stats["model"] else ""}
{"<span class='mi'><svg viewBox='0 0 16 16' fill='currentColor'><path d='M11.93 8.5a4.002 4.002 0 01-7.86 0H.75a.75.75 0 010-1.5h3.32a4.002 4.002 0 017.86 0h3.32a.75.75 0 010 1.5h-3.32zm-1.43-.75a2.5 2.5 0 10-5 0 2.5 2.5 0 005 0z'/></svg>" + esc(stats["git_branch"]) + "</span>" if stats["git_branch"] else ""}
<a class="mi" href="{prompts_filter_url}" style="cursor:pointer" title="Filter to prompts only"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0110.25 10H7.061l-2.574 2.573A1.458 1.458 0 012 11.543V10h-.25A1.75 1.75 0 010 8.25v-5.5C0 1.784.784 1 1.75 1z"/></svg>{stats["n_user"]} prompts</a>
<span class="mi"><svg viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304z"/></svg>{stats["n_tool"]} tool call{"s" if stats["n_tool"] != 1 else ""}</span>
</div>
</header>
'''


def _transcript_html_entries(page_entries: list[dict[str, Any]],
                             tool_results: dict[str, dict[str, Any]],
                             search_val: str,
                             filter_banner: str, search_result: str,
                             nav_html: str, live_base_url: str,
                             name: str, token: str, search_query: str,
                             total: int, per_page: int, page: int,
                             total_pages: int,
                             esc: Callable[[str], str],
                             session_id: str | None,
                             filter_mode: str) -> str:
    """Return the search form, filter/search banners, thread content and pagination."""
    _tool_results = tool_results
    # Render blocks — group consecutive assistant entries into a turn-body
    # No avatar/label on assistant turns (matches AmpCode: only user has avatar)
    blocks = []
    in_assistant_turn = False

    # When live_base_url is set (static snapshot), links point to bridge endpoint
    _url_prefix = live_base_url + "?" if live_base_url else "?"

    # Build context URL for search mode (click message → jump to full transcript)
    def _ctx_url(entry: dict[str, Any]) -> str:
        """Build a context URL for search results — links to full transcript."""
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
        return f'{_url_prefix}{"&".join(ctx_qs)}#e-{idx}'

    for entry in page_entries:
        etype = entry.get("type", "")
        role = entry.get("message", {}).get("role", "")
        # Skip tool_result entries — they're merged into tool_use blocks
        is_tool_result = (etype == "user" and role == "user" and
                          isinstance(entry.get("message", {}).get("content"), list) and
                          any(c.get("type") == "tool_result" for c in entry.get("message", {}).get("content", []) if isinstance(c, dict)))
        if is_tool_result:
            continue
        entry_html = _transcript_entry_to_html(entry, esc, tool_results=_tool_results)
        if not entry_html:
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
            if curl:
                entry_html = f'<a class="ctx-wrap" href="{curl}"{anchor}>{entry_html}</a>'
            elif anchor:
                entry_html = f'<div{anchor}>{entry_html}</div>'
            blocks.append(entry_html)
    if in_assistant_turn:
        blocks.append('</div>')

    return f'''<form class="search-bar" method="get"{' action="' + esc(live_base_url) + '"' if live_base_url else ''}>
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
<div id="live-banner" class="live-banner" style="display:none"></div>
<div class="thread" id="thread"{' data-search="' + esc(search_query) + '"' if search_query else ''} data-total="{total}" data-name="{esc(name)}" data-token="{esc(token)}" data-updates-url="{esc(live_base_url) + '/updates' if live_base_url else '/transcript/' + esc(name) + '/updates'}" data-page-url="{esc(live_base_url) if live_base_url else '/transcript/' + esc(name)}" data-per-page="{per_page}" data-page="{page}" data-total-pages="{total_pages}">
{"".join(blocks)}
</div>
{nav_html}
'''


def _transcript_html_footer(sid: str, stats: dict[str, Any],
                              file_size_str: str, total: int,
                              page: int, total_pages: int,
                              esc: Callable[[str], str]) -> str:
    """Return the closing content div, sidebar panel, layout wrappers and jump buttons."""
    return f'''</div>
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
'''


def _transcript_html_search_js() -> str:
    """Return the JavaScript block for markdown rendering, search highlight and live updates."""
    return '''<script>
// Render markdown blocks with marked.js + highlight.js
marked.setOptions({
  highlight: function(code, lang) {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, {language: lang}).value;
    }
    return hljs.highlightAuto(code).value;
  },
  breaks: true,
  gfm: true
});
function decodeB64Utf8(b64) {
  var bin = atob(b64);
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder('utf-8').decode(bytes);
}
document.querySelectorAll('.markdown[data-md]').forEach(function(el) {
  try {
    var md = decodeB64Utf8(el.getAttribute('data-md'));
    el.innerHTML = marked.parse(md);
  } catch(e) {
    el.textContent = 'Error rendering markdown: ' + e.message;
  }
});
// Wrap tables in scroll containers
document.querySelectorAll('.a-text table').forEach(function(table) {
  var wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  table.parentNode.insertBefore(wrap, table);
  wrap.appendChild(table);
});
// Add copy buttons to code blocks
document.querySelectorAll('.a-text pre').forEach(function(pre) {
  var btn = document.createElement('button');
  btn.className = 'copy-btn';
  btn.textContent = 'Copy';
  btn.onclick = function() {
    var code = pre.querySelector('code');
    navigator.clipboard.writeText(code ? code.textContent : pre.textContent).then(function() {
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(function() { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
    });
  };
  pre.appendChild(btn);
});
// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '/') { e.preventDefault(); document.querySelector('.search-bar input').focus(); }
  if (e.key === 'Home') { window.scrollTo(0,0); }
  if (e.key === 'End') { window.scrollTo(0,document.body.scrollHeight); }
});
// Highlight search terms in thread content
(function() {
  var thread = document.getElementById('thread');
  var q = thread && thread.getAttribute('data-search');
  if (!q) return;
  var terms = q.split(/\\s+/).filter(function(t) { return t.length > 0; });
  if (!terms.length) return;
  var pattern = new RegExp('(' + terms.map(function(t) {
    return t.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
  }).join('|') + ')', 'gi');
  function walk(node) {
    if (node.nodeType === 3) {
      var text = node.textContent;
      if (!pattern.test(text)) return;
      pattern.lastIndex = 0;
      var frag = document.createDocumentFragment();
      var last = 0;
      var match;
      while ((match = pattern.exec(text)) !== null) {
        if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
        var mark = document.createElement('mark');
        mark.textContent = match[0];
        frag.appendChild(mark);
        last = pattern.lastIndex;
      }
      if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    } else if (node.nodeType === 1 && !/^(script|style|mark|code|pre)$/i.test(node.tagName)) {
      var children = Array.from(node.childNodes);
      for (var i = 0; i < children.length; i++) walk(children[i]);
    }
  }
  // Highlight in user messages and assistant text
  thread.querySelectorAll('.u-text, .a-text').forEach(function(el) { walk(el); });
})();
// Render timestamps in browser timezone
var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
document.querySelectorAll('.ts[data-ts]').forEach(function(el) {
  try {
    var d = new Date(el.getAttribute('data-ts'));
    if (!isNaN(d)) {
      var mon = months[d.getMonth()];
      var day = d.getDate();
      var h = String(d.getHours()).padStart(2,'0');
      var m = String(d.getMinutes()).padStart(2,'0');
      el.textContent = mon + ' ' + day + ' ' + h + ':' + m;
    }
  } catch(e) {}
});
// Live updates: poll for new entries, show banner when available
(function() {
  var thread = document.getElementById('thread');
  if (!thread) return;
  var total = parseInt(thread.getAttribute('data-total')) || 0;
  var updatesUrl = thread.getAttribute('data-updates-url');
  var pageUrl = thread.getAttribute('data-page-url');
  var token = thread.getAttribute('data-token');
  var perPage = thread.getAttribute('data-per-page') || '50';
  var curPage = parseInt(thread.getAttribute('data-page')) || 1;
  var totalPages = parseInt(thread.getAttribute('data-total-pages')) || 1;
  if (!updatesUrl || !token) return;
  // Only poll when viewing the last page (most recent entries)
  if (curPage < totalPages) return;
  var banner = document.getElementById('live-banner');
  var polling = true;
  var pollUrl = updatesUrl + '?token=' + encodeURIComponent(token) + '&since=' + total;
  function poll() {
    if (!polling) return;
    fetch(pollUrl).then(function(r) { return r.json(); }).then(function(d) {
      if (d.new > 0) {
        banner.textContent = d.new + ' new entr' + (d.new === 1 ? 'y' : 'ies') + ' — click to load';
        banner.style.display = 'block';
        polling = false;  // Stop polling once banner is shown
      } else {
        setTimeout(poll, 5000);
      }
    }).catch(function() {
      setTimeout(poll, 10000);  // Retry slower on error
    });
  }
  banner.addEventListener('click', function() {
    // Navigate to last page of transcript (fresh render with new entries)
    var url = pageUrl + '?token=' + encodeURIComponent(token) + '&per_page=' + perPage;
    window.location.href = url;
  });
  setTimeout(poll, 5000);  // Start polling after 5s
})();
</script>
</body>
</html>'''


def _render_transcript_html(name: str, session_id: str | None = None,
                            page: int | None = None, per_page: int = 50,
                            search_query: str = "", token: str = "",
                            filter_mode: str = "", search_sort: str = "relevance",
                            live_base_url: str = "") -> str:
    """Render a worker's transcript as polished HTML (ampcode.com style).

    Supports pagination (?page=N&per_page=50) and search (?q=term).
    filter_mode="prompts" shows only user messages.
    page=None means "show last page" (most recent entries).
    Uses marked.js for markdown and highlight.js for syntax highlighting.

    live_base_url: when set, pagination/search/filter links use this absolute
    URL prefix instead of relative URLs.  Used by /rewind static snapshots so
    links point back to the bridge's live /transcript/<name> endpoint.
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
        except OSError as exc:
            _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")

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


    # When live_base_url is set (static snapshot), links point to bridge endpoint
    _url_prefix = live_base_url + "?" if live_base_url else "?"

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

    def page_url(p: str) -> str:
        """Build a full URL for a transcript/PR page path."""
        parts = [f"page={p}"]
        if qs_base:
            parts.append(qs_base)
        return _url_prefix + "&".join(parts)

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
        sort_url = f'{_url_prefix}{"&".join(sort_qs)}'
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
    prompts_filter_url = _url_prefix + "&".join(_filt_qs) if _filt_qs else _url_prefix.rstrip("?")
    filter_banner = ""
    if filter_mode == "prompts":
        _clear_qs = [p for p in _filt_qs]  # already excludes filter=prompts
        _clear_url = _url_prefix + "&".join(_clear_qs) if _clear_qs else _url_prefix.rstrip("?")
        filter_banner = f'<div class="search-info">Showing prompts only — <a href="{_clear_url}">show all</a></div>'

    return (_transcript_html_head(name, esc)
            + _transcript_html_nav(name, stats, prompts_filter_url, esc)
            + _transcript_html_entries(
                page_entries, _tool_results, search_val,
                filter_banner, search_result, nav_html,
                live_base_url, name, token, search_query, total, per_page,
                page, total_pages, esc, session_id, filter_mode)
            + _transcript_html_footer(sid, stats, file_size_str, total,
                                       page, total_pages, esc)
            + _transcript_html_search_js())


# ── EndpointRouter + Handler: thin HTTP dispatch ──

class EndpointRouter:
    """Maps HTTP paths to handler functions via dispatch table.

    Supports POST, GET, and DELETE methods. Exact paths are checked first,
    then regex patterns in registration order (important for prefix collisions
    like /team-chat-media/ vs /team-chat).
    """

    def __init__(self) -> None:
        """Initialize per-method route tables."""
        self._post_exact: dict[str, Callable[..., None]] = {}
        self._post_patterns: list[tuple[re.Pattern[str], Callable[..., None]]] = []
        self._get_exact: dict[str, Callable[..., None]] = {}
        self._get_patterns: list[tuple[re.Pattern[str], Callable[..., None]]] = []
        self._delete_exact: dict[str, Callable[..., None]] = {}
        self._delete_patterns: list[tuple[re.Pattern[str], Callable[..., None]]] = []

    def post(self, path: str, handler: Callable[..., None]) -> None:
        """Register a POST handler for an exact path."""
        self._post_exact[path] = handler

    def post_pattern(self, pattern: str, handler: Callable[..., None]) -> None:
        """Register a POST handler for a regex path pattern."""
        self._post_patterns.append((re.compile(pattern), handler))

    def get(self, path: str, handler: Callable[..., None]) -> None:
        """Register a GET handler for an exact path."""
        self._get_exact[path] = handler

    def get_pattern(self, pattern: str, handler: Callable[..., None]) -> None:
        """Register a GET handler for a regex path pattern."""
        self._get_patterns.append((re.compile(pattern), handler))

    def delete(self, path: str, handler: Callable[..., None]) -> None:
        """Register a DELETE handler for an exact path."""
        self._delete_exact[path] = handler

    def delete_pattern(self, pattern: str, handler: Callable[..., None]) -> None:
        """Register a DELETE handler for a regex path pattern."""
        self._delete_patterns.append((re.compile(pattern), handler))

    def _resolve(self, exact: dict[str, Callable[..., None]],
                 patterns: list[tuple[re.Pattern[str], Callable[..., None]]],
                 path: str) -> RouteResolution:
        """Resolve a path against exact then pattern tables."""
        handler = exact.get(path)
        if handler:
            return RouteResolution(handler, None)
        for regex, pat_handler in patterns:
            match = regex.match(path)
            if match:
                return RouteResolution(pat_handler, match)
        return RouteResolution(None, None)

    def resolve_post(self, path: str) -> RouteResolution:
        """Find handler for POST path. Returns RouteResolution(handler, match)."""
        return self._resolve(self._post_exact, self._post_patterns, path)

    def resolve_get(self, path: str) -> RouteResolution:
        """Find handler for GET path. Returns RouteResolution(handler, match)."""
        return self._resolve(self._get_exact, self._get_patterns, path)

    def resolve_delete(self, path: str) -> RouteResolution:
        """Find handler for DELETE path. Returns RouteResolution(handler, match)."""
        return self._resolve(self._delete_exact, self._delete_patterns, path)


# Singleton endpoint router — populated after Handler class is defined
_endpoint_router = EndpointRouter()




def _checkin_can_restart(name: str, tmux_name: str,
                         host: str | None, pane_cwd: str,
                         requested_cwd: str) -> tuple[bool, str]:
    """Check restart guards for a CWD-triggered checkin restart.

    Returns (allowed, block_reason). If allowed=False, block_reason
    explains why (cooldown, inflight, Claude running).
    """
    # Cooldown: prevent restart loops from repeated checkins
    last_restart = watchdog.recent_restarts.get(name, 0)
    elapsed = _clock.time() - last_restart
    if elapsed < RESTART_COOLDOWN:
        # Narrow exemption: allow one CWD repair after force restart
        if watchdog.force_restart_pending_cwd.pop(name, False):
            _log(_LOG_WARN, "checkin", f"{name}: cooldown bypassed (post-force CWD repair)")
        else:
            _log(_LOG_WARN, "checkin", f"{name}: BLOCKED restart (cooldown {elapsed:.0f}s < {RESTART_COOLDOWN}s)")
            return False, (f"Checkin restart blocked: {name} was restarted {elapsed:.0f}s ago "
                           f"(cooldown {RESTART_COOLDOWN}s). CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")

    # Guard: skip if worker is already running Claude
    if is_claude_running(tmux_name, host=host):
        _log(_LOG_INFO, "checkin", f"{name}: BLOCKED restart (Claude already running in tmux)")
        return False, (f"Checkin restart skipped: {name} has Claude running. "
                       f"CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}")

    # In-flight dedupe: skip if restart already in progress
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
    """Execute a CWD-triggered restart and notify the manager.

    Returns (ok, error_msg). Cleans up inflight tracking on completion.
    """
    notify_chat_id = get_manager_chat_id(name)
    try:
        if notify_chat_id is not None:
            send_telegram_message(
                notify_chat_id,
                f"{name} is restarting in a new directory. "
                "Messages during restart may be lost.",
            )

        if host:
            restart_backend = get_backend(backend_name)
            ok, err = command_router._restart_remote_worker(
                name, backend_name, restart_backend, tmux_name, host, "relaunch")
        else:
            ok, err = worker_manager.restart(name, mode="relaunch")

        watchdog.recent_restarts[name] = _clock.time()
        _log(_LOG_INFO, "checkin", f"{name}: restart result ok={ok}, err={err}")

        if not ok:
            if notify_chat_id is not None:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} could not restart. "
                    f"Run /restart {name} before sending new messages.",
                )
            return False, err or "restart failed"

        if notify_chat_id is not None:
            if _wait_for_restart_ready(tmux_name, backend_name, host=host):
                send_telegram_message(notify_chat_id, f"{name} is ready. Safe to send messages now.")
            else:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} restarted but is not ready yet. "
                    f"Hold messages for now. If this continues, run /restart {name}.",
                )

        return True, ""
    finally:
        with watchdog.restart_lock:
            watchdog.restart_in_progress.pop(name, None)




class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for Telegram webhook and worker API endpoints."""

    # ── Guest Endpoints ────────────────────────────────────────────

    def _guest_auth(self, parsed: dict[str, Any] | None = None) -> GuestSessionDict | None:
        """Authenticate guest from token query param. Returns guest dict or None (sends 403)."""
        query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(self.path).query)
        token = query_params.get("token", [""])[0]
        if not token:
            self._send_json(403, {"ok": False, "error": "token required"})
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with guest_store.lock:
            guest = guest_store.guests.get(token_hash)
        if not guest or guest_is_expired(guest["expires_at_unix"]):
            if guest:
                with guest_store.lock:
                    guest_store.guests.pop(token_hash, None)
                    _guest_save()
            self._send_json(403, {"ok": False, "error": "invalid or expired guest session"})
            return None
        return guest

    def handle_guest_register(self, body: bytes = b"") -> None:
        """POST /guest — register as a temporary guest agent."""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            data = {}

        requested_name = data.get("name", "").strip().lower()
        team_workers = set(get_registered_sessions().keys())

        with guest_store.lock:
            existing_guests = {g["name"] for g in guest_store.guests.values()}

        if requested_name:
            ok, err = guest_validate_name(requested_name, team_workers, existing_guests)
            if not ok:
                status = 409 if "conflicts" in err or "already taken" in err else 400
                self._send_json(status, {"ok": False, "error": err})
                return
            name = requested_name
        else:
            name = guest_generate_name(existing_names=team_workers | existing_guests)

        token, token_hash = guest_create_token()
        now = _clock.time()
        expires_at_unix = now + GUEST_TTL
        expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_unix))

        guest = {
            "name": name,
            "token_hash": token_hash,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "expires_at": expires_at,
            "expires_at_unix": expires_at_unix,
            "notified_workers": set(),
        }

        with guest_store.lock:
            guest_store.guests[token_hash] = guest
            guest_store.inboxes[name] = []
            _guest_save()

        base_url = _relay_base_url()

        inbox_url = f"/guest/inbox?token={token}"
        send_url = f"/guest/send?token={token}"

        listen_script = (
            f'python3 -c "\n'
            f"import json,time,urllib.request as u,sys\n"
            f"TOKEN,URL='{token}','{base_url}'\n"
            f"last=''\n"
            f"print('[listener] connected — waiting for messages',flush=True)\n"
            f"while True:\n"
            f"    try:\n"
            f"        q=URL+'/guest/inbox?token='+TOKEN+('&after='+last if last else '')\n"
            f"        d=json.loads(u.urlopen(u.Request(q),timeout=TIMEOUT_HTTP_API).read())\n"
            f"        for m in d.get('messages',[]):\n"
            f"            print(m.get('from','?')+': '+m['text'],flush=True)\n"
            f"            last=m['id']\n"
            f"    except Exception as e:\n"
            f"        if '403' in str(e) or '404' in str(e):\n"
            f"            print('[listener] session expired — closing',flush=True);break\n"
            f"    time.sleep(3)\n"
            f'"'
        )

        # Telegram notification
        try:
            if admin_chat_id:
                send_telegram_message(admin_chat_id,
                    f"\U0001f514 Guest \"{name}\" connected")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")

        _log(_LOG_INFO, "guest", f"Guest registered: {name} (expires {expires_at})")
        self._send_json(200, {
            "ok": True,
            "name": name,
            "token": token,
            "expires": expires_at,
            "inbox_url": inbox_url,
            "send_url": send_url,
            "channels_url": f"/channels?token={token}",
            "channel_create_url": f"/channels?token={token}",
            "listen_script": listen_script,
        })

    def handle_guest_send(self, body: bytes = b"") -> None:
        """POST /guest/send?token=xxx — guest sends to worker(s), guest(s), or channel(s).

        Body: {"to": "lee" | ["lee","kai","#ops"], "text": "..."}
        Legacy: {"worker": "lee", "text": "..."} still supported.
        Targets: bare name = worker, "guest:name" = guest, "#label" or "ch_xxx" = channel.
        """
        parsed = urlparse(self.path)
        guest = self._guest_auth(parsed)
        if not guest:
            return
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        text = data.get("text", "").strip()
        if not text:
            self._send_json(400, {"ok": False, "error": "text required"})
            return

        # Normalize targets: support "to" (string or list) or legacy "worker"
        raw_to = data.get("to", data.get("worker", ""))
        if isinstance(raw_to, str):
            targets = [raw_to.strip()] if raw_to.strip() else []
        elif isinstance(raw_to, list):
            targets = [t.strip() for t in raw_to if isinstance(t, str) and t.strip()]
        else:
            targets = []
        if not targets:
            self._send_json(400, {"ok": False, "error": "to (or worker) required"})
            return

        guest_name = guest["name"]
        from_member = f"guest:{guest_name}"
        tagged_text = f"[guest:{guest_name}] {text}"
        registered = get_registered_sessions()
        results = []

        for target in targets:
            if target.startswith("#"):
                # Channel by label — find channel ID
                ch_id = None
                with channel_store.lock:
                    for cid, ch in channel_store.channels.items():
                        if ch["label"] == target[1:] and not channel_is_expired(ch):
                            ch_id = cid
                            break
                if not ch_id:
                    results.append({"target": target, "ok": False, "error": "channel not found"})
                    continue
                with channel_store.lock:
                    ch = channel_store.channels.get(ch_id)
                    if from_member not in ch["members"]:
                        results.append({"target": target, "ok": False, "error": "not a member"})
                        continue
                    msg = channel_append_message(ch, from_member, text)
                    members_snapshot = dict(ch["members"])
                _fanout_channel_message(ch_id, from_member, text, msg, members_snapshot, registered)
                results.append({"target": target, "ok": True, "channel": ch_id, "message_id": msg["id"]})

            elif target.startswith("ch_"):
                # Channel by ID
                with channel_store.lock:
                    channel = channel_store.channels.get(target)
                    if not channel or channel_is_expired(channel):
                        results.append({"target": target, "ok": False, "error": "channel not found"})
                        continue
                    if from_member not in channel["members"]:
                        results.append({"target": target, "ok": False, "error": "not a member"})
                        continue
                    msg = channel_append_message(channel, from_member, text)
                    members_snapshot = dict(channel["members"])
                _fanout_channel_message(target, from_member, text, msg, members_snapshot, registered)
                results.append({"target": target, "ok": True, "channel": target, "message_id": msg["id"]})

            elif target.startswith("guest:"):
                # Send to another guest's inbox
                target_guest = target.split(":", 1)[1]
                msg_id = f"gm_{secrets.token_hex(4)}"
                with guest_store.lock:
                    ginbox = guest_store.inboxes.get(target_guest, [])
                    guest_store.inboxes[target_guest] = guest_inbox_append(ginbox, {
                        "id": msg_id, "from": from_member,
                        "text": text, "ts": int(_clock.time()),
                    })
                results.append({"target": target, "ok": True, "message_id": msg_id})

            else:
                # Bare name = worker
                worker = target
                if worker not in registered:
                    results.append({"target": worker, "ok": False, "error": f"worker '{worker}' not found"})
                    continue
                info = registered[worker]
                backend_name = get_worker_backend(worker, info)
                backend = get_backend(backend_name)
                tmux_name = f"{TMUX_PREFIX}{worker}"
                delivered = backend.send(worker, tmux_name, tagged_text,
                                        f"http://localhost:{PORT}", SESSIONS_DIR)
                msg_id = f"gm_{secrets.token_hex(4)}"
                with guest_store.lock:
                    inbox = guest_store.inboxes.get(guest_name, [])
                    guest_store.inboxes[guest_name] = guest_inbox_append(inbox, {
                        "id": msg_id, "from": guest_name, "to": worker,
                        "text": text, "ts": int(_clock.time()),
                    })
                    if worker not in guest.get("notified_workers", set()):
                        guest.setdefault("notified_workers", set()).add(worker)
                        try:
                            if admin_chat_id:
                                send_telegram_message(admin_chat_id,
                                    f"\U0001f514 Guest \"{guest_name}\" → {worker}")
                        except (urllib.error.URLError, OSError, TimeoutError) as exc:
                            _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")
                results.append({"target": worker, "ok": True, "delivered": delivered, "message_id": msg_id})

        # Single target: flat response for backwards compat
        if len(results) == 1:
            self._send_json(200, {**results[0], "ok": results[0].get("ok", True)})
        else:
            self._send_json(200, {"ok": all(r.get("ok") for r in results), "results": results})

    def handle_guest_reply(self, body: bytes = b"") -> None:
        """POST /guest/reply — worker sends reply to a guest's inbox."""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        guest_name = data.get("guest", "").strip()
        from_worker = data.get("from", "").strip()
        text = data.get("text", "").strip()
        if not guest_name or not text:
            self._send_json(400, {"ok": False, "error": "guest and text required"})
            return

        msg_id = f"gm_{secrets.token_hex(4)}"
        with guest_store.lock:
            if guest_name not in guest_store.inboxes:
                guest_store.inboxes[guest_name] = []
            guest_store.inboxes[guest_name] = guest_inbox_append(
                guest_store.inboxes[guest_name],
                {"id": msg_id, "from": from_worker or "worker",
                 "text": text, "ts": int(_clock.time())},
            )

        self._send_json(200, {"ok": True, "message_id": msg_id})

    def handle_guest_inbox(self, parsed: dict[str, Any]) -> None:
        """GET /guest/inbox?token=xxx[&after=gm_xxx] — poll for messages."""
        guest = self._guest_auth(parsed)
        if not guest:
            return
        query_params = parse_qs(parsed.query)
        after = query_params.get("after", [None])[0]
        guest_name = guest["name"]
        with guest_store.lock:
            msgs = list(guest_store.inboxes.get(guest_name, []))
        filtered = guest_inbox_filter(msgs, after=after)
        self._send_json(200, {
            "ok": True, "name": guest_name, "messages": filtered,
        })

    def handle_guest_status(self, parsed: dict[str, Any]) -> None:
        """GET /guest/status?token=xxx — check session validity."""
        guest = self._guest_auth(parsed)
        if not guest:
            return
        with guest_store.lock:
            workers = list(guest.get("notified_workers", set()))
        self._send_json(200, {
            "ok": True, "name": guest["name"],
            "expires": guest["expires_at"],
            "connected_workers": workers,
        })

    def handle_guests_list(self) -> None:
        """GET /guests — list active guests (admin only)."""
        with guest_store.lock:
            guests_list = []
            expired = []
            for token_hash, g in guest_store.guests.items():
                if guest_is_expired(g["expires_at_unix"]):
                    expired.append(token_hash)
                else:
                    guests_list.append({
                        "name": g["name"],
                        "expires": g["expires_at"],
                        "connected_workers": list(g.get("notified_workers", set())),
                    })
            for th in expired:
                name = guest_store.guests[th]["name"]
                guest_store.guests.pop(th, None)
                guest_store.inboxes.pop(name, None)
            if expired:
                _guest_save()
        self._send_json(200, {"ok": True, "guests": guests_list})

    def handle_guest_disconnect(self, parsed: dict[str, Any]) -> None:
        """DELETE /guest?token=xxx — disconnect guest session."""
        query_params = parse_qs(parsed.query)
        token = query_params.get("token", [""])[0]
        if not token:
            self._send_json(403, {"ok": False, "error": "token required"})
            return
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with guest_store.lock:
            guest = guest_store.guests.pop(token_hash, None)
            if guest:
                guest_store.inboxes.pop(guest["name"], None)
                _guest_save()
        if not guest:
            self._send_json(403, {"ok": False, "error": "invalid token"})
            return

        try:
            if admin_chat_id:
                send_telegram_message(admin_chat_id,
                    f"\U0001f514 Guest \"{guest['name']}\" disconnected")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:handle_guest_disconnect", f"{type(exc).__name__}: {exc}")

        _log(_LOG_INFO, "guest", f"Guest disconnected: {guest['name']}")
        self._send_json(200, {"ok": True, "name": guest["name"]})



    # ── Channel Endpoints ──────────────────────────────────────────

    def _channel_auth_guest(self, parsed: dict[str, Any]) -> GuestSessionDict | None:
        """Authenticate a guest from query token for channel access. Returns guest or sends error."""
        query_params = parse_qs(parsed.query)
        token = query_params.get("token", [None])[0]
        if not token:
            self._send_json(403, {"ok": False, "error": "token required"})
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with guest_store.lock:
            guest = guest_store.guests.get(token_hash)
        if not guest or guest_is_expired(guest["expires_at_unix"]):
            self._send_json(403, {"ok": False, "error": "invalid or expired token"})
            return None
        return guest

    def handle_channel_create(self, body: bytes = b"") -> None:
        """POST /channels — create a group channel."""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        label = data.get("label", "").strip()
        members = data.get("members", [])
        include_manager = data.get("include_manager", True)
        ttl = min(data.get("ttl_seconds", CHANNEL_TTL), CHANNEL_TTL)

        if not isinstance(members, list):
            self._send_json(400, {"ok": False, "error": "members must be a list"})
            return

        # Validate member format
        valid_members = []
        for m in members:
            if isinstance(m, str) and (m == "manager" or ":" in m):
                valid_members.append(m)
        if include_manager and "manager" not in valid_members:
            valid_members.append("manager")

        # Check from query token (guest-created) or admin
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)
        token = query_params.get("token", [None])[0]
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with guest_store.lock:
                guest = guest_store.guests.get(token_hash)
            if not guest or guest_is_expired(guest["expires_at_unix"]):
                self._send_json(403, {"ok": False, "error": "invalid or expired token"})
                return
            created_by = f"guest:{guest['name']}"
            if created_by not in valid_members:
                valid_members.append(created_by)
        else:
            created_by = "manager"

        channel_id = channel_create_id(label)
        channel = channel_new(channel_id, label, created_by, valid_members, ttl)

        with channel_store.lock:
            channel_store.channels[channel_id] = channel
            _channel_save()

        # Telegram notification
        member_str = ", ".join(valid_members)
        try:
            if admin_chat_id:
                send_telegram_message(admin_chat_id,
                    f"\U0001f4e2 Channel {channel_id} created by {created_by}\nMembers: {member_str}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")

        _log(_LOG_INFO, "channel", f"Channel created: {channel_id} by {created_by} members=[{member_str}]")
        self._send_json(200, {
            "ok": True,
            "channel": channel_id,
            "label": label,
            "members": valid_members,
            "send_url": f"/channels/{channel_id}/send",
            "messages_url": f"/channels/{channel_id}/messages",
        })

    def handle_channel_members(self, channel_id: str, body: bytes = b"") -> None:
        """POST /channels/{id}/members — add/remove members."""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        with channel_store.lock:
            channel = channel_store.channels.get(channel_id)
            if not channel or channel_is_expired(channel):
                self._send_json(404, {"ok": False, "error": "channel not found"})
                return

            added = channel_add_members(channel, data.get("add", []))
            removed = channel_remove_members(channel, data.get("remove", []))
            current = list(channel["members"].keys())

        if added or removed:
            try:
                if admin_chat_id:
                    parts = []
                    if added:
                        parts.append(f"added {', '.join(added)}")
                    if removed:
                        parts.append(f"removed {', '.join(removed)}")
                    send_telegram_message(admin_chat_id,
                        f"\U0001f4e2 Channel {channel_id}: {'; '.join(parts)}")
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                _log(_LOG_DEBUG, "notify:handle_channel_members", f"{type(exc).__name__}: {exc}")

        self._send_json(200, {
            "ok": True,
            "channel": channel_id,
            "added": added,
            "removed": removed,
            "members": current,
        })

    def handle_channel_send(self, channel_id: str, body: bytes = b"") -> None:
        """POST /channels/{id}/send — send message to channel (fan-out)."""
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return

        text = data.get("text", "").strip()
        if not text:
            self._send_json(400, {"ok": False, "error": "text required"})
            return

        # Determine sender: from token (guest), from field (worker), or manager
        parsed = urlparse(self.path)
        query_params = parse_qs(parsed.query)
        token = query_params.get("token", [None])[0]
        from_member = data.get("from", "")

        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with guest_store.lock:
                guest = guest_store.guests.get(token_hash)
            if not guest or guest_is_expired(guest["expires_at_unix"]):
                self._send_json(403, {"ok": False, "error": "invalid or expired token"})
                return
            from_member = f"guest:{guest['name']}"
        elif not from_member:
            from_member = "manager"

        with channel_store.lock:
            channel = channel_store.channels.get(channel_id)
            if not channel or channel_is_expired(channel):
                self._send_json(404, {"ok": False, "error": "channel not found"})
                return
            if from_member not in channel["members"] and from_member != "manager":
                self._send_json(403, {"ok": False, "error": f"{from_member} not a member"})
                return
            msg = channel_append_message(channel, from_member, text)
            members_snapshot = dict(channel["members"])

        # Fan-out to all members except sender
        tagged = f"[{channel_id} from {from_member}] {text}"
        for member_key, info in members_snapshot.items():
            if member_key == from_member:
                continue
            if info["type"] == "worker":
                worker_name = info["name"]
                registered = get_registered_sessions()
                if worker_name in registered:
                    worker_info = registered[worker_name]
                    backend_name = get_worker_backend(worker_name, worker_info)
                    backend = get_backend(backend_name)
                    tmux_name = f"{TMUX_PREFIX}{worker_name}"
                    try:
                        backend.send(worker_name, tmux_name, tagged,
                                     f"http://localhost:{PORT}", SESSIONS_DIR)
                    except (subprocess.SubprocessError, ConnectionError, TimeoutError) as e:
                        _log(_LOG_WARN, "worker", f"Channel fan-out to {worker_name} failed: {e}")
            elif info["type"] == "guest":
                guest_name = info["name"]
                with guest_store.lock:
                    inbox = guest_store.inboxes.get(guest_name, [])
                    guest_store.inboxes[guest_name] = guest_inbox_append(inbox, {
                        "id": msg["id"], "from": from_member,
                        "channel": channel_id, "text": text,
                        "ts": msg["ts"],
                    })
            elif info["type"] == "manager":
                try:
                    if admin_chat_id:
                        send_telegram_message(admin_chat_id,
                            f"[{channel_id}] {from_member}: {text}")
                except (urllib.error.URLError, OSError, TimeoutError) as exc:
                    _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")

        self._send_json(200, {
            "ok": True,
            "channel": channel_id,
            "message_id": msg["id"],
            "seq": msg["seq"],
        })

    def handle_channel_messages(self, channel_id: str, parsed: dict[str, Any]) -> None:
        """GET /channels/{id}/messages — poll channel messages. Guests must provide ?token=."""
        query_params = parse_qs(parsed.query)
        after = query_params.get("after", [None])[0]
        token = query_params.get("token", [None])[0]

        # If token provided, verify guest is a member
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with guest_store.lock:
                guest = guest_store.guests.get(token_hash)
            if not guest or guest_is_expired(guest["expires_at_unix"]):
                self._send_json(403, {"ok": False, "error": "invalid or expired token"})
                return
            from_member = f"guest:{guest['name']}"
            with channel_store.lock:
                channel = channel_store.channels.get(channel_id)
                if not channel or channel_is_expired(channel):
                    self._send_json(404, {"ok": False, "error": "channel not found"})
                    return
                if from_member not in channel["members"]:
                    self._send_json(403, {"ok": False, "error": "not a member of this channel"})
                    return
                msgs, truncated = channel_get_messages(channel, after)
        else:
            with channel_store.lock:
                channel = channel_store.channels.get(channel_id)
                if not channel or channel_is_expired(channel):
                    self._send_json(404, {"ok": False, "error": "channel not found"})
                    return
                msgs, truncated = channel_get_messages(channel, after)

        resp = {
            "ok": True,
            "channel": channel_id,
            "messages": msgs,
        }
        if truncated:
            resp["truncated"] = True
        self._send_json(200, resp)

    def handle_channels_list(self, parsed: dict[str, Any]=None) -> None:
        """GET /channels — list active channels. With ?token=, filter to guest's channels."""
        query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(self.path).query)
        token = query_params.get("token", [None])[0]
        filter_member = None
        if token:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with guest_store.lock:
                guest = guest_store.guests.get(token_hash)
            if not guest or guest_is_expired(guest["expires_at_unix"]):
                self._send_json(403, {"ok": False, "error": "invalid or expired token"})
                return
            filter_member = f"guest:{guest['name']}"

        with channel_store.lock:
            active = []
            expired_ids = []
            for cid, ch in channel_store.channels.items():
                if channel_is_expired(ch):
                    expired_ids.append(cid)
                else:
                    if filter_member and filter_member not in ch["members"]:
                        continue
                    active.append({
                        "id": ch["id"],
                        "label": ch["label"],
                        "members": list(ch["members"].keys()),
                        "message_count": len(ch["messages"]),
                        "created_by": ch["created_by"],
                        "send_url": f"/channels/{ch['id']}/send",
                        "messages_url": f"/channels/{ch['id']}/messages",
                    })
            for cid in expired_ids:
                del channel_store.channels[cid]
            if expired_ids:
                _channel_save()
        self._send_json(200, {"ok": True, "channels": active})

    def handle_channel_delete(self, channel_id: str) -> None:
        """DELETE /channels/{id} — delete a channel."""
        with channel_store.lock:
            channel = channel_store.channels.pop(channel_id, None)
            if channel:
                _channel_save()
        if not channel:
            self._send_json(404, {"ok": False, "error": "channel not found"})
            return
        try:
            if admin_chat_id:
                send_telegram_message(admin_chat_id,
                    f"\U0001f4e2 Channel {channel_id} closed")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:handle_channel_delete", f"{type(exc).__name__}: {exc}")
        _log(_LOG_INFO, "channel", f"Channel deleted: {channel_id}")
        self._send_json(200, {"ok": True, "channel": channel_id})



    # ── Relay Endpoints ────────────────────────────────────────────

    def _relay_get_token(self) -> str | None:
        """Extract Bearer token from Authorization header or query param."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        parsed = urlparse(self.path)
        params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
        return params.get("token", "")

    def handle_relay_get(self, channel_id: str, action: str | None, parsed: dict[str, Any]) -> None:
        """Handle GET /v1/<channel_id>[/action]."""
        token = self._relay_get_token()
        if not token:
            self._send_json(401, {"error": "missing token"})
            return

        channel = relay_auth_guest(channel_id, token)
        if not channel:
            self._send_json(403, {"error": "invalid or expired channel/token"})
            return

        if action is None:
            guide = relay_guide_text(channel, token)
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.end_headers()
            self.wfile.write(guide.encode())
            return

        if action == "messages":
            params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
            after = params.get("after")
            msgs = relay_get_messages(channel_id, after=after)
            self._send_json(200, {"messages": msgs})
            return

        if action == "status":
            self._send_json(200, {
                "channel_id": channel_id,
                "worker": channel["worker"],
                "label": channel["label"],
                "expires_at": channel["expires_at"],
                "message_count": len(channel["messages"]),
            })
            return

        self._send_json(404, {"error": f"unknown action: {action}"})

    def handle_relay_send(self, channel_id: str, body: bytes = b"") -> None:
        """Handle POST /v1/<channel_id>/send — guest sends message to worker."""
        token = self._relay_get_token()
        if not token:
            self._send_json(401, {"error": "missing token"})
            return

        channel = relay_auth_guest(channel_id, token)
        if not channel:
            self._send_json(403, {"error": "invalid or expired channel/token"})
            return

        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid JSON"})
            return

        text = data.get("text", "").strip()
        if not text:
            self._send_json(400, {"error": "missing text"})
            return

        envelope, msg = relay_guest_send(channel_id, text)
        if not envelope:
            self._send_json(500, {"error": "channel not found"})
            return

        workers = channel.get("workers", [channel["worker"]])
        delivered = {}
        for w in workers:
            delivered[w] = send_to_worker(w, envelope)
        self._send_json(200, {
            "message_id": msg["message_id"],
            "delivered": all(delivered.values()),
            "workers": delivered,
        })

    def handle_relay_reply(self, channel_id: str, body: bytes = b"") -> None:
        """Handle POST /v1/<channel_id>/reply — worker replies to guest."""
        token = self._relay_get_token()
        if not token:
            self._send_json(401, {"error": "missing token"})
            return

        channel = relay_auth_reply(channel_id, token)
        if not channel:
            self._send_json(403, {"error": "invalid or expired channel/token"})
            return

        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid JSON"})
            return

        text = data.get("text", "").strip()
        if not text:
            self._send_json(400, {"error": "missing text"})
            return

        msg = relay_worker_reply(channel_id, text)
        if not msg:
            self._send_json(500, {"error": "channel not found"})
            return

        self._send_json(200, {
            "message_id": msg["message_id"],
            "delivered": True,
        })



    # ── PR Endpoints ──────────────────────────────────────────────

    def handle_pr_file_content(self, parsed: dict[str, Any]) -> None:
        """Fetch file content from GitHub for diff context expansion."""
        import base64 as _b64
        params = dict(parse_qs(parsed.query))
        token = params.get("token", [None])[0]

        now = _clock.time()
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
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}",
                 "--jq", ".content"],
                capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
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
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            self.send_response(500)
            self.end_headers()

    def handle_pr_keepalive(self, parsed: dict[str, Any]) -> None:
        """Extend PR review token expiry on client activity."""
        params = dict(parse_qs(parsed.query))
        token = params.get("token", [None])[0]
        now = _clock.time()
        if not token or token not in PR_REVIEW_TOKENS or PR_REVIEW_TOKENS[token]["expires_at"] <= now:
            self.send_response(403)
            self.end_headers()
            return
        PR_REVIEW_TOKENS[token]["expires_at"] = now + 300
        self.send_response(204)
        self.end_headers()

    def handle_pr_general_comment(self, body: bytes) -> None:
        """Post a general (non-inline) comment on a PR via GitHub API."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        token = data.get("token", "")
        now = _clock.time()
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
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/issues/{pr_num}/comments",
                 "--method", "POST", "-f", f"body={comment_body}"],
                capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
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
            _urlopen(req, timeout=TIMEOUT_TMUX_SEND)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_WARN, "pr-comment", f"Telegram notification failed (best-effort): {exc}")

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

    def handle_pr_merge(self, body: bytes) -> None:
        """Merge a PR via GitHub API."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        token = data.get("token", "")
        now = _clock.time()
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
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/merge",
                 "--method", "PUT", "-f", f"merge_method={merge_method}"],
                capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
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
            _urlopen(req, timeout=TIMEOUT_TMUX_SEND)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def handle_pr_review_endpoint(self, parsed: dict[str, Any]) -> None:
        """Serve generated PR review HTML.

        Requires a valid token (?token=...) generated by /pr command.
        Token expires after 5 minutes (same as rewind).
        """
        params = dict(parse_qs(parsed.query))
        token = params.get("token", [None])[0]

        # Cleanup expired tokens
        now = _clock.time()
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

    def handle_pr_comment(self, body: bytes) -> None:
        """Post an inline comment on a PR via GitHub API + notify Telegram."""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        token = data.get("token", "")
        now = _clock.time()
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
            r = _subprocess_runner.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_num}/comments",
                 "--method", "POST", "--input", "-"],
                input=gh_payload, capture_output=True, text=True, timeout=TIMEOUT_FILE_TRANSFER)
            if r.returncode != 0:
                err = r.stderr.strip() or r.stdout.strip()
                _log(_LOG_ERROR, "pr-comment", f"GitHub API error: {err}")
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



    # ── Transcript Endpoints ─────────────────────────────────────

    def handle_transcript_endpoint(self, parsed: dict[str, Any]) -> None:
        """Serve polished HTML transcript for a worker.

        Requires a valid rewind token (?token=...) generated by /rewind command.
        GET /transcript/<name>?token=...      — required auth
        GET /transcript/<name>?token=...&sid=...        — specific session ID
        GET /transcript/<name>?token=...&page=2         — pagination
        GET /transcript/<name>?token=...&per_page=100   — entries per page (default 50)
        GET /transcript/<name>?token=...&q=search+term  — full-text search
        """
        try:
            query_params = parse_qs(parsed.query)
            # Token auth — clean up expired tokens first
            now = _clock.time()
            expired = [k for k, v in REWIND_TOKENS.items() if v["expires_at"] <= now]
            for k in expired:
                del REWIND_TOKENS[k]
            token = query_params.get("token", [None])[0]
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

            # /transcript/<name>/updates — lightweight poll for new entry count
            if len(parts) >= 4 and parts[3] == "updates":
                query_params = parse_qs(parsed.query)
                since = int(query_params.get("since", [0])[0])
                session_id = query_params.get("sid", [None])[0]
                host = get_worker_host(name)
                if host:
                    cwd = get_claude_session_cwd(name) or ""
                    sid = session_id or get_claude_session_id(name)
                    if not sid:
                        self._send_json(200, {"total": 0, "new": 0})
                        return
                    remote_home = _get_remote_home(host) or ""
                    remote_cwd = cwd
                    local_home = os.path.expanduser("~")
                    if remote_cwd.startswith(local_home) and remote_home != local_home:
                        remote_cwd = remote_home + remote_cwd[len(local_home):]
                    remote_slug = _project_slug(remote_cwd)
                    jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                else:
                    _tp, sid, _cwd = _resolve_transcript_path(name, session_id)
                    if not _tp or not sid:
                        self._send_json(200, {"total": 0, "new": 0})
                        return
                    jsonl_path = str(_tp)
                result = _run_transcript_query(jsonl_path, sid, "stats", host=host)
                total = 0
                if result:
                    total = result.get("n_user", 0) + result.get("n_tool", 0)
                    # Use a more accurate total from entries query
                    count_result = _run_transcript_query(
                        jsonl_path, sid, "entries", host=host, page=1, per_page=1)
                    if count_result:
                        total = count_result.get("total", total)
                new_count = max(0, total - since)
                self._send_json(200, {"total": total, "new": new_count})
                return

            query_params = parse_qs(parsed.query)
            session_id = query_params.get("sid", [None])[0]
            page_raw = query_params.get("page", [None])[0]
            try:
                page = max(1, int(page_raw)) if page_raw is not None else None
            except (ValueError, TypeError):
                page = None
            try:
                per_page = max(1, min(500, int(query_params.get("per_page", [50])[0])))
            except (ValueError, TypeError):
                per_page = 50
            search_query = query_params.get("q", [""])[0].strip()
            search_sort = query_params.get("sort", ["relevance"])[0].strip()
            if search_sort not in ("relevance", "time"):
                search_sort = "relevance"
            filter_mode = query_params.get("filter", [""])[0].strip()
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
        except (OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "transcript", f"Transcript endpoint error: {e}", exc=e)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_team_chat_endpoint(self, parsed: dict[str, Any]) -> None:
        """Serve team Telegram chat viewer.

        GET /team-chat?token=...
        GET /team-chat?token=...&page=2
        GET /team-chat?token=...&q=search+term
        """
        try:
            query_params = parse_qs(parsed.query)
            # Token auth — same as transcript endpoint
            now = _clock.time()
            expired = [k for k, v in REWIND_TOKENS.items() if v["expires_at"] <= now]
            for k in expired:
                del REWIND_TOKENS[k]
            token = query_params.get("token", [None])[0]
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

            page_raw = query_params.get("page", [None])[0]
            try:
                page = max(1, int(page_raw)) if page_raw is not None else None
            except (ValueError, TypeError):
                page = None
            try:
                per_page = max(1, min(500, int(query_params.get("per_page", [50])[0])))
            except (ValueError, TypeError):
                per_page = 50
            search_query = query_params.get("q", [""])[0].strip()

            html_content = _render_team_chat_html(
                page=page, per_page=per_page,
                search_query=search_query, token=token)
            self._send_html(html_content.encode("utf-8"))
        except (OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Team chat endpoint error: {e}", exc=e)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_team_chat_media(self, parsed: dict[str, Any]) -> None:
        """Serve media files (photos/files) from team chat export.

        GET /team-chat-media/photos/photo_1@28-01-2026_18-09-13.jpg?token=...
        GET /team-chat-media/files/somefile.pdf?token=...

        Requires valid rewind token (same as /team-chat).
        Only serves files under TEAM_CHAT_MEDIA_DIR (no path traversal).
        """
        query_params = parse_qs(parsed.query)

        # Token auth
        now = _clock.time()
        token = query_params.get("token", [None])[0]
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
        except OSError:
            self.send_response(500)
            self.end_headers()




    # ── Core HTTP Handler ─────────────────────────────────────────

    def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
        """Send a JSON response with proper Content-Type."""
        body = json.dumps(data).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status_code: int, message: str) -> None:
        """Send a JSON error response with status code and message."""
        self._send_json(status_code, {"error": message})

    def _send_html(self, body: bytes, status: int = 200) -> None:
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

    def _send_text(self, status_code: int, text: str) -> None:
        """Send a plain text response with proper Content-Type."""
        body = text.encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def _send_unknown_endpoint(self, method: str, path: str) -> None:
        """Return 404 JSON for unrecognized endpoints with available alternatives."""
        self._send_json(404, {
            "error": f"Unknown endpoint: {method} {path}",
            "available_endpoints": API_ENDPOINTS,
            "hint": "Messages from manager arrive as prompts. There is no polling endpoint.",
        })

    def do_POST(self) -> None:
        # Exact-match and pattern-match POST handlers are registered in
        # _setup_endpoint_routes() below. Adding a new endpoint means
        # one registration call, not another if/elif here.
        """Handle all incoming HTTP POST requests."""
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve_post(parsed.path)
        if handler:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if match:
                handler(self, body, match)
            else:
                handler(self, body)
            return

        # Only accept Telegram webhook on root path — 404 for unknown POST paths
        if parsed.path != "/":
            self._send_unknown_endpoint("POST", parsed.path)
            return

        # Telegram webhook - optional secret verification
        if WEBHOOK_SECRET:
            header_token = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if header_token != WEBHOOK_SECRET:
                _log(_LOG_WARN, "webhook", f"Webhook rejected: invalid secret token")
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
            update_types = [k for k in update.keys() if k != "update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "") or msg.get("caption", "")
            _log(_LOG_INFO, "webhook", f"update_id={update.get('update_id')}, types={update_types}, text={repr(text[:50]) if text else '(none)'}")
            if "message" in update:
                def _safe_handle(upd: dict[str, Any]) -> None:
                    """Handle a Telegram update in a thread, logging errors instead of crashing."""
                    try:
                        command_router.handle_message(upd)
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                        _log(_LOG_ERROR, "webhook", f"handle_message CRASH: {exc}", exc=exc)
                threading.Thread(
                    target=_safe_handle,
                    args=(update,),
                    daemon=True,
                ).start()
        except (json.JSONDecodeError, KeyError) as e:
            _log(_LOG_ERROR, "webhook", f"parse error: {e}", exc=e)

    def handle_notify(self, body: bytes = b"") -> None:
        """Handle system notification request (internal, HMAC-authenticated).

        SECURITY: This endpoint allows the shell script to trigger
        notifications without having access to the bot token.
        Used for tunnel watchdog alerts and worker notifications.

        Supports [[image:/path|caption]] and [[file:/path|caption]] tags.
        Pass optional "name" field to enable remote file fetching for
        teleported workers.
        """
        try:
            data = json.loads(body)
            text = data.get("text", "")
            name = data.get("name", "")

            if not text:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing text")
                return

            # Parse media tags (support [[image:]] and [[file:]] in notifications)
            host = get_worker_host(name) if name else None
            if host:
                _accept_all = lambda p: (True, Path(p))
                clean_text, images = _parse_media_tags(text, "image", _accept_all)
                clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
            else:
                clean_text, images = parse_image_tags(text)
                clean_text, files = parse_file_tags(clean_text)

            if name and (images or files):
                images = _localize_media(name, images)
                files = _localize_media(name, files)

            # Send to all known chat_ids
            chat_ids = get_all_chat_ids()
            sent = 0
            label = name or "notify"
            for chat_id in chat_ids:
                if clean_text:
                    result = transport.send_text(chat_id, clean_text)
                    if result and result.get("ok"):
                        sent += 1
                for img_path, caption in images:
                    if img_path is None:
                        transport.send_text(chat_id, f"{label}: {caption}")
                        continue
                    full_caption = f"{label}: {caption}" if caption else f"{label}:"
                    if Path(img_path).suffix.lower() in (".gif", ".mp4"):
                        send_animation(chat_id, img_path, full_caption)
                    else:
                        send_photo(chat_id, img_path, full_caption)
                for fpath, caption in files:
                    if fpath is None:
                        transport.send_text(chat_id, f"{label}: {caption}")
                        continue
                    full_caption = f"{label}: {caption}" if caption else f"{label}:"
                    ext = Path(fpath).suffix.lower()
                    if ext in VIDEO_EXTENSIONS:
                        send_video(chat_id, fpath, full_caption)
                    elif ext in AUDIO_EXTENSIONS:
                        send_audio(chat_id, fpath, full_caption)
                    elif ext in VOICE_EXTENSIONS:
                        send_voice(chat_id, fpath, full_caption)
                    else:
                        send_document(chat_id, fpath, full_caption)

            has_media = len(images) + len(files)
            _log(_LOG_INFO, "notify", f"sent to {sent}/{len(chat_ids)} chats: {text[:50]}..."
                 f"{f' ({has_media} media)' if has_media else ''}")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"Sent to {sent} chats".encode())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"Notify error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_health_alert(self, body: bytes = b"") -> None:
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
            _log(_LOG_WARN, "health", f"Health alert: {worker} — {issue} (age={age}s)")

            chat_ids = get_all_chat_ids()
            for chat_id in chat_ids:
                transport.send_text(chat_id, alert_text)

            self._send_json(200, {"ok": True})
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            _log(_LOG_ERROR, "bridge", f"Health alert error: {e}")
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_forge_register(self, body: bytes = b"") -> None:
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
                    _log(_LOG_INFO, "worker", f"Callback worker registered: {name} (host={host}, url={callback_url}, version={version})")
                else:
                    _registry_add(name, DEFAULT_BACKEND, host=host)
                    _log(_LOG_INFO, "worker", f"Forge worker registered: {name} (host={host}, version={version})")
                backend_name = get_worker_backend(name, {"host": host})
                tmux_name = f"{TMUX_PREFIX}{name}"
                reg_host = host or None
                if tmux_exists(tmux_name, host=reg_host):
                    export_hook_env(tmux_name, backend_name, host=reg_host)
                ensure_session_dir(name)
                if admin_chat_id is not None:
                    cid_file = get_chat_id_file(name)
                    if not cid_file.exists():
                        cid_file.write_text(str(admin_chat_id))
                        cid_file.chmod(0o600)
            tmux_session = f"{TMUX_PREFIX}{name}" if name else ""
            conflict = False
            active_workers = []
            try:
                r = _subprocess_runner.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                                           capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r.returncode == 0:
                    active_workers = [s.removeprefix(TMUX_PREFIX)
                                      for s in r.stdout.strip().split("\n")
                                      if s.startswith(TMUX_PREFIX)]
                    conflict = name in active_workers if name else False
            except (subprocess.SubprocessError, OSError) as exc:
                _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
            worker_manager.invalidate_sessions_cache()
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
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Register error: {e}")
            self._send_json(500, {"ok": False, "error": str(e)})

    def handle_send_endpoint(self, body: bytes = b"") -> None:
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

    def handle_hook_response(self, body: bytes = b"") -> None:
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

            # Get chat_id from session's file, fall back to admin_chat_id
            chat_id_file = get_chat_id_file(session_name)
            if chat_id_file.exists():
                chat_id = chat_id_file.read_text().strip()
            elif admin_chat_id is not None:
                chat_id = str(admin_chat_id)
                ensure_session_dir(session_name)
                chat_id_file.write_text(chat_id)
                chat_id_file.chmod(0o600)
                _log(_LOG_INFO, "hook", f"Hook response: auto-created chat_id for session '{session_name}' from admin_chat_id")
            else:
                _log(_LOG_WARN, "hook", f"Hook response: no chat_id for session '{session_name}'")
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"No chat_id for session")
                return

            # Debug: log short messages to trace source of empty "name:" messages
            if len(text.strip()) <= 5:
                source_ip = self.client_address[0] if self.client_address else "unknown"
                hook_sid = data.get("session_id", "")
                escape_flag = data.get("escape", False)
                _log(_LOG_DEBUG, "hook", f"Hook response DEBUG: {session_name} -> chat {chat_id}, "
                     f"text={repr(text)}, len={len(text)}, "
                     f"source={data.get('source', 'hook')}, "
                     f"session_id={hook_sid[:12] if hook_sid else 'none'}, "
                     f"escape={escape_flag}, ip={source_ip}")

            _log(_LOG_INFO, "hook", f"Hook response: {session_name} -> chat {chat_id} ({len(text)} chars)")

            # Update session ID cache if provided (keeps VPS in sync with remote workers).
            # _cache_session_id stores the CWD alongside the session_id, so
            # get_claude_session_id will self-invalidate if the CWD changes later.
            hook_sid = data.get("session_id", "")
            if hook_sid:
                _cache_session_id(session_name, hook_sid)

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
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Hook response error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    # ── Guest System Handlers ──────────────────────────────────────────────









    # ─────────────────────────────────────────────────────────────
    # GROUP CHANNEL endpoints
    # ─────────────────────────────────────────────────────────────








    # ── Relay v1 endpoint handlers ──────────────────────────────────────





    def do_GET(self) -> None:
        """Handle all incoming HTTP GET requests via EndpointRouter dispatch."""
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve_get(parsed.path)
        if handler:
            if match:
                handler(self, parsed, match)
            else:
                handler(self, parsed)
            return

        # API index (also serves as health check — returns 200)
        if parsed.path == "/":
            self._send_json(200, {
                "name": "claudecode-telegram bridge",
                "endpoints": API_ENDPOINTS,
                "note": "Messages from manager arrive as prompts. There is no polling endpoint.",
            })
            return

        self._send_unknown_endpoint("GET", parsed.path)

    def do_DELETE(self) -> None:
        """Handle all incoming HTTP DELETE requests via EndpointRouter dispatch."""
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve_delete(parsed.path)
        if handler:
            if match:
                handler(self, parsed, match)
            else:
                handler(self, parsed)
            return
        self._send_unknown_endpoint("DELETE", parsed.path)

    def handle_workers_endpoint(self, parsed: dict[str, Any]=None) -> None:
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
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "worker", f"Workers endpoint error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def handle_machines_endpoint(self, parsed: dict[str, Any]=None) -> None:
        """Return configured machines with derived workers, access, and health.

        GET /machines             — bridge-POV access hints
        GET /machines?from=<name> — caller-POV access hints
        """
        try:
            caller_from = None
            if parsed is not None:
                caller_from = parse_qs(parsed.query).get("from", [None])[0]
            self._send_json(200, get_machines(caller_from=caller_from))
        except MachineConfigError as e:
            _log(_LOG_ERROR, "bridge", f"Machines endpoint config error: {e}")
            self._send_json(500, {"error": str(e)})
        except KeyError as e:
            _log(_LOG_ERROR, "bridge", f"Machines endpoint error: {e}")
            self._send_json(500, {"error": str(e)})

    def handle_checkin_endpoint(self, parsed: dict[str, Any]) -> None:
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
                worker_host = get_worker_host(name)
                requested_cwd, cwd_err = validate_cwd(raw_cwd, host=worker_host)
                if cwd_err:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(f"Invalid cwd: {cwd_err}".encode())
                    return

            # Resolve backend: use worker's actual backend if registered, else default
            _sync_worker_manager()
            registered = worker_manager.get_registered_sessions()
            tmux_name = ""
            host: str | None = None
            if name in registered:
                backend_name = get_worker_backend(name, registered[name])
                tmux_name = registered[name].get("tmux", f"{TMUX_PREFIX}{name}")
                host = get_worker_host(name)
                if tmux_exists(tmux_name, host=host):
                    export_hook_env(tmux_name, backend_name, host=host)
            else:
                backend_name = DEFAULT_BACKEND
            backend_obj = get_backend(backend_name)

            # Handle CWD change: update saved cwd, restart if pane cwd differs
            if requested_cwd:
                _set_worker_cwd(name, requested_cwd)
                old_cwd = get_claude_session_cwd(name)
                save_claude_session_cwd(name, requested_cwd)
                _ensure_workspace_trusted(requested_cwd)
                if old_cwd and old_cwd.rstrip("/") != requested_cwd.rstrip("/"):
                    old_sid = get_claude_session_id(name)
                    _log_session_event(name, old_sid or "(none)", requested_cwd, "cwd_change")
                    # No need to clear session_id — get_claude_session_id()
                    # self-validates by comparing stored CWD against current CWD.
                    # Stale session IDs (from old directory) are ignored at read time.
                    _log(_LOG_WARN, "checkin", f"{name}: CWD changed ({old_cwd} -> {requested_cwd}), stale sessions will self-invalidate")
                _log(_LOG_INFO, "checkin", f"{name}: requested_cwd={requested_cwd}, tmux={tmux_name}, host={host}")

                if tmux_name and tmux_exists(tmux_name, host=host):
                    pane_cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
                    same_cwd = pane_cwd and pane_cwd.rstrip("/") == requested_cwd.rstrip("/")
                    _log(_LOG_INFO, "checkin", f"{name}: pane_cwd={pane_cwd}, same_cwd={same_cwd}")

                    if not same_cwd:
                        # Check restart guards (cooldown, inflight, Claude running)
                        allowed, block_msg = _checkin_can_restart(
                            name, tmux_name, host, pane_cwd or "", requested_cwd)
                        if not allowed:
                            notify_chat_id = get_manager_chat_id(name)
                            if notify_chat_id is not None:
                                send_telegram_message(notify_chat_id, block_msg)
                            self.send_response(200)
                            self.send_header("Content-Type", "text/plain")
                            self.end_headers()
                            self.wfile.write(block_msg.encode())
                            return

                        # Execute the restart
                        _log(_LOG_INFO, "checkin", f"{name}: triggering restart (cwd mismatch: pane={pane_cwd} vs requested={requested_cwd})")
                        ok, err = _checkin_do_restart(
                            name, backend_name, tmux_name, host, requested_cwd)
                        if not ok:
                            self.send_response(500)
                            self.send_header("Content-Type", "text/plain")
                            self.end_headers()
                            self.wfile.write(f"Failed to restart in {requested_cwd}: {err}".encode())
                        else:
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
        except (subprocess.SubprocessError, OSError, KeyError) as exc:
            _log(_LOG_ERROR, "bridge", f"Checkin endpoint error: {exc}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(exc).encode())

    def handle_health_workers_endpoint(self) -> None:
        """Return watchdog worker states as JSON (debug endpoint)."""
        try:
            now = _clock.time()
            registered = get_registered_sessions()
            with watchdog.lock:
                state_snapshot = dict(watchdog.worker_states)
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
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            _log(_LOG_ERROR, "worker", f"Health workers endpoint error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())











# ── Endpoint registration ──

def _setup_endpoint_routes() -> None:
    """Register all POST and GET endpoints in the endpoint router.

    Called once at module load time. Pattern handlers receive
    (handler_instance, body, re_match); exact handlers receive
    (handler_instance, body).
    """
    r = _endpoint_router

    # POST endpoints (exact match)
    r.post("/response", lambda h, b: h.handle_hook_response(b))
    r.post("/notify", lambda h, b: h.handle_notify(b))
    r.post("/send", lambda h, b: h.handle_send_endpoint(b))
    r.post("/pr-comment", lambda h, b: h.handle_pr_comment(b))
    r.post("/pr-general-comment", lambda h, b: h.handle_pr_general_comment(b))
    r.post("/pr-merge", lambda h, b: h.handle_pr_merge(b))
    r.post("/register", lambda h, b: h.handle_forge_register(b))
    r.post("/guest", lambda h, b: h.handle_guest_register(b))
    r.post("/channels", lambda h, b: h.handle_channel_create(b))
    r.post("/health-alert", lambda h, b: h.handle_health_alert(b))

    # POST endpoints (prefix/pattern match)
    r.post_pattern(r'^/guest/send', lambda h, b, m: h.handle_guest_send(b))
    r.post_pattern(r'^/guest/reply', lambda h, b, m: h.handle_guest_reply(b))
    r.post_pattern(
        r'^/channels/([^/]+)/members$',
        lambda h, b, m: h.handle_channel_members(m.group(1), b)
    )
    r.post_pattern(
        r'^/channels/([^/]+)/send$',
        lambda h, b, m: h.handle_channel_send(m.group(1), b)
    )
    r.post_pattern(
        r'^/v1/([^/]+)/send$',
        lambda h, b, m: h.handle_relay_send(m.group(1), b)
    )
    r.post_pattern(
        r'^/v1/([^/]+)/reply$',
        lambda h, b, m: h.handle_relay_reply(m.group(1), b)
    )

    # GET endpoints (exact match) — handler signature: (handler_instance, parsed)
    r.get("/guests", lambda h, p: h.handle_guests_list())
    r.get("/workers", lambda h, p: h.handle_workers_endpoint(p))
    r.get("/machines", lambda h, p: h.handle_machines_endpoint(p))
    r.get("/checkin", lambda h, p: h.handle_checkin_endpoint(p))
    r.get("/health/workers", lambda h, p: h.handle_health_workers_endpoint())
    r.get("/channels", lambda h, p: h.handle_channels_list(p))
    r.get("/pr-file-content", lambda h, p: h.handle_pr_file_content(p))
    r.get("/pr-keepalive", lambda h, p: h.handle_pr_keepalive(p))

    # GET endpoints (pattern match) — order matters for prefix collisions
    r.get_pattern(r'^/guest/inbox', lambda h, p, m: h.handle_guest_inbox(p))
    r.get_pattern(r'^/guest/status', lambda h, p, m: h.handle_guest_status(p))
    r.get_pattern(
        r'^/v1/([^/]+)(?:/(.+))?$',
        lambda h, p, m: h.handle_relay_get(m.group(1), m.group(2), p)
    )
    r.get_pattern(
        r'^/channels/([^/]+)/messages$',
        lambda h, p, m: h.handle_channel_messages(m.group(1), p)
    )
    r.get_pattern(r'^/team-chat-media/', lambda h, p, m: h.handle_team_chat_media(p))
    r.get_pattern(r'^/team-chat', lambda h, p, m: h.handle_team_chat_endpoint(p))
    r.get_pattern(r'^/transcript/', lambda h, p, m: h.handle_transcript_endpoint(p))
    r.get_pattern(r'^/pr-review/', lambda h, p, m: h.handle_pr_review_endpoint(p))

    # DELETE endpoints
    r.delete("/guest", lambda h, p: h.handle_guest_disconnect(p))
    r.delete_pattern(
        r'^/channels/([^/]+)$',
        lambda h, p, m: h.handle_channel_delete(m.group(1))
    )


_setup_endpoint_routes()


# ============================================================
# MAIN
# ============================================================

def graceful_shutdown(signum: int, frame: types.FrameType | None) -> None:
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
    except (OSError, UnicodeDecodeError) as exc:
        _log(_LOG_DEBUG, "io:graceful_shutdown", f"{type(exc).__name__}: {exc}")

    print(f"\n[{timestamp}] Received {sig_name} ({parent_info}), shutting down...")

    if grpc_server is not None:
        try:
            grpc_server.stop()
            print("gRPC server stopped")
        except OSError as e:
            _log(_LOG_WARN, "bridge", f"gRPC server stop failed: {e}")

    if gmail_connector_instance is not None:
        try:
            gmail_connector_instance.stop()
            print("Gmail connector stopped")
        except (RuntimeError, OSError) as exc:
            _log(_LOG_DEBUG, "io:graceful_shutdown", f"{type(exc).__name__}: {exc}")

    if github_connector_instance is not None:
        try:
            github_connector_instance.stop()
            print("GitHub connector stopped")
        except (RuntimeError, OSError) as exc:
            _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")

    send_shutdown_message()
    sys.exit(0)


def _discover_and_configure_sessions() -> dict[str, TmuxSessionDict]:
    """Discover existing tmux sessions and re-export hook env vars."""
    registered = scan_tmux_sessions()
    registered = get_registered_sessions(registered)
    if registered:
        print(f"Discovered sessions: {list(registered.keys())}")
        for name, info in registered.items():
            # SAFETY: only touch sessions that match OUR prefix
            tmux_name = info.get("tmux", f"{TMUX_PREFIX}{name}")
            if not tmux_name.startswith(TMUX_PREFIX):
                print(f"  SKIP {name}: tmux '{tmux_name}' doesn't match prefix '{TMUX_PREFIX}'")
                continue
            backend_name = get_worker_backend(name, info)
            backend_obj = get_backend(backend_name)
            if not backend_obj.is_interactive:
                ensure_worker_pipe(name)
            host = info.get("host") or get_worker_host(name)
            if tmux_exists(tmux_name, host=host):
                export_hook_env(tmux_name, backend_name, host=host)
    return registered


def _restore_bridge_state(registered: dict[str, TmuxSessionDict]) -> int | None:
    """Restore persisted bridge state (active worker, admin, relay/channel/guest).

    Returns the last known chat_id or None.
    """
    global admin_chat_id

    last_active = load_last_active()
    if last_active and last_active in registered:
        state["active"] = last_active
        print(f"Restored last active worker: {last_active}")
    elif last_active:
        print(f"Last active worker '{last_active}' no longer exists")

    # Log team dir status
    if os.path.isdir(TEAM_DIR):
        print(f"Team dir: {TEAM_DIR}")
        _startup_note = read_checkin_note()
        if _startup_note:
            print(f"  Checkin note: {_CHECKIN_NOTE_PATH} ({len(_startup_note)} chars)")
        else:
            print(f"  No checkin note at {_CHECKIN_NOTE_PATH}")
    else:
        print(f"Team dir not found: {TEAM_DIR} (checkin note disabled)")

    last_chat_id = load_last_chat_id()
    if last_chat_id:
        if admin_chat_id is None:
            admin_chat_id = last_chat_id
            print(f"Restored admin from last_chat_id: {admin_chat_id}")

    _relay_load()
    _channel_load()
    _guest_load()
    return last_chat_id


def _log_startup_info(registered: dict[str, TmuxSessionDict]) -> None:
    """Print startup configuration summary to stdout."""
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

    if SANDBOX_ENABLED:
        print(f"Sandbox mode: Workers run in Docker containers")
        print(f"Mounted: {Path.home()} → /workspace")
        if SANDBOX_EXTRA_MOUNTS:
            for host_path, container_path, ro in SANDBOX_EXTRA_MOUNTS:
                ro_flag = " (ro)" if ro else ""
                print(f"Mounted: {host_path} → {container_path}{ro_flag}")
        print("Workers can only access mounted directories")
    else:
        print("Sandbox mode: disabled (direct execution)")


def _send_startup_notification(last_chat_id: int, registered: dict[str, TmuxSessionDict]) -> None:
    """Send startup notification to admin via Telegram."""
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
        _log(_LOG_WARN, "bridge", f"Failed to send startup notification: {result}")


# ── Connector infrastructure (Gmail/GitHub) ────────────────────────

_connector_message_log: dict[str, Any] = {}  # tag -> deque of {ts, html, plain, targets}


def _connector_log_message(tag: str, html_text: str, plain_text: str, targets: list[str]) -> None:
    """Log a connector message for debugging (capped at 20 per tag)."""
    from collections import deque
    if tag not in _connector_message_log:
        _connector_message_log[tag] = deque(maxlen=20)
    _connector_message_log[tag].append({
        "ts": _clock.time(),
        "html": html_text,
        "plain": plain_text,
        "targets": targets or [],
    })


def _connector_render_html(tag: str, current_html: str) -> str:
    """Render HTML page with current message + recent history (rewind style)."""
    import html as html_mod
    esc = html_mod.escape
    msgs = list(_connector_message_log.get(tag, []))
    icon = "🔔" if tag == "github" else "📧"
    title = f"{tag.title()} Feed"

    blocks = []
    for i, m in enumerate(msgs):
        ts = time.strftime("%b %d, %H:%M", time.gmtime(m["ts"]))
        who = ", ".join(m["targets"]) if m["targets"] else "all"
        content = m["html"]
        content = re.sub(r'(https?://\S+)', r'<a href="\1" target="_blank" rel="noopener">\1</a>', content)
        content = content.replace("\n", "<br>")
        is_latest = (i == len(msgs) - 1)
        cls = "chat-msg latest" if is_latest else "chat-msg"
        badge = f'<span class="badge">Latest</span>' if is_latest else ""
        av_letter = tag[0].upper()
        av_color = "#8b5cf6" if tag == "github" else "#f59e0b"
        blocks.append(
            f'<div class="{cls}">'
            f'<div class="u-av"><svg viewBox="0 0 28 28"><rect width="28" height="28" rx="14" fill="{av_color}"/>'
            f'<text x="14" y="18" text-anchor="middle" fill="#fff" font-size="12" font-weight="600">{av_letter}</text></svg></div>'
            f'<div class="chat-body">'
            f'<span class="u-name">{esc(tag.title())}</span>'
            f'<span class="ts">{ts}</span>'
            f'<span class="target">→ {esc(who)}</span>'
            f'{badge}'
            f'<div class="chat-text">{content}</div>'
            f'</div></div>'
        )

    blocks_html = "\n".join(blocks)
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{icon} {title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
:root {{
  --bg:#0b0d0b; --fg:#e5e5e0; --border:rgba(135,139,134,.12); --muted:#9ca49c;
  --user-bg:rgba(255,255,255,.04); --code-bg:#1a1c1a; --link:#75dbf0; --radius:6px;
  --sans:"Inter",ui-sans-serif,system-ui,-apple-system,sans-serif;
  --accent:#8b5cf6;
}}
@media(prefers-color-scheme:light){{
  :root{{--bg:#fafaf8;--fg:#1a1a1a;--muted:#595959;--border:rgba(135,139,134,.2);
    --user-bg:rgba(0,0,0,.03);--code-bg:#f4f4f0;--link:#0969da;--accent:#7c3aed;}}
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html{{font-size:14px}}
body{{font-family:var(--sans);background:var(--bg);color:var(--fg);line-height:1.6;
  -webkit-font-smoothing:antialiased}}
.wrap{{max-width:48rem;margin:0 auto;padding:24px 16px 80px}}
header{{border-bottom:1px solid var(--border);padding-bottom:16px;margin-bottom:20px}}
h1{{font-size:1.3rem;font-weight:700}}
.meta{{color:var(--muted);font-size:.85rem;margin-top:4px}}
.thread{{display:flex;flex-direction:column;gap:4px}}
.chat-msg{{display:grid;grid-template-columns:28px 1fr;gap:10px;padding:8px 8px;
  border-radius:8px;transition:background .2s}}
.chat-msg:hover{{background:var(--user-bg)}}
.chat-msg.latest{{background:rgba(117,219,240,.06);border:1px solid rgba(117,219,240,.1)}}
.chat-body{{min-width:0}}
.u-av{{width:28px;height:28px;border-radius:50%;overflow:hidden;flex-shrink:0;margin-top:2px}}
.u-av svg{{width:100%;height:100%}}
.u-name{{font-weight:600;font-size:.85rem;margin-right:6px}}
.ts{{font-size:.75rem;color:var(--muted)}}
.target{{font-size:.75rem;color:var(--muted);margin-left:6px}}
.badge{{font-size:.65rem;font-weight:600;color:var(--link);background:rgba(117,219,240,.1);
  padding:1px 6px;border-radius:3px;margin-left:8px;text-transform:uppercase}}
.chat-text{{white-space:pre-wrap;word-break:break-word;font-size:.9rem;line-height:1.6;margin-top:2px}}
.chat-text a{{color:var(--link)}}
blockquote{{border-left:3px solid var(--border);padding-left:10px;margin:4px 0;color:var(--muted)}}
</style>
</head>
<body>
<div class="wrap">
<header>
<h1>{icon} {title}</h1>
<div class="meta">{len(msgs)} recent message{"s" if len(msgs) != 1 else ""} &middot; Updated {time.strftime("%b %d, %H:%M UTC", time.gmtime(_clock.time()))}</div>
</header>
<div class="thread">
{blocks_html}
</div>
</div>
</body>
</html>'''


def _connector_short_summary(tag: str, plain_text: str, serve_url: str | None = None, metadata: dict[str, Any] | None = None) -> str:
    """Create concise Telegram HTML summary (max 4 lines, clickable link)."""
    import html as _html
    icon = "🔔" if tag == "github" else "📧"
    body = plain_text.strip()
    body = re.sub(r'^manager\s*\(via\s+\w+[^)]*\):\s*', '', body)
    body = re.sub(r'\[thread:[^\]]+\]\s*', '', body)
    body = " ".join(body.split())
    if len(body) > 200:
        body = body[:197] + "…"
    ref_match = re.search(r'#(\d+)', plain_text)
    thread_match = re.search(r'\[thread:([^\]]+)\]', plain_text)
    header = f"{icon} <b>{tag.title()}</b>"
    if ref_match:
        num = ref_match.group(1)
        repo = (metadata or {}).get("repo", "BasedHardware/omi")
        gh_url = f"https://github.com/{repo}/issues/{num}"
        header += f' <a href="{gh_url}">#{num}</a>'
    elif thread_match:
        header += f" {_html.escape('thread:' + thread_match.group(1))}"
    parts = [header, _html.escape(body)]
    if serve_url:
        parts.append(f'<a href="{_html.escape(serve_url)}">View full →</a>')
    return "\n".join(parts)


def _connector_export_github(number: int, repo: str) -> str | None:
    """Export a GitHub issue/PR via beast github export --serve, return public URL."""
    try:
        r = _subprocess_runner.run(
            ["beast", "github", "export", str(number), "--format", "print",
             "--serve", "--repo", repo, "--fresh"],
            capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode == 0:
            for line in r.stderr.splitlines() + r.stdout.splitlines():
                if "http" in line and ("localhost" in line or "serve" in line.lower()):
                    url = line.strip().split()[-1].rstrip("/")
                    if "localhost" in url:
                        host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "157.180.48.254"
                        url = url.replace("localhost", host)
                    return url
    except subprocess.SubprocessError as e:
        _log(_LOG_WARN, "github", f"export failed for #{number}: {e}")
    return None


def _connector_on_message(tag: str) -> Callable[..., None]:
    """Create a message handler for a connector tag (Gmail/GitHub)."""
    def handler(targets: list[str], html_text: str, plain_text: str | None = None, attachments: list[str] | None = None, metadata: dict[str, Any] | None = None) -> None:
        """Route connector message to Telegram admin and/or target workers."""
        if plain_text is None:
            plain_text = html_text
        _connector_log_message(tag, html_text, plain_text, targets)
        if admin_chat_id:
            serve_url: str | None = None
            if tag == "github" and metadata and metadata.get("number"):
                try:
                    serve_url = _connector_export_github(
                        metadata["number"], metadata.get("repo", "BasedHardware/omi"))
                except KeyError as e:
                    _log(_LOG_WARN, tag, f"github export failed: {e}")
            if not serve_url:
                try:
                    page_html = _connector_render_html(tag, html_text)
                    tmp_path = f"/tmp/connector-{tag}.html"
                    with open(tmp_path, "w") as f:
                        f.write(page_html)
                    serve_url = _beast_serve_deploy(tmp_path, f"connector-{tag}")
                except OSError as e:
                    _log(_LOG_WARN, tag, f"beast serve failed: {e}")
            summary = _connector_short_summary(tag, plain_text, serve_url, metadata)
            try:
                send_telegram_message(admin_chat_id, summary, parse_mode="HTML")
            except OSError:
                try:
                    send_telegram_message(admin_chat_id, plain_text[:300])
                except (urllib.error.URLError, OSError, TimeoutError) as e:
                    _log(_LOG_WARN, tag, f"Telegram send failed: {e}")
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
                _log(_LOG_INFO, tag, f"attachment -> Telegram: {fname}")
        if targets:
            for name in targets:
                send_to_worker(name, plain_text)
                _log(_LOG_INFO, tag, f"-> {name}: {plain_text[:80]}...")
        else:
            _log(_LOG_INFO, tag, f"-> Telegram only (no mentions): {plain_text[:80]}...")
    return handler


def _connector_get_workers() -> set[str]:
    """Return the set of registered worker names for connector routing."""
    return set(get_registered_sessions().keys())


def _connector_on_alert(tag: str) -> Callable[[str], None]:
    """Create an alert handler for a connector tag (sends to admin chat)."""
    def handler(text: str) -> None:
        """Forward alert text to admin via Telegram."""
        if admin_chat_id:
            try:
                send_telegram_message(admin_chat_id, text)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                _log(_LOG_WARN, tag, f"Failed to send Telegram alert: {e}")
    return handler


def _start_grpc_server() -> Any:
    """Start the gRPC server if available, return the server instance or None.

    Returns BridgeGRPCServer when available, None otherwise.
    Typed as Any because BridgeGRPCServer is conditionally imported.
    """
    if BridgeGRPCServer is not None:
        try:
            server = BridgeGRPCServer(
                on_worker_response=handle_grpc_worker_response,
                on_worker_register=handle_grpc_worker_register,
                on_worker_disconnect=handle_grpc_worker_disconnect,
                on_jsonl_received=handle_grpc_jsonl_received,
            )
            server.start(GRPC_PORT)
            _log(_LOG_INFO, "grpc", f"gRPC server on {BRIDGE_BIND}:{GRPC_PORT}")
            return server
        except (OSError, KeyboardInterrupt) as e:
            _log(_LOG_WARN, "grpc", f"gRPC server disabled: {e}")
            return None
    elif BRIDGE_GRPC_IMPORT_ERROR is not None:
        _log(_LOG_ERROR, "bridge", f"gRPC server disabled: {BRIDGE_GRPC_IMPORT_ERROR}")
    return None


def _start_connectors() -> tuple[Any, Any]:
    """Start Gmail and GitHub connectors if enabled, return (gmail, github) instances."""
    gmail_inst = None
    if GMAIL_ENABLED and GmailConnector is not None:
        gmail_inst = GmailConnector(
            gws_bin=GMAIL_GWS_BIN,
            from_filter=GMAIL_FROM_FILTER,
            poll_interval=GMAIL_POLL_INTERVAL,
            on_message=_connector_on_message("gmail"),
            get_registered_workers=_connector_get_workers,
            on_alert=_connector_on_alert("gmail"),
        )
        gmail_inst.start()
        print(f"Gmail connector: polling every {GMAIL_POLL_INTERVAL}s for {GMAIL_FROM_FILTER}")
    elif GMAIL_ENABLED and GmailConnector is None:
        _log(_LOG_ERROR, "bridge", f"Gmail connector disabled: {GMAIL_IMPORT_ERROR}")

    github_inst = None
    if GITHUB_ENABLED and GitHubConnector is not None:
        github_inst = GitHubConnector(
            repo=GITHUB_REPO,
            from_user=GITHUB_FROM_USER,
            poll_interval=GITHUB_POLL_INTERVAL,
            on_message=_connector_on_message("github"),
            get_registered_workers=_connector_get_workers,
            on_alert=_connector_on_alert("github"),
            state_file=str(NODE_DIR / "github_state.json"),
        )
        github_inst.start()
        print(f"GitHub connector: polling every {GITHUB_POLL_INTERVAL}s for {GITHUB_FROM_USER} on {GITHUB_REPO}")
    elif GITHUB_ENABLED and GitHubConnector is None:
        _log(_LOG_ERROR, "bridge", f"GitHub connector disabled: {GITHUB_IMPORT_ERROR}")

    return gmail_inst, github_inst


def main() -> None:
    """Entry point — configure and start the bridge HTTP server.

    Orchestrates: validation → signal setup → session discovery →
    state restoration → startup logging → notification → background services.
    """
    global admin_chat_id, grpc_server, gmail_connector_instance, github_connector_instance

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

    registered = _discover_and_configure_sessions()
    last_chat_id = _restore_bridge_state(registered)
    _log_startup_info(registered)

    if last_chat_id:
        _send_startup_notification(last_chat_id, registered)

    watchdog = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog.start()

    _load_learning_reminder_state()
    _seed_learning_reminder_state(registered.keys())
    _schedule_idle_scan()
    print(f"Learning reminder idle scan: started (every 30 min, {len(learning_reminders.state)} workers tracked)")

    grpc_server = _start_grpc_server()
    gmail_connector_instance, github_connector_instance = _start_connectors()

    try:
        ReuseAddrServer((BRIDGE_BIND, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
