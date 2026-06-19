package forge

import (
	"fmt"

	"gopkg.in/yaml.v3"
)

type Manifest struct {
	Name      string                 `yaml:"name"`
	Version   string                 `yaml:"version"`
	Vars      map[string]VarSpec     `yaml:"vars"`
	Dirs      []string               `yaml:"dirs"`
	Files     []FileSpec             `yaml:"files"`
	Tools     []ToolSpec             `yaml:"tools"`
	Readiness []ReadinessCheck       `yaml:"readiness"`
	Hooks     []HookSpec             `yaml:"hooks"`
	Extra     map[string]interface{} `yaml:",inline"`
}

type VarSpec struct {
	Source      string `yaml:"source"`
	Default     string `yaml:"default"`
	Required    bool   `yaml:"required"`
	Description string `yaml:"description"`
}

type FileSpec struct {
	Source     string `yaml:"source"`
	Dest       string `yaml:"dest"`
	Encrypted  bool   `yaml:"encrypted"`
	Merge      bool   `yaml:"merge"`
	Integrity  string `yaml:"integrity"`
	Checksum   string `yaml:"checksum"`
	Overwrite  string `yaml:"overwrite"`
	ContentKey string `yaml:"-"`
}

type ToolSpec struct {
	Name     string            `yaml:"name"`
	Check    string            `yaml:"check"`
	Install  map[string]string `yaml:"install"`
	Required bool              `yaml:"required"`
}

type ReadinessCheck struct {
	Name     string `yaml:"name"`
	Check    string `yaml:"check"`
	Expect   string `yaml:"expect"`
	Fix      string `yaml:"fix"`
	Required bool   `yaml:"required"`
}

type HookSpec struct {
	Event     string `yaml:"event"`
	Matcher   string `yaml:"matcher"`
	Command   string `yaml:"command"`
	Source    string `yaml:"source"`
	Generated bool   `yaml:"-"`
}

func ParseManifest(data []byte) (*Manifest, error) {
	var manifest Manifest
	if err := yaml.Unmarshal(data, &manifest); err != nil {
		return nil, err
	}
	if manifest.Vars == nil {
		manifest.Vars = map[string]VarSpec{}
	}
	return &manifest, nil
}

func ValidateManifest(manifest *Manifest) error {
	if manifest == nil {
		return fmt.Errorf("manifest is nil")
	}
	if manifest.Name == "" {
		return fmt.Errorf("manifest name is required")
	}
	if manifest.Version == "" {
		return fmt.Errorf("manifest version is required")
	}

	validSources := map[string]struct{}{
		"env":     {},
		"flag":    {},
		"creds":   {},
		"default": {},
	}

	for name, spec := range manifest.Vars {
		if _, ok := validSources[spec.Source]; !ok {
			return fmt.Errorf("var %q uses unsupported source %q", name, spec.Source)
		}
	}

	return nil
}
