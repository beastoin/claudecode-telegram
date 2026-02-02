// Package server provides HTTP handlers for the Telegram webhook and response endpoints.
package server

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/beastoin/claudecode-telegram/internal/files"
	"github.com/beastoin/claudecode-telegram/internal/sandbox"
	"github.com/beastoin/claudecode-telegram/internal/telegram"
)

// Update represents a Telegram webhook update.
type Update struct {
	UpdateID int64    `json:"update_id"`
	Message  *Message `json:"message"`
}

// Message represents a Telegram message.
type Message struct {
	MessageID      int64       `json:"message_id"`
	From           *User       `json:"from"`
	Chat           *Chat       `json:"chat"`
	Text           string      `json:"text"`
	Caption        string      `json:"caption"`
	ReplyToMessage *Message    `json:"reply_to_message"`
	Document       *Document   `json:"document"`
	Photo          []PhotoSize `json:"photo"`
}

// Document represents a file sent as a document.
type Document struct {
	FileID   string `json:"file_id"`
	FileName string `json:"file_name"`
}

// PhotoSize represents one size of a photo or file thumbnail.
type PhotoSize struct {
	FileID string `json:"file_id"`
	Width  int    `json:"width"`
	Height int    `json:"height"`
}

// Chat represents a Telegram chat.
type Chat struct {
	ID int64 `json:"id"`
}

// User represents a Telegram user.
type User struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
}

// ResponsePayload represents the payload for the /response endpoint.
type ResponsePayload struct {
	Session string `json:"session"`
	Text    string `json:"text"`
}

// SessionInfo represents basic session information.
type SessionInfo struct {
	Name string
}

// BotCommand represents a Telegram bot command for the command menu.
type BotCommand struct {
	Command     string `json:"command"`
	Description string `json:"description"`
}

// TelegramClient is the interface for sending Telegram messages.
type TelegramClient interface {
	SendMessage(chatID, text string) error
	SendMessageHTML(chatID, text string) error
	SendChatAction(chatID, action string) error
	SetMessageReaction(chatID string, messageID int64, emoji string) error
	SendPhoto(chatID, filePath, caption string) error
	SendDocument(chatID, filePath, caption string) error
	AdminChatID() string
	DownloadFile(fileID string) ([]byte, error)
	SetMyCommands(commands []BotCommand) error
}

// TmuxManager is the interface for managing tmux sessions.
type TmuxManager interface {
	ListSessions() ([]SessionInfo, error)
	CreateSession(name, workdir string) error
	SendMessage(sessionName, text string) error
	SendKeys(sessionName string, keys ...string) error
	KillSession(sessionName string) error
	SessionExists(sessionName string) bool
	PromptEmpty(sessionName string, timeout time.Duration) bool
	GetPaneCommand(sessionName string) (string, error)
	IsClaudeRunning(sessionName string) bool
	RestartClaude(sessionName string) error
}

// HandlerConfig holds configuration for the handler.
type HandlerConfig struct {
	Port           string
	Prefix         string
	SessionsDir    string
	SandboxEnabled bool
	SandboxImage   string
	SandboxMounts  []sandbox.Mount
}

// Handler handles HTTP requests for the webhook server.
type Handler struct {
	telegram TelegramClient
	tmux     TmuxManager

	// Configuration
	sessionsDir string
	config      HandlerConfig

	// State
	mu            sync.RWMutex
	focusedWorker string
}

// NewHandler creates a new webhook handler.
func NewHandler(telegram TelegramClient, tmux TmuxManager) *Handler {
	return &Handler{
		telegram: telegram,
		tmux:     tmux,
	}
}

// SetSessionsDir sets the sessions directory for file handling.
func (h *Handler) SetSessionsDir(dir string) {
	h.sessionsDir = dir
}

// SetConfig sets the handler configuration.
func (h *Handler) SetConfig(cfg HandlerConfig) {
	h.config = cfg
	if cfg.SessionsDir != "" {
		h.sessionsDir = cfg.SessionsDir
	}
}

// ServeHTTP implements http.Handler.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/webhook":
		h.handleWebhook(w, r)
	case "/response":
		h.handleResponse(w, r)
	case "/notify":
		h.handleNotify(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (h *Handler) handleWebhook(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		// Return 200 to Telegram to prevent retries
		log.Printf("Error reading webhook body: %v", err)
		w.WriteHeader(http.StatusOK)
		return
	}

	var update Update
	if err := json.Unmarshal(body, &update); err != nil {
		log.Printf("Error parsing webhook JSON: %v", err)
		w.WriteHeader(http.StatusOK)
		return
	}

	// Handle the update
	if update.Message == nil {
		w.WriteHeader(http.StatusOK)
		return
	}

	h.processMessage(update.Message)
	w.WriteHeader(http.StatusOK)
}

