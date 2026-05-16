# Worker-Forge: Standalone Worker Binary

## Overview

Every worker is a single Go binary — `./mon` — with files (knowledge, skills, memory, creds) and a manifest burned in. The binary manages its own tmux session, Claude CLI, hooks, and watchdog. It connects to the bridge via gRPC over Tailscale for message routing only. Works on any host — same machine as bridge or cross-host.

## Core Principles

1. **Standalone binary** — single `./mon` file, run anywhere
2. **Self-upgrade** — worker rebuilds itself when manager provides the build key
3. **Self-managing** — binary owns tmux, Claude CLI, hooks, watchdog
4. **Bridge is the hub** — all message routing goes through bridge via gRPC

## Startup Phases

```
./mon [--bridge-url URL] [--session NAME]

Phase 0: PREPARATION
  Resolve manifest variables → know where everything goes

Phase 1: EXTRACT
  Write embedded files to resolved paths

Phase 2: BOOTSTRAP
  Check + install required tools

Phase 3: READINESS
  Verify environment is ready to work

Phase 4: CONNECT
  Start runtime (tmux or terminal), spawn Claude, install hooks
  Connect to bridge via gRPC, start working

Phase 5: WATCH
  Ongoing health monitoring
```

### Runtime Modes

The binary uses a `Runtime` interface so the input mechanism is swappable. Today only tmux is implemented — future runtimes (direct PTY/stdin, programmatic API) plug in here.

| Flag | Behavior | Persistence |
|------|----------|-------------|
| *(default)* | Create new tmux session (`claude-prod-mon`) | tmux (survives disconnect) |
| `--session NAME` | Adopt existing tmux session, rename + take ownership | tmux (survives disconnect) |

**`--session`**: useful for migrating live workers. Binary attaches to the named session, renames it to its convention if needed, and takes ownership. No restart, no downtime.

**Why tmux for now**: the bridge injects messages via `tmux send-keys` (paste into pane). Without tmux, we'd need an alternative input path (stdin pipe, programmatic API). The `Runtime` interface keeps the door open without solving that problem today.

## Phase 0: Preparation

Each worker declares its OWN vars and dirs. Different workers need different things — mon needs GCP/kubectl/Codemagic, luck needs TEAM_DIR/GWS/SSH keys, lee needs BRIDGE_REPO. The manifest is the worker's declaration of what it needs to function.

### How Preparation Works

```
1. Read vars from manifest
2. For each var:
   - source: env → read from environment ($HOME, $PATH, etc.)
   - source: flag → read from CLI flag (--bridge-url, etc.)
   - source: creds → extracted from encrypted creds bundle
   - source: default → expand using already-resolved vars
   - required: true → STOP if not resolved
3. Create dirs listed in dirs: section
4. All subsequent phases use resolved vars — never hardcoded paths
```

### Var Sources

| Source | Meaning | Example |
|--------|---------|---------|
| `env` | Read from host environment | HOME, PATH |
| `flag` | CLI flag or env override | --bridge-url, BRIDGE_URL= |
| `creds` | From encrypted creds bundle | ANTHROPIC_API_KEY, GH_TOKEN |
| `default` | Derived from other vars | $HOME/.config/gcloud |

### Overrides

Any var can be overridden via env var or CLI flag:

```bash
TEAM_DIR=/opt/team ./mon --bridge-url http://100.125.36.102:8271
```

## Worker Manifests (Real Examples)

Each worker declares exactly what THEY need. These came from the workers themselves.

### mon (Ops: DevOps + ProdOps)

