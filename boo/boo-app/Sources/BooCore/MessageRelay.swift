import Foundation

/// Errors from MessageRelay operations.
public enum MessageRelayError: Error, Sendable {
    case unknownSender
    case unknownRecipient
}

/// A message between peers.
public struct PeerMessage: Sendable {
    public let id: String
    public let from: String
    public let content: String
    public let timestamp: Date
}

/// Routes messages between registered peers.
public actor MessageRelay {
    private struct StoredMessage {
        let id: String
        let from: String  // sender name
        let content: String
        let timestamp: Date
    }

    private let registry: PeerRegistry
    private let messageTTL: TimeInterval
    private var inboxes: [String: [StoredMessage]] = [:]  // peerID -> messages

    public init(registry: PeerRegistry, messageTTL: TimeInterval = 300) { // 5 min default
        self.registry = registry
        self.messageTTL = messageTTL
    }

    /// Send a message from one peer to another by name.
    public func send(from senderID: String, to recipientName: String, content: String) async throws {
        // Verify sender
        guard let senderInfo = await registry.getStatus(peerID: senderID) else {
            throw MessageRelayError.unknownSender
        }

        // Find recipient by name
        guard let recipientID = await registry.peerID(forName: recipientName) else {
            throw MessageRelayError.unknownRecipient
        }

        let msg = StoredMessage(
            id: UUID().uuidString.lowercased(),
            from: senderInfo.name,
            content: content,
            timestamp: Date()
        )

        inboxes[recipientID, default: []].append(msg)
    }

    /// Broadcast a message to all peers except sender.
    public func broadcast(from senderID: String, content: String) async throws -> [String] {
        guard let senderInfo = await registry.getStatus(peerID: senderID) else {
            throw MessageRelayError.unknownSender
        }

        let peers = await registry.listPeers()
        var delivered: [String] = []

        let msg = StoredMessage(
            id: UUID().uuidString.lowercased(),
            from: senderInfo.name,
            content: content,
            timestamp: Date()
        )

        for peer in peers where peer.peerID != senderID {
            inboxes[peer.peerID, default: []].append(msg)
            delivered.append(peer.name)
        }

        return delivered
    }

    /// Receive and drain all messages for a peer.
    public func receive(peerID: String) -> [PeerMessage] {
        guard let messages = inboxes[peerID], !messages.isEmpty else { return [] }
        inboxes[peerID] = []
        return messages.map { m in
            PeerMessage(id: m.id, from: m.from, content: m.content, timestamp: m.timestamp)
        }
    }

    /// Remove messages older than TTL.
    public func evictStaleMessages() {
        let cutoff = Date().addingTimeInterval(-messageTTL)
        for (peerID, messages) in inboxes {
            inboxes[peerID] = messages.filter { $0.timestamp > cutoff }
        }
    }
}
