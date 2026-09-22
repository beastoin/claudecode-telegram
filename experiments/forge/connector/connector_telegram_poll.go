package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type TelegramPollConnector struct {
	token      string
	chatID     string
	name       string
	apiBaseURL string
	offset     int64
	client     *http.Client
	inboxDir   string
}

func (t *TelegramPollConnector) apiBase() string {
	if t.apiBaseURL != "" {
		return t.apiBaseURL
	}
	return "https://api.telegram.org"
}

func init() {
	RegisterConnector("telegram-poll", func() Connector {
		return &TelegramPollConnector{}
	})
}

func (t *TelegramPollConnector) Type() string { return "telegram-poll" }

func (t *TelegramPollConnector) Capabilities() Caps {
	return CapText | CapFiles | CapMarkdown | CapVoice
}

func (t *TelegramPollConnector) Requirements() Reqs {
	return ReqRuntime
}

func (t *TelegramPollConnector) Init(ctx context.Context, cfg ConnectorConfig) error {
	t.token = cfg.Config["TELEGRAM_BOT_TOKEN"]
	if t.token == "" {
		return fmt.Errorf("telegram-poll connector requires TELEGRAM_BOT_TOKEN in config")
	}
	t.chatID = cfg.Config["TELEGRAM_CHAT_ID"]
	t.name = cfg.WorkerName
	t.apiBaseURL = cfg.Config["TELEGRAM_API_BASE"]
	t.client = &http.Client{
		Timeout: 35 * time.Second,
	}

	t.inboxDir = cfg.Config["INBOX_DIR"]
	if t.inboxDir == "" {
		t.inboxDir = filepath.Join(os.TempDir(), "forge-telegram-inbox", t.name)
	}
	os.MkdirAll(t.inboxDir, 0700)

	url := fmt.Sprintf("%s/bot%s/getMe", t.apiBase(), t.token)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := t.client.Do(req)
	if err != nil {
		return fmt.Errorf("telegram getMe failed: %w", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("telegram getMe returned %s", resp.Status)
	}

	return nil
}

const telegramMaxLength = 4096

var (
	imageTagRe = regexp.MustCompile(`\[\[image:([^\]|]+)(?:\|([^\]]*))?\]\]`)
	fileTagRe  = regexp.MustCompile(`\[\[file:([^\]|]+)(?:\|([^\]]*))?\]\]`)
)

func (t *TelegramPollConnector) Send(ctx context.Context, resp Response) error {
	chatID := resp.Target
	if chatID == "" {
		chatID = t.chatID
	}
	if chatID == "" {
		return fmt.Errorf("no chat_id configured and none in response target")
	}

	text := resp.Text

	var images []mediaTag
	text = imageTagRe.ReplaceAllStringFunc(text, func(match string) string {
		m := imageTagRe.FindStringSubmatch(match)
		if m != nil {
			path := strings.TrimSpace(m[1])
			caption := ""
			if len(m) > 2 {
				caption = strings.TrimSpace(m[2])
			}
			if _, err := os.Stat(path); err == nil {
				images = append(images, mediaTag{Path: path, Caption: caption})
				return ""
			}
		}
		return match
	})

	var files []mediaTag
	text = fileTagRe.ReplaceAllStringFunc(text, func(match string) string {
		m := fileTagRe.FindStringSubmatch(match)
		if m != nil {
			path := strings.TrimSpace(m[1])
			caption := ""
			if len(m) > 2 {
				caption = strings.TrimSpace(m[2])
			}
			if _, err := os.Stat(path); err == nil {
				files = append(files, mediaTag{Path: path, Caption: caption})
				return ""
			}
		}
		return match
	})

	text = strings.TrimSpace(text)

	if text != "" {
		chunks := splitTelegramMessage(text, telegramMaxLength-len(t.name)-10)
		var prevMsgID int64
		for _, chunk := range chunks {
			msgID, err := t.sendText(ctx, chatID, chunk, prevMsgID)
			if err != nil {
				return err
			}
			prevMsgID = msgID
		}
	}

	for _, img := range images {
		ext := strings.ToLower(filepath.Ext(img.Path))
		if ext == ".gif" || ext == ".mp4" {
			t.sendAnimation(ctx, chatID, img.Path, img.Caption)
		} else {
			t.sendPhoto(ctx, chatID, img.Path, img.Caption)
		}
	}

	for _, f := range files {
		ext := strings.ToLower(filepath.Ext(f.Path))
		switch {
		case isVideoExt(ext):
			t.sendVideo(ctx, chatID, f.Path, f.Caption)
		case isAudioExt(ext):
			t.sendAudio(ctx, chatID, f.Path, f.Caption)
		case isVoiceExt(ext):
			t.sendVoice(ctx, chatID, f.Path, f.Caption)
		default:
			t.sendDocument(ctx, chatID, f.Path, f.Caption)
		}
	}

	return nil
}

type mediaTag struct {
	Path    string
	Caption string
}

func (t *TelegramPollConnector) sendText(ctx context.Context, chatID string, text string, replyTo int64) (int64, error) {
	url := fmt.Sprintf("%s/bot%s/sendMessage", t.apiBase(), t.token)

	payload := map[string]interface{}{
		"chat_id":    chatID,
		"text":       text,
		"parse_mode": "HTML",
	}
	if replyTo > 0 {
		payload["reply_to_message_id"] = replyTo
	}

	msgID, err := t.doTextSend(ctx, url, payload)
	if err != nil && strings.Contains(err.Error(), "can't parse entities") {
		plain := stripHTML(text)
		payload = map[string]interface{}{
			"chat_id": chatID,
			"text":    plain,
		}
		if replyTo > 0 {
			payload["reply_to_message_id"] = replyTo
		}
		return t.doTextSend(ctx, url, payload)
	}
	return msgID, err
}

func (t *TelegramPollConnector) doTextSend(ctx context.Context, url string, payload map[string]interface{}) (int64, error) {
	body, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("telegram sendMessage failed: %s %s", resp.Status, string(respBody))
	}

	var result struct {
		OK     bool `json:"ok"`
		Result struct {
			MessageID int64 `json:"message_id"`
		} `json:"result"`
	}
	json.Unmarshal(respBody, &result)
	return result.Result.MessageID, nil
}

