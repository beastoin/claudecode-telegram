// Package main provides the CLI entry point for cctg.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

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
	case "status":
		return runStatus(args[2:], stdout)
	case "fix":
		return runFix(args[2:], stdout)
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
	case "serve", "hook", "webhook", "tunnel", "status", "fix", "version":
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
  status        Show node health status with diagnostics
  fix           Auto-fix recoverable issues (tunnel, webhook)
  hook          Send response to bridge (used by Claude's Stop hook)
  hook install  Install Claude Code stop hook to settings.json
  webhook       Register webhook URL with Telegram
  tunnel        Start cloudflared tunnel + auto-register webhook
  version       Print version information
  help          Show this help message

Serve options:
  --token    Telegram bot token (or TELEGRAM_BOT_TOKEN env)
  --admin    Admin chat ID (or ADMIN_CHAT_ID env)
  --node     Node name: prod|dev|test|custom (isolation identity)
             Derives: prefix, sessions dir
  --port     HTTP server port (default: 8080)
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
  --node           Node name: prod|dev|test|custom (default: prod)
  --port           HTTP server port (default: 8080)
  --webhook-path   Webhook path to register (default: /webhook)
  --cloudflared    Path to cloudflared binary (default: cloudflared)

Status options:
  --node           Node name to check (default: prod)
  --all            Show status for all nodes
  --token          Telegram bot token (for webhook verification)

Fix options:
  --node           Node name to fix (default: prod)
  --token          Telegram bot token (or TELEGRAM_BOT_TOKEN env)

Exit codes (status/fix):
  0  Healthy - all checks pass
  1  Degraded - warnings present but functional
  2  Critical - errors that prevent operation

Examples:
  cctg serve --token "123:ABC" --admin "999"
  cctg status --node test
  cctg status --all
  cctg fix --node test
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

	// Get secrets from environment
	envCfg := app.ConfigFromEnv()

	token := fs.String("token", envCfg.Token, "Telegram bot token")
	admin := fs.String("admin", envCfg.AdminChatID, "Admin chat ID")
	node := fs.String("node", "", "Node name (prod, dev, test, or custom)")
	port := fs.String("port", "", "HTTP server port (default: 8080)")
	// Prefix, SessionsDir, BridgeURL always derived from node/port - not configurable
	jsonLog := fs.Bool("json", false, "Use JSON structured logging")
	sandbox := fs.Bool("sandbox", false, "Run workers in Docker containers")
	noSandbox := fs.Bool("no-sandbox", false, "Run workers directly (--dangerously-skip-permissions)")
	sandboxImage := fs.String("sandbox-image", "", "Docker image for workers")

	mountSpecs := []string{}
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
		Token:       *token,
		AdminChatID: *admin,
		NodeName:    *node,
		Port:        *port,
		// Prefix, SessionsDir, BridgeURL derived by DeriveNodeConfig()
		JSONLog:        *jsonLog,
		SandboxEnabled: *sandbox,
		SandboxImage:   *sandboxImage,
		SandboxMounts:  strings.Join(mountSpecs, ","),
	}
	if *noSandbox {
		cfg.SandboxEnabled = false
	}

	// Derive defaults based on node name (only fills in empty fields)
	cfg.DeriveNodeConfig()

	return cfg, nil
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

