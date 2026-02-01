package telegram

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNewClient(t *testing.T) {
	client := NewClient("test-token", "123456")
	if client.token != "test-token" {
		t.Fatalf("expected token 'test-token', got %q", client.token)
	}
	if client.adminChatID != "123456" {
		t.Fatalf("expected adminChatID '123456', got %q", client.adminChatID)
	}
	if client.httpClient == nil {
		t.Fatal("expected httpClient to be set")
	}
}

func TestSendMessage(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/sendMessage") {
			t.Errorf("expected path to end with /sendMessage, got %s", r.URL.Path)
		}

		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendMessage("123456", "Hello, World!")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if receivedBody["chat_id"] != "123456" {
		t.Errorf("expected chat_id '123456', got %v", receivedBody["chat_id"])
	}
	if receivedBody["text"] != "Hello, World!" {
		t.Errorf("expected text 'Hello, World!', got %v", receivedBody["text"])
	}
}

func TestSendMessageHTML(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/sendMessage") {
			t.Errorf("expected path to end with /sendMessage, got %s", r.URL.Path)
		}

		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendMessageHTML("123456", "<b>Hello</b>")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if receivedBody["chat_id"] != "123456" {
		t.Errorf("expected chat_id '123456', got %v", receivedBody["chat_id"])
	}
	if receivedBody["text"] != "<b>Hello</b>" {
		t.Errorf("expected text '<b>Hello</b>', got %v", receivedBody["text"])
	}
	if receivedBody["parse_mode"] != "HTML" {
		t.Errorf("expected parse_mode 'HTML', got %v", receivedBody["parse_mode"])
	}
}

func TestSendMessageHTMLAutoSplit(t *testing.T) {
	var modes []string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req map[string]interface{}
		json.Unmarshal(body, &req)
		if mode, ok := req["parse_mode"].(string); ok {
			modes = append(modes, mode)
		} else {
			modes = append(modes, "")
		}

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	longText := strings.Repeat("a", 3000) + "\n\n" + strings.Repeat("b", 2000)
	err := client.SendMessageHTML("123456", longText)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(modes) != 2 {
		t.Fatalf("expected 2 messages (split), got %d", len(modes))
	}
	for _, mode := range modes {
		if mode != "HTML" {
			t.Errorf("expected parse_mode 'HTML', got %v", mode)
		}
	}
}

func TestSendMessageAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"ok": false, "description": "Bad Request: chat not found"}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendMessage("invalid-chat", "Hello")
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "chat not found") {
		t.Errorf("expected error to contain 'chat not found', got %v", err)
	}
}

func TestSendMessageAutoSplit(t *testing.T) {
	var messages []string

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req map[string]interface{}
		json.Unmarshal(body, &req)
		messages = append(messages, req["text"].(string))

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"message_id": 1}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	// Create a message that exceeds MaxMessageLength
	longText := strings.Repeat("a", 3000) + "\n\n" + strings.Repeat("b", 2000)
	err := client.SendMessage("123456", longText)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(messages) != 2 {
		t.Fatalf("expected 2 messages (split), got %d", len(messages))
	}
}

func TestSendChatAction(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/sendChatAction") {
			t.Errorf("expected path to end with /sendChatAction, got %s", r.URL.Path)
		}

		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": true}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SendChatAction("123456", "typing")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if receivedBody["chat_id"] != "123456" {
		t.Errorf("expected chat_id '123456', got %v", receivedBody["chat_id"])
	}
	if receivedBody["action"] != "typing" {
		t.Errorf("expected action 'typing', got %v", receivedBody["action"])
	}
}

func TestSetWebhook(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/setWebhook") {
			t.Errorf("expected path to end with /setWebhook, got %s", r.URL.Path)
		}

		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": true}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SetWebhook("https://example.com/webhook")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if receivedBody["url"] != "https://example.com/webhook" {
		t.Errorf("expected url 'https://example.com/webhook', got %v", receivedBody["url"])
	}
}

