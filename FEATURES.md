# claudecode-telegram Features (from code)

> Source files: `bridge.py`, `claudecode-telegram.sh`, `hooks/send-to-telegram.sh`, `test.sh`

## Telegram bot commands

### Primary commands
- `/team` — show team list + focused worker + status (b8ffadb)
- `/focus <name>` — set focused worker (b8ffadb)
- `/progress` — detailed status of focused worker (b8ffadb)
- `/learn [topic]` — prompt focused worker for learnings (Problem/Fix/Why) (7a11589)
- `/pause` — pause focused worker (Escape) (b8ffadb)
- `/relaunch` — restart focused worker (b8ffadb)
- `/settings` — show system configuration (secrets redacted) (b8ffadb)
- `/hire <name>` — create new worker (b8ffadb)
- `/end <name>` — offboard worker (b8ffadb)

### Dynamic worker shortcuts
- `/<worker>` with **no** message → switch focus to that worker (1b5266f)
- `/<worker> <message>` → route message to worker **and** switch focus (1b5266f)
- Bot command list is updated dynamically to include worker names (958c890)

### Blocked commands
These slash commands are rejected with a message:
- `/mcp`, `/help`, `/config`, `/model`, `/compact`, `/cost`, `/doctor`, `/init`, `/login`, `/logout`, `/memory`, `/permissions`, `/pr`, `/review`, `/terminal`, `/vim`, `/approved-tools`, `/listen` (958c890)

### Worker naming rules
- Names are lowercased and stripped to `a-z`, `0-9`, and `-` (238c58b)
- Reserved names are rejected (commands/aliases + `all`, `start`, `help`) (1b5266f)

## CLI commands (claudecode-telegram.sh)

### Commands
- `run` — start bridge + tunnel + webhook (default) (238c58b)
- `stop` — stop node (bridge, tunnel, tmux sessions) (238c58b)
- `restart` — restart bridge/tunnel (preserves tmux sessions) (c96aaf0)
- `clean` — remove admin/chat_id files for node (df9de52)
- `status` — show node status (238c58b)
- `webhook <url>` — set webhook URL (238c58b)
- `webhook info` — show current webhook info (238c58b)
- `webhook delete` — delete current webhook (238c58b)
- `hook install` — install Stop hook (238c58b)
- `hook uninstall` — uninstall Stop hook (df9de52)
- `hook test` — send test message to Telegram (238c58b)
- `help` — show help (238c58b)

### Global flags
- `-h`, `--help` — help (238c58b)
- `-V`, `--version` — version (238c58b)
- `-n`, `--node <name>` — target node (fb276e8)
- `--all` — all nodes (stop/status only) (fb276e8)
- `-p`, `--port <port>` — bridge port (238c58b)
- `--no-tunnel` — skip tunnel/webhook (fdae0a9)
- `--tunnel-url <url>` — use existing tunnel (8d0a939)
- `--headless` — non-interactive mode (68cdb08)
- `-q`, `--quiet` — suppress non-error output (238c58b)
- `-v`, `--verbose` — debug output (238c58b)
- `--json` — JSON output (status) (238c58b)
- `--no-color` — disable colors (238c58b)
- `--env-file <path>` — load env vars from file (238c58b)
- `-f`, `--force` — overwrite/remove without prompt (238c58b)
- `--sandbox` — run workers in Docker (2e1c548)
- `--no-sandbox` — run workers directly (2e1c548)
- `--sandbox-image <img>` — Docker image (2e1c548)
- `--mount <path>` — extra mount (host:container or same path) (df9de52)
- `--mount-ro <path>` — extra read-only mount (df9de52)
- `--no-tmux`, `--direct` — direct mode: bypass tmux, use Claude JSON streaming (NEW)

### Node selection & defaults
- Target resolution: `--node` > `NODE_NAME` > auto-detect running nodes (fb276e8)
- If multiple nodes are running: interactive prompt (headless mode requires `--node` or `--all`) (68cdb08)
- Default node for `run` when none are running: `prod` (fb276e8)
- Default ports by node name: `prod=8081`, `dev=8082`, `test=8095`, otherwise `8080` (override with `PORT`/`--port`) (df9de52)
- Node names are sanitized to lowercase alphanumeric + hyphen (fb276e8)

### Run / tunnel behavior
- `run` auto-installs the hook if missing (238c58b)
- If no `--no-tunnel` or `--tunnel-url`, starts `cloudflared`, waits for a trycloudflare URL, and sets webhook with retries (238c58b)
- Cleans up bridge/tunnel processes if webhook setup fails (238c58b)
- Tunnel watchdog auto-restarts on failure/unreachable, re-sets webhook with backoff, and notifies chats via `/notify` (656db47)

### Status diagnostics
- Detects orphan `cloudflared`/`bridge.py` processes not owned by any node (dfb028c)
- Warns if multiple running nodes share the same bot ID (webhook conflict) (54ae529)
- Flags tmux env mismatches (port/sessions/prefix) and suggests node restart (54ae529)
- Flags stale hooks when `settings.json` changed after Claude started (restart Claude to reload) (54ae529)

