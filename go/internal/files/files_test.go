package files

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestInboxDir(t *testing.T) {
	tests := []struct {
		name        string
		sessionsDir string
		workerName  string
		want        string
	}{
		{
			name:        "basic path",
			sessionsDir: "/home/user/.claude/telegram/sessions",
			workerName:  "alice",
			want:        "/home/user/.claude/telegram/sessions/alice/inbox",
		},
		{
			name:        "different worker",
			sessionsDir: "/var/data/sessions",
			workerName:  "bob",
			want:        "/var/data/sessions/bob/inbox",
		},
		{
			name:        "trailing slash in sessions dir",
			sessionsDir: "/home/user/.claude/telegram/sessions/",
			workerName:  "charlie",
			want:        "/home/user/.claude/telegram/sessions/charlie/inbox",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := InboxDir(tt.sessionsDir, tt.workerName)
			if got != tt.want {
				t.Errorf("InboxDir() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestSaveFile(t *testing.T) {
	// Create temporary directory for tests
	tmpDir, err := os.MkdirTemp("", "files-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	t.Run("saves file with correct content", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker1", "inbox")
		data := []byte("test file content")

		path, err := SaveFile(inboxDir, "test.txt", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		// Verify file exists and has correct content
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("Failed to read saved file: %v", err)
		}
		if string(content) != string(data) {
			t.Errorf("File content = %q, want %q", string(content), string(data))
		}
	})

	t.Run("creates inbox directory if not exists", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker2", "inbox")
		data := []byte("test data")

		_, err := SaveFile(inboxDir, "file.txt", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		// Verify directory was created
		info, err := os.Stat(inboxDir)
		if err != nil {
			t.Fatalf("Inbox directory not created: %v", err)
		}
		if !info.IsDir() {
			t.Error("Inbox path is not a directory")
		}
	})

	t.Run("returns path within inbox directory", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker3", "inbox")
		data := []byte("test")

		path, err := SaveFile(inboxDir, "myfile.png", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		// Path should be within inbox directory
		if !filepath.HasPrefix(path, inboxDir) {
			t.Errorf("Saved path %q not in inbox dir %q", path, inboxDir)
		}
	})

	t.Run("handles binary data", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker4", "inbox")
		// Binary data with null bytes and various byte values
		data := []byte{0x00, 0x01, 0xFF, 0x89, 0x50, 0x4E, 0x47} // PNG header-like

		path, err := SaveFile(inboxDir, "image.png", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("Failed to read file: %v", err)
		}
		if string(content) != string(data) {
			t.Errorf("Binary content mismatch")
		}
	})

	t.Run("generates unique filename to avoid conflicts", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker5", "inbox")

		// Save first file
		path1, err := SaveFile(inboxDir, "duplicate.txt", []byte("first"))
		if err != nil {
			t.Fatalf("SaveFile() first call error = %v", err)
		}

		// Save second file with same name
		path2, err := SaveFile(inboxDir, "duplicate.txt", []byte("second"))
		if err != nil {
			t.Fatalf("SaveFile() second call error = %v", err)
		}

		// Paths should be different
		if path1 == path2 {
			t.Error("Expected different paths for files with same name")
		}

		// Both files should exist with correct content
		content1, _ := os.ReadFile(path1)
		content2, _ := os.ReadFile(path2)
		if string(content1) != "first" {
			t.Errorf("First file content = %q, want %q", string(content1), "first")
		}
		if string(content2) != "second" {
			t.Errorf("Second file content = %q, want %q", string(content2), "second")
		}
	})

	t.Run("preserves file extension", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker6", "inbox")
		data := []byte("test")

		path, err := SaveFile(inboxDir, "document.pdf", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		if filepath.Ext(path) != ".pdf" {
			t.Errorf("File extension = %q, want %q", filepath.Ext(path), ".pdf")
		}
	})

	t.Run("handles filename without extension", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker7", "inbox")
		data := []byte("test")

		path, err := SaveFile(inboxDir, "noextension", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		// Should still save successfully
		if _, err := os.Stat(path); err != nil {
			t.Errorf("File not created: %v", err)
		}
	})

	t.Run("sets secure file permissions", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "worker8", "inbox")
		data := []byte("secret data")

		path, err := SaveFile(inboxDir, "secret.txt", data)
		if err != nil {
			t.Fatalf("SaveFile() error = %v", err)
		}

		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("Failed to stat file: %v", err)
		}

		// File should be readable/writable only by owner (0600)
		perm := info.Mode().Perm()
		if perm != 0600 {
			t.Errorf("File permissions = %o, want %o", perm, 0600)
		}
	})
}

