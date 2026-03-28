# Design Philosophy

> Version: 0.28.2

## Current Philosophy (Summary)

| Principle | Description |
|-----------|-------------|
| **tmux IS persistence** | No database, no state.json - tmux sessions are the source of truth |
| **`claude-<name>` naming** | Configurable prefix via `TMUX_PREFIX` (default: `claude-`) |
| **RAM state only** | Derived on demand from tmux, never persisted |
| **Per-session files** | Minimal hook↔gateway coordination via filesystem |
| **Fail loudly** | No silent errors, no hidden retries |
| **Token isolation** | `TELEGRAM_BOT_TOKEN` never leaves bridge process |
| **Admin config** | Pre-set via `ADMIN_CHAT_ID` or auto-learn first user |
| **Secure by default** | 0o700 dirs, 0o600 files, silent rejection of non-admins |
| **Decentralized worker comms** | Bridge provides discovery only; workers communicate directly via protocol |

---

## Core Principle: tmux IS the Persistence

The most important design decision: **no database, no state files, no JSON persistence**. tmux sessions ARE the source of truth.

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

1. **Gateway crashes? No problem.** Restart it, scan tmux, continue working.
2. **No sync issues.** Can't have stale state if there's no stored state.
3. **Manual tmux usage works.** Start `claude` in any `claude-*` session, gateway finds it.
4. **Debugging is trivial.** `tmux list-sessions` shows exactly what exists.

## Naming Convention: `claude-<name>`

User says `/hire backend` → tmux session `claude-backend`

This prefix pattern enables:
- **Auto-discovery**: `tmux list-sessions | grep ^claude-` finds all managed sessions
- **Namespace isolation**: Won't conflict with user's other tmux sessions
- **Clear ownership**: Obvious which sessions belong to the bridge

## RAM State: Ephemeral by Design

```python
state = {
    "active": "backend",      # Which session receives bare messages
    "pending_registration": None
}
```

This state is:
- Derived on demand from tmux
- Never persisted to disk
- Authoritative only for "active" selection (user preference)

## Per-Session Files: Minimal Coordination

```
~/.claude/telegram/sessions/
├── backend/
│   ├── pending      # Timestamp when request started
│   └── chat_id      # Where to send the response
└── frontend/
    ├── pending
    └── chat_id
```

Why files instead of IPC?
- **Hook runs in Claude's process**, not the gateway's
- Files are the simplest cross-process communication
- Hook just needs: "where do I send this?" and "should I send at all?"

## Message Routing: Simple Rules

```
Input                    → Routes to
─────────────────────────────────────
/hire backend            → creates claude-backend, sets active
/focus frontend          → sets active = frontend
@backend do something    → claude-backend (one-off, focus unchanged)
fix the bug              → active session (currently frontend)
```

`@name` mentions route messages without changing focus. Use `/focus <name>` to switch.

## Feedback Philosophy

- 👀 means the message hit the worker.
- The worker reply is the confirmation: `worker_name: response`.
- Text replies only for errors and state/info commands (`/hire`, `/end`, `/focus`, `/team`, `/progress`).
- Regular messages and `@mentions` are silent.
- Managers want clean chat; the emoji is instant feedback.
- If no worker reply is coming, then we speak.

## Registration Flow: Adopt Existing Sessions

What if someone manually started `tmux new -s claude` and ran `claude`?

```
User: hello
Bot: Unregistered session detected: claude
     Register with: {"name": "your-session-name"}

User: {"name": "myproject"}
Bot: ✓ Registered "myproject" (now active)
     [tmux session renamed: claude → claude-myproject]
```

This makes the bridge non-destructive. It adopts existing work rather than requiring users to start fresh.

## No Summaries, No Magic

The `/team` command shows sessions and their pending status. That's it.

```
  backend ← active
  frontend (busy)
  api
```

We deliberately avoid:
- AI-generated summaries of what each Claude is doing
- Automatic context sharing between sessions
- "Smart" routing based on message content

Why? Because:
1. Each Claude session has its own context and project
2. The user knows which session should handle what
3. Magic routing would be wrong often enough to be frustrating

## Error Handling: Fail Loudly, Recover Gracefully

- Session doesn't exist? Tell the user immediately.
- tmux died? Next message will report it.
- Gateway restarted? Scan and continue.

No silent failures. No retry loops that hide problems.

## The Hook: Minimal and Defensive

```bash
# Only respond if:
[ ! -f "$PENDING_FILE" ] && exit 0           # 1. We're expecting a response
[ $((NOW - PENDING_TIME)) -gt 600 ] && exit  # 2. Request isn't stale (10min)
[ ! -f "$CHAT_ID_FILE" ] && exit             # 3. We know where to send
```

The hook runs on EVERY Claude stop event. Most of the time it should do nothing. The checks ensure it only acts when the gateway initiated the request.

## Why Single Chat?

One Telegram DM manages all Claude instances because:
1. **Context stays in one place.** Scroll up to see what you asked any Claude.
2. **No channel/group management.** Just DM the bot.
3. **Mobile-friendly.** One conversation, explicit routing via commands.

The `@name` syntax and `/focus` command give you full control without the overhead of multiple chats.

## Bridge Architecture (v0.19.0)

The bridge is now organized around small, explicit classes:

- **Backend Protocol** (`typing.Protocol`): `Backend` interface with `name`, `is_interactive`, `start_cmd()` (supports optional `append_system_prompt`), `send()`, `is_online()`.
- **Backend implementations**: `ClaudeBackend` (interactive), `CodexBackend`, `GeminiBackend`, `OpenCodeBackend` (non-interactive) — all in `bridge.py`.
- **WorkerManager**: worker lifecycle + routing (`hire`, `end`, `send`, `is_online`, `get_workers`, `scan_tmux_sessions`).
- **TelegramAPI**: wraps all Telegram API calls (sendMessage/sendPhoto/sendDocument/etc.).
- **CommandRouter**: all `/command` handlers and message routing; delegates to `WorkerManager` + `TelegramAPI`.

Interactive vs non-interactive detection is backend-driven (`backend.is_interactive`), not hardcoded to a specific backend.

## Inter-Worker Messaging (Decentralized Discovery)

**Status:** Available (tmux send-keys + named pipes)

**Design philosophy:** Bridge provides discovery only; workers communicate directly using the provided protocol. The bridge does NOT route messages between workers - it only tells workers how to reach each other. This means:
- **No manager visibility:** Private worker-to-worker conversations stay private
- **Direct P2P communication:** Workers talk to each other without bridge involvement
- **Protocol flexibility:** Each worker advertises how to reach it (tmux, pipe, etc.)

**Current state:**
- **tmux backends:** Workers use `echo 'message' | tmux load-buffer - && tmux paste-buffer -r -t claude-<node>-<worker> && tmux send-keys -t claude-<node>-<worker> Enter`
- **All backends:** Each worker gets a named pipe at `/tmp/claudecode-telegram/<node>/<worker>/in.pipe`
  - Node name derived from `TMUX_PREFIX` (`claude-test-` → `test`, `claude-` → `default`)

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
      "send_example": "echo 'your message here' | tmux load-buffer - && tmux paste-buffer -r -t claude-prod-alice && tmux send-keys -t claude-prod-alice Enter"
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
| `tmux` | Session name | `echo "msg" \| tmux load-buffer - && tmux paste-buffer -r -t <address> && tmux send-keys -t <address> Enter` | tmux only |
| `pipe` | Named pipe path | `echo "message" > <address>` | All backends |

**Recommended: Named pipes as unified protocol**

Named pipes (FIFOs) work across all backends:
```bash
# Bridge creates on worker startup
mkfifo /tmp/claudecode-telegram/<node>/<worker>/in.pipe

# Worker A sends to Worker B
echo "Hey bob, can you review PR #42?" > /tmp/claudecode-telegram/<node>/bob/in.pipe

# Worker B reads (poll or inotifywait)
cat /tmp/claudecode-telegram/<node>/<worker>/in.pipe
```

**Why this design:**
- Workers collaborate without manager overhead
- Standard Unix mechanism, no custom protocol
- Works consistently across tmux and exec backends
- Bridge stays simple - just discovery, no message routing

**Tests:**
- `test_worker_pipe_creation_on_startup` - pipes created on worker startup
- `test_worker_to_worker_pipe` - end-to-end worker communication via pipe

