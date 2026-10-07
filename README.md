# claudecode-telegram

Manage multiple AI coding workers from a single Telegram chat. Message your bot, it routes to workers on your machines, they reply back in Telegram.

<table>
<tr>
<td><img width="320" alt="Worker chat" src="https://github.com/user-attachments/assets/987c93d9-4f8c-43b3-8385-9c0259991f67" /></td>
<td><img width="320" alt="Team status" src="https://github.com/user-attachments/assets/df3ab259-c505-46d7-99ae-c8f3e1284846" /></td>
<td><img width="320" alt="Multi-worker" src="https://github.com/user-attachments/assets/97c06219-7ba1-47de-8c10-526ad5c2a208" /></td>
</tr>
<tr>
<td colspan="2"><img width="640" alt="Terminal viewer" src="https://github.com/user-attachments/assets/f97c0de7-305e-418c-8426-a1ffb812507f" /></td>
<td></td>
</tr>
</table>

---

## How it works

```
Telegram ──webhook──► bridge.py ──tmux──► Claude worker sessions
                         │                       │
                         │                       │ hook fires on stop
                         │                       ▼
                         │◄──── POST /outputs ──── claudecode.sh
```

The bridge is a Python HTTP server that receives Telegram webhooks and routes messages to tmux sessions running AI workers. Each worker is an independent Claude Code (or Codex) session. No database — tmux sessions are the persistence layer.

---

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/beastoin/claudecode-telegram/main/install.sh | bash
```

The installer clones the repo, installs Python 3.12+, tmux, Node.js, Claude CLI — asks for your bot token from [@BotFather](https://t.me/BotFather) — installs hooks — done. Then send `/hire myworker` to your bot on Telegram.

**Already set up?** Just `cd claudecode-telegram && ./bridge.sh run`.

---

## Commands

| Command | What it does |
|---------|-------------|
| `/hire <name>` | Create a worker |
| `/focus <name>` | Set which worker gets bare messages |
| `/team` | List all workers with health state |
| `/end <name>` | Remove a worker |
| `/restart [name]` | Restart worker (resumes context) |
| `/teleport <name> <host>` | Move worker to another machine |
| `/teleback <name>` | Bring teleported worker back |
| `/settings` | Show bridge configuration |
| `/pilot <name>` | Open web terminal viewer |
| `/relay <worker>` | Open public channel for a worker |
| `/rewind <name>` | Open transcript viewer |
| `/pr <url>` | PR review viewer |
| `@name <msg>` | One-off message to named worker |
| `@all <msg>` | Broadcast to all workers |

**Backend selection:** `/hire codex-alice` (prefix) or `/hire alice --backend codex` (flag).

**Shell commands:** `./bridge.sh run`, `stop`, `restart`, `status`, `hook install`. Always use `./bridge.sh stop` — never raw `kill`.

---

## Features

- **Multi-worker** — hire, end, focus, restart workers from Telegram
- **Multi-backend** — Claude Code, Codex
- **Multi-machine** — teleport workers between hosts via SSH
- **Worker-to-worker** — direct P2P messaging via pipes or tmux
- **Connectors** — Gmail and GitHub polling with sender allowlists
- **Voice transcription** — STT for incoming Telegram voice messages
- **Pilot** — web terminal viewer for live worker sessions
- **Relay** — public channels for worker interaction
- **PR review** — interactive PR review in Telegram
- **Health monitoring** — watchdog with activity extraction, stuck/poisoned detection
- **Clean chat** — 👀 = received, `name: ...` replies, bridge speaks only for errors

---

## Project Structure

```
claudecode-telegram/
├── bridge.py              # Control plane — routing, workers, health, machines
├── claudecode.py          # Runtime primitives — tmux, backends, sessions, SSH
├── telegram.py            # Human interface — formatting, transport, parsing
├── bridge.sh              # CLI wrapper — start/stop, tunnel, webhook setup
├── claudecode.sh          # Claude Code hook — stop/start/tool-failure events
├── connectors.py          # Gmail, GitHub polling integrations
├── review.py              # PR review page generator
├── indexer.py             # JSONL transcript indexer (SQLite FTS5)
├── install.sh             # One-line installer
├── tests/                 # pytest suite (439 tests)
├── test.sh                # Acceptance tests (bash integration)
├── pilot/                 # Terminal viewer (Node.js)
└── pyproject.toml         # Project metadata, mypy config
```

---

## Documentation

| Doc | Purpose |
|-----|---------|
| [SPEC.md](SPEC.md) | System design and architecture |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [AGENTS.md](AGENTS.md) | Agent workflow, message flow map, learnings |
| [TEST.md](TEST.md) | Testing modes, commands, test inventory |

---

**Version:** 0.47.0 · **Tests:** 439 pytest + bash integration

---

## Acknowledgments

This project started as a fork of [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram) by Han Xiao, which provided the original single-session Telegram-to-Claude bridge concept. The project has since been rewritten into a multi-worker, multi-machine, multi-backend team orchestration platform.

## License

MIT — see [LICENSE](LICENSE) for details.
