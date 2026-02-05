# claudecode-telegram Product Specification (v0.19.0)

## Overview
- MUST provide a Telegram bot plus an HTTP bridge that routes manager messages to multiple workers and returns worker responses to Telegram.
- MUST keep the Telegram bot token inside the bridge process and allow workers/hooks/adapters to operate without the token.

## Telegram Bot Commands
### Primary commands
- MUST implement `/team` to list all workers, the focused worker, each worker's availability, and each worker's backend.
- MUST implement `/focus <name>` to set the focused worker and persist the focus.
- MUST implement `/progress` to report focused worker status including pending, backend, online/ready state, and mode.
- MUST implement `/learn [topic]` to prompt the focused worker with a Problem/Fix/Why template (topic optional).
- MUST implement `/pause` to interrupt the focused worker and clear pending state.
- MUST implement `/relaunch` to restart the focused worker for its backend.
- MUST implement `/settings` to show configuration with secrets redacted and sandbox status.
- MUST implement `/hire <name>` to create a worker and set focus to it.
- MUST implement `/end <name>` to offboard a worker and remove its resources.
- MUST accept `/command@botname` by stripping the `@botname` suffix before routing.

### Dynamic worker shortcuts
- MUST register bot commands dynamically so each worker name becomes a slash command.
- MUST treat `/<worker>` with no message as a focus switch to that worker.
- MUST treat `/<worker> <message>` as a message to that worker and set focus to it.

### Blocked commands
- MUST reject the following commands with a notice that interactive commands are not supported: `/mcp`, `/help`, `/config`, `/model`, `/compact`, `/cost`, `/doctor`, `/init`, `/login`, `/logout`, `/memory`, `/permissions`, `/pr`, `/review`, `/terminal`, `/vim`, `/approved-tools`, `/listen`.

### Worker naming rules
- MUST normalize worker names to lowercase and strip to `a-z`, `0-9`, and `-` only.
- MUST reject reserved names: `team`, `focus`, `progress`, `learn`, `pause`, `relaunch`, `settings`, `hire`, `end`, `all`, `start`, `help`.

## CLI Commands & Flags
### Commands
- MUST implement `run` to start the bridge and (unless disabled) the tunnel and webhook.
- MUST implement `stop` to stop a node including bridge, tunnel, and tmux sessions.
- MUST implement `restart` to restart bridge and tunnel without killing tmux sessions.
- MUST implement `clean` to remove node admin/chat_id files so admin can re-register.
- MUST implement `status` to show node status (and JSON when requested).
- MUST implement `webhook <url>` to set webhook for the node.
- MUST implement `webhook info` to show current webhook status.
- MUST implement `webhook delete` to remove the webhook.
- MUST implement `hook install` to install the Stop hook.
- MUST implement `hook uninstall` to remove the Stop hook.
- MUST implement `hook test` to send a test message to the most recent chat_id for the node.
- MUST implement `help` to show usage, commands, flags, env vars, and exit codes.

### Global flags
- MUST implement `-h`, `--help` to show help.
- MUST implement `-V`, `--version` to show CLI version.
- MUST implement `-n`, `--node <name>` to target a specific node.
- MUST implement `--all` to target all nodes (stop/status only).
- MUST implement `-p`, `--port <port>` to set bridge port.
- MUST implement `--no-tunnel` to skip tunnel and webhook setup.
- MUST implement `--tunnel-url <url>` to use an existing tunnel URL.
- MUST implement `--headless` to disable interactive prompts.
- MUST implement `-q`, `--quiet` to suppress non-error output.
- MUST implement `-v`, `--verbose` for debug output.
- MUST implement `--json` to emit JSON output for status.
- MUST implement `--no-color` to disable ANSI colors.
- MUST implement `--env-file <path>` to source env vars before parsing other flags.
- MUST implement `-f`, `--force` to skip prompts/overwrite during destructive actions.
- MUST implement `--sandbox` to enable Docker sandbox for workers.
- MUST implement `--no-sandbox` to disable Docker sandbox.
- MUST implement `--sandbox-image <img>` to set Docker image name.
- MUST implement `--mount <path>` and `--mount-ro <path>` to add extra mounts.

### Node selection and defaults
- MUST resolve target node by priority: `--node` flag, then `NODE_NAME`, then auto-detect running nodes.
- MUST prompt interactively when multiple nodes are running unless `--headless` or non-tty, in which case it MUST error unless `--node` or `--all` is provided.
- MUST default `run` to node `prod` when no nodes are running.
- MUST map default ports by node name: `prod=8081`, `dev=8082`, `test=8095`, otherwise `8080`.
- MUST sanitize node names to lowercase alphanumeric plus hyphen.
- MUST derive tmux prefix as `claude-<node>-` for node isolation.

### Run/tunnel behavior
- MUST require `tmux` and `python3` for `run`.
- MUST require `cloudflared` when tunnel is not disabled and no tunnel URL is provided.
- MUST auto-install the Stop hook on first run if it is missing.
- MUST set `TELEGRAM_BOT_TOKEN`, `PORT`, `SESSIONS_DIR`, and `TMUX_PREFIX` when launching the bridge.
- MUST start the tunnel (or use provided URL), wait for URL, and set Telegram webhook with retries.
- MUST clean up bridge/tunnel processes if webhook setup fails.
- MUST run tunnel watchdog when tunnel is used and restart it on failure or unreachable state.
- MUST re-set webhook after tunnel restart and notify chats via `/notify` on failures.

