# LOOP2: Worker-Forge Architecture

## Goal

Forge runs workers from manifests and connects them to external systems.
The design separates AI engine setup from process runtime and connector IO.

## Non-Goals

- No plugin marketplace or external plugin SDK
- No database-backed orchestration
- No connector-specific engine hooks

## Core Architecture

```
Manifest → EngineDriver → Runtime → Connector
```

- **Manifest** declares what to run (files, creds, tools, readiness, hooks)
- **EngineDriver** materializes engine-specific config (settings.json, hooks, CLAUDE.md, boot command)
- **Runtime** owns process execution (tmux lifecycle, supervision, event routing)
- **Connector** owns external message transport (Telegram, Slack, bridge, etc.)

### Runtime Event Flow

```
Inbound:   Connector → Runtime.Send() → tmux stdin
Outbound:  Engine hook → curl to unix socket → HookListener → Connector.Send()
```

## Terminology

| Term | Meaning |
|------|---------|
| Engine | AI coding system abstraction (e.g., Claude Code) |
| EngineDriver | Go adapter: Prepare, StartSpec, Capabilities |
| ClaudeCodeDriver | First concrete driver |
| Runtime | Starts and supervises the AI process in tmux |
| Connector | Platform messaging boundary (Telegram, Slack, bridge) |
| Run | One worker execution instance |

## Principles

1. **Four clean layers** — Manifest, Engine, Runtime, Connector — each owns its concerns
2. **Engine doesn't know connectors** — ClaudeCodeDriver never imports telegram/slack/bridge
3. **Connectors don't know engines** — TelegramConnector never patches settings.json
4. **Runtime is the orchestrator** — calls Engine.Prepare(), Engine.StartSpec(), manages process
5. **Worker is standalone** — `./mon --connector telegram` needs only a bot token
6. **bridge.py is one connector type** — thin wrapper, not reimplemented
7. **Respect each platform's native pattern** — webhook, poll, stream, external injection
8. **Plug and play** — add connector = implement interface + register
9. **Capabilities and requirements declared as data** — host checks before starting
10. **Fail loudly** — no hidden retries, no silent drops

## EngineDriver Interface

```go
type EngineDriver interface {
    ID() string
    Prepare(ctx context.Context, req PrepareRequest) (*PreparedEngine, error)
    StartSpec(ctx context.Context, prepared *PreparedEngine) (*StartSpec, error)
    Capabilities() EngineCapabilities
}

type PrepareRequest struct {
    Manifest   *Manifest
    Vars       map[string]string
    RuntimeDir string
    GOOS       string
    Source     EmbeddedFileSource
}

type PreparedEngine struct {
    Env   map[string]string
    Files []PreparedFile
}

type PreparedFile struct {
    Path    string
    Content []byte
    Mode    os.FileMode
}

type StartSpec struct {
    Command []string
    Env     map[string]string
}

type EngineCapabilities struct {
    SupportsHooks       bool
    SupportsResume      bool
    SupportsPermissions bool
    HookEvents          []string
    InstructionFiles    []string
    ConfigFormat        string
}
```

Simple registry — no dynamic loading:

```go
func NewEngineDriver(kind string) (EngineDriver, error) {
    switch kind {
    case "claude-code":
        return &ClaudeCodeDriver{}, nil
    default:
        return nil, fmt.Errorf("unknown engine: %s", kind)
    }
}
```

## ClaudeCodeDriver

First implementation. Owns all Claude Code-specific logic:

**Prepare()** responsibilities:
- Install hook scripts from embedded source to `$CLAUDE_CONFIG_DIR/hooks/`
- Generate emit hook scripts for hooks without source (connector-agnostic curl to unix socket)
- Patch `settings.json` with hook registrations
- Write `.claude.json` onboarding marker
- Write `api-key-helper.sh` for API key injection
- Set engine-specific env vars (`FORGE_WORKER_NAME`, `FORGE_API_KEY_HELPER`)
- Return `PreparedEngine.Env` — merged into resolved vars by App.Run

**StartSpec()** returns:
- Command: `["claude", "--dangerously-skip-permissions"]` (or `--permission-mode dontAsk` for root)
- Env: engine-specific vars (ANTHROPIC_API_KEY, etc.)

**Does NOT know about:**
- Telegram, Slack, WhatsApp, bridge
- Connector tokens or URLs
- How messages are delivered

