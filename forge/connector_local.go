package forge

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"
)

// LocalConnector is a connector for deterministic testing and CLI usage.
// It exposes a local HTTP API to send messages to the worker and
// collects responses for verification.
//
// Implements: Connector + PollReceiver
//
// Usage:
//   ./mon --connector local --local-port 9000
//
// Then send messages via:
//   curl -X POST http://localhost:9000/send -d '{"text":"hello"}'
//
// Read responses:
//   curl http://localhost:9000/responses
type LocalConnector struct {
	port      int
	name      string
	listener  net.Listener
	server    *http.Server
	mu        sync.Mutex
	inbox     []InboundMessage
	responses []Response
	inboxCh   chan struct{} // signal when new message arrives
}

func init() {
	RegisterConnector("local", func() Connector {
		return &LocalConnector{}
	})
}

func (c *LocalConnector) Type() string { return "local" }

func (c *LocalConnector) Capabilities() Caps {
	return CapText | CapFiles | CapMarkdown
}

func (c *LocalConnector) Requirements() Reqs {
	return ReqRuntime
}

func (c *LocalConnector) Init(_ context.Context, cfg ConnectorConfig) error {
	c.name = cfg.WorkerName
	c.inboxCh = make(chan struct{}, 100)

	portStr := cfg.Config["LOCAL_PORT"]
	if portStr != "" {
		fmt.Sscanf(portStr, "%d", &c.port)
	}
	if c.port == 0 {
		c.port = 19876
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/send", c.handleSend)
	mux.HandleFunc("/responses", c.handleResponses)
	mux.HandleFunc("/health", c.handleHealth)

	addr := fmt.Sprintf("127.0.0.1:%d", c.port)
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("local connector listen %s: %w", addr, err)
	}
	c.listener = ln
	c.server = &http.Server{Handler: mux}

	go c.server.Serve(ln)
	return nil
}

func (c *LocalConnector) Send(_ context.Context, resp Response) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.responses = append(c.responses, resp)
	return nil
}

func (c *LocalConnector) Close() error {
	if c.server != nil {
		c.server.Close()
	}
	if c.listener != nil {
		c.listener.Close()
	}
	return nil
}

// PollReceiver implementation

func (c *LocalConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
	// Wait for a message or timeout
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.inboxCh:
	case <-time.After(5 * time.Second):
		return nil, nil
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if len(c.inbox) == 0 {
		return nil, nil
	}

	msgs := make([]InboundMessage, len(c.inbox))
	copy(msgs, c.inbox)
	c.inbox = c.inbox[:0]
	return msgs, nil
}

func (c *LocalConnector) PollInterval() time.Duration {
	return 0 // signal-based, not interval-based
}

// HTTP handlers

func (c *LocalConnector) handleSend(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var msg struct {
		Text string `json:"text"`
		From string `json:"from"`
	}
	if err := json.NewDecoder(r.Body).Decode(&msg); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	if msg.From == "" {
		msg.From = "test"
	}

	c.mu.Lock()
	c.inbox = append(c.inbox, InboundMessage{Text: msg.Text, From: msg.From})
	c.mu.Unlock()

	select {
	case c.inboxCh <- struct{}{}:
	default:
	}

	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]bool{"accepted": true})
}

func (c *LocalConnector) handleResponses(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	resp := make([]Response, len(c.responses))
	copy(resp, c.responses)
	c.mu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (c *LocalConnector) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"ok":        true,
		"connector": "local",
		"worker":    c.name,
	})
}

// Responses returns collected responses (for testing).
func (c *LocalConnector) Responses() []Response {
	c.mu.Lock()
	defer c.mu.Unlock()
	resp := make([]Response, len(c.responses))
	copy(resp, c.responses)
	return resp
}

// Port returns the port the connector is listening on.
func (c *LocalConnector) Port() int {
	return c.port
}

var (
	_ Connector    = (*LocalConnector)(nil)
	_ PollReceiver = (*LocalConnector)(nil)
)
