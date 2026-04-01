# Boo

AI coding agents talking to each other across machines — no cloud, no accounts, sub-millisecond latency.

## How it works

Boo connects to [Ghostty Boo](https://ghostty.org) (a Ghostty fork with a Unix socket control API) and exposes agent discovery and messaging as MCP tools. Claude Code and Codex CLI agents can register, find each other, and exchange messages across machines.

```
MacBook Pro                          Mac Mini
Ghostty Boo                         Ghostty Boo
  |- Agent A                          |- Agent C
  |- Agent B                          |- Agent D
  \- /tmp/ghostty.sock               \- /tmp/ghostty.sock
       |                                   |
       \---- SSH tunnel -------------------/
                    |
              +-------------+
              |   boo-app    |
              |  MCP Server  |
              +-------------+
```

## Quick start

1. Download `Boo.app` from [Releases](https://github.com/beastoin/claudecode-telegram/releases/tag/boo-v0.1.0) and drag to Applications
2. Launch — the onboarding wizard auto-detects Ghostty and configures MCP
3. Click "Enable MCP" — done

Boo registers itself as an MCP server in Claude Code. Your agents can now discover and message each other.

## MCP tools

| Tool | What it does |
|------|-------------|
| `register_peer` | Register an agent with a name and optional role |
| `list_peers` | See all agents across all machines |
| `send_message` | Send a message to another agent by name |
| `broadcast` | Send to all agents at once |
| `receive_messages` | Check your inbox |
| `get_peer_status` | Check if an agent is online |

## Components

| Component | Description |
|-----------|-------------|
| **boo-app** | Native macOS menu bar app — agent dashboard, MCP server, Ghostty installer, SSH tunnel manager |
| **ghostty-bridge** | Node.js MCP server — same 6 tools, for non-macOS or headless use |
| **ghostty** | Ghostty fork adding Unix socket API (`list_terminals`, `send_text`, `read_screen`, `send_key`, `get_terminal_info`) |

## Multi-machine setup

Boo uses SSH tunnels to forward Ghostty sockets between machines:

```bash
# Automatic via boo-app Settings > Machines tab
# Or manually:
ssh -L /tmp/ghostty-remote.sock:/tmp/ghostty.sock user@other-machine
```

Agents on different machines appear in the same peer list and can message each other directly.

## Ghostty Boo

Ghostty Boo is an unofficial fork of [Ghostty](https://ghostty.org) by Mitchell Hashimoto. It adds a `control-socket` config option that exposes a JSON-RPC 2.0 API over a Unix socket. All other Ghostty functionality is unchanged.

Boo is not affiliated with or endorsed by the Ghostty project. Licensed under MIT.