func (t *TelegramPollConnector) sendPhoto(ctx context.Context, chatID, path, caption string) error {
	return t.sendMultipart(ctx, "sendPhoto", "photo", chatID, path, caption)
}

func (t *TelegramPollConnector) sendAnimation(ctx context.Context, chatID, path, caption string) error {
	return t.sendMultipart(ctx, "sendAnimation", "animation", chatID, path, caption)
}

func (t *TelegramPollConnector) sendDocument(ctx context.Context, chatID, path, caption string) error {
	return t.sendMultipart(ctx, "sendDocument", "document", chatID, path, caption)
}

func (t *TelegramPollConnector) sendVideo(ctx context.Context, chatID, path, caption string) error {
	return t.sendMultipart(ctx, "sendVideo", "video", chatID, path, caption)
}

func (t *TelegramPollConnector) sendAudio(ctx context.Context, chatID, path, caption string) error {
	return t.sendMultipart(ctx, "sendAudio", "audio", chatID, path, caption)
}

func (t *TelegramPollConnector) sendVoice(ctx context.Context, chatID, path, caption string) error {
	return t.sendMultipart(ctx, "sendVoice", "voice", chatID, path, caption)
}

func (t *TelegramPollConnector) sendMultipart(ctx context.Context, method, field, chatID, path, caption string) error {
	url := fmt.Sprintf("%s/bot%s/%s", t.apiBase(), t.token, method)

	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)

	w.WriteField("chat_id", chatID)
	if caption != "" {
		w.WriteField("caption", caption)
	}

	ct := mime.TypeByExtension(filepath.Ext(path))
	if ct == "" {
		ct = "application/octet-stream"
	}
	h := make(textproto.MIMEHeader)
	h.Set("Content-Disposition", fmt.Sprintf(`form-data; name="%s"; filename="%s"`, field, filepath.Base(path)))
	h.Set("Content-Type", ct)
	part, err := w.CreatePart(h)
	if err != nil {
		return err
	}
	io.Copy(part, f)
	w.Close()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", w.FormDataContentType())

	resp, err := t.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	return nil
}

