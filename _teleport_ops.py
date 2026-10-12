"""Teleport operations: git state, file sync, preflight, worker lifecycle."""

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
    _log,
    _LOG_ERROR,
    _LOG_WARN,
    _LOG_INFO,
    _LOG_DEBUG,
    _subprocess_runner,
    _clock,
)
from claudecode import (
    GIT_SERVER_DIR,
    GitPushStateResult,
    _get_remote_home,
    _is_git_repo,
    _project_slug,
    _remap_path,
    _remote_run,
    _resolve_remote_tool,
    ensure_session_dir,
    get_claude_session_cwd,
    get_claude_session_id,
    get_session_dir,
    get_worker_backend,
    is_claude_running,
    normalize_backend,
    save_claude_session_cwd,
    clear_claude_session_id,
    get_any_session_id,
    get_backend,
    tmux_exists,
    watchdog,
)


def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    import bridge as _b

    return _b._get_claude_pid(pane_pid, host=host)


def get_worker_host(name: str) -> str | None:
    import bridge as _b

    return _b.get_worker_host(name)


if TYPE_CHECKING:
    from claudecode import Backend, TmuxSessionDict


def _ensure_bare_repo(project_name: str) -> str:
    bare_path = os.path.join(GIT_SERVER_DIR, f"{project_name}.git")
    if not os.path.isdir(bare_path):
        os.makedirs(GIT_SERVER_DIR, exist_ok=True)
        _subprocess_runner.run(
            ["git", "init", "--bare", bare_path], capture_output=True, text=True, check=True, timeout=10
        )
    return bare_path


def _bare_repo_url(bare_repo_path: str, target_host: str | None = None) -> str:
    if target_host:
        return f"claude@100.125.36.102:{bare_repo_path}"
    return bare_repo_path


def _git_push_state(
    source_cwd: str, worker_name: str, bare_repo: str, host: str | None = None
) -> GitPushStateResult | None:
    import bridge

    try:
        r = _remote_run(
            ["git", "-C", source_cwd, "rev-parse", "HEAD"],
            host=host,
            capture_output=True,
            text=True,
            timeout=bridge.TIMEOUT_FILE_TRANSFER,
        )
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"rev-parse HEAD failed: {r.stderr[:200]}")
            return None
        orig_sha = r.stdout.strip()
        r = _remote_run(
            ["git", "-C", source_cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            host=host,
            capture_output=True,
            text=True,
            timeout=bridge.TIMEOUT_REMOTE_CMD,
        )
        orig_branch = r.stdout.strip() if r.returncode == 0 else "HEAD"
        r = _remote_run(
            ["git", "-C", source_cwd, "diff", "--cached", "--name-only"],
            host=host,
            capture_output=True,
            text=True,
            timeout=bridge.TIMEOUT_FILE_TRANSFER,
        )
        staged_files = [f for f in r.stdout.strip().split("\n") if f] if r.returncode == 0 else []
        _remote_run(
            ["git", "-C", source_cwd, "add", "-A"],
            host=host,
            capture_output=True,
            text=True,
            timeout=bridge.TIMEOUT_GIT_OP,
        )
        try:
            r = _remote_run(
                ["git", "-C", source_cwd, "stash", "create"],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_GIT_OP,
            )
            stash_sha = r.stdout.strip() if r.returncode == 0 else ""
        finally:
            _remote_run(
                ["git", "-C", source_cwd, "reset", "HEAD"],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_FILE_TRANSFER,
            )
            if staged_files:
                _remote_run(
                    ["git", "-C", source_cwd, "add", "--"] + staged_files,
                    host=host,
                    capture_output=True,
                    text=True,
                    timeout=bridge.TIMEOUT_FILE_TRANSFER,
                )
        push_sha = stash_sha if stash_sha else orig_sha
        ref = f"refs/heads/teleport/{worker_name}"
        if host:
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force", f"claude@100.125.36.102:{bare_repo}", f"{push_sha}:{ref}"],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_LARGE_TRANSFER,
            )
        else:
            r = _remote_run(
                ["git", "-C", source_cwd, "push", "--force", bare_repo, f"{push_sha}:{ref}"],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_LARGE_TRANSFER,
            )
        if r.returncode != 0:
            _log(_LOG_WARN, "git-sync", f"push failed: {r.stderr[:200]}")
            return None
        return {
            "orig_sha": orig_sha,
            "orig_branch": orig_branch,
            "staged_files": staged_files,
            "stash_sha": stash_sha or None,
        }
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"push state error: {e}")
        return None


