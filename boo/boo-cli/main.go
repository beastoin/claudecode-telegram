package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const version = "0.3.0"

// ── Configuration (overridable via env) ──

func macHost() string {
	if v := os.Getenv("BOO_MAC_HOST"); v != "" {
		return v
	}
	return "beastoin-agents-f1-mac-mini"
}

func defaultTag() string {
	if v := os.Getenv("BOO_RELEASE_TAG"); v != "" {
		return v
	}
	return "boo-v0.1.0"
}

const (
	booSourceMac     = "/Users/beastoinagents/rnd/boo-app"
	ghosttySourceMac = "/Users/beastoinagents/rnd/ghostty-boo"
	swiftBuildDir    = "/tmp/swift-build-beastoinagents"
	bundleIDGhostty  = "com.beastoin.ghostty-boo"
	appPathBoo       = "/tmp/Boo.app"
	appPathGhostty   = "/tmp/Ghostty Boo.app"
	githubRepo       = "beastoin/claudecode-telegram"
	agentEnvPath     = ".config/claudecode-telegram/agent.env"
	// Secrets env file on Mac Mini and VPS
	secretsEnvPath   = ".config/boo/secrets.env"
)

// Secrets loaded from ~/.config/boo/secrets.env or env vars
func signingIdentity() string { return envOrSecret("BOO_SIGNING_IDENTITY") }
func notaryKey() string       { return envOrSecret("BOO_NOTARY_KEY") }
func notaryKeyID() string     { return envOrSecret("BOO_NOTARY_KEY_ID") }
func notaryIssuer() string    { return envOrSecret("BOO_NOTARY_ISSUER") }
func keychainPW() string      { return envOrSecret("BOO_KEYCHAIN_PW") }

var secretsCache map[string]string

func envOrSecret(key string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	if secretsCache == nil {
		secretsCache = make(map[string]string)
		home, _ := os.UserHomeDir()
		data, err := os.ReadFile(filepath.Join(home, secretsEnvPath))
		if err == nil {
			for _, line := range strings.Split(string(data), "\n") {
				line = strings.TrimSpace(line)
				if line == "" || strings.HasPrefix(line, "#") {
					continue
				}
				if k, v, ok := strings.Cut(line, "="); ok {
					secretsCache[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), "\"'")
				}
			}
		}
	}
	return secretsCache[key]
}

// ── Global state ──

var (
	noColor  bool
	dryRun   bool
	jsonOut  bool
)

// ── JSON output (agent-friendly) ──

type Result struct {
	Command   string `json:"command"`
	Status    string `json:"status"` // "ok" or "error"
	Message   string `json:"message,omitempty"`
	Error     string `json:"error,omitempty"`
	ExitCode  int    `json:"exit_code"`
	DurationS float64 `json:"duration_s,omitempty"`
	Data      any    `json:"data,omitempty"`
}

func emitJSON(r Result) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	enc.Encode(r)
}

// ── Human output (stderr) ──

func colorStr(c, msg string) string {
	if noColor || os.Getenv("NO_COLOR") != "" || os.Getenv("TERM") == "dumb" {
		return msg
	}
	return c + msg + "\033[0m"
}

func step(n int, msg string) {
	if jsonOut {
		return
	}
	fmt.Fprintf(os.Stderr, "%s %s\n", colorStr("\033[1m\033[36m", fmt.Sprintf("[%d]", n)), msg)
}

func ok(msg string) {
	if jsonOut {
		return
	}
	fmt.Fprintf(os.Stderr, "  %s %s\n", colorStr("\033[32m", "✓"), msg)
}

func fail(msg string) {
	if jsonOut {
		return
	}
	fmt.Fprintf(os.Stderr, "  %s %s\n", colorStr("\033[31m", "✗"), msg)
}

func warn(msg string) {
	if jsonOut {
		return
	}
	fmt.Fprintf(os.Stderr, "  %s %s\n", colorStr("\033[33m", "…"), msg)
}

// ── Shell execution ──

