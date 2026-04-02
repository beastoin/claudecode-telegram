# Boo

## What is this

Boo lets AI coding agents talk to each other across machines — no cloud, no accounts, sub-millisecond latency.

Three components:

- **ghostty-socket** — Ghostty fork with Unix socket API for terminal control
- **ghostty-bridge** — Node.js relay connecting Ghostty instances across machines via MCP
- **boo-app** — Native macOS menu bar app for agent discovery, messaging dashboard, and Ghostty socket installer
- **boo-cli** — Go CLI (`boo`) for build/sign/notarize/release automation. Runs on VPS, SSHs to Mac Mini for macOS operations. Commands: `dev build`, `dev test`, `dev sync`, `sign`, `notarize`, `verify`, `release`, `ship`

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
             │   Peer Registry     │
             │   Message Relay     │
             │   MCP Server        │
             └────────────────────┘
                      ▲
                      │ same logic, native UI
             ┌────────────────────┐
             │   boo-app (macOS)   │
             │   Menu bar agent    │
             │   dashboard +       │
             │   installer         │
             └────────────────────┘
```

Each agent gets its own bridge process (spawned via MCP stdio). Bridge connects to all Ghostty sockets and routes messages.

boo-app wraps the same bridge logic in a native macOS menu bar app with visual agent dashboard and one-click Ghostty socket installer.

## Features

**ghostty-socket**: list terminals, send text, read screen, send keys, get terminal info. Cross-platform (macOS + Linux). Disabled by default, 0600 permissions.

**ghostty-bridge**: cross-machine messaging by agent name, auto-discovery via `list_terminals`, SSH tunnel management with auto-reconnect, works with Claude Code and Codex CLI via native MCP config.

**boo-app**:
- Menu bar agent dashboard — see all agents across all machines, their status, message counts
- Auto-discovery — detects running Ghostty processes via NSWorkspace, finds socket paths automatically
- One-click Ghostty socket install — downloads fork, configures `control-socket` in Ghostty config
- Native MCP server — same stdio interface as Node.js bridge, but runs inside the app
- SSH tunnel management — persistent background tunnels with auto-reconnect
- macOS notifications — alerts when an agent sends you a message
- WidgetKit — macOS widget showing agent status at a glance

## Decisions

| # | Decision | Why |
|---|----------|-----|
| 1 | Unix socket in Ghostty, not TCP | Terminal emulators shouldn't be network daemons. Networking is the bridge's job. Keeps fork upstream-able. |
| 2 | JSON-RPC 2.0 over HTTP/1.1 | Simple, testable with curl, no WebSocket complexity |
| 3 | MCP stdio per agent | No shared daemon, no port conflicts |
| 4 | SSH tunnels for cross-machine | Already configured on every dev machine, auto-reconnect |
| 5 | Message delivery via `send_text` | Zero changes needed in Claude Code or Codex — agents process injected input naturally |
| 6 | In-memory peer registry | No persistence needed for ephemeral coding sessions. 10min peer TTL, 5min message TTL. |
| 7 | Ghostty only, not terminal-agnostic | Deep integration > shallow compatibility |
| 8 | Two machines first | MacBook Pro + Mac Mini. Works for 3-4, not designed for 20. |
| 9 | Replaces claudecode-telegram locally | ~1ms vs ~500ms, no cloud dependency. Telegram remains for remote/mobile. |
| 10 | Fork now, upstream later | Socket API designed to be upstream-able. Bridge is ours. |
| 11 | Swift native app follows CodexBar pattern | SwiftUI App + NSApplicationDelegateAdaptor, StatusItemController, @Observable. Proven menu bar app architecture (CodexBar: 9.6K stars). |
| 12 | Node.js bridge first, Swift native second | Bridge proves the protocol and ships fast. Swift app is the polished product layer on top. Same Ghostty socket API, same MCP tools. |
| 13 | boo-app includes installer | Discovering and installing the Ghostty fork + configuring control-socket should be one click, not manual setup. |
| 14 | boo-app follows Apple Human Interface Guidelines | Native macOS look and feel — use Apple design system ([macOS 26 Figma kit](https://www.figma.com/community/file/1543337041090580818/macos-26)) for controls, spacing, typography, menu bar popover sizing, and dark mode colors. Must feel like a system app, not a web app in a window. |
| 15 | Three-click onboarding | Download → launch → click "Enable MCP" → done. First-run wizard auto-detects Ghostty, offers fork install if missing, one-click MCP registration via MCPConfigManager. Brew formula + .app bundle for distribution. No terminal, no git clone, no manual config. |

## API

**ghostty-socket** (Unix socket, JSON-RPC 2.0):

| Method | Params | Returns |
|--------|--------|---------|
| `list_terminals` | — | `[{id, title, working_directory, focused, columns, rows}]` |
| `get_terminal_info` | `{terminal_id}` | `{id, title, working_directory, focused, columns, rows}` |
| `send_text` | `{terminal_id, text}` | `{ok: true}` |
| `send_key` | `{terminal_id, key, modifiers?}` | `{ok: true}` |
| `read_screen` | `{terminal_id, rows?: {start, end}}` | `{lines: [string]}` |

**ghostty-bridge / boo-app** (MCP tools):

| Tool | Params | Returns |
|------|--------|---------|
| `register_peer` | `{name, role?}` | `{peer_id}` |
| `list_peers` | — | `[{peer_id, name, role, machine, status}]` |
| `send_message` | `{to, content}` | `{ok: true}` |
| `broadcast` | `{content}` | `{ok: true, delivered_to: [names]}` |
| `receive_messages` | `{peer_id}` | `[{id, from, content, timestamp}]` |
| `get_peer_status` | `{peer_id}` | `{name, machine, status, last_seen}` |

## Milestones

| # | What | Status |
|---|------|--------|
| M1 | Ghostty Unix socket — 5 JSON-RPC methods, stable UUIDs | Done |
| M2 | Bridge core — Ghostty client, peer registry, message relay | Done |
| M3 | MCP integration — stdio server, 6 tools, config auto-setup | Done |
| M4 | Multi-machine — SSH tunnels, cross-machine messaging | Done |
| M5 | boo-app — Swift menu bar app, agent dashboard, Ghostty installer | Done |
| M6 | boo-app MCP — native MCP server replacing Node.js bridge on macOS | Done |
