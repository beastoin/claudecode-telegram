package forge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
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

func TestIntegration_VerifyModeDetectsTamper(t *testing.T) {
	t.Parallel()

	root := t.TempDir()

	manifestYAML := []byte(`
name: tamper-test
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
files:
  - source: knowledge/charter.md
    dest: $HOME/team/tamper-test/charter.md
`)
	manifest, err := ParseManifest(manifestYAML)
	if err != nil {
		t.Fatalf("ParseManifest() error = %v", err)
	}

	source := MapFileSource{
		"knowledge/charter.md": []byte("charter content"),
	}

	checksums := checksumMap(t, map[string][]byte{
		"files/knowledge/charter.md": []byte("charter content"),
		"knowledge/charter.md":       []byte("charter content"),
	})

	// Extract the file with checksum verification.
	err = Extract(manifest, ExtractOptions{
		Vars:     map[string]string{"HOME": root},
		Source:   source,
		Verifier: NewChecksumVerifier(checksums),
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	dest := filepath.Join(root, "team", "tamper-test", "charter.md")
	assertFileContent(t, dest, "charter content")

	// Verify passes on the untampered file.
	_, err = RunVerifyMode(manifest, VerifyOptions{
		Resolve:   ResolveOptions{Env: map[string]string{"HOME": root}},
		Checksums: checksums,
	})
	if err != nil {
		t.Fatalf("RunVerifyMode() before tamper error = %v", err)
	}

	// Tamper the file on disk.
	if err := os.WriteFile(dest, []byte("tampered content"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(tamper) error = %v", err)
	}

	// Verify now detects the tamper.
	_, err = RunVerifyMode(manifest, VerifyOptions{
		Resolve:   ResolveOptions{Env: map[string]string{"HOME": root}},
		Checksums: checksums,
	})
	if err == nil {
		t.Fatal("RunVerifyMode() after tamper error = nil, want checksum mismatch")
	}
	if !strings.Contains(err.Error(), "checksum mismatch") {
		t.Fatalf("RunVerifyMode() after tamper error = %v, want checksum mismatch", err)
	}
}

func TestFormatVerifyJSON_StructuredOutput(t *testing.T) {
	t.Parallel()

	report := VerifyReport{
		Worker:   "mon",
		Version:  "2.0.0",
		Verified: []string{"files/knowledge/charter.md", "files/hooks/send-to-telegram.sh"},
		Skipped:  []string{"files/memory/state.md"},
	}

	out := FormatVerifyJSON(report)
	var result VerifyResultJSON
	if err := json.Unmarshal([]byte(out), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v\nraw: %s", err, out)
	}

	if result.Command != "verify" {
		t.Fatalf("result.Command = %q, want %q", result.Command, "verify")
	}
	if result.Worker != "mon" {
		t.Fatalf("result.Worker = %q, want %q", result.Worker, "mon")
	}
	if result.Status != "pass" {
		t.Fatalf("result.Status = %q, want %q", result.Status, "pass")
	}
	if result.Summary.Verified != 2 {
		t.Fatalf("result.Summary.Verified = %d, want 2", result.Summary.Verified)
	}
	if result.Summary.Skipped != 1 {
		t.Fatalf("result.Summary.Skipped = %d, want 1", result.Summary.Skipped)
	}
	if len(result.Files) != 3 {
		t.Fatalf("len(result.Files) = %d, want 3", len(result.Files))
	}

	// Check verified file
	found := false
	for _, f := range result.Files {
		if f.Path == "files/knowledge/charter.md" {
			found = true
			if f.Status != "pass" {
				t.Fatalf("charter.Status = %q, want pass", f.Status)
			}
		}
	}
	if !found {
		t.Fatal("missing file files/knowledge/charter.md")
	}

	// Check skipped file
	found = false
	for _, f := range result.Files {
		if f.Path == "files/memory/state.md" {
			found = true
			if f.Status != "skip" {
				t.Fatalf("state.Status = %q, want skip", f.Status)
			}
		}
	}
	if !found {
		t.Fatal("missing file files/memory/state.md")
	}
}

func TestRunVerifyMode_JSONOutput(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)

	dest := filepath.Join(root, "team", "mon", "charter.md")
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.WriteFile(dest, []byte("charter"), 0o644); err != nil {
		t.Fatalf("os.WriteFile() error = %v", err)
	}

	cksums := checksumMap(t, map[string][]byte{
		"files/knowledge/charter.md": []byte("charter"),
	})
	ckdata, _ := json.Marshal(cksums)

	var buf strings.Builder
	err := RunEmbeddedWorker(
		[]string{"verify", "--output-json", "--bridge-url", "http://bridge"},
		WorkerDeps{
			Assets: EmbeddedAssets{
				Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
files:
  - source: knowledge/charter.md
    dest: $HOME/team/mon/charter.md
`),
				Checksums: ckdata,
			},
			Stdout: &buf,
			Runner: &stubRunner{},
		},
	)
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	var result VerifyResultJSON
	if err := json.Unmarshal([]byte(buf.String()), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v\nraw: %s", err, buf.String())
	}
	if result.Command != "verify" {
		t.Fatalf("result.Command = %q, want verify", result.Command)
	}
	if result.Status != "pass" {
		t.Fatalf("result.Status = %q, want pass", result.Status)
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
