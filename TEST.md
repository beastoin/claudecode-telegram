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
- Direct mode E2E tests (requires claude CLI):
  - Full flow: hire -> message -> response -> end
  - Multi-worker focus switching
  - @mention routing
  - /pause, /relaunch, /settings, /progress commands
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

## Test Coverage

**Current coverage: 98.7%** (144 of 146 features tested)

| Category | Tests | Coverage |
|----------|-------|----------|
| Telegram Bot Commands | 19 | 100% |
| CLI Commands & Flags | 37 | 97% |
| Message Routing | 13 | 100% |
| Security | 11 | 100% |
| Hook Behavior | 15 | 100% |
| Persistence Files | 14 | 100% |
| Image/Document Handling | 20 | 100% |
| HTTP Endpoints | 8 | 100% |
| Direct Mode | 14 | 100% |
| Direct Mode E2E | 13 | 100% |
| Misc Behavior | 12 | 100% |

**Only 2 features untested:**
- `-f`, `--force` flag (tested implicitly in other tests)
- Interactive prompt for multiple nodes (requires TTY input)

## Feature Test Matrix (Mode Parity)

Track test coverage for features across tmux and direct modes. When adding a feature to one mode, ensure equivalent test exists for the other.

| Feature | Tmux Mode Test | Direct Mode Test | Status |
|---------|---------------|------------------|--------|
| Session/worker creation | `test_hire_command` | `test_direct_mode_hire_creates_worker` | ✅ Parity |
| Session/worker survival | `test_tmux_mode_session_stays_alive` | `test_direct_mode_subprocess_stays_alive` | ✅ Parity |
| Message delivery verified | `test_tmux_mode_message_delivery` | `test_direct_mode_worker_accepts_messages` | ✅ Parity |
| HTML escaping | `test_forward_to_bridge_html_escape` | `test_direct_mode_html_escape` | ✅ Parity |
| Focus switching | `test_focus_command` | `test_direct_mode_e2e_focus_switch` | ✅ Parity |
| @mention routing | `test_at_mention` | `test_direct_mode_e2e_at_mention` | ✅ Parity |
| Pause/interrupt | `test_pause_command` | `test_direct_mode_e2e_pause` | ✅ Parity |
| Relaunch/restart | `test_relaunch_command` | `test_direct_mode_e2e_relaunch` | ✅ Parity |
| End/kill session | `test_end_command` | `test_direct_mode_end_kills_worker` | ✅ Parity |
| /team listing | `test_team_command` | `test_direct_mode_team_shows_workers` | ✅ Parity |
| /settings display | `test_settings_command` | `test_direct_mode_e2e_settings` | ✅ Parity |
| /progress display | `test_progress_command` | `test_direct_mode_e2e_progress` | ✅ Parity |
| Graceful shutdown | `test_graceful_shutdown` | `test_direct_mode_graceful_shutdown` | ✅ Parity |
| @all broadcast | `test_at_all_broadcast` | `test_direct_mode_at_all_broadcast` | ✅ Parity |
| Reply routing | `test_reply_routing` | `test_direct_mode_reply_routing` | ✅ Parity |
| Reply context | `test_reply_context` | `test_direct_mode_reply_context` | ✅ Parity |
| Worker shortcut focus | `test_worker_shortcut_focus_only` | `test_direct_mode_worker_shortcut_focus` | ✅ Parity |
| Worker shortcut + msg | `test_worker_shortcut_with_message` | `test_direct_mode_worker_shortcut_with_message` | ✅ Parity |
| /learn command | `test_learn_command` | `test_direct_mode_learn` | ✅ Parity |
| Unknown cmd passthrough | `test_unknown_command_passthrough` | `test_direct_mode_unknown_cmd_passthrough` | ✅ Parity |
| Inter-worker messaging | `test_worker_to_worker_pipe` | `test_worker_to_worker_pipe_direct` | ✅ Parity |
| Image/document handling | multiple | `test_direct_mode_image_handling` | ✅ Parity |

## Complete Test Inventory

> **Total: 202 test functions**
>
> Keep this list updated when adding new tests.

