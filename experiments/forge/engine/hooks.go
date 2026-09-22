package engine

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/beastoin/claudecode-telegram/forge/manifest"
)

// HookManager handles hook script extraction and settings.json patching.
type HookManager struct {
	Source FileSource
}

func (m HookManager) Install(mf *manifest.Manifest, vars map[string]string) error {
	if err := m.ExtractHookSources(mf, vars); err != nil {
		return err
	}
	return m.InstallSettings(mf, vars)
}

func (m HookManager) ExtractHookSources(mf *manifest.Manifest, vars map[string]string) error {
	configDir, ok := vars["CLAUDE_CONFIG_DIR"]
	if !ok || configDir == "" {
		return fmt.Errorf("CLAUDE_CONFIG_DIR is required for hook installation")
	}

	for _, hook := range mf.Hooks {
		if hook.Source == "" {
			continue
		}
		if m.Source == nil {
			return fmt.Errorf("hook source %q declared but no embedded source configured", hook.Source)
		}

		data, err := m.Source.ReadFile(hook.Source)
		if err != nil {
			data, err = m.readEmbeddedHookSource(hook.Source)
			if err != nil {
				return fmt.Errorf("read hook source %q: %w", hook.Source, err)
			}
		}

		dest, err := ExpandTemplate(hook.Command, vars)
		if err != nil {
			return fmt.Errorf("expand hook destination %q: %w", hook.Command, err)
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
			return fmt.Errorf("create hook dir for %q: %w", dest, err)
		}
		if err := os.WriteFile(dest, data, 0o755); err != nil {
			return fmt.Errorf("write hook source %q: %w", dest, err)
		}
		if err := os.Chmod(dest, 0o755); err != nil {
			return fmt.Errorf("chmod hook %q: %w", dest, err)
		}
	}

	return nil
}

func (m HookManager) readEmbeddedHookSource(source string) ([]byte, error) {
	if m.Source == nil {
		return nil, fmt.Errorf("embedded source is nil")
	}

	candidates := []string{
		CanonicalSource(source),
		BuildArtifactKey(source, false),
	}

	var lastErr error
	for _, candidate := range candidates {
		data, err := m.Source.ReadFile(candidate)
		if err == nil {
			return data, nil
		}
		lastErr = err
	}

	return nil, lastErr
}

