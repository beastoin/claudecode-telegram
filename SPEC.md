# Design Philosophy

> Version: 0.52.14

## Documentation Contract

This document is the **spec** — the source of truth for system design and architecture.
Authority flows downstream through five layers. Citations point upstream only.

```
Layer 0: Manager Messages (MSG)    ← raw source, transcript timestamps
           ↑ cited by
Layer 1: Understanding (UND)       ← agent interpretation of messages
           ↑ cited by
Layer 2: Goals & Constraints (GOAL) ← project-level, durable
           ↑ cited by
Layer 3: Product Spec (SPEC)       ← feature-level contracts
           ↑ cited by
Layer 4: Implementation            ← code + tests
```

```
Messages → Understanding → Goals → Spec → AGENTS.md → Code → Tests
   ↑            ↑            ↑       ↑         ↑         ↑       ↑
 manager      agent       agent   manager    agent     agent   agent

Authority flows →  (downstream)
Citations point ←  (upstream only)
```

**Rules:**
- Citations point upstream only. Code cites `[SPEC-NNN]`. SPEC cites `[GOAL-NNN]`. GOAL cites `[UND-NNN]`. UND cites `[MSG-NNN]`. Never reverse.
- Manager owns approval authority over all layers. Agent drafts UND/GOAL/SPEC; manager approves. Agent owns Layer 4 (code + tests) and AGENTS.md.
- When a layer changes, everything downstream is invalidated and must be updated (cascade forward).
- When code diverges from spec, agent STOPS and notifies manager — never patch downstream and hope upstream catches up.

**Citation format:**
- Code cites: `# SPEC-NNN` (in comments on key decisions)
- AGENTS.md cites: `[SPEC-NNN]` for product specs, `[GOAL-NNN]` for process constraints
- SPEC sections cite: `[GOAL-NNN]`
- GOAL entries cite: `[UND-NNN]`
- UND entries cite: `[MSG-NNN]`
- Tests cite: test function names trace to spec behavior

**GOAL coverage rules:**
- **Product GOALs** (Architecture, Product goal, Security, UX types): must be cited by at least one `SPEC-NNN`.
- **Process GOALs** (Process, Operational types): may be cited by `AGENTS.md` instead of a SPEC. Process rules constrain HOW work is done, not WHAT the product does.
- **Open GOALs** (marked `[open]`): pending manager decision. No consumer required until resolved.
- **One-off UNDs** (marked `[disposition: task]`): investigation tasks, not durable goals. No GOAL required.
- **Legacy provenance:** GOALs citing FB/AUDIT entries have incomplete MSG→UND chains (pre-2026 directives). This is a known exception, not a contract violation. Do not manufacture historical quotes.

**Contract invariants:**
- Every `SPEC-NNN` in the Spec Index has exactly one detailed section below.
- Every detailed `SPEC-NNN` section appears in the Spec Index.
- Every product `GOAL-NNN` is cited by at least one `SPEC-NNN` (unless marked `[open]`).
- Every process/operational `GOAL-NNN` is cited by at least one `SPEC-NNN` or by `AGENTS.md` (unless marked `[open]`).
- Every `UND-NNN` is cited by at least one `GOAL-NNN` (or has `[disposition: task]`).
- Every `MSG-NNN` is cited by at least one `UND-NNN`.

**Intake IDs:** Every input that changes the spec gets an ID:
- Manager messages → `MSG-{NNN}` (raw quote + UTC timestamp)
- Understanding → `UND-{NNN}` (agent interpretation)
- Goals → `GOAL-{NNN}` (project constraint)
- Legacy feedback → `FB-{NNN}` (pre-layered entries, being migrated to MSG/UND/GOAL)
- Audit findings → `AUDIT-{NNN}`

---

## Feedback Log (Legacy)

Pre-layered entries. Being migrated to MSG → UND → GOAL chain.

| ID | Source | Approx Date | Summary |
|----|--------|------|---------|
| FB-001 | Manager initial design | 2024-06 | Core architecture: tmux persistence, single chat, fail loudly, token isolation |
| FB-002 | Manager | 2024-10 | Workers should communicate directly, not through the bridge |
| FB-003 | Manager | 2025-01 | Multi-machine support: static topology, runtime workers |
| FB-004 | Manager quality push | 2025-04 | SOLID principles, DI, type safety — all within bridge.py |
| AUDIT-001 | Security review | 2024-07 | Token isolation, file permissions, connector allowlists |
| FB-005 | Manager reorg | 2026-08 | Project restructure: connectors/, tools/, tests/, experiments/ |
| FB-006 | Manager doc contract | 2026-09 | Adopt voxboard spec/citation system for docs-as-contract |
| AUDIT-002 | Operational incidents | 2024-09 | pkill killed prod, direct-to-prod deploy broke v0.9.2 |

---

## Layer 0: Manager Messages (MSG)

Raw manager quotes with UTC timestamps. Source: `beast transcript search --prefix "manager:" --worker lee`, sessions 9afdbdce (Mar–Jun 2026), 13661579 (Jun–Oct 2026).

