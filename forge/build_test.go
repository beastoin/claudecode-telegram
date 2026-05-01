package forge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"filippo.io/age"
)

func TestBuildConfig_ParsesManifestPath(t *testing.T) {
	t.Parallel()

	cfg := BuildConfig{
		ManifestPath: "workers/mon/manifest.yaml",
		Identity:     "/tmp/manager.agekey",
		Targets:      []string{"linux/amd64", "darwin/arm64"},
		OutputDir:    "dist",
	}

	if cfg.ManifestPath != "workers/mon/manifest.yaml" {
		t.Fatalf("cfg.ManifestPath = %q, want %q", cfg.ManifestPath, "workers/mon/manifest.yaml")
	}
	if cfg.Identity != "/tmp/manager.agekey" {
		t.Fatalf("cfg.Identity = %q, want %q", cfg.Identity, "/tmp/manager.agekey")
	}
	if len(cfg.Targets) != 2 {
		t.Fatalf("len(cfg.Targets) = %d, want 2", len(cfg.Targets))
	}
	if cfg.OutputDir != "dist" {
		t.Fatalf("cfg.OutputDir = %q, want %q", cfg.OutputDir, "dist")
	}
}

func TestCollector_GathersFilesFromManifest(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifestPath := filepath.Join(root, "workers", "mon", "manifest.yaml")
	if err := os.MkdirAll(filepath.Dir(manifestPath), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}

	fixtures := map[string]string{
		filepath.Join(root, "workers", "mon", "knowledge", "charter.md"):    "charter",
		filepath.Join(root, "workers", "mon", "skills", "demo", "SKILL.md"): "skill",
		filepath.Join(root, "workers", "mon", "secrets", "agent.env"):       "TOKEN=secret",
	}
	for path, content := range fixtures {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatalf("os.MkdirAll(%q) error = %v", path, err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatalf("os.WriteFile(%q) error = %v", path, err)
		}
	}
	if err := os.WriteFile(manifestPath, []byte("name: mon\nversion: 1.0.0\n"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifestPath) error = %v", err)
	}

	manifest := &Manifest{
		Files: []FileSpec{
			{Source: "knowledge/charter.md"},
			{Source: "skills/demo/"},
			{Source: "secrets/agent.env", Encrypted: true},
		},
	}

	collected, err := CollectManifestFiles(manifestPath, manifest)
	if err != nil {
		t.Fatalf("CollectManifestFiles() error = %v", err)
	}

	want := map[string]string{
		"files/knowledge/charter.md": "charter",
		"files/skills/demo/SKILL.md": "skill",
		"creds/secrets/agent.env":    "TOKEN=secret",
	}
	if len(collected) != len(want) {
		t.Fatalf("len(collected) = %d, want %d", len(collected), len(want))
	}
	for name, expected := range want {
		data, ok := collected[name]
		if !ok {
			t.Fatalf("collected[%q] missing", name)
		}
		if got := string(data); got != expected {
			t.Fatalf("collected[%q] = %q, want %q", name, got, expected)
		}
	}
}

func TestChecksumGenerator_ProducesJSON(t *testing.T) {
	t.Parallel()

	collected := map[string][]byte{
		"files/knowledge/charter.md": []byte("charter"),
		"creds/secrets/agent.env":    []byte("TOKEN=secret"),
	}

	data, err := GenerateChecksumsJSON(collected)
	if err != nil {
		t.Fatalf("GenerateChecksumsJSON() error = %v", err)
	}

	var checksums map[string]string
	if err := json.Unmarshal(data, &checksums); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}

	for name, content := range collected {
		sum := sha256.Sum256(content)
		want := hex.EncodeToString(sum[:])
		if got := checksums[name]; got != want {
			t.Fatalf("checksums[%q] = %q, want %q", name, got, want)
		}
	}
}

func TestCredsEncryptor_EncryptsWithAge(t *testing.T) {
	t.Parallel()

	identity, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatalf("age.GenerateX25519Identity() error = %v", err)
	}

	encryptor := StubCredsEncryptor{}
	plaintext := map[string][]byte{
		"creds/secrets/agent.env": []byte("TOKEN=secret"),
	}

	blob, err := encryptor.Encrypt(plaintext, identity.Recipient())
	if err != nil {
		t.Fatalf("Encrypt() error = %v", err)
	}

	if !strings.Contains(string(blob), identity.Recipient().String()) {
		t.Fatalf("encrypted blob = %q, want public key marker", blob)
	}
	if !strings.Contains(string(blob), "TOKEN=secret") {
		t.Fatalf("encrypted blob = %q, want embedded plaintext for stub", blob)
	}
}

