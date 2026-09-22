# Design Philosophy

> Version: 0.44.7

## Documentation Contract

This document is the **spec** — the source of truth for system design and architecture.
Authority flows downstream. Citations point upstream only.

```
SPEC.md (spec)  →  AGENTS.md (rules & workflow)  →  Code  →  Tests
   ↑                    ↑                        ↑         ↑
 manager             agent                    agent     agent
 (owner)            (owner)                  (owner)   (owner)

Authority flows →  (downstream)
Citations point ←  (upstream only)
```

**Rules:**
- Citations point upstream only. AGENTS.md cites SPEC.md spec IDs (`[SPEC-NNN]`). Code cites spec IDs in comments. SPEC.md never references AGENTS.md.
- Manager owns SPEC.md. Agent owns AGENTS.md + code + tests.
- When a spec changes, everything downstream is invalidated and must be updated (cascade forward).
- When code diverges from spec, agent STOPS and notifies manager — never patch downstream and hope upstream catches up.

**Citation format:**
- AGENTS.md cites: `[SPEC-NNN]`
- Code cites: `# SPEC-NNN` (in comments on key decisions)
- Tests cite: test function names trace to spec behavior

**Contract invariants:**
- Every `SPEC-NNN` in the Spec Index has exactly one detailed section header below.
- Every detailed `SPEC-NNN` section appears in the Spec Index.
- Every AGENTS.md Quick Reference row cites at least one `SPEC-NNN`.

**Feedback intake:** Every input that changes the spec gets an ID at intake:
- Manager feedback → `FB-{NNN}` (e.g., FB-001 = initial design session)
- Audit findings → `AUDIT-{NNN}` (e.g., AUDIT-001 = security review)
- Requirements → `REQ-{NNN}`

---

## Spec Index

Spec IDs are stable references and may not appear in numeric order in the document.

| ID | Title | Status | Citations | Commit |
|----|-------|--------|-----------|--------|
| SPEC-001 | tmux IS persistence | Active | FB-001 | 8d6a973 |
| SPEC-002 | `claude-<name>` naming | Active | FB-001 | 8d6a973 |
| SPEC-003 | RAM state only | Active | FB-001 | 8d6a973 |
| SPEC-004 | Per-session files | Active | FB-001 | 8d6a973 |
| SPEC-005 | Fail loudly | Active | FB-001 | 8d6a973 |
| SPEC-006 | Token isolation | Active | FB-001, AUDIT-001 | 8d6a973 |
| SPEC-007 | Admin config | Active | FB-001 | 8d6a973 |
| SPEC-008 | Secure by default | Active | FB-001, AUDIT-001 | 8d6a973 |
| SPEC-009 | Decentralized worker comms | Active | FB-002 | 8d6a973 |
| SPEC-010 | Machines are config | Active | FB-003 | 8d6a973 |
| SPEC-011 | SOLID in a single file | Active | FB-004 | 8d6a973 |
| SPEC-012 | Message routing | Active | FB-001 | 8d6a973 |
| SPEC-013 | Feedback philosophy (clean chat) | Active | FB-001 | 8d6a973 |
| SPEC-014 | Worker registration (3 paths) | Active | FB-001 | 8d6a973 |
| SPEC-015 | No magic routing | Active | FB-001 | 8d6a973 |
| SPEC-016 | Hook: minimal and defensive | Active | FB-001 | 8d6a973 |
| SPEC-017 | Single chat UX | Active | FB-001 | 8d6a973 |
| SPEC-018 | Bridge architecture (OOP) | Active | FB-004 | 8d6a973 |
| SPEC-019 | Machine catalog | Active | FB-003 | 8d6a973 |
| SPEC-020 | Connector sender allowlists | Active | AUDIT-001 | 8d6a973 |
| SPEC-021 | Behavior tests required | Active | FB-001 | 2b6cd8f |
| SPEC-022 | Configurable paths (no hardcoding) | Active | AUDIT-002 | 2b6cd8f |
| SPEC-023 | Node isolation | Active | FB-003 | 2b6cd8f |
| SPEC-024 | Process lifecycle safety (PID-based) | Active | AUDIT-002 | 2b6cd8f |
| SPEC-025 | Dev-before-prod deployment | Active | AUDIT-002 | 2b6cd8f |
| SPEC-026 | Learning reminders (periodic self-reflection) | Active | — | — |

## Superseded Specs

| ID | Original | Superseded by | Why |
|----|----------|---------------|-----|
| *(none yet)* | | | |

## Feedback Log

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

## SPEC-001. tmux IS the Persistence. [FB-001]

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

