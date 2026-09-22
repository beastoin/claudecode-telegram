package forge

import (
	"context"
	"time"

	"github.com/beastoin/claudecode-telegram/forge/connector"
	"github.com/beastoin/claudecode-telegram/forge/engine"
)

// --- Engine types (re-exported from engine/) ---

type EngineDriver = engine.EngineDriver
type AuthSpec = engine.AuthSpec
type AutoResponse = engine.AutoResponse
type PrepareRequest = engine.PrepareRequest
type PreparedEngine = engine.PreparedEngine
type PreparedFile = engine.PreparedFile
type StartSpec = engine.StartSpec
type EngineCapabilities = engine.EngineCapabilities
type ClaudeCodeDriver = engine.ClaudeCodeDriver
type HookManager = engine.HookManager
type EmitOptions = engine.EmitOptions

// Auth types
type AuthState = engine.AuthState
type AuthCoordinator = engine.AuthCoordinator

const (
	AuthIdle           = engine.AuthIdle
	AuthWaitingForURL  = engine.AuthWaitingForURL
	AuthURLSent        = engine.AuthURLSent
	AuthWaitingForCode = engine.AuthWaitingForCode
	AuthCodeSubmitted  = engine.AuthCodeSubmitted
	AuthComplete       = engine.AuthComplete
	AuthFailed         = engine.AuthFailed
)

// Engine factory
var NewEngineDriver = engine.NewEngineDriver

// Emit functions
var ParseEmitArgs = engine.ParseEmitArgs
var RunEmit = engine.RunEmit

// GenerateEmitScript returns a shell script for hook output forwarding.
var GenerateEmitScript = engine.GenerateEmitScript

// DetectOAuthURL searches text for an OAuth URL matching patterns.
var DetectOAuthURL = engine.DetectOAuthURL

// Hook helpers (exported for use by integrity.go and other root files)
var NormalizeEventHooks = engine.NormalizeEventHooks
var EnsureHookGroup = engine.EnsureHookGroup
var AppendCommandHook = engine.AppendCommandHook
var GroupHasCommand = engine.GroupHasCommand
var HookGroupsToAny = engine.HookGroupsToAny
var PruneStaleHooks = engine.PruneStaleHooks
var HookCommandExists = engine.HookCommandExists
var AppendHookEntry = engine.AppendHookEntry

// Auth helper functions (wrappers that bridge connector types to engine types)

func matchAuthCode(msg InboundMessage, promptMsgID string) string {
	return engine.MatchAuthCode(engine.InboundMessage{
		Text:      msg.Text,
		From:      msg.From,
		ReplyToID: msg.ReplyToID,
	}, promptMsgID)
}

func matchesAnyMarker(output string, markers []string) bool {
	return engine.MatchesAnyMarker(output, markers)
}

func extractAuthCode(text string) string {
	return engine.ExtractAuthCode(text)
}

func looksLikeAuthCode(s string) bool {
	return engine.LooksLikeAuthCode(s)
}

// --- Bridge types between engine/ consumer interfaces and connector/ types ---
// The engine/ package defines its own consumer interfaces (AuthConnector, etc.)
// to avoid importing connector/. These adapters bridge the gap so that
// root-package code can pass connector.Connector values to engine.AuthCoordinator.

// connectorAuthBase provides the core AuthConnector implementation.
type connectorAuthBase struct {
	conn Connector
}

func (a *connectorAuthBase) Type() string { return a.conn.Type() }
func (a *connectorAuthBase) Send(ctx context.Context, resp engine.AuthResponse) error {
	return a.conn.Send(ctx, Response{Text: resp.Text})
}

// connectorAuthNotifier adds AuthStatusNotifier if the underlying connector supports it.
func adaptNotifyStatus(conn Connector, status, detail string) {
	if notifier, ok := conn.(connector.AuthStatusNotifier); ok {
		notifier.NotifyAuthStatus(status, detail)
	}
}

// --- Poll-based adapters ---

type connectorPollAdapter struct {
	connectorAuthBase
	pr connector.PollReceiver
}

