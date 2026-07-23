package connector

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// Telegram webhook connector behavior tests
// ---------------------------------------------------------------------------

func TestTelegramConnector_Init_VerifiesToken(t *testing.T) {
	t.Parallel()

	var getMeHits int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/getMe") {
			getMeHits++
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	c := &TelegramConnector{}
	err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "test-tg",
		Config: map[string]string{
			"TELEGRAM_BOT_TOKEN": "123:fake",
			"TELEGRAM_API_BASE":  server.URL,
		},
	})
	if err != nil {
		t.Fatalf("Init() error = %v", err)
	}
	if getMeHits != 1 {
		t.Fatalf("getMe hits = %d, want 1", getMeHits)
	}
}

func TestTelegramConnector_Init_BadTokenFails(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer server.Close()

	c := &TelegramConnector{}
	err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "test-tg",
		Config: map[string]string{
			"TELEGRAM_BOT_TOKEN": "bad-token",
			"TELEGRAM_API_BASE":  server.URL,
		},
	})
	if err == nil {
		t.Fatal("Init() with bad token should return error")
	}
	if !strings.Contains(err.Error(), "getMe") {
		t.Fatalf("error = %v, want to mention getMe", err)
	}
}

func TestTelegramConnector_Send_PostsToAPI(t *testing.T) {
	t.Parallel()

	var gotPath string
	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &TelegramConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
	}

	err := c.Send(context.Background(), Response{Text: "hello world"})
	if err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	if gotPath != "/bot123:fake/sendMessage" {
		t.Fatalf("path = %q, want /bot123:fake/sendMessage", gotPath)
	}
	if gotPayload["chat_id"] != "42" {
		t.Fatalf("chat_id = %v, want 42", gotPayload["chat_id"])
	}
	if gotPayload["text"] != "hello world" {
		t.Fatalf("text = %v, want hello world", gotPayload["text"])
	}
	if gotPayload["parse_mode"] != "Markdown" {
		t.Fatalf("parse_mode = %v, want Markdown", gotPayload["parse_mode"])
	}
}

func TestTelegramConnector_Webhook_FiltersByChatID(t *testing.T) {
	t.Parallel()

	var delivered []InboundMessage
	c := &TelegramConnector{
		token:  "123:fake",
		chatID: "42",
		deliver: func(msg InboundMessage) {
			delivered = append(delivered, msg)
		},
		mux: http.NewServeMux(),
	}
	c.mux.HandleFunc("/webhook", c.handleWebhook)

	// Send message from wrong chat
	wrongChat := `{"message":{"text":"wrong","from":{"username":"bob"},"chat":{"id":99}}}`
	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(wrongChat))
	w := httptest.NewRecorder()
	c.mux.ServeHTTP(w, req)

	if len(delivered) != 0 {
		t.Fatalf("expected no messages from wrong chat, got %d", len(delivered))
	}

	// Send message from correct chat
	rightChat := `{"message":{"text":"hello","from":{"username":"alice"},"chat":{"id":42}}}`
	req = httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(rightChat))
	w = httptest.NewRecorder()
	c.mux.ServeHTTP(w, req)

	if len(delivered) != 1 {
		t.Fatalf("expected 1 message from correct chat, got %d", len(delivered))
	}
	if delivered[0].Text != "hello" {
		t.Fatalf("delivered text = %q, want hello", delivered[0].Text)
	}
	if delivered[0].From != "alice" {
		t.Fatalf("delivered from = %q, want alice", delivered[0].From)
	}
}

func TestTelegramConnector_ImplementsInboundSink(t *testing.T) {
	t.Parallel()

	var c Connector = &TelegramConnector{}
	sink, ok := c.(InboundSink)
	if !ok {
		t.Fatal("TelegramConnector must implement InboundSink")
	}

	var got InboundMessage
	sink.SetInboundSink(func(msg InboundMessage) { got = msg })

	c.(*TelegramConnector).deliver(InboundMessage{Text: "test", From: "user"})
	if got.Text != "test" {
		t.Fatalf("sink got text = %q, want test", got.Text)
	}
}

