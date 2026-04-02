import Testing
import Foundation
@testable import BooCore

/// Integration tests that require a real Ghostty socket.
/// Set BOO_GHOSTTY_SOCKET env var to run (e.g., /tmp/ghostty-test.sock).
@Suite("Integration", .enabled(if: ProcessInfo.processInfo.environment["BOO_GHOSTTY_SOCKET"] != nil))
struct IntegrationTests {

    var socketPath: String {
        ProcessInfo.processInfo.environment["BOO_GHOSTTY_SOCKET"]!
    }

    @Test("list_terminals returns real terminal data")
    func listTerminals() throws {
        let client = GhosttyClient(socketPath: socketPath)
        let terminals = try client.listTerminals()

        #expect(!terminals.isEmpty, "Expected at least one terminal")
        let first = terminals[0]
        #expect(!first.id.isEmpty)
        #expect(!first.title.isEmpty)
        #expect(first.columns > 0)
        #expect(first.rows > 0)
    }

    @Test("send_text + read_screen round-trip")
    func sendTextReadScreen() throws {
        let client = GhosttyClient(socketPath: socketPath)
        let terminals = try client.listTerminals()
        guard let term = terminals.first else {
            Issue.record("No terminals available")
            return
        }

        // Send a unique marker
        let marker = "boo-test-\(Int.random(in: 10000...99999))"
        try client.sendText(terminalID: term.id, text: "echo \(marker)\r")

        // Brief delay for terminal to process
        Thread.sleep(forTimeInterval: 0.5)

        let lines = try client.readScreen(terminalID: term.id)
        #expect(!lines.isEmpty, "Screen should have content")
        let content = lines.joined()
        #expect(content.contains(marker), "Screen should contain the marker we sent")
    }

    @Test("2-peer register, send, receive flow")
    func peerMessaging() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)

        let idA = await registry.register(name: "test-agent-a", role: "sender")
        let idB = await registry.register(name: "test-agent-b", role: "receiver")

        try await relay.send(from: idA, to: "test-agent-b", content: "integration test message")
        let messages = await relay.receive(peerID: idB)

        #expect(messages.count == 1)
        #expect(messages[0].from == "test-agent-a")
        #expect(messages[0].content == "integration test message")
    }

    @Test("Broadcast delivery to all peers")
    func broadcastDelivery() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)

        let idA = await registry.register(name: "broadcast-a", role: nil)
        let idB = await registry.register(name: "broadcast-b", role: nil)
        let idC = await registry.register(name: "broadcast-c", role: nil)

        let delivered = try await relay.broadcast(from: idA, content: "broadcast test")
        #expect(delivered.count == 2)

        let msgsB = await relay.receive(peerID: idB)
        let msgsC = await relay.receive(peerID: idC)
        #expect(msgsB.count == 1)
        #expect(msgsC.count == 1)
        #expect(msgsB[0].content == "broadcast test")
    }

    @Test("AppState connects to real socket and discovers terminals")
    @MainActor func appStateIntegration() async {
        let state = AppState()
        await state.connect(socketPath: socketPath)

        #expect(state.connectionStatus == .connected)
        #expect(!state.discoveredTerminals.isEmpty)
    }
}

// MARK: - SSH Tunnel Integration (requires BOO_SSH_HOST env var)

/// Tests that exercise the real SSH tunnel + RemoteProvisioner flow.
/// Set BOO_SSH_HOST=localhost to run against the local machine.
@Suite("SSH Integration", .enabled(if: ProcessInfo.processInfo.environment["BOO_SSH_HOST"] != nil))
struct SSHIntegrationTests {

    var sshHost: String {
        ProcessInfo.processInfo.environment["BOO_SSH_HOST"]!
    }

    @Test("SSH connection test via RemoteProvisioner")
    func sshConnection() async {
        let provisioner = RemoteProvisioner()
        let host = SSHHost(id: sshHost, alias: sshHost, hostname: sshHost, user: NSUserName())
        let connected = await provisioner.testConnection(host: host)
        #expect(connected, "SSH connection to \(sshHost) should succeed")
    }

    @Test("Detect Ghostty on remote host")
    func detectGhostty() async {
        let provisioner = RemoteProvisioner()
        let host = SSHHost(id: sshHost, alias: sshHost, hostname: sshHost, user: NSUserName())
        let (installed, hasSocket, variant, socketPath) = await provisioner.detectRemoteGhostty(host: host)
        #expect(installed, "Ghostty should be installed on \(sshHost)")
        #expect(hasSocket, "Ghostty should have an active socket")
        #expect(variant != nil)
        #expect(socketPath != nil, "Should detect active socket path")
    }