// cctgHookCommand is the command we install - used for duplicate detection
// Note: No --session flag - the hook auto-detects session from tmux
const cctgHookCommand = `cctg hook`

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

	// Check existing Stop hooks
	var existingStops []interface{}
	if existing, ok := hooks["Stop"].([]interface{}); ok {
		existingStops = existing
	}

	// Check if cctg hook already exists (avoid duplicates)
	if hookExists(existingStops, cctgHookCommand) {
		fmt.Fprintf(stdout, "Hook already installed in %s\n", settingsPath)
		return nil
	}

	// Create the new hook entry
	newHookEntry := map[string]interface{}{
		"type":    "command",
		"command": cctgHookCommand,
	}

	// Append to existing hooks array inside the first Stop object (correct format)
	// Format: { "hooks": { "Stop": [ { "hooks": [ {hook1}, {hook2} ] } ] } }
	if len(existingStops) > 0 {
		// Add to existing first object's hooks array
		if firstStop, ok := existingStops[0].(map[string]interface{}); ok {
			if innerHooks, ok := firstStop["hooks"].([]interface{}); ok {
				firstStop["hooks"] = append(innerHooks, newHookEntry)
			} else {
				firstStop["hooks"] = []interface{}{newHookEntry}
			}
		}
	} else {
		// No existing Stop hooks - create new structure
		existingStops = []interface{}{
			map[string]interface{}{
				"hooks": []interface{}{newHookEntry},
			},
		}
		hooks["Stop"] = existingStops
	}

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

	// Count existing hooks for message
	existingCount := 0
	if len(existingStops) > 0 {
		if firstStop, ok := existingStops[0].(map[string]interface{}); ok {
			if innerHooks, ok := firstStop["hooks"].([]interface{}); ok {
				existingCount = len(innerHooks) - 1 // -1 because we just added one
			}
		}
	}
	if existingCount > 0 {
		fmt.Fprintf(stdout, "Hook appended to %s (%d existing hooks preserved)\n", settingsPath, existingCount)
	} else {
		fmt.Fprintf(stdout, "Hook installed to %s\n", settingsPath)
	}
	return nil
}

// hookExists checks if a hook with the given command already exists in the Stop hooks array
func hookExists(stops []interface{}, command string) bool {
	for _, stop := range stops {
		stopMap, ok := stop.(map[string]interface{})
		if !ok {
			continue
		}
		innerHooks, ok := stopMap["hooks"].([]interface{})
		if !ok {
			continue
		}
		for _, h := range innerHooks {
			hookMap, ok := h.(map[string]interface{})
			if !ok {
				continue
			}
			if cmd, ok := hookMap["command"].(string); ok && cmd == command {
				return true
			}
		}
	}
	return false
}

// runHookWithReader is used for testing
func runHookWithReader(cfg app.HookConfig, r io.Reader) error {
	return app.RunHook(cfg, r)
}

