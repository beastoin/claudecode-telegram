# Claude Code Project Instructions

## Version Management

When making changes that result in a new version:

1. **Update version** in `claudecode-telegram.sh`:
   ```bash
   VERSION="x.y.z"
   ```

2. **Update `doc.md`** with:
   - New version number in header
   - Changelog entry describing:
     - Breaking changes (table format if applicable)
     - New features
     - Architecture changes
   - Update design philosophy sections if core principles changed

3. **Run acceptance tests** before committing:
   ```bash
   python3 -c "import bridge; ..."  # See test script
   ./claudecode-telegram.sh --version
   ```

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
| `doc.md` | Design philosophy, changelog |

## Architecture Principles (preserve these)

1. **tmux IS persistence** - No database, no state.json
2. **`claude-<name>` naming** - Enables auto-discovery
3. **RAM state only** - Rebuilt on startup from tmux
4. **Per-session files** - Minimal hook↔gateway coordination
5. **Fail loudly** - No silent errors, no hidden retries

## Security Principles (v0.3.0+)

1. **Token isolation** - `TELEGRAM_BOT_TOKEN` NEVER leaves bridge process
   - Claude sessions don't have token in environment
   - Hook forwards to bridge via localhost HTTP, not Telegram API
2. **Admin auto-learn** - First user to message becomes admin (RAM only)
3. **Silent rejection** - Non-admin users get no response
4. **Secure permissions** - Directories 0o700, files 0o600
5. **Optional webhook verification** - `TELEGRAM_WEBHOOK_SECRET` for extra security