```yaml
name: mon
version: 1.0.0

vars:
  HOME:
    source: env
    required: true
    description: "home directory — everything anchors off this"
  MON_WORK_DIR:
    source: flag
    default: "$HOME/ops"
    required: true
    description: "primary working directory — data cache, reports, charts"
  BRIDGE_URL:
    source: env
    required: true
    description: "bridge URL for messaging"
  TZ:
    source: default
    default: "UTC"
    required: true
    description: "all timestamps, API queries, BQ windows must be UTC"
  MONITOR_ENV:
    source: default
    default: "$HOME/.config/omi/monitor/.env"
    required: true
    description: "env file with 31 API keys (Stripe, Mixpanel, Shopify, Deepgram, Grafana, Anthropic)"
  AGENT_ENV:
    source: default
    default: "$HOME/.config/claudecode-telegram/agent.env"
    required: true
    description: "env file with GH_TOKEN, bridge config"
  GOOGLE_APPLICATION_CREDENTIALS:
    source: default
    default: "$HOME/.config/gcloud/beastoin-agents.json"
    required: true
    description: "GCP SA key — gcloud, kubectl, BQ, Firestore, Cloud Logging"
  KUBECONFIG:
    source: default
    default: "$HOME/.kube/config"
    required: true
    description: "kubectl config with prod-omi-gke context"
  JAVA_HOME:
    source: default
    default: "$HOME/tools/jdk-21.0.6+7"
    required: false
    description: "JDK for Android SDK tools"
  ANDROID_HOME:
    source: default
    default: "$HOME/Android/Sdk"
    required: false
    description: "Android SDK for adb/emulator screenshots"
  CLAUDE_PROJECTS_DIR:
    source: default
    default: "$HOME/.claude/projects"
    required: true
    description: "Claude Code project memory/settings root"
  CLAUDE_PROJECT_SLUG:
    source: flag
    default: "-home-claude-ops"
    required: true
    description: "Claude Code project slug (mangled cwd path, e.g. -home-claude-ops for /home/claude/ops)"
  CM_COOKIE_PATH:
    source: default
    default: "/tmp/cm-cookie.txt"
    required: false
    description: "Codemagic session cookie for build API"

dirs:
  - $MON_WORK_DIR
  - $MON_WORK_DIR/data
  - $HOME/.config/omi/monitor
  - $HOME/.config/claudecode-telegram
  - $HOME/.config/gcloud
  - $HOME/.config/gcloud/configurations
  - $HOME/.kube
  - $HOME/team/mon
  - $HOME/team/mon/incidents

# --- Phase 1: Extract (build-time source → runtime dest using worker's vars) ---
# All files use the same format. merge: true = keep disk version if newer.
# Creds are age-encrypted in the binary; everything else is plaintext embed.
files:
  # knowledge
  - source: ~/team/mon/charter.md
    dest: $HOME/team/mon/charter.md
  - source: ~/team/mon/playbook.md
    dest: $HOME/team/mon/playbook.md
  - source: ~/team/mon/kanban.md
    dest: $HOME/team/mon/kanban.md
  - source: ~/team/mon/current_state.md
    dest: $HOME/team/mon/current_state.md
  - source: ~/team/mon/soul.md
    dest: $HOME/team/mon/soul.md
  - source: ~/team/playbook.md
    dest: $HOME/team/playbook.md
  - source: ~/team/learnings.md
    dest: $HOME/team/learnings.md
  - source: ~/team/checkin-note.txt
    dest: $HOME/team/checkin-note.txt
  - source: ~/.claude/CLAUDE.md
    dest: $HOME/.claude/CLAUDE.md

  # skills
  - source: ~/.claude/skills/mon-daily-ops-email/
    dest: $HOME/.claude/skills/mon-daily-ops-email/
  - source: ~/.claude/skills/omi-pr-workflow/
    dest: $HOME/.claude/skills/omi-pr-workflow/
  - source: ~/.claude/skills/omi-prod-deploy-monitor/
    dest: $HOME/.claude/skills/omi-prod-deploy-monitor/
  - source: ~/.claude/skills/omi-incident-detection/
    dest: $HOME/.claude/skills/omi-incident-detection/
  - source: ~/.claude/skills/concise-cto-report/
    dest: $HOME/.claude/skills/concise-cto-report/
  - source: ~/.claude/skills/omi-monthly-report/
    dest: $HOME/.claude/skills/omi-monthly-report/
  - source: ~/.claude/skills/mon-weekly-cto-email/
    dest: $HOME/.claude/skills/mon-weekly-cto-email/

  # memory (merge: keep disk if newer than embedded snapshot)
  - source: ~/.claude/projects/-home-claude-ops/memory/
    dest: $CLAUDE_PROJECTS_DIR/$CLAUDE_PROJECT_SLUG/memory/
    merge: true

  # creds (age-encrypted in binary)
  - source: ~/.config/omi/monitor/.env
    dest: $MONITOR_ENV
    encrypted: true
  - source: ~/.config/claudecode-telegram/agent.env
    dest: $AGENT_ENV
    encrypted: true
  - source: ~/.config/gcloud/beastoin-agents.json
    dest: $GOOGLE_APPLICATION_CREDENTIALS
    encrypted: true
  - source: ~/.kube/config
    dest: $KUBECONFIG
    encrypted: true

# --- Phase 2: Bootstrap ---
tools:
  - name: claude
    check: claude --version
    install:
      linux: npm install -g @anthropic-ai/claude-code
      darwin: npm install -g @anthropic-ai/claude-code
    required: true
  - name: gcloud
    check: gcloud --version
    install:
      linux: curl -sSL https://sdk.cloud.google.com | bash
      darwin: brew install --cask google-cloud-sdk
    required: true
  - name: kubectl
    check: kubectl version --client
    install:
      linux: gcloud components install kubectl
      darwin: gcloud components install kubectl
    required: true
  - name: gh
    check: gh --version
    install:
      linux: apt install -y gh
      darwin: brew install gh
    required: true
  - name: gws
    check: gws --version
    required: false
  - name: tmux
    check: tmux -V
    install:
      linux: apt install -y tmux
      darwin: brew install tmux
    required: true

# --- Phase 3: Readiness ---
readiness:
  - name: gcp-auth
    check: gcloud auth list --format="value(account)"
    expect: contains "beastoin-agents"
    fix: gcloud auth activate-service-account --key-file=$GOOGLE_APPLICATION_CREDENTIALS
    required: true
  - name: gke-cluster
    check: kubectl cluster-info
    expect: exit 0
    fix: gcloud container clusters get-credentials prod-omi-gke --region us-central1 --project based-hardware
    required: true
  - name: github-auth
    check: gh auth status
    expect: exit 0
    fix: gh auth login --with-token <<< $GH_TOKEN
    required: true
  - name: bridge-reachable
    check: curl -sf $BRIDGE_URL/health
    expect: exit 0
    required: true
  - name: ssh-mac-mini
    check: ssh -o ConnectTimeout=3 beastoin-agents-f1-mac-mini echo ok
    expect: exit 0
    required: false
```

