package forge

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	pb "github.com/beastoin/claudecode-telegram/forge/proto/workerforge"
	"google.golang.org/grpc"
)

func TestTransport_InterfaceContract(t *testing.T) {
	t.Parallel()

	var transport Transport = transportContractStub{}

	if err := transport.Connect(context.Background(), "http://bridge"); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	if _, err := transport.Register(context.Background(), RegisterRequest{Name: "mon"}); err != nil {
		t.Fatalf("Register() error = %v", err)
	}
	if err := transport.SendMessage(context.Background(), WorkerMessage{Type: "status"}); err != nil {
		t.Fatalf("SendMessage() error = %v", err)
	}
	if _, err := transport.ReceiveMessage(context.Background()); err != nil {
		t.Fatalf("ReceiveMessage() error = %v", err)
	}
	if err := transport.StreamJSONL(context.Background(), JSONLChunk{StreamID: "stream-1"}); err != nil {
		t.Fatalf("StreamJSONL() error = %v", err)
	}
	if _, err := transport.PullKnowledge(context.Background(), "mon", "memory"); err != nil {
		t.Fatalf("PullKnowledge() error = %v", err)
	}
	if _, _, _, _, err := transport.CheckUpgrade(context.Background(), "mon", "1.0.0"); err != nil {
		t.Fatalf("CheckUpgrade() error = %v", err)
	}
	if err := transport.Heartbeat(context.Background()); err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}
	if err := transport.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
}

func TestStubTransport_RegisterReturnsOK(t *testing.T) {
	t.Parallel()

	transport := &StubTransport{}

	if err := transport.Connect(context.Background(), "http://bridge"); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}

	response, err := transport.Register(context.Background(), RegisterRequest{
		Name:    "mon",
		Version: "1.0.0",
	})
	if err != nil {
		t.Fatalf("Register() error = %v", err)
	}

	if !response.OK {
		t.Fatalf("Register() OK = false, want true")
	}
	if transport.BridgeURL != "http://bridge" {
		t.Fatalf("transport.BridgeURL = %q, want %q", transport.BridgeURL, "http://bridge")
	}
	if transport.LastRegister.Name != "mon" {
		t.Fatalf("transport.LastRegister.Name = %q, want %q", transport.LastRegister.Name, "mon")
	}
}

func TestGRPCTransport_ImplementsInterface(t *testing.T) {
	t.Parallel()

	var _ Transport = &GRPCTransport{}
}

func TestGRPCTransport_ConnectAndRegister(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	resp, err := transport.Register(context.Background(), RegisterRequest{
		Name:    "mon",
		Host:    "triassic-4",
		Version: "1.0.0",
		Tools:   map[string]string{"claude": "2.1.114"},
	})
	if err != nil {
		t.Fatalf("Register() error = %v", err)
	}
	if !resp.OK {
		t.Fatal("Register() OK = false, want true")
	}
	if transport.LastRegister.Name != "mon" {
		t.Fatalf("LastRegister.Name = %q, want %q", transport.LastRegister.Name, "mon")
	}
	if transport.WorkerName != "mon" {
		t.Fatalf("WorkerName = %q, want %q", transport.WorkerName, "mon")
	}
}

func TestGRPCTransport_Heartbeat(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	transport.WorkerName = "mon"
	if err := transport.Heartbeat(context.Background()); err != nil {
		t.Fatalf("Heartbeat() error = %v", err)
	}
}

func TestGRPCTransport_SendAndReceiveMessage(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	if err := transport.SendMessage(context.Background(), WorkerMessage{
		Type: "status",
		Text: "hello bridge",
	}); err != nil {
		t.Fatalf("SendMessage() error = %v", err)
	}

	msg, err := transport.ReceiveMessage(context.Background())
	if err != nil {
		t.Fatalf("ReceiveMessage() error = %v", err)
	}
	if msg.Type != "echo" {
		t.Fatalf("ReceiveMessage().Type = %q, want %q", msg.Type, "echo")
	}
	if msg.Text != "hello bridge" {
		t.Fatalf("ReceiveMessage().Text = %q, want %q", msg.Text, "hello bridge")
	}
}

