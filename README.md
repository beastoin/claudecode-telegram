# claudecode-telegram

> Control your AI dev team from Telegram.

Telegram bot that lets you manage multiple Claude Code workers via chat. Like having a remote AI dev team you message from your phone.

## What You Can Do

- **Hire workers**: `/hire api` — spin up a new AI dev
- **Assign tasks**: Just type or send `/api Build the billing endpoints`
- **Check status**: `/progress` — see if they're working
- **Get updates**: Workers reply when done as `worker_name: ...`
- **Send screenshots**: Drop an image, worker analyzes it

---

## Setup (Do Once)

### For Operators (technical)

```bash
# 1. Install dependencies
brew install tmux cloudflared jq   # macOS
# apt install tmux jq && snap install cloudflared   # Linux

# 2. Clone and enter
git clone https://github.com/beastoin/claudecode-telegram
cd claudecode-telegram

# 3. Get bot token from @BotFather on Telegram
# Create bot, copy token

# 4. Install hooks
./claudecode-telegram.sh hook install

# 5. Run (token required)
export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
./claudecode-telegram.sh run
```

### For Managers (non-technical)

1. Ask your operator to run the setup above
2. Open Telegram, find your bot
3. Send `/hire myworker` — you're ready!

---

## Daily Use (Telegram Only)

### Core Commands

| Command | What it does |
|---------|--------------|
| `/hire <name>` | Hire a new AI worker |
| `/team` | See all your workers |
| `/focus <name>` | Switch who gets your messages |
| `/progress` | Check if focused worker is busy |
| `/pause` | Interrupt focused worker |
| `/relaunch` | Restart a stuck worker |
| `/end <name>` | Remove a worker |
| `/learn` | Ask worker what they learned today |

### Quick Patterns

```
/hire api                    — Hire worker named "api"
/focus api                   — Talk to api
Build the billing endpoints  — Task goes to focused worker (api)
/frontend Fix the login bug  — Shortcut: switch focus + send task
@api Add webhook payloads    — One-off message without changing focus
```

### What to Expect (Message Flow)

1. You send a task
2. Bot reacts with 👀 to confirm delivery
3. Worker replies later as `worker_name: ...`
4. If nothing comes back, use `/progress` or `/relaunch`

---

## Day in the Life (Example Conversation)

```
08:45  You: /hire api
       Bot: Api is now on your team and assigned.

08:46  You: /hire frontend
       Bot: Frontend is now on your team and assigned.

08:47  You: /team
       Bot: Your team:
            Focused: frontend
            Workers:
            - api (available)
            - frontend (focused, available)

08:50  You: /api Draft the billing API endpoints and error codes.
       Bot: 👀 (reaction)

08:52  You: /frontend Sketch the billing UI sections + fields.
       Bot: Now talking to Frontend.
       Bot: 👀 (reaction)

09:05  frontend: UI plan:
                 1) Pricing summary
                 2) Payment method form
                 3) Invoices table
                 ...

09:06  You: @api Also include webhook payloads for failed charges.
       Bot: 👀 (reaction)

09:10  You: /progress
       Bot: Progress for focused worker: frontend
            Focused: yes
            Working: no
            Online: yes
            Ready: yes

09:12  api: Endpoints + error codes + webhook payloads:
            ...

09:20  You: /learn
       Bot: 👀 (reaction)

09:22  frontend: Problem: Was hardcoding paths
                 Fix: Use environment variables
                 Why: Makes deployment flexible

09:30  You: /end frontend
       Bot: Frontend removed from your team.
```

---

## Troubleshooting

| Symptom | Usually means | What to do |
|---------|---------------|------------|
| Bot doesn't respond to anything | Bridge not running or you're not admin | Ask operator to check `./claudecode-telegram.sh status` |
| Bot reacts 👀 but no reply | Worker still busy or stuck | `/progress` to check. If `Ready: no`, try `/relaunch` |
| "No one assigned" | No focused worker | `/team` then `/focus <name>` |
| Wrong worker answered | Focus on wrong worker | `/team` to see focus, `/focus <name>` to switch |
| Image didn't reach worker | No focused worker | `/focus <name>` then resend image |
| Worker not responding after /relaunch | Worker crashed | `/end <name>` then `/hire <name>` for fresh start |

### Operator Commands

```bash
./claudecode-telegram.sh status          # Check if running
./claudecode-telegram.sh --node prod restart   # Restart
./claudecode-telegram.sh hook install --force  # Reinstall hooks
```

---

## Gotchas & Limits

- **Single admin**: First person to message becomes admin (or set `ADMIN_CHAT_ID`)
- **Focus resets on restart**: Use `/focus` again after operator restarts
- **Workers are long-lived**: They keep context. Want fresh start? `/end` + `/hire`
- **Reserved names**: Can't name workers `team`, `focus`, `hire`, etc.
- **Replies aren't streaming**: You see reply when Claude finishes the step
- **Long messages split**: Telegram has 4096 char limit, long replies come in parts

---

## How It Works

```
Telegram → Cloudflare Tunnel → Bridge → tmux (Claude Code)
                                 ↑
Claude Stop Hook → Bridge ───────┴──→ Telegram
```

- **Workers are tmux sessions** — survive restarts, no database
- **Bot token never touches Claude** — security by architecture
- **Images supported both ways** — send screenshots, receive diagrams

---

## Project Structure

```
claudecode-telegram/
├── bridge.py              # Telegram webhook handler
├── claudecode-telegram.sh # CLI wrapper, tunnel/webhook setup
├── hooks/
│   ├── send-to-telegram.sh  # Claude Stop hook
│   └── forward-to-bridge.py # Forward response to bridge
├── DOC.md                 # Design philosophy, changelog
└── test.sh                # Acceptance tests
```

## Credits

Original project by [Han Xiao](https://github.com/hanxiao) — [hanxiao/claudecode-telegram](https://github.com/hanxiao/claudecode-telegram)

## License

MIT
