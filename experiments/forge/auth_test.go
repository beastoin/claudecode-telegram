package forge

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/beastoin/claudecode-telegram/forge/engine"
)

var testURLPatterns = []*regexp.Regexp{
	regexp.MustCompile(`https://(?:claude\.ai|claude\.com|console\.anthropic\.com|platform\.claude\.com)/[^\s\x1b"'>)\]]+`),
}

func testSpec() AuthSpec {
	return AuthSpec{
		Required:       true,
		URLPatterns:    testURLPatterns,
		SuccessMarkers: []string{"What can I help you with?", "claude>", "$  "},
		URLTimeout:     2 * time.Second,
		CodeTimeout:    5 * time.Second,
	}
}

func TestDetectOAuthURL(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name  string
		input string
		want  string
	}{
		{
			name:  "claude.ai URL",
			input: "Please visit https://claude.ai/oauth/authorize?code=abc123 to authenticate",
			want:  "https://claude.ai/oauth/authorize?code=abc123",
		},
		{
			name:  "console.anthropic.com URL",
			input: "Open https://console.anthropic.com/oauth/callback?token=xyz in your browser",
			want:  "https://console.anthropic.com/oauth/callback?token=xyz",
		},
		{
			name:  "URL with trailing punctuation",
			input: "Visit https://claude.ai/auth/login?redirect=true.",
			want:  "https://claude.ai/auth/login?redirect=true",
		},
		{
			name:  "claude.com OAuth URL",
			input: "Visit https://claude.com/cai/oauth/authorize?code=true&client_id=abc to auth",
			want:  "https://claude.com/cai/oauth/authorize?code=true&client_id=abc",
		},
		{
			name:  "platform.claude.com URL",
			input: "Redirect to https://platform.claude.com/oauth/code/callback?code=xyz",
			want:  "https://platform.claude.com/oauth/code/callback?code=xyz",
		},
		{
			name:  "no URL",
			input: "What can I help you with today?",
			want:  "",
		},
		{
			name:  "non-anthropic URL",
			input: "Visit https://google.com/auth for more info",
			want:  "",
		},
		{
			name:  "URL wrapped across lines",
			input: "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88\ned-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.co\nm%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key",
			want:  "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key",
		},
		{
			name:  "URL with ANSI escape sequences",
			input: "Visit \x1b[34mhttps://claude.ai/oauth/login\x1b[0m now",
			want:  "https://claude.ai/oauth/login",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := DetectOAuthURL(tt.input, testURLPatterns)
			if got != tt.want {
				t.Fatalf("DetectOAuthURL(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

type authRuntimeSpy struct {
	mu      sync.Mutex
	outputs []string
	index   int
	sendLog []string
}

func (r *authRuntimeSpy) LastOutput() (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.index >= len(r.outputs) {
		return r.outputs[len(r.outputs)-1], nil
	}
	out := r.outputs[r.index]
	r.index++
	return out, nil
}

func (r *authRuntimeSpy) Send(message string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sendLog = append(r.sendLog, message)
	r.outputs = append(r.outputs, "What can I help you with?")
	return nil
}

func (r *authRuntimeSpy) getSendLog() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	cp := make([]string, len(r.sendLog))
	copy(cp, r.sendLog)
	return cp
}

// authConnectorSpy implements Connector + AuthPrompter + PollReceiver for testing.
type authConnectorSpy struct {
	mu        sync.Mutex
	prompts   []AuthPromptRequest
	resultID  string
	sendTexts []string
	inbox     []InboundMessage
	inboxCh   chan struct{}
}

func (c *authConnectorSpy) Type() string                                { return "auth-spy" }
func (c *authConnectorSpy) Capabilities() Caps                          { return CapText }
func (c *authConnectorSpy) Requirements() Reqs                          { return 0 }
func (c *authConnectorSpy) Init(context.Context, ConnectorConfig) error {
	c.inboxCh = make(chan struct{}, 10)
	return nil
}
func (c *authConnectorSpy) Close() error { return nil }

func (c *authConnectorSpy) Send(_ context.Context, resp Response) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sendTexts = append(c.sendTexts, resp.Text)
	return nil
}

func (c *authConnectorSpy) SendAuthPrompt(_ context.Context, req AuthPromptRequest) (AuthPromptResult, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.prompts = append(c.prompts, req)
	return AuthPromptResult{MessageID: c.resultID}, nil
}

func (c *authConnectorSpy) PollInterval() time.Duration { return 50 * time.Millisecond }

func (c *authConnectorSpy) Poll(ctx context.Context) ([]InboundMessage, error) {
	if c.inboxCh == nil {
		return nil, nil
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.inboxCh:
	case <-time.After(100 * time.Millisecond):
		return nil, nil
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	msgs := make([]InboundMessage, len(c.inbox))
	copy(msgs, c.inbox)
	c.inbox = c.inbox[:0]
	return msgs, nil
}

func (c *authConnectorSpy) injectMessage(msg InboundMessage) {
	c.mu.Lock()
	c.inbox = append(c.inbox, msg)
	c.mu.Unlock()
	if c.inboxCh != nil {
		select {
		case c.inboxCh <- struct{}{}:
		default:
		}
	}
}

func (c *authConnectorSpy) getPrompts() []AuthPromptRequest {
	c.mu.Lock()
	defer c.mu.Unlock()
	cp := make([]AuthPromptRequest, len(c.prompts))
	copy(cp, c.prompts)
	return cp
}

// plainPollConnectorSpy implements Connector + PollReceiver (no AuthPrompter).
type plainPollConnectorSpy struct {
	mu        sync.Mutex
	sendTexts []string
	inbox     []InboundMessage
	inboxCh   chan struct{}
}

func (c *plainPollConnectorSpy) Type() string                                { return "plain-spy" }
func (c *plainPollConnectorSpy) Capabilities() Caps                          { return CapText }
func (c *plainPollConnectorSpy) Requirements() Reqs                          { return 0 }
func (c *plainPollConnectorSpy) Init(context.Context, ConnectorConfig) error {
	c.inboxCh = make(chan struct{}, 10)
	return nil
}
func (c *plainPollConnectorSpy) Close() error { return nil }

func (c *plainPollConnectorSpy) Send(_ context.Context, resp Response) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sendTexts = append(c.sendTexts, resp.Text)
	return nil
}

func (c *plainPollConnectorSpy) PollInterval() time.Duration { return 50 * time.Millisecond }

func (c *plainPollConnectorSpy) Poll(ctx context.Context) ([]InboundMessage, error) {
	if c.inboxCh == nil {
		return nil, nil
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.inboxCh:
	case <-time.After(100 * time.Millisecond):
		return nil, nil
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	msgs := make([]InboundMessage, len(c.inbox))
	copy(msgs, c.inbox)
	c.inbox = c.inbox[:0]
	return msgs, nil
}

func (c *plainPollConnectorSpy) injectMessage(msg InboundMessage) {
	c.mu.Lock()
	c.inbox = append(c.inbox, msg)
	c.mu.Unlock()
	if c.inboxCh != nil {
		select {
		case c.inboxCh <- struct{}{}:
		default:
		}
	}
}

func (c *plainPollConnectorSpy) getSendTexts() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	cp := make([]string, len(c.sendTexts))
	copy(cp, c.sendTexts)
	return cp
}

func TestAuthCoordinator_AlreadyAuthenticated(t *testing.T) {
	t.Parallel()

	runtime := &authRuntimeSpy{
		outputs: []string{"What can I help you with?"},
	}

	connector := &authConnectorSpy{}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       testSpec(),
		WorkerName: "mon",
	}

	err := coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}
}

