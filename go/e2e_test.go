// Package e2e_test provides end-to-end tests for the claudecode-telegram Go rewrite.
// These tests wire together all real components to verify complete MVP flows.
package e2e_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/beastoin/claudecode-telegram/internal/server"
	"github.com/beastoin/claudecode-telegram/internal/tmux"
)

// tmuxAvailable checks if tmux is available on the system.
func tmuxAvailable() bool {
	_, err := exec.LookPath("tmux")
	return err == nil
}

// cleanupE2ESessions kills all tmux sessions with the given prefix.
func cleanupE2ESessions(t *testing.T, prefix string) {
	t.Helper()
	cmd := exec.Command("tmux", "list-sessions", "-F", "#{session_name}")
	out, err := cmd.Output()
	if err != nil {
		// No sessions or tmux not running - that's fine
		return
	}
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, prefix) {
			exec.Command("tmux", "kill-session", "-t", line).Run()
		}
	}
}

// MockTelegramAPI is a mock Telegram API server that records all requests.
type MockTelegramAPI struct {
	mu            sync.Mutex
	server        *httptest.Server
	sentMessages  []MockMessage
	sentActions   []MockAction
	adminChatID   string
	getFileResult map[string]string // fileID -> filePath
}

// MockMessage records a sent message.
type MockMessage struct {
	ChatID string
	Text   string
}

// MockAction records a sent chat action.
type MockAction struct {
	ChatID string
	Action string
}

