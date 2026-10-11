"""Claudecode implementation — codex backend, pipes, sessions."""
from __future__ import annotations
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast
import shlex
import tempfile
import urllib.error
import urllib.request
from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_INFO, _LOG_DEBUG,
    _clock, _subprocess_runner, _urlopen,
    _str_field, _dict_field,
    NODE_DIR, SESSIONS_DIR, TMUX_PREFIX, PORT, _CLAUDE_JSON_PATH,
    DEFAULT_WORKER_BACKEND, TIMEOUT_TMUX_SEND, TIMEOUT_REMOTE_CMD,
    BRIDGE_URL, BRIDGE_PUBLIC_URL, DEFAULT_BACKEND,
    TIMEOUT_THREAD_JOIN, TIMEOUT_TMUX_CHECK,
    WORKER_PIPE_ROOT, DELAY_BRIEF, CLAUDE_PROJECTS_DIR,
)
from claudecode import (
    Backend, ProcessRegistry, processes, _resolve_remote_tool,
    _remote_run, _detect_os_family, _project_slug, _which_binary,
    _is_git_repo, build_claude_start_cmd,
)
def _codex_session_id_path(worker_name: str, sessions_dir: Path) -> Path:
    return sessions_dir / worker_name / "codex_session_id"
def _codex_load_session_id(worker_name: str, sessions_dir: Path) -> str:
    p = _codex_session_id_path(worker_name, sessions_dir)
    if p.exists(): return p.read_text(encoding="utf-8").strip()
    return ""
def _codex_save_session_id(worker_name: str, sessions_dir: Path, session_id: str) -> None:
    target = _codex_session_id_path(worker_name, sessions_dir); target.parent.mkdir(parents=True, exist_ok=True); fd: int = -1
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".session_id_"); os.write(fd, session_id.encode("utf-8"))
        os.close(fd); fd = -1; os.replace(tmp_path, str(target))
    except BaseException:
        if fd >= 0: os.close(fd)
        raise
def _codex_parse_jsonl(output: str) -> tuple[str, str]:
    response_parts: list[str] = []; thread_id: str = ""
    for line in output.strip().split("\n"):
        if not line.strip(): continue
        try: event: dict[str, object] = json.loads(line)
        except json.JSONDecodeError: continue
        event_type = _str_field(event, "type")
        if event_type == "thread.started": thread_id = _str_field(event, "thread_id")
        elif event_type == "item.completed":
            item = _dict_field(event, "item")
            if _str_field(item, "type") == "agent_message":
                text = _str_field(item, "text")
                if text: response_parts.append(text)
    return "\n".join(response_parts).strip(), thread_id
def _codex_run(message: str, session_id: str = "", workdir: str = "") -> tuple[str, str, int]:
    cmd: list[str] = ["codex", "exec", "--json", "--yolo"]
    if workdir: cmd.extend(["-C", workdir])
    if session_id and not session_id.startswith("-"): cmd.extend(["resume", session_id, "-"])
    else: cmd.append("-")
    try:
        result = subprocess.run(cmd, input=message, capture_output=True, text=True)
        response, new_session_id = _codex_parse_jsonl(result.stdout)
        if result.returncode != 0 and not response: stderr = (result.stderr or "").strip(); response = stderr or "Codex exec failed."
        return response, new_session_id or session_id, result.returncode
    except (OSError, subprocess.SubprocessError) as e: return f"Error: {e}", session_id, 1
def _codex_send_to_bridge(session_name: str, text: str, bridge_url: str) -> bool:
    try:
        data = json.dumps({"session": session_name, "text": text, "source": session_name, "backend": "codex", "escape": True}).encode()
        req = urllib.request.Request(f"{bridge_url}/outputs", data=data, headers={"Content-Type": "application/json"})
        with _urlopen(req, timeout=5) as r: return r.status == 200
    except (urllib.error.URLError, OSError) as e: _log(_LOG_WARN, "codex", f"Failed to send to bridge: {e}"); return False
