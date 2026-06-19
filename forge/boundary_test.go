package forge

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBoundary_EngineDoesNotReferenceConnectorTypes(t *testing.T) {
	t.Parallel()

	engineFiles := []string{"engine.go", "engine_claude.go"}
	connectorTypes := []string{
		"Connector ", "ConnectorConfig", "ConnectorHost",
		"ExternalReceiver", "PushReceiver", "PollReceiver", "StreamReceiver",
		"InboundMessage", "HookListener",
		"TelegramConnector", "SlackConnector", "WhatsAppConnector", "BridgeConnector",
	}

	for _, file := range engineFiles {
		data, err := os.ReadFile(filepath.Join(".", file))
		if err != nil {
			t.Fatalf("read %s: %v", file, err)
		}
		content := string(data)
		for _, typ := range connectorTypes {
			if strings.Contains(content, typ) {
				t.Errorf("%s references connector type %q — engine must not know about connectors", file, typ)
			}
		}
	}
}

func TestBoundary_ConnectorsDoNotReferenceEngineTypes(t *testing.T) {
	t.Parallel()

	connectorFiles, err := filepath.Glob("connector_*.go")
	if err != nil {
		t.Fatal(err)
	}

	engineTypes := []string{
		"EngineDriver", "ClaudeCodeDriver", "PrepareRequest", "PreparedEngine",
		"StartSpec", "EngineCapabilities", "NewEngineDriver",
		"settings.json", "patchSettings", "InstallSettings",
	}

	for _, file := range connectorFiles {
		if strings.HasSuffix(file, "_test.go") {
			continue
		}
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatalf("read %s: %v", file, err)
		}
		content := string(data)
		for _, typ := range engineTypes {
			if strings.Contains(content, typ) {
				t.Errorf("%s references engine type %q — connectors must not know about engines", file, typ)
			}
		}
	}
}

func TestBoundary_TmuxRuntimeIsGeneric(t *testing.T) {
	t.Parallel()

	data, err := os.ReadFile("tmux.go")
	if err != nil {
		t.Fatal(err)
	}
	content := string(data)

	claudeSpecific := []string{
		"claude --", "dangerously-skip-permissions", "permission-mode",
		"apiKeyHelper", "APIKeyHelper", ".claude.json",
		"onboarding", "isRootUser", "IsRoot",
	}

	for _, term := range claudeSpecific {
		if strings.Contains(content, term) {
			t.Errorf("tmux.go contains Claude-specific term %q — TmuxRuntime should be generic", term)
		}
	}
}

func TestBoundary_ConnectorHostDoesNotImportEngine(t *testing.T) {
	t.Parallel()

	data, err := os.ReadFile("connector_host.go")
	if err != nil {
		t.Fatal(err)
	}
	content := string(data)

	engineTerms := []string{
		"EngineDriver", "ClaudeCodeDriver", "PrepareRequest",
		"settings.json", "patchSettings",
	}

	for _, term := range engineTerms {
		if strings.Contains(content, term) {
			t.Errorf("connector_host.go references engine term %q", term)
		}
	}
}
