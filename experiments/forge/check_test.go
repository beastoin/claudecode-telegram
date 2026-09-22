package forge

import (
	"encoding/json"
	"testing"
)

func TestRunCheckMode_DoesNotRequirePreviouslyExtractedFiles(t *testing.T) {
	t.Parallel()

	report, err := RunCheckMode(&Manifest{
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
		},
	}, CheckOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": t.TempDir()},
		},
		GOOS:   "linux",
		Runner: &stubRunner{},
	})
	if err != nil {
		t.Fatalf("RunCheckMode() error = %v", err)
	}
	if report.Worker != "mon" {
		t.Fatalf("report.Worker = %q, want mon", report.Worker)
	}
}

func TestRunCheckMode_DoesNotInstallMissingRequiredTools(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V":             {ExitCode: 1},
			"apt install -y tmux": {ExitCode: 0},
		},
	}
	report, err := RunCheckMode(&Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
		},
		Tools: []ToolSpec{
			{
				Name:  "tmux",
				Check: "tmux -V",
				Install: map[string]string{
					"linux": "apt install -y tmux",
				},
				Required: true,
			},
		},
	}, CheckOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": t.TempDir()},
		},
		GOOS:   "linux",
		Runner: runner,
	})
	if err != nil {
		t.Fatalf("RunCheckMode() error = %v", err)
	}
	if len(report.Tools) != 1 || report.Tools[0].Installed {
		t.Fatalf("report.Tools = %#v, want missing required tool report", report.Tools)
	}
	if got := runner.commands; len(got) != 1 || got[0] != "tmux -V" {
		t.Fatalf("runner.commands = %#v, want only check command", got)
	}
}

func TestFormatCheckJSON_StructuredOutput(t *testing.T) {
	t.Parallel()

	report := CheckReport{
		Worker:  "mon",
		Version: "2.0.0",
		ResolvedVars: map[string]string{
			"HOME":       "/home/claude",
			"BRIDGE_URL": "http://bridge:8271",
		},
		Tools: []ToolStatus{
			{Name: "tmux", Installed: true, Required: true, Version: "3.4"},
			{Name: "codex", Installed: false, Required: false},
		},
		Readiness: []ReadinessStatus{
			{Name: "bridge-reachable", Passed: true, Required: true},
			{Name: "gcloud-auth", Passed: false, Required: true, Output: "not authenticated"},
		},
	}

	out := FormatCheckJSON(report)
	var result CheckResultJSON
	if err := json.Unmarshal([]byte(out), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v\nraw: %s", err, out)
	}

	if result.Command != "check" {
		t.Fatalf("result.Command = %q, want %q", result.Command, "check")
	}
	if result.Worker != "mon" {
		t.Fatalf("result.Worker = %q, want %q", result.Worker, "mon")
	}
	if result.Status != "fail" {
		t.Fatalf("result.Status = %q, want %q", result.Status, "fail")
	}
	if result.Summary.Passed != 2 {
		t.Fatalf("result.Summary.Passed = %d, want 2", result.Summary.Passed)
	}
	if result.Summary.Failed != 1 {
		t.Fatalf("result.Summary.Failed = %d, want 1", result.Summary.Failed)
	}
	if result.Summary.Warned != 1 {
		t.Fatalf("result.Summary.Warned = %d, want 1", result.Summary.Warned)
	}
	if len(result.Checks) != 4 {
		t.Fatalf("len(result.Checks) = %d, want 4", len(result.Checks))
	}

	// Check a specific check item
	found := false
	for _, c := range result.Checks {
		if c.ID == "readiness.gcloud-auth" {
			found = true
			if c.Status != "fail" {
				t.Fatalf("gcloud-auth.Status = %q, want fail", c.Status)
			}
			if c.Severity != "required" {
				t.Fatalf("gcloud-auth.Severity = %q, want required", c.Severity)
			}
		}
	}
	if !found {
		t.Fatal("missing check item readiness.gcloud-auth")
	}
}

func TestFormatCheckJSON_AllPass(t *testing.T) {
	t.Parallel()

	report := CheckReport{
		Worker:  "mon",
		Version: "1.0.0",
		Tools: []ToolStatus{
			{Name: "tmux", Installed: true, Required: true},
		},
		Readiness: []ReadinessStatus{
			{Name: "bridge", Passed: true, Required: true},
		},
	}

	out := FormatCheckJSON(report)
	var result CheckResultJSON
	if err := json.Unmarshal([]byte(out), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if result.Status != "pass" {
		t.Fatalf("result.Status = %q, want pass", result.Status)
	}
	if result.Summary.Failed != 0 {
		t.Fatalf("result.Summary.Failed = %d, want 0", result.Summary.Failed)
	}
}
