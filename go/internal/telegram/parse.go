package telegram

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// MaxMediaSize is the maximum file size allowed for media uploads (20MB)
const MaxMediaSize = 20 * 1024 * 1024

// MediaTag represents a parsed media tag from response text.
type MediaTag struct {
	Type    string // "image" or "file"
	Path    string
	Caption string
}

// mediaTagPattern matches [[image:/path|caption]] or [[file:/path|caption]]
var mediaTagPattern = regexp.MustCompile(`\[\[(image|file):([^\]|]+)(?:\|([^\]]*))?\]\]`)

// blockedExtensions are file extensions that should never be sent
var blockedExtensions = map[string]bool{
	".pem":      true,
	".key":      true,
	".env":      true,
	".p12":      true,
	".pfx":      true,
	".jks":      true,
	".keystore": true,
}

// blockedFilenames are filenames that should never be sent
var blockedFilenames = map[string]bool{
	"credentials":      true,
	"credentials.json": true,
	"secrets":          true,
	"secrets.json":     true,
	"id_rsa":           true,
	"id_dsa":           true,
	"id_ed25519":       true,
	"id_ecdsa":         true,
}

// ParseMediaTags extracts [[image:...]] and [[file:...]] tags from text.
// Returns the cleaned text and list of media tags.
func ParseMediaTags(text string) (string, []MediaTag) {
	var tags []MediaTag

	matches := mediaTagPattern.FindAllStringSubmatchIndex(text, -1)
	if len(matches) == 0 {
		return text, nil
	}

	// Process matches in reverse order to preserve indices when removing
	cleanedText := text
	for i := len(matches) - 1; i >= 0; i-- {
		match := matches[i]
		fullStart, fullEnd := match[0], match[1]

		// Extract groups
		tagType := text[match[2]:match[3]]
		path := text[match[4]:match[5]]

		var caption string
		if match[6] >= 0 && match[7] >= 0 {
			caption = text[match[6]:match[7]]
		}

		// Skip tags with empty paths
		if strings.TrimSpace(path) == "" {
			continue
		}

		// Add tag (in reverse order, will reverse later)
		tags = append(tags, MediaTag{
			Type:    tagType,
			Path:    path,
			Caption: caption,
		})

		// Remove the tag from text
		cleanedText = cleanedText[:fullStart] + cleanedText[fullEnd:]
	}

	// Reverse tags to maintain original order
	for i, j := 0, len(tags)-1; i < j; i, j = i+1, j-1 {
		tags[i], tags[j] = tags[j], tags[i]
	}

	// Clean up whitespace
	cleanedText = strings.TrimSpace(cleanedText)
	// Replace multiple spaces with single space
	cleanedText = regexp.MustCompile(`\s+`).ReplaceAllString(cleanedText, " ")
	cleanedText = strings.TrimSpace(cleanedText)

	return cleanedText, tags
}

// ValidateMediaPath checks if a file path is allowed to be sent.
// Only allows files from: /tmp, sessions directory, or current working directory.
// Blocks sensitive file extensions and filenames.
func ValidateMediaPath(path, sessionsDir string) error {
	// Clean and resolve the path
	absPath, err := filepath.Abs(path)
	if err != nil {
		return fmt.Errorf("invalid path: %w", err)
	}
	absPath = filepath.Clean(absPath)

	// Check for blocked extensions
	ext := strings.ToLower(filepath.Ext(absPath))
	if blockedExtensions[ext] {
		return fmt.Errorf("blocked extension: %s", ext)
	}

	// Check for blocked filenames
	filename := strings.ToLower(filepath.Base(absPath))
	if blockedFilenames[filename] {
		return fmt.Errorf("blocked filename: %s", filename)
	}

	// Also check if filename starts with . (hidden files that might be sensitive)
	if filename == ".env" || strings.HasPrefix(filename, ".env.") {
		return fmt.Errorf("blocked extension: .env")
	}

	// Check if file exists
	if _, err := os.Stat(absPath); os.IsNotExist(err) {
		return fmt.Errorf("file does not exist: %s", path)
	}

	// Check if in allowed directories
	tmpDir := os.TempDir()
	cwd, _ := os.Getwd()

	// Normalize directories
	tmpDir = filepath.Clean(tmpDir)
	cwd = filepath.Clean(cwd)
	sessionsDir = filepath.Clean(sessionsDir)

	inTmp := strings.HasPrefix(absPath, tmpDir+string(filepath.Separator)) || absPath == tmpDir
	inSessions := sessionsDir != "" && (strings.HasPrefix(absPath, sessionsDir+string(filepath.Separator)) || absPath == sessionsDir)
	inCwd := strings.HasPrefix(absPath, cwd+string(filepath.Separator)) || absPath == cwd

	if !inTmp && !inSessions && !inCwd {
		return fmt.Errorf("file not in allowed directories (tmp, sessions, cwd): %s", path)
	}

	return nil
}

// ValidateMediaSize checks if a file is within the size limit.
func ValidateMediaSize(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("cannot stat file: %w", err)
	}

	if info.Size() > MaxMediaSize {
		return fmt.Errorf("file too large: %d bytes (max %d)", info.Size(), MaxMediaSize)
	}

	return nil
}
