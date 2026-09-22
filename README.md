# Claude Code - Telegram

Run multiple AI workers from one Telegram chat.

A bridge between Telegram and AI coding assistants. You message your bot, the bot routes tasks to workers on your machine, workers reply back in Telegram. [SPEC-017]

<img width="400" alt="image" src="https://github.com/user-attachments/assets/a12cbdbf-cf18-4ba4-8645-08a3a359559a" />

**Glossary:** A **worker** is an AI coding session running in tmux. The **manager** is the human who messages the bot. The **focused worker** (set by `/focus`) receives bare messages. The **bridge** (`bridge.py`) routes everything between Telegram and workers.

---

## Architecture

```
Telegram ──webhook──► bridge.py ──tmux──► Claude worker sessions
                         │                       │
                         │                       │ hook fires on stop
                         │                       ▼
                         │◄──── POST /response ──── send-to-telegram.sh
                         │
                         ├── tmux sessions = source of truth [SPEC-001]
                         ├── workers discovered by prefix scan [SPEC-002]
                         ├── token never leaves bridge process [SPEC-006]
                         └── per-node isolation (prod/dev/test) [SPEC-023]
```

**Core model:** The bridge is a Python HTTP server that receives Telegram webhooks and routes messages to tmux sessions running AI workers. Each worker is an independent Claude Code (or Codex/Gemini/OpenCode) session. The hook script captures worker output and POSTs it back to the bridge, which sends it to Telegram. No database — tmux sessions are the persistence layer. [SPEC-001, SPEC-011]

**Backends:** Claude Code (default, interactive), Codex, Gemini, OpenCode (non-interactive). Backend selected by name prefix (`codex-alice`) or `--backend` flag.

**Multi-machine:** Static host topology in `machines.json`. Workers can be teleported between machines. Discovery via `GET /workers`. [SPEC-010, SPEC-019]

---

## Features

| Feature | What it does | Spec |
|---------|-------------|------|
| **Worker management** | `/hire`, `/end`, `/focus`, `/restart`, `/team`, `/progress` | SPEC-001, SPEC-014 |
| **Message routing** | `@name` one-off, `/focus` switch, bare text to active worker | SPEC-012 |
| **Multi-backend** | Claude, Codex, Gemini, OpenCode — prefix or `--backend` flag | SPEC-018 |
| **Multi-machine** | `/teleport` workers between hosts, cross-machine discovery | SPEC-010, SPEC-019 |
| **Worker-to-worker** | Direct P2P messaging via tmux paste-buffer or named pipes | SPEC-009 |
| **Connectors** | Gmail and GitHub polling with sender allowlists | SPEC-020 |
| **Guest/relay access** | Public channels to workers, guest tokens | |
| **Voice mode** | STT + TTS for Telegram voice messages | |
| **Pilot** | Web terminal viewer for worker sessions | |
| **PR review** | `/pr <url>` opens interactive PR review | |
| **Transcript search** | `/rewind`, `/memory` for session history | |
| **Clean chat** | 👀 = received, worker replies as `name: ...`, bridge speaks only for errors | SPEC-013 |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/beastoin/claudecode-telegram.git
cd claudecode-telegram

# 2. Install hooks
./bridge.sh hook install

# 3. Set your bot token
export TELEGRAM_BOT_TOKEN='your-token-from-botfather'

