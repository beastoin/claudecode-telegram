"""Tunnel lifecycle management for the claudecode-telegram bridge.

Manages cloudflared quick-tunnel, Telegram webhook registration, poll
fallback (getUpdates long-polling), and the watchdog that keeps them
healthy.  Designed for dependency injection — every external call goes
through a seam so tests never touch the network.

Dependency graph:
    core.py  →  tunnel.py  →  bridge.py (wires it in)
"""

from __future__ import annotations

import enum
import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from core import (
    Clock,
    SubprocessRunner,
    TunnelConfig,
    _LOG_ERROR,
    _LOG_INFO,
    _LOG_WARN,
    _RealClock,
    _RealSubprocessRunner,
    _clock as _default_clock,
    _log,
    _subprocess_runner as _default_subprocess_runner,
    _urlopen as _default_urlopen,
)
if TYPE_CHECKING:
    import http.client


# ── State enum ────────────────────────────────────────────────────────

class TunnelState(enum.Enum):
    """Current tunnel lifecycle state."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    POLL_FALLBACK = "poll_fallback"
    FAILED = "failed"


# ── TunnelManager ─────────────────────────────────────────────────────

class TunnelManager:
    """Manages cloudflared tunnel, webhook, poll fallback, and watchdog.

    Usage:
        mgr = TunnelManager(config=..., bot_token=..., port=..., node_dir=...)
        mgr.start()   # non-blocking — spawns watchdog thread
        ...
        mgr.stop()    # blocks until watchdog thread exits
    """

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
        urlopen: Callable[..., http.client.HTTPResponse] | None = None,
        on_notify: Callable[[str], None] | None = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._token = bot_token
        self._port = port
        self._node_dir = node_dir
        self._bind_host = bind_host
        self._webhook_secret = webhook_secret
        self._runner = subprocess_runner or _default_subprocess_runner
        self._clock = clock or _default_clock
        self._urlopen = urlopen or _default_urlopen
        self._on_notify = on_notify or (lambda _msg: None)
        self._on_update = on_update  # poll fallback forwards here

        # Mutable state (protected by _lock where needed)
        self._lock = threading.Lock()
        self._state = TunnelState.STOPPED
        self._tunnel_url: str = ""
        self._tunnel_proc: subprocess.Popen[str] | None = None
        self._polling_active: bool = False

        # Threads
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog thread (non-blocking)."""
        if self._config.mode == "none":
            _log(_LOG_INFO, "tunnel", "Tunnel disabled (mode=none)")
            return
        self._kill_stale_tunnel()
        self._stop_event.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="tunnel-watchdog", daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        """Signal the watchdog to stop and wait for it to finish."""
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

    def status(self) -> dict[str, str | bool]:
        """Return a dict suitable for /status endpoint."""
        return {
            "mode": self._config.mode,
            "state": self._state.value,
            "tunnel_url": self._tunnel_url,
            "polling_active": self._polling_active,
        }

    # ── Telegram API helpers (use injected urlopen) ────────────────────

    def _telegram_api(self, method: str, payload: dict[str, object] | None = None) -> dict[str, Any]:
        """Call a Telegram Bot API method using the injected urlopen.

        Returns the parsed JSON response dict, or {} on any error.
        This avoids importing telegram.py — keeping tunnel.py independent.
        """
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with self._urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except Exception:
            return {}

    def _tg_set_webhook(self, webhook_url: str, secret_token: str = "") -> dict[str, Any]:
        payload: dict[str, object] = {"url": webhook_url}
        if secret_token:
            payload["secret_token"] = secret_token
        return self._telegram_api("setWebhook", payload)

    def _tg_delete_webhook(self) -> dict[str, Any]:
        return self._telegram_api("deleteWebhook")

    def _tg_get_webhook_info(self) -> dict[str, Any]:
        return self._telegram_api("getWebhookInfo")

    def _tg_get_me(self) -> dict[str, Any]:
        return self._telegram_api("getMe")

    # ── Watchdog loop ─────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Main lifecycle loop — runs in a daemon thread."""
        self._state = TunnelState.STARTING

        # Phase 1: Wait for bridge HTTP server to bind
        if not self._wait_for_port():
            _log(_LOG_ERROR, "tunnel", f"Bridge did not bind to port {self._port}")
            self._state = TunnelState.FAILED
            return

        # Phase 2: Establish tunnel + webhook (or go straight to polling)
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

        # Save bot info (best-effort)
        self._save_bot_info()

        # Phase 3: Monitoring loop
        check_counter = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(self._config.watchdog_interval)
            if self._stop_event.is_set():
                break

            # Monitor cloudflared process health
            if self._tunnel_proc is not None:
                problem = self._check_tunnel_health()
                if problem == "process died":
                    # Cloudflared crashed — must restart to get a new one
                    _log(_LOG_WARN, "tunnel", "Tunnel process died, restarting...")
                    self._on_notify("⚠️ Tunnel process died. Reconnecting...")
                    self._state = TunnelState.RESTARTING

                    new_url = self._restart_with_retry()
                    if new_url:
                        self._tunnel_url = new_url
                        self._save_tunnel_url(new_url)
                        _log(_LOG_INFO, "tunnel", f"Tunnel restarted: {new_url}")

                        # Stop polling BEFORE setting webhook to avoid 409
                        # (getUpdates and webhook can't coexist)
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
                    # Tunnel process alive but URL not reachable — likely
                    # DNS propagation delay.  Do NOT restart (that creates
                    # a new URL and resets DNS from scratch).  Fall back to
                    # polling; _periodic_webhook_check restores webhook
                    # once DNS resolves.
                    if not self._polling_active:
                        _log(_LOG_WARN, "tunnel", "Tunnel URL unreachable (DNS?), falling back to polling")
                        self._start_poll_fallback()

            # Monitor poll fallback health
            if self._polling_active and self._poll_thread and not self._poll_thread.is_alive():
                _log(_LOG_WARN, "tunnel", "Poll fallback thread died, restarting...")
                self._poll_stop.clear()
                self._start_poll_fallback()

            # Periodic health checks
            check_counter = (check_counter + 1) % self._config.webhook_check_cycles
            if check_counter == 0:
                if self._tunnel_url:
                    self._periodic_webhook_check()
                elif self._polling_active and self._tunnel_proc is None and self._config.mode == "auto":
                    # Cloudflared never started (e.g. Cloudflare 429 rate limit).
                    # Periodically retry starting it.
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

    # ── Cloudflared management ────────────────────────────────────────

    def _start_cloudflared(self) -> str:
        """Spawn cloudflared and wait for the quick-tunnel URL. Returns URL or ''."""
        log_file = self._node_dir / "tunnel.log"
        log_file.write_text("")  # truncate

        try:
            proc = self._runner.popen(
                [self._config.cloudflared_binary, "tunnel", "--url", f"http://localhost:{self._port}"],
                stdout=open(log_file, "w"),
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
        """Poll the cloudflared log for a trycloudflare.com URL."""
        elapsed = 0
        while elapsed < timeout and not self._stop_event.is_set():
            self._clock.sleep(1)
            elapsed += 1
            try:
                text = log_file.read_text()
            except OSError:
                continue
            # cloudflared prints: https://xxx-yyy-zzz.trycloudflare.com
            for line in text.splitlines():
                for word in line.split():
                    if word.startswith("https://") and ".trycloudflare.com" in word and "api.trycloudflare.com" not in word:
                        return word.rstrip("/")
        return ""

    def _kill_cloudflared(self) -> None:
        """Kill the cloudflared process if running."""
        proc = self._tunnel_proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._tunnel_proc = None

    def _is_tunnel_alive(self) -> bool:
        """Check if the cloudflared process is still running."""
        proc = self._tunnel_proc
        return proc is not None and proc.poll() is None

    def _is_tunnel_reachable(self) -> bool:
        """HTTP probe the tunnel URL.

        Any HTTP response (even 4xx/5xx) proves the tunnel is working —
        a 501 from our bridge for HEAD is still reachable.  Only network-
        level failures (DNS, timeout, connection refused) mean unreachable.
        """
        if not self._tunnel_url:
            return False
        try:
            req = urllib.request.Request(self._tunnel_url, method="HEAD")
            self._urlopen(req, timeout=self._config.reachability_timeout)
            return True
        except urllib.error.HTTPError:
            # Got an HTTP response — tunnel is reachable even if the
            # bridge returns 501 (no do_HEAD) or any other status code.
            return True
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def _check_tunnel_health(self) -> str:
        """Returns a problem description, or '' if healthy.

        Only 'process died' triggers a restart — DNS/reachability failures
        should NOT kill the cloudflared process because getting a new URL
        resets DNS propagation (vicious cycle).
        """
        if not self._is_tunnel_alive():
            return "process died"
        if not self._is_tunnel_reachable():
            return "unreachable"
        return ""

    def _restart_with_retry(self) -> str:
        """Kill and restart cloudflared with exponential backoff. Returns new URL or ''."""
        backoff = self._config.initial_backoff
        for attempt in range(1, self._config.max_restart_attempts + 1):
            if attempt > 1:
                _log(_LOG_INFO, "tunnel", f"Restart attempt {attempt}/{self._config.max_restart_attempts} (backoff {backoff}s)")
                self._clock.sleep(backoff)
                backoff *= 2

            self._kill_cloudflared()
            url = self._start_cloudflared()
            if url:
                return url

        return ""

    # ── Webhook management ────────────────────────────────────────────

    def _set_webhook_with_retry(self, url: str) -> bool:
        """Set the Telegram webhook with retry delays. Returns True on success."""
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

        # All retries exhausted — delete webhook to prevent partial registration
        # from blocking getUpdates (409 Conflict) when falling back to polling.
        self._tg_delete_webhook()
        return False

    def _periodic_webhook_check(self) -> None:
        """Check webhook health and fix if stale. Called every ~60s."""
        if self._polling_active:
            # While polling, try to restore webhook.
            # Must stop polling FIRST — setWebhook + getUpdates can't coexist
            # or Telegram returns 409 Conflict on getUpdates.
            self._stop_poll_fallback()
            resp = self._tg_set_webhook(self._tunnel_url, secret_token=self._webhook_secret)
            if resp.get("ok"):
                self._state = TunnelState.RUNNING
                _log(_LOG_INFO, "tunnel", "Webhook restored, poll fallback stopped")
                self._on_notify("✅ Webhook restored (DNS resolved)")
            else:
                # setWebhook failed — clean up and resume polling
                self._tg_delete_webhook()
                self._start_poll_fallback()
        else:
            # Verify webhook points to our URL
            info = self._tg_get_webhook_info()
            if info:
                result = info.get("result", {})
                current_url = result.get("url", "") if isinstance(result, dict) else ""
                if not current_url or self._tunnel_url not in current_url:
                    resp = self._tg_set_webhook(self._tunnel_url, secret_token=self._webhook_secret)
                    if resp.get("ok"):
                        _log(_LOG_INFO, "tunnel", "Webhook re-registered (was stale)")
                        self._on_notify("✅ Webhook re-registered")

    # ── Poll fallback ─────────────────────────────────────────────────

    def _start_poll_fallback(self) -> None:
        """Start getUpdates long-polling in a background thread."""
        if self._polling_active:
            return

        # Delete webhook so Telegram sends via getUpdates
        self._tg_delete_webhook()
        self._clock.sleep(1)

        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="tunnel-poll", daemon=True,
        )
        self._poll_thread.start()
        self._polling_active = True
        self._state = TunnelState.POLL_FALLBACK
        _log(_LOG_INFO, "tunnel", "Poll fallback started")

    def _stop_poll_fallback(self) -> None:
        """Stop the poll fallback thread."""
        if not self._polling_active:
            return
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._polling_active = False
        _log(_LOG_INFO, "tunnel", "Poll fallback stopped")

    def _poll_loop(self) -> None:
        """getUpdates long-polling loop. Runs in a daemon thread."""
        offset = 0
        _bridge_host = self._bind_host or "127.0.0.1"
        _log(_LOG_INFO, "tunnel:poll", f"Poll loop started (bridge={_bridge_host}:{self._port})")
        while not self._poll_stop.is_set():
            try:
                url = f"https://api.telegram.org/bot{self._token}/getUpdates?offset={offset}&timeout={self._config.poll_timeout}"
                req = urllib.request.Request(url)
                with self._urlopen(req, timeout=self._config.poll_timeout + 5) as resp:
                    data = json.loads(resp.read())

                if not data.get("ok"):
                    self._clock.sleep(1)
                    continue

                for update in data.get("result", []):
                    uid = update.get("update_id", 0)
                    if self._on_update:
                        try:
                            self._on_update(update)
                        except Exception as exc:
                            _log(_LOG_ERROR, "tunnel:poll", f"Forward failed update_id={uid}: {exc}")
                    else:
                        self._forward_to_localhost(update)
                    offset = uid + 1

            except Exception as exc:
                if not self._poll_stop.is_set():
                    _log(_LOG_WARN, "tunnel:poll", f"Poll error: {exc}")
                    self._clock.sleep(self._config.poll_error_delay)

    def _forward_to_localhost(self, update: dict[str, Any]) -> None:
        """Forward a polled update to the bridge via HTTP POST (fallback path)."""
        _bridge_host = self._bind_host or "127.0.0.1"
        try:
            req = urllib.request.Request(
                f"http://{_bridge_host}:{self._port}/",
                data=json.dumps(update).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            self._urlopen(req, timeout=5)
        except Exception as exc:
            _log(_LOG_WARN, "tunnel:poll", f"Forward to {_bridge_host} failed: {exc}")

    # ── Port wait ─────────────────────────────────────────────────────

    def _wait_for_port(self) -> bool:
        """Wait for bridge HTTP server to bind to the port."""
        elapsed = 0
        bind_host = self._bind_host or "127.0.0.1"
        while elapsed < self._config.port_wait_timeout and not self._stop_event.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    s.connect((bind_host, self._port))
                    _log(_LOG_INFO, "tunnel", f"Bridge ready on {bind_host}:{self._port} ({elapsed}s)")
                    return True
            except (ConnectionRefusedError, OSError):
                pass
            self._clock.sleep(1)
            elapsed += 1
        return False

    # ── File persistence ──────────────────────────────────────────────

    def _save_tunnel_url(self, url: str) -> None:
        try:
            (self._node_dir / "tunnel_url").write_text(url)
        except OSError:
            pass

    def _kill_stale_tunnel(self) -> None:
        """Kill any orphaned cloudflared from a previous crash/SIGKILL."""
        pid_file = self._node_dir / "tunnel.pid"
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            import os
            os.kill(pid, 9)
            _log(_LOG_WARN, "tunnel", f"Killed stale cloudflared (pid={pid})")
        except (OSError, ProcessLookupError):
            pass  # already dead
        try:
            pid_file.unlink()
        except OSError:
            pass

    def _save_pid(self, pid: int) -> None:
        try:
            (self._node_dir / "tunnel.pid").write_text(str(pid))
        except OSError:
            pass

    def _save_bot_info(self) -> None:
        """Save bot ID and username for the status command."""
        try:
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
        except Exception:
            pass
