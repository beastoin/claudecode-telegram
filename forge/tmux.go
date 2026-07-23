package forge

import frt "github.com/beastoin/claudecode-telegram/forge/runtime"

type TmuxRuntime = frt.TmuxRuntime

func joinShellCommand(args ...string) string {
	return frt.JoinShellCommand(args...)
}