func TestGRPCTransport_ConnectParsesGRPCScheme(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), "grpc://"+addr); err != nil {
		t.Fatalf("Connect(grpc://...) error = %v", err)
	}
	defer transport.Close()

	if transport.BridgeURL != "grpc://"+addr {
		t.Fatalf("BridgeURL = %q, want %q", transport.BridgeURL, "grpc://"+addr)
	}
}

func TestGRPCTransport_ConnectFailsOnUnhealthyServer(t *testing.T) {
	t.Parallel()

	transport := &GRPCTransport{}
	err := transport.Connect(context.Background(), "localhost:1")
	if err == nil {
		transport.Close()
		t.Fatal("Connect() to dead server should fail")
	}
}

func TestGRPCTransport_CloseIsIdempotent(t *testing.T) {
	t.Parallel()

	transport := &GRPCTransport{}
	if err := transport.Close(); err != nil {
		t.Fatalf("Close() on unconnected transport error = %v", err)
	}
	if err := transport.Close(); err != nil {
		t.Fatalf("double Close() error = %v", err)
	}
}

func TestNewTransport_SelectsGRPC(t *testing.T) {
	t.Parallel()

	transport := NewTransport("grpc://localhost:50051")
	if _, ok := transport.(*GRPCTransport); !ok {
		t.Fatalf("NewTransport(grpc://...) returned %T, want *GRPCTransport", transport)
	}
}

func TestNewTransport_SelectsHTTP(t *testing.T) {
	t.Parallel()

	transport := NewTransport("http://localhost:8271")
	if _, ok := transport.(*HTTPTransport); !ok {
		t.Fatalf("NewTransport(http://...) returned %T, want *HTTPTransport", transport)
	}
}

func TestNewTransport_DefaultsToHTTP(t *testing.T) {
	t.Parallel()

	transport := NewTransport("")
	if _, ok := transport.(*HTTPTransport); !ok {
		t.Fatalf("NewTransport('') returned %T, want *HTTPTransport", transport)
	}
}

func TestGRPCTransport_StreamJSONL(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	if err := transport.StreamJSONL(context.Background(), JSONLChunk{
		StreamID: "test-stream",
		Data:     []byte(`{"event":"test"}`),
		Final:    true,
	}); err != nil {
		t.Fatalf("StreamJSONL() error = %v", err)
	}
}

func TestGRPCTransport_PullKnowledge(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	got, err := transport.PullKnowledge(context.Background(), "mon", "memory")
	if err != nil {
		t.Fatalf("PullKnowledge() error = %v", err)
	}
	if string(got) != "knowledge:mon:memory" {
		t.Fatalf("PullKnowledge() = %q, want %q", got, "knowledge:mon:memory")
	}
}

func TestGRPCTransport_CheckUpgrade(t *testing.T) {
	t.Parallel()

	srv, addr := startTestGRPCServer(t)
	defer srv.Stop()

	transport := &GRPCTransport{}
	if err := transport.Connect(context.Background(), addr); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	defer transport.Close()

	available, version, url, checksum, err := transport.CheckUpgrade(context.Background(), "mon", "1.0.0")
	if err != nil {
		t.Fatalf("CheckUpgrade() error = %v", err)
	}
	if !available {
		t.Fatal("CheckUpgrade() available = false, want true")
	}
	if version != "1.2.3" {
		t.Fatalf("CheckUpgrade() version = %q, want %q", version, "1.2.3")
	}
	if url != "" {
		t.Fatalf("CheckUpgrade() url = %q, want empty URL because proto has no url field", url)
	}
	if checksum != "abc123" {
		t.Fatalf("CheckUpgrade() checksum = %q, want %q", checksum, "abc123")
	}
}