### Unit Tests (FAST mode, no bridge)

| Test | Description |
|------|-------------|
| `test_imports` | Verify bridge.py imports without errors |
| `test_version` | Verify CLI version command works |
| `test_message_splitting_short` | Messages under 4096 chars unchanged |
| `test_message_splitting_newlines` | Split at newline boundaries |
| `test_message_splitting_hard` | Hard split when no boundaries |
| `test_message_split_safe_boundaries` | Verify safe split boundary detection |
| `test_multipart_formatting` | Session prefix on multi-part messages |
| `test_multipart_chained_reply_to` | Reply chain for multipart messages |
| `test_telegram_max_length` | TELEGRAM_MAX_LENGTH constant = 4096 |
| `test_image_tag_parsing` | `[[image:/path\|caption]]` extraction |
| `test_file_tag_parsing` | `[[file:/path\|caption]]` extraction |
| `test_image_path_validation` | Allowlisted image extensions |
| `test_file_extension_validation` | Allowlisted/blocked document extensions |
| `test_code_fence_protection` | Tags inside code fences not parsed |
| `test_escape_tag_preservation` | Escaped `\[[image:...]]` preserved |
| `test_response_prefix_formatting` | Response prefix formatting |
| `test_response_with_image_tags` | Response with image tags |
| `test_persistence_file_functions` | save/load last_chat_id and last_active |
| `test_pending_set_and_clear` | set_pending and clear_pending functions |
| `test_pending_auto_timeout` | 10 minute pending auto-cleanup |
| `test_worker_name_sanitization` | Names sanitized to a-z, 0-9, hyphen |
| `test_hire_backend_parsing` | /hire backend parsing (--codex, codex- prefix) |
| `test_team_output_includes_backend` | /team output includes backend metadata |
| `test_progress_output_includes_backend` | /progress output includes backend metadata |
| `test_worker_send_uses_backend` | worker_send routes to backend handler |
| `test_backend_env_metadata` | WORKER_BACKEND exported via tmux env |
| `test_reserved_names_rejection` | Reserved names (commands, aliases) rejected |
| `test_bot_commands_structure` | BOT_COMMANDS list structure |
| `test_blocked_commands_list` | All blocked commands configured |
| `test_max_file_size` | MAX_FILE_SIZE = 20MB |
| `test_sandbox_config` | Sandbox config constants |
| `test_sandbox_docker_cmd` | Docker command generation |
| `test_extra_mounts_docker_cmd` | Extra mounts in Docker command |
| `test_tmux_send_locks` | Per-session lock mechanism |
| `test_graceful_shutdown` | graceful_shutdown function exists |
| `test_startup_notification_flag` | startup_notified flag exists |
| `test_typing_indicator_function` | Typing indicator function exists |
| `test_welcome_message_new_worker` | Welcome message constant exists |
| `test_file_tag_welcome_instructions` | File tag parsers available |
| `test_reply_context_formatting` | Reply context format |
| `test_document_message_format` | Document message format |
| `test_direct_mode_flag` | --no-tmux and --direct flags |
| `test_direct_mode_env_var` | DIRECT_MODE env var |
| `test_direct_worker_dataclass` | DirectWorker dataclass |
| `test_direct_worker_functions_exist` | Direct worker functions exist |
| `test_direct_mode_no_hook_install` | Direct mode skips hook install |
| `test_direct_mode_handle_event` | handle_direct_event parses JSON |
| `test_direct_mode_html_escape` | escape_html escapes <, >, & for Telegram |
| `test_direct_mode_is_pending` | is_pending in direct mode |
| `test_direct_mode_get_registered_sessions` | get_registered_sessions in direct mode |
| `test_direct_mode_graceful_shutdown` | Shutdown kills direct workers |

### Direct Mode Integration Tests (Default mode, bridge required)

| Test | Description |
|------|-------------|
| `test_direct_mode_bridge_starts` | Direct mode bridge starts and responds |
| `test_direct_mode_hire_creates_worker` | /hire creates direct worker subprocess |
| `test_direct_mode_message_routing` | Messages routed to direct worker |
| `test_direct_mode_team_shows_workers` | /team lists direct workers |
| `test_direct_mode_end_kills_worker` | /end terminates direct worker |

