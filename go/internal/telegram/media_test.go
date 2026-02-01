package telegram

import (
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSendPhoto(t *testing.T) {
	// Create a temp file for testing
	tmpFile, _ := os.CreateTemp("", "test_photo_*.png")
	tmpFile.WriteString("fake png data")
	tmpFile.Close()
	defer os.Remove(tmpFile.Name())

	var receivedContentType string
	var receivedChatID string
	var receivedCaption string
	var receivedFilename string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/sendPhoto") {
			t.Errorf("expected path to end with /sendPhoto, got %s", r.URL.Path)
		}

		receivedContentType = r.Header.Get("Content-Type")

		// Parse multipart form
		r.ParseMultipartForm(32 << 20)
		receivedChatID = r.FormValue("chat_id")
		receivedCaption = r.FormValue("caption")

		file, header, _ := r.FormFile("photo")
		if file != nil {
			receivedFilename = header.Filename
			file.Close()
		}

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendPhoto("123456", tmpFile.Name(), "Test caption")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if !strings.HasPrefix(receivedContentType, "multipart/form-data") {
		t.Errorf("expected multipart/form-data, got %s", receivedContentType)
	}
	if receivedChatID != "123456" {
		t.Errorf("expected chat_id '123456', got %q", receivedChatID)
	}
	if receivedCaption != "Test caption" {
		t.Errorf("expected caption 'Test caption', got %q", receivedCaption)
	}
	if receivedFilename == "" {
		t.Error("expected filename to be set")
	}
}

func TestSendPhotoNoCaption(t *testing.T) {
	tmpFile, _ := os.CreateTemp("", "test_photo_*.png")
	tmpFile.WriteString("fake png data")
	tmpFile.Close()
	defer os.Remove(tmpFile.Name())

	var receivedCaption string
	var captionReceived bool

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.ParseMultipartForm(32 << 20)
		receivedCaption = r.FormValue("caption")
		captionReceived = r.Form.Has("caption")

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendPhoto("123456", tmpFile.Name(), "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Empty caption should not be sent
	if captionReceived && receivedCaption != "" {
		t.Errorf("expected no caption for empty string, got %q", receivedCaption)
	}
}

func TestSendPhotoFileNotFound(t *testing.T) {
	client := NewClient("test-token", "123456")

	err := client.SendPhoto("123456", "/nonexistent/file.png", "caption")
	if err == nil {
		t.Fatal("expected error for nonexistent file, got nil")
	}
}

func TestSendPhotoAPIError(t *testing.T) {
	tmpFile, _ := os.CreateTemp("", "test_photo_*.png")
	tmpFile.WriteString("fake png data")
	tmpFile.Close()
	defer os.Remove(tmpFile.Name())

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"ok": false, "description": "Bad Request: photo is too large"}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendPhoto("123456", tmpFile.Name(), "caption")
	if err == nil {
		t.Fatal("expected error for API error, got nil")
	}
	if !strings.Contains(err.Error(), "photo is too large") {
		t.Errorf("expected error about photo size, got %v", err)
	}
}

func TestSendDocument(t *testing.T) {
	tmpFile, _ := os.CreateTemp("", "test_doc_*.pdf")
	tmpFile.WriteString("fake pdf data")
	tmpFile.Close()
	defer os.Remove(tmpFile.Name())

	var receivedContentType string
	var receivedChatID string
	var receivedCaption string
	var receivedFilename string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/sendDocument") {
			t.Errorf("expected path to end with /sendDocument, got %s", r.URL.Path)
		}

		receivedContentType = r.Header.Get("Content-Type")

		// Parse multipart form
		r.ParseMultipartForm(32 << 20)
		receivedChatID = r.FormValue("chat_id")
		receivedCaption = r.FormValue("caption")

		file, header, _ := r.FormFile("document")
		if file != nil {
			receivedFilename = header.Filename
			file.Close()
		}

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendDocument("123456", tmpFile.Name(), "Test document")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if !strings.HasPrefix(receivedContentType, "multipart/form-data") {
		t.Errorf("expected multipart/form-data, got %s", receivedContentType)
	}
	if receivedChatID != "123456" {
		t.Errorf("expected chat_id '123456', got %q", receivedChatID)
	}
	if receivedCaption != "Test document" {
		t.Errorf("expected caption 'Test document', got %q", receivedCaption)
	}
	if receivedFilename == "" {
		t.Error("expected filename to be set")
	}
}

func TestSendDocumentNoCaption(t *testing.T) {
	tmpFile, _ := os.CreateTemp("", "test_doc_*.pdf")
	tmpFile.WriteString("fake pdf data")
	tmpFile.Close()
	defer os.Remove(tmpFile.Name())

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendDocument("123456", tmpFile.Name(), "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestSendDocumentFileNotFound(t *testing.T) {
	client := NewClient("test-token", "123456")

	err := client.SendDocument("123456", "/nonexistent/file.pdf", "caption")
	if err == nil {
		t.Fatal("expected error for nonexistent file, got nil")
	}
}

func TestSendDocumentAPIError(t *testing.T) {
	tmpFile, _ := os.CreateTemp("", "test_doc_*.pdf")
	tmpFile.WriteString("fake pdf data")
	tmpFile.Close()
	defer os.Remove(tmpFile.Name())

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"ok": false, "description": "Bad Request: file is too large"}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendDocument("123456", tmpFile.Name(), "caption")
	if err == nil {
		t.Fatal("expected error for API error, got nil")
	}
	if !strings.Contains(err.Error(), "file is too large") {
		t.Errorf("expected error about file size, got %v", err)
	}
}

// Helper function to check multipart form field exists
func parseMultipartField(r *http.Request, field string) (string, bool) {
	if err := r.ParseMultipartForm(32 << 20); err != nil {
		return "", false
	}
	values := r.Form[field]
	if len(values) == 0 {
		return "", false
	}
	return values[0], true
}

// Helper for checking file upload
func parseMultipartFile(r *http.Request, field string) (*multipart.FileHeader, error) {
	if err := r.ParseMultipartForm(32 << 20); err != nil {
		return nil, err
	}
	_, header, err := r.FormFile(field)
	return header, err
}

// Verify we preserve original filename
func TestSendPhotoPreservesFilename(t *testing.T) {
	tmpDir := t.TempDir()
	filePath := filepath.Join(tmpDir, "my_screenshot.png")
	os.WriteFile(filePath, []byte("fake png data"), 0644)

	var receivedFilename string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.ParseMultipartForm(32 << 20)
		_, header, _ := r.FormFile("photo")
		if header != nil {
			receivedFilename = header.Filename
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	client.SendPhoto("123456", filePath, "")

	if receivedFilename != "my_screenshot.png" {
		t.Errorf("expected filename 'my_screenshot.png', got %q", receivedFilename)
	}
}

func TestSendDocumentPreservesFilename(t *testing.T) {
	tmpDir := t.TempDir()
	filePath := filepath.Join(tmpDir, "report_2024.pdf")
	os.WriteFile(filePath, []byte("fake pdf data"), 0644)

	var receivedFilename string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.ParseMultipartForm(32 << 20)
		_, header, _ := r.FormFile("document")
		if header != nil {
			receivedFilename = header.Filename
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	client.SendDocument("123456", filePath, "")

	if receivedFilename != "report_2024.pdf" {
		t.Errorf("expected filename 'report_2024.pdf', got %q", receivedFilename)
	}
}

// Suppress unused import warning
var _ = io.EOF
