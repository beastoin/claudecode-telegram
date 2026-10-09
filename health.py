"""Watchdog loop and host health monitoring.

Extracted from bridge.py — the _BridgeModule.__getattr__/__setattr__
forwards bridge.X lookups and patches here automatically.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
from pathlib import Path
from typing import Any, Callable, cast

from core import (
    _log, _LOG_ERROR, _LOG_WARN, _LOG_DEBUG,
    _clock, _subprocess_runner,
    admin_chat_id, _node_name,
    ALERT_COOLDOWN, CPU_ACTIVE, CPU_HOG_DURATION_MIN, CPU_IDLE,
    DELAY_BRIEF, HOST_DOWN_THRESHOLD, IDLE_STREAK_STUCK, START_GRACE,
    STALE_PENDING, THINK_GRACE, TIMEOUT_GIT_OP, TIMEOUT_REMOTE_CMD,
    TIMEOUT_TMUX_CHECK, TIMEOUT_TMUX_SEND, TOOL_GAP_GRACE,
    WATCHDOG_INTERVAL, SESSIONS_DIR, TMUX_PREFIX,
    _res_cfg,
)
from claudecode import (
    CpuHogEntry, DiskUsageDict, IoUsageDict, MemUsageDict,
    ProcStatsEntry, QuestionDetails, TmuxActivityResult,
    TmuxSessionDict, WorkerStateEntry, WorktreeItemDict,
    _capture_pane_text, _find_codex_transcript,
    _INTERACTIVE_CONTENT, _INTERACTIVE_FOOTERS,
    _remote_run, _remap_path, _tmux_pane_pids,
    get_backend, get_session_dir, get_worker_backend,
    HOOK_FAILURE_THRESHOLD, HOOK_FAILURE_WINDOW, normalize_backend,
    POISON_PATTERNS,
    host_health, processes, watchdog,
)
import time

from core import DEFAULT_BACKEND

# ---------------------------------------------------------------------------
# Team formatting (pure functions, runtime state injected by wrappers below)
# ---------------------------------------------------------------------------
def _normalize_activity(raw: str) -> str:
    if not raw: return raw
    m = re.match(r'^([A-Z][a-z]+ing)\s*(?:\((.+)\))?\s*$', raw)
    if m:
        verb = m.group(1); dur = m.group(2)
        if dur: return f"Thinking ({dur})"
        return "Thinking"
    return raw
def _team_attention_summary(watchdog_status: str, activity: str) -> tuple[str, str, int]:
    status = (watchdog_status or "").lower(); act = (activity or "").lower()
    if "rate limit" in act: return "🔴", "rate limit", 0
    if "error" in act or "traceback" in act or "not running" in act or "failed" in act: return "🔴", "error", 0
    if "needs input" in status or "needs reply" in status: return "🟡", "needs reply", 1
    if "stuck" in status or "no progress" in status: return "🔴", "stuck", 0
    if "poisoned" in status or "error loop" in status: return "🔴", "error loop", 0
    if "dead" in status or "not responding" in status: return "🔴", "stopped", 0
    if "offline" in status: return "🔴", "offline", 0
    if "exited" in status or "session ended" in status: return "🔴", "session ended", 0
    waiting_signals = (
        "waiting for",
        "awaiting",
        "approval",
        "accept edits",
        "confirm",
        "in plan mode", )
    if "working (waiting)" in status or any(sig in act for sig in waiting_signals): return "🟡", "needs reply", 1
    return "🟢", "ok", 2
def _format_watchdog_status_pure(name: str,
                            pending_lookup: Callable[[str], bool] | None = None,
                            state_snapshot: dict[str, WorkerStateEntry] | None = None,
                            clock_now: float | None = None) -> str:
    if pending_lookup is None: pending_lookup = lambda _: False  # noqa: E731
    if clock_now is None: clock_now = time.time()
    entry = state_snapshot.get(name) if state_snapshot else None
    if not entry: return "Working" if pending_lookup(name) else "Ready"
    state, _reason, since = entry
    now = clock_now
    _SIMPLE = {"READY": "Ready", "BUSY_TOOL": "Working", "BUSY_THINKING": "Thinking",
               "WAITING": "Working", "DEAD": "Not responding", "HOST_OFFLINE": "Host offline",
               "OFFLINE": "Offline", "EXITED": "Session ended", "UNTRACKED_BUSY": "Working"}
    if state in _SIMPLE: return _SIMPLE[state]
    minutes = max(0, int((now - since) / 60)) if since else 0
    if state == "WAITING_INPUT": return f"Needs reply ({minutes}m)"
    if state == "STUCK":
        age_match = re.search(r"age=(\d+)s", _reason) if _reason else None
        if age_match: minutes = int(age_match.group(1)) // 60
        return f"No progress ({minutes}m)"
    if state == "POISONED": return f"Error loop ({minutes}m)"
    return state.lower()
def _format_team_lines_pure(
    registered: dict[str, TmuxSessionDict],
    active: str | None,
    pending_lookup: Callable[[str], bool] | None = None,
    worker_live: dict[str, TmuxSessionDict] | dict[str, dict[str, str | None]] | None = None,
    *,
    state_snapshot: dict[str, WorkerStateEntry] | None = None,
    clock_now: float | None = None,
    normalize_backend_fn: Callable[[str | None], str] | None = None,
) -> list[str]:
    if pending_lookup is None: pending_lookup = lambda _: False  # noqa: E731
    if worker_live is None: worker_live = {}
    if state_snapshot is None: state_snapshot = {}
    if clock_now is None: clock_now = time.time()
    if normalize_backend_fn is None: normalize_backend_fn = lambda b: b or DEFAULT_BACKEND  # noqa: E731
    backend_values = set()
    for name, session in registered.items():
        live = worker_live.get(name, {}); backend = normalize_backend_fn(live.get("backend") or session.get("backend"))
        backend_values.add(backend)
    show_backend = len(backend_values) > 1; rows = []; counts = {"🔴": 0, "🟡": 0, "🟢": 0}
    for name in sorted(registered.keys()):
        session = registered[name]
        watchdog_status = _format_watchdog_status_pure(name, pending_lookup,
                                                  state_snapshot=state_snapshot,
                                                  clock_now=clock_now)
        live = worker_live.get(name, {}); backend = normalize_backend_fn(live.get("backend") or session.get("backend"))
        raw_activity = str(live.get("activity") or "").strip()
        if not raw_activity or raw_activity == "Unknown": raw_activity = watchdog_status
        activity = _normalize_activity(raw_activity)
        if len(activity) > 42: activity = activity[:39].rstrip() + "..."
        context_pct = str(live.get("context_pct") or "").strip()
        icon, blocker, severity_rank = _team_attention_summary(watchdog_status, raw_activity)
        counts[icon] += 1
        name_cell = f"{name} 🎯" if name == active else name
        ctx_part = f" | ctx {context_pct}" if context_pct and context_pct != "--" else ""
        row = f"{icon} {name_cell} — {activity}{ctx_part}"
        if show_backend: row += f" | backend={backend}"
        focus_rank = 0 if name == active else 1
        rows.append((severity_rank, focus_rank, name, blocker, row))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    attention_rows = [f"{name} ({blocker})" for rank, _focus, name, blocker, _row in rows if rank < 2]; lines = []
    focused = active or "(none)"
    lines.append(
        f"Team: {len(registered)} agents · focused: {focused} | "
        f"🟢 {counts['🟢']} ok · 🟡 {counts['🟡']} need reply · 🔴 {counts['🔴']} blocked" )
    if attention_rows: lines.append("Needs your reply: " + ", ".join(attention_rows))
    lines.extend(row for _rank, _focus, _name, _blocker, row in rows)
    return lines

# Resource thresholds — derived from _res_cfg (same source as bridge.py)
DISK_WARN_THRESHOLD_PCT = _res_cfg.disk_warn_pct
DISK_ALERT_THRESHOLD_PCT = _res_cfg.disk_alert_pct
DISK_ALERT_THRESHOLD_GB = _res_cfg.disk_alert_gb
DISK_ALERT_COOLDOWN = _res_cfg.disk_cooldown
CPU_HOG_THRESHOLD_PCT = _res_cfg.cpu_hog_pct
CPU_HOG_ALERT_COOLDOWN = _res_cfg.cpu_hog_cooldown
WORKTREE_ALERT_THRESHOLD_GB = _res_cfg.worktree_threshold_gb
WORKTREE_ALERT_COOLDOWN = _res_cfg.worktree_cooldown
MEM_ALERT_THRESHOLD_PCT = _res_cfg.mem_threshold_pct
MEM_ALERT_THRESHOLD_GB = _res_cfg.mem_threshold_gb
MEM_ALERT_COOLDOWN = _res_cfg.mem_cooldown
IO_ALERT_IOWAIT_PCT = _res_cfg.io_iowait_pct
IO_ALERT_COOLDOWN = _res_cfg.io_cooldown
INFRA_ALERT_COOLDOWN = _res_cfg.infra_cooldown

# ---------------------------------------------------------------------------
# Late-import helpers for bridge-resident functions
# ---------------------------------------------------------------------------

def get_worker_host(name: str) -> str | None:
    import bridge as _b; return _b.get_worker_host(name)

def get_registered_sessions(registered: dict[str, TmuxSessionDict] | None = None) -> dict[str, TmuxSessionDict]:
    import bridge as _b; return _b.get_registered_sessions(registered)

def _pending_timestamp(name: str) -> float | None:
    import bridge as _b; return _b._pending_timestamp(name)

def clear_pending(name: str) -> None:
    import bridge as _b; _b.clear_pending(name)

def is_pending(name: str) -> bool:
    import bridge as _b; return _b.is_pending(name)

# ---------------------------------------------------------------------------
# Activity detection helpers
# ---------------------------------------------------------------------------

def _read_noninteractive_activity(worker_name: str) -> str:
    with processes.adapter_pids_lock: entry = processes.adapter_pids.get(worker_name)
    if entry:
        proc, _ = entry
        if proc.poll() is None: return "adapter running"
    host = get_worker_host(worker_name); path = _find_codex_transcript(worker_name, host=host)
    if path:
        try:
            if host:
                r = _remote_run(["stat", "-c", "%Y", path], host=host,
                                capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r.returncode == 0: mtime = float(r.stdout.strip()); age = int(_clock.time() - mtime)
                else: age = -1
            else: mtime = os.path.getmtime(path); age = int(_clock.time() - mtime)
            if age >= 0:
                if age < 60: return f"idle (last response {age}s ago)"
                elif age < 3600: return f"idle (last response {age // 60}m ago)"
                else: return f"idle (last response {age // 3600}h ago)"
        except (subprocess.SubprocessError, OSError, ValueError) as exc: _log(_LOG_DEBUG, "probe:unknown", f"{type(exc).__name__}: {exc}")
    return "idle"
def _check_hook_failure_signal(name: str) -> str | None:
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"; host = get_worker_host(name)
    if host:
        try:
            r = _remote_run(["cat", signal_path], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            raw = r.stdout.strip()
        except (subprocess.SubprocessError, OSError): return None
    else:
        signal_file = Path(signal_path)
        if not signal_file.exists(): return None
        try: raw = signal_file.read_text().strip()
        except OSError: return None
    if not raw: return None
    lines = raw.splitlines(); cutoff = int(_clock.time()) - HOOK_FAILURE_WINDOW; recent = 0
    for line in lines:
        parts = line.split(None, 1)
        if not parts: continue
        try: ts = int(parts[0])
        except ValueError: continue
        if ts >= cutoff: recent += 1
    if recent >= HOOK_FAILURE_THRESHOLD: return f"hook failure signal: {recent} tool failures in {HOOK_FAILURE_WINDOW}s"
    return None
def _clear_hook_failures(name: str) -> None:
    signal_path = f"/tmp/claudecode-telegram/{_node_name}/{name}/hooks/failures"; host = get_worker_host(name)
    if host:
        try:
            _remote_run(["rm", "-f", signal_path], host=host, capture_output=True, timeout=TIMEOUT_TMUX_SEND)
        except (subprocess.SubprocessError, OSError) as exc: _log(_LOG_DEBUG, "probe:_clear_hook_failures", f"{type(exc).__name__}: {exc}")
    else:
        try: Path(signal_path).unlink(missing_ok=True)
        except OSError as exc: _log(_LOG_DEBUG, "io:_clear_hook_failures", f"{type(exc).__name__}: {exc}")
def _detect_poisoned(name: str, tmux_name: str) -> str | None:
    hook_reason = _check_hook_failure_signal(name)
    if hook_reason: return hook_reason
    backend_name = get_worker_backend(name); backend = get_backend(backend_name); host = get_worker_host(name); text_parts = []
    if backend.is_interactive: text_parts.append(_capture_pane_text(tmux_name, host=host))
    else: text_parts.append(_check_adapter_log(name))
    combined = "\n".join([part for part in text_parts if part])
    if not combined: return None
    for pattern in POISON_PATTERNS:
        if len(pattern.findall(combined)) >= 3: return pattern.pattern
    return None
def _activity_from_spinner(stripped: list[str]) -> str | None:
    _ACTIVE_SPINNER_CHARS = {"·", "*", "✢", "✦", "✧", "✹", "✵", "∙", "•", "✻"}
    for raw in reversed(stripped):
        first = raw[0] if raw else ""
        if first == "✻" and "…" not in raw and "..." not in raw: continue
        if first not in _ACTIVE_SPINNER_CHARS: continue
        match = re.match(r'^.\s+(.+?)(?:…|\.{3})\s*\(([^()]+)\)\s*$', raw)
        if match: verb = match.group(1).strip(); dur = match.group(2).split('·')[0].strip(); return f"{verb} ({dur})"
        verb_match = re.match(r'^.\s+(.+?)(?:…|\.{3})?\s*$', raw)
        if verb_match:
            verb = verb_match.group(1).strip(); dur_match = re.search(r'(\d+m?\s*\d*\.?\d*s)', raw)
            return f"{verb} ({dur_match.group(1).strip()})" if dur_match else verb
    return None
def _activity_from_tool(stripped: list[str]) -> str | None:
    last_running_tool = None
    for i, raw in enumerate(stripped):
        tool_match = re.match(r'^●\s*([A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*)\(', raw)
        if not tool_match: continue
        tool = tool_match.group(1)
        for j in range(i + 1, min(i + 6, len(stripped))):
            line = stripped[j]
            if not line: continue
            if line.startswith("⎿"):
                if "Running" in line and "background" not in line: last_running_tool = tool
                break
    if last_running_tool:
        if last_running_tool.startswith("mcp__"):
            parts = last_running_tool.split("__")
            last_running_tool = ".".join(parts[1:]) if len(parts) > 1 else last_running_tool
        return f"Running {last_running_tool}"
    return None
def _activity_from_rate_limit(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        lower = raw.lower()
        if "rate limit" in lower: return "Rate limited — waiting to retry"
        if "connection error" in lower and "retrying" in lower: return "Connection error — retrying"
        if lower.startswith("retrying") or "retrying in" in lower: return "Retrying API request"
    return None
def _activity_from_interactive(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                for question_line in stripped:
                    if "☐" in question_line:
                        question = question_line.replace("☐", "").strip()
                        if question: return f"Waiting for input: {question}"
                return "Waiting for user input"
    for raw in stripped:
        for pattern in _INTERACTIVE_CONTENT:
            if pattern in raw:
                if "plan" in raw.lower() and ("proceed" in raw.lower() or "execute" in raw.lower()): return "Waiting for plan approval"
                if "plan mode" in raw.lower(): return "Waiting for plan mode decision"
                if raw.startswith("Allow "): return "Waiting for tool permission"
                return "Waiting for user input"
    return None
def _activity_from_prompt(stripped: list[str]) -> str | None:
    last_prompt_idx = None; last_plan_bar_idx = None
    for i, raw in enumerate(stripped):
        if raw.startswith("❯"): last_prompt_idx = i
        if raw.startswith("⏸"): last_plan_bar_idx = i
    if last_plan_bar_idx is not None:
        if last_prompt_idx is None or last_prompt_idx < last_plan_bar_idx: return "In plan mode"
    if last_prompt_idx is not None: return "Ready"
    return None
_REVERSE_SCAN_PATTERNS: list[tuple[str, str]] = [
    ("Save and close editor to continue", "Waiting for external editor"),
    ("Running SessionStart", "Running SessionStart hooks"),
    ("Running PreCompact", "Running PreCompact hooks"),
    ("Do you want to proceed?", "Waiting for plan approval"),
    ("Would you like to proceed?", "Waiting for plan approval"),
    ("Exit plan mode?", "In plan mode"), ("Entering plan mode", "In plan mode"),
    ("Waiting for team lead", "Waiting for team lead approval"), ]
def _activity_from_reverse_scan(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        for needle, result in _REVERSE_SCAN_PATTERNS:
            if needle in raw: return result
    return None
def _activity_from_tasks(stripped: list[str]) -> str | None:
    done = 0; total = 0
    for raw in stripped:
        line = raw.lstrip()
        if line.startswith("✔") or line.startswith("✅"): done += 1; total += 1
        elif line.startswith("◻"): total += 1
    if total >= 2: return f"Tasks ({done}/{total} done)"
    return None
def _is_output_block_end(text: str) -> bool:
    trimmed = text.lstrip()
    if trimmed.startswith("Context left until auto-compact:"): return True
    return trimmed.startswith(("●", "·", "*", "✻", "─", "❯", "⏵", "⏸"))
def _activity_from_output_block(stripped: list[str]) -> str | None:
    for i in range(len(stripped) - 1, -1, -1):
        raw = stripped[i]
        if not raw.startswith("●"): continue
        if re.match(r'^●\s*[A-Za-z][A-Za-z0-9_]*(?:__[A-Za-z0-9_]+)*\(', raw): continue
        parts: list[str] = []
        head = re.sub(r'^●\s*', '', raw).strip()
        if head and not head.startswith("⎿") and not head.startswith("(ctrl+"): parts.append(head)
        j = i + 1
        while j < len(stripped):
            nxt = stripped[j]
            if _is_output_block_end(nxt): break
            text = nxt.strip()
            if text and not text.startswith("⎿") and not text.startswith("(ctrl+"): parts.append(text)
            j += 1
        if parts:
            msg = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
            if len(msg) > 120: msg = msg[:117].rstrip() + "..."
            return msg
    return None
def _activity_from_error(stripped: list[str]) -> str | None:
    for raw in reversed(stripped):
        if re.match(r'^(FAIL|ERROR|Error|Traceback|Fail)\b', raw, re.IGNORECASE):
            lower = raw.lower()
            if lower.startswith("error"):
                tail = raw[len("Error"):].lstrip(": ").strip()
                return f"Error: {tail}" if tail else "Error"
            return f"Error: {raw[:60]}"
    return None
def _extract_activity(lines: list[str]) -> str:
    if not lines: return "Active"
    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped: return "Idle"
    _CHECKS: list[Callable[[list[str]], str | None]] = [
        _activity_from_spinner, _activity_from_tool, _activity_from_rate_limit,
        _activity_from_interactive, _activity_from_prompt, _activity_from_reverse_scan,
        _activity_from_tasks, _activity_from_output_block, _activity_from_error]
    for check in _CHECKS:
        result: str | None = check(stripped)
        if result is not None: return result
    return "Active"
def _extract_context_pct(lines: list[str]) -> str | None:
    for line in reversed(lines):
        m = re.search(r'Context left.*?(\d+)%', line)
        if m: return f"{m.group(1)}%"
    return None
def _read_tmux_activity(tmux_name: str, host: str | None = None) -> TmuxActivityResult:
    try:
        if host:
            proc = _remote_run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
        else:
            proc = _subprocess_runner.run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p"],
                capture_output=True, text=True, timeout=TIMEOUT_TMUX_CHECK )
        if proc.returncode != 0: return TmuxActivityResult("Unknown", None, None)
        lines = proc.stdout.split("\n"); tail = lines[-40:]
        return TmuxActivityResult(_extract_activity(tail), _extract_context_pct(tail), tail)
    except (subprocess.SubprocessError, OSError): return TmuxActivityResult("Unknown", None, None)
def _extract_question_details(lines: list[str]) -> QuestionDetails | None:
    if not lines: return None
    stripped = [l.strip() for l in lines if l.strip()]
    if not stripped: return None
    tail = stripped[-5:]
    if any(line == "❯" for line in tail): return None
    has_interactive = False
    for raw in reversed(stripped):
        for footer in _INTERACTIVE_FOOTERS:
            if footer in raw:
                has_interactive = True
                break
        if has_interactive: break
    if not has_interactive:
        for raw in stripped:
            for pattern in _INTERACTIVE_CONTENT:
                if pattern in raw:
                    has_interactive = True
                    break
            if has_interactive: break
    if not has_interactive: return None
    header = ""
    for raw in stripped:
        if "☐" in raw:
            header = raw.replace("☐", "").strip()
            break
    options = []; selected_num = 0; opt_re = re.compile(r'^(❯)?\s*(\d+)\.\s+(.+)')
    for raw in stripped:
        m = opt_re.match(raw)
        if m:
            is_selected = m.group(1) == "❯"; num = int(m.group(2)); label = m.group(3).strip()
            options.append({"num": num, "label": label, "selected": is_selected})
            if is_selected: selected_num = num
    if not options: return None
    return cast(QuestionDetails, { "header": header, "options": options, "selected_num": selected_num,
    })
def _send_interactive_reply(tmux_name: str, reply: str, details: QuestionDetails, host: str | None = None) -> bool:
    reply = reply.strip().lower()
    if reply in ("skip", "cancel", "esc"):
        _remote_run(["tmux", "send-keys", "-t", tmux_name, "Escape"], host=host, timeout=TIMEOUT_TMUX_SEND)
        return True
    if reply.isdigit():
        target_num = int(reply); option_nums = [o["num"] for o in details["options"]]
        if target_num not in option_nums: return False
        target_idx = option_nums.index(target_num); current_idx = 0
        for i, o in enumerate(details["options"]):
            if o["selected"]:
                current_idx = i
                break
        diff = target_idx - current_idx; keys = []
        if diff > 0: keys = ["Down"] * diff
        elif diff < 0: keys = ["Up"] * abs(diff)
        keys.append("Enter")
        for key in keys:
            _remote_run(["tmux", "send-keys", "-t", tmux_name, key], host=host, timeout=TIMEOUT_TMUX_SEND)
            _clock.sleep(DELAY_BRIEF)
        return True
    return False

# ---------------------------------------------------------------------------
# Process inspection helpers
# ---------------------------------------------------------------------------

def _get_claude_pid(pane_pid: str, host: str | None = None) -> str | None:
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pane_pid), "-f", "claude"],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return None
    if result.returncode != 0: return None
    output = result.stdout.strip().splitlines()
    if not output: return None
    return output[0].strip()
def _child_count(pid: str, host: str | None = None) -> int:
    if not pid: return 0
    try:
        result = _remote_run(
            ["pgrep", "-P", str(pid)],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return 0
    if result.returncode != 0: return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])
def _ps_stats(pids: list[str], host: str | None = None) -> dict[str, ProcStatsEntry]:
    pid_list = [str(pid) for pid in pids if pid]
    if not pid_list: return {}
    try:
        result = _remote_run(
            ["ps", "-o", "pid=,%cpu=,state=", "-p", ",".join(pid_list)],
            host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND )
    except (subprocess.SubprocessError, OSError): return {}
    if result.returncode != 0: return {}
    stats = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 3: continue
        pid = parts[0]
        try: cpu = float(parts[1])
        except ValueError: cpu = 0.0
        state = parts[2]
        stats[pid] = cast(ProcStatsEntry, {"cpu": cpu, "state": state})
    return stats

# ---------------------------------------------------------------------------
# Worker state computation
# ---------------------------------------------------------------------------

def compute_state(
    tmux_exists: bool,
    claude_pid: str | None,
    pending: bool,
    pending_ts: int | None,
    pending_age: float,
    children: int,
    last_child_ts: float,
    cpu: float,
    last_hook_ts: float | None,
    last_seen_claude: float | None,
    now: float,
    is_interactive: bool = True,
    adapter_alive: bool = False,
    poisoned_reason: str | None = None,
) -> tuple[str, str]:
    if not tmux_exists: return "OFFLINE", "tmux missing"
    if not is_interactive:
        if adapter_alive: return "BUSY_TOOL", "adapter running"
        if pending:
            if pending_age < STALE_PENDING: return "WAITING", f"age={int(pending_age)}s"
            hook_since_pending = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
            if pending_age >= STALE_PENDING and not hook_since_pending:
                if poisoned_reason is not None: return "POISONED", f"{poisoned_reason}"
                return "STUCK", f"age={int(pending_age)}s"
            return "WAITING", f"age={int(pending_age)}s"
        return "READY", "idle"
    if not claude_pid and last_seen_claude is not None:
        if (now - last_seen_claude) > START_GRACE: return "DEAD", f"claude missing {int(now - last_seen_claude)}s"
    if pending and children > 0: return "BUSY_TOOL", f"children={children}"
    if pending and children == 0:
        if (pending_age <= THINK_GRACE) or ((now - last_child_ts) <= TOOL_GAP_GRACE) or (cpu >= CPU_ACTIVE):
            return "BUSY_THINKING", f"age={int(pending_age)}s cpu={cpu:.1f}"
        if pending_age < STALE_PENDING: return "WAITING", f"age={int(pending_age)}s"
        hook_since_pending = last_hook_ts is not None and pending_ts is not None and last_hook_ts > pending_ts
        if pending_age >= STALE_PENDING and cpu < CPU_IDLE and not hook_since_pending:
            if poisoned_reason is not None: return "POISONED", f"{poisoned_reason}"
            return "STUCK", f"age={int(pending_age)}s cpu={cpu:.1f}"
        return "WAITING", f"age={int(pending_age)}s"
    if not pending and children > 0: return "UNTRACKED_BUSY", f"children={children}"
    if claude_pid and not pending: return "READY", "idle"
    return "OFFLINE", "tmux alive, claude missing"
def _check_adapter_log(name: str, tail_lines: int = 20) -> str:
    if tail_lines <= 0: return ""
    host = get_worker_host(name)
    if host:
        try:
            remote_log = _remap_path(str(get_session_dir(name) / "adapter.log"), host)
            r = _remote_run(["tail", "-n", str(tail_lines), remote_log], host=host,
                            capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            return r.stdout if r.returncode == 0 else ""
        except (subprocess.SubprocessError, OSError): return ""
    log_path = get_session_dir(name) / "adapter.log"
    if not log_path.exists(): return ""
    try:
        with log_path.open("r", errors="ignore") as fh: lines = fh.readlines()
        return "".join(lines[-tail_lines:])
    except OSError: return ""
def _format_watchdog_status(name: str,
                            pending_lookup: Callable[[str], bool] | None = None,
                            state_snapshot: dict[str, WorkerStateEntry] | None = None) -> str:
    if pending_lookup is None: pending_lookup = is_pending
    if state_snapshot is None:
        with watchdog.lock: state_snapshot = dict(watchdog.worker_states)
    return _format_watchdog_status_pure(
        name, pending_lookup=pending_lookup,
        state_snapshot=state_snapshot, clock_now=_clock.time(), )
def format_team_lines(  # type: ignore[no-redef]
    registered: dict[str, TmuxSessionDict],
    active: str | None,
    pending_lookup: Callable[[str], bool] | None = None,
    worker_live: dict[str, TmuxSessionDict] | dict[str, dict[str, str | None]] | None = None
) -> list[str]:
    if pending_lookup is None: pending_lookup = is_pending
    with watchdog.lock: state_snapshot = dict(watchdog.worker_states)
    return _format_team_lines_pure(
        registered, active,
        pending_lookup=pending_lookup, worker_live=worker_live,
        state_snapshot=state_snapshot, clock_now=_clock.time(),
        normalize_backend_fn=normalize_backend, )

# ---------------------------------------------------------------------------
# Watchdog alert sending
# ---------------------------------------------------------------------------

def _watchdog_alert(category: str, text: str | None) -> None:
    if not text or not admin_chat_id: return
    try:
        import telegram as _tg
        _tg.transport.send_text(admin_chat_id, text)
        _log(_LOG_WARN, "watchdog", f"{category}: {text.splitlines()[0]}")
    except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_ERROR, "watchdog", f"{category} error: {e}")

def _send_watchdog_alert(name: str, state: str, reason: str) -> None:
    if admin_chat_id is None: return
    now = _clock.time()
    with watchdog.lock: last = watchdog.last_alert_ts.get(name)
    if last and (now - last) < ALERT_COOLDOWN:
        _log(_LOG_WARN, "watchdog", f"Alert suppressed for {name} ({state}): cooldown {now - last:.0f}s < {ALERT_COOLDOWN}s")
        return
    if state == "WAITING_INPUT":
        with watchdog.lock: details = watchdog.waiting_input_details.get(name)
        header = details.get("header", "") if details else ""; title = f"🟡 {name} needs your reply"
        if header: title += f": {header}"
        parts = [title]
        if details and details.get("options"):
            for o in details["options"]:
                marker = "➔ " if o.get("selected") else "  "
                parts.append(f"{marker}{o['num']}. {o['label']}")
            max_num = max(o["num"] for o in details["options"])
            parts.append(f"\nReply 1-{max_num} to choose, or \"skip\" to cancel.")
        text = "\n".join(parts)
    elif state == "STUCK":
        age_match = re.search(r"age=(\d+)s", reason); age_min = int(age_match.group(1)) // 60 if age_match else 0
        age_str = f"{age_min}min" if age_min > 0 else reason.split()[0]
        text = f"🔴 {name} has made no progress for {age_str}.\n/restart --clean {name} (starts fresh)"
    elif state == "POISONED": text = f"🔴 {name} is stuck in an error loop.\n/restart --clean {name} (starts fresh)"
    elif state == "DEAD": text = f"🔴 {name} stopped unexpectedly.\n/restart --clean {name} (starts fresh)"
    elif state == "EXITED": text = f"🟡 {name}'s session ended.\n/restart {name}"
    elif state == "OFFLINE": text = f"🔴 {name} is not running.\n/hire {name}"
    else: text = f"{name}: {state} ({reason}). Check /team"
    try:
        import telegram as _tg
        result = _tg.transport.send_text(admin_chat_id, text)
        if result and result.get("ok"):
            _log(_LOG_WARN, "watchdog", f"Alert sent for {name} ({state}): {text[:80]}")
            _res = result.get("result", {}); msg_id = _res.get("message_id") if isinstance(_res, dict) else None
            with watchdog.lock:
                watchdog.last_alert_ts[name] = now
                if msg_id: watchdog.alert_msg_ids[name] = (msg_id, text)
        else: _log(_LOG_WARN, "watchdog", f"Alert FAILED for {name} ({state}): {result}")
    except KeyError as e: _log(_LOG_ERROR, "watchdog", f"Watchdog alert error: {e}")

# ---------------------------------------------------------------------------
# Host probe recording
# ---------------------------------------------------------------------------

def _record_host_probe(host: str, ok: bool, error: str | None = None) -> None:
    now = _clock.time()
    with watchdog.lock:
        was_down = host_health.down.get(host, False)
        if ok:
            host_health.ssh_failures[host] = 0; host_health.last_error.pop(host, None)
            if was_down:
                host_health.down[host] = False
                down_since = host_health.down_since.pop(host, now); duration = int(now - down_since)
                workers_on_host = [n for n, s in get_registered_sessions().items() if get_worker_host(n) == host]
                _do_send = True; alert_text = f"✅ Host BACK UP: {host}\nWas down for {duration // 60}m {duration % 60}s\nWorkers affected: {', '.join(workers_on_host) or 'none'}"
            else: _do_send = False; alert_text = None
        else:
            failures = host_health.ssh_failures.get(host, 0) + 1
            host_health.ssh_failures[host] = failures; host_health.last_error[host] = error or "ssh probe failed"
            if not was_down and failures >= HOST_DOWN_THRESHOLD:
                host_health.down[host] = True; host_health.down_since[host] = now
                workers_on_host = [n for n, s in get_registered_sessions().items() if get_worker_host(n) == host]
                _do_send = True; alert_text = f"🔴 Host DOWN: {host}\nAfter {failures} consecutive SSH failures\nError: {error or 'unknown'}\nWorkers affected: {', '.join(workers_on_host) or 'none'}"
            else: _do_send = False; alert_text = None
    if _do_send: _watchdog_alert("Host", alert_text)

def _is_host_down(host: str) -> bool:
    with watchdog.lock: return host_health.down.get(host, False)

# ---------------------------------------------------------------------------
# Resource checks: disk, memory, IO, CPU hogs, worktrees, tailscale
# ---------------------------------------------------------------------------

def _check_disk_usage(host: str | None = None) -> DiskUsageDict | None:
    is_mac = bool(host and "mac" in host.lower())
    try:
        cmd = ["df", "-g", "/"] if is_mac else ["df", "-BG", "--output=size,used,avail,pcent", "/"]
        r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2: return None
        parts = lines[1].split()
        if is_mac:
            if len(parts) < 6: return None
            return {"pct": int(parts[4].rstrip("%")), "free_gb": float(parts[3]), "total_gb": float(parts[1])}
        if len(parts) < 4: return None
        return {"pct": int(parts[3].rstrip("%")), "free_gb": float(parts[2].rstrip("G")), "total_gb": float(parts[0].rstrip("G"))}
    except (ValueError, KeyError): return None

def _check_mem_usage(host: str | None = None) -> MemUsageDict | None:
    is_mac = bool(host and "mac" in host.lower())
    try:
        if is_mac:
            r = _remote_run(["bash", "-c", "sysctl -n hw.memsize && vm_stat"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            lines = r.stdout.strip().splitlines()
            if len(lines) < 2: return None
            total = int(lines[0]); page_size = 16384; free_p = inactive_p = spec_p = 0
            for line in lines[1:]:
                if "page size of" in line:
                    try: page_size = int(line.split("page size of")[1].strip().rstrip("."))
                    except (ValueError, IndexError): pass
                elif "Pages free:" in line: free_p = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages inactive:" in line: inactive_p = int(line.split(":")[1].strip().rstrip("."))
                elif "Pages speculative:" in line: spec_p = int(line.split(":")[1].strip().rstrip("."))
            avail = (free_p + inactive_p + spec_p) * page_size; used = total - avail
        else:
            r = _remote_run(["free", "-b"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            mem_line = next((l for l in r.stdout.strip().splitlines() if l.startswith("Mem:")), None)
            if not mem_line: return None
            parts = mem_line.split(); total = int(parts[1]); used = int(parts[2])
            avail = int(parts[6]) if len(parts) >= 7 else total - used
        total_gb = total / (1024**3); used_gb = used / (1024**3); avail_gb = avail / (1024**3)
        pct = int((used / total) * 100) if total > 0 else 0
        top_procs = _get_top_mem_procs(host)
        return {"pct": pct, "used_gb": used_gb, "total_gb": total_gb, "avail_gb": avail_gb, "top_procs": top_procs}
    except (ValueError, KeyError): return None

def _get_top_mem_procs(host: str | None = None) -> list[dict[str, object]]:
    try:
        r = _remote_run(["ps", "aux", "--sort=-rss"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return []
        procs = []
        for line in r.stdout.strip().splitlines()[1:6]:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                procs.append(cast(dict[str, object], {"pid": parts[1], "rss_gb": round(int(parts[5]) / (1024 * 1024), 1), "pct": parts[3], "cmd": parts[10][:80]}))
        return procs
    except (ValueError, KeyError): return []

def _check_io_usage(host: str | None = None) -> IoUsageDict | None:
    is_mac = bool(host and "mac" in host.lower())
    if is_mac:
        try:
            r = _remote_run(["iostat", "-c", "2", "-w", "1"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode != 0: return None
            lines = r.stdout.strip().splitlines()
            if len(lines) < 3: return None
            parts = lines[-1].split()
            return {"iowait_pct": 0, "read_iops": int(float(parts[0])), "write_iops": int(float(parts[1])), "util_pct": 0} if len(parts) >= 6 else None
        except (ValueError, KeyError): return None
    try:
        r = _remote_run(["iostat", "-x", "-d", "1", "2", "-o", "JSON"], host=host, capture_output=True, text=True, timeout=TIMEOUT_REMOTE_CMD)
        if r.returncode == 0 and r.stdout.strip():
            data = cast(dict[str, object], json.loads(r.stdout)); _sysstat = data.get("sysstat", {})
            _hosts = _sysstat.get("hosts", [{}]) if isinstance(_sysstat, dict) else [{}]
            _host0 = _hosts[0] if isinstance(_hosts, list) and _hosts else {}
            stats = _host0.get("statistics", []) if isinstance(_host0, dict) else []
            if isinstance(stats, list) and len(stats) >= 2:
                disks = stats[-1].get("disk", []) if isinstance(stats[-1], dict) else []
                r_iops = sum(d.get("r/s", 0) for d in disks); w_iops = sum(d.get("w/s", 0) for d in disks)
                m_util = max((d.get("util", d.get("%util", 0)) for d in disks), default=0); iowait = 0.0
                r2 = _remote_run(["vmstat", "1", "2"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
                if r2.returncode == 0:
                    vlines = r2.stdout.strip().splitlines()
                    if len(vlines) >= 3:
                        vp = vlines[-1].split()
                        if len(vp) >= 16: iowait = float(vp[15])
                return {"iowait_pct": round(iowait, 1), "read_iops": round(r_iops), "write_iops": round(w_iops), "util_pct": round(m_util, 1)}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
        _log(_LOG_DEBUG, "parse:io", f"{type(exc).__name__}: {exc}")
    try:
        r = _remote_run(["vmstat", "1", "2"], host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return None
        lines = r.stdout.strip().splitlines()
        if len(lines) < 3: return None
        parts = lines[-1].split()
        if len(parts) < 16: return None
        return {"iowait_pct": round(float(parts[15]), 1), "read_iops": int(parts[8]), "write_iops": int(parts[9]), "util_pct": 0}
    except (ValueError, KeyError): return None

def _probe_metric(remote_hosts: set[str], *, check_fn: Callable[..., dict[str, Any] | None],  # type: ignore[explicit-any]
                  usage_store: dict[str, Any], alerted_store: dict[str, Any],  # type: ignore[explicit-any]
                  alert_ts_store: dict[str, float], cooldown: int | float,
                  is_critical_fn: Callable[[dict[str, Any]], bool],  # type: ignore[explicit-any]
                  alert_fmt: Callable[[str, dict[str, Any]], str],  # type: ignore[explicit-any]
                  recovery_fmt: Callable[[str, dict[str, Any]], str], category: str) -> None:  # type: ignore[explicit-any]
    """Generic host metric probe: check -> threshold -> alert with cooldown."""
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host): continue
        host_label = host or "VPS"
        usage = check_fn(host)
        if usage is None: continue
        is_crit = is_critical_fn(usage); alert_text = None
        with watchdog.lock:
            usage_store[host_label] = {**usage, "ts": now}
            was_alerted = alerted_store.get(host_label, False)
            if is_crit and not was_alerted:
                if now - alert_ts_store.get(host_label, 0) >= cooldown:
                    alert_text = alert_fmt(host_label, usage)
                    alert_ts_store[host_label] = now; alerted_store[host_label] = True
            elif not is_crit and was_alerted:
                alerted_store[host_label] = False; alert_text = recovery_fmt(host_label, usage)
        _watchdog_alert(category, alert_text)

def _probe_disk_all_hosts(remote_hosts: set[str]) -> None:
    _probe_metric(remote_hosts, check_fn=_check_disk_usage,  # type: ignore[arg-type]
        usage_store=host_health.disk_usage, alerted_store=host_health.disk_alerted,
        alert_ts_store=host_health.disk_alert_ts, cooldown=DISK_ALERT_COOLDOWN,
        is_critical_fn=lambda u: u["pct"] >= DISK_ALERT_THRESHOLD_PCT or u["free_gb"] < DISK_ALERT_THRESHOLD_GB,
        alert_fmt=lambda h, u: f"🔴 Disk space critical: {h}\nUsage: {u['pct']}% ({u['free_gb']:.1f}GB free of {u['total_gb']:.0f}GB)\nAction needed: clean up old files, worktrees, or logs",
        recovery_fmt=lambda h, u: f"✅ Disk space recovered: {h} — {u['pct']}% ({u['free_gb']:.1f}GB free)", category="Disk")

def _probe_mem_all_hosts(remote_hosts: set[str]) -> None:
    def _fmt(h: str, u: dict[str, Any]) -> str:  # type: ignore[explicit-any]
        text = f"🧠 Memory critical: {h}\nUsage: {u['pct']}% ({u['avail_gb']:.1f}GB available of {u['total_gb']:.0f}GB)"
        for p in u.get("top_procs", [])[:3]: text += f"\n  {p['pid']} {p['rss_gb']}GB {p['cmd']}"
        return text
    _probe_metric(remote_hosts, check_fn=_check_mem_usage,  # type: ignore[arg-type]
        usage_store=host_health.mem_usage, alerted_store=host_health.mem_alerted,
        alert_ts_store=host_health.mem_alert_ts, cooldown=MEM_ALERT_COOLDOWN,
        is_critical_fn=lambda u: u["pct"] >= MEM_ALERT_THRESHOLD_PCT or u["avail_gb"] < MEM_ALERT_THRESHOLD_GB,
        alert_fmt=_fmt, recovery_fmt=lambda h, u: f"✅ Memory recovered: {h} — {u['pct']}% ({u['avail_gb']:.1f}GB available)", category="Memory")

def _probe_io_all_hosts(remote_hosts: set[str]) -> None:
    _probe_metric(remote_hosts, check_fn=_check_io_usage,  # type: ignore[arg-type]
        usage_store=host_health.io_usage, alerted_store=host_health.io_alerted,
        alert_ts_store=host_health.io_alert_ts, cooldown=IO_ALERT_COOLDOWN,
        is_critical_fn=lambda u: u["iowait_pct"] >= IO_ALERT_IOWAIT_PCT,
        alert_fmt=lambda h, u: f"⚡ IO critical: {h}\nIO wait: {u['iowait_pct']}%\nIOPS: {u['read_iops']}r + {u['write_iops']}w" + (f" | disk util: {u['util_pct']}%" if u["util_pct"] else ""),
        recovery_fmt=lambda h, u: f"✅ IO recovered: {h} — iowait {u['iowait_pct']}%, IOPS {u['read_iops']}r+{u['write_iops']}w", category="IO")

def _get_cpu_hogs(host: str | None = None) -> list[CpuHogEntry]:
    is_mac = bool(host and "mac" in host.lower())
    try:
        cmd = ["ps", "-eo", "pid,pcpu,etime,comm", "-r"] if is_mac else ["ps", "-eo", "pid,pcpu,etime,comm", "--sort=-pcpu"]
        r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        if r.returncode != 0: return []
        hogs: list[CpuHogEntry] = []
        for line in r.stdout.strip().splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 4: continue
            try: pid = int(parts[0]); cpu = float(parts[1])
            except (ValueError, IndexError): continue
            if cpu < CPU_HOG_THRESHOLD_PCT: break
            etime_str = parts[2]; days = 0
            if "-" in etime_str: day_part, etime_str = etime_str.split("-", 1); days = int(day_part)
            tp = etime_str.split(":")
            try:
                mins = days * 1440 + (int(tp[0]) * 60 + int(tp[1]) if len(tp) == 3 else int(tp[0])) if len(tp) >= 2 else None
            except (ValueError, IndexError): mins = None
            if mins is None: continue
            hogs.append({"pid": pid, "cpu": cpu, "etime_min": mins, "cmd": parts[3][:80] if len(parts) > 3 else "?"})
        return hogs
    except (ValueError, KeyError): return []

def _probe_cpu_hogs(remote_hosts: set[str]) -> None:
    now = _clock.time()
    for host in [None, *remote_hosts]:
        if host and _is_host_down(host): continue
        host_label = host or "VPS"
        real_hogs = [h for h in _get_cpu_hogs(host) if h["etime_min"] >= CPU_HOG_DURATION_MIN]
        with watchdog.lock: host_health.cpu_hogs[host_label] = real_hogs
        if real_hogs and now - host_health.cpu_hog_alert_ts.get(host_label, 0) >= CPU_HOG_ALERT_COOLDOWN:
            lines = []
            for h in real_hogs[:5]:
                elapsed = f"{h['etime_min'] // 60}h{h['etime_min'] % 60}m" if h["etime_min"] >= 60 else f"{h['etime_min']}m"
                lines.append(f"  PID {h['pid']}: {h['cpu']}% CPU for {elapsed} — {h['cmd']}")
            host_health.cpu_hog_alert_ts[host_label] = now
            _watchdog_alert("CPU hog", f"🔥 Runaway process{'es' if len(real_hogs) > 1 else ''} on {host_label}:\n" + "\n".join(lines))

def _check_worktree_usage(host: str | None = None) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    is_mac = bool(host and "mac" in host.lower())
    try:
        du_flag = "-sk" if is_mac else "-sb"
        cmd = ["bash", "-c", f"find $HOME -maxdepth 4 -type d -name worktrees 2>/dev/null | while read d; do du {du_flag} \"$d\" 2>/dev/null; done"]
        r = _remote_run(cmd, host=host, capture_output=True, text=True, timeout=TIMEOUT_GIT_OP)
        if r.returncode != 0: return None
        items: list[WorktreeItemDict] = []; total_bytes = 0
        for line in (r.stdout.strip().splitlines() if r.stdout.strip() else []):
            parts = line.split(None, 1)
            if len(parts) < 2: continue
            try: size_val = int(parts[0])
            except (ValueError, IndexError): continue
            size_bytes = size_val * 1024 if is_mac else size_val; total_bytes += size_bytes
            items.append({"path": parts[1], "size_gb": round(size_bytes / (1024**3), 1)})
        return {"total_gb": round(total_bytes / (1024**3), 1), "items": items}
    except (urllib.error.URLError, OSError, TimeoutError): return None

def _probe_worktree_sizes(remote_hosts: set[str]) -> None:
    def _fmt(h: str, u: dict[str, Any]) -> str:  # type: ignore[explicit-any]
        top = sorted(u.get("items", []), key=lambda x: x["size_gb"], reverse=True)[:5]
        return f"📁 Worktree bloat on {h}: {u['total_gb']:.1f}GB total (threshold: {WORKTREE_ALERT_THRESHOLD_GB}GB)\nTop directories:\n" + "\n".join(f"  {it['size_gb']}GB — {it['path']}" for it in top)
    _probe_metric(remote_hosts, check_fn=_check_worktree_usage,
        usage_store=host_health.worktree_usage, alerted_store=host_health.worktree_alerted,
        alert_ts_store=host_health.worktree_alert_ts, cooldown=WORKTREE_ALERT_COOLDOWN,
        is_critical_fn=lambda u: u["total_gb"] >= WORKTREE_ALERT_THRESHOLD_GB,
        alert_fmt=_fmt, recovery_fmt=lambda h, u: f"✅ Worktree size recovered: {h} — {u['total_gb']:.1f}GB (below {WORKTREE_ALERT_THRESHOLD_GB}GB)", category="Worktree")

def _probe_tailscale() -> None:
    now = _clock.time()
    try:
        r = _subprocess_runner.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
        is_up = r.returncode == 0 and json.loads(r.stdout).get("BackendState") == "Running"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError): is_up = False
    alert_text = None
    with watchdog.lock:
        if not is_up and not host_health.tailscale_down:
            if now - host_health.tailscale_alert_ts >= INFRA_ALERT_COOLDOWN:
                host_health.tailscale_down = True; host_health.tailscale_alert_ts = now
                alert_text = "🚨 Tailscale is DOWN on VPS — 100.125.36.102 unreachable from external network.\nRun: sudo tailscale up"
        elif is_up and host_health.tailscale_down:
            host_health.tailscale_down = False; alert_text = "✅ Tailscale recovered — VPS reachable at 100.125.36.102"
    _watchdog_alert("Tailscale", alert_text)

# ---------------------------------------------------------------------------
# Watchdog transition & state recording
# ---------------------------------------------------------------------------

def _send_resolved_alert(name: str, new_state: str) -> None:
    if admin_chat_id is None: return
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    with watchdog.lock: prev_state = watchdog.prev_worker_states.get(name)
    if prev_state not in bad_states or new_state not in good_states: return
    restart_ts = watchdog.recent_restarts.get(name)
    if restart_ts and _clock.time() - restart_ts < 30: return
    now = _clock.time(); last_resolved = watchdog.last_resolved_ts.get(name, 0)
    if now - last_resolved < 180: return
    watchdog.last_resolved_ts[name] = now
    with watchdog.lock: alert_info = watchdog.alert_msg_ids.pop(name, None)
    if alert_info:
        old_msg_id, old_text = alert_info
        resolved_text = f"✅ {name} resolved (was: {old_text.splitlines()[0]})"
        try:
            import telegram as _tg
            _tg.transport.edit_message(admin_chat_id, old_msg_id, resolved_text)
            _log(_LOG_WARN, "watchdog", f"Edited alert for {name} -> resolved")
            return
        except (urllib.error.URLError, OSError, TimeoutError): pass
    text = f"✅ {name} is back to normal."
    try:
        import telegram as _tg
        _tg.transport.send_text(admin_chat_id, text)
    except (urllib.error.URLError, OSError, TimeoutError) as e: _log(_LOG_ERROR, "watchdog", f"Watchdog resolved alert error: {e}")

def _handle_watchdog_transition( name: str, state: str, reason: str, since: float, now: float | None = None,
) -> None:
    if now is None: now = _clock.time()
    bad_states = {"OFFLINE", "DEAD", "STUCK", "POISONED", "EXITED", "WAITING_INPUT", "HOST_OFFLINE"}
    good_states = {"READY", "BUSY_TOOL", "BUSY_THINKING"}
    with watchdog.lock: prev_state = watchdog.prev_worker_states.get(name)
    state_changed = prev_state is None or prev_state != state
    if state == "HOST_OFFLINE":
        with watchdog.lock: watchdog.prev_worker_states[name] = state
        return
    teleport_state_file = SESSIONS_DIR / name / "teleport_state"
    if state in {"OFFLINE", "DEAD", "EXITED"} and teleport_state_file.exists():
        with watchdog.lock: watchdog.prev_worker_states[name] = state
        return
    def eligible_for_alert() -> bool:
        if state in {"OFFLINE", "DEAD", "EXITED"}: return since is not None and (now - since) >= START_GRACE
        return True
    GOOD_PROBE_THRESHOLD = 3; BAD_PROBE_THRESHOLD = 3; is_remote = bool(get_worker_host(name))
    if state in bad_states:
        with watchdog.lock: watchdog.consecutive_good_probes[name] = 0
        if is_remote and state in {"OFFLINE", "DEAD"}:
            with watchdog.lock:
                watchdog.consecutive_bad_probes[name] = watchdog.consecutive_bad_probes.get(name, 0) + 1
                bad_count = watchdog.consecutive_bad_probes[name]
            if bad_count < BAD_PROBE_THRESHOLD: return
        if state_changed or prev_state is None:
            if eligible_for_alert():
                _log(_LOG_WARN, "watchdog", f"State change {name}: {prev_state} -> {state} ({reason}), sending alert")
                _send_watchdog_alert(name, state, reason)
        elif state in {"OFFLINE", "DEAD", "EXITED"} and eligible_for_alert(): _send_watchdog_alert(name, state, reason)
        with watchdog.lock: watchdog.prev_worker_states[name] = state
        return
    if state in good_states and prev_state in bad_states:
        with watchdog.lock:
            watchdog.consecutive_good_probes[name] = watchdog.consecutive_good_probes.get(name, 0) + 1
            watchdog.consecutive_bad_probes[name] = 0
            good_count = watchdog.consecutive_good_probes[name]
        if good_count >= GOOD_PROBE_THRESHOLD:
            _send_resolved_alert(name, state)
            with watchdog.lock:
                watchdog.consecutive_good_probes[name] = 0
                watchdog.prev_worker_states[name] = state
        return
    with watchdog.lock:
        watchdog.consecutive_good_probes[name] = 0
        watchdog.consecutive_bad_probes[name] = 0
        watchdog.prev_worker_states[name] = state

def _record_worker_state(name: str, state: str, reason: str, now: float) -> float:
    with watchdog.lock:
        prev = watchdog.worker_states.get(name)
        if prev and prev[0] == state: since = prev[2]
        else: since = now
        watchdog.worker_states[name] = WorkerStateEntry(state, reason, since)
    return since

# ---------------------------------------------------------------------------
# Main watchdog loop and sub-steps
# ---------------------------------------------------------------------------

def watchdog_loop() -> None:
    _disk_check_counter = 0
    while not watchdog.stop_event.is_set():
        try:
            now = _clock.time(); registered = get_registered_sessions(); pane_pids = _tmux_pane_pids()
            registered_names = set(registered.keys()); probe_failed = bool(registered_names) and not pane_pids
            _watchdog_update_probe_failures(registered_names, probe_failed)
            remote_workers, remote_pane_pids, failed_hosts = _watchdog_probe_remote_hosts(registered)
            claude_pids, tmux_present, backend_info = _watchdog_collect_worker_pids(
                registered, pane_pids, remote_pane_pids, now)
            stats = _watchdog_gather_cpu_stats(claude_pids)
            _watchdog_evaluate_workers(
                registered, tmux_present, claude_pids, backend_info, stats,
                probe_failed, failed_hosts, now)
            _watchdog_cleanup_stale(registered_names)
            _disk_check_counter += 1
            if _disk_check_counter >= 20:
                _disk_check_counter = 0
                _watchdog_resource_checks(set(remote_workers.keys()))
        except (subprocess.SubprocessError, ValueError, KeyError) as e: _log(_LOG_ERROR, "watchdog", f"Watchdog error: {e}")
        watchdog.stop_event.wait(WATCHDOG_INTERVAL)

def _watchdog_update_probe_failures(registered_names: set[str], probe_failed: bool) -> None:
    if probe_failed:
        for name in registered_names: watchdog.consecutive_probe_failures[name] = watchdog.consecutive_probe_failures.get(name, 0) + 1
    else:
        for name in registered_names: watchdog.consecutive_probe_failures[name] = 0

def _watchdog_probe_remote_hosts( registered: dict[str, TmuxSessionDict]
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str], set[str]]:
    remote_workers: dict[str, list[tuple[str, str]]] = {}
    for name, session in registered.items():
        host = get_worker_host(name)
        if host:
            tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}")
            remote_workers.setdefault(host, []).append((name, tmux_name))
    remote_pane_pids: dict[str, str] = {}
    failed_hosts: set[str] = set()
    for host, workers in remote_workers.items():
        try:
            r = _remote_run(
                ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
                host=host, capture_output=True, text=True, timeout=TIMEOUT_TMUX_SEND)
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1].isdigit(): remote_pane_pids[parts[0]] = parts[1]
                _record_host_probe(host, ok=True)
            else:
                failed_hosts.add(host)
                _record_host_probe(host, ok=False, error=f"tmux list-panes exit {r.returncode}")
        except (subprocess.SubprocessError, KeyError) as e:
            failed_hosts.add(host)
            _record_host_probe(host, ok=False, error=str(e)[:200])
    return remote_workers, remote_pane_pids, failed_hosts

def _watchdog_collect_worker_pids(
    registered: dict[str, TmuxSessionDict],
    pane_pids: dict[str, str],
    remote_pane_pids: dict[str, str],
    now: float
) -> tuple[dict[str, str], dict[str, bool], dict[str, 'Backend']]:
    from claudecode import Backend
    claude_pids: dict[str, str] = {}
    tmux_present: dict[str, bool] = {}
    backend_info: dict[str, Backend] = {}
    for name, session in registered.items():
        backend_name = get_worker_backend(name, session); backend = get_backend(backend_name)
        backend_info[name] = backend
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}"); host = get_worker_host(name)
        pane_pid = remote_pane_pids.get(tmux_name) if host else pane_pids.get(tmux_name); tmux_exists = bool(pane_pid)
        tmux_present[name] = tmux_exists
        if not tmux_exists: continue
        if backend.is_interactive and pane_pid:
            claude_pid = _get_claude_pid(pane_pid, host=host)
            if claude_pid:
                claude_pids[name] = claude_pid
                with watchdog.lock: watchdog.last_seen_claude[name] = now
            else:
                with watchdog.lock:
                    if name not in watchdog.last_seen_claude: watchdog.last_seen_claude[name] = now
    return claude_pids, tmux_present, backend_info

def _watchdog_gather_cpu_stats(claude_pids: dict[str, str]) -> dict[str, ProcStatsEntry]:
    pids_by_host: dict[str | None, list[str]] = {}
    for name, pid in claude_pids.items():
        host = get_worker_host(name)
        pids_by_host.setdefault(host, []).append(pid)
    stats: dict[str, ProcStatsEntry] = {}
    for host, pids in pids_by_host.items(): stats.update(_ps_stats(pids, host=host))
    return stats

def _watchdog_evaluate_workers(
    registered: dict[str, TmuxSessionDict],
    tmux_present: dict[str, bool],
    claude_pids: dict[str, str],
    backend_info: dict[str, 'Backend'],
    stats: dict[str, ProcStatsEntry],
    probe_failed: bool,
    failed_hosts: set[str],
    now: float
) -> None:
    from claudecode import Backend
    for name, session in registered.items():
        tmux_name = session.get("tmux", f"{TMUX_PREFIX}{name}"); tmux_exists = tmux_present.get(name, False)
        if not tmux_exists and "tmux" not in session:
            since = _record_worker_state(name, "EXITED", "session gone", now)
            _handle_watchdog_transition(name, "EXITED", "session gone", since, now=now)
            continue
        if probe_failed and not tmux_exists and watchdog.consecutive_probe_failures.get(name, 0) < 3: continue
        host = get_worker_host(name)
        if host and _is_host_down(host):
            reason = f"host {host} offline"; since = _record_worker_state(name, "HOST_OFFLINE", reason, now)
            _handle_watchdog_transition(name, "HOST_OFFLINE", reason, since, now=now)
            continue
        if host and host in failed_hosts and not tmux_exists: continue
        backend = backend_info.get(name)
        if backend is None: backend_name = get_worker_backend(name, session); backend = get_backend(backend_name)
        is_interactive = backend.is_interactive; adapter_alive = False
        if not is_interactive:
            with processes.adapter_pids_lock: entry = processes.adapter_pids.get(name)
            if entry:
                proc, _stderr = entry
                adapter_alive = proc.poll() is None
        host = get_worker_host(name); claude_pid = claude_pids.get(name) if is_interactive else None; cpu = 0.0
        if claude_pid and claude_pid in stats: cpu = stats[claude_pid].get("cpu", 0.0)
        children_total = _child_count(claude_pid, host=host) if claude_pid else 0
        children = _watchdog_compute_children(name, children_total, is_interactive, claude_pid, now)
        if children > 0:
            with watchdog.lock: watchdog.last_child_ts[name] = now
        _watchdog_track_activity(name, children, cpu, now)
        pending_ts = _pending_timestamp(name); pending = pending_ts is not None
        with watchdog.lock: last_activity = watchdog.last_activity_ts.get(name, 0.0)
        if pending_ts:
            effective_start = max(pending_ts, last_activity) if last_activity > pending_ts else pending_ts
            pending_age = now - effective_start
        else: pending_age = 0.0
        with watchdog.lock:
            last_child_ts = watchdog.last_child_ts.get(name, 0.0); last_hook_ts = watchdog.last_hook_ts.get(name)
            last_seen_claude = watchdog.last_seen_claude.get(name)
        if not is_interactive: last_seen_claude = None
        state_kw: dict[str, object] = dict(tmux_exists=tmux_exists, claude_pid=claude_pid,
            pending=pending, pending_ts=pending_ts, pending_age=pending_age, children=children,
            last_child_ts=last_child_ts, cpu=cpu, last_hook_ts=last_hook_ts,
            last_seen_claude=last_seen_claude, now=now, is_interactive=is_interactive, adapter_alive=adapter_alive)
        worker_state, reason = compute_state(**state_kw)  # type: ignore[arg-type]
        worker_state, reason = _watchdog_refine_state(
            name, tmux_name, worker_state, reason, state_kw, host)
        since = _record_worker_state(name, worker_state, reason, now)
        _handle_watchdog_transition(name, worker_state, reason, since, now=now)

def _watchdog_compute_children(name: str, children_total: int,
                                is_interactive: bool, claude_pid: str | None,
                                now: float) -> int:
    pending_ts = _pending_timestamp(name); pending = pending_ts is not None
    if is_interactive and claude_pid:
        with watchdog.lock:
            baseline = watchdog.idle_child_baseline.get(name)
            if baseline is None:
                watchdog.idle_child_baseline[name] = children_total
                baseline = children_total
            elif not pending:
                baseline = min(baseline, children_total)
                watchdog.idle_child_baseline[name] = baseline
        return max(0, children_total - baseline)
    return children_total

def _watchdog_track_activity(name: str, children: int, cpu: float, now: float) -> None:
    with watchdog.lock:
        prev_children = watchdog.prev_children.get(name)
        activity_increased = (prev_children is not None and children > prev_children)
        if activity_increased or cpu >= CPU_ACTIVE: watchdog.last_activity_ts[name] = now
        watchdog.prev_children[name] = children

def _watchdog_refine_state(
    name: str, tmux_name: str,
    worker_state: str, reason: str,
    state_kw: dict[str, object],
    host: str | None,
) -> tuple[str, str]:
    is_interactive = bool(state_kw.get("is_interactive", True)); pending = bool(state_kw.get("pending"))
    pending_age = float(state_kw.get("pending_age", 0.0)); now = float(state_kw.get("now", 0.0))  # type: ignore[arg-type]
    if worker_state == "STUCK":
        watchdog.idle_streak[name] = watchdog.idle_streak.get(name, 0) + 1
        streak = watchdog.idle_streak[name]
        if streak < IDLE_STREAK_STUCK: worker_state = "WAITING"
        else:
            if is_interactive and pending:
                pane_text = _capture_pane_text(tmux_name, lines=15, host=host)
                if pane_text:
                    activity = _extract_activity(pane_text.splitlines())
                    if activity == "Idle at prompt":
                        _log(_LOG_WARN, "watchdog", f"Auto-clearing stale pending for {name} (idle at prompt, age={int(pending_age)}s)")
                        clear_pending(name)
                        watchdog.idle_streak[name] = 0
                        since = _record_worker_state(name, "READY", "idle (auto-cleared stale pending)", now)
                        _handle_watchdog_transition(name, "READY", "idle (auto-cleared stale pending)", since, now=now)
                        return "READY", "idle (auto-cleared stale pending)"
            poisoned_reason = _detect_poisoned(name, tmux_name)
            worker_state, reason = compute_state(**state_kw, poisoned_reason=poisoned_reason)  # type: ignore[arg-type]
        reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    elif worker_state == "POISONED":
        streak = watchdog.idle_streak.get(name, 0)
        if streak: reason = f"{reason} streak={streak}/{IDLE_STREAK_STUCK}"
    else: watchdog.idle_streak[name] = 0
    if worker_state == "READY" and is_interactive:
        pane_text = _capture_pane_text(tmux_name, lines=30, host=host)
        if pane_text:
            pane_lines = pane_text.splitlines(); details = _extract_question_details(pane_lines)
            if details:
                with watchdog.lock: watchdog.waiting_input_details[name] = details
                worker_state = "WAITING_INPUT"; header = details.get("header", "")
                reason = f"question={header}" if header else "interactive prompt"
    return worker_state, reason

def _watchdog_cleanup_stale(registered_names: set[str]) -> None:
    with watchdog.lock:
        stale_dicts: list[dict[str, object]] = cast(list[dict[str, object]], [
            watchdog.worker_states, watchdog.last_child_ts,
            watchdog.last_seen_claude, watchdog.last_hook_ts,
            watchdog.prev_worker_states, watchdog.last_alert_ts,
            watchdog.idle_streak, watchdog.idle_child_baseline,
            watchdog.prev_children, watchdog.last_activity_ts,
        ])
        for d in stale_dicts:
            for name in list(d.keys()):
                if name not in registered_names: d.pop(name, None)
    for name in list(watchdog.consecutive_probe_failures.keys()):
        if name not in registered_names: watchdog.consecutive_probe_failures.pop(name, None)

def _watchdog_resource_checks(remote_hosts: set[str]) -> None:
    checks: list[tuple[str, Callable[[], None]]] = [
        ("Disk", lambda: _probe_disk_all_hosts(remote_hosts)),
        ("Memory", lambda: _probe_mem_all_hosts(remote_hosts)),
        ("IO", lambda: _probe_io_all_hosts(remote_hosts)),
        ("CPU hog", lambda: _probe_cpu_hogs(remote_hosts)),
        ("Worktree", lambda: _probe_worktree_sizes(remote_hosts)),
        ("Tailscale", lambda: _probe_tailscale()), ]
    for label, check_fn in checks:
        try: check_fn()
        except (subprocess.SubprocessError, OSError) as e: _log(_LOG_ERROR, "watchdog", f"{label} check error: {e}")
