package forge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// TelegramConnector connects directly to the Telegram Bot API in webhook mode.
// Telegram sends updates to the worker's HTTP server; the worker processes them
// and injects into the runtime.
//
// Implements: Connector + PushReceiver
type TelegramConnector struct {
	token      string
	chatID     string
	listenAddr string
	name       string
	client     *http.Client
	mux        *http.ServeMux
	runtime    Runtime
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
	t.client = &http.Client{}

	t.runtime = cfg.Runtime
	t.listenAddr = cfg.ListenAddr
	if t.listenAddr == "" {
		t.listenAddr = ":8443"
	}

	// Set up webhook handler
	t.mux = http.NewServeMux()
	t.mux.HandleFunc("/webhook", t.handleWebhook)

	// Verify token with getMe
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

	// Handle file sends
	if len(resp.Files) > 0 {
		return t.sendDocument(ctx, chatID, resp)
	}

	payload, _ := json.Marshal(map[string]interface{}{
		"chat_id":    chatID,
		"text":       resp.Text,
		"parse_mode": "Markdown",
	})

	url := fmt.Sprintf("https://api.telegram.org/bot%s/sendMessage", t.token)
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

// PushReceiver implementation

func (t *TelegramConnector) Handler() http.Handler {
	return t.mux
}

func (t *TelegramConnector) ListenAddr() string {
	return t.listenAddr
}

// handleWebhook processes incoming Telegram webhook updates.
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
		w.WriteHeader(http.StatusOK) // Telegram expects 200 even for non-message updates
		return
	}

	// Filter by chat ID if configured
	if t.chatID != "" {
		expectedID := t.chatID
		actualID := fmt.Sprintf("%d", update.Message.Chat.ID)
		if actualID != expectedID {
			w.WriteHeader(http.StatusOK)
			return
		}
	}

	// Inject into runtime
	if t.runtime != nil && update.Message.Text != "" {
		t.runtime.Send(update.Message.Text)
	}

	w.WriteHeader(http.StatusOK)
}

// verifyToken calls getMe to verify the bot token is valid.
func (t *TelegramConnector) verifyToken(ctx context.Context) error {
	url := fmt.Sprintf("https://api.telegram.org/bot%s/getMe", t.token)
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

// sendDocument sends a file via the Telegram sendDocument API.
func (t *TelegramConnector) sendDocument(ctx context.Context, chatID string, resp Response) error {
	// For simplicity, send text alongside the first file reference.
	// Full multipart upload would be needed for actual file data.
	payload, _ := json.Marshal(map[string]interface{}{
		"chat_id": chatID,
		"caption": resp.Text,
	})

	url := fmt.Sprintf("https://api.telegram.org/bot%s/sendDocument", t.token)
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

// Verify interface compliance at compile time.
var (
	_ Connector    = (*TelegramConnector)(nil)
	_ PushReceiver = (*TelegramConnector)(nil)
)
