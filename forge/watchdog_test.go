package forge

import (
	"context"
	"errors"
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
	bridge := &stubBridgeClient{alertCh: make(chan string, 1)}
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

	select {
	case alert := <-bridge.alertCh:
		if !strings.Contains(alert, "integrity") {
			t.Fatalf("alert = %q, want integrity drift message", alert)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for integrity alert")
	}
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if got := bridge.getAlerts(); len(got) != 1 {
		t.Fatalf("bridge.alerts = %#v, want 1 alert", got)
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
	bridge := &stubBridgeClient{alertCh: make(chan string, 1)}
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

	select {
	case alert := <-bridge.alertCh:
		if !strings.Contains(alert, "integrity") {
			t.Fatalf("alert = %q, want to contain 'integrity'", alert)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for integrity alert")
	}
	cancel()
	<-done

	if got := bridge.getAlerts(); len(got) != 1 {
		t.Fatalf("bridge.alerts = %d, want 1", len(got))
	}
}

// ---------------------------------------------------------------------------
// New behavior tests (codex audit gap-fills)
// ---------------------------------------------------------------------------

func TestWatchdog_CheckOnce_RunsBothTicks(t *testing.T) {
	t.Parallel()

	runtime := &watchdogRuntimeSpy{healthErr: errBoom, startCh: make(chan struct{}, 1)}
	bridge := &stubBridgeClient{alertCh: make(chan string, 1)}
	watchdog := &Watchdog{
		Runtime:   runtime,
		Bridge:    bridge,
		Integrity: stubIntegrityMonitor{err: errBoom},
		Clock:     fixedClock{},
	}

	if err := watchdog.CheckOnce(); err != nil {
		t.Fatalf("CheckOnce() error = %v", err)
	}

	if runtime.startCalls != 1 {
		t.Fatalf("runtime.startCalls = %d, want 1 (health failed → restart)", runtime.startCalls)
	}
	alerts := bridge.getAlerts()
	if len(alerts) != 1 {
		t.Fatalf("bridge.alerts = %d, want 1 integrity alert", len(alerts))
	}
	if !strings.Contains(alerts[0], "integrity") {
		t.Fatalf("alert = %q, want integrity drift message", alerts[0])
	}
}

func TestWatchdog_Run_HealthyRuntimeDoesNotRestart(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 2)
	runtime := &watchdogRuntimeSpy{}
	watchdog := &Watchdog{
		Runtime:    runtime,
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	fastTick <- time.Now()
	fastTick <- time.Now()
	time.Sleep(50 * time.Millisecond)
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if runtime.startCalls != 0 {
		t.Fatalf("runtime.startCalls = %d, want 0 (healthy runtime should not restart)", runtime.startCalls)
	}
}

func TestWatchdog_Run_OutputChangeResetsStaleTimer(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 3)
	clock := &watchdogClockStub{
		times: []time.Time{
			time.Unix(0, 0), // tick 1: sets lastOutputChanged
			time.Unix(5, 0), // tick 2: output changes → resets timer to 5s
			time.Unix(9, 0), // tick 3: output same, 4s since change < 10s threshold
		},
	}
	runtime := &watchdogRuntimeSpy{
		outputs: []string{"thinking", "done", "done"},
	}
	watchdog := &Watchdog{
		Runtime:              runtime,
		Clock:                clock,
		FastTicker:           fastTick,
		ClaudeStaleThreshold: 10 * time.Second,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	fastTick <- time.Now()
	time.Sleep(10 * time.Millisecond)
	fastTick <- time.Now()
	time.Sleep(10 * time.Millisecond)
	fastTick <- time.Now()
	time.Sleep(50 * time.Millisecond)
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if runtime.startCalls != 0 {
		t.Fatalf("runtime.startCalls = %d, want 0 (output changed mid-way, timer reset)", runtime.startCalls)
	}
}

func TestWatchdog_Run_RuntimeRestartErrorPropagates(t *testing.T) {
	t.Parallel()

	restartErr := errors.New("restart failed")
	fastTick := make(chan time.Time, 1)
	runtime := &watchdogRuntimeSpy{
		healthErr: errBoom,
		startErr:  restartErr,
	}
	watchdog := &Watchdog{
		Runtime:    runtime,
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	fastTick <- time.Now()

	err := <-done
	if err == nil {
		t.Fatal("watchdog.Run() should return error when runtime restart fails")
	}
	if !strings.Contains(err.Error(), "restart runtime") {
		t.Fatalf("error = %v, want wrapped 'restart runtime'", err)
	}
	if !errors.Is(err, restartErr) {
		t.Fatalf("error should wrap restartErr, got %v", err)
	}
}

func TestWatchdog_Run_TransportRegisterErrorPropagates(t *testing.T) {
	t.Parallel()

	registerErr := errors.New("register failed")
	fastTick := make(chan time.Time, 1)
	transport := &failingRegisterTransportSpy{
		heartbeatErr: errBoom,
		registerErr:  registerErr,
	}
	watchdog := &Watchdog{
		Transport:  transport,
		Register:   RegisterRequest{Name: "mon"},
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	fastTick <- time.Now()

	err := <-done
	if err == nil {
		t.Fatal("watchdog.Run() should return error when transport register fails")
	}
	if !strings.Contains(err.Error(), "re-register transport") {
		t.Fatalf("error = %v, want wrapped 're-register transport'", err)
	}
	if !errors.Is(err, registerErr) {
		t.Fatalf("error should wrap registerErr, got %v", err)
	}
}

func TestWatchdog_Run_PeriodicRegisterCadence(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 6)
	transport := &cadenceTransportSpy{
		registerCh: make(chan struct{}, 10),
	}
	watchdog := &Watchdog{
		Transport:  transport,
		Register:   RegisterRequest{Name: "mon"},
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	for i := 0; i < 6; i++ {
		fastTick <- time.Now()
		time.Sleep(10 * time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond)
	cancel()

	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}
	if transport.heartbeatCalls != 6 {
		t.Fatalf("heartbeatCalls = %d, want 6", transport.heartbeatCalls)
	}
	// Register on fastTickCount==1 and fastTickCount==5
	if transport.registerCalls != 2 {
		t.Fatalf("registerCalls = %d, want 2 (tick 1 and tick 5)", transport.registerCalls)
	}
	if len(transport.registerTicks) != 2 || transport.registerTicks[0] != 1 || transport.registerTicks[1] != 5 {
		t.Fatalf("registerTicks = %v, want [1, 5]", transport.registerTicks)
	}
}

func TestWatchdog_RestartCap_StopsAfterMaxRestarts(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 20)
	// Clock advances enough to pass any backoff
	times := make([]time.Time, 20)
	for i := range times {
		times[i] = time.Unix(int64(i*600), 0) // 10min apart, always past backoff
	}
	clock := &watchdogClockStub{times: times}
	runtime := &watchdogRuntimeSpy{healthErr: errBoom, startCh: make(chan struct{}, 20)}
	watchdog := &Watchdog{
		Runtime:     runtime,
		Clock:       clock,
		FastTicker:  fastTick,
		MaxRestarts: 3,
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	// Send enough ticks to exceed the cap
	for i := 0; i < 5; i++ {
		fastTick <- time.Now()
		time.Sleep(10 * time.Millisecond)
	}

	err := <-done
	if err == nil {
		t.Fatal("watchdog should return error when restart cap is reached")
	}
	if !strings.Contains(err.Error(), "restart cap reached") {
		t.Fatalf("error = %v, want 'restart cap reached'", err)
	}
	if runtime.startCalls != 3 {
		t.Fatalf("startCalls = %d, want 3 (cap)", runtime.startCalls)
	}
}

func TestWatchdog_RestartBackoff_SkipsWhenTooSoon(t *testing.T) {
	t.Parallel()

	fastTick := make(chan time.Time, 5)
	// Ticks 1 second apart — too fast for backoff
	times := make([]time.Time, 10)
	for i := range times {
		times[i] = time.Unix(int64(i), 0) // 1s apart
	}
	clock := &watchdogClockStub{times: times}
	runtime := &watchdogRuntimeSpy{healthErr: errBoom, startCh: make(chan struct{}, 5)}
	watchdog := &Watchdog{
		Runtime:    runtime,
		Clock:      clock,
		FastTicker: fastTick,
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- watchdog.Run(ctx) }()

	// First tick triggers immediate restart (no backoff for restartCount=0)
	fastTick <- time.Now()
	<-runtime.startCh

	// Second tick: restartCount=1, backoff=30s, only 1s elapsed → skipped
	fastTick <- time.Now()
	time.Sleep(20 * time.Millisecond)

	// Third tick: same, still within backoff → skipped
	fastTick <- time.Now()
	time.Sleep(20 * time.Millisecond)

	cancel()
	if err := <-done; err != nil {
		t.Fatalf("watchdog.Run() error = %v", err)
	}

	// Only the first restart should have fired; the rest hit backoff
	if runtime.startCalls != 1 {
		t.Fatalf("startCalls = %d, want 1 (subsequent restarts in backoff)", runtime.startCalls)
	}
}

func TestWatchdog_Run_CancellationReturnsNil(t *testing.T) {
	t.Parallel()

	watchdog := &Watchdog{
		Runtime: &watchdogRuntimeSpy{},
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := watchdog.Run(ctx)
	if err != nil {
		t.Fatalf("watchdog.Run() = %v, want nil on cancellation", err)
	}
}

// ---------------------------------------------------------------------------
// Spy: failingRegisterTransportSpy — heartbeat and register both fail
// ---------------------------------------------------------------------------

type failingRegisterTransportSpy struct {
	heartbeatErr error
	registerErr  error
}

func (f *failingRegisterTransportSpy) Connect(context.Context, string) error { return nil }
func (f *failingRegisterTransportSpy) Register(context.Context, RegisterRequest) (RegisterResponse, error) {
	return RegisterResponse{}, f.registerErr
}
func (f *failingRegisterTransportSpy) SendMessage(context.Context, WorkerMessage) error { return nil }
func (f *failingRegisterTransportSpy) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, nil
}
func (f *failingRegisterTransportSpy) StreamJSONL(context.Context, JSONLChunk) error { return nil }
func (f *failingRegisterTransportSpy) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, nil
}
func (f *failingRegisterTransportSpy) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", nil
}
func (f *failingRegisterTransportSpy) Heartbeat(context.Context) error { return f.heartbeatErr }
func (f *failingRegisterTransportSpy) Close() error                    { return nil }

// ---------------------------------------------------------------------------
// Spy: cadenceTransportSpy — tracks which tick triggered register
// ---------------------------------------------------------------------------

type cadenceTransportSpy struct {
	heartbeatCalls int
	registerCalls  int
	registerTicks  []int
	registerCh     chan struct{}
}

func (c *cadenceTransportSpy) Connect(context.Context, string) error { return nil }
func (c *cadenceTransportSpy) Register(_ context.Context, _ RegisterRequest) (RegisterResponse, error) {
	c.registerCalls++
	c.registerTicks = append(c.registerTicks, c.heartbeatCalls)
	if c.registerCh != nil {
		c.registerCh <- struct{}{}
	}
	return RegisterResponse{OK: true}, nil
}
func (c *cadenceTransportSpy) SendMessage(context.Context, WorkerMessage) error { return nil }
func (c *cadenceTransportSpy) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, nil
}
func (c *cadenceTransportSpy) StreamJSONL(context.Context, JSONLChunk) error { return nil }
func (c *cadenceTransportSpy) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, nil
}
func (c *cadenceTransportSpy) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", nil
}
func (c *cadenceTransportSpy) Heartbeat(context.Context) error {
	c.heartbeatCalls++
	return nil
}
func (c *cadenceTransportSpy) Close() error { return nil }

