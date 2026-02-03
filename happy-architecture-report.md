# Happy Codebase Architecture Report

Research conducted on https://github.com/slopus/happy

## Executive Summary

Happy is a Claude Code wrapper that provides mobile/web access to Claude Code sessions running on desktop machines. Unlike our claudecode-telegram approach (tmux + hooks), Happy uses **direct process spawning with JSON streaming** and **WebSocket-based real-time communication**.

---

## 1. Core Architecture Differences

| Aspect | Happy | claudecode-telegram |
|--------|-------|---------------------|
| **Transport** | WebSocket (Socket.IO) | Telegram webhook |
| **Claude Integration** | `spawn()` with stdin/stdout pipes | tmux sessions |
| **Message Format** | ACP (Agent Communication Protocol) | Raw text |
| **State Storage** | Database (Prisma) + encrypted content | tmux sessions only |
| **Session Persistence** | One process, multiple messages via streaming | One process per message turn |
| **Response Capture** | stdout JSON streaming | Stop hooks + tmux capture |

---

## 2. Message Flow Architecture

### 2.1 Ingestion: WebSocket-Based (Not Webhooks)

Happy uses Socket.IO WebSockets for real-time bidirectional communication.

**Entry Point:** `packages/happy-server/sources/app/api/socket.ts`

```
Client connects to: /v1/updates
Authentication: Token-based verification
Connection Types:
  - session-scoped: CLI connected to specific session
  - user-scoped: Mobile/web connected to user account
  - machine-scoped: Daemon machine heartbeat
```

### 2.2 Message Routing

**Handler:** `packages/happy-server/sources/app/api/socket/sessionUpdateHandler.ts`

When a client sends a message:
1. **Message Reception** - Receives `{sid, message, localId}` via socket
2. **Session Validation** - Verifies session belongs to authenticated user
3. **Deduplication** - Checks if `localId` already exists
4. **Sequence Allocation** - Assigns user-level and session-level sequence numbers
5. **Persistence** - Stores encrypted message content
6. **Broadcasting** - Emits to relevant connected clients

### 2.3 Complete Flow Diagram

```
┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
│  User   │     │   CLI   │     │  Server │     │ Mobile  │
└────┬────┘     └────┬────┘     └────┬────┘     └────┬────┘
     │               │               │               │
     │  Type prompt  │               │               │
     │───────────────>               │               │
     │               │               │               │
     │               │ encrypt +     │               │
     │               │ socket.emit   │               │
     │               │──────────────>│               │
     │               │               │               │
     │               │               │ validate      │
     │               │               │ persist       │
     │               │               │ broadcast     │
     │               │               │──────────────>│
     │               │               │               │
     │               │  Claude SDK   │               │
     │               │<──────────────│               │
     │               │               │               │
     │               │ convert to    │               │
     │               │ ACP format    │               │
     │               │               │               │
     │               │ queue + send  │               │
     │               │──────────────>│               │
     │               │               │               │
     │               │               │ store +       │
     │               │               │ broadcast     │
     │               │               │──────────────>│
     │               │               │               │
     │  response     │               │               │
     │<──────────────│               │               │
```

---

## 3. Claude Code Integration

### 3.1 They Spawn Claude CLI (NOT Direct API)

Happy spawns the Claude Code CLI as a child process - they do NOT call Anthropic API directly.

**Core Spawning Code:** `packages/happy-cli/src/claude/sdk/query.ts:346-353`

```typescript
const child = spawn(spawnCommand, spawnArgs, {
    cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
    signal: config.options?.abort,
    env: spawnEnv,
}) as ChildProcessWithoutNullStreams
```

**CLI Arguments:**
```bash
claude --output-format stream-json --input-format stream-json --verbose
# Plus: --resume, --model, --permission-mode, --mcp-config, --allowedTools, etc.
```

### 3.2 How They Find Claude CLI

**File:** `packages/happy-cli/src/claude/sdk/utils.ts:137-175`

Priority order:
1. `HAPPY_CLAUDE_PATH` env var (explicit override)
2. Global `claude` command (`which claude`)
3. Bundled fallback (`node_modules/@anthropic-ai/claude-code/cli.js`)

Additional search locations:
- npm global installation
- Bun global installation
- Homebrew installation
- Native installer (`~/.local/bin/claude`)

### 3.3 Two Modes of Spawning

#### Local Mode (Interactive Terminal)