func TestConnectorHost_WiresInboundSink(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &TelegramConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
		mux:        http.NewServeMux(),
	}
	c.Init(context.Background(), ConnectorConfig{
		WorkerName: "test",
		Config: map[string]string{
			"TELEGRAM_BOT_TOKEN": "123:fake",
			"TELEGRAM_API_BASE":  server.URL,
		},
	})

	rt := &connStubRuntime{}
	host := NewConnectorHost(c, rt, ConnectorConfig{WorkerName: "test"})

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately, we just test the wiring

	host.Run(ctx)

	if c.deliver == nil {
		t.Fatal("ConnectorHost.Run should have wired InboundSink on TelegramConnector")
	}
}

func TestConnectorHost_InboundSink_CommandsWork(t *testing.T) {
	t.Parallel()

	var delivered []InboundMessage
	sinkConn := &sinkPushStub{
		deliver: func(msg InboundMessage) {
			delivered = append(delivered, msg)
		},
	}

	rt := &connStubRuntime{}
	host := NewConnectorHost(sinkConn, rt, ConnectorConfig{WorkerName: "test"})

	var cmdCalled bool
	host.RegisterCommand(CommandSpec{Name: "test"}, func(_ context.Context, _ CommandInvocation) CommandResult {
		cmdCalled = true
		return CommandResult{Text: "ok"}
	})

	// Wire the sink (normally done in Run)
	if sink, ok := host.connector.(InboundSink); ok {
		sink.SetInboundSink(host.DeliverInbound)
	}

	// Deliver a command through the sink
	sinkConn.deliver(InboundMessage{Text: "/test"})
	if !cmdCalled {
		t.Fatal("command should have been dispatched through InboundSink")
	}

	// Deliver a regular message
	sinkConn.deliver(InboundMessage{Text: "hello", From: "user"})
	sent := rt.getSent()
	if len(sent) != 1 || sent[0] != "[user] hello" {
		t.Fatalf("sent = %v, want [\"[user] hello\"]", sent)
	}
}

// ---------------------------------------------------------------------------
// Telegram poll connector behavior tests
// ---------------------------------------------------------------------------

func TestTelegramPollConnector_Poll_AdvancesOffset(t *testing.T) {
	t.Parallel()

	var pollCount int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/getUpdates") {
			pollCount++
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]interface{}{
				"ok": true,
				"result": []map[string]interface{}{
					{
						"update_id": 100,
						"message": map[string]interface{}{
							"text": "msg1",
							"from": map[string]string{"username": "alice"},
							"chat": map[string]interface{}{"id": 42},
						},
					},
					{
						"update_id": 101,
						"message": map[string]interface{}{
							"text": "msg2",
							"from": map[string]string{"username": "bob"},
							"chat": map[string]interface{}{"id": 42},
						},
					},
				},
			})
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &TelegramPollConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
		inboxDir:   t.TempDir(),
	}

	msgs, err := c.Poll(context.Background())
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 2 {
		t.Fatalf("len(msgs) = %d, want 2", len(msgs))
	}
	if msgs[0].Text != "msg1" {
		t.Fatalf("msgs[0].Text = %q, want msg1", msgs[0].Text)
	}
	if msgs[1].From != "bob" {
		t.Fatalf("msgs[1].From = %q, want bob", msgs[1].From)
	}
	if c.offset != 102 {
		t.Fatalf("offset = %d, want 102", c.offset)
	}
}

func TestTelegramPollConnector_Poll_FiltersByChatID(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"ok": true,
			"result": []map[string]interface{}{
				{
					"update_id": 200,
					"message": map[string]interface{}{
						"text": "wrong chat",
						"from": map[string]string{"username": "eve"},
						"chat": map[string]interface{}{"id": 999},
					},
				},
				{
					"update_id": 201,
					"message": map[string]interface{}{
						"text": "right chat",
						"from": map[string]string{"username": "alice"},
						"chat": map[string]interface{}{"id": 42},
					},
				},
			},
		})
	}))
	defer server.Close()

	c := &TelegramPollConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
		inboxDir:   t.TempDir(),
	}

	msgs, err := c.Poll(context.Background())
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("len(msgs) = %d, want 1 (filtered)", len(msgs))
	}
	if msgs[0].Text != "right chat" {
		t.Fatalf("msgs[0].Text = %q, want 'right chat'", msgs[0].Text)
	}
}

