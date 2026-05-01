package forge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"filippo.io/age"
)

type BuildConfig struct {
	ManifestPath string
	Identity     string
	Targets      []string
	OutputDir    string
}

type CredsEncryptor interface {
	Encrypt(files map[string][]byte, recipients ...age.Recipient) ([]byte, error)
}

type StubCredsEncryptor struct{}
type stubAgeRecipient string

func (s stubAgeRecipient) Wrap(_ []byte) ([]*age.Stanza, error) {
	return nil, fmt.Errorf("stub age recipient %q cannot wrap keys", string(s))
}

type EmbedLayout struct {
	Manifest       []byte
	Files          map[string][]byte
	CredsEncrypted []byte
	ChecksumsJSON  []byte
}

type BuildResult struct {
	OutputDir string
	Artifacts []string
}

const workerForgeSkillMD = `# worker-forge-manifest

Use /worker-forge-manifest to generate a real manifest.yaml for the current worker role.

## Goal

Inspect the worker's actual environment, tools, secrets, paths, hooks, and runtime needs. Output a valid manifest that worker-forge can build into a standalone binary.

## Manifest Schema

Required top-level sections:

name: mon
version: 1.0.0

vars:
  HOME:
    source: env
    required: true
    description: "home directory"

dirs:
  - $HOME/team/mon

files:
  - source: ~/team/mon/charter.md
    dest: $HOME/team/mon/charter.md

tools:
  - name: tmux
    check: tmux -V
    required: true

readiness:
  - name: bridge-reachable
    check: curl -sf $BRIDGE_URL/health
    expect: exit 0
    required: true

hooks:
  - event: Stop
    command: $CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh
    source: ~/.claude/hooks/send-to-telegram.sh

## Var Sources

- source: env: read from host environment, for example HOME or PATH
- source: flag: provided by CLI flag or env override, for example BRIDGE_URL or CLAUDE_PROJECT_SLUG
- source: creds: resolved from the encrypted creds bundle, for example ANTHROPIC_API_KEY or GH_TOKEN
- source: default: derived from already-resolved vars, for example $HOME/.config/gcloud

## File Flags

- merge: true: keep the disk copy if it already exists and is newer; use for memory or worker-mutated state
- encrypted: true: package the file into creds.age instead of plaintext embed
- integrity: skip: exclude runtime-mutable files from watchdog integrity checks

## Real Worker Examples

### mon (Ops: DevOps + ProdOps)

Key vars include:

- BRIDGE_URL for bridge messaging
- MON_WORK_DIR for ops data and reports
- GOOGLE_APPLICATION_CREDENTIALS and KUBECONFIG for GCP/GKE
- CLAUDE_PROJECTS_DIR and CLAUDE_PROJECT_SLUG for Claude project memory

Typical files include knowledge, skills, memory, and encrypted creds:

files:
  - source: ~/team/mon/charter.md
    dest: $HOME/team/mon/charter.md
  - source: ~/.claude/skills/mon-daily-ops-email/
    dest: $HOME/.claude/skills/mon-daily-ops-email/
  - source: ~/.claude/projects/-home-claude-ops/memory/
    dest: $CLAUDE_PROJECTS_DIR/$CLAUDE_PROJECT_SLUG/memory/
    merge: true
    integrity: skip
  - source: ~/.config/omi/monitor/.env
    dest: $MONITOR_ENV
    encrypted: true

### luck (PeopleOps: wrap-ups, worker management, identity)

Key vars include:

- TEAM_DIR for shared team state
- TMUX_PREFIX for cross-worker tmux actions
- GWS_CONFIG_DIR and GWS_BIN for Google Workspace tooling
- SSH_KEY_MAC_MINI and SSH_CONFIG for Mac Mini access

Typical declaration pattern:

vars:
  TEAM_DIR:
    source: flag
    default: "$HOME/team"
    required: true
  TMUX_PREFIX:
    source: default
    default: "claude-prod-"
    required: true
  GH_TOKEN:
    source: creds
    required: true

## Generation Rules

- Emit real values and file paths from the current worker environment, not placeholders.
- All source: paths in files: and hooks: MUST use absolute paths with ~/ prefix (e.g., ~/team/mon/charter.md, ~/.claude/hooks/send-to-telegram.sh). NEVER use relative paths — the build pipeline resolves ~/ to the build machine's home directory. Relative paths cause build failures because files don't exist under workers/<name>/.
- Declare every directory the worker needs under dirs:.
- Put plaintext knowledge, skills, and stable files in files:.
- Mark secrets as encrypted: true.
- Mark memory and runtime-mutated files with merge: true and integrity: skip when appropriate.
- Include tool checks and readiness checks that match the worker's actual responsibilities.
- Include hook scripts plus hook registration commands when Claude Code hooks are required.

## Output

Write only the final manifest.yaml unless the caller explicitly asks for analysis first.
`

