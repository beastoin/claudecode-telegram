package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
)

type HookListener struct {
	socketPath string
	connector  Connector
	listener   net.Listener
	server     *http.Server
	mu         sync.Mutex
	running    bool
}

func NewHookListener(socketPath string, connector Connector) *HookListener {
	return &HookListener{
		socketPath: socketPath,
		connector:  connector,
	}
}

func (h *HookListener) Start() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if h.running {
		return fmt.Errorf("hook listener already running")
	}

	os.Remove(h.socketPath)

	ln, err := net.Listen("unix", h.socketPath)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", h.socketPath, err)
	}

	os.Chmod(h.socketPath, 0600)

	mux := http.NewServeMux()
	mux.HandleFunc("/response", h.handleResponse)
	mux.HandleFunc("/health", h.handleHealth)

	h.listener = ln
	h.server = &http.Server{Handler: mux}
	h.running = true

	go h.server.Serve(ln)

	return nil
}

func (h *HookListener) Stop() {
	h.mu.Lock()
	defer h.mu.Unlock()

	if !h.running {
		return
	}

	h.server.Shutdown(context.Background())
	h.listener.Close()
	os.Remove(h.socketPath)
	h.running = false
}

func (h *HookListener) SocketPath() string {
	return h.socketPath
}

func (h *HookListener) handleResponse(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var resp Response
	if err := json.NewDecoder(r.Body).Decode(&resp); err != nil {
		http.Error(w, fmt.Sprintf("invalid json: %v", err), http.StatusBadRequest)
		return
	}

	if err := h.connector.Send(r.Context(), resp); err != nil {
		http.Error(w, fmt.Sprintf("send failed: %v", err), http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusOK)
	w.Write([]byte("ok"))
}

func (h *HookListener) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("ok"))
}