func TestGRPCTransport_MethodsFailWhenDisconnected(t *testing.T) {
	t.Parallel()

	transport := &GRPCTransport{}

	if _, err := transport.Register(context.Background(), RegisterRequest{Name: "x"}); err == nil {
		t.Fatal("Register() on disconnected transport should fail")
	}
	if err := transport.Heartbeat(context.Background()); err == nil {
		t.Fatal("Heartbeat() on disconnected transport should fail")
	}
	if err := transport.StreamJSONL(context.Background(), JSONLChunk{}); err == nil {
		t.Fatal("StreamJSONL() on disconnected transport should fail")
	}
	if _, err := transport.PullKnowledge(context.Background(), "x", "memory"); err == nil {
		t.Fatal("PullKnowledge() on disconnected transport should fail")
	}
	if _, _, _, _, err := transport.CheckUpgrade(context.Background(), "x", "1.0.0"); err == nil {
		t.Fatal("CheckUpgrade() on disconnected transport should fail")
	}
}

func TestTransport_ReconnectsWithBackoff(t *testing.T) {
	t.Parallel()

	base := &failingConnectTransport{failuresRemaining: 2}
	var sleeps []time.Duration

	transport := &ReconnectTransport{
		Base:        base,
		BaseBackoff: 10 * time.Millisecond,
		MaxBackoff:  40 * time.Millisecond,
		Sleep: func(delay time.Duration) {
			sleeps = append(sleeps, delay)
		},
	}

	if err := transport.Connect(context.Background(), "http://bridge"); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}

	if base.connectCalls != 3 {
		t.Fatalf("base.connectCalls = %d, want 3", base.connectCalls)
	}
	wantSleeps := []time.Duration{10 * time.Millisecond, 20 * time.Millisecond}
	if len(sleeps) != len(wantSleeps) {
		t.Fatalf("len(sleeps) = %d, want %d (%v)", len(sleeps), len(wantSleeps), sleeps)
	}
	for i, want := range wantSleeps {
		if sleeps[i] != want {
			t.Fatalf("sleeps[%d] = %s, want %s", i, sleeps[i], want)
		}
	}
}

func TestHTTPTransport_ConnectChecksBridgeHealth(t *testing.T) {
	t.Parallel()

	var hits int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			hits++
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	transport := &HTTPTransport{}
	if err := transport.Connect(context.Background(), server.URL); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}

	if hits != 1 {
		t.Fatalf("root hits = %d, want 1", hits)
	}
	if transport.BridgeURL != server.URL {
		t.Fatalf("transport.BridgeURL = %q, want %q", transport.BridgeURL, server.URL)
	}
}

func TestHTTPTransport_ConnectRespectsContextDeadline(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()

	err := (&HTTPTransport{}).Connect(ctx, server.URL)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Connect() error = %v, want context deadline exceeded", err)
	}
}

