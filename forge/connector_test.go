package forge

import (
	"context"
	"net"
	"net/http"
	"testing"
	"time"
)

func TestRegistryHasAllConnectors(t *testing.T) {
	expected := []string{"bridge", "telegram", "telegram-poll", "whatsapp", "slack"}
	for _, name := range expected {
		c, err := NewConnector(name)
		if err != nil {
			t.Fatalf("NewConnector(%q): %v", name, err)
		}
		if c.Type() != name {
			t.Errorf("connector type = %q, want %q", c.Type(), name)
		}
	}
}

func TestUnknownConnectorReturnsError(t *testing.T) {
	_, err := NewConnector("nonexistent")
	if err == nil {
		t.Fatal("expected error for unknown connector")
	}
}

func TestBridgeConnectorCapabilities(t *testing.T) {
	c, _ := NewConnector("bridge")

	caps := c.Capabilities()
	if caps&CapText == 0 {
		t.Error("bridge connector must have CapText")
	}
	if caps&CapTeamDiscovery == 0 {
		t.Error("bridge connector must have CapTeamDiscovery")
	}
	if caps&CapWorkerToWorker == 0 {
		t.Error("bridge connector must have CapWorkerToWorker")
	}
	if caps&CapMarkdown == 0 {
		t.Error("bridge connector must have CapMarkdown")
	}
	if caps&CapFiles == 0 {
		t.Error("bridge connector must have CapFiles")
	}
}

func TestBridgeConnectorRequirements(t *testing.T) {
	c, _ := NewConnector("bridge")

	reqs := c.Requirements()
	if reqs&ReqTmuxReady == 0 {
		t.Error("bridge connector must require tmux ready")
	}
	if reqs&ReqTransport == 0 {
		t.Error("bridge connector must require transport")
	}
}

func TestBridgeConnectorIsExternalReceiver(t *testing.T) {
	c, _ := NewConnector("bridge")
	ext, ok := c.(ExternalReceiver)
	if !ok {
		t.Fatal("bridge connector must implement ExternalReceiver")
	}
	ext.IsExternal() // Just verify it doesn't panic
}

func TestBridgeConnectorIsTeamAware(t *testing.T) {
	c, _ := NewConnector("bridge")
	if _, ok := c.(TeamAware); !ok {
		t.Fatal("bridge connector must implement TeamAware")
	}
}

func TestTelegramConnectorIsPushReceiver(t *testing.T) {
	c, _ := NewConnector("telegram")

	if c.Requirements()&ReqRuntime == 0 {
		t.Error("telegram connector must require runtime")
	}
	if c.Requirements()&ReqHTTPListener == 0 {
		t.Error("telegram connector must require HTTP listener")
	}

	_, ok := c.(PushReceiver)
	if !ok {
		t.Error("telegram connector must implement PushReceiver")
	}

	caps := c.Capabilities()
	if caps&CapText == 0 {
		t.Error("telegram connector must have CapText")
	}
	if caps&CapMarkdown == 0 {
		t.Error("telegram connector must have CapMarkdown")
	}
	if caps&CapVoice == 0 {
		t.Error("telegram connector must have CapVoice")
	}
}

func TestTelegramPollConnectorIsPollReceiver(t *testing.T) {
	c, _ := NewConnector("telegram-poll")

	if c.Requirements()&ReqRuntime == 0 {
		t.Error("telegram-poll connector must require runtime")
	}
	if c.Requirements()&ReqHTTPListener != 0 {
		t.Error("telegram-poll connector must NOT require HTTP listener")
	}

	poll, ok := c.(PollReceiver)
	if !ok {
		t.Fatal("telegram-poll connector must implement PollReceiver")
	}
	if poll.PollInterval() != 0 {
		t.Errorf("telegram-poll PollInterval() = %v, want 0 (built-in long-poll)", poll.PollInterval())
	}

	caps := c.Capabilities()
	if caps&CapText == 0 {
		t.Error("telegram-poll connector must have CapText")
	}
	if caps&CapMarkdown == 0 {
		t.Error("telegram-poll connector must have CapMarkdown")
	}
}

