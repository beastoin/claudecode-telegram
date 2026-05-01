package forge

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCollector_GathersHookSourcesReferencedOnlyByManifestHooks(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifestPath := filepath.Join(root, "manifest.yaml")
	hookPath := filepath.Join(root, "hooks", "send-to-telegram.sh")

	if err := os.MkdirAll(filepath.Dir(hookPath), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.WriteFile(manifestPath, []byte("name: mon\nversion: 1.0.0\n"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}
	if err := os.WriteFile(hookPath, []byte("#!/bin/sh\necho sent\n"), 0o755); err != nil {
		t.Fatalf("os.WriteFile(hook) error = %v", err)
	}

	collected, err := CollectManifestFiles(manifestPath, &Manifest{
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
				Source:  "hooks/send-to-telegram.sh",
			},
		},
	})
	if err != nil {
		t.Fatalf("CollectManifestFiles() error = %v", err)
	}

	if got := string(collected["files/hooks/send-to-telegram.sh"]); got != "#!/bin/sh\necho sent\n" {
		t.Fatalf("collected hook source = %q, want extracted hook content", got)
	}
}