def _git_pull_state(
    target_cwd: str, worker_name: str, bare_repo_url: str, metadata: GitPushStateResult, host: str | None = None
) -> bool:
    import bridge

    try:
        orig_sha = metadata["orig_sha"]
        orig_branch = metadata["orig_branch"]
        staged_files = metadata.get("staged_files", [])
        stash_sha = metadata.get("stash_sha")
        ref = f"teleport/{worker_name}"
        is_existing = False
        try:
            r = _remote_run(
                ["git", "-C", target_cwd, "rev-parse", "--git-dir"],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_REMOTE_CMD,
            )
            is_existing = r.returncode == 0
        except (subprocess.SubprocessError, OSError) as exc:
            _log(_LOG_DEBUG, "probe:_git_pull_state", f"{type(exc).__name__}: {exc}")
        if not is_existing:
            r = _remote_run(
                ["git", "clone", "--no-checkout", bare_repo_url, target_cwd],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_RSYNC,
            )
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"clone failed: {r.stderr[:200]}")
                return False
            _remote_run(
                ["git", "-C", target_cwd, "config", "user.email", "teleport@bridge"], host=host, capture_output=True
            )
            _remote_run(["git", "-C", target_cwd, "config", "user.name", "teleport"], host=host, capture_output=True)
        else:
            _remote_run(["git", "-C", target_cwd, "remote", "remove", "vps"], host=host, capture_output=True)
            _remote_run(
                ["git", "-C", target_cwd, "remote", "add", "vps", bare_repo_url],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_REMOTE_CMD,
            )
            r = _remote_run(
                ["git", "-C", target_cwd, "fetch", "vps", ref],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_RSYNC,
            )
            if r.returncode != 0:
                _log(_LOG_WARN, "git-sync", f"fetch failed: {r.stderr[:200]}")
                return False
        if orig_branch and orig_branch != "HEAD":
            _remote_run(
                ["git", "-C", target_cwd, "checkout", "-B", orig_branch, orig_sha],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_GIT_OP,
            )
        else:
            _remote_run(
                ["git", "-C", target_cwd, "checkout", orig_sha],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_GIT_OP,
            )
        if stash_sha:
            fetch_ref = f"vps/{ref}" if is_existing else f"origin/{ref}"
            r = _remote_run(
                ["git", "-C", target_cwd, "stash", "apply", fetch_ref],
                host=host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_GIT_OP,
            )
            if r.returncode != 0:
                _remote_run(
                    ["git", "-C", target_cwd, "read-tree", "-u", "--reset", orig_sha],
                    host=host,
                    capture_output=True,
                    text=True,
                    timeout=bridge.TIMEOUT_FILE_TRANSFER,
                )
                r = _remote_run(
                    ["git", "-C", target_cwd, "cherry-pick", "--no-commit", fetch_ref],
                    host=host,
                    capture_output=True,
                    text=True,
                    timeout=bridge.TIMEOUT_GIT_OP,
                )
            if staged_files:
                _remote_run(
                    ["git", "-C", target_cwd, "reset", "HEAD"],
                    host=host,
                    capture_output=True,
                    text=True,
                    timeout=bridge.TIMEOUT_FILE_TRANSFER,
                )
                _remote_run(
                    ["git", "-C", target_cwd, "add", "--"] + staged_files,
                    host=host,
                    capture_output=True,
                    text=True,
                    timeout=bridge.TIMEOUT_FILE_TRANSFER,
                )
        return True
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "git-sync", f"pull state error: {e}")
        return False


def _get_project_name(cwd: str, host: str | None = None) -> str | None:
    import bridge

    try:
        r = _remote_run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            host=host,
            capture_output=True,
            text=True,
            timeout=bridge.TIMEOUT_REMOTE_CMD,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        url = r.stdout.strip()
        if url.endswith(".git"):
            url = url[:-4]
        if ":" in url and not url.startswith("http"):
            name = url.rsplit("/", 1)[-1] if "/" in url.split(":")[-1] else url.split(":")[-1]
        else:
            name = url.rsplit("/", 1)[-1]
        return name if name else None
    except (subprocess.SubprocessError, OSError):
        return None