func TestChatIDPersistence(t *testing.T) {
	tmpDir := t.TempDir()
	sessionsDir := filepath.Join(tmpDir, "sessions")

	t.Run("save and get chat_id", func(t *testing.T) {
		if err := SaveChatID(sessionsDir, "alice", "123456"); err != nil {
			t.Fatalf("SaveChatID() error = %v", err)
		}

		// Verify file content
		path := filepath.Join(sessionsDir, "alice", "chat_id")
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("failed to read chat_id file: %v", err)
		}
		if string(content) != "123456" {
			t.Errorf("chat_id content = %q, want %q", string(content), "123456")
		}

		// Verify permissions
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("failed to stat chat_id file: %v", err)
		}
		if info.Mode().Perm() != 0600 {
			t.Errorf("chat_id permissions = %o, want %o", info.Mode().Perm(), 0600)
		}

		// Verify GetChatID
		chatID, err := GetChatID(sessionsDir, "alice")
		if err != nil {
			t.Fatalf("GetChatID() error = %v", err)
		}
		if chatID != "123456" {
			t.Errorf("GetChatID() = %q, want %q", chatID, "123456")
		}
	})

	t.Run("missing chat_id returns error", func(t *testing.T) {
		if _, err := GetChatID(sessionsDir, "missing"); err == nil {
			t.Fatal("expected error for missing chat_id, got nil")
		}
	})
}