def _codex_adapter_thread(worker_name: str, text: str,
                          bridge_url: str, sessions_dir: Path) -> None:
    try:
        session_id = _codex_load_session_id(worker_name, sessions_dir); response, new_session_id, _rc = _codex_run(text, session_id)
        if new_session_id: _codex_save_session_id(worker_name, sessions_dir, new_session_id)
        if response: _codex_send_to_bridge(worker_name, response, bridge_url)
    except Exception as e: _log(_LOG_ERROR, "codex", f"Adapter thread for '{worker_name}' failed: {e}")
def _codex_adapter_remote(worker_name: str, text: str,
                          bridge_url: str, sessions_dir: Path, host: str) -> None:
    try:
        import bridge
        remote_sessions = bridge._remap_sessions_dir(host); sid_file = f"{remote_sessions}/{worker_name}/codex_session_id"; session_id = ""
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", host, "cat", sid_file],
                capture_output=True, text=True, timeout=10, )
            if r.returncode == 0: session_id = r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired): pass
        cmd_parts = ["codex", "exec", "--json", "--yolo"]
        if session_id and not session_id.startswith("-"): cmd_parts.extend(["resume", session_id, "-"])
        else: cmd_parts.append("-")
        ssh_cmd = ["ssh", "-o", "ConnectTimeout=5", host] + cmd_parts
        result = subprocess.run(ssh_cmd, input=text, capture_output=True, text=True)
        response, new_session_id = _codex_parse_jsonl(result.stdout)
        if result.returncode != 0 and not response: response = (result.stderr or "").strip() or "Codex exec failed."
        if new_session_id:
            try:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", host,
                     "mkdir", "-p", f"{remote_sessions}/{worker_name}",
                     "&&", "echo", shlex.quote(new_session_id), ">", sid_file],
                    timeout=10, capture_output=True, )
            except (OSError, subprocess.TimeoutExpired): pass
        if response: target_url = BRIDGE_PUBLIC_URL or bridge_url; _codex_send_to_bridge(worker_name, response, target_url)
    except Exception as e: _log(_LOG_ERROR, "codex", f"Remote adapter for '{worker_name}' on {host} failed: {e}")
class CodexBackend:
    name = "codex"; binary = "codex"; is_interactive = False; is_exec = True
    def start_cmd(self, resume_id: str = "") -> str:
        return "echo 'Codex worker ready (non-interactive)'"
    def send(self, worker_name: str, tmux_name: str, text: str,
             bridge_url: str, sessions_dir: Path) -> bool:
        import bridge
        host = bridge.get_worker_host(worker_name)
        if host: threading.Thread(target=_codex_adapter_remote, args=(worker_name, text, bridge_url, sessions_dir, host), daemon=True).start()
        else: threading.Thread(target=_codex_adapter_thread, args=(worker_name, text, bridge_url, sessions_dir), daemon=True).start()
        return True
    def is_online(self, tmux_name: str) -> bool:
        import claudecode as _cc; return _cc.tmux_exists(tmux_name)
