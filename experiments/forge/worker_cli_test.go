package forge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
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

	err := RunEmbeddedWorker([]string{"check", "--bridge-url", "http://bridge"}, WorkerDeps{
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
	err = RunEmbeddedWorker([]string{"verify"}, WorkerDeps{
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
		RuntimeFactory: func(_ *Manifest, _ WorkerCLIOptions, _ Runner) Runtime {
			return &stubRuntime{}
		},
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
	cmd := exec.Command(binaryPath, "check", "--bridge-url", "http://bridge")
	runtimeHome := filepath.Join(buildRoot, "runtime-home")
	cmd.Env = withoutEnv(os.Environ(), "ANTHROPIC_API_KEY")
	cmd.Env = append(cmd.Env, "HOME="+runtimeHome)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker check error = %v\n%s", err, output)
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
	cmd := exec.Command(binaryPath, "check", "--bridge-url", "http://bridge", "--identity", identityPath)
	runtimeHome := filepath.Join(buildRoot, "runtime-home")
	cmd.Env = withoutEnv(os.Environ(), "ANTHROPIC_API_KEY")
	cmd.Env = append(cmd.Env, "HOME="+runtimeHome)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker check error = %v\n%s", err, output)
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
	r.launch = r.LaunchCommand
	return r.err
}

func TestWorkerCLI_SessionPrefixOverridesDefault(t *testing.T) {
	t.Parallel()

	manifest := &Manifest{Name: "mon"}
	runner := &stubRunner{}

	defaultRT := defaultWorkerRuntimeFactory(manifest, WorkerCLIOptions{}, runner)
	tmux := defaultRT.(*TmuxRuntime)
	// Default prefix is PID-scoped, not "claude-prod-"
	wantDefault := fmt.Sprintf("claude-%d-mon", os.Getpid())
	if tmux.Session != wantDefault {
		t.Fatalf("default session = %q, want %q", tmux.Session, wantDefault)
	}

	customRT := defaultWorkerRuntimeFactory(manifest, WorkerCLIOptions{SessionPrefix: "claude-test-"}, runner)
	tmux = customRT.(*TmuxRuntime)
	if tmux.Session != "claude-test-mon" {
		t.Fatalf("custom session = %q, want claude-test-mon", tmux.Session)
	}

	// Explicit prod prefix still works
	prodRT := defaultWorkerRuntimeFactory(manifest, WorkerCLIOptions{SessionPrefix: "claude-prod-"}, runner)
	tmux = prodRT.(*TmuxRuntime)
	if tmux.Session != "claude-prod-mon" {
		t.Fatalf("prod session = %q, want claude-prod-mon", tmux.Session)
	}
}

func TestRunCheckMode_JSONOutput(t *testing.T) {
	t.Parallel()

	var stdout bytes.Buffer
	runner := &stubRunner{
		results: map[string]RunResult{
			"claude --version":              {ExitCode: 0, Stdout: "claude 2.1.114"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}

	err := RunEmbeddedWorker([]string{"check", "--output-json", "--bridge-url", "http://bridge"}, WorkerDeps{
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

	if !json.Valid(stdout.Bytes()) {
		t.Fatalf("output is not valid JSON: %s", stdout.String())
	}

	var result CheckResultJSON
	if err := json.Unmarshal(stdout.Bytes(), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if result.Command != "check" {
		t.Fatalf("result.Command = %q, want check", result.Command)
	}
	if result.Status != "pass" {
		t.Fatalf("result.Status = %q, want pass", result.Status)
	}
}

type workerWatchdogSpy struct {
	ctx context.Context
}

func (w *workerWatchdogSpy) Run(ctx context.Context) error {
	w.ctx = ctx
	return nil
}

// --- Subcommand parsing tests ---

func TestParseWorkerCLI_SubcommandCheck(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"check", "--bridge-url", "http://bridge"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.Check {
		t.Fatal("opts.Check = false, want true")
	}
	if opts.Command != "check" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "check")
	}
	if opts.BridgeURL != "http://bridge" {
		t.Fatalf("opts.BridgeURL = %q, want %q", opts.BridgeURL, "http://bridge")
	}
}

func TestParseWorkerCLI_SubcommandVerify(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"verify"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.Verify {
		t.Fatal("opts.Verify = false, want true")
	}
	if opts.Command != "verify" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "verify")
	}
}

func TestParseWorkerCLI_SubcommandRun(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"run", "--bridge-url", "http://bridge", "--connector", "telegram"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if opts.Command != "run" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "run")
	}
	if opts.BridgeURL != "http://bridge" {
		t.Fatalf("opts.BridgeURL = %q, want %q", opts.BridgeURL, "http://bridge")
	}
	if opts.ConnectorType != "telegram" {
		t.Fatalf("opts.ConnectorType = %q, want %q", opts.ConnectorType, "telegram")
	}
}

func TestParseWorkerCLI_SubcommandOnboard(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"onboard", "--identity", "AGE-KEY"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.Onboard {
		t.Fatal("opts.Onboard = false, want true")
	}
	if opts.Command != "onboard" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "onboard")
	}
}

