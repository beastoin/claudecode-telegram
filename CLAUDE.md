# Claude Code Project Instructions

## Version Management

When making changes that result in a new version:

1. **Update version** in `claudecode-telegram.sh`:
   ```bash
   VERSION="x.y.z"
   ```

2. **Update `DOC.md`** with:
   - New version number in header
   - Changelog entry describing:
     - Breaking changes (table format if applicable)
     - New features
     - Architecture changes
   - Update design philosophy sections if core principles changed

3. **Run acceptance tests** before committing:
   ```bash
   TELEGRAM_BOT_TOKEN='...' ./test.sh
   ```
   See `TEST.md` for full testing documentation.

## When to Bump Version

- **Patch (0.0.x)**: Bug fixes, minor tweaks
- **Minor (0.x.0)**: New features, backward-compatible changes
- **Major (x.0.0)**: Breaking changes, architecture overhaul

## Key Files

| File | Purpose |
|------|---------|
| `bridge.py` | Telegram webhook handler, session management |
| `claudecode-telegram.sh` | CLI wrapper, tunnel/webhook setup |
| `hooks/send-to-telegram.sh` | Claude Stop hook, sends responses |
| `test.sh` | Automated acceptance tests |
| `CLAUDE.md` | Project instructions + operational learnings (AGENTS.md symlink) |
| `DOC.md` | Design philosophy, changelog |
| `TEST.md` | Testing documentation |

## Testing Requirements

### Test Modes

Use the appropriate test mode for your workflow:

| Mode | Command | Time | When to Use |
|------|---------|------|-------------|
| **FAST** | `FAST=1 ./test.sh` | ~15s | While coding (TDD inner loop) |
| **Default** | `./test.sh` | ~2-3 min | Before committing |
| **FULL** | `FULL=1 ./test.sh` | ~5 min | Before pushing |

### TDD Workflow

```bash
# While developing - run frequently
FAST=1 TEST_BOT_TOKEN='...' ./test.sh

# Before commit - full local validation
TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh

# Before push - including tunnel tests
FULL=1 TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh
```

### Test Guidelines

1. **Write tests alongside features** - add to `test.sh`
2. **Focus on e2e tests** - test the full flow, not just units
3. **Use FAST mode during development** - for quick feedback
4. **Run default mode before committing** - catch integration issues
5. **Treat tests as usage examples** - prefer real Telegram flows (hire → send → reply) and keep them deterministic

**Why e2e tests matter:**
- They catch integration bugs that unit tests miss
- They document how features actually work
- They give confidence when refactoring
- They're the safety net for this project

### Running Tests

**Rule: Always run default tests before committing, FULL before pushing.**

```bash
# Quick validation while coding
FAST=1 TEST_BOT_TOKEN='...' ./test.sh

# Full test before commit
TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh
```

**Test isolation:** Tests run isolated using `--node test` under `~/.claude/telegram/nodes/test/` (port 8095, prefix `claude-test-`). You can run tests while production is active.

### Test Coverage

**Current: 210 test functions** (see `TEST.md` for inventory)

| Category | Tests | Coverage |
|----------|-------|----------|
| Unit (FAST) | 115 | imports, formatting, core helpers |
| CLI (FAST) | 30 | flags, commands, webhook/hook coverage |
| Integration | 64 | commands, security, routing, endpoints |
| Tunnel (FULL) | 1 | cloudflare tunnel, webhook setup |

See `TEST.md` for complete test inventory.

### When Adding New Tests

**IMPORTANT: Keep `TEST.md` test inventory updated.**

When you add a new test:
1. Add the test function to `test.sh` in the appropriate section
2. Add the function to the appropriate runner (`run_unit_tests`, `run_cli_tests`, `run_integration_tests`, or `run_tunnel_tests`)
3. **Update the test inventory in `TEST.md`** with the new test name and description
4. Update the coverage stats if adding tests for previously untested features

## Design Philosophy

**Source of truth: `DOC.md`** - All principles are documented there with full context.

When making changes, ensure they align with the philosophy in `DOC.md`. If adding new principles, update `DOC.md` first (both the summary table at the top AND the detailed section), then reference here.

### Quick Reference (see DOC.md for details)

| Principle | Rule |
|-----------|------|
| **Tests required** | Every new feature MUST have an e2e test |
| tmux IS persistence | No database, no state.json |
| `claude-<name>` naming | Enables auto-discovery |
| RAM state only | Rebuilt on startup from tmux |
| Per-session files | Minimal hook↔gateway coordination |
| Fail loudly | No silent errors, no hidden retries |
| Token isolation | `TELEGRAM_BOT_TOKEN` NEVER leaves bridge |
| Admin config | `ADMIN_CHAT_ID` env var or auto-learn first user |
| Secure by default | 0o700 dirs, 0o600 files |

