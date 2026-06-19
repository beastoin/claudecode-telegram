package forge

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type ClaudeCodeDriver struct{}

func (d *ClaudeCodeDriver) ID() string { return "claude-code" }

func (d *ClaudeCodeDriver) Capabilities() EngineCapabilities {
	return EngineCapabilities{
		SupportsHooks:       true,
		SupportsResume:      true,
		SupportsPermissions: true,
		HookEvents:          []string{"Stop", "SessionStart", "PreToolUse", "PostToolUse"},
		InstructionFiles:    []string{"CLAUDE.md", "AGENTS.md"},
		ConfigFormat:        "settings.json",
	}
}

func (d *ClaudeCodeDriver) Prepare(ctx context.Context, req PrepareRequest) (*PreparedEngine, error) {
	configDir := req.Vars["CLAUDE_CONFIG_DIR"]
	if configDir == "" {
		return nil, fmt.Errorf("CLAUDE_CONFIG_DIR is required")
	}

	prepared := &PreparedEngine{
		Env: map[string]string{},
	}

	if err := d.installHookScripts(req, configDir); err != nil {
		return nil, fmt.Errorf("install hook scripts: %w", err)
	}

	if err := d.generateEmitHooks(req.Manifest, req.Vars, configDir); err != nil {
		return nil, fmt.Errorf("generate emit hooks: %w", err)
	}

	if err := d.patchSettings(req.Manifest, req.Vars, configDir); err != nil {
		return nil, fmt.Errorf("patch settings: %w", err)
	}

	if err := d.writeOnboardingMarker(configDir); err != nil {
		return nil, fmt.Errorf("onboarding marker: %w", err)
	}

	apiKey := req.Vars["ANTHROPIC_API_KEY"]
	if apiKey != "" {
		helperPath := filepath.Join(configDir, "hooks", "api-key-helper.sh")
		if err := d.writeAPIKeyHelper(helperPath); err != nil {
			return nil, fmt.Errorf("api key helper: %w", err)
		}

		credsFile := filepath.Join(configDir, ".credentials.json")
		if _, err := os.Stat(credsFile); err != nil {
			prepared.Env["FORGE_API_KEY_HELPER"] = helperPath
		}
	}

	prepared.Env["FORGE_WORKER_NAME"] = req.Manifest.Name

	return prepared, nil
}

func (d *ClaudeCodeDriver) StartSpec(ctx context.Context, prepared *PreparedEngine) (*StartSpec, error) {
	args := []string{"claude"}

	if helperPath := prepared.Env["FORGE_API_KEY_HELPER"]; helperPath != "" {
		settingsJSON, err := json.Marshal(map[string]string{
			"apiKeyHelper": helperPath,
		})
		if err == nil {
			args = append(args, "--settings", string(settingsJSON))
		}
	}

	if os.Geteuid() == 0 {
		args = append(args, "--permission-mode", "dontAsk")
	} else {
		args = append(args, "--dangerously-skip-permissions")
	}

	return &StartSpec{
		Command: args,
		Env:     prepared.Env,
	}, nil
}

func (d *ClaudeCodeDriver) installHookScripts(req PrepareRequest, configDir string) error {
	for _, hook := range req.Manifest.Hooks {
		if hook.Source == "" {
			continue
		}

		if req.Source == nil {
			return fmt.Errorf("hook source %q declared but no embedded source configured", hook.Source)
		}

		data, err := req.Source.ReadFile(hook.Source)
		if err != nil {
			data, err = req.Source.ReadFile(canonicalSource(hook.Source))
		}
		if err != nil {
			data, err = req.Source.ReadFile(buildArtifactKey(hook.Source, false))
		}
		if err != nil {
			return fmt.Errorf("read hook source %q: %w", hook.Source, err)
		}

		dest, err := ExpandTemplate(hook.Command, req.Vars)
		if err != nil {
			return fmt.Errorf("expand hook command %q: %w", hook.Command, err)
		}

		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return fmt.Errorf("create hook dir: %w", err)
		}
		if err := os.WriteFile(dest, data, 0o755); err != nil {
			return fmt.Errorf("write hook %q: %w", dest, err)
		}
	}

	return nil
}

func (d *ClaudeCodeDriver) generateEmitHooks(manifest *Manifest, vars map[string]string, configDir string) error {
	workerName := manifest.Name

	for i, hook := range manifest.Hooks {
		if hook.Source != "" {
			continue
		}

		dest, err := ExpandTemplate(hook.Command, vars)
		if err != nil {
			return fmt.Errorf("expand hook command %q: %w", hook.Command, err)
		}

		script := GenerateEmitScript(workerName)
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return fmt.Errorf("create hook dir: %w", err)
		}
		if err := os.WriteFile(dest, []byte(script), 0o755); err != nil {
			return fmt.Errorf("write emit hook %q: %w", dest, err)
		}

		manifest.Hooks[i].Generated = true
	}

	return nil
}

