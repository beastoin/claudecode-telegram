// Package telegram provides a client for the Telegram Bot API.
package telegram

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

const defaultBaseURL = "https://api.telegram.org"

// User represents a Telegram user or bot.
type User struct {
	ID        int64  `json:"id"`
	IsBot     bool   `json:"is_bot"`
	FirstName string `json:"first_name"`
	Username  string `json:"username"`
}

// File represents a file ready to be downloaded from Telegram.
type File struct {
	FileID   string `json:"file_id"`
	FilePath string `json:"file_path"`
}

// BotCommand represents a Telegram bot command for the command menu.
type BotCommand struct {
	Command     string `json:"command"`
	Description string `json:"description"`
}

// APIResponse represents a response from the Telegram API.
type APIResponse struct {
	OK          bool            `json:"ok"`
	Description string          `json:"description"`
	Result      json.RawMessage `json:"result"`
}

// Client is a Telegram Bot API client.
type Client struct {
	token       string
	adminChatID string
	httpClient  *http.Client
	baseURL     string // configurable for testing
}

// NewClient creates a new Telegram API client.
func NewClient(token, adminChatID string) *Client {
	return &Client{
		token:       token,
		adminChatID: adminChatID,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
		baseURL: defaultBaseURL,
	}
}

// AdminChatID returns the configured admin chat ID.
func (c *Client) AdminChatID() string {
	return c.adminChatID
}

// SetAdminChatID sets the admin chat ID (used for auto-learn).
func (c *Client) SetAdminChatID(chatID string) {
	c.adminChatID = chatID
}

// SetBaseURL sets the base URL for the Telegram API (for testing).
func (c *Client) SetBaseURL(url string) {
	c.baseURL = url
}

// SendMessage sends a text message to the specified chat.
// Long messages are automatically split using SplitMessage.
func (c *Client) SendMessage(chatID, text string) error {
	return c.sendMessageWithParseMode(chatID, text, "")
}

// SendMessageHTML sends a text message with HTML parse mode to the specified chat.
// Long messages are automatically split using SplitMessage.
func (c *Client) SendMessageHTML(chatID, text string) error {
	return c.sendMessageWithParseMode(chatID, text, "HTML")
}

func (c *Client) sendMessageWithParseMode(chatID, text, parseMode string) error {
	chunks := SplitMessage(text)
	for _, chunk := range chunks {
		if err := c.sendSingleMessage(chatID, chunk, parseMode); err != nil {
			return err
		}
	}
	return nil
}

func (c *Client) sendSingleMessage(chatID, text, parseMode string) error {
	payload := map[string]string{
		"chat_id": chatID,
		"text":    text,
	}
	if parseMode != "" {
		payload["parse_mode"] = parseMode
	}
	return c.postJSON("sendMessage", payload)
}

// SendChatAction sends a chat action (e.g., "typing") to the specified chat.
func (c *Client) SendChatAction(chatID, action string) error {
	payload := map[string]string{
		"chat_id": chatID,
		"action":  action,
	}
	return c.postJSON("sendChatAction", payload)
}

// SetWebhook registers a webhook URL with Telegram.
func (c *Client) SetWebhook(url string) error {
	payload := map[string]string{
		"url": url,
	}
	return c.postJSON("setWebhook", payload)
}

// WebhookInfo contains information about the current webhook.
type WebhookInfo struct {
	URL string `json:"url"`
}

// GetWebhookInfo retrieves the current webhook configuration from Telegram.
func (c *Client) GetWebhookInfo() (WebhookInfo, error) {
	url := fmt.Sprintf("%s/bot%s/getWebhookInfo", c.baseURL, c.token)

	resp, err := c.httpClient.Get(url)
	if err != nil {
		return WebhookInfo{}, fmt.Errorf("getWebhookInfo request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return WebhookInfo{}, fmt.Errorf("read response: %w", err)
	}

	var apiResp APIResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		return WebhookInfo{}, fmt.Errorf("parse response: %w", err)
	}

	if !apiResp.OK {
		return WebhookInfo{}, fmt.Errorf("telegram API error: %s", apiResp.Description)
	}

	var info WebhookInfo
	if err := json.Unmarshal(apiResp.Result, &info); err != nil {
		return WebhookInfo{}, fmt.Errorf("parse webhook info: %w", err)
	}

	return info, nil
}

// GetMe returns information about the bot.
func (c *Client) GetMe() (User, error) {
	url := fmt.Sprintf("%s/bot%s/getMe", c.baseURL, c.token)

	resp, err := c.httpClient.Get(url)
	if err != nil {
		return User{}, fmt.Errorf("getMe request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return User{}, fmt.Errorf("read response: %w", err)
	}

	var apiResp APIResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		return User{}, fmt.Errorf("parse response: %w", err)
	}

	if !apiResp.OK {
		return User{}, fmt.Errorf("telegram API error: %s", apiResp.Description)
	}

	var user User
	if err := json.Unmarshal(apiResp.Result, &user); err != nil {
		return User{}, fmt.Errorf("parse user: %w", err)
	}

	return user, nil
}

