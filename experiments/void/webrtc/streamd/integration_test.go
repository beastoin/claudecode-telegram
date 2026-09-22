package streamd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/pion/webrtc/v4"
)

type integrationCDP struct {
	mu    sync.Mutex
	calls []string
	ch    chan string
}

func newIntegrationCDP() *integrationCDP {
	return &integrationCDP{ch: make(chan string, 8)}
}

func (m *integrationCDP) Dispatch(_ context.Context, method string, _ map[string]any) error {
	m.mu.Lock()
	m.calls = append(m.calls, method)
	m.mu.Unlock()
	m.ch <- method
	return nil
}

func TestIntegrationCreateSessionConnectWebRTCSendInput(t *testing.T) {
	secret := "gateway-secret"
	cdp := newIntegrationCDP()
	streamd, err := NewServer(Config{SessionSecret: secret, MaxSessions: 2, IdleTimeout: 5 * time.Minute}, cdp, nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer streamd.Close()

	streamdHTTP := httptest.NewServer(streamd.Handler())
	defer streamdHTTP.Close()

	gatewayURL, stopGateway := startGatewayProcess(t, streamdHTTP.URL, "turn:127.0.0.1:34780?transport=udp", secret)
	defer stopGateway()

	sessionID, token := gatewayCreateSession(t, gatewayURL)

	clientPC, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatalf("NewPeerConnection: %v", err)
	}
	defer clientPC.Close()

	dc, err := clientPC.CreateDataChannel("input", nil)
	if err != nil {
		t.Fatalf("CreateDataChannel: %v", err)
	}
	offer, err := clientPC.CreateOffer(nil)
	if err != nil {
		t.Fatalf("CreateOffer: %v", err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(clientPC)
	if err := clientPC.SetLocalDescription(offer); err != nil {
		t.Fatalf("SetLocalDescription: %v", err)
	}
	<-gatherComplete

	answer := gatewaySignalOffer(t, gatewayURL, sessionID, token, *clientPC.LocalDescription())
	if err := clientPC.SetRemoteDescription(answer); err != nil {
		t.Fatalf("SetRemoteDescription: %v", err)
	}

	opened := make(chan struct{}, 1)
	dc.OnOpen(func() {
		if sendErr := dc.SendText(`{"type":"touch","x":10,"y":20,"phase":"start"}`); sendErr != nil {
			t.Errorf("SendText: %v", sendErr)
		}
		opened <- struct{}{}
	})

	select {
	case <-opened:
	case <-time.After(4 * time.Second):
		t.Fatal("timed out waiting for data channel open")
	}

	select {
	case method := <-cdp.ch:
		if method != "Input.dispatchTouchEvent" {
			t.Fatalf("expected touch dispatch method, got %s", method)
		}
	case <-time.After(4 * time.Second):
		t.Fatal("timed out waiting for CDP dispatch")
	}
}

func TestIntegrationSessionCleanupOnDisconnect(t *testing.T) {
	secret := "gateway-secret"
	cdp := newIntegrationCDP()
	streamd, err := NewServer(Config{SessionSecret: secret, MaxSessions: 2, IdleTimeout: 5 * time.Minute}, cdp, nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer streamd.Close()

	streamdHTTP := httptest.NewServer(streamd.Handler())
	defer streamdHTTP.Close()

	gatewayURL, stopGateway := startGatewayProcess(t, streamdHTTP.URL, "turn:127.0.0.1:34780?transport=udp", secret)
	defer stopGateway()

	sessionID, token := gatewayCreateSession(t, gatewayURL)

	clientPC, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatalf("NewPeerConnection: %v", err)
	}
	dc, err := clientPC.CreateDataChannel("input", nil)
	if err != nil {
		t.Fatalf("CreateDataChannel: %v", err)
	}
	_ = dc

	offer, err := clientPC.CreateOffer(nil)
	if err != nil {
		t.Fatalf("CreateOffer: %v", err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(clientPC)
	if err := clientPC.SetLocalDescription(offer); err != nil {
		t.Fatalf("SetLocalDescription: %v", err)
	}
	<-gatherComplete

	answer := gatewaySignalOffer(t, gatewayURL, sessionID, token, *clientPC.LocalDescription())
	if err := clientPC.SetRemoteDescription(answer); err != nil {
		t.Fatalf("SetRemoteDescription: %v", err)
	}

	opened := make(chan struct{}, 1)
	dc.OnOpen(func() {
		opened <- struct{}{}
	})
	select {
	case <-opened:
	case <-time.After(4 * time.Second):
		t.Fatal("timed out waiting for data channel open")
	}

	if err := clientPC.Close(); err != nil {
		t.Fatalf("Close clientPC: %v", err)
	}

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if streamd.ActiveSessions() == 0 {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("expected active sessions to be 0 after disconnect, got %d", streamd.ActiveSessions())
}

func TestIntegrationTURNRelayConnectivity(t *testing.T) {
	secret := "gateway-secret"
	udpConn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("ListenPacket: %v", err)
	}
	defer udpConn.Close()
	turnAddr := udpConn.LocalAddr().String()

	streamd, err := NewServer(Config{SessionSecret: secret, MaxSessions: 2, IdleTimeout: 5 * time.Minute}, newIntegrationCDP(), nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer streamd.Close()
	streamdHTTP := httptest.NewServer(streamd.Handler())
	defer streamdHTTP.Close()

	gatewayURL, stopGateway := startGatewayProcess(t, streamdHTTP.URL, "turn:"+turnAddr+"?transport=udp", secret)
	defer stopGateway()

	sessionID, _ := gatewayCreateSession(t, gatewayURL)
	status, payload := postJSON(t, gatewayURL+"/turn/credentials", map[string]any{"session_id": sessionID, "ttl_seconds": 60})
	if status != http.StatusOK {
		t.Fatalf("expected 200 from /turn/credentials, got %d payload=%s", status, string(payload))
	}
	var body struct {
		URLs []string `json:"urls"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("unmarshal turn response: %v", err)
	}
	if len(body.URLs) != 1 {
		t.Fatalf("expected one TURN url, got %v", body.URLs)
	}
	urlValue := strings.TrimPrefix(body.URLs[0], "turn:")
	hostPort := strings.SplitN(urlValue, "?", 2)[0]

	gotPacket := make(chan []byte, 1)
	go func() {
		buf := make([]byte, 32)
		_ = udpConn.SetReadDeadline(time.Now().Add(3 * time.Second))
		n, _, readErr := udpConn.ReadFrom(buf)
		if readErr == nil {
			gotPacket <- buf[:n]
		}
	}()

	conn, err := net.Dial("udp", hostPort)
	if err != nil {
		t.Fatalf("Dial udp turn endpoint: %v", err)
	}
	defer conn.Close()
	if _, err := conn.Write([]byte("ping")); err != nil {
		t.Fatalf("udp write: %v", err)
	}

	select {
	case pkt := <-gotPacket:
		if string(pkt) != "ping" {
			t.Fatalf("unexpected packet: %q", string(pkt))
		}
	case <-time.After(4 * time.Second):
		t.Fatal("timed out waiting for packet at TURN endpoint")
	}
}

func startGatewayProcess(t *testing.T, streamdURL, turnURL, secret string) (string, func()) {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	scriptPath := filepath.Clean(filepath.Join(filepath.Dir(thisFile), "..", "gateway", "gateway.py"))
	if _, err := os.Stat(scriptPath); err != nil {
		t.Fatalf("gateway.py not found at %s: %v", scriptPath, err)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen free port: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	addr := fmt.Sprintf("127.0.0.1:%d", port)

	cmd := exec.Command("python3", scriptPath)
	stderr := &bytes.Buffer{}
	cmd.Stdout = stderr
	cmd.Stderr = stderr
	cmd.Env = append(os.Environ(),
		"SESSION_SECRET="+secret,
		"TURN_SECRET=turn-secret",
		"TURN_URL="+turnURL,
		"STREAMD_URL="+streamdURL,
		"GATEWAY_ADDR="+addr,
	)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start gateway: %v", err)
	}

	baseURL := "http://" + addr
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(baseURL + "/__ready__")
		if err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == http.StatusNotFound {
				break
			}
		}
		time.Sleep(50 * time.Millisecond)
	}

	stop := func() {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
	}
	return baseURL, stop
}

func gatewayCreateSession(t *testing.T, gatewayURL string) (string, string) {
	t.Helper()
	status, payload := postJSON(t, gatewayURL+"/session/create", map[string]any{})
	if status != http.StatusOK {
		t.Fatalf("expected 200 from /session/create, got %d payload=%s", status, string(payload))
	}
	var body struct {
		SessionID string `json:"session_id"`
		Token     string `json:"token"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("unmarshal create session: %v", err)
	}
	if body.SessionID == "" || body.Token == "" {
		t.Fatalf("invalid /session/create response: %s", string(payload))
	}
	return body.SessionID, body.Token
}

func gatewaySignalOffer(t *testing.T, gatewayURL, sessionID, token string, offer webrtc.SessionDescription) webrtc.SessionDescription {
	t.Helper()
	status, payload := postJSON(t, gatewayURL+"/signal/offer", map[string]any{
		"session_id": sessionID,
		"token":      token,
		"offer":      offer,
	})
	if status != http.StatusOK {
		t.Fatalf("expected 200 from /signal/offer, got %d payload=%s", status, string(payload))
	}
	var body struct {
		Answer webrtc.SessionDescription `json:"answer"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		t.Fatalf("unmarshal signal offer response: %v", err)
	}
	return body.Answer
}

func postJSON(t *testing.T, url string, payload any) (int, []byte) {
	t.Helper()
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, body
}