### Status diagnostics
- MUST detect orphan bridge/tunnel processes not owned by any node and surface them in status output.
- MUST warn if multiple running nodes share the same bot ID (webhook conflict risk).
- MUST warn when tmux env mismatches detected for a node and suggest restart.
- MUST warn when hooks are stale relative to `settings.json` mtime.

### Hook install/uninstall behavior
- MUST install the Stop hook by copying `hooks/send-to-telegram.sh` to `~/.claude/hooks/`.
- MUST install `hooks/forward-to-bridge.py` alongside the hook.
- MUST update `~/.claude/settings.json` using `jq` when available, or create a minimal file if missing.
- MUST uninstall by removing the hook file and removing the hook entry from `settings.json` when possible.

### Exit codes
- MUST use exit code `0` for success.
- MUST use exit code `1` for runtime errors.
- MUST use exit code `2` for invalid usage or unsupported flags.
- MUST use exit code `3` for missing required configuration (e.g., missing token).
- MUST use exit code `4` for missing dependencies.

## Backend System
### Backend protocol
- MUST define a backend interface with: `name`, `is_interactive`, `start_cmd()`, `send()`, and `is_online()`.
- MUST keep all backend routing behind a single backend registry.

### Implementations
- MUST implement `ClaudeBackend` as interactive (tmux-based) with `claude --dangerously-skip-permissions` and tmux send-keys.
- MUST implement `CodexBackend` as non-interactive using `hooks/codex-tmux-adapter.py`.
- MUST implement `GeminiBackend` as non-interactive using `hooks/gemini-adapter.py`.
- MUST implement `OpenCodeBackend` as non-interactive using `hooks/opencode-adapter.py`.
- MUST treat non-interactive backends as stateless workers with no tmux session.

### Hire syntax and backend selection
- MUST accept `/hire <name>` with default backend `claude`.
- MUST accept `/hire <name> --backend <name>` to select a backend.
- MUST accept legacy `/hire <name> --codex` to select `codex`.
- MUST accept backend-prefix syntax (e.g., `codex-alice`, `gemini-bob`) and map to the corresponding backend.
- MUST reject unknown backends and list available backends.
- MUST send a welcome message on hire that includes the bridge URL and inter-worker discovery instructions.

### Per-session backend state
- MUST store backend selection at `SESSIONS_DIR/<worker>/backend`.
- MUST write the `chat_id` file before sending the welcome message for non-interactive workers.

## Environment Variables
### Bridge (bridge.py)
- MUST require `TELEGRAM_BOT_TOKEN`.
- MUST accept `PORT` (default `8080`).
- MUST accept `TELEGRAM_WEBHOOK_SECRET` (optional).
- MUST accept `SESSIONS_DIR` (default `~/.claude/telegram/sessions`).
- MUST accept `TMUX_PREFIX` (default `claude-`).
- MUST accept `BRIDGE_URL` (default `http://localhost:<PORT>`).
- MUST accept `ADMIN_CHAT_ID` (optional preset admin).
- MUST accept `SANDBOX_ENABLED` (`1`/`0`).
- MUST accept `SANDBOX_IMAGE` (default `claudecode-telegram:latest`).
- MUST accept `SANDBOX_MOUNTS` (comma-separated, supports `ro:` prefix).

### CLI (claudecode-telegram.sh)
- MUST accept `TELEGRAM_BOT_TOKEN`.
- MUST accept `ADMIN_CHAT_ID` (optional).
- MUST accept `TUNNEL_URL` (optional).
- MUST accept `TELEGRAM_WEBHOOK_SECRET` (optional).
- MUST accept `PORT` (optional).
- MUST accept `NODE_NAME` (optional).
- MUST accept `SANDBOX_ENABLED` (optional).
- MUST accept `SANDBOX_IMAGE` (optional).

### Hook (hooks/send-to-telegram.sh)
- MUST read `BRIDGE_URL`, `PORT`, `TMUX_PREFIX`, and `SESSIONS_DIR`.
- MUST honor `TMUX_FALLBACK=0` to disable tmux capture fallback.
- MUST honor `BRIDGE_SESSION` when running in Docker (tmux unavailable).
- MUST prefer tmux session env values and fall back to shell env values.

### Test harness (test.sh)
- MUST accept `TEST_BOT_TOKEN` (required for tests).
- MUST accept `TEST_CHAT_ID` (optional, enables full e2e validation).
- MUST accept `TEST_PORT` (optional, default `8095`).
- MUST allow `FAST=1` and `FULL=1` for test mode selection.

## Persistence & File Layout
### Per-node
- MUST store node data under `~/.claude/telegram/nodes/<node>/`.
- MUST store `pid` (main script PID), `bridge.pid`, and `tunnel.pid`.
- MUST store `bridge.log` and `tunnel.log` when applicable.
- MUST store `tunnel_url`, `port`, `bot_id`, and `bot_username`.
- MUST store `last_chat_id` and `last_active` (bridge persistence).
- MUST allow an `admin_chat_id` file to exist and allow `clean` to remove it when present.
- MUST store sessions under `~/.claude/telegram/nodes/<node>/sessions/` when launched via CLI.

