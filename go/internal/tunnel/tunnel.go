// Package tunnel provides helpers for running cloudflared tunnels.
package tunnel

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"regexp"
	"strings"
	"sync"
)

// Config controls how the tunnel is started.
type Config struct {
	LocalURL string // Local URL to expose (e.g., http://localhost:8081)
}

// Runner starts a tunnel and streams output.
type Runner interface {
	Run(ctx context.Context, cfg Config, stdout io.Writer, onURL func(string) error) error
}

// CloudflaredRunner runs the cloudflared CLI.
type CloudflaredRunner struct {
	Path string
}

// NewRunner creates a cloudflared runner with the given binary path.
func NewRunner(path string) *CloudflaredRunner {
	if strings.TrimSpace(path) == "" {
		path = "cloudflared"
	}
	return &CloudflaredRunner{Path: path}
}

// ErrTunnelURLNotFound indicates that no tunnel URL was detected.
var ErrTunnelURLNotFound = errors.New("tunnel URL not found in cloudflared output")

var tunnelURLPattern = regexp.MustCompile(`https?://[a-zA-Z0-9.-]+\.(trycloudflare\.com|cfargotunnel\.com)\b`)

// ExtractTunnelURL returns the tunnel URL if present in the line.
func ExtractTunnelURL(line string) (string, bool) {
	match := tunnelURLPattern.FindString(line)
	if match == "" {
		return "", false
	}
	return match, true
}

// JoinWebhookURL joins the tunnel base URL with the webhook path.
func JoinWebhookURL(baseURL, webhookPath string) string {
	if webhookPath == "" {
		webhookPath = "/webhook"
	}
	if !strings.HasPrefix(webhookPath, "/") {
		webhookPath = "/" + webhookPath
	}
	baseURL = strings.TrimRight(baseURL, "/")
	return baseURL + webhookPath
}

// Run starts cloudflared, detects the tunnel URL, and keeps streaming output.
func (r *CloudflaredRunner) Run(ctx context.Context, cfg Config, stdout io.Writer, onURL func(string) error) error {
	if cfg.LocalURL == "" {
		return fmt.Errorf("local url is required")
	}
	if stdout == nil {
		stdout = io.Discard
	}

	cmd := exec.CommandContext(ctx, r.Path, "tunnel", "--url", cfg.LocalURL)

	urlCh := make(chan string, 1)
	stopCh := make(chan struct{})
	var urlFound bool
	var urlMu sync.Mutex

	lineBuffer := newLineBuffer(func(line string) {
		if url, ok := ExtractTunnelURL(line); ok {
			urlMu.Lock()
			if !urlFound {
				urlFound = true
				select {
				case urlCh <- url:
				default:
				}
			}
			urlMu.Unlock()
		}
	})

	lockedOut := &lockedWriter{w: stdout}
	mw := io.MultiWriter(lockedOut, lineBuffer)
	cmd.Stdout = mw
	cmd.Stderr = mw

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start cloudflared: %w", err)
	}

	regErrCh := make(chan error, 1)
	go func() {
		var err error
		select {
		case url := <-urlCh:
			if onURL != nil {
				err = onURL(url)
			}
		case <-ctx.Done():
		case <-stopCh:
		}
		if err != nil && cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		regErrCh <- err
	}()

	waitErr := cmd.Wait()
	lineBuffer.Flush()
	close(stopCh)
	regErr := <-regErrCh

	if regErr != nil {
		return regErr
	}
	if waitErr != nil {
		if ctx.Err() != nil {
			return nil
		}
		return fmt.Errorf("cloudflared exited: %w", waitErr)
	}
	urlMu.Lock()
	found := urlFound
	urlMu.Unlock()
	if !found {
		return ErrTunnelURLNotFound
	}
	return nil
}

type lockedWriter struct {
	mu sync.Mutex
	w  io.Writer
}

func (l *lockedWriter) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.w.Write(p)
}

type lineBuffer struct {
	mu     sync.Mutex
	buf    bytes.Buffer
	onLine func(string)
}

func newLineBuffer(onLine func(string)) *lineBuffer {
	if onLine == nil {
		onLine = func(string) {}
	}
	return &lineBuffer{onLine: onLine}
}

func (b *lineBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	n, err := b.buf.Write(p)
	if err != nil {
		return n, err
	}

	for {
		data := b.buf.Bytes()
		idx := bytes.IndexByte(data, '\n')
		if idx == -1 {
			break
		}
		line := string(data[:idx])
		if strings.HasSuffix(line, "\r") {
			line = strings.TrimSuffix(line, "\r")
		}
		b.onLine(line)
		b.buf.Next(idx + 1)
	}

	return n, nil
}

// Flush processes any buffered partial line.
func (b *lineBuffer) Flush() {
	b.mu.Lock()
	defer b.mu.Unlock()

	if b.buf.Len() == 0 {
		return
	}
	line := b.buf.String()
	if strings.HasSuffix(line, "\r") {
		line = strings.TrimSuffix(line, "\r")
	}
	b.onLine(line)
	b.buf.Reset()
}
