package forge

import (
	"os"
	"path/filepath"
	"testing"
)

func TestIntegrityMonitor_VerifiesHooksCredsAndSettingsHookEntries(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	hookPath := filepath.Join(configDir, "hooks", "send-to-telegram.sh")
	credsPath := filepath.Join(root, ".config", "agent.env")

	if err := os.MkdirAll(filepath.Dir(hookPath), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(hook) error = %v", err)
	}
	if err := os.MkdirAll(filepath.Dir(credsPath), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(creds) error = %v", err)
	}
	if err := os.WriteFile(hookPath, []byte("#!/bin/sh\necho sent\n"), 0o755); err != nil {
		t.Fatalf("os.WriteFile(hook) error = %v", err)
	}
	if err := os.WriteFile(credsPath, []byte("TOKEN=secret\n"), 0o600); err != nil {
		t.Fatalf("os.WriteFile(creds) error = %v", err)
	}

	writeJSONFile(t, filepath.Join(configDir, "settings.json"), map[string]any{
		"hooks": map[string]any{
			"Stop": []any{
				map[string]any{"command": filepath.Join(configDir, "hooks", "send-to-telegram.sh")},
			},
		},
	})

	monitor := &FileIntegrityMonitor{
		Manifest: &Manifest{
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
			},
			Files: []FileSpec{
				{
					Source:    "secrets/agent.env",
					Dest:      "$HOME/.config/agent.env",
					Encrypted: true,
				},
			},
			Hooks: []HookSpec{
				{
					Event:   "Stop",
					Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
					Source:  "hooks/send-to-telegram.sh",
				},
			},
		},
		Vars: map[string]string{
			"HOME":              root,
			"CLAUDE_CONFIG_DIR": configDir,
		},
		Verifier: NewChecksumVerifier(checksumMap(t, map[string][]byte{
			"files/hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
			"creds/secrets/agent.env":         []byte("TOKEN=secret\n"),
		})),
	}

	if err := monitor.VerifyCritical(); err != nil {
		t.Fatalf("VerifyCritical() error = %v", err)
	}
}

func TestIntegrityMonitor_SkipsMergeFilesAndIntegritySkipFiles(t *testing.T) {
	t.Parallel()

	root := t.TempDir()

	monitor := &FileIntegrityMonitor{
		Manifest: &Manifest{
			Vars: map[string]VarSpec{
				"HOME": {
					Source:   "env",
					Required: true,
				},
			},
			Files: []FileSpec{
				{
					Source: "memory/state.md",
					Dest:   "$HOME/team/mon/state.md",
					Merge:  true,
				},
				{
					Source:    "runtime/cache.json",
					Dest:      "$HOME/team/mon/cache.json",
					Integrity: "skip",
				},
			},
		},
		Vars: map[string]string{
			"HOME": root,
		},
		Verifier: NewChecksumVerifier(map[string]string{}),
	}

	if err := monitor.VerifyCritical(); err != nil {
		t.Fatalf("VerifyCritical() error = %v", err)
	}
}

func TestIntegrityMonitor_DetectsRemovedHookRegistration(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	hookPath := filepath.Join(configDir, "hooks", "send-to-telegram.sh")

	if err := os.MkdirAll(filepath.Dir(hookPath), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.WriteFile(hookPath, []byte("#!/bin/sh\necho sent\n"), 0o755); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}
	writeJSONFile(t, filepath.Join(configDir, "settings.json"), map[string]any{
		"hooks": map[string]any{},
	})

	monitor := &FileIntegrityMonitor{
		Manifest: &Manifest{
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
			},
			Hooks: []HookSpec{
				{
					Event:   "Stop",
					Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
					Source:  "hooks/send-to-telegram.sh",
				},
			},
		},
		Vars: map[string]string{
			"HOME":              root,
			"CLAUDE_CONFIG_DIR": configDir,
		},
		Verifier: NewChecksumVerifier(checksumMap(t, map[string][]byte{
			"files/hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		})),
	}

	if err := monitor.VerifyCritical(); err == nil {
		t.Fatal("VerifyCritical() error = nil, want missing hook registration error")
	}
}