### Per-session
- MUST store per-worker state under `SESSIONS_DIR/<worker>/`.
- MUST store `chat_id` (reply target) and `pending` (timestamp) as 0600 files.
- MUST store `backend` to record the selected backend.
- MUST store exec-backend metadata files (e.g., `codex_session_id`, `codex_session_id.lock`, `gemini.lock`, `opencode.lock`).

### Temp and inbox
- MUST store incoming files in `/tmp/claudecode-telegram/<node>/<worker>/inbox/`.
- MUST create per-worker named pipes at `/tmp/claudecode-telegram/<node>/<worker>/in.pipe`.
- MUST derive `<node>` from `TMUX_PREFIX` by stripping `claude-` and trailing hyphens; use `default` when empty.

### Permissions
- MUST create node and session directories with `0700`.
- MUST create per-session files `chat_id` and `pending` with `0600`.
- MUST create inbox directories with `0700` and downloaded files with `0600`.
- MUST create named pipes with `0600`.

## HTTP Endpoints
### `GET /`
- MUST return `200 OK` with body `Claude-Telegram Multi-Session Bridge`.

### `POST /` (Telegram webhook)
- MUST accept Telegram Update JSON.
- MUST validate `X-Telegram-Bot-Api-Secret-Token` when `TELEGRAM_WEBHOOK_SECRET` is set and return `403` on mismatch.
- MUST return `200 OK` for handled updates.

### `POST /response`
- MUST accept JSON body with `session` and `text` fields.
- MUST accept optional fields `escape` (boolean) and `source` (`codex`, `gemini`, `opencode`).
- MUST return `400` when `session` or `text` is missing.
- MUST return `404` when the session has no `chat_id` file.
- MUST route the response to Telegram and clear the session's `pending` file.
- MUST HTML-escape text when `escape` is true or when `source` is `codex`.
- MUST parse `[[image:...]]` and `[[file:...]]` tags and send media accordingly.

### `POST /notify`
- MUST accept JSON body with `text`.
- MUST return `400` when `text` is missing.
- MUST send the text to all known chat IDs and return `200` on success.

### `GET /workers`
- MUST return JSON `{ "workers": [ ... ] }`.
- MUST include entries with `name`, `protocol`, `address`, and `send_example`.
- MUST return an empty list when no workers exist.

## Message Routing
### Admin and access control
- MUST accept messages only from the admin chat ID.
- MUST auto-register the first sender as admin when `ADMIN_CHAT_ID` is not set.
- MUST restore admin from `last_chat_id` on restart when `ADMIN_CHAT_ID` is unset.
- MUST ignore non-admin messages without responding.

### Command routing
- MUST route recognized commands to the appropriate command handler.
- MUST pass unknown `/` commands through to the focused worker (unless blocked).
- MUST update dynamic worker commands on hire/end.

### Mentions and broadcasts
- MUST route `@all <message>` to all online workers without changing focus.
- MUST route `@<name> <message>` to the named worker without changing focus when the worker exists.

### Reply routing
- MUST route replies to bot messages prefixed with `<name>:` back to that worker.
- MUST route replies to non-worker messages to the focused worker.
- MUST prepend reply context using:
  - `Manager reply:`
  - `Context (your previous message):`

### Attachments (Telegram to worker)
- MUST require a focused worker for attachments and prompt the admin to use `/focus` when none is set.
- MUST prepend captions to forwarded attachment messages when captions are present.

### Worker shortcuts
- MUST treat `/<worker>` as a focus shortcut and `/<worker> <message>` as focus + send.

## Inter-Worker Communication
- MUST expose `GET /workers` for discovery of active workers and protocols.
- MUST create a named pipe for every worker at `/tmp/claudecode-telegram/<node>/<worker>/in.pipe`.
- MUST run a pipe reader thread per worker to forward messages to that worker's backend.
- MUST forward each non-empty line written to the pipe as a worker message.
- MUST remove the named pipe and stop its reader when the worker is offboarded.
- MUST report tmux workers with `protocol=tmux` and exec workers with `protocol=pipe`.

## Security
- MUST keep `TELEGRAM_BOT_TOKEN` inside the bridge only.
- MUST reject webhook requests with invalid secret when `TELEGRAM_WEBHOOK_SECRET` is set.
- MUST fail closed in the hook if required env vars are missing (to prevent cross-node leakage).
- MUST enforce admin-only access.
- MUST serialize tmux sends per session to prevent interleaving.
- MUST serialize exec-backend sessions with per-worker lock files.
- MUST auto-clear `pending` after 10 minutes to avoid stuck busy state.

## Image & Document Handling
### Incoming (Telegram to worker)
- MUST download photos and image documents to `/tmp/claudecode-telegram/<node>/<worker>/inbox/`.
- MUST download non-image documents to the same inbox and forward metadata (filename, size, mime type, path).
- MUST prepend any caption text to the forwarded message.
- MUST reject files over 20 MB.
- MUST report download failures to the admin.
- MUST clean inbox contents when a worker is offboarded.

### Outgoing (worker to Telegram)
- MUST recognize `[[image:/path|caption]]` and `[[file:/path|caption]]` tags in worker responses.
- MUST ignore tags inside fenced code blocks and inline code.
- MUST preserve escaped tags such as `\[[image:...]]`.
- MUST send photos with `sendPhoto` and documents with `sendDocument`.