| ID | Date | Raw Quote |
|----|------|-----------|
| MSG-001 | 2026-04-29 12:19 | "1/ remove that redundant pls 2/ B. wait for 7s each worker." |
| MSG-002 | 2026-05-06 02:00 | "again!!! did you really check the log and having fact before fix things????" |
| MSG-003 | 2026-05-13 06:34 | "2 fragmented watchdog is too complicated. can we move it to python? architect wise ofc?" |
| MSG-004 | 2026-05-16 07:50 | "dont mess the integration test and user acceptance test. we need both." |
| MSG-005 | 2026-05-17 03:48 | "the design is bad, dont tightly with one deployment target like docker" |
| MSG-006 | 2026-05-17 03:56 | "build the command base on the worker functionality and the users is the normal manager. why put the internal facing to the user?" |
| MSG-007 | 2026-05-17 06:02 | "we should not reimpl the claudecode telegram bridge py, instead lets having a good architect for the connector that understand the bridge protocol" |
| MSG-008 | 2026-05-17 06:17 | "each connector need specific way to communicate, make sure you respect them, make the architect clean and easy to add more connectors in future" |
| MSG-009 | 2026-05-17 11:37 | "async to thread or loop executor? we need to standardize. which one is better?" |
| MSG-010 | 2026-05-17 12:06 | "i dont think workers should includes the bridge env vars on manifest since they can run on another connectors" |
| MSG-011 | 2026-05-17 12:09 | "my insight: claudecode is a requirement independent to the connector." |
| MSG-012 | 2026-05-17 13:31 | "yes pls fix all three structural issue and also update claude md agent md for the future self" |
| MSG-013 | 2026-10-16 01:50 | "why mae keep telling me the bridge down???? ask him the friction and fix it completely at root cause can u" |
| MSG-014 | 2026-10-16 03:02 | "ask mae to check why he keep sending 2 messages its kind of a waste :)" |
| MSG-015 | 2026-10-16 03:03 | "wait our worker can use that curl directly?" |
| MSG-016 | 2026-10-17 02:07 | "how can we have the best logs/observation to detect these failure signal of bridge py? currently they are blind" |
| MSG-017 | 2026-10-17 02:33 | "the observation layer need to be cheap. and best practice, cover all product failure signal ok? rework" |
| MSG-018 | 2026-10-17 04:14 | "our bridge has the channel infrastructure right? can we leverage them?" |
| MSG-019 | 2026-10-17 04:48 | "did you have a good managing code for all kind of alerting likes flooding?" |
| MSG-020 | 2026-10-17 05:39 | "why that bridge watchdog should be sh but not python?" |
| MSG-021 | 2026-10-17 08:47 | "you should work with luck to streamlines how to use bridge for our team (dont mention any noise!) keep the single source of truth and clean the confusing on all surface" |
| MSG-022 | 2026-10-17 09:21 | "for the tools likes rewinding/piloting, i dont think the bridge owning the webserver is the right architect. The bridge should spawn the right tool, setup cred/token/life... spawn on demand!" |
| MSG-023 | 2026-10-17 12:10 | "the are tools not viewers solely, they do have the api call got? make sure investing to end to end testing!" |
| MSG-024 | 2026-10-17 12:20 | "now its tools or viewers? all rewind, pilot, pr review supported with no regression?" |
| MSG-025 | 2026-10-17 12:55 | "!!!!" (response to Python proposal for tools — agent noted Bun/TS directive) |
| MSG-026 | 2026-10-17 13:01 | "they are tools! and dont get back to me if they are not done completely" |
| MSG-027 | 2026-10-18 01:40 | "architect wise, are these tools talk to the bridge?" |
| MSG-028 | 2026-10-18 01:43 | "how about the review i thought they have the comment feature which need to relay the comment to the telegram?" |
| MSG-029 | 2026-10-18 04:36 | "again how can test code touch to your tmux in prod node? fix it completely the isolated level!" |
| MSG-030 | 2026-10-18 06:53 | "you owns the bridge you should not ask me about their correct working dir and the claudecode session since you literally owns the bridge!" |
| MSG-031 | 2026-10-18 07:20 | "are u really understand the diff of working dir and cwd and all its mismatch state" |
| MSG-032 | 2026-10-18 07:24 | "the session start hook should guide worker to run the checkin with their working dir, not specific the cwd." |
| MSG-033 | 2026-10-18 07:26 | "so any reason why do we need to persistent the worker pwds?" |
| MSG-034 | 2026-10-18 08:05 | "so what is the minimal persistent state of the worker bridge should keep, eg claudecode session id, working dir..." |
| MSG-035 | 2026-10-18 08:20 | "on teleport, teleback they are syncing the claudecode session as well so we don't need the extra field" |
| MSG-036 | 2026-10-18 08:59 | "spec follows? end to end test with new host in triassic-4?" |
| MSG-037 | 2026-10-19 | "isnt the default host should be the current bridge host and bridge understand it well?" |
| MSG-038 | 2026-10-18 01:20 | "run perf analysis chaos for each tools on their usages eg search on the rewind" |
| MSG-039 | 2026-10-19 | "you guys should learn completely about our bridge terms eg machines, workers, etc make them supersolid" |

---

## Layer 1: Understanding (UND)

Agent interpretation of manager messages. Each cites the raw MSG it derives from.

| ID | Understanding | Cites |
|----|---------------|-------|
| UND-001 | Bridge is the control plane: it spawns, manages, and routes. Tool UIs (rewind, pilot, PR review) are separate processes the bridge spawns on demand with credentials and lifecycle. Bridge's own HTTP API and webhook are NOT affected. | MSG-022, MSG-027 |
| UND-002 | All three tools — rewind, pilot, PR review — must be fully functional (including API calls like review comments → Telegram relay), not just page viewers. No regression from pre-extraction behavior. | MSG-023, MSG-024, MSG-026, MSG-028 |
| UND-003 | Manager reacted strongly against Python for tools (`"!!!!"` when Python was proposed). Bun/TypeScript is the required runtime. [impl — original Bun/TS instruction predates this transcript window; MSG-025 is reinforcement, not sole evidence. Provenance incomplete.] | MSG-025 |
| UND-004 | Observation must be cheap (low overhead), cover ALL product failure signals including tools/hooks, and use the existing relay channel infrastructure for alert delivery with flood management. | MSG-016, MSG-017, MSG-018, MSG-019 |
| UND-005 | Workers discover peers via `/workers?from=<name>` and send directly. Bridge provides discovery only, not relay. Workers should not manually call internal endpoints. "Don't mention noise" means worker-facing docs must not teach internal endpoint names. Does NOT prohibit `/workers`, `/checkin`, automatic hooks, or tool callbacks. | MSG-015, MSG-021 |
| UND-006 | Working directory (the project a worker is assigned to) is different from CWD (the OS-level current directory). The SessionStart hook should guide workers to checkin with their working directory, not hard-set the CWD. | MSG-031, MSG-032 |
| UND-007 | Bridge should persist only what's essential for restart/teleport consistency. Manager named session_id and working_dir as candidates. Teleport already syncs sessions — don't add redundant fields. MSG-033 is a question ("any reason?"), not a prohibition. | MSG-033, MSG-034, MSG-035 |
| UND-008 | When host field is absent in registry = local to this bridge. Stale remote host from teleport must be cleared on local hire. | MSG-037 |
| UND-009 | Manager specified 7-second wait between workers. [impl — context was `/restart all`; `.claude.json` race rationale is agent interpretation from observed behavior, not explicit in MSG-001.] | MSG-001 |
| UND-010 | Integration tests and user acceptance tests are distinct. Both are required. "Behavior tests" means testing what users care about, not code structure. | MSG-004 |
| UND-011 | Test code must NEVER touch prod tmux sessions or processes. Complete isolation. Cross-platform E2E on triassic-4 required, including fresh-host scenarios. | MSG-029, MSG-036 |
| UND-012 | Connectors adapt to the bridge wire protocol — don't reimplement bridge. Each connector respects its own communication semantics. Architecture must be clean and easy to extend. | MSG-007, MSG-008 |
| UND-013 | Claude Code (hooks, runtime) is independent of the connector (transport). Workers should not include bridge env vars in manifests since they can run on different connectors. | MSG-010, MSG-011 |
| UND-014 | Concurrency model should be standardized. Manager asked "thread or loop executor?" Decision not recorded in transcript. Current impl uses ThreadPoolExecutor. | MSG-009 |
| UND-015 | Commands are for managers (the user), not developers. Never expose internal-facing implementation details in user-facing commands. | MSG-006 |
| UND-016 | Always check logs and get facts before fixing. Never guess at root cause. Complete fixes, not patches — but defensive guards are OK as part of a complete solution. | MSG-002, MSG-013 |
| UND-017 | Bridge owner must know worker state (CWD, session, host) authoritatively without asking the manager. | MSG-030 |
| UND-018 | Manager requires docs to be kept accurate alongside code changes. "Update claude md agent md for the future self" and "spec follows?" mean: when code changes, ensure affected docs reflect the change. Authority still flows SPEC → Code per the documentation contract. | MSG-012, MSG-036 |
| UND-019 | Each tool must be stress-tested: perf analysis / chaos for realistic operations (search on rewind, terminal on pilot, comments on review). Not just generic bridge resilience. | MSG-038 |
| UND-020 | Manager challenged fragmented bash watchdogs, asked about moving to Python. Internal health is already Python. External bash watchdog scope is open — no final decision recorded. | MSG-003, MSG-020 |
| UND-021 | Project terminology (machines, workers, hosts, sessions) must be "supersolid" — precise, consistent, no ambiguity. | MSG-039 |
| UND-022 | Don't couple to deployment targets (Docker, etc.). Commands should be based on worker functionality for manager use, not internal infrastructure. [impl — primarily forge-context but principle applies broadly.] | MSG-005, MSG-006 |
| UND-023 | Manager questioned why a worker sends 2 messages — investigate and fix the waste. Does not establish a universal one-message limit. [disposition: task — one-off investigation, not durable goal] | MSG-014 |