func TestAuthCoordinator_FullFlow(t *testing.T) {
	t.Parallel()

	oauthURL := "https://claude.ai/oauth/authorize?code=test123"

	runtime := &authRuntimeSpy{
		outputs: []string{
			"Loading...",
			"Please visit " + oauthURL + " to authenticate",
		},
	}
	connector := &authConnectorSpy{resultID: "42"}
	connector.Init(context.Background(), ConnectorConfig{})

	spec := testSpec()
	spec.URLTimeout = 5 * time.Second
	spec.CodeTimeout = 10 * time.Second

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "mon-the-fox",
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	}()

	// Wait for auth to reach waiting-for-code state
	deadline := time.After(5 * time.Second)
	for {
		if coord.State() == AuthWaitingForCode {
			break
		}
		select {
		case <-deadline:
			t.Fatal("timed out waiting for AuthWaitingForCode state")
		case <-time.After(10 * time.Millisecond):
		}
	}

	// Simulate manager replying with auth code via poll
	connector.injectMessage(InboundMessage{
		Text:      "auth-code-xyz",
		From:      "manager",
		ReplyToID: "42",
	})

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run() did not complete")
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	prompts := connector.getPrompts()
	if len(prompts) != 1 {
		t.Fatalf("connector got %d prompts, want 1", len(prompts))
	}
	if prompts[0].URL != oauthURL {
		t.Fatalf("prompt URL = %q, want %q", prompts[0].URL, oauthURL)
	}
	if prompts[0].WorkerName != "mon-the-fox" {
		t.Fatalf("prompt WorkerName = %q, want mon-the-fox", prompts[0].WorkerName)
	}

	sent := runtime.getSendLog()
	if len(sent) != 1 || sent[0] != "auth-code-xyz" {
		t.Fatalf("runtime.Send() log = %v, want [auth-code-xyz]", sent)
	}
}