func TestEmbedder_WritesGoEmbedLayout(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	collected := map[string][]byte{
		"files/knowledge/charter.md": []byte("charter"),
		"creds/secrets/agent.env":    []byte("TOKEN=secret"),
	}
	checksumsJSON, err := GenerateChecksumsJSON(collected)
	if err != nil {
		t.Fatalf("GenerateChecksumsJSON() error = %v", err)
	}

	err = WriteEmbedLayout(root, EmbedLayout{
		Manifest:       []byte("name: mon\nversion: 1.0.0\n"),
		Files:          collected,
		CredsEncrypted: []byte("encrypted-blob"),
		ChecksumsJSON:  checksumsJSON,
	})
	if err != nil {
		t.Fatalf("WriteEmbedLayout() error = %v", err)
	}

	assertFileContent(t, filepath.Join(root, "files", "knowledge", "charter.md"), "charter")
	assertFileContent(t, filepath.Join(root, "creds.age"), "encrypted-blob")
	assertFileContent(t, filepath.Join(root, "checksums.json"), string(checksumsJSON))
	assertFileContent(t, filepath.Join(root, "manifest.yaml"), "name: mon\nversion: 1.0.0\n")
	if _, err := os.Stat(filepath.Join(root, "files", "creds", "secrets", "agent.env")); !os.IsNotExist(err) {
		t.Fatalf("unexpected plaintext creds file, stat error = %v", err)
	}
}

func TestBuild_EndToEnd(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifestPath := filepath.Join(root, "manifest.yaml")
	if err := os.MkdirAll(filepath.Join(root, "knowledge"), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.MkdirAll(filepath.Join(root, "secrets"), 0o755); err != nil {
		t.Fatalf("os.MkdirAll() error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "knowledge", "charter.md"), []byte("charter"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(charter) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "secrets", "agent.env"), []byte("TOKEN=secret"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(secret) error = %v", err)
	}
	manifestData := []byte(`
name: mon
version: 1.0.0
files:
  - source: knowledge/charter.md
    dest: $HOME/team/mon/charter.md
  - source: secrets/agent.env
    dest: $HOME/.config/agent.env
    encrypted: true
`)
	if err := os.WriteFile(manifestPath, manifestData, 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(root, "out")
	result, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
	}, StubCredsEncryptor{})
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	if result.OutputDir != outputDir {
		t.Fatalf("result.OutputDir = %q, want %q", result.OutputDir, outputDir)
	}
	assertFileContent(t, filepath.Join(outputDir, "files", "knowledge", "charter.md"), "charter")
	assertFileContent(t, filepath.Join(outputDir, "manifest.yaml"), string(manifestData))
	assertFileContent(t, filepath.Join(outputDir, "creds.age"), "{\n  \"files\": {\n    \"creds/secrets/agent.env\": \"TOKEN=secret\"\n  },\n  \"recipient\": \"age1examplepublickey\"\n}")
	if _, err := os.Stat(filepath.Join(outputDir, "checksums.json")); err != nil {
		t.Fatalf("expected checksums.json, stat error = %v", err)
	}
}

func TestBuild_EmbedLayoutUsesSecurePermissions(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	manifestPath := filepath.Join(root, "manifest.yaml")
	if err := os.WriteFile(manifestPath, []byte(`
name: mon
version: 1.0.0
`), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(root, "out")
	if _, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
	}, StubCredsEncryptor{}); err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	embedRoot := filepath.Join(outputDir, "embed")
	if err := filepath.Walk(embedRoot, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		got := info.Mode().Perm()
		if info.IsDir() {
			if got != 0o700 {
				t.Fatalf("dir %q mode = %#o, want %#o", path, got, 0o700)
			}
			return nil
		}
		if got != 0o600 {
			t.Fatalf("file %q mode = %#o, want %#o", path, got, 0o600)
		}
		return nil
	}); err != nil {
		t.Fatalf("filepath.Walk(%q) error = %v", embedRoot, err)
	}
}

