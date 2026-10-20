# SPEC Rubric — claudecode-telegram

> Scoring framework for implementation quality. Zero drift from constraints.
> Created: 2026-10-19. Codex (gpt-6.1-sol) consulted on framework design.

## Methodology

### Dimension Derivation
Every dimension traces to a GOAL constraint. One GOAL may produce multiple dimensions when it contains independently testable obligations. No dimension exists without a GOAL citation.

### Scoring Scale
| Score | Meaning |
|-------|---------|
| 1 | Constraint violated or no obligations pass |
| 2–4 | Substantial portions fail |
| 5–7 | Partial implementation; remaining failures include required cases |
| 8 | Every required acceptance case passes with reproducible evidence |
| 9 | 8 + negative control confirms detection of violation |
| 10 | 9 + independent replay confirms result |

For scores below 8: `score = 1 + floor(7 × passed_obligations / total_obligations)`

### Aggregation
**SPEC score = min(dimension scores)**. Mean shown as supplementary.

| Result | Decision |
|--------|----------|
| Minimum ≥ 8 | **Acceptable** |
| Minimum 5–7 | **Needs improvement** — contract unmet |
| Minimum 1–4 | **Reimplementation review** — structural assessment needed |
| Hard gate fails | **Unacceptable** regardless of aggregate |

### Hard Gates
- **HG-TOKEN**: Bot token never in worker/hook/tmux env (SPEC-006, SPEC-030, SPEC-032) — **ALL PASS**
- **HG-PROD-ISOLATION**: Test code never touches prod (SPEC-021, SPEC-024) — **PASS**
- **HG-NO-DATABASE**: No external database dependency (SPEC-003) — **PASS**

---

## Summary Dashboard

| SPEC | Title | Min | Mean | Verdict | Issue |
|------|-------|-----|------|---------|-------|
| 001 | tmux IS persistence | 9 | 9.0 | ✅ Acceptable | — |
| 002 | claude-\<name\> naming | 9 | 9.0 | ✅ Acceptable | — |
| 003 | No external database | 8 | 8.0 | ✅ Acceptable | Dead `import sqlite3` |
| 004 | Per-session files | 9 | 9.0 | ✅ Acceptable | — |
| 005 | Fail loudly | 8 | 8.5 | ✅ Acceptable | — |
| 006 | Token isolation | 9 | 9.0 | ✅ Acceptable | HG-TOKEN ✅ |
| 007 | Admin config | 9 | 9.0 | ✅ Acceptable | — |
| 008 | Secure by default | 8 | 8.5 | ✅ Acceptable | — |
| 009 | Decentralized comms | 8 | 8.5 | ✅ Acceptable | POST /messages exists |
| 010 | Machines are config | 9 | 9.0 | ✅ Acceptable | — |
| 011 | SOLID in single file | **6** | 7.5 | ⚠️ Needs improvement | 4-file split vs spec |
| 012 | Message routing | 9 | 9.0 | ✅ Acceptable | — |
| 013 | Clean chat | 8 | 8.0 | ✅ Acceptable | — |
| 014 | Worker registration | 9 | 9.0 | ✅ Acceptable | — |
| 015 | No magic routing | 8 | 8.5 | ✅ Acceptable | — |
| 016 | Hook: minimal/defensive | 9 | 9.0 | ✅ Acceptable | — |
| 017 | Single chat UX | 9 | 9.0 | ✅ Acceptable | — |
| 018 | Bridge architecture | **7** | 8.0 | ⚠️ Needs improvement | Monolithic CommandRouter |
| 019 | Machine catalog | 9 | 9.0 | ✅ Acceptable | — |
| 020 | Connector allowlists | 9 | 9.0 | ✅ Acceptable | — |
| 021 | Behavior tests | **5** | 6.7 | ⚠️ Needs improvement | No formal UAT category |
| 022 | Configurable paths | 8 | 8.0 | ✅ Acceptable | — |
| 023 | Node isolation | 8 | 8.0 | ✅ Acceptable | — |
| 024 | Process lifecycle safety | **7** | 7.0 | ⚠️ Needs improvement | lsof pattern in tests |
| 025 | Dev-before-prod | **4** | 5.5 | 🔴 Reimpl review | No automated E2E |
| 026 | Learning reminders | 8 | 8.0 | ✅ Acceptable | — |
| 030 | Security model | 8 | 8.5 | ✅ Acceptable | HG-TOKEN ✅ |
| 031 | Logging/observability | 8 | 8.0 | ✅ Acceptable | — |
| 032 | Spawn-on-demand tools | **7** | 8.0 | ⚠️ Needs improvement | Review relay split |
| 033 | Worker state consistency | **6** | 7.4 | ⚠️ Needs improvement | Host clearing bug |

