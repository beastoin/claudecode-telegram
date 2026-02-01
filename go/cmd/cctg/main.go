// Package main provides the CLI entry point for cctg.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"

	"github.com/beastoin/claudecode-telegram/internal/app"
	"github.com/beastoin/claudecode-telegram/internal/tunnel"
)

const version = "dev"

func main() {
	if err := run(os.Args, os.Stdin, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) error {
	cmd := getCommand(args)

	switch cmd {
	case "serve":
		return runServe(args[2:])
	case "hook":
		return runHook(args[2:], stdin)
	case "webhook":
		return runWebhook(args[2:], stdout)
	case "tunnel":
		return runTunnel(args[2:], stdout)
	case "version":
		printVersion(stdout)
		return nil
	case "help":
		printUsage(stdout)
		return nil
	default:
		printUsage(stderr)
		return fmt.Errorf("unknown command: %s", cmd)
	}
}

func getCommand(args []string) string {
	if len(args) < 2 {
		return "help"
	}

	arg := args[1]
	switch arg {
	case "serve", "hook", "webhook", "tunnel", "version":
		return arg
	case "-h", "--help", "help":
		return "help"
	case "-v", "--version":
		return "version"
	default:
		return arg
	}
}

func printVersion(w io.Writer) {
	fmt.Fprintf(w, "cctg %s\n", version)
}

func printUsage(w io.Writer) {
	fmt.Fprintf(w, `cctg - Claude Code Telegram Gateway

Usage: cctg <command> [options]

Commands:
  serve         Start the webhook server
  hook          Send response to bridge (used by Claude's Stop hook)
  hook install  Install Claude Code stop hook to settings.json
  webhook       Register webhook URL with Telegram
  tunnel        Start cloudflared tunnel + auto-register webhook
  version       Print version information
  help          Show this help message

Serve options:
  --token    Telegram bot token (or TELEGRAM_BOT_TOKEN env)
  --admin    Admin chat ID (or ADMIN_CHAT_ID env)
  --node     Node name: prod|dev|test|custom (or NODE_NAME env)
             Sets port/prefix defaults: prod=8081, dev=8082, test=8095
  --port     HTTP server port (overrides node default, or PORT env)
  --prefix   tmux session prefix (overrides node default, or TMUX_PREFIX env)
  --json     Use JSON structured logging
  --sandbox          Run workers in Docker containers (default: disabled)
  --no-sandbox       Run workers directly (--dangerously-skip-permissions)
  --sandbox-image    Docker image for workers (or SANDBOX_IMAGE env)
  --mount            Extra mount for sandbox (repeatable, or SANDBOX_MOUNTS env)
  --mount-ro         Extra read-only mount for sandbox (repeatable)

Hook options:
  --url      Bridge URL (or BRIDGE_URL env)
  --session  Session name (or SESSION_NAME env)

Webhook options:
  --token    Telegram bot token (or TELEGRAM_BOT_TOKEN env)
  --url      Webhook URL (required)

Tunnel options:
  --token          Telegram bot token (or TELEGRAM_BOT_TOKEN env)
  --url            Local server URL to expose (defaults to localhost:PORT)
  --node           Node name: prod|dev|test|custom (or NODE_NAME env)
  --port           HTTP server port (overrides node default, or PORT env)
  --webhook-path   Webhook path to register (default: /webhook)
  --cloudflared    Path to cloudflared binary (default: cloudflared)

Examples:
  cctg serve --token "123:ABC" --admin "999"
  cctg hook --url "http://localhost:8080/response" --session "alice" < response.txt
  cctg hook install
  cctg webhook --url "https://example.com/webhook"
  cctg tunnel --token "123:ABC" --port 8081

`)
}

func runServe(args []string) error {
	cfg, err := parseServeFlags(args)
	if err != nil {
		return err
	}

	if err := cfg.Validate(); err != nil {
		return err
	}

	if cfg.SandboxEnabled && !dockerAvailable() {
		fmt.Fprintln(os.Stderr, "Warning: Docker not found - falling back to direct execution")
		cfg.SandboxEnabled = false
	}

	if cfg.SandboxEnabled {
		fmt.Println("Sandbox mode enabled - workers run in Docker containers")
		if home, err := os.UserHomeDir(); err == nil {
			fmt.Printf("Default mount: %s -> /workspace\n", home)
		}
		if cfg.SandboxMounts != "" {
			fmt.Printf("Extra mounts: %s\n", cfg.SandboxMounts)
		}
	} else {
		fmt.Println("Sandbox mode disabled - workers run directly (--dangerously-skip-permissions)")
	}

	a, err := app.New(cfg)
	if err != nil {
		return fmt.Errorf("failed to create app: %w", err)
	}

	// Set up signal handling for graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigCh
		fmt.Println("\nShutting down...")
		cancel()
	}()

	fmt.Printf("Starting server on port %s...\n", cfg.Port)
	return a.Run(ctx)
}

