from __future__ import annotations
import json
import os
import shlex
import subprocess
import tempfile
import threading
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING, cast
from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _subprocess_runner, _clock,
)
from claudecode import (
    GIT_SERVER_DIR, GitPushStateResult,
    _get_remote_home, _is_git_repo, _project_slug, _remap_path,
    _remote_run, _resolve_remote_tool,
    ensure_session_dir, get_claude_session_cwd, get_claude_session_id,
    get_session_dir, get_worker_backend,
    is_claude_running, normalize_backend, save_claude_session_cwd,
    clear_claude_session_id, get_any_session_id,
    get_backend, tmux_exists, watchdog,
)
def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    import bridge as _b; return _b._get_claude_pid(pane_pid, host=host)
def get_worker_host(name: str) -> str | None:
    import bridge as _b; return _b.get_worker_host(name)
if TYPE_CHECKING:
    from claudecode import Backend, TmuxSessionDict
import _teleport_ops as _tpo_mod
from _teleport_ops import (
    _bare_repo_url, _build_teleport_context, _ensure_bare_repo, _ensure_workspace_trusted_remote,
    _get_project_name, _git_pull_state, _git_push_state, check_teleback_conflicts,
    install_hooks_on_target, rsync_working_directory, run_teleport_preflight,
    start_worker_on_target, stop_worker_for_teleport, sync_credentials_to_target,
    sync_session_files_to_target, sync_session_transcript, sync_shared_repos,
    sync_worker_data_back, sync_working_directory, teleport_notify, teleport_rollback)
