package forge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	goruntime "runtime"
	"strings"
	"testing"
	"testing/fstest"

	"filippo.io/age"
)

func TestWorkerCLI_ConstructsFullLaunchCommand(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)

	runtime := &launchCapturingRuntime{
		TmuxRuntime: TmuxRuntime{
			Session: "claude-prod-mon",
		},
		err: errors.New("stop after launch capture"),
	}

	err := RunEmbeddedWorker([]string{"--bridge-url", "http://bridge"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  BRIDGE_URL:
    source: flag
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
  TMUX_PREFIX:
    source: default
    default: "claude-prod-"
    required: true
  ANTHROPIC_API_KEY:
    source: creds
    required: true
dirs:
  - $CLAUDE_CONFIG_DIR/hooks
`),
			Files:          fstest.MapFS{},
			CredsEncrypted: []byte(`{"files":{"creds/__vars__.json":"{\"ANTHROPIC_API_KEY\":\"bundle-secret\"}"}}`),
		},
		Decryptor: &StubBundleDecryptor{},
		RuntimeFactory: func(_ *Manifest, _ WorkerCLIOptions, _ Runner) Runtime {
			return runtime
		},
		GOOS: "linux",
	})
	if !errors.Is(err, runtime.err) {
		t.Fatalf("RunEmbeddedWorker() error = %v, want %v", err, runtime.err)
	}

	launch := runtime.launch
	helperPath := filepath.Join(root, ".claude", "hooks", "api-key-helper.sh")
	for _, snippet := range []string{
		"claude",
		"--settings",
		helperPath,
		"--dangerously-skip-permissions",
	} {
		if !strings.Contains(launch, snippet) {
			t.Fatalf("launch call = %q, want %q", launch, snippet)
		}
	}
}

func TestRunCheckMode_HumanReadableOutput(t *testing.T) {
	t.Parallel()

	var stdout bytes.Buffer
	runner := &stubRunner{
		results: map[string]RunResult{
			"claude --version":              {ExitCode: 0, Stdout: "claude 2.1.114"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}

	err := RunEmbeddedWorker([]string{"--check", "--bridge-url", "http://bridge"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  BRIDGE_URL:
    source: flag
    required: true
tools:
  - name: claude
    check: claude --version
    required: true
readiness:
  - name: bridge-reachable
    check: curl -sf $BRIDGE_URL/health
    expect: exit 0
    required: true
`),
		},
		Runner: runner,
		GOOS:   "linux",
		Stdout: &stdout,
	})
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	output := stdout.String()
	for _, snippet := range []string{"Worker:", "Tools:", "Status: READY"} {
		if !strings.Contains(output, snippet) {
			t.Fatalf("output missing %q\n%s", snippet, output)
		}
	}
	if !strings.Contains(output, "✓") && !strings.Contains(output, "READY") {
		t.Fatalf("output = %q, want status indicators", output)
	}
	if json.Valid(stdout.Bytes()) {
		t.Fatalf("output = %q, want human-readable text instead of JSON", output)
	}
}

func TestRunVerifyMode_HumanReadableOutput(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	verifiedPath := filepath.Join(root, "charter.md")
	if err := os.WriteFile(verifiedPath, []byte("charter"), 0o600); err != nil {
		t.Fatalf("os.WriteFile(%q) error = %v", verifiedPath, err)
	}

	checksums, err := GenerateChecksumsJSON(map[string][]byte{
		"files/knowledge/charter.md": []byte("charter"),
	})
	if err != nil {
		t.Fatalf("GenerateChecksumsJSON() error = %v", err)
	}

	var stdout bytes.Buffer
	err = RunEmbeddedWorker([]string{"--verify"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 1.0.0
files:
  - source: knowledge/charter.md
    dest: ` + verifiedPath + `
  - source: memory/state.md
    dest: ` + filepath.Join(root, "state.md") + `
    integrity: skip
`),
			Checksums: checksums,
		},
		Stdout: &stdout,
	})
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	output := stdout.String()
	for _, snippet := range []string{"✓", "skipped", "Status: VERIFIED"} {
		if !strings.Contains(output, snippet) {
			t.Fatalf("output missing %q\n%s", snippet, output)
		}
	}
	if json.Valid(stdout.Bytes()) {
		t.Fatalf("output = %q, want human-readable text instead of JSON", output)
	}
}

func TestRunEmbeddedWorker_PassesContextToApp(t *testing.T) {
	t.Parallel()

	watchdog := &workerWatchdogSpy{}
	err := RunEmbeddedWorker(nil, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
`),
		},
		Watchdog: watchdog,
	})
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}
	if watchdog.ctx == nil {
		t.Fatal("watchdog.ctx = nil, want context")
	}
	if watchdog.ctx.Done() == nil {
		t.Fatal("watchdog.ctx.Done() = nil, want cancelable context")
	}
}