func ssh(script string) error {
	if dryRun {
		preview := strings.TrimSpace(script)
		if len(preview) > 80 {
			preview = preview[:80]
		}
		fmt.Fprintf(os.Stderr, "  [dry-run] ssh %s: %s\n", macHost(), preview)
		return nil
	}
	cmd := exec.Command("ssh", macHost(), "bash", "-s")
	cmd.Stdin = strings.NewReader(script)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func sshOutput(script string) (string, error) {
	if dryRun {
		preview := strings.TrimSpace(script)
		if len(preview) > 80 {
			preview = preview[:80]
		}
		fmt.Fprintf(os.Stderr, "  [dry-run] ssh %s: %s\n", macHost(), preview)
		return "[dry-run]", nil
	}
	cmd := exec.Command("ssh", macHost(), "bash", "-s")
	cmd.Stdin = strings.NewReader(script)
	out, err := cmd.CombinedOutput()
	return strings.TrimSpace(string(out)), err
}

func localRun(name string, args ...string) error {
	if dryRun {
		fmt.Fprintf(os.Stderr, "  [dry-run] %s %s\n", name, strings.Join(args, " "))
		return nil
	}
	cmd := exec.Command(name, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func vpsSourcePath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "claudecode-telegram", "boo", "boo-app")
}

// ── git tag ──

// gitTag creates a git tag at HEAD if it doesn't already exist.
func gitTag(tag string) error {
	repoDir := filepath.Join(filepath.Dir(vpsSourcePath()), "..") // claudecode-telegram root

	// Check if tag exists locally
	cmd := exec.Command("git", "tag", "-l", tag)
	cmd.Dir = repoDir
	out, _ := cmd.Output()
	if strings.TrimSpace(string(out)) == tag {
		fmt.Fprintf(os.Stderr, "  Tag %s already exists locally, using it\n", tag)
		return nil
	}
	// Create tag
	step(0, fmt.Sprintf("Creating git tag %s", tag))
	createCmd := exec.Command("git", "tag", tag)
	createCmd.Dir = repoDir
	createCmd.Stdout = os.Stdout
	createCmd.Stderr = os.Stderr
	if err := createCmd.Run(); err != nil {
		fail(fmt.Sprintf("Failed to create tag %s", tag))
		return err
	}
	// Push tag (ignore error if already exists on remote)
	pushCmd := exec.Command("git", "push", "origin", tag)
	pushCmd.Dir = repoDir
	pushCmd.Stdout = os.Stdout
	pushCmd.Stderr = os.Stderr
	if err := pushCmd.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "  Tag may already exist on remote, continuing\n")
	}
	ok(fmt.Sprintf("Tagged %s", tag))
	return nil
}

// ── sync source to Mac Mini ──

// syncBooSource rsync's boo-app source from VPS git to Mac Mini, ensuring
// the build always uses the exact code from git (not stale files on Mac Mini).
func syncBooSource() error {
	src := vpsSourcePath() + "/"
	dst := fmt.Sprintf("%s:%s/", macHost(), booSourceMac)
	step(0, "Syncing boo-app source to Mac Mini")
	if err := localRun("rsync", "-az", "--delete",
		"--exclude", ".build/",
		"--exclude", ".swiftpm/",
		src, dst); err != nil {
		fail("Source sync failed")
		return err
	}
	// Verify key files match
	out, err := sshOutput(fmt.Sprintf(
		`/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" %s/Sources/BooApp/Info.plist`,
		booSourceMac))
	if err == nil {
		fmt.Fprintf(os.Stderr, "  Mac Mini source version: %s\n", out)
	}
	ok("Source synced from VPS git")
	return nil
}

// ── dev build ──

func cmdDevBuild(target string, release bool) error {
	switch target {
	case "boo":
		return cmdDevBuildBoo(release)
	case "ghostty-boo":
		return cmdDevBuildGhosttyBoo(release)
	case "all":
		if err := cmdDevBuildBoo(release); err != nil {
			return err
		}
		return cmdDevBuildGhosttyBoo(release)
	default:
		return fmt.Errorf("unknown target: %s (use boo, ghostty-boo, or all)", target)
	}
}

func cmdDevBuildBoo(release bool) error {
	mode := "debug"
	flags := ""
	if release {
		mode = "release"
		flags = "-c release"
		// Sync source from VPS git before release builds
		if err := syncBooSource(); err != nil {
			return err
		}
	}

	step(1, fmt.Sprintf("Building boo-app (%s) on Mac Mini", mode))
	if err := ssh(fmt.Sprintf(`cd %s && SWIFT_BUILD_DIR=%s swift build %s 2>&1`, booSourceMac, swiftBuildDir, flags)); err != nil {
		fail("Build failed")
		return err
	}
	ok("Build succeeded")

	if release {
		step(2, "Creating Boo.app bundle")
		if err := ssh(fmt.Sprintf(`
set -e
APP="%s"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp %s/.build/release/BooApp "$APP/Contents/MacOS/BooApp"
cp %s/Sources/BooApp/Info.plist "$APP/Contents/Info.plist"
echo "Bundle created at $APP"
ls -lh "$APP/Contents/MacOS/BooApp"
/usr/libexec/PlistBuddy -c "Print CFBundleIdentifier" "$APP/Contents/Info.plist"
`, appPathBoo, booSourceMac, booSourceMac)); err != nil {
			fail("Bundle creation failed")
			return err
		}
		ok("Boo.app bundle ready")
	}
	return nil
}