from telegram import ChatId
def do_teleport(name: str, target_host: str, target_cwd: str, full_sync: bool,
                chat_id: "int | str", workers: object, is_teleback: bool = False) -> None:
    import bridge
    try:
        registered = workers.get_registered_sessions(); session = registered.get(name, {})  # type: ignore[attr-defined]
        tmux_name = session.get("tmux", f"{bridge.TMUX_PREFIX}{name}"); backend_name = get_worker_backend(name, session)
        source_host = get_worker_host(name); source_cwd = get_claude_session_cwd(name) or ""
        _log(_LOG_INFO, "teleport", f"{name}: source_host={source_host}, source_cwd={source_cwd}, target_host={target_host}, target_cwd={target_cwd}")
        if not target_cwd: target_cwd = _remap_path(source_cwd, target_host) if target_host else source_cwd
        if target_cwd and target_cwd.startswith("~"):
            if target_host:
                rh = _get_remote_home(target_host)
                if rh: target_cwd = rh + target_cwd[1:]
            else: target_cwd = os.path.expanduser(target_cwd)
        ensure_session_dir(name)
        state_file = bridge.SESSIONS_DIR / name / "teleport_state"; _tmp_ts = state_file.with_suffix('.tmp')
        _tmp_ts.write_text(json.dumps({"phase": 1, "source_host": source_host, "target_host": target_host,
            "target_cwd": target_cwd, "started_at": int(_clock.time())}))
        os.replace(str(_tmp_ts), str(state_file))
        teleport_notify(chat_id, f"Stopping {name}...")
        session_id = stop_worker_for_teleport(name, tmux_name, source_host)
        _log(_LOG_INFO, "teleport", f"{name}: stopped, session_id={session_id}")
        if source_cwd and target_cwd:
            teleport_notify(chat_id, f"Syncing working directory...")
            _log(_LOG_INFO, "teleport", f"{name}: syncing {source_cwd} → {target_cwd}")
            ok = sync_working_directory( source_cwd, target_cwd, source_host, target_host, full_sync)
            _log(_LOG_INFO, "teleport", f"{name}: working dir sync ok={ok}")
            if not ok:
                teleport_rollback(name, tmux_name, source_host, source_cwd,
                                        session_id, backend_name, chat_id,
                                        "working directory sync failed")
                return
        if session_id:
            teleport_notify(chat_id, "Syncing session transcript...")
            sync_session_transcript( session_id, source_cwd, target_cwd, source_host, target_host)
            _log(_LOG_INFO, "teleport", f"{name}: transcript sync done")
        if not is_teleback:
            teleport_notify(chat_id, "Syncing team config and hooks...")
            sw = sync_shared_repos(target_host, chat_id)
            hook_warnings = install_hooks_on_target(target_host); all_warnings = sw + hook_warnings
            if all_warnings:
                teleport_notify(
                    chat_id,
                    f"Config sync completed with warnings:\n" +
                    "\n".join(f"- {w}" for w in all_warnings))
            _log(_LOG_WARN, "teleport", f"{name}: team config + hooks synced ({len(all_warnings)} warnings)")
        _tmp_ts2 = state_file.with_suffix('.tmp')
        _tmp_ts2.write_text(json.dumps({"phase": 2, "source_host": source_host, "target_host": target_host,
            "target_cwd": target_cwd, "started_at": int(_clock.time())}))
        os.replace(str(_tmp_ts2), str(state_file))
        save_claude_session_cwd(name, target_cwd)
        clear_claude_session_id(name)
        _ensure_workspace_trusted_remote(target_cwd, target_host)
        teleport_notify(chat_id, f"Starting {name} on {target_host or 'local'}...")
        resume_id = session_id
        _log(_LOG_INFO, "teleport", f"{name}: calling _start_worker_on_target(target_cwd={target_cwd}, session_id={resume_id}, backend={backend_name})")
        ok = start_worker_on_target( name, target_host, target_cwd, resume_id, backend_name)
        _log(_LOG_INFO, "teleport", f"{name}: _start_worker_on_target returned {ok}")
        if not ok:
            _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host, capture_output=True)
            teleport_rollback(name, tmux_name, source_host, source_cwd,
                                    session_id, backend_name, chat_id,
                                    "failed to start on target")
            return
        if is_teleback: bridge._registry_clear_teleport(name)
        else: bridge._registry_update_teleport( name, host=target_host, home_host=source_host, home_cwd=source_cwd)
        _remote_run(["tmux", "kill-session", "-t", tmux_name],
                    host=source_host, capture_output=True)
        if is_teleback: sync_worker_data_back(name, source_host)
        if not is_teleback:
            try:
                backend_obj = get_backend(backend_name); welcome = workers._build_welcome(name, backend_obj)  # type: ignore[attr-defined]
                _clock.sleep(bridge.DELAY_PROCESS_SETTLE)
                workers.send(name, welcome)  # type: ignore[attr-defined]
                if source_host != target_host:
                    ctx = _build_teleport_context(
                        name=name,
                        source_host=source_host,
                        target_host=target_host,
                        source_cwd=source_cwd,
                        session_id=session_id, )
                    _clock.sleep(0.5)
                    workers.send(name, ctx)  # type: ignore[attr-defined]
            except (ConnectionError, TimeoutError, AttributeError, OSError) as e: _log(_LOG_WARN, "teleport", f"Warning: failed to send welcome to {name}: {e}")
        state_file.unlink(missing_ok=True)
        dest_label = target_host or "local"; action = "teleported back" if is_teleback else "teleported"
        msg = f"{name} {action} to {dest_label}:{target_cwd}"
        if session_id: msg += f"\nSession resumed ({session_id[:8]}...)."
        if not is_teleback: msg += f"\nUse /teleback {name} to bring it back."
        teleport_notify(chat_id, msg)
    except (subprocess.SubprocessError, ConnectionError, TimeoutError, TypeError, AttributeError, OSError, ValueError, KeyError) as e:
        _log(_LOG_ERROR, "teleport", f"Teleport failed: {e}", exc=e)
        teleport_notify(chat_id, f"Teleport failed: {e}")
        try:
            state_file = bridge.SESSIONS_DIR / name / "teleport_state"
            state_file.unlink(missing_ok=True)
        except OSError as exc: _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