def _ensure_workspace_trusted_remote(
    cwd: str | None,
    host: str | None,
) -> None:
    import bridge

    if not cwd:
        return
    if not host:
        bridge._ensure_workspace_trusted(cwd)
        return
    script = (
        "import json, pathlib, os, fcntl; "
        "p = pathlib.Path(os.path.expanduser('~/.claude.json')); "
        "lk = open(str(p) + '.lock', 'w'); "
        "fcntl.flock(lk, fcntl.LOCK_EX); "
        "d = json.loads(p.read_text()) if p.exists() else {}; "
        "proj = d.setdefault('projects', {}); "
        f"e = proj.get({cwd!r}, {{}}); "
        "changed = e.get('hasTrustDialogAccepted') is not True; "
        f"proj[{cwd!r}] = {{**e, 'hasTrustDialogAccepted': True}} if changed else e; "
        "p.write_text(json.dumps(d, indent=2)) if changed else None; "
        "fcntl.flock(lk, fcntl.LOCK_UN); lk.close(); "
        "print('trusted' if changed else 'already')"
    )
    try:
        r = _remote_run(
            ["python3", "-c", script], host=host, capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND
        )
        if r.returncode == 0:
            _log(_LOG_INFO, "trust", f"remote pre-trust {cwd} on {host}: {r.stdout.strip()}")
        else:
            _log(_LOG_WARN, "trust", f"remote pre-trust failed on {host}: {r.stderr[:200]}")
    except (OSError, TimeoutError) as exc:
        _log(_LOG_WARN, "trust", f"remote pre-trust {cwd} on {host}: {exc}")


def _build_teleport_context(
    name: str,
    source_host: str | None,
    target_host: str,
    source_cwd: str | None,
    session_id: str | None,
) -> str:
    src_label = source_host or "VPS (local)"
    lines = [
        f"📦 You were teleported from {src_label} to {target_host}.",
        f"Previous workspace: {source_cwd}",
    ]
    if session_id:
        lines.append(f"Previous session: {session_id}")
        lines.append("")
        lines.append(
            "To retrieve your previous work context, run:\n"
            f"  beast transcript search --session {session_id} --last 20 --full"
        )
    else:
        lines.append("No previous session was active.")
    return "\n".join(lines)


