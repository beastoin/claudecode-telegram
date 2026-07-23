package forge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
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

func TestIntegration_SettingsMergePreservesExistingConfig(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")

	// Pre-create settings.json with existing hooks, MCP servers, and permissions.
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}

	existing := map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{
					"hooks": []any{
						map[string]any{
							"type":    "command",
							"command": "/existing/hook.sh",
						},
					},
				},
			},
		},
		"mcpServers": map[string]any{
			"existing-server": map[string]any{
				"command": "npx",
				"args":    []any{"-y", "some-mcp"},
			},
		},
		"permissions": map[string]any{
			"allow": []any{"Read", "Write"},
		},
	}

	data, err := json.MarshalIndent(existing, "", "  ")
	if err != nil {
		t.Fatalf("json.MarshalIndent() error = %v", err)
	}
	settingsPath := filepath.Join(configDir, "settings.json")
	if err := os.WriteFile(settingsPath, data, 0o600); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}

	source := MapFileSource{
		"hooks/test-hook.sh": []byte("#!/bin/sh\necho forge\n"),
	}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/test-hook.sh",
				Source:  "hooks/test-hook.sh",
			},
		},
	}
	vars := map[string]string{
		"CLAUDE_CONFIG_DIR": configDir,
	}

	// First install.
	manager := HookManager{Source: source}
	if err := manager.Install(manifest, vars); err != nil {
		t.Fatalf("HookManager.Install() error = %v", err)
	}

	// Read resulting settings.
	var settings map[string]any
	readJSONFile(t, settingsPath, &settings)

	// Assert hooks section.
	hooks := settings["hooks"].(map[string]any)
	stopHooks := hooks["Stop"].([]any)
	if len(stopHooks) != 1 {
		t.Fatalf("Stop hook groups = %d, want 1 (merged into single group)", len(stopHooks))
	}

	group := stopHooks[0].(map[string]any)
	entries := group["hooks"].([]any)
	if len(entries) != 2 {
		t.Fatalf("hook entries = %d, want 2 (existing + forge)", len(entries))
	}

	// Existing hook preserved.
	existingEntry := entries[0].(map[string]any)
	if got := existingEntry["command"]; got != "/existing/hook.sh" {
		t.Fatalf("existing hook command = %v, want /existing/hook.sh", got)
	}

	// Forge hook added.
	forgeEntry := entries[1].(map[string]any)
	wantForgeCmd := filepath.Join(configDir, "hooks", "test-hook.sh")
	if got := forgeEntry["command"]; got != wantForgeCmd {
		t.Fatalf("forge hook command = %v, want %v", got, wantForgeCmd)
	}

	// Assert MCP servers preserved.
	mcpServers, ok := settings["mcpServers"].(map[string]any)
	if !ok {
		t.Fatal("mcpServers missing from settings")
	}
	server, ok := mcpServers["existing-server"].(map[string]any)
	if !ok {
		t.Fatal("existing-server missing from mcpServers")
	}
	if got := server["command"]; got != "npx" {
		t.Fatalf("mcpServers[existing-server].command = %v, want npx", got)
	}

	// Assert permissions preserved.
	permissions, ok := settings["permissions"].(map[string]any)
	if !ok {
		t.Fatal("permissions missing from settings")
	}
	allow, ok := permissions["allow"].([]any)
	if !ok {
		t.Fatal("permissions.allow missing from settings")
	}
	if len(allow) != 2 {
		t.Fatalf("permissions.allow len = %d, want 2", len(allow))
	}
	if got := allow[0]; got != "Read" {
		t.Fatalf("permissions.allow[0] = %v, want Read", got)
	}
	if got := allow[1]; got != "Write" {
		t.Fatalf("permissions.allow[1] = %v, want Write", got)
	}

	// Idempotence: install again.
	if err := manager.Install(manifest, vars); err != nil {
		t.Fatalf("HookManager.Install() second call error = %v", err)
	}

	var settings2 map[string]any
	readJSONFile(t, settingsPath, &settings2)

	hooks2 := settings2["hooks"].(map[string]any)
	stopHooks2 := hooks2["Stop"].([]any)
	group2 := stopHooks2[0].(map[string]any)
	entries2 := group2["hooks"].([]any)
	if len(entries2) != 2 {
		t.Fatalf("after second install: hook entries = %d, want 2 (no duplication)", len(entries2))
	}

	// MCP servers still present after second install.
	mcpServers2, ok := settings2["mcpServers"].(map[string]any)
	if !ok {
		t.Fatal("mcpServers missing after second install")
	}
	if _, ok := mcpServers2["existing-server"]; !ok {
		t.Fatal("existing-server missing from mcpServers after second install")
	}

	// Permissions still present after second install.
	permissions2, ok := settings2["permissions"].(map[string]any)
	if !ok {
		t.Fatal("permissions missing after second install")
	}
	if _, ok := permissions2["allow"]; !ok {
		t.Fatal("permissions.allow missing after second install")
	}
}

func TestInstallSettings_NullHookEventsNotPropagated(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// Simulate settings.json with null hook events (the bug scenario).
	raw := []byte(`{
  "hooks": {
    "Stop": null,
    "SessionStart": null,
    "PostToolUseFailure": null
  },
  "model": "claude-opus-4-6"
}`)
	settingsPath := filepath.Join(configDir, "settings.json")
	if err := os.WriteFile(settingsPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}

	// Run InstallSettings with a manifest that has one Stop hook.
	manager := HookManager{}
	manifest := &Manifest{
		Hooks: []HookSpec{
			{Event: "Stop", Command: configDir + "/hooks/forge-hook.sh"},
		},
	}
	vars := map[string]string{"CLAUDE_CONFIG_DIR": configDir}

	if err := manager.InstallSettings(manifest, vars); err != nil {
		t.Fatalf("InstallSettings error: %v", err)
	}

	// Read back and verify no null values remain.
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatal(err)
	}

	if strings.Contains(string(data), "null") {
		t.Errorf("settings.json still contains null after InstallSettings:\n%s", data)
	}

	var settings map[string]any
	if err := json.Unmarshal(data, &settings); err != nil {
		t.Fatal(err)
	}

	hooks := settings["hooks"].(map[string]any)

	// Stop should have the forge hook (not null).
	stopEntries, ok := hooks["Stop"].([]any)
	if !ok || stopEntries == nil {
		t.Fatalf("Stop is nil or wrong type, got %T: %v", hooks["Stop"], hooks["Stop"])
	}
	if len(stopEntries) == 0 {
		t.Fatal("Stop has zero entries, expected forge hook")
	}

	// SessionStart and PostToolUseFailure had null + no manifest hook.
	// They should be removed entirely, not written as null.
	for _, event := range []string{"SessionStart", "PostToolUseFailure"} {
		val, exists := hooks[event]
		if exists && val == nil {
			t.Errorf("hooks[%q] is null — should be removed or empty array", event)
		}
	}
}

func TestPruneStaleHooks_ReturnsEmptySliceNotNil(t *testing.T) {
	t.Parallel()

	// When all entries are pruned, result should be [] not nil (avoids JSON null).
	entries := []any{
		map[string]any{
			"hooks": []any{
				map[string]any{
					"type":    "command",
					"command": "/other-node/.claude/hooks/stale-hook.sh",
				},
			},
		},
	}

	result := PruneStaleHooks(entries, "/home/claude/.claude")
	if result == nil {
		t.Fatal("PruneStaleHooks returned nil, want empty slice (would serialize as JSON null)")
	}

	data, _ := json.Marshal(result)
	if string(data) == "null" {
		t.Errorf("json.Marshal(result) = %q, want []", string(data))
	}
}
