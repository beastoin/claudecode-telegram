# Forge Worker Skill

## When To Use
Use Forge when building, validating, onboarding, running, or debugging standalone Claude Code worker binaries. Each binary embeds its manifest, config files, encrypted credentials, and hooks.

## Two Binaries

| Binary | Purpose |
|--------|---------|
| `worker-forge` | Build tool — compiles worker binaries from manifests |
| `./mon` (built worker) | Runtime — the standalone worker binary |

## Quick Start

```bash
# Build a worker (auto-sources cred files from manifest)
worker-forge build mon --manifest workers/mon/manifest.yaml --identity ~/.age/forge.key

# Deploy to host
scp mon-linux-amd64 host:~/mon && scp ~/.age/forge.key host:~/.age-key

# First run (saves identity for future use)
./mon run --identity ~/.age-key --bridge-url http://bridge:8271

# Subsequent runs (identity remembered, bridge-url via env or flag)
./mon run --bridge-url http://bridge:8271

# Management (zero flags)
./mon health
./mon stop
```

## Design Principle: Say It Once

Forge remembers settings so you don't repeat flags:

1. **`--identity`** — saved to `.forge-state.json` after first use. All subsequent commands auto-load it.
2. **`--bridge-url`** — required for bridge connector. Use `FORGE_BRIDGE_URL` env var to avoid repeating.
3. **`--skip-conflicts`** — default behavior on `run`. Use `--force-extract` to override.
4. **`--session-prefix`** — comes from manifest `TMUX_PREFIX` or bridge `/register`. Rarely needed as a flag.

## Commands

### Inspect Command Schema
```bash
./mon describe           # all commands with flags and types
./mon describe check     # schema for a single command
```

### Check Readiness
```bash
./mon check --output-json
```
Returns structured JSON with per-check pass/fail/warn, stable IDs (`tool.<name>`, `readiness.<name>`), and severity.

### Verify Integrity
```bash
./mon verify --output-json
```
Returns JSON with per-file pass/skip status against embedded checksums.

### Onboard
```bash
./mon onboard --dry-run    # preview
./mon onboard              # execute (identity auto-loaded from state)
```

### Auth (OAuth Onboarding)
```bash
# Auto-detected during run (token from creds bundle)
./mon run --connector telegram-poll

# Explicit auth/reauth
./mon auth --connector telegram-poll
```
Detects missing Claude Code OAuth tokens, sends auth URL to manager via connector, waits for code, completes auth, then transitions to normal worker mode.

### Connector Options Resolution

Connector options (bot tokens, chat IDs, etc.) resolve from:
1. Explicit `--connector-opt KEY=VALUE` (highest priority)
2. Manifest creds bundle (embedded encrypted values)

If `TELEGRAM_BOT_TOKEN` is in the creds bundle, no `--connector-opt` needed.

### Run / Stop / Health
```bash
./mon run --bridge-url http://bridge:8271   # identity auto-loaded from state
./mon health
./mon stop
```

### Version
```bash
./mon version
```

## JSON Rules
- Use `--output-json` for agent automation.
- Parse stdout only — stderr is diagnostics.
- Exit code 0 = success, 1 = runtime error, 2 = bad input.

## Environment Variables
Flags > env vars > state file > manifest defaults.

| Variable | Overrides |
|----------|-----------|
| `FORGE_BRIDGE_URL` | `--bridge-url` |
| `FORGE_IDENTITY` | `--identity` |
| `FORGE_CONNECTOR` | `--connector` |
| `FORGE_OUTPUT_JSON=1` | `--output-json` |

## Common Check IDs and Fixes

| Check ID | Fix |
|----------|-----|
| `tool.claude` | Install Claude Code CLI |
| `tool.tmux` | `apt install tmux` |
| `readiness.bridge-reachable` | Start bridge or fix `--bridge-url` |

## Safety
- Use `--dry-run` before `onboard` to preview changes.
- Do not expose decrypted credentials (age identity keys).
- Run `verify` before `run` after upgrades to check file integrity.
- `--force-extract` overwrites existing files — default `run` behavior is skip-conflicts.