### Outgoing validation
- MUST allow image extensions: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`.
- MUST restrict image paths to `/tmp`, `SESSIONS_DIR`, or the current working directory.
- MUST allow document extensions from the configured allowlist (docs/data/code).
- MUST block document extensions containing secrets/keys (e.g., `.pem`, `.key`, `.p12`, `.pfx`, `.crt`, `.cer`, `.der`, `.jks`, `.keystore`, `.kdb`, `.pgp`, `.gpg`, `.asc`).
- MUST block sensitive filenames (e.g., `.env*`, `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, `id_rsa`, `id_ed25519`, `credentials`, `kubeconfig`).
- MUST allow documents from any filesystem path (no path restriction beyond allowlist/blocklist).
- MUST reject files over 20 MB.
- MUST send a text failure notice when a media send fails.

## Hook System
### Stop hook (send-to-telegram.sh)
- MUST read Claude's transcript path from the Stop hook JSON input.
- MUST determine the session name from tmux or `BRIDGE_SESSION` when in Docker.
- MUST read config from tmux session env first and fall back to shell env.
- MUST exit without sending if `TMUX_PREFIX`, `SESSIONS_DIR`, or both `BRIDGE_URL`/`PORT` are missing.
- MUST extract assistant text after the last user message from the transcript, retrying up to 5 seconds.
- MUST fall back to tmux capture (last 500 lines) when transcript extraction fails, unless `TMUX_FALLBACK=0`.
- MUST append a short warning when tmux fallback is used.
- MUST forward responses to `POST /response` with a 5-second timeout and clear the `pending` file.

### Forwarder (forward-to-bridge.py)
- MUST convert markdown to Telegram-compatible HTML (bold, italic, inline code, fenced code blocks).
- MUST POST JSON `{"session": <name>, "text": <html>}` to `/response`.

### Exec adapters
- MUST provide adapters for Codex, Gemini, and OpenCode that invoke their CLIs in non-interactive mode.
- MUST serialize per-worker adapter execution with lock files when supported.
- MUST POST adapter responses to `/response` with `escape=true` and a `source` field.
- MUST persist per-worker session identifiers when the backend supports session resume (Codex).

## Operational Behavior
- MUST start an HTTP server on `0.0.0.0:<PORT>` with `SO_REUSEADDR` enabled.
- MUST create `SESSIONS_DIR` with secure permissions on startup.
- MUST discover existing tmux sessions and exec workers on startup.
- MUST restore `last_active` focus when the worker still exists.
- MUST restore admin from `last_chat_id` if available and `ADMIN_CHAT_ID` is unset.
- MUST set Telegram bot commands on startup and after hire/end.
- MUST send a startup message on first admin interaction when no prior startup notification was sent.
- MUST send a startup notification to the last known chat ID when available.
- MUST send a shutdown notification to all known chat IDs on SIGINT/SIGTERM.
- MUST show typing indicators while a worker request is pending.
- MUST add the 👀 reaction when a message is accepted (tmux prompt empty or exec backend).
- MUST split long responses at 4096 characters using safe boundaries and chain parts via `reply_to_message_id`.
- MUST prefix all worker responses with `<b>{worker}:</b>` and use HTML parse mode.

## Sandbox Mode
- MUST run workers in Docker when `SANDBOX_ENABLED=1` or `--sandbox` is set.
- MUST mount `~` to `/workspace` (rw) and set working directory to `/workspace`.
- MUST mount `SESSIONS_DIR` and `/tmp/claudecode-telegram/<node>` into the container.
- MUST apply extra mounts from `SANDBOX_MOUNTS`, supporting `ro:` for read-only.
- MUST set container env: `BRIDGE_URL`, `PORT`, `TMUX_PREFIX`, `SESSIONS_DIR`, `BRIDGE_SESSION`, `TMUX_FALLBACK=1`.
- MUST use `http://host.docker.internal:<PORT>` as `BRIDGE_URL` when no explicit `BRIDGE_URL` is provided.
- MUST add `host.docker.internal` mapping on Linux hosts.
- MUST stop the container when a worker is offboarded or relaunched.

## Appendix A: Telegram Message Format Contracts
### Parse mode and response types
- MUST send **command/admin replies** as plain text (no `parse_mode`).
- MUST send **worker responses** via `sendMessage` with `parse_mode="HTML"`.
- MUST send media captions without `parse_mode`.

### /team response template
```
Your team:
Focused: <active|(none)>
Workers:
- <name> (<status-list>)
- <name> (<status-list>)
```
Where `<status-list>` is a comma-separated list built in this order:
1) `focused` (only if this worker is the active one)
2) `working` or `available` (based on pending state)
3) `backend=<backend>`

If there are no workers:
```
No team members yet. Add someone with /hire <name>.
```

### /progress response template
When no focused worker:
```
No one assigned. Who should I talk to? Use /team or /focus <name>.
```
When focused worker is missing:
```
Can't find them. Check /team for who's available.
```
When focused worker exists:
```
Progress for focused worker: <name>
Focused: yes
Working: <yes|no>
Backend: <backend>
Online: <yes|no>
Ready: <yes|no>
Needs attention: worker app is not running. Use /relaunch.
Mode: <mode>
```
Where `<mode>` is either `tmux` or `<backend> exec (stateless)`.
The `Needs attention` line is included only when the tmux session exists but `claude` is not running.

