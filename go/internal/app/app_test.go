package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

// Test Config parsing from flags and environment
func TestConfigFromEnv(t *testing.T) {
	// Save original env vars
	origToken := os.Getenv("TELEGRAM_BOT_TOKEN")
	origAdmin := os.Getenv("ADMIN_CHAT_ID")
	origPort := os.Getenv("PORT")
	defer func() {
		os.Setenv("TELEGRAM_BOT_TOKEN", origToken)
		os.Setenv("ADMIN_CHAT_ID", origAdmin)
		os.Setenv("PORT", origPort)
	}()

	os.Setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
	os.Setenv("ADMIN_CHAT_ID", "987654")
	os.Setenv("PORT", "9090")

	cfg := ConfigFromEnv()

	if cfg.Token != "test-token-123" {
		t.Errorf("expected token 'test-token-123', got %q", cfg.Token)
	}
	if cfg.AdminChatID != "987654" {
		t.Errorf("expected admin chat ID '987654', got %q", cfg.AdminChatID)
	}
	if cfg.Port != "9090" {
		t.Errorf("expected port '9090', got %q", cfg.Port)
	}
}

func TestConfigFromEnvDefaults(t *testing.T) {
	// Clear env vars
	origToken := os.Getenv("TELEGRAM_BOT_TOKEN")
	origAdmin := os.Getenv("ADMIN_CHAT_ID")
	origPort := os.Getenv("PORT")
	origPrefix := os.Getenv("TMUX_PREFIX")
	origNodeName := os.Getenv("NODE_NAME")
	origSessionsDir := os.Getenv("SESSIONS_DIR")
	defer func() {
		os.Setenv("TELEGRAM_BOT_TOKEN", origToken)
		os.Setenv("ADMIN_CHAT_ID", origAdmin)
		os.Setenv("PORT", origPort)
		if origPrefix != "" {
			os.Setenv("TMUX_PREFIX", origPrefix)
		} else {
			os.Unsetenv("TMUX_PREFIX")
		}
		if origNodeName != "" {
			os.Setenv("NODE_NAME", origNodeName)
		} else {
			os.Unsetenv("NODE_NAME")
		}
		if origSessionsDir != "" {
			os.Setenv("SESSIONS_DIR", origSessionsDir)
		} else {
			os.Unsetenv("SESSIONS_DIR")
		}
	}()

	os.Unsetenv("TELEGRAM_BOT_TOKEN")
	os.Unsetenv("ADMIN_CHAT_ID")
	os.Unsetenv("PORT")
	os.Unsetenv("TMUX_PREFIX")
	os.Unsetenv("NODE_NAME")
	os.Unsetenv("SESSIONS_DIR")

	// ConfigFromEnv no longer sets defaults - that's done by DeriveNodeConfig()
	cfg := ConfigFromEnv()
	cfg.DeriveNodeConfig() // Defaults are now set by DeriveNodeConfig

	// Default node is prod, so default port is 8081 and prefix is claude-prod-
	if cfg.Port != "8081" {
		t.Errorf("expected default port '8081' (prod node default), got %q", cfg.Port)
	}
	if cfg.Prefix != "claude-prod-" {
		t.Errorf("expected default prefix 'claude-prod-', got %q", cfg.Prefix)
	}
}

func TestConfigValidate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     Config
		wantErr string
	}{
		{
			name: "valid config",
			cfg: Config{
				Token:       "test-token",
				AdminChatID: "123456",
				Port:        "8080",
				Prefix:      "claude-",
			},
			wantErr: "",
		},
		{
			name: "missing token",
			cfg: Config{
				AdminChatID: "123456",
				Port:        "8080",
				Prefix:      "claude-",
			},
			wantErr: "token",
		},
		{
			name: "missing admin chat ID",
			cfg: Config{
				Token:  "test-token",
				Port:   "8080",
				Prefix: "claude-",
			},
			wantErr: "admin",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.cfg.Validate()
			if tt.wantErr == "" {
				if err != nil {
					t.Errorf("expected no error, got %v", err)
				}
			} else {
				if err == nil {
					t.Errorf("expected error containing %q, got nil", tt.wantErr)
				} else if !strings.Contains(strings.ToLower(err.Error()), tt.wantErr) {
					t.Errorf("expected error containing %q, got %v", tt.wantErr, err)
				}
			}
		})
	}
}

