package forge

import (
	"context"
	"strings"
	"testing"
)

func TestParseCommand(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		input   string
		wantOK  bool
		wantCmd string
		wantN   int
	}{
		{"slash help", "/help", true, "help", 0},
		{"slash login", "/login", true, "login", 0},
		{"with args", "/status --verbose", true, "status", 1},
		{"multi args", "/send foo bar baz", true, "send", 3},
		{"not a command", "hello world", false, "", 0},
		{"empty", "", false, "", 0},
		{"bare slash", "/", false, "", 0},
		{"with leading space", "  /help  ", true, "help", 0},
		{"mid-text slash", "use /help command", false, "", 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			inv, ok := ParseCommand(tt.input)
			if ok != tt.wantOK {
				t.Fatalf("ParseCommand(%q) ok = %v, want %v", tt.input, ok, tt.wantOK)
			}
			if !ok {
				return
			}
			if inv.Command != tt.wantCmd {
				t.Errorf("command = %q, want %q", inv.Command, tt.wantCmd)
			}
			if len(inv.Args) != tt.wantN {
				t.Errorf("args count = %d, want %d", len(inv.Args), tt.wantN)
			}
			if inv.Raw != strings.TrimSpace(tt.input) {
				t.Errorf("raw = %q, want %q", inv.Raw, strings.TrimSpace(tt.input))
			}
		})
	}
}

func TestConnectorHost_CommandInterception(t *testing.T) {
	t.Parallel()

	var sent []string
	connector := &spyConnector{
		sendFn: func(_ context.Context, resp Response) error {
			sent = append(sent, resp.Text)
			return nil
		},
	}

	runtime := &spyRuntime{}
	host := NewConnectorHost(connector, runtime, ConnectorConfig{})

	host.RegisterCommand(CommandSpec{
		Name:        "ping",
		Description: "Test command",
	}, func(ctx context.Context, inv CommandInvocation) CommandResult {
		return CommandResult{Text: "pong"}
	})

	// Command should be intercepted
	host.DeliverInbound(InboundMessage{Text: "/ping", From: "tester"})

	if len(sent) != 1 || sent[0] != "pong" {
		t.Fatalf("sent = %v, want [pong]", sent)
	}
	if len(runtime.sends) != 0 {
		t.Fatalf("runtime.sends = %v, want empty (command should not reach runtime)", runtime.sends)
	}
}

func TestConnectorHost_UnknownCommand(t *testing.T) {
	t.Parallel()

	var sent []string
	connector := &spyConnector{
		sendFn: func(_ context.Context, resp Response) error {
			sent = append(sent, resp.Text)
			return nil
		},
	}

	host := NewConnectorHost(connector, nil, ConnectorConfig{})

	host.DeliverInbound(InboundMessage{Text: "/unknown"})

	if len(sent) != 1 || !strings.Contains(sent[0], "Unknown command") {
		t.Fatalf("sent = %v, want unknown command message", sent)
	}
}

func TestConnectorHost_NonCommand(t *testing.T) {
	t.Parallel()

	connector := &spyConnector{}
	runtime := &spyRuntime{}
	host := NewConnectorHost(connector, runtime, ConnectorConfig{})

	host.RegisterCommand(CommandSpec{Name: "help"}, func(ctx context.Context, inv CommandInvocation) CommandResult {
		return CommandResult{Text: "help text"}
	})

	// Regular message should go to runtime
	host.DeliverInbound(InboundMessage{Text: "hello world", From: "user"})

	if len(runtime.sends) != 1 {
		t.Fatalf("runtime.sends = %v, want 1 message", runtime.sends)
	}
	if !strings.Contains(runtime.sends[0], "hello world") {
		t.Errorf("runtime got %q, want hello world", runtime.sends[0])
	}
}

func TestConnectorHost_CommandError(t *testing.T) {
	t.Parallel()

	var sent []string
	connector := &spyConnector{
		sendFn: func(_ context.Context, resp Response) error {
			sent = append(sent, resp.Text)
			return nil
		},
	}

	host := NewConnectorHost(connector, nil, ConnectorConfig{})
	host.RegisterCommand(CommandSpec{Name: "fail"}, func(ctx context.Context, inv CommandInvocation) CommandResult {
		return CommandResult{Error: context.DeadlineExceeded}
	})

	host.DeliverInbound(InboundMessage{Text: "/fail"})

	if len(sent) != 1 || !strings.Contains(sent[0], "Error:") {
		t.Fatalf("sent = %v, want error message", sent)
	}
}