func TestTelegramPollConnector_Poll_OKFalseReturnsError(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":false,"description":"Unauthorized"}`))
	}))
	defer server.Close()

	c := &TelegramPollConnector{
		token:      "bad",
		apiBaseURL: server.URL,
		client:     server.Client(),
		inboxDir:   t.TempDir(),
	}

	_, err := c.Poll(context.Background())
	if err == nil {
		t.Fatal("Poll() with ok=false should return error")
	}
	if !strings.Contains(err.Error(), "ok=false") {
		t.Fatalf("error = %v, want to mention ok=false", err)
	}
}

func TestTelegramPollConnector_SendText_PostsJSON(t *testing.T) {
	t.Parallel()

	var gotPath string
	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true,"result":{"message_id":1}}`))
	}))
	defer server.Close()

	c := &TelegramPollConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
		inboxDir:   t.TempDir(),
	}

	err := c.Send(context.Background(), Response{Text: "test msg", Target: "42"})
	if err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	if gotPath != "/bot123:fake/sendMessage" {
		t.Fatalf("path = %q, want /bot123:fake/sendMessage", gotPath)
	}
	if gotPayload["chat_id"] != "42" {
		t.Fatalf("chat_id = %v, want 42", gotPayload["chat_id"])
	}
	if gotPayload["parse_mode"] != "HTML" {
		t.Fatalf("parse_mode = %v, want HTML", gotPayload["parse_mode"])
	}
}

func TestTelegramPollConnector_SendAuthPrompt_ForceReply(t *testing.T) {
	t.Parallel()

	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true,"result":{"message_id":77}}`))
	}))
	defer server.Close()

	c := &TelegramPollConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
		inboxDir:   t.TempDir(),
	}

	result, err := c.SendAuthPrompt(context.Background(), AuthPromptRequest{
		WorkerName: "mon-the-fox",
		URL:        "https://claude.ai/oauth/test",
	})
	if err != nil {
		t.Fatalf("SendAuthPrompt() error = %v", err)
	}

	if result.MessageID != "77" {
		t.Fatalf("MessageID = %q, want 77", result.MessageID)
	}

	// Verify force_reply markup
	markup, ok := gotPayload["reply_markup"].(map[string]interface{})
	if !ok {
		t.Fatal("reply_markup missing or wrong type")
	}
	if markup["force_reply"] != true {
		t.Fatalf("force_reply = %v, want true", markup["force_reply"])
	}
	if markup["input_field_placeholder"] != "Paste auth code here" {
		t.Fatalf("placeholder = %v, want 'Paste auth code here'", markup["input_field_placeholder"])
	}

	// Verify text contains worker name and URL
	text, _ := gotPayload["text"].(string)
	if !strings.Contains(text, "mon-the-fox") {
		t.Fatalf("text should contain worker name, got %q", text)
	}
	if !strings.Contains(text, "https://claude.ai/oauth/test") {
		t.Fatalf("text should contain URL, got %q", text)
	}
}

func TestTelegramPollConnector_Poll_PopulatesReplyToID(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"ok": true,
			"result": []map[string]interface{}{
				{
					"update_id": 300,
					"message": map[string]interface{}{
						"message_id": 88,
						"text":       "my-code",
						"from":       map[string]string{"username": "manager"},
						"chat":       map[string]interface{}{"id": 42},
						"reply_to_message": map[string]interface{}{
							"message_id": 77,
							"text":       "Auth required for mon...",
						},
					},
				},
			},
		})
	}))
	defer server.Close()

	c := &TelegramPollConnector{
		token:      "123:fake",
		chatID:     "42",
		apiBaseURL: server.URL,
		client:     server.Client(),
		inboxDir:   t.TempDir(),
	}

	msgs, err := c.Poll(context.Background())
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("len(msgs) = %d, want 1", len(msgs))
	}

	msg := msgs[0]
	if msg.MessageID != "88" {
		t.Fatalf("MessageID = %q, want 88", msg.MessageID)
	}
	if msg.ReplyToID != "77" {
		t.Fatalf("ReplyToID = %q, want 77", msg.ReplyToID)
	}
}

