#!/usr/bin/env python3
"""Transcript rendering and sync — extracted from bridge.py."""
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, TypedDict, cast

from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_DEBUG,
    _subprocess_runner, _clock, _int_field,
)
from claudecode import (
    _remote_run, _remap_path, _project_slug, _get_remote_home,
    normalize_cwd, AuthorDetection,
)

# These are accessed via late-import from bridge so tests can patch bridge.X
def get_worker_host(name: str) -> str | None:
    import bridge as _b; return _b.get_worker_host(name)  # noqa: E402
def get_claude_session_cwd(name: str) -> str | None:
    import bridge as _b; return _b.get_claude_session_cwd(name)  # noqa: E402
def get_claude_session_id(name: str, authoritative: bool = False) -> str:
    import bridge as _b; return _b.get_claude_session_id(name, authoritative=authoritative)  # noqa: E402

# ---------------------------------------------------------------------------
# TypedDicts
# ---------------------------------------------------------------------------
class TranscriptSyncState(TypedDict, total=False):
    status: str; progress: str; error: str | None; path: str | None; started: float; pct: int; remote_size: int
class TranscriptMessageUsage(TypedDict, total=False):
    input_tokens: int; output_tokens: int; cache_read_input_tokens: int; cache_creation_input_tokens: int
class TranscriptMessageContent(TypedDict, total=False):
    type: str; text: str; name: str; id: str; input: dict[str, str]; content: str | list[object]; is_error: bool
class TranscriptMessage(TypedDict, total=False):
    role: str; content: str | list[TranscriptMessageContent]; model: str; usage: TranscriptMessageUsage
class TranscriptEntry(TypedDict, total=False):
    type: str; message: TranscriptMessage; timestamp: str; version: str; gitBranch: str; _idx: int
class TranscriptStatsDict(TypedDict):
    n_user: int; n_tool: int; n_edit: int; lines_add: int; lines_del: int; lines_mod: int; n_files: int; model: str; version: str
    git_branch: str; first_ts: str; last_ts: str; input_tokens: int; output_tokens: int; duration: str
class ToolResultDict(TypedDict, total=False):
    content: str; is_error: bool

# ---------------------------------------------------------------------------
# Template assets
# ---------------------------------------------------------------------------
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TRANSCRIPT_CSS = (_TEMPLATE_DIR / "transcript.css").read_text() if (_TEMPLATE_DIR / "transcript.css").exists() else ""
_TRANSCRIPT_JS = (_TEMPLATE_DIR / "transcript.js").read_text() if (_TEMPLATE_DIR / "transcript.js").exists() else ""

# ---------------------------------------------------------------------------
# Sync registry
# ---------------------------------------------------------------------------
class TranscriptSyncRegistry:
    def __init__(self) -> None:
        self._state: dict[str, TranscriptSyncState] = {}
        self._lock: threading.Lock = threading.Lock()
    def get(self, key: str) -> TranscriptSyncState | None:
        with self._lock: return self._state.get(key)
    def set(self, key: str, value: TranscriptSyncState) -> None:
        with self._lock: self._state[key] = value
    def update(self, key: str, **fields: object) -> None:
        with self._lock:
            if key in self._state: self._state[key].update(fields)  # type: ignore[typeddict-item]
    def get_started(self, key: str) -> float:
        with self._lock:
            entry = self._state.get(key, {})
            return entry.get("started", 0)
_transcript_sync = TranscriptSyncRegistry()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INDEXER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indexer.py")

_TRANSCRIPT_CHEVRON_SVG = '<svg class="chev" viewBox="0 0 16 16" fill="currentColor"><path d="M6.22 3.22a.75.75 0 011.06 0l4.25 4.25a.75.75 0 010 1.06l-4.25 4.25a.75.75 0 01-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 010-1.06z"/></svg>'
_TRANSCRIPT_CLAUDE_AVATAR = '<div class="cl-av"><svg viewBox="0 0 24 24" fill="none"><path d="M16.98 5.35L12 2L7.02 5.35L1.28 6.35L3.28 12.1L1.28 17.85L7.02 18.85L12 22.2L16.98 18.85L22.72 17.85L20.72 12.1L22.72 6.35L16.98 5.35Z" fill="currentColor"/></svg></div>'
_TRANSCRIPT_TOOL_SVGS: dict[str, str] = {
    "Read": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 1.5a.25.25 0 00-.25.25v11.5c0 .138.112.25.25.25h8.5a.25.25 0 00.25-.25V6H9.75A1.75 1.75 0 018 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.086c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v8.086A1.75 1.75 0 0112.25 15h-8.5A1.75 1.75 0 012 13.25V1.75z"/></svg>',
    "Write": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M3.75 1.5a.25.25 0 00-.25.25v11.5c0 .138.112.25.25.25h8.5a.25.25 0 00.25-.25V6H9.75A1.75 1.75 0 018 4.25V1.5H3.75zm5.75.56v2.19c0 .138.112.25.25.25h2.19L9.5 2.06zM2 1.75C2 .784 2.784 0 3.75 0h5.086c.464 0 .909.184 1.237.513l3.414 3.414c.329.328.513.773.513 1.237v8.086A1.75 1.75 0 0112.25 15h-8.5A1.75 1.75 0 012 13.25V1.75z"/></svg>',
    "Edit": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M11.013 1.427a1.75 1.75 0 012.474 0l1.086 1.086a1.75 1.75 0 010 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 01-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61zm1.414 1.06a.25.25 0 00-.354 0L10.811 3.75l1.439 1.44 1.263-1.263a.25.25 0 000-.354l-1.086-1.086zM11.189 6.25L9.75 4.811l-6.286 6.287a.25.25 0 00-.064.108l-.558 1.953 1.953-.558a.249.249 0 00.108-.064l6.286-6.287z"/></svg>',
    "Bash": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0114.25 15H1.75A1.75 1.75 0 010 13.25V2.75zm1.75-.25a.25.25 0 00-.25.25v10.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25V2.75a.25.25 0 00-.25-.25H1.75zM7.25 8a.75.75 0 01-.22.53l-2.25 2.25a.75.75 0 11-1.06-1.06L5.44 8 3.72 6.28a.75.75 0 111.06-1.06l2.25 2.25c.141.14.22.331.22.53zm1.5 1.5a.75.75 0 000 1.5h3a.75.75 0 000-1.5h-3z"/></svg>',
    "Grep": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M10.68 11.74a6 6 0 01-7.922-8.982 6 6 0 018.982 7.922l3.04 3.04a.749.749 0 01-.326 1.275.749.749 0 01-.734-.215l-3.04-3.04zM11.5 7a4.499 4.499 0 10-8.997 0A4.499 4.499 0 0011.5 7z"/></svg>',
    "Glob": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1A1.75 1.75 0 000 2.75v10.5C0 14.216.784 15 1.75 15h12.5A1.75 1.75 0 0016 13.25v-8.5A1.75 1.75 0 0014.25 3H7.5a.25.25 0 01-.2-.1l-.9-1.2C6.07 1.26 5.55 1 5 1H1.75z"/></svg>',
    "Agent": '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M6.5.75a.75.75 0 00-1.5 0V2H3.75A1.75 1.75 0 002 3.75V5h-.25a.75.75 0 000 1.5H2v3h-.25a.75.75 0 000 1.5H2v1.25c0 .966.784 1.75 1.75 1.75h8.5A1.75 1.75 0 0014 12.25V11h.25a.75.75 0 000-1.5H14v-3h.25a.75.75 0 000-1.5H14V3.75A1.75 1.75 0 0012.25 2H11V.75a.75.75 0 00-1.5 0V2h-3V.75z"/></svg>',
}
_TRANSCRIPT_DEFAULT_TOOL_SVG = '<svg class="t-icon" viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304zM10.567 2.304A4.49 4.49 0 0112.5 6c0 1.598-.832 3.002-2.09 3.802-.518.328-.929.923-.902 1.64v.008l.164 3.337a.75.75 0 101.498-.073l-.163-3.34c-.007-.14.1-.313.357-.476A5.994 5.994 0 0114 6c0-2.033-1.01-3.83-2.555-4.916a1.89 1.89 0 00-.878 1.22z"/></svg>'

