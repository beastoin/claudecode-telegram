package forge

import (
	"context"
	"fmt"
	"strings"
)

type RunResult struct {
	ExitCode int
	Stdout   string
	Stderr   string
}

type Runner interface {
	Run(command string) (RunResult, error)
}

type ContextRunner interface {
	RunWithContext(ctx context.Context, command string) (RunResult, error)
}

type ToolStatus struct {
	Name                 string
	Installed            bool
	Required             bool
	Version              string
	InstalledByBootstrap bool
}

func Bootstrap(manifest *Manifest, goos string, runner Runner) ([]ToolStatus, error) {
	return inspectTools(manifest, goos, runner, true)
}

func CheckTools(manifest *Manifest, goos string, runner Runner) ([]ToolStatus, error) {
	return inspectTools(manifest, goos, runner, false)
}

func inspectTools(manifest *Manifest, goos string, runner Runner, installMissing bool) ([]ToolStatus, error) {
	statuses := make([]ToolStatus, 0, len(manifest.Tools))

	for _, tool := range manifest.Tools {
		status := ToolStatus{
			Name:     tool.Name,
			Required: tool.Required,
		}

		result, err := runner.Run(tool.Check)
		if err != nil {
			return nil, fmt.Errorf("check tool %q: %w", tool.Name, err)
		}

		if result.ExitCode == 0 {
			status.Installed = true
			status.Version = strings.TrimSpace(result.Stdout)
			statuses = append(statuses, status)
			continue
		}

		if !tool.Required {
			statuses = append(statuses, status)
			continue
		}
		if !installMissing {
			statuses = append(statuses, status)
			continue
		}

		installCommand := tool.Install[goos]
		if installCommand == "" {
			return nil, fmt.Errorf("required tool %q is missing and has no install command for %s", tool.Name, goos)
		}

		if _, err := runner.Run(installCommand); err != nil {
			return nil, fmt.Errorf("install tool %q: %w", tool.Name, err)
		}

		result, err = runner.Run(tool.Check)
		if err != nil {
			return nil, fmt.Errorf("re-check tool %q: %w", tool.Name, err)
		}
		if result.ExitCode != 0 {
			return nil, fmt.Errorf("required tool %q is still unavailable after install", tool.Name)
		}

		status.Installed = true
		status.InstalledByBootstrap = true
		status.Version = strings.TrimSpace(result.Stdout)
		statuses = append(statuses, status)
	}

	return statuses, nil
}
