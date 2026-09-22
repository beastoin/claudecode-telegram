package forge

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func startTestSocket(t *testing.T, socketPath string, handler http.HandlerFunc) *http.Server {
	t.Helper()
	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatalf("listen on %s: %v", socketPath, err)
	}
	srv := &http.Server{Handler: handler}
	go srv.Serve(ln)
	t.Cleanup(func() {
		srv.Close()
		os.Remove(socketPath)
	})
	return srv
}

func TestParseEmitArgs_RequiresEvent(t *testing.T) {
	t.Parallel()
	_, err := ParseEmitArgs([]string{"--worker", "mon"})
	if err == nil {
		t.Fatal("expected error for missing --event")
	}
}

func TestParseEmitArgs_RequiresWorkerName(t *testing.T) {
	t.Parallel()
	os.Unsetenv("FORGE_WORKER_NAME")
	_, err := ParseEmitArgs([]string{"--event", "stop"})
	if err == nil {
		t.Fatal("expected error for missing worker name")
	}
}

func TestParseEmitArgs_DefaultsFromEnv(t *testing.T) {
	t.Setenv("FORGE_WORKER_NAME", "testworker")
	opts, err := ParseEmitArgs([]string{"--event", "stop"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if opts.WorkerName != "testworker" {
		t.Errorf("WorkerName = %q, want testworker", opts.WorkerName)
	}
	if opts.SocketPath != "/tmp/forge-testworker.sock" {
		t.Errorf("SocketPath = %q, want /tmp/forge-testworker.sock", opts.SocketPath)
	}
}

func TestParseEmitArgs_ExplicitSocket(t *testing.T) {
	t.Parallel()
	opts, err := ParseEmitArgs([]string{"--event", "stop", "--worker", "mon", "--socket", "/custom/path.sock"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if opts.SocketPath != "/custom/path.sock" {
		t.Errorf("SocketPath = %q, want /custom/path.sock", opts.SocketPath)
	}
}

func TestRunEmit_ReadsPayloadFile(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	payloadFile := filepath.Join(dir, "payload.txt")
	if err := os.WriteFile(payloadFile, []byte("hello from hook"), 0o644); err != nil {
		t.Fatal(err)
	}

	socketPath := filepath.Join(dir, "forge.sock")
	var received Response
	srv := startTestSocket(t, socketPath, func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		w.WriteHeader(200)
		w.Write([]byte("ok"))
	})
	defer srv.Close()

	err := RunEmit(EmitOptions{
		Event:       "stop",
		PayloadFile: payloadFile,
		WorkerName:  "test",
		SocketPath:  socketPath,
	}, nil)
	if err != nil {
		t.Fatalf("RunEmit error: %v", err)
	}
	if received.Text != "hello from hook" {
		t.Errorf("received text = %q, want 'hello from hook'", received.Text)
	}
}

func TestRunEmit_ReadsStdin(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	socketPath := filepath.Join(dir, "forge.sock")
	var received Response
	srv := startTestSocket(t, socketPath, func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&received)
		w.WriteHeader(200)
		w.Write([]byte("ok"))
	})
	defer srv.Close()

	err := RunEmit(EmitOptions{
		Event:      "stop",
		WorkerName: "test",
		SocketPath: socketPath,
	}, strings.NewReader("stdin payload"))
	if err != nil {
		t.Fatalf("RunEmit error: %v", err)
	}
	if received.Text != "stdin payload" {
		t.Errorf("received text = %q, want 'stdin payload'", received.Text)
	}
}

func TestRunEmit_EmptyPayloadIsNoop(t *testing.T) {
	t.Parallel()
	err := RunEmit(EmitOptions{
		Event:      "stop",
		WorkerName: "test",
		SocketPath: "/nonexistent.sock",
	}, strings.NewReader("   \n  "))
	if err != nil {
		t.Fatalf("expected no error for empty payload, got: %v", err)
	}
}

func TestRunEmit_ErrorOnMissingSocket(t *testing.T) {
	t.Parallel()
	err := RunEmit(EmitOptions{
		Event:      "stop",
		WorkerName: "test",
		SocketPath: "/tmp/forge-nonexistent-test.sock",
	}, strings.NewReader("hello"))
	if err == nil {
		t.Fatal("expected error when socket doesn't exist")
	}
}

func TestIntegration_EmitToHookListener(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	socketPath := filepath.Join(dir, "forge-test.sock")

	connector := &captureConnector{}
	hl := NewHookListener(socketPath, connector)
	if err := hl.Start(); err != nil {
		t.Fatalf("HookListener.Start: %v", err)
	}
	defer hl.Stop()

	err := RunEmit(EmitOptions{
		Event:      "stop",
		WorkerName: "test",
		SocketPath: socketPath,
	}, strings.NewReader("integration test output"))
	if err != nil {
		t.Fatalf("RunEmit: %v", err)
	}

	if connector.lastResponse.Text != "integration test output" {
		t.Errorf("connector received %q, want 'integration test output'", connector.lastResponse.Text)
	}
}

type captureConnector struct {
	lastResponse Response
	mu           sync.Mutex
}

func (c *captureConnector) Type() string                                  { return "capture" }
func (c *captureConnector) Init(_ context.Context, _ ConnectorConfig) error { return nil }
func (c *captureConnector) Send(_ context.Context, resp Response) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lastResponse = resp
	return nil
}
func (c *captureConnector) Capabilities() Caps { return CapText }
func (c *captureConnector) Requirements() Reqs { return 0 }
func (c *captureConnector) Close() error       { return nil }

func TestRunEmit_ErrorOnServerFailure(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	socketPath := filepath.Join(dir, "forge.sock")
	srv := startTestSocket(t, socketPath, func(w http.ResponseWriter, r *http.Request) {
		io.ReadAll(r.Body)
		http.Error(w, "connector offline", 503)
	})
	defer srv.Close()

	err := RunEmit(EmitOptions{
		Event:      "stop",
		WorkerName: "test",
		SocketPath: socketPath,
	}, strings.NewReader("hello"))
	if err == nil {
		t.Fatal("expected error on 503")
	}
	if !strings.Contains(err.Error(), "503") {
		t.Errorf("error = %v, want mention of 503", err)
	}
}
