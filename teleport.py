"""Teleport/teleback functionality extracted from bridge.py's CommandRouter.

All functions are free functions that accept explicit dependencies
instead of accessing ``self``.  Bridge-level globals and helpers are
imported late (inside each function) to avoid circular imports.
"""
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
    _get_remote_home, _is_git_repo, _project_slug, _remap_path,
    _remote_run, _resolve_remote_tool,
    ensure_session_dir, get_claude_session_cwd, get_claude_session_id,
    get_session_dir, get_worker_backend,
    is_claude_running, normalize_backend, save_claude_session_cwd,
    clear_claude_session_id, get_any_session_id,
    get_backend, tmux_exists, watchdog,
)

# Late-import wrappers — these live in bridge.py, not claudecode
def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    import bridge as _b; return _b._get_claude_pid(pane_pid, host=host)
def get_worker_host(name: str) -> str | None:
    import bridge as _b; return _b.get_worker_host(name)

if TYPE_CHECKING:
    from claudecode import Backend, TmuxSessionDict
    from telegram import ChatId

# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------

def teleport_notify(chat_id: "ChatId | None", text: str) -> None:
    if chat_id is None:
        return
    try:
        import bridge
        bridge.transport.send_text(chat_id, text)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _log(_LOG_DEBUG, "notify:_teleport_notify", f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# Preflight & conflict checks
# ---------------------------------------------------------------------------

def check_teleback_conflicts(name: str, remote_host: str | None, local_cwd: str | None) -> list[str]:
    import bridge
    conflicts: list[str] = []
    if not local_cwd or not remote_host:
        return conflicts
    local_is_git = os.path.isdir(os.path.join(local_cwd, ".git"))
    if not local_is_git:
        return conflicts
    local_status = _subprocess_runner.run(
        ["git", "-C", local_cwd, "status", "--porcelain"],
        capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
    local_changed = bool(local_status.stdout.strip()) if local_status.returncode == 0 else False
    local_head = _subprocess_runner.run(
        ["git", "-C", local_cwd, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND)
    local_commit = local_head.stdout.strip() if local_head.returncode == 0 else ""
    remote_cwd = _remap_path(local_cwd, remote_host)
    r_status = _remote_run(
        ["git", "-C", remote_cwd, "status", "--porcelain"],
        host=remote_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
    remote_changed = bool(r_status.stdout.strip()) if r_status.returncode == 0 else False
    r_head = _remote_run(
        ["git", "-C", remote_cwd, "rev-parse", "HEAD"],
        host=remote_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND)
    remote_commit = r_head.stdout.strip() if r_head.returncode == 0 else ""
    if local_changed and remote_changed:
        local_files = [l.strip().split(None, 1)[-1]
                      for l in local_status.stdout.strip().splitlines()[:5]]
        remote_files = [l.strip().split(None, 1)[-1]
                       for l in r_status.stdout.strip().splitlines()[:5]]
        conflicts.append( f"VPS has uncommitted changes: {', '.join(local_files)}")
        conflicts.append(
            f"Remote has uncommitted changes: {', '.join(remote_files)}")
    elif local_commit and remote_commit and local_commit != remote_commit:
        if local_changed: conflicts.append( f"VPS has uncommitted changes AND different commit than remote")
        elif remote_changed:
            conflicts.append( f"VPS has new commits since teleport (HEAD: {local_commit[:8]})")
            conflicts.append( f"Remote has uncommitted changes (HEAD: {remote_commit[:8]})")
    return conflicts


def run_teleport_preflight(target_host: str, worker_name: str, backend_name: str) -> list[str]:
    import bridge
    fails = []
    preflight_dirs = [
        os.path.expanduser("~/agent-config/teleport-preflight.d"),
        os.path.expanduser("~/.config/claudecode-telegram/teleport-preflight.d"), ]
    env = os.environ.copy()
    env["TARGET_HOST"] = target_host or ""
    env["WORKER_NAME"] = worker_name
    env["BACKEND"] = backend_name
    env["BRIDGE_URL"] = bridge.BRIDGE_PUBLIC_URL or bridge.BRIDGE_URL
    seen_scripts: set[str] = set()
    for pdir in preflight_dirs:
        if not os.path.isdir(pdir): continue
        scripts = sorted(
            f for f in os.listdir(pdir)
            if f.endswith(".sh") and os.access(os.path.join(pdir, f), os.X_OK))
        for script in scripts:
            script_path = os.path.join(pdir, script); real_path = os.path.realpath(script_path)
            if real_path in seen_scripts: continue
            seen_scripts.add(real_path)
            try:
                r = _subprocess_runner.run(
                    [script_path], env=env,
                    capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
                if r.returncode != 0:
                    reason = r.stdout.strip().split("\n")[0] if r.stdout.strip() else f"{script} failed"
                    fails.append(reason)
            except subprocess.TimeoutExpired: fails.append(f"{script} timed out")
            except (subprocess.SubprocessError, OSError) as e: fails.append(f"{script} error: {e}")
    return fails

# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def sync_working_directory(source_cwd: str, target_cwd: str,
                           source_host: str | None = None, target_host: str | None = None,
                           full: bool = False) -> bool:
    import bridge
    if not full and _is_git_repo(source_cwd, host=source_host):
        project = bridge._get_project_name(source_cwd, host=source_host)
        if project:
            try:
                bare_repo = bridge._ensure_bare_repo(project)
                meta = bridge._git_push_state(source_cwd, project, bare_repo, host=source_host)
                if meta:
                    bare_url = bridge._bare_repo_url(bare_repo, target_host=target_host)
                    if bridge._git_pull_state(target_cwd, project, bare_url, meta,
                                              host=target_host):
                        _log(_LOG_INFO, "teleport", f"git sync succeeded for {project}")
                        return True
                    _log(_LOG_WARN, "teleport", f"git pull failed, falling back to rsync")
                else: _log(_LOG_WARN, "teleport", f"git push failed, falling back to rsync")
            except (subprocess.SubprocessError, OSError, KeyError) as e:
                _log(_LOG_ERROR, "teleport", f"git sync error, falling back to rsync: {e}")
    return rsync_working_directory(
        source_cwd, target_cwd, source_host, target_host, full)


def rsync_working_directory(source_cwd: str, target_cwd: str,
                            source_host: str | None = None, target_host: str | None = None,
                            full: bool = False) -> bool:
    import bridge
    _remote_run(["mkdir", "-p", target_cwd], host=target_host, capture_output=True)
    cmd = ["rsync", "-az", "--delete"]; gitignore_tmpfile = None
    if not full:
        try:
            gi_result = _remote_run(
                ["git", "-C", source_cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
                host=source_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_FILE_TRANSFER)
            if gi_result.returncode == 0 and gi_result.stdout.strip():
                fd, gitignore_tmpfile = tempfile.mkstemp( prefix="rsync-gitignore-", suffix=".txt")
                try:
                    os.write(fd, gi_result.stdout.encode())
                finally:
                    os.close(fd)
                cmd.extend(["--exclude-from", gitignore_tmpfile])
        except (ConnectionError, TimeoutError, OSError) as e:
            _log(_LOG_WARN, "teleport", f"git ls-files failed, skipping gitignore excludes: {e}")
        for excl in bridge.TELEPORT_RSYNC_EXCLUDES: cmd.extend(["--exclude", excl])
    src = source_cwd.rstrip("/") + "/"; dst = target_cwd.rstrip("/") + "/"
    if source_host: cmd.extend([f"{source_host}:{src}", dst])
    elif target_host: cmd.extend([src, f"{target_host}:{dst}"])
    else: cmd.extend([src, dst])
    try:
        r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=bridge.TIMEOUT_FULL_SYNC)
        if r.returncode != 0: _log(_LOG_WARN, "teleport", f"rsync failed: cmd={cmd} rc={r.returncode} stderr={r.stderr[:500]}")
        return r.returncode == 0
    finally:
        if gitignore_tmpfile and os.path.exists(gitignore_tmpfile): os.unlink(gitignore_tmpfile)


def sync_session_transcript(session_id: str, source_cwd: str, target_cwd: str,
                            source_host: str | None = None, target_host: str | None = None) -> None:
    import bridge
    if not session_id: return
    source_slug = _project_slug(source_cwd); target_slug = _project_slug(target_cwd)
    source_dir = f".claude/projects/{source_slug}"; target_dir = f".claude/projects/{target_slug}"
    if target_host: _remote_run(["bash", "-c", f"mkdir -p $HOME/{target_dir}"], host=target_host, capture_output=True)
    else: os.makedirs(os.path.expanduser(f"~/{target_dir}"), exist_ok=True)
    jsonl = f"{session_id}.jsonl"
    for item in [jsonl, f"{session_id}/"]:
        if source_host:
            src_path = f"~/{source_dir}/{item}"; local_dst = os.path.expanduser(f"~/{target_dir}/")
            cmd = ["rsync", "-az", f"{source_host}:{src_path}", local_dst]
        elif target_host:
            local_src = os.path.expanduser(f"~/{source_dir}/{item}"); dst_path = f"~/{target_dir}/"
            cmd = ["rsync", "-az", local_src, f"{target_host}:{dst_path}"]
        else:
            local_src = os.path.expanduser(f"~/{source_dir}/{item}")
            local_dst = os.path.expanduser(f"~/{target_dir}/"); cmd = ["rsync", "-az", local_src, local_dst]
        r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=bridge.TIMEOUT_RSYNC)
        if r.returncode != 0: _log(_LOG_WARN, "teleport", f"transcript sync failed for {item}: {r.stderr[:200]}")


def sync_shared_repos(target_host: str, chat_id: "int | str | None" = None) -> list[str]:
    import bridge
    if not target_host: return []
    warnings: list[str] = []
    def _warn(msg: str) -> None: warnings.append(msg); _log(_LOG_INFO, "teleport", msg)
    try:
        home = os.path.expanduser("~")
        git_repos = {"team": os.path.join(home, "team"), "agent-config": os.path.join(home, "agent-config")}
        for rn, rp in git_repos.items():
            if os.path.isdir(os.path.join(rp, ".git")):
                try:
                    for cmd in [["git", "-C", rp, "add", "-A"], ["git", "-C", rp, "commit", "-m", f"teleport sync: {rn}"]]:
                        _subprocess_runner.run(cmd, capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
                    _subprocess_runner.run(["git", "-C", rp, "push", "origin", "master"], capture_output=True, timeout=bridge.TIMEOUT_LARGE_TRANSFER)
                except (subprocess.SubprocessError, OSError) as e: _warn(f"git push {rn}: {e}")
        for rn in git_repos:
            try: _remote_run(["bash", "-c", f"cd ~/{rn} 2>/dev/null && git pull origin master 2>/dev/null || true"], host=target_host, capture_output=True, timeout=bridge.TIMEOUT_LARGE_TRANSFER)
            except (subprocess.SubprocessError, OSError) as e: _warn(f"git pull {rn} on {target_host}: {e}")
        for subdir in ("skills", "hooks", "scripts"):
            try: _remote_run(["bash", "-c", f"[ -d ~/agent-config/.claude/{subdir} ] && rsync -az --checksum ~/agent-config/.claude/{subdir}/ ~/.claude/{subdir}/"], host=target_host, capture_output=True, timeout=bridge.TIMEOUT_LARGE_TRANSFER)
            except (subprocess.SubprocessError, OSError) as e: _warn(f"agent-config deploy {subdir}: {e}")
        remote_home = _get_remote_home(target_host)
        if remote_home and remote_home != home:
            settings_src = os.path.expanduser("~/.claude/settings.json")
            if os.path.exists(settings_src):
                with open(settings_src) as f: settings_text = f.read().replace(home, remote_home)
                fd, tmp = tempfile.mkstemp(suffix=".json")
                try: os.write(fd, settings_text.encode())
                finally: os.close(fd)
                try: _subprocess_runner.run(["rsync", "-az", tmp, f"{target_host}:.claude/settings.json"], capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
                finally: os.unlink(tmp)
    except (subprocess.SubprocessError, OSError) as e: _warn(f"shared repo sync: {e}")
    return warnings


def sync_worker_data_back(name: str, source_host: str | None) -> None:
    import bridge
    if not source_host: return
    home = os.path.expanduser("~"); worker_team_dir = os.path.join(home, "team", name)
    if os.path.isdir(worker_team_dir):
        _subprocess_runner.run(
            ["rsync", "-az", f"{source_host}:team/{name}/", f"{worker_team_dir}/"],
            capture_output=True, timeout=bridge.TIMEOUT_GIT_OP)
    r = _remote_run(
        ["bash", "-c", "find ~/.claude/projects/*/memory -name '*.md' 2>/dev/null | head -50"],
        host=source_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
    if r.returncode == 0 and r.stdout.strip():
        for remote_file in r.stdout.strip().splitlines():
            remote_home = _get_remote_home(source_host)
            if remote_home and remote_file.startswith(remote_home):
                local_file = home + remote_file[len(remote_home):]; local_dir = os.path.dirname(local_file)
                os.makedirs(local_dir, exist_ok=True)
                _subprocess_runner.run(
                    ["rsync", "-az", f"{source_host}:{remote_file}", local_file],
                    capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)


def install_hooks_on_target(target_host: str) -> list[str]:
    import bridge
    if not target_host: return []
    warnings = []
    try:
        _remote_run(["chmod", "-R", "700", ".claude/hooks"], host=target_host, capture_output=True)
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            _subprocess_runner.run(
                ["rsync", "-az", claude_json, f"{target_host}:.claude.json"],
                capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
        else:
            _remote_run(
                ["python3", "-c",
                 'import json,os,pathlib;'
                 'p=pathlib.Path(os.path.expanduser("~/.claude.json"));'
                 'd=json.loads(p.read_text()) if p.exists() else {};'
                 'd["hasCompletedOnboarding"]=True;'
                 'd.setdefault("numStartups",1);'
                 'p.write_text(json.dumps(d))'],
                host=target_host, capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
    except (subprocess.SubprocessError, OSError) as e:
        w = f"hook install: {e}"
        warnings.append(w)
        _log(_LOG_INFO, "teleport", f"{w}")
    return warnings


def sync_session_files_to_target(name: str, target_sessions_dir: str, target_host: str) -> None:
    import bridge
    local_session_dir = bridge.SESSIONS_DIR / name
    if not local_session_dir.is_dir(): return
    remote_session_dir = f"{target_sessions_dir}/{name}"
    _remote_run(["mkdir", "-p", remote_session_dir], host=target_host, capture_output=True)
    for fname in ["chat_id", "claude_session_id"]:
        local_file = local_session_dir / fname
        if local_file.exists():
            _subprocess_runner.run(
                ["rsync", "-az", str(local_file), f"{target_host}:{remote_session_dir}/{fname}"],
                capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)


def sync_credentials_to_target(target_host: str) -> None:
    import bridge
    local_creds = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.exists(local_creds): return
    try:
        r = _remote_run(["cat", ".claude/.credentials.json"],
                         host=target_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND)
        if r.returncode == 0 and r.stdout.strip():
            remote_data = cast(dict[str, object], json.loads(r.stdout))
            local_data = cast(dict[str, object], json.loads(Path(local_creds).read_text()))
            _ro = remote_data.get("claudeAiOauth", {}); remote_oauth = _ro if isinstance(_ro, dict) else {}
            _lo = local_data.get("claudeAiOauth", {}); local_oauth = _lo if isinstance(_lo, dict) else {}
            remote_refresh = str(remote_oauth.get("refreshToken", ""))
            local_refresh = str(local_oauth.get("refreshToken", ""))
            remote_exp = int(remote_oauth.get("expiresAt", 0)); now_ms = int(_clock.time() * 1000)
            if remote_refresh and remote_refresh != local_refresh and remote_exp > now_ms:
                _log(_LOG_INFO, "creds", f"Target {target_host} has independent valid credentials, skipping sync")
                return
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, subprocess.SubprocessError, OSError): pass
    _remote_run(["mkdir", "-p", ".claude"], host=target_host, capture_output=True)
    _subprocess_runner.run(
        ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json.tmp"],
        capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
    _remote_run(["mv", ".claude/.credentials.json.tmp",
                  ".claude/.credentials.json"],
                 host=target_host, capture_output=True)
    _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host, capture_output=True)
    _log(_LOG_INFO, "creds", f"Synced credentials to {target_host}")

# ---------------------------------------------------------------------------
# Stop / start / rollback
# ---------------------------------------------------------------------------

def stop_worker_for_teleport(name: str, tmux_name: str, host: str | None = None) -> str | None:
    import bridge
    session_id = get_claude_session_id(name, authoritative=True)
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"],
                 host=host, capture_output=True)
    for _ in range(20):
        _clock.sleep(bridge.DELAY_RETRY)
        r = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
            host=host, capture_output=True, text=True)
        if r.returncode != 0: break
        pane_pid = r.stdout.strip()
        if pane_pid:
            claude_pid = _get_claude_pid(pane_pid, host=host)
            if not claude_pid: break
    else:
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], host=host, capture_output=True)
        _clock.sleep(bridge.DELAY_STARTUP)
    return get_claude_session_id(name, authoritative=True) or session_id


def start_worker_on_target(name: str, target_host: str, target_cwd: str | None,
                           session_id: str | None, backend_name: str, skip_session_sync: bool = False) -> bool:
    import bridge
    tmux_name = f"{bridge.TMUX_PREFIX}{name}"
    _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host, capture_output=True)
    _clock.sleep(bridge.DELAY_TMUX_SEND)
    r = _remote_run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
        host=target_host, capture_output=True, text=True)
    if r.returncode != 0:
        _log(_LOG_WARN, "teleport", f"tmux new-session failed: rc={r.returncode} stderr={r.stderr[:200] if r.stderr else ''}")
        return False
    _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
                host=target_host, capture_output=True)
    _clock.sleep(bridge.DELAY_RETRY)
    target_sessions_dir = _remap_path(str(bridge.SESSIONS_DIR), target_host)
    if target_host and not skip_session_sync: sync_session_files_to_target(name, target_sessions_dir, target_host)
    if target_host: sync_credentials_to_target(target_host)
    for key, value in {
        "PORT": str(bridge.PORT),
        "TMUX_PREFIX": bridge.TMUX_PREFIX,
        "SESSIONS_DIR": target_sessions_dir,
        "WORKER_BACKEND": normalize_backend(backend_name),
        "BRIDGE_URL": bridge.BRIDGE_PUBLIC_URL or bridge.BRIDGE_URL,
    }.items():
        _remote_run(["tmux", "set-environment", "-t", tmux_name, key, value], host=target_host, capture_output=True)
    _clock.sleep(bridge.DELAY_TMUX_SEND)
    _remote_run(
        ["tmux", "send-keys", "-t", tmux_name, 'eval "$(tmux show-environment -s)" && unset CLAUDECODE', "Enter"],
        host=target_host, capture_output=True)
    _clock.sleep(bridge.DELAY_TMUX_SEND)
    backend = get_backend(backend_name); cli_cmd = backend.start_cmd(session_id or "")
    if target_host:
        id_result = _remote_run(["id", "-u"], host=target_host, capture_output=True, text=True)
        if id_result.returncode == 0 and id_result.stdout.strip() == "0":
            cli_cmd = cli_cmd.replace(" --dangerously-skip-permissions", "")
    start_cmd = f'unset CLAUDECODE && {cli_cmd}'
    if target_cwd: start_cmd = f'cd {shlex.quote(target_cwd)} && {start_cmd}'
    _log(_LOG_INFO, "teleport", f"start_cmd={start_cmd}")
    _remote_run(
        ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
        host=target_host, capture_output=True)
    if backend.is_interactive:
        for delay, key in [(3.0, "Enter"), (2.0, "Enter"),
                           (1.0, "Enter"), (1.0, "Enter")]:
            _clock.sleep(delay)
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key],
                         host=target_host, capture_output=True)
    for attempt in range(30):
        _clock.sleep(bridge.DELAY_STARTUP)
        r = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
            host=target_host, capture_output=True, text=True)
        if r.returncode != 0:
            if attempt % 10 == 0:
                _log(_LOG_WARN, "teleport", f"verify attempt {attempt}: tmux display-message failed rc={r.returncode}")
            continue
        pane_pid = r.stdout.strip(); claude_pid = _get_claude_pid(pane_pid, host=target_host) if pane_pid else None
        if claude_pid:
            _log(_LOG_INFO, "teleport", f"verified: claude running as pid {claude_pid} (pane {pane_pid})")
            return True
        if attempt % 10 == 0:
            _log(_LOG_INFO, "teleport", f"verify attempt {attempt}: pane_pid={pane_pid}, no claude yet")
            cap = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=target_host, capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND)
            if cap.returncode == 0: _log(_LOG_INFO, "teleport", f"pane content: {cap.stdout[:300]}")
    _log(_LOG_WARN, "teleport", f"verify FAILED after 30 attempts")
    return False


