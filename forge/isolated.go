package forge

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type IsolationDriver interface {
	Name() string
	Available() bool
	Build(opts IsolationBuildOpts) error
	Start(opts IsolationStartOpts) error
	Stop(name string) error
	Health(name string) (bool, string, error)
}

type IsolationBuildOpts struct {
	ImageTag   string
	ContextDir string
}

type IsolationStartOpts struct {
	ContainerName string
	ImageTag      string
	Volumes       []string
	Network       string
	Env           []string
	Labels        map[string]string
	Entrypoint    []string
	Args          []string
}

type ContainerDriver struct {
	Binary string
	Runner Runner
}

func (d *ContainerDriver) Name() string {
	return d.Binary
}

func (d *ContainerDriver) Available() bool {
	_, err := exec.LookPath(d.Binary)
	return err == nil
}

func (d *ContainerDriver) Build(opts IsolationBuildOpts) error {
	result, err := d.run(d.Binary, "build", "-t", opts.ImageTag, opts.ContextDir)
	if err != nil {
		return fmt.Errorf("%s build: %w", d.Binary, err)
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("%s build failed: %s", d.Binary, result.Stderr)
	}
	return nil
}

func (d *ContainerDriver) Start(opts IsolationStartOpts) error {
	args := []string{"run", "-d", "--name", opts.ContainerName}
	if opts.Network != "" {
		args = append(args, "--network", opts.Network)
	}
	for _, v := range opts.Volumes {
		args = append(args, "-v", v)
	}
	for _, e := range opts.Env {
		args = append(args, "-e", e)
	}
	for k, v := range opts.Labels {
		args = append(args, "--label", k+"="+v)
	}
	if len(opts.Entrypoint) > 0 {
		args = append(args, "--entrypoint", opts.Entrypoint[0])
	}
	args = append(args, opts.ImageTag)
	if len(opts.Entrypoint) > 1 {
		args = append(args, opts.Entrypoint[1:]...)
	}
	args = append(args, opts.Args...)

	result, err := d.run(d.Binary, args...)
	if err != nil {
		return fmt.Errorf("%s run: %w", d.Binary, err)
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("%s run failed: %s", d.Binary, result.Stderr)
	}
	return nil
}

func (d *ContainerDriver) Stop(name string) error {
	d.run(d.Binary, "stop", name)
	result, err := d.run(d.Binary, "rm", "-f", name)
	if err != nil {
		return fmt.Errorf("%s rm: %w", d.Binary, err)
	}
	if result.ExitCode != 0 {
		return fmt.Errorf("%s rm failed: %s", d.Binary, result.Stderr)
	}
	return nil
}

func (d *ContainerDriver) Health(name string) (bool, string, error) {
	result, err := d.run(d.Binary, "inspect", "--format", "{{.State.Status}}", name)
	if err != nil {
		return false, "", err
	}
	if result.ExitCode != 0 {
		return false, "not found", nil
	}
	status := strings.TrimSpace(result.Stdout)
	return status == "running", status, nil
}

func (d *ContainerDriver) run(name string, args ...string) (RunResult, error) {
	if executor, ok := d.Runner.(CommandExecutor); ok {
		return executor.Execute(name, args...)
	}
	return d.Runner.Run(name + " " + strings.Join(args, " "))
}

func DetectIsolationDriver(runner Runner) (IsolationDriver, error) {
	override := os.Getenv("FORGE_RUNTIME")
	if override != "" {
		driver := &ContainerDriver{Binary: override, Runner: runner}
		if !driver.Available() {
			return nil, fmt.Errorf("FORGE_RUNTIME=%s not found in PATH", override)
		}
		return driver, nil
	}

	for _, name := range []string{"docker", "podman"} {
		driver := &ContainerDriver{Binary: name, Runner: runner}
		if driver.Available() {
			return driver, nil
		}
	}
	return nil, fmt.Errorf("no container runtime found (tried docker, podman)")
}

type IsolatedRunOpts struct {
	Manifest    *Manifest
	Vars        map[string]string
	Identity    string
	BridgeURL   string
	Stdout      io.Writer
	Runner      Runner
	SelfPath    string
	DataDir     string
	ImageTag    string
	SessionPrefix string
}