func parseHookFlags(args []string) (app.HookConfig, error) {
	fs := flag.NewFlagSet("hook", flag.ContinueOnError)

	// Get defaults from environment (including tmux session env)
	envCfg := app.HookConfigFromEnv()

	url := fs.String("url", "", "Bridge URL")
	session := fs.String("session", "", "Session name")

	if err := fs.Parse(args); err != nil {
		return app.HookConfig{}, err
	}

	// Use flag values if non-empty, otherwise fall back to env defaults
	bridgeURL := *url
	if bridgeURL == "" {
		bridgeURL = envCfg.BridgeURL
	}

	sessionName := *session
	if sessionName == "" {
		sessionName = envCfg.Session
	}

	return app.HookConfig{
		BridgeURL: bridgeURL,
		Session:   sessionName,
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

	// Get secrets from environment
	envCfg := app.ConfigFromEnv()

	token := fs.String("token", envCfg.Token, "Telegram bot token")
	node := fs.String("node", "", "Node name (prod, dev, test, or custom)")
	port := fs.String("port", "", "HTTP server port (default: 8080)")
	url := fs.String("url", "", "Local server URL to expose")
	webhookPath := fs.String("webhook-path", "/webhook", "Webhook path to register")
	cloudflaredPath := fs.String("cloudflared", "cloudflared", "cloudflared binary path")

	if err := fs.Parse(args); err != nil {
		return tunnelConfig{}, err
	}

	// Derive port from node if not specified
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

// statusConfig holds the configuration for the status command.
type statusConfig struct {
	NodeName string // Node name (prod, dev, test, etc.)
	All      bool   // Show status for all nodes
	Token    string // Telegram bot token (for webhook check)
}

// healthIssue represents a detected problem.
type healthIssue struct {
	Level   string // "ERROR" or "WARN"
	Message string
	Fix     string
}

// nodeHealth contains the full health check results.
type nodeHealth struct {
	NodeName       string
	NodeDir        string
	Port           string
	ServerPID      string
	ServerRunning  bool
	TunnelURL      string
	TunnelRunning  bool
	TunnelReachable bool
	WebhookURL     string
	WebhookMatches bool
	Sessions       []tmuxSessionInfo
	HookInstalled  bool
	Issues         []healthIssue
}

// ExitCode returns the appropriate exit code based on issues.
// 0=healthy, 1=degraded, 2=critical
func (h *nodeHealth) ExitCode() int {
	hasError := false
	hasWarn := false
	for _, issue := range h.Issues {
		if issue.Level == "ERROR" {
			hasError = true
		} else if issue.Level == "WARN" {
			hasWarn = true
		}
	}
	if hasError {
		return 2 // critical
	}
	if hasWarn {
		return 1 // degraded
	}
	return 0 // healthy
}

func runStatus(args []string, stdout io.Writer) error {
	cfg, err := parseStatusFlags(args)
	if err != nil {
		return err
	}

	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	nodesDir := homeDir + "/.claude/telegram/nodes"

	if cfg.All {
		exitCode := showAllNodesStatus(nodesDir, cfg.Token, homeDir, stdout)
		if exitCode != 0 {
			os.Exit(exitCode)
		}
		return nil
	}

	health := checkNodeHealth(nodesDir, cfg.NodeName, cfg.Token, homeDir)
	printNodeHealth(health, stdout)
	exitCode := health.ExitCode()
	if exitCode != 0 {
		os.Exit(exitCode)
	}
	return nil
}

func parseStatusFlags(args []string) (statusConfig, error) {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)

	node := fs.String("node", "", "Node name (prod, dev, test, or custom)")
	all := fs.Bool("all", false, "Show status for all nodes")
	token := fs.String("token", os.Getenv("TELEGRAM_BOT_TOKEN"), "Telegram bot token (for webhook check)")

	if err := fs.Parse(args); err != nil {
		return statusConfig{}, err
	}

	nodeName := *node
	if nodeName == "" && !*all {
		// Default to "prod" if no node specified
		nodeName = "prod"
	}

	return statusConfig{
		NodeName: nodeName,
		All:      *all,
		Token:    *token,
	}, nil
}

func showAllNodesStatus(nodesDir, token, homeDir string, stdout io.Writer) int {
	entries, err := os.ReadDir(nodesDir)
	if err != nil {
		if os.IsNotExist(err) {
			fmt.Fprintln(stdout, "No nodes configured")
			fmt.Fprintln(stdout, "Run: cctg serve --node <name>")
			return 0
		}
		fmt.Fprintf(stdout, "Error reading nodes directory: %v\n", err)
		return 2
	}

	if len(entries) == 0 {
		fmt.Fprintln(stdout, "No nodes configured")
		return 0
	}

	fmt.Fprintln(stdout, "All Nodes")
	fmt.Fprintln(stdout, "")

	worstExit := 0
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		health := checkNodeHealth(nodesDir, entry.Name(), token, homeDir)
		printNodeHealth(health, stdout)
		fmt.Fprintln(stdout, "")
		if health.ExitCode() > worstExit {
			worstExit = health.ExitCode()
		}
	}

	return worstExit
}

