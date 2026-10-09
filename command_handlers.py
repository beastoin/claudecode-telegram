"""Command handler functions — extracted from bridge.py CommandRouter class."""
from __future__ import annotations
import os
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import cast

import core
import claudecode
import health
import telegram
from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    BRIDGE_PUBLIC_URL, PORT, DEFAULT_BACKEND,
    TIMEOUT_TMUX_SEND, PENDING_TIMEOUT, VERSION,
)

def cmd_pilot(router, name: str, chat_id) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /pilot <name> [name2 ...]", outcome="Needs decision"); return True
    names = name.lower().strip().split(); prefix = os.environ.get("TMUX_PREFIX", "claude-prod-")
    pilot_port = os.environ.get("PILOT_PORT", "10170")
    import urllib.request, json as _json
    from urllib.parse import urlparse, quote as _urlquote
    if "all" in names:
        from bridge import worker_manager, _load_registry
        assert worker_manager is not None
        registered = worker_manager.scan_tmux_sessions(); registry = _load_registry()
        for rname, rinfo in registry.get("workers", {}).items():
            if rinfo.get("host") and rname not in registered: registered[rname] = {"tmux": f"{prefix}{rname}", "host": rinfo["host"]}
        if not registered:
            router.reply(chat_id, "No active workers found", outcome="Needs decision"); return True
        names = sorted(registered.keys())
    enabled = []; session_names = []; errors = []
    for n in names:
        session_name = f"{prefix}{n}" if not n.startswith("claude-") else n
        try:
            worker_host = health.get_worker_host(n)
            url = f"http://localhost:{pilot_port}/api/pilot?session={session_name}"
            if worker_host: url += f"&host={_urlquote(worker_host)}"
            req = urllib.request.Request(url, method="POST")
            with core._urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp: cast(dict[str, object], _json.loads(resp.read()))
            enabled.append(n); session_names.append(session_name)
        except (urllib.error.URLError, OSError, TimeoutError) as e: errors.append(f"{n}: {e}")
    if not enabled:
        router.reply(chat_id, f"Pilot error: {'; '.join(errors)}", outcome="Needs decision"); return True
    ts = time.strftime("%m%d-%H%M", time.gmtime(core._clock.time()))
    if len(enabled) <= 3: slug = "-".join(enabled) + "-" + ts
    else: slug = f"team{len(enabled)}-{ts}"
    try:
        payload = _json.dumps({"slug": slug, "sessions": session_names, "ttl": 1800}).encode()
        req = urllib.request.Request(
            f"http://localhost:{pilot_port}/api/grid-session",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        with core._urlopen(req, timeout=TIMEOUT_TMUX_SEND) as resp: cast(dict[str, object], _json.loads(resp.read()))
    except (urllib.error.URLError, OSError, TimeoutError) as exc: _log(_LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")
    from urllib.parse import urlparse, quote as _urlquote
    host = urlparse(BRIDGE_PUBLIC_URL).hostname if BRIDGE_PUBLIC_URL else "localhost"
    pilot_url = f"http://{host}:{pilot_port}/grid/{_urlquote(slug)}"; names_str = ", ".join(enabled)
    msg = f"✈️ Pilot: {names_str} (30min)\n{pilot_url}"
    if errors: msg += f"\n⚠️ Failed: {'; '.join(errors)}"
    router.reply(chat_id, msg)
    return True

def cmd_rewind(router, name: str, chat_id) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /rewind <name>", outcome="Needs decision"); return True
    name = name.lower().strip()
    import secrets
    from bridge import tokens, _render_transcript_html, _beast_serve_deploy
    base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
    token = secrets.token_urlsafe(32)
    tokens.add_rewind(token, name)
    url = f"{base_url}/transcript/{name}?token={token}"
    try:
        html_content = _render_transcript_html(
            name, per_page=200, token=token,
            live_base_url=f"{base_url}/transcript/{name}")
        snap_path = f"/tmp/rewind-{name}.html"
        with open(snap_path, "w") as f: f.write(html_content)
        serve_url = _beast_serve_deploy(snap_path, f"rewind-{name}")
        if serve_url:
            router.reply(chat_id, f"⏪ Rewind for {name}\n{serve_url}"); return True
    except OSError as e: _log(_LOG_WARN, "bridge", f"Rewind snapshot deploy failed for {name}: {e}")
    router.reply(chat_id, f"⏪ Rewind for {name}\n{url}")
    return True

def cmd_pr_review(router, arg: str, chat_id) -> bool:
    if not arg:
        router.reply(chat_id, "Usage: /pr <github_pr_url>\nExample: /pr https://github.com/BasedHardware/omi/pull/6426", outcome="Needs decision"); return True
    arg = arg.strip(); clean_url = arg.split('#')[0]
    m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', clean_url)
    if not m:
        try:
            pr_num = int(clean_url); owner, repo = 'BasedHardware', 'omi'
        except ValueError:
            router.reply(chat_id, "Invalid PR URL. Example: /pr https://github.com/BasedHardware/omi/pull/6426", outcome="Needs decision"); return True
    else: owner, repo, pr_num = m.group(1), m.group(2), int(m.group(3))
    router.reply(chat_id, f"Generating PR review for {owner}/{repo}#{pr_num}...")
    script_path = Path(__file__).parent / "review.py"; out_path = f"/tmp/pr-review-{pr_num}.html"
    try:
        r = core._subprocess_runner.run(
            [sys.executable, str(script_path), arg, "--no-serve"],
            capture_output=True, text=True, timeout=PENDING_TIMEOUT)
        if r.returncode != 0 or not os.path.exists(out_path):
            router.reply(chat_id, f"Failed to generate PR review:\n{r.stderr[:500]}", outcome="Needs decision"); return True
    except subprocess.TimeoutExpired:
        router.reply(chat_id, "PR review generation timed out (>300s).", outcome="Needs decision"); return True
    from bridge import _beast_serve_deploy
    slug = f"pr-{pr_num}"; serve_url = _beast_serve_deploy(out_path, slug)
    if serve_url: router.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{serve_url}")
    else:
        import secrets
        from bridge import tokens
        token = secrets.token_urlsafe(32)
        tokens.add_pr_review(token, pr_num, owner, repo)
        base_url = BRIDGE_PUBLIC_URL or f"http://localhost:{PORT}"
        url = f"{base_url}/tools/review/{pr_num}?token={token}"
        router.reply(chat_id, f"PR #{pr_num}: {owner}/{repo}\n{url}")
    return True

def cmd_team(router, chat_id) -> bool:
    from bridge import state
    registered = router.workers.scan_tmux_sessions(); registered = router.workers.get_registered_sessions(registered)
    if not registered:
        router.reply(chat_id, "No team members yet. Add someone with /hire <name>."); return True
    worker_live = {}
    for name, session in registered.items():
        backend_name = claudecode.get_worker_backend(name, session)
        activity = None; context_pct = None
        tmux_name = session.get("tmux", f"{router.workers.tmux_prefix}{name}"); host = health.get_worker_host(name)
        tmux_alive = "tmux" in session and claudecode.tmux_exists(tmux_name, host=host)
        if tmux_alive:
            backend = claudecode.get_backend(backend_name)
            if backend.is_interactive:
                if claudecode.is_claude_running(tmux_name, host=host): activity, context_pct, _ = health._read_tmux_activity(tmux_name, host=host)
                else: activity = "worker app not running"
            else: activity = health._read_noninteractive_activity(name)
        worker_live[name] = {"backend": backend_name, "activity": activity, "context_pct": context_pct}
    lines = health.format_team_lines(registered, state.active, worker_live=worker_live)
    router.reply(chat_id, "\n".join(lines))
    return True

def cmd_status(router, chat_id) -> bool:
    import time as _time
    from bridge import state, watchdog, tunnel_manager, get_machine_catalog, _get_connectors_status
    registered = router.workers.get_registered_sessions()
    try:
        uptime_sec = int(_time.time() - os.stat(f"/proc/{os.getpid()}").st_mtime)
        d, rem = divmod(uptime_sec, 86400); h, rem = divmod(rem, 3600); m, _ = divmod(rem, 60)
        uptime_str = f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m" if h > 0 else f"{m}m"
    except (OSError, ValueError): uptime_str = "unknown"
    tm_status = tunnel_manager.status() if tunnel_manager else {}
    tunnel_url = tm_status.get("tunnel_url", "")
    inbound = f"webhook ({tunnel_url})" if tunnel_url else "poll" if tm_status.get("polling_active", False) else "DOWN"
    busy = 0; idle = 0; offline = 0; busy_names = []
    for name, session in registered.items():
        if router.workers.is_online(name, session):
            wd = watchdog.worker_states.get(name)
            if wd and wd.status == "BUSY": busy += 1; busy_names.append(name)
            else: idle += 1
        else: offline += 1
    total = busy + idle + offline; machine_workers = {}
    ssh_to_display = {None: "local"}
    for mach in get_machine_catalog().values():
        if mach.ssh_target: ssh_to_display[mach.ssh_target] = mach.display_name or mach.id
    for name in registered:
        host = health.get_worker_host(name); machine_workers.setdefault(ssh_to_display.get(host, host or "local"), []).append(name)
    conn_parts = [f"{cn} ({info.get('consecutive_failures', 0)} err)" if info.get("consecutive_failures", 0) else cn
        for cn, info in _get_connectors_status().items() if info.get("running", False)]
    lines = [f"<b>v{VERSION}</b> | up {uptime_str} | {inbound}", "",
        f"<b>{total}</b> workers: {busy} busy, {idle} idle, {offline} off", f"focus: <b>{state.active or 'none'}</b>"]
    if busy_names: lines.append(f"busy: {', '.join(sorted(busy_names))}")
    if len(machine_workers) > 1:
        lines.append(" / ".join(f"{label} {len(ws)}" for label, ws in sorted(machine_workers.items())))
    if conn_parts: lines.append(", ".join(conn_parts))
    if router.transport is not None and chat_id is not None: router.transport.send_text(chat_id, "\n".join(lines), parse_mode="HTML")
    return True

def cmd_focus(router, name: str, chat_id) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /focus <name>", outcome="Needs decision"); return True
    name = name.lower().strip()
    from bridge import switch_session
    ok, err = switch_session(name)
    if ok: router.reply(chat_id, f"Now talking to {name.capitalize()}.")
    else: router.reply(chat_id, f"Could not focus \"{name}\". {err}", outcome="Needs decision")
    return True

def cmd_hire(router, name: str, chat_id) -> bool:
    if not name:
        router.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision"); return True
    from bridge import parse_hire_args, RESERVED_NAMES, create_session, PERSISTENCE_NOTE
    parsed_name, backend = parse_hire_args(name)
    if not parsed_name:
        router.reply(chat_id, "Usage: /hire <name>", outcome="Needs decision"); return True
    name = parsed_name.lower().strip(); name = re.sub(r'[^a-z0-9-]', '', name)
    if not name:
        router.reply(chat_id, "Name must use letters, numbers, and hyphens only.", outcome="Needs decision"); return True
    if name in RESERVED_NAMES:
        router.reply(chat_id, f"Cannot use \"{name}\" - reserved command. Choose another name.", outcome="Needs decision"); return True
    ok, err = create_session(name, backend, chat_id=chat_id)
    if ok:
        router.reply(chat_id, f"{name.capitalize()} is added and assigned. {PERSISTENCE_NOTE}")
        telegram.update_bot_commands()
    else: router.reply(chat_id, f"Could not hire \"{name}\". {err}", outcome="Needs decision")
    return True

def cmd_end(router, name: str, chat_id) -> bool:
    if not name:
        router.reply(chat_id, "This is permanent. Usage: /end <name>", outcome="Needs decision"); return True
    name = name.lower().strip()
    from bridge import kill_session
    ok, err = kill_session(name)
    if ok:
        router.reply(chat_id, f"{name.capitalize()} removed from your team.")
        telegram.update_bot_commands()
    else: router.reply(chat_id, f"Could not remove \"{name}\". {err}", outcome="Needs decision")
    return True

def cmd_restart(router, chat_id, args: str = "") -> bool:
    from bridge import state, watchdog
    args = (args or "").strip(); clean = False; force = False; tokens = args.split()
    remaining = []
    for t in tokens:
        if t == "--clean": clean = True
        elif t == "--force": force = True
        else: remaining.append(t)
    name_arg = remaining[0].lower() if remaining else ""
    if name_arg == "cancel": return _cmd_restart_cancel(router, chat_id)
    if name_arg == "all": return _cmd_restart_all(router, chat_id, clean)
    if len(remaining) > 1:
        names = [n.lower() for n in remaining]
        router.reply(chat_id, f"Restarting {len(names)} workers: {', '.join(names)}...")
        for n in names: cmd_restart(router, chat_id, f"{'--clean ' if clean else ''}{'--force ' if force else ''}{n}")
        return True
    if name_arg: name = name_arg
    else:
        if not state.active:
            registered = router.workers.get_registered_sessions()
            if len(registered) == 1:
                name = next(iter(registered)); state.active = name
                telegram.save_last_active(name)
            else:
                router.reply(chat_id, "No one assigned."); return True
        else: name = state.active
    registered = router.workers.get_registered_sessions(); session = registered.get(name)
    if name not in registered:
        if registered:
            names_str = ", ".join(registered.keys())
            router.reply(chat_id, f"Can't find \"{name}\". Available workers: {names_str}")
        else: router.reply(chat_id, "No team members yet. Add someone with /hire <name>.")
        return True
    if name_arg:
        state.active = name; telegram.save_last_active(name)
    host = health.get_worker_host(name)
    tmux_name = session.get("tmux", f"{router.workers.tmux_prefix}{name}") if session else f"{router.workers.tmux_prefix}{name}"
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
            _log(_LOG_INFO, "cmd_restart", f"{name}: BLOCKED (restart in progress since {core._clock.time() - inflight_ts:.0f}s ago)")
            router.reply(chat_id, f"{name.capitalize()} restart already in progress. Wait for it to finish.")
            return True
        watchdog.restart_in_progress[name] = core._clock.time()
    try:
        result = _do_restart(router, name, session, chat_id, host, tmux_name, force, clean)
        if force: watchdog.force_restart_pending_cwd[name] = True
        return result
    finally:
        with watchdog.restart_lock: watchdog.restart_in_progress.pop(name, None)

def _do_restart(router, name, session, chat_id, host, tmux_name, force, clean) -> bool:
    from bridge import restart_claude
    if host:
        mode = "relaunch" if clean else "resume"
        backend_name = claudecode.get_worker_backend(name, session) if session else DEFAULT_BACKEND
        backend_obj = claudecode.get_backend(backend_name)
        _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart mode={mode}, host={host}")
        router.reply(chat_id, f"Restarting {name.capitalize()} on remote host...")
        from bridge import watchdog
        ok, err = router._restart_remote_worker(name, backend_name, backend_obj, tmux_name, host, mode)
        watchdog.recent_restarts[name] = core._clock.time()
        _log(_LOG_INFO, "cmd_restart", f"{name}: remote restart result ok={ok}, err={err}")
        return _restart_reply(router, chat_id, name, ok, err, host)
    backend_name = claudecode.get_worker_backend(name, session) if session else DEFAULT_BACKEND
    backend = claudecode.get_backend(backend_name)
    tmux_name = session.get("tmux", f"{router.workers.tmux_prefix}{name}") if session else f"{router.workers.tmux_prefix}{name}"
    if not clean and not backend.is_interactive and session and "tmux" in session and claudecode.tmux_exists(tmux_name):
        session_id, source = claudecode.get_any_session_id(name)
        if session_id: router.reply(chat_id, f"{name.capitalize()} is still active. Next message continues where you left off.")
        else: router.reply(chat_id, f"No active session for {name.capitalize()}. Next message starts fresh.")
        return True
    mode = "relaunch" if clean else "resume"
    if mode == "resume":
        session_dir = claudecode.get_session_dir(name)
        if not session_dir.exists() or not any(session_dir.glob("*_session_id")): mode = "relaunch"
    label = "Resuming" if mode == "resume" else ("Bringing" if clean else "Restarting")
    router.reply(chat_id, f"{label} {name.capitalize()}...")
    ok, err = restart_claude(name, mode=mode)
    if ok:
        from bridge import watchdog
        watchdog.recent_restarts[name] = core._clock.time()
    return _restart_reply(router, chat_id, name, ok, err)

def _restart_reply(router, chat_id, name, ok, err, host=None) -> bool:
    if ok: router.reply(chat_id, f"{name.capitalize()} is back and ready.")
    else:
        loc = f" on {host}" if host else ""
        router.reply(chat_id, f"Could not restart \"{name}\"{loc}. {err}", outcome="Needs decision")
    return True

def _cmd_restart_all(router, chat_id, clean) -> bool:
    import threading
    from bridge import state
    registered = router.workers.get_registered_sessions()
    if not registered:
        router.reply(chat_id, "No team members yet. Add someone with /hire <name>."); return True
    with router._restart_all_lock:
        if router._restart_all_running:
            router.reply(chat_id, "A /restart all is already running. Use /restart cancel to stop it."); return True
        router._restart_all_running = True
        router._restart_all_abort.clear()
    names = sorted(registered.keys()); active = state.active
    if active and active in names:
        names.remove(active); names.append(active)
    mode = "relaunch" if clean else "resume"
    router.reply(chat_id, f"Restarting {len(names)} workers sequentially ({mode})...")
    router._restart_all_thread = threading.Thread(
        target=_run_restart_all_sequence, args=(router, chat_id, names, mode),
        name="restart-all", daemon=True)
    router._restart_all_thread.start()
    return True

def _run_restart_all_sequence(router, chat_id, names, mode) -> None:
    from bridge import restart_claude, watchdog, worker_manager, _sync_worker_manager
    delay_s = 7; failed = []
    try:
        total = len(names)
        for i, name in enumerate(names, 1):
            if router._restart_all_abort.is_set():
                router.reply(chat_id, f"Restart sequence aborted at {i-1}/{total}."); return
            host = health.get_worker_host(name)
            if host:
                _sync_worker_manager()
                assert worker_manager is not None
                reg = worker_manager.get_registered_sessions(); session = reg.get(name, {})
                backend_name = claudecode.get_worker_backend(name, session); backend_obj = claudecode.get_backend(backend_name)
                tmux_name = session.get("tmux", f"{router.workers.tmux_prefix}{name}")
                from teleport import restart_remote_worker
                ok, err = restart_remote_worker(name, backend_name, backend_obj, tmux_name, host, mode, router.workers)
            else: ok, err = restart_claude(name, mode=mode)
            if ok: router.reply(chat_id, f"[{i}/{total}] {name.capitalize()} restarted.")
            else:
                failed.append((name, err))
                router.reply(chat_id, f"[{i}/{total}] {name.capitalize()} failed: {err}")
            if i < total:
                for _ in range(5):
                    if router._restart_all_abort.is_set(): break
                    core._clock.sleep(delay_s / 5)
        if failed:
            summary = ", ".join(n for n, _ in failed)
            router.reply(chat_id, f"Restart all done. {len(failed)} failed: {summary}")
        else: router.reply(chat_id, f"Restart all done. All {total} workers restarted.")
    finally:
        with router._restart_all_lock:
            router._restart_all_running = False
            router._restart_all_abort.clear()
            router._restart_all_thread = None

def _cmd_restart_cancel(router, chat_id) -> bool:
    with router._restart_all_lock:
        if not router._restart_all_running:
            router.reply(chat_id, "No restart-all sequence is running."); return True
        router._restart_all_abort.set()
    router.reply(chat_id, "Stopping restart-all sequence...")
    return True