def teleport_notify(chat_id: "ChatId | None", text: str) -> None:
    if chat_id is None:
        return
    try:
        import bridge

        bridge.transport.send_text(chat_id, text)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _log(_LOG_DEBUG, "notify:_teleport_notify", f"{type(exc).__name__}: {exc}")


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
        capture_output=True,
        text=True,
        timeout=bridge.TIMEOUT_REMOTE_CMD,
    )
    local_changed = bool(local_status.stdout.strip()) if local_status.returncode == 0 else False
    local_head = _subprocess_runner.run(
        ["git", "-C", local_cwd, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=bridge.TIMEOUT_TMUX_SEND
    )
    local_commit = local_head.stdout.strip() if local_head.returncode == 0 else ""
    remote_cwd = _remap_path(local_cwd, remote_host)
    r_status = _remote_run(
        ["git", "-C", remote_cwd, "status", "--porcelain"],
        host=remote_host,
        capture_output=True,
        text=True,
        timeout=bridge.TIMEOUT_REMOTE_CMD,
    )
    remote_changed = bool(r_status.stdout.strip()) if r_status.returncode == 0 else False
    r_head = _remote_run(
        ["git", "-C", remote_cwd, "rev-parse", "HEAD"],
        host=remote_host,
        capture_output=True,
        text=True,
        timeout=bridge.TIMEOUT_TMUX_SEND,
    )
    remote_commit = r_head.stdout.strip() if r_head.returncode == 0 else ""
    if local_changed and remote_changed:
        local_files = [l.strip().split(None, 1)[-1] for l in local_status.stdout.strip().splitlines()[:5]]
        remote_files = [l.strip().split(None, 1)[-1] for l in r_status.stdout.strip().splitlines()[:5]]
        conflicts.append(f"VPS has uncommitted changes: {', '.join(local_files)}")
        conflicts.append(f"Remote has uncommitted changes: {', '.join(remote_files)}")
    elif local_commit and remote_commit and local_commit != remote_commit:
        if local_changed:
            conflicts.append(f"VPS has uncommitted changes AND different commit than remote")
        elif remote_changed:
            conflicts.append(f"VPS has new commits since teleport (HEAD: {local_commit[:8]})")
            conflicts.append(f"Remote has uncommitted changes (HEAD: {remote_commit[:8]})")
    return conflicts


def run_teleport_preflight(target_host: str, worker_name: str, backend_name: str) -> list[str]:
    import bridge

    fails = []
    preflight_dirs = [
        os.path.expanduser("~/agent-config/teleport-preflight.d"),
        os.path.expanduser("~/.config/claudecode-telegram/teleport-preflight.d"),
    ]
    env = os.environ.copy()
    env["TARGET_HOST"] = target_host or ""
    env["WORKER_NAME"] = worker_name
    env["BACKEND"] = backend_name
    env["BRIDGE_URL"] = bridge.BRIDGE_PUBLIC_URL or bridge.BRIDGE_URL
    seen_scripts: set[str] = set()
    for pdir in preflight_dirs:
        if not os.path.isdir(pdir):
            continue
        scripts = sorted(f for f in os.listdir(pdir) if f.endswith(".sh") and os.access(os.path.join(pdir, f), os.X_OK))
        for script in scripts:
            script_path = os.path.join(pdir, script)
            real_path = os.path.realpath(script_path)
            if real_path in seen_scripts:
                continue
            seen_scripts.add(real_path)
            try:
                r = _subprocess_runner.run(
                    [script_path], env=env, capture_output=True, text=True, timeout=bridge.TIMEOUT_REMOTE_CMD
                )
                if r.returncode != 0:
                    reason = r.stdout.strip().split("\n")[0] if r.stdout.strip() else f"{script} failed"
                    fails.append(reason)
            except subprocess.TimeoutExpired:
                fails.append(f"{script} timed out")
            except (subprocess.SubprocessError, OSError) as e:
                fails.append(f"{script} error: {e}")
    return fails


def sync_working_directory(
    source_cwd: str, target_cwd: str, source_host: str | None = None, target_host: str | None = None, full: bool = False
) -> bool:
    import bridge

    if not full and _is_git_repo(source_cwd, host=source_host):
        project = _get_project_name(source_cwd, host=source_host)
        if project:
            try:
                bare_repo = _ensure_bare_repo(project)
                meta = _git_push_state(source_cwd, project, bare_repo, host=source_host)
                if meta:
                    bare_url = _bare_repo_url(bare_repo, target_host=target_host)
                    if _git_pull_state(target_cwd, project, bare_url, meta, host=target_host):
                        _log(_LOG_INFO, "teleport", f"git sync succeeded for {project}")
                        return True
                    _log(_LOG_WARN, "teleport", f"git pull failed, falling back to rsync")
                else:
                    _log(_LOG_WARN, "teleport", f"git push failed, falling back to rsync")
            except (subprocess.SubprocessError, OSError, KeyError) as e:
                _log(_LOG_ERROR, "teleport", f"git sync error, falling back to rsync: {e}")
    return rsync_working_directory(source_cwd, target_cwd, source_host, target_host, full)


def rsync_working_directory(
    source_cwd: str, target_cwd: str, source_host: str | None = None, target_host: str | None = None, full: bool = False
) -> bool:
    import bridge

    _remote_run(["mkdir", "-p", target_cwd], host=target_host, capture_output=True)
    cmd = ["rsync", "-az", "--delete"]
    gitignore_tmpfile = None
    if not full:
        try:
            gi_result = _remote_run(
                ["git", "-C", source_cwd, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
                host=source_host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_FILE_TRANSFER,
            )
            if gi_result.returncode == 0 and gi_result.stdout.strip():
                fd, gitignore_tmpfile = tempfile.mkstemp(prefix="rsync-gitignore-", suffix=".txt")
                try:
                    os.write(fd, gi_result.stdout.encode())
                finally:
                    os.close(fd)
                cmd.extend(["--exclude-from", gitignore_tmpfile])
        except (ConnectionError, TimeoutError, OSError) as e:
            _log(_LOG_WARN, "teleport", f"git ls-files failed, skipping gitignore excludes: {e}")
        for excl in bridge.TELEPORT_RSYNC_EXCLUDES:
            cmd.extend(["--exclude", excl])
    src = source_cwd.rstrip("/") + "/"
    dst = target_cwd.rstrip("/") + "/"
    if source_host:
        cmd.extend([f"{source_host}:{src}", dst])
    elif target_host:
        cmd.extend([src, f"{target_host}:{dst}"])
    else:
        cmd.extend([src, dst])
    try:
        r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=bridge.TIMEOUT_FULL_SYNC)
        if r.returncode != 0:
            _log(_LOG_WARN, "teleport", f"rsync failed: cmd={cmd} rc={r.returncode} stderr={r.stderr[:500]}")
        return r.returncode == 0
    finally:
        if gitignore_tmpfile and os.path.exists(gitignore_tmpfile):
            os.unlink(gitignore_tmpfile)


def sync_session_transcript(
    session_id: str, source_cwd: str, target_cwd: str, source_host: str | None = None, target_host: str | None = None
) -> None:
    import bridge

    if not session_id:
        return
    source_slug = _project_slug(source_cwd)
    target_slug = _project_slug(target_cwd)
    source_dir = f".claude/projects/{source_slug}"
    target_dir = f".claude/projects/{target_slug}"
    if target_host:
        _remote_run(["bash", "-c", f"mkdir -p $HOME/{target_dir}"], host=target_host, capture_output=True)
    else:
        os.makedirs(os.path.expanduser(f"~/{target_dir}"), exist_ok=True)
    jsonl = f"{session_id}.jsonl"
    for item in [jsonl, f"{session_id}/"]:
        if source_host:
            src_path = f"~/{source_dir}/{item}"
            local_dst = os.path.expanduser(f"~/{target_dir}/")
            cmd = ["rsync", "-az", f"{source_host}:{src_path}", local_dst]
        elif target_host:
            local_src = os.path.expanduser(f"~/{source_dir}/{item}")
            dst_path = f"~/{target_dir}/"
            cmd = ["rsync", "-az", local_src, f"{target_host}:{dst_path}"]
        else:
            local_src = os.path.expanduser(f"~/{source_dir}/{item}")
            local_dst = os.path.expanduser(f"~/{target_dir}/")
            cmd = ["rsync", "-az", local_src, local_dst]
        r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=bridge.TIMEOUT_RSYNC)
        if r.returncode != 0:
            _log(_LOG_WARN, "teleport", f"transcript sync failed for {item}: {r.stderr[:200]}")