func cmdDevBuildGhosttyBoo(release bool) error {
	zigFlags := ""
	if release {
		zigFlags = "--release=fast"
	}

	step(1, "Building Zig library")
	if err := ssh(fmt.Sprintf(`cd %s && zig build %s -Dapp-runtime=none 2>&1`, ghosttySourceMac, zigFlags)); err != nil {
		fail("Zig build failed")
		return err
	}
	ok("Zig library built")

	step(2, "Building Xcode app (Release)")
	if err := ssh(fmt.Sprintf(`cd %s && xcodebuild -project macos/Ghostty.xcodeproj \
  -scheme Ghostty -configuration Release \
  -derivedDataPath /tmp/ghostty-boo-build \
  SYMROOT=/tmp/ghostty-boo-build \
  CODE_SIGN_IDENTITY="-" CODE_SIGNING_REQUIRED=NO \
  build 2>&1 | tail -5`, ghosttySourceMac)); err != nil {
		fail("Xcode build failed")
		return err
	}
	ok("Xcode build succeeded")

	step(3, "Preparing Ghostty Boo.app")
	if err := ssh(fmt.Sprintf(`
set -e
rm -rf "%s"
cp -R "/tmp/ghostty-boo-build/Release/Ghostty.app" "%s"
/usr/libexec/PlistBuddy -c "Set :CFBundleName 'Ghostty Boo'" "%s/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier '%s'" "%s/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile GhosttyBoo" "%s/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIconName GhosttyBoo" "%s/Contents/Info.plist"
# Replace icon with Ghostty Boo variant (boo badge)
if [ -f /tmp/GhosttyBoo.icns ]; then
  cp /tmp/GhosttyBoo.icns "%s/Contents/Resources/GhosttyBoo.icns"
elif [ -f "%s/Contents/Resources/Ghostty.icns" ] && [ ! -f "%s/Contents/Resources/GhosttyBoo.icns" ]; then
  mv "%s/Contents/Resources/Ghostty.icns" "%s/Contents/Resources/GhosttyBoo.icns"
fi
echo "Build mode:"
"%s/Contents/MacOS/ghostty" --version 2>&1 | grep "build mode" || true
`, appPathGhostty, appPathGhostty, appPathGhostty, bundleIDGhostty, appPathGhostty, appPathGhostty, appPathGhostty, appPathGhostty, appPathGhostty, appPathGhostty, appPathGhostty, appPathGhostty, appPathGhostty)); err != nil {
		fail("App preparation failed")
		return err
	}
	ok("Ghostty Boo.app ready")
	return nil
}

// ── dev test ──

func cmdDevTest() error {
	step(1, "Running Swift tests on Mac Mini")
	if err := ssh(fmt.Sprintf(`cd %s && SWIFT_BUILD_DIR=%s swift test 2>&1`, booSourceMac, swiftBuildDir)); err != nil {
		fail("Tests failed")
		return err
	}
	ok("All tests passed")
	return nil
}

// ── dev sync ──

func cmdDevSync(file string) error {
	src := filepath.Join(vpsSourcePath(), file)
	dst := fmt.Sprintf("%s:%s/%s", macHost(), booSourceMac, file)
	step(1, fmt.Sprintf("Syncing %s → Mac Mini", file))
	if err := localRun("scp", src, dst); err != nil {
		fail("Sync failed")
		return err
	}
	ok("Synced")
	return nil
}

// ── sign ──

func cmdSign(target string) error {
	targets := resolveTargets(target)
	if targets == nil {
		return fmt.Errorf("unknown target: %s (use boo, ghostty-boo, or all)", target)
	}
	for i, t := range targets {
		step(i+1, fmt.Sprintf("Signing %s", t.name))
		if err := t.signFn(); err != nil {
			fail(fmt.Sprintf("Signing %s failed", t.name))
			return err
		}
		ok(fmt.Sprintf("%s signed", t.name))
	}
	return nil
}

func signBoo() error {
	return ssh(fmt.Sprintf(`
set -e
security unlock-keychain -p "%s" ~/Library/Keychains/login.keychain-db 2>/dev/null || true
codesign --deep --force --options runtime --timestamp --sign "%s" "%s" 2>&1
codesign --verify --strict "%s" 2>&1
`, keychainPW(), signingIdentity(), appPathBoo, appPathBoo))
}