---

## Layer 2: Goals & Constraints (GOAL)

Project-level goals and durable constraints. Each cites the UND it derives from.
GOALs citing FB/AUDIT entries predate the MSG→UND layer; their provenance chain is incomplete (no transcript quotes available for pre-2026 directives).

| ID | Goal | Type | Cites |
|----|------|------|-------|
| GOAL-001 | Single-chat multi-worker management from Telegram | Product goal | FB-001 |
| GOAL-002 | tmux sessions are the persistence layer — no database | Architecture | FB-001 |
| GOAL-003 | Multi-machine worker mobility via teleport/teleback | Product goal | FB-003 |
| GOAL-004 | Bridge = control plane; tools are spawned on demand | Architecture | UND-001 |
| GOAL-005 | Tools are fully functional (API + relay), not just viewers | Product goal | UND-002 |
| GOAL-006 | Tools run under Bun/TypeScript | Architecture | UND-003 |
| GOAL-007 | Observation: cheap, complete, channel-delivered, flood-managed | Product goal | UND-004 |
| GOAL-008 | P2P worker messaging; bridge provides discovery only | Architecture | UND-005 |
| GOAL-009 | Working directory ≠ CWD; hook guides checkin with working dir | Architecture | UND-006 |
| GOAL-010 | Minimal persistent state; session_id + working_dir are candidates named by manager — consistent across restart/teleport/teleback | Architecture | UND-007 |
| GOAL-011 | Default host = bridge host; absence of host field = local | Architecture | UND-008 |
| GOAL-012 | 7-second restart stagger (manager specified the delay; auth file race rationale is [impl]) | Operational | UND-009 |
| GOAL-013 | Both integration tests AND user acceptance tests required | Process | UND-010 |
| GOAL-014 | Test isolation: test code never touches prod | Process | UND-011 |
| GOAL-015 | Cross-platform E2E on triassic-4 including fresh-host | Process | UND-011 |
| GOAL-016 | Connectors adapt to bridge protocol; easy to extend | Architecture | UND-012 |
| GOAL-017 | Claude Code independent of connector; hooks ≠ transport | Architecture | UND-013 |
| GOAL-018 | Commands for managers, not developers | UX | UND-015 |
| GOAL-019 | Facts before fixes; complete solutions, not patches | Process | UND-016 |
| GOAL-020 | Bridge owner knows worker state authoritatively | Process | UND-017 |
| GOAL-021 | Docs updated with every code change | Process | UND-018 |
| GOAL-022 | Per-tool stress/chaos testing for realistic operations | Process | UND-019 |
| GOAL-023 | Precise, consistent project terminology | Process | UND-021 |
| GOAL-024 | Token isolation: Claude never sees bot token | Security | FB-001, AUDIT-001 |
| GOAL-025 | SOLID within single file (bridge.py) | Architecture | FB-004 |
| GOAL-026 | Clean chat: 👀 = received, bridge speaks only for errors | UX | FB-001 |
| GOAL-027 | Secure by default: localhost binding, admin allowlist, webhook secret | Security | FB-001, AUDIT-001 |
| GOAL-028 | Single-source-of-truth worker guidance; no noise in docs | Process | UND-005, UND-021 |
| GOAL-029 | Don't couple to deployment targets; commands for managers, not infra [impl — MSG source is forge context; generalization to bridge-wide is agent interpretation] | Process | UND-022 |
| GOAL-030 | Concurrency model standardization [open — decision not recorded] | Architecture | UND-014 |
| GOAL-031 | Watchdog consolidation [open — external watchdog scope undecided] | Architecture | UND-020 |

---

## Spec Index

Spec IDs are stable references and may not appear in numeric order in the document.
Each SPEC cites the GOAL(s) it implements.

