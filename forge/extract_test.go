package forge

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"testing"

	"filippo.io/age"
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

func TestExtract_DirectorySource(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source:     "~/.claude/skills/my-skill/",
				Dest:       "$HOME/.claude/skills/my-skill/",
				ContentKey: ".claude/skills/my-skill",
			},
		},
	}

	source := MapFileSource{
		".claude/skills/my-skill/SKILL.md":   []byte("# My Skill"),
		".claude/skills/my-skill/helpers.sh":  []byte("#!/bin/bash"),
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: source,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, filepath.Join(root, ".claude/skills/my-skill/SKILL.md"), "# My Skill")
	assertFileContent(t, filepath.Join(root, ".claude/skills/my-skill/helpers.sh"), "#!/bin/bash")
}

func TestExtract_DirectorySourceWithMerge(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	existingPath := filepath.Join(root, ".claude/memory/state.md")
	writeTestFile(t, existingPath, "local state")

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source:     "~/.claude/memory/",
				Dest:       "$HOME/.claude/memory/",
				ContentKey: ".claude/memory",
				Merge:      true,
			},
		},
	}

	source := MapFileSource{
		".claude/memory/state.md":  []byte("embedded state"),
		".claude/memory/index.md":  []byte("new index"),
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: source,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, existingPath, "local state")
	assertFileContent(t, filepath.Join(root, ".claude/memory/index.md"), "new index")
}

func TestExpandDirSources_MixedFileAndDir(t *testing.T) {
	t.Parallel()

	source := MapFileSource{
		"charter.md":                          []byte("charter"),
		".claude/skills/my-skill/SKILL.md":    []byte("skill"),
		".claude/skills/my-skill/helpers.sh":   []byte("helpers"),
	}

	files := []FileSpec{
		{Source: "charter.md", ContentKey: "charter.md", Dest: "$HOME/charter.md"},
		{Source: "~/.claude/skills/my-skill/", ContentKey: ".claude/skills/my-skill", Dest: "$HOME/.claude/skills/my-skill/"},
	}

	expanded := ExpandDirSources(files, source)
	if len(expanded) != 3 {
		t.Fatalf("ExpandDirSources() len = %d, want 3", len(expanded))
	}

	if expanded[0].ContentKey != "charter.md" {
		t.Errorf("expanded[0].ContentKey = %q, want %q", expanded[0].ContentKey, "charter.md")
	}
	if expanded[1].ContentKey != ".claude/skills/my-skill/SKILL.md" {
		t.Errorf("expanded[1].ContentKey = %q, want %q", expanded[1].ContentKey, ".claude/skills/my-skill/SKILL.md")
	}
	if expanded[2].ContentKey != ".claude/skills/my-skill/helpers.sh" {
		t.Errorf("expanded[2].ContentKey = %q, want %q", expanded[2].ContentKey, ".claude/skills/my-skill/helpers.sh")
	}
}

func TestIntegration_BuildDirSource_ThenExtract(t *testing.T) {
	t.Parallel()

	buildRoot := t.TempDir()
	runtimeRoot := t.TempDir()

	skillDir := filepath.Join(buildRoot, "skills", "my-skill")
	subDir := filepath.Join(skillDir, "templates")
	os.MkdirAll(subDir, 0o755)
	os.WriteFile(filepath.Join(skillDir, "SKILL.md"), []byte("# My Skill"), 0o644)
	os.WriteFile(filepath.Join(skillDir, "helpers.sh"), []byte("#!/bin/bash\necho ok"), 0o644)
	os.WriteFile(filepath.Join(subDir, "email.html"), []byte("<html>hi</html>"), 0o644)

	manifestPath := filepath.Join(buildRoot, "manifest.yaml")
	os.WriteFile(manifestPath, []byte(`
name: test
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
files:
  - source: skills/my-skill/
    dest: $HOME/.claude/skills/my-skill/
`), 0o644)

	manifest, err := ParseManifest([]byte(`
name: test
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
files:
  - source: skills/my-skill/
    dest: $HOME/.claude/skills/my-skill/
`))
	if err != nil {
		t.Fatalf("ParseManifest() error = %v", err)
	}

	collected, err := CollectManifestFiles(manifestPath, manifest)
	if err != nil {
		t.Fatalf("CollectManifestFiles() error = %v", err)
	}
	if len(collected) != 3 {
		t.Fatalf("CollectManifestFiles() collected %d files, want 3", len(collected))
	}

	embedDir := filepath.Join(buildRoot, "embed")
	err = WriteEmbedLayout(embedDir, EmbedLayout{
		Manifest:      []byte("test"),
		Files:         collected,
		ChecksumsJSON: []byte("{}"),
	})
	if err != nil {
		t.Fatalf("WriteEmbedLayout() error = %v", err)
	}

	filesDir := filepath.Join(embedDir, "files")
	filesFS := os.DirFS(filesDir)
	source := FSFileSource{FS: filesFS}

	for i := range manifest.Files {
		manifest.Files[i].ContentKey = canonicalSource(manifest.Files[i].Source)
	}

	err = Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": runtimeRoot},
		Source: source,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, filepath.Join(runtimeRoot, ".claude/skills/my-skill/SKILL.md"), "# My Skill")
	assertFileContent(t, filepath.Join(runtimeRoot, ".claude/skills/my-skill/helpers.sh"), "#!/bin/bash\necho ok")
	assertFileContent(t, filepath.Join(runtimeRoot, ".claude/skills/my-skill/templates/email.html"), "<html>hi</html>")
}

