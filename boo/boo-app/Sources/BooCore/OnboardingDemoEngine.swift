import Foundation

/// Drives the First Contact live demo using real Ghostty terminals + MCP tool calls.
/// If Ghostty is connected: opens real panes, visualizes MCP calls, starts Claude/Codex sessions.
/// Falls back to simulated in-memory demo if Ghostty is unavailable.
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
                let done = await self.runLiveDemo(
                    peerName: peerName, registry: registry, relay: relay,
                    machineName: machineName, client: client,
                    continuation: continuation
                )
                if !done {
                    self.yield(continuation, .error, "connect", "Could not open terminal panes — open a Ghostty Boo window first")
                }
                continuation.finish()
            }
        }
    }

    // MARK: - Live Demo (Real Ghostty Terminals)

    private func runLiveDemo(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        // 1. Discover existing terminals — if none, open a new window
        var existingTerminals = (try? client.listTerminals()) ?? []
        if existingTerminals.isEmpty {
            // Ghostty Boo is running but has no windows — open one
            if let processName = await findGhosttyProcessName() {
                _ = runOsascript("tell application \"\(processName)\" to activate")
                try? await Task.sleep(for: .milliseconds(500))
                // Cmd+N for new window
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
        guard !existingTerminals.isEmpty else {
            return false
        }
        let existingIDs = Set(existingTerminals.map(\.id))

        // 2. Create a new tab via AppleScript
        guard let processName = await findGhosttyProcessName() else { return false }
        await createNewTab(processName: processName)
        try? await Task.sleep(for: .milliseconds(1000))

        // 3. Discover new terminal
        guard let allTerminals = try? client.listTerminals() else { return false }
        let newTerminals = allTerminals.filter { !existingIDs.contains($0.id) }

        let pane1: String
        let pane2: String

        if let newPane = newTerminals.first {
            pane2 = newPane.id
            pane1 = existingTerminals.first(where: { $0.focused })?.id ?? existingTerminals[0].id
        } else if allTerminals.count >= 2 {
            pane1 = allTerminals[0].id
            pane2 = allTerminals[1].id
        } else {
            return false
        }

        // 4. Run scripted demo (reliable, fast, shows all MCP tools in action)
        // Agent demo is too fragile (trust prompts, macOS dialogs, API keys, timeouts)
        return await runScriptedDemo(
            peerName: peerName, registry: registry, relay: relay,
            machineName: machineName, client: client,
            pane1: pane1, pane2: pane2,
            continuation: continuation
        )
    }

    // MARK: - Scripted Demo (Terminal Visualization)

    private func runScriptedDemo(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        pane1: String, pane2: String,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        let alphaName = peerName.isEmpty ? "alpha" : peerName
        let betaName = "boo-guide"

        // Clear panes and show headers
        sendToTerminal(client, pane1, "clear && printf '\\033[1;36m═══ Agent: \(alphaName) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "clear && printf '\\033[1;35m═══ Agent: \(betaName) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 1: Register alpha
        yield(continuation, .request, "register_peer", "{ name: \"\(alphaName)\", role: \"claude\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m register_peer(name: \"\(alphaName)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        let alphaID = await registry.register(name: alphaName, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(alphaID))\" }")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m registered: \(short(alphaID))\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 2: Register beta
        yield(continuation, .request, "register_peer", "{ name: \"\(betaName)\", role: \"guide\" }")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m register_peer(name: \"\(betaName)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        let betaID = await registry.register(name: betaName, role: "guide", machine: "local")
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(betaID))\" }")
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m registered: \(short(betaID))\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 3: List peers
        yield(continuation, .request, "list_peers", "{}")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m list_peers()\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        let peers = await registry.listPeers()
        let peerNames = peers.map(\.name).joined(separator: ", ")
        yield(continuation, .response, "list_peers", "[ \(peerNames) ]")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m found: \(peerNames)\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 4: Alpha sends message to beta
        yield(continuation, .request, "send_message", "{ to: \"\(betaName)\", content: \"hello from boo\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m send_message(to: \"\(betaName)\", \"hello from boo\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        do {
            try await relay.send(from: alphaID, to: betaName, content: "hello from boo")
            yield(continuation, .response, "send_message", "{ status: \"delivered\" }")
            sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        } catch {
            yield(continuation, .error, "send_message", "{ error: \"\(error)\" }")
        }
        try? await Task.sleep(for: .milliseconds(400))

        // Show message arriving in pane 2
        sendToTerminal(client, pane2, "printf '\\033[33m📨 from \(alphaName): \"hello from boo\"\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 5: Beta replies
        do {
            try await relay.send(from: betaID, to: alphaName, content: "welcome to boo")
        } catch {}
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m send_message(to: \"\(alphaName)\", \"welcome to boo\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 6: Alpha receives
        yield(continuation, .request, "receive_messages", "{ peer_id: \"\(short(alphaID))\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m receive_messages()\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        let messages = await relay.receive(peerID: alphaID)
        if let msg = messages.first {
            yield(continuation, .response, "receive_messages", "[ { from: \"\(msg.from)\", content: \"\(msg.content)\" } ]")
            sendToTerminal(client, pane1, "printf '\\033[33m📨 from \(msg.from): \"\(msg.content)\"\\033[0m\\n\\n'\n")
        } else {
            yield(continuation, .response, "receive_messages", "[]")
        }
        try? await Task.sleep(for: .milliseconds(600))

        // Step 7: Status check
        yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(short(betaID))\" }")
        try? await Task.sleep(for: .milliseconds(400))
        if let status = await registry.getStatus(peerID: betaID) {
            yield(continuation, .response, "get_peer_status", "{ name: \"\(status.name)\", status: \"\(status.status.rawValue)\" }")
        }
        try? await Task.sleep(for: .milliseconds(400))

        // Show "try it yourself" hint
        sendToTerminal(client, pane1, "printf '\\n\\033[1;36m─── Try it yourself ───\\033[0m\\n'\n")
        sendToTerminal(client, pane1, "printf 'Run: \\033[1mclaude\\033[0m to start an AI session with boo MCP tools\\n'\n")

        // Clean up
        await registry.remove(peerID: alphaID)
        await registry.remove(peerID: betaID)

        return true
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

    /// Create a new tab in Ghostty via AppleScript.
    private func createNewTab(processName: String) async {
        // Activate Ghostty first
        _ = runOsascript("tell application \"\(processName)\" to activate")
        try? await Task.sleep(for: .milliseconds(300))

        // Send Cmd+T for new tab
        let script = """
        tell application "System Events"
            tell process "\(processName)"
                keystroke "t" using command down
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
