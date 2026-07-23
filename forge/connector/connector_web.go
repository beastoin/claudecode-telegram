package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type WebConnector struct {
	port     int
	name     string
	listener net.Listener
	server   *http.Server

	mu        sync.Mutex
	inbox     []InboundMessage
	responses []Response
	inboxCh   chan struct{}
	nextMsgID int

	ssesMu sync.Mutex
	sses   map[chan sseEvent]struct{}

	authMu      sync.Mutex
	authPrompts []authPromptEntry

	cmdMu    sync.RWMutex
	commands []CommandSpec
}

type sseEvent struct {
	Event string
	HTML  string
}

func (w *WebConnector) SSECount() int {
	w.ssesMu.Lock()
	defer w.ssesMu.Unlock()
	return len(w.sses)
}

type authPromptEntry struct {
	MessageID  string `json:"message_id"`
	WorkerName string `json:"worker_name"`
	URL        string `json:"url"`
}

func init() {
	RegisterConnector("web", func() Connector {
		return &WebConnector{}
	})
}

func (c *WebConnector) Type() string { return "web" }

func (c *WebConnector) Capabilities() Caps {
	return CapText | CapMarkdown
}

func (c *WebConnector) Requirements() Reqs {
	return ReqRuntime
}

func (c *WebConnector) Init(_ context.Context, cfg ConnectorConfig) error {
	c.name = cfg.WorkerName
	c.inboxCh = make(chan struct{}, 100)
	c.sses = make(map[chan sseEvent]struct{})

	if portStr := cfg.Config["WEB_PORT"]; portStr != "" {
		p, err := strconv.Atoi(portStr)
		if err != nil {
			return fmt.Errorf("invalid WEB_PORT %q: %w", portStr, err)
		}
		c.port = p
	}

	bindAddr := "0.0.0.0:0"
	if c.port > 0 {
		bindAddr = fmt.Sprintf("0.0.0.0:%d", c.port)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", c.handleIndex)
	mux.HandleFunc("/api/send", c.handleSend)
	mux.HandleFunc("/api/responses", c.handleResponses)
	mux.HandleFunc("/api/health", c.handleHealth)
	mux.HandleFunc("/events", c.handleSSE)

	ln, err := net.Listen("tcp", bindAddr)
	if err != nil {
		return fmt.Errorf("web connector listen %s: %w", bindAddr, err)
	}
	c.listener = ln
	c.mu.Lock()
	c.port = ln.Addr().(*net.TCPAddr).Port
	c.mu.Unlock()
	c.server = &http.Server{Handler: mux}

	go c.server.Serve(ln)
	return nil
}

func (c *WebConnector) Send(_ context.Context, resp Response) error {
	c.mu.Lock()
	c.responses = append(c.responses, resp)
	c.mu.Unlock()

	c.broadcast(sseEvent{
		Event: "message",
		HTML:  renderWorkerMsg(c.name, resp.Text),
	})
	return nil
}

func (c *WebConnector) Close() error {
	c.ssesMu.Lock()
	for ch := range c.sses {
		close(ch)
	}
	c.sses = nil
	c.ssesMu.Unlock()

	if c.server != nil {
		c.server.Close()
	}
	if c.listener != nil {
		c.listener.Close()
	}
	return nil
}

func (c *WebConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-c.inboxCh:
	case <-time.After(5 * time.Second):
		return nil, nil
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.inbox) == 0 {
		return nil, nil
	}
	msgs := make([]InboundMessage, len(c.inbox))
	copy(msgs, c.inbox)
	c.inbox = c.inbox[:0]
	return msgs, nil
}

func (c *WebConnector) PollInterval() time.Duration { return 0 }

func (c *WebConnector) SendAuthPrompt(_ context.Context, req AuthPromptRequest) (AuthPromptResult, error) {
	c.authMu.Lock()
	c.mu.Lock()
	c.nextMsgID++
	msgID := strconv.Itoa(c.nextMsgID)
	c.authPrompts = append(c.authPrompts, authPromptEntry{
		MessageID:  msgID,
		WorkerName: req.WorkerName,
		URL:        req.URL,
	})
	c.responses = append(c.responses, Response{
		Text: fmt.Sprintf("Auth required for %s\nURL: %s\nReply with the auth code (reply_to_id: %s)", req.WorkerName, req.URL, msgID),
	})
	c.mu.Unlock()
	c.authMu.Unlock()

	c.broadcast(sseEvent{
		Event: "message",
		HTML:  renderAuthCard(msgID, req.WorkerName, req.URL),
	})

	return AuthPromptResult{MessageID: msgID}, nil
}

func (c *WebConnector) PendingAuthPromptID() string {
	c.authMu.Lock()
	defer c.authMu.Unlock()
	if len(c.authPrompts) > 0 {
		return c.authPrompts[0].MessageID
	}
	return ""
}