func TestIntegration_ExtractEncryptedCreds_RealAge(t *testing.T) {
	t.Parallel()

	runtimeRoot := t.TempDir()

	identity, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatalf("age.GenerateX25519Identity() error = %v", err)
	}

	credsFiles := map[string][]byte{
		"creds/config/monitor.env": []byte("STRIPE_KEY=sk_test_123\nMIXPANEL_TOKEN=mp_abc"),
		"creds/__vars__.json":      []byte(`{"API_KEY":"secret-api-key"}`),
	}

	encryptor := AgeCredsEncryptor{}
	ciphertext, err := encryptor.Encrypt(credsFiles, identity.Recipient())
	if err != nil {
		t.Fatalf("Encrypt() error = %v", err)
	}
	if len(ciphertext) < 100 {
		t.Fatal("ciphertext too short — may not be encrypted")
	}

	manifest := &Manifest{
		Name:    "test",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {Source: "env", Required: true},
		},
		Files: []FileSpec{
			{
				Source:     "config/monitor.env",
				Dest:       "$HOME/.config/monitor.env",
				Encrypted:  true,
				ContentKey: "creds/config/monitor.env",
			},
		},
	}

	source := FSFileSource{CredsEncrypted: ciphertext}
	decryptor := AgeBundleDecryptor{Identities: []age.Identity{identity}}

	err = Extract(manifest, ExtractOptions{
		Vars:      map[string]string{"HOME": runtimeRoot},
		Source:    source,
		Decryptor: &decryptor,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, filepath.Join(runtimeRoot, ".config/monitor.env"), "STRIPE_KEY=sk_test_123\nMIXPANEL_TOKEN=mp_abc")
}

func TestIntegration_ConflictDetection_ForceAndSkip(t *testing.T) {
	t.Parallel()

	source := MapFileSource{
		"hooks/hook.sh": []byte("new-hook"),
		"hooks/util.sh": []byte("new-util"),
		"charter.md":    []byte("charter"),
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "hooks/hook.sh", Dest: "$HOME/hooks/hook.sh"},
			{Source: "hooks/util.sh", Dest: "$HOME/hooks/util.sh"},
			{Source: "charter.md", Dest: "$HOME/charter.md"},
		},
	}

	t.Run("default stops on conflict", func(t *testing.T) {
		root := t.TempDir()
		writeTestFile(t, filepath.Join(root, "hooks/hook.sh"), "old-hook")
		writeTestFile(t, filepath.Join(root, "hooks/util.sh"), "old-util")

		err := Extract(manifest, ExtractOptions{
			Vars:   map[string]string{"HOME": root},
			Source: source,
		})
		var conflictErr *ExtractConflictError
		if !errors.As(err, &conflictErr) {
			t.Fatalf("Extract() error = %T, want *ExtractConflictError", err)
		}
		if len(conflictErr.Conflicts) != 2 {
			t.Fatalf("conflicts = %d, want 2", len(conflictErr.Conflicts))
		}
		assertFileContent(t, filepath.Join(root, "hooks/hook.sh"), "old-hook")
	})

	t.Run("force-extract overrides all", func(t *testing.T) {
		root := t.TempDir()
		writeTestFile(t, filepath.Join(root, "hooks/hook.sh"), "old-hook")
		writeTestFile(t, filepath.Join(root, "hooks/util.sh"), "old-util")

		err := Extract(manifest, ExtractOptions{
			Vars:         map[string]string{"HOME": root},
			Source:       source,
			ForceExtract: true,
		})
		if err != nil {
			t.Fatalf("Extract(force) error = %v", err)
		}
		assertFileContent(t, filepath.Join(root, "hooks/hook.sh"), "new-hook")
		assertFileContent(t, filepath.Join(root, "hooks/util.sh"), "new-util")
		assertFileContent(t, filepath.Join(root, "charter.md"), "charter")
	})

	t.Run("skip-conflicts keeps originals", func(t *testing.T) {
		root := t.TempDir()
		writeTestFile(t, filepath.Join(root, "hooks/hook.sh"), "old-hook")

		err := Extract(manifest, ExtractOptions{
			Vars:          map[string]string{"HOME": root},
			Source:        source,
			SkipConflicts: true,
		})
		if err != nil {
			t.Fatalf("Extract(skip) error = %v", err)
		}
		assertFileContent(t, filepath.Join(root, "hooks/hook.sh"), "old-hook")
		assertFileContent(t, filepath.Join(root, "charter.md"), "charter")
	})
}

