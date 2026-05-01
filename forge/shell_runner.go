package forge

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
)

type ShellRunner struct {
	Dir string
	Env []string
}

func (r ShellRunner) Run(command string) (RunResult, error) {
	return r.RunWithContext(context.Background(), command)
}

func (r ShellRunner) RunWithContext(ctx context.Context, command string) (RunResult, error) {
	cmd := exec.CommandContext(ctx, "sh", "-c", command)
	return r.runCmd(ctx, cmd)
}

func (r ShellRunner) Execute(name string, args ...string) (RunResult, error) {
	cmd := exec.Command(name, args...)
	return r.runCmd(context.Background(), cmd)
}

func (r ShellRunner) runCmd(ctx context.Context, cmd *exec.Cmd) (RunResult, error) {
	if r.Dir != "" {
		cmd.Dir = r.Dir
	}
	if len(r.Env) > 0 {
		cmd.Env = append(os.Environ(), r.Env...)
	}

	var stdout bytes.Buffer
	var stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	result := RunResult{
		Stdout: stdout.String(),
		Stderr: stderr.String(),
	}

	if err == nil {
		return result, nil
	}
	if ctxErr := ctx.Err(); ctxErr != nil {
		return result, ctxErr
	}

	var exitErr *exec.ExitError
	if ok := errors.As(err, &exitErr); ok {
		result.ExitCode = exitErr.ExitCode()
		return result, nil
	}

	return RunResult{}, err
}
