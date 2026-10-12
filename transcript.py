#!/usr/bin/env python3
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, TypedDict, cast
from core import (
    _log,
    _LOG_ERROR,
    _LOG_WARN,
    _LOG_DEBUG,
    _subprocess_runner,
    _clock,
    _int_field,
)
from claudecode import (
    _remote_run,
    _remap_path,
    _project_slug,
    _get_remote_home,
    normalize_cwd,
    AuthorDetection,
)
from _types import (
    TranscriptSyncState,
    TranscriptMessageUsage,
    TranscriptMessageContent,
    TranscriptMessage,
    TranscriptEntry,
    TranscriptStatsDict,
    ToolResultDict,
)


def get_worker_host(name: str) -> str | None:
    import bridge as _b

    return _b.get_worker_host(name)  # noqa: E402


def get_claude_session_cwd(name: str) -> str | None:
    import bridge as _b

    return _b.get_claude_session_cwd(name)  # noqa: E402


def get_claude_session_id(name: str, authoritative: bool = False) -> str:
    import bridge as _b

    return _b.get_claude_session_id(name, authoritative=authoritative)  # noqa: E402


_TEMPLATE_DIR = Path(__file__).parent / "templates"


class TranscriptSyncRegistry:
    def __init__(self) -> None:
        self._state: dict[str, TranscriptSyncState] = {}
        self._lock: threading.Lock = threading.Lock()

    def get(self, key: str) -> TranscriptSyncState | None:
        with self._lock:
            return self._state.get(key)

    def set(self, key: str, value: TranscriptSyncState) -> None:
        with self._lock:
            self._state[key] = value

    def update(self, key: str, **fields: object) -> None:
        with self._lock:
            if key in self._state:
                self._state[key].update(fields)  # type: ignore[typeddict-item]

    def get_started(self, key: str) -> float:
        with self._lock:
            entry = self._state.get(key, {})
            return entry.get("started", 0)


_transcript_sync = TranscriptSyncRegistry()
INDEXER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indexer.py")
from _transcript_render import *  # noqa: F401,F403
from _transcript_render import (  # explicit re-exports for type-checkers
    _TEAM_MEMBERS,
    _MANAGER_AV,
    _render_transcript_html,
    _render_transcript_loading,
    _err_page,
    _generate_member_avatar,
    _detect_message_author,
    _transcript_entry_to_html,
    _format_model_name,
    _transcript_stats,
    _transcript_html_head,
    _transcript_html_nav,
    _transcript_html_entries,
    _transcript_html_footer,
    _transcript_html_search_js,
)


def _run_transcript_query(
    jsonl_path: str,
    sid: str,
    query: str,
    host: str | None = None,
    *,
    page: int | None = None,
    per_page: int | None = None,
    search: str | None = None,
    filter_mode: str | None = None,
    sort: str | None = None,
) -> dict[str, object] | None:
    import bridge as _b  # late import to avoid circular dependency

    db_path = f"/tmp/transcript-cache/{sid}.db"
    script_path = INDEXER_SCRIPT
    if host:
        remote_home = _get_remote_home(host) or ""
        if remote_home:
            script_path = f"{remote_home}/claudecode-telegram/indexer.py"
    cmd = ["python3", script_path, "transcript", "--jsonl", str(jsonl_path), "--db", db_path, "--query", query]
    if page is not None:
        cmd.extend(["--page", str(page)])
    if per_page is not None:
        cmd.extend(["--per-page", str(per_page)])
    if search:
        cmd.extend(["--search", search])
    if filter_mode:
        cmd.extend(["--filter", filter_mode])
    if sort and sort != "relevance":
        cmd.extend(["--sort", sort])
    try:
        if host:
            r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=_b.TIMEOUT_LARGE_TRANSFER)
        else:
            r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=_b.TIMEOUT_GIT_OP)
        if r.returncode == 0 and r.stdout.strip():
            return cast(dict[str, object], json.loads(r.stdout))
    except (subprocess.SubprocessError, OSError) as e:
        _log(_LOG_ERROR, "transcript", f"Transcript query error: {e}")
    return None


