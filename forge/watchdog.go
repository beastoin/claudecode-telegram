package forge

import (
	"context"
	"fmt"
	"time"
)

type BridgeClient interface {
	Alert(message string) error
}

type Clock interface {
	Now() time.Time
}

type IntegrityMonitor interface {
	VerifyCritical() error
}

type Watchdog struct {
	Runtime              Runtime
	Bridge               BridgeClient
	Integrity            IntegrityMonitor
	Transport            Transport
	Register             RegisterRequest
	Clock                Clock
	FastInterval         time.Duration
	SlowInterval         time.Duration
	ClaudeStaleThreshold time.Duration
	FastTicker           <-chan time.Time
	IntegrityTicker      <-chan time.Time
	fastTickCount        int
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
			if restartErr := w.Runtime.Start(); restartErr != nil {
				return fmt.Errorf("restart runtime: %w", restartErr)
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
				if restartErr := w.Runtime.Start(); restartErr != nil {
					return fmt.Errorf("restart stale runtime: %w", restartErr)
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
