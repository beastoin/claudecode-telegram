package forge

import (
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var Version = "dev"

type CLICommand struct {
	Name     string
	Worker   string
	Build    BuildConfig
	SkillDir string
}

func ParseCLI(args []string) (CLICommand, error) {
	if len(args) == 0 {
		return CLICommand{}, &UsageError{ShowHelp: true}
	}

	switch args[0] {
	case "build":
		return parseBuildCLI(args[1:])
	case "install-skill":
		return parseInstallSkillCLI(args[1:])
	case "help", "--help", "-h":
		return CLICommand{}, &UsageError{ShowHelp: true}
	case "version", "--version":
		return CLICommand{Name: "version"}, nil
	default:
		return CLICommand{}, &UsageError{
			Message: fmt.Sprintf("unknown command %q", args[0]),
			Hint:    "Run 'worker-forge help' for available commands.",
		}
	}
}

func parseBuildCLI(args []string) (CLICommand, error) {
	if len(args) == 0 {
		return CLICommand{}, &UsageError{
			Message: "worker name is required",
			Hint:    "Usage: worker-forge build <worker> --manifest <path> [flags]",
		}
	}

	if args[0] == "--help" || args[0] == "-h" {
		return CLICommand{}, &UsageError{ShowHelp: true, Command: "build"}
	}

	worker := args[0]
	fs := flag.NewFlagSet("build", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var manifestPath string
	var identity string
	var targets string
	var outputDir string

	fs.StringVar(&manifestPath, "manifest", "", "")
	fs.StringVar(&identity, "identity", "", "")
	fs.StringVar(&targets, "target", "", "")
	fs.StringVar(&outputDir, "output", "", "")

	if err := fs.Parse(args[1:]); err != nil {
		return CLICommand{}, &UsageError{
			Message: err.Error(),
			Hint:    "Run 'worker-forge build --help' for flag details.",
		}
	}

	if manifestPath == "" {
		defaultPath := filepath.Join("workers", worker, "manifest.yaml")
		if _, err := os.Stat(defaultPath); err == nil {
			manifestPath = defaultPath
		} else {
			return CLICommand{}, &UsageError{
				Message: "manifest path is required",
				Hint:    fmt.Sprintf("Usage: worker-forge build %s --manifest <path>", worker),
			}
		}
	}

	if outputDir == "" {
		outputDir = filepath.Join("build", worker)
	}

	var parsedTargets []string
	if targets != "" {
		parsedTargets = strings.Split(targets, ",")
	}

	return CLICommand{
		Name:   "build",
		Worker: worker,
		Build: BuildConfig{
			ManifestPath: manifestPath,
			Identity:     identity,
			Targets:      parsedTargets,
			OutputDir:    outputDir,
		},
	}, nil
}

func parseInstallSkillCLI(args []string) (CLICommand, error) {
	if len(args) > 0 && (args[0] == "--help" || args[0] == "-h") {
		return CLICommand{}, &UsageError{ShowHelp: true, Command: "install-skill"}
	}

	fs := flag.NewFlagSet("install-skill", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var dir string
	fs.StringVar(&dir, "dir", "", "")
	if err := fs.Parse(args); err != nil {
		return CLICommand{}, &UsageError{
			Message: err.Error(),
			Hint:    "Run 'worker-forge install-skill --help' for details.",
		}
	}
	if dir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return CLICommand{}, err
		}
		dir = filepath.Join(home, ".claude", "skills")
	}

	return CLICommand{
		Name:     "install-skill",
		SkillDir: dir,
	}, nil
}

type UsageError struct {
	Message string
	Hint    string
	Command string
	ShowHelp bool
}

func (e *UsageError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return "usage error"
}

type CLIOutput struct {
	w     io.Writer
	color bool
}

func NewCLIOutput(w io.Writer, isTTY bool) *CLIOutput {
	color := isTTY
	if os.Getenv("NO_COLOR") != "" {
		color = false
	}
	if os.Getenv("TERM") == "dumb" {
		color = false
	}
	return &CLIOutput{w: w, color: color}
}

func (o *CLIOutput) Bold(s string) string {
	if !o.color {
		return s
	}
	return "\033[1m" + s + "\033[0m"
}

func (o *CLIOutput) Green(s string) string {
	if !o.color {
		return s
	}
	return "\033[32m" + s + "\033[0m"
}

func (o *CLIOutput) Red(s string) string {
	if !o.color {
		return s
	}
	return "\033[31m" + s + "\033[0m"
}

func (o *CLIOutput) Yellow(s string) string {
	if !o.color {
		return s
	}
	return "\033[33m" + s + "\033[0m"
}

func (o *CLIOutput) Dim(s string) string {
	if !o.color {
		return s
	}
	return "\033[2m" + s + "\033[0m"
}

func (o *CLIOutput) Cyan(s string) string {
	if !o.color {
		return s
	}
	return "\033[36m" + s + "\033[0m"
}

func (o *CLIOutput) Printf(format string, args ...any) {
	fmt.Fprintf(o.w, format, args...)
}

func (o *CLIOutput) Println(args ...any) {
	fmt.Fprintln(o.w, args...)
}

func (o *CLIOutput) PrintVersion() {
	o.Printf("worker-forge %s\n", Version)
}

func (o *CLIOutput) PrintHelp(command string) {
	switch command {
	case "build":
		o.printBuildHelp()
	case "install-skill":
		o.printInstallSkillHelp()
	default:
		o.printMainHelp()
	}
}

func (o *CLIOutput) printMainHelp() {
	o.Println(o.Bold("worker-forge") + " — package Claude Code agents into standalone binaries")
	o.Println()
	o.Println(o.Bold("USAGE"))
	o.Printf("  worker-forge <command> [flags]\n")
	o.Println()
	o.Println(o.Bold("COMMANDS"))
	o.Printf("  %-18s Build a worker binary from a manifest\n", o.Cyan("build"))
	o.Printf("  %-18s Install /worker-forge-manifest skill for Claude Code\n", o.Cyan("install-skill"))
	o.Printf("  %-18s Show this help\n", o.Cyan("help"))
	o.Printf("  %-18s Show version\n", o.Cyan("version"))
	o.Println()
	o.Println(o.Bold("EXAMPLES"))
	o.Println(o.Dim("  # Build mon worker for Linux"))
	o.Printf("  worker-forge build mon --manifest workers/mon/manifest.yaml --target linux/amd64\n")
	o.Println()
	o.Println(o.Dim("  # Build with age-encrypted creds, multiple targets"))
	o.Printf("  worker-forge build mon --manifest workers/mon/manifest.yaml \\\n")
	o.Printf("    --identity ~/.age/manager.pub --target linux/amd64,darwin/arm64\n")
	o.Println()
	o.Println(o.Dim("  # Install the manifest-generation skill"))
	o.Printf("  worker-forge install-skill\n")
	o.Println()
	o.Println(o.Bold("WORKER BINARY FLAGS") + o.Dim(" (for the generated binary)"))
	o.Printf("  --bridge-url <url>     Bridge endpoint for registration and messaging\n")
	o.Printf("  --session <name>       Adopt existing tmux session instead of creating one\n")
	o.Printf("  --identity <path>      Age identity for credential decryption\n")
	o.Printf("  --check                Run readiness checks and exit\n")
	o.Printf("  --verify               Verify extracted file integrity and exit\n")
	o.Printf("  --force-extract        Override conflicting files during extract\n")
	o.Printf("  --skip-conflicts       Keep existing files, extract only new ones\n")
	o.Println()
	o.Printf("  %s\n", o.Dim("https://github.com/beastoin/claudecode-telegram"))
}

func (o *CLIOutput) printBuildHelp() {
	o.Println(o.Bold("worker-forge build") + " — build a worker binary from a manifest")
	o.Println()
	o.Println(o.Bold("USAGE"))
	o.Printf("  worker-forge build <worker-name> [flags]\n")
	o.Println()
	o.Println(o.Bold("ARGUMENTS"))
	o.Printf("  %-20s Name of the worker (used as binary name)\n", o.Cyan("<worker-name>"))
	o.Println()
	o.Println(o.Bold("FLAGS"))
	o.Printf("  --manifest <path>    Path to manifest.yaml %s\n", o.Dim("[default: workers/<name>/manifest.yaml]"))
	o.Printf("  --identity <key>     Age public key or path for credential encryption\n")
	o.Printf("  --target <os/arch>   Build targets, comma-separated %s\n", o.Dim("[e.g. linux/amd64,darwin/arm64]"))
	o.Printf("  --output <dir>       Output directory %s\n", o.Dim("[default: build/<name>]"))
	o.Println()
	o.Println(o.Bold("EXAMPLES"))
	o.Println(o.Dim("  # Minimal build (layout only, no compiled binary)"))
	o.Printf("  worker-forge build mon --manifest workers/mon/manifest.yaml\n")
	o.Println()
	o.Println(o.Dim("  # Build for Linux with encrypted creds"))
	o.Printf("  worker-forge build mon \\\n")
	o.Printf("    --manifest workers/mon/manifest.yaml \\\n")
	o.Printf("    --identity age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p \\\n")
	o.Printf("    --target linux/amd64\n")
	o.Println()
	o.Println(o.Dim("  # Cross-compile for VPS + Mac Mini"))
	o.Printf("  worker-forge build luck \\\n")
	o.Printf("    --manifest workers/luck/manifest.yaml \\\n")
	o.Printf("    --identity ~/.age/manager.pub \\\n")
	o.Printf("    --target linux/amd64,darwin/arm64 \\\n")
	o.Printf("    --output ./dist\n")
	o.Println()
	o.Println(o.Bold("NOTES"))
	o.Printf("  Credentials (source: creds) must be set in the build environment.\n")
	o.Printf("  The build collects files, encrypts creds, generates checksums,\n")
	o.Printf("  and optionally cross-compiles standalone Go binaries.\n")
}

func (o *CLIOutput) printInstallSkillHelp() {
	o.Println(o.Bold("worker-forge install-skill") + " — install the manifest-generation skill")
	o.Println()
	o.Println(o.Bold("USAGE"))
	o.Printf("  worker-forge install-skill [flags]\n")
	o.Println()
	o.Println(o.Bold("FLAGS"))
	o.Printf("  --dir <path>    Target skills directory %s\n", o.Dim("[default: ~/.claude/skills]"))
	o.Println()
	o.Println(o.Bold("DESCRIPTION"))
	o.Printf("  Installs a Claude Code skill that teaches agents how to generate\n")
	o.Printf("  their own manifest.yaml. Agents invoke /worker-forge-manifest to\n")
	o.Printf("  introspect their environment and produce a valid manifest.\n")
	o.Println()
	o.Println(o.Bold("EXAMPLES"))
	o.Printf("  worker-forge install-skill\n")
	o.Printf("  worker-forge install-skill --dir /opt/skills\n")
}

func (o *CLIOutput) PrintBuildStart(worker, manifest string, targets []string) {
	o.Printf("%s %s\n", o.Bold("Building"), o.Cyan(worker))
	o.Printf("  manifest:  %s\n", manifest)
	if len(targets) > 0 {
		o.Printf("  targets:   %s\n", strings.Join(targets, ", "))
	}
}

func (o *CLIOutput) PrintBuildResult(result *BuildResult) {
	o.Println()
	if len(result.Artifacts) > 0 {
		o.Printf("%s Built %d artifact(s):\n", o.Green("✓"), len(result.Artifacts))
		for _, a := range result.Artifacts {
			o.Printf("  %s\n", a)
		}
	} else {
		o.Printf("%s Layout written to %s\n", o.Green("✓"), result.OutputDir)
		o.Println(o.Dim("  No --target specified; skipped binary compilation."))
	}
}

func (o *CLIOutput) PrintInstallSkillResult(dir string) {
	o.Printf("%s Installed %s\n", o.Green("✓"), o.Cyan("worker-forge-manifest"))
	o.Printf("  %s\n", filepath.Join(dir, "worker-forge-manifest", "SKILL.md"))
	o.Println(o.Dim("  Agents can now use /worker-forge-manifest to generate manifests."))
}

func (o *CLIOutput) PrintError(err error) {
	var ue *UsageError
	if isUsageError(err, &ue) {
		if ue.ShowHelp {
			o.PrintHelp(ue.Command)
			return
		}
		o.Printf("%s %s\n", o.Red("Error:"), ue.Message)
		if ue.Hint != "" {
			o.Println(o.Dim(ue.Hint))
		}
		return
	}

	var ce *ExtractConflictError
	if isConflictError(err, &ce) {
		o.Println(o.Yellow("Extract conflict detected:"))
		o.Println()
		for _, c := range ce.Conflicts {
			o.Printf("  %s  %s\n", o.Red("CONFLICT"), c.Path)
			o.Printf("             Existing: %d bytes, modified %s\n", c.ExistingSize, c.ExistingMod)
			o.Printf("             Embedded: %d bytes\n", c.EmbeddedSize)
			o.Println()
		}
		o.Printf("%d conflict(s). Options:\n", len(ce.Conflicts))
		o.Printf("  %s     Override all conflicting files\n", o.Cyan("--force-extract"))
		o.Printf("  %s     Keep existing files, extract only new ones\n", o.Cyan("--skip-conflicts"))
		return
	}

	o.Printf("%s %s\n", o.Red("Error:"), err)
}

func isUsageError(err error, target **UsageError) bool {
	if ue, ok := err.(*UsageError); ok {
		*target = ue
		return true
	}
	return false
}

func isConflictError(err error, target **ExtractConflictError) bool {
	if ce, ok := err.(*ExtractConflictError); ok {
		*target = ce
		return true
	}
	return false
}
