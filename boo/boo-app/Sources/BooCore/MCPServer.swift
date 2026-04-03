import Foundation

/// MCP (Model Context Protocol) stdio server.
/// Handles JSON-RPC 2.0 framing and MCP handshake only.
/// Tool implementations live in ToolHandlers.
public actor MCPServer {
    private let toolContext: ToolContext
    private var localPeerID: String?

    public init(registry: PeerRegistry? = nil, machineName: String = "", socketPath: String? = nil, sharedStore: SharedPeerStore? = nil) {
        let reg = registry ?? PeerRegistry()
        let machine = machineName.isEmpty ? (Host.current().localizedName ?? "unknown") : machineName

        let effectiveSocket = socketPath ?? Self.findGhosttySocket()
        let client = effectiveSocket.map { GhosttyClient(socketPath: $0) }

        self.toolContext = ToolContext(
            registry: reg,
            relay: MessageRelay(registry: reg),
            sharedStore: sharedStore ?? SharedPeerStore(),
            ghosttyClient: client,
            machineName: machine
        )
    }

    /// Access the underlying registry (for AppState integration).
    public var registry: PeerRegistry { toolContext.registry }

    private static func findGhosttySocket() -> String? {
        for path in ["/tmp/ghostty.sock", "/tmp/ghostty-boo.sock", "/tmp/ghostty-test.sock"] {
            if let _ = try? GhosttyClient(socketPath: path).listTerminals() {
                return path
            }
        }
        return nil
    }

    /// Process a single line of newline-delimited JSON.
    public func processLine(_ line: String) async throws -> String? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        guard let response = try await handleMessage(trimmed) else { return nil }
        return response + "\n"
    }

    /// Handle a single JSON-RPC message.
    public func handleMessage(_ message: String) async throws -> String? {
        guard let data = message.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return errorResponse(id: nil, code: -32700, message: "Parse error")
        }

        let method = json["method"] as? String ?? ""
        let id = json["id"]
        let params = json["params"] as? [String: Any] ?? [:]

        if id == nil { return nil }

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
        jsonResponse(id: id, result: [
            "protocolVersion": "2025-03-26",
            "capabilities": ["tools": ["listChanged": false]],
            "serverInfo": ["name": "boo-app", "version": "1.0.0"],
        ])
    }

    // MARK: - Tools list

    private func toolsListResponse(id: Any) -> String {
        let tools: [[String: Any]] = [
            toolDef("register_peer", "Register a new peer agent",
                    required: ["name"], properties: [
                        "name": ["type": "string", "description": "Agent name"],
                        "role": ["type": "string", "description": "Agent role"],
                    ]),
            toolDef("list_peers", "List all registered peers (local and cross-instance)",
                    required: [], properties: [:]),
            toolDef("send_message", "Send a message to a peer by name (works across instances)",
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
        var schema: [String: Any] = ["type": "object", "properties": properties]
        if !required.isEmpty { schema["required"] = required }
        return ["name": name, "description": description, "inputSchema": schema]
    }

    // MARK: - Tools dispatch

    private func toolsCallResponse(id: Any, params: [String: Any]) async -> String {
        let toolName = params["name"] as? String ?? ""
        // Safe: args contains only JSON primitives from JSONSerialization
        nonisolated(unsafe) let args = params["arguments"] as? [String: Any] ?? [:]

        switch toolName {
        case "register_peer":
            let result = await ToolHandlers.registerPeer(ctx: toolContext, args: args)
            if !result.peerID.isEmpty { localPeerID = result.peerID }
            return formatResult(id: id, result: result.result)
        case "list_peers":
            return formatResult(id: id, result: await ToolHandlers.listPeers(ctx: toolContext))
        case "send_message":
            return formatResult(id: id, result: await ToolHandlers.sendMessage(ctx: toolContext, args: args))
        case "broadcast":
            return formatResult(id: id, result: await ToolHandlers.broadcast(ctx: toolContext, args: args))
        case "receive_messages":
            return formatResult(id: id, result: await ToolHandlers.receiveMessages(ctx: toolContext, args: args))
        case "get_peer_status":
            return formatResult(id: id, result: await ToolHandlers.getPeerStatus(ctx: toolContext, args: args))
        default:
            return errorResponse(id: id, code: -32602, message: "Unknown tool: \(toolName)")
        }
    }

    // MARK: - Response formatting

    private func formatResult(id: Any, result: ToolResult) -> String {
        switch result {
        case .json(let obj):
            return textResult(id: id, json: obj)
        case .jsonArray(let arr):
            return textResult(id: id, jsonArray: arr)
        case .error(let code, let msg):
            return errorResponse(id: id, code: code, message: msg)
        }
    }

    private func textResult(id: Any, json: [String: Any]) -> String {
        guard let text = serializeJSON(json) else {
            return errorResponse(id: id, code: -32603, message: "Internal: JSON serialization failed")
        }
        return jsonResponse(id: id, result: ["content": [["type": "text", "text": text]]])
    }

    private func textResult(id: Any, jsonArray: [[String: Any]]) -> String {
        guard let text = serializeJSON(jsonArray) else {
            return errorResponse(id: id, code: -32603, message: "Internal: JSON serialization failed")
        }
        return jsonResponse(id: id, result: ["content": [["type": "text", "text": text]]])
    }

    private func jsonResponse(id: Any, result: [String: Any]) -> String {
        let response: [String: Any] = ["jsonrpc": "2.0", "id": id, "result": result]
        return serializeJSON(response) ?? "{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32603,\"message\":\"Internal error\"}}"
    }

    private func errorResponse(id: Any?, code: Int, message: String) -> String {
        var response: [String: Any] = ["jsonrpc": "2.0", "error": ["code": code, "message": message]]
        if let id { response["id"] = id } else { response["id"] = NSNull() }
        return serializeJSON(response) ?? "{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32603,\"message\":\"Internal error\"}}"
    }

    private func serializeJSON(_ object: Any) -> String? {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