func (h *Handler) processMessage(msg *Message) {
	if msg.Chat == nil {
		return
	}

	chatIDStr := strconv.FormatInt(msg.Chat.ID, 10)
	messageID := msg.MessageID
	adminChatID := h.telegram.AdminChatID()

	// Admin gating - silent rejection (security best practice)
	if chatIDStr != adminChatID {
		return
	}

	// Check for file attachments first
	if h.hasFileAttachment(msg) {
		h.processFileMessage(chatIDStr, messageID, msg)
		return
	}

	text := strings.TrimSpace(msg.Text)
	if text == "" {
		return
	}

	// Routing priority:
	// 1. Known commands (/hire, /end, etc.)
	// 2. Direct worker routing (/<worker> message)
	// 3. Broadcast (@all message)
	// 4. Reply-to routing (reply to [worker] message)
	// 5. Focused worker

	// Check for commands (starts with /)
	if strings.HasPrefix(text, "/") {
		parts := strings.Fields(text)
		if len(parts) > 0 {
			cmd := strings.ToLower(parts[0])
			// Strip @botname suffix
			if atIdx := strings.Index(cmd, "@"); atIdx != -1 {
				cmd = cmd[:atIdx]
			}

			// Check if this is a blocked command
			if isBlockedCommand(cmd) {
				h.telegram.SendMessage(chatIDStr, fmt.Sprintf("%s is interactive and not supported here.", cmd))
				return
			}

			// Check if this is a known command or worker shortcut
			workerName := strings.TrimPrefix(cmd, "/")
			if h.isKnownCommand(cmd) || h.tmux.SessionExists(workerName) {
				h.handleCommand(chatIDStr, messageID, text)
				return
			}

			// Unknown command - pass through to focused worker (could be a Claude command)
		}
	}

	// Check for @all broadcast
	if strings.HasPrefix(text, "@all") {
		content := strings.TrimSpace(strings.TrimPrefix(text, "@all"))
		h.routeToBroadcast(chatIDStr, messageID, content)
		return
	}

	// Check for reply-to routing
	if msg.ReplyToMessage != nil {
		workerName := h.extractWorkerFromReply(msg.ReplyToMessage.Text)
		if workerName != "" {
			if h.tmux.SessionExists(workerName) {
				h.routeToWorker(chatIDStr, messageID, workerName, text)
				return
			}
			// Worker from reply no longer exists
			h.telegram.SendMessage(chatIDStr, fmt.Sprintf("Worker %s does not exist.", workerName))
			return
		}
		// No worker prefix in reply, fall through to focused worker
	}

	// Default: route to focused worker
	h.routeToFocusedWorker(chatIDStr, messageID, text)
}

// isKnownCommand checks if the command is a known built-in command.
func (h *Handler) isKnownCommand(cmd string) bool {
	switch cmd {
	case "/hire", "/new", "/end", "/kill", "/team", "/list", "/focus", "/use",
		"/pause", "/stop", "/progress", "/status", "/relaunch", "/restart",
		"/settings", "/system", "/learn":
		return true
	default:
		return false
	}
}

// isReservedName checks if a name conflicts with a command.
func isReservedName(name string) bool {
	reserved := []string{
		// Bridge commands
		"team", "focus", "progress", "learn", "pause", "relaunch", "settings", "hire", "end",
		// Aliases
		"new", "use", "list", "kill", "status", "stop", "restart", "system",
		// Special
		"all", "start", "help",
	}
	for _, r := range reserved {
		if strings.EqualFold(name, r) {
			return true
		}
	}
	return false
}

// blockedCommands are Claude commands that are interactive and not supported via Telegram.
var blockedCommands = []string{
	"/mcp", "/help", "/config", "/model", "/compact", "/cost",
	"/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
	"/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen", "/ide",
}

// isBlockedCommand checks if a command is blocked (interactive Claude commands).
func isBlockedCommand(cmd string) bool {
	for _, blocked := range blockedCommands {
		if strings.EqualFold(cmd, blocked) {
			return true
		}
	}
	return false
}

// persistenceNote is appended to messages about worker persistence.
const persistenceNote = "They'll stay on your team."

// extractWorkerFromReply extracts a worker name from a [worker] prefix in text.
func (h *Handler) extractWorkerFromReply(text string) string {
	if !strings.HasPrefix(text, "[") {
		return ""
	}
	end := strings.Index(text, "]")
	if end == -1 {
		return ""
	}
	return text[1:end]
}

