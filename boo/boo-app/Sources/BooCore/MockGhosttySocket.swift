import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// A terminal description for MockGhosttySocket.
public struct MockTerminal: Sendable {
    public let id: String
    public let title: String
    public let workingDirectory: String
    public let focused: Bool
    public let columns: Int
    public let rows: Int
    public var screenLines: [String]

    public init(id: String, title: String, workingDirectory: String, focused: Bool,
                columns: Int, rows: Int, screenLines: [String] = []) {
        self.id = id
        self.title = title
        self.workingDirectory = workingDirectory
        self.focused = focused
        self.columns = columns
        self.rows = rows
        self.screenLines = screenLines
    }
}

/// Records a send_text call.
public struct SentText: Sendable {
    public let terminalID: String
    public let text: String
}

/// A mock Ghostty socket server for testing. Listens on a Unix socket
/// and responds to JSON-RPC methods like Ghostty would.
/// Not an actor — uses a lock to avoid actor-reentrancy deadlocks in tests.
public final class MockGhosttySocket: @unchecked Sendable {
    public var terminals: [MockTerminal] = []
    private let lock = NSLock()
    private var _sentTexts: [SentText] = []
    public var sentTexts: [SentText] {
        lock.lock()
        defer { lock.unlock() }
        return _sentTexts
    }
    private var serverFD: Int32 = -1
    private var socketPath: String = ""
    private var stopPipe: (read: Int32, write: Int32) = (-1, -1)
    private var thread: Thread?

    public init() {}

    public func start(path: String) throws {
        socketPath = path
        unlink(path)

        serverFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard serverFD >= 0 else { throw MockSocketError.bindFailed }

        // Non-blocking for poll-based accept
        let flags = fcntl(serverFD, F_GETFL)
        _ = fcntl(serverFD, F_SETFL, flags | O_NONBLOCK)

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            path.withCString { cstr in
                _ = memcpy(ptr, cstr, min(path.utf8.count, 104))
            }
        }

        let bindResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.bind(serverFD, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bindResult == 0 else {
            Darwin.close(serverFD)
            throw MockSocketError.bindFailed
        }

        guard listen(serverFD, 5) == 0 else {
            Darwin.close(serverFD)
            throw MockSocketError.listenFailed
        }

        // Self-pipe for clean shutdown
        var pipeFDs: [Int32] = [0, 0]
        _ = pipe(&pipeFDs)
        stopPipe = (read: pipeFDs[0], write: pipeFDs[1])

        let fd = serverFD
        let pipeReadFD = stopPipe.read
        let terminalsCopy = terminals

        // Run accept loop on a plain thread (no actor involvement)
        thread = Thread {
            while true {
                var fds = [
                    pollfd(fd: fd, events: Int16(POLLIN), revents: 0),
                    pollfd(fd: pipeReadFD, events: Int16(POLLIN), revents: 0),
                ]
                let ready = poll(&fds, 2, 200)
                if ready < 0 { break }
                if fds[1].revents & Int16(POLLIN) != 0 { break }

                if fds[0].revents & Int16(POLLIN) != 0 {
                    let clientFD = accept(fd, nil, nil)
                    if clientFD < 0 { continue }

                    var data = Data()
                    var buffer = [UInt8](repeating: 0, count: 4096)
                    let n = recv(clientFD, &buffer, buffer.count, 0)
                    if n > 0 {
                        data.append(contentsOf: buffer[..<n])
                    }

                    if let responseData = Self.handleRequest(data, terminals: terminalsCopy, appendSentText: { st in
                        self.lock.lock()
                        self._sentTexts.append(st)
                        self.lock.unlock()
                    }) {
                        _ = responseData.withUnsafeBytes { buf in
                            send(clientFD, buf.baseAddress!, buf.count, 0)
                        }
                    }
                    Darwin.close(clientFD)
                }
            }
        }
        thread?.start()
    }

    public func stop() {
        var byte: UInt8 = 1
        _ = Darwin.write(stopPipe.write, &byte, 1)
        // Give thread time to exit
        Thread.sleep(forTimeInterval: 0.3)

        if serverFD >= 0 {
            Darwin.close(serverFD)
            serverFD = -1
        }
        Darwin.close(stopPipe.read)
        Darwin.close(stopPipe.write)
        stopPipe = (-1, -1)
        unlink(socketPath)
    }

    private static func handleRequest(
        _ data: Data, terminals: [MockTerminal],
        appendSentText: (SentText) -> Void
    ) -> Data? {
        guard let str = String(data: data, encoding: .utf8),
              let bodyRange = str.range(of: "\r\n\r\n") else {
            return nil
        }

        let bodyStr = String(str[bodyRange.upperBound...])
        guard let bodyData = bodyStr.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: bodyData) as? [String: Any] else {
            return nil
        }

        let method = json["method"] as? String ?? ""
        let id = json["id"]
        let params = json["params"] as? [String: Any]

        let result: Any
        var errorObj: [String: Any]? = nil

        switch method {
        case "list_terminals":
            result = terminals.map { t -> [String: Any] in
                [
                    "id": t.id, "title": t.title,
                    "working_directory": t.workingDirectory,
                    "focused": t.focused, "columns": t.columns, "rows": t.rows,
                ]
            }

        case "send_text":
            let termID = params?["terminal_id"] as? String ?? ""
            let text = params?["text"] as? String ?? ""
            if terminals.contains(where: { $0.id == termID }) {
                appendSentText(SentText(terminalID: termID, text: text))
                result = ["ok": true]
            } else {
                errorObj = ["code": -32001, "message": "Terminal not found"]
                result = NSNull()
            }

        case "read_screen":
            let termID = params?["terminal_id"] as? String ?? ""
            if let term = terminals.first(where: { $0.id == termID }) {
                result = ["lines": term.screenLines]
            } else {
                errorObj = ["code": -32001, "message": "Terminal not found"]
                result = NSNull()
            }

        case "get_terminal_info":
            let termID = params?["terminal_id"] as? String ?? ""
            if let t = terminals.first(where: { $0.id == termID }) {
                result = [
                    "id": t.id, "title": t.title,
                    "working_directory": t.workingDirectory,
                    "focused": t.focused, "columns": t.columns, "rows": t.rows,
                ] as [String: Any]
            } else {
                errorObj = ["code": -32001, "message": "Terminal not found"]
                result = NSNull()
            }

        case "send_key":
            let termID = params?["terminal_id"] as? String ?? ""
            if terminals.contains(where: { $0.id == termID }) {
                result = ["ok": true]
            } else {
                errorObj = ["code": -32001, "message": "Terminal not found"]
                result = NSNull()
            }

        default:
            errorObj = ["code": -32601, "message": "Method not found"]
            result = NSNull()
        }

        var response: [String: Any] = ["jsonrpc": "2.0"]
        if let id { response["id"] = id }
        if let errorObj {
            response["error"] = errorObj
        } else {
            response["result"] = result
        }

        guard let responseBody = try? JSONSerialization.data(withJSONObject: response),
              let bodyString = String(data: responseBody, encoding: .utf8) else {
            return nil
        }

        let httpResponse = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(responseBody.count)\r\n\r\n\(bodyString)"
        return Data(httpResponse.utf8)
    }
}

enum MockSocketError: Error {
    case bindFailed
    case listenFailed
}
