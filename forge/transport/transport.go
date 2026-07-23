package transport

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	pb "github.com/beastoin/claudecode-telegram/forge/proto/workerforge"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type Transport interface {
	Connect(ctx context.Context, bridgeURL string) error
	Register(ctx context.Context, req RegisterRequest) (RegisterResponse, error)
	SendMessage(ctx context.Context, msg WorkerMessage) error
	ReceiveMessage(ctx context.Context) (BridgeMessage, error)
	StreamJSONL(ctx context.Context, chunk JSONLChunk) error
	PullKnowledge(ctx context.Context, name string, knowledgeType string) ([]byte, error)
	CheckUpgrade(ctx context.Context, name string, currentVersion string) (bool, string, string, string, error)
	Heartbeat(ctx context.Context) error
	Close() error
}

type RegisterRequest struct {
	Name    string
	Host    string
	Version string
	Tools   map[string]string
}

type RegisterResponse struct {
	OK bool
}

type BridgeMessage struct {
	Type    string
	Text    string
	From    string
	Payload []byte
}

type WorkerMessage struct {
	Type    string
	Text    string
	Payload []byte
}

type JSONLChunk struct {
	StreamID string
	Data     []byte
	Final    bool
}

var ErrUnsupported = errors.New("unsupported")

const defaultTransportConnectTimeout = 10 * time.Second

func NewTransport(bridgeURL string) Transport {
	if strings.HasPrefix(bridgeURL, "grpc://") {
		return &GRPCTransport{}
	}
	return &HTTPTransport{}
}

type HTTPTransport struct {
	BridgeURL string
	Client    *http.Client
	Source    string
}

func (h *HTTPTransport) Connect(ctx context.Context, bridgeURL string) error {
	if bridgeURL == "" {
		return errors.New("bridge URL is required")
	}

	connectCtx, cancel := context.WithTimeout(ctx, defaultTransportConnectTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(connectCtx, http.MethodGet, bridgeURL+"/", nil)
	if err != nil {
		return err
	}

	resp, err := h.client().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("bridge health check failed: %s", resp.Status)
	}

	h.BridgeURL = bridgeURL
	return nil
}

func (h *HTTPTransport) Register(ctx context.Context, req RegisterRequest) (RegisterResponse, error) {
	if h.BridgeURL == "" {
		return RegisterResponse{}, errors.New("transport is not connected")
	}

	body, err := json.Marshal(req)
	if err != nil {
		return RegisterResponse{}, err
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, h.BridgeURL+"/register", bytes.NewReader(body))
	if err != nil {
		return RegisterResponse{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := h.client().Do(httpReq)
	if err != nil {
		return RegisterResponse{}, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return RegisterResponse{}, fmt.Errorf("bridge register failed: %s", resp.Status)
	}

	return RegisterResponse{OK: true}, nil
}

func (h *HTTPTransport) SendMessage(ctx context.Context, msg WorkerMessage) error {
	if h.BridgeURL == "" {
		return errors.New("transport is not connected")
	}

	body, err := json.Marshal(map[string]string{
		"type":   msg.Type,
		"text":   msg.Text,
		"source": h.source(),
	})
	if err != nil {
		return err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.BridgeURL+"/response", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := h.client().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("bridge send failed: %s", resp.Status)
	}

	return nil
}

func (h *HTTPTransport) ReceiveMessage(context.Context) (BridgeMessage, error) {
	return BridgeMessage{}, ErrUnsupported
}

func (h *HTTPTransport) StreamJSONL(context.Context, JSONLChunk) error {
	return ErrUnsupported
}

func (h *HTTPTransport) PullKnowledge(context.Context, string, string) ([]byte, error) {
	return nil, ErrUnsupported
}

func (h *HTTPTransport) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	return false, "", "", "", ErrUnsupported
}

func (h *HTTPTransport) Heartbeat(ctx context.Context) error {
	if h.BridgeURL == "" {
		return errors.New("transport is not connected")
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.BridgeURL+"/", nil)
	if err != nil {
		return err
	}

	resp, err := h.client().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("bridge health check failed: %s", resp.Status)
	}

	return nil
}

func (h *HTTPTransport) Close() error {
	return nil
}