func (c *WebConnector) NotifyAuthStatus(status, detail string) {
	c.broadcast(sseEvent{
		Event: "message",
		HTML:  renderAuthStatus(status, detail),
	})
}

func (c *WebConnector) ConfigureCommands(commands []CommandSpec) error {
	sorted := make([]CommandSpec, len(commands))
	copy(sorted, commands)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Name < sorted[j].Name })

	c.cmdMu.Lock()
	c.commands = sorted
	c.cmdMu.Unlock()
	return nil
}

func (c *WebConnector) broadcast(evt sseEvent) {
	c.ssesMu.Lock()
	defer c.ssesMu.Unlock()
	for ch := range c.sses {
		select {
		case ch <- evt:
		default:
		}
	}
}

func (c *WebConnector) addSSE() chan sseEvent {
	ch := make(chan sseEvent, 64)
	c.ssesMu.Lock()
	if c.sses != nil {
		c.sses[ch] = struct{}{}
	}
	c.ssesMu.Unlock()
	return ch
}

func (c *WebConnector) removeSSE(ch chan sseEvent) {
	c.ssesMu.Lock()
	delete(c.sses, ch)
	c.ssesMu.Unlock()
}

func (c *WebConnector) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}

	c.cmdMu.RLock()
	cmds := c.commands
	c.cmdMu.RUnlock()

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	pageTmpl.Execute(w, map[string]interface{}{
		"Name":       c.name,
		"CommandBar": renderCommandBar(cmds),
	})
}

func (c *WebConnector) handleSend(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var text, from, replyToID string

	ct := r.Header.Get("Content-Type")
	if strings.Contains(ct, "application/json") {
		var msg struct {
			Text      string `json:"text"`
			From      string `json:"from"`
			ReplyToID string `json:"reply_to_id"`
		}
		if err := json.NewDecoder(r.Body).Decode(&msg); err != nil {
			http.Error(w, "invalid json", http.StatusBadRequest)
			return
		}
		text, from, replyToID = msg.Text, msg.From, msg.ReplyToID
	} else {
		if err := r.ParseForm(); err != nil {
			http.Error(w, "invalid form", http.StatusBadRequest)
			return
		}
		text = r.FormValue("text")
		from = r.FormValue("from")
		replyToID = r.FormValue("reply_to_id")
	}

	if text == "" {
		http.Error(w, "text required", http.StatusBadRequest)
		return
	}
	if from == "" {
		from = "web"
	}

	c.mu.Lock()
	c.nextMsgID++
	inbound := InboundMessage{
		Text:      text,
		From:      from,
		ReplyToID: replyToID,
		MessageID: strconv.Itoa(c.nextMsgID),
	}
	c.inbox = append(c.inbox, inbound)
	c.mu.Unlock()

	select {
	case c.inboxCh <- struct{}{}:
	default:
	}

	if r.Header.Get("HX-Request") == "true" {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if replyToID != "" {
			w.Header().Set("HX-Trigger", `{"authCodeSubmitted":"`+replyToID+`"}`)
		}
		fmt.Fprint(w, renderUserMsg(text))
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "accepted", "message_id": inbound.MessageID})
}

func (c *WebConnector) handleResponses(w http.ResponseWriter, _ *http.Request) {
	c.mu.Lock()
	resp := make([]Response, len(c.responses))
	copy(resp, c.responses)
	c.mu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (c *WebConnector) handleHealth(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"ok":        true,
		"connector": "web",
		"worker":    c.name,
	})
}

func (c *WebConnector) handleSSE(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	flusher.Flush()

	c.authMu.Lock()
	prompts := make([]authPromptEntry, len(c.authPrompts))
	copy(prompts, c.authPrompts)
	c.authMu.Unlock()

	c.mu.Lock()
	responses := make([]Response, len(c.responses))
	copy(responses, c.responses)
	c.mu.Unlock()

	for _, p := range prompts {
		writeSSETo(w, "message", renderAuthCard(p.MessageID, p.WorkerName, p.URL))
	}
	for _, resp := range responses {
		if IsAuthResponseText(resp.Text) {
			continue
		}
		writeSSETo(w, "message", renderWorkerMsg(c.name, resp.Text))
	}
	flusher.Flush()

	ch := c.addSSE()
	defer c.removeSSE(ch)

	for {
		select {
		case <-r.Context().Done():
			return
		case evt, ok := <-ch:
			if !ok {
				return
			}
			writeSSETo(w, evt.Event, evt.HTML)
			flusher.Flush()
		}
	}
}

func (c *WebConnector) Port() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.port
}

func IsAuthResponseText(text string) bool {
	return len(text) > 15 && text[:15] == "Auth required f"
}

var (
	_ Connector          = (*WebConnector)(nil)
	_ PollReceiver       = (*WebConnector)(nil)
	_ AuthPrompter       = (*WebConnector)(nil)
	_ AuthStatusNotifier = (*WebConnector)(nil)
	_ CommandSurface     = (*WebConnector)(nil)
)
