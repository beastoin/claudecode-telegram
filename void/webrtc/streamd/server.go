package streamd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/pion/webrtc/v4"
)

const (
	defaultIdleTimeout       = 5 * time.Minute
	defaultIdleCheckInterval = 1 * time.Second
	defaultMaxSessions       = 2
)

// Config controls streamd behavior.
type Config struct {
	CDPURL            string
	TURNURL           string
	SessionSecret     string
	MaxSessions       int
	IdleTimeout       time.Duration
	IdleCheckInterval time.Duration
}

// SessionClaims are parsed from a validated JWT.
type SessionClaims struct {
	SessionID string
	JWTID     string
	ExpiresAt time.Time
}

// CDPClient dispatches input events to the browser.
type CDPClient interface {
	Dispatch(ctx context.Context, method string, params map[string]any) error
}

type offerRequest struct {
	Token string                    `json:"token"`
	Offer webrtc.SessionDescription `json:"offer"`
}

type offerResponse struct {
	Answer webrtc.SessionDescription `json:"answer"`
}

type iceRequest struct {
	Token     string                   `json:"token"`
	Candidate webrtc.ICECandidateInit `json:"candidate"`
}

type serverSession struct {
	id        string
	pc        *webrtc.PeerConnection
	rtpTrack  *webrtc.TrackLocalStaticRTP // set when broadcaster is active
	lastInput time.Time
	done      chan struct{}
	mu        sync.Mutex
}

func (s *serverSession) touch(now time.Time) {
	s.mu.Lock()
	s.lastInput = now
	s.mu.Unlock()
}

func (s *serverSession) lastInputAt() time.Time {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastInput
}

// Server hosts HTTP signaling and manages active stream sessions.
type Server struct {
	cfg         Config
	cdp         CDPClient
	now         func() time.Time
	broadcaster *VideoBroadcaster // optional; enables live video relay

	mu       sync.Mutex
	sessions map[string]*serverSession
	closed   bool
}

// NewServer creates a streamd server.
func NewServer(cfg Config, cdp CDPClient, now func() time.Time) (*Server, error) {
	cfg = normalizeConfig(cfg)
	if cfg.SessionSecret == "" {
		return nil, errors.New("SESSION_SECRET is required")
	}
	if cdp == nil {
		cdp = &NoopCDPClient{}
	}
	if now == nil {
		now = time.Now
	}
	return &Server{
		cfg:      cfg,
		cdp:      cdp,
		now:      now,
		sessions: make(map[string]*serverSession),
	}, nil
}

// LoadConfigFromEnv reads streamd config from environment.
func LoadConfigFromEnv() (Config, error) {
	cfg := Config{
		CDPURL:        os.Getenv("CDP_URL"),
		TURNURL:       os.Getenv("TURN_URL"),
		SessionSecret: os.Getenv("SESSION_SECRET"),
	}
	if v := os.Getenv("MAX_SESSIONS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil {
			return Config{}, fmt.Errorf("invalid MAX_SESSIONS: %w", err)
		}
		cfg.MaxSessions = n
	}
	return normalizeConfig(cfg), nil
}

func normalizeConfig(cfg Config) Config {
	if cfg.MaxSessions <= 0 {
		cfg.MaxSessions = defaultMaxSessions
	}
	if cfg.IdleTimeout <= 0 {
		cfg.IdleTimeout = defaultIdleTimeout
	}
	if cfg.IdleCheckInterval <= 0 {
		cfg.IdleCheckInterval = defaultIdleCheckInterval
	}
	return cfg
}

// Handler returns streamd HTTP handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/offer", s.handleOffer)
	mux.HandleFunc("/answer", s.handleAnswer)
	mux.HandleFunc("/ice", s.handleICE)
	mux.HandleFunc("/health", s.handleHealth)
	return mux
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "active_sessions": s.ActiveSessions()})
}

func (s *Server) handleAnswer(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "/answer is not used in offer/answer mode"})
}