def cmd_teleport(router: object, arg: str, chat_id: "ChatId", check_only: bool = False) -> bool:
    import bridge
    if not arg:
        cmd_name = "/teleport-check" if check_only else "/teleport"
        router.reply(chat_id, f"Usage: {cmd_name} <worker> <host>[:/path]")  # type: ignore[attr-defined]
        return True
    parts = arg.split(); worker_name = parts[0].lower(); target_spec = " ".join(parts[1:]) if len(parts) > 1 else ""
    if not target_spec:
        router.reply(chat_id, "Usage: /teleport <worker> <host>[:/path] [--full]")  # type: ignore[attr-defined]
        return True
    full_sync = "--full" in target_spec; target_spec = target_spec.replace("--full", "").strip()
    if ":" in target_spec and not target_spec.startswith("/"): target_host, target_cwd = target_spec.split(":", 1)
    else: target_host = target_spec; target_cwd = ""
    machines = bridge.get_machine_catalog()
    if target_host in machines:
        machine = machines[target_host]
        if machine.ssh_target: target_host = machine.ssh_target
    registry = bridge._load_registry(); worker_entry = registry.get("workers", {}).get(worker_name)
    if not worker_entry:
        router.reply(chat_id, f"Worker '{worker_name}' not found in registry.")  # type: ignore[attr-defined]
        return True
    backend_name = worker_entry.get("backend", "claude")
    with watchdog.lock: worker_state = watchdog.worker_states.get(worker_name, ("UNKNOWN", "", 0))
    current_state = worker_state[0]
    if current_state in ("BUSY_TOOL", "BUSY_THINKING"):
        router.reply(chat_id,  # type: ignore[attr-defined]
            f"{worker_name} is busy. Must be idle to teleport.\n"
            f"Wait for it to finish or /pause {worker_name} first.")
        return True
    teleport_file = bridge.SESSIONS_DIR / worker_name / "teleport_state"
    if teleport_file.exists():
        router.reply(chat_id, f"{worker_name} has a teleport in progress.")  # type: ignore[attr-defined]
        return True
    r = _remote_run(["echo", "ok"], host=target_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
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
        host=target_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND)
    if r.returncode == 0 and "exists" in r.stdout:
        teleport_notify(chat_id,
            f"⚠️ tmux session '{tmux_name}' already exists on {target_host} — will be replaced.")
    target_bridge_url = bridge.BRIDGE_PUBLIC_URL or bridge.BRIDGE_URL
    if "localhost" in target_bridge_url or "127.0.0.1" in target_bridge_url:
        router.reply(chat_id,  # type: ignore[attr-defined]
            "Cannot teleport: no reachable bridge URL. "
            "Set BRIDGE_PUBLIC_URL to this machine's network IP "
            "(e.g., BRIDGE_PUBLIC_URL=http://100.125.36.102:8271).")
        return True
    r = _remote_run(["curl", "-sf", "--connect-timeout", "5",
                     f"{target_bridge_url}/"],
                    host=target_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
    if r.returncode != 0:
        router.reply(chat_id,  # type: ignore[attr-defined]
            f"Target {target_host} cannot reach {target_bridge_url}. "
            f"Ensure BRIDGE_BIND=0.0.0.0 and network connectivity.")
        return True
    r = _remote_run(["test", "-f", ".claude/.credentials.json"],
                    host=target_host, capture_output=True, timeout=bridge.TIMEOUT_TMUX_SEND)
    if r.returncode != 0:
        local_creds = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.exists(local_creds):
            _remote_run(["mkdir", "-p", ".claude"], host=target_host, capture_output=True)
            _subprocess_runner.run(
                ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json"],
                capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
            _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host, capture_output=True)
            teleport_notify(chat_id, "Synced credentials to target.")
        else:
            router.reply(chat_id,  # type: ignore[attr-defined]
                f"No Claude credentials on {target_host} or locally. "
                f"Run: ssh {target_host} claude login")
            return True
    r = _remote_run(["test", "-f", ".claude/hooks/claudecode.sh"],
                    host=target_host, capture_output=True, timeout=bridge.TIMEOUT_TMUX_SEND)
    if r.returncode != 0: teleport_notify(chat_id, "Hooks missing on target — will install during teleport.")
    preflight_fails = run_teleport_preflight( target_host, worker_name, backend_name)
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
        daemon=True
    ).start()
    return True
