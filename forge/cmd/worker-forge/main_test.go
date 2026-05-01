package main

import (
	"bytes"
	"strings"
	"testing"

	forge "github.com/beastoin/claudecode-telegram/forge"
)

func TestRunWorkerForge_Build(t *testing.T) {
	t.Parallel()

	var got forge.BuildConfig
	out := forge.NewCLIOutput(&bytes.Buffer{}, false)
	err := run([]string{
		"build",
		"mon",
		"--manifest", "workers/mon/manifest.yaml",
		"--identity", "age1manager",
		"--target", "linux/amd64,darwin/arm64",
		"--output", "./dist",
	}, workerForgeDeps{
		build: func(cfg forge.BuildConfig) (*forge.BuildResult, error) {
			got = cfg
			return &forge.BuildResult{OutputDir: cfg.OutputDir}, nil
		},
		installSkill: func(string) error {
			t.Fatal("installSkill should not be called")
			return nil
		},
	}, out)
	if err != nil {
		t.Fatalf("run() error = %v", err)
	}

	if got.ManifestPath != "workers/mon/manifest.yaml" {
		t.Fatalf("got.ManifestPath = %q, want %q", got.ManifestPath, "workers/mon/manifest.yaml")
	}
	if got.Identity != "age1manager" {
		t.Fatalf("got.Identity = %q, want %q", got.Identity, "age1manager")
	}
	if got.OutputDir != "./dist" {
		t.Fatalf("got.OutputDir = %q, want %q", got.OutputDir, "./dist")
	}
}

func TestRunWorkerForge_InstallSkill(t *testing.T) {
	t.Parallel()

	var gotDir string
	out := forge.NewCLIOutput(&bytes.Buffer{}, false)
	err := run([]string{
		"install-skill",
		"--dir", "/tmp/skills",
	}, workerForgeDeps{
		build: func(forge.BuildConfig) (*forge.BuildResult, error) {
			t.Fatal("build should not be called")
			return nil, nil
		},
		installSkill: func(dir string) error {
			gotDir = dir
			return nil
		},
	}, out)
	if err != nil {
		t.Fatalf("run() error = %v", err)
	}

	if gotDir != "/tmp/skills" {
		t.Fatalf("gotDir = %q, want %q", gotDir, "/tmp/skills")
	}
}

func TestRunWorkerForge_Version(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := forge.NewCLIOutput(&buf, false)
	err := run([]string{"version"}, workerForgeDeps{}, out)
	if err != nil {
		t.Fatalf("run() error = %v", err)
	}

	if !strings.Contains(buf.String(), "worker-forge") {
		t.Fatalf("version output = %q, want to contain 'worker-forge'", buf.String())
	}
}

func TestRunWorkerForge_HelpShowsUsage(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := forge.NewCLIOutput(&buf, false)
	err := run([]string{"help"}, workerForgeDeps{}, out)

	var ue *forge.UsageError
	if !isUsageErr(err, &ue) {
		t.Fatalf("run() error type = %T, want *UsageError", err)
	}

	out.PrintError(err)
	output := buf.String()
	if !strings.Contains(output, "USAGE") {
		t.Fatalf("help output missing USAGE section")
	}
	if !strings.Contains(output, "build") {
		t.Fatalf("help output missing build command")
	}
}

func TestRunWorkerForge_NoArgs_ShowsHelp(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := forge.NewCLIOutput(&buf, false)
	err := run([]string{}, workerForgeDeps{}, out)

	var ue *forge.UsageError
	if !isUsageErr(err, &ue) {
		t.Fatalf("run() error type = %T, want *UsageError", err)
	}
	if !ue.ShowHelp {
		t.Fatal("UsageError.ShowHelp = false, want true")
	}
}

func TestRunWorkerForge_UnknownCommand_ShowsHint(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := forge.NewCLIOutput(&buf, false)
	err := run([]string{"bogus"}, workerForgeDeps{}, out)

	var ue *forge.UsageError
	if !isUsageErr(err, &ue) {
		t.Fatalf("run() error type = %T, want *UsageError", err)
	}
	if ue.Hint == "" {
		t.Fatal("UsageError.Hint is empty, want suggestion")
	}
}

func TestRunWorkerForge_BuildHelp(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := forge.NewCLIOutput(&buf, false)
	err := run([]string{"build", "--help"}, workerForgeDeps{}, out)

	var ue *forge.UsageError
	if !isUsageErr(err, &ue) {
		t.Fatalf("run() error type = %T, want *UsageError", err)
	}
	if ue.Command != "build" {
		t.Fatalf("UsageError.Command = %q, want %q", ue.Command, "build")
	}
}

func isUsageErr(err error, target **forge.UsageError) bool {
	if ue, ok := err.(*forge.UsageError); ok {
		*target = ue
		return true
	}
	return false
}
