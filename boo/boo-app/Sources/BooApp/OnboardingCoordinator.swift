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
    private let installer = GhosttyBooInstaller()

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
        // Set up probes FIRST so UI always has content
        probes = [
            Probe(id: "ghostty", label: "GHOSTTY BOO", isBlocking: true),
            Probe(id: "socket", label: "SOCKET", isBlocking: true),
            Probe(id: "terminals", label: "TERMINALS", isBlocking: false),
            Probe(id: "claude-mcp", label: "CLAUDE MCP", isBlocking: true),
            Probe(id: "codex-mcp", label: "CODEX MCP", isBlocking: false),
        ]

        // Quick skip: use only fast, non-blocking checks (no socket IO)
        // File reads only — no network, no process spawning
        let mcpManager = MCPConfigManager()
        if mcpManager.isInstalled() && !mcpManager.isStale() {
            // Check Ghostty Boo via file system only (avoid NSWorkspace which can be slow)
            let fm = FileManager.default
            let hasGhosttyBoo = fm.fileExists(atPath: "/Applications/Ghostty Boo.app")
                || fm.fileExists(atPath: NSHomeDirectory() + "/Applications/Ghostty Boo.app")
            if hasGhosttyBoo {
                currentStep = .ready
                return
            }
        }

        // Auto-fill peer name
        peerName = await detector.defaultPeerName()

        // Discover SSH hosts in background
        sshHosts = await detector.detectSSHHosts()

        // Run probes sequentially for visual effect
        await runProbes()
    }

    private func runProbes() async {
        // Probe 1: Ghostty Boo install (only Ghostty Boo counts — original Ghostty doesn't have our socket API)
        updateProbe("ghostty", state: .running)
        try? await Task.sleep(for: .milliseconds(400))
        var hasGhosttyBoo = await detector.detectGhosttyBooInstall()
        if hasGhosttyBoo {
            updateProbe("ghostty", state: .passed("Ghostty Boo"))
        } else {
            // Auto-install Ghostty Boo
            updateProbe("ghostty", state: .running)
            do {
                try await installer.install { [weak self] status in
                    guard let self else { return }
                    switch status {
                    case .downloading: self.updateProbe("ghostty", state: .running)
                    case .unzipping: break
                    case .installing: break
                    case .configuring: break
                    case .done(let path):
                        self.updateProbe("ghostty", state: .passed("Installed to \(path)"))
                    case .error(let msg):
                        self.updateProbe("ghostty", state: .failed(msg))
                    case .idle: break
                    }
                }
                hasGhosttyBoo = true

                // Remove stale socket before launching so Ghostty Boo creates a fresh one
                try? FileManager.default.removeItem(atPath: "/tmp/ghostty.sock")
                // Auto-launch Ghostty Boo so socket + terminal become available
                launchGhosttyBoo()
                // Wait for Ghostty Boo to start and create the socket
                try? await Task.sleep(for: .seconds(3))
            } catch {
                let msg = (error as? GhosttyBooInstallerError)?.localizedDescription ?? error.localizedDescription
                updateProbe("ghostty", state: .failed(msg))
            }
        }

        // Probe 2: Socket — skip if Ghostty Boo not installed (no point checking)
        updateProbe("socket", state: .running)
        try? await Task.sleep(for: .milliseconds(400))
        var terminals: [TerminalInfo] = []
        if hasGhosttyBoo {
            var result = await detector.probeSocket()
            if result.path == nil {
                // Socket not responding — remove stale file, launch Ghostty Boo, retry
                try? FileManager.default.removeItem(atPath: "/tmp/ghostty.sock")
                launchGhosttyBoo()
                try? await Task.sleep(for: .seconds(3))
                result = await detector.probeSocket()
            }
            if let path = result.path {
                terminals = result.terminals
                updateProbe("socket", state: .passed("Reachable at \(path)"))
                await appState.connect(socketPath: path)
            } else {
                updateProbe("socket", state: .failed("No socket — launch Ghostty Boo"))
            }
        } else {
            updateProbe("socket", state: .skipped("Waiting for Ghostty Boo"))
        }

        // Probe 3: Terminals — skip if no socket
        updateProbe("terminals", state: .running)
        try? await Task.sleep(for: .milliseconds(300))
        if !hasGhosttyBoo {
            updateProbe("terminals", state: .skipped("Waiting for Ghostty Boo"))
        } else if !terminals.isEmpty {
            updateProbe("terminals", state: .passed("\(terminals.count) terminal\(terminals.count == 1 ? "" : "s") discovered"))
        } else {
            updateProbe("terminals", state: .skipped("0 terminals — open a Ghostty Boo window"))
        }

        // Probe 4: Claude MCP — auto-fix if not configured
        updateProbe("claude-mcp", state: .running)
        try? await Task.sleep(for: .milliseconds(300))
        let (claudeInstalled, claudeStale) = await detector.checkClaudeMCP()
        if claudeInstalled && !claudeStale {
            updateProbe("claude-mcp", state: .passed("Registered"))
        } else {
            // Auto-fix: install or repair without user intervention
            let manager = MCPConfigManager()
            let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
            do {
                if claudeStale {
                    try manager.repair()
                } else {
                    try manager.install(binaryPath: binaryPath)
                }
                updateProbe("claude-mcp", state: .passed("Registered"))
            } catch {
                updateProbe("claude-mcp", state: .failed("Error: \(error.localizedDescription)"))
            }
        }

        // Probe 5: Codex MCP — auto-fix if Codex installed but MCP not configured
        updateProbe("codex-mcp", state: .running)
        try? await Task.sleep(for: .milliseconds(300))
        let (codexExists, codexConfigured, codexStale) = await detector.checkCodexMCP()
        if !codexExists {
            updateProbe("codex-mcp", state: .skipped("Codex CLI not installed"))
        } else if codexConfigured && !codexStale {
            updateProbe("codex-mcp", state: .passed("Registered"))
        } else {
            // Auto-fix: install or repair without user intervention
            let manager = CodexConfigManager()
            let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
            do {
                if codexStale {
                    try manager.repair()
                } else {
                    try manager.install(binaryPath: binaryPath)
                }
                updateProbe("codex-mcp", state: .passed("Registered"))
            } catch {
                updateProbe("codex-mcp", state: .failed("Error: \(error.localizedDescription)"))
            }
        }

        // Start recheck loop if not all blocking probes pass (user may fix externally)
        if !isFullyConfigured {
            recheckTask = Task { await recheckLoop() }
        }
        // Never auto-advance — let user click Continue
    }

    private var recheckTask: Task<Void, Never>?

    /// Poll environment every 3 seconds to detect external fixes (e.g. user installed Ghostty Boo).
    private func recheckLoop() async {
        while !Task.isCancelled && currentStep == .boot {
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled && currentStep == .boot else { break }

            // Recheck ghostty
            let nowHasGhosttyBoo = await detector.detectGhosttyBooInstall()
            if nowHasGhosttyBoo {
                if let probe = probes.first(where: { $0.id == "ghostty" }), !probe.state.isPassed {
                    updateProbe("ghostty", state: .passed("Ghostty Boo"))
                }

                // Recheck socket
                let (socketPath, terminals) = await detector.probeSocket()
                if let path = socketPath {
                    if let probe = probes.first(where: { $0.id == "socket" }), !probe.state.isPassed {
                        updateProbe("socket", state: .passed("Reachable at \(path)"))
                        await appState.connect(socketPath: path)
                    }
                    if !terminals.isEmpty {
                        updateProbe("terminals", state: .passed("\(terminals.count) terminal\(terminals.count == 1 ? "" : "s") discovered"))
                    }
                }
            }

            // Recheck MCP
            let (claudeInstalled, claudeStale) = await detector.checkClaudeMCP()
            if claudeInstalled && !claudeStale {
                updateProbe("claude-mcp", state: .passed("Registered"))
            }

            // Stop rechecking once all pass — user will click Continue
            if isFullyConfigured {
                break
            }
        }
    }

    private func updateProbe(_ id: String, state: ProbeState) {
        if let index = probes.firstIndex(where: { $0.id == id }) {
            probes[index].state = state
        }
    }

    /// Launch Ghostty Boo so its socket and terminal become available.
    private func launchGhosttyBoo() {
        #if canImport(AppKit)
        let fm = FileManager.default
        let path: String
        if fm.fileExists(atPath: GhosttyBooInstaller.systemInstallPath) {
            path = GhosttyBooInstaller.systemInstallPath
        } else if fm.fileExists(atPath: GhosttyBooInstaller.userInstallPath) {
            path = GhosttyBooInstaller.userInstallPath
        } else {
            return
        }
        let url = URL(fileURLWithPath: path)
        let config = NSWorkspace.OpenConfiguration()
        NSWorkspace.shared.openApplication(at: url, configuration: config)
        #endif
    }

    // MARK: - Probe Fix Actions

    func fixProbe(_ id: String) async {
        switch id {
        case "ghostty":
            updateProbe("ghostty", state: .running)
            do {
                try await installer.install { [weak self] status in
                    guard let self else { return }
                    if case .done(let path) = status {
                        self.updateProbe("ghostty", state: .passed("Installed to \(path)"))
                    }
                }
                // Auto-launch Ghostty Boo and wait for socket
                launchGhosttyBoo()
                try? await Task.sleep(for: .seconds(3))
                // Re-run socket/terminal probes after install
                await rerunSocketProbes()
            } catch {
                let msg = (error as? GhosttyBooInstallerError)?.localizedDescription ?? error.localizedDescription
                updateProbe("ghostty", state: .failed(msg))
            }

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

    // MARK: - Re-run socket probes after Ghostty Boo install

    private func rerunSocketProbes() async {
        // Brief delay for Ghostty Boo to start if user launches it
        try? await Task.sleep(for: .milliseconds(500))

        updateProbe("socket", state: .running)
        let (socketPath, terminals) = await detector.probeSocket()
        if let path = socketPath {
            updateProbe("socket", state: .passed("Reachable at \(path)"))
            await appState.connect(socketPath: path)
        } else {
            updateProbe("socket", state: .skipped("Open Ghostty Boo to connect"))
        }

        updateProbe("terminals", state: .running)
        if !terminals.isEmpty {
            updateProbe("terminals", state: .passed("\(terminals.count) terminal\(terminals.count == 1 ? "" : "s") discovered"))
        } else {
            updateProbe("terminals", state: .skipped("Open a Ghostty Boo window"))
        }
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
