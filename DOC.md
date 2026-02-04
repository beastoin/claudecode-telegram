# Design Philosophy

> Version: 0.18.0

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
@backend do something    → claude-backend (one-off)
fix the bug              → active session (currently frontend)
```

The `@name` syntax allows one-off messages without switching context. You're working on frontend but need backend to do something? `@backend run the tests` — no context switch needed.

## Feedback Philosophy

- 👀 means the message hit the worker.
- The worker reply is the confirmation: `worker_name: response`.
- Text replies only for errors and state/info commands (`/hire`, `/end`, `/focus`, `/team`, `/progress`).
- Regular messages, `@mentions`, and `/learn` are silent.
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

## Direct Mode (--no-tmux)

Direct mode spawns Claude Code as a subprocess with JSON streaming instead of using tmux sessions. This enables tighter integration and avoids terminal emulation overhead.

### Architecture Comparison

**tmux mode (default):**
```
┌──────────┐     ┌──────────┐     ┌─────────────┐     ┌─────────────┐
│ Telegram │────▶│  Bridge  │────▶│ tmux session│────▶│   Claude    │
│ Webhook  │     │ (Python) │     │ send-keys   │     │ (terminal)  │
└──────────┘     └──────────┘     └─────────────┘     └─────────────┘
                       ▲                                     │
                       │                              Stop Hook fires
                       │          ┌─────────────┐           │
                       └──────────│ Hook script │◀──────────┘
                      POST /response  (bash)
```

**direct mode (--no-tmux):**
```
┌──────────┐     ┌──────────────────────────────────────────┐
│ Telegram │────▶│              Bridge (Python)              │
│ Webhook  │     │  ┌────────────────────────────────────┐  │
└──────────┘     │  │         DirectWorker               │  │
                 │  │  ┌──────────┐      ┌────────────┐  │  │
                 │  │  │  stdin   │─JSON▶│   Claude   │  │  │
                 │  │  │  (pipe)  │      │ subprocess │  │  │
                 │  │  │          │◀JSON─│            │  │  │
                 │  │  │  stdout  │      └────────────┘  │  │
                 │  │  └──────────┘                      │  │
                 │  └────────────────────────────────────┘  │
      ◀──────────│                                          │
   Response      └──────────────────────────────────────────┘