type StubTransport struct {
	BridgeURL    string
	LastRegister RegisterRequest
	SentMessages []WorkerMessage
	JSONLChunks  []JSONLChunk
	Received     []BridgeMessage
	Knowledge    []byte
	Connected    bool
	Closed       bool
}

func (s *StubTransport) Connect(_ context.Context, bridgeURL string) error {
	s.BridgeURL = bridgeURL
	s.Connected = true
	return nil
}

func (s *StubTransport) Register(_ context.Context, req RegisterRequest) (RegisterResponse, error) {
	if !s.Connected {
		return RegisterResponse{}, errors.New("transport is not connected")
	}

	s.LastRegister = req
	return RegisterResponse{OK: true}, nil
}

func (s *StubTransport) SendMessage(_ context.Context, msg WorkerMessage) error {
	if !s.Connected {
		return errors.New("transport is not connected")
	}

	s.SentMessages = append(s.SentMessages, msg)
	return nil
}

func (s *StubTransport) ReceiveMessage(context.Context) (BridgeMessage, error) {
	if len(s.Received) == 0 {
		return BridgeMessage{}, errors.New("no bridge messages available")
	}

	msg := s.Received[0]
	s.Received = s.Received[1:]
	return msg, nil
}

func (s *StubTransport) StreamJSONL(_ context.Context, chunk JSONLChunk) error {
	if !s.Connected {
		return errors.New("transport is not connected")
	}

	s.JSONLChunks = append(s.JSONLChunks, chunk)
	return nil
}

func (s *StubTransport) PullKnowledge(context.Context, string, string) ([]byte, error) {
	if !s.Connected {
		return nil, errors.New("transport is not connected")
	}

	if s.Knowledge != nil {
		return append([]byte(nil), s.Knowledge...), nil
	}
	return []byte("stub knowledge"), nil
}

func (s *StubTransport) CheckUpgrade(context.Context, string, string) (bool, string, string, string, error) {
	if !s.Connected {
		return false, "", "", "", errors.New("transport is not connected")
	}

	return true, "stub-version", "stub-url", "stub-checksum", nil
}

func (s *StubTransport) Heartbeat(context.Context) error {
	if !s.Connected {
		return errors.New("transport is not connected")
	}
	return nil
}

func (s *StubTransport) Close() error {
	s.Closed = true
	s.Connected = false
	return nil
}

type GRPCTransport struct {
	BridgeURL    string
	LastRegister RegisterRequest
	DialOpts     []grpc.DialOption
	WorkerName   string

	mu        sync.Mutex
	conn      *grpc.ClientConn
	client    pb.BridgeClient
	msgStream grpc.BidiStreamingClient[pb.WorkerMessage, pb.BridgeMessage]
}

func (g *GRPCTransport) Client() pb.BridgeClient {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.client
}

func (g *GRPCTransport) Connect(ctx context.Context, bridgeURL string) error {
	addr := bridgeURL
	if strings.HasPrefix(addr, "grpc://") {
		addr = strings.TrimPrefix(addr, "grpc://")
	}

	opts := g.DialOpts
	if len(opts) == 0 {
		opts = []grpc.DialOption{grpc.WithTransportCredentials(insecure.NewCredentials())}
	}

	conn, err := grpc.NewClient(addr, opts...)
	if err != nil {
		return fmt.Errorf("grpc dial: %w", err)
	}

	connectCtx, cancel := context.WithTimeout(ctx, defaultTransportConnectTimeout)
	defer cancel()

	client := pb.NewBridgeClient(conn)

	resp, err := client.Check(connectCtx, &pb.HealthCheckRequest{})
	if err != nil {
		conn.Close()
		return fmt.Errorf("grpc health check: %w", err)
	}
	if resp.Status != pb.HealthCheckResponse_SERVING {
		conn.Close()
		return fmt.Errorf("bridge not serving: %s", resp.Status)
	}

	g.mu.Lock()
	g.conn = conn
	g.client = client
	g.BridgeURL = bridgeURL
	g.mu.Unlock()
	return nil
}

