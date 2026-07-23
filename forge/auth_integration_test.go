package forge

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
)

func tmuxAvailable() bool {
	_, err := exec.LookPath("tmux")
	return err == nil
}

func TestAuthIntegration_FullFlowWithTmux(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	tmpDir := t.TempDir()
	session := fmt.Sprintf("auth-test-%d", os.Getpid())

	// Fake claude script: prints login prompt, waits for auto-response,
	// prints OAuth URL, reads auth code, prints success marker.
	fakeScript := filepath.Join(tmpDir, "fake-claude.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Select login method"
echo "1. Browser"
echo "2. API Key"
# Wait for auto-response (Enter)
read -t 10 _choice
sleep 0.3
echo "Opening browser for authentication..."
echo "Visit https://claude.ai/oauth/authorize?code=integration-test-xyz to authenticate"
echo "Waiting for authentication..."
# Wait for auth code input
read -t 30 auth_code
if [ -n "$auth_code" ]; then
  echo "Authenticated successfully with code: $auth_code"
  sleep 0.3
  echo "What can I help you with?"
else
  echo "Auth timeout"
  exit 1
fi
# Keep alive briefly so tmux session stays
sleep 2
`), 0755)

	// Create tmux session running the fake script
	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux session: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	// Wait for tmux to initialize
	time.Sleep(500 * time.Millisecond)

	// Create a test connector that captures prompts and injects auth code
	connector := &integrationConnectorSpy{
		promptMsgID: "int-42",
		codeToSend:  "my-integration-auth-code",
		inboxCh:     make(chan struct{}, 10),
	}

	spec := AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?"},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    15 * time.Second,
		AutoResponses: []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
		},
	}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "integration-test-worker",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	err := coord.Run(ctx, AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("AuthCoordinator.Run() error = %v", err)
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	// Verify the prompt was sent via connector
	prompts := connector.getPrompts()
	if len(prompts) != 1 {
		t.Fatalf("got %d prompts, want 1", len(prompts))
	}
	if !strings.Contains(prompts[0].URL, "claude.ai/oauth/authorize") {
		t.Fatalf("prompt URL = %q, want to contain claude.ai/oauth/authorize", prompts[0].URL)
	}
	if prompts[0].WorkerName != "integration-test-worker" {
		t.Fatalf("prompt WorkerName = %q, want integration-test-worker", prompts[0].WorkerName)
	}

	// Wait for the fake script to process the code and print output.
	var output string
	for i := 0; i < 20; i++ {
		time.Sleep(200 * time.Millisecond)
		output, err = runtime.CaptureHistory(200)
		if err == nil && strings.Contains(output, "What can I help you with?") {
			break
		}
	}
	if !strings.Contains(output, "Authenticated successfully") {
		t.Fatalf("tmux output missing auth confirmation, got:\n%s", output)
	}
	if !strings.Contains(output, "What can I help you with?") {
		t.Fatalf("tmux output missing success marker, got:\n%s", output)
	}
}

func TestAuthIntegration_AlreadyAuthed(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-already-%d", os.Getpid())
	tmpDir := t.TempDir()

	// Script that immediately shows success (already authenticated)
	fakeScript := filepath.Join(tmpDir, "fake-claude-authed.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "What can I help you with?"
sleep 5
`), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(500 * time.Millisecond)

	connector := &integrationConnectorSpy{inboxCh: make(chan struct{}, 10)}

	spec := AuthSpec{
		Required:       true,
		URLPatterns:    []*regexp.Regexp{regexp.MustCompile(`https://claude\.ai/[^\s]+`)},
		SuccessMarkers: []string{"What can I help you with?"},
		URLTimeout:     5 * time.Second,
		CodeTimeout:    5 * time.Second,
	}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "already-authed",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	err := coord.Run(ctx, AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	// Should NOT have sent any prompts
	prompts := connector.getPrompts()
	if len(prompts) != 0 {
		t.Fatalf("got %d prompts for already-authed, want 0", len(prompts))
	}
}

func TestAuthIntegration_AutoResponseThenURL(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-auto-%d", os.Getpid())
	tmpDir := t.TempDir()

	// Script: two auto-response prompts, then URL
	fakeScript := filepath.Join(tmpDir, "fake-claude-multi.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Select login method"
read -t 10 _
sleep 0.3
echo "Choose the text style"
read -t 10 _
sleep 0.3
echo "Please visit https://console.anthropic.com/oauth/callback?token=multi123"
read -t 30 auth_code
if [ -n "$auth_code" ]; then
  echo "What can I help you with?"
fi
sleep 2
`), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(500 * time.Millisecond)

	connector := &integrationConnectorSpy{
		promptMsgID: "auto-77",
		codeToSend:  "multi-code-xyz",
		inboxCh:     make(chan struct{}, 10),
	}

	spec := AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://(?:claude\.ai|console\.anthropic\.com)/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?"},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    15 * time.Second,
		AutoResponses: []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
			{Marker: "Choose the text style", Response: "", Once: false},
		},
	}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "multi-auto-test",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	err := coord.Run(ctx, AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	prompts := connector.getPrompts()
	if len(prompts) != 1 {
		t.Fatalf("got %d prompts, want 1", len(prompts))
	}
	if !strings.Contains(prompts[0].URL, "console.anthropic.com") {
		t.Fatalf("URL = %q, want console.anthropic.com", prompts[0].URL)
	}
}

func TestAuthIntegration_WithLocalConnector(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-local-%d", os.Getpid())
	tmpDir := t.TempDir()

	fakeScript := filepath.Join(tmpDir, "fake-claude-local.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Visit https://claude.ai/oauth/local-test?code=abc to authenticate"
read -t 30 auth_code
if [ -n "$auth_code" ]; then
  echo "Authenticated: $auth_code"
  echo "What can I help you with?"
fi
sleep 2
`), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(500 * time.Millisecond)

	// Use the real LocalConnector
	localConn := &LocalConnector{}
	if err := localConn.Init(context.Background(), ConnectorConfig{
		WorkerName: "local-auth-test",
		Config:     map[string]string{},
	}); err != nil {
		t.Fatalf("LocalConnector.Init() error = %v", err)
	}
	t.Cleanup(func() { localConn.Close() })

	spec := AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://claude\.ai/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?"},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    15 * time.Second,
	}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "local-auth-test",
	}

	// Run auth coordinator in background
	errCh := make(chan error, 1)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	go func() {
		errCh <- coord.Run(ctx, AdaptConnectorForAuth(localConn))
	}()

	// Wait for prompt to appear in responses
	var responses []Response
	deadline := time.After(15 * time.Second)
	for {
		responses = localConn.Responses()
		if len(responses) > 0 {
			break
		}
		select {
		case <-deadline:
			t.Fatal("timed out waiting for auth prompt in LocalConnector responses")
		case <-time.After(100 * time.Millisecond):
		}
	}

	if !strings.Contains(responses[0].Text, "claude.ai/oauth/local-test") {
		t.Fatalf("response text = %q, want to contain the auth URL", responses[0].Text)
	}

	// Inject auth code via the local connector's inbox
	// LocalConnector uses fallback matching (no message ID)
	localConn.InjectMessage(InboundMessage{Text: "local-auth-code-999"})

	// Wait for completion
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("Run() did not complete")
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	// Verify code was sent to tmux
	output, err := runtime.CaptureHistory(100)
	if err != nil {
		t.Fatalf("CaptureHistory error = %v", err)
	}
	if !strings.Contains(output, "What can I help you with?") {
		t.Fatalf("tmux output missing success, got:\n%s", output)
	}
}

