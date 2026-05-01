package forge

import (
	"strings"
	"testing"
)

func TestBootstrap_ToolFound(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V": {ExitCode: 0, Stdout: "tmux 3.4\n"},
		},
	}
	manifest := &Manifest{
		Tools: []ToolSpec{
			{Name: "tmux", Check: "tmux -V", Required: true},
		},
	}

	statuses, err := Bootstrap(manifest, "linux", runner)
	if err != nil {
		t.Fatalf("Bootstrap() error = %v", err)
	}

	if len(statuses) != 1 {
		t.Fatalf("len(statuses) = %d, want 1", len(statuses))
	}
	status := statuses[0]
	if !status.Installed || status.InstalledByBootstrap {
		t.Fatalf("status = %#v, want installed without bootstrap", status)
	}
	if status.Version != "tmux 3.4" {
		t.Fatalf("status.Version = %q, want %q", status.Version, "tmux 3.4")
	}
	wantCommands := []string{"tmux -V"}
	if strings.Join(runner.commands, "|") != strings.Join(wantCommands, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", runner.commands, wantCommands)
	}
}

func TestBootstrap_ToolMissing(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		sequence: map[string][]RunResult{
			"tmux -V": {
				{ExitCode: 1, Stderr: "missing"},
				{ExitCode: 0, Stdout: "tmux 3.4"},
			},
			"apt-get install -y tmux": {
				{ExitCode: 0},
			},
		},
	}
	manifest := &Manifest{
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Install:  map[string]string{"linux": "apt-get install -y tmux"},
				Required: true,
			},
		},
	}

	statuses, err := Bootstrap(manifest, "linux", runner)
	if err != nil {
		t.Fatalf("Bootstrap() error = %v", err)
	}

	if len(statuses) != 1 || !statuses[0].Installed || !statuses[0].InstalledByBootstrap {
		t.Fatalf("statuses = %#v, want installed by bootstrap", statuses)
	}
	wantCommands := []string{"tmux -V", "apt-get install -y tmux", "tmux -V"}
	if strings.Join(runner.commands, "|") != strings.Join(wantCommands, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", runner.commands, wantCommands)
	}
}

func TestBootstrap_OptionalToolMissing(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"gws --version": {ExitCode: 127, Stderr: "not found"},
		},
	}
	manifest := &Manifest{
		Tools: []ToolSpec{
			{
				Name:     "gws",
				Check:    "gws --version",
				Install:  map[string]string{"linux": "apt-get install -y gws"},
				Required: false,
			},
		},
	}

	statuses, err := Bootstrap(manifest, "linux", runner)
	if err != nil {
		t.Fatalf("Bootstrap() error = %v", err)
	}

	if len(statuses) != 1 {
		t.Fatalf("len(statuses) = %d, want 1", len(statuses))
	}
	status := statuses[0]
	if status.Installed || status.InstalledByBootstrap || status.Required {
		t.Fatalf("status = %#v, want missing optional tool", status)
	}
	wantCommands := []string{"gws --version"}
	if strings.Join(runner.commands, "|") != strings.Join(wantCommands, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", runner.commands, wantCommands)
	}
}
