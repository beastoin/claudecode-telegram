"""Tests for tunnel.py — tunnel lifecycle, poll fallback, and watchdog.

Behavior tests: each test verifies something the bridge actually depends on.
All tests use DI seams — no real subprocesses, network, or cloudflared.
"""
import tunnel

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from core import TunnelConfig


# ── Test doubles ──────────────────────────────────────────────────────


class FakeClock:
    """Deterministic clock — sleep just advances a counter."""

    def __init__(self) -> None:
        self._time: float = 1000.0
        self._sleeps: list[float] = []

    def time(self) -> float:
        return self._time

    def sleep(self, seconds: float) -> None:
        self._sleeps.append(seconds)
        self._time += seconds


class FakePopen:
    """Simulates a cloudflared process."""

    def __init__(self, *, alive: bool = True, pid: int = 9999) -> None:
        self.pid = pid
        self._alive = alive
        self._killed = False

    def poll(self) -> int | None:
        return None if self._alive and not self._killed else 1

    def kill(self) -> None:
        self._killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 1


class FakeSubprocessRunner:
    """Injectable subprocess runner for tests."""

    def __init__(self) -> None:
        self.popen_calls: list[list[str]] = []
        self._next_popen: FakePopen | None = None

    def set_next_popen(self, proc: FakePopen) -> None:
        self._next_popen = proc

    def run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "", "")

    def popen(self, args: list[str], **kwargs: object) -> FakePopen:
        self.popen_calls.append(args)
        proc = self._next_popen or FakePopen()
        self._next_popen = None
        return proc  # type: ignore[return-value]


class FakeUrlopen:
    """Injectable urlopen that returns canned responses."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._responses: dict[str, dict[str, Any]] = {}
        self._fail: bool = False

    def set_response(self, url_substring: str, data: dict[str, Any]) -> None:
        self._responses[url_substring] = data

    def set_fail(self, fail: bool = True) -> None:
        self._fail = fail

    def __call__(self, req: Any, timeout: float = 10) -> Any:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        if self._fail:
            raise ConnectionError("Fake connection error")
        for substr, data in self._responses.items():
            if substr in url:
                return FakeHTTPResponse(json.dumps(data).encode())
        return FakeHTTPResponse(b'{"ok":true}')


class FakeHTTPResponse:
    """Minimal HTTP response for FakeUrlopen."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_manager(
    tmp_path: Path,
    mode: str = "auto",
    provided_url: str = "",
    **overrides: Any,
) -> tuple[tunnel.TunnelManager, FakeSubprocessRunner, FakeClock, FakeUrlopen]:
    """Build a TunnelManager with all fakes injected."""
    runner = FakeSubprocessRunner()
    clock = FakeClock()
    urlopen = FakeUrlopen()

    defaults: dict[str, Any] = {
        "startup_timeout": 3,     # short for tests
        "max_restart_attempts": 2,
        "initial_backoff": 1,
        "watchdog_interval": 1,
        "webhook_check_cycles": 3,
        "port_wait_timeout": 3,
        "poll_timeout": 5,
        "poll_error_delay": 1,
    }
    defaults.update(overrides)

    config = TunnelConfig(
        mode=mode,  # type: ignore[arg-type]
        provided_url=provided_url,
        **defaults,
    )

    notifications: list[str] = []
    updates: list[dict[str, Any]] = []

    mgr = tunnel.TunnelManager(
        config=config,
        bot_token="fake:token",
        port=19999,
        node_dir=tmp_path,
        subprocess_runner=runner,
        clock=clock,
        urlopen=urlopen,
        on_notify=lambda msg: notifications.append(msg),
        on_update=lambda upd: updates.append(upd),
    )
    mgr._notifications = notifications  # type: ignore[attr-defined]
    mgr._updates = updates  # type: ignore[attr-defined]

    return mgr, runner, clock, urlopen


# ── Tests: Configuration ─────────────────────────────────────────────


