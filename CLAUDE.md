# Claude Code Project Instructions

## Version Management

When making changes that result in a new version:

1. **Update version** in `claudecode-telegram.sh`:
   ```bash
   VERSION="x.y.z"
   ```

2. **Update `DOC.md`** with:
   - New version number in header
   - Changelog entry describing:
     - Breaking changes (table format if applicable)
     - New features
     - Architecture changes
   - Update design philosophy sections if core principles changed

3. **Run acceptance tests** before committing:
   ```bash
   TELEGRAM_BOT_TOKEN='...' ./test.sh
   ```
   See `TEST.md` for full testing documentation.

## When to Bump Version

- **Patch (0.0.x)**: Bug fixes, minor tweaks
- **Minor (0.x.0)**: New features, backward-compatible changes
- **Major (x.0.0)**: Breaking changes, architecture overhaul

## Key Files

| File | Purpose |
|------|---------|
| `bridge.py` | Telegram webhook handler, session management |
| `claudecode-telegram.sh` | CLI wrapper, tunnel/webhook setup |
| `hooks/send-to-telegram.sh` | Claude Stop hook, sends responses |
| `test.sh` | Automated acceptance tests |
| `DOC.md` | Design philosophy, changelog |
| `TEST.md` | Testing documentation |

## Testing Requirements

**All changes must pass tests before committing:**

```bash
TELEGRAM_BOT_TOKEN='your-test-token' ./test.sh
```

Tests cover:
- Unit: imports, version command
- Integration: all commands, admin security, session files
- Tunnel: webhook setup (optional, skip with `SKIP_TUNNEL=1`)

See `TEST.md` for detailed test documentation.

## Design Philosophy

**Source of truth: `DOC.md`** - All principles are documented there with full context.

When making changes, ensure they align with the philosophy in `DOC.md`. If adding new principles, update `DOC.md` first (both the summary table at the top AND the detailed section), then reference here.

### Quick Reference (see DOC.md for details)

| Principle | Rule |
|-----------|------|
| tmux IS persistence | No database, no state.json |
| `claude-<name>` naming | Enables auto-discovery |
| RAM state only | Rebuilt on startup from tmux |
| Per-session files | Minimal hook↔gateway coordination |
| Fail loudly | No silent errors, no hidden retries |
| Token isolation | `TELEGRAM_BOT_TOKEN` NEVER leaves bridge |
| Admin auto-learn | First user becomes admin (RAM only) |
| Secure by default | 0o700 dirs, 0o600 files |