---

## Security Model (v0.3.0+)

### Token Isolation

The most important security principle: **Claude never sees the bot token.**

Claude is a powerful agent that could inadvertently leak tokens via:
- Tool use (e.g., `curl` commands in responses)
- Log files
- Error messages
- Responses to the user

The bridge-centric architecture ensures this can't happen:

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
│                    │ POST localhost:8270/response       │
│                    ▼                                    │
│              Bridge ──► Telegram API         ← SAFE    │
└─────────────────────────────────────────────────────────┘
```

### Admin Configuration

Two modes:

**1. Pre-configured (recommended for production):**
```bash
ADMIN_CHAT_ID=121604706  # Lock to specific user
```

**2. Auto-learn (default):**
```python
admin_chat_id = None  # RAM only, first user becomes admin
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
1. **Pre-configured** - Secure, no race condition on first message
2. **Auto-learn** - Zero configuration for quick setup
3. **RAM-only** - Restart to reset admin (feature, not bug)

### Optional Webhook Verification

If `TELEGRAM_WEBHOOK_SECRET` is set:
1. Gateway adds `secret_token` to webhook registration
2. Telegram sends `X-Telegram-Bot-Api-Secret-Token` header
3. Bridge verifies header matches, rejects mismatches

If not set, works like before (simpler setup, still localhost-only for hook).

### File Permissions

All session files use restrictive permissions:
- Directories: `0o700` (owner only)
- Files: `0o600` (owner only)

This prevents other users on multi-user systems from reading chat IDs or session data.

---

## Changelog

### v0.28.2 - Fix BRIDGE_URL for teleported workers

**Bug fix:** `export_hook_env()` was exporting `BRIDGE_URL=http://localhost:8271` to remote workers — localhost doesn't resolve to VPS from Mac Mini. Now uses `BRIDGE_PUBLIC_URL` (Tailscale IP) for remote hosts, keeps localhost for local workers. Also fixed pre-existing test failure in `test_remote_dispatch_export_hook_env` (mock needed echo $HOME handling). FAST 236/1.

### v0.28.1 - Fix 11 teleport gaps (Codex-audited)

**Systematic fix of all remaining teleport-unaware code paths**, identified by Codex audit.

**CRASH fixes (5):**
- `/end` command: tmux kill-session now routes through `_remote_run(host=host)` for teleported workers
- `WorkerManager.restart()`: returns `(False, "use_remote_restart")` sentinel for teleported workers — callers route to `_restart_remote_worker()`
- Media inbox: `download_telegram_file()` now rsyncs files to remote inbox after local save
- `export_hook_env()`: remaps SESSIONS_DIR to remote `$HOME` prefix (e.g., `/Users/beastoinagents/...`)
- `_spawn_adapter()`: guards against teleported non-interactive workers (logs warning, returns False)

**WRONG fixes (6):**
- `is_online()`: for remote interactive workers, checks both `tmux_exists` AND `is_claude_running` (was tmux-only)
- `get_claude_session_id()`/`get_claude_session_cwd()`: reads from remote host via SSH for teleported workers
- `_check_hook_failure_signal()`/`_clear_hook_failures()`: reads/clears signal files on remote host
- `_localize_media()`: always fetches from remote for teleported workers (was skipping if local file existed)
- `/workers`: non-interactive remote workers show "not supported yet" instead of local FIFO paths
- `_check_adapter_log()`: reads adapter logs from remote host via `tail -n`

**11 new TDD tests**, Codex-designed test specifications, FAST 234/2 (1 pre-existing).

### v0.28.0 - Git-based teleport sync + checkin CWD fix

**New feature: Git-based teleport sync** replaces rsync for git repositories. VPS hosts bare repos at `~/git-server/<project>.git`, workers push WIP state (via `git stash create`) to per-worker branches, target fetches deltas. ~0-50s vs 600s+ for rsync over Tailscale on large repos.

**Architecture:**
- `_is_git_repo()` — detects git repos (local or remote via SSH)
- `_get_project_name()` — extracts project name from `remote.origin.url`
- `_ensure_bare_repo()` — creates `~/git-server/<project>.git` on VPS
- `_git_push_state()` — non-mutating snapshot: `git add -A` → `git stash create` → `git reset` → push to `refs/heads/teleport/<worker>`
- `_git_pull_state()` — clone/fetch from bare repo, apply stash, restore branch + staged files
- `_bare_repo_url()` — SSH URL for remote targets, direct path for local
- `_sync_working_directory()` — tries git first, falls back to rsync on failure or `full=True`

**Key design:** `git stash create` doesn't support `-u` for untracked files. Workaround: temporarily `git add -A`, create stash, then `git reset HEAD` + re-stage original files. Source tree is never mutated.

**Bug fix: Checkin CWD validation for teleported workers.** `/checkin?cwd=` rejected remote paths (e.g., `/Users/beastoinagents/omi/omi-ren`) because `validate_cwd()` checked the VPS filesystem. Now uses `get_worker_host()` to detect teleported workers and validates via SSH on the remote host.

**29 new tests** (222 total, 1 pre-existing failure):
- 6 helper tests (_is_git_repo, _get_project_name)
- 2 bare repo management tests
- 5 source push tests (clean, dirty, untracked, staged, no-mutation)
- 5 target pull tests (fresh clone, existing, branch, staged, unstaged)
- 4 edge case tests (detached HEAD, push/pull failures, non-git)
- 3 remote dispatch tests (SSH routing, bare repo URL)
- 3 integration tests (git path, full flag, fallback)
- 1 checkin CWD remote validation test

### v0.27.4 - Fix long text batch input (bracketed paste)

**Bug fixed:** Long text messages sent via bridge get pasted into Claude Code's input as `[Pasted text #N +M lines]` but the Enter key is swallowed — text sits in input without submitting. Caused by `paste-buffer -r` sending raw text without bracketed paste control codes. Claude Code's time-based paste detection treats the immediately-following Enter as part of the paste.

**Fix:**
- Add `-p` flag to `paste-buffer`: sends proper bracketed paste codes (`\e[200~`...`\e[201~`) so TUI apps know exactly where the paste ends.
- Add 50ms delay between paste and Enter for processing safety margin.
- Fix incorrect code comment that said `-r` controls bracketed paste (`-r` is LF→CR conversion; `-p` is bracketed paste).
- Update `/workers` `send_example` with matching `-p` + delay.

**Chaos test:** 0/5 without `-p`, 5/5 with `-p -r` against TUI simulator with bracketed paste mode. **2 new tests:** `test_paste_buffer_uses_bracketed_paste`, `test_long_text_enter_with_bracketed_paste`.

### v0.27.3 - Fix state reliability (is_pending + backend canonical)

**Bugs fixed:**
- **is_pending() side-effect**: `is_pending()` auto-deleted the pending file at 10min (`PENDING_TIMEOUT=600`), but watchdog STUCK triggers at 15min (`STALE_PENDING=900`). The 5-min gap silently suppressed STUCK detection. Fix: `is_pending()` is now non-mutating — only `clear_pending()` deletes the file.
- **Backend source drift**: `get_worker_backend()` checked session dict before backend file, allowing drift when registry/RAM state gets stale. Fix: backend file (`SESSIONS_DIR/<name>/backend`) is now the canonical source, session dict is fallback only.

**3 new tests:** `test_pending_no_side_effect`, `test_stale_pending_survives_for_watchdog`, `test_backend_file_is_canonical`.

### v0.27.2 - Fix tmux send interleaving (cross-process flock)

**Bug fixed:** When multiple processes send to the same tmux session concurrently (worker-to-worker or bridge+worker), the `load-buffer → paste-buffer → send-keys Enter` sequence interleaves. Text from different senders concatenates on one line, while orphan Enters submit empty input. 93% corruption rate in chaos testing.

**Fix:**
- **Two-layer locking in `tmux_send_message`**: existing Python `threading.Lock` (intra-process) + new `flock` on `/tmp/claudecode-telegram/<node>/locks/<session>.lock` (cross-process).
- **`/workers` `send_example`** now wraps tmux commands in `flock`, so inter-worker sends use the same lock file.
- Lock files are node-namespaced (multi-node isolation, matches pipe isolation pattern).

**5 new tests:** flock creation, send_example flock, concurrent send behavior (25/25 clean with 5 parallel senders), per-session isolation, node namespace.

