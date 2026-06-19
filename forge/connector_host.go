package forge

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// ConnectorHost orchestrates the connector lifecycle.
// It checks requirements, initializes the connector, detects the receiver
// pattern via type switch, and runs the appropriate loop.
type ConnectorHost struct {
	connector  Connector
	runtime    Runtime
	config     ConnectorConfig
	hookSocket *HookListener
}

// NewConnectorHost creates a new ConnectorHost.
func NewConnectorHost(connector Connector, runtime Runtime, config ConnectorConfig) *ConnectorHost {
	return &ConnectorHost{
		connector: connector,
		runtime:   runtime,
		config:    config,
	}
}

// Run starts the connector and appropriate receive loop.
// Blocks until ctx is cancelled or an error occurs.
func (h *ConnectorHost) Run(ctx context.Context) error {
	// Check requirements before Init
	if err := h.checkRequirements(); err != nil {
		return fmt.Errorf("requirements check failed: %w", err)
	}

	h.config.Runtime = h.runtime

	// Initialize the connector
	if err := h.connector.Init(ctx, h.config); err != nil {
		return fmt.Errorf("connector init failed: %w", err)
	}

	h.startHookListener()

	// Detect receiver pattern and run appropriate loop
	switch c := h.connector.(type) {
	case ExternalReceiver:
		// Nothing to run — bridge injects externally.
		// Just block until ctx cancelled.
		<-ctx.Done()
		return nil

	case PushReceiver:
		return h.runWebhook(ctx, c)

	case PollReceiver:
		return h.runPollLoop(ctx, c)

	case StreamReceiver:
		return h.runStream(ctx, c)

	default:
		return fmt.Errorf("connector %q implements no receiver pattern", h.connector.Type())
	}
}

// Stop gracefully shuts down the connector host.
func (h *ConnectorHost) Stop() error {
	if h.hookSocket != nil {
		h.hookSocket.Stop()
	}
	return h.connector.Close()
}

// checkRequirements verifies that the host environment satisfies the connector's requirements.
func (h *ConnectorHost) checkRequirements() error {
	reqs := h.connector.Requirements()

	if reqs&ReqRuntime != 0 && h.runtime == nil {
		return fmt.Errorf("connector %q requires Runtime but none provided", h.connector.Type())
	}

	if reqs&ReqHTTPListener != 0 {
		if h.config.ListenAddr == "" {
			h.config.ListenAddr = ":8443"
		}
	}

	return nil
}

// runPollLoop calls Poll on the connector at the configured interval and delivers
// messages to the runtime.
func (h *ConnectorHost) runPollLoop(ctx context.Context, c PollReceiver) error {
	interval := c.PollInterval()
	if interval <= 0 {
		// For connectors with built-in long-poll (e.g., Telegram getUpdates),
		// use a minimal interval just to re-enter the loop.
		interval = 100 * time.Millisecond
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			msgs, err := c.Poll(ctx)
			if err != nil {
				// Log and continue; don't crash on transient poll failures.
				continue
			}
			for _, msg := range msgs {
				h.deliverInbound(msg)
			}
		}
	}
}

// runWebhook starts an HTTP server with the connector's handler.
func (h *ConnectorHost) runWebhook(ctx context.Context, c PushReceiver) error {
	addr := c.ListenAddr()
	if addr == "" {
		addr = h.config.ListenAddr
	}

	srv := &http.Server{
		Addr:    addr,
		Handler: c.Handler(),
	}

	errCh := make(chan error, 1)
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			errCh <- err
		}
		close(errCh)
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	case err := <-errCh:
		return err
	}
}

// runStream reads messages from the connector's channel and delivers them.
func (h *ConnectorHost) runStream(ctx context.Context, c StreamReceiver) error {
	ch := c.Messages()
	for {
		select {
		case <-ctx.Done():
			return nil
		case msg, ok := <-ch:
			if !ok {
				return nil
			}
			h.deliverInbound(msg)
		}
	}
}

// deliverInbound sends a received message to the runtime for injection into tmux.
func (h *ConnectorHost) deliverInbound(msg InboundMessage) {
	if h.runtime != nil && msg.Text != "" {
		h.runtime.Send(formatInbound(msg))
	}
}

// startHookListener starts a Unix socket listener for hook IPC.
// This allows stop hooks to route responses through the active connector.
func (h *ConnectorHost) startHookListener() {
	socketPath := fmt.Sprintf("/tmp/forge-%s.sock", sanitizeSocketName(h.config.WorkerName))
	hl := NewHookListener(socketPath, h.connector)
	if err := hl.Start(); err != nil {
		// Non-fatal — hook IPC is a convenience, not critical.
		return
	}
	h.hookSocket = hl
}

func sanitizeSocketName(name string) string {
	r := strings.NewReplacer("/", "-", " ", "-", "\\", "-")
	return r.Replace(name)
}

// formatInbound formats an InboundMessage for injection into the runtime.
func formatInbound(msg InboundMessage) string {
	if msg.From != "" {
		return fmt.Sprintf("[%s] %s", msg.From, msg.Text)
	}
	return msg.Text
}