// routeToDirectWorker sends a message to a specific worker with direct routing.
func (h *Handler) routeToDirectWorker(chatID string, messageID int64, workerName string, messageParts []string) {
	content := strings.Join(messageParts, " ")
	if content == "" {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Usage: /%s <message>", workerName))
		return
	}

	// Save chat_id for this session (needed for /response endpoint)
	if h.sessionsDir != "" {
		if err := files.SaveChatID(h.sessionsDir, workerName, chatID); err != nil {
			log.Printf("Failed to save chat_id for %s: %v", workerName, err)
		}
	}

	// Send typing indicator
	h.telegram.SendChatAction(chatID, "typing")

	if err := h.tmux.SendMessage(workerName, content); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to send message: %v", err))
		return
	}

	// Send reaction only if Claude accepted the message (prompt is empty)
	if h.tmux.PromptEmpty(workerName, 1*time.Second) {
		if err := h.telegram.SetMessageReaction(chatID, messageID, "\U0001F440"); err != nil {
			log.Printf("Failed to set reaction for message %d: %v", messageID, err)
		}
	} else {
		log.Printf("PromptEmpty returned false for session %s, skipping reaction", workerName)
	}
}

// routeToBroadcast sends a message to all active workers.
func (h *Handler) routeToBroadcast(chatID string, messageID int64, content string) {
	if content == "" {
		return
	}

	sessions, err := h.tmux.ListSessions()
	if err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to list workers: %v", err))
		return
	}

	if len(sessions) == 0 {
		h.telegram.SendMessage(chatID, "No team members yet. Add someone with /hire <name>.")
		return
	}

	// Send typing indicator
	h.telegram.SendChatAction(chatID, "typing")

	anyAccepted := false
	for _, session := range sessions {
		// Save chat_id for each session (needed for /response endpoint)
		if h.sessionsDir != "" {
			if err := files.SaveChatID(h.sessionsDir, session.Name, chatID); err != nil {
				log.Printf("Failed to save chat_id for %s: %v", session.Name, err)
			}
		}

		if err := h.tmux.SendMessage(session.Name, content); err != nil {
			h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to send to %s: %v", session.Name, err))
		} else if h.tmux.PromptEmpty(session.Name, 1*time.Second) {
			anyAccepted = true
		}
	}

	// Send reaction only if at least one Claude accepted the message
	if anyAccepted {
		if err := h.telegram.SetMessageReaction(chatID, messageID, "\U0001F440"); err != nil {
			log.Printf("Failed to set reaction for message %d: %v", messageID, err)
		}
	} else {
		log.Printf("No Claude instance accepted the broadcast message, skipping reaction")
	}
}

// routeToWorker sends a message to a specific worker.
func (h *Handler) routeToWorker(chatID string, messageID int64, workerName, text string) {
	// Save chat_id for this session (needed for /response endpoint)
	if h.sessionsDir != "" {
		if err := files.SaveChatID(h.sessionsDir, workerName, chatID); err != nil {
			log.Printf("Failed to save chat_id for %s: %v", workerName, err)
		}
	}

	// Send typing indicator
	h.telegram.SendChatAction(chatID, "typing")

	if err := h.tmux.SendMessage(workerName, text); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to send message: %v", err))
		return
	}

	// Send reaction only if Claude accepted the message (prompt is empty)
	if h.tmux.PromptEmpty(workerName, 1*time.Second) {
		if err := h.telegram.SetMessageReaction(chatID, messageID, "\U0001F440"); err != nil {
			log.Printf("Failed to set reaction for message %d: %v", messageID, err)
		}
	} else {
		log.Printf("PromptEmpty returned false for session %s, skipping reaction", workerName)
	}
}

func (h *Handler) handleCommand(chatID string, messageID int64, text string) {
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return
	}

	cmd := strings.ToLower(parts[0])
	// Strip @botname suffix (Telegram appends this in groups/autocomplete)
	if atIdx := strings.Index(cmd, "@"); atIdx != -1 {
		cmd = cmd[:atIdx]
	}
	args := parts[1:]

	// Check for blocked commands first
	if isBlockedCommand(cmd) {
		h.telegram.SendMessage(chatID, fmt.Sprintf("%s is interactive and not supported here.", cmd))
		return
	}

	switch cmd {
	case "/hire", "/new":
		h.cmdHire(chatID, args)
	case "/end", "/kill":
		h.cmdEnd(chatID, args)
	case "/team", "/list":
		h.cmdTeam(chatID)
	case "/focus", "/use":
		h.cmdFocus(chatID, args)
	case "/pause", "/stop":
		h.cmdPause(chatID)
	case "/progress", "/status":
		h.cmdProgress(chatID)
	case "/relaunch", "/restart":
		h.cmdRelaunch(chatID)
	case "/settings", "/system":
		h.cmdSettings(chatID)
	case "/learn":
		h.cmdLearn(chatID, messageID, args)
	default:
		// Check if command is a worker shortcut: /lee hello -> route to lee AND switch focus
		workerName := strings.TrimPrefix(cmd, "/")
		if h.tmux.SessionExists(workerName) {
			h.handleWorkerShortcut(chatID, messageID, workerName, args)
		}
	}
}