func (HookManager) InstallSettings(mf *manifest.Manifest, vars map[string]string) error {
	configDir, ok := vars["CLAUDE_CONFIG_DIR"]
	if !ok || configDir == "" {
		return fmt.Errorf("CLAUDE_CONFIG_DIR is required for hook installation")
	}

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

	for event, rawEntries := range hooksValue {
		entries, ok := rawEntries.([]any)
		if !ok || entries == nil {
			delete(hooksValue, event)
			continue
		}
		pruned := PruneStaleHooks(entries, configDir)
		if len(pruned) == 0 {
			delete(hooksValue, event)
		} else {
			hooksValue[event] = pruned
		}
	}

	for _, hook := range mf.Hooks {
		command, err := ExpandTemplate(hook.Command, vars)
		if err != nil {
			return fmt.Errorf("expand hook command %q: %w", hook.Command, err)
		}

		rawEventHooks, _ := hooksValue[hook.Event].([]any)
		eventHooks := NormalizeEventHooks(rawEventHooks)
		group := EnsureHookGroup(&eventHooks, hook.Matcher)
		AppendCommandHook(group, command)
		hooksValue[hook.Event] = HookGroupsToAny(eventHooks)
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
	if err := os.Rename(tmpPath, settingsPath); err != nil {
		return fmt.Errorf("replace settings.json: %w", err)
	}

	return nil
}

// HookCommandExists checks if a command already exists in the hook entries.
func HookCommandExists(entries []any, command string) bool {
	for _, group := range NormalizeEventHooks(entries) {
		if GroupHasCommand(group, command) {
			return true
		}
	}
	return false
}

// NormalizeEventHooks converts raw JSON hook entries into a normalised group structure.
func NormalizeEventHooks(entries []any) []map[string]any {
	groups := []map[string]any{}
	for _, entry := range entries {
		entryMap, ok := entry.(map[string]any)
		if !ok {
			continue
		}

		matcher, _ := entryMap["matcher"].(string)
		group := EnsureHookGroup(&groups, matcher)

		if hooks, ok := entryMap["hooks"].([]any); ok {
			for _, hookEntry := range hooks {
				hookMap, ok := hookEntry.(map[string]any)
				if !ok {
					continue
				}
				AppendHookEntry(group, hookMap)
			}
			continue
		}

		if command, _ := entryMap["command"].(string); command != "" {
			AppendCommandHook(group, command)
		}
	}
	return groups
}

// EnsureHookGroup finds or creates a hook group with the given matcher.
func EnsureHookGroup(groups *[]map[string]any, matcher string) map[string]any {
	for _, group := range *groups {
		if existing, _ := group["matcher"].(string); existing == matcher {
			return group
		}
	}

	group := map[string]any{
		"hooks": []any{},
	}
	if matcher != "" {
		group["matcher"] = matcher
	}
	*groups = append(*groups, group)
	return group
}

// AppendCommandHook adds a command hook entry to a group if not already present.
func AppendCommandHook(group map[string]any, command string) {
	if GroupHasCommand(group, command) {
		return
	}
	AppendHookEntry(group, map[string]any{
		"type":    "command",
		"command": command,
	})
}

// AppendHookEntry adds a hook entry to a group, deduplicating by command+type.
func AppendHookEntry(group map[string]any, entry map[string]any) {
	hooks, _ := group["hooks"].([]any)
	command, _ := entry["command"].(string)
	entryType, _ := entry["type"].(string)
	for _, existing := range hooks {
		existingMap, ok := existing.(map[string]any)
		if !ok {
			continue
		}
		if existingCommand, _ := existingMap["command"].(string); existingCommand != command {
			continue
		}
		if existingType, _ := existingMap["type"].(string); existingType == entryType {
			return
		}
	}
	group["hooks"] = append(hooks, entry)
}

// GroupHasCommand checks if a hook group already contains a command.
func GroupHasCommand(group map[string]any, command string) bool {
	hooks, _ := group["hooks"].([]any)
	for _, existing := range hooks {
		existingMap, ok := existing.(map[string]any)
		if !ok {
			continue
		}
		if existingCommand, _ := existingMap["command"].(string); existingCommand == command {
			return true
		}
	}
	return false
}

// HookGroupsToAny converts a slice of hook groups to []any for JSON serialization.
func HookGroupsToAny(groups []map[string]any) []any {
	entries := make([]any, 0, len(groups))
	for _, group := range groups {
		entries = append(entries, group)
	}
	return entries
}

// PruneStaleHooks removes hooks that reference other node's config directories.
func PruneStaleHooks(entries []any, configDir string) []any {
	hooksDir := filepath.Join(configDir, "hooks") + string(filepath.Separator)
	result := []any{}
	for _, entry := range entries {
		entryMap, ok := entry.(map[string]any)
		if !ok {
			result = append(result, entry)
			continue
		}
		hooks, _ := entryMap["hooks"].([]any)
		if hooks == nil {
			if cmd, _ := entryMap["command"].(string); cmd != "" {
				if filepath.IsAbs(cmd) && strings.Contains(cmd, "/.claude/hooks/") && !strings.HasPrefix(cmd, hooksDir) {
					continue
				}
			}
			result = append(result, entry)
			continue
		}
		var live []any
		for _, h := range hooks {
			hMap, ok := h.(map[string]any)
			if !ok {
				live = append(live, h)
				continue
			}
			cmd, _ := hMap["command"].(string)
			if cmd != "" && filepath.IsAbs(cmd) && strings.Contains(cmd, "/.claude/hooks/") && !strings.HasPrefix(cmd, hooksDir) {
				continue
			}
			live = append(live, h)
		}
		if len(live) > 0 {
			entryMap["hooks"] = live
			result = append(result, entryMap)
		}
	}
	return result
}