1. **Gateway crashes? No problem.** Restart it. Scan tmux. Continue.
2. **No sync issues.** There is no stored state, so there is no stale state.
3. **Manual tmux usage works.** Start `claude` in any `claude-*` session. The bridge finds it.
4. **Debugging is trivial.** Run `tmux list-sessions` to see what exists.

## SPEC-002. Naming Convention: `claude-<name>`. [FB-001]

The user says `/hire backend`. The bridge creates tmux session `claude-backend`.

This prefix pattern gives you:
- **Auto-discovery**: `tmux list-sessions | grep ^claude-` finds all managed sessions
- **Namespace isolation**: No conflict with other tmux sessions
- **Clear ownership**: You can see which sessions the bridge manages

## SPEC-003. RAM State: Ephemeral by Design. [FB-001]

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

## SPEC-004. Per-Session Files: Minimal Coordination. [FB-001]

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

## SPEC-012. Message Routing: Simple Rules. [FB-001]

```
Input                    → Routes to
─────────────────────────────────────
/hire backend            → creates claude-backend, sets active
/focus frontend          → sets active = frontend
@backend do something    → claude-backend (one-off, focus unchanged)
fix the bug              → active session (currently frontend)
```

`@name` mentions route messages without changing focus. Use `/focus <name>` to switch.

## SPEC-013. Feedback Philosophy (Clean Chat). [FB-001]

- 👀 means the message reached the worker.
- The worker reply is the confirmation: `worker_name: response`.
- The bridge sends text replies only for errors and state commands (`/hire`, `/end`, `/focus`, `/team`, `/progress`).
- Regular messages and `@mentions` are silent.
- The manager wants a clean chat. The emoji gives instant feedback.
- The bridge speaks only when no worker reply will come.

## SPEC-014. Worker Registration (3 Paths). [FB-001]

Workers register through three paths:

1. **`/hire` command** — the bridge creates a tmux session and registers the worker.
2. **Auto-discovery** — the bridge scans for tmux sessions that match the `TMUX_PREFIX` pattern on startup.
3. **`POST /register`** — external workers (forge, callback-based) register through the HTTP endpoint.

The persistent worker registry (`workers.json`) survives bridge restarts. The bridge rebuilds runtime state from tmux sessions and the registry on startup.

## SPEC-015. No Summaries, No Magic. [FB-001]

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

## SPEC-005. Error Handling: Fail Loudly, Recover Gracefully. [FB-001]

- Session does not exist? Tell the user immediately.
- tmux died? The next message reports it.
- Bridge restarted? Scan tmux and continue.

No silent failures. No retry loops that hide problems.

## SPEC-016. The Hook: Minimal and Defensive. [FB-001]

The hook (`send-to-telegram.sh`) runs on every Claude stop event. It reads configuration from the tmux session environment:

- `BRIDGE_URL` or `PORT` — where to send the response
- `TMUX_PREFIX` — to derive the worker name
- `SESSIONS_DIR` — to find session files

The hook POSTs extracted text to `{BRIDGE_URL}/response`. It also writes `claude_session_id` and `claude_session_cwd` to the session directory. If the JSONL transcript is stale, it sends an alert to `/health-alert`.

```bash
# Key guard — exit if no chat_id (bridge never set one for this session):
[ ! -f "$CHAT_ID_FILE" ] && exit
```

The `pending` file is used for the busy indicator in `/team` and `/progress`. It is not a send gate (changed in v0.6.2 to enable proactive messaging).

## SPEC-017. Why Single Chat? [FB-001]

One Telegram DM manages all Claude instances because:
1. **Context stays in one place.** Scroll up to see what you asked any Claude.
2. **No channel or group management.** Just DM the bot.
3. **Mobile-friendly.** One conversation with explicit routing via commands.

The `@name` syntax and `/focus` command give full control without the overhead of multiple chats.

## SPEC-018. Bridge Architecture. [FB-004]

The bridge uses small, explicit classes:

- **Backend Protocol** (`typing.Protocol`): `Backend` interface with `name`, `binary`, `is_interactive`, `start_cmd(resume_id="")`, `send()`, `is_online()`.
- **Backend implementations**: `ClaudeBackend` (interactive), `CodexBackend` (non-interactive). All live in `bridge.py`.
- **WorkerManager**: Worker lifecycle and routing (`hire`, `end`, `send`, `is_online`, `get_workers`, `scan_tmux_sessions`).
- **TelegramAPI**: Wraps all Telegram API calls (sendMessage, sendPhoto, sendDocument, etc.).
- **CommandRouter**: All `/command` handlers and message routing. Delegates to `WorkerManager` and `TelegramAPI`.

The bridge detects interactive vs non-interactive mode from `backend.is_interactive`. It does not hardcode backend names.