// NewMockTelegramAPI creates a new mock Telegram API server.
func NewMockTelegramAPI(adminChatID string) *MockTelegramAPI {
	mock := &MockTelegramAPI{
		adminChatID:   adminChatID,
		getFileResult: make(map[string]string),
	}

	mock.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mock.mu.Lock()
		defer mock.mu.Unlock()

		// Parse the endpoint from path like /bot<token>/<method>
		path := r.URL.Path
		parts := strings.Split(path, "/")
		if len(parts) < 3 {
			http.Error(w, "invalid path", http.StatusBadRequest)
			return
		}
		method := parts[len(parts)-1]

		switch method {
		case "sendMessage":
			var payload struct {
				ChatID string `json:"chat_id"`
				Text   string `json:"text"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			mock.sentMessages = append(mock.sentMessages, MockMessage{
				ChatID: payload.ChatID,
				Text:   payload.Text,
			})
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"ok":true,"result":{"message_id":1}}`))

		case "sendChatAction":
			var payload struct {
				ChatID string `json:"chat_id"`
				Action string `json:"action"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			mock.sentActions = append(mock.sentActions, MockAction{
				ChatID: payload.ChatID,
				Action: payload.Action,
			})
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"ok":true,"result":true}`))

		case "getFile":
			fileID := r.URL.Query().Get("file_id")
			filePath, ok := mock.getFileResult[fileID]
			if !ok {
				filePath = "files/" + fileID
			}
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(fmt.Sprintf(`{"ok":true,"result":{"file_id":"%s","file_path":"%s"}}`, fileID, filePath)))

		case "setWebhook":
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"ok":true,"result":true}`))

		case "getMe":
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"ok":true,"result":{"id":123,"is_bot":true,"first_name":"TestBot","username":"test_bot"}}`))

		default:
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"ok":true,"result":{}}`))
		}
	}))

	return mock
}

// URL returns the mock server URL.
func (m *MockTelegramAPI) URL() string {
	return m.server.URL
}

// Close shuts down the mock server.
func (m *MockTelegramAPI) Close() {
	m.server.Close()
}

// SentMessages returns a copy of all sent messages.
func (m *MockTelegramAPI) SentMessages() []MockMessage {
	m.mu.Lock()
	defer m.mu.Unlock()
	result := make([]MockMessage, len(m.sentMessages))
	copy(result, m.sentMessages)
	return result
}

// SentActions returns a copy of all sent actions.
func (m *MockTelegramAPI) SentActions() []MockAction {
	m.mu.Lock()
	defer m.mu.Unlock()
	result := make([]MockAction, len(m.sentActions))
	copy(result, m.sentActions)
	return result
}

// ClearMessages clears all recorded messages.
func (m *MockTelegramAPI) ClearMessages() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.sentMessages = nil
	m.sentActions = nil
}

// TelegramClientWithMockAPI is a Telegram client that uses a mock API server.
type TelegramClientWithMockAPI struct {
	baseURL     string
	adminChatID string
	httpClient  *http.Client
}

// NewTelegramClientWithMockAPI creates a new Telegram client pointing to a mock API.
func NewTelegramClientWithMockAPI(mockURL, adminChatID string) *TelegramClientWithMockAPI {
	return &TelegramClientWithMockAPI{
		baseURL:     mockURL,
		adminChatID: adminChatID,
		httpClient:  &http.Client{Timeout: 5 * time.Second},
	}
}

func (c *TelegramClientWithMockAPI) SendMessage(chatID, text string) error {
	payload := map[string]string{
		"chat_id": chatID,
		"text":    text,
	}
	data, _ := json.Marshal(payload)
	resp, err := c.httpClient.Post(c.baseURL+"/botTOKEN/sendMessage", "application/json", bytes.NewReader(data))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func (c *TelegramClientWithMockAPI) SendMessageHTML(chatID, text string) error {
	payload := map[string]string{
		"chat_id":    chatID,
		"text":       text,
		"parse_mode": "HTML",
	}
	data, _ := json.Marshal(payload)
	resp, err := c.httpClient.Post(c.baseURL+"/botTOKEN/sendMessage", "application/json", bytes.NewReader(data))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func (c *TelegramClientWithMockAPI) SendChatAction(chatID, action string) error {
	payload := map[string]string{
		"chat_id": chatID,
		"action":  action,
	}
	data, _ := json.Marshal(payload)
	resp, err := c.httpClient.Post(c.baseURL+"/botTOKEN/sendChatAction", "application/json", bytes.NewReader(data))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	return nil
}

func (c *TelegramClientWithMockAPI) AdminChatID() string {
	return c.adminChatID
}

func (c *TelegramClientWithMockAPI) DownloadFile(fileID string) ([]byte, error) {
	// For e2e tests, return dummy file data
	return []byte("test file content"), nil
}

func (c *TelegramClientWithMockAPI) SetMessageReaction(chatID string, messageID int64, emoji string) error {
	// For e2e tests, no-op (reaction is visual confirmation only)
	return nil
}

func (c *TelegramClientWithMockAPI) SendPhoto(chatID, filePath, caption string) error {
	// For e2e tests, just verify the file exists
	_, err := os.Stat(filePath)
	return err
}

func (c *TelegramClientWithMockAPI) SendDocument(chatID, filePath, caption string) error {
	// For e2e tests, just verify the file exists
	_, err := os.Stat(filePath)
	return err
}

func (c *TelegramClientWithMockAPI) SetMyCommands(commands []server.BotCommand) error {
	// For e2e tests, no-op (command menu is visual only)
	return nil
}

// TmuxManagerAdapter adapts tmux.Manager to server.TmuxManager interface.
type TmuxManagerAdapter struct {
	manager *tmux.Manager
}

func NewTmuxManagerAdapter(prefix string) *TmuxManagerAdapter {
	return &TmuxManagerAdapter{
		manager: tmux.NewManager(prefix),
	}
}

func (a *TmuxManagerAdapter) ListSessions() ([]server.SessionInfo, error) {
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

func (a *TmuxManagerAdapter) CreateSession(name, workdir string) error {
	return a.manager.CreateSession(name, workdir)
}

func (a *TmuxManagerAdapter) SendMessage(sessionName, text string) error {
	return a.manager.SendMessage(sessionName, text)
}

func (a *TmuxManagerAdapter) KillSession(sessionName string) error {
	return a.manager.KillSession(sessionName)
}

func (a *TmuxManagerAdapter) SessionExists(sessionName string) bool {
	return a.manager.SessionExists(sessionName)
}

func (a *TmuxManagerAdapter) PromptEmpty(sessionName string, timeout time.Duration) bool {
	return a.manager.PromptEmpty(sessionName, timeout)
}

func (a *TmuxManagerAdapter) SendKeys(sessionName string, keys ...string) error {
	return a.manager.SendKeys(sessionName, keys...)
}

func (a *TmuxManagerAdapter) GetPaneCommand(sessionName string) (string, error) {
	return a.manager.GetPaneCommand(sessionName)
}

func (a *TmuxManagerAdapter) IsClaudeRunning(sessionName string) bool {
	return a.manager.IsClaudeRunning(sessionName)
}

func (a *TmuxManagerAdapter) RestartClaude(sessionName string) error {
	return a.manager.RestartClaude(sessionName)
}

// E2ETestEnv holds all components for an e2e test.
type E2ETestEnv struct {
	t              *testing.T
	prefix         string
	adminChatID    string
	mockAPI        *MockTelegramAPI
	telegramClient *TelegramClientWithMockAPI
	tmuxAdapter    *TmuxManagerAdapter
	handler        *server.Handler
	webhookServer  *httptest.Server
}

// NewE2ETestEnv creates a new e2e test environment.
func NewE2ETestEnv(t *testing.T, testName string) *E2ETestEnv {
	prefix := fmt.Sprintf("e2e-%s-", testName)
	adminChatID := "123456"

	// Clean up any leftover sessions from previous test runs
	cleanupE2ESessions(t, prefix)

	mockAPI := NewMockTelegramAPI(adminChatID)
	telegramClient := NewTelegramClientWithMockAPI(mockAPI.URL(), adminChatID)
	tmuxAdapter := NewTmuxManagerAdapter(prefix)
	handler := server.NewHandler(telegramClient, tmuxAdapter)

	webhookServer := httptest.NewServer(handler)

	return &E2ETestEnv{
		t:              t,
		prefix:         prefix,
		adminChatID:    adminChatID,
		mockAPI:        mockAPI,
		telegramClient: telegramClient,
		tmuxAdapter:    tmuxAdapter,
		handler:        handler,
		webhookServer:  webhookServer,
	}
}

// Cleanup cleans up all resources.
func (e *E2ETestEnv) Cleanup() {
	e.webhookServer.Close()
	e.mockAPI.Close()
	cleanupE2ESessions(e.t, e.prefix)
}

// SendWebhookUpdate sends a Telegram webhook update to the server.
func (e *E2ETestEnv) SendWebhookUpdate(update server.Update) *http.Response {
	body, _ := json.Marshal(update)
	resp, err := http.Post(e.webhookServer.URL+"/webhook", "application/json", bytes.NewReader(body))
	if err != nil {
		e.t.Fatalf("Failed to send webhook update: %v", err)
	}
	return resp
}

// SendResponseHook sends a response hook POST to the server.
func (e *E2ETestEnv) SendResponseHook(session, text string) *http.Response {
	payload := server.ResponsePayload{
		Session: session,
		Text:    text,
	}
	body, _ := json.Marshal(payload)
	resp, err := http.Post(e.webhookServer.URL+"/response", "application/json", bytes.NewReader(body))
	if err != nil {
		e.t.Fatalf("Failed to send response hook: %v", err)
	}
	return resp
}

// WaitForMessages waits for at least n messages to be sent, with timeout.
func (e *E2ETestEnv) WaitForMessages(n int, timeout time.Duration) []MockMessage {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		msgs := e.mockAPI.SentMessages()
		if len(msgs) >= n {
			return msgs
		}
		time.Sleep(50 * time.Millisecond)
	}
	return e.mockAPI.SentMessages()
}

// TestE2EWorkerLifecycle tests the complete worker lifecycle: /hire -> /focus -> send message -> /end
func TestE2EWorkerLifecycle(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "lifecycle")
	defer env.Cleanup()

	// Step 1: Hire a worker
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice /tmp",
		},
	})
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("Expected 200, got %d", resp.StatusCode)
	}

	// Wait for confirmation message
	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected at least 1 message after /hire")
	}
	// Python format: "Alice is added and assigned. They'll stay on your team."
	if !strings.Contains(msgs[0].Text, "Alice") || !strings.Contains(msgs[0].Text, "added") {
		t.Errorf("Expected hire confirmation, got: %q", msgs[0].Text)
	}

	// Verify session exists
	if !env.tmuxAdapter.SessionExists("alice") {
		t.Error("Expected alice session to exist after /hire")
	}

	// Step 2: Focus on the worker
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/focus alice",
		},
	})
	resp.Body.Close()

	msgs = env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message after /focus")
	}
	// Python format: "Now talking to Alice."
	if !strings.Contains(msgs[0].Text, "Alice") || !strings.Contains(msgs[0].Text, "talking") {
		t.Errorf("Expected focus confirmation, got: %q", msgs[0].Text)
	}

	// Step 3: Send a message to the focused worker
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 3,
		Message: &server.Message{
			MessageID: 3,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "Please review the PR",
		},
	})
	resp.Body.Close()

	// Give tmux time to receive the message
	time.Sleep(200 * time.Millisecond)

	// Verify typing action was sent
	actions := env.mockAPI.SentActions()
	foundTyping := false
	for _, action := range actions {
		if action.Action == "typing" && action.ChatID == env.adminChatID {
			foundTyping = true
			break
		}
	}
	if !foundTyping {
		t.Error("Expected typing action to be sent when sending message to worker")
	}

	// Step 4: End the worker
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 4,
		Message: &server.Message{
			MessageID: 4,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/end alice",
		},
	})
	resp.Body.Close()

	msgs = env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message after /end")
	}
	// Python format: "Alice has been let go."
	if !strings.Contains(msgs[0].Text, "Alice") || !strings.Contains(msgs[0].Text, "let go") {
		t.Errorf("Expected end confirmation, got: %q", msgs[0].Text)
	}

	// Verify session no longer exists
	if env.tmuxAdapter.SessionExists("alice") {
		t.Error("Expected alice session to be gone after /end")
	}
}

// TestE2EMultiWorkerFlow tests managing multiple workers: /hire alice -> /hire bob -> /team -> @all broadcast
func TestE2EMultiWorkerFlow(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "multi")
	defer env.Cleanup()

	// Hire alice
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Hire bob
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire bob",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Verify both sessions exist
	if !env.tmuxAdapter.SessionExists("alice") {
		t.Error("Expected alice session to exist")
	}
	if !env.tmuxAdapter.SessionExists("bob") {
		t.Error("Expected bob session to exist")
	}

	// List team
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 3,
		Message: &server.Message{
			MessageID: 3,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/team",
		},
	})
	resp.Body.Close()

	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message after /team")
	}
	teamMsg := msgs[0].Text
	if !strings.Contains(teamMsg, "alice") {
		t.Errorf("Expected team list to contain alice, got: %q", teamMsg)
	}
	if !strings.Contains(teamMsg, "bob") {
		t.Errorf("Expected team list to contain bob, got: %q", teamMsg)
	}

	// Broadcast to all workers
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 4,
		Message: &server.Message{
			MessageID: 4,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "@all Please commit your changes",
		},
	})
	resp.Body.Close()

	// Give tmux time to receive the messages
	time.Sleep(300 * time.Millisecond)

	// Verify typing action was sent for broadcast
	actions := env.mockAPI.SentActions()
	foundTyping := false
	for _, action := range actions {
		if action.Action == "typing" {
			foundTyping = true
			break
		}
	}
	if !foundTyping {
		t.Error("Expected typing action for broadcast")
	}

	// Cleanup
	env.tmuxAdapter.KillSession("alice")
	env.tmuxAdapter.KillSession("bob")
}

// TestE2EDirectRoutingFlow tests direct worker routing: /alice message -> message received
func TestE2EDirectRoutingFlow(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "direct")
	defer env.Cleanup()

	// Hire alice
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Send message directly to alice without focusing
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/alice Please review the code",
		},
	})
	resp.Body.Close()

	// Give tmux time to receive the message
	time.Sleep(200 * time.Millisecond)

	// Verify typing action was sent
	actions := env.mockAPI.SentActions()
	foundTyping := false
	for _, action := range actions {
		if action.Action == "typing" {
			foundTyping = true
			break
		}
	}
	if !foundTyping {
		t.Error("Expected typing action for direct routing")
	}

	// Cleanup
	env.tmuxAdapter.KillSession("alice")
}

// TestE2EResponseHookFlow tests the /response endpoint: POST /response -> message sent to admin
func TestE2EResponseHookFlow(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "response")
	defer env.Cleanup()

	// Send a response hook (simulating a worker sending a response)
	resp := env.SendResponseHook("alice", "Task completed successfully!")
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("Expected 200, got %d", resp.StatusCode)
	}

	// Verify message was sent to admin
	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected at least 1 message after /response")
	}

	msg := msgs[0]
	if msg.ChatID != env.adminChatID {
		t.Errorf("Expected message to admin chat %q, got %q", env.adminChatID, msg.ChatID)
	}
	if !strings.Contains(msg.Text, "alice") {
		t.Errorf("Expected message to contain worker name 'alice', got: %q", msg.Text)
	}
	if !strings.Contains(msg.Text, "Task completed successfully!") {
		t.Errorf("Expected message to contain response text, got: %q", msg.Text)
	}
}

// TestE2EReplyToRouting tests reply-to routing: reply to [worker] message -> routed to worker
func TestE2EReplyToRouting(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "reply")
	defer env.Cleanup()

	// Hire alice
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Send a reply-to message (replying to a worker's message)
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "Yes, please proceed with that",
			ReplyToMessage: &server.Message{
				MessageID: 1,
				Text:      "[alice] I found a bug in the code, should I fix it?",
			},
		},
	})
	resp.Body.Close()

	// Give tmux time to receive the message
	time.Sleep(200 * time.Millisecond)

	// Verify typing action was sent (indicates message was routed)
	actions := env.mockAPI.SentActions()
	foundTyping := false
	for _, action := range actions {
		if action.Action == "typing" {
			foundTyping = true
			break
		}
	}
	if !foundTyping {
		t.Error("Expected typing action for reply-to routing")
	}

	// Cleanup
	env.tmuxAdapter.KillSession("alice")
}

// TestE2EAdminGating tests that non-admin users are rejected.
func TestE2EAdminGating(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "admin")
	defer env.Cleanup()

	// Non-admin tries to hire a worker
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 999999, Username: "stranger"}, // Different user ID
			Chat:      &server.Chat{ID: 999999},                       // Different chat ID
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()

	// Should still return 200 (Telegram expects 200)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("Expected 200, got %d", resp.StatusCode)
	}

	// Silent rejection (security best practice) - no message sent
	msgs := env.WaitForMessages(1, 500*time.Millisecond)
	if len(msgs) > 0 {
		t.Errorf("Expected silent rejection (no message), got: %q", msgs[0].Text)
	}

	// Verify session was NOT created
	if env.tmuxAdapter.SessionExists("alice") {
		t.Error("Session should not be created for non-admin user")
	}
}

// TestE2EFocusedWorkerShowsInTeam tests that focused worker is marked in /team output.
func TestE2EFocusedWorkerShowsInTeam(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "focusteam")
	defer env.Cleanup()

	// Hire two workers
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire bob",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Focus on alice
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 3,
		Message: &server.Message{
			MessageID: 3,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/focus alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// List team
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 4,
		Message: &server.Message{
			MessageID: 4,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/team",
		},
	})
	resp.Body.Close()

	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message after /team")
	}

	teamMsg := msgs[0].Text
	if !strings.Contains(teamMsg, "focused") {
		t.Errorf("Expected 'focused' marker in team list, got: %q", teamMsg)
	}
	// Verify alice is the one marked as focused
	if !strings.Contains(teamMsg, "alice") || !strings.Contains(teamMsg, "focused") {
		t.Errorf("Expected alice to be marked as focused, got: %q", teamMsg)
	}

	// Cleanup
	env.tmuxAdapter.KillSession("alice")
	env.tmuxAdapter.KillSession("bob")
}

// TestE2EEndClearsFocus tests that /end clears focus if ended worker was focused.
func TestE2EEndClearsFocus(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "endfocus")
	defer env.Cleanup()

	// Hire and focus on alice
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/focus alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// End alice
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 3,
		Message: &server.Message{
			MessageID: 3,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/end alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Try to send a message - should fail because focus was cleared
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 4,
		Message: &server.Message{
			MessageID: 4,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "This should not work",
		},
	})
	resp.Body.Close()

	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message about no focus")
	}
	// Python format: "No team members yet. Add someone with /hire <name>."
	if !strings.Contains(msgs[0].Text, "/hire") {
		t.Errorf("Expected hint about /hire, got: %q", msgs[0].Text)
	}
}

// TestE2EConcurrentWorkerMessages tests sending concurrent messages to multiple workers.
func TestE2EConcurrentWorkerMessages(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "concurrent")
	defer env.Cleanup()

	// Hire multiple workers
	workers := []string{"alice", "bob", "charlie"}
	for i, worker := range workers {
		resp := env.SendWebhookUpdate(server.Update{
			UpdateID: int64(i + 1),
			Message: &server.Message{
				MessageID: int64(i + 1),
				From:      &server.User{ID: 123456, Username: "admin"},
				Chat:      &server.Chat{ID: 123456},
				Text:      "/hire " + worker,
			},
		})
		resp.Body.Close()
		env.WaitForMessages(i+1, 2*time.Second)
	}

	// Verify all workers exist
	for _, worker := range workers {
		if !env.tmuxAdapter.SessionExists(worker) {
			t.Errorf("Expected %s session to exist", worker)
		}
	}

	// Send concurrent messages to all workers via direct routing
	env.mockAPI.ClearMessages()
	var wg sync.WaitGroup
	for i, worker := range workers {
		wg.Add(1)
		go func(idx int, w string) {
			defer wg.Done()
			resp := env.SendWebhookUpdate(server.Update{
				UpdateID: int64(100 + idx),
				Message: &server.Message{
					MessageID: int64(100 + idx),
					From:      &server.User{ID: 123456, Username: "admin"},
					Chat:      &server.Chat{ID: 123456},
					Text:      fmt.Sprintf("/%s Task for you: %d", w, idx),
				},
			})
			resp.Body.Close()
		}(i, worker)
	}
	wg.Wait()

	// Give tmux time to receive all messages
	time.Sleep(500 * time.Millisecond)

	// All workers should have received messages (verify via typing actions)
	actions := env.mockAPI.SentActions()
	typingCount := 0
	for _, action := range actions {
		if action.Action == "typing" {
			typingCount++
		}
	}
	if typingCount != len(workers) {
		t.Errorf("Expected %d typing actions, got %d", len(workers), typingCount)
	}

	// Cleanup
	for _, worker := range workers {
		env.tmuxAdapter.KillSession(worker)
	}
}

// TestE2EResponseHookInvalidPayload tests /response endpoint with invalid payloads.
func TestE2EResponseHookInvalidPayload(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "resperr")
	defer env.Cleanup()

	tests := []struct {
		name    string
		payload string
		status  int
	}{
		{"invalid json", "not json", http.StatusBadRequest},
		{"missing session", `{"text":"hello"}`, http.StatusBadRequest},
		{"missing text", `{"session":"alice"}`, http.StatusBadRequest},
		{"empty session", `{"session":"","text":"hello"}`, http.StatusBadRequest},
		{"empty text", `{"session":"alice","text":""}`, http.StatusBadRequest},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			resp, err := http.Post(env.webhookServer.URL+"/response", "application/json", strings.NewReader(tt.payload))
			if err != nil {
				t.Fatalf("Failed to send request: %v", err)
			}
			resp.Body.Close()

			if resp.StatusCode != tt.status {
				t.Errorf("Expected status %d, got %d", tt.status, resp.StatusCode)
			}
		})
	}
}

// TestE2EPauseAndProgressCommands tests /pause and /progress commands.
func TestE2EPauseAndProgressCommands(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "pauseprog")
	defer env.Cleanup()

	// Hire and focus on alice
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/focus alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Test /pause
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 3,
		Message: &server.Message{
			MessageID: 3,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/pause",
		},
	})
	resp.Body.Close()

	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message after /pause")
	}
	// Python format: "Alice is paused. I'll pick up where we left off."
	if !strings.Contains(msgs[0].Text, "paused") {
		t.Errorf("Expected pause confirmation, got: %q", msgs[0].Text)
	}

	// Test /progress
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 4,
		Message: &server.Message{
			MessageID: 4,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/progress",
		},
	})
	resp.Body.Close()

	// /progress sends /status to the tmux session, no direct response
	time.Sleep(200 * time.Millisecond)

	// Cleanup
	env.tmuxAdapter.KillSession("alice")
}

// TestE2ERelaunchCommand tests the /relaunch command.
func TestE2ERelaunchCommand(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	env := NewE2ETestEnv(t, "relaunch")
	defer env.Cleanup()

	// Hire and focus on alice
	resp := env.SendWebhookUpdate(server.Update{
		UpdateID: 1,
		Message: &server.Message{
			MessageID: 1,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/hire alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 2,
		Message: &server.Message{
			MessageID: 2,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/focus alice",
		},
	})
	resp.Body.Close()
	env.WaitForMessages(1, 2*time.Second)

	// Test /relaunch
	env.mockAPI.ClearMessages()
	resp = env.SendWebhookUpdate(server.Update{
		UpdateID: 3,
		Message: &server.Message{
			MessageID: 3,
			From:      &server.User{ID: 123456, Username: "admin"},
			Chat:      &server.Chat{ID: 123456},
			Text:      "/relaunch",
		},
	})
	resp.Body.Close()

	msgs := env.WaitForMessages(1, 2*time.Second)
	if len(msgs) < 1 {
		t.Fatal("Expected message after /relaunch")
	}
	// Python format: "Bringing Alice back online..."
	if !strings.Contains(msgs[0].Text, "online") || !strings.Contains(msgs[0].Text, "Alice") {
		t.Errorf("Expected relaunch confirmation, got: %q", msgs[0].Text)
	}

	// Cleanup
	env.tmuxAdapter.KillSession("alice")
}
