# SDD: Machine/Host as a First-Class Concept

**Author:** lee
**Date:** 2026-04-15
**Status:** Proposed — awaiting manager approval
**Consultation:** 3 turns with codex (gpt-5.4), responses archived at `/tmp/codex-turn{1,2,3}-response.txt`

---

## 1. Problem

Today, "which machine does a worker live on" is expressed as an **optional string field `host`** on worker rows in `workers.json`, where `host=None` means "local to the bridge". That sentinel has leaked into 20+ host-aware helper functions, 40+ `if host is None` branch sites, two execution primitives (`_remote_run`, `_remote_copy`), one exported hook env rule, and the `/workers` endpoint — each with its own assumptions about how "local" and "remote" differ. The scattering produces 6 concrete failure modes:

| # | Gap | Symptom |
|---|-----|---------|
| 1 | `/workers` not caller-aware | `kai` (Mac Mini) gets a bare tmux command for `mon` (VPS), fails, hand-wraps in ssh. Cross-host messaging is unreliable. |
| 2 | Remote non-interactive inbox unsupported | Codex/Gemini workers on Mac Mini cannot receive pipe messages. |
| 3 | No generation fencing on hook pushes | Late Stop hook from source host silently overwrites target state after teleport. Invisible race. Root cause behind v0.30.0 staleness band-aid. |
| 4 | Session-id cache has no provenance tag | Stale values from any source "look correct" forever. |
| 5 | `/response` handler doesn't know origin host | Handler re-reads registry at process time; racey against concurrent teleport. |
| 6 | `BRIDGE_PUBLIC_URL` is a single global env | Multi-bridge deployments can't coexist. |

Manager directive: *"make machine (host) aware the feature in architecture wise"*. Target: machine is a first-class type, every worker has a canonical machine, every state push carries its origin, cross-host messaging just works, the `host is None` sentinel dies.

## 2. Goals / Non-Goals

**Goals**
- G1: Single primitive for cross-machine execution (`MachineAdapter`), collapses the 40 branch sites.
- G2: Every authoritative state push carries `(machine_id, generation)` and is rejected on mismatch. Closes gap 3.
- G3: `/workers` response is caller-aware and includes machine metadata. Closes gap 1.
- G4: Non-interactive workers can receive messages regardless of machine. Closes gap 2.
- G5: `_scan_latest_session_id` band-aid is removed from correctness-critical paths. Closes gap 4.
- G6: No production downtime during rollout. Advisory mode for legacy workers until the quiet cutoff.

**Non-goals**
- Multi-bridge topology (gap 6). Punt until a second bridge is actually needed.
- SQLite. Already decided against in the prior consult. `workers.json` + atomic rewrite + flock is the storage.
- Per-session (per-transcript-UUID) fencing. Per-worker generation is sufficient.
- Auto-scaling / machine discovery. Machines are a hand-curated static list.
- Rewriting teleport flow as a transaction/saga. That's already scheduled separately; this SDD only introduces the state required for it.

## 3. Core Model

> **One rule:** `Worker = logical identity + assigned machine + generation`.
> `Machine = transport/path/reachability adapter`.
> `Hook/checkin = observed state tagged with (machine_id, generation)`.

### 3.1 Machine — static infrastructure catalog

New file: `~/.config/claudecode-telegram/machines.json`. Operator-edited. Loaded at bridge startup, never mutated at runtime.

```json
{
  "version": 1,
  "machines": {
    "vps": {
      "ssh_target": null,
      "bridge_base_url": "http://localhost:8271",
      "home_root": "/home/claude",
      "os_family": "linux"
    },
    "macmini": {
      "ssh_target": "beastoin-agents-f1-mac-mini",
      "bridge_base_url": "http://100.125.36.102:8271",
      "home_root": "/Users/beastoinagents",
      "os_family": "darwin"
    }
  }
}
```

**`ssh_target=null`** is the marker for "run locally on the bridge host". It replaces the `host is None` sentinel with an explicit field. The `vps` machine record is where "None" used to live.

**`bridge_base_url`** is how a worker on THIS machine reaches back to the bridge. Not a property of the machine itself — a property of the machine's view of the bridge. Named per codex's pushback on the original `public_url` naming.

