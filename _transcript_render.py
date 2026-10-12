"""Transcript HTML rendering — extracted from transcript.py."""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Callable, cast
from core import _log, _LOG_ERROR, _LOG_WARN, _LOG_DEBUG, _clock, _int_field
from claudecode import (
    _remote_run,
    _remap_path,
    _project_slug,
    _get_remote_home,
    normalize_cwd,
    AuthorDetection,
)
from _types import TranscriptEntry, TranscriptStatsDict, ToolResultDict

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TRANSCRIPT_CSS = (_TEMPLATE_DIR / "transcript.css").read_text() if (_TEMPLATE_DIR / "transcript.css").exists() else ""
_TRANSCRIPT_JS = (_TEMPLATE_DIR / "transcript.js").read_text() if (_TEMPLATE_DIR / "transcript.js").exists() else ""
_ICONS = (
    json.loads((_TEMPLATE_DIR / "transcript_icons.json").read_text())
    if (_TEMPLATE_DIR / "transcript_icons.json").exists()
    else {}
)

_TRANSCRIPT_CHEVRON_SVG = _ICONS.get("chevron", "")
_TRANSCRIPT_CLAUDE_AVATAR = _ICONS.get("claude_avatar", "")
_TRANSCRIPT_DEFAULT_TOOL_SVG = _ICONS.get("default_tool", "")
_TRANSCRIPT_TOOL_SVGS: dict[str, str] = _ICONS.get("tools", {})
_TEAM_MEMBERS = {
    "chen",
    "geni",
    "hiro",
    "jin",
    "kai",
    "kelvin",
    "kenji",
    "lee",
    "luck",
    "mon",
    "noa",
    "ren",
    "ryo",
    "sora",
    "taro",
    "x",
    "yuki",
    "finn",
}
_MANAGER_AV = _ICONS.get("manager_avatar", "")
_LOADING_CSS = (
    (_TEMPLATE_DIR / "transcript_loading.css").read_text().strip()
    if (_TEMPLATE_DIR / "transcript_loading.css").exists()
    else ""
)


def _generate_member_avatar(name: str) -> str:
    name_hash = hash(name) & 0xFFFFFFFF
    hue = (name_hash % 36) * 10
    sat = 55 + (name_hash >> 6 & 1) * 15
    shape_idx = (name_hash >> 7) % 6
    accent_idx = (name_hash >> 10) % 5
    initials = name[:2].upper() if len(name) >= 2 else name.upper()
    bg = f"hsl({hue},{sat}%,42%)"
    fg = f"hsl({hue},{max(sat-20,30)}%,75%)"
    shapes = [
        '<circle cx="14" cy="14" r="14"/>',
        '<rect x="1" y="1" width="26" height="26" rx="6"/>',
        '<polygon points="14,0 28,7 28,21 14,28 0,21 0,7"/>',
        '<polygon points="14,1 27,14 14,27 1,14"/>',
        '<polygon points="14,0 28,10 22,28 6,28 0,10"/>',
        '<rect x="0" y="0" width="28" height="28" rx="10"/>',
    ]
    accents = [
        "",
        f'<circle cx="14" cy="14" r="8" fill="none" stroke="{fg}" stroke-width="1.5" opacity=".3"/>',
        f'<circle cx="7" cy="7" r="2" fill="{fg}" opacity=".2"/><circle cx="21" cy="7" r="2" fill="{fg}" opacity=".2"/>',
        f'<line x1="4" y1="4" x2="24" y2="24" stroke="{fg}" stroke-width="1" opacity=".15"/><line x1="4" y1="24" x2="24" y2="4" stroke="{fg}" stroke-width="1" opacity=".15"/>',
        f'<rect x="4" y="12" width="20" height="4" rx="2" fill="{fg}" opacity=".15"/>',
    ]
    return (
        f'<div class="u-av"><svg viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">'
        f'<g fill="{bg}">{shapes[shape_idx]}</g>'
        f"{accents[accent_idx]}"
        f'<text x="14" y="14" text-anchor="middle" dominant-baseline="central" '
        f'fill="#fff" font-family="Inter,system-ui,sans-serif" font-size="11" font-weight="600" '
        f'opacity=".9">{initials}</text></svg></div>'
    )


