# claudecode-imessage Specification

## Overview

iMessage bridge for Claude Code workers. Ported from claudecode-telegram, preserving core architecture (tmux sessions, file-based state, hook coordination) while replacing Telegram transport with iMessage.

## Architecture

```
INBOUND (iMessage → Claude Code):
┌─────────────────┐
│  Messages.app   │
│    chat.db      │
└────────┬────────┘
         │ (FSEvents + poll)
         v
┌─────────────────┐
│ imessage_bridge │ (receiver thread)
│  127.0.0.1:8083 │
└────────┬────────┘
         │ (route to tmux)
         v
┌─────────────────┐
│  tmux session   │
│ (Claude Code)   │
└─────────────────┘

OUTBOUND (Claude Code → iMessage):
┌─────────────────┐
│  tmux session   │
│ (Claude Code)   │
└────────┬────────┘
         │ (stop hook fires)
         v
┌─────────────────┐
│ send-to-imessage│ (hook script)
│     .sh         │
└────────┬────────┘
         │ POST /response
         v
┌─────────────────┐
│ imessage_bridge │
│  127.0.0.1:8083 │
└────────┬────────┘
         │ (AppleScript)
         v
┌─────────────────┐
│  Messages.app   │
└─────────────────┘
```

## System Requirements

- **macOS** with Messages.app logged into Apple ID
- **Full Disk Access** permission for Terminal/bridge (to read chat.db)
- **Automation** permission to control Messages.app
- **GUI session** available (AppleScript needs it)
- **tmux** installed

## Core Components

### 1. bridge.py
Main bridge process:
- Local HTTP server on 127.0.0.1:8083
- Endpoints: POST /response, POST /notify
- Routes outbound messages via AppleScript
- Manages tmux sessions (create, kill, send)
- Per-session file coordination (pending, chat_handle)

### 2. imessage/receiver.py
Inbound message detection:
- Watches ~/Library/Messages/chat.db via FSEvents
- Periodic poll fallback (default 2s)
- Tracks last_message_rowid per session
- Filters out self-authored messages
- Routes new messages to tmux sessions

### 3. imessage/sender.py
Outbound message sending:
- AppleScript to Messages.app
- Ensures Messages.app is running
- Handles chunking (default 3500 chars)
- Retry with backoff on failure

### 4. imessage/db.py
chat.db SQLite reader:
- Read-only access
- Query new messages since rowid
- Parse handle, text, date, is_from_me
- Handle WAL timing issues

### 5. imessage/watch.py
File system watcher:
- FSEvents-based watching on macOS
- Fallback to polling
- Debounce rapid changes

### 6. hooks/send-to-imessage.sh
Claude stop hook:
- Reads response from transcript/tmux
- Posts to bridge /response endpoint
- No Apple ID or system perms in Claude env

## Data Flow

### Incoming Message
1. User sends iMessage
2. Messages.app writes to chat.db
3. Receiver detects change (FSEvents/poll)
4. Query new messages since last_rowid
5. Filter: skip is_from_me=1
6. Resolve session by handle or create new
7. Send message to tmux session
8. Update last_rowid

### Outgoing Message
1. Claude Code stops (user presses Escape or task completes)
2. Stop hook fires, extracts response
3. Hook POSTs to bridge /response
4. Bridge looks up chat_handle from session file
5. Bridge calls AppleScript sender
6. Messages.app sends iMessage
7. Clear pending file

## Session State (Files)

```
~/.claude/imessage/sessions/<worker-name>/
├── chat_handle    # Phone/email (e.g., +1234567890, user@icloud.com)
├── last_rowid     # Last processed message rowid from chat.db
├── pending        # Timestamp when request started
└── (minimal - same philosophy as Telegram)
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| IMESSAGE_BRIDGE_PORT | 8083 | HTTP server port |
| IMESSAGE_DB_PATH | ~/Library/Messages/chat.db | Messages database |
| IMESSAGE_POLL_INTERVAL | 2 | Seconds between polls |
| IMESSAGE_ALLOWED_HANDLES | (empty = all) | Comma-separated allowlist |
| IMESSAGE_AUTO_LEARN_ADMIN | 0 | Auto-learn first sender as admin |
| IMESSAGE_MAX_CHUNK_LEN | 3500 | Max message chunk size |
| SESSIONS_DIR | ~/.claude/imessage/sessions | Session files |
| TMUX_PREFIX | claude-imessage- | tmux session prefix |

## Commands (via iMessage)

| Command | Description |
|---------|-------------|
| /hire <name> | Create new worker |
| /focus <name> | Set active worker |
| /team | List workers |
| /progress | Check if active worker busy |
| /end <name> | Kill worker |
| /pause | Send Escape to active worker |
| @name message | Route to specific worker |
| @all message | Broadcast to all workers |

## Security Model

### Permissions Required
- **Full Disk Access**: Read ~/Library/Messages/chat.db
- **Automation**: Control Messages.app via AppleScript

### Isolation
- Bridge binds to 127.0.0.1 only (no external access)
- No Apple ID credentials in Claude environment
- Per-session files have 0o600 permissions
- Directories have 0o700 permissions

### Recommendations
- Use dedicated macOS user + Apple ID for bridge
- Use IMESSAGE_ALLOWED_HANDLES to restrict who can interact
- Don't log message content in production

## Error Handling

| Error | Behavior |
|-------|----------|
| Missing FDA permission | Fail loudly with instructions |
| Missing Automation permission | Fail loudly with instructions |
| Messages.app not running | Auto-launch, retry |
| chat.db locked | Retry with backoff |
| AppleScript send fails | Retry 3x, then log error |
| Unknown handle | Create new session or reject |

## Testing Strategy

1. **Unit tests**: Import checks, message parsing
2. **Integration tests**:
   - Mock chat.db with test messages
   - AppleScript dry-run mode
   - Session file creation/cleanup
3. **Manual tests**: Real Messages.app on dev macOS

## Differences from Telegram

| Aspect | Telegram | iMessage |
|--------|----------|----------|
| Inbound | Webhook (push) | chat.db watcher (poll) |
| Outbound | HTTP API | AppleScript |
| Auth | Bot token | macOS permissions |
| Identifier | chat_id (int) | handle (phone/email) |
| Tunnel | Required (cloudflared) | Not needed (local) |
| Platform | Any | macOS only |
| Message limit | 4096 chars | ~20000 chars (practical) |

## Version History

### v0.1.0 (Initial)
- Port from claudecode-telegram v0.10.2
- AppleScript sender
- chat.db receiver with FSEvents + polling
- Command parity with Telegram version
