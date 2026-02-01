package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/beastoin/claudecode-telegram/internal/tunnel"
)

func TestParseServeFlags(t *testing.T) {
	tests := []struct {
		name              string
		args              []string
		env               map[string]string
		wantToken         string
		wantAdmin         string
		wantPort          string
		wantPrefix        string
		wantJSONLog       bool
		wantSandbox       bool
		wantSandboxImage  string
		wantSandboxMounts string
		wantErr           bool
	}{
		{
			name:             "all flags",
			args:             []string{"--token", "abc123", "--admin", "999", "--port", "9090", "--prefix", "test-"},
			wantToken:        "abc123",
			wantAdmin:        "999",
			wantPort:         "9090",
			wantPrefix:       "test-",
			wantSandboxImage: "claudecode-telegram:latest",
		},
		{
			name: "env vars only",
			args: []string{},
			env: map[string]string{
				"TELEGRAM_BOT_TOKEN": "env-token",
				"ADMIN_CHAT_ID":      "env-admin",
				"PORT":               "7070",
			},
			wantToken:        "env-token",
			wantAdmin:        "env-admin",
			wantPort:         "7070",
			wantPrefix:       "claude-prod-", // prod node default (no node specified defaults to prod)
			wantSandboxImage: "claudecode-telegram:latest",
		},
		{
			name: "flags override env",
			args: []string{"--token", "flag-token", "--admin", "flag-admin"},
			env: map[string]string{
				"TELEGRAM_BOT_TOKEN": "env-token",
				"ADMIN_CHAT_ID":      "env-admin",
			},
			wantToken:        "flag-token",
			wantAdmin:        "flag-admin",
			wantPort:         "8081",         // prod node default
			wantPrefix:       "claude-prod-", // prod node default
			wantSandboxImage: "claudecode-telegram:latest",
		},
		{
			name:             "defaults",
			args:             []string{"--token", "t", "--admin", "a"},
			wantToken:        "t",
			wantAdmin:        "a",
			wantPort:         "8081",         // prod node default (no node = prod)
			wantPrefix:       "claude-prod-", // prod node default
			wantSandboxImage: "claudecode-telegram:latest",
		},
		{
			name:             "with json flag",
			args:             []string{"--token", "t", "--admin", "a", "--json"},
			wantToken:        "t",
			wantAdmin:        "a",
			wantPort:         "8081",         // prod node default
			wantPrefix:       "claude-prod-", // prod node default
			wantJSONLog:      true,
			wantSandboxImage: "claudecode-telegram:latest",
		},
		{
			name:              "sandbox flags",
			args:              []string{"--token", "t", "--admin", "a", "--sandbox", "--sandbox-image", "sandbox:latest", "--mount", "/host:/container", "--mount-ro", "/secret:/secret"},
			wantToken:         "t",
			wantAdmin:         "a",
			wantPort:          "8081",
			wantPrefix:        "claude-prod-",
			wantSandbox:       true,
			wantSandboxImage:  "sandbox:latest",
			wantSandboxMounts: "/host:/container,ro:/secret:/secret",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// Save all relevant env vars
			savedVars := []string{"TELEGRAM_BOT_TOKEN", "ADMIN_CHAT_ID", "PORT", "TMUX_PREFIX", "SANDBOX_ENABLED", "SANDBOX_IMAGE", "SANDBOX_MOUNTS", "BRIDGE_URL"}
			savedEnv := make(map[string]string)
			for _, k := range savedVars {
				savedEnv[k] = os.Getenv(k)
			}
			for k := range tt.env {
				if _, ok := savedEnv[k]; !ok {
					savedEnv[k] = os.Getenv(k)
				}
			}
			defer func() {
				for k, v := range savedEnv {
					if v == "" {
						os.Unsetenv(k)
					} else {
						os.Setenv(k, v)
					}
				}
			}()

			// Clear all env vars first
			for _, k := range savedVars {
				os.Unsetenv(k)
			}

			// Set test env
			for k, v := range tt.env {
				os.Setenv(k, v)
			}

			cfg, err := parseServeFlags(tt.args)
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if cfg.Token != tt.wantToken {
				t.Errorf("token: want %q, got %q", tt.wantToken, cfg.Token)
			}
			if cfg.AdminChatID != tt.wantAdmin {
				t.Errorf("admin: want %q, got %q", tt.wantAdmin, cfg.AdminChatID)
			}
			if cfg.Port != tt.wantPort {
				t.Errorf("port: want %q, got %q", tt.wantPort, cfg.Port)
			}
			if cfg.Prefix != tt.wantPrefix {
				t.Errorf("prefix: want %q, got %q", tt.wantPrefix, cfg.Prefix)
			}
			if cfg.JSONLog != tt.wantJSONLog {
				t.Errorf("jsonlog: want %v, got %v", tt.wantJSONLog, cfg.JSONLog)
			}
			if cfg.SandboxEnabled != tt.wantSandbox {
				t.Errorf("sandbox: want %v, got %v", tt.wantSandbox, cfg.SandboxEnabled)
			}
			if cfg.SandboxImage != tt.wantSandboxImage {
				t.Errorf("sandbox image: want %q, got %q", tt.wantSandboxImage, cfg.SandboxImage)
			}
			if cfg.SandboxMounts != tt.wantSandboxMounts {
				t.Errorf("sandbox mounts: want %q, got %q", tt.wantSandboxMounts, cfg.SandboxMounts)
			}
		})
	}
}

