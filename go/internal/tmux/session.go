// Package tmux provides tmux session management for Claude workers.
//
// tmux IS the database - no other persistence mechanism is used.
// Sessions are discovered by prefix pattern matching (e.g., "claude-prod-").
package tmux

import (
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/beastoin/claudecode-telegram/internal/sandbox"
)

const sandboxInboxRoot = "/tmp/claudecode-telegram"

// Session represents a tmux session running a Claude worker.
type Session struct {
	Name    string    // Worker name (without prefix)
	Created time.Time // When the session was created
	WorkDir string    // Working directory (if known)
}

// Manager handles tmux session operations for a specific prefix.
// Multiple managers with different prefixes can coexist (multi-node support).
type Manager struct {
	Prefix   string // Session name prefix (e.g., "claude-prod-")
	TmuxPath string // Path to tmux binary (default: "tmux")
	// Sandbox configuration (Docker isolation)
	SandboxEnabled bool
	SandboxImage   string
	SandboxMounts  []sandbox.Mount
	SessionsDir    string
	BridgeURL      string
	Port           string

	// Per-session locks to prevent concurrent send interleaving
	sendLocks      map[string]*sync.Mutex
	sendLocksMutex sync.Mutex
}

// NewManager creates a new tmux Manager with the given prefix.
func NewManager(prefix string) *Manager {
	return &Manager{
		Prefix:    prefix,
		TmuxPath:  "tmux",
		sendLocks: make(map[string]*sync.Mutex),
	}
}

// ListSessions returns all sessions matching this manager's prefix.
func (m *Manager) ListSessions() ([]Session, error) {
	cmd := exec.Command(m.tmuxPath(), "list-sessions", "-F", "#{session_name}")
	out, err := cmd.Output()
	if err != nil {
		// Check if it's just "no sessions" vs a real error
		if exitErr, ok := err.(*exec.ExitError); ok {
			stderr := string(exitErr.Stderr)
			if strings.Contains(stderr, "no server running") ||
				strings.Contains(stderr, "no sessions") {
				return []Session{}, nil
			}
		}
		return nil, fmt.Errorf("list sessions: %w", err)
	}

	var sessions []Session
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}

		name, matches := parseSessionName(m.Prefix, line)
		if matches {
			sessions = append(sessions, Session{
				Name:    name,
				Created: time.Now(), // We don't track creation time yet
			})
		}
	}

	return sessions, nil
}

// CreateSession creates a new tmux session for a worker.
// The session is created with a 200x50 terminal size.
func (m *Manager) CreateSession(name, workdir string) error {
	fullName := fullSessionName(m.Prefix, name)

	// Check if session already exists
	if m.sessionExistsFull(fullName) {
		return fmt.Errorf("session %q already exists", name)
	}

	// Create the tmux session
	args := []string{"new-session", "-d", "-s", fullName, "-x", "200", "-y", "50"}
	if workdir != "" {
		args = append(args, "-c", workdir)
	}

	cmd := exec.Command(m.tmuxPath(), args...)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("create session: %w", err)
	}

	if m.SandboxEnabled {
		dockerCmd, err := m.dockerRunCommand(name)
		if err != nil {
			return err
		}
		if err := m.SendMessage(name, dockerCmd); err != nil {
			return fmt.Errorf("start sandbox: %w", err)
		}
		return nil
	}

	m.exportHookEnv(name)
	return nil
}

// SendMessage sends text to a worker's tmux session.
// Uses per-session locking to prevent concurrent message interleaving.
func (m *Manager) SendMessage(sessionName, text string) error {
	fullName := fullSessionName(m.Prefix, sessionName)

	// Check session exists first
	if !m.sessionExistsFull(fullName) {
		return fmt.Errorf("session %q does not exist", sessionName)
	}

	// Get or create lock for this session
	lock := m.getSendLock(sessionName)
	lock.Lock()
	defer lock.Unlock()

	// Send text with -l flag (literal)
	cmd := exec.Command(m.tmuxPath(), "send-keys", "-t", fullName, "-l", text)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("send text: %w", err)
	}

	// Send Enter key separately
	cmd = exec.Command(m.tmuxPath(), "send-keys", "-t", fullName, "Enter")
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("send enter: %w", err)
	}

	return nil
}