func TestAuthCoordinator_FallbackToPlainSend(t *testing.T) {
	t.Parallel()

	oauthURL := "https://claude.ai/oauth/authorize?code=test123"

	runtime := &authRuntimeSpy{
		outputs: []string{"Visit " + oauthURL + " to auth"},
	}
	connector := &plainPollConnectorSpy{}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       testSpec(),
		WorkerName: "mon",
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	}()

	deadline := time.After(5 * time.Second)
	for {
		if coord.State() == AuthWaitingForCode {
			break
		}
		select {
		case <-deadline:
			t.Fatal("timed out waiting for AuthWaitingForCode state")
		case <-time.After(10 * time.Millisecond):
		}
	}

	// Plain connector has no message ID — fallback code matching
	connector.injectMessage(InboundMessage{Text: "my-auth-code"})

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run() did not complete")
	}

	texts := connector.getSendTexts()
	if len(texts) != 1 {
		t.Fatalf("connector got %d sends, want 1", len(texts))
	}
	if !strings.Contains(texts[0], oauthURL) {
		t.Fatalf("send text = %q, should contain URL", texts[0])
	}
}

func TestMatchAuthCode_ReplyToMatching(t *testing.T) {
	t.Parallel()

	// Non-matching reply-to
	code := matchAuthCode(InboundMessage{Text: "hello", ReplyToID: "50"}, "99")
	if code != "" {
		t.Fatalf("matched with wrong ReplyToID: %q", code)
	}

	// Matching reply-to
	code = matchAuthCode(InboundMessage{Text: "the-code", ReplyToID: "99"}, "99")
	if code != "the-code" {
		t.Fatalf("code = %q, want the-code", code)
	}

	// No prompt ID — falls back to code-like check
	code = matchAuthCode(InboundMessage{Text: "ABCD-1234"}, "")
	if code != "ABCD-1234" {
		t.Fatalf("code = %q, want ABCD-1234", code)
	}

	// Has prompt ID, no reply — rejected
	code = matchAuthCode(InboundMessage{Text: "ABCD-1234"}, "99")
	if code != "" {
		t.Fatalf("should not match without ReplyToID when promptMsgID set, got %q", code)
	}
}

