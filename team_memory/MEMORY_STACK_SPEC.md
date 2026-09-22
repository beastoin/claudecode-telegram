# Memory Stack — CLI + /memory Command Spec
**Owner**: geni (spec/design) | **Implementer**: lee (bridge /memory integration)

## What This Is

4-layer memory system accessible via CLI (manager testing) and `/memory` subcommands (Telegram).
**No bridge startup integration** — this is a standalone tool and /memory enhancement only.

## CLI (already working)

Manager can run directly on VPS:

```bash
cd ~/claudecode-telegram

# Status — chunk counts, wing distribution
python3 -m team_memory.memory_stack status

# Wake-up text — L0 identity + L1 essential story (~900 tokens)
python3 -m team_memory.memory_stack wake-up
python3 -m team_memory.memory_stack wake-up --wing=omi

# On-demand recall — filtered by wing/room
python3 -m team_memory.memory_stack recall --wing=omi
python3 -m team_memory.memory_stack recall --wing=omi --room=prs

# Deep search — semantic search
python3 -m team_memory.memory_stack search who merged PR 6377
python3 -m team_memory.memory_stack search pricing change --wing=omi
```

## /memory Subcommands (lee implements in bridge.py)

Add these subcommands to the existing `cmd_memory()`:

### `/memory status`
Shows memory stack health.

```
/memory status
```
Response:
```
Memory Stack Status:
  Chunks: 5,430 (omi: 2,132, claudecode-telegram: 1,229, general: 1,388, infrastructure: 568, operations: 113)
  Messages: 35,846
  Summaries: 76
  L0 identity: 88 tokens (19 agents, 3 projects, 5 wings)
  L1 essential: last 7 days, top 15 items
```

Implementation:
```python
if query.strip().lower() == "status":
    from team_memory.memory_stack import MemoryStack
    stack = MemoryStack()
    info = stack.status()
    wings = info.get("wing_distribution", {})
    wing_str = ", ".join(f"{w}: {n}" for w, n in sorted(wings.items(), key=lambda x: -x[1]))
    lines = [
        "Memory Stack Status:",
        f"  Chunks: {info.get('total_chunks', 0)} ({wing_str})",
        f"  Messages: {info.get('total_messages', 0)}",
        f"  Summaries: {info.get('total_summaries', 0)}",
        f"  L0 identity: {info['L0_identity']['tokens']} tokens ({info['L0_identity']['agents']} agents, {info['L0_identity']['projects']} projects, {info['L0_identity']['wings']} wings)",
        f"  L1 essential: last 7 days, top 15 items",
    ]
    self.reply(chat_id, "\n".join(lines))
    return True
```

### `/memory wake-up [wing]`
Shows the L0+L1 wake-up text (what a worker would see on startup).

```
/memory wake-up          # all wings
/memory wake-up omi      # omi-focused
```

Implementation:
```python
if query.strip().lower().startswith("wake-up"):
    from team_memory.memory_stack import MemoryStack
    stack = MemoryStack()
    parts = query.strip().split()
    wing = parts[1] if len(parts) > 1 else None
    text = stack.wake_up(wing=wing)
    # Truncate for Telegram (4096 char limit)
    if len(text) > 4000:
        text = text[:3997] + "..."
    self.reply(chat_id, text)
    return True
```

### `/memory recall [--wing=X] [--room=Y]`
L2 on-demand retrieval.

```
/memory recall --wing=omi
/memory recall --wing=omi --room=prs
/memory recall --wing=infrastructure --room=oauth
```

Implementation:
```python
if query.strip().lower().startswith("recall"):
    from team_memory.memory_stack import MemoryStack
    stack = MemoryStack()
    # Parse --wing= and --room= flags
    wing = room = None
    for part in query.split():
        if part.startswith("--wing="):
            wing = part.split("=", 1)[1]
        elif part.startswith("--room="):
            room = part.split("=", 1)[1]
    text = stack.recall(wing=wing, room=room)
    if len(text) > 4000:
        text = text[:3997] + "..."
    self.reply(chat_id, text)
    return True
```

### Existing `/memory <query>` — unchanged
The existing search flow (`search_memory()`) stays as-is. It already uses wing/room detection via search.py.

## File Layout

```
~/claudecode-telegram/team_memory/
  memory_stack.py      # 4-layer stack module (geni built, CLI works)
  search.py            # existing search (powers /memory <query>)
  ingest.py            # existing ingest (wing/room classification)
  config.py            # paths, weights
  parse.py             # Telegram export parser
  MEMORY_STACK_SPEC.md # THIS FILE
  SPEC.md              # existing /memory command spec
```

## Testing

After lee's changes:
```
/memory status                    → shows chunk/message counts
/memory wake-up                   → shows L0+L1 text
/memory wake-up omi               → omi-filtered wake-up
/memory recall --wing=omi         → omi chunks
/memory recall --wing=omi --room=prs  → omi PR chunks
/memory who merged PR 6377        → existing search (unchanged)
```