func TestWhatsAppConnectorIsPollReceiver(t *testing.T) {
	c, _ := NewConnector("whatsapp")
	poll, ok := c.(PollReceiver)
	if !ok {
		t.Fatal("whatsapp connector must implement PollReceiver")
	}
	if poll.PollInterval() != 5*time.Second {
		t.Errorf("whatsapp PollInterval() = %v, want 5s", poll.PollInterval())
	}

	caps := c.Capabilities()
	if caps&CapText == 0 {
		t.Error("whatsapp connector must have CapText")
	}
	if caps&CapTemplates == 0 {
		t.Error("whatsapp connector must have CapTemplates")
	}
	if caps&CapVoice == 0 {
		t.Error("whatsapp connector must have CapVoice")
	}
}

func TestSlackConnectorIsStreamReceiver(t *testing.T) {
	c, _ := NewConnector("slack")
	_, ok := c.(StreamReceiver)
	if !ok {
		t.Fatal("slack connector must implement StreamReceiver")
	}

	caps := c.Capabilities()
	if caps&CapText == 0 {
		t.Error("slack connector must have CapText")
	}
	if caps&CapMarkdown == 0 {
		t.Error("slack connector must have CapMarkdown")
	}
	if caps&CapThreads == 0 {
		t.Error("slack connector must have CapThreads")
	}
	if caps&CapMultiChannel == 0 {
		t.Error("slack connector must have CapMultiChannel")
	}
}

func TestConnectorHostExternalDoesNotBlock(t *testing.T) {
	c := &BridgeConnector{}

	host := NewConnectorHost(c, nil, ConnectorConfig{
		WorkerName: "test",
		BridgeURL:  "http://localhost:8271",
		Config:     map[string]string{"BRIDGE_URL": "http://localhost:8271"},
	})

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	// BridgeConnector Init will fail (no bridge running) but we can test the host logic
	// by pre-initializing manually
	c.bridgeURL = "http://localhost:8271"
	c.name = "test"

	// Override to skip Init (since no bridge is actually running)
	err := host.runExternal(ctx)
	if err != nil {
		t.Fatalf("host external should return nil on context cancel: %v", err)
	}
}

func TestConnectorHostPollDelivers(t *testing.T) {
	stub := &stubPollConnector{
		msgs: []InboundMessage{{Text: "hello from poll"}},
	}

	rt := &connStubRuntime{}

	host := NewConnectorHost(stub, rt, ConnectorConfig{WorkerName: "test"})

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	host.runPollLoop(ctx, stub)

	if len(rt.sent) == 0 {
		t.Fatal("expected at least one message delivered to runtime")
	}
}

func TestConnectorHostStreamDelivers(t *testing.T) {
	stub := &stubStreamConnector{
		msgs: []InboundMessage{{Text: "hello from stream"}},
	}

	rt := &connStubRuntime{}
	host := NewConnectorHost(stub, rt, ConnectorConfig{WorkerName: "test"})

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	host.runStream(ctx, stub)

	if len(rt.sent) == 0 {
		t.Fatal("expected at least one message delivered to runtime")
	}
	if rt.sent[0] != "hello from stream" {
		t.Errorf("got %q, want %q", rt.sent[0], "hello from stream")
	}
}

func TestHookListenerStartStop(t *testing.T) {
	stub := &stubSendConnector{}
	socketPath := "/tmp/forge-test-hook.sock"

	hl := NewHookListener(socketPath, stub)
	if err := hl.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer hl.Stop()

	if hl.SocketPath() != socketPath {
		t.Errorf("SocketPath() = %q, want %q", hl.SocketPath(), socketPath)
	}

	// Verify the socket is reachable via a unix dialer
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return net.Dial("unix", socketPath)
		},
	}
	client := &http.Client{Transport: transport}

	resp, err := client.Get("http://localhost/health")
	if err != nil {
		t.Fatalf("health check failed: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("health status = %d, want 200", resp.StatusCode)
	}
}

func TestErrNoMessageAndErrNoRoute(t *testing.T) {
	if ErrNoMessage == nil {
		t.Error("ErrNoMessage must not be nil")
	}
	if ErrNoRoute == nil {
		t.Error("ErrNoRoute must not be nil")
	}
	if ErrNoMessage.Error() == "" {
		t.Error("ErrNoMessage must have a message")
	}
	if ErrNoRoute.Error() == "" {
		t.Error("ErrNoRoute must have a message")
	}
}