func TestHTTPTransport_RegisterPostsFullRegisterRequestJSON(t *testing.T) {
	t.Parallel()

	var gotMethod string
	var gotPath string
	var payload RegisterRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.RequestURI()
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("io.ReadAll() error = %v", err)
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatalf("json.Unmarshal() error = %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL}
	if _, err := transport.Register(context.Background(), RegisterRequest{
		Name:    "mon",
		Host:    "triassic-4",
		Version: "1.0.0",
		Tools:   map[string]string{"claude": "2.1.114"},
	}); err != nil {
		t.Fatalf("Register() error = %v", err)
	}

	if gotMethod != http.MethodPost {
		t.Fatalf("request method = %q, want %q", gotMethod, http.MethodPost)
	}
	if gotPath != "/register" {
		t.Fatalf("request URI = %q, want /register", gotPath)
	}
	if payload.Name != "mon" {
		t.Fatalf("payload.Name = %q, want mon", payload.Name)
	}
	if payload.Host != "triassic-4" {
		t.Fatalf("payload.Host = %q, want triassic-4", payload.Host)
	}
	if payload.Version != "1.0.0" {
		t.Fatalf("payload.Version = %q, want 1.0.0", payload.Version)
	}
	if got := payload.Tools["claude"]; got != "2.1.114" {
		t.Fatalf("payload.Tools[claude] = %q, want 2.1.114", got)
	}
}

func TestHTTPTransport_SendMessagePostsResponsePayload(t *testing.T) {
	t.Parallel()

	var gotPath string
	var payload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("io.ReadAll() error = %v", err)
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatalf("json.Unmarshal() error = %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL}
	if err := transport.SendMessage(context.Background(), WorkerMessage{Text: "hello"}); err != nil {
		t.Fatalf("SendMessage() error = %v", err)
	}

	if gotPath != "/response" {
		t.Fatalf("request path = %q, want /response", gotPath)
	}
	if payload["text"] != "hello" {
		t.Fatalf("payload[text] = %q, want hello", payload["text"])
	}
	if payload["source"] != "worker-forge" {
		t.Fatalf("payload[source] = %q, want worker-forge", payload["source"])
	}
}

func TestHTTPTransport_SendMessageIncludesWorkerMessageType(t *testing.T) {
	t.Parallel()

	var payload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("io.ReadAll() error = %v", err)
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatalf("json.Unmarshal() error = %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL}
	if err := transport.SendMessage(context.Background(), WorkerMessage{
		Type: "response",
		Text: "hello",
	}); err != nil {
		t.Fatalf("SendMessage() error = %v", err)
	}

	if payload["type"] != "response" {
		t.Fatalf("payload[type] = %q, want response", payload["type"])
	}
}

func TestHTTPTransport_ReceiveMessageReturnsUnsupported(t *testing.T) {
	t.Parallel()

	_, err := (&HTTPTransport{}).ReceiveMessage(context.Background())
	if !errors.Is(err, ErrUnsupported) {
		t.Fatalf("ReceiveMessage() error = %v, want ErrUnsupported", err)
	}
}

func TestHTTPTransport_RegisterFailsOnNon200(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL}
	_, err := transport.Register(context.Background(), RegisterRequest{Name: "mon"})
	if err == nil {
		t.Fatal("Register() on 503 should fail")
	}
	if !strings.Contains(err.Error(), "503") {
		t.Fatalf("Register() error = %v, want to contain 503", err)
	}
}

func TestHTTPTransport_SendMessageFailsOnNon200(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL}
	err := transport.SendMessage(context.Background(), WorkerMessage{Text: "hello"})
	if err == nil {
		t.Fatal("SendMessage() on 502 should fail")
	}
	if !strings.Contains(err.Error(), "502") {
		t.Fatalf("SendMessage() error = %v, want to contain 502", err)
	}
}

func TestHTTPTransport_HeartbeatFailsOnNon200(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL}
	err := transport.Heartbeat(context.Background())
	if err == nil {
		t.Fatal("Heartbeat() on 500 should fail")
	}
	if !strings.Contains(err.Error(), "500") {
		t.Fatalf("Heartbeat() error = %v, want to contain 500", err)
	}
}

func TestHTTPTransport_ConnectFailsOnEmptyURL(t *testing.T) {
	t.Parallel()

	err := (&HTTPTransport{}).Connect(context.Background(), "")
	if err == nil {
		t.Fatal("Connect('') should fail")
	}
	if !strings.Contains(err.Error(), "bridge URL is required") {
		t.Fatalf("Connect('') error = %v, want 'bridge URL is required'", err)
	}
}

func TestHTTPTransport_ConnectFailsOnNon200Health(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	err := (&HTTPTransport{}).Connect(context.Background(), server.URL)
	if err == nil {
		t.Fatal("Connect() to unhealthy bridge should fail")
	}
	if !strings.Contains(err.Error(), "health check failed") {
		t.Fatalf("Connect() error = %v, want 'health check failed'", err)
	}
}