// ---------------------------------------------------------------------------
// Slack connector behavior tests
// ---------------------------------------------------------------------------

func TestSlackConnector_Send_AuthHeader(t *testing.T) {
	t.Parallel()

	var gotAuth string
	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	c := &SlackConnector{
		botToken:   "xoxb-test-token",
		channel:    "C123",
		apiBaseURL: server.URL,
		client:     server.Client(),
	}

	err := c.Send(context.Background(), Response{Text: "hello slack"})
	if err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	if gotAuth != "Bearer xoxb-test-token" {
		t.Fatalf("Authorization = %q, want Bearer xoxb-test-token", gotAuth)
	}
	if gotPayload["channel"] != "C123" {
		t.Fatalf("channel = %v, want C123", gotPayload["channel"])
	}
	if gotPayload["text"] != "hello slack" {
		t.Fatalf("text = %v, want hello slack", gotPayload["text"])
	}
}

func TestSlackConnector_Send_ErrorResponse(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":false,"error":"channel_not_found"}`))
	}))
	defer server.Close()

	c := &SlackConnector{
		botToken:   "xoxb-test",
		channel:    "C999",
		apiBaseURL: server.URL,
		client:     server.Client(),
	}

	err := c.Send(context.Background(), Response{Text: "hi"})
	if err == nil {
		t.Fatal("Send() with ok=false should return error")
	}
	if !strings.Contains(err.Error(), "channel_not_found") {
		t.Fatalf("error = %v, want to contain channel_not_found", err)
	}
}

func TestSlackConnector_Send_UsesTargetOverChannel(t *testing.T) {
	t.Parallel()

	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	c := &SlackConnector{
		botToken:   "xoxb-test",
		channel:    "C-default",
		apiBaseURL: server.URL,
		client:     server.Client(),
	}

	c.Send(context.Background(), Response{Text: "hi", Target: "C-override"})

	if gotPayload["channel"] != "C-override" {
		t.Fatalf("channel = %v, want C-override", gotPayload["channel"])
	}
}

func TestSlackConnector_GetWebSocketURL(t *testing.T) {
	t.Parallel()

	var gotAuth string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok":true,"url":"wss://test.slack.com/link"}`))
	}))
	defer server.Close()

	c := &SlackConnector{
		appToken:   "xapp-test-token",
		apiBaseURL: server.URL,
		client:     server.Client(),
	}

	url, err := c.getWebSocketURL(context.Background())
	if err != nil {
		t.Fatalf("getWebSocketURL() error = %v", err)
	}
	if url != "wss://test.slack.com/link" {
		t.Fatalf("url = %q, want wss://test.slack.com/link", url)
	}
	if gotAuth != "Bearer xapp-test-token" {
		t.Fatalf("Authorization = %q, want Bearer xapp-test-token", gotAuth)
	}
}

// ---------------------------------------------------------------------------
// WhatsApp connector behavior tests
// ---------------------------------------------------------------------------

func TestWhatsAppConnector_Send_AuthAndPayload(t *testing.T) {
	t.Parallel()

	var gotAuth string
	var gotPath string
	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &WhatsAppConnector{
		token:   "wa-token-123",
		phoneID: "phone42",
		apiURL:  server.URL,
		client:  server.Client(),
	}

	err := c.Send(context.Background(), Response{Text: "hello whatsapp", Target: "+1234567890"})
	if err != nil {
		t.Fatalf("Send() error = %v", err)
	}

	if gotAuth != "Bearer wa-token-123" {
		t.Fatalf("Authorization = %q, want Bearer wa-token-123", gotAuth)
	}
	if gotPath != "/phone42/messages" {
		t.Fatalf("path = %q, want /phone42/messages", gotPath)
	}
	if gotPayload["messaging_product"] != "whatsapp" {
		t.Fatalf("messaging_product = %v, want whatsapp", gotPayload["messaging_product"])
	}
	if gotPayload["to"] != "+1234567890" {
		t.Fatalf("to = %v, want +1234567890", gotPayload["to"])
	}
}