// SendKeys sends raw keys to a worker's tmux session (no Enter appended).
// Useful for special keys like Escape.
func (m *Manager) SendKeys(sessionName string, keys ...string) error {
	fullName := fullSessionName(m.Prefix, sessionName)

	if !m.sessionExistsFull(fullName) {
		return fmt.Errorf("session %q does not exist", sessionName)
	}

	args := []string{"send-keys", "-t", fullName}
	args = append(args, keys...)

	cmd := exec.Command(m.tmuxPath(), args...)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("send keys: %w", err)
	}

	return nil
}

// KillSession terminates a worker's tmux session.
func (m *Manager) KillSession(sessionName string) error {
	fullName := fullSessionName(m.Prefix, sessionName)

	if !m.sessionExistsFull(fullName) {
		return fmt.Errorf("session %q does not exist", sessionName)
	}

	if m.SandboxEnabled {
		m.stopDockerContainer(sessionName)
	}

	cmd := exec.Command(m.tmuxPath(), "kill-session", "-t", fullName)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("kill session: %w", err)
	}

	// Clean up the send lock
	m.sendLocksMutex.Lock()
	delete(m.sendLocks, sessionName)
	m.sendLocksMutex.Unlock()

	return nil
}

// SessionExists checks if a worker session exists.
func (m *Manager) SessionExists(sessionName string) bool {
	return m.sessionExistsFull(fullSessionName(m.Prefix, sessionName))
}

// SetEnvironment sets an environment variable in the tmux session.
// This persists across restarts of the process within the session.
func (m *Manager) SetEnvironment(sessionName, key, value string) error {
	fullName := fullSessionName(m.Prefix, sessionName)

	if !m.sessionExistsFull(fullName) {
		return fmt.Errorf("session %q does not exist", sessionName)
	}

	cmd := exec.Command(m.tmuxPath(), "set-environment", "-t", fullName, key, value)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("set environment: %w", err)
	}

	return nil
}

// GetPaneCommand returns the current command running in the session's pane.
func (m *Manager) GetPaneCommand(sessionName string) (string, error) {
	fullName := fullSessionName(m.Prefix, sessionName)

	cmd := exec.Command(m.tmuxPath(), "display-message", "-t", fullName, "-p", "#{pane_current_command}")
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("get pane command: %w", err)
	}

	return strings.TrimSpace(string(out)), nil
}

// CapturePaneContent captures the visible content of the session's pane.
func (m *Manager) CapturePaneContent(sessionName string) (string, error) {
	fullName := fullSessionName(m.Prefix, sessionName)

	cmd := exec.Command(m.tmuxPath(), "capture-pane", "-t", fullName, "-p")
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("capture pane: %w", err)
	}

	return string(out), nil
}

// exportHookEnv configures per-session env vars for hook execution.
// Failures are ignored to keep session creation resilient.
func (m *Manager) exportHookEnv(sessionName string) {
	if m.Port != "" {
		_ = m.SetEnvironment(sessionName, "PORT", m.Port)
	}
	if m.Prefix != "" {
		_ = m.SetEnvironment(sessionName, "TMUX_PREFIX", m.Prefix)
	}
	if m.SessionsDir != "" {
		_ = m.SetEnvironment(sessionName, "SESSIONS_DIR", m.SessionsDir)
	}
	if strings.TrimSpace(m.BridgeURL) != "" {
		_ = m.SetEnvironment(sessionName, "BRIDGE_URL", m.BridgeURL)
	} else {
		_ = m.unsetEnvironment(sessionName, "BRIDGE_URL")
	}
}

func (m *Manager) unsetEnvironment(sessionName, key string) error {
	fullName := fullSessionName(m.Prefix, sessionName)

	if !m.sessionExistsFull(fullName) {
		return fmt.Errorf("session %q does not exist", sessionName)
	}

	cmd := exec.Command(m.tmuxPath(), "set-environment", "-u", "-t", fullName, key)
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("unset environment: %w", err)
	}
	return nil
}