### Direct Mode E2E Tests (FULL mode, requires claude CLI)

| Test | Description |
|------|-------------|
| `test_direct_mode_subprocess_stays_alive` | **BEHAVIOR:** Verify subprocess stays running, not just starts |
| `test_direct_mode_worker_accepts_messages` | **BEHAVIOR:** Verify worker accepts messages via stdin |
| `test_direct_mode_e2e_full_flow` | Complete flow: hire -> message -> response -> end |
| `test_direct_mode_e2e_focus_switch` | Create 2 workers, verify /focus switches between them |
| `test_direct_mode_e2e_at_mention` | @worker routing without focus change |
| `test_direct_mode_at_all_broadcast` | @all broadcast to multiple workers |
| `test_direct_mode_reply_routing` | Reply to worker message routes correctly |
| `test_direct_mode_reply_context` | Reply context included for non-bot messages |
| `test_direct_mode_e2e_pause` | /pause sends interrupt to worker |
| `test_direct_mode_e2e_relaunch` | /relaunch restarts worker subprocess |
| `test_direct_mode_e2e_settings` | /settings shows direct mode indicator |
| `test_direct_mode_e2e_progress` | /progress shows worker status |
| `test_direct_mode_vs_tmux_parity` | Verify code paths exist for both modes |

### CLI Tests (FAST mode, no bridge)

| Test | Description |
|------|-------------|
| `test_cli_help` | --help shows usage |
| `test_cli_version` | --version shows version |
| `test_cli_node_flag` | --node=value and --node value syntax |
| `test_cli_port_flag` | -p=value and --port value syntax |
| `test_cli_all_flag` | --all flag syntax |
| `test_cli_no_tunnel_flag` | --no-tunnel flag syntax |
| `test_cli_tunnel_url_flag` | --tunnel-url flag syntax |
| `test_cli_headless_flag` | --headless flag syntax |
| `test_cli_quiet_flag` | --quiet flag syntax |
| `test_cli_verbose_flag` | --verbose flag syntax |
| `test_cli_no_color_flag` | --no-color flag syntax |
| `test_cli_env_file_flag` | --env-file flag syntax |
| `test_cli_sandbox_image_flag` | --sandbox-image flag syntax |
| `test_cli_mount_flag` | --mount flag syntax |
| `test_cli_mount_ro_flag` | --mount-ro flag syntax |
| `test_cli_default_ports` | Default ports by node name |
| `test_cli_unknown_command` | Unknown command rejection |
| `test_cli_missing_token_error` | Missing token error message |
| `test_cli_hook_install_uninstall` | Hook install command |
| `test_cli_hook_uninstall` | Hook uninstall command |
| `test_cli_hook_test_no_chat` | Hook test reports missing chat ID |
| `test_cli_stop_command` | stop command syntax |
| `test_cli_restart_command` | restart command syntax |
| `test_cli_clean_command` | clean command syntax |
| `test_cli_status_command` | status command execution |
| `test_cli_status_json_output` | --json flag produces valid JSON |
| `test_cli_webhook_info` | webhook info subcommand |
| `test_cli_webhook_set_url` | webhook URL setting |
| `test_cli_webhook_set_requires_https` | webhook rejects non-HTTPS |
| `test_cli_webhook_delete_requires_confirm` | webhook delete confirmation |
| `test_node_resolution_priority` | --node > NODE_NAME > auto-detect |
| `test_node_name_sanitization_cli` | Node name sanitization |
| `test_default_node_when_none_running` | Default node = prod |
| `test_equals_syntax` | --flag=value syntax |

### Hook Tests (FAST mode, no bridge)

