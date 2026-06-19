package forge

import (
	"context"
	"fmt"
	"os"
)

type EngineDriver interface {
	ID() string
	Prepare(ctx context.Context, req PrepareRequest) (*PreparedEngine, error)
	StartSpec(ctx context.Context, prepared *PreparedEngine) (*StartSpec, error)
	Capabilities() EngineCapabilities
}

type PrepareRequest struct {
	Manifest   *Manifest
	Vars       map[string]string
	RuntimeDir string
	GOOS       string
	Source     EmbeddedFileSource
}

type PreparedEngine struct {
	Env   map[string]string
	Files []PreparedFile
}

type PreparedFile struct {
	Path    string
	Content []byte
	Mode    os.FileMode
}

type StartSpec struct {
	Command []string
	Env     map[string]string
}

type EngineCapabilities struct {
	SupportsHooks       bool
	SupportsResume      bool
	SupportsPermissions bool
	HookEvents          []string
	InstructionFiles    []string
	ConfigFormat        string
}

func NewEngineDriver(kind string) (EngineDriver, error) {
	switch kind {
	case "claude-code":
		return &ClaudeCodeDriver{}, nil
	default:
		return nil, fmt.Errorf("unknown engine: %s", kind)
	}
}