```go
type ClaudeCodeDriver struct{}

func (d *ClaudeCodeDriver) ID() string { return "claude-code" }

func (d *ClaudeCodeDriver) Capabilities() EngineCapabilities {
    return EngineCapabilities{
        SupportsHooks:       true,
        SupportsResume:      true,
        SupportsPermissions: true,
        HookEvents:          []string{"Stop", "SessionStart", "PreToolUse", "PostToolUse"},
        InstructionFiles:    []string{"CLAUDE.md", "AGENTS.md"},
        ConfigFormat:        "settings.json",
    }
}

func (d *ClaudeCodeDriver) Prepare(ctx context.Context, req PrepareRequest) (*PreparedEngine, error) {
    // 1. Install hook scripts from Source (errors if Source is nil but hook.Source is set)
    // 2. Generate emit hooks for sourceless hooks (curl-based, connector-agnostic)
    // 3. Patch settings.json
    // 4. Write onboarding marker
    // 5. Write API key helper
    // 6. Return env (FORGE_WORKER_NAME, FORGE_API_KEY_HELPER)
}

func (d *ClaudeCodeDriver) StartSpec(ctx context.Context, prepared *PreparedEngine) (*StartSpec, error) {
    // Return claude command + flags based on env
}
```

## Hook Event Routing

Two mechanisms for hook output delivery, both connector-agnostic:

### 1. `forge emit` subcommand (programmatic)

The worker binary includes a `forge emit` subcommand for programmatic use:

```bash
# From hook script or tool
./worker emit --event stop --payload-file "$BODY_FILE"
./worker emit --event stop --worker mon --socket /tmp/forge-mon.sock
```

`forge emit` reads stdin or --payload-file, posts JSON to the worker's unix socket.

### 2. Generated emit scripts (auto-generated by ClaudeCodeDriver)

For hooks declared in the manifest without a `source:` field, ClaudeCodeDriver generates lightweight bash scripts that curl directly to the unix socket. This avoids requiring the worker binary on PATH inside the tmux session.

```bash
#!/bin/bash
set -euo pipefail
SOCKET="/tmp/forge-{worker}.sock"
[ ! -S "$SOCKET" ] && exit 0
BODY=$(cat)
[ -z "$BODY" ] && exit 0
ESCAPED=$(printf '%s' "$BODY" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\r' '\a' | sed 's/\a/\\r/g' | tr '\n' '\a' | sed 's/\a/\\n/g')
curl -sf --unix-socket "$SOCKET" \
  -X POST http://forge/response \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"$ESCAPED\"}" >/dev/null 2>&1 || true
```

Worker names with slashes/spaces are sanitized to hyphens in socket paths.

### Hook source rules

| Manifest hook | What happens |
|--------------|-------------|
| `source:` set | Installed from embedded source, NOT auto-generated |
| `source:` empty | Auto-generated emit script written to `command:` path |

## Manifest Shape

Manifests declare worker needs. Engine type is implicit (currently always `claude-code`):

```yaml
name: mon
version: "1.0.0"

vars:
  ANTHROPIC_API_KEY:
    source: creds
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"

files:
  - source: ~/team/mon/charter.md
    dest: $HOME/team/mon/charter.md
  - source: ~/.claude/settings.json
    dest: $CLAUDE_CONFIG_DIR/settings.json
    merge: false

tools:
  - name: gcloud
    check: gcloud --version | head -1
    required: true

readiness:
  - name: gcloud-auth
    check: gcloud auth print-identity-token 2>/dev/null | head -c 20
    expect: "ey"
    required: true

hooks:
  - event: Stop
    command: $CLAUDE_CONFIG_DIR/hooks/send-response.sh
    source: ~/.claude/hooks/send-response.sh
```

Manifest readiness = work tools only. Never connector/bridge readiness.

## App.Run Flow

```go
func (a App) Run(opts RunOptions) error {
    // 1. Resolve vars (generic)
    // 2. Prepare dirs (generic)
    // 3. Extract files (generic)
    // 4. Engine.Prepare() → engine-specific config
    //    4a. Merge enginePrepared.Env into resolved vars
    // 5. Bootstrap tools (generic)
    // 6. Readiness checks (generic)
    // 7. ConfigureRuntime (sets tmux env from resolved + engine vars)
    // 8. Engine.StartSpec() → get launch command
    // 9. Runtime.Start()
    // 10. Start ConnectorHost (goroutine, errors surfaced)
    // 11. Run watchdog (races against connector errors)
}
```

### Engine env propagation

