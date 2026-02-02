// Package hook handles Claude Code stop hook processing.
package hook

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
)

// Input represents the JSON input from Claude's stop hook.
type Input struct {
	TranscriptPath string `json:"transcript_path"`
}

// TranscriptMessage represents a message in the transcript.
type TranscriptMessage struct {
	Type    string `json:"type"`
	Message struct {
		Content []ContentBlock `json:"content"`
	} `json:"message"`
}

// ContentBlock represents a content block in a message.
type ContentBlock struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// ParseInput parses the JSON input from stdin.
func ParseInput(data []byte) (*Input, error) {
	var input Input
	if err := json.Unmarshal(data, &input); err != nil {
		return nil, fmt.Errorf("parse hook input: %w", err)
	}
	return &input, nil
}

// ExtractFromTranscript extracts assistant text from a transcript file.
// Returns the concatenated text from all assistant messages after the last user message.
func ExtractFromTranscript(transcriptPath string) (string, error) {
	data, err := os.ReadFile(transcriptPath)
	if err != nil {
		return "", fmt.Errorf("read transcript: %w", err)
	}

	lines := strings.Split(string(data), "\n")

	// Find the last user message line index
	lastUserIdx := -1
	for i, line := range lines {
		if strings.Contains(line, `"type":"user"`) {
			lastUserIdx = i
		}
	}

	if lastUserIdx == -1 {
		return "", nil
	}

	// Extract text from assistant messages after the last user message
	var texts []string
	for i := lastUserIdx; i < len(lines); i++ {
		line := lines[i]
		if !strings.Contains(line, `"type":"assistant"`) {
			continue
		}

		var msg TranscriptMessage
		if err := json.Unmarshal([]byte(line), &msg); err != nil {
			continue
		}

		for _, block := range msg.Message.Content {
			if block.Type == "text" && block.Text != "" {
				texts = append(texts, block.Text)
			}
		}
	}

	return strings.Join(texts, "\n\n"), nil
}

// TmuxFallback captures text from the tmux pane as a fallback.
// Returns the extracted response text.
func TmuxFallback(sessionName string) (string, error) {
	// Capture last 500 lines from the pane
	cmd := exec.Command("tmux", "capture-pane", "-t", sessionName, "-p", "-S", "-500")
	output, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("tmux capture-pane: %w", err)
	}

	return extractResponseFromTmux(string(output)), nil
}

// extractResponseFromTmux parses tmux pane content to extract Claude's response.
// Looks for text between ● (response start) and ❯ or ─── (prompt/separator).
func extractResponseFromTmux(content string) string {
	lines := strings.Split(content, "\n")

	var (
		inResponse   bool
		response     strings.Builder
		lastResponse string
	)

	bulletRE := regexp.MustCompile(`^\s*● `)
	promptRE := regexp.MustCompile(`^\s*❯`)
	separatorRE := regexp.MustCompile(`^\s*───`)
	skipRE := regexp.MustCompile(`^[·✶✻⏵⎿]|stop hook|Whirring|Herding|Mulling|Recombobulating|Cooked for|Saut|^[a-z]+:$|Tip:`)
	feedbackRE := regexp.MustCompile(`How is Claude doing this session`)

	for _, line := range lines {
		if bulletRE.MatchString(line) {
			inResponse = true
			response.Reset()
			// Remove the bullet prefix
			line = bulletRE.ReplaceAllString(line, "")
			response.WriteString(line)
			continue
		}

		if promptRE.MatchString(line) || separatorRE.MatchString(line) {
			if inResponse && response.Len() > 0 {
				text := response.String()
				if !feedbackRE.MatchString(text) {
					lastResponse = text
				}
			}
			inResponse = false
			response.Reset()
			continue
		}

		if inResponse {
			// Skip status lines
			if skipRE.MatchString(line) {
				continue
			}
			// Remove leading indent (1-2 spaces)
			line = strings.TrimPrefix(line, "  ")
			line = strings.TrimPrefix(line, " ")
			if response.Len() > 0 {
				response.WriteString("\n")
			}
			response.WriteString(line)
		}
	}

	// Check final buffer
	if inResponse && response.Len() > 0 {
		text := response.String()
		if !feedbackRE.MatchString(text) {
			return text
		}
	}

	return lastResponse
}

// FallbackWarning is appended when using tmux fallback.
const FallbackWarning = "\n\n⚠️ May be incomplete. Retry if needed."

// GetTmuxSessionName gets the current tmux session name.
func GetTmuxSessionName() string {
	cmd := exec.Command("tmux", "display-message", "-p", "#{session_name}")
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(output))
}

// GetTmuxEnv reads an environment variable from the tmux session.
func GetTmuxEnv(sessionName, key string) string {
	if sessionName == "" {
		return ""
	}
	cmd := exec.Command("tmux", "show-environment", "-t", sessionName, key)
	output, err := cmd.Output()
	if err != nil {
		return ""
	}
	// Output is "KEY=value", extract value
	line := strings.TrimSpace(string(output))
	parts := strings.SplitN(line, "=", 2)
	if len(parts) == 2 {
		return parts[1]
	}
	return ""
}