### /settings response template
```
claudecode-telegram v<VERSION>
They'll stay on your team.

Bot token: <redacted-token>
Admin: <admin-chat-id|(auto-learn)>
Webhook verification: <redacted-secret|(disabled)>
Team storage: <SESSIONS_DIR.parent>

Team state
Focused worker: <active|(none)>
Workers: <comma-list-or-(none)>

Sandbox: enabled (Docker isolation)
Image: <SANDBOX_IMAGE>
Default mount: <HOME> → /workspace
Extra mounts:
  <host> → <container>
  <host> → <container> (ro)

Note: Workers run in containers with access
only to mounted directories. System paths
outside mounts are not accessible.
```
If sandbox is disabled, replace the sandbox section with:
```
Sandbox: disabled (direct execution)
Workers run with full system access.
```
`Extra mounts` is included only when `SANDBOX_EXTRA_MOUNTS` is non-empty.
Redaction rules:
- If value is empty: `(not set)`
- If length ≤ 8: `***`
- Else: first 4 chars + `...` + last 4 chars

### /hire responses
Success:
```
<Name> is added and assigned. They'll stay on your team.
```
Errors:
```
Usage: /hire <name>
Name must use letters, numbers, and hyphens only.
Cannot use "<name>" - reserved command. Choose another name.
Could not hire "<name>". <error-from-backend>
```

### /end responses
Success:
```
<Name> removed from your team.
```
Errors:
```
Offboarding is permanent. Usage: /end <name>
Could not offboard "<name>". <error-from-backend>
```

### /focus responses
Success:
```
Now talking to <Name>.
```
Errors:
```
Usage: /focus <name>
Could not focus "<name>". <error-from-backend>
```

### /learn prompt
With topic:
```
What did you learn about <topic> today? Please answer in Problem / Fix / Why format:
Problem: <what went wrong or was inefficient>
Fix: <the better approach>
Why: <root cause or insight>
```
Without topic:
```
What did you learn today? Please answer in Problem / Fix / Why format:
Problem: <what went wrong or was inefficient>
Fix: <the better approach>
Why: <root cause or insight>
```

### Other exact error messages
- Blocked commands:
```
/<cmd> is interactive and not supported here.
```
- No focused worker (text or attachments):
```
Needs decision - No focused worker. Use /focus <name> first.
```
- Attachment download failures:
```
Needs decision - Could not download image. Try again or send as file.
Needs decision - Could not download file. Try again.
```
- Route fallbacks:
```
No one assigned. Your team: <names>
Who should I talk to?
No team members yet. Add someone with /hire <name>.
Can't find <name>. Check /team for who's available.
<Name> is offline. Try /relaunch.
Could not send to <Name>. Try /relaunch.
No one's online to share with.
```
- Pause/relaunch:
```
<Name> is paused. I'll pick up where we left off.
Bringing <Name> back online...
Could not relaunch "<name>". <error-from-backend>
```
- No active worker (pause/relaunch):
```
No one assigned.
```

### Reaction contract
- MUST call Telegram `setMessageReaction` with payload:
```
{"chat_id": <chat_id>, "message_id": <msg_id>, "reaction": [{"type":"emoji","emoji":"👀"}]}
```
- Reaction is sent only when:
  - the incoming message has a `message_id`, AND
  - send succeeds, AND
  - backend is non-interactive OR `tmux_prompt_empty()` returns true within 0.5s.
  - `tmux_prompt_empty()` polls `tmux capture-pane` every 0.1s for a line matching `^❯\s*$`.

### 4096-char split + reply chaining
- Worker responses are prefixed with HTML:
```
<b><name>:</b>
<text>
```
- Before splitting, the bridge reserves `len(name) + 30` characters to account for prefix/HTML.
- Split priority (best-first): blank line (`\n\n`) → newline (`\n`) → space (` `) → hard cut.
- Each split point must be past halfway of the max length; otherwise fall back to the next rule.
- Each chunk is `rstrip()`’d, and remaining text is `lstrip()`’d.
- Every chunk is sent as `<b>name:</b>\nchunk` with **no part numbering**.
- When multiple chunks are sent, each later chunk sets `reply_to_message_id` to the previous chunk’s message id (chaining).

### Worker response prefix and media captions
- Text prefix: `<b>name:</b>\n` (HTML mode).
- Image caption: `name: <caption>` or `name:` when empty.
- File caption: `name: <caption>` or `name:` when empty.
- Media failure messages:
  - `name: [Image failed: /path]`
  - `name: [File failed: /path]`

### Startup and shutdown notifications
Startup message (sent on first admin interaction, and on restart when last_chat_id exists):
```
I'm online and ready.
Team: <comma-list>
Focused: <active>
No workers yet. Hire your first long-lived worker with /hire <name>.
Sandbox: <HOME> → /workspace
```
Include only the lines that apply (team list, focused worker, or no-workers message; sandbox line only when enabled).
Shutdown message (SIGINT/SIGTERM):
```
Going offline briefly. Your team stays the same.
```

## Appendix B: Telegram Update Handling
### Update types processed
- Only `update["message"]` is processed.
- All other update types (`edited_message`, `callback_query`, `channel_post`, etc.) are ignored (logged once if received).