`Engine.Prepare()` returns `PreparedEngine.Env` with engine-specific vars (e.g., `FORGE_WORKER_NAME`, `FORGE_API_KEY_HELPER`). App.Run merges these into the resolved vars map **before** ConfigureRuntime, so they reach the tmux environment.

### Connector error handling

ConnectorHost.Run executes in a goroutine. Its error channel is raced against the watchdog — if the connector fails (init failure, webhook bind error), App.Run returns the error immediately instead of silently continuing.

## Runtime Responsibilities

Runtime owns:
- Parse manifest and select engine driver
- Call Prepare() before StartSpec()
- Start process/tmux session
- Track run state
- Process health and restart

Runtime does NOT own:
- How to patch settings.json (engine concern)
- How to launch Claude Code (engine concern)
- How to send to Telegram (connector concern)

## Connector Layer

Connectors implement the Connector interface with one receiver pattern.

### Connector Interface

```go
type Connector interface {
    Type() string
    Init(ctx context.Context, cfg ConnectorConfig) error
    Send(ctx context.Context, resp Response) error
    Capabilities() Caps
    Requirements() Reqs
    Close() error
}

type ConnectorConfig struct {
    WorkerName string
    BridgeURL  string
    ListenAddr string
    Config     map[string]string
    Runtime    Runtime   // Injected by ConnectorHost before Init
}
```

### Receiver Sub-Interfaces

Each connector implements exactly ONE:

| Pattern | Interface | How messages arrive | Used by |
|---------|-----------|-------------------|---------|
| External | ExternalReceiver | Bridge pushes into tmux | bridge |
| Webhook | PushReceiver | Platform POSTs to worker HTTP server | telegram |
| Poll | PollReceiver | Worker polls platform API | telegram-poll, whatsapp |
| Stream | StreamReceiver | Persistent WebSocket connection | slack |

### Connector Implementations

| Connector | Pattern | Status | Capabilities |
|-----------|---------|--------|-------------|
| bridge | ExternalReceiver | **Complete** | text, files, team discovery, worker-to-worker, markdown |
| telegram | PushReceiver | **Complete** | text, files, markdown, voice |
| telegram-poll | PollReceiver | **Complete** | text, files, markdown, voice |
| whatsapp | PollReceiver | Stub (outbound only) | text, files, templates, voice |
| slack | StreamReceiver | Stub (skeleton) | text, files, markdown, threads, multi-channel |
| local | PollReceiver | **Complete** | text (testing) |

### ConnectorHost

Orchestrates connector lifecycle. Detects receiver pattern and runs appropriate loop.

```go
type ConnectorHost struct {
    connector  Connector
    runtime    Runtime
    config     ConnectorConfig
    hookSocket *HookListener
}
```

ConnectorHost:
1. Checks connector requirements (ReqRuntime, ReqHTTPListener)
2. Injects Runtime into ConnectorConfig before Init
3. Starts HookListener for **all** connectors (including ExternalReceiver)
4. Detects receiver pattern and runs appropriate loop
5. Routes hook events through `connector.Send()`

### HookListener

Starts for **all** connector types. Even ExternalReceiver (bridge) gets a HookListener so generated emit hooks always have a target socket. This is necessary because:
- Generated emit hooks always curl to `/tmp/forge-{worker}.sock`
- Without a listener, those curls would silently fail
- The HookListener routes through `connector.Send()` which is always implemented

## Event Socket (Hook IPC)

```
Claude hook fires → hook script → curl --unix-socket /tmp/forge-{worker}.sock
    → HookListener receives → connector.Send() → platform
```

One socket per worker. Managed by ConnectorHost. Works identically regardless of which connector is active.

Routes:
- `POST /response` — hook output (typically Stop event response)
- `GET /health` — liveness check

Socket path: `/tmp/forge-{sanitized-worker-name}.sock` (slashes/spaces → hyphens, permissions 0600)

## Security / Isolation

- Connector tokens never enter EngineDriver
- Engine API keys never enter Connector
- Event socket 0600
- Hook scripts 0755
- Settings files 0600
- Config/hook dirs 0755 (Claude Code requires read access)

## Usage

Subcommand syntax only. Flag resolution: flag > env var > state file > manifest default.

