package forge

import "testing"

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