func TestParseHookFlags(t *testing.T) {
	tests := []struct {
		name        string
		args        []string
		env         map[string]string
		wantURL     string
		wantSession string
		wantErr     bool
	}{
		{
			name:        "all flags",
			args:        []string{"--url", "http://localhost:8080/response", "--session", "alice"},
			wantURL:     "http://localhost:8080/response",
			wantSession: "alice",
		},
		{
			name: "env vars only",
			args: []string{},
			env: map[string]string{
				"BRIDGE_URL":   "http://env.local/response",
				"SESSION_NAME": "bob",
			},
			wantURL:     "http://env.local/response",
			wantSession: "bob",
		},
		{
			name: "flags override env",
			args: []string{"--url", "http://flag.local/response", "--session", "charlie"},
			env: map[string]string{
				"BRIDGE_URL":   "http://env.local/response",
				"SESSION_NAME": "bob",
			},
			wantURL:     "http://flag.local/response",
			wantSession: "charlie",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// Save and restore env
			savedEnv := make(map[string]string)
			for k := range tt.env {
				savedEnv[k] = os.Getenv(k)
			}
			defer func() {
				for k, v := range savedEnv {
					if v == "" {
						os.Unsetenv(k)
					} else {
						os.Setenv(k, v)
					}
				}
			}()

			// Clear env first
			os.Unsetenv("BRIDGE_URL")
			os.Unsetenv("SESSION_NAME")

			// Set test env
			for k, v := range tt.env {
				os.Setenv(k, v)
			}

			cfg, err := parseHookFlags(tt.args)
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if cfg.BridgeURL != tt.wantURL {
				t.Errorf("url: want %q, got %q", tt.wantURL, cfg.BridgeURL)
			}
			if cfg.Session != tt.wantSession {
				t.Errorf("session: want %q, got %q", tt.wantSession, cfg.Session)
			}
		})
	}
}

func TestHookCommand(t *testing.T) {
	var receivedPayload struct {
		Session string `json:"session"`
		Text    string `json:"text"`
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedPayload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	// Simulate stdin
	oldStdin := os.Stdin
	r, w, _ := os.Pipe()
	os.Stdin = r
	defer func() { os.Stdin = oldStdin }()

	go func() {
		w.WriteString("Test message from hook")
		w.Close()
	}()

	args := []string{"--url", server.URL, "--session", "test-worker"}
	cfg, err := parseHookFlags(args)
	if err != nil {
		t.Fatalf("failed to parse flags: %v", err)
	}

	// Use bytes.Buffer for testing
	input := bytes.NewBufferString("Test message from hook")

	err = runHookWithReader(cfg, input)
	if err != nil {
		t.Fatalf("hook command failed: %v", err)
	}

	if receivedPayload.Session != "test-worker" {
		t.Errorf("expected session 'test-worker', got %q", receivedPayload.Session)
	}
	if receivedPayload.Text != "Test message from hook" {
		t.Errorf("expected text 'Test message from hook', got %q", receivedPayload.Text)
	}
}

func TestVersionCommand(t *testing.T) {
	var buf bytes.Buffer
	printVersion(&buf)
	output := buf.String()

	if !strings.Contains(output, "cctg") {
		t.Errorf("version output should contain 'cctg', got %q", output)
	}
	if !strings.Contains(output, version) {
		t.Errorf("version output should contain version %q, got %q", version, output)
	}
}

func TestUsage(t *testing.T) {
	var buf bytes.Buffer
	printUsage(&buf)
	output := buf.String()

	// Check that usage contains expected commands
	if !strings.Contains(output, "serve") {
		t.Error("usage should mention 'serve' command")
	}
	if !strings.Contains(output, "hook") {
		t.Error("usage should mention 'hook' command")
	}
	if !strings.Contains(output, "version") {
		t.Error("usage should mention 'version' command")
	}
}

func TestMainCommandRouting(t *testing.T) {
	tests := []struct {
		name    string
		args    []string
		wantCmd string
	}{
		{
			name:    "serve command",
			args:    []string{"cctg", "serve"},
			wantCmd: "serve",
		},
		{
			name:    "hook command",
			args:    []string{"cctg", "hook"},
			wantCmd: "hook",
		},
		{
			name:    "webhook command",
			args:    []string{"cctg", "webhook"},
			wantCmd: "webhook",
		},
		{
			name:    "tunnel command",
			args:    []string{"cctg", "tunnel"},
			wantCmd: "tunnel",
		},
		{
			name:    "version command",
			args:    []string{"cctg", "version"},
			wantCmd: "version",
		},
		{
			name:    "help flag",
			args:    []string{"cctg", "--help"},
			wantCmd: "help",
		},
		{
			name:    "no args shows help",
			args:    []string{"cctg"},
			wantCmd: "help",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cmd := getCommand(tt.args)
			if cmd != tt.wantCmd {
				t.Errorf("want command %q, got %q", tt.wantCmd, cmd)
			}
		})
	}
}

