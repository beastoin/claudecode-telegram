# Claude Code - Telegram

Run multiple AI workers from one Telegram chat.

A bridge between Telegram and AI coding assistants. You message your bot, the bot routes tasks to workers on your machine, workers reply back in Telegram.

<img width="400" alt="image" src="https://github.com/user-attachments/assets/a12cbdbf-cf18-4ba4-8645-08a3a359559a" />

**Glossary:** A **worker** is an AI coding session running in tmux. The **manager** is the human who messages the bot. The **focused worker** (set by `/focus`) receives bare messages. The **bridge** (`bridge.py`) routes everything between Telegram and workers.

---

## How it works

```
Telegram ──webhook──► bridge.py ──tmux──► Claude worker sessions
                         │                       │
                         │                       │ hook fires on stop
                         │                       ▼
                         │◄──── POST /response ──── send-to-telegram.sh
```

The bridge is a Python HTTP server that receives Telegram webhooks and routes messages to tmux sessions running AI workers. Each worker is an independent Claude Code (or Codex/Gemini/OpenCode) session. No database — tmux sessions are the persistence layer.

---

## Quick Start

```bash
git clone https://github.com/beastoin/claudecode-telegram.git
cd claudecode-telegram
./bridge.sh hook install
export TELEGRAM_BOT_TOKEN='your-token-from-botfather'
./bridge.sh run
```

**Prerequisites:** Python 3, tmux, Node.js, cloudflared, Claude CLI authenticated.

First time? See the full **[Setup Guide](SETUP.md)** for step-by-step installation.

---

## Commands

| Command | What it does |
|---------|-------------|
| `/hire <name>` | Create a worker |
| `/focus <name>` | Set which worker gets messages |
| `/team` | List all workers with health state |
| `/end <name>` | Remove a worker |
| `/pause` | Interrupt focused worker |
| `/restart [name]` | Restart worker (resume context) |
| `/teleport <name> <host>` | Move worker to another machine |
| `/teleback <name>` | Bring teleported worker back |
| `/progress [name]` | Check worker status |
| `/channel create <label> <members>` | Create a worker channel |
| `/settings` | Show bridge configuration |
| `/voice on\|off` | Toggle voice replies |
| `/pilot <name>` | Toggle web terminal viewer |
| `/relay <worker>` | Open public channel |
| `/rewind <name>` | Open transcript viewer |
| `/pr <url>` | PR review viewer |
| `@name <msg>` | One-off message to named worker |

**Backend selection:** `/hire codex-alice` (prefix) or `/hire alice --backend codex` (flag).

**Shell commands:** `./bridge.sh run`, `stop`, `restart`, `status`, `hook install`. Always use `./bridge.sh stop` — never raw `kill`.

---

## Features

- **Multi-worker** — hire/end/focus/restart workers from Telegram
- **Multi-backend** — Claude, Codex, Gemini, OpenCode
- **Multi-machine** — teleport workers between hosts
- **Worker-to-worker** — direct P2P messaging
- **Connectors** — Gmail and GitHub polling with sender allowlists
- **Voice mode** — STT + TTS for Telegram voice messages
- **Pilot** — web terminal viewer for worker sessions
- **PR review** — interactive PR review in Telegram
- **Clean chat** — 👀 = received, `name: ...` replies, bridge speaks only for errors

---

## Project Structure

```
claudecode-telegram/
├── bridge.py              # HTTP server, worker management, all endpoints
├── bridge.sh              # CLI wrapper, tunnel/webhook setup
├── hooks/                 # Claude Code hooks (stop, start, notification)
├── connectors/            # Gmail, GitHub polling integrations
├── tools/                 # PR review, indexers, pilot terminal viewer
├── skills/                # Claude skill definitions
├── tests/                 # Python test files
├── experiments/           # Forge worker binary, void microVM, MCP prototypes
└── test.sh                # Automated acceptance tests
```

---

## Documentation

| Doc | Purpose |
|-----|---------|
| [SETUP.md](SETUP.md) | Full installation and troubleshooting guide |
| [SPEC.md](SPEC.md) | System design and architecture specs |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [AGENTS.md](AGENTS.md) | Agent workflow, message flow map, and operational learnings |
| [TEST.md](TEST.md) | Testing modes, commands, and test inventory |

---

**Version:** 0.44.7 · **Tests:** 431 passing

## Credits

Original project by Han Xiao (hanxiao/claudecode-telegram).

## License

MIT