**Not in `machines.json`:** no credentials, no secrets. SSH auth is handled by the existing `~/.ssh/config` and ControlMaster setup.

### 3.2 MachineAdapter — the execution seam

Pure transport layer. **Not** a worker-semantic API (no `enqueue`, no `delivery`).

```python
@dataclass(frozen=True)
class Machine:
    id: str
    ssh_target: str | None
    bridge_base_url: str
    home_root: str
    os_family: Literal["linux", "darwin"]

    @property
    def is_local(self) -> bool:
        return self.ssh_target is None

    def sessions_dir(self, node_name: str) -> str:
        return f"{self.home_root}/.claude/telegram/nodes/{node_name}/sessions"

    def project_home(self) -> str:
        return f"{self.home_root}/.claude/projects"


class MachineAdapter:
    """Transport-level operations against a Machine. One instance per machine, held in bridge process."""
    def __init__(self, machine: Machine): ...

    # Command execution
    def run(
        self,
        argv: list[str],
        *,
        input_text: str | None = None,
        timeout: float = 10.0,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...

    # Filesystem
    def put_text(self, path: str, content: str, *, mode: int | None = None) -> None: ...
    def append_text(self, path: str, content: str) -> None: ...
    def mkdir_p(self, path: str, *, mode: int | None = None) -> None: ...
    def read_text(self, path: str) -> str | None: ...

    # Tmux
    def tmux_has_session(self, session_name: str) -> bool: ...
    def tmux_send(self, session_name: str, text: str, *, press_enter: bool = True) -> None: ...
    def tmux_capture_pane(self, session_name: str, lines: int = 40) -> str: ...
    def tmux_set_env(self, session_name: str, key: str, value: str) -> None: ...

    # Process
    def pgrep(self, pattern: str) -> list[int]: ...
    def ps_stats(self, pids: list[int]) -> list[dict]: ...

    # Reachability (not correctness — for status display only)
    def probe(self, timeout: float = 3.0) -> bool: ...
```

Registry lookup: `get_machine(id) -> MachineAdapter`. `get_machine_for_worker(name) -> MachineAdapter`. Both read from the in-process machine registry populated at startup.

All 20 existing host-aware functions become thin wrappers over `adapter.run/tmux_send/...`. The `_remote_run(cmd, host=None)` and `_remote_copy(...)` primitives are retired in the same PR.

### 3.3 Worker row — assigned vs observed state

**Persistent** (in `workers.json`, atomic rewrite under flock):
```json
{
  "backend": "claude",
  "chat_id": 123,
  "hire_time": 1713194400,

  "machine_id": "vps",          // replaces "host" — never null
  "home_machine_id": "vps",     // replaces "home_host"
  "home_cwd": "/home/claude/project",

  "generation": 7,              // bumped on every teleport/restart/rehire
  "teleport_id": null,          // UUID during active teleport, null otherwise

  "resume": {
    "generation": 7,
    "machine_id": "vps",
    "session_id": "abc-def-...",
    "cwd": "/home/claude/project",
    "updated_at": 1713194567
  }
}
```

**RAM-only** (bridge process dict, lost on restart):
```python
@dataclass
class WorkerRuntimeState:
    last_seen: float | None        # epoch from last hook push
    last_hook_at: float | None     # high-frequency, do not persist
    last_status: str | None        # "idle" | "active" | "waiting_input" | ...
    last_reachable: bool           # from probe()
```