**Totals: 23 Acceptable · 6 Needs improvement · 1 Reimpl review**

---

## Detailed Scores

### SPEC-001: tmux IS persistence [GOAL-002]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R001-G002-authority | tmux authority for worker presence | 9 | `scan_tmux_sessions()` derives workers from `tmux list-sessions` on local+remote. Watchdog uses `tmux_exists()`. workers.json merged but tmux authoritative for liveness. |
| R001-G002-recovery | Recovery from surviving sessions | 9 | `_discover_and_configure_sessions()` at startup: scans tmux, merges registry, re-exports hook env, bootstraps via `_registry_bootstrap`. Multi-machine via SSH. |

**Min: 9 · Verdict: Acceptable**

### SPEC-002: claude-\<name\> naming [GOAL-002]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R002-G002-naming | Session naming conformity | 9 | All creation uses `f"{TMUX_PREFIX}{name}"`. Consistent across hire, restart, discovery. |
| R002-G002-discovery | Discovery namespace correctness | 9 | `scan_tmux_sessions()` filters by `session_name.startswith(self.tmux_prefix)`. Non-matching sessions warned and skipped. |

**Min: 9 · Verdict: Acceptable**

### SPEC-003: No external database [GOAL-002, GOAL-010]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R003-G002-no-db | Absence of database dependency | 8 | No SQLite/Postgres/Redis calls in bridge.py. Dead `import sqlite3` exists (line 29) — unused artifact from tool extraction. JSON files only. |
| R003-G010-recovery | Persistence necessity and recovery consistency | 8 | JSON persists: workers.json, guest_state, channel_state, relay_state, learning_reminders, last_chat_id. Matches GOAL-010 minimal state. |

**Hard gate HG-NO-DATABASE: PASS**
**Min: 8 · Verdict: Acceptable**
**Repair note:** Remove dead `import sqlite3`.

### SPEC-004: Per-session files [GOAL-002]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R004-G002-files | Per-worker coordination-file correctness | 9 | `ensure_session_dir()` creates `SESSIONS_DIR/<worker>/` (0o700). `set_pending()` writes pending+chat_id. `clear_pending()` removes pending. Hook reads chat_id for reply routing. |

**Min: 9 · Verdict: Acceptable**

### SPEC-005: Fail loudly [GOAL-002]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R005-G002-errors | Missing/dead-session error visibility | 8 | route_message: "Can't find X" for missing, "X is offline" for dead, "Could not send" for failed, "Being teleported" for in-flight. Errors surface to Telegram. |
| R005-G002-rediscovery | Restart rediscovery | 9 | `_discover_and_configure_sessions()` scans tmux + registry on startup. Sends startup notification listing all found workers. |

**Min: 8 · Verdict: Acceptable**

### SPEC-006: Token isolation [GOAL-024]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R006-G024-exclusion | Bot-token exclusion from Claude execution and outputs | 9 | `export_hook_env()` exports PORT, TMUX_PREFIX, SESSIONS_DIR, WORKER_BACKEND, BRIDGE_URL. No TELEGRAM_BOT_TOKEN. Token in shared.py + bridge startup only. Hook never sees it. |

**Hard gate HG-TOKEN: PASS**
**Min: 9 · Verdict: Acceptable**

### SPEC-007: Admin config [GOAL-027]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R007-G027-configured | Configured-admin enforcement | 9 | `_check_admin()` checks `chat_id == admin_chat_id`. Non-admins silently rejected. ADMIN_CHAT_ID env var at startup. |
| R007-G027-learned | Learned-admin acquisition and persistence | 9 | When admin_chat_id is None, first message auto-learns. `save_last_chat_id()` persists. `load_last_chat_id()` restores on restart. |

**Min: 9 · Verdict: Acceptable**