_TEAM_MEMBERS = {
    "chen", "geni", "hiro", "jin", "kai", "kelvin", "kenji",
    "lee", "luck", "mon", "noa", "ren", "ryo", "sora", "taro",
    "x", "yuki", "finn", }
_MANAGER_AV = '<div class="u-av"><img src="https://avatars.githubusercontent.com/u/4256921" alt="manager"></div>'

_LOADING_CSS = "body{font-family:-apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#0b0d0b;color:#e5e5e0}.card{text-align:center;max-width:420px;padding:40px;width:100%}h1{font-size:1.3rem;margin-bottom:8px;font-weight:600}.sub{color:#878b86;font-size:.9rem;margin-bottom:24px}.bar{background:#1a1c1a;border-radius:6px;height:8px;overflow:hidden;margin:16px 0}.bar-fill{background:#22c55e;height:100%;border-radius:6px;transition:width .5s ease}.bar-fill.err{background:#ef4444}.progress{color:#a0a4a0;font-size:.85rem;margin:8px 0}.elapsed{color:#5a5e5a;font-size:.8rem;margin-top:4px}.err-msg{color:#ef4444;font-size:.85rem;margin-top:12px}.spinner{display:inline-block;width:20px;height:20px;border:2px solid #2a2c2a;border-top-color:#22c55e;border-radius:50%;animation:spin 1s linear infinite;vertical-align:middle;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}"

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------
def _run_transcript_query(jsonl_path: str, sid: str, query: str,
                          host: str | None = None, *,
                          page: int | None = None,
                          per_page: int | None = None,
                          search: str | None = None,
                          filter_mode: str | None = None,
                          sort: str | None = None) -> dict[str, object] | None:
    import bridge as _b  # late import to avoid circular dependency
    db_path = f"/tmp/transcript-cache/{sid}.db"; script_path = INDEXER_SCRIPT
    if host:
        remote_home = _get_remote_home(host) or ""
        if remote_home: script_path = f"{remote_home}/claudecode-telegram/indexer.py"
    cmd = ["python3", script_path, "transcript", "--jsonl", str(jsonl_path), "--db", db_path, "--query", query]
    if page is not None: cmd.extend(["--page", str(page)])
    if per_page is not None: cmd.extend(["--per-page", str(per_page)])
    if search: cmd.extend(["--search", search])
    if filter_mode: cmd.extend(["--filter", filter_mode])
    if sort and sort != "relevance": cmd.extend(["--sort", sort])
    try:
        if host: r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=_b.TIMEOUT_LARGE_TRANSFER)
        else: r = _subprocess_runner.run(cmd, capture_output=True, text=True, timeout=_b.TIMEOUT_GIT_OP)
        if r.returncode == 0 and r.stdout.strip(): return cast(dict[str, object], json.loads(r.stdout))
    except (subprocess.SubprocessError, OSError) as e: _log(_LOG_ERROR, "transcript", f"Transcript query error: {e}")
    return None