| ID | Title | Status | GOAL | Legacy Citation | Commit |
|----|-------|--------|------|-----------------|--------|
| SPEC-001 | tmux IS persistence | Active | GOAL-002 | FB-001 | 8d6a973 |
| SPEC-002 | `claude-<name>` naming | Active | GOAL-002 | FB-001 | 8d6a973 |
| SPEC-003 | No external database (file + RAM) | Active | GOAL-002, GOAL-010 | FB-001 | 8d6a973 |
| SPEC-004 | Per-session files | Active | GOAL-002 | FB-001 | 8d6a973 |
| SPEC-005 | Fail loudly | Active | GOAL-002 | FB-001 | 8d6a973 |
| SPEC-006 | Token isolation | Active | GOAL-024 | FB-001, AUDIT-001 | 8d6a973 |
| SPEC-007 | Admin config | Active | GOAL-027 | FB-001 | 8d6a973 |
| SPEC-008 | Secure by default | Active | GOAL-027 | FB-001, AUDIT-001 | 8d6a973 |
| SPEC-009 | Decentralized worker comms | Active | GOAL-008 | FB-002 | 8d6a973 |
| SPEC-010 | Machines are config | Active | GOAL-003, GOAL-011 | FB-003 | 8d6a973 |
| SPEC-011 | SOLID in a single file | Active | GOAL-025 | FB-004 | 8d6a973 |
| SPEC-012 | Message routing | Active | GOAL-001 | FB-001 | 8d6a973 |
| SPEC-013 | Feedback philosophy (clean chat) | Active | GOAL-026 | FB-001 | 8d6a973 |
| SPEC-014 | Worker registration (3 paths) | Active | GOAL-001 | FB-001 | 8d6a973 |
| SPEC-015 | No magic routing | Active | GOAL-018 | FB-001 | 8d6a973 |
| SPEC-016 | Hook: minimal and defensive | Active | GOAL-017 | FB-001 | 8d6a973 |
| SPEC-017 | Single chat UX | Active | GOAL-001 | FB-001 | 8d6a973 |
| SPEC-018 | Bridge architecture (OOP) | Active | GOAL-004, GOAL-025 | FB-004 | 8d6a973 |
| SPEC-019 | Machine catalog | Active | GOAL-003, GOAL-011 | FB-003 | 8d6a973 |
| SPEC-020 | Connector sender allowlists | Active | GOAL-016, GOAL-017 | AUDIT-001 | 8d6a973 |
| SPEC-021 | Behavior tests required | Active | GOAL-013, GOAL-014 | FB-001 | 2b6cd8f |
| SPEC-022 | Configurable paths (no hardcoding) | Active | GOAL-014 | AUDIT-002 | 2b6cd8f |
| SPEC-023 | Node isolation | Active | GOAL-014 | FB-003 | 2b6cd8f |
| SPEC-024 | Process lifecycle safety (PID-based) | Active | GOAL-014 | AUDIT-002 | 2b6cd8f |
| SPEC-025 | Dev-before-prod deployment | Active | GOAL-014, GOAL-015 | AUDIT-002 | 2b6cd8f |
| SPEC-026 | Learning reminders (periodic self-reflection) | Active | GOAL-021 | — | — |
| SPEC-030 | Security model | Active | GOAL-024, GOAL-027 | AUDIT-001 | — |
| SPEC-031 | Structured logging and observability | Active | GOAL-007 | MSG-016, MSG-017 | — |
| SPEC-032 | Spawn-on-demand tools | Active | GOAL-004, GOAL-005, GOAL-006, GOAL-022, GOAL-024 | MSG-022 | — |
| SPEC-033 | Worker state and migration consistency | Active | GOAL-009, GOAL-010, GOAL-011, GOAL-012, GOAL-020 | MSG-034 | — |

---

## SPEC-001. tmux IS the Persistence. [GOAL-002]

The most important design decision: **tmux sessions are the primary source of truth for worker state**. [FB-001] The bridge derives worker presence, online status, and routing from tmux on demand. Supplementary JSON files (`workers.json`, `guest_state.json`, `channel_state.json`, `relay_state.json`, `last_chat_id`, `last_active`) persist feature-specific data under the node directory.

```
Traditional approach:          This approach:
┌─────────────────┐            ┌─────────────────┐
│   state.json    │            │   tmux sessions │ ← source of truth
│   database      │            │   claude-backend│
│   config files  │            │   claude-frontend
└────────┬────────┘            └────────┬────────┘
         │                              │
    read/write                     scan on demand
         │                              │
    ┌────▼────┐                    ┌────▼────┐
    │ gateway │                    │ gateway │
    └─────────┘                    └─────────┘
```

### Why This Matters

1. **Gateway crashes? No problem.** Restart it. Scan tmux. Reload JSON files. Continue.
2. **No sync issues.** Worker presence derives from tmux (always fresh). Lifecycle metadata persists in JSON files (see SPEC-003).
3. **Manual tmux usage works.** Start `claude` in any `claude-*` session. The bridge finds it.
4. **Debugging is trivial.** Run `tmux list-sessions` to see what exists.

## SPEC-002. Naming Convention: `claude-<name>`. [GOAL-002]

The user says `/hire backend`. The bridge creates tmux session `claude-backend`.

This prefix pattern gives you:
- **Auto-discovery**: `tmux list-sessions | grep ^claude-` finds all managed sessions
- **Namespace isolation**: No conflict with other tmux sessions
- **Clear ownership**: You can see which sessions the bridge manages

## SPEC-003. No External Database: File + RAM State. [GOAL-002, GOAL-010]

```python
state = {
    "active": "backend",      # Which session receives bare messages
}
```

Core worker state (which sessions exist, who is active) is:
- Derived from tmux on demand
- Authoritative only for the "active" selection (user preference)

Supplementary features persist data to JSON files:
- `workers.json` — worker registry (survives tmux crashes)
- `guest_state.json` — guest tokens and inboxes
- `channel_state.json` — group channel membership
- `relay_state.json` — relay link tokens
- `last_active`, `last_chat_id` — focus and admin persistence

## SPEC-004. Per-Session Files: Minimal Coordination. [GOAL-002]

```
~/.claude/telegram/sessions/
├── <worker_name>/           # e.g. "backend", "frontend", "lee"
│   ├── pending              # Timestamp when request started
│   └── chat_id              # Where to send the response
└── <another_worker>/
    ├── pending
    └── chat_id
```

Why files instead of IPC?
- The hook runs in Claude's process, not in the bridge process.
- Files are the simplest cross-process communication method.
- The hook only needs two facts: "where do I send this?" and "should I send at all?"

## SPEC-012. Message Routing: Simple Rules. [GOAL-001]

```
Input                    → Routes to
─────────────────────────────────────
/hire backend            → creates claude-backend, sets active
/focus frontend          → sets active = frontend
@backend do something    → claude-backend (one-off, focus unchanged)
fix the bug              → active session (currently frontend)
```

`@name` mentions route messages without changing focus. Use `/focus <name>` to switch.

## SPEC-013. Feedback Philosophy (Clean Chat). [GOAL-026]

- 👀 means the message reached the worker.
- The worker reply is the confirmation: `worker_name: response`.
- The bridge sends text replies only for errors and state commands (`/hire`, `/end`, `/focus`, `/team`).
- Regular messages and `@mentions` are silent.
- The manager wants a clean chat. The emoji gives instant feedback.
- The bridge speaks only when no worker reply will come.

## SPEC-014. Worker Registration (3 Paths). [GOAL-001]

Workers register through three paths:

1. **`/hire` command** — the bridge creates a tmux session and registers the worker.
2. **Auto-discovery** — the bridge scans for tmux sessions that match the `TMUX_PREFIX` pattern on startup.
3. **`POST /workers`** — external workers (forge, callback-based) register through the HTTP endpoint.

The persistent worker registry (`workers.json`) survives bridge restarts. The bridge rebuilds runtime state from tmux sessions and the registry on startup.

## SPEC-015. No Summaries, No Magic. [GOAL-018]

The `/team` command shows workers with health state, focus indicator, backend, and activity context.

```
👤 Team Status
  lee ← focus [WORKING] (claude) attention: 3m, reviewing PR #4920
  riku [READY] (claude) idle 12m
  mon [WORKING] (codex) attention: 1m, running cost analysis
```

Internal health states: READY, BUSY_TOOL, BUSY_THINKING, STUCK, DEAD, OFFLINE, HOST_OFFLINE, POISONED, EXITED, WAITING_INPUT. The `/team` display maps BUSY_TOOL and BUSY_THINKING to "Working".

