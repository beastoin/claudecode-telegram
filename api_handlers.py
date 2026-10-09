"""Miscellaneous API HTTP handlers — extracted from bridge.py Handler class."""
from __future__ import annotations
import json
import os
import subprocess
import urllib.error
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs

import core
import claudecode
import health
import telegram
from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _str_field, _int_field,
    DEFAULT_BACKEND, TMUX_PREFIX, NODE_NAME, TIMEOUT_TMUX_SEND,
)
from claudecode import (
    get_backend, get_worker_backend, normalize_cwd, validate_cwd,
    get_claude_session_id, get_claude_session_cwd, save_claude_session_cwd,
    _ensure_workspace_trusted, _log_session_event,
)
from telegram import _build_cwd_change_notice

if TYPE_CHECKING:
    from urllib.parse import ParseResult

def handle_notify(handler, body: bytes = b"") -> None:
    from bridge import _parse_response_media, _send_response_media, get_all_chat_ids
    try:
        data = cast(dict[str, object], json.loads(body)); text = _str_field(data, "text")
        name = _str_field(data, "name")
        if not text: handler._send_text(400, "Missing text"); return
        clean_text, images, files = _parse_response_media(name or "", text)
        chat_ids = get_all_chat_ids(); sent = 0; label = name or "notify"
        for chat_id in chat_ids:
            if clean_text:
                result = telegram.transport.send_text(chat_id, clean_text)
                if result and result.get("ok"): sent += 1
            _send_response_media(label, images, files, chat_id)
        has_media = len(images) + len(files)
        _log(_LOG_INFO, "notify", f"sent to {sent}/{len(chat_ids)} chats: {text[:50]}..."
             f"{f' ({has_media} media)' if has_media else ''}")
        handler._send_text(200, f"Sent to {sent} chats")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"Notify error: {e}")
        handler._send_text(500, str(e))

def handle_health_alert(handler, body: bytes = b"") -> None:
    from bridge import get_all_chat_ids
    try:
        data = json.loads(body) if body else {}
        worker = _str_field(data, "worker", "unknown"); issue = _str_field(data, "issue", "unknown")
        age = _int_field(data, "transcript_age")
        age_human = f"{age // 3600}h{(age % 3600) // 60}m" if age >= 3600 else f"{age // 60}m"
        alert_text = f"🔴 {worker}: JSONL transcript stale ({age_human}). Session active but not recording. `/restart {worker}` to fix."
        _log(_LOG_WARN, "health", f"Health alert: {worker} — {issue} (age={age}s)")
        chat_ids = get_all_chat_ids()
        for chat_id in chat_ids: telegram.transport.send_text(chat_id, alert_text)
        handler._send_json(200, {"ok": True})
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _log(_LOG_ERROR, "bridge", f"Health alert error: {e}")
        handler._send_json(500, {"ok": False, "error": str(e)})

def handle_forge_register(handler, body: bytes = b"") -> None:
    from bridge import _registry_add, _registry_add_callback, worker_manager
    try:
        data = json.loads(body) if body else {}
        name = _str_field(data, "Name") or _str_field(data, "name")
        host = _str_field(data, "Host") or _str_field(data, "host")
        version = _str_field(data, "Version") or _str_field(data, "version")
        callback_url = _str_field(data, "CallbackURL") or _str_field(data, "callback_url") or _str_field(data, "callbackUrl")
        tools = data.get("Tools", data.get("tools", {}))
        if name:
            if callback_url:
                _registry_add_callback(name, callback_url, host=host, version=version, tools=tools)
                _log(_LOG_INFO, "worker", f"Callback worker registered: {name} (host={host}, url={callback_url}, version={version})")
            else:
                _registry_add(name, DEFAULT_BACKEND, host=host)
                _log(_LOG_INFO, "worker", f"Forge worker registered: {name} (host={host}, version={version})")
            backend_name = get_worker_backend(name, {"host": host}); tmux_name = f"{TMUX_PREFIX}{name}"
            reg_host = host or None
            if claudecode.tmux_exists(tmux_name, host=reg_host): claudecode.export_hook_env(tmux_name, backend_name, host=reg_host)
            claudecode.ensure_session_dir(name)
            if core.admin_chat_id is not None:
                cid_file = claudecode.get_chat_id_file(name)
                if not cid_file.exists():
                    _tmp_cid = cid_file.with_suffix('.tmp'); _tmp_cid.write_text(str(core.admin_chat_id)); _tmp_cid.chmod(0o600); os.replace(str(_tmp_cid), str(cid_file))
        tmux_session = f"{TMUX_PREFIX}{name}" if name else ""; conflict = False; active_workers = []
        try:
            r = core._subprocess_runner.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                                       capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode == 0:
                active_workers = [s.removeprefix(TMUX_PREFIX)
                                  for s in r.stdout.strip().split("\n")
                                  if s.startswith(TMUX_PREFIX)]
                conflict = name in active_workers if name else False
        except (subprocess.SubprocessError, OSError) as exc: _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
        worker_manager.invalidate_sessions_cache()
        handler._send_json(200, {"ok": True, "settings": {"tmux_prefix": TMUX_PREFIX, "node_name": NODE_NAME or "", "tmux_session": tmux_session},
            "conflict": conflict, "active_workers": active_workers})
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError) as e:
        _log(_LOG_ERROR, "bridge", f"Register error: {e}")
        handler._send_json(500, {"ok": False, "error": str(e)})