func TestHTTPTransport_MethodsFailWhenDisconnected(t *testing.T) {
	t.Parallel()

	transport := &HTTPTransport{}

	if _, err := transport.Register(context.Background(), RegisterRequest{Name: "x"}); err == nil {
		t.Fatal("Register() on disconnected HTTP transport should fail")
	}
	if err := transport.SendMessage(context.Background(), WorkerMessage{Text: "x"}); err == nil {
		t.Fatal("SendMessage() on disconnected HTTP transport should fail")
	}
	if err := transport.Heartbeat(context.Background()); err == nil {
		t.Fatal("Heartbeat() on disconnected HTTP transport should fail")
	}
}

func TestHTTPTransport_SendMessageIncludesCustomSource(t *testing.T) {
	t.Parallel()

	var payload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &payload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	transport := &HTTPTransport{BridgeURL: server.URL, Source: "mon"}
	if err := transport.SendMessage(context.Background(), WorkerMessage{Text: "hello"}); err != nil {
		t.Fatalf("SendMessage() error = %v", err)
	}

	if payload["source"] != "mon" {
		t.Fatalf("payload[source] = %q, want mon", payload["source"])
	}
}

func TestHTTPTransport_UnsupportedMethods(t *testing.T) {
	t.Parallel()

	transport := &HTTPTransport{}

	if err := transport.StreamJSONL(context.Background(), JSONLChunk{}); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("StreamJSONL() error = %v, want ErrUnsupported", err)
	}
	if _, err := transport.PullKnowledge(context.Background(), "x", "memory"); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("PullKnowledge() error = %v, want ErrUnsupported", err)
	}
	if _, _, _, _, err := transport.CheckUpgrade(context.Background(), "x", "1.0.0"); !errors.Is(err, ErrUnsupported) {
		t.Fatalf("CheckUpgrade() error = %v, want ErrUnsupported", err)
	}
}

func TestReconnectTransport_HeartbeatReconnectsOnFailure(t *testing.T) {
	t.Parallel()

	base := &heartbeatFailTransport{heartbeatErr: errBoom}
	transport := &ReconnectTransport{
		Base:        base,
		BaseBackoff: time.Millisecond,
		Sleep:       func(time.Duration) {},
	}

	if err := transport.Connect(context.Background(), "http://bridge"); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}

	initialConnects := base.connectCalls

	if err := transport.Heartbeat(context.Background()); err != nil {
		t.Fatalf("Heartbeat() error = %v, want reconnect success", err)
	}

	if base.heartbeatCalls != 1 {
		t.Fatalf("base.heartbeatCalls = %d, want 1", base.heartbeatCalls)
	}
	if base.connectCalls <= initialConnects {
		t.Fatalf("base.connectCalls = %d, want > %d (reconnect)", base.connectCalls, initialConnects)
	}
}

func TestReconnectTransport_HeartbeatFailsWithoutPriorConnect(t *testing.T) {
	t.Parallel()

	base := &heartbeatFailTransport{heartbeatErr: errBoom}
	transport := &ReconnectTransport{
		Base:  base,
		Sleep: func(time.Duration) {},
	}

	err := transport.Heartbeat(context.Background())
	if err == nil {
		t.Fatal("Heartbeat() without prior Connect should fail")
	}
	if !strings.Contains(err.Error(), "before initial connect") {
		t.Fatalf("Heartbeat() error = %v, want 'before initial connect'", err)
	}
}

func TestReconnectTransport_ConnectRespectsContextCancel(t *testing.T) {
	t.Parallel()

	base := &failingConnectTransport{failuresRemaining: 1000}
	transport := &ReconnectTransport{
		Base:        base,
		BaseBackoff: time.Millisecond,
		Sleep:       func(time.Duration) {},
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := transport.Connect(ctx, "http://bridge")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Connect() error = %v, want context.Canceled", err)
	}
}