The bridge deliberately avoids:
- AI-generated summaries of what each worker does
- Automatic context sharing between sessions
- "Smart" routing based on message content

Why?
1. Each worker session has its own context and project.
2. The manager knows which worker should handle each task.
3. Magic routing fails often enough to cause frustration.

## SPEC-005. Error Handling: Fail Loudly, Recover Gracefully. [GOAL-002]

- Session does not exist? Tell the user immediately.
- tmux died? The next message reports it.
- Bridge restarted? Scan tmux and continue.

No silent failures. No retry loops that hide problems.

## SPEC-016. The Hook: Minimal and Defensive. [GOAL-017]

The hook (`claudecode.sh`) runs on every Claude stop event. It reads configuration from the tmux session environment:

- `BRIDGE_URL` or `PORT` — where to send the response
- `TMUX_PREFIX` — to derive the worker name
- `SESSIONS_DIR` — to find session files

The hook POSTs extracted text to `{BRIDGE_URL}/outputs`. It also writes `claude_session_id` and `claude_session_cwd` to the session directory. If the JSONL transcript is stale, it sends an alert to `/alerts`.

```bash
# Key guard — exit if no chat_id (bridge never set one for this session):
[ ! -f "$CHAT_ID_FILE" ] && exit
```

The `pending` file is used for the busy indicator in `/team`. It is not a send gate (changed in v0.6.2 to enable proactive messaging).

## SPEC-017. Why Single Chat? [GOAL-001]

One Telegram DM manages all Claude instances because:
1. **Context stays in one place.** Scroll up to see what you asked any Claude.
2. **No channel or group management.** Just DM the bot.
3. **Mobile-friendly.** One conversation with explicit routing via commands.

The `@name` syntax and `/focus` command give full control without the overhead of multiple chats.

## SPEC-018. Bridge Architecture. [GOAL-004, GOAL-025]

The bridge uses small, explicit classes:

- **Backend Protocol** (`typing.Protocol`): `Backend` interface with `name`, `binary`, `is_interactive`, `start_cmd(resume_id="")`, `send()`, `is_online()`.
- **Backend implementations**: `ClaudeBackend` (interactive), `CodexBackend` (non-interactive). All live in `bridge.py`.
- **WorkerManager**: Worker lifecycle and routing (`hire`, `end`, `send`, `is_online`, `get_workers`, `scan_tmux_sessions`).
- **TelegramAPI**: Wraps all Telegram API calls (sendMessage, sendPhoto, sendDocument, etc.).
- **CommandRouter**: All `/command` handlers and message routing. Delegates to `WorkerManager` and `TelegramAPI`.

The bridge detects interactive vs non-interactive mode from `backend.is_interactive`. It does not hardcode backend names.

## SPEC-010. Machines Are Config. [GOAL-003, GOAL-011]

Static host topology lives in `machines.json`. Machines are infrastructure, not runtime state. The bridge reads the machine catalog on startup and serves it via `/machines`. Worker placement is derived from tmux sessions at runtime — it is never stored in the machine config.

Adding a machine: add an entry to `machines.json` with `ssh_target`, `bridge_base_url`, `home_root`, `os_family`. The bridge discovers workers on all configured machines.

## SPEC-011. SOLID in a Single File. [GOAL-025]

All bridge logic lives in `bridge.py`. The architecture uses SOLID principles within a single file:

- **AppContext** for dependency injection (subprocess, clock, urlopen)
- **TypedDicts** and **NamedTuples** for data structures (58 TypedDicts, 9 NamedTuples)
- **Service classes** for responsibilities (WorkerManager, TelegramAPI, CommandRouter)
- **Protocols** for interfaces (Backend, SubprocessRunner, Clock)
- **Registry dispatch** for extensible command routing

CommandRouter logic is inlined (mixins were removed during consolidation). Connector implementations live in `connectors/`, not in bridge.py.

This is a deliberate choice: one file for core logic keeps the system greppable, diffable, and simple to deploy. See the changelog for the quality push history (v0.33.0 through v0.44.0).

## SPEC-019. Machine Catalog. [GOAL-003, GOAL-011]

**Status:** Read-only catalog endpoint.

Machines are static infrastructure, not runtime worker state. The bridge reads `MACHINES_CONFIG_FILE` (default `~/.config/claudecode-telegram/machines.json`) with the SDD v1 schema:

```json
{
  "version": 1,
  "machines": {
    "vps": {
      "ssh_target": null,
      "bridge_base_url": "http://localhost:8271",
      "home_root": "/home/claude",
      "os_family": "linux",
      "display_name": "VPS",
      "tailscale_ip": "100.125.36.102",
      "role": "bridge"
    }
  }
}
```

Required fields match the SDD: `ssh_target`, `bridge_base_url`, `home_root`, `os_family`. Optional fields (`display_name`, `tailscale_ip`, `role`) are operator metadata for `/machines`.

`GET /machines` returns the catalog plus derived worker placement, caller-aware access hints (`local` or `ssh <target>`), and current host health. A missing file falls back to one implicit local bridge machine. A malformed file fails loudly.

## SPEC-009. Inter-Worker Messaging (Decentralized Discovery). [GOAL-008]

**Status:** Available (tmux send-keys + named pipes)

**Design:** The bridge provides discovery only. Workers communicate directly with each other via p2p (tmux/pipe). `POST /outputs` is reserved for a worker's own hook output to Telegram — it validates that the payload's `source` matches `session` and rejects misuse with a helpful error.

This means:
- **No manager visibility:** Private worker-to-worker conversations stay private.
- **Direct P2P communication:** Workers talk to each other without the bridge.
- **Protocol flexibility:** Each worker advertises how to reach it (tmux, pipe, etc.).
- **Response isolation:** `/outputs` accepts worker output only when the payload identifies the same `source` and `session`; source/session mismatches are rejected.

**Current state:**
- **tmux backends (interactive):** Workers use `flock /tmp/claudecode-telegram/<node>/locks/<session>.lock sh -c 'echo "message" | tmux load-buffer - && tmux paste-buffer -p -r -t claude-<node>-<worker> && sleep 0.05 && tmux send-keys -t claude-<node>-<worker> Enter'`
- **Local non-interactive backends:** Each worker gets a named pipe at `/tmp/claudecode-telegram/<node>/<worker>/in.pipe`
- Remote non-interactive workers, callback workers, and exited workers use other protocols (`adapter`, `http`, `none`).
- The node name comes from `TMUX_PREFIX` (`claude-test-` → `test`, `claude-` → `default`)

**Discovery endpoint:**

```
GET /workers
```

