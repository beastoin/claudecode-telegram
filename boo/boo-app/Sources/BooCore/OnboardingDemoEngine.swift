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

                // Always use scripted demo — it's reliable and shows all MCP tools.
                // Live Claude sessions are too fragile (MCP trust prompts, API keys,
                // stale binary paths, macOS dialogs).
                let done = await self.runScriptedDemo3Agents(
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
            await createNewTab(processName: processName)
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

    // MARK: - Scripted 3-Agent Demo

    /// Scripted demo with 3 agents showing all MCP tools in action.
    private func runScriptedDemo3Agents(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        guard let panes = await setupTerminals(client: client, count: 3) else {
            // Fall back to 2 panes
            guard let panes2 = await setupTerminals(client: client, count: 2) else { return false }
            return await runScriptedDemo2Panes(
                peerName: peerName, registry: registry, relay: relay,
                machineName: machineName, client: client,
                pane1: panes2[0], pane2: panes2[1],
                continuation: continuation
            )
        }
        let pane1 = panes[0], pane2 = panes[1], pane3 = panes[2]

        let agent1 = peerName.isEmpty ? "alpha" : peerName
        let agent2 = "scout"
        let agent3 = "oracle"

        // Clear panes and show headers
        sendToTerminal(client, pane1, "clear && printf '\\033[1;36m═══ Agent: \(agent1) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "clear && printf '\\033[1;35m═══ Agent: \(agent2) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane3, "clear && printf '\\033[1;33m═══ Agent: \(agent3) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 1: Register all 3 agents
        yield(continuation, .request, "register_peer", "{ name: \"\(agent1)\", role: \"claude\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m register_peer(name: \"\(agent1)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let id1 = await registry.register(name: agent1, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(id1))\" }")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m registered: \(short(id1))\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        yield(continuation, .request, "register_peer", "{ name: \"\(agent2)\", role: \"claude\" }")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m register_peer(name: \"\(agent2)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let id2 = await registry.register(name: agent2, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(id2))\" }")
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m registered: \(short(id2))\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        yield(continuation, .request, "register_peer", "{ name: \"\(agent3)\", role: \"claude\" }")
        sendToTerminal(client, pane3, "printf '\\033[36m→\\033[0m register_peer(name: \"\(agent3)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let id3 = await registry.register(name: agent3, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(id3))\" }")
        sendToTerminal(client, pane3, "printf '\\033[32m✓\\033[0m registered: \(short(id3))\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 2: List peers — all 3 visible
        yield(continuation, .request, "list_peers", "{}")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m list_peers()\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let peers = await registry.listPeers()
        let peerNames = peers.map(\.name).joined(separator: ", ")
        yield(continuation, .response, "list_peers", "[ \(peerNames) ]")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m found: \(peerNames)\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 3: Agent 1 → Agent 2
        yield(continuation, .request, "send_message", "{ to: \"\(agent2)\", content: \"scout, check the logs\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m send_message(to: \"\(agent2)\", \"scout, check the logs\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        try? await relay.send(from: id1, to: agent2, content: "scout, check the logs")
        yield(continuation, .response, "send_message", "{ status: \"delivered\" }")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))

        // Show in pane 2
        sendToTerminal(client, pane2, "printf '\\033[33m📨 from \(agent1): \"scout, check the logs\"\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 4: Agent 2 → Agent 3
        yield(continuation, .request, "send_message", "{ to: \"\(agent3)\", content: \"oracle, need analysis on build errors\" }")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m send_message(to: \"\(agent3)\", \"oracle, need analysis on build errors\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        try? await relay.send(from: id2, to: agent3, content: "oracle, need analysis on build errors")
        yield(continuation, .response, "send_message", "{ status: \"delivered\" }")
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))

        // Show in pane 3
        sendToTerminal(client, pane3, "printf '\\033[33m📨 from \(agent2): \"oracle, need analysis on build errors\"\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 5: Agent 3 replies to both
        try? await relay.send(from: id3, to: agent2, content: "found 2 type errors in MCPServer.swift")
        sendToTerminal(client, pane3, "printf '\\033[36m→\\033[0m send_message(to: \"\(agent2)\", \"found 2 type errors\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane3, "printf '\\033[32m✓\\033[0m delivered\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))

        try? await relay.send(from: id3, to: agent1, content: "analysis complete, 2 issues found")
        sendToTerminal(client, pane3, "printf '\\033[36m→\\033[0m send_message(to: \"\(agent1)\", \"analysis complete\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane3, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Step 6: Agent 1 receives messages
        yield(continuation, .request, "receive_messages", "{ peer_id: \"\(short(id1))\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m receive_messages()\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let messages = await relay.receive(peerID: id1)
        if let msg = messages.first {
            yield(continuation, .response, "receive_messages", "[ { from: \"\(msg.from)\", content: \"\(msg.content)\" } ]")
            sendToTerminal(client, pane1, "printf '\\033[33m📨 from \(msg.from): \"\(msg.content)\"\\033[0m\\n\\n'\n")
        } else {
            yield(continuation, .response, "receive_messages", "[]")
        }
        try? await Task.sleep(for: .milliseconds(500))

        // Step 7: Status checks
        yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(short(id2))\" }")
        try? await Task.sleep(for: .milliseconds(300))
        if let status = await registry.getStatus(peerID: id2) {
            yield(continuation, .response, "get_peer_status", "{ name: \"\(status.name)\", status: \"\(status.status.rawValue)\" }")
        }
        try? await Task.sleep(for: .milliseconds(300))

        yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(short(id3))\" }")
        try? await Task.sleep(for: .milliseconds(300))
        if let status = await registry.getStatus(peerID: id3) {
            yield(continuation, .response, "get_peer_status", "{ name: \"\(status.name)\", status: \"\(status.status.rawValue)\" }")
        }

        // Show "try it yourself" hint
        sendToTerminal(client, pane1, "printf '\\n\\033[1;36m─── Try it yourself ───\\033[0m\\n'\n")
        sendToTerminal(client, pane1, "printf 'Run: \\033[1mclaude\\033[0m to start an AI session with boo MCP tools\\n'\n")

        // Clean up
        await registry.remove(peerID: id1)
        await registry.remove(peerID: id2)
        await registry.remove(peerID: id3)

        return true
    }

    /// Fallback 2-pane scripted demo with 3 agents sharing 2 panes.
    private func runScriptedDemo2Panes(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        pane1: String, pane2: String,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        let agent1 = peerName.isEmpty ? "alpha" : peerName
        let agent2 = "scout"
        let agent3 = "oracle"

        // Clear panes
        sendToTerminal(client, pane1, "clear && printf '\\033[1;36m═══ Agent: \(agent1) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "clear && printf '\\033[1;35m═══ Agents: \(agent2) & \(agent3) ═══\\033[0m\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Register all 3
        yield(continuation, .request, "register_peer", "{ name: \"\(agent1)\", role: \"claude\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m register_peer(name: \"\(agent1)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let id1 = await registry.register(name: agent1, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(id1))\" }")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m registered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))

        yield(continuation, .request, "register_peer", "{ name: \"\(agent2)\", role: \"claude\" }")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m register_peer(name: \"\(agent2)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let id2 = await registry.register(name: agent2, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(id2))\" }")
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m registered\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))

        yield(continuation, .request, "register_peer", "{ name: \"\(agent3)\", role: \"claude\" }")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m register_peer(name: \"\(agent3)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let id3 = await registry.register(name: agent3, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(short(id3))\" }")
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m registered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // List peers
        yield(continuation, .request, "list_peers", "{}")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m list_peers()\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let peers = await registry.listPeers()
        let peerNames = peers.map(\.name).joined(separator: ", ")
        yield(continuation, .response, "list_peers", "[ \(peerNames) ]")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m found: \(peerNames)\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Agent 1 → Agent 2
        yield(continuation, .request, "send_message", "{ to: \"\(agent2)\", content: \"scout, check the logs\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m send_message(to: \"\(agent2)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        try? await relay.send(from: id1, to: agent2, content: "scout, check the logs")
        yield(continuation, .response, "send_message", "{ status: \"delivered\" }")
        sendToTerminal(client, pane1, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "printf '\\033[33m📨 \(agent2) ← \(agent1): \"scout, check the logs\"\\033[0m\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Agent 2 → Agent 3
        try? await relay.send(from: id2, to: agent3, content: "oracle, need analysis")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m \(agent2) send_message(to: \"\(agent3)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m delivered\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "printf '\\033[33m📨 \(agent3) ← \(agent2): \"oracle, need analysis\"\\033[0m\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Agent 3 replies to agent 1
        try? await relay.send(from: id3, to: agent1, content: "analysis complete, 2 issues found")
        sendToTerminal(client, pane2, "printf '\\033[36m→\\033[0m \(agent3) send_message(to: \"\(agent1)\")\\n'\n")
        try? await Task.sleep(for: .milliseconds(300))
        sendToTerminal(client, pane2, "printf '\\033[32m✓\\033[0m delivered\\n\\n'\n")
        try? await Task.sleep(for: .milliseconds(500))

        // Agent 1 receives
        yield(continuation, .request, "receive_messages", "{ peer_id: \"\(short(id1))\" }")
        sendToTerminal(client, pane1, "printf '\\033[36m→\\033[0m receive_messages()\\n'\n")
        try? await Task.sleep(for: .milliseconds(400))
        let messages = await relay.receive(peerID: id1)
        if let msg = messages.first {
            yield(continuation, .response, "receive_messages", "[ { from: \"\(msg.from)\", content: \"\(msg.content)\" } ]")
            sendToTerminal(client, pane1, "printf '\\033[33m📨 from \(msg.from): \"\(msg.content)\"\\033[0m\\n\\n'\n")
        } else {
            yield(continuation, .response, "receive_messages", "[]")
        }
        try? await Task.sleep(for: .milliseconds(500))

        // Status
        yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(short(id2))\" }")
        try? await Task.sleep(for: .milliseconds(300))
        if let status = await registry.getStatus(peerID: id2) {
            yield(continuation, .response, "get_peer_status", "{ name: \"\(status.name)\", status: \"\(status.status.rawValue)\" }")
        }

        sendToTerminal(client, pane1, "printf '\\n\\033[1;36m─── Try it yourself ───\\033[0m\\n'\n")
        sendToTerminal(client, pane1, "printf 'Run: \\033[1mclaude\\033[0m to start an AI session with boo MCP tools\\n'\n")

        await registry.remove(peerID: id1)
        await registry.remove(peerID: id2)
        await registry.remove(peerID: id3)

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
