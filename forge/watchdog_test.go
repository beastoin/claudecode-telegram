package forge

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestWatchdog_Run_HealthTickerRestartsDeadRuntime(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 1)
	runtime := &watchdogRuntimeSpy{healthErr: errBoom, startCh: make(chan struct{}, 1)}
	watchdog := &Watchdog{
		Runtime:    runtime,
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- watchdog.Run(ctx)
	}()

	fastTick <- time.Now()
	<-runtime.startCh
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
}

func TestWatchdog_Run_HealthTickerHeartbeatsAndReregistersTransport(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 1)
	transport := &watchdogTransportSpy{
		heartbeatErr: errBoom,
		registerCh:   make(chan struct{}, 1),
	}
	watchdog := &Watchdog{
		Transport:  transport,
		Register:   RegisterRequest{Name: "mon"},
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- watchdog.Run(ctx)
	}()

	fastTick <- time.Now()
	<-transport.registerCh
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if transport.heartbeatCalls != 1 {
		t.Fatalf("transport.heartbeatCalls = %d, want 1", transport.heartbeatCalls)
	}
	if transport.registerCalls != 1 {
		t.Fatalf("transport.registerCalls = %d, want 1", transport.registerCalls)
	}
	if transport.lastRegister.Name != "mon" {
		t.Fatalf("transport.lastRegister.Name = %q, want mon", transport.lastRegister.Name)
	}
}

func TestWatchdog_Run_PeriodicallyReregistersEvenWhenHeartbeatSucceeds(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 2)
	transport := &watchdogTransportSpy{
		registerCh: make(chan struct{}, 2),
	}
	watchdog := &Watchdog{
		Transport:  transport,
		Register:   RegisterRequest{Name: "mon"},
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- watchdog.Run(ctx)
	}()

	fastTick <- time.Now()
	fastTick <- time.Now()
	<-transport.registerCh
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if transport.heartbeatCalls < 2 {
		t.Fatalf("transport.heartbeatCalls = %d, want at least 2", transport.heartbeatCalls)
	}
	if transport.registerCalls < 1 {
		t.Fatalf("transport.registerCalls = %d, want at least 1", transport.registerCalls)
	}
}

func TestWatchdog_Run_RestartsRuntimeWhenClaudeOutputIsStale(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 2)
	clock := &watchdogClockStub{
		times: []time.Time{
			time.Unix(0, 0),
			time.Unix(2, 0),
		},
	}
	runtime := &watchdogRuntimeSpy{
		lastOutput: "still thinking",
		startCh:    make(chan struct{}, 1),
	}
	watchdog := &Watchdog{
		Runtime:              runtime,
		Clock:                clock,
		FastTicker:           fastTick,
		ClaudeStaleThreshold: time.Second,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- watchdog.Run(ctx)
	}()

	fastTick <- time.Now()
	fastTick <- time.Now()
	<-runtime.startCh
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1", runtime.startCalls)
	}
}

func TestWatchdog_Run_IntegrityTickerAlertsOnCriticalDrift(t *testing.T) {
	t.Parallel()

	integrityTick := make(chan time.Time, 1)
	bridge := &stubBridgeClient{}
	watchdog := &Watchdog{
		Bridge:          bridge,
		Integrity:       stubIntegrityMonitor{err: errBoom},
		Clock:           fixedClock{},
		IntegrityTicker: integrityTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- watchdog.Run(ctx)
	}()

	integrityTick <- time.Now()
	for len(bridge.alerts) == 0 {
		time.Sleep(time.Millisecond)
	}
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if len(bridge.alerts) != 1 {
		t.Fatalf("bridge.alerts = %#v, want 1 alert", bridge.alerts)
	}
}

