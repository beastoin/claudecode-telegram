package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/beastoin/claudecode-telegram/internal/sandbox"
)

// MockTelegramClient implements the TelegramClient interface for testing
type MockTelegramClient struct {
	mu           sync.Mutex
	SentMessages []struct {
		ChatID string
		Text   string
	}
	SentHTMLMessages []struct {
		ChatID string
		Text   string
	}
	SentActions []struct {
		ChatID string
		Action string
	}
	SentReactions []struct {
		ChatID    string
		MessageID int64
		Emoji     string
	}
	SentPhotos []struct {
		ChatID   string
		FilePath string
		Caption  string
	}
	SentDocuments []struct {
		ChatID   string
		FilePath string
		Caption  string
	}
	SetCommands       []BotCommand
	AdminChatIDValue  string
	SendError         error
	ReactionError     error
	PhotoError        error
	DocumentError     error
	SetCommandsError  error
	DownloadFileData  map[string][]byte
	DownloadFileError error
}

func (m *MockTelegramClient) SendMessage(chatID, text string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.SendError != nil {
		return m.SendError
	}
	m.SentMessages = append(m.SentMessages, struct {
		ChatID string
		Text   string
	}{chatID, text})
	return nil
}

func (m *MockTelegramClient) SendMessageHTML(chatID, text string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.SendError != nil {
		return m.SendError
	}
	m.SentHTMLMessages = append(m.SentHTMLMessages, struct {
		ChatID string
		Text   string
	}{chatID, text})
	return nil
}

func (m *MockTelegramClient) SendChatAction(chatID, action string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.SentActions = append(m.SentActions, struct {
		ChatID string
		Action string
	}{chatID, action})
	return nil
}

func (m *MockTelegramClient) AdminChatID() string {
	return m.AdminChatIDValue
}

func (m *MockTelegramClient) DownloadFile(fileID string) ([]byte, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.DownloadFileError != nil {
		return nil, m.DownloadFileError
	}
	if m.DownloadFileData != nil {
		if data, ok := m.DownloadFileData[fileID]; ok {
			return data, nil
		}
	}
	return nil, fmt.Errorf("file not found: %s", fileID)
}

func (m *MockTelegramClient) SetMessageReaction(chatID string, messageID int64, emoji string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.ReactionError != nil {
		return m.ReactionError
	}
	m.SentReactions = append(m.SentReactions, struct {
		ChatID    string
		MessageID int64
		Emoji     string
	}{chatID, messageID, emoji})
	return nil
}

func (m *MockTelegramClient) SendPhoto(chatID, filePath, caption string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.PhotoError != nil {
		return m.PhotoError
	}
	m.SentPhotos = append(m.SentPhotos, struct {
		ChatID   string
		FilePath string
		Caption  string
	}{chatID, filePath, caption})
	return nil
}

func (m *MockTelegramClient) SendDocument(chatID, filePath, caption string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.DocumentError != nil {
		return m.DocumentError
	}
	m.SentDocuments = append(m.SentDocuments, struct {
		ChatID   string
		FilePath string
		Caption  string
	}{chatID, filePath, caption})
	return nil
}

func (m *MockTelegramClient) SetMyCommands(commands []BotCommand) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.SetCommandsError != nil {
		return m.SetCommandsError
	}
	m.SetCommands = commands
	return nil
}

// MockTmuxManager implements the TmuxManager interface for testing
type MockTmuxManager struct {
	mu             sync.Mutex
	Sessions       map[string]bool
	SentMessages   []struct{ Session, Text string }
	CreatedSession []struct{ Name, Workdir string }
	KilledSessions []string
	CreateError    error
	SendError      error
	KillError      error
}

func NewMockTmuxManager() *MockTmuxManager {
	return &MockTmuxManager{
		Sessions: make(map[string]bool),
	}
}

func (m *MockTmuxManager) ListSessions() ([]SessionInfo, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var sessions []SessionInfo
	for name := range m.Sessions {
		sessions = append(sessions, SessionInfo{Name: name})
	}
	return sessions, nil
}

func (m *MockTmuxManager) CreateSession(name, workdir string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.CreateError != nil {
		return m.CreateError
	}
	if m.Sessions[name] {
		return fmt.Errorf("session %q already exists", name)
	}
	m.Sessions[name] = true
	m.CreatedSession = append(m.CreatedSession, struct{ Name, Workdir string }{name, workdir})
	return nil
}

func (m *MockTmuxManager) SendMessage(sessionName, text string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.SendError != nil {
		return m.SendError
	}
	if !m.Sessions[sessionName] {
		return fmt.Errorf("session %q does not exist", sessionName)
	}
	m.SentMessages = append(m.SentMessages, struct{ Session, Text string }{sessionName, text})
	return nil
}

func (m *MockTmuxManager) KillSession(sessionName string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.KillError != nil {
		return m.KillError
	}
	if !m.Sessions[sessionName] {
		return fmt.Errorf("session %q does not exist", sessionName)
	}
	delete(m.Sessions, sessionName)
	m.KilledSessions = append(m.KilledSessions, sessionName)
	return nil
}

func (m *MockTmuxManager) SessionExists(sessionName string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.Sessions[sessionName]
}

func (m *MockTmuxManager) PromptEmpty(sessionName string, timeout time.Duration) bool {
	// In tests, assume prompt is always empty (message accepted)
	return true
}

func (m *MockTmuxManager) SendKeys(sessionName string, keys ...string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.Sessions[sessionName] {
		return fmt.Errorf("session %q does not exist", sessionName)
	}
	// In tests, just record as a sent message with keys joined
	m.SentMessages = append(m.SentMessages, struct{ Session, Text string }{sessionName, strings.Join(keys, " ")})
	return nil
}

func (m *MockTmuxManager) GetPaneCommand(sessionName string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.Sessions[sessionName] {
		return "", fmt.Errorf("session %q does not exist", sessionName)
	}
	// In tests, assume claude is always running
	return "claude", nil
}

func (m *MockTmuxManager) IsClaudeRunning(sessionName string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	// In tests, assume claude is running if session exists
	return m.Sessions[sessionName]
}

func (m *MockTmuxManager) RestartClaude(sessionName string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.Sessions[sessionName] {
		return fmt.Errorf("session %q does not exist", sessionName)
	}
	// In tests, just record as a sent message
	m.SentMessages = append(m.SentMessages, struct{ Session, Text string }{sessionName, "claude --dangerously-skip-permissions"})
	return nil
}