func TestParseTunnelFlags(t *testing.T) {
	tests := []struct {
		name       string
		args       []string
		env        map[string]string
		wantToken  string
		wantURL    string
		wantBinary string
		wantPath   string
	}{
		{
			name:       "all flags",
			args:       []string{"--token", "abc123", "--url", "http://localhost:9000", "--webhook-path", "/hook", "--cloudflared", "/usr/bin/cloudflared"},
			wantToken:  "abc123",
			wantURL:    "http://localhost:9000",
			wantBinary: "/usr/bin/cloudflared",
			wantPath:   "/hook",
		},
		{
			name: "env vars with node default",
			env: map[string]string{
				"TELEGRAM_BOT_TOKEN": "env-token",
				"NODE_NAME":          "dev",
			},
			wantToken:  "env-token",
			wantURL:    "http://localhost:8082",
			wantBinary: "cloudflared",
			wantPath:   "/webhook",
		},
		{
			name: "env port override",
			env: map[string]string{
				"TELEGRAM_BOT_TOKEN": "env-token",
				"PORT":               "9123",
			},
			wantToken:  "env-token",
			wantURL:    "http://localhost:9123",
			wantBinary: "cloudflared",
			wantPath:   "/webhook",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			savedVars := []string{"TELEGRAM_BOT_TOKEN", "NODE_NAME", "PORT"}
			savedEnv := make(map[string]string)
			for _, k := range savedVars {
				savedEnv[k] = os.Getenv(k)
			}
			for k := range tt.env {
				if _, ok := savedEnv[k]; !ok {
					savedEnv[k] = os.Getenv(k)
				}
			}
			defer func() {
				for k, v := range savedEnv {
					if v == "" {
						os.Unsetenv(k)
					} else {
						os.Setenv(k, v)
					}
				}
			}()

			for _, k := range savedVars {
				os.Unsetenv(k)
			}

			for k, v := range tt.env {
				os.Setenv(k, v)
			}

			cfg, err := parseTunnelFlags(tt.args)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if cfg.Token != tt.wantToken {
				t.Errorf("token: want %q, got %q", tt.wantToken, cfg.Token)
			}
			if cfg.LocalURL != tt.wantURL {
				t.Errorf("url: want %q, got %q", tt.wantURL, cfg.LocalURL)
			}
			if cfg.CloudflaredPath != tt.wantBinary {
				t.Errorf("cloudflared: want %q, got %q", tt.wantBinary, cfg.CloudflaredPath)
			}
			if cfg.WebhookPath != tt.wantPath {
				t.Errorf("webhook path: want %q, got %q", tt.wantPath, cfg.WebhookPath)
			}
		})
	}
}