// ---------------------------------------------------------------------------
// Existing spy types
// ---------------------------------------------------------------------------

type watchdogRuntimeSpy struct {
	startCalls int
	startErr   error
	healthErr  error
	startCh    chan struct{}
	lastOutput string
	outputs    []string
	outputIdx  int
}

func (s *watchdogRuntimeSpy) Start() error {
	s.startCalls++
	if s.startCh != nil {
		s.startCh <- struct{}{}
	}
	return s.startErr
}

func (s *watchdogRuntimeSpy) Send(string) error {
	return nil
}

func (s *watchdogRuntimeSpy) Health() error {
	return s.healthErr
}

func (s *watchdogRuntimeSpy) LastOutput() (string, error) {
	if len(s.outputs) > 0 && s.outputIdx < len(s.outputs) {
		out := s.outputs[s.outputIdx]
		s.outputIdx++
		return out, nil
	}
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

// ---------------------------------------------------------------------------
// RestartPolicy tests
// ---------------------------------------------------------------------------

func TestExponentialBackoffPolicy_FirstRestartIsImmediate(t *testing.T) {
	t.Parallel()

	policy := NewExponentialBackoffPolicy(10, nil)
	ok, wait := policy.ShouldRestart()
	if !ok {
		t.Fatal("first restart should be allowed")
	}
	if wait != 0 {
		t.Fatalf("first restart wait = %v, want 0", wait)
	}
}

func TestExponentialBackoffPolicy_BackoffDoubles(t *testing.T) {
	t.Parallel()

	clock := &watchdogClockStub{times: make([]time.Time, 20)}
	for i := range clock.times {
		clock.times[i] = time.Unix(int64(i*600), 0) // 10min apart
	}
	policy := NewExponentialBackoffPolicy(10, clock)

	// First restart
	policy.RecordRestart()

	// Second: backoff should be 30s
	ok, wait := policy.ShouldRestart()
	if !ok {
		t.Fatal("second restart should be allowed")
	}
	if wait != 30*time.Second {
		t.Fatalf("second restart backoff = %v, want 30s", wait)
	}

	policy.RecordRestart()

	// Third: backoff should be 60s
	ok, wait = policy.ShouldRestart()
	if !ok {
		t.Fatal("third restart should be allowed")
	}
	if wait != 60*time.Second {
		t.Fatalf("third restart backoff = %v, want 60s", wait)
	}
}

func TestExponentialBackoffPolicy_CapsAt5Min(t *testing.T) {
	t.Parallel()

	clock := &watchdogClockStub{times: make([]time.Time, 20)}
	for i := range clock.times {
		clock.times[i] = time.Unix(int64(i*600), 0)
	}
	policy := NewExponentialBackoffPolicy(20, clock)

	// Record 10 restarts to push backoff past 5 minutes
	for i := 0; i < 10; i++ {
		policy.RecordRestart()
	}

	ok, wait := policy.ShouldRestart()
	if !ok {
		t.Fatal("restart should still be allowed (under cap)")
	}
	if wait != 5*time.Minute {
		t.Fatalf("backoff = %v, want 5m (capped)", wait)
	}
}

func TestExponentialBackoffPolicy_ReturnsNotOKAfterCap(t *testing.T) {
	t.Parallel()

	policy := NewExponentialBackoffPolicy(3, nil)
	for i := 0; i < 3; i++ {
		policy.RecordRestart()
	}

	ok, _ := policy.ShouldRestart()
	if ok {
		t.Fatal("restart should be denied after cap reached")
	}
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