def cmd_teleback(router: object, arg: str, chat_id: "ChatId") -> bool:
    import bridge
    parts = arg.split(); worker_name = parts[0].lower() if parts else ""; full_sync = "--full" in parts
    if not worker_name:
        router.reply(chat_id, "Usage: /teleback <worker> [--full]")  # type: ignore[attr-defined]
        return True
    registry = bridge._load_registry(); worker = registry.get("workers", {}).get(worker_name)
    if not worker:
        router.reply(chat_id, f"Worker '{worker_name}' not in registry.")  # type: ignore[attr-defined]
        return True
    current_host = worker.get("host"); home_host = worker.get("home_host"); home_cwd = worker.get("home_cwd")
    if current_host is None and home_cwd is None:
        router.reply(chat_id, f"{worker_name} hasn't been teleported.")  # type: ignore[attr-defined]
        return True
    with watchdog.lock: worker_state = watchdog.worker_states.get(worker_name, ("UNKNOWN", "", 0))
    if worker_state[0] in ("BUSY_TOOL", "BUSY_THINKING"):
        router.reply(chat_id,  # type: ignore[attr-defined]
            f"{worker_name} is busy. Must be idle to teleback. "
            f"Wait or /pause {worker_name} first.")
        return True
    teleport_file = bridge.SESSIONS_DIR / worker_name / "teleport_state"
    if teleport_file.exists():
        router.reply(chat_id, f"{worker_name} has a teleport in progress.")  # type: ignore[attr-defined]
        return True
    target_host = home_host; target_cwd = home_cwd or get_claude_session_cwd(worker_name)
    if target_host:
        r = _remote_run(["echo", "ok"], host=target_host,
                        capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            router.reply(chat_id, f"Cannot reach home host {target_host} via SSH.")  # type: ignore[attr-defined]
            return True
        for tool in ("claude", "tmux", "rsync"):
            tool_path = _resolve_remote_tool(tool, target_host)
            if tool_path == tool:
                router.reply(chat_id, f"{tool} not found on {target_host}. Install it first.")  # type: ignore[attr-defined]
                return True
    if current_host:
        r = _remote_run(["echo", "ok"], host=current_host,
                        capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
        if r.returncode != 0:
            router.reply(chat_id, f"Cannot reach {current_host} where {worker_name} currently is.")  # type: ignore[attr-defined]
            return True
    if not full_sync:
        conflicts = check_teleback_conflicts( worker_name, current_host, target_cwd)
        if conflicts:
            router.reply(chat_id,  # type: ignore[attr-defined]
                f"Teleback conflict detected for {worker_name}:\n"
                + "\n".join(f"  {c}" for c in conflicts)
                + "\n\nUse /teleback " + worker_name + " --full to force sync "
                "(remote overwrites local).")
            return True
    dest_label = home_host or "local"
    router.reply(chat_id, f"Bringing {worker_name} back to {dest_label}...")  # type: ignore[attr-defined]
    threading.Thread(
        target=do_teleport,
        args=(worker_name, target_host, target_cwd, full_sync, chat_id, router.workers, True),  # type: ignore[attr-defined]
        name=f"teleback-{worker_name}",
        daemon=True
    ).start()
    return True
def restart_remote_worker(name: str, backend_name: str, backend: "Backend", tmux_name: str,
                          host: str, mode: str, workers: object) -> tuple[bool, str | None]:
    import bridge
    resume_id = ""; target_cwd = get_claude_session_cwd(name)
    if target_cwd and host: target_cwd = _remap_path(target_cwd, host)
    _log(_LOG_INFO, "_restart_remote", f"{name}: mode={mode}, host={host}, tmux={tmux_name}, cwd={target_cwd}")
    if mode == "resume":
        resume_id = get_claude_session_id(name, authoritative=False)
        if not resume_id: resume_id = get_claude_session_id(name, authoritative=True)
        _log(_LOG_INFO, "_restart_remote", f"{name}: resume_id={resume_id}")
    else:
        session_dir = bridge.SESSIONS_DIR / name
        session_dir.mkdir(parents=True, exist_ok=True)
        cleared = list(session_dir.glob("*_session_id"))
        for f in cleared: f.unlink()
        bridge._clear_hook_failures(name)
        _log(_LOG_INFO, "_restart_remote", f"{name}: cleared {len(cleared)} session files for relaunch")
    if tmux_exists(tmux_name, host=host):
        _log(_LOG_INFO, "_restart_remote", f"{name}: stopping remote tmux {tmux_name}")
        stop_worker_for_teleport(name, tmux_name, host=host)
        _remote_run(["tmux", "kill-session", "-t", tmux_name], host=host, capture_output=True)
        _clock.sleep(bridge.DELAY_RETRY)
    else: _log(_LOG_INFO, "_restart_remote", f"{name}: tmux {tmux_name} not found on {host}")
    if mode == "resume":
        cached = get_claude_session_id(name, authoritative=False)
        if cached: resume_id = cached
        else: resume_id = get_claude_session_id(name, authoritative=True) or resume_id
        _log(_LOG_INFO, "_restart_remote", f"{name}: post-stop resume_id={resume_id}")
    if resume_id and target_cwd and host:
        remote_home = _get_remote_home(host)
        if remote_home:
            cwd_slug = target_cwd.replace("/", "-")
            session_file = f"{remote_home}/.claude/projects/{cwd_slug}/{resume_id}.jsonl"
            check = _remote_run(["test", "-f", session_file], host=host,
                                 capture_output=True, timeout=bridge.TIMEOUT_TMUX_SEND)
            if check.returncode != 0:
                _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} NOT found at {session_file}, starting fresh")
                resume_id = ""; session_dir = bridge.SESSIONS_DIR / name
                session_dir.mkdir(parents=True, exist_ok=True)
                for f in session_dir.glob("*_session_id"): f.unlink()
            else: _log(_LOG_INFO, "_restart_remote", f"{name}: session {resume_id} validated at {session_file}")
    _ensure_workspace_trusted_remote(target_cwd, host)
    _log(_LOG_INFO, "_restart_remote", f"{name}: calling _start_worker_on_target(cwd={target_cwd}, resume={resume_id}, backend={backend_name})")
    ok = start_worker_on_target( name, host, target_cwd, resume_id, backend_name, skip_session_sync=True)
    if not ok:
        _log(_LOG_WARN, "_restart_remote", f"{name}: _start_worker_on_target FAILED")
        return False, f"Failed to restart {name} on {host}"
    welcome = workers._build_welcome(name, backend)  # type: ignore[attr-defined]
    if backend.is_interactive:
        started = False
        for _ in range(10):
            _clock.sleep(bridge.DELAY_STARTUP)
            if is_claude_running(tmux_name, host=host):
                started = True
                break
        if started: workers.send(name, welcome)  # type: ignore[attr-defined]
        else: _log(_LOG_WARN, "_restart_remote", f"{name}: Claude did not start within 10s, skipping welcome")
    _log(_LOG_INFO, "_restart_remote", f"{name}: restarted successfully (mode={mode})")
    return True, None
# Propagate test patches to _teleport_ops (same pattern as bridge._BridgeModule)
import types as _types_mod
class _TeleportModule(_types_mod.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_tpo_mod, name): setattr(_tpo_mod, name, value)
import sys as _sys; _sys.modules[__name__].__class__ = _TeleportModule