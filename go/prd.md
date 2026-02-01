# ClaudeCode Telegram Go Rewrite - PRD

## Project overview
Rewrite the current claudecode-telegram bridge (Python + Bash) as a single Go binary that preserves core behavior and operator UX while improving maintainability, testability, and deployment simplicity. The Go binary will handle Telegram webhooks, worker lifecycle, tmux routing, and hook responses without external dependencies.

## Goals
- 10/10 clean code: readable, minimal, deterministic, and well-tested.
- Single binary: no Python/Bash runtime required at runtime.
- Standard library only: no third-party Go packages.

## MVP features (Phase 1)
Use the existing Python/Bash feature set as the source of truth for mapping and parity, with the fixes below.

1) Telegram bridge + webhook server
- HTTP server with webhook endpoint and optional secret verification.
- Telegram API client (sendMessage, sendChatAction, sendPhoto, sendDocument, getFile).
- Handlers: webhook, /response, /notify.
- Mapping: `bridge.py` (telegram_api, HTTP handler, webhook secret).

2) Admin gating and state model (RAM-only)
- First chat to message becomes admin unless ADMIN_CHAT_ID is set.
- In-memory state only; no persistent DB/state beyond tmux.
- Mapping: `bridge.py` (admin_chat_id, state dict).

3) Worker lifecycle + tmux integration
- /hire <name>: create tmux session and register worker.
- /end <name>: terminate tmux session and cleanup inbox.
- /team: list workers and focused worker.
- /focus <name>: set active worker.
- /progress: show active worker state (running/idle/paused as available).
- /pause: send Escape to tmux to interrupt current worker.
- /relaunch: restart Claude in existing session.
- Mapping: `bridge.py` (tmux helpers, command handlers).

4) Message routing + reply context
- Route manager messages to focused worker.
- Direct worker routing via /<worker> alias (one-off or focus change on bare /<worker>).
- @all broadcast to all workers.
- Reply-to-message routing: if the manager replies to a worker message, route to that worker.
- Response prefix: "<b>worker:</b>" and safe HTML handling.
- Mapping: `bridge.py` (route_message, command parsing, response formatting).

5) Hook processing and response handling
- Receive worker responses via /response POSTs from the bash hook.
- No transcript polling; the hook extracts the response and posts directly.
- Split long responses to fit Telegram 4096-char limit with safe boundaries.
- Typing indicators: sendChatAction loop while worker is pending, stop on response.
- Mapping: `bridge.py` (hook handler, split_message).

6) File inbox + incoming attachments (MVP scope)
- Accept incoming images/documents; download to ~/.claude/telegram/sessions/<worker>/inbox.
- Pass file path + metadata to worker.
- Cleanup on worker end.
- Mapping: `bridge.py` (download_telegram_file, ensure_inbox_dir, cleanup_inbox).

## Phase 2 features
1) Outgoing media tags
- Parse [[image:/path|caption]] and [[file:/path|caption]] tags from worker output.
- Enforce allowlist, blocked extensions, size limit, and path sandbox.
- Mapping: `bridge.py` (parse_image_tags, parse_file_tags, send_photo/document).

2) Sandbox mode (Docker isolation)
- CLI flags/env for --sandbox, --sandbox-image, --mount/--mount-ro.
- Controlled mount set, read-only root, reduced privileges.
- Mapping: `bridge.py` + `claudecode-telegram.sh` (sandbox flags/env).

3) Multi-node management and CLI parity
- Node directories, per-node ports, start/stop/status.
- Parity with current shell UX (run, status, hook install).
- Mapping: `claudecode-telegram.sh` (node management + CLI).

4) Dynamic bot commands and reserved-name validation
- Update Telegram command list when workers change.
- Reject reserved names at hire time.
- Mapping: `bridge.py` (BOT_COMMANDS, RESERVED_NAMES).

5) Operational polish
- Structured logs, JSON output option, verbose/quiet flags.
- Graceful shutdown handling and PID tracking.
- Mapping: `claudecode-telegram.sh` + `bridge.py` (logging, SIGTERM diagnostics).

## Architecture (packages, file structure)
Single module with internal packages to keep a clean boundary; no external deps.

Simplified structure (5 internal packages + cmd entrypoint):
- `go/cmd/cctg/main.go`
  - CLI entrypoint, config loading, starts HTTP server.
- `go/internal/app/`
  - Wiring, lifecycle, state, and dependency setup.
- `go/internal/server/`
  - HTTP handlers: webhook, /response, /notify, and middleware.
- `go/internal/telegram/`
  - Telegram API client, DTOs, message formatting, splitting, typing indicator loop.
- `go/internal/tmux/`
  - tmux wrapper: create, send, list, kill, pause, relaunch.
- `go/internal/files/`
  - Inbox management, file download, cleanup.

## CLI commands
- `cctg serve` - start the HTTP server and Telegram webhook processing.
- `cctg hook` - invoke the Claude Code Stop hook integration (used by the bash hook).

## Configuration (env vars)
- `TELEGRAM_BOT_TOKEN` (required)
- `PORT` (default 8080)
- `ADMIN_CHAT_ID` (optional; auto-learn on first message)
- `TELEGRAM_WEBHOOK_SECRET` (optional)
- `SESSIONS_DIR` (default ~/.claude/telegram/sessions)
- `TMUX_PREFIX` (default "claude-")

## LOC estimates (target ~950 total)
- `go/cmd/cctg` ~50 LOC
- `go/internal/app` ~150 LOC
- `go/internal/server` ~250 LOC
- `go/internal/telegram` ~200 LOC
- `go/internal/tmux` ~150 LOC
- `go/internal/files` ~150 LOC

## TDD approach (test requirements)
- All new features require tests; tests are written before or alongside implementation.
- Unit tests for: command parsing, message splitting, path validation, reply-to routing.
- Integration tests for: webhook request handling, tmux command execution (via test doubles), file download flow (local fixtures), Telegram API request payloads.
- E2E tests for: /hire -> /focus -> message -> /response -> Telegram response.
- Standard library `testing` only; no external test frameworks.

## Success criteria
- Feature parity with Phase 1 list and passing test suite.
- Single Go binary with zero external runtime dependencies.
- No third-party Go modules; `go mod` contains only standard library usage.
- Clean, maintainable package boundaries with clear responsibilities.

## Non-goals
- Building a full UI or web dashboard.
- Persisting data beyond tmux and in-memory state.
- Changing the user-facing command set or behavior in Phase 1.
- Supporting non-Telegram transports.
- Introducing external Go libraries or runtime services.
