import Testing
import Foundation
@testable import BooCore

@Suite("GhosttyClient")
struct GhosttyClientTests {

    @Test("listTerminals returns terminal array")
    func listTerminals() throws {
        let (client, sock) = try makeClientWithMock(terminals: [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp", focused: true, columns: 80, rows: 24),
            MockTerminal(id: "t2", title: "vim", workingDirectory: "/home", focused: false, columns: 120, rows: 40),
        ])
        defer { sock.stop() }

        let terminals = try client.listTerminals()
        #expect(terminals.count == 2)
        #expect(terminals[0].id == "t1")
        #expect(terminals[0].title == "zsh")
        #expect(terminals[0].focused == true)
        #expect(terminals[0].columns == 80)
        #expect(terminals[1].id == "t2")
        #expect(terminals[1].focused == false)
    }

    @Test("listTerminals returns empty array when no terminals")
    func listTerminalsEmpty() throws {
        let (client, sock) = try makeClientWithMock(terminals: [])
        defer { sock.stop() }

        let terminals = try client.listTerminals()
        #expect(terminals.isEmpty)
    }

    @Test("sendText succeeds for valid terminal")
    func sendText() throws {
        let (client, sock) = try makeClientWithMock(terminals: [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp", focused: true, columns: 80, rows: 24),
        ])
        defer { sock.stop() }

        try client.sendText(terminalID: "t1", text: "echo hello\r")

        let sent = sock.sentTexts
        #expect(sent.count == 1)
        #expect(sent[0].text == "echo hello\r")
    }

    @Test("sendText throws for unknown terminal")
    func sendTextUnknownTerminal() throws {
        let (client, sock) = try makeClientWithMock(terminals: [])
        defer { sock.stop() }

        #expect(throws: GhosttyClientError.self) {
            try client.sendText(terminalID: "nonexistent", text: "hello")
        }
    }

    @Test("readScreen returns lines")
    func readScreen() throws {
        let (client, sock) = try makeClientWithMock(terminals: [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp", focused: true, columns: 80, rows: 24,
                         screenLines: ["$ whoami", "user", "$ "]),
        ])
        defer { sock.stop() }

        let lines = try client.readScreen(terminalID: "t1")
        #expect(lines == ["$ whoami", "user", "$ "])
    }

    @Test("getTerminalInfo returns terminal details")
    func getTerminalInfo() throws {
        let (client, sock) = try makeClientWithMock(terminals: [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/Users/me", focused: true, columns: 132, rows: 43),
        ])
        defer { sock.stop() }

        let info = try client.getTerminalInfo(terminalID: "t1")
        #expect(info.id == "t1")
        #expect(info.title == "zsh")
        #expect(info.workingDirectory == "/Users/me")
        #expect(info.columns == 132)
        #expect(info.rows == 43)
    }

    @Test("Throws on connection to nonexistent socket")
    func throwsOnBadSocket() {
        let client = GhosttyClient(socketPath: "/tmp/nonexistent-boo-sock.sock")
        #expect(throws: GhosttyClientError.self) {
            try client.listTerminals()
        }
    }

    private func makeClientWithMock(
        terminals: [MockTerminal]
    ) throws -> (GhosttyClient, MockGhosttySocket) {
        let sock = MockGhosttySocket()
        sock.terminals = terminals
        let path = "/tmp/boo-test-gc-\(ProcessInfo.processInfo.processIdentifier)-\(Int.random(in: 0...99999)).sock"
        try sock.start(path: path)
        let client = GhosttyClient(socketPath: path)
        return (client, sock)
    }
}
