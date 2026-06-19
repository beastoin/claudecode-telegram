package forge

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
)

// HookListener serves HTTP on a Unix socket for hook IPC.
// Stop hooks POST responses to this socket, which then routes them
// through the active connector's Send method.
//
// Socket path: /tmp/forge-{worker-name}.sock
type HookListener struct {
	socketPath string
	connector  Connector
	listener   net.Listener
	server     *http.Server
	mu         sync.Mutex
	running    bool
}

// NewHookListener creates a HookListener for the given socket path and connector.
func NewHookListener(socketPath string, connector Connector) *HookListener {
	return &HookListener{
		socketPath: socketPath,
		connector:  connector,
	}
}

// Start begins serving on the Unix socket.
func (h *HookListener) Start() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if h.running {
		return fmt.Errorf("hook listener already running")
	}

	// Remove stale socket if it exists
	os.Remove(h.socketPath)

	ln, err := net.Listen("unix", h.socketPath)
	if err != nil {
		return fmt.Errorf("listen on %s: %w", h.socketPath, err)
	}

	// Set permissions so only the current user can access
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

// Stop gracefully shuts down the listener and removes the socket file.
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

// SocketPath returns the path to the Unix socket.
func (h *HookListener) SocketPath() string {
	return h.socketPath
}

// handleResponse processes POST /response from hooks.
// The hook sends the Claude output here, and we route it through the connector.
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

// handleHealth responds to health checks.
func (h *HookListener) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("ok"))
}