def teleport_rollback(name: str, tmux_name: str, source_host: str | None, source_cwd: str | None,
                      session_id: str | None, backend_name: str, chat_id: "ChatId | None", reason: str) -> None:
    import bridge
    teleport_notify(chat_id, f"Teleport failed: {reason}. Rolling back...")
    try:
        if source_cwd: save_claude_session_cwd(name, source_cwd)
        if not tmux_exists(tmux_name, host=source_host):
            _remote_run(
                ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
                host=source_host, capture_output=True)
            _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"],
                        host=source_host, capture_output=True)
        backend = get_backend(backend_name)
        start_cmd = f'unset CLAUDECODE && {backend.start_cmd(session_id or "")}'
        if source_cwd: start_cmd = f'cd {shlex.quote(source_cwd)} && {start_cmd}'
        _remote_run(
            ["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"],
            host=source_host, capture_output=True)
        teleport_notify(chat_id, f"{name} restarted on source. Teleport cancelled.")
    except (subprocess.SubprocessError, OSError) as e: teleport_notify(chat_id, f"Rollback also failed: {e}")
    try:
        state_file = bridge.SESSIONS_DIR / name / "teleport_state"
        state_file.unlink(missing_ok=True)
    except OSError as exc: _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")

# ---------------------------------------------------------------------------
# Main teleport orchestration
# ---------------------------------------------------------------------------

