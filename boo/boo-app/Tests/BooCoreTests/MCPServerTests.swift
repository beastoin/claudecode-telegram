import Testing
import Foundation
@testable import BooCore

@Suite("MCPServer")
struct MCPServerTests {

    // MARK: - Protocol handshake

    @Test("Responds to initialize with server info and capabilities")
    func initialize() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
        """)
        let json = try parse(resp)

        #expect(json["id"] as? Int == 1)
        let result = json["result"] as! [String: Any]
        #expect(result["protocolVersion"] as? String == "2025-03-26")
        let serverInfo = result["serverInfo"] as! [String: Any]
        #expect(serverInfo["name"] as? String == "boo-app")
        let caps = result["capabilities"] as! [String: Any]
        #expect(caps["tools"] != nil)
    }

    @Test("Handles notifications/initialized without response")
    func initialized() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
        """)
        // Notifications produce no response
        #expect(resp == nil)
    }

    // MARK: - tools/list

    @Test("Lists all 6 MCP tools")
    func toolsList() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
        """)
        let json = try parse(resp)
        let result = json["result"] as! [String: Any]
        let tools = result["tools"] as! [[String: Any]]
        #expect(tools.count == 6)

        let names = Set(tools.map { $0["name"] as! String })
        #expect(names.contains("register_peer"))
        #expect(names.contains("list_peers"))
        #expect(names.contains("send_message"))
        #expect(names.contains("broadcast"))
        #expect(names.contains("receive_messages"))
        #expect(names.contains("get_peer_status"))
    }

    @Test("Each tool has inputSchema")
    func toolsHaveSchema() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
        """)
        let json = try parse(resp)
        let tools = (json["result"] as! [String: Any])["tools"] as! [[String: Any]]
        for tool in tools {
            #expect(tool["inputSchema"] != nil, "Tool \(tool["name"]!) missing inputSchema")
        }
    }

    // MARK: - tools/call: register_peer

    @Test("register_peer returns peer_id")
    func registerPeer() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"agent-a","role":"coder"}}}
        """)
        let json = try parse(resp)
        let result = json["result"] as! [String: Any]
        let content = result["content"] as! [[String: Any]]
        #expect(content.count == 1)
        #expect(content[0]["type"] as? String == "text")
        let text = content[0]["text"] as! String
        let parsed = try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [String: Any]
        #expect(parsed["peer_id"] != nil)
    }

    // MARK: - tools/call: list_peers

    @Test("list_peers returns registered peers")
    func listPeers() async throws {
        let server = MCPServer()
        // Register first
        _ = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"agent-a","role":"coder"}}}
        """)
        // List
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_peers","arguments":{}}}
        """)
        let text = try extractText(resp)
        let peers = try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [[String: Any]]
        #expect(peers.count == 1)
        #expect(peers[0]["name"] as? String == "agent-a")
        #expect(peers[0]["role"] as? String == "coder")
        #expect(peers[0]["status"] as? String == "active")
    }

    // MARK: - tools/call: send_message + receive_messages

    @Test("send_message then receive_messages round-trip")
    func sendReceive() async throws {
        let server = MCPServer()
        // Register 2 peers
        let respA = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice"}}}
        """)
        let idA = try extractPeerID(respA)

        let respB = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"bob"}}}
        """)
        let idB = try extractPeerID(respB)

        // Send message from alice to bob
        let sendResp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"send_message","arguments":{"peer_id":"\(idA)","to":"bob","content":"hello bob"}}}
        """)
        let sendText = try extractText(sendResp)
        let sendResult = try JSONSerialization.jsonObject(with: Data(sendText.utf8)) as! [String: Any]
        #expect(sendResult["ok"] as? Bool == true)

        // Receive as bob
        let recvResp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"receive_messages","arguments":{"peer_id":"\(idB)"}}}
        """)
        let recvText = try extractText(recvResp)
        let messages = try JSONSerialization.jsonObject(with: Data(recvText.utf8)) as! [[String: Any]]
        #expect(messages.count == 1)
        #expect(messages[0]["from"] as? String == "alice")
        #expect(messages[0]["content"] as? String == "hello bob")
    }

    // MARK: - tools/call: broadcast

    @Test("broadcast delivers to all except sender")
    func broadcast() async throws {
        let server = MCPServer()
        let respA = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice"}}}
        """)
        let idA = try extractPeerID(respA)

        let respB = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"bob"}}}
        """)
        let idB = try extractPeerID(respB)

        _ = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"charlie"}}}
        """)

        // Broadcast from alice
        let bcastResp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"broadcast","arguments":{"peer_id":"\(idA)","content":"hello everyone"}}}
        """)
        let bcastText = try extractText(bcastResp)
        let bcastResult = try JSONSerialization.jsonObject(with: Data(bcastText.utf8)) as! [String: Any]
        #expect(bcastResult["ok"] as? Bool == true)
        let delivered = bcastResult["delivered_to"] as! [String]
        #expect(delivered.count == 2)
        #expect(delivered.contains("bob"))
        #expect(delivered.contains("charlie"))

        // Bob receives
        let recvResp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"receive_messages","arguments":{"peer_id":"\(idB)"}}}
        """)
        let msgs = try JSONSerialization.jsonObject(with: Data(try extractText(recvResp).utf8)) as! [[String: Any]]
        #expect(msgs.count == 1)
        #expect(msgs[0]["content"] as? String == "hello everyone")
    }

    // MARK: - tools/call: get_peer_status

    @Test("get_peer_status returns peer info")
    func getPeerStatus() async throws {
        let server = MCPServer()
        let respA = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice","role":"coder"}}}
        """)
        let idA = try extractPeerID(respA)

        let statusResp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_peer_status","arguments":{"peer_id":"\(idA)"}}}
        """)
        let text = try extractText(statusResp)
        let status = try JSONSerialization.jsonObject(with: Data(text.utf8)) as! [String: Any]
        #expect(status["name"] as? String == "alice")
        #expect(status["status"] as? String == "active")
    }

    @Test("get_peer_status returns error for unknown peer")
    func getPeerStatusUnknown() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_peer_status","arguments":{"peer_id":"nonexistent"}}}
        """)
        let json = try parse(resp)
        #expect(json["error"] != nil)
    }

    // MARK: - Error handling

    @Test("Unknown method returns error")
    func unknownMethod() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"unknown/method","params":{}}
        """)
        let json = try parse(resp)
        let error = json["error"] as! [String: Any]
        #expect(error["code"] as? Int == -32601)
    }

    @Test("Unknown tool returns error")
    func unknownTool() async throws {
        let server = MCPServer()
        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"nonexistent","arguments":{}}}
        """)
        let json = try parse(resp)
        #expect(json["error"] != nil)
    }

    @Test("send_message to unknown peer returns error")
    func sendToUnknown() async throws {
        let server = MCPServer()
        let respA = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"register_peer","arguments":{"name":"alice"}}}
        """)
        let idA = try extractPeerID(respA)

        let resp = try await server.handleMessage("""
        {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"send_message","arguments":{"peer_id":"\(idA)","to":"nobody","content":"hello"}}}
        """)
        let json = try parse(resp)
        #expect(json["error"] != nil)
    }

    // MARK: - Stdio framing

    @Test("processLine handles newline-delimited JSON")
    func processLine() async throws {
        let server = MCPServer()
        let line = """
        {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
        """
        let output = try await server.processLine(line)
        #expect(output != nil)
        #expect(output!.hasSuffix("\n"))
        // Should be valid JSON before the newline
        let jsonStr = String(output!.dropLast())
        let json = try JSONSerialization.jsonObject(with: Data(jsonStr.utf8)) as! [String: Any]
        #expect(json["id"] as? Int == 1)
    }

    // MARK: - Helpers

    private func parse(_ response: String?) throws -> [String: Any] {
        guard let response else { throw TestError.noResponse }
        return try JSONSerialization.jsonObject(with: Data(response.utf8)) as! [String: Any]
    }

    private func extractText(_ response: String?) throws -> String {
        let json = try parse(response)
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

enum TestError: Error {
    case noResponse
}
