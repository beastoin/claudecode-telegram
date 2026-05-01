package main

import (
	"os/exec"
	"strings"
	"testing"

	forge "github.com/beastoin/claudecode-telegram/forge"
)

func TestParseWorkerCLI(t *testing.T) {
	t.Parallel()

	opts, err := forge.ParseWorkerCLI([]string{
		"--bridge-url", "http://bridge",
		"--session", "live-mon",
		"--launch-command", "sh -lc 'echo booted; exec sleep 600'",
		"--identity", "~/.age/manager.key",
		"--check",
		"--verify",
		"--name-override", "mon-dev",
		"--show-embedded", "placeholder.txt",
	})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}

	if opts.BridgeURL != "http://bridge" {
		t.Fatalf("opts.BridgeURL = %q, want %q", opts.BridgeURL, "http://bridge")
	}
	if opts.Session != "live-mon" {
		t.Fatalf("opts.Session = %q, want %q", opts.Session, "live-mon")
	}
	if opts.LaunchCommand != "sh -lc 'echo booted; exec sleep 600'" {
		t.Fatalf("opts.LaunchCommand = %q, want fake launcher", opts.LaunchCommand)
	}
	if opts.Identity != "~/.age/manager.key" {
		t.Fatalf("opts.Identity = %q, want %q", opts.Identity, "~/.age/manager.key")
	}
	if !opts.Check {
		t.Fatal("opts.Check = false, want true")
	}
	if !opts.Verify {
		t.Fatal("opts.Verify = false, want true")
	}
	if opts.NameOverride != "mon-dev" {
		t.Fatalf("opts.NameOverride = %q, want %q", opts.NameOverride, "mon-dev")
	}
	if opts.EmbeddedPath != "placeholder.txt" {
		t.Fatalf("opts.EmbeddedPath = %q, want %q", opts.EmbeddedPath, "placeholder.txt")
	}
}

func TestWorkerCLI_ShowEmbedded(t *testing.T) {
	t.Parallel()

	cmd := exec.Command("go", "run", ".", "--show-embedded", "placeholder.txt")
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker --show-embedded error = %v\n%s", err, output)
	}

	if string(output) != "worker-forge placeholder asset\n" {
		t.Fatalf("output = %q, want embedded placeholder asset", output)
	}
}

func TestEmbeddedManifest_IncludesBridgeReachabilityReadinessCheck(t *testing.T) {
	t.Parallel()

	manifest, err := forge.ParseManifest(manifestYAML)
	if err != nil {
		t.Fatalf("ParseManifest() error = %v", err)
	}

	var found bool
	for _, check := range manifest.Readiness {
		if check.Name == "bridge-reachable" {
			found = true
			if !check.Required {
				t.Fatal("bridge-reachable check must be required")
			}
			if !strings.Contains(check.Check, "$BRIDGE_URL") {
				t.Fatalf("bridge-reachable check = %q, want $BRIDGE_URL reference", check.Check)
			}
			break
		}
	}
	if !found {
		t.Fatal("embedded manifest missing required 'bridge-reachable' readiness check")
	}
}
