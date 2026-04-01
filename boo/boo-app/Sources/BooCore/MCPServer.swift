import Foundation

/// MCP (Model Context Protocol) stdio server.
/// Handles JSON-RPC 2.0 messages over newline-delimited stdin/stdout.
/// Implements the 6 boo-app MCP tools.
public actor MCPServer {
    private let registry: PeerRegistry
    private let relay: MessageRelay
    private let machineName: String

    public init(registry: PeerRegistry? = nil, machineName: String = "") {
        let reg = registry ?? PeerRegistry()
        self.registry = reg
        self.relay = MessageRelay(registry: reg)
        self.machineName = machineName.isEmpty ? (Host.current().localizedName ?? "unknown") : machineName
    }

    /// Process a single line of newline-delimited JSON. Returns response line (with \n) or nil for notifications.
    public func processLine(_ line: String) async throws -> String? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        guard let response = try await handleMessage(trimmed) else { return nil }
        return response + "\n"
    }

    /// Handle a single JSON-RPC message. Returns JSON response string or nil for notifications.
    public func handleMessage(_ message: String) async throws -> String? {
        guard let data = message.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return errorResponse(id: nil, code: -32700, message: "Parse error")
        }

        let method = json["method"] as? String ?? ""
        let id = json["id"]  // nil for notifications
        let params = json["params"] as? [String: Any] ?? [:]

        // Notifications (no id) produce no response
        if id == nil {
            return nil
        }

        switch method {
        case "initialize":
            return initializeResponse(id: id!)
        case "tools/list":
            return toolsListResponse(id: id!)
        case "tools/call":
            return await toolsCallResponse(id: id!, params: params)
        default:
            return errorResponse(id: id, code: -32601, message: "Method not found: \(method)")
        }
    }

    // MARK: - Initialize

    private func initializeResponse(id: Any) -> String {
        let result: [String: Any] = [
            "protocolVersion": "2025-03-26",
            "capabilities": [
                "tools": ["listChanged": false]
            ],
            "serverInfo": [
                "name": "boo-app",
                "version": "1.0.0",
            ]
        ]
        return jsonResponse(id: id, result: result)
    }

    // MARK: - Tools list

    private func toolsListResponse(id: Any) -> String {
        let tools: [[String: Any]] = [
            toolDef("register_peer", "Register a new peer agent",
                    required: ["name"], properties: [
                        "name": ["type": "string", "description": "Agent name"],
                        "role": ["type": "string", "description": "Agent role"],
                    ]),
            toolDef("list_peers", "List all registered peers",
                    required: [], properties: [:]),
            toolDef("send_message", "Send a message to a peer by name",
                    required: ["peer_id", "to", "content"], properties: [
                        "peer_id": ["type": "string", "description": "Your peer ID"],
                        "to": ["type": "string", "description": "Recipient name"],
                        "content": ["type": "string", "description": "Message content"],
                    ]),
            toolDef("broadcast", "Broadcast a message to all peers",
                    required: ["peer_id", "content"], properties: [
                        "peer_id": ["type": "string", "description": "Your peer ID"],
                        "content": ["type": "string", "description": "Message content"],
                    ]),
            toolDef("receive_messages", "Receive pending messages",
                    required: ["peer_id"], properties: [
                        "peer_id": ["type": "string", "description": "Your peer ID"],
                    ]),
            toolDef("get_peer_status", "Get status of a specific peer",
                    required: ["peer_id"], properties: [
                        "peer_id": ["type": "string", "description": "Peer ID to query"],
                    ]),
        ]
        return jsonResponse(id: id, result: ["tools": tools])
    }

    private func toolDef(_ name: String, _ description: String,
                         required: [String], properties: [String: [String: String]]) -> [String: Any] {
        var schema: [String: Any] = [
            "type": "object",
            "properties": properties,
        ]
        if !required.isEmpty {
            schema["required"] = required
        }
        return [
            "name": name,
            "description": description,
            "inputSchema": schema,
        ]
    }

    // MARK: - Tools call

    private func toolsCallResponse(id: Any, params: [String: Any]) async -> String {
        let toolName = params["name"] as? String ?? ""
        let args = params["arguments"] as? [String: Any] ?? [:]

        switch toolName {
        case "register_peer":
            return await callRegisterPeer(id: id, args: args)
        case "list_peers":
            return await callListPeers(id: id)
        case "send_message":
            return await callSendMessage(id: id, args: args)
        case "broadcast":
            return await callBroadcast(id: id, args: args)
        case "receive_messages":
            return await callReceiveMessages(id: id, args: args)
        case "get_peer_status":
            return await callGetPeerStatus(id: id, args: args)
        default:
            return errorResponse(id: id, code: -32602, message: "Unknown tool: \(toolName)")
        }
    }

    private func callRegisterPeer(id: Any, args: [String: Any]) async -> String {
        guard let name = args["name"] as? String else {
            return errorResponse(id: id, code: -32602, message: "Missing required param: name")
        }
        let role = args["role"] as? String
        let peerID = await registry.register(name: name, role: role, machine: machineName)
        return textResult(id: id, json: ["peer_id": peerID])
    }

    private func callListPeers(id: Any) async -> String {
        let peers = await registry.listPeers()
        let list = peers.map { p -> [String: Any] in
            var entry: [String: Any] = [
                "peer_id": p.peerID,
                "name": p.name,
                "machine": p.machine,
                "status": p.status == .active ? "active" : "stale",
            ]
            if let role = p.role { entry["role"] = role }
            return entry
        }
        return textResult(id: id, jsonArray: list)
    }

    private func callSendMessage(id: Any, args: [String: Any]) async -> String {
        guard let peerID = args["peer_id"] as? String,
              let to = args["to"] as? String,
              let content = args["content"] as? String else {
            return errorResponse(id: id, code: -32602, message: "Missing required params: peer_id, to, content")
        }
        do {
            try await relay.send(from: peerID, to: to, content: content)
            return textResult(id: id, json: ["ok": true])
        } catch {
            return errorResponse(id: id, code: -32000, message: "\(error)")
        }
    }

    private func callBroadcast(id: Any, args: [String: Any]) async -> String {
        guard let peerID = args["peer_id"] as? String,
              let content = args["content"] as? String else {
            return errorResponse(id: id, code: -32602, message: "Missing required params: peer_id, content")
        }
        do {
            let delivered = try await relay.broadcast(from: peerID, content: content)
            return textResult(id: id, json: ["ok": true, "delivered_to": delivered])
        } catch {
            return errorResponse(id: id, code: -32000, message: "\(error)")
        }
    }

    private func callReceiveMessages(id: Any, args: [String: Any]) async -> String {
        guard let peerID = args["peer_id"] as? String else {
            return errorResponse(id: id, code: -32602, message: "Missing required param: peer_id")
        }
        let messages = await relay.receive(peerID: peerID)
        let list = messages.map { m -> [String: Any] in
            [
                "id": m.id,
                "from": m.from,
                "content": m.content,
                "timestamp": ISO8601DateFormatter().string(from: m.timestamp),
            ]
        }
        return textResult(id: id, jsonArray: list)
    }

    private func callGetPeerStatus(id: Any, args: [String: Any]) async -> String {
        guard let peerID = args["peer_id"] as? String else {
            return errorResponse(id: id, code: -32602, message: "Missing required param: peer_id")
        }
        guard let info = await registry.getStatus(peerID: peerID) else {
            return errorResponse(id: id, code: -32001, message: "Unknown peer: \(peerID)")
        }
        var result: [String: Any] = [
            "name": info.name,
            "machine": info.machine,
            "status": info.status == .active ? "active" : "stale",
            "last_seen": ISO8601DateFormatter().string(from: info.lastSeen),
        ]
        if let role = info.role { result["role"] = role }
        return textResult(id: id, json: result)
    }

    // MARK: - Response helpers

    private func textResult(id: Any, json: [String: Any]) -> String {
        let text = String(data: try! JSONSerialization.data(withJSONObject: json, options: [.sortedKeys]), encoding: .utf8)!
        return jsonResponse(id: id, result: [
            "content": [["type": "text", "text": text]]
        ])
    }

    private func textResult(id: Any, jsonArray: [[String: Any]]) -> String {
        let text = String(data: try! JSONSerialization.data(withJSONObject: jsonArray, options: [.sortedKeys]), encoding: .utf8)!
        return jsonResponse(id: id, result: [
            "content": [["type": "text", "text": text]]
        ])
    }

    private func jsonResponse(id: Any, result: [String: Any]) -> String {
        let response: [String: Any] = ["jsonrpc": "2.0", "id": id, "result": result]
        return String(data: try! JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]), encoding: .utf8)!
    }

    private func errorResponse(id: Any?, code: Int, message: String) -> String {
        var response: [String: Any] = [
            "jsonrpc": "2.0",
            "error": ["code": code, "message": message],
        ]
        if let id { response["id"] = id } else { response["id"] = NSNull() }
        return String(data: try! JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]), encoding: .utf8)!
    }
}