### luck (PeopleOps: wrap-ups, worker management, identity)

```yaml
name: luck
version: 1.0.0

vars:
  HOME:
    source: env
    required: true
    description: "User home directory"
  WORKER_NAME:
    source: default
    default: "luck"
    required: true
    description: "My identity — bridge registration, tmux naming, file paths"
  TEAM_DIR:
    source: flag
    default: "$HOME/team"
    required: true
    description: "Shared team repo — I write to 17 workers' current_state.md, learnings.md, sessions/"
  BRIDGE_URL:
    source: flag
    default: "http://localhost:8271"
    required: true
    description: "Bridge endpoint for worker discovery, checkin, messaging"
  TMUX_PREFIX:
    source: default
    default: "claude-prod-"
    required: true
    description: "Session name prefix — I send tmux commands to ${TMUX_PREFIX}<worker> for wrap-ups"
  ANTHROPIC_API_KEY:
    source: creds
    required: true
    description: "Claude CLI auth"
  GH_TOKEN:
    source: creds
    required: true
    description: "GitHub CLI auth — PR/issue lookups"
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
    description: "Claude Code config — CLAUDE.md, skills/, project settings"
  CLAUDE_PROJECTS_DIR:
    source: default
    default: "$CLAUDE_CONFIG_DIR/projects"
    required: true
    description: "Claude Code project memory/settings root"
  CLAUDE_PROJECT_SLUG:
    source: flag
    default: "-home-claude-team"
    required: true
    description: "Claude Code project slug (mangled cwd path, e.g. -home-claude-team for /home/claude/team)"
  AGENT_DIR:
    source: default
    default: "$HOME/.agent"
    required: true
    description: "Git-tracked skills source of truth"
  GWS_CONFIG_DIR:
    source: default
    default: "$HOME/.config/gws"
    required: true
    description: "gws OAuth creds — client_secret.json, credentials.json"
  GWS_BIN:
    source: default
    default: "$HOME/bin/gws"
    required: true
    description: "Google Workspace CLI — Gmail thread lookups for ops emails"
  SSH_CONFIG:
    source: default
    default: "$HOME/.ssh/config"
    required: true
    description: "SSH config with Mac Mini alias"
  SSH_KEY_MAC_MINI:
    source: creds
    required: true
    description: "SSH private key for Mac Mini (Tailscale)"
  GCLOUD_CONFIG_DIR:
    source: default
    default: "$HOME/.config/gcloud"
    required: false
    description: "gcloud SA configs — for GCS upload and deploy status checks"
  OPS_EMAIL_SCRIPT:
    source: default
    default: "$TEAM_DIR/scripts/ops_email_send.py"
    required: true
    description: "Shared email sender for daily report"

dirs:
  - $TEAM_DIR/$WORKER_NAME
  - $TEAM_DIR/sessions
  - $TEAM_DIR/scripts
  - $CLAUDE_CONFIG_DIR/skills
  - $CLAUDE_PROJECTS_DIR/$CLAUDE_PROJECT_SLUG/memory
  - $AGENT_DIR/skills
  - $GWS_CONFIG_DIR
  - $HOME/.ssh
```