func TestE2E_BuildRunCheckVerify_WithCreds(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		if err == exec.ErrNotFound {
			t.Skip("go binary not available")
		}
		t.Fatalf("exec.LookPath(go) error = %v", err)
	}

	buildRoot := t.TempDir()
	manifestPath := filepath.Join(buildRoot, "manifest.yaml")
	t.Setenv("ANTHROPIC_API_KEY", "bundle-secret")

	if err := os.WriteFile(manifestPath, []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  BRIDGE_URL:
    source: flag
    required: true
  ANTHROPIC_API_KEY:
    source: creds
    required: true
`), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(buildRoot, "dist")
	result, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
		Targets:      []string{goruntime.GOOS + "/" + goruntime.GOARCH},
	}, StubCredsEncryptor{})
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	binaryPath := filepath.Join(result.OutputDir, expectedBinaryName("mon", goruntime.GOOS, goruntime.GOARCH))
	cmd := exec.Command(binaryPath, "--check", "--bridge-url", "http://bridge")
	runtimeHome := filepath.Join(buildRoot, "runtime-home")
	cmd.Env = withoutEnv(os.Environ(), "ANTHROPIC_API_KEY")
	cmd.Env = append(cmd.Env, "HOME="+runtimeHome)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker --check error = %v\n%s", err, output)
	}
	if !strings.Contains(string(output), "Worker: mon v1.0.0") {
		t.Fatalf("output = %q, want worker header", output)
	}
	if !strings.Contains(string(output), "ANTHROPIC_API_KEY = bundle-secret") {
		t.Fatalf("output = %q, want resolved creds value", output)
	}
}

func TestBuildAndRunWorker_WithRealAgeEncryptedCreds(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		if err == exec.ErrNotFound {
			t.Skip("go binary not available")
		}
		t.Fatalf("exec.LookPath(go) error = %v", err)
	}

	identity, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatalf("age.GenerateX25519Identity() error = %v", err)
	}

	buildRoot := t.TempDir()
	identityPath := filepath.Join(buildRoot, "manager.agekey")
	if err := os.WriteFile(identityPath, []byte(identity.String()+"\n"), 0o600); err != nil {
		t.Fatalf("os.WriteFile(identity) error = %v", err)
	}

	manifestPath := filepath.Join(buildRoot, "manifest.yaml")
	if err := os.WriteFile(manifestPath, []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  BRIDGE_URL:
    source: flag
    required: true
  ANTHROPIC_API_KEY:
    source: creds
    required: true
`), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(buildRoot, "dist")
	buildCmd := exec.Command("go", "run", "./cmd/worker-forge", "build", "mon",
		"--manifest", manifestPath,
		"--identity", identityPath,
		"--output", outputDir,
		"--target", goruntime.GOOS+"/"+goruntime.GOARCH,
	)
	buildCmd.Env = append(os.Environ(), "ANTHROPIC_API_KEY=sk-test-123")
	if output, err := buildCmd.CombinedOutput(); err != nil {
		t.Fatalf("worker-forge build error = %v\n%s", err, output)
	}

	binaryPath := filepath.Join(outputDir, expectedBinaryName("mon", goruntime.GOOS, goruntime.GOARCH))
	cmd := exec.Command(binaryPath, "--check", "--bridge-url", "http://bridge", "--identity", identityPath)
	runtimeHome := filepath.Join(buildRoot, "runtime-home")
	cmd.Env = withoutEnv(os.Environ(), "ANTHROPIC_API_KEY")
	cmd.Env = append(cmd.Env, "HOME="+runtimeHome)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker --check error = %v\n%s", err, output)
	}
	if !strings.Contains(string(output), "Worker: mon v1.0.0") {
		t.Fatalf("output = %q, want worker header", output)
	}
	if !strings.Contains(string(output), "ANTHROPIC_API_KEY = sk-test-123") {
		t.Fatalf("output = %q, want resolved real age creds value", output)
	}
}

