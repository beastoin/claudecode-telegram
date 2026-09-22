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

---

## Message Flow Map

Every path a message takes through the system. This is the audit surface — check here before changing any message behavior.

### Inbound: Telegram → Worker

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Hire worker | `/hire <name>` | Creates tmux session `claude-<node>-<name>` | Registers worker, sets focus, sends welcome | SPEC-001, SPEC-014 |
| Bare message | Any text (no `/` or `@`) | Routes to focused worker | 👀 react, `tmux send-keys` to active session | SPEC-012, SPEC-013 |
| @mention | `@name <msg>` | Routes to named worker, focus unchanged | 👀 react, one-off delivery | SPEC-012 |
| @all broadcast | `@all <msg>` | Routes to every active worker | 👀 react, delivered to all sessions | SPEC-012 |
| Implicit `/name` | Bare `<name>` as entire message | Treated as `/focus <name>` if worker exists | Shorthand focus switch | SPEC-012 |
| Reply-to | Telegram reply to a worker message | Routes to originating worker | 👀 react, preserves reply context | SPEC-012 |
| Focus switch | `/focus <name>` | Changes active worker | Next bare messages go to new focus | SPEC-012 |
| File/image | Telegram attachment | Downloads to temp, path injected into message | Worker receives local file path | |
| Non-admin | Message from unknown chat_id | Silently rejected | No response, no error revealed | SPEC-007, SPEC-008 |

### Outbound: Worker → Telegram

| Flow | Trigger | Route | Behavior | Spec |
|------|---------|-------|----------|------|
| Worker reply | Claude stop event fires hook | `send-to-telegram.sh` → `POST /response` → Telegram | Message appears as `worker_name: <text>` | SPEC-016, SPEC-013 |
| Media tags | `[[image:/path\|caption]]` in output | Hook sends raw text; bridge parses tags and sends via `sendPhoto`/`sendDocument` | Inline image or file in chat | SPEC-016 |
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
| Team status | `/team` | Scans tmux sessions | Health state per worker (READY/BUSY/STUCK/DEAD/OFFLINE/...) | SPEC-001, SPEC-015 |
| Worker restart | `/restart <name>` | Kills + restarts tmux session | Resumes session context if available | SPEC-001 |
| Teleport | `/teleport <name> <host>` | Syncs state, starts on target, stops source | Cross-machine worker migration | SPEC-010 |
| Remote register | `POST /register` from remote host | Registers pre-existing worker session | Adds to registry, exports env vars | SPEC-010 |
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
| `/teleback <name>` | Bring teleported worker back |
| `/channel create <label> <members>` | Create a worker channel |
| `/settings` | Show bridge configuration |
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

Full shell reference and runtime flags in the Setup Guide below.

---

## Status

**Version:** 0.44.7 · **Tests:** 431 passing (FAST mode, v0.44.7)

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
├── team_memory/           # /memory command — chat history search [SPEC.md inside]
├── tools/                 # PR review, indexers, pilot terminal viewer, git-hooks
├── skills/                # Claude skill definitions (bridge interaction guide)
├── tests/                 # Python test files
├── experiments/           # Forge worker binary, void microVM, MCP prototypes
├── SPEC.md                # System design specs and changelog
├── FEATURES.md            # Behavioral spec (MUST requirements)
├── AGENTS.md              # Agent workflow and operational learnings
├── TEST.md                # Testing documentation
└── test.sh                # Automated acceptance tests (431 tests)
```

---

## Documentation

| Doc | Purpose | Owner |
|-----|---------|-------|
| [SPEC.md](SPEC.md) | System design, architecture specs, changelog | Manager |
| [FEATURES.md](FEATURES.md) | Behavioral spec — MUST requirements | Manager |
| [AGENTS.md](AGENTS.md) | Agent workflow and operational learnings | Agent |
| [TEST.md](TEST.md) | Testing modes, env vars, test inventory | Agent |

---

## Setup Guide

Follow each step in order. If you skip steps, workers can start but fail later.

### Part 1: Get Your Accounts Ready (5 min)

#### Step 1: Create a Telegram bot (with BotFather)

This step creates your bot identity. It gives you the bot token this project needs.

1. Open Telegram and search for `@BotFather`.
2. Open the BotFather chat and press **Start**.
3. Send this command in BotFather:

```bash
/newbot
```

4. BotFather asks for a bot name. Send any display name you want.
5. BotFather asks for a username. Send a unique username that ends with `bot` (example: `myteamhelper_bot`).
6. Copy the token BotFather sends you.

What you should see in Telegram:
- A BotFather message similar to: `Done! Congratulations on your new bot...`
- A line that contains your bot token.

Verification:
- Your token looks like this: `123456789:AAExampleTokenStringHere`.

If this fails:
- Error: `Sorry, this username is already taken.`
- Fix: choose another username. Keep the required `bot` suffix.

#### Step 2: Create an Anthropic API key

Workers cannot call Claude without your API key.

1. Open `https://console.anthropic.com` and sign in.
2. Open API keys and create a new key.
3. Copy the key immediately.