## Learnings

### Never hardcode paths

**Problem:** Hardcoded paths break test isolation and make the system inflexible.

**Rule:** All paths must be configurable via environment variables with sensible defaults.

```bash
# Good: configurable with default
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/telegram/sessions}"

# Bad: hardcoded
SESSIONS_DIR="$HOME/.claude/telegram/sessions"
```

### Env vars must propagate through the full chain

**Problem:** When process A spawns process B which runs process C, env vars set in A don't automatically reach C.

**Rule:** If a subprocess needs config, explicitly export it at each boundary:
- Parent process sets env var
- Parent exports to child's environment (e.g., `tmux send-keys "export VAR=value"`)
- Child process reads env var

**Check all entry points:** If a session can be created via `create_session()`, `register_session()`, or `restart_claude()`, ALL of them must export the required env vars.

### When adding configurable behavior, audit all code paths

**Problem:** Adding a new config option in one place but missing other places that need it.

**Rule:** When making something configurable:
1. Search for ALL usages of the old hardcoded value
2. Update every location that references it
3. Ensure all entry points (create, register, restart, discover) handle it consistently

### Keep project memory current

**Problem:** Fixes and gotchas get rediscovered when the memory is stale or scattered.

**Rule:** Capture new operational learnings here, architecture changes in `DOC.md`, and test additions in `TEST.md`. Remove or update notes if behavior changes.

**Why:** The agent (and future contributors) rely on these files as the source of truth.

### Per-node pipe + inbox isolation

**Problem:** Pipes/inboxes under `/tmp` were shared across nodes, causing collisions between prod/dev/test.

**Rule:** Namespace all `/tmp` paths by node (derived from `TMUX_PREFIX`):
```
/tmp/claudecode-telegram/<node>/<worker>/in.pipe
/tmp/claudecode-telegram/<node>/<worker>/inbox/
```

### Watchdog for bridge requires careful testing

**Problem:** Adding bridge auto-restart to the watchdog (like tunnel has) seems simple but has hidden complexity:
- Shell output buffering when redirected to files
- Port conflicts between test stages
- Race conditions between process cleanup and restart
- Test timeouts vs DNS propagation delays

**Current state:** Only tunnel watchdog exists (v0.5.0). Bridge watchdog was attempted (v0.5.4) but reverted due to test failures.

**If re-implementing:**
1. Test the script manually first, not just via test harness
2. Use `stdbuf -oL` for unbuffered output in tests
3. Add longer delays between killing processes and checking ports
4. Consider skipping watchdog tests in CI (mark as slow/optional)
5. Ensure `start_bridge()` passes ALL required env vars, not just token/port
6. Add explicit stop conditions (max retries or timeouts) and log when the watchdog gives up

### NEVER use pkill on multi-node setups

**Problem:** `pkill -f cloudflared` or `pkill -f bridge.py` kills ALL matching processes across ALL nodes, not just the target node.

**Rule:** ALWAYS use PID-based killing, NEVER pattern-based.

```bash
# WRONG - kills ALL nodes
pkill -f cloudflared
pkill -f bridge.py

# WRONG - kills without knowing which node owns the port
lsof -ti :8081 | xargs kill

# RIGHT - use specific PID from file
kill $(cat ~/.claude/telegram/nodes/prod/pid)

# RIGHT - use the script's stop command
./claudecode-telegram.sh --node prod stop
```

**Why this matters:** Production runs multiple nodes (prod, dev, test) simultaneously. Pattern-based killing causes collateral damage to other running nodes.

### Verify port ownership before killing

**Problem:** Ran `lsof -ti :8081 | xargs kill` thinking it was dev node, but port 8081 = prod. Killed production bridge while team was working.

**Port assignments:**
| Port | Node | Sandbox |
|------|------|---------|
| 8080 | sandbox (or custom) | `--sandbox` |
| 8081 | **prod** | `--no-sandbox` |
| 8082 | dev | `--no-sandbox` |
| 8095 | test (test.sh) | `--no-sandbox` |

**Why `--no-sandbox` for prod/dev/test?** Docker overhead is too slow. Sandbox node is for untrusted/experimental code.

**Rule:** Before killing any port, verify which node owns it:
```bash
# Check what's running on a port BEFORE killing
lsof -ti :8081  # Just list, don't kill

# Or check node configs
cat ~/.claude/telegram/nodes/*/port  # See all port assignments
```

**Why:** Port numbers are not intuitive (8081 looks like it could be "secondary" or "dev"). Always verify before destructive operations.

### Use script commands or PID files to stop services

