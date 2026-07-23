package forge

import (
	"os"
	"testing"
)

func TestParseWorkerCLI_Subcommands(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name  string
		args  []string
		check func(t *testing.T, opts WorkerCLIOptions)
	}{
		{
			name: "check subcommand",
			args: []string{"check", "--bridge-url", "http://bridge"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "check" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if !opts.Check {
					t.Fatal("Check = false")
				}
				if opts.BridgeURL != "http://bridge" {
					t.Fatalf("BridgeURL = %q", opts.BridgeURL)
				}
			},
		},
		{
			name: "verify subcommand",
			args: []string{"verify", "--output-json"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "verify" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if !opts.Verify {
					t.Fatal("Verify = false")
				}
				if !opts.OutputJSON {
					t.Fatal("OutputJSON = false")
				}
			},
		},
		{
			name: "onboard subcommand with dry-run",
			args: []string{"onboard", "--dry-run", "--bridge-url", "http://b"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "onboard" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if !opts.Onboard {
					t.Fatal("Onboard = false")
				}
				if !opts.DryRun {
					t.Fatal("DryRun = false")
				}
			},
		},
		{
			name: "run subcommand with connector",
			args: []string{"run", "--connector", "local", "--connector-opt", "LOCAL_PORT=9000"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "run" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if opts.ConnectorType != "local" {
					t.Fatalf("ConnectorType = %q", opts.ConnectorType)
				}
				if opts.ConnectorConf["LOCAL_PORT"] != "9000" {
					t.Fatalf("ConnectorConf = %v", opts.ConnectorConf)
				}
			},
		},
		{
			name: "describe subcommand no target",
			args: []string{"describe"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "describe" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if opts.DescribeTarget != "" {
					t.Fatalf("DescribeTarget = %q", opts.DescribeTarget)
				}
			},
		},
		{
			name: "describe with target",
			args: []string{"describe", "check"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "describe" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if opts.DescribeTarget != "check" {
					t.Fatalf("DescribeTarget = %q", opts.DescribeTarget)
				}
			},
		},
		{
			name: "version subcommand",
			args: []string{"version"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "version" {
					t.Fatalf("Command = %q", opts.Command)
				}
			},
		},
		{
			name: "health subcommand",
			args: []string{"health"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "health" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if !opts.Health {
					t.Fatal("Health = false")
				}
			},
		},
		{
			name: "stop subcommand",
			args: []string{"stop"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "stop" {
					t.Fatalf("Command = %q", opts.Command)
				}
				if !opts.Stop {
					t.Fatal("Stop = false")
				}
			},
		},
		{
			name: "no args defaults to run",
			args: []string{},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Command != "run" {
					t.Fatalf("Command = %q", opts.Command)
				}
			},
		},
		{
			name: "connector opt with equals in value",
			args: []string{"run", "--connector-opt", "TOKEN=a=b=c"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.ConnectorConf["TOKEN"] != "a=b=c" {
					t.Fatalf("ConnectorConf = %v", opts.ConnectorConf)
				}
			},
		},
		{
			name: "multiple connector opts",
			args: []string{"run", "--connector-opt", "A=1", "--connector-opt", "B=2"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.ConnectorConf["A"] != "1" || opts.ConnectorConf["B"] != "2" {
					t.Fatalf("ConnectorConf = %v", opts.ConnectorConf)
				}
			},
		},
		{
			name: "name-override flag",
			args: []string{"run", "--name-override", "monx"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.NameOverride != "monx" {
					t.Fatalf("NameOverride = %q", opts.NameOverride)
				}
			},
		},
		{
			name: "force-extract",
			args: []string{"onboard", "--force-extract"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if !opts.ForceExtract {
					t.Fatal("ForceExtract = false")
				}
			},
		},
		{
			name: "skip-conflicts",
			args: []string{"onboard", "--skip-conflicts"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if !opts.SkipConflicts {
					t.Fatal("SkipConflicts = false")
				}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			opts, err := ParseWorkerCLI(tt.args)
			if err != nil {
				t.Fatalf("ParseWorkerCLI(%v) error = %v", tt.args, err)
			}
			tt.check(t, opts)
		})
	}
}