func signGhosttyBoo() error {
	return ssh(fmt.Sprintf(`
set -e
security unlock-keychain -p "%s" ~/Library/Keychains/login.keychain-db 2>/dev/null || true
IDENTITY="%s"
APP="%s"
SPARKLE="$APP/Contents/Frameworks/Sparkle.framework"

# Fix Sparkle symlinks (Xcode copies as dirs, not symlinks)
if [ -d "$SPARKLE/Versions/Current" ] && [ ! -L "$SPARKLE/Versions/Current" ]; then
    rm -rf "$SPARKLE/Versions/Current"
    ln -sf B "$SPARKLE/Versions/Current"
    for item in Autoupdate Resources Sparkle Updater.app XPCServices; do
        if [ -e "$SPARKLE/$item" ] && [ ! -L "$SPARKLE/$item" ]; then
            rm -rf "$SPARKLE/$item"
            ln -sf "Versions/Current/$item" "$SPARKLE/$item"
        fi
    done
fi

# Sign inside-out
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/XPCServices/Downloader.xpc" 2>&1
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/XPCServices/Installer.xpc" 2>&1
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/Updater.app" 2>&1
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE/Versions/B/Autoupdate" 2>&1
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$SPARKLE" 2>&1
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP/Contents/PlugIns/DockTilePlugin.plugin" 2>&1
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP" 2>&1
codesign --verify --strict "$APP" 2>&1
`, keychainPW(), signingIdentity(), appPathGhostty))
}

// ── notarize ──

func cmdNotarize(target string) error {
	targets := resolveTargets(target)
	if targets == nil {
		return fmt.Errorf("unknown target: %s (use boo, ghostty-boo, or all)", target)
	}
	for i, t := range targets {
		step(i+1, fmt.Sprintf("Notarizing %s", t.name))
		if err := t.notarizeFn(); err != nil {
			fail(fmt.Sprintf("Notarizing %s failed", t.name))
			return err
		}
		ok(fmt.Sprintf("%s notarized + stapled", t.name))
	}
	return nil
}

func notarizeApp(appPath, zipName string) error {
	return ssh(fmt.Sprintf(`
set -e
cd /tmp
rm -f "%s"
ditto -c -k --keepParent "%s" "%s"

xcrun notarytool submit "%s" \
  --key %s \
  --key-id %s \
  --issuer %s \
  --wait 2>&1

xcrun stapler staple "%s" 2>&1

rm -f "%s"
ditto -c -k --keepParent "%s" "%s"

spctl --assess --type execute --verbose "%s" 2>&1 || true
echo "Size: $(ls -lh /tmp/%s | awk '{print $5}')"
`, zipName, appPath, zipName, zipName, notaryKey(), notaryKeyID(), notaryIssuer(), appPath, zipName, appPath, zipName, appPath, zipName))
}

func notarizeBoo() error {
	return notarizeApp(appPathBoo, "Boo-macOS-arm64.zip")
}

func notarizeGhosttyBoo() error {
	return notarizeApp(appPathGhostty, "GhosttyBoo-macOS-arm64.zip")
}

// ── verify ──

type VerifyResult struct {
	Target  string `json:"target"`
	Valid   bool   `json:"valid"`
	Details string `json:"details"`
}

func cmdVerify(target string) error {
	targets := resolveTargets(target)
	if targets == nil {
		return fmt.Errorf("unknown target: %s (use boo, ghostty-boo, or all)", target)
	}
	var results []VerifyResult
	for i, t := range targets {
		step(i+1, fmt.Sprintf("Verifying %s", t.name))
		out, err := sshOutput(fmt.Sprintf(`
codesign --verify --strict --verbose=2 "%s" 2>&1
echo "---"
spctl --assess --type execute --verbose "%s" 2>&1
`, t.appPath, t.appPath))
		if !jsonOut {
			fmt.Println(out)
		}
		if err != nil {
			fail(fmt.Sprintf("%s verification failed", t.name))
			results = append(results, VerifyResult{t.name, false, out})
			return err
		}
		ok(fmt.Sprintf("%s verified", t.name))
		results = append(results, VerifyResult{t.name, true, out})
	}
	if jsonOut {
		emitJSON(Result{Command: "verify", Status: "ok", ExitCode: 0, Data: results})
	}
	return nil
}

// ── release ──

type ReleaseAsset struct {
	Name     string `json:"name"`
	SHA256   string `json:"sha256"`
	Uploaded bool   `json:"uploaded"`
}