Response:
```json
{
  "workers": [
    {
      "name": "alice",
      "protocol": "tmux",
      "address": "claude-prod-alice",
      "send_example": "flock /tmp/claudecode-telegram/prod/locks/claude-prod-alice.lock sh -c 'echo '\"'\"'your message here'\"'\"' | tmux load-buffer - && tmux paste-buffer -p -r -t claude-prod-alice && sleep 0.05 && tmux send-keys -t claude-prod-alice Enter'"
    },
    {
      "name": "bob",
      "protocol": "pipe",
      "address": "/tmp/claudecode-telegram/<node>/bob/in.pipe",
      "send_example": "echo 'your message here' > /tmp/claudecode-telegram/<node>/bob/in.pipe"
    }
  ]
}
```

**Protocol types:**

| Protocol | Address Format | How to Send | Backends |
|----------|---------------|-------------|------|
| `tmux` | Session name | `flock <lock> sh -c 'echo "msg" \| tmux load-buffer - && tmux paste-buffer -p -r -t <address> && sleep 0.05 && tmux send-keys -t <address> Enter'` | Interactive (local) |
| `pipe` | Named pipe path | `echo "message" > <address>` | Non-interactive (local) |

**Named pipes for non-interactive workers:**

Named pipes (FIFOs) work for local non-interactive backends:
```bash
# Bridge creates on worker startup
mkfifo /tmp/claudecode-telegram/<node>/<worker>/in.pipe

# Worker A sends to Worker B
echo "Hey bob, can you review PR #42?" > /tmp/claudecode-telegram/<node>/bob/in.pipe

# Worker B reads (poll or inotifywait)
cat /tmp/claudecode-telegram/<node>/<worker>/in.pipe
```

**Why this design:**
- Workers collaborate without manager overhead.
- Standard Unix mechanism. No custom protocol.
- Works the same across tmux and exec backends.
- The bridge stays simple — just discovery, no message routing.

**Tests:**
- `test_worker_pipe_creation_on_startup` — pipes created on worker startup
- `test_worker_to_worker_pipe` — end-to-end worker communication via pipe

---

## SPEC-021. Behavior Tests Required. [GOAL-013, GOAL-014]

Every new feature must have an end-to-end behavior test. Tests verify what users care about, not that code structure exists. A test that checks "HTTP returns 200" without verifying delivery is scaffolding, not a behavior test.

**What qualifies as a behavior test:**
- Worker stays alive after hire (not just "hire returned OK")
- Message reaches worker end-to-end (not just "send function exists")
- Pipe delivery works between workers (not just "pipe was created")

See AGENTS.md `[SPEC-021]` for the full testing workflow (TDD, mode gates, test/scaffolding examples).

## SPEC-022. Configurable Paths (No Hardcoding). [GOAL-014]

All paths must be configurable through environment variables with sensible defaults. Hardcoded paths break test isolation and multi-node setups.

```bash
# Right: configurable with default
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/telegram/sessions}"

# Wrong: hardcoded
SESSIONS_DIR="$HOME/.claude/telegram/sessions"
```

Env vars must propagate through the full process chain. Use `tmux set-environment` at each boundary, not `tmux send-keys "export ..."`.

## SPEC-023. Node Isolation. [GOAL-014]

All runtime paths are namespaced by node (derived from `TMUX_PREFIX`). No shared state between prod, dev, and test nodes.

```
/tmp/claudecode-telegram/<node>/<worker>/in.pipe
/tmp/claudecode-telegram/<node>/<worker>/inbox/
/tmp/claudecode-telegram/<node>/locks/<session>.lock
```

This prevents collisions when multiple nodes run simultaneously on the same machine.

## SPEC-024. Process Lifecycle Safety (PID-Based). [GOAL-014]

Always use PID-based process management. Never use pattern-based killing (`pkill`, `killall`). Production runs multiple nodes concurrently — pattern-based killing causes collateral damage.

```bash
# Right: PID-based
./bridge.sh --node prod stop
kill $(cat ~/.claude/telegram/nodes/prod/pid)

# Wrong: pattern-based (kills ALL nodes)
pkill -f bridge.py
lsof -ti :8271 | xargs kill
```

Before killing any port, verify which node owns it: `cat ~/.claude/telegram/nodes/*/port`.

## SPEC-025. Dev-Before-Prod Deployment. [GOAL-014, GOAL-015]

Always test on the dev node before deploying to prod:

1. Start dev bridge with dev bot token on port 8272
2. Run full integration tests against dev
3. Test manually through Telegram on the dev bot
4. Only then deploy to prod

Local/unit tests prove concepts in isolation. Real integration bugs only surface with actual Telegram traffic.

---

## SPEC-006. Token Isolation. [GOAL-024]

The most important security principle: **Claude never sees the bot token.**

Claude is a powerful agent that could inadvertently leak tokens through:
- Tool use (for example, `curl` commands in responses)
- Log files
- Error messages
- Responses to the user

The bridge-centric architecture prevents this:

```
┌─────────────────────────────────────────────────────────┐
│ .env file ──► Gateway/Bridge (ONLY place with token)   │
│                    │                                    │
│                    │ creates tmux (NO token)            │
│                    ▼                                    │
│              Claude session (NO token)       ← SAFE    │
│                    │                                    │
│                    │ hook runs on stop                  │
│                    ▼                                    │
│              Hook (NO token needed)                     │
│                    │                                    │
│                    │ POST localhost:{PORT}/outputs     │
│                    ▼                                    │
│              Bridge ──► Telegram API         ← SAFE    │
└─────────────────────────────────────────────────────────┘
```

PORT varies by node: prod=8271, dev=8272, test=8295, default=8270.

## SPEC-007. Admin Configuration. [GOAL-027]

Two modes:

**1. Pre-configured (recommended for production):**
```bash
ADMIN_CHAT_ID=121604706  # Lock to specific user
```

**2. Auto-learn (default):**
```python
admin_chat_id = None  # First user becomes admin; persisted in last_chat_id
```

```python
def handle_message(update):
    chat_id = update["message"]["chat"]["id"]

    # First user becomes admin (if not pre-configured)
    if admin_chat_id is None:
        admin_chat_id = chat_id

    # Reject non-admins silently
    if chat_id != admin_chat_id:
        return  # Don't reveal bot exists
```

Why two modes?
1. **Pre-configured** — Secure. No race condition on the first message.
2. **Auto-learn** — Zero configuration for quick setup.
3. **Persisted admin** — The learned admin chat ID is stored in `last_chat_id` and restored on bridge restart. Delete the `last_chat_id` file to reset the admin.

## SPEC-008. Secure by Default. [GOAL-027]

### Webhook Verification

If `TELEGRAM_WEBHOOK_SECRET` is set:
1. The bridge adds `secret_token` to the webhook registration.
2. Telegram sends the `X-Telegram-Bot-Api-Secret-Token` header with each update.
3. The bridge verifies the header matches. It rejects mismatches.

If not set, the bridge works without verification. The hook endpoint is still localhost-only.

### File Permissions

All session files use restrictive permissions:
- Directories: `0o700` (owner only)
- Files: `0o600` (owner only)

