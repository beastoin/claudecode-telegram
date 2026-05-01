package forge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestParseManifest_MinimalManifest(t *testing.T) {
	t.Parallel()

	input := []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
`)

	manifest, err := ParseManifest(input)
	if err != nil {
		t.Fatalf("ParseManifest() error = %v", err)
	}

	if manifest.Name != "mon" {
		t.Fatalf("manifest.Name = %q, want %q", manifest.Name, "mon")
	}

	if manifest.Version != "1.0.0" {
		t.Fatalf("manifest.Version = %q, want %q", manifest.Version, "1.0.0")
	}

	spec, ok := manifest.Vars["HOME"]
	if !ok {
		t.Fatalf("manifest.Vars[HOME] missing")
	}

	if spec.Source != "env" {
		t.Fatalf("manifest.Vars[HOME].Source = %q, want %q", spec.Source, "env")
	}

	if !spec.Required {
		t.Fatalf("manifest.Vars[HOME].Required = false, want true")
	}
}

func TestParseManifest_InvalidYAML(t *testing.T) {
	t.Parallel()

	_, err := ParseManifest([]byte("name: mon\nvars:\n  HOME: ["))
	if err == nil {
		t.Fatal("ParseManifest() error = nil, want non-nil")
	}
}

func TestValidateManifest_RejectsUnknownVarSource(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "mystery",
				Required: true,
			},
		},
	}

	err := ValidateManifest(manifest)
	if err == nil {
		t.Fatal("ValidateManifest() error = nil, want non-nil")
	}
}

func TestValidateManifest_RejectsMissingRequiredFields(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{}

	err := ValidateManifest(manifest)
	if err == nil {
		t.Fatal("ValidateManifest() error = nil, want non-nil")
	}
}

func TestResolveVars_FlagOverridesEnvAndDefault(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"BRIDGE_URL": {
				Source:   "flag",
				Default:  "http://default",
				Required: true,
			},
		},
	}

	resolved, err := ResolveVars(manifest, ResolveOptions{
		Env: map[string]string{
			"BRIDGE_URL": "http://env",
		},
		Flags: map[string]string{
			"BRIDGE_URL": "http://flag",
		},
	})
	if err != nil {
		t.Fatalf("ResolveVars() error = %v", err)
	}

	if got := resolved["BRIDGE_URL"]; got != "http://flag" {
		t.Fatalf("resolved[BRIDGE_URL] = %q, want %q", got, "http://flag")
	}
}

func TestResolveVars_ExpandsNestedDefaults(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{
		Name:    "luck",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"TEAM_DIR": {
				Source:   "default",
				Default:  "$HOME/team",
				Required: true,
			},
			"OPS_EMAIL_SCRIPT": {
				Source:   "default",
				Default:  "$TEAM_DIR/scripts/ops_email_send.py",
				Required: true,
			},
		},
	}

	resolved, err := ResolveVars(manifest, ResolveOptions{
		Env: map[string]string{
			"HOME": "/tmp/agent",
		},
	})
	if err != nil {
		t.Fatalf("ResolveVars() error = %v", err)
	}

	want := "/tmp/agent/team/scripts/ops_email_send.py"
	if got := resolved["OPS_EMAIL_SCRIPT"]; got != want {
		t.Fatalf("resolved[OPS_EMAIL_SCRIPT] = %q, want %q", got, want)
	}
}

func TestResolveVars_CrossHostBridgeURL(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"BRIDGE_URL": {
				Source:   "flag",
				Default:  "http://localhost:8271",
				Required: true,
			},
		},
	}

	resolved, err := ResolveVars(manifest, ResolveOptions{
		Env: map[string]string{
			"BRIDGE_URL": "http://100.125.36.102:8271",
		},
	})
	if err != nil {
		t.Fatalf("ResolveVars() error = %v", err)
	}

	if got := resolved["BRIDGE_URL"]; got != "http://100.125.36.102:8271" {
		t.Fatalf("resolved[BRIDGE_URL] = %q, want %q", got, "http://100.125.36.102:8271")
	}
}

func TestResolveVars_MissingRequiredFails(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{
		Name:    "luck",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"ANTHROPIC_API_KEY": {
				Source:   "creds",
				Required: true,
			},
		},
	}

	_, err := ResolveVars(manifest, ResolveOptions{})
	if err == nil {
		t.Fatal("ResolveVars() error = nil, want non-nil")
	}

	if !strings.Contains(err.Error(), "ANTHROPIC_API_KEY") {
		t.Fatalf("ResolveVars() error = %q, want variable name in message", err)
	}
}

func TestPrepare_CreatesResolvedDirs(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Dirs: []string{
			"$HOME/team",
			"$HOME/team/mon",
		},
	}

	prepared, err := Prepare(manifest, map[string]string{"HOME": root})
	if err != nil {
		t.Fatalf("Prepare() error = %v", err)
	}

	for _, dir := range prepared.Dirs {
		info, err := os.Stat(dir)
		if err != nil {
			t.Fatalf("os.Stat(%q) error = %v", dir, err)
		}
		if !info.IsDir() {
			t.Fatalf("%q is not a directory", dir)
		}
	}
}

func TestPrepare_IsIdempotent(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Dirs: []string{
			"$HOME/cache",
		},
	}

	first, err := Prepare(manifest, map[string]string{"HOME": root})
	if err != nil {
		t.Fatalf("Prepare() first error = %v", err)
	}

	second, err := Prepare(manifest, map[string]string{"HOME": root})
	if err != nil {
		t.Fatalf("Prepare() second error = %v", err)
	}

	want := filepath.Join(root, "cache")
	if len(first.Dirs) != 1 || first.Dirs[0] != want {
		t.Fatalf("first.Dirs = %#v, want [%q]", first.Dirs, want)
	}
	if len(second.Dirs) != 1 || second.Dirs[0] != want {
		t.Fatalf("second.Dirs = %#v, want [%q]", second.Dirs, want)
	}
}

func TestPrepare_CreatesDirsWith0700Permissions(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Dirs: []string{
			"$HOME/team",
			"$HOME/team/mon",
		},
	}

	prepared, err := Prepare(manifest, map[string]string{"HOME": root})
	if err != nil {
		t.Fatalf("Prepare() error = %v", err)
	}

	for _, dir := range prepared.Dirs {
		info, err := os.Stat(dir)
		if err != nil {
			t.Fatalf("os.Stat(%q) error = %v", dir, err)
		}
		if got := info.Mode().Perm(); got != 0o700 {
			t.Fatalf("permissions for %q = %o, want 700", dir, got)
		}
	}
}

func TestExtract_WritesEmbeddedPlaintextFile(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "files/team/mon/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"files/team/mon/charter.md": []byte("charter"),
		},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	data, err := os.ReadFile(filepath.Join(root, "team", "mon", "charter.md"))
	if err != nil {
		t.Fatalf("os.ReadFile() error = %v", err)
	}

	if got := string(data); got != "charter" {
		t.Fatalf("written file = %q, want %q", got, "charter")
	}
}

func TestExtract_WritesFilesWith0600Permissions(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "files/team/mon/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"files/team/mon/charter.md": []byte("charter"),
		},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	info, err := os.Stat(filepath.Join(root, "team", "mon", "charter.md"))
	if err != nil {
		t.Fatalf("os.Stat() error = %v", err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Fatalf("file permissions = %o, want 600", got)
	}
}

func TestExtract_VerifiesChecksumAfterWrite(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	content := []byte("charter")
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "files/team/mon/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"files/team/mon/charter.md": content,
		},
		Verifier: NewChecksumVerifier(map[string]string{
			"files/team/mon/charter.md": sha256Hex(content),
		}),
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}
}

func TestExtract_StopsOnChecksumMismatch(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "files/team/mon/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"files/team/mon/charter.md": []byte("charter"),
		},
		Verifier: NewChecksumVerifier(map[string]string{
			"files/team/mon/charter.md": strings.Repeat("0", 64),
		}),
	})
	if err == nil {
		t.Fatal("Extract() error = nil, want checksum mismatch")
	}
}

func TestExtract_MergeKeepsNewerDiskFile(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "memory", "state.md")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("disk version"), 0o644); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "files/memory/state.md",
				Dest:   "$HOME/memory/state.md",
				Merge:  true,
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"files/memory/state.md": []byte("embedded version"),
		},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	data, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("os.ReadFile() error = %v", err)
	}

	if got := string(data); got != "disk version" {
		t.Fatalf("merged file = %q, want %q", got, "disk version")
	}
}

func TestExtract_EncryptedFileUsesDecryptor(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	decryptor := &stubDecryptor{plaintext: []byte("decrypted secret")}
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source:    "creds/agent.env",
				Dest:      "$HOME/.config/agent.env",
				Encrypted: true,
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"creds/agent.env": []byte("ciphertext"),
		},
		Decryptor: decryptor,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	if decryptor.calls != 1 {
		t.Fatalf("decryptor.calls = %d, want 1", decryptor.calls)
	}

	data, err := os.ReadFile(filepath.Join(root, ".config", "agent.env"))
	if err != nil {
		t.Fatalf("os.ReadFile() error = %v", err)
	}

	if got := string(data); got != "decrypted secret" {
		t.Fatalf("written secret = %q, want %q", got, "decrypted secret")
	}
}

func TestBootstrap_ChecksInstalledTools(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"claude --version": {ExitCode: 0, Stdout: "claude 1.0.0"},
		},
	}
	manifest := &Manifest{
		Tools: []ToolSpec{
			{
				Name:     "claude",
				Check:    "claude --version",
				Required: true,
			},
		},
	}

	report, err := Bootstrap(manifest, "linux", runner)
	if err != nil {
		t.Fatalf("Bootstrap() error = %v", err)
	}

	if len(report) != 1 || !report[0].Installed {
		t.Fatalf("Bootstrap() report = %#v, want installed tool", report)
	}

	if got := runner.commands; len(got) != 1 || got[0] != "claude --version" {
		t.Fatalf("runner.commands = %#v, want only check command", got)
	}
}

func TestBootstrap_InstallsMissingRequiredTool(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		sequence: map[string][]RunResult{
			"tmux -V": {
				{ExitCode: 1},
				{ExitCode: 0, Stdout: "tmux 3.4"},
			},
			"apt install -y tmux": {
				{ExitCode: 0},
			},
		},
	}
	manifest := &Manifest{
		Tools: []ToolSpec{
			{
				Name:  "tmux",
				Check: "tmux -V",
				Install: map[string]string{
					"linux": "apt install -y tmux",
				},
				Required: true,
			},
		},
	}

	report, err := Bootstrap(manifest, "linux", runner)
	if err != nil {
		t.Fatalf("Bootstrap() error = %v", err)
	}

	if len(report) != 1 || !report[0].Installed || !report[0].InstalledByBootstrap {
		t.Fatalf("Bootstrap() report = %#v, want installed-via-bootstrap", report)
	}

	want := []string{"tmux -V", "apt install -y tmux", "tmux -V"}
	if got := runner.commands; strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", got, want)
	}
}

func TestBootstrap_SkipsOptionalMissingTool(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"gws --version": {ExitCode: 1},
		},
	}
	manifest := &Manifest{
		Tools: []ToolSpec{
			{
				Name:     "gws",
				Check:    "gws --version",
				Required: false,
			},
		},
	}

	report, err := Bootstrap(manifest, "linux", runner)
	if err != nil {
		t.Fatalf("Bootstrap() error = %v", err)
	}

	if len(report) != 1 || report[0].Installed {
		t.Fatalf("Bootstrap() report = %#v, want missing optional tool", report)
	}
}

func TestReadiness_RunsFixThenRechecks(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		sequence: map[string][]RunResult{
			"gcloud auth list --format=\"value(account)\"": {
				{ExitCode: 0, Stdout: "other-account"},
				{ExitCode: 0, Stdout: "beastoin-agents"},
			},
			"gcloud auth activate-service-account --key-file=/tmp/key.json": {
				{ExitCode: 0},
			},
		},
	}
	manifest := &Manifest{
		Readiness: []ReadinessCheck{
			{
				Name:     "gcp-auth",
				Check:    "gcloud auth list --format=\"value(account)\"",
				Expect:   "contains \"beastoin-agents\"",
				Fix:      "gcloud auth activate-service-account --key-file=$GOOGLE_APPLICATION_CREDENTIALS",
				Required: true,
			},
		},
	}

	report, err := RunReadiness(manifest, map[string]string{
		"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/key.json",
	}, runner)
	if err != nil {
		t.Fatalf("RunReadiness() error = %v", err)
	}

	if len(report) != 1 || !report[0].Passed || !report[0].Fixed {
		t.Fatalf("RunReadiness() report = %#v, want fixed passing check", report)
	}
}

func TestReadiness_RequiredFailureStops(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"curl -sf http://bridge/health": {ExitCode: 1},
		},
	}
	manifest := &Manifest{
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge-reachable",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
	}

	_, err := RunReadiness(manifest, map[string]string{
		"BRIDGE_URL": "http://bridge",
	}, runner)
	if err == nil {
		t.Fatal("RunReadiness() error = nil, want required failure")
	}
}

func TestCheckMode_ReportsResolvedVarsToolsAndReadiness(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V":                       {ExitCode: 0, Stdout: "tmux 3.4"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"BRIDGE_URL": {
				Source:   "flag",
				Default:  "http://default",
				Required: true,
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
	}

	report, err := RunCheckMode(manifest, CheckOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{
				"HOME": "/tmp/home",
			},
			Flags: map[string]string{
				"BRIDGE_URL": "http://bridge",
			},
		},
		GOOS:   "linux",
		Runner: runner,
	})
	if err != nil {
		t.Fatalf("RunCheckMode() error = %v", err)
	}

	if report.Worker != "mon" || report.Version != "1.0.0" {
		t.Fatalf("RunCheckMode() worker/version = %q/%q, want mon/1.0.0", report.Worker, report.Version)
	}
	if got := report.ResolvedVars["BRIDGE_URL"]; got != "http://bridge" {
		t.Fatalf("report.ResolvedVars[BRIDGE_URL] = %q, want %q", got, "http://bridge")
	}
	if len(report.Tools) != 1 || report.Tools[0].Name != "tmux" {
		t.Fatalf("report.Tools = %#v, want tmux status", report.Tools)
	}
	if len(report.Readiness) != 1 || !report.Readiness[0].Passed {
		t.Fatalf("report.Readiness = %#v, want passing readiness", report.Readiness)
	}
}

func TestTmuxRuntime_StartNewSession(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: "claude-prod-mon",
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	want := []string{
		"tmux new-session -d -s claude-prod-mon",
		`tmux send-keys -t claude-prod-mon "claude --dangerously-skip-permissions" Enter`,
	}
	if got := runner.commands; strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", got, want)
	}
}

func TestHookManager_MergesWorkerHooksIntoExistingSettings(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}

	settingsPath := filepath.Join(configDir, "settings.json")
	initial := map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{"command": "/existing/hook.sh"},
			},
		},
	}
	writeJSONFile(t, settingsPath, initial)

	manager := HookManager{}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
			},
		},
	}

	if err := manager.Install(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}); err != nil {
		t.Fatalf("HookManager.Install() error = %v", err)
	}

	var settings map[string]any
	readJSONFile(t, settingsPath, &settings)
	if !settingsHookExists(settings, "Stop", "/existing/hook.sh", "") {
		t.Fatalf("settings missing existing hook: %#v", settings)
	}
	if !settingsHookExists(settings, "Stop", filepath.Join(configDir, "hooks", "send-to-telegram.sh"), "") {
		t.Fatalf("settings missing worker hook: %#v", settings)
	}
}

func TestHookManager_PreservesUnrelatedSettings(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}

	settingsPath := filepath.Join(configDir, "settings.json")
	initial := map[string]any{
		"mcpServers": map[string]any{
			"memory": map[string]any{"command": "memory-server"},
		},
	}
	writeJSONFile(t, settingsPath, initial)

	manager := HookManager{}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
			},
		},
	}

	if err := manager.Install(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}); err != nil {
		t.Fatalf("HookManager.Install() error = %v", err)
	}

	var settings map[string]any
	readJSONFile(t, settingsPath, &settings)
	if _, ok := settings["mcpServers"]; !ok {
		t.Fatalf("settings missing preserved mcpServers: %#v", settings)
	}
}

func TestTmuxRuntime_AdoptsExistingSession(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{}
	runtime := &TmuxRuntime{
		Runner:       runner,
		Session:      "claude-prod-mon",
		AdoptSession: "legacy-mon",
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	want := []string{
		"tmux rename-session -t legacy-mon claude-prod-mon",
	}
	if got := runner.commands; strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", got, want)
	}
}

func TestTmuxRuntime_SendUsesSendKeys(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: "claude-prod-mon",
	}

	if err := runtime.Send("hello world"); err != nil {
		t.Fatalf("runtime.Send() error = %v", err)
	}

	want := []string{`tmux send-keys -t claude-prod-mon "hello world" Enter`}
	if got := runner.commands; strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", got, want)
	}
}

func TestTmuxRuntime_HealthDetectsDeadSession(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: "claude-prod-mon",
	}

	if err := runtime.Health(); err == nil {
		t.Fatal("runtime.Health() error = nil, want dead session error")
	}
}

func TestWatchdog_RestartsDeadRuntime(t *testing.T) {
	t.Parallel()

	runtime := &stubRuntime{healthErr: errBoom}
	watchdog := &Watchdog{
		Runtime: runtime,
		Clock:   fixedClock{},
	}

	if err := watchdog.CheckOnce(); err != nil {
		t.Fatalf("watchdog.CheckOnce() error = %v", err)
	}

	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
}

func TestWatchdog_AlertsOnCriticalIntegrityDrift(t *testing.T) {
	t.Parallel()

	bridge := &stubBridgeClient{}
	watchdog := &Watchdog{
		Runtime:   &stubRuntime{},
		Bridge:    bridge,
		Integrity: stubIntegrityMonitor{err: errBoom},
		Clock:     fixedClock{},
	}

	if err := watchdog.CheckOnce(); err != nil {
		t.Fatalf("watchdog.CheckOnce() error = %v", err)
	}

	if len(bridge.alerts) != 1 {
		t.Fatalf("bridge.alerts = %#v, want 1 alert", bridge.alerts)
	}
}

func TestAppRun_LocalMVPHappyPath(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	runtime := &stubRuntime{}
	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V":                       {ExitCode: 0, Stdout: "tmux 3.4"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"CLAUDE_CONFIG_DIR": {
				Source:   "default",
				Default:  "$HOME/.claude",
				Required: true,
			},
			"BRIDGE_URL": {
				Source:   "flag",
				Default:  "http://default",
				Required: true,
			},
		},
		Dirs: []string{
			"$CLAUDE_CONFIG_DIR/hooks",
		},
		Files: []FileSpec{
			{
				Source: "hooks/send-to-telegram.sh",
				Dest:   "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
			},
		},
	}

	app := App{
		Manifest: manifest,
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		},
		Runner:      runner,
		Runtime:     runtime,
		HookManager: HookManager{},
		GOOS:        "linux",
	}

	if err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{
				"HOME": root,
			},
			Flags: map[string]string{
				"BRIDGE_URL": "http://bridge",
			},
		},
	}); err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}

	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
	if _, err := os.Stat(filepath.Join(configDir, "hooks", "send-to-telegram.sh")); err != nil {
		t.Fatalf("expected extracted hook script, stat error = %v", err)
	}
	if _, err := os.Stat(filepath.Join(configDir, "settings.json")); err != nil {
		t.Fatalf("expected settings.json, stat error = %v", err)
	}
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func writeJSONFile(t *testing.T, path string, value any) {
	t.Helper()

	data, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}
}

func readJSONFile(t *testing.T, path string, dest any) {
	t.Helper()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("os.ReadFile() error = %v", err)
	}
	if err := json.Unmarshal(data, dest); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
}

type stubDecryptor struct {
	plaintext []byte
	calls     int
}

func (s *stubDecryptor) Decrypt(ciphertext []byte) ([]byte, error) {
	s.calls++
	return append([]byte(nil), s.plaintext...), nil
}

type stubRunner struct {
	results  map[string]RunResult
	sequence map[string][]RunResult
	commands []string
}

func (s *stubRunner) Run(command string) (RunResult, error) {
	s.commands = append(s.commands, command)

	if len(s.sequence[command]) > 0 {
		result := s.sequence[command][0]
		s.sequence[command] = s.sequence[command][1:]
		return result, nil
	}

	if result, ok := s.results[command]; ok {
		return result, nil
	}

	return RunResult{ExitCode: 0}, nil
}

var errBoom = errors.New("boom")

type stubRuntime struct {
	startCalls int
	healthErr  error
}

func (s *stubRuntime) Start() error {
	s.startCalls++
	return nil
}

func (s *stubRuntime) Send(message string) error {
	return nil
}

func (s *stubRuntime) Health() error {
	return s.healthErr
}

type stubBridgeClient struct {
	alerts []string
}

func (s *stubBridgeClient) Alert(message string) error {
	s.alerts = append(s.alerts, message)
	return nil
}

type stubIntegrityMonitor struct {
	err error
}

func (s stubIntegrityMonitor) VerifyCritical() error {
	return s.err
}

type fixedClock struct{}

func (fixedClock) Now() time.Time {
	return time.Unix(0, 0)
}

func TestExtract_ConflictDetection_StopsOnDifferingExistingFile(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("MkdirAll error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("existing version"), 0o600); err != nil {
		t.Fatalf("WriteFile error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "hooks/send-to-telegram.sh",
				Dest:   "$HOME/hooks/send-to-telegram.sh",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"hooks/send-to-telegram.sh": []byte("embedded version")},
	})

	var conflictErr *ExtractConflictError
	if !errors.As(err, &conflictErr) {
		t.Fatalf("Extract() error type = %T, want *ExtractConflictError", err)
	}
	if len(conflictErr.Conflicts) != 1 {
		t.Fatalf("len(conflicts) = %d, want 1", len(conflictErr.Conflicts))
	}
	if conflictErr.Conflicts[0].Path != dest {
		t.Fatalf("conflict path = %q, want %q", conflictErr.Conflicts[0].Path, dest)
	}

	data, _ := os.ReadFile(dest)
	if string(data) != "existing version" {
		t.Fatalf("file was overwritten despite conflict")
	}
}

func TestExtract_ConflictDetection_AllowsIdenticalExistingFile(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("MkdirAll error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("same content"), 0o600); err != nil {
		t.Fatalf("WriteFile error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "hooks/send-to-telegram.sh",
				Dest:   "$HOME/hooks/send-to-telegram.sh",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"hooks/send-to-telegram.sh": []byte("same content")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v, want nil (identical files)", err)
	}
}

func TestExtract_ConflictDetection_NewFileWritesWithoutConflict(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "team/mon2/charter.md",
				Dest:   "$HOME/team/mon2/charter.md",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"team/mon2/charter.md": []byte("new charter")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	data, _ := os.ReadFile(filepath.Join(root, "team", "mon2", "charter.md"))
	if string(data) != "new charter" {
		t.Fatalf("file content = %q, want %q", string(data), "new charter")
	}
}

func TestExtract_ForceExtract_OverridesConflictingFiles(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("MkdirAll error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("existing version"), 0o600); err != nil {
		t.Fatalf("WriteFile error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "hooks/send-to-telegram.sh",
				Dest:   "$HOME/hooks/send-to-telegram.sh",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:         map[string]string{"HOME": root},
		Source:       MapFileSource{"hooks/send-to-telegram.sh": []byte("embedded version")},
		ForceExtract: true,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	data, _ := os.ReadFile(dest)
	if string(data) != "embedded version" {
		t.Fatalf("file content = %q, want %q", string(data), "embedded version")
	}
}

func TestExtract_SkipConflicts_KeepsExistingFiles(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("MkdirAll error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("existing version"), 0o600); err != nil {
		t.Fatalf("WriteFile error = %v", err)
	}

	newDest := filepath.Join(root, "team", "mon2", "charter.md")

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "hooks/send-to-telegram.sh",
				Dest:   "$HOME/hooks/send-to-telegram.sh",
			},
			{
				Source: "team/mon2/charter.md",
				Dest:   "$HOME/team/mon2/charter.md",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("embedded version"),
			"team/mon2/charter.md":      []byte("new charter"),
		},
		SkipConflicts: true,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	data, _ := os.ReadFile(dest)
	if string(data) != "existing version" {
		t.Fatalf("conflicting file was overwritten; got %q, want %q", string(data), "existing version")
	}

	data, _ = os.ReadFile(newDest)
	if string(data) != "new charter" {
		t.Fatalf("new file not written; got %q, want %q", string(data), "new charter")
	}
}

func TestExtract_OverwriteAlways_SkipsConflictCheck(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "team", "mon2", "charter.md")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("MkdirAll error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("old charter"), 0o600); err != nil {
		t.Fatalf("WriteFile error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source:    "team/mon2/charter.md",
				Dest:      "$HOME/team/mon2/charter.md",
				Overwrite: "always",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"team/mon2/charter.md": []byte("new charter")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	data, _ := os.ReadFile(dest)
	if string(data) != "new charter" {
		t.Fatalf("file content = %q, want %q", string(data), "new charter")
	}
}

func TestExtract_MultipleConflicts_ReportsAll(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	for _, name := range []string{"hooks/a.sh", "hooks/b.sh"} {
		dest := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			t.Fatalf("MkdirAll error = %v", err)
		}
		if err := os.WriteFile(dest, []byte("existing-"+name), 0o600); err != nil {
			t.Fatalf("WriteFile error = %v", err)
		}
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "hooks/a.sh", Dest: "$HOME/hooks/a.sh"},
			{Source: "hooks/b.sh", Dest: "$HOME/hooks/b.sh"},
			{Source: "new.md", Dest: "$HOME/new.md"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"hooks/a.sh": []byte("embedded-a"),
			"hooks/b.sh": []byte("embedded-b"),
			"new.md":     []byte("new file"),
		},
	})

	var conflictErr *ExtractConflictError
	if !errors.As(err, &conflictErr) {
		t.Fatalf("Extract() error type = %T, want *ExtractConflictError", err)
	}
	if len(conflictErr.Conflicts) != 2 {
		t.Fatalf("len(conflicts) = %d, want 2", len(conflictErr.Conflicts))
	}

	if _, err := os.Stat(filepath.Join(root, "new.md")); !os.IsNotExist(err) {
		t.Fatal("new file was written despite conflicts — should not write any files")
	}
}

func TestExtract_ConflictDetection_DoesNotBlockMergeFiles(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "memory", "state.md")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("MkdirAll error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("disk version"), 0o600); err != nil {
		t.Fatalf("WriteFile error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source: "memory/state.md",
				Dest:   "$HOME/memory/state.md",
				Merge:  true,
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"memory/state.md": []byte("embedded version")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v, merge files should not trigger conflicts", err)
	}

	data, _ := os.ReadFile(dest)
	if string(data) != "disk version" {
		t.Fatalf("merge file was overwritten; got %q, want %q", string(data), "disk version")
	}
}

func TestExtractConflictError_FormatsMessage(t *testing.T) {
	t.Parallel()

	err := &ExtractConflictError{
		Conflicts: []ExtractConflict{
			{
				Path:         "/home/user/.claude/hooks/forward-to-bridge.py",
				ExistingSize: 1531,
				ExistingMod:  "2026-04-14 12:50",
				EmbeddedSize: 1247,
			},
		},
	}

	msg := err.Error()
	if !strings.Contains(msg, "CONFLICT") {
		t.Fatalf("error message missing CONFLICT label")
	}
	if !strings.Contains(msg, "forward-to-bridge.py") {
		t.Fatalf("error message missing file path")
	}
	if !strings.Contains(msg, "--force-extract") {
		t.Fatalf("error message missing --force-extract suggestion")
	}
	if !strings.Contains(msg, "--skip-conflicts") {
		t.Fatalf("error message missing --skip-conflicts suggestion")
	}
}

func TestParseWorkerCLI_ForceExtractAndSkipConflictsFlags(t *testing.T) {
	t.Parallel()

	opts, err := ParseWorkerCLI([]string{"--force-extract"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI error = %v", err)
	}
	if !opts.ForceExtract {
		t.Fatal("ForceExtract = false, want true")
	}

	opts, err = ParseWorkerCLI([]string{"--skip-conflicts"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI error = %v", err)
	}
	if !opts.SkipConflicts {
		t.Fatal("SkipConflicts = false, want true")
	}
}
