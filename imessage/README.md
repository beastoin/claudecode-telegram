# Claude Code iMessage

Manage Claude Code workers from iMessage on macOS.

## What This Is

An iMessage bridge that lets you run Claude Code workers and control them via text message. Send tasks from your phone, get responses back. Workers persist across restarts via tmux.

For managers: this runs locally on your Mac and only watches **new** incoming iMessages after the bridge starts. It does not upload your message history anywhere.

## Requirements

- **macOS** with Messages.app signed into Apple ID
- **Full Disk Access** permission (to read Messages database)
- **Automation** permission (to send messages via AppleScript)
- **tmux** installed (`brew install tmux`)

## Quick Start

```bash
# 1. Install the Claude Code hook
./scripts/claudecode-imessage.sh hook install

# 2. Grant permissions when prompted (first run)
./scripts/claudecode-imessage.sh run
```

Then from iMessage:
```
/hire backend
Hello, can you check the API docs?
```

## Commands (via iMessage)

| Command | What it does |
|---------|--------------|
| `/hire <name>` | Create a worker |
| `/focus <name>` | Set active worker |
| `/team` | List workers |
| `/progress` | Check if worker is busy |
| `/end <name>` | Remove worker |
| `/pause` | Send Escape to stop current task |
| `@name message` | Send to specific worker |
| `@all message` | Broadcast to all |

## How It Works

```
Your iMessage
   ↓  (new incoming messages only)
Messages database (chat.db)
   ↓  (bridge watches for new rows)
claudecode-imessage bridge
   ↔  tmux workers (Claude Code)
   ↓  (responses)
Messages.app → iMessage back to you
```

- **Inbound**: The bridge reads only new incoming messages after it starts (not your history).
- **Outbound**: Worker replies go through the Claude stop hook, then AppleScript sends the response.
- **Local-only**: Everything runs on your Mac; no external network access is required.

## Who Can Message My Workers?

By default, **anyone who messages your Mac** after the bridge starts can interact with workers.
Messages sent from the same Apple ID are ignored to avoid loops.

You can lock this down in two ways:

1) **Allowlist** (recommended):
   - Set `IMESSAGE_ALLOWED_HANDLES` to a comma-separated list of phone numbers/emails.
   - Only those handles can trigger commands or send tasks.

2) **Single-admin mode** (optional):
   - Set `IMESSAGE_AUTO_LEARN_ADMIN=1` to make the **first allowed sender** the admin.
   - After that, all other handles are ignored.

Tip: If you only want *your* number to control the bridge, set the allowlist to just yourself and enable auto-learn.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `IMESSAGE_BRIDGE_PORT` | 8083 | HTTP server port |
| `IMESSAGE_ALLOWED_HANDLES` | (all) | Comma-separated phone/email allowlist |
| `IMESSAGE_POLL_INTERVAL` | 2 | Seconds between DB polls |
| `IMESSAGE_AUTO_LEARN_ADMIN` | 0 | Auto-learn first sender as admin (locks to one sender) |

## Permissions Setup

### Full Disk Access (required to read messages)
1. Open **System Settings** → **Privacy & Security** → **Full Disk Access**
2. Click **+** and add **Terminal** (or your terminal app)

### Automation (required to send messages)
1. Open **System Settings** → **Privacy & Security** → **Automation**
2. Enable **Messages** for **Terminal** (or your terminal app)

The bridge checks permissions on startup and tells you what's missing.

## Is This Safe?

Short answer: **it can be safe if you restrict who can message it**.

- **Runs locally** on your Mac; no inbound network port is exposed beyond `127.0.0.1`.
- **Reads only new incoming messages** after it starts; it does not scan your old chat history.
- **Needs powerful permissions** (Full Disk Access + Automation) because it reads Messages and sends replies.
- **Data sent to Claude**: Any message you route to a worker becomes a Claude Code prompt.
- **Logs**: `~/.claude/imessage/bridge.log` can include short previews of incoming messages.
- **Best practice**: Use a dedicated macOS user + Apple ID or a separate Mac if you want a clean boundary.

If you set an allowlist (and optionally single-admin mode), it is reasonable to run on a personal Mac.

## Your First Worker (Step-by-Step)

1) **Pick who can control it** (recommended):
   ```bash
   export IMESSAGE_ALLOWED_HANDLES=+15551234567
   export IMESSAGE_AUTO_LEARN_ADMIN=1
   ```
2) **Install the hook and start the bridge**:
   ```bash
   ./scripts/claudecode-imessage.sh hook install
   ./scripts/claudecode-imessage.sh run
   ```
3) **Text your Mac's iMessage account from a different device**:
   - Your Mac is signed into Messages with an Apple ID (e.g., `you@icloud.com` or phone number)
   - From your **phone** (using a **different** Apple ID or phone number), text that address
   - Send `/hire assistant`
   - **Note**: Messages from the same Apple ID are ignored to avoid loops
4) **What happens next**:
   - The bridge sees the new message, verifies it is allowed, and creates a worker.
   - You get a confirmation message back in iMessage.
5) **Send a task**:
   - Text: `Review the onboarding docs and suggest improvements.`
6) **Check status**:
   - Send `/progress` or `/team`.

## Recommended Safe Defaults

- `IMESSAGE_ALLOWED_HANDLES` set to only your number or a small, trusted list.
- `IMESSAGE_AUTO_LEARN_ADMIN=1` if you want a single controller.
- Run on a dedicated macOS user + Apple ID if you share a laptop.

## Files

```
~/.claude/imessage/
├── bridge.pid          # Process ID
├── bridge.log          # Logs
└── sessions/
    └── <worker>/
        ├── chat_handle # Phone/email
        ├── pending     # Request in progress
        └── last_rowid  # Last processed message
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Full Disk Access required" | Add Terminal to FDA in System Settings |
| "Automation permission required" | Enable Messages automation in System Settings |
| Messages not detected | Check Messages.app is running and signed in |
| No response | Run `/progress`, check `~/.claude/imessage/bridge.log` |

## Differences from Telegram Version

| Aspect | Telegram | iMessage |
|--------|----------|----------|
| Platform | Any | macOS only |
| Inbound | Webhook (push) | chat.db (poll) |
| Outbound | HTTP API | AppleScript |
| Setup | Bot token | macOS permissions |
| Tunnel | Required | Not needed |

## License

MIT
