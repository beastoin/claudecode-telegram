package forge

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestAppRun_FailsIfExtractedFileChecksumDriftsBeforeRuntimeStart(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	target := filepath.Join(root, "team", "mon", "charter.md")
	runtime := &stubRuntime{}
	runner := &mutatingRunner{
		t: t,
		results: map[string]RunResult{
			"tmux -V": {ExitCode: 0, Stdout: "tmux 3.4"},
		},
		mutate: func() {
			if err := os.WriteFile(target, []byte("tampered"), 0o644); err != nil {
				t.Fatalf("os.WriteFile(tampered) error = %v", err)
			}
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"CLAUDE_CONFIG_DIR": {
				Source:   "default",
				Default:  "$HOME/.claude",
				Required: true,
			},
		},
		Files: []FileSpec{
			{
				Source: "knowledge/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
	}

	err := (App{
		Manifest: manifest,
		Source: MapFileSource{
			"knowledge/charter.md": []byte("charter"),
		},
		Verifier: NewChecksumVerifier(checksumMap(t, map[string][]byte{
			"files/knowledge/charter.md": []byte("charter"),
			"knowledge/charter.md":       []byte("charter"),
		})),
		Runner:      runner,
		Runtime:     runtime,
		HookManager: HookManager{},
		GOOS:        "linux",
	}).Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
		},
	})
	if err == nil {
		t.Fatal("app.Run() error = nil, want checksum drift error")
	}
	if !strings.Contains(err.Error(), "checksum mismatch") {
		t.Fatalf("app.Run() error = %v, want checksum mismatch", err)
	}

	if runtime.startCalls != 0 {
		t.Fatalf("runtime.startCalls = %d, want 0", runtime.startCalls)
	}
}

func TestAppRun_VerifiesExtractedArtifactsBeforeReadiness(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	target := filepath.Join(root, "team", "mon", "charter.md")
	runner := &mutatingRunner{
		t: t,
		results: map[string]RunResult{
			"tmux -V":           {ExitCode: 0, Stdout: "tmux 3.4"},
			"curl http://ready": {ExitCode: 0, Stdout: "ok"},
		},
		mutate: func() {
			if err := os.WriteFile(target, []byte("tampered"), 0o644); err != nil {
				t.Fatalf("os.WriteFile(tampered) error = %v", err)
			}
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"CLAUDE_CONFIG_DIR": {
				Source:   "default",
				Default:  "$HOME/.claude",
				Required: true,
			},
		},
		Files: []FileSpec{
			{
				Source: "knowledge/charter.md",
				Dest:   "$HOME/team/mon/charter.md",
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl http://ready",
				Required: true,
			},
		},
	}

	err := (App{
		Manifest: manifest,
		Source: MapFileSource{
			"knowledge/charter.md": []byte("charter"),
		},
		Verifier: NewChecksumVerifier(checksumMap(t, map[string][]byte{
			"files/knowledge/charter.md": []byte("charter"),
			"knowledge/charter.md":       []byte("charter"),
		})),
		Runner:      runner,
		HookManager: HookManager{},
		GOOS:        "linux",
	}).Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
		},
	})
	if err == nil {
		t.Fatal("app.Run() error = nil, want checksum drift error")
	}
	if !strings.Contains(err.Error(), "checksum mismatch") {
		t.Fatalf("app.Run() error = %v, want checksum mismatch", err)
	}
	for _, command := range runner.commands {
		if command == "curl http://ready" {
			t.Fatalf("runner.commands = %#v, want readiness command to not run", runner.commands)
		}
	}
}

func TestAppRun_StartsWatchdogAndBlocksUntilContextCanceled(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	runtime := &stubRuntime{}
	transport := &appTransportSpy{runtime: runtime}
	watchdog := &watchdogRunnerSpy{
		started: make(chan struct{}, 1),
		done:    make(chan struct{}, 1),
	}
	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V": {ExitCode: 0, Stdout: "tmux 3.4"},
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"CLAUDE_CONFIG_DIR": {
				Source:   "default",
				Default:  "$HOME/.claude",
				Required: true,
			},
			"BRIDGE_URL": {
				Source:   "flag",
				Required: true,
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() {
		done <- (App{
			Manifest:    manifest,
			Runner:      runner,
			Runtime:     runtime,
			Transport:   transport,
			Watchdog:    watchdog,
			HookManager: HookManager{},
			GOOS:        "linux",
		}).Run(RunOptions{
			Context: ctx,
			Resolve: ResolveOptions{
				Env: map[string]string{"HOME": root},
				Flags: map[string]string{
					"BRIDGE_URL": "http://bridge",
				},
			},
		})
	}()

	<-watchdog.started

	select {
	case err := <-done:
		t.Fatalf("app.Run() returned early with %v, want block until cancel", err)
	case <-time.After(20 * time.Millisecond):
	}

	cancel()

	if err := <-done; err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}
	<-watchdog.done
}

