package forge

import (
	"context"
	"fmt"
	"strings"
	"time"
)

const defaultReadinessTimeout = 30 * time.Second

type ReadinessStatus struct {
	Name     string
	Passed   bool
	Required bool
	Fixed    bool
	Output   string
}

func RunReadiness(manifest *Manifest, vars map[string]string, runner Runner) ([]ReadinessStatus, error) {
	statuses := make([]ReadinessStatus, 0, len(manifest.Readiness))

	for _, check := range manifest.Readiness {
		status := ReadinessStatus{
			Name:     check.Name,
			Required: check.Required,
		}

		command, err := ExpandTemplate(check.Check, vars)
		if err != nil {
			return nil, fmt.Errorf("expand readiness check %q: %w", check.Name, err)
		}

		result, err := runReadinessCommand(runner, command)
		if err != nil {
			return nil, fmt.Errorf("run readiness check %q: %w", check.Name, err)
		}

		status.Output = strings.TrimSpace(strings.Join([]string{result.Stdout, result.Stderr}, "\n"))
		if readinessExpectationMet(check.Expect, result) {
			status.Passed = true
			statuses = append(statuses, status)
			continue
		}

		if check.Fix != "" {
			fixCommand, err := ExpandTemplate(check.Fix, vars)
			if err != nil {
				return nil, fmt.Errorf("expand readiness fix %q: %w", check.Name, err)
			}
			if _, err := runReadinessCommand(runner, fixCommand); err != nil {
				return nil, fmt.Errorf("run readiness fix %q: %w", check.Name, err)
			}

			result, err = runReadinessCommand(runner, command)
			if err != nil {
				return nil, fmt.Errorf("re-run readiness check %q: %w", check.Name, err)
			}

			status.Output = strings.TrimSpace(strings.Join([]string{result.Stdout, result.Stderr}, "\n"))
			if readinessExpectationMet(check.Expect, result) {
				status.Passed = true
				status.Fixed = true
				statuses = append(statuses, status)
				continue
			}
		}

		statuses = append(statuses, status)
		if check.Required {
			return statuses, fmt.Errorf("required readiness check %q failed", check.Name)
		}
	}

	return statuses, nil
}

func runReadinessCommand(runner Runner, command string) (RunResult, error) {
	ctx, cancel := context.WithTimeout(context.Background(), defaultReadinessTimeout)
	defer cancel()

	if contextRunner, ok := runner.(ContextRunner); ok {
		return contextRunner.RunWithContext(ctx, command)
	}

	type runnerResult struct {
		result RunResult
		err    error
	}
	done := make(chan runnerResult, 1)
	go func() {
		result, err := runner.Run(command)
		done <- runnerResult{result: result, err: err}
	}()

	select {
	case result := <-done:
		return result.result, result.err
	case <-ctx.Done():
		return RunResult{}, fmt.Errorf("readiness command timed out after %s: %w", defaultReadinessTimeout, ctx.Err())
	}
}

func readinessExpectationMet(expect string, result RunResult) bool {
	expect = strings.TrimSpace(expect)
	switch {
	case expect == "", expect == "exit 0":
		return result.ExitCode == 0
	case strings.HasPrefix(expect, "contains "):
		want := strings.TrimPrefix(expect, "contains ")
		want = strings.Trim(want, "\"")
		return result.ExitCode == 0 && strings.Contains(result.Stdout, want)
	default:
		return false
	}
}
