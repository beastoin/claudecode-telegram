package forge

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNewEngineDriver_ClaudeCode(t *testing.T) {
	driver, err := NewEngineDriver("claude-code")
	if err != nil {
		t.Fatalf("NewEngineDriver: %v", err)
	}
	if driver.ID() != "claude-code" {
		t.Errorf("ID = %q, want %q", driver.ID(), "claude-code")
	}
}

func TestNewEngineDriver_Unknown(t *testing.T) {
	_, err := NewEngineDriver("vim-mode")
	if err == nil {
		t.Fatal("expected error for unknown engine")
	}
	if !strings.Contains(err.Error(), "unknown engine") {
		t.Errorf("error = %q, want 'unknown engine'", err.Error())
	}
}

func TestClaudeCodeDriver_Capabilities(t *testing.T) {
	driver := &ClaudeCodeDriver{}
	caps := driver.Capabilities()

	if !caps.SupportsHooks {
		t.Error("expected SupportsHooks = true")
	}
	if !caps.SupportsResume {
		t.Error("expected SupportsResume = true")
	}
	if caps.ConfigFormat != "settings.json" {
		t.Errorf("ConfigFormat = %q, want %q", caps.ConfigFormat, "settings.json")
	}
	if len(caps.HookEvents) == 0 {
		t.Error("expected non-empty HookEvents")
	}

	hasStop := false
	for _, e := range caps.HookEvents {
		if e == "Stop" {
			hasStop = true
		}
	}
	if !hasStop {
		t.Error("HookEvents should include Stop")
	}
}

func TestClaudeCodeDriver_Prepare_CreatesOnboardingMarker(t *testing.T) {
	tmpDir := t.TempDir()
	configDir := filepath.Join(tmpDir, ".claude")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "test-worker",
		Version: "1.0.0",
	}

	_, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
		},
		GOOS: "linux",
	})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}

	markerPath := filepath.Join(configDir, ".claude.json")
	if _, err := os.Stat(markerPath); err != nil {
		t.Errorf("onboarding marker not created: %v", err)
	}
}

func TestClaudeCodeDriver_Prepare_SkipsExistingMarker(t *testing.T) {
	tmpDir := t.TempDir()
	configDir := filepath.Join(tmpDir, ".claude")
	os.MkdirAll(configDir, 0o755)

	existing := []byte(`{"existing": true}`)
	markerPath := filepath.Join(configDir, ".claude.json")
	os.WriteFile(markerPath, existing, 0o600)

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "test-worker",
		Version: "1.0.0",
	}

	_, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
		},
		GOOS: "linux",
	})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}

	data, _ := os.ReadFile(markerPath)
	if string(data) != string(existing) {
		t.Error("existing marker was overwritten")
	}
}

func TestClaudeCodeDriver_Prepare_PatchesSettings(t *testing.T) {
	tmpDir := t.TempDir()
	configDir := filepath.Join(tmpDir, ".claude")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "test-worker",
		Version: "1.0.0",
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: configDir + "/hooks/send-response.sh",
			},
		},
	}

	_, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
		},
		GOOS: "linux",
	})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}

	settingsPath := filepath.Join(configDir, "settings.json")
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("read settings.json: %v", err)
	}

	if !strings.Contains(string(data), "send-response.sh") {
		t.Error("settings.json does not contain hook command")
	}
	if !strings.Contains(string(data), "Stop") {
		t.Error("settings.json does not contain Stop event")
	}
}

func TestClaudeCodeDriver_Prepare_APIKeyHelper(t *testing.T) {
	tmpDir := t.TempDir()
	configDir := filepath.Join(tmpDir, ".claude")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "test-worker",
		Version: "1.0.0",
	}

	prepared, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
			"ANTHROPIC_API_KEY": "sk-test-key",
		},
		GOOS: "linux",
	})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}

	helperPath := filepath.Join(configDir, "hooks", "api-key-helper.sh")
	if _, err := os.Stat(helperPath); err != nil {
		t.Errorf("api key helper not created: %v", err)
	}

	info, _ := os.Stat(helperPath)
	if info.Mode()&0o100 == 0 {
		t.Error("api key helper is not executable")
	}

	if prepared.Env["FORGE_API_KEY_HELPER"] != helperPath {
		t.Errorf("FORGE_API_KEY_HELPER = %q, want %q", prepared.Env["FORGE_API_KEY_HELPER"], helperPath)
	}
}

func TestClaudeCodeDriver_Prepare_SetsWorkerName(t *testing.T) {
	tmpDir := t.TempDir()
	configDir := filepath.Join(tmpDir, ".claude")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
	}

	prepared, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
		},
		GOOS: "linux",
	})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}

	if prepared.Env["FORGE_WORKER_NAME"] != "mon" {
		t.Errorf("FORGE_WORKER_NAME = %q, want %q", prepared.Env["FORGE_WORKER_NAME"], "mon")
	}
}

