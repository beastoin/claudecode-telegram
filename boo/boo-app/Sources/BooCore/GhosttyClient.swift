import Foundation

/// Errors from GhosttyClient operations.
public enum GhosttyClientError: Error, Sendable {
    case connectionFailed
    case sendFailed
    case receiveFailed
    case rpcError(code: Int, message: String)
    case unexpectedResult
}

/// Terminal info returned by Ghostty socket.
public struct TerminalInfo: Sendable, Equatable {
    public let id: String
    public let title: String
    public let workingDirectory: String
    public let focused: Bool
    public let columns: Int
    public let rows: Int
}

/// A client for the Ghostty Unix socket JSON-RPC API.
/// Sendable struct — each call opens a new socket connection.
public struct GhosttyClient: Sendable {
    public let socketPath: String
    private var nextID: Int { Int.random(in: 1...999999) }

    public init(socketPath: String) {
        self.socketPath = socketPath
    }

    /// Lists all terminals.
    public func listTerminals() throws -> [TerminalInfo] {
        let resp = try call(method: "list_terminals", params: nil)
        if resp.error != nil { throw rpcError(resp) }

        // Real Ghostty wraps in {"terminals": [...]}, mock returns [...] directly
        let arr: [JSONValue]
        if case .object(let obj) = resp.result, case .array(let terminals) = obj["terminals"] {
            arr = terminals
        } else if case .array(let terminals) = resp.result {
            arr = terminals
        } else {
            return []
        }
        return arr.compactMap { parseTerminal($0) }
    }

    /// Sends text to a terminal.
    public func sendText(terminalID: String, text: String) throws {
        let params: JSONValue = .object([
            "terminal_id": .string(terminalID),
            "text": .string(text),
        ])
        let resp = try call(method: "send_text", params: params)
        if resp.error != nil { throw rpcError(resp) }
    }

    /// Reads screen content from a terminal.
    public func readScreen(terminalID: String) throws -> [String] {
        let params: JSONValue = .object(["terminal_id": .string(terminalID)])
        let resp = try call(method: "read_screen", params: params)
        if resp.error != nil { throw rpcError(resp) }
        guard case .object(let obj) = resp.result,
              case .array(let lines) = obj["lines"] else {
            throw GhosttyClientError.unexpectedResult
        }
        return lines.compactMap { if case .string(let s) = $0 { return s } else { return nil } }
    }

    /// Gets info for a single terminal.
    public func getTerminalInfo(terminalID: String) throws -> TerminalInfo {
        let params: JSONValue = .object(["terminal_id": .string(terminalID)])
        let resp = try call(method: "get_terminal_info", params: params)
        if resp.error != nil { throw rpcError(resp) }
        guard let info = resp.result.flatMap({ parseTerminal($0) }) else {
            throw GhosttyClientError.unexpectedResult
        }
        return info
    }

    // MARK: - Private

    private func call(method: String, params: JSONValue?) throws -> JSONRPCResponse {
        let rpc = JSONRPCRequest(id: .int(nextID), method: method, params: params)
        let requestData = try HTTPMessage.buildRequest(rpc: rpc)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw GhosttyClientError.connectionFailed }
        defer { close(fd) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            socketPath.withCString { cstr in
                _ = memcpy(ptr, cstr, min(socketPath.utf8.count, 104))
            }
        }

        let connectResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                connect(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connectResult == 0 else { throw GhosttyClientError.connectionFailed }

        let sent = requestData.withUnsafeBytes { buf in
            send(fd, buf.baseAddress!, buf.count, 0)
        }
        guard sent == requestData.count else { throw GhosttyClientError.sendFailed }

        // Signal we're done sending so server can process
        shutdown(fd, SHUT_WR)

        var responseData = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = recv(fd, &buffer, buffer.count, 0)
            if n <= 0 { break }
            responseData.append(contentsOf: buffer[..<n])
        }
        guard !responseData.isEmpty else { throw GhosttyClientError.receiveFailed }

        return try HTTPMessage.parseResponse(responseData)
    }

    private func parseTerminal(_ value: JSONValue) -> TerminalInfo? {
        guard case .object(let obj) = value,
              case .string(let id) = obj["id"],
              case .string(let title) = obj["title"] else { return nil }

        let wd: String
        if case .string(let w) = obj["working_directory"] { wd = w } else { wd = "" }
        let focused: Bool
        if case .bool(let f) = obj["focused"] { focused = f } else { focused = false }
        let cols: Int
        if case .int(let c) = obj["columns"] { cols = c } else { cols = 80 }
        let rows: Int
        if case .int(let r) = obj["rows"] { rows = r } else { rows = 24 }

        return TerminalInfo(id: id, title: title, workingDirectory: wd,
                            focused: focused, columns: cols, rows: rows)
    }

    private func rpcError(_ resp: JSONRPCResponse) -> GhosttyClientError {
        if let err = resp.error {
            return .rpcError(code: err.code, message: err.message)
        }
        return .unexpectedResult
    }
}