def _detect_message_author(text: str) -> AuthorDetection:
    stripped = text.strip()
    colon_pos = stripped.find(":")
    if 0 < colon_pos <= 10:
        prefix = stripped[:colon_pos].lower().strip()
        rest = stripped[colon_pos + 1 :].strip()
        if prefix == "manager":
            return AuthorDetection("manager", _MANAGER_AV, rest or stripped)
        if prefix in _TEAM_MEMBERS:
            return AuthorDetection(prefix, _generate_member_avatar(prefix), rest or stripped)
    return AuthorDetection("manager", _MANAGER_AV, stripped)


def _transcript_entry_to_html(
    entry: TranscriptEntry, esc: Callable[[str], str], tool_results: dict[str, ToolResultDict] | None = None
) -> str:
    import base64 as _b64

    if tool_results is None:
        tool_results = {}
    etype = entry.get("type", "")
    msg = entry.get("message", {})
    role = msg.get("role", "")
    content = msg.get("content", "")
    _chev = _TRANSCRIPT_CHEVRON_SVG
    _claude_av = _TRANSCRIPT_CLAUDE_AVATAR
    _tool_svgs = _TRANSCRIPT_TOOL_SVGS
    _default_tool_svg = _TRANSCRIPT_DEFAULT_TOOL_SVG
    parts: list[str] = []
    ts_raw = entry.get("timestamp", "")
    ts_html = ""
    if ts_raw:
        ts_html = f'<span class="ts" data-ts="{esc(ts_raw)}">{esc(ts_raw[:16].replace("T"," "))}</span>'
    if etype == "user" and role == "user":
        if isinstance(content, str) and content.strip():
            raw = content.strip()
            if raw.startswith("<task-notification>") or raw.startswith("<system-reminder>"):
                return ""
            _author, _avatar, _display = _detect_message_author(raw)
            text = esc(_display)
            if len(text) > 2000:
                text = text[:2000] + "\n\n<em>… truncated</em>"
            _name_html = f'<span class="u-name">{esc(_author)}</span>' if _author != "manager" else ""
            parts.append(
                f'<div class="user-msg">{_avatar}<div class="u-body">{_name_html}<div class="u-text">{text}{ts_html}</div></div></div>'
            )
        elif isinstance(content, list):
            pass
    elif etype == "assistant" and role == "assistant":
        if isinstance(content, list):
            for item in content:
                ct = str(item.get("type", ""))
                if ct == "thinking":
                    raw_thinking = str(item.get("thinking", ""))
                    if raw_thinking and raw_thinking.strip():
                        tt = esc(raw_thinking[:3000])
                        if len(raw_thinking) > 3000:
                            tt += "\n… truncated"
                        parts.append(
                            f'<details class="think"><summary class="think-h">{_chev} Thinking</summary><div class="think-t">{tt}</div></details>'
                        )
                elif ct == "text":
                    text = item.get("text", "")
                    if text and text != "(no content)":
                        b64 = _b64.b64encode(text.encode("utf-8")).decode("ascii")
                        parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
                elif ct == "tool_use":
                    tn = item.get("name", "?")
                    ti = item.get("input", {})
                    tool_svg = _tool_svgs.get(tn, _default_tool_svg)
                    _INPUT_KEYS = {
                        "Read": "file_path",
                        "Write": "file_path",
                        "Edit": "file_path",
                        "Glob": "pattern",
                        "Bash": "command",
                        "Grep": "pattern",
                        "Search": "pattern",
                    }
                    key = _INPUT_KEYS.get(tn)
                    if key:
                        inp = ti.get(key, "")
                    elif tn == "Agent":
                        inp = ti.get("description", "") or str(ti.get("prompt", ""))[:80]
                    else:
                        inp = json.dumps(ti, ensure_ascii=False)[:200]
                    is_fp = tn in ("Read", "Write", "Edit") and inp and "/" in inp
                    inp = str(inp)[:300]
                    tool_id = item.get("id", "")
                    tr = tool_results.get(tool_id, {})
                    tr_text = tr.get("content", "")
                    tr_err = tr.get("is_error", False)
                    has_result = bool(tr_text and tr_text.strip() and tr_text != "Bash completed with no output")
                    tr_esc = ""
                    if has_result:
                        tr_str = str(tr_text)[:5000]
                        tr_esc = esc(tr_str)
                        if len(str(tr_text)) > 5000:
                            tr_esc += "\n… truncated"
                    err_cls = " act-err" if tr_err else ""
                    if tn == "Edit" and ti.get("old_string") is not None:
                        fp = esc(ti.get("file_path", "?"))
                        old = ti.get("old_string", "").splitlines(True)
                        new = ti.get("new_string", "").splitlines(True)
                        dl = [
                            f'<div class="diff-del"><span class="diff-ln">{i+1}</span><span class="diff-sign">-</span>{esc(l.rstrip())}</div>'
                            for i, l in enumerate(old[:60])
                        ]
                        dl += [
                            f'<div class="diff-add"><span class="diff-ln">{i+1}</span><span class="diff-sign">+</span>{esc(l.rstrip())}</div>'
                            for i, l in enumerate(new[:60])
                        ]
                        if len(old) > 60 or len(new) > 60:
                            dl.append(
                                '<div class="diff-ctx"><span class="diff-ln"></span><span class="diff-sign"> </span>… truncated</div>'
                            )
                        no, na, nm = len(old), len(new), min(len(old), len(new))
                        stats = f'<span class="diff-stat"><span class="diff-plus">+{na-nm}</span> <span class="diff-minus">-{no-nm}</span> <span class="diff-mod">~{nm}</span></span>'
                        pp = fp.rsplit("/", 1)
                        fp_h = f'<span class="fp-dir">{esc(pp[0])}/</span>{esc(pp[1])}' if len(pp) > 1 else fp
                        parts.append(
                            f'<details class="act diff-act{err_cls}"><summary class="act-h">{tool_svg}<span class="fp">{fp_h}</span>{stats}{_chev}</summary><div class="diff-body">{chr(10).join(dl)}</div></details>'
                        )
                    elif tn == "Bash" and inp:
                        body_html = f'<div class="act-cmd">{esc(inp)}</div>'
                        if tr_esc:
                            body_html += f'<div class="act-out{" act-out-err" if tr_err else ""}">{tr_esc}</div>'
                        parts.append(
                            f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}<span class="t-det">{esc(inp[:80])}</span>{_chev}</summary><div class="act-body">{body_html}</div></details>'
                        )
                    else:
                        if is_fp:
                            pp = inp.rsplit("/", 1)
                            dp = esc(pp[0]) if len(pp) > 1 else ""
                            bp = esc(pp[-1])
                            inner = f'<span class="fp-dir">{dp}/</span>{bp}' if dp else bp
                            label = f'<span class="fp">{inner}</span>'
                        else:
                            label = f'<span class="t-det">{esc(inp[:80])}</span>'
                        if tr_esc and has_result:
                            parts.append(
                                f'<details class="act{err_cls}"><summary class="act-h">{tool_svg}{label}{_chev}</summary><div class="act-body"><pre class="t-out">{tr_esc}</pre></div></details>'
                            )
                        else:
                            parts.append(f'<div class="chip">{tool_svg}{label}</div>')
        elif isinstance(content, str) and content.strip():
            b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
            parts.append(f'<div class="a-text markdown" data-md="{b64}"></div>')
    return "\n".join(parts)


