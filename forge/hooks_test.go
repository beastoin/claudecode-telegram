package forge

import (
	"os"
	"path/filepath"
	"testing"
)

func TestInstallHooks_ExtractsHookSourceFiles(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	manager := HookManager{
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		},
	}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	}

	if err := manager.Install(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}); err != nil {
		t.Fatalf("HookManager.Install() error = %v", err)
	}

	hookPath := filepath.Join(configDir, "hooks", "send-to-telegram.sh")
	data, err := os.ReadFile(hookPath)
	if err != nil {
		t.Fatalf("os.ReadFile(%q) error = %v", hookPath, err)
	}
	if got := string(data); got != "#!/bin/sh\necho sent\n" {
		t.Fatalf("hook file = %q, want extracted source", got)
	}
}

func TestInstallHooks_PatchesSettingsAfterExtractingSources(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	manager := HookManager{
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		},
	}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	}

	if err := manager.Install(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}); err != nil {
		t.Fatalf("HookManager.Install() error = %v", err)
	}

	if _, err := os.Stat(filepath.Join(configDir, "hooks", "send-to-telegram.sh")); err != nil {
		t.Fatalf("expected extracted hook source, stat error = %v", err)
	}

	var settings map[string]any
	readJSONFile(t, filepath.Join(configDir, "settings.json"), &settings)
	hooks := settings["hooks"].(map[string]any)
	stopHooks := hooks["Stop"].([]any)
	if len(stopHooks) != 1 {
		t.Fatalf("Stop hooks len = %d, want 1", len(stopHooks))
	}

	group := stopHooks[0].(map[string]any)
	entries := group["hooks"].([]any)
	if len(entries) != 1 {
		t.Fatalf("hook entries len = %d, want 1", len(entries))
	}
	entry := entries[0].(map[string]any)
	if got := entry["command"]; got != filepath.Join(configDir, "hooks", "send-to-telegram.sh") {
		t.Fatalf("hook command = %#v, want extracted hook path", got)
	}
}

func TestInstallHooks_ErrorsWhenHookSourceMissing(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	manager := HookManager{Source: MapFileSource{}}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	}

	err := manager.Install(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	})
	if err == nil {
		t.Fatal("HookManager.Install() error = nil, want missing source error")
	}
}

func TestHookManager_WritesHooks0755AndSettings0600(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	manager := HookManager{
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		},
	}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	}

	if err := manager.Install(manifest, map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}); err != nil {
		t.Fatalf("HookManager.Install() error = %v", err)
	}

	hookInfo, err := os.Stat(filepath.Join(configDir, "hooks", "send-to-telegram.sh"))
	if err != nil {
		t.Fatalf("os.Stat(hook) error = %v", err)
	}
	if got := hookInfo.Mode().Perm(); got != 0o755 {
		t.Fatalf("hook mode = %#o, want %#o", got, 0o755)
	}

	settingsInfo, err := os.Stat(filepath.Join(configDir, "settings.json"))
	if err != nil {
		t.Fatalf("os.Stat(settings.json) error = %v", err)
	}
	if got := settingsInfo.Mode().Perm(); got != 0o600 {
		t.Fatalf("settings.json mode = %#o, want %#o", got, 0o600)
	}
}