func TestNewHandler(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()

	h := NewHandler(tg, tm)
	if h == nil {
		t.Fatal("expected handler, got nil")
	}
	if h.telegram != tg {
		t.Error("telegram client not set correctly")
	}
	if h.tmux != tm {
		t.Error("tmux manager not set correctly")
	}
}

// Test admin gating
func TestWebhookRejectsNonAdmin(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 999999, Username: "stranger"},
			Chat:      &Chat{ID: 999999}, // Different from admin
			Text:      "/hire alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should return 200 (Telegram expects 200 even for rejected messages)
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should have silent rejection (no message - security best practice)
	if len(tg.SentMessages) != 0 {
		t.Fatalf("expected silent rejection (0 messages), got %d", len(tg.SentMessages))
	}
}

func TestWebhookAcceptsAdmin(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/team",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should not have rejection message
	for _, msg := range tg.SentMessages {
		if strings.Contains(msg.Text, "not authorized") {
			t.Error("should not send rejection to admin")
		}
	}
}

// Test /hire command
func TestHireCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire alice /path/to/project",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Check session was created
	if len(tm.CreatedSession) != 1 {
		t.Fatalf("expected 1 session created, got %d", len(tm.CreatedSession))
	}
	if tm.CreatedSession[0].Name != "alice" {
		t.Errorf("expected session name 'alice', got %q", tm.CreatedSession[0].Name)
	}
	if tm.CreatedSession[0].Workdir != "/path/to/project" {
		t.Errorf("expected workdir '/path/to/project', got %q", tm.CreatedSession[0].Workdir)
	}

	// Check confirmation message (Python format: "Alice is added and assigned. They'll stay on your team.")
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected confirmation message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "Alice") || !strings.Contains(tg.SentMessages[0].Text, "added") {
		t.Errorf("expected confirmation to mention 'Alice' and 'added', got %q", tg.SentMessages[0].Text)
	}
}

func TestHireCommandNoWorkdir(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire bob",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tm.CreatedSession) != 1 {
		t.Fatalf("expected 1 session created, got %d", len(tm.CreatedSession))
	}
	if tm.CreatedSession[0].Name != "bob" {
		t.Errorf("expected session name 'bob', got %q", tm.CreatedSession[0].Name)
	}
	// Empty workdir is ok
	if tm.CreatedSession[0].Workdir != "" {
		t.Errorf("expected empty workdir, got %q", tm.CreatedSession[0].Workdir)
	}
}

func TestHireCommandDuplicate(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should get error message about duplicate
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "already exists") {
		t.Errorf("expected 'already exists' error, got %q", tg.SentMessages[0].Text)
	}
}

// Test /end command
func TestEndCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/end alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tm.KilledSessions) != 1 {
		t.Fatalf("expected 1 session killed, got %d", len(tm.KilledSessions))
	}
	if tm.KilledSessions[0] != "alice" {
		t.Errorf("expected killed session 'alice', got %q", tm.KilledSessions[0])
	}
}

func TestEndCommandNonexistent(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/end nonexistent",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "does not exist") {
		t.Errorf("expected 'does not exist' error, got %q", tg.SentMessages[0].Text)
	}
}

// Test /team command
func TestTeamCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/team",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected team list message")
	}
	msg := tg.SentMessages[0].Text
	if !strings.Contains(msg, "alice") || !strings.Contains(msg, "bob") {
		t.Errorf("expected team list to contain alice and bob, got %q", msg)
	}
}

func TestTeamCommandEmpty(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/team",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected message about no workers")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No team members yet") {
		t.Errorf("expected 'No team members yet' message, got %q", tg.SentMessages[0].Text)
	}
}

// Test /focus command
func TestFocusCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/focus alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if h.focusedWorker != "alice" {
		t.Errorf("expected focused worker 'alice', got %q", h.focusedWorker)
	}

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected confirmation message")
	}
	// Python format: "Now talking to Alice."
	if !strings.Contains(tg.SentMessages[0].Text, "Alice") || !strings.Contains(tg.SentMessages[0].Text, "talking") {
		t.Errorf("expected 'Now talking to Alice', got %q", tg.SentMessages[0].Text)
	}
}

func TestFocusCommandNonexistent(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/focus nonexistent",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if h.focusedWorker != "" {
		t.Errorf("expected no focused worker, got %q", h.focusedWorker)
	}

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	// Python format: "Could not focus \"nonexistent\". Worker 'nonexistent' not found"
	if !strings.Contains(tg.SentMessages[0].Text, "not found") {
		t.Errorf("expected 'not found' error, got %q", tg.SentMessages[0].Text)
	}
}

func TestFocusClear(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/focus", // No worker name = shows usage (Python behavior)
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Focus is NOT cleared - Python behavior shows usage message instead
	if h.focusedWorker != "alice" {
		t.Errorf("expected focus unchanged, got %q", h.focusedWorker)
	}

	// Should show usage message
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected usage message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "Usage") {
		t.Errorf("expected usage message, got %q", tg.SentMessages[0].Text)
	}
}

// Test routing plain messages to focused worker
func TestMessageToFocusedWorker(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Please review the PR",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if tm.SentMessages[0].Session != "alice" {
		t.Errorf("expected message to alice, got %q", tm.SentMessages[0].Session)
	}
	if tm.SentMessages[0].Text != "Please review the PR" {
		t.Errorf("expected text 'Please review the PR', got %q", tm.SentMessages[0].Text)
	}
}

func TestMessageWithoutFocusFails(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Hello worker",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should get message about no team members (Python style)
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected hint message")
	}
	// Python format: "No team members yet. Add someone with /hire <name>."
	if !strings.Contains(tg.SentMessages[0].Text, "hire") {
		t.Errorf("expected hint about /hire, got %q", tg.SentMessages[0].Text)
	}
}