### SPEC-008: Secure by default [GOAL-027]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R008-G027-webhook | Webhook secret enforcement | 8 | When WEBHOOK_SECRET set, handler checks X-Telegram-Bot-Api-Secret-Token and rejects mismatches. Optional for poll mode (default transport). |
| R008-G027-perms | Session-file access restrictions | 9 | Directories 0o700, files 0o600 throughout (claudecode.py:884, 943, 1000; bridge.py:672, 3650, 5753). Pipes 0o600. |

**Min: 8 · Verdict: Acceptable**

### SPEC-009: Decentralized worker comms [GOAL-008]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R009-G008-discovery | Discovery correctness | 9 | `/workers` returns protocol, address, send_example per worker. Caller-aware SSH wrapping via `?from=`. tmux workers get flock commands, pipe workers get echo. |
| R009-G008-direct | Direct delivery without bridge relay | 8 | Workers send via tmux/pipe directly. `/outputs` validates source==session. POST /messages exists as bridge-mediated alternative for non-interactive backends — justified accommodation, not violation. |

**Min: 8 · Verdict: Acceptable**

### SPEC-010: Machines are config [GOAL-003, GOAL-011]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R010-G003-catalog | Configuration-based machine discovery | 9 | machines.json with SDD v1 schema. `load_machines_config()` validates, `get_machine_catalog()` caches. |
| R010-G003-separation | Topology/placement separation | 9 | Machine dataclass holds infra only (ssh_target, home_root, os_family). Worker placement derived from tmux at runtime. |
| R010-G011-local | Implicit local-host resolution | 9 | `_implicit_local_machine()` returns fallback. Machine.is_local=True when ssh_target is None. |

**Min: 9 · Verdict: Acceptable**

### SPEC-011: SOLID in single file [GOAL-025]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R011-G025-boundary | Single-file core boundary | **6** | Core split across 4 files: bridge.py (10685), telegram.py (1605), claudecode.py (1338), shared.py (1099). TelegramAPI, Backend Protocol, both backend impls live outside bridge.py. |
| R011-G025-responsibility | Responsibility separation | 8 | WorkerManager, CommandRouter, ToolManager, TunnelManager, BaseConnector, BridgeHealth distinct. |
| R011-G025-substitutability | Backend substitutability | 9 | Backend Protocol (claudecode.py:257). ClaudeBackend + CodexBackend implement it. Bridge dispatches through protocol. |
| R011-G025-di | Dependency injection | 7 | WorkerManager/TunnelManager take SubprocessRunner+Clock. But no centralized AppContext — ad-hoc per class with module globals as defaults. |

**Min: 6 · Verdict: Needs improvement**
**Assessment:** The 4-file split was manager-directed consolidation from 32 files. If approved architecture, **SPEC text needs updating** (spec-code divergence). If SPEC reflects true goal, code needs consolidation. **Recommend: Update SPEC-011 to "SOLID within core files" — repair, not reimpl.**

### SPEC-012: Message routing [GOAL-001]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R012-G001-target | Explicit message-target correctness | 9 | `_route_text_message`: /commands → handle_command, @all → route_to_all, @mention → one_off, reply-to → originating worker, bare text → active. |
| R012-G001-focus | Focus-transition correctness | 9 | @mention routes with one_off=True (focus unchanged). /focus sets state.active. Bare message uses current active. Auto-focus after 2+ consecutive @mentions is explicit design. |

**Min: 9 · Verdict: Acceptable**

### SPEC-013: Clean chat [GOAL-026]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R013-G026-eyes | Delivery-accurate 👀 feedback | 8 | set_reaction called after verified delivery (send_ok check). Guard on backend type and prompt state. |
| R013-G026-silence | Suppression of routine bridge replies | 8 | route_to_active returns silently on success. Bridge speaks for errors ("Can't find", "offline") and state commands only. |

**Min: 8 · Verdict: Acceptable**

### SPEC-014: Worker registration [GOAL-001]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R014-G001-paths | Registration-path completeness | 9 | /hire → workers.hire, scan_tmux_sessions auto-discovers, POST /workers handles external registration. All 3 paths. |
| R014-G001-recovery | Registered-worker recovery and usability | 9 | workers.json atomic write (tmp+os.replace). Loaded at startup. Corrupt registry renamed .corrupt and recovered empty. Merged with tmux scan. |

**Min: 9 · Verdict: Acceptable**