func TestConnectorHost_Commands(t *testing.T) {
	t.Parallel()

	host := NewConnectorHost(&spyConnector{}, nil, ConnectorConfig{})
	host.RegisterCommand(CommandSpec{Name: "a", Description: "first"}, nil)
	host.RegisterCommand(CommandSpec{Name: "b", Description: "second"}, nil)

	cmds := host.Commands()
	if len(cmds) != 2 {
		t.Fatalf("Commands() = %d, want 2", len(cmds))
	}
}

func TestConnectorHost_CommandSurface(t *testing.T) {
	t.Parallel()

	surface := &surfaceConnector{}
	host := NewConnectorHost(surface, nil, ConnectorConfig{})
	host.RegisterCommand(CommandSpec{Name: "help", Description: "List commands"}, nil)
	host.RegisterCommand(CommandSpec{Name: "status", Description: "Show status"}, nil)

	if len(surface.configured) != 0 {
		t.Fatalf("commands before Run = %d, want 0", len(surface.configured))
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	host.Run(ctx)

	if len(surface.configured) != 2 {
		t.Fatalf("commands after Run = %d, want 2", len(surface.configured))
	}
}

type surfaceConnector struct {
	spyConnector
	configured []CommandSpec
}

func (s *surfaceConnector) ConfigureCommands(cmds []CommandSpec) error {
	s.configured = cmds
	return nil
}

func (s *surfaceConnector) IsExternal() {}

func TestHelpCommand(t *testing.T) {
	t.Parallel()

	host := NewConnectorHost(&spyConnector{}, nil, ConnectorConfig{})
	RegisterBuiltinCommands(host, CommandServices{WorkerName: "test-worker"})

	cmds := host.Commands()
	found := false
	for _, c := range cmds {
		if c.Name == "help" {
			found = true
			break
		}
	}
	if !found {
		t.Fatal("help command not registered")
	}
}

func TestStatusCommand(t *testing.T) {
	t.Parallel()

	var sent []string
	connector := &spyConnector{
		sendFn: func(_ context.Context, resp Response) error {
			sent = append(sent, resp.Text)
			return nil
		},
	}

	runtime := &spyRuntime{healthErr: nil}
	host := NewConnectorHost(connector, runtime, ConnectorConfig{})
	RegisterBuiltinCommands(host, CommandServices{
		Runtime:    runtime,
		WorkerName: "test-worker",
	})

	host.DeliverInbound(InboundMessage{Text: "/status"})

	if len(sent) != 1 {
		t.Fatalf("sent = %v, want 1 message", sent)
	}
	if !strings.Contains(sent[0], "test-worker") {
		t.Errorf("status output missing worker name: %q", sent[0])
	}
	if !strings.Contains(sent[0], "healthy") {
		t.Errorf("status output missing health: %q", sent[0])
	}
}

func TestBuiltinCommands_NoAuthService(t *testing.T) {
	t.Parallel()

	host := NewConnectorHost(&spyConnector{}, nil, ConnectorConfig{})
	RegisterBuiltinCommands(host, CommandServices{WorkerName: "w"})

	cmds := host.Commands()
	for _, c := range cmds {
		if c.Name == "login" || c.Name == "logout" {
			t.Errorf("auth command %q registered without AuthCommandService", c.Name)
		}
	}
}

// spyConnector is a minimal connector for testing.
type spyConnector struct {
	sendFn func(context.Context, Response) error
}

func (s *spyConnector) Type() string                                    { return "spy" }
func (s *spyConnector) Capabilities() Caps                              { return CapText }
func (s *spyConnector) Requirements() Reqs                              { return 0 }
func (s *spyConnector) Init(_ context.Context, _ ConnectorConfig) error { return nil }
func (s *spyConnector) Close() error                                    { return nil }
func (s *spyConnector) Send(ctx context.Context, resp Response) error {
	if s.sendFn != nil {
		return s.sendFn(ctx, resp)
	}
	return nil
}

// spyRuntime captures Send calls and supports Health.
type spyRuntime struct {
	sends     []string
	healthErr error
}

func (r *spyRuntime) Start() error                     { return nil }
func (r *spyRuntime) Health() error                    { return r.healthErr }
func (r *spyRuntime) LastOutput() (string, error)      { return "", nil }
func (r *spyRuntime) Send(msg string) error            { r.sends = append(r.sends, msg); return nil }
func (r *spyRuntime) Stop() error                      { return nil }
