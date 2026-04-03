import Foundation

/// Shared context passed to every tool handler.
public struct ToolContext: Sendable {
    public let registry: PeerRegistry
    public let relay: MessageRelay
    public let sharedStore: SharedPeerStore
    public let directory: PeerDirectory
    public let ghosttyClient: GhosttyClient?
    public let machineName: String

    public init(registry: PeerRegistry, relay: MessageRelay, sharedStore: SharedPeerStore,
                ghosttyClient: GhosttyClient?, machineName: String) {
        self.registry = registry
        self.relay = relay
        self.sharedStore = sharedStore
        self.directory = PeerDirectory(registry: registry, sharedStore: sharedStore)
        self.ghosttyClient = ghosttyClient
        self.machineName = machineName
    }
}

/// Result from a tool handler — MCPServer maps this to JSON-RPC.
/// Uses @unchecked Sendable because [String: Any] isn't Sendable,
/// but we only put JSON-safe primitives in.
public enum ToolResult: @unchecked Sendable {
    case json([String: Any])
    case jsonArray([[String: Any]])
    case error(Int, String)
}

/// Static tool implementations — one function per MCP tool.
/// MCPServer handles JSON-RPC framing; ToolHandlers handle business logic.
public enum ToolHandlers {

    // MARK: - register_peer

    public struct RegisterResult: Sendable {
        public let peerID: String
        public let result: ToolResult
    }

    public static func registerPeer(ctx: ToolContext, args: [String: Any]) async -> RegisterResult {
        guard let name = args["name"] as? String else {
            return RegisterResult(peerID: "", result: .error(-32602, "Missing required param: name"))
        }
        let role = args["role"] as? String
        let terminalID = detectTerminalID(client: ctx.ghosttyClient)
        let peerID = await ctx.registry.register(name: name, role: role, machine: ctx.machineName, terminalID: terminalID)

        await ctx.sharedStore.registerPeer(SharedPeer(
            peerID: peerID, name: name, role: role,
            machine: ctx.machineName, terminalID: terminalID
        ))

        return RegisterResult(peerID: peerID, result: .json(["peer_id": peerID]))
    }

    // MARK: - list_peers

    public static func listPeers(ctx: ToolContext) async -> ToolResult {
        let allPeers = await ctx.directory.listAll()

        let list = allPeers.map { p -> [String: Any] in
            var entry: [String: Any] = [
                "peer_id": p.peerID,
                "name": p.name,
                "machine": p.machine,
                "status": p.status == .active ? "active" : "stale",
            ]
            if let role = p.role { entry["role"] = role }
            return entry
        }
        return .jsonArray(list)
    }

    // MARK: - send_message

    public static func sendMessage(ctx: ToolContext, args: [String: Any]) async -> ToolResult {
        guard let peerID = args["peer_id"] as? String,
              let to = args["to"] as? String,
              let content = args["content"] as? String else {
            return .error(-32602, "Missing required params: peer_id, to, content")
        }

        // Try local relay first
        do {
            try await ctx.relay.send(from: peerID, to: to, content: content)
            // Push: inject notification into recipient's terminal
            await injectTerminalNotification(ctx: ctx, recipientName: to, senderPeerID: peerID, content: content)
            return .json(["ok": true])
        } catch {
            // Recipient not in local registry — try cross-instance delivery
        }

        guard let _ = await ctx.directory.lookup(name: to),
              let targetShared = await ctx.sharedStore.lookupPeer(name: to) else {
            return .error(-32000, "Unknown recipient: \(to)")
        }

        let senderName = await ctx.directory.resolveName(peerID: peerID)

        await ctx.sharedStore.storeMessage(
            toPeerID: targetShared.peerID,
            message: SharedMessage(from: senderName, content: content)
        )

        if let terminalID = targetShared.terminalID, let client = ctx.ghosttyClient {
            let notification = "\n[Boo message from \(senderName)]: \(content)\n"
            try? client.sendText(terminalID: terminalID, text: notification)
        }

        return .json(["ok": true])
    }

    // MARK: - broadcast