func TestMatchAuthCode_StripsReplyContext(t *testing.T) {
	t.Parallel()

	code := matchAuthCode(InboundMessage{
		Text:      "my-code-123\n\nContext (previous message):\nAuth required for test...",
		ReplyToID: "42",
	}, "42")
	if code != "my-code-123" {
		t.Fatalf("code = %q, want my-code-123", code)
	}
}

func TestAuthCoordinator_URLTimeout(t *testing.T) {
	t.Parallel()

	runtime := &authRuntimeSpy{
		outputs: []string{"Loading...", "Still loading..."},
	}

	spec := testSpec()
	spec.URLTimeout = 1 * time.Second

	connector := &authConnectorSpy{}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "mon",
	}

	err := coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("Run() error = %v, want nil (timeout = no auth needed)", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete (no URL found = already authed)", coord.State())
	}
}

func TestAuthCoordinator_ContextCancelled(t *testing.T) {
	t.Parallel()

	runtime := &authRuntimeSpy{
		outputs: []string{"Loading..."},
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	spec := testSpec()
	spec.URLTimeout = 30 * time.Second

	connector := &authConnectorSpy{}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "mon",
	}

	err := coord.Run(ctx, AdaptConnectorForAuth(connector))
	if err == nil {
		t.Fatal("Run() error = nil, want context error")
	}
	if coord.State() != AuthFailed {
		t.Fatalf("state = %v, want AuthFailed", coord.State())
	}
}

func TestAuthCoordinator_PrompterError(t *testing.T) {
	t.Parallel()

	runtime := &authRuntimeSpy{
		outputs: []string{"Visit https://claude.ai/oauth/test to auth"},
	}

	failConn := &failingAuthPollConnector{err: fmt.Errorf("telegram down")}
	failConn.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       testSpec(),
		WorkerName: "mon",
	}

	err := coord.Run(context.Background(), AdaptConnectorForAuth(failConn))
	if err == nil {
		t.Fatal("Run() error = nil, want prompter error")
	}
	if !strings.Contains(err.Error(), "telegram down") {
		t.Fatalf("error = %v, want to contain 'telegram down'", err)
	}
	if coord.State() != AuthFailed {
		t.Fatalf("state = %v, want AuthFailed", coord.State())
	}
}

type failingAuthPollConnector struct {
	err     error
	inboxCh chan struct{}
}

func (c *failingAuthPollConnector) Type() string                                { return "fail" }
func (c *failingAuthPollConnector) Capabilities() Caps                          { return CapText }
func (c *failingAuthPollConnector) Requirements() Reqs                          { return 0 }
func (c *failingAuthPollConnector) Init(context.Context, ConnectorConfig) error {
	c.inboxCh = make(chan struct{}, 10)
	return nil
}
func (c *failingAuthPollConnector) Close() error                        { return nil }
func (c *failingAuthPollConnector) Send(context.Context, Response) error { return c.err }
func (c *failingAuthPollConnector) SendAuthPrompt(_ context.Context, _ AuthPromptRequest) (AuthPromptResult, error) {
	return AuthPromptResult{}, c.err
}
func (c *failingAuthPollConnector) PollInterval() time.Duration { return 50 * time.Millisecond }
func (c *failingAuthPollConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(100 * time.Millisecond):
		return nil, nil
	}
}

func TestAuthCoordinator_StateTransitions(t *testing.T) {
	t.Parallel()

	coord := &AuthCoordinator{
		WorkerName: "test",
	}
	if coord.State() != AuthIdle {
		t.Fatalf("initial state = %v, want AuthIdle", coord.State())
	}
}

func TestAuthState_String(t *testing.T) {
	t.Parallel()

	tests := []struct {
		state AuthState
		want  string
	}{
		{AuthIdle, "idle"},
		{AuthWaitingForURL, "waiting-for-url"},
		{AuthURLSent, "url-sent"},
		{AuthWaitingForCode, "waiting-for-code"},
		{AuthCodeSubmitted, "code-submitted"},
		{AuthComplete, "complete"},
		{AuthFailed, "failed"},
		{AuthState(99), "unknown"},
	}

	for _, tt := range tests {
		t.Run(tt.want, func(t *testing.T) {
			t.Parallel()
			if got := tt.state.String(); got != tt.want {
				t.Fatalf("AuthState(%d).String() = %q, want %q", tt.state, got, tt.want)
			}
		})
	}
}