def sync_shared_repos(target_host: str, chat_id: "int | str | None" = None) -> list[str]:
    import bridge

    if not target_host:
        return []
    warnings: list[str] = []

    def _warn(msg: str) -> None:
        warnings.append(msg)
        _log(_LOG_INFO, "teleport", msg)

    try:
        home = os.path.expanduser("~")
        git_repos = {"team": os.path.join(home, "team"), "agent-config": os.path.join(home, "agent-config")}
        for rn, rp in git_repos.items():
            if os.path.isdir(os.path.join(rp, ".git")):
                try:
                    for cmd in [
                        ["git", "-C", rp, "add", "-A"],
                        ["git", "-C", rp, "commit", "-m", f"teleport sync: {rn}"],
                    ]:
                        _subprocess_runner.run(cmd, capture_output=True, timeout=bridge.TIMEOUT_REMOTE_CMD)
                    _subprocess_runner.run(
                        ["git", "-C", rp, "push", "origin", "master"],
                        capture_output=True,
                        timeout=bridge.TIMEOUT_LARGE_TRANSFER,
                    )
                except (subprocess.SubprocessError, OSError) as e:
                    _warn(f"git push {rn}: {e}")
        for rn in git_repos:
            try:
                _remote_run(
                    ["bash", "-c", f"cd ~/{rn} 2>/dev/null && git pull origin master 2>/dev/null || true"],
                    host=target_host,
                    capture_output=True,
                    timeout=bridge.TIMEOUT_LARGE_TRANSFER,
                )
            except (subprocess.SubprocessError, OSError) as e:
                _warn(f"git pull {rn} on {target_host}: {e}")
        for subdir in ("skills", "hooks", "scripts"):
            try:
                _remote_run(
                    [
                        "bash",
                        "-c",
                        f"[ -d ~/agent-config/.claude/{subdir} ] && rsync -az --checksum ~/agent-config/.claude/{subdir}/ ~/.claude/{subdir}/",
                    ],
                    host=target_host,
                    capture_output=True,
                    timeout=bridge.TIMEOUT_LARGE_TRANSFER,
                )
            except (subprocess.SubprocessError, OSError) as e:
                _warn(f"agent-config deploy {subdir}: {e}")
        remote_home = _get_remote_home(target_host)
        if remote_home and remote_home != home:
            settings_src = os.path.expanduser("~/.claude/settings.json")
            if os.path.exists(settings_src):
                with open(settings_src) as f:
                    settings_text = f.read().replace(home, remote_home)
                fd, tmp = tempfile.mkstemp(suffix=".json")
                try:
                    os.write(fd, settings_text.encode())
                finally:
                    os.close(fd)
                try:
                    _subprocess_runner.run(
                        ["rsync", "-az", tmp, f"{target_host}:.claude/settings.json"],
                        capture_output=True,
                        timeout=bridge.TIMEOUT_REMOTE_CMD,
                    )
                finally:
                    os.unlink(tmp)
    except (subprocess.SubprocessError, OSError) as e:
        _warn(f"shared repo sync: {e}")
    return warnings