This prevents other users on multi-user systems from reading chat IDs or session data.

## SPEC-020. Connector Sender Allowlists. [GOAL-016, GOAL-017]

External connectors (Gmail, GitHub) poll third-party APIs and forward messages to the bridge. Each connector has a **mandatory sender filter**. Only messages from the configured sender reach Telegram and workers. The connector drops everything else silently.

| Connector | Env var | Default | What it filters |
|-----------|---------|---------|-----------------|
| Gmail | `GMAIL_FROM_FILTER` | `ngocthinhdp@gmail.com` | Email `From:` header must match |
| GitHub | `BRIDGE_GHPOLL_USER` | `beastoin` | Comment `user.login` must match |

**Architecture:**

```
                          ┌─────────────────────────────┐
  Gmail API ← gws CLI ──►│                             │
                          │  BaseConnector              │
                          │  ├─ sender_filter (required) │
                          │  ├─ extract_sender() ←──── subclass overrides
                          │  └─ is_allowed_sender() ──► reject if mismatch
                          │                             │
  GitHub API ← beast ───►│  on_message() ────────────► Telegram + workers
                          └─────────────────────────────┘
```

**Security invariants:**
- `sender_filter` cannot be empty. `BaseConnector.__init__` raises `ValueError`.
- `is_allowed_sender()` lives in the base class. Subclasses override `extract_sender()` only. They cannot bypass the check.
- CLIs (gws, beast) are dumb transport. They fetch and return JSON. They do not filter.

**How to add a new connector:**
1. Subclass `BaseConnector`.
2. Override `extract_sender(message)`. Return the lowercased sender identity.
3. Override `preflight_check()` and `poll_once()`.
4. Add an env var for the sender filter. Require it to be non-empty (fail-closed).
5. Wire it in `bridge.py` with `_connector_on_message(tag)`, `_connector_get_workers`, `_connector_on_alert(tag)`.

## SPEC-026. Learning Reminders (Periodic Self-Reflection). [GOAL-021]

Workers receive periodic nudges to reflect on what they learned and update their playbooks. Two triggers fire reminders:

1. **Response threshold** — after every 15 worker responses (`LEARNING_REMINDER_RESPONSE_THRESHOLD`).
2. **Idle timeout** — if a worker has responded at least twice but then goes silent for 6 hours (`LEARNING_REMINDER_IDLE_HOURS`). Checked by a timer every 30 minutes.

**Anti-annoyance:** After a reminder fires, all triggers are suppressed until the worker responds (the `reminder_pending` flag). This prevents reminder spam.

**State persistence:** Per-worker counters (`response_count`, `last_reminder_ts`, `last_response_ts`, `reminder_pending`) are persisted to `NODE_DIR/learning_reminders.json`. Bridge restarts don't reset progress.

**Lifecycle hooks:**
- `_reset_learning_reminder(name)` — called on hire and restart. Zeroes counters.
- `_check_learning_reminder(name)` — called on every worker response. Increments counter, fires if threshold met.
- `_scan_idle_workers()` — timer-based scan every 30 minutes for idle timeout.
- `_seed_learning_reminder_state(names)` — called at startup to initialize any workers not already tracked.

**Reminder text:** Read from `TEAM_DIR/learning-reminder.txt` if it exists (supports `{name}` substitution). Falls back to a hardcoded default covering what to capture, format ("When X, do Y, because Z"), where to write, and a 20-rule cap.

## SPEC-030. Security Model. [GOAL-024, GOAL-027]

The bridge is an internal coordination tool, not a public API. Security matches the actual threat model: single manager, same-user workers, private network.

### Architecture

```
Poll mode (default):    Bridge → api.telegram.org/getUpdates
                        No inbound surface. Nothing listens publicly.

Tunnel mode (opt-in):   Telegram → Cloudflare tunnel → 127.0.0.1:8271
                        Only POST / (webhook) reaches the bridge.
```

### Trust Boundaries

| Boundary | Mechanism | Status |
|----------|-----------|--------|
| **Network** | `BRIDGE_BIND=127.0.0.1` (default) | ✅ Active |
| **Inbound** | Poll mode default — no public listener | ✅ Active (v0.50.0) |
| **Telegram identity** | `admin_chat_id` allowlist | ✅ Active |
| **Webhook origin** | `TELEGRAM_WEBHOOK_SECRET` header check | ✅ Available, optional |
| **Guest/Relay tokens** | `secrets.token_urlsafe(32)` + SHA-256 | ✅ Active |
| **Worker source** | `/outputs` validates `source` against registered workers | ✅ Active |

### What's Protected and How

| Endpoint | Protection | Why sufficient |
|----------|-----------|----------------|
| Telegram commands | `admin_chat_id` check — unknown chat_ids silently rejected | Only manager controls workers |
| `POST /outputs` | Localhost-only + source consistency (must be registered worker) | Workers are our own Claude Code instances |
| `POST /messages` | Localhost-only + `from` field verified | Same-user, same-machine |
| `GET /workers`, `/checkin` | Localhost-only, read-only | No mutation, no sensitive data |
| `GET /health/*` | Unauthenticated, read-only | Monitoring probes |
| Webhook (`POST /`) | `TELEGRAM_WEBHOOK_SECRET` when tunnel active | Prevents fake updates via tunnel URL |
| Guest sessions | Token + SHA-256 hash | Temporary, scoped, revocable |

### Explicit Non-Goals

- **HMAC/mTLS on localhost endpoints**: Workers run as the same OS user. If a process can read one secret, it can read all. Token ceremony adds complexity without isolation.
- **Per-role auth tokens**: Same reasoning — no OS-level boundary between "worker token" and "admin token".
- **Webhook proxy on separate port**: The bridge already rejects non-webhook paths with 404. Proxy adds a process for the same result.
- **TLS termination**: Handled by reverse proxy or Cloudflare tunnel if needed.

### Remote Workers

Remote workers (Mac Mini) access the bridge via SSH tunnel:
```
ssh vps -L 127.0.0.1:8271:127.0.0.1:8271   # port forward
curl http://127.0.0.1:8271/outputs           # hits bridge via SSH
```
No Tailscale port exposure. SSH keys provide the authentication layer.

## SPEC-031. Structured Logging and Observability. [GOAL-007]

The bridge uses a unified logging and health system for operational visibility.

### Logging

All operational messages use `_log(level, component, message)` from `shared.py`:

```
[2026-10-17 04:30:15 INFO:bridge] Multi-Session Bridge on 127.0.0.1:8271
[2026-10-17 04:30:16 WARN:gmail] gws error: token expired
[2026-10-17 04:30:17 ERROR:github] Error processing comment 12345: KeyError
```

**Levels:** ERROR (0) → WARN (1) → INFO (2) → DEBUG (3). Default minimum: INFO. Override: `BRIDGE_LOG_LEVEL=DEBUG`.

