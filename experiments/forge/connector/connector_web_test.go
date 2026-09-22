package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

func initWebConnector(t *testing.T, config map[string]string) *WebConnector {
	t.Helper()
	c := &WebConnector{}
	if config == nil {
		config = map[string]string{}
	}
	if err := c.Init(context.Background(), ConnectorConfig{
		WorkerName: "test-web",
		Config:     config,
	}); err != nil {
		t.Fatalf("Init() error = %v", err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

func webURL(c *WebConnector) string {
	return fmt.Sprintf("http://127.0.0.1:%d", c.Port())
}

func TestWebConnector_Init_BindsRandomPort(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)
	if c.Port() <= 0 {
		t.Fatalf("Port() = %d, want > 0", c.Port())
	}
}

func TestWebConnector_Init_BindsSpecificPort(t *testing.T) {
	t.Parallel()
	port := findFreePort(t)
	c := initWebConnector(t, map[string]string{"WEB_PORT": fmt.Sprintf("%d", port)})
	if c.Port() != port {
		t.Fatalf("Port() = %d, want %d", c.Port(), port)
	}
}

func TestWebConnector_Type(t *testing.T) {
	t.Parallel()
	c := &WebConnector{}
	if c.Type() != "web" {
		t.Fatalf("Type() = %q, want %q", c.Type(), "web")
	}
}

func TestWebConnector_Capabilities(t *testing.T) {
	t.Parallel()
	c := &WebConnector{}
	caps := c.Capabilities()
	if caps&CapText == 0 {
		t.Fatal("should have CapText")
	}
	if caps&CapMarkdown == 0 {
		t.Fatal("should have CapMarkdown")
	}
}

func TestWebConnector_Requirements(t *testing.T) {
	t.Parallel()
	c := &WebConnector{}
	if c.Requirements()&ReqRuntime == 0 {
		t.Fatal("should require ReqRuntime")
	}
}

func TestWebConnector_HealthEndpoint(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	resp, err := http.Get(webURL(c) + "/api/health")
	if err != nil {
		t.Fatalf("GET /api/health error = %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	var health map[string]any
	json.NewDecoder(resp.Body).Decode(&health)
	if health["ok"] != true {
		t.Fatalf("health.ok = %v, want true", health["ok"])
	}
	if health["connector"] != "web" {
		t.Fatalf("health.connector = %v, want 'web'", health["connector"])
	}
	if health["worker"] != "test-web" {
		t.Fatalf("health.worker = %v, want 'test-web'", health["worker"])
	}
}

func TestWebConnector_IndexPage(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	resp, err := http.Get(webURL(c) + "/")
	if err != nil {
		t.Fatalf("GET / error = %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	html := string(body)

	if !strings.Contains(html, "test-web") {
		t.Fatal("index should contain worker name")
	}
	if !strings.Contains(html, "htmx") {
		t.Fatal("index should include HTMX")
	}
	if !strings.Contains(html, "sse-connect") {
		t.Fatal("index should have SSE connection")
	}
	if !strings.Contains(html, "/api/send") {
		t.Fatal("index should have send form")
	}
}

func TestWebConnector_IndexPage_404ForOtherPaths(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	resp, err := http.Get(webURL(c) + "/nonexistent")
	if err != nil {
		t.Fatalf("GET /nonexistent error = %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
}

func TestWebConnector_SendAndPoll_JSON(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	body := strings.NewReader(`{"text":"hello from web","from":"user1"}`)
	resp, err := http.Post(webURL(c)+"/api/send", "application/json", body)
	if err != nil {
		t.Fatalf("POST /api/send error = %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	var result map[string]string
	json.NewDecoder(resp.Body).Decode(&result)
	if result["status"] != "accepted" {
		t.Fatalf("status = %q, want 'accepted'", result["status"])
	}

	msgs, err := c.Poll(context.Background())
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("Poll() got %d messages, want 1", len(msgs))
	}
	if msgs[0].Text != "hello from web" {
		t.Fatalf("msg.Text = %q, want 'hello from web'", msgs[0].Text)
	}
	if msgs[0].From != "user1" {
		t.Fatalf("msg.From = %q, want 'user1'", msgs[0].From)
	}
}

func TestWebConnector_SendAndPoll_Form(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	body := strings.NewReader("text=hello+form&from=user2")
	resp, err := http.Post(webURL(c)+"/api/send", "application/x-www-form-urlencoded", body)
	if err != nil {
		t.Fatalf("POST /api/send error = %v", err)
	}
	resp.Body.Close()

	msgs, err := c.Poll(context.Background())
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 1 || msgs[0].Text != "hello form" {
		t.Fatalf("Poll() = %v, want message 'hello form'", msgs)
	}
}

func TestWebConnector_Send_EmptyTextRejected(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	body := strings.NewReader(`{"text":"","from":"user"}`)
	resp, err := http.Post(webURL(c)+"/api/send", "application/json", body)
	if err != nil {
		t.Fatalf("POST /api/send error = %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("status = %d, want 400 for empty text", resp.StatusCode)
	}
}

func TestWebConnector_Send_DefaultFrom(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	body := strings.NewReader(`{"text":"no from field"}`)
	resp, err := http.Post(webURL(c)+"/api/send", "application/json", body)
	if err != nil {
		t.Fatalf("POST error = %v", err)
	}
	resp.Body.Close()

	msgs, _ := c.Poll(context.Background())
	if len(msgs) != 1 || msgs[0].From != "web" {
		t.Fatalf("default from should be 'web', got %v", msgs)
	}
}

func TestWebConnector_Send_MethodNotAllowed(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	resp, err := http.Get(webURL(c) + "/api/send")
	if err != nil {
		t.Fatalf("GET /api/send error = %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != 405 {
		t.Fatalf("status = %d, want 405", resp.StatusCode)
	}
}

func TestWebConnector_Send_HTMXResponse(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	body := strings.NewReader(`{"text":"htmx message"}`)
	req, _ := http.NewRequest("POST", webURL(c)+"/api/send", body)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("HX-Request", "true")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST error = %v", err)
	}
	defer resp.Body.Close()

	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "text/html") {
		t.Fatalf("Content-Type = %q, want text/html for HTMX", ct)
	}

	html, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(html), "htmx message") {
		t.Fatalf("HTMX response should contain message text, got %q", html)
	}
	if !strings.Contains(string(html), "msg user") {
		t.Fatalf("HTMX response should have user msg class, got %q", html)
	}
}

func TestWebConnector_Send_ReplyToID_HTMXTrigger(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	body := strings.NewReader(`{"text":"my-code","reply_to_id":"42"}`)
	req, _ := http.NewRequest("POST", webURL(c)+"/api/send", body)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("HX-Request", "true")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST error = %v", err)
	}
	resp.Body.Close()

	trigger := resp.Header.Get("HX-Trigger")
	if !strings.Contains(trigger, "authCodeSubmitted") {
		t.Fatalf("HX-Trigger = %q, want authCodeSubmitted", trigger)
	}
	if !strings.Contains(trigger, "42") {
		t.Fatalf("HX-Trigger = %q, want message ID '42'", trigger)
	}

	msgs, _ := c.Poll(context.Background())
	if len(msgs) != 1 || msgs[0].ReplyToID != "42" {
		t.Fatalf("Poll() reply_to_id = %q, want '42'", msgs[0].ReplyToID)
	}
}

func TestWebConnector_ResponsesEndpoint(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	c.Send(context.Background(), Response{Text: "hello from worker"})
	c.Send(context.Background(), Response{Text: "second message"})

	resp, err := http.Get(webURL(c) + "/api/responses")
	if err != nil {
		t.Fatalf("GET /api/responses error = %v", err)
	}
	defer resp.Body.Close()

	var responses []Response
	json.NewDecoder(resp.Body).Decode(&responses)
	if len(responses) != 2 {
		t.Fatalf("got %d responses, want 2", len(responses))
	}
	if responses[0].Text != "hello from worker" {
		t.Fatalf("responses[0].Text = %q", responses[0].Text)
	}
}

func TestWebConnector_Poll_ReturnsNilOnTimeout(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	msgs, err := c.Poll(ctx)
	if err != nil && err != context.DeadlineExceeded {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 0 {
		t.Fatalf("Poll() = %v, want empty", msgs)
	}
}

func TestWebConnector_Poll_DrainsBatch(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	for i := 0; i < 3; i++ {
		body := strings.NewReader(fmt.Sprintf(`{"text":"msg-%d"}`, i))
		resp, _ := http.Post(webURL(c)+"/api/send", "application/json", body)
		resp.Body.Close()
	}

	time.Sleep(50 * time.Millisecond)
	msgs, _ := c.Poll(context.Background())
	if len(msgs) != 3 {
		t.Fatalf("Poll() got %d messages, want 3", len(msgs))
	}

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	msgs2, _ := c.Poll(ctx)
	if len(msgs2) != 0 {
		t.Fatalf("second Poll() got %d messages, want 0 (drained)", len(msgs2))
	}
}

func TestWebConnector_AuthPrompt(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	result, err := c.SendAuthPrompt(context.Background(), AuthPromptRequest{
		WorkerName: "test-worker",
		URL:        "https://claude.ai/oauth/test",
	})
	if err != nil {
		t.Fatalf("SendAuthPrompt() error = %v", err)
	}
	if result.MessageID == "" {
		t.Fatal("MessageID should not be empty")
	}

	if id := c.PendingAuthPromptID(); id != result.MessageID {
		t.Fatalf("PendingAuthPromptID() = %q, want %q", id, result.MessageID)
	}

	resp, _ := http.Get(webURL(c) + "/api/responses")
	defer resp.Body.Close()
	var responses []Response
	json.NewDecoder(resp.Body).Decode(&responses)

	found := false
	for _, r := range responses {
		if strings.Contains(r.Text, "claude.ai/oauth/test") {
			found = true
		}
	}
	if !found {
		t.Fatal("auth prompt URL should appear in responses")
	}
}

func TestWebConnector_AuthPrompt_NoPendingWhenEmpty(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)
	if id := c.PendingAuthPromptID(); id != "" {
		t.Fatalf("PendingAuthPromptID() = %q, want empty", id)
	}
}

func TestWebConnector_AuthCodeSubmission_E2E(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	result, _ := c.SendAuthPrompt(context.Background(), AuthPromptRequest{
		WorkerName: "auth-test",
		URL:        "https://claude.ai/oauth/test-code",
	})

	body := strings.NewReader(fmt.Sprintf(`{"text":"my-auth-code-123","from":"user","reply_to_id":"%s"}`, result.MessageID))
	resp, err := http.Post(webURL(c)+"/api/send", "application/json", body)
	if err != nil {
		t.Fatalf("POST /api/send error = %v", err)
	}
	resp.Body.Close()

	msgs, err := c.Poll(context.Background())
	if err != nil {
		t.Fatalf("Poll() error = %v", err)
	}
	if len(msgs) != 1 {
		t.Fatalf("Poll() got %d messages, want 1", len(msgs))
	}
	if msgs[0].Text != "my-auth-code-123" {
		t.Fatalf("msg.Text = %q, want 'my-auth-code-123'", msgs[0].Text)
	}
	if msgs[0].ReplyToID != result.MessageID {
		t.Fatalf("msg.ReplyToID = %q, want %q", msgs[0].ReplyToID, result.MessageID)
	}
}

func TestWebConnector_AuthStatusNotification(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	sseCh := make(chan string, 10)
	ch := c.addSSE()
	defer c.removeSSE(ch)
	go func() {
		for evt := range ch {
			sseCh <- evt.HTML
		}
	}()

	c.NotifyAuthStatus("submitting", "Code received, submitting...")

	select {
	case html := <-sseCh:
		if !strings.Contains(html, "submitting") {
			t.Fatalf("SSE event HTML = %q, want 'submitting'", html)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no SSE event received")
	}

	c.NotifyAuthStatus("success", "Authentication complete!")

	select {
	case html := <-sseCh:
		if !strings.Contains(html, "success") {
			t.Fatalf("SSE event HTML = %q, want 'success'", html)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no SSE event received for success")
	}
}

func TestWebConnector_SSE_Endpoint(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	req, _ := http.NewRequestWithContext(ctx, "GET", webURL(c)+"/events", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("GET /events error = %v", err)
	}
	defer resp.Body.Close()

	if ct := resp.Header.Get("Content-Type"); !strings.Contains(ct, "text/event-stream") {
		t.Fatalf("Content-Type = %q, want text/event-stream", ct)
	}

	eventually(t, time.Second, func() bool {
		return c.SSECount() > 0
	})

	c.Send(context.Background(), Response{Text: "sse test message"})

	buf := make([]byte, 4096)
	n, _ := resp.Body.Read(buf)
	data := string(buf[:n])

	if !strings.Contains(data, "event: message") {
		t.Fatalf("SSE data should contain 'event: message', got %q", data)
	}
	if !strings.Contains(data, "sse test message") {
		t.Fatalf("SSE data should contain message text, got %q", data)
	}
}

func TestWebConnector_SSE_ReplaysPendingAuth(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	c.SendAuthPrompt(context.Background(), AuthPromptRequest{
		WorkerName: "sse-replay",
		URL:        "https://claude.ai/oauth/replay-test",
	})

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	req, _ := http.NewRequestWithContext(ctx, "GET", webURL(c)+"/events", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("GET /events error = %v", err)
	}
	defer resp.Body.Close()

	buf := make([]byte, 8192)
	n, _ := resp.Body.Read(buf)
	data := string(buf[:n])

	if !strings.Contains(data, "claude.ai/oauth/replay-test") {
		t.Fatalf("SSE should replay auth prompt on connect, got %q", data)
	}
}

func TestWebConnector_SSE_CountTracking(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	if c.SSECount() != 0 {
		t.Fatalf("SSECount() = %d, want 0", c.SSECount())
	}

	ch := c.addSSE()
	if c.SSECount() != 1 {
		t.Fatalf("SSECount() = %d, want 1", c.SSECount())
	}

	c.removeSSE(ch)
	if c.SSECount() != 0 {
		t.Fatalf("SSECount() = %d, want 0 after remove", c.SSECount())
	}
}

func TestWebConnector_ConfigureCommands(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	err := c.ConfigureCommands([]CommandSpec{
		{Name: "login", Description: "Login to authenticate"},
		{Name: "status", Description: "Show worker status"},
		{Name: "help", Description: "List commands"},
	})
	if err != nil {
		t.Fatalf("ConfigureCommands() error = %v", err)
	}

	resp, _ := http.Get(webURL(c) + "/")
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	html := string(body)

	if !strings.Contains(html, "sendCmd('help')") {
		t.Fatal("page should have help command button")
	}
	if !strings.Contains(html, "sendCmd('login')") {
		t.Fatal("page should have login command button")
	}
	if !strings.Contains(html, "sendCmd('status')") {
		t.Fatal("page should have status command button")
	}
}

func TestWebConnector_ConfigureCommands_SortedAlphabetically(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	c.ConfigureCommands([]CommandSpec{
		{Name: "zebra", Description: "Z"},
		{Name: "alpha", Description: "A"},
		{Name: "mid", Description: "M"},
	})

	c.cmdMu.RLock()
	defer c.cmdMu.RUnlock()
	if c.commands[0].Name != "alpha" || c.commands[1].Name != "mid" || c.commands[2].Name != "zebra" {
		t.Fatalf("commands not sorted: %v", c.commands)
	}
}

func TestWebConnector_Close_CleansUp(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)
	port := c.Port()

	ch := c.addSSE()
	_ = ch

	c.Close()

	_, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/api/health", port))
	if err == nil {
		t.Fatal("server should be closed after Close()")
	}
}

func TestWebConnector_ConcurrentSendAndPoll(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)

	const count = 20
	var wg sync.WaitGroup
	wg.Add(count)

	for i := 0; i < count; i++ {
		go func(n int) {
			defer wg.Done()
			body := strings.NewReader(fmt.Sprintf(`{"text":"msg-%d"}`, n))
			resp, err := http.Post(webURL(c)+"/api/send", "application/json", body)
			if err != nil {
				t.Errorf("POST error = %v", err)
				return
			}
			resp.Body.Close()
		}(i)
	}
	wg.Wait()

	time.Sleep(100 * time.Millisecond)

	total := 0
	for i := 0; i < 5; i++ {
		msgs, _ := c.Poll(context.Background())
		total += len(msgs)
		if total >= count {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}

	if total != count {
		t.Fatalf("total polled = %d, want %d", total, count)
	}
}

func TestWebConnector_AuthFlow_FullE2E(t *testing.T) {
	t.Parallel()
	c := initWebConnector(t, nil)
	url := webURL(c)

	result, err := c.SendAuthPrompt(context.Background(), AuthPromptRequest{
		WorkerName: "e2e-worker",
		URL:        "https://claude.ai/oauth/e2e-test",
	})
	if err != nil {
		t.Fatalf("SendAuthPrompt() error = %v", err)
	}

	statusEvents := make([]string, 0)
	var statusMu sync.Mutex
	ch := c.addSSE()
	defer c.removeSSE(ch)
	go func() {
		for evt := range ch {
			statusMu.Lock()
			statusEvents = append(statusEvents, evt.HTML)
			statusMu.Unlock()
		}
	}()

	resp, _ := http.Get(url + "/api/responses")
	defer resp.Body.Close()
	var responses []Response
	json.NewDecoder(resp.Body).Decode(&responses)
	authFound := false
	for _, r := range responses {
		if strings.Contains(r.Text, "claude.ai/oauth/e2e-test") {
			authFound = true
		}
	}
	if !authFound {
		t.Fatal("auth prompt should be in responses")
	}

	codeBody := strings.NewReader(fmt.Sprintf(`{"text":"e2e-code-xyz","from":"manager","reply_to_id":"%s"}`, result.MessageID))
	codeResp, err := http.Post(url+"/api/send", "application/json", codeBody)
	if err != nil {
		t.Fatalf("POST code error = %v", err)
	}
	codeResp.Body.Close()

	msgs, _ := c.Poll(context.Background())
	if len(msgs) != 1 {
		t.Fatalf("Poll() got %d messages, want 1", len(msgs))
	}
	if msgs[0].Text != "e2e-code-xyz" {
		t.Fatalf("code = %q, want 'e2e-code-xyz'", msgs[0].Text)
	}
	if msgs[0].ReplyToID != result.MessageID {
		t.Fatalf("reply_to_id = %q, want %q", msgs[0].ReplyToID, result.MessageID)
	}

	c.NotifyAuthStatus("submitting", "Code received, submitting...")
	c.NotifyAuthStatus("verifying", "Waiting for authentication...")
	c.NotifyAuthStatus("success", "Authentication complete!")

	time.Sleep(100 * time.Millisecond)
	statusMu.Lock()
	defer statusMu.Unlock()

	if len(statusEvents) < 3 {
		t.Fatalf("got %d status events, want >= 3", len(statusEvents))
	}

	hasSubmitting := false
	hasSuccess := false
	for _, e := range statusEvents {
		if strings.Contains(e, "submitting") || strings.Contains(e, "Code received") {
			hasSubmitting = true
		}
		if strings.Contains(e, "success") {
			hasSuccess = true
		}
	}
	if !hasSubmitting {
		t.Fatal("missing 'submitting' status event")
	}
	if !hasSuccess {
		t.Fatal("missing 'success' status event")
	}
}

func TestWebConnector_Registry(t *testing.T) {
	t.Parallel()
	c, err := NewConnector("web")
	if err != nil {
		t.Fatalf("NewConnector('web') error = %v", err)
	}
	if c.Type() != "web" {
		t.Fatalf("Type() = %q, want 'web'", c.Type())
	}
}

func TestWebConnector_InterfaceCompliance(t *testing.T) {
	t.Parallel()
	var _ Connector = (*WebConnector)(nil)
	var _ PollReceiver = (*WebConnector)(nil)
	var _ AuthPrompter = (*WebConnector)(nil)
	var _ AuthStatusNotifier = (*WebConnector)(nil)
	var _ CommandSurface = (*WebConnector)(nil)
}

