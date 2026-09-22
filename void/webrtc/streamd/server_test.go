package streamd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/pion/webrtc/v4"
)

type cdpCall struct {
	Method string
	Params map[string]any
}

type mockCDPClient struct {
	mu    sync.Mutex
	calls []cdpCall
	ch    chan cdpCall
}

func newMockCDPClient() *mockCDPClient {
	return &mockCDPClient{ch: make(chan cdpCall, 8)}
}

func (m *mockCDPClient) Dispatch(_ context.Context, method string, params map[string]any) error {
	call := cdpCall{Method: method, Params: params}
	m.mu.Lock()
	m.calls = append(m.calls, call)
	m.mu.Unlock()
	m.ch <- call
	return nil
}

func TestOfferEndpointAcceptsOfferReturnsAnswer(t *testing.T) {
	srv, ts := newHTTPTestServer(t, Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute})
	defer ts.Close()
	defer srv.Close()

	clientPC, offer, _ := makeOffer(t, false)
	defer clientPC.Close()

	status, answer, _ := postOffer(t, ts.URL, testToken(t, "test-secret", "sess-1", 1*time.Minute), offer)
	if status != http.StatusOK {
		t.Fatalf("expected status 200, got %d", status)
	}
	if answer.Type != webrtc.SDPTypeAnswer || answer.SDP == "" {
		t.Fatalf("expected valid SDP answer, got %+v", answer)
	}
}

func TestDataChannelReceivesTouchEventAndDispatchesToCDP(t *testing.T) {
	_, ts := newHTTPTestServer(t, Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute})
	defer ts.Close()

	cdp := newMockCDPClient()
	srv, err := NewServer(Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute}, cdp, nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Close()

	serverTS := httptest.NewServer(srv.Handler())
	defer serverTS.Close()

	clientPC, offer, dc := makeOffer(t, true)
	defer clientPC.Close()

	_, answer, _ := postOffer(t, serverTS.URL, testToken(t, "test-secret", "sess-touch", 1*time.Minute), offer)
	if err := clientPC.SetRemoteDescription(answer); err != nil {
		t.Fatalf("SetRemoteDescription: %v", err)
	}

	sendOnOpen(t, dc, `{"type":"touch","x":120,"y":44,"phase":"start"}`)
	call := waitForCDPCall(t, cdp)
	if call.Method != "Input.dispatchTouchEvent" {
		t.Fatalf("expected touch dispatch, got %q", call.Method)
	}
}

func TestDataChannelReceivesKeyEventAndDispatchesToCDP(t *testing.T) {
	cdp := newMockCDPClient()
	srv, err := NewServer(Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute}, cdp, nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	defer srv.Close()

	ts := httptest.NewServer(srv.Handler())
	defer ts.Close()

	clientPC, offer, dc := makeOffer(t, true)
	defer clientPC.Close()

	_, answer, _ := postOffer(t, ts.URL, testToken(t, "test-secret", "sess-key", 1*time.Minute), offer)
	if err := clientPC.SetRemoteDescription(answer); err != nil {
		t.Fatalf("SetRemoteDescription: %v", err)
	}

	sendOnOpen(t, dc, `{"type":"key","key":"Enter","action":"down"}`)
	call := waitForCDPCall(t, cdp)
	if call.Method != "Input.dispatchKeyEvent" {
		t.Fatalf("expected key dispatch, got %q", call.Method)
	}
}

func TestOfferAddsH264VideoTrack(t *testing.T) {
	srv, ts := newHTTPTestServer(t, Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute})
	defer ts.Close()
	defer srv.Close()

	clientPC, offer, _ := makeOffer(t, false)
	defer clientPC.Close()

	status, answer, _ := postOffer(t, ts.URL, testToken(t, "test-secret", "sess-h264", 1*time.Minute), offer)
	if status != http.StatusOK {
		t.Fatalf("expected status 200, got %d", status)
	}
	if !bytes.Contains([]byte(answer.SDP), []byte("H264")) {
		t.Fatalf("expected answer SDP to advertise H264 codec")
	}
}

func TestSessionTokenValidationRejectsExpiredOrInvalidTokens(t *testing.T) {
	srv, ts := newHTTPTestServer(t, Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute})
	defer ts.Close()
	defer srv.Close()

	clientPC, offer, _ := makeOffer(t, false)
	defer clientPC.Close()

	status, _, body := postOffer(t, ts.URL, "totally-invalid", offer)
	if status != http.StatusUnauthorized {
		t.Fatalf("expected invalid token to get 401, got %d body=%s", status, body)
	}

	status, _, body = postOffer(t, ts.URL, testToken(t, "test-secret", "sess-expired", -1*time.Minute), offer)
	if status != http.StatusUnauthorized {
		t.Fatalf("expected expired token to get 401, got %d body=%s", status, body)
	}
}