// Test App creation
func TestNewApp(t *testing.T) {
	cfg := Config{
		Token:       "test-token",
		AdminChatID: "123456",
		Port:        "8080",
		Prefix:      "claude-",
	}

	app, err := New(cfg)
	if err != nil {
		t.Fatalf("failed to create app: %v", err)
	}
	if app == nil {
		t.Fatal("expected app, got nil")
	}
}

// Test App graceful shutdown
func TestAppGracefulShutdown(t *testing.T) {
	cfg := Config{
		Token:       "test-token",
		AdminChatID: "123456",
		Port:        "0", // Use random available port
		Prefix:      "claude-",
	}

	app, err := New(cfg)
	if err != nil {
		t.Fatalf("failed to create app: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())

	var wg sync.WaitGroup
	var runErr error

	wg.Add(1)
	go func() {
		defer wg.Done()
		runErr = app.Run(ctx)
	}()

	// Give server time to start
	time.Sleep(50 * time.Millisecond)

	// Cancel context to trigger shutdown
	cancel()

	// Wait for shutdown with timeout
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		// Good - shutdown completed
	case <-time.After(2 * time.Second):
		t.Fatal("shutdown timed out")
	}

	// Context cancellation is not an error
	if runErr != nil && runErr != context.Canceled && !strings.Contains(runErr.Error(), "closed") {
		t.Errorf("unexpected error: %v", runErr)
	}
}

// Test HookConfig parsing
func TestHookConfigFromEnv(t *testing.T) {
	origURL := os.Getenv("BRIDGE_URL")
	origSession := os.Getenv("SESSION_NAME")
	defer func() {
		os.Setenv("BRIDGE_URL", origURL)
		os.Setenv("SESSION_NAME", origSession)
	}()

	os.Setenv("BRIDGE_URL", "http://localhost:8080/response")
	os.Setenv("SESSION_NAME", "alice")

	cfg := HookConfigFromEnv()

	if cfg.BridgeURL != "http://localhost:8080/response" {
		t.Errorf("expected bridge URL 'http://localhost:8080/response', got %q", cfg.BridgeURL)
	}
	if cfg.Session != "alice" {
		t.Errorf("expected session 'alice', got %q", cfg.Session)
	}
}

func TestHookConfigValidate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     HookConfig
		wantErr string
	}{
		{
			name: "valid config",
			cfg: HookConfig{
				BridgeURL: "http://localhost:8080/response",
				Session:   "alice",
			},
			wantErr: "",
		},
		{
			name: "missing bridge URL",
			cfg: HookConfig{
				Session: "alice",
			},
			wantErr: "bridge",
		},
		{
			name: "missing session",
			cfg: HookConfig{
				BridgeURL: "http://localhost:8080/response",
			},
			wantErr: "session",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.cfg.Validate()
			if tt.wantErr == "" {
				if err != nil {
					t.Errorf("expected no error, got %v", err)
				}
			} else {
				if err == nil {
					t.Errorf("expected error containing %q, got nil", tt.wantErr)
				} else if !strings.Contains(strings.ToLower(err.Error()), tt.wantErr) {
					t.Errorf("expected error containing %q, got %v", tt.wantErr, err)
				}
			}
		})
	}
}

// Test hook command sends data to server
func TestRunHook(t *testing.T) {
	var receivedPayload struct {
		Session string `json:"session"`
		Text    string `json:"text"`
	}

	// Create test server
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedPayload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := HookConfig{
		BridgeURL: server.URL,
		Session:   "alice",
	}

	input := bytes.NewBufferString("Hello from Claude!")

	err := RunHook(cfg, input)
	if err != nil {
		t.Fatalf("RunHook failed: %v", err)
	}

	if receivedPayload.Session != "alice" {
		t.Errorf("expected session 'alice', got %q", receivedPayload.Session)
	}
	if receivedPayload.Text != "Hello from Claude!" {
		t.Errorf("expected text 'Hello from Claude!', got %q", receivedPayload.Text)
	}
}

