package forge

import "github.com/beastoin/claudecode-telegram/forge/verify"

type VerifyOptions struct {
	Resolve   ResolveOptions
	Checksums map[string]string
}

type VerifyReport = verify.VerifyReport
type VerifyResultJSON = verify.VerifyResultJSON
type VerifySummaryJSON = verify.VerifySummaryJSON
type VerifyFileJSON = verify.VerifyFileJSON
type ExtractedArtifact = verify.ExtractedArtifact
type IntegrityVerifier = verify.IntegrityVerifier
type ChecksumVerifier = verify.ChecksumVerifier

var FormatVerifyJSON = verify.FormatVerifyJSON
var NewChecksumVerifier = verify.NewChecksumVerifier
var VerifyExtractedArtifacts = verify.VerifyExtractedArtifacts

func EnumerateExtractedArtifacts(manifest *Manifest, vars map[string]string) ([]ExtractedArtifact, error) {
	return verify.EnumerateExtractedArtifacts(manifest, vars, ExpandTemplate, buildArtifactKey)
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