// handleWorkerShortcut handles /workername messages.
// /lee with no message -> switch focus only
// /lee hello -> route message to lee AND switch focus
func (h *Handler) handleWorkerShortcut(chatID string, messageID int64, workerName string, args []string) {
	h.mu.Lock()
	prevFocus := h.focusedWorker
	h.focusedWorker = workerName
	h.mu.Unlock()

	if len(args) == 0 {
		// Just /lee with no message - switch focus only
		h.telegram.SendMessage(chatID, fmt.Sprintf("Now talking to %s.", strings.Title(workerName)))
		return
	}

	// /lee hello -> route message to lee AND switch focus
	// Notify focus change if switching from different worker
	if prevFocus != workerName {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Now talking to %s.", strings.Title(workerName)))
	}

	content := strings.Join(args, " ")
	h.routeToWorker(chatID, messageID, workerName, content)
}

func (h *Handler) cmdHire(chatID string, args []string) {
	if len(args) < 1 {
		h.telegram.SendMessage(chatID, "Usage: /hire <name>")
		return
	}

	name := strings.ToLower(strings.TrimSpace(args[0]))

	// Check if name is reserved
	if isReservedName(name) {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Cannot use \"%s\" - reserved command. Choose another name.", name))
		return
	}

	workdir := ""
	if len(args) > 1 {
		workdir = args[1]
	}

	if err := h.tmux.CreateSession(name, workdir); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Could not hire \"%s\". %v", name, err))
		return
	}

	// Set as focused worker
	h.mu.Lock()
	h.focusedWorker = name
	h.mu.Unlock()

	h.telegram.SendMessage(chatID, fmt.Sprintf("%s is added and assigned. %s", strings.Title(name), persistenceNote))

	// Update bot commands to include the new worker
	h.updateBotCommands()
}

func (h *Handler) cmdEnd(chatID string, args []string) {
	if len(args) < 1 {
		h.telegram.SendMessage(chatID, "Offboarding is permanent. Usage: /end <name>")
		return
	}

	name := strings.ToLower(strings.TrimSpace(args[0]))
	if err := h.tmux.KillSession(name); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Could not offboard \"%s\". %v", name, err))
		return
	}

	// Clear focus if the ended worker was focused
	h.mu.Lock()
	if h.focusedWorker == name {
		h.focusedWorker = ""
	}
	h.mu.Unlock()

	h.telegram.SendMessage(chatID, fmt.Sprintf("%s removed from your team.", strings.Title(name)))

	// Update bot commands to remove the worker
	h.updateBotCommands()
}

func (h *Handler) cmdTeam(chatID string) {
	sessions, err := h.tmux.ListSessions()
	if err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to list workers: %v", err))
		return
	}

	if len(sessions) == 0 {
		h.telegram.SendMessage(chatID, "No team members yet. Add someone with /hire <name>.")
		return
	}

	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	// Format like Python: Your team: / Focused: / Workers:
	var lines []string
	lines = append(lines, "Your team:")
	if focused != "" {
		lines = append(lines, fmt.Sprintf("Focused: %s", focused))
	} else {
		lines = append(lines, "Focused: (none)")
	}
	lines = append(lines, "Workers:")

	for _, s := range sessions {
		var status []string
		if s.Name == focused {
			status = append(status, "focused")
		}
		// Check if worker is running claude
		if h.tmux.IsClaudeRunning(s.Name) {
			status = append(status, "available")
		} else {
			status = append(status, "offline")
		}
		lines = append(lines, fmt.Sprintf("- %s (%s)", s.Name, strings.Join(status, ", ")))
	}

	h.telegram.SendMessage(chatID, strings.Join(lines, "\n"))
}

func (h *Handler) cmdFocus(chatID string, args []string) {
	if len(args) < 1 {
		h.telegram.SendMessage(chatID, "Usage: /focus <name>")
		return
	}

	name := strings.ToLower(strings.TrimSpace(args[0]))
	if !h.tmux.SessionExists(name) {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Could not focus \"%s\". Worker '%s' not found", name, name))
		return
	}

	h.mu.Lock()
	h.focusedWorker = name
	h.mu.Unlock()

	h.telegram.SendMessage(chatID, fmt.Sprintf("Now talking to %s.", strings.Title(name)))
}

