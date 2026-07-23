package forge

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
)

type RunOptions struct {
	Context       context.Context
	Resolve       ResolveOptions
	ForceExtract  bool
	SkipConflicts bool
	AuthOnly      bool
	NoAPIKey      bool
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
	Connector            Connector
	ConnectorCfg         ConnectorConfig
	ConnectorInitialized bool
	Engine               EngineDriver
	OriginalName         string
	Identity             string
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

	var enginePrepared *PreparedEngine
	if a.Engine != nil {
		var err error
		enginePrepared, err = a.Engine.Prepare(opts.Context, PrepareRequest{
			Manifest: a.Manifest,
			Vars:     resolved,
			GOOS:     a.GOOS,
			Source:   a.Source,
		})
		if err != nil {
			return err
		}
		for k, v := range enginePrepared.Env {
			if v != "" {
				resolved[k] = v
			}
		}
	} else {
		if a.HookManager.Source == nil {
			a.HookManager.Source = a.Source
		}
		if err := a.HookManager.Install(a.Manifest, resolved); err != nil {
			return err
		}
	}

	tools, err := Bootstrap(a.Manifest, a.GOOS, a.Runner)
	if err != nil {
		return err
	}

	if a.Verifier != nil && !opts.NoAPIKey {
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

	if opts.NoAPIKey {
		delete(resolved, "ANTHROPIC_API_KEY")
		configDir := resolved["CLAUDE_CONFIG_DIR"]
		if configDir != "" {
			os.Remove(filepath.Join(configDir, ".credentials.json"))
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

		if a.Engine != nil && enginePrepared != nil {
			spec, err := a.Engine.StartSpec(opts.Context, enginePrepared)
			if err != nil {
				return err
			}
			if launcher, ok := a.Runtime.(LaunchCommander); ok && len(spec.Command) > 0 {
				launcher.SetLaunchCommand(joinShellCommand(spec.Command...))
			}
		}

		if err := a.Runtime.Start(); err != nil {
			return err
		}
		if tmuxRT, ok := a.Runtime.(*TmuxRuntime); ok {
			writeForgeState(tmuxRT.Session, a.OriginalName, a.Identity)
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

	// Start connector host if configured
	var connectorErr <-chan error
	if a.Connector != nil && opts.Context != nil {
		cfg := a.ConnectorCfg
		cfg.WorkerName = a.Manifest.Name
		if cfg.BridgeURL == "" {
			cfg.BridgeURL = resolved["BRIDGE_URL"]
		}

		if !a.ConnectorInitialized {
			if err := a.Connector.Init(opts.Context, cfg); err != nil {
				return fmt.Errorf("connector init: %w", err)
			}
		}

		// Auth lifecycle phase: runs between connector init and host loop
		if a.Engine != nil {
			spec := a.Engine.AuthSpec()
			if opts.NoAPIKey {
				spec.SuccessMarkers = []string{
					"What can I help you with?",
					"claude>",
					"Type your message",
				}
				spec.FailureMarkers = append(spec.FailureMarkers,
					"OAuth error",
					"Invalid code",
					"invalid_grant",
					"Authentication failed",
					"Login failed",
					"code has expired",
					"Unable to exchange",
				)
				spec.AutoResponses = append(spec.AutoResponses,
					AutoResponse{Marker: "run /login", Response: "/login", Once: true},
					AutoResponse{Marker: "bypass permissions", Response: "/login", Once: true},
					AutoResponse{Marker: "don't ask on", Response: "/login", Once: true},
					AutoResponse{Marker: "Press Enter to continue", Response: "", Once: true},
				)
			}
			if spec.Required || opts.AuthOnly {
				if monitor, ok := a.Runtime.(RuntimeMonitor); ok {
					auth := &AuthCoordinator{
						Runtime:    monitor,
						Spec:       spec,
						WorkerName: a.Manifest.Name,
					}
					if err := auth.Run(opts.Context, AdaptConnectorForAuth(a.Connector)); err != nil {
						return fmt.Errorf("auth: %w", err)
					}
				}
			}
		}

		if opts.AuthOnly {
			return nil
		}

		host := NewConnectorHost(a.Connector, a.Runtime, cfg)
		RegisterBuiltinCommands(host, CommandServices{
			Runtime:    a.Runtime,
			WorkerName: a.Manifest.Name,
		})

		ch := make(chan error, 1)
		go func() {
			ch <- host.Run(opts.Context)
		}()
		connectorErr = ch
		defer host.Stop()
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
			if connectorErr == nil {
				return watchdog.Run(opts.Context)
			}
			watchdogDone := make(chan error, 1)
			go func() { watchdogDone <- watchdog.Run(opts.Context) }()
			select {
			case err := <-connectorErr:
				return fmt.Errorf("connector: %w", err)
			case err := <-watchdogDone:
				return err
			}
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
