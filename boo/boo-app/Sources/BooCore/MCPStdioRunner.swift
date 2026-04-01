import Foundation

/// Runs the MCP server as a stdio process.
/// Reads newline-delimited JSON from stdin, writes responses to stdout.
public struct MCPStdioRunner: Sendable {
    private let server: MCPServer

    public init(server: MCPServer) {
        self.server = server
    }

    /// Run the stdio loop. Blocks until stdin closes (EOF).
    /// Must be called from an async context.
    public func run() async {
        let stdinFD = FileHandle.standardInput.fileDescriptor
        let stdoutFD = FileHandle.standardOutput.fileDescriptor

        while let line = readLineSync(fd: stdinFD) {
            do {
                if let response = try await server.processLine(line) {
                    if let data = response.data(using: .utf8) {
                        data.withUnsafeBytes { buf in
                            _ = Darwin.write(stdoutFD, buf.baseAddress!, buf.count)
                        }
                    }
                }
            } catch {
                let errResponse = """
                {"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"Internal error"}}

                """
                if let data = errResponse.data(using: .utf8) {
                    data.withUnsafeBytes { buf in
                        _ = Darwin.write(stdoutFD, buf.baseAddress!, buf.count)
                    }
                }
            }
        }
    }

    /// Read one line from a file descriptor (blocking, but called from async context).
    private func readLineSync(fd: Int32) -> String? {
        var buffer = Data()
        var byte: UInt8 = 0
        while true {
            let n = Darwin.read(fd, &byte, 1)
            if n <= 0 {
                return buffer.isEmpty ? nil : String(data: buffer, encoding: .utf8)
            }
            if byte == UInt8(ascii: "\n") {
                return String(data: buffer, encoding: .utf8)
            }
            buffer.append(byte)
        }
    }
}