def _start_transcript_sync(name: str, host: str, remote_path: str, local_tmp: Path, key: str) -> None:
    import bridge as _b  # late import

    try:
        _transcript_sync.set(
            key,
            {
                "status": "syncing",
                "progress": "Connecting to remote host...",
                "started": _clock.time(),
                "path": None,
                "error": None,
            },
        )
        r = _subprocess_runner.run(
            ["ssh", host, f"stat -f%z '{remote_path}' 2>/dev/null || stat -c%s '{remote_path}' 2>/dev/null"],
            capture_output=True,
            text=True,
            timeout=_b.TIMEOUT_REMOTE_CMD,
        )
        remote_size = 0
        if r.returncode == 0 and r.stdout.strip().isdigit():
            remote_size = int(r.stdout.strip())
        if remote_size > 0:
            size_mb = remote_size / 1_048_576
            _transcript_sync.update(key, progress=f"Syncing transcript ({size_mb:.1f} MB)...", remote_size=remote_size)
        else:
            _transcript_sync.update(key, progress="Syncing transcript...")
        proc = _subprocess_runner.popen(
            ["rsync", "-az", f"{host}:{remote_path}", str(local_tmp)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            while proc.poll() is None:
                _clock.sleep(_b.DELAY_STARTUP)
                try:
                    if local_tmp.exists() and remote_size > 0:
                        local_size = local_tmp.stat().st_size
                        pct = min(99, int(local_size * 100 / remote_size))
                        _transcript_sync.update(
                            key,
                            progress=f"Syncing... {pct}% ({local_size / 1_048_576:.1f} / {remote_size / 1_048_576:.1f} MB)",
                            pct=pct,
                        )
                except (OSError, ValueError) as exc:
                    _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
            if proc.returncode == 0 and local_tmp.exists() and local_tmp.stat().st_size > 0:
                _transcript_sync.set(
                    key,
                    {
                        "status": "done",
                        "progress": "Ready",
                        "path": str(local_tmp),
                        "started": _transcript_sync.get_started(key),
                        "error": None,
                    },
                )
            else:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                _transcript_sync.set(
                    key,
                    {
                        "status": "error",
                        "progress": "Sync failed",
                        "started": _transcript_sync.get_started(key),
                        "path": None,
                        "error": stderr[:200] or "rsync failed",
                    },
                )
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        _transcript_sync.set(
            key,
            {
                "status": "error",
                "progress": "Sync failed",
                "started": _transcript_sync.get_started(key),
                "path": None,
                "error": str(e)[:200],
            },
        )


def _resolve_transcript_path(name: str, session_id: str | None = None) -> tuple[str | None, str | None, str]:
    import bridge as _b  # late import

    cwd = get_claude_session_cwd(name)
    if not cwd:
        host = get_worker_host(name)
        tmux_name = f"{_b.TMUX_PREFIX}{name}"
        try:
            assert _b.worker_manager is not None
            cwd = normalize_cwd(_b.worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
        except Exception:
            pass
    cwd = cwd or os.path.expanduser("~")
    sid = session_id or get_claude_session_id(name, authoritative=True)
    if not sid:
        return None, "", cwd
    slug = _project_slug(cwd)
    transcript_path = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"
    if not transcript_path.exists():
        reg = _b._load_registry().get("workers", {})
        entry = reg.get(name, {})
        host = entry.get("host")
        if host:
            try:
                remote_home = _get_remote_home(host)
                if remote_home:
                    remote_cwd = _remap_path(cwd, host)
                    remote_slug = _project_slug(remote_cwd)
                    remote_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                    local_tmp = Path(f"/tmp/transcript-{name}-{sid}.jsonl")
                    sync_key = f"{name}:{sid}"
                    sync_info = _transcript_sync.get(sync_key)
                    if sync_info and sync_info["status"] == "done" and local_tmp.exists():
                        transcript_path = local_tmp
                    elif sync_info and sync_info["status"] == "syncing":
                        return "syncing", sid, cwd
                    elif sync_info and sync_info["status"] == "error":
                        err = sync_info.get("error", "unknown error")
                        _log(_LOG_WARN, "transcript", f"sync failed for {name}:{sid}: {err}")
                        return None, sid, cwd
                    else:
                        _b._task_pool.submit(_start_transcript_sync, name, host, remote_path, local_tmp, sync_key)
                        return "syncing", sid, cwd
            except (OSError, subprocess.SubprocessError) as exc:
                _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    if transcript_path.exists():
        return str(transcript_path), sid, cwd
    return None, sid, cwd


def _parse_transcript_entries(transcript_path: str) -> list[TranscriptEntry]:
    entries = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = cast(TranscriptEntry, json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype = entry.get("type", "")
            if etype in ("progress", "queue-operation", "file-history-snapshot"):
                continue
            if etype == "system":
                continue
            entries.append(entry)
    return entries


def handle_transcript_endpoint(handler, parsed) -> None:
    import bridge as _b

    try:
        from urllib.parse import parse_qs

        query_params = parse_qs(parsed.query)
        token = query_params.get("token", [None])[0]
        if not _b.tokens.validate_rewind(token):
            handler._send_html(
                _err_page(
                    "Session Expired",
                    "This link has expired or is invalid. Send <code>/rewind &lt;name&gt;</code> in Telegram to get a fresh 5-minute link.",
                ).encode("utf-8"),
                403,
            )
            return
        parts = parsed.path.rstrip("/").split("/")
        if len(parts) < 3 or not parts[2]:
            handler._send_json(400, {"error": "Usage: /transcript/<worker_name>"})
            return
        name = parts[2]
        if len(parts) >= 4 and parts[3] == "updates":
            query_params = parse_qs(parsed.query)
            since = int(query_params.get("since", [0])[0])
            session_id = query_params.get("sid", [None])[0]
            host = _b.get_worker_host(name)
            if host:
                cwd = _b.get_claude_session_cwd(name) or ""
                sid = session_id or _b.get_claude_session_id(name)
                if not sid:
                    handler._send_json(200, {"total": 0, "new": 0})
                    return
                remote_home = _b._get_remote_home(host) or ""
                remote_cwd = _b._remap_path(cwd, host)
                remote_slug = _b._project_slug(remote_cwd)
                jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
            else:
                _tp, sid, _cwd = _resolve_transcript_path(name, session_id)
                if not _tp or not sid:
                    handler._send_json(200, {"total": 0, "new": 0})
                    return
                jsonl_path = str(_tp)
            result = _run_transcript_query(jsonl_path, sid, "stats", host=host)
            total = 0
            if result:
                total = _b._int_field(result, "n_user") + _b._int_field(result, "n_tool")
                count_result = _run_transcript_query(jsonl_path, sid, "entries", host=host, page=1, per_page=1)
                if count_result:
                    total = _b._int_field(count_result, "total", total)
            new_count = max(0, total - since)
            handler._send_json(200, {"total": total, "new": new_count})
            return
        query_params = parse_qs(parsed.query)
        session_id = query_params.get("sid", [None])[0]
        page_raw = query_params.get("page", [None])[0]
        try:
            page = max(1, int(page_raw)) if page_raw is not None else None
        except (ValueError, TypeError):
            page = None
        try:
            per_page = max(1, min(500, int(query_params.get("per_page", [50])[0])))
        except (ValueError, TypeError):
            per_page = 50
        search_query = query_params.get("q", [""])[0].strip()
        search_sort = query_params.get("sort", ["relevance"])[0].strip()
        if search_sort not in ("relevance", "time"):
            search_sort = "relevance"
        filter_mode = query_params.get("filter", [""])[0].strip()
        host = _b.get_worker_host(name)
        if not host:
            _tp, _sid, _cwd = _resolve_transcript_path(name, session_id)
            if _tp == "syncing":
                sync_key = f"{name}:{_sid}"
                handler._send_html(_render_transcript_loading(name, _sid, token or "", sync_key).encode("utf-8"))
                return
        html_content = _render_transcript_html(
            name,
            session_id=session_id,
            page=page,
            per_page=per_page,
            search_query=search_query,
            token=token or "",
            filter_mode=filter_mode,
            search_sort=search_sort,
        )
        handler._send_html(html_content.encode("utf-8"))
    except (OSError, ValueError, KeyError) as e:
        _b._log(_b._LOG_ERROR, "transcript", f"Transcript endpoint error: {e}", exc=e)
        handler._send_text(500, str(e))
