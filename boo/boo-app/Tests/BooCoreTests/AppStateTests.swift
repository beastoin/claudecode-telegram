import Testing
import Foundation
@testable import BooCore

@Suite("AppState")
struct AppStateTests {

    @Test("Initial state is disconnected with empty lists")
    @MainActor func initialState() {
        let state = AppState()
        #expect(state.connectionStatus == .disconnected)
        #expect(state.discoveredTerminals.isEmpty)
        #expect(state.peers.isEmpty)
    }

    @Test("Connect to mock socket discovers terminals")
    @MainActor func connectDiscoverTerminals() async throws {
        let sock = MockGhosttySocket()
        sock.terminals = [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp",
                         focused: true, columns: 80, rows: 24),
        ]
        let path = "/tmp/boo-test-as-\(ProcessInfo.processInfo.processIdentifier).sock"
        try sock.start(path: path)
        defer { sock.stop() }

        let state = AppState()
        await state.connect(socketPath: path)

        #expect(state.connectionStatus == .connected)
        #expect(state.discoveredTerminals.count == 1)
        #expect(state.discoveredTerminals[0].id == "t1")
    }

    @Test("Connect to bad socket sets error state")
    @MainActor func connectBadSocket() async {
        let state = AppState()
        await state.connect(socketPath: "/tmp/nonexistent-boo.sock")

        #expect(state.connectionStatus == .error)
    }

    @Test("Register peer and list")
    @MainActor func registerAndListPeers() async {
        let state = AppState()
        let id = await state.registerPeer(name: "agent-a", role: "coder")
        #expect(!id.isEmpty)

        await state.refreshPeers()
        #expect(state.peers.count == 1)
        #expect(state.peers[0].name == "agent-a")
    }

    @Test("Send and receive message")
    @MainActor func sendReceiveMessage() async throws {
        let state = AppState()
        let idA = await state.registerPeer(name: "agent-a", role: nil)
        let idB = await state.registerPeer(name: "agent-b", role: nil)

        try await state.sendMessage(from: idA, to: "agent-b", content: "hello")

        let messages = await state.receiveMessages(peerID: idB)
        #expect(messages.count == 1)
        #expect(messages[0].content == "hello")
    }

    @Test("Broadcast message")
    @MainActor func broadcastMessage() async throws {
        let state = AppState()
        let idA = await state.registerPeer(name: "agent-a", role: nil)
        let _ = await state.registerPeer(name: "agent-b", role: nil)

        let delivered = try await state.broadcast(from: idA, content: "broadcast")
        #expect(delivered.contains("agent-b"))
    }

    @Test("Refresh terminals updates list")
    @MainActor func refreshTerminals() async throws {
        let sock = MockGhosttySocket()
        sock.terminals = [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp",
                         focused: true, columns: 80, rows: 24),
        ]
        let path = "/tmp/boo-test-as2-\(ProcessInfo.processInfo.processIdentifier).sock"
        try sock.start(path: path)
        defer { sock.stop() }

        let state = AppState()
        await state.connect(socketPath: path)
        #expect(state.discoveredTerminals.count == 1)

        // Add another terminal to mock
        sock.terminals.append(
            MockTerminal(id: "t2", title: "vim", workingDirectory: "/home",
                         focused: false, columns: 120, rows: 40)
        )

        // Reconnect to pick up new terminals
        // (mock captures terminals at start time, so we restart)
        sock.stop()
        try sock.start(path: path)
        await state.refreshTerminals()

        #expect(state.discoveredTerminals.count == 2)
    }
}
