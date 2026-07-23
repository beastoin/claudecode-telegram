package connector

import (
	"context"
	"net"
	"net/http"
	"os/exec"
	"sync"
	"testing"
)

type connStubRuntime struct {
	mu   sync.Mutex
	sent []string
}

func (s *connStubRuntime) Send(msg string) error {
	s.mu.Lock()
	s.sent = append(s.sent, msg)
	s.mu.Unlock()
	return nil
}

func (s *connStubRuntime) Health() error { return nil }

func (s *connStubRuntime) getSent() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	cp := make([]string, len(s.sent))
	copy(cp, s.sent)
	return cp
}

type sinkPushStub struct {
	deliver func(InboundMessage)
	sent    []Response
}

func (s *sinkPushStub) Type() string                                    { return "sink-push-stub" }
func (s *sinkPushStub) Capabilities() Caps                              { return CapText }
func (s *sinkPushStub) Requirements() Reqs                              { return 0 }
func (s *sinkPushStub) Init(_ context.Context, _ ConnectorConfig) error { return nil }
func (s *sinkPushStub) Send(_ context.Context, resp Response) error     { s.sent = append(s.sent, resp); return nil }
func (s *sinkPushStub) Close() error                                    { return nil }
func (s *sinkPushStub) Handler() http.Handler                           { return http.NotFoundHandler() }
func (s *sinkPushStub) ListenAddr() string                              { return ":0" }
func (s *sinkPushStub) SetInboundSink(fn func(InboundMessage))          { s.deliver = fn }

var (
	_ PushReceiver = (*sinkPushStub)(nil)
	_ InboundSink  = (*sinkPushStub)(nil)
)

func tmuxAvailable() bool {
	_, err := exec.LookPath("tmux")
	return err == nil
}

func findFreePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("findFreePort: %v", err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return port
}
