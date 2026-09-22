package engine

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/beastoin/claudecode-telegram/forge/runtime"
)

// AuthState represents the current state of the authentication flow.
type AuthState int

const (
	AuthIdle AuthState = iota
	AuthWaitingForURL
	AuthURLSent
	AuthWaitingForCode
	AuthCodeSubmitted
	AuthComplete
	AuthFailed
)

func (s AuthState) String() string {
	switch s {
	case AuthIdle:
		return "idle"
	case AuthWaitingForURL:
		return "waiting-for-url"
	case AuthURLSent:
		return "url-sent"
	case AuthWaitingForCode:
		return "waiting-for-code"
	case AuthCodeSubmitted:
		return "code-submitted"
	case AuthComplete:
		return "complete"
	case AuthFailed:
		return "failed"
	default:
		return "unknown"
	}
}

// --- Consumer interfaces for connector types ---
// These avoid importing the connector package directly.

// AuthConnector is the minimal connector interface needed by AuthCoordinator.
type AuthConnector interface {
	Type() string
	Send(ctx context.Context, resp AuthResponse) error
}

// AuthResponse is the engine-local response type for auth messages.
type AuthResponse struct {
	Text string
}

// AuthPrompter is implemented by connectors that support native auth UX.
type AuthPrompter interface {
	SendAuthPrompt(ctx context.Context, req AuthPromptRequest) (AuthPromptResult, error)
}

// AuthPromptRequest contains the data sent to the connector for auth.
type AuthPromptRequest struct {
	WorkerName string
	URL        string
}

// AuthPromptResult is the response from an auth prompt.
type AuthPromptResult struct {
	MessageID string
}

// AuthStatusNotifier is implemented by connectors that show auth progress.
type AuthStatusNotifier interface {
	NotifyAuthStatus(status string, detail string)
}

// InboundMessage represents a message received from the connector.
type InboundMessage struct {
	Text      string
	From      string
	ReplyToID string
}

// PollReceiver is implemented by connectors that support polling for messages.
type PollReceiver interface {
	PollInterval() time.Duration
	Poll(ctx context.Context) ([]InboundMessage, error)
}

// StreamReceiver is implemented by connectors that push messages via a channel.
type StreamReceiver interface {
	Messages() <-chan InboundMessage
}

// AuthCoordinator manages OAuth authentication as a standalone lifecycle phase.
// It reads from the Runtime (tmux output), gets its auth contract from the
// Engine (AuthSpec), and prompts/receives codes through the Connector.
type AuthCoordinator struct {
	Runtime    runtime.RuntimeMonitor
	Spec       AuthSpec
	WorkerName string

	mu    sync.Mutex
	state AuthState
}

// State returns the current auth state.
func (a *AuthCoordinator) State() AuthState {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.state
}

func (a *AuthCoordinator) setState(s AuthState) {
	a.mu.Lock()
	a.state = s
	a.mu.Unlock()
}

// Run performs the full auth flow: detect URL in runtime output, send prompt
// via connector, receive code via connector's inbound, submit to runtime.
// This is a blocking call that owns its own receive loop.
func (a *AuthCoordinator) Run(ctx context.Context, connector AuthConnector) error {
	a.setState(AuthWaitingForURL)

	url, err := a.pollForURL(ctx)
	if err != nil {
		a.setState(AuthFailed)
		return fmt.Errorf("auth url detection: %w", err)
	}
	if url == "" {
		a.setState(AuthComplete)
		return nil
	}

	a.setState(AuthURLSent)
	msgID, err := a.sendPrompt(ctx, connector, url)
	if err != nil {
		a.setState(AuthFailed)
		return fmt.Errorf("send auth prompt: %w", err)
	}

	a.setState(AuthWaitingForCode)

	_, hasPoll := connector.(PollReceiver)
	_, hasStream := connector.(StreamReceiver)
	hasInbound := hasPoll || hasStream

	if hasInbound {
		code, err := a.waitForCode(ctx, connector, msgID)
		if err != nil {
			a.setState(AuthFailed)
			a.notifyStatus(connector, "failed", "Timed out waiting for auth code")
			return fmt.Errorf("wait for auth code: %w", err)
		}

		a.setState(AuthCodeSubmitted)
		a.notifyStatus(connector, "submitting", "Code received, submitting...")
		if literalSender, ok := a.Runtime.(interface{ SendLiteral(string) error }); ok {
			if err := literalSender.SendLiteral(code); err != nil {
				a.setState(AuthFailed)
				a.notifyStatus(connector, "failed", "Failed to submit code")
				return fmt.Errorf("submit auth code to runtime: %w", err)
			}
		} else if sender, ok := a.Runtime.(interface{ Send(string) error }); ok {
			if err := sender.Send(code); err != nil {
				a.setState(AuthFailed)
				a.notifyStatus(connector, "failed", "Failed to submit code")
				return fmt.Errorf("submit auth code to runtime: %w", err)
			}
		}
	}
	// When connector has no inbound (e.g. bridge), the auth code arrives
	// via the normal message delivery path (bridge → tmux send-keys).
	// Just wait for the success marker in tmux output.

	a.notifyStatus(connector, "verifying", "Waiting for authentication...")
	if err := a.waitForCompletion(ctx); err != nil {
		a.setState(AuthFailed)
		a.notifyStatus(connector, "failed", "Authentication failed — check the code and try again")
		return fmt.Errorf("auth verification: %w", err)
	}

	a.setState(AuthComplete)
	a.notifyStatus(connector, "success", "Authentication complete!")
	return nil
}