// DownloadFile downloads a file from Telegram by its file ID.
func (c *Client) DownloadFile(fileID string) ([]byte, error) {
	// First, get the file path
	filePath, err := c.getFilePath(fileID)
	if err != nil {
		return nil, err
	}

	// Then download the file
	url := fmt.Sprintf("%s/file/bot%s/%s", c.baseURL, c.token, filePath)
	resp, err := c.httpClient.Get(url)
	if err != nil {
		return nil, fmt.Errorf("download file: %w", err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read file: %w", err)
	}

	return data, nil
}

func (c *Client) getFilePath(fileID string) (string, error) {
	url := fmt.Sprintf("%s/bot%s/getFile?file_id=%s", c.baseURL, c.token, fileID)

	resp, err := c.httpClient.Get(url)
	if err != nil {
		return "", fmt.Errorf("getFile request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read response: %w", err)
	}

	var apiResp APIResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		return "", fmt.Errorf("parse response: %w", err)
	}

	if !apiResp.OK {
		return "", fmt.Errorf("telegram API error: %s", apiResp.Description)
	}

	var file File
	if err := json.Unmarshal(apiResp.Result, &file); err != nil {
		return "", fmt.Errorf("parse file: %w", err)
	}

	return file.FilePath, nil
}

// SendPhoto sends an image file to a chat.
func (c *Client) SendPhoto(chatID, filePath, caption string) error {
	return c.sendFile("sendPhoto", "photo", chatID, filePath, caption)
}

// SendDocument sends a document file to a chat.
func (c *Client) SendDocument(chatID, filePath, caption string) error {
	return c.sendFile("sendDocument", "document", chatID, filePath, caption)
}

// SetMessageReaction sets a reaction emoji on a message.
func (c *Client) SetMessageReaction(chatID string, messageID int64, emoji string) error {
	payload := map[string]interface{}{
		"chat_id":    chatID,
		"message_id": messageID,
		"reaction": []map[string]string{
			{
				"type":  "emoji",
				"emoji": emoji,
			},
		},
	}
	return c.postJSON("setMessageReaction", payload)
}

// SetMyCommands registers the bot's command menu with Telegram.
func (c *Client) SetMyCommands(commands []BotCommand) error {
	payload := map[string]interface{}{
		"commands": commands,
	}
	return c.postJSON("setMyCommands", payload)
}

func (c *Client) postJSON(method string, payload interface{}) error {
	url := fmt.Sprintf("%s/bot%s/%s", c.baseURL, c.token, method)

	data, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal payload: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("%s request failed: %w", method, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read response: %w", err)
	}

	var apiResp APIResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		return fmt.Errorf("parse response: %w", err)
	}

	if !apiResp.OK {
		return fmt.Errorf("telegram API error: %s", apiResp.Description)
	}

	return nil
}

// sendFile sends a file using multipart/form-data.
func (c *Client) sendFile(method, fieldName, chatID, filePath, caption string) error {
	// Open the file
	file, err := os.Open(filePath)
	if err != nil {
		return fmt.Errorf("open file: %w", err)
	}
	defer file.Close()

	// Create multipart form
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)

	// Add chat_id field
	if err := writer.WriteField("chat_id", chatID); err != nil {
		return fmt.Errorf("write chat_id: %w", err)
	}

	// Add caption field if not empty
	if caption != "" {
		if err := writer.WriteField("caption", caption); err != nil {
			return fmt.Errorf("write caption: %w", err)
		}
	}

	// Add file field
	part, err := writer.CreateFormFile(fieldName, filepath.Base(filePath))
	if err != nil {
		return fmt.Errorf("create form file: %w", err)
	}

	if _, err := io.Copy(part, file); err != nil {
		return fmt.Errorf("copy file: %w", err)
	}

	if err := writer.Close(); err != nil {
		return fmt.Errorf("close writer: %w", err)
	}

	// Create and send request
	url := fmt.Sprintf("%s/bot%s/%s", c.baseURL, c.token, method)
	req, err := http.NewRequest(http.MethodPost, url, &buf)
	if err != nil {
		return fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("%s request failed: %w", method, err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("read response: %w", err)
	}

	var apiResp APIResponse
	if err := json.Unmarshal(body, &apiResp); err != nil {
		return fmt.Errorf("parse response: %w", err)
	}

	if !apiResp.OK {
		return fmt.Errorf("telegram API error: %s", apiResp.Description)
	}

	return nil
}