Verification:
- Your key starts with `sk-ant-`.

If this fails:
- Error: key is missing or does not start with `sk-ant-`.
- Fix: create a new key and copy it again.

Note:
- API usage costs money per request. Check pricing at `https://www.anthropic.com/pricing`.

---

### Part 2: Install Software (10 min)

Choose your platform path. Complete every step in that section.

### macOS Setup Path

#### Step 1: Install Homebrew (package manager)

Homebrew makes installing required tools simple and consistent.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Verification command:

```bash
brew --version
```

You should see output like: `Homebrew 4.x.x`.

If this fails:
- Error: `xcode-select: error: tool 'xcode-select' requires Xcode`
- Fix: run the command below, then retry Step 1.

```bash
xcode-select --install
```

#### Step 2: Install Node.js

Claude CLI is a Node package. Node.js must be installed first.

You can also download Node.js from `https://nodejs.org` instead of Homebrew.

```bash
brew install node
```

Verification command:

```bash
node --version
```

You should see output like: `v20.11.1` (v18+ is required).

#### Step 3: Install Claude CLI

This installs the `claude` command that workers run.

> [!WARNING]
> Common failure before this step: `npm ERR! code EACCES`.
> If you see this on macOS, install Node via Homebrew first (Step 2), then retry.

```bash
npm install -g @anthropic-ai/claude-code
```

Verification command:

```bash
claude --version
```

You should see output like: `1.0.x`.

#### Step 4: Install tmux

tmux keeps multiple workers alive in parallel background sessions.

```bash
brew install tmux
```

Verification command:

```bash
tmux -V
```

You should see output like: `tmux 3.4`.

#### Step 5: Install Python 3

The bridge is written in Python.

```bash
brew install python
```

Verification command:

```bash
python3 --version
```

You should see output like: `Python 3.11.x`.

#### Step 6: Install jq

Setup scripts use jq for JSON editing and checks.

```bash
brew install jq
```

Verification command:

```bash
jq --version
```

You should see output like: `jq-1.7`.

#### Step 7: Verify curl

Setup and tunnel workflows use `curl` in multiple places.

```bash
curl --version
```

You should see output that begins with `curl`.

#### Step 8: Install cloudflared

cloudflared opens the secure tunnel Telegram needs to reach your machine.

```bash
brew install cloudflared
```

Verification command:

```bash
cloudflared --version
```

You should see output that contains: `cloudflared version`.

### Linux/Ubuntu Setup Path

#### Step 1: Update package index

This refreshes available package versions before you install tools.

```bash
sudo apt update
```

Verification command:

```bash
apt-cache policy nodejs
```

You should see package metadata instead of `Unable to locate package`.

#### Step 2: Install Node.js and npm

Claude CLI needs both Node.js and npm.

```bash
sudo apt install -y nodejs npm
```

Verification command:

```bash
node --version
```

You should see `v18` or newer.

If this fails:
- Problem: Ubuntu repo may install an older Node version.
- Fix: run NodeSource setup, then reinstall Node.js.

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
```

```bash
sudo apt install -y nodejs
```

#### Step 3: Install Claude CLI

This provides the `claude` executable that worker sessions use.

> [!WARNING]
> Common failure before this step: `npm ERR! code EACCES`.
> Fix: retry with sudo on Ubuntu.

```bash
sudo npm install -g @anthropic-ai/claude-code
```

Verification command:

```bash
claude --version
```

You should see output like: `1.0.x`.

#### Step 4: Install tmux, jq, curl, and Python 3

The bridge runtime and setup scripts need these tools.

```bash
sudo apt install -y tmux jq curl python3
```

Verification command:

```bash
tmux -V
```

You should see output like: `tmux 3.3a`.

Verification command:

```bash
python3 --version
```

You should see output like: `Python 3.10.x` or newer.

#### Step 5: Verify curl

Download and webhook troubleshooting commands use `curl`.

```bash
curl --version
```

You should see output that begins with `curl`.

#### Step 6: Check CPU architecture

The cloudflared download URL depends on CPU type.

```bash
uname -m
```

You should see `x86_64` or `aarch64`.

#### Step 7: Download cloudflared (x86_64)

This fetches cloudflared for most Intel/AMD Linux systems.

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared
```

