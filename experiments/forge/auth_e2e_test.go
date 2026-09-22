package forge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"testing/fstest"
	"time"
)

func TestAuthE2E_FullLifecycle(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	localPort := findFreePort(t)

	fakeScript := filepath.Join(tmpDir, "fake-claude.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Select login method"
echo "1. Browser"
echo "2. API Key"
read -t 15 _choice
sleep 0.3
echo "Visit https://claude.ai/oauth/authorize?code=e2e-full to authenticate"
read -t 60 auth_code
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "What can I help you with?"
fi
sleep 3
`), 0755)

	manifest := fmt.Sprintf(`
name: e2e-auth-test
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-auth-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-auth-test").Run()
	})

	engine := &fakeAuthEngine{launchCmd: fakeScript}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "local",
				"--connector-opt", fmt.Sprintf("LOCAL_PORT=%d", localPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	localURL := fmt.Sprintf("http://127.0.0.1:%d", localPort)
	if !waitForURL(t, localURL+"/health", 15*time.Second) {
		t.Fatal("local connector never became ready")
	}

	var authURL string
	deadline := time.After(15 * time.Second)
	for {
		resp, err := http.Get(localURL + "/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			var responses []struct{ Text string }
			json.Unmarshal(body, &responses)
			for _, r := range responses {
				if strings.Contains(r.Text, "claude.ai/oauth") {
					authURL = r.Text
					break
				}
			}
		}
		if authURL != "" {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared in responses")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	if !strings.Contains(authURL, "claude.ai/oauth/authorize") {
		t.Fatalf("auth prompt = %q, want to contain OAuth URL", authURL)
	}

	codeBody := bytes.NewBufferString(`{"text":"e2e-test-code-999","from":"manager"}`)
	resp, err := http.Post(localURL+"/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST /send error = %v", err)
	}
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}

func TestAuthE2E_AlreadyAuthenticated(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	localPort := findFreePort(t)

	fakeScript := filepath.Join(tmpDir, "fake-claude-authed.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "What can I help you with?"
sleep 5
`), 0755)

	manifest := fmt.Sprintf(`
name: e2e-already-authed
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-authed-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-already-authed").Run()
	})

	engine := &fakeAuthEngine{launchCmd: fakeScript}

	var stdout bytes.Buffer
	err := RunEmbeddedWorker(
		[]string{"auth",
			"--connector", "local",
			"--connector-opt", fmt.Sprintf("LOCAL_PORT=%d", localPort),
			"--session-prefix", sessionPrefix,
		},
		WorkerDeps{
			Assets: EmbeddedAssets{
				Manifest: []byte(manifest),
				Files:    fstest.MapFS{},
			},
			Engine: engine,
			GOOS:   "linux",
			Stdout: &stdout,
		},
	)
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}
}

func TestAuthE2E_AutoResponseAndURL(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	localPort := findFreePort(t)

	fakeScript := filepath.Join(tmpDir, "fake-claude-auto.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Select login method"
read -t 15 _
sleep 0.3
echo "Choose the text style"
read -t 15 _
sleep 0.3
echo "Visit https://claude.com/cai/oauth/authorize?code=auto-e2e"
read -t 60 auth_code
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "What can I help you with?"
fi
sleep 3
`), 0755)

	manifest := fmt.Sprintf(`
name: e2e-auto-auth
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-auto-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-auto-auth").Run()
	})

	engine := &fakeAuthEngine{launchCmd: fakeScript}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "local",
				"--connector-opt", fmt.Sprintf("LOCAL_PORT=%d", localPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	localURL := fmt.Sprintf("http://127.0.0.1:%d", localPort)
	if !waitForURL(t, localURL+"/health", 15*time.Second) {
		t.Fatal("local connector never became ready")
	}

	deadline := time.After(20 * time.Second)
	found := false
	for !found {
		resp, err := http.Get(localURL + "/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if strings.Contains(string(body), "claude.com/cai/oauth") {
				found = true
			}
		}
		if found {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	codeBody := bytes.NewBufferString(`{"text":"auto-e2e-code","from":"manager"}`)
	resp, err := http.Post(localURL+"/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST /send error = %v", err)
	}
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}

// fakeAuthEngine provides a test EngineDriver that launches a custom script
// and declares the same AuthSpec as ClaudeCodeDriver but with shorter timeouts.
type fakeAuthEngine struct {
	launchCmd string
}

func (e *fakeAuthEngine) ID() string { return "fake-auth" }
func (e *fakeAuthEngine) Capabilities() EngineCapabilities {
	return EngineCapabilities{}
}
func (e *fakeAuthEngine) Prepare(_ context.Context, _ PrepareRequest) (*PreparedEngine, error) {
	return &PreparedEngine{Env: map[string]string{}}, nil
}
func (e *fakeAuthEngine) StartSpec(_ context.Context, _ *PreparedEngine) (*StartSpec, error) {
	return &StartSpec{
		Command: []string{e.launchCmd},
	}, nil
}
func (e *fakeAuthEngine) AuthSpec() AuthSpec {
	return AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?", "claude>", "$  "},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    30 * time.Second,
		AutoResponses: []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
			{Marker: "Choose the text style", Response: "", Once: false},
		},
	}
}

