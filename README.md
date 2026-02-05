# Claude Code - Telegram

Run multiple AI workers from Telegram—research, operations, and development in one chat.

<img width="400" alt="image" src="https://github.com/user-attachments/assets/a12cbdbf-cf18-4ba4-8645-08a3a359559a" />


## What This Is

Claude Code - Telegram is a Telegram bot + bridge that lets you spin up and coordinate parallel AI workers from a single chat. Use it for research deep dives, operational tasks, or any workstream that benefits from fast, concurrent execution with persistent context.

## Quick Start

### For Managers (Telegram only)

1. Ask your operator to run the setup below.
2. Open the bot in Telegram.
3. Send `/hire myworker` and start assigning work.

### For Operators (one-time setup)

```bash
# macOS
brew install tmux cloudflared jq

# Debian/Ubuntu
sudo apt install tmux jq curl python3
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && sudo chmod +x /usr/local/bin/cloudflared

# clone + run
git clone https://github.com/beastoin/claudecode-telegram
cd claudecode-telegram
./claudecode-telegram.sh hook install
export TELEGRAM_BOT_TOKEN="<token from @BotFather>"
./claudecode-telegram.sh run
```

## Daily Use (Real Commands)

```
/hire ops
/hire triage
/hire research
/hire frontend
/hire qa

/ops Run 5 worker queue: scrape refunds, reconcile invoices, update CRM notes, draft escalation email, and ping @qa for flaky test owners
/triage Triage the latest GitHub issues; label, close dupes, and summarize top 10 with links
/research Compare auth flows across api/, web/, and mobile/ repos; highlight inconsistencies + suggested fix
/frontend Audit the settings UI for missing states and propose copy improvements
/qa Reproduce the top crash from yesterday and draft a minimal repro

@ops Coordinate with @frontend on status banner copy; @research share findings with @triage
/progress

(Later, after you sleep)
/progress
@ops Post the nightly summary + anything blocked
```

You can also drop a screenshot and ask, "What is wrong with this UI?"

## Core Commands (You'll Actually Use)

| Command | What it does |
|---------|--------------|
| `/hire <name>` | Add a worker |
| `/focus <name>` | Set who gets your next message |
| `/progress` | See if the focused worker is busy |
| `/team` | List workers + focus |
| `/end <name>` | Remove a worker |

Backend selection: `/hire alice --codex` (or `/hire codex-alice`), plus `--gemini`, `--opencode`, or `--backend <name>`.

## What to Expect (Message Flow)

1. You send a task.
2. Bot reacts with 👀 to confirm delivery.
3. Worker replies later as `worker_name: ...`.

## Troubleshooting (Top 3)

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Bot doesn't respond | Bridge down or you're not admin | Ask operator to run `./claudecode-telegram.sh status` |
| 👀 but no reply | Worker is busy or stuck | Run `/progress`, then `/relaunch` if needed |
| "No one assigned" | No focused worker | `/team` then `/focus <name>` |

## Gotchas & Limits

- **Single admin**: First person to message becomes admin unless `ADMIN_CHAT_ID` is set.
- **Focus resets + context persists**: After restart, run `/focus` again. Want a clean slate? `/end <name>` then `/hire <name>`.
- **Telegram limits**: Long replies split after 4096 chars.

## Project Structure

```
claudecode-telegram/
|-- bridge.py
|-- claudecode-telegram.sh
|-- hooks/
|-- DOC.md
`-- test.sh
```

## Manager Outcomes

- **Throughput while offline.** Run multiple workers in parallel so work continues after hours.
- **Less context tax.** Long-lived workers keep state, so you don't re-explain.
- **One place to coordinate.** Broadcast, delegate, and check status from a single chat.

## Real Results (From Our Team)

- **@chen** triaged 290 issues in one session and tagged priorities + root causes.
- **@geni** did deep research on 2 OSS projects, tracing end-to-end flows and dependencies.
- **Ops manager** keeps 5 workers running; code ships while they're offline.

## Where Data Lives

- **Messages stay in Telegram.** The bridge does not store message history elsewhere.
- **Worker context is the chat.** Each worker continues from the same ongoing Telegram thread.
- **Easy to resume.** Pick up any time from the existing chat history.

## Why This Architecture

- **Fewer places for data to live means lower risk.**
- **Less to secure and less to monitor.**
- **Easier reviews when you need to check what happened.**
- **Fewer moving parts to break.**

## Use Cases

- **Ops:** incident updates, checklists, status notes.
- **Research:** quick briefs, vendor comparisons, market scans.
- **Triage:** sort tickets, label issues, route requests.
- **Support:** draft replies, summarize threads, suggest next steps.

## Compounding Team Knowledge

- Keep a lightweight team memory with two shared files: `~/team/playbook.md` and `~/team/learnings.md`.
- Daily: ops manager asks for learnings/help, team adds quick notes.
- Result: the team gets smarter every day and repeats fewer mistakes.
- **[See our playbook template with real examples](TEMPLATE-PLAYBOOK.md)**


## Credits

Original project by Han Xiao (hanxiao/claudecode-telegram).

## License

MIT