def test_none_mode_does_nothing(tmp_path: Path) -> None:
    """mode=none: no tunnel, no thread, no subprocess."""
    mgr, runner, _, _ = _make_manager(tmp_path, mode="none")
    mgr.start()
    time.sleep(0.1)
    mgr.stop()

    assert mgr.state == tunnel.TunnelState.STOPPED
    assert runner.popen_calls == []
    assert mgr.tunnel_url == ""


def test_provided_url_skips_cloudflared(tmp_path: Path) -> None:
    """mode=provided: uses given URL, no cloudflared spawn."""
    mgr, runner, clock, urlopen = _make_manager(
        tmp_path, mode="provided", provided_url="https://test.trycloudflare.com",
    )
    urlopen.set_response("setWebhook", {"ok": True})
    urlopen.set_response("getMe", {"ok": True, "result": {"id": 123, "username": "testbot"}})

    # Simulate port being available immediately
    _simulate_port_ready(mgr)

    # Use start()/stop() — the watchdog runs in a daemon thread
    mgr.start()
    time.sleep(0.5)  # let Phase 1+2 complete
    mgr.stop()

    assert runner.popen_calls == []
    assert mgr.tunnel_url == "https://test.trycloudflare.com"


def test_auto_mode_spawns_cloudflared(tmp_path: Path) -> None:
    """mode=auto: spawns cloudflared and waits for URL."""
    mgr, runner, clock, urlopen = _make_manager(tmp_path, mode="auto")
    urlopen.set_response("setWebhook", {"ok": True})
    urlopen.set_response("getMe", {"ok": True, "result": {"id": 42, "username": "bot"}})

    proc = FakePopen()
    runner.set_next_popen(proc)

    # Write tunnel URL to log file (simulating cloudflared output)
    _simulate_port_ready(mgr)
    _write_tunnel_url_to_log(tmp_path, "https://abc-def.trycloudflare.com")

    mgr._start_cloudflared()

    assert len(runner.popen_calls) == 1
    assert "cloudflared" in runner.popen_calls[0][0]


# ── Tests: URL extraction ────────────────────────────────────────────


def test_wait_for_url_finds_trycloudflare(tmp_path: Path) -> None:
    """URL extraction parses cloudflared log output correctly."""
    mgr, _, _, _ = _make_manager(tmp_path)
    log_file = tmp_path / "tunnel.log"
    log_file.write_text(
        "2024-01-01 INF Starting tunnel\n"
        "2024-01-01 INF https://abc-xyz-123.trycloudflare.com\n"
        "2024-01-01 INF Connection registered\n"
    )

    url = mgr._wait_for_url(log_file, timeout=2)
    assert url == "https://abc-xyz-123.trycloudflare.com"


def test_wait_for_url_ignores_api_domain(tmp_path: Path) -> None:
    """URL extraction filters out api.trycloudflare.com."""
    mgr, _, _, _ = _make_manager(tmp_path)
    log_file = tmp_path / "tunnel.log"
    log_file.write_text(
        "https://api.trycloudflare.com/something\n"
        "https://real-tunnel-url.trycloudflare.com\n"
    )

    url = mgr._wait_for_url(log_file, timeout=2)
    assert url == "https://real-tunnel-url.trycloudflare.com"


def test_wait_for_url_timeout_returns_empty(tmp_path: Path) -> None:
    """URL extraction returns '' when no URL appears within timeout."""
    mgr, _, _, _ = _make_manager(tmp_path)
    log_file = tmp_path / "tunnel.log"
    log_file.write_text("Some other log output\n")

    url = mgr._wait_for_url(log_file, timeout=2)
    assert url == ""


# ── Tests: Tunnel health ─────────────────────────────────────────────


def test_alive_check_uses_poll(tmp_path: Path) -> None:
    """is_tunnel_alive returns True when process.poll() is None."""
    mgr, _, _, _ = _make_manager(tmp_path)
    mgr._tunnel_proc = FakePopen(alive=True)  # type: ignore[assignment]
    assert mgr._is_tunnel_alive() is True

    mgr._tunnel_proc = FakePopen(alive=False)  # type: ignore[assignment]
    assert mgr._is_tunnel_alive() is False