func (h *Handler) cmdPause(chatID string) {
	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	if focused == "" {
		h.telegram.SendMessage(chatID, "No one assigned.")
		return
	}

	// Send Escape key to interrupt Claude
	if err := h.tmux.SendKeys(focused, "Escape"); err != nil {
		log.Printf("Failed to send Escape to %s: %v", focused, err)
	}

	h.telegram.SendMessage(chatID, fmt.Sprintf("%s is paused. I'll pick up where we left off.", strings.Title(focused)))
}

func (h *Handler) cmdProgress(chatID string) {
	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	if focused == "" {
		// List available workers
		sessions, _ := h.tmux.ListSessions()
		if len(sessions) > 0 {
			var names []string
			for _, s := range sessions {
				names = append(names, s.Name)
			}
			h.telegram.SendMessage(chatID, fmt.Sprintf("No one assigned. Your team: %s\nWho should I talk to?", strings.Join(names, ", ")))
		} else {
			h.telegram.SendMessage(chatID, "No one assigned. Who should I talk to? Use /team or /focus <name>.")
		}
		return
	}

	// Build status like Python
	var status []string
	status = append(status, fmt.Sprintf("Progress for focused worker: %s", focused))
	status = append(status, "Focused: yes")

	// Check if session exists
	sessionExists := h.tmux.SessionExists(focused)
	status = append(status, fmt.Sprintf("Online: %s", boolYesNo(sessionExists)))

	if sessionExists {
		claudeRunning := h.tmux.IsClaudeRunning(focused)
		// "Working" in Python checks is_pending - we'll approximate with IsClaudeRunning
		status = append(status, fmt.Sprintf("Working: %s", boolYesNo(claudeRunning)))
		status = append(status, fmt.Sprintf("Ready: %s", boolYesNo(claudeRunning)))
		if !claudeRunning {
			status = append(status, "Needs attention: worker app is not running. Use /relaunch.")
		}
	}

	h.telegram.SendMessage(chatID, strings.Join(status, "\n"))
}

func boolYesNo(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}

func (h *Handler) cmdRelaunch(chatID string) {
	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	if focused == "" {
		h.telegram.SendMessage(chatID, "No one assigned.")
		return
	}

	// Restart claude: send Ctrl+C, wait, then start claude with --dangerously-skip-permissions
	if err := h.tmux.RestartClaude(focused); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Could not relaunch \"%s\". %v", focused, err))
		return
	}

	h.telegram.SendMessage(chatID, fmt.Sprintf("Bringing %s back online...", strings.Title(focused)))
}

func (h *Handler) cmdSettings(chatID string) {
	adminChatID := h.telegram.AdminChatID()

	// Get session info for team state
	sessions, _ := h.tmux.ListSessions()
	var workerNames []string
	for _, s := range sessions {
		workerNames = append(workerNames, s.Name)
	}
	teamList := "(none)"
	if len(workerNames) > 0 {
		teamList = strings.Join(workerNames, ", ")
	}

	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()
	if focused == "" {
		focused = "(none)"
	}

	// Format like Python
	lines := []string{
		"claudecode-telegram (Go)",
		persistenceNote,
		"",
		fmt.Sprintf("Bot token: %s", redact(h.config.Port)), // We don't have token, show port
		fmt.Sprintf("Admin: %s", adminChatID),
		fmt.Sprintf("Team storage: %s", h.config.SessionsDir),
		"",
		"Team state",
		fmt.Sprintf("Focused worker: %s", focused),
		fmt.Sprintf("Workers: %s", teamList),
	}

	lines = append(lines, "")
	if h.config.SandboxEnabled {
		lines = append(lines, "Sandbox: enabled (Docker isolation)")
		if h.config.SandboxImage != "" {
			lines = append(lines, fmt.Sprintf("Image: %s", h.config.SandboxImage))
		}
		if home, err := os.UserHomeDir(); err == nil {
			lines = append(lines, fmt.Sprintf("Default mount: %s -> /workspace", home))
		}
		if len(h.config.SandboxMounts) > 0 {
			lines = append(lines, "Extra mounts:")
			for _, mount := range h.config.SandboxMounts {
				ro := ""
				if mount.ReadOnly {
					ro = " (ro)"
				}
				lines = append(lines, fmt.Sprintf("  %s -> %s%s", mount.HostPath, mount.ContainerPath, ro))
			}
		}
		lines = append(lines, "")
		lines = append(lines, "Note: Workers run in containers with access")
		lines = append(lines, "only to mounted directories. System paths")
		lines = append(lines, "outside mounts are not accessible.")
	} else {
		lines = append(lines, "Sandbox: disabled (direct execution)")
		lines = append(lines, "Workers run with full system access.")
	}

	h.telegram.SendMessage(chatID, strings.Join(lines, "\n"))
}

