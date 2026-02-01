package hook

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseInput(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		wantPath string
		wantErr  bool
	}{
		{
			name:     "valid input",
			input:    `{"transcript_path":"/tmp/transcript.jsonl"}`,
			wantPath: "/tmp/transcript.jsonl",
		},
		{
			name:     "empty object",
			input:    `{}`,
			wantPath: "",
		},
		{
			name:    "invalid json",
			input:   `{invalid`,
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			input, err := ParseInput([]byte(tt.input))
			if tt.wantErr {
				if err == nil {
					t.Error("expected error, got nil")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if input.TranscriptPath != tt.wantPath {
				t.Errorf("got path %q, want %q", input.TranscriptPath, tt.wantPath)
			}
		})
	}
}

func TestExtractFromTranscript(t *testing.T) {
	// Create temp transcript file
	tmpDir := t.TempDir()
	transcriptPath := filepath.Join(tmpDir, "transcript.jsonl")

	// Write test transcript
	transcript := `{"type":"user","message":{"content":[{"type":"text","text":"Hello"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"Hi there!"}]}}
{"type":"user","message":{"content":[{"type":"text","text":"How are you?"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"I'm doing well, thanks!"}]}}
{"type":"assistant","message":{"content":[{"type":"text","text":"How can I help you today?"}]}}`

	if err := os.WriteFile(transcriptPath, []byte(transcript), 0600); err != nil {
		t.Fatalf("write transcript: %v", err)
	}

	text, err := ExtractFromTranscript(transcriptPath)
	if err != nil {
		t.Fatalf("ExtractFromTranscript: %v", err)
	}

	// Should get assistant messages after last user message
	if !strings.Contains(text, "I'm doing well") {
		t.Errorf("expected 'I'm doing well' in output, got %q", text)
	}
	if !strings.Contains(text, "How can I help you today") {
		t.Errorf("expected 'How can I help you today' in output, got %q", text)
	}
	// Should NOT contain the first assistant message
	if strings.Contains(text, "Hi there") {
		t.Errorf("should not contain first assistant message, got %q", text)
	}
}

func TestExtractFromTranscript_NoUserMessage(t *testing.T) {
	tmpDir := t.TempDir()
	transcriptPath := filepath.Join(tmpDir, "transcript.jsonl")

	transcript := `{"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}`
	if err := os.WriteFile(transcriptPath, []byte(transcript), 0600); err != nil {
		t.Fatalf("write transcript: %v", err)
	}

	text, err := ExtractFromTranscript(transcriptPath)
	if err != nil {
		t.Fatalf("ExtractFromTranscript: %v", err)
	}

	if text != "" {
		t.Errorf("expected empty string when no user message, got %q", text)
	}
}

func TestExtractFromTranscript_FileNotFound(t *testing.T) {
	_, err := ExtractFromTranscript("/nonexistent/path")
	if err == nil {
		t.Error("expected error for nonexistent file")
	}
}

func TestExtractResponseFromTmux(t *testing.T) {
	tests := []struct {
		name    string
		content string
		want    string
	}{
		{
			name: "simple response",
			content: `● Hello, I'm Claude!
❯`,
			want: "Hello, I'm Claude!",
		},
		{
			name: "multiline response",
			content: `● First line
  Second line
  Third line
❯`,
			want: "First line\nSecond line\nThird line",
		},
		{
			name: "skip status lines",
			content: `● Real response here
·✶ Thinking...
  More response
❯`,
			want: "Real response here\nMore response",
		},
		{
			name: "feedback prompt ignored",
			content: `● First response
❯
● How is Claude doing this session?
❯`,
			want: "First response",
		},
		{
			name: "separator ending",
			content: `● The answer is 42
───────────────────`,
			want: "The answer is 42",
		},
		{
			name:    "empty content",
			content: ``,
			want:    "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := extractResponseFromTmux(tt.content)
			if got != tt.want {
				t.Errorf("extractResponseFromTmux() = %q, want %q", got, tt.want)
			}
		})
	}
}