**Problem:** Used pkill to restart bridge, caused production outage.

**Fix:** Use the script's stop command or kill via PID file:
```bash
# RIGHT - use script command
./claudecode-telegram.sh stop

# RIGHT - use PID file
kill $(cat ~/.claude/telegram/claudecode-telegram.pid)
```

**Why:** pkill is too broad and can kill processes unexpectedly, causing downtime.

### Always test on dev node before prod deployment

**Problem:** Deployed v0.9.2 fix directly to prod without testing on dev node first. Ran local stress test but skipped real integration testing on dev.

**Fix:** Always test on dev node before prod deployment:
1. Start dev bridge with dev bot token on port 8082
2. Run full integration tests against dev
3. Test manually via Telegram on dev bot
4. Only then deploy to prod

**Why:** Local/unit tests prove concepts work in isolation, but real integration bugs only surface with actual Telegram traffic on a separate dev instance. Prod is not for testing.

### tmux send race condition

**Problem:** Concurrent sends to same tmux session interleave (text1, text2, Enter1, Enter2) causing ~50% message loss.

**Fix:** Per-session locks in `tmux_send_message()` serialize sends to same session.

**Why:** Two subprocess calls (`send-keys -l text`, `send-keys Enter`) are not atomic. Without locking, concurrent sends to the same session corrupt each other.

### macOS vs Linux shell compatibility

**Problem:** GNU coreutils (Linux) and BSD coreutils (macOS) have different flags for the same operations.

**Common pitfalls:**
| Operation | Linux (GNU) | macOS (BSD) |
|-----------|-------------|-------------|
| File size | `stat -c %s file` | `stat -f%z file` |
| Milliseconds | `date +%s%3N` | Not supported (`%N` is GNU extension) |
| sed in-place | `sed -i 's/a/b/'` | `sed -i '' 's/a/b/'` |
| grep -P | Supported | Not supported (use `grep -E`) |

**Fix:** Always use portable alternatives or try-fallback pattern:
```bash
# Portable file size
size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)

# Portable timing: use iteration counts instead of milliseconds
for i in $(seq 1 40); do sleep 0.05; done  # 2 seconds total
```

### Sed placeholders in conditionals

**Problem:** Template had conditional logic that referenced the placeholder being substituted:
```bash
NODE_NAME="__NODE_NAME__"
if [[ "$NODE_NAME" != "__NODE_NAME__" ]]; then  # Always false after sed!
```
After `sed -e "s|__NODE_NAME__|prod|g"`, condition becomes `"prod" != "prod"` → false.

**Fix:** Don't use conditionals in templates. Just bake values directly:
```bash
TMUX_PREFIX="__NODE_PREFIX__"   # Becomes "claude-prod-" after sed
SESSIONS_DIR="__NODE_SESSIONS_DIR__"
BRIDGE_PORT="__NODE_PORT__"
```

**Why:** Sed substitution is global. It replaces ALL occurrences of the pattern, including in comparison strings. Keep templates simple - no fallback logic needed since templates are never run directly.

**Prevention:**
1. Run `shellcheck` on all shell scripts
2. Test on macOS before merging (primary target platform)
3. Avoid GNU-specific extensions: `%N`, `stat -c`, `sed -i`, `grep -P`

### Test behavior, not scaffolding

**Problem:** Tests verified structure (functions exist, HTTP returns OK) but not actual behavior. An exec-mode worker subprocess was dying immediately, but tests passed because they only checked:
- `test_bridge_starts` → bridge starts
- `test_hire_command` → HTTP returns "OK"
- `test_send_to_worker_function_exists` → functions exist

None of these verified that the worker actually stayed running or could receive messages.

**Rule:** Tests must verify the actual behavior users care about, not just that code structure exists.

```bash
# BAD - tests scaffolding
test_bridge_starts() {
    curl -s /health >/dev/null  # 200 OK, but doesn't prove workers run
}

test_hire_command() {
    [[ $(curl -s /hire) == "OK" ]]  # Returns OK but worker may have died!
}

test_send_to_worker_function_exists() {
    python3 -c "from bridge import send_to_worker; assert callable(send_to_worker)"
}

# GOOD - tests behavior
test_tmux_mode_session_stays_alive() {
    curl -s /hire  # Create worker
    sleep 3        # Wait a bit
    # Verify worker/session is STILL running, not just that it started
    tmux has-session -t claude-test-worker
}

test_worker_to_worker_pipe() {
    # Verify inter-worker pipe messages are delivered end-to-end
    assert_no_log "Cannot forward pipe message"
}
```

**Why this matters:** When tests pass but features are broken, you waste time debugging and lose trust in the test suite. Behavior tests catch real bugs; scaffolding tests give false confidence.