### Command parsing rules
- A command is any message starting with `/`.
- The command token is lowercased and may include an `@botname` suffix, which is stripped.
- The remainder (if any) after the first whitespace is the argument string.
- Commands are case-insensitive because the command token is lowercased.

### Dynamic worker command rules
- If the command is `/name` and `name` matches a registered worker:
  - If no argument: focus that worker and reply `Now talking to <Name>.`
  - If an argument is present: focus the worker, optionally send `Now talking to <Name>.` if focus changed, then route the message.

### Mentions and replies
- `@all <message>` broadcasts to all online workers without changing focus.
- `@name <message>` routes to `name` without changing focus (only if `name` exists).
- Reply routing:
  - If replying to a bot message that starts with `name:`, route to that worker.
  - Otherwise route to the focused worker.
  - Reply context is prepended exactly:
```
Manager reply:
<reply_text>

Context (your previous message):
<context_text>
```

### Photo/document/file handling
- Text for routing is `message.text` or, if absent, `message.caption`.
- Photos:
  - Use `message.photo` and select the largest by `file_size`.
  - If a `document` has `mime_type` starting with `image/`, it is treated as an image.
  - Download flow:
    1) `POST https://api.telegram.org/bot<TOKEN>/getFile` with `{"file_id": "<id>"}`
    2) Use returned `file_path` to download from `https://api.telegram.org/file/bot<TOKEN>/<file_path>`
  - Saved path: `/tmp/claudecode-telegram/<node>/<worker>/inbox/<uuid>.<ext>`
  - Routed message to worker:
```
<caption-if-any>

Manager sent image: <local_path>
```
- Documents (non-image):
  - Use `message.document` fields: `file_id`, `file_name`, `file_size`, `mime_type`.
  - Download flow is identical to photos.
  - Routed message to worker:
```
<caption-if-any>

Manager sent file: <file_name> (<size>, <mime_type>)
Path: <local_path>
```

### Supported message types and ignored content
- Supported: text, caption, photo, document.
- Ignored: stickers, audio, voice, video, location, polls, etc. (no response).
- Non-admin messages are ignored entirely (no response), except the first message which establishes admin.

## Appendix C: File Allowlists & Blocklists
### Exact image extensions (outgoing)
```
[".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]
```

### Exact allowed document extensions (outgoing)
```
[
  ".md", ".txt", ".rst", ".pdf",
  ".json", ".csv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
  ".log", ".sql", ".patch", ".diff",
  ".py", ".js", ".ts", ".jsx", ".tsx",
  ".go", ".rs", ".java", ".kt", ".swift",
  ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
  ".sh", ".html", ".css", ".scss"
]
```

### Exact blocked extensions (outgoing documents)
```
[
  ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
  ".jks", ".keystore", ".kdb", ".pgp", ".gpg", ".asc"
]
```

### Exact blocked filenames (case-insensitive)
```
[
  ".env", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
  "id_rsa", "id_ed25519", "id_dsa", "credentials", "kubeconfig"
]
```
Also blocked: any filename starting with `.env` (e.g., `.env.local`).

### Image path restrictions
- Outgoing image paths MUST be under one of:
  - `/tmp`
  - `SESSIONS_DIR`
  - current working directory (`Path.cwd()`)
- Document paths have **no path restriction** beyond allowlist/blocklist.

## Appendix D: Hook & Adapter Protocol
### Stop hook (hooks/send-to-telegram.sh)
**Input schema (JSON on stdin):**
```
{"transcript_path": "/path/to/transcript.jsonl", ...}
```
Only `transcript_path` is used; all other fields are ignored.

**Session name resolution:**
1) `tmux display-message -p '#{session_name}'`
2) If missing and `BRIDGE_SESSION` is set (Docker), use `${TMUX_PREFIX:-claude-}${BRIDGE_SESSION}`

**Config resolution order (fail-closed):**
1) Read `BRIDGE_URL`, `TMUX_PREFIX`, `SESSIONS_DIR`, `PORT` from tmux session env
2) Fallback to shell env
3) Exit if `TMUX_PREFIX` or `SESSIONS_DIR` missing, or if both `BRIDGE_URL` and `PORT` missing
4) If `SESSION_NAME` does not start with `TMUX_PREFIX`, exit silently (not our session)

**Bridge endpoint selection:**
- If `BRIDGE_URL` is set: `${BRIDGE_URL%/}/response`
- Else: `http://localhost:${PORT}/response`

**Transcript parsing algorithm:**
1) Find last line containing `"type":"user"` in the transcript.
2) From that line onward, extract assistant messages:
   - `jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")'`
3) Retry up to 10 times with 0.5s delay (total 5s) for race conditions.
4) If no last user line, clear `pending` and exit. If transcript is missing, exit without sending.

**Fallback (tmux capture) when transcript parsing fails:**
- Enabled by default; disable with `TMUX_FALLBACK=0`.
- Capture last 500 lines: `tmux capture-pane -S -500`.
- Extract text between lines starting with `● ` and the next prompt (`❯`) or divider (`───`).
- Skip lines:
  - UI markers (`·`, `✶`, `✻`, `⏵`, `⎿`)
  - “stop hook”, “Whirring”, “Herding”, “Mulling”, “Recombobulating”, “Cooked for”, “Saut”
  - single word headers like `analysis:` (regex `^[a-z]+:$`)
  - lines containing `Tip:`
