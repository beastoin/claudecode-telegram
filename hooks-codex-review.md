# Codex Review: Hooks vs tmux Pane Parsing

## (1) Priority Order for Maximum Stability Gain

Rank by fragility-eliminated / effort ratio:

**Tier 1 — Do first (highest ROI):**
1. `PostToolUseFailure` → replace POISONED regex detection
   - Currently: regex 50 pane lines for error patterns 3+ times. Breaks when Claude Code changes error formatting.
   - Hook gives: exact tool name, exact error, structured data. Count failures in RAM, threshold at 3 = POISONED.
   - Effort: Small. One hook, one counter, one bridge endpoint.

2. `Notification(elicitation_dialog|permission_prompt)` → replace WAITING_INPUT pane parsing
   - Currently: 13 footer strings + 11 content patterns hardcoded to TUI rendering. This is the MOST fragile code.
   - Hook gives: exact notification type. No string matching needed.
   - Effort: Small-medium. Need to verify all interactive prompt types fire as Notification events.

**Tier 2 — Do second (good ROI, more surface area):**
3. `PreToolUse`/`PostToolUse`/`Stop` → replace `_extract_activity()` spinner parsing
   - Currently: Unicode spinner char detection, regex for tool names from TUI output. Breaks on every CC update.
   - Hooks give: exact state transitions with tool names. "Running Bash", "Running Edit", "Thinking".
   - Effort: Medium. Three hooks, need to handle rapid fire events, need display state machine.

**Tier 3 — Do later (nice to have):**
4. `SessionStart`/`SessionEnd` → supplement OFFLINE/DEAD detection
   - Current PID-based detection works fine. Hooks add clarity on WHY a session ended (exit_reason) but don't replace the need for PID monitoring (crashed processes can't fire hooks).
   - Effort: Small but low urgency.

5. `SubagentStart`/`SubagentStop` → track agent delegation
   - Useful for `/progress` display ("Running 2 subagents") but not a stability concern.

## (2) HTTP Hooks vs Command Hooks

**Command hooks. Definitively.**

Reasons:
- You already use `$BRIDGE_URL` and `$WORKER_NAME` env vars throughout the hook chain. HTTP hooks can't expand env vars — the URL is static in config.
- Your bridge runs on dynamic ports per node (8271 prod, 8272 dev, 8295 test). Command hooks resolve `$BRIDGE_URL` at runtime. HTTP hooks would need the port baked into config at template time — one more sed substitution to maintain.
- Command hooks let you write to signal files as a side effect. You can both POST to bridge AND write `hooks/last_tool` in one command. HTTP hooks are fire-and-forget to one URL.
- Error handling: command hooks can check curl exit code and fall back (write to file on network failure). HTTP hooks just fail silently.
- You already have the pattern: `send-to-telegram.sh` is a command hook. Extending the pattern is lower risk than introducing a new hook type.

**Template pattern:**
```bash
#!/bin/bash
# hooks/pre-tool-use.sh (templated at session creation)
curl -sf -X POST "$BRIDGE_URL/hook" \
  -H "Content-Type: application/json" \
  -d "{\"event\":\"PreToolUse\",\"worker\":\"$WORKER_NAME\",\"tool\":\"$1\"}" \
  --max-time 1 &
# Write signal file as fallback
echo "$(date +%s) $1" > "$HOOK_STATE_DIR/last_pre_tool"
```

The `&` (background) + `--max-time 1` pattern prevents hooks from blocking Claude Code even if bridge is temporarily unreachable.

## (3) Graceful Degradation: Keep tmux Fallback

**Yes, keep tmux parsing as fallback. Hybrid for at least 2 weeks.**

Reasoning:
- Hooks are only as reliable as the process running them. If Claude Code crashes mid-tool-call, `PostToolUse` never fires. Your PID-based DEAD detection catches this — pane parsing isn't needed here.
- But if hooks silently stop working (e.g., settings.json gets overwritten during CC auto-update, or a curl timeout causes the hook to exit non-zero and get disabled), you'd lose state visibility entirely.
- The cost of keeping pane parsing is low — it's already written and working. The cost of NOT having it when hooks fail is high — blind spot on worker state.

**Degradation protocol:**
1. Primary: hook-derived state (from bridge `/hook` endpoint or signal files)
2. If no hook event received in 60s AND process is running → fall back to pane scan
3. Log the fallback so you can measure hook reliability over time
4. After 2 weeks with <1% fallback rate → remove pane parsing for that state

**What to remove immediately (no fallback needed):**
- Spinner verb parsing in `_extract_activity()` — this is display-only, not watchdog-critical. If hooks give you tool names, the spinner parsing adds zero value. Safe to remove once PreToolUse/PostToolUse hooks work.

**What to keep as fallback longer:**
- WAITING_INPUT pane detection — until you've verified ALL interactive prompt types fire Notification hooks
- POISONED regex — until you've validated PostToolUseFailure fires reliably for ALL failure modes (not just tool failures — what about network errors, permission denials, etc.?)

## (4) Risks and Gotchas You Missed