## SPEC-010. Machines Are Config. [FB-003]

Static host topology lives in `machines.json`. Machines are infrastructure, not runtime state. The bridge reads the machine catalog on startup and serves it via `/machines`. Worker placement is derived from tmux sessions at runtime — it is never stored in the machine config.

Adding a machine: add an entry to `machines.json` with `ssh_target`, `bridge_base_url`, `home_root`, `os_family`. The bridge discovers workers on all configured machines.

## SPEC-011. SOLID in a Single File. [FB-004]

All bridge logic lives in `bridge.py`. The architecture uses SOLID principles within a single file:

- **AppContext** for dependency injection (subprocess, clock, urlopen)
- **TypedDicts** and **NamedTuples** for data structures (31 TypedDicts, 5 NamedTuples)
- **Service classes** for responsibilities (WorkerManager, TelegramAPI, CommandRouter)
- **Protocols** for interfaces (Backend, SubprocessRunner, Clock)
- **Registry dispatch** for extensible command routing

CommandRouter logic is inlined (mixins were removed during consolidation). Connector implementations live in `connectors/`, not in bridge.py.

This is a deliberate choice: one file for core logic keeps the system greppable, diffable, and simple to deploy. See the changelog for the quality push history (v0.33.0 through v0.44.0).

## SPEC-019. Machine Catalog. [FB-003]

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

## SPEC-009. Inter-Worker Messaging (Decentralized Discovery). [FB-002]

**Status:** Available (tmux send-keys + named pipes)

**Design:** The bridge provides discovery only. Workers communicate directly with each other. Manager tools may use `POST /send` to reach workers. The bridge does not route worker-to-worker messages.

This means:
- **No manager visibility:** Private worker-to-worker conversations stay private.
- **Direct P2P communication:** Workers talk to each other without the bridge.
- **Protocol flexibility:** Each worker advertises how to reach it (tmux, pipe, etc.).

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

## SPEC-021. Behavior Tests Required. [FB-001]

Every new feature must have an end-to-end behavior test. Tests verify what users care about, not that code structure exists. A test that checks "HTTP returns 200" without verifying delivery is scaffolding, not a behavior test.

**What qualifies as a behavior test:**
- Worker stays alive after hire (not just "hire returned OK")
- Message reaches worker end-to-end (not just "send function exists")
- Pipe delivery works between workers (not just "pipe was created")

See AGENTS.md `[SPEC-021]` for the full testing workflow (TDD, mode gates, test/scaffolding examples).

## SPEC-022. Configurable Paths (No Hardcoding). [AUDIT-002]

All paths must be configurable through environment variables with sensible defaults. Hardcoded paths break test isolation and multi-node setups.

```bash
# Right: configurable with default
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/telegram/sessions}"

# Wrong: hardcoded
SESSIONS_DIR="$HOME/.claude/telegram/sessions"
```

Env vars must propagate through the full process chain. Use `tmux set-environment` at each boundary, not `tmux send-keys "export ..."`.

## SPEC-023. Node Isolation. [FB-003]

All runtime paths are namespaced by node (derived from `TMUX_PREFIX`). No shared state between prod, dev, and test nodes.

```
/tmp/claudecode-telegram/<node>/<worker>/in.pipe
/tmp/claudecode-telegram/<node>/<worker>/inbox/
/tmp/claudecode-telegram/<node>/locks/<session>.lock
```

This prevents collisions when multiple nodes run simultaneously on the same machine.

## SPEC-024. Process Lifecycle Safety (PID-Based). [AUDIT-002]

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

## SPEC-025. Dev-Before-Prod Deployment. [AUDIT-002]

Always test on the dev node before deploying to prod:

1. Start dev bridge with dev bot token on port 8272
2. Run full integration tests against dev
3. Test manually through Telegram on the dev bot
4. Only then deploy to prod

Local/unit tests prove concepts in isolation. Real integration bugs only surface with actual Telegram traffic.

---

## SPEC-006. Token Isolation. [FB-001, AUDIT-001]

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
│                    │ POST localhost:{PORT}/response     │
│                    ▼                                    │
│              Bridge ──► Telegram API         ← SAFE    │
└─────────────────────────────────────────────────────────┘
```

PORT varies by node: prod=8271, dev=8272, test=8295, default=8270.

## SPEC-007. Admin Configuration. [FB-001]

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

## SPEC-008. Secure by Default. [FB-001, AUDIT-001]

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

## SPEC-020. Connector Sender Allowlists. [AUDIT-001]

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

## SPEC-026. Learning Reminders (Periodic Self-Reflection).

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

---

For version history, see [CHANGELOG.md](./CHANGELOG.md).