If your previous step returned `aarch64`, use this command instead:

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o /tmp/cloudflared
```

#### Step 8: Install cloudflared binary

This places cloudflared in your executable path.

```bash
sudo install /tmp/cloudflared /usr/local/bin/cloudflared
```

Verification command:

```bash
cloudflared --version
```

You should see output that contains: `cloudflared version`.

If this fails:
- Error: `Permission denied` while installing.
- Fix: rerun Step 8 with `sudo` exactly as shown.

---

### Part 3: Configure Authentication (5 min)

#### Step 1: Save your Anthropic API key permanently

Workers run in tmux sessions. tmux must inherit `ANTHROPIC_API_KEY` from your shell startup file.

The bridge propagates bridge-specific variables into workers. It does **not** inject `ANTHROPIC_API_KEY` for you.

> [!WARNING]
> Do **not** create `~/.claude/.credentials.json` for API-key auth.
> It can cause: `OAuth error: Invalid code`.

**macOS (default shell: zsh)**

`~/.zshrc` runs every time you open a terminal.

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-paste-your-real-key-here"' >> ~/.zshrc
```

Replace `sk-ant-paste-your-real-key-here` with your real key from Part 1.

```bash
source ~/.zshrc
```

```bash
echo "$ANTHROPIC_API_KEY"
```

You should see your key printed. It starts with `sk-ant-`.

**Linux/Ubuntu (default shell: bash)**

`~/.bashrc` runs every time you open a terminal.

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-paste-your-real-key-here"' >> ~/.bashrc
```

Replace `sk-ant-paste-your-real-key-here` with your real key from Part 1.

```bash
source ~/.bashrc
```

```bash
echo "$ANTHROPIC_API_KEY"
```

You should see your key printed. It starts with `sk-ant-`.

If this fails:
- Symptom: command prints a blank line.
- Fix: repeat the `echo 'export ANTHROPIC_API_KEY=...' >> ...` step carefully. Then reload with `source`.

#### Step 2: Complete Claude CLI first-run wizard once

First-run setup must finish once interactively. If you skip this, workers can get stuck in setup and never answer.

```bash
claude --dangerously-skip-permissions
```

In the interactive wizard:
1. Pick any theme.
2. Choose `Yes` when asked about custom API key.
3. Confirm permissions bypass.
4. Type `/exit` to close.

Verification command:

```bash
claude --version
```

You should see only a version line with no wizard prompts.

If this fails:
- Error: `OAuth error: Invalid code`.
- Fix: remove the wrong credentials file, then rerun this step.

```bash
rm -f ~/.claude/.credentials.json
```

---

### Part 4: Download and Install claudecode-telegram (2 min)

Choose one option.

#### Option A: Git clone (recommended)

Clone gives you easy future updates.

1. Clone the repository.

```bash
git clone https://github.com/beastoin/claudecode-telegram
```

2. Verify that key project files are present.

```bash
ls claudecode-telegram/
```

You should see entries that include `bridge.py`, `bridge.sh`, `hooks`, `SPEC.md`, and `test.sh`.

3. Enter the project folder.

```bash
cd claudecode-telegram
```

If this fails:
- Error: `git: command not found`.
- Fix (macOS): run `brew install git`.
- Fix (Ubuntu): run `sudo apt install -y git`.

#### Option B: Tarball (if repo access is restricted)

Use this method when GitHub clone access is unavailable.

1. Extract the tarball.

```bash
tar xzf claudecode-telegram.tar.gz
```

2. Verify that key project files are present.

```bash
ls claudecode-telegram/
```

You should see entries that include `bridge.py`, `bridge.sh`, `hooks`, `SPEC.md`, and `test.sh`.

3. Enter the project folder.

```bash
cd claudecode-telegram
```

---

### Part 5: Start the Bridge (2 min)

#### Step 1: Install Claude hooks

Hooks are small scripts that send worker replies from Claude sessions back into Telegram.

```bash
./bridge.sh hook install
```

You should see success lines that mention Stop/SessionStart hooks.

If this fails:
- Error: `jq: command not found`.
- Fix: install jq (Part 2), then rerun this step.

#### Step 2: Set your Telegram bot token for this terminal

The bridge cannot call Telegram without `TELEGRAM_BOT_TOKEN`.

```bash
export TELEGRAM_BOT_TOKEN="123456789:paste-your-real-bot-token-here"
```

Replace the entire value with the token from BotFather.

Verification command:

```bash
echo "$TELEGRAM_BOT_TOKEN"
```

You should see a value with digits, a colon, and a long token string.

If this fails:
- Symptom: output is blank.
- Fix: rerun the `export TELEGRAM_BOT_TOKEN=...` command.

#### Step 3: Run the bridge

This starts the bridge, tunnel, and webhook so Telegram can reach your workers.

> [!WARNING]
> Common failure before this step: `tmux: command not found`.
> Fix: install tmux first (Part 2), verify with `tmux -V`, then rerun.

```bash
./bridge.sh run
```

You should see output similar to:
- `Multi-Session Bridge on 127.0.0.1:<port>` (port depends on node: prod=8271, dev=8272, default=8270)
- `Tunnel URL: https://...`
- `Webhook configured`

