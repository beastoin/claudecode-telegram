package forge

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestReadiness_CheckPasses(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		results: map[string]RunResult{
			"curl -sf http://bridge/health": {ExitCode: 0, Stdout: "ok\n"},
		},
	}
	manifest := &Manifest{
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
	}

	statuses, err := RunReadiness(manifest, map[string]string{"BRIDGE_URL": "http://bridge"}, runner)
	if err != nil {
		t.Fatalf("RunReadiness() error = %v", err)
	}

	if len(statuses) != 1 || !statuses[0].Passed || statuses[0].Fixed {
		t.Fatalf("statuses = %#v, want one passing non-fixed check", statuses)
	}
	if statuses[0].Output != "ok" {
		t.Fatalf("status.Output = %q, want %q", statuses[0].Output, "ok")
	}
}

func TestReadiness_CheckFailsWithFix(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		sequence: map[string][]RunResult{
			"gcloud auth list": {
				{ExitCode: 0, Stdout: "wrong-account"},
				{ExitCode: 0, Stdout: "beastoin-agents"},
			},
			"gcloud auth activate-service-account --key-file=/tmp/key.json": {
				{ExitCode: 0},
			},
		},
	}
	manifest := &Manifest{
		Readiness: []ReadinessCheck{
			{
				Name:     "gcp-auth",
				Check:    "gcloud auth list",
				Expect:   "contains \"beastoin-agents\"",
				Fix:      "gcloud auth activate-service-account --key-file=$GOOGLE_APPLICATION_CREDENTIALS",
				Required: true,
			},
		},
	}

	statuses, err := RunReadiness(manifest, map[string]string{"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/key.json"}, runner)
	if err != nil {
		t.Fatalf("RunReadiness() error = %v", err)
	}

	if len(statuses) != 1 || !statuses[0].Passed || !statuses[0].Fixed {
		t.Fatalf("statuses = %#v, want fixed passing check", statuses)
	}
	wantCommands := []string{
		"gcloud auth list",
		"gcloud auth activate-service-account --key-file=/tmp/key.json",
		"gcloud auth list",
	}
	if strings.Join(runner.commands, "|") != strings.Join(wantCommands, "|") {
		t.Fatalf("runner.commands = %#v, want %#v", runner.commands, wantCommands)
	}
}

func TestReadiness_RequiredCheckFails(t *testing.T) {
	t.Parallel()

	runner := &stubRunner{
		sequence: map[string][]RunResult{
			"curl -sf http://bridge/health": {
				{ExitCode: 1, Stderr: "down"},
				{ExitCode: 1, Stderr: "still down"},
			},
			"restart bridge": {
				{ExitCode: 0},
			},
		},
	}
	manifest := &Manifest{
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Fix:      "restart bridge",
				Required: true,
			},
		},
	}

	statuses, err := RunReadiness(manifest, map[string]string{"BRIDGE_URL": "http://bridge"}, runner)
	if err == nil {
		t.Fatal("RunReadiness() error = nil, want required check failure")
	}
	if !strings.Contains(err.Error(), "required readiness check \"bridge\" failed") {
		t.Fatalf("RunReadiness() error = %v, want bridge failure", err)
	}
	if len(statuses) != 1 || statuses[0].Passed || statuses[0].Fixed {
		t.Fatalf("statuses = %#v, want one failed required check", statuses)
	}
	if statuses[0].Output != "still down" {
		t.Fatalf("status.Output = %q, want %q", statuses[0].Output, "still down")
	}
}

func TestReadiness_UsesContextRunnerTimeout(t *testing.T) {
	t.Parallel()

	runner := &contextRunnerSpy{}
	manifest := &Manifest{
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
	}

	_, err := RunReadiness(manifest, map[string]string{"BRIDGE_URL": "http://bridge"}, runner)
	if err != nil {
		t.Fatalf("RunReadiness() error = %v", err)
	}

	if len(runner.deadlines) != 1 {
		t.Fatalf("len(runner.deadlines) = %d, want 1", len(runner.deadlines))
	}
	remaining := time.Until(runner.deadlines[0])
	if remaining <= 0 || remaining > defaultReadinessTimeout {
		t.Fatalf("readiness deadline remaining = %s, want within %s", remaining, defaultReadinessTimeout)
	}
}

type contextRunnerSpy struct {
	deadlines []time.Time
}

func (s *contextRunnerSpy) Run(command string) (RunResult, error) {
	return RunResult{ExitCode: 0}, nil
}

func (s *contextRunnerSpy) RunWithContext(ctx context.Context, command string) (RunResult, error) {
	deadline, ok := ctx.Deadline()
	if !ok {
		return RunResult{}, context.DeadlineExceeded
	}
	s.deadlines = append(s.deadlines, deadline)
	return RunResult{ExitCode: 0}, nil
}