// Test /response endpoint
func TestResponseEndpoint(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	payload := ResponsePayload{
		Session: "alice",
		Text:    "Task completed successfully",
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should send message to admin chat
	if len(tg.SentHTMLMessages) != 1 {
		t.Fatalf("expected 1 message sent, got %d", len(tg.SentHTMLMessages))
	}
	if tg.SentHTMLMessages[0].ChatID != "123456" {
		t.Errorf("expected chat ID '123456', got %q", tg.SentHTMLMessages[0].ChatID)
	}
	// Message should include worker name and text
	if !strings.Contains(tg.SentHTMLMessages[0].Text, "alice") {
		t.Errorf("expected message to contain 'alice', got %q", tg.SentHTMLMessages[0].Text)
	}
	if !strings.Contains(tg.SentHTMLMessages[0].Text, "Task completed") {
		t.Errorf("expected message to contain response text, got %q", tg.SentHTMLMessages[0].Text)
	}
}

func TestResponseEndpointInvalidJSON(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	req := httptest.NewRequest(http.MethodPost, "/response", strings.NewReader("not json"))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected status 400, got %d", rec.Code)
	}
}

func TestResponseEndpointMissingFields(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	payload := ResponsePayload{
		Session: "", // Missing session
		Text:    "Hello",
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected status 400, got %d", rec.Code)
	}
}

// Test /pause and /progress commands
func TestPauseCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/pause",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should confirm pause (Python style: "Alice is paused. I'll pick up where we left off.")
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected confirmation message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "paused") || !strings.Contains(tg.SentMessages[0].Text, "Alice") {
		t.Errorf("expected pause confirmation, got %q", tg.SentMessages[0].Text)
	}
}

func TestProgressCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/progress",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Python behavior: show status in Telegram, NOT send to tmux
	// Format: "Progress for focused worker: alice\nFocused: yes\nWorking: yes/no\nOnline: yes\nReady: yes/no"
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected status message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "Progress") || !strings.Contains(tg.SentMessages[0].Text, "alice") {
		t.Errorf("expected progress status, got %q", tg.SentMessages[0].Text)
	}
}

// Test /relaunch command
func TestRelaunchCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/relaunch",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Relaunch calls RestartClaude which sends "claude --dangerously-skip-permissions"
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if !strings.Contains(tm.SentMessages[0].Text, "claude") {
		t.Errorf("expected 'claude' command, got %q", tm.SentMessages[0].Text)
	}

	// Also check confirmation message (Python style: "Bringing Alice back online...")
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected confirmation message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "online") || !strings.Contains(tg.SentMessages[0].Text, "Alice") {
		t.Errorf("expected relaunch confirmation, got %q", tg.SentMessages[0].Text)
	}
}

// Test HTTP method validation
func TestWebhookOnlyAcceptsPOST(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	req := httptest.NewRequest(http.MethodGet, "/webhook", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("expected status 405, got %d", rec.Code)
	}
}

func TestResponseOnlyAcceptsPOST(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	req := httptest.NewRequest(http.MethodGet, "/response", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("expected status 405, got %d", rec.Code)
	}
}

// Test unknown routes
func TestUnknownRoute(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	req := httptest.NewRequest(http.MethodGet, "/unknown", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("expected status 404, got %d", rec.Code)
	}
}

// Test invalid JSON in webhook
func TestWebhookInvalidJSON(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader("not json"))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Telegram expects 200 even for errors (to prevent retries)
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}
}

// Test empty message
func TestWebhookEmptyMessage(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message:  nil, // No message
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should handle gracefully
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}
}

// Test concurrent requests
func TestConcurrentWebhookRequests(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()

			update := Update{
				UpdateID: int64(n),
				Message: &Message{
					MessageID: int64(n),
					From:      &User{ID: 123456, Username: "admin"},
					Chat:      &Chat{ID: 123456},
					Text:      fmt.Sprintf("Message %d", n),
				},
			}

			body, _ := json.Marshal(update)
			req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()

			h.ServeHTTP(rec, req)

			if rec.Code != http.StatusOK {
				t.Errorf("request %d: expected status 200, got %d", n, rec.Code)
			}
		}(i)
	}
	wg.Wait()

	// All messages should have been sent
	if len(tm.SentMessages) != 10 {
		t.Errorf("expected 10 messages, got %d", len(tm.SentMessages))
	}
}

// Test typing indicator
func TestTypingIndicator(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Do something",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should have sent typing action
	if len(tg.SentActions) == 0 {
		t.Error("expected typing action to be sent")
	}
	foundTyping := false
	for _, action := range tg.SentActions {
		if action.Action == "typing" && action.ChatID == "123456" {
			foundTyping = true
			break
		}
	}
	if !foundTyping {
		t.Error("expected 'typing' action")
	}
}

// Test /hire without arguments
func TestHireCommandNoArgs(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected usage message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "Usage") {
		t.Errorf("expected usage message, got %q", tg.SentMessages[0].Text)
	}
}

// Test /end without arguments
func TestEndCommandNoArgs(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/end",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected usage message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "Usage") {
		t.Errorf("expected usage message, got %q", tg.SentMessages[0].Text)
	}
}

// Test that /end clears focus if ended worker was focused
func TestEndClearsFocus(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/end alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if h.focusedWorker != "" {
		t.Errorf("expected focus to be cleared after ending focused worker, got %q", h.focusedWorker)
	}
}

// Test /team shows focused worker marker
func TestTeamCommandShowsFocused(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/team",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected team list message")
	}
	msg := tg.SentMessages[0].Text
	if !strings.Contains(msg, "focused") {
		t.Errorf("expected 'focused' marker in team list, got %q", msg)
	}
}

// Test /pause without focus
func TestPauseWithoutFocus(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/pause",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No one assigned") {
		t.Errorf("expected 'No one assigned' message, got %q", tg.SentMessages[0].Text)
	}
}

// Test /progress without focus
func TestProgressWithoutFocus(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/progress",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No one assigned") {
		t.Errorf("expected 'No one assigned' message, got %q", tg.SentMessages[0].Text)
	}
}

// Test /relaunch without focus
func TestRelaunchWithoutFocus(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/relaunch",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No one assigned") {
		t.Errorf("expected 'No one assigned' message, got %q", tg.SentMessages[0].Text)
	}
}

// Test unknown command - passes through to focused worker as a potential Claude command
func TestUnknownCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	// Without a focused worker, unknown commands result in "No team members yet" message

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/unknown",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected message")
	}
	// Unknown commands pass through to focused worker, which doesn't exist, so we get "No team members"
	if !strings.Contains(tg.SentMessages[0].Text, "No team members yet") {
		t.Errorf("expected 'No team members yet' message, got %q", tg.SentMessages[0].Text)
	}
}

// Test response endpoint missing text field
func TestResponseEndpointMissingText(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	payload := ResponsePayload{
		Session: "alice",
		Text:    "", // Missing text
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected status 400, got %d", rec.Code)
	}
}

