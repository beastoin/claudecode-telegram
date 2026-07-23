package connector

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"
)

type ConnectorHost struct {
	connector  Connector
	runtime    Runtime
	config     ConnectorConfig
	hookSocket *HookListener

	cmdMu    sync.RWMutex
	commands map[string]registeredCommand
}

type registeredCommand struct {
	Spec    CommandSpec
	Handler CommandHandler
}

func NewConnectorHost(connector Connector, runtime Runtime, config ConnectorConfig) *ConnectorHost {
	return &ConnectorHost{
		connector: connector,
		runtime:   runtime,
		config:    config,
		commands:  make(map[string]registeredCommand),
	}
}

func (h *ConnectorHost) RegisterCommand(spec CommandSpec, handler CommandHandler) {
	h.cmdMu.Lock()
	defer h.cmdMu.Unlock()
	h.commands[spec.Name] = registeredCommand{Spec: spec, Handler: handler}
}

func (h *ConnectorHost) Commands() []CommandSpec {
	h.cmdMu.RLock()
	defer h.cmdMu.RUnlock()
	specs := make([]CommandSpec, 0, len(h.commands))
	for _, rc := range h.commands {
		specs = append(specs, rc.Spec)
	}
	return specs
}

func (h *ConnectorHost) Run(ctx context.Context) error {
	if err := h.checkRequirements(); err != nil {
		return fmt.Errorf("requirements check failed: %w", err)
	}

	if sink, ok := h.connector.(InboundSink); ok {
		sink.SetInboundSink(h.DeliverInbound)
	}

	if surface, ok := h.connector.(CommandSurface); ok {
		surface.ConfigureCommands(h.Commands())
	}

	h.startHookListener()
	return h.runReceiveLoop(ctx)
}

func (h *ConnectorHost) runReceiveLoop(ctx context.Context) error {
	switch c := h.connector.(type) {
	case ExternalReceiver:
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

func (h *ConnectorHost) Stop() error {
	if h.hookSocket != nil {
		h.hookSocket.Stop()
	}
	return h.connector.Close()
}

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

func (h *ConnectorHost) runPollLoop(ctx context.Context, c PollReceiver) error {
	interval := c.PollInterval()
	if interval <= 0 {
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
				continue
			}
			for _, msg := range msgs {
				h.DeliverInbound(msg)
			}
		}
	}
}

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
			h.DeliverInbound(msg)
		}
	}
}

func (h *ConnectorHost) DeliverInbound(msg InboundMessage) {
	if msg.Text != "" {
		if inv, ok := ParseCommand(msg.Text); ok {
			inv.Source = msg
			h.executeCommand(context.Background(), inv)
			return
		}
	}
	if h.runtime != nil && msg.Text != "" {
		h.runtime.Send(FormatInbound(msg))
	}
}

func (h *ConnectorHost) executeCommand(ctx context.Context, inv CommandInvocation) {
	h.cmdMu.RLock()
	rc, ok := h.commands[inv.Command]
	h.cmdMu.RUnlock()

	var result CommandResult
	if !ok {
		result = CommandResult{Text: fmt.Sprintf("Unknown command /%s. Type /help for available commands.", inv.Command)}
	} else {
		result = rc.Handler(ctx, inv)
	}

	if result.Error != nil {
		result.Text = fmt.Sprintf("Error: %v", result.Error)
	}
	if result.Text != "" {
		h.connector.Send(ctx, Response{Text: result.Text})
	}
}

func ParseCommand(text string) (CommandInvocation, bool) {
	text = strings.TrimSpace(text)
	if !strings.HasPrefix(text, "/") {
		return CommandInvocation{}, false
	}

	parts := strings.Fields(text)
	cmd := strings.TrimPrefix(parts[0], "/")
	if cmd == "" {
		return CommandInvocation{}, false
	}

	var args []string
	if len(parts) > 1 {
		args = parts[1:]
	}

	return CommandInvocation{
		Command: cmd,
		Args:    args,
		Raw:     text,
	}, true
}

func (h *ConnectorHost) startHookListener() {
	socketPath := fmt.Sprintf("/tmp/forge-%s.sock", sanitizeSocketName(h.config.WorkerName))
	hl := NewHookListener(socketPath, h.connector)
	if err := hl.Start(); err != nil {
		return
	}
	h.hookSocket = hl
}

func sanitizeSocketName(name string) string {
	r := strings.NewReplacer("/", "-", " ", "-", "\\", "-")
	return r.Replace(name)
}

func FormatInbound(msg InboundMessage) string {
	if msg.From != "" {
		return fmt.Sprintf("[%s] %s", msg.From, msg.Text)
	}
	return msg.Text
}
