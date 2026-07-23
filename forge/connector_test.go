package forge

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
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
	ext.IsExternal()
}

func TestBridgeConnectorIsTeamAware(t *testing.T) {
	c, _ := NewConnector("bridge")
	if _, ok := any(c).(TeamAware); !ok {
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
	t.Parallel()

	stub := &externalStub{}
	cfg := ConnectorConfig{WorkerName: "ext-test"}
	if err := stub.Init(context.Background(), cfg); err != nil {
		t.Fatalf("Init() error = %v", err)
	}

	host := NewConnectorHost(stub, nil, cfg)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	done := make(chan error, 1)
	go func() { done <- host.Run(ctx) }()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("host.Run() error = %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("host.Run() did not return after context cancellation")
	}

	if !stub.initCalled {
		t.Fatal("connector Init was not called")
	}
}

// ---------------------------------------------------------------------------
// BridgeConnector HTTP behavior tests (using Init for setup)
// ---------------------------------------------------------------------------

func TestBridgeConnector_Init_HealthChecksAndRegisters(t *testing.T) {
	t.Parallel()

	var healthHits int
	var registerHits int
	var registerPayload map[string]string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/":
			healthHits++
		case r.Method == http.MethodPost && r.URL.Path == "/register":
			registerHits++
			body, _ := io.ReadAll(r.Body)
			json.Unmarshal(body, &registerPayload)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "mon",
		BridgeURL:  server.URL,
	})
	if err != nil {
		t.Fatalf("Init() error = %v", err)
	}

	if healthHits != 1 {
		t.Fatalf("health check hits = %d, want 1", healthHits)
	}
	if registerHits != 1 {
		t.Fatalf("register hits = %d, want 1", registerHits)
	}
	if registerPayload["name"] != "mon" {
		t.Fatalf("register payload name = %q, want mon", registerPayload["name"])
	}
}

func TestBridgeConnector_Init_FallsBackToConfigBridgeURL(t *testing.T) {
	t.Parallel()

	var healthHits int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			healthHits++
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "mon",
		Config:     map[string]string{"BRIDGE_URL": server.URL},
	})
	if err != nil {
		t.Fatalf("Init() error = %v", err)
	}

	if healthHits != 1 {
		t.Fatalf("health check hits = %d, want 1 (fallback to Config BRIDGE_URL)", healthHits)
	}
}

func TestBridgeConnector_Send_PostsToResponseEndpoint(t *testing.T) {
	t.Parallel()

	var gotPath string
	var gotPayload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/response" {
			gotPath = r.URL.Path
			body, _ := io.ReadAll(r.Body)
			json.Unmarshal(body, &gotPayload)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "mon",
		BridgeURL:  server.URL,
	})

	if err := c.Send(context.Background(), Response{Text: "done"}); err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	if gotPath != "/response" {
		t.Fatalf("path = %q, want /response", gotPath)
	}
	if gotPayload["text"] != "done" {
		t.Fatalf("payload[text] = %q, want done", gotPayload["text"])
	}
	if gotPayload["source"] != "mon" {
		t.Fatalf("payload[source] = %q, want mon", gotPayload["source"])
	}
	if gotPayload["session"] != "mon" {
		t.Fatalf("payload[session] = %q, want mon", gotPayload["session"])
	}
}

func TestBridgeConnector_Send_FailsWithNoBridgeURL(t *testing.T) {
	t.Parallel()

	c := &BridgeConnector{}
	err := c.Send(context.Background(), Response{Text: "hello"})
	if err == nil {
		t.Fatal("Send() with no bridgeURL should return error")
	}
	if !strings.Contains(err.Error(), "not configured") {
		t.Fatalf("error = %v, want 'not configured'", err)
	}
}

func TestBridgeConnector_Send_FailsOnNon200(t *testing.T) {
	t.Parallel()

	reqCount := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqCount++
		if r.URL.Path == "/response" {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "mon",
		BridgeURL:  server.URL,
	})

	err := c.Send(context.Background(), Response{Text: "hello"})
	if err == nil {
		t.Fatal("Send() on 503 should return error")
	}
	if !strings.Contains(err.Error(), "HTTP 503") {
		t.Fatalf("error = %v, want to contain 'HTTP 503'", err)
	}
}

