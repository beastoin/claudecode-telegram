package forge

import (
	"bytes"
	"strings"
	"testing"
)

func TestCLI_InstallSkillDefaultsToClaudeSkillsDir(t *testing.T) {
	t.Parallel()

	cmd, err := parseInstallSkillCLI([]string{})
	if err != nil {
		t.Fatalf("parseInstallSkillCLI() error = %v", err)
	}
	if !strings.Contains(cmd.SkillDir, ".claude/skills") {
		t.Fatalf("cmd.SkillDir = %q, want path containing .claude/skills", cmd.SkillDir)
	}
}

func TestCLI_BuildSetsWorkerName(t *testing.T) {
	t.Parallel()

	cmd, err := ParseCLI([]string{"build", "mon", "--manifest", "workers/mon/manifest.yaml"})
	if err != nil {
		t.Fatalf("ParseCLI() error = %v", err)
	}
	if cmd.Name != "build" {
		t.Fatalf("cmd.Name = %q, want %q", cmd.Name, "build")
	}
	if cmd.Worker != "mon" {
		t.Fatalf("cmd.Worker = %q, want %q", cmd.Worker, "mon")
	}
}

func TestCLI_InstallSkillSetsDir(t *testing.T) {
	t.Parallel()

	cmd, err := ParseCLI([]string{"install-skill", "--dir", "/tmp/skills"})
	if err != nil {
		t.Fatalf("ParseCLI() error = %v", err)
	}
	if cmd.Name != "install-skill" {
		t.Fatalf("cmd.Name = %q, want %q", cmd.Name, "install-skill")
	}
	if cmd.SkillDir != "/tmp/skills" {
		t.Fatalf("cmd.SkillDir = %q, want %q", cmd.SkillDir, "/tmp/skills")
	}
}

func TestCLI_NoArgs_ReturnsUsageError(t *testing.T) {
	t.Parallel()

	_, err := ParseCLI([]string{})
	if err == nil {
		t.Fatal("ParseCLI() error = nil, want UsageError")
	}
	ue, ok := err.(*UsageError)
	if !ok {
		t.Fatalf("err type = %T, want *UsageError", err)
	}
	if !ue.ShowHelp {
		t.Fatal("UsageError.ShowHelp = false, want true")
	}
}

func TestCLI_UnknownCommand_ReturnsUsageErrorWithHint(t *testing.T) {
	t.Parallel()

	_, err := ParseCLI([]string{"bogus"})
	if err == nil {
		t.Fatal("ParseCLI() error = nil, want UsageError")
	}
	ue, ok := err.(*UsageError)
	if !ok {
		t.Fatalf("err type = %T, want *UsageError", err)
	}
	if ue.Hint == "" {
		t.Fatal("UsageError.Hint is empty, want suggestion")
	}
}

func TestCLI_VersionCommand(t *testing.T) {
	t.Parallel()

	cmd, err := ParseCLI([]string{"version"})
	if err != nil {
		t.Fatalf("ParseCLI() error = %v", err)
	}
	if cmd.Name != "version" {
		t.Fatalf("cmd.Name = %q, want %q", cmd.Name, "version")
	}
}

func TestCLI_HelpFlag(t *testing.T) {
	t.Parallel()

	_, err := ParseCLI([]string{"--help"})
	ue, ok := err.(*UsageError)
	if !ok {
		t.Fatalf("err type = %T, want *UsageError", err)
	}
	if !ue.ShowHelp {
		t.Fatal("UsageError.ShowHelp = false, want true")
	}
}

func TestCLI_BuildDefaultsManifestAndOutput(t *testing.T) {
	t.Parallel()

	t.Run("missing default manifest errors", func(t *testing.T) {
		_, err := ParseCLI([]string{"build", "nonexistent-worker"})
		if err == nil {
			t.Fatal("ParseCLI() error = nil, want error for missing manifest")
		}
	})

	t.Run("uses default manifest when present", func(t *testing.T) {
		cmd, err := ParseCLI([]string{"build", "mon"})
		if err != nil {
			t.Fatalf("ParseCLI() error = %v", err)
		}
		if cmd.Build.ManifestPath != "workers/mon/manifest.yaml" {
			t.Fatalf("cmd.Build.ManifestPath = %q, want %q", cmd.Build.ManifestPath, "workers/mon/manifest.yaml")
		}
	})
}

func TestCLIOutput_NoColor(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := NewCLIOutput(&buf, false)

	bold := out.Bold("test")
	if bold != "test" {
		t.Fatalf("Bold() with color=false = %q, want %q", bold, "test")
	}

	green := out.Green("ok")
	if green != "ok" {
		t.Fatalf("Green() with color=false = %q, want %q", green, "ok")
	}
}

func TestCLIOutput_WithColor(t *testing.T) {
	t.Setenv("NO_COLOR", "")
	t.Setenv("TERM", "xterm")
	var buf bytes.Buffer
	out := NewCLIOutput(&buf, true)

	bold := out.Bold("test")
	if !strings.Contains(bold, "\033[1m") {
		t.Fatalf("Bold() with color=true = %q, want ANSI bold", bold)
	}
}

func TestCLIOutput_RespectsNO_COLOR(t *testing.T) {
	t.Setenv("NO_COLOR", "1")
	var buf bytes.Buffer
	out := NewCLIOutput(&buf, true)

	bold := out.Bold("test")
	if bold != "test" {
		t.Fatalf("Bold() with NO_COLOR=1 = %q, want plain %q", bold, "test")
	}
}

func TestCLIOutput_HelpContainsSections(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	out := NewCLIOutput(&buf, false)
	out.PrintHelp("")

	output := buf.String()
	for _, section := range []string{"USAGE", "COMMANDS", "EXAMPLES"} {
		if !strings.Contains(output, section) {
			t.Fatalf("help output missing %s section", section)
		}
	}
}
