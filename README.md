# claudecode-telegram

Run multiple AI workers from one Telegram chat.

A bridge between Telegram and AI coding assistants. You message your bot, the bot routes tasks to workers on your machine, workers reply back in Telegram.

<img width="320" alt="image" src="https://github.com/user-attachments/assets/987c93d9-4f8c-43b3-8385-9c0259991f67" />

<img width="320" alt="image" src="https://github.com/user-attachments/assets/df3ab259-c505-46d7-99ae-c8f3e1284846" />

<img width="320" alt="image" src="https://github.com/user-attachments/assets/97c06219-7ba1-47de-8c10-526ad5c2a208" />

<img width="640" alt="image" src="https://github.com/user-attachments/assets/f97c0de7-305e-418c-8426-a1ffb812507f" />



**Glossary:** A **worker** is an AI coding session running in tmux. The **manager** is the human who messages the bot. The **focused worker** (set by `/focus`) receives bare messages. The **bridge** (`bridge.py`) routes everything between Telegram and workers.

---

## How it works

```
Telegram ──webhook──► bridge.py ──tmux──► Claude worker sessions
                         │                       │
                         │                       │ hook fires on stop
                         │                       ▼
                         │◄──── POST /response ──── claudecode.sh
```

The bridge is a Python HTTP server that receives Telegram webhooks and routes messages to tmux sessions running AI workers. Each worker is an independent Claude Code (or Codex) session. No database — tmux sessions are the persistence layer.

---

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/beastoin/claudecode-telegram/main/install.sh | bash
```

That's it. The installer clones the repo, installs Python 3.12+, tmux, Node.js, Claude CLI — asks for your bot token from [@BotFather](https://t.me/BotFather) — installs hooks — done. Then send `/hire myworker` to your bot on Telegram.

**Already set up?** Just `cd claudecode-telegram && ./bridge.sh run`.

---

## Commands

| Command | What it does |
|---------|-------------|
| `/hire <name>` | Create a worker |
| `/focus <name>` | Set which worker gets messages |
| `/team` | List all workers with health state |
| `/end <name>` | Remove a worker |
| `/restart [name]` | Restart worker (resume context) |
| `/teleport <name> <host>` | Move worker to another machine |
| `/teleback <name>` | Bring teleported worker back |
| `/settings` | Show bridge configuration |
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
├── claudecode.sh          # Claude Code hook (stop/start/tool-failure events)
├── review.py              # PR review page generator
├── indexer.py             # JSONL transcript indexer (SQLite FTS5)
├── tests/                 # pytest test suite
├── pilot/                 # Terminal viewer (Node.js, separate server)
├── test.sh                # Acceptance tests (mypy + pytest + bash + Go)
└── pyproject.toml         # Project metadata, mypy strict config
```

---

## Documentation

| Doc | Purpose |
|-----|---------|
| [SPEC.md](SPEC.md) | System design and architecture specs |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [AGENTS.md](AGENTS.md) | Agent workflow, message flow map, and operational learnings |
| [TEST.md](TEST.md) | Testing modes, commands, and test inventory |

---

**Version:** 0.45.0 · **Tests:** 349 pytest + bash + Go · **mypy:** 0 errors

---

## Acknowledgments

This project started as a fork of [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram) by Han Xiao, which provided the original single-session Telegram-to-Claude bridge concept. The project has since been rewritten and extended into a multi-worker, multi-machine, multi-backend team orchestration platform.

## License

MIT — see [LICENSE](LICENSE) for details.