func dockerAvailable() bool {
	_, err := exec.LookPath("docker")
	return err == nil
}

func parseServeFlags(args []string) (app.Config, error) {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)

	// Get defaults from environment
	envCfg := app.ConfigFromEnv()

	token := fs.String("token", envCfg.Token, "Telegram bot token")
	admin := fs.String("admin", envCfg.AdminChatID, "Admin chat ID")
	node := fs.String("node", envCfg.NodeName, "Node name (prod, dev, test, or custom)")
	port := fs.String("port", envCfg.Port, "HTTP server port")
	prefix := fs.String("prefix", envCfg.Prefix, "tmux session prefix")
	sessionsDir := fs.String("sessions-dir", envCfg.SessionsDir, "Sessions directory")
	jsonLog := fs.Bool("json", false, "Use JSON structured logging")
	sandbox := fs.Bool("sandbox", envCfg.SandboxEnabled, "Run workers in Docker containers")
	noSandbox := fs.Bool("no-sandbox", false, "Run workers directly (--dangerously-skip-permissions)")
	sandboxImage := fs.String("sandbox-image", envCfg.SandboxImage, "Docker image for workers")

	mountSpecs := splitMountSpecs(envCfg.SandboxMounts)
	fs.Func("mount", "Extra mount for sandbox (repeatable)", func(value string) error {
		value = strings.TrimSpace(value)
		if value != "" {
			mountSpecs = append(mountSpecs, value)
		}
		return nil
	})
	fs.Func("mount-ro", "Extra read-only mount for sandbox (repeatable)", func(value string) error {
		value = strings.TrimSpace(value)
		if value != "" {
			mountSpecs = append(mountSpecs, "ro:"+value)
		}
		return nil
	})

	if err := fs.Parse(args); err != nil {
		return app.Config{}, err
	}

	cfg := app.Config{
		Token:          *token,
		AdminChatID:    *admin,
		NodeName:       *node,
		Port:           *port,
		Prefix:         *prefix,
		SessionsDir:    *sessionsDir,
		JSONLog:        *jsonLog,
		SandboxEnabled: *sandbox,
		SandboxImage:   *sandboxImage,
		SandboxMounts:  strings.Join(mountSpecs, ","),
		BridgeURL:      envCfg.BridgeURL,
	}
	if *noSandbox {
		cfg.SandboxEnabled = false
	}

	// Derive defaults based on node name (only fills in empty fields)
	cfg.DeriveNodeConfig()

	return cfg, nil
}

func splitMountSpecs(spec string) []string {
	if strings.TrimSpace(spec) == "" {
		return nil
	}
	var parts []string
	for _, part := range strings.Split(spec, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		parts = append(parts, part)
	}
	return parts
}

func runHook(args []string, stdin io.Reader) error {
	// Check for subcommand
	if len(args) > 0 && args[0] == "install" {
		homeDir, err := os.UserHomeDir()
		if err != nil {
			return fmt.Errorf("failed to get home directory: %w", err)
		}
		return runHookInstall(homeDir+"/.claude", os.Stdout)
	}

	cfg, err := parseHookFlags(args)
	if err != nil {
		return err
	}

	if err := cfg.Validate(); err != nil {
		return err
	}

	return app.RunHook(cfg, stdin)
}

