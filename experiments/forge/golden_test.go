package forge

import (
	"os"
	"path/filepath"
	"testing"
)

func assertGolden(t *testing.T, name string, got string) {
	t.Helper()
	path := filepath.Join("testdata", name)
	if os.Getenv("UPDATE_GOLDEN") == "1" {
		os.MkdirAll(filepath.Dir(path), 0o755)
		if err := os.WriteFile(path, []byte(got), 0o644); err != nil {
			t.Fatalf("update golden: %v", err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden %q: %v\nHint: run with UPDATE_GOLDEN=1 to create", path, err)
	}
	if string(want) != got {
		t.Fatalf("golden mismatch for %s:\n--- want ---\n%s\n--- got ---\n%s", name, string(want), got)
	}
}

func TestGolden_FormatCheckReport(t *testing.T) {
	report := CheckReport{
		Worker:  "testmon",
		Version: "1.0.0",
		ResolvedVars: map[string]string{
			"HOME":             "/home/test",
			"CLAUDE_CONFIG_DIR": "/home/test/.claude",
			"BRIDGE_URL":       "http://localhost:8271",
		},
		Tools: []ToolStatus{
			{Name: "tmux", Installed: true, Version: "3.4", Required: true},
			{Name: "claude", Installed: true, Version: "2.1.114", Required: true},
			{Name: "codex", Installed: false, Required: false},
		},
		Readiness: []ReadinessStatus{
			{Name: "bridge", Passed: true, Required: true},
			{Name: "gcloud-auth", Passed: false, Required: true, Output: "credentials expired"},
		},
	}
	got := FormatCheckReport(report, "linux")
	assertGolden(t, "check_report.txt", got)
}

func TestGolden_FormatCheckJSON(t *testing.T) {
	report := CheckReport{
		Worker:  "testmon",
		Version: "1.0.0",
		Tools: []ToolStatus{
			{Name: "tmux", Installed: true, Version: "3.4", Required: true},
			{Name: "codex", Installed: false, Required: false},
		},
		Readiness: []ReadinessStatus{
			{Name: "bridge", Passed: true, Required: true},
		},
	}
	got := FormatCheckJSON(report)
	assertGolden(t, "check_report.json", got)
}

func TestGolden_FormatVerifyReport(t *testing.T) {
	report := VerifyReport{
		Worker:   "testmon",
		Version:  "1.0.0",
		Verified: []string{"team/test/charter.md", "hooks/send-to-telegram.sh"},
		Skipped:  []string{"memory/MEMORY.md"},
	}
	got := FormatVerifyReport(report)
	assertGolden(t, "verify_report.txt", got)
}

func TestGolden_FormatVerifyJSON(t *testing.T) {
	report := VerifyReport{
		Worker:   "testmon",
		Version:  "1.0.0",
		Verified: []string{"charter.md"},
		Skipped:  []string{"kanban.md"},
	}
	got := FormatVerifyJSON(report)
	assertGolden(t, "verify_report.json", got)
}

func TestGolden_FormatDescribe_Full(t *testing.T) {
	manifest := &Manifest{Name: "testmon", Version: "1.0.0"}
	got := FormatDescribe(manifest, "")
	assertGolden(t, "describe_full.json", got)
}

func TestGolden_FormatDescribe_SingleCommand(t *testing.T) {
	manifest := &Manifest{Name: "testmon", Version: "1.0.0"}
	got := FormatDescribe(manifest, "check")
	assertGolden(t, "describe_check.json", got)
}

func TestGolden_FormatDescribe_UnknownCommand(t *testing.T) {
	manifest := &Manifest{Name: "testmon", Version: "1.0.0"}
	got := FormatDescribe(manifest, "nonexistent")
	assertGolden(t, "describe_unknown.json", got)
}