### v0.27.1 - TDD workflow and TEST_FILTER

**New features:**
- **TEST_FILTER env var**: Run specific tests by substring match. `TEST_FILTER=test_registry FAST=1 ./test.sh` runs only matching tests. Enables fast Red-Green-Refactor inner loops.
- **CLAUDE.md TDD section**: Expanded with increment-based decomposition ladder (degenerate → happy path → variations → edges → errors → integration), per-increment RED/GREEN/REFACTOR steps, and mode gate guidelines.

**Architecture changes:**
- `should_run_test()` / `run_test()` helpers in test.sh — all test invocations wrapped through `run_test()`.
- `count_matching_tests()` pre-scans runner functions to show how many tests will execute when filter is active.
- `tests_run` counter tracks actual tests executed (vs total available).

### v0.27.0 - Worker CWD shift via checkin API

**New features:**
- **Checkin CWD**: Workers can shift their working directory via `curl checkin?name=X&cwd=/path`. Bridge validates the path, stores it in RAM, and restarts the worker in the new directory if it differs from the current tmux pane CWD.
- **RAM-only CWD storage**: `_worker_cwds` dict (not persisted to workers.json). Cleared on `/end`.

**Architecture changes:**
- `validate_cwd()` / `normalize_cwd()` — path validation and normalization.
- `_get_startup_cwd()` — resolves CWD priority (explicit > RAM > fallback).
- `_get_tmux_pane_cwd()` — reads current tmux pane working directory.
- `_cd_tmux_to_cwd()` — sends cd command to tmux session.
- `hire()`, `restart()`, `_restart_dead_worker()` all respect stored CWD.

### v0.26.0 - Persistent worker registry

**Breaking changes:** None.

**New features:**
- **Persistent worker registry (`workers.json`)**: Bridge remembers all hired workers in `NODE_DIR/workers.json`. When a worker's tmux session dies (context limit, crash, idle timeout), the bridge still knows about it and can recover it.
- **Dead worker recovery**: `/restart <name>` now works for workers whose tmux session has died. Re-creates the tmux session, exports hook env, starts the backend, and sends a welcome message. Supports both `resume` and `relaunch` modes.
- **EXITED watchdog state**: New watchdog state for workers that are in the registry but have no tmux session. Triggers an alert with a suggested `/restart <name>` action.
- **`/team` shows exited workers**: Workers whose tmux sessions have died appear as "exited" in `/team` output instead of silently disappearing.
- **`/progress` shows exited state**: Reports "Session exited. Use /restart to bring back." for dead workers.
- **`/workers` includes exited workers**: Returns exited workers with `status: "exited"` and `protocol: "none"` (interactive) or `protocol: "pipe"` (non-interactive).
- **First-run bootstrap**: If `workers.json` doesn't exist, it's auto-created from current tmux sessions so existing deployments don't lose track of workers.

**Architecture changes:**
- Registry file: `NODE_DIR/workers.json` — atomic write via tmpfile + `os.replace()`, chmod 0o600.
- Registry CRUD: `_load_registry()`, `_save_registry()`, `_registry_add()`, `_registry_remove()`, `_registry_bootstrap()`.
- Corrupt file recovery: renames to `.corrupt.<timestamp>`, logs warning, returns empty dict.
- Thread safety: all registry operations guarded by existing `_watchdog_lock`.
- `hire()` writes to registry, `end()` removes, `get_registered_sessions()` merges.
- `_restart_dead_worker()`: full dead worker recovery path (tmux create, env export, backend start, welcome).
- 8 new tests: registry CRUD, bootstrap, corrupt recovery, registered sessions merge, dead worker restart, end cleanup, team display, watchdog EXITED state.

### v0.25.0 - SessionStart hook for auto-checkin + checkin notes

**Breaking changes:** None.

**New features:**
- **SessionStart hook (`checkin-on-start.sh`)**: Auto-runs `/checkin` after `/compact` or `/resume`, re-injecting bridge instructions (file sending, `/workers` discovery, name prefix rule) into Claude's context. Workers no longer lose instructions after compaction.
- **Matcher-based firing**: Hook uses `compact|resume` matcher — fires on compaction and session resume, but NOT on fresh startup (which already gets the welcome message from `/hire`).
- **`hook install` now installs both hooks**: Stop hook (response forwarding) and SessionStart hook (auto-checkin) are installed together.
- **Checkin notes (file-based)**: Bridge reads `TEAM_DIR/checkin-note.txt` (`TEAM_DIR` env var, default `~/team`) and appends it to all `/checkin` responses AND `/hire` welcome messages. Supports `{name}` placeholder for per-worker substitution. The file IS the API — edit it directly, no HTTP endpoints needed.

**Architecture changes:**
- New `hooks/checkin-on-start.sh` — reuses the tmux env resolution pattern from `send-to-telegram.sh` (reads `BRIDGE_URL`, `TMUX_PREFIX`, `PORT` from tmux session env).
- `cmd_hook_install` / `cmd_hook_uninstall` updated to manage both Stop and SessionStart hooks in `settings.json`.
- Auto-install on `run` now checks for both hook files.
- `TEAM_DIR` env var (default `~/team`) — shared team knowledge base directory. Checkin note read from `TEAM_DIR/checkin-note.txt`.
- `read_checkin_note()` reads from file on every call — no in-memory cache, single source of truth.

### v0.24.0 - MCP tool inventory injection

**Breaking changes:** None.

**New features:**
- **MCP tool inventory injection**: Claude workers now inject a sanitized MCP tool inventory into the system prompt on every start/resume, so resumed sessions immediately know available tools.
- **Configurable inventory**: Added env knobs for MCP inventory (`MCP_INVENTORY_ENABLED`, `MCP_CONFIG_PATHS`, `MCP_PROJECT_FILES`, `MCP_PROJECT_ROOT`, `MCP_PROJECT_SEARCH_DEPTH`, `MCP_INVENTORY_MAX_CHARS`, `MCP_INVENTORY_INCLUDE_COMMAND`, `MCP_INVENTORY_INCLUDE_ENV_KEYS`).

**Architecture changes:**
- **MCP config parser**: Reads MCP servers from Claude settings and project `.mcp.json` (jsonc supported), with source labeling and secret redaction.
- **Unified Claude start command**: Centralized command builder used by both direct and Docker starts; Docker resume now propagates the session id and appended system prompt.

### v0.23.0 - Worker health watchdog

**Breaking changes:**
- `/learn` command removed.

**New features:**
- **Worker health watchdog thread** (Phase 1): Background daemon samples tmux pane PIDs, Claude PIDs, child counts, and CPU stats every 4s to compute worker health states.
- **8 worker states**: OFFLINE, DEAD, READY, BUSY_TOOL, BUSY_THINKING, WAITING, STUCK, POISONED — with detailed reasons.
- **Detailed `/team` display**: Shows "ready", "working (tools/thinking/waiting)", "STUCK Xm", "POISONED Xm", "DEAD", and "offline".
- **`GET /health/workers` endpoint**: Returns JSON with watchdog state, reason, and age for debugging.
- **Hook event tracking**: Hook responses record timestamps to improve stuck-pending detection.
- **Proactive Telegram alerts** (Phase 2): Watchdog sends alerts to admin when workers enter DEAD/STUCK/POISONED states, with 3-min cooldown per worker. Sends "resolved" alerts when workers recover.
- **POISONED state detection** (Phase 3): When a worker is STUCK, watchdog inspects tmux pane output (interactive) or `adapter.log` (non-interactive) for repeated error signatures (API errors, overloaded, rate limits). 3+ matches of any pattern = POISONED.
- **Explicit restart targeting**: `/restart [--clean] <name>` accepts a worker name argument.
- **GIF support**: Incoming Telegram animations are forwarded to workers; outgoing GIFs use `sendAnimation` to preserve motion.
- **Expanded poison patterns**: Added more error signatures (rate limits, context length, timeouts, 5xx) to POISONED detection.

**Architecture changes:**
- **Watchdog state cache**: In-memory state dictionaries track last child activity, last hook response, last Claude presence, previous states, and alert timestamps.
- **`compute_state()` pure function**: All state logic in a single testable function with no side effects.
- **`_handle_watchdog_transition()`**: Extracted transition logic for alert/resolved decisions, enabling unit testing.
- **Thread-safe watchdog**: Shared watchdog state transitions guarded by a dedicated lock.
- **Adapter-aware watchdog**: Non-interactive workers report BUSY_TOOL while an adapter process is alive.