func (m *Manager) dockerRunCommand(name string) (string, error) {
	if m.SandboxImage == "" {
		return "", fmt.Errorf("sandbox image is required")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("resolve home dir: %w", err)
	}
	port := m.Port
	if port == "" {
		port = "8080"
	}
	sessionsDir := m.SessionsDir
	if sessionsDir == "" {
		return "", fmt.Errorf("sessions dir is required for sandbox mode")
	}

	cmdParts := []string{
		"docker", "run", "-it",
		fmt.Sprintf("--name=claude-worker-%s", name),
		"--rm",
	}

	if runtime.GOOS == "linux" {
		cmdParts = append(cmdParts, "--add-host=host.docker.internal:host-gateway")
	}

	cmdParts = append(cmdParts, fmt.Sprintf("-v=%s:/workspace", home))

	for _, mount := range m.SandboxMounts {
		host := mount.HostPath
		container := mount.ContainerPath
		if container == "" {
			container = host
		}
		if host == "" {
			continue
		}
		if mount.ReadOnly {
			cmdParts = append(cmdParts, fmt.Sprintf("-v=%s:%s:ro", host, container))
		} else {
			cmdParts = append(cmdParts, fmt.Sprintf("-v=%s:%s", host, container))
		}
	}

	cmdParts = append(cmdParts, fmt.Sprintf("-v=%s:%s", sessionsDir, sessionsDir))

	if err := os.MkdirAll(sandboxInboxRoot, 0700); err != nil {
		return "", fmt.Errorf("create inbox dir: %w", err)
	}
	cmdParts = append(cmdParts, fmt.Sprintf("-v=%s:%s", sandboxInboxRoot, sandboxInboxRoot))

	bridgeURL := strings.TrimSpace(m.BridgeURL)
	if bridgeURL == "" {
		bridgeURL = fmt.Sprintf("http://host.docker.internal:%s", port)
	}

	cmdParts = append(cmdParts,
		fmt.Sprintf("-e=BRIDGE_URL=%s", bridgeURL),
		fmt.Sprintf("-e=PORT=%s", port),
		fmt.Sprintf("-e=TMUX_PREFIX=%s", m.Prefix),
		fmt.Sprintf("-e=SESSIONS_DIR=%s", sessionsDir),
		fmt.Sprintf("-e=BRIDGE_SESSION=%s", name),
		"-e=TMUX_FALLBACK=1",
	)

	cmdParts = append(cmdParts, "-w", "/workspace")
	cmdParts = append(cmdParts, m.SandboxImage)
	cmdParts = append(cmdParts, "claude --dangerously-skip-permissions")

	return strings.Join(cmdParts, " "), nil
}

func (m *Manager) stopDockerContainer(name string) {
	containerName := fmt.Sprintf("claude-worker-%s", name)
	exec.Command("docker", "stop", containerName).Run()
	exec.Command("docker", "rm", "-f", containerName).Run()
}

// Helper functions

func (m *Manager) tmuxPath() string {
	if m.TmuxPath != "" {
		return m.TmuxPath
	}
	return "tmux"
}

func (m *Manager) sessionExistsFull(fullName string) bool {
	cmd := exec.Command(m.tmuxPath(), "has-session", "-t", fullName)
	return cmd.Run() == nil
}

func (m *Manager) getSendLock(sessionName string) *sync.Mutex {
	m.sendLocksMutex.Lock()
	defer m.sendLocksMutex.Unlock()

	if m.sendLocks == nil {
		m.sendLocks = make(map[string]*sync.Mutex)
	}

	if _, ok := m.sendLocks[sessionName]; !ok {
		m.sendLocks[sessionName] = &sync.Mutex{}
	}

	return m.sendLocks[sessionName]
}

// parseSessionName extracts the worker name from a full tmux session name.
// Returns the name and whether it matched the prefix.
func parseSessionName(prefix, sessionName string) (string, bool) {
	if !strings.HasPrefix(sessionName, prefix) {
		return "", false
	}
	return sessionName[len(prefix):], true
}

// fullSessionName creates the full tmux session name from prefix and worker name.
func fullSessionName(prefix, name string) string {
	return prefix + name
}
