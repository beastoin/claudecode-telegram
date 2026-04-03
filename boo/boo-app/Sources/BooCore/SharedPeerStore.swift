import Foundation

/// File-based shared peer and message store at /tmp/boo/.
/// Enables cross-instance MCP communication — multiple BooApp --mcp processes
/// can discover each other's peers and exchange messages through this shared store.
public actor SharedPeerStore {
    private let baseDir: String
    private let peersFile: String
    private let messagesDir: String

    public init(baseDir: String = "/tmp/boo") {
        self.baseDir = baseDir
        self.peersFile = baseDir + "/peers.json"
        self.messagesDir = baseDir + "/messages"
        Self.ensureDirectories(baseDir: baseDir, messagesDir: baseDir + "/messages")
    }

    // MARK: - Peers

    /// Register or update a peer in the shared store.
    public func registerPeer(_ peer: SharedPeer) {
        var peers = loadPeers()
        peers.removeAll { $0.name == peer.name }
        peers.append(peer)
        savePeers(peers)
    }

    /// List all non-expired peers.
    public func listPeers() -> [SharedPeer] {
        loadPeers().filter {
            Date().timeIntervalSince($0.lastSeen) < BooDefaults.peerTTL
        }
    }

    /// Look up a peer by name.
    public func lookupPeer(name: String) -> SharedPeer? {
        listPeers().first { $0.name == name }
    }

    /// Look up a peer by ID.
    public func lookupPeer(peerID: String) -> SharedPeer? {
        listPeers().first { $0.peerID == peerID }
    }

    /// Remove a peer by ID.
    public func removePeer(peerID: String) {
        var peers = loadPeers()
        peers.removeAll { $0.peerID == peerID }
        savePeers(peers)
    }

    /// Remove all peers (for cleanup).
    public func removeAll() {
        savePeers([])
        // Also clean up messages
        try? FileManager.default.removeItem(atPath: messagesDir)
        Self.ensureDirectories(baseDir: baseDir, messagesDir: messagesDir)
    }

    // MARK: - Messages

    /// Store a message in a peer's file-based inbox.
    public func storeMessage(toPeerID: String, message: SharedMessage) {
        let inboxDir = messagesDir + "/" + toPeerID
        try? FileManager.default.createDirectory(atPath: inboxDir, withIntermediateDirectories: true)
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: inboxDir)
        let filePath = inboxDir + "/" + message.id + ".json"
        if let data = try? JSONEncoder().encode(message) {
            try? data.write(to: URL(fileURLWithPath: filePath), options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: filePath)
        }
    }

    /// Read and consume all messages for a peer.
    public func receiveMessages(peerID: String) -> [SharedMessage] {
        let inboxDir = messagesDir + "/" + peerID
        guard let files = try? FileManager.default.contentsOfDirectory(atPath: inboxDir) else {
            return []
        }
        var messages: [SharedMessage] = []
        for file in files where file.hasSuffix(".json") {
            let path = inboxDir + "/" + file
            if let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
               let msg = try? JSONDecoder().decode(SharedMessage.self, from: data) {
                messages.append(msg)
                try? FileManager.default.removeItem(atPath: path) // Consume on read
            }
        }
        return messages.sorted { $0.timestamp < $1.timestamp }
    }

    // MARK: - Private

    private static nonisolated func ensureDirectories(baseDir: String, messagesDir: String) {
        for dir in [baseDir, messagesDir] {
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir)
        }
    }

    private func loadPeers() -> [SharedPeer] {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: peersFile)),
              let peers = try? JSONDecoder().decode([SharedPeer].self, from: data) else {
            return []
        }
        return peers
    }

    private func savePeers(_ peers: [SharedPeer]) {
        if let data = try? JSONEncoder().encode(peers) {
            try? data.write(to: URL(fileURLWithPath: peersFile), options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: peersFile)
        }
    }
}

// MARK: - Types

public struct SharedPeer: Codable, Sendable {
    public let peerID: String
    public let name: String
    public let role: String?
    public let machine: String
    public let terminalID: String?
    public let lastSeen: Date

    public init(peerID: String, name: String, role: String?, machine: String,
                terminalID: String?, lastSeen: Date = Date()) {
        self.peerID = peerID
        self.name = name
        self.role = role
        self.machine = machine
        self.terminalID = terminalID
        self.lastSeen = lastSeen
    }
}

public struct SharedMessage: Codable, Sendable {
    public let id: String
    public let from: String
    public let content: String
    public let timestamp: Date

    public init(id: String = UUID().uuidString.lowercased(), from: String,
                content: String, timestamp: Date = Date()) {
        self.id = id
        self.from = from
        self.content = content
        self.timestamp = timestamp
    }
}
