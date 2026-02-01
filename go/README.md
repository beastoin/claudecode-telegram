# Claude Code Telegram - Go Edition

A clean, minimal Go rewrite of claudecode-telegram. Single binary, zero external dependencies.

## Status

**Phase 1 MVP: Complete** | **Phase 2: Complete** | **Phase 3: Complete** (367 tests)

| Feature | Status |
|---------|--------|
| Webhook server + Telegram API | ✅ |
| Admin gating | ✅ |
| Worker lifecycle (/hire, /end, /team, /focus, /pause, /progress, /relaunch, /settings, /learn) | ✅ |
| Message routing (focused, /<worker>, @all, reply-to) | ✅ |
| Hook handling (/response endpoint) | ✅ |
| Full hook replacement (markdown→HTML, transcript parsing, tmux fallback) | ✅ |
| Typing indicators | ✅ |
| Message splitting (4096 chars) | ✅ |
| File inbox (images/documents) | ✅ |
| Message reactions (👀) | ✅ |
| Outgoing media ([[image:...]], [[file:...]]) | ✅ |
| Reserved name validation | ✅ |
| Startup notification | ✅ |
| Webhook registration (`cctg webhook`) | ✅ |
| Hook install (`cctg hook install`) | ✅ |
| Dynamic bot commands | ✅ |
| Multi-node support (--node) | ✅ |
| JSON logging (--json) | ✅ |
| Tunnel integration (`cctg tunnel`) | ✅ |
| Sandbox mode (--sandbox) | ✅ |
| /notify endpoint (broadcast to all chats) | ✅ |
| Graceful shutdown (broadcast offline message) | ✅ |

See [progress.txt](progress.txt) for detailed checklist.

## Quick Start

### Build

```bash
go build -o cctg ./cmd/cctg/
```

### Run Server

```bash
# Using flags
./cctg serve --token "YOUR_BOT_TOKEN" --admin "YOUR_CHAT_ID" --port 8080

# Using environment variables
export TELEGRAM_BOT_TOKEN="YOUR_BOT_TOKEN"
export ADMIN_CHAT_ID="YOUR_CHAT_ID"
./cctg serve
```

### Set Up Webhook

You need a public URL for Telegram to send updates. Use cloudflared or ngrok:

```bash
# Terminal 1: Start tunnel
cloudflared tunnel --url http://localhost:8080

# Terminal 2: Register webhook with Telegram
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${TUNNEL_URL}/webhook"

# Terminal 3: Start server
./cctg serve
```

### Install Claude Hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/cctg hook --url http://localhost:8080/response --session $CLAUDE_SESSION_NAME"
          }
        ]
      }
    ]
  }
}
```

Or use environment variables in the tmux session:
```bash
export BRIDGE_URL="http://localhost:8080/response"
export SESSION_NAME="worker_name"
```

## Commands

| Command | Description |
|---------|-------------|
| `/hire <name> [dir]` | Create a new worker |
| `/end <name>` | Remove a worker |
| `/team` | List all workers |
| `/focus <name>` | Set who gets your messages |
| `/focus` | Clear focus |
| `/pause` | Send Escape to focused worker |
| `/progress` | Check worker status |
| `/relaunch` | Restart Claude in session |
| `/learn [topic]` | Ask worker about lessons learned |
| `/settings` | Show current configuration |

## Routing

| Input | Routes to |
|-------|-----------|
| `/alice hello` | Worker "alice" directly |
| `@all status?` | All workers (broadcast) |
| Reply to `[bob]...` | Worker "bob" |
| Plain message | Focused worker |

## File Attachments

Send images or documents to your focused worker. Files are saved to the worker's inbox directory and the path is passed to Claude.

## Architecture

```
cmd/cctg/main.go          # CLI entry point
internal/
├── app/app.go            # App wiring, Config
├── files/files.go        # Inbox management
├── server/handler.go     # HTTP handlers
├── telegram/
│   ├── client.go         # Telegram API
│   └── split.go          # Message splitting
└── tmux/session.go       # tmux management
```

## Configuration

| Flag | Env Var | Default | Description |
|------|---------|---------|-------------|
| `--token` | `TELEGRAM_BOT_TOKEN` | required | Bot token from @BotFather |
| `--admin` | `ADMIN_CHAT_ID` | required | Your Telegram chat ID |
| `--node` | `NODE_NAME` | prod | Node name (prod/dev/test/custom) |
| `--port` | `PORT` | per-node | HTTP server port |
| `--prefix` | `TMUX_PREFIX` | per-node | tmux session prefix |
| `--json` | - | false | JSON structured logging |

## Multi-Node Support

Run multiple isolated instances with different configs:

```bash
# Production (port 8081, prefix claude-prod-)
cctg serve --node prod --token "$PROD_TOKEN" --admin "$ADMIN"

# Development (port 8082, prefix claude-dev-)
cctg serve --node dev --token "$DEV_TOKEN" --admin "$ADMIN"

# Test (port 8095, prefix claude-test-)
cctg serve --node test --token "$TEST_TOKEN" --admin "$ADMIN"
```

| Node | Port | Prefix | Sessions Dir |
|------|------|--------|--------------|
| prod | 8081 | claude-prod- | ~/.claude/telegram/nodes/prod/sessions/ |
| dev | 8082 | claude-dev- | ~/.claude/telegram/nodes/dev/sessions/ |
| test | 8095 | claude-test- | ~/.claude/telegram/nodes/test/sessions/ |

## Testing

```bash
go test ./...           # Run all tests
go test -v ./...        # Verbose output
go test -run TestE2E .  # Run e2e tests only
```

## Comparison with Original

| Aspect | Original (Python/Bash) | Go Edition |
|--------|------------------------|------------|
| Dependencies | Python, tmux, cloudflared, jq | Go, tmux |
| Lines of code | ~3500 | ~3000 |
| Deployment | Multiple files + scripts | Single binary |
| Tests | ~30 | 367 |
| Config | env + flags + config files | env + flags only |

## License

MIT
