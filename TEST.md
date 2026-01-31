# Testing Guide

## Quick Start

```bash
TELEGRAM_BOT_TOKEN='your-test-bot-token' ./test.sh
```

## Test Categories

### Unit Tests (No Network)

| Test | Description |
|------|-------------|
| `test_imports` | Verify bridge.py imports without errors |
| `test_version` | Verify CLI version command works |

### Integration Tests (Local Bridge)

| Test | Description |
|------|-------------|
| `test_bridge_starts` | Bridge starts and responds on configured port |
| `test_admin_registration` | First user auto-registered as admin |
| `test_non_admin_rejection` | Non-admin users silently rejected |
| `test_new_command` | `/new <name>` creates tmux session `claude-<name>` |
| `test_list_command` | `/list` returns session list |
| `test_use_command` | `/use <name>` switches active session |
| `test_status_command` | `/status` shows session details + Claude process state |
| `test_restart_command` | `/restart` restarts Claude in session |
| `test_stop_command` | `/stop` sends Escape to interrupt |
| `test_at_mention` | `@name message` routes to specific session |
| `test_session_files` | Session dirs (0700) and files (0600) have secure permissions |
| `test_kill_command` | `/kill <name>` removes tmux session |
| `test_blocked_commands` | Interactive commands (`/mcp`, `/vim`, etc.) blocked |
| `test_response_endpoint` | Full hook → bridge → Telegram response flow |

### Tunnel Tests (Optional)

| Test | Description |
|------|-------------|
| `test_with_tunnel` | Quick tunnel starts, webhook configured |

Skip with: `SKIP_TUNNEL=1 ./test.sh`

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TEST_PORT` | No | Bridge port (default: 8095) |
| `TEST_CHAT_ID` | No | Simulated chat ID (default: 123456789) |
| `ADMIN_CHAT_ID` | No | Pre-lock admin to this chat ID (enables full e2e test with real Telegram messages) |
| `SKIP_TUNNEL` | No | Set to `1` to skip tunnel tests |

## Manual Testing

### Simulate Telegram Webhook

```bash
# Start bridge
TELEGRAM_BOT_TOKEN='...' PORT=8095 python3 bridge.py &

# Send simulated message
curl -X POST http://localhost:8095 \
  -H "Content-Type: application/json" \
  -d '{
    "update_id": 1,
    "message": {
      "message_id": 1,
      "from": {"id": 123456789, "first_name": "Test"},
      "chat": {"id": 123456789, "type": "private"},
      "date": 1706400000,
      "text": "/list"
    }
  }'
```

### Test with Real Telegram

```bash
# Quick tunnel (random URL each time)
./claudecode-telegram.sh run

# Or with persistent URL
./claudecode-telegram.sh run --tunnel-url https://your.domain.com
```

## Test Coverage

### Commands Tested

- [x] `/new <name>` - Create session
- [x] `/use <name>` - Switch session
- [x] `/list` - List sessions
- [x] `/kill <name>` - Kill session
- [x] `/status` - Session status with Claude process check
- [x] `/stop` - Interrupt (Escape)
- [x] `/restart` - Restart Claude
- [x] `/system` - Show system config (secrets redacted)
- [x] `@name <msg>` - Mention routing
- [x] `<message>` - Route to active session

### Security Tested

- [x] Admin auto-learn (first user)
- [x] Non-admin silent rejection
- [x] Session directory permissions (0700)
- [x] Session file permissions (0600)
- [x] Blocked interactive commands

### Lifecycle Tested

- [x] Bridge startup message
- [x] Bridge shutdown message (manual test via Ctrl+C)
- [x] Graceful signal handling (SIGTERM, SIGINT)

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

Then call from `main()`:

```bash
test_my_feature
```

## Test Isolation

Tests run isolated using `--node test` under `~/.claude/telegram/nodes/test/`:

| Resource | Test | Production |
|----------|------|------------|
| Node dir | `~/.claude/telegram/nodes/test/` | `~/.claude/telegram/nodes/prod/` |
| Port | 8095 | 8081 |
| tmux prefix | `claude-test-` | `claude-prod-` |
| Session files | `.../nodes/test/sessions/` | `.../nodes/prod/sessions/` |
| PID file | `.../nodes/test/pid` | `.../nodes/prod/pid` |
| Logs | `.../nodes/test/*.log` | `.../nodes/prod/*.log` |
| Bot token | Separate test bot | Production bot |

This allows running tests while production is active.

## Full E2E Test

To test the complete response flow (hook → bridge → Telegram):

```bash
TELEGRAM_BOT_TOKEN='...' ADMIN_CHAT_ID='your-chat-id' ./test.sh
```

With `ADMIN_CHAT_ID` set:
- Bridge pre-locks to your chat ID (no auto-learn)
- Test messages use your real chat ID
- Response test sends actual message to your Telegram

## CI Integration

```yaml
# GitHub Actions example
- name: Run tests
  env:
    TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_TEST_TOKEN }}
    SKIP_TUNNEL: 1
  run: ./test.sh
```
