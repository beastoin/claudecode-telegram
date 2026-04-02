import SwiftUI
import BooCore

/// Transcript line for the live MCP demo.
struct TranscriptLine: Identifiable {
    let id = UUID()
    let direction: Direction
    let tool: String
    let content: String
    let timestamp: Date

    enum Direction {
        case request
        case response
        case error
    }
}

/// State machine driving the onboarding flow.
@MainActor
@Observable
final class OnboardingCoordinator {
    enum Step: Int, CaseIterable {
        case boot          // Screen 1: auto-scan
        case firstContact  // Screen 2: live demo
        case remoteLink    // Screen 3: SSH (optional)
        case ready         // Screen 4: mission ready
    }

    var currentStep: Step = .boot
    var probes: [Probe] = []
    var demoTranscript: [TranscriptLine] = []
    var peerName: String = ""
    var sshHosts: [SSHHost] = []
    var demoComplete = false

    private let detector = SetupDetector()
    private let appState: AppState

    init(appState: AppState) {
        self.appState = appState
    }

    var isFullyConfigured: Bool {
        probes.allSatisfy { !$0.isBlocking || $0.state.isPassed }
    }

    var stepLabels: [String] {
        Step.allCases.map {
            switch $0 {
            case .boot: "BOOT"
            case .firstContact: "DEMO"
            case .remoteLink: "REMOTE"
            case .ready: "READY"
            }
        }
    }

    // MARK: - Boot Sequence

    func start() async {
        // Check skip logic: if already fully configured, jump ahead
        let mcpManager = MCPConfigManager()
        if mcpManager.isInstalled() && !mcpManager.isStale() {
            let (socketPath, terminals) = await detector.probeSocket()
            if socketPath != nil && !terminals.isEmpty {
                // Already set up — skip to ready
                currentStep = .ready
                return
            }
        }

        // Set up probes
        probes = [
            Probe(id: "ghostty", label: "GHOSTTY", isBlocking: true),
            Probe(id: "socket", label: "SOCKET", isBlocking: true),
            Probe(id: "terminals", label: "TERMINALS", isBlocking: false),
            Probe(id: "claude-mcp", label: "CLAUDE MCP", isBlocking: true),
            Probe(id: "codex-mcp", label: "CODEX MCP", isBlocking: false),
        ]

        // Auto-fill peer name
        peerName = await detector.defaultPeerName()

        // Discover SSH hosts in background
        sshHosts = await detector.detectSSHHosts()

        // Run probes sequentially for visual effect
        await runProbes()
    }

    private func runProbes() async {
        // Probe 1: Ghostty install
        updateProbe("ghostty", state: .running)
        try? await Task.sleep(for: .milliseconds(400))
        let (ghosttyInstalled, variant) = await detector.detectGhosttyInstall()
        if ghosttyInstalled {
            updateProbe("ghostty", state: .passed(variant ?? "Ghostty"))
        } else {
            updateProbe("ghostty", state: .failed("Not found — Install Ghostty Boo"))
        }

        // Probe 2: Socket
        updateProbe("socket", state: .running)
        try? await Task.sleep(for: .milliseconds(400))
        let (socketPath, terminals) = await detector.probeSocket()
        if let path = socketPath {
            updateProbe("socket", state: .passed("Reachable at \(path)"))
            // Also connect AppState
            await appState.connect(socketPath: path)
        } else {
            updateProbe("socket", state: .failed("No socket — Enable control-socket in Ghostty config"))
        }

        // Probe 3: Terminals
        updateProbe("terminals", state: .running)
        try? await Task.sleep(for: .milliseconds(300))
        if !terminals.isEmpty {
            updateProbe("terminals", state: .passed("\(terminals.count) terminal\(terminals.count == 1 ? "" : "s") discovered"))
        } else {
            updateProbe("terminals", state: .skipped("0 terminals — open a Ghostty window"))
        }

        // Probe 4: Claude MCP
        updateProbe("claude-mcp", state: .running)
        try? await Task.sleep(for: .milliseconds(300))
        let (claudeInstalled, claudeStale) = await detector.checkClaudeMCP()
        if claudeInstalled && !claudeStale {
            updateProbe("claude-mcp", state: .passed("Registered"))
        } else if claudeInstalled && claudeStale {
            updateProbe("claude-mcp", state: .failed("Stale — Repair"))
        } else {
            updateProbe("claude-mcp", state: .failed("Not configured — Enable"))
        }

        // Probe 5: Codex MCP
        updateProbe("codex-mcp", state: .running)
        try? await Task.sleep(for: .milliseconds(300))
        let (codexExists, codexConfigured, codexStale) = await detector.checkCodexMCP()
        if !codexExists {
            updateProbe("codex-mcp", state: .skipped("Codex CLI not installed — Skip"))
        } else if codexConfigured && !codexStale {
            updateProbe("codex-mcp", state: .passed("Registered"))
        } else if codexConfigured && codexStale {
            updateProbe("codex-mcp", state: .failed("Stale — Repair"))
        } else {
            updateProbe("codex-mcp", state: .failed("Not configured — Enable"))
        }

        // Auto-advance if all blocking probes pass
        if isFullyConfigured {
            try? await Task.sleep(for: .seconds(1.5))
            advance()
        }
    }

    private func updateProbe(_ id: String, state: ProbeState) {
        if let index = probes.firstIndex(where: { $0.id == id }) {
            probes[index].state = state
        }
    }

    // MARK: - Probe Fix Actions

    func fixProbe(_ id: String) async {
        switch id {
        case "claude-mcp":
            let manager = MCPConfigManager()
            let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
            do {
                if manager.isStale() {
                    try manager.repair()
                } else {
                    try manager.install(binaryPath: binaryPath)
                }
                updateProbe("claude-mcp", state: .passed("Registered"))
            } catch {
                updateProbe("claude-mcp", state: .failed("Error: \(error.localizedDescription)"))
            }

        case "codex-mcp":
            let manager = CodexConfigManager()
            let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
            do {
                if manager.isStale() {
                    try manager.repair()
                } else {
                    try manager.install(binaryPath: binaryPath)
                }
                updateProbe("codex-mcp", state: .passed("Registered"))
            } catch {
                updateProbe("codex-mcp", state: .failed("Error: \(error.localizedDescription)"))
            }

        default:
            break
        }
        // Don't auto-advance after fix — let the user see the result
        // and click Continue Anyway or wait for the initial runProbes auto-advance
    }

    // MARK: - Navigation

    func advance() {
        guard let next = Step(rawValue: currentStep.rawValue + 1) else { return }
        currentStep = next
    }

    func back() {
        guard let prev = Step(rawValue: currentStep.rawValue - 1) else { return }
        currentStep = prev
    }

    func complete() {
        UserDefaults.standard.set(true, forKey: "hasCompletedOnboarding")
    }
}

// MARK: - Probe Model

struct Probe: Identifiable {
    let id: String
    let label: String
    var state: ProbeState = .pending
    let isBlocking: Bool
}

extension ProbeState {
    var isPassed: Bool {
        if case .passed = self { return true }
        return false
    }

    var isPending: Bool {
        if case .pending = self { return true }
        return false
    }

    var isRunning: Bool {
        if case .running = self { return true }
        return false
    }

    var isFailed: Bool {
        if case .failed = self { return true }
        return false
    }

    var detail: String? {
        switch self {
        case .passed(let msg), .failed(let msg), .skipped(let msg): msg
        case .pending: nil
        case .running: "Scanning..."
        }
    }
}
