package forge

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

type CommandExecutor interface {
	Execute(name string, args ...string) (RunResult, error)
}

type TmuxRuntime struct {
	Commander     CommandExecutor
	Runner        Runner
	Session       string
	AdoptSession  string
	LaunchCommand string
	Environment   map[string]string
	APIKeyHelper  string
	IsRoot        func() bool
	Sleep         func(time.Duration)
}

func (t *TmuxRuntime) Start() error {
	if t.AdoptSession != "" {
		_, err := t.exec("tmux", "rename-session", "-t", t.AdoptSession, t.Session)
		return err
	}

	_, err := t.exec("tmux", "new-session", "-d", "-s", t.Session)
	if err != nil {
		return err
	}

	for _, key := range sortedEnvKeys(t.Environment) {
		value := t.Environment[key]
		if value == "" {
			continue
		}
		if _, err := t.exec("tmux", "set-environment", "-t", t.Session, key, value); err != nil {
			return err
		}
	}

	if err := t.writeAPIKeyHelper(); err != nil {
		return err
	}

	_, err = t.exec("tmux", "send-keys", "-t", t.Session, t.launchCommand(), "Enter")
	return err
}

func (t *TmuxRuntime) Send(message string) error {
	_, err := t.exec("tmux", "send-keys", "-t", t.Session, message, "Enter")
	return err
}

func (t *TmuxRuntime) Health() error {
	result, err := t.exec("tmux", "has-session", "-t", t.Session)
	if err != nil {
		return err
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("tmux session %q is not healthy", t.Session)
	}
	return nil
}

func (t *TmuxRuntime) LastOutput() (string, error) {
	result, err := t.exec("tmux", "capture-pane", "-t", t.Session, "-p")
	if err != nil {
		return "", err
	}
	return result.Stdout, nil
}

func (t *TmuxRuntime) exec(name string, args ...string) (RunResult, error) {
	if t.Commander != nil {
		return t.Commander.Execute(name, args...)
	}

	if t.Runner == nil {
		return RunResult{}, fmt.Errorf("tmux runtime requires a runner")
	}

	command := name
	for _, arg := range args {
		command += " " + shellQuote(arg)
	}
	return t.Runner.Run(command)
}

func shellQuote(value string) string {
	if value == "" {
		return `""`
	}

	for _, ch := range value {
		switch ch {
		case ' ', '\t', '\n', '"', '{', '}', '$', '`', '\\', '!', '(', ')', '[', ']', '|', '&', ';', '<', '>', '~', '#', '*', '?':
			return fmt.Sprintf("%q", value)
		}
	}

	return value
}

func joinShellCommand(args ...string) string {
	quoted := make([]string, 0, len(args))
	for _, arg := range args {
		quoted = append(quoted, shellQuote(arg))
	}
	return strings.Join(quoted, " ")
}

func (t *TmuxRuntime) launchCommand() string {
	if t.LaunchCommand == "" {
		return t.defaultLaunchCommand()
	}
	return t.LaunchCommand
}

func (t *TmuxRuntime) ConfigureRuntime(config RuntimeConfig) error {
	t.Environment = copyStringMap(config.Vars)
	if t.Environment == nil {
		t.Environment = map[string]string{}
	}

	configDir := t.Environment["CLAUDE_CONFIG_DIR"]
	if configDir != "" && t.Environment["ANTHROPIC_API_KEY"] != "" {
		t.APIKeyHelper = filepath.Join(configDir, "hooks", "api-key-helper.sh")
	}

	return nil
}

func (t *TmuxRuntime) defaultLaunchCommand() string {
	args := []string{"claude"}

	if t.APIKeyHelper != "" {
		settingsJSON, err := json.Marshal(map[string]string{
			"apiKeyHelper": t.APIKeyHelper,
		})
		if err == nil {
			args = append(args, "--settings", string(settingsJSON))
		}
	}

	if t.isRootUser() {
		args = append(args, "--permission-mode", "dontAsk")
	} else {
		args = append(args, "--dangerously-skip-permissions")
	}
	return joinShellCommand(args...)
}

func (t *TmuxRuntime) writeAPIKeyHelper() error {
	if t.APIKeyHelper == "" || t.Environment["ANTHROPIC_API_KEY"] == "" {
		return nil
	}

	if err := os.MkdirAll(filepath.Dir(t.APIKeyHelper), 0o755); err != nil {
		return fmt.Errorf("create api key helper dir: %w", err)
	}

	const script = `#!/bin/bash
set -euo pipefail

key="${ANTHROPIC_API_KEY:-}"
if [ -z "$key" ]; then
  session_name="$(tmux display-message -p '#{session_name}' 2>/dev/null || true)"
  if [ -n "$session_name" ]; then
    tmux_value="$(tmux show-environment -t "$session_name" ANTHROPIC_API_KEY 2>/dev/null || true)"
    key="${tmux_value#*=}"
  fi
fi

[ -n "$key" ] || exit 1
printf '%s\n' "$key"
`

	if err := os.WriteFile(t.APIKeyHelper, []byte(script), 0o700); err != nil {
		return fmt.Errorf("write api key helper: %w", err)
	}

	return nil
}

func sortedEnvKeys(values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func (t *TmuxRuntime) isRootUser() bool {
	if t.IsRoot != nil {
		return t.IsRoot()
	}
	return os.Geteuid() == 0
}

func (t *TmuxRuntime) sleep(delay time.Duration) {
	if t.Sleep != nil {
		t.Sleep(delay)
		return
	}
	time.Sleep(delay)
}