def _find_codex_transcript(worker_name: str, host: str | None = None) -> str | None:
    sid_file = SESSIONS_DIR / worker_name / "codex_session_id"
    if not sid_file.exists(): return None
    session_id = sid_file.read_text().strip()
    if not session_id: return None
    home = os.path.expanduser("~")
    if host: home = _get_remote_home(host) or home
    codex_dir = os.path.join(home, ".codex", "sessions")
    if host:
        try:
            r = _remote_run(
                ["find", codex_dir, "-name", f"*{session_id}*", "-name", "*.jsonl"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode == 0 and r.stdout.strip(): return r.stdout.strip().split("\n")[0]
        except (subprocess.SubprocessError, OSError) as exc: _log(_LOG_DEBUG, "probe:_find_codex_transcript", f"{type(exc).__name__}: {exc}")
        return None
    import glob
    matches = glob.glob(os.path.join(codex_dir, "**", f"*{session_id}*"), recursive=True)
    jsonl_matches = [m for m in matches if m.endswith(".jsonl")]
    if jsonl_matches: return max(jsonl_matches, key=os.path.getmtime)
    return None
def get_worker_pipe_path(name: str) -> Path:
    return WORKER_PIPE_ROOT / name / "in.pipe"
def ensure_worker_pipe(name: str) -> Path:
    pipe_path = get_worker_pipe_path(name); pipe_dir = pipe_path.parent; pipe_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pipe_dir.chmod(0o700)
    if not pipe_path.exists(): os.mkfifo(str(pipe_path), mode=0o600); _log(_LOG_INFO, "pipe", f"Created worker pipe: {pipe_path}")
    start_pipe_reader(name); return pipe_path
def cleanup_worker_pipe(name: str) -> None:
    stop_pipe_reader(name); pipe_path = get_worker_pipe_path(name)
    if pipe_path.exists():
        try: pipe_path.unlink(); _log(_LOG_INFO, "pipe", f"Removed worker pipe: {pipe_path}")
        except OSError as e: _log(_LOG_WARN, "worker", f"Failed to remove worker pipe {pipe_path}: {e}")
    pipe_dir = pipe_path.parent
    if pipe_dir.exists():
        try: pipe_dir.rmdir()
        except OSError: pass
def pipe_reader_loop(name: str, stop_event: threading.Event) -> None:
    pipe_path = get_worker_pipe_path(name); _log(_LOG_INFO, "pipe", f"Pipe reader started for worker '{name}' at {pipe_path}")
    while not stop_event.is_set():
        try:
            if stop_event.is_set(): break
            with open(str(pipe_path), 'r') as pipe:
                while not stop_event.is_set():
                    line = pipe.readline()
                    if not line: break
                    message = line.strip()
                    if message:
                        _log(_LOG_INFO, "pipe", f"Pipe message for '{name}': {message[:100]}{'...' if len(message) > 100 else ''}")
                        try: _forward_pipe_message(name, message)
                        except (OSError, ValueError) as e: _log(_LOG_ERROR, "bridge", f"Error forwarding pipe message to '{name}': {e}")
        except FileNotFoundError: _log(_LOG_WARN, "pipe", f"Pipe for '{name}' no longer exists, stopping reader"); break
        except OSError as e:
            if stop_event.is_set(): break
            _log(_LOG_ERROR, "bridge", f"Pipe reader error for '{name}': {e}"); stop_event.wait(0.5)
    with processes.pipe_readers_lock:
        if name in processes.pipe_readers: processes.pipe_readers.pop(name, None)
    _log(_LOG_INFO, "pipe", f"Pipe reader stopped for worker '{name}'")
def _forward_pipe_message(name: str, message: str) -> None:
    import bridge
    if bridge.worker_manager is None or not bridge.worker_manager.send(name, message):
        _log(_LOG_WARN, "worker", f"Warning: Cannot forward pipe message to '{name}' - worker not found")
def start_pipe_reader(name: str) -> None:
    with processes.pipe_readers_lock:
        if name in processes.pipe_readers:
            thread, _stop = processes.pipe_readers[name]
            if thread.is_alive(): return
            _log(_LOG_WARN, "pipe", f"Pipe reader thread for '{name}' is dead, restarting"); processes.pipe_readers.pop(name, None)
    pipe_path = get_worker_pipe_path(name)
    if not pipe_path.exists(): _log(_LOG_WARN, "bridge", f"Cannot start pipe reader: pipe does not exist for '{name}'"); return
    stop_event = threading.Event()
    thread = threading.Thread( target=pipe_reader_loop, args=(name, stop_event), daemon=True, name=f"pipe-reader-{name}"
    )
    with processes.pipe_readers_lock: processes.pipe_readers[name] = (thread, stop_event)
    thread.start(); _log(_LOG_INFO, "pipe", f"Started pipe reader thread for '{name}'")
def stop_pipe_reader(name: str) -> None:
    with processes.pipe_readers_lock:
        if name not in processes.pipe_readers: return
        thread, stop_event = processes.pipe_readers.pop(name)
    stop_event.set(); pipe_path = get_worker_pipe_path(name)
    if pipe_path.exists():
        try:
            fd = os.open(str(pipe_path), os.O_WRONLY | os.O_NONBLOCK)
            try: os.write(fd, b"\n")
            finally: os.close(fd)
        except OSError: pass
    thread.join(timeout=TIMEOUT_THREAD_JOIN)
    if thread.is_alive(): _log(_LOG_WARN, "bridge", f"Warning: pipe reader thread for '{name}' did not stop gracefully")
def get_session_dir(name: str) -> Path:
    return SESSIONS_DIR / name
def ensure_session_dir(name: str) -> Path:
    session_dir = get_session_dir(name); session_dir.mkdir(parents=True, exist_ok=True, mode=0o700); SESSIONS_DIR.chmod(0o700)
    session_dir.chmod(0o700); return session_dir
def get_chat_id_file(name: str) -> Path:
    return get_session_dir(name) / "chat_id"
def _scan_latest_session_id(cwd: str, host: str | None = None) -> str:
    if not cwd: return ""
    slug = _project_slug(cwd)
    if host:
        try:
            cmd = [ "bash", "-c", f'ls -1t "$HOME/.claude/projects/{slug}"/*.jsonl 2>/dev/null | head -1', ]
            r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode != 0: return ""
            path = (r.stdout or "").strip()
            if not path: return ""
            return os.path.basename(path).removesuffix(".jsonl")
        except (subprocess.SubprocessError, OSError): return ""
    slug_dir = CLAUDE_PROJECTS_DIR / slug
    if not slug_dir.is_dir(): return ""
    try:
        jsonls = [p for p in slug_dir.iterdir()
                  if p.is_file() and p.suffix == ".jsonl"]
    except OSError: return ""
    if not jsonls: return ""
    latest = max(jsonls, key=lambda p: p.stat().st_mtime); return latest.stem
def _log_session_event(name: str, session_id: str, cwd: str, event: str) -> None:
    if not session_id: return
    try:
        session_dir = ensure_session_dir(name); history_file = session_dir / "session_history.jsonl"
        entry = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_clock.time())), "session_id": session_id, "cwd": cwd or "", "event": event})
        with open(history_file, "a") as fh: fh.write(entry + "\n")
        history_file.chmod(0o600)
    except OSError as exc: _log(_LOG_DEBUG, "io:_log_session_event", f"{type(exc).__name__}: {exc}")