func TestBuildPipeline_ProducesCompilableLayout(t *testing.T) {
	t.Parallel()

	if _, err := exec.LookPath("go"); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			t.Skip("go binary not available")
		}
		t.Fatalf("exec.LookPath(go) error = %v", err)
	}

	root := t.TempDir()
	manifestPath := filepath.Join(root, "manifest.yaml")
	if err := os.MkdirAll(filepath.Join(root, "knowledge"), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(knowledge) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "knowledge", "charter.md"), []byte("charter"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(charter) error = %v", err)
	}
	manifestData := []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
  BRIDGE_URL:
    source: flag
    default: "http://localhost:8271"
    required: true
dirs:
  - $CLAUDE_CONFIG_DIR/hooks
files:
  - source: knowledge/charter.md
    dest: $HOME/team/mon/charter.md
`)
	if err := os.WriteFile(manifestPath, manifestData, 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(root, "out")
	if _, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
	}, StubCredsEncryptor{}); err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	for _, required := range []string{
		filepath.Join(outputDir, "main.go"),
		filepath.Join(outputDir, "go.mod"),
		filepath.Join(outputDir, "embed", "manifest.yaml"),
		filepath.Join(outputDir, "embed", "files", "knowledge", "charter.md"),
		filepath.Join(outputDir, "embed", "checksums.json"),
		filepath.Join(outputDir, "embed", "creds.age"),
	} {
		if _, err := os.Stat(required); err != nil {
			t.Fatalf("expected %q, stat error = %v", required, err)
		}
	}

	cmd := exec.Command("go", "build", ".")
	cmd.Dir = outputDir
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("go build error = %v\n%s", err, output)
	}
}

func TestBuildPipeline_CrossCompiles(t *testing.T) {
	t.Parallel()

	if _, err := exec.LookPath("go"); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			t.Skip("go binary not available")
		}
		t.Fatalf("exec.LookPath(go) error = %v", err)
	}

	root := t.TempDir()
	manifestPath := filepath.Join(root, "manifest.yaml")
	if err := os.WriteFile(manifestPath, []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
  BRIDGE_URL:
    source: flag
    default: "http://localhost:8271"
    required: true
dirs:
  - $CLAUDE_CONFIG_DIR/hooks
`), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(root, "out")
	target := runtime.GOOS + "/" + runtime.GOARCH
	result, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
		Targets:      []string{target},
	}, StubCredsEncryptor{})
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	binaryPath := filepath.Join(result.OutputDir, expectedBinaryName("mon", runtime.GOOS, runtime.GOARCH))
	if _, err := os.Stat(binaryPath); err != nil {
		t.Fatalf("expected %q, stat error = %v", binaryPath, err)
	}
}