func (s *Server) handleICE(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var req iceRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	claims, err := validateSessionToken(req.Token, s.cfg.SessionSecret, s.now())
	if err != nil {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid session token"})
		return
	}
	sess := s.lookupSession(claims.SessionID)
	if sess == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "session not found"})
		return
	}
	if err := sess.pc.AddICECandidate(req.Candidate); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid ice candidate"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleOffer(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var req offerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	claims, err := validateSessionToken(req.Token, s.cfg.SessionSecret, s.now())
	if err != nil {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "invalid session token"})
		return
	}

	sessionID := claims.SessionID
	if sessionID == "" {
		sessionID = claims.JWTID
	}
	if sessionID == "" {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "token missing subject"})
		return
	}

	if err := s.reserveSession(sessionID); err != nil {
		status := http.StatusConflict
		if errors.Is(err, errSessionLimit) {
			status = http.StatusTooManyRequests
		}
		writeJSON(w, status, map[string]string{"error": err.Error()})
		return
	}
	rollback := true
	defer func() {
		if rollback {
			s.closeSession(sessionID)
		}
	}()

	pc, err := webrtc.NewPeerConnection(s.peerConfig())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to create peer connection"})
		return
	}
	sess := &serverSession{id: sessionID, pc: pc, lastInput: s.now(), done: make(chan struct{})}
	s.storeSession(sessionID, sess)

	if err := s.addVideoTrack(pc, sess); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to add video track"})
		return
	}

	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		switch state {
		case webrtc.PeerConnectionStateDisconnected, webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			s.closeSession(sessionID)
		}
	})

	pc.OnDataChannel(func(dc *webrtc.DataChannel) {
		dc.OnMessage(func(msg webrtc.DataChannelMessage) {
			sess.touch(s.now())
			s.dispatchInput(msg.Data)
		})
	})

	if err := pc.SetRemoteDescription(req.Offer); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid SDP offer"})
		return
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to create answer"})
		return
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to set local description"})
		return
	}
	<-gatherComplete

	go s.idleWatch(sessionID, sess)
	rollback = false
	writeJSON(w, http.StatusOK, offerResponse{Answer: *pc.LocalDescription()})
}

var (
	errSessionLimit = errors.New("session limit reached")
	errSessionBusy  = errors.New("session already active")
)

func (s *Server) reserveSession(sessionID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return errors.New("server closed")
	}
	if _, exists := s.sessions[sessionID]; exists {
		return errSessionBusy
	}
	if len(s.sessions) >= s.cfg.MaxSessions {
		return errSessionLimit
	}
	// Placeholder reserves slot until storeSession replaces it.
	s.sessions[sessionID] = nil
	return nil
}

func (s *Server) storeSession(sessionID string, sess *serverSession) {
	s.mu.Lock()
	s.sessions[sessionID] = sess
	s.mu.Unlock()
}

func (s *Server) lookupSession(sessionID string) *serverSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sessions[sessionID]
}

func (s *Server) peerConfig() webrtc.Configuration {
	cfg := webrtc.Configuration{}
	if s.cfg.TURNURL != "" {
		cfg.ICEServers = append(cfg.ICEServers, webrtc.ICEServer{URLs: []string{s.cfg.TURNURL}})
	}
	return cfg
}

// SetBroadcaster enables live video relay from ffmpeg RTP output.
func (s *Server) SetBroadcaster(vb *VideoBroadcaster) {
	s.broadcaster = vb
}

func (s *Server) addVideoTrack(pc *webrtc.PeerConnection, sess *serverSession) error {
	if s.broadcaster != nil {
		return s.addRTPTrack(pc, sess)
	}
	return s.addH264Track(pc)
}

func (s *Server) addRTPTrack(pc *webrtc.PeerConnection, sess *serverSession) error {
	track, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeH264, ClockRate: 90000},
		"video",
		"void-stream",
	)
	if err != nil {
		return err
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		return err
	}
	sess.rtpTrack = track
	s.broadcaster.AddTrack(track)
	go func() {
		rtcpBuf := make([]byte, 1500)
		for {
			if _, _, readErr := sender.Read(rtcpBuf); readErr != nil {
				return
			}
		}
	}()
	return nil
}

func (s *Server) addH264Track(pc *webrtc.PeerConnection) error {
	track, err := webrtc.NewTrackLocalStaticSample(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeH264, ClockRate: 90000},
		"video",
		"void-stream",
	)
	if err != nil {
		return err
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		return err
	}
	go func() {
		rtcpBuf := make([]byte, 1500)
		for {
			if _, _, readErr := sender.Read(rtcpBuf); readErr != nil {
				return
			}
		}
	}()
	return nil
}

func (s *Server) dispatchInput(payload []byte) {
	var msg map[string]any
	if err := json.Unmarshal(payload, &msg); err != nil {
		return
	}
	eventType, _ := msg["type"].(string)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	switch eventType {
	case "touch":
		s.dispatchTouch(ctx, msg)
	case "key":
		s.dispatchKey(ctx, msg)
	case "mouse":
		s.dispatchMouse(ctx, msg)
	case "navigate":
		if url, ok := msg["url"].(string); ok {
			_ = s.cdp.Dispatch(ctx, "Page.navigate", map[string]any{"url": url})
		}
	}
}