func TestGetMe(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/getMe") {
			t.Errorf("expected path to end with /getMe, got %s", r.URL.Path)
		}

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": {"id": 12345, "is_bot": true, "first_name": "TestBot", "username": "test_bot"}}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	user, err := client.GetMe()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if user.ID != 12345 {
		t.Errorf("expected ID 12345, got %d", user.ID)
	}
	if !user.IsBot {
		t.Error("expected IsBot to be true")
	}
	if user.FirstName != "TestBot" {
		t.Errorf("expected FirstName 'TestBot', got %s", user.FirstName)
	}
	if user.Username != "test_bot" {
		t.Errorf("expected Username 'test_bot', got %s", user.Username)
	}
}

func TestGetMeAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte(`{"ok": false, "description": "Unauthorized"}`))
	}))
	defer server.Close()

	client := NewClient("invalid-token", "123456")
	client.baseURL = server.URL

	_, err := client.GetMe()
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "Unauthorized") {
		t.Errorf("expected error to contain 'Unauthorized', got %v", err)
	}
}

func TestDownloadFile(t *testing.T) {
	fileContent := []byte("test file content")

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/getFile") {
			// First call: getFile to get file path
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"ok": true, "result": {"file_id": "abc123", "file_path": "documents/test.txt"}}`))
			return
		}

		// Second call: download the file
		if strings.Contains(r.URL.Path, "/file/") {
			w.Write(fileContent)
			return
		}

		t.Errorf("unexpected path: %s", r.URL.Path)
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	data, err := client.DownloadFile("abc123")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if string(data) != string(fileContent) {
		t.Errorf("expected content %q, got %q", string(fileContent), string(data))
	}
}

func TestDownloadFileGetFileError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"ok": false, "description": "Bad Request: file not found"}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	_, err := client.DownloadFile("invalid-file-id")
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "file not found") {
		t.Errorf("expected error to contain 'file not found', got %v", err)
	}
}

func TestNetworkError(t *testing.T) {
	client := NewClient("test-token", "123456")
	client.baseURL = "http://localhost:1" // Invalid port, will fail to connect

	err := client.SendMessage("123456", "Hello")
	if err == nil {
		t.Fatal("expected error for network failure, got nil")
	}
}

func TestAdminChatID(t *testing.T) {
	client := NewClient("test-token", "admin123")
	if client.AdminChatID() != "admin123" {
		t.Errorf("expected AdminChatID 'admin123', got %s", client.AdminChatID())
	}
}

func TestSetMyCommands(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/setMyCommands") {
			t.Errorf("expected path to end with /setMyCommands, got %s", r.URL.Path)
		}

		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": true}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	commands := []BotCommand{
		{Command: "hire", Description: "Add a new worker"},
		{Command: "end", Description: "Remove a worker"},
		{Command: "alice", Description: "Message worker alice"},
	}

	err := client.SetMyCommands(commands)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify the commands were sent correctly
	receivedCommands, ok := receivedBody["commands"].([]interface{})
	if !ok {
		t.Fatal("expected commands array in request body")
	}
	if len(receivedCommands) != 3 {
		t.Fatalf("expected 3 commands, got %d", len(receivedCommands))
	}

	// Check first command
	cmd1 := receivedCommands[0].(map[string]interface{})
	if cmd1["command"] != "hire" {
		t.Errorf("expected first command 'hire', got %v", cmd1["command"])
	}
	if cmd1["description"] != "Add a new worker" {
		t.Errorf("expected description 'Add a new worker', got %v", cmd1["description"])
	}
}

func TestSetMyCommandsAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"ok": false, "description": "Bad Request: invalid commands"}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	commands := []BotCommand{
		{Command: "hire", Description: "Add a new worker"},
	}

	err := client.SetMyCommands(commands)
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "invalid commands") {
		t.Errorf("expected error to contain 'invalid commands', got %v", err)
	}
}