func TestApp_RunWithTransport(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	runtime := &stubRuntime{}
	transport := &appTransportSpy{runtime: runtime}
	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V":                       {ExitCode: 0, Stdout: "tmux 3.4"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"CLAUDE_CONFIG_DIR": {
				Source:   "default",
				Default:  "$HOME/.claude",
				Required: true,
			},
			"BRIDGE_URL": {
				Source:   "flag",
				Default:  "http://default",
				Required: true,
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
	}

	app := App{
		Manifest:    manifest,
		Runner:      runner,
		Runtime:     runtime,
		Transport:   transport,
		HookManager: HookManager{},
		GOOS:        "linux",
	}

	if err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{
				"HOME": root,
			},
			Flags: map[string]string{
				"BRIDGE_URL": "http://bridge",
			},
		},
	}); err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}

	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
	wantEvents := []string{"connect", "register"}
	if len(transport.events) != len(wantEvents) {
		t.Fatalf("transport.events = %#v, want %#v", transport.events, wantEvents)
	}
	for i, want := range wantEvents {
		if transport.events[i] != want {
			t.Fatalf("transport.events[%d] = %q, want %q", i, transport.events[i], want)
		}
	}
	if transport.bridgeURL != "http://bridge" {
		t.Fatalf("transport.bridgeURL = %q, want %q", transport.bridgeURL, "http://bridge")
	}
	if transport.register.Name != "mon" {
		t.Fatalf("transport.register.Name = %q, want %q", transport.register.Name, "mon")
	}
	if transport.register.Version != "1.0.0" {
		t.Fatalf("transport.register.Version = %q, want %q", transport.register.Version, "1.0.0")
	}
	if got := transport.register.Tools["tmux"]; got != "tmux 3.4" {
		t.Fatalf("transport.register.Tools[tmux] = %q, want %q", got, "tmux 3.4")
	}
}

func TestAppRun_FullWithTransport(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, ".claude")
	runtime := &stubRuntime{}
	transport := &StubTransport{}
	runner := &stubRunner{
		results: map[string]RunResult{
			"tmux -V":                       {ExitCode: 0, Stdout: "tmux 3.4"},
			"curl -sf http://bridge/health": {ExitCode: 0},
		},
	}
	manifest := &Manifest{
		Name:    "mon",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME": {
				Source:   "env",
				Required: true,
			},
			"CLAUDE_CONFIG_DIR": {
				Source:   "default",
				Default:  "$HOME/.claude",
				Required: true,
			},
			"BRIDGE_URL": {
				Source:   "flag",
				Default:  "http://default",
				Required: true,
			},
		},
		Dirs: []string{
			"$CLAUDE_CONFIG_DIR/hooks",
		},
		Files: []FileSpec{
			{
				Source: "hooks/send-to-telegram.sh",
				Dest:   "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
			},
		},
		Tools: []ToolSpec{
			{
				Name:     "tmux",
				Check:    "tmux -V",
				Required: true,
			},
		},
		Readiness: []ReadinessCheck{
			{
				Name:     "bridge",
				Check:    "curl -sf $BRIDGE_URL/health",
				Expect:   "exit 0",
				Required: true,
			},
		},
		Hooks: []HookSpec{
			{
				Event:   "Stop",
				Command: "$CLAUDE_CONFIG_DIR/hooks/send-to-telegram.sh",
			},
		},
	}

	app := App{
		Manifest: manifest,
		Source: MapFileSource{
			"hooks/send-to-telegram.sh": []byte("#!/bin/sh\necho sent\n"),
		},
		Runner:      runner,
		Runtime:     runtime,
		Transport:   transport,
		HookManager: HookManager{},
		GOOS:        "linux",
	}

	if err := app.Run(RunOptions{
		Resolve: ResolveOptions{
			Env: map[string]string{
				"HOME": root,
			},
			Flags: map[string]string{
				"BRIDGE_URL": "http://bridge",
			},
		},
	}); err != nil {
		t.Fatalf("app.Run() error = %v", err)
	}

	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
	if !transport.Connected {
		t.Fatal("transport.Connected = false, want true")
	}
	if transport.BridgeURL != "http://bridge" {
		t.Fatalf("transport.BridgeURL = %q, want %q", transport.BridgeURL, "http://bridge")
	}
	if transport.LastRegister.Name != "mon" {
		t.Fatalf("transport.LastRegister.Name = %q, want %q", transport.LastRegister.Name, "mon")
	}
	if _, err := os.Stat(filepath.Join(configDir, "hooks", "send-to-telegram.sh")); err != nil {
		t.Fatalf("expected extracted hook script, stat error = %v", err)
	}
	if _, err := os.Stat(filepath.Join(configDir, "settings.json")); err != nil {
		t.Fatalf("expected settings.json, stat error = %v", err)
	}
}

