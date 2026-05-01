package forge

import (
	"fmt"
	"os"
	"path/filepath"
)

type VerifyOptions struct {
	Resolve   ResolveOptions
	Checksums map[string]string
}

type VerifyReport struct {
	Worker   string   `json:"worker"`
	Version  string   `json:"version"`
	Verified []string `json:"verified"`
	Skipped  []string `json:"skipped"`
}

func RunVerifyMode(manifest *Manifest, opts VerifyOptions) (*VerifyReport, error) {
	resolved, err := ResolveVars(manifest, opts.Resolve)
	if err != nil {
		return nil, err
	}

	verifier := NewChecksumVerifier(opts.Checksums)
	report := &VerifyReport{
		Worker:   manifest.Name,
		Version:  manifest.Version,
		Verified: []string{},
		Skipped:  []string{},
	}

	artifacts, err := EnumerateExtractedArtifacts(manifest, resolved)
	if err != nil {
		return nil, err
	}
	report.Verified, report.Skipped, err = VerifyExtractedArtifacts(artifacts, verifier, func(artifact ExtractedArtifact) bool {
		return artifact.SkipVerification
	})
	if err != nil {
		return nil, err
	}

	return report, nil
}

type ExtractedArtifact struct {
	ContentKey       string
	Dest             string
	SkipVerification bool
	Critical         bool
}

func EnumerateExtractedArtifacts(manifest *Manifest, vars map[string]string) ([]ExtractedArtifact, error) {
	artifacts := make([]ExtractedArtifact, 0, len(manifest.Files)+len(manifest.Hooks))

	for _, file := range manifest.Files {
		contentKey := file.ContentKey
		if contentKey == "" {
			contentKey = buildArtifactKey(file.Source, file.Encrypted)
		}

		dest, err := ExpandTemplate(file.Dest, vars)
		if err != nil {
			return nil, fmt.Errorf("expand destination %q: %w", file.Dest, err)
		}

		artifacts = append(artifacts, ExtractedArtifact{
			ContentKey:       contentKey,
			Dest:             dest,
			SkipVerification: file.Integrity == "skip",
			Critical:         !file.Merge && file.Integrity != "skip",
		})
	}

	configDir := vars["CLAUDE_CONFIG_DIR"]
	for _, hook := range manifest.Hooks {
		if hook.Source == "" {
			continue
		}

		dest := filepath.Join(configDir, filepath.FromSlash(hook.Source))
		if hook.Command != "" {
			expandedDest, err := ExpandTemplate(hook.Command, vars)
			if err != nil {
				return nil, fmt.Errorf("expand hook command %q: %w", hook.Command, err)
			}
			dest = expandedDest
		}

		artifacts = append(artifacts, ExtractedArtifact{
			ContentKey: buildArtifactKey(hook.Source, false),
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
