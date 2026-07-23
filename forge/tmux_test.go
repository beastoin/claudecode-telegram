package forge

import (
	"errors"
	"strings"
	"testing"
)

func TestRealTmux_CommandsAreCorrect(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}
	if err := runtime.Send("hello world"); err != nil {
		t.Fatalf("runtime.Send() error = %v", err)
	}
	recorder.results["tmux has-session -t claude-prod-mon"] = RunResult{ExitCode: 0}
	if err := runtime.Health(); err != nil {
		t.Fatalf("runtime.Health() error = %v", err)
	}

	got := strings.Join(recorder.calls, "|")
	want := strings.Join([]string{
		"tmux has-session -t claude-prod-mon",
		"tmux new-session -d -s claude-prod-mon",
		"tmux send-keys -t claude-prod-mon bash Enter",
		"tmux send-keys -t claude-prod-mon hello world Enter",
		"tmux has-session -t claude-prod-mon",
	}, "|")
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_StartRejectsDuplicateSession(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 0},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}

	err := runtime.Start()
	if err == nil {
		t.Fatal("runtime.Start() error = nil, want duplicate session error")
	}
	if !strings.Contains(err.Error(), "already exists") {
		t.Fatalf("runtime.Start() error = %q, want 'already exists'", err)
	}
}

func TestTmuxRuntime_StartNewSessionLaunchesClaudeCommand(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander:     recorder,
		Session:       "claude-prod-mon",
		LaunchCommand: "claude",
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	got := strings.Join(recorder.calls, "|")
	want := strings.Join([]string{
		"tmux has-session -t claude-prod-mon",
		"tmux new-session -d -s claude-prod-mon",
		"tmux send-keys -t claude-prod-mon claude Enter",
	}, "|")
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_AdoptSessionOnlyRenamesAndDoesNotRelaunch(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{}
	runtime := &TmuxRuntime{
		Commander:     recorder,
		Session:       "claude-prod-mon",
		AdoptSession:  "existing-session",
		LaunchCommand: "claude",
		Environment: map[string]string{
			"BRIDGE_URL": "http://bridge",
		},
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	got := strings.Join(recorder.calls, "|")
	want := "tmux rename-session -t existing-session claude-prod-mon"
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_StartReturnsLaunchError(t *testing.T) {
	t.Parallel()

	recorder := &erroringCommandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
		errs: map[string]error{
			"tmux send-keys -t claude-prod-mon bash Enter": errors.New("send failed"),
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}

	err := runtime.Start()
	if err == nil {
		t.Fatal("runtime.Start() error = nil, want launch error")
	}
}

func TestTmuxRuntime_LastOutputDetectsRecentActivity(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux capture-pane -t claude-prod-mon -p": {Stdout: "claude is still working"},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}

	output, err := runtime.LastOutput()
	if err != nil {
		t.Fatalf("runtime.LastOutput() error = %v", err)
	}
	if output != "claude is still working" {
		t.Fatalf("runtime.LastOutput() = %q, want %q", output, "claude is still working")
	}
}

func TestTmuxRuntime_SetsEnvironmentVariables(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}
	if err := runtime.ConfigureRuntime(RuntimeConfig{
		Vars: map[string]string{
			"BRIDGE_URL":  "http://bridge",
			"TMUX_PREFIX": "claude-prod-",
		},
	}); err != nil {
		t.Fatalf("runtime.ConfigureRuntime() error = %v", err)
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	got := strings.Join(recorder.calls, "|")
	want := strings.Join([]string{
		"tmux has-session -t claude-prod-mon",
		"tmux new-session -d -s claude-prod-mon",
		"tmux set-environment -t claude-prod-mon BRIDGE_URL http://bridge",
		"tmux set-environment -t claude-prod-mon TMUX_PREFIX claude-prod-",
		"tmux send-keys -t claude-prod-mon export BRIDGE_URL=http://bridge Enter",
		"tmux send-keys -t claude-prod-mon export TMUX_PREFIX=claude-prod- Enter",
		"tmux send-keys -t claude-prod-mon bash Enter",
	}, "|")
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_SetsEnvironmentBeforeLaunch(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}
	if err := runtime.ConfigureRuntime(RuntimeConfig{
		Vars: map[string]string{
			"BRIDGE_URL": "http://bridge",
		},
	}); err != nil {
		t.Fatalf("runtime.ConfigureRuntime() error = %v", err)
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	launchIndex := -1
	envIndex := -1
	for i, call := range recorder.calls {
		if strings.Contains(call, "set-environment") {
			envIndex = i
		}
		if strings.Contains(call, "send-keys") {
			launchIndex = i
		}
	}

	if envIndex == -1 || launchIndex == -1 {
		t.Fatalf("recorder.calls = %#v, want environment and launch calls", recorder.calls)
	}
	if envIndex > launchIndex {
		t.Fatalf("recorder.calls = %#v, want environment before launch", recorder.calls)
	}
}

func TestTmuxRuntime_EnvironmentEmptyMapIsNoOp(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	for _, call := range recorder.calls {
		if strings.Contains(call, "set-environment") {
			t.Fatalf("recorder.calls = %#v, want no set-environment calls", recorder.calls)
		}
	}
}

func TestTmuxRuntime_DefaultLaunchCommandIsBash(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	launch := recorder.calls[len(recorder.calls)-1]
	if !strings.Contains(launch, "bash") {
		t.Fatalf("launch call = %q, want bash (generic default)", launch)
	}
}

func TestTmuxRuntime_SetLaunchCommandOverridesDefault(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux has-session -t claude-prod-mon": {ExitCode: 1},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}
	runtime.SetLaunchCommand("claude --dangerously-skip-permissions")

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	launch := recorder.calls[len(recorder.calls)-1]
	if !strings.Contains(launch, "claude --dangerously-skip-permissions") {
		t.Fatalf("launch call = %q, want claude --dangerously-skip-permissions", launch)
	}
}

type commandRecorder struct {
	calls   []string
	results map[string]RunResult
}

func (c *commandRecorder) Execute(name string, args ...string) (RunResult, error) {
	call := strings.TrimSpace(strings.Join(append([]string{name}, args...), " "))
	c.calls = append(c.calls, call)
	if result, ok := c.results[call]; ok {
		return result, nil
	}
	return RunResult{}, nil
}

type erroringCommandRecorder struct {
	calls   []string
	results map[string]RunResult
	errs    map[string]error
}

func (c *erroringCommandRecorder) Execute(name string, args ...string) (RunResult, error) {
	call := strings.TrimSpace(strings.Join(append([]string{name}, args...), " "))
	c.calls = append(c.calls, call)
	if err, ok := c.errs[call]; ok {
		return RunResult{}, err
	}
	if result, ok := c.results[call]; ok {
		return result, nil
	}
	return RunResult{}, nil
}
