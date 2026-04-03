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
    /// Returns (pane1, pane2, pane3) terminal IDs, or nil if setup failed.
    private func setupTerminals(client: GhosttyClient, count: Int) async -> [String]? {
        // Discover existing terminals — if none, open a new window
        var existingTerminals = (try? client.listTerminals()) ?? []
        if existingTerminals.isEmpty {
            if let processName = await findGhosttyProcessName() {
                _ = runOsascript("tell application \"\(processName)\" to activate")
                try? await Task.sleep(for: .milliseconds(500))
                let script = """
                tell application "System Events"
                    tell process "\(processName)"
                        keystroke "n" using command down
                    end tell
                end tell
                """
                _ = runOsascript(script)
                try? await Task.sleep(for: .seconds(2))
                existingTerminals = (try? client.listTerminals()) ?? []
            }
        }
        guard !existingTerminals.isEmpty else { return nil }

        let existingIDs = Set(existingTerminals.map(\.id))
        guard let processName = await findGhosttyProcessName() else { return nil }

        // Create enough new tabs to have `count` panes total
        var allPanes = existingTerminals.map(\.id)
        let needed = count - allPanes.count
        for _ in 0..<max(0, needed) {
            await createNewPane(processName: processName)
            try? await Task.sleep(for: .milliseconds(800))
            if let terminals = try? client.listTerminals() {
                let newOnes = terminals.filter { !existingIDs.contains($0.id) && !allPanes.contains($0.id) }
                if let newPane = newOnes.first {
                    allPanes.append(newPane.id)
                }
            }
        }

        // Return first `count` panes
        guard allPanes.count >= count else { return nil }
        return Array(allPanes.prefix(count))
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

    /// Launch real Claude Code agents that talk via MCP tools.
    /// Uses --dangerously-skip-permissions to bypass MCP trust prompts.
    /// Falls back to scripted demo if agents fail to register within timeout.
    private func runLiveClaudeDemo(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        guard let panes = await setupTerminals(client: client, count: 3) else { return false }

        let agentName = peerName.isEmpty ? "alpha" : peerName

        // Clean SharedPeerStore so we only see peers from this demo
        let sharedStore = SharedPeerStore()
        await sharedStore.removeAll()

        // Clear terminals
        for pane in panes {
            sendToTerminal(client, pane, "clear\n")
        }
        try? await Task.sleep(for: .milliseconds(500))

        // Build prompts — each agent registers, then interacts via MCP
        let prompt1 = "You are AI agent \\x27\(agentName)\\x27. Use boo MCP tools: 1) register_peer name=\\x27\(agentName)\\x27 role=\\x27claude\\x27 2) list_peers 3) send_message to \\x27scout\\x27 content \\x27hey scout, check the build logs for errors\\x27 4) Wait 15s then receive_messages. Be very concise, just call tools."
        let prompt2 = "You are AI agent \\x27scout\\x27. Use boo MCP tools: 1) register_peer name=\\x27scout\\x27 role=\\x27claude\\x27 2) Wait 10s 3) receive_messages 4) send_message to \\x27oracle\\x27 forwarding what you received. Be very concise."
        let prompt3 = "You are AI agent \\x27oracle\\x27. Use boo MCP tools: 1) register_peer name=\\x27oracle\\x27 role=\\x27claude\\x27 2) Wait 20s 3) receive_messages 4) Reply to sender via send_message with brief analysis. Be very concise."

        // Launch 3 Claude sessions staggered
        sendToTerminal(client, panes[0], "claude -p --dangerously-skip-permissions $'\(prompt1)'\n")
        try? await Task.sleep(for: .seconds(2))
        sendToTerminal(client, panes[1], "claude -p --dangerously-skip-permissions $'\(prompt2)'\n")
        try? await Task.sleep(for: .seconds(2))
        sendToTerminal(client, panes[2], "claude -p --dangerously-skip-permissions $'\(prompt3)'\n")

        // Monitor SharedPeerStore (file-based) for peer registrations.
        // Each claude -p session spawns a separate BooApp --mcp process with its
        // own PeerRegistry — the only shared state is SharedPeerStore at /tmp/boo/.
        var knownPeerNames: Set<String> = []
        var yieldedListPeers = false

        for tick in 0..<120 {
            try? await Task.sleep(for: .seconds(1))

            // Poll SharedPeerStore for new registrations
            let sharedPeers = await sharedStore.listPeers()
            for peer in sharedPeers {
                if !knownPeerNames.contains(peer.name) {
                    knownPeerNames.insert(peer.name)
                    yield(continuation, .request, "register_peer", "{ name: \"\(peer.name)\", role: \"\(peer.role ?? "claude")\" }")
                    yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(peer.peerID))\" }")
                }
            }

            // After seeing 3+ peers, yield list_peers
            if sharedPeers.count >= 3 && !yieldedListPeers {
                let names = sharedPeers.map(\.name).joined(separator: ", ")
                yield(continuation, .request, "list_peers", "{}")
                yield(continuation, .response, "list_peers", "[ \(names) ]")
                yieldedListPeers = true
            }

            // After enough time with 3 peers, yield status checks and finish
            if knownPeerNames.count >= 3 && yieldedListPeers && tick > 25 {
                for peer in sharedPeers {
                    yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(short(peer.peerID))\" }")
                    yield(continuation, .response, "get_peer_status", "{ name: \"\(peer.name)\", status: \"active\" }")
                    try? await Task.sleep(for: .milliseconds(300))
                }

                // Check for any messages exchanged
                for peer in sharedPeers {
                    let msgs = await sharedStore.receiveMessages(peerID: peer.peerID)
                    for msg in msgs {
                        yield(continuation, .request, "receive_messages", "{ peer: \"\(peer.name)\" }")
                        yield(continuation, .response, "receive_messages", "{ from: \"\(msg.from)\", content: \"\(msg.content)\" }")
                    }
                }
                break
            }

            // Bail early if no peers after 60 seconds — claude probably failed
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
                for path in ["/tmp/ghostty-boo.sock", "/tmp/ghostty-test.sock"] {
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
        for name in ["Ghostty Boo", "GhosttyBoo"] {
            let script = "tell application \"System Events\" to return exists process \"\(name)\""
            if let result = runOsascript(script),
               result.trimmingCharacters(in: .whitespacesAndNewlines) == "true" {
                return name
            }
        }
        return nil
    }

    /// Create a new pane (split) in Ghostty via AppleScript.
    /// Uses Cmd+D for vertical split so agents show side-by-side.
    private func createNewPane(processName: String) async {
        // Activate Ghostty first
        _ = runOsascript("tell application \"\(processName)\" to activate")
        try? await Task.sleep(for: .milliseconds(300))

        // Send Cmd+D for vertical split (side-by-side panes)
        let script = """
        tell application "System Events"
            tell process "\(processName)"
                keystroke "d" using command down
            end tell
        end tell
        """
        _ = runOsascript(script)
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