func TestWhatsAppConnector_Send_Accepts201(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
	}))
	defer server.Close()

	c := &WhatsAppConnector{
		token:   "tok",
		phoneID: "p1",
		apiURL:  server.URL,
		client:  server.Client(),
	}

	err := c.Send(context.Background(), Response{Text: "hi", Target: "+1"})
	if err != nil {
		t.Fatalf("Send() with 201 should succeed, got %v", err)
	}
}

func TestWhatsAppConnector_Send_ErrorOnBadStatus(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		w.Write([]byte(`{"error":"invalid token"}`))
	}))
	defer server.Close()

	c := &WhatsAppConnector{
		token:   "bad",
		phoneID: "p1",
		apiURL:  server.URL,
		client:  server.Client(),
	}

	err := c.Send(context.Background(), Response{Text: "hi", Target: "+1"})
	if err == nil {
		t.Fatal("Send() with 403 should return error")
	}
	if !strings.Contains(err.Error(), "403") {
		t.Fatalf("error = %v, want to contain 403", err)
	}
}

func TestWhatsAppConnector_SendTemplate_Payload(t *testing.T) {
	t.Parallel()

	var gotPayload map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &gotPayload)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	c := &WhatsAppConnector{
		token:   "tok",
		phoneID: "p1",
		apiURL:  server.URL,
		client:  server.Client(),
	}

	err := c.SendTemplate(context.Background(), "+1234", "welcome_msg", "en", []string{"Alice"})
	if err != nil {
		t.Fatalf("SendTemplate() error = %v", err)
	}

	if gotPayload["type"] != "template" {
		t.Fatalf("type = %v, want template", gotPayload["type"])
	}

	tmpl, ok := gotPayload["template"].(map[string]interface{})
	if !ok {
		t.Fatal("template field missing or wrong type")
	}
	if tmpl["name"] != "welcome_msg" {
		t.Fatalf("template.name = %v, want welcome_msg", tmpl["name"])
	}

	lang, ok := tmpl["language"].(map[string]interface{})
	if !ok {
		t.Fatal("template.language field missing")
	}
	if lang["code"] != "en" {
		t.Fatalf("language.code = %v, want en", lang["code"])
	}
}

// ---------------------------------------------------------------------------
// Contract tests: all registered connector types
// ---------------------------------------------------------------------------

func TestConnectorContract_TypeIsNonEmpty(t *testing.T) {
	t.Parallel()
	for _, name := range ListConnectorTypes() {
		c, _ := NewConnector(name)
		if c.Type() == "" {
			t.Errorf("connector %q: Type() returned empty string", name)
		}
	}
}

func TestConnectorContract_CapabilitiesIncludesText(t *testing.T) {
	t.Parallel()
	for _, name := range ListConnectorTypes() {
		c, _ := NewConnector(name)
		if c.Capabilities()&CapText == 0 {
			t.Errorf("connector %q: must have CapText", name)
		}
	}
}

func TestConnectorContract_ImplementsExactlyOneReceiverPattern(t *testing.T) {
	t.Parallel()
	for _, name := range ListConnectorTypes() {
		c, _ := NewConnector(name)
		count := 0
		if _, ok := c.(ExternalReceiver); ok {
			count++
		}
		if _, ok := c.(PushReceiver); ok {
			count++
		}
		if _, ok := c.(PollReceiver); ok {
			count++
		}
		if _, ok := c.(StreamReceiver); ok {
			count++
		}
		if count != 1 {
			t.Errorf("connector %q: implements %d receiver patterns, want exactly 1", name, count)
		}
	}
}

func TestConnectorContract_CloseIsIdempotent(t *testing.T) {
	t.Parallel()
	for _, name := range ListConnectorTypes() {
		c, _ := NewConnector(name)
		if err := c.Close(); err != nil {
			t.Errorf("connector %q: first Close() error = %v", name, err)
		}
		if err := c.Close(); err != nil {
			t.Errorf("connector %q: second Close() error = %v", name, err)
		}
	}
}
