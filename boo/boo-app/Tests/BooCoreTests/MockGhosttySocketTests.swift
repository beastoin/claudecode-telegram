import Testing
import Foundation
@testable import BooCore

@Suite("MockGhosttySocket")
struct MockGhosttySocketTests {

    @Test("Starts and stops without error")
    func startsAndStops() throws {
        let sock = MockGhosttySocket()
        let path = "/tmp/boo-test-\(ProcessInfo.processInfo.processIdentifier).sock"
        try sock.start(path: path)
        #expect(FileManager.default.fileExists(atPath: path))
        sock.stop()
        #expect(!FileManager.default.fileExists(atPath: path))
    }

    @Test("Responds to list_terminals with configured terminals")
    func respondsToListTerminals() async throws {
        let sock = MockGhosttySocket()
        let path = "/tmp/boo-test-\(ProcessInfo.processInfo.processIdentifier)-lt.sock"
        sock.terminals = [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp", focused: true, columns: 80, rows: 24),
            MockTerminal(id: "t2", title: "vim", workingDirectory: "/home", focused: false, columns: 120, rows: 40),
        ]
        try sock.start(path: path)
        defer { sock.stop() }

        let resp = try await sendRPC(path: path, method: "list_terminals", params: nil, id: 1)
        #expect(resp.error == nil)
        if case .array(let arr) = resp.result {
            #expect(arr.count == 2)
        } else {
            Issue.record("Expected array result, got \(String(describing: resp.result))")
        }
    }

    @Test("Responds to send_text with ok")
    func respondsToSendText() async throws {
        let sock = MockGhosttySocket()
        let path = "/tmp/boo-test-\(ProcessInfo.processInfo.processIdentifier)-st.sock"
        sock.terminals = [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp", focused: true, columns: 80, rows: 24),
        ]
        try sock.start(path: path)
        defer { sock.stop() }

        let params: JSONValue = .object([
            "terminal_id": .string("t1"),
            "text": .string("echo hello\r"),
        ])
        let resp = try await sendRPC(path: path, method: "send_text", params: params, id: 2)
        #expect(resp.error == nil)
        if case .object(let obj) = resp.result {
            #expect(obj["ok"] == .bool(true))
        } else {
            Issue.record("Expected {ok: true}")
        }
        let sent = sock.sentTexts
        #expect(sent.count == 1)
        #expect(sent[0].terminalID == "t1")
        #expect(sent[0].text == "echo hello\r")
    }

    @Test("Returns error for unknown method")
    func returnsErrorForUnknownMethod() async throws {
        let sock = MockGhosttySocket()
        let path = "/tmp/boo-test-\(ProcessInfo.processInfo.processIdentifier)-um.sock"
        try sock.start(path: path)
        defer { sock.stop() }

        let resp = try await sendRPC(path: path, method: "nonexistent", params: nil, id: 3)
        #expect(resp.error != nil)
        #expect(resp.error?.code == -32601)
    }

    @Test("Returns error for send_text to unknown terminal")
    func returnsErrorForUnknownTerminal() async throws {
        let sock = MockGhosttySocket()
        let path = "/tmp/boo-test-\(ProcessInfo.processInfo.processIdentifier)-ut.sock"
        try sock.start(path: path)
        defer { sock.stop() }

        let params: JSONValue = .object([
            "terminal_id": .string("nonexistent"),
            "text": .string("hello"),
        ])
        let resp = try await sendRPC(path: path, method: "send_text", params: params, id: 4)
        #expect(resp.error != nil)
    }

    @Test("Responds to read_screen with lines")
    func respondsToReadScreen() async throws {
        let sock = MockGhosttySocket()
        let path = "/tmp/boo-test-\(ProcessInfo.processInfo.processIdentifier)-rs.sock"
        sock.terminals = [
            MockTerminal(id: "t1", title: "zsh", workingDirectory: "/tmp", focused: true, columns: 80, rows: 24,
                         screenLines: ["$ echo hello", "hello", "$ "]),
        ]
        try sock.start(path: path)
        defer { sock.stop() }

        let params: JSONValue = .object(["terminal_id": .string("t1")])
        let resp = try await sendRPC(path: path, method: "read_screen", params: params, id: 5)
        #expect(resp.error == nil)
        if case .object(let obj) = resp.result, case .array(let lines) = obj["lines"] {
            #expect(lines.count == 3)
            #expect(lines[0] == .string("$ echo hello"))
        } else {
            Issue.record("Expected {lines: [...]}")
        }
    }

    private func sendRPC(path: String, method: String, params: JSONValue?, id: Int) async throws -> JSONRPCResponse {
        let rpc = JSONRPCRequest(id: .int(id), method: method, params: params)
        let requestData = try HTTPMessage.buildRequest(rpc: rpc)

        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global().async {
                do {
                    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
                    guard fd >= 0 else { throw SocketError.connectFailed }
                    defer { close(fd) }

                    var addr = sockaddr_un()
                    addr.sun_family = sa_family_t(AF_UNIX)
                    withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
                        path.withCString { cstr in
                            _ = memcpy(ptr, cstr, min(path.utf8.count, 104))
                        }
                    }

                    let connectResult = withUnsafePointer(to: &addr) { ptr in
                        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                            connect(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
                        }
                    }
                    guard connectResult == 0 else { throw SocketError.connectFailed }

                    _ = requestData.withUnsafeBytes { buf in
                        send(fd, buf.baseAddress!, buf.count, 0)
                    }
                    shutdown(fd, SHUT_WR)

                    var responseData = Data()
                    var buffer = [UInt8](repeating: 0, count: 4096)
                    while true {
                        let n = recv(fd, &buffer, buffer.count, 0)
                        if n <= 0 { break }
                        responseData.append(contentsOf: buffer[..<n])
                    }

                    let resp = try HTTPMessage.parseResponse(responseData)
                    continuation.resume(returning: resp)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }
}

enum SocketError: Error {
    case connectFailed
}