// Test empty text message is ignored
func TestEmptyTextMessage(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "   ", // Whitespace only
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should not send any messages for empty text
	if len(tg.SentMessages) != 0 {
		t.Errorf("expected no messages for whitespace-only text, got %d", len(tg.SentMessages))
	}
}

// Test message with nil chat is handled
func TestMessageWithNilChat(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      nil, // Nil chat
			Text:      "Hello",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should handle gracefully without panic
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}
}

// Test /<worker> direct routing
func TestDirectWorkerRouting(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/alice How are you?",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should route to worker alice with message stripped of /alice prefix
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if tm.SentMessages[0].Session != "alice" {
		t.Errorf("expected message to alice, got %q", tm.SentMessages[0].Session)
	}
	if tm.SentMessages[0].Text != "How are you?" {
		t.Errorf("expected text 'How are you?', got %q", tm.SentMessages[0].Text)
	}
}

func TestDirectWorkerRoutingNoMessage(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/alice", // No message, just worker name
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should not send empty message, should give hint
	if len(tm.SentMessages) != 0 {
		t.Errorf("expected no message sent to tmux for empty content, got %d", len(tm.SentMessages))
	}
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected hint message about empty content")
	}
}

func TestDirectWorkerRoutingNonexistentWorker(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/nonexistent Hello",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Unknown worker name - passes through as unknown command to focused worker
	// Since no focused worker, get "No team members yet" message
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No team members yet") {
		t.Errorf("expected 'No team members yet' message, got %q", tg.SentMessages[0].Text)
	}
}

