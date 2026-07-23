package engine

import (
	"context"
	"fmt"
	"os"
	"path"
	"regexp"
	"strings"
	"time"

	"github.com/beastoin/claudecode-telegram/forge/manifest"
)

// FileSource provides read access to embedded files (hook scripts, etc.).
// This is the engine-local consumer interface for the root EmbeddedFileSource.
type FileSource interface {
	ReadFile(path string) ([]byte, error)
}

// EngineDriver defines the contract for a runtime engine (e.g., Claude Code).
type EngineDriver interface {
	ID() string
	Prepare(ctx context.Context, req PrepareRequest) (*PreparedEngine, error)
	StartSpec(ctx context.Context, prepared *PreparedEngine) (*StartSpec, error)
	Capabilities() EngineCapabilities
	AuthSpec() AuthSpec
}

// AuthSpec declares the engine's authentication contract.
// Each engine defines its own URL patterns, success/failure markers, timeouts,
// and interactive prompts to auto-handle.
type AuthSpec struct {
	Required          bool
	URLPatterns       []*regexp.Regexp
	SuccessMarkers    []string
	FailureMarkers    []string
	URLTimeout        time.Duration
	CodeTimeout       time.Duration
	CompletionTimeout time.Duration
	AutoResponses     []AutoResponse
}

// AutoResponse describes an interactive prompt to handle automatically
// during the auth flow (e.g., login method selection, theme picker).
type AutoResponse struct {
	Marker   string
	Response string
	Once     bool
}

// PrepareRequest holds the input for EngineDriver.Prepare.
type PrepareRequest struct {
	Manifest   *manifest.Manifest
	Vars       map[string]string
	RuntimeDir string
	GOOS       string
	Source     FileSource
}

// PreparedEngine holds the output from EngineDriver.Prepare.
type PreparedEngine struct {
	Env   map[string]string
	Files []PreparedFile
}

// PreparedFile is a file to write during engine preparation.
type PreparedFile struct {
	Path    string
	Content []byte
	Mode    os.FileMode
}

// StartSpec holds the command and env needed to launch the engine.
type StartSpec struct {
	Command []string
	Env     map[string]string
}

// EngineCapabilities describes what features the engine supports.
type EngineCapabilities struct {
	SupportsHooks       bool
	SupportsResume      bool
	SupportsPermissions bool
	HookEvents          []string
	InstructionFiles    []string
	ConfigFormat        string
}

// NewEngineDriver returns an EngineDriver for the given kind.
func NewEngineDriver(kind string) (EngineDriver, error) {
	switch kind {
	case "claude-code":
		return &ClaudeCodeDriver{}, nil
	default:
		return nil, fmt.Errorf("unknown engine: %s", kind)
	}
}

// ExpandTemplate substitutes $VAR and ${VAR} references in a template string.
var varRefPattern = regexp.MustCompile(`\$(\{)?([A-Za-z_][A-Za-z0-9_]*)\}?`)

func ExpandTemplate(template string, vars map[string]string) (string, error) {
	var missing []string

	result := varRefPattern.ReplaceAllStringFunc(template, func(match string) string {
		name := strings.TrimPrefix(match, "$")
		name = strings.TrimPrefix(name, "{")
		name = strings.TrimSuffix(name, "}")
		value, ok := vars[name]
		if !ok {
			missing = append(missing, name)
			return ""
		}
		return value
	})

	if len(missing) > 0 {
		return "", fmt.Errorf("missing variables: %s", strings.Join(missing, ", "))
	}

	return result, nil
}

// CanonicalSource normalises an embedded file source path.
func CanonicalSource(source string) string {
	cleaned := strings.ReplaceAll(source, "\\", "/")
	cleaned = strings.TrimPrefix(cleaned, "~/")
	cleaned = strings.TrimPrefix(cleaned, "./")
	cleaned = strings.TrimPrefix(cleaned, "/")
	return path.Clean(cleaned)
}

// BuildArtifactKey returns the storage key for an embedded artifact.
func BuildArtifactKey(source string, encrypted bool) string {
	prefix := "files"
	if encrypted {
		prefix = "creds"
	}
	return path.Join(prefix, CanonicalSource(source))
}
