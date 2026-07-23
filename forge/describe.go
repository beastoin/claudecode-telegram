package forge

import "encoding/json"

type CommandSchema struct {
	Description string            `json:"description"`
	Flags       map[string]string `json:"flags,omitempty"`
	OutputJSON  bool              `json:"output_json"`
	DryRun      bool              `json:"dry_run"`
}

var commandSchemas = map[string]CommandSchema{
	"run": {
		Description: "Extract files, verify integrity, install hooks, launch worker with watchdog",
		Flags: map[string]string{
			"--identity":        "Age decryption key (saved to state, auto-loaded on next run)",
			"--bridge-url":      "Bridge URL (required for bridge connector)",
			"--connector":       "Connector type (telegram, bridge, etc.)",
			"--connector-opt":   "Connector option (key=val, repeatable)",
			"--name-override":   "Override worker name from manifest",
			"--force-extract":   "Overwrite conflicting files (default: skip-conflicts)",
			"--session-prefix":  "Tmux session prefix (default from manifest/bridge)",
			"--launch-command":  "Override claude launch command",
		},
		OutputJSON: false,
		DryRun:     false,
	},
	"check": {
		Description: "Run readiness checks (tools, auth, bridge connectivity)",
		Flags: map[string]string{
			"--output-json":  "Output structured JSON to stdout",
		},
		OutputJSON: true,
		DryRun:     false,
	},
	"verify": {
		Description: "Verify integrity of extracted files against embedded checksums",
		Flags: map[string]string{
			"--output-json":  "Output structured JSON to stdout",
		},
		OutputJSON: true,
		DryRun:     false,
	},
	"onboard": {
		Description: "Extract files and set up credentials for first-time use",
		Flags: map[string]string{
			"--identity":        "Age decryption key (saved to state for future use)",
			"--bridge-url":      "Bridge URL (for var resolution)",
			"--dry-run":         "Show what would be extracted without writing",
		},
		OutputJSON: false,
		DryRun:     true,
	},
	"auth": {
		Description: "Authenticate Claude Code via OAuth (sends URL to connector, waits for code)",
		Flags: map[string]string{
			"--bridge-url":    "Bridge URL for worker communication",
			"--connector":     "Connector type (telegram, telegram-poll)",
			"--connector-opt": "Connector option (key=val, repeatable)",
		},
		OutputJSON: false,
		DryRun:     false,
	},
	"health": {
		Description: "Check if worker tmux session is running",
		Flags:       map[string]string{},
		OutputJSON:  false,
		DryRun:      false,
	},
	"stop": {
		Description: "Stop worker tmux session",
		Flags:       map[string]string{},
		OutputJSON:  false,
		DryRun:      false,
	},
	"describe": {
		Description: "Show command schema as JSON",
		Flags:       map[string]string{},
		OutputJSON:  true,
		DryRun:      false,
	},
	"version": {
		Description: "Print worker name and version",
		Flags:       map[string]string{},
		OutputJSON:  false,
		DryRun:      false,
	},
}

func FormatDescribe(manifest *Manifest, target string) string {
	if target != "" {
		schema, ok := commandSchemas[target]
		if !ok {
			result := map[string]string{"error": "unknown command: " + target}
			data, _ := json.MarshalIndent(result, "", "  ")
			return string(data) + "\n"
		}
		result := map[string]interface{}{
			"command":     target,
			"description": schema.Description,
			"flags":       schema.Flags,
			"output_json": schema.OutputJSON,
			"dry_run":     schema.DryRun,
		}
		data, _ := json.MarshalIndent(result, "", "  ")
		return string(data) + "\n"
	}

	result := map[string]interface{}{
		"worker":   manifest.Name,
		"version":  manifest.Version,
		"commands": commandSchemas,
		"env_vars": map[string]string{
			"FORGE_BRIDGE_URL":  "Override --bridge-url (usually not needed — manifest has default)",
			"FORGE_IDENTITY":    "Override --identity (usually not needed — state file remembers)",
			"FORGE_CONNECTOR":   "Override --connector",
			"FORGE_OUTPUT_JSON": "Set to 1 to enable JSON output",
		},
	}
	data, _ := json.MarshalIndent(result, "", "  ")
	return string(data) + "\n"
}