func cmdRelease(tag string) error {
	step(1, "Downloading zips from Mac Mini")
	os.MkdirAll("/tmp/release-assets", 0755)
	for _, name := range []string{"Boo-macOS-arm64.zip", "GhosttyBoo-macOS-arm64.zip"} {
		if err := localRun("scp", macHost()+":/tmp/"+name, "/tmp/release-assets/"); err != nil {
			fail(fmt.Sprintf("Failed to download %s", name))
			return err
		}
	}
	ok("Zips downloaded")

	// Build boo-cli itself for the release
	step(2, "Building boo binary for release")
	booBinPath := "/tmp/release-assets/boo-linux-amd64"
	exe, _ := os.Executable()
	srcDir := filepath.Dir(exe)
	// If running from source dir, build from there; otherwise use known path
	goFile := filepath.Join(srcDir, "main.go")
	if _, err := os.Stat(goFile); err != nil {
		home, _ := os.UserHomeDir()
		srcDir = filepath.Join(home, "claudecode-telegram", "boo", "boo-cli")
	}
	buildCmd := exec.Command("go", "build", "-o", booBinPath, ".")
	buildCmd.Dir = srcDir
	buildCmd.Env = append(os.Environ(), "GOOS=linux", "GOARCH=amd64", "CGO_ENABLED=0")
	buildCmd.Stdout = os.Stdout
	buildCmd.Stderr = os.Stderr
	if !dryRun {
		if err := buildCmd.Run(); err != nil {
			fail("Failed to build boo binary")
			return err
		}
	}
	ok("boo-linux-amd64 built")

	step(3, "Computing checksums")
	assets := []ReleaseAsset{}
	allFiles := []string{"Boo-macOS-arm64.zip", "GhosttyBoo-macOS-arm64.zip", "boo-linux-amd64"}
	for _, name := range allFiles {
		path := filepath.Join("/tmp/release-assets", name)
		hash, err := sha256File(path)
		if err != nil {
			fail(fmt.Sprintf("Checksum failed for %s", name))
			return err
		}
		if !jsonOut {
			fmt.Printf("  %s  %s\n", hash, name)
		}
		assets = append(assets, ReleaseAsset{Name: name, SHA256: hash})
	}

	// Write checksums file
	checksumPath := filepath.Join("/tmp/release-assets", "SHA256SUMS.txt")
	var checksumLines string
	for _, a := range assets {
		checksumLines += fmt.Sprintf("%s  %s\n", a.SHA256, a.Name)
	}
	if err := os.WriteFile(checksumPath, []byte(checksumLines), 0644); err != nil {
		fail("Failed to write SHA256SUMS.txt")
		return err
	}
	allFiles = append(allFiles, "SHA256SUMS.txt")
	assets = append(assets, ReleaseAsset{Name: "SHA256SUMS.txt", SHA256: ""})

	step(4, "Loading GitHub token")
	token := loadGHToken()
	if token == "" {
		fail("GH_TOKEN_BEASTOIN not found in ~/" + agentEnvPath)
		return fmt.Errorf("missing token")
	}
	ok("Token loaded")

	step(5, fmt.Sprintf("Uploading to %s tag %s", githubRepo, tag))
	env := append(os.Environ(), "GH_TOKEN="+token)
	for i, name := range allFiles {
		path := filepath.Join("/tmp/release-assets", name)
		cmd := exec.Command("gh", "release", "upload", tag, path, "--clobber", "--repo", githubRepo)
		cmd.Env = env
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			fail(fmt.Sprintf("Upload failed for %s", name))
			return err
		}
		ok(fmt.Sprintf("Uploaded %s", name))
		assets[i].Uploaded = true
	}

	if jsonOut {
		emitJSON(Result{Command: "release", Status: "ok", ExitCode: 0, Data: map[string]any{
			"tag":    tag,
			"repo":   githubRepo,
			"assets": assets,
		}})
	}
	return nil
}

// ── ship ──

func cmdShip(tag string) error {
	stages := []struct {
		name string
		fn   func() error
	}{
		{"Tag release", func() error { return gitTag(tag) }},
		{"Build Boo (release)", func() error { return cmdDevBuildBoo(true) }},
		{"Build Ghostty Boo (release)", func() error { return cmdDevBuildGhosttyBoo(true) }},
		{"Sign all", func() error { return cmdSign("all") }},
		{"Notarize all", func() error { return cmdNotarize("all") }},
		{"Verify all", func() error { return cmdVerify("all") }},
		{"Release", func() error { return cmdRelease(tag) }},
	}

	if !jsonOut {
		fmt.Fprintf(os.Stderr, "\n%s\n\n", colorStr("\033[1m\033[36m", "═══ Ship Pipeline ═══"))
	}
	for i, s := range stages {
		if !jsonOut {
			fmt.Fprintf(os.Stderr, "%s\n", colorStr("\033[1m\033[33m", fmt.Sprintf("── Stage %d/%d: %s ──", i+1, len(stages), s.name)))
		}
		if err := s.fn(); err != nil {
			if !jsonOut {
				fmt.Fprintf(os.Stderr, "\n%s\n", colorStr("\033[1m\033[31m", fmt.Sprintf("Pipeline failed at stage %d: %s", i+1, s.name)))
			}
			return err
		}
		if !jsonOut {
			fmt.Fprintln(os.Stderr)
		}
	}
	if !jsonOut {
		fmt.Fprintf(os.Stderr, "%s\n", colorStr("\033[1m\033[32m", "═══ Ship Complete ═══"))
	}
	return nil
}