def sync_worker_data_back(name: str, source_host: str | None) -> None:
    import bridge

    if not source_host:
        return
    home = os.path.expanduser("~")
    worker_team_dir = os.path.join(home, "team", name)
    if os.path.isdir(worker_team_dir):
        _subprocess_runner.run(
            ["rsync", "-az", f"{source_host}:team/{name}/", f"{worker_team_dir}/"],
            capture_output=True,
            timeout=bridge.TIMEOUT_GIT_OP,
        )
    r = _remote_run(
        ["bash", "-c", "find ~/.claude/projects/*/memory -name '*.md' 2>/dev/null | head -50"],
        host=source_host,
        capture_output=True,
        text=True,
        timeout=bridge.TIMEOUT_REMOTE_CMD,
    )
    if r.returncode == 0 and r.stdout.strip():
        for remote_file in r.stdout.strip().splitlines():
            remote_home = _get_remote_home(source_host)
            if remote_home and remote_file.startswith(remote_home):
                local_file = home + remote_file[len(remote_home) :]
                local_dir = os.path.dirname(local_file)
                os.makedirs(local_dir, exist_ok=True)
                _subprocess_runner.run(
                    ["rsync", "-az", f"{source_host}:{remote_file}", local_file],
                    capture_output=True,
                    timeout=bridge.TIMEOUT_REMOTE_CMD,
                )


def install_hooks_on_target(target_host: str) -> list[str]:
    import bridge

    if not target_host:
        return []
    warnings = []
    try:
        _remote_run(["chmod", "-R", "700", ".claude/hooks"], host=target_host, capture_output=True)
        claude_json = os.path.expanduser("~/.claude.json")
        if os.path.exists(claude_json):
            _subprocess_runner.run(
                ["rsync", "-az", claude_json, f"{target_host}:.claude.json"],
                capture_output=True,
                timeout=bridge.TIMEOUT_REMOTE_CMD,
            )
        else:
            _remote_run(
                [
                    "python3",
                    "-c",
                    "import json,os,pathlib;"
                    'p=pathlib.Path(os.path.expanduser("~/.claude.json"));'
                    "d=json.loads(p.read_text()) if p.exists() else {};"
                    'd["hasCompletedOnboarding"]=True;'
                    'd.setdefault("numStartups",1);'
                    "p.write_text(json.dumps(d))",
                ],
                host=target_host,
                capture_output=True,
                timeout=bridge.TIMEOUT_REMOTE_CMD,
            )
    except (subprocess.SubprocessError, OSError) as e:
        w = f"hook install: {e}"
        warnings.append(w)
        _log(_LOG_INFO, "teleport", f"{w}")
    return warnings