func TestBridgeConnector_DiscoverWorkers_ParsesResponse(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/workers" {
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`[{"name":"mon","send_example":"curl -X POST ..."},{"name":"luck","send_example":"curl -X POST /luck"}]`))
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "testworker",
		BridgeURL:  server.URL,
	})

	ta := any(c).(TeamAware)
	workers, err := ta.DiscoverWorkers(context.Background())
	if err != nil {
		t.Fatalf("DiscoverWorkers() error = %v", err)
	}
	if len(workers) != 2 {
		t.Fatalf("len(workers) = %d, want 2", len(workers))
	}
	if workers[0].Name != "mon" {
		t.Fatalf("workers[0].Name = %q, want mon", workers[0].Name)
	}
	if workers[1].Name != "luck" {
		t.Fatalf("workers[1].Name = %q, want luck", workers[1].Name)
	}
	if workers[0].SendExample != "curl -X POST ..." {
		t.Fatalf("workers[0].SendExample = %q, want 'curl -X POST ...'", workers[0].SendExample)
	}
}

func TestBridgeConnector_DiscoverWorkers_IncludesFromParam(t *testing.T) {
	t.Parallel()

	var gotQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/workers" {
			gotQuery = r.URL.RawQuery
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`[]`))
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "testworker",
		BridgeURL:  server.URL,
	})

	ta := any(c).(TeamAware)
	ta.DiscoverWorkers(context.Background())
	if !strings.Contains(gotQuery, "from=testworker") {
		t.Fatalf("query = %q, want to contain 'from=testworker'", gotQuery)
	}
}

func TestBridgeConnector_SendToWorker_PostsToSendEndpoint(t *testing.T) {
	t.Parallel()

	var gotPath string
	var gotPayload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/send" {
			gotPath = r.URL.Path
			body, _ := io.ReadAll(r.Body)
			json.Unmarshal(body, &gotPayload)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "lee",
		BridgeURL:  server.URL,
	})

	ta := any(c).(TeamAware)
	if err := ta.SendToWorker(context.Background(), "luck", "hello luck"); err != nil {
		t.Fatalf("SendToWorker() error = %v", err)
	}

	if gotPath != "/send" {
		t.Fatalf("path = %q, want /send", gotPath)
	}
	if gotPayload["from"] != "lee" {
		t.Fatalf("payload[from] = %q, want lee", gotPayload["from"])
	}
	if gotPayload["to"] != "luck" {
		t.Fatalf("payload[to] = %q, want luck", gotPayload["to"])
	}
	if gotPayload["text"] != "hello luck" {
		t.Fatalf("payload[text] = %q, want 'hello luck'", gotPayload["text"])
	}
}

