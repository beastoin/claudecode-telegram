// Package files provides inbox management for file attachments from Telegram.
package files

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// InboxDir returns the inbox path for a given worker.
// The inbox directory is where downloaded files are stored.
func InboxDir(sessionsDir, workerName string) string {
	// Clean the sessions dir to handle trailing slashes
	sessionsDir = filepath.Clean(sessionsDir)
	return filepath.Join(sessionsDir, workerName, "inbox")
}

func chatIDPath(sessionsDir, workerName string) string {
	sessionsDir = filepath.Clean(sessionsDir)
	return filepath.Join(sessionsDir, workerName, "chat_id")
}

// SaveChatID stores the chat ID for a worker session.
// It creates the session directory if it doesn't exist.
// The chat_id file is written with secure permissions (0600).
func SaveChatID(sessionsDir, workerName, chatID string) error {
	if strings.TrimSpace(sessionsDir) == "" {
		return fmt.Errorf("sessions dir is empty")
	}
	if strings.TrimSpace(workerName) == "" {
		return fmt.Errorf("worker name is empty")
	}
	chatID = strings.TrimSpace(chatID)
	if chatID == "" {
		return fmt.Errorf("chat_id is empty")
	}

	sessionDir := filepath.Join(filepath.Clean(sessionsDir), workerName)
	if err := os.MkdirAll(sessionDir, 0700); err != nil {
		return fmt.Errorf("create session directory: %w", err)
	}

	path := chatIDPath(sessionsDir, workerName)
	if err := os.WriteFile(path, []byte(chatID), 0600); err != nil {
		return fmt.Errorf("write chat_id: %w", err)
	}

	return nil
}

// GetChatID retrieves the chat ID for a worker session.
func GetChatID(sessionsDir, workerName string) (string, error) {
	if strings.TrimSpace(sessionsDir) == "" {
		return "", fmt.Errorf("sessions dir is empty")
	}
	if strings.TrimSpace(workerName) == "" {
		return "", fmt.Errorf("worker name is empty")
	}

	data, err := os.ReadFile(chatIDPath(sessionsDir, workerName))
	if err != nil {
		return "", err
	}

	chatID := strings.TrimSpace(string(data))
	if chatID == "" {
		return "", fmt.Errorf("chat_id is empty")
	}

	return chatID, nil
}

