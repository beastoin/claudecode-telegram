package forge

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/signal"
	"runtime"
	"sort"
	"strings"
	"syscall"
)

type WorkerCLIOptions struct {
	BridgeURL     string
	Session        string
	LaunchCommand  string
	Identity       string
	Check          bool
	Verify         bool
	NameOverride   string
	SessionPrefix  string
	ForceExtract   bool
	SkipConflicts  bool
	EmbeddedPath   string
}

type EmbeddedAssets struct {
	Manifest       []byte
	Files          fs.FS
	CredsEncrypted []byte
	Checksums      []byte
}

type WorkerRuntimeFactory func(manifest *Manifest, opts WorkerCLIOptions, runner Runner) Runtime

type WorkerDeps struct {
	Assets         EmbeddedAssets
	Runner         Runner
	Transport      Transport
	HookManager    HookManager
	Decryptor      Decryptor
	GOOS           string
	Host           string
	Stdout         io.Writer
	RuntimeFactory WorkerRuntimeFactory
	Watchdog       WatchdogRunner
}

func ParseWorkerCLI(args []string) (WorkerCLIOptions, error) {
	fs := flag.NewFlagSet("worker", flag.ContinueOnError)
	fs.SetOutput(io.Discard)

	var opts WorkerCLIOptions
	fs.StringVar(&opts.BridgeURL, "bridge-url", "", "bridge URL")
	fs.StringVar(&opts.Session, "session", "", "existing tmux session to adopt")
	fs.StringVar(&opts.LaunchCommand, "launch-command", "", "command to launch inside tmux")
	fs.StringVar(&opts.Identity, "identity", "", "age identity path or key")
	fs.BoolVar(&opts.Check, "check", false, "run readiness checks and exit")
	fs.BoolVar(&opts.Verify, "verify", false, "verify extracted file integrity and exit")
	fs.StringVar(&opts.NameOverride, "name-override", "", "override worker name")
	fs.StringVar(&opts.SessionPrefix, "session-prefix", "", "tmux session name prefix (default: claude-prod-)")
	fs.BoolVar(&opts.ForceExtract, "force-extract", false, "override all conflicting files during extract")
	fs.BoolVar(&opts.SkipConflicts, "skip-conflicts", false, "keep existing files, extract only new ones")
	fs.StringVar(&opts.EmbeddedPath, "show-embedded", "", "print embedded file at path and exit")

	if err := fs.Parse(args); err != nil {
		return WorkerCLIOptions{}, err
	}

	return opts, nil
}