**Components:** `bridge`, `gmail`, `github`, `tunnel`, `pr-review`, `pr-comment`, `health-alert`, connector names.

**Rules:**
- All bridge operational output uses `_log()` — never bare `print()` (exception: PR review CLI tool stdout).
- `_log_suppress` context manager replaces `contextlib.suppress` for error paths that must not crash but should be visible.
- `_LOG_DEBUG` level for expected/routine failures (fd cleanup, optional probes). `_LOG_WARN` for unexpected but recoverable. `_LOG_ERROR` for failures requiring attention.

### Log Rotation

| Layer | Trigger | Retention | Mechanism |
|-------|---------|-----------|-----------|
| `bridge.sh` startup | Bridge start | 10K lines | Truncate, save old as `.1` |
| `bridge-watchdog.sh` | Every 2min (cron) | 5MB | copytruncate (safe for `tee -a`) |
| `watchdog.log` | Every 2min | 500 lines | tail + mv |
| Hook debug logs | Each hook invocation | 100KB | tail + mv |

### Health Signals

`BridgeHealth` tracks 7 signals and 5 threads:

| Signal | Source | OK trigger | Error trigger |
|--------|--------|------------|---------------|
| `poll` | TunnelManager._poll_loop | Successful getUpdates | Poll error streak |
| `dispatch` | route_message | Message delivered | Send failed |
| `hook` | _flush_hook_response | Response forwarded to Telegram | Send failed |
| `telegram_api` | Rich text / HTML send | Telegram API 200 | API error |
| `watchdog` | watchdog_loop | End of each iteration | — |
| `gmail` | GmailConnector.poll_once | Successful poll | Poll error |
| `github` | GitHubConnector.poll_once | Successful poll | Poll error |

| Thread | Source |
|--------|--------|
| `worker-watchdog` | main() |
| `tunnel-watchdog` | TunnelManager.start() |
| `tunnel-poll` | TunnelManager._start_poll() |
| `gmail-poller` | start_connectors() |
| `github-poller` | start_connectors() |

### Health Endpoint

`GET /health` returns a unified JSON snapshot:

```json
{
  "ok": true,
  "version": "0.52.11",
  "uptime_s": 3600,
  "signals": {"poll": {"ok": true, "last_ok_ago_s": 5.2, "errors": 0}, ...},
  "threads": {"worker-watchdog": true, ...},
  "pools": {"message_pool": {"max": 8, "pending": 0}, ...}
}
```

HTTP 200 when `ok=true`, 503 otherwise. Used by `bridge-watchdog.sh` (cron) and can be used by external monitors.

### Health Alerting

Alerts route through the relay channel system (`bridge-health` channel):

- **Internal:** `_health_alert_check()` runs every watchdog cycle (~4s). Checks signal staleness, thread liveness, pool saturation.
- **External:** `bridge-watchdog.sh` runs via cron every 2min. Hits `/health`, alerts via direct Telegram API (bypasses bridge).
- **Delivery:** Alerts post to the `bridge-health` relay channel (persistent history) and deliver directly to worker tmux sessions listed in `BRIDGE_ALERT_WORKERS`.
- **Cooldown:** 300s between repeated alerts for the same issue. Recovery notifications sent when issues clear.

---

## SPEC-032. Spawn-on-Demand Tools. [GOAL-004, GOAL-005, GOAL-006, GOAL-022, GOAL-024]

Tools (rewind, pilot, PR review) are standalone Bun/TypeScript processes spawned by the bridge on demand.

### Lifecycle

1. User sends command (`/rewind`, `/pilot`, `/pr`)
2. Bridge generates content (transcript data, session info, PR data)
3. `ToolManager.spawn(kind, name, ttl, args)` allocates a port (10171–10179), generates a per-spawn token (reusable within that spawn's lifetime), spawns `bun <tool>/src/<entry>.ts --port P --token T --ttl S ...`
4. Bridge replies with URL: `http://<host>:<port>/?token=T`
5. Tool serves requests, resets idle timer on each hit
6. After TTL of inactivity → `process.exit(0)` (self-destruct)

### Invariants

- **Bridge = control plane only.** Bridge spawns, tracks, and cleans up tool processes. Bridge does NOT serve tool web pages. [GOAL-004]
- **Full functionality.** Each tool is fully functional including API calls (e.g., PR review comment → Telegram relay). Not just a viewer. [GOAL-005]
- **Bun/TypeScript runtime.** All tools run under Bun. Not Python, not Node.js. [GOAL-006]
- **Token isolation.** Tool tokens are per-spawn and reusable within that spawn's lifetime (not single-use). Bridge API secret is never passed to tools. [GOAL-024]
- **Port pool.** 10171–10179 (9 ports). Exhaustion returns an error to the user.
- **Cleanup.** `ToolManager.cleanup()` runs from the watchdog, reaps exited processes.

### Per-Tool Stress Testing [GOAL-022]

Each tool must be stress-tested for its realistic operations:
- Rewind: search performance on large transcripts
- Pilot: terminal input/output under load
- PR Review: comment relay and large diff rendering

---

## SPEC-033. Worker State and Migration Consistency. [GOAL-009, GOAL-010, GOAL-011, GOAL-012, GOAL-020]

Worker state must be consistent across restart, teleport, and teleback.

### Minimal Persistent State

The bridge persists only what is essential for recovery:
- `session_id` — Claude Code session identifier (authoritative from JSONL scan)
- `working_dir` — the project directory the worker is assigned to (≠ OS CWD)
- `host` — which machine the worker runs on (absent = local to this bridge)

### State Invariants

- **Working directory ≠ CWD.** Two distinct values: `working_dir` is the assigned project directory (persisted in registry, used for checkin/restart/migration). Process CWD is the OS-level `pwd` (observed live, not persisted). The SessionStart hook guides workers to checkin with their `working_dir`. Bridge authoritative state uses `working_dir` for all lifecycle operations. [GOAL-009]
- **Default host = bridge host.** When `host` field is absent in the registry, the worker is local. On local hire after teleport, stale remote host must be cleared. [GOAL-011]
- **7-second restart stagger.** `/restart all` waits 7 seconds between each worker. Manager specified the delay; the `.claude.json` auth file race rationale is agent interpretation [impl], not explicit in MSG-001. [GOAL-012]
- **Teleport syncs sessions.** Teleport/teleback already syncs Claude Code sessions — no redundant fields. [GOAL-010]
- **Bridge owner knows state.** The bridge must know worker state (session_id, working_dir, host) authoritatively without asking the manager. [GOAL-020]
- **JSONL selection rule.** When multiple transcripts exist under `~/.claude/projects/<slug>/`, the bridge selects the newest by mtime (`_scan_latest_session_id`). This is authoritative — cache is optimization, not truth.

---

For version history, see [CHANGELOG.md](./CHANGELOG.md).