func TestRunEmbeddedWorker_UsesEmbeddedKeysForExpandedSources(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)

	runtime := &launchCapturingRuntime{
		TmuxRuntime: TmuxRuntime{
			Session: "claude-prod-mon2",
		},
		err: errors.New("stop after launch capture"),
	}
	checksums, err := GenerateChecksumsJSON(map[string][]byte{
		"files/team/mon/charter.md":               []byte("charter"),
		"files/.claude/hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
	})
	if err != nil {
		t.Fatalf("GenerateChecksumsJSON() error = %v", err)
	}

	err = RunEmbeddedWorker([]string{"--bridge-url", "http://bridge"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon2
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  BRIDGE_URL:
    source: flag
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
dirs:
  - $CLAUDE_CONFIG_DIR/hooks
  - $HOME/team/mon2
files:
  - source: ~/team/mon/charter.md
    dest: $HOME/team/mon2/charter.md
hooks:
  - event: Stop
    command: $CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh
    source: ~/.claude/hooks/send-to-telegram.sh
`),
			Files: fstest.MapFS{
				"team/mon/charter.md":               &fstest.MapFile{Data: []byte("charter")},
				".claude/hooks/send-to-telegram.sh": &fstest.MapFile{Data: []byte("#!/bin/sh\necho sent\n")},
			},
			Checksums: checksums,
		},
		RuntimeFactory: func(_ *Manifest, _ WorkerCLIOptions, _ Runner) Runtime {
			return runtime
		},
		GOOS: "linux",
	})
	if !errors.Is(err, runtime.err) {
		t.Fatalf("RunEmbeddedWorker() error = %v, want %v", err, runtime.err)
	}

	if data, readErr := os.ReadFile(filepath.Join(root, "team", "mon2", "charter.md")); readErr != nil {
		t.Fatalf("os.ReadFile(charter) error = %v", readErr)
	} else if string(data) != "charter" {
		t.Fatalf("charter content = %q, want %q", data, "charter")
	}
	if _, statErr := os.Stat(filepath.Join(root, ".claude", "hooks", "send-to-telegram.sh")); statErr != nil {
		t.Fatalf("hook script stat error = %v", statErr)
	}
	if _, statErr := os.Stat(filepath.Join(root, ".claude", "settings.json")); statErr != nil {
		t.Fatalf("settings.json stat error = %v", statErr)
	}
}

func withoutEnv(env []string, key string) []string {
	prefix := key + "="
	filtered := make([]string, 0, len(env))
	for _, entry := range env {
		if strings.HasPrefix(entry, prefix) {
			continue
		}
		filtered = append(filtered, entry)
	}
	return filtered
}

type launchCapturingRuntime struct {
	TmuxRuntime
	launch string
	err    error
}

func (r *launchCapturingRuntime) Start() error {
	if err := r.writeAPIKeyHelper(); err != nil {
		return err
	}
	r.launch = r.launchCommand()
	return r.err
}

func TestWorkerCLI_SessionPrefixOverridesDefault(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{Name: "mon"}
	runner := ShellRunner{}

	defaultRT := defaultWorkerRuntimeFactory(manifest, WorkerCLIOptions{}, runner)
	tmux := defaultRT.(*TmuxRuntime)
	if tmux.Session != "claude-prod-mon" {
		t.Fatalf("default session = %q, want claude-prod-mon", tmux.Session)
	}

	customRT := defaultWorkerRuntimeFactory(manifest, WorkerCLIOptions{SessionPrefix: "claude-test-"}, runner)
	tmux = customRT.(*TmuxRuntime)
	if tmux.Session != "claude-test-mon" {
		t.Fatalf("custom session = %q, want claude-test-mon", tmux.Session)
	}
}

type workerWatchdogSpy struct {
	ctx context.Context
}

func (w *workerWatchdogSpy) Run(ctx context.Context) error {
	w.ctx = ctx
	return nil
}