def test_reachable_check_probes_url(tmp_path: Path) -> None:
    """is_tunnel_reachable does an HTTP probe to the tunnel URL."""
    mgr, _, _, urlopen = _make_manager(tmp_path)
    mgr._tunnel_url = "https://test.trycloudflare.com"

    assert mgr._is_tunnel_reachable() is True
    assert any("test.trycloudflare.com" in c for c in urlopen.calls)


def test_reachable_returns_false_on_error(tmp_path: Path) -> None:
    """is_tunnel_reachable returns False when HTTP probe fails."""
    mgr, _, _, urlopen = _make_manager(tmp_path)
    mgr._tunnel_url = "https://test.trycloudflare.com"
    urlopen.set_fail(True)

    assert mgr._is_tunnel_reachable() is False


# ── Tests: Webhook ────────────────────────────────────────────────────


def test_webhook_set_on_success(tmp_path: Path) -> None:
    """Webhook is set when Telegram returns ok:true."""
    mgr, _, _, urlopen = _make_manager(tmp_path)
    urlopen.set_response("setWebhook", {"ok": True})

    result = mgr._set_webhook_with_retry("https://test.trycloudflare.com")
    assert result is True
    assert any("setWebhook" in c for c in urlopen.calls)


def test_webhook_retry_on_failure(tmp_path: Path) -> None:
    """Webhook retries on initial failure (DNS propagation delay)."""
    mgr, _, clock, urlopen = _make_manager(
        tmp_path, webhook_retry_delays=(0, 1),
    )

    call_count = [0]
    original_urlopen = urlopen  # keep reference to original

    def counting_urlopen(req: Any, timeout: float = 10) -> Any:
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("DNS not ready")
        return original_urlopen(req, timeout)

    # Replace on the manager — Python dunder __call__ lookup goes
    # through the class, not the instance, so replacing urlopen.__call__
    # doesn't work; replace mgr._urlopen directly.
    mgr._urlopen = counting_urlopen
    urlopen.set_response("setWebhook", {"ok": True})

    result = mgr._set_webhook_with_retry("https://test.trycloudflare.com")
    assert result is True
    assert call_count[0] >= 2


# ── Tests: Poll fallback ─────────────────────────────────────────────


def test_poll_fallback_starts_and_stops(tmp_path: Path) -> None:
    """Poll fallback starts a thread and stops it cleanly."""
    mgr, _, _, urlopen = _make_manager(tmp_path)
    urlopen.set_response("deleteWebhook", {"ok": True})
    urlopen.set_response("getUpdates", {"ok": True, "result": []})

    mgr._start_poll_fallback()
    assert mgr.polling_active is True
    assert mgr._poll_thread is not None
    assert mgr._poll_thread.is_alive()

    mgr._stop_poll_fallback()
    assert mgr.polling_active is False


def test_poll_forwards_updates_via_callback(tmp_path: Path) -> None:
    """Poll loop forwards updates to the on_update callback."""
    mgr, _, _, urlopen = _make_manager(tmp_path)
    urlopen.set_response("deleteWebhook", {"ok": True})

    fake_update = {"update_id": 42, "message": {"text": "hello", "chat": {"id": 1}}}

    call_counter = [0]

    def fake_urlopen(req: Any, timeout: float = 10) -> FakeHTTPResponse:
        url = req.full_url if hasattr(req, "full_url") else str(req)
        call_counter[0] += 1
        if "getUpdates" in url:
            if call_counter[0] <= 2:  # return update on first call
                return FakeHTTPResponse(json.dumps({"ok": True, "result": [fake_update]}).encode())
            # stop after first batch
            mgr._poll_stop.set()
            return FakeHTTPResponse(json.dumps({"ok": True, "result": []}).encode())
        return FakeHTTPResponse(json.dumps({"ok": True}).encode())

    mgr._urlopen = fake_urlopen
    mgr._start_poll_fallback()
    time.sleep(0.5)
    mgr._stop_poll_fallback()

    assert len(mgr._updates) >= 1  # type: ignore[attr-defined]
    assert mgr._updates[0]["update_id"] == 42  # type: ignore[attr-defined]


