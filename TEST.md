# Testing Guide

## Test Modes

The test suite has three modes. Each mode runs more tests than the one before it.

| Mode | Command | Time | What it runs |
|------|---------|------|--------------|
| **FAST** | `FAST=1 ./test.sh` | ~10-15s | Unit + CLI tests only |
| **Default** | `./test.sh` | ~2-3 min | FAST + integration tests |
| **FULL** | `FULL=1 ./test.sh` | ~5 min | Default + tunnel tests |

See `AGENTS.md` for workflow rules on when to run each mode.

### FAST Mode (No Bridge, No Network)

FAST mode tests run without a bridge process. They test:
- Python imports and function behavior
- Message formatting and splitting
- CLI flags (`--help`, `--version`, `--node`, `--port`)
- Constants and configuration values
- Concurrency helpers (locks, paste buffer)
- Hook install and uninstall
- Forge Go tests (with `-short` flag)

### Default Mode (Bridge Running Locally)

Default mode starts a bridge on port 8295. It runs all FAST tests plus:
- Bridge startup and health check
- All Telegram commands (`/hire`, `/team`, `/focus`, etc.)
- Admin authorization
- Worker routing (`@mention`, `@all`, reply-to)
- Security (webhook secret, token isolation, file permissions)
- Image and document handling
- `/response` and `/notify` endpoints
- Persistence files
- Guest system endpoints
- Pilot grid endpoints

### FULL Mode

FULL mode runs all Default tests plus:
- Cloudflare tunnel startup
- Webhook configuration with the real Telegram API

## Test Pyramid

```
        /\
       /  \  FULL: Tunnel + Webhook (1 test)
      /----\
     /      \ Default: Bridge + Commands (~92 tests)
    /--------\
   /          \ FAST: Unit + CLI (~392 tests)
  /------------\
```

## Quick Start

```bash
TEST_BOT_TOKEN='your-test-bot-token' ./test.sh
```

## Test Coverage

**Current coverage: ~485 test functions across all modes**

### By Mode

| Mode | Tests | Notes |
|------|-------|-------|
| Unit (FAST) | ~381 | Imports, formatting, helpers, all subsystems |
| CLI (FAST) | ~10 | Flags, commands, webhook, hook coverage |
| Forge Go (FAST) | 1 aggregate | Runs Go test suite internally |
| Integration (Default) | ~92 | Commands, security, routing, endpoints |
| Tunnel (FULL) | 1 | Cloudflare tunnel + webhook setup |

### By Subsystem

| Subsystem | Scope |
|-----------|-------|
| Message formatting | Splitting, HTML, markdown conversion, tables |
| Backend registry | Claude, Codex, Gemini, OpenCode backends |
| Worker lifecycle | Hire, end, restart, pause, naming, state |
| Teleport | SSH foundation, remote dispatch, host propagation |
| Git sync | Repo detection, push/pull state, rsync fallback |
| Media | Image/file tags, validation, size limits |
| Persistence | Registry, pending, session files |
| Concurrency | Locks, paste buffer, bracketed paste, flock |
| Voice | STT transcription, TTS synthesis, auto-TTS |
| Transcript | HTML viewer, search (BM25), pagination, edit diffs |
| Team chat | Indexing, FTS5 search, rewind, reply context |
| Memory | Status, wake-up, recall, failure isolation |
| Transport | Interface, local transport, log file |
| Gmail connector | Import, poll cycle |
| GitHub connector | Import, poll, dedup, sender filtering |
| Guest system | Token, expiry, inbox, send/reply |
| Channels | Create, members, send, cap, expiry, fan-out |
| Relay | Tokens, URL format, auth, send/reply/poll |

## Feature Test Matrix (Backend Coverage)

Each feature must work for both tmux and exec backends. This table tracks coverage.

