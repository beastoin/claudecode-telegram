package forge

// Dependency Direction
//
// This documents the allowed dependency graph between forge subsystems.
// Arrow means "depends on" (imports from / calls into).
//
//   app ──→ prepare, extract, manifest, runtime, transport, connectors, watchdog, engine
//   connectors ──→ connector (interfaces), commands, auth (via optional interfaces)
//   connector_host ──→ connector (interfaces), runtime, commands
//   watchdog ──→ runtime, transport, verify (integrity)
//   engine ──→ manifest, runtime (via LaunchCommander)
//   extract ──→ manifest (FileSpec, VarSpec)
//   build ──→ manifest, extract (EmbeddedFileSource)
//   commands ──→ runtime (for /status health check)
//   auth ──→ runtime (RuntimeMonitor, RuntimeHistoryCapture), connector (AuthPrompter)
//   upgrade ──→ manifest, transport
//
// Rules:
//   - Connectors MUST NOT import runtime directly; use InboundSink for message delivery
//   - Watchdog MUST NOT import connectors
//   - Commands MUST NOT import specific connector implementations
//   - Extract MUST NOT import runtime or transport
//   - No circular dependencies between subsystems