// sendPrompt sends the auth URL through the connector.
func (a *AuthCoordinator) sendPrompt(ctx context.Context, connector AuthConnector, url string) (string, error) {
	if prompter, ok := connector.(AuthPrompter); ok {
		result, err := prompter.SendAuthPrompt(ctx, AuthPromptRequest{
			WorkerName: a.WorkerName,
			URL:        url,
		})
		if err != nil {
			return "", err
		}
		return result.MessageID, nil
	}

	err := connector.Send(ctx, AuthResponse{
		Text: fmt.Sprintf("Auth required for %s\n\nVisit this URL to authenticate:\n%s\n\nThen send the auth code back here.", a.WorkerName, url),
	})
	return "", err
}

// waitForCode runs a temporary receive loop on the connector to get the auth code.
func (a *AuthCoordinator) waitForCode(ctx context.Context, connector AuthConnector, promptMsgID string) (string, error) {
	timeout := a.Spec.CodeTimeout
	if timeout == 0 {
		timeout = 15 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	switch c := connector.(type) {
	case PollReceiver:
		return pollForCode(ctx, c, promptMsgID)
	case StreamReceiver:
		return streamForCode(ctx, c, promptMsgID)
	default:
		<-ctx.Done()
		return "", fmt.Errorf("connector %q has no inbound receive pattern for auth", connector.Type())
	}
}

func pollForCode(ctx context.Context, c PollReceiver, promptMsgID string) (string, error) {
	interval := c.PollInterval()
	if interval <= 0 {
		interval = 100 * time.Millisecond
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-ticker.C:
			msgs, err := c.Poll(ctx)
			if err != nil {
				continue
			}
			for _, msg := range msgs {
				if code := MatchAuthCode(msg, promptMsgID); code != "" {
					return code, nil
				}
			}
		}
	}
}

func streamForCode(ctx context.Context, c StreamReceiver, promptMsgID string) (string, error) {
	ch := c.Messages()
	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case msg, ok := <-ch:
			if !ok {
				return "", fmt.Errorf("stream closed before auth code received")
			}
			if code := MatchAuthCode(msg, promptMsgID); code != "" {
				return code, nil
			}
		}
	}
}

// MatchAuthCode checks if an inbound message contains an auth code.
// Uses reply-to matching when a prompt message ID is available,
// falls back to code-like text matching otherwise.
func MatchAuthCode(msg InboundMessage, promptMsgID string) string {
	code := ExtractAuthCode(msg.Text)
	if code == "" {
		return ""
	}
	if promptMsgID != "" && msg.ReplyToID == promptMsgID {
		return code
	}
	if promptMsgID == "" && LooksLikeAuthCode(code) {
		return code
	}
	return ""
}

// DetectOAuthURL searches text for an OAuth URL matching any of the given patterns.
// Handles line-wrapped URLs by joining continuation lines.
func DetectOAuthURL(text string, patterns []*regexp.Regexp) string {
	lines := strings.Split(text, "\n")
	for i, line := range lines {
		line = strings.TrimSpace(line)
		for _, pat := range patterns {
			match := pat.FindString(line)
			if match == "" {
				continue
			}
			url := match
			for j := i + 1; j < len(lines); j++ {
				cont := strings.TrimSpace(lines[j])
				if cont == "" || strings.HasPrefix(cont, " ") {
					break
				}
				if isURLContinuation(cont) {
					url += cont
				} else {
					break
				}
			}
			return strings.TrimRight(url, ".,;:!?")
		}
	}
	return ""
}

func isURLContinuation(line string) bool {
	if line == "" {
		return false
	}
	if strings.HasPrefix(line, "http") {
		return false
	}
	for _, prefix := range []string{"Paste", "Browser", "Select", "Welcome", " "} {
		if strings.HasPrefix(line, prefix) {
			return false
		}
	}
	return strings.ContainsAny(line, "=&%/_-.")
}

func (a *AuthCoordinator) pollForURL(ctx context.Context) (string, error) {
	timeout := a.Spec.URLTimeout
	if timeout == 0 {
		timeout = 3 * time.Minute
	}
	deadline := time.After(timeout)
	interval := 500 * time.Millisecond
	fastPhase := time.After(30 * time.Second)
	fastPhaseDone := false
	handled := map[string]bool{}
	for {
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-deadline:
			return "", nil
		default:
		}

		output, err := a.captureOutput()
		if err != nil {
			select {
			case <-time.After(interval):
				continue
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}

		if MatchesAnyMarker(output, a.Spec.SuccessMarkers) {
			return "", nil
		}

		processAutoResponses(output, a.Spec.AutoResponses, handled, a.Runtime)

		url := DetectOAuthURL(output, a.Spec.URLPatterns)
		if url != "" {
			return url, nil
		}

		if !fastPhaseDone {
			select {
			case <-fastPhase:
				fastPhaseDone = true
				interval = 1 * time.Second
			default:
			}
		}

		select {
		case <-time.After(interval):
		case <-ctx.Done():
			return "", ctx.Err()
		case <-deadline:
			return "", nil
		}
	}
}