// getHookSubcommand returns the subcommand for the hook command.
func getHookSubcommand(args []string) string {
	// args: ["cctg", "hook", ...]
	if len(args) < 3 {
		return ""
	}
	subcmd := args[2]
	// Only return if it's a known subcommand, not a flag
	if subcmd == "install" {
		return subcmd
	}
	return ""
}

func runHookInstall(claudeDir string, stdout io.Writer) error {
	settingsPath := claudeDir + "/settings.json"

	// Read existing settings or create empty map
	var settings map[string]interface{}
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return fmt.Errorf("failed to read settings.json: %w", err)
		}
		settings = make(map[string]interface{})
	} else {
		if err := json.Unmarshal(data, &settings); err != nil {
			return fmt.Errorf("failed to parse settings.json: %w", err)
		}
	}

	// Ensure hooks map exists
	hooks, ok := settings["hooks"].(map[string]interface{})
	if !ok {
		hooks = make(map[string]interface{})
		settings["hooks"] = hooks
	}

	// Create the Stop hook entry
	stopHook := []map[string]interface{}{
		{
			"hooks": []map[string]interface{}{
				{
					"type":    "command",
					"command": `cctg hook --url "$BRIDGE_URL" --session "$CLAUDE_SESSION_NAME"`,
				},
			},
		},
	}
	hooks["Stop"] = stopHook

	// Marshal with indentation
	output, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal settings: %w", err)
	}

	// Ensure directory exists
	if err := os.MkdirAll(claudeDir, 0700); err != nil {
		return fmt.Errorf("failed to create directory: %w", err)
	}

	// Write settings file
	if err := os.WriteFile(settingsPath, output, 0600); err != nil {
		return fmt.Errorf("failed to write settings.json: %w", err)
	}

	fmt.Fprintf(stdout, "Hook installed to %s\n", settingsPath)
	return nil
}

// runHookWithReader is used for testing
func runHookWithReader(cfg app.HookConfig, r io.Reader) error {
	return app.RunHook(cfg, r)
}

func parseHookFlags(args []string) (app.HookConfig, error) {
	fs := flag.NewFlagSet("hook", flag.ContinueOnError)

	// Get defaults from environment
	envCfg := app.HookConfigFromEnv()

	url := fs.String("url", envCfg.BridgeURL, "Bridge URL")
	session := fs.String("session", envCfg.Session, "Session name")

	if err := fs.Parse(args); err != nil {
		return app.HookConfig{}, err
	}

	return app.HookConfig{
		BridgeURL: *url,
		Session:   *session,
	}, nil
}

// webhookConfig holds the configuration for the webhook command.
type webhookConfig struct {
	Token   string // Telegram bot token
	URL     string // Webhook URL
	baseURL string // Base URL for Telegram API (for testing)
}

// Validate checks that required fields are set.
func (c webhookConfig) Validate() error {
	if c.Token == "" {
		return fmt.Errorf("token is required (set TELEGRAM_BOT_TOKEN or use --token)")
	}
	if c.URL == "" {
		return fmt.Errorf("url is required (use --url)")
	}
	return nil
}

func runWebhook(args []string, stdout io.Writer) error {
	cfg, err := parseWebhookFlags(args)
	if err != nil {
		return err
	}

	if err := cfg.Validate(); err != nil {
		return err
	}

	return runWebhookCommand(cfg, stdout)
}

func parseWebhookFlags(args []string) (webhookConfig, error) {
	fs := flag.NewFlagSet("webhook", flag.ContinueOnError)

	// Get defaults from environment
	token := fs.String("token", os.Getenv("TELEGRAM_BOT_TOKEN"), "Telegram bot token")
	url := fs.String("url", "", "Webhook URL")

	if err := fs.Parse(args); err != nil {
		return webhookConfig{}, err
	}

	return webhookConfig{
		Token: *token,
		URL:   *url,
	}, nil
}