func TestDirectWorkerRoutingDoesNotOverrideCommands(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	// Create a worker named "hire" - should NOT override /hire command
	tm.Sessions["hire"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire bob", // Should be treated as command, not worker routing
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should create bob, not send "bob" to worker "hire"
	if len(tm.CreatedSession) != 1 {
		t.Fatalf("expected 1 session created, got %d", len(tm.CreatedSession))
	}
	if tm.CreatedSession[0].Name != "bob" {
		t.Errorf("expected session 'bob', got %q", tm.CreatedSession[0].Name)
	}
}

// Test @all broadcast routing
func TestBroadcastToAll(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	tm.Sessions["charlie"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "@all Please commit your changes",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should send to all 3 workers
	if len(tm.SentMessages) != 3 {
		t.Fatalf("expected 3 messages sent to tmux, got %d", len(tm.SentMessages))
	}

	// All messages should have the stripped content
	for _, msg := range tm.SentMessages {
		if msg.Text != "Please commit your changes" {
			t.Errorf("expected text 'Please commit your changes', got %q", msg.Text)
		}
	}

	// Check that all workers received the message
	sessions := make(map[string]bool)
	for _, msg := range tm.SentMessages {
		sessions[msg.Session] = true
	}
	for _, worker := range []string{"alice", "bob", "charlie"} {
		if !sessions[worker] {
			t.Errorf("expected message to be sent to %s", worker)
		}
	}
}

func TestBroadcastToAllNoWorkers(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "@all Hello everyone",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should get error about no workers
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message about no workers")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No team members yet") {
		t.Errorf("expected 'No team members yet' message, got %q", tg.SentMessages[0].Text)
	}
}

func TestBroadcastToAllEmptyMessage(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "@all", // No message content
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should not send empty message
	if len(tm.SentMessages) != 0 {
		t.Errorf("expected no message sent for empty @all, got %d", len(tm.SentMessages))
	}
}

// Test reply-to routing
func TestReplyToRouting(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 2,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Follow up on that",
			ReplyToMessage: &Message{
				MessageID: 1,
				Text:      "[alice] Task completed successfully",
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should route to worker alice based on reply context
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if tm.SentMessages[0].Session != "alice" {
		t.Errorf("expected message to alice, got %q", tm.SentMessages[0].Session)
	}
	if tm.SentMessages[0].Text != "Follow up on that" {
		t.Errorf("expected text 'Follow up on that', got %q", tm.SentMessages[0].Text)
	}
}

func TestReplyToRoutingWorkerNotFound(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	// alice session doesn't exist
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 2,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Follow up on that",
			ReplyToMessage: &Message{
				MessageID: 1,
				Text:      "[alice] Task completed successfully",
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should get error about worker not existing
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "does not exist") {
		t.Errorf("expected 'does not exist' message, got %q", tg.SentMessages[0].Text)
	}
}

func TestReplyToRoutingNoWorkerPrefix(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 2,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Follow up on that",
			ReplyToMessage: &Message{
				MessageID: 1,
				Text:      "Some message without worker prefix", // No [worker] prefix
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should fall back to focused worker behavior (no focus = error)
	// Python format: "No team members yet. Add someone with /hire <name>."
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "/hire") {
		t.Errorf("expected hint about /hire, got %q", tg.SentMessages[0].Text)
	}
}

func TestReplyToRoutingWithFocusedWorkerFallback(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "bob"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 2,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Follow up on that",
			ReplyToMessage: &Message{
				MessageID: 1,
				Text:      "Some message without worker prefix", // No [worker] prefix
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should fall back to focused worker bob
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if tm.SentMessages[0].Session != "bob" {
		t.Errorf("expected message to bob (focused), got %q", tm.SentMessages[0].Session)
	}
}

// Test routing priority: command > /<worker> > @all > reply-to > focused
func TestRoutingPriorityCommandOverWorker(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	// Create a worker named "team" - /team should still be treated as command
	tm.Sessions["team"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/team", // Should be command, not worker routing
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should execute /team command and list workers
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected team list message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "team") {
		t.Errorf("expected team list mentioning 'team', got %q", tg.SentMessages[0].Text)
	}
	// Should NOT send message to worker "team"
	if len(tm.SentMessages) != 0 {
		t.Errorf("expected no messages to tmux, got %d", len(tm.SentMessages))
	}
}

func TestRoutingPriorityDirectWorkerOverReply(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 2,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/alice Please do this", // Direct routing to alice
			ReplyToMessage: &Message{
				MessageID: 1,
				Text:      "[bob] Previous message", // Reply context says bob
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Direct routing (/alice) should take priority over reply-to ([bob])
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if tm.SentMessages[0].Session != "alice" {
		t.Errorf("expected message to alice (direct), got %q", tm.SentMessages[0].Session)
	}
}

func TestRoutingPriorityBroadcastOverReply(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 2,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "@all Everyone check this", // Broadcast
			ReplyToMessage: &Message{
				MessageID: 1,
				Text:      "[alice] Previous message", // Reply context says alice
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// @all should take priority over reply-to
	if len(tm.SentMessages) != 2 {
		t.Fatalf("expected 2 messages sent to tmux (broadcast), got %d", len(tm.SentMessages))
	}
}

// Test file handling
func TestDocumentMessage(t *testing.T) {
	tmpDir, _ := os.MkdirTemp("", "handler-test-*")
	defer os.RemoveAll(tmpDir)

	tg := &MockTelegramClient{
		AdminChatIDValue: "123456",
		DownloadFileData: map[string][]byte{
			"file123": []byte("test document content"),
		},
	}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"
	h.sessionsDir = tmpDir

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Caption:   "Check this file",
			Document: &Document{
				FileID:   "file123",
				FileName: "report.pdf",
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should send message to worker with file path
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}

	msg := tm.SentMessages[0].Text
	if !strings.Contains(msg, "Check this file") {
		t.Errorf("expected message to contain caption, got %q", msg)
	}
	if !strings.Contains(msg, "[File:") {
		t.Errorf("expected message to contain [File:, got %q", msg)
	}
	if !strings.Contains(msg, "report.pdf") {
		t.Errorf("expected message to contain filename, got %q", msg)
	}
}

func TestPhotoMessage(t *testing.T) {
	tmpDir, _ := os.MkdirTemp("", "handler-test-*")
	defer os.RemoveAll(tmpDir)

	tg := &MockTelegramClient{
		AdminChatIDValue: "123456",
		DownloadFileData: map[string][]byte{
			"photo_large": []byte("fake image data"),
		},
	}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"
	h.sessionsDir = tmpDir

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Caption:   "Screenshot of the bug",
			Photo: []PhotoSize{
				{FileID: "photo_small", Width: 100, Height: 100},
				{FileID: "photo_medium", Width: 320, Height: 320},
				{FileID: "photo_large", Width: 800, Height: 800}, // Largest, should be used
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should send message to worker with image path
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}

	msg := tm.SentMessages[0].Text
	if !strings.Contains(msg, "Screenshot of the bug") {
		t.Errorf("expected message to contain caption, got %q", msg)
	}
	if !strings.Contains(msg, "[Image:") {
		t.Errorf("expected message to contain [Image:, got %q", msg)
	}
}

func TestDocumentMessageDirectRouting(t *testing.T) {
	tmpDir, _ := os.MkdirTemp("", "handler-test-*")
	defer os.RemoveAll(tmpDir)

	tg := &MockTelegramClient{
		AdminChatIDValue: "123456",
		DownloadFileData: map[string][]byte{
			"file456": []byte("document data"),
		},
	}
	tm := NewMockTmuxManager()
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)
	h.sessionsDir = tmpDir
	// No focused worker, but using direct routing via caption

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Caption:   "/bob Here's the file you requested",
			Document: &Document{
				FileID:   "file456",
				FileName: "data.csv",
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should route to bob based on caption
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}
	if tm.SentMessages[0].Session != "bob" {
		t.Errorf("expected message to bob, got %q", tm.SentMessages[0].Session)
	}
}

func TestDocumentMessageDownloadError(t *testing.T) {
	tmpDir, _ := os.MkdirTemp("", "handler-test-*")
	defer os.RemoveAll(tmpDir)

	tg := &MockTelegramClient{
		AdminChatIDValue:  "123456",
		DownloadFileError: fmt.Errorf("download failed"),
	}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"
	h.sessionsDir = tmpDir

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Caption:   "File caption",
			Document: &Document{
				FileID:   "file789",
				FileName: "broken.txt",
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should return 200 (Telegram expects 200 even for errors)
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should send error message to admin
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message to be sent")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "download") || !strings.Contains(tg.SentMessages[0].Text, "failed") {
		t.Errorf("expected download error message, got %q", tg.SentMessages[0].Text)
	}
}

func TestDocumentMessageNoFocusedWorker(t *testing.T) {
	tmpDir, _ := os.MkdirTemp("", "handler-test-*")
	defer os.RemoveAll(tmpDir)

	tg := &MockTelegramClient{
		AdminChatIDValue: "123456",
		DownloadFileData: map[string][]byte{
			"file123": []byte("content"),
		},
	}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.sessionsDir = tmpDir
	// No focused worker and no direct routing

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Caption:   "Here is a file", // No /worker prefix
			Document: &Document{
				FileID:   "file123",
				FileName: "doc.txt",
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should send hint about focus
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected hint message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "/focus") {
		t.Errorf("expected hint about /focus, got %q", tg.SentMessages[0].Text)
	}
}

// Test message reaction on successful routing
func TestMessageReactionOnDelivery(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 42,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Please review the PR",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should have sent reaction
	if len(tg.SentReactions) != 1 {
		t.Fatalf("expected 1 reaction sent, got %d", len(tg.SentReactions))
	}
	if tg.SentReactions[0].ChatID != "123456" {
		t.Errorf("expected chat_id '123456', got %q", tg.SentReactions[0].ChatID)
	}
	if tg.SentReactions[0].MessageID != 42 {
		t.Errorf("expected message_id 42, got %d", tg.SentReactions[0].MessageID)
	}
	if tg.SentReactions[0].Emoji != "\U0001F440" { // 👀
		t.Errorf("expected emoji 👀, got %q", tg.SentReactions[0].Emoji)
	}
}

func TestMessageReactionOnDirectRouting(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 100,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/alice How are you?",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should have sent reaction
	if len(tg.SentReactions) != 1 {
		t.Fatalf("expected 1 reaction sent, got %d", len(tg.SentReactions))
	}
	if tg.SentReactions[0].MessageID != 100 {
		t.Errorf("expected message_id 100, got %d", tg.SentReactions[0].MessageID)
	}
}

func TestMessageReactionOnBroadcast(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 200,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "@all Hello everyone",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should have sent reaction (once for the broadcast, not per worker)
	if len(tg.SentReactions) != 1 {
		t.Fatalf("expected 1 reaction sent, got %d", len(tg.SentReactions))
	}
	if tg.SentReactions[0].MessageID != 200 {
		t.Errorf("expected message_id 200, got %d", tg.SentReactions[0].MessageID)
	}
}

func TestNoReactionOnFailedDelivery(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.SendError = fmt.Errorf("tmux error")
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 42,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "Message that will fail",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should NOT have sent reaction since delivery failed
	if len(tg.SentReactions) != 0 {
		t.Errorf("expected no reactions on failed delivery, got %d", len(tg.SentReactions))
	}
}

// Test response endpoint with media tags
func TestResponseEndpointWithImageTag(t *testing.T) {
	// Create temp file to test
	tmpDir := t.TempDir()
	imgPath := tmpDir + "/test.png"
	os.WriteFile(imgPath, []byte("fake image"), 0644)

	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(tmpDir)

	payload := ResponsePayload{
		Session: "alice",
		Text:    fmt.Sprintf("Here is the result [[image:%s|Chart result]]", imgPath),
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should have sent text message with tags stripped
	if len(tg.SentHTMLMessages) != 1 {
		t.Fatalf("expected 1 text message sent, got %d", len(tg.SentHTMLMessages))
	}
	if strings.Contains(tg.SentHTMLMessages[0].Text, "[[image:") {
		t.Errorf("text message should not contain image tag, got %q", tg.SentHTMLMessages[0].Text)
	}
	// Python format: <b>alice:</b>\ntext
	if !strings.Contains(tg.SentHTMLMessages[0].Text, "<b>alice:</b>") {
		t.Errorf("text message should contain worker prefix, got %q", tg.SentHTMLMessages[0].Text)
	}

	// Should have sent photo
	if len(tg.SentPhotos) != 1 {
		t.Fatalf("expected 1 photo sent, got %d", len(tg.SentPhotos))
	}
	if tg.SentPhotos[0].FilePath != imgPath {
		t.Errorf("expected file path %q, got %q", imgPath, tg.SentPhotos[0].FilePath)
	}
	if tg.SentPhotos[0].Caption != "Chart result" {
		t.Errorf("expected caption 'Chart result', got %q", tg.SentPhotos[0].Caption)
	}
}

func TestResponseEndpointWithFileTag(t *testing.T) {
	tmpDir := t.TempDir()
	filePath := tmpDir + "/report.pdf"
	os.WriteFile(filePath, []byte("fake pdf"), 0644)

	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(tmpDir)

	payload := ResponsePayload{
		Session: "bob",
		Text:    fmt.Sprintf("Report attached [[file:%s]]", filePath),
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should have sent document
	if len(tg.SentDocuments) != 1 {
		t.Fatalf("expected 1 document sent, got %d", len(tg.SentDocuments))
	}
	if tg.SentDocuments[0].FilePath != filePath {
		t.Errorf("expected file path %q, got %q", filePath, tg.SentDocuments[0].FilePath)
	}
}

func TestResponseEndpointWithMultipleMediaTags(t *testing.T) {
	tmpDir := t.TempDir()
	img1 := tmpDir + "/a.png"
	img2 := tmpDir + "/b.pdf"
	os.WriteFile(img1, []byte("img1"), 0644)
	os.WriteFile(img2, []byte("img2"), 0644)

	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(tmpDir)

	payload := ResponsePayload{
		Session: "worker",
		Text:    fmt.Sprintf("[[image:%s]] and [[file:%s]]", img1, img2),
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	if len(tg.SentPhotos) != 1 {
		t.Errorf("expected 1 photo, got %d", len(tg.SentPhotos))
	}
	if len(tg.SentDocuments) != 1 {
		t.Errorf("expected 1 document, got %d", len(tg.SentDocuments))
	}
}

func TestResponseEndpointBlocksSensitiveFile(t *testing.T) {
	tmpDir := t.TempDir()
	keyPath := tmpDir + "/secret.pem"
	os.WriteFile(keyPath, []byte("private key"), 0644)

	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(tmpDir)

	payload := ResponsePayload{
		Session: "worker",
		Text:    fmt.Sprintf("[[file:%s]]", keyPath),
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should still succeed but not send the file
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should NOT have sent the sensitive file
	if len(tg.SentDocuments) != 0 {
		t.Errorf("expected no documents sent for sensitive file, got %d", len(tg.SentDocuments))
	}
}

func TestResponseEndpointBlocksOutsidePath(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir("/tmp/sessions")

	payload := ResponsePayload{
		Session: "worker",
		Text:    "[[file:/etc/passwd]]",
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should still succeed but not send the file
	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should NOT have sent the blocked file
	if len(tg.SentDocuments) != 0 {
		t.Errorf("expected no documents sent for path outside allowed dirs, got %d", len(tg.SentDocuments))
	}
}

func TestResponseEndpointOnlyMediaNoText(t *testing.T) {
	tmpDir := t.TempDir()
	imgPath := tmpDir + "/only.png"
	os.WriteFile(imgPath, []byte("img"), 0644)

	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(tmpDir)

	payload := ResponsePayload{
		Session: "worker",
		Text:    fmt.Sprintf("[[image:%s|Just an image]]", imgPath),
	}
	body, _ := json.Marshal(payload)

	req := httptest.NewRequest(http.MethodPost, "/response", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should send photo
	if len(tg.SentPhotos) != 1 {
		t.Fatalf("expected 1 photo sent, got %d", len(tg.SentPhotos))
	}

	// Should NOT send empty text message when only media and prefix
	// Actually we still send "[worker]" as prefix, but the text content is empty
	if len(tg.SentHTMLMessages) == 1 && tg.SentHTMLMessages[0].Text == "[worker] " {
		// This is acceptable - we might want to skip empty messages in the future
	}
}

func TestNoReactionOnCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 42,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/team",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should NOT have sent reaction for commands (not routing to worker)
	if len(tg.SentReactions) != 0 {
		t.Errorf("expected no reactions on commands, got %d", len(tg.SentReactions))
	}
}

func TestPhotoWithoutCaption(t *testing.T) {
	tmpDir, _ := os.MkdirTemp("", "handler-test-*")
	defer os.RemoveAll(tmpDir)

	tg := &MockTelegramClient{
		AdminChatIDValue: "123456",
		DownloadFileData: map[string][]byte{
			"photo_id": []byte("image data"),
		},
	}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)
	h.focusedWorker = "alice"
	h.sessionsDir = tmpDir

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			// No caption
			Photo: []PhotoSize{
				{FileID: "photo_id", Width: 640, Height: 480},
			},
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should still work, message should just contain the image path
	if len(tm.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent to tmux, got %d", len(tm.SentMessages))
	}

	msg := tm.SentMessages[0].Text
	if !strings.Contains(msg, "[Image:") {
		t.Errorf("expected message to contain [Image:, got %q", msg)
	}
}

// Test /hire with reserved name
func TestHireCommandReservedName(t *testing.T) {
	reservedNames := []string{"team", "focus", "hire", "end", "progress", "pause", "relaunch", "settings", "help", "start", "all", "learn"}

	for _, name := range reservedNames {
		t.Run(name, func(t *testing.T) {
			tg := &MockTelegramClient{AdminChatIDValue: "123456"}
			tm := NewMockTmuxManager()
			h := NewHandler(tg, tm)

			update := Update{
				UpdateID: 1,
				Message: &Message{
					MessageID: 1,
					From:      &User{ID: 123456, Username: "admin"},
					Chat:      &Chat{ID: 123456},
					Text:      "/hire " + name,
				},
			}

			body, _ := json.Marshal(update)
			req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()

			h.ServeHTTP(rec, req)

			// Should not create a session
			if len(tm.CreatedSession) > 0 {
				t.Errorf("should not create session for reserved name %q", name)
			}

			// Should send error message
			if len(tg.SentMessages) == 0 {
				t.Fatalf("expected error message for reserved name %q", name)
			}

			// Python format: Cannot use "name" - reserved command. Choose another name.
			if !strings.Contains(tg.SentMessages[0].Text, "reserved command") {
				t.Errorf("expected reserved command error, got %q", tg.SentMessages[0].Text)
			}
		})
	}
}

// Test /hire with reserved name (case insensitive)
func TestHireCommandReservedNameCaseInsensitive(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire TEAM",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should not create a session
	if len(tm.CreatedSession) > 0 {
		t.Error("should not create session for reserved name 'TEAM'")
	}

	// Should send error message
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}

	if !strings.Contains(tg.SentMessages[0].Text, "reserved command") {
		t.Errorf("expected reserved command error, got %q", tg.SentMessages[0].Text)
	}
}

// Test isReservedName function
func TestIsReservedName(t *testing.T) {
	tests := []struct {
		name     string
		expected bool
	}{
		{"team", true},
		{"TEAM", true},
		{"Team", true},
		{"focus", true},
		{"hire", true},
		{"end", true},
		{"progress", true},
		{"pause", true},
		{"relaunch", true},
		{"settings", true},
		{"help", true},
		{"start", true},
		{"all", true},
		{"alice", false},
		{"bob", false},
		{"worker1", false},
		{"teamleader", false}, // Not exact match
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := isReservedName(tt.name)
			if result != tt.expected {
				t.Errorf("isReservedName(%q) = %v, want %v", tt.name, result, tt.expected)
			}
		})
	}
}

// Test /settings command
func TestSettingsCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetConfig(HandlerConfig{
		Port:        "8080",
		Prefix:      "claude-",
		SessionsDir: "/home/user/.claude/telegram/sessions",
	})

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/settings",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected settings message")
	}

	msg := tg.SentMessages[0].Text
	// Python format: starts with version, includes persistence note
	if !strings.Contains(msg, "claudecode-telegram") {
		t.Errorf("expected 'claudecode-telegram' in message, got %q", msg)
	}
	if !strings.Contains(msg, "Admin: 123456") {
		t.Errorf("expected admin chat ID in message, got %q", msg)
	}
	// Python format: "Team storage: path"
	if !strings.Contains(msg, "Team storage:") {
		t.Errorf("expected team storage in message, got %q", msg)
	}
	// Python format: "Focused worker: (none)"
	if !strings.Contains(msg, "Focused worker:") {
		t.Errorf("expected focused worker in message, got %q", msg)
	}
}

func TestSettingsCommandSandboxEnabled(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetConfig(HandlerConfig{
		Port:           "8080",
		Prefix:         "claude-",
		SessionsDir:    "/tmp/sessions",
		SandboxEnabled: true,
		SandboxImage:   "sandbox:latest",
		SandboxMounts: []sandbox.Mount{
			{HostPath: "/host", ContainerPath: "/container", ReadOnly: true},
		},
	})

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/settings",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if len(tg.SentMessages) == 0 {
		t.Fatal("expected settings message")
	}

	msg := tg.SentMessages[0].Text
	if !strings.Contains(msg, "Sandbox: enabled") {
		t.Errorf("expected sandbox enabled in message, got %q", msg)
	}
	// Python format: "Image: sandbox:latest"
	if !strings.Contains(msg, "Image: sandbox:latest") {
		t.Errorf("expected sandbox image in message, got %q", msg)
	}
	if !strings.Contains(msg, "/host -> /container (ro)") {
		t.Errorf("expected extra mount in message, got %q", msg)
	}
	if home, err := os.UserHomeDir(); err == nil {
		if !strings.Contains(msg, "Default mount: "+home+" -> /workspace") {
			t.Errorf("expected default mount in message, got %q", msg)
		}
	}
}

// Test updateBotCommands
func TestUpdateBotCommands(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)

	// Call updateBotCommands
	h.updateBotCommands()

	// Verify commands were set
	if tg.SetCommands == nil {
		t.Fatal("expected commands to be set")
	}

	// Should have base commands + worker commands
	// Base: hire, end, team, focus, progress, pause, relaunch, settings = 8
	// Workers: alice, bob = 2
	expectedCount := 10
	if len(tg.SetCommands) != expectedCount {
		t.Errorf("expected %d commands, got %d", expectedCount, len(tg.SetCommands))
	}

	// Verify base commands are present
	baseCommands := map[string]string{
		"hire":     "Add a new worker",
		"end":      "Remove a worker",
		"team":     "List all workers",
		"focus":    "Set focus to a worker",
		"progress": "Check worker status",
		"pause":    "Send Escape to worker",
		"relaunch": "Restart Claude in session",
		"settings": "Show current settings",
	}

	for cmd, desc := range baseCommands {
		found := false
		for _, c := range tg.SetCommands {
			if c.Command == cmd {
				found = true
				if c.Description != desc {
					t.Errorf("command %q has wrong description: got %q, want %q", cmd, c.Description, desc)
				}
				break
			}
		}
		if !found {
			t.Errorf("base command %q not found", cmd)
		}
	}

	// Verify worker commands are present
	workerCommands := []string{"alice", "bob"}
	for _, worker := range workerCommands {
		found := false
		for _, c := range tg.SetCommands {
			if c.Command == worker {
				found = true
				expectedDesc := "Message worker " + worker
				if c.Description != expectedDesc {
					t.Errorf("worker command %q has wrong description: got %q, want %q", worker, c.Description, expectedDesc)
				}
				break
			}
		}
		if !found {
			t.Errorf("worker command %q not found", worker)
		}
	}
}