# ── Tests: Restart with retry ────────────────────────────────────────


def test_restart_returns_url_on_success(tmp_path: Path) -> None:
    """Restart spawns new cloudflared and returns URL."""
    mgr, runner, _, _ = _make_manager(tmp_path)

    runner.set_next_popen(FakePopen())

    # _start_cloudflared truncates tunnel.log before spawning, so we
    # can't pre-write the URL.  Patch _wait_for_url to return directly.
    mgr._wait_for_url = lambda _log_file, _timeout=3: "https://new-tunnel.trycloudflare.com"  # type: ignore[assignment]

    url = mgr._restart_with_retry()
    assert "trycloudflare.com" in url


def test_restart_returns_empty_after_max_attempts(tmp_path: Path) -> None:
    """Restart returns '' after exhausting all attempts."""
    mgr, runner, _, _ = _make_manager(tmp_path, max_restart_attempts=2)

    # All attempts fail (no URL in log)
    url = mgr._restart_with_retry()
    assert url == ""


# ── Tests: Status ─────────────────────────────────────────────────────


def test_status_dict_has_expected_keys(tmp_path: Path) -> None:
    """status() returns a dict with mode, state, url, and polling."""
    mgr, _, _, _ = _make_manager(tmp_path, mode="auto")
    s = mgr.status()
    assert "mode" in s
    assert "state" in s
    assert "tunnel_url" in s
    assert "polling_active" in s
    assert s["mode"] == "auto"
    assert s["state"] == "stopped"


# ── Tests: File persistence ──────────────────────────────────────────


def test_tunnel_url_saved_to_file(tmp_path: Path) -> None:
    """Tunnel URL is persisted to node_dir/tunnel_url."""
    mgr, _, _, _ = _make_manager(tmp_path)
    mgr._save_tunnel_url("https://test.trycloudflare.com")
    assert (tmp_path / "tunnel_url").read_text() == "https://test.trycloudflare.com"


def test_pid_saved_to_file(tmp_path: Path) -> None:
    """Cloudflared PID is persisted to node_dir/tunnel.pid."""
    mgr, _, _, _ = _make_manager(tmp_path)
    mgr._save_pid(12345)
    assert (tmp_path / "tunnel.pid").read_text() == "12345"


def test_bot_info_saved(tmp_path: Path) -> None:
    """Bot ID and username are saved for status command."""
    mgr, _, _, urlopen = _make_manager(tmp_path)
    urlopen.set_response("getMe", {"ok": True, "result": {"id": 999, "username": "mybot"}})

    mgr._save_bot_info()
    assert (tmp_path / "bot_id").read_text() == "999"
    assert (tmp_path / "bot_username").read_text() == "mybot"


# ── Tests: Port wait ─────────────────────────────────────────────────


def test_port_wait_returns_false_on_timeout(tmp_path: Path) -> None:
    """_wait_for_port returns False when port never opens."""
    mgr, _, _, _ = _make_manager(tmp_path, port_wait_timeout=2)
    # Port 19999 is not bound — should timeout
    result = mgr._wait_for_port()
    assert result is False


# ── Helpers ───────────────────────────────────────────────────────────


def _simulate_port_ready(mgr: tunnel.TunnelManager) -> None:
    """Patch _wait_for_port to always succeed."""
    mgr._wait_for_port = lambda: True  # type: ignore[assignment]


def _write_tunnel_url_to_log(tmp_path: Path, url: str) -> None:
    """Write a cloudflared-style log line with the tunnel URL."""
    log_file = tmp_path / "tunnel.log"
    log_file.write_text(f"INF | {url}\n")