def _ensure_workspace_trusted( cwd: str, config_path: Path | None = None,
) -> None:
    if not cwd: return
    target = config_path or _CLAUDE_JSON_PATH
    try:
        import fcntl
        lock_path = target.with_suffix(".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if target.exists(): data = cast(dict[str, object], json.loads(target.read_text()))
                else: data = {}
                _projects_raw = data.setdefault("projects", {})
                projects = _projects_raw if isinstance(_projects_raw, dict) else {}; _entry_raw = projects.get(cwd, {})
                entry = _entry_raw if isinstance(_entry_raw, dict) else {}
                if entry.get("hasTrustDialogAccepted") is True: return
                projects[cwd] = {**entry, "hasTrustDialogAccepted": True}; _tmp = target.with_suffix('.tmp')
                _tmp.write_text(json.dumps(data, indent=2)); os.replace(str(_tmp), str(target))
                _log(_LOG_INFO, "trust", f"pre-trusted workspace: {cwd}")
            finally: fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError) as exc: _log(_LOG_WARN, "trust", f"could not pre-trust {cwd}: {exc}")
def _cache_session_id(name: str, sid: str) -> None:
    if not sid: return
    try:
        session_dir = ensure_session_dir(name); id_file = session_dir / "claude_session_id"
        cwd = get_claude_session_cwd(name) or ""; old_content = id_file.read_text().strip() if id_file.exists() else ""
        old_lines = old_content.split("\n", 1) if old_content else []; old_sid = old_lines[0].strip() if old_lines else ""
        old_cwd = old_lines[1].strip() if len(old_lines) > 1 else ""
        if (sid == old_sid and old_cwd and cwd and
                old_cwd.rstrip("/") != cwd.rstrip("/")):
            _log(_LOG_WARN, "session",
                 f"{name}: rejecting stale session_id write "
                 f"(sid={sid[:12]}, old_cwd={old_cwd}, current_cwd={cwd})")
            return
        if old_sid != sid: _log_session_event(name, sid, cwd, "cache")
        _tmp = id_file.with_suffix('.tmp'); _tmp.write_text(f"{sid}\n{cwd}"); _tmp.chmod(0o600); os.replace(str(_tmp), str(id_file))
    except OSError as exc: _log(_LOG_DEBUG, "io:_cache_session_id", f"{type(exc).__name__}: {exc}")