func TestIntegration_DirSourceNestedSubdirs(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	source := MapFileSource{
		".claude/skills/my-skill/SKILL.md":            []byte("skill"),
		".claude/skills/my-skill/lib/utils.sh":        []byte("utils"),
		".claude/skills/my-skill/lib/data/config.json": []byte(`{"key":"val"}`),
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source:     "~/.claude/skills/my-skill/",
				Dest:       "$HOME/.claude/skills/my-skill/",
				ContentKey: ".claude/skills/my-skill",
			},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: source,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, filepath.Join(root, ".claude/skills/my-skill/SKILL.md"), "skill")
	assertFileContent(t, filepath.Join(root, ".claude/skills/my-skill/lib/utils.sh"), "utils")
	assertFileContent(t, filepath.Join(root, ".claude/skills/my-skill/lib/data/config.json"), `{"key":"val"}`)
}

func TestIntegration_DirSourceEmpty(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	source := MapFileSource{
		"charter.md": []byte("charter"),
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{
				Source:     "~/.claude/skills/empty-skill/",
				Dest:       "$HOME/.claude/skills/empty-skill/",
				ContentKey: ".claude/skills/empty-skill",
			},
			{Source: "charter.md", Dest: "$HOME/charter.md"},
		},
	}

	err := Extract(manifest, ExtractOptions{
		Vars:   map[string]string{"HOME": root},
		Source: source,
	})
	if err != nil {
		t.Fatalf("Extract() error = %v", err)
	}

	assertFileContent(t, filepath.Join(root, "charter.md"), "charter")
}

func TestIntegration_BuildDirSource_ChecksumsIncludeAllFiles(t *testing.T) {
	t.Parallel()

	buildRoot := t.TempDir()
	skillDir := filepath.Join(buildRoot, "skills", "test-skill")
	os.MkdirAll(skillDir, 0o755)
	os.WriteFile(filepath.Join(skillDir, "SKILL.md"), []byte("skill content"), 0o644)
	os.WriteFile(filepath.Join(skillDir, "helper.sh"), []byte("helper content"), 0o644)

	manifestPath := filepath.Join(buildRoot, "manifest.yaml")
	os.WriteFile(manifestPath, []byte(`
name: test
version: 1.0.0
vars:
  HOME: {source: env, required: true}
files:
  - source: skills/test-skill/
    dest: $HOME/skills/test-skill/
`), 0o644)
	manifest, _ := ParseManifest([]byte(`
name: test
version: 1.0.0
vars:
  HOME: {source: env, required: true}
files:
  - source: skills/test-skill/
    dest: $HOME/skills/test-skill/
`))

	collected, err := CollectManifestFiles(manifestPath, manifest)
	if err != nil {
		t.Fatalf("CollectManifestFiles() error = %v", err)
	}

	checksumsJSON, err := GenerateChecksumsJSON(collected)
	if err != nil {
		t.Fatalf("GenerateChecksumsJSON() error = %v", err)
	}

	var checksums map[string]string
	json.Unmarshal(checksumsJSON, &checksums)

	if _, ok := checksums["files/skills/test-skill/SKILL.md"]; !ok {
		t.Error("checksums missing files/skills/test-skill/SKILL.md")
	}
	if _, ok := checksums["files/skills/test-skill/helper.sh"]; !ok {
		t.Error("checksums missing files/skills/test-skill/helper.sh")
	}
}

// Silence unused import warnings — fs is used by TestIntegration_BuildDirSource_ThenExtract
var _ = fs.FS(nil)

func writeTestFile(t *testing.T, path string, content string) {
	t.Helper()

	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(%q) error = %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("os.WriteFile(%q) error = %v", path, err)
	}
}