func TestGetAllChatIDs(t *testing.T) {
	tmpDir := t.TempDir()
	sessionsDir := filepath.Join(tmpDir, "sessions")

	if err := SaveChatID(sessionsDir, "alice", "123"); err != nil {
		t.Fatalf("SaveChatID() error = %v", err)
	}
	if err := SaveChatID(sessionsDir, "bob", "456"); err != nil {
		t.Fatalf("SaveChatID() error = %v", err)
	}
	if err := SaveChatID(sessionsDir, "charlie", "123"); err != nil {
		t.Fatalf("SaveChatID() error = %v", err)
	}

	// Create empty chat_id file
	emptyDir := filepath.Join(sessionsDir, "empty")
	if err := os.MkdirAll(emptyDir, 0700); err != nil {
		t.Fatalf("failed to create empty session dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(emptyDir, "chat_id"), []byte(""), 0600); err != nil {
		t.Fatalf("failed to write empty chat_id file: %v", err)
	}

	// Non-directory entry should be ignored
	if err := os.WriteFile(filepath.Join(sessionsDir, "not-a-dir"), []byte("ignore"), 0600); err != nil {
		t.Fatalf("failed to create non-dir entry: %v", err)
	}

	ids, err := GetAllChatIDs(sessionsDir)
	if err != nil {
		t.Fatalf("GetAllChatIDs() error = %v", err)
	}

	want := []string{"123", "456"}
	if !reflect.DeepEqual(ids, want) {
		t.Errorf("GetAllChatIDs() = %v, want %v", ids, want)
	}
}

func TestGetAllChatIDsMissingDir(t *testing.T) {
	tmpDir := t.TempDir()
	sessionsDir := filepath.Join(tmpDir, "missing")

	ids, err := GetAllChatIDs(sessionsDir)
	if err != nil {
		t.Fatalf("GetAllChatIDs() error = %v", err)
	}
	if len(ids) != 0 {
		t.Errorf("expected empty chat ID list, got %v", ids)
	}
}

func TestPendingFileFunctions(t *testing.T) {
	tmpDir := t.TempDir()
	sessionsDir := filepath.Join(tmpDir, "sessions")

	t.Run("SetPending creates pending file with timestamp", func(t *testing.T) {
		if err := SetPending(sessionsDir, "alice"); err != nil {
			t.Fatalf("SetPending() error = %v", err)
		}

		// Verify file exists
		path := filepath.Join(sessionsDir, "alice", "pending")
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("pending file not created: %v", err)
		}

		// Verify content is a valid timestamp
		var ts int64
		if _, err := fmt.Sscanf(string(data), "%d", &ts); err != nil {
			t.Fatalf("pending file does not contain valid timestamp: %v", err)
		}

		// Timestamp should be recent (within last 5 seconds)
		now := time.Now().Unix()
		if now-ts > 5 {
			t.Errorf("timestamp too old: %d (now: %d)", ts, now)
		}

		// Verify secure permissions
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("failed to stat pending file: %v", err)
		}
		if info.Mode().Perm() != 0600 {
			t.Errorf("pending permissions = %o, want %o", info.Mode().Perm(), 0600)
		}
	})

	t.Run("IsPending returns true for recent pending", func(t *testing.T) {
		if err := SetPending(sessionsDir, "bob"); err != nil {
			t.Fatalf("SetPending() error = %v", err)
		}

		if !IsPending(sessionsDir, "bob") {
			t.Error("IsPending() = false, want true")
		}
	})

	t.Run("IsPending returns false when no pending file", func(t *testing.T) {
		if IsPending(sessionsDir, "nonexistent") {
			t.Error("IsPending() = true for nonexistent session, want false")
		}
	})

	t.Run("ClearPending removes pending file", func(t *testing.T) {
		if err := SetPending(sessionsDir, "charlie"); err != nil {
			t.Fatalf("SetPending() error = %v", err)
		}

		ClearPending(sessionsDir, "charlie")

		if IsPending(sessionsDir, "charlie") {
			t.Error("IsPending() = true after ClearPending, want false")
		}
	})

	t.Run("ClearPending is idempotent (no error if no file)", func(t *testing.T) {
		// Should not panic or error
		ClearPending(sessionsDir, "nonexistent")
	})

	t.Run("IsPending auto-clears after 10 minutes", func(t *testing.T) {
		// Create pending file with old timestamp (11 minutes ago)
		workerDir := filepath.Join(sessionsDir, "expired")
		if err := os.MkdirAll(workerDir, 0700); err != nil {
			t.Fatalf("failed to create worker dir: %v", err)
		}

		oldTs := time.Now().Unix() - 660 // 11 minutes ago
		pendingPath := filepath.Join(workerDir, "pending")
		if err := os.WriteFile(pendingPath, []byte(fmt.Sprintf("%d", oldTs)), 0600); err != nil {
			t.Fatalf("failed to write old pending file: %v", err)
		}

		// Should return false and auto-clear
		if IsPending(sessionsDir, "expired") {
			t.Error("IsPending() = true for expired pending, want false")
		}

		// File should be removed
		if _, err := os.Stat(pendingPath); !os.IsNotExist(err) {
			t.Error("expired pending file should have been auto-cleared")
		}
	})

	t.Run("SetPending with empty sessionsDir returns error", func(t *testing.T) {
		if err := SetPending("", "alice"); err == nil {
			t.Error("SetPending() with empty sessionsDir should error")
		}
	})

	t.Run("SetPending with empty workerName returns error", func(t *testing.T) {
		if err := SetPending(sessionsDir, ""); err == nil {
			t.Error("SetPending() with empty workerName should error")
		}
	})

	t.Run("IsPending with empty params returns false", func(t *testing.T) {
		if IsPending("", "alice") {
			t.Error("IsPending() with empty sessionsDir should return false")
		}
		if IsPending(sessionsDir, "") {
			t.Error("IsPending() with empty workerName should return false")
		}
	})
}