def get_claude_session_id(name: str, authoritative: bool = False) -> str:
    cache_file = get_session_dir(name) / "claude_session_id"; current_cwd = get_claude_session_cwd(name)
    def _read_cache() -> str:
        if not cache_file.exists(): return ""
        content = cache_file.read_text().strip()
        if not content: return ""
        lines = content.split("\n", 1); sid = lines[0].strip()
        if not sid: return ""
        if len(lines) > 1:
            cached_cwd = lines[1].strip()
            if (cached_cwd and current_cwd and
                    cached_cwd.rstrip("/") != current_cwd.rstrip("/")):
                _log(_LOG_INFO, "session",
                     f"{name}: stale session_id (cached_cwd={cached_cwd}, "
                     f"current_cwd={current_cwd})")
                return ""
        return sid
    val = _read_cache()
    if val: return val
    if current_cwd:
        import bridge
        host = bridge.get_worker_host(name); scanned = _scan_latest_session_id(current_cwd, host=host)
        if scanned: _cache_session_id(name, scanned); return scanned
    return ""
def get_claude_session_cwd(name: str) -> str | None:
    import bridge
    cwd = bridge._get_worker_cwd(name)
    if cwd: return os.path.expanduser(cwd)
    return None
def save_claude_session_cwd(name: str, cwd: str) -> None:
    import bridge
    if cwd: cwd = os.path.expanduser(cwd)
    bridge._set_worker_cwd(name, cwd)
def clear_claude_session_id(name: str) -> None:
    id_file = get_session_dir(name) / "claude_session_id"
    if id_file.exists(): id_file.unlink()
def get_any_session_id(name: str) -> tuple[str, str]:
    session_dir = get_session_dir(name)
    if not session_dir.exists(): return "", ""
    for f in sorted(session_dir.glob("*_session_id")):
        val = f.read_text().strip()
        if val: source = f.name.replace("_session_id", ""); return val, source
    return "", ""
def _remap_path(local_path: str, host: str | None) -> str:
    if not host or not local_path: return local_path
    remote_home = _get_remote_home(host); local_home = os.path.expanduser("~")
    if remote_home and remote_home != local_home and local_path.startswith(local_home): return remote_home + local_path[len(local_home):]
    return local_path
def _get_remote_home(host: str | None) -> str:
    import claudecode as _cc  # late import — avoid circular
    with _cc.remote_cache.lock: cached = _cc.remote_cache.home_dirs.get(host or "")
    if cached is not None: return cached
    try:
        r = _remote_run(["bash", "-c", "echo $HOME"], host=host,
                        capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        home = r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError): home = ""
    with _cc.remote_cache.lock: _cc.remote_cache.home_dirs[host or ""] = home
    return home
def _capture_pane_text(tmux_name: str, lines: int = 50, host: str | None = None) -> str:
    if lines <= 0: return ""
    try:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", f"-{lines}"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return ""
    if result.returncode != 0: return ""
    return result.stdout