### What's Different

| Need | mon | luck |
|------|-----|------|
| GCP/GKE | GOOGLE_APPLICATION_CREDENTIALS, KUBECONFIG | not needed |
| Android | JAVA_HOME, ANDROID_HOME | not needed |
| Shared team writes | not needed | TEAM_DIR (writes 17 workers' files) |
| tmux other workers | not needed | TMUX_PREFIX (wrap-ups, nudges) |
| Google Workspace | not needed | GWS_CONFIG_DIR, GWS_BIN |
| Mac Mini SSH | not needed | SSH_KEY_MAC_MINI, SSH_CONFIG |
| Working dir | MON_WORK_DIR ($HOME/ops) | TEAM_DIR ($HOME/team) |
| Timezone | TZ=UTC (critical for BQ) | not declared |

## What Gets Burned In

```go
//go:embed manifest.yaml
var Manifest []byte

//go:embed files/*
var Files embed.FS       // knowledge + skills + memory — all plaintext files

//go:embed creds.age
var CredsEncrypted []byte // encrypted files (age)

//go:embed checksums.json
var Checksums []byte     // SHA256 of every file, generated at build time
```

## Binary Structure

```
./mon
  ├── main.go          — entry, signal handling
  ├── manifest.go      — parse manifest, resolve vars
  ├── prepare.go       — Phase 0: resolve vars, create dirs
  ├── extract.go       — Phase 1: write embedded files to resolved paths
  ├── bootstrap.go     — Phase 2: check + install tools (OS-aware)
  ├── readiness.go     — Phase 3: pre-flight checks + auto-fix
  ├── secrets.go       — decrypt creds.age
  ├── runtime.go       — Runtime interface: start/stop/health/send (swappable)
  ├── tmux.go          — tmux runtime: create/adopt session, send-keys input
  ├── hooks.go         — hook handling
  ├── transport.go     — gRPC client to bridge
  ├── watchdog.go      — Phase 5: self-health monitoring
  └── upgrade.go       — self-rebuild with manager's key
```

## Hook Installation

Claude Code hooks require **two things** — the script files AND registration in `settings.json`. Just extracting hook files isn't enough.

```
~/.claude/settings.json (registration — tells Claude Code when to fire hooks)
  └── hooks.Stop[].command → "/path/to/send-to-telegram.sh"
  └── hooks.SessionStart[].command → "/path/to/checkin-on-start.sh"

~/.claude/hooks/ (scripts — the actual hook code)
  └── send-to-telegram.sh
  └── checkin-on-start.sh
```

### How the binary handles this

Phase 1 (Extract):
1. Write hook scripts to `$CLAUDE_CONFIG_DIR/hooks/`
2. **Merge** hook registrations into `$CLAUDE_CONFIG_DIR/settings.json`
   - Read existing settings.json
   - Add/update hook entries (match by command path)
   - Preserve existing hooks, MCP servers, permissions — never overwrite
   - Write back atomically (write tmp + rename)

### Manifest hook declaration

```yaml
hooks:
  - event: Stop
    command: $CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh
    source: hooks/send-to-telegram.sh
  - event: SessionStart
    matcher: "compact|resume|init|start"
    command: $CLAUDE_CONFIG_DIR/hooks/checkin-on-start.sh
    source: hooks/checkin-on-start.sh
```

The binary embeds hook scripts in `files/`, extracts them, then patches `settings.json` to register them. On uninstall or upgrade, it can cleanly remove only its own hook entries.

## Cross-Host

```
Mac Mini                              VPS (bridge)
┌──────────────┐   gRPC/Tailscale   ┌──────────────┐
│ ./mon        │ ──────────────────→│ bridge.py    │
│              │                    │              │
│ tmux/Claude  │                    │ routes msgs  │
│ hooks        │                    │ Telegram ↔   │
│ watchdog     │                    │ gRPC         │
│              │                    │              │
└──────────────┘                    └──────────────┘

Flow:
1. Binary starts on Mac Mini
2. Phase 0: resolve vars (HOME=/Users/beastoinagents, etc.)
3. Phase 1-3: extract, bootstrap, readiness
4. Phase 4: start runtime (tmux session, adopt session, or terminal)
   → spawn Claude, install hooks
   → gRPC connect to bridge for message routing only
5. Phase 5: watchdog monitors runtime + Claude + bridge connectivity
```

## Readiness Check: --check Flag

```
./mon --check

Worker: mon v1.0.0

Preparation (mon's declared vars):
  HOME                             = /Users/beastoinagents
  MON_WORK_DIR                     = /Users/beastoinagents/ops
  BRIDGE_URL                       = http://100.125.36.102:8271
  TZ                               = UTC
  MONITOR_ENV                      = /Users/beastoinagents/.config/omi/monitor/.env
  AGENT_ENV                        = /Users/beastoinagents/.config/claudecode-telegram/agent.env
  GOOGLE_APPLICATION_CREDENTIALS   = /Users/beastoinagents/.config/gcloud/beastoin-agents.json
  KUBECONFIG                       = /Users/beastoinagents/.kube/config
  OS                               = darwin/arm64

Tools:
  ✓ claude 1.0.30
  ✓ gcloud 556.0
  ✓ kubectl 1.35.0
  ✓ gh 2.86.0
  ⚠ gws: not found (optional)
  ✓ tmux 3.4

Environment:
  ✓ GCP: authenticated as beastoin-agents@based-hardware
  ✓ GKE: cluster reachable
  ✓ GitHub: authenticated
  ✓ Bridge: reachable
  ⚠ Mac Mini SSH: skipped (running on Mac Mini)

Credentials:
  ✓ ANTHROPIC_API_KEY: set
  ✓ GH_TOKEN: set
  ✓ GCP SA key: present

Status: READY (1 optional warning)
```

## Extract Behavior

One loop iterates all `files:` entries. Four flags control special handling:

```
For each file in manifest files:
  encrypted: true   → decrypt from creds.age first
  merge: true       → keep disk version if it exists and differs (it's newer)
  integrity: skip   → exempt from watchdog integrity checks (for files that change at runtime)
  overwrite: always → always write, even if dest exists and differs (worker-owned files)
  default           → CONFLICT CHECK: if dest exists and content differs, STOP with error

After writing each file:
  Compute SHA256 → compare against checksums.json → STOP if mismatch (corrupt binary)
```

### Extract Safety (Conflict Detection)

**Problem**: Workers share directories (e.g. `~/.claude/hooks/`). Extracting one worker's
files can silently overwrite another worker's files, breaking the entire system. This happened
when mon2's extract overwrote `forward-to-bridge.py` with an older version, breaking the Stop
hook for ALL workers.

**Rule**: Before overwriting any existing file, compare content. If the existing file differs
from the embedded version, STOP and report the conflict. The user must explicitly choose to
override or skip.

```
./mon --bridge-url http://...

Extract conflict detected:

  CONFLICT  ~/.claude/hooks/forward-to-bridge.py
            Existing file differs from embedded version.
            Existing: 1531 bytes, modified 2026-04-14 12:50
            Embedded: 1247 bytes

  CONFLICT  ~/.claude/hooks/send-to-telegram.sh
            Existing file differs from embedded version.
            Existing: 8996 bytes, modified 2026-04-07 13:34
            Embedded: 8812 bytes

  OK        ~/team/mon2/charter.md (new file)
  OK        ~/team/mon2/playbook.md (new file)

2 conflicts detected. Options:
  --force-extract     Override all conflicting files
  --skip-conflicts    Keep existing files, extract only new ones

To inspect differences:
  diff <(./mon --show-embedded hooks/forward-to-bridge.py) ~/.claude/hooks/forward-to-bridge.py
```

### Conflict check behavior by file flag

| Flag | Existing file matches | Existing file differs | No existing file |
|------|----------------------|----------------------|------------------|
| *(default)* | Write (no-op, same content) | **STOP with conflict** | Write |
| `merge: true` | Skip (keep disk) | Skip (keep disk) | Write |
| `overwrite: always` | Write | Write (force) | Write |
| `encrypted: true` | Write (no-op, same content) | **STOP with conflict** | Write |

### CLI flags for conflict resolution

| Flag | Behavior |
|------|----------|
| `--force-extract` | Override all conflicting files (user verified) |
| `--skip-conflicts` | Keep existing files, only write new ones |
| *(neither)* | **STOP with error listing all conflicts** |

### Manifest `overwrite` flag

For files the worker truly owns (charter, playbook, etc.), use `overwrite: always` to skip
conflict checking:

```yaml
files:
  - source: knowledge/charter.md
    dest: $HOME/team/mon2/charter.md
    overwrite: always                    # worker-owned, always write

  - source: hooks/send-to-telegram.sh
    dest: $CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh
    # no overwrite flag → conflict check (shared file)
```

## File Integrity

`checksums.json` is generated at build time — SHA256 of every embedded file (plaintext and pre-encryption creds).

```json
{
  "files/team/mon/charter.md": "a1b2c3...",
  "files/skills/omi-pr-workflow/SKILL.md": "d4e5f6...",
  "creds/monitor.env": "789abc...",
  "hooks/send-to-telegram.sh": "def012..."
}
```

### When checksums are verified

| Phase | What | Action on mismatch |
|-------|------|-------------------|
| **Phase 1 (Extract)** | Every file after writing to disk | **STOP** — corrupt binary, refuse to run |
| **Phase 3 (Readiness)** | All extracted files on disk | **STOP** — files tampered since extract |
| **Phase 5 (Watch)** | Critical files every 5 min | **Alert** via bridge — drift detected |

### What counts as critical (Phase 5 watchdog)

Files checked periodically at runtime:
- **Hooks** — modified hook = modified behavior, security risk
- **Creds** — modified creds = potential redirect/leak
- **settings.json hook entries** — someone could unregister hooks

Files NOT checked at runtime (they're expected to change):
- **Memory files** (`merge: true`) — evolve during work
- **current_state.md, kanban.md** — updated by the worker itself

Workers control this via `integrity: skip` flag on files that legitimately change at runtime.

### `--verify` flag

```
./mon --verify

Worker: mon v1.0.0
Integrity Check:

  ✓ hooks/send-to-telegram.sh       matches build checksum
  ✓ hooks/checkin-on-start.sh       matches build checksum
  ✓ creds/monitor.env               matches build checksum
  ✓ creds/gcloud-sa.json            matches build checksum
  ✓ settings.json hooks             registered correctly
  ⊘ memory/ (19 files)              skipped (merge files)
  ⊘ current_state.md                skipped (runtime mutable)

Status: VERIFIED (6 checked, 20 skipped)
```

## Local Watchdog

```
Every 30s:
  1. Runtime healthy? → restart via Runtime interface if dead
  2. Claude CLI responsive? → restart if stuck (no output >5 min)
  3. Bridge reachable? → reconnect with backoff
  4. Registered? → re-register if dropped

Every 5 min:
  5. Critical file integrity → alert via bridge if drift detected
```

## Self-Upgrade

```
Path A: Pre-built (recommended):
  1. Manager: worker-forge build mon → new binary
  2. Manager: /upgrade mon → bridge tells worker to download
  3. Worker: download → verify checksum → atomic replace → restart
  4. New binary runs Phase 0-3 on restart

Path B: Source rebuild (manager sends key):
  1. Manager: /upgrade mon <age-identity-key>
  2. Worker: receives key (transient, in memory only)
  3. Worker: pull latest knowledge, decrypt creds vault
  4. Worker: rebuild binary, replace self, restart
  5. Key discarded — never stored

Both paths: rollback to previous binary on failure.
```

## gRPC Service Definition

Workers communicate with the bridge over gRPC via Tailscale network. This gives us typed contracts, bidirectional streaming (for JSONL transcript/log streams), built-in health checks, and clean code generation for both Go (worker) and Python (bridge).

### Proto Definition

```protobuf
syntax = "proto3";
package workerforge;

service Bridge {
  // Worker lifecycle
  rpc Register(RegisterRequest) returns (RegisterResponse);
  rpc Heartbeat(HeartbeatRequest) returns (HeartbeatResponse);
  rpc Deregister(DeregisterRequest) returns (DeregisterResponse);

  // Message routing (bidirectional stream)
  rpc MessageStream(stream WorkerMessage) returns (stream BridgeMessage);

  // Knowledge/data streaming
  rpc StreamJSONL(stream JSONLChunk) returns (StreamAck);
  rpc PullKnowledge(KnowledgeRequest) returns (stream KnowledgeChunk);

  // Upgrade
  rpc CheckUpgrade(UpgradeCheckRequest) returns (UpgradeCheckResponse);
  rpc DownloadBinary(DownloadRequest) returns (stream BinaryChunk);

  // Health (standard gRPC health check)
  rpc Check(HealthCheckRequest) returns (HealthCheckResponse);
}

message RegisterRequest {
  string name = 1;
  string host = 2;            // Tailscale IP
  string version = 3;
  map<string, string> tools = 4;      // tool → version
}

message BridgeMessage {
  string type = 1;            // "message", "upgrade", "reload"
  string text = 2;
  string from = 3;            // "manager", worker name
  bytes payload = 4;          // for binary data
}

message WorkerMessage {
  string type = 1;            // "response", "status", "error"
  string text = 2;
  bytes payload = 3;
}

message JSONLChunk {
  string stream_id = 1;       // e.g., "transcript-mon-2026-04-18"
  bytes data = 2;             // JSONL bytes
  bool final = 3;
}
```

### Why gRPC over WebSocket

| Factor | gRPC | WebSocket |
|--------|------|-----------|
| **Streaming JSONL** | Native bidirectional streaming | Manual framing |
| **Type safety** | Proto-enforced contracts | JSON, hope for the best |
| **Code generation** | Go + Python from same .proto | Manual serialization |
| **Health checks** | Built-in standard protocol | Custom implementation |
| **Reconnect** | Built-in with backoff | Manual implementation |
| **Network** | Tailscale (direct IP, HTTP/2) | Tailscale (works too) |
| **Binary data** | First-class (bytes field) | Base64 or binary frames |
| **Multiplexing** | HTTP/2 multiplexed streams | Single connection |

### Bridge Changes

```python
# Bridge runs gRPC server (grpcio) alongside existing HTTP server
# Worker connects as gRPC client (Go, google.golang.org/grpc)

class BridgeServicer(workerforge_pb2_grpc.BridgeServicer):
    def Register(self, request, context):
        name = request.name
        host = request.host
        self.register_worker(name, context)
        return RegisterResponse(ok=True)
    
    def MessageStream(self, request_iterator, context):
        """Bidirectional: bridge pushes messages, worker sends responses"""
        for worker_msg in request_iterator:
            # Process worker response, route to Telegram
            yield BridgeMessage(type="message", text=next_message)
    
    def StreamJSONL(self, request_iterator, context):
        """Worker streams JSONL data (transcripts, logs) to bridge"""
        for chunk in request_iterator:
            self.process_jsonl_chunk(chunk)
        return StreamAck(ok=True)
```

## worker-forge CLI

### `worker-forge install-skill`

Installs a Claude Code skill that teaches agents how to generate their own manifest. Run this on any machine where agents work — they get `/worker-forge-manifest` as a ready-to-use skill.

```
worker-forge install-skill [--dir ~/.claude/skills]

Installs:
  ~/.claude/skills/worker-forge-manifest/
    SKILL.md    — manifest schema, var sources, file flags (merge/encrypted),
                  real examples (mon, luck), instructions for introspecting
                  the agent's own env to produce a valid manifest.yaml
```

The skill is embedded in the worker-forge binary itself — always in sync with the current schema. When the schema changes, `worker-forge install-skill` updates the skill to match.

Agents invoke `/worker-forge-manifest` → introspect their environment (tools, dirs, creds, env vars) → produce a valid `manifest.yaml` for their role.

### `worker-forge build`

```
worker-forge build mon \
  --manifest workers/mon/manifest.yaml \
  --identity ~/.age/manager.key \
  --target linux/amd64,darwin/arm64 \
  --output ./dist/

Steps:
  1. Parse manifest.yaml
  2. Resolve source: paths (build-time only — on build machine)
  3. Collect files (knowledge, skills, memory → plaintext embed)
  4. Collect encrypted files (creds → age encrypt → embed)
  5. Generate checksums.json (SHA256 of every file)
  6. Embed everything + manifest + checksums into Go binary
  7. Cross-compile for each target OS/arch
  8. Sign binaries with manager's key

Output:
  dist/
    mon-linux-amd64
    mon-darwin-arm64
    checksums.txt.sig
```

## OS Service Integration

```ini
# systemd (Linux)
[Unit]
Description=Worker mon
After=network-online.target

[Service]
ExecStart=/opt/workers/mon --bridge-url https://bridge.example.com
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```xml
<!-- launchd (macOS) -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.workerforge.mon</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/workers/mon</string>
    <string>--bridge-url</string>
    <string>https://bridge.example.com</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

## Migration Plan

### Phase 1: Core Binary + Local MVP
- [x] Define manifest schema with vars, dirs, knowledge, skills, creds, tools, readiness
- [x] Go project: worker-forge/ with embed, age, CLI
- [x] Phase 0: var resolution engine ($HOME, $NAME, overrides)
- [x] Phase 1: extract to resolved paths, memory merge
- [x] Phase 2: tool bootstrap (OS-aware: linux vs darwin)
- [x] Phase 3: readiness checks + auto-fix
- [x] `--check` flag (preparation + readiness only)
- [x] tmux session management, Claude CLI spawn, hooks
- [x] Watchdog loop
- [x] E2E test: build binary → run → verify tmux + bridge registration + watchdog (test-e2e.sh, 11 assertions)
- [x] Live test: --check READY on VPS (13MB binary, 12 tools, 5 readiness checks pass)
- [ ] Live run test (deferred — would conflict with prod claude-prod-mon session)

### Phase 2: gRPC Transport + Cross-Host
- [x] Proto definition: Bridge service (Register, MessageStream, Heartbeat, StreamJSONL, PullKnowledge, CheckUpgrade, DownloadBinary)
- [x] Bridge: gRPC server (grpcio) on dedicated port, alongside existing HTTP (bridge_grpc.py)
- [x] Binary: gRPC client, connect to bridge, register (transport.go GRPCTransport)
- [x] Bridge: MessageStream for bidirectional message routing
- [x] StreamJSONL: transcript/log streaming from worker to bridge
- [x] Cross-compile: linux/amd64, linux/arm64, darwin/arm64 (Makefile build-all)
- [ ] Live test: ./mon on Mac Mini, bridge on VPS

### Phase 3: Self-Upgrade
- [x] Pre-built upgrade (download + verify + atomic replace) — upgrade.go
- [x] Rollback on failure — upgrade.go
- [ ] Source rebuild (transient key) — DEFERRED per Codex recommendation
- [ ] Live test

### Phase 4: All Workers
- [x] Worker inventory (workers.yaml — 20 workers, roles, platforms, creds)
- [x] Manifest template (workers/_template/manifest.yaml)
- [x] Pilot manifests (chen, mon, ren)
- [x] Write manifests for remaining 17 workers (dean, finn, geni, hiro, jin, kai, kelvin, kenji, lee, luck, noa, ryo, seth, sora, taro, x, yuki)
- [ ] Package all workers (package-all script)
- [ ] Per-worker least-privilege credentials

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Standalone binary | Single binary per worker | Principle #1: run anywhere |
| Self-managing | Binary owns tmux/Claude/hooks | No bridge dependency for local operations |
| Preparation phase | Vars resolved before extract | Binary adapts to any host — HOME, paths, bridge URL |
| Manifest-driven | Per-worker manifest | Each worker is different |
| OS-aware bootstrap | linux/darwin install commands | Same binary concept, different install per OS |
| Readiness checks | Pre-flight + auto-fix | Verify env before starting work |
| Everything embedded | Knowledge + creds + skills + memory | Binary carries everything |
| Self-upgrade | Pre-built + source rebuild | Principle #2 |
| Memory merge | Keep disk if newer | Memory evolves at runtime |
| Worker identity | Name-based | Stable across rebuilds |
| Transport | gRPC over Tailscale | Typed contracts, bidirectional streaming, JSONL support |

## Open Questions

1. Should vars support nested expansion? (e.g., `$TEAM_DIR/$WORKER_NAME/kanban.md` → multi-level)
2. Should preparation validate dir permissions (writable?) or just create?
3. Should `--check` output be JSON for monitoring dashboards?
