package forge

import "encoding/json"

type CheckOptions struct {
	Resolve ResolveOptions
	GOOS    string
	Runner  Runner
}

type CheckReport struct {
	Worker       string
	Version      string
	ResolvedVars map[string]string
	Tools        []ToolStatus
	Readiness    []ReadinessStatus
}

type CheckResultJSON struct {
	Command string           `json:"command"`
	Worker  string           `json:"worker"`
	Version string           `json:"version"`
	Status  string           `json:"status"`
	Summary CheckSummaryJSON `json:"summary"`
	Checks  []CheckItemJSON  `json:"checks"`
}

type CheckSummaryJSON struct {
	Passed int `json:"passed"`
	Warned int `json:"warned"`
	Failed int `json:"failed"`
}

type CheckItemJSON struct {
	ID       string `json:"id"`
	Label    string `json:"label"`
	Status   string `json:"status"`
	Severity string `json:"severity"`
	Message  string `json:"message,omitempty"`
	Observed string `json:"observed,omitempty"`
	Hint     string `json:"hint,omitempty"`
}

func FormatCheckJSON(report CheckReport) string {
	result := CheckResultJSON{
		Command: "check",
		Worker:  report.Worker,
		Version: report.Version,
	}

	for _, t := range report.Tools {
		item := CheckItemJSON{
			ID:    "tool." + t.Name,
			Label: t.Name,
		}
		if t.Required {
			item.Severity = "required"
		} else {
			item.Severity = "optional"
		}
		if t.Installed {
			item.Status = "pass"
			item.Message = t.Name + " found"
			item.Observed = t.Version
			result.Summary.Passed++
		} else if t.Required {
			item.Status = "fail"
			item.Message = t.Name + " not found"
			item.Hint = "Install " + t.Name + " or remove from manifest"
			result.Summary.Failed++
		} else {
			item.Status = "warn"
			item.Message = t.Name + " not found (optional)"
			result.Summary.Warned++
		}
		result.Checks = append(result.Checks, item)
	}

	for _, r := range report.Readiness {
		item := CheckItemJSON{
			ID:    "readiness." + r.Name,
			Label: r.Name,
		}
		if r.Required {
			item.Severity = "required"
		} else {
			item.Severity = "optional"
		}
		if r.Passed {
			item.Status = "pass"
			item.Message = r.Name + " passed"
			result.Summary.Passed++
		} else if r.Required {
			item.Status = "fail"
			item.Message = r.Output
			if item.Message == "" {
				item.Message = r.Name + " failed"
			}
			result.Summary.Failed++
		} else {
			item.Status = "warn"
			item.Message = r.Output
			if item.Message == "" {
				item.Message = r.Name + " failed (optional)"
			}
			result.Summary.Warned++
		}
		result.Checks = append(result.Checks, item)
	}

	if result.Summary.Failed > 0 {
		result.Status = "fail"
	} else {
		result.Status = "pass"
	}

	data, _ := json.MarshalIndent(result, "", "  ")
	return string(data) + "\n"
}

func RunCheckMode(manifest *Manifest, opts CheckOptions) (*CheckReport, error) {
	resolved, err := ResolveVars(manifest, opts.Resolve)
	if err != nil {
		return nil, err
	}

	tools, err := CheckTools(manifest, opts.GOOS, opts.Runner)
	if err != nil {
		return nil, err
	}

	readiness, err := RunReadiness(manifest, resolved, opts.Runner)
	if err != nil {
		return nil, err
	}

	return &CheckReport{
		Worker:       manifest.Name,
		Version:      manifest.Version,
		ResolvedVars: resolved,
		Tools:        tools,
		Readiness:    readiness,
	}, nil
}
