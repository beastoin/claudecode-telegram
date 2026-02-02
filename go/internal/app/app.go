// Package app provides application wiring and lifecycle management for cctg.
package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/beastoin/claudecode-telegram/internal/hook"
	"github.com/beastoin/claudecode-telegram/internal/markdown"
	"github.com/beastoin/claudecode-telegram/internal/sandbox"
	"github.com/beastoin/claudecode-telegram/internal/server"
	"github.com/beastoin/claudecode-telegram/internal/telegram"
	"github.com/beastoin/claudecode-telegram/internal/tmux"
)

// Config holds the configuration for the serve command.
type Config struct {
	Token       string // Telegram bot token
	AdminChatID string // Admin chat ID for authorization
	Port        string // HTTP server port
	Prefix      string // tmux session prefix
	NodeName    string // Node name (prod, dev, test, etc.)
	SessionsDir string // Where session files go
	JSONLog     bool   // Use JSON structured logging
	// Sandbox mode (Docker isolation)
	SandboxEnabled bool
	SandboxImage   string
	SandboxMounts  string
	BridgeURL      string
}

// DeriveNodeConfig sets defaults based on node name.
//
// Node and Port are independent concepts:
//   - Node: isolation identity (determines prefix, sessions dir)
//   - Port: network binding (default 8080, specify via --port)
//
// Fields ALWAYS derived from node (cannot be overridden):
//   - Prefix: always "claude-{node}-"
//   - SessionsDir: always "~/.claude/telegram/nodes/{node}/sessions"
//
// Fields ALWAYS derived from port (cannot be overridden):
//   - BridgeURL: always "http://localhost:{port}"
func (c *Config) DeriveNodeConfig() {
	if c.NodeName == "" {
		c.NodeName = "prod"
	}
	// Port: single default, independent of node
	// Specify via --port flag
	if c.Port == "" {
		c.Port = "8080"
	}
	// Prefix: ALWAYS derived from node (no override) to ensure session isolation
	c.Prefix = "claude-" + c.NodeName + "-"
	// SessionsDir: ALWAYS derived from node (no override) to prevent cross-node mixing
	home, _ := os.UserHomeDir()
	c.SessionsDir = filepath.Join(home, ".claude", "telegram", "nodes", c.NodeName, "sessions")
	// BridgeURL: ALWAYS derived from port (no override) - workers need correct bridge
	c.BridgeURL = "http://localhost:" + c.Port
	// SandboxImage: has default, rarely needs override
	if c.SandboxImage == "" {
		c.SandboxImage = "claudecode-telegram:latest"
	}
}

// ConfigFromEnv creates a Config from environment variables.
// Note: This does NOT call DeriveNodeConfig() - caller should do that after
// merging with command-line flags.
//
// Only secrets are read from the environment:
//   - TELEGRAM_BOT_TOKEN
//   - ADMIN_CHAT_ID
func ConfigFromEnv() Config {
	return Config{
		Token:       os.Getenv("TELEGRAM_BOT_TOKEN"),
		AdminChatID: os.Getenv("ADMIN_CHAT_ID"),
	}
}

// Validate checks that required fields are set.
func (c Config) Validate() error {
	if c.Token == "" {
		return fmt.Errorf("token is required (set TELEGRAM_BOT_TOKEN or use --token)")
	}
	if c.AdminChatID == "" {
		return fmt.Errorf("admin chat ID is required (set ADMIN_CHAT_ID or use --admin)")
	}
	return nil
}

// HookConfig holds the configuration for the hook command.
type HookConfig struct {
	BridgeURL string // URL to send responses to
	Session   string // Session name
}

// HookConfigFromEnv creates a HookConfig from environment variables.
// Falls back to reading from tmux session environment if shell env is empty.
func HookConfigFromEnv() HookConfig {
	bridgeURL := os.Getenv("BRIDGE_URL")
	session := os.Getenv("SESSION_NAME")

	// If not set in shell env, try tmux session env
	if bridgeURL == "" || session == "" {
		sessionName := hook.GetTmuxSessionName()
		if sessionName != "" {
			if bridgeURL == "" {
				bridgeURL = hook.GetTmuxEnv(sessionName, "BRIDGE_URL")
				// Fall back to PORT if BRIDGE_URL not set
				if bridgeURL == "" {
					port := hook.GetTmuxEnv(sessionName, "PORT")
					if port != "" {
						bridgeURL = "http://localhost:" + port
					}
				}
			}
			if session == "" {
				// Extract session name from tmux session name using prefix
				prefix := hook.GetTmuxEnv(sessionName, "TMUX_PREFIX")
				if prefix != "" && strings.HasPrefix(sessionName, prefix) {
					session = sessionName[len(prefix):]
				}
			}
		}
	}

	return HookConfig{
		BridgeURL: bridgeURL,
		Session:   session,
	}
}

