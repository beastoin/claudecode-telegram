package streamd

import "context"

// NoopCDPClient drops all input events.
type NoopCDPClient struct{}

func (n *NoopCDPClient) Dispatch(_ context.Context, _ string, _ map[string]any) error {
	return nil
}