def _start_transcript_sync(name: str, host: str, remote_path: str, local_tmp: Path, key: str) -> None:
    import bridge as _b  # late import
    try:
        _transcript_sync.set(key, {"status": "syncing", "progress": "Connecting to remote host...",
                                   "started": _clock.time(), "path": None, "error": None})
        r = _subprocess_runner.run(["ssh", host, f"stat -f%z '{remote_path}' 2>/dev/null || stat -c%s '{remote_path}' 2>/dev/null"],
                           capture_output=True, text=True, timeout=_b.TIMEOUT_REMOTE_CMD)
        remote_size = 0
        if r.returncode == 0 and r.stdout.strip().isdigit(): remote_size = int(r.stdout.strip())
        if remote_size > 0:
            size_mb = remote_size / 1_048_576
            _transcript_sync.update(key, progress=f"Syncing transcript ({size_mb:.1f} MB)...", remote_size=remote_size)
        else: _transcript_sync.update(key, progress="Syncing transcript...")
        proc = _subprocess_runner.popen(
            ["rsync", "-az", f"{host}:{remote_path}", str(local_tmp)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while proc.poll() is None:
                _clock.sleep(_b.DELAY_STARTUP)
                try:
                    if local_tmp.exists() and remote_size > 0:
                        local_size = local_tmp.stat().st_size; pct = min(99, int(local_size * 100 / remote_size))
                        _transcript_sync.update(key,
                            progress=f"Syncing... {pct}% ({local_size / 1_048_576:.1f} / {remote_size / 1_048_576:.1f} MB)",
                            pct=pct)
                except (OSError, ValueError) as exc: _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
            if proc.returncode == 0 and local_tmp.exists() and local_tmp.stat().st_size > 0:
                _transcript_sync.set(key, {"status": "done", "progress": "Ready", "path": str(local_tmp),
                                           "started": _transcript_sync.get_started(key), "error": None})
            else:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                _transcript_sync.set(key, {"status": "error", "progress": "Sync failed",
                                           "started": _transcript_sync.get_started(key),
                                           "path": None, "error": stderr[:200] or "rsync failed"})
        finally:
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        _transcript_sync.set(key, {"status": "error", "progress": "Sync failed",
                                   "started": _transcript_sync.get_started(key),
                                   "path": None, "error": str(e)[:200]})
def _resolve_transcript_path(name: str, session_id: str | None = None) -> tuple[str | None, str | None, str]:
    import bridge as _b  # late import
    cwd = get_claude_session_cwd(name)
    if not cwd:
        host = get_worker_host(name); tmux_name = f"{_b.TMUX_PREFIX}{name}"
        try:
            assert _b.worker_manager is not None
            cwd = normalize_cwd(_b.worker_manager._get_tmux_pane_cwd(tmux_name, host=host))
        except Exception: pass
    cwd = cwd or os.path.expanduser("~"); sid = session_id or get_claude_session_id(name, authoritative=True)
    if not sid: return None, "", cwd
    slug = _project_slug(cwd); transcript_path = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"
    if not transcript_path.exists():
        reg = _b._load_registry().get("workers", {}); entry = reg.get(name, {}); host = entry.get("host")
        if host:
            try:
                remote_home = _get_remote_home(host)
                if remote_home:
                    remote_cwd = _remap_path(cwd, host)
                    remote_slug = _project_slug(remote_cwd)
                    remote_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
                    local_tmp = Path(f"/tmp/transcript-{name}-{sid}.jsonl"); sync_key = f"{name}:{sid}"
                    sync_info = _transcript_sync.get(sync_key)
                    if sync_info and sync_info["status"] == "done" and local_tmp.exists(): transcript_path = local_tmp
                    elif sync_info and sync_info["status"] == "syncing": return "syncing", sid, cwd
                    elif sync_info and sync_info["status"] == "error":
                        err = sync_info.get("error", "unknown error")
                        _log(_LOG_WARN, "transcript", f"sync failed for {name}:{sid}: {err}")
                        return None, sid, cwd
                    else:
                        _b._task_pool.submit(_start_transcript_sync, name, host, remote_path, local_tmp, sync_key)
                        return "syncing", sid, cwd
            except (OSError, subprocess.SubprocessError) as exc: _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    if transcript_path.exists(): return str(transcript_path), sid, cwd
    return None, sid, cwd
def _parse_transcript_entries(transcript_path: str) -> list[TranscriptEntry]:
    entries = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: entry = cast(TranscriptEntry, json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError): continue
            etype = entry.get("type", "")
            if etype in ("progress", "queue-operation", "file-history-snapshot"): continue
            if etype == "system": continue
            entries.append(entry)
    return entries
def _generate_member_avatar(name: str) -> str:
    name_hash = hash(name) & 0xFFFFFFFF; hue = (name_hash % 36) * 10; sat = 55 + (name_hash >> 6 & 1) * 15
    shape_idx = (name_hash >> 7) % 6; accent_idx = (name_hash >> 10) % 5
    initials = name[:2].upper() if len(name) >= 2 else name.upper(); bg = f"hsl({hue},{sat}%,42%)"
    fg = f"hsl({hue},{max(sat-20,30)}%,75%)"
    shapes = [
        '<circle cx="14" cy="14" r="14"/>',
        '<rect x="1" y="1" width="26" height="26" rx="6"/>',
        '<polygon points="14,0 28,7 28,21 14,28 0,21 0,7"/>',
        '<polygon points="14,1 27,14 14,27 1,14"/>',
        '<polygon points="14,0 28,10 22,28 6,28 0,10"/>',
        '<rect x="0" y="0" width="28" height="28" rx="10"/>', ]
    accents = [
        '',
        f'<circle cx="14" cy="14" r="8" fill="none" stroke="{fg}" stroke-width="1.5" opacity=".3"/>',
        f'<circle cx="7" cy="7" r="2" fill="{fg}" opacity=".2"/><circle cx="21" cy="7" r="2" fill="{fg}" opacity=".2"/>',
        f'<line x1="4" y1="4" x2="24" y2="24" stroke="{fg}" stroke-width="1" opacity=".15"/><line x1="4" y1="24" x2="24" y2="4" stroke="{fg}" stroke-width="1" opacity=".15"/>',
        f'<rect x="4" y="12" width="20" height="4" rx="2" fill="{fg}" opacity=".15"/>', ]
    return (f'<div class="u-av"><svg viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">'
            f'<g fill="{bg}">{shapes[shape_idx]}</g>'
            f'{accents[accent_idx]}'
            f'<text x="14" y="14" text-anchor="middle" dominant-baseline="central" '
            f'fill="#fff" font-family="Inter,system-ui,sans-serif" font-size="11" font-weight="600" '
            f'opacity=".9">{initials}</text></svg></div>')
def _detect_message_author(text: str) -> AuthorDetection:
    stripped = text.strip(); colon_pos = stripped.find(":")
    if 0 < colon_pos <= 10:
        prefix = stripped[:colon_pos].lower().strip(); rest = stripped[colon_pos + 1:].strip()
        if prefix == "manager": return AuthorDetection("manager", _MANAGER_AV, rest or stripped)
        if prefix in _TEAM_MEMBERS: return AuthorDetection(prefix, _generate_member_avatar(prefix), rest or stripped)
    return AuthorDetection("manager", _MANAGER_AV, stripped)
def _transcript_entry_to_html(entry: TranscriptEntry, esc: Callable[[str], str], tool_results: dict[str, ToolResultDict] | None = None) -> str:
    import base64 as _b64
    if tool_results is None: tool_results = {}
    etype = entry.get("type", ""); msg = entry.get("message", {}); role = msg.get("role", "")
    content = msg.get("content", ""); _chev = _TRANSCRIPT_CHEVRON_SVG; _claude_av = _TRANSCRIPT_CLAUDE_AVATAR
    _tool_svgs = _TRANSCRIPT_TOOL_SVGS; _default_tool_svg = _TRANSCRIPT_DEFAULT_TOOL_SVG
    parts: list[str] = []
    ts_raw = entry.get("timestamp", ""); ts_html = ""
    if ts_raw: ts_html = f'<span class="ts" data-ts="{esc(ts_raw)}">{esc(ts_raw[:16].replace("T"," "))}</span>'
    if etype == "user" and role == "user":
        if isinstance(content, str) and content.strip():
            raw = content.strip()
            if raw.startswith("<task-notification>") or raw.startswith("<system-reminder>"): return ""
            _author, _avatar, _display = _detect_message_author(raw)
            text = esc(_display)
            if len(text) > 2000: text = text[:2000] + "\n\n<em>… truncated</em>"
            _name_html = f'<span class="u-name">{esc(_author)}</span>' if _author != "manager" else ""
            parts.append(f'<div class="user-msg">{_avatar}<div class="u-body">{_name_html}<div class="u-text">{text}{ts_html}</div></div></div>')
        elif isinstance(content, list): pass
    elif etype == "assistant" and role == "assistant":
        if isinstance(content, list):
            for item in content:
                ct = str(item.get("type", ""))
                if ct == "thinking":
                    raw_thinking = str(item.get("thinking", ""))
                    if raw_thinking and raw_thinking.strip():
                        tt = esc(raw_thinking[:3000])
                        if len(raw_thinking) > 3000: tt += "\n… truncated"
                        parts.append(f'<details class="think"><summary class="think-h">{_chev} Thinking</summary><div class="think-t">{tt}</div></details>')
                elif ct == "text":
                    text = item.get("text", "")
                    if text and text != "(no content)":
                        b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
                        parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
                elif ct == "tool_use":
                    tn = item.get("name", "?"); ti = item.get("input", {})
                    tool_svg = _tool_svgs.get(tn, _default_tool_svg)
                    _INPUT_KEYS = {"Read": "file_path", "Write": "file_path", "Edit": "file_path",
                                   "Glob": "pattern", "Bash": "command", "Grep": "pattern", "Search": "pattern"}
                    key = _INPUT_KEYS.get(tn)
                    if key: inp = ti.get(key, "")
                    elif tn == "Agent": inp = ti.get("description", "") or str(ti.get("prompt", ""))[:80]
                    else: inp = json.dumps(ti, ensure_ascii=False)[:200]
                    is_fp = tn in ("Read", "Write", "Edit") and inp and "/" in inp
                    inp = str(inp)[:300]; tool_id = item.get("id", ""); tr = tool_results.get(tool_id, {})
                    tr_text = tr.get("content", ""); tr_err = tr.get("is_error", False)
                    has_result = bool(tr_text and tr_text.strip() and tr_text != "Bash completed with no output")
                    tr_esc = ""
                    if has_result:
                        tr_str = str(tr_text)[:5000]; tr_esc = esc(tr_str)
                        if len(str(tr_text)) > 5000: tr_esc += "\n… truncated"
                    err_cls = " act-err" if tr_err else ""
                    if tn == "Edit" and ti.get("old_string") is not None:
                        fp = esc(ti.get("file_path", "?")); old = ti.get("old_string", "").splitlines(True); new = ti.get("new_string", "").splitlines(True)
                        dl = [f'<div class="diff-del"><span class="diff-ln">{i+1}</span><span class="diff-sign">-</span>{esc(l.rstrip())}</div>' for i, l in enumerate(old[:60])]
                        dl += [f'<div class="diff-add"><span class="diff-ln">{i+1}</span><span class="diff-sign">+</span>{esc(l.rstrip())}</div>' for i, l in enumerate(new[:60])]
                        if len(old) > 60 or len(new) > 60: dl.append('<div class="diff-ctx"><span class="diff-ln"></span><span class="diff-sign"> </span>… truncated</div>')
                        no, na, nm = len(old), len(new), min(len(old), len(new))
                        stats = f'<span class="diff-stat"><span class="diff-plus">+{na-nm}</span> <span class="diff-minus">-{no-nm}</span> <span class="diff-mod">~{nm}</span></span>'
                        pp = fp.rsplit("/", 1); fp_h = f'<span class="fp-dir">{esc(pp[0])}/</span>{esc(pp[1])}' if len(pp) > 1 else fp
                        parts.append(f'<details class="act diff-act{err_cls}"><summary class="act-h">{tool_svg}<span class="fp">{fp_h}</span>{stats}{_chev}</summary><div class="diff-body">{chr(10).join(dl)}</div></details>')
                    elif tn == "Bash" and inp:
                        body_html = f'<div class="act-cmd">{esc(inp)}</div>'
                        if tr_esc: body_html += f'<div class="act-out{" act-out-err" if tr_err else ""}">{tr_esc}</div>'
                        parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="t-det">{esc(inp[:80])}</span>{_chev}</summary><div class="act-body">{body_html}</div></details>')
                    else:
                        if is_fp:
                            pp = inp.rsplit("/", 1); dp = esc(pp[0]) if len(pp) > 1 else ""; bp = esc(pp[-1])
                            inner = f'<span class="fp-dir">{dp}/</span>{bp}' if dp else bp
                            label = f'<span class="fp">{inner}</span>'
                        else: label = f'<span class="t-det">{esc(inp[:80])}</span>'
                        if tr_esc and has_result:
                            parts.append(f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}{label}{_chev}</summary><div class="act-body"><pre class="t-out">{tr_esc}</pre></div></details>')
                        else: parts.append(f'<div class="chip">{tool_svg}{label}</div>')
        elif isinstance(content, str) and content.strip():
            b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
            parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
    return "\n".join(parts)
def _format_model_name(model_name: str) -> str:
    import re as _re
    s = model_name.replace("claude-", ""); s = _re.sub(r"-\d{8}$", "", s); s = _re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", s)
    s = _re.sub(r"-(\d+)\.(\d+)$", r" \1.\2", s); s = s.replace("-", " ").title()
    return s
def _transcript_stats(entries: list[TranscriptEntry]) -> TranscriptStatsDict:
    n_user = sum(1 for e in entries if e.get("type") == "user"
                 and e.get("message", {}).get("role") == "user"
                 and isinstance(e.get("message", {}).get("content"), str))
    n_tool = 0; n_edit = 0; lines_add = 0; lines_del = 0; lines_mod = 0; files_modified: set[str] = set()
    for e in entries:
        if e.get("type") != "assistant": continue
        for c in (e.get("message", {}).get("content") or []):
            if not isinstance(c, dict) or c.get("type") != "tool_use": continue
            n_tool += 1; ti = c.get("input", {}); cname = c.get("name")
            fp = ti.get("file_path", "")
            if cname == "Edit" and ti.get("old_string") is not None:
                n_edit += 1; n_old = len(ti.get("old_string", "").splitlines(True)); n_new = len(ti.get("new_string", "").splitlines(True))
                overlap = min(n_old, n_new); lines_mod += overlap; lines_del += n_old - overlap; lines_add += n_new - overlap
            if cname in ("Write", "Read", "Edit") and fp: files_modified.add(fp)
    model = version = git_branch = first_ts = last_ts = ""; input_tokens = output_tokens = 0
    for e in entries:
        if not model: model = e.get("message", {}).get("model", "")
        if not version: version = e.get("version", "")
        if not git_branch: git_branch = e.get("gitBranch", "")
        ts = e.get("timestamp", "")
        if ts:
            if not first_ts: first_ts = ts
            last_ts = ts
        usage = e.get("message", {}).get("usage", {})
        input_tokens += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        output_tokens += usage.get("output_tokens", 0)
    duration_str = ""
    if first_ts and last_ts:
        try:
            from datetime import datetime
            total_s = max(0, int((datetime.fromisoformat(last_ts.replace("Z", "+00:00")) - datetime.fromisoformat(first_ts.replace("Z", "+00:00"))).total_seconds()))
            d, rem = divmod(total_s, 86400); h = rem // 3600; m = (rem % 3600) // 60
            duration_str = f"{d}d {h}h" if d > 0 else f"{h}h {m}m" if h > 0 else f"{m}m"
        except (ValueError, TypeError, KeyError) as exc: _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
    return {"n_user": n_user, "n_tool": n_tool, "n_edit": n_edit, "lines_add": lines_add, "lines_del": lines_del, "lines_mod": lines_mod, "n_files": len(files_modified), "model": model, "version": version, "git_branch": git_branch, "first_ts": first_ts, "last_ts": last_ts, "input_tokens": input_tokens, "output_tokens": output_tokens, "duration": duration_str}
def _render_transcript_loading(name: str, sid: str | None, token: str, sync_key: str) -> str:
    import html as html_mod
    esc = html_mod.escape; info = _transcript_sync.get(sync_key) or {}; status = info.get("status", "syncing")
    progress = esc(info.get("progress", "Starting sync...")); pct = info.get("pct", 0)
    elapsed = int(_clock.time() - info.get("started", _clock.time())); error = info.get("error")
    if status == "error": bar_html = '<div class="bar-fill err" style="width:100%"></div>'; msg = f'<p class="err-msg">Error: {esc(error or "Unknown error")}</p>'; meta_js = ""
    else: bar_html = f'<div class="bar-fill" style="width:{pct}%"></div>'; msg = ""; meta_js = '<meta http-equiv="refresh" content="2">'
    spin = '<span class="spinner"></span>' if status == "syncing" else ""
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Loading {esc(name)}</title>{meta_js}<style>{_LOADING_CSS}</style></head><body><div class="card"><h1>Preparing Transcript</h1><p class="sub">{esc(name)}</p><div class="bar">{bar_html}</div><p class="progress">{spin}{progress}</p><p class="elapsed">{elapsed}s elapsed</p>{msg}</div></body></html>'
def _transcript_html_head(name: str, esc: Callable[[str], str]) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(name)} — Transcript</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">'
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" media="(prefers-color-scheme: dark)">'
            '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css" media="(prefers-color-scheme: light)">'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.1/marked.min.js"></script>'
            '<style>' + _TRANSCRIPT_CSS + '</style></head><body><div class="wrap"><div class="main"><div class="content">')
