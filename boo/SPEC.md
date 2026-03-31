# Boo

## What is this

Boo lets AI coding agents (Claude Code, Codex CLI) talk to each other across machines. You run agents in Ghostty terminal panes on your MacBook and Mac Mini — Boo connects them so they can send messages, discover each other, and collaborate without Telegram, Slack, or any cloud service in between.

Two components:

- **ghostty-socket** — A Ghostty fork that exposes a Unix socket API for controlling terminals (list them, send text, read screen). This is the local foundation.
- **ghostty-bridge** — A lightweight Node.js app that connects to Ghostty sockets across machines and gives each agent MCP tools to message other agents. This is the network layer.

No cloud dependency. No accounts. Works on any LAN or Tailscale network. Sub-millisecond local latency.


## Architecture

```
MacBook Pro                                Mac Mini
────────────────────                       ────────────────────
Ghostty (forked)                           Ghostty (forked)
  ├─ Agent A (pane)                          ├─ Agent C (pane)
  ├─ Agent B (pane)                          ├─ Agent D (pane)
  └─ /tmp/ghostty.sock                      └─ /tmp/ghostty.sock
       │                                          │
       │ local                     SSH tunnel     │
       └──────────┐          ┌────────────────────┘
                  ▼          ▼
             ┌────────────────────┐
             │   ghostty-bridge    │
             │                    │
             │  ┌──────────────┐  │
             │  │ Peer Registry │  │  Tracks all agents across machines
             │  └──────────────┘  │
             │  ┌──────────────┐  │
             │  │ Message Relay │  │  Routes messages to correct terminal
             │  └──────────────┘  │
             │  ┌──────────────┐  │
             │  │  MCP Server   │  │  Exposes tools to each agent (stdio)
             │  └──────────────┘  │
             └────────────────────┘
```

Each agent gets a bridge process (spawned by Claude Code via MCP). The bridge connects to all Ghostty sockets and handles message routing.

Remote sockets are accessed via SSH tunnel — the bridge manages tunnel lifecycle automatically.


## Features

### ghostty-socket (Ghostty fork)

- **Terminal listing** — Enumerate all open terminal panes with UUID, title, working directory, grid size, focus state
- **Text injection** — Send text to any terminal as if typed (same path as AppleScript `input text`)
- **Screen reading** — Read visible terminal content by row range
- **Key events** — Send keystrokes with modifier support (Ctrl+C, arrow keys, etc.)
- **Terminal info** — Query detailed metadata for a specific terminal
- **Cross-platform** — Works on macOS and Linux (Zig core, no platform-specific dependencies)
- **Disabled by default** — Opt-in via Ghostty config, socket has 0600 permissions

### ghostty-bridge (Network relay)

- **Cross-machine messaging** — Agents on different machines send messages to each other by name
- **Auto-discovery** — Bridge polls `list_terminals` to detect agents, peers self-register with identity
- **MCP native** — Each agent gets standard MCP tools (register, send, receive, broadcast, list peers)
- **SSH tunnel management** — Auto-establishes and reconnects tunnels for remote machines
- **Zero infrastructure** — No server, no database, no cloud. Just SSH keys you already have
- **Works with Claude Code and Codex CLI** — Configures both via their native MCP config files


## Technical Decisions

1. **Unix socket, not TCP, in Ghostty** — Ghostty is a terminal emulator, not a network daemon. The socket API stays local and uses filesystem permissions (0600) for security. Networking is the bridge's job. This split also makes the Ghostty changes upstream-able.

2. **JSON-RPC 2.0 over HTTP/1.1** — Simple request-response per connection (`Connection: close`). No WebSocket, no streaming, no keep-alive. Easy to test with curl. Same pattern Calyx uses.

3. **MCP over stdio for agents** — Each Claude Code session spawns its own bridge process. No shared daemon, no port conflicts. Claude Code and Codex both support MCP stdio natively.

4. **SSH tunnels for cross-machine** — Forward remote Unix sockets to local paths. SSH is already configured on every dev machine. Auto-reconnect on tunnel death. No need for mDNS, Bonjour, or custom discovery for a 2-3 machine setup.