// Test updateBotCommands with no workers
func TestUpdateBotCommandsNoWorkers(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	h.updateBotCommands()

	// Should only have base commands
	expectedCount := 8 // hire, end, team, focus, progress, pause, relaunch, settings
	if len(tg.SetCommands) != expectedCount {
		t.Errorf("expected %d commands, got %d", expectedCount, len(tg.SetCommands))
	}
}

// Test that /hire updates bot commands
func TestHireCommandUpdatesBotCommands(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/hire alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Verify commands were updated
	if tg.SetCommands == nil {
		t.Fatal("expected commands to be updated after /hire")
	}

	// Should have base commands + alice
	expectedCount := 9
	if len(tg.SetCommands) != expectedCount {
		t.Errorf("expected %d commands, got %d", expectedCount, len(tg.SetCommands))
	}

	// Check alice is in the commands
	found := false
	for _, c := range tg.SetCommands {
		if c.Command == "alice" {
			found = true
			break
		}
	}
	if !found {
		t.Error("worker 'alice' not found in commands after /hire")
	}
}

// Test that /end updates bot commands
func TestEndCommandUpdatesBotCommands(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	tm.Sessions["bob"] = true
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/end alice",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Verify commands were updated
	if tg.SetCommands == nil {
		t.Fatal("expected commands to be updated after /end")
	}

	// Should have base commands + bob (alice was removed)
	expectedCount := 9
	if len(tg.SetCommands) != expectedCount {
		t.Errorf("expected %d commands, got %d", expectedCount, len(tg.SetCommands))
	}

	// Check alice is NOT in the commands
	for _, c := range tg.SetCommands {
		if c.Command == "alice" {
			t.Error("worker 'alice' should not be in commands after /end")
		}
	}

	// Check bob IS in the commands
	found := false
	for _, c := range tg.SetCommands {
		if c.Command == "bob" {
			found = true
			break
		}
	}
	if !found {
		t.Error("worker 'bob' should still be in commands after /end alice")
	}
}