// scenarioEngine uses the Go fake-claude binary with a JSON scenario file.
type scenarioEngine struct {
	fakeBinary   string
	scenarioPath string
	autoResponses []AutoResponse
}

func (e *scenarioEngine) ID() string { return "scenario-auth" }
func (e *scenarioEngine) Capabilities() EngineCapabilities {
	return EngineCapabilities{}
}
func (e *scenarioEngine) Prepare(_ context.Context, _ PrepareRequest) (*PreparedEngine, error) {
	return &PreparedEngine{Env: map[string]string{}}, nil
}
func (e *scenarioEngine) StartSpec(_ context.Context, _ *PreparedEngine) (*StartSpec, error) {
	return &StartSpec{
		Command: []string{e.fakeBinary, "-scenario", e.scenarioPath},
	}, nil
}
func (e *scenarioEngine) AuthSpec() AuthSpec {
	autoResp := e.autoResponses
	if autoResp == nil {
		autoResp = []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
		}
	}
	return AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?", "claude>"},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    30 * time.Second,
		AutoResponses:  autoResp,
	}
}

func buildFakeClaude(t *testing.T) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "fake-claude")
	cmd := exec.Command("go", "build", "-o", binary, "./testdata/fake-claude/")
	cmd.Dir = filepath.Join(".")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("build fake-claude: %v\n%s", err, out)
	}
	return binary
}

func TestAuthE2E_ScenarioHappyPath(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	fakeBinary := buildFakeClaude(t)
	tmpDir := t.TempDir()
	localPort := findFreePort(t)
	scenarioPath := filepath.Join("testdata", "fake-claude", "scenarios", "happy_path.json")

	manifest := fmt.Sprintf(`
name: e2e-scenario-happy
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-scenario-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-scenario-happy").Run()
	})

	engine := &scenarioEngine{
		fakeBinary:   fakeBinary,
		scenarioPath: scenarioPath,
	}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "local",
				"--connector-opt", fmt.Sprintf("LOCAL_PORT=%d", localPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	localURL := fmt.Sprintf("http://127.0.0.1:%d", localPort)
	if !waitForURL(t, localURL+"/health", 15*time.Second) {
		t.Fatal("local connector never became ready")
	}

	var authURL string
	deadline := time.After(15 * time.Second)
	for {
		resp, err := http.Get(localURL + "/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			var responses []struct{ Text string }
			json.Unmarshal(body, &responses)
			for _, r := range responses {
				if strings.Contains(r.Text, "claude.com/cai/oauth") {
					authURL = r.Text
					break
				}
			}
		}
		if authURL != "" {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared in responses")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	if !strings.Contains(authURL, "code_challenge=abc123") {
		t.Fatalf("auth prompt = %q, want to contain scenario's code_challenge", authURL)
	}

	codeBody := bytes.NewBufferString(`{"text":"scenario-happy-code","from":"manager"}`)
	resp, err := http.Post(localURL+"/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST /send error = %v", err)
	}
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}

func TestAuthE2E_ScenarioRetryThenSuccess(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	fakeBinary := buildFakeClaude(t)
	tmpDir := t.TempDir()
	localPort := findFreePort(t)
	scenarioPath := filepath.Join("testdata", "fake-claude", "scenarios", "retry_then_success.json")

	manifest := fmt.Sprintf(`
name: e2e-scenario-retry
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-retry-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-scenario-retry").Run()
	})

	engine := &scenarioEngine{
		fakeBinary:   fakeBinary,
		scenarioPath: scenarioPath,
		autoResponses: []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
			{Marker: "Press Enter to retry", Response: "", Once: false},
		},
	}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "local",
				"--connector-opt", fmt.Sprintf("LOCAL_PORT=%d", localPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	localURL := fmt.Sprintf("http://127.0.0.1:%d", localPort)
	if !waitForURL(t, localURL+"/health", 15*time.Second) {
		t.Fatal("local connector never became ready")
	}

	deadline := time.After(15 * time.Second)
	found := false
	for !found {
		resp, err := http.Get(localURL + "/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if strings.Contains(string(body), "claude.com/cai/oauth") {
				found = true
			}
		}
		if found {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	codeBody := bytes.NewBufferString(`{"text":"retry-scenario-code","from":"manager"}`)
	resp, err := http.Post(localURL+"/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST /send error = %v", err)
	}
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v (should have auto-retried after 400 error)", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return — likely hung on Press Enter to retry")
	}
}