func redact(s string) string {
	if s == "" {
		return "(not set)"
	}
	if len(s) <= 8 {
		return "***"
	}
	return s[:4] + "..." + s[len(s)-4:]
}

func (h *Handler) cmdLearn(chatID string, messageID int64, args []string) {
	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	if focused == "" {
		h.telegram.SendMessage(chatID, "No one assigned. Who should I talk to?")
		return
	}

	// Check if worker is online
	if !h.tmux.SessionExists(focused) || !h.tmux.IsClaudeRunning(focused) {
		h.telegram.SendMessage(chatID, fmt.Sprintf("%s is offline. Try /relaunch.", strings.Title(focused)))
		return
	}

	// Build prompt based on whether topic is provided
	var prompt string
	if len(args) > 0 {
		topic := strings.Join(args, " ")
		prompt = fmt.Sprintf("What did you learn about %s today? Please answer in Problem / Fix / Why format:\n"+
			"Problem: <what went wrong or was inefficient>\n"+
			"Fix: <the better approach>\n"+
			"Why: <root cause or insight>", topic)
	} else {
		prompt = "What did you learn today? Please answer in Problem / Fix / Why format:\n" +
			"Problem: <what went wrong or was inefficient>\n" +
			"Fix: <the better approach>\n" +
			"Why: <root cause or insight>"
	}

	// Save chat_id for this session (needed for /response endpoint)
	if h.sessionsDir != "" {
		if err := files.SaveChatID(h.sessionsDir, focused, chatID); err != nil {
			log.Printf("Failed to save chat_id for %s: %v", focused, err)
		}
	}

	// Send typing indicator
	h.telegram.SendChatAction(chatID, "typing")

	// Send prompt to worker
	if err := h.tmux.SendMessage(focused, prompt); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to send prompt: %v", err))
		return
	}

	// Send reaction if Claude accepted the message
	if h.tmux.PromptEmpty(focused, 1*time.Second) {
		if err := h.telegram.SetMessageReaction(chatID, messageID, "\U0001F440"); err != nil {
			log.Printf("Failed to set reaction for message %d: %v", messageID, err)
		}
	} else {
		log.Printf("PromptEmpty returned false for session %s, skipping reaction", focused)
	}
}

func (h *Handler) routeToFocusedWorker(chatID string, messageID int64, text string) {
	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	if focused == "" {
		// List available workers
		sessions, _ := h.tmux.ListSessions()
		if len(sessions) > 0 {
			var names []string
			for _, s := range sessions {
				names = append(names, s.Name)
			}
			h.telegram.SendMessage(chatID, fmt.Sprintf("No one assigned. Your team: %s\nWho should I talk to?", strings.Join(names, ", ")))
		} else {
			h.telegram.SendMessage(chatID, "No team members yet. Add someone with /hire <name>.")
		}
		return
	}

	// Save chat_id for this session (needed for /response endpoint)
	if h.sessionsDir != "" {
		if err := files.SaveChatID(h.sessionsDir, focused, chatID); err != nil {
			log.Printf("Failed to save chat_id for %s: %v", focused, err)
		}
	}

	// Send typing indicator
	h.telegram.SendChatAction(chatID, "typing")

	if err := h.tmux.SendMessage(focused, text); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to send message: %v", err))
		return
	}

	// Send reaction only if Claude accepted the message (prompt is empty)
	if h.tmux.PromptEmpty(focused, 1*time.Second) {
		if err := h.telegram.SetMessageReaction(chatID, messageID, "\U0001F440"); err != nil {
			log.Printf("Failed to set reaction for message %d: %v", messageID, err)
		}
	} else {
		log.Printf("PromptEmpty returned false for session %s, skipping reaction", focused)
	}
}

