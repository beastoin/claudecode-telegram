package verify

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/beastoin/claudecode-telegram/forge/manifest"
)

type Manifest = manifest.Manifest

type IntegrityVerifier interface {
	Verify(name string, data []byte) error
}

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

type VerifyReport struct {
	Worker   string   `json:"worker"`
	Version  string   `json:"version"`
	Verified []string `json:"verified"`
	Skipped  []string `json:"skipped"`
}

type VerifyResultJSON struct {
	Command string            `json:"command"`
	Worker  string            `json:"worker"`
	Version string            `json:"version"`
	Status  string            `json:"status"`
	Summary VerifySummaryJSON `json:"summary"`
	Files   []VerifyFileJSON  `json:"files"`
}

type VerifySummaryJSON struct {
	Verified int `json:"verified"`
	Skipped  int `json:"skipped"`
}

type VerifyFileJSON struct {
	Path   string `json:"path"`
	Status string `json:"status"`
}

func FormatVerifyJSON(report VerifyReport) string {
	result := VerifyResultJSON{
		Command: "verify",
		Worker:  report.Worker,
		Version: report.Version,
		Status:  "pass",
		Summary: VerifySummaryJSON{
			Verified: len(report.Verified),
			Skipped:  len(report.Skipped),
		},
	}
	for _, v := range report.Verified {
		result.Files = append(result.Files, VerifyFileJSON{Path: v, Status: "pass"})
	}
	for _, s := range report.Skipped {
		result.Files = append(result.Files, VerifyFileJSON{Path: s, Status: "skip"})
	}
	data, _ := json.MarshalIndent(result, "", "  ")
	return string(data) + "\n"
}

type ExtractedArtifact struct {
	ContentKey       string
	Dest             string
	SkipVerification bool
	Critical         bool
}

type ExpandFunc func(tmpl string, vars map[string]string) (string, error)

func EnumerateExtractedArtifacts(m *Manifest, vars map[string]string, expand ExpandFunc, artifactKey func(source string, encrypted bool) string) ([]ExtractedArtifact, error) {
	artifacts := make([]ExtractedArtifact, 0, len(m.Files)+len(m.Hooks))

	for _, file := range m.Files {
		contentKey := file.ContentKey
		if contentKey == "" {
			contentKey = artifactKey(file.Source, file.Encrypted)
		}

		dest, err := expand(file.Dest, vars)
		if err != nil {
			return nil, fmt.Errorf("expand destination %q: %w", file.Dest, err)
		}

		info, statErr := os.Stat(dest)
		if statErr == nil && info.IsDir() {
			prefix := strings.TrimSuffix(contentKey, "/") + "/"
			walkErr := filepath.Walk(dest, func(path string, fi os.FileInfo, wErr error) error {
				if wErr != nil || fi.IsDir() {
					return wErr
				}
				rel, _ := filepath.Rel(dest, path)
				artifacts = append(artifacts, ExtractedArtifact{
					ContentKey:       prefix + rel,
					Dest:             path,
					SkipVerification: file.Integrity == "skip" || file.Merge,
					Critical:         !file.Merge && file.Integrity != "skip",
				})
				return nil
			})
			if walkErr != nil {
				return nil, fmt.Errorf("walk directory %q: %w", dest, walkErr)
			}
			continue
		}

		artifacts = append(artifacts, ExtractedArtifact{
			ContentKey:       contentKey,
			Dest:             dest,
			SkipVerification: file.Integrity == "skip" || file.Merge,
			Critical:         !file.Merge && file.Integrity != "skip",
		})
	}

	configDir := vars["CLAUDE_CONFIG_DIR"]
	for _, hook := range m.Hooks {
		if hook.Source == "" {
			continue
		}

		dest := filepath.Join(configDir, filepath.FromSlash(hook.Source))
		if hook.Command != "" {
			expandedDest, err := expand(hook.Command, vars)
			if err != nil {
				return nil, fmt.Errorf("expand hook command %q: %w", hook.Command, err)
			}
			dest = expandedDest
		}

		artifacts = append(artifacts, ExtractedArtifact{
			ContentKey: artifactKey(hook.Source, false),
			Dest:       dest,
			Critical:   true,
		})
	}

	return artifacts, nil
}

func VerifyExtractedArtifacts(artifacts []ExtractedArtifact, verifier IntegrityVerifier, skip func(artifact ExtractedArtifact) bool) ([]string, []string, error) {
	verified := make([]string, 0, len(artifacts))
	skipped := []string{}

	for _, artifact := range artifacts {
		if skip != nil && skip(artifact) {
			skipped = append(skipped, artifact.ContentKey)
			continue
		}

		data, err := os.ReadFile(artifact.Dest)
		if err != nil {
			return nil, nil, fmt.Errorf("read %q: %w", artifact.Dest, err)
		}
		if err := verifier.Verify(artifact.ContentKey, data); err != nil {
			return nil, nil, err
		}

		verified = append(verified, artifact.ContentKey)
	}

	return verified, skipped, nil
}