// checkNodeHealth performs comprehensive health checks on a node.
func checkNodeHealth(nodesDir, nodeName, token, homeDir string) *nodeHealth {
	h := &nodeHealth{
		NodeName: nodeName,
		NodeDir:  nodesDir + "/" + nodeName,
	}
	prefix := "claude-" + nodeName + "-"

	// Check if node directory exists
	if _, err := os.Stat(h.NodeDir); os.IsNotExist(err) {
		h.Issues = append(h.Issues, healthIssue{
			Level:   "ERROR",
			Message: "Node not configured",
			Fix:     "cctg serve --node " + nodeName,
		})
		return h
	}

	// Check port and server
	portData, _ := os.ReadFile(h.NodeDir + "/port")
	h.Port = strings.TrimSpace(string(portData))

	if h.Port != "" {
		h.ServerRunning, h.ServerPID = isPortInUse(h.Port)
	}

	if !h.ServerRunning {
		h.Issues = append(h.Issues, healthIssue{
			Level:   "ERROR",
			Message: "Server not running",
			Fix:     fmt.Sprintf("cctg serve --node %s --port %s", nodeName, h.Port),
		})
	}

	// Check tunnel
	tunnelData, _ := os.ReadFile(h.NodeDir + "/tunnel_url")
	h.TunnelURL = strings.TrimSpace(string(tunnelData))

	if h.TunnelURL == "" {
		h.TunnelRunning = false
		if h.ServerRunning {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "ERROR",
				Message: "Tunnel not configured",
				Fix:     fmt.Sprintf("cctg tunnel --node %s --port %s", nodeName, h.Port),
			})
		}
	} else {
		// Check if tunnel process is running (cloudflared for this port)
		h.TunnelRunning = isTunnelRunning(h.Port)
		if !h.TunnelRunning {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "ERROR",
				Message: "Tunnel not running",
				Fix:     fmt.Sprintf("cctg tunnel --node %s --port %s", nodeName, h.Port),
			})
		} else {
			// Check if tunnel URL is reachable
			h.TunnelReachable = isTunnelReachable(h.TunnelURL)
			if !h.TunnelReachable {
				h.Issues = append(h.Issues, healthIssue{
					Level:   "ERROR",
					Message: "Tunnel URL unreachable",
					Fix:     fmt.Sprintf("cctg fix --node %s", nodeName),
				})
			}
		}
	}

	// Check webhook (if token provided)
	if token != "" && h.TunnelURL != "" {
		h.WebhookURL = getWebhookURL(token)
		expectedWebhook := h.TunnelURL + "/webhook"
		h.WebhookMatches = h.WebhookURL == expectedWebhook

		if h.WebhookURL == "" {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "ERROR",
				Message: "Webhook not set",
				Fix:     fmt.Sprintf("cctg fix --node %s", nodeName),
			})
		} else if !h.WebhookMatches {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "ERROR",
				Message: fmt.Sprintf("Webhook pointing to stale URL (%s)", truncateURL(h.WebhookURL)),
				Fix:     fmt.Sprintf("cctg fix --node %s", nodeName),
			})
		}
	}

	// Check sessions
	sessions, err := listTmuxSessions(prefix)
	if err == nil {
		h.Sessions = sessions
	}

	// Check session env vars
	expectedBridgeURL := "http://localhost:" + h.Port
	expectedPrefix := prefix
	expectedSessionsDir := h.NodeDir + "/sessions"

	for _, sess := range h.Sessions {
		workerName := strings.TrimPrefix(sess.Name, prefix)

		// Check BRIDGE_URL
		if sess.BridgeURL == "" {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "ERROR",
				Message: fmt.Sprintf("Session %s missing BRIDGE_URL", workerName),
				Fix:     fmt.Sprintf("tmux set-environment -t %s BRIDGE_URL %s", sess.Name, expectedBridgeURL),
			})
		} else if sess.BridgeURL != expectedBridgeURL {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "WARN",
				Message: fmt.Sprintf("Session %s BRIDGE_URL mismatch (got %s, expected %s)", workerName, sess.BridgeURL, expectedBridgeURL),
				Fix:     fmt.Sprintf("tmux set-environment -t %s BRIDGE_URL %s", sess.Name, expectedBridgeURL),
			})
		}

		// Check TMUX_PREFIX
		if sess.TmuxPrefix == "" {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "ERROR",
				Message: fmt.Sprintf("Session %s missing TMUX_PREFIX", workerName),
				Fix:     fmt.Sprintf("tmux set-environment -t %s TMUX_PREFIX %s", sess.Name, expectedPrefix),
			})
		} else if sess.TmuxPrefix != expectedPrefix {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "WARN",
				Message: fmt.Sprintf("Session %s TMUX_PREFIX mismatch (got %s, expected %s)", workerName, sess.TmuxPrefix, expectedPrefix),
				Fix:     fmt.Sprintf("tmux set-environment -t %s TMUX_PREFIX %s", sess.Name, expectedPrefix),
			})
		}

		// Check SESSIONS_DIR (warn only, not critical)
		if sess.SessionsDir == "" {
			h.Issues = append(h.Issues, healthIssue{
				Level:   "WARN",
				Message: fmt.Sprintf("Session %s missing SESSIONS_DIR", workerName),
				Fix:     fmt.Sprintf("tmux set-environment -t %s SESSIONS_DIR %s", sess.Name, expectedSessionsDir),
			})
		}
	}

	// Check hook installation
	h.HookInstalled = isHookInstalled(homeDir)

	if !h.HookInstalled {
		h.Issues = append(h.Issues, healthIssue{
			Level:   "WARN",
			Message: "Hook not installed",
			Fix:     "cctg hook install",
		})
	}

	return h
}