func TestBridgeConnector_SendToWorker_FailsOnNon200(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/send" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "lee",
		BridgeURL:  server.URL,
	})

	ta := any(c).(TeamAware)
	err := ta.SendToWorker(context.Background(), "unknown", "hey")
	if err == nil {
		t.Fatal("SendToWorker() on 404 should return error")
	}
	if !strings.Contains(err.Error(), "HTTP 404") {
		t.Fatalf("error = %v, want to contain 'HTTP 404'", err)
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

	host.Run(ctx)

	if len(rt.getSent()) == 0 {
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

	host.Run(ctx)

	sent := rt.getSent()
	if len(sent) == 0 {
		t.Fatal("expected at least one message delivered to runtime")
	}
	if sent[0] != "hello from stream" {
		t.Errorf("got %q, want %q", sent[0], "hello from stream")
	}
}

func TestHookListenerStartStop(t *testing.T) {
	t.Parallel()

	stub := &stubSendConnector{}
	socketPath := filepath.Join(t.TempDir(), "test.sock")

	hl := NewHookListener(socketPath, stub)
	if err := hl.Start(); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer hl.Stop()

	if hl.SocketPath() != socketPath {
		t.Errorf("SocketPath() = %q, want %q", hl.SocketPath(), socketPath)
	}

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
// Test stubs
// ---------------------------------------------------------------------------

type externalStub struct {
	initCalled bool
}

func (s *externalStub) Type() string                                    { return "external-test" }
func (s *externalStub) Capabilities() Caps                              { return CapText }
func (s *externalStub) Requirements() Reqs                              { return 0 }
func (s *externalStub) Init(_ context.Context, _ ConnectorConfig) error { s.initCalled = true; return nil }
func (s *externalStub) Send(context.Context, Response) error            { return nil }
func (s *externalStub) Close() error                                    { return nil }
func (s *externalStub) IsExternal()                                     {}

type connStubRuntime struct {
	mu   sync.Mutex
	sent []string
}

func (s *connStubRuntime) Start() error          { return nil }
func (s *connStubRuntime) Send(msg string) error { s.mu.Lock(); s.sent = append(s.sent, msg); s.mu.Unlock(); return nil }
func (s *connStubRuntime) Health() error         { return nil }

func (s *connStubRuntime) getSent() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := make([]string, len(s.sent))
	copy(cp, s.sent)
	return cp
}

type stubPollConnector struct {
	msgs   []InboundMessage
	polled bool
}

func (s *stubPollConnector) Type() string                                    { return "stub-poll" }
func (s *stubPollConnector) Capabilities() Caps                              { return CapText }
func (s *stubPollConnector) Requirements() Reqs                              { return 0 }
func (s *stubPollConnector) Init(context.Context, ConnectorConfig) error     { return nil }
func (s *stubPollConnector) Send(context.Context, Response) error            { return nil }
func (s *stubPollConnector) Close() error                                    { return nil }
func (s *stubPollConnector) PollInterval() time.Duration                     { return 10 * time.Millisecond }
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

func (s *stubStreamConnector) Type() string                                    { return "stub-stream" }
func (s *stubStreamConnector) Capabilities() Caps                              { return CapText }
func (s *stubStreamConnector) Requirements() Reqs                              { return 0 }
func (s *stubStreamConnector) Init(context.Context, ConnectorConfig) error     { return nil }
func (s *stubStreamConnector) Send(context.Context, Response) error            { return nil }
func (s *stubStreamConnector) Close() error                                    { return nil }
func (s *stubStreamConnector) Messages() <-chan InboundMessage {
	ch := make(chan InboundMessage, len(s.msgs))
	for _, msg := range s.msgs {
		ch <- msg
	}
	close(ch)
	return ch
}

type stubSendConnector struct{}

func (s *stubSendConnector) Type() string                                    { return "stub-send" }
func (s *stubSendConnector) Capabilities() Caps                              { return CapText }
func (s *stubSendConnector) Requirements() Reqs                              { return 0 }
func (s *stubSendConnector) Init(context.Context, ConnectorConfig) error     { return nil }
func (s *stubSendConnector) Send(context.Context, Response) error            { return nil }
func (s *stubSendConnector) Close() error                                    { return nil }

type stubWebhookConnector struct {
	handler http.Handler
}

func (s *stubWebhookConnector) Type() string                                    { return "stub-webhook" }
func (s *stubWebhookConnector) Capabilities() Caps                              { return CapText }
func (s *stubWebhookConnector) Requirements() Reqs                              { return ReqHTTPListener }
func (s *stubWebhookConnector) Init(context.Context, ConnectorConfig) error     { return nil }
func (s *stubWebhookConnector) Send(context.Context, Response) error            { return nil }
func (s *stubWebhookConnector) Close() error                                    { return nil }
func (s *stubWebhookConnector) Handler() http.Handler                           { return s.handler }
func (s *stubWebhookConnector) ListenAddr() string                              { return ":0" }

var (
	_ PollReceiver   = (*stubPollConnector)(nil)
	_ StreamReceiver = (*stubStreamConnector)(nil)
	_ PushReceiver   = (*stubWebhookConnector)(nil)
)