| Feature | Test | Backend Coverage |
|---------|------|------------------|
| Worker creation | `test_hire_command` | tmux e2e; exec via `test_hire_backend_parsing` |
| Worker survival | `test_tmux_mode_session_stays_alive` | tmux e2e; exec via backend unit tests |
| Message delivery | `test_tmux_mode_message_delivery` | tmux e2e; exec via `test_pipe_forwarding_to_codex` |
| Escape flag | `test_forward_to_bridge_escape_flag` | exec responses (codex/gemini/opencode) |
| @mention routing | `test_mention_routing` | All backends |
| End/kill session | `test_end_command` | All backends |
| Inter-worker messaging | `test_worker_to_worker_pipe` | All backends |
| Image/document handling | `test_incoming_document_e2e` | All backends |

## Complete Test Inventory

> **Total: ~485 test functions**
>
> Update this list when you add new tests.

### Unit Tests (FAST Mode)

These tests run without a bridge. They test Python functions and constants directly.

**Core formatting and splitting:**
- Response prefix and multipart formatting
- Message splitting (short, newlines, hard, HTML-aware)
- Sandbox Docker command generation
- Hook transcript extraction (single, multiple, skip, failure)

**Markdown conversion:**
- `markdown_to_telegram_html` conversion
- `_pipe_tables_to_html` inline markdown and links
- `_wrap_plain_tables` no double-escaping
- Partial `sendRichMessage` failure deduplication
- Forward-to-bridge raw markdown payload

**Backend registry:**
- Registry exists with expected backends
- `get_registered_sessions` includes non-interactive workers

**Worker naming and lifecycle:**
- `/hire` backend parsing (`--codex`, `codex-` prefix)
- `/hire` rejects missing backend binary
- `/restart` rejects missing backend binary
- `/restart` defaults to resume, `--clean` does relaunch
- `/pause` clears pending for codex workers
- `/end` cleans codex session metadata and pipe
- Adapter PID tracking, pause kills adapter, end kills adapter
- Adapter stderr logging
- Poisoned detection via adapter.log
- `compute_state` for non-interactive backends
- Watchdog alert on stuck transition
- `/workers` includes codex exec workers
- `@mention` parsing
- Reserved names rejection
- Bot commands structure and blocked commands list

**Teleport SSH foundation:**
- `_remote_run` and `_remote_copy` helpers
- Host fields in worker registry
- `BRIDGE_URL` auto-detection
- Machines config loading

**Git sync:**
- Git repo detection and project naming
- Bare repos, push/pull state
- rsync fallback

**Remote dispatch (Phase 1):**
- Pane command execution
- Process running checks
- tmux operations on remote hosts

**Call site host propagation (Phase 2):**
- `/team`, `/pause`, `/progress` pass host
- Interactive reply, checkin, watchdog pass host

**Node-derived config:**
- tmux prefix, sessions dir, port, bridge URL by node name

**Media tags:**
- Image and file tag parsing
- `/notify` image tags
- Remote worker image tags (no local validation)

**Persistence functions:**
- File functions, pending set/clear/timeout
- Backend file operations

**Worker registry:**
- Add, remove, bootstrap, corrupt recovery
- Host preservation, registry operations

**Copy improvement:**
- Activity normalization
- Team header formatting
- Watchdog alert copy

**Concurrency:**
- tmux send locks, paste buffer
- Bracketed paste, flock

**Misc behavior:**
- Watchdog alert, extra mounts, checkin note

**File validation:**
- Size limit (50MB maximum)
- Incoming media types and extensions

**Worker discovery:**
- Pipe creation and cleanup
- Liveness check

**send_to_worker abstraction:**
- Backend registry usage
- Missing worker handling
- tmux mode routing

**Voice mode:**
- STT transcription and TTS synthesis
- Voice toggle and auto-TTS

**Transcript viewer:**
- HTML rendering, search (BM25), pagination
- Edit diffs, turn grouping, rewind tokens

**Transcript index:**
- Missing/empty file handling
- Indexing, noise skipping
- FTS5 search, pagination, stats

**Team chat index:**
- Indexing, sender resolution
- FTS5 search, page-for-msg

**Team chat bridge:**
- Rewind team token, rendering
- Search, anchor, reply context

**Memory subcommand:**
- Status, wake-up, recall, failure isolation

**Transport abstraction:**
- Interface, local transport, log file, init selection

**Gmail connector:**
- Import, poll cycle

**Rich message / reply context:**
- Text extraction, reply context formatting

