package tmux

import (
	"os/exec"
	"strings"
	"testing"
	"time"
)

func tmuxAvailable() bool {
	_, err := exec.LookPath("tmux")
	return err == nil
}

func TestSessionNameParsing(t *testing.T) {
	tests := []struct {
		prefix      string
		sessionName string
		wantName    string
		wantMatch   bool
	}{
		{"claude-prod-", "claude-prod-alice", "alice", true},
		{"claude-prod-", "claude-prod-bob", "bob", true},
		{"claude-prod-", "claude-dev-alice", "", false},
		{"claude-", "claude-alice", "alice", true},
		{"claude-", "other-session", "", false},
		{"claude-prod-", "claude-prod-", "", true}, // Edge case: empty name
	}

	for _, tt := range tests {
		gotName, gotMatch := parseSessionName(tt.prefix, tt.sessionName)
		if gotMatch != tt.wantMatch {
			t.Errorf("parseSessionName(%q, %q) match = %v, want %v",
				tt.prefix, tt.sessionName, gotMatch, tt.wantMatch)
		}
		if gotName != tt.wantName {
			t.Errorf("parseSessionName(%q, %q) name = %q, want %q",
				tt.prefix, tt.sessionName, gotName, tt.wantName)
		}
	}
}

func TestFullSessionName(t *testing.T) {
	tests := []struct {
		prefix   string
		name     string
		wantFull string
	}{
		{"claude-prod-", "alice", "claude-prod-alice"},
		{"claude-", "bob", "claude-bob"},
		{"test-", "worker1", "test-worker1"},
	}

	for _, tt := range tests {
		got := fullSessionName(tt.prefix, tt.name)
		if got != tt.wantFull {
			t.Errorf("fullSessionName(%q, %q) = %q, want %q",
				tt.prefix, tt.name, got, tt.wantFull)
		}
	}
}

func TestListSessionsNoTmux(t *testing.T) {
	// Create a manager with a non-existent tmux path
	m := &Manager{
		Prefix:   "test-",
		TmuxPath: "/nonexistent/tmux",
	}

	sessions, err := m.ListSessions()
	if err == nil {
		t.Error("expected error for non-existent tmux, got nil")
	}
	if sessions != nil {
		t.Errorf("expected nil sessions, got %v", sessions)
	}
}

func TestCreateSessionNoTmux(t *testing.T) {
	m := &Manager{
		Prefix:   "test-",
		TmuxPath: "/nonexistent/tmux",
	}

	err := m.CreateSession("worker", "/tmp")
	if err == nil {
		t.Error("expected error for non-existent tmux, got nil")
	}
}

func TestSendMessageNoTmux(t *testing.T) {
	m := &Manager{
		Prefix:   "test-",
		TmuxPath: "/nonexistent/tmux",
	}

	err := m.SendMessage("worker", "hello")
	if err == nil {
		t.Error("expected error for non-existent tmux, got nil")
	}
}

func TestKillSessionNoTmux(t *testing.T) {
	m := &Manager{
		Prefix:   "test-",
		TmuxPath: "/nonexistent/tmux",
	}

	err := m.KillSession("worker")
	if err == nil {
		t.Error("expected error for non-existent tmux, got nil")
	}
}

// Integration tests - require real tmux
func TestIntegrationListSessions(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	prefix := "gotest-list-"
	m := NewManager(prefix)

	// Clean up any leftover test sessions first
	cleanupTestSessions(t, prefix)

	// List should return empty initially (for our test prefix)
	sessions, err := m.ListSessions()
	if err != nil {
		t.Fatalf("ListSessions() error = %v", err)
	}
	if len(sessions) != 0 {
		t.Errorf("expected 0 sessions, got %d", len(sessions))
	}
}

func TestIntegrationCreateAndKillSession(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	prefix := "gotest-ck-"
	m := NewManager(prefix)

	// Clean up first
	cleanupTestSessions(t, prefix)

	// Create a session
	err := m.CreateSession("worker1", "/tmp")
	if err != nil {
		t.Fatalf("CreateSession() error = %v", err)
	}

	// Verify it exists
	sessions, err := m.ListSessions()
	if err != nil {
		t.Fatalf("ListSessions() error = %v", err)
	}
	if len(sessions) != 1 {
		t.Fatalf("expected 1 session, got %d", len(sessions))
	}
	if sessions[0].Name != "worker1" {
		t.Errorf("expected session name 'worker1', got %q", sessions[0].Name)
	}

	// Kill the session
	err = m.KillSession("worker1")
	if err != nil {
		t.Fatalf("KillSession() error = %v", err)
	}

	// Verify it's gone
	sessions, err = m.ListSessions()
	if err != nil {
		t.Fatalf("ListSessions() after kill error = %v", err)
	}
	if len(sessions) != 0 {
		t.Errorf("expected 0 sessions after kill, got %d", len(sessions))
	}
}

func TestIntegrationCreateDuplicate(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	prefix := "gotest-dup-"
	m := NewManager(prefix)

	// Clean up first
	cleanupTestSessions(t, prefix)
	defer cleanupTestSessions(t, prefix)

	// Create first session
	err := m.CreateSession("dup", "/tmp")
	if err != nil {
		t.Fatalf("first CreateSession() error = %v", err)
	}

	// Try to create duplicate - should fail
	err = m.CreateSession("dup", "/tmp")
	if err == nil {
		t.Error("expected error for duplicate session, got nil")
	}
	if !strings.Contains(err.Error(), "already exists") {
		t.Errorf("expected 'already exists' error, got: %v", err)
	}
}

