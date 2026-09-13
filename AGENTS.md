# claudecode-telegram Agent Guide

These rules apply to every AI agent that works in this repository.
This file is the single source of truth.
`CLAUDE.md` points here.

## Version Management

Do these three steps when you make changes that need a new version:

1. Update the version in `claudecode-telegram.sh`:
   ```bash
   VERSION="x.y.z"
   ```

2. Update `DOC.md`:
   - Set the new version number in the header.
   - Add a changelog entry. Describe breaking changes, new features, and architecture changes.
   - Update the design philosophy sections if core principles changed.

3. Run the acceptance tests before you commit:
   ```bash
   TEST_BOT_TOKEN='...' ./test.sh
   ```
   See `TEST.md` for the full testing documentation.

## When to Bump the Version

- **Patch (0.0.x):** Bug fixes and minor corrections.
- **Minor (0.x.0):** New features and backward-compatible changes.
- **Major (x.0.0):** Breaking changes and architecture overhauls.

## Key Files

| File | Purpose |
|------|---------|
| `bridge.py` | Telegram webhook handler, worker management, all HTTP endpoints |
| `claudecode-telegram.sh` | CLI wrapper, tunnel/webhook setup |
| `hooks/send-to-telegram.sh` | Claude Stop hook — sends responses to Telegram |
| `test.sh` | Automated acceptance tests |
| `AGENTS.md` | Agent instructions, rules, learnings (this file) |
| `CLAUDE.md` | Pointer to AGENTS.md |
| `DOC.md` | Design philosophy and changelog |
| `TEST.md` | Testing documentation |

## Testing Requirements

Follow these workflow rules:

- Use FAST mode during development (TDD inner loop). Run default mode before you commit. Run FULL mode before you push.
- Write tests alongside features. Focus on end-to-end behavior, not scaffolding.
- Treat tests as usage examples. Prefer real Telegram flows (hire, send, reply). Keep tests deterministic.
- Follow `TEST.md` when you add tests.
- See `TEST.md` for mode definitions, env vars, isolation details, and test inventories.

### TDD Workflow: Red-Green-Refactor

Development follows increment-based TDD.
Break each feature into small testable increments.
Each increment follows Red-Green-Refactor.

**Decompose first.** Before you write code, break the feature into an increment ladder:

1. Degenerate/empty case (zero, nil, no-op)
2. Simplest happy path (one item, minimal valid input)
3. Variations (multiple items, different valid inputs)
4. Edge cases (boundaries, limits, special characters)
5. Error cases (invalid input, missing data, failure modes)
6. Integration (combine with other components)

**Per increment:**

1. **RED:** Write one failing test in test.sh. Run it with TEST_FILTER to confirm the failure:
   ```bash
   TEST_FILTER=test_name FAST=1 TEST_BOT_TOKEN='...' ./test.sh
   ```
2. **GREEN:** Write minimal code to make the test pass. Run the filtered test again.
3. **REFACTOR:** Clean up if necessary. Run the filtered test to confirm it still passes.
4. Move to the next increment.

**Mode gates:**
```bash
# While you develop — run a single test frequently
TEST_FILTER=test_name FAST=1 TEST_BOT_TOKEN='...' ./test.sh

# After each increment passes — run the FAST suite
FAST=1 TEST_BOT_TOKEN='...' ./test.sh

# Before you commit — run full local validation
TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh

# Before you push — run tunnel tests too
FULL=1 TEST_BOT_TOKEN='...' TEST_CHAT_ID='...' ./test.sh
```

**Rules:**

- Write the test before the implementation code.
- Test one behavior per test. Keep tests focused and readable.
- Run the full FAST suite after each increment to catch regressions.
- Show the first failing test evidence before you start implementation in plan reviews.

**Why end-to-end tests matter:**

- End-to-end tests catch integration bugs that unit tests miss.
- End-to-end tests document how features work in practice.
- End-to-end tests give you confidence when you refactor.
- End-to-end tests are the safety net for this project.

## Design Philosophy

The source of truth is `DOC.md`.
All principles are documented there with full context.

When you make changes, verify that the changes align with the philosophy in `DOC.md`.
If you add new principles, update `DOC.md` first (both the summary table and the detailed section).

### Quick Reference (see DOC.md for details)

| Principle | Rule |
|-----------|------|
| Tests required | Every new feature must have an end-to-end test |
| tmux IS persistence | No database. tmux sessions are the source of truth for worker state |
| `claude-<name>` naming | Configurable prefix via `TMUX_PREFIX` (default: `claude-`) |
| RAM state only | Core worker state derived from tmux. Supplementary files for registry, guests, channels, relay |
| Per-session files | Minimal hook-to-gateway coordination |
| Fail loudly | No silent errors, no hidden retries |
| Token isolation | `TELEGRAM_BOT_TOKEN` never leaves the bridge process |
| Admin config | `ADMIN_CHAT_ID` env var or auto-learn first user |
| Secure by default | 0o700 dirs, 0o600 files |
| Decentralized worker comms | Worker-to-worker sends stay direct; manager tools may use bridge APIs |
| Machines are config | Static host topology lives in `machines.json`; tmux/workers are runtime truth |

