#!/usr/bin/env python3
"""External bridge health watchdog — runs via cron every 2 minutes.

Checks the unified /health endpoint. Alerts workers via tmux.
Falls back to Telegram bot API only when the bridge is completely dead.

Alert lifecycle (state-based, no spam):
  - New failure detected   → 🔶 alert once
  - Failure persists       → silent (already alerted)
  - Failure resolves       → ✅ resolved once, clears state
  - Bridge dead            → 🚨 Telegram DM (bypasses bridge)

State is persisted in .watchdog-state.json between cron runs.

Install: crontab -e → */2 * * * * /home/claude/claudecode-telegram/bridge-watchdog.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# --- Config ---


@dataclass(frozen=True, slots=True)
class Config:
    node: str
    port: int
    state_dir: Path
    env_file: Path
    bot_token: str
    admin_chat: str
    alert_workers: list[str]
    tmux_prefix: str

    @staticmethod
    def from_env() -> Config:
        node = os.environ.get("BRIDGE_NODE", "prod")
        port = int(os.environ.get("BRIDGE_PORT", "8271"))
        home = Path.home()
        state_dir = home / ".claude/telegram/nodes" / node
        env_file = home / f".config/claudecode-telegram/{node}.env"

        # Load env file for tokens
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"'))

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        admin_chat = os.environ.get("ADMIN_CHAT_ID", "")
        if not admin_chat:
            chat_file = state_dir / "last_chat_id"
            if chat_file.is_file():
                admin_chat = chat_file.read_text().strip()

        workers_str = os.environ.get("BRIDGE_ALERT_WORKERS", "")
        alert_workers = [w.strip() for w in workers_str.split(",") if w.strip()]
        tmux_prefix = os.environ.get("TMUX_PREFIX", f"claude-{node}-")

        return Config(
            node=node,
            port=port,
            state_dir=state_dir,
            env_file=env_file,
            bot_token=bot_token,
            admin_chat=admin_chat,
            alert_workers=alert_workers,
            tmux_prefix=tmux_prefix,
        )


# --- Alert state persistence ---


@dataclass(slots=True)
class AlertState:
    """Tracks which signals are currently failing across cron runs.

    Only state changes trigger alerts:
    - new key in `active` → fire alert
    - key removed from `active` → fire resolved
    - key still in `active` → silent (already alerted)
    """

    active: dict[str, float] = field(default_factory=dict)  # signal -> first_seen timestamp

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"active": self.active}, indent=2))

    @staticmethod
    def load(path: Path) -> AlertState:
        try:
            data = json.loads(path.read_text())
            return AlertState(active=data.get("active", {}))
        except (OSError, json.JSONDecodeError, KeyError):
            return AlertState()


# --- HTTP helpers ---


def http_get_json(url: str, timeout: int = 5) -> dict[str, Any]:
    """GET JSON from URL. Returns {"error": "..."} on any failure.

    Handles non-200 responses (e.g. 503 from /health when ok=false)
    by reading the response body — the bridge still sends valid JSON.
    """
    try:
        with urlopen(Request(url), timeout=timeout) as resp:
            return json.loads(resp.read())
    except URLError as e:
        # HTTPError is a subclass of URLError and has a readable body
        if hasattr(e, "read"):
            try:
                return json.loads(e.read())  # type: ignore[union-attr]
            except (json.JSONDecodeError, ValueError):
                pass
        return {"error": str(e)}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"error": str(e)}


def telegram_send(token: str, chat_id: str, text: str) -> None:
    """Send message via Telegram bot API (bypasses bridge)."""
    if not token or not chat_id:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urlopen(req, timeout=10):
            pass
    except (URLError, OSError):
        pass


# --- Delivery ---


def deliver_to_workers(cfg: Config, msg: str) -> None:
    """Send message to alert workers via tmux."""
    for worker in cfg.alert_workers:
        session = f"{cfg.tmux_prefix}{worker}"
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                ["tmux", "send-keys", "-t", session, f"bridge-watchdog: {msg}", "Enter"],
                capture_output=True,
                timeout=5,
            )


# --- Health evaluation ---


def collect_problems(cfg: Config, health: dict[str, Any]) -> dict[str, str]:
    """Extract current problems from health response, filtering snoozed signals."""
    # Get snoozed signals from bridge's alert system
    snoozed: set[str] = set()
    alerts = http_get_json(f"http://127.0.0.1:{cfg.port}/alerts", timeout=3)
    if "snoozed" in alerts:
        snoozed = set(alerts["snoozed"].keys())

    problems: dict[str, str] = {}  # key -> human description

    for sig, info in health.get("signals", {}).items():
        if not info.get("ok") and sig not in snoozed:
            age = info.get("last_ok_ago_s", "?")
            errors = info.get("errors", 0)
            problems[sig] = f"{sig}(age={age}s, errors={errors})"

    for tname, alive in health.get("threads", {}).items():
        key = f"thread:{tname}"
        if not alive and key not in snoozed:
            problems[key] = f"thread {tname} DEAD"

    for pname, pinfo in health.get("pools", {}).items():
        if isinstance(pinfo, dict):
            pending = pinfo.get("pending", 0)
            mx = pinfo.get("max", 1)
            if pending > mx * 2:
                problems[f"pool:{pname}"] = f"{pname} saturated ({pending} pending)"

    return problems


def reconcile_alerts(
    state: AlertState,
    current_problems: dict[str, str],
    now: float,
) -> tuple[list[str], list[str]]:
    """Compare previous state with current problems.

    Returns (new_alerts, resolved_alerts) — only state changes.
    Unchanged problems are silent (already alerted on previous run).
    """
    new_alerts: list[str] = []
    resolved: list[str] = []

    # Detect resolved: was active, now OK
    for key in list(state.active):
        if key not in current_problems:
            resolved.append(key)
            del state.active[key]

    # Detect new failures: not previously active
    for key, desc in current_problems.items():
        if key not in state.active:
            state.active[key] = now
            new_alerts.append(f"🔶 {desc}")

    return new_alerts, resolved


# --- Log rotation ---


def _append_log(path: Path, line: str) -> None:
    with open(path, "a") as f:
        f.write(line + "\n")


def rotate_log(path: Path, max_lines: int) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except OSError:
        pass


def rotate_bridge_log(cfg: Config, max_bytes: int = 5_242_880) -> None:
    """Copytruncate bridge.log if over max_bytes."""
    bridge_log = cfg.state_dir / "bridge.log"
    if not bridge_log.is_file():
        return
    try:
        if bridge_log.stat().st_size > max_bytes:
            shutil.copy2(bridge_log, cfg.state_dir / "bridge.log.1")
            with open(bridge_log, "r+") as f:
                f.truncate(0)
    except OSError:
        pass


# --- Main ---


def main() -> int:
    cfg = Config.from_env()
    state_file = cfg.state_dir / ".watchdog-state.json"
    state = AlertState.load(state_file)
    log_path = cfg.state_dir / "watchdog.log"
    now = time.time()
    ts = datetime.now(UTC).isoformat()

    # Check 1: process alive?
    pid: int | None = None
    for name in ("bridge.pid", "pid"):
        pidfile = cfg.state_dir / name
        if pidfile.is_file():
            try:
                p = int(pidfile.read_text().strip())
                os.kill(p, 0)
                pid = p
                break
            except (ValueError, OSError):
                continue

    if pid is None:
        msg = f"Bridge process not running (node={cfg.node})"
        # Bridge dead is a special state — always alert (can't snooze via bridge)
        if "bridge:dead" not in state.active:
            state.active["bridge:dead"] = now
            deliver_to_workers(cfg, f"🚨 {msg}")
            telegram_send(cfg.bot_token, cfg.admin_chat, f"🚨 Bridge watchdog: {msg}")
            _append_log(log_path, f"{ts} ALERT: {msg}")
        state.save(state_file)
        return 1

    # Check 2: HTTP responding?
    health = http_get_json(f"http://127.0.0.1:{cfg.port}/health")
    if "error" in health:
        msg = f"Bridge HTTP not responding on port {cfg.port} (PID={pid})"
        if "bridge:http" not in state.active:
            state.active["bridge:http"] = now
            deliver_to_workers(cfg, f"🚨 {msg}")
            telegram_send(cfg.bot_token, cfg.admin_chat, f"🚨 Bridge watchdog: {msg}")
            _append_log(log_path, f"{ts} ALERT: {msg}")
        state.save(state_file)
        return 1

    # Bridge is alive — clear bridge-level failures if they were active
    bridge_resolved: list[str] = []
    for bkey in ("bridge:dead", "bridge:http"):
        if bkey in state.active:
            del state.active[bkey]
            bridge_resolved.append(bkey)
    if bridge_resolved:
        deliver_to_workers(cfg, f"✅ Bridge recovered ({', '.join(bridge_resolved)})")
        _append_log(log_path, f"{ts} RESOLVED: {', '.join(bridge_resolved)}")

    # Check 3: health signals/threads/pools
    if health.get("ok"):
        # Everything is green — resolve any leftover active alerts
        if state.active:
            resolved_keys = list(state.active.keys())
            state.active.clear()
            deliver_to_workers(cfg, f"✅ Resolved: {', '.join(resolved_keys)}")
            _append_log(log_path, f"{ts} RESOLVED: {', '.join(resolved_keys)}")

        version = health.get("version", "?")
        uptime = health.get("uptime_s", "?")
        _append_log(log_path, f"{ts} OK pid={pid} v={version} uptime={uptime}s")
        state.save(state_file)
        rotate_log(log_path, max_lines=500)
        rotate_bridge_log(cfg)
        return 0

    # Not fully OK — collect and reconcile
    problems = collect_problems(cfg, health)

    if not problems:
        # All failures are snoozed — treat as OK from watchdog's perspective
        version = health.get("version", "?")
        _append_log(log_path, f"{ts} OK pid={pid} v={version} (all failures snoozed)")
        # Still resolve any previously-active alerts that are now snoozed
        if state.active:
            resolved_keys = [k for k in state.active if k not in problems]
            for k in resolved_keys:
                del state.active[k]
            if resolved_keys:
                deliver_to_workers(cfg, f"✅ Resolved (snoozed): {', '.join(resolved_keys)}")
        state.save(state_file)
        rotate_log(log_path, max_lines=500)
        rotate_bridge_log(cfg)
        return 0

    new_alerts, resolved = reconcile_alerts(state, problems, now)

    # Deliver resolved notifications
    if resolved:
        deliver_to_workers(cfg, f"✅ Resolved: {', '.join(resolved)}")
        _append_log(log_path, f"{ts} RESOLVED: {', '.join(resolved)}")

    # Deliver new alerts only (no repeats for known failures)
    if new_alerts:
        deliver_to_workers(cfg, "\n".join(new_alerts))
        _append_log(log_path, f"{ts} ALERT: {'; '.join(new_alerts)}")

    if not new_alerts and not resolved:
        # No state change — silent heartbeat
        _append_log(log_path, f"{ts} WATCH pid={pid} active={list(state.active.keys())}")

    state.save(state_file)
    rotate_log(log_path, max_lines=500)
    rotate_bridge_log(cfg)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