```bash
# First run on new host (identity saved to state file for future use)
./mon run --identity ~/.age/forge.key --bridge-url http://bridge:8271

# Subsequent runs (identity auto-loaded)
./mon run --bridge-url http://bridge:8271

# Bridge connector (explicit)
./mon run --connector bridge --bridge-url http://bridge:8271

# Direct Telegram via polling (token from creds bundle — zero connector-opts)
./mon run --connector telegram-poll

# Direct Telegram with explicit opts (override creds bundle)
./mon run --connector telegram-poll --connector-opt TELEGRAM_BOT_TOKEN=$TOKEN --connector-opt TELEGRAM_CHAT_ID=12345

# Direct Telegram via webhook (token from creds bundle)
./mon run --connector telegram

# Local testing
./mon run --connector local

# Management (zero flags — state file has session info)
./mon health
./mon stop

# Readiness check (human-readable or JSON)
./mon check
./mon check --output-json

# Verify file integrity
./mon verify --output-json

# First-time setup (with preview)
./mon onboard --dry-run
./mon onboard --identity ~/.age/forge.key

# Self-describe (JSON schema of all commands)
./mon describe
./mon describe check

# Version
./mon version

# Env var fallbacks (useful in containers/CI)
FORGE_BRIDGE_URL=http://... FORGE_OUTPUT_JSON=1 ./mon check
```

## Project Structure

```
forge/                            # App orchestration, CLI, lifecycle, cross-cutting
├── app.go                        # App struct, Run(), wiring
├── cli.go                        # CLI flag parsing
├── worker_cli.go                 # Subcommand dispatch
├── prepare.go                    # Phase 0: resolve vars, create dirs
├── extract.go                    # Phase 1: write embedded files
├── readiness.go                  # Phase 3: readiness checks + auto-fix
├── check.go                      # Check command output
├── verify.go                     # Verify command + integrity types
├── integrity.go                  # Integrity monitoring
├── secrets.go                    # Age decryption
├── isolated.go                   # Container mode
├── upgrade.go                    # Self-upgrade
├── auth.go                       # AuthCoordinator (cross-cutting wiring)
├── emit.go                       # forge emit subcommand
├── describe.go                   # Describe command — JSON schema
├── hooks.go                      # HookManager (legacy fallback)
├── transport.go                  # GRPCTransport (lifecycle/wiring)
├── deps.go                       # Dependency graph documentation
├── SKILL.md                      # Agent-friendly skill doc
│
├── manifest/                     # Pure data types (leaf — no forge imports)
│   └── manifest.go               # Manifest, VarSpec, FileSpec, ToolSpec, HookSpec
│
├── protocol/                     # Wire format data types (leaf — no forge imports)
│   └── types.go                  # RegisterRequest, RegisterResponse, WorkerMessage, JSONLChunk
│
├── runtime/                      # Process execution, supervision
│   ├── runtime.go                # Runtime, RuntimeMonitor, LaunchCommander interfaces
│   ├── tmux.go                   # TmuxRuntime implementation
│   ├── shell_runner.go           # ShellRunner implementation
│   └── bootstrap.go              # Tool check/install
│
├── engine/                       # AI engine abstraction
│   ├── engine.go                 # EngineDriver interface, registry, types
│   └── engine_claude.go          # ClaudeCodeDriver implementation
│
├── connector/                    # Platform I/O, message routing
│   ├── connector.go              # Connector interface, sub-interfaces, registry
│   ├── connector_host.go         # ConnectorHost orchestrator
│   ├── connector_hook.go         # HookListener — Unix socket IPC
│   ├── connector_bridge.go       # BridgeConnector (ExternalReceiver)
│   ├── connector_telegram.go     # TelegramConnector (PushReceiver)
│   ├── connector_telegram_poll.go # TelegramPollConnector (PollReceiver)
│   ├── connector_slack.go        # SlackConnector (StreamReceiver) — stub
│   ├── connector_whatsapp.go     # WhatsAppConnector (PollReceiver) — stub
│   ├── connector_local.go        # LocalConnector (testing)
│   ├── connector_web.go          # WebConnector + templates + markdown
│   └── commands.go               # RegisterBuiltinCommands, CommandServices
│
├── watchdog/                     # Health monitoring
│   └── watchdog.go               # Watchdog, RestartPolicy, ExponentialBackoffPolicy
│
└── build/                        # Worker binary construction (worker-forge only)
    └── build.go                  # ScaffoldWorkerDir, BuildWorkerBinary, checksums
```

Dependency graph:
```
manifest/   → nothing
protocol/   → nothing
runtime/    → manifest/
engine/     → manifest/, runtime/
connector/  → manifest/, runtime/, protocol/
watchdog/   → nothing (consumer-defined interfaces)
build/      → manifest/
root        → everything (wiring)
```