- If the current capture is a feedback prompt (“How is Claude doing this session”), fall back to the last non-feedback response.
- When fallback is used, append:
```

⚠️ May be incomplete. Retry if needed.
```

**Forwarding and retries:**
- POSTed via `hooks/forward-to-bridge.py` using:
  - `timeout 5 python3 forward-to-bridge.py <tmpfile> <bridge_session> <bridge_endpoint>`
- Forwarding runs in the background; pending file is removed immediately after spawn.

### forward-to-bridge.py (markdown → HTML)
Conversion rules (in order):
1) Extract fenced code blocks: ```lang\ncode``` → placeholder
2) Extract inline code: `code` → placeholder
3) Escape HTML: `&`, `<`, `>`
4) Bold: `**text**` → `<b>text</b>`
5) Italic: `*text*` → `<i>text</i>` (non-`**`)
6) Restore code blocks:
   - With language: `<pre><code class="language-<lang>">...</code></pre>`
   - Without language: `<pre>...</pre>`
7) Restore inline code: `<code>...</code>`
No other markdown is converted (headings, links, lists are left as-is).

**POST body to bridge:**
```
{"session":"<worker>","text":"<html>"}
```

### codex-tmux-adapter.py
**Invocation:**
```
python3 hooks/codex-tmux-adapter.py <worker> <message> <bridge_url> [sessions_dir] [workdir]
```
**CLI command used:**
```
codex exec --json --yolo [-C <workdir>] [resume <session_id>] <message>
```
**Session reuse:**
- Session ID stored in `SESSIONS_DIR/<worker>/codex_session_id`
- Lock file: `SESSIONS_DIR/<worker>/codex_session_id.lock` (fcntl lock)

**Response parsing:**
- JSONL events; concatenates all `item.completed` with `item.type == "agent_message"` into the response.
- Captures session id from `thread.started.thread_id`.

**POST body to bridge:**
```
{"session":"<worker>","text":"<raw>","source":"codex","escape":true}
```

### gemini-adapter.py
**Invocation:**
```
python3 hooks/gemini-adapter.py <worker> <message> <bridge_url> [sessions_dir]
```
**CLI command used:**
```
gemini -p "<message>" --output-format json
```
**Timeout:** 300s.

**POST body to bridge:**
```
{"session":"<worker>","text":"<raw>","source":"gemini","escape":true}
```

### opencode-adapter.py
**Invocation:**
```
python3 hooks/opencode-adapter.py <worker> <message> <bridge_url> [sessions_dir]
```
**CLI command used:**
```
opencode run "<message>" --format json
```
**Timeout:** 300s.

**POST body to bridge:**
```
{"session":"<worker>","text":"<raw>","source":"opencode","escape":true}
```

## Appendix E: Backend CLI Commands
### ClaudeBackend (tmux)
- `start_cmd()`:
```
claude --dangerously-skip-permissions
```
- `send()`:
  - `tmux send-keys -t <session> -l "<text>"`
  - sleep 0.2s
  - `tmux send-keys -t <session> Enter`
- `is_online()`:
  - tmux session exists AND
  - current pane command contains `claude` OR a child process named `claude` is running under the pane PID

### CodexBackend (non-interactive)
- `start_cmd()`:
```
echo 'Codex worker ready (non-interactive)'
```
- `send()`:
  - `python3 hooks/codex-tmux-adapter.py <worker> "<text>" <bridge_url> <sessions_dir>`
  - runs via `subprocess.Popen`, stdout/stderr discarded
- `is_online()`: always `true`
- Pipe delivery path: `pipe_reader` → `_forward_pipe_message` → `WorkerManager.send` → adapter → `codex exec`

### GeminiBackend (non-interactive)
- `start_cmd()`:
```
echo 'Gemini worker ready (non-interactive)'
```
- `send()`:
  - `python3 hooks/gemini-adapter.py <worker> "<text>" <bridge_url> <sessions_dir>`
- `is_online()`: always `true`
- Pipe delivery path: `pipe_reader` → `_forward_pipe_message` → `WorkerManager.send` → adapter → `gemini -p`

### OpenCodeBackend (non-interactive)
- `start_cmd()`:
```
echo 'OpenCode worker ready (non-interactive)'
```
- `send()`:
  - `python3 hooks/opencode-adapter.py <worker> "<text>" <bridge_url> <sessions_dir>`
- `is_online()`: always `true`
- Pipe delivery path: `pipe_reader` → `_forward_pipe_message` → `WorkerManager.send` → adapter → `opencode run`

### Message flow (exec backends)
```
Telegram -> bridge -> backend.send()
  -> adapter CLI (codex/gemini/opencode)
    -> POST /response (escape=true, source=<backend>)
      -> bridge sendMessage(parse_mode=HTML)
```

### tmux env vars exported per worker
These are set via `tmux set-environment` on the worker session:
```
BRIDGE_URL=<bridge_url>
PORT=<port>
SESSIONS_DIR=<sessions_dir>
TMUX_PREFIX=<tmux_prefix>
WORKER_BACKEND=<backend>
```