## Learnings

### Never hardcode paths

**Problem:** Hardcoded paths break test isolation. They make the system inflexible.

**Rule:** Make all paths configurable through environment variables. Provide sensible defaults.

```bash
# Good: configurable with default
SESSIONS_DIR="${SESSIONS_DIR:-$HOME/.claude/telegram/sessions}"

# Bad: hardcoded
SESSIONS_DIR="$HOME/.claude/telegram/sessions"
```

### Env vars must propagate through the full chain

**Problem:** Process A spawns process B. Process B runs process C. Env vars from A do not automatically reach C.

**Rule:** Export env vars explicitly at each boundary:

- The parent process sets the env var.
- The parent exports to the child's tmux environment (via `tmux set-environment`).
- The child process reads the env var from the tmux environment.

```bash
# Good: use tmux set-environment (what export_hook_env() does)
tmux set-environment -t session_name BRIDGE_URL "http://localhost:8271"

# Bad: inject shell text (races with other sends, fragile)
tmux send-keys -t session_name "export BRIDGE_URL=http://localhost:8271" Enter
```

Check all entry points. If a session can be created through `WorkerManager.hire()`, `WorkerManager.restart()`, `_restart_dead_worker()`, or `POST /register`, all four must export the required env vars.

### When you add configurable behavior, audit all code paths

**Problem:** A new config option was added in one place. Other places that need the option were missed.

**Rule:** When you make something configurable:

1. Search for all usages of the old hardcoded value.
2. Update every location that references the value.
3. Verify that all entry points (`hire()`, `restart()`, `_restart_dead_worker()`, `POST /register`) handle the option consistently.

### Keep project memory current

**Problem:** Fixes and gotchas get rediscovered when the memory is stale or scattered.

**Rule:** Record new operational learnings here. Record architecture changes in `DOC.md`. Record test additions in `TEST.md`. Remove or update notes when behavior changes.

Agents and future contributors rely on these files as the source of truth.

### Per-node pipe and inbox isolation

**Problem:** Pipes and inboxes under `/tmp` were shared across nodes. This caused collisions between prod, dev, and test.

**Rule:** Namespace all `/tmp` paths by node (derived from `TMUX_PREFIX`):
```
/tmp/claudecode-telegram/<node>/<worker>/in.pipe
/tmp/claudecode-telegram/<node>/<worker>/inbox/
```

### Watchdog for bridge requires careful testing

**Problem:** Bridge auto-restart in the watchdog has hidden complexity:

- Shell output buffering when redirected to files.
- Port conflicts between test stages.
- Race conditions between process cleanup and restart.
- Test timeouts versus DNS propagation delays.

**Current state:** Only the tunnel watchdog exists (v0.5.0). A bridge watchdog was attempted (v0.5.4) but was reverted because of test failures.

**If you re-implement the bridge watchdog:**

1. Test the script manually first, not just through the test harness.
2. Use `stdbuf -oL` for unbuffered output in tests.
3. Add longer delays between process kill and port check.
4. Consider marking watchdog tests as slow/optional in CI.
5. Make sure `start_bridge()` passes all required env vars, not just token and port.
6. Add explicit stop conditions (max retries or timeouts). Log when the watchdog gives up.

### Never use pkill on multi-node setups

**Problem:** `pkill -f cloudflared` or `pkill -f bridge.py` kills all matching processes across all nodes.

**Rule:** Always use PID-based killing. Never use pattern-based killing.

```bash
# WRONG — kills ALL nodes
pkill -f cloudflared
pkill -f bridge.py

# WRONG — kills without knowing which node owns the port
lsof -ti :8271 | xargs kill

# RIGHT — use specific PID from file
kill $(cat ~/.claude/telegram/nodes/prod/pid)

# RIGHT — use the script's stop command
./claudecode-telegram.sh --node prod stop
```

Production runs multiple nodes (prod, dev, test) at the same time. Pattern-based killing causes collateral damage to other running nodes.

### Verify port ownership before you kill

**Problem:** Ran `lsof -ti :8271 | xargs kill` and assumed it was the dev node. Port 8271 is prod. This killed the production bridge.

**Default port assignments (override with `--port` or `PORT` env var):**

| Default Port | Node | Sandbox |
|--------------|------|---------|
| 8270 | sandbox (or custom) | `--sandbox` |
| 8271 | **prod** | `--no-sandbox` |
| 8272 | dev | `--no-sandbox` |
| 8295 | test (test.sh) | `--no-sandbox` |

Ports are dynamic. Always check the actual running port. Do not assume defaults.

Prod, dev, and test use `--no-sandbox` because Docker overhead is too slow. The sandbox node is for untrusted or experimental code.

**Rule:** Before you kill any port, verify which node owns it:
```bash
# Check all running port assignments BEFORE killing
cat ~/.claude/telegram/nodes/*/port

# Or check a specific node
cat ~/.claude/telegram/nodes/prod/port
```

Do not assume that a port belongs to a specific node. Always verify before you run destructive operations.

### Node credentials live in ~/.config/claudecode-telegram/

