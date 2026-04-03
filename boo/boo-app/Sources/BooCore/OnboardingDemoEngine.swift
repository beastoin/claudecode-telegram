import Foundation

/// Drives the First Contact live demo using real Ghostty terminals + MCP tool calls.
/// If Claude Code is available: opens real Claude agents that talk via MCP.
/// Otherwise: runs a scripted 3-agent demo showing all MCP tools in action.
public actor OnboardingDemoEngine {
    public struct DemoLine: Sendable {
        public let direction: Direction
        public let tool: String
        public let content: String
        public let timestamp: Date

        public enum Direction: Sendable {
            case request
            case response
            case error
        }
    }

    public init() {}

    /// Run the demo using real Ghostty terminals. Requires a connected socket.
    /// If Ghostty is unavailable, yields an error line and finishes.
    public func runDemo(
        peerName: String,
        registry: PeerRegistry,
        relay: MessageRelay,
        machineName: String,
        socketPath: String? = nil
    ) -> AsyncStream<DemoLine> {
        AsyncStream { continuation in
            Task {
                var socket = socketPath

                // If no socket, try launching Ghostty Boo
                if socket == nil {
                    socket = await self.tryLaunchGhostty()
                }

                // Require a real Ghostty connection — no fake demo
                guard let socket else {
                    self.yield(continuation, .error, "connect", "Ghostty Boo not connected — go back and ensure socket is working")
                    continuation.finish()
                    return
                }

                let client = GhosttyClient(socketPath: socket)

                // Require Claude Code CLI — we are agent tools, no scripted fallback.
                guard self.detectClaudeCLI() != nil else {
                    self.yield(continuation, .error, "skip", "Claude Code CLI not found — install claude to run the live demo")
                    continuation.finish()
                    return
                }

                // Pre-check: verify boo MCP is configured in Claude Code
                let mcpConfig = MCPConfigManager()
                if !mcpConfig.isInstalled() {
                    self.yield(continuation, .error, "mcp", "Boo MCP not configured — open Settings and click Install MCP")
                    continuation.finish()
                    return
                }
                if mcpConfig.isStale() {
                    try? mcpConfig.repair()
                }

                let done = await self.runLiveClaudeDemo(
                    peerName: peerName, registry: registry, relay: relay,
                    machineName: machineName, client: client,
                    continuation: continuation
                )

                if !done {
                    self.yield(continuation, .error, "demo", "Live demo did not complete — ensure Claude Code can access boo MCP tools")
                }
                continuation.finish()
            }
        }
    }

    // MARK: - Terminal Setup

    /// Ensure we have enough terminal panes for the demo.
    /// Strategy: 1) use existing terminals, 2) try pane splits, 3) fall back to new windows via `open -na`.
    private func setupTerminals(client: GhosttyClient, count: Int) async -> [String]? {
        var existingTerminals = (try? client.listTerminals()) ?? []

        // If no terminals at all, launch Ghostty Boo
        if existingTerminals.isEmpty {
            await launchGhosttyWindow()
            try? await Task.sleep(for: .seconds(2))
            existingTerminals = (try? client.listTerminals()) ?? []
        }
        guard !existingTerminals.isEmpty else { return nil }

        var allPanes = existingTerminals.map(\.id)
        let knownIDs = Set(allPanes)

        // If we already have enough, use them
        if allPanes.count >= count {
            return Array(allPanes.prefix(count))
        }

        // Try pane splits first (works when BooApp has Accessibility permissions)
        if let processName = await findGhosttyProcessName() {
            let needed = count - allPanes.count
            for _ in 0..<needed {
                await createNewPane(processName: processName)
                try? await Task.sleep(for: .milliseconds(800))
                if let terminals = try? client.listTerminals() {
                    let newOnes = terminals.filter { !knownIDs.contains($0.id) && !allPanes.contains($0.id) }
                    if let newPane = newOnes.first {
                        allPanes.append(newPane.id)
                    }
                }
            }
        }

        // If splits didn't work, fall back to opening new Ghostty windows
        if allPanes.count < count {
            let stillNeeded = count - allPanes.count
            for _ in 0..<stillNeeded {
                await launchGhosttyWindow()
                try? await Task.sleep(for: .seconds(2))
                if let terminals = try? client.listTerminals() {
                    let newOnes = terminals.filter { !knownIDs.contains($0.id) && !allPanes.contains($0.id) }
                    if let newPane = newOnes.first {
                        allPanes.append(newPane.id)
                    }
                }
            }
        }

        guard allPanes.count >= count else { return nil }
        return Array(allPanes.prefix(count))
    }

    /// Launch a new Ghostty Boo window via `open -na`.
    private func launchGhosttyWindow() async {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-na", "Ghostty Boo.app"]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        try? process.run()
        process.waitUntilExit()
    }

    // MARK: - Claude CLI Detection

    /// Check if Claude Code CLI is installed and return its path.
    private func detectClaudeCLI() -> String? {
        let candidates = [
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
            NSHomeDirectory() + "/.local/bin/claude",
            NSHomeDirectory() + "/.claude/local/claude",
        ]
        for path in candidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                return path
            }
        }
        // Fallback: which
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        process.arguments = ["claude"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            return (path?.isEmpty == false) ? path : nil
        } catch { return nil }
    }

    // MARK: - Live Claude Demo

    /// Launch real Claude Code agents in interactive mode with push messaging.
    /// Uses --dangerously-skip-permissions to bypass MCP trust prompts.
    /// Agents stay alive and receive messages via terminal injection (no polling).
    private func runLiveClaudeDemo(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        guard let panes = await setupTerminals(client: client, count: 3) else {
            yield(continuation, .error, "setup", "Could not create 3 terminal panes — try splitting Ghostty manually first")
            return false
        }

        let agentName = peerName.isEmpty ? "alpha" : peerName

        // Clean SharedPeerStore so we only see peers from this demo
        let sharedStore = SharedPeerStore()
        await sharedStore.removeAll()

        // Clear terminals
        for pane in panes {
            sendToTerminal(client, pane, "clear\n")
        }
        try? await Task.sleep(for: .milliseconds(500))

        // Launch 3 interactive Claude sessions (not -p, so they stay alive for push messages)
        for pane in panes {
            sendToTerminal(client, pane, "claude --dangerously-skip-permissions\n")
        }

        // Wait for Claude to start, then accept the folder trust prompt (press Enter)
        try? await Task.sleep(for: .seconds(4))
        for pane in panes {
            sendToTerminal(client, pane, "\r")  // Accept "Yes, I trust this folder"
        }
        try? await Task.sleep(for: .seconds(5))

        // Send initial instructions to each agent via terminal input.
        // Push messaging injects text into the terminal as user input,
        // so interactive Claude sessions will see messages from other agents naturally.
        let instruction1 = "You are agent \"\(agentName)\". Use boo MCP tools: 1) register_peer name=\"\(agentName)\" role=\"claude\" 2) list_peers 3) send_message to \"scout\" with content \"hey scout, check the build logs\". Stay alive — you'll receive push messages in this terminal. Be concise."
        let instruction2 = "You are agent \"scout\". Use boo MCP tools: 1) register_peer name=\"scout\" role=\"claude\" 2) list_peers. Then wait — messages will appear here via push. When you get one, forward it to \"oracle\" via send_message. Be concise."
        let instruction3 = "You are agent \"oracle\". Use boo MCP tools: 1) register_peer name=\"oracle\" role=\"claude\" 2) list_peers. Then wait — messages will appear here via push. When you get one, reply to the sender with a brief analysis. Be concise."

        // Use \r (carriage return) to submit — Claude's TUI uses raw mode where \r = Enter
        sendToTerminal(client, panes[0], instruction1 + "\r")
        try? await Task.sleep(for: .seconds(3))
        sendToTerminal(client, panes[1], instruction2 + "\r")
        try? await Task.sleep(for: .seconds(3))
        sendToTerminal(client, panes[2], instruction3 + "\r")

        // Monitor SharedPeerStore for peer registrations and message activity.
        // Push messages are injected directly into terminals — no polling needed by agents.
        // We still monitor SharedPeerStore to update the demo UI transcript.
        var knownPeerNames: Set<String> = []
        var yieldedListPeers = false
        var yieldedMessages = false

        for tick in 0..<120 {
            try? await Task.sleep(for: .seconds(1))

            let sharedPeers = await sharedStore.listPeers()
            for peer in sharedPeers {
                if !knownPeerNames.contains(peer.name) {
                    knownPeerNames.insert(peer.name)
                    yield(continuation, .request, "register_peer", "{ name: \"\(peer.name)\", role: \"\(peer.role ?? "claude")\" }")
                    yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(peer.peerID))\" }")
                }
            }

            if sharedPeers.count >= 3 && !yieldedListPeers {
                let names = sharedPeers.map(\.name).joined(separator: ", ")
                yield(continuation, .request, "list_peers", "{}")
                yield(continuation, .response, "list_peers", "[ \(names) ]")
                yieldedListPeers = true
            }

            // Check for messages — agents use push mode so messages flow via terminal injection
            if knownPeerNames.count >= 2 && !yieldedMessages && tick > 15 {
                for peer in sharedPeers {
                    let msgs = await sharedStore.receiveMessages(peerID: peer.peerID)
                    for msg in msgs {
                        yield(continuation, .request, "send_message", "{ to: \"\(peer.name)\", content: \"\(msg.content.prefix(40))\" }")
                        yield(continuation, .response, "send_message", "{ pushed to terminal }")
                        yieldedMessages = true
                    }
                }
            }

            // Done when we have peers and some message activity (or enough time passed)
            if knownPeerNames.count >= 3 && yieldedListPeers && tick > 30 {
                for peer in sharedPeers {
                    yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(short(peer.peerID))\" }")
                    yield(continuation, .response, "get_peer_status", "{ name: \"\(peer.name)\", status: \"active\" }")
                    try? await Task.sleep(for: .milliseconds(300))
                }
                break
            }

            if tick > 60 && knownPeerNames.isEmpty {
                return false
            }
        }

        return knownPeerNames.count >= 2
    }

    // MARK: - Ghostty Launch

    /// Try to launch Ghostty Boo and return socket path when ready.
    private func tryLaunchGhostty() async -> String? {
        // Delete stale socket before launching
        try? FileManager.default.removeItem(atPath: "/tmp/ghostty-boo.sock")

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-b", "com.beastoin.ghostty-boo"]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }

            // Wait for socket (up to 5 seconds)
            for _ in 0..<10 {
                try? await Task.sleep(for: .milliseconds(500))
                for path in ["/tmp/ghostty-boo.sock", "/tmp/ghostty.sock", "/tmp/ghostty-test.sock"] {
                    if let _ = try? GhosttyClient(socketPath: path).listTerminals() {
                        return path
                    }
                }
            }
            return nil // Launched but socket not available
        } catch {
            return nil
        }
    }

    /// Find the running Ghostty Boo process name for AppleScript.
    private func findGhosttyProcessName() async -> String? {
        for name in ["Ghostty Boo", "GhosttyBoo", "ghostty"] {
            let script = "tell application \"System Events\" to return exists process \"\(name)\""
            if let result = runOsascript(script),
               result.trimmingCharacters(in: .whitespacesAndNewlines) == "true" {
                return name
            }
        }
        return nil
    }

    /// Create a new pane (split) in Ghostty via AppleScript menu click.
    /// Uses "Split Right" menu item for reliability (keystroke approach is fragile).
    private func createNewPane(processName: String) async {
        // Activate Ghostty first
        _ = runOsascript("tell application \"\(processName)\" to activate")
        try? await Task.sleep(for: .milliseconds(300))

        // Use menu click — more reliable than keystroke injection
        let menuScript = """
        tell application "System Events"
            tell process "\(processName)"
                click menu item "Split Right" of menu 1 of menu bar item "Window" of menu bar 1
            end tell
        end tell
        """
        if runOsascript(menuScript) == nil {
            // Fallback to keystroke if menu click fails
            let keystrokeScript = """
            tell application "System Events"
                tell process "\(processName)"
                    keystroke "d" using command down
                end tell
            end tell
            """
            _ = runOsascript(keystrokeScript)
        }
    }

    // MARK: - Helpers

    private func yield(
        _ continuation: AsyncStream<DemoLine>.Continuation,
        _ direction: DemoLine.Direction, _ tool: String, _ content: String
    ) {
        continuation.yield(DemoLine(
            direction: direction, tool: tool, content: content, timestamp: Date()
        ))
    }

    private func short(_ uuid: String) -> String {
        String(uuid.prefix(8))
    }

    private func sendToTerminal(_ client: GhosttyClient, _ terminalID: String, _ text: String) {
        try? client.sendText(terminalID: terminalID, text: text)
    }

    private func runOsascript(_ script: String) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 {
                return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)
            }
        } catch {}
        return nil
    }

}