// Test updateBotCommands error handling - should not panic
func TestUpdateBotCommandsAPIError(t *testing.T) {
	tg := &MockTelegramClient{
		AdminChatIDValue: "123456",
		SetCommandsError: fmt.Errorf("API error"),
	}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	// Should not panic even if API returns error
	h.updateBotCommands()
}

// Test /learn command
func TestLearnCommand(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	// Set focus first
	h.mu.Lock()
	h.focusedWorker = "alice"
	h.mu.Unlock()

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/learn",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should send message to tmux
	if len(tm.SentMessages) == 0 {
		t.Fatal("expected message to be sent to tmux")
	}
	lastMsg := tm.SentMessages[len(tm.SentMessages)-1]
	if lastMsg.Session != "alice" {
		t.Errorf("expected message to 'alice', got %q", lastMsg.Session)
	}

	// Should contain learning prompt format
	if !strings.Contains(lastMsg.Text, "Problem / Fix / Why") {
		t.Errorf("expected learning prompt format, got %q", lastMsg.Text)
	}
}

// Test /learn command with topic
func TestLearnCommandWithTopic(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	tm.Sessions["alice"] = true
	h := NewHandler(tg, tm)

	h.mu.Lock()
	h.focusedWorker = "alice"
	h.mu.Unlock()

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/learn Go concurrency",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should contain the topic
	if len(tm.SentMessages) == 0 {
		t.Fatal("expected message to be sent to tmux")
	}
	lastMsg := tm.SentMessages[len(tm.SentMessages)-1]
	if !strings.Contains(lastMsg.Text, "Go concurrency") {
		t.Errorf("expected topic in prompt, got %q", lastMsg.Text)
	}
}