    public static func broadcast(ctx: ToolContext, args: [String: Any]) async -> ToolResult {
        guard let peerID = args["peer_id"] as? String,
              let content = args["content"] as? String else {
            return .error(-32602, "Missing required params: peer_id, content")
        }

        let senderName = await ctx.directory.resolveName(peerID: peerID)

        var delivered: [String] = []
        if let localDelivered = try? await ctx.relay.broadcast(from: peerID, content: content) {
            delivered.append(contentsOf: localDelivered)
        }

        let sharedPeers = await ctx.sharedStore.listPeers()
        for sp in sharedPeers where sp.peerID != peerID && !delivered.contains(sp.name) {
            await ctx.sharedStore.storeMessage(
                toPeerID: sp.peerID,
                message: SharedMessage(from: senderName, content: content)
            )
            if let terminalID = sp.terminalID, let client = ctx.ghosttyClient {
                let notification = "\n[Boo broadcast from \(senderName)]: \(content)\n"
                try? client.sendText(terminalID: terminalID, text: notification)
            }
            delivered.append(sp.name)
        }

        return .json(["ok": true, "delivered_to": delivered])
    }

    // MARK: - receive_messages

    public static func receiveMessages(ctx: ToolContext, args: [String: Any]) async -> ToolResult {
        guard let peerID = args["peer_id"] as? String else {
            return .error(-32602, "Missing required param: peer_id")
        }

        var messages = await ctx.relay.receive(peerID: peerID)

        let sharedMsgs = await ctx.sharedStore.receiveMessages(peerID: peerID)
        for sm in sharedMsgs {
            messages.append(PeerMessage(id: sm.id, from: sm.from, content: sm.content, timestamp: sm.timestamp))
        }

        let list = messages.map { m -> [String: Any] in
            [
                "id": m.id,
                "from": m.from,
                "content": m.content,
                "timestamp": ISO8601DateFormatter().string(from: m.timestamp),
            ]
        }
        return .jsonArray(list)
    }

    // MARK: - get_peer_status

    public static func getPeerStatus(ctx: ToolContext, args: [String: Any]) async -> ToolResult {
        guard let peerID = args["peer_id"] as? String else {
            return .error(-32602, "Missing required param: peer_id")
        }

        guard let info = await ctx.directory.lookup(peerID: peerID) else {
            return .error(-32001, "Unknown peer: \(peerID)")
        }

        var result: [String: Any] = [
            "name": info.name,
            "machine": info.machine,
            "status": info.status == .active ? "active" : "stale",
            "last_seen": ISO8601DateFormatter().string(from: info.lastSeen),
        ]
        if let role = info.role { result["role"] = role }
        return .json(result)
    }

    // MARK: - Helpers

    /// Inject a notification into the recipient's terminal (push messaging).
    /// Looks up terminalID from local registry first, then shared store.
    private static func injectTerminalNotification(ctx: ToolContext, recipientName: String, senderPeerID: String, content: String) async {
        guard let client = ctx.ghosttyClient else { return }

        let senderName = await ctx.directory.resolveName(peerID: senderPeerID)

        // Check local registry for terminalID
        if let recipientID = await ctx.registry.peerID(forName: recipientName),
           let info = await ctx.registry.getStatus(peerID: recipientID),
           let terminalID = info.terminalID {
            let notification = "\n[Boo message from \(senderName)]: \(content)\n"
            try? client.sendText(terminalID: terminalID, text: notification)
            return
        }

        // Fall back to shared store
        if let sp = await ctx.sharedStore.lookupPeer(name: recipientName),
           let terminalID = sp.terminalID {
            let notification = "\n[Boo message from \(senderName)]: \(content)\n"
            try? client.sendText(terminalID: terminalID, text: notification)
        }
    }

    /// Detect which Ghostty terminal this MCP instance belongs to.
    private static func detectTerminalID(client: GhosttyClient?) -> String? {
        if let envID = ProcessInfo.processInfo.environment["BOO_TERMINAL_ID"] {
            return envID
        }

        guard let client, let terminals = try? client.listTerminals() else { return nil }

        let cwd = FileManager.default.currentDirectoryPath
        if let match = terminals.first(where: { $0.workingDirectory == cwd }) {
            return match.id
        }

        return terminals.first(where: { $0.focused })?.id
    }
}