// printNodeHealth prints the health status in the specified format.
func printNodeHealth(h *nodeHealth, stdout io.Writer) {
	// Determine overall status
	status := "running"
	if h.ExitCode() == 2 {
		status = "CRITICAL"
	} else if h.ExitCode() == 1 {
		status = "DEGRADED"
	} else if !h.ServerRunning {
		status = "stopped"
	}

	fmt.Fprintf(stdout, "Node: %s [%s]\n", h.NodeName, status)

	// Server line
	if h.Port != "" {
		if h.ServerRunning {
			fmt.Fprintf(stdout, "  server:   :%s (PID %s)\n", h.Port, h.ServerPID)
		} else {
			fmt.Fprintf(stdout, "  server:   :%s NOT RUNNING <- PROBLEM\n", h.Port)
		}
	} else {
		fmt.Fprintln(stdout, "  server:   not configured")
	}

	// Tunnel line
	if h.TunnelURL != "" {
		if h.TunnelRunning && h.TunnelReachable {
			fmt.Fprintf(stdout, "  tunnel:   %s [reachable]\n", h.TunnelURL)
		} else if h.TunnelRunning && !h.TunnelReachable {
			fmt.Fprintf(stdout, "  tunnel:   %s [UNREACHABLE] <- PROBLEM\n", h.TunnelURL)
		} else {
			fmt.Fprintln(stdout, "  tunnel:   NOT RUNNING <- PROBLEM")
		}
	} else {
		if h.ServerRunning {
			fmt.Fprintln(stdout, "  tunnel:   not configured <- PROBLEM")
		} else {
			fmt.Fprintln(stdout, "  tunnel:   not configured")
		}
	}

	// Webhook line
	if h.WebhookURL != "" {
		if h.WebhookMatches {
			fmt.Fprintln(stdout, "  webhook:  OK (matches tunnel)")
		} else {
			fmt.Fprintf(stdout, "  webhook:  %s (stale) <- PROBLEM\n", truncateURL(h.WebhookURL))
		}
	} else if h.TunnelURL != "" {
		fmt.Fprintln(stdout, "  webhook:  not set <- PROBLEM")
	} else {
		fmt.Fprintln(stdout, "  webhook:  -")
	}

	// Sessions line
	if len(h.Sessions) > 0 {
		fmt.Fprintf(stdout, "  sessions: %d active\n", len(h.Sessions))
		prefix := "claude-" + h.NodeName + "-"
		expectedBridgeURL := "http://localhost:" + h.Port
		for _, s := range h.Sessions {
			workerName := strings.TrimPrefix(s.Name, prefix)
			var status []string
			if s.ClaudeRunning {
				status = append(status, "claude running")
			} else {
				status = append(status, "claude not running")
			}
			// Check env vars
			if s.BridgeURL == "" {
				status = append(status, "NO BRIDGE_URL")
			} else if s.BridgeURL != expectedBridgeURL {
				status = append(status, "BRIDGE_URL mismatch")
			}
			if s.TmuxPrefix == "" {
				status = append(status, "NO TMUX_PREFIX")
			}
			fmt.Fprintf(stdout, "    - %s [%s]\n", workerName, strings.Join(status, ", "))
		}
	} else {
		fmt.Fprintln(stdout, "  sessions: none")
	}

	// Hook line
	if h.HookInstalled {
		fmt.Fprintln(stdout, "  hook:     installed")
	} else {
		fmt.Fprintln(stdout, "  hook:     NOT INSTALLED <- PROBLEM")
	}

	// Health summary
	fmt.Fprintln(stdout, "")
	if len(h.Issues) == 0 {
		fmt.Fprintln(stdout, "Health: OK")
	} else {
		fmt.Fprintf(stdout, "Health: %d issue(s) found\n", len(h.Issues))
		for _, issue := range h.Issues {
			fmt.Fprintf(stdout, "  [%s] %s\n", issue.Level, issue.Message)
			fmt.Fprintf(stdout, "          Fix: %s\n", issue.Fix)
		}

		// Show fix suggestion if there are fixable issues
		hasFixable := false
		for _, issue := range h.Issues {
			if strings.Contains(issue.Fix, "cctg fix") {
				hasFixable = true
				break
			}
		}
		if hasFixable {
			fmt.Fprintf(stdout, "\nRun 'cctg fix --node %s' to auto-fix recoverable issues.\n", h.NodeName)
		}
	}
}