### SPEC-015: No magic routing [GOAL-018]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R015-G018-status | Manager-readable status | 8 | cmd_team shows health state, backend, activity context, attention timers. cmd_status shows version, uptime, worker counts, machine distribution. |
| R015-G018-explicit | Explicit routing, no inferred reassignment | 9 | No AI summarization, no automatic context sharing. Auto-focus after 2+ @mentions is explicit per-session state, not inference. |

**Min: 8 · Verdict: Acceptable**

### SPEC-016: Hook: minimal/defensive [GOAL-017]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R016-G017-independent | Connector-independent Claude operation | 9 | Hook reads config from tmux env, POSTs to bridge localhost. No Telegram/connector dependency. |
| R016-G017-defensive | Defensive hook behavior | 9 | Guards: exit if no SESSION_NAME, no BRIDGE_SESSION, no chat_id file. set -uo pipefail. Timeouts on curl. Stale transcript alert to /alerts. |

**Min: 9 · Verdict: Acceptable**

### SPEC-017: Single chat UX [GOAL-001]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R017-G001-single | Complete multi-worker management within one DM | 9 | Full suite: /hire, /end, /focus, @mention, @all, /team, /restart, bare message, reply-to, media forwarding, implicit /name focus. Single admin_chat_id. |

**Min: 9 · Verdict: Acceptable**

### SPEC-018: Bridge architecture [GOAL-004, GOAL-025]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R018-G004-separation | Control-plane/tool-serving separation | 9 | ToolManager spawns Bun processes. Bridge never serves tool web pages. /transcript/ routes removed. |
| R018-G025-boundaries | Responsibility and backend-interface boundaries | **7** | Backend Protocol clean. Service classes separated. But: CommandRouter ~1000 lines mixing command handlers with routing; no centralized AppContext/DI. |

**Min: 7 · Verdict: Needs improvement**
**Repair:** Extract command handlers from CommandRouter into standalone functions. Formalize DI container. Bounded change, not reimpl.

### SPEC-019: Machine catalog [GOAL-003, GOAL-011]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R019-G003-catalog | Catalog correctness and derived placement | 9 | GET /machines returns full catalog + worker placement + caller-aware access hints + health. Schema validation. |
| R019-G011-fallback | Missing-config local fallback | 9 | No machines.json → `_implicit_local_machine()` returns one local bridge machine. |

**Min: 9 · Verdict: Acceptable**

### SPEC-020: Connector allowlists [GOAL-016, GOAL-017]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R020-G016-adaptation | Connector protocol adaptation and extension | 9 | BaseConnector[M] generic with abstract methods. GmailConnector + GitHubConnector subclass. sender_filter validated non-empty in __init__. Clear extension pattern. |
| R020-G017-independence | Connector-independent worker operation | 9 | Connectors deliver via on_message callback only. Workers have zero connector dependency. Disabling connectors changes nothing for workers. |

**Min: 9 · Verdict: Acceptable**

### SPEC-021: Behavior tests [GOAL-013, GOAL-014]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R021-G013-integration | Integration behavior coverage | 7 | 537 pytest + 491 test.sh tests. Many genuine behavior tests. Some still lean unit/mock. Good but not complete. |
| R021-G013-acceptance | User acceptance behavior coverage | **5** | No formal UAT category. test.sh integration tests approximate UAT but not labeled or separated. SPEC requires both integration AND acceptance. Tools have e2e (18 tests) but worker lifecycle UAT implicit. |
| R021-G014-isolation | Production isolation | 8 | test.sh: port guard, TEST_NODE≠"prod", `kill_test_bridge()` with tracked PIDs. Minor: `lsof -ti :PORT | xargs kill -9` in 4 pilot cleanup spots. |

**Hard gate HG-PROD-ISOLATION: PASS** (minor lsof pattern, but scoped to specific test port)
**Min: 5 · Verdict: Needs improvement**
**Repair:** Create explicit UAT test category. Label existing behavior tests. Add missing UAT for worker lifecycle. Bounded work.

### SPEC-022: Configurable paths [GOAL-014]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R022-G014-paths | Path configurability | 8 | All major paths env-configurable: SESSIONS_DIR, NODE_DIR, TMUX_PREFIX, NODE_NAME, PORT, BRIDGE_BIND, BRIDGE_URL, WORKER_REGISTRY_FILE, TEAM_DIR. |
| R022-G014-propagation | Config propagation across process boundaries | 8 | `export_hook_env()` uses `tmux set-environment`. Called at 6 entry points (hire, restart, dead restart, POST /workers, teleport, startup). Remote path remapping. |