## Increment Plan

1. ✅ **Add engine interface + ClaudeCodeDriver** — `engine.go`, `engine_claude.go`, tests
2. ✅ **Move Claude-specific logic into driver** — launch command from tmux.go, settings.json patching from hooks.go
3. ✅ **Update App.Run to use EngineDriver** — call Prepare() then StartSpec()
4. ✅ **Generate connector-agnostic emit hooks** — curl-based scripts for hooks without source
5. ✅ **forge emit subcommand** — programmatic hook event routing via worker binary
6. ✅ **Clean up TmuxRuntime** — remove Claude-specific defaultLaunchCommand, now returns `bash`
7. ✅ **Boundary tests** — verify connector doesn't import engine, engine doesn't import connector
8. ✅ **Engine env propagation** — merge PreparedEngine.Env into resolved vars before ConfigureRuntime
9. ✅ **Runtime injection** — ConnectorHost injects Runtime into ConnectorConfig before Init
10. ✅ **Connector error surfacing** — ConnectorHost errors raced against watchdog
11. ✅ **Nil source guard** — installHookScripts rejects hooks with Source but no embedded source
12. ✅ **HookListener for all connectors** — including ExternalReceiver
13. ✅ **Socket path sanitization** — worker names with slashes/spaces → hyphens
14. ✅ **Subcommand parsing** — `./mon check` alongside legacy `./mon --check`
15. ✅ **Structured JSON output** — `check --output-json`, `verify --output-json` with per-item IDs and severity
16. ✅ **Describe command** — `./mon describe [cmd]` returns JSON schema of all commands/flags
17. ✅ **Dry-run** — `onboard --dry-run` previews extraction without writing
18. ✅ **Env var fallbacks** — `FORGE_BRIDGE_URL`, `FORGE_IDENTITY`, `FORGE_CONNECTOR`, `FORGE_OUTPUT_JSON`
19. ✅ **Version command** — `./mon version` prints worker name and version
20. ✅ **SKILL.md** — agent-friendly reference doc for forge commands
21. ✅ **Auth onboarding** — connector-agnostic AuthCoordinator with Observe() pattern, AuthPrompter sub-interface, ConnectorHost integration, reply-to matching
22. ✅ **Auth subcommand** — `./mon auth` explicit repair/reauth command using shared coordinator

## Migration Notes

### Legacy Transport + Connector coexistence

During the transition from the legacy Transport-based bridge to the Connector layer, both paths are active in App.Run:

- `Transport`: legacy path — registers with bridge, handles watchdog re-registration, sends worker messages
- `Connector + ConnectorHost`: new path — handles connector lifecycle, hook listener, inbound/outbound

Both paths can be active simultaneously (e.g., `--connector bridge --bridge-url ...` uses both). The legacy Transport handles watchdog re-registration while the Connector handles structured inbound/outbound. This will converge when the Transport layer is fully replaced.

### HookManager vs EngineDriver

When `App.Engine` is set, EngineDriver.Prepare() handles hook installation and settings patching. When `App.Engine` is nil, the legacy `HookManager` is used as a fallback. New code should always use EngineDriver.

## Auth Onboarding (Connector-Agnostic OAuth)

When a forge worker launches Claude Code in an isolated environment (e.g., beast host user),
Claude Code needs OAuth authentication on first run. The auth coordinator is integrated into
ConnectorHost and uses the connector's native UX for auth prompts and code collection.

### Architecture

Auth is a **connector sub-interface** (`AuthPrompter`), not a separate system. Each connector
translates auth into its platform's native UX:

| Connector | UX Pattern | How code comes back |
|-----------|-----------|-------------------|
| Telegram | `force_reply` + reply-to | Manager replies to auth message |
| Slack | Thread reply | Reply in thread |
| Local | HTTP POST /send | POST the code |
| Bridge | Falls back to plain Send | Any next message |

### Flow

```
ConnectorHost.Run()
  → Init connector
  → Start auth goroutine (polls capture-pane for URL)
  → Start receive loop (poll/stream/webhook)
  → Auth detects URL → sends via AuthPrompter (force_reply etc.)
  → Receive loop feeds Observe() for every inbound message
  → Observe() matches reply-to message ID → delivers code to auth
  → Auth submits code to runtime (tmux send-keys)
  → Auth completes, receive loop continues normally
```

### Key Types

