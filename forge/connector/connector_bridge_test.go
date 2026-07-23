package connector

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBridgeConnector_Send_IncludesSession(t *testing.T) {
	t.Parallel()

	var gotPayload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/response" {
			body, _ := io.ReadAll(r.Body)
			json.Unmarshal(body, &gotPayload)
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	b := &BridgeConnector{
		bridgeURL: server.URL,
		name:      "forge-mon",
		client:    &http.Client{},
	}

	err := b.Send(context.Background(), Response{Text: "hello"})
	if err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	if gotPayload["session"] != "forge-mon" {
		t.Errorf("session = %q, want %q", gotPayload["session"], "forge-mon")
	}
	if gotPayload["source"] != "forge-mon" {
		t.Errorf("source = %q, want %q", gotPayload["source"], "forge-mon")
	}
	if gotPayload["text"] != "hello" {
		t.Errorf("text = %q, want %q", gotPayload["text"], "hello")
	}
}

func TestBridgeConnector_Send_NoBridgeURL(t *testing.T) {
	t.Parallel()
	b := &BridgeConnector{name: "test", client: &http.Client{}}
	err := b.Send(context.Background(), Response{Text: "hello"})
	if err == nil || !strings.Contains(err.Error(), "bridge URL not configured") {
		t.Errorf("expected 'bridge URL not configured' error, got %v", err)
	}
}

func TestBridgeConnector_Send_NonOKStatus(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
	}))
	defer server.Close()

	b := &BridgeConnector{
		bridgeURL: server.URL,
		name:      "test",
		client:    &http.Client{},
	}

	err := b.Send(context.Background(), Response{Text: "hello"})
	if err == nil || !strings.Contains(err.Error(), "HTTP 400") {
		t.Errorf("expected HTTP 400 error, got %v", err)
	}
}

func TestBridgeConnector_Init_RegistersWorker(t *testing.T) {
	t.Parallel()

	var gotRegisterPayload map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/register" {
			body, _ := io.ReadAll(r.Body)
			json.Unmarshal(body, &gotRegisterPayload)
			resp := registerResponse{}
			resp.OK = true
			resp.Settings.TmuxPrefix = "claude-prod-"
			resp.Settings.TmuxSession = "claude-prod-forge-mon"
			data, _ := json.Marshal(resp)
			w.Write(data)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "forge-mon",
		BridgeURL:  server.URL,
	})
	if err != nil {
		t.Fatalf("Init() error = %v", err)
	}

	if gotRegisterPayload["name"] != "forge-mon" {
		t.Errorf("register name = %q, want %q", gotRegisterPayload["name"], "forge-mon")
	}
	if c.TmuxPrefix != "claude-prod-" {
		t.Errorf("TmuxPrefix = %q, want %q", c.TmuxPrefix, "claude-prod-")
	}
	if c.TmuxSession != "claude-prod-forge-mon" {
		t.Errorf("TmuxSession = %q, want %q", c.TmuxSession, "claude-prod-forge-mon")
	}
}

func TestBridgeConnector_Init_ConflictError(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/register" {
			resp := registerResponse{Conflict: true}
			resp.OK = true
			resp.Settings.TmuxSession = "claude-prod-forge-mon"
			data, _ := json.Marshal(resp)
			w.Write(data)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &BridgeConnector{}
	err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "forge-mon",
		BridgeURL:  server.URL,
	})
	if err == nil || !strings.Contains(err.Error(), "already active") {
		t.Errorf("expected conflict error, got %v", err)
	}
}