func (t *TelegramPollConnector) Poll(ctx context.Context) ([]InboundMessage, error) {
	url := fmt.Sprintf("%s/bot%s/getUpdates?offset=%d&timeout=30",
		t.apiBase(), t.token, t.offset)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}

	resp, err := t.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var result tgUpdatesResponse
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("parse getUpdates: %w", err)
	}

	if !result.OK {
		return nil, fmt.Errorf("getUpdates returned ok=false")
	}

	var msgs []InboundMessage
	for _, update := range result.Result {
		t.offset = update.UpdateID + 1

		msg := update.Message
		if msg == nil {
			continue
		}

		if t.chatID != "" {
			actualID := fmt.Sprintf("%d", msg.Chat.ID)
			if actualID != t.chatID {
				continue
			}
		}

		inbound := t.parseInboundMessage(msg)
		if inbound.Text == "" && len(inbound.Files) == 0 {
			continue
		}
		inbound.Raw = body
		msgs = append(msgs, inbound)
	}
	return msgs, nil
}

func (t *TelegramPollConnector) parseInboundMessage(msg *tgMessage) InboundMessage {
	from := msg.From.Username
	if from == "" {
		from = msg.From.FirstName
	}

	text := msg.Text
	if text == "" {
		text = msg.Caption
	}

	var files []File

	if len(msg.Photo) > 0 {
		largest := msg.Photo[0]
		for _, p := range msg.Photo[1:] {
			if p.FileSize > largest.FileSize {
				largest = p
			}
		}
		localPath := t.downloadFile(largest.FileID)
		if localPath != "" {
			files = append(files, File{Name: "photo.jpg", URL: localPath, Mime: "image/jpeg"})
			if text == "" {
				text = fmt.Sprintf("Sent image: `%s`", localPath)
			} else {
				text = fmt.Sprintf("%s\n\nSent image: `%s`", text, localPath)
			}
		}
	}

	if msg.Document != nil {
		localPath := t.downloadFile(msg.Document.FileID)
		if localPath != "" {
			fileName := msg.Document.FileName
			if fileName == "" {
				fileName = "document"
			}
			mimeType := msg.Document.MimeType
			size := msg.Document.FileSize
			files = append(files, File{
				Name: fileName, URL: localPath,
				Mime: mimeType, Size: int64(size),
			})
			sizeStr := formatFileSize(size)
			fileInfo := fmt.Sprintf("Sent file: %s (%s, %s)\nPath: `%s`", fileName, sizeStr, mimeType, localPath)
			if text == "" {
				text = fileInfo
			} else {
				text = fmt.Sprintf("%s\n\n%s", text, fileInfo)
			}
		}
	}

	if msg.Animation != nil {
		localPath := t.downloadFile(msg.Animation.FileID)
		if localPath != "" {
			files = append(files, File{Name: "animation.mp4", URL: localPath, Mime: "video/mp4"})
			if text == "" {
				text = fmt.Sprintf("Sent GIF: `%s`", localPath)
			} else {
				text = fmt.Sprintf("%s\n\nSent GIF: `%s`", text, localPath)
			}
		}
	}

	if msg.Voice != nil {
		localPath := t.downloadFile(msg.Voice.FileID)
		if localPath != "" {
			duration := msg.Voice.Duration
			files = append(files, File{Name: "voice.ogg", URL: localPath, Mime: "audio/ogg"})
			if text == "" {
				text = fmt.Sprintf("Sent voice message: (%ds)\nPath: `%s`", duration, localPath)
			} else {
				text = fmt.Sprintf("%s\n\nSent voice message: (%ds)\nPath: `%s`", text, duration, localPath)
			}
		}
	}

	if msg.Audio != nil {
		localPath := t.downloadFile(msg.Audio.FileID)
		if localPath != "" {
			title := msg.Audio.Title
			if title == "" {
				title = msg.Audio.FileName
			}
			if title == "" {
				title = "audio"
			}
			files = append(files, File{Name: title, URL: localPath, Mime: msg.Audio.MimeType})
			if text == "" {
				text = fmt.Sprintf("Sent audio: %s (%ds)\nPath: `%s`", title, msg.Audio.Duration, localPath)
			} else {
				text = fmt.Sprintf("%s\n\nSent audio: %s (%ds)\nPath: `%s`", text, title, msg.Audio.Duration, localPath)
			}
		}
	}

	if msg.Video != nil {
		localPath := t.downloadFile(msg.Video.FileID)
		if localPath != "" {
			fileName := msg.Video.FileName
			if fileName == "" {
				fileName = "video.mp4"
			}
			files = append(files, File{Name: fileName, URL: localPath, Mime: msg.Video.MimeType})
			if text == "" {
				text = fmt.Sprintf("Sent video: %s (%ds)\nPath: `%s`", fileName, msg.Video.Duration, localPath)
			} else {
				text = fmt.Sprintf("%s\n\nSent video: %s (%ds)\nPath: `%s`", text, fileName, msg.Video.Duration, localPath)
			}
		}
	}

	if msg.Sticker != nil {
		emoji := msg.Sticker.Emoji
		localPath := t.downloadFile(msg.Sticker.FileID)
		if localPath != "" {
			files = append(files, File{Name: "sticker.webp", URL: localPath, Mime: "image/webp"})
			if text == "" {
				text = fmt.Sprintf("Sent sticker: %s\nPath: %s", emoji, localPath)
			}
		}
	}

	var replyToID string
	if msg.ReplyToMessage != nil {
		replyToID = fmt.Sprintf("%d", msg.ReplyToMessage.MessageID)
		replyText := msg.ReplyToMessage.Text
		if replyText == "" {
			replyText = msg.ReplyToMessage.Caption
		}
		if replyText != "" && text != "" {
			text = fmt.Sprintf("%s\n\nContext (previous message):\n%s", text, replyText)
		}
	}

	return InboundMessage{
		Text:      text,
		From:      from,
		MessageID: fmt.Sprintf("%d", msg.MessageID),
		ReplyToID: replyToID,
		Files:     files,
	}
}