func TestMatchesAnyMarker(t *testing.T) {
	t.Parallel()

	markers := []string{"What can I help you with?", "claude>", "$  "}

	tests := []struct {
		input string
		want  bool
	}{
		{"What can I help you with?", true},
		{"claude> ", true},
		{"Loading OAuth...", false},
		{"Visit https://claude.ai/auth to log in", false},
		{"$  ", true},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			t.Parallel()
			if got := matchesAnyMarker(tt.input, markers); got != tt.want {
				t.Fatalf("matchesAnyMarker(%q) = %v, want %v", tt.input, got, tt.want)
			}
		})
	}
}

type historyCaptureSpy struct {
	authRuntimeSpy
	historyOutput string
}

func (h *historyCaptureSpy) CaptureHistory(_ int) (string, error) {
	h.mu.Lock()
	sent := len(h.sendLog) > 0
	h.mu.Unlock()
	if sent {
		return "What can I help you with?", nil
	}
	return h.historyOutput, nil
}

func TestAuthCoordinator_UsesHistoryCapture(t *testing.T) {
	t.Parallel()

	runtime := &historyCaptureSpy{
		historyOutput: "Visit https://claude.ai/oauth/history-test to auth",
	}
	connector := &authConnectorSpy{resultID: "10"}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       testSpec(),
		WorkerName: "mon",
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	}()

	deadline := time.After(5 * time.Second)
	for {
		if coord.State() == AuthWaitingForCode {
			break
		}
		select {
		case <-deadline:
			t.Fatal("timed out waiting for AuthWaitingForCode state")
		case <-time.After(10 * time.Millisecond):
		}
	}

	connector.injectMessage(InboundMessage{Text: "code123", ReplyToID: "10"})

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run() did not complete")
	}

	prompts := connector.getPrompts()
	if len(prompts) != 1 || !strings.Contains(prompts[0].URL, "history-test") {
		t.Fatalf("expected URL from CaptureHistory, got %v", prompts)
	}
}

func TestExtractAuthCode(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name  string
		input string
		want  string
	}{
		{"simple code", "ABCD-1234", "ABCD-1234"},
		{"with reply context", "ABCD-1234\n\nContext (previous message):\nAuth required...", "ABCD-1234"},
		{"with newline", "code-123\nextra line", "code-123"},
		{"empty", "", ""},
		{"whitespace", "  code-123  ", "code-123"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := extractAuthCode(tt.input)
			if got != tt.want {
				t.Fatalf("extractAuthCode(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestLooksLikeAuthCode(t *testing.T) {
	t.Parallel()

	tests := []struct {
		input string
		want  bool
	}{
		{"ABCD-1234", true},
		{"short", true},
		{"abc", false},
		{"a b c", false},
		{"", false},
		{"a-very-long-code-that-is-still-valid-1234", true},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			t.Parallel()
			got := looksLikeAuthCode(tt.input)
			if got != tt.want {
				t.Fatalf("looksLikeAuthCode(%q) = %v, want %v", tt.input, got, tt.want)
			}
		})
	}
}

func TestAuthCoordinator_AutoResponses(t *testing.T) {
	t.Parallel()

	runtime := &authRuntimeSpy{
		outputs: []string{
			"Select login method:\n1. Browser\n2. API Key",
			"What can I help you with?",
		},
	}

	spec := testSpec()
	spec.AutoResponses = []AutoResponse{
		{Marker: "Select login method", Response: "", Once: true},
	}

	connector := &authConnectorSpy{}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "mon",
	}

	err := coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	sent := runtime.getSendLog()
	if len(sent) != 1 || sent[0] != "" {
		t.Fatalf("runtime.Send() = %v, want one empty string (Enter)", sent)
	}
}

