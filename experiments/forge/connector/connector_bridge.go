package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type BridgeConnector struct {
	bridgeURL      string
	name           string
	client         *http.Client
	TmuxPrefix     string
	TmuxSession    string
	ActiveWorkers  []string
	Conflict       bool
}

type registerResponse struct {
	OK            bool     `json:"ok"`
	Settings      struct {
		TmuxPrefix  string `json:"tmux_prefix"`
		NodeName    string `json:"node_name"`
		TmuxSession string `json:"tmux_session"`
	} `json:"settings"`
	Conflict      bool     `json:"conflict"`
	ActiveWorkers []string `json:"active_workers"`
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
		defer resp.Body.Close()

		body, err := io.ReadAll(resp.Body)
		if err == nil {
			var reg registerResponse
			if json.Unmarshal(body, &reg) == nil {
				b.TmuxPrefix = reg.Settings.TmuxPrefix
				b.TmuxSession = reg.Settings.TmuxSession
				b.ActiveWorkers = reg.ActiveWorkers
				b.Conflict = reg.Conflict
			}
		}

		if b.Conflict {
			return fmt.Errorf("worker %q already active on bridge (session %s)", b.name, b.TmuxSession)
		}
	}

	return nil
}

func (b *BridgeConnector) Send(ctx context.Context, resp Response) error {
	if b.bridgeURL == "" {
		return fmt.Errorf("bridge URL not configured")
	}

	payload, _ := json.Marshal(map[string]string{
		"text":    resp.Text,
		"source":  b.name,
		"session": b.name,
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

func (b *BridgeConnector) IsExternal() {}

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

var (
	_ Connector        = (*BridgeConnector)(nil)
	_ ExternalReceiver = (*BridgeConnector)(nil)
	_ TeamAware        = (*BridgeConnector)(nil)
)