// MatchesAnyMarker returns true if the output contains any of the given markers.
func MatchesAnyMarker(output string, markers []string) bool {
	for _, m := range markers {
		if strings.Contains(output, m) {
			return true
		}
	}
	return false
}

func (a *AuthCoordinator) captureOutput() (string, error) {
	if hist, ok := a.Runtime.(runtime.RuntimeHistoryCapture); ok {
		return hist.CaptureHistory(2000)
	}
	return a.Runtime.LastOutput()
}

// ExtractAuthCode extracts the auth code from a message, stripping any
// reply context that connectors may append.
func ExtractAuthCode(text string) string {
	text = strings.TrimSpace(text)
	if text == "" {
		return ""
	}
	if idx := strings.Index(text, "\n\nContext (previous message):"); idx >= 0 {
		text = text[:idx]
	}
	if idx := strings.Index(text, "\n"); idx >= 0 {
		text = text[:idx]
	}
	return strings.TrimSpace(text)
}

func (a *AuthCoordinator) verifyAuth(ctx context.Context) error {
	timeout := 30 * time.Second
	deadline := time.After(timeout)
	interval := 500 * time.Millisecond

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return nil
		default:
		}

		output, err := a.captureOutput()
		if err != nil {
			select {
			case <-time.After(interval):
				continue
			case <-ctx.Done():
				return ctx.Err()
			}
		}

		if MatchesAnyMarker(output, a.Spec.SuccessMarkers) {
			return nil
		}

		for _, marker := range a.Spec.FailureMarkers {
			if strings.Contains(output, marker) {
				return fmt.Errorf("%s", marker)
			}
		}

		if strings.Contains(output, "OAuth error") || strings.Contains(output, "Invalid code") {
			return fmt.Errorf("invalid auth code")
		}

		select {
		case <-time.After(interval):
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return nil
		}
	}
}

func (a *AuthCoordinator) waitForCompletion(ctx context.Context) error {
	timeout := a.Spec.CompletionTimeout
	if timeout == 0 {
		timeout = 2 * time.Minute
	}
	deadline := time.After(timeout)
	interval := 2 * time.Second
	handled := map[string]bool{}

	// Snapshot baseline: pre-populate handled map with markers already in
	// scrollback so they don't re-fire. New markers (like "Press Enter to
	// retry") will fire because they aren't in the baseline.
	baseline, _ := a.captureOutput()
	for _, ar := range a.Spec.AutoResponses {
		if strings.Contains(baseline, ar.Marker) {
			handled[ar.Marker] = true
		}
	}

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return fmt.Errorf("timed out waiting for auth completion")
		default:
		}

		output, err := a.captureOutput()
		if err != nil {
			select {
			case <-time.After(interval):
				continue
			case <-ctx.Done():
				return ctx.Err()
			}
		}

		if MatchesAnyMarker(output, a.Spec.SuccessMarkers) {
			return nil
		}

		for _, marker := range a.Spec.FailureMarkers {
			if strings.Contains(output, marker) {
				return fmt.Errorf("%s", marker)
			}
		}

		processAutoResponses(output, a.Spec.AutoResponses, handled, a.Runtime)

		select {
		case <-time.After(interval):
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return fmt.Errorf("timed out waiting for auth completion")
		}
	}
}

// processAutoResponses fires auto-responses using edge-triggered semantics.
// For Once: true markers, fires once then never again.
// For Once: false markers, fires on absent→present transitions. When the marker
// disappears from output and reappears, it fires again. This prevents stale
// tmux scrollback from generating continuous spurious keypresses.
func processAutoResponses(output string, autoResponses []AutoResponse, handled map[string]bool, runtime runtime.RuntimeMonitor) {
	for _, ar := range autoResponses {
		present := strings.Contains(output, ar.Marker)
		if ar.Once {
			if handled[ar.Marker] {
				continue
			}
			if present {
				if sender, ok := runtime.(interface{ Send(string) error }); ok {
					sender.Send(ar.Response)
				}
				handled[ar.Marker] = true
			}
		} else {
			if present && !handled[ar.Marker] {
				if sender, ok := runtime.(interface{ Send(string) error }); ok {
					sender.Send(ar.Response)
				}
				handled[ar.Marker] = true
			} else if !present && handled[ar.Marker] {
				delete(handled, ar.Marker)
			}
		}
	}
}

func (a *AuthCoordinator) notifyStatus(connector AuthConnector, status, detail string) {
	if n, ok := connector.(AuthStatusNotifier); ok {
		n.NotifyAuthStatus(status, detail)
	}
}

// LooksLikeAuthCode returns true if the string looks like an auth code.
func LooksLikeAuthCode(s string) bool {
	if len(s) < 4 || len(s) > 128 {
		return false
	}
	return !strings.Contains(s, " ")
}
