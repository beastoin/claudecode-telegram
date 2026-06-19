package forge

import (
	"bytes"
	"strings"
	"testing"
)

type mockDriver struct {
	name      string
	available bool
	built     bool
	started   bool
	stopped   bool
	running   bool
	buildOpts IsolationBuildOpts
	startOpts IsolationStartOpts
}

func (d *mockDriver) Name() string      { return d.name }
func (d *mockDriver) Available() bool    { return d.available }
func (d *mockDriver) Build(opts IsolationBuildOpts) error {
	d.built = true
	d.buildOpts = opts
	return nil
}
func (d *mockDriver) Start(opts IsolationStartOpts) error {
	d.started = true
	d.startOpts = opts
	return nil
}
func (d *mockDriver) Stop(name string) error {
	d.stopped = true
	return nil
}
func (d *mockDriver) Health(name string) (bool, string, error) {
	if d.running {
		return true, "running", nil
	}
	return false, "not found", nil
}

func TestContainerNameFor(t *testing.T) {
	if got := containerNameFor("mon"); got != "worker-mon" {
		t.Errorf("containerNameFor(mon) = %q, want worker-mon", got)
	}
	if got := containerNameFor("luck"); got != "worker-luck" {
		t.Errorf("containerNameFor(luck) = %q, want worker-luck", got)
	}
}

func TestGenerateDockerfile(t *testing.T) {
	manifest := &Manifest{
		Name:    "mon",
		Version: "2.0.0",
		Tools: []ToolSpec{
			{Name: "gh", Required: true, Install: map[string]string{"linux": "apt install -y gh"}},
			{Name: "codex", Required: false, Install: map[string]string{"linux": "npm install -g codex"}},
		},
	}

	df := generateDockerfile(manifest)

	if !strings.Contains(df, "FROM ubuntu:24.04") {
		t.Error("missing base image")
	}
	if !strings.Contains(df, "apt install -y gh") {
		t.Error("missing required tool install")
	}
	if strings.Contains(df, "codex") {
		t.Error("should not install optional tools")
	}
	if !strings.Contains(df, "COPY mon-linux-amd64 /usr/local/bin/mon") {
		t.Error("missing binary copy")
	}
	if !strings.Contains(df, "ENTRYPOINT [\"mon\"]") {
		t.Error("missing entrypoint")
	}
	if !strings.Contains(df, "HOME=/home/worker") {
		t.Error("missing HOME env")
	}
}

func TestHealthCheck_NothingRunning(t *testing.T) {
	manifest := &Manifest{Name: "testworker", Version: "1.0.0"}
	runner := &mockExecutor{results: map[string]RunResult{
		"tmux has-session -t claude-prod-testworker": {ExitCode: 1},
	}}

	var buf bytes.Buffer
	err := HealthCheck(manifest, runner, &buf)
	if err != nil {
		t.Fatal(err)
	}
	output := buf.String()
	if !strings.Contains(output, "not running") {
		t.Errorf("expected 'not running' in output, got: %s", output)
	}
}

func TestHealthCheck_TmuxRunning(t *testing.T) {
	manifest := &Manifest{Name: "testworker", Version: "1.0.0"}
	runner := &mockExecutor{results: map[string]RunResult{
		"tmux has-session -t claude-prod-testworker": {ExitCode: 0},
	}}

	var buf bytes.Buffer
	err := HealthCheck(manifest, runner, &buf)
	if err != nil {
		t.Fatal(err)
	}
	output := buf.String()
	if !strings.Contains(output, "bare metal: running") {
		t.Errorf("expected 'bare metal: running', got: %s", output)
	}
}

func TestStopWorker_TmuxSession(t *testing.T) {
	manifest := &Manifest{Name: "testworker", Version: "1.0.0"}
	runner := &mockExecutor{results: map[string]RunResult{
		"tmux has-session -t claude-prod-testworker":  {ExitCode: 0},
		"tmux kill-session -t claude-prod-testworker": {ExitCode: 0},
	}}

	var buf bytes.Buffer
	err := StopWorker(manifest, runner, &buf)
	if err != nil {
		t.Fatal(err)
	}
	output := buf.String()
	if !strings.Contains(output, "Session claude-prod-testworker stopped") {
		t.Errorf("expected session stop message, got: %s", output)
	}
}

func TestStopWorker_NothingRunning(t *testing.T) {
	manifest := &Manifest{Name: "testworker", Version: "1.0.0"}
	runner := &mockExecutor{results: map[string]RunResult{
		"tmux has-session -t claude-prod-testworker": {ExitCode: 1},
	}}

	var buf bytes.Buffer
	err := StopWorker(manifest, runner, &buf)
	if err != nil {
		t.Fatal(err)
	}
	output := buf.String()
	if !strings.Contains(output, "not running") {
		t.Errorf("expected 'not running', got: %s", output)
	}
}

func TestParseCLI_IsolatedFlag(t *testing.T) {
	opts, err := ParseWorkerCLI([]string{"--isolated", "--bridge-url", "http://test"})
	if err != nil {
		t.Fatal(err)
	}
	if !opts.Isolated {
		t.Error("expected Isolated=true")
	}
	if opts.BridgeURL != "http://test" {
		t.Errorf("BridgeURL = %q, want http://test", opts.BridgeURL)
	}
}

func TestParseCLI_StopFlag(t *testing.T) {
	opts, err := ParseWorkerCLI([]string{"--stop"})
	if err != nil {
		t.Fatal(err)
	}
	if !opts.Stop {
		t.Error("expected Stop=true")
	}
}

func TestParseCLI_HealthFlag(t *testing.T) {
	opts, err := ParseWorkerCLI([]string{"--health"})
	if err != nil {
		t.Fatal(err)
	}
	if !opts.Health {
		t.Error("expected Health=true")
	}
}

type mockExecutor struct {
	results map[string]RunResult
	calls   []string
}

func (m *mockExecutor) Run(command string) (RunResult, error) {
	m.calls = append(m.calls, command)
	if r, ok := m.results[command]; ok {
		return r, nil
	}
	return RunResult{ExitCode: 127}, nil
}

func (m *mockExecutor) RunWithContext(_ interface{}, command string) (RunResult, error) {
	return m.Run(command)
}

func (m *mockExecutor) Execute(name string, args ...string) (RunResult, error) {
	cmd := name + " " + strings.Join(args, " ")
	return m.Run(cmd)
}