    @Test("Full provisioning pipeline creates working tunnel")
    func fullProvision() async throws {
        let provisioner = RemoteProvisioner()
        let tunnelManager = SSHTunnelManager(reconnectDelay: 1, maxReconnectAttempts: 2)
        let host = SSHHost(id: sshHost, alias: sshHost, hostname: sshHost, user: NSUserName())

        var steps: [StepResult] = []
        for await result in await provisioner.provision(
            host: host,
            tunnelManager: tunnelManager,
            releaseURL: RemoteProvisioner.defaultReleaseURL
        ) {
            steps.append(result)
        }

        // Should have completed all the way through
        let completedSteps = steps.compactMap { result -> ProvisionStep? in
            if case .completed(let step, _) = result { return step }
            return nil
        }
        let failedSteps = steps.compactMap { result -> (ProvisionStep, String)? in
            if case .failed(let step, let msg) = result { return (step, msg) }
            return nil
        }

        if let (step, msg) = failedSteps.first {
            Issue.record("Provisioning failed at \(step.rawValue): \(msg)")
        }

        #expect(completedSteps.contains(.connecting), "Should complete connecting step")
        #expect(completedSteps.contains(.detecting), "Should complete detecting step")
        #expect(completedSteps.contains(.tunnel), "Should complete tunnel step")
        #expect(completedSteps.contains(.discovered), "Should complete discovered step")

        // Validate tunnel socket is usable
        let localSocket = await tunnelManager.localSocket(host.alias)
        #expect(localSocket != nil, "Tunnel should have a local socket")

        if let path = localSocket {
            let client = GhosttyClient(socketPath: path)
            let terminals = try client.listTerminals()
            #expect(!terminals.isEmpty, "Should discover terminals via tunnel")
        }

        // Cleanup
        await tunnelManager.stopAll()
    }
}

// MARK: - MCP → AppState E2E (no socket needed)

@Suite("MCP E2E")
struct MCPEndToEndTests {

    /// Create an isolated shared store for test use.
    private func testSharedStore() -> SharedPeerStore {
        SharedPeerStore(baseDir: NSTemporaryDirectory() + "boo-test-\(UUID().uuidString)")
    }

    @Test("Peers registered via MCP appear in AppState")
    @MainActor func mcpToAppState() async throws {
        let state = AppState()
        let server = MCPServer(registry: state.registry, machineName: "test-machine", socketPath: nil, sharedStore: testSharedStore())

        // Register 2 peers via MCP
        let respA = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice","role":"coder"}}}
        """)
        _ = try extractPeerID(respA)

        let respB = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"bob","role":"reviewer"}}}
        """)
        _ = try extractPeerID(respB)

        // Refresh AppState peers
        await state.refreshPeers()

        // Verify peers show up in AppState
        #expect(state.peers.count == 2)
        let names = Set(state.peers.map(\.name))
        #expect(names.contains("alice"))
        #expect(names.contains("bob"))

        // Verify machine name
        for peer in state.peers {
            #expect(peer.machine == "test-machine")
        }

        // Verify roles
        let alice = state.peers.first { $0.name == "alice" }!
        let bob = state.peers.first { $0.name == "bob" }!
        #expect(alice.role == "coder")
        #expect(bob.role == "reviewer")
        #expect(alice.status == .active)
        #expect(bob.status == .active)
    }

    @Test("Messages sent via MCP are receivable via MCP")
    @MainActor func mcpMessageRoundTrip() async throws {
        let state = AppState()
        let server = MCPServer(registry: state.registry, machineName: "test-machine", socketPath: nil, sharedStore: testSharedStore())

        // Register 2 peers
        let respA = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice"}}}
        """)
        let idA = try extractPeerID(respA)

        let respB = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"bob"}}}
        """)
        let idB = try extractPeerID(respB)

        // Send message alice → bob
        _ = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"send_message","arguments":{"peer_id":"\(idA)","to":"bob","content":"hello from alice"}}}
        """)

        // Receive as bob
        let recvResp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"receive_messages","arguments":{"peer_id":"\(idB)"}}}
        """)
        let text = try extractText(recvResp)
        let messages = try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [[String: Any]]
        #expect(messages.count == 1)
        #expect(messages[0]["from"] as? String == "alice")
        #expect(messages[0]["content"] as? String == "hello from alice")
    }

    @Test("Multi-machine peers group correctly")
    @MainActor func multiMachinePeers() async throws {
        let state = AppState()
        let store = testSharedStore()
        let serverA = MCPServer(registry: state.registry, machineName: "mac-mini", socketPath: nil, sharedStore: store)
        let serverB = MCPServer(registry: state.registry, machineName: "vps", socketPath: nil, sharedStore: store)

        // Register peers on different machines
        _ = try await serverA.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice","role":"coder"}}}
        """)
        _ = try await serverB.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"bob","role":"reviewer"}}}
        """)
        _ = try await serverA.handleMessage("""
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"charlie","role":"tester"}}}
        """)

        await state.refreshPeers()

        #expect(state.peers.count == 3)

        // Group by machine
        let macMiniPeers = state.peers.filter { $0.machine == "mac-mini" }
        let vpsPeers = state.peers.filter { $0.machine == "vps" }
        #expect(macMiniPeers.count == 2)
        #expect(vpsPeers.count == 1)
        #expect(vpsPeers[0].name == "bob")
    }

    // MARK: - Helpers

    private func extractText(_ response: String?) throws -> String {
        guard let response else { throw TestError.noResponse }
        let json = try JSONSerialization.jsonObject(with: Data(response.utf8)) as! [String: Any]
        let result = json["result"] as! [String: Any]
        let content = result["content"] as! [[String: Any]]
        return content[0]["text"] as! String
    }

    private func extractPeerID(_ response: String?) throws -> String {
        let text = try extractText(response)
        let parsed = try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [String: Any]
        return parsed["peer_id"] as! String
    }
}