func RunIsolated(opts IsolatedRunOpts) error {
	stdout := opts.Stdout
	if stdout == nil {
		stdout = os.Stdout
	}

	driver, err := DetectIsolationDriver(opts.Runner)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "Using runtime: %s\n", driver.Name())

	containerName := containerNameFor(opts.Manifest.Name)
	imageTag := opts.ImageTag
	if imageTag == "" {
		imageTag = os.Getenv("FORGE_IMAGE_TAG")
	}
	if imageTag == "" {
		imageTag = fmt.Sprintf("worker-forge/%s:%s", opts.Manifest.Name, opts.Manifest.Version)
	}

	_, existingStatus, _ := driver.Health(containerName)
	if existingStatus != "" && existingStatus != "not found" {
		return fmt.Errorf("container %q already exists (status: %s) — use --stop first", containerName, existingStatus)
	}

	selfPath := opts.SelfPath
	if selfPath == "" {
		selfPath, err = os.Executable()
		if err != nil {
			return fmt.Errorf("find self: %w", err)
		}
	}

	dataDir := opts.DataDir
	if dataDir == "" {
		dataDir = os.Getenv("FORGE_DATA_DIR")
	}
	if dataDir == "" {
		dataDir = fmt.Sprintf("./forge-%s-data", opts.Manifest.Name)
	}
	dataDir, _ = filepath.Abs(dataDir)

	homeDir := filepath.Join(dataDir, "home")
	teamDir := filepath.Join(dataDir, "team")
	os.MkdirAll(homeDir, 0o777)
	os.MkdirAll(teamDir, 0o777)
	os.Chmod(homeDir, 0o777)
	os.Chmod(teamDir, 0o777)

	contextDir, err := buildIsolationContext(selfPath, opts.Manifest)
	if err != nil {
		return fmt.Errorf("build context: %w", err)
	}
	defer os.RemoveAll(contextDir)

	fmt.Fprintf(stdout, "Building image %s...\n", imageTag)
	if err := driver.Build(IsolationBuildOpts{
		ImageTag:   imageTag,
		ContextDir: contextDir,
	}); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "✓ Image built\n")

	volumes := []string{
		homeDir + ":/home/worker",
		teamDir + ":/home/worker/team",
	}
	if opts.Identity != "" {
		absIdentity, _ := filepath.Abs(opts.Identity)
		volumes = append(volumes, absIdentity+":/tmp/identity.agekey:ro")
	}

	var entryArgs []string
	entryArgs = append(entryArgs, "--force-extract")
	if opts.BridgeURL != "" {
		entryArgs = append(entryArgs, "--bridge-url", opts.BridgeURL)
	}
	if opts.Identity != "" {
		entryArgs = append(entryArgs, "--identity", "/tmp/identity.agekey")
	}
	if opts.SessionPrefix != "" {
		entryArgs = append(entryArgs, "--session-prefix", opts.SessionPrefix)
	}

	labels := map[string]string{
		"worker-forge.name":    opts.Manifest.Name,
		"worker-forge.version": opts.Manifest.Version,
	}

	fmt.Fprintf(stdout, "Starting container %s...\n", containerName)
	if err := driver.Start(IsolationStartOpts{
		ContainerName: containerName,
		ImageTag:      imageTag,
		Volumes:       volumes,
		Network:       "host",
		Labels:        labels,
		Args:          entryArgs,
	}); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "✓ Running: %s\n", containerName)
	fmt.Fprintf(stdout, "\n  health: %s --health\n", filepath.Base(selfPath))
	fmt.Fprintf(stdout, "  stop:   %s --stop\n", filepath.Base(selfPath))
	return nil
}

func StopWorker(manifest *Manifest, runner Runner, stdout io.Writer) error {
	if stdout == nil {
		stdout = os.Stdout
	}

	stopped := false

	// Try stopping container (running or exited)
	driver, driverErr := DetectIsolationDriver(runner)
	if driverErr == nil {
		containerName := containerNameFor(manifest.Name)
		_, status, _ := driver.Health(containerName)
		if status != "" && status != "not found" {
			if err := driver.Stop(containerName); err != nil {
				return fmt.Errorf("stop container: %w", err)
			}
			fmt.Fprintf(stdout, "✓ Container %s removed\n", containerName)
			stopped = true
		}
	}

	// Try stopping tmux session
	if executor, ok := runner.(CommandExecutor); ok {
		prefix := "claude-prod-"
		session := prefix + manifest.Name
		result, err := executor.Execute("tmux", "has-session", "-t", session)
		if err == nil && result.ExitCode == 0 {
			executor.Execute("tmux", "kill-session", "-t", session)
			fmt.Fprintf(stdout, "✓ Session %s stopped\n", session)
			stopped = true
		}
	}

	if !stopped {
		fmt.Fprintf(stdout, "Worker %s is not running\n", manifest.Name)
	}
	return nil
}

