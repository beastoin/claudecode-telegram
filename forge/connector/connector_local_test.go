package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"
)

func eventually(t *testing.T, timeout time.Duration, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if check() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met within timeout")
}

func initLocalConnector(t *testing.T, config map[string]string) *LocalConnector {
	t.Helper()
	c := &LocalConnector{}
	if config == nil {
		config = map[string]string{}
	}
	if err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "test-local",
		Config:     config,
	}); err != nil {
		t.Fatalf("Init() error = %v", err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

func TestLocalConnector_Init_BindsRandomPort(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	if c.Port() <= 0 {
		t.Fatalf("Port() = %d, want > 0", c.Port())
	}
}

func TestLocalConnector_Init_BindsExplicitZeroPort(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, map[string]string{"LOCAL_PORT": "0"})

	if c.Port() <= 0 {
		t.Fatalf("Port() = %d, want > 0", c.Port())
	}
}

func TestLocalConnector_SendAndReceive(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	// POST /send
	url := fmt.Sprintf("http://127.0.0.1:%d/send", c.Port())
	resp, err := http.Post(url, "application/json",
		strings.NewReader(`{"text":"hello","from":"tester"}`))
	if err != nil {
		t.Fatalf("POST /send: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("POST /send status = %d, want 200", resp.StatusCode)
	}

	// Poll for the message
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	msgs, err := c.Poll(ctx)
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("Poll() returned %d messages, want 1", len(msgs))
	}
	if msgs[0].Text != "hello" {
		t.Fatalf("msgs[0].Text = %q, want %q", msgs[0].Text, "hello")
	}
	if msgs[0].From != "tester" {
		t.Fatalf("msgs[0].From = %q, want %q", msgs[0].From, "tester")
	}

	// Send a response
	if err := c.Send(context.Background(), Response{Text: "reply"}); err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	// GET /responses
	respURL := fmt.Sprintf("http://127.0.0.1:%d/responses", c.Port())
	httpResp, err := http.Get(respURL)
	if err != nil {
		t.Fatalf("GET /responses: %v", err)
	}
	defer httpResp.Body.Close()

	var responses []Response
	if err := json.NewDecoder(httpResp.Body).Decode(&responses); err != nil {
		t.Fatalf("decode responses: %v", err)
	}
	if len(responses) != 1 {
		t.Fatalf("responses = %d, want 1", len(responses))
	}
	if responses[0].Text != "reply" {
		t.Fatalf("responses[0].Text = %q, want %q", responses[0].Text, "reply")
	}
}

func TestLocalConnector_Health(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	url := fmt.Sprintf("http://127.0.0.1:%d/health", c.Port())
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET /health: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	var body map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["ok"] != true {
		t.Fatalf("ok = %v, want true", body["ok"])
	}
	if body["connector"] != "local" {
		t.Fatalf("connector = %v, want local", body["connector"])
	}
}

func TestLocalConnector_Send_MethodNotAllowed(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	url := fmt.Sprintf("http://127.0.0.1:%d/send", c.Port())
	resp, err := http.Get(url)
	if err != nil {
		t.Fatalf("GET /send: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", resp.StatusCode)
	}
}

func TestLocalConnector_Send_InvalidJSON(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	url := fmt.Sprintf("http://127.0.0.1:%d/send", c.Port())
	resp, err := http.Post(url, "application/json",
		strings.NewReader(`{invalid`))
	if err != nil {
		t.Fatalf("POST /send: %v", err)
	}
	resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
}

func TestLocalConnector_Poll_ReturnsNilOnTimeout(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	msgs, err := c.Poll(ctx)
	if err != nil && err != context.DeadlineExceeded {
		t.Fatalf("Poll() error = %v, want nil or deadline exceeded", err)
	}
	if len(msgs) != 0 {
		t.Fatalf("Poll() returned %d messages, want 0", len(msgs))
	}
}

func TestLocalConnector_Poll_ReturnsErrorOnCancelledContext(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := c.Poll(ctx)
	if err != context.Canceled {
		t.Fatalf("Poll() error = %v, want context.Canceled", err)
	}
}

func TestLocalConnector_Responses_EmptyInitially(t *testing.T) {
	t.Parallel()

	c := initLocalConnector(t, nil)

	resp := c.Responses()
	if len(resp) != 0 {
		t.Fatalf("Responses() = %d, want 0", len(resp))
	}
}

func TestLocalConnector_ConnectorHostIntegration(t *testing.T) {
	t.Parallel()

	connector := &LocalConnector{}
	rt := &connStubRuntime{}
	cfg := ConnectorConfig{
		WorkerName: "local-integration-test",
		Config:     map[string]string{},
	}

	if err := connector.Init(context.Background(), cfg); err != nil {
		t.Fatalf("Init() error = %v", err)
	}
	defer connector.Close()

	host := NewConnectorHost(connector, rt, cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() { done <- host.Run(ctx) }()

	// Wait for the connector to be listening
	eventually(t, 2*time.Second, func() bool { return connector.Port() > 0 })

	// POST /send
	url := fmt.Sprintf("http://127.0.0.1:%d/send", connector.Port())
	resp, err := http.Post(url, "application/json",
		strings.NewReader(`{"text":"integration test","from":"alice"}`))
	if err != nil {
		t.Fatalf("POST /send: %v", err)
	}
	resp.Body.Close()

	// Wait for the runtime to receive the formatted message
	eventually(t, 2*time.Second, func() bool {
		return len(rt.getSent()) >= 1
	})

	sent := rt.getSent()
	if sent[0] != "[alice] integration test" {
		t.Fatalf("runtime.sent[0] = %q, want %q", sent[0], "[alice] integration test")
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run() error = %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run() did not return after cancel")
	}

	host.Stop()
}
