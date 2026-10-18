#!/usr/bin/env python3
from __future__ import annotations

import sys

if __name__ == "__main__":
    sys.modules.setdefault("bridge", sys.modules[__name__])

import abc
import argparse
import base64
import binascii
import collections
import difflib
import enum
import gzip
import hashlib
import html
import http.client
import http.server
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    NamedTuple,
    Optional,
    Protocol,
    TypedDict,
    cast,
)
from urllib.parse import ParseResult, parse_qs, urlparse
from urllib.parse import quote as _urlquote

import shared as core
from claudecode import *
from claudecode import (
    _INTERACTIVE_CONTENT,
    _INTERACTIVE_FOOTERS,
    _LEARNING_REMINDER_TEXT,
    _acquire_flock,
    _cache_session_id,
    _capture_pane_text,
    _codex_load_session_id,
    _codex_save_session_id,
    _codex_session_id_path,
    _detect_os_family,
    _ensure_workspace_trusted,
    _find_codex_transcript,
    _forward_pipe_message,
    _get_remote_home,
    _get_tmux_send_lock,
    _is_git_repo,
    _log_session_event,
    _project_slug,
    _release_flock,
    _remap_path,
    _remote_run,
    _resolve_remote_tool,
    _scan_latest_session_id,
    _tmux_pane_pids,
    _which_binary,
)
from shared import *
from shared import (
    _CHECKIN_NOTE_PATH,
    _DEFAULT_PORTS,
    _LEARNING_REMINDER_PATH,
    _LOG_DEBUG,
    _LOG_ERROR,
    _LOG_INFO,
    _LOG_WARN,
    _app_context,
    _bool_field,
    _bridge_url_env,
    _build_app_context,
    _build_tunnel_config,
    _clock,
    _GuestSessionDictRequired,
    _int_field,
    _log,
    _log_suppress,
    _MentionSnapshot,
    _node_name,
    _RealClock,
    _RealSubprocessRunner,
    _res_cfg,
    _StateSnapshot,
    _str_field,
    _subprocess_runner,
    _urlopen,
    _wd_cfg,
)
from telegram import *
from telegram import (
    _MEDIA_GROUP_WAIT,
    _build_cwd_change_notice,
    _collapse_excess_newlines,
    _extract_msg_text,
    _init_transport,
    _parse_media_tags,
    _pipe_tables_to_html,
    _prepare_photo_for_telegram,
    _render_md_inline_html,
    _render_md_inline_plain,
    _render_table_as_pre,
    _sanitize_telegram_html,
    _split_protected_segments,
    _TelegramHTMLSanitizer,
    _wrap_plain_tables,
)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
# Review templates (_REVIEW_CSS, _REVIEW_JS, _REVIEW_PAGE_TPL) removed — loaded by review/generate.py


# parse_patch() and generate_html() removed — served by review/generate.py


# Transcript dataclasses (PaginatedResult, EntriesResult, SearchResult, TranscriptSearchResult,
# TranscriptStats, _EMPTY_TRANSCRIPT_STATS) and Python indexer functions (_sanitize_fts5_query,
# _setup_db, _check_incremental, _update_meta, _paginate, _transcript_create_tables,
# _extract_plain_text, _transcript_index, _transcript_query_entries, _transcript_query_search,
# _transcript_query_stats, _run_transcript, _indexer_main) removed — all served by spawn-on-demand
# Bun viewer with bun:sqlite FTS5 (rewind/)


class UrlOpenFn(Protocol):
    def __call__(self, req: urllib.request.Request, *, timeout: float) -> http.client.HTTPResponse: ...
class TunnelState(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    POLL_FALLBACK = "poll_fallback"
    FAILED = "failed"


class TunnelManager:
    def __init__(
        self,
        config: TunnelConfig,
        bot_token: str,
        port: int,
        node_dir: Path,
        webhook_secret: str = "",
        bind_host: str = "",
        subprocess_runner: SubprocessRunner | None = None,
        clock: Clock | None = None,
        urlopen: UrlOpenFn | None = None,
        on_notify: Callable[[str], None] | None = None,
        on_update: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._config = config
        self._token = bot_token
        self._port = port
        self._node_dir = node_dir
        self._bind_host = bind_host
        self._webhook_secret = webhook_secret
        self._runner = subprocess_runner or _RealSubprocessRunner()
        self._clock = clock or _RealClock()
        self._urlopen: UrlOpenFn = urlopen or cast(UrlOpenFn, _urlopen)
        self._on_notify = on_notify or (lambda _msg: None)
        self._on_update = on_update
        self._lock = threading.Lock()
        self._state = TunnelState.STOPPED
        self._tunnel_url: str = ""
        self._tunnel_proc: subprocess.Popen[str] | None = None
        self._polling_active: bool = False
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        # Poll health tracking
        self._poll_last_ok: float = 0.0  # monotonic time of last successful getUpdates
        self._poll_last_error: float = 0.0
        self._poll_error_streak: int = 0
        self._poll_updates_total: int = 0
        self._poll_heartbeat_at: float = 0.0  # last heartbeat log time
        self._poll_stale_alerted: bool = False

    def start(self) -> None:
        if self._config.mode == "none":
            _log(_LOG_INFO, "tunnel", "Tunnel disabled (mode=none)")
            return
        self._kill_stale_tunnel()
        self._stop_event.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="tunnel-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        health.register_thread("tunnel-watchdog", self._watchdog_thread)

    def stop(self) -> None:
        self._stop_event.set()
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=10)
        self._kill_cloudflared()
        self._state = TunnelState.STOPPED
        _log(_LOG_INFO, "tunnel", "Tunnel manager stopped")

    @property
    def state(self) -> TunnelState:
        return self._state

    @property
    def tunnel_url(self) -> str:
        return self._tunnel_url

    @property
    def polling_active(self) -> bool:
        return self._polling_active

    def status(self) -> dict[str, str | bool | int | float | None]:
        now = self._clock.time()
        poll_age = round(now - self._poll_last_ok, 1) if self._poll_last_ok > 0 else None
        return {
            "mode": self._config.mode,
            "state": self._state.value,
            "tunnel_url": self._tunnel_url,
            "polling_active": self._polling_active,
            "poll_last_ok_ago_s": poll_age,
            "poll_error_streak": self._poll_error_streak,
            "poll_updates_total": self._poll_updates_total,
            "poll_thread_alive": self._poll_thread.is_alive() if self._poll_thread else None,
        }

    def _telegram_api(self, method: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with self._urlopen(req, timeout=10) as resp:
                result: dict[str, object] = json.loads(resp.read())
                return result
        except Exception:
            return {}

    def _tg_set_webhook(self, webhook_url: str, secret_token: str = "") -> dict[str, object]:
        payload: dict[str, object] = {"url": webhook_url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._telegram_api("setWebhook", payload)

    def _tg_delete_webhook(self) -> dict[str, object]:
        return self._telegram_api("deleteWebhook")

    def _tg_get_webhook_info(self) -> dict[str, object]:
        return self._telegram_api("getWebhookInfo")

    def _tg_get_me(self) -> dict[str, object]:
        return self._telegram_api("getMe")

    def _watchdog_loop(self) -> None:
        self._state = TunnelState.STARTING
        if not self._wait_for_port():
            _log(_LOG_ERROR, "tunnel", f"Bridge did not bind to port {self._port}")
            self._state = TunnelState.FAILED
            return
        if self._config.mode == "poll":
            _log(_LOG_INFO, "tunnel", "Poll mode — using getUpdates long-polling (no tunnel)")
            self._start_poll_fallback()
        elif self._config.mode == "provided":
            self._tunnel_url = self._config.provided_url
            _log(_LOG_INFO, "tunnel", f"Using provided tunnel URL: {self._tunnel_url}")
        elif self._config.mode == "auto":
            url = self._start_cloudflared()
            if url:
                self._tunnel_url = url
                _log(_LOG_INFO, "tunnel", f"Tunnel ready: {self._tunnel_url}")
                self._save_tunnel_url(url)
            else:
                _log(_LOG_WARN, "tunnel", "Tunnel failed to start — using poll fallback")
                self._start_poll_fallback()
        if self._tunnel_url and not self._polling_active:
            if not self._set_webhook_with_retry(self._tunnel_url):
                _log(_LOG_WARN, "tunnel", "Webhook DNS not ready — using poll fallback")
                self._start_poll_fallback()
            else:
                self._state = TunnelState.RUNNING
        self._save_bot_info()
        check_counter = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(self._config.watchdog_interval)
            if self._stop_event.is_set():
                break
            if self._tunnel_proc is not None:
                problem = self._check_tunnel_health()
                if problem == "process died":
                    _log(_LOG_WARN, "tunnel", "Tunnel process died, restarting...")
                    self._on_notify("⚠️ Tunnel process died. Reconnecting...")
                    self._state = TunnelState.RESTARTING
                    new_url = self._restart_with_retry()
                    if new_url:
                        self._tunnel_url = new_url
                        self._save_tunnel_url(new_url)
                        _log(_LOG_INFO, "tunnel", f"Tunnel restarted: {new_url}")
                        self._stop_poll_fallback()
                        if self._set_webhook_with_retry(new_url):
                            self._state = TunnelState.RUNNING
                            self._on_notify("✅ Tunnel reconnected")
                        else:
                            _log(_LOG_WARN, "tunnel", "Webhook DNS not ready after restart")
                            self._start_poll_fallback()
                            self._on_notify("📡 Using poll fallback while DNS propagates...")
                    else:
                        _log(_LOG_WARN, "tunnel", "Tunnel restart failed after retries")
                        self._start_poll_fallback()
                        continue
                elif problem == "unreachable":
                    if not self._polling_active:
                        _log(_LOG_WARN, "tunnel", "Tunnel URL unreachable (DNS?), falling back to polling")
                        self._start_poll_fallback()
            if self._polling_active and self._poll_thread and not self._poll_thread.is_alive():
                _log(_LOG_WARN, "tunnel", "Poll fallback thread died, restarting...")
                self._on_notify("⚠️ Poll thread died — restarting now")
                self._poll_stop.clear()
                self._start_poll_fallback()
            # Stale poll detection: alert if no successful poll for 3+ minutes
            if self._polling_active and self._poll_last_ok > 0:
                stale_seconds = self._clock.time() - self._poll_last_ok
                if stale_seconds > 180 and not self._poll_stale_alerted:
                    self._poll_stale_alerted = True
                    msg = f"⚠️ Poll stale: no successful getUpdates for {int(stale_seconds)}s (error_streak={self._poll_error_streak})"
                    _log(_LOG_WARN, "tunnel", msg)
                    self._on_notify(msg)
            check_counter = (check_counter + 1) % self._config.webhook_check_cycles
            if check_counter == 0:
                if self._tunnel_url:
                    self._periodic_webhook_check()
                elif self._polling_active and self._tunnel_proc is None and self._config.mode == "auto":
                    _log(_LOG_INFO, "tunnel", "Retrying cloudflared start...")
                    url = self._start_cloudflared()
                    if url:
                        self._tunnel_url = url
                        self._save_tunnel_url(url)
                        _log(_LOG_INFO, "tunnel", f"Tunnel established: {url}")
                        self._stop_poll_fallback()
                        if self._set_webhook_with_retry(url):
                            self._state = TunnelState.RUNNING
                            self._on_notify("✅ Tunnel established (webhook active)")
                        else:
                            _log(_LOG_WARN, "tunnel", "Webhook DNS not ready after tunnel start")
                            self._start_poll_fallback()

    def _start_cloudflared(self) -> str:
        log_file = self._node_dir / "tunnel.log"
        log_file.write_text("")
        try:
            proc = self._runner.popen(
                [self._config.cloudflared_binary, "tunnel", "--url", f"http://localhost:{self._port}"],
                stdout=open(log_file, "w"),  # noqa: SIM115
                stderr=subprocess.STDOUT,
            )
        except (OSError, FileNotFoundError) as exc:
            _log(_LOG_ERROR, "tunnel", f"Failed to start cloudflared: {exc}")
            return ""
        self._tunnel_proc = proc
        self._save_pid(proc.pid)
        url = self._wait_for_url(log_file, self._config.startup_timeout)
        if not url:
            self._kill_cloudflared()
        return url

    def _wait_for_url(self, log_file: Path, timeout: int) -> str:
        elapsed = 0
        while elapsed < timeout and not self._stop_event.is_set():
            self._clock.sleep(1)
            elapsed += 1
            try:
                text = log_file.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                for word in line.split():
                    if (
                        word.startswith("https://")
                        and ".trycloudflare.com" in word
                        and "api.trycloudflare.com" not in word
                    ):
                        return word.rstrip("/")
        return ""

    def _kill_cloudflared(self) -> None:
        proc = self._tunnel_proc
        if proc is not None:
            with suppress(OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait(timeout=5)
            self._tunnel_proc = None

    def _is_tunnel_alive(self) -> bool:
        proc = self._tunnel_proc
        return proc is not None and proc.poll() is None

    def _is_tunnel_reachable(self) -> bool:
        if not self._tunnel_url:
            return False
        try:
            req = urllib.request.Request(self._tunnel_url, method="HEAD")
            self._urlopen(req, timeout=self._config.reachability_timeout)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _check_tunnel_health(self) -> str:
        if not self._is_tunnel_alive():
            return "process died"
        if not self._is_tunnel_reachable():
            return "unreachable"
        return ""

    def _restart_with_retry(self) -> str:
        backoff = self._config.initial_backoff
        for attempt in range(1, self._config.max_restart_attempts + 1):
            if attempt > 1:
                _log(
                    _LOG_INFO,
                    "tunnel",
                    f"Restart attempt {attempt}/{self._config.max_restart_attempts} (backoff {backoff}s)",
                )
                self._clock.sleep(backoff)
                backoff *= 2
            self._kill_cloudflared()
            url = self._start_cloudflared()
            if url:
                return url
        return ""

    def _set_webhook_with_retry(self, url: str) -> bool:
        for delay in self._config.webhook_retry_delays:
            if delay > 0:
                _log(_LOG_INFO, "tunnel", f"Webhook not ready, retrying in {delay}s...")
                self._clock.sleep(delay)
            if self._stop_event.is_set():
                return False
            resp = self._tg_set_webhook(url, secret_token=self._webhook_secret)
            if resp.get("ok"):
                _log(_LOG_INFO, "tunnel", "Webhook configured")
                return True
        self._tg_delete_webhook()
        return False

    def _periodic_webhook_check(self) -> None:
        if self._polling_active:
            self._stop_poll_fallback()
            resp = self._tg_set_webhook(self._tunnel_url, secret_token=self._webhook_secret)
            if resp.get("ok"):
                self._state = TunnelState.RUNNING
                _log(_LOG_INFO, "tunnel", "Webhook restored, poll fallback stopped")
                self._on_notify("✅ Webhook restored (DNS resolved)")
            else:
                self._tg_delete_webhook()
                self._start_poll_fallback()
        else:
            info = self._tg_get_webhook_info()
            if info:
                result = info.get("result", {})
                current_url = result.get("url", "") if isinstance(result, dict) else ""
                if not current_url or self._tunnel_url not in current_url:
                    resp = self._tg_set_webhook(self._tunnel_url, secret_token=self._webhook_secret)
                    if resp.get("ok"):
                        _log(_LOG_INFO, "tunnel", "Webhook re-registered (was stale)")
                        self._on_notify("✅ Webhook re-registered")

    def _start_poll_fallback(self) -> None:
        if self._polling_active:
            return
        self._tg_delete_webhook()
        self._clock.sleep(1)
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="tunnel-poll",
            daemon=True,
        )
        self._poll_thread.start()
        health.register_thread("tunnel-poll", self._poll_thread)
        self._polling_active = True
        self._state = TunnelState.POLL_FALLBACK
        _log(_LOG_INFO, "tunnel", "Poll fallback started")

    def _stop_poll_fallback(self) -> None:
        if not self._polling_active:
            return
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._polling_active = False
        _log(_LOG_INFO, "tunnel", "Poll fallback stopped")

    def _poll_loop(self) -> None:
        offset = 0
        _bridge_host = self._bind_host or "127.0.0.1"
        _log(_LOG_INFO, "tunnel:poll", f"Poll loop started (bridge={_bridge_host}:{self._port})")
        now = self._clock.time()
        self._poll_last_ok = now
        self._poll_heartbeat_at = now
        self._poll_error_streak = 0
        self._poll_stale_alerted = False
        while not self._poll_stop.is_set():
            try:
                url = f"https://api.telegram.org/bot{self._token}/getUpdates?offset={offset}&timeout={self._config.poll_timeout}"
                req = urllib.request.Request(url)
                with self._urlopen(req, timeout=self._config.poll_timeout + 5) as resp:
                    data = json.loads(resp.read())
                if not data.get("ok"):
                    self._clock.sleep(1)
                    continue
                # Successful poll
                now = self._clock.time()
                self._poll_last_ok = now
                self._poll_error_streak = 0
                health.ok("poll")
                if self._poll_stale_alerted:
                    self._poll_stale_alerted = False
                    _log(_LOG_INFO, "tunnel:poll", "Poll recovered — getUpdates responding again")
                    self._on_notify("✅ Poll recovered — getUpdates responding again")
                n_updates = len(data.get("result", []))
                self._poll_updates_total += n_updates
                # Heartbeat every 5 minutes
                if now - self._poll_heartbeat_at >= 300:
                    _log(
                        _LOG_INFO,
                        "tunnel:poll",
                        f"heartbeat: {self._poll_updates_total} updates total, streak_ok, offset={offset}",
                    )
                    self._poll_heartbeat_at = now
                for update in data.get("result", []):
                    uid = update.get("update_id", 0)
                    if self._on_update:
                        with _log_suppress(
                            Exception, level=_LOG_ERROR, tag="tunnel:poll", msg=f"Forward failed update_id={uid}: "
                        ):
                            self._on_update(update)
                    else:
                        self._forward_to_localhost(update)
                    offset = uid + 1
            except Exception as exc:
                if not self._poll_stop.is_set():
                    now = self._clock.time()
                    self._poll_last_error = now
                    self._poll_error_streak += 1
                    health.error("poll")
                    _log(_LOG_WARN, "tunnel:poll", f"Poll error (streak={self._poll_error_streak}): {exc}")
                    self._clock.sleep(self._config.poll_error_delay)

    def _forward_to_localhost(self, update: dict[str, object]) -> None:
        _bridge_host = self._bind_host or "127.0.0.1"
        with _log_suppress(Exception, tag="tunnel:poll", msg=f"Forward to {_bridge_host} failed: "):
            req = urllib.request.Request(
                f"http://{_bridge_host}:{self._port}/",
                data=json.dumps(update).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self._urlopen(req, timeout=5)

    def _wait_for_port(self) -> bool:
        elapsed = 0
        bind_host = self._bind_host or "127.0.0.1"
        while elapsed < self._config.port_wait_timeout and not self._stop_event.is_set():
            with suppress(ConnectionRefusedError, OSError), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect((bind_host, self._port))
                _log(_LOG_INFO, "tunnel", f"Bridge ready on {bind_host}:{self._port} ({elapsed}s)")
                return True
            self._clock.sleep(1)
            elapsed += 1
        return False

    def _save_tunnel_url(self, url: str) -> None:
        with suppress(OSError):
            (self._node_dir / "tunnel_url").write_text(url)

    def _kill_stale_tunnel(self) -> None:
        pid_file = self._node_dir / "tunnel.pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        with suppress(OSError, ProcessLookupError):
            os.kill(pid, 9)
            _log(_LOG_WARN, "tunnel", f"Killed stale cloudflared (pid={pid})")
        with suppress(OSError):
            pid_file.unlink()

    def _save_pid(self, pid: int) -> None:
        with suppress(OSError):
            (self._node_dir / "tunnel.pid").write_text(str(pid))

    def _save_bot_info(self) -> None:
        with _log_suppress(Exception, level=_LOG_DEBUG, tag="tunnel:bot-info"):
            info = self._tg_get_me()
            if info.get("ok"):
                result = info.get("result", {})
                if isinstance(result, dict):
                    bot_id = result.get("id")
                    username = result.get("username", "")
                    if bot_id:
                        (self._node_dir / "bot_id").write_text(str(bot_id))
                    if username:
                        (self._node_dir / "bot_username").write_text(username)


class MessageCallback(Protocol):
    def __call__(
        self,
        targets: list[str],
        html_text: str,
        plain_text: str,
        attachments: list[Attachment],
        metadata: ConnectorMetadata | None = None,
    ) -> None: ...
class AlertCallback(Protocol):
    def __call__(self, text: str) -> None: ...
class WorkerListCallback(Protocol):
    def __call__(self) -> set[str]: ...


_REPO_SEGMENT = r"[a-zA-Z0-9](?:[a-zA-Z0-9_-]|\.(?!\.))*[a-zA-Z0-9]"
_REPO_PATTERN = re.compile(rf"^(?:{_REPO_SEGMENT}|[a-zA-Z0-9])/(?:{_REPO_SEGMENT}|[a-zA-Z0-9])$")
_MAX_FILENAME_LEN = 200  # Limit attachment filename length
_B64_URLSAFE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_MAX_MIME_DEPTH = 10  # Max recursion depth for MIME part traversal
_MAX_MIME_PARTS = 100  # Max total MIME parts to process
CONSECUTIVE_FAIL_WARN = 3
CONSECUTIVE_FAIL_REBOOTSTRAP = 5
_MAX_POLL_INTERVAL = 3600
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB
_MAX_SEEN_IDS = 500
_PRUNE_KEEP = 200


def _atomic_write(path: str, payload: str | bytes) -> None:
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".tmp_", suffix=".tmp")
    renamed = False
    try:
        os.fchmod(fd, 0o600)
        mode = "wb" if isinstance(payload, bytes) else "w"
        with os.fdopen(fd, mode) as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        renamed = True
        with suppress(OSError):
            dir_fd = os.open(dirname, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        if not renamed:
            with suppress(OSError):
                os.unlink(tmp)
        raise


class BaseConnector[M](abc.ABC):
    connector_name: str = "base"

    def __init__(
        self,
        sender_filter: str,
        poll_interval: int,
        on_message: MessageCallback,
        get_registered_workers: WorkerListCallback,
        on_alert: AlertCallback | None = None,
    ) -> None:
        if not sender_filter or not sender_filter.strip():
            raise ValueError(f"{self.connector_name}: sender_filter is required (security: cannot be empty)")
        if isinstance(poll_interval, bool) or poll_interval < 1 or poll_interval > _MAX_POLL_INTERVAL:
            raise ValueError(
                f"{self.connector_name}: poll_interval must be 1–{_MAX_POLL_INTERVAL}, got {poll_interval}"
            )
        self.sender_filter: str = sender_filter.strip().lower()
        self.poll_interval: int = poll_interval
        self.on_message: MessageCallback = on_message
        self.get_registered_workers: WorkerListCallback = get_registered_workers
        self.on_alert: AlertCallback | None = on_alert
        self._stop_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures: int = 0
        self._handler_errors: int = 0  # messages that arrived but failed to deliver
        self._handler_error_alerted: bool = False
        self._alert_sent: bool = False
        self._lock: threading.RLock = threading.RLock()

    @abc.abstractmethod
    def preflight_check(self) -> tuple[bool, str]: ...
    @abc.abstractmethod
    def poll_once(self) -> None: ...
    @abc.abstractmethod
    def extract_sender(self, message: M) -> str: ...
    def _on_preflight_ok(self) -> None:  # noqa: B027
        pass

    def _start_thread(self, skip_preflight: bool = False) -> threading.Thread:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            kwargs={"skip_preflight": skip_preflight} if skip_preflight else {},
            daemon=True,
            name=f"{self.connector_name}-poller",
        )
        self._thread.start()
        return self._thread

    def start(self) -> threading.Thread:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(f"{self.connector_name}: already running (call stop() first)")
            return self._start_thread()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=5)

    def restart(self) -> tuple[bool, str]:
        self.stop()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, f"{self.connector_name}: old thread did not exit in time"
            self._consecutive_failures = 0
            self._alert_sent = False
        ok, msg = self.preflight_check()
        if not ok:
            return False, msg
        with self._lock:
            self._start_thread(skip_preflight=True)
        return True, msg

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status(self) -> ConnectorStatus:
        with self._lock:
            s = ConnectorStatus(
                name=self.connector_name,
                running=self.running,
                sender_filter=self.sender_filter,
                poll_interval=self.poll_interval,
                consecutive_failures=self._consecutive_failures,
                alert_sent=self._alert_sent,
            )
            s["handler_errors"] = self._handler_errors  # type: ignore[typeddict-unknown-key]
            return s

    def is_allowed_sender(self, message: M) -> bool:
        return self.extract_sender(message) == self.sender_filter

    def parse_mentions(self, text: str) -> tuple[list[str], str]:
        if not text:
            return [], ""
        registered = self.get_registered_workers()
        found: list[str] = []
        for match in re.finditer(r"@([a-zA-Z0-9_-]+)", text):
            name = match.group(1).lower()
            if name in registered and name not in found:
                found.append(name)
        if not found:
            return [], text
        found_set = set(found)
        cleaned = re.sub(
            r"@([a-zA-Z0-9_-]+)",
            lambda m: "" if m.group(1).lower() in found_set else m.group(0),
            text,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return found, cleaned

    def _send_alert(self, text: str) -> None:
        tag = f"[{self.connector_name}]"
        _log(_LOG_WARN, self.connector_name, f"ALERT: {text}")
        alert_fn = self.on_alert  # Capture ref outside lock
        if alert_fn is None:
            return
        with self._lock:
            if self._alert_sent:
                return
            self._alert_sent = True
        try:
            alert_fn(f"{tag} {text}")
        except Exception as e:
            _log(_LOG_WARN, self.connector_name, f"Failed to send alert: {e}")
            with self._lock:
                self._alert_sent = False  # Rollback — allow retry

    def _clear_alert(self) -> None:
        with self._lock:
            was_alerted = self._alert_sent
            self._alert_sent = False
        if was_alerted:
            tag = f"[{self.connector_name}]"
            if self.on_alert:
                try:
                    self.on_alert(f"{tag} Recovered — polling resumed")
                except Exception as e:
                    _log(_LOG_WARN, self.connector_name, f"Failed to send recovery alert: {e}")

    def track_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            should_alert = self._consecutive_failures >= CONSECUTIVE_FAIL_WARN and not self._alert_sent
        if should_alert:
            self._send_alert(f"Polling failing ({self._consecutive_failures} consecutive errors)")

    def track_success(self) -> None:
        with self._lock:
            was_failing = self._consecutive_failures > 0
            self._consecutive_failures = 0
        if was_failing:
            self._clear_alert()

    def track_handler_error(self, error: str) -> None:
        """Track a message that arrived but failed to deliver (handler error)."""
        with self._lock:
            self._handler_errors += 1
            count = self._handler_errors
            should_alert = count >= 3 and not self._handler_error_alerted
            if should_alert:
                self._handler_error_alerted = True
        if should_alert:
            tag = f"[{self.connector_name}]"
            msg = f"{tag} ⚠️ {count} messages arrived but failed to deliver: {error}"
            _log(_LOG_WARN, self.connector_name, f"ALERT: handler errors = {count}")
            if self.on_alert:
                try:
                    self.on_alert(msg)
                except Exception as e:
                    _log(_LOG_WARN, self.connector_name, f"Failed to send handler alert: {e}")

    def clear_handler_errors(self) -> None:
        with self._lock:
            self._handler_errors = 0
            self._handler_error_alerted = False

    def _poll_loop(self, skip_preflight: bool = False) -> None:
        tag = f"[{self.connector_name}]"
        if not skip_preflight:
            ok, msg = self.preflight_check()
            if not ok:
                _log(_LOG_ERROR, self.connector_name, f"Preflight FAILED: {msg}")
                _log(_LOG_INFO, self.connector_name, "Retrying in 60s...")
                if self._stop_event.wait(60):
                    return
                ok, msg = self.preflight_check()
                if not ok:
                    self._send_alert(f"{self.connector_name} disabled — {msg}")
                    return
            _log(_LOG_INFO, self.connector_name, f"Preflight OK: {msg}")
        else:
            _log(_LOG_INFO, self.connector_name, "Preflight OK (restart)")
        self._on_preflight_ok()
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                _log(_LOG_ERROR, self.connector_name, f"Poll error: {e}")
                self.track_failure()
            self._stop_event.wait(self.poll_interval)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _sanitize_filename(name: str) -> str:
    name = os.path.basename(name)  # strip directory components
    name = name.replace("..", "_").replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\-]", "_", name)  # keep only safe chars; name = name or "attachment"
    if len(name) > _MAX_FILENAME_LEN:
        root, ext = os.path.splitext(name)
        if len(ext) >= _MAX_FILENAME_LEN:
            ext = ext[:10]  # Truncate absurdly long extensions
        name = root[: _MAX_FILENAME_LEN - len(ext)] + ext
    return name


def _truncate(text: str, limit: int = 1500) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut + "\n… (truncated)"


def _get_header(headers: list[GmailHeader], name: str) -> str:
    target = name.lower()
    for h in headers:
        if not isinstance(h, dict):
            continue
        h_name = h.get("name")
        if not isinstance(h_name, str):
            continue
        if h_name.lower() == target:
            h_value = h.get("value")
            return str(h_value) if isinstance(h_value, str) else ""
    return ""


def _safe_json_loads_list(raw: str) -> list[GithubComment]:
    raw = raw.strip()
    if not raw:
        return []
    raw_items: list[object] = []
    try:
        result: object = json.loads(raw)
        if isinstance(result, list):
            raw_items = result
        else:
            return []  # unexpected type
    except json.JSONDecodeError:
        for chunk in re.split(r"\]\s*\[", raw):
            chunk = chunk.strip()
            if not chunk.startswith("["):
                chunk = "[" + chunk
            if not chunk.endswith("]"):
                chunk = chunk + "]"
            try:
                parsed: object = json.loads(chunk)
                if isinstance(parsed, list):
                    raw_items.extend(parsed)
            except json.JSONDecodeError:
                continue
    return [cast(GithubComment, item) for item in raw_items if isinstance(item, dict)]


class GmailConnector(BaseConnector[GmailMessage]):
    connector_name: str = "gmail"

    def __init__(
        self,
        gws_bin: str,
        from_filter: str,
        poll_interval: int,
        on_message: MessageCallback,
        get_registered_workers: WorkerListCallback,
        on_alert: AlertCallback | None = None,
        history_file: str = "",
    ) -> None:
        super().__init__(
            sender_filter=from_filter,
            poll_interval=poll_interval,
            on_message=on_message,
            get_registered_workers=get_registered_workers,
            on_alert=on_alert,
        )
        self.gws_bin: str = gws_bin
        self._history_id: str | None = None
        self._history_file_path: str = history_file or os.path.join(
            os.path.expanduser("~"),
            ".cache",
            "beast",
            "email",
            "gmail_history_id",
        )

    def preflight_check(self) -> tuple[bool, str]:
        if not os.path.isfile(self.gws_bin):
            return False, f"gws binary not found at {self.gws_bin}"
        if not os.access(self.gws_bin, os.X_OK):
            return False, f"gws binary not executable: {self.gws_bin}"
        profile = self._get_profile()
        if profile is None:
            return False, "gws auth failed — token may be expired (run: gws gmail users getProfile)"
        email = profile.get("emailAddress")
        email_str: str = str(email) if isinstance(email, str) else "?"
        return True, f"OK (email={email_str})"

    def status(self) -> GmailStatus:
        with self._lock:
            s = GmailStatus(
                name=self.connector_name,
                running=self.running,
                sender_filter=self.sender_filter,
                poll_interval=self.poll_interval,
                consecutive_failures=self._consecutive_failures,
                alert_sent=self._alert_sent,
                history_id=self._history_id,
            )
            s["handler_errors"] = self._handler_errors  # type: ignore[typeddict-unknown-key]
            return s

    def _run_gws(self, *args: str, json_body: str | None = None) -> dict[str, object] | None:
        cmd: list[str] = [self.gws_bin, "gmail", "users", *list(args)]
        if json_body:
            cmd.extend(["--json", json_body])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                stderr = result.stderr[:200]
                _log(_LOG_WARN, "gmail", f"gws error: {stderr}")
                if any(kw in stderr.lower() for kw in ("token", "auth", "expired")):
                    _log(
                        _LOG_ERROR,
                        "gmail",
                        'TOKEN MAY BE EXPIRED — run: gws gmail users getProfile --params \'{"userId":"me"}\'',
                    )

                return None
            parsed: object = json.loads(result.stdout)
            return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            _log(_LOG_WARN, "gmail", f"gws call failed: {e}")
            return None

    def _get_profile(self) -> GmailProfile | None:
        return cast(GmailProfile | None, self._run_gws("getProfile", "--params", '{"userId":"me"}'))

    def get_message(self, msg_id: str) -> GmailMessage | None:
        params = json.dumps({"userId": "me", "id": msg_id, "format": "full"})
        return cast(GmailMessage | None, self._run_gws("messages", "get", "--params", params))

    def _get_history(self, start_history_id: str) -> GmailHistoryResponse | None:
        params = json.dumps({"userId": "me", "startHistoryId": start_history_id})
        return cast(GmailHistoryResponse | None, self._run_gws("history", "list", "--params", params))

    def _get_attachment_data(self, msg_id: str, att_id: str) -> GmailAttachmentData | None:
        params = json.dumps({"userId": "me", "messageId": msg_id, "id": att_id})
        return cast(GmailAttachmentData | None, self._run_gws("messages", "attachments", "get", "--params", params))

    def _list_messages(self, query: str, max_results: int = 5) -> GmailMessageListResponse | None:
        params = json.dumps({"userId": "me", "maxResults": max_results, "q": query})
        return cast(GmailMessageListResponse | None, self._run_gws("messages", "list", "--params", params))

    def mark_as_read(self, msg_id: str) -> bool:
        params = json.dumps({"userId": "me", "id": msg_id})
        body = json.dumps({"removeLabelIds": ["UNREAD"]})
        result = self._run_gws("messages", "modify", "--params", params, json_body=body)
        return result is not None

    def _save_history_id(self) -> None:
        with self._lock:
            hid = self._history_id
        if not hid:
            return
        try:
            _atomic_write(self._history_file_path, hid)
        except OSError as e:
            _log(_LOG_WARN, "gmail", f"Failed to save historyId: {e}")

    def _load_history_id(self) -> str | None:
        try:
            with open(self._history_file_path) as f:
                hid = f.read().strip()
                if hid:
                    return hid
        except FileNotFoundError:
            pass
        except OSError as e:
            _log(_LOG_WARN, "gmail", f"Failed to load historyId: {e}")
        return None

    def _bootstrap_history_id(self, skip_disk: bool = False) -> str | None:
        if not skip_disk:
            saved = self._load_history_id()
            if saved:
                _log(_LOG_INFO, "gmail", f"Resumed historyId={saved} from disk")
                return saved
        profile = self._get_profile()
        if profile and profile.get("historyId"):
            hid = str(profile["historyId"])
            with self._lock:
                self._history_id = hid
            self._save_history_id()
            return hid
        return None

    def _hdrs(self, message: GmailMessage) -> list[GmailHeader]:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return []
        headers = payload.get("headers")
        return headers if isinstance(headers, list) else []

    def extract_sender(self, message: GmailMessage) -> str:
        from_val = _get_header(self._hdrs(message), "from")
        if not from_val:
            return ""
        match = re.search(r"<([^>]+)>", from_val)
        return match.group(1).lower() if match else from_val.strip().lower()

    def extract_sender_name(self, message: GmailMessage) -> str:
        from_val = _get_header(self._hdrs(message), "from")
        if not from_val:
            return ""
        match = re.match(r"^([^<]+)\s*<", from_val)
        return match.group(1).strip().strip('"') if match else from_val.split("@")[0]

    def extract_subject(self, message: GmailMessage) -> str:
        return _get_header(self._hdrs(message), "subject")

    def extract_message_id(self, message: GmailMessage) -> str:
        return _get_header(self._hdrs(message), "message-id")

    def is_sent_message(self, message: GmailMessage) -> bool:
        label_ids = message.get("labelIds")
        if not isinstance(label_ids, list):
            return False
        return "SENT" in label_ids

    def extract_body_text(self, message: GmailMessage) -> str:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return ""
        return self._find_text_part(payload)

    @staticmethod
    def _walk_mime(
        part: GmailPayloadPart,
        visitor: Callable[[GmailPayloadPart], None],
        depth: int = 0,
        counter: list[int] | None = None,
    ) -> None:
        if counter is None:
            counter = [0]
        counter[0] += 1
        if depth > _MAX_MIME_DEPTH or counter[0] > _MAX_MIME_PARTS:
            return
        visitor(part)
        parts = part.get("parts")
        if isinstance(parts, list):
            for sub in parts:
                if counter[0] > _MAX_MIME_PARTS:
                    break
                if isinstance(sub, dict):
                    GmailConnector._walk_mime(sub, visitor, depth + 1, counter)

    def _find_text_part(self, part: GmailPayloadPart) -> str:
        found: list[str] = []

        def _visit(p: GmailPayloadPart) -> None:
            if found:
                return
            raw_mime = p.get("mimeType")
            if (str(raw_mime) if isinstance(raw_mime, str) else "") != "text/plain":
                return
            body = p.get("body")
            if not isinstance(body, dict):
                return
            raw_data = body.get("data")
            data = str(raw_data) if isinstance(raw_data, str) else ""
            if not data:
                return
            padded = data + "=" * (-len(data) % 4)
            if not _B64_URLSAFE.match(padded):
                return
            with suppress(ValueError, binascii.Error):
                found.append(base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace"))

        self._walk_mime(part, _visit)
        return found[0] if found else ""

    def extract_attachments(self, message: GmailMessage) -> list[GmailAttachmentInfo]:
        attachments: list[GmailAttachmentInfo] = []
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return attachments

        def _visit(p: GmailPayloadPart) -> None:
            raw_fn = p.get("filename")
            filename = str(raw_fn) if isinstance(raw_fn, str) else ""
            body = p.get("body")
            if not isinstance(body, dict):
                return
            raw_att_id = body.get("attachmentId")
            att_id = str(raw_att_id) if isinstance(raw_att_id, str) else ""
            raw_size = body.get("size")
            size = int(raw_size) if isinstance(raw_size, int) and not isinstance(raw_size, bool) else 0
            if not (filename and att_id):
                return
            if size > _MAX_ATTACHMENT_BYTES:
                _log(_LOG_WARN, "gmail", f"Skipping oversized attachment: {filename} ({size} bytes)")
                return
            raw_mime = p.get("mimeType")
            attachments.append(
                GmailAttachmentInfo(
                    filename=_sanitize_filename(filename),
                    mimeType=str(raw_mime) if isinstance(raw_mime, str) else "",
                    size=size,
                    attachmentId=att_id,
                )
            )

        self._walk_mime(payload, _visit)
        return attachments

    def is_inbox_unread(self, label_ids: list[str]) -> bool:
        return "INBOX" in label_ids and "UNREAD" in label_ids

    def _clean_body(self, text: str) -> str:
        text = text.replace("\r\n", "\n")
        text = re.sub(r"<https?://[^>]+>", "", text)
        text = re.sub(r"\[image:[^\]]*\]", "", text)
        text = re.sub(r"Get Outlook for iOS\s*", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _strip_reply_chain(self, text: str) -> str:
        patterns = [
            r"\s+On \w{3}, \w{3,9} \d{1,2}, \d{4}[ ,].{5,80} wrote:",
            r"\s+On \d{1,2} \w{3,9} \d{4}[ ,].{5,80} wrote:",
            r"\n>[ >].*(?:\n>[ >].*)*",
            r"\n-{2,}\s*Reply above this line\s*-{2,}",
            r"\n_{2,}\nFrom:.*?\nSent:",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.DOTALL)
            if m:
                text = text[: m.start()]
        return text.strip()

    def _detect_forward_split(self, body: str) -> tuple[str | None, str | None]:
        gmail_marker = "---------- Forwarded message ---------"
        if gmail_marker in body:
            parts = body.split(gmail_marker, 1)
            return parts[0].strip(), parts[1].strip()
        outlook_match = re.search(
            r"\n_{3,}\n\s*From:.*?\nSent:.*?\nTo:.*?\nSubject:",
            body,
            re.DOTALL,
        )
        if outlook_match:
            return body[: outlook_match.start()].strip(), body[outlook_match.start() :].strip()
        generic_match = re.search(
            r"\n-{2,}\s*(?:Original Message|Forwarded)\s*-{2,}",
            body,
            re.IGNORECASE,
        )
        if generic_match:
            return body[: generic_match.start()].strip(), body[generic_match.start() :].strip()
        return None, None

    def format_email_message(self, body: str, subject: str, thread_id: str = "") -> tuple[str, str]:
        body = self._clean_body(body)
        manager_text, forwarded_content = self._detect_forward_split(body)
        thread_tag = f" [thread:{thread_id}]" if thread_id else ""
        if forwarded_content is not None:
            if manager_text:
                manager_text = self._strip_reply_chain(manager_text)
            forwarded_content = self._clean_body(forwarded_content)
            forwarded_content = _truncate(forwarded_content, 1200)
            fwd_subject = subject if re.match(r"(?i)^(fwd?|forwarded):", subject) else f"Fwd: {subject}"
            html_parts = [f"📧 <b>{_escape_html(fwd_subject)}</b>{thread_tag}"]
            plain_parts = [f"manager (via email):{thread_tag}"]
            if manager_text:
                html_parts.append(f"\n{_escape_html(manager_text)}")
                plain_parts.append(manager_text)
            html_parts.append(f"<blockquote>{_escape_html(forwarded_content)}</blockquote>")
            plain_parts.append(f"--- Forwarded: {subject} ---")
            plain_parts.append(forwarded_content)
            return "\n".join(html_parts), "\n".join(plain_parts)
        body = self._strip_reply_chain(body)
        body = _truncate(body)
        html = f"📧 <b>{_escape_html(subject)}</b>{thread_tag}\n\n{_escape_html(body)}"
        plain = f"manager (via email):{thread_tag} {body}"
        return html, plain

    def _format_sent_reply(self, body: str, subject: str, thread_id: str, message: GmailMessage) -> tuple[str, str]:
        body = self._clean_body(body)
        body = self._strip_reply_chain(body)
        body = _truncate(body)
        sender_name = self.extract_sender_name(message)
        thread_tag = f" [thread:{thread_id}]" if thread_id else ""
        html = f"✉️ <b>Sent:</b> {_escape_html(subject)}{thread_tag}\n\n{_escape_html(body)}"
        plain = f"{sender_name} (sent via email):{thread_tag} {body}"
        return html, plain

    def _get_new_message_ids(self) -> tuple[list[str] | None, str | None]:
        with self._lock:
            current_hid = self._history_id
        if not current_hid:
            return [], None
        data = self._get_history(current_hid)
        if data is None:
            return None, None
        msg_ids: list[str] = []
        seen: set[str] = set()
        for entry in data.get("history") or []:
            if not isinstance(entry, dict):
                continue
            for added in entry.get("messagesAdded") or []:
                msg = added.get("message") if isinstance(added, dict) else None
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id", "")
                label_ids = msg.get("labelIds") or []
                if (
                    msg_id
                    and isinstance(msg_id, str)
                    and msg_id not in seen
                    and (self.is_inbox_unread(label_ids) or "SENT" in label_ids)
                ):
                    seen.add(msg_id)
                    msg_ids.append(msg_id)
        raw_hid = data.get("historyId")
        return msg_ids, str(raw_hid) if raw_hid is not None else None

    def _format_attachment_line(self, attachments: list[GmailAttachmentInfo]) -> str:
        return (
            f"\n[{len(attachments)} attachment(s): {', '.join(str(a.get('filename') or '?') for a in attachments)}]"
            if attachments
            else ""
        )

    def _download_attachment(self, msg_id: str, att: GmailAttachmentInfo) -> str | None:
        att_id = str(att.get("attachmentId") or "")
        if not att_id:
            return None
        data = self._get_attachment_data(msg_id, att_id)
        if not data:
            return None
        raw_b64 = data.get("data")
        if not isinstance(raw_b64, str) or not raw_b64:
            return None
        padded_b64 = raw_b64 + "=" * (-len(raw_b64) % 4)
        if not _B64_URLSAFE.match(padded_b64):
            return None
        try:
            decoded = base64.urlsafe_b64decode(padded_b64)
        except (ValueError, binascii.Error):
            return None
        if len(decoded) > _MAX_ATTACHMENT_BYTES:
            return None
        prefix = re.sub(r"[^\w]", "", msg_id[:8]) if msg_id else ""
        filename = _sanitize_filename(
            f"{prefix}_{att.get('filename', 'attachment')}" if prefix else str(att.get("filename", "attachment"))
        )
        att_dir = os.path.join(os.path.expanduser("~"), ".cache", "beast", "email", "attachments")
        try:
            _atomic_write(os.path.join(att_dir, filename), decoded)
            return os.path.join(att_dir, filename)
        except OSError:
            return None

    def _process_message(self, msg_id: str) -> None:
        message = self.get_message(msg_id)
        if not message:
            raise RuntimeError(f"get_message({msg_id}) returned None — transient API failure")
        is_sent = self.is_sent_message(message)
        if not is_sent and not self.is_allowed_sender(message):
            return
        body = self.extract_body_text(message)
        subject = self.extract_subject(message)
        raw_tid = message.get("threadId")
        thread_id: str = str(raw_tid) if isinstance(raw_tid, str) else ""
        message_id = self.extract_message_id(message)
        attachments = self.extract_attachments(message)
        if not body.strip():
            return
        downloaded: list[Attachment] = []
        for att in attachments:
            path = self._download_attachment(msg_id, att)
            if path:
                downloaded.append(Attachment(path=path, filename=att["filename"], mimeType=att["mimeType"]))
        if is_sent:
            html_text, plain_text = self._format_sent_reply(body, subject, thread_id, message)
            self.on_message([], html_text, plain_text, downloaded)
            return
        body = self._strip_reply_chain(body)
        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_email_message(cleaned, subject, thread_id)
        else:
            html_text, plain_text = self.format_email_message(body, subject, thread_id)
        att_line = self._format_attachment_line(attachments)
        html_text += att_line
        plain_text += att_line
        if targets and message_id:
            safe_subject = subject.replace("'", "'\\''")
            safe_thread = thread_id.replace("'", "'\\''")
            safe_msgid = message_id.replace("'", "'\\''")
            reply_hint = f"\n\nReply (prefer HTML): beast email send -s 'Re: {safe_subject}' --thread-id '{safe_thread}' --in-reply-to '{safe_msgid}' --html-file /tmp/reply.html"
            reply_hint += f"\nReply (plain text): beast email send -s 'Re: {safe_subject}' --thread-id '{safe_thread}' --in-reply-to '{safe_msgid}' --body 'your reply'"
            plain_text += reply_hint
        self.on_message(targets, html_text, plain_text, downloaded)
        if not self.mark_as_read(msg_id):
            _log(_LOG_WARN, "gmail", f"Failed to mark message {msg_id} as read")

    def poll_once(self) -> None:
        msg_ids, new_hid = self._get_new_message_ids()
        if msg_ids is None:
            self.track_failure()
            with self._lock:
                fail_count = self._consecutive_failures
            if fail_count >= CONSECUTIVE_FAIL_REBOOTSTRAP:
                _log(_LOG_WARN, "gmail", f"{fail_count} failures — attempting re-bootstrap")
                new_id = self._bootstrap_history_id(skip_disk=True)
                if new_id:
                    with self._lock:
                        self._history_id = new_id
                    self.track_success()
                    _log(_LOG_INFO, "gmail", f"Re-bootstrap OK (historyId={new_id})")
                else:
                    self._send_alert(
                        "Gmail re-bootstrap failed — token expired, needs manual renewal (ask geni to run gws renewal)"
                    )
            return
        self.track_success()
        had_errors: bool = False
        last_error: str = ""
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                _log(_LOG_ERROR, "gmail", f"Error processing message {msg_id}: {e}")
                had_errors = True
                last_error = str(e)
                self.track_handler_error(str(e))
                health.error("gmail")
        if not had_errors:
            self.clear_handler_errors()
            health.ok("gmail")
        if new_hid and not had_errors:
            with self._lock:
                self._history_id = new_hid
            self._save_history_id()
        elif had_errors:
            _log(_LOG_WARN, "gmail", "historyId NOT advanced — some messages failed, will retry next poll")

    def _on_preflight_ok(self) -> None:
        hid = self._bootstrap_history_id()
        with self._lock:
            self._history_id = hid
        if not hid:
            _log(_LOG_WARN, "gmail", "Bootstrap failed, retrying in 60s...")
            if self._stop_event.wait(60):
                return
            hid = self._bootstrap_history_id()
            with self._lock:
                self._history_id = hid
            if not hid:
                self._send_alert("Gmail connector disabled — cannot get historyId")
                self._stop_event.set()
                return
        _log(
            _LOG_INFO, "gmail", f"Started (interval={self.poll_interval}s, from={self.sender_filter}, historyId={hid})"
        )
        self._catchup_unread()

    def _catchup_unread(self) -> None:
        data = self._list_messages(f"from:{self.sender_filter} is:unread in:inbox newer_than:1d", max_results=5)
        messages_list = (data or {}).get("messages")
        if not isinstance(messages_list, list):
            return
        msg_ids = [m["id"] for m in messages_list if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if not msg_ids:
            return
        _log(_LOG_INFO, "gmail", f"Catch-up: {len(msg_ids)} unread from {self.sender_filter}")
        for msg_id in msg_ids:
            try:
                self._process_message(msg_id)
            except Exception as e:
                _log(_LOG_WARN, "gmail", f"Catch-up error {msg_id}: {e}")

    def stop(self) -> None:
        super().stop()  # Wait for poll thread to exit before saving
        self._save_history_id()


class GitHubConnector(BaseConnector[GithubComment]):
    connector_name: str = "github"

    def __init__(
        self,
        repo: str | list[str],
        from_user: str,
        poll_interval: int,
        on_message: MessageCallback,
        get_registered_workers: WorkerListCallback,
        on_alert: AlertCallback | None = None,
        state_file: str = "",
        gh_bin: str = "",
    ) -> None:
        super().__init__(
            sender_filter=from_user,
            poll_interval=poll_interval,
            on_message=on_message,
            get_registered_workers=get_registered_workers,
            on_alert=on_alert,
        )
        self.gh_bin: str = gh_bin or "gh"
        repos = [repo] if isinstance(repo, str) else list(repo)
        if not repos:
            raise ValueError("github: at least one repo required")
        for r in repos:
            if not _REPO_PATTERN.match(r):
                raise ValueError(f"github: repo must be 'owner/name' format, got: {r!r}")
        self.repos: list[str] = repos
        self._state_file: str = state_file
        self._last_poll_time: str | None = None
        self._seen_ids: set[int] = set()

    def preflight_check(self) -> tuple[bool, str]:
        for repo in self.repos:
            try:
                result = subprocess.run(
                    [self.gh_bin, "api", f"/repos/{repo}", "--jq", ".id"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    return False, f"gh api failed for {repo}: {result.stderr.strip()}"
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                return False, f"gh preflight error for {repo}: {e}"
        return True, "OK"

    def status(self) -> GithubStatus:
        with self._lock:
            s = GithubStatus(
                name=self.connector_name,
                running=self.running,
                sender_filter=self.sender_filter,
                poll_interval=self.poll_interval,
                consecutive_failures=self._consecutive_failures,
                alert_sent=self._alert_sent,
                repos=self.repos,
                last_poll_time=self._last_poll_time,
                seen_ids_count=len(self._seen_ids),
            )
            s["handler_errors"] = self._handler_errors  # type: ignore[typeddict-unknown-key]
            return s

    def _load_state(self) -> bool:
        if not self._state_file:
            return False
        try:
            with open(self._state_file) as f:
                data: object = json.load(f)
            if not isinstance(data, dict):
                return False
            with self._lock:
                lpt = data.get("last_poll_time")
                if isinstance(lpt, str) and lpt:
                    self._last_poll_time = lpt
                seen_raw = data.get("seen_ids")
                if isinstance(seen_raw, list):
                    for sid in seen_raw:
                        if isinstance(sid, int) and not isinstance(sid, bool):
                            self._seen_ids.add(sid)
                return bool(self._last_poll_time)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return False

    def _save_state(self) -> None:
        if not self._state_file:
            return
        with self._lock:
            state = GithubState(
                last_poll_time=self._last_poll_time,
                seen_ids=sorted(self._seen_ids)[-_MAX_SEEN_IDS:],
            )
        try:
            _atomic_write(self._state_file, json.dumps(state))
        except OSError as e:
            _log(_LOG_WARN, "github", f"Failed to save state: {e}")

    def _on_preflight_ok(self) -> None:
        restored = self._load_state()
        if restored:
            with self._lock:
                seen_count = len(self._seen_ids)
                poll_time = self._last_poll_time
            _log(_LOG_INFO, "github", f"Restart — restored state: {seen_count} seen IDs, since={poll_time}")
        else:
            initial_time = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with self._lock:
                self._last_poll_time = initial_time
            seed, _ = self._get_all_comments(initial_time)
            if seed:
                with self._lock:
                    for c in seed:
                        cid = c.get("id")
                        if isinstance(cid, int) and not isinstance(cid, bool):
                            self._seen_ids.add(cid)
                _log(_LOG_INFO, "github", f"First run — seeded {len(seed)} existing comments as seen")
            else:
                _log(_LOG_INFO, "github", f"First run — no comments to seed")
            self._save_state()
        with self._lock:
            poll_time = self._last_poll_time
        repos_str = ", ".join(self.repos)
        _log(
            _LOG_INFO,
            "github",
            f"Started (interval={self.poll_interval}s, repos={repos_str}, user={self.sender_filter}, since={poll_time})",
        )

    def _gh_api(self, endpoint: str, timeout: int = 30) -> str | None:
        try:
            result = subprocess.run(
                [self.gh_bin, "api", endpoint, "--paginate"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                _log(_LOG_WARN, "github", f"gh api error: {result.stderr.strip()}")
                return None
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            _log(_LOG_WARN, "github", f"gh api timed out: {endpoint}")
            return None
        except OSError as e:
            _log(_LOG_WARN, "github", f"gh api exception: {e}")
            return None

    def _get_all_comments(self, since: str) -> tuple[list[GithubComment] | None, bool]:
        all_raws: list[str | None] = []
        any_success = False
        is_complete = True
        for repo in self.repos:
            issue_raw = self._gh_api(
                f"/repos/{repo}/issues/comments?since={since}&sort=updated&direction=asc&per_page=100",
            )
            pr_raw = self._gh_api(
                f"/repos/{repo}/pulls/comments?since={since}&sort=updated&direction=asc&per_page=100",
            )
            if issue_raw is not None or pr_raw is not None:
                any_success = True
            if issue_raw is None or pr_raw is None:
                is_complete = False
                failed = "issues" if issue_raw is None else "pulls"
                _log(_LOG_WARN, "github", f"Partial fetch for {repo} — {failed} comments failed")
            all_raws.extend([issue_raw, pr_raw])
        if not any_success:
            return None, False
        seen: set[int] = set()
        merged: list[GithubComment] = []
        for raw in all_raws:
            if not raw:
                continue
            comments = _safe_json_loads_list(raw)
            for c in comments:
                if not isinstance(c, dict):
                    continue
                cid = c.get("id", 0)
                if isinstance(cid, int) and not isinstance(cid, bool) and cid not in seen:
                    seen.add(cid)
                    merged.append(c)
        return merged, is_complete

    def extract_sender(self, comment: GithubComment) -> str:
        user = comment.get("user")
        if not isinstance(user, dict):
            return ""
        login = user.get("login")
        return str(login).lower() if isinstance(login, str) and login else ""

    def extract_issue_context(self, comment: GithubComment) -> IssueContext:
        if comment.get("issue_num") and comment.get("kind"):
            return IssueContext(
                number=str(comment["issue_num"]), kind=str(comment["kind"]), url=str(comment.get("html_url", ""))
            )

        def _str(key: str) -> str:
            v = comment.get(key)
            return str(v) if isinstance(v, str) else ""

        issue_url = _str("issue_url") or _str("pull_request_url")
        html_url = _str("html_url")
        number = ""
        for url, kind_val, pattern in [
            (issue_url, "issue", r"/issues/(\d+)"),
            (issue_url, "PR", r"/pulls/(\d+)"),
            (html_url, "PR", r"/pull/(\d+)"),
        ]:
            m = re.search(pattern, url)
            if m:
                return IssueContext(number=m.group(1), kind=kind_val, url=html_url)
        return IssueContext(number="", kind="comment", url=html_url)

    def format_comment(self, body: str, context: IssueContext) -> tuple[str, str]:
        body = _truncate(body)
        kind = context["kind"]
        number = context["number"]
        url = context["url"]
        tag = f"#{number}" if number else ""
        link = f'<a href="{_escape_html(url)}">#{number}</a>' if url and number else tag
        html = f"🔔 <b>GitHub {kind} {link}</b>\n\n{markdown_to_telegram_html(body)}"
        plain = f"manager (via GitHub {kind} {tag}): {body}"
        return html, plain

    @staticmethod
    def _extract_repo_from_comment(comment: GithubComment) -> str:
        for key in ("issue_url", "pull_request_url", "html_url"):
            url = comment.get(key)
            if not isinstance(url, str):
                continue
            m = re.search(r"/repos/([^/]+/[^/]+)/", url)
            if m:
                return m.group(1)
            m = re.search(r"github\.com/([^/]+/[^/]+)/", url)
            if m:
                return m.group(1)
        return ""

    def _process_comment(self, comment: GithubComment) -> None:
        cid = comment.get("id", 0)
        if not isinstance(cid, int) or isinstance(cid, bool) or cid == 0:
            return
        with self._lock:
            if cid in self._seen_ids:
                return
        if not self.is_allowed_sender(comment):
            with self._lock:
                self._seen_ids.add(cid)  # Non-target sender: mark seen, no retry needed
            return
        raw_body = comment.get("body")
        body: str = str(raw_body).strip() if isinstance(raw_body, str) else ""
        if not body:
            with self._lock:
                self._seen_ids.add(cid)
            return
        context = self.extract_issue_context(comment)
        targets, cleaned = self.parse_mentions(body)
        if targets:
            html_text, plain_text = self.format_comment(cleaned, context)
        else:
            html_text, plain_text = self.format_comment(body, context)
        comment_repo = self._extract_repo_from_comment(comment) or (self.repos[0] if self.repos else "")
        if targets and context["number"]:
            num = context["number"]
            safe_repo = comment_repo.replace("'", "'\\''")
            reply_hint = f"\n\nView: beast github show {num} --repo '{safe_repo}'"
            reply_hint += f"\nReply: beast github comment {num} --repo '{safe_repo}' --body 'your reply'"
            plain_text += reply_hint
        metadata = ConnectorMetadata(number=context["number"], repo=comment_repo, comment_id=cid)
        self.on_message(targets, html_text, plain_text, [], metadata=metadata)
        with self._lock:
            self._seen_ids.add(cid)
        _log(_LOG_INFO, "github", f"{context['kind']} #{context['number']}: -> {targets or 'Telegram only'}")

    def poll_once(self) -> None:
        with self._lock:
            poll_time = self._last_poll_time
        if not poll_time:
            return
        all_comments, fetch_complete = self._get_all_comments(poll_time)
        if all_comments is None:
            self.track_failure()
            return
        self.track_success()
        had_errors: bool = False
        for comment in all_comments:
            try:
                self._process_comment(comment)
            except Exception as e:
                _log(_LOG_ERROR, "github", f"Error processing comment {comment.get('id')}: {e}")
                had_errors = True
                self.track_handler_error(str(e))
                health.error("github")
        if not had_errors:
            self.clear_handler_errors()
            health.ok("github")
        if not had_errors and fetch_complete:
            with self._lock:
                self._last_poll_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            _log(
                _LOG_WARN,
                "github",
                f"poll_time NOT advanced — {', '.join(r for r in ['partial fetch' if not fetch_complete else '', 'processing errors' if had_errors else ''] if r)}, will retry",
            )
        self._prune_seen_ids()
        self._save_state()

    def _prune_seen_ids(self) -> None:
        with self._lock:
            if len(self._seen_ids) > _MAX_SEEN_IDS:
                self._seen_ids = set(sorted(self._seen_ids)[-_PRUNE_KEEP:])

    def stop(self) -> None:
        super().stop()  # Wait for poll thread to exit before saving
        self._save_state()


def connector_render_html(tag: str, current_html: str) -> str:
    esc = html.escape
    msgs = connectors.get_log(tag)
    icon = "🔔" if tag == "github" else "📧"
    title = f"{tag.title()} Feed"
    blocks = []
    av_letter = tag[0].upper()
    av_color = "#8b5cf6" if tag == "github" else "#f59e0b"
    av_svg = f'<div class="u-av"><svg viewBox="0 0 28 28"><rect width="28" height="28" rx="14" fill="{av_color}"/><text x="14" y="18" text-anchor="middle" fill="#fff" font-size="12" font-weight="600">{av_letter}</text></svg></div>'
    tag_title = esc(tag.title())
    for i, m in enumerate(msgs):
        ts = time.strftime("%b %d, %H:%M", time.gmtime(m["ts"]))
        who = ", ".join(m["targets"]) if m["targets"] else "all"
        content = re.sub(r"(https?://\S+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', m["html"]).replace(
            "\n", "<br>"
        )
        is_latest = i == len(msgs) - 1
        cls = "chat-msg latest" if is_latest else "chat-msg"
        badge = '<span class="badge">Latest</span>' if is_latest else ""
        blocks.append(
            f'<div class="{cls}">{av_svg}<div class="chat-body"><span class="u-name">{tag_title}</span><span class="ts">{ts}</span><span class="target">→ {esc(who)}</span>{badge}<div class="chat-text">{content}</div></div></div>'
        )
    blocks_html = "\n".join(blocks)
    updated = time.strftime("%b %d, %H:%M UTC", time.gmtime(core._clock.time()))
    count_text = f'{len(msgs)} recent message{"s" if len(msgs) != 1 else ""}'
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{icon} {title}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">'
        "<style>" + _CONNECTOR_CSS + f'</style></head><body><div class="wrap"><header><h1>{icon} {title}</h1>'
        f'<div class="meta">{count_text} &middot; Updated {updated}</div></header>'
        f'<div class="thread">{blocks_html}</div></div></body></html>'
    )


def connector_short_summary(
    tag: str, plain_text: str, serve_url: str | None = None, metadata: ConnectorMetadataDict | None = None
) -> str:

    icon = "🔔" if tag == "github" else "📧"
    body = plain_text.strip()
    body = re.sub(r"^manager\s*\(via\s+\w+[^)]*\):\s*", "", body)
    body = re.sub(r"\[thread:[^\]]+\]\s*", "", body)
    body = " ".join(body.split())
    if len(body) > 200:
        body = body[:197] + "…"
    ref_match = re.search(r"#(\d+)", plain_text)
    thread_match = re.search(r"\[thread:([^\]]+)\]", plain_text)
    header = f"{icon} <b>{tag.title()}</b>"
    if ref_match:
        num = ref_match.group(1)
        repo = (metadata or {}).get("repo", "BasedHardware/omi")
        gh_url = f"https://github.com/{repo}/issues/{num}"
        header += f' <a href="{gh_url}">#{num}</a>'
    elif thread_match:
        header += f" {html.escape('thread:' + thread_match.group(1))}"
    parts = [header, html.escape(body)]
    if serve_url:
        parts.append(f'<a href="{html.escape(serve_url)}">View full →</a>')
    return "\n".join(parts)


def connector_export_github(number: int, repo: str) -> str | None:

    try:
        r = core._subprocess_runner.run(
            ["beast", "github", "export", str(number), "--format", "print", "--serve", "--repo", repo, "--fresh"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_GIT_OP,
        )
        if r.returncode == 0:
            for line in r.stderr.splitlines() + r.stdout.splitlines():
                if "http" in line and ("localhost" in line or "serve" in line.lower()):
                    url = line.strip().split()[-1].rstrip("/")
                    if "localhost" in url:
                        host = (urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else None) or "157.180.48.254"
                        url = url.replace("localhost", host)
                    return url
    except subprocess.SubprocessError as e:
        _log(_LOG_WARN, "github", f"export failed for #{number}: {e}")
    return None


def connector_on_message(tag: str) -> Callable:
    def handler(
        targets: list[str],
        html_text: str,
        plain_text: str | None = None,
        attachments: list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        if plain_text is None:
            plain_text = html_text
        connectors.log_message(tag, html_text, plain_text, targets)
        if core.admin_chat_id:
            serve_url = None
            if tag == "github" and metadata and metadata.get("number"):
                with _log_suppress(KeyError, tag=tag, msg="github export failed: "):
                    serve_url = connector_export_github(metadata["number"], metadata.get("repo", "BasedHardware/omi"))
            if not serve_url:
                with _log_suppress(OSError, tag=tag, msg="beast serve failed: "):
                    page_html = connector_render_html(tag, html_text)
                    tmp_path = f"/tmp/connector-{tag}.html"
                    with open(tmp_path, "w") as f:
                        f.write(page_html)
                    serve_url = _beast_serve_deploy(tmp_path, f"connector-{tag}")
            summary = connector_short_summary(tag, plain_text, serve_url, metadata)
            try:
                send_telegram_message(core.admin_chat_id, summary, parse_mode="HTML")
            except OSError:
                with _log_suppress(urllib.error.URLError, OSError, TimeoutError, tag=tag, msg="Telegram send failed: "):
                    send_telegram_message(core.admin_chat_id, plain_text[:300])
            for att in attachments or []:
                fpath = att.get("path", "")
                fname = att.get("filename", "")
                if not fpath or not os.path.isfile(fpath):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                caption = f"📧 {fname}"
                if ext in ALLOWED_IMAGE_EXTENSIONS:
                    send_photo(core.admin_chat_id, fpath, caption)
                elif ext in VIDEO_EXTENSIONS:
                    send_video(core.admin_chat_id, fpath, caption)
                else:
                    send_document(core.admin_chat_id, fpath, caption)
                _log(_LOG_INFO, tag, f"attachment -> Telegram: {fname}")
        if targets:
            for name in targets:
                send_to_worker(name, plain_text)
                _log(_LOG_INFO, tag, f"-> {name}: {plain_text[:80]}...")
        else:
            _log(_LOG_INFO, tag, f"-> Telegram only (no mentions): {plain_text[:80]}...")

    return handler


def connector_get_workers() -> set[str]:
    return set(get_registered_sessions().keys())


def connector_on_alert(tag: str) -> Callable:
    def handler(text: str) -> None:
        if core.admin_chat_id:
            with _log_suppress(
                urllib.error.URLError, OSError, TimeoutError, tag=tag, msg="Failed to send Telegram alert: "
            ):
                send_telegram_message(core.admin_chat_id, text)

    return handler


def new_gmail() -> GmailConnector:
    return GmailConnector(
        gws_bin=GMAIL_GWS_BIN,
        from_filter=GMAIL_FROM_FILTER,
        poll_interval=GMAIL_POLL_INTERVAL,
        on_message=connector_on_message("gmail"),
        get_registered_workers=connector_get_workers,
        on_alert=connector_on_alert("gmail"),
    )


def new_github() -> GitHubConnector:
    return GitHubConnector(
        repo=GITHUB_REPOS,
        from_user=GITHUB_FROM_USER,
        poll_interval=GITHUB_POLL_INTERVAL,
        on_message=connector_on_message("github"),
        get_registered_workers=connector_get_workers,
        on_alert=connector_on_alert("github"),
        state_file=str(NODE_DIR / "github_state.json"),
    )


def start_connectors() -> tuple[object, object]:
    gmail_inst = None
    if GMAIL_ENABLED and GmailConnector is not None:
        gmail_inst = new_gmail()
        gmail_inst.start()
        if gmail_inst._thread:
            health.register_thread("gmail-poller", gmail_inst._thread)
        _log(_LOG_INFO, "gmail", f"Connector started: polling every {GMAIL_POLL_INTERVAL}s for {GMAIL_FROM_FILTER}")
    elif GMAIL_ENABLED and GmailConnector is None:
        _log(_LOG_ERROR, "bridge", f"Gmail connector disabled: {GMAIL_IMPORT_ERROR}")
    github_inst = None
    if GITHUB_ENABLED and GitHubConnector is not None:
        github_inst = new_github()
        github_inst.start()
        if github_inst._thread:
            health.register_thread("github-poller", github_inst._thread)
        _log(
            _LOG_INFO,
            "github",
            f"Connector started: polling every {GITHUB_POLL_INTERVAL}s for {GITHUB_FROM_USER} on {', '.join(GITHUB_REPOS)}",
        )
    elif GITHUB_ENABLED and GitHubConnector is None:
        _log(_LOG_ERROR, "bridge", f"GitHub connector disabled: {GITHUB_IMPORT_ERROR}")
    return gmail_inst, github_inst


def restart_connector(name: str) -> tuple[bool, str]:
    _cfg = {
        "gmail": (GMAIL_ENABLED, GmailConnector, GMAIL_IMPORT_ERROR, "GMAIL_ENABLED=0", new_gmail),
        "github": (GITHUB_ENABLED, GitHubConnector, GITHUB_IMPORT_ERROR, "BRIDGE_GHPOLL_ENABLED=0", new_github),
    }
    entry = _cfg.get(name)
    if not entry:
        return False, f"Unknown connector: {name} (valid: gmail, github)"
    enabled, cls, import_err, env_name, factory = entry
    if not enabled:
        return False, f"{name.title()} connector not enabled ({env_name})"
    if cls is None:
        return False, f"{name.title()} connector import failed: {import_err}"
    old = getattr(connectors, name, None)
    if old is not None:
        old.stop()
    inst = factory()
    setattr(connectors, name, inst)
    ok, msg = inst.restart()
    _log(_LOG_INFO, name, f"Connector {'restarted' if ok else 'restart failed'}: {msg}")
    return ok, msg


def get_connectors_status() -> dict[str, ConnectorStatusDict]:
    result: dict[str, ConnectorStatusDict] = {}
    for cname, enabled in [("gmail", GMAIL_ENABLED), ("github", GITHUB_ENABLED)]:
        if not enabled:
            result[cname] = {"name": cname, "running": False, "enabled": False}
        else:
            inst = getattr(connectors, cname, None)
            result[cname] = (
                cast("ConnectorStatusDict", inst.status())
                if inst
                else {"name": cname, "running": False, "error": "not initialized"}
            )
    return result


def _read_noninteractive_activity(worker_name: str) -> str:
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
                r = _remote_run(["stat", "-c", "%Y", path], host=host, timeout=TIMEOUT_TMUX_SEND)
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


def _read_host_file(path: str, host: str | None) -> str | None:
    """Read file content from local or remote host. Returns None if missing/unreadable."""
    if host:
        try:
            r = _remote_run(["cat", path], host=host, timeout=TIMEOUT_TMUX_SEND)
            return r.stdout if r.returncode == 0 else None
        except (subprocess.SubprocessError, OSError):
            return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_text()
    except OSError:
        return None


def _check_hook_failure_signal(name: str) -> str | None:
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"
    raw = (_read_host_file(signal_path, get_worker_host(name)) or "").strip()
    if not raw:
        return None
    cutoff = int(_clock.time()) - HOOK_FAILURE_WINDOW
    recent = 0
    for line in raw.splitlines():
        parts = line.split(None, 1)
        if parts:
            with suppress(ValueError):
                if int(parts[0]) >= cutoff:
                    recent += 1
    if recent >= HOOK_FAILURE_THRESHOLD:
        return f"hook failure signal: {recent} tool failures in {HOOK_FAILURE_WINDOW}s"
    return None


def _clear_hook_failures(name: str) -> None:
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"
    host = get_worker_host(name)
    if host:
        with suppress(subprocess.SubprocessError, OSError):
            _remote_run(["rm", "-f", signal_path], host=host, timeout=TIMEOUT_TMUX_SEND)
    else:
        with suppress(OSError):
            Path(signal_path).unlink(missing_ok=True)


def _detect_poisoned(name: str, tmux_name: str) -> str | None:
    hook_reason = _check_hook_failure_signal(name)
    if hook_reason:
        return hook_reason
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


_REVERSE_SCAN_PATTERNS: list[tuple[str, str]] = [
    ("Save and close editor to continue", "Waiting for external editor"),
    ("Running SessionStart", "Running SessionStart hooks"),
    ("Running PreCompact", "Running PreCompact hooks"),
    ("Do you want to proceed?", "Waiting for plan approval"),
    ("Would you like to proceed?", "Waiting for plan approval"),
    ("Exit plan mode?", "In plan mode"),
    ("Entering plan mode", "In plan mode"),
    ("Waiting for team lead", "Waiting for team lead approval"),
]
_ACTIVE_SPINNER_CHARS = {"·", "*", "✢", "✦", "✧", "✹", "✵", "∙", "•", "✻"}


def _extract_activity(lines: list[str]) -> str:
    if not lines:
        return "Active"
    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped:
        return "Idle"
    # Spinner check
    for raw in reversed(stripped):
        first = raw[0] if raw else ""
        if first == "✻" and "…" not in raw and "..." not in raw:
            continue
        if first in _ACTIVE_SPINNER_CHARS:
            match = re.match(r"^.\s+(.+?)(?:…|\.{3})\s*\(([^()]+)\)\s*$", raw)
            if match:
                return f"{match.group(1).strip()} ({match.group(2).split('·')[0].strip()})"
            verb_match = re.match(r"^.\s+(.+?)(?:…|\.{3})?\s*$", raw)
            if verb_match:
                verb = verb_match.group(1).strip()
                dur = re.search(r"(\d+m?\s*\d*\.?\d*s)", raw)
                return f"{verb} ({dur.group(1).strip()})" if dur else verb
    # Running tool check
    last_running_tool = None
    for i, raw in enumerate(stripped):
        tool_match = re.match(r"^●\s*([A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*)\(", raw)
        if not tool_match:
            continue
        for j in range(i + 1, min(i + 6, len(stripped))):
            if not stripped[j]:
                continue
            if stripped[j].startswith("⎿"):
                if "Running" in stripped[j] and "background" not in stripped[j]:
                    last_running_tool = tool_match.group(1)
                break
    if last_running_tool:
        if last_running_tool.startswith("mcp__"):
            parts = last_running_tool.split("__")
            last_running_tool = ".".join(parts[1:]) if len(parts) > 1 else last_running_tool
        return f"Running {last_running_tool}"
    # Rate limit / connection error
    for raw in reversed(stripped):
        lower = raw.lower()
        if "rate limit" in lower:
            return "Rate limited — waiting to retry"
        if "connection error" in lower and "retrying" in lower:
            return "Connection error — retrying"
        if lower.startswith("retrying") or "retrying in" in lower:
            return "Retrying API request"
    # Interactive input waiting
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                for q in stripped:
                    if "☐" in q:
                        question = q.replace("☐", "").strip()
                        if question:
                            return f"Waiting for input: {question}"
                return "Waiting for user input"
    for raw in stripped:
        for pattern in _INTERACTIVE_CONTENT:
            if pattern in raw:
                lower = raw.lower()
                if "plan" in lower and ("proceed" in lower or "execute" in lower):
                    return "Waiting for plan approval"
                if "plan mode" in lower:
                    return "Waiting for plan mode decision"
                if raw.startswith("Allow "):
                    return "Waiting for tool permission"
                return "Waiting for user input"
    # Prompt / plan mode
    last_prompt_idx = last_plan_idx = None
    for i, raw in enumerate(stripped):
        if raw.startswith("❯"):
            last_prompt_idx = i
        if raw.startswith("⏸"):
            last_plan_idx = i
    if last_plan_idx is not None and (last_prompt_idx is None or last_prompt_idx < last_plan_idx):
        return "In plan mode"
    if last_prompt_idx is not None:
        return "Ready"
    # Reverse scan patterns
    for raw in reversed(stripped):
        for needle, result in _REVERSE_SCAN_PATTERNS:
            if needle in raw:
                return result
    # Task progress
    done = total = 0
    for raw in stripped:
        line = raw.lstrip()
        if line.startswith(("✔", "✅")):
            done += 1
            total += 1
        elif line.startswith("◻"):
            total += 1
    if total >= 2:
        return f"Tasks ({done}/{total} done)"
    # Output block
    for i in range(len(stripped) - 1, -1, -1):
        raw = stripped[i]
        if not raw.startswith("●") or re.match(r"^●\s*[A-Za-z]\w*(?:__\w+)*\(", raw):
            continue
        parts: list[str] = []
        head = re.sub(r"^●\s*", "", raw).strip()
        if head and not head.startswith(("⎿", "(ctrl+")):
            parts.append(head)
        for j in range(i + 1, len(stripped)):
            nxt = stripped[j].lstrip()
            if nxt.startswith(("Context left until auto-compact:", "●", "·", "*", "✻", "─", "❯", "⏵", "⏸")):
                break
            text = stripped[j].strip()
            if text and not text.startswith(("⎿", "(ctrl+")):
                parts.append(text)
        if parts:
            msg = re.sub(r"\s+", " ", " ".join(parts)).strip()
            return msg[:117].rstrip() + "..." if len(msg) > 120 else msg
    # Error
    for raw in reversed(stripped):
        if re.match(r"^(FAIL|ERROR|Error|Traceback|Fail)\b", raw, re.IGNORECASE):
            if raw.lower().startswith("error"):
                tail = raw[len("Error") :].lstrip(": ").strip()
                return f"Error: {tail}" if tail else "Error"
            return f"Error: {raw[:60]}"
    return "Active"


def _extract_context_pct(lines: list[str]) -> str | None:
    for line in reversed(lines):
        m = re.search(r"Context left.*?(\d+)%", line)
        if m:
            return f"{m.group(1)}%"
    return None


def _read_tmux_activity(tmux_name: str, host: str | None = None) -> TmuxActivityResult:
    try:
        if host:
            proc = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host,
                timeout=TIMEOUT_TMUX_SEND,
            )
        else:
            proc = _subprocess_runner.run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_TMUX_CHECK,
            )
        if proc.returncode != 0:
            return TmuxActivityResult("Unknown", None, None)
        lines = proc.stdout.split("\n")
        tail = lines[-40:]
        return TmuxActivityResult(_extract_activity(tail), _extract_context_pct(tail), tail)
    except (subprocess.SubprocessError, OSError):
        return TmuxActivityResult("Unknown", None, None)


def _extract_question_details(lines: list[str]) -> QuestionDetails | None:
    stripped = [l.strip() for l in lines if l.strip()]
    if not stripped or any(line == "❯" for line in stripped[-5:]):
        return None
    has_interactive = any(f in raw for raw in reversed(stripped) for f in _INTERACTIVE_FOOTERS) or any(
        p in raw for raw in stripped for p in _INTERACTIVE_CONTENT
    )
    if not has_interactive:
        return None
    header = next((raw.replace("☐", "").strip() for raw in stripped if "☐" in raw), "")
    options = []
    selected_num = 0
    opt_re = re.compile(r"^(❯)?\s*(\d+)\.\s+(.+)")
    for raw in stripped:
        m = opt_re.match(raw)
        if m:
            num = int(m.group(2))
            is_selected = m.group(1) == "❯"
            options.append({"num": num, "label": m.group(3).strip(), "selected": is_selected})
            if is_selected:
                selected_num = num
    if not options:
        return None
    return cast(QuestionDetails, {"header": header, "options": options, "selected_num": selected_num})


def _send_interactive_reply(tmux_name: str, reply: str, details: QuestionDetails, host: str | None = None) -> bool:
    reply = reply.strip().lower()
    if reply in ("skip", "cancel", "esc"):
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)
        return True
    if reply.isdigit():
        target_num = int(reply)
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


def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pane_pid), "-f", "claude"],
            host=host,
            timeout=TIMEOUT_TMUX_SEND,
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
    if not pid:
        return 0
    try:
        result = _remote_run(["pgrep", "-P", str(pid)], host=host, timeout=TIMEOUT_TMUX_SEND)
    except (subprocess.SubprocessError, OSError):
        return 0
    if result.returncode != 0:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _ps_stats(pids: list[str], host: str | None = None) -> dict[str, ProcStatsEntry]:
    pid_list = [str(pid) for pid in pids if pid]
    if not pid_list:
        return {}
    try:
        result = _remote_run(
            ["ps", "-o", "pid=,%cpu=,state=", "-p", ",".join(pid_list)],
            host=host,
            timeout=TIMEOUT_TMUX_SEND,
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
        stats[pid] = cast(ProcStatsEntry, {"cpu": cpu, "state": state})
    return stats


_METRICS_SCRIPT = str(Path(__file__).parent / "scripts" / "host_metrics.sh")


def _run_metric(metric: str, host: str | None = None) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """Run host_metrics.sh for a single metric, return parsed JSON or None."""
    try:
        r = _remote_run(
            ["bash", _METRICS_SCRIPT, metric],
            host=host,
            timeout=TIMEOUT_REMOTE_CMD,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        result = json.loads(r.stdout)
        return None if result is None else result
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError):
        return None


def _probe_metric(
    remote_hosts: set[str],
    *,
    metric: str,
    usage_store: dict[str, Any],
    alerted_store: dict[str, Any],  # type: ignore[explicit-any]
    alert_ts_store: dict[str, float],
    cooldown: int | float,
    is_critical_fn: Callable[[dict[str, Any]], bool],  # type: ignore[explicit-any]
    alert_fmt: Callable[[str, dict[str, Any]], str],  # type: ignore[explicit-any]
    recovery_fmt: Callable[[str, dict[str, Any]], str],
    category: str,
) -> None:  # type: ignore[explicit-any]
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host):
            continue
        host_label = host or "VPS"
        usage = _run_metric(metric, host)
        if usage is None:
            continue
        is_crit = is_critical_fn(usage)
        alert_text = None
        with watchdog.lock:
            usage_store[host_label] = {**usage, "ts": now}
            was_alerted = alerted_store.get(host_label, False)
            if is_crit and not was_alerted:
                if now - alert_ts_store.get(host_label, 0) >= cooldown:
                    alert_text = alert_fmt(host_label, usage)
                    alert_ts_store[host_label] = now
                    alerted_store[host_label] = True
            elif not is_crit and was_alerted:
                alerted_store[host_label] = False
                alert_text = recovery_fmt(host_label, usage)
        _watchdog_alert(category, alert_text)


def _probe_disk_all_hosts(remote_hosts: set[str]) -> None:
    _probe_metric(
        remote_hosts,
        metric="disk",
        usage_store=host_health.disk_usage,
        alerted_store=host_health.disk_alerted,
        alert_ts_store=host_health.disk_alert_ts,
        cooldown=_res_cfg.disk_cooldown,
        is_critical_fn=lambda u: u["pct"] >= _res_cfg.disk_alert_pct or u["free_gb"] < _res_cfg.disk_alert_gb,
        alert_fmt=lambda h, u: f"🔴 Disk space critical: {h}\nUsage: {u['pct']}% ({u['free_gb']:.1f}GB free of {u['total_gb']:.0f}GB)\nAction needed: clean up old files, worktrees, or logs",
        recovery_fmt=lambda h, u: f"✅ Disk space recovered: {h} — {u['pct']}% ({u['free_gb']:.1f}GB free)",
        category="Disk",
    )


def _probe_mem_all_hosts(remote_hosts: set[str]) -> None:
    def _fmt(h: str, u: dict[str, Any]) -> str:  # type: ignore[explicit-any]
        text = f"🧠 Memory critical: {h}\nUsage: {u['pct']}% ({u['avail_gb']:.1f}GB available of {u['total_gb']:.0f}GB)"
        for p in u.get("top_procs", [])[:3]:
            text += f"\n  {p['pid']} {p['rss_gb']}GB {p['cmd']}"
        return text

    _probe_metric(
        remote_hosts,
        metric="mem",
        usage_store=host_health.mem_usage,
        alerted_store=host_health.mem_alerted,
        alert_ts_store=host_health.mem_alert_ts,
        cooldown=_res_cfg.mem_cooldown,
        is_critical_fn=lambda u: u["pct"] >= _res_cfg.mem_threshold_pct or u["avail_gb"] < _res_cfg.mem_threshold_gb,
        alert_fmt=_fmt,
        recovery_fmt=lambda h, u: f"✅ Memory recovered: {h} — {u['pct']}% ({u['avail_gb']:.1f}GB available)",
        category="Memory",
    )


def _probe_io_all_hosts(remote_hosts: set[str]) -> None:
    _probe_metric(
        remote_hosts,
        metric="io",
        usage_store=host_health.io_usage,
        alerted_store=host_health.io_alerted,
        alert_ts_store=host_health.io_alert_ts,
        cooldown=_res_cfg.io_cooldown,
        is_critical_fn=lambda u: u["iowait_pct"] >= _res_cfg.io_iowait_pct,
        alert_fmt=lambda h, u: f"⚡ IO critical: {h}\nIO wait: {u['iowait_pct']}%\nIOPS: {u['read_iops']}r + {u['write_iops']}w"
        + (f" | disk util: {u['util_pct']}%" if u["util_pct"] else ""),
        recovery_fmt=lambda h, u: f"✅ IO recovered: {h} — iowait {u['iowait_pct']}%, IOPS {u['read_iops']}r+{u['write_iops']}w",
        category="IO",
    )


def _probe_cpu_hogs(remote_hosts: set[str]) -> None:
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host):
            continue
        host_label = host or "VPS"
        all_hogs = _run_metric("cpu_hogs", host) or []
        real_hogs = [h for h in all_hogs if isinstance(h, dict) and h.get("etime_min", 0) >= CPU_HOG_DURATION_MIN]
        with watchdog.lock:
            host_health.cpu_hogs[host_label] = real_hogs
        if real_hogs and now - host_health.cpu_hog_alert_ts.get(host_label, 0) >= _res_cfg.cpu_hog_cooldown:
            lines = []
            for h in real_hogs[:5]:
                elapsed = (
                    f"{h['etime_min'] // 60}h{h['etime_min'] % 60}m" if h["etime_min"] >= 60 else f"{h['etime_min']}m"
                )
                lines.append(f"  PID {h['pid']}: {h['cpu']}% CPU for {elapsed} — {h['cmd']}")
            host_health.cpu_hog_alert_ts[host_label] = now
            _watchdog_alert(
                "CPU hog",
                f"🔥 Runaway process{'es' if len(real_hogs) > 1 else ''} on {host_label}:\n" + "\n".join(lines),
            )


def _probe_worktree_sizes(remote_hosts: set[str]) -> None:
    def _fmt(h: str, u: dict[str, Any]) -> str:  # type: ignore[explicit-any]
        top = sorted(u.get("items", []), key=lambda x: x["size_gb"], reverse=True)[:5]
        return (
            f"📁 Worktree bloat on {h}: {u['total_gb']:.1f}GB total (threshold: {_res_cfg.worktree_threshold_gb}GB)\nTop directories:\n"
            + "\n".join(f"  {it['size_gb']}GB — {it['path']}" for it in top)
        )

    _probe_metric(
        remote_hosts,
        metric="worktree",
        usage_store=host_health.worktree_usage,
        alerted_store=host_health.worktree_alerted,
        alert_ts_store=host_health.worktree_alert_ts,
        cooldown=_res_cfg.worktree_cooldown,
        is_critical_fn=lambda u: u["total_gb"] >= _res_cfg.worktree_threshold_gb,
        alert_fmt=_fmt,
        recovery_fmt=lambda h, u: f"✅ Worktree size recovered: {h} — {u['total_gb']:.1f}GB (below {_res_cfg.worktree_threshold_gb}GB)",
        category="Worktree",
    )


def _probe_tailscale() -> None:
    now = _clock.time()
    result = _run_metric("tailscale")
    is_up = isinstance(result, dict) and result.get("up", False)
    alert_text = None
    with watchdog.lock:
        if not is_up and not host_health.tailscale_down:
            if now - host_health.tailscale_alert_ts >= _res_cfg.infra_cooldown:
                host_health.tailscale_down = True
                host_health.tailscale_alert_ts = now
                alert_text = "🚨 Tailscale is DOWN on VPS — 100.125.36.102 unreachable from external network.\nRun: sudo tailscale up"
        elif is_up and host_health.tailscale_down:
            host_health.tailscale_down = False
            alert_text = "✅ Tailscale recovered — VPS reachable at 100.125.36.102"
    _watchdog_alert("Tailscale", alert_text)


# Bridge proxy helpers removed — real definitions later in this file


# --- Pure display functions ---
def _normalize_activity(raw: str) -> str:
    if not raw:
        return raw
    m = re.match(r"^([A-Z][a-z]+ing)\s*(?:\((.+)\))?\s*$", raw)
    if m:
        verb = m.group(1)
        dur = m.group(2)
        if dur:
            return f"Thinking ({dur})"
        return "Thinking"
    return raw


def _team_attention_summary(watchdog_status: str, activity: str) -> tuple[str, str, int]:
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


def _format_watchdog_status_pure(
    name: str,
    pending_lookup: Callable[[str], bool] | None = None,
    state_snapshot: dict[str, WorkerStateEntry] | None = None,
    clock_now: float | None = None,
) -> str:
    if pending_lookup is None:

        def pending_lookup(_: str) -> bool:
            return False

    if clock_now is None:
        clock_now = time.time()
    entry = state_snapshot.get(name) if state_snapshot else None
    if not entry:
        return "Working" if pending_lookup(name) else "Ready"
    state, _reason, since = entry
    now = clock_now
    _SIMPLE = {
        "READY": "Ready",
        "BUSY_TOOL": "Working",
        "BUSY_THINKING": "Thinking",
        "WAITING": "Working",
        "DEAD": "Not responding",
        "HOST_OFFLINE": "Host offline",
        "OFFLINE": "Offline",
        "EXITED": "Session ended",
        "UNTRACKED_BUSY": "Working",
    }
    if state in _SIMPLE:
        return _SIMPLE[state]
    minutes = max(0, int((now - since) / 60)) if since else 0
    if state == "WAITING_INPUT":
        return f"Needs reply ({minutes}m)"
    if state == "STUCK":
        age_match = re.search(r"age=(\d+)s", _reason) if _reason else None
        if age_match:
            minutes = int(age_match.group(1)) // 60
        return f"No progress ({minutes}m)"
    if state == "POISONED":
        return f"Error loop ({minutes}m)"
    return state.lower()


def _format_team_lines_pure(
    registered: dict[str, TmuxSessionDict],
    active: str | None,
    pending_lookup: Callable[[str], bool] | None = None,
    worker_live: dict[str, TmuxSessionDict] | dict[str, dict[str, str | None]] | None = None,
    *,
    state_snapshot: dict[str, WorkerStateEntry] | None = None,
    clock_now: float | None = None,
    normalize_backend_fn: Callable[[str | None], str] | None = None,
) -> list[str]:
    pending_lookup = pending_lookup or (lambda _: False)
    worker_live = worker_live or {}
    state_snapshot = state_snapshot or {}
    clock_now = clock_now or time.time()
    normalize_backend_fn = normalize_backend_fn or (lambda b: b or DEFAULT_BACKEND)
    backend_values = set()
    for name, session in registered.items():
        live = worker_live.get(name, {})
        backend = normalize_backend_fn(live.get("backend") or session.get("backend"))
        backend_values.add(backend)
    show_backend = len(backend_values) > 1
    rows = []
    counts = {"🔴": 0, "🟡": 0, "🟢": 0}
    for name in sorted(registered.keys()):
        session = registered[name]
        watchdog_status = _format_watchdog_status_pure(
            name, pending_lookup, state_snapshot=state_snapshot, clock_now=clock_now
        )
        live = worker_live.get(name, {})
        backend = normalize_backend_fn(live.get("backend") or session.get("backend"))
        raw_activity = str(live.get("activity") or "").strip()
        if not raw_activity or raw_activity == "Unknown":
            raw_activity = watchdog_status
        activity = _normalize_activity(raw_activity)
        if len(activity) > 42:
            activity = activity[:39].rstrip() + "..."
        context_pct = str(live.get("context_pct") or "").strip()
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


# --- State computation ---
def _check_stuck_or_poisoned(
    pending_age: float,
    last_hook_ts: float | None,
    pending_ts: int | None,
    poisoned_reason: str | None,
    reason_suffix: str,
) -> tuple[str, str] | None:
    if pending_age < STALE_PENDING:
        return None
    hook_since = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
    if not hook_since:
        if poisoned_reason is not None:
            return "POISONED", f"{poisoned_reason}"
        return "STUCK", f"age={int(pending_age)}s{reason_suffix}"
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
    if not tmux_exists:
        return "OFFLINE", "tmux missing"
    if not is_interactive:
        if adapter_alive:
            return "BUSY_TOOL", "adapter running"
        if pending:
            stuck = _check_stuck_or_poisoned(pending_age, last_hook_ts, pending_ts, poisoned_reason, "")
            return stuck if stuck else ("WAITING", f"age={int(pending_age)}s")
        return "READY", "idle"
    if not claude_pid and last_seen_claude is not None and (now - last_seen_claude) > START_GRACE:
        return "DEAD", f"claude missing {int(now - last_seen_claude)}s"
    if pending and children > 0:
        return "BUSY_TOOL", f"children={children}"
    if pending and children == 0:
        if (pending_age <= THINK_GRACE) or ((now - last_child_ts) <= TOOL_GAP_GRACE) or (cpu >= CPU_ACTIVE):
            return "BUSY_THINKING", f"age={int(pending_age)}s cpu={cpu:.1f}"
        stuck = _check_stuck_or_poisoned(pending_age, last_hook_ts, pending_ts, poisoned_reason, f" cpu={cpu:.1f}")
        if stuck and cpu < CPU_IDLE:
            return stuck
        return "WAITING", f"age={int(pending_age)}s"
    if not pending and children > 0:
        return "UNTRACKED_BUSY", f"children={children}"
    if claude_pid and not pending:
        return "READY", "idle"
    return "OFFLINE", "tmux alive, claude missing"


def _check_adapter_log(name: str, tail_lines: int = 20) -> str:
    if tail_lines <= 0:
        return ""
    host = get_worker_host(name)
    if host:
        try:
            remote_log = _remap_path(str(get_session_dir(name) / "adapter.log"), host)
            r = _remote_run(
                ["tail", "-n", str(tail_lines), remote_log],
                host=host,
                timeout=TIMEOUT_TMUX_SEND,
            )
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


# --- Watchdog alerting ---
def _watchdog_alert(category: str, text: str | None) -> None:
    if not text or not admin_chat_id:
        return
    with _log_suppress(
        urllib.error.URLError, OSError, TimeoutError, level=_LOG_ERROR, tag="watchdog", msg=f"{category} error: "
    ):
        transport.send_text(admin_chat_id, text)
        _log(_LOG_WARN, "watchdog", f"{category}: {text.splitlines()[0]}")


def _send_watchdog_alert(name: str, state: str, reason: str) -> None:
    if admin_chat_id is None:
        return
    now = _clock.time()
    with watchdog.lock:
        last = watchdog.last_alert_ts.get(name)
    if last and (now - last) < ALERT_COOLDOWN:
        _log(
            _LOG_WARN,
            "watchdog",
            f"Alert suppressed for {name} ({state}): cooldown {now - last:.0f}s < {ALERT_COOLDOWN}s",
        )
        return
    _ALERT_TEMPLATES = {
        "POISONED": ("🔴", "is stuck in an error loop", "/restart --clean"),
        "DEAD": ("🔴", "stopped unexpectedly", "/restart --clean"),
        "EXITED": ("🟡", "'s session ended", "/restart"),
        "OFFLINE": ("🔴", "is not running", "/hire"),
    }
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
                parts.append(f"{'➔ ' if o.get('selected') else '  '}{o['num']}. {o['label']}")
            parts.append(f'\nReply 1-{max(o["num"] for o in details["options"])} to choose, or "skip" to cancel.')
        text = "\n".join(parts)
    elif state == "STUCK":
        age_match = re.search(r"age=(\d+)s", reason)
        age_min = int(age_match.group(1)) // 60 if age_match else 0
        age_str = f"{age_min}min" if age_min > 0 else reason.split()[0]
        text = f"🔴 {name} has made no progress for {age_str}.\n/restart --clean {name} (starts fresh)"
    elif state in _ALERT_TEMPLATES:
        icon, msg, cmd = _ALERT_TEMPLATES[state]
        text = f"{icon} {name} {msg}.\n{cmd} {name}"
    else:
        text = f"{name}: {state} ({reason}). Check /team"
    with _log_suppress(KeyError, level=_LOG_ERROR, tag="watchdog", msg="Watchdog alert error: "):
        result = transport.send_text(admin_chat_id, text)
        if result and result.get("ok"):
            _log(_LOG_WARN, "watchdog", f"Alert sent for {name} ({state}): {text[:80]}")
            _res = result.get("result", {})
            msg_id = _res.get("message_id") if isinstance(_res, dict) else None
            with watchdog.lock:
                watchdog.last_alert_ts[name] = now
                if msg_id:
                    watchdog.alert_msg_ids[name] = (msg_id, text)
        else:
            _log(_LOG_WARN, "watchdog", f"Alert FAILED for {name} ({state}): {result}")


def _record_host_probe(host: str, ok: bool, error: str | None = None) -> None:
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
                _do_send = True
                alert_text = f"✅ Host BACK UP: {host}\nWas down for {duration // 60}m {duration % 60}s\nWorkers affected: {', '.join(workers_on_host) or 'none'}"
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
                _do_send = True
                alert_text = f"🔴 Host DOWN: {host}\nAfter {failures} consecutive SSH failures\nError: {error or 'unknown'}\nWorkers affected: {', '.join(workers_on_host) or 'none'}"
            else:
                _do_send = False
                alert_text = None
    if _do_send:
        _watchdog_alert("Host", alert_text)


def _is_host_down(host: str) -> bool:
    with watchdog.lock:
        return host_health.down.get(host, False)


def _send_resolved_alert(name: str, new_state: str) -> None:
    if admin_chat_id is None:
        return
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    with watchdog.lock:
        prev_state = watchdog.prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states:
        return
    restart_ts = watchdog.recent_restarts.get(name)
    if restart_ts and _clock.time() - restart_ts < 30:
        return
    now = _clock.time()
    last_resolved = watchdog.last_resolved_ts.get(name, 0)
    if now - last_resolved < 180:
        return
    watchdog.last_resolved_ts[name] = now
    with watchdog.lock:
        alert_info = watchdog.alert_msg_ids.pop(name, None)
    if alert_info:
        old_msg_id, old_text = alert_info
        resolved_text = f"✅ {name} resolved (was: {old_text.splitlines()[0]})"
        with suppress(urllib.error.URLError, OSError, TimeoutError):
            transport.edit_message(admin_chat_id, old_msg_id, resolved_text)
            _log(_LOG_WARN, "watchdog", f"Edited alert for {name} -> resolved")
            return
    text = f"✅ {name} is back to normal."
    with _log_suppress(
        urllib.error.URLError,
        OSError,
        TimeoutError,
        level=_LOG_ERROR,
        tag="watchdog",
        msg="Watchdog resolved alert error: ",
    ):
        transport.send_text(admin_chat_id, text)


def _handle_watchdog_transition(
    name: str,
    state: str,
    reason: str,
    since: float,
    now: float | None = None,
) -> None:
    if now is None:
        now = _clock.time()
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with watchdog.lock:
        prev_state = watchdog.prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state
    if state == "HOST_OFFLINE":
        with watchdog.lock:
            watchdog.prev_worker_states[name] = state
        return
    teleport_state_file = SESSIONS_DIR / name / "teleport_state"
    if state in {"OFFLINE", "DEAD", "EXITED"} and teleport_state_file.exists():
        with watchdog.lock:
            watchdog.prev_worker_states[name] = state
        return

    def eligible_for_alert() -> bool:
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
    with watchdog.lock:
        prev = watchdog.worker_states.get(name)
        since = prev[2] if prev and prev[0] == state else now
        watchdog.worker_states[name] = WorkerStateEntry(state, reason, since)
    return since


# --- Watchdog loop and helpers ---
def watchdog_loop() -> None:
    _disk_check_counter = 0
    while not watchdog.stop_event.is_set():
        with _log_suppress(
            subprocess.SubprocessError, ValueError, KeyError, level=_LOG_ERROR, tag="watchdog", msg="Watchdog error: "
        ):
            now = _clock.time()
            registered = get_registered_sessions()
            pane_pids = _tmux_pane_pids()
            registered_names = set(registered.keys())
            probe_failed = bool(registered_names) and not pane_pids
            _watchdog_update_probe_failures(registered_names, probe_failed)
            remote_workers, remote_pane_pids, failed_hosts = _watchdog_probe_remote_hosts(registered)
            claude_pids, tmux_present, backend_info = _watchdog_collect_worker_pids(
                registered, pane_pids, remote_pane_pids, now
            )
            stats = _watchdog_gather_cpu_stats(claude_pids)
            _watchdog_evaluate_workers(
                registered, tmux_present, claude_pids, backend_info, stats, probe_failed, failed_hosts, now
            )
            _watchdog_cleanup_stale(registered_names)
            _disk_check_counter += 1
            if _disk_check_counter >= 20:
                _disk_check_counter = 0
                _watchdog_resource_checks(set(remote_workers.keys()))
            health.ok("watchdog")
            _health_alert_check()
        watchdog.stop_event.wait(WATCHDOG_INTERVAL)


def _watchdog_update_probe_failures(registered_names: set[str], probe_failed: bool) -> None:
    if probe_failed:
        for name in registered_names:
            watchdog.consecutive_probe_failures[name] = watchdog.consecutive_probe_failures.get(name, 0) + 1
    else:
        for name in registered_names:
            watchdog.consecutive_probe_failures[name] = 0


def _watchdog_probe_remote_hosts(
    registered: dict[str, TmuxSessionDict]
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str], set[str]]:
    remote_workers: dict[str, list[tuple[str, str]]] = {}
    for name, session in registered.items():
        host = get_worker_host(name)
        if host:
            tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
            remote_workers.setdefault(host, []).append((name, tmux_name))
    remote_pane_pids: dict[str, str] = {}
    failed_hosts: set[str] = set()
    for host, _workers in remote_workers.items():
        try:
            r = _remote_run(
                ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
                host=host,
                timeout=TIMEOUT_TMUX_SEND,
            )
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
    registered: dict[str, TmuxSessionDict], pane_pids: dict[str, str], remote_pane_pids: dict[str, str], now: float
) -> tuple[dict[str, str], dict[str, bool], dict[str, Backend]]:

    claude_pids: dict[str, str] = {}
    tmux_present: dict[str, bool] = {}
    backend_info: dict[str, Backend] = {}
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
        if backend.is_interactive and pane_pid:
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


def _watchdog_gather_cpu_stats(claude_pids: dict[str, str]) -> dict[str, ProcStatsEntry]:
    pids_by_host: dict[str | None, list[str]] = {}
    for name, pid in claude_pids.items():
        host = get_worker_host(name)
        pids_by_host.setdefault(host, []).append(pid)
    stats: dict[str, ProcStatsEntry] = {}
    for host, pids in pids_by_host.items():
        stats.update(_ps_stats(pids, host=host))
    return stats


def _watchdog_evaluate_workers(
    registered: dict[str, TmuxSessionDict],
    tmux_present: dict[str, bool],
    claude_pids: dict[str, str],
    backend_info: dict[str, Backend],
    stats: dict[str, ProcStatsEntry],
    probe_failed: bool,
    failed_hosts: set[str],
    now: float,
) -> None:

    for name, session in registered.items():
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
        tmux_exists = tmux_present.get(name, False)
        if not tmux_exists and "tmux" not in session:
            since = _record_worker_state(name, "EXITED", "session gone", now)
            _handle_watchdog_transition(name, "EXITED", "session gone", since, now=now)
            continue
        if probe_failed and not tmux_exists and watchdog.consecutive_probe_failures.get(name, 0) < 3:
            continue
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
            with processes.adapter_pids_lock:
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
        state_kw: dict[str, object] = {
            "tmux_exists": tmux_exists,
            "claude_pid": claude_pid,
            "pending": pending,
            "pending_ts": pending_ts,
            "pending_age": pending_age,
            "children": children,
            "last_child_ts": last_child_ts,
            "cpu": cpu,
            "last_hook_ts": last_hook_ts,
            "last_seen_claude": last_seen_claude,
            "now": now,
            "is_interactive": is_interactive,
            "adapter_alive": adapter_alive,
        }
        worker_state, reason = compute_state(**state_kw)  # type: ignore[arg-type]
        worker_state, reason = _watchdog_refine_state(name, tmux_name, worker_state, reason, state_kw, host)
        since = _record_worker_state(name, worker_state, reason, now)
        _handle_watchdog_transition(name, worker_state, reason, since, now=now)


def _watchdog_compute_children(
    name: str, children_total: int, is_interactive: bool, claude_pid: str | None, now: float
) -> int:
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
    with watchdog.lock:
        prev_children = watchdog.prev_children.get(name)
        activity_increased = prev_children is not None and children > prev_children
        if activity_increased or cpu >= CPU_ACTIVE:
            watchdog.last_activity_ts[name] = now
        watchdog.prev_children[name] = children


def _watchdog_refine_state(
    name: str,
    tmux_name: str,
    worker_state: str,
    reason: str,
    state_kw: dict[str, object],
    host: str | None,
) -> tuple[str, str]:
    is_interactive = bool(state_kw.get("is_interactive", True))
    pending = bool(state_kw.get("pending"))
    pending_age = float(state_kw.get("pending_age", 0.0))
    now = float(state_kw.get("now", 0.0))  # type: ignore[arg-type]
    if worker_state == "STUCK":
        watchdog.idle_streak[name] = watchdog.idle_streak.get(name, 0) + 1
        streak = watchdog.idle_streak[name]
        if streak < IDLE_STREAK_STUCK:
            worker_state = "WAITING"
        else:
            if is_interactive and pending:
                pane_text = _capture_pane_text(tmux_name, lines=15, host=host)
                if pane_text:
                    activity = _extract_activity(pane_text.splitlines())
                    if activity == "Idle at prompt":
                        _log(
                            _LOG_WARN,
                            "watchdog",
                            f"Auto-clearing stale pending for {name} (idle at prompt, age={int(pending_age)}s)",
                        )
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
        if streak:
            reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    else:
        watchdog.idle_streak[name] = 0
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
    with watchdog.lock:
        stale_dicts: list[dict[str, object]] = cast(
            list[dict[str, object]],
            [
                watchdog.worker_states,
                watchdog.last_child_ts,
                watchdog.last_seen_claude,
                watchdog.last_hook_ts,
                watchdog.prev_worker_states,
                watchdog.last_alert_ts,
                watchdog.idle_streak,
                watchdog.idle_child_baseline,
                watchdog.prev_children,
                watchdog.last_activity_ts,
            ],
        )
        for d in stale_dicts:
            for name in list(d.keys()):
                if name not in registered_names:
                    d.pop(name, None)
    for name in list(watchdog.consecutive_probe_failures.keys()):
        if name not in registered_names:
            watchdog.consecutive_probe_failures.pop(name, None)


def _watchdog_resource_checks(remote_hosts: set[str]) -> None:
    checks: list[tuple[str, Callable[[], None]]] = [
        ("Disk", lambda: _probe_disk_all_hosts(remote_hosts)),
        ("Memory", lambda: _probe_mem_all_hosts(remote_hosts)),
        ("IO", lambda: _probe_io_all_hosts(remote_hosts)),
        ("CPU hog", lambda: _probe_cpu_hogs(remote_hosts)),
        ("Worktree", lambda: _probe_worktree_sizes(remote_hosts)),
        ("Tailscale", lambda: _probe_tailscale()),
    ]
    for label, check_fn in checks:
        with _log_suppress(
            subprocess.SubprocessError, OSError, level=_LOG_ERROR, tag="watchdog", msg=f"{label} check error: "
        ):
            check_fn()


def format_team_lines(  # type: ignore[no-redef]
    registered: dict[str, TmuxSessionDict],
    active: str | None,
    pending_lookup: Callable[[str], bool] | None = None,
    worker_live: dict[str, TmuxSessionDict] | dict[str, dict[str, str | None]] | None = None,
) -> list[str]:
    if pending_lookup is None:
        pending_lookup = is_pending
    with watchdog.lock:
        state_snapshot = dict(watchdog.worker_states)
    return _format_team_lines_pure(
        registered,
        active,
        pending_lookup=pending_lookup,
        worker_live=worker_live,
        state_snapshot=state_snapshot,
        clock_now=_clock.time(),
        normalize_backend_fn=normalize_backend,
    )


# Template loading, _transcript_stats, _render_transcript_loading, _render_transcript_html
# removed — all served by spawn-on-demand Bun viewer (rewind/)


def _err_page(title: str, detail: str = "") -> str:
    d = f"<p>{detail}</p>" if detail else ""
    return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>{title}</h1>{d}</body></html>"


class TranscriptSyncRegistry:
    def __init__(self) -> None:
        self._state: dict[str, TranscriptSyncState] = {}
        self._lock: threading.Lock = threading.Lock()

    def get(self, key: str) -> TranscriptSyncState | None:
        with self._lock:
            return self._state.get(key)

    def set(self, key: str, value: TranscriptSyncState) -> None:
        with self._lock:
            self._state[key] = value

    def update(self, key: str, **fields: object) -> None:
        with self._lock:
            if key in self._state:
                self._state[key].update(fields)  # type: ignore[typeddict-item]

    def get_started(self, key: str) -> float:
        with self._lock:
            entry = self._state.get(key, {})
            return entry.get("started", 0)


_transcript_sync = TranscriptSyncRegistry()

# _run_transcript_query + INDEXER_SCRIPT removed — Bun viewer indexes directly via bun:sqlite


def _start_transcript_sync(name: str, host: str, remote_path: str, local_tmp: Path, key: str) -> None:
    try:
        _transcript_sync.set(
            key,
            {
                "status": "syncing",
                "progress": "Connecting to remote host...",
                "started": _clock.time(),
                "path": None,
                "error": None,
            },
        )
        r = _subprocess_runner.run(
            ["ssh", host, f"stat -f%z '{remote_path}' 2>/dev/null || stat -c%s '{remote_path}' 2>/dev/null"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_REMOTE_CMD,
        )
        remote_size = 0
        if r.returncode == 0 and r.stdout.strip().isdigit():
            remote_size = int(r.stdout.strip())
        if remote_size > 0:
            size_mb = remote_size / 1_048_576
            _transcript_sync.update(key, progress=f"Syncing transcript ({size_mb:.1f} MB)...", remote_size=remote_size)
        else:
            _transcript_sync.update(key, progress="Syncing transcript...")
        proc = _subprocess_runner.popen(
            ["rsync", "-az", f"{host}:{remote_path}", str(local_tmp)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            while proc.poll() is None:
                _clock.sleep(DELAY_STARTUP)
                try:
                    if local_tmp.exists() and remote_size > 0:
                        local_size = local_tmp.stat().st_size
                        pct = min(99, int(local_size * 100 / remote_size))
                        _transcript_sync.update(
                            key,
                            progress=f"Syncing... {pct}% ({local_size / 1_048_576:.1f} / {remote_size / 1_048_576:.1f} MB)",
                            pct=pct,
                        )
                except (OSError, ValueError) as exc:
                    _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
            if proc.returncode == 0 and local_tmp.exists() and local_tmp.stat().st_size > 0:
                _transcript_sync.set(
                    key,
                    {
                        "status": "done",
                        "progress": "Ready",
                        "path": str(local_tmp),
                        "started": _transcript_sync.get_started(key),
                        "error": None,
                    },
                )
            else:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                _transcript_sync.set(
                    key,
                    {
                        "status": "error",
                        "progress": "Sync failed",
                        "started": _transcript_sync.get_started(key),
                        "path": None,
                        "error": stderr[:200] or "rsync failed",
                    },
                )
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        _transcript_sync.set(
            key,
            {
                "status": "error",
                "progress": "Sync failed",
                "started": _transcript_sync.get_started(key),
                "path": None,
                "error": str(e)[:200],
            },
        )


def _resolve_transcript_path(name: str, session_id: str | None = None) -> tuple[str | None, str | None, str]:
    cwd = get_claude_session_cwd(name)
    if not cwd:
        host = get_worker_host(name)
        tmux_name = f"{TMUX_PREFIX}{name}"
        with suppress(Exception):
            assert worker_manager is not None
            cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
    cwd = cwd or os.path.expanduser("~")
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
                    remote_cwd = _remap_path(cwd, host)
                    remote_slug = _project_slug(remote_cwd)
                    remote_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                    local_tmp = Path(f"/tmp/transcript-{name}-{sid}.jsonl")
                    sync_key = f"{name}:{sid}"
                    sync_info = _transcript_sync.get(sync_key)
                    if sync_info and sync_info["status"] == "done" and local_tmp.exists():
                        transcript_path = local_tmp
                    elif sync_info and sync_info["status"] == "syncing":
                        return "syncing", sid, cwd
                    elif sync_info and sync_info["status"] == "error":
                        err = sync_info.get("error", "unknown error")
                        _log(_LOG_WARN, "transcript", f"sync failed for {name}:{sid}: {err}")
                        return None, sid, cwd
                    else:
                        _task_pool.submit(_start_transcript_sync, name, host, remote_path, local_tmp, sync_key)
                        return "syncing", sid, cwd
            except (OSError, subprocess.SubprocessError) as exc:
                _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    if transcript_path.exists():
        return str(transcript_path), sid, cwd
    return None, sid, cwd


# _parse_transcript_entries, handle_transcript_endpoint removed — served by Bun viewer (rewind/)


# PR review data fetching (PRCache, fetch_pr_cached, fetch_pr_*, _gh_api, generate_html,
# _pr_review_main, _fetch_file_context, _notify_telegram, _route_mentions_to_workers)
# removed — served by standalone scripts (review/generate.py, review/src/serve.ts)


class GuestSessionDict(_GuestSessionDictRequired, total=False):
    token_hash: str
    expires_at: str


GuestInboxMessageDict = TypedDict(
    "GuestInboxMessageDict",
    {
        "id": str,
        "from": str,
        "sender": str,
        "text": str,
        "ts": int,
        "to": str,
        "channel": str,
    },
    total=False,
)
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


def _save_state_json(path: Path, data: object, label: str) -> None:
    with _log_suppress(OSError, tag=label, msg="Failed to save state: "):
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.rename(path)
        os.chmod(path, 0o600)


def _load_state_json(path: Path, label: str) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            result: object = json.load(f)
            return cast(dict[str, object], result) if isinstance(result, dict) else None
    except (json.JSONDecodeError, OSError, KeyError) as e:
        _log(_LOG_WARN, label, f"Failed to load state: {e}")
        return None


class GuestStore:
    def __init__(self) -> None:
        self.guests: dict[str, GuestSessionDict] = {}
        self.inboxes: dict[str, list[GuestInboxMessageDict]] = {}
        self.lock: threading.Lock = threading.Lock()


guest_store = GuestStore()
GUEST_TTL = 86400
GUEST_INBOX_CAP = 200


def _guest_save() -> None:
    now = _clock.time()
    active_guests = {}
    for k, v in guest_store.guests.items():
        if now <= v.get("expires_at_unix", 0):
            guest = dict(v)
            if isinstance(guest.get("notified_workers"), set):
                guest["notified_workers"] = list(cast("set[str] | list[str]", guest.get("notified_workers", [])))
            active_guests[k] = guest
    active_names = {guest["name"] for guest in active_guests.values()}
    active_inboxes = {k: v for k, v in guest_store.inboxes.items() if k in active_names}
    _save_state_json(NODE_DIR / "guest_state.json", {"guests": active_guests, "inboxes": active_inboxes}, "guest")


def _guest_load() -> None:
    data = _load_state_json(NODE_DIR / "guest_state.json", "guest")
    if not data:
        return
    now = _clock.time()
    restored = 0
    raw_guests = data.get("guests", {})
    if isinstance(raw_guests, dict):
        for k, v in raw_guests.items():
            if isinstance(v, dict) and now <= v.get("expires_at_unix", 0):
                if isinstance(v.get("notified_workers"), list):
                    v["notified_workers"] = set(v["notified_workers"])
                guest_store.guests[k] = cast("GuestSessionDict", v)
                restored += 1
    raw_inboxes = data.get("inboxes", {})
    if isinstance(raw_inboxes, dict):
        guest_store.inboxes.update(cast("dict[str, list[GuestInboxMessageDict]]", raw_inboxes))
    if restored:
        _log(_LOG_INFO, "guest", f"Restored {restored} active guest(s) from disk")


def guest_create_token() -> tuple[str, str]:
    token = f"gt_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


def guest_generate_name(existing_names: set[str] | None = None) -> str:
    if existing_names is None:
        existing_names = set()
    for _ in range(100):
        name = secrets.token_urlsafe(3).rstrip("=").lower()[:5]
        if len(name) >= 3 and name not in existing_names:
            return name
    return secrets.token_urlsafe(4).rstrip("=").lower()[:6]


def guest_validate_name(name: str, team_workers: set[str], existing_guests: set[str]) -> tuple[bool, str]:
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
    return _clock.time() >= expires_at_unix


def guest_inbox_filter(messages: list[GuestInboxMessageDict], after: str | None = None) -> list[GuestInboxMessageDict]:
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


def guest_inbox_append(inbox: list[GuestInboxMessageDict], msg: GuestInboxMessageDict) -> list[GuestInboxMessageDict]:
    inbox.append(msg)
    if len(inbox) > GUEST_INBOX_CAP:
        inbox = inbox[-GUEST_INBOX_CAP:]
    return inbox


class ChannelStore:
    def __init__(self) -> None:
        self.channels: dict[str, ChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()


channel_store = ChannelStore()
CHANNEL_TTL = 86400
CHANNEL_MSG_CAP = 200


def _channel_save() -> None:
    active = {cid: ch for cid, ch in channel_store.channels.items() if not channel_is_expired(ch)}
    _save_state_json(NODE_DIR / "channel_state.json", active, "channel")


def _load_store_entries(
    target: dict,
    path: Path,
    label: str,
    is_active: Callable[[dict], bool],
    cast_type: str = "",
) -> None:
    data = _load_state_json(path, label)
    if not data:
        return
    restored = 0
    for k, v in data.items():
        if isinstance(v, dict) and is_active(v):
            target[k] = v
            restored += 1
    if restored:
        _log(_LOG_INFO, label, f"Restored {restored} active {label}(s) from disk")


def _channel_load() -> None:
    _load_store_entries(
        channel_store.channels,
        NODE_DIR / "channel_state.json",
        "channel",
        lambda ch: not channel_is_expired(ch),
    )


def channel_create_id(label: str = "") -> str:
    short = secrets.token_urlsafe(4).rstrip("=").lower()[:6]
    return f"ch_{short}"


def _channel_member_entry(m: str) -> tuple[str, ChannelMemberDict] | None:
    if m == "manager":
        return "manager", cast("ChannelMemberDict", {"type": "manager"})
    if ":" in m:
        mtype, name = m.split(":", 1)
        if mtype in ("worker", "guest"):
            return m, cast("ChannelMemberDict", {"type": mtype, "name": name})
    return None


def channel_new(
    channel_id: str, label: str, created_by: str, members: list[str], ttl: int = CHANNEL_TTL
) -> ChannelDict:
    now = _clock.time()
    member_dict: dict[str, ChannelMemberDict] = {
        k: v for m in members for pair in [_channel_member_entry(m)] if pair for k, v in [pair]
    }
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
    added = []
    for m in members:
        if m in channel["members"]:
            continue
        entry = _channel_member_entry(m)
        if entry:
            channel["members"][entry[0]] = entry[1]
            added.append(m)
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
        "ts": int(_clock.time()),
    }
    channel["messages"].append(cast("ChannelMessageDict", msg))
    if len(channel["messages"]) > CHANNEL_MSG_CAP:
        channel["messages"] = channel["messages"][-CHANNEL_MSG_CAP:]
    return cast("ChannelMessageDict", msg)


def channel_get_messages(channel: ChannelDict, after: str | None = None) -> tuple[list[ChannelMessageDict], bool]:
    if not after:
        return list(channel["messages"]), False
    found_idx = -1
    for i, m in enumerate(channel["messages"]):
        if m["id"] == after:
            found_idx = i
            break
    if found_idx == -1:
        return list(channel["messages"]), True
    return channel["messages"][found_idx + 1 :], False


def channel_is_expired(channel: ChannelDict) -> bool:
    return _clock.time() > channel["expires_at_unix"]


class RelayStore:
    def __init__(self) -> None:
        self.channels: dict[str, RelayChannelDict] = {}
        self.lock: threading.Lock = threading.Lock()


relay_store = RelayStore()
RELAY_PUBLIC_HOST = os.environ.get("RELAY_PUBLIC_HOST", "157.180.48.254")


def _relay_save() -> None:
    now = _clock.time()
    active = {cid: ch for cid, ch in relay_store.channels.items() if now <= ch["expires_at_unix"]}
    _save_state_json(NODE_DIR / "relay_state.json", active, "relay")


def _relay_load() -> None:
    now = _clock.time()
    _load_store_entries(
        relay_store.channels,
        NODE_DIR / "relay_state.json",
        "relay",
        lambda ch: now <= ch.get("expires_at_unix", 0),
    )


def relay_channel_create(worker: str, label: str, ttl: int = 86400) -> tuple[RelayChannelDict, str, str]:
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
    return cast("RelayChannelDict", channel), guest_token, reply_token


def _relay_base_url() -> str:
    return BRIDGE_PUBLIC_URL or f"http://{RELAY_PUBLIC_HOST}:{PORT}"


def relay_guide_url(channel_id: str, guest_token: str) -> str:
    return f"{_relay_base_url()}/relay/{channel_id}?token={guest_token}"


def relay_guide_text(channel: RelayChannelDict, guest_token: str) -> str:
    base = f"{_relay_base_url()}/relay/{channel['id']}"
    workers = channel.get("workers", [channel["worker"]])
    w = ", ".join(workers)
    exp = channel["expires_at"]
    return (
        f"#!/usr/bin/env bash\n"
        f"# Relay channel to {w} — expires {exp}\n"
        f"# Connected workers: {w}\n"
        f"#\n"
        f"# Usage:\n"
        f"#   1. Source this script: source relay.sh\n"
        f'#   2. Send a message:    relay_send "your message here"\n'
        f"#   3. Check messages:    relay_messages\n"
        f"#   4. Poll for new:      relay_poll  (waits 2s between checks)\n"
        f"#\n"
        f"# Guidelines:\n"
        f"#   - Always prefix your name in messages so workers know who sent it\n"
        f"#   - Messages are delivered to ALL connected workers: {w}\n"
        f"#   - Workers reply asynchronously — use relay_poll to watch for responses\n"
        f"#   - Channel auto-expires at {exp}\n"
        f"\n"
        f'export RELAY_TOKEN="{guest_token}"\n'
        f'export RELAY="{base}"\n'
        f"\n"
        f"# Send a message to the connected workers\n"
        f"relay_send() {{\n"
        f'  local text="${{1:?Usage: relay_send \\"your message\\"}}" \n'
        f'  curl -fsS "$RELAY/send" \\\n'
        f'    -H "Authorization: Bearer $RELAY_TOKEN" \\\n'
        f'    -H "Content-Type: application/json" \\\n'
        f'    -d "$(printf \'{{"text":"%s"}}\' "$text")"\n'
        f"}}\n"
        f"\n"
        f"# Get all messages (or new messages after a given ID)\n"
        f"relay_messages() {{\n"
        f'  local after="${{1:-}}"\n'
        f'  local url="$RELAY/messages"\n'
        f'  [ -n "$after" ] && url="$url?after=$after"\n'
        f'  curl -fsS "$url" -H "Authorization: Bearer $RELAY_TOKEN"\n'
        f"}}\n"
        f"\n"
        f"# Poll for new messages (prints new ones, loops until Ctrl-C)\n"
        f"relay_poll() {{\n"
        f'  local last_id=""\n'
        f"  while true; do\n"
        f"    local resp\n"
        f'    resp=$(relay_messages "$last_id")\n'
        f"    local msgs\n"
        f'    msgs=$(echo "$resp" | grep -o \'"message_id":"[^"]*"\' | tail -1 | cut -d\'"\' -f4)\n'
        f'    if [ -n "$msgs" ]; then\n'
        f'      echo "$resp"\n'
        f'      last_id="$msgs"\n'
        f"    fi\n"
        f"    sleep 2\n"
        f"  done\n"
        f"}}\n"
    )


def _relay_auth(channel_id: str, token: str, hash_key: str) -> RelayChannelDict | None:
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel or _clock.time() > channel.get("expires_at_unix", 0):
        return None
    if hashlib.sha256(token.encode()).hexdigest() != channel.get(hash_key):
        return None
    return channel


def relay_auth_guest(channel_id: str, token: str) -> RelayChannelDict | None:
    return _relay_auth(channel_id, token, "guest_token_hash")


def relay_auth_reply(channel_id: str, token: str) -> RelayChannelDict | None:
    return _relay_auth(channel_id, token, "reply_token_hash")


def _relay_append_msg(channel: RelayChannelDict, direction: str, from_: str, to: str, text: str) -> RelayMessageDict:
    msg_id = f"msg_{secrets.token_urlsafe(4)}"
    relay_msg = cast(
        "RelayMessageDict",
        {
            "message_id": msg_id,
            "direction": direction,
            "from": from_,
            "to": to,
            "text": text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time())),
        },
    )
    with relay_store.lock:
        channel["messages"].append(relay_msg)
        _relay_save()
    return relay_msg


def relay_guest_send(channel_id: str, text: str) -> tuple[str | None, RelayMessageDict | None]:
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return None, None
    relay_msg = _relay_append_msg(channel, "guest_to_worker", channel["label"], channel["worker"], text)
    base = f"{_relay_base_url()}/relay/{channel_id}"
    rt = channel["reply_token"]
    envelope = (
        f"[RELAY from {channel['label']}]\nchannel: {channel_id}\nmessage_id: {relay_msg['message_id']}\n"
        f"reply: curl -fsS {base}/reply -H 'Authorization: Bearer {rt}' "
        f"-H 'Content-Type: application/json' -d '{{\"text\":\"YOUR_REPLY\"}}'\n\n{text}\n[/RELAY]"
    )
    return envelope, relay_msg


def relay_worker_reply(channel_id: str, text: str) -> RelayMessageDict | None:
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return None
    return _relay_append_msg(channel, "worker_to_guest", channel["worker"], channel["label"], text)


def relay_get_messages(channel_id: str, after: str | None = None) -> list[RelayMessageDict]:
    with relay_store.lock:
        channel = relay_store.channels.get(channel_id)
    if not channel:
        return []
    msgs = channel["messages"]
    if not after:
        return list(msgs)
    for i, m in enumerate(msgs):
        if m["message_id"] == after:
            return list(msgs[i + 1 :])
    return list(msgs)


def _relay_get_token(handler: BaseHTTPRequestHandler) -> str | None:
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    parsed = urlparse(handler.path)
    params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
    return params.get("token", "")


def _relay_auth_and_parse(
    handler: BaseHTTPRequestHandler, channel_id: str, auth_fn: Callable[[str], bool], body: bytes
) -> dict[str, object] | None:
    token = _relay_get_token(handler)
    if not token:
        handler._send_json(401, {"error": "missing token"})
        return None, "", None
    channel = auth_fn(channel_id, token)
    if not channel:
        handler._send_json(403, {"error": "invalid or expired channel/token"})
        return None, "", None
    try:
        data = cast(dict[str, object], json.loads(body)) if body else {}
    except (json.JSONDecodeError, ValueError):
        handler._send_json(400, {"error": "invalid JSON"})
        return None, "", None
    text = _str_field(data, "text").strip()
    if not text:
        handler._send_json(400, {"error": "missing text"})
        return None, "", None
    return data, text, channel


def handle_relay_get(handler: BaseHTTPRequestHandler, channel_id: str, action: str, parsed: ParseResult) -> None:
    token = _relay_get_token(handler)
    if not token:
        handler._send_json(401, {"error": "missing token"})
        return
    channel = relay_auth_guest(channel_id, token)
    if not channel:
        handler._send_json(403, {"error": "invalid or expired channel/token"})
        return
    if action is None:
        handler._send_text(200, relay_guide_text(channel, token))
        return
    if action == "messages":
        params = dict(p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
        handler._send_json(200, {"messages": relay_get_messages(channel_id, after=params.get("after"))})
        return
    if action == "status":
        handler._send_json(
            200,
            {
                "channel_id": channel_id,
                "worker": channel["worker"],
                "label": channel["label"],
                "expires_at": channel["expires_at"],
                "message_count": len(channel["messages"]),
            },
        )
        return
    handler._send_json(404, {"error": f"unknown action: {action}"})


def handle_relay_send(handler: BaseHTTPRequestHandler, channel_id: str, body: bytes = b"") -> None:
    data, text, channel = _relay_auth_and_parse(handler, channel_id, relay_auth_guest, body)
    if not data:
        return
    envelope, msg = relay_guest_send(channel_id, text)
    if not envelope:
        handler._send_json(500, {"error": "channel not found"})
        return
    workers = channel.get("workers", [channel["worker"]])
    delivered = {w: send_to_worker(w, envelope) for w in workers}
    handler._send_json(
        200, {"message_id": msg["message_id"], "delivered": all(delivered.values()), "workers": delivered}
    )


def handle_relay_reply(handler: BaseHTTPRequestHandler, channel_id: str, body: bytes = b"") -> None:
    data, text, _channel = _relay_auth_and_parse(handler, channel_id, relay_auth_reply, body)
    if not data:
        return
    msg = relay_worker_reply(channel_id, text)
    if not msg:
        handler._send_json(500, {"error": "channel not found"})
        return
    handler._send_json(200, {"message_id": msg["message_id"], "delivered": True})


def cmd_relay_list(router: CommandRouter, chat_id: int) -> bool:
    with relay_store.lock:
        active = [(cid, ch) for cid, ch in relay_store.channels.items() if _clock.time() <= ch["expires_at_unix"]]
    if active:
        lines = []
        for cid, ch in active:
            msg_count = len(ch.get("messages", []))
            workers = ch.get("workers", [ch["worker"]])
            worker_str = ", ".join(workers)
            lines.append(f"• {cid} → {worker_str} ({msg_count} msgs, expires {ch['expires_at']})")
        router.reply(chat_id, "📡 Active relays:\n" + "\n".join(lines))
    else:
        router.reply(chat_id, "No active relays.")
    return True


def _find_active_relay(channel_id: str) -> RelayChannelDict | None:
    with relay_store.lock:
        ch = relay_store.channels.get(channel_id)
        if ch and _clock.time() <= ch["expires_at_unix"]:
            return ch
    return None


def cmd_relay_add(router: CommandRouter, parts: list[str], chat_id: int) -> bool:
    if len(parts) < 3:
        router.reply(chat_id, "Usage: /relay add <channel_id> <worker> [worker2 ...]")
        return True
    channel_id = parts[1]
    new_workers = [p.lower() for p in parts[2:]]
    registered = get_registered_sessions()
    not_found = [w for w in new_workers if w not in registered]
    if not_found:
        router.reply(chat_id, f'Worker(s) not found: {", ".join(not_found)}')
        return True
    found = _find_active_relay(channel_id)
    if not found:
        router.reply(chat_id, f'Relay "{channel_id}" not found. Use /relay list to see active channels.')
        return True
    existing = set(found.get("workers", [found["worker"]]))
    to_add = [w for w in new_workers if w not in existing]
    if not to_add:
        router.reply(chat_id, f"All specified workers are already in {channel_id}.")
        return True
    with relay_store.lock:
        found.setdefault("workers", [found["worker"]]).extend(to_add)
        _relay_save()
    router.reply(chat_id, f"📡 Added {', '.join(to_add)} to {channel_id}. Workers: {', '.join(found['workers'])}")
    return True


def cmd_relay_remove(router: CommandRouter, parts: list[str], chat_id: int) -> bool:
    if len(parts) < 3:
        router.reply(chat_id, "Usage: /relay remove <channel_id> <worker>")
        return True
    channel_id, rm_worker = parts[1], parts[2].lower()
    found = _find_active_relay(channel_id)
    if not found:
        router.reply(chat_id, f'Relay "{channel_id}" not found. Use /relay list to see active channels.')
        return True
    workers = found.get("workers", [found["worker"]])
    if rm_worker not in workers:
        router.reply(chat_id, f"{rm_worker} is not in {channel_id}.")
        return True
    if len(workers) <= 1:
        router.reply(chat_id, f"Can't remove the last worker. Use /relay stop {channel_id} to close it.")
        return True
    with relay_store.lock:
        found["workers"].remove(rm_worker)
        _relay_save()
    router.reply(chat_id, f"📡 Removed {rm_worker} from {channel_id}. Workers: {', '.join(found['workers'])}")
    return True


def cmd_relay_status(router: CommandRouter, chat_id: int) -> bool:
    now = _clock.time()
    with relay_store.lock:
        relay_active = [(cid, ch) for cid, ch in relay_store.channels.items() if now <= ch["expires_at_unix"]]
    with channel_store.lock:
        ch_active = [(cid, ch) for cid, ch in channel_store.channels.items() if not channel_is_expired(ch)]
    with guest_store.lock:
        guest_active = [g for g in guest_store.guests.values() if now <= g.get("expires_at_unix", 0)]
    lines = ["📡 System Status\n"]
    lines.append(f"Relays: {len(relay_active)}")
    for _cid, ch in relay_active:
        msg_count = len(ch.get("messages", []))
        workers = ch.get("workers", [ch["worker"]])
        lines.append(f"  • {ch['label']} → {', '.join(workers)} ({msg_count} msgs)")
    lines.append(f"\nChannels: {len(ch_active)}")
    for cid, chan in ch_active:
        members_str = ", ".join(chan["members"].keys())
        msg_count = len(chan.get("messages", []))
        lines.append(f"  • {cid} ({chan['label']}) — {members_str} ({msg_count} msgs)")
    lines.append(f"\nGuests: {len(guest_active)}")
    for g in guest_active:
        inbox_count = len(guest_store.inboxes.get(g["name"], []))
        lines.append(f"  • {g['name']} (inbox: {inbox_count} msgs)")
    router.reply(chat_id, "\n".join(lines))
    return True


def cmd_relay_stop(router: CommandRouter, parts: list[str], chat_id: int) -> bool:
    if len(parts) < 2:
        router.reply(chat_id, "Usage: /relay stop <label>")
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
        router.reply(chat_id, f'📡 Relay "{target}" closed.')
    else:
        router.reply(chat_id, f'Relay "{target}" not found.')
    return True


def cmd_relay(router: CommandRouter, arg: str, chat_id: int) -> bool:
    if not arg:
        with relay_store.lock:
            active = [ch for ch in relay_store.channels.values() if _clock.time() <= ch["expires_at_unix"]]
        lines = ["📡 Relay — connect any agent to the team\n"]
        lines.append("/relay <worker> [worker2 ...] — create relay, get a guideline link")
        lines.append("/relay add <channel_id> <worker> [worker2 ...] — add worker(s) to relay")
        lines.append("/relay remove <channel_id> <worker> — remove worker")
        lines.append("/relay list — active relays")
        lines.append("/relay status — counts for relays, channels, guests")
        lines.append("/relay stop <name> — close relay")
        if active:
            lines.append(f"\nActive: {', '.join(ch['label'] for ch in active)}")
        router.reply(chat_id, "\n".join(lines))
        return True
    parts = arg.strip().split()
    sub = parts[0].lower()
    subcommands = {
        "list": lambda: cmd_relay_list(router, chat_id),
        "add": lambda: cmd_relay_add(router, parts, chat_id),
        "remove": lambda: cmd_relay_remove(router, parts, chat_id),
        "status": lambda: cmd_relay_status(router, chat_id),
        "stop": lambda: cmd_relay_stop(router, parts, chat_id),
    }
    if sub in subcommands:
        return subcommands[sub]()
    # All remaining parts are worker names: /relay mio sui geni
    registered = get_registered_sessions()
    worker_names = [p.lower() for p in parts]
    not_found = [w for w in worker_names if w not in registered]
    if not_found:
        router.reply(chat_id, f'Worker(s) not found: {", ".join(not_found)}')
        return True
    primary = worker_names[0]
    label = f"relay-{primary}" if len(worker_names) == 1 else f"relay-{'+'.join(worker_names)}"
    ch, guest_token, _reply_token = relay_channel_create(primary, label)
    if len(worker_names) > 1:
        ch["workers"] = worker_names
    with relay_store.lock:
        relay_store.channels[ch["id"]] = ch
        _relay_save()
    url = relay_guide_url(ch["id"], guest_token)
    worker_str = ", ".join(worker_names)
    lines = [f"📡 Relay to {worker_str} (24h) — {ch['id']}"]
    lines.append(f"\nManage: /relay add {ch['id']} <worker> to add more workers")
    lines.append(f"\nPaste this to the external agent:\n")
    lines.append(f"---")
    lines.append(
        f"You have a direct channel to {worker_str}. Open this link to see the API guide (setup + curl commands):"
    )
    lines.append(f"{url}")
    lines.append(
        f"Steps: (1) open the link, (2) copy the env vars and curl commands from the page, (3) send your message via the /send endpoint, (4) poll /messages for replies. Channel expires in 24h."
    )
    lines.append(f"---")
    router.reply(chat_id, "\n".join(lines))
    return True


class MachineConfigError(ValueError):
    pass


@dataclass
class Machine:
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
        return self.ssh_target is None

    def public_dict(self) -> MachinePublicDict:
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


def _validate_machine_id(machine_id: str) -> str:
    if not isinstance(machine_id, str) or not machine_id:
        raise MachineConfigError("machine id must be a non-empty string")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", machine_id):
        raise MachineConfigError(f"invalid machine id: {machine_id!r}")
    return machine_id


def _coerce_optional_str(value: object, field: str, machine_id: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MachineConfigError(f"machine {machine_id!r} field {field!r} must be a string")
    return value


def _implicit_local_machine() -> Machine:
    _id = BRIDGE_SSH_TARGET or "vps"
    return Machine(
        id=_id,
        ssh_target=None,
        bridge_base_url=BRIDGE_URL,
        home_root=str(Path.home()),
        os_family=_detect_os_family(),
        display_name=_id.upper(),
        role="bridge",
        configured=False,
    )


def load_machines_config(path: Path | None = None) -> dict[str, Machine]:
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
    with remote_cache.lock:
        if force_reload or remote_cache.machines is None or remote_cache.machines_path != MACHINES_CONFIG_FILE:
            remote_cache.machines = load_machines_config(MACHINES_CONFIG_FILE)
            remote_cache.machines_path = MACHINES_CONFIG_FILE
        return dict(remote_cache.machines)


def _machine_for_worker_host(host: str | None, machines: dict[str, Machine]) -> Machine:
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
    return health


def _machine_access(machine: Machine, caller_host: str | None) -> str:
    target_host = machine.ssh_target
    if caller_host == target_host:
        return "local"
    if target_host is None:
        return f"ssh {BRIDGE_SSH_TARGET}"
    return f"ssh {target_host}"


def get_machines(caller_from: str | None = None) -> MachinesCatalogResponse:
    machines = get_machine_catalog()
    registered = get_registered_sessions()
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
        rows[machine.id]["workers"].append(
            {
                "name": name,
                "backend": info.get("backend", DEFAULT_BACKEND),
                "status": "online" if info.get("tmux") or info.get("callback_url") else "exited",
            }
        )
        rows[machine.id]["worker_count"] += 1
    return {
        "version": 1,
        "config_path": str(MACHINES_CONFIG_FILE),
        "caller": caller_from or None,
        "machines": list(rows.values()),
    }


def parse_hire_args(raw: str) -> tuple[str, str]:
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
            backend = "codex"
        elif part.startswith("--"):
            pass
        else:
            name_parts.append(part)
        i += 1
    if len(name_parts) != 1:
        return "", backend
    name = name_parts[0]
    for backend_name in list_backends():
        prefix = f"{backend_name}-"
        if name.startswith(prefix):
            backend = backend_name
            name = name[len(prefix) :]
            break
    if not is_valid_backend(backend):
        return name, backend
    return name, backend


def _remote_copy(src: str, dst: str, host: str | None = None, direction: str = "push") -> None:
    if not host:
        shutil.copy2(src, dst)
    elif direction == "push":
        _subprocess_runner.run(["scp", "-q", src, f"{host}:{dst}"], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)
    else:
        _subprocess_runner.run(["scp", "-q", f"{host}:{src}", dst], capture_output=True, timeout=TIMEOUT_FILE_TRANSFER)


def _read_learning_reminder(name: str) -> str:
    try:
        if os.path.isfile(_LEARNING_REMINDER_PATH):
            text = Path(_LEARNING_REMINDER_PATH).read_text().strip()
            if text:
                return text.replace("{name}", name)
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to read learning reminder from {_LEARNING_REMINDER_PATH}: {e}")
    return _LEARNING_REMINDER_TEXT.replace("{name}", name)


def _new_reminder_state() -> ReminderState:
    now = _clock.time()
    return {
        "response_count": 0,
        "last_reminder_ts": now,
        "last_response_ts": now,
        "reminder_pending": False,
    }


def _reset_learning_reminder(name: str) -> None:
    with learning_reminders.lock:
        learning_reminders.state[name] = _new_reminder_state()


def _fire_reminder(name: str, st: ReminderState) -> None:
    st["response_count"] = 0
    st["last_reminder_ts"] = _clock.time()
    st["reminder_pending"] = True
    _task_pool.submit(_send_learning_reminder, name, _read_learning_reminder(name))


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
            return
        st["response_count"] = st.get("response_count", 0) + 1
        if st["response_count"] >= LEARNING_REMINDER_RESPONSE_THRESHOLD:
            _fire_reminder(name, st)


def _scan_idle_workers() -> None:
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


def _seed_learning_reminder_state(worker_names: Iterable[str]) -> None:
    with learning_reminders.lock:
        for name in worker_names:
            if name not in learning_reminders.state:
                learning_reminders.state[name] = _new_reminder_state()


def _schedule_idle_scan() -> None:
    learning_reminders.idle_scan_timer = threading.Timer(1800, _scan_idle_workers)
    learning_reminders.idle_scan_timer.name = "idle-scan"
    learning_reminders.idle_scan_timer.daemon = True
    learning_reminders.idle_scan_timer.start()


def _send_learning_reminder(name: str, text: str) -> None:
    with _log_suppress(
        ConnectionError, OSError, TimeoutError, level=_LOG_ERROR, msg=f"Learning reminder error for {name}: "
    ):
        _clock.sleep(DELAY_RESPONSE_GAP)
        if send_to_worker(name, text):
            _log(_LOG_INFO, "worker", f"Learning reminder sent to {name}")
        else:
            _log(_LOG_WARN, "bridge", f"Learning reminder: failed to send to {name}")


class ConnectorRegistry:
    def __init__(self) -> None:
        self.gmail: Any = None  # type: ignore[explicit-any]
        self.github: Any = None  # type: ignore[explicit-any]
        self._log: dict[str, collections.deque[ConnectorMessageLogEntry]] = {}
        self._lock: threading.Lock = threading.Lock()

    def stop_all(self) -> None:
        for name, inst in [("gmail", self.gmail), ("github", self.github)]:
            if inst is not None:
                with _log_suppress(RuntimeError, OSError, level=_LOG_DEBUG, tag=f"shutdown:{name}"):
                    inst.stop()
                    _log(_LOG_INFO, name, "Connector stopped")

    def log_message(self, tag: str, html_text: str, plain_text: str, targets: list[str]) -> None:
        with self._lock:
            if tag not in self._log:
                self._log[tag] = collections.deque(maxlen=20)
            self._log[tag].append(
                {
                    "ts": _clock.time(),
                    "html": html_text,
                    "plain": plain_text,
                    "targets": targets or [],
                }
            )

    def get_log(self, tag: str) -> list[ConnectorMessageLogEntry]:
        with self._lock:
            return list(self._log.get(tag, []))


def _parse_codex_transcript(path: str, host: str | None = None) -> list[CodexTranscriptEntry]:
    try:
        if host:
            r = _remote_run(["cat", path], host=host, timeout=TIMEOUT_GIT_OP)
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
            ev = cast(dict[str, object], json.loads(line))
        except json.JSONDecodeError:
            continue
        ev_type = str(ev.get("type", ""))
        raw_payload = ev.get("payload", {})
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
                if text and role != "developer":
                    messages.append({"role": role, "text": text, "timestamp": ts})
            elif item_type == "function_call":
                name = str(payload.get("name", ""))
                messages.append({"role": "tool_use", "text": f"[{name}]", "timestamp": ts})
            elif item_type == "function_call_output":
                output = str(payload.get("output", ""))
                if output:
                    messages.append({"role": "tool_result", "text": output[:500], "timestamp": ts})
    return cast(list[CodexTranscriptEntry], messages)


REWIND_TIMEOUT: int = 24 * 60 * 60
PR_REVIEW_EXTEND: int = 300


class TokenStore:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()

    def _validate(
        self, store: dict[str, dict[str, object]], token: str | None, extend: bool, ttl: int
    ) -> dict[str, object] | None:
        if not token:
            return None
        now = _clock.time()
        with self._lock:
            for k in [k for k, v in store.items() if v["expires_at"] <= now]:
                del store[k]
            entry = store.get(token)
            if entry and extend:
                entry["expires_at"] = now + ttl
            return entry

    def add_rewind(self, token: str, name: str, timeout: int = REWIND_TIMEOUT) -> None:
        with self._lock:
            REWIND_TOKENS[token] = {"name": name, "expires_at": _clock.time() + timeout}

    def validate_rewind(self, token: str | None, extend: bool = True) -> RewindTokenEntry | None:
        return self._validate(REWIND_TOKENS, token, extend, REWIND_TIMEOUT)

    def add_pr_review(self, token: str, pr_num: int, owner: str, repo: str) -> None:
        with self._lock:
            PR_REVIEW_TOKENS[token] = {
                "pr_num": pr_num,
                "owner": owner,
                "repo": repo,
                "expires_at": _clock.time() + PR_REVIEW_EXTEND,
            }

    def validate_pr_review(self, token: str | None, extend: bool = True) -> PrReviewTokenEntry | None:
        return self._validate(PR_REVIEW_TOKENS, token, extend, PR_REVIEW_EXTEND)


# --- Viewer Manager: spawn-on-demand Bun servers ---

_TOOL_PORT_MIN = 10171
_TOOL_PORT_MAX = 10179


class _SpawnedTool(NamedTuple):
    pid: int
    port: int
    token: str
    kind: str
    name: str
    started_at: float


class ToolManager:
    """Spawn and track lightweight Bun tool servers on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tools: dict[str, _SpawnedTool] = {}

    def _allocate_port(self) -> int | None:
        """Find an available port in the tool range."""
        used_ports = {v.port for v in self._tools.values()}
        for port in range(_TOOL_PORT_MIN, _TOOL_PORT_MAX + 1):
            if port in used_ports:
                continue
            # Also check if port is actually free
            try:
                r = _subprocess_runner.run(
                    ["ss", "-tlnp", f"sport = :{port}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if f":{port}" not in r.stdout:
                    return port
            except (OSError, subprocess.SubprocessError):
                return port
        return None

    def spawn(
        self,
        kind: str,
        name: str,
        ttl: int,
        extra_args: list[str],
        runtime: str = "bun",
        script_dir: str = "",
    ) -> tuple[str, str] | None:
        """Spawn a tool server. Returns (url, token) or None on failure.

        runtime: "bun" (default)
        script_dir: override script location (default: <kind>/src/<kind>.ts)
        """
        key = f"{kind}:{name}"
        with self._lock:
            # Reuse existing viewer if still alive
            existing = self._tools.get(key)
            if existing:
                try:
                    os.kill(existing.pid, 0)
                    base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
                    host = urlparse(base_url).hostname or "localhost"
                    url = f"http://{host}:{existing.port}/?token={existing.token}"
                    return url, existing.token
                except OSError:
                    del self._tools[key]

            port = self._allocate_port()
            if port is None:
                _log(_LOG_ERROR, "tool", "No available tool ports (10171-10179 all in use)")
                return None

        token = secrets.token_urlsafe(32)
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = script_dir or os.path.join(bridge_dir, kind, "src", f"{kind}.ts")

        if not os.path.exists(script_path):
            _log(_LOG_ERROR, "tool", f"Tool script not found: {script_path}")
            return None

        cmd = [runtime, script_path, "--port", str(port), "--token", token, "--ttl", str(ttl), *extra_args]

        try:
            proc = _subprocess_runner.popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Brief wait for startup
            time.sleep(0.3)
            if proc.poll() is not None:
                _log(_LOG_ERROR, "tool", f"Tool exited immediately (port {port})")
                return None

            viewer = _SpawnedTool(
                pid=proc.pid,
                port=port,
                token=token,
                kind=kind,
                name=name,
                started_at=_clock.time(),
            )
            with self._lock:
                self._tools[key] = viewer

            base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
            host = urlparse(base_url).hostname or "localhost"
            url = f"http://{host}:{port}/?token={token}"
            _log(_LOG_INFO, "tool", f"Spawned tool for {name} on port {port} (pid={proc.pid}, ttl={ttl}s)")
            return url, token
        except (OSError, subprocess.SubprocessError) as e:
            _log(_LOG_ERROR, "tool", f"Failed to spawn tool: {e}")
            return None

    def cleanup(self) -> None:
        """Remove dead tools from tracking."""
        with self._lock:
            dead = []
            for key, v in self._tools.items():
                try:
                    os.kill(v.pid, 0)
                except OSError:
                    dead.append(key)
            for key in dead:
                del self._tools[key]

    def active(self) -> list[_SpawnedTool]:
        """List active tools."""
        self.cleanup()
        with self._lock:
            return list(self._tools.values())


tool_manager = ToolManager()


def _extract_file_id(msg: TelegramMessageDict) -> tuple[str | None, str]:
    if msg.get("animation"):
        return msg["animation"].get("file_id"), "GIF"
    if msg.get("photo"):
        return max(msg["photo"], key=lambda p: p.get("file_size", 0)).get("file_id"), "image"
    doc = msg.get("document")
    if doc:
        if doc.get("mime_type", "").startswith("image/"):
            return doc.get("file_id"), "image"
        return doc.get("file_id"), "file"
    for key, label in [
        ("video", "video"),
        ("audio", "audio"),
        ("voice", "voice"),
        ("video_note", "video note"),
        ("sticker", "sticker"),
    ]:
        if msg.get(key):
            return msg[key].get("file_id"), label
    return None, "media"


def _format_offline_warning(statuses: list[MentionRouteResult | None]) -> str | None:
    sent_to = [s["name"] for s in statuses if s and s.get("status") == "sent"]
    offline = [s["name"] for s in statuses if s and s.get("status") == "offline"]
    if not offline:
        return None
    parts = [f"⚠️ {', '.join(offline)} {'is' if len(offline) == 1 else 'are'} offline."]
    if sent_to:
        parts.append(f"Delivered to {', '.join(sent_to)}.")
    return " ".join(parts)


def _gh_api_call(
    args: list[str], *, input_data: str | None = None, timeout: int = TIMEOUT_FILE_TRANSFER
) -> tuple[bool, str]:
    try:
        kw: dict[str, object] = {"capture_output": True, "text": True, "timeout": timeout}
        if input_data is not None:
            kw["input"] = input_data
        r = _subprocess_runner.run(["gh", "api", *args], **kw)
        if r.returncode != 0:
            return False, r.stderr.strip() or r.stdout.strip()
        return True, r.stdout
    except subprocess.TimeoutExpired:
        return False, "timeout"


def _fetch_remote_file(host: str, remote_path: str) -> str | None:
    original_name = Path(remote_path).name
    tmp_dir = tempfile.mkdtemp(prefix="remote-file-")
    local_path = os.path.join(tmp_dir, original_name)
    try:
        r = _subprocess_runner.run(
            ["rsync", "-az", f"{host}:{remote_path}", local_path], capture_output=True, text=True, timeout=TIMEOUT_RSYNC
        )
        if r.returncode == 0 and os.path.getsize(local_path) > 0:
            return local_path
        if r.returncode != 0:
            _log(
                _LOG_ERROR, "bridge", f"rsync failed (exit {r.returncode}): {host}:{remote_path} -> {r.stderr.strip()}"
            )
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_WARN, "bridge", f"Remote file fetch failed: {host}:{remote_path} -> {e}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None


def _localize_media(name: str, media_list: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    host = get_worker_host(name)
    if not host:
        return media_list
    result: list[tuple[str | None, str]] = []
    for file_path, caption in media_list:
        if not file_path:
            result.append((file_path, caption))
            continue
        local = _fetch_remote_file(host, file_path)
        if local:
            result.append((local, caption))
        elif os.path.isfile(file_path):
            # File exists locally on the bridge machine (e.g. worker generated
            # it via a tool that ran on the bridge host, not on the worker host)
            _log(_LOG_INFO, "bridge", f"Remote fetch failed but file exists locally: {file_path} for {name}")
            result.append((file_path, caption))
        else:
            _log(_LOG_WARN, "bridge", f"Cannot fetch remote file {host}:{file_path} for {name}")
            result.append((None, f"[Fetch failed: {file_path}]"))
    return result


def _parse_response_media(
    name: str, text: str
) -> tuple[str, list[tuple[str | None, str]], list[tuple[str | None, str]]]:
    host = get_worker_host(name)
    if host:

        def _accept_all(p: str | Path) -> FileValidation:
            return FileValidation(True, Path(p))

        clean_text, images = _parse_media_tags(text, "image", _accept_all)
        clean_text, files = _parse_media_tags(clean_text, "file", _accept_all)
    else:
        clean_text, images = parse_image_tags(text)
        clean_text, files = parse_file_tags(clean_text)
    images = _localize_media(name, images)
    files = _localize_media(name, files)
    return clean_text, images, files


def _send_text_via_telegram(name: str, clean_text: str, chat_id: int, log_prefix: str) -> None:
    rich_sent = False
    rich_failed_at = -1
    rich_chunks: list[str] = []
    prev_msg_id: int | None = None
    if hasattr(transport, "send_rich_text"):
        rich_text = clean_text.lstrip()
        prefix_lower = f"{name}:".lower()
        if rich_text.lower().startswith(prefix_lower):
            rich_text = rich_text[len(prefix_lower) :].lstrip()
        rich_text = _pipe_tables_to_html(rich_text)
        rich_md = f"**{name}:**\n{rich_text}"
        prefix_reserve = len(name) + 30
        rich_chunks = split_message(rich_md, TELEGRAM_RICH_MAX_LENGTH - prefix_reserve)
        rich_sent = True
        for i, chunk in enumerate(rich_chunks):
            if i > 0:
                chunk = f"**{name}:** _(continued)_\n{chunk}"
            result = transport.send_rich_text(chat_id, chunk, reply_to=prev_msg_id if prev_msg_id else None)
            if result and result.get("ok"):
                health.ok("telegram_api")
                _rr = result.get("result", {})
                prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
                if len(rich_chunks) > 1:
                    _log(
                        _LOG_INFO,
                        "telegram",
                        f"{log_prefix} sent (rich): {name} part {i+1}/{len(rich_chunks)} -> Telegram OK",
                    )
                else:
                    _log(_LOG_INFO, "telegram", f"{log_prefix} sent (rich): {name} -> Telegram OK")
            else:
                health.error("telegram_api")
                error_code = (result or {}).get("error_code", 0)
                desc = (result or {}).get("description", "")
                _log(
                    _LOG_ERROR,
                    "bridge",
                    f"{log_prefix} sendRichMessage failed ({error_code}: {desc}), falling back to HTML",
                )
                rich_sent = False
                rich_failed_at = i
                break
            if i < len(rich_chunks) - 1:
                _clock.sleep(DELAY_BRIEF)
    if not rich_sent and rich_failed_at > 0:
        _send_as_html(name, "\n".join(rich_chunks[rich_failed_at:]), chat_id, log_prefix, prev_msg_id)
        rich_sent = True
    if not rich_sent:
        _send_as_html(name, clean_text, chat_id, log_prefix)


def _send_as_html(name: str, text: str, chat_id: int, log_prefix: str, prev_msg_id: int | None = None) -> int | None:
    html_text = markdown_to_telegram_html(text)
    prefix_reserve = len(name) + 30
    chunks = split_message(html_text, TELEGRAM_MAX_LENGTH - prefix_reserve)
    formatted_parts = format_multipart_messages(name, chunks)
    for i, part in enumerate(formatted_parts):
        result = transport.send_text(chat_id, part, parse_mode="HTML", reply_to=prev_msg_id if prev_msg_id else None)
        if result and result.get("ok"):
            health.ok("telegram_api")
            _rr = result.get("result", {})
            prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
            if len(formatted_parts) > 1:
                _log(
                    _LOG_INFO, "telegram", f"{log_prefix} sent: {name} part {i+1}/{len(formatted_parts)} -> Telegram OK"
                )
            else:
                _log(_LOG_INFO, "telegram", f"{log_prefix} sent: {name} -> Telegram OK")
        else:
            health.error("telegram_api")
            desc = str((result or {}).get("description", ""))
            _log(_LOG_WARN, "bridge", f"{log_prefix} HTML send failed ({desc}), retrying as plain text")
            plain_text = re.sub(r"<[^>]+>", "", part)
            plain_text = plain_text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            result = transport.send_text(chat_id, plain_text, reply_to=prev_msg_id if prev_msg_id else None)
            if result and result.get("ok"):
                _rr = result.get("result", {})
                prev_msg_id = _rr.get("message_id") if isinstance(_rr, dict) else None
        if i < len(formatted_parts) - 1:
            _clock.sleep(DELAY_BRIEF)
    return prev_msg_id


def _send_response_media(
    name: str, images: list[tuple[str | None, str]], files: list[tuple[str | None, str]], chat_id: int
) -> None:
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


def send_response_to_telegram(name: str, text: str, chat_id: int, log_prefix: str = "Response") -> None:
    clean_text, images, files = _parse_response_media(name, text)
    if clean_text and len(clean_text.strip()) <= 5:
        _log(
            _LOG_DEBUG,
            "telegram",
            f"{log_prefix} short msg: {name}, text={clean_text!r}, " f"images={len(images)}, files={len(files)}",
        )
    if clean_text:
        _send_text_via_telegram(name, clean_text, chat_id, log_prefix)
    _send_response_media(name, images, files, chat_id)


def _beast_serve_deploy(html_path: str, slug: str) -> str | None:
    try:
        r = _subprocess_runner.run(
            ["beast", "serve", "deploy", html_path, "--slug", slug, "--output-json"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_GIT_OP,
        )
        if r.returncode == 0:
            data = cast(dict[str, object], json.loads(r.stdout))
            url = str(data.get("url", ""))
            if url and "localhost" in url:
                host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "157.180.48.254"
                url = url.replace("localhost", str(host))
            return url or None
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_WARN, "bridge", f"beast serve deploy failed for {slug}: {e}")
    return None


class _LegacyTransportAdapter(MessageTransport):
    def __init__(self, legacy: MessageTransport) -> None:
        self._legacy = legacy

    @property
    def name(self) -> str:
        return "legacy-adapter"

    def send_text(
        self, chat_id: ChatId, text: str, parse_mode: ParseMode = None, reply_to: MessageId | None = None
    ) -> TelegramApiResponse:
        result = self._legacy.send_message(chat_id, text)
        return result if result else {"ok": True, "result": {"message_id": 1}}

    def set_reaction(self, chat_id: ChatId, message_id: MessageId, reaction: list[dict[str, str]]) -> None:
        if hasattr(self._legacy, "set_reaction"):
            self._legacy.set_reaction(chat_id, message_id, reaction)

    def edit_message(
        self, chat_id: ChatId, message_id: MessageId, text: str, parse_mode: ParseMode = None
    ) -> TelegramApiResponse:
        return {"ok": True, "result": {"message_id": message_id}}


for _m in ("send_photo", "send_document", "send_animation", "send_video", "send_audio", "send_voice", "send_sticker"):
    setattr(_LegacyTransportAdapter, _m, lambda self, *a, **kw: False)
for _m in ("send_chat_action", "setup_commands"):
    setattr(_LegacyTransportAdapter, _m, lambda self, *a, **kw: None)
setattr(_LegacyTransportAdapter, "download_file", lambda self, *a, **kw: None)


def _checkin_can_restart(
    name: str, tmux_name: str, host: str | None, pane_cwd: str, requested_cwd: str
) -> tuple[bool, str]:
    last_restart = watchdog.recent_restarts.get(name, 0)
    elapsed = _clock.time() - last_restart
    if elapsed < RESTART_COOLDOWN:
        if watchdog.force_restart_pending_cwd.pop(name, False):
            _log(_LOG_WARN, "checkin", f"{name}: cooldown bypassed (post-force CWD repair)")
        else:
            _log(_LOG_WARN, "checkin", f"{name}: BLOCKED restart (cooldown {elapsed:.0f}s < {RESTART_COOLDOWN}s)")
            return False, (
                f"Checkin restart blocked: {name} was restarted {elapsed:.0f}s ago "
                f"(cooldown {RESTART_COOLDOWN}s). CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}"
            )
    if is_claude_running(tmux_name, host=host):
        _log(_LOG_INFO, "checkin", f"{name}: BLOCKED restart (Claude already running in tmux)")
        return False, (
            f"Checkin restart skipped: {name} has Claude running. "
            f"CWD mismatch: pane={pane_cwd} vs requested={requested_cwd}"
        )
    with watchdog.restart_lock:
        inflight_ts = watchdog.restart_in_progress.get(name)
        if inflight_ts and _clock.time() - inflight_ts < 120:
            _log(
                _LOG_INFO,
                "checkin",
                f"{name}: BLOCKED restart (in-flight since {_clock.time() - inflight_ts:.0f}s ago)",
            )
            return (
                False,
                f"Checkin restart blocked: {name} restart already in progress ({_clock.time() - inflight_ts:.0f}s).",
            )
        watchdog.restart_in_progress[name] = _clock.time()
    return True, ""


def _checkin_do_restart(
    name: str, backend_name: str, tmux_name: str, host: str | None, requested_cwd: str
) -> tuple[bool, str]:
    notify_chat_id = get_manager_chat_id(name)
    try:
        if notify_chat_id is not None:
            send_telegram_message(
                notify_chat_id,
                f"{name} is restarting in a new directory. " "Messages during restart may be lost.",
            )
        if host:
            restart_backend = get_backend(backend_name)
            ok, err = command_router._restart_remote_worker(
                name, backend_name, restart_backend, tmux_name, host, "relaunch"
            )
        else:
            ok, err = worker_manager.restart(name, mode="relaunch")
        watchdog.recent_restarts[name] = _clock.time()
        _log(_LOG_INFO, "checkin", f"{name}: restart result ok={ok}, err={err}")
        if not ok:
            if notify_chat_id is not None:
                send_telegram_message(
                    notify_chat_id,
                    f"{name} could not restart. " f"Run /restart {name} before sending new messages.",
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


import claudecode  # noqa: E402  # circular

if TYPE_CHECKING:
    from telegram import ChatId


class WorkerManager:
    def __init__(
        self,
        sessions_dir: Path,
        tmux_prefix: str,
        runner: SubprocessRunner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.tmux_prefix = tmux_prefix
        self._runner: SubprocessRunner = runner or core._subprocess_runner
        self._clock: Clock = clock or core._clock

    def _sync_paths(self) -> None:
        if self.sessions_dir != core.SESSIONS_DIR:
            self.sessions_dir = core.SESSIONS_DIR
        if self.tmux_prefix != core.TMUX_PREFIX:
            self.tmux_prefix = core.TMUX_PREFIX

    def _get_startup_cwd(self, name: str, requested_cwd: str = "", fallback_cwd: str = "") -> str:
        candidate = normalize_cwd(requested_cwd)
        if not candidate:
            candidate = normalize_cwd(_get_worker_cwd(name))
        if candidate:
            if os.path.isdir(candidate):
                return candidate
            _log(_LOG_WARN, "bridge", f"Ignoring invalid startup cwd for {name}: {candidate}")
        fallback = normalize_cwd(fallback_cwd)
        if fallback and os.path.isdir(fallback):
            return fallback
        return ""

    def _get_tmux_pane_cwd(self, tmux_name: str, host: str | None = None) -> str:
        result = claudecode._remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_path}"],
            host=host,
            timeout=TIMEOUT_TMUX_CHECK,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return ""

    def _cd_tmux_to_cwd(self, tmux_name: str, cwd: str) -> None:
        if not cwd:
            return
        self._runner.run(
            ["tmux", "send-keys", "-t", tmux_name, f"cd {shlex.quote(cwd)}", "Enter"], timeout=TIMEOUT_TMUX_SEND
        )
        self._clock.sleep(DELAY_SHORT)

    def scan_tmux_sessions(self) -> dict[str, TmuxSessionDict]:
        self._sync_paths()
        registered: dict[str, TmuxSessionDict] = {}
        try:
            result = self._runner.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_TMUX_CHECK,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix) :]
                        backend = normalize_backend(get_tmux_env_value(session_name, "WORKER_BACKEND"))
                        registered[name] = {"tmux": session_name, "backend": backend}
        except (subprocess.SubprocessError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Error scanning local tmux: {e}")
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
                r = claudecode._remote_run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    host=machine.ssh_target,
                    timeout=TIMEOUT_REMOTE_CMD,
                )
                if r.returncode != 0:
                    continue
                for line in r.stdout.strip().split("\n"):
                    if not line:
                        continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix) :]
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

    _sessions_cache = None
    _sessions_cache_ts: float = 0
    _sessions_cache_lock = threading.Lock()
    _SESSIONS_CACHE_TTL = 15

    def invalidate_sessions_cache(self) -> None:
        with self._sessions_cache_lock:
            self._sessions_cache = None
            self._sessions_cache_ts = 0

    def get_registered_sessions(
        self, registered: dict[str, TmuxSessionDict] | None = None
    ) -> dict[str, TmuxSessionDict]:

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
                    if val:
                        entry[key] = str(val)
                if info.get("host") and not info.get("callback_url"):
                    entry["tmux"] = f"{self.tmux_prefix}{name}"
                registered[name] = entry
            else:
                for key in ("protocol", "callback_url", "host", "version"):
                    if info.get(key):
                        registered[name][key] = info.get(key)  # type: ignore[literal-required]
        if state.active and state.active not in registered:
            state.active = None
        if registered and not state.active:
            state.active = next(iter(registered.keys()))
        with self._sessions_cache_lock:
            self._sessions_cache = dict(registered)
            self._sessions_cache_ts = self._clock.time()
        return registered

    def is_online(self, name: str, session: TmuxSessionDict | None = None) -> bool:
        self._sync_paths()
        if not session:
            sessions = self.get_registered_sessions()
            session = sessions.get(name)
        if not session:
            return False
        if session.get("callback_url"):
            return True
        backend_name = normalize_backend(session.get("backend"))
        backend = claudecode.get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        host = get_worker_host(name)
        if host:
            try:
                if not claudecode.tmux_exists(tmux_name, host=host, timeout=TIMEOUT_TMUX_CHECK):
                    return False
                if backend.is_interactive:
                    try:
                        return claudecode.is_claude_running(tmux_name, host=host)
                    except (subprocess.SubprocessError, OSError):
                        return True
                return True
            except (subprocess.SubprocessError, OSError):
                return True
        return backend.is_online(tmux_name)

    def send(
        self, name: str, message: str, chat_id: ChatId | None = None, session: TmuxSessionDict | None = None
    ) -> bool:

        self._sync_paths()
        if not session:
            sessions = self.get_registered_sessions()
            session = sessions.get(name)
        if not session:
            return False
        if session.get("callback_url"):
            return _send_to_callback_worker(name, message, "manager", session)
        backend_name = normalize_backend(session.get("backend"))
        backend = claudecode.get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        return backend.send(name, tmux_name, message, BRIDGE_URL, self.sessions_dir)

    def _pipe_worker_entry(self, name: str, peer_host: str | None, caller_host: str | None) -> WorkerEndpointInfo:
        pipe_path = claudecode.ensure_worker_pipe(name)
        pipe_cmd = f"echo 'YOUR_NAME: your message here' > {pipe_path} &"
        return cast(
            "WorkerEndpointInfo",
            {
                "name": name,
                "machine": peer_host or BRIDGE_SSH_TARGET,
                "protocol": "pipe",
                "address": str(pipe_path),
                "send_example": self._wrap_for_caller(pipe_cmd, peer_host, caller_host),
                "note": "Non-interactive. IMPORTANT: Always prefix your name (e.g., 'kenji: hello'). Always use & (background) when writing to pipe — it BLOCKS until read. Never use cat/echo without & or your session will freeze.",
            },
        )

    def get_workers(self, caller_from: str | None = None) -> list[WorkerEndpointInfo]:
        self._sync_paths()
        workers: list[WorkerEndpointInfo] = []
        registered = self.get_registered_sessions()
        caller_host = get_worker_host(caller_from) if caller_from else None
        for name, info in registered.items():
            callback_url = info.get("callback_url", "")
            if callback_url:
                msg_url = callback_url.rstrip("/")
                if not msg_url.endswith("/msg"):
                    msg_url = f"{msg_url}/msg"
                payload = json.dumps({"from": "YOUR_NAME", "text": "your message here"})
                workers.append(
                    cast(
                        "WorkerEndpointInfo",
                        {
                            "name": name,
                            "machine": info.get("host", "") or BRIDGE_SSH_TARGET,
                            "protocol": "http",
                            "address": msg_url,
                            "send_example": f"curl -sS -X POST {shlex.quote(msg_url)} -H 'Content-Type: application/json' --data-raw {shlex.quote(payload)}",
                            "note": "HTTP callback worker. POST JSON with from/text. Always set from to your worker name.",
                        },
                    )
                )
                continue
            backend_name = claudecode.get_worker_backend(name, info)
            backend = claudecode.get_backend(backend_name)
            peer_host = get_worker_host(name)
            if "tmux" not in info:
                if not backend.is_interactive:
                    workers.append(self._pipe_worker_entry(name, peer_host, caller_host))
                else:
                    workers.append(
                        cast(
                            "WorkerEndpointInfo",
                            {
                                "name": name,
                                "machine": peer_host or BRIDGE_SSH_TARGET,
                                "protocol": "none",
                                "address": "",
                                "status": "exited",
                                "note": f"Worker exited. Use /restart {name} to bring back.",
                            },
                        )
                    )
                continue
            if not backend.is_interactive:
                if peer_host:
                    workers.append(
                        cast(
                            "WorkerEndpointInfo",
                            {
                                "name": name,
                                "machine": peer_host,
                                "protocol": "adapter",
                                "address": f"{peer_host}:{info.get('tmux', '')}",
                                "note": f"Non-interactive ({backend_name}) on {peer_host}. Use @{name} from Telegram or bridge API.",
                            },
                        )
                    )
                else:
                    workers.append(self._pipe_worker_entry(name, peer_host, caller_host))
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
                workers.append(
                    cast(
                        "WorkerEndpointInfo",
                        {
                            "name": name,
                            "machine": peer_host or BRIDGE_SSH_TARGET,
                            "protocol": "tmux",
                            "address": f"{peer_host}:{tmux_name}" if peer_host else tmux_name,
                            "send_example": send_example,
                            "note": note,
                        },
                    )
                )
        return workers

    def _wrap_for_caller(self, cmd: str, peer_host: str | None, caller_host: str | None) -> str:
        if caller_host == peer_host:
            return cmd
        ssh_target = peer_host if peer_host is not None else BRIDGE_SSH_TARGET
        escaped = cmd.replace('"', '\\"')
        return f'ssh {ssh_target} "{escaped}"'

    def _build_welcome(self, name: str, backend_obj: Backend) -> str:
        welcome = (
            "You are connected to Telegram via claudecode-telegram bridge. "
            "RECEIVING FILES: Manager sends files (images, PDFs, documents) — they appear as local paths you can read directly. "
            "SENDING FILES: Use [[image:/path/to/photo.png|caption]] for images (jpg/png/webp/bmp) and animations (gif/mp4), or [[file:/path/to/file|caption]] for documents, video (mp4/mov/avi — shows player), audio (mp3/m4a/flac — shows player), and voice (ogg/opus — voice bubble). "
            f'MESSAGING WORKERS: Run `curl -s "$BRIDGE_URL/workers?from={name}"` to discover other workers — returns JSON with a `send_example` field containing ready-to-use send commands wrapped correctly for your machine (auto-adds ssh when a peer lives elsewhere). Always call /workers?from={name} before messaging, never guess addresses. Never use POST /outputs to message another worker; /outputs only publishes your own worker output to Telegram. '
            f"NAME PREFIX: Always prefix your name in messages (e.g., '{name}: your message'). "
            f"REFRESH INSTRUCTIONS: Run `curl -s $BRIDGE_URL/checkin?name={name}` to re-read these instructions anytime. "
            f'WORKING DIRECTORY: To switch project directory (reloads CLAUDE.md), run `curl -s "$BRIDGE_URL/checkin?name={name}&cwd=/path/to/project"`. '
            "BRIDGE API: Available endpoints: GET /workers, GET /checkin. Messages from manager arrive as prompts — there is NO polling endpoint. "
            "WARNING: Do NOT output worker messages normally — they go to Telegram. Use the send commands from /workers instead."
        )
        if not backend_obj.is_interactive:
            welcome += (
                " NON-INTERACTIVE MODE: Your bridge URL is in $BRIDGE_URL env var. "
                "Each message triggers a blocking CLI call, responses arrive async in Telegram. "
                "Use nohup/& if calling CLI directly."
            )
        note = read_checkin_note()
        if note:
            rendered = note.replace("{name}", name)
            host = get_worker_host(name)
            machine = f"Mac Mini ({host})" if host else "VPS (100.125.36.102)"
            rendered = rendered.replace("{machine}", machine)
            welcome += f"\n\nMANAGER NOTE:\n{rendered}"
            _log(_LOG_INFO, "checkin", f"Checkin note included for {name}")
        return welcome

    def hire(self, name: str, backend: str = "", chat_id: ChatId | None = None) -> tuple[bool, str | None]:
        if not backend:
            backend = DEFAULT_BACKEND
        self._sync_paths()
        if not is_valid_backend(backend):
            return False, f"Unknown backend '{backend}'. Available: {', '.join(list_backends())}"
        backend_obj = claudecode.get_backend(backend)
        if not _which_binary(backend_obj.binary):
            return False, f"'{backend_obj.binary}' not found in PATH. Install it first."
        tmux_name = f"{self.tmux_prefix}{name}"
        if claudecode.tmux_exists(tmux_name):
            return False, f"Worker '{name}' already exists"
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True,
            env=clean_env,
            timeout=TIMEOUT_REMOTE_CMD,
        )
        if result.returncode != 0:
            return False, "Could not start the worker workspace"
        self._runner.run(
            ["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
            capture_output=True,
            timeout=TIMEOUT_TMUX_SEND,
        )
        self._clock.sleep(DELAY_RETRY)
        startup_cwd = self._get_startup_cwd(name)
        if startup_cwd:
            self._cd_tmux_to_cwd(tmux_name, startup_cwd)
        claudecode.export_hook_env(tmux_name, backend)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._runner.run(
            ["tmux", "send-keys", "-t", tmux_name, 'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
            timeout=TIMEOUT_TMUX_SEND,
        )
        self._clock.sleep(DELAY_TMUX_SEND)
        claudecode.ensure_session_dir(name)
        if chat_id:
            chat_id_file = claudecode.get_chat_id_file(name)
            _tmp = chat_id_file.with_suffix(".tmp")
            _tmp.write_text(str(chat_id))
            _tmp.chmod(0o600)
            os.replace(str(_tmp), str(chat_id_file))
        if not backend_obj.is_interactive:
            claudecode.ensure_worker_pipe(name)
        start_cmd = f"unset CLAUDECODE && {backend_obj.start_cmd()}"
        if startup_cwd:
            start_cmd = f"cd {shlex.quote(startup_cwd)} && {start_cmd}"
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        if backend_obj.is_interactive:
            self._clock.sleep(DELAY_STARTUP_LONG)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        if backend_obj.is_interactive:
            self._clock.sleep(DELAY_RESPONSE_GAP)
        welcome = self._build_welcome(name, backend_obj)
        if not backend_obj.is_interactive:
            if chat_id:
                set_pending(name, chat_id)
            self._runner.run(
                ["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND
            )
        else:
            self.send(name, welcome)
        state.active = name

        save_last_active(name)
        _registry_add(name, backend, chat_id)
        _reset_learning_reminder(name)
        if not backend_obj.is_interactive:
            _log(_LOG_INFO, "worker", f"Created {backend} worker '{name}' (non-interactive mode)")
        self.invalidate_sessions_cache()
        return True, None

    def end(self, name: str) -> tuple[bool, str | None]:
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered:
            return False, f"Worker '{name}' not found"
        session = registered[name]
        backend_name = claudecode.get_worker_backend(name, session)
        backend = claudecode.get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        if not backend.is_interactive:
            kill_adapter(name)
            session_dir = self.sessions_dir / name
            try:
                for session_id_file in session_dir.glob("*_session_id"):
                    session_id_file.unlink()
            except OSError as e:
                return False, f"Failed to clean non-interactive metadata: {e}"
        clear_pending(name)
        _set_worker_cwd(name, "")
        host = get_worker_host(name)
        claudecode._remote_run(["tmux", "kill-session", "-t", tmux_name], host=host)
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
        if name not in registered:
            return False, f"Worker '{name}' not found"
        host = get_worker_host(name)
        if host:
            return False, "use_remote_restart"
        session = registered[name]
        backend_name = claudecode.get_worker_backend(name, session)
        backend = claudecode.get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        if not claudecode.tmux_exists(tmux_name):
            return self._restart_dead_worker(name, backend_name, backend, tmux_name, mode)
        if not _which_binary(backend.binary):
            return False, f"'{backend.binary}' not found in PATH. Install it first."
        resume_id, startup_cwd = self._prepare_restart_state(name, mode)
        if mode != "resume":
            _clear_hook_failures(name)
        session_dir = self.sessions_dir / name
        if not backend.is_interactive:
            session_dir.mkdir(parents=True, exist_ok=True)
            claudecode.ensure_worker_pipe(name)
            clear_pending(name)
        elif claudecode.is_claude_running(tmux_name):
            self._stop_running_claude(name, tmux_name)
        if backend.is_interactive and not claudecode.is_claude_running(tmux_name):
            self._kill_stray_children(name, tmux_name)
        claudecode.export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._send_start_command(name, tmux_name, backend, resume_id, startup_cwd)
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = self._wait_for_startup(name, tmux_name, backend, resume_id, startup_cwd)
            if started:
                self.send(name, welcome)
            else:
                _log(_LOG_WARN, "restart", f"{name}: Claude did not start within 10s, skipping welcome")
        else:
            self._runner.run(
                ["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND
            )
        _reset_learning_reminder(name)
        self.invalidate_sessions_cache()
        return True, None

    def _prepare_restart_state(self, name: str, mode: str) -> tuple[str, str]:
        resume_id = ""
        resume_cwd = ""
        session_dir = self.sessions_dir / name
        if mode == "resume":
            resume_id = (
                claudecode.get_claude_session_id(name, authoritative=False)
                or claudecode.get_claude_session_id(name, authoritative=True)
                or ""
            )
            resume_cwd = ""
        else:
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"):
                session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd:
            claudecode._ensure_workspace_trusted(startup_cwd)
        return resume_id, startup_cwd

    def _stop_running_claude(self, name: str, tmux_name: str) -> None:
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_STARTUP)
        if claudecode.is_claude_running(tmux_name):
            pane_pid = claudecode._tmux_pane_pids().get(tmux_name)
            if pane_pid:
                claude_pid = _get_claude_pid(pane_pid)
                if claude_pid:
                    self._runner.run(["kill", claude_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        for _ in range(20):
            if not claudecode.is_claude_running(tmux_name):
                break
            self._clock.sleep(DELAY_SHORT)
        else:
            _log(_LOG_WARN, "restart", f"{name}: Claude still running after 5s kill wait")

    def _kill_stray_children(self, name: str, tmux_name: str) -> None:
        pane_pids = claudecode._tmux_pane_pids()
        pane_pid = pane_pids.get(tmux_name)
        if pane_pid:
            stray = self._runner.run(
                ["pgrep", "-P", str(pane_pid)], capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
            )
            if stray.returncode == 0:
                for child_pid in stray.stdout.strip().splitlines():
                    child_pid = child_pid.strip()
                    if child_pid and child_pid.isdigit():
                        _log(_LOG_INFO, "restart", f"{name}: killing stray child pid {child_pid}")
                        self._runner.run(["kill", child_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
                self._clock.sleep(DELAY_RETRY)

    def _send_start_command(
        self, name: str, tmux_name: str, backend: Backend, resume_id: str, startup_cwd: str
    ) -> None:
        self._runner.run(
            ["tmux", "send-keys", "-t", tmux_name, 'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
            timeout=TIMEOUT_TMUX_SEND,
        )
        self._clock.sleep(DELAY_TMUX_SEND)
        start_cmd = backend.start_cmd(resume_id)
        start_cmd = f"unset CLAUDECODE && {start_cmd}"
        if startup_cwd:
            start_cmd = f"cd {shlex.quote(startup_cwd)} && {start_cmd}"
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)

    def _wait_for_startup(self, name: str, tmux_name: str, backend: Backend, resume_id: str, startup_cwd: str) -> bool:
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if claudecode.is_claude_running(tmux_name):
                started = True
                break
        if not started and resume_id:
            started = self._retry_after_stale_resume(name, tmux_name, backend, resume_id, startup_cwd)
        return started

    def _retry_after_stale_resume(
        self, name: str, tmux_name: str, backend: Backend, resume_id: str, startup_cwd: str
    ) -> bool:

        _log(_LOG_WARN, "restart", f"{name}: resume failed (stale session {resume_id[:8]}), auto-retrying fresh")
        clear_claude_session_id(name)
        _clear_hook_failures(name)
        start_cmd = backend.start_cmd("")
        start_cmd = f"unset CLAUDECODE && {start_cmd}"
        if startup_cwd:
            start_cmd = f"cd {shlex.quote(startup_cwd)} && {start_cmd}"
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if claudecode.is_claude_running(tmux_name):
                started = True
                break
        if started:
            _log(_LOG_INFO, "restart", f"{name}: fresh start succeeded after stale resume")
            _notify_admin(f"⚠️ {name}: stale session ID {resume_id[:8]}… — auto-restarted fresh ✓")
        else:
            _log(_LOG_WARN, "restart", f"{name}: fresh start also failed after stale resume")
            _notify_admin(
                f"🔴 {name}: resume failed (stale {resume_id[:8]}…) AND fresh start failed.\n/restart --clean {name} to try manually."
            )
        return started

    def _restart_dead_worker(
        self, name: str, backend_name: str, backend: Backend, tmux_name: str, mode: str
    ) -> tuple[bool, str | None]:

        if not _which_binary(backend.binary):
            return False, f"'{backend.binary}' not found in PATH. Install it first."
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True,
            env=clean_env,
            timeout=TIMEOUT_REMOTE_CMD,
        )
        if result.returncode != 0:
            return False, "Could not create worker workspace"
        self._runner.run(
            ["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
            capture_output=True,
            timeout=TIMEOUT_TMUX_SEND,
        )
        self._clock.sleep(DELAY_RETRY)
        claudecode.ensure_session_dir(name)
        if not backend.is_interactive:
            claudecode.ensure_worker_pipe(name)
        resume_id, startup_cwd = self._prepare_restart_state(name, mode)
        claudecode.export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._send_start_command(name, tmux_name, backend, resume_id, startup_cwd)
        if backend.is_interactive:
            self._clock.sleep(DELAY_STARTUP_LONG)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = self._wait_for_startup(name, tmux_name, backend, resume_id, startup_cwd)
            if started:
                self.send(name, welcome)
            else:
                _log(_LOG_WARN, "restart", f"{name}: dead worker did not start within 10s, skipping welcome")
        else:
            self._runner.run(
                ["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND
            )
        _log(_LOG_INFO, "worker", f"Dead worker '{name}' recovered from registry (mode={mode})")
        self.invalidate_sessions_cache()
        return True, None


def cmd_pilot(router: CommandRouter, name: str, chat_id: int) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /pilot <name> [name2 ...]", outcome="Needs decision")
        return True
    names = name.lower().strip().split()
    prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
    if "all" in names:
        assert worker_manager is not None
        registered = worker_manager.scan_tmux_sessions()
        registry = _load_registry()
        for rname, rinfo in registry.get("workers", {}).items():
            if rinfo.get("host") and rname not in registered:
                registered[rname] = {"tmux": f"{prefix}{rname}", "host": rinfo["host"]}
        if not registered:
            router.reply(chat_id, "No active workers found", outcome="Needs decision")
            return True
        names = sorted(registered.keys())

    # Build session list: "tmux_name[@host]" for --sessions arg
    session_entries: list[str] = []
    display_names: list[str] = []
    errors: list[str] = []
    for n in names:
        session_name = f"{prefix}{n}" if not n.startswith("claude-") else n
        worker_host = get_worker_host(n)
        entry = f"{session_name}@{worker_host}" if worker_host else session_name
        session_entries.append(entry)
        display_names.append(n)

    if not session_entries:
        router.reply(chat_id, "No workers specified", outcome="Needs decision")
        return True

    # Spawn pilot via ToolManager (Node.js — Bun's node-pty can't attach to tmux)
    bridge_dir = os.path.dirname(os.path.abspath(__file__))
    pilot_script = os.path.join(bridge_dir, "pilot", "pilot.js")

    slug_name = "-".join(display_names[:3]) if len(display_names) <= 3 else f"team{len(display_names)}"
    result = tool_manager.spawn(
        "pilot",
        slug_name,
        ttl=1800,
        extra_args=["--sessions", ",".join(session_entries)],
        runtime="node",
        script_dir=pilot_script,
    )
    if not result:
        router.reply(chat_id, "Pilot error: no available ports or spawn failed", outcome="Needs decision")
        return True

    url, _token = result
    # For multi-worker, create a grid session via the pilot's API
    if len(session_entries) > 1:
        ts = time.strftime("%m%d-%H%M", time.gmtime(core._clock.time()))
        slug = "-".join(display_names) + "-" + ts if len(display_names) <= 3 else f"team{len(display_names)}-{ts}"
        session_names = [e.split("@")[0] for e in session_entries]
        # Parse port from the spawned URL
        pilot_port = urlparse(url).port or 10171
        with _log_suppress(urllib.error.URLError, OSError, TimeoutError, level=_LOG_DEBUG, tag="notify:unknown"):
            payload = json.dumps({"slug": slug, "sessions": session_names, "ttl": 1800}).encode()
            req = urllib.request.Request(
                f"http://localhost:{pilot_port}/api/grid-session?token={_token}",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with core._urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp:
                cast(dict[str, object], json.loads(resp.read()))
        host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "localhost"
        pilot_url = f"http://{host}:{pilot_port}/grid/{_urlquote(slug)}?token={_token}"
    else:
        pilot_url = url

    names_str = ", ".join(display_names)
    msg = f"✈️ Pilot: {names_str} (30min)\n{pilot_url}"
    if errors:
        msg += f"\n⚠️ Failed: {'; '.join(errors)}"
    router.reply(chat_id, msg)
    return True


def cmd_rewind(router: CommandRouter, name: str, chat_id: int) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /rewind <name>", outcome="Needs decision")
        return True
    name = name.lower().strip()

    # Resolve JSONL path (local or remote)
    host = get_worker_host(name)
    if host:
        cwd = get_claude_session_cwd(name) or ""
        sid = get_claude_session_id(name, authoritative=True)
        if not sid:
            router.reply(chat_id, f"No session found for {name}", outcome="Needs decision")
            return True
        remote_home = _get_remote_home(host) or ""
        if not remote_home:
            router.reply(chat_id, f"Cannot resolve remote home for {name}", outcome="Needs decision")
            return True
        remote_cwd = _remap_path(cwd, host)
        remote_slug = _project_slug(remote_cwd)
        remote_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
        local_tmp = Path(f"/tmp/transcript-{name}-{sid}.jsonl")
        sync_key = f"{name}:{sid}"
        sync_info = _transcript_sync.get(sync_key)
        if sync_info and sync_info["status"] == "done" and local_tmp.exists():
            jsonl_path = str(local_tmp)
        elif sync_info and sync_info["status"] == "syncing":
            router.reply(chat_id, f"⏪ Syncing transcript for {name}… try again in a few seconds")
            return True
        else:
            _task_pool.submit(_start_transcript_sync, name, host, remote_path, local_tmp, sync_key)
            router.reply(chat_id, f"⏪ Syncing transcript for {name}… try again in a few seconds")
            return True
    else:
        transcript_path, sid, _cwd = _resolve_transcript_path(name)
        if not sid:
            router.reply(chat_id, f"No session found for {name}", outcome="Needs decision")
            return True
        if not transcript_path or transcript_path == "syncing":
            router.reply(chat_id, f"⏪ Syncing transcript for {name}… try again in a few seconds")
            return True
        jsonl_path = str(transcript_path)

    # Spawn Bun rewind viewer
    result = tool_manager.spawn(
        "rewind",
        name,
        ttl=300,
        extra_args=["--jsonl-path", jsonl_path, "--name", name, "--session-id", sid or "unknown"],
    )
    if not result:
        router.reply(chat_id, "⏪ Rewind failed: no available tool ports", outcome="Needs decision")
        return True
    url, _token = result
    router.reply(chat_id, f"⏪ Rewind for {name} (5 min)\n{url}")
    return True


def cmd_pr_review(router: CommandRouter, arg: str, chat_id: int) -> bool:
    if not arg:
        router.reply(
            chat_id,
            "Usage: /pr <github_pr_url>\nExample: /pr https://github.com/BasedHardware/omi/pull/6426",
            outcome="Needs decision",
        )
        return True
    arg = arg.strip()
    clean_url = arg.split("#")[0]
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", clean_url)
    if not m:
        try:
            pr_num = int(clean_url)
            owner, repo = "BasedHardware", "omi"
        except ValueError:
            router.reply(
                chat_id,
                "Invalid PR URL. Example: /pr https://github.com/BasedHardware/omi/pull/6426",
                outcome="Needs decision",
            )
            return True
    else:
        owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
    router.reply(chat_id, f"Generating PR review for {owner}/{repo}#{pr_num}...")

    # Generate HTML via standalone script
    bridge_dir = os.path.dirname(os.path.abspath(__file__))
    generate_script = os.path.join(bridge_dir, "review", "generate.py")
    out_path = f"/tmp/pr-review-{pr_num}.html"
    try:
        r = core._subprocess_runner.run(
            [sys.executable, generate_script, arg],
            capture_output=True,
            text=True,
            timeout=PENDING_TIMEOUT,
        )
        if r.returncode != 0 or not os.path.exists(out_path):
            router.reply(chat_id, f"Failed to generate PR review:\n{r.stderr[:500]}", outcome="Needs decision")
            return True
    except subprocess.TimeoutExpired:
        router.reply(chat_id, "PR review generation timed out (>300s).", outcome="Needs decision")
        return True

    # Try beast serve deploy first (external CDN hosting)
    slug = f"pr-{pr_num}"
    serve_url = _beast_serve_deploy(out_path, slug)
    if serve_url:
        router.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{serve_url}")
        return True

    # Fallback: spawn Bun review server via ToolManager
    serve_script = os.path.join(bridge_dir, "review", "src", "serve.ts")
    result = tool_manager.spawn(
        "review",
        slug,
        ttl=300,
        extra_args=[
            "--html-path",
            out_path,
            "--owner",
            owner,
            "--repo",
            repo,
            "--pr-num",
            str(pr_num),
        ],
        runtime="bun",
        script_dir=serve_script,
    )
    if result:
        url, _token = result
        router.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{url}")
    else:
        router.reply(
            chat_id,
            f"PR #{pr_num}: generated at {out_path} but no ports available to serve it",
            outcome="Needs decision",
        )
    return True


def cmd_team(router: CommandRouter, chat_id: int) -> bool:
    registered = router.workers.scan_tmux_sessions()
    registered = router.workers.get_registered_sessions(registered)
    if not registered:
        router.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
        return True
    worker_live = {}
    for name, session in registered.items():
        backend_name = claudecode.get_worker_backend(name, session)
        activity = None
        context_pct = None
        tmux_name = session.get("tmux", f"{router.workers.tmux_prefix}{name}")
        host = get_worker_host(name)
        tmux_alive = "tmux" in session and claudecode.tmux_exists(tmux_name, host=host)
        if tmux_alive:
            backend = claudecode.get_backend(backend_name)
            if backend.is_interactive:
                if claudecode.is_claude_running(tmux_name, host=host):
                    activity, context_pct, _ = _read_tmux_activity(tmux_name, host=host)
                else:
                    activity = "worker app not running"
            else:
                activity = _read_noninteractive_activity(name)
        worker_live[name] = {"backend": backend_name, "activity": activity, "context_pct": context_pct}
    lines = format_team_lines(registered, state.active, worker_live=worker_live)
    router.reply(chat_id, "\n".join(lines))
    return True


def cmd_status(router: CommandRouter, chat_id: int) -> bool:
    registered = router.workers.get_registered_sessions()
    try:
        uptime_sec = int(time.time() - os.stat(f"/proc/{os.getpid()}").st_mtime)
        d, rem = divmod(uptime_sec, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        uptime_str = f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m" if h > 0 else f"{m}m"
    except (OSError, ValueError):
        uptime_str = "unknown"
    tm_status = tunnel_manager.status() if tunnel_manager else {}
    tunnel_url = tm_status.get("tunnel_url", "")
    inbound = f"webhook ({tunnel_url})" if tunnel_url else "poll" if tm_status.get("polling_active", False) else "DOWN"
    busy = 0
    idle = 0
    offline = 0
    busy_names = []
    for name, session in registered.items():
        if router.workers.is_online(name, session):
            wd = watchdog.worker_states.get(name)
            if wd and wd.status == "BUSY":
                busy += 1
                busy_names.append(name)
            else:
                idle += 1
        else:
            offline += 1
    total = busy + idle + offline
    machine_workers = {}
    ssh_to_display = {None: "local"}
    for mach in get_machine_catalog().values():
        if mach.ssh_target:
            ssh_to_display[mach.ssh_target] = mach.display_name or mach.id
    for name in registered:
        host = get_worker_host(name)
        machine_workers.setdefault(ssh_to_display.get(host, host or "local"), []).append(name)
    conn_parts = [
        f"{cn} ({info.get('consecutive_failures', 0)} err)" if info.get("consecutive_failures", 0) else cn
        for cn, info in _get_connectors_status().items()
        if info.get("running", False)
    ]
    lines = [
        f"<b>v{VERSION}</b> | up {uptime_str} | {inbound}",
        "",
        f"<b>{total}</b> workers: {busy} busy, {idle} idle, {offline} off",
        f"focus: <b>{state.active or 'none'}</b>",
    ]
    if busy_names:
        lines.append(f"busy: {', '.join(sorted(busy_names))}")
    if len(machine_workers) > 1:
        lines.append(" / ".join(f"{label} {len(ws)}" for label, ws in sorted(machine_workers.items())))
    if conn_parts:
        lines.append(", ".join(conn_parts))
    if router.transport is not None and chat_id is not None:
        router.transport.send_text(chat_id, "\n".join(lines), parse_mode="HTML")
    return True


def cmd_focus(router: CommandRouter, name: str, chat_id: int) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /focus <name>", outcome="Needs decision")
        return True
    name = name.lower().strip()

    ok, err = switch_session(name)
    if ok:
        router.reply(chat_id, f"Now talking to {name.capitalize()}.")
    else:
        router.reply(chat_id, f'Could not focus "{name}". {err}', outcome="Needs decision")
    return True


def cmd_hire(router: CommandRouter, name: str, chat_id: int) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
        return True

    parsed_name, backend = parse_hire_args(name)
    if not parsed_name:
        router.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision")
        return True
    name = parsed_name.lower().strip()
    name = re.sub(r"[^a-z0-9-]", "", name)
    if not name:
        router.reply(chat_id, "Name must use letters, numbers, and hyphens only.", outcome="Needs decision")
        return True
    if name in RESERVED_NAMES:
        router.reply(chat_id, f'Cannot use "{name}" - reserved command. Choose another name.', outcome="Needs decision")
        return True
    ok, err = _wm().hire(name, backend, chat_id=chat_id)
    if ok:
        router.reply(chat_id, f"{name.capitalize()} is added and assigned. {PERSISTENCE_NOTE}")
        update_bot_commands()
    else:
        router.reply(chat_id, f'Could not hire "{name}". {err}', outcome="Needs decision")
    return True


def cmd_end(router: CommandRouter, name: str, chat_id: int) -> bool:
    if not name:
        router.reply(chat_id, "This is permanent. Usage: /end <name>", outcome="Needs decision")
        return True
    name = name.lower().strip()

    ok, err = kill_session(name)
    if ok:
        router.reply(chat_id, f"{name.capitalize()} removed from your team.")
        update_bot_commands()
    else:
        router.reply(chat_id, f'Could not remove "{name}". {err}', outcome="Needs decision")
    return True


def cmd_restart(router: CommandRouter, chat_id: int, args: str = "") -> bool:
    args = (args or "").strip()
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
    if name_arg == "cancel":
        return _cmd_restart_cancel(router, chat_id)
    if name_arg == "all":
        return _cmd_restart_all(router, chat_id, clean)
    if len(remaining) > 1:
        names = [n.lower() for n in remaining]
        router.reply(chat_id, f"Restarting {len(names)} workers: {', '.join(names)}...")
        for n in names:
            cmd_restart(router, chat_id, f"{'--clean ' if clean else ''}{'--force ' if force else ''}{n}")
        return True
    if name_arg:
        name = name_arg
    else:
        if not state.active:
            registered = router.workers.get_registered_sessions()
            if len(registered) == 1:
                name = next(iter(registered))
                state.active = name
                save_last_active(name)
            else:
                router.reply(chat_id, "No one assigned.")
                return True
        else:
            name = state.active
    registered = router.workers.get_registered_sessions()
    session = registered.get(name)
    if name not in registered:
        if registered:
            names_str = ", ".join(registered.keys())
            router.reply(chat_id, f'Can\'t find "{name}". Available workers: {names_str}')
        else:
            router.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
        return True
    if name_arg:
        state.active = name
        save_last_active(name)
    host = get_worker_host(name)
    tmux_name = (
        session.get("tmux", f"{router.workers.tmux_prefix}{name}") if session else f"{router.workers.tmux_prefix}{name}"
    )
    _log(_LOG_INFO, "cmd_restart", f"{name}: force={force}, clean={clean}, host={host}, tmux={tmux_name}")
    tmux_alive = claudecode.tmux_exists(tmux_name, host=host)
    claude_running = claudecode.is_claude_running(tmux_name, host=host) if tmux_alive else False
    _log(_LOG_INFO, "cmd_restart", f"{name}: tmux_alive={tmux_alive}, claude_running={claude_running}")
    if not force and tmux_alive and claude_running:
        _log(_LOG_INFO, "cmd_restart", f"{name}: BLOCKED (already running)")
        router.reply(chat_id, f"{name.capitalize()} is already running. Use /restart --force {name} to force.")
        return True
    with watchdog.restart_lock:
        inflight_ts = watchdog.restart_in_progress.get(name)
        if inflight_ts and core._clock.time() - inflight_ts < 120:
            _log(
                _LOG_INFO,
                "cmd_restart",
                f"{name}: BLOCKED (restart in progress since {core._clock.time() - inflight_ts:.0f}s ago)",
            )
            router.reply(chat_id, f"{name.capitalize()} restart already in progress. Wait for it to finish.")
            return True
        watchdog.restart_in_progress[name] = core._clock.time()
    try:
        result = _do_restart(router, name, session, chat_id, host, tmux_name, force, clean)
        if force:
            watchdog.force_restart_pending_cwd[name] = True
        return result
    finally:
        with watchdog.restart_lock:
            watchdog.restart_in_progress.pop(name, None)


def _do_restart(
    router: CommandRouter,
    name: str,
    session: dict[str, object],
    chat_id: int,
    host: str | None,
    tmux_name: str,
    force: bool,
    clean: bool,
) -> bool:
    if host:
        mode = "relaunch" if clean else "resume"
        backend_name = claudecode.get_worker_backend(name, session) if session else DEFAULT_BACKEND
        backend_obj = claudecode.get_backend(backend_name)
        _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart mode={mode}, host={host}")
        router.reply(chat_id, f"Restarting {name.capitalize()} on remote host...")

        ok, err = router._restart_remote_worker(name, backend_name, backend_obj, tmux_name, host, mode)
        watchdog.recent_restarts[name] = core._clock.time()
        _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart result ok={ok}, err={err}")
        return _restart_reply(router, chat_id, name, ok, err, host)
    backend_name = claudecode.get_worker_backend(name, session) if session else DEFAULT_BACKEND
    backend = claudecode.get_backend(backend_name)
    tmux_name = (
        session.get("tmux", f"{router.workers.tmux_prefix}{name}") if session else f"{router.workers.tmux_prefix}{name}"
    )
    if not clean and not backend.is_interactive and session and "tmux" in session and claudecode.tmux_exists(tmux_name):
        session_id, _source = claudecode.get_any_session_id(name)
        if session_id:
            router.reply(chat_id, f"{name.capitalize()} is still active. Next message continues where you left off.")
        else:
            router.reply(chat_id, f"No active session for {name.capitalize()}. Next message starts fresh.")
        return True
    mode = "relaunch" if clean else "resume"
    if mode == "resume":
        session_dir = claudecode.get_session_dir(name)
        if not session_dir.exists() or not any(session_dir.glob("*_session_id")):
            mode = "relaunch"
    label = "Resuming" if mode == "resume" else ("Bringing" if clean else "Restarting")
    router.reply(chat_id, f"{label} {name.capitalize()}...")
    ok, err = restart_claude(name, mode=mode)
    if ok:

        watchdog.recent_restarts[name] = core._clock.time()
    return _restart_reply(router, chat_id, name, ok, err)


def _restart_reply(router: CommandRouter, chat_id: int, name: str, ok: bool, err: str, host: str | None = None) -> bool:
    if ok:
        router.reply(chat_id, f"{name.capitalize()} is back and ready.")
    else:
        loc = f" on {host}" if host else ""
        router.reply(chat_id, f'Could not restart "{name}"{loc}. {err}', outcome="Needs decision")
    return True


def _cmd_restart_all(router: CommandRouter, chat_id: int, clean: bool) -> bool:
    registered = router.workers.get_registered_sessions()
    if not registered:
        router.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
        return True
    with router._restart_all_lock:
        if router._restart_all_running:
            router.reply(chat_id, "A /restart all is already running. Use /restart cancel to stop it.")
            return True
        router._restart_all_running = True
        router._restart_all_abort.clear()
    names = sorted(registered.keys())
    active = state.active
    if active and active in names:
        names.remove(active)
        names.append(active)
    mode = "relaunch" if clean else "resume"
    router.reply(chat_id, f"Restarting {len(names)} workers sequentially ({mode})...")
    router._restart_all_thread = threading.Thread(
        target=_run_restart_all_sequence, args=(router, chat_id, names, mode), name="restart-all", daemon=True
    )
    router._restart_all_thread.start()
    return True


def _run_restart_all_sequence(router: CommandRouter, chat_id: int, names: list[str], mode: str) -> None:
    delay_s = 7
    failed = []
    try:
        total = len(names)
        for i, name in enumerate(names, 1):
            if router._restart_all_abort.is_set():
                router.reply(chat_id, f"Restart sequence aborted at {i-1}/{total}.")
                return
            host = get_worker_host(name)
            if host:
                reg = _wm().get_registered_sessions()
                session = reg.get(name, {})
                backend_name = claudecode.get_worker_backend(name, session)
                backend_obj = claudecode.get_backend(backend_name)
                tmux_name = session.get("tmux", f"{router.workers.tmux_prefix}{name}")

                ok, err = restart_remote_worker(name, backend_name, backend_obj, tmux_name, host, mode, router.workers)
            else:
                ok, err = restart_claude(name, mode=mode)
            if ok:
                router.reply(chat_id, f"[{i}/{total}] {name.capitalize()} restarted.")
            else:
                failed.append((name, err))
                router.reply(chat_id, f"[{i}/{total}] {name.capitalize()} failed: {err}")
            if i < total:
                for _ in range(5):
                    if router._restart_all_abort.is_set():
                        break
                    core._clock.sleep(delay_s / 5)
        if failed:
            summary = ", ".join(n for n, _ in failed)
            router.reply(chat_id, f"Restart all done. {len(failed)} failed: {summary}")
        else:
            router.reply(chat_id, f"Restart all done. All {total} workers restarted.")
    finally:
        with router._restart_all_lock:
            router._restart_all_running = False
            router._restart_all_abort.clear()
            router._restart_all_thread = None


def _cmd_restart_cancel(router: CommandRouter, chat_id: int) -> bool:
    with router._restart_all_lock:
        if not router._restart_all_running:
            router.reply(chat_id, "No restart-all sequence is running.")
            return True
        router._restart_all_abort.set()
    router.reply(chat_id, "Stopping restart-all sequence...")
    return True


# _get_claude_pid and get_worker_host — defined earlier (from _health_impl)


def _ensure_bare_repo(project_name: str) -> str:
    bare_path = os.path.join(GIT_SERVER_DIR, f"{project_name}.git")
    if not os.path.isdir(bare_path):
        os.makedirs(GIT_SERVER_DIR, exist_ok=True)
        _subprocess_runner.run(
            ["git", "init", "--bare", bare_path], capture_output=True, text=True, check=True, timeout=10
        )
    return bare_path


def _bare_repo_url(bare_repo_path: str, target_host: str | None = None) -> str:
    if target_host:
        return f"claude@100.125.36.102:{bare_repo_path}"
    return bare_repo_path


def _gitcmd(
    cwd: str, *args: str, host: str | None = None, timeout: int = TIMEOUT_GIT_OP
) -> subprocess.CompletedProcess[str]:
    """Run a git -C <cwd> <args> command, returning the CompletedProcess."""
    return _remote_run(["git", "-C", cwd, *args], host=host, timeout=timeout)


def _git_push_state(
    source_cwd: str, worker_name: str, bare_repo: str, host: str | None = None
) -> GitPushStateResult | None:
    def git(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        return _gitcmd(source_cwd, *a, host=host, **kw)

    try:
        r = git("rev-parse", "HEAD", timeout=TIMEOUT_FILE_TRANSFER)
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"rev-parse HEAD failed: {r.stderr[:200]}")
            return None
        orig_sha = r.stdout.strip()
        r = git("rev-parse", "--abbrev-ref", "HEAD", timeout=TIMEOUT_REMOTE_CMD)
        orig_branch = r.stdout.strip() if r.returncode == 0 else "HEAD"
        r = git("diff", "--cached", "--name-only", timeout=TIMEOUT_FILE_TRANSFER)
        staged_files = [f for f in r.stdout.strip().split("\n") if f] if r.returncode == 0 else []
        git("add", "-A")
        try:
            r = git("stash", "create")
            stash_sha = r.stdout.strip() if r.returncode == 0 else ""
        finally:
            git("reset", "HEAD", timeout=TIMEOUT_FILE_TRANSFER)
            if staged_files:
                git("add", "--", *staged_files, timeout=TIMEOUT_FILE_TRANSFER)
        push_sha = stash_sha or orig_sha
        ref = f"refs/heads/teleport/{worker_name}"
        repo_url = _bare_repo_url(bare_repo, host)
        r = git("push", "--force", repo_url, f"{push_sha}:{ref}", timeout=TIMEOUT_LARGE_TRANSFER)
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


def _git_pull_state(
    target_cwd: str, worker_name: str, bare_repo_url: str, metadata: GitPushStateResult, host: str | None = None
) -> bool:
    def git(*a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        return _gitcmd(target_cwd, *a, host=host, **kw)

    try:
        orig_sha, orig_branch = metadata["orig_sha"], metadata["orig_branch"]
        staged_files = metadata.get("staged_files", [])
        stash_sha = metadata.get("stash_sha")
        ref = f"teleport/{worker_name}"
        is_existing = False
        with _log_suppress(subprocess.SubprocessError, OSError, level=_LOG_DEBUG, tag="probe:_git_pull_state"):
            is_existing = git("rev-parse", "--git-dir", timeout=TIMEOUT_REMOTE_CMD).returncode == 0
        if not is_existing:
            r = _remote_run(
                ["git", "clone", "--no-checkout", bare_repo_url, target_cwd], host=host, timeout=TIMEOUT_RSYNC
            )
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"clone failed: {r.stderr[:200]}")
                return False
            git("config", "user.email", "teleport@bridge")
            git("config", "user.name", "teleport")
        else:
            git("remote", "remove", "vps")
            git("remote", "add", "vps", bare_repo_url, timeout=TIMEOUT_REMOTE_CMD)
            r = git("fetch", "vps", ref, timeout=TIMEOUT_RSYNC)
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"fetch failed: {r.stderr[:200]}")
                return False
        checkout_args = ("-B", orig_branch, orig_sha) if orig_branch and orig_branch != "HEAD" else (orig_sha,)
        git("checkout", *checkout_args)
        if stash_sha:
            fetch_ref = f"vps/{ref}" if is_existing else f"origin/{ref}"
            r = git("stash", "apply", fetch_ref)
            if r.returncode != 0:
                git("read-tree", "-u", "--reset", orig_sha, timeout=TIMEOUT_FILE_TRANSFER)
                git("cherry-pick", "--no-commit", fetch_ref)
            if staged_files:
                git("reset", "HEAD", timeout=TIMEOUT_FILE_TRANSFER)
                git("add", "--", *staged_files, timeout=TIMEOUT_FILE_TRANSFER)
        return True
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"pull state error: {e}")
        return False


def _get_project_name(cwd: str, host: str | None = None) -> str | None:
    try:
        r = _remote_run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            host=host,
            timeout=TIMEOUT_REMOTE_CMD,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        url = r.stdout.strip()
        if url.endswith(".git"):
            url = url[:-4]
        if ":" in url and not url.startswith("http"):
            name = url.rsplit("/", 1)[-1] if "/" in url.split(":")[-1] else url.split(":")[-1]
        else:
            name = url.rsplit("/", 1)[-1]
        return name if name else None
    except (subprocess.SubprocessError, OSError):
        return None


def _ensure_workspace_trusted_remote(
    cwd: str | None,
    host: str | None,
) -> None:

    if not cwd:
        return
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
        "print('trusted' if changed else 'already')"
    )
    with _log_suppress(OSError, TimeoutError, tag="trust", msg=f"remote pre-trust {cwd} on {host}: "):
        r = _remote_run(["python3", "-c", script], host=host, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode == 0:
            _log(_LOG_INFO, "trust", f"remote pre-trust {cwd} on {host}: {r.stdout.strip()}")
        else:
            _log(_LOG_WARN, "trust", f"remote pre-trust failed on {host}: {r.stderr[:200]}")


def _build_teleport_context(
    name: str,
    source_host: str | None,
    target_host: str,
    source_cwd: str | None,
    session_id: str | None,
) -> str:
    src_label = source_host or "VPS (local)"
    lines = [
        f"📦 You were teleported from {src_label} to {target_host}.",
        f"Previous workspace: {source_cwd}",
    ]
    if session_id:
        lines.append(f"Previous session: {session_id}")
        lines.append("")
        lines.append(
            "To retrieve your previous work context, run:\n"
            f"  beast transcript search --session {session_id} --last 20 --full"
        )
    else:
        lines.append("No previous session was active.")
    return "\n".join(lines)


def teleport_notify(chat_id: ChatId | None, text: str) -> None:
    if chat_id is None:
        return
    with _log_suppress(urllib.error.URLError, OSError, TimeoutError, level=_LOG_DEBUG, tag="notify:_teleport_notify"):
        transport.send_text(chat_id, text)


def check_teleback_conflicts(name: str, remote_host: str | None, local_cwd: str | None) -> list[str]:
    conflicts: list[str] = []
    if not local_cwd or not remote_host:
        return conflicts
    local_is_git = os.path.isdir(os.path.join(local_cwd, ".git"))
    if not local_is_git:
        return conflicts
    local_status = _subprocess_runner.run(
        ["git", "-C", local_cwd, "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_REMOTE_CMD,
    )
    local_changed = bool(local_status.stdout.strip()) if local_status.returncode == 0 else False
    local_head = _subprocess_runner.run(
        ["git", "-C", local_cwd, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND
    )
    local_commit = local_head.stdout.strip() if local_head.returncode == 0 else ""
    remote_cwd = _remap_path(local_cwd, remote_host)
    r_status = _remote_run(
        ["git", "-C", remote_cwd, "status", "--porcelain"],
        host=remote_host,
        timeout=TIMEOUT_REMOTE_CMD,
    )
    remote_changed = bool(r_status.stdout.strip()) if r_status.returncode == 0 else False
    r_head = _remote_run(
        ["git", "-C", remote_cwd, "rev-parse", "HEAD"],
        host=remote_host,
        timeout=TIMEOUT_TMUX_SEND,
    )
    remote_commit = r_head.stdout.strip() if r_head.returncode == 0 else ""
    if local_changed and remote_changed:
        local_files = [l.strip().split(None, 1)[-1] for l in local_status.stdout.strip().splitlines()[:5]]
        remote_files = [l.strip().split(None, 1)[-1] for l in r_status.stdout.strip().splitlines()[:5]]
        conflicts.append(f"VPS has uncommitted changes: {', '.join(local_files)}")
        conflicts.append(f"Remote has uncommitted changes: {', '.join(remote_files)}")
    elif local_commit and remote_commit and local_commit != remote_commit:
        if local_changed:
            conflicts.append(f"VPS has uncommitted changes AND different commit than remote")
        elif remote_changed:
            conflicts.append(f"VPS has new commits since teleport (HEAD: {local_commit[:8]})")
            conflicts.append(f"Remote has uncommitted changes (HEAD: {remote_commit[:8]})")
    return conflicts


def run_teleport_preflight(target_host: str, worker_name: str, backend_name: str) -> list[str]:
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
    seen_scripts: set[str] = set()
    for pdir in preflight_dirs:
        if not os.path.isdir(pdir):
            continue
        scripts = sorted(f for f in os.listdir(pdir) if f.endswith(".sh") and os.access(os.path.join(pdir, f), os.X_OK))
        for script in scripts:
            script_path = os.path.join(pdir, script)
            real_path = os.path.realpath(script_path)
            if real_path in seen_scripts:
                continue
            seen_scripts.add(real_path)
            try:
                r = _subprocess_runner.run(
                    [script_path], env=env, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD
                )
                if r.returncode != 0:
                    reason = r.stdout.strip().split("\n")[0] if r.stdout.strip() else f"{script} failed"
                    fails.append(reason)
            except subprocess.TimeoutExpired:
                fails.append(f"{script} timed out")
            except (subprocess.SubprocessError, OSError) as e:
                fails.append(f"{script} error: {e}")
    return fails


def sync_working_directory(
    source_cwd: str, target_cwd: str, source_host: str | None = None, target_host: str | None = None, full: bool = False
) -> bool:

    if not full and _is_git_repo(source_cwd, host=source_host):
        project = _get_project_name(source_cwd, host=source_host)
        if project:
            try:
                bare_repo = _ensure_bare_repo(project)
                meta = _git_push_state(source_cwd, project, bare_repo, host=source_host)
                if meta:
                    bare_url = _bare_repo_url(bare_repo, target_host=target_host)
                    if _git_pull_state(target_cwd, project, bare_url, meta, host=target_host):
                        _log(_LOG_INFO, "teleport", f"git sync succeeded for {project}")
                        return True
                    _log(_LOG_WARN, "teleport", f"git pull failed, falling back to rsync")
                else:
                    _log(_LOG_WARN, "teleport", f"git push failed, falling back to rsync")
            except (subprocess.SubprocessError, OSError, KeyError) as e:
                _log(_LOG_ERROR, "teleport", f"git sync error, falling back to rsync: {e}")
    return rsync_working_directory(source_cwd, target_cwd, source_host, target_host, full)


def rsync_working_directory(
    source_cwd: str, target_cwd: str, source_host: str | None = None, target_host: str | None = None, full: bool = False
) -> bool:

    _remote_run(["mkdir", "-p", target_cwd], host=target_host)
    cmd = ["rsync", "-az", "--delete"]
    gitignore_tmpfile = None
    if not full:
        with _log_suppress(
            ConnectionError,
            TimeoutError,
            OSError,
            tag="teleport",
            msg="git ls-files failed, skipping gitignore excludes: ",
        ):
            gi_result = _remote_run(
                ["git", "-C", source_cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
                host=source_host,
                timeout=TIMEOUT_FILE_TRANSFER,
            )
            if gi_result.returncode == 0 and gi_result.stdout.strip():
                fd, gitignore_tmpfile = tempfile.mkstemp(prefix="rsync-gitignore-", suffix=".txt")
                try:
                    os.write(fd, gi_result.stdout.encode())
                finally:
                    os.close(fd)
                cmd.extend(["--exclude-from", gitignore_tmpfile])
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


def sync_session_transcript(
    session_id: str, source_cwd: str, target_cwd: str, source_host: str | None = None, target_host: str | None = None
) -> None:

    if not session_id:
        return
    source_slug = _project_slug(source_cwd)
    target_slug = _project_slug(target_cwd)
    source_dir = f".claude/projects/{source_slug}"
    target_dir = f".claude/projects/{target_slug}"
    if target_host:
        _remote_run(["bash", "-c", f"mkdir -p $HOME/{target_dir}"], host=target_host)
    else:
        os.makedirs(os.path.expanduser(f"~/{target_dir}"), exist_ok=True)
    jsonl = f"{session_id}.jsonl"
    for item in [jsonl, f"{session_id}/"]:
        if source_host:
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


def sync_shared_repos(target_host: str, chat_id: int | str | None = None) -> list[str]:
    if not target_host:
        return []
    warnings: list[str] = []

    def _warn(msg: str) -> None:
        warnings.append(msg)
        _log(_LOG_INFO, "teleport", msg)

    try:
        home = os.path.expanduser("~")
        git_repos = {"team": os.path.join(home, "team"), "agent-config": os.path.join(home, "agent-config")}
        for rn, rp in git_repos.items():
            if os.path.isdir(os.path.join(rp, ".git")):
                try:
                    for cmd in [
                        ["git", "-C", rp, "add", "-A"],
                        ["git", "-C", rp, "commit", "-m", f"teleport sync: {rn}"],
                    ]:
                        _subprocess_runner.run(cmd, capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
                    _subprocess_runner.run(
                        ["git", "-C", rp, "push", "origin", "master"],
                        capture_output=True,
                        timeout=TIMEOUT_LARGE_TRANSFER,
                    )
                except (subprocess.SubprocessError, OSError) as e:
                    _warn(f"git push {rn}: {e}")
        for rn in git_repos:
            try:
                _remote_run(
                    ["bash", "-c", f"cd ~/{rn} 2>/dev/null && git pull origin master 2>/dev/null || true"],
                    host=target_host,
                    timeout=TIMEOUT_LARGE_TRANSFER,
                )
            except (subprocess.SubprocessError, OSError) as e:
                _warn(f"git pull {rn} on {target_host}: {e}")
        for subdir in ("skills", "hooks", "scripts"):
            try:
                _remote_run(
                    [
                        "bash",
                        "-c",
                        f"[ -d ~/agent-config/.claude/{subdir} ] && rsync -az --checksum ~/agent-config/.claude/{subdir}/ ~/.claude/{subdir}/",
                    ],
                    host=target_host,
                    timeout=TIMEOUT_LARGE_TRANSFER,
                )
            except (subprocess.SubprocessError, OSError) as e:
                _warn(f"agent-config deploy {subdir}: {e}")
        remote_home = _get_remote_home(target_host)
        if remote_home and remote_home != home:
            settings_src = os.path.expanduser("~/.claude/settings.json")
            if os.path.exists(settings_src):
                with open(settings_src) as f:
                    settings_text = f.read().replace(home, remote_home)
                fd, tmp = tempfile.mkstemp(suffix=".json")
                try:
                    os.write(fd, settings_text.encode())
                finally:
                    os.close(fd)
                try:
                    _subprocess_runner.run(
                        ["rsync", "-az", tmp, f"{target_host}:.claude/settings.json"],
                        capture_output=True,
                        timeout=TIMEOUT_REMOTE_CMD,
                    )
                finally:
                    os.unlink(tmp)
    except (subprocess.SubprocessError, OSError) as e:
        _warn(f"shared repo sync: {e}")
    return warnings


def sync_worker_data_back(name: str, source_host: str | None) -> None:
    if not source_host:
        return
    home = os.path.expanduser("~")
    worker_team_dir = os.path.join(home, "team", name)
    if os.path.isdir(worker_team_dir):
        _subprocess_runner.run(
            ["rsync", "-az", f"{source_host}:team/{name}/", f"{worker_team_dir}/"],
            capture_output=True,
            timeout=TIMEOUT_GIT_OP,
        )
    r = _remote_run(
        ["bash", "-c", "find ~/.claude/projects/*/memory -name '*.md' 2>/dev/null | head -50"],
        host=source_host,
        timeout=TIMEOUT_REMOTE_CMD,
    )
    if r.returncode == 0 and r.stdout.strip():
        for remote_file in r.stdout.strip().splitlines():
            remote_home = _get_remote_home(source_host)
            if remote_home and remote_file.startswith(remote_home):
                local_file = home + remote_file[len(remote_home) :]
                local_dir = os.path.dirname(local_file)
                os.makedirs(local_dir, exist_ok=True)
                _subprocess_runner.run(
                    ["rsync", "-az", f"{source_host}:{remote_file}", local_file],
                    capture_output=True,
                    timeout=TIMEOUT_REMOTE_CMD,
                )


def install_hooks_on_target(target_host: str) -> list[str]:
    if not target_host:
        return []
    warnings = []
    try:
        _remote_run(["chmod", "-R", "700", ".claude/hooks"], host=target_host)
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            _subprocess_runner.run(
                ["rsync", "-az", claude_json, f"{target_host}:.claude.json"],
                capture_output=True,
                timeout=TIMEOUT_REMOTE_CMD,
            )
        else:
            _remote_run(
                [
                    "python3",
                    "-c",
                    "import json,os,pathlib;"
                    'p=pathlib.Path(os.path.expanduser("~/.claude.json"));'
                    "d=json.loads(p.read_text()) if p.exists() else {};"
                    'd["hasCompletedOnboarding"]=True;'
                    'd.setdefault("numStartups",1);'
                    "p.write_text(json.dumps(d))",
                ],
                host=target_host,
                timeout=TIMEOUT_REMOTE_CMD,
            )
    except (subprocess.SubprocessError, OSError) as e:
        w = f"hook install: {e}"
        warnings.append(w)
        _log(_LOG_INFO, "teleport", f"{w}")
    return warnings


def sync_session_files_to_target(name: str, target_sessions_dir: str, target_host: str) -> None:
    local_session_dir = SESSIONS_DIR / name
    if not local_session_dir.is_dir():
        return
    remote_session_dir = f"{target_sessions_dir}/{name}"
    _remote_run(["mkdir", "-p", remote_session_dir], host=target_host)
    for fname in ["chat_id", "claude_session_id"]:
        local_file = local_session_dir / fname
        if local_file.exists():
            _subprocess_runner.run(
                ["rsync", "-az", str(local_file), f"{target_host}:{remote_session_dir}/{fname}"],
                capture_output=True,
                timeout=TIMEOUT_REMOTE_CMD,
            )


def sync_credentials_to_target(target_host: str) -> None:
    local_creds = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.exists(local_creds):
        return
    with suppress(
        json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, subprocess.SubprocessError, OSError
    ):
        r = _remote_run(
            ["cat", ".claude/.credentials.json"],
            host=target_host,
            timeout=TIMEOUT_TMUX_SEND,
        )
        if r.returncode == 0 and r.stdout.strip():
            remote_data = cast(dict[str, object], json.loads(r.stdout))
            local_data = cast(dict[str, object], json.loads(Path(local_creds).read_text()))
            _ro = remote_data.get("claudeAiOauth", {})
            remote_oauth = _ro if isinstance(_ro, dict) else {}
            _lo = local_data.get("claudeAiOauth", {})
            local_oauth = _lo if isinstance(_lo, dict) else {}
            remote_refresh = str(remote_oauth.get("refreshToken", ""))
            local_refresh = str(local_oauth.get("refreshToken", ""))
            remote_exp = int(remote_oauth.get("expiresAt", 0))
            now_ms = int(_clock.time() * 1000)
            if remote_refresh and remote_refresh != local_refresh and remote_exp > now_ms:
                _log(_LOG_INFO, "creds", f"Target {target_host} has independent valid credentials, skipping sync")
                return
    _remote_run(["mkdir", "-p", ".claude"], host=target_host)
    _subprocess_runner.run(
        ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json.tmp"],
        capture_output=True,
        timeout=TIMEOUT_REMOTE_CMD,
    )
    _remote_run(
        ["mv", ".claude/.credentials.json.tmp", ".claude/.credentials.json"], host=target_host, capture_output=True
    )
    _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host)
    _log(_LOG_INFO, "creds", f"Synced credentials to {target_host}")


def stop_worker_for_teleport(name: str, tmux_name: str, host: str | None = None) -> str | None:
    session_id = get_claude_session_id(name, authoritative=True)
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"], host=host)
    for _ in range(20):
        _clock.sleep(DELAY_RETRY)
        r = _remote_run(["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"], host=host)
        if r.returncode != 0:
            break
        pane_pid = r.stdout.strip()
        if pane_pid:
            claude_pid = _get_claude_pid(pane_pid, host=host)
            if not claude_pid:
                break
    else:
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], host=host)
        _clock.sleep(DELAY_STARTUP)
    return get_claude_session_id(name, authoritative=True) or session_id


def start_worker_on_target(
    name: str,
    target_host: str,
    target_cwd: str | None,
    session_id: str | None,
    backend_name: str,
    skip_session_sync: bool = False,
) -> bool:

    tmux_name = f"{TMUX_PREFIX}{name}"
    _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host)
    _clock.sleep(DELAY_TMUX_SEND)
    r = _remote_run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
        host=target_host,
    )
    if r.returncode != 0:
        _log(
            _LOG_WARN,
            "teleport",
            f"tmux new-session failed: rc={r.returncode} stderr={r.stderr[:200] if r.stderr else ''}",
        )
        return False
    _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], host=target_host)
    _clock.sleep(DELAY_RETRY)
    target_sessions_dir = _remap_path(str(SESSIONS_DIR), target_host)
    if target_host and not skip_session_sync:
        sync_session_files_to_target(name, target_sessions_dir, target_host)
    if target_host:
        sync_credentials_to_target(target_host)
    for key, value in {
        "PORT": str(PORT),
        "TMUX_PREFIX": TMUX_PREFIX,
        "SESSIONS_DIR": target_sessions_dir,
        "WORKER_BACKEND": normalize_backend(backend_name),
        "BRIDGE_URL": BRIDGE_PUBLIC_URL or BRIDGE_URL,
    }.items():
        _remote_run(["tmux", "set-environment", "-t", tmux_name, key, value], host=target_host)
    _clock.sleep(DELAY_TMUX_SEND)
    _remote_run(
        ["tmux", "send-keys", "-t", tmux_name, 'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
        host=target_host,
    )
    _clock.sleep(DELAY_TMUX_SEND)
    backend = get_backend(backend_name)
    cli_cmd = backend.start_cmd(session_id or "")
    if target_host:
        id_result = _remote_run(["id", "-u"], host=target_host)
        if id_result.returncode == 0 and id_result.stdout.strip() == "0":
            cli_cmd = cli_cmd.replace(" --dangerously-skip-permissions", "")
    start_cmd = f"unset CLAUDECODE && {cli_cmd}"
    if target_cwd:
        start_cmd = f"cd {shlex.quote(target_cwd)} && {start_cmd}"
    _log(_LOG_INFO, "teleport", f"start_cmd={start_cmd}")
    _remote_run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], host=target_host)
    if backend.is_interactive:
        for delay, key in [(3.0, "Enter"), (2.0, "Enter"), (1.0, "Enter"), (1.0, "Enter")]:
            _clock.sleep(delay)
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key], host=target_host)
    for attempt in range(30):
        _clock.sleep(DELAY_STARTUP)
        r = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
            host=target_host,
        )
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
            cap = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=target_host,
                timeout=TIMEOUT_TMUX_SEND,
            )
            if cap.returncode == 0:
                _log(_LOG_INFO, "teleport", f"pane content: {cap.stdout[:300]}")
    _log(_LOG_WARN, "teleport", f"verify FAILED after 30 attempts")
    return False


def teleport_rollback(
    name: str,
    tmux_name: str,
    source_host: str | None,
    source_cwd: str | None,
    session_id: str | None,
    backend_name: str,
    chat_id: ChatId | None,
    reason: str,
) -> None:

    teleport_notify(chat_id, f"Teleport failed: {reason}. Rolling back...")
    try:
        if source_cwd:
            save_claude_session_cwd(name, source_cwd)
        if not tmux_exists(tmux_name, host=source_host):
            _remote_run(
                ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
                host=source_host,
            )
            _remote_run(
                ["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], host=source_host, capture_output=True
            )
        backend = get_backend(backend_name)
        start_cmd = f'unset CLAUDECODE && {backend.start_cmd(session_id or "")}'
        if source_cwd:
            start_cmd = f"cd {shlex.quote(source_cwd)} && {start_cmd}"
        _remote_run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], host=source_host)
        teleport_notify(chat_id, f"{name} restarted on source. Teleport cancelled.")
    except (subprocess.SubprocessError, OSError) as e:
        teleport_notify(chat_id, f"Rollback also failed: {e}")
    with _log_suppress(OSError, level=_LOG_DEBUG, tag="io:unknown"):
        state_file = SESSIONS_DIR / name / "teleport_state"
        state_file.unlink(missing_ok=True)


def handle_notify(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    try:
        data = cast(dict[str, object], json.loads(body))
        text = _str_field(data, "text")
        name = _str_field(data, "name")
        if not text:
            handler._send_text(400, "Missing text")
            return
        clean_text, images, files = _parse_response_media(name or "", text)
        chat_ids = get_all_chat_ids()
        sent = 0
        label = name or "notify"
        for chat_id in chat_ids:
            if clean_text:
                result = transport.send_text(chat_id, clean_text)
                if result and result.get("ok"):
                    sent += 1
            _send_response_media(label, images, files, chat_id)
        has_media = len(images) + len(files)
        _log(
            _LOG_INFO,
            "notify",
            f"sent to {sent}/{len(chat_ids)} chats: {text[:50]}..." f"{f' ({has_media} media)' if has_media else ''}",
        )
        handler._send_text(200, f"Sent to {sent} chats")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"Notify error: {e}")
        handler._send_text(500, str(e))


def handle_health_alert(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    try:
        data = json.loads(body) if body else {}
        worker = _str_field(data, "worker", "unknown")
        issue = _str_field(data, "issue", "unknown")
        age = _int_field(data, "transcript_age")
        age_human = f"{age // 3600}h{(age % 3600) // 60}m" if age >= 3600 else f"{age // 60}m"
        alert_text = f"🔴 {worker}: JSONL transcript stale ({age_human}). Session active but not recording. `/restart {worker}` to fix."
        _log(_LOG_WARN, "health", f"Health alert: {worker} — {issue} (age={age}s)")
        chat_ids = get_all_chat_ids()
        for chat_id in chat_ids:
            transport.send_text(chat_id, alert_text)
        handler._send_json(200, {"ok": True})
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"Health alert error: {e}")
        handler._send_json(500, {"ok": False, "error": str(e)})


def handle_forge_register(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    try:
        data = json.loads(body) if body else {}
        name = _str_field(data, "Name") or _str_field(data, "name")
        host = _str_field(data, "Host") or _str_field(data, "host")
        version = _str_field(data, "Version") or _str_field(data, "version")
        callback_url = (
            _str_field(data, "CallbackURL") or _str_field(data, "callback_url") or _str_field(data, "callbackUrl")
        )
        tools = data.get("Tools", data.get("tools", {}))
        if name:
            if callback_url:
                _registry_add_callback(name, callback_url, host=host, version=version, tools=tools)
                _log(
                    _LOG_INFO,
                    "worker",
                    f"Callback worker registered: {name} (host={host}, url={callback_url}, version={version})",
                )
            else:
                _registry_add(name, DEFAULT_BACKEND, host=host)
                _log(_LOG_INFO, "worker", f"Forge worker registered: {name} (host={host}, version={version})")
            backend_name = get_worker_backend(name, {"host": host})
            tmux_name = f"{TMUX_PREFIX}{name}"
            reg_host = host or None
            if claudecode.tmux_exists(tmux_name, host=reg_host):
                claudecode.export_hook_env(tmux_name, backend_name, host=reg_host)
            claudecode.ensure_session_dir(name)
            if core.admin_chat_id is not None:
                cid_file = claudecode.get_chat_id_file(name)
                if not cid_file.exists():
                    _tmp_cid = cid_file.with_suffix(".tmp")
                    _tmp_cid.write_text(str(core.admin_chat_id))
                    _tmp_cid.chmod(0o600)
                    os.replace(str(_tmp_cid), str(cid_file))
        tmux_session = f"{TMUX_PREFIX}{name}" if name else ""
        conflict = False
        active_workers = []
        with _log_suppress(subprocess.SubprocessError, OSError, level=_LOG_DEBUG, tag="probe:unknown"):
            r = core._subprocess_runner.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_TMUX_SEND,
            )
            if r.returncode == 0:
                active_workers = [
                    s.removeprefix(TMUX_PREFIX) for s in r.stdout.strip().split("\n") if s.startswith(TMUX_PREFIX)
                ]
                conflict = name in active_workers if name else False
        worker_manager.invalidate_sessions_cache()
        handler._send_json(
            200,
            {
                "ok": True,
                "settings": {"tmux_prefix": TMUX_PREFIX, "node_name": NODE_NAME or "", "tmux_session": tmux_session},
                "conflict": conflict,
                "active_workers": active_workers,
            },
        )
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError) as e:
        _log(_LOG_ERROR, "bridge", f"Register error: {e}")
        handler._send_json(500, {"ok": False, "error": str(e)})


def handle_checkin_endpoint(handler: BaseHTTPRequestHandler, parsed: ParseResult) -> None:
    try:
        params = parse_qs(parsed.query)
        name = params.get("name", ["worker"])[0]
        raw_cwd = params.get("cwd", [None])[0]
        requested_cwd = ""
        if raw_cwd is not None:
            worker_host = get_worker_host(name)
            requested_cwd, cwd_err = validate_cwd(raw_cwd, host=worker_host)
            if cwd_err:
                handler._send_text(400, f"Invalid cwd: {cwd_err}")
                return
        registered = _wm().get_registered_sessions()
        tmux_name = ""
        host = None
        if name in registered:
            backend_name = get_worker_backend(name, registered[name])
            tmux_name = registered[name].get("tmux", f"{TMUX_PREFIX}{name}")
            host = get_worker_host(name)
            if claudecode.tmux_exists(tmux_name, host=host):
                claudecode.export_hook_env(tmux_name, backend_name, host=host)
        else:
            backend_name = DEFAULT_BACKEND
        backend_obj = get_backend(backend_name)
        if requested_cwd:
            _set_worker_cwd(name, requested_cwd)
            old_cwd = get_claude_session_cwd(name)
            save_claude_session_cwd(name, requested_cwd)
            _ensure_workspace_trusted(requested_cwd)
            if old_cwd and old_cwd.rstrip("/") != requested_cwd.rstrip("/"):
                old_sid = get_claude_session_id(name)
                _log_session_event(name, old_sid or "(none)", requested_cwd, "cwd_change")
                _log(
                    _LOG_WARN,
                    "checkin",
                    f"{name}: CWD changed ({old_cwd} -> {requested_cwd}), stale sessions will self-invalidate",
                )
                notice = _build_cwd_change_notice(name, old_cwd, requested_cwd, old_sid or "")
                notify_chat_id = get_manager_chat_id(name)
                if notify_chat_id is not None:
                    send_telegram_message(notify_chat_id, notice, parse_mode="HTML")
            _log(_LOG_INFO, "checkin", f"{name}: requested_cwd={requested_cwd}, tmux={tmux_name}, host={host}")
            if tmux_name and claudecode.tmux_exists(tmux_name, host=host):
                pane_cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
                same_cwd = pane_cwd and pane_cwd.rstrip("/") == requested_cwd.rstrip("/")
                _log(_LOG_INFO, "checkin", f"{name}: pane_cwd={pane_cwd}, same_cwd={same_cwd}")
                if not same_cwd:
                    allowed, block_msg = _checkin_can_restart(name, tmux_name, host, pane_cwd or "", requested_cwd)
                    if not allowed:
                        notify_chat_id = get_manager_chat_id(name)
                        if notify_chat_id is not None:
                            send_telegram_message(notify_chat_id, block_msg)
                        handler._send_text(200, block_msg)
                        return
                    _log(
                        _LOG_INFO,
                        "checkin",
                        f"{name}: triggering restart (cwd mismatch: pane={pane_cwd} vs requested={requested_cwd})",
                    )
                    ok, err = _checkin_do_restart(name, backend_name, tmux_name, host, requested_cwd)
                    if not ok:
                        handler._send_text(500, f"Failed to restart in {requested_cwd}: {err}")
                    else:
                        handler._send_text(200, f"Restarting in {requested_cwd}...")
                    return
        handler._send_text(200, worker_manager._build_welcome(name, backend_obj))
    except (subprocess.SubprocessError, OSError, KeyError) as exc:
        _log(_LOG_ERROR, "bridge", f"Checkin endpoint error: {exc}")
        handler._send_text(500, str(exc))


def handle_health_workers_endpoint(handler: BaseHTTPRequestHandler) -> None:
    try:
        now = core._clock.time()
        registered = get_registered_sessions()
        with watchdog.lock:
            state_snapshot = dict(watchdog.worker_states)
        workers = {}
        for name in sorted(registered.keys()):
            entry = state_snapshot.get(name)
            if entry:
                workers[name] = {
                    "state": entry.status,
                    "reason": entry.reason,
                    "since": entry.since,
                    "age_sec": int(now - entry.since) if entry.since else None,
                }
            else:
                workers[name] = {"state": "unknown"}
        handler._send_json(200, {"workers": workers})
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_ERROR, "worker", f"Health workers endpoint error: {e}")
        handler._send_text(500, str(e))


# PR review handler functions (handle_pr_file_content, handle_pr_review_comments,
# handle_pr_merge, handle_pr_review_endpoint, handle_pr_keepalive) removed —
# served by spawn-on-demand Python viewer (review/src/serve.ts)

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler


def _fanout_channel_message(
    channel_id: str, from_member: str, text: str, msg: dict, members_snapshot: dict, registered: dict
) -> None:
    tagged = f"[{channel_id} from {from_member}] {text}"
    for member_key, minfo in members_snapshot.items():
        if member_key == from_member:
            continue
        if minfo["type"] == "worker":
            wname = minfo.get("name", "")
            if wname and wname in registered:
                winfo = registered[wname]
                backend_name = claudecode.get_worker_backend(wname, winfo)
                backend = claudecode.get_backend(backend_name)
                with _log_suppress(ConnectionError, TimeoutError, msg=f"Channel fan-out to {wname} failed: "):
                    backend.send(wname, f"{TMUX_PREFIX}{wname}", tagged, BRIDGE_URL, SESSIONS_DIR)
        elif minfo["type"] == "guest":
            gname = minfo.get("name", "")
            if gname:
                with guest_store.lock:
                    ginbox = guest_store.inboxes.get(gname, [])
                    guest_store.inboxes[gname] = guest_inbox_append(
                        ginbox,
                        {
                            "id": msg["id"],
                            "from": from_member,
                            "channel": channel_id,
                            "text": text,
                            "ts": msg["ts"],
                        },
                    )
        elif minfo["type"] == "manager":

            _notify_admin(f"[{channel_id}] {from_member}: {text}")


def _guest_auth(handler: BaseHTTPRequestHandler, parsed: ParseResult | None = None) -> dict | None:
    query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(handler.path).query)
    token = query_params.get("token", [""])[0]
    if not token:
        handler._send_error_json(403, "token required")
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with guest_store.lock:
        guest = guest_store.guests.get(token_hash)
    if not guest or guest_is_expired(guest["expires_at_unix"]):
        if guest:
            with guest_store.lock:
                guest_store.guests.pop(token_hash, None)
                _guest_save()
        handler._send_error_json(403, "invalid or expired guest session")
        return None
    return guest


def _channel_guest_auth(handler: BaseHTTPRequestHandler, query_params: dict) -> str | None:
    token = query_params.get("token", [None])[0]
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with guest_store.lock:
        guest = guest_store.guests.get(token_hash)
    if not guest or guest_is_expired(guest["expires_at_unix"]):
        handler._send_error_json(403, "invalid or expired token")
        return ""
    return f"guest:{guest['name']}"


def handle_guest_register(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    try:
        data = cast(dict, json.loads(body)) if body else {}
    except (json.JSONDecodeError, ValueError):
        data = {}
    requested_name = _str_field(data, "name").strip().lower()
    team_workers = set(get_registered_sessions().keys())
    with guest_store.lock:
        existing_guests = {g["name"] for g in guest_store.guests.values()}
    if requested_name:
        ok, err = guest_validate_name(requested_name, team_workers, existing_guests)
        if not ok:
            status = 409 if "conflicts" in err or "already taken" in err else 400
            handler._send_json(status, {"ok": False, "error": err})
            return
        name = requested_name
    else:
        name = guest_generate_name(existing_names=team_workers | existing_guests)
    token, token_hash = guest_create_token()
    now = _clock.time()
    expires_at_unix = now + GUEST_TTL
    expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_unix))
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    guest = cast(
        GuestSessionDict,
        {
            "name": name,
            "token_hash": token_hash,
            "created_at": created_at,
            "expires_at": expires_at,
            "expires_at_unix": expires_at_unix,
            "notified_workers": set(),
        },
    )
    with guest_store.lock:
        guest_store.guests[token_hash] = guest
        guest_store.inboxes[name] = []
        _guest_save()
    base_url = _relay_base_url()
    listen_script = (
        f"python3 -c \"import json,time,urllib.request as u;TOKEN,URL='{token}','{base_url}';last='';"
        f"print('[listener] connected',flush=True)\nwhile True:\n try:\n  q=URL+'/guests/inbox?token='+TOKEN+("
        f"'&after='+last if last else '');d=json.loads(u.urlopen(u.Request(q),timeout=10).read())\n  for m in d.get('messages',[]):"
        f"\n   print(m.get('from','?')+': '+m['text'],flush=True);last=m['id']\n except Exception as e:\n  "
        f"if '403' in str(e) or '404' in str(e):print('[listener] expired',flush=True);break\n time.sleep(3)\""
    )
    _notify_admin(f'\U0001f514 Guest "{name}" connected')
    _log(_LOG_INFO, "guest", f"Guest registered: {name} (expires {expires_at})")
    handler._send_json(
        200,
        {
            "ok": True,
            "name": name,
            "token": token,
            "expires": expires_at,
            "inbox_url": f"/guests/inbox?token={token}",
            "send_url": f"/guests/send?token={token}",
            "channels_url": f"/channels?token={token}",
            "channel_create_url": f"/channels?token={token}",
            "listen_script": listen_script,
        },
    )


def handle_guest_send(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    parsed = urlparse(handler.path)
    guest = _guest_auth(handler, parsed)
    if not guest:
        return
    data = handler._parse_body(body)
    if data is None:
        return
    text = _str_field(data, "text").strip()
    if not text:
        handler._send_error_json(400, "text required")
        return
    raw_to = data.get("to", _str_field(data, "worker"))
    if isinstance(raw_to, str):
        targets = [raw_to.strip()] if raw_to.strip() else []
    elif isinstance(raw_to, list):
        targets = [t.strip() for t in raw_to if isinstance(t, str) and t.strip()]
    else:
        targets = []
    if not targets:
        handler._send_error_json(400, "to (or worker) required")
        return
    guest_name = guest["name"]
    from_member = f"guest:{guest_name}"
    tagged_text = f"[guest:{guest_name}] {text}"
    registered = get_registered_sessions()
    results = []
    for target in targets:
        if target.startswith(("#", "ch_")):
            with channel_store.lock:
                if target.startswith("#"):
                    ch_id = next(
                        (
                            cid
                            for cid, ch in channel_store.channels.items()
                            if ch["label"] == target[1:] and not channel_is_expired(ch)
                        ),
                        None,
                    )
                else:
                    ch_id = target
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
            target_guest = target.split(":", 1)[1]
            msg_id = f"gm_{secrets.token_hex(4)}"
            with guest_store.lock:
                ginbox = guest_store.inboxes.get(target_guest, [])
                guest_store.inboxes[target_guest] = guest_inbox_append(
                    ginbox,
                    {
                        "id": msg_id,
                        "from": from_member,
                        "text": text,
                        "ts": int(_clock.time()),
                    },
                )
            results.append({"target": target, "ok": True, "message_id": msg_id})
        else:
            worker = target
            if worker not in registered:
                results.append({"target": worker, "ok": False, "error": f"worker '{worker}' not found"})
                continue
            info = registered[worker]
            backend_name = claudecode.get_worker_backend(worker, info)
            backend = claudecode.get_backend(backend_name)
            tmux_name = f"{TMUX_PREFIX}{worker}"
            delivered = backend.send(worker, tmux_name, tagged_text, BRIDGE_URL, SESSIONS_DIR)
            msg_id = f"gm_{secrets.token_hex(4)}"
            with guest_store.lock:
                inbox = guest_store.inboxes.get(guest_name, [])
                guest_store.inboxes[guest_name] = guest_inbox_append(
                    inbox,
                    {
                        "id": msg_id,
                        "from": guest_name,
                        "to": worker,
                        "text": text,
                        "ts": int(_clock.time()),
                    },
                )
                notified = guest.get("notified_workers", set())
                if worker not in notified:
                    if isinstance(notified, set):
                        notified.add(worker)
                    guest["notified_workers"] = notified
                    _notify_admin(f'\U0001f514 Guest "{guest_name}" → {worker}')
            results.append({"target": worker, "ok": True, "delivered": delivered, "message_id": msg_id})
    if len(results) == 1:
        handler._send_json(200, {**results[0], "ok": results[0].get("ok", True)})
    else:
        handler._send_json(200, {"ok": all(r.get("ok") for r in results), "results": results})


def handle_guest_reply(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    data = handler._parse_body(body)
    if data is None:
        return
    guest_name = _str_field(data, "guest").strip()
    from_worker = _str_field(data, "from").strip()
    text = _str_field(data, "text").strip()
    if not guest_name or not text:
        handler._send_error_json(400, "guest and text required")
        return
    msg_id = f"gm_{secrets.token_hex(4)}"
    with guest_store.lock:
        if guest_name not in guest_store.inboxes:
            guest_store.inboxes[guest_name] = []
        guest_store.inboxes[guest_name] = guest_inbox_append(
            guest_store.inboxes[guest_name],
            {"id": msg_id, "from": from_worker or "worker", "text": text, "ts": int(_clock.time())},
        )
    handler._send_json(200, {"ok": True, "message_id": msg_id})


def handle_guest_inbox(handler: BaseHTTPRequestHandler, parsed: ParseResult) -> None:
    guest = _guest_auth(handler, parsed)
    if not guest:
        return
    query_params = parse_qs(parsed.query)
    after = query_params.get("after", [None])[0]
    guest_name = guest["name"]
    with guest_store.lock:
        msgs = list(guest_store.inboxes.get(guest_name, []))
    filtered = guest_inbox_filter(msgs, after=after)
    handler._send_json(
        200,
        {
            "ok": True,
            "name": guest_name,
            "messages": filtered,
        },
    )


def handle_guest_status(handler: BaseHTTPRequestHandler, parsed: ParseResult) -> None:
    guest = _guest_auth(handler, parsed)
    if not guest:
        return
    with guest_store.lock:
        workers = list(guest.get("notified_workers", set()))
    handler._send_json(
        200, {"ok": True, "name": guest["name"], "expires": guest["expires_at"], "connected_workers": workers}
    )


def handle_guests_list(handler: BaseHTTPRequestHandler) -> None:
    with guest_store.lock:
        guests_list = []
        expired = []
        for th, g in guest_store.guests.items():
            if guest_is_expired(g["expires_at_unix"]):
                expired.append(th)
            else:
                guests_list.append(
                    {
                        "name": g["name"],
                        "expires": g["expires_at"],
                        "connected_workers": list(g.get("notified_workers", set())),
                    }
                )
        for th in expired:
            guest_store.inboxes.pop(guest_store.guests.pop(th, {}).get("name", ""), None)
        if expired:
            _guest_save()
    handler._send_json(200, {"ok": True, "guests": guests_list})


def handle_guest_disconnect(handler: BaseHTTPRequestHandler, parsed: ParseResult) -> None:
    query_params = parse_qs(parsed.query)
    token = query_params.get("token", [""])[0]
    if not token:
        handler._send_error_json(403, "token required")
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with guest_store.lock:
        guest = guest_store.guests.pop(token_hash, None)
        if guest:
            guest_store.inboxes.pop(guest["name"], None)
            _guest_save()
    if not guest:
        handler._send_error_json(403, "invalid token")
        return
    _notify_admin(f"\U0001f514 Guest \"{guest['name']}\" disconnected")
    _log(_LOG_INFO, "guest", f"Guest disconnected: {guest['name']}")
    handler._send_json(200, {"ok": True, "name": guest["name"]})


def handle_channel_create(handler: BaseHTTPRequestHandler, body: bytes = b"") -> None:
    data = handler._parse_body(body)
    if data is None:
        return
    label = _str_field(data, "label").strip()
    members = data.get("members", [])
    include_manager = _bool_field(data, "include_manager", True)
    ttl = min(_int_field(data, "ttl_seconds", CHANNEL_TTL), CHANNEL_TTL)
    if not isinstance(members, list):
        handler._send_error_json(400, "members must be a list")
        return
    valid_members = []
    for m in members:
        if isinstance(m, str) and (m == "manager" or ":" in m):
            valid_members.append(m)
    if include_manager and "manager" not in valid_members:
        valid_members.append("manager")
    parsed = urlparse(handler.path)
    query_params = parse_qs(parsed.query)
    guest_member = _channel_guest_auth(handler, query_params)
    if guest_member == "":
        return
    created_by = guest_member or "manager"
    if guest_member and guest_member not in valid_members:
        valid_members.append(guest_member)
    channel_id = channel_create_id(label)
    channel = channel_new(channel_id, label, created_by, valid_members, ttl)
    with channel_store.lock:
        channel_store.channels[channel_id] = channel
        _channel_save()
    member_str = ", ".join(valid_members)
    _notify_admin(f"\U0001f4e2 Channel {channel_id} created by {created_by}\nMembers: {member_str}")
    _log(_LOG_INFO, "channel", f"Channel created: {channel_id} by {created_by} members=[{member_str}]")
    handler._send_json(
        200,
        {
            "ok": True,
            "channel": channel_id,
            "label": label,
            "members": valid_members,
            "send_url": f"/channels/{channel_id}/send",
            "messages_url": f"/channels/{channel_id}/messages",
        },
    )


def handle_channel_members(handler: BaseHTTPRequestHandler, channel_id: str, body: bytes = b"") -> None:
    data = handler._parse_body(body)
    if data is None:
        return
    with channel_store.lock:
        channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel):
            handler._send_error_json(404, "channel not found")
            return
        added = channel_add_members(channel, data.get("add", []) if isinstance(data.get("add", []), list) else [])
        removed = channel_remove_members(
            channel, data.get("remove", []) if isinstance(data.get("remove", []), list) else []
        )
        current = list(channel["members"].keys())
    if added or removed:
        _notify_admin(
            f"\U0001f4e2 Channel {channel_id}: {'; '.join((['added ' + ', '.join(added)] if added else []) + (['removed ' + ', '.join(removed)] if removed else []))}"
        )
    handler._send_json(200, {"ok": True, "channel": channel_id, "added": added, "removed": removed, "members": current})


def handle_channel_send(handler: BaseHTTPRequestHandler, channel_id: str, body: bytes = b"") -> None:
    data = handler._parse_body(body)
    if data is None:
        return
    text = _str_field(data, "text").strip()
    if not text:
        handler._send_error_json(400, "text required")
        return
    parsed = urlparse(handler.path)
    query_params = parse_qs(parsed.query)
    guest_member = _channel_guest_auth(handler, query_params)
    if guest_member == "":
        return
    from_member = guest_member or _str_field(data, "from") or "manager"
    with channel_store.lock:
        channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel):
            handler._send_error_json(404, "channel not found")
            return
        if from_member not in channel["members"] and from_member != "manager":
            handler._send_error_json(403, f"{from_member} not a member")
            return
        msg = channel_append_message(channel, from_member, text)
        members_snapshot = dict(channel["members"])
    _fanout_channel_message(channel_id, from_member, text, msg, members_snapshot, get_registered_sessions())
    handler._send_json(200, {"ok": True, "channel": channel_id, "message_id": msg["id"], "seq": msg["seq"]})


def handle_channel_messages(handler: BaseHTTPRequestHandler, channel_id: str, parsed: ParseResult) -> None:
    query_params = parse_qs(parsed.query)
    after = query_params.get("after", [None])[0]
    from_member = _channel_guest_auth(handler, query_params)
    if from_member == "":
        return
    with channel_store.lock:
        channel = channel_store.channels.get(channel_id)
        if not channel or channel_is_expired(channel):
            handler._send_error_json(404, "channel not found")
            return
        if from_member and from_member not in channel["members"]:
            handler._send_error_json(403, "not a member of this channel")
            return
        msgs, truncated = channel_get_messages(channel, after)
    resp: dict = {"ok": True, "channel": channel_id, "messages": msgs}
    if truncated:
        resp["truncated"] = True
    handler._send_json(200, resp)


def handle_channels_list(handler: BaseHTTPRequestHandler, parsed: ParseResult | None = None) -> None:
    query_params = parse_qs(parsed.query) if parsed else parse_qs(urlparse(handler.path).query)
    filter_member = _channel_guest_auth(handler, query_params)
    if filter_member == "":
        return
    with channel_store.lock:
        active = []
        expired_ids = []
        for cid, ch in channel_store.channels.items():
            if channel_is_expired(ch):
                expired_ids.append(cid)
            else:
                if filter_member and filter_member not in ch["members"]:
                    continue
                active.append(
                    {
                        "id": ch["id"],
                        "label": ch["label"],
                        "members": list(ch["members"].keys()),
                        "message_count": len(ch["messages"]),
                        "created_by": ch["created_by"],
                        "send_url": f"/channels/{ch['id']}/send",
                        "messages_url": f"/channels/{ch['id']}/messages",
                    }
                )
        for cid in expired_ids:
            del channel_store.channels[cid]
        if expired_ids:
            _channel_save()
    handler._send_json(200, {"ok": True, "channels": active})


def handle_channel_delete(handler: BaseHTTPRequestHandler, channel_id: str) -> None:
    with channel_store.lock:
        channel = channel_store.channels.pop(channel_id, None)
        if channel:
            _channel_save()
    if not channel:
        handler._send_error_json(404, "channel not found")
        return
    _notify_admin(f"\U0001f4e2 Channel {channel_id} closed")
    _log(_LOG_INFO, "channel", f"Channel deleted: {channel_id}")
    handler._send_json(200, {"ok": True, "channel": channel_id})


# Aliases (originally from connector_glue import ... as ...)
_connector_on_message = connector_on_message
_connector_get_workers = connector_get_workers
_connector_on_alert = connector_on_alert
_start_connectors = start_connectors
_restart_connector = restart_connector
_get_connectors_status = get_connectors_status


class WorkerRegistryEntry:
    backend: str = "claude"
    protocol: str = ""
    callback_url: str = ""
    host: str | None = None
    version: str = ""
    chat_id: int | None = None
    hire_time: int = 0
    tools: dict[str, object] | None = None
    home_host: str = ""
    home_cwd: str = ""


class WorkerRecord:
    name: str
    backend: str = "claude"
    host: str | None = None
    tmux_name: str = ""
    callback_url: str = ""
    protocol: str = ""
    version: str = ""
    tools: dict[str, object] | None = None
    chat_id: int | None = None
    cwd: str = ""
    home_host: str = ""
    home_cwd: str = ""

    @property
    def is_remote(self) -> bool:
        return bool(self.host)

    @property
    def is_interactive(self) -> bool:
        return self.backend == "claude"


def _registry_update_teleport(name: str, host: str | None, home_host: str | None, home_cwd: str | None) -> None:
    with watchdog.lock:
        data = _load_registry()
        worker = data.get("workers", {}).get(name, {})
        for key, val in [("host", host), ("home_host", home_host), ("home_cwd", home_cwd)]:
            if val is not None:
                worker[key] = val
            else:
                worker.pop(key, None)
        data.setdefault("workers", {})[name] = worker
        _save_registry(data)


def _registry_clear_teleport(name: str) -> None:
    _registry_update_teleport(name, host=None, home_host=None, home_cwd=None)


def mark_hook_event(session_name: str) -> None:
    with watchdog.lock:
        watchdog.last_hook_ts[session_name] = _clock.time()


def kill_adapter(name: str) -> None:
    with processes.adapter_pids_lock:
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
        with _log_suppress(OSError, level=_LOG_DEBUG, tag="io:kill_adapter"):
            stderr_fh.close()


def _load_registry() -> RegistryFileDict:
    try:
        if not WORKER_REGISTRY_FILE.exists():
            return {}
        raw = WORKER_REGISTRY_FILE.read_text()
        data = cast(RegistryFileDict, json.loads(raw))
        if not isinstance(data, dict) or "workers" not in data:
            raise ValueError("invalid registry format")
        return data
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        if WORKER_REGISTRY_FILE.exists():
            corrupt_path = WORKER_REGISTRY_FILE.with_suffix(f".corrupt.{int(_clock.time())}")
            _log(_LOG_INFO, "worker", f"Corrupt worker registry, renaming to {corrupt_path}: {e}")
            with _log_suppress(OSError, level=_LOG_DEBUG, tag="io:_load_registry"):
                WORKER_REGISTRY_FILE.rename(corrupt_path)
        return {}


def _save_registry(data: RegistryFileDict) -> None:
    with _log_suppress(OSError, msg="Failed to save worker registry: "):
        NODE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(NODE_DIR), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(WORKER_REGISTRY_FILE))
        except OSError:
            with _log_suppress(OSError, level=_LOG_DEBUG, tag="io:_save_registry"):
                os.unlink(tmp_path)
            raise


def _registry_add(name: str, backend: str, chat_id: ChatId | None = None, host: str | None = None) -> None:
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data:
            data = {"version": 1, "workers": {}}
        existing = data.get("workers", {}).get(name, {})
        preserved_keys = {"host", "home_host", "home_cwd"}
        entry = {k: v for k, v in existing.items() if k in preserved_keys}
        entry.update(
            {
                "backend": backend,
                "chat_id": chat_id,
                "hire_time": int(_clock.time()),
            }
        )
        if host:
            entry["host"] = host
        data["workers"][name] = cast(RegistryWorkerDict, entry)
        _save_registry(data)


def _registry_add_callback(
    name: str, callback_url: str, host: str = "", version: str = "", tools: dict[str, object] | None = None
) -> None:
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data:
            data = {"version": 1, "workers": {}}
        entry: dict[str, object] = {
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
        data["workers"][name] = cast(RegistryWorkerDict, entry)
        _save_registry(data)


def _registry_remove(name: str) -> None:
    with watchdog.lock:
        data = _load_registry()
        if "workers" not in data:
            return
        data["workers"].pop(name, None)
        _save_registry(data)


def _set_worker_cwd(name: str, cwd: str) -> None:
    normalized = normalize_cwd(cwd)
    with watchdog.lock:
        if normalized:
            watchdog.worker_cwds[name] = normalized
        else:
            watchdog.worker_cwds.pop(name, None)


def _get_worker_cwd(name: str) -> str:
    with watchdog.lock:
        cwd = watchdog.worker_cwds.get(name)
    return cwd if isinstance(cwd, str) else ""


def _registry_bootstrap(registered: dict[str, TmuxSessionDict]) -> None:
    if WORKER_REGISTRY_FILE.exists():
        return
    if not registered:
        return
    data: RegistryFileDict = {"version": 1, "workers": {}}
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
    try:
        path = _CHECKIN_NOTE_PATH
        if os.path.isfile(path):
            text = Path(path).read_text().strip()
            if text:
                return text
    except OSError as e:
        _log(_LOG_WARN, "bridge", f"Failed to read checkin note from {_CHECKIN_NOTE_PATH}: {e}")
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
            with _log_suppress(OSError, msg=f"Failed to delete {f}: "):
                f.unlink()


def get_workers(caller_from: str | None = None) -> list[WorkerEndpointInfo]:
    return _wm().get_workers(caller_from=caller_from)


def get_pending_file(name: str) -> Path:
    return get_session_dir(name) / "pending"


def _get_pending_lock(name: str) -> threading.Lock:
    with processes.pending_locks_guard:
        if name not in processes.pending_locks:
            processes.pending_locks[name] = threading.Lock()
        return processes.pending_locks[name]


def set_pending(name: str, chat_id: ChatId) -> None:
    session_dir = ensure_session_dir(name)
    pending = session_dir / "pending"
    chat_id_file = session_dir / "chat_id"
    _tmp_p = pending.with_suffix(".tmp")
    _tmp_p.write_text(str(int(_clock.time())))
    _tmp_p.chmod(0o600)
    os.replace(str(_tmp_p), str(pending))
    _tmp_c = chat_id_file.with_suffix(".tmp")
    _tmp_c.write_text(str(chat_id))
    _tmp_c.chmod(0o600)
    os.replace(str(_tmp_c), str(chat_id_file))
    _sync_chat_id_to_remote(name, str(chat_id_file))


def _sync_chat_id_to_remote(name: str, local_chat_id_path: str) -> None:
    host = get_worker_host(name)
    if not host:
        return
    with _log_suppress(
        subprocess.SubprocessError, OSError, tag="set_pending", msg=f"Failed to sync chat_id to {host} for {name}: "
    ):
        remote_sessions_dir = _remap_path(str(SESSIONS_DIR), host)
        _remote_run(["mkdir", "-p", f"{remote_sessions_dir}/{name}"], host=host, timeout=TIMEOUT_TMUX_CHECK)
        _remote_copy(local_chat_id_path, f"{remote_sessions_dir}/{name}/chat_id", host=host, direction="push")


def clear_pending(name: str) -> None:
    session_dir = get_session_dir(name)
    pending = session_dir / "pending"
    with _log_suppress(OSError, level=_LOG_DEBUG, tag="cleanup:clear_pending"):
        pending.unlink()


def is_pending(name: str) -> bool:
    pending = get_pending_file(name)
    if not pending.exists():
        return False
    try:
        ts = int(pending.read_text().strip())
        return not _clock.time() - ts > PENDING_TIMEOUT
    except (OSError, ValueError):
        return False


def try_set_pending(name: str, chat_id: ChatId) -> bool:
    with _get_pending_lock(name):
        if is_pending(name):
            return False
        set_pending(name, chat_id)
        return True


def _pending_timestamp(name: str) -> int | None:
    pending = get_pending_file(name)
    if not pending.exists():
        return None
    try:
        return int(pending.read_text().strip())
    except OSError:
        return None


def _wait_for_restart_ready(tmux_name: str, backend_name: str, timeout: float = 45.0, host: str | None = None) -> bool:
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


def _send_to_callback_worker(
    name: str, message: str, from_name: str = "manager", session: TmuxSessionDict | None = None
) -> bool:
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


def _wm() -> WorkerManager:
    """Return worker_manager after syncing its config."""
    assert worker_manager is not None, "worker_manager not initialized"
    worker_manager.sessions_dir = SESSIONS_DIR
    worker_manager.tmux_prefix = TMUX_PREFIX
    return worker_manager


_sync_worker_manager = _wm  # backward compat for tests


def scan_tmux_sessions() -> dict[str, TmuxSessionDict]:
    return _wm().scan_tmux_sessions()


class ReuseAddrServer(ThreadingHTTPServer):
    allow_reuse_address = True


def do_teleport(
    name: str,
    target_host: str,
    target_cwd: str,
    full_sync: bool,
    chat_id: int | str,
    workers: object,
    is_teleback: bool = False,
) -> None:

    try:
        registered = workers.get_registered_sessions()
        session = registered.get(name, {})  # type: ignore[attr-defined]
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
        backend_name = get_worker_backend(name, session)
        source_host = get_worker_host(name)
        source_cwd = get_claude_session_cwd(name) or ""
        _log(
            _LOG_INFO,
            "teleport",
            f"{name}: source_host={source_host}, source_cwd={source_cwd}, target_host={target_host}, target_cwd={target_cwd}",
        )
        if not target_cwd:
            target_cwd = _remap_path(source_cwd, target_host) if target_host else source_cwd
        if target_cwd and target_cwd.startswith("~"):
            if target_host:
                rh = _get_remote_home(target_host)
                if rh:
                    target_cwd = rh + target_cwd[1:]
            else:
                target_cwd = os.path.expanduser(target_cwd)
        ensure_session_dir(name)
        state_file = SESSIONS_DIR / name / "teleport_state"

        def _save_phase(phase: int) -> None:
            _atomic_write(
                str(state_file),
                json.dumps(
                    {
                        "phase": phase,
                        "source_host": source_host,
                        "target_host": target_host,
                        "target_cwd": target_cwd,
                        "started_at": int(_clock.time()),
                    }
                ),
            )

        _save_phase(1)
        teleport_notify(chat_id, f"Stopping {name}...")
        session_id = stop_worker_for_teleport(name, tmux_name, source_host)
        _log(_LOG_INFO, "teleport", f"{name}: stopped, session_id={session_id}")
        if source_cwd and target_cwd:
            teleport_notify(chat_id, f"Syncing working directory...")
            _log(_LOG_INFO, "teleport", f"{name}: syncing {source_cwd} → {target_cwd}")
            ok = sync_working_directory(source_cwd, target_cwd, source_host, target_host, full_sync)
            _log(_LOG_INFO, "teleport", f"{name}: working dir sync ok={ok}")
            if not ok:
                teleport_rollback(
                    name,
                    tmux_name,
                    source_host,
                    source_cwd,
                    session_id,
                    backend_name,
                    chat_id,
                    "working directory sync failed",
                )
                return
        if session_id:
            teleport_notify(chat_id, "Syncing session transcript...")
            sync_session_transcript(session_id, source_cwd, target_cwd, source_host, target_host)
            _log(_LOG_INFO, "teleport", f"{name}: transcript sync done")
        if not is_teleback:
            teleport_notify(chat_id, "Syncing team config and hooks...")
            sw = sync_shared_repos(target_host, chat_id)
            hook_warnings = install_hooks_on_target(target_host)
            all_warnings = sw + hook_warnings
            if all_warnings:
                teleport_notify(
                    chat_id, f"Config sync completed with warnings:\n" + "\n".join(f"- {w}" for w in all_warnings)
                )
            _log(_LOG_WARN, "teleport", f"{name}: team config + hooks synced ({len(all_warnings)} warnings)")
        _save_phase(2)
        save_claude_session_cwd(name, target_cwd)
        clear_claude_session_id(name)
        _ensure_workspace_trusted_remote(target_cwd, target_host)
        teleport_notify(chat_id, f"Starting {name} on {target_host or 'local'}...")
        resume_id = session_id
        _log(
            _LOG_INFO,
            "teleport",
            f"{name}: calling _start_worker_on_target(target_cwd={target_cwd}, session_id={resume_id}, backend={backend_name})",
        )
        ok = start_worker_on_target(name, target_host, target_cwd, resume_id, backend_name)
        _log(_LOG_INFO, "teleport", f"{name}: _start_worker_on_target returned {ok}")
        if not ok:
            _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host)
            teleport_rollback(
                name, tmux_name, source_host, source_cwd, session_id, backend_name, chat_id, "failed to start on target"
            )
            return
        if is_teleback:
            _registry_clear_teleport(name)
        else:
            _registry_update_teleport(name, host=target_host, home_host=source_host, home_cwd=source_cwd)
        _remote_run(["tmux", "kill-session", "-t", tmux_name], host=source_host)
        if is_teleback:
            sync_worker_data_back(name, source_host)
        if not is_teleback:
            try:
                backend_obj = get_backend(backend_name)
                welcome = workers._build_welcome(name, backend_obj)  # type: ignore[attr-defined]
                _clock.sleep(DELAY_PROCESS_SETTLE)
                workers.send(name, welcome)  # type: ignore[attr-defined]
                if source_host != target_host:
                    ctx = _build_teleport_context(
                        name=name,
                        source_host=source_host,
                        target_host=target_host,
                        source_cwd=source_cwd,
                        session_id=session_id,
                    )
                    _clock.sleep(0.5)
                    workers.send(name, ctx)  # type: ignore[attr-defined]
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
        teleport_notify(chat_id, msg)
    except (
        subprocess.SubprocessError,
        ConnectionError,
        TimeoutError,
        TypeError,
        AttributeError,
        OSError,
        ValueError,
        KeyError,
    ) as e:
        _log(_LOG_ERROR, "teleport", f"Teleport failed: {e}", exc=e)
        teleport_notify(chat_id, f"Teleport failed: {e}")
        with _log_suppress(OSError, level=_LOG_DEBUG, tag="io:unknown"):
            state_file = SESSIONS_DIR / name / "teleport_state"
            state_file.unlink(missing_ok=True)


def _teleport_preflight_common(
    router: object, worker_name: str, chat_id: ChatId, action: str = "teleport"
) -> dict[str, object] | None:
    """Shared preflight for teleport/teleback: registry, busy, in-progress checks. Returns worker entry or None (already replied)."""
    registry = _load_registry()
    worker_entry = registry.get("workers", {}).get(worker_name)
    if not worker_entry:
        router.reply(chat_id, f"Worker '{worker_name}' not found in registry.")  # type: ignore[attr-defined]
        return None
    with watchdog.lock:
        worker_state = watchdog.worker_states.get(worker_name, ("UNKNOWN", "", 0))
    if worker_state[0] in ("BUSY_TOOL", "BUSY_THINKING"):
        router.reply(chat_id, f"{worker_name} is busy. Must be idle to {action}.\nWait for it to finish or /pause {worker_name} first.")  # type: ignore[attr-defined]
        return None
    teleport_file = SESSIONS_DIR / worker_name / "teleport_state"
    if teleport_file.exists():
        router.reply(chat_id, f"{worker_name} has a teleport in progress.")  # type: ignore[attr-defined]
        return None
    return worker_entry


def cmd_teleport(router: object, arg: str, chat_id: ChatId, check_only: bool = False) -> bool:
    if not arg:
        cmd_name = "/teleport-check" if check_only else "/teleport"
        router.reply(chat_id, f"Usage: {cmd_name} <worker> <host>[:/path]")  # type: ignore[attr-defined]
        return True
    parts = arg.split()
    worker_name = parts[0].lower()
    target_spec = " ".join(parts[1:]) if len(parts) > 1 else ""
    if not target_spec:
        router.reply(chat_id, "Usage: /teleport <worker> <host>[:/path] [--full]")  # type: ignore[attr-defined]
        return True
    full_sync = "--full" in target_spec
    target_spec = target_spec.replace("--full", "").strip()
    if ":" in target_spec and not target_spec.startswith("/"):
        target_host, target_cwd = target_spec.split(":", 1)
    else:
        target_host = target_spec
        target_cwd = ""
    machines = get_machine_catalog()
    if target_host in machines:
        machine = machines[target_host]
        if machine.ssh_target:
            target_host = machine.ssh_target
    worker_entry = _teleport_preflight_common(router, worker_name, chat_id, "teleport")
    if worker_entry is None:
        return True
    backend_name = worker_entry.get("backend", "claude")
    r = _remote_run(["echo", "ok"], host=target_host, timeout=TIMEOUT_REMOTE_CMD)
    if r.returncode != 0:
        router.reply(chat_id, f"Cannot reach {target_host} via SSH.")  # type: ignore[attr-defined]
        return True
    required_tools = ["claude", "tmux", "rsync"] + ([backend_name] if backend_name != "claude" else [])
    for tool in required_tools:
        if _resolve_remote_tool(tool, target_host) == tool:
            router.reply(chat_id, f"{tool} not found on {target_host}. Install it first.")  # type: ignore[attr-defined]
            return True
    tmux_name = f"{router.workers.tmux_prefix}{worker_name}"  # type: ignore[attr-defined]
    r = _remote_run(
        ["bash", "-c", f"tmux has-session -t {tmux_name} 2>/dev/null && echo exists || echo none"],
        host=target_host,
        timeout=TIMEOUT_TMUX_SEND,
    )
    if r.returncode == 0 and "exists" in r.stdout:
        teleport_notify(chat_id, f"⚠️ tmux session '{tmux_name}' already exists on {target_host} — will be replaced.")
    target_bridge_url = BRIDGE_PUBLIC_URL or BRIDGE_URL
    if "localhost" in target_bridge_url or "127.0.0.1" in target_bridge_url:
        router.reply(
            chat_id,  # type: ignore[attr-defined]
            "Cannot teleport: no reachable bridge URL. "
            "Set BRIDGE_PUBLIC_URL to this machine's network IP "
            "(e.g., BRIDGE_PUBLIC_URL=http://100.125.36.102:8271).",
        )
        return True
    r = _remote_run(
        ["curl", "-sf", "--connect-timeout", "5", f"{target_bridge_url}/"],
        host=target_host,
        timeout=TIMEOUT_REMOTE_CMD,
    )
    if r.returncode != 0:
        router.reply(
            chat_id,  # type: ignore[attr-defined]
            f"Target {target_host} cannot reach {target_bridge_url}. "
            f"Ensure BRIDGE_BIND=0.0.0.0 and network connectivity.",
        )
        return True
    r = _remote_run(
        ["test", "-f", ".claude/.credentials.json"],
        host=target_host,
        timeout=TIMEOUT_TMUX_SEND,
    )
    if r.returncode != 0:
        local_creds = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.exists(local_creds):
            _remote_run(["mkdir", "-p", ".claude"], host=target_host)
            _subprocess_runner.run(
                ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json"],
                capture_output=True,
                timeout=TIMEOUT_REMOTE_CMD,
            )
            _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host)
            teleport_notify(chat_id, "Synced credentials to target.")
        else:
            router.reply(
                chat_id,  # type: ignore[attr-defined]
                f"No Claude credentials on {target_host} or locally. " f"Run: ssh {target_host} claude login",
            )
            return True
    r = _remote_run(
        ["test", "-f", ".claude/hooks/claudecode.sh"],
        host=target_host,
        timeout=TIMEOUT_TMUX_SEND,
    )
    if r.returncode != 0:
        teleport_notify(chat_id, "Hooks missing on target — will install during teleport.")
    preflight_fails = run_teleport_preflight(target_host, worker_name, backend_name)
    if preflight_fails:
        router.reply(chat_id, f"Preflight failed:\n" + "\n".join(f" - {f}" for f in preflight_fails))  # type: ignore[attr-defined]
        return True
    if check_only:
        router.reply(chat_id, f"Preflight OK — {worker_name} is clear to teleport to {target_host}.")  # type: ignore[attr-defined]
        return True
    router.reply(chat_id, f"Teleporting {worker_name} to {target_host}...")  # type: ignore[attr-defined]
    threading.Thread(
        target=do_teleport,
        args=(worker_name, target_host, target_cwd, full_sync, chat_id, router.workers),  # type: ignore[attr-defined]
        name=f"teleport-{worker_name}",
        daemon=True,
    ).start()
    return True


def cmd_alert(router: object, arg: str, chat_id: ChatId) -> bool:
    """Manage health alerts: /alert [status|snooze <signal>|history]"""
    parts = arg.strip().split()
    sub = parts[0].lower() if parts else "status"
    if sub == "status":
        router.reply(chat_id, _alert_status())  # type: ignore[attr-defined]
        return True
    if sub == "snooze":
        signal = parts[1] if len(parts) > 1 else ""
        if not signal:
            router.reply(chat_id, "Usage: /alert snooze <signal>\nActive: " + ", ".join(_alert_active) or "none")  # type: ignore[attr-defined]
            return True
        duration = _ALERT_SNOOZE
        if len(parts) > 2:
            with contextlib.suppress(ValueError):
                duration = int(parts[2]) * 60  # minutes to seconds
        result = _alert_snooze(signal, duration)
        router.reply(chat_id, result)  # type: ignore[attr-defined]
        return True
    if sub == "history":
        if _health_channel_id and _health_channel_id in relay_store.channels:
            ch = relay_store.channels[_health_channel_id]
            msgs = ch.get("messages", [])
            if not msgs:
                router.reply(chat_id, "No alert history.")  # type: ignore[attr-defined]
                return True
            # Show last 10 alerts
            recent = msgs[-10:]
            lines = [f"Last {len(recent)} alerts (channel {_health_channel_id[:8]}):"]
            for m in recent:
                ts = m.get("ts", "?")
                text = m.get("text", "?")
                lines.append(f"  [{ts}] {text[:100]}")
            router.reply(chat_id, "\n".join(lines))  # type: ignore[attr-defined]
        else:
            router.reply(chat_id, "Health channel not active. Set BRIDGE_ALERT_WORKERS and restart.")  # type: ignore[attr-defined]
        return True
    router.reply(chat_id, "Usage: /alert [status|snooze <signal> [minutes]|history]")  # type: ignore[attr-defined]
    return True


def cmd_teleback(router: object, arg: str, chat_id: ChatId) -> bool:
    parts = arg.split()
    worker_name = parts[0].lower() if parts else ""
    full_sync = "--full" in parts
    if not worker_name:
        router.reply(chat_id, "Usage: /teleback <worker> [--full]")  # type: ignore[attr-defined]
        return True
    worker = _teleport_preflight_common(router, worker_name, chat_id, "teleback")
    if worker is None:
        return True
    current_host = worker.get("host")
    home_host = worker.get("home_host")
    home_cwd = worker.get("home_cwd")
    if current_host is None and home_cwd is None:
        router.reply(chat_id, f"{worker_name} hasn't been teleported.")  # type: ignore[attr-defined]
        return True
    target_host = home_host
    target_cwd = home_cwd or get_claude_session_cwd(worker_name)
    if target_host:
        r = _remote_run(["echo", "ok"], host=target_host, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            router.reply(chat_id, f"Cannot reach home host {target_host} via SSH.")  # type: ignore[attr-defined]
            return True
        for tool in ("claude", "tmux", "rsync"):
            tool_path = _resolve_remote_tool(tool, target_host)
            if tool_path == tool:
                router.reply(chat_id, f"{tool} not found on {target_host}. Install it first.")  # type: ignore[attr-defined]
                return True
    if current_host:
        r = _remote_run(["echo", "ok"], host=current_host, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            router.reply(chat_id, f"Cannot reach {current_host} where {worker_name} currently is.")  # type: ignore[attr-defined]
            return True
    if not full_sync:
        conflicts = check_teleback_conflicts(worker_name, current_host, target_cwd)
        if conflicts:
            router.reply(
                chat_id,  # type: ignore[attr-defined]
                f"Teleback conflict detected for {worker_name}:\n"
                + "\n".join(f"  {c}" for c in conflicts)
                + "\n\nUse /teleback "
                + worker_name
                + " --full to force sync "
                "(remote overwrites local).",
            )
            return True
    dest_label = home_host or "local"
    router.reply(chat_id, f"Bringing {worker_name} back to {dest_label}...")  # type: ignore[attr-defined]
    threading.Thread(
        target=do_teleport,
        args=(worker_name, target_host, target_cwd, full_sync, chat_id, router.workers, True),  # type: ignore[attr-defined]
        name=f"teleback-{worker_name}",
        daemon=True,
    ).start()
    return True


def restart_remote_worker(
    name: str, backend_name: str, backend: Backend, tmux_name: str, host: str, mode: str, workers: object
) -> tuple[bool, str | None]:

    resume_id = ""
    target_cwd = get_claude_session_cwd(name)
    if target_cwd and host:
        target_cwd = _remap_path(target_cwd, host)
    _log(_LOG_INFO, "_restart_remote", f"{name}: mode={mode}, host={host}, tmux={tmux_name}, cwd={target_cwd}")
    if mode == "resume":
        resume_id = get_claude_session_id(name, authoritative=False)
        if not resume_id:
            resume_id = get_claude_session_id(name, authoritative=True)
        _log(_LOG_INFO, "_restart_remote", f"{name}: resume_id={resume_id}")
    else:
        session_dir = SESSIONS_DIR / name
        session_dir.mkdir(parents=True, exist_ok=True)
        cleared = list(session_dir.glob("*_session_id"))
        for f in cleared:
            f.unlink()
        _clear_hook_failures(name)
        _log(_LOG_INFO, "_restart_remote", f"{name}: cleared {len(cleared)} session files for relaunch")
    if tmux_exists(tmux_name, host=host):
        _log(_LOG_INFO, "_restart_remote", f"{name}: stopping remote tmux {tmux_name}")
        stop_worker_for_teleport(name, tmux_name, host=host)
        _remote_run(["tmux", "kill-session", "-t", tmux_name], host=host)
        _clock.sleep(DELAY_RETRY)
    else:
        _log(_LOG_INFO, "_restart_remote", f"{name}: tmux {tmux_name} not found on {host}")
    if mode == "resume":
        cached = get_claude_session_id(name, authoritative=False)
        resume_id = cached or (get_claude_session_id(name, authoritative=True) or resume_id)
        _log(_LOG_INFO, "_restart_remote", f"{name}: post-stop resume_id={resume_id}")
    if resume_id and target_cwd and host:
        remote_home = _get_remote_home(host)
        if remote_home:
            cwd_slug = target_cwd.replace("/", "-")
            session_file = f"{remote_home}/.claude/projects/{cwd_slug}/{resume_id}.jsonl"
            check = _remote_run(["test", "-f", session_file], host=host, timeout=TIMEOUT_TMUX_SEND)
            if check.returncode != 0:
                _log(
                    _LOG_INFO,
                    "_restart_remote",
                    f"{name}: session {resume_id} NOT found at {session_file}, starting fresh",
                )
                resume_id = ""
                session_dir = SESSIONS_DIR / name
                session_dir.mkdir(parents=True, exist_ok=True)
                for f in session_dir.glob("*_session_id"):
                    f.unlink()
            else:
                _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} validated at {session_file}")
    _ensure_workspace_trusted_remote(target_cwd, host)
    _log(
        _LOG_INFO,
        "_restart_remote",
        f"{name}: calling _start_worker_on_target(cwd={target_cwd}, resume={resume_id}, backend={backend_name})",
    )
    ok = start_worker_on_target(name, host, target_cwd, resume_id, backend_name, skip_session_sync=True)
    if not ok:
        _log(_LOG_WARN, "_restart_remote", f"{name}: _start_worker_on_target FAILED")
        return False, f"Failed to restart {name} on {host}"
    welcome = workers._build_welcome(name, backend)  # type: ignore[attr-defined]
    if backend.is_interactive:
        started = False
        for _ in range(10):
            _clock.sleep(DELAY_STARTUP)
            if is_claude_running(tmux_name, host=host):
                started = True
                break
        if started:
            workers.send(name, welcome)  # type: ignore[attr-defined]
        else:
            _log(_LOG_WARN, "_restart_remote", f"{name}: Claude did not start within 10s, skipping welcome")
    _log(_LOG_INFO, "_restart_remote", f"{name}: restarted successfully (mode={mode})")
    return True, None


class CommandRouter:

    def _restart_remote_worker(
        self, name: str, backend_name: str, backend: Backend, tmux_name: str, host: str, mode: str
    ) -> tuple[bool, str | None]:
        return restart_remote_worker(name, backend_name, backend, tmux_name, host, mode, self.workers)

    def _extract_reply_media(self, reply_to: TelegramMessageDict, target_worker: str) -> str | None:
        file_id, media_label = _extract_file_id(reply_to)
        if reply_to.get("document") and media_label == "file":
            media_label = f"file: {reply_to['document'].get('file_name', 'unknown')}"
        elif reply_to.get("sticker"):
            media_label = f"sticker: {reply_to['sticker'].get('emoji', '')}"
        elif reply_to.get("voice"):
            media_label = "voice message"
        if not file_id:
            return None
        local_path = download_telegram_file(file_id, target_worker)
        if not local_path:
            return None
        if reply_to.get("voice"):
            transcript = transcribe_voice(local_path)
            if transcript:
                return transcript
        return f"Manager forwarded {media_label}: {local_path}"

    def _worker_from_reply(self, msg: TelegramMessageDict | None) -> str | None:
        reply_to = msg.get("reply_to_message") if msg else None
        if not reply_to:
            return None
        reply_text = _extract_msg_text(reply_to)
        if not reply_text:
            return None
        first_line = reply_text.split("\n", 1)[0]
        if first_line.endswith(":"):
            candidate = first_line[:-1].strip().lower()
            registered = self.workers.get_registered_sessions()
            if candidate in registered:
                return candidate
        return None

    def _handle_media_group_flush(self, group_id: str) -> None:
        with media_groups.lock:
            group = media_groups.buffer.pop(group_id, None)
        if not group:
            return
        items = group["items"]
        caption = group.get("caption", "")
        first_msg = items[0] if items else {}
        chat_id = first_msg.get("chat", {}).get("id")
        msg_id = first_msg.get("message_id")
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
            target = state.active
        all_paths = []
        for msg in items:
            file_id, media_type = _extract_file_id(msg)
            if file_id:
                local_path = download_telegram_file(file_id, target)
                if local_path:
                    all_paths.append((local_path, media_type))
        if not all_paths:
            return
        path_lines = "\n".join(f"Manager sent {mt}: `{p}`" for p, mt in all_paths)
        full_text = f"{caption}\n\n{path_lines}" if caption else path_lines
        self._route_media_message(full_text, caption, chat_id, msg_id, msg=first_msg)

    def _resolve_media_target(self, caption: str, msg: TelegramMessageDict) -> str | None:
        if caption:
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                return targets[0]
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            return reply_worker
        return state.active

    def _route_media_message(
        self,
        media_text: str,
        caption: str,
        chat_id: ChatId | None,
        msg_id: int | None,
        msg: TelegramMessageDict | None = None,
    ) -> None:
        if caption:
            unknown_mentions = self.unknown_at_mentions(caption)
            if unknown_mentions:
                self.reply(chat_id, self.format_unknown_mentions_warning(unknown_mentions))
                return
            targets, _ = self.parse_at_mentions(caption)
            if targets:
                statuses = [self._route_mention(name, media_text, chat_id, msg_id) for name in targets]
                warning = _format_offline_warning(statuses)
                if warning:
                    self.reply(chat_id, warning)
                return
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            self.route_message(reply_worker, media_text, chat_id, msg_id, one_off=True)
            return
        self.route_to_active(media_text, chat_id, msg_id)

    def _reset_mention_streak(self) -> None:
        _last_mention.target = None
        _last_mention.count = 0
        _last_mention.ts = 0

    def _handle_mention_routing(
        self,
        targets: list[str],
        message: str,
        text: str,
        chat_id: ChatId | None,
        msg_id: int | None,
        reply_to: TelegramMessageDict | None,
        reply_context: str,
        reply_context_ts: int | None,
    ) -> None:
        if len(targets) == 1 and re.fullmatch(r"\s*@[a-zA-Z0-9-]+\s*", text):
            target = targets[0]
            registered = self.workers.get_registered_sessions()
            if target in registered:
                state.active = target
                save_last_active(target)
            else:
                self.reply(chat_id, f"Can't focus guest {target}.")
            self._reset_mention_streak()
            return
        if reply_context:
            message = self.format_reply_context(message, reply_context, reply_context_ts)
        final_msg = message
        if reply_to:
            reply_media = self._extract_reply_media(reply_to, targets[0])
            if reply_media:
                final_msg = f"{message}\n\n{reply_media}" if message else reply_media
        statuses = [self._route_mention(name, final_msg, chat_id, msg_id) for name in targets]
        warning = _format_offline_warning(statuses)
        if warning:
            self.reply(chat_id, warning)
        registered = self.workers.get_registered_sessions()
        now = _clock.time()
        if len(targets) == 1 and targets[0] in registered:
            target = targets[0]
            if _last_mention.target == target and now - _last_mention.ts <= 60:
                _last_mention.count += 1
            else:
                _last_mention.target = target
                _last_mention.count = 1
            _last_mention.ts = now
            if _last_mention.count >= 2 and state.active != target:
                state.active = target
                save_last_active(target)
                self.reply(chat_id, f"Switched to {target} (you mentioned them twice).")
        else:
            self._reset_mention_streak()

    _mention_re = re.compile(r"(?<![a-zA-Z0-9._+\-])@([a-zA-Z0-9-]+)")

    def _known_names(self) -> set[str]:
        registered = self.workers.get_registered_sessions()
        with guest_store.lock:
            guest_names = {g["name"] for g in guest_store.guests.values() if not guest_is_expired(g["expires_at_unix"])}
        return set(registered.keys()) | guest_names

    def parse_at_mentions(self, text: str) -> tuple[list[str], str]:
        if not text:
            return [], ""
        known = self._known_names()
        found = []
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name in known and name not in found:
                found.append(name)
        return found, text

    def unknown_at_mentions(self, text: str) -> list[str]:
        if not text:
            return []
        known = self._known_names() | {"all"}
        unknown = []
        for match in self._mention_re.finditer(text):
            name = match.group(1).lower()
            if name not in known and name not in unknown:
                unknown.append(name)
        return unknown

    def format_unknown_mentions_warning(self, unknown_mentions: list[str]) -> str:
        known = sorted(self._known_names())
        parts = []
        for name in unknown_mentions:
            suggestions = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
            if suggestions:
                parts.append(f"@{name}. Did you mean {', '.join('@' + s for s in suggestions)}?")
            else:
                parts.append(f"@{name}.")
        return f"⚠️ Unknown: {' '.join(parts)}"

    def _route_mention(
        self, name: str, message: str, chat_id: ChatId | None, msg_id: int | None
    ) -> MentionRouteResult | None:
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
                guest_store.inboxes[name] = guest_inbox_append(inbox, cast("GuestInboxMessageDict", msg_obj))
            return {"name": name, "status": "sent"}
        return {"name": name, "status": "unknown"}

    def get_reply_context(self, reply_msg: TelegramMessageDict) -> tuple[str, int | None]:
        if not reply_msg:
            return "", None
        text = _extract_msg_text(reply_msg)
        ts = reply_msg.get("date")
        return text, ts

    def format_reply_context(self, reply_text: str, context_text: str, context_ts: int | None = None) -> str:
        reply_text = (reply_text or "").strip()
        context_text = (context_text or "").strip()
        if context_text:
            ts_str = ""
            if context_ts:
                ts_str = f" at {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(context_ts))}"
            return (
                "Manager reply:\n" f"{reply_text}\n\n" f"Context (your previous message{ts_str}):\n" f"{context_text}"
            )
        return f"Manager reply:\n{reply_text}"

    def __init__(self, transport: MessageTransport | None, workers: WorkerManager) -> None:
        if transport is not None and not isinstance(transport, MessageTransport):
            transport = _LegacyTransportAdapter(transport)
        self.transport: MessageTransport | None = transport
        self.workers: WorkerManager = workers
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
            "/teleback": lambda arg, cid, mid: self.cmd_teleback(arg, cid),
            "/alert": lambda arg, cid, mid: self.cmd_alert(arg, cid),
        }

    def reply(self, chat_id: ChatId | None, text: str, outcome: str | None = None) -> None:
        if self.transport is not None and chat_id is not None:
            self.transport.send_text(chat_id, text)

    def send_startup_message(self, chat_id: ChatId | None) -> None:
        registered = self.workers.get_registered_sessions()
        sessions = list(registered.keys())
        active = state.active
        lines = ["I'm online and ready."]
        if sessions:
            lines.append(f"Team: {', '.join(sessions)}")
            if active:
                lines.append(f"Focused: {active}")
        else:
            lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")
        self.reply(chat_id, "\n".join(lines))

    def handle_message(self, update: TelegramUpdate) -> None:
        global admin_chat_id
        _log(_LOG_INFO, "handle_message", f"ENTER update_id={update.get('update_id')}")
        incoming = IncomingMessage.from_update(update)
        msg = incoming.raw_msg
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        _log(_LOG_INFO, "handle_message", f"chat_id={chat_id} admin={admin_chat_id} text={text[:40]!r}")
        media_group_id = msg.get("media_group_id")
        has_media = (
            incoming.photo
            or incoming.document
            or incoming.animation
            or incoming.video
            or incoming.audio
            or incoming.voice
            or incoming.video_note
            or incoming.sticker
        )
        if media_group_id and has_media and chat_id:
            if not self._check_admin(chat_id):
                return
            self._buffer_media_group(media_group_id, msg, text)
            return
        if has_media and chat_id and self._handle_single_media(incoming):
            return
        if text and chat_id:
            self._route_text_message(incoming)

    def _check_admin(self, chat_id: ChatId | None) -> bool:
        global admin_chat_id
        if chat_id is None:
            return False
        if admin_chat_id is None:
            admin_chat_id = chat_id  # type: ignore[assignment]
            return True
        return chat_id == admin_chat_id

    def _buffer_media_group(self, media_group_id: str, msg: TelegramMessageDict, text: str) -> None:
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
            t = threading.Timer(_MEDIA_GROUP_WAIT, self._handle_media_group_flush, args=[media_group_id])
            t.name = f"media-flush-{media_group_id}"
            t.daemon = True
            group["timer"] = t
            t.start()

    def _handle_single_media(self, incoming: IncomingMessage) -> bool:
        global admin_chat_id
        msg = incoming.raw_msg
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        media_dict: dict[str, object] = {}
        for attr in ("animation", "photo", "document", "video", "audio", "voice", "video_note", "sticker"):
            val = getattr(incoming, attr, None)
            if val:
                media_dict[attr] = val
        if incoming.doc_is_image and incoming.document:
            media_dict["document"] = incoming.document
        file_id, media_label = _extract_file_id(media_dict)
        if not file_id:
            return False
        if not self._check_admin(chat_id):
            return True
        if not state.active and not self.parse_at_mentions(text)[0]:
            self.reply(chat_id, "No focused worker. Use /focus <name> or @worker in caption.")
            return True
        download_target = self._resolve_media_target(text, msg)
        local_path = download_telegram_file(file_id, download_target)
        if not local_path:
            self.reply(chat_id, f"Could not download {media_label}. Try again.")
            return True
        media_text = self._format_media_text(incoming, local_path, media_label)
        if media_text is None:
            return True
        if text:
            media_text = f"{text}\n\n{media_text}"
        self._route_media_message(media_text, text, chat_id, msg_id, msg=msg)
        return True

    def _format_media_text(self, incoming: IncomingMessage, local_path: str, media_label: str) -> str | None:
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        msg = incoming.raw_msg
        if media_label in ("GIF", "image"):
            return f"Manager sent {media_label}: `{local_path}`"
        if media_label == "file" and incoming.document:
            d = incoming.document
            return f"Manager sent file: {d.get('file_name', 'unknown')} ({format_file_size(d.get('file_size', 0))}, {d.get('mime_type', 'unknown')})\nPath: `{local_path}`"
        if incoming.audio:
            return f"Manager sent audio: {incoming.audio.get('title', incoming.audio.get('file_name', 'audio'))} ({incoming.audio.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.voice:
            transcript = transcribe_voice(local_path)
            if transcript:
                self.reply(chat_id, f"🎤 _{transcript}_")
                self._route_media_message(
                    f"{text}\n\n{transcript}" if text else transcript, text or transcript, chat_id, msg_id, msg=msg
                )
                return None
            return f"Manager sent voice message: ({incoming.voice.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.video:
            return f"Manager sent video: {incoming.video.get('file_name', 'video')} ({incoming.video.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.video_note:
            return f"Manager sent video note: ({incoming.video_note.get('duration', 0)}s)\nPath: `{local_path}`"
        if incoming.sticker:
            return f"Manager sent sticker: {incoming.sticker.get('emoji', '')}\nPath: {local_path}"
        return f"Manager sent media: {local_path}"

    def _route_text_message(self, incoming: IncomingMessage) -> None:
        global admin_chat_id
        text = incoming.text
        chat_id = incoming.chat_id
        msg_id = incoming.msg_id
        msg = incoming.raw_msg
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
        if text.startswith("/") and self.handle_command(text, chat_id, msg_id):
            _last_mention.target = None
            _last_mention.count = 0
            return
        if re.match(r"^\s*@all(?:\s|[:,]|$)", text, re.IGNORECASE):
            self.route_to_all(text, chat_id, msg_id)
            self._reset_mention_streak()
            return
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
            self._handle_mention_routing(
                targets, message, text, chat_id, msg_id, reply_to, reply_context, reply_context_ts
            )
            return
        reply_worker = self._worker_from_reply(msg)
        if reply_worker:
            routed_text = text
            if reply_context:
                routed_text = self.format_reply_context(text, reply_context, reply_context_ts)
            self.route_message(reply_worker, routed_text, chat_id, msg_id, one_off=True)
            self._reset_mention_streak()
            return
        self._reset_mention_streak()
        routed_text = text
        if reply_context:
            routed_text = self.format_reply_context(text, reply_context, reply_context_ts)
        self.route_to_active(routed_text, chat_id, msg_id)

    def handle_command(self, text: str, chat_id: ChatId | None, msg_id: int | None) -> bool:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        handler = self._commands.get(cmd)
        if handler:
            return handler(arg, chat_id or 0, msg_id or 0)
        if cmd in BLOCKED_COMMANDS:
            self.reply(chat_id, f"{cmd} is interactive and not supported here.", outcome="Needs decision")
            return True
        worker_name = cmd[1:]
        registered = self.workers.get_registered_sessions()
        if worker_name in registered:
            prev_focus = state.active
            state.active = worker_name
            save_last_active(worker_name)
            if not arg:
                return True
            if prev_focus != worker_name and self.transport and chat_id is not None:
                self.transport.send_text(chat_id, f"Now talking to {worker_name.capitalize()}.")
            self.route_message(worker_name, arg, chat_id, msg_id, one_off=False)
            return True
        return False

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

    def route_message(
        self, session_name: str, text: str, chat_id: ChatId | None, msg_id: int | None, one_off: bool = False
    ) -> None:
        registered = self.workers.get_registered_sessions()
        session = registered.get(session_name)
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
        backend_name = get_worker_backend(session_name, session)
        backend = get_backend(backend_name)
        shortcut = text.strip().lower()
        if backend.is_interactive and shortcut in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "skip", "cancel"):
            tmux_name = session.get("tmux", f"{self.workers.tmux_prefix}{session_name}")
            host = get_worker_host(session_name)
            _, _, raw_lines = _read_tmux_activity(tmux_name, host=host)
            if raw_lines:
                details = _extract_question_details(raw_lines)
                if details and _send_interactive_reply(tmux_name, shortcut, details, host=host):
                    action = f"Skipped" if shortcut in ("skip", "cancel") else f"Picked option {shortcut}"
                    self.reply(chat_id, f"{action}.")
                    return
        if not backend.is_interactive and chat_id is not None and not try_set_pending(session_name, chat_id):
            self.reply(
                chat_id,
                f"{session_name.capitalize()} is still working on the previous request. Wait for a response or use /pause.",
            )
            return
        if not text.startswith("Manager sent "):
            text = f"manager: {text}"
        _log(_LOG_INFO, "dispatch", f"chat_id={chat_id} -> {session_name}: {text[:50]}...")
        if backend.is_interactive and chat_id is not None:
            set_pending(session_name, chat_id)
        if chat_id is not None:
            _task_pool.submit(send_typing_loop, chat_id, session_name)
        try:
            send_ok = self.workers.send(session_name, text, chat_id, session)
        except Exception:
            send_ok = False
            _log(_LOG_ERROR, "dispatch", f"Exception sending to {session_name}", exc=sys.exc_info()[1])
        if send_ok:
            health.ok("dispatch")
        else:
            health.error("dispatch")
        if not send_ok:
            clear_pending(session_name)
            self.reply(
                chat_id, f"Could not send to {session_name.capitalize()}. Try /restart.", outcome="Needs decision"
            )
            return
        if msg_id and send_ok:
            host = get_worker_host(session_name)
            if (
                (not backend.is_interactive or tmux_prompt_empty(session.get("tmux", ""), host=host))
                and self.transport
                and chat_id is not None
            ):
                self.transport.set_reaction(chat_id, msg_id, [{"type": "emoji", "emoji": "👀"}])


PostRouteHandler = Callable[["Handler", bytes, re.Match[str] | None], None]
GetRouteHandler = Callable[["Handler", ParseResult, re.Match[str] | None], None]


class EndpointRouter:
    def __init__(self) -> None:
        self._exact: dict[str, dict[str, object]] = {"POST": {}, "GET": {}, "DELETE": {}}
        self._pats: dict[str, list[tuple[re.Pattern[str], object]]] = {"POST": [], "GET": [], "DELETE": []}

    def add(self, method: str, path: str, handler: object) -> None:
        self._exact[method][path] = handler

    def add_pattern(self, method: str, pattern: str, handler: object) -> None:
        self._pats[method].append((re.compile(pattern), handler))

    def resolve(self, method: str, path: str) -> tuple[object | None, re.Match[str] | None]:
        h = self._exact[method].get(path)
        if h:
            return h, None
        for regex, ph in self._pats[method]:
            m = regex.match(path)
            if m:
                return ph, m
        return None, None


class Handler(BaseHTTPRequestHandler):
    def _parse_body(self, body: bytes) -> dict[str, object] | None:
        try:
            return cast(dict[str, object], json.loads(body)) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_error_json(400, "invalid JSON")
            return None

    # Guest, channel, relay, PR, transcript, notify, forge, checkin, health
    # delegations are auto-generated after the class via _HANDLER_DELEGATES.

    def _send_status(self, code: int) -> None:
        self.send_response(code)
        self.end_headers()

    def _send_json(self, code: int, data: Mapping[str, object]) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code: int, message: str) -> None:
        self._send_json(code, {"ok": False, "error": message})

    def _send_html(self, body: bytes, status: int = 200) -> None:
        use_gzip = "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024
        if use_gzip:
            body = gzip.compress(body, compresslevel=6)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def _send_unknown_endpoint(self, method: str, path: str) -> None:
        self._send_json(
            404,
            {
                "error": f"Unknown endpoint: {method} {path}",
                "available_endpoints": API_ENDPOINTS,
                "hint": "Messages from manager arrive as prompts. There is no polling endpoint.",
            },
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve("POST", parsed.path)
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
            update_types = [k for k in update if k != "update_id"]
            msg = update.get("message", {})
            text = msg.get("text", "") or msg.get("caption", "")
            _log(
                _LOG_INFO,
                "webhook",
                f"update_id={update.get('update_id')}, types={update_types}, text={repr(text[:50]) if text else '(none)'}",
            )
            if "message" in update:

                def _safe_handle(upd: TelegramUpdate) -> None:
                    with _log_suppress(
                        json.JSONDecodeError,
                        KeyError,
                        ValueError,
                        TypeError,
                        level=_LOG_ERROR,
                        tag="webhook",
                        msg="handle_message CRASH: ",
                        exc_info=True,
                    ):
                        command_router.handle_message(upd)

                _message_pool.submit(_safe_handle, update)
        except (json.JSONDecodeError, KeyError) as e:
            _log(_LOG_ERROR, "webhook", f"parse error: {e}", exc=e)

    def handle_connectors_status(self) -> None:
        self._send_json(200, _get_connectors_status())

    def handle_connectors_restart(self, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None:
            return
        name = str(data.get("name", "")).strip().lower()
        if not name:
            self._send_error_json(400, "Missing 'name' (gmail or github)")
            return
        ok, msg = _restart_connector(name)
        self._send_json(200 if ok else 500, {"ok": ok, "name": name, "message": msg})

    def handle_send_endpoint(self, body: bytes = b"") -> None:
        data = self._parse_body(body)
        if data is None:
            return
        worker = _str_field(data, "worker").strip()
        message = _str_field(data, "message") or _str_field(data, "text")
        sender = _str_field(data, "from", "system").strip() or "system"
        if not worker:
            self._send_error_json(400, "Missing worker")
            return
        if not isinstance(message, str) or not message.strip():
            self._send_error_json(400, "Missing message")
            return
        prefixed = f"{sender}: {message}"
        delivered = send_to_worker(worker, prefixed)
        status = 200 if delivered else 404
        self._send_json(
            status,
            {
                "ok": delivered,
                "worker": worker,
                "delivered": delivered,
                "error": None if delivered else "Worker not found or not reachable",
            },
        )

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
            data = cast(HookResponseBody, json.loads(body))
            session_name = _str_field(data, "session")
            text = _str_field(data, "text")
            if not session_name or not text:
                self._send_text(400, "Missing session or text")
                return
            source_error = self._validate_response_source(data, session_name)
            if source_error:
                _log(_LOG_WARN, "hook", f"Rejected /outputs for {session_name}: {source_error}")
                self._send_text(403, source_error)
                return
            chat_id_file = get_chat_id_file(session_name)
            if chat_id_file.exists():
                chat_id = chat_id_file.read_text().strip()
            elif admin_chat_id is not None:
                chat_id = str(admin_chat_id)
                ensure_session_dir(session_name)
                _tmp_hk = chat_id_file.with_suffix(".tmp")
                _tmp_hk.write_text(chat_id)
                _tmp_hk.chmod(0o600)
                os.replace(str(_tmp_hk), str(chat_id_file))
                _log(
                    _LOG_INFO,
                    "hook",
                    f"Hook response: auto-created chat_id for session '{session_name}' from admin_chat_id",
                )
            else:
                _log(_LOG_WARN, "hook", f"Hook response: no chat_id for session '{session_name}'")
                self._send_text(404, "No chat_id for session")
                return
            if len(text.strip()) <= 5:
                source_ip = self.client_address[0] if self.client_address else "unknown"
                hook_sid = _str_field(data, "session_id")
                escape_flag = _bool_field(data, "escape")
                _log(
                    _LOG_DEBUG,
                    "hook",
                    f"Hook response DEBUG: {session_name} -> chat {chat_id}, "
                    f"text={text!r}, len={len(text)}, "
                    f"source={_str_field(data, 'source', 'hook')}, "
                    f"session_id={hook_sid[:12] if hook_sid else 'none'}, "
                    f"escape={escape_flag}, ip={source_ip}",
                )
            hook_sid = _str_field(data, "session_id") or ""
            # Debounce: buffer this response; if another arrives within the
            # window it replaces this one so only the final text is sent.
            _debounce_hook_response(session_name, text, int(chat_id), hook_sid)
            self._send_text(200, "OK")
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as e:
            _log(_LOG_ERROR, "bridge", f"Hook response error: {e}")
            self._send_text(500, str(e))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve("GET", parsed.path)
        if handler:
            handler(self, parsed, match)
            return
        if parsed.path == "/":
            self._send_json(
                200,
                {
                    "name": "claudecode-telegram bridge",
                    "endpoints": API_ENDPOINTS,
                    "note": "Messages from manager arrive as prompts. There is no polling endpoint.",
                },
            )
            return
        self._send_unknown_endpoint("GET", parsed.path)

    def do_HEAD(self) -> None:
        if urlparse(self.path).path == "/":
            self._send_status(200)
        else:
            self._send_status(404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        handler, match = _endpoint_router.resolve("DELETE", parsed.path)
        if handler:
            handler(self, parsed, match)
            return
        self._send_unknown_endpoint("DELETE", parsed.path)

    def handle_workers_endpoint(self, parsed: ParseResult | None = None) -> None:
        try:
            caller_from = None
            if parsed is not None:
                caller_from = parse_qs(parsed.query).get("from", [None])[0]
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
            _log(_LOG_ERROR, "bridge", f"Machines endpoint error: {e}")
            self._send_json(500, {"error": str(e)})

    def handle_health_tunnel_endpoint(self) -> None:
        if tunnel_manager is not None:
            data = tunnel_manager.status()
        else:
            data = {"mode": "none", "state": "disabled", "tunnel_url": "", "polling_active": False}
        self._send_json(200, data)


def _install_delegates(cls: type, names: list[str]) -> None:
    for name in names:
        fn = globals()[name]

        def _make(f: Callable = fn, n: str = name) -> Callable:
            def _d(self: object, *a: object, **kw: object) -> object:
                return f(self, *a, **kw)

            _d.__name__ = n
            _d.__qualname__ = f"{cls.__name__}.{n}"
            return _d

        setattr(cls, name, _make())


_install_delegates(
    Handler,
    [
        "_guest_auth",
        "_channel_guest_auth",
        "_relay_get_token",
        "_relay_auth_and_parse",
        "handle_guest_register",
        "handle_guest_send",
        "handle_guest_reply",
        "handle_guest_inbox",
        "handle_guest_status",
        "handle_guests_list",
        "handle_guest_disconnect",
        "handle_channel_create",
        "handle_channel_members",
        "handle_channel_send",
        "handle_channel_messages",
        "handle_channels_list",
        "handle_channel_delete",
        "handle_relay_get",
        "handle_relay_send",
        "handle_relay_reply",
        # PR review handlers (handle_pr_*) removed — served by review/src/serve.ts
        # Transcript handler (handle_transcript_endpoint) removed — served by rewind/
        "handle_notify",
        "handle_health_alert",
        "handle_forge_register",
        "handle_checkin_endpoint",
        "handle_health_workers_endpoint",
    ],
)
_install_delegates(
    CommandRouter,
    [
        "cmd_teleport",
        "cmd_alert",
        "cmd_teleback",
        "cmd_hire",
        "cmd_end",
        "cmd_restart",
        "cmd_relay",
        "cmd_pilot",
        "cmd_rewind",
        "cmd_pr_review",
        "cmd_focus",
        "cmd_team",
        "cmd_status",
    ],
)


def _mg(m: re.Match[str] | None, n: int = 1) -> str:
    return m.group(n) if m else ""


def _mg2(m: re.Match[str] | None) -> str:
    return m.group(2) if m else None


def _setup_endpoint_routes() -> None:
    r = _endpoint_router
    P, G, D = "POST", "GET", "DELETE"
    r.add(P, "/outputs", lambda h, b, _m: h.handle_hook_response(b))
    r.add(P, "/notifications", lambda h, b, _m: h.handle_notify(b))
    r.add(P, "/messages", lambda h, b, _m: h.handle_send_endpoint(b))
    # PR review routes (comments, merges) removed — served by review/src/serve.ts
    r.add(P, "/workers", lambda h, b, _m: h.handle_forge_register(b))
    r.add(P, "/guests", lambda h, b, _m: h.handle_guest_register(b))
    r.add(P, "/channels", lambda h, b, _m: h.handle_channel_create(b))
    r.add(P, "/alerts", lambda h, b, _m: h.handle_health_alert(b))
    r.add(P, "/connectors/restarts", lambda h, b, _m: h.handle_connectors_restart(b))
    r.add_pattern(P, r"^/guests/send", lambda h, b, m: h.handle_guest_send(b))
    r.add_pattern(P, r"^/guests/reply", lambda h, b, m: h.handle_guest_reply(b))
    r.add_pattern(P, r"^/channels/([^/]+)/members$", lambda h, b, m: h.handle_channel_members(_mg(m), b))
    r.add_pattern(P, r"^/channels/([^/]+)/send$", lambda h, b, m: h.handle_channel_send(_mg(m), b))
    r.add_pattern(P, r"^/relay/([^/]+)/send$", lambda h, b, m: h.handle_relay_send(_mg(m), b))
    r.add_pattern(P, r"^/relay/([^/]+)/reply$", lambda h, b, m: h.handle_relay_reply(_mg(m), b))
    r.add(G, "/guests", lambda h, p, _m: h.handle_guests_list())
    r.add(G, "/workers", lambda h, p, _m: h.handle_workers_endpoint(p))
    r.add(G, "/machines", lambda h, p, _m: h.handle_machines_endpoint(p))
    r.add(G, "/checkin", lambda h, p, _m: h.handle_checkin_endpoint(p))
    r.add(G, "/health", lambda h, p, _m: (lambda s: h._send_json(200 if s.get("ok") else 503, s))(health.snapshot()))
    r.add(
        G,
        "/alerts",
        lambda h, p, _m: h._send_json(
            200,
            {
                "status": _alert_status(),
                "active": dict(_alert_active),
                "snoozed": {k: v - time.time() for k, v in _alert_snoozed.items() if v > time.time()},
            },
        ),
    )
    r.add(
        P,
        "/alerts/snooze",
        lambda h, b, _m: (
            lambda d: h._send_json(
                200, {"result": _alert_snooze(d.get("signal", ""), int(d.get("duration", _ALERT_SNOOZE)))}
            )
        )(json.loads(b) if b else {}),
    )
    r.add(G, "/health/workers", lambda h, p, _m: h.handle_health_workers_endpoint())
    r.add(G, "/health/tunnel", lambda h, p, _m: h.handle_health_tunnel_endpoint())
    r.add(G, "/connectors", lambda h, p, _m: h.handle_connectors_status())
    r.add(G, "/channels", lambda h, p, _m: h.handle_channels_list(p))
    # PR review file content and keepalive routes removed — served by review/src/serve.ts
    r.add_pattern(G, r"^/guests/inbox", lambda h, p, m: h.handle_guest_inbox(p))
    r.add_pattern(G, r"^/guests/status", lambda h, p, m: h.handle_guest_status(p))
    r.add_pattern(G, r"^/relay/([^/]+)(?:/(.+))?$", lambda h, p, m: h.handle_relay_get(_mg(m), _mg2(m), p))
    r.add_pattern(G, r"^/channels/([^/]+)/messages$", lambda h, p, m: h.handle_channel_messages(_mg(m), p))
    # transcript endpoint removed — served by spawn-on-demand Bun viewer (rewind/)
    # PR review endpoint removed — served by review/src/serve.ts
    r.add(D, "/guests", lambda h, p, _m: h.handle_guest_disconnect(p))
    r.add_pattern(D, r"^/channels/([^/]+)$", lambda h, p, m: h.handle_channel_delete(_mg(m)))


_CONNECTOR_CSS = (_TEMPLATE_DIR / "connector.css").read_text() if (_TEMPLATE_DIR / "connector.css").exists() else ""


class MentionTracker:
    def __init__(self) -> None:
        self.target: str | None = None
        self.count: int = 0
        self.ts: float = 0.0

    def snapshot(self) -> _MentionSnapshot:
        return _MentionSnapshot(target=self.target, mention_count=self.count, ts=self.ts)

    def restore(self, snap: _MentionSnapshot) -> None:
        self.target = snap.target
        self.count = snap.mention_count
        self.ts = snap.ts


class BridgeHealth:
    """Unified health registry. Every subsystem writes timestamps here.
    One endpoint reads them all. One external watchdog alerts on staleness.

    Signals tracked (all as monotonic timestamps of last success):
      poll_ok         — last successful getUpdates response
      dispatch_ok     — last successful message dispatch to a worker
      hook_ok         — last successful worker→Telegram delivery via /outputs
      telegram_api_ok — last successful Telegram API send (text/photo/doc)
      watchdog_ok     — last worker-watchdog loop iteration
      gmail_ok        — last successful gmail message delivery
      github_ok       — last successful github comment delivery
    Error counters:
      poll_errors     — consecutive getUpdates failures
      hook_errors     — consecutive hook delivery failures
      telegram_errors — consecutive Telegram API failures
    Thread liveness:
      threads         — {name: alive} for all expected permanent threads
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ok_ts: dict[str, float] = {}  # signal -> time.time() of last success
        self._errors: dict[str, int] = {}  # signal -> consecutive error count
        self._threads: dict[str, threading.Thread] = {}  # name -> thread ref
        self._start_time: float = time.time()

    def ok(self, signal: str) -> None:
        """Record a successful operation."""
        now = time.time()
        with self._lock:
            self._ok_ts[signal] = now
            self._errors[signal] = 0

    def error(self, signal: str) -> None:
        """Record a failed operation."""
        with self._lock:
            self._errors[signal] = self._errors.get(signal, 0) + 1

    def error_count(self, signal: str) -> int:
        with self._lock:
            return self._errors.get(signal, 0)

    def register_thread(self, name: str, thread: threading.Thread) -> None:
        with self._lock:
            self._threads[name] = thread

    # Event-driven signals: only unhealthy if errors accumulate, not if idle
    _EVENT_SIGNALS = frozenset({"telegram_api", "hook", "dispatch"})

    def snapshot(self) -> dict[str, object]:
        """Full health snapshot for the /health endpoint."""
        now = time.time()
        with self._lock:
            signals = {}
            for sig, ts in self._ok_ts.items():
                age = round(now - ts, 1)
                errors = self._errors.get(sig, 0)
                if sig in self._EVENT_SIGNALS:  # noqa: SIM108
                    # Event-driven: OK unless errors are accumulating
                    sig_ok = errors < 10
                else:
                    # Periodic: must have a recent heartbeat AND low errors
                    sig_ok = age < 300 and errors < 10
                signals[sig] = {
                    "last_ok_ago_s": age,
                    "errors": errors,
                    "ok": sig_ok,
                }
            threads = {}
            for name, t in self._threads.items():
                threads[name] = t.is_alive()
        # Pool depth (outside lock — reads are thread-safe qsize)
        pools: dict[str, object] = {}
        try:
            pools["msg_pool"] = {"pending": _message_pool._work_queue.qsize(), "max": _message_pool._max_workers}
            pools["task_pool"] = {"pending": _task_pool._work_queue.qsize(), "max": _task_pool._max_workers}
        except Exception:
            pass
        all_ok = (all(s["ok"] for s in signals.values()) and all(threads.values())) if signals else False
        return {
            "ok": all_ok,
            "version": VERSION,
            "uptime_s": round(now - self._start_time),
            "signals": signals,
            "threads": threads,
            "pools": pools,
        }


health = BridgeHealth()

# --- Health alert routing via relay channel ---
# Leverages the existing relay channel infrastructure for alert delivery.
# On startup, creates (or restores) a "bridge-health" relay channel with
# subscribed workers. Health alerts are posted as messages to this channel.
# Workers receive alerts via the relay delivery path and can poll /relay/<id>
# for alert history. The channel never expires (TTL = 10 years).
ALERT_WORKERS: list[str] = [w.strip() for w in os.environ.get("BRIDGE_ALERT_WORKERS", "").split(",") if w.strip()]
_ALERT_COOLDOWN = 300  # seconds between repeated alerts for same signal
_ALERT_ESCALATE = 1800  # 30min: escalate if unresolved (shorter cooldown, severity bump)
_ALERT_SNOOZE = 3600  # 1hr: /alert snooze duration
_alert_active: dict[str, float] = {}  # signal -> time.time() when first alerted
_alert_last_sent: dict[str, float] = {}  # signal -> time.time() when last alert sent
_alert_snoozed: dict[str, float] = {}  # signal -> time.time() when snooze expires
_alert_lock = threading.Lock()
_health_channel_id: str | None = None  # set during startup


def _health_channel_ensure() -> str | None:
    """Create or restore the bridge-health relay channel."""
    global _health_channel_id
    if not ALERT_WORKERS:
        return None
    # Check if we already have one from a previous run
    with relay_store.lock:
        for cid, ch in relay_store.channels.items():
            if ch.get("label") == "bridge-health":
                # Update workers list in case it changed
                ch["workers"] = list(ALERT_WORKERS)
                _health_channel_id = cid
                _log(_LOG_INFO, "health", f"Restored health relay channel: {cid} (workers={ALERT_WORKERS})")
                return cid
    # Create new channel with very long TTL (never expires in practice)
    ch, _gt, _rt = relay_channel_create(ALERT_WORKERS[0], "bridge-health", ttl=86400 * 3650)
    ch["workers"] = list(ALERT_WORKERS)
    with relay_store.lock:
        relay_store.channels[ch["id"]] = ch
        _relay_save()
    _health_channel_id = ch["id"]
    _log(_LOG_INFO, "health", f"Created health relay channel: {ch['id']} (workers={ALERT_WORKERS})")
    return ch["id"]


def _health_alert_check() -> None:
    """Evaluate health snapshot and send alerts to subscribed workers.
    Called from the watchdog loop (every WATCHDOG_INTERVAL ≈ 4s).

    Alert lifecycle:
    1. Problem detected → 🔶 WARNING (first fire, 5min cooldown)
    2. Unresolved 30min+ → 🚨 CRITICAL (escalated, 2min cooldown)
    3. /alert snooze <signal> → suppressed for 1hr
    4. Signal recovers → ✅ RESOLVED (clears all state)
    """
    if not ALERT_WORKERS or not _health_channel_id:
        return
    snap = health.snapshot()
    now = time.time()
    current_problems: dict[str, str] = {}  # key -> description
    # Check signals
    for sig, info in snap.get("signals", {}).items():
        if not info.get("ok"):
            current_problems[sig] = f"{sig}(age={info.get('last_ok_ago_s', '?')}s, errors={info.get('errors', 0)})"
    # Check threads
    for tname, alive in snap.get("threads", {}).items():
        key = f"thread:{tname}"
        if not alive:
            current_problems[key] = f"thread {tname} DEAD"
    # Check pool saturation
    for pname, pinfo in snap.get("pools", {}).items():
        key = f"pool:{pname}"
        pending = pinfo.get("pending", 0) if isinstance(pinfo, dict) else 0
        mx = pinfo.get("max", 1) if isinstance(pinfo, dict) else 1
        if pending > mx * 2:
            current_problems[key] = f"{pname} saturated ({pending} pending, max {mx})"
    # --- Resolved: was active, now OK ---
    with _alert_lock:
        resolved = [k for k in _alert_active if k not in current_problems]
    for key in resolved:
        with _alert_lock:
            _alert_active.pop(key, None)
            _alert_last_sent.pop(key, None)
            _alert_snoozed.pop(key, None)
        _send_health_alert(f"✅ Resolved: {key}")
    # --- Active problems: classify severity, apply cooldown/snooze ---
    if not current_problems:
        return
    to_send: list[str] = []
    for key, desc in current_problems.items():
        with _alert_lock:
            # Snooze check
            snooze_until = _alert_snoozed.get(key, 0)
            if now < snooze_until:
                continue  # snoozed, skip
            elif key in _alert_snoozed:
                del _alert_snoozed[key]  # snooze expired
            # Track first-seen time
            first_seen = _alert_active.get(key)
            if first_seen is None:
                _alert_active[key] = now
                first_seen = now
            # Determine severity + cooldown
            age = now - first_seen
            if age >= _ALERT_ESCALATE:
                severity = "🚨"
                cooldown = 120  # 2min cooldown when escalated
            else:
                severity = "🔶"
                cooldown = _ALERT_COOLDOWN  # 5min cooldown
            # Cooldown check
            last_sent = _alert_last_sent.get(key, 0)
            if now - last_sent < cooldown:
                continue
            _alert_last_sent[key] = now
        duration = _fmt_duration(now - first_seen)
        to_send.append(f"{severity} {desc} ({duration})")
    if to_send:
        _send_health_alert("\n".join(to_send))


def _fmt_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h{m}m"


def _alert_snooze(signal: str, duration: int = _ALERT_SNOOZE) -> str:
    """Snooze alerts for a signal. Returns confirmation message."""
    with _alert_lock:
        # Exact match first
        if signal in _alert_active:
            _alert_snoozed[signal] = time.time() + duration
            return f"Snoozed '{signal}' for {_fmt_duration(duration)}"
        # Partial match (e.g., "gmail" matches "gmail")
        matches = [k for k in _alert_active if signal in k]
        if len(matches) == 1:
            _alert_snoozed[matches[0]] = time.time() + duration
            return f"Snoozed '{matches[0]}' for {_fmt_duration(duration)}"
        if matches:
            return f"Ambiguous: {', '.join(matches)}. Be more specific."
    return f"No active alert matching '{signal}'. Active: {', '.join(_alert_active) or 'none'}"


def _alert_status() -> str:
    """Return current alert state summary."""
    now = time.time()
    lines: list[str] = []
    with _alert_lock:
        if not _alert_active:
            return "No active alerts."
        for key, first_seen in sorted(_alert_active.items()):
            age = _fmt_duration(now - first_seen)
            parts = [f"  {key}: active {age}"]
            if key in _alert_snoozed:
                remaining = _alert_snoozed[key] - now
                if remaining > 0:
                    parts.append(f"(snoozed {_fmt_duration(remaining)})")
                else:
                    parts.append("(snooze expired)")
            if now - first_seen >= _ALERT_ESCALATE:
                parts.append("⚡ESCALATED")
            lines.append(" ".join(parts))
    return "Active alerts:\n" + "\n".join(lines)


def _send_health_alert(text: str) -> None:
    """Post alert to the bridge-health relay channel.
    Workers subscribed to the channel receive it via relay delivery.
    Also delivers directly to worker tmux sessions for immediate visibility."""
    if not _health_channel_id:
        return
    # Post to relay channel (persists in message history, visible via /relay/<id>)
    with _log_suppress(Exception, tag="health-alert", msg="relay append failed: "):
        _relay_append_msg(
            relay_store.channels[_health_channel_id],
            "system",
            "bridge-health",
            ",".join(ALERT_WORKERS),
            text,
        )
    # Also deliver to worker sessions directly for immediate prompt
    for worker in ALERT_WORKERS:
        with _log_suppress(Exception, tag="health-alert", msg=f"tmux delivery to {worker} failed: "):
            if worker_manager is not None:
                worker_manager.send(worker, f"bridge-health: {text}")


class BridgeRuntimeState:
    def __init__(self) -> None:
        self.active: str | None = None
        self.startup_notified: bool = False
        self.mention: MentionTracker = MentionTracker()

    def snapshot(self) -> _StateSnapshot:
        return _StateSnapshot(
            active=self.active,
            startup_notified=self.startup_notified,
        )

    def restore(self, snap: _StateSnapshot) -> None:
        self.active = snap.active
        self.startup_notified = snap.startup_notified


state = BridgeRuntimeState()
_last_mention = state.mention
API_ENDPOINTS = {
    "GET /": "API index — lists all endpoints",
    "GET /machines": "List configured machines, access hints, workers, and health",
    "GET /workers": "List active workers with send commands",
    "GET /checkin?name=<name>": "Refresh worker instructions (optional: &cwd=/path)",
    "GET /health": "Unified health: all signals, threads, uptime — single source of truth",
    "GET /health/workers": "Watchdog state for all workers",
    "GET /health/tunnel": "Tunnel and poll fallback state",
    "GET /transcript/<name>": "Polished HTML transcript tool for a worker",
    "GET /transcript/<name>/updates": "Poll for new transcript entries (returns {total, new})",
    # PR review viewer removed — served by review/src/serve.ts
    "POST /messages": "Send a prompt to a worker: {worker, message, from}",
    "POST /outputs": "Hook only: publish this worker's own response to Telegram",
    "POST /notifications": "Send notification to all admin chats",
    "POST /alerts": "Hook: JSONL health alert (stale transcript detection)",
    "POST /workers": "Worker registration (name, host, version, tools, callback_url)",
    "GET /connectors": "Connector status (gmail, github — running, failures, config)",
    "POST /connectors/restarts": "Restart a connector: {name: 'gmail'|'github'}",
    # PR review endpoints (comments, files, merges) removed — served by review/src/serve.ts
    "GET /guests": "List active guest sessions",
    "POST /guests": "Register a temporary guest agent session",
    "DELETE /guests?token=<token>": "Disconnect a guest session",
    "GET /relay/<channel_id>": "Relay channel guide, messages, status",
    "POST /relay/<channel_id>/send": "Guest sends message via relay channel",
    "POST /relay/<channel_id>/reply": "Worker replies via relay channel",
}


def get_worker_host(name: str) -> str | None:
    registry = _load_registry()
    worker = registry.get("workers", {}).get(name, {})
    return worker.get("host")


def get_registered_sessions(registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
    return _wm().get_registered_sessions(registered)


def send_to_worker(name: str, message: str, chat_id: int | None = None) -> bool:
    return _wm().send(name, message, chat_id)


worker_manager = WorkerManager(SESSIONS_DIR, TMUX_PREFIX)  # type: ignore[assignment]
try:

    GMAIL_IMPORT_ERROR: ImportError | None = None
    GITHUB_IMPORT_ERROR: ImportError | None = None
except ImportError as e:
    GmailConnector = None  # type: ignore[assignment,misc]
    GitHubConnector = None  # type: ignore[assignment,misc]
    GMAIL_IMPORT_ERROR = e
    GITHUB_IMPORT_ERROR = e
_message_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="msg")
_task_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task")

# ── Hook response debounce ──────────────────────────────────────────────────
# Claude Code fires Stop for every assistant turn, including intermediate
# turns between tool calls.  Without debounce a single task can send 2-3
# messages to Telegram.  We buffer the last response per worker and flush
# after _HOOK_DEBOUNCE_SECS of quiet.
_HOOK_DEBOUNCE_SECS = 8.0
_hook_debounce_lock = threading.Lock()
_hook_debounce: dict[str, dict[str, object]] = {}  # name -> {text, chat_id, session_id, timer}


def _flush_hook_response(name: str) -> None:
    """Send the debounced hook response to Telegram after the quiet window."""
    with _hook_debounce_lock:
        entry = _hook_debounce.pop(name, None)
    if not entry:
        return
    text = cast(str, entry["text"])
    chat_id = cast(int, entry["chat_id"])
    session_id = cast(str, entry.get("session_id", ""))
    _log(_LOG_INFO, "hook", f"Hook response: {name} -> chat {chat_id} ({len(text)} chars)")
    if session_id:
        _cache_session_id(name, session_id)
    try:
        send_response_to_telegram(name, text, chat_id, log_prefix="Response")
        health.ok("hook")
    except Exception:
        health.error("hook")
        raise
    _check_learning_reminder(name)
    clear_pending(name)
    mark_hook_event(name)


def _debounce_hook_response(name: str, text: str, chat_id: int, session_id: str) -> None:
    """Buffer a hook response, replacing any pending one for this worker."""
    with _hook_debounce_lock:
        prev = _hook_debounce.get(name)
        if prev and prev.get("timer"):
            cast(threading.Timer, prev["timer"]).cancel()
            prev_len = len(cast(str, prev.get("text", "")))
            _log(
                _LOG_DEBUG,
                "hook",
                f"Debounce: replaced {name} buffered ({prev_len} chars) with new ({len(text)} chars)",
            )
        t = threading.Timer(_HOOK_DEBOUNCE_SECS, _flush_hook_response, args=[name])
        t.name = f"hook-debounce-{name}"
        t.daemon = True
        _hook_debounce[name] = {"text": text, "chat_id": chat_id, "session_id": session_id, "timer": t}
        t.start()


connectors = ConnectorRegistry()
tunnel_manager: TunnelManager | None = None
if not BRIDGE_PUBLIC_URL:
    with suppress(subprocess.SubprocessError, OSError):
        _ts_ip = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        ).stdout.strip()  # direct call — runs before constants init
        if _ts_ip:
            BRIDGE_PUBLIC_URL = f"http://{_ts_ip}:{PORT}"
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
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    "build",
    "dist",
    "target",
    ".gradle",
    ".cache",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "*.pyc",
    ".build",
    ".claude/worktrees",
]
media_groups = MediaGroupState()
tokens = TokenStore()


def kill_session(name: str) -> tuple[bool, str | None]:
    return _wm().end(name)


def restart_claude(name: str, mode: str = "relaunch") -> tuple[bool, str | None]:
    return _wm().restart(name, mode=mode)


def switch_session(name: str) -> tuple[bool, str | None]:
    registered = get_registered_sessions()
    if name not in registered:
        return False, f"Worker '{name}' not found"
    state.active = name
    save_last_active(name)
    return True, None


def _notify_admin(msg: str) -> None:
    with suppress(urllib.error.URLError, OSError, TimeoutError):
        if admin_chat_id:
            send_telegram_message(admin_chat_id, msg)


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
                    with _log_suppress(OSError, ValueError, level=_LOG_DEBUG, tag="parse:get_all_chat_ids"):
                        raw = chat_id_file.read_text().strip()
                        if raw:
                            chat_ids.add(int(raw))
    if admin_chat_id:
        chat_ids.add(admin_chat_id)
    return list(chat_ids)


def send_shutdown_message() -> None:
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        _log(_LOG_WARN, "notify", "No chat_ids to notify")
        return
    _log(_LOG_INFO, "notify", f"Sending shutdown to {len(chat_ids)} chat(s)...")
    for chat_id in chat_ids:
        transport.send_text(chat_id, "Going offline briefly. Your team stays the same.")
    _log(_LOG_INFO, "notify", "Shutdown notifications sent")


CommandFn = Callable[[str, ChatId, MessageId], bool]
assert worker_manager is not None
command_router = CommandRouter(transport, worker_manager)

_endpoint_router = EndpointRouter()
_setup_endpoint_routes()


def graceful_shutdown(signum: int, frame: types.FrameType | None) -> None:
    sig_name = signal.Signals(signum).name if signum else "unknown"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ppid = os.getppid()
    parent_info = f"ppid={ppid}"
    with (
        _log_suppress(OSError, UnicodeDecodeError, level=_LOG_DEBUG, tag="io:graceful_shutdown"),
        open(f"/proc/{ppid}/cmdline", "rb") as f,
    ):
        cmdline = f.read().decode().replace("\x00", " ").strip()
        parent_info = f"ppid={ppid} cmd={cmdline[:100]}"
    _log(_LOG_INFO, "bridge", f"Received {sig_name} ({parent_info}), shutting down...")
    connectors.stop_all()
    if tunnel_manager is not None:
        with _log_suppress(RuntimeError, OSError, level=_LOG_DEBUG, tag="shutdown:tunnel"):
            tunnel_manager.stop()
            _log(_LOG_INFO, "bridge", "Tunnel manager stopped")
    watchdog.stop_event.set()
    for cancelable in [learning_reminders.idle_scan_timer]:
        if cancelable is not None:
            with _log_suppress(RuntimeError, OSError, level=_LOG_DEBUG, tag="shutdown:timer"):
                cancelable.cancel()
    with media_groups.lock:
        for _mg_entry in media_groups.buffer.values():
            t = _mg_entry.get("timer")
            if t is not None:
                with suppress(RuntimeError, OSError):
                    t.cancel()
        media_groups.buffer.clear()
    with processes.adapter_pids_lock:
        adapter_names = list(processes.adapter_pids.keys())
    for name in adapter_names:
        with suppress(OSError, ProcessLookupError):
            kill_adapter(name)
    with processes.pipe_readers_lock:
        pipe_names = list(processes.pipe_readers.keys())
    for name in pipe_names:
        with suppress(OSError):
            stop_pipe_reader(name)
    # Flush any pending debounced hook responses before shutdown
    with _hook_debounce_lock:
        pending_names = list(_hook_debounce.keys())
    for name in pending_names:
        _flush_hook_response(name)
    _message_pool.shutdown(wait=False, cancel_futures=True)
    _task_pool.shutdown(wait=False, cancel_futures=True)
    send_shutdown_message()
    sys.exit(0)


def _discover_and_configure_sessions() -> dict[str, TmuxSessionDict]:
    registered = scan_tmux_sessions()
    registered = get_registered_sessions(registered)
    if registered:
        _log(_LOG_INFO, "bridge", f"Discovered sessions: {list(registered.keys())}")
        for name, info in registered.items():
            tmux_name = info.get("tmux", f"{TMUX_PREFIX}{name}")
            if not tmux_name.startswith(TMUX_PREFIX):
                _log(_LOG_WARN, "bridge", f"SKIP {name}: tmux '{tmux_name}' doesn't match prefix '{TMUX_PREFIX}'")
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
    global admin_chat_id
    last_active = load_last_active()
    if last_active and last_active in registered:
        state.active = last_active
        _log(_LOG_INFO, "bridge", f"Restored last active worker: {last_active}")
    elif last_active:
        _log(_LOG_WARN, "bridge", f"Last active worker '{last_active}' no longer exists")
    if os.path.isdir(TEAM_DIR):
        _startup_note = read_checkin_note()
        _log(
            _LOG_INFO,
            "bridge",
            f"Team dir: {TEAM_DIR}" + (f"  Checkin note: {len(_startup_note)} chars" if _startup_note else ""),
        )
    else:
        _log(_LOG_WARN, "bridge", f"Team dir not found: {TEAM_DIR} (checkin note disabled)")
    last_chat_id = load_last_chat_id()
    if last_chat_id and admin_chat_id is None:
        admin_chat_id = last_chat_id
        _log(_LOG_INFO, "bridge", f"Restored admin from last_chat_id: {admin_chat_id}")
    _relay_load()
    _channel_load()
    _guest_load()
    return last_chat_id


def _log_startup_info(registered: dict[str, TmuxSessionDict]) -> None:
    update_bot_commands()
    _log(_LOG_INFO, "bridge", f"Multi-Session Bridge on {BRIDGE_BIND}:{PORT}")
    if BRIDGE_BIND == "0.0.0.0":
        _log(_LOG_WARN, "bridge", "Bound to 0.0.0.0 — exposed on ALL interfaces. Use BRIDGE_BIND=<tailscale-ip>")
    _log(
        _LOG_INFO,
        "bridge",
        f"Hook: http://{BRIDGE_BIND}:{PORT}/outputs | Active: {state.active or 'none'} | Sessions: {list(registered.keys()) or 'none'}",
    )
    _log(
        _LOG_INFO,
        "bridge",
        f"Webhook: {'enabled' if WEBHOOK_SECRET else 'disabled'} | Admin: {admin_chat_id or 'auto-learn'} | Exec: direct",
    )


def _send_startup_notification(last_chat_id: int, registered: dict[str, TmuxSessionDict]) -> None:
    state.startup_notified = True
    sessions = list(registered.keys())
    active = state.active
    lines = ["I'm online and ready."]
    if sessions:
        lines.append(f"Team: {', '.join(sessions)}")
        if active:
            lines.append(f"Focused: {active}")
    else:
        lines.append("No workers yet. Hire your first long-lived worker with /hire <name>.")
    result = transport.send_text(last_chat_id, "\n".join(lines))
    if result and result.get("ok"):
        _log(_LOG_INFO, "bridge", f"Sent startup notification to chat {last_chat_id}")
    else:
        _log(_LOG_WARN, "bridge", f"Failed to send startup notification: {result}")


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
        _log(_LOG_INFO, "bridge", f"Machine catalog: {list(machines.keys())} ({MACHINES_CONFIG_FILE})")
    except MachineConfigError as e:
        _log(_LOG_ERROR, "bridge", f"Error: {e}")
        sys.exit(1)
    registered = _discover_and_configure_sessions()
    last_chat_id = _restore_bridge_state(registered)
    _log_startup_info(registered)
    if last_chat_id:
        _send_startup_notification(last_chat_id, registered)
    watchdog = threading.Thread(target=watchdog_loop, name="worker-watchdog", daemon=True)
    watchdog.start()
    health.register_thread("worker-watchdog", watchdog)
    _seed_learning_reminder_state(registered.keys())
    _schedule_idle_scan()
    _log(
        _LOG_INFO,
        "bridge",
        f"Learning reminder idle scan: started (every 30 min, {len(learning_reminders.state)} workers tracked)",
    )
    connectors.gmail, connectors.github = _start_connectors()
    _health_channel_ensure()
    server = ReuseAddrServer((BRIDGE_BIND, PORT), Handler)
    tunnel_config = _build_tunnel_config()
    if tunnel_config.mode != "none" and BOT_TOKEN:

        def _tunnel_on_update(update: dict[str, object]) -> None:
            if "message" in update:

                def _safe_handle(upd: dict[str, object]) -> None:
                    with _log_suppress(
                        json.JSONDecodeError,
                        KeyError,
                        ValueError,
                        TypeError,
                        AttributeError,
                        OSError,
                        level=_LOG_ERROR,
                        tag="tunnel:poll",
                        msg="handle_message CRASH: ",
                        exc_info=True,
                    ):
                        command_router.handle_message(upd)

                _message_pool.submit(_safe_handle, update)

        def _tunnel_on_notify(msg: str) -> None:
            if admin_chat_id:
                with _log_suppress(Exception, tag="tunnel", msg="notify failed: "):
                    transport.send_text(admin_chat_id, msg)

        tunnel_manager = TunnelManager(
            config=tunnel_config,
            bot_token=BOT_TOKEN,
            port=PORT,
            node_dir=NODE_DIR,
            webhook_secret=WEBHOOK_SECRET,
            bind_host=BRIDGE_BIND,
            on_update=_tunnel_on_update,
            on_notify=_tunnel_on_notify,
        )
        tunnel_manager.start()
        _log(_LOG_INFO, "tunnel", f"Tunnel manager started (mode={tunnel_config.mode})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        graceful_shutdown(signal.SIGINT, None)


import claudecode as _cc_mod  # noqa: E402  # circular: claudecode imports bridge
import shared as _core_mod  # noqa: E402  # module ref for attribute propagation
import telegram as _tg_mod  # noqa: E402  # circular: telegram imports bridge


class _BridgeModule(types.ModuleType):
    _sources = ()

    def __setattr__(self, name: str, value: object) -> None:
        for mod in self._sources:
            if hasattr(mod, name):
                setattr(mod, name, value)
        super().__setattr__(name, value)

    def __getattr__(self, name: str) -> object:
        for mod in self._sources:
            with suppress(AttributeError):
                return getattr(mod, name)
        raise AttributeError(f"module 'bridge' has no attribute {name}")


_this_module = sys.modules[__name__]
_this_module.__class__ = _BridgeModule
_this_module._sources = (_core_mod, _tg_mod, _cc_mod)


if __name__ == "__main__":
    _sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if _sub == "review":
        # Forward to standalone generate script
        _gen = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review", "generate.py")
        sys.exit(subprocess.call([sys.executable, _gen, *sys.argv[2:]]))
    elif _sub == "indexer":
        # Indexer was extracted to rewind/ (Bun + bun:sqlite FTS5) in request 115.
        # Falling through to main() here would start the FULL bridge including Telegram
        # getUpdates polling — causing 409 Conflict with the real bridge. Fail loudly.
        print("ERROR: 'bridge.py indexer' was removed in request 115.", file=sys.stderr)
        print("Use: bun rewind/src/rewind.ts --port P --token T --ttl S --jsonl-path PATH", file=sys.stderr)
        sys.exit(1)
    else:
        main()
