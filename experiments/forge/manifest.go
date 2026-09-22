package forge

import "github.com/beastoin/claudecode-telegram/forge/manifest"

type Manifest = manifest.Manifest
type VarSpec = manifest.VarSpec
type FileSpec = manifest.FileSpec
type ToolSpec = manifest.ToolSpec
type ReadinessCheck = manifest.ReadinessCheck
type HookSpec = manifest.HookSpec

func ParseManifest(data []byte) (*Manifest, error) {
	return manifest.Parse(data)
}

func ValidateManifest(m *Manifest) error {
	return manifest.Validate(m)
}
