# Testing Guide

## Test Modes

The test suite supports three modes for different use cases:

| Mode | Command | Time | Use Case |
|------|---------|------|----------|
| **FAST** | `FAST=1 ./test.sh` | ~10-15s | TDD inner loop, quick validation |
| **Default** | `./test.sh` | ~2-3 min | Pre-commit, full validation |
| **FULL** | `FULL=1 ./test.sh` | ~5 min | Before push, includes tunnel tests |

### TDD Workflow

```bash
# While coding - run after each change
FAST=1 TEST_BOT_TOKEN='...' ./test.sh

# Before committing
TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh

# Before pushing
FULL=1 TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh
```

### What Each Mode Tests

**FAST mode** (no bridge, no network):
- Python imports and functions
- Message formatting and splitting
- CLI flags (--help, --version, --node, --port)
- Constants and configuration validation
- Concurrency helpers (locks)
- Hook install/uninstall

**Default mode** (bridge running locally):
- Everything in FAST mode, plus:
- Bridge startup and health check
- All Telegram commands (/hire, /team, /focus, etc.)
- Admin authorization
- Worker routing (@mention, @all, reply-to)
- Security (webhook secret, token isolation, file permissions)
- Image/document handling
- /response and /notify endpoints
- Persistence files

**FULL mode**:
- Everything in Default mode, plus:
- Cloudflare tunnel startup
- Webhook configuration with real Telegram API

## Test Pyramid

```
        /\
       /  \  FULL: Tunnel + Webhook (rare, slow)
      /----\
     /      \ Default: Bridge + Commands (every commit)
    /--------\
   /          \ FAST: Unit + CLI (every code change)
  /-----------\
```

The goal is to run FAST tests frequently during development, Default tests before commits, and FULL tests before pushing to catch integration issues.

## Quick Start

```bash
TEST_BOT_TOKEN='your-test-bot-token' ./test.sh
```

## Test Categories

### Unit Tests (No Network)

| Test | Description |
|------|-------------|
| `test_imports` | Verify bridge.py imports without errors |
| `test_version` | Verify CLI version command works |
| `test_message_splitting_*` | Message chunking for Telegram's 4096 char limit |
| `test_multipart_formatting` | Session prefix on multi-part messages |
| `test_*_tag_parsing` | Image and file tag extraction |
| `test_persistence_file_functions` | Save/load last_chat_id and last_active |

### CLI Tests (No Network)

| Test | Description |
|------|-------------|
| `test_cli_help` | --help shows usage |
| `test_cli_version` | --version shows version |
| `test_cli_node_flag` | --node=value and --node value syntax |
| `test_cli_port_flag` | -p=value and --port value syntax |
| `test_cli_hook_install_uninstall` | Hook file and settings.json registration |

### Integration Tests (Bridge Required)

| Test | Description |
|------|-------------|
| `test_bridge_starts` | Bridge starts and responds on configured port |
| `test_admin_registration` | First user auto-registered as admin |
| `test_non_admin_rejection` | Non-admin users silently rejected |
| `test_hire_command` | `/hire <name>` creates tmux session `claude-<name>` |
| `test_team_command` | `/team` returns session list |
| `test_focus_command` | `/focus <name>` switches active session |
| `test_at_mention` | `@name message` routes to specific session |
| `test_session_files` | Session dirs (0700) and files (0600) have secure permissions |
| `test_end_command` | `/end <name>` removes tmux session |
| `test_blocked_commands` | Interactive commands (`/mcp`, `/vim`, etc.) blocked |
| `test_response_endpoint` | Full hook -> bridge -> Telegram response flow |

### Tunnel Tests (Optional, FULL mode only)

| Test | Description |
|------|-------------|
| `test_with_tunnel` | Cloudflare tunnel starts, webhook configured |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TEST_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TEST_PORT` | No | Bridge port (default: 8095) |
| `TEST_CHAT_ID` | No | Your chat ID for e2e tests (default: mock 123456789) |
| `FAST` | No | Set to `1` for unit + CLI tests only |
| `FULL` | No | Set to `1` to include tunnel tests |

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
      "text": "/team"
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

To test the complete response flow (hook -> bridge -> Telegram):

```bash
TEST_BOT_TOKEN='...' TEST_CHAT_ID='your-chat-id' ./test.sh
```

With `TEST_CHAT_ID` set:
- Bridge pre-locks to your chat ID (no auto-learn)
- Test messages use your real chat ID
- Response test sends actual message to your Telegram

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

Then add to the appropriate runner function:
- `run_unit_tests()` for tests that don't need the bridge
- `run_cli_tests()` for CLI-only tests
- `run_integration_tests()` for tests that need the bridge running
- `run_tunnel_tests()` for tests that need the tunnel

Call from the runner function:

```bash
run_unit_tests() {
    # ... existing tests ...
    test_my_feature
}
```