func (h *Handler) handleResponse(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read body", http.StatusBadRequest)
		return
	}

	var payload ResponsePayload
	if err := json.Unmarshal(body, &payload); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	if payload.Session == "" {
		http.Error(w, "Missing session field", http.StatusBadRequest)
		return
	}

	if payload.Text == "" {
		http.Error(w, "Missing text field", http.StatusBadRequest)
		return
	}

	adminChatID := h.telegram.AdminChatID()

	// Parse media tags from the response text
	cleanedText, mediaTags := telegram.ParseMediaTags(payload.Text)

	// Send text message (with media tags removed)
	// Format: <b>session:</b>\ntext (HTML bold, colon, newline)
	msg := fmt.Sprintf("<b>%s:</b>\n%s", payload.Session, cleanedText)
	if err := h.telegram.SendMessageHTML(adminChatID, msg); err != nil {
		log.Printf("Failed to send response to Telegram: %v", err)
		http.Error(w, "Failed to send message", http.StatusInternalServerError)
		return
	}

	// Send each media file
	for _, tag := range mediaTags {
		// Validate file path (security check)
		if err := telegram.ValidateMediaPath(tag.Path, h.sessionsDir); err != nil {
			log.Printf("Blocked media file: %v", err)
			continue
		}

		// Check file size
		if err := telegram.ValidateMediaSize(tag.Path); err != nil {
			log.Printf("Media file too large: %v", err)
			continue
		}

		// Send the file based on type
		switch tag.Type {
		case "image":
			if err := h.telegram.SendPhoto(adminChatID, tag.Path, tag.Caption); err != nil {
				log.Printf("Failed to send photo: %v", err)
			}
		case "file":
			if err := h.telegram.SendDocument(adminChatID, tag.Path, tag.Caption); err != nil {
				log.Printf("Failed to send document: %v", err)
			}
		}
	}

	w.WriteHeader(http.StatusOK)
}

// NotifyPayload represents the payload for the /notify endpoint.
type NotifyPayload struct {
	Text string `json:"text"`
}

func (h *Handler) handleNotify(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "Failed to read body", http.StatusBadRequest)
		return
	}

	var payload NotifyPayload
	if err := json.Unmarshal(body, &payload); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	if payload.Text == "" {
		http.Error(w, "Missing text", http.StatusBadRequest)
		return
	}

	// Send to all known chat_ids
	chatIDs, err := files.GetAllChatIDs(h.sessionsDir)
	if err != nil {
		log.Printf("Failed to get chat IDs: %v", err)
		// Fall back to admin chat ID
		chatIDs = []string{h.telegram.AdminChatID()}
	}

	// Include admin chat ID if not already present
	adminID := h.telegram.AdminChatID()
	hasAdmin := false
	for _, id := range chatIDs {
		if id == adminID {
			hasAdmin = true
			break
		}
	}
	if !hasAdmin && adminID != "" {
		chatIDs = append(chatIDs, adminID)
	}

	sent := 0
	for _, chatID := range chatIDs {
		if err := h.telegram.SendMessage(chatID, payload.Text); err != nil {
			log.Printf("Failed to send notify to %s: %v", chatID, err)
		} else {
			sent++
		}
	}

	log.Printf("Notify: sent to %d/%d chats: %s...", sent, len(chatIDs), truncate(payload.Text, 50))
	w.WriteHeader(http.StatusOK)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// BroadcastShutdown sends a shutdown message to all known chat_ids.
// Call this before shutting down the server.
func (h *Handler) BroadcastShutdown() {
	chatIDs, err := files.GetAllChatIDs(h.sessionsDir)
	if err != nil {
		log.Printf("Failed to get chat IDs for shutdown: %v", err)
		// Fall back to admin chat ID
		chatIDs = []string{h.telegram.AdminChatID()}
	}

	// Include admin if not present
	adminID := h.telegram.AdminChatID()
	hasAdmin := false
	for _, id := range chatIDs {
		if id == adminID {
			hasAdmin = true
			break
		}
	}
	if !hasAdmin && adminID != "" {
		chatIDs = append(chatIDs, adminID)
	}

	message := "Going offline briefly. Your team stays the same."
	sent := 0
	for _, chatID := range chatIDs {
		if err := h.telegram.SendMessage(chatID, message); err != nil {
			log.Printf("Failed to send shutdown to %s: %v", chatID, err)
		} else {
			sent++
		}
	}

	log.Printf("Shutdown: sent to %d/%d chats", sent, len(chatIDs))
}

// hasFileAttachment checks if the message contains a file attachment.
func (h *Handler) hasFileAttachment(msg *Message) bool {
	return msg.Document != nil || len(msg.Photo) > 0
}