def _transcript_html_nav(name: str, stats: TranscriptStatsDict,
                         prompts_filter_url: str,
                         esc: Callable[[str], str]) -> str:
    _info_svg = '<svg viewBox="0 0 16 16" fill="currentColor" width="16" height="16"><path d="M0 8a8 8 0 1116 0A8 8 0 010 8zm8-6.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM6.5 7.75A.75.75 0 017.25 7h1a.75.75 0 01.75.75v2.75h.25a.75.75 0 010 1.5h-2a.75.75 0 010-1.5h.25v-2h-.25a.75.75 0 01-.75-.75zM8 6a1 1 0 110-2 1 1 0 010 2z"/></svg>'
    _clock_svg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 16A8 8 0 108 0a8 8 0 000 16zm.25-11.75v4l3 1.5-.5 1-3.5-1.75v-4.75h1z"/></svg>'
    _model_svg = "<svg viewBox='0 0 16 16' fill='currentColor'><path d='M8 1.5c-2.363 0-4 1.69-4 3.75 0 .984.424 1.625.984 2.304l.214.253c.223.264.47.556.673.848.284.411.537.896.621 1.49a.75.75 0 01-1.484.211c-.04-.282-.163-.547-.37-.847a8.456 8.456 0 00-.542-.68c-.084-.1-.173-.205-.268-.32C3.201 7.75 2.5 6.766 2.5 5.25 2.5 2.31 4.863 0 8 0s5.5 2.31 5.5 5.25c0 1.516-.701 2.5-1.328 3.259-.095.115-.184.22-.268.319-.207.245-.383.453-.541.681-.208.3-.33.565-.37.847a.75.75 0 01-1.485-.212c.084-.593.337-1.078.621-1.489.203-.292.45-.584.673-.848l.213-.253c.561-.679.985-1.32.985-2.304 0-2.06-1.637-3.75-4-3.75zM6 15.25a.75.75 0 01.75-.75h2.5a.75.75 0 010 1.5h-2.5a.75.75 0 01-.75-.75zM5.75 12a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-4.5z'/></svg>"
    _branch_svg = "<svg viewBox='0 0 16 16' fill='currentColor'><path d='M11.93 8.5a4.002 4.002 0 01-7.86 0H.75a.75.75 0 010-1.5h3.32a4.002 4.002 0 017.86 0h3.32a.75.75 0 010 1.5h-3.32zm-1.43-.75a2.5 2.5 0 10-5 0 2.5 2.5 0 005 0z'/></svg>"
    _chat_svg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M1.75 1h8.5c.966 0 1.75.784 1.75 1.75v5.5A1.75 1.75 0 0110.25 10H7.061l-2.574 2.573A1.458 1.458 0 012 11.543V10h-.25A1.75 1.75 0 010 8.25v-5.5C0 1.784.784 1 1.75 1z"/></svg>'
    _tool_svg = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5.433 2.304A4.49 4.49 0 003.5 6c0 1.598.832 3.002 2.09 3.802.518.328.929.923.902 1.64v.008l-.164 3.337a.75.75 0 11-1.498-.073l.163-3.34c.007-.14-.1-.313-.357-.476A5.994 5.994 0 012 6c0-2.033 1.01-3.83 2.555-4.916A1.89 1.89 0 015.433 2.304z"/></svg>'
    meta = []
    if stats["first_ts"]: meta.append(f"<span class='mi'>{_clock_svg}{esc(stats['first_ts'][:10])}</span>")
    if stats["model"]: meta.append(f"<span class='mi'>{_model_svg}{esc(stats['model'])}</span>")
    if stats["git_branch"]: meta.append(f"<span class='mi'>{_branch_svg}{esc(stats['git_branch'])}</span>")
    meta.append(f'<a class="mi" href="{prompts_filter_url}" style="cursor:pointer" title="Filter to prompts only">{_chat_svg}{stats["n_user"]} prompts</a>')
    meta.append(f'<span class="mi">{_tool_svg}{stats["n_tool"]} tool call{"s" if stats["n_tool"] != 1 else ""}</span>')
    return (f'<header><div class="h-row"><h1>{esc(name)}</h1><button class="sb-toggle" onclick="document.querySelector(\'.sidebar\').classList.toggle(\'sb-open\')" title="Session info">{_info_svg}</button></div>'
            f'<div class="meta">{"".join(meta)}</div></header>')
