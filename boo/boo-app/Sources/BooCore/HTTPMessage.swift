import Foundation

/// Errors from HTTP response parsing.
public enum HTTPParseError: Error, Sendable {
    case malformedResponse
    case missingBody
}

/// HTTP/1.1 message builder and parser for JSON-RPC over Unix sockets.
public enum HTTPMessage {

    /// Builds an HTTP/1.1 POST request from a JSON-RPC request.
    public static func buildRequest(rpc: JSONRPCRequest) throws -> Data {
        let body = try JSONEncoder().encode(rpc)
        var header = "POST / HTTP/1.1\r\n"
        header += "Content-Type: application/json\r\n"
        header += "Content-Length: \(body.count)\r\n"
        header += "Connection: close\r\n"
        header += "\r\n"
        var data = Data(header.utf8)
        data.append(body)
        return data
    }

    /// Parses an HTTP/1.1 response and extracts the JSON-RPC response body.
    public static func parseResponse(_ data: Data) throws -> JSONRPCResponse {
        guard let str = String(data: data, encoding: .utf8) else {
            throw HTTPParseError.malformedResponse
        }

        guard str.hasPrefix("HTTP/") else {
            throw HTTPParseError.malformedResponse
        }

        // Split headers from body at \r\n\r\n
        guard let separatorRange = str.range(of: "\r\n\r\n") else {
            throw HTTPParseError.missingBody
        }

        let bodyStr = String(str[separatorRange.upperBound...])
        guard !bodyStr.isEmpty else {
            throw HTTPParseError.missingBody
        }

        return try JSONDecoder().decode(JSONRPCResponse.self, from: Data(bodyStr.utf8))
    }
}
