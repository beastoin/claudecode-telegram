#!/usr/bin/env python3
"""External bridge liveness watchdog — runs via cron every 2 minutes.

Checks ONLY bridge liveness: process alive + HTTP responding.
Health signal evaluation (gmail, github, threads, pools) is handled
exclusively by the internal alert system in bridge.py with routing.

This watchdog exists because only an external process can detect
bridge death — a dead bridge can't self-report.

Alert lifecycle (state-based, no spam):
  - Bridge dead/unresponsive → 🚨 alert once (Telegram DM + tmux)
  - Bridge recovers          → ✅ resolved once, clears state

State is persisted in .watchdog-state.json between cron runs.

Install: crontab -e → */2 * * * * /home/claude/claudecode-telegram/bridge-watchdog.py
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
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


def _gen_ref() -> str:
    return secrets.token_hex(3)


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
        if "bridge:dead" not in state.active:
            ref = _gen_ref()
            state.active["bridge:dead"] = now
            deliver_to_workers(cfg, f"🚨 {msg} ref:{ref}")
            telegram_send(cfg.bot_token, cfg.admin_chat, f"🚨 Bridge watchdog: {msg} ref:{ref}")
            _append_log(log_path, f"{ts} ALERT: {msg} ref:{ref}")
        state.save(state_file)
        return 1

    # Check 2: HTTP responding?
    health = http_get_json(f"http://127.0.0.1:{cfg.port}/health")
    if "error" in health:
        msg = f"Bridge HTTP not responding on port {cfg.port} (PID={pid})"
        if "bridge:http" not in state.active:
            ref = _gen_ref()
            state.active["bridge:http"] = now
            deliver_to_workers(cfg, f"🚨 {msg} ref:{ref}")
            telegram_send(cfg.bot_token, cfg.admin_chat, f"🚨 Bridge watchdog: {msg} ref:{ref}")
            _append_log(log_path, f"{ts} ALERT: {msg} ref:{ref}")
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

    # Bridge is alive and responding — health signal evaluation is handled
    # exclusively by the internal alert system (bridge.py _health_alert_check)
    version = health.get("version", "?")
    uptime = health.get("uptime_s", "?")
    ok = health.get("ok", False)
    _append_log(log_path, f"{ts} OK pid={pid} v={version} uptime={uptime}s health={'green' if ok else 'degraded'}")
    state.save(state_file)
    rotate_log(log_path, max_lines=500)
    rotate_bridge_log(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