def do_teleport(name: str, target_host: str, target_cwd: str, full_sync: bool,
                chat_id: "int | str", workers: object, is_teleback: bool = False) -> None:
    """Execute the full teleport/teleback sequence.

    ``workers`` is the WorkerManager instance (used for
    ``get_registered_sessions``, ``send``, and ``_build_welcome``).
    """
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
        bridge._ensure_workspace_trusted_remote(target_cwd, target_host)
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
                    ctx = bridge._build_teleport_context(
                        name=name,
                        source_host=source_host,
                        target_host=target_host,
                        source_cwd=source_cwd,
                        session_id=session_id, )
                    _clock.sleep(0.5)
                    workers.send(name, ctx)  # type: ignore[attr-defined]
            except (ConnectionError, TimeoutError, AttributeError, OSError) as e:
                _log(_LOG_WARN, "teleport", f"Warning: failed to send welcome to {name}: {e}")
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

# ---------------------------------------------------------------------------
# Command entry points
# ---------------------------------------------------------------------------

def cmd_teleport(router: object, arg: str, chat_id: "ChatId", check_only: bool = False) -> bool:
    """Handle /teleport command.  ``router`` is the CommandRouter instance."""
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
    """Handle /teleback command.  ``router`` is the CommandRouter instance."""
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

# ---------------------------------------------------------------------------
# Remote worker restart (uses teleport infra)
# ---------------------------------------------------------------------------

def restart_remote_worker(name: str, backend_name: str, backend: "Backend", tmux_name: str,
                          host: str, mode: str, workers: object) -> tuple[bool, str | None]:
    """Restart a worker on a remote host.  ``workers`` is the WorkerManager."""
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
    bridge._ensure_workspace_trusted_remote(target_cwd, host)
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