func TestClaudeCodeDriver_StartSpec_ReturnsClaudeCommand(t *testing.T) {
	driver := &ClaudeCodeDriver{}

	spec, err := driver.StartSpec(context.Background(), &PreparedEngine{
		Env: map[string]string{},
	})
	if err != nil {
		t.Fatalf("StartSpec: %v", err)
	}

	if len(spec.Command) == 0 {
		t.Fatal("expected non-empty command")
	}
	if spec.Command[0] != "claude" {
		t.Errorf("command[0] = %q, want %q", spec.Command[0], "claude")
	}

	hasPerms := false
	for _, arg := range spec.Command {
		if arg == "--dangerously-skip-permissions" || arg == "--permission-mode" {
			hasPerms = true
		}
	}
	if !hasPerms {
		t.Error("expected permission flag in command")
	}
}

func TestClaudeCodeDriver_StartSpec_WithAPIKeyHelper(t *testing.T) {
	driver := &ClaudeCodeDriver{}

	spec, err := driver.StartSpec(context.Background(), &PreparedEngine{
		Env: map[string]string{
			"FORGE_API_KEY_HELPER": "/tmp/test-helper.sh",
		},
	})
	if err != nil {
		t.Fatalf("StartSpec: %v", err)
	}

	hasSettings := false
	for _, arg := range spec.Command {
		if arg == "--settings" {
			hasSettings = true
		}
	}
	if !hasSettings {
		t.Error("expected --settings flag when API key helper is set")
	}
}

func TestClaudeCodeDriver_Prepare_RequiresConfigDir(t *testing.T) {
	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "test",
		Version: "1.0.0",
	}

	_, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars:     map[string]string{},
		GOOS:     "linux",
	})
	if err == nil {
		t.Fatal("expected error when CLAUDE_CONFIG_DIR missing")
	}
	if !strings.Contains(err.Error(), "CLAUDE_CONFIG_DIR") {
		t.Errorf("error = %q, want mention of CLAUDE_CONFIG_DIR", err.Error())
	}
}

func TestClaudeCodeDriver_Prepare_InstallsHookScripts(t *testing.T) {
	tmpDir := t.TempDir()
	configDir := filepath.Join(tmpDir, ".claude")

	hookContent := []byte("#!/bin/bash\necho hello")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "test-worker",
		Version: "1.0.0",
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: configDir + "/hooks/send-response.sh",
				Source:  "~/.claude/hooks/send-response.sh",
			},
		},
	}

	source := &mapFileSource{
		files: map[string][]byte{
			".claude/hooks/send-response.sh": hookContent,
		},
	}

	_, err := driver.Prepare(context.Background(), PrepareRequest{
		Manifest: manifest,
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
		},
		GOOS:   "linux",
		Source: source,
	})
	if err != nil {
		t.Fatalf("Prepare: %v", err)
	}

	hookPath := filepath.Join(configDir, "hooks", "send-response.sh")
	data, err := os.ReadFile(hookPath)
	if err != nil {
		t.Fatalf("hook not installed: %v", err)
	}
	if string(data) != string(hookContent) {
		t.Errorf("hook content = %q, want %q", string(data), string(hookContent))
	}

	info, _ := os.Stat(hookPath)
	if info.Mode()&0o111 == 0 {
		t.Error("hook script is not executable")
	}
}

func TestGenerateEmitScript_ContainsSocketPath(t *testing.T) {
	t.Parallel()
	script := GenerateEmitScript("mon")
	if !strings.Contains(script, "/tmp/forge-mon.sock") {
		t.Error("script missing socket path")
	}
	if !strings.Contains(script, "#!/bin/bash") {
		t.Error("script missing shebang")
	}
	if !strings.Contains(script, "curl") {
		t.Error("script missing curl call")
	}
}

func TestClaudeCodeDriver_GeneratesEmitHooksForSourcelessEntries(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "testworker",
		Version: "1.0.0",
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/stop-emit.sh",
			},
		},
	}

	err := driver.generateEmitHooks(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}, configDir)
	if err != nil {
		t.Fatalf("generateEmitHooks: %v", err)
	}

	hookPath := filepath.Join(configDir, "hooks", "stop-emit.sh")
	data, err := os.ReadFile(hookPath)
	if err != nil {
		t.Fatalf("hook not written: %v", err)
	}
	if !strings.Contains(string(data), "/tmp/forge-testworker.sock") {
		t.Error("generated hook missing worker socket path")
	}

	info, err := os.Stat(hookPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&0o111 == 0 {
		t.Error("generated hook not executable")
	}

	if !manifest.Hooks[0].Generated {
		t.Error("hook not marked as Generated")
	}
}

func TestClaudeCodeDriver_SkipsEmitForHooksWithSource(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	driver := &ClaudeCodeDriver{}
	manifest := &Manifest{
		Name:    "testworker",
		Version: "1.0.0",
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	}

	err := driver.generateEmitHooks(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}, configDir)
	if err != nil {
		t.Fatalf("generateEmitHooks: %v", err)
	}

	hookPath := filepath.Join(configDir, "hooks", "send-to-telegram.sh")
	if _, err := os.Stat(hookPath); err == nil {
		t.Error("expected no file for hook with Source — should be skipped")
	}

	if manifest.Hooks[0].Generated {
		t.Error("hook with Source should not be marked Generated")
	}
}

type mapFileSource struct {
	files map[string][]byte
}

func (m *mapFileSource) ReadFile(name string) ([]byte, error) {
	data, ok := m.files[name]
	if !ok {
		return nil, os.ErrNotExist
	}
	return data, nil
}