```

### Message Flow: tmux Mode

1. **Telegram webhook** receives user message
2. **Bridge** routes message to target worker's tmux session
3. **tmux send-keys** types the message into Claude's terminal
4. **Claude** processes and generates response
5. **Stop Hook fires** when Claude finishes responding
6. **Hook script** reads transcript, POSTs to `/response` endpoint
7. **Bridge** sends response to Telegram

### Message Flow: Direct Mode

1. **Telegram webhook** receives user message
2. **Bridge** writes JSON message to worker's stdin pipe:
   ```json
   {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Hello"}]}}
   ```
3. **Claude subprocess** processes and streams JSON events to stdout:
   ```json
   {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello! How can I help?"}]}}
   {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " I'm Claude..."}}
   {"type": "result", ...}
   ```
4. **Reader thread** buffers text from `assistant` and `content_block_delta` events
5. **On `result` event** - accumulated text is sent to Telegram
6. No hook needed - bridge handles everything directly

### Event Handling (Like Happy)

The bridge handles Claude's JSON streaming events similar to the Happy codebase:

| Event Type | Action |
|------------|--------|
| `assistant` | Extract text from message content, buffer it |
| `content_block_delta` | Extract delta text, append to buffer |
| `result` | **Send buffered text to Telegram**, clear buffer |
| `error` | Log error, include in response |

Key insight: We buffer during streaming, only send on `result`. This prevents flooding Telegram with partial messages.

### When to Use Each Mode

| Mode | Use Case | Pros | Cons |
|------|----------|------|------|
| **tmux (default)** | Production, debugging | Can attach to session, visible history, hook-based | Terminal overhead, needs hook setup |
| **direct (--no-tmux)** | Lightweight, embedded | No tmux needed, cleaner integration, no hooks | Can't attach to see session, process-per-worker |

### CLI Usage

```bash
# tmux mode (default)
./claudecode-telegram.sh run

# Direct mode
./claudecode-telegram.sh run --no-tmux
```

## Future Features (Planned)

### Inter-Worker Messaging (Decentralized Discovery)

**Status:** Partial (tmux mode only via direct tmux send-keys)

**Design philosophy:** Bridge provides discovery only; workers communicate directly using the provided protocol. The bridge does NOT route messages between workers - it only tells workers how to reach each other. This means:
- **No manager visibility:** Private worker-to-worker conversations stay private
- **Direct P2P communication:** Workers talk to each other without bridge involvement
- **Protocol flexibility:** Each worker advertises how to reach it (tmux, pipe, etc.)

**Current state:**
- **tmux mode:** Workers can use `tmux send-keys -t claude-<node>-<worker> "message" Enter` directly
- **Direct mode:** No solution yet (planned: named pipes)

**Planned design - Discovery endpoint:**

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
      "send_example": "tmux send-keys -t claude-prod-alice 'your message here' Enter"
    },
    {
      "name": "bob",
      "protocol": "pipe",
      "address": "/tmp/claudecode-telegram/bob/in.pipe",
      "send_example": "echo 'your message here' > /tmp/claudecode-telegram/bob/in.pipe"
    }
  ]
}
```

**Protocol types:**

| Protocol | Address Format | How to Send | Mode |
|----------|---------------|-------------|------|
| `tmux` | Session name | `tmux send-keys -t <address> "message" Enter` | tmux only |
| `pipe` | Named pipe path | `echo "message" > <address>` | Both modes |

**Recommended: Named pipes as unified protocol**

Named pipes (FIFOs) work in both tmux and direct modes:
```bash
# Bridge creates on worker startup
mkfifo /tmp/claudecode-telegram/<worker>/in.pipe

# Worker A sends to Worker B
echo "Hey bob, can you review PR #42?" > /tmp/claudecode-telegram/bob/in.pipe

# Worker B reads (poll or inotifywait)
cat /tmp/claudecode-telegram/<worker>/in.pipe
```

**Why this design:**
- Workers collaborate without manager overhead
- Standard Unix mechanism, no custom protocol
- Works consistently in both tmux and direct modes
- Bridge stays simple - just discovery, no message routing

**Missing tests:**
- `test_worker_discovery_endpoint` - GET /workers returns worker list
- `test_worker_pipe_creation` - pipes created on worker startup
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
- PORT now derived from node name: prod=8081, dev=8082, test=8095
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
| `PORT=8081` (default) | Local setup, hook builds `http://localhost:8081/response` |
| `BRIDGE_URL=http://host.docker.internal:8081` | Docker containers |
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

**Allowed extensions:**
- Docs: `.md`, `.txt`, `.rst`, `.pdf`
- Data: `.json`, `.csv`, `.yaml`, `.yml`, `.toml`, `.ini`, `.cfg`, `.xml`, `.log`, `.sql`, `.patch`, `.diff`
- Code: `.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.go`, `.rs`, `.java`, `.kt`, `.swift`, `.rb`, `.php`, `.c`, `.cpp`, `.h`, `.hpp`, `.sh`, `.html`, `.css`, `.scss`

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
- **Same inbox system** - files stored in `/tmp/claudecode-telegram/<session>/inbox/`
- **Automatic cleanup** - files removed when worker is offboarded

**Message format to Claude:**
```
Manager sent file: document.pdf (1.5 MB, application/pdf)
Path: /tmp/claudecode-telegram/worker/inbox/abc123.pdf
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
- `~/learnings/` - Team learnings
- `~/team-playbook.md` - Team playbook (read-only)
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

Previously, `--port=1789` was silently ignored (falling back to node config), causing confusing errors like "Port 8081 is already in use" when you specified a different port.

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
- Fixed malformed log output "on :8080" → "on port 8080"
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
- Port ownership verification: always check before killing (prod=8081, dev=8082, test=8095)
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
- `/lee hello` routes message directly to lee (one-off, no focus change)
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
- **Image inbox moved to /tmp**: Images now stored in `/tmp/claudecode-telegram/<session>/inbox/` instead of `~/.claude/telegram/sessions/<session>/inbox/`
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
- Team playbook management is external (e.g., `~/team-playbook.md`) - not bridge's responsibility

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
  - Images downloaded to `/tmp/claudecode-telegram/<worker>/inbox/` (ephemeral)
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
- Extension validation: Only .jpg, .jpeg, .png, .gif, .webp, .bmp allowed
- Size limit: 20MB max (Telegram's limit)
- Inbox directories use 0o700 permissions, session-namespaced

**Usage:**
```
# Manager sends image in Telegram
[photo attachment with optional caption]

# Worker receives
Manager sent image: /tmp/claudecode-telegram/worker/inbox/abc123.jpg
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
| `/restart` | `/relaunch` |
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
- Team playbook managed externally (e.g., `~/team-playbook.md`) - bridge doesn't persist

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
- **Derived ports**: prod=8081, dev=8082, test=8095 (override with `--port`)

**Usage:**
```bash
# Start nodes (PORT derived from node name)
NODE_NAME=prod ./claudecode-telegram.sh --no-sandbox run    # port 8081
NODE_NAME=dev ./claudecode-telegram.sh --no-sandbox run     # port 8082

# Stop specific node
./claudecode-telegram.sh --node dev stop

# Clean stale chat_id (fixes wrong admin)
./claudecode-telegram.sh --node prod clean

# Status of all nodes
./claudecode-telegram.sh --all status
```

**Recommended node configurations:**
| Node | Token | Sandbox | Port | Purpose |
|------|-------|---------|------|---------|
| test | TEST_BOT_TOKEN | `--no-sandbox` | 8095 | Automated tests (fast, no Docker) |
| prod | PROD_BOT_TOKEN | `--no-sandbox` | 8081 | Production (performance) |
| dev | DEV_BOT_TOKEN | `--no-sandbox` | 8082 | Development |
| sandbox | TEST_BOT_TOKEN | `--sandbox` | 8080 | Untrusted/experimental code |

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