### Hook install/uninstall details
- `hook install` copies hook + helper and updates `~/.claude/settings.json` (uses `jq` if available, otherwise creates a minimal file) (eb48b27)
- `hook uninstall` removes hook + helper and removes the settings entry (when `jq` is available) (df9de52)
- `hook test` uses the most recent `chat_id` file for the node (238c58b)
- `webhook delete` prompts unless `--force` or `--headless` (68cdb08)

## Environment variables

### Bridge (bridge.py)
- `TELEGRAM_BOT_TOKEN` (required) — Telegram bot token (958c890)
- `PORT` — HTTP port (default: 8080) (958c890)
- `TELEGRAM_WEBHOOK_SECRET` — webhook secret verification token (86228c8)
- `SESSIONS_DIR` — sessions root (default: `~/.claude/telegram/sessions`) (238c58b)
- `TMUX_PREFIX` — tmux session prefix (default: `claude-`) (5895f72)
- `BRIDGE_URL` — explicit bridge URL (optional) (86228c8)
- `ADMIN_CHAT_ID` — pre-set admin chat ID (5895f72)
- `SANDBOX_ENABLED` — enable Docker sandbox (`1`/`0`) (2e1c548)
- `SANDBOX_IMAGE` — Docker image (default: `claudecode-telegram:latest`) (2e1c548)
- `SANDBOX_MOUNTS` — extra mounts (comma-separated, supports `ro:` prefix) (2e1c548)
- `DIRECT_MODE` — direct mode: bypass tmux, use JSON streaming (`1`/`0`, default `0`) (NEW)

### CLI (claudecode-telegram.sh)
- `TELEGRAM_BOT_TOKEN` (required) (238c58b)
- `ADMIN_CHAT_ID` (optional) (fb276e8)
- `TUNNEL_URL` (optional) (8d0a939)
- `TELEGRAM_WEBHOOK_SECRET` (optional) (86228c8)
- `PORT` (optional) (238c58b)
- `NODE_NAME` (optional) (fb276e8)
- `SANDBOX_ENABLED` (optional) (2e1c548)
- `SANDBOX_IMAGE` (optional) (2e1c548)
- `SANDBOX_MOUNTS` (derived from flags) (df9de52)
- `DIRECT_MODE` (optional, or from `--no-tmux`/`--direct` flag) (NEW)

### Hook (hooks/send-to-telegram.sh)
- `BRIDGE_URL` — full bridge URL (preferred) (86228c8)
- `PORT` — bridge port (fallback if `BRIDGE_URL` unset) (86228c8)
- `TMUX_PREFIX` — required session prefix (5895f72)
- `SESSIONS_DIR` — required session files root (c1e24c3)
- `TMUX_FALLBACK` — set `0` to disable tmux capture fallback (45cd0de)

## Hook behavior (send-to-telegram.sh)
- Reads config from tmux session env first, then falls back to process env (1bb71d2)
- Extracts assistant text from transcript after last user message (retries up to 5s for race conditions) (958c890)
- Optional tmux capture fallback (last 500 lines) when transcript extraction fails; appends a brief warning (45cd0de)
- Forwards to bridge asynchronously with a 5s timeout and clears pending state (68cdb08)

### Tests (test.sh)
- `TEST_BOT_TOKEN` — test bot token (fb276e8)
- `TEST_CHAT_ID` — real Telegram chat ID (for e2e) (8d0a939)
- `TEST_PORT` — override test port (default: 8095) (8d0a939)

## Persistence files & directories

### Per-node (in `~/.claude/telegram/nodes/<node>/`)
- `pid` — main process PID (843e3f4)
- `bridge.pid` — bridge PID (fb276e8)
- `tunnel.pid` — tunnel PID (fb276e8)
- `tunnel.log` — tunnel output log (fb276e8)
- `tunnel_url` — current tunnel URL (238c58b)
- `port` — bridge port (fb276e8)
- `bot_id` — bot id cached from Telegram (dfb028c)
- `bot_username` — bot username cached from Telegram (dfb028c)
- `last_chat_id` — last admin chat ID (bridge persistence) (9e38f92)
- `last_active` — last focused worker name (bridge persistence) (9e38f92)
- `bridge.log` — bridge output log (5b718a8)
- `admin_chat_id` — referenced by `clean` (removed if present) (df9de52)

### Per-session (in `SESSIONS_DIR/<worker>/`)
- `pending` — timestamp while a request is pending (238c58b)
- `chat_id` — chat id to reply to (958c890)
- `inbox/` — incoming files (images/documents), 0700 (3adaf3f)

### Temp / inbox
- `/tmp/claudecode-telegram/<worker>/inbox/` — inbox root for downloads (3adaf3f)

## HTTP endpoints
- `POST /` — Telegram webhook (optional secret header check) (958c890)
- `POST /response` — hook → bridge response forwarding (86228c8)
- `POST /notify` — internal notifications to all known chat IDs (e.g., tunnel watchdog) (656db47)
- `GET /` — health string: `Claude-Telegram Multi-Session Bridge` (958c890)