### v0.21.5 - Adapter stderr logging

**New features:**
- **Adapter stderr logging**: Non-interactive backend adapters (codex, gemini, opencode) now log stderr to `<sessions_dir>/<worker>/adapter.log` (append mode). Previously stderr was sent to `/dev/null`, making adapter failures invisible.
- **`_spawn_adapter()` helper**: Consolidated duplicate Popen logic from three backend classes into a single helper that handles adapter discovery, log file opening, process spawning, and PID tracking.

**Changes:**
- `_adapter_pids` now stores `(Popen, stderr_file_handle)` tuples instead of bare `Popen` objects. `kill_adapter()` closes the file handle on termination.

### v0.21.4 - Adapter PID tracking (kill on /pause)

**New features:**
- **Adapter PID tracking**: Non-interactive backend `send()` methods now store the subprocess PID in `_adapter_pids` dict.
- **`kill_adapter(name)`**: Terminates an inflight adapter process (terminate → wait → kill fallback).
- **`/pause` kills adapter**: For non-interactive backends, `/pause` now kills the running adapter before clearing pending, preventing stale responses.
- **`/end` kills adapter**: Worker removal also kills any inflight adapter.
- **SIGTERM handler in codex adapter**: `codex-tmux-adapter.py` handles SIGTERM gracefully — exits without sending response to bridge.

### v0.21.3 - Non-interactive backend fixes

**New features:**
- **Backend-aware `/restart`**: Non-interactive backends (codex, gemini, opencode) get validation-only resume — reports thread continuity status instead of attempting a restart that does nothing.
- **Backend-aware `/progress`**: Shows `Continuity: on/off` and thread ID for non-interactive backends instead of misleading `Resume: not available`.
- **Backpressure for non-interactive**: Rejects new messages while a non-interactive worker is still processing, preventing silent adapter queuing.
- **`get_any_session_id()`**: Generic helper finds any `*_session_id` file (codex, claude, etc.) instead of hardcoding claude-only.

### v0.21.2 - markdown-it-py renderer + send fallback

**New features:**
- **`markdown_to_telegram_html()`**: AST-based markdown→Telegram HTML converter using `markdown-it-py`. Handles bold, italic, strikethrough, code, code blocks, links, blockquotes, headings, lists, tables (as bullet lists), horizontal rules.
- **400-fallback**: If Telegram rejects HTML (parse error), automatically retries as plain text. No more lost messages.

**Changes:**
- `forward-to-bridge.py` simplified to thin forwarder — sends raw markdown with `escape: True`. All formatting logic centralized in bridge.py.
- Backward compatible: `escape=False` still passes through pre-formatted HTML unchanged.

### v0.21.1 - Remove file sending restrictions

**Changes:**
- **Photos: no path restrictions** — removed allowed-directory check (was limited to /tmp, sessions dir, cwd). Any readable file can now be sent as a photo.
- **Documents: blocklist-only** — removed extension allowlist. Documents now only check against the blocklist of sensitive extensions (.pem, .key, .env, etc.) instead of requiring a known extension.
- **Inter-worker sleep fix** — `send_example` in `/workers` response now includes `&& sleep 1` to prevent tmux batching when sending multiple messages.
- **Updated welcome text** — removed "Allowed paths: /tmp, current directory" since path restrictions are gone.

### v0.21.0 - Worker checkin + instruction refresh

**New features:**
- **`GET /checkin` endpoint**: Workers can `curl -s $BRIDGE_URL/checkin?name=lee` to refresh bridge instructions anytime. Returns plain text with labeled sections (RECEIVING FILES, SENDING FILES, MESSAGING WORKERS, NAME PREFIX, REFRESH INSTRUCTIONS).
- **Welcome on restart**: `restart()` now re-sends the welcome message after resume or relaunch, so workers always get fresh instructions.

**Improvements:**
- **Structured welcome message**: Instructions now have labeled sections instead of a wall of text. Each capability is clearly identified.
- **`_build_welcome()` method**: Welcome message extracted into shared method used by `hire()`, `restart()`, and `/checkin`. Single source of truth for instructions.
- **Self-documenting recovery**: Welcome message includes "REFRESH INSTRUCTIONS: curl -s $BRIDGE_URL/checkin?name=YOUR_NAME" so workers can recover instructions even after context compression.

### v0.20.0 - Session resume + worker messaging

**New features:**
- **`/restart` command**: Restart a worker (default = resume previous session with full context; `--clean` = fresh start). Falls back to fresh start if no session ID is available.
- **Session ID persistence**: Stop hook (`send-to-telegram.sh`) captures session UUID from transcript path and saves to `claude_session_id` file per worker.
- **CWD persistence**: Working directory saved at hire time and in stop hook fallback, enabling cross-directory resume (`--resume` requires matching cwd).
- **`/progress` shows resume status**: Displays "Resume: available (session abc12345...)" or "Resume: not available".

**Improvements:**
- **Simplified worker messaging instructions**: Welcome message now directs workers to call `GET /workers` for ready-to-use send commands, instead of listing all protocol details inline. Workers no longer need to memorize tmux vs pipe syntax.

**How resume works:**
1. Stop hook saves session UUID to `<session_dir>/claude_session_id` on every response
2. Hire saves pane cwd to `<session_dir>/claude_session_cwd`
3. `/restart` reads both files, runs `cd "<cwd>" && claude --resume <UUID> --dangerously-skip-permissions`
4. `/restart --clean` clears all `*_session_id` files for a fresh start

### v0.19.0 - OOP refactor + backend protocol

**Breaking changes:**
- Removed `DIRECT_MODE` support (`--no-tmux` / `--direct` flags and `DIRECT_MODE` env var)
- `backends.py` removed (backend logic lives in `bridge.py`)

**New features:**
- **Backend Protocol** (`typing.Protocol`) with `name`, `is_interactive`, `start_cmd()` (supports optional `append_system_prompt`), `send()`, `is_online()`
- **Backend implementations**: `ClaudeBackend` (interactive), `CodexBackend`, `GeminiBackend`, `OpenCodeBackend` (non-interactive) — all in `bridge.py`
- **WorkerManager**, **TelegramAPI**, **CommandRouter** classes for clearer separation of responsibilities
- **Per-node pipe + inbox isolation**: `/tmp/claudecode-telegram/<node>/<worker>/...`
- **Non-interactive welcome message** includes `BRIDGE_URL` and inter-worker messaging instructions

**Fixes:**
- `chat_id` race condition for non-interactive workers (write `chat_id` before welcome message to avoid `/response` 404s)

**Architecture changes:**
- OOP refactor: lifecycle in `WorkerManager`, command handling in `CommandRouter`, Telegram calls in `TelegramAPI`
- Interactive/non-interactive detection is backend-driven (`backend.is_interactive`), replacing hardcoded backend checks

**Backend interface (grug-brain simple):**
```python
class Backend(Protocol):
    name: str
    is_interactive: bool

    def start_cmd(self, resume_id: str = "", append_system_prompt: str = "") -> str: ...
    def send(self, worker_name, tmux_name, text, bridge_url, sessions_dir) -> bool: ...
    def is_online(self, tmux_name) -> bool: ...
```

### v0.18.3 - Codex learn reaction parity

**Breaking changes:** None.

**New features:**
- None.

**Architecture changes:**
- Codex `/learn` reactions no longer depend on tmux prompt checks.

### v0.18.2 - Codex parity cleanup

**Breaking changes:** None.

**New features:**
- Bot command shortcuts now include codex exec workers.

**Architecture changes:**
- Codex reactions no longer depend on tmux prompt checks.
- Codex pause now clears pending without tmux escape.

### v0.18.1 - Codex backend hardening

**Breaking changes:** None.

**New features:**
- `/workers` now lists codex exec workers with pipe send examples.
- Codex responses are flagged for safe HTML escaping.

**Architecture changes:**
- Codex adapter serializes per-worker session access with a lock file.
- Codex lifecycle now cleans metadata on `/end` and resets session ID on `/restart --clean`.
- Broadcast routing includes codex workers.