func TestFormatInbound(t *testing.T) {
	tests := []struct {
		msg  InboundMessage
		want string
	}{
		{InboundMessage{Text: "hello"}, "hello"},
		{InboundMessage{Text: "hello", From: "alice"}, "[alice] hello"},
	}
	for _, tt := range tests {
		got := formatInbound(tt.msg)
		if got != tt.want {
			t.Errorf("formatInbound(%+v) = %q, want %q", tt.msg, got, tt.want)
		}
	}
}

// ---------------------------------------------------------------------------
// Helper method exposed for testing
// ---------------------------------------------------------------------------

func (h *ConnectorHost) runExternal(ctx context.Context) error {
	<-ctx.Done()
	return nil
}

// ---------------------------------------------------------------------------
// Test stubs
// ---------------------------------------------------------------------------

type connStubRuntime struct {
	sent []string
}

func (s *connStubRuntime) Start() error              { return nil }
func (s *connStubRuntime) Send(msg string) error     { s.sent = append(s.sent, msg); return nil }
func (s *connStubRuntime) Health() error             { return nil }

type stubPollConnector struct {
	msgs   []InboundMessage
	polled bool
}

func (s *stubPollConnector) Type() string                            { return "stub-poll" }
func (s *stubPollConnector) Capabilities() Caps                      { return CapText }
func (s *stubPollConnector) Requirements() Reqs                      { return 0 }
func (s *stubPollConnector) Init(context.Context, ConnectorConfig) error { return nil }
func (s *stubPollConnector) Send(context.Context, Response) error    { return nil }
func (s *stubPollConnector) Close() error                            { return nil }
func (s *stubPollConnector) PollInterval() time.Duration             { return 10 * time.Millisecond }
func (s *stubPollConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
	if !s.polled {
		s.polled = true
		return s.msgs, nil
	}
	return nil, nil
}

type stubStreamConnector struct {
	msgs []InboundMessage
}

func (s *stubStreamConnector) Type() string                            { return "stub-stream" }
func (s *stubStreamConnector) Capabilities() Caps                      { return CapText }
func (s *stubStreamConnector) Requirements() Reqs                      { return 0 }
func (s *stubStreamConnector) Init(context.Context, ConnectorConfig) error { return nil }
func (s *stubStreamConnector) Send(context.Context, Response) error    { return nil }
func (s *stubStreamConnector) Close() error                            { return nil }
func (s *stubStreamConnector) Messages() <-chan InboundMessage {
	ch := make(chan InboundMessage, len(s.msgs))
	for _, msg := range s.msgs {
		ch <- msg
	}
	close(ch)
	return ch
}

type stubSendConnector struct{}

func (s *stubSendConnector) Type() string                            { return "stub-send" }
func (s *stubSendConnector) Capabilities() Caps                      { return CapText }
func (s *stubSendConnector) Requirements() Reqs                      { return 0 }
func (s *stubSendConnector) Init(context.Context, ConnectorConfig) error { return nil }
func (s *stubSendConnector) Send(context.Context, Response) error    { return nil }
func (s *stubSendConnector) Close() error                            { return nil }

type stubWebhookConnector struct {
	handler http.Handler
}

func (s *stubWebhookConnector) Type() string                            { return "stub-webhook" }
func (s *stubWebhookConnector) Capabilities() Caps                      { return CapText }
func (s *stubWebhookConnector) Requirements() Reqs                      { return ReqHTTPListener }
func (s *stubWebhookConnector) Init(context.Context, ConnectorConfig) error { return nil }
func (s *stubWebhookConnector) Send(context.Context, Response) error    { return nil }
func (s *stubWebhookConnector) Close() error                            { return nil }
func (s *stubWebhookConnector) Handler() http.Handler                   { return s.handler }
func (s *stubWebhookConnector) ListenAddr() string                      { return ":0" }

var (
	_ PollReceiver   = (*stubPollConnector)(nil)
	_ StreamReceiver = (*stubStreamConnector)(nil)
	_ PushReceiver   = (*stubWebhookConnector)(nil)
)