// ── schema (agent-friendly introspection) ──

type SchemaCommand struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Usage       string         `json:"usage"`
	Args        []SchemaArg    `json:"args,omitempty"`
	Flags       []SchemaFlag   `json:"flags,omitempty"`
	Subcommands []SchemaCommand `json:"subcommands,omitempty"`
	Idempotent  bool           `json:"idempotent"`
	Mutating    bool           `json:"mutating"`
}

type SchemaArg struct {
	Name     string `json:"name"`
	Required bool   `json:"required"`
	Default  string `json:"default,omitempty"`
	Values   string `json:"values,omitempty"`
}

type SchemaFlag struct {
	Name    string `json:"name"`
	Type    string `json:"type"`
	Default string `json:"default,omitempty"`
	Desc    string `json:"description"`
}

func cmdSchema(command string) {
	schema := buildSchema()
	if command != "" {
		for _, cmd := range schema {
			if cmd.Name == command {
				enc := json.NewEncoder(os.Stdout)
				enc.SetIndent("", "  ")
				enc.Encode(cmd)
				return
			}
		}
		emitJSON(Result{Command: "schema", Status: "error", Error: fmt.Sprintf("unknown command: %s", command), ExitCode: 2})
		os.Exit(2)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	enc.Encode(map[string]any{
		"name":        "boo",
		"version":     version,
		"description": "Boo app build, sign, and release CLI",
		"commands":    schema,
		"global_flags": []SchemaFlag{
			{"--json", "bool", "false", "Output structured JSON to stdout"},
			{"--no-color", "bool", "false", "Disable colored output"},
			{"--dry-run", "bool", "false", "Print commands without executing"},
		},
		"env": []map[string]string{
			{"name": "BOO_MAC_HOST", "default": "beastoin-agents-f1-mac-mini", "description": "SSH host for Mac Mini"},
			{"name": "BOO_RELEASE_TAG", "default": "boo-v0.1.0", "description": "Default release tag"},
			{"name": "NO_COLOR", "description": "Disable colors"},
		},
	})
}

func buildSchema() []SchemaCommand {
	targetArg := SchemaArg{Name: "target", Required: false, Default: "all", Values: "boo|ghostty-boo|all"}
	releaseFlag := SchemaFlag{"--release", "bool", "false", "Build in release mode"}
	tagFlag := SchemaFlag{"--tag", "string", defaultTag(), "GitHub release tag"}

	return []SchemaCommand{
		{
			Name:        "dev",
			Description: "Development commands (Mac Mini via SSH)",
			Usage:       "boo dev <subcommand> [args]",
			Subcommands: []SchemaCommand{
				{
					Name:        "build",
					Description: "Build app on Mac Mini. Release mode auto-creates .app bundle.",
					Usage:       "boo dev build [--release] [boo|ghostty-boo|all]",
					Args:        []SchemaArg{{Name: "target", Required: false, Default: "boo", Values: "boo|ghostty-boo|all"}},
					Flags:       []SchemaFlag{releaseFlag},
					Idempotent:  true,
					Mutating:    true,
				},
				{
					Name:        "test",
					Description: "Run Swift tests on Mac Mini",
					Usage:       "boo dev test",
					Idempotent:  true,
				},
				{
					Name:        "sync",
					Description: "Sync source file from VPS to Mac Mini via scp",
					Usage:       "boo dev sync <path>",
					Args:        []SchemaArg{{Name: "path", Required: true}},
					Mutating:    true,
				},
			},
		},
		{
			Name:        "sign",
			Description: "Code sign apps with Developer ID certificate",
			Usage:       "boo sign [boo|ghostty-boo|all]",
			Args:        []SchemaArg{targetArg},
			Idempotent:  true,
			Mutating:    true,
		},
		{
			Name:        "notarize",
			Description: "Notarize and staple apps via Apple notary service",
			Usage:       "boo notarize [boo|ghostty-boo|all]",
			Args:        []SchemaArg{targetArg},
			Mutating:    true,
		},
		{
			Name:        "verify",
			Description: "Verify code signing and notarization status",
			Usage:       "boo verify [boo|ghostty-boo|all]",
			Args:        []SchemaArg{targetArg},
			Idempotent:  true,
		},
		{
			Name:        "release",
			Description: "Download zips from Mac Mini, build boo binary, compute checksums, upload to GitHub release",
			Usage:       "boo release [--tag TAG]",
			Flags:       []SchemaFlag{tagFlag},
			Mutating:    true,
		},
		{
			Name:        "ship",
			Description: "Full pipeline: build → sign → notarize → verify → release",
			Usage:       "boo ship [--tag TAG]",
			Flags:       []SchemaFlag{tagFlag},
			Mutating:    true,
		},
		{
			Name:        "schema",
			Description: "Print machine-readable command schema (JSON)",
			Usage:       "boo schema [command]",
			Args:        []SchemaArg{{Name: "command", Required: false}},
			Idempotent:  true,
		},
	}
}

// ── Helpers ──

type appTarget struct {
	name       string
	appPath    string
	signFn     func() error
	notarizeFn func() error
}

func resolveTargets(target string) []appTarget {
	boo := appTarget{"Boo", appPathBoo, signBoo, notarizeBoo}
	ghostty := appTarget{"Ghostty Boo", appPathGhostty, signGhosttyBoo, notarizeGhosttyBoo}
	switch target {
	case "boo":
		return []appTarget{boo}
	case "ghostty-boo":
		return []appTarget{ghostty}
	case "all", "":
		return []appTarget{boo, ghostty}
	default:
		fail(fmt.Sprintf("Unknown target: %s (use boo, ghostty-boo, or all)", target))
		return nil
	}
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", h.Sum(nil)), nil
}

func loadGHToken() string {
	home, _ := os.UserHomeDir()
	data, err := os.ReadFile(filepath.Join(home, agentEnvPath))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "GH_TOKEN_BEASTOIN=") {
			val := strings.TrimPrefix(line, "GH_TOKEN_BEASTOIN=")
			return strings.Trim(val, "\"'")
		}
	}
	return ""
}