func (a *connectorPollAdapter) PollInterval() time.Duration { return a.pr.PollInterval() }
func (a *connectorPollAdapter) Poll(ctx context.Context) ([]engine.InboundMessage, error) {
	msgs, err := a.pr.Poll(ctx)
	if err != nil {
		return nil, err
	}
	result := make([]engine.InboundMessage, len(msgs))
	for i, m := range msgs {
		result[i] = engine.InboundMessage{Text: m.Text, From: m.From, ReplyToID: m.ReplyToID}
	}
	return result, nil
}
func (a *connectorPollAdapter) NotifyAuthStatus(status, detail string) {
	adaptNotifyStatus(a.conn, status, detail)
}

type connectorPollPrompterAdapter struct {
	connectorPollAdapter
	ap connector.AuthPrompter
}

func (a *connectorPollPrompterAdapter) SendAuthPrompt(ctx context.Context, req engine.AuthPromptRequest) (engine.AuthPromptResult, error) {
	result, err := a.ap.SendAuthPrompt(ctx, connector.AuthPromptRequest{WorkerName: req.WorkerName, URL: req.URL})
	if err != nil {
		return engine.AuthPromptResult{}, err
	}
	return engine.AuthPromptResult{MessageID: result.MessageID}, nil
}

// --- Stream-based adapters ---

type connectorStreamAdapter struct {
	connectorAuthBase
	sr connector.StreamReceiver
}

func (a *connectorStreamAdapter) Messages() <-chan engine.InboundMessage {
	srcCh := a.sr.Messages()
	dstCh := make(chan engine.InboundMessage)
	go func() {
		defer close(dstCh)
		for msg := range srcCh {
			dstCh <- engine.InboundMessage{Text: msg.Text, From: msg.From, ReplyToID: msg.ReplyToID}
		}
	}()
	return dstCh
}
func (a *connectorStreamAdapter) NotifyAuthStatus(status, detail string) {
	adaptNotifyStatus(a.conn, status, detail)
}

type connectorStreamPrompterAdapter struct {
	connectorStreamAdapter
	ap connector.AuthPrompter
}

func (a *connectorStreamPrompterAdapter) SendAuthPrompt(ctx context.Context, req engine.AuthPromptRequest) (engine.AuthPromptResult, error) {
	result, err := a.ap.SendAuthPrompt(ctx, connector.AuthPromptRequest{WorkerName: req.WorkerName, URL: req.URL})
	if err != nil {
		return engine.AuthPromptResult{}, err
	}
	return engine.AuthPromptResult{MessageID: result.MessageID}, nil
}

// --- Fallback (no poll or stream) ---

type connectorBasicAdapter struct {
	connectorAuthBase
}

func (a *connectorBasicAdapter) NotifyAuthStatus(status, detail string) {
	adaptNotifyStatus(a.conn, status, detail)
}

// AdaptConnectorForAuth wraps a Connector for use with engine.AuthCoordinator.
// The returned adapter only implements PollReceiver/StreamReceiver/AuthPrompter
// if the underlying connector supports them, so the engine's type assertions work.
func AdaptConnectorForAuth(conn Connector) engine.AuthConnector {
	base := connectorAuthBase{conn: conn}
	ap, hasAP := conn.(connector.AuthPrompter)

	if pr, ok := conn.(connector.PollReceiver); ok {
		pollAdapter := &connectorPollAdapter{connectorAuthBase: base, pr: pr}
		if hasAP {
			return &connectorPollPrompterAdapter{connectorPollAdapter: *pollAdapter, ap: ap}
		}
		return pollAdapter
	}

	if sr, ok := conn.(connector.StreamReceiver); ok {
		streamAdapter := &connectorStreamAdapter{connectorAuthBase: base, sr: sr}
		if hasAP {
			return &connectorStreamPrompterAdapter{connectorStreamAdapter: *streamAdapter, ap: ap}
		}
		return streamAdapter
	}

	return &connectorBasicAdapter{connectorAuthBase: base}
}