// Validate checks that required fields are set.
func (c HookConfig) Validate() error {
	if c.BridgeURL == "" {
		return fmt.Errorf("bridge URL is required (set BRIDGE_URL or use --url)")
	}
	if c.Session == "" {
		return fmt.Errorf("session is required (set SESSION_NAME or use --session)")
	}
	return nil
}

// TelegramClientInterface is the interface the handler expects.
type TelegramClientInterface interface {
	SendMessage(chatID, text string) error
	SendMessageHTML(chatID, text string) error
	SendChatAction(chatID, action string) error
	SetMessageReaction(chatID string, messageID int64, emoji string) error
	SendPhoto(chatID, filePath, caption string) error
	SendDocument(chatID, filePath, caption string) error
	AdminChatID() string
	DownloadFile(fileID string) ([]byte, error)
}

// TmuxManagerInterface is the interface the handler expects.
type TmuxManagerInterface interface {
	ListSessions() ([]server.SessionInfo, error)
	CreateSession(name, workdir string) error
	SendMessage(sessionName, text string) error
	KillSession(sessionName string) error
	SessionExists(sessionName string) bool
}

// telegramClientAdapter adapts telegram.Client to TelegramClientInterface.
type telegramClientAdapter struct {
	client *telegram.Client
}

func (a *telegramClientAdapter) SendMessage(chatID, text string) error {
	return a.client.SendMessage(chatID, text)
}

func (a *telegramClientAdapter) SendMessageHTML(chatID, text string) error {
	return a.client.SendMessageHTML(chatID, text)
}

func (a *telegramClientAdapter) SendChatAction(chatID, action string) error {
	return a.client.SendChatAction(chatID, action)
}

func (a *telegramClientAdapter) AdminChatID() string {
	return a.client.AdminChatID()
}

func (a *telegramClientAdapter) DownloadFile(fileID string) ([]byte, error) {
	return a.client.DownloadFile(fileID)
}

func (a *telegramClientAdapter) SetMessageReaction(chatID string, messageID int64, emoji string) error {
	return a.client.SetMessageReaction(chatID, messageID, emoji)
}

func (a *telegramClientAdapter) SendPhoto(chatID, filePath, caption string) error {
	return a.client.SendPhoto(chatID, filePath, caption)
}

func (a *telegramClientAdapter) SendDocument(chatID, filePath, caption string) error {
	return a.client.SendDocument(chatID, filePath, caption)
}

func (a *telegramClientAdapter) SetMyCommands(commands []server.BotCommand) error {
	// Convert server.BotCommand to telegram.BotCommand
	telegramCommands := make([]telegram.BotCommand, len(commands))
	for i, c := range commands {
		telegramCommands[i] = telegram.BotCommand{
			Command:     c.Command,
			Description: c.Description,
		}
	}
	return a.client.SetMyCommands(telegramCommands)
}

// tmuxManagerAdapter adapts tmux.Manager to TmuxManagerInterface.
type tmuxManagerAdapter struct {
	manager *tmux.Manager
}

func (a *tmuxManagerAdapter) ListSessions() ([]server.SessionInfo, error) {
	sessions, err := a.manager.ListSessions()
	if err != nil {
		return nil, err
	}
	result := make([]server.SessionInfo, len(sessions))
	for i, s := range sessions {
		result[i] = server.SessionInfo{Name: s.Name}
	}
	return result, nil
}

func (a *tmuxManagerAdapter) CreateSession(name, workdir string) error {
	return a.manager.CreateSession(name, workdir)
}

func (a *tmuxManagerAdapter) SendMessage(sessionName, text string) error {
	return a.manager.SendMessage(sessionName, text)
}

func (a *tmuxManagerAdapter) KillSession(sessionName string) error {
	return a.manager.KillSession(sessionName)
}

func (a *tmuxManagerAdapter) SessionExists(sessionName string) bool {
	return a.manager.SessionExists(sessionName)
}

func (a *tmuxManagerAdapter) PromptEmpty(sessionName string, timeout time.Duration) bool {
	return a.manager.PromptEmpty(sessionName, timeout)
}