func TestIntegration_Watchdog_DetectsRealFileDrift(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	charterPath := filepath.Join(root, "team", "test", "charter.md")
	if err := os.MkdirAll(filepath.Dir(charterPath), 0o700); err != nil {
		t.Fatal(err)
	}
	charterContent := []byte("charter v1")
	if err := os.WriteFile(charterPath, charterContent, 0o600); err != nil {
		t.Fatal(err)
	}

	manifest := &Manifest{
		Name:    "test",
		Version: "1.0.0",
		Vars: map[string]VarSpec{
			"HOME":              {Source: "env", Required: true},
			"CLAUDE_CONFIG_DIR": {Source: "default", Default: "$HOME/.claude", Required: true},
		},
		Files: []FileSpec{
			{Source: "team/test/charter.md", Dest: "$HOME/team/test/charter.md", ContentKey: "files/team/test/charter.md"},
		},
	}

	checksums := checksumMap(t, map[string][]byte{
		"files/team/test/charter.md": charterContent,
		"team/test/charter.md":       charterContent,
	})
	verifier := NewChecksumVerifier(checksums)
	vars := map[string]string{"HOME": root, "CLAUDE_CONFIG_DIR": filepath.Join(root, ".claude")}

	monitor := &FileIntegrityMonitor{
		Manifest: manifest,
		Vars:     vars,
		Verifier: verifier,
	}

	if err := monitor.VerifyCritical(); err != nil {
		t.Fatalf("VerifyCritical() with clean files: %v", err)
	}

	os.WriteFile(charterPath, []byte("TAMPERED"), 0o600)

	err := monitor.VerifyCritical()
	if err == nil {
		t.Fatal("VerifyCritical() after tamper: want error, got nil")
	}
	if !strings.Contains(err.Error(), "checksum mismatch") {
		t.Fatalf("VerifyCritical() error = %v, want checksum mismatch", err)
	}

	integrityTick := make(chan time.Time, 1)
	bridge := &stubBridgeClient{}
	watchdog := &Watchdog{
		Bridge:          bridge,
		Integrity:       monitor,
		Clock:           fixedClock{},
		IntegrityTicker: integrityTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	integrityTick <- time.Now()
	for len(bridge.alerts) == 0 {
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-done

	if len(bridge.alerts) != 1 {
		t.Fatalf("bridge.alerts = %d, want 1", len(bridge.alerts))
	}
	if !strings.Contains(bridge.alerts[0], "integrity") {
		t.Fatalf("alert = %q, want to contain 'integrity'", bridge.alerts[0])
	}
}

type watchdogRuntimeSpy struct {
	startCalls int
	healthErr  error
	startCh    chan struct{}
	lastOutput string
}

func (s *watchdogRuntimeSpy) Start() error {
	s.startCalls++
	if s.startCh != nil {
		s.startCh <- struct{}{}
	}
	return nil
}

func (s *watchdogRuntimeSpy) Send(string) error {
	return nil
}

func (s *watchdogRuntimeSpy) Health() error {
	return s.healthErr
}

func (s *watchdogRuntimeSpy) LastOutput() (string, error) {
	return s.lastOutput, nil
}

type watchdogTransportSpy struct {
	heartbeatErr   error
	heartbeatCalls int
	registerCalls  int
	lastRegister   RegisterRequest
	registerCh     chan struct{}
}

func (s *watchdogTransportSpy) Connect(context.Context, string) error {
	return nil
}

func (s *watchdogTransportSpy) Register(_ context.Context, req RegisterRequest) (RegisterResponse, error) {
	s.registerCalls++
	s.lastRegister = req
	if s.registerCh != nil {
		s.registerCh <- struct{}{}
	}
	return RegisterResponse{OK: true}, nil
}

func (s *watchdogTransportSpy) SendMessage(context.Context, WorkerMessage) error {
	return nil
}

func (s *watchdogTransportSpy) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, nil
}

func (s *watchdogTransportSpy) StreamJSONL(context.Context, JSONLChunk) error {
	return nil
}

func (s *watchdogTransportSpy) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, nil
}

func (s *watchdogTransportSpy) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", nil
}

func (s *watchdogTransportSpy) Heartbeat(context.Context) error {
	s.heartbeatCalls++
	return s.heartbeatErr
}

func (s *watchdogTransportSpy) Close() error {
	return nil
}

type watchdogClockStub struct {
	times []time.Time
	index int
}

func (c *watchdogClockStub) Now() time.Time {
	if len(c.times) == 0 {
		return time.Unix(0, 0)
	}
	if c.index >= len(c.times) {
		return c.times[len(c.times)-1]
	}
	current := c.times[c.index]
	c.index++
	return current
}