func TestIdleTimeoutClosesConnectionAfterNoInput(t *testing.T) {
	srv, ts := newHTTPTestServer(t, Config{
		SessionSecret:      "test-secret",
		MaxSessions:        2,
		IdleTimeout:        60 * time.Millisecond,
		IdleCheckInterval:  10 * time.Millisecond,
	})
	defer ts.Close()
	defer srv.Close()

	clientPC, offer, _ := makeOffer(t, false)
	defer clientPC.Close()

	status, _, body := postOffer(t, ts.URL, testToken(t, "test-secret", "sess-idle", 1*time.Minute), offer)
	if status != http.StatusOK {
		t.Fatalf("expected status 200, got %d body=%s", status, body)
	}

	deadline := time.Now().Add(1 * time.Second)
	for time.Now().Before(deadline) {
		if srv.ActiveSessions() == 0 {
			return
		}
		time.Sleep(15 * time.Millisecond)
	}
	t.Fatalf("expected idle session to be closed")
}

func TestConcurrentSessionLimitMaxTwo(t *testing.T) {
	srv, ts := newHTTPTestServer(t, Config{SessionSecret: "test-secret", MaxSessions: 2, IdleTimeout: 5 * time.Minute})
	defer ts.Close()
	defer srv.Close()

	pc1, offer1, _ := makeOffer(t, false)
	defer pc1.Close()
	status, _, body := postOffer(t, ts.URL, testToken(t, "test-secret", "sess-1", 1*time.Minute), offer1)
	if status != http.StatusOK {
		t.Fatalf("first offer expected 200, got %d body=%s", status, body)
	}

	pc2, offer2, _ := makeOffer(t, false)
	defer pc2.Close()
	status, _, body = postOffer(t, ts.URL, testToken(t, "test-secret", "sess-2", 1*time.Minute), offer2)
	if status != http.StatusOK {
		t.Fatalf("second offer expected 200, got %d body=%s", status, body)
	}

	pc3, offer3, _ := makeOffer(t, false)
	defer pc3.Close()
	status, _, body = postOffer(t, ts.URL, testToken(t, "test-secret", "sess-3", 1*time.Minute), offer3)
	if status != http.StatusTooManyRequests {
		t.Fatalf("third offer expected 429, got %d body=%s", status, body)
	}
}

func newHTTPTestServer(t *testing.T, cfg Config) (*Server, *httptest.Server) {
	t.Helper()
	srv, err := NewServer(cfg, newMockCDPClient(), nil)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	return srv, httptest.NewServer(srv.Handler())
}

func makeOffer(t *testing.T, withDataChannel bool) (*webrtc.PeerConnection, webrtc.SessionDescription, *webrtc.DataChannel) {
	t.Helper()
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatalf("NewPeerConnection: %v", err)
	}
	var dc *webrtc.DataChannel
	if withDataChannel {
		dc, err = pc.CreateDataChannel("input", nil)
		if err != nil {
			t.Fatalf("CreateDataChannel: %v", err)
		}
	} else {
		if _, err := pc.AddTransceiverFromKind(webrtc.RTPCodecTypeVideo); err != nil {
			t.Fatalf("AddTransceiverFromKind: %v", err)
		}
	}
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		t.Fatalf("CreateOffer: %v", err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		t.Fatalf("SetLocalDescription: %v", err)
	}
	<-gatherComplete
	return pc, *pc.LocalDescription(), dc
}

func postOffer(t *testing.T, baseURL, token string, offer webrtc.SessionDescription) (int, webrtc.SessionDescription, string) {
	t.Helper()
	payload := map[string]any{"token": token, "offer": offer}
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("Marshal payload: %v", err)
	}
	resp, err := http.Post(baseURL+"/offer", "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("POST /offer: %v", err)
	}
	defer resp.Body.Close()
	var out struct {
		Answer webrtc.SessionDescription `json:"answer"`
		Error  string                    `json:"error"`
	}
	body, _ := ioReadAll(resp)
	_ = json.Unmarshal(body, &out)
	return resp.StatusCode, out.Answer, string(body)
}

func waitForCDPCall(t *testing.T, cdp *mockCDPClient) cdpCall {
	t.Helper()
	select {
	case call := <-cdp.ch:
		return call
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for CDP dispatch")
		return cdpCall{}
	}
}

func sendOnOpen(t *testing.T, dc *webrtc.DataChannel, payload string) {
	t.Helper()
	opened := make(chan struct{}, 1)
	dc.OnOpen(func() {
		if err := dc.SendText(payload); err != nil {
			t.Errorf("SendText: %v", err)
		}
		opened <- struct{}{}
	})
	select {
	case <-opened:
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for data channel open")
	}
}

func testToken(t *testing.T, secret, sessionID string, ttl time.Duration) string {
	t.Helper()
	claims := jwt.MapClaims{
		"sub": sessionID,
		"jti": fmt.Sprintf("jti-%d", time.Now().UnixNano()),
		"exp": time.Now().Add(ttl).Unix(),
		"iat": time.Now().Unix(),
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	signed, err := token.SignedString([]byte(secret))
	if err != nil {
		t.Fatalf("SignedString: %v", err)
	}
	return signed
}

func ioReadAll(resp *http.Response) ([]byte, error) {
	buf := new(bytes.Buffer)
	_, err := buf.ReadFrom(resp.Body)
	return buf.Bytes(), err
}
