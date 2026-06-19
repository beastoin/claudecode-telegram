package forge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// WhatsAppConnector connects to the WhatsApp Business API.
// Uses polling to check for new messages (via an intermediate queue or
// the on-premises API pull endpoint).
//
// Implements: Connector + PollReceiver
type WhatsAppConnector struct {
	apiURL  string
	token   string
	phoneID string
	name    string
	client  *http.Client
}

func init() {
	RegisterConnector("whatsapp", func() Connector {
		return &WhatsAppConnector{}
	})
}

func (w *WhatsAppConnector) Type() string { return "whatsapp" }

func (w *WhatsAppConnector) Capabilities() Caps {
	return CapText | CapFiles | CapTemplates | CapVoice
}

func (w *WhatsAppConnector) Requirements() Reqs {
	return ReqRuntime
}

func (w *WhatsAppConnector) Init(ctx context.Context, cfg ConnectorConfig) error {
	w.token = cfg.Config["WHATSAPP_TOKEN"]
	if w.token == "" {
		return fmt.Errorf("whatsapp connector requires WHATSAPP_TOKEN in config")
	}
	w.phoneID = cfg.Config["WHATSAPP_PHONE_ID"]
	if w.phoneID == "" {
		return fmt.Errorf("whatsapp connector requires WHATSAPP_PHONE_ID in config")
	}
	w.apiURL = cfg.Config["WHATSAPP_API_URL"]
	if w.apiURL == "" {
		w.apiURL = "https://graph.facebook.com/v18.0"
	}
	w.name = cfg.WorkerName
	w.client = &http.Client{Timeout: 30 * time.Second}
	return nil
}

func (w *WhatsAppConnector) Send(ctx context.Context, resp Response) error {
	if resp.Target == "" {
		return fmt.Errorf("whatsapp send requires a target phone number")
	}

	// Handle template messages if CapTemplates is relevant
	// For now, send as plain text message
	url := fmt.Sprintf("%s/%s/messages", w.apiURL, w.phoneID)
	payload, _ := json.Marshal(map[string]interface{}{
		"messaging_product": "whatsapp",
		"to":                resp.Target,
		"type":              "text",
		"text":              map[string]string{"body": resp.Text},
	})

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytesReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+w.token)

	httpResp, err := w.client.Do(req)
	if err != nil {
		return err
	}
	defer httpResp.Body.Close()

	if httpResp.StatusCode != http.StatusOK && httpResp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(httpResp.Body)
		return fmt.Errorf("whatsapp send failed: HTTP %d: %s", httpResp.StatusCode, string(body))
	}
	return nil
}

func (w *WhatsAppConnector) Close() error { return nil }

// PollReceiver implementation

func (w *WhatsAppConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
	// WhatsApp Cloud API primarily uses webhooks for receiving messages.
	// For environments where webhook setup isn't possible, this polls
	// an intermediate webhook-to-queue service or uses the WhatsApp
	// On-Premises API's pull endpoint.
	//
	// Placeholder implementation — actual polling depends on the specific
	// message queue or on-premises API endpoint being used.
	return nil, nil
}

func (w *WhatsAppConnector) PollInterval() time.Duration {
	return 5 * time.Second
}

// SendTemplate sends a template message (WhatsApp Business requirement for
// initiating conversations outside the 24-hour window).
func (w *WhatsAppConnector) SendTemplate(ctx context.Context, to string, templateName string, lang string, params []string) error {
	url := fmt.Sprintf("%s/%s/messages", w.apiURL, w.phoneID)

	components := make([]map[string]interface{}, 0)
	if len(params) > 0 {
		parameters := make([]map[string]string, len(params))
		for i, p := range params {
			parameters[i] = map[string]string{"type": "text", "text": p}
		}
		components = append(components, map[string]interface{}{
			"type":       "body",
			"parameters": parameters,
		})
	}

	payload, _ := json.Marshal(map[string]interface{}{
		"messaging_product": "whatsapp",
		"to":                to,
		"type":              "template",
		"template": map[string]interface{}{
			"name": templateName,
			"language": map[string]string{
				"code": lang,
			},
			"components": components,
		},
	})

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytesReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+w.token)

	httpResp, err := w.client.Do(req)
	if err != nil {
		return err
	}
	defer httpResp.Body.Close()

	if httpResp.StatusCode != http.StatusOK && httpResp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(httpResp.Body)
		return fmt.Errorf("whatsapp template send failed: HTTP %d: %s", httpResp.StatusCode, string(body))
	}
	return nil
}

// Verify interface compliance at compile time.
var (
	_ Connector    = (*WhatsAppConnector)(nil)
	_ PollReceiver = (*WhatsAppConnector)(nil)
)