**GitHub connector:**
- Import, gh API usage, dedup
- Poll cycle, sender filtering

**Guest system:**
- Token generation, name generation/validation
- Expiry, inbox operations

**Group channels:**
- Create, members, messages
- Cap, expiry

**Relay guideline links:**
- Tokens, URL format, markdown
- Auth, send/reply/poll

### CLI Tests (FAST Mode)

These tests run `claudecode-telegram.sh` directly. They do not start a bridge.

- `--help` shows usage
- `--version` shows version
- `--node`, `--port`, `--all` flag syntax
- `--no-tunnel`, `--tunnel-url`, `--headless`, `--quiet`, `--verbose` flags
- `--no-color`, `--env-file`, `--sandbox-image`, `--mount`, `--mount-ro` flags
- Default ports by node name
- Unknown command rejection
- Missing token error message
- Hook install and uninstall
- `stop`, `restart`, `clean`, `status` commands
- `--json` flag produces valid JSON
- `webhook info`, `webhook set`, `webhook delete` subcommands
- Node resolution priority (`--node` > `NODE_NAME` > auto-detect)
- Node name sanitization
- Default node = prod when none running
- `--flag=value` syntax

### Integration Tests (Default Mode)

These tests start a bridge on port 8295. They test real HTTP endpoints and Telegram command processing.

**HTTP endpoints:**
- `GET /` health check
- `POST /response` endpoint (valid, missing fields, no chat_id, without pending)
- `POST /notify` endpoint (valid, missing text)
- `GET /workers` endpoint (exists, JSON structure, shows tmux workers, empty state)
- `GET /machines` endpoint structure
- API index returns curated endpoint list (not all routed endpoints)
- 404 for unknown endpoints
- `POST /register` forge registration

**Admin:**
- First user becomes admin
- Auto-learn admin behavior
- `ADMIN_CHAT_ID` preset
- Admin restored from `last_chat_id`
- Non-admin silently rejected

**Bot commands:**
- `/hire` creates worker
- `/team` lists workers
- `/focus` switches worker
- `/progress` shows status
- `/pause` sends Escape
- `/restart` restarts worker
- `/settings` shows config
- `/end` offboards worker
- Dynamic bot command list updates on `/hire` and `/end`

**Worker naming (integration):**
- Reserved names rejected via webhook
- Worker shortcuts and unknown commands

**Routing:**
- `@name` mention routing
- `@all` broadcast
- Reply routing and context
- Explicit context format

**Tmux mode behavior:**
- Session stays alive after creation
- Message delivery to tmux session

**Security:**
- Webhook secret acceptance and validation
- Graceful shutdown notification
- Typing indicator loop
- Token isolation (token not exposed to tmux)
- Directory permissions (0700)
- Session file permissions

**Image/document handling:**
- Inbox directory creation
- Document routing
- Incoming document and image e2e (requires `TEST_CHAT_ID`)
- Response with image tags
- Photo and document without focused worker
- Caption prepended to message
- Download failure notification
- Inbox under `/tmp`
- Inbox cleanup on `/end`
- Document path flexibility
- Blocked filenames
- Send failure notification
- 50MB size limit

**Persistence (integration):**
- `last_chat_id` persistence
- `last_active` persistence

**Hook behavior (integration):**
- Hook env validation
- Checkin hook validation and calls

**Worker discovery (integration):**
- Workers endpoint existence and structure
- tmux workers shown, empty state

**send_to_worker integration:**
- `POST /send` delivers through bridge routing

**Worker-to-worker pipe:**
- Pipe message delivery

**Tmux/process inspection:**
- Process state checks

**export_hook_env guard:**
- Guard conditions

**Pilot grid:**
- Usage, grid endpoint
- Single, multi, all enable
- Nonexistent error

**Guest system (integration):**
- Register, send, inbox
- Status, disconnect, expiry
- Listing, mention routing
- Telegram notification

**Group channel (integration):**
- Create, add members, send, messages
- Nonmember rejection, delete, list
- Telegram `/ch` command
- Guest fan-out

**Relay guideline link (integration):**
- Guide endpoint, auth
- Send, reply, messages
- Guest relay multi-target
- Channel send, filtered list
- Legacy compat