def sync_session_files_to_target(name: str, target_sessions_dir: str, target_host: str) -> None:
    import bridge

    local_session_dir = bridge.SESSIONS_DIR / name
    if not local_session_dir.is_dir():
        return
    remote_session_dir = f"{target_sessions_dir}/{name}"
    _remote_run(["mkdir", "-p", remote_session_dir], host=target_host, capture_output=True)
    for fname in ["chat_id", "claude_session_id"]:
        local_file = local_session_dir / fname
        if local_file.exists():
            _subprocess_runner.run(
                ["rsync", "-az", str(local_file), f"{target_host}:{remote_session_dir}/{fname}"],
                capture_output=True,
                timeout=bridge.TIMEOUT_REMOTE_CMD,
            )


def sync_credentials_to_target(target_host: str) -> None:
    import bridge

    local_creds = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.exists(local_creds):
        return
    try:
        r = _remote_run(
            ["cat", ".claude/.credentials.json"],
            host=target_host,
            capture_output=True,
            text=True,
            timeout=bridge.TIMEOUT_TMUX_SEND,
        )
        if r.returncode == 0 and r.stdout.strip():
            remote_data = cast(dict[str, object], json.loads(r.stdout))
            local_data = cast(dict[str, object], json.loads(Path(local_creds).read_text()))
            _ro = remote_data.get("claudeAiOauth", {})
            remote_oauth = _ro if isinstance(_ro, dict) else {}
            _lo = local_data.get("claudeAiOauth", {})
            local_oauth = _lo if isinstance(_lo, dict) else {}
            remote_refresh = str(remote_oauth.get("refreshToken", ""))
            local_refresh = str(local_oauth.get("refreshToken", ""))
            remote_exp = int(remote_oauth.get("expiresAt", 0))
            now_ms = int(_clock.time() * 1000)
            if remote_refresh and remote_refresh != local_refresh and remote_exp > now_ms:
                _log(_LOG_INFO, "creds", f"Target {target_host} has independent valid credentials, skipping sync")
                return
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, subprocess.SubprocessError, OSError):
        pass
    _remote_run(["mkdir", "-p", ".claude"], host=target_host, capture_output=True)
    _subprocess_runner.run(
        ["rsync", "-az", local_creds, f"{target_host}:.claude/.credentials.json.tmp"],
        capture_output=True,
        timeout=bridge.TIMEOUT_REMOTE_CMD,
    )
    _remote_run(
        ["mv", ".claude/.credentials.json.tmp", ".claude/.credentials.json"], host=target_host, capture_output=True
    )
    _remote_run(["chmod", "600", ".claude/.credentials.json"], host=target_host, capture_output=True)
    _log(_LOG_INFO, "creds", f"Synced credentials to {target_host}")


def stop_worker_for_teleport(name: str, tmux_name: str, host: str | None = None) -> str | None:
    import bridge

    session_id = get_claude_session_id(name, authoritative=True)
    _remote_run(["tmux", "send-keys", "-t", tmux_name, "/exit", "Enter"], host=host, capture_output=True)
    for _ in range(20):
        _clock.sleep(bridge.DELAY_RETRY)
        r = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"], host=host, capture_output=True, text=True
        )
        if r.returncode != 0:
            break
        pane_pid = r.stdout.strip()
        if pane_pid:
            claude_pid = _get_claude_pid(pane_pid, host=host)
            if not claude_pid:
                break
    else:
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "C-c", ""], host=host, capture_output=True)
        _clock.sleep(bridge.DELAY_STARTUP)
    return get_claude_session_id(name, authoritative=True) or session_id