func TestAuthCoordinator_StreamReceiver(t *testing.T) {
	t.Parallel()

	oauthURL := "https://claude.ai/oauth/stream-test"
	runtime := &authRuntimeSpy{
		outputs: []string{"Visit " + oauthURL + " to auth"},
	}

	connector := &authStreamConnectorSpy{
		resultID: "55",
		ch:       make(chan InboundMessage, 5),
	}

	spec := testSpec()
	spec.URLTimeout = 5 * time.Second
	spec.CodeTimeout = 10 * time.Second

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "stream-test",
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	}()

	deadline := time.After(5 * time.Second)
	for {
		if coord.State() == AuthWaitingForCode {
			break
		}
		select {
		case <-deadline:
			t.Fatal("timed out waiting for AuthWaitingForCode")
		case <-time.After(10 * time.Millisecond):
		}
	}

	connector.ch <- InboundMessage{Text: "stream-code", ReplyToID: "55"}

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run() did not complete")
	}

	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}
}

type authStreamConnectorSpy struct {
	resultID string
	ch       chan InboundMessage
}

func (c *authStreamConnectorSpy) Type() string                                { return "stream-spy" }
func (c *authStreamConnectorSpy) Capabilities() Caps                          { return CapText }
func (c *authStreamConnectorSpy) Requirements() Reqs                          { return 0 }
func (c *authStreamConnectorSpy) Init(context.Context, ConnectorConfig) error { return nil }
func (c *authStreamConnectorSpy) Close() error                                { return nil }
func (c *authStreamConnectorSpy) Send(context.Context, Response) error        { return nil }
func (c *authStreamConnectorSpy) Messages() <-chan InboundMessage             { return c.ch }
func (c *authStreamConnectorSpy) SendAuthPrompt(_ context.Context, _ AuthPromptRequest) (AuthPromptResult, error) {
	return AuthPromptResult{MessageID: c.resultID}, nil
}

// authFullRuntimeSpy extends authRuntimeSpy with Start/Health to satisfy Runtime.
type authFullRuntimeSpy struct {
	authRuntimeSpy
}

func (r *authFullRuntimeSpy) Start() error { return nil }
func (r *authFullRuntimeSpy) Health() error { return nil }

// bridgeConnectorSpy implements only AuthConnector (no PollReceiver/StreamReceiver).
// Simulates the bridge connector path where auth code arrives via tmux send-keys.
type bridgeConnectorSpy struct {
	mu        sync.Mutex
	sendTexts []string
}

func (c *bridgeConnectorSpy) Type() string { return "bridge-spy" }
func (c *bridgeConnectorSpy) Send(_ context.Context, resp engine.AuthResponse) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sendTexts = append(c.sendTexts, resp.Text)
	return nil
}
func (c *bridgeConnectorSpy) getSendTexts() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	cp := make([]string, len(c.sendTexts))
	copy(cp, c.sendTexts)
	return cp
}

// authRuntimeSpyWithDelayedSuccess simulates OAuth URL appearing, then
// success marker appearing after a delay (simulates code arriving via tmux send-keys).
type authRuntimeSpyWithDelayedSuccess struct {
	mu           sync.Mutex
	outputs      []string
	index        int
	sendLog      []string
	successAfter time.Time
}

func (r *authRuntimeSpyWithDelayedSuccess) LastOutput() (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.successAfter.IsZero() && time.Now().After(r.successAfter) {
		return "Welcome back Thinh!", nil
	}
	if r.index >= len(r.outputs) {
		return r.outputs[len(r.outputs)-1], nil
	}
	out := r.outputs[r.index]
	r.index++
	return out, nil
}

func (r *authRuntimeSpyWithDelayedSuccess) Send(message string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sendLog = append(r.sendLog, message)
	return nil
}

func (r *authRuntimeSpyWithDelayedSuccess) scheduleSuccess(delay time.Duration) {
	r.mu.Lock()
	r.successAfter = time.Now().Add(delay)
	r.mu.Unlock()
}