func TestAuthE2E_ScenarioProactiveLogin(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	fakeBinary := buildFakeClaude(t)
	tmpDir := t.TempDir()
	localPort := findFreePort(t)
	scenarioPath := filepath.Join("testdata", "fake-claude", "scenarios", "proactive_login.json")

	manifest := fmt.Sprintf(`
name: e2e-scenario-proactive
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-proactive-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-scenario-proactive").Run()
	})

	engine := &scenarioEngine{
		fakeBinary:   fakeBinary,
		scenarioPath: scenarioPath,
		autoResponses: []AutoResponse{
			{Marker: "don't ask on", Response: "/login", Once: true},
			{Marker: "Select login method", Response: "", Once: true},
		},
	}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "local",
				"--connector-opt", fmt.Sprintf("LOCAL_PORT=%d", localPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	localURL := fmt.Sprintf("http://127.0.0.1:%d", localPort)
	if !waitForURL(t, localURL+"/health", 15*time.Second) {
		t.Fatal("local connector never became ready")
	}

	deadline := time.After(20 * time.Second)
	found := false
	for !found {
		resp, err := http.Get(localURL + "/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if strings.Contains(string(body), "claude.com/cai/oauth") {
				found = true
			}
		}
		if found {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared — proactive /login auto-response may not have fired")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	codeBody := bytes.NewBufferString(`{"text":"proactive-code","from":"manager"}`)
	resp, err := http.Post(localURL+"/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST /send error = %v", err)
	}
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}

func findFreePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("findFreePort: %v", err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return port
}

func waitForURL(t *testing.T, url string, timeout time.Duration) bool {
	t.Helper()
	deadline := time.After(timeout)
	for {
		resp, err := http.Get(url)
		if err == nil {
			resp.Body.Close()
			return true
		}
		select {
		case <-deadline:
			return false
		case <-time.After(200 * time.Millisecond):
		}
	}
}

func extractReplyToID(responseText string) string {
	if idx := strings.Index(responseText, "reply_to_id: "); idx >= 0 {
		rest := responseText[idx+len("reply_to_id: "):]
		if end := strings.IndexByte(rest, ')'); end >= 0 {
			return rest[:end]
		}
		return strings.TrimSpace(strings.SplitN(rest, "\n", 2)[0])
	}
	return ""
}

// --- Web Connector E2E Tests ---
// These tests run the full auth pipeline through the web connector (Go+HTMX UI)
// instead of the local connector. They verify the web connector's auth card,
// code submission via HTTP form/API, and SSE status notifications.

func TestAuthE2E_WebConnector_FullLifecycle(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	webPort := findFreePort(t)

	fakeScript := filepath.Join(tmpDir, "fake-claude-web.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Select login method"
echo "1. Browser"
echo "2. API Key"
read -t 15 _choice
sleep 0.3
echo "Visit https://claude.ai/oauth/authorize?code=web-e2e to authenticate"
read -t 60 auth_code
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "What can I help you with?"
fi
sleep 3
`), 0755)

	manifest := fmt.Sprintf(`
name: e2e-web-auth
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-web-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-web-auth").Run()
	})

	engine := &fakeAuthEngine{launchCmd: fakeScript}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "web",
				"--connector-opt", fmt.Sprintf("WEB_PORT=%d", webPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	webURL := fmt.Sprintf("http://127.0.0.1:%d", webPort)
	if !waitForURL(t, webURL+"/api/health", 15*time.Second) {
		t.Fatal("web connector never became ready")
	}

	// Verify index page serves HTMX UI
	indexResp, err := http.Get(webURL + "/")
	if err != nil {
		t.Fatalf("GET / error = %v", err)
	}
	indexBody, _ := io.ReadAll(indexResp.Body)
	indexResp.Body.Close()
	if !strings.Contains(string(indexBody), "htmx") {
		t.Fatal("index page should include HTMX")
	}
	if !strings.Contains(string(indexBody), "e2e-web-auth") {
		t.Fatal("index page should show worker name")
	}

	// Wait for auth prompt with OAuth URL
	var authURL string
	deadline := time.After(15 * time.Second)
	for {
		resp, err := http.Get(webURL + "/api/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			var responses []struct{ Text string }
			json.Unmarshal(body, &responses)
			for _, r := range responses {
				if strings.Contains(r.Text, "claude.ai/oauth") {
					authURL = r.Text
					break
				}
			}
		}
		if authURL != "" {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared in web connector responses")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	if !strings.Contains(authURL, "claude.ai/oauth/authorize") {
		t.Fatalf("auth prompt = %q, want to contain OAuth URL", authURL)
	}

	// Extract reply_to_id from the auth prompt response text
	// Web connector embeds it as "reply_to_id: <id>" in the response
	promptMsgID := extractReplyToID(authURL)

	// Submit auth code via web API (JSON) with reply_to_id
	codeJSON := fmt.Sprintf(`{"text":"web-e2e-code-123","from":"manager","reply_to_id":"%s"}`, promptMsgID)
	codeBody := bytes.NewBufferString(codeJSON)
	resp, err := http.Post(webURL+"/api/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST /api/send error = %v", err)
	}
	resp.Body.Close()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}

func TestAuthE2E_WebConnector_AlreadyAuthenticated(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	webPort := findFreePort(t)

	fakeScript := filepath.Join(tmpDir, "fake-claude-web-authed.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "What can I help you with?"
sleep 5
`), 0755)

	manifest := fmt.Sprintf(`
name: e2e-web-authed
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-webauthed-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-web-authed").Run()
	})

	engine := &fakeAuthEngine{launchCmd: fakeScript}

	var stdout bytes.Buffer
	err := RunEmbeddedWorker(
		[]string{"auth",
			"--connector", "web",
			"--connector-opt", fmt.Sprintf("WEB_PORT=%d", webPort),
			"--session-prefix", sessionPrefix,
		},
		WorkerDeps{
			Assets: EmbeddedAssets{
				Manifest: []byte(manifest),
				Files:    fstest.MapFS{},
			},
			Engine: engine,
			GOOS:   "linux",
			Stdout: &stdout,
		},
	)
	if err != nil {
		t.Fatalf("RunEmbeddedWorker() error = %v", err)
	}
}

func TestAuthE2E_WebConnector_ScenarioHappyPath(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	fakeBinary := buildFakeClaude(t)
	tmpDir := t.TempDir()
	webPort := findFreePort(t)
	scenarioPath := filepath.Join("testdata", "fake-claude", "scenarios", "happy_path.json")

	manifest := fmt.Sprintf(`
name: e2e-web-scenario
version: 0.0.1
vars:
  HOME:
    source: env
    required: true
  CLAUDE_CONFIG_DIR:
    source: default
    default: "%s/claude-config"
    required: true
`, tmpDir)

	sessionPrefix := fmt.Sprintf("e2e-webscn-%d-", os.Getpid())
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", sessionPrefix+"e2e-web-scenario").Run()
	})

	engine := &scenarioEngine{
		fakeBinary:   fakeBinary,
		scenarioPath: scenarioPath,
	}

	var stdout bytes.Buffer
	errCh := make(chan error, 1)
	go func() {
		errCh <- RunEmbeddedWorker(
			[]string{"auth",
				"--connector", "web",
				"--connector-opt", fmt.Sprintf("WEB_PORT=%d", webPort),
				"--session-prefix", sessionPrefix,
			},
			WorkerDeps{
				Assets: EmbeddedAssets{
					Manifest: []byte(manifest),
					Files:    fstest.MapFS{},
				},
				Engine: engine,
				GOOS:   "linux",
				Stdout: &stdout,
			},
		)
	}()

	webURL := fmt.Sprintf("http://127.0.0.1:%d", webPort)
	if !waitForURL(t, webURL+"/api/health", 15*time.Second) {
		t.Fatal("web connector never became ready")
	}

	// Wait for auth prompt
	deadline := time.After(15 * time.Second)
	found := false
	for !found {
		resp, err := http.Get(webURL + "/api/responses")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if strings.Contains(string(body), "code_challenge=abc123") {
				found = true
			}
		}
		if found {
			break
		}
		select {
		case <-deadline:
			t.Fatal("auth prompt never appeared")
		case err := <-errCh:
			t.Fatalf("RunEmbeddedWorker returned early: %v", err)
		case <-time.After(200 * time.Millisecond):
		}
	}

	// Get reply_to_id from response
	var scenarioAuthText string
	{
		resp, _ := http.Get(webURL + "/api/responses")
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		var responses []struct{ Text string }
		json.Unmarshal(body, &responses)
		for _, r := range responses {
			if strings.Contains(r.Text, "code_challenge=abc123") {
				scenarioAuthText = r.Text
			}
		}
	}
	replyID := extractReplyToID(scenarioAuthText)

	// Submit code via HTMX form (form-encoded, with HX-Request header)
	formBody := strings.NewReader(fmt.Sprintf("text=scenario-web-code&from=user&reply_to_id=%s", replyID))
	req, _ := http.NewRequest("POST", webURL+"/api/send", formBody)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("HX-Request", "true")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST /api/send error = %v", err)
	}
	htmlBody, _ := io.ReadAll(resp.Body)
	resp.Body.Close()

	// HTMX response should render user message bubble
	if !strings.Contains(string(htmlBody), "msg user") {
		t.Fatalf("HTMX response should contain user msg class, got %q", htmlBody)
	}

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("RunEmbeddedWorker() error = %v", err)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("RunEmbeddedWorker did not return")
	}
}