## Message routing rules

### Admin / access control
- First user to message becomes admin (unless `ADMIN_CHAT_ID` is set) (5895f72)
- Non-admins are silently rejected (no responses) (86228c8)

### Command routing
- `/hire` creates worker and sets focus (b8ffadb)
- `/focus` sets focus (b8ffadb)
- `/team` lists all workers and their status (b8ffadb)
- `/progress` shows focused worker status (b8ffadb)
- `/pause` sends Escape to focused worker and clears pending (b8ffadb)
- `/relaunch` restarts focused worker (Docker or direct) (2e1c548)
- `/settings` shows system config (b8ffadb)
- `/learn [topic]` prompts focused worker (7a11589)
- Slash commands with `@botname` suffix are supported (suffix stripped) (8d0a939)
- Unknown `/` commands are passed through to the worker (not consumed) (238c58b)

### One-off routing
- `@all <message>` broadcasts to all running workers (no focus change) (2761259)
- `@<name> <message>` routes to that worker (no focus change) (238c58b)

### Reply routing
- Reply to a **worker message** (`<name>:` prefix from bot) routes back to that worker (eba3847)
- Reply to any **non-worker** message routes to focused worker (eba3847)
- Reply payload includes explicit context: (eba3847)
  - `Manager reply:` + reply text (eba3847)
  - `Context (your previous message):` + replied-to message (eba3847)

### Worker shortcuts
- `/<worker>` with no message → focus that worker (1b5266f)
- `/<worker> <message>` → route to worker and focus them (1b5266f)

## Security features
- Token isolation: Telegram token stays in bridge only; hook never sees it (86228c8)
- Admin-only access with silent rejection of non-admins (86228c8)
- Webhook secret validation via `TELEGRAM_WEBHOOK_SECRET` (86228c8)
- Hook fails closed if required env vars are missing (prevents cross-node leakage) (1bb71d2)
- Secure permissions: session dirs 0700, session files 0600, inbox dirs 0700 (3adaf3f)
- Per-session locks prevent concurrent tmux sends from interleaving (1bcfd3c)
- Pending auto-timeout (10 minutes) to avoid stuck “busy” (10f3b05)

## Image & document handling

### Incoming (Telegram → worker)
- Photos and image documents are downloaded to inbox (3adaf3f)
- Non-image documents are downloaded to inbox and sent to worker with: (3adaf3f)
  - filename, size (human-readable), mime type, path (3adaf3f)
- Captions on incoming photos/documents are prepended to the forwarded message (3adaf3f)
- Attachments require a focused worker; otherwise bot asks to `/focus` first (3adaf3f)
- Download failures are reported back to the manager (3adaf3f)
- Size limit: 20MB (Telegram limit) (3adaf3f)
- Incoming files stored under `/tmp/claudecode-telegram/<worker>/inbox/` (3adaf3f)
- Auto-cleanup of inbox when worker is offboarded (3adaf3f)

### Outgoing (worker → Telegram)
- `[[image:/path|caption]]` — sends photo via `sendPhoto` (3adaf3f)
- `[[file:/path|caption]]` — sends document via `sendDocument` (3adaf3f)
- Caption optional for both (3adaf3f)
- Tags are ignored in code fences and inline code; escaped tags `\[[image:...]]` are preserved (1bb71d2)

### Outgoing validation
- Images: allowlisted extensions (`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`) (3adaf3f)
- Images: path must be inside `/tmp`, `SESSIONS_DIR`, or current working directory (3adaf3f)
- Documents: allowlisted extensions (docs/data/code); blocked extensions for secrets (3adaf3f)
- Documents: blocked filenames (`.env*`, `.npmrc`, `id_rsa`, etc.) (3adaf3f)
- Size limit: 20MB for images/documents (3adaf3f)
- Documents can be sent from any path (no path restriction beyond allowlist/blocklist) (3adaf3f)
- If sending an image/file fails, bridge posts a text failure notice to chat (3adaf3f)

## Misc behavior
- Telegram responses are split at 4096 chars with safe boundaries (ab30ea3)
- Multipart responses are chained with `reply_to_message_id` (ab30ea3)
- 👀 reaction is added only if Claude accepted the message (empty prompt) (e03cc60)
- Typing indicator is shown while a worker request is pending (958c890)
- Startup notification is sent on first admin interaction and on restart if `last_chat_id` is known (9e38f92)
- Graceful shutdown sends an offline notice to all known chat IDs (8d0a939)
- Admin is restored from `last_chat_id` on restart (if `ADMIN_CHAT_ID` is unset) (9e38f92)
- New workers receive a welcome message with file-tag instructions; direct (non-sandbox) mode auto-accepts the initial confirmation prompt (1b5266f)
- Sandbox mode runs workers in Docker with default mount `~ → /workspace` (2e1c548)
- Optional extra mounts via `--mount` and `--mount-ro` (df9de52)
- Direct mode (`--no-tmux`/`--direct`) runs workers as subprocesses with JSON streaming, bypassing tmux (NEW)