func (g *GRPCTransport) Register(ctx context.Context, req RegisterRequest) (RegisterResponse, error) {
	g.mu.Lock()
	client := g.client
	g.mu.Unlock()
	if client == nil {
		return RegisterResponse{}, errors.New("transport is not connected")
	}

	tools := make(map[string]string, len(req.Tools))
	for k, v := range req.Tools {
		tools[k] = v
	}

	resp, err := client.Register(ctx, &pb.RegisterRequest{
		Name:    req.Name,
		Host:    req.Host,
		Version: req.Version,
		Tools:   tools,
	})
	if err != nil {
		return RegisterResponse{}, fmt.Errorf("grpc register: %w", err)
	}

	g.mu.Lock()
	g.LastRegister = req
	g.WorkerName = req.Name
	g.mu.Unlock()

	return RegisterResponse{OK: resp.Ok}, nil
}

func (g *GRPCTransport) SendMessage(ctx context.Context, msg WorkerMessage) error {
	stream, err := g.getOrCreateMsgStream(ctx)
	if err != nil {
		return err
	}

	return stream.Send(&pb.WorkerMessage{
		Type:    msg.Type,
		Text:    msg.Text,
		Payload: msg.Payload,
	})
}

func (g *GRPCTransport) ReceiveMessage(ctx context.Context) (BridgeMessage, error) {
	stream, err := g.getOrCreateMsgStream(ctx)
	if err != nil {
		return BridgeMessage{}, err
	}

	pbMsg, err := stream.Recv()
	if err != nil {
		g.mu.Lock()
		g.msgStream = nil
		g.mu.Unlock()
		return BridgeMessage{}, fmt.Errorf("grpc receive: %w", err)
	}

	return BridgeMessage{
		Type:    pbMsg.Type,
		Text:    pbMsg.Text,
		From:    pbMsg.From,
		Payload: pbMsg.Payload,
	}, nil
}

func (g *GRPCTransport) StreamJSONL(ctx context.Context, chunk JSONLChunk) error {
	g.mu.Lock()
	client := g.client
	g.mu.Unlock()
	if client == nil {
		return errors.New("transport is not connected")
	}

	stream, err := client.StreamJSONL(ctx)
	if err != nil {
		return fmt.Errorf("grpc stream jsonl: %w", err)
	}

	if err := stream.Send(&pb.JSONLChunk{
		StreamId: chunk.StreamID,
		Data:     chunk.Data,
		Final:    chunk.Final,
	}); err != nil {
		return fmt.Errorf("grpc send jsonl: %w", err)
	}

	if chunk.Final {
		if _, err := stream.CloseAndRecv(); err != nil {
			return fmt.Errorf("grpc close jsonl: %w", err)
		}
	}

	return nil
}

func (g *GRPCTransport) PullKnowledge(ctx context.Context, name string, knowledgeType string) ([]byte, error) {
	g.mu.Lock()
	client := g.client
	g.mu.Unlock()
	if client == nil {
		return nil, errors.New("transport is not connected")
	}

	stream, err := client.PullKnowledge(ctx, &pb.KnowledgeRequest{
		Name: name,
		Type: knowledgeType,
	})
	if err != nil {
		return nil, fmt.Errorf("grpc pull knowledge: %w", err)
	}

	var buf []byte
	for {
		chunk, err := stream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("grpc pull knowledge recv: %w", err)
		}
		buf = append(buf, chunk.Data...)
		if chunk.Final {
			break
		}
	}
	return buf, nil
}

func (g *GRPCTransport) CheckUpgrade(ctx context.Context, name string, currentVersion string) (bool, string, string, string, error) {
	g.mu.Lock()
	client := g.client
	g.mu.Unlock()
	if client == nil {
		return false, "", "", "", errors.New("transport is not connected")
	}

	resp, err := client.CheckUpgrade(ctx, &pb.UpgradeCheckRequest{
		Name:           name,
		CurrentVersion: currentVersion,
	})
	if err != nil {
		return false, "", "", "", fmt.Errorf("grpc check upgrade: %w", err)
	}

	return resp.Available, resp.Version, "", resp.Checksum, nil
}

func (g *GRPCTransport) Heartbeat(ctx context.Context) error {
	g.mu.Lock()
	client := g.client
	name := g.WorkerName
	g.mu.Unlock()
	if client == nil {
		return errors.New("transport is not connected")
	}

	resp, err := client.Heartbeat(ctx, &pb.HeartbeatRequest{Name: name})
	if err != nil {
		return fmt.Errorf("grpc heartbeat: %w", err)
	}
	if !resp.Ok {
		return errors.New("heartbeat rejected")
	}
	return nil
}

