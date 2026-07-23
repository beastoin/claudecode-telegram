package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"
)

type LocalConnector struct {
	port      int
	name      string
	listener  net.Listener
	server    *http.Server
	mu        sync.Mutex
	inbox     []InboundMessage
	responses []Response
	inboxCh   chan struct{}
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

	addr := "127.0.0.1:0"
	if c.port > 0 {
		addr = fmt.Sprintf("127.0.0.1:%d", c.port)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/send", c.handleSend)
	mux.HandleFunc("/responses", c.handleResponses)
	mux.HandleFunc("/health", c.handleHealth)

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("local connector listen %s: %w", addr, err)
	}
	c.listener = ln
	c.mu.Lock()
	c.port = ln.Addr().(*net.TCPAddr).Port
	c.mu.Unlock()
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

func (c *LocalConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
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
	return 0
}

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

func (c *LocalConnector) Responses() []Response {
	c.mu.Lock()
	defer c.mu.Unlock()
	resp := make([]Response, len(c.responses))
	copy(resp, c.responses)
	return resp
}

func (c *LocalConnector) Port() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.port
}

func (c *LocalConnector) SendAuthPrompt(ctx context.Context, req AuthPromptRequest) (AuthPromptResult, error) {
	c.Send(ctx, Response{
		Text: fmt.Sprintf("Auth required for %s\nURL: %s\nSend the auth code via POST /send", req.WorkerName, req.URL),
	})
	return AuthPromptResult{}, nil
}

// InjectMessage adds a message to the inbox for testing.
func (c *LocalConnector) InjectMessage(msg InboundMessage) {
	c.mu.Lock()
	c.inbox = append(c.inbox, msg)
	c.mu.Unlock()
	select {
	case c.inboxCh <- struct{}{}:
	default:
	}
}

var (
	_ Connector    = (*LocalConnector)(nil)
	_ PollReceiver = (*LocalConnector)(nil)
	_ AuthPrompter = (*LocalConnector)(nil)
)