Open a second terminal in the same folder and verify:

```bash
./bridge.sh status
```

You should see node status marked running.

If this fails:
- Error: `Connection refused`.
- Fix: the bridge is not running. Run `./bridge.sh run` again. Keep that terminal open.
- Error: `Webhook setup failed (DNS may still be propagating)`.
- Fix: wait 20-60 seconds. Then rerun `./bridge.sh run`.

---

### Part 6: Start Using It in Telegram (1 min)

#### Step 1: Open your bot chat

All manager and operator actions happen in Telegram.

1. Open Telegram.
2. Search for your bot username from BotFather.
3. Press **Start**.

#### Step 2: Hire your first worker

No one can receive tasks until a worker exists.

Send this in Telegram:

```bash
/hire myworker
```

You should see confirmation that worker `myworker` was created.

If this fails:
- Error: `No one assigned` on later messages.
- Fix: run `/team` then `/focus myworker`.

#### Step 3: Send your first task

This validates end-to-end delivery from Telegram to worker and back.

Send this in Telegram:

```bash
Summarize today's priorities from our latest commit messages.
```

What to expect:
1. Bot reacts with `👀` (delivery confirmed).
2. Worker replies as `myworker: ...`.

If this fails:
- Symptom: no `👀` reaction.
- Fix: verify `TELEGRAM_BOT_TOKEN` and bridge status.
- Symptom: `👀` appears but no reply.
- Fix: run `/progress`, then `/restart` if the worker is stuck.

---

## Troubleshooting

### Quick top-3 checks

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Bot does not respond | Bridge down or wrong admin | Run `./bridge.sh status`. Restart if needed. |
| `👀` but no reply | Worker busy or stuck | Run `/progress`, then `/restart`. |
| `No one assigned` | No focused worker | Run `/team`, then `/focus <name>`. |

### `OAuth error: Invalid code`

Why it happens:
- Claude CLI was not fully initialized with API-key flow. Or `~/.claude/.credentials.json` was created manually.

Fix:

```bash
rm -f ~/.claude/.credentials.json
claude --dangerously-skip-permissions
```

Complete wizard and exit with `/exit`.

### `ModuleNotFoundError: No module named ...`

Fix (macOS): `brew install python`
Fix (Ubuntu): `sudo apt install -y python3`

### `tmux: command not found`

Fix (macOS): `brew install tmux`
Fix (Ubuntu): `sudo apt install -y tmux`

### Worker starts but never responds

Why: `ANTHROPIC_API_KEY` is not available inside tmux-created sessions.

Fix: Add the key to your shell startup file (Part 3). Open a new terminal. Then `./bridge.sh restart`.

### Bot does not react with `👀`

Why: Invalid or missing `TELEGRAM_BOT_TOKEN`, webhook misconfiguration, or bridge is down.

Fix: Check `echo "$TELEGRAM_BOT_TOKEN"`, then `./bridge.sh status`, then `./bridge.sh run`.

### Tunnel issues (cloudflared)

Fix: Verify `cloudflared --version`. If missing, install it (Part 2). Then `./bridge.sh run`.

### Webhook mismatch or stale webhook

Fix: `./bridge.sh webhook info` → `./bridge.sh webhook delete` → `./bridge.sh run`.

### Wrong admin account controls the bot

Why: First user to message becomes admin when `ADMIN_CHAT_ID` is not set.

Fix: `./bridge.sh clean`, then restart. Send the first message from the correct account.

### Hooks installed but messages not forwarded

Fix: `./bridge.sh hook install`, then `/restart` worker in Telegram.

---

## Security Hardening (Optional)

### Already enabled by default

- **Default localhost binding**: Bridge binds to `127.0.0.1`. Only cloudflared can reach it. When `BRIDGE_PUBLIC_URL` is set (Tailscale), auto-binds to `0.0.0.0`.
- **Webhook secret**: Set `TELEGRAM_WEBHOOK_SECRET` to verify incoming webhooks.
- **Token isolation**: Workers never see `TELEGRAM_BOT_TOKEN`. [SPEC-006]

### Recommended system-level hardening

#### 1. Run bridge under a dedicated Unix user

Prevents workers from reading bridge environment via `/proc`.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin bridge-user
```

Run bridge as `bridge-user`, workers as your normal user.

#### 2. Hide process information between users

```bash
sudo mount -o remount,hidepid=2 /proc
```

#### 3. Set ADMIN_CHAT_ID explicitly

```bash
export ADMIN_CHAT_ID="123456789"
```

Get your chat ID from [@userinfobot](https://t.me/userinfobot).

#### 4. Enable Telegram webhook verification

```bash
export TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 16)"
```

---

## Credits

Original project by Han Xiao (hanxiao/claudecode-telegram).

## License

MIT