func TestRunHookServerError(t *testing.T) {
	// Create test server that returns error
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	cfg := HookConfig{
		BridgeURL: server.URL,
		Session:   "alice",
	}

	input := bytes.NewBufferString("Hello")

	err := RunHook(cfg, input)
	if err == nil {
		t.Fatal("expected error for server error response")
	}
}

func TestRunHookEmptyInput(t *testing.T) {
	// Create test server
	requestReceived := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestReceived = true
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	cfg := HookConfig{
		BridgeURL: server.URL,
		Session:   "alice",
	}

	input := bytes.NewBufferString("")

	// Empty input should not send request (nothing to send)
	err := RunHook(cfg, input)
	if err != nil {
		t.Fatalf("RunHook failed: %v", err)
	}

	if requestReceived {
		t.Error("should not send request for empty input")
	}
}

// Test adapters
func TestTelegramClientAdapter(t *testing.T) {
	// This tests that the adapter correctly wraps the real client
	// We can't fully test without mocking, but we can verify the interface
	// is satisfied at compile time (done by type assertion)

	// Interface check (compile-time verification)
	var _ TelegramClientInterface = (*telegramClientAdapter)(nil)
}

func TestTmuxManagerAdapter(t *testing.T) {
	// Interface check (compile-time verification)
	var _ TmuxManagerInterface = (*tmuxManagerAdapter)(nil)
}

// MockTelegramClientForApp implements TelegramClientInterface for testing
type MockTelegramClientForApp struct {
	mu           sync.Mutex
	SentMessages []struct {
		ChatID string
		Text   string
	}
	AdminChatIDValue string
	SendError        error
}

func (m *MockTelegramClientForApp) SendMessage(chatID, text string) error {
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

func (m *MockTelegramClientForApp) SendMessageHTML(chatID, text string) error {
	return m.SendMessage(chatID, text)
}

func (m *MockTelegramClientForApp) SendChatAction(chatID, action string) error { return nil }
func (m *MockTelegramClientForApp) SetMessageReaction(chatID string, messageID int64, emoji string) error {
	return nil
}
func (m *MockTelegramClientForApp) SendPhoto(chatID, filePath, caption string) error    { return nil }
func (m *MockTelegramClientForApp) SendDocument(chatID, filePath, caption string) error { return nil }
func (m *MockTelegramClientForApp) AdminChatID() string                                 { return m.AdminChatIDValue }
func (m *MockTelegramClientForApp) DownloadFile(fileID string) ([]byte, error)          { return nil, nil }

// Test startup notification
func TestSendStartupNotification(t *testing.T) {
	mockTg := &MockTelegramClientForApp{AdminChatIDValue: "123456"}

	app := &App{
		config: Config{AdminChatID: "123456"},
	}
	app.telegram = mockTg

	err := app.SendStartupNotification()
	if err != nil {
		t.Fatalf("SendStartupNotification failed: %v", err)
	}

	if len(mockTg.SentMessages) != 1 {
		t.Fatalf("expected 1 message sent, got %d", len(mockTg.SentMessages))
	}

	if mockTg.SentMessages[0].ChatID != "123456" {
		t.Errorf("expected chat ID '123456', got %q", mockTg.SentMessages[0].ChatID)
	}

	if mockTg.SentMessages[0].Text != "Server online." {
		t.Errorf("expected message 'Server online.', got %q", mockTg.SentMessages[0].Text)
	}
}

func TestSendStartupNotificationError(t *testing.T) {
	mockTg := &MockTelegramClientForApp{
		AdminChatIDValue: "123456",
		SendError:        fmt.Errorf("network error"),
	}

	app := &App{
		config: Config{AdminChatID: "123456"},
	}
	app.telegram = mockTg

	// Should return error but not panic
	err := app.SendStartupNotification()
	if err == nil {
		t.Error("expected error, got nil")
	}
}

// Test JSON logger
func TestNewJSONLogger(t *testing.T) {
	var buf bytes.Buffer
	logger := NewJSONLogger(&buf)

	logger.Info("Test message", "port", "8080")

	output := buf.String()

	// Verify it's valid JSON
	var logEntry map[string]interface{}
	if err := json.Unmarshal([]byte(output), &logEntry); err != nil {
		t.Fatalf("expected valid JSON, got error: %v\noutput: %s", err, output)
	}

	// Check required fields
	if logEntry["level"] != "info" {
		t.Errorf("expected level 'info', got %v", logEntry["level"])
	}
	if logEntry["msg"] != "Test message" {
		t.Errorf("expected msg 'Test message', got %v", logEntry["msg"])
	}
	if logEntry["port"] != "8080" {
		t.Errorf("expected port '8080', got %v", logEntry["port"])
	}
	if logEntry["time"] == nil {
		t.Error("expected 'time' field to be present")
	}
}

func TestJSONLoggerLevels(t *testing.T) {
	tests := []struct {
		name    string
		logFunc func(logger *JSONLogger, msg string, kvs ...interface{})
		wantLvl string
	}{
		{
			name: "info",
			logFunc: func(l *JSONLogger, msg string, kvs ...interface{}) {
				l.Info(msg, kvs...)
			},
			wantLvl: "info",
		},
		{
			name: "error",
			logFunc: func(l *JSONLogger, msg string, kvs ...interface{}) {
				l.Error(msg, kvs...)
			},
			wantLvl: "error",
		},
		{
			name: "debug",
			logFunc: func(l *JSONLogger, msg string, kvs ...interface{}) {
				l.Debug(msg, kvs...)
			},
			wantLvl: "debug",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var buf bytes.Buffer
			logger := NewJSONLogger(&buf)

			tt.logFunc(logger, "test", "key", "value")

			var logEntry map[string]interface{}
			if err := json.Unmarshal(buf.Bytes(), &logEntry); err != nil {
				t.Fatalf("expected valid JSON: %v", err)
			}

			if logEntry["level"] != tt.wantLvl {
				t.Errorf("expected level %q, got %v", tt.wantLvl, logEntry["level"])
			}
		})
	}
}