func runWebhookCommand(cfg webhookConfig, stdout io.Writer) error {
	client := app.NewTelegramClientForWebhook(cfg.Token, cfg.baseURL)
	if err := client.SetWebhook(cfg.URL); err != nil {
		return fmt.Errorf("failed to set webhook: %w", err)
	}

	fmt.Fprintf(stdout, "Webhook registered: %s\n", cfg.URL)
	return nil
}

// tunnelConfig holds the configuration for the tunnel command.
type tunnelConfig struct {
	Token           string // Telegram bot token
	LocalURL        string // Local URL to expose
	WebhookPath     string // Path to register with Telegram
	CloudflaredPath string // cloudflared binary path
	baseURL         string // Base URL for Telegram API (for testing)
}

// Validate checks that required fields are set.
func (c tunnelConfig) Validate() error {
	if c.Token == "" {
		return fmt.Errorf("token is required (set TELEGRAM_BOT_TOKEN or use --token)")
	}
	if c.LocalURL == "" {
		return fmt.Errorf("url is required (use --url or --port)")
	}
	return nil
}

func runTunnel(args []string, stdout io.Writer) error {
	cfg, err := parseTunnelFlags(args)
	if err != nil {
		return err
	}

	if err := cfg.Validate(); err != nil {
		return err
	}

	// Set up signal handling for graceful shutdown
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		<-sigCh
		fmt.Fprintln(stdout, "\nShutting down...")
		cancel()
	}()

	fmt.Fprintf(stdout, "Starting tunnel for %s...\n", cfg.LocalURL)
	return runTunnelCommand(ctx, cfg, stdout)
}

func parseTunnelFlags(args []string) (tunnelConfig, error) {
	fs := flag.NewFlagSet("tunnel", flag.ContinueOnError)

	envCfg := app.ConfigFromEnv()
	envCfg.DeriveNodeConfig()

	token := fs.String("token", envCfg.Token, "Telegram bot token")
	node := fs.String("node", envCfg.NodeName, "Node name (prod, dev, test, or custom)")
	port := fs.String("port", envCfg.Port, "HTTP server port")
	url := fs.String("url", "", "Local server URL to expose")
	webhookPath := fs.String("webhook-path", "/webhook", "Webhook path to register")
	cloudflaredPath := fs.String("cloudflared", "cloudflared", "cloudflared binary path")

	if err := fs.Parse(args); err != nil {
		return tunnelConfig{}, err
	}

	localURL := *url
	if localURL == "" {
		derived := app.Config{NodeName: *node, Port: *port}
		derived.DeriveNodeConfig()
		localURL = "http://localhost:" + derived.Port
	}

	return tunnelConfig{
		Token:           *token,
		LocalURL:        localURL,
		WebhookPath:     *webhookPath,
		CloudflaredPath: *cloudflaredPath,
	}, nil
}

func runTunnelCommand(ctx context.Context, cfg tunnelConfig, stdout io.Writer) error {
	return runTunnelCommandWithRunner(ctx, cfg, stdout, tunnel.NewRunner(cfg.CloudflaredPath))
}

func runTunnelCommandWithRunner(ctx context.Context, cfg tunnelConfig, stdout io.Writer, runner tunnel.Runner) error {
	client := app.NewTelegramClientForWebhook(cfg.Token, cfg.baseURL)
	onURL := func(url string) error {
		webhookURL := tunnel.JoinWebhookURL(url, cfg.WebhookPath)
		if err := client.SetWebhook(webhookURL); err != nil {
			return fmt.Errorf("failed to set webhook: %w", err)
		}
		fmt.Fprintf(stdout, "Webhook registered: %s\n", webhookURL)
		return nil
	}

	return runner.Run(ctx, tunnel.Config{LocalURL: cfg.LocalURL}, stdout, onURL)
}