func GenerateEmitScript(workerName string) string {
	safeName := strings.NewReplacer("/", "-", " ", "-", "\\", "-").Replace(workerName)
	return fmt.Sprintf(`#!/bin/bash
set -euo pipefail
SOCKET="/tmp/forge-%s.sock"
[ ! -S "$SOCKET" ] && exit 0
BODY=$(cat)
[ -z "$BODY" ] && exit 0
# JSON-encode body: escape backslashes, quotes, control chars
ESCAPED=$(printf '%%s' "$BODY" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\r' '\a' | sed 's/\a/\\r/g' | tr '\n' '\a' | sed 's/\a/\\n/g')
curl -sf --unix-socket "$SOCKET" \
  -X POST http://forge/response \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"$ESCAPED\"}" >/dev/null 2>&1 || true
`, safeName)
}

func (d *ClaudeCodeDriver) patchSettings(manifest *Manifest, vars map[string]string, configDir string) error {
	settingsPath := filepath.Join(configDir, "settings.json")
	settings := map[string]any{}

	if data, err := os.ReadFile(settingsPath); err == nil {
		if len(data) > 0 {
			if err := json.Unmarshal(data, &settings); err != nil {
				return fmt.Errorf("parse settings.json: %w", err)
			}
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("read settings.json: %w", err)
	}

	hooksValue, ok := settings["hooks"].(map[string]any)
	if !ok {
		hooksValue = map[string]any{}
	}

	hooksDir := filepath.Join(configDir, "hooks") + string(filepath.Separator)
	for event, rawEntries := range hooksValue {
		entries, ok := rawEntries.([]any)
		if !ok || entries == nil {
			delete(hooksValue, event)
			continue
		}
		pruned := pruneStaleForgeHooks(entries, hooksDir)
		if len(pruned) == 0 {
			delete(hooksValue, event)
		} else {
			hooksValue[event] = pruned
		}
	}

	for _, hook := range manifest.Hooks {
		command, err := ExpandTemplate(hook.Command, vars)
		if err != nil {
			return fmt.Errorf("expand hook command %q: %w", hook.Command, err)
		}

		rawEventHooks, _ := hooksValue[hook.Event].([]any)
		eventHooks := normalizeEventHooks(rawEventHooks)
		group := ensureHookGroup(&eventHooks, hook.Matcher)
		appendCommandHook(group, command)
		hooksValue[hook.Event] = hookGroupsToAny(eventHooks)
	}

	settings["hooks"] = hooksValue

	data, err := json.MarshalIndent(settings, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal settings.json: %w", err)
	}

	if err := os.MkdirAll(configDir, 0o755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}

	tmpPath := settingsPath + ".tmp"
	if err := os.WriteFile(tmpPath, data, 0o600); err != nil {
		return fmt.Errorf("write temp settings.json: %w", err)
	}
	return os.Rename(tmpPath, settingsPath)
}

func pruneStaleForgeHooks(entries []any, hooksDir string) []any {
	configDir := filepath.Dir(strings.TrimSuffix(hooksDir, string(filepath.Separator)))
	return pruneStaleHooks(entries, configDir)
}

func (d *ClaudeCodeDriver) writeOnboardingMarker(configDir string) error {
	claudeJSON := filepath.Join(configDir, ".claude.json")
	if _, err := os.Stat(claudeJSON); err == nil {
		return nil
	}

	if err := os.MkdirAll(configDir, 0o755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}

	marker := []byte(`{"hasCompletedOnboarding":true,"lastOnboardingVersion":"0.0.0","firstStartTime":"` +
		time.Now().UTC().Format(time.RFC3339) + `"}`)

	return os.WriteFile(claudeJSON, marker, 0o600)
}

func (d *ClaudeCodeDriver) writeAPIKeyHelper(helperPath string) error {
	if err := os.MkdirAll(filepath.Dir(helperPath), 0o755); err != nil {
		return err
	}

	const script = `#!/bin/bash
set -euo pipefail

key="${ANTHROPIC_API_KEY:-}"
if [ -z "$key" ]; then
  session_name="$(tmux display-message -p '#{session_name}' 2>/dev/null || true)"
  if [ -n "$session_name" ]; then
    tmux_value="$(tmux show-environment -t "$session_name" ANTHROPIC_API_KEY 2>/dev/null || true)"
    key="${tmux_value#*=}"
  fi
fi

[ -n "$key" ] || exit 1
printf '%s\n' "$key"
`
	return os.WriteFile(helperPath, []byte(script), 0o700)
}