func TestJSONLoggerMultipleKeyValues(t *testing.T) {
	var buf bytes.Buffer
	logger := NewJSONLogger(&buf)

	logger.Info("Server started", "port", "8080", "admin", "123", "prefix", "claude-")

	var logEntry map[string]interface{}
	if err := json.Unmarshal(buf.Bytes(), &logEntry); err != nil {
		t.Fatalf("expected valid JSON: %v", err)
	}

	if logEntry["port"] != "8080" {
		t.Errorf("expected port '8080', got %v", logEntry["port"])
	}
	if logEntry["admin"] != "123" {
		t.Errorf("expected admin '123', got %v", logEntry["admin"])
	}
	if logEntry["prefix"] != "claude-" {
		t.Errorf("expected prefix 'claude-', got %v", logEntry["prefix"])
	}
}

func TestConfigWithJSONLog(t *testing.T) {
	cfg := Config{
		Token:       "test-token",
		AdminChatID: "123456",
		Port:        "8080",
		Prefix:      "claude-",
		JSONLog:     true,
	}

	if !cfg.JSONLog {
		t.Error("expected JSONLog to be true")
	}
}

// Multi-node support tests (TDD: tests written first)

func TestDeriveNodeConfigDefaults(t *testing.T) {
	home, _ := os.UserHomeDir()

	tests := []struct {
		name            string
		nodeName        string
		wantPort        string
		wantPrefix      string
		wantSessionsDir string
	}{
		{
			name:            "prod node defaults",
			nodeName:        "prod",
			wantPort:        "8081",
			wantPrefix:      "claude-prod-",
			wantSessionsDir: home + "/.claude/telegram/nodes/prod/sessions",
		},
		{
			name:            "dev node defaults",
			nodeName:        "dev",
			wantPort:        "8082",
			wantPrefix:      "claude-dev-",
			wantSessionsDir: home + "/.claude/telegram/nodes/dev/sessions",
		},
		{
			name:            "test node defaults",
			nodeName:        "test",
			wantPort:        "8095",
			wantPrefix:      "claude-test-",
			wantSessionsDir: home + "/.claude/telegram/nodes/test/sessions",
		},
		{
			name:            "custom node defaults to port 8080",
			nodeName:        "mynode",
			wantPort:        "8080",
			wantPrefix:      "claude-mynode-",
			wantSessionsDir: home + "/.claude/telegram/nodes/mynode/sessions",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := Config{
				Token:       "test-token",
				AdminChatID: "123456",
				NodeName:    tt.nodeName,
			}
			cfg.DeriveNodeConfig()

			if cfg.Port != tt.wantPort {
				t.Errorf("expected port %q, got %q", tt.wantPort, cfg.Port)
			}
			if cfg.Prefix != tt.wantPrefix {
				t.Errorf("expected prefix %q, got %q", tt.wantPrefix, cfg.Prefix)
			}
			if cfg.SessionsDir != tt.wantSessionsDir {
				t.Errorf("expected sessions dir %q, got %q", tt.wantSessionsDir, cfg.SessionsDir)
			}
		})
	}
}

