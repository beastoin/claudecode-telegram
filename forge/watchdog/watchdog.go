package watchdog

import (
	"context"
	"fmt"
	"time"

	tp "github.com/beastoin/claudecode-telegram/forge/transport"
)

type Runtime interface {
	Health() error
	Start() error
}

type RuntimeMonitor interface {
	LastOutput() (string, error)
}

type RegisterRequest = tp.RegisterRequest
type RegisterResponse = tp.RegisterResponse
type Transport = tp.Transport

type BridgeClient interface {
	Alert(message string) error
}

type Clock interface {
	Now() time.Time
}

type IntegrityMonitor interface {
	VerifyCritical() error
}

type RestartPolicy interface {
	ShouldRestart() (ok bool, backoff time.Duration)
	RecordRestart()
}

type ExponentialBackoffPolicy struct {
	maxRestarts  int
	restartCount int
	clock        Clock
}

func NewExponentialBackoffPolicy(maxRestarts int, clock Clock) *ExponentialBackoffPolicy {
	return &ExponentialBackoffPolicy{maxRestarts: maxRestarts, clock: clock}
}

func (p *ExponentialBackoffPolicy) ShouldRestart() (bool, time.Duration) {
	max := p.maxRestarts
	if max <= 0 {
		max = 10
	}
	if p.restartCount >= max {
		return false, 0
	}
	if p.restartCount == 0 {
		return true, 0
	}
	backoff := time.Duration(1<<uint(p.restartCount-1)) * 30 * time.Second
	if backoff > 5*time.Minute {
		backoff = 5 * time.Minute
	}
	return true, backoff
}

func (p *ExponentialBackoffPolicy) RecordRestart() {
	p.restartCount++
}

type Watchdog struct {
	Runtime              Runtime
	Bridge               BridgeClient
	Integrity            IntegrityMonitor
	Transport            Transport
	Register             RegisterRequest
	Clock                Clock
	RestartPolicy        RestartPolicy
	FastInterval         time.Duration
	SlowInterval         time.Duration
	ClaudeStaleThreshold time.Duration
	MaxRestarts          int
	FastTicker           <-chan time.Time
	IntegrityTicker      <-chan time.Time
	fastTickCount        int
	restartCount         int
	lastRestartAt        time.Time
	lastOutput           string
	lastOutputChanged    time.Time
}

func (w *Watchdog) CheckOnce() error {
	if err := w.handleFastTick(context.Background()); err != nil {
		return err
	}
	if err := w.handleIntegrityTick(); err != nil {
		return err
	}
	return nil
}

func (w *Watchdog) now() time.Time {
	if w.Clock == nil {
		return time.Now().UTC()
	}
	return w.Clock.Now()
}

func (w *Watchdog) Run(ctx context.Context) error {
	fastTick, stopFast := w.fastTicker()
	defer stopFast()

	integrityTick, stopIntegrity := w.integrityTicker()
	defer stopIntegrity()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-fastTick:
			if err := w.handleFastTick(ctx); err != nil {
				return err
			}
		case <-integrityTick:
			if err := w.handleIntegrityTick(); err != nil {
				return err
			}
		}
	}
}

func (w *Watchdog) handleFastTick(ctx context.Context) error {
	if w.Runtime != nil {
		if err := w.Runtime.Health(); err != nil {
			if restartErr := w.tryRestart(); restartErr != nil {
				return restartErr
			}
		}
		if monitor, ok := w.Runtime.(RuntimeMonitor); ok {
			output, err := monitor.LastOutput()
			if err != nil {
				return fmt.Errorf("capture runtime output: %w", err)
			}
			now := w.now()
			if w.lastOutputChanged.IsZero() || output != w.lastOutput {
				w.lastOutput = output
				w.lastOutputChanged = now
			} else if now.Sub(w.lastOutputChanged) > w.claudeStaleThreshold() {
				if restartErr := w.tryRestart(); restartErr != nil {
					return restartErr
				}
				w.lastOutputChanged = now
			}
		}
	}

	if w.Transport != nil {
		w.fastTickCount++
		if err := w.Transport.Heartbeat(ctx); err != nil {
			if _, registerErr := w.Transport.Register(ctx, w.Register); registerErr != nil {
				return fmt.Errorf("re-register transport: %w", registerErr)
			}
			return nil
		}
		if w.fastTickCount == 1 || w.fastTickCount%5 == 0 {
			if _, err := w.Transport.Register(ctx, w.Register); err != nil {
				return fmt.Errorf("refresh transport registration: %w", err)
			}
		}
	}

	return nil
}

func (w *Watchdog) maxRestarts() int {
	if w.MaxRestarts > 0 {
		return w.MaxRestarts
	}
	return 10
}

func (w *Watchdog) tryRestart() error {
	maxR := w.maxRestarts()
	if w.restartCount >= maxR {
		return fmt.Errorf("runtime restart cap reached (%d restarts)", maxR)
	}

	if w.restartCount > 0 {
		backoff := time.Duration(1<<uint(w.restartCount-1)) * 30 * time.Second
		if backoff > 5*time.Minute {
			backoff = 5 * time.Minute
		}
		elapsed := w.now().Sub(w.lastRestartAt)
		if elapsed < backoff {
			return nil
		}
	}

	if err := w.Runtime.Start(); err != nil {
		return fmt.Errorf("restart runtime: %w", err)
	}
	w.restartCount++
	w.lastRestartAt = w.now()
	return nil
}

func (w *Watchdog) claudeStaleThreshold() time.Duration {
	if w.ClaudeStaleThreshold > 0 {
		return w.ClaudeStaleThreshold
	}
	return 5 * time.Minute
}

func (w *Watchdog) handleIntegrityTick() error {
	if w.Integrity != nil {
		if err := w.Integrity.VerifyCritical(); err != nil && w.Bridge != nil {
			message := fmt.Sprintf("critical integrity drift detected at %s: %v", w.now().Format(time.RFC3339), err)
			if alertErr := w.Bridge.Alert(message); alertErr != nil {
				return fmt.Errorf("send integrity alert: %w", alertErr)
			}
		}
	}

	return nil
}

func (w *Watchdog) fastTicker() (<-chan time.Time, func()) {
	if w.FastTicker != nil {
		return w.FastTicker, func() {}
	}
	interval := w.FastInterval
	if interval <= 0 {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	return ticker.C, ticker.Stop
}

func (w *Watchdog) integrityTicker() (<-chan time.Time, func()) {
	if w.IntegrityTicker != nil {
		return w.IntegrityTicker, func() {}
	}
	interval := w.SlowInterval
	if interval <= 0 {
		interval = 5 * time.Minute
	}
	ticker := time.NewTicker(interval)
	return ticker.C, ticker.Stop
}