// isTunnelRunning checks if cloudflared is running for the given port.
func isTunnelRunning(port string) bool {
	// Check for cloudflared process with this port in arguments
	cmd := exec.Command("pgrep", "-f", "cloudflared.*"+port)
	return cmd.Run() == nil
}

// isTunnelReachable checks if the tunnel URL responds to HTTP requests.
func isTunnelReachable(tunnelURL string) bool {
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(tunnelURL)
	if err != nil {
		return false
	}
	resp.Body.Close()
	// Any response (even 404) means tunnel is reachable
	return true
}

// getWebhookURL retrieves the current webhook URL from Telegram.
func getWebhookURL(token string) string {
	client := &http.Client{Timeout: 10 * time.Second}
	url := fmt.Sprintf("https://api.telegram.org/bot%s/getWebhookInfo", token)
	resp, err := client.Get(url)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()

	var result struct {
		OK     bool `json:"ok"`
		Result struct {
			URL string `json:"url"`
		} `json:"result"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return ""
	}
	if !result.OK {
		return ""
	}
	return result.Result.URL
}

// isHookInstalled checks if the cctg hook is installed in Claude settings.
func isHookInstalled(homeDir string) bool {
	settingsPath := homeDir + "/.claude/settings.json"
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		return false
	}

	var settings map[string]interface{}
	if err := json.Unmarshal(data, &settings); err != nil {
		return false
	}

	hooks, ok := settings["hooks"].(map[string]interface{})
	if !ok {
		return false
	}

	stops, ok := hooks["Stop"].([]interface{})
	if !ok {
		return false
	}

	// Check if any stop hook contains "cctg hook"
	for _, stop := range stops {
		stopMap, ok := stop.(map[string]interface{})
		if !ok {
			continue
		}
		innerHooks, ok := stopMap["hooks"].([]interface{})
		if !ok {
			continue
		}
		for _, h := range innerHooks {
			hookMap, ok := h.(map[string]interface{})
			if !ok {
				continue
			}
			if cmd, ok := hookMap["command"].(string); ok {
				if strings.Contains(cmd, "cctg hook") {
					return true
				}
			}
		}
	}
	return false
}

// truncateURL truncates a URL for display.
func truncateURL(url string) string {
	if len(url) > 50 {
		return url[:47] + "..."
	}
	return url
}

// fixConfig holds configuration for the fix command.
type fixConfig struct {
	NodeName string
	Token    string
}

func runFix(args []string, stdout io.Writer) error {
	cfg, err := parseFixFlags(args)
	if err != nil {
		return err
	}

	if cfg.Token == "" {
		return fmt.Errorf("token is required (set TELEGRAM_BOT_TOKEN or use --token)")
	}

	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	nodesDir := homeDir + "/.claude/telegram/nodes"
	health := checkNodeHealth(nodesDir, cfg.NodeName, cfg.Token, homeDir)

	// Print current status
	fmt.Fprintf(stdout, "Checking node: %s\n\n", cfg.NodeName)

	fixed := 0
	notFixed := 0

	// Fix webhook issues
	if !health.WebhookMatches && health.TunnelURL != "" && health.TunnelRunning && health.TunnelReachable {
		fmt.Fprintf(stdout, "Fixing webhook...\n")
		expectedURL := health.TunnelURL + "/webhook"
		if err := setWebhook(cfg.Token, expectedURL); err != nil {
			fmt.Fprintf(stdout, "  [FAILED] Could not set webhook: %v\n", err)
			notFixed++
		} else {
			fmt.Fprintf(stdout, "  [FIXED] Webhook set to %s\n", expectedURL)
			fixed++
		}
	}

	// Report unfixable issues
	for _, issue := range health.Issues {
		// Skip webhook issues (we tried to fix them above)
		if strings.Contains(issue.Message, "Webhook") {
			continue
		}

		// These issues cannot be auto-fixed
		switch {
		case strings.Contains(issue.Message, "Server not running"):
			fmt.Fprintf(stdout, "  [CANNOT FIX] %s\n", issue.Message)
			fmt.Fprintf(stdout, "               Manual fix: %s\n", issue.Fix)
			notFixed++
		case strings.Contains(issue.Message, "Tunnel not"):
			fmt.Fprintf(stdout, "  [CANNOT FIX] %s\n", issue.Message)
			fmt.Fprintf(stdout, "               Manual fix: %s\n", issue.Fix)
			notFixed++
		case strings.Contains(issue.Message, "Hook not installed"):
			fmt.Fprintf(stdout, "  [CANNOT FIX] %s\n", issue.Message)
			fmt.Fprintf(stdout, "               Manual fix: %s\n", issue.Fix)
			notFixed++
		case strings.Contains(issue.Message, "Node not configured"):
			fmt.Fprintf(stdout, "  [CANNOT FIX] %s\n", issue.Message)
			fmt.Fprintf(stdout, "               Manual fix: %s\n", issue.Fix)
			notFixed++
		}
	}

	// Summary
	fmt.Fprintln(stdout, "")
	if fixed > 0 || notFixed > 0 {
		fmt.Fprintf(stdout, "Fixed: %d, Cannot fix: %d\n", fixed, notFixed)
	}

	if len(health.Issues) == 0 {
		fmt.Fprintln(stdout, "No issues to fix - node is healthy.")
		return nil
	}

	// Re-check health after fixes
	if fixed > 0 {
		fmt.Fprintln(stdout, "\nRe-checking health...")
		newHealth := checkNodeHealth(nodesDir, cfg.NodeName, cfg.Token, homeDir)
		if newHealth.ExitCode() == 0 {
			fmt.Fprintln(stdout, "Health: OK")
		} else {
			fmt.Fprintf(stdout, "Health: %d issue(s) remaining\n", len(newHealth.Issues))
			os.Exit(newHealth.ExitCode())
		}
	}

	if notFixed > 0 {
		os.Exit(1)
	}

	return nil
}

func parseFixFlags(args []string) (fixConfig, error) {
	fs := flag.NewFlagSet("fix", flag.ContinueOnError)

	node := fs.String("node", "prod", "Node name (prod, dev, test, or custom)")
	token := fs.String("token", os.Getenv("TELEGRAM_BOT_TOKEN"), "Telegram bot token")

	if err := fs.Parse(args); err != nil {
		return fixConfig{}, err
	}

	return fixConfig{
		NodeName: *node,
		Token:    *token,
	}, nil
}

// setWebhook registers a webhook URL with Telegram.
func setWebhook(token, webhookURL string) error {
	client := &http.Client{Timeout: 10 * time.Second}
	apiURL := fmt.Sprintf("https://api.telegram.org/bot%s/setWebhook", token)

	payload := map[string]string{"url": webhookURL}
	data, _ := json.Marshal(payload)

	req, err := http.NewRequest(http.MethodPost, apiURL, strings.NewReader(string(data)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	var result struct {
		OK          bool   `json:"ok"`
		Description string `json:"description"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return err
	}
	if !result.OK {
		return fmt.Errorf("telegram API error: %s", result.Description)
	}
	return nil
}