**Problem:** During a prod restart, time was wasted extracting the bot token from `/proc/<pid>/environ`. The token was already stored in a config file.

**Rule:** Token env files are at `~/.config/claudecode-telegram/<node>.env`. Use them for restarts:
```bash
# Load token and restart prod
source ~/.config/claudecode-telegram/prod.env
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" ./claudecode-telegram.sh --node prod --no-sandbox run
```

| File | Purpose |
|------|---------|
| `~/.config/claudecode-telegram/prod.env` | Prod bot token |
| `~/.config/claudecode-telegram/test.env` | Test bot token |

This is faster and more reliable than extracting tokens from process memory. It works even if the bridge is already dead.

### Use script commands or PID files to stop services

**Problem:** Used pkill to restart the bridge. This caused a production outage.

**Rule:** Use the stop command from the script, or kill through the node PID file:
```bash
# RIGHT — use script command
./claudecode-telegram.sh --node prod stop

# RIGHT — use per-node PID file
kill $(cat ~/.claude/telegram/nodes/prod/pid)
```

pkill is too broad. It can kill processes across nodes. This causes downtime.

### Always test on the dev node before you deploy to prod

**Problem:** Deployed a v0.9.2 fix directly to prod without testing on dev first. Ran a local stress test but skipped real integration testing.

**Rule:** Always test on the dev node before you deploy to prod:

1. Start the dev bridge with the dev bot token on port 8272.
2. Run full integration tests against dev.
3. Test manually through Telegram on the dev bot.
4. Only then deploy to prod.

Local and unit tests prove that concepts work in isolation. Real integration bugs only surface with actual Telegram traffic on a separate dev instance. Do not test on prod.

### tmux send race condition

**Problem:** Concurrent sends to the same tmux session interleave (text1, text2, Enter1, Enter2). This causes about 50% message loss.

**Fix:** Per-session locks in `tmux_send_message()` serialize sends to the same session.

Two subprocess calls (`send-keys -l text`, `send-keys Enter`) are not atomic. Without locking, concurrent sends to the same session corrupt each other.

### macOS versus Linux shell compatibility

**Problem:** GNU coreutils (Linux) and BSD coreutils (macOS) use different flags for the same operations.

**Common pitfalls:**

| Operation | Linux (GNU) | macOS (BSD) |
|-----------|-------------|-------------|
| File size | `stat -c %s file` | `stat -f%z file` |
| Milliseconds | `date +%s%3N` | Not supported (`%N` is GNU extension) |
| sed in-place | `sed -i 's/a/b/'` | `sed -i '' 's/a/b/'` |
| grep -P | Supported | Not supported (use `grep -E`) |

**Fix:** Always use portable alternatives or a try-fallback pattern:
```bash
# Portable file size
size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null)

# Portable timing: use iteration counts instead of milliseconds
for i in $(seq 1 40); do sleep 0.05; done  # 2 seconds total
```

### Sed placeholders in conditionals

**Problem:** A template had conditional logic that referenced the placeholder that sed substitutes:
```bash
NODE_NAME="__NODE_NAME__"
if [[ "$NODE_NAME" != "__NODE_NAME__" ]]; then  # Always false after sed!
```
After `sed -e "s|__NODE_NAME__|prod|g"`, the condition becomes `"prod" != "prod"` — always false.

**Fix:** Do not use conditionals in templates. Bake the values directly:
```bash
TMUX_PREFIX="__NODE_PREFIX__"   # Becomes "claude-prod-" after sed
SESSIONS_DIR="__NODE_SESSIONS_DIR__"
BRIDGE_PORT="__NODE_PORT__"
```

Sed substitution is global. It replaces all occurrences of the pattern, including those in comparison strings. Keep templates simple. Templates are never run directly. They do not need fallback logic.

**Prevention:**

1. Run `shellcheck` on all shell scripts.
2. Test on macOS before you merge (this is the primary target platform).
3. Avoid GNU-specific extensions: `%N`, `stat -c`, `sed -i`, `grep -P`.

### Test behavior, not scaffolding

**Problem:** Tests verified structure (functions exist, HTTP returns OK) but not actual behavior. A non-interactive worker subprocess died immediately, but the tests passed because they only checked:

- `test_bridge_starts`: bridge starts
- `test_hire_command`: HTTP returns "OK"
- `test_send_to_worker_function_exists`: functions exist

None of these tests verified that the worker stayed running or that it could receive messages.

**Rule:** Tests must verify the actual behavior that users care about. Do not test that code structure exists.

```bash
# BAD — tests scaffolding
test_bridge_starts() {
    curl -s /health/workers >/dev/null  # 200 OK, but does not prove workers run
}

test_workers_endpoint() {
    [[ $(curl -s /workers) == *"workers"* ]]  # Returns JSON but does not prove delivery!
}

test_send_to_worker_function_exists() {
    python3 -c "from bridge import send_to_worker; assert callable(send_to_worker)"
}

# GOOD — tests behavior
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

When tests pass but features are broken, you waste time debugging and lose trust in the test suite. Behavior tests catch real bugs. Scaffolding tests give false confidence.
