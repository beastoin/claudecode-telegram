package forge

import (
	"fmt"
	"os"
	"regexp"
	"strings"
)

type ResolveOptions struct {
	Env   map[string]string
	Flags map[string]string
	Creds map[string]string
}

var varRefPattern = regexp.MustCompile(`\$(\{)?([A-Za-z_][A-Za-z0-9_]*)\}?`)

type Prepared struct {
	Vars map[string]string
	Dirs []string
}

func ResolveVars(manifest *Manifest, opts ResolveOptions) (map[string]string, error) {
	if err := ValidateManifest(manifest); err != nil {
		return nil, err
	}

	resolved := make(map[string]string, len(manifest.Vars))
	state := make(map[string]visitState, len(manifest.Vars))

	for name := range manifest.Vars {
		if _, err := resolveVar(name, manifest, opts, resolved, state); err != nil {
			return nil, err
		}
	}

	return resolved, nil
}

type visitState int

const (
	visitUnseen visitState = iota
	visitActive
	visitDone
)

func resolveVar(name string, manifest *Manifest, opts ResolveOptions, resolved map[string]string, state map[string]visitState) (string, error) {
	if value, ok := resolved[name]; ok {
		return value, nil
	}

	spec, ok := manifest.Vars[name]
	if !ok {
		return "", fmt.Errorf("unknown variable %q", name)
	}

	switch state[name] {
	case visitActive:
		return "", fmt.Errorf("variable resolution cycle detected at %q", name)
	case visitDone:
		return resolved[name], nil
	}

	state[name] = visitActive
	defer func() {
		if state[name] == visitActive {
			state[name] = visitDone
		}
	}()

	if value, ok := firstNonEmpty(opts.Flags[name], opts.Env[name]); ok {
		resolved[name] = value
		return value, nil
	}

	var value string
	switch spec.Source {
	case "env":
		value = opts.Env[name]
	case "flag":
		value = opts.Flags[name]
	case "creds":
		value = opts.Creds[name]
	case "default":
		expanded, err := expandValue(spec.Default, manifest, opts, resolved, state)
		if err != nil {
			return "", fmt.Errorf("expand default for %q: %w", name, err)
		}
		value = expanded
	default:
		return "", fmt.Errorf("var %q uses unsupported source %q", name, spec.Source)
	}

	if value == "" {
		if spec.Default != "" && spec.Source != "default" {
			expanded, err := expandValue(spec.Default, manifest, opts, resolved, state)
			if err != nil {
				return "", fmt.Errorf("expand fallback default for %q: %w", name, err)
			}
			value = expanded
		}
	}

	if value == "" && spec.Required {
		return "", fmt.Errorf("required variable %q is not resolved", name)
	}

	resolved[name] = value
	return value, nil
}

func expandValue(template string, manifest *Manifest, opts ResolveOptions, resolved map[string]string, state map[string]visitState) (string, error) {
	var expandErr error

	result := varRefPattern.ReplaceAllStringFunc(template, func(match string) string {
		if expandErr != nil {
			return ""
		}

		name := strings.TrimPrefix(match, "$")
		name = strings.TrimPrefix(name, "{")
		name = strings.TrimSuffix(name, "}")

		if _, ok := manifest.Vars[name]; ok {
			value, err := resolveVar(name, manifest, opts, resolved, state)
			if err != nil {
				expandErr = err
				return ""
			}
			return value
		}

		return opts.Env[name]
	})

	if expandErr != nil {
		return "", expandErr
	}

	return result, nil
}

func firstNonEmpty(values ...string) (string, bool) {
	for _, value := range values {
		if value != "" {
			return value, true
		}
	}
	return "", false
}

func Prepare(manifest *Manifest, vars map[string]string) (*Prepared, error) {
	prepared := &Prepared{
		Vars: copyStringMap(vars),
		Dirs: make([]string, 0, len(manifest.Dirs)),
	}

	for _, rawDir := range manifest.Dirs {
		dir, err := ExpandTemplate(rawDir, vars)
		if err != nil {
			return nil, fmt.Errorf("expand dir %q: %w", rawDir, err)
		}
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, fmt.Errorf("create dir %q: %w", dir, err)
		}
		prepared.Dirs = append(prepared.Dirs, dir)
	}

	return prepared, nil
}

func ExpandTemplate(template string, vars map[string]string) (string, error) {
	var missing []string

	result := varRefPattern.ReplaceAllStringFunc(template, func(match string) string {
		name := strings.TrimPrefix(match, "$")
		name = strings.TrimPrefix(name, "{")
		name = strings.TrimSuffix(name, "}")
		value, ok := vars[name]
		if !ok {
			missing = append(missing, name)
			return ""
		}
		return value
	})

	if len(missing) > 0 {
		return "", fmt.Errorf("missing variables: %s", strings.Join(missing, ", "))
	}

	return result, nil
}

func copyStringMap(input map[string]string) map[string]string {
	out := make(map[string]string, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}
