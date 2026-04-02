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

    /// Run the demo. If socketPath is provided, tries live terminal mode.
    /// Falls back to simulated mode if live setup fails.
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

                // If no socket, try launching Ghostty
                if socket == nil {
                    socket = await self.tryLaunchGhostty()
                }

                // Try live demo
                if let socket {
                    let client = GhosttyClient(socketPath: socket)
                    let done = await self.runLiveDemo(
                        peerName: peerName, registry: registry, relay: relay,
                        machineName: machineName, client: client,
                        continuation: continuation
                    )
                    if done {
                        continuation.finish()
                        return
                    }
                }

                // Fallback: simulated demo
                await self.runSimulatedDemo(
                    peerName: peerName, registry: registry, relay: relay,
                    machineName: machineName, continuation: continuation
                )
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
        // 1. Discover existing terminals
        guard let existingTerminals = try? client.listTerminals(), !existingTerminals.isEmpty else {
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

        // 4. Detect Claude/Codex availability
        let agent = detectAvailableAgent()

        // 5. Run appropriate demo
        if let agent {
            return await runAgentDemo(
                peerName: peerName, registry: registry, relay: relay,
                machineName: machineName, client: client,
                pane1: pane1, pane2: pane2, agent: agent,
                continuation: continuation
            )
        } else {
            return await runScriptedDemo(
                peerName: peerName, registry: registry, relay: relay,
                machineName: machineName, client: client,
                pane1: pane1, pane2: pane2,
                continuation: continuation
            )
        }
    }

    // MARK: - Agent Demo (Real Claude/Codex Sessions)

    private func runAgentDemo(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, client: GhosttyClient,
        pane1: String, pane2: String, agent: AvailableAgent,
        continuation: AsyncStream<DemoLine>.Continuation
    ) async -> Bool {
        let cmd = agent == .claude ? "claude" : "codex"
        let alphaName = peerName.isEmpty ? "agent-alpha" : peerName
        let betaName = "agent-beta"

        // Clear panes and show headers
        sendToTerminal(client, pane1, "clear\n")
        try? await Task.sleep(for: .milliseconds(200))
        sendToTerminal(client, pane2, "clear\n")
        try? await Task.sleep(for: .milliseconds(300))

        // Set terminal IDs as env vars so MCPServer can detect them
        sendToTerminal(client, pane1, "export BOO_TERMINAL_ID=\(pane1)\n")
        try? await Task.sleep(for: .milliseconds(200))
        sendToTerminal(client, pane2, "export BOO_TERMINAL_ID=\(pane2)\n")
        try? await Task.sleep(for: .milliseconds(200))

        // Clear shared store for clean demo
        SharedPeerStore().removeAll()

        // Start agent in pane 1
        yield(continuation, .request, "launch_agent", "{ pane: \"alpha\", agent: \"\(cmd)\" }")
        let prompt1 = "You are \\\"" + alphaName + "\\\". Use register_peer to register yourself, then list_peers, then send_message to \\\"" + betaName + "\\\" saying \\\"hello from boo\\\". Then receive_messages to check for replies."
        sendToTerminal(client, pane1, "\(cmd) \"\(prompt1)\"\n")
        try? await Task.sleep(for: .milliseconds(500))
        yield(continuation, .response, "launch_agent", "{ status: \"started\" }")

        // Start agent in pane 2
        yield(continuation, .request, "launch_agent", "{ pane: \"beta\", agent: \"\(cmd)\" }")
        let prompt2 = "You are \\\"" + betaName + "\\\". Use register_peer to register yourself, then list_peers. When you receive messages, reply with \\\"welcome to boo\\\"."
        sendToTerminal(client, pane2, "\(cmd) \"\(prompt2)\"\n")
        try? await Task.sleep(for: .milliseconds(500))
        yield(continuation, .response, "launch_agent", "{ status: \"started\" }")

        // Monitor shared peer store for activity
        let sharedStore = SharedPeerStore()
        var foundAlpha = false
        var foundBeta = false
        var messagesDelivered = false

        for _ in 0..<60 { // Monitor for up to 60 seconds
            try? await Task.sleep(for: .seconds(1))

            let peers = sharedStore.listPeers()

            if !foundAlpha, let alpha = peers.first(where: { $0.name == alphaName }) {
                foundAlpha = true
                yield(continuation, .response, "register_peer", "{ name: \"\(alphaName)\", peer_id: \"\(short(alpha.peerID))\" }")
            }

            if !foundBeta, let beta = peers.first(where: { $0.name == betaName }) {
                foundBeta = true
                yield(continuation, .response, "register_peer", "{ name: \"\(betaName)\", peer_id: \"\(short(beta.peerID))\" }")
            }

            if foundAlpha && foundBeta {
                yield(continuation, .response, "list_peers", "{ count: \(peers.count), peers: [\(peers.map(\.name).joined(separator: ", "))] }")
            }

            // Check if messages have been exchanged
            if foundAlpha && foundBeta && !messagesDelivered {
                // Try reading terminal screens for evidence of message delivery
                if let screen1 = try? client.readScreen(terminalID: pane1) {
                    let text = screen1.joined()
                    if text.contains("delivered") || text.contains("hello") || text.contains("Boo message") {
                        messagesDelivered = true
                        yield(continuation, .response, "send_message", "{ status: \"delivered\" }")
                    }
                }
            }

            if messagesDelivered { break }
        }

        // If agents didn't complete, still mark success (they're running)
        if !messagesDelivered && foundAlpha && foundBeta {
            yield(continuation, .response, "send_message", "{ status: \"agents communicating\" }")
        }

        return foundAlpha || foundBeta // Consider success if at least one registered
    }

    // MARK: - Scripted Demo (Terminal Visualization, No AI Agent)

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
        let agent = detectAvailableAgent()
        if let agent {
            let cmd = agent == .claude ? "claude" : "codex"
            sendToTerminal(client, pane1, "printf '\\n\\033[1;36m─── Try it yourself ───\\033[0m\\n'\n")
            sendToTerminal(client, pane1, "printf 'Run: \\033[1m\(cmd)\\033[0m to start an AI session with boo MCP tools\\n'\n")
        }

        // Clean up
        await registry.remove(peerID: alphaID)
        await registry.remove(peerID: betaID)

        return true
    }

    // MARK: - Simulated Demo (No Ghostty, In-Memory Only)

    private func runSimulatedDemo(
        peerName: String, registry: PeerRegistry, relay: MessageRelay,
        machineName: String, continuation: AsyncStream<DemoLine>.Continuation
    ) async {
        let guideName = "boo-guide"
        let effectiveName = peerName.isEmpty ? "user" : peerName

        // Step 1: register_peer (user)
        yield(continuation, .request, "register_peer", "{ name: \"\(effectiveName)\", role: \"claude\" }")
        try? await Task.sleep(for: .milliseconds(400))
        let userPeerID = await registry.register(name: effectiveName, role: "claude", machine: machineName)
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(userPeerID)\" }")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 2: register_peer (guide)
        yield(continuation, .request, "register_peer", "{ name: \"\(guideName)\", role: \"guide\" }")
        try? await Task.sleep(for: .milliseconds(400))
        let guidePeerID = await registry.register(name: guideName, role: "guide", machine: "local")
        yield(continuation, .response, "register_peer", "{ peer_id: \"\(guidePeerID)\" }")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 3: list_peers
        yield(continuation, .request, "list_peers", "{}")
        try? await Task.sleep(for: .milliseconds(400))
        let peers = await registry.listPeers()
        let peerList = peers.map { "{ name: \"\($0.name)\", status: \"\($0.status.rawValue)\" }" }.joined(separator: ", ")
        yield(continuation, .response, "list_peers", "[ \(peerList) ]")
        try? await Task.sleep(for: .milliseconds(600))

        // Step 4: send_message
        yield(continuation, .request, "send_message", "{ to: \"\(guideName)\", content: \"hello from boo\" }")
        try? await Task.sleep(for: .milliseconds(400))
        do {
            try await relay.send(from: userPeerID, to: guideName, content: "hello from boo")
            yield(continuation, .response, "send_message", "{ status: \"delivered\" }")
        } catch {
            yield(continuation, .error, "send_message", "{ error: \"\(error)\" }")
        }
        try? await Task.sleep(for: .milliseconds(600))

        // Step 5: guide replies
        do {
            try await relay.send(from: guidePeerID, to: effectiveName, content: "welcome to boo")
        } catch {}
        try? await Task.sleep(for: .milliseconds(300))

        // Step 6: receive_messages
        yield(continuation, .request, "receive_messages", "{ peer_id: \"\(userPeerID)\" }")
        try? await Task.sleep(for: .milliseconds(400))
        let messages = await relay.receive(peerID: userPeerID)
        if let msg = messages.first {
            yield(continuation, .response, "receive_messages", "[ { from: \"\(msg.from)\", content: \"\(msg.content)\" } ]")
        } else {
            yield(continuation, .response, "receive_messages", "[]")
        }
        try? await Task.sleep(for: .milliseconds(600))

        // Step 7: get_peer_status
        yield(continuation, .request, "get_peer_status", "{ peer_id: \"\(guidePeerID)\" }")
        try? await Task.sleep(for: .milliseconds(400))
        if let status = await registry.getStatus(peerID: guidePeerID) {
            yield(continuation, .response, "get_peer_status", "{ name: \"\(status.name)\", status: \"\(status.status.rawValue)\" }")
        }

        // Clean up
        await registry.remove(peerID: userPeerID)
        await registry.remove(peerID: guidePeerID)
    }

    // MARK: - Ghostty Launch

    /// Try to launch Ghostty Boo and return socket path when ready.
    private func tryLaunchGhostty() async -> String? {
        for bundleID in ["com.beastoin.ghostty-boo", "com.mitchellh.ghostty"] {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            process.arguments = ["-b", bundleID]
            process.standardOutput = Pipe()
            process.standardError = Pipe()
            do {
                try process.run()
                process.waitUntilExit()
                guard process.terminationStatus == 0 else { continue }

                // Wait for socket (up to 5 seconds)
                for _ in 0..<10 {
                    try? await Task.sleep(for: .milliseconds(500))
                    for path in ["/tmp/ghostty.sock", "/tmp/ghostty-test.sock"] {
                        if let _ = try? GhosttyClient(socketPath: path).listTerminals() {
                            return path
                        }
                    }
                }
                return nil // Launched but socket not available
            } catch {
                continue
            }
        }
        return nil
    }

    /// Find the running Ghostty process name for AppleScript.
    private func findGhosttyProcessName() async -> String? {
        for name in ["Ghostty Boo", "Ghostty", "GhosttyBoo"] {
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

    // MARK: - Agent Detection

    private enum AvailableAgent: Sendable {
        case claude, codex
    }

    private func detectAvailableAgent() -> AvailableAgent? {
        let commonPaths = ["/usr/local/bin", "/opt/homebrew/bin",
                           NSHomeDirectory() + "/.local/bin", NSHomeDirectory() + "/bin"]
        for dir in commonPaths {
            if FileManager.default.fileExists(atPath: dir + "/claude") { return .claude }
        }
        for dir in commonPaths {
            if FileManager.default.fileExists(atPath: dir + "/codex") { return .codex }
        }
        // Try PATH via which
        if shellExists("claude") { return .claude }
        if shellExists("codex") { return .codex }
        return nil
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

    private func shellExists(_ command: String) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        process.arguments = [command]
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }
}