func TestParseWorkerCLI_SubcommandHealth(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"health"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.Health {
		t.Fatal("opts.Health = false, want true")
	}
	if opts.Command != "health" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "health")
	}
}

func TestParseWorkerCLI_SubcommandStop(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"stop"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.Stop {
		t.Fatal("opts.Stop = false, want true")
	}
	if opts.Command != "stop" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "stop")
	}
}

func TestParseWorkerCLI_SubcommandVersion(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"version"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if opts.Command != "version" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "version")
	}
}

func TestParseWorkerCLI_SubcommandDescribe(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"describe", "check"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if opts.Command != "describe" {
		t.Fatalf("opts.Command = %q, want %q", opts.Command, "describe")
	}
	if opts.DescribeTarget != "check" {
		t.Fatalf("opts.DescribeTarget = %q, want %q", opts.DescribeTarget, "check")
	}
}

func TestParseWorkerCLI_OutputJSONFlag(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"check", "--output-json", "--bridge-url", "http://b"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.OutputJSON {
		t.Fatal("opts.OutputJSON = false, want true")
	}
}

func TestParseWorkerCLI_DryRunFlag(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI([]string{"onboard", "--dry-run"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if !opts.DryRun {
		t.Fatal("opts.DryRun = false, want true")
	}
}

func TestParseWorkerCLI_EnvVarFallback(t *testing.T) {
	t.Setenv("FORGE_BRIDGE_URL", "http://env-bridge")
	t.Setenv("FORGE_IDENTITY", "/path/to/key")
	opts, err := ParseWorkerCLI([]string{"check"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if opts.BridgeURL != "http://env-bridge" {
		t.Fatalf("opts.BridgeURL = %q, want %q (from FORGE_BRIDGE_URL)", opts.BridgeURL, "http://env-bridge")
	}
	if opts.Identity != "/path/to/key" {
		t.Fatalf("opts.Identity = %q, want %q (from FORGE_IDENTITY)", opts.Identity, "/path/to/key")
	}
}

func TestParseWorkerCLI_FlagOverridesEnvVar(t *testing.T) {
	t.Setenv("FORGE_BRIDGE_URL", "http://env-bridge")
	opts, err := ParseWorkerCLI([]string{"check", "--bridge-url", "http://flag-bridge"})
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if opts.BridgeURL != "http://flag-bridge" {
		t.Fatalf("opts.BridgeURL = %q, want %q (flag should override env)", opts.BridgeURL, "http://flag-bridge")
	}
}

func TestParseWorkerCLI_NoArgsDefaultsToRun(t *testing.T) {
	t.Parallel()
	opts, err := ParseWorkerCLI(nil)
	if err != nil {
		t.Fatalf("ParseWorkerCLI() error = %v", err)
	}
	if opts.Command != "run" {
		t.Fatalf("opts.Command = %q, want %q (no args = run)", opts.Command, "run")
	}
}

func TestRunDescribe_AllCommands(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	err := RunEmbeddedWorker([]string{"describe"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
`),
		},
		Stdout: &buf,
		Runner: &stubRunner{},
	})
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	if !json.Valid(buf.Bytes()) {
		t.Fatalf("output is not valid JSON: %s", buf.String())
	}

	var result map[string]interface{}
	if err := json.Unmarshal(buf.Bytes(), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}

	if result["worker"] != "mon" {
		t.Fatalf("result[worker] = %v, want mon", result["worker"])
	}
	cmds, ok := result["commands"].(map[string]interface{})
	if !ok {
		t.Fatalf("result[commands] is not a map: %T", result["commands"])
	}
	for _, required := range []string{"run", "check", "verify", "onboard", "health", "stop"} {
		if _, exists := cmds[required]; !exists {
			t.Fatalf("missing command %q in describe output", required)
		}
	}
}

func TestRunDescribe_SingleCommand(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	err := RunEmbeddedWorker([]string{"describe", "check"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
`),
		},
		Stdout: &buf,
		Runner: &stubRunner{},
	})
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	if !json.Valid(buf.Bytes()) {
		t.Fatalf("output is not valid JSON: %s", buf.String())
	}

	var result map[string]interface{}
	if err := json.Unmarshal(buf.Bytes(), &result); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}

	if result["command"] != "check" {
		t.Fatalf("result[command] = %v, want check", result["command"])
	}
	if _, ok := result["flags"]; !ok {
		t.Fatalf("missing flags in describe check output")
	}
}

func TestRunOnboard_DryRun(t *testing.T) {
	root := t.TempDir()
	t.Setenv("HOME", root)

	configDir := filepath.Join(root, ".claude")
	if err := os.MkdirAll(configDir, 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}

	var buf bytes.Buffer
	err := RunEmbeddedWorker(
		[]string{"onboard", "--dry-run", "--bridge-url", "http://bridge"},
		WorkerDeps{
			Assets: EmbeddedAssets{
				Manifest: []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
files:
  - source: knowledge/charter.md
    dest: $HOME/team/mon/charter.md
`),
				Files: fstest.MapFS{
					"knowledge/charter.md": &fstest.MapFile{Data: []byte("charter content")},
				},
				Checksums: func() []byte {
					d, _ := GenerateChecksumsJSON(map[string][]byte{
						"files/knowledge/charter.md": []byte("charter content"),
						"knowledge/charter.md":       []byte("charter content"),
					})
					return d
				}(),
			},
			Stdout:      &buf,
			Runner:      &stubRunner{},
			HookManager: HookManager{},
		},
	)
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	out := buf.String()
	if !strings.Contains(out, "dry-run") && !strings.Contains(out, "would") {
		t.Fatalf("dry-run output should indicate simulation: %s", out)
	}

	// Verify files were NOT extracted
	destFile := filepath.Join(root, "team", "mon", "charter.md")
	if _, err := os.Stat(destFile); err == nil {
		t.Fatal("dry-run should not extract files, but charter.md exists")
	}
}

func TestRunVersion(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	err := RunEmbeddedWorker([]string{"version"}, WorkerDeps{
		Assets: EmbeddedAssets{
			Manifest: []byte(`
name: mon
version: 2.5.0
vars:
  HOME:
    source: env
    required: true
`),
		},
		Stdout: &buf,
		Runner: &stubRunner{},
	})
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}

	out := buf.String()
	if !strings.Contains(out, "mon") {
		t.Fatalf("version output missing worker name: %s", out)
	}
	if !strings.Contains(out, "2.5.0") {
		t.Fatalf("version output missing version: %s", out)
	}
}
