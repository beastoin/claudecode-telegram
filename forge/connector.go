package forge

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// Capabilities & Requirements
// ---------------------------------------------------------------------------

// Caps declares what a connector can do (bitmask).
type Caps uint32

const (
	CapText            Caps = 1 << iota // Can send/receive plain text
	CapFiles                            // Can send/receive files
	CapTeamDiscovery                    // Can enumerate other workers
	CapWorkerToWorker                   // Can send messages to other workers
	CapThreads                          // Supports threaded replies
	CapMarkdown                         // Supports markdown formatting
	CapVoice                            // Voice messages
	CapTemplates                        // Message templates (WhatsApp)
	CapMultiChannel                     // Multiple channels/chats
)

// Reqs declares what a connector needs from the host (bitmask).
type Reqs uint32

const (
	ReqTmuxReady    Reqs = 1 << iota // tmux session must exist before Init
	ReqRuntime                        // Needs Runtime.Send() for self-injection
	ReqHTTPListener                   // Needs host to bind HTTP port (webhook)
	ReqTransport                      // Needs existing Transport for bridge communication
)

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

// ErrNoMessage is returned when a poll finds no messages.
var ErrNoMessage = errors.New("no message available")

// ErrNoRoute is returned when a connector cannot route to the requested target.
var ErrNoRoute = errors.New("no route to target")

// ---------------------------------------------------------------------------
// Core Types
// ---------------------------------------------------------------------------

// ConnectorConfig holds connector-specific configuration.
type ConnectorConfig struct {
	WorkerName string
	BridgeURL  string
	ListenAddr string
	Config     map[string]string
	Runtime    Runtime
}

// InboundMessage is the normalized message a connector delivers to the worker.
type InboundMessage struct {
	Text     string
	From     string
	ThreadID string
	Files    []File
	Raw      []byte // Original platform payload for connector-specific handling
}

// File represents a file received from or sent to a platform.
type File struct {
	Name string
	URL  string
	Data []byte
	Mime string
	Size int64
}

// Response is what the worker sends back through the connector.
type Response struct {
	Text     string
	ThreadID string
	Target   string // For worker-to-worker or specific chat routing
	Files    []FileSend
}

// FileSend describes a file to send through the connector.
type FileSend struct {
	Name string
	Path string // Local file path
	Data []byte // Or raw data
	Mime string
}

// WorkerInfo describes a reachable worker (for team discovery).
type WorkerInfo struct {
	Name        string
	SendExample string // How to reach this worker
}

// ---------------------------------------------------------------------------
// Connector Interface
// ---------------------------------------------------------------------------

// Connector is the base interface every platform adapter implements.
type Connector interface {
	// Type returns the connector identifier (e.g., "bridge", "telegram", "slack").
	Type() string

	// Init sets up the connector with the provided configuration and environment.
	Init(ctx context.Context, cfg ConnectorConfig) error

	// Send delivers a response through this connector's native mechanism.
	Send(ctx context.Context, resp Response) error

	// Capabilities returns what this connector can do.
	Capabilities() Caps

	// Requirements returns what this connector needs from the host.
	Requirements() Reqs

	// Close tears down the connector.
	Close() error
}

// ---------------------------------------------------------------------------
// Receiver sub-interfaces (connectors implement exactly one)
// ---------------------------------------------------------------------------

// ExternalReceiver: messages arrive via external process (bridge injects into tmux).
// Worker doesn't receive messages itself — bridge.py pushes into tmux directly.
// The connector only handles outbound (Send) and team features.
type ExternalReceiver interface {
	Connector
	// IsExternal is a marker method. No receive method needed.
	IsExternal()
}

// PushReceiver: connector starts an HTTP server, platform pushes via webhook.
// Used by: Telegram (webhook mode)
type PushReceiver interface {
	Connector
	// Handler returns the HTTP handler that processes inbound webhooks.
	Handler() http.Handler
	// ListenAddr returns the address the host should bind to.
	ListenAddr() string
}

// PollReceiver: host calls Poll() on interval, connector pulls from platform.
// Used by: Telegram (getUpdates), WhatsApp
type PollReceiver interface {
	Connector
	// Poll checks for new messages. Returns zero or more messages.
	Poll(ctx context.Context) ([]InboundMessage, error)
	// PollInterval returns how often the host should call Poll.
	// Return 0 for connectors with built-in long-poll (e.g., Telegram getUpdates).
	PollInterval() time.Duration
}

// StreamReceiver: connector maintains persistent connection, pushes to channel.
// Used by: Slack (Socket Mode WebSocket)
type StreamReceiver interface {
	Connector
	// Messages returns a channel that receives inbound messages.
	Messages() <-chan InboundMessage
}

// ---------------------------------------------------------------------------
// Optional interfaces
// ---------------------------------------------------------------------------

// TeamAware is implemented by connectors that can discover and message other workers.
type TeamAware interface {
	DiscoverWorkers(ctx context.Context) ([]WorkerInfo, error)
	SendToWorker(ctx context.Context, target string, msg string) error
}

// FileCapable is implemented by connectors that can send/receive files.
type FileCapable interface {
	SendFile(ctx context.Context, f FileSend) error
	DownloadFile(ctx context.Context, fileRef string) (File, error)
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

var (
	registryMu   sync.RWMutex
	connectorReg = map[string]func() Connector{}
)

// RegisterConnector registers a connector factory by type name.
func RegisterConnector(typeName string, factory func() Connector) {
	registryMu.Lock()
	defer registryMu.Unlock()
	connectorReg[typeName] = factory
}

// NewConnector creates a connector by its registered type name.
func NewConnector(typeName string) (Connector, error) {
	registryMu.RLock()
	factory, ok := connectorReg[typeName]
	registryMu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown connector type %q", typeName)
	}
	return factory(), nil
}

// ListConnectorTypes returns all registered connector type names.
func ListConnectorTypes() []string {
	registryMu.RLock()
	defer registryMu.RUnlock()
	types := make([]string, 0, len(connectorReg))
	for t := range connectorReg {
		types = append(types, t)
	}
	return types
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func bytesReader(data []byte) io.Reader {
	return bytes.NewReader(data)
}