def start_worker_on_target(
    name: str,
    target_host: str,
    target_cwd: str | None,
    session_id: str | None,
    backend_name: str,
    skip_session_sync: bool = False,
) -> bool:
    import bridge

    tmux_name = f"{bridge.TMUX_PREFIX}{name}"
    _remote_run(["tmux", "kill-session", "-t", tmux_name], host=target_host, capture_output=True)
    _clock.sleep(bridge.DELAY_TMUX_SEND)
    r = _remote_run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
        host=target_host,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        _log(
            _LOG_WARN,
            "teleport",
            f"tmux new-session failed: rc={r.returncode} stderr={r.stderr[:200] if r.stderr else ''}",
        )
        return False
    _remote_run(["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], host=target_host, capture_output=True)
    _clock.sleep(bridge.DELAY_RETRY)
    target_sessions_dir = _remap_path(str(bridge.SESSIONS_DIR), target_host)
    if target_host and not skip_session_sync:
        sync_session_files_to_target(name, target_sessions_dir, target_host)
    if target_host:
        sync_credentials_to_target(target_host)
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
        host=target_host,
        capture_output=True,
    )
    _clock.sleep(bridge.DELAY_TMUX_SEND)
    backend = get_backend(backend_name)
    cli_cmd = backend.start_cmd(session_id or "")
    if target_host:
        id_result = _remote_run(["id", "-u"], host=target_host, capture_output=True, text=True)
        if id_result.returncode == 0 and id_result.stdout.strip() == "0":
            cli_cmd = cli_cmd.replace(" --dangerously-skip-permissions", "")
    start_cmd = f"unset CLAUDECODE && {cli_cmd}"
    if target_cwd:
        start_cmd = f"cd {shlex.quote(target_cwd)} && {start_cmd}"
    _log(_LOG_INFO, "teleport", f"start_cmd={start_cmd}")
    _remote_run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], host=target_host, capture_output=True)
    if backend.is_interactive:
        for delay, key in [(3.0, "Enter"), (2.0, "Enter"), (1.0, "Enter"), (1.0, "Enter")]:
            _clock.sleep(delay)
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key], host=target_host, capture_output=True)
    for attempt in range(30):
        _clock.sleep(bridge.DELAY_STARTUP)
        r = _remote_run(
            ["tmux", "display-message", "-t", tmux_name, "-p", "#{pane_pid}"],
            host=target_host,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            if attempt % 10 == 0:
                _log(_LOG_WARN, "teleport", f"verify attempt {attempt}: tmux display-message failed rc={r.returncode}")
            continue
        pane_pid = r.stdout.strip()
        claude_pid = _get_claude_pid(pane_pid, host=target_host) if pane_pid else None
        if claude_pid:
            _log(_LOG_INFO, "teleport", f"verified: claude running as pid {claude_pid} (pane {pane_pid})")
            return True
        if attempt % 10 == 0:
            _log(_LOG_INFO, "teleport", f"verify attempt {attempt}: pane_pid={pane_pid}, no claude yet")
            cap = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=target_host,
                capture_output=True,
                text=True,
                timeout=bridge.TIMEOUT_TMUX_SEND,
            )
            if cap.returncode == 0:
                _log(_LOG_INFO, "teleport", f"pane content: {cap.stdout[:300]}")
    _log(_LOG_WARN, "teleport", f"verify FAILED after 30 attempts")
    return False


def teleport_rollback(
    name: str,
    tmux_name: str,
    source_host: str | None,
    source_cwd: str | None,
    session_id: str | None,
    backend_name: str,
    chat_id: "ChatId | None",
    reason: str,
) -> None:
    import bridge

    teleport_notify(chat_id, f"Teleport failed: {reason}. Rolling back...")
    try:
        if source_cwd:
            save_claude_session_cwd(name, source_cwd)
        if not tmux_exists(tmux_name, host=source_host):
            _remote_run(
                ["tmux", "new-session", "-d", "-s", tmux_name, "-x", "200", "-y", "50"],
                host=source_host,
                capture_output=True,
            )
            _remote_run(
                ["tmux", "set-option", "-t", tmux_name, "window-size", "manual"], host=source_host, capture_output=True
            )
        backend = get_backend(backend_name)
        start_cmd = f'unset CLAUDECODE && {backend.start_cmd(session_id or "")}'
        if source_cwd:
            start_cmd = f"cd {shlex.quote(source_cwd)} && {start_cmd}"
        _remote_run(["tmux", "send-keys", "-t", tmux_name, start_cmd, "Enter"], host=source_host, capture_output=True)
        teleport_notify(chat_id, f"{name} restarted on source. Teleport cancelled.")
    except (subprocess.SubprocessError, OSError) as e:
        teleport_notify(chat_id, f"Rollback also failed: {e}")
    try:
        state_file = bridge.SESSIONS_DIR / name / "teleport_state"
        state_file.unlink(missing_ok=True)
    except OSError as exc:
        _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