func TestAppRun_PassesResolvedVarsToRuntime(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	runtime := &configCapturingRuntime{}
	app := App{
		Manifest: &Manifest{
			Name:    "mon",
			Version: "1.0.0",
			Vars: map[string]VarSpec{
				"HOME": {
					Source:   "env",
					Required: true,
				},
				"CLAUDE_CONFIG_DIR": {
					Source:   "default",
					Default:  "$HOME/.claude",
					Required: true,
				},
				"BRIDGE_URL": {
					Source:   "flag",
					Required: true,
				},
				"TMUX_PREFIX": {
					Source:   "default",
					Default:  "claude-prod-",
					Required: true,
				},
			},
		},
		Runtime: runtime,
	}

	if err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
			Flags: map[string]string{
				"BRIDGE_URL": "http://bridge",
			},
		},
	}); err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}

	if got := runtime.vars["BRIDGE_URL"]; got != "http://bridge" {
		t.Fatalf("runtime.vars[BRIDGE_URL] = %q, want %q", got, "http://bridge")
	}
	if got := runtime.vars["TMUX_PREFIX"]; got != "claude-prod-" {
		t.Fatalf("runtime.vars[TMUX_PREFIX] = %q, want %q", got, "claude-prod-")
	}
}

func TestAppRun_IncludesCredsInRuntimeEnv(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	runtime := &configCapturingRuntime{}
	app := App{
		Manifest: &Manifest{
			Name:    "mon",
			Version: "1.0.0",
			Vars: map[string]VarSpec{
				"HOME": {
					Source:   "env",
					Required: true,
				},
				"CLAUDE_CONFIG_DIR": {
					Source:   "default",
					Default:  "$HOME/.claude",
					Required: true,
				},
				"ANTHROPIC_API_KEY": {
					Source:   "creds",
					Required: true,
				},
			},
		},
		Runtime: runtime,
	}

	if err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
			Creds: map[string]string{
				"ANTHROPIC_API_KEY": "bundle-secret",
			},
		},
	}); err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}

	if got := runtime.vars["ANTHROPIC_API_KEY"]; got != "bundle-secret" {
		t.Fatalf("runtime.vars[ANTHROPIC_API_KEY] = %q, want %q", got, "bundle-secret")
	}
}

func TestAppRun_StopsBeforeRuntimeWhenReadinessFails(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	runtime := &stubRuntime{}
	runner := &stubRunner{
		results: map[string]RunResult{
			"curl http://ready": {ExitCode: 1, Stderr: "bridge unreachable"},
		},
	}
	app := App{
		Manifest: &Manifest{
			Name:    "mon",
			Version: "1.0.0",
			Vars: map[string]VarSpec{
				"HOME": {
					Source:   "env",
					Required: true,
				},
				"CLAUDE_CONFIG_DIR": {
					Source:   "default",
					Default:  "$HOME/.claude",
					Required: true,
				},
			},
			Readiness: []ReadinessCheck{
				{
					Name:     "bridge",
					Check:    "curl http://ready",
					Required: true,
				},
			},
		},
		Runner:      runner,
		Runtime:     runtime,
		HookManager: HookManager{},
	}

	err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{"HOME": root},
		},
	})
	if err == nil {
		t.Fatal("app.Run() error = nil, want readiness failure")
	}
	if !strings.Contains(err.Error(), `required readiness check "bridge" failed`) {
		t.Fatalf("app.Run() error = %v, want required readiness failure", err)
	}
	if runtime.startCalls != 0 {
		t.Fatalf("runtime.startCalls = %d, want 0", runtime.startCalls)
	}
}

type mutatingRunner struct {
	t        *testing.T
	results  map[string]RunResult
	mutate   func()
	mutated  bool
	commands []string
}

func (m *mutatingRunner) Run(command string) (RunResult, error) {
	m.commands = append(m.commands, command)
	if !m.mutated && m.mutate != nil {
		m.mutated = true
		m.mutate()
	}
	if result, ok := m.results[command]; ok {
		return result, nil
	}
	return RunResult{ExitCode: 0}, nil
}

type watchdogRunnerSpy struct {
	started chan struct{}
	done    chan struct{}
}

func (w *watchdogRunnerSpy) Run(ctx context.Context) error {
	if w.started != nil {
		w.started <- struct{}{}
	}
	<-ctx.Done()
	if w.done != nil {
		w.done <- struct{}{}
	}
	return nil
}

type configCapturingRuntime struct {
	stubRuntime
	vars map[string]string
}

func (r *configCapturingRuntime) ConfigureRuntime(config RuntimeConfig) error {
	r.vars = copyStringMap(config.Vars)
	return nil
}