func TestAuthCoordinator_BridgeConnectorNoInbound(t *testing.T) {
	t.Parallel()

	oauthURL := "https://claude.ai/oauth/authorize?code=bridge-test"

	runtime := &authRuntimeSpyWithDelayedSuccess{
		outputs: []string{
			"Loading...",
			"Visit " + oauthURL + " to authenticate",
		},
	}
	// Success marker appears 500ms after URL is detected (simulates
	// code arriving via normal bridge→tmux send-keys path).
	runtime.scheduleSuccess(1500 * time.Millisecond)

	connector := &bridgeConnectorSpy{}

	spec := testSpec()
	spec.URLTimeout = 5 * time.Second
	spec.CodeTimeout = 10 * time.Second
	spec.SuccessMarkers = []string{"Welcome back"}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "bridge-test-worker",
	}

	err := coord.Run(context.Background(), connector)
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	texts := connector.getSendTexts()
	if len(texts) != 1 {
		t.Fatalf("connector got %d sends, want 1", len(texts))
	}
	if !strings.Contains(texts[0], oauthURL) {
		t.Fatalf("send text = %q, should contain URL", texts[0])
	}
	if !strings.Contains(texts[0], "bridge-test-worker") {
		t.Fatalf("send text = %q, should contain worker name", texts[0])
	}
}

func TestAuthCoordinator_TrustPromptAutoResponse(t *testing.T) {
	t.Parallel()

	runtime := &authRuntimeSpy{
		outputs: []string{
			"Yes, I trust this folder\n  2. No, exit",
			"Welcome back Thinh!",
		},
	}

	spec := testSpec()
	spec.SuccessMarkers = []string{"Welcome back"}
	spec.AutoResponses = []AutoResponse{
		{Marker: "Yes, I trust this folder", Response: "", Once: true},
	}

	connector := &authConnectorSpy{}
	connector.Init(context.Background(), ConnectorConfig{})

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "trust-test",
	}

	err := coord.Run(context.Background(), AdaptConnectorForAuth(connector))
	if err != nil {
		t.Fatalf("Run() error = %v", err)
	}
	if coord.State() != AuthComplete {
		t.Fatalf("state = %v, want AuthComplete", coord.State())
	}

	sent := runtime.getSendLog()
	if len(sent) != 1 || sent[0] != "" {
		t.Fatalf("runtime.Send() = %v, want one empty string (Enter for trust prompt)", sent)
	}
}

func TestAuthCoordinator_SuccessMarkersDoNotMatchBashPrompt(t *testing.T) {
	t.Parallel()

	oauthURL := "https://claude.ai/oauth/authorize?code=test"
	runtime := &authRuntimeSpyWithDelayedSuccess{
		outputs: []string{
			"forge-mon@triassic:~$ export ANTHROPIC_API_KEY=sk-test\nforge-mon@triassic:~$ ",
			"Visit " + oauthURL + " to authenticate",
		},
	}
	// The bash prompt "$" should NOT trigger early success.
	// Success comes later via delayed marker (simulates code arriving via tmux).
	runtime.scheduleSuccess(1500 * time.Millisecond)

	spec := testSpec()
	spec.SuccessMarkers = []string{
		"What can I help you with?",
		"Welcome back",
		"Try \"edit",
	}
	spec.URLTimeout = 3 * time.Second
	spec.CodeTimeout = 10 * time.Second

	connector := &bridgeConnectorSpy{}

	coord := &AuthCoordinator{
		Runtime:    runtime,
		Spec:       spec,
		WorkerName: "bash-prompt-test",
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- coord.Run(context.Background(), connector)
	}()

	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("Run() did not complete — likely stuck on success marker matching bash prompt")
	}

	texts := connector.getSendTexts()
	if len(texts) != 1 {
		t.Fatalf("connector got %d sends, want 1 (URL prompt)", len(texts))
	}
	if !strings.Contains(texts[0], oauthURL) {
		t.Fatalf("send text = %q, should contain URL", texts[0])
	}
}