// tmuxSessionInfo holds basic info about a tmux session.
type tmuxSessionInfo struct {
	Name          string
	ClaudeRunning bool
	// Env vars from tmux session
	BridgeURL   string
	TmuxPrefix  string
	Port        string
	SessionsDir string
}

// listTmuxSessions lists tmux sessions matching the given prefix.
func listTmuxSessions(prefix string) ([]tmuxSessionInfo, error) {
	cmd := exec.Command("tmux", "list-sessions", "-F", "#{session_name}")
	out, err := cmd.Output()
	if err != nil {
		// "no server running" or "no sessions" is not an error
		if exitErr, ok := err.(*exec.ExitError); ok {
			stderr := string(exitErr.Stderr)
			if strings.Contains(stderr, "no server running") ||
				strings.Contains(stderr, "no sessions") {
				return nil, nil
			}
		}
		return nil, err
	}

	var sessions []tmuxSessionInfo
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || !strings.HasPrefix(line, prefix) {
			continue
		}

		// Check if claude is running in this session
		claudeRunning := isClaudeRunning(line)

		// Get env vars from tmux session
		envVars := getTmuxSessionEnv(line)

		sessions = append(sessions, tmuxSessionInfo{
			Name:          line,
			ClaudeRunning: claudeRunning,
			BridgeURL:     envVars["BRIDGE_URL"],
			TmuxPrefix:    envVars["TMUX_PREFIX"],
			Port:          envVars["PORT"],
			SessionsDir:   envVars["SESSIONS_DIR"],
		})
	}

	return sessions, nil
}

