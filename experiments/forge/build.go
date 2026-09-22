package forge

import "github.com/beastoin/claudecode-telegram/forge/packaging"

type BuildConfig = packaging.BuildConfig
type CredsEncryptor = packaging.CredsEncryptor
type StubCredsEncryptor = packaging.StubCredsEncryptor
type EmbedLayout = packaging.EmbedLayout
type BuildResult = packaging.BuildResult
type AgeCredsEncryptor = packaging.AgeCredsEncryptor
type AgeBundleDecryptor = packaging.AgeBundleDecryptor
type CredsBundlePayload = packaging.CredsBundlePayload

var generatedWorkerMainGo = packaging.GeneratedWorkerMainGo
var workerForgeSkillMD = packaging.WorkerForgeSkillMD

var CollectManifestFiles = packaging.CollectManifestFiles
var GenerateChecksumsJSON = packaging.GenerateChecksumsJSON
var Build = packaging.Build
var WriteEmbedLayout = packaging.WriteEmbedLayout
var InstallSkill = packaging.InstallSkill
var ParseAgeRecipients = packaging.ParseAgeRecipients
var ParseAgeIdentities = packaging.ParseAgeIdentities
var ParseCredsBundle = packaging.ParseCredsBundle

func expectedBinaryName(workerName, goos, goarch string) string {
	return packaging.ExpectedBinaryName(workerName, goos, goarch)
}

func buildArtifactKey(source string, encrypted bool) string {
	return packaging.BuildArtifactKey(source, encrypted)
}

func canonicalSource(source string) string {
	return packaging.CanonicalSource(source)
}
