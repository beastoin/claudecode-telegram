package forge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// BridgeConnector communicates through the claudecode-telegram bridge.
// The bridge manages tmux injection externally — this connector does NOT
// receive messages itself. It only handles:
//   - Registration with the bridge
//   - Sending responses back to the bridge
//   - Team discovery (/workers)
//   - Worker-to-worker messaging (via bridge routing)
//
// Message flow: Bridge -> tmux (external) -> Claude reads from tmux pane
// Response flow: Claude hook -> this connector -> bridge /response endpoint
type BridgeConnector struct {
	bridgeURL  string
	name       string
	client     *http.Client
	transport  Transport
}

func init() {
	RegisterConnector("bridge", func() Connector {
		return &BridgeConnector{}
	})
}

func (b *BridgeConnector) Type() string { return "bridge" }

func (b *BridgeConnector) Capabilities() Caps {
	return CapText | CapFiles | CapTeamDiscovery | CapWorkerToWorker | CapMarkdown
}

func (b *BridgeConnector) Requirements() Reqs {
	return ReqTmuxReady | ReqTransport
}

func (b *BridgeConnector) Init(ctx context.Context, cfg ConnectorConfig) error {
	b.name = cfg.WorkerName
	b.bridgeURL = cfg.BridgeURL
	if b.bridgeURL == "" {
		b.bridgeURL = cfg.Config["BRIDGE_URL"]
	}
	b.client = &http.Client{}

	// Health check the bridge
	if b.bridgeURL != "" {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, b.bridgeURL+"/", nil)
		if err != nil {
			return fmt.Errorf("bridge health check: %w", err)
		}
		resp, err := b.client.Do(req)
		if err != nil {
			return fmt.Errorf("bridge unreachable: %w", err)
		}
		resp.Body.Close()
	}

	// Register with bridge
	if b.bridgeURL != "" && b.name != "" {
		payload, _ := json.Marshal(map[string]string{
			"name": b.name,
		})
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.bridgeURL+"/register", bytes.NewReader(payload))
		if err != nil {
			return fmt.Errorf("bridge register: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := b.client.Do(req)
		if err != nil {
			return fmt.Errorf("bridge register: %w", err)
		}
		resp.Body.Close()
	}

	return nil
}

func (b *BridgeConnector) Send(ctx context.Context, resp Response) error {
	if b.bridgeURL == "" {
		return fmt.Errorf("bridge URL not configured")
	}

	payload, _ := json.Marshal(map[string]string{
		"text":   resp.Text,
		"source": b.name,
	})

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.bridgeURL+"/response", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	httpResp, err := b.client.Do(req)
	if err != nil {
		return fmt.Errorf("bridge send: %w", err)
	}
	defer httpResp.Body.Close()

	if httpResp.StatusCode != http.StatusOK {
		return fmt.Errorf("bridge send failed: HTTP %d", httpResp.StatusCode)
	}
	return nil
}

func (b *BridgeConnector) Close() error {
	return nil
}

// ExternalReceiver marker — bridge injects messages into tmux directly.
func (b *BridgeConnector) IsExternal() {}

// ---------------------------------------------------------------------------
// TeamAware implementation
// ---------------------------------------------------------------------------

func (b *BridgeConnector) DiscoverWorkers(ctx context.Context) ([]WorkerInfo, error) {
	if b.bridgeURL == "" {
		return nil, fmt.Errorf("bridge URL not configured")
	}

	url := fmt.Sprintf("%s/workers?from=%s", b.bridgeURL, b.name)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}

	resp, err := b.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("discover workers: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var raw []struct {
		Name        string `json:"name"`
		SendExample string `json:"send_example"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, fmt.Errorf("parse workers response: %w", err)
	}

	workers := make([]WorkerInfo, len(raw))
	for i, w := range raw {
		workers[i] = WorkerInfo{Name: w.Name, SendExample: w.SendExample}
	}
	return workers, nil
}

func (b *BridgeConnector) SendToWorker(ctx context.Context, target string, msg string) error {
	if b.bridgeURL == "" {
		return fmt.Errorf("bridge URL not configured")
	}

	payload, _ := json.Marshal(map[string]string{
		"from": b.name,
		"to":   target,
		"text": msg,
	})

	url := fmt.Sprintf("%s/send", b.bridgeURL)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.client.Do(req)
	if err != nil {
		return fmt.Errorf("send to worker %q: %w", target, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("send to worker %q: HTTP %d", target, resp.StatusCode)
	}
	return nil
}

// Verify interface compliance at compile time.
var (
	_ Connector        = (*BridgeConnector)(nil)
	_ ExternalReceiver = (*BridgeConnector)(nil)
	_ TeamAware        = (*BridgeConnector)(nil)
)