// ── CLI parsing ──

func hasFlag(args []string, flag string) bool {
	for _, a := range args {
		if a == flag {
			return true
		}
	}
	return false
}

func flagValue(args []string, flag, def string) string {
	for i, a := range args {
		if a == flag && i+1 < len(args) {
			return args[i+1]
		}
	}
	return def
}

// Boolean flags that take no value — positionalArg must not skip args after these.
var boolFlags = map[string]bool{
	"--release": true, "--json": true, "--no-color": true, "--dry-run": true,
}

func positionalArg(args []string, def string) string {
	for i, a := range args {
		if !strings.HasPrefix(a, "-") {
			// Skip values that follow a value-taking flag (not boolean flags)
			if i > 0 && strings.HasPrefix(args[i-1], "--") && !boolFlags[args[i-1]] {
				continue
			}
			return a
		}
	}
	return def
}

// ── Usage ──

func usage() {
	fmt.Fprintf(os.Stderr, `%s — Boo app build, sign, and release CLI

%s
  boo [flags] <command> [subcommand] [args]

%s
  dev build [--release] [boo|ghostty-boo|all]   Build app on Mac Mini (default: boo)
  dev test                                       Run Swift tests on Mac Mini
  dev sync <path>                                Sync source file from VPS to Mac Mini

  sign [boo|ghostty-boo|all]                     Code sign apps (default: all)
  notarize [boo|ghostty-boo|all]                 Notarize + staple (default: all)
  verify [boo|ghostty-boo|all]                   Verify signing and notarization (default: all)

  release [--tag TAG]                            Upload zips to GitHub release
  ship [--tag TAG]                               Full pipeline: build → sign → notarize → release

  schema [command]                               Print command schema (JSON)
  help                                           Show this help
  version                                        Print version

%s
  --json       Output structured JSON to stdout
  --no-color   Disable colored output
  --dry-run    Print commands without executing

%s
  BOO_MAC_HOST      SSH host for Mac Mini (default: beastoin-agents-f1-mac-mini)
  BOO_RELEASE_TAG   Default release tag (default: boo-v0.1.0)
  NO_COLOR          Disable colors

%s
  boo dev build --release boo       Release build of Boo
  boo dev build --release all       Release build of both apps
  boo dev test                      Run Swift tests
  boo sign boo                      Sign only Boo.app
  boo ship --tag boo-v0.2.0        Full pipeline to new tag
  boo --json verify boo             JSON output for automation
  boo schema                        Print full command schema
  boo dev sync Sources/BooApp/SettingsView.swift
`,
		colorStr("\033[1m\033[36m", "boo"),
		colorStr("\033[1m", "USAGE"),
		colorStr("\033[1m", "COMMANDS"),
		colorStr("\033[1m", "FLAGS"),
		colorStr("\033[1m", "ENVIRONMENT"),
		colorStr("\033[1m", "EXAMPLES"),
	)
}