func TestIntegration_BuildThenCheck(t *testing.T) {
	t.Parallel()

	if _, err := exec.LookPath("go"); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			t.Skip("go binary not available")
		}
		t.Fatalf("exec.LookPath(go) error = %v", err)
	}

	root := t.TempDir()
	manifestPath := filepath.Join(root, "manifest.yaml")
	if err := os.WriteFile(manifestPath, []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
  BRIDGE_URL:
    source: flag
    default: "http://localhost:8271"
    required: true
dirs:
  - $CLAUDE_CONFIG_DIR/hooks
`), 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(root, "out")
	target := runtime.GOOS + "/" + runtime.GOARCH
	result, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
		Targets:      []string{target},
	}, StubCredsEncryptor{})
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	binaryPath := filepath.Join(result.OutputDir, expectedBinaryName("mon", runtime.GOOS, runtime.GOARCH))
	cmd := exec.Command(binaryPath, "--check", "--bridge-url", "http://bridge")
	runtimeHome := filepath.Join(root, "runtime-home")
	cmd.Env = append(os.Environ(), "HOME="+runtimeHome)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("worker --check error = %v\n%s", err, output)
	}

	outputText := string(output)
	if !strings.Contains(outputText, "Worker: mon v1.0.0") {
		t.Fatalf("output = %q, want worker header", output)
	}
	if !strings.Contains(outputText, "BRIDGE_URL = http://bridge") {
		t.Fatalf("output = %q, want bridge URL", output)
	}
	if !strings.Contains(outputText, "HOME = "+runtimeHome) {
		t.Fatalf("output = %q, want runtime home", output)
	}
}

func TestInstallSkill_WritesSkillMD(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	if err := InstallSkill(root); err != nil {
		t.Fatalf("InstallSkill() error = %v", err)
	}

	skillPath := filepath.Join(root, "worker-forge-manifest", "SKILL.md")
	data, err := os.ReadFile(skillPath)
	if err != nil {
		t.Fatalf("os.ReadFile(%q) error = %v", skillPath, err)
	}

	content := string(data)
	if !strings.Contains(content, "manifest.yaml") {
		t.Fatalf("SKILL.md = %q, want manifest.yaml documentation", content)
	}
	if !strings.Contains(content, "merge") || !strings.Contains(content, "encrypted") {
		t.Fatalf("SKILL.md = %q, want file flag documentation", content)
	}
}

func TestInstallSkill_ProducesValidSkillMD(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	if err := InstallSkill(root); err != nil {
		t.Fatalf("InstallSkill() error = %v", err)
	}

	data, err := os.ReadFile(filepath.Join(root, "worker-forge-manifest", "SKILL.md"))
	if err != nil {
		t.Fatalf("os.ReadFile(SKILL.md) error = %v", err)
	}

	content := string(data)
	for _, snippet := range []string{
		"vars:",
		"source: env",
		"source: flag",
		"source: creds",
		"source: default",
		"merge: true",
		"encrypted: true",
		"integrity: skip",
		"mon (Ops: DevOps + ProdOps)",
		"luck (PeopleOps: wrap-ups, worker management, identity)",
		"BRIDGE_URL",
		"TMUX_PREFIX",
		"/worker-forge-manifest",
	} {
		if !strings.Contains(content, snippet) {
			t.Fatalf("SKILL.md missing %q\n%s", snippet, content)
		}
	}
}

func TestGeneratedWorkerMain_EmbedsDotfiles(t *testing.T) {
	t.Parallel()

	if !strings.Contains(generatedWorkerMainGo, "//go:embed all:embed/files") {
		t.Fatalf("generatedWorkerMainGo missing all:embed/files directive:\n%s", generatedWorkerMainGo)
	}
}

func TestCLI_ParsesBuildCommand(t *testing.T) {
	t.Parallel()

	cmd, err := ParseCLI([]string{
		"build",
		"mon",
		"--manifest", "workers/mon/manifest.yaml",
		"--identity", "~/.age/manager.key",
		"--target", "linux/amd64,darwin/arm64",
		"--output", "./dist",
	})
	if err != nil {
		t.Fatalf("ParseCLI() error = %v", err)
	}

	if cmd.Name != "build" {
		t.Fatalf("cmd.Name = %q, want %q", cmd.Name, "build")
	}
	if cmd.Build.Identity != "~/.age/manager.key" {
		t.Fatalf("cmd.Build.Identity = %q, want %q", cmd.Build.Identity, "~/.age/manager.key")
	}
	if got := strings.Join(cmd.Build.Targets, ","); got != "linux/amd64,darwin/arm64" {
		t.Fatalf("cmd.Build.Targets = %q, want %q", got, "linux/amd64,darwin/arm64")
	}
	if cmd.Build.OutputDir != "./dist" {
		t.Fatalf("cmd.Build.OutputDir = %q, want %q", cmd.Build.OutputDir, "./dist")
	}
}

func TestCLI_ParsesInstallSkillCommand(t *testing.T) {
	t.Parallel()

	cmd, err := ParseCLI([]string{
		"install-skill",
		"--dir", "/tmp/skills",
	})
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

func TestE2E_BuildAndRunWorker(t *testing.T) {
	t.Parallel()

	buildRoot := t.TempDir()
	if err := os.MkdirAll(filepath.Join(buildRoot, "knowledge"), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(knowledge) error = %v", err)
	}
	if err := os.MkdirAll(filepath.Join(buildRoot, "hooks"), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(hooks) error = %v", err)
	}
	if err := os.MkdirAll(filepath.Join(buildRoot, "secrets"), 0o755); err != nil {
		t.Fatalf("os.MkdirAll(secrets) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(buildRoot, "knowledge", "charter.md"), []byte("charter"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(charter) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(buildRoot, "hooks", "send-to-telegram.sh"), []byte("#!/bin/sh\necho sent\n"), 0o755); err != nil {
		t.Fatalf("os.WriteFile(hook) error = %v", err)
	}
	if err := os.WriteFile(filepath.Join(buildRoot, "secrets", "agent.env"), []byte("TOKEN=secret"), 0o644); err != nil {
		t.Fatalf("os.WriteFile(secret) error = %v", err)
	}

	manifestPath := filepath.Join(buildRoot, "manifest.yaml")
	manifestYAML := []byte(`
name: mon
version: 1.0.0
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "$HOME/.claude"
    required: true
  BRIDGE_URL:
    source: flag
    default: "http://default"
    required: true
dirs:
  - $HOME/team/mon
  - $CLAUDE_CONFIG_DIR/hooks
files:
  - source: knowledge/charter.md
    dest: $HOME/team/mon/charter.md
  - source: hooks/send-to-telegram.sh
    dest: $CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh
  - source: secrets/agent.env
    dest: $HOME/.config/agent.env
    encrypted: true
tools:
  - name: tmux
    check: tmux -V
    required: true
readiness:
  - name: bridge
    check: curl -sf $BRIDGE_URL/health
    expect: exit 0
    required: true
hooks:
  - event: Stop
    command: $CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh
`)
	if err := os.WriteFile(manifestPath, manifestYAML, 0o644); err != nil {
		t.Fatalf("os.WriteFile(manifest) error = %v", err)
	}

	outputDir := filepath.Join(buildRoot, "dist")
	if _, err := Build(BuildConfig{
		ManifestPath: manifestPath,
		Identity:     "age1examplepublickey",
		OutputDir:    outputDir,
		Targets:      []string{"linux/amd64", "darwin/arm64"},
	}, StubCredsEncryptor{}); err != nil {
		t.Fatalf("Build() error = %v", err)
	}

	manifest, err := ParseManifest(mustReadFile(t, filepath.Join(outputDir, "manifest.yaml")))
	if err != nil {
		t.Fatalf("ParseManifest() error = %v", err)
	}
	for i := range manifest.Files {
		manifest.Files[i].ContentKey = buildArtifactKey(manifest.Files[i].Source, manifest.Files[i].Encrypted)
	}

	var checksums map[string]string
	if err := json.Unmarshal(mustReadFile(t, filepath.Join(outputDir, "checksums.json")), &checksums); err != nil {
		t.Fatalf("json.Unmarshal(checksums) error = %v", err)
	}

	runtimeRoot := t.TempDir()
	runtime := &stubRuntime{}
	transport := &appTransportSpy{runtime: runtime}
	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V":                       {ExitCode: 0, Stdout: "tmux 3.4"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}

	app := App{
		Manifest:    manifest,
		Source:      BuiltLayoutSource{Root: outputDir},
		Decryptor:   &StubBundleDecryptor{},
		Verifier:    NewChecksumVerifier(checksums),
		Runner:      runner,
		Runtime:     runtime,
		Transport:   transport,
		HookManager: HookManager{},
		GOOS:        "linux",
	}

	if err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{
				"HOME": runtimeRoot,
			},
			Flags: map[string]string{
				"BRIDGE_URL": "http://bridge",
			},
		},
	}); err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}

	assertFileContent(t, filepath.Join(runtimeRoot, "team", "mon", "charter.md"), "charter")
	assertFileContent(t, filepath.Join(runtimeRoot, ".claude", "hooks", "send-to-telegram.sh"), "#!/bin/sh\necho sent\n")
	assertFileContent(t, filepath.Join(runtimeRoot, ".config", "agent.env"), "TOKEN=secret")
	if _, err := os.Stat(filepath.Join(runtimeRoot, ".claude", "settings.json")); err != nil {
		t.Fatalf("expected settings.json, stat error = %v", err)
	}
	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
	if got := strings.Join(transport.events, ","); got != "connect,register" {
		t.Fatalf("transport.events = %q, want %q", got, "connect,register")
	}
}

func assertFileContent(t *testing.T, path string, want string) {
	t.Helper()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("os.ReadFile(%q) error = %v", path, err)
	}
	if got := string(data); got != want {
		t.Fatalf("%s = %q, want %q", path, got, want)
	}
}

func mustReadFile(t *testing.T, path string) []byte {
	t.Helper()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("os.ReadFile(%q) error = %v", path, err)
	}
	return data
}