// GetAllChatIDs returns all unique chat IDs found in session directories.
func GetAllChatIDs(sessionsDir string) ([]string, error) {
	if strings.TrimSpace(sessionsDir) == "" {
		return nil, nil
	}

	sessionsDir = filepath.Clean(sessionsDir)
	info, err := os.Stat(sessionsDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("stat sessions dir: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("sessions path is not a directory: %s", sessionsDir)
	}

	entries, err := os.ReadDir(sessionsDir)
	if err != nil {
		return nil, fmt.Errorf("read sessions dir: %w", err)
	}

	chatIDs := make(map[string]struct{})
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		path := filepath.Join(sessionsDir, entry.Name(), "chat_id")
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		chatID := strings.TrimSpace(string(data))
		if chatID == "" {
			continue
		}
		chatIDs[chatID] = struct{}{}
	}

	ids := make([]string, 0, len(chatIDs))
	for id := range chatIDs {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	return ids, nil
}

// SaveFile saves file data to the inbox directory and returns the file path.
// It creates the inbox directory if it doesn't exist.
// It generates unique filenames to avoid conflicts.
// Files are created with secure permissions (0600).
func SaveFile(inboxDir, filename string, data []byte) (string, error) {
	// Create inbox directory with secure permissions (0700)
	if err := os.MkdirAll(inboxDir, 0700); err != nil {
		return "", fmt.Errorf("create inbox directory: %w", err)
	}

	// Generate unique filename
	path := generateUniquePath(inboxDir, filename)

	// Write file with secure permissions (0600)
	if err := os.WriteFile(path, data, 0600); err != nil {
		return "", fmt.Errorf("write file: %w", err)
	}

	return path, nil
}

// generateUniquePath generates a unique file path, adding a timestamp suffix if needed.
func generateUniquePath(dir, filename string) string {
	basePath := filepath.Join(dir, filename)

	// If file doesn't exist, use the original name
	if _, err := os.Stat(basePath); os.IsNotExist(err) {
		return basePath
	}

	// File exists, add timestamp to make it unique
	ext := filepath.Ext(filename)
	name := strings.TrimSuffix(filename, ext)
	timestamp := time.Now().UnixNano()

	return filepath.Join(dir, fmt.Sprintf("%s_%d%s", name, timestamp, ext))
}

// pendingPath returns the path to the pending file for a session.
func pendingPath(sessionsDir, workerName string) string {
	sessionsDir = filepath.Clean(sessionsDir)
	return filepath.Join(sessionsDir, workerName, "pending")
}

// SetPending marks a session as having a pending request.
// Writes the current timestamp to the pending file.
func SetPending(sessionsDir, workerName string) error {
	if strings.TrimSpace(sessionsDir) == "" {
		return fmt.Errorf("sessions dir is empty")
	}
	if strings.TrimSpace(workerName) == "" {
		return fmt.Errorf("worker name is empty")
	}

	sessionDir := filepath.Join(filepath.Clean(sessionsDir), workerName)
	if err := os.MkdirAll(sessionDir, 0700); err != nil {
		return fmt.Errorf("create session directory: %w", err)
	}

	path := pendingPath(sessionsDir, workerName)
	timestamp := fmt.Sprintf("%d", time.Now().Unix())
	if err := os.WriteFile(path, []byte(timestamp), 0600); err != nil {
		return fmt.Errorf("write pending file: %w", err)
	}

	return nil
}

// ClearPending removes the pending status for a session.
func ClearPending(sessionsDir, workerName string) {
	if strings.TrimSpace(sessionsDir) == "" || strings.TrimSpace(workerName) == "" {
		return
	}
	path := pendingPath(sessionsDir, workerName)
	os.Remove(path) // Ignore errors - file may not exist
}

// IsPending checks if a session has a pending request.
// Auto-clears pending status if it's older than 10 minutes.
func IsPending(sessionsDir, workerName string) bool {
	if strings.TrimSpace(sessionsDir) == "" || strings.TrimSpace(workerName) == "" {
		return false
	}

	path := pendingPath(sessionsDir, workerName)
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}

	// Parse timestamp and check for 10 minute timeout
	var ts int64
	if _, err := fmt.Sscanf(string(data), "%d", &ts); err != nil {
		return false
	}

	// Auto-clear if older than 10 minutes
	if time.Now().Unix()-ts > 600 {
		os.Remove(path)
		return false
	}

	return true
}

// CleanupInbox removes files older than maxAge from the inbox directory.
// It only removes regular files, not subdirectories.
// Returns nil if the directory doesn't exist or is empty.
func CleanupInbox(inboxDir string, maxAge time.Duration) error {
	// Check if directory exists
	info, err := os.Stat(inboxDir)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("stat inbox directory: %w", err)
	}
	if !info.IsDir() {
		return fmt.Errorf("inbox path is not a directory: %s", inboxDir)
	}

	// Read directory entries
	entries, err := os.ReadDir(inboxDir)
	if err != nil {
		return fmt.Errorf("read inbox directory: %w", err)
	}

	cutoff := time.Now().Add(-maxAge)

	for _, entry := range entries {
		// Skip directories
		if entry.IsDir() {
			continue
		}

		filePath := filepath.Join(inboxDir, entry.Name())
		fileInfo, err := entry.Info()
		if err != nil {
			// Skip files we can't stat
			continue
		}

		// Check if file is older than maxAge
		if fileInfo.ModTime().Before(cutoff) {
			if err := os.Remove(filePath); err != nil {
				// Log but don't fail on individual file removal errors
				continue
			}
		}
	}

	return nil
}