type transportContractStub struct{}

type failingConnectTransport struct {
	failuresRemaining int
	connectCalls      int
}

type appTransportSpy struct {
	runtime   *stubRuntime
	events    []string
	bridgeURL string
	register  RegisterRequest
}

func (a *appTransportSpy) Connect(_ context.Context, bridgeURL string) error {
	if a.runtime.startCalls == 0 {
		return errors.New("runtime must start before transport connect")
	}
	a.events = append(a.events, "connect")
	a.bridgeURL = bridgeURL
	return nil
}

func (a *appTransportSpy) Register(_ context.Context, req RegisterRequest) (RegisterResponse, error) {
	a.events = append(a.events, "register")
	a.register = req
	return RegisterResponse{OK: true}, nil
}

func (a *appTransportSpy) SendMessage(context.Context, WorkerMessage) error {
	return nil
}

func (a *appTransportSpy) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, nil
}

func (a *appTransportSpy) StreamJSONL(context.Context, JSONLChunk) error {
	return nil
}

func (a *appTransportSpy) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, nil
}

func (a *appTransportSpy) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", nil
}

func (a *appTransportSpy) Heartbeat(context.Context) error {
	return nil
}

func (a *appTransportSpy) Close() error {
	return nil
}

func (f *failingConnectTransport) Connect(context.Context, string) error {
	f.connectCalls++
	if f.failuresRemaining > 0 {
		f.failuresRemaining--
		return errBoom
	}
	return nil
}

func (f *failingConnectTransport) Register(context.Context, RegisterRequest) (RegisterResponse, error) {
	return RegisterResponse{OK: true}, nil
}

func (f *failingConnectTransport) SendMessage(context.Context, WorkerMessage) error {
	return nil
}

func (f *failingConnectTransport) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, nil
}

func (f *failingConnectTransport) StreamJSONL(context.Context, JSONLChunk) error {
	return nil
}

func (f *failingConnectTransport) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, nil
}

func (f *failingConnectTransport) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", nil
}

func (f *failingConnectTransport) Heartbeat(context.Context) error {
	return nil
}

func (f *failingConnectTransport) Close() error {
	return nil
}

func (transportContractStub) Connect(context.Context, string) error {
	return nil
}

func (transportContractStub) Register(context.Context, RegisterRequest) (RegisterResponse, error) {
	return RegisterResponse{OK: true}, nil
}

func (transportContractStub) SendMessage(context.Context, WorkerMessage) error {
	return nil
}

func (transportContractStub) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{Type: "message"}, nil
}

func (transportContractStub) StreamJSONL(context.Context, JSONLChunk) error {
	return nil
}

func (transportContractStub) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return []byte("knowledge"), nil
}

func (transportContractStub) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return true, "1.2.3", "https://bridge/1.2.3", "abc123", nil
}