func RunEmbeddedWorker(args []string, deps WorkerDeps) error {
	opts, err := ParseWorkerCLI(args)
	if err != nil {
		return err
	}
	if opts.EmbeddedPath != "" {
		return WriteEmbeddedFile(deps.Assets.Files, opts.EmbeddedPath, deps.Stdout)
	}

	manifest, err := ParseManifest(deps.Assets.Manifest)
	if err != nil {
		return fmt.Errorf("parse embedded manifest: %w", err)
	}
	if opts.NameOverride != "" {
		manifest.Name = opts.NameOverride
	}
	prepareEmbeddedManifest(manifest)

	runner := deps.Runner
	if runner == nil {
		runner = ShellRunner{}
	}

	resolve := ResolveOptions{
		Env: currentEnvMap(),
		Flags: map[string]string{
			"BRIDGE_URL": opts.BridgeURL,
		},
	}
	if deps.Host != "" {
		resolve.Env["HOST"] = deps.Host
	}

	decryptor, err := resolveWorkerDecryptor(opts, deps.Decryptor)
	if err != nil {
		return err
	}

	resolve.Creds, err = ResolveCredsFromBundle(manifest, decryptor, deps.Assets.CredsEncrypted)
	if err != nil {
		return err
	}

	checksums, err := parseChecksums(deps.Assets.Checksums)
	if err != nil {
		return err
	}

	if opts.Check {
		report, err := RunCheckMode(manifest, CheckOptions{
			Resolve: resolve,
			GOOS:    firstNonEmptyString(deps.GOOS, runtime.GOOS),
			Runner:  runner,
		})
		if err != nil {
			return err
		}
		return writeString(deps.Stdout, FormatCheckReport(*report, firstNonEmptyString(deps.GOOS, runtime.GOOS)))
	}

	if opts.Verify {
		report, err := RunVerifyMode(manifest, VerifyOptions{
			Resolve:   resolve,
			Checksums: checksums,
		})
		if err != nil {
			return err
		}
		return writeString(deps.Stdout, FormatVerifyReport(*report))
	}

	runtimeFactory := deps.RuntimeFactory
	if runtimeFactory == nil {
		runtimeFactory = defaultWorkerRuntimeFactory
	}

	app := App{
		Manifest:    manifest,
		Source:      FSFileSource{FS: deps.Assets.Files, CredsEncrypted: deps.Assets.CredsEncrypted},
		Decryptor:   decryptor,
		Verifier:    NewChecksumVerifier(checksums),
		Runner:      runner,
		Runtime:     runtimeFactory(manifest, opts, runner),
		Transport:   deps.Transport,
		Watchdog:    deps.Watchdog,
		HookManager: deps.HookManager,
		GOOS:        firstNonEmptyString(deps.GOOS, runtime.GOOS),
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	return app.Run(RunOptions{
		Context:       ctx,
		Resolve:       resolve,
		ForceExtract:  opts.ForceExtract,
		SkipConflicts: opts.SkipConflicts,
	})
}

func parseChecksums(data []byte) (map[string]string, error) {
	if len(data) == 0 {
		return map[string]string{}, nil
	}

	var raw map[string]string
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse embedded checksums: %w", err)
	}
	checksums := make(map[string]string, len(raw))
	for key, value := range raw {
		checksums[key] = value
		stripped := strings.TrimPrefix(key, "files/")
		stripped = strings.TrimPrefix(stripped, "creds/")
		if stripped != key {
			checksums[stripped] = value
		}
	}
	return checksums, nil
}

func defaultWorkerRuntimeFactory(manifest *Manifest, opts WorkerCLIOptions, runner Runner) Runtime {
	prefix := "claude-prod-"
	if opts.SessionPrefix != "" {
		prefix = opts.SessionPrefix
	}
	tmuxRuntime := &TmuxRuntime{
		Runner:        runner,
		Session:       prefix + manifest.Name,
		AdoptSession:  opts.Session,
		LaunchCommand: opts.LaunchCommand,
	}
	if executor, ok := runner.(CommandExecutor); ok {
		tmuxRuntime.Commander = executor
	}
	return tmuxRuntime
}