| Test | Description |
|------|-------------|
| `test_hook_env_validation` | Hook exits on missing env vars |
| `test_hook_session_filtering` | Only processes matching TMUX_PREFIX |
| `test_hook_bridge_url_precedence` | BRIDGE_URL over PORT |
| `test_hook_bridge_url_env` | BRIDGE_URL env var usage |
| `test_hook_port_fallback` | PORT fallback when no BRIDGE_URL |
| `test_hook_tmux_prefix_usage` | TMUX_PREFIX usage |
| `test_hook_sessions_dir_usage` | SESSIONS_DIR usage |
| `test_hook_tmux_fallback_flag` | TMUX_FALLBACK=0 disables fallback |
| `test_hook_fails_closed` | Silent exit on missing config |
| `test_hook_pending_cleanup` | Pending file removed after hook |
| `test_hook_reads_tmux_env_first` | Tmux env takes precedence |
| `test_hook_transcript_extraction_retry` | Transcript extraction retry logic |
| `test_hook_tmux_fallback_warning` | Fallback warning message |
| `test_hook_async_forward_timeout` | Async forward with timeout |
| `test_hook_helper_script_exists` | Helper script exists |

### Bridge Environment Tests (FAST mode)

| Test | Description |
|------|-------------|
| `test_bridge_env_bot_token` | TELEGRAM_BOT_TOKEN handling |
| `test_bridge_env_port` | PORT env var handling |
| `test_bridge_env_webhook_secret` | TELEGRAM_WEBHOOK_SECRET handling |
| `test_bridge_env_sessions_dir` | SESSIONS_DIR handling |
| `test_bridge_env_tmux_prefix` | TMUX_PREFIX handling |
| `test_bridge_env_bridge_url` | BRIDGE_URL handling |
| `test_bridge_env_sandbox` | SANDBOX_* env vars handling |

### Persistence File Tests (FAST mode)

| Test | Description |
|------|-------------|
| `test_pid_file_creation` | pid file creation code |
| `test_bridge_pid_file_creation` | bridge.pid file creation code |
| `test_tunnel_pid_file_creation` | tunnel.pid file creation code |
| `test_tunnel_log_file_creation` | tunnel.log file creation code |
| `test_tunnel_url_file_creation` | tunnel_url file creation code |
| `test_port_file_creation` | port file creation code |
| `test_bot_id_cached` | bot_id caching code |
| `test_bot_username_cached` | bot_username caching code |
| `test_bridge_log_file_creation` | bridge.log file creation code |
| `test_pending_file_timestamp` | pending file timestamp format |
| `test_chat_id_file_content` | chat_id file content format |

### Status Diagnostics Tests (FAST mode)

| Test | Description |
|------|-------------|
| `test_orphan_process_detection` | Orphan process detection code |
| `test_webhook_conflict_warning` | Webhook conflict warning code |
| `test_tmux_env_mismatch_detection` | Tmux env mismatch detection code |
| `test_stale_hooks_detection` | Stale hooks detection code |

### Run/Tunnel Behavior Tests (FAST mode)

| Test | Description |
|------|-------------|
| `test_run_auto_installs_hook` | run auto-installs hook code |
| `test_webhook_failure_cleanup` | Cleanup on webhook failure code |
| `test_tunnel_watchdog_behavior` | Tunnel watchdog behavior code |

### Integration Tests (Default mode, bridge required)

