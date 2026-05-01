package forge

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestExtract_WritesNewFile(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "team", "mon", "charter.md")
	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "files/charter.md", Dest: "$HOME/team/mon/charter.md"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"files/charter.md": []byte("charter")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, dest, "charter")
}

func TestExtract_ConflictStopsOnDiff(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	writeTestFile(t, dest, "existing")
	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "hooks/send-to-telegram.sh", Dest: "$HOME/hooks/send-to-telegram.sh"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"hooks/send-to-telegram.sh": []byte("embedded")},
	})

	var conflictErr *ExtractConflictError
	if !errors.As(err, &conflictErr) {
		t.Fatalf("Extract() error = %T %v, want *ExtractConflictError", err, err)
	}
	if len(conflictErr.Conflicts) != 1 {
		t.Fatalf("len(conflicts) = %d, want 1", len(conflictErr.Conflicts))
	}
	if conflictErr.Conflicts[0].Path != dest {
		t.Fatalf("conflict path = %q, want %q", conflictErr.Conflicts[0].Path, dest)
	}
	assertFileContent(t, dest, "existing")
}

func TestExtract_MergeKeepsExisting(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "memory", "state.md")
	writeTestFile(t, dest, "local state")
	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "memory/state.md", Dest: "$HOME/memory/state.md", Merge: true},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"memory/state.md": []byte("embedded state")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, dest, "local state")
}

func TestExtract_OverwriteAlways(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "team", "mon", "charter.md")
	writeTestFile(t, dest, "old")
	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "files/charter.md", Dest: "$HOME/team/mon/charter.md", Overwrite: "always"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: MapFileSource{"files/charter.md": []byte("new")},
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, dest, "new")
}

func TestExtract_ForceExtractOverrides(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	dest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	writeTestFile(t, dest, "existing")
	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "hooks/send-to-telegram.sh", Dest: "$HOME/hooks/send-to-telegram.sh"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:         map[string]string{"HOME": root},
		Source:       MapFileSource{"hooks/send-to-telegram.sh": []byte("embedded")},
		ForceExtract: true,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, dest, "embedded")
}

func TestExtract_SkipConflicts(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	conflictDest := filepath.Join(root, "hooks", "send-to-telegram.sh")
	newDest := filepath.Join(root, "team", "mon", "charter.md")
	writeTestFile(t, conflictDest, "existing")
	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "hooks/send-to-telegram.sh", Dest: "$HOME/hooks/send-to-telegram.sh"},
			{Source: "files/charter.md", Dest: "$HOME/team/mon/charter.md"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars: map[string]string{"HOME": root},
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("embedded"),
			"files/charter.md":          []byte("charter"),
		},
		SkipConflicts: true,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, conflictDest, "existing")
	assertFileContent(t, newDest, "charter")
}

func writeTestFile(t *testing.T, path string, content string) {
	t.Helper()

	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(%q) error = %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("os.WriteFile(%q) error = %v", path, err)
	}
}