```typescript
// packages/happy-cli/src/claude/claudeLocal.ts:241-246
const child = spawn('node', [claudeCliPath, ...args], {
    stdio: ['inherit', 'inherit', 'inherit', 'pipe'],  // Terminal passthrough
    signal: opts.abort,
    cwd: opts.path,
    env,
});
```

- Uses `stdin: 'inherit'` - user types directly to Claude
- Launcher intercepts fetch to track "thinking" state via fd 3

#### Remote Mode (JSON Streaming)

```typescript
// packages/happy-cli/src/claude/sdk/query.ts:346
const child = spawn(spawnCommand, spawnArgs, {
    stdio: ['pipe', 'pipe', 'pipe'],  // Programmatic control
    ...
})
```

- Uses `--input-format stream-json` and `--output-format stream-json`
- Messages sent via `stdin.write(JSON.stringify(message) + '\n')`
- Responses read from stdout line by line as JSON

---

## 4. Session Persistence (Key Innovation)

### 4.1 One Process, Multiple Messages

Happy keeps ONE Claude CLI process running and streams multiple messages to it.

**File:** `packages/happy-cli/src/claude/claudeRemote.ts:148-162`

```typescript
// Create a pushable async iterable for streaming messages
let messages = new PushableAsyncIterable<SDKUserMessage>();

// Push initial message
messages.push({
    type: 'user',
    message: { role: 'user', content: initial.message },
});

// Start Claude Code with streaming input
const response = query({
    prompt: messages,  // AsyncIterable - NOT a string!
    options: sdkOptions,
});
```

### 4.2 Multi-Message Loop

```typescript
// packages/happy-cli/src/claude/claudeRemote.ts:168-217
for await (const message of response) {
    opts.onMessage(message);

    // When Claude finishes responding (result message)
    if (message.type === 'result') {
        updateThinking(false);
        opts.onReady();

        // Get NEXT message from user (blocks until available)
        const next = await opts.nextMessage();
        if (!next) {
            messages.end();  // No more messages, end the stream
            return;
        }

        // Push next message to the SAME running process
        messages.push({ type: 'user', message: { role: 'user', content: next.message } });
    }
}
```

### 4.3 PushableAsyncIterable Pattern

**File:** `packages/happy-cli/src/claude/utils/PushableAsyncIterable.ts`

```typescript
class PushableAsyncIterable<T> implements AsyncIterableIterator<T> {
    private queue: T[] = []
    private waiters: Array<{resolve, reject}> = []

    push(value: T): void {
        const waiter = this.waiters.shift()
        if (waiter) {
            waiter.resolve({ done: false, value })  // Deliver directly
        } else {
            this.queue.push(value)  // Queue for later
        }
    }

    end(): void {
        this.isDone = true  // Signal stream end
    }
}
```

### 4.4 When Process Restarts

Process only restarts (with `--resume`) when:
- Mode changes (permission, model)
- User aborts
- Switch between local/remote mode
- `/clear` command (no resume - fresh session)

| Scenario | Process Behavior | Context Preservation |
|----------|------------------|---------------------|
| Multiple messages, same mode | Same process | Full context in memory |
| Mode change | New process with `--resume` | Context from session file |
| User abort | New process with `--resume` | Context from session file |
| `/clear` command | New process, no `--resume` | Fresh session |

---

## 5. Hook Usage

### 5.1 They DO Use Hooks (SessionStart Only)

Happy uses Claude Code hooks, but only for **SessionStart** - not Stop hooks.

| Hook Type | Happy | claudecode-telegram |
|-----------|-------|---------------------|
| **SessionStart** | ✅ Track session ID changes | ❌ Not used |
| **Stop** | ❌ Not used | ✅ Capture responses |

### 5.2 Hook Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         HOOK SYSTEM ARCHITECTURE                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  runClaude.ts (startup)                                                  │
│       │                                                                  │
│       ├──► startHookServer()                                             │
│       │    └──► HTTP server on random port (e.g., 52290)                │
│       │        └──► Listens at POST /hook/session-start                 │
│       │                                                                  │
│       ├──► generateHookSettingsFile(port)                               │
│       │    └──► Creates ~/.happy/tmp/hooks/session-hook-<pid>.json      │
│       │                                                                  │
│       └──► spawn claude --settings <hookSettingsPath>                   │
│                                                                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  When Claude Session Changes (new/resume/compact/fork):                  │
│                                                                          │
│  Claude CLI ──► Executes hook command:                                  │
│       │        node session_hook_forwarder.cjs <port>                   │
│       │             │                                                    │
│       │             └──► HTTP POST to http://127.0.0.1:<port>/hook/...  │
│       │                       │                                          │
│       │                       └──► Updates Session.sessionId            │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.3 Why SessionStart Hook?

