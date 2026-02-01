package telegram

import (
	"os"
	"path/filepath"
	"testing"
)

func TestParseMediaTags(t *testing.T) {
	tests := []struct {
		name         string
		input        string
		wantText     string
		wantTagCount int
		wantTags     []MediaTag
	}{
		{
			name:         "no tags",
			input:        "Hello, this is a plain message",
			wantText:     "Hello, this is a plain message",
			wantTagCount: 0,
		},
		{
			name:         "single image tag",
			input:        "Here is the screenshot [[image:/tmp/screenshot.png]]",
			wantText:     "Here is the screenshot",
			wantTagCount: 1,
			wantTags: []MediaTag{
				{Type: "image", Path: "/tmp/screenshot.png", Caption: ""},
			},
		},
		{
			name:         "single file tag",
			input:        "Report attached [[file:/tmp/report.pdf]]",
			wantText:     "Report attached",
			wantTagCount: 1,
			wantTags: []MediaTag{
				{Type: "file", Path: "/tmp/report.pdf", Caption: ""},
			},
		},
		{
			name:         "image with caption",
			input:        "Check this [[image:/tmp/chart.png|Sales Chart Q4]]",
			wantText:     "Check this",
			wantTagCount: 1,
			wantTags: []MediaTag{
				{Type: "image", Path: "/tmp/chart.png", Caption: "Sales Chart Q4"},
			},
		},
		{
			name:         "file with caption",
			input:        "[[file:/tmp/data.csv|Quarterly Data]]",
			wantText:     "",
			wantTagCount: 1,
			wantTags: []MediaTag{
				{Type: "file", Path: "/tmp/data.csv", Caption: "Quarterly Data"},
			},
		},
		{
			name:         "multiple tags",
			input:        "Here are the files: [[image:/tmp/a.png]] and [[file:/tmp/b.pdf]]",
			wantText:     "Here are the files: and",
			wantTagCount: 2,
			wantTags: []MediaTag{
				{Type: "image", Path: "/tmp/a.png", Caption: ""},
				{Type: "file", Path: "/tmp/b.pdf", Caption: ""},
			},
		},
		{
			name:         "tag at start",
			input:        "[[image:/tmp/first.png]] Here is the image",
			wantText:     "Here is the image",
			wantTagCount: 1,
			wantTags: []MediaTag{
				{Type: "image", Path: "/tmp/first.png", Caption: ""},
			},
		},
		{
			name:         "only tag no text",
			input:        "[[file:/tmp/only.zip]]",
			wantText:     "",
			wantTagCount: 1,
			wantTags: []MediaTag{
				{Type: "file", Path: "/tmp/only.zip", Caption: ""},
			},
		},
		{
			name:         "invalid tag type ignored",
			input:        "Hello [[video:/tmp/v.mp4]] world",
			wantText:     "Hello [[video:/tmp/v.mp4]] world",
			wantTagCount: 0,
		},
		{
			name:         "malformed tag kept as text",
			input:        "Hello [[image:no closing bracket",
			wantText:     "Hello [[image:no closing bracket",
			wantTagCount: 0,
		},
		{
			name:         "empty path ignored",
			input:        "Hello [[image:]] world",
			wantText:     "Hello [[image:]] world",
			wantTagCount: 0,
		},
		{
			name:         "whitespace handling",
			input:        "  [[image:/tmp/test.png]]  Extra text  ",
			wantText:     "Extra text",
			wantTagCount: 1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gotText, gotTags := ParseMediaTags(tt.input)

			if gotText != tt.wantText {
				t.Errorf("ParseMediaTags() text = %q, want %q", gotText, tt.wantText)
			}

			if len(gotTags) != tt.wantTagCount {
				t.Errorf("ParseMediaTags() tag count = %d, want %d", len(gotTags), tt.wantTagCount)
			}

			if tt.wantTags != nil {
				for i, want := range tt.wantTags {
					if i >= len(gotTags) {
						t.Errorf("missing tag %d", i)
						continue
					}
					got := gotTags[i]
					if got.Type != want.Type {
						t.Errorf("tag[%d].Type = %q, want %q", i, got.Type, want.Type)
					}
					if got.Path != want.Path {
						t.Errorf("tag[%d].Path = %q, want %q", i, got.Path, want.Path)
					}
					if got.Caption != want.Caption {
						t.Errorf("tag[%d].Caption = %q, want %q", i, got.Caption, want.Caption)
					}
				}
			}
		})
	}
}

