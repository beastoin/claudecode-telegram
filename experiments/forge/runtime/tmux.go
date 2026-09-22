package runtime

import (
	"fmt"
	"sort"
	"strings"
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
}

func (t *TmuxRuntime) Start() error {
	if t.AdoptSession != "" {
		_, err := t.exec("tmux", "rename-session", "-t", t.AdoptSession, t.Session)
		return err
	}

	existing, _ := t.exec("tmux", "has-session", "-t", t.Session)
	if existing.ExitCode == 0 {
		return fmt.Errorf("tmux session %q already exists", t.Session)
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

	// Export env vars into the shell so the launch command inherits them.
	// tmux set-environment only affects NEW panes, not the initial shell.
	for _, key := range sortedEnvKeys(t.Environment) {
		value := t.Environment[key]
		if value == "" {
			continue
		}
		cmd := fmt.Sprintf("export %s=%s", key, shellQuote(value))
		if _, err := t.exec("tmux", "send-keys", "-t", t.Session, cmd, "Enter"); err != nil {
			return err
		}
	}

	_, err = t.exec("tmux", "send-keys", "-t", t.Session, t.launchCommand(), "Enter")
	return err
}

func (t *TmuxRuntime) Send(message string) error {
	_, err := t.exec("tmux", "send-keys", "-t", t.Session, message, "Enter")
	return err
}

func (t *TmuxRuntime) SendLiteral(message string) error {
	if _, err := t.exec("tmux", "send-keys", "-l", "-t", t.Session, message); err != nil {
		return err
	}
	_, err := t.exec("tmux", "send-keys", "-t", t.Session, "Enter")
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

func (t *TmuxRuntime) CaptureHistory(lines int) (string, error) {
	startLine := fmt.Sprintf("-%d", lines)
	result, err := t.exec("tmux", "capture-pane", "-t", t.Session, "-p", "-J", "-S", startLine)
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

func JoinShellCommand(args ...string) string {
	quoted := make([]string, 0, len(args))
	for _, arg := range args {
		quoted = append(quoted, shellQuote(arg))
	}
	return strings.Join(quoted, " ")
}

func (t *TmuxRuntime) SetLaunchCommand(command string) {
	t.LaunchCommand = command
}

func (t *TmuxRuntime) launchCommand() string {
	if t.LaunchCommand == "" {
		return "bash"
	}
	return t.LaunchCommand
}

func (t *TmuxRuntime) ConfigureRuntime(config RuntimeConfig) error {
	t.Environment = copyStringMap(config.Vars)
	if t.Environment == nil {
		t.Environment = map[string]string{}
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

func copyStringMap(input map[string]string) map[string]string {
	out := make(map[string]string, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}