func TestIntegrationSendMessage(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	prefix := "gotest-send-"
	m := NewManager(prefix)

	// Clean up first
	cleanupTestSessions(t, prefix)
	defer cleanupTestSessions(t, prefix)

	// Create a session
	err := m.CreateSession("sender", "/tmp")
	if err != nil {
		t.Fatalf("CreateSession() error = %v", err)
	}

	// Give the session time to initialize
	time.Sleep(100 * time.Millisecond)

	// Send a message
	err = m.SendMessage("sender", "hello world")
	if err != nil {
		t.Fatalf("SendMessage() error = %v", err)
	}

	// Send another message (test the lock doesn't deadlock)
	err = m.SendMessage("sender", "second message")
	if err != nil {
		t.Fatalf("SendMessage() second call error = %v", err)
	}
}

func TestIntegrationSendMessageNonexistent(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	m := NewManager("gotest-nonexist-")

	err := m.SendMessage("nosuchworker", "hello")
	if err == nil {
		t.Error("expected error for nonexistent session, got nil")
	}
}

func TestIntegrationKillNonexistent(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	m := NewManager("gotest-killne-")

	err := m.KillSession("nosuchworker")
	if err == nil {
		t.Error("expected error for nonexistent session, got nil")
	}
}

func TestIntegrationMultipleSessions(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	prefix := "gotest-multi-"
	m := NewManager(prefix)

	// Clean up first
	cleanupTestSessions(t, prefix)
	defer cleanupTestSessions(t, prefix)

	// Create multiple sessions
	names := []string{"alice", "bob", "charlie"}
	for _, name := range names {
		err := m.CreateSession(name, "/tmp")
		if err != nil {
			t.Fatalf("CreateSession(%q) error = %v", name, err)
		}
	}

	// List and verify
	sessions, err := m.ListSessions()
	if err != nil {
		t.Fatalf("ListSessions() error = %v", err)
	}
	if len(sessions) != 3 {
		t.Fatalf("expected 3 sessions, got %d", len(sessions))
	}

	// Verify all names are present
	foundNames := make(map[string]bool)
	for _, s := range sessions {
		foundNames[s.Name] = true
	}
	for _, name := range names {
		if !foundNames[name] {
			t.Errorf("session %q not found in list", name)
		}
	}
}

func TestIntegrationSessionIsolation(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	// Create sessions with different prefixes
	prefix1 := "gotest-iso1-"
	prefix2 := "gotest-iso2-"
	m1 := NewManager(prefix1)
	m2 := NewManager(prefix2)

	// Clean up first
	cleanupTestSessions(t, prefix1)
	cleanupTestSessions(t, prefix2)
	defer cleanupTestSessions(t, prefix1)
	defer cleanupTestSessions(t, prefix2)

	// Create sessions in both prefixes
	err := m1.CreateSession("worker", "/tmp")
	if err != nil {
		t.Fatalf("m1.CreateSession() error = %v", err)
	}

	err = m2.CreateSession("worker", "/tmp")
	if err != nil {
		t.Fatalf("m2.CreateSession() error = %v", err)
	}

	// Each manager should only see its own sessions
	sessions1, err := m1.ListSessions()
	if err != nil {
		t.Fatalf("m1.ListSessions() error = %v", err)
	}
	if len(sessions1) != 1 {
		t.Errorf("m1 expected 1 session, got %d", len(sessions1))
	}

	sessions2, err := m2.ListSessions()
	if err != nil {
		t.Fatalf("m2.ListSessions() error = %v", err)
	}
	if len(sessions2) != 1 {
		t.Errorf("m2 expected 1 session, got %d", len(sessions2))
	}
}

func TestIntegrationSessionExists(t *testing.T) {
	if !tmuxAvailable() {
		t.Skip("tmux not available")
	}

	prefix := "gotest-exists-"
	m := NewManager(prefix)

	// Clean up first
	cleanupTestSessions(t, prefix)
	defer cleanupTestSessions(t, prefix)

	// Non-existent session
	if m.SessionExists("noworker") {
		t.Error("SessionExists returned true for non-existent session")
	}

	// Create a session
	err := m.CreateSession("existstest", "/tmp")
	if err != nil {
		t.Fatalf("CreateSession() error = %v", err)
	}

	// Now it should exist
	if !m.SessionExists("existstest") {
		t.Error("SessionExists returned false for existing session")
	}

	// Kill it
	err = m.KillSession("existstest")
	if err != nil {
		t.Fatalf("KillSession() error = %v", err)
	}

	// Should no longer exist
	if m.SessionExists("existstest") {
		t.Error("SessionExists returned true after kill")
	}
}

// cleanupTestSessions kills all sessions with the given prefix
func cleanupTestSessions(t *testing.T, prefix string) {
	t.Helper()

	// List all tmux sessions
	cmd := exec.Command("tmux", "list-sessions", "-F", "#{session_name}")
	out, err := cmd.Output()
	if err != nil {
		// No sessions or tmux not running - that's fine
		return
	}

	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, prefix) {
			exec.Command("tmux", "kill-session", "-t", line).Run()
		}
	}
}
