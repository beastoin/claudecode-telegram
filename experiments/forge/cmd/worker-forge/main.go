package main

import (
	"fmt"
	"os"

	forge "github.com/beastoin/claudecode-telegram/forge"
	"golang.org/x/term"
)

type workerForgeDeps struct {
	build        func(cfg forge.BuildConfig) (*forge.BuildResult, error)
	installSkill func(dir string) error
}

func main() {
	isTTY := term.IsTerminal(int(os.Stderr.Fd()))
	out := forge.NewCLIOutput(os.Stderr, isTTY)

	err := run(os.Args[1:], workerForgeDeps{
		build: func(cfg forge.BuildConfig) (*forge.BuildResult, error) {
			return forge.Build(cfg, forge.AgeCredsEncryptor{})
		},
		installSkill: forge.InstallSkill,
	}, out)
	if err != nil {
		out.PrintError(err)
		if ue, ok := err.(*forge.UsageError); ok {
			if ue.ShowHelp && ue.Message == "" {
				os.Exit(0)
			}
			os.Exit(2)
		}
		os.Exit(1)
	}
}

func run(args []string, deps workerForgeDeps, out *forge.CLIOutput) error {
	cmd, err := forge.ParseCLI(args)
	if err != nil {
		return err
	}

	switch cmd.Name {
	case "version":
		out.PrintVersion()
		return nil
	case "build":
		if deps.build == nil {
			return fmt.Errorf("build handler is required")
		}
		out.PrintBuildStart(cmd.Worker, cmd.Build.ManifestPath, cmd.Build.Targets)
		result, err := deps.build(cmd.Build)
		if err != nil {
			return err
		}
		out.PrintBuildResult(result)
		return nil
	case "install-skill":
		if deps.installSkill == nil {
			return fmt.Errorf("install-skill handler is required")
		}
		if err := deps.installSkill(cmd.SkillDir); err != nil {
			return err
		}
		out.PrintInstallSkillResult(cmd.SkillDir)
		return nil
	default:
		return fmt.Errorf("unknown command %q", cmd.Name)
	}
}

func isUsage(err error) bool {
	_, ok := err.(*forge.UsageError)
	return ok
}