def normalize_backend(backend: str | None) -> str:
    return backend or DEFAULT_BACKEND
def normalize_cwd(cwd: str | None) -> str:
    if cwd is None: return ""
    raw = cwd.strip()
    if not raw: return ""
    return os.path.abspath(os.path.expanduser(raw))
def validate_cwd(cwd: str | None, host: str | None = None) -> tuple[str, str]:
    normalized = normalize_cwd(cwd)
    if not normalized: return "", "cwd is empty"
    if host:
        try:
            r = _remote_run(["test", "-d", normalized], host=host, capture_output=True, timeout=TIMEOUT_REMOTE_CMD)
            if r.returncode != 0: return "", f"cwd does not exist on {host}: {normalized}"
        except (subprocess.SubprocessError, OSError) as e: return "", f"cwd check failed on {host}: {e}"
    else:
        if not os.path.exists(normalized): return "", f"cwd does not exist: {normalized}"
        if not os.path.isdir(normalized): return "", f"cwd is not a directory: {normalized}"
    return normalized, ""
def get_worker_backend(name: str, session: RegistryWorkerDict | TmuxSessionDict | None = None) -> str:
    if session and session.get("backend"): return normalize_backend(str(session.get("backend")))
    import bridge
    registry = bridge._load_registry(); entry = registry.get("workers", {}).get(name, {})
    if entry.get("backend"): return normalize_backend(str(entry["backend"]))
    return DEFAULT_BACKEND
def get_tmux_env_value(tmux_name: str, key: str) -> str:
    result = _subprocess_runner.run(
        ["tmux", "show-environment", "-t", tmux_name, key],
        capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK )
    if result.returncode != 0: return ""
    value = result.stdout.strip()
    if "=" not in value: return ""
    return value.split("=", 1)[1]
def tmux_prompt_empty(tmux_name: str, timeout: float=0.5, host: str | None = None) -> bool:
    import re
    start = _clock.time()
    while _clock.time() - start < timeout:
        result = _remote_run(
            ["tmux", "capture-pane", "-t", tmux_name, "-p"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK )
        if result.returncode == 0:
            if re.search(r'^❯\s*$', result.stdout, re.MULTILINE): return True
        _clock.sleep(DELAY_BRIEF * 2)
    return False
def export_hook_env(tmux_name: str, backend: str = DEFAULT_WORKER_BACKEND, host: str | None = None) -> None:
    our_url = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    try:
        r = _remote_run(["tmux", "show-environment", "-t", tmux_name, "BRIDGE_URL"],
                        host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK)
        existing = r.stdout.strip().split("=", 1)[-1] if r.returncode == 0 else ""
        if existing and existing != our_url:
            _urlopen(existing, timeout=TIMEOUT_THREAD_JOIN).read()
            _log(_LOG_DEBUG, "worker", f"  SKIP export_hook_env({tmux_name}): owned by live bridge at {existing}"); return
    except (urllib.error.URLError, OSError, TimeoutError): pass
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "PORT", str(PORT)], host=host, timeout=TIMEOUT_TMUX_CHECK)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "TMUX_PREFIX", TMUX_PREFIX], host=host, timeout=TIMEOUT_TMUX_CHECK)
    sessions_dir_val = _remap_path(str(SESSIONS_DIR), host) if host else str(SESSIONS_DIR)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "SESSIONS_DIR", sessions_dir_val], host=host, timeout=TIMEOUT_TMUX_CHECK)
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "WORKER_BACKEND", normalize_backend(backend)], host=host, timeout=TIMEOUT_TMUX_CHECK)
    bridge_url_val = (BRIDGE_PUBLIC_URL or BRIDGE_URL) if host else BRIDGE_URL
    _remote_run(["tmux", "set-environment", "-t", tmux_name, "BRIDGE_URL", bridge_url_val], host=host, timeout=TIMEOUT_TMUX_CHECK)
