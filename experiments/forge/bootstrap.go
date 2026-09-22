package forge

import frt "github.com/beastoin/claudecode-telegram/forge/runtime"

type RunResult = frt.RunResult
type Runner = frt.Runner
type ContextRunner = frt.ContextRunner
type ToolStatus = frt.ToolStatus
type CommandExecutor = frt.CommandExecutor

func Bootstrap(manifest *Manifest, goos string, runner Runner) ([]ToolStatus, error) {
	return frt.Bootstrap(manifest, goos, runner)
}

func CheckTools(manifest *Manifest, goos string, runner Runner) ([]ToolStatus, error) {
	return frt.CheckTools(manifest, goos, runner)
}