From `startHookServer.ts` documentation:

> File watching has race conditions when multiple Happy processes run.
> With hooks, Claude directly tells THIS specific process about its session,
> ensuring 1:1 mapping between Happy process and Claude session.

Use cases:
- Fresh start → new session created
- `--continue` → continues last session (may fork)
- `--resume` → resume specific session
- `/compact` command → compacts and forks session
- Double-escape fork → user forks conversation

---

## 6. Message Format: ACP (Agent Communication Protocol)

**File:** `packages/happy-cli/src/api/apiSession.ts:19-38`

Supported message types:
- `message` - Text content
- `reasoning` / `thinking` - Model reasoning
- `tool-call` - Tool invocations
- `tool-result` - Tool outputs
- `file-edit` - File modifications with diffs
- `terminal-output` - Bash outputs
- `task_started` / `task_complete` / `turn_aborted` - Lifecycle events
- `permission-request` - Permission prompts
- `token_count` - Usage metrics

### Message Schemas

**User Messages (from mobile/web):**
```typescript
{
    role: 'user',
    content: {type: 'text', text: string},
    localKey?: string,
    meta?: {
        sentFrom?, permissionMode?, model?,
        fallbackModel?, customSystemPrompt?,
        allowedTools?, disallowedTools?
    }
}
```

**Agent Messages (from Claude):**
```typescript
{
    role: 'agent',
    content: {
        type: 'output' | 'acp' | 'codex' | 'event',
        data: provider_specific_format
    }
}
```

---

## 7. Key Files Reference

| Component | File | Purpose |
|-----------|------|---------|
| Socket Setup | `happy-server/.../socket.ts` | WebSocket connection handling |
| Message Handler | `happy-server/.../sessionUpdateHandler.ts` | Message routing and persistence |
| Event Router | `happy-server/.../eventRouter.ts` | Broadcasting to connected clients |
| API Session Client | `happy-cli/.../apiSession.ts` | ACP message handling |
| SDK Converter | `happy-cli/.../sdkToLogConverter.ts` | Claude output parsing |
| Message Queue | `happy-cli/.../OutgoingMessageQueue.ts` | Message ordering |
| Query/Spawn | `happy-cli/.../sdk/query.ts` | Claude CLI spawning |
| Session Management | `happy-cli/.../claudeRemote.ts` | Multi-message streaming |
| Hook Server | `happy-cli/.../utils/startHookServer.ts` | SessionStart hook handling |
| Hook Settings | `happy-cli/.../utils/generateHookSettings.ts` | Hook config generation |

---

## 8. Learnings for claudecode-telegram

### What Happy Does Better

1. **Single process for multiple messages** - More efficient than spawning per turn
2. **Structured JSON streaming** - Cleaner than regex parsing tmux output
3. **Direct process control** - No terminal emulation overhead
4. **SessionStart hooks** - Better session ID tracking than file scraping

### Why We Use Our Approach

1. **Simplicity** - tmux + hooks is simpler to set up and debug
2. **No custom CLI wrapper** - Works with stock Claude Code
3. **Terminal preservation** - Users can attach to tmux and interact directly
4. **Minimal dependencies** - Just bash, Python, and tmux

### Potential Improvements

1. Consider using `--output-format stream-json` for cleaner response parsing
2. Consider using `--input-format stream-json` for programmatic input
3. Add SessionStart hook support for better session tracking
4. Explore keeping process alive between messages (like Happy does)

---

## 9. Conclusion

Happy represents a more sophisticated approach to wrapping Claude Code, with proper JSON streaming, WebSocket communication, and database persistence. However, it comes with significantly more complexity.

Our claudecode-telegram approach trades some efficiency for simplicity - using tmux as the "process manager" and Stop hooks for response capture. This makes it easier to debug and allows direct terminal access to sessions.

The key insight from Happy is that `--input-format stream-json` + `--output-format stream-json` enables keeping a single Claude process alive for multiple message turns, which could significantly improve our efficiency if we moved away from tmux-based session management.