// getTmuxSessionEnv gets environment variables from a tmux session.
func getTmuxSessionEnv(sessionName string) map[string]string {
	result := make(map[string]string)

	cmd := exec.Command("tmux", "show-environment", "-t", sessionName)
	out, err := cmd.Output()
	if err != nil {
		return result
	}

	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "-") {
			continue // Skip empty lines and unset vars (prefixed with -)
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) == 2 {
			result[parts[0]] = parts[1]
		}
	}

	return result
}

// isClaudeRunning checks if claude process is running in a tmux session.
func isClaudeRunning(sessionName string) bool {
	// First try pane_current_command (fast path)
	cmd := exec.Command("tmux", "display-message", "-t", sessionName, "-p", "#{pane_current_command}")
	out, err := cmd.Output()
	if err == nil {
		paneCmd := strings.TrimSpace(string(out))
		if strings.Contains(strings.ToLower(paneCmd), "claude") {
			return true
		}
	}

	// Fallback: check if claude is a child process of the pane
	cmd = exec.Command("tmux", "display-message", "-t", sessionName, "-p", "#{pane_pid}")
	out, err = cmd.Output()
	if err != nil {
		return false
	}

	panePID := strings.TrimSpace(string(out))
	if panePID == "" {
		return false
	}

	// Check for claude as child process using pgrep
	pgrepCmd := exec.Command("pgrep", "-P", panePID, "claude")
	return pgrepCmd.Run() == nil
}

// isPortInUse checks if something is listening on the given port.
// Returns (inUse, pid) where pid is the process ID if found.
func isPortInUse(port string) (bool, string) {
	// Try lsof first (works on macOS and Linux)
	cmd := exec.Command("lsof", "-ti", ":"+port)
	out, err := cmd.Output()
	if err == nil {
		pid := strings.TrimSpace(string(out))
		if pid != "" {
			// May have multiple PIDs, take first one
			pids := strings.Split(pid, "\n")
			return true, pids[0]
		}
	}

	// Fallback: try to connect to the port
	conn, err := net.DialTimeout("tcp", "localhost:"+port, time.Second)
	if err == nil {
		conn.Close()
		return true, ""
	}

	return false, ""
}