func TestValidateMediaPath(t *testing.T) {
	// Create temp dir for testing
	tmpDir := t.TempDir()
	sessionsDir := filepath.Join(tmpDir, "sessions")
	os.MkdirAll(sessionsDir, 0755)

	// Create some test files
	validTmpFile := filepath.Join(os.TempDir(), "test_valid.png")
	os.WriteFile(validTmpFile, []byte("test"), 0644)
	defer os.Remove(validTmpFile)

	validSessionFile := filepath.Join(sessionsDir, "worker", "output.png")
	os.MkdirAll(filepath.Dir(validSessionFile), 0755)
	os.WriteFile(validSessionFile, []byte("test"), 0644)

	// Get current working directory
	cwd, _ := os.Getwd()
	validCwdFile := filepath.Join(cwd, "test_cwd_file.txt")
	os.WriteFile(validCwdFile, []byte("test"), 0644)
	defer os.Remove(validCwdFile)

	tests := []struct {
		name        string
		path        string
		sessionsDir string
		wantErr     bool
		errContains string
	}{
		{
			name:        "valid tmp file",
			path:        validTmpFile,
			sessionsDir: sessionsDir,
			wantErr:     false,
		},
		{
			name:        "valid sessions file",
			path:        validSessionFile,
			sessionsDir: sessionsDir,
			wantErr:     false,
		},
		{
			name:        "valid cwd file",
			path:        validCwdFile,
			sessionsDir: sessionsDir,
			wantErr:     false,
		},
		{
			name:        "blocked .pem extension",
			path:        "/tmp/secret.pem",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "blocked extension",
		},
		{
			name:        "blocked .key extension",
			path:        "/tmp/private.key",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "blocked extension",
		},
		{
			name:        "blocked .env extension",
			path:        "/tmp/.env",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "blocked extension",
		},
		{
			name:        "blocked credentials file",
			path:        "/tmp/credentials",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "blocked",
		},
		{
			name:        "path outside allowed dirs",
			path:        "/etc/passwd",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "not in allowed",
		},
		{
			name:        "path traversal attempt",
			path:        "/tmp/../etc/passwd",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "not in allowed",
		},
		{
			name:        "file does not exist",
			path:        "/tmp/nonexistent_file_12345.png",
			sessionsDir: sessionsDir,
			wantErr:     true,
			errContains: "does not exist",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := ValidateMediaPath(tt.path, tt.sessionsDir)
			if tt.wantErr {
				if err == nil {
					t.Errorf("ValidateMediaPath() expected error, got nil")
				} else if tt.errContains != "" && !contains(err.Error(), tt.errContains) {
					t.Errorf("ValidateMediaPath() error = %v, want containing %q", err, tt.errContains)
				}
			} else {
				if err != nil {
					t.Errorf("ValidateMediaPath() unexpected error = %v", err)
				}
			}
		})
	}
}

func TestValidateMediaSize(t *testing.T) {
	tmpDir := t.TempDir()

	// Create a small file
	smallFile := filepath.Join(tmpDir, "small.txt")
	os.WriteFile(smallFile, []byte("small content"), 0644)

	// Create a file that exceeds 20MB limit
	largeFile := filepath.Join(tmpDir, "large.bin")
	f, _ := os.Create(largeFile)
	// Write 21MB of zeros
	f.Truncate(21 * 1024 * 1024)
	f.Close()

	tests := []struct {
		name    string
		path    string
		wantErr bool
	}{
		{
			name:    "small file",
			path:    smallFile,
			wantErr: false,
		},
		{
			name:    "large file exceeds limit",
			path:    largeFile,
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := ValidateMediaSize(tt.path)
			if tt.wantErr && err == nil {
				t.Errorf("ValidateMediaSize() expected error for large file")
			}
			if !tt.wantErr && err != nil {
				t.Errorf("ValidateMediaSize() unexpected error = %v", err)
			}
		})
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > 0 && (s[:len(substr)] == substr || contains(s[1:], substr)))
}
