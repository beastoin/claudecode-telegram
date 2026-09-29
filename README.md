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

The bridge is a Python HTTP server that receives Telegram webhooks and routes messages to tmux sessions running AI workers. Each worker is an independent Claude Code (or Codex) session. No database — tmux sessions are the persistence layer.

---

## Quick Start

```bash
git clone https://github.com/beastoin/claudecode-telegram.git
cd claudecode-telegram
./bridge.sh setup
```

That's it. The setup wizard checks your tools, asks for your bot token, installs hooks, and starts the bridge. Then send `/hire myworker` to your bot on Telegram.

**What you need:** Python 3.12+, tmux, Node.js, Claude CLI (`npm install -g @anthropic-ai/claude-code`), and a Telegram bot token from [@BotFather](https://t.me/BotFather). The wizard checks all of these and tells you what's missing.

**Already set up?** Just `./bridge.sh run`.

> Need cloudflared for a public webhook? `brew install cloudflared` (macOS) — the bridge starts a tunnel automatically. Or use `--no-tunnel` with your own URL. See [SETUP.md](SETUP.md) for detailed troubleshooting.

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
- **Multi-backend** — Claude, Codex
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
├── connectors.py          # Gmail, GitHub polling integrations
├── hooks.sh               # Claude Code stop hook (sends replies to Telegram)
├── tests/                 # pytest test suite (341 tests)
├── tools/                 # PR review, indexers, pilot terminal viewer
├── test.sh                # Acceptance tests (mypy + pytest + bash + Go)
└── pyproject.toml         # Project metadata, mypy strict config
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

**Version:** 0.45.0 · **Tests:** 431 passing (341 pytest + bash + Go) · **mypy:** 0 errors

## Credits

Original project by Han Xiao (hanxiao/claudecode-telegram).

## License

MIT