### Tunnel Tests (FULL Mode)

- `test_with_tunnel` — Cloudflare tunnel startup + webhook configuration

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TEST_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TEST_CHAT_ID` | No | `123456789` | Your chat ID for e2e tests |
| `TEST_PORT` | No | `8295` | Bridge port |
| `TEST_PILOT_PORT` | No | `10175` | Pilot server port |
| `TEST_NODE_DIR` | No | `~/.claude/telegram/nodes/test` | Node directory |
| `TEST_FILTER` | No | — | Run only tests that match this string |
| `FAST` | No | — | Set to `1` for unit + CLI tests only |
| `FULL` | No | — | Set to `1` to include tunnel tests |

## Manual Testing

### Simulate a Telegram Webhook

Start the bridge manually:

```bash
TELEGRAM_BOT_TOKEN='...' PORT=8295 python3 bridge.py &
```

Send a simulated message:

```bash
curl -X POST http://localhost:8295 \
  -H "Content-Type: application/json" \
  -d '{
    "update_id": 1,
    "message": {
      "message_id": 1,
      "from": {"id": 123456789, "first_name": "Test"},
      "chat": {"id": 123456789, "type": "private"},
      "date": 1706400000,
      "text": "/team"
    }
  }'
```

### Test with Real Telegram

Start a quick tunnel (random URL each time):

```bash
./claudecode-telegram.sh run
```

Or use a persistent URL:

```bash
./claudecode-telegram.sh run --tunnel-url https://your.domain.com
```

## Test Isolation

Tests run in isolation under `--node test`. This keeps test state separate from production.

| Resource | Test | Production |
|----------|------|------------|
| Node dir | `~/.claude/telegram/nodes/test/` | `~/.claude/telegram/nodes/prod/` |
| Port | 8295 | 8271 |
| tmux prefix | `claude-test-` | `claude-prod-` |
| Session files | `.../nodes/test/sessions/` | `.../nodes/prod/sessions/` |
| PID file | `.../nodes/test/pid` | `.../nodes/prod/pid` |
| Logs | `.../nodes/test/*.log` | `.../nodes/prod/*.log` |
| Bot token | Separate test bot | Production bot |

You can run tests while production is active. The two nodes do not share state.

## Full E2E Test

To test the complete response flow (hook → bridge → Telegram):

```bash
TEST_BOT_TOKEN='...' TEST_CHAT_ID='your-chat-id' ./test.sh
```

When you set `TEST_CHAT_ID`:
- The bridge pre-locks to your chat ID (no auto-learn).
- Test messages use your real chat ID.
- The response test sends an actual message to your Telegram.

## CI Integration

```yaml
# GitHub Actions example
- name: Run tests
  env:
    TEST_BOT_TOKEN: ${{ secrets.TELEGRAM_TEST_TOKEN }}
  run: ./test.sh
```

## Writing New Tests

Add test functions to `test.sh`:

```bash
test_my_feature() {
    info "Testing my feature..."

    local result
    result=$(send_message "/mycommand")

    if [[ "$result" == "OK" ]]; then
        success "My feature works"
    else
        fail "My feature failed"
    fi
}
```

Add the function call to the correct runner:
- `run_unit_tests()` — tests that do not need the bridge
- `run_cli_tests()` — CLI-only tests
- `run_integration_tests()` — tests that need the bridge running
- `run_full_tests()` — tests that need the tunnel

Then update this file:
- Add the test to the **Complete Test Inventory** section above.
- Update the **Test Coverage** counts if they changed.

## Missing Tests

### Critical (Mode Parity)

All critical mode parity tests are implemented. ✅

### Important (No Test)

These features have no test coverage:

| Feature | Description |
|---------|-------------|
| Multipart response chaining | Reply chain for multipart messages (`reply_to_message_id`) |

### Nice to Have

Lower priority tests for edge cases:

| Feature | Description |
|---------|-------------|
| Worker crash recovery | Process crash detection and cleanup |
| Concurrent pipe writes | Multiple workers writing to the same pipe |
| Pipe permissions | Named pipe has correct permissions (0o600) |
| Path traversal protection | Prevent `../` in worker names for inbox paths |
