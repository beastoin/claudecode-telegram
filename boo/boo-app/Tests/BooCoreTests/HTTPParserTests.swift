import Testing
import Foundation
@testable import BooCore

@Suite("HTTP Parser")
struct HTTPParserTests {

    // MARK: - Request building

    @Test("Builds HTTP request from JSON-RPC request")
    func buildsHTTPRequest() throws {
        let rpc = JSONRPCRequest(id: .int(1), method: "list_terminals", params: nil)
        let data = try HTTPMessage.buildRequest(rpc: rpc)
        let str = String(data: data, encoding: .utf8)!

        #expect(str.hasPrefix("POST / HTTP/1.1\r\n"))
        #expect(str.contains("Content-Type: application/json\r\n"))
        #expect(str.contains("Content-Length:"))
        #expect(str.contains("Connection: close\r\n"))
        // Body follows blank line
        let parts = str.components(separatedBy: "\r\n\r\n")
        #expect(parts.count == 2)
        let body = parts[1]
        let json = try JSONSerialization.jsonObject(with: body.data(using: .utf8)!) as! [String: Any]
        #expect(json["method"] as? String == "list_terminals")
    }

    // MARK: - Response parsing

    @Test("Parses HTTP response with JSON body")
    func parsesHTTPResponse() throws {
        let body = """
        {"jsonrpc":"2.0","id":1,"result":{"ok":true}}
        """
        let raw = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\n\r\n\(body)"
        let resp = try HTTPMessage.parseResponse(Data(raw.utf8))

        #expect(resp.id == .int(1))
        if case .object(let obj) = resp.result {
            #expect(obj["ok"] == .bool(true))
        } else {
            Issue.record("Expected object result")
        }
    }

    @Test("Parses response with extra headers")
    func parsesWithExtraHeaders() throws {
        let body = """
        {"jsonrpc":"2.0","id":2,"result":[]}
        """
        let raw = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Custom: foo\r\nContent-Length: \(body.utf8.count)\r\n\r\n\(body)"
        let resp = try HTTPMessage.parseResponse(Data(raw.utf8))

        #expect(resp.id == .int(2))
        if case .array(let arr) = resp.result {
            #expect(arr.isEmpty)
        } else {
            Issue.record("Expected array result")
        }
    }

    @Test("Throws on malformed HTTP response")
    func throwsOnMalformed() {
        #expect(throws: HTTPParseError.self) {
            try HTTPMessage.parseResponse(Data("not http".utf8))
        }
    }

    @Test("Throws on missing body separator")
    func throwsOnMissingSeparator() {
        #expect(throws: HTTPParseError.self) {
            try HTTPMessage.parseResponse(Data("HTTP/1.1 200 OK\r\nContent-Type: json".utf8))
        }
    }

    @Test("Parses response without Content-Length (read until end)")
    func parsesWithoutContentLength() throws {
        let body = """
        {"jsonrpc":"2.0","id":3,"result":null}
        """
        let raw = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n\(body)"
        let resp = try HTTPMessage.parseResponse(Data(raw.utf8))

        #expect(resp.id == .int(3))
        #expect(resp.result == nil)
    }
}
