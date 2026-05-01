package forge

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRealTmux_CommandsAreCorrect(t *testing.T) {
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

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}
	if err := runtime.Send("hello world"); err != nil {
		t.Fatalf("runtime.Send() error = %v", err)
	}
	if err := runtime.Health(); err != nil {
		t.Fatalf("runtime.Health() error = %v", err)
	}

	got := strings.Join(recorder.calls, "|")
	want := strings.Join([]string{
		"tmux new-session -d -s claude-prod-mon",
		"tmux send-keys -t claude-prod-mon claude --dangerously-skip-permissions Enter",
		"tmux send-keys -t claude-prod-mon hello world Enter",
		"tmux has-session -t claude-prod-mon",
	}, "|")
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_StartNewSessionLaunchesClaudeCommand(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{}
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
		"tmux new-session -d -s claude-prod-mon",
		"tmux send-keys -t claude-prod-mon claude Enter",
	}, "|")
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_AdoptSessionOnlyRenamesAndDoesNotRelaunchClaude(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	recorder := &commandRecorder{}
	runtime := &TmuxRuntime{
		Commander:     recorder,
		Session:       "claude-prod-mon",
		AdoptSession:  "existing-session",
		LaunchCommand: "claude",
		Environment: map[string]string{
			"BRIDGE_URL": "http://bridge",
		},
		APIKeyHelper: filepath.Join(root, ".claude", "hooks", "api-key-helper.sh"),
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
		errs: map[string]error{
			"tmux send-keys -t claude-prod-mon claude --dangerously-skip-permissions Enter": errors.New("send failed"),
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

	recorder := &commandRecorder{}
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
		"tmux new-session -d -s claude-prod-mon",
		"tmux set-environment -t claude-prod-mon BRIDGE_URL http://bridge",
		"tmux set-environment -t claude-prod-mon TMUX_PREFIX claude-prod-",
		"tmux send-keys -t claude-prod-mon claude --dangerously-skip-permissions Enter",
	}, "|")
	if got != want {
		t.Fatalf("recorder.calls = %q, want %q", got, want)
	}
}

func TestTmuxRuntime_SetsEnvironmentBeforeLaunch(t *testing.T) {
	t.Parallel()

	recorder := &commandRecorder{}
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

	recorder := &commandRecorder{}
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

func TestTmuxRuntime_WritesAPIKeyHelper(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	recorder := &commandRecorder{}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}
	if err := runtime.ConfigureRuntime(RuntimeConfig{
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
			"ANTHROPIC_API_KEY": "secret-key",
		},
	}); err != nil {
		t.Fatalf("runtime.ConfigureRuntime() error = %v", err)
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	helperPath := filepath.Join(configDir, "hooks", "api-key-helper.sh")
	data, err := os.ReadFile(helperPath)
	if err != nil {
		t.Fatalf("os.ReadFile(%q) error = %v", helperPath, err)
	}

	content := string(data)
	if !strings.Contains(content, "ANTHROPIC_API_KEY") {
		t.Fatalf("helper content = %q, want ANTHROPIC_API_KEY lookup", content)
	}
	if !strings.Contains(content, "tmux show-environment") {
		t.Fatalf("helper content = %q, want tmux show-environment fallback", content)
	}
}

func TestTmuxRuntime_ConstructsLaunchCommandWithSettings(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	recorder := &commandRecorder{}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}
	if err := runtime.ConfigureRuntime(RuntimeConfig{
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
			"ANTHROPIC_API_KEY": "secret-key",
		},
	}); err != nil {
		t.Fatalf("runtime.ConfigureRuntime() error = %v", err)
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	launch := recorder.calls[len(recorder.calls)-1]
	helperPath := filepath.Join(configDir, "hooks", "api-key-helper.sh")
	if !strings.Contains(launch, "--settings") {
		t.Fatalf("launch call = %q, want --settings", launch)
	}
	if !strings.Contains(launch, helperPath) {
		t.Fatalf("launch call = %q, want helper path %q", launch, helperPath)
	}
}

func TestTmuxRuntime_LaunchCommandIncludesAutoAccept(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	recorder := &commandRecorder{}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
	}
	if err := runtime.ConfigureRuntime(RuntimeConfig{
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
			"ANTHROPIC_API_KEY": "secret-key",
		},
	}); err != nil {
		t.Fatalf("runtime.ConfigureRuntime() error = %v", err)
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	launch := recorder.calls[len(recorder.calls)-1]
	if !strings.Contains(launch, "--dangerously-skip-permissions") {
		t.Fatalf("launch call = %q, want --dangerously-skip-permissions", launch)
	}
}

func TestTmuxRuntime_LaunchCommandUsesDontAskWhenRunningAsRoot(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	recorder := &commandRecorder{
		results: map[string]RunResult{
			"tmux capture-pane -t claude-prod-mon -p -S -60": {Stdout: ""},
		},
	}
	runtime := &TmuxRuntime{
		Commander: recorder,
		Session:   "claude-prod-mon",
		IsRoot: func() bool {
			return true
		},
		Sleep: func(time.Duration) {},
	}
	if err := runtime.ConfigureRuntime(RuntimeConfig{
		Vars: map[string]string{
			"CLAUDE_CONFIG_DIR": configDir,
			"ANTHROPIC_API_KEY": "secret-key",
		},
	}); err != nil {
		t.Fatalf("runtime.ConfigureRuntime() error = %v", err)
	}

	if err := runtime.Start(); err != nil {
		t.Fatalf("runtime.Start() error = %v", err)
	}

	var launch string
	for _, call := range recorder.calls {
		if strings.Contains(call, "tmux send-keys -t claude-prod-mon claude") {
			launch = call
			break
		}
	}
	if launch == "" {
		t.Fatalf("recorder.calls = %#v, want launch call", recorder.calls)
	}
	if !strings.Contains(launch, "--permission-mode dontAsk") {
		t.Fatalf("launch call = %q, want --permission-mode dontAsk", launch)
	}
	if strings.Contains(launch, "--dangerously-skip-permissions") {
		t.Fatalf("launch call = %q, want no dangerous skip flag", launch)
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
	calls []string
	errs  map[string]error
}

func (c *erroringCommandRecorder) Execute(name string, args ...string) (RunResult, error) {
	call := strings.TrimSpace(strings.Join(append([]string{name}, args...), " "))
	c.calls = append(c.calls, call)
	if err, ok := c.errs[call]; ok {
		return RunResult{}, err
	}
	return RunResult{}, nil
}
