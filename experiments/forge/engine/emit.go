package engine

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

// EmitOptions holds the configuration for the emit command.
type EmitOptions struct {
	Event       string
	PayloadFile string
	WorkerName  string
	SocketPath  string
}

// ParseEmitArgs parses command-line arguments for the emit subcommand.
func ParseEmitArgs(args []string) (EmitOptions, error) {
	fs := flag.NewFlagSet("emit", flag.ContinueOnError)
	fs.SetOutput(io.Discard)

	var opts EmitOptions
	fs.StringVar(&opts.Event, "event", "", "hook event name (e.g., stop)")
	fs.StringVar(&opts.PayloadFile, "payload-file", "", "path to payload file (reads stdin if omitted)")
	fs.StringVar(&opts.WorkerName, "worker", "", "worker name (defaults to FORGE_WORKER_NAME env)")
	fs.StringVar(&opts.SocketPath, "socket", "", "explicit socket path (overrides auto-detection)")

	if err := fs.Parse(args); err != nil {
		return EmitOptions{}, err
	}

	if opts.Event == "" {
		return EmitOptions{}, fmt.Errorf("--event is required")
	}

	if opts.WorkerName == "" {
		opts.WorkerName = os.Getenv("FORGE_WORKER_NAME")
	}
	if opts.WorkerName == "" {
		return EmitOptions{}, fmt.Errorf("--worker or FORGE_WORKER_NAME is required")
	}

	if opts.SocketPath == "" {
		name := strings.NewReplacer("/", "-", " ", "-", "\\", "-").Replace(opts.WorkerName)
		opts.SocketPath = fmt.Sprintf("/tmp/forge-%s.sock", name)
	}

	return opts, nil
}

// emitPayload is the JSON payload sent to the forge socket.
// Fields intentionally lack JSON tags to match connector.Response serialization.
type emitPayload struct {
	Text string
}

// RunEmit sends a hook payload to the forge socket.
func RunEmit(opts EmitOptions, stdin io.Reader) error {
	var payload []byte
	var err error

	if opts.PayloadFile != "" {
		payload, err = os.ReadFile(opts.PayloadFile)
		if err != nil {
			return fmt.Errorf("read payload file: %w", err)
		}
	} else if stdin != nil {
		payload, err = io.ReadAll(stdin)
		if err != nil {
			return fmt.Errorf("read stdin: %w", err)
		}
	}

	text := strings.TrimSpace(string(payload))
	if text == "" {
		return nil
	}

	body, err := json.Marshal(emitPayload{Text: text})
	if err != nil {
		return fmt.Errorf("marshal response: %w", err)
	}

	client := &http.Client{
		Transport: &http.Transport{
			DialContext: func(_ context.Context, _, _ string) (net.Conn, error) {
				return net.DialTimeout("unix", opts.SocketPath, 5*time.Second)
			},
		},
		Timeout: 10 * time.Second,
	}

	resp, err := client.Post("http://forge/response", "application/json", strings.NewReader(string(body)))
	if err != nil {
		return fmt.Errorf("post to socket %s: %w", opts.SocketPath, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		respBody, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("socket returned %d: %s", resp.StatusCode, string(respBody))
	}

	return nil
}