func (a *tmuxManagerAdapter) SendKeys(sessionName string, keys ...string) error {
	return a.manager.SendKeys(sessionName, keys...)
}

func (a *tmuxManagerAdapter) GetPaneCommand(sessionName string) (string, error) {
	return a.manager.GetPaneCommand(sessionName)
}

func (a *tmuxManagerAdapter) IsClaudeRunning(sessionName string) bool {
	return a.manager.IsClaudeRunning(sessionName)
}

func (a *tmuxManagerAdapter) RestartClaude(sessionName string) error {
	return a.manager.RestartClaude(sessionName)
}

// TmuxListSessions is an interface for listing sessions (used by App for startup message).
type TmuxListSessions interface {
	ListSessions() ([]server.SessionInfo, error)
}

// App represents the application with all its components.
type App struct {
	config   Config
	handler  *server.Handler
	server   *http.Server
	telegram TelegramClientInterface
	tmux     TmuxListSessions
}

// New creates a new App with the given configuration.
func New(cfg Config) (*App, error) {
	// Create Telegram client
	tgClient := telegram.NewClient(cfg.Token, cfg.AdminChatID)
	tgAdapter := &telegramClientAdapter{client: tgClient}

	// Create tmux manager
	tmuxManager := tmux.NewManager(cfg.Prefix)
	tmuxManager.SessionsDir = cfg.SessionsDir
	tmuxManager.Port = cfg.Port
	tmuxManager.BridgeURL = cfg.BridgeURL
	tmuxManager.SandboxEnabled = cfg.SandboxEnabled
	tmuxManager.SandboxImage = cfg.SandboxImage
	tmuxManager.SandboxMounts = sandbox.ParseMounts(cfg.SandboxMounts)
	tmuxAdapter := &tmuxManagerAdapter{manager: tmuxManager}

	// Create handler
	handler := server.NewHandler(tgAdapter, tmuxAdapter)

	// Pass configuration to handler (for file handling and /settings display)
	handler.SetConfig(server.HandlerConfig{
		Port:           cfg.Port,
		Prefix:         cfg.Prefix,
		SessionsDir:    cfg.SessionsDir,
		SandboxEnabled: cfg.SandboxEnabled,
		SandboxImage:   cfg.SandboxImage,
		SandboxMounts:  sandbox.ParseMounts(cfg.SandboxMounts),
	})

	// Create HTTP server
	httpServer := &http.Server{
		Addr:         ":" + cfg.Port,
		Handler:      handler,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 30 * time.Second,
	}

	return &App{
		config:   cfg,
		handler:  handler,
		server:   httpServer,
		telegram: tgAdapter,
		tmux:     tmuxAdapter,
	}, nil
}

// SendStartupNotification sends startup message with team context like Python.
func (a *App) SendStartupNotification() error {
	sessions, _ := a.tmux.ListSessions()

	var lines []string
	lines = append(lines, "I'm online and ready.")

	if len(sessions) > 0 {
		var names []string
		for _, s := range sessions {
			names = append(names, s.Name)
		}
		lines = append(lines, fmt.Sprintf("Team: %s", strings.Join(names, ", ")))
		// Note: We don't track focused state in App yet, handler does
	} else {
		lines = append(lines, "No workers yet. Hire your first long-lived worker with /hire <name>.")
	}

	// Short sandbox note
	if a.config.SandboxEnabled {
		if home, err := os.UserHomeDir(); err == nil {
			lines = append(lines, fmt.Sprintf("Sandbox: %s -> /workspace", home))
		}
	}

	return a.telegram.SendMessage(a.config.AdminChatID, strings.Join(lines, "\n"))
}

// Run starts the application and blocks until the context is cancelled.
func (a *App) Run(ctx context.Context) error {
	// Create listener
	ln, err := net.Listen("tcp", a.server.Addr)
	if err != nil {
		return fmt.Errorf("failed to listen: %w", err)
	}

	// Start server in goroutine
	errCh := make(chan error, 1)
	go func() {
		if err := a.server.Serve(ln); err != nil && err != http.ErrServerClosed {
			errCh <- err
		}
		close(errCh)
	}()

	// Wait for context cancellation or server error
	select {
	case <-ctx.Done():
		// Send shutdown notification to all chats
		a.handler.BroadcastShutdown()

		// Graceful shutdown
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return a.server.Shutdown(shutdownCtx)
	case err := <-errCh:
		return err
	}
}

