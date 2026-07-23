package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

type TelegramConnector struct {
	token      string
	chatID     string
	listenAddr string
	name       string
	apiBaseURL string
	client     *http.Client
	mux        *http.ServeMux
	deliver    func(InboundMessage)
}

func (t *TelegramConnector) apiBase() string {
	if t.apiBaseURL != "" {
		return t.apiBaseURL
	}
	return "https://api.telegram.org"
}

func init() {
	RegisterConnector("telegram", func() Connector {
		return &TelegramConnector{}
	})
}

func (t *TelegramConnector) Type() string { return "telegram" }

func (t *TelegramConnector) Capabilities() Caps {
	return CapText | CapFiles | CapMarkdown | CapVoice
}

func (t *TelegramConnector) Requirements() Reqs {
	return ReqRuntime | ReqHTTPListener
}

func (t *TelegramConnector) Init(ctx context.Context, cfg ConnectorConfig) error {
	t.token = cfg.Config["TELEGRAM_BOT_TOKEN"]
	if t.token == "" {
		return fmt.Errorf("telegram connector requires TELEGRAM_BOT_TOKEN in config")
	}
	t.chatID = cfg.Config["TELEGRAM_CHAT_ID"]
	t.name = cfg.WorkerName
	t.apiBaseURL = cfg.Config["TELEGRAM_API_BASE"]
	t.client = &http.Client{}

	t.listenAddr = cfg.ListenAddr
	if t.listenAddr == "" {
		t.listenAddr = ":8443"
	}

	t.mux = http.NewServeMux()
	t.mux.HandleFunc("/webhook", t.handleWebhook)

	if err := t.verifyToken(ctx); err != nil {
		return fmt.Errorf("telegram getMe failed: %w", err)
	}

	return nil
}

func (t *TelegramConnector) Send(ctx context.Context, resp Response) error {
	chatID := resp.Target
	if chatID == "" {
		chatID = t.chatID
	}
	if chatID == "" {
		return fmt.Errorf("no chat_id configured and none in response target")
	}

	if len(resp.Files) > 0 {
		return t.sendDocument(ctx, chatID, resp)
	}

	payload, _ := json.Marshal(map[string]interface{}{
		"chat_id":    chatID,
		"text":       resp.Text,
		"parse_mode": "Markdown",
	})

	url := fmt.Sprintf("%s/bot%s/sendMessage", t.apiBase(), t.token)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytesReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	httpResp, err := t.client.Do(req)
	if err != nil {
		return err
	}
	defer httpResp.Body.Close()

	if httpResp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(httpResp.Body)
		return fmt.Errorf("telegram sendMessage failed: %s %s", httpResp.Status, string(body))
	}
	return nil
}

func (t *TelegramConnector) Close() error { return nil }

func (t *TelegramConnector) Handler() http.Handler {
	return t.mux
}

func (t *TelegramConnector) ListenAddr() string {
	return t.listenAddr
}

func (t *TelegramConnector) handleWebhook(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}

	var update struct {
		Message *struct {
			Text string `json:"text"`
			From struct {
				Username string `json:"username"`
			} `json:"from"`
			Chat struct {
				ID int64 `json:"id"`
			} `json:"chat"`
		} `json:"message"`
	}
	if err := json.Unmarshal(body, &update); err != nil || update.Message == nil {
		w.WriteHeader(http.StatusOK)
		return
	}

	if t.chatID != "" {
		expectedID := t.chatID
		actualID := fmt.Sprintf("%d", update.Message.Chat.ID)
		if actualID != expectedID {
			w.WriteHeader(http.StatusOK)
			return
		}
	}

	if t.deliver != nil && update.Message.Text != "" {
		t.deliver(InboundMessage{
			Text: update.Message.Text,
			From: update.Message.From.Username,
		})
	}

	w.WriteHeader(http.StatusOK)
}

func (t *TelegramConnector) verifyToken(ctx context.Context) error {
	url := fmt.Sprintf("%s/bot%s/getMe", t.apiBase(), t.token)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}

	resp, err := t.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("getMe returned %s", resp.Status)
	}
	return nil
}

func (t *TelegramConnector) sendDocument(ctx context.Context, chatID string, resp Response) error {
	payload, _ := json.Marshal(map[string]interface{}{
		"chat_id": chatID,
		"caption": resp.Text,
	})

	url := fmt.Sprintf("%s/bot%s/sendDocument", t.apiBase(), t.token)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytesReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	httpResp, err := t.client.Do(req)
	if err != nil {
		return err
	}
	defer httpResp.Body.Close()

	if httpResp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(httpResp.Body)
		return fmt.Errorf("telegram sendDocument failed: %s %s", httpResp.Status, string(body))
	}
	return nil
}

func (t *TelegramConnector) SendAuthPrompt(ctx context.Context, req AuthPromptRequest) (AuthPromptResult, error) {
	chatID := t.chatID
	if chatID == "" {
		return AuthPromptResult{}, fmt.Errorf("no chat_id configured for auth prompt")
	}

	text := fmt.Sprintf("Auth required for *%s*\n\nOpen this URL to authenticate:\n%s\n\nReply to this message with the auth code.", req.WorkerName, req.URL)
	payload, _ := json.Marshal(map[string]interface{}{
		"chat_id":    chatID,
		"text":       text,
		"parse_mode": "Markdown",
		"reply_markup": map[string]interface{}{
			"force_reply":             true,
			"input_field_placeholder": "Paste auth code here",
		},
	})

	url := fmt.Sprintf("%s/bot%s/sendMessage", t.apiBase(), t.token)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytesReader(payload))
	if err != nil {
		return AuthPromptResult{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	httpResp, err := t.client.Do(httpReq)
	if err != nil {
		return AuthPromptResult{}, err
	}
	defer httpResp.Body.Close()

	body, _ := io.ReadAll(httpResp.Body)
	if httpResp.StatusCode != http.StatusOK {
		return AuthPromptResult{}, fmt.Errorf("telegram sendMessage failed: %s %s", httpResp.Status, string(body))
	}

	var result struct {
		OK     bool `json:"ok"`
		Result struct {
			MessageID int64 `json:"message_id"`
		} `json:"result"`
	}
	json.Unmarshal(body, &result)

	return AuthPromptResult{
		MessageID: fmt.Sprintf("%d", result.Result.MessageID),
	}, nil
}

func (t *TelegramConnector) SetInboundSink(fn func(InboundMessage)) {
	t.deliver = fn
}

var (
	_ Connector    = (*TelegramConnector)(nil)
	_ PushReceiver = (*TelegramConnector)(nil)
	_ InboundSink  = (*TelegramConnector)(nil)
	_ AuthPrompter = (*TelegramConnector)(nil)
)