func TestDeriveNodeConfigEmptyNodeDefaultsToProd(t *testing.T) {
	home, _ := os.UserHomeDir()

	cfg := Config{
		Token:       "test-token",
		AdminChatID: "123456",
		// NodeName is empty
	}
	cfg.DeriveNodeConfig()

	if cfg.NodeName != "prod" {
		t.Errorf("expected node name 'prod', got %q", cfg.NodeName)
	}
	if cfg.Port != "8081" {
		t.Errorf("expected port '8081', got %q", cfg.Port)
	}
	if cfg.Prefix != "claude-prod-" {
		t.Errorf("expected prefix 'claude-prod-', got %q", cfg.Prefix)
	}
	if cfg.SessionsDir != home+"/.claude/telegram/nodes/prod/sessions" {
		t.Errorf("expected sessions dir to end with '/nodes/prod/sessions', got %q", cfg.SessionsDir)
	}
}

func TestDeriveNodeConfigExplicitValuesOverrideDefaults(t *testing.T) {
	cfg := Config{
		Token:       "test-token",
		AdminChatID: "123456",
		NodeName:    "prod",
		Port:        "9000",                 // Explicit port overrides default
		Prefix:      "custom-",              // Explicit prefix overrides default
		SessionsDir: "/custom/sessions/dir", // Explicit dir overrides default
	}
	cfg.DeriveNodeConfig()

	// Explicit values should not be overwritten
	if cfg.Port != "9000" {
		t.Errorf("expected port '9000' (explicit), got %q", cfg.Port)
	}
	if cfg.Prefix != "custom-" {
		t.Errorf("expected prefix 'custom-' (explicit), got %q", cfg.Prefix)
	}
	if cfg.SessionsDir != "/custom/sessions/dir" {
		t.Errorf("expected sessions dir '/custom/sessions/dir' (explicit), got %q", cfg.SessionsDir)
	}
}

func TestConfigFromEnvWithNodeName(t *testing.T) {
	// Save original env vars
	origNodeName := os.Getenv("NODE_NAME")
	origToken := os.Getenv("TELEGRAM_BOT_TOKEN")
	origAdmin := os.Getenv("ADMIN_CHAT_ID")
	origPort := os.Getenv("PORT")
	origPrefix := os.Getenv("TMUX_PREFIX")
	origSessionsDir := os.Getenv("SESSIONS_DIR")
	defer func() {
		if origNodeName != "" {
			os.Setenv("NODE_NAME", origNodeName)
		} else {
			os.Unsetenv("NODE_NAME")
		}
		os.Setenv("TELEGRAM_BOT_TOKEN", origToken)
		os.Setenv("ADMIN_CHAT_ID", origAdmin)
		os.Setenv("PORT", origPort)
		if origPrefix != "" {
			os.Setenv("TMUX_PREFIX", origPrefix)
		} else {
			os.Unsetenv("TMUX_PREFIX")
		}
		if origSessionsDir != "" {
			os.Setenv("SESSIONS_DIR", origSessionsDir)
		} else {
			os.Unsetenv("SESSIONS_DIR")
		}
	}()

	os.Setenv("NODE_NAME", "dev")
	os.Setenv("TELEGRAM_BOT_TOKEN", "test-token")
	os.Setenv("ADMIN_CHAT_ID", "123456")
	os.Unsetenv("PORT")
	os.Unsetenv("TMUX_PREFIX")
	os.Unsetenv("SESSIONS_DIR")

	cfg := ConfigFromEnv()

	if cfg.NodeName != "dev" {
		t.Errorf("expected node name 'dev', got %q", cfg.NodeName)
	}
}