// JSONLogger provides structured JSON logging.
type JSONLogger struct {
	w io.Writer
}

// NewJSONLogger creates a new JSON logger.
func NewJSONLogger(w io.Writer) *JSONLogger {
	return &JSONLogger{w: w}
}

// log writes a JSON log entry.
func (l *JSONLogger) log(level, msg string, kvs ...interface{}) {
	entry := map[string]interface{}{
		"time":  time.Now().UTC().Format(time.RFC3339),
		"level": level,
		"msg":   msg,
	}

	// Add key-value pairs
	for i := 0; i < len(kvs)-1; i += 2 {
		key, ok := kvs[i].(string)
		if ok {
			entry[key] = kvs[i+1]
		}
	}

	data, _ := json.Marshal(entry)
	fmt.Fprintln(l.w, string(data))
}

// Info logs an info level message.
func (l *JSONLogger) Info(msg string, kvs ...interface{}) {
	l.log("info", msg, kvs...)
}

// Error logs an error level message.
func (l *JSONLogger) Error(msg string, kvs ...interface{}) {
	l.log("error", msg, kvs...)
}

// Debug logs a debug level message.
func (l *JSONLogger) Debug(msg string, kvs ...interface{}) {
	l.log("debug", msg, kvs...)
}

// WebhookClient is an interface for webhook operations.
type WebhookClient interface {
	SetWebhook(url string) error
}

// webhookClientAdapter adapts telegram.Client for webhook operations.
type webhookClientAdapter struct {
	client *telegram.Client
}

func (a *webhookClientAdapter) SetWebhook(url string) error {
	return a.client.SetWebhook(url)
}

// NewTelegramClientForWebhook creates a client for webhook registration.
func NewTelegramClientForWebhook(token, baseURL string) WebhookClient {
	client := telegram.NewClient(token, "")
	if baseURL != "" {
		client.SetBaseURL(baseURL)
	}
	return &webhookClientAdapter{client: client}
}

// RunHook executes the hook command, sending text to the bridge.
// It parses the Claude hook JSON input, extracts text from transcript,
// converts markdown to HTML, and sends to the bridge.
func RunHook(cfg HookConfig, r io.Reader) error {
	// Read all input
	data, err := io.ReadAll(r)
	if err != nil {
		return fmt.Errorf("failed to read input: %w", err)
	}

	rawInput := strings.TrimSpace(string(data))
	if rawInput == "" {
		return nil
	}

	var text string
	var usedFallback bool

	// Try to parse as Claude hook JSON input
	hookInput, err := hook.ParseInput(data)
	if err == nil && hookInput.TranscriptPath != "" {
		// Extract text from transcript
		text, err = hook.ExtractFromTranscript(hookInput.TranscriptPath)
		if err != nil {
			// Log but continue to fallback
			text = ""
		}
	}

	// Fallback: use tmux capture if transcript extraction failed
	// Enabled by default, set TMUX_FALLBACK=0 to disable
	if (text == "" || text == "null") && os.Getenv("TMUX_FALLBACK") != "0" {
		sessionName := os.Getenv("TMUX")
		if sessionName == "" {
			// Try to get session name from tmux
			sessionName = cfg.Session
		}
		if sessionName != "" {
			fallbackText, fallbackErr := hook.TmuxFallback(sessionName)
			if fallbackErr == nil && fallbackText != "" {
				text = fallbackText
				usedFallback = true
			}
		}
	}

	// If still no text, treat raw input as plain text (backward compatibility)
	if text == "" && hookInput == nil {
		text = rawInput
	}

	if text == "" || text == "null" {
		return nil
	}

	// Add warning when using tmux fallback
	if usedFallback {
		text += hook.FallbackWarning
	}

	// Convert markdown to HTML
	text = markdown.ToHTML(text)

	// Create payload
	payload := struct {
		Session string `json:"session"`
		Text    string `json:"text"`
	}{
		Session: cfg.Session,
		Text:    text,
	}

	jsonData, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal payload: %w", err)
	}

	bridgeURL := strings.TrimSpace(cfg.BridgeURL)
	if !strings.HasSuffix(bridgeURL, "/response") {
		bridgeURL = strings.TrimRight(bridgeURL, "/") + "/response"
	}

	// Send to bridge
	client := &http.Client{Timeout: 30 * time.Second}
	resp, err := client.Post(bridgeURL, "application/json", bytes.NewReader(jsonData))
	if err != nil {
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("server returned %d: %s", resp.StatusCode, string(body))
	}

	return nil
}
