---
name: bridge
description: "How to interact with the Telegram bridge (bridge.py) that connects Claude Code workers to the manager. Covers sending files/images, messaging teammates, and the bridge API. Use this skill whenever you need to send a file or image to Telegram, message another worker, check who's online, or understand how your responses reach the manager. Also use it when you see [[image:]] or [[file:]] syntax, or need to use BRIDGE_URL."
---

# Bridge Interaction Guide

You are a Claude Code worker running on the claudecode-telegram bridge. This guide covers how your responses reach Telegram and how to interact with teammates.

## How Your Responses Reach Telegram

The **Stop hook** (`hooks/send-to-telegram.sh`) fires after every assistant text turn. It extracts your response text and POSTs it to the bridge's `/response` endpoint. The bridge converts markdown to Telegram HTML and sends it to the manager's chat.

**The bridge only sees your response text.** Output from Bash tool calls, file reads, or any other tool stdout is invisible to the bridge — it stays inside your Claude Code session. This is the single most important thing to understand: if you want something to reach Telegram, it must be in your response text, not inside a tool call.

## Sending Files & Images

Use media tags **directly in your response text** to send files to Telegram:

```
[[image:/path/to/photo.png|Optional caption]]
[[file:/path/to/document.pdf|Optional caption]]
```

**Image tag** — for photos and animations:
- Formats: jpg, jpeg, png, webp, bmp (photos), gif, mp4 (animations)
- Telegram auto-displays these inline in the chat

**File tag** — for any non-blocked file under 50 MB:
- Video: mp4, mov, avi, mkv, webm (shows video player in Telegram)
- Audio: mp3, m4a, flac, aac, wav (shows audio player)
- Voice: ogg, opus, oga (shows as voice bubble)
- Documents: everything else (pdf, xlsx, csv, txt, json, zip, etc.)

**Speak tag** — trigger text-to-speech:
- `[[speak:custom text]]` — synthesizes and sends the custom text as a voice message

**Rules:**
- Tags must be in your response text, outside of any tool call
- Use absolute paths (`/home/claude/...`) — do NOT use `~/`, the parser doesn't expand it
- Captions are optional — omit the `|caption` part if not needed: `[[image:/tmp/chart.png]]`
- Telegram limit: 50 MB per file
- For remote workers (Mac Mini): use your local path — the bridge auto-SCPs from the remote host
- To escape a tag (show it literally without sending): prefix with backslash: `\[[image:...]]`
- Tags inside code blocks (`` ` `` or ` ``` `) are not parsed — safe to discuss the syntax
- Tags are lowercase only — `[[Image:...]]` or `[[FILE:...]]` won't parse
- Path cannot contain `|` or `]`; caption cannot contain `]`
- Security: `.pem`, `.key`, `.env`, `id_rsa`, `credentials` and similar sensitive files are blocked (local workers only)

### Common mistake: media tags inside Bash

```bash
# WRONG — bridge never sees this (Bash stdout is invisible to the hook)
echo "[[image:/tmp/chart.png|My chart]]"

# WRONG — there is no /message endpoint
curl -X POST "$BRIDGE_URL/message" -d '{"text": "[[image:/tmp/chart.png]]"}'

# WRONG — printf in Bash is still tool stdout
printf '[[file:/tmp/report.pdf|Report]]'
```

The fix is always the same: put the tag in your response text. Generate the file with tools, then mention it in your response:

```
I've generated the chart. Here it is:

[[image:/tmp/chart.png|Sales Q3]]
```

## Messaging Teammates

All teammates are Claude Code agents on the same bridge. To message one:

### Step 1: Discover who's online

```bash
curl -s "$BRIDGE_URL/workers?from={your_name}"
```

This returns JSON with every active worker, their machine, and a ready-to-use `send_example` command (when available). Always call this first — never guess tmux session names or hardcode addresses.

### Step 2: Use the send_example (or POST /send)

Most workers include a `send_example` field. Use it directly. If a worker has `protocol: "none"` (exited) or `protocol: "adapter"` (no direct tmux), use `POST /send` instead (see below).

For a local (VPS) worker:

```bash
echo 'lee: your message here' | tmux load-buffer - && tmux paste-buffer -p -r -t claude-prod-aki && sleep 1 && tmux send-keys -t claude-prod-aki Enter
```

For a remote (Mac Mini) worker, it wraps in SSH automatically:

```bash
ssh beastoin-agents-f1-mac-mini "echo 'lee: your message here' | tmux load-buffer - && tmux paste-buffer -p -r -t claude-prod-x && sleep 1 && tmux send-keys -t claude-prod-x Enter"
```

**Rules:**
- Always prefix your name (`lee: message`) so the recipient knows who sent it
- Replace `YOUR_NAME` in the send_example with your actual name
- Never output `"name: message"` in your response text — that goes to Telegram (the manager), not to the worker
- The `sleep 1` before Enter is important — the TUI needs time to render the pasted text

### Alternative: POST /send

For simpler messaging (no tmux quoting needed):

```bash
curl -s -X POST "$BRIDGE_URL/send" \
  -H 'Content-Type: application/json' \
  -d '{"worker": "aki", "message": "can you review my PR?", "from": "lee"}'
```

The `from` field (default: "system") is prefixed to the message automatically.

## Bridge API Reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/workers?from={name}` | GET | List active workers with send commands |
| `/send` | POST | Send a prompt to a worker: `{worker, message, from}` |
| `/checkin?name={name}` | GET | Refresh your bridge instructions |
| `/notify` | POST | Send notification to all admin chats: `{text, name}` |
| `/health/workers` | GET | Watchdog state for all workers |

There is **no** `/message` endpoint, no polling endpoint, and no way to read incoming messages via API. Messages from the manager arrive as prompts in your Claude Code session automatically.

## Manager Commands (for context)

These are Telegram commands the manager uses — you don't invoke them, but knowing them helps you understand what's happening:

| Command | What it does |
|---------|-------------|
| `/hire <name>` | Create a new worker |
| `/end <name>` | Stop a worker (preserves identity) |
| `/restart <name>` | Restart a worker (with session resume) |
| `/progress [name]` | Check worker status |
| `/team` | List all workers and their status |
| `/focus <name>` | Switch which worker receives messages |
| `@name message` | Route a message to a specific worker |

## Quick Reference

**Send image:** `[[image:/absolute/path.png|caption]]` in response text

**Send file:** `[[file:/absolute/path.pdf|caption]]` in response text

**Message teammate:** `curl -s "$BRIDGE_URL/workers?from={name}"` → use `send_example`, or `POST /send`

**Refresh instructions:** `curl -s "$BRIDGE_URL/checkin?name={name}"`

**Notify all admins:** `POST /notify` with `{text, name}`

Path must be absolute. Caption is optional (omit `|caption`). Tags go in response text, never in Bash.
