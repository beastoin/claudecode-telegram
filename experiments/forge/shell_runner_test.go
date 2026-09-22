package forge

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

func TestRealRunner_ExecutesCommand(t *testing.T) {
	t.Parallel()

	result, err := (ShellRunner{}).Run("echo hello")
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if result.ExitCode != 0 {
		t.Fatalf("result.ExitCode = %d, want 0", result.ExitCode)
	}
	if got := strings.TrimSpace(result.Stdout); got != "hello" {
		t.Fatalf("result.Stdout = %q, want %q", got, "hello")
	}
}

func TestRealRunner_RunWithContextTimesOut(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()

	_, err := (ShellRunner{}).RunWithContext(ctx, "sleep 1")
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("RunWithContext() error = %v, want context deadline exceeded", err)
	}
}