func TestCleanupInbox(t *testing.T) {
	// Create temporary directory for tests
	tmpDir, err := os.MkdirTemp("", "cleanup-test-*")
	if err != nil {
		t.Fatalf("Failed to create temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	t.Run("deletes files older than maxAge", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "cleanup1")
		if err := os.MkdirAll(inboxDir, 0700); err != nil {
			t.Fatalf("Failed to create inbox dir: %v", err)
		}

		// Create an old file
		oldFile := filepath.Join(inboxDir, "old.txt")
		if err := os.WriteFile(oldFile, []byte("old"), 0600); err != nil {
			t.Fatalf("Failed to create old file: %v", err)
		}
		// Set modification time to 2 hours ago
		oldTime := time.Now().Add(-2 * time.Hour)
		if err := os.Chtimes(oldFile, oldTime, oldTime); err != nil {
			t.Fatalf("Failed to set file time: %v", err)
		}

		// Run cleanup with 1 hour max age
		if err := CleanupInbox(inboxDir, 1*time.Hour); err != nil {
			t.Fatalf("CleanupInbox() error = %v", err)
		}

		// Old file should be deleted
		if _, err := os.Stat(oldFile); !os.IsNotExist(err) {
			t.Error("Old file should have been deleted")
		}
	})

	t.Run("keeps files newer than maxAge", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "cleanup2")
		if err := os.MkdirAll(inboxDir, 0700); err != nil {
			t.Fatalf("Failed to create inbox dir: %v", err)
		}

		// Create a recent file
		recentFile := filepath.Join(inboxDir, "recent.txt")
		if err := os.WriteFile(recentFile, []byte("recent"), 0600); err != nil {
			t.Fatalf("Failed to create recent file: %v", err)
		}

		// Run cleanup with 1 hour max age
		if err := CleanupInbox(inboxDir, 1*time.Hour); err != nil {
			t.Fatalf("CleanupInbox() error = %v", err)
		}

		// Recent file should still exist
		if _, err := os.Stat(recentFile); err != nil {
			t.Errorf("Recent file should not have been deleted: %v", err)
		}
	})

	t.Run("handles mixed old and new files", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "cleanup3")
		if err := os.MkdirAll(inboxDir, 0700); err != nil {
			t.Fatalf("Failed to create inbox dir: %v", err)
		}

		// Create an old file
		oldFile := filepath.Join(inboxDir, "old.txt")
		if err := os.WriteFile(oldFile, []byte("old"), 0600); err != nil {
			t.Fatalf("Failed to create old file: %v", err)
		}
		oldTime := time.Now().Add(-2 * time.Hour)
		if err := os.Chtimes(oldFile, oldTime, oldTime); err != nil {
			t.Fatalf("Failed to set file time: %v", err)
		}

		// Create a recent file
		recentFile := filepath.Join(inboxDir, "recent.txt")
		if err := os.WriteFile(recentFile, []byte("recent"), 0600); err != nil {
			t.Fatalf("Failed to create recent file: %v", err)
		}

		// Run cleanup
		if err := CleanupInbox(inboxDir, 1*time.Hour); err != nil {
			t.Fatalf("CleanupInbox() error = %v", err)
		}

		// Old file should be deleted
		if _, err := os.Stat(oldFile); !os.IsNotExist(err) {
			t.Error("Old file should have been deleted")
		}

		// Recent file should remain
		if _, err := os.Stat(recentFile); err != nil {
			t.Error("Recent file should not have been deleted")
		}
	})

	t.Run("handles non-existent directory", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "nonexistent")

		// Should not error for non-existent directory
		if err := CleanupInbox(inboxDir, 1*time.Hour); err != nil {
			t.Errorf("CleanupInbox() should not error for non-existent dir: %v", err)
		}
	})

	t.Run("handles empty directory", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "cleanup4")
		if err := os.MkdirAll(inboxDir, 0700); err != nil {
			t.Fatalf("Failed to create inbox dir: %v", err)
		}

		// Should not error for empty directory
		if err := CleanupInbox(inboxDir, 1*time.Hour); err != nil {
			t.Errorf("CleanupInbox() should not error for empty dir: %v", err)
		}
	})

	t.Run("does not delete subdirectories", func(t *testing.T) {
		inboxDir := filepath.Join(tmpDir, "cleanup5")
		subDir := filepath.Join(inboxDir, "subdir")
		if err := os.MkdirAll(subDir, 0700); err != nil {
			t.Fatalf("Failed to create subdirectory: %v", err)
		}

		// Set old time on subdirectory
		oldTime := time.Now().Add(-2 * time.Hour)
		if err := os.Chtimes(subDir, oldTime, oldTime); err != nil {
			t.Fatalf("Failed to set dir time: %v", err)
		}

		// Run cleanup
		if err := CleanupInbox(inboxDir, 1*time.Hour); err != nil {
			t.Fatalf("CleanupInbox() error = %v", err)
		}

		// Subdirectory should still exist
		if _, err := os.Stat(subDir); err != nil {
			t.Error("Subdirectory should not have been deleted")
		}
	})
}