def handle_checkin_endpoint(handler, parsed: ParseResult) -> None:
    from bridge import _sync_worker_manager, worker_manager, _set_worker_cwd, _checkin_can_restart, _checkin_do_restart
    try:
        params = parse_qs(parsed.query); name = params.get("name", ["worker"])[0]
        raw_cwd = params.get("cwd", [None])[0]; requested_cwd = ""
        if raw_cwd is not None:
            worker_host = health.get_worker_host(name)
            requested_cwd, cwd_err = validate_cwd(raw_cwd, host=worker_host)
            if cwd_err: handler._send_text(400, f"Invalid cwd: {cwd_err}"); return
        _sync_worker_manager()
        registered = worker_manager.get_registered_sessions(); tmux_name = ""
        host = None
        if name in registered:
            backend_name = get_worker_backend(name, registered[name])
            tmux_name = registered[name].get("tmux", f"{TMUX_PREFIX}{name}"); host = health.get_worker_host(name)
            if claudecode.tmux_exists(tmux_name, host=host): claudecode.export_hook_env(tmux_name, backend_name, host=host)
        else: backend_name = DEFAULT_BACKEND
        backend_obj = get_backend(backend_name)
        if requested_cwd:
            _set_worker_cwd(name, requested_cwd)
            old_cwd = get_claude_session_cwd(name)
            save_claude_session_cwd(name, requested_cwd)
            _ensure_workspace_trusted(requested_cwd)
            if old_cwd and old_cwd.rstrip("/") != requested_cwd.rstrip("/"):
                old_sid = get_claude_session_id(name)
                _log_session_event(name, old_sid or "(none)", requested_cwd, "cwd_change")
                _log(_LOG_WARN, "checkin", f"{name}: CWD changed ({old_cwd} -> {requested_cwd}), stale sessions will self-invalidate")
                notice = _build_cwd_change_notice(name, old_cwd, requested_cwd, old_sid or "")
                notify_chat_id = telegram.get_manager_chat_id(name)
                if notify_chat_id is not None: telegram.send_telegram_message(notify_chat_id, notice, parse_mode="HTML")
            _log(_LOG_INFO, "checkin", f"{name}: requested_cwd={requested_cwd}, tmux={tmux_name}, host={host}")
            if tmux_name and claudecode.tmux_exists(tmux_name, host=host):
                pane_cwd = normalize_cwd(worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
                same_cwd = pane_cwd and pane_cwd.rstrip("/") == requested_cwd.rstrip("/")
                _log(_LOG_INFO, "checkin", f"{name}: pane_cwd={pane_cwd}, same_cwd={same_cwd}")
                if not same_cwd:
                    allowed, block_msg = _checkin_can_restart(name, tmux_name, host, pane_cwd or "", requested_cwd)
                    if not allowed:
                        notify_chat_id = telegram.get_manager_chat_id(name)
                        if notify_chat_id is not None: telegram.send_telegram_message(notify_chat_id, block_msg)
                        handler._send_text(200, block_msg); return
                    _log(_LOG_INFO, "checkin", f"{name}: triggering restart (cwd mismatch: pane={pane_cwd} vs requested={requested_cwd})")
                    ok, err = _checkin_do_restart(name, backend_name, tmux_name, host, requested_cwd)
                    if not ok: handler._send_text(500, f"Failed to restart in {requested_cwd}: {err}")
                    else: handler._send_text(200, f"Restarting in {requested_cwd}...")
                    return
        handler._send_text(200, worker_manager._build_welcome(name, backend_obj))
    except (subprocess.SubprocessError, OSError, KeyError) as exc:
        _log(_LOG_ERROR, "bridge", f"Checkin endpoint error: {exc}")
        handler._send_text(500, str(exc))

def handle_health_workers_endpoint(handler) -> None:
    from bridge import watchdog
    try:
        now = core._clock.time(); registered = health.get_registered_sessions()
        with watchdog.lock: state_snapshot = dict(watchdog.worker_states)
        workers = {}
        for name in sorted(registered.keys()):
            entry = state_snapshot.get(name)
            if entry: workers[name] = {"state": entry.status, "reason": entry.reason, "since": entry.since, "age_sec": int(now - entry.since) if entry.since else None}
            else: workers[name] = {"state": "unknown"}
        handler._send_json(200, {"workers": workers})
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        _log(_LOG_ERROR, "worker", f"Health workers endpoint error: {e}"); handler._send_text(500, str(e))