func HealthCheck(manifest *Manifest, runner Runner, stdout io.Writer) error {
	if stdout == nil {
		stdout = os.Stdout
	}

	fmt.Fprintf(stdout, "Worker: %s v%s\n\n", manifest.Name, manifest.Version)
	found := false

	// Check container
	driver, driverErr := DetectIsolationDriver(runner)
	if driverErr == nil {
		containerName := containerNameFor(manifest.Name)
		running, status, _ := driver.Health(containerName)
		if running {
			fmt.Fprintf(stdout, "  ✓ isolated: running (%s)\n", containerName)
			found = true
		} else if status != "" && status != "not found" {
			fmt.Fprintf(stdout, "  ✗ isolated: %s (%s)\n", status, containerName)
			found = true
		}
	}

	// Check tmux session
	if executor, ok := runner.(CommandExecutor); ok {
		prefix := "claude-prod-"
		session := prefix + manifest.Name
		result, err := executor.Execute("tmux", "has-session", "-t", session)
		if err == nil && result.ExitCode == 0 {
			fmt.Fprintf(stdout, "  ✓ bare metal: running (session %s)\n", session)
			found = true
		}
	}

	if !found {
		fmt.Fprintf(stdout, "  ✗ not running\n")
	}
	return nil
}

func containerNameFor(workerName string) string {
	return "worker-" + workerName
}

func buildIsolationContext(selfPath string, manifest *Manifest) (string, error) {
	contextDir, err := os.MkdirTemp("", "forge-isolated-*")
	if err != nil {
		return "", err
	}

	selfData, err := os.ReadFile(selfPath)
	if err != nil {
		os.RemoveAll(contextDir)
		return "", fmt.Errorf("read self binary: %w", err)
	}
	binaryDest := filepath.Join(contextDir, manifest.Name+"-linux-amd64")
	if err := os.WriteFile(binaryDest, selfData, 0o755); err != nil {
		os.RemoveAll(contextDir)
		return "", err
	}

	dockerfile := generateDockerfile(manifest)
	if err := os.WriteFile(filepath.Join(contextDir, "Dockerfile"), []byte(dockerfile), 0o644); err != nil {
		os.RemoveAll(contextDir)
		return "", err
	}

	return contextDir, nil
}

func generateDockerfile(manifest *Manifest) string {
	var b strings.Builder
	b.WriteString("FROM ubuntu:24.04\n\n")
	b.WriteString("ENV DEBIAN_FRONTEND=noninteractive\n")
	b.WriteString("ENV HOME=/home/worker\n")
	b.WriteString("ENV TZ=UTC\n\n")

	b.WriteString("RUN apt-get update && apt-get install -y --no-install-recommends \\\n")
	b.WriteString("    tmux curl jq python3 git ca-certificates gnupg sudo \\\n")
	b.WriteString("    && rm -rf /var/lib/apt/lists/*\n\n")

	// Install tools based on manifest
	for _, tool := range manifest.Tools {
		if !tool.Required {
			continue
		}
		if install, ok := tool.Install["linux"]; ok {
			b.WriteString(fmt.Sprintf("# %s\n", tool.Name))
			b.WriteString(fmt.Sprintf("RUN %s\n\n", install))
		}
	}

	b.WriteString("# Node.js (for Claude CLI)\n")
	b.WriteString("RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\\n")
	b.WriteString("    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*\n\n")
	b.WriteString("RUN npm install -g @anthropic-ai/claude-code\n\n")

	b.WriteString("# Worker user\n")
	b.WriteString("RUN useradd -m -s /bin/bash -d /home/worker worker \\\n")
	b.WriteString("    && echo \"worker ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/worker\n\n")

	b.WriteString(fmt.Sprintf("COPY %s-linux-amd64 /usr/local/bin/%s\n", manifest.Name, manifest.Name))
	b.WriteString(fmt.Sprintf("RUN chmod 755 /usr/local/bin/%s\n\n", manifest.Name))

	b.WriteString("USER worker\n")
	b.WriteString("WORKDIR /home/worker\n\n")

	b.WriteString(fmt.Sprintf("ENTRYPOINT [\"%s\"]\n", manifest.Name))
	return b.String()
}