| Test | Description |
|------|-------------|
| `test_bridge_starts` | Bridge starts and responds |
| `test_health_endpoint` | GET / health check |
| `test_admin_registration` | First user becomes admin |
| `test_admin_auto_learn_first_user` | Auto-learn admin behavior |
| `test_admin_chat_id_preset` | ADMIN_CHAT_ID preset behavior |
| `test_admin_restored_from_last_chat_id` | Admin restored on restart |
| `test_non_admin_rejection` | Non-admin silently rejected |
| `test_hire_command` | /hire creates worker |
| `test_team_command` | /team lists workers |
| `test_focus_command` | /focus switches worker |
| `test_progress_command` | /progress shows status |
| `test_learn_command` | /learn prompts worker |
| `test_pause_command` | /pause sends Escape |
| `test_relaunch_command` | /relaunch restarts worker |
| `test_settings_command` | /settings shows config |
| `test_end_command` | /end offboards worker |
| `test_dynamic_bot_command_list_update` | Bot command list updates on /hire and /end |
| `test_additional_commands` | Additional command tests |
| `test_worker_shortcut_focus_only` | /<worker> switches focus |
| `test_worker_shortcut_with_message` | /<worker> msg routes + focuses |
| `test_command_with_botname_suffix` | Command @botname suffix stripped |
| `test_blocked_commands` | Blocked commands rejected |
| `test_blocked_commands_integration` | Blocked commands via webhook |
| `test_unknown_command_passthrough` | Unknown /cmd passed to worker |
| `test_unknown_commands_passthrough` | Multiple unknown commands |
| `test_at_mention` | @name routing |
| `test_at_all_broadcast` | @all broadcast |
| `test_reply_routing` | Reply to worker message |
| `test_reply_context` | Reply context payload |
| `test_reply_with_explicit_context` | Explicit context format |
| `test_session_files` | Session file permissions |
| `test_secure_directory_permissions` | Directory permissions 0700 |
| `test_inbox_directory` | Inbox directory creation |
| `test_last_chat_id_persistence` | last_chat_id persistence |
| `test_last_active_persistence` | last_active persistence |
| `test_response_endpoint` | POST /response endpoint |
| `test_response_endpoint_missing_fields` | /response rejects missing fields |
| `test_response_endpoint_no_chat_id` | /response 404 for unknown session |
| `test_response_without_pending` | /response works without pending |
| `test_notify_endpoint` | POST /notify endpoint |
| `test_notify_endpoint_missing_text` | /notify rejects missing text |
| `test_webhook_secret_acceptance` | Webhook secret acceptance path |
| `test_webhook_secret_validation` | Webhook secret validation |
| `test_token_isolation` | Token not exposed to tmux |
| `test_photo_message_no_focused` | Photo without focused worker |
| `test_document_message_no_focused` | Document without focused worker |
| `test_document_message_routing` | Document routing |
| `test_status_shows_workers` | Status shows workers |

### Image/Document E2E Tests (requires TEST_CHAT_ID)

| Test | Description |
|------|-------------|
| `test_incoming_document_e2e` | Document upload → webhook → download |
| `test_incoming_image_e2e` | Image upload → webhook → download |
| `test_caption_prepended_to_message` | Caption prepended to message |
| `test_download_failure_notification` | Download failure notification |
| `test_inbox_path_under_tmp` | Inbox under /tmp |
| `test_inbox_cleanup_on_offboard` | Inbox cleanup on /end |
| `test_image_path_restriction` | Image path restriction |
| `test_document_no_path_restriction` | Document path flexibility |
| `test_blocked_filenames_list` | Blocked filenames |
| `test_send_failure_notification` | Send failure notification |
| `test_20mb_size_limit` | 20MB size limit |

### Misc Behavior Tests (Integration)

| Test | Description |
|------|-------------|
| `test_eye_reaction_on_acceptance` | Eyes reaction on acceptance |
| `test_typing_indicator_sent_while_pending` | Typing indicator while pending |
| `test_new_worker_welcome_message` | New worker welcome message |
| `test_test_env_vars_documented` | Test env vars documented |

### Tunnel Tests (FULL mode only)

| Test | Description |
|------|-------------|
| `test_with_tunnel` | Cloudflare tunnel + webhook config |

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

## Missing Tests (To Be Implemented)

This section tracks tests that should be added to ensure mode parity and complete coverage.

### Critical (Mode Parity)

All critical mode parity tests are now implemented. ✅

### Important (No Test)

These features have no tests in either mode and should be tested:

| Feature | Description |
|---------|-------------|
| Multipart response chaining behavior | Reply chain for multipart messages (reply_to_message_id) |

### Nice to Have

Lower priority tests for edge cases and robustness:

| Feature | Description |
|---------|-------------|
| Direct worker crash recovery | Worker process crash detection and cleanup |
| Concurrent pipe writes | Multiple workers writing to same pipe simultaneously |
| Pipe permissions | Named pipe has correct permissions (0o600) |
| Path traversal protection | Prevent `../` in worker names for inbox paths |