## Appendix F: CLI Output Contracts
### Exit codes (complete)
- `0`: success
- `1`: runtime error
- `2`: invalid usage / unsupported flag
- `3`: missing required configuration (e.g., missing token)
- `4`: missing dependency

### Status output (plain text)
Single node (no `--json`):
```
Node: <node> [running|stopped]
  port:     <port>
  tunnel:   <tunnel_url>
  sessions: <N> running | none
            - <session_name>
            - <session_name> [port dir prefix stale-hooks]
  ⚠ env mismatch: restart node to fix
  ⚠ stale-hooks: restart Claude (/exit) to reload settings.json
  hook:     installed | not installed
  bot:      online (@<username>, id:<id>) | error | not configured
  webhook:  set | not set | mismatch (pointing to different URL)
            actual:   <webhook_url>
            expected: <tunnel_url>
```
Include only the lines that apply (port/tunnel only when running; mismatch details only when webhook differs).

All nodes (`--all`):
```
All Nodes

<node block>

<node block>

⚠ CONFLICT: <count> nodes running with same bot (id:<bot_id>)
  Running: <node1> <node2> ...
  Only ONE node receives webhook. Others miss messages.
  Fix: Use different TELEGRAM_BOT_TOKEN per node, or stop extras.
```

Orphan detection (always appended):
```
⚠ ORPHAN PROCESSES DETECTED
  tunnel: PID <pid> (port <port>) - kill <pid>
  bridge: PID <pid> - kill <pid>
  Fix: kill orphan processes or restart node
```

### Status output (JSON)
Each node emits one JSON object (even with `--all`, it prints one per line):
```
{"node":"<node>","running":true|false,"port":"<port>","sessions":["<tmux_session>",...],"hook":true|false,"settings":true|false,"token":true|false,"bot":"<bot_username>","webhook":"<webhook_url>"}
```

### Common error messages (exact)
- Missing token:
```
error: TELEGRAM_BOT_TOKEN not set
```
- Port in use:
```
error: Port <port> is already in use
→ Stop the other process or use: --port <other-port>
```
- Missing dependencies:
```
error: tmux not installed
→ brew install tmux
error: python3 not installed
error: cloudflared not installed
→ brew install cloudflared (or use --no-tunnel)
```
- Multiple nodes in headless mode:
```
error: Multiple nodes running. Specify with --node <name> or --all
→ Running nodes: <node1> <node2> ...
```
- Invalid node name:
```
error: Invalid node name: <value>
```
- Unknown command/flag:
```
error: Unknown command: <cmd>
→ ./claudecode-telegram.sh --help
error: Unknown flag: <flag>
```

### Webhook output formats
Set:
```
Setting webhook for node '<node>': <url>
Webhook configured
```
Info:
```
Node: <node>
URL:     <url>
Pending: <count>
```
Or:
```
Node: <node>
No webhook configured
```
Delete:
```
Webhook deleted
```

## Appendix G: Sequence Diagrams (ASCII)
### /hire flow
```
Telegram -> POST / (bridge)
  -> CommandRouter.cmd_hire
    -> WorkerManager.hire
      -> tmux new-session (interactive) OR non-interactive metadata (non-interactive backend)
      -> export_hook_env (tmux env vars)
      -> start_cmd or docker run
      -> (tmux non-sandbox only) send-keys "2" then Enter
      -> send welcome message to worker
    -> reply: "<Name> is added and assigned. They'll stay on your team."
    -> update_bot_commands
```

### Message flow (manager -> worker -> Telegram)
```
Telegram -> POST / (bridge)
  -> CommandRouter.route_message
    -> set pending + typing indicator
    -> WorkerManager.send
      -> tmux send-keys OR adapter CLI
Claude -> Stop hook
  -> send-to-telegram.sh
    -> forward-to-bridge.py
      -> POST /response
        -> send_response_to_telegram (HTML, prefix, split)
          -> Telegram sendMessage
```

### Worker-to-worker (discovery + pipe)
```
Worker A -> GET /workers
Bridge -> returns [{"name","protocol","address","send_example"}...]
Worker A -> echo "msg" > /tmp/claudecode-telegram/<node>/<workerB>/in.pipe
Bridge pipe reader -> _forward_pipe_message
  -> WorkerManager.send -> backend send (tmux/exec)
```

### Shutdown flow
```
SIGINT/SIGTERM -> graceful_shutdown
  -> send_shutdown_message
    -> Telegram sendMessage to all known chat_ids
  -> exit(0)
```

## Appendix H: State Machine
### Worker states
```
creating -> online -> pending -> online -> pending -> ...
```
- `creating`: /hire in progress (tmux session created or exec metadata written).
- `online`: worker exists and is ready (`tmux` + `claude` running, or exec backend).
- `pending`: a manager message has been sent; `pending` file exists.
- `pending` auto-clears after 10 minutes; transitions back to `online`.
- `/pause`, `/relaunch`, `/end`, or `/response` also clear pending.

### Admin states
```
unknown -> learned -> persisted
```
- `unknown`: no admin yet, `ADMIN_CHAT_ID` unset.
- `learned`: first incoming chat becomes admin; saved to `last_chat_id`.
- `persisted`: admin restored from `last_chat_id` on restart (when `ADMIN_CHAT_ID` unset).

## Roadmap
- MUST document any unimplemented features here; none are defined in the current codebase.
