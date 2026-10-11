from __future__ import annotations
import re
from typing import Callable
from core import _clock, DEFAULT_BACKEND
from claudecode import (
    TmuxSessionDict, WorkerStateEntry,
    normalize_backend,
    watchdog,
)
from _health_impl import (  # noqa: F401 — re-exported
    _activity_from_error, _activity_from_interactive, _activity_from_output_block,
    _activity_from_prompt, _activity_from_rate_limit, _activity_from_reverse_scan,
    _activity_from_spinner, _activity_from_tasks, _activity_from_tool,
    _check_adapter_log, _check_disk_usage, _check_hook_failure_signal,
    _check_io_usage, _check_mem_usage, _check_worktree_usage,
    _child_count, _clear_hook_failures, _detect_poisoned,
    _extract_activity, _extract_context_pct, _extract_question_details,
    _format_team_lines_pure, _format_watchdog_status_pure,
    _get_claude_pid, _get_cpu_hogs, _get_top_mem_procs,
    _handle_watchdog_transition,
    _is_host_down, _is_output_block_end, _normalize_activity,
    _probe_cpu_hogs, _probe_disk_all_hosts, _probe_io_all_hosts, _probe_mem_all_hosts,
    _probe_metric, _probe_tailscale, _probe_worktree_sizes, _ps_stats,
    _read_noninteractive_activity, _read_tmux_activity,
    _record_host_probe, _record_worker_state,
    _REVERSE_SCAN_PATTERNS, _send_interactive_reply,
    _send_resolved_alert, _send_watchdog_alert,
    _team_attention_summary, _watchdog_alert,
    _watchdog_cleanup_stale, _watchdog_collect_worker_pids,
    _watchdog_compute_children, _watchdog_evaluate_workers,
    _watchdog_gather_cpu_stats, _watchdog_probe_remote_hosts,
    _watchdog_refine_state, _watchdog_resource_checks,
    _watchdog_track_activity, _watchdog_update_probe_failures,
    clear_pending, compute_state, get_registered_sessions,
    get_worker_host, is_pending, _pending_timestamp,
    watchdog_loop,
)
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
