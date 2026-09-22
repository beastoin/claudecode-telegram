package streamd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"sync/atomic"

	"github.com/gorilla/websocket"
)

type cdpMessage struct {
	ID     int64           `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Params json.RawMessage `json:"params,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

type cdpResponse struct {
	result json.RawMessage
	err    error
}

// CDPWSClient sends input events to Chromium via CDP WebSocket.
type CDPWSClient struct {
	conn    *websocket.Conn
	mu      sync.Mutex
	nextID  atomic.Int64
	pending sync.Map // id -> chan cdpResponse
	done    chan struct{}
}

// DiscoverCDPWebSocket finds the Chrome DevTools WebSocket URL.
func DiscoverCDPWebSocket(debugURL string) (string, error) {
	resp, err := http.Get(debugURL + "/json/version")
	if err != nil {
		return "", fmt.Errorf("discover CDP: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	var info struct {
		WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
	}
	if err := json.Unmarshal(body, &info); err != nil {
		return "", fmt.Errorf("parse CDP version: %w", err)
	}
	if info.WebSocketDebuggerURL == "" {
		// Try page-level endpoint
		resp2, err := http.Get(debugURL + "/json")
		if err != nil {
			return "", fmt.Errorf("discover CDP pages: %w", err)
		}
		defer resp2.Body.Close()
		body2, _ := io.ReadAll(resp2.Body)
		var pages []struct {
			WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
		}
		if err := json.Unmarshal(body2, &pages); err != nil || len(pages) == 0 {
			return "", fmt.Errorf("no CDP pages found")
		}
		return pages[0].WebSocketDebuggerURL, nil
	}
	return info.WebSocketDebuggerURL, nil
}

// NewCDPWSClient connects to Chrome via CDP WebSocket.
func NewCDPWSClient(wsURL string) (*CDPWSClient, error) {
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		return nil, fmt.Errorf("connect CDP: %w", err)
	}
	c := &CDPWSClient{
		conn: conn,
		done: make(chan struct{}),
	}
	go c.readLoop()
	return c, nil
}

func (c *CDPWSClient) readLoop() {
	defer close(c.done)
	for {
		_, data, err := c.conn.ReadMessage()
		if err != nil {
			c.failAllPending(fmt.Errorf("CDP connection closed: %w", err))
			return
		}
		var msg cdpMessage
		if err := json.Unmarshal(data, &msg); err != nil {
			continue
		}
		if msg.ID > 0 {
			c.resolvePending(msg)
		}
	}
}

func (c *CDPWSClient) resolvePending(msg cdpMessage) {
	if msg.ID <= 0 {
		return
	}
	chRaw, ok := c.pending.LoadAndDelete(msg.ID)
	if !ok {
		return
	}
	ch := chRaw.(chan cdpResponse)
	if msg.Error != nil {
		select {
		case ch <- cdpResponse{err: fmt.Errorf("CDP error %d: %s", msg.Error.Code, msg.Error.Message)}:
		default:
		}
		return
	}
	select {
	case ch <- cdpResponse{result: msg.Result}:
	default:
	}
}

func (c *CDPWSClient) failAllPending(err error) {
	if err == nil {
		err = fmt.Errorf("CDP connection closed")
	}
	c.pending.Range(func(key, _ any) bool {
		chRaw, ok := c.pending.LoadAndDelete(key)
		if !ok {
			return true
		}
		ch := chRaw.(chan cdpResponse)
		select {
		case ch <- cdpResponse{err: err}:
		default:
		}
		return true
	})
}

// Dispatch sends a CDP command. Implements CDPClient.
func (c *CDPWSClient) Dispatch(ctx context.Context, method string, params map[string]any) error {
	id := c.nextID.Add(1)
	ch := make(chan cdpResponse, 1)
	c.pending.Store(id, ch)

	msg := map[string]any{"id": id, "method": method}
	if params != nil {
		msg["params"] = params
	}

	c.mu.Lock()
	err := c.conn.WriteJSON(msg)
	c.mu.Unlock()
	if err != nil {
		c.pending.Delete(id)
		return err
	}

	select {
	case resp := <-ch:
		return resp.err
	case <-ctx.Done():
		c.pending.Delete(id)
		return ctx.Err()
	case <-c.done:
		return fmt.Errorf("CDP connection closed")
	}
}

// Navigate sends the browser to a new URL.
func (c *CDPWSClient) Navigate(ctx context.Context, url string) error {
	return c.Dispatch(ctx, "Page.navigate", map[string]any{"url": url})
}

// Close closes the CDP connection.
func (c *CDPWSClient) Close() error {
	return c.conn.Close()
}