func TestParseWorkerCLI_EnvVarFallbacks(t *testing.T) {
	tests := []struct {
		name  string
		env   map[string]string
		args  []string
		check func(t *testing.T, opts WorkerCLIOptions)
	}{
		{
			name: "FORGE_BRIDGE_URL fallback",
			env:  map[string]string{"FORGE_BRIDGE_URL": "http://env-bridge"},
			args: []string{"check"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.BridgeURL != "http://env-bridge" {
					t.Fatalf("BridgeURL = %q", opts.BridgeURL)
				}
			},
		},
		{
			name: "flag overrides FORGE_BRIDGE_URL",
			env:  map[string]string{"FORGE_BRIDGE_URL": "http://env"},
			args: []string{"check", "--bridge-url", "http://flag"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.BridgeURL != "http://flag" {
					t.Fatalf("BridgeURL = %q, want http://flag", opts.BridgeURL)
				}
			},
		},
		{
			name: "FORGE_IDENTITY fallback",
			env:  map[string]string{"FORGE_IDENTITY": "/path/to/key"},
			args: []string{"run"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.Identity != "/path/to/key" {
					t.Fatalf("Identity = %q", opts.Identity)
				}
			},
		},
		{
			name: "FORGE_CONNECTOR fallback",
			env:  map[string]string{"FORGE_CONNECTOR": "telegram-poll"},
			args: []string{"run"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.ConnectorType != "telegram-poll" {
					t.Fatalf("ConnectorType = %q", opts.ConnectorType)
				}
			},
		},
		{
			name: "FORGE_OUTPUT_JSON=1 enables JSON",
			env:  map[string]string{"FORGE_OUTPUT_JSON": "1"},
			args: []string{"check"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if !opts.OutputJSON {
					t.Fatal("OutputJSON = false")
				}
			},
		},
		{
			name: "FORGE_OUTPUT_JSON=0 does not enable",
			env:  map[string]string{"FORGE_OUTPUT_JSON": "0"},
			args: []string{"check"},
			check: func(t *testing.T, opts WorkerCLIOptions) {
				if opts.OutputJSON {
					t.Fatal("OutputJSON = true")
				}
			},
		},
	}

	envKeys := []string{"FORGE_BRIDGE_URL", "FORGE_IDENTITY", "FORGE_CONNECTOR", "FORGE_OUTPUT_JSON"}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			saved := map[string]string{}
			for _, k := range envKeys {
				saved[k] = os.Getenv(k)
				os.Unsetenv(k)
			}
			defer func() {
				for _, k := range envKeys {
					if v := saved[k]; v != "" {
						os.Setenv(k, v)
					} else {
						os.Unsetenv(k)
					}
				}
			}()

			for k, v := range tt.env {
				os.Setenv(k, v)
			}

			opts, err := ParseWorkerCLI(tt.args)
			if err != nil {
				t.Fatalf("error = %v", err)
			}
			tt.check(t, opts)
		})
	}
}

func TestParseConnectorOpts(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name  string
		flags []string
		want  map[string]string
	}{
		{"empty", nil, map[string]string{}},
		{"single", []string{"K=V"}, map[string]string{"K": "V"}},
		{"equals in value", []string{"K=a=b"}, map[string]string{"K": "a=b"}},
		{"multiple", []string{"A=1", "B=2"}, map[string]string{"A": "1", "B": "2"}},
		{"no equals ignored", []string{"NOEQ"}, map[string]string{}},
		{"empty value", []string{"K="}, map[string]string{"K": ""}},
		{"duplicate key last wins", []string{"K=1", "K=2"}, map[string]string{"K": "2"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			got := parseConnectorOpts(tt.flags)
			for k, want := range tt.want {
				if got[k] != want {
					t.Errorf("got[%q] = %q, want %q", k, got[k], want)
				}
			}
			if len(got) != len(tt.want) {
				t.Errorf("got %d keys, want %d", len(got), len(tt.want))
			}
		})
	}
}

func TestParseWorkerCLI_UnknownFlagReturnsError(t *testing.T) {
	t.Parallel()
	_, err := ParseWorkerCLI([]string{"--nonexistent-flag"})
	if err == nil {
		t.Fatal("expected error for unknown flag")
	}
}
