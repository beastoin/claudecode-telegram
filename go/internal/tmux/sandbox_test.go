package tmux

import (
	"os"
	"runtime"
	"strings"
	"testing"

	"github.com/beastoin/claudecode-telegram/internal/sandbox"
)

func TestDockerRunCommand(t *testing.T) {
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("failed to get home dir: %v", err)
	}

	m := &Manager{
		Prefix:         "claude-prod-",
		SessionsDir:    "/tmp/sessions",
		Port:           "8081",
		SandboxImage:   "sandbox:latest",
		SandboxEnabled: true,
		SandboxMounts: []sandbox.Mount{
			{HostPath: "/host", ContainerPath: "/container", ReadOnly: false},
			{HostPath: "/ro", ContainerPath: "/ro", ReadOnly: true},
		},
	}

	cmd, err := m.dockerRunCommand("alice")
	if err != nil {
		t.Fatalf("dockerRunCommand error: %v", err)
	}

	assertContains := func(needle string) {
		t.Helper()
		if !strings.Contains(cmd, needle) {
			t.Errorf("expected command to contain %q, got %q", needle, cmd)
		}
	}

	assertContains("docker run -it")
	assertContains("--name=claude-worker-alice")
	assertContains("--rm")
	if runtime.GOOS == "linux" {
		assertContains("--add-host=host.docker.internal:host-gateway")
	}
	assertContains("-v=" + home + ":/workspace")
	assertContains("-v=/host:/container")
	assertContains("-v=/ro:/ro:ro")
	assertContains("-v=/tmp/sessions:/tmp/sessions")
	assertContains("-v=" + sandboxInboxRoot + ":" + sandboxInboxRoot)
	assertContains("-e=BRIDGE_URL=http://host.docker.internal:8081")
	assertContains("-e=PORT=8081")
	assertContains("-e=TMUX_PREFIX=claude-prod-")
	assertContains("-e=SESSIONS_DIR=/tmp/sessions")
	assertContains("-e=BRIDGE_SESSION=alice")
	assertContains("-e=TMUX_FALLBACK=1")
	assertContains("-w /workspace")
	assertContains("sandbox:latest")
	assertContains("claude --dangerously-skip-permissions")
}
