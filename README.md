# claudecode-telegram

> **Personal fork of [@beastoin](https://github.com/beastoin). If you're not me, don't run this.**
>
> Original repo: [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram)

Telegram bot bridge for Claude Code. Multi-session support with security hardening.

## Quick Start

```bash
# 1. Install deps
brew install tmux cloudflared jq

# 2. Clone
git clone https://github.com/beastoin/claudecode-telegram
cd claudecode-telegram

# 3. Install hooks
cp hooks/*.sh hooks/*.py ~/.claude/hooks/

# 4. Run
export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
./claudecode-telegram.sh run
```

Then from Telegram: `/hire myproject` to create your first AI worker.

## Commands (Telegram)

| Command | Alias | Description |
|---------|-------|-------------|
| `/hire <name>` | `/new` | Hire a long-lived AI worker |
| `/focus <name>` | `/use` | Focus on a worker |
| `/team` | `/list` | Show your team |
| `/end <name>` | `/kill` | Offboard a worker |
| `/progress` | `/status` | Check focused worker status |
| `/pause` | `/stop` | Pause focused worker |
| `/relaunch` | `/restart` | Relaunch focused worker |
| `/settings` | `/system` | Show system config |
| `/learn` | - | Ask focused worker what they learned |
| `@name <msg>` | - | One-off message to specific worker |
| `@all <msg>` | - | Broadcast to all workers |
| `[photo]` | - | Send image to focused worker |

## How it works

```
Telegram -> Cloudflare Tunnel -> Bridge -> tmux (Claude Code)
                                   ^
Claude Stop Hook -----> Bridge ----+----> Telegram
```

## Philosophy

- **Workers are long-lived** - They keep context across restarts
- **tmux IS persistence** - No database, no state.json
- **Image support** - Send screenshots, receive diagrams
- **Security by architecture** - Bot credentials never touch Claude

See [DOC.md](DOC.md) for details.

## Project Structure

```
claudecode-telegram/
├── bridge.py              # Telegram webhook handler
├── claudecode-telegram.sh # CLI wrapper, tunnel/webhook setup
├── hooks/
│   ├── send-to-telegram.sh  # Claude Stop hook (bash)
│   └── forward-to-bridge.py # Forward response to bridge (python)
├── CLAUDE.md              # Project instructions
├── DOC.md                 # Design philosophy, changelog
├── TEST.md                # Testing documentation
└── test.sh                # Acceptance tests
```

## Credits

Original project by [Han Xiao](https://github.com/hanxiao) - [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram)

## License

MIT
