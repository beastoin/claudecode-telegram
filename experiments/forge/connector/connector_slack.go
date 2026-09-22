package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

type SlackConnector struct {
	appToken   string
	botToken   string
	channel    string
	name       string
	apiBaseURL string
	client     *http.Client
	msgChan    chan InboundMessage
	cancelFn   context.CancelFunc
}

func (s *SlackConnector) apiBase() string {
	if s.apiBaseURL != "" {
		return s.apiBaseURL
	}
	return "https://slack.com/api"
}

func init() {
	RegisterConnector("slack", func() Connector {
		return &SlackConnector{}
	})
}

func (s *SlackConnector) Type() string { return "slack" }

func (s *SlackConnector) Capabilities() Caps {
	return CapText | CapFiles | CapMarkdown | CapThreads | CapMultiChannel
}

func (s *SlackConnector) Requirements() Reqs {
	return ReqRuntime
}

func (s *SlackConnector) Init(ctx context.Context, cfg ConnectorConfig) error {
	s.appToken = cfg.Config["SLACK_APP_TOKEN"]
	if s.appToken == "" {
		return fmt.Errorf("slack connector requires SLACK_APP_TOKEN in config")
	}
	s.botToken = cfg.Config["SLACK_BOT_TOKEN"]
	if s.botToken == "" {
		return fmt.Errorf("slack connector requires SLACK_BOT_TOKEN in config")
	}
	s.apiBaseURL = cfg.Config["SLACK_API_BASE"]
	s.channel = cfg.Config["SLACK_CHANNEL_ID"]
	s.name = cfg.WorkerName
	s.client = &http.Client{Timeout: 30 * time.Second}
	s.msgChan = make(chan InboundMessage, 64)

	wsCtx, cancel := context.WithCancel(ctx)
	s.cancelFn = cancel
	go s.connectAndStream(wsCtx)

	return nil
}

func (s *SlackConnector) Send(ctx context.Context, resp Response) error {
	channel := resp.Target
	if channel == "" {
		channel = s.channel
	}
	if channel == "" {
		return fmt.Errorf("no channel configured and none in response target")
	}

	payload, _ := json.Marshal(map[string]interface{}{
		"channel":   channel,
		"text":      resp.Text,
		"thread_ts": resp.ThreadID,
	})

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		s.apiBase()+"/chat.postMessage", bytesReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("Authorization", "Bearer "+s.botToken)

	httpResp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer httpResp.Body.Close()

	body, _ := io.ReadAll(httpResp.Body)

	var result struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return fmt.Errorf("slack postMessage parse error: %w", err)
	}
	if !result.OK {
		return fmt.Errorf("slack postMessage failed: %s", result.Error)
	}
	return nil
}

func (s *SlackConnector) Close() error {
	if s.cancelFn != nil {
		s.cancelFn()
	}
	if s.msgChan != nil {
		close(s.msgChan)
		s.msgChan = nil
	}
	return nil
}

func (s *SlackConnector) Messages() <-chan InboundMessage {
	return s.msgChan
}

func (s *SlackConnector) connectAndStream(ctx context.Context) {
	<-ctx.Done()
}

func (s *SlackConnector) getWebSocketURL(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		s.apiBase()+"/apps.connections.open", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+s.appToken)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := s.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	var result struct {
		OK  bool   `json:"ok"`
		URL string `json:"url"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return "", err
	}
	if !result.OK {
		return "", fmt.Errorf("apps.connections.open failed")
	}
	return result.URL, nil
}

var (
	_ Connector      = (*SlackConnector)(nil)
	_ StreamReceiver = (*SlackConnector)(nil)
)
