# Design Philosophy

> Version: 0.3.0 (Security Hardening)

## Current Philosophy (Summary)

| Principle | Description |
|-----------|-------------|
| **tmux IS persistence** | No database, no state.json - tmux sessions are the source of truth |
| **`claude-<name>` naming** | Enables auto-discovery via `tmux list-sessions \| grep ^claude-` |
| **RAM state only** | Rebuilt on startup from tmux, never persisted |
| **Per-session files** | Minimal hook↔gateway coordination via filesystem |
| **Fail loudly** | No silent errors, no hidden retries |
| **Token isolation** | `TELEGRAM_BOT_TOKEN` never leaves bridge process |
| **Admin auto-learn** | First user to message becomes admin (RAM only) |
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

### Admin Auto-Learn

```python
admin_chat_id = None  # RAM only

def handle_message(update):
    chat_id = update["message"]["chat"]["id"]

    # First user becomes admin
    if admin_chat_id is None:
        admin_chat_id = chat_id

    # Reject non-admins silently
    if chat_id != admin_chat_id:
        return  # Don't reveal bot exists
```

Why auto-learn instead of config file?
1. **Zero configuration** - Just start and message
2. **Natural UX** - First user is obviously the owner
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
