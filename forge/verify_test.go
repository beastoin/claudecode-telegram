package forge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestRunVerifyMode_VerifiesExtractedFiles(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "team", "mon", "charter.md")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("charter"), 0o644); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}

	report, err := RunVerifyMode(&Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
		},
		Files: []FileSpec{
			{
				Source: "knowledge/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
			{
				Source:    "memory/state.md",
				Dest:      "$HOME/team/mon/state.md",
				Integrity: "skip",
			},
		},
	}, VerifyOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
		},
		Checksums: checksumMap(t, map[string][]byte{
			"files/knowledge/charter.md": []byte("charter"),
		}),
	})
	if err != nil {
		t.Fatalf("RunVerifyMode() error = %v", err)
	}

	if len(report.Verified) != 1 || report.Verified[0] != "files/knowledge/charter.md" {
		t.Fatalf("report.Verified = %#v, want files/knowledge/charter.md", report.Verified)
	}
	if len(report.Skipped) != 1 || report.Skipped[0] != "files/memory/state.md" {
		t.Fatalf("report.Skipped = %#v, want files/memory/state.md", report.Skipped)
	}
}

func TestRunVerifyMode_VerifiesHookFilesAndSkipsRuntimeMutableFiles(t *testing.T) {
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

	report, err := RunVerifyMode(&Manifest{
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
		},
		Files: []FileSpec{
			{
				Source:    "memory/state.md",
				Dest:      "$HOME/team/mon/state.md",
				Integrity: "skip",
			},
		},
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	}, VerifyOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
		},
		Checksums: checksumMap(t, map[string][]byte{
			"files/hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		}),
	})
	if err != nil {
		t.Fatalf("RunVerifyMode() error = %v", err)
	}

	if len(report.Verified) != 1 || report.Verified[0] != "files/hooks/send-to-telegram.sh" {
		t.Fatalf("report.Verified = %#v, want files/hooks/send-to-telegram.sh", report.Verified)
	}
	if len(report.Skipped) != 1 || report.Skipped[0] != "files/memory/state.md" {
		t.Fatalf("report.Skipped = %#v, want files/memory/state.md", report.Skipped)
	}
}

func checksumMap(t *testing.T, files map[string][]byte) map[string]string {
	t.Helper()

	data, err := GenerateChecksumsJSON(files)
	if err != nil {
		t.Fatalf("GenerateChecksumsJSON() error = %v", err)
	}

	var checksums map[string]string
	if err := json.Unmarshal(data, &checksums); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	return checksums
}