func resolveWorkerDecryptor(opts WorkerCLIOptions, decryptor Decryptor) (Decryptor, error) {
	if decryptor != nil {
		return decryptor, nil
	}

	identity := strings.TrimSpace(opts.Identity)
	if identity == "" {
		identity = strings.TrimSpace(os.Getenv("AGE_IDENTITY"))
	}
	if identity == "" {
		return &StubBundleDecryptor{}, nil
	}

	identities, err := ParseAgeIdentities(identity)
	if err != nil {
		return nil, err
	}
	return AgeBundleDecryptor{Identities: identities}, nil
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

func writeString(w io.Writer, value string) error {
	if w == nil {
		return nil
	}
	_, err := io.WriteString(w, value)
	return err
}

func WriteEmbeddedFile(files fs.FS, embeddedPath string, w io.Writer) error {
	embeddedPath = strings.TrimPrefix(embeddedPath, "/")
	embeddedPath = strings.TrimPrefix(embeddedPath, "embed/files/")
	embeddedPath = strings.TrimPrefix(embeddedPath, "files/")
	if embeddedPath == "" || embeddedPath == "." || !fs.ValidPath(embeddedPath) {
		return fmt.Errorf("invalid embedded path %q", embeddedPath)
	}

	data, err := fs.ReadFile(files, embeddedPath)
	if err != nil {
		return fmt.Errorf("read embedded file %q: %w", embeddedPath, err)
	}
	if w == nil {
		return nil
	}
	_, err = w.Write(data)
	return err
}

func FormatCheckReport(report CheckReport, goos string) string {
	var builder strings.Builder
	builder.WriteString(fmt.Sprintf("Worker: %s v%s\n\n", report.Worker, report.Version))
	builder.WriteString("Preparation:\n")
	for _, key := range sortedResolvedVarKeys(report.ResolvedVars) {
		builder.WriteString(fmt.Sprintf("  %s = %s\n", key, report.ResolvedVars[key]))
	}

	builder.WriteString("\nTools:\n")
	if len(report.Tools) == 0 {
		builder.WriteString("  (none)\n")
	}
	for _, tool := range report.Tools {
		status := "✗"
		if tool.Installed {
			status = "✓"
		}
		line := fmt.Sprintf("  %s %s", status, tool.Name)
		if tool.Installed {
			if tool.Version != "" {
				line += " " + tool.Version
			}
			if tool.InstalledByBootstrap {
				line += " (installed)"
			}
		} else {
			line += ": not found"
			if tool.Required {
				line += " (required)"
			}
		}
		builder.WriteString(line + "\n")
	}

	builder.WriteString("\nReadiness:\n")
	if len(report.Readiness) == 0 {
		builder.WriteString("  (none)\n")
	}
	for _, readiness := range report.Readiness {
		status := "✗"
		if readiness.Passed {
			status = "✓"
		}
		line := fmt.Sprintf("  %s %s", status, readiness.Name)
		if !readiness.Passed && readiness.Output != "" {
			line += ": " + readiness.Output
		}
		if readiness.Fixed {
			line += " (fixed)"
		}
		builder.WriteString(line + "\n")
	}

	status := "READY"
	if !checkReportReady(report) {
		status = "NOT READY"
	}
	builder.WriteString(fmt.Sprintf("\nStatus: %s\n", status))

	_ = goos
	return builder.String()
}

func FormatVerifyReport(report VerifyReport) string {
	var builder strings.Builder
	builder.WriteString(fmt.Sprintf("Worker: %s v%s\n", report.Worker, report.Version))
	builder.WriteString("Integrity Check:\n\n")
	for _, verified := range report.Verified {
		builder.WriteString(fmt.Sprintf("  ✓ %-28s matches\n", verified))
	}
	for _, skipped := range report.Skipped {
		builder.WriteString(fmt.Sprintf("  ⊘ %-28s skipped\n", skipped))
	}
	builder.WriteString(fmt.Sprintf("\nStatus: VERIFIED (%d checked, %d skipped)\n", len(report.Verified), len(report.Skipped)))
	return builder.String()
}

func sortedResolvedVarKeys(values map[string]string) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func checkReportReady(report CheckReport) bool {
	for _, tool := range report.Tools {
		if tool.Required && !tool.Installed {
			return false
		}
	}
	for _, readiness := range report.Readiness {
		if readiness.Required && !readiness.Passed {
			return false
		}
	}
	return true
}

func currentEnvMap() map[string]string {
	env := make(map[string]string)
	for _, entry := range os.Environ() {
		key, value, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		env[key] = value
	}
	return env
}

func prepareEmbeddedManifest(manifest *Manifest) {
	if manifest == nil {
		return
	}

	for i := range manifest.Files {
		manifest.Files[i].ContentKey = embeddedRuntimeSourceKey(manifest.Files[i].Source, manifest.Files[i].Encrypted)
	}
}

func embeddedRuntimeSourceKey(source string, encrypted bool) string {
	if encrypted {
		return buildArtifactKey(source, true)
	}
	return canonicalSource(source)
}
