# claudecode-telegram Agent Guide

These rules apply to every AI agent that works in this repository.
This file is the single source of truth for agent workflow.
`CLAUDE.md` points here.

## Doc Contract

Authority flows downstream: `SPEC.md → AGENTS.md → Code → Tests`. Citations point upstream only (`[SPEC-NNN]`). Manager owns SPEC.md. Agent owns AGENTS.md, code, and tests. When code diverges from spec, STOP and notify manager. See SPEC.md "Documentation Contract" for the full rules.

## Version Management

Do these three steps when you make changes that need a new version:

1. Update the version in `bridge.sh`:
   ```bash
   VERSION="x.y.z"
   ```

2. Update `SPEC.md` and `CHANGELOG.md`:
   - Set the new version number in the SPEC.md header.
   - Add a changelog entry in `CHANGELOG.md`. Describe breaking changes, new features, and architecture changes.
   - Update the design philosophy sections in SPEC.md if core principles changed.

3. Run the acceptance tests before you commit:
   ```bash
   TEST_BOT_TOKEN='...' ./test.sh
   ```
   See `TEST.md` for the full testing documentation.

### When to Bump the Version

- **Patch (0.0.x):** Bug fixes and minor corrections.
- **Minor (0.x.0):** New features and backward-compatible changes.
- **Major (x.0.0):** Breaking changes and architecture overhauls.

## Update Surfaces

When you change a message path (inbound, outbound, worker-to-worker, connector, management, or error), update the **Message Flow Map** below — it is the audit surface.

## Message Flow Map

Every path a message takes through the system. Check here before changing any message behavior.

### Inbound: Telegram → Worker

| Flow | Trigger | Behavior |
|------|---------|----------|
| Hire worker | `/hire <name>` | Creates tmux session, registers worker, sets focus |
| Bare message | Text (no `/` or `@`) | 👀 react, `tmux send-keys` to focused worker |
| @mention | `@name <msg>` | 👀 react, one-off delivery, focus unchanged |
| @all broadcast | `@all <msg>` | 👀 react, delivered to all active workers |
| Implicit `/name` | Bare `<name>` as entire message | Treated as `/focus <name>` if worker exists |
| Reply-to | Telegram reply to a worker message | Routes to originating worker |
| Focus switch | `/focus <name>` | Changes active worker |
| File/image | Telegram attachment | Downloads to temp, path injected into message |
| Non-admin | Message from unknown chat_id | Silently rejected |

### Outbound: Worker → Telegram

| Flow | Trigger | Behavior |
|------|---------|----------|
| Worker reply | Claude stop hook fires | `send-to-telegram.sh` → `POST /response` → `name: <text>` in Telegram |
| Media tags | `[[image:/path\|caption]]` in output | Hook sends raw text; bridge parses and sends via `sendPhoto`/`sendDocument` |
| Long reply | Output > 4096 chars | Bridge splits into multiple messages, preserves code blocks |
| Proactive message | Worker outputs without pending request | Hook sends if `chat_id` file exists |

### Worker ↔ Worker

| Flow | Trigger | Behavior |
|------|---------|----------|
| tmux send | Worker calls send command from `/workers` | `flock` + `tmux paste-buffer` (serialized) |
| Pipe send | Worker writes to named pipe | `echo "msg" > /tmp/claudecode-telegram/<node>/<worker>/in.pipe` |
| Cross-machine | Worker calls SSH + send command | SSH wraps the tmux send |

### Connector Inbound

| Flow | Trigger | Behavior |
|------|---------|----------|
| Gmail | New email from allowed sender | Connector polls → bridge → Telegram + workers |
| GitHub | New comment from allowed user | Connector polls → bridge → Telegram + workers |
| Blocked sender | Email/comment from non-allowed sender | Silently dropped (fail-closed allowlist) |

### Management

| Flow | Trigger | Behavior |
|------|---------|----------|
| Team status | `/team` | Scans tmux sessions, health state per worker |
| Worker restart | `/restart <name>` | Kills + restarts tmux session |
| Teleport | `/teleport <name> <host>` | Syncs state, starts on target, stops source |
| Remote register | `POST /register` | Registers pre-existing worker session from remote host |
| End worker | `/end <name>` | Kills tmux session, removes from registry |
| Bridge restart | Process restart | Scans tmux + reads `workers.json`, recovers all workers |

### Error Paths

| Flow | Trigger | Behavior |
|------|---------|----------|
| Session missing | Message to non-existent worker | Bridge replies with error in Telegram |
| Worker dead | tmux session exited | Health check marks EXITED, visible in `/team` |
| Hook no chat_id | Hook fires but no `chat_id` file | Hook exits silently |
| Stale transcript | JSONL not updated recently | Hook sends health alert to `/health-alert` endpoint |

## Testing Requirements

- Write tests alongside features. Focus on end-to-end behavior, not scaffolding [SPEC-021].
- Use FAST mode during development. Run default mode before you commit. Run FULL mode before you push.
- Follow TDD: Red-Green-Refactor per increment.
- See `TEST.md` for mode definitions, commands, env vars, TDD workflow, and test inventories.

## Learnings

These capture operational gotchas not covered by SPEC.md. For spec-level rules, see `SPEC.md` directly.

### Env var propagation [SPEC-022]

Export env vars explicitly at each process boundary. Use `tmux set-environment`, not `tmux send-keys "export ..."`. Check all entry points: `hire()`, `restart()`, `_restart_dead_worker()`, `POST /register`.

### Audit all code paths when adding config [SPEC-022]

When you make something configurable, search for all usages of the old hardcoded value. Verify all entry points handle the option consistently.

### Watchdog for bridge requires careful testing

Only the tunnel watchdog exists (v0.5.0). A bridge watchdog was attempted but reverted. If re-implementing: test manually first, use `stdbuf -oL` for unbuffered output, add delays between kill and port check, make `start_bridge()` pass all required env vars.

### Node credentials [SPEC-006]

Token env files live at `~/.config/claudecode-telegram/<node>.env`. Use them for restarts:
```bash
source ~/.config/claudecode-telegram/prod.env
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" ./bridge.sh --node prod --no-sandbox run
```

### tmux send race condition [SPEC-001]

Concurrent sends to the same tmux session interleave. Fix: per-session locks in `tmux_send_message()` serialize sends. Two subprocess calls (`send-keys -l text`, `send-keys Enter`) are not atomic.

### macOS vs Linux shell compatibility

| Operation | Linux (GNU) | macOS (BSD) |
|-----------|-------------|-------------|
| File size | `stat -c %s file` | `stat -f%z file` |
| Milliseconds | `date +%s%3N` | Not supported |
| sed in-place | `sed -i 's/a/b/'` | `sed -i '' 's/a/b/'` |
| grep -P | Supported | Not supported (use `grep -E`) |

Always use portable alternatives or a try-fallback pattern.

### Sed placeholders in conditionals

`sed` substitution is global — it replaces the placeholder in comparison strings too. Don't use conditionals in templates that reference the placeholder. Bake values directly.

### Test behavior, not scaffolding [SPEC-021]

Tests must verify actual behavior users care about. Don't test that code structure exists.

```bash
# BAD — tests scaffolding
test_bridge_starts() {
    curl -s /health/workers >/dev/null  # 200 OK, but doesn't prove workers run
}

# GOOD — tests behavior
test_tmux_mode_session_stays_alive() {
    curl -s /hire  # Create worker
    sleep 3
    tmux has-session -t claude-test-worker  # Still running?
}
```