**Key principle (codex's model):** assigned state is what the bridge **wills** to be true. Observed state is what the workers **report**. Hook payloads NEVER mutate assigned state; they only update observed state (and `resume.*` as a durable checkpoint).

### 3.4 Generation fencing contract

- `workers.json.generation: int` — starts at 1, monotonic.
- Incremented on every authority transition:
  - `/hire` (initial = 1)
  - `/restart` → +1
  - `/teleport` → +1
  - `/rehire` → +1 (conceptual future event)
- `export_hook_env()` writes into the target tmux session:
  - `BRIDGE_WORKER_NAME=<name>`
  - `BRIDGE_WORKER_MACHINE_ID=<id>`
  - `BRIDGE_WORKER_GENERATION=<n>`
- Also writes a **sidecar file** on the target machine: `<sessions_dir>/<name>/hook_env.json`:
  ```json
  {"name": "lee", "machine_id": "vps", "generation": 7, "bridge_base_url": "http://localhost:8271"}
  ```
  atomic write (tmp + rename), 0600.
- Hooks read env-first, sidecar-fallback-only. If both exist and disagree → log fatal, POST nothing, exit non-zero.
- Hook POSTs include `{machine_id, generation}` in every body.
- **Acceptance rule:** `POST /response` (and any state-mutating hook endpoint) accepts iff:
  - `body.machine_id == row.machine_id`
  - `body.generation == row.generation`
- On mismatch → **HTTP 409**, log `stale_post worker=<w> got=(<mid>,<gen>) have=(<mid>,<gen>) teleport_id=<tid>`, do NOT mutate state. Optionally record to a rolling telemetry buffer.
- **`status` field is advisory**, not part of the acceptance contract. A valid POST from the new target during `status=teleporting` is still accepted.
- **`teleport_id`** (UUID) is issued at teleport start, cleared at teleport finalize. Included in acceptance key if present (covers the same-generation-different-teleport edge case if we ever increment gen twice in quick succession — defense in depth).

### 3.5 Teleport transaction ordering

Replaces current `_do_teleport` flow. **Row lock is held only for brief metadata mutations.** The 20-60s work happens unlocked.

```
T1. Brief lock:
    CAS row → {
      generation: N+1,
      machine_id: target,
      teleport_id: T,
      resume.generation: N+1,  # invalidate old resume
      resume.session_id: null,  # cleared, will be set by SessionStart push
    }
    Release lock.

T2. Stop source tmux (send /exit, wait up to 30s).

T3. Sync working directory (rsync/git).

T4. Start target tmux via SSH. Hook env written BEFORE claude launches
    with generation=N+1, machine_id=target. Sidecar file written too.

T5. Wait for first SessionStart hook from target (timeout: 10s).
    This push lands at generation=N+1, matches row → accepted,
    fills resume.session_id atomically.

T6. Brief lock:
    CAS row → {teleport_id: null} only if row.teleport_id == T and
                                      row.generation == N+1.
    Release lock.
```

**Invariants:**
- Any Stop hook from the old source arrives with `generation=N` (the value baked into its env at tmux create time) → rejected as stale.
- Any Stop hook from the new target arrives with `generation=N+1` → accepted regardless of `teleport_id` status.
- If the bridge crashes between T1 and T6, the row is recoverable: it has `teleport_id` set, a sweeper on startup can compensate.

### 3.6 /response handler snapshot read

The handler is lock-free for the acceptance check:

```python
def handle_response_post(body):
    snapshot = with_row_lock(body.worker, lambda row: row.copy())
    if snapshot.machine_id != body.machine_id or snapshot.generation != body.generation:
        return 409, "stale"
    # Apply observed-state updates + resume checkpoint
    with_row_lock(body.worker, lambda row: merge_resume(row, body) if row.generation == body.generation else None)
    return 200, "ok"
```

Readers take a brief per-row lock (fcntl.flock on a per-worker lock file) to copy-out a snapshot, then validate unlocked. A 30s teleport **does not block** hook POSTs because the teleport only holds the row lock during T1 and T6 (<50ms each).

### 3.7 POST /send (new endpoint) and caller-aware /workers

**New endpoint:** `POST /send {from_worker, to_worker, text, request_id}`.

| Target mode | Target machine | Behavior |
|---|---|---|
| Interactive, any machine | reachable | `adapter.tmux_send(session_name, text)` |
| Interactive, any machine | unreachable | HTTP 503, no queue |
| Non-interactive, local | reachable | write to local fifo |
| Non-interactive, remote | reachable | atomic file write into remote inbox dir via `adapter.put_text` |
| Non-interactive, any | unreachable | HTTP 202, enqueue to bridge-local outbox (§3.8) |

**`/workers` endpoint** stops being the primary send path. It now returns:
```json
{
  "workers": [
    {
      "name": "mon",
      "backend": "claude",
      "machine_id": "vps",
      "protocol": "interactive",
      "delivery": {"method": "bridge", "endpoint": "/send"},
      "send_example_human": "curl -s -X POST $BRIDGE_URL/send -d '{\"from_worker\":\"YOUR_NAME\",\"to_worker\":\"mon\",\"text\":\"hi\"}'"
    }
  ]
}
```

`send_example_human` is for operator debugging only; the canonical path is `POST /send`. Callers never see tmux names or SSH targets.

**Deprecation:** the old tmux-command `send_example` is removed. This breaks any existing worker that was constructing its own tmux commands; since the only known caller is other Claude agents reading `/workers`, we update the welcome message in the same release so new starts pick up the new protocol.

### 3.8 Outbox for non-interactive offline delivery

Physical layout: `nodes/<node>/outbox/<worker>/` — one file per message.

- Filename: `<monotonic_seq>_<uuid4>.msg` (per-worker sequence from bridge memory; ordering is stable even if clocks skew)
- Atomic write: `<filename>.tmp` → `rename` to `<filename>`
- Drain claim: `rename` to `inflight/<filename>` before sending, delete after success, move back on failure.
- One drainer per worker (serialized by per-worker lock). Drain runs on: (i) machine reachability transition false→true, (ii) periodic tick every 30s, (iii) explicit kick on /send POST.

### 3.9 SessionStart push → replaces `_scan_latest_session_id`

New endpoint: `POST /hook/session-start {name, machine_id, generation, session_id, cwd, event}`.

Fires from the SessionStart hook on `init|resume|compact|start`. Lands `resume.{session_id, cwd, generation, machine_id, updated_at}` atomically if generation matches.

Once this ships: `_scan_latest_session_id` is removed from all correctness-critical call sites (`/rewind`, `/restart --resume`, transcript HTML, `start_worker resume`, `_stop_worker_for_teleport`). The scanner remains available as a **diagnostic CLI tool** for incident response, but the bridge never calls it in hot paths.

During the brief window after `claude --resume` but before the first SessionStart hook fires: `/rewind` returns "resume pending, retry in a few seconds" — **not** a guessed UUID. This is the codex rule: bypass-of-generation-authority is disallowed.

## 4. Migration / Rollout

Codex's sequencing, adapted. Each phase is a shippable PR with its own TDD increments.

### Phase 0 (prep)
- Retrofit `machines.json` config file with `vps` and `macmini` entries matching current reality. Load-on-startup, strict schema. **No runtime behavior change yet.**
- Test: bridge starts with valid config; bridge refuses to start with malformed config.

### Phase 1 (adapter seam — "big bang, narrow scope")
- Introduce `Machine` dataclass + `MachineAdapter` class.
- Rewrite all 20 host-aware functions to call `adapter.run/...` instead of `_remote_run(cmd, host=...)`. Mechanical rewrite, no semantic change.
- Rewrite the 40 `if host is None` branch sites to use `get_machine_for_worker(name)`.
- Retire `_remote_run`, `_remote_copy`, `get_worker_host` (as a private readers → all point at adapter).
- `workers.json` schema: `host` is still read, migrated on load to `machine_id`. Dual-write for one release: writes both `host` and `machine_id` for rollback.
- **Explicitly NOT in this PR:** generation, resume struct, POST /send, SessionStart endpoint, outbox. Those are state-machine changes, separate review surface.
- Blast radius: every host-aware call site. Every test that mocks `host=`. Big diff, tight scope, reviewable as pure refactor.

### Phase 2 (generation field, advisory mode)
- Add `generation: int` to worker rows (default 1 for existing workers on first load).
- `export_hook_env` writes `BRIDGE_WORKER_GENERATION`. Also writes the sidecar `hook_env.json`.
- Handlers accept hook POSTs without `generation` (log WARN as "legacy_unversioned_hook").
- **Writes from unversioned POSTs are quarantined**: they may update `last_seen` in RAM but MUST NOT mutate `resume.*` or assigned state. This is not "full advisory" — it's "observed-only advisory".
- Legacy `_scan_latest_session_id` remains in place.
- Test: old hooks still work, new hooks carry generation, strict-path writes are observable in telemetry.

### Phase 3 (SessionStart push + resume struct)
- New endpoint `POST /hook/session-start`.
- Worker row schema adds `resume: {...}` block.
- SessionStart hook script `checkin-on-start.sh` gains the push call.
- `/rewind`, `/restart --resume`, transcript HTML, start_worker all read from `row.resume.session_id` with generation gating.
- Legacy `_scan_latest_session_id` removed from these paths (retained as `scan-session-id` CLI tool for incident response).

### Phase 4 (teleport transaction rewrite)
- Refactor `_do_teleport` to the T1–T6 order.
- Introduce `teleport_id` field.
- Add startup compensating sweeper: if a row has `teleport_id != null` on bridge start, investigate (heartbeat both source and target, pick winner, clear).

### Phase 5 (strict mode flip)
- **Cutover rule:** flip to strict rejection of unversioned POSTs after `zero unversioned POSTs for 72h`, hard cap at 7 days since Phase 2 landed.
- If cap hits with lingering legacy workers: operator restarts or quarantines them.
- Remove advisory-mode code paths.

### Phase 6 (POST /send + caller-aware /workers)
- New `POST /send` endpoint with the delivery matrix from §3.7.
- `/workers` response reshape: new schema, `send_example_human` only.
- Welcome message updated: agents learn to use `POST /send`.
- Fallback: old `send_example` field remains for one release as deprecated alias (unwrapped, pointing at `/send`).

### Phase 7 (outbox for non-interactive remote)
- `nodes/<node>/outbox/<worker>/` directory structure.
- Drain loop wired to reachability transitions.
- Works for both local and remote non-interactive workers (Codex/Gemini).

### Phase 8 (cleanup)
- Remove dual-write of `host` in workers.json.
- Remove deprecated `send_example` field.
- Remove `scan-session-id` CLI tool if unused for 30 days.

## 5. TDD Increment Ladder

Following project TDD convention (Red-Green-Refactor per CLAUDE.md §"TDD Workflow"). Each increment is one failing test → minimal code → refactor, then re-run FAST to catch regressions. Phases correspond to section 4 above.

### Phase 0 — machines.json config

| # | Test | Behavior |
|---|------|----------|
| 1 | `test_machines_config_loads_valid` | `~/.config/claudecode-telegram/machines.json` with vps + macmini → bridge starts, `get_machine("vps")` returns adapter |
| 2 | `test_machines_config_missing_file` | No config → bridge starts with implicit `{vps}` fallback derived from current env |
| 3 | `test_machines_config_malformed` | Invalid JSON → bridge refuses to start, clear error |
| 4 | `test_machines_config_missing_required_field` | Machine entry missing `home_root` → reject at load |

### Phase 1 — MachineAdapter seam (big-bang rewrite)

| # | Test | Behavior |
|---|------|----------|
| 5 | `test_adapter_local_run` | `vps.run(["echo", "hi"])` → no ssh wrap, captures stdout |
| 6 | `test_adapter_remote_run` | `macmini.run(["echo", "hi"])` → emits `ssh <target> echo hi`, captures stdout |
| 7 | `test_adapter_put_text_local_atomic` | Local write goes through tmp+rename |
| 8 | `test_adapter_put_text_remote_atomic` | Remote write uses scp or `ssh host "cat > tmp && mv"` with atomicity |
| 9 | `test_adapter_tmux_send_local` | Local paste-buffer + send-keys Enter |
| 10 | `test_adapter_tmux_send_remote` | Remote tmux_send wraps in ssh |
| 11 | `test_get_machine_for_worker_from_row` | Worker with `machine_id="macmini"` → returns macmini adapter |
| 12 | `test_legacy_host_field_migrated` | Existing row with `host="mac-mini"` → loader rewrites to `machine_id="macmini"` on first read |
| 13 | `test_legacy_host_dual_write` | Writing a row sets both `host` and `machine_id` for rollback safety |
| 14 | `test_tmux_exists_through_adapter` | Existing `tmux_exists(tmux_name, host=...)` callers work unchanged (wrapper delegates to adapter) |
| 15 | `test_hire_remote_uses_adapter` | `/hire` on a remote machine uses adapter.run for tmux creation |
| 16 | `test_restart_remote_uses_adapter` | `_restart_remote_worker` calls adapter, no direct `_remote_run` |
| 17 | `test_no_remote_run_references` | Grep assertion: `_remote_run` appears nowhere in bridge.py after rewrite |

### Phase 2 — generation field + advisory mode

| # | Test | Behavior |
|---|------|----------|
| 18 | `test_worker_row_has_generation_default_1` | Existing workers load with `generation=1` |
| 19 | `test_hire_sets_generation_1` | New `/hire` creates row with generation=1 |
| 20 | `test_restart_bumps_generation` | `/restart` transitions 1→2 atomically |
| 21 | `test_teleport_bumps_generation` | `/teleport` transitions 2→3 atomically |
| 22 | `test_hook_env_exports_generation` | `export_hook_env` writes `BRIDGE_WORKER_GENERATION=<n>` into tmux env |
| 23 | `test_hook_env_writes_sidecar_file` | `hook_env.json` written atomically on target machine |
| 24 | `test_hook_env_sidecar_0600` | Sidecar file permissions enforced |
| 25 | `test_response_accepts_matching_generation` | POST with `{gen=3, mid="vps"}` against row `(gen=3, mid="vps")` → 200 |
| 26 | `test_response_rejects_stale_generation` | POST with `{gen=2}` against row `gen=3` → 409, resume unchanged |
| 27 | `test_response_accepts_legacy_no_generation_advisory` | POST without generation in advisory mode → 200, WARN log, resume NOT mutated |
| 28 | `test_response_unversioned_updates_ram_last_seen` | Legacy POST updates RAM last_seen but not persisted resume |
| 29 | `test_response_rejects_wrong_machine_id` | POST with `{gen=3, mid="macmini"}` against row `mid="vps"` → 409 |

### Phase 3 — SessionStart push + resume struct

| # | Test | Behavior |
|---|------|----------|
| 30 | `test_session_start_endpoint_writes_resume` | POST /hook/session-start → row.resume updated |
| 31 | `test_session_start_rejects_stale_generation` | Stale gen → 409 |
| 32 | `test_session_start_on_init_event` | `event=init` → resume populated |
| 33 | `test_session_start_on_resume_event` | `event=resume` → resume.session_id = new UUID |
| 34 | `test_rewind_uses_resume_session_id` | `/rewind` reads from row.resume, not from scan |
| 35 | `test_restart_resume_uses_row_resume` | `/restart` reads session_id from row.resume |
| 36 | `test_rewind_pre_session_start_returns_pending` | No session_start fired yet → "resume pending" response, not stale UUID |
| 37 | `test_scan_session_id_not_called_in_hot_paths` | Grep assertion: `_scan_latest_session_id` appears only in CLI tool module, not bridge.py handlers |

### Phase 4 — teleport transaction rewrite

| # | Test | Behavior |
|---|------|----------|
| 38 | `test_teleport_T1_commits_generation_before_work` | Mock stop → observe row at generation=N+1 before stop is called |
| 39 | `test_teleport_sets_teleport_id` | Row has `teleport_id=<uuid>` during T2–T5 |
| 40 | `test_teleport_T6_finalizes_only_on_match` | CAS succeeds only if teleport_id still matches |
| 41 | `test_late_source_hook_rejected_after_teleport` | Source hook with gen=N arrives after T1 → 409 |
| 42 | `test_target_hook_accepted_during_teleporting_status` | Target hook at gen=N+1 during `status=teleporting` → 200 |
| 43 | `test_teleport_crash_between_T1_T6_recoverable` | Simulated bridge crash at T3 → startup sweeper detects `teleport_id != null`, investigates |
| 44 | `test_row_lock_not_held_during_sync` | Concurrent `/response` POST from a DIFFERENT worker during teleport T2–T5 → 200 (not blocked) |

### Phase 5 — strict mode

| # | Test | Behavior |
|---|------|----------|
| 45 | `test_strict_mode_rejects_legacy` | After flip, POST without generation → 409 |
| 46 | `test_quiet_window_counter_resets` | Legacy POST during Phase 2 resets the 72h clock |

### Phase 6 — POST /send + caller-aware /workers

| # | Test | Behavior |
|---|------|----------|
| 47 | `test_send_interactive_same_machine` | `POST /send {to=lee}` when both on vps → local tmux_send |
| 48 | `test_send_interactive_cross_machine` | `POST /send` from mac-mini worker to vps worker → bridge routes locally |
| 49 | `test_send_interactive_target_offline_returns_503` | Target tmux gone → 503 |
| 50 | `test_send_non_interactive_local_fifo` | Codex on vps → writes local pipe |
| 51 | `test_send_non_interactive_remote_uses_adapter_put` | Codex on mac-mini → `adapter.put_text(inbox_dir/<msg>)` |
| 52 | `test_workers_returns_machine_id` | Every worker entry has `machine_id` field |
| 53 | `test_workers_delivery_points_at_send` | `delivery.endpoint == "/send"` for every worker |
| 54 | `test_workers_no_direct_tmux_example` | Tmux-command send_example is removed |

### Phase 7 — outbox

| # | Test | Behavior |
|---|------|----------|
| 55 | `test_outbox_writes_one_file_per_message` | Atomic file creation under `outbox/<worker>/` |
| 56 | `test_outbox_monotonic_sequence` | Two messages in same ms → distinct filenames, preserved order |
| 57 | `test_outbox_drain_on_reachability_transition` | Machine becomes reachable → drain fires |
| 58 | `test_outbox_claim_via_inflight` | Drain renames to `inflight/` before sending |
| 59 | `test_outbox_crash_during_send_recovers` | File in `inflight/` on startup → re-delivered |

### Phase 8 — cleanup

| # | Test | Behavior |
|---|------|----------|
| 60 | `test_host_field_removed_from_workers_json` | New hires don't write `host` key |
| 61 | `test_deprecated_send_example_removed` | `/workers` response has no deprecated field |

Total: **61 increments**. Matches the "big bang seam, small state-machine PRs" shape of codex's migration recommendation.

## 6. Open Questions (for manager approval)

1. **Scope of Phase 1 PR.** The big-bang rewrite of 20 functions + 40 branch sites produces a large diff even if mechanical. Acceptable in one PR, or split into two (execution helpers first, command handlers second)?
2. **`machines.json` location.** `~/.config/claudecode-telegram/machines.json` follows the existing convention of `prod.env` at the same path. OK to introduce a second config file, or keep machine list in `nodes/<node>/machines.json` for per-node flexibility?
3. **Strict-mode cutover gate.** Codex recommends "72h quiet, 7-day cap". Is this acceptable automation, or does the manager want a manual operator flag (`STRICT_HOOK_FENCING=1`) to flip the switch with human observation?
4. **`POST /send` backpressure.** If a non-interactive outbox grows unbounded (machine offline for days), what's the cap? Drop oldest, drop newest, or refuse new /send with 429?
5. **Deprecation window for tmux-command `send_example`.** One release (= days in this project) or one week?

## 7. Risk Table

| Risk | Mitigation |
|------|------------|
| Big-bang Phase 1 introduces latent bug at an edge site not covered by tests | Aggressive test parity: keep existing host= parameter tests, add adapter tests in parallel, diff behavior under both paths for one release |
| Strict-mode flip takes a worker offline | Advisory window + sweeper + operator `/force-restart <name>` escape hatch |
| Sidecar file disagrees with tmux env (split brain) | Hook fails loud; do not guess; operator investigates |
| Outbox fills disk during extended offline | Quota + telemetry alert |
| Late SessionStart hook arrives from a terminated session (user hit Ctrl-C) | Generation fencing handles it — the Ctrl-C'd session has the old gen from before its own tmux restart |
| `_scan_latest_session_id` removal breaks some flow I forgot | Phase 3 retains it as a CLI diagnostic tool; Phase 8 deletes it only after 30 days of unused |

## 8. What This SDD Is NOT Covering

- Multi-bridge deployments. Gap 6 is parked.
- Worker identity/auth. Today's "anyone on Tailscale" trust model is retained.
- Machine-machine direct messaging. All routing is bridge-mediated in the new model.
- Concurrent teleports (A→B and C→B at the same time). Teleport acquires a target-machine lock today, this SDD doesn't change that.

---

**Next step:** manager review. On approval, I'll open PR #1 (Phase 0 + Phase 1 Phase 0) with tests 1–17 as the first increment ladder. Each subsequent phase is its own PR with its own review surface. Codex consultation transcripts preserved at `/tmp/codex-turn{1,2,3}-response.txt`.
