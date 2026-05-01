package forge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type ChecksumVerifier struct {
	checksums map[string]string
}

func NewChecksumVerifier(checksums map[string]string) *ChecksumVerifier {
	copyChecksums := make(map[string]string, len(checksums))
	for name, checksum := range checksums {
		copyChecksums[name] = checksum
	}
	return &ChecksumVerifier{checksums: copyChecksums}
}

func (v *ChecksumVerifier) Verify(name string, data []byte) error {
	expected, ok := v.checksums[name]
	if !ok {
		return fmt.Errorf("no checksum for %q", name)
	}

	sum := sha256.Sum256(data)
	actual := hex.EncodeToString(sum[:])
	if actual != expected {
		return fmt.Errorf("checksum mismatch for %q", name)
	}

	return nil
}

type FileIntegrityMonitor struct {
	Manifest *Manifest
	Vars     map[string]string
	Verifier IntegrityVerifier
}

func (m *FileIntegrityMonitor) VerifyCritical() error {
	if m.Manifest == nil {
		return fmt.Errorf("manifest is required")
	}
	if m.Verifier == nil {
		return fmt.Errorf("verifier is required")
	}

	artifacts, err := EnumerateExtractedArtifacts(m.Manifest, m.Vars)
	if err != nil {
		return err
	}
	if _, _, err := VerifyExtractedArtifacts(artifacts, m.Verifier, func(artifact ExtractedArtifact) bool {
		return !artifact.Critical
	}); err != nil {
		return err
	}

	if len(m.Manifest.Hooks) == 0 {
		return nil
	}

	configDir := m.Vars["CLAUDE_CONFIG_DIR"]
	settingsPath := filepath.Join(configDir, "settings.json")
	data, err := os.ReadFile(settingsPath)
	if err != nil {
		return fmt.Errorf("read %q: %w", settingsPath, err)
	}

	var settings map[string]any
	if err := json.Unmarshal(data, &settings); err != nil {
		return fmt.Errorf("parse settings.json: %w", err)
	}

	for _, hook := range m.Manifest.Hooks {
		command, err := ExpandTemplate(hook.Command, m.Vars)
		if err != nil {
			return fmt.Errorf("expand hook command %q: %w", hook.Command, err)
		}
		if !settingsHookExists(settings, hook.Event, command, hook.Matcher) {
			return fmt.Errorf("missing hook registration for %s %q", hook.Event, command)
		}
	}

	return nil
}

func settingsHookExists(settings map[string]any, event string, command string, matcher string) bool {
	hooks, _ := settings["hooks"].(map[string]any)
	entries, _ := hooks[event].([]any)
	for _, group := range normalizeEventHooks(entries) {
		existingMatcher, _ := group["matcher"].(string)
		if matcher != "" && existingMatcher != matcher {
			continue
		}
		if groupHasCommand(group, command) {
			return true
		}
	}
	return false
}