const generatedWorkerMainGo = `package main

import (
	"embed"
	"fmt"
	"io/fs"
	"os"
	"runtime"

	forge "github.com/beastoin/claudecode-telegram/forge"
)

//go:embed embed/manifest.yaml
var manifestYAML []byte

//go:embed all:embed/files embed/checksums.json embed/creds.age
var embeddedFiles embed.FS

//go:embed embed/checksums.json
var checksumsJSON []byte

//go:embed embed/creds.age
var credsEncrypted []byte

func main() {
	filesFS, err := fs.Sub(embeddedFiles, "embed/files")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	opts, err := forge.ParseWorkerCLI(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	switch {
	case opts.EmbeddedPath != "":
		if err := forge.WriteEmbeddedFile(filesFS, opts.EmbeddedPath, os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	transport := forge.NewTransport(opts.BridgeURL)

	err = forge.RunEmbeddedWorker(os.Args[1:], forge.WorkerDeps{
		Assets: forge.EmbeddedAssets{
			Manifest:       manifestYAML,
			Files:          filesFS,
			CredsEncrypted: credsEncrypted,
			Checksums:      checksumsJSON,
		},
		Runner:      forge.ShellRunner{},
		Transport:   transport,
		HookManager: forge.HookManager{},
		GOOS:        runtime.GOOS,
		Stdout:      os.Stdout,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
`

func CollectManifestFiles(manifestPath string, manifest *Manifest) (map[string][]byte, error) {
	manifestDir := filepath.Dir(manifestPath)
	collected := make(map[string][]byte)

	for _, entry := range manifest.Files {
		if err := collectManifestSource(manifestDir, entry.Source, entry.Encrypted, collected); err != nil {
			return nil, err
		}
	}

	for _, hook := range manifest.Hooks {
		if hook.Source == "" {
			continue
		}
		if err := collectManifestSource(manifestDir, hook.Source, false, collected); err != nil {
			return nil, err
		}
	}

	return collected, nil
}

func collectManifestSource(manifestDir, source string, encrypted bool, collected map[string][]byte) error {
	resolvedPath, err := resolveBuildSourcePath(manifestDir, source)
	if err != nil {
		return fmt.Errorf("resolve source %q: %w", source, err)
	}

	info, err := os.Stat(resolvedPath)
	if err != nil {
		return fmt.Errorf("stat source %q: %w", source, err)
	}

	if info.IsDir() {
		if err := filepath.Walk(resolvedPath, func(current string, currentInfo os.FileInfo, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if current == resolvedPath || currentInfo.IsDir() {
				return nil
			}

			relativePath, err := filepath.Rel(resolvedPath, current)
			if err != nil {
				return err
			}

			key := buildArtifactKey(path.Join(canonicalSource(source), filepath.ToSlash(relativePath)), encrypted)
			return collectBuildFile(current, key, collected)
		}); err != nil {
			return fmt.Errorf("walk source %q: %w", source, err)
		}
		return nil
	}

	key := buildArtifactKey(canonicalSource(source), encrypted)
	if err := collectBuildFile(resolvedPath, key, collected); err != nil {
		return fmt.Errorf("collect source %q: %w", source, err)
	}
	return nil
}

func collectBuildFile(sourcePath, key string, collected map[string][]byte) error {
	data, err := os.ReadFile(sourcePath)
	if err != nil {
		return err
	}
	collected[key] = data
	return nil
}

func resolveBuildSourcePath(manifestDir, source string) (string, error) {
	if strings.HasPrefix(source, "~/") {
		homeDir, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		return filepath.Join(homeDir, source[2:]), nil
	}

	if filepath.IsAbs(source) {
		return filepath.Clean(source), nil
	}

	return filepath.Join(manifestDir, filepath.FromSlash(source)), nil
}

func buildArtifactKey(source string, encrypted bool) string {
	prefix := "files"
	if encrypted {
		prefix = "creds"
	}
	return path.Join(prefix, canonicalSource(source))
}

func canonicalSource(source string) string {
	cleaned := filepath.ToSlash(source)
	cleaned = strings.TrimPrefix(cleaned, "~/")
	cleaned = strings.TrimPrefix(cleaned, "./")
	cleaned = strings.TrimPrefix(cleaned, "/")
	return path.Clean(cleaned)
}