func usageDev() {
	fmt.Fprintf(os.Stderr, `%s — Development commands (Mac Mini via SSH)

%s
  boo dev <subcommand> [args]

%s
  build [--release] [boo|ghostty-boo|all]   Build app (default: boo)
  test                                       Run Swift tests
  sync <path>                                Sync source file VPS → Mac Mini

%s
  boo dev build --release boo
  boo dev build ghostty-boo
  boo dev test
  boo dev sync Sources/BooApp/SettingsView.swift
`,
		colorStr("\033[1m\033[36m", "boo dev"),
		colorStr("\033[1m", "USAGE"),
		colorStr("\033[1m", "SUBCOMMANDS"),
		colorStr("\033[1m", "EXAMPLES"),
	)
}

// ── Main ──

func main() {
	started := time.Now()

	// Parse global flags
	args := os.Args[1:]
	var filtered []string
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--json":
			jsonOut = true
			noColor = true
		case "--no-color":
			noColor = true
		case "--dry-run":
			dryRun = true
		case "--version":
			if jsonOut {
				emitJSON(Result{Command: "version", Status: "ok", ExitCode: 0, Data: map[string]string{
					"version": version,
					"os":      runtime.GOOS,
					"arch":    runtime.GOARCH,
				}})
			} else {
				fmt.Printf("boo %s\n", version)
			}
			os.Exit(0)
		default:
			filtered = append(filtered, args[i])
		}
	}
	args = filtered

	if len(args) == 0 {
		if jsonOut {
			cmdSchema("")
		} else {
			usage()
		}
		os.Exit(0)
	}

	var err error
	cmdName := args[0]

	switch cmdName {
	case "dev":
		err = dispatchDev(args[1:])

	case "sign":
		target := positionalArg(args[1:], "all")
		err = cmdSign(target)

	case "notarize":
		target := positionalArg(args[1:], "all")
		err = cmdNotarize(target)

	case "verify":
		target := positionalArg(args[1:], "all")
		err = cmdVerify(target)

	case "release":
		tag := flagValue(args[1:], "--tag", defaultTag())
		err = cmdRelease(tag)

	case "ship":
		tag := flagValue(args[1:], "--tag", defaultTag())
		err = cmdShip(tag)

	case "schema":
		cmd := ""
		if len(args) > 1 {
			cmd = args[1]
		}
		cmdSchema(cmd)

	case "help", "-h", "--help":
		usage()

	case "version":
		if jsonOut {
			emitJSON(Result{Command: "version", Status: "ok", ExitCode: 0, Data: map[string]string{
				"version": version,
				"os":      runtime.GOOS,
				"arch":    runtime.GOARCH,
			}})
		} else {
			fmt.Printf("boo %s\n", version)
		}

	default:
		if jsonOut {
			emitJSON(Result{Command: cmdName, Status: "error", Error: fmt.Sprintf("unknown command: %s", cmdName), ExitCode: 2})
		} else {
			fail(fmt.Sprintf("Unknown command: %s", cmdName))
			fmt.Fprintln(os.Stderr)
			usage()
		}
		os.Exit(2)
	}

	if err != nil {
		if jsonOut {
			emitJSON(Result{
				Command:   cmdName,
				Status:    "error",
				Error:     err.Error(),
				ExitCode:  1,
				DurationS: time.Since(started).Seconds(),
			})
		}
		os.Exit(1)
	}
}

func dispatchDev(args []string) error {
	if len(args) == 0 {
		if jsonOut {
			cmdSchema("dev")
		} else {
			usageDev()
		}
		return nil
	}

	switch args[0] {
	case "build":
		sub := args[1:]
		release := hasFlag(sub, "--release")
		target := positionalArg(sub, "boo")
		return cmdDevBuild(target, release)

	case "test":
		return cmdDevTest()

	case "sync":
		if len(args) < 2 {
			if jsonOut {
				emitJSON(Result{Command: "dev sync", Status: "error", Error: "missing required arg: path", ExitCode: 2})
				os.Exit(2)
			}
			fail("Usage: boo dev sync <relative-path>")
			return fmt.Errorf("missing path")
		}
		return cmdDevSync(args[1])

	case "help", "-h", "--help":
		usageDev()
		return nil

	default:
		if jsonOut {
			emitJSON(Result{Command: "dev " + args[0], Status: "error", Error: fmt.Sprintf("unknown subcommand: %s", args[0]), ExitCode: 2})
			os.Exit(2)
		}
		fail(fmt.Sprintf("Unknown dev subcommand: %s", args[0]))
		fmt.Fprintln(os.Stderr)
		usageDev()
		return fmt.Errorf("unknown subcommand: %s", args[0])
	}
}
