package forge

import "context"

type RunOptions struct {
	Context       context.Context
	Resolve       ResolveOptions
	ForceExtract  bool
	SkipConflicts bool
}

type WatchdogRunner interface {
	Run(ctx context.Context) error
}

type App struct {
	Manifest    *Manifest
	Source      EmbeddedFileSource
	Decryptor   Decryptor
	Verifier    IntegrityVerifier
	Runner      Runner
	Runtime     Runtime
	Transport   Transport
	Watchdog    WatchdogRunner
	HookManager HookManager
	GOOS        string
}

func (a App) Run(opts RunOptions) error {
	a.Manifest.Files = ExpandDirSources(a.Manifest.Files, a.Source)

	resolved, err := ResolveVars(a.Manifest, opts.Resolve)
	if err != nil {
		return err
	}

	if _, err := Prepare(a.Manifest, resolved); err != nil {
		return err
	}

	if err := Extract(a.Manifest, ExtractOptions{
		Vars:          resolved,
		Source:         a.Source,
		Decryptor:      a.Decryptor,
		Verifier:       a.Verifier,
		ForceExtract:   opts.ForceExtract,
		SkipConflicts:  opts.SkipConflicts,
	}); err != nil {
		return err
	}

	if a.HookManager.Source == nil {
		a.HookManager.Source = a.Source
	}
	if err := a.HookManager.Install(a.Manifest, resolved); err != nil {
		return err
	}

	tools, err := Bootstrap(a.Manifest, a.GOOS, a.Runner)
	if err != nil {
		return err
	}

	if a.Verifier != nil {
		artifacts, err := EnumerateExtractedArtifacts(a.Manifest, resolved)
		if err != nil {
			return err
		}
		if _, _, err := VerifyExtractedArtifacts(artifacts, a.Verifier, func(artifact ExtractedArtifact) bool {
			return artifact.SkipVerification
		}); err != nil {
			return err
		}
	}

	if _, err := RunReadiness(a.Manifest, resolved, a.Runner); err != nil {
		return err
	}

	if a.Runtime != nil {
		if configurable, ok := a.Runtime.(RuntimeConfigurer); ok {
			if err := configurable.ConfigureRuntime(RuntimeConfig{
				Vars: copyStringMap(resolved),
			}); err != nil {
				return err
			}
		}
		if err := a.Runtime.Start(); err != nil {
			return err
		}
	}

	if a.Transport != nil {
		bridgeURL := resolved["BRIDGE_URL"]
		if err := a.Transport.Connect(context.Background(), bridgeURL); err != nil {
			return err
		}

		// Register is best-effort; the watchdog retries periodically
		a.Transport.Register(context.Background(), RegisterRequest{
			Name:    a.Manifest.Name,
			Host:    resolved["HOST"],
			Version: a.Manifest.Version,
			Tools:   installedTools(tools),
		})
	}

	if opts.Context != nil {
		watchdog := a.Watchdog
		if watchdog == nil {
			var integrity IntegrityMonitor
			if a.Verifier != nil {
				integrity = &FileIntegrityMonitor{
					Manifest: a.Manifest,
					Vars:     resolved,
					Verifier: a.Verifier,
				}
			}

			var bridge BridgeClient
			if a.Transport != nil {
				bridge = transportBridgeClient{transport: a.Transport}
			}

			watchdog = &Watchdog{
				Runtime:   a.Runtime,
				Bridge:    bridge,
				Integrity: integrity,
				Transport: a.Transport,
				Register: RegisterRequest{
					Name:    a.Manifest.Name,
					Host:    resolved["HOST"],
					Version: a.Manifest.Version,
					Tools:   installedTools(tools),
				},
			}
		}
		if watchdog != nil {
			return watchdog.Run(opts.Context)
		}
	}

	return nil
}

type transportBridgeClient struct {
	transport Transport
}

func (t transportBridgeClient) Alert(message string) error {
	return t.transport.SendMessage(context.Background(), WorkerMessage{Text: message})
}

func installedTools(statuses []ToolStatus) map[string]string {
	tools := make(map[string]string, len(statuses))
	for _, status := range statuses {
		if !status.Installed {
			continue
		}
		tools[status.Name] = status.Version
	}
	return tools
}