func (g *GRPCTransport) Close() error {
	g.mu.Lock()
	defer g.mu.Unlock()

	if g.msgStream != nil {
		g.msgStream.CloseSend()
		g.msgStream = nil
	}
	if g.conn != nil {
		err := g.conn.Close()
		g.conn = nil
		g.client = nil
		return err
	}
	return nil
}

func (g *GRPCTransport) getOrCreateMsgStream(ctx context.Context) (grpc.BidiStreamingClient[pb.WorkerMessage, pb.BridgeMessage], error) {
	g.mu.Lock()
	defer g.mu.Unlock()

	if g.msgStream != nil {
		return g.msgStream, nil
	}
	if g.client == nil {
		return nil, errors.New("transport is not connected")
	}

	stream, err := g.client.MessageStream(ctx)
	if err != nil {
		return nil, fmt.Errorf("grpc message stream: %w", err)
	}
	g.msgStream = stream
	return stream, nil
}

type ReconnectTransport struct {
	Base        Transport
	BaseBackoff time.Duration
	MaxBackoff  time.Duration
	Sleep       func(time.Duration)

	bridgeURL string
}

func (r *ReconnectTransport) Connect(ctx context.Context, bridgeURL string) error {
	if r.Base == nil {
		return errors.New("base transport is required")
	}

	r.bridgeURL = bridgeURL
	backoff := r.baseBackoff()

	for {
		err := r.Base.Connect(ctx, bridgeURL)
		if err == nil {
			return nil
		}

		if ctx.Err() != nil {
			return ctx.Err()
		}

		r.sleep(backoff)
		backoff = r.nextBackoff(backoff)
	}
}

func (r *ReconnectTransport) Register(ctx context.Context, req RegisterRequest) (RegisterResponse, error) {
	return r.Base.Register(ctx, req)
}

func (r *ReconnectTransport) SendMessage(ctx context.Context, msg WorkerMessage) error {
	return r.Base.SendMessage(ctx, msg)
}

func (r *ReconnectTransport) ReceiveMessage(ctx context.Context) (BridgeMessage, error) {
	return r.Base.ReceiveMessage(ctx)
}

func (r *ReconnectTransport) StreamJSONL(ctx context.Context, chunk JSONLChunk) error {
	return r.Base.StreamJSONL(ctx, chunk)
}

func (r *ReconnectTransport) PullKnowledge(ctx context.Context, name string, knowledgeType string) ([]byte, error) {
	return r.Base.PullKnowledge(ctx, name, knowledgeType)
}

func (r *ReconnectTransport) CheckUpgrade(ctx context.Context, name string, currentVersion string) (bool, string, string, string, error) {
	return r.Base.CheckUpgrade(ctx, name, currentVersion)
}

func (r *ReconnectTransport) Heartbeat(ctx context.Context) error {
	if err := r.Base.Heartbeat(ctx); err == nil {
		return nil
	}

	if r.bridgeURL == "" {
		return errors.New("transport heartbeat failed before initial connect")
	}

	return r.Connect(ctx, r.bridgeURL)
}

func (r *ReconnectTransport) Close() error {
	if r.Base == nil {
		return nil
	}
	return r.Base.Close()
}

func (h *HTTPTransport) client() *http.Client {
	if h.Client != nil {
		return h.Client
	}
	return http.DefaultClient
}

func (h *HTTPTransport) source() string {
	if h.Source != "" {
		return h.Source
	}
	return "worker-forge"
}

func (r *ReconnectTransport) baseBackoff() time.Duration {
	if r.BaseBackoff <= 0 {
		return time.Second
	}
	return r.BaseBackoff
}

func (r *ReconnectTransport) nextBackoff(current time.Duration) time.Duration {
	if current <= 0 {
		return r.baseBackoff()
	}

	next := current * 2
	if r.MaxBackoff > 0 && next > r.MaxBackoff {
		return r.MaxBackoff
	}
	return next
}

func (r *ReconnectTransport) sleep(delay time.Duration) {
	if r.Sleep != nil {
		r.Sleep(delay)
		return
	}
	time.Sleep(delay)
}