# 4. Run
./bridge.sh run
```

**Prerequisites:** Python 3, tmux, Node.js, cloudflared, Claude CLI authenticated.

**Default port:** 8271 (prod). Override with `--port` or `PORT` env var. See [SPEC-023] for node isolation.

**Full setup guide:** [SETUP.md](SETUP.md) — step-by-step for macOS and Linux with verification at each step.

---

## Message Flow Map

Every path a message takes through the system. This is the audit surface — check here before changing any message behavior.

### Inbound: Telegram → Worker

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Hire worker | `/hire <name>` | Creates tmux session `claude-<node>-<name>` | Registers worker, sets focus, sends welcome | SPEC-001, SPEC-014 |
| Bare message | Any text (no `/` or `@`) | Routes to focused worker | 👀 react, `tmux send-keys` to active session | SPEC-012, SPEC-013 |
| @mention | `@name <msg>` | Routes to named worker, focus unchanged | 👀 react, one-off delivery | SPEC-012 |
| Focus switch | `/focus <name>` | Changes active worker | Next bare messages go to new focus | SPEC-012 |
| File/image | Telegram attachment | Downloads to temp, path injected into message | Worker receives local file path | |
| Non-admin | Message from unknown chat_id | Silently rejected | No response, no error revealed | SPEC-007, SPEC-008 |

### Outbound: Worker → Telegram

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Worker reply | Claude stop event fires hook | `send-to-telegram.sh` → `POST /response` → Telegram | Message appears as `worker_name: <text>` | SPEC-016, SPEC-013 |
| Media tags | `[[image:/path\|caption]]` in output | Hook parses, bridge sends via `sendPhoto`/`sendDocument` | Inline image or file in chat | SPEC-016 |
| Long reply | Output > 4096 chars | Bridge splits into multiple messages | Preserves code blocks across splits | |
| Proactive message | Worker outputs without pending request | Hook sends if `chat_id` file exists | No `pending` gate — always delivers | SPEC-016 |

### Worker ↔ Worker

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| tmux send | Worker calls send command from `/workers` | `flock` + `tmux paste-buffer` (serialized) | Direct delivery, no bridge routing | SPEC-009 |
| Pipe send | Worker writes to named pipe | `echo "msg" > /tmp/claudecode-telegram/<node>/<worker>/in.pipe` | Non-interactive backend reads pipe | SPEC-009, SPEC-023 |
| Cross-machine | Worker calls SSH + send command | SSH wraps the tmux send | Transparent to sender | SPEC-009, SPEC-010 |

### Connector Inbound

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Gmail | New email from allowed sender | Connector polls → bridge → Telegram + workers | Subject + body forwarded | SPEC-020 |
| GitHub | New comment from allowed user | Connector polls → bridge → Telegram + workers | Comment forwarded with context | SPEC-020 |
| Blocked sender | Email/comment from non-allowed sender | Silently dropped | Fail-closed allowlist | SPEC-020 |

### Management

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Team status | `/team` | Scans tmux sessions | Health state per worker (READY/WORKING/STUCK/...) | SPEC-001, SPEC-015 |
| Worker restart | `/restart <name>` | Kills + restarts tmux session | Resumes session context if available | SPEC-001 |
| Teleport | `/teleport <name> <host>` | Syncs state, starts on target, stops source | Cross-machine worker migration | SPEC-010 |
| End worker | `/end <name>` | Kills tmux session | Removes from registry | SPEC-001, SPEC-014 |
| Bridge restart | Process restart | Scans tmux + reads `workers.json` | Recovers all workers, restores focus | SPEC-001, SPEC-003 |

### Error Paths

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Session missing | Message to non-existent worker | Bridge replies with error | "Worker not found" in Telegram | SPEC-005 |
| Worker dead | tmux session exited | Health check marks EXITED | Visible in `/team` output | SPEC-005 |
| Hook no chat_id | Hook fires but no `chat_id` file | Hook exits silently | Expected for unrouted sessions | SPEC-016 |
| Stale transcript | JSONL not updated recently | Hook sends health alert | `/health-alert` endpoint notified | SPEC-005 |

---

## Commands Reference

### Telegram

| Command | What it does |
|---------|-------------|
| `/hire <name>` | Create a worker |
| `/focus <name>` | Set which worker gets messages |
| `/progress [name]` | Check worker status |
| `/team` | List all workers with health state |
| `/end <name>` | Remove a worker |
| `/pause` | Interrupt focused worker |
| `/restart [name]` | Restart worker (resume context) |
| `/restart --clean` | Restart with fresh context |
| `/teleport <name> <host>` | Move worker to another machine |
| `/voice on\|off` | Toggle voice replies |
| `/pilot <name>` | Toggle web terminal viewer |
| `/relay <worker>` | Open public channel |
| `/rewind <name>` | Open transcript viewer |
| `/pr <url>` | PR review viewer |
| `/memory <query>` | Search team chat memory |
| `@name <msg>` | One-off message to named worker |

Backend selection: `/hire codex-alice` (prefix) or `/hire alice --backend codex` (flag).

### Shell [SPEC-024]

| Command | What it does |
|---------|-------------|
| `./bridge.sh run` | Start bridge + tunnel + webhook |
| `./bridge.sh stop` | Stop node (PID-based, safe for multi-node) |
| `./bridge.sh restart` | Restart node |
| `./bridge.sh status` | Show status |
| `./bridge.sh hook install` | Install Claude hooks |

Always use `./bridge.sh stop` — never `pkill` or raw `kill`. [SPEC-024]

Full shell reference and runtime flags: [SETUP.md](SETUP.md).

---

## Status

**Version:** 0.45.0 · **Tests:** 431 passing

**Changelog and specifications:** [SPEC.md](SPEC.md)

**Agent rules and workflow:** [AGENTS.md](AGENTS.md)

**Testing documentation:** [TEST.md](TEST.md)

---

## Project Structure

```
claudecode-telegram/
├── bridge.py              # HTTP server, worker management, all endpoints [SPEC-011]
├── bridge.sh              # CLI wrapper, tunnel/webhook setup
├── hooks/                 # Claude Code hooks (stop, start, notification)
├── connectors/            # Gmail, GitHub polling integrations [SPEC-020]
├── tools/                 # PR review, indexers, pilot terminal viewer
├── tests/                 # Python test files
├── experiments/           # Forge worker binary, STT service, MCP prototypes
├── SETUP.md               # Step-by-step install guide
├── TROUBLESHOOTING.md     # Error recovery guide
├── SECURITY.md            # Security hardening options
├── SPEC.md                # System design specs and changelog
├── AGENTS.md              # Agent rules, workflow, learnings
├── TEST.md                # Testing documentation
└── test.sh                # Automated acceptance tests (431 tests)
```

---

## Documentation

| Doc | Purpose | Owner |
|-----|---------|-------|
| [SPEC.md](SPEC.md) | System design, architecture specs, changelog | Manager |
| [AGENTS.md](AGENTS.md) | Agent rules, workflow, operational learnings | Agent |
| [TEST.md](TEST.md) | Testing modes, env vars, test inventory | Agent |
| [SETUP.md](SETUP.md) | Step-by-step install for macOS and Linux | Agent |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Error recovery guide | Agent |
| [SECURITY.md](SECURITY.md) | Security hardening options | Agent |

---

## Credits

Original project by Han Xiao (hanxiao/claudecode-telegram).

## License

MIT
