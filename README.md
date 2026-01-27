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

# 3. Run (one command!)
export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
./claudecode-telegram.sh run
```

Then from Telegram: `/new myproject` to create a Claude instance.

## Commands (Telegram)

| Command | Description |
|---------|-------------|
| `/new <name>` | Create Claude instance |
| `/use <name>` | Switch active session |
| `/list` | List all instances |
| `/kill <name>` | Stop instance |
| `/status` | Show status |
| `/stop` | Interrupt Claude |
| `/restart` | Restart Claude in session |
| `/system` | Show system config (secrets redacted) |
| `@name <msg>` | One-off message to specific instance |

## How it works

```
Telegram -> Cloudflare Tunnel -> Bridge -> tmux (Claude Code)
                                   ^
Claude Stop Hook -----> Bridge ----+----> Telegram
```

## Philosophy

- **Claude Code IS persistence** - Claude manages its own context and memory
- **LLM tokens stay low** - No context rebuilding, Claude remembers
- **Security by architecture** - Bot credentials never touch Claude
- **Zero external state** - No database, no state.json

See [DOC.md](DOC.md) for details.

## Credits

Original project by [Han Xiao](https://github.com/hanxiao) - [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram)

## License

MIT
