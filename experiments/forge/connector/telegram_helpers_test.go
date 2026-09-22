package connector

import (
	"strings"
	"testing"
)

func TestSplitTelegramMessage(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name     string
		text     string
		maxLen   int
		wantN    int
		checkAll func(t *testing.T, chunks []string)
	}{
		{
			name:   "short text returns single chunk",
			text:   "hello",
			maxLen: 100,
			wantN:  1,
		},
		{
			name:   "exact maxLen returns single chunk",
			text:   "abcde",
			maxLen: 5,
			wantN:  1,
		},
		{
			name:   "splits at blank line",
			text:   "first paragraph\n\nsecond paragraph",
			maxLen: 20,
			wantN:  2,
			checkAll: func(t *testing.T, chunks []string) {
				if chunks[0] != "first paragraph" {
					t.Errorf("chunk[0] = %q, want %q", chunks[0], "first paragraph")
				}
				if chunks[1] != "second paragraph" {
					t.Errorf("chunk[1] = %q, want %q", chunks[1], "second paragraph")
				}
			},
		},
		{
			name:   "splits at newline when no blank line",
			text:   "line one\nline two is here",
			maxLen: 15,
			wantN:  3,
			checkAll: func(t *testing.T, chunks []string) {
				if chunks[0] != "line one" {
					t.Errorf("chunk[0] = %q, want %q", chunks[0], "line one")
				}
			},
		},
		{
			name:   "splits at space when no newline",
			text:   "word1 word2 word3",
			maxLen: 10,
			wantN:  3,
			checkAll: func(t *testing.T, chunks []string) {
				if chunks[0] != "word1" {
					t.Errorf("chunk[0] = %q, want %q", chunks[0], "word1")
				}
			},
		},
		{
			name:   "hard cuts with no split point",
			text:   "abcdefghij",
			maxLen: 4,
			wantN:  3,
			checkAll: func(t *testing.T, chunks []string) {
				if chunks[0] != "abcd" {
					t.Errorf("chunk[0] = %q, want %q", chunks[0], "abcd")
				}
			},
		},
		{
			name:   "empty text returns single empty chunk",
			text:   "",
			maxLen: 100,
			wantN:  1,
		},
		{
			name:   "unicode multi-byte characters",
			text:   "日本語テスト文字列",
			maxLen: 15,
			wantN:  2,
			checkAll: func(t *testing.T, chunks []string) {
				for i, c := range chunks {
					if len(c) > 15 {
						t.Errorf("chunk[%d] len = %d, want <= 15", i, len(c))
					}
				}
			},
		},
		{
			name:   "long text splits into multiple chunks",
			text:   strings.Repeat("x", 5000),
			maxLen: 4096,
			wantN:  2,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			chunks := splitTelegramMessage(tt.text, tt.maxLen)
			if len(chunks) != tt.wantN {
				t.Fatalf("len(chunks) = %d, want %d; chunks = %q", len(chunks), tt.wantN, chunks)
			}
			for i, c := range chunks {
				if len(c) > tt.maxLen {
					t.Errorf("chunk[%d] len = %d exceeds maxLen %d", i, len(c), tt.maxLen)
				}
			}
			if tt.checkAll != nil {
				tt.checkAll(t, chunks)
			}
		})
	}
}

func TestStripHTML(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name  string
		input string
		want  string
	}{
		{
			name:  "strips bold tags",
			input: "<b>bold</b>",
			want:  "bold",
		},
		{
			name:  "strips italic tags",
			input: "<i>italic</i>",
			want:  "italic",
		},
		{
			name:  "decodes HTML entities",
			input: "&lt;test&amp;foo&gt;",
			want:  "<test&foo>",
		},
		{
			name:  "handles nested tags",
			input: "<b><i>nested</i></b>",
			want:  "nested",
		},
		{
			name:  "passes through plain text",
			input: "no tags here",
			want:  "no tags here",
		},
		{
			name:  "strips anchor tags",
			input: `<a href="http://example.com">link</a>`,
			want:  "link",
		},
		{
			name:  "empty string",
			input: "",
			want:  "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := stripHTML(tt.input)
			if got != tt.want {
				t.Errorf("stripHTML(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

func TestFormatFileSize(t *testing.T) {
	t.Parallel()

	tests := []struct {
		size int
		want string
	}{
		{0, "0 B"},
		{512, "512 B"},
		{1023, "1023 B"},
		{1024, "1.0 KB"},
		{1536, "1.5 KB"},
		{1048576, "1.0 MB"},
		{2621440, "2.5 MB"},
	}

	for _, tt := range tests {
		got := formatFileSize(tt.size)
		if got != tt.want {
			t.Errorf("formatFileSize(%d) = %q, want %q", tt.size, got, tt.want)
		}
	}
}

func TestIsVideoExt(t *testing.T) {
	t.Parallel()

	yes := []string{".mp4", ".mov", ".avi", ".mkv", ".webm"}
	no := []string{".mp3", ".jpg", ".txt", ".ogg", ""}

	for _, ext := range yes {
		if !isVideoExt(ext) {
			t.Errorf("isVideoExt(%q) = false, want true", ext)
		}
	}
	for _, ext := range no {
		if isVideoExt(ext) {
			t.Errorf("isVideoExt(%q) = true, want false", ext)
		}
	}
}

func TestIsAudioExt(t *testing.T) {
	t.Parallel()

	yes := []string{".mp3", ".m4a", ".flac", ".aac", ".wav"}
	no := []string{".mp4", ".jpg", ".txt", ".ogg", ""}

	for _, ext := range yes {
		if !isAudioExt(ext) {
			t.Errorf("isAudioExt(%q) = false, want true", ext)
		}
	}
	for _, ext := range no {
		if isAudioExt(ext) {
			t.Errorf("isAudioExt(%q) = true, want false", ext)
		}
	}
}

func TestIsVoiceExt(t *testing.T) {
	t.Parallel()

	yes := []string{".ogg", ".opus", ".oga"}
	no := []string{".mp3", ".mp4", ".wav", ".txt", ""}

	for _, ext := range yes {
		if !isVoiceExt(ext) {
			t.Errorf("isVoiceExt(%q) = false, want true", ext)
		}
	}
	for _, ext := range no {
		if isVoiceExt(ext) {
			t.Errorf("isVoiceExt(%q) = true, want false", ext)
		}
	}
}
