package forge

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