// processFileMessage handles messages with file attachments.
func (h *Handler) processFileMessage(chatID string, messageID int64, msg *Message) {
	// Determine target worker from caption routing
	caption := strings.TrimSpace(msg.Caption)
	targetWorker, messageContent := h.determineFileTarget(caption, msg)

	if targetWorker == "" {
		h.telegram.SendMessage(chatID, "No worker focused. Use /focus <name> first.")
		return
	}

	// Download and save the file
	filePath, fileType, err := h.downloadAndSaveFile(msg, targetWorker)
	if err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to download file: %v", err))
		return
	}

	// Construct the message with file path
	var fullMessage string
	if messageContent != "" {
		fullMessage = fmt.Sprintf("%s\n[%s: %s]", messageContent, fileType, filePath)
	} else {
		fullMessage = fmt.Sprintf("[%s: %s]", fileType, filePath)
	}

	// Save chat_id for this session (needed for /response endpoint)
	if h.sessionsDir != "" {
		if err := files.SaveChatID(h.sessionsDir, targetWorker, chatID); err != nil {
			log.Printf("Failed to save chat_id for %s: %v", targetWorker, err)
		}
	}

	// Send typing indicator
	h.telegram.SendChatAction(chatID, "typing")

	// Send to worker
	if err := h.tmux.SendMessage(targetWorker, fullMessage); err != nil {
		h.telegram.SendMessage(chatID, fmt.Sprintf("Failed to send message: %v", err))
		return
	}

	// Send reaction only if Claude accepted the message (prompt is empty)
	if h.tmux.PromptEmpty(targetWorker, 1*time.Second) {
		if err := h.telegram.SetMessageReaction(chatID, messageID, "\U0001F440"); err != nil {
			log.Printf("Failed to set reaction for message %d: %v", messageID, err)
		}
	} else {
		log.Printf("PromptEmpty returned false for session %s, skipping reaction", targetWorker)
	}
}

// determineFileTarget determines the target worker and message content from the caption.
func (h *Handler) determineFileTarget(caption string, msg *Message) (targetWorker, messageContent string) {
	// Check for direct worker routing via caption (e.g., "/alice Here's the file")
	if strings.HasPrefix(caption, "/") {
		parts := strings.Fields(caption)
		if len(parts) > 0 {
			workerName := strings.TrimPrefix(parts[0], "/")
			// Skip known commands
			cmd := strings.ToLower(parts[0])
			if !h.isKnownCommand(cmd) && h.tmux.SessionExists(workerName) {
				messageContent = strings.TrimSpace(strings.Join(parts[1:], " "))
				return workerName, messageContent
			}
		}
	}

	// Check for reply-to routing
	if msg.ReplyToMessage != nil {
		workerName := h.extractWorkerFromReply(msg.ReplyToMessage.Text)
		if workerName != "" && h.tmux.SessionExists(workerName) {
			return workerName, caption
		}
	}

	// Default to focused worker
	h.mu.RLock()
	focused := h.focusedWorker
	h.mu.RUnlock()

	return focused, caption
}

// downloadAndSaveFile downloads a file from Telegram and saves it to the worker's inbox.
// Returns the file path, file type label (File or Image), and any error.
func (h *Handler) downloadAndSaveFile(msg *Message, workerName string) (filePath, fileType string, err error) {
	var fileID, filename string

	if msg.Document != nil {
		fileID = msg.Document.FileID
		filename = msg.Document.FileName
		if filename == "" {
			filename = "document"
		}
		fileType = "File"
	} else if len(msg.Photo) > 0 {
		// Use the largest photo (last in array)
		photo := msg.Photo[len(msg.Photo)-1]
		fileID = photo.FileID
		filename = fmt.Sprintf("photo_%d.jpg", msg.MessageID)
		fileType = "Image"
	} else {
		return "", "", fmt.Errorf("no file attachment found")
	}

	// Download file from Telegram
	data, err := h.telegram.DownloadFile(fileID)
	if err != nil {
		return "", "", fmt.Errorf("download failed: %w", err)
	}

	// Save to inbox
	inboxDir := files.InboxDir(h.sessionsDir, workerName)
	filePath, err = files.SaveFile(inboxDir, filename, data)
	if err != nil {
		return "", "", fmt.Errorf("save failed: %w", err)
	}

	return filePath, fileType, nil
}

// updateBotCommands registers the bot's command menu with Telegram.
// It includes base commands plus a command for each active worker.
func (h *Handler) updateBotCommands() {
	// Base commands (always present)
	commands := []BotCommand{
		{Command: "hire", Description: "Add a new worker"},
		{Command: "end", Description: "Remove a worker"},
		{Command: "team", Description: "List all workers"},
		{Command: "focus", Description: "Set focus to a worker"},
		{Command: "progress", Description: "Check worker status"},
		{Command: "pause", Description: "Send Escape to worker"},
		{Command: "relaunch", Description: "Restart Claude in session"},
		{Command: "settings", Description: "Show current settings"},
	}

	// Add worker commands
	sessions, err := h.tmux.ListSessions()
	if err != nil {
		log.Printf("Failed to list sessions for bot commands: %v", err)
		// Continue with base commands only
	} else {
		for _, session := range sessions {
			commands = append(commands, BotCommand{
				Command:     session.Name,
				Description: "Message worker " + session.Name,
			})
		}
	}

	// Register commands with Telegram
	if err := h.telegram.SetMyCommands(commands); err != nil {
		log.Printf("Failed to set bot commands: %v", err)
	}
}
