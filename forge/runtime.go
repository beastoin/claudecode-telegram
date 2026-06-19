package forge

type Runtime interface {
	Start() error
	Send(message string) error
	Health() error
}

type RuntimeMonitor interface {
	LastOutput() (string, error)
}

type RuntimeConfig struct {
	Vars map[string]string
}

type RuntimeConfigurer interface {
	ConfigureRuntime(config RuntimeConfig) error
}

type LaunchCommander interface {
	SetLaunchCommand(command string)
}