// integrationConnectorSpy is a PollReceiver + AuthPrompter for integration tests.
// When a prompt is sent, it automatically injects the auth code after a short delay.
type integrationConnectorSpy struct {
	mu          sync.Mutex
	prompts     []AuthPromptRequest
	promptMsgID string
	codeToSend  string
	inbox       []InboundMessage
	inboxCh     chan struct{}
	sendTexts   []string
}

func (c *integrationConnectorSpy) Type() string     { return "integration-spy" }
func (c *integrationConnectorSpy) Capabilities() Caps { return CapText }
func (c *integrationConnectorSpy) Requirements() Reqs { return 0 }
func (c *integrationConnectorSpy) Init(_ context.Context, _ ConnectorConfig) error {
	c.inboxCh = make(chan struct{}, 10)
	return nil
}
func (c *integrationConnectorSpy) Close() error { return nil }
func (c *integrationConnectorSpy) Send(_ context.Context, resp Response) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sendTexts = append(c.sendTexts, resp.Text)
	return nil
}

func (c *integrationConnectorSpy) SendAuthPrompt(_ context.Context, req AuthPromptRequest) (AuthPromptResult, error) {
	c.mu.Lock()
	c.prompts = append(c.prompts, req)
	code := c.codeToSend
	msgID := c.promptMsgID
	c.mu.Unlock()

	// Inject auth code after a short delay (simulates user responding)
	if code != "" {
		go func() {
			time.Sleep(500 * time.Millisecond)
			c.mu.Lock()
			c.inbox = append(c.inbox, InboundMessage{
				Text:      code,
				ReplyToID: msgID,
			})
			c.mu.Unlock()
			select {
			case c.inboxCh <- struct{}{}:
			default:
			}
		}()
	}

	return AuthPromptResult{MessageID: msgID}, nil
}