### v0.18.0 - Worker backend selection (codex + claude)

**Breaking changes:** None.

**New features:**
- `/hire` now supports worker backend selection:
  - `--codex` flag (e.g., `/hire --codex alice`)
  - `codex-<name>` prefix (e.g., `/hire codex-alice`)
- `/team` and `/progress` now show worker backend metadata.

**Architecture changes:**
- Backend metadata is stored per tmux session via `WORKER_BACKEND` and read during session scans.
- Backend-aware routing paths in the bridge (`worker_is_online`, `worker_send`) for future backend adapters.

### v0.17.0 - Persistence: last chat ID and last active worker

**New features:**
- **Last known chat ID persistence**: Saves chat ID to file; on restart, automatically sends "I'm online" message to last known chat
- **Last active worker persistence**: Saves focused worker name to file; on restart, automatically restores focus to that worker

**How it works:**
- `~/.claude/telegram/nodes/<node>/last_chat_id` - stores last admin chat ID
- `~/.claude/telegram/nodes/<node>/last_active` - stores last focused worker name
- On bridge startup:
  - Loads last chat ID and sends startup notification immediately
  - Loads last active worker and sets as focused (if still exists)
- Chat ID is updated on every admin message (keeps it fresh)
- Active worker is saved on `/hire`, `/focus`, and worker slash commands

**Why:**
- Previously, after bridge restart you had to send a message to the bot to know it was online
- Previously, after bridge restart the focus was lost and you had to `/focus <name>` again
- Now the bridge proactively notifies you and restores your workflow

### v0.16.4 - Orphan detection, status improvements, tmux delay fix

**Fixes:**
- Add 0.2s delay between `tmux send-keys` text and Enter to prevent race condition where text and Enter interleave

**New features:**
- `status --all` now detects orphan processes (tunnels/bridges not owned by any node)
- Conflict detection is per bot_id (not just running node count)
- Webhook mismatch warning when tunnel URL differs from actual webhook URL