def _format_model_name(model_name: str) -> str:
    import re as _re

    s = model_name.replace("claude-", "")
    s = _re.sub(r"-\d{8}$", "", s)
    s = _re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", s)
    s = _re.sub(r"-(\d+)\.(\d+)$", r" \1.\2", s)
    s = s.replace("-", " ").title()
    return s


def _transcript_stats(entries: list[TranscriptEntry]) -> TranscriptStatsDict:
    n_user = sum(
        1
        for e in entries
        if e.get("type") == "user"
        and e.get("message", {}).get("role") == "user"
        and isinstance(e.get("message", {}).get("content"), str)
    )
    n_tool = 0
    n_edit = 0
    lines_add = 0
    lines_del = 0
    lines_mod = 0
    files_modified: set[str] = set()
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for c in e.get("message", {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            n_tool += 1
            ti = c.get("input", {})
            cname = c.get("name")
            fp = ti.get("file_path", "")
            if cname == "Edit" and ti.get("old_string") is not None:
                n_edit += 1
                n_old = len(ti.get("old_string", "").splitlines(True))
                n_new = len(ti.get("new_string", "").splitlines(True))
                overlap = min(n_old, n_new)
                lines_mod += overlap
                lines_del += n_old - overlap
                lines_add += n_new - overlap
            if cname in ("Write", "Read", "Edit") and fp:
                files_modified.add(fp)
    model = version = git_branch = first_ts = last_ts = ""
    input_tokens = output_tokens = 0
    for e in entries:
        if not model:
            model = e.get("message", {}).get("model", "")
        if not version:
            version = e.get("version", "")
        if not git_branch:
            git_branch = e.get("gitBranch", "")
        ts = e.get("timestamp", "")
        if ts:
            if not first_ts:
                first_ts = ts
            last_ts = ts
        usage = e.get("message", {}).get("usage", {})
        input_tokens += (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        output_tokens += usage.get("output_tokens", 0)
    duration_str = ""
    if first_ts and last_ts:
        try:
            from datetime import datetime

            total_s = max(
                0,
                int(
                    (
                        datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                        - datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                    ).total_seconds()
                ),
            )
            d, rem = divmod(total_s, 86400)
            h = rem // 3600
            m = (rem % 3600) // 60
            duration_str = f"{d}d {h}h" if d > 0 else f"{h}h {m}m" if h > 0 else f"{m}m"
        except (ValueError, TypeError, KeyError) as exc:
            _log(_LOG_DEBUG, "parse:unknown", f"{type(exc).__name__}: {exc}")
    return {
        "n_user": n_user,
        "n_tool": n_tool,
        "n_edit": n_edit,
        "lines_add": lines_add,
        "lines_del": lines_del,
        "lines_mod": lines_mod,
        "n_files": len(files_modified),
        "model": model,
        "version": version,
        "git_branch": git_branch,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration": duration_str,
    }


def _render_transcript_loading(name: str, sid: str | None, token: str, sync_key: str) -> str:
    import html as html_mod

    esc = html_mod.escape
    info = _transcript_sync.get(sync_key) or {}
    status = info.get("status", "syncing")
    progress = esc(info.get("progress", "Starting sync..."))
    pct = info.get("pct", 0)
    elapsed = int(_clock.time() - info.get("started", _clock.time()))
    error = info.get("error")
    if status == "error":
        bar_html = '<div class="bar-fill err" style="width:100%"></div>'
        msg = f'<p class="err-msg">Error: {esc(error or "Unknown error")}</p>'
        meta_js = ""
    else:
        bar_html = f'<div class="bar-fill" style="width:{pct}%"></div>'
        msg = ""
        meta_js = '<meta http-equiv="refresh" content="2">'
    spin = '<span class="spinner"></span>' if status == "syncing" else ""
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Loading {esc(name)}</title>{meta_js}<style>{_LOADING_CSS}</style></head><body><div class="card"><h1>Preparing Transcript</h1><p class="sub">{esc(name)}</p><div class="bar">{bar_html}</div><p class="progress">{spin}{progress}</p><p class="elapsed">{elapsed}s elapsed</p>{msg}</div></body></html>'


def _transcript_html_head(name: str, esc: Callable[[str], str]) -> str:
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(name)} — Transcript</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">'
        '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" media="(prefers-color-scheme: dark)">'
        '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css" media="(prefers-color-scheme: light)">'
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>'
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.1/marked.min.js"></script>'
        "<style>" + _TRANSCRIPT_CSS + '</style></head><body><div class="wrap"><div class="main"><div class="content">'
    )


def _transcript_html_nav(
    name: str, stats: TranscriptStatsDict, prompts_filter_url: str, esc: Callable[[str], str]
) -> str:
    _nav = _ICONS.get("nav", {})
    _info_svg = _nav.get("info", "")
    _clock_svg = _nav.get("clock", "")
    _model_svg = _nav.get("model", "")
    _branch_svg = _nav.get("branch", "")
    _chat_svg = _nav.get("chat", "")
    _tool_svg = _nav.get("tool", "")
    meta = []
    if stats["first_ts"]:
        meta.append(f"<span class='mi'>{_clock_svg}{esc(stats['first_ts'][:10])}</span>")
    if stats["model"]:
        meta.append(f"<span class='mi'>{_model_svg}{esc(stats['model'])}</span>")
    if stats["git_branch"]:
        meta.append(f"<span class='mi'>{_branch_svg}{esc(stats['git_branch'])}</span>")
    meta.append(
        f'<a class="mi" href="{prompts_filter_url}" style="cursor:pointer" title="Filter to prompts only">{_chat_svg}{stats["n_user"]} prompts</a>'
    )
    meta.append(f'<span class="mi">{_tool_svg}{stats["n_tool"]} tool call{"s" if stats["n_tool"] != 1 else ""}</span>')
    return (
        f'<header><div class="h-row"><h1>{esc(name)}</h1><button class="sb-toggle" onclick="document.querySelector(\'.sidebar\').classList.toggle(\'sb-open\')" title="Session info">{_info_svg}</button></div>'
        f'<div class="meta">{"".join(meta)}</div></header>'
    )


def _transcript_html_entries(
    page_entries: list[TranscriptEntry],
    tool_results: dict[str, ToolResultDict],
    search_val: str,
    filter_banner: str,
    search_result: str,
    nav_html: str,
    live_base_url: str,
    name: str,
    token: str,
    search_query: str,
    total: int,
    per_page: int,
    page: int,
    total_pages: int,
    esc: Callable[[str], str],
    session_id: str | None,
    filter_mode: str,
) -> str:
    _tool_results = tool_results
    blocks = []
    in_assistant_turn = False
    _url_prefix = live_base_url + "?" if live_base_url else "?"

    def _ctx_url(entry: TranscriptEntry) -> str:
        if not search_query:
            return ""
        idx = entry.get("_idx", -1)
        if idx < 0:
            return ""
        ctx_page = (idx // per_page) + 1
        ctx_qs = []
        if token:
            ctx_qs.append(f"token={esc(token)}")
        if session_id:
            ctx_qs.append(f"sid={esc(session_id)}")
        if per_page != 50:
            ctx_qs.append(f"per_page={per_page}")
        ctx_qs.append(f"page={ctx_page}")
        return f'{_url_prefix}{"&".join(ctx_qs)}#e-{idx}'

    for entry in page_entries:
        etype = entry.get("type", "")
        role = entry.get("message", {}).get("role", "")
        is_tool_result = (
            etype == "user"
            and role == "user"
            and isinstance(entry.get("message", {}).get("content"), list)
            and any(
                c.get("type") == "tool_result"
                for c in entry.get("message", {}).get("content", [])
                if isinstance(c, dict)
            )
        )
        if is_tool_result:
            continue
        entry_html = _transcript_entry_to_html(entry, esc, tool_results=_tool_results)
        if not entry_html:
            continue
        eidx = entry.get("_idx", -1)
        anchor = f' id="e-{eidx}"' if eidx >= 0 else ""
        curl = _ctx_url(entry)
        is_assistant = etype == "assistant" and role == "assistant"
        if is_assistant:
            if curl:
                if in_assistant_turn:
                    blocks.append("</div>")
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
                blocks.append("</div>")
                in_assistant_turn = False
            if curl:
                entry_html = f'<a class="ctx-wrap" href="{curl}"{anchor}>{entry_html}</a>'
            elif anchor:
                entry_html = f"<div{anchor}>{entry_html}</div>"
            blocks.append(entry_html)
    if in_assistant_turn:
        blocks.append("</div>")
    hidden = "".join(
        f"<input type='hidden' name='{k}' value='{esc(str(v))}'>"
        for k, v in [
            ("token", token),
            ("sid", session_id),
            ("per_page", per_page if per_page != 50 else ""),
            ("filter", "prompts" if filter_mode == "prompts" else ""),
        ]
        if v
    )
    action = f' action="{esc(live_base_url)}"' if live_base_url else ""
    upd_url = esc(live_base_url) + "/updates" if live_base_url else "/transcript/" + esc(name) + "/updates"
    pg_url = esc(live_base_url) if live_base_url else "/transcript/" + esc(name)
    search_attr = f' data-search="{esc(search_query)}"' if search_query else ""
    return (
        f'<form class="search-bar" method="get"{action}><input type="text" name="q" placeholder="Search transcript…" value="{search_val}"><button type="submit">Search</button>{hidden}</form>'
        f"{filter_banner}{search_result}{nav_html}"
        f'<div id="live-banner" class="live-banner" style="display:none"></div>'
        f'<div class="thread" id="thread"{search_attr} data-total="{total}" data-name="{esc(name)}" data-token="{esc(token)}" data-updates-url="{upd_url}" data-page-url="{pg_url}" data-per-page="{per_page}" data-page="{page}" data-total-pages="{total_pages}">'
        f'{"".join(blocks)}</div>{nav_html}'
    )


def _transcript_html_footer(
    sid: str,
    stats: TranscriptStatsDict,
    file_size_str: str,
    total: int,
    page: int,
    total_pages: int,
    esc: Callable[[str], str],
) -> str:
    def _row(label: str, val: str) -> str:
        return f"<div class='sb-row'><span class='sb-label'>{label}</span><span class='sb-val'>{val}</span></div>"

    _d = '<div class="sb-divider"></div>'
    rows = [_row("Session", esc(sid[:12]))]
    if stats["model"]:
        rows.append(_row("Model", esc(_format_model_name(stats["model"]))))
    if stats["version"]:
        rows.append(_row("Version", esc(stats["version"])))
    if stats["git_branch"]:
        rows.append(_row("Branch", esc(stats["git_branch"])))
    rows.append(_d)
    rows += [_row("Prompts", str(stats["n_user"])), _row("Tool calls", str(stats["n_tool"]))]
    if stats["n_edit"]:
        rows.append(_row("Edits", str(stats["n_edit"])))
    if stats["n_files"]:
        rows.append(_row("Files touched", str(stats["n_files"])))
    if stats["lines_add"] or stats["lines_del"] or stats["lines_mod"]:
        rows.append(
            _d
            + f'<div class="sb-lines"><span class="plus">+{stats["lines_add"]}</span><span class="minus">-{stats["lines_del"]}</span><span class="mod">~{stats["lines_mod"]}</span></div>'
        )
    if stats["duration"] or file_size_str or stats["input_tokens"]:
        rows.append(_d)
    if stats["duration"]:
        rows.append(_row("Duration", esc(stats["duration"])))
    if file_size_str:
        rows.append(_row("File size", esc(file_size_str)))
    if stats["input_tokens"]:
        rows.append(_row("Input tokens", f'{stats["input_tokens"]:,}'))
    if stats["output_tokens"]:
        rows.append(_row("Output tokens", f'{stats["output_tokens"]:,}'))
    rows += [_d, _row("Total entries", str(total)), _row("Page", f"{page}/{total_pages}")]
    sb = "\n".join(rows)
    return f'</div><aside class="sidebar"><div class="sidebar-inner"><div class="sb-title">Session Info</div>{sb}</div></aside></div></div><div class="jump"><a href="#" title="Top" onclick="window.scrollTo(0,0);return false">↑</a><a href="#" title="Bottom" onclick="window.scrollTo(0,document.body.scrollHeight);return false">↓</a></div>'


def _transcript_html_search_js() -> str:
    return "<script>\n" + _TRANSCRIPT_JS + "\n</script>\n</body>\n</html>\n"


def _err_page(title: str, detail: str = "") -> str:
    d = f"<p>{detail}</p>" if detail else ""
    return f"<html><body style='background:#0b0d0b;color:#f6fff5;font-family:system-ui;padding:40px'><h1>{title}</h1>{d}</body></html>"


def _render_transcript_html(
    name: str,
    session_id: str | None = None,
    page: int | None = None,
    per_page: int = 50,
    search_query: str = "",
    token: str = "",
    filter_mode: str = "",
    search_sort: str = "relevance",
    live_base_url: str = "",
) -> str:
    import html as html_mod
    import transcript as _ts  # late import — transcript.py imports from us

    get_worker_host = _ts.get_worker_host
    get_claude_session_cwd = _ts.get_claude_session_cwd
    get_claude_session_id = _ts.get_claude_session_id
    _resolve_transcript_path = _ts._resolve_transcript_path
    _run_transcript_query = _ts._run_transcript_query
    _parse_transcript_entries = _ts._parse_transcript_entries
    esc = html_mod.escape
    host = get_worker_host(name)
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
        jsonl_path = f"{remote_home}/.claude/projects/{remote_slug}/{sid}.jsonl"
        transcript_path = None
    else:
        transcript_path, sid, cwd = _resolve_transcript_path(name, session_id)
        if not sid:
            return _err_page(f"No session found for {esc(name)}")
        if not transcript_path or transcript_path == "syncing":
            return _err_page("Transcript not found", f"Worker: {esc(name)}, Session: {esc(sid)}")
        jsonl_path = str(transcript_path)
    query_result = (
        _run_transcript_query(
            jsonl_path,
            sid,
            "search+stats",
            host=host,
            search=search_query,
            page=page or 1,
            per_page=per_page,
            sort=search_sort,
        )
        if search_query
        else _run_transcript_query(
            jsonl_path, sid, "entries+stats", host=host, page=page, per_page=per_page, filter_mode=filter_mode
        )
    )
    stats_result = query_result.pop("stats", None) if query_result else None
    if not query_result:
        if not transcript_path or not Path(str(transcript_path)).exists():
            return _err_page("Transcript not available", f"Worker: {esc(name)}, Session: {esc(sid)}")
        all_entries = _parse_transcript_entries(transcript_path)
        total = len(all_entries)
        for _i, _e in enumerate(all_entries):
            _e["_idx"] = _i
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page is None:
            page = total_pages
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        page_entries = all_entries[start : start + per_page]
        stats = _transcript_stats(all_entries)
        file_size_str = ""
    else:
        page_entries = []
        _entries_raw = query_result.get("entries", [])
        for e in _entries_raw if isinstance(_entries_raw, list) else []:
            try:
                entry = cast(TranscriptEntry, json.loads(e["raw_json"]))
                entry["_idx"] = e.get("idx", -1)
                page_entries.append(entry)
            except (json.JSONDecodeError, KeyError):
                continue
        if search_query:
            total = _int_field(query_result, "total_results")
        else:
            total = _int_field(query_result, "total")
        total_pages = _int_field(query_result, "total_pages", 1)
        page = _int_field(query_result, "page", 1)
        _empty_stats: TranscriptStatsDict = {
            "n_user": 0,
            "n_tool": 0,
            "n_edit": 0,
            "lines_add": 0,
            "lines_del": 0,
            "lines_mod": 0,
            "n_files": 0,
            "model": "",
            "version": "",
            "git_branch": "",
            "first_ts": "",
            "last_ts": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "duration": "",
        }
        stats = cast(TranscriptStatsDict, stats_result) if stats_result else _empty_stats
    file_size_str = ""
    if not host:
        try:
            file_size_bytes = os.path.getsize(transcript_path) if transcript_path else 0
            if file_size_bytes >= 1_048_576:
                file_size_str = f"{file_size_bytes / 1_048_576:.1f} MB"
            elif file_size_bytes >= 1024:
                file_size_str = f"{file_size_bytes / 1024:.0f} KB"
            else:
                file_size_str = f"{file_size_bytes} B"
        except OSError as exc:
            _log(_LOG_DEBUG, "io:unknown", f"{type(exc).__name__}: {exc}")
    _tool_results: dict[str, ToolResultDict] = {}
    for entry in page_entries:
        ct = entry.get("message", {}).get("content", []) if entry.get("type") == "user" else []
        if not isinstance(ct, list):
            continue
        for item in ct:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                rt = item.get("content", "")
                rt = (
                    "\n".join(str(r.get("text", "")) for r in rt if isinstance(r, dict) and r.get("type") == "text")
                    if isinstance(rt, list)
                    else str(rt)
                )
                _tool_results[str(item.get("tool_use_id", ""))] = cast(
                    ToolResultDict, {"content": rt, "is_error": bool(item.get("is_error"))}
                )
    _url_prefix = live_base_url + "?" if live_base_url else "?"

    def _qs(*extras: str) -> str:
        return "&".join(
            [
                p
                for p in [
                    f"token={esc(token)}" if token else "",
                    f"sid={esc(session_id)}" if session_id else "",
                    f"per_page={per_page}" if per_page != 50 else "",
                ]
                if p
            ]
            + list(extras)
        )

    qs_base = _qs(
        *(
            ([f"q={esc(search_query)}"] if search_query else [])
            + ([f"filter={esc(filter_mode)}"] if filter_mode else [])
        )
    )

    def page_url(p: int | str) -> str:
        return _url_prefix + (f"page={p}&{qs_base}" if qs_base else f"page={p}")

    nav_html = ""
    if total_pages > 1:

        def _pg(label: str, p: int, dis: bool = False, cur: bool = False) -> str:
            cls = " pg-dis" if dis else (" pg-cur" if cur else "")
            return f'<a class="pg-btn{cls}" href="{page_url(p)}">{label}</a>'

        start_p = max(1, min(page - 3, total_pages - 6))
        end_p = min(total_pages, start_p + 6)
        nav_items = [_pg("First", 1, page <= 1), _pg("Prev", page - 1, page <= 1)]
        nav_items += [_pg(str(p), p, cur=(p == page)) for p in range(start_p, end_p + 1)]
        nav_items += [_pg("Next", page + 1, page >= total_pages), _pg("Last", total_pages, page >= total_pages)]
        nav_html = f'<nav class="pg">{"".join(nav_items)}<span class="pg-info">Page {page}/{total_pages} ({total} entries)</span></nav>'
    search_val = esc(search_query) if search_query else ""
    search_result = ""
    if search_query:
        sort_label = "by time" if search_sort == "time" else "by relevance"
        alt_sort = "time" if search_sort == "relevance" else "relevance"
        sort_url = _url_prefix + _qs(f"q={esc(search_query)}", f"sort={alt_sort}")
        search_result = (
            f'<div class="search-info">Found {total} matching entries for "<strong>{esc(search_query)}</strong>" '
            f'(sorted {sort_label}) &middot; <a href="{sort_url}">sort by {alt_sort}</a></div>'
        )
    filt_qs = _qs("filter=prompts") if filter_mode != "prompts" else _qs()
    prompts_filter_url = _url_prefix + filt_qs if filt_qs else _url_prefix.rstrip("?")
    filter_banner = ""
    if filter_mode == "prompts":
        clear_url = _url_prefix + _qs() if _qs() else _url_prefix.rstrip("?")
        filter_banner = f'<div class="search-info">Showing prompts only — <a href="{clear_url}">show all</a></div>'
    return (
        _transcript_html_head(name, esc)
        + _transcript_html_nav(name, stats, prompts_filter_url, esc)
        + _transcript_html_entries(
            page_entries,
            _tool_results,
            search_val,
            filter_banner,
            search_result,
            nav_html,
            live_base_url,
            name,
            token,
            search_query,
            total,
            per_page,
            page,
            total_pages,
            esc,
            session_id,
            filter_mode,
        )
        + _transcript_html_footer(sid, stats, file_size_str, total, page, total_pages, esc)
        + _transcript_html_search_js()
    )
