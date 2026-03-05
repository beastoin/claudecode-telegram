# Copy Review: Manager-Facing Text in bridge.py

Task: Implement all changes below. Consult with Codex before implementing. Use TDD — write tests first, then implement.

## PRIORITY 1: Watchdog status strings (_format_watchdog_status around line 3237)

These appear in /team rows and alerts. Internal state names are leaking to the manager.

| Current | Replacement |
|---------|-------------|
| `ready` | `Ready` |
| `working (tools)` | `Working` |
| `working (thinking)` | `Thinking` |
| `working (waiting)` | `Working` |
| `NEEDS INPUT {minutes}m` | `Needs reply ({minutes}m)` |
| `STUCK {minutes}m` | `No progress ({minutes}m)` |
| `POISONED {minutes}m` | `Error loop ({minutes}m)` |
| `DEAD` | `Not responding` |
| `offline` | `Offline` |
| `exited` | `Session ended` |
| `working (untracked)` | `Working` |
| `working` (pending fallback) | `Working` |
| `available` (fallback) | `Ready` |

## PRIORITY 2: Remove "Needs decision" prefix from displayed text

The `outcome="Needs decision"` param stays (internal routing). Just remove it from the TEXT the manager sees.

| Current displayed text | Replacement |
|----------------------|-------------|
| `Needs decision - No focused worker. Use /focus <name> first.` | `No focused worker. Use /focus <name> first.` |
| `Needs decision - Could not download GIF. Try again.` | `Could not download GIF. Try again.` |
| `Needs decision - Could not download image. Try again or send as file.` | `Could not download image. Try again or send as file.` |
| `Needs decision - Could not download file. Try again.` | `Could not download file. Try again.` |
| `Needs decision - Could not download {media_type}. Try again.` | `Could not download {media_type}. Try again.` |

## PRIORITY 3: /progress detail lines

| Current | Replacement |
|---------|-------------|
| `Doing: Working on a request` | `Doing: Working` |
| `worker app is not running. Use /restart.` | `Not running. Use /restart.` |
| `Continuity: on (claude thread abc12345...)` | `Continuity: on` |
| `Resume: available (session abc12345...)` | `Resume: available` |

## PRIORITY 4: /end command jargon

| Current | Replacement |
|---------|-------------|
| `Offboarding is permanent. Usage: /end <name>` | `This is permanent. Usage: /end <name>` |
| `Could not offboard "{name}". {err}` | `Could not remove "{name}". {err}` |

## PRIORITY 5: /teleport busy-state leak

| Current | Replacement |
|---------|-------------|
| `{name} is busy ({current_state}). Must be idle to teleport.` | `{name} is busy. Must be idle to teleport.` |
| `{name} is busy ({ws[0]}). Must be idle to teleback.` | `{name} is busy. Must be idle to teleback.` |

## PRIORITY 6: /restart detail noise

| Current | Replacement |
|---------|-------------|
| `Resume is automatic for {backend}. Thread {id}... is active — next message continues it.` | `{name} is still active. Next message continues where you left off.` |
| `No thread found for {name}. Next message starts a new {backend} thread.` | `No active session for {name}. Next message starts fresh.` |
| `No resume info found. Restarting {name} fresh...` | `Restarting {name} fresh...` |

## PRIORITY 7: CWD restart notification

| Current | Replacement |
|---------|-------------|
| `{name} is switching to {path} and restarting now. Messages sent during this restart can be lost.` | `{name} is restarting in a new directory. Messages during restart may be lost.` |
| `{name} could not restart in {path}. Please run /restart {name} before sending new messages.` | `{name} could not restart. Run /restart {name} before sending new messages.` |
| `{name} restarted in {path} but is not ready yet. It may be starting up.` | `{name} restarted but is not ready yet.` |

## Strings that are fine (no changes needed)
- All /hire messages
- All /focus messages
- /team empty state + header (already improved)
- /pause messages
- /restart basic flow (Bringing X back online, X is back and ready, errors)
- /restart all sequence ([1/5] format)
- Auto-focus messages
- Startup/shutdown messages
- Routing messages (No one assigned, is offline, still working)
- Teleport status updates (Stopping, Syncing, Installing hooks)
- Watchdog alerts (already improved)
- Resolved alert (already improved)

## Total: 23 string changes across 7 priorities