**Improvements:**
- Save bot_id/username at startup for faster status display
- Exit cleanly on webhook setup failure (don't leave orphan processes)
- Add .dockerignore for cleaner Docker builds

### v0.16.3 - Hook reliability and status diagnostics

**Fixes:**
- Hook env var precedence: tmux session env takes priority over shell env
- Hook session name detection works without TMUX env var

### v0.16.2 - Reliable message reaction

**Improvement:** 👀 reaction now only appears when Claude Code actually accepts the message.

**How it works:**
- After sending message + Enter, polls tmux pane for up to 500ms
- Checks if Claude Code's input prompt (`❯`) is empty
- Empty prompt = message was submitted → show 👀
- Text still in prompt = submit failed → no reaction

**Why:** Previously, reaction appeared immediately after `tmux send-keys` succeeded, but this didn't guarantee Claude Code actually processed the Enter as a submit (could be in multi-line mode, busy, etc.).

### v0.16.1 - Sandbox disabled by default

**Breaking change:** Sandbox mode is now disabled by default (was enabled).

**Why:** Sandbox mode is not stable yet. Use `--sandbox` flag to explicitly enable Docker isolation.

**Added:** Node configuration documentation with recommended sandbox settings per node type.

### v0.16.0 - Simplify config (remove config.env, add clean command)

**New `clean` command:**
```bash
./claudecode-telegram.sh --node prod clean   # Reset stale chat_id files
```
Fixes the "wrong chat_id persists forever" issue by removing admin_chat_id and session chat_id files. Next message re-registers admin.

**Removed config.env:**
- No more `~/.claude/telegram/nodes/<node>/config.env` files
- PORT now derived from node name: prod=8271, dev=8272, test=8295
- Override via `--port` flag or `PORT` env var

**Why:** config.env caused stale config issues similar to chat_id persistence. All config should be explicit (env vars, flags) or derived (node name), not persisted files.

### v0.15.0 - Code cleanup (Codex-reviewed)

**Removed unused code:**
- `docker_container_exists()` function (never called)
- `SESSION_ID` variable in hook (parsed but never used)
- `NODE_NAME` export to bridge (bridge never reads it)
- Port file write (hook never reads it)

**Simplified `export_hook_env()`:**
- Removed dangerous `tmux send-keys 'export ...'` that could inject text into running Claude sessions
- Now uses only `tmux set-environment` (hooks read via `tmux show-environment`)
- `BRIDGE_URL` only set when user-provided (not default) - clearer override semantics
- Removed `TMUX_FALLBACK` export (hook already defaults to 1)

**Net result:** ~30 lines removed, safer hook env setup, cleaner config semantics.

### v0.14.0 - BRIDGE_URL support for remote workers

**New feature:** Workers can now connect to remote bridges, enabling distributed deployments.

| Config | Use Case |
|--------|----------|
| `PORT=8271` (default) | Local setup, hook builds `http://localhost:8271/response` |
| `BRIDGE_URL=http://host.docker.internal:8271` | Docker containers |
| `BRIDGE_URL=https://bridge.company.com` | Remote workers on different machines |

**How it works:**
- Bridge exports `BRIDGE_URL` to tmux session environment (alongside existing PORT, TMUX_PREFIX, SESSIONS_DIR)
- Hook reads `BRIDGE_URL` first, falls back to building from `PORT`
- User-provided `BRIDGE_URL` takes precedence over auto-generated URLs
- Trailing slashes are stripped automatically

**Backward compatible:** Existing setups using only `PORT` continue to work unchanged.

### v0.13.2 - Fix hook template causing duplicate messages

**Bug fix:** Per-node hooks were using template mode defaults instead of baked config.

The issue was overly complex conditional logic that broke after sed substitution.
All three hooks (prod/dev/sandbox) matched the same sessions → duplicates.

**Fix:** Simplified hook template - removed all conditionals, just use baked values directly:
```bash
# Before: complex conditional with template mode fallback
NODE_NAME="__NODE_NAME__"
NODE_PREFIX="__NODE_PREFIX__"
if [[ "$NODE_NAME" != "__NODE_NAME__" ]]; then
    TMUX_PREFIX="$NODE_PREFIX"
else
    TMUX_PREFIX="${TMUX_PREFIX:-claude-}"  # fallback
fi

# After: direct assignment, no conditionals
TMUX_PREFIX="__NODE_PREFIX__"
SESSIONS_DIR="__NODE_SESSIONS_DIR__"
BRIDGE_PORT="__NODE_PORT__"
```

### v0.13.1 - Fix hook install structure for multi-node

**Bug fix:** `hook install` was generating incorrect Claude Code settings.json structure.

The hooks configuration must use nested structure per Claude Code docs:
```json
// WRONG (old)
{"hooks":{"Stop":[{"type":"command","command":"..."}]}}

// CORRECT (fixed)
{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"..."}]}]}}
```

**Changes:**
- Fixed `cmd_hook_install` to create correct nested structure
- Added migration logic for existing incorrect settings
- New installs now create proper `hooks.Stop[0].hooks[]` array

### v0.13.0 - Simplified sandbox mode

**Breaking changes:**
- Removed `--project-root` flag (no longer needed)
- Removed `SANDBOX_PROJECT_ROOT` env var
- Simplified Docker command (no read-only rootfs, no tmpfs mounts)

**New design:**
- Default: mounts `~` to `/workspace` (read-write)
- Workers run with `--dangerously-skip-permissions` (same as non-sandbox)
- Extra mounts via `--mount` and `--mount-ro` CLI flags

**CLI flags:**
```bash
--sandbox              # Enable (default)
--no-sandbox           # Disable
--sandbox-image <img>  # Docker image
--mount <path>         # Extra mount (host:container or just path)
--mount-ro <path>      # Extra mount, read-only
```

**Example:**
```bash
./claudecode-telegram.sh start --sandbox \
  --mount /data \
  --mount /var/log:/logs \
  --mount-ro /etc/ssl/certs
```

**Startup messages:**
- Terminal: Shows sandbox status with all mounts
- Telegram: Short sandbox note on "server online"
- `/settings`: Detailed sandbox info (image, mounts, limitations)

**Implementation:**
- `SANDBOX_EXTRA_MOUNTS` replaces `SANDBOX_MOUNTS` and `SANDBOX_MOUNT_FILES`
- `get_docker_run_cmd()` simplified (no project_root parameter)
- `cmd_settings()` shows detailed sandbox info
- `send_startup_message()` includes sandbox note

### v0.12.1 - Outgoing file support (workers can send files back)

**New feature:** Workers can now send documents back to Telegram using `[[file:/path|caption]]` tag.

**Tag syntax:**
```
[[file:/tmp/report.pdf|Q4 Report]]
[[file:/tmp/data.csv]]  (caption optional)
```

**Allowed extensions (auto-routed to best Telegram API method):**
- Docs: `.md`, `.txt`, `.rst`, `.pdf` → sendDocument
- Data: `.json`, `.csv`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.xml`, `.log`, `.sql`, `.patch`, `.diff` → sendDocument
- Code: `.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.go`, `.rs`, `.java`, `.kt`, `.swift`, `.rb`, `.php`, `.c`, `.cpp`, `.h`, `.hpp`, `.sh`, `.html`, `.css`, `.scss` → sendDocument
- Archives: `.zip`, `.tar`, `.gz` → sendDocument
- Video: `.mp4`, `.mov`, `.avi`, `.mkv`, `.webm` → sendVideo (player UI)
- Audio: `.mp3`, `.m4a`, `.flac`, `.aac`, `.wav` → sendAudio (player UI)
- Voice: `.ogg`, `.opus`, `.oga` → sendVoice (voice bubble)
- Stickers: `.tgs` → sendSticker (animated sticker)

**Security (blocked):**
- Extensions: `.pem`, `.key`, `.p12`, `.pfx`, `.crt`, `.cer`, `.der`, `.jks`, `.keystore`, `.kdb`, `.pgp`, `.gpg`, `.asc`
- Filenames: `.env`, `.env.*`, `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, `id_rsa`, `id_ed25519`, `credentials`, `kubeconfig`

**Implementation:**
- `send_document()` function with Telegram sendDocument API
- `parse_file_tags()` for tag parsing
- `is_blocked_filename()` for sensitive file detection
- Path validation (must be in /tmp, sessions dir, or cwd)
- Size limit: 20MB (Telegram's limit)

### v0.12.0 - File attachment support

**New feature:** Manager can now send any file type to workers, not just images.

- **PDF, documents, code files** - all file types are accepted and downloaded to worker's inbox
- **Metadata passed to Claude** - filename, size, mime type included in message
- **Same inbox system** - files stored in `/tmp/claudecode-telegram/<node>/<session>/inbox/`
- **Automatic cleanup** - files removed when worker is offboarded

**Message format to Claude:**
```
Manager sent file: document.pdf (1.5 MB, application/pdf)
Path: /tmp/claudecode-telegram/<node>/worker/inbox/abc123.pdf
```

**Implementation:**
- `send_document_message()` test helper added
- `format_file_size()` function for human-readable sizes
- `FILE_INBOX_ROOT` renamed from `IMAGE_INBOX_ROOT` (now handles all files)
- Welcome message updated to mention file attachment support

### v0.11.0 - Sandbox mode (Docker isolation)

**New Features:**
- **Sandbox mode** (default: disabled): Workers can run in Docker containers for isolation
- No more `--dangerously-skip-permissions` needed when using sandbox
- `--sandbox` / `--no-sandbox` flags to toggle
- `--sandbox-image` to specify Docker image
- `--project-root` to mount project directories

**Mounts in sandbox mode:**
- `~/.claude` - Claude Code config
- `~/.codex` - Codex config
- `~/.gemini` - Gemini config
- `~/team/` - Team playbook, learnings, per-agent state (read-only)
- Project directory (specified via `--project-root`)

**Security improvements:**
- Workers isolated via Docker containers
- Read-only root filesystem
- Dropped all capabilities
- No new privileges
- PID limit (512)

**Fallback:** Use `--no-sandbox` for legacy behavior (direct execution with `--dangerously-skip-permissions`)

### v0.10.2 - Message splitting for Telegram 4096 char limit

**New feature:** Long responses are now automatically split into multiple messages to fit Telegram's 4096 character limit.

**Split strategy:**
- Messages split on safe boundaries: blank lines → newlines → spaces → hard cut
- Multi-part messages show part numbers: `<b>worker (1/3):</b>`
- Parts are chained with `reply_to_message_id` for visual grouping
- Small delay (50ms) between parts ensures correct ordering

**Implementation:**
- `split_message(text, max_len)` - splits text on safe boundaries
- `find_split_point(text, max_len)` - finds best split point
- `format_multipart_messages(session_name, chunks)` - adds prefix + part numbers
- `handle_hook_response()` now loops through chunks and sends sequentially

### v0.10.1 - Support --flag=value argument syntax

**Bug fix:** Argument parser now accepts both `--flag=value` and `--flag value` syntax.

Previously, `--port=1789` was silently ignored (falling back to node config), causing confusing errors like "Port 8271 is already in use" when you specified a different port.

Now both work:
```bash
./claudecode-telegram.sh run --node=prod --port=1789   # ✅ equals syntax
./claudecode-telegram.sh run --node prod --port 1789   # ✅ space syntax
```

### v0.10.0 - Simplify CLI (~200 lines removed)

**Breaking changes:**
- Removed `start` command (use `run --no-tunnel` instead)
- Removed `setup` command (use `status` instead)

**New flag:**
- `--no-tunnel` for `run` command: skip tunnel/webhook setup (replaces `start`)

**Simplifications:**
- Removed smart port conflict recovery (now just errors if port busy)
- Removed `find_free_port()`, `is_our_bridge()`, `handle_port_conflict()`, `offer_alternative_port()`
- Added simple `require_port_free()` - errors with hint to use `--port`

**Why:** Less magic, more predictable. If port is busy, user decides what to do.

### v0.9.8 - Remove unused HOST variable

**Bug fix:**
- Removed `HOST` variable from `cmd_start()` that was supposed to be removed in v0.9.5
- Fixed malformed log output "on :8080" → "on port 8270"
- Removed `--host` flag from `cmd_start` (was non-functional)

### v0.9.7 - SIGTERM diagnostics and improved logging

**Shutdown diagnostics:**
- Bridge now logs timestamp, parent PID, and parent cmdline on SIGTERM
- Helps identify what process/script triggered unexpected shutdowns

**Logging improvements:**
- Both `start` and `run` commands now write bridge output to log file
- `start` command uses `tee` to show output AND log to file
- `run` command appends to `$node_dir/bridge.log`

### v0.9.6 - Documentation updates & hook refinements

**CLAUDE.md learnings added:**
- Port ownership verification: always check actual port file before killing (ports are configurable defaults)
- Dev before prod deployment order: local tests → dev node → manual verify → prod
- tmux send race condition: per-session locks serialize concurrent sends

**Hook improvements:**
- Refined `wait_for_transcript()` polling with clearer deadline-based logic
- Removed redundant comments for cleaner code

**C bridge test fix:**
- Added `TMUX_TMPDIR` to test environment for proper isolation

### v0.9.5 - Simplified environment variables & smart port conflict handling

**Environment variable simplification:**
- Only `TELEGRAM_BOT_TOKEN` is required to run
- `ADMIN_CHAT_ID` and `TUNNEL_URL` remain optional
- Internal vars (`PORT`, `SESSIONS_DIR`, `TMUX_PREFIX`) auto-derived per node
- Removed unused `HOST` variable

**Smart port conflict handling:**
- Detects if port is held by our bridge vs another process
- Auto-restarts old bridge gracefully (no user action needed)
- Falls back to suggesting alternative port if not our process
- Uses `SO_REUSEADDR` to prevent "Address already in use" on restart

**Manager-friendly copy improvements:**
- First-person assistant voice for status messages
- "Kenji is paused. I'll pick up where we left off." vs technical jargon
- Focus hint on worker switch: "Now talking to Lee."

**Test improvements:**
- E2E image test skips gracefully when no real `TEST_CHAT_ID` provided
- Updated test.sh header with clearer usage documentation

### v0.9.4 - Slash command routing: /lee, /chen, etc.

**New feature: Direct worker routing via slash commands**
- `/lee hello` routes message directly to lee and sets focus
- `/lee` (no message) switches focus to lee (same as `/focus lee`)
- Telegram autocomplete shows all workers as commands

**Hire validation:**
- Cannot hire workers with reserved names (team, focus, hire, end, etc.)
- Prevents command collisions

**Dynamic bot commands:**
- Bot command list updates when workers are hired/offboarded
- Workers appear in Telegram's command autocomplete

**Why this matters:**
- UX improvement: `/lee` is shorter than `/focus lee` or `@lee`
- Telegram native: uses command autocomplete, works in groups with privacy mode
- Safe: reserved names blocked at hire time

### v0.9.3 - Philosophy alignment: ephemeral image inbox, prompt-only /learn, hook polling

**Philosophy fixes:**
- **Image inbox moved to /tmp**: Images now stored in `/tmp/claudecode-telegram/<node>/<session>/inbox/` instead of `~/.claude/telegram/sessions/<session>/inbox/`
- **Inbox cleanup on session end**: `cleanup_inbox()` called when worker is offboarded via `/end`
- **Removed playbook persistence**: `/learn` is prompt-only, doesn't write to disk
- **Session isolation**: Each session's inbox is namespaced to prevent cross-session access

**Hook transcript race condition fix:**
- **Problem**: Stop hook fires before Claude Code flushes final text response to transcript
- **Symptom**: Image tags (`[[image:...]]`) missing from responses sent to Telegram
- **Fix**: Hook now polls transcript until stable (file size unchanged + content parseable)
- **Implementation**: `wait_for_transcript()` function with 2s timeout, 50ms polling interval
- **Why polling over sleep**: Avoids magic numbers, only waits when needed, has clear timeout

**Why this matters:**
- Aligns with "RAM state only" principle - no durable state outside tmux
- Per-session files remain minimal coordination metadata (pending, chat_id)
- Images are ephemeral input artifacts, cleaned up automatically
- Team playbook management is external (e.g., `~/team/playbook.md`) - not bridge's responsibility

### v0.9.2 - Fix tmux send race condition

**Problem:** Concurrent messages to the same tmux session could interleave, causing messages to corrupt each other. This was especially visible under rapid message load where ~50% of messages would fail.

**Root cause:** The two-call pattern (`tmux_send` + `tmux_send_enter`) was not atomic. When multiple threads sent messages to the same session simultaneously, their calls could interleave (e.g., text1, text2, Enter1, Enter2).

**Fix:** Added per-session locks to serialize sends to the same tmux session:
- New `_tmux_send_locks` dictionary holds one lock per session
- New `tmux_send_message()` function wraps send+enter in a lock
- All three send locations (route_message, cmd_learn, share_learning_with_team) now use the locked function

**Testing:** Stress test showed improvement from 58% → 100% delivery rate under concurrent load.

**Note:** v0.9.1 was attempted with an atomic `text\n` approach but made things worse because `-l` flag sends literal newline, not Enter key. That was reverted.

### v0.9.0 - Image Support

**New features:**
- **Incoming images**: Manager can send photos/images to workers
  - Images downloaded to `/tmp/claudecode-telegram/<node>/<worker>/inbox/` (ephemeral)
  - Path passed to Claude: "Manager sent image: /path/to/image.jpg"
  - Supports photos and image documents (files sent as attachments)
  - Optional caption included in message
  - Cleaned up automatically when worker is offboarded

- **Outgoing images**: Workers can send images back via tag syntax
  - Use `[[image:/path/to/file.jpg|optional caption]]` in responses
  - Bridge parses tags and sends via Telegram's sendPhoto API
  - Multiple images per response supported
  - Caption is optional: `[[image:/path.png]]` works too

**Security:**
- Path allowlist: Only files in /tmp, sessions dir, or cwd can be sent
- Extension validation: .jpg, .jpeg, .png, .gif, .webp, .bmp, .mp4 (animations) allowed
- Size limit: 20MB max (Telegram's limit)
- Inbox directories use 0o700 permissions, session-namespaced

**Usage:**
```
# Manager sends image in Telegram
[photo attachment with optional caption]

# Worker receives
Manager sent image: /tmp/claudecode-telegram/<node>/worker/inbox/abc123.jpg
Please describe this screenshot

# Worker responds with image
Here's the diagram:
[[image:/tmp/diagram.png|Architecture overview]]
```

### v0.8.0 - Manager-friendly UX overhaul

**New command aliases (manager-friendly):**
| Old | New |
|-----|-----|
| `/new` | `/hire` |
| `/use` | `/focus` |
| `/list` | `/team` |
| `/kill` | `/end` |
| `/status` | `/progress` |
| `/stop` | `/pause` |
| `/restart` | `/restart` |
| `/system` | `/settings` |

**New command:**
- `/learn` - Ask focused worker about today's learnings (prompt-only, no persistent storage)

**Voice & terms updated:**
- "sessions" → "workers" in all user-facing messages
- "active" → "focused"
- Outcome-first responses: `Done —`, `Working —`, `Needs decision —`
- Persistence emphasized: "Workers are long-lived and keep context across restarts."

**Daily Learning workflow:**
- `/learn` prompts the focused worker to share learnings (Problem/Fix/Why format)
- Learnings shared with all online workers via tmux
- Team playbook managed externally (e.g., `~/team/playbook.md`) - bridge doesn't persist

### v0.7.0 - Threaded HTTP + session refactors

**Changes:**
- Use `ThreadingHTTPServer` to handle concurrent requests
- Remove cached session registry; derive sessions on demand from tmux
- Centralize hook environment export in `export_hook_env()`

### v0.6.9 - Remove HTML escaping

**Changes:**
- Removed HTML escaping from responses
- Claude Code already handles output safety
- Simpler code, preserves all formatting

### v0.6.8 - Fix HTML tag rendering

**Bug fix:**
- Allowed HTML tags (`<code>`, `<pre>`, `<b>`, `<i>`) now render properly in Telegram
- Other HTML is still escaped for safety
- Fixes issue where Claude's code formatting showed literal `<code>` tags

### v0.6.7 - Session-prefixed responses

**Changes:**
- `/response` messages now include a bold `<b>{session}:</b>` prefix
- HTML-escaped body prevents formatting injection from Claude output

### v0.6.6 - @all broadcast

**New feature:**
- `@all <message>` broadcasts to all running Claude sessions
- Each session receives the message and responds independently
- Confirmation shows which sessions received the broadcast

**Usage:**
```
@all what's your status?
```

### v0.6.5 - Auto-clear stale pending files

**Changes:**
- Pending files now auto-delete after 10 minutes
- Reverted double Enter (didn't help with batching)
- Fixes "busy" status getting stuck when hooks don't fire

### v0.6.4 - Fix message batching with double Enter

**Bug fix:**
- Bridge now sends double Enter when routing messages
- Forces Claude Code to submit even when processing previous message
- Prevents messages from batching together and causing missed responses

### v0.6.3 - Fix port mismatch on bridge restart

**Bug fix:**
- Bridge now writes `port` file to node directory on startup
- Hook reads port from file instead of env var
- Fixes issue where hook sent to wrong port after bridge restart

### v0.6.2 - Remove pending gate, enable proactive messaging

**Breaking change in hook behavior:**
- Hook now sends to Telegram if `chat_id` exists, regardless of `pending` file
- `pending` file is now only used for busy status indicator (UI), not as a send gate

**Why this change:**
- Fixes race condition where multiple rapid messages could cause lost responses
- Enables Claude to send proactive messages to Telegram
- Simplifies the send logic: `chat_id` exists = Telegram session = send responses

**What stays the same:**
- `pending` file still created when message arrives (for busy indicator)
- `pending` file still cleared after response (to update busy status)
- Sessions without `chat_id` (non-Telegram) don't send to Telegram

### v0.6.1 - Fix pending file cleanup

**Bug fix:**
- Stop hook now cleans up `pending` file after successfully sending response to bridge
- Previously, sessions would appear "busy" forever because pending file was never removed
- Also fixes early exit on empty response (now properly cleans up pending file)

### v0.6.0 - Multi-Node Support

**Breaking changes:**

| Before | After |
|--------|-------|
| Single node, shared state | Multiple nodes, isolated state |
| `~/.claude/telegram/sessions/` | `~/.claude/telegram/nodes/<node>/sessions/` |
| `claude-<name>` tmux prefix | `claude-<node>-<name>` tmux prefix |
| `TELEGRAM_BOT_TOKEN` required | Same - always via env var |

**New features:**
- **`NODE_NAME` env var**: Target specific node
- **`--node` / `-n` flag**: Target specific node via CLI
- **`--all` flag**: Target all nodes (stop, status)
- **`clean` command**: Reset stale chat_id files
- **Per-node state isolation**: Each node has its own sessions, PIDs, ports
- **Smart auto-detection**: If only one node running, uses it; if multiple, prompts or errors
- **Default ports**: prod=8271, dev=8272, test=8295 (override with `--port` or `PORT` env var)

**Usage:**
```bash
# Start nodes (PORT defaults derived from node name, overridable)
NODE_NAME=prod ./claudecode-telegram.sh --no-sandbox run    # default port 8271
NODE_NAME=dev ./claudecode-telegram.sh --no-sandbox run     # default port 8272

# Stop specific node
./claudecode-telegram.sh --node dev stop

# Clean stale chat_id (fixes wrong admin)
./claudecode-telegram.sh --node prod clean

# Status of all nodes
./claudecode-telegram.sh --all status
```

**Recommended node configurations:**
| Node | Token | Sandbox | Default Port | Purpose |
|------|-------|---------|--------------|---------|
| test | TEST_BOT_TOKEN | `--no-sandbox` | 8295 | Automated tests (fast, no Docker) |
| prod | PROD_BOT_TOKEN | `--no-sandbox` | 8271 | Production (performance) |
| dev | DEV_BOT_TOKEN | `--no-sandbox` | 8272 | Development |
| sandbox | TEST_BOT_TOKEN | `--sandbox` | 8270 | Untrusted/experimental code |

Ports are defaults, not fixed — override with `--port <n>` or `PORT` env var.

**Why `--no-sandbox` for prod/dev/test?** Docker overhead impacts performance. Use sandbox node for isolation when running untrusted code.

**Directory structure:**
```
~/.claude/telegram/nodes/
├── prod/
│   ├── pid             # Main process PID
│   ├── bridge.pid      # Bridge process PID
│   ├── tunnel.pid      # Tunnel process PID
│   ├── tunnel_url      # Current tunnel URL
│   └── sessions/       # Per-session files
│       └── <worker>/
│           ├── chat_id   # Admin chat ID for responses
│           └── pending   # Request timestamp
└── dev/
    └── ...
```

**Backward compatibility:**
- Default node is "prod" if no `NODE_NAME` specified
- Existing single-node setups continue to work

### v0.5.4 - Tunnel Health Check

**Improvements:**
- **Tunnel watchdog now checks reachability**: Previously only checked if cloudflared process was alive. Now also curls the tunnel URL to verify it's actually reachable.
- **Kills zombie tunnels**: If tunnel process is alive but URL unreachable, kills the process and restarts.

**Why this matters:**
- Cloudflare quick tunnels can become unreachable while the process is still running
- This catches cases where DNS expires or Cloudflare revokes the tunnel
- Faster recovery from tunnel failures

### v0.5.3 - Restart Command

**New features:**
- **`restart` command**: Restart gateway only, preserves tmux sessions
- **Version in startup log**: Shows `Starting Claude Code Telegram Bridge v0.5.3`

**Usage:**
```bash
./claudecode-telegram.sh restart   # Restarts bridge + tunnel, keeps sessions
```

### v0.5.2 - PID File

**New features:**
- **PID file**: Main process writes PID to `~/.claude/telegram/claudecode-telegram.pid`
- **Improved `stop` command**: Uses PID file for clean shutdown of main process + children
- PID displayed at startup for easy identification
- PID file removed on clean shutdown

**Why:**
- Easy identification of claudecode-telegram processes
- Clean termination via `./claudecode-telegram.sh stop` or `kill $(cat ~/.claude/telegram/claudecode-telegram.pid)`

### v0.5.1 - Test Isolation & System Command

**New features:**
- **`/system` command**: Shows system configuration with secrets redacted
- **`ADMIN_CHAT_ID` env var**: Pre-lock admin instead of auto-learn (recommended for production)
- **`TMUX_PREFIX` env var**: Configurable tmux session prefix (default: `claude-`)
- **`SESSIONS_DIR` env var**: Configurable session files directory

**Test improvements:**
- Full test/prod isolation (separate prefix, port, session dir)
- New `test_response_endpoint`: Tests complete hook → bridge → Telegram flow
- `ADMIN_CHAT_ID` in tests enables full e2e with real Telegram messages
- Success logging for response sends

**Hook improvements:**
- Hook now reads `TMUX_PREFIX` env var for session detection
- Hook reads `PORT` env var for bridge endpoint

### v0.5.0 - Tunnel Watchdog

**New features:**
- Watchdog monitors cloudflared process every 10 seconds
- Auto-restarts tunnel if it dies
- Updates webhook with new URL automatically
- Notifies users via Telegram on tunnel reconnect
- `/notify` endpoint for system alerts

**Architecture:**
- Shell script manages cloudflared lifecycle
- Token stays in bridge (security principle maintained)

### v0.4.0 - Testing Framework

**New features:**
- `/restart` command to restart Claude in session
- `/status` shows Claude process state (not just tmux)
- `--tunnel-url` flag for persistent tunnel URLs
- Startup/shutdown notifications to Telegram
- Fix `/command@botname` parsing (Telegram autocomplete)

**Testing:**
- `test.sh` automated acceptance tests
- `TEST.md` testing documentation

### v0.3.1 - Bug Fixes

**Fixes:**
- **Startup crash**: Fixed `((attempts++))` causing script exit with `set -e` when attempts=0 (bash arithmetic returns exit code 1 when expression evaluates to 0)
- **Claude confirmation dialog**: Added automatic acceptance of `--dangerously-skip-permissions` confirmation prompt (selects "Yes, I accept")

**Technical details:**
- Changed `((attempts++))` to `((++attempts))` in tunnel URL wait loop
- Added keystrokes to bridge.py to navigate and accept Claude's startup dialog

### v0.3.0 - Security Hardening

**Security principle: Token never leaves the bridge.**

| Before (v0.2.0) | After (v0.3.0) |
|-----------------|----------------|
| Token exported to Claude tmux session | Token only in bridge process |
| Hook calls Telegram API directly | Hook forwards to bridge via localhost |
| Any Telegram user can control bot | First user auto-registered as admin |
| Default file permissions | Explicit 0o700/0o600 permissions |
| No webhook verification | Optional `TELEGRAM_WEBHOOK_SECRET` |

**New security features:**
- **Token isolation**: Claude sessions never see `TELEGRAM_BOT_TOKEN`
- **Bridge-centric architecture**: Hook → localhost HTTP → bridge → Telegram
- **Admin auto-learn**: First user to message becomes admin (RAM only)
- **Silent rejection**: Non-admin users get no response (bot doesn't reveal itself)
- **Secure file permissions**: Session directories 0o700, files 0o600
- **Optional webhook verification**: Set `TELEGRAM_WEBHOOK_SECRET` to verify Telegram requests

**Architecture change:**
```
Before:                              After:
Claude (has token)                   Claude (NO token)
    │                                    │
    └─► Hook calls Telegram API          └─► Hook POSTs to localhost:8270/response
                                              │
                                              ▼
                                         Bridge (has token) ─► Telegram API
```

### v0.2.0 - Multi-Session Control Panel

**Breaking changes from v0.1.0:**

| v0.1.0 (Single Session) | v0.2.0 (Multi-Session) |
|-------------------------|------------------------|
| One tmux session: `claude` | Multiple: `claude-<name>` |
| Global files: `~/.claude/telegram_chat_id` | Per-session: `~/.claude/telegram/sessions/<name>/` |
| `TMUX_SESSION` env var | Sessions created via `/new` |
| Messages → single Claude | Messages → active session or `@name` routing |

**New features:**
- `/new <name>` - Create named Claude instance
- `/use <name>` - Switch active session
- `/list` - List all instances (scans tmux)
- `/kill <name>` - Stop and remove instance
- `@name <msg>` - One-off message routing
- Auto-discovery of `claude-*` sessions on startup
- JSON registration for unregistered sessions

**Architecture changes:**
- No persistent state file - tmux IS the persistence
- RAM state rebuilt on gateway restart
- Per-session file isolation for hook coordination

### v0.1.0 - Initial Release

- Single tmux session support
- Basic Telegram ↔ Claude bridging
- `/clear`, `/resume`, `/continue_`, `/loop`, `/stop`, `/status` commands
