"""WorkerManager — extracted from bridge.py for complexity reduction."""
from __future__ import annotations
import json
import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

import core
import claudecode
import health
from core import (
    Clock, SubprocessRunner,
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO,
    BRIDGE_URL, BRIDGE_SSH_TARGET, DEFAULT_BACKEND,
    TIMEOUT_TMUX_CHECK, TIMEOUT_TMUX_SEND, TIMEOUT_REMOTE_CMD,
    DELAY_SHORT, DELAY_TMUX_SEND, DELAY_STARTUP, DELAY_STARTUP_LONG,
    DELAY_RETRY, DELAY_RESPONSE_GAP,
)
from claudecode import (
    normalize_backend, is_valid_backend, list_backends,
    _which_binary, normalize_cwd, get_tmux_env_value,
    cleanup_worker_pipe, clear_claude_session_id,
)

if TYPE_CHECKING:
    from bridge import WorkerEndpointInfo
    from claudecode import Backend, TmuxSessionDict
    from telegram import ChatId


class WorkerManager:
    def __init__(self, sessions_dir: Path, tmux_prefix: str,
                 runner: "SubprocessRunner | None" = None,
                 clock: "Clock | None" = None) -> None:
        self.sessions_dir = sessions_dir; self.tmux_prefix = tmux_prefix
        self._runner: SubprocessRunner = runner or core._subprocess_runner
        self._clock: Clock = clock or core._clock

    def _sync_paths(self) -> None:
        if self.sessions_dir != core.SESSIONS_DIR: self.sessions_dir = core.SESSIONS_DIR
        if self.tmux_prefix != core.TMUX_PREFIX: self.tmux_prefix = core.TMUX_PREFIX

    def _get_startup_cwd(self, name: str, requested_cwd: str = "", fallback_cwd: str = "") -> str:
        from bridge import _get_worker_cwd
        candidate = normalize_cwd(requested_cwd)
        if not candidate: candidate = normalize_cwd(_get_worker_cwd(name))
        if candidate:
            if os.path.isdir(candidate): return candidate
            _log(_LOG_WARN, "bridge", f"Ignoring invalid startup cwd for {name}: {candidate}")
        fallback = normalize_cwd(fallback_cwd)
        if fallback and os.path.isdir(fallback): return fallback
        return ""

    def _get_tmux_pane_cwd(self, tmux_name: str, host: str | None = None) -> str:
        result = claudecode._remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_current_path}"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK)
        if result.returncode == 0: return result.stdout.strip()
        return ""

    def _cd_tmux_to_cwd(self, tmux_name: str, cwd: str) -> None:
        if not cwd: return
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"cd {shlex.quote(cwd)}", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_SHORT)

    def scan_tmux_sessions(self) -> dict[str, TmuxSessionDict]:
        from bridge import _registry_add, get_machine_catalog
        self._sync_paths()
        registered: dict[str, TmuxSessionDict] = {}
        try:
            result = self._runner.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK)
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line: continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix):]
                        backend = normalize_backend(get_tmux_env_value(session_name, "WORKER_BACKEND"))
                        registered[name] = {"tmux": session_name, "backend": backend}
        except (subprocess.SubprocessError, KeyError) as e: _log(_LOG_ERROR, "bridge", f"Error scanning local tmux: {e}")
        try: machines = get_machine_catalog()
        except (subprocess.SubprocessError, OSError): machines = {}
        for machine in machines.values():
            if machine.is_local or not machine.ssh_target: continue
            if machine.role not in ("worker-host", ""): continue
            try:
                r = claudecode._remote_run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    host=machine.ssh_target,
                    capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
                if r.returncode != 0: continue
                for line in r.stdout.strip().split("\n"):
                    if not line: continue
                    session_name = line.strip()
                    if session_name.startswith(self.tmux_prefix):
                        name = session_name[len(self.tmux_prefix):]
                        if name not in registered:
                            registered[name] = {
                                "tmux": session_name,
                                "backend": DEFAULT_BACKEND,
                                "host": machine.ssh_target, }
                            _registry_add(name, DEFAULT_BACKEND, host=machine.ssh_target)
            except (subprocess.SubprocessError, KeyError) as e:
                _log(_LOG_ERROR, "bridge", f"Error scanning tmux on {machine.ssh_target}: {e}")
        return registered

    _sessions_cache = None
    _sessions_cache_ts: float = 0
    _sessions_cache_lock = threading.Lock(); _SESSIONS_CACHE_TTL = 15

    def invalidate_sessions_cache(self) -> None:
        with self._sessions_cache_lock: self._sessions_cache = None; self._sessions_cache_ts = 0

    def get_registered_sessions(self, registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
        from bridge import _load_registry, _registry_bootstrap, state
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
                    if val: entry[key] = str(val)
                if info.get("host") and not info.get("callback_url"): entry["tmux"] = f"{self.tmux_prefix}{name}"
                registered[name] = entry
            else:
                for key in ("protocol", "callback_url", "host", "version"):
                    if info.get(key): registered[name][key] = info.get(key)  # type: ignore[literal-required]
        if state.active and state.active not in registered: state.active = None
        if registered and not state.active: state.active = list(registered.keys())[0]
        with self._sessions_cache_lock:
            self._sessions_cache = dict(registered); self._sessions_cache_ts = self._clock.time()
        return registered

    def is_online(self, name: str, session: TmuxSessionDict | None = None) -> bool:
        self._sync_paths()
        if not session: sessions = self.get_registered_sessions(); session = sessions.get(name)
        if not session: return False
        if session.get("callback_url"): return True
        backend_name = normalize_backend(session.get("backend")); backend = claudecode.get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}"); host = health.get_worker_host(name)
        if host:
            try:
                if not claudecode.tmux_exists(tmux_name, host=host, timeout=TIMEOUT_TMUX_CHECK): return False
                if backend.is_interactive:
                    try: return claudecode.is_claude_running(tmux_name, host=host)
                    except (subprocess.SubprocessError, OSError): return True
                return True
            except (subprocess.SubprocessError, OSError): return True
        return backend.is_online(tmux_name)

    def send(self, name: str, message: str, chat_id: ChatId | None = None, session: TmuxSessionDict | None = None) -> bool:
        from bridge import _send_to_callback_worker
        self._sync_paths()
        if not session: sessions = self.get_registered_sessions(); session = sessions.get(name)
        if not session: return False
        if session.get("callback_url"): return _send_to_callback_worker(name, message, "manager", session)
        backend_name = normalize_backend(session.get("backend")); backend = claudecode.get_backend(backend_name)
        tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        return backend.send(name, tmux_name, message, BRIDGE_URL, self.sessions_dir)

    def _pipe_worker_entry(self, name: str, peer_host: str | None, caller_host: str | None) -> WorkerEndpointInfo:
        pipe_path = claudecode.ensure_worker_pipe(name)
        pipe_cmd = f"echo 'YOUR_NAME: your message here' > {pipe_path} &"
        return cast("WorkerEndpointInfo", {
            "name": name, "machine": peer_host or BRIDGE_SSH_TARGET, "protocol": "pipe",
            "address": str(pipe_path), "send_example": self._wrap_for_caller(pipe_cmd, peer_host, caller_host),
            "note": "Non-interactive. IMPORTANT: Always prefix your name (e.g., 'kenji: hello'). Always use & (background) when writing to pipe — it BLOCKS until read. Never use cat/echo without & or your session will freeze.",
        })

    def get_workers(self, caller_from: str | None = None) -> list[WorkerEndpointInfo]:
        from bridge import get_machine_catalog
        self._sync_paths()
        workers: list[WorkerEndpointInfo] = []
        registered = self.get_registered_sessions(); caller_host = health.get_worker_host(caller_from) if caller_from else None
        for name, info in registered.items():
            callback_url = info.get("callback_url", "")
            if callback_url:
                msg_url = callback_url.rstrip("/")
                if not msg_url.endswith("/msg"): msg_url = f"{msg_url}/msg"
                payload = json.dumps({"from": "YOUR_NAME", "text": "your message here"})
                workers.append(cast("WorkerEndpointInfo", {"name": name, "machine": info.get("host", "") or BRIDGE_SSH_TARGET,
                    "protocol": "http", "address": msg_url,
                    "send_example": f"curl -sS -X POST {shlex.quote(msg_url)} -H 'Content-Type: application/json' --data-raw {shlex.quote(payload)}",
                    "note": "HTTP callback worker. POST JSON with from/text. Always set from to your worker name."}))
                continue
            backend_name = claudecode.get_worker_backend(name, info); backend = claudecode.get_backend(backend_name)
            peer_host = health.get_worker_host(name)
            if "tmux" not in info:
                if not backend.is_interactive: workers.append(self._pipe_worker_entry(name, peer_host, caller_host))
                else:
                    workers.append(cast("WorkerEndpointInfo", {"name": name, "machine": peer_host or BRIDGE_SSH_TARGET,
                        "protocol": "none", "address": "", "status": "exited", "note": f"Worker exited. Use /restart {name} to bring back."}))
                continue
            if not backend.is_interactive:
                if peer_host:
                    workers.append(cast("WorkerEndpointInfo", {"name": name, "machine": peer_host, "protocol": "adapter",
                        "address": f"{peer_host}:{info.get('tmux', '')}",
                        "note": f"Non-interactive ({backend_name}) on {peer_host}. Use @{name} from Telegram or bridge API."}))
                else: workers.append(self._pipe_worker_entry(name, peer_host, caller_host))
            else:
                tmux_name = info.get("tmux")
                tmux_cmd = (
                    f"echo 'YOUR_NAME: your message here' | "
                    f"tmux load-buffer - && "
                    f"tmux paste-buffer -p -r -t {tmux_name} && "
                    f"sleep 1 && tmux send-keys -t {tmux_name} Enter")
                send_example = self._wrap_for_caller(tmux_cmd, peer_host, caller_host)
                if peer_host and caller_host == peer_host:
                    note = f"On {peer_host} (same machine as caller). Uses paste-buffer -p. Always prefix your name."
                elif peer_host: note = f"On {peer_host}. Uses SSH + paste-buffer -p (bracketed paste). Always prefix your name."
                elif caller_host: note = f"On bridge host (cross-machine from caller). Uses SSH + paste-buffer -p. Always prefix your name."
                else:
                    note = "Uses paste-buffer -p (bracketed paste) for reliable delivery. Sleep 1s before Enter — TUI needs time to render. Always prefix your name."
                workers.append(cast("WorkerEndpointInfo", {"name": name, "machine": peer_host or BRIDGE_SSH_TARGET, "protocol": "tmux",
                    "address": f"{peer_host}:{tmux_name}" if peer_host else tmux_name, "send_example": send_example, "note": note}))
        return workers

    def _wrap_for_caller(self, cmd: str, peer_host: str | None, caller_host: str | None) -> str:
        if caller_host == peer_host: return cmd
        ssh_target = peer_host if peer_host is not None else BRIDGE_SSH_TARGET
        escaped = cmd.replace('"', '\\"')
        return f'ssh {ssh_target} "{escaped}"'

    def _build_welcome(self, name: str, backend_obj: Backend) -> str:
        from bridge import read_checkin_note
        welcome = (
            "You are connected to Telegram via claudecode-telegram bridge. "
            "RECEIVING FILES: Manager sends files (images, PDFs, documents) — they appear as local paths you can read directly. "
            "SENDING FILES: Use [[image:/path/to/photo.png|caption]] for images (jpg/png/webp/bmp) and animations (gif/mp4), or [[file:/path/to/file|caption]] for documents, video (mp4/mov/avi — shows player), audio (mp3/m4a/flac — shows player), and voice (ogg/opus — voice bubble). "
            f"MESSAGING WORKERS: Run `curl -s \"$BRIDGE_URL/workers?from={name}\"` to discover other workers — returns JSON with a `send_example` field containing ready-to-use send commands wrapped correctly for your machine (auto-adds ssh when a peer lives elsewhere). Always call /workers?from={name} before messaging, never guess addresses. Never use POST /outputs to message another worker; /outputs only publishes your own worker output to Telegram. "
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
                "Use nohup/& if calling CLI directly.")
        note = read_checkin_note()
        if note:
            rendered = note.replace("{name}", name); host = health.get_worker_host(name)
            if host: machine = f"Mac Mini ({host})"
            else: machine = "VPS (100.125.36.102)"
            rendered = rendered.replace("{machine}", machine); welcome += f"\n\nMANAGER NOTE:\n{rendered}"
            _log(_LOG_INFO, "checkin", f"Checkin note included for {name}")
        return welcome

    def hire(self, name: str, backend: str = "", chat_id: ChatId | None = None) -> tuple[bool, str | None]:
        from bridge import set_pending, state, _registry_add, _reset_learning_reminder
        if not backend: backend = DEFAULT_BACKEND
        self._sync_paths()
        if not is_valid_backend(backend): return False, f"Unknown backend '{backend}'. Available: {', '.join(list_backends())}"
        backend_obj = claudecode.get_backend(backend)
        if not _which_binary(backend_obj.binary): return False, f"'{backend_obj.binary}' not found in PATH. Install it first."
        tmux_name = f"{self.tmux_prefix}{name}"
        if claudecode.tmux_exists(tmux_name): return False, f"Worker '{name}' already exists"
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env, timeout=TIMEOUT_REMOTE_CMD)
        if result.returncode != 0: return False, "Could not start the worker workspace"
        self._runner.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        startup_cwd = self._get_startup_cwd(name)
        if startup_cwd: self._cd_tmux_to_cwd(tmux_name, startup_cwd)
        claudecode.export_hook_env(tmux_name, backend)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)
        claudecode.ensure_session_dir(name)
        if chat_id:
            chat_id_file = claudecode.get_chat_id_file(name); _tmp = chat_id_file.with_suffix('.tmp')
            _tmp.write_text(str(chat_id))
            _tmp.chmod(0o600)
            os.replace(str(_tmp), str(chat_id_file))
        if not backend_obj.is_interactive: claudecode.ensure_worker_pipe(name)
        start_cmd = f'unset CLAUDECODE && {backend_obj.start_cmd()}'
        if startup_cwd: start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        if backend_obj.is_interactive:
            self._clock.sleep(DELAY_STARTUP_LONG)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], timeout=TIMEOUT_TMUX_SEND)
        if backend_obj.is_interactive: self._clock.sleep(DELAY_RESPONSE_GAP)
        welcome = self._build_welcome(name, backend_obj)
        if not backend_obj.is_interactive:
            if chat_id: set_pending(name, chat_id)
            self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        else: self.send(name, welcome)
        state.active = name
        import telegram as _tg
        _tg.save_last_active(name)
        _registry_add(name, backend, chat_id)
        _reset_learning_reminder(name)
        if not backend_obj.is_interactive: _log(_LOG_INFO, "worker", f"Created {backend} worker '{name}' (non-interactive mode)")
        self.invalidate_sessions_cache()
        return True, None

    def end(self, name: str) -> tuple[bool, str | None]:
        from bridge import kill_adapter, clear_pending, _set_worker_cwd, cleanup_inbox, _registry_remove, state
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered: return False, f"Worker '{name}' not found"
        session = registered[name]; backend_name = claudecode.get_worker_backend(name, session)
        backend = claudecode.get_backend(backend_name); tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        if not backend.is_interactive:
            kill_adapter(name)
            session_dir = self.sessions_dir / name
            try:
                for session_id_file in session_dir.glob("*_session_id"): session_id_file.unlink()
            except OSError as e: return False, f"Failed to clean non-interactive metadata: {e}"
        clear_pending(name)
        _set_worker_cwd(name, "")
        host = health.get_worker_host(name)
        claudecode._remote_run(["tmux", "kill-session", "-t", tmux_name], host=host, capture_output=True)
        cleanup_inbox(name)
        cleanup_worker_pipe(name)
        _registry_remove(name)
        self.invalidate_sessions_cache()
        if state.active == name:
            state.active = None
            self.get_registered_sessions()
        return True, None

    def restart(self, name: str, mode: str = "relaunch") -> tuple[bool, str | None]:
        from bridge import clear_pending, _reset_learning_reminder
        self._sync_paths()
        registered = self.get_registered_sessions()
        if name not in registered: return False, f"Worker '{name}' not found"
        host = health.get_worker_host(name)
        if host: return False, "use_remote_restart"
        session = registered[name]; backend_name = claudecode.get_worker_backend(name, session)
        backend = claudecode.get_backend(backend_name); tmux_name = session.get("tmux", f"{self.tmux_prefix}{name}")
        if not claudecode.tmux_exists(tmux_name): return self._restart_dead_worker(name, backend_name, backend, tmux_name, mode)
        if not _which_binary(backend.binary): return False, f"'{backend.binary}' not found in PATH. Install it first."
        resume_id, startup_cwd = self._prepare_restart_state(name, mode)
        if mode != "resume": health._clear_hook_failures(name)
        session_dir = self.sessions_dir / name
        if not backend.is_interactive:
            session_dir.mkdir(parents=True, exist_ok=True)
            claudecode.ensure_worker_pipe(name)
            clear_pending(name)
        elif claudecode.is_claude_running(tmux_name): self._stop_running_claude(name, tmux_name)
        if backend.is_interactive and not claudecode.is_claude_running(tmux_name): self._kill_stray_children(name, tmux_name)
        claudecode.export_hook_env(tmux_name, backend_name)
        self._clock.sleep(DELAY_TMUX_SEND)
        self._send_start_command(name, tmux_name, backend, resume_id, startup_cwd)
        welcome = self._build_welcome(name, backend)
        if backend.is_interactive:
            started = self._wait_for_startup(name, tmux_name, backend, resume_id, startup_cwd)
            if started: self.send(name, welcome)
            else: _log(_LOG_WARN, "restart", f"{name}: Claude did not start within 10s, skipping welcome")
        else: self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        _reset_learning_reminder(name)
        self.invalidate_sessions_cache()
        return True, None

    def _prepare_restart_state(self, name: str, mode: str) -> tuple[str, str]:
        resume_id = ""; resume_cwd = ""; session_dir = self.sessions_dir / name
        if mode == "resume":
            resume_id = (claudecode.get_claude_session_id(name, authoritative=False) or
                         claudecode.get_claude_session_id(name, authoritative=True) or "")
            resume_cwd = ""
        else:
            session_dir.mkdir(parents=True, exist_ok=True)
            for session_id_file in session_dir.glob("*_session_id"): session_id_file.unlink()
        startup_cwd = self._get_startup_cwd(name, fallback_cwd=resume_cwd)
        if startup_cwd: claudecode._ensure_workspace_trusted(startup_cwd)
        return resume_id, startup_cwd

    def _stop_running_claude(self, name: str, tmux_name: str) -> None:
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_STARTUP)
        if claudecode.is_claude_running(tmux_name):
            pane_pid = claudecode._tmux_pane_pids().get(tmux_name)
            if pane_pid:
                claude_pid = health._get_claude_pid(pane_pid)
                if claude_pid: self._runner.run(["kill", claude_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        for _ in range(20):
            if not claudecode.is_claude_running(tmux_name): break
            self._clock.sleep(DELAY_SHORT)
        else: _log(_LOG_WARN, "restart", f"{name}: Claude still running after 5s kill wait")

    def _kill_stray_children(self, name: str, tmux_name: str) -> None:
        pane_pids = claudecode._tmux_pane_pids(); pane_pid = pane_pids.get(tmux_name)
        if pane_pid:
            stray = self._runner.run(
                ["pgrep", "-P", str(pane_pid)],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if stray.returncode == 0:
                for child_pid in stray.stdout.strip().splitlines():
                    child_pid = child_pid.strip()
                    if child_pid and child_pid.isdigit():
                        _log(_LOG_INFO, "restart", f"{name}: killing stray child pid {child_pid}")
                        self._runner.run(["kill", child_pid], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
                self._clock.sleep(DELAY_RETRY)

    def _send_start_command(self, name: str, tmux_name: str, backend: Backend,
                            resume_id: str, startup_cwd: str) -> None:
        self._runner.run(["tmux", "send-keys", "-t", tmux_name,
                        'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"], timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_TMUX_SEND)
        start_cmd = backend.start_cmd(resume_id); start_cmd = f'unset CLAUDECODE && {start_cmd}'
        if startup_cwd: start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
        self._runner.run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], timeout=TIMEOUT_TMUX_SEND)

    def _wait_for_startup(self, name: str, tmux_name: str, backend: Backend,
                          resume_id: str, startup_cwd: str) -> bool:
        started = False
        for _ in range(10):
            self._clock.sleep(DELAY_STARTUP)
            if claudecode.is_claude_running(tmux_name):
                started = True
                break
        if not started and resume_id: started = self._retry_after_stale_resume(name, tmux_name, backend, resume_id, startup_cwd)
        return started

    def _retry_after_stale_resume(self, name: str, tmux_name: str, backend: Backend,
                                  resume_id: str, startup_cwd: str) -> bool:
        from bridge import _notify_admin
        _log(_LOG_WARN, "restart", f"{name}: resume failed (stale session {resume_id[:8]}), auto-retrying fresh")
        clear_claude_session_id(name)
        health._clear_hook_failures(name)
        start_cmd = backend.start_cmd(""); start_cmd = f'unset CLAUDECODE && {start_cmd}'
        if startup_cwd: start_cmd = f'cd {shlex.quote(startup_cwd)} && {start_cmd}'
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
            _notify_admin(f"🔴 {name}: resume failed (stale {resume_id[:8]}…) AND fresh start failed.\n/restart --clean {name} to try manually.")
        return started

    def _restart_dead_worker(self, name: str, backend_name: str, backend: Backend, tmux_name: str, mode: str) -> tuple[bool, str | None]:
        from bridge import _reset_learning_reminder
        if not _which_binary(backend.binary): return False, f"'{backend.binary}' not found in PATH. Install it first."
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = self._runner.run(["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
            capture_output=True, env=clean_env, timeout=TIMEOUT_REMOTE_CMD)
        if result.returncode != 0: return False, "Could not create worker workspace"
        self._runner.run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        self._clock.sleep(DELAY_RETRY)
        claudecode.ensure_session_dir(name)
        if not backend.is_interactive: claudecode.ensure_worker_pipe(name)
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
            if started: self.send(name, welcome)
            else: _log(_LOG_WARN, "restart", f"{name}: dead worker did not start within 10s, skipping welcome")
        else: self._runner.run(["tmux", "send-keys", "-t", tmux_name, f"echo '{welcome[:200]}...'", "Enter"], timeout=TIMEOUT_TMUX_SEND)
        _log(_LOG_INFO, "worker", f"Dead worker '{name}' recovered from registry (mode={mode})")
        self.invalidate_sessions_cache()
        return True, None