```go
// Optional connector sub-interface for native auth UX
type AuthPrompter interface {
    SendAuthPrompt(ctx context.Context, req AuthPromptRequest) (AuthPromptResult, error)
}

// AuthCoordinator integrates with ConnectorHost
type AuthCoordinator struct {
    Runtime     RuntimeMonitor
    WorkerName  string
    // Observe() called by ConnectorHost for every inbound message
    // Returns true if consumed (auth code matched)
}
```

### Observe Matching

ConnectorHost calls `auth.Observe(msg)` before `deliverInbound(msg)`:

1. **Reply-to match**: `msg.ReplyToID == promptMsgID` (primary, works with force_reply)
2. **Fallback match**: when no prompt message ID, accept code-like text (no spaces, 4-128 chars)

When a prompt message ID exists (Telegram, Slack), ONLY reply-to matching is used.
This prevents accidental code submission from random messages.

### InboundMessage Fields

`MessageID` and `ReplyToID` are string fields on InboundMessage, populated by connectors:
- Telegram: `msg.MessageID` from `message_id`, `msg.ReplyToID` from `reply_to_message.message_id`
- Other connectors: populate as appropriate for their platform

### ConnectorHost Integration

```go
type ConnectorHost struct {
    Auth     *AuthCoordinator  // optional
    AuthOnly bool              // stop after auth completes
}
```

- Auth runs as a goroutine inside ConnectorHost.Run()
- The receive loop (poll/stream/webhook) runs concurrently
- In AuthOnly mode: receive loop runs in background, Run() returns when auth completes

### Detection Strategy

1. **Behavioral**: after Runtime.Start(), poll `capture-pane -p -J -S -2000` for OAuth URL pattern
2. **Login/theme auto-handling**: detect "Select login method" / "Choose the text style" and send Enter
3. **Already authed**: detect normal prompt ("What can I help you with?") → skip auth

### Timing

- Poll interval: 500ms for first 30s, then 1s
- URL detection timeout: 3 minutes
- Code entry timeout: 15 minutes

### Security

- Codes never logged
- Only accept codes in waiting-for-code state
- With prompt message ID, strict reply-to matching prevents accidental submission
- Short-lived attempts with explicit timeouts

### Commands

```bash
# Auto-detect during run
./mon run --connector telegram-poll --connector-opt TELEGRAM_BOT_TOKEN=$TOKEN ...

# Explicit auth command
./mon auth --connector telegram-poll --connector-opt TELEGRAM_BOT_TOKEN=$TOKEN ...
```

### File Layout

```
forge/
├── auth.go                     # AuthCoordinator, Observe(), state machine (cross-cutting — root)
├── auth_test.go                # Observe matching, URL detection tests

forge/connector/
├── connector.go                # AuthPrompter interface, AuthPromptRequest/Result types
├── connector_host.go           # Auth/AuthOnly fields, Observe wiring in deliverInbound
├── connector_telegram_poll.go  # SendAuthPrompt with force_reply, ReplyToID parsing
├── connector_telegram.go       # SendAuthPrompt with force_reply (webhook mode)
├── connector_local.go          # SendAuthPrompt (plain text fallback)
```

## Key Decisions

| Decision | Why |
|----------|-----|
| Engine as separate layer | Claude-specific logic was scattered across tmux.go, hooks.go, worker_cli.go |
| Thin interface (Prepare + StartSpec) | Runtime only needs two things: set up config, get start command |
| Curl-based emit hooks | Simpler than requiring worker binary on PATH inside tmux; works in all environments |
| `forge emit` subcommand | Programmatic alternative for hook scripts that prefer binary over curl |
| Simple switch registry | ~20 workers, one engine type today; no need for dynamic plugin loading |
| Event socket per worker | Simple IPC; works for both bridge and direct connectors |
| HookListener for all connectors | Generated emit hooks always target the socket; need a listener even for ExternalReceiver |
| Manifest doesn't name engine | Currently always claude-code; add `engine:` field when second engine arrives |
| Engine env → resolved vars | Engine vars (FORGE_WORKER_NAME) must reach tmux env for hook scripts to work |
| ConnectorConfig.Runtime | Connectors that need Runtime (webhook delivery) receive it through config injection |

## Testing

```bash
# Unit tests (269+)
go test ./... -count=1

# E2E behavioral tests — 3 connectors, 31 assertions
bash test-e2e-connectors.sh

# Individual connector verification
bash test-e2e-connectors.sh  # local (10) + bridge (14) + telegram-poll (7)
```