func TestTunnelConfigValidate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     tunnelConfig
		wantErr string
	}{
		{
			name: "valid config",
			cfg: tunnelConfig{
				Token:    "test-token",
				LocalURL: "http://localhost:8080",
			},
		},
		{
			name: "missing token",
			cfg: tunnelConfig{
				LocalURL: "http://localhost:8080",
			},
			wantErr: "token",
		},
		{
			name: "missing url",
			cfg: tunnelConfig{
				Token: "test-token",
			},
			wantErr: "url",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.cfg.Validate()
			if tt.wantErr == "" {
				if err != nil {
					t.Fatalf("expected no error, got %v", err)
				}
				return
			}
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tt.wantErr)
			}
			if !strings.Contains(strings.ToLower(err.Error()), tt.wantErr) {
				t.Fatalf("expected error containing %q, got %v", tt.wantErr, err)
			}
		})
	}
}

type fakeTunnelRunner struct {
	url string
}

func (f *fakeTunnelRunner) Run(ctx context.Context, cfg tunnel.Config, stdout io.Writer, onURL func(string) error) error {
	if onURL != nil {
		return onURL(f.url)
	}
	return nil
}

func TestTunnelCommandRegistersWebhook(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/setWebhook") {
			t.Errorf("expected path to end with /setWebhook, got %s", r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": true}`))
	}))
	defer server.Close()

	cfg := tunnelConfig{
		Token:       "test-token",
		LocalURL:    "http://localhost:8080",
		WebhookPath: "/webhook",
		baseURL:     server.URL,
	}

	runner := &fakeTunnelRunner{url: "https://example.trycloudflare.com"}

	var stdout bytes.Buffer
	err := runTunnelCommandWithRunner(context.Background(), cfg, &stdout, runner)
	if err != nil {
		t.Fatalf("tunnel command failed: %v", err)
	}

	if receivedBody["url"] != "https://example.trycloudflare.com/webhook" {
		t.Fatalf("expected webhook url, got %v", receivedBody["url"])
	}

	output := stdout.String()
	if !strings.Contains(output, "Webhook registered") {
		t.Errorf("expected output to contain 'Webhook registered', got %q", output)
	}
}