def _transcript_html_entries(page_entries: list[TranscriptEntry],
                             tool_results: dict[str, ToolResultDict],
                             search_val: str,
                             filter_banner: str, search_result: str,
                             nav_html: str, live_base_url: str,
                             name: str, token: str, search_query: str,
                             total: int, per_page: int, page: int,
                             total_pages: int,
                             esc: Callable[[str], str],
                             session_id: str | None,
                             filter_mode: str) -> str:
    _tool_results = tool_results; blocks = []; in_assistant_turn = False
    _url_prefix = live_base_url + "?" if live_base_url else "?"
    def _ctx_url(entry: TranscriptEntry) -> str:
        if not search_query: return ""
        idx = entry.get("_idx", -1)
        if idx < 0: return ""
        ctx_page = (idx // per_page) + 1; ctx_qs = []
        if token: ctx_qs.append(f"token={esc(token)}")
        if session_id: ctx_qs.append(f"sid={esc(session_id)}")
        if per_page != 50: ctx_qs.append(f"per_page={per_page}")
        ctx_qs.append(f"page={ctx_page}")
        return f'{_url_prefix}{"&".join(ctx_qs)}#e-{idx}'
    for entry in page_entries:
        etype = entry.get("type", ""); role = entry.get("message", {}).get("role", "")
        is_tool_result = (etype == "user" and role == "user" and
                          isinstance(entry.get("message", {}).get("content"), list) and
                          any(c.get("type") == "tool_result" for c in entry.get("message", {}).get("content", []) if isinstance(c, dict)))
        if is_tool_result: continue
        entry_html = _transcript_entry_to_html(entry, esc, tool_results=_tool_results)
        if not entry_html: continue
        eidx = entry.get("_idx", -1); anchor = f' id="e-{eidx}"' if eidx >= 0 else ""; curl = _ctx_url(entry)
        is_assistant = (etype == "assistant" and role == "assistant")
        if is_assistant:
            if curl:
                if in_assistant_turn:
                    blocks.append('</div>')
                    in_assistant_turn = False
                entry_html = f'<a class="ctx-wrap" href="{curl}"{anchor}>{entry_html}</a>'
                blocks.append(entry_html)
            else:
                if not in_assistant_turn:
                    blocks.append(f'<div class="turn-body"{anchor}>')
                    in_assistant_turn = True
                blocks.append(entry_html)
        else:
            if in_assistant_turn:
                blocks.append('</div>')
                in_assistant_turn = False
            if curl: entry_html = f'<a class="ctx-wrap" href="{curl}"{anchor}>{entry_html}</a>'
            elif anchor: entry_html = f'<div{anchor}>{entry_html}</div>'
            blocks.append(entry_html)
    if in_assistant_turn: blocks.append('</div>')
    hidden = "".join(f"<input type='hidden' name='{k}' value='{esc(str(v))}'>" for k, v in [("token", token), ("sid", session_id), ("per_page", per_page if per_page != 50 else ""), ("filter", "prompts" if filter_mode == "prompts" else "")] if v)
    action = f' action="{esc(live_base_url)}"' if live_base_url else ''
    upd_url = esc(live_base_url) + '/updates' if live_base_url else '/transcript/' + esc(name) + '/updates'
    pg_url = esc(live_base_url) if live_base_url else '/transcript/' + esc(name)
    search_attr = f' data-search="{esc(search_query)}"' if search_query else ''
    return (f'<form class="search-bar" method="get"{action}><input type="text" name="q" placeholder="Search transcript…" value="{search_val}"><button type="submit">Search</button>{hidden}</form>'
            f'{filter_banner}{search_result}{nav_html}'
            f'<div id="live-banner" class="live-banner" style="display:none"></div>'
            f'<div class="thread" id="thread"{search_attr} data-total="{total}" data-name="{esc(name)}" data-token="{esc(token)}" data-updates-url="{upd_url}" data-page-url="{pg_url}" data-per-page="{per_page}" data-page="{page}" data-total-pages="{total_pages}">'
            f'{"".join(blocks)}</div>{nav_html}')
def _transcript_html_footer(sid: str, stats: TranscriptStatsDict,
                              file_size_str: str, total: int,
                              page: int, total_pages: int,
                              esc: Callable[[str], str]) -> str:
    def _row(label: str, val: str) -> str: return f"<div class='sb-row'><span class='sb-label'>{label}</span><span class='sb-val'>{val}</span></div>"
    _d = '<div class="sb-divider"></div>'
    rows = [_row("Session", esc(sid[:12]))]
    if stats["model"]: rows.append(_row("Model", esc(_format_model_name(stats["model"]))))
    if stats["version"]: rows.append(_row("Version", esc(stats["version"])))
    if stats["git_branch"]: rows.append(_row("Branch", esc(stats["git_branch"])))
    rows.append(_d)
    rows += [_row("Prompts", str(stats["n_user"])), _row("Tool calls", str(stats["n_tool"]))]
    if stats["n_edit"]: rows.append(_row("Edits", str(stats["n_edit"])))
    if stats["n_files"]: rows.append(_row("Files touched", str(stats["n_files"])))
    if stats["lines_add"] or stats["lines_del"] or stats["lines_mod"]:
        rows.append(_d + f'<div class="sb-lines"><span class="plus">+{stats["lines_add"]}</span><span class="minus">-{stats["lines_del"]}</span><span class="mod">~{stats["lines_mod"]}</span></div>')
    if stats["duration"] or file_size_str or stats["input_tokens"]: rows.append(_d)
    if stats["duration"]: rows.append(_row("Duration", esc(stats["duration"])))
    if file_size_str: rows.append(_row("File size", esc(file_size_str)))
    if stats["input_tokens"]: rows.append(_row("Input tokens", f'{stats["input_tokens"]:,}'))
    if stats["output_tokens"]: rows.append(_row("Output tokens", f'{stats["output_tokens"]:,}'))
    rows += [_d, _row("Total entries", str(total)), _row("Page", f"{page}/{total_pages}")]
    sb = "\n".join(rows)
    return f'</div><aside class="sidebar"><div class="sidebar-inner"><div class="sb-title">Session Info</div>{sb}</div></aside></div></div><div class="jump"><a href="#" title="Top" onclick="window.scrollTo(0,0);return false">↑</a><a href="#" title="Bottom" onclick="window.scrollTo(0,document.body.scrollHeight);return false">↓</a></div>'
def _transcript_html_search_js() -> str:
    return '<script>\n' + _TRANSCRIPT_JS + '\n</script>\n</body>\n</html>\n'
def _err_page(title: str, detail: str = "") -> str:
    d = f"<p>{detail}</p>" if detail else ""
    return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>{title}</h1>{d}</body></html>"
def _render_transcript_html(name: str, session_id: str | None = None,
                            page: int | None = None, per_page: int = 50,
                            search_query: str = "", token: str = "",
                            filter_mode: str = "", search_sort: str = "relevance",
                            live_base_url: str = "") -> str:
    import html as html_mod
    esc = html_mod.escape; host = get_worker_host(name)
    if host:
        cwd = get_claude_session_cwd(name) or ""
        sid: str | None = session_id or get_claude_session_id(name, authoritative=True)
        if not sid:
            return _err_page(f"No session found for {esc(name)}")
        remote_home = _get_remote_home(host) or ""
        if not remote_home:
            return _err_page(f"Cannot resolve remote home for {esc(name)}")
        remote_cwd = _remap_path(cwd, host)
        remote_slug = _project_slug(remote_cwd)
        jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"; transcript_path = None
    else:
        transcript_path, sid, cwd = _resolve_transcript_path(name, session_id)
        if not sid:
            return _err_page(f"No session found for {esc(name)}")
        if not transcript_path or transcript_path == "syncing":
            return _err_page("Transcript not found", f"Worker: {esc(name)}, Session: {esc(sid)}")
        jsonl_path = str(transcript_path)
    query_result = (_run_transcript_query(jsonl_path, sid, "search+stats", host=host,
        search=search_query, page=page or 1, per_page=per_page, sort=search_sort) if search_query else
        _run_transcript_query(jsonl_path, sid, "entries+stats", host=host,
        page=page, per_page=per_page, filter_mode=filter_mode))
    stats_result = query_result.pop("stats", None) if query_result else None
    if not query_result:
        if not transcript_path or not Path(str(transcript_path)).exists():
            return _err_page("Transcript not available", f"Worker: {esc(name)}, Session: {esc(sid)}")
        all_entries = _parse_transcript_entries(transcript_path); total = len(all_entries)
        for _i, _e in enumerate(all_entries): _e["_idx"] = _i
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page is None: page = total_pages
        page = max(1, min(page, total_pages)); start = (page - 1) * per_page
        page_entries = all_entries[start:start + per_page]; stats = _transcript_stats(all_entries); file_size_str = ""
    else:
        page_entries = []; _entries_raw = query_result.get("entries", [])
        for e in (_entries_raw if isinstance(_entries_raw, list) else []):
            try:
                entry = cast(TranscriptEntry, json.loads(e["raw_json"]))
                entry["_idx"] = e.get("idx", -1)
                page_entries.append(entry)
            except (json.JSONDecodeError, KeyError): continue
        if search_query: total = _int_field(query_result, "total_results")
        else: total = _int_field(query_result, "total")
        total_pages = _int_field(query_result, "total_pages", 1); page = _int_field(query_result, "page", 1)
        _empty_stats: TranscriptStatsDict = {"n_user": 0, "n_tool": 0, "n_edit": 0, "lines_add": 0, "lines_del": 0, "lines_mod": 0, "n_files": 0, "model": "", "version": "", "git_branch": "", "first_ts": "", "last_ts": "", "input_tokens": 0, "output_tokens": 0, "duration": ""}
        stats = cast(TranscriptStatsDict, stats_result) if stats_result else _empty_stats
    file_size_str = ""
    if not host:
        try:
            file_size_bytes = os.path.getsize(transcript_path) if transcript_path else 0
            if file_size_bytes >= 1_048_576: file_size_str = f"{file_size_bytes / 1_048_576:.1f} MB"
            elif file_size_bytes >= 1024: file_size_str = f"{file_size_bytes / 1024:.0f} KB"
            else: file_size_str = f"{file_size_bytes} B"
        except OSError as exc: _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
    _tool_results: dict[str, ToolResultDict] = {}
    for entry in page_entries:
        ct = entry.get("message", {}).get("content", []) if entry.get("type") == "user" else []
        if not isinstance(ct, list): continue
        for item in ct:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                rt = item.get("content", ""); rt = "\n".join(str(r.get("text", "")) for r in rt if isinstance(r, dict) and r.get("type") == "text") if isinstance(rt, list) else str(rt)
                _tool_results[str(item.get("tool_use_id", ""))] = cast(ToolResultDict, {"content": rt, "is_error": bool(item.get("is_error"))})
    _url_prefix = live_base_url + "?" if live_base_url else "?"
    def _qs(*extras: str) -> str:
        return "&".join([p for p in [f"token={esc(token)}" if token else "", f"sid={esc(session_id)}" if session_id else "", f"per_page={per_page}" if per_page != 50 else ""] if p] + list(extras))
    qs_base = _qs(*(([f"q={esc(search_query)}"] if search_query else []) + ([f"filter={esc(filter_mode)}"] if filter_mode else [])))
    def page_url(p: int | str) -> str:
        return _url_prefix + (f"page={p}&{qs_base}" if qs_base else f"page={p}")
    nav_html = ""
    if total_pages > 1:
        def _pg(label: str, p: int, dis: bool = False, cur: bool = False) -> str:
            cls = " pg-dis" if dis else (" pg-cur" if cur else "")
            return f'<a class="pg-btn{cls}" href="{page_url(p)}">{label}</a>'
        start_p = max(1, min(page - 3, total_pages - 6)); end_p = min(total_pages, start_p + 6)
        nav_items = [_pg("First", 1, page <= 1), _pg("Prev", page - 1, page <= 1)]
        nav_items += [_pg(str(p), p, cur=(p == page)) for p in range(start_p, end_p + 1)]
        nav_items += [_pg("Next", page + 1, page >= total_pages), _pg("Last", total_pages, page >= total_pages)]
        nav_html = f'<nav class="pg">{"".join(nav_items)}<span class="pg-info">Page {page}/{total_pages} ({total} entries)</span></nav>'
    search_val = esc(search_query) if search_query else ""; search_result = ""
    if search_query:
        sort_label = "by time" if search_sort == "time" else "by relevance"
        alt_sort = "time" if search_sort == "relevance" else "relevance"
        sort_url = _url_prefix + _qs(f"q={esc(search_query)}", f"sort={alt_sort}")
        search_result = (
            f'<div class="search-info">Found {total} matching entries for "<strong>{esc(search_query)}</strong>" '
            f'(sorted {sort_label}) &middot; <a href="{sort_url}">sort by {alt_sort}</a></div>' )
    filt_qs = _qs("filter=prompts") if filter_mode != "prompts" else _qs()
    prompts_filter_url = _url_prefix + filt_qs if filt_qs else _url_prefix.rstrip("?"); filter_banner = ""
    if filter_mode == "prompts":
        clear_url = _url_prefix + _qs() if _qs() else _url_prefix.rstrip("?")
        filter_banner = f'<div class="search-info">Showing prompts only — <a href="{clear_url}">show all</a></div>'
    return (_transcript_html_head(name, esc)
            + _transcript_html_nav(name, stats, prompts_filter_url, esc)
            + _transcript_html_entries(
                page_entries, _tool_results, search_val,
                filter_banner, search_result, nav_html,
                live_base_url, name, token, search_query, total, per_page,
                page, total_pages, esc, session_id, filter_mode)
            + _transcript_html_footer(sid, stats, file_size_str, total, page, total_pages, esc)
            + _transcript_html_search_js())

# --- HTTP handler function (extracted from bridge.py Handler class) ---
def handle_transcript_endpoint(handler, parsed) -> None:
    import bridge as _b
    try:
        from urllib.parse import parse_qs
        query_params = parse_qs(parsed.query); token = query_params.get("token", [None])[0]
        if not _b.tokens.validate_rewind(token):
            handler._send_html(_err_page("Session Expired", "This link has expired or is invalid. Send <code>/rewind &lt;name&gt;</code> in Telegram to get a fresh 5-minute link.").encode("utf-8"), 403); return
        parts = parsed.path.rstrip("/").split("/")
        if len(parts) < 3 or not parts[2]:
            handler._send_json(400, {"error": "Usage: /transcript/<worker_name>"}); return
        name = parts[2]
        if len(parts) >= 4 and parts[3] == "updates":
            query_params = parse_qs(parsed.query); since = int(query_params.get("since", [0])[0])
            session_id = query_params.get("sid", [None])[0]; host = _b.get_worker_host(name)
            if host:
                cwd = _b.get_claude_session_cwd(name) or ""
                sid = session_id or _b.get_claude_session_id(name)
                if not sid:
                    handler._send_json(200, {"total": 0, "new": 0}); return
                remote_home = _b._get_remote_home(host) or ""
                remote_cwd = _b._remap_path(cwd, host); remote_slug = _b._project_slug(remote_cwd)
                jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
            else:
                _tp, sid, _cwd = _resolve_transcript_path(name, session_id)
                if not _tp or not sid:
                    handler._send_json(200, {"total": 0, "new": 0}); return
                jsonl_path = str(_tp)
            result = _run_transcript_query(jsonl_path, sid, "stats", host=host); total = 0
            if result:
                total = _b._int_field(result, "n_user") + _b._int_field(result, "n_tool")
                count_result = _run_transcript_query(jsonl_path, sid, "entries", host=host, page=1, per_page=1)
                if count_result: total = _b._int_field(count_result, "total", total)
            new_count = max(0, total - since)
            handler._send_json(200, {"total": total, "new": new_count}); return
        query_params = parse_qs(parsed.query); session_id = query_params.get("sid", [None])[0]
        page_raw = query_params.get("page", [None])[0]
        try: page = max(1, int(page_raw)) if page_raw is not None else None
        except (ValueError, TypeError): page = None
        try: per_page = max(1, min(500, int(query_params.get("per_page", [50])[0])))
        except (ValueError, TypeError): per_page = 50
        search_query = query_params.get("q", [""])[0].strip()
        search_sort = query_params.get("sort", ["relevance"])[0].strip()
        if search_sort not in ("relevance", "time"): search_sort = "relevance"
        filter_mode = query_params.get("filter", [""])[0].strip(); host = _b.get_worker_host(name)
        if not host:
            _tp, _sid, _cwd = _resolve_transcript_path(name, session_id)
            if _tp == "syncing":
                sync_key = f"{name}:{_sid}"
                handler._send_html(_render_transcript_loading(name, _sid, token or "", sync_key).encode("utf-8")); return
        html_content = _render_transcript_html(
                name, session_id=session_id,
                page=page, per_page=per_page, search_query=search_query,
                token=token or "", filter_mode=filter_mode, search_sort=search_sort)
        handler._send_html(html_content.encode("utf-8"))
    except (OSError, ValueError, KeyError) as e:
        _b._log(_b._LOG_ERROR, "transcript", f"Transcript endpoint error: {e}", exc=e)
        handler._send_text(500, str(e))
