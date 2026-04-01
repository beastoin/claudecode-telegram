# Boo — CLAUDE.md

## What is Boo?

macOS menu bar app for cross-machine AI agent IPC via Ghostty terminals. Agents register, discover, and message each other through MCP tools over Unix sockets.

## Architecture

| Component | Stack | Location |
|-----------|-------|----------|
| **boo-app** | Swift 6 / SwiftPM / macOS 14+ | `boo-app/` |
| **ghostty-bridge** | Node.js MCP relay | `ghostty-bridge/` |
| **ghostty** (fork) | Zig 0.15.2 + Xcode | `ghostty/` (gitignored, separate .git) |

boo-app has two targets:
- **BooCore** — library: GhosttyClient, PeerRegistry, MessageRelay, MCPServer, SSHTunnelManager
- **BooApp** — executable: AppDelegate, StatusItemController, PopoverView, SettingsView, OnboardingView

No external Swift dependencies. Pure SwiftPM, no Xcode project.

## Development

### Build (Mac Mini)

```bash
cd boo/boo-app
SWIFT_BUILD_DIR=/tmp/swift-build-$(whoami) swift build          # debug
SWIFT_BUILD_DIR=/tmp/swift-build-$(whoami) swift build -c release  # release
```

`SWIFT_BUILD_DIR` avoids `.build/build.db` lock conflicts between users.

### Run tests

```bash
SWIFT_BUILD_DIR=/tmp/swift-build-$(whoami) swift test
```

### Source location on Mac Mini

The boo-app source is at `/Users/beastoinagents/rnd/boo-app/` on Mac Mini (not inside claudecode-telegram). When updating, sync files from VPS:
```bash
scp ~/claudecode-telegram/boo/boo-app/Sources/BooApp/SomeFile.swift beastoin-agents-f1-mac-mini:/Users/beastoinagents/rnd/boo-app/Sources/BooApp/
```

## Release Process

### 1. Build release binary (Mac Mini)

```bash
ssh beastoin-agents-f1-mac-mini
cd /Users/beastoinagents/rnd/boo-app
SWIFT_BUILD_DIR=/tmp/swift-build-beastoinagents swift build -c release
```

### 2. Create app bundle

The app bundle at `/tmp/Boo.app` has this structure:
```
Boo.app/
  Contents/
    MacOS/BooApp          ← release binary
    Resources/AppIcon.icns ← icon from BooIcons.swift base64 → PIL → iconutil
    Info.plist             ← must have CFBundleExecutable, CFBundlePackageType=APPL
```

Copy release binary into bundle:
```bash
cp /tmp/swift-build-beastoinagents/release/BooApp /tmp/Boo.app/Contents/MacOS/BooApp
cp Sources/BooApp/Info.plist /tmp/Boo.app/Contents/Info.plist
```

### 3. Code sign

```bash
source ~/.config/boo/secrets.env
codesign --deep --force --options runtime --timestamp --sign "$BOO_SIGNING_IDENTITY" /tmp/Boo.app
```

The signing identity is in Mac Mini's System keychain (imported via .p12). Unlock keychain first if needed:
```bash
security unlock-keychain -p "$BOO_KEYCHAIN_PW" ~/Library/Keychains/login.keychain-db
```

Secrets are in `~/.config/boo/secrets.env` (0600) on both VPS and Mac Mini.

### 4. Notarize

```bash
source ~/.config/boo/secrets.env

# Zip first
ditto -c -k --keepParent /tmp/Boo.app /tmp/Boo-macOS-arm64.zip

# Submit
xcrun notarytool submit /tmp/Boo-macOS-arm64.zip \
  --key "$BOO_NOTARY_KEY" \
  --key-id "$BOO_NOTARY_KEY_ID" \
  --issuer "$BOO_NOTARY_ISSUER" \
  --wait

# Staple
xcrun stapler staple /tmp/Boo.app

# Re-zip with stapled ticket
rm /tmp/Boo-macOS-arm64.zip
ditto -c -k --keepParent /tmp/Boo.app /tmp/Boo-macOS-arm64.zip
```

### 5. Upload to GitHub release

```bash
# From VPS
source ~/.config/claudecode-telegram/agent.env
GH_TOKEN="$GH_TOKEN_BEASTOIN" gh release upload boo-v0.1.0 /tmp/release-assets/Boo-macOS-arm64.zip --clobber --repo beastoin/claudecode-telegram
```

Token: `GH_TOKEN_BEASTOIN` (NOT `GH_TOKEN` which is for omi).

## Ghostty Boo Release Process

### Build Zig library (ReleaseFast)

```bash
cd /Users/beastoinagents/rnd/ghostty-boo
zig build --release=fast -Dapp-runtime=none
```

### Build Xcode app (Release)

```bash
xcodebuild -project macos/Ghostty.xcodeproj \
  -scheme Ghostty -configuration Release \
  -derivedDataPath /tmp/ghostty-boo-build \
  SYMROOT=/tmp/ghostty-boo-build \
  CODE_SIGN_IDENTITY="-" CODE_SIGNING_REQUIRED=NO build
```

### Sign Ghostty Boo

Sparkle framework needs inside-out signing + symlink fix:

```bash
source ~/.config/boo/secrets.env
IDENTITY="$BOO_SIGNING_IDENTITY"
APP="/tmp/Ghostty Boo.app"
SPARKLE="$APP/Contents/Frameworks/Sparkle.framework"

# Fix Sparkle symlinks (Xcode copies as dirs, not symlinks)
rm -rf "$SPARKLE/Versions/Current"
ln -sf B "$SPARKLE/Versions/Current"
for item in Autoupdate Resources Sparkle Updater.app XPCServices; do
    [ -e "$SPARKLE/$item" ] && [ ! -L "$SPARKLE/$item" ] && rm -rf "$SPARKLE/$item" && ln -sf "Versions/Current/$item" "$SPARKLE/$item"
done

# Sign inside-out
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/XPCServices/Downloader.xpc"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/XPCServices/Installer.xpc"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/Updater.app"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/Autoupdate"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP/Contents/PlugIns/DockTilePlugin.plugin"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
```

Then notarize + staple same as Boo.

## Gotchas

### Info.plist must have CFBundleExecutable + CFBundlePackageType

Without these, macOS shows "damaged or incomplete" even if signed and notarized.

```xml
<key>CFBundleExecutable</key>
<string>BooApp</string>
<key>CFBundlePackageType</key>
<string>APPL</string>
```

### macOS icon cache is extremely persistent

macOS caches app icons by **bundle ID + icon filename**. If you update an icon:
- Rename the .icns file (e.g., `Ghostty.icns` → `GhosttyBoo.icns`)
- Update `CFBundleIconFile` and `CFBundleIconName` in Info.plist
- Use a distinct `CFBundleIdentifier` (e.g., `com.beastoin.ghostty-boo`)

Users must clear cache: `sudo rm -rf /Library/Caches/com.apple.iconservices.store && killall Dock`

### Sparkle framework symlinks break during Xcode copy

Xcode copies `Versions/Current` as a directory instead of a symlink to `B`. This causes `codesign` to fail with "bundle format is ambiguous". Fix by recreating symlinks before signing (see above).

### Ghostty Zig build mode

`zig build` defaults to Debug. Always use `--release=fast` for distribution. Verify with:
```bash
/path/to/ghostty --version  # Check "build mode: .ReleaseFast"
```

Debug builds show a warning banner and are ~2x larger.

### MCP binary path

`ProcessInfo.processInfo.arguments[0]` returns the debug build path during development. Use `Bundle.main.executablePath` instead — it resolves to `/Applications/Boo.app/Contents/MacOS/BooApp` when running from the installed app.

### Apple API key limitations

The App Store Connect API key (`BOO_NOTARY_KEY_ID` in secrets.env) can authenticate and notarize, but **cannot create Developer ID certificates** — that requires Account Holder role. Create certs manually at developer.apple.com.

### GitHub token

Use `GH_TOKEN_BEASTOIN` from `~/.config/claudecode-telegram/agent.env` for the beastoin/claudecode-telegram repo. `GH_TOKEN` is for omi.

## Bundle Identifiers

| App | Bundle ID |
|-----|-----------|
| Boo | `com.beastoin.boo` |
| Ghostty Boo | `com.beastoin.ghostty-boo` |

## boo CLI

Go CLI at `boo-cli/` that automates the full build/sign/notarize/release pipeline. Runs on VPS, SSHs to Mac Mini for all macOS operations.

### Build & install

```bash
cd boo/boo-cli
go build -o boo .
cp boo ~/bin/   # optional: add to PATH
```

### Commands

```
boo dev build [--release] [boo|ghostty-boo|all]   Build on Mac Mini
boo dev test                                       Run Swift tests
boo dev sync <path>                                Sync file VPS → Mac Mini
boo sign [boo|ghostty-boo|all]                     Code sign
boo notarize [boo|ghostty-boo|all]                 Notarize + staple
boo verify [boo|ghostty-boo|all]                   Verify signing
boo release [--tag TAG]                            Upload to GitHub release
boo ship [--tag TAG]                               Full pipeline
boo schema [command]                               Print command schema (JSON)
```

### Agent-friendly (per https://justin.poehnelt.com/posts/rewrite-your-cli-for-ai-agents/)

- `--json` flag: all commands emit structured JSON to stdout (errors too)
- `boo schema`: runtime introspection — full command tree, args, flags, defaults, idempotency/mutating annotations
- `--dry-run`: preview without executing
- Human diagnostics to stderr, machine data to stdout
- `NO_COLOR` / `--no-color` respected
- Exit codes: 0=success, 1=failure, 2=invalid usage

### Config via env

| Var | Default | Purpose |
|-----|---------|---------|
| `BOO_MAC_HOST` | `beastoin-agents-f1-mac-mini` | SSH host |
| `BOO_RELEASE_TAG` | `boo-v0.1.0` | Default release tag |
| `NO_COLOR` | — | Disable colors |

### Typical workflows

```bash
# Full release
boo ship --tag boo-v0.2.0

# Dev iteration
boo dev build boo               # debug build
boo dev sync Sources/BooApp/SettingsView.swift
boo dev test

# Just re-sign and verify
boo sign boo && boo verify boo
```

## Key Files

| File | Purpose |
|------|---------|
| `boo-app/Sources/BooApp/Info.plist` | App bundle metadata |
| `boo-app/Sources/BooApp/BooIcons.swift` | Base64-embedded PNG icons |
| `boo-app/Sources/BooCore/MCPServer.swift` | MCP JSON-RPC server (6 tools) |
| `boo-app/Sources/BooCore/MCPConfigManager.swift` | Registers MCP in ~/.claude/settings.json |
| `boo-app/Sources/BooApp/SettingsView.swift` | Settings + Ghostty Boo installer |
| `boo-cli/main.go` | boo CLI (build/sign/release automation) |
| `ghostty/FORK.md` | Documents fork changes from upstream |
| `SPEC.md` | Architecture and API specification |