### A. Hook exit code semantics are dangerous
Claude Code hooks have special exit code meanings:
- Exit 0 = proceed, stdout injected into context
- Exit 2 = BLOCK the action, stderr shown as feedback
- Any other = proceed, logged only

Your hook scripts MUST exit 0 or non-zero-non-2. If a hook accidentally exits 2 (e.g., a curl command that happens to return exit code 2), it will BLOCK the tool call. This could break workers.

**Mitigation:** Every hook script should end with `exit 0` explicitly. Never let curl's exit code propagate.

### B. PreToolUse hooks add latency to EVERY tool call
PreToolUse fires BEFORE the tool runs. If your hook takes 500ms (network timeout), that's 500ms added to every Bash, Edit, Read, Write call. For a worker making 50 tool calls/minute, that's 25 seconds of added latency per minute.

**Mitigation:** Background the curl (`&`) and don't wait. Or use signal files only (no network call on the hot path). Reserve HTTP POSTs for less frequent events (PostToolUse, Stop, Notification).

### C. Hook stdout injection is a footgun
If a command hook prints to stdout, that text gets injected into Claude's context. A hook that accidentally echoes debug output could confuse the model.

**Mitigation:** Redirect all output to /dev/null or a log file. `curl -sf ... > /dev/null 2>&1 &`

### D. Settings.json scope and override chain
Claude Code hooks can be set at 3 levels:
1. `~/.claude/settings.json` (user-level)
2. `.claude/settings.json` (project-level)
3. `.claude/settings.local.json` (local overrides)

If a worker `cd`s to a different project directory (teleport, CWD change), it may pick up different project-level hooks or lose them entirely.

**Mitigation:** Put state-reporting hooks at USER level (`~/.claude/settings.json`), not project level. Use project-level only for project-specific hooks like `send-to-telegram.sh`.

### E. Concurrent hook execution
If Claude makes 3 rapid tool calls, 3 PreToolUse hooks fire concurrently. Your bridge `/hook` endpoint must handle concurrent POSTs from the same worker without race conditions.

**Mitigation:** Use per-worker locks in the endpoint handler (you already do this for tmux sends — same pattern).

### F. The Notification hook payload is underdocumented
The analysis lists `elicitation_dialog`, `permission_prompt`, `idle_prompt`, `auth_success` as notification types. But:
- Does `ExitPlanMode` fire as `elicitation_dialog`? (Probably not — it's a tool call, not a notification)
- Does `AskUserQuestion` fire as both `elicitation_dialog` AND appear as a tool call? (Possible double-fire)
- What about the new `TeammateIdle` — is that a Notification subtype or a separate hook?

**Mitigation:** Write a test hook that logs ALL events to a file. Run a worker through every interactive scenario. Map the actual events before building state logic on assumptions.

## (5) Ideal Hook Configuration

```json
{
  "hooks": {
    "PostToolUseFailure": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/on-tool-failure.sh"
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "elicitation_dialog|permission_prompt",
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/on-notification.sh"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/on-pre-tool.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/on-post-tool.sh"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/send-to-telegram.sh"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/on-session-start.sh"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/claude/.claude/telegram/hooks/on-session-end.sh"
          }
        ]
      }
    ]
  }
}
```

**Each hook script pattern:**
```bash
#!/bin/bash
# on-tool-failure.sh
# Reads hook payload from stdin, posts to bridge, writes signal file
set -euo pipefail
PAYLOAD=$(cat)
TOOL=$(echo "$PAYLOAD" | jq -r '.tool_name // "unknown"')

# Signal file (always works, even if bridge is down)
HOOK_DIR="${HOOK_STATE_DIR:-/tmp/claudecode-telegram/$NODE_NAME/$WORKER_NAME/hooks}"
mkdir -p "$HOOK_DIR"
echo "$(date +%s) $TOOL" >> "$HOOK_DIR/failures"

# HTTP to bridge (best-effort, backgrounded)
curl -sf -X POST "$BRIDGE_URL/hook" \
  -H "Content-Type: application/json" \
  -d "{\"event\":\"PostToolUseFailure\",\"worker\":\"$WORKER_NAME\",\"tool\":\"$TOOL\"}" \
  --max-time 1 > /dev/null 2>&1 &

exit 0  # CRITICAL: always exit 0 to not block Claude
```

**Dual-write pattern (signal file + HTTP)** gives you:
- Signal files: watchdog reads these on its 4s cycle, always available, no network dependency
- HTTP POST: real-time state update for display layer, enables instant /team refresh

## Summary Recommendation

**Phase 1 (this week):** PostToolUseFailure hook → POISONED detection. Smallest scope, highest reliability gain, easiest to validate.

**Phase 2 (next week):** Notification hook → WAITING_INPUT detection. Run the event-mapping test first to verify all interactive prompts fire correctly.

**Phase 3 (week after):** PreToolUse/PostToolUse/Stop → replace `_extract_activity()` display layer. This is the biggest change but also the biggest stability win.

**Keep throughout:** PID monitoring, `pending` file, tmux session checks. These are process-level signals that hooks can never fully replace.

**Kill after Phase 3 validates:** All pane text parsing in `_extract_activity()` and `_extract_question_details()`. This removes ~200 lines of the most fragile code in bridge.py.