func (transportContractStub) Heartbeat(context.Context) error {
	return nil
}

func (transportContractStub) Close() error {
	return nil
}

var _ io.Closer = transportContractStub{}

type testBridgeServer struct {
	pb.UnimplementedBridgeServer
}

func (s *testBridgeServer) Check(context.Context, *pb.HealthCheckRequest) (*pb.HealthCheckResponse, error) {
	return &pb.HealthCheckResponse{Status: pb.HealthCheckResponse_SERVING}, nil
}

func (s *testBridgeServer) Register(_ context.Context, req *pb.RegisterRequest) (*pb.RegisterResponse, error) {
	return &pb.RegisterResponse{Ok: true, Message: "registered " + req.Name}, nil
}

func (s *testBridgeServer) Heartbeat(_ context.Context, req *pb.HeartbeatRequest) (*pb.HeartbeatResponse, error) {
	return &pb.HeartbeatResponse{Ok: true}, nil
}

func (s *testBridgeServer) MessageStream(stream grpc.BidiStreamingServer[pb.WorkerMessage, pb.BridgeMessage]) error {
	for {
		msg, err := stream.Recv()
		if err != nil {
			return err
		}
		if err := stream.Send(&pb.BridgeMessage{
			Type: "echo",
			Text: msg.Text,
			From: "bridge",
		}); err != nil {
			return err
		}
	}
}

func (s *testBridgeServer) StreamJSONL(stream grpc.ClientStreamingServer[pb.JSONLChunk, pb.StreamAck]) error {
	var total int64
	for {
		chunk, err := stream.Recv()
		if err != nil {
			break
		}
		total += int64(len(chunk.Data))
		if chunk.Final {
			break
		}
	}
	return stream.SendAndClose(&pb.StreamAck{Ok: true, BytesReceived: total})
}

func (s *testBridgeServer) PullKnowledge(req *pb.KnowledgeRequest, stream grpc.ServerStreamingServer[pb.KnowledgeChunk]) error {
	if err := stream.Send(&pb.KnowledgeChunk{Data: []byte("knowledge:" + req.Name + ":")}); err != nil {
		return err
	}
	return stream.Send(&pb.KnowledgeChunk{Data: []byte(req.Type), Final: true})
}

func (s *testBridgeServer) CheckUpgrade(context.Context, *pb.UpgradeCheckRequest) (*pb.UpgradeCheckResponse, error) {
	return &pb.UpgradeCheckResponse{
		Available: true,
		Version:   "1.2.3",
		Checksum:  "abc123",
	}, nil
}

type heartbeatFailTransport struct {
	connectCalls   int
	heartbeatCalls int
	heartbeatErr   error
}

func (h *heartbeatFailTransport) Connect(context.Context, string) error {
	h.connectCalls++
	return nil
}

func (h *heartbeatFailTransport) Register(context.Context, RegisterRequest) (RegisterResponse, error) {
	return RegisterResponse{OK: true}, nil
}

func (h *heartbeatFailTransport) SendMessage(context.Context, WorkerMessage) error {
	return nil
}

func (h *heartbeatFailTransport) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, nil
}

func (h *heartbeatFailTransport) StreamJSONL(context.Context, JSONLChunk) error {
	return nil
}

func (h *heartbeatFailTransport) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, nil
}

func (h *heartbeatFailTransport) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", nil
}

func (h *heartbeatFailTransport) Heartbeat(context.Context) error {
	h.heartbeatCalls++
	return h.heartbeatErr
}

func (h *heartbeatFailTransport) Close() error {
	return nil
}

func startTestGRPCServer(t *testing.T) (*grpc.Server, string) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to listen: %v", err)
	}
	srv := grpc.NewServer()
	pb.RegisterBridgeServer(srv, &testBridgeServer{})
	go srv.Serve(lis)
	t.Cleanup(func() { srv.Stop() })
	return srv, lis.Addr().String()
}