func GenerateChecksumsJSON(collected map[string][]byte) ([]byte, error) {
	keys := make([]string, 0, len(collected))
	for name := range collected {
		keys = append(keys, name)
	}
	sort.Strings(keys)

	checksums := make(map[string]string, len(collected))
	for _, name := range keys {
		sum := sha256.Sum256(collected[name])
		checksums[name] = hex.EncodeToString(sum[:])
	}

	return json.MarshalIndent(checksums, "", "  ")
}

func (StubCredsEncryptor) Encrypt(files map[string][]byte, recipients ...age.Recipient) ([]byte, error) {
	recipient := ""
	if len(recipients) > 0 {
		recipient = fmt.Sprint(recipients[0])
	}

	payload := map[string]any{
		"recipient": recipient,
		"files":     make(map[string]string, len(files)),
	}
	filePayload := payload["files"].(map[string]string)

	keys := make([]string, 0, len(files))
	for name := range files {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	for _, name := range keys {
		filePayload[name] = string(files[name])
	}

	return json.MarshalIndent(payload, "", "  ")
}

func WriteEmbedLayout(root string, layout EmbedLayout) error {
	if err := os.MkdirAll(root, 0o700); err != nil {
		return fmt.Errorf("create embed root: %w", err)
	}

	if err := writeEmbedFile(filepath.Join(root, "manifest.yaml"), layout.Manifest); err != nil {
		return err
	}
	if err := writeEmbedFile(filepath.Join(root, "checksums.json"), layout.ChecksumsJSON); err != nil {
		return err
	}
	if err := writeEmbedFile(filepath.Join(root, "creds.age"), layout.CredsEncrypted); err != nil {
		return err
	}

	keys := make([]string, 0, len(layout.Files))
	for name := range layout.Files {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	for _, name := range keys {
		if !strings.HasPrefix(name, "files/") {
			continue
		}
		target := filepath.Join(root, filepath.FromSlash(name))
		if err := writeEmbedFile(target, layout.Files[name]); err != nil {
			return err
		}
	}

	if len(filterCollectedFiles(layout.Files, "files/")) == 0 {
		if err := writeEmbedFile(filepath.Join(root, "files", "placeholder.txt"), []byte("worker-forge placeholder asset\n")); err != nil {
			return err
		}
	}

	return nil
}

func writeEmbedFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create parent dir for %q: %w", path, err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("write %q: %w", path, err)
	}
	return nil
}

func Build(config BuildConfig, encryptor CredsEncryptor) (*BuildResult, error) {
	manifestData, err := os.ReadFile(config.ManifestPath)
	if err != nil {
		return nil, fmt.Errorf("read manifest: %w", err)
	}

	manifest, err := ParseManifest(manifestData)
	if err != nil {
		return nil, fmt.Errorf("parse manifest: %w", err)
	}

	collected, err := CollectManifestFiles(config.ManifestPath, manifest)
	if err != nil {
		return nil, err
	}

	credsVars, err := collectManifestCredsVars(manifest)
	if err != nil {
		return nil, err
	}
	if len(credsVars) > 0 {
		data, err := json.Marshal(credsVars)
		if err != nil {
			return nil, fmt.Errorf("marshal creds vars: %w", err)
		}
		collected[credsBundleVarsPath] = data
	}

	checksumsJSON, err := GenerateChecksumsJSON(collected)
	if err != nil {
		return nil, fmt.Errorf("generate checksums: %w", err)
	}

	var credsEncrypted []byte
	credsFiles := filterCollectedFiles(collected, "creds/")
	if len(credsFiles) > 0 {
		if encryptor == nil {
			return nil, fmt.Errorf("encryptor is required for encrypted files")
		}
		recipients, err := ParseAgeRecipients(config.Identity)
		if err != nil {
			switch encryptor.(type) {
			case StubCredsEncryptor, *StubCredsEncryptor:
				recipients = []age.Recipient{stubAgeRecipient(config.Identity)}
			default:
				return nil, fmt.Errorf("resolve age recipients: %w", err)
			}
		}
		credsEncrypted, err = encryptor.Encrypt(credsFiles, recipients...)
		if err != nil {
			return nil, fmt.Errorf("encrypt creds: %w", err)
		}
	}

	if err := WriteEmbedLayout(config.OutputDir, EmbedLayout{
		Manifest:       manifestData,
		Files:          collected,
		CredsEncrypted: credsEncrypted,
		ChecksumsJSON:  checksumsJSON,
	}); err != nil {
		return nil, err
	}

	if err := WriteEmbedLayout(filepath.Join(config.OutputDir, "embed"), EmbedLayout{
		Manifest:       manifestData,
		Files:          collected,
		CredsEncrypted: credsEncrypted,
		ChecksumsJSON:  checksumsJSON,
	}); err != nil {
		return nil, err
	}

	if err := writeGeneratedWorkerProject(config.OutputDir); err != nil {
		return nil, err
	}

	result := &BuildResult{OutputDir: config.OutputDir}
	for _, target := range config.Targets {
		artifact, err := buildTargetBinary(config.OutputDir, manifest.Name, target)
		if err != nil {
			return nil, err
		}
		result.Artifacts = append(result.Artifacts, artifact)
	}

	return result, nil
}