func (t *TelegramPollConnector) downloadFile(fileID string) string {
	if fileID == "" {
		return ""
	}

	url := fmt.Sprintf("%s/bot%s/getFile", t.apiBase(), t.token)
	payload, _ := json.Marshal(map[string]string{"file_id": fileID})
	resp, err := t.client.Post(url, "application/json", bytes.NewReader(payload))
	if err != nil {
		return ""
	}
	defer resp.Body.Close()

	var result struct {
		OK     bool `json:"ok"`
		Result struct {
			FilePath string `json:"file_path"`
			FileSize int    `json:"file_size"`
		} `json:"result"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil || !result.OK {
		return ""
	}
	if result.Result.FilePath == "" {
		return ""
	}
	if result.Result.FileSize > 50*1024*1024 {
		return ""
	}

	dlURL := fmt.Sprintf("%s/file/bot%s/%s", t.apiBase(), t.token, result.Result.FilePath)
	dlResp, err := t.client.Get(dlURL)
	if err != nil {
		return ""
	}
	defer dlResp.Body.Close()

	content, err := io.ReadAll(io.LimitReader(dlResp.Body, 50*1024*1024+1))
	if err != nil || len(content) > 50*1024*1024 {
		return ""
	}

	ext := filepath.Ext(result.Result.FilePath)
	localPath := filepath.Join(t.inboxDir, fmt.Sprintf("%d%s", time.Now().UnixNano(), ext))
	if err := os.WriteFile(localPath, content, 0600); err != nil {
		return ""
	}
	return localPath
}

func (t *TelegramPollConnector) PollInterval() time.Duration {
	return 0
}

func (t *TelegramPollConnector) Close() error { return nil }

func (t *TelegramPollConnector) SendAuthPrompt(ctx context.Context, req AuthPromptRequest) (AuthPromptResult, error) {
	chatID := t.chatID
	if chatID == "" {
		return AuthPromptResult{}, fmt.Errorf("no chat_id configured for auth prompt")
	}

	text := fmt.Sprintf("Auth required for *%s*\n\nOpen this URL to authenticate:\n%s\n\nReply to this message with the auth code.", req.WorkerName, req.URL)
	payload, _ := json.Marshal(map[string]interface{}{
		"chat_id":    chatID,
		"text":       text,
		"parse_mode": "Markdown",
		"reply_markup": map[string]interface{}{
			"force_reply":             true,
			"input_field_placeholder": "Paste auth code here",
		},
	})

	url := fmt.Sprintf("%s/bot%s/sendMessage", t.apiBase(), t.token)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
	if err != nil {
		return AuthPromptResult{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(httpReq)
	if err != nil {
		return AuthPromptResult{}, err
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return AuthPromptResult{}, fmt.Errorf("telegram sendMessage failed: %s %s", resp.Status, string(respBody))
	}

	var result struct {
		OK     bool `json:"ok"`
		Result struct {
			MessageID int64 `json:"message_id"`
		} `json:"result"`
	}
	json.Unmarshal(respBody, &result)

	return AuthPromptResult{
		MessageID: fmt.Sprintf("%d", result.Result.MessageID),
	}, nil
}

type tgUpdatesResponse struct {
	OK     bool       `json:"ok"`
	Result []tgUpdate `json:"result"`
}

type tgUpdate struct {
	UpdateID int64      `json:"update_id"`
	Message  *tgMessage `json:"message"`
}

type tgMessage struct {
	MessageID int64  `json:"message_id"`
	Text      string `json:"text"`
	Caption   string `json:"caption"`
	From      struct {
		Username  string `json:"username"`
		FirstName string `json:"first_name"`
	} `json:"from"`
	Chat struct {
		ID int64 `json:"id"`
	} `json:"chat"`
	Photo          []tgPhotoSize `json:"photo"`
	Document       *tgDocument   `json:"document"`
	Animation      *tgAnimation  `json:"animation"`
	Voice          *tgVoice      `json:"voice"`
	Audio          *tgAudio      `json:"audio"`
	Video          *tgVideo      `json:"video"`
	Sticker        *tgSticker    `json:"sticker"`
	ReplyToMessage *tgMessage    `json:"reply_to_message"`
}

type tgPhotoSize struct {
	FileID   string `json:"file_id"`
	FileSize int    `json:"file_size"`
	Width    int    `json:"width"`
	Height   int    `json:"height"`
}

type tgDocument struct {
	FileID   string `json:"file_id"`
	FileName string `json:"file_name"`
	MimeType string `json:"mime_type"`
	FileSize int    `json:"file_size"`
}

type tgAnimation struct {
	FileID   string `json:"file_id"`
	Duration int    `json:"duration"`
	FileSize int    `json:"file_size"`
}

type tgVoice struct {
	FileID   string `json:"file_id"`
	Duration int    `json:"duration"`
	FileSize int    `json:"file_size"`
}

type tgAudio struct {
	FileID   string `json:"file_id"`
	Duration int    `json:"duration"`
	Title    string `json:"title"`
	FileName string `json:"file_name"`
	MimeType string `json:"mime_type"`
	FileSize int    `json:"file_size"`
}

type tgVideo struct {
	FileID   string `json:"file_id"`
	Duration int    `json:"duration"`
	FileName string `json:"file_name"`
	MimeType string `json:"mime_type"`
	FileSize int    `json:"file_size"`
}

type tgSticker struct {
	FileID string `json:"file_id"`
	Emoji  string `json:"emoji"`
}

func splitTelegramMessage(text string, maxLen int) []string {
	if len(text) <= maxLen {
		return []string{text}
	}

	var chunks []string
	for len(text) > 0 {
		if len(text) <= maxLen {
			chunks = append(chunks, text)
			break
		}

		chunk := text[:maxLen]
		splitAt := -1

		if idx := strings.LastIndex(chunk, "\n\n"); idx > 0 {
			splitAt = idx
		}
		if splitAt < 0 {
			if idx := strings.LastIndex(chunk, "\n"); idx > 0 {
				splitAt = idx
			}
		}
		if splitAt < 0 {
			if idx := strings.LastIndex(chunk, " "); idx > 0 {
				splitAt = idx
			}
		}
		if splitAt < 0 {
			splitAt = maxLen
		}

		chunks = append(chunks, text[:splitAt])
		text = strings.TrimLeft(text[splitAt:], "\n ")
	}
	return chunks
}

func stripHTML(s string) string {
	re := regexp.MustCompile(`<[^>]+>`)
	s = re.ReplaceAllString(s, "")
	s = strings.ReplaceAll(s, "&lt;", "<")
	s = strings.ReplaceAll(s, "&gt;", ">")
	s = strings.ReplaceAll(s, "&amp;", "&")
	return s
}

func formatFileSize(size int) string {
	if size < 1024 {
		return fmt.Sprintf("%d B", size)
	} else if size < 1024*1024 {
		return fmt.Sprintf("%.1f KB", float64(size)/1024)
	}
	return fmt.Sprintf("%.1f MB", float64(size)/(1024*1024))
}

func isVideoExt(ext string) bool {
	switch ext {
	case ".mp4", ".mov", ".avi", ".mkv", ".webm":
		return true
	}
	return false
}

func isAudioExt(ext string) bool {
	switch ext {
	case ".mp3", ".m4a", ".flac", ".aac", ".wav":
		return true
	}
	return false
}

func isVoiceExt(ext string) bool {
	switch ext {
	case ".ogg", ".opus", ".oga":
		return true
	}
	return false
}

var (
	_ Connector    = (*TelegramPollConnector)(nil)
	_ PollReceiver = (*TelegramPollConnector)(nil)
	_ AuthPrompter = (*TelegramPollConnector)(nil)
)
