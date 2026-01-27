# Design Philosophy

> Version: 0.6.0 (Multi-Node Support)

## Current Philosophy (Summary)

| Principle | Description |
|-----------|-------------|
| **tmux IS persistence** | No database, no state.json - tmux sessions are the source of truth |
| **`claude-<name>` naming** | Configurable prefix via `TMUX_PREFIX` (default: `claude-`) |
| **RAM state only** | Rebuilt on startup from tmux, never persisted |
| **Per-session files** | Minimal hook↔gateway coordination via filesystem |
| **Fail loudly** | No silent errors, no hidden retries |
| **Token isolation** | `TELEGRAM_BOT_TOKEN` never leaves bridge process |
| **Admin config** | Pre-set via `ADMIN_CHAT_ID` or auto-learn first user |
| **Secure by default** | 0o700 dirs, 0o600 files, silent rejection of non-admins |

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

User says `/new backend` → tmux session `claude-backend`

This prefix pattern enables:
- **Auto-discovery**: `tmux list-sessions | grep ^claude-` finds all managed sessions
- **Namespace isolation**: Won't conflict with user's other tmux sessions
- **Clear ownership**: Obvious which sessions belong to the bridge

## RAM State: Ephemeral by Design

```python
state = {
    "active": "backend",      # Which session receives bare messages
    "sessions": {...},        # Cache of discovered sessions
    "pending_registration": None
}
```

This state is:
- Rebuilt on startup from tmux
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
/new backend             → creates claude-backend, sets active
/use frontend            → sets active = frontend
@backend do something    → claude-backend (one-off)
fix the bug              → active session (currently frontend)
```

The `@name` syntax allows one-off messages without switching context. You're working on frontend but need backend to do something? `@backend run the tests` — no context switch needed.

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

The `/list` command shows sessions and their pending status. That's it.

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

The `@name` syntax and `/use` command give you full control without the overhead of multiple chats.

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
│                    │ POST localhost:8080/response       │
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

### v0.6.0 - Multi-Node Support

**Breaking changes:**

| Before | After |
|--------|-------|
| Single node, shared state | Multiple nodes, isolated state |
| `~/.claude/telegram/sessions/` | `~/.claude/telegram/nodes/<node>/sessions/` |
| `claude-<name>` tmux prefix | `claude-<node>-<name>` tmux prefix |
| `TELEGRAM_BOT_TOKEN` required | Can use per-node `config.env` |

**New features:**
- **`NODE_NAME` env var**: Target specific node
- **`--node` / `-n` flag**: Target specific node via CLI
- **`--all` flag**: Target all nodes (stop, status)
- **Per-node config files**: `~/.claude/telegram/nodes/<node>/config.env`
- **Per-node state isolation**: Each node has its own sessions, PIDs, ports
- **Smart auto-detection**: If only one node running, uses it; if multiple, prompts or errors

**Usage:**
```bash
# Start nodes
NODE_NAME=prod ./claudecode-telegram.sh run
NODE_NAME=dev ./claudecode-telegram.sh run

# Stop specific node
./claudecode-telegram.sh --node dev stop

# Status of all nodes
./claudecode-telegram.sh --all status

# Per-node config (optional)
cat ~/.claude/telegram/nodes/prod/config.env
# TELEGRAM_BOT_TOKEN=...
# PORT=8080
# ADMIN_CHAT_ID=...
```

**Directory structure:**
```
~/.claude/telegram/nodes/
├── prod/
│   ├── config.env      # Node configuration
│   ├── pid             # Main process PID
│   ├── bridge.pid      # Bridge process PID
│   ├── tunnel.pid      # Tunnel process PID
│   ├── port            # Current port
│   ├── tunnel_url      # Current tunnel URL
│   └── sessions/       # Per-session files
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
    └─► Hook calls Telegram API          └─► Hook POSTs to localhost:8080/response
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