**Min: 8 · Verdict: Acceptable**

### SPEC-023: Node isolation [GOAL-014]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R023-G014-namespace | Runtime-state namespace isolation | 8 | All paths namespaced: `/tmp/claudecode-telegram/<node>/<worker>/`, sessions by node, locks by node, TMUX_PREFIX includes node. Registry per-node. |

**Min: 8 · Verdict: Acceptable**

### SPEC-024: Process lifecycle safety [GOAL-014]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R024-G014-pid | Node-specific process ownership and lifecycle targeting | **7** | bridge.sh tracks PIDs. bridge.py uses os.kill(pid, 0). `kill_test_bridge()` PID-based. But 4 instances of `lsof -ti :PORT | xargs kill -9` in test.sh for pilot port — pattern-based. bridge.sh has `kill -9` as last-resort fallback. |

**Min: 7 · Verdict: Needs improvement**
**Repair:** Replace lsof+xargs with PID-tracked cleanup in test.sh. Bounded change.

### SPEC-025: Dev-before-prod [GOAL-014, GOAL-015]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R025-G014-dev | Isolated dev verification | 7 | test.sh uses TEST_NODE="test", port 8295. bridge.sh supports --node dev (port 8272). No automated enforcement against direct-to-prod. |
| R025-G015-e2e | Cross-platform E2E on triassic-4 | **4** | No automated cross-platform E2E pipeline. test_chaos_isolated.sh not in repo. Playbook references manual `beast host exec` commands. Fresh-host testing entirely manual. |

**Min: 4 · Verdict: Reimplementation review**
**Assessment: Repair, not reimpl.** The infrastructure exists (triassic-4 user, Python 3.12, beast host exec). Missing: an automated script that runs E2E on triassic-4 from CI or test.sh. This is a new automation task, not a design contradiction.

### SPEC-026: Learning reminders [GOAL-021]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R026-G021-triggers | Reminder-trigger correctness | 8 | `_check_learning_reminder()` fires at response threshold. `_scan_idle_workers()` runs 30-min timer for idle timeout. Tests exist. |
| R026-G021-suppression | Suppression and lifecycle-state correctness | 8 | reminder_pending flag blocks re-fire. `_reset_learning_reminder()` on hire/restart. State persists to JSON. Anti-annoyance test exists. |

**Min: 8 · Verdict: Acceptable**

### SPEC-030: Security model [GOAL-024, GOAL-027]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R030-G024-isolation | Credential isolation | 9 | Bot token in bridge process only. Hook via HTTP localhost, no token. ToolManager generates per-spawn tokens, never passes bridge secrets. |
| R030-G027-boundaries | Trust boundary enforcement | 8 | BRIDGE_BIND defaults 127.0.0.1. Admin allowlist on every inbound. /outputs validates source==worker. Webhook secret available. Non-goals (localhost HMAC, per-role tokens) explicitly documented. |

**Hard gate HG-TOKEN: PASS**
**Min: 8 · Verdict: Acceptable**

### SPEC-031: Logging/observability [GOAL-007]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R031-G007-overhead | Observation overhead | 8 | `_log()` is level-gated print. No file I/O per call. Debug noise filtered in prod. |
| R031-G007-coverage | Failure-signal coverage | 8 | BridgeHealth: 7 signals + 5 thread monitors + 2 pool checks. 14 .ok() and 5 .error() call sites verified. |
| R031-G007-delivery | Channel delivery | 8 | bridge-health relay channel. Alert to channel + BRIDGE_ALERT_WORKERS. External watchdog via direct Telegram API. |
| R031-G007-flood | Flood control | 8 | 300s cooldown, 30min→2min escalation, 1hr snooze. Per-signal tracking. Resolution notifications. |

**Min: 8 · Verdict: Acceptable**