func collectManifestCredsVars(manifest *Manifest) (map[string]string, error) {
	creds := make(map[string]string)

	for name, spec := range manifest.Vars {
		if spec.Source != "creds" {
			continue
		}

		value, ok := os.LookupEnv(name)
		if ok && value != "" {
			creds[name] = value
			continue
		}

		if spec.Required {
			return nil, fmt.Errorf("required creds variable %q is not set in build environment", name)
		}
	}

	return creds, nil
}

func filterCollectedFiles(collected map[string][]byte, prefix string) map[string][]byte {
	filtered := make(map[string][]byte)
	for name, data := range collected {
		if !strings.HasPrefix(name, prefix) {
			continue
		}
		filtered[name] = append([]byte(nil), data...)
	}
	return filtered
}

func InstallSkill(dir string) error {
	skillDir := filepath.Join(dir, "worker-forge-manifest")
	if err := os.MkdirAll(skillDir, 0o755); err != nil {
		return fmt.Errorf("create skill dir: %w", err)
	}
	if err := os.WriteFile(filepath.Join(skillDir, "SKILL.md"), []byte(workerForgeSkillMD), 0o644); err != nil {
		return fmt.Errorf("write SKILL.md: %w", err)
	}
	return nil
}

func writeGeneratedWorkerProject(root string) error {
	moduleRoot, err := currentModuleRoot()
	if err != nil {
		return err
	}

	goMod := fmt.Sprintf(`module workerforge/generated

go 1.25.0

require (
	github.com/beastoin/claudecode-telegram/forge v0.0.0
	filippo.io/age v1.3.1 // indirect
	filippo.io/hpke v0.4.0 // indirect
	golang.org/x/crypto v0.47.0 // indirect
	golang.org/x/net v0.49.0 // indirect
	golang.org/x/sys v0.43.0 // indirect
	golang.org/x/term v0.42.0 // indirect
	golang.org/x/text v0.33.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260120221211-b8f7ae30c516 // indirect
	google.golang.org/grpc v1.80.0 // indirect
	google.golang.org/protobuf v1.36.11 // indirect
	gopkg.in/yaml.v3 v3.0.1
)

replace github.com/beastoin/claudecode-telegram/forge => %s
`, moduleRoot)

	goSum, err := os.ReadFile(filepath.Join(moduleRoot, "go.sum"))
	if err != nil {
		return fmt.Errorf("read module go.sum: %w", err)
	}

	if err := writeEmbedFile(filepath.Join(root, "main.go"), []byte(generatedWorkerMainGo)); err != nil {
		return err
	}
	if err := writeEmbedFile(filepath.Join(root, "go.mod"), []byte(goMod)); err != nil {
		return err
	}
	if err := writeEmbedFile(filepath.Join(root, "go.sum"), goSum); err != nil {
		return err
	}
	return nil
}

func currentModuleRoot() (string, error) {
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		return "", fmt.Errorf("resolve module root: runtime caller unavailable")
	}
	return filepath.Dir(filename), nil
}

func buildTargetBinary(projectDir, workerName, target string) (string, error) {
	goos, goarch, err := parseTarget(target)
	if err != nil {
		return "", err
	}

	binaryName := expectedBinaryName(workerName, goos, goarch)
	artifactPath := filepath.Join(projectDir, binaryName)

	cmd := exec.Command("go", "build", "-ldflags", "-s -w", "-o", artifactPath, ".")
	cmd.Dir = projectDir
	cmd.Env = append(os.Environ(), "GOOS="+goos, "GOARCH="+goarch)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("build target %s: %w\n%s", target, err, output)
	}

	return artifactPath, nil
}

func parseTarget(target string) (string, string, error) {
	goos, goarch, ok := strings.Cut(strings.TrimSpace(target), "/")
	if !ok || goos == "" || goarch == "" {
		return "", "", fmt.Errorf("invalid target %q", target)
	}
	return goos, goarch, nil
}

func expectedBinaryName(workerName, goos, goarch string) string {
	name := fmt.Sprintf("%s-%s-%s", workerName, goos, goarch)
	if goos == "windows" {
		return name + ".exe"
	}
	return name
}
