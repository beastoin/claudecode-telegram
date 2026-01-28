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
| `DOC.md` | Design philosophy, changelog |
| `TEST.md` | Testing documentation |

## Testing Requirements

### Every new feature MUST have a test

**This is non-negotiable.** When adding any new feature:

1. **Write the test first** (or alongside the feature)
2. **Focus on end-to-end tests** - test the full flow, not just units
3. **Add to `test.sh`** - all tests live in one file for simplicity
4. **Verify the test fails** without your feature (proves it tests something)
5. **Run full test suite** before committing

**Why e2e tests matter:**
- They catch integration bugs that unit tests miss
- They document how features actually work
- They give confidence when refactoring
- They're the safety net for this project

### Running tests

**Rule: Always run tests and ensure all pass before pushing.**

```bash
# Before any commit/push:
TEST_BOT_TOKEN='8117592253:AAE1vEf5WW1VJyzWD9iQg9A5A1xfEFOG8KU' TEST_CHAT_ID='121604706' ./test.sh
```

Alternative formats:
```bash
# Basic test (uses mock chat ID)
TELEGRAM_BOT_TOKEN='your-test-token' ./test.sh

# Full e2e test (sends real Telegram messages)
TELEGRAM_BOT_TOKEN='your-test-token' ADMIN_CHAT_ID='your-chat-id' ./test.sh
```

**Test isolation:** Tests run isolated from production under `~/.claude/telegram-test/` (separate port 8095, prefix `claudetest-`, separate PID file). You can run tests while production is active.

### Test coverage

| Category | What's tested |
|----------|---------------|
| Unit | imports, version command |
| Integration | all commands, admin security, session files, /response, /notify |

See `TEST.md` for detailed test documentation.

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

### NEVER use pkill on multi-node setups

**Problem:** `pkill -f cloudflared` or `pkill -f bridge.py` kills ALL matching processes across ALL nodes, not just the target node.

**Rule:** ALWAYS use PID-based killing, NEVER pattern-based.

```bash
# WRONG - kills ALL nodes
pkill -f cloudflared
pkill -f bridge.py

# RIGHT - use specific PID from file
kill $(cat ~/.claude/telegram/nodes/prod/pid)

# RIGHT - use port-specific
lsof -ti :8081 | xargs kill

# RIGHT - use the script's stop command
./claudecode-telegram.sh --node prod stop
```

**Why this matters:** Production runs multiple nodes (prod, dev, test) simultaneously. Pattern-based killing causes collateral damage to other running nodes.

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