### SPEC-032: Spawn-on-demand tools [GOAL-004, GOAL-005, GOAL-006, GOAL-022, GOAL-024]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R032-G004-lifecycle | Spawned-tool lifecycle and control-plane separation | 8 | ToolManager allocates ports 10171-10179, spawns Bun via Popen, tracks PIDs, reaps from watchdog. Bridge never serves tool pages. |
| R032-G005-complete | Per-tool functional completeness | **7** | Rewind: full search/index/stats. Pilot: full terminal. Review: serves HTML + comment POST. But review comment→Telegram relay is partially in bridge.py (handle_pr_comment), not fully in tool. Split responsibility. |
| R032-G006-bun | Bun/TypeScript execution | 8 | All three tools: rewind/src/rewind.ts, pilot/src/pilot.ts, review/src/serve.ts. ToolManager runtime="bun". |
| R032-G022-stress | Per-tool stress/chaos verification | 8 | test_tools_perf_chaos.sh (602 lines, ~30 tests): search perf, SQL injection, concurrent load, malformed JSONL, pilot auth flood. All three tools covered. |
| R032-G024-creds | Credential isolation | 9 | Per-spawn tokens via secrets.token_urlsafe(32). No bridge secret passed. BOT_TOKEN never reaches tools. |

**Hard gate HG-TOKEN: PASS**
**Min: 7 · Verdict: Needs improvement**
**Repair:** Move review comment relay fully into review tool. Bridge receives callback, tools handle the Telegram-facing relay. Bounded change.

### SPEC-033: Worker state and migration consistency [GOAL-009, GOAL-010, GOAL-011, GOAL-012, GOAL-020]

| ID | Dimension | Score | Evidence |
|----|-----------|-------|----------|
| R033-G009-wd-cwd | Working-directory/CWD distinction | 8 | `_set_worker_cwd()` persists working_dir in registry. `get_claude_session_cwd()` reads OS CWD from session files. SessionStart hook guides checkin. Naming inconsistent (registry field "cwd" = working_dir). |
| R033-G010-recovery | Minimal recovery state and migration consistency | 7 | Registry persists host, cwd, backend, hire_time. session_id is file-based (not in registry), adding complexity during migration. SPEC says it's a candidate but not yet consolidated. |
| R033-G011-local | Local-host semantics | **6** | `_registry_add` preserves existing host from preserved_keys. Local /hire after teleport does NOT clear stale remote host. SPEC invariant violated. Known bug (Option A fix pending manager approval). |
| R033-G012-stagger | 7-second restart staggering | 9 | `_run_restart_all_sequence`: delay_s = 7. Loop sleeps between workers with abort check. |
| R033-G020-authoritative | Authoritative worker-state resolution | 8 | `_scan_latest_session_id()` selects newest JSONL by mtime. `get_claude_session_id(authoritative=True)`. Bridge owns state without asking manager. |

**Min: 6 · Verdict: Needs improvement**
**Repair:** Apply Option A (clear stale host on local hire). Consolidate session_id into registry. Both bounded changes.

---

## Action Items (Ordered by Impact)

### Must Fix (contract violations)

1. **SPEC-033 D3 (score 6):** Host clearing on local hire — apply Option A fix in `_registry_add`. SPEC invariant currently violated.
2. **SPEC-025 D2 (score 4):** Automated cross-platform E2E — create `test_e2e_triassic4.sh` using existing infrastructure.
3. **SPEC-021 D2 (score 5):** Formal UAT category — label and separate acceptance tests from integration tests.

### Should Fix (partial implementations)

4. **SPEC-011 D1 (score 6):** SPEC text says "single file" but manager approved 4-file architecture — update SPEC-011 text to match approved design.
5. **SPEC-024 D1 (score 7):** Replace `lsof -ti :PORT | xargs kill -9` with PID-tracked cleanup in test.sh.
6. **SPEC-032 D2 (score 7):** Move review comment→Telegram relay fully into review tool.
7. **SPEC-018 D2 (score 7):** Formalize DI pattern; reduce CommandRouter monolith.
8. **SPEC-033 D2 (score 7):** Consolidate session_id into workers.json registry.

### Cleanup

9. **SPEC-003:** Remove dead `import sqlite3` from bridge.py.

---

## Traceability Notes

1. **SPEC-011 spec-code divergence:** The 4-file split was a manager-directed consolidation from 32→4 files. If this is the approved architecture, SPEC-011 text must be updated — the low score reflects a documentation gap, not a code failure.
2. **SPEC-020 allowlist provenance:** GOAL-016 (connector adaptation) and GOAL-017 (connector independence) address architecture, not security. The sender allowlist's security obligation traces to AUDIT-001 via legacy provenance exception.
3. **SPEC-026 → GOAL-021 nominal:** GOAL-021 says "docs updated with every change." Reminders support this but cannot guarantee it. Structural — dimension scores the mechanism, not the outcome.