func (s *Server) dispatchTouch(ctx context.Context, msg map[string]any) {
	x, _ := toFloat(msg["x"])
	y, _ := toFloat(msg["y"])
	phase, _ := msg["phase"].(string)

	var cdpType string
	switch phase {
	case "start":
		cdpType = "touchStart"
	case "move":
		cdpType = "touchMove"
	case "end", "cancel":
		cdpType = "touchEnd"
	default:
		return
	}

	params := map[string]any{
		"type": cdpType,
		"touchPoints": []map[string]any{
			{"x": x, "y": y},
		},
		"modifiers": 0,
	}
	_ = s.cdp.Dispatch(ctx, "Input.dispatchTouchEvent", params)
}

func (s *Server) dispatchMouse(ctx context.Context, msg map[string]any) {
	x, _ := toFloat(msg["x"])
	y, _ := toFloat(msg["y"])
	action, _ := msg["action"].(string)
	button, _ := msg["button"].(string)
	if button == "" {
		button = "left"
	}

	var cdpType string
	clickCount := 0
	switch action {
	case "down":
		cdpType = "mousePressed"
		clickCount = 1
	case "up":
		cdpType = "mouseReleased"
		clickCount = 1
	case "move":
		cdpType = "mouseMoved"
	default:
		return
	}

	params := map[string]any{
		"type":       cdpType,
		"x":          x,
		"y":          y,
		"button":     button,
		"clickCount": clickCount,
	}
	_ = s.cdp.Dispatch(ctx, "Input.dispatchMouseEvent", params)
}

func (s *Server) dispatchKey(ctx context.Context, msg map[string]any) {
	action, _ := msg["action"].(string)
	key, _ := msg["key"].(string)

	var cdpType string
	switch action {
	case "down":
		cdpType = "keyDown"
	case "up":
		cdpType = "keyUp"
	default:
		return
	}

	params := map[string]any{
		"type": cdpType,
		"key":  key,
	}
	if len(key) == 1 {
		params["text"] = key
	}
	_ = s.cdp.Dispatch(ctx, "Input.dispatchKeyEvent", params)
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	default:
		return 0, false
	}
}

func (s *Server) idleWatch(sessionID string, sess *serverSession) {
	ticker := time.NewTicker(s.cfg.IdleCheckInterval)
	defer ticker.Stop()
	for {
		select {
		case <-sess.done:
			return
		case <-ticker.C:
			if s.now().Sub(sess.lastInputAt()) > s.cfg.IdleTimeout {
				s.closeSession(sessionID)
				return
			}
		}
	}
}

func (s *Server) closeSession(sessionID string) {
	var sess *serverSession
	s.mu.Lock()
	sess = s.sessions[sessionID]
	delete(s.sessions, sessionID)
	s.mu.Unlock()
	if sess == nil {
		return
	}
	if sess.rtpTrack != nil && s.broadcaster != nil {
		s.broadcaster.RemoveTrack(sess.rtpTrack)
	}
	select {
	case <-sess.done:
	default:
		close(sess.done)
	}
	_ = sess.pc.Close()
}

// ActiveSessions returns currently active sessions.
func (s *Server) ActiveSessions() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.sessions)
}

// Close shuts down all active sessions.
func (s *Server) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	ids := make([]string, 0, len(s.sessions))
	for id := range s.sessions {
		ids = append(ids, id)
	}
	s.mu.Unlock()
	for _, id := range ids {
		s.closeSession(id)
	}
	return nil
}

func validateSessionToken(tokenValue, secret string, now time.Time) (*SessionClaims, error) {
	token, err := jwt.Parse(tokenValue, func(token *jwt.Token) (any, error) {
		if token.Method != jwt.SigningMethodHS256 {
			return nil, errors.New("unexpected signing method")
		}
		return []byte(secret), nil
	}, jwt.WithExpirationRequired(), jwt.WithIssuedAt())
	if err != nil || !token.Valid {
		return nil, errors.New("invalid token")
	}

	claims, ok := token.Claims.(jwt.MapClaims)
	if !ok {
		return nil, errors.New("invalid claims")
	}
	exp, err := claims.GetExpirationTime()
	if err != nil || exp == nil || now.After(exp.Time) {
		return nil, errors.New("token expired")
	}
	sub, _ := claims.GetSubject()
	jtiStr, _ := claims["jti"].(string)
	return &SessionClaims{SessionID: sub, JWTID: jtiStr, ExpiresAt: exp.Time}, nil
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