func (c *integrationConnectorSpy) PollInterval() time.Duration { return 50 * time.Millisecond }

func (c *integrationConnectorSpy) Poll(ctx context.Context) ([]InboundMessage, error) {
	if c.inboxCh == nil {
		return nil, nil
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.inboxCh:
	case <-time.After(200 * time.Millisecond):
		return nil, nil
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	msgs := make([]InboundMessage, len(c.inbox))
	copy(msgs, c.inbox)
	c.inbox = c.inbox[:0]
	return msgs, nil
}

func (c *integrationConnectorSpy) getPrompts() []AuthPromptRequest {
	c.mu.Lock()
	defer c.mu.Unlock()
	cp := make([]AuthPromptRequest, len(c.prompts))
	copy(cp, c.prompts)
	return cp
}

func TestAuthIntegration_RetryAutoResponseInWaitForCompletion(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-retry-%d", os.Getpid())
	tmpDir := t.TempDir()

	fakeScript := filepath.Join(tmpDir, "fake-claude-retry.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Select login method"
read -t 10 _
sleep 0.3
echo "Visit https://claude.com/cai/oauth/authorize?code=retry-test to authenticate"
echo "Paste code here >"
read -t 30 auth_code
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "OAuth error: code expired (HTTP 400)"
  echo "Press Enter to retry"
  read -t 15 _
  sleep 0.3
  echo "Authentication successful!"
  echo "What can I help you with?"
fi
sleep 2
`), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(500 * time.Millisecond)

	connector := &integrationConnectorSpy{
		promptMsgID: "retry-42",
		codeToSend:  "expired-code-123",
		inboxCh:     make(chan struct{}, 10),
	}

	spec := AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?"},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    30 * time.Second,
		AutoResponses: []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
			{Marker: "Press Enter to retry", Response: "", Once: false},
		},
	}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "retry-test-worker",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	err := coord.Run(ctx, AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("AuthCoordinator.Run() error = %v", err)
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	output, _ := runtime.CaptureHistory(200)
	if !strings.Contains(output, "Press Enter to retry") {
		t.Fatalf("tmux output missing retry prompt, got:\n%s", output)
	}
	if !strings.Contains(output, "What can I help you with?") {
		t.Fatalf("tmux output missing success marker after retry, got:\n%s", output)
	}
}

func TestAuthIntegration_ProactiveLoginViaDontAskOnMarker(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-proactive-%d", os.Getpid())
	tmpDir := t.TempDir()

	fakeScript := filepath.Join(tmpDir, "fake-claude-proactive.sh")
	os.WriteFile(fakeScript, []byte(`#!/bin/bash
echo "Claude Code v2.1.201"
echo "Opus 4.6 with high effort"
echo ""
echo "don't ask on (shift+tab to cycle)"
read -t 15 cmd
if [ "$cmd" = "/login" ]; then
  echo "Select login method"
  read -t 10 _
  sleep 0.3
  echo "Visit https://claude.com/cai/oauth/authorize?code=proactive-test to authenticate"
  echo "Paste code here >"
  read -t 30 auth_code
  if [ -n "$auth_code" ]; then
    sleep 0.3
    echo "What can I help you with?"
  fi
fi
sleep 2
`), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(500 * time.Millisecond)

	connector := &integrationConnectorSpy{
		promptMsgID: "proactive-42",
		codeToSend:  "proactive-code-456",
		inboxCh:     make(chan struct{}, 10),
	}

	spec := AuthSpec{
		Required: true,
		URLPatterns: []*regexp.Regexp{
			regexp.MustCompile(`https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/[^\s\x1b"'>)\]]+`),
		},
		SuccessMarkers: []string{"What can I help you with?", "claude>"},
		URLTimeout:     15 * time.Second,
		CodeTimeout:    30 * time.Second,
		AutoResponses: []AutoResponse{
			{Marker: "Select login method", Response: "", Once: true},
			{Marker: "don't ask on", Response: "/login", Once: true},
		},
	}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "proactive-test-worker",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	err := coord.Run(ctx, AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("AuthCoordinator.Run() error = %v", err)
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	output, _ := runtime.CaptureHistory(200)
	if !strings.Contains(output, "Paste code here") {
		t.Fatalf("tmux output missing OAuth prompt — /login auto-response didn't fire, got:\n%s", output)
	}
}

func TestAuthIntegration_BracketedPasteRejection(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-paste-%d", os.Getpid())
	tmpDir := t.TempDir()

	logFile := filepath.Join(tmpDir, "input.log")

	fakeScript := filepath.Join(tmpDir, "fake-claude-paste.sh")
	os.WriteFile(fakeScript, []byte(fmt.Sprintf(`#!/bin/bash
echo "Select login method"
read -t 10 _
sleep 0.3
echo "Visit https://claude.com/cai/oauth/authorize?code=paste-test to authenticate"
echo "Paste code here >"
read -t 30 auth_code
echo "INPUT: $auth_code" >> %s
if [ -n "$auth_code" ]; then
  sleep 0.3
  echo "What can I help you with?"
fi
sleep 2
`, logFile)), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(500 * time.Millisecond)

	waitForTmuxOutput := func(marker string, timeout time.Duration) bool {
		deadline := time.After(timeout)
		for {
			output, err := runtime.CaptureHistory(200)
			if err == nil && strings.Contains(output, marker) {
				return true
			}
			select {
			case <-deadline:
				return false
			case <-time.After(200 * time.Millisecond):
			}
		}
	}

	if !waitForTmuxOutput("Select login method", 5*time.Second) {
		t.Fatal("login prompt never appeared")
	}
	runtime.Send("")

	if !waitForTmuxOutput("Paste code here", 10*time.Second) {
		t.Fatal("paste code prompt never appeared")
	}

	// Test 1: Send code via literal send-keys (should work)
	exec.Command("tmux", "send-keys", "-t", session, "-l", "literal-test-code-789").Run()
	time.Sleep(200 * time.Millisecond)
	exec.Command("tmux", "send-keys", "-t", session, "Enter").Run()

	if !waitForTmuxOutput("What can I help you with?", 10*time.Second) {
		t.Fatal("success marker never appeared — literal send-keys may have failed")
	}

	logData, err := os.ReadFile(logFile)
	if err != nil {
		t.Fatalf("read input log: %v", err)
	}
	logStr := string(logData)
	if !strings.Contains(logStr, "literal-test-code-789") {
		t.Fatalf("input log = %q, want to contain literal-test-code-789", logStr)
	}
}

func TestAuthIntegration_BracketedPasteVsLiteralSendKeys(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}
	if os.Getenv("FAST") == "1" {
		t.Skip("skipped in FAST mode")
	}

	session := fmt.Sprintf("auth-paste-vs-%d", os.Getpid())
	tmpDir := t.TempDir()

	logFile := filepath.Join(tmpDir, "input.log")

	fakeScript := filepath.Join(tmpDir, "fake-paste-compare.sh")
	os.WriteFile(fakeScript, []byte(fmt.Sprintf(`#!/bin/bash
echo "Ready for input"
while IFS= read -r -t 30 line; do
  echo "GOT: $line" >> %s
done
sleep 1
`, logFile)), 0755)

	runner := ShellRunner{}
	runtime := &TmuxRuntime{
		Runner:  runner,
		Session: session,
	}
	runtime.SetLaunchCommand(fakeScript)

	if err := runtime.Start(); err != nil {
		t.Fatalf("failed to start tmux: %v", err)
	}
	t.Cleanup(func() {
		exec.Command("tmux", "kill-session", "-t", session).Run()
	})

	time.Sleep(1 * time.Second)

	// Send via literal send-keys (no bracketed paste markers)
	exec.Command("tmux", "send-keys", "-t", session, "-l", "LITERAL_CODE").Run()
	time.Sleep(100 * time.Millisecond)
	exec.Command("tmux", "send-keys", "-t", session, "Enter").Run()
	time.Sleep(500 * time.Millisecond)

	// Send via paste-buffer -p (with bracketed paste markers)
	exec.Command("tmux", "set-buffer", "PASTED_CODE").Run()
	exec.Command("tmux", "paste-buffer", "-t", session, "-p").Run()
	time.Sleep(100 * time.Millisecond)
	exec.Command("tmux", "send-keys", "-t", session, "Enter").Run()
	time.Sleep(500 * time.Millisecond)

	logData, err := os.ReadFile(logFile)
	if err != nil {
		t.Fatalf("read log: %v", err)
	}
	logStr := string(logData)

	if !strings.Contains(logStr, "GOT: LITERAL_CODE") {
		t.Errorf("literal send-keys not received, log:\n%s", logStr)
	}

	// Note: bash `read` strips bracketed paste escape sequences, so both methods
	// typically deliver to bash. The issue is specific to TUI apps that handle raw
	// terminal input (like Claude Code's OAuth dialog). This test documents that
	// both methods work for bash, establishing the baseline. The real regression
	// is tested via the Go fake-claude binary which can detect raw escape sequences.
	if !strings.Contains(logStr, "GOT: PASTED_CODE") {
		t.Logf("paste-buffer -p result different from send-keys -l (expected in some TUIs)")
		t.Logf("log contents:\n%s", logStr)
	}
}
