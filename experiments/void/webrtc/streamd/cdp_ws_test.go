package streamd

import (
	"errors"
	"testing"
	"time"
)

func TestCDPResolvePendingPropagatesProtocolError(t *testing.T) {
	client := &CDPWSClient{done: make(chan struct{})}
	ch := make(chan cdpResponse, 1)
	client.pending.Store(int64(7), ch)

	client.resolvePending(cdpMessage{
		ID: 7,
		Error: &struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		}{
			Code:    -32000,
			Message: "permission denied",
		},
	})

	select {
	case resp := <-ch:
		if resp.err == nil {
			t.Fatalf("expected protocol error, got nil")
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatalf("timed out waiting for pending response")
	}

	if _, ok := client.pending.Load(int64(7)); ok {
		t.Fatalf("pending request was not removed")
	}
}

func TestCDPFailAllPendingOnConnectionClose(t *testing.T) {
	client := &CDPWSClient{done: make(chan struct{})}
	ch1 := make(chan cdpResponse, 1)
	ch2 := make(chan cdpResponse, 1)
	client.pending.Store(int64(1), ch1)
	client.pending.Store(int64(2), ch2)

	client.failAllPending(errors.New("connection closed"))

	for _, ch := range []chan cdpResponse{ch1, ch2} {
		select {
		case resp := <-ch:
			if resp.err == nil {
				t.Fatalf("expected close error, got nil")
			}
		case <-time.After(500 * time.Millisecond):
			t.Fatalf("timed out waiting for pending close response")
		}
	}

	left := 0
	client.pending.Range(func(_, _ any) bool {
		left++
		return true
	})
	if left != 0 {
		t.Fatalf("expected no pending entries after close, got %d", left)
	}
}