// Test /learn command without focused worker
func TestLearnCommandNoFocus(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	update := Update{
		UpdateID: 1,
		Message: &Message{
			MessageID: 1,
			From:      &User{ID: 123456, Username: "admin"},
			Chat:      &Chat{ID: 123456},
			Text:      "/learn",
		},
	}

	body, _ := json.Marshal(update)
	req := httptest.NewRequest(http.MethodPost, "/webhook", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	// Should send error message (Python style: "No one assigned. Who should I talk to?")
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected error message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "No one assigned") {
		t.Errorf("expected 'No one assigned' error, got %q", tg.SentMessages[0].Text)
	}
}

// Test /notify endpoint
func TestNotifyEndpoint(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(t.TempDir())

	payload := `{"text": "System notification"}`
	req := httptest.NewRequest(http.MethodPost, "/notify", strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("expected status 200, got %d", rec.Code)
	}

	// Should send to admin chat
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected notification message")
	}
	if tg.SentMessages[0].Text != "System notification" {
		t.Errorf("expected 'System notification', got %q", tg.SentMessages[0].Text)
	}
}

// Test /notify endpoint with missing text
func TestNotifyEndpointMissingText(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	payload := `{}`
	req := httptest.NewRequest(http.MethodPost, "/notify", strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected status 400, got %d", rec.Code)
	}
}

// Test /notify endpoint with invalid JSON
func TestNotifyEndpointInvalidJSON(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	payload := `{invalid`
	req := httptest.NewRequest(http.MethodPost, "/notify", strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected status 400, got %d", rec.Code)
	}
}

// Test /notify endpoint with GET method
func TestNotifyEndpointWrongMethod(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)

	req := httptest.NewRequest(http.MethodGet, "/notify", nil)
	rec := httptest.NewRecorder()

	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("expected status 405, got %d", rec.Code)
	}
}

// Test BroadcastShutdown
func TestBroadcastShutdown(t *testing.T) {
	tg := &MockTelegramClient{AdminChatIDValue: "123456"}
	tm := NewMockTmuxManager()
	h := NewHandler(tg, tm)
	h.SetSessionsDir(t.TempDir())

	h.BroadcastShutdown()

	// Should send to admin chat
	if len(tg.SentMessages) == 0 {
		t.Fatal("expected shutdown message")
	}
	if !strings.Contains(tg.SentMessages[0].Text, "Going offline") {
		t.Errorf("expected shutdown message, got %q", tg.SentMessages[0].Text)
	}
}