func TestParseWebhookFlags(t *testing.T) {
	tests := []struct {
		name      string
		args      []string
		env       map[string]string
		wantToken string
		wantURL   string
		wantErr   bool
	}{
		{
			name:      "all flags",
			args:      []string{"--token", "abc123", "--url", "https://example.com/webhook"},
			wantToken: "abc123",
			wantURL:   "https://example.com/webhook",
		},
		{
			name: "env vars only",
			args: []string{"--url", "https://example.com/webhook"},
			env: map[string]string{
				"TELEGRAM_BOT_TOKEN": "env-token",
			},
			wantToken: "env-token",
			wantURL:   "https://example.com/webhook",
		},
		{
			name: "flags override env",
			args: []string{"--token", "flag-token", "--url", "https://example.com/webhook"},
			env: map[string]string{
				"TELEGRAM_BOT_TOKEN": "env-token",
			},
			wantToken: "flag-token",
			wantURL:   "https://example.com/webhook",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// Save and restore env
			savedEnv := make(map[string]string)
			savedEnv["TELEGRAM_BOT_TOKEN"] = os.Getenv("TELEGRAM_BOT_TOKEN")
			defer func() {
				for k, v := range savedEnv {
					if v == "" {
						os.Unsetenv(k)
					} else {
						os.Setenv(k, v)
					}
				}
			}()

			// Clear env first
			os.Unsetenv("TELEGRAM_BOT_TOKEN")

			// Set test env
			for k, v := range tt.env {
				os.Setenv(k, v)
			}

			cfg, err := parseWebhookFlags(tt.args)
			if tt.wantErr {
				if err == nil {
					t.Fatal("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if cfg.Token != tt.wantToken {
				t.Errorf("token: want %q, got %q", tt.wantToken, cfg.Token)
			}
			if cfg.URL != tt.wantURL {
				t.Errorf("url: want %q, got %q", tt.wantURL, cfg.URL)
			}
		})
	}
}

func TestWebhookConfigValidate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     webhookConfig
		wantErr string
	}{
		{
			name: "valid config",
			cfg: webhookConfig{
				Token: "test-token",
				URL:   "https://example.com/webhook",
			},
			wantErr: "",
		},
		{
			name: "missing token",
			cfg: webhookConfig{
				URL: "https://example.com/webhook",
			},
			wantErr: "token",
		},
		{
			name: "missing url",
			cfg: webhookConfig{
				Token: "test-token",
			},
			wantErr: "url",
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

func TestWebhookCommand(t *testing.T) {
	// Create test server that accepts setWebhook calls
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/setWebhook") {
			t.Errorf("expected path to end with /setWebhook, got %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": true}`))
	}))
	defer server.Close()

	var stdout bytes.Buffer
	cfg := webhookConfig{
		Token:   "test-token",
		URL:     "https://example.com/webhook",
		baseURL: server.URL,
	}

	err := runWebhookCommand(cfg, &stdout)
	if err != nil {
		t.Fatalf("webhook command failed: %v", err)
	}

	output := stdout.String()
	if !strings.Contains(output, "Webhook registered") {
		t.Errorf("expected output to contain 'Webhook registered', got %q", output)
	}
	if !strings.Contains(output, "https://example.com/webhook") {
		t.Errorf("expected output to contain webhook URL, got %q", output)
	}
}

func TestHookSubcommandRouting(t *testing.T) {
	tests := []struct {
		name       string
		args       []string
		wantSubcmd string
	}{
		{
			name:       "hook install",
			args:       []string{"cctg", "hook", "install"},
			wantSubcmd: "install",
		},
		{
			name:       "hook with no subcommand (direct hook)",
			args:       []string{"cctg", "hook", "--url", "http://example.com"},
			wantSubcmd: "",
		},
		{
			name:       "hook with empty args",
			args:       []string{"cctg", "hook"},
			wantSubcmd: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			subcmd := getHookSubcommand(tt.args)
			if subcmd != tt.wantSubcmd {
				t.Errorf("want subcommand %q, got %q", tt.wantSubcmd, subcmd)
			}
		})
	}
}

func TestHookInstallCommand(t *testing.T) {
	// Create temporary directory to simulate home directory
	tmpDir := t.TempDir()
	claudeDir := tmpDir + "/.claude"

	var stdout bytes.Buffer
	err := runHookInstall(claudeDir, &stdout)
	if err != nil {
		t.Fatalf("hook install failed: %v", err)
	}

	// Check the settings.json was created
	settingsPath := claudeDir + "/settings.json"
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("failed to read settings.json: %v", err)
	}

	// Verify content has expected structure
	var settings map[string]interface{}
	if err := json.Unmarshal(data, &settings); err != nil {
		t.Fatalf("failed to parse settings.json: %v", err)
	}

	hooks, ok := settings["hooks"].(map[string]interface{})
	if !ok {
		t.Fatal("expected 'hooks' key in settings.json")
	}

	stopHooks, ok := hooks["Stop"].([]interface{})
	if !ok {
		t.Fatal("expected 'Stop' key in hooks")
	}

	if len(stopHooks) == 0 {
		t.Fatal("expected at least one Stop hook")
	}

	// Check output message
	output := stdout.String()
	if !strings.Contains(output, "Hook installed") {
		t.Errorf("expected output to contain 'Hook installed', got %q", output)
	}
}

func TestHookInstallUpdatesExisting(t *testing.T) {
	// Create temporary directory
	tmpDir := t.TempDir()
	claudeDir := tmpDir + "/.claude"
	os.MkdirAll(claudeDir, 0755)

	// Create existing settings.json with other content
	existingSettings := `{"someOtherSetting": "value", "hooks": {"SomeOtherHook": []}}`
	settingsPath := claudeDir + "/settings.json"
	os.WriteFile(settingsPath, []byte(existingSettings), 0644)

	var stdout bytes.Buffer
	err := runHookInstall(claudeDir, &stdout)
	if err != nil {
		t.Fatalf("hook install failed: %v", err)
	}

	// Verify the file was updated
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		t.Fatalf("failed to read settings.json: %v", err)
	}

	var settings map[string]interface{}
	if err := json.Unmarshal(data, &settings); err != nil {
		t.Fatalf("failed to parse settings.json: %v", err)
	}

	// Check that the old setting is preserved
	if settings["someOtherSetting"] != "value" {
		t.Error("expected existing setting to be preserved")
	}

	// Check that Stop hook was added
	hooks, ok := settings["hooks"].(map[string]interface{})
	if !ok {
		t.Fatal("expected 'hooks' key in settings.json")
	}

	if _, ok := hooks["Stop"]; !ok {
		t.Fatal("expected 'Stop' key in hooks")
	}

	// Check that other hooks are preserved
	if _, ok := hooks["SomeOtherHook"]; !ok {
		t.Error("expected SomeOtherHook to be preserved")
	}
}

// Multi-node support tests

func TestParseServeFlagsWithNodeFlag(t *testing.T) {
	home, _ := os.UserHomeDir()

	tests := []struct {
		name            string
		args            []string
		env             map[string]string
		wantNodeName    string
		wantPort        string
		wantPrefix      string
		wantSessionsDir string
	}{
		{
			name:            "prod node from flag",
			args:            []string{"--token", "t", "--admin", "a", "--node", "prod"},
			wantNodeName:    "prod",
			wantPort:        "8081",
			wantPrefix:      "claude-prod-",
			wantSessionsDir: home + "/.claude/telegram/nodes/prod/sessions",
		},
		{
			name:            "dev node from flag",
			args:            []string{"--token", "t", "--admin", "a", "--node", "dev"},
			wantNodeName:    "dev",
			wantPort:        "8082",
			wantPrefix:      "claude-dev-",
			wantSessionsDir: home + "/.claude/telegram/nodes/dev/sessions",
		},
		{
			name:            "test node from flag",
			args:            []string{"--token", "t", "--admin", "a", "--node", "test"},
			wantNodeName:    "test",
			wantPort:        "8095",
			wantPrefix:      "claude-test-",
			wantSessionsDir: home + "/.claude/telegram/nodes/test/sessions",
		},
		{
			name: "dev node from NODE_NAME env var",
			args: []string{"--token", "t", "--admin", "a"},
			env: map[string]string{
				"NODE_NAME": "dev",
			},
			wantNodeName:    "dev",
			wantPort:        "8082",
			wantPrefix:      "claude-dev-",
			wantSessionsDir: home + "/.claude/telegram/nodes/dev/sessions",
		},
		{
			name: "flag overrides NODE_NAME env var",
			args: []string{"--token", "t", "--admin", "a", "--node", "test"},
			env: map[string]string{
				"NODE_NAME": "dev",
			},
			wantNodeName:    "test",
			wantPort:        "8095",
			wantPrefix:      "claude-test-",
			wantSessionsDir: home + "/.claude/telegram/nodes/test/sessions",
		},
		{
			name:            "explicit port overrides node default",
			args:            []string{"--token", "t", "--admin", "a", "--node", "prod", "--port", "9000"},
			wantNodeName:    "prod",
			wantPort:        "9000", // Explicit overrides derived
			wantPrefix:      "claude-prod-",
			wantSessionsDir: home + "/.claude/telegram/nodes/prod/sessions",
		},
		{
			name:            "explicit prefix overrides node default",
			args:            []string{"--token", "t", "--admin", "a", "--node", "prod", "--prefix", "custom-"},
			wantNodeName:    "prod",
			wantPort:        "8081",
			wantPrefix:      "custom-", // Explicit overrides derived
			wantSessionsDir: home + "/.claude/telegram/nodes/prod/sessions",
		},
		{
			name:            "custom node name",
			args:            []string{"--token", "t", "--admin", "a", "--node", "mynode"},
			wantNodeName:    "mynode",
			wantPort:        "8080", // Custom nodes default to 8080
			wantPrefix:      "claude-mynode-",
			wantSessionsDir: home + "/.claude/telegram/nodes/mynode/sessions",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// Save all relevant env vars
			savedVars := []string{"TELEGRAM_BOT_TOKEN", "ADMIN_CHAT_ID", "PORT", "TMUX_PREFIX", "NODE_NAME", "SESSIONS_DIR"}
			savedEnv := make(map[string]string)
			for _, k := range savedVars {
				savedEnv[k] = os.Getenv(k)
			}
			defer func() {
				for k, v := range savedEnv {
					if v == "" {
						os.Unsetenv(k)
					} else {
						os.Setenv(k, v)
					}
				}
			}()

			// Clear all env vars first
			for _, k := range savedVars {
				os.Unsetenv(k)
			}

			// Set test env
			for k, v := range tt.env {
				os.Setenv(k, v)
			}

			cfg, err := parseServeFlags(tt.args)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}

			if cfg.NodeName != tt.wantNodeName {
				t.Errorf("node name: want %q, got %q", tt.wantNodeName, cfg.NodeName)
			}
			if cfg.Port != tt.wantPort {
				t.Errorf("port: want %q, got %q", tt.wantPort, cfg.Port)
			}
			if cfg.Prefix != tt.wantPrefix {
				t.Errorf("prefix: want %q, got %q", tt.wantPrefix, cfg.Prefix)
			}
			if cfg.SessionsDir != tt.wantSessionsDir {
				t.Errorf("sessions dir: want %q, got %q", tt.wantSessionsDir, cfg.SessionsDir)
			}
		})
	}
}