5. **Message delivery via `send_text`** — Messages are injected into the receiving agent's terminal PTY. The agent sees it as input. Wrapped in `<boo-message>` tags so agents can distinguish from user input. This means zero changes needed in Claude Code or Codex — they just process the input.

6. **In-memory peer registry** — No persistence. Peers expire after 10 minutes, messages after 5 minutes. Max 100 messages per inbox. If the bridge restarts, agents re-register. Simplicity over durability — these are ephemeral coding sessions, not a message queue.

7. **Stable UUID surface IDs** — Added to Ghostty core (`Surface.zig`). GTK previously used raw pointers as IDs (explicitly marked "SUPER SUS" in their code). UUIDs are safe to expose over IPC and don't leak memory addresses.

8. **Bridge manages one config file** — Lists machines, socket paths, and SSH tunnel params. No auto-discovery beyond `list_terminals` polling. Explicit is better for a small, known fleet.


## Product Decisions

1. **Ghostty only** — Not terminal-agnostic. The socket API is a Ghostty fork. If you switch terminals, Boo doesn't work. This is intentional — deep integration with one terminal is better than shallow integration with many.

2. **No central server** — Every machine runs Ghostty with the socket. The bridge is a local process, not a hosted service. This means no auth tokens, no accounts, no vendor dependency. Trade-off: you need SSH access between machines.

3. **Agents don't need modification** — Claude Code and Codex work as-is. They get MCP tools via their standard config. No custom CLI, no SDK, no wrapper scripts. If a new AI coding tool supports MCP, it works with Boo automatically.

4. **Two machines first** — Designed for the MacBook Pro + Mac Mini use case. Works for 3-4 machines but not designed for 20. If you need large-scale agent orchestration, that's a different product.

5. **Replace claudecode-telegram for local work** — Current agent-to-agent communication goes through Telegram (500ms+ latency, cloud dependency, message corruption). Boo replaces this for machines on the same network with ~1ms latency and no external dependency. Telegram bridge remains for remote/mobile access.

6. **Fork, not upstream** — The Ghostty socket API is designed to be upstream-able (local, opt-in, secure). The bridge is ours. If upstream Ghostty ever ships a socket API, the bridge works with that too — the JSON-RPC contract is the interface.


## API Reference

### ghostty-socket (Unix socket, JSON-RPC 2.0)

| Method | Params | Returns |
|--------|--------|---------|
| `list_terminals` | — | `{terminals: [{id, title, working_directory, focused, pid, columns, rows}]}` |
| `get_terminal_info` | `{terminal_id}` | `{id, title, working_directory, focused, pid, columns, rows, foreground_process}` |
| `send_text` | `{terminal_id, text}` | `{ok: true}` |
| `send_key` | `{terminal_id, key, modifiers?}` | `{ok: true}` |
| `read_screen` | `{terminal_id, rows?: {start, end}}` | `{lines: [string]}` |

Config: `control-socket = /tmp/ghostty.sock` and `control-socket-permissions = 0600` in Ghostty config.

### ghostty-bridge (MCP tools)

| Tool | Params | Returns |
|------|--------|---------|
| `register_peer` | `{name, role?}` | `{peer_id}` |
| `list_peers` | — | `[{peer_id, name, role, machine, terminal_id, status, last_seen}]` |
| `send_message` | `{to, content}` | `{ok: true}` |
| `broadcast` | `{content}` | `{ok: true, delivered_to: [names]}` |
| `receive_messages` | `{peer_id}` | `[{id, from, content, timestamp}]` |
| `get_peer_status` | `{peer_id}` | `{name, machine, status, last_seen}` |

Config: `BOO_CONFIG=/path/to/config.json` env var pointing to machine list.


## Milestones

| # | What | Status |
|---|------|--------|
| M1 | Ghostty Unix socket — config, socket server, 5 JSON-RPC methods, stable UUIDs | Done |
| M2 | Bridge core — Ghostty client, peer registry, message relay, unit tests | In progress |
| M3 | MCP integration — stdio server, 6 MCP tools, integration tests, config auto-setup | Not started |
| M4 | Multi-machine — SSH tunnel management, cross-machine peer discovery and messaging | Not started |
