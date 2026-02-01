package telegram

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestSetMessageReaction(t *testing.T) {
	var receivedBody map[string]interface{}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if !strings.HasSuffix(r.URL.Path, "/setMessageReaction") {
			t.Errorf("expected path to end with /setMessageReaction, got %s", r.URL.Path)
		}

		body, _ := io.ReadAll(r.Body)
		json.Unmarshal(body, &receivedBody)

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"ok": true, "result": true}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SetMessageReaction("123456", 789, "👀")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if receivedBody["chat_id"] != "123456" {
		t.Errorf("expected chat_id '123456', got %v", receivedBody["chat_id"])
	}

	msgID, ok := receivedBody["message_id"].(float64)
	if !ok || int64(msgID) != 789 {
		t.Errorf("expected message_id 789, got %v", receivedBody["message_id"])
	}

	// Check reaction array structure
	reaction, ok := receivedBody["reaction"].([]interface{})
	if !ok || len(reaction) != 1 {
		t.Fatalf("expected reaction array with 1 element, got %v", receivedBody["reaction"])
	}

	reactionItem, ok := reaction[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected reaction item to be object, got %v", reaction[0])
	}

	if reactionItem["type"] != "emoji" {
		t.Errorf("expected reaction type 'emoji', got %v", reactionItem["type"])
	}
	if reactionItem["emoji"] != "👀" {
		t.Errorf("expected emoji '👀', got %v", reactionItem["emoji"])
	}
}

func TestSetMessageReactionAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"ok": false, "description": "Bad Request: message not found"}`))
	}))
	defer server.Close()

	client := NewClient("test-token", "123456")
	client.baseURL = server.URL

	err := client.SetMessageReaction("123456", 999, "👀")
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !strings.Contains(err.Error(), "message not found") {
		t.Errorf("expected error to contain 'message not found', got %v", err)
	}
}
