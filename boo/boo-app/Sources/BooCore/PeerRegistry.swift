import Foundation

/// Peer connection status.
public enum PeerStatus: String, Sendable {
    case active
    case stale
}

/// A registered peer in the network.
public struct PeerInfo: Sendable {
    public let peerID: String
    public let name: String
    public let role: String?
    public let machine: String
    public let terminalID: String?
    public let status: PeerStatus
    public let lastSeen: Date
}

/// Actor-based peer registry with TTL eviction.
/// Manages registered peers for the message relay.
public actor PeerRegistry {
    private struct PeerRecord {
        let peerID: String
        let name: String
        let role: String?
        let machine: String
        let terminalID: String?
        var lastSeen: Date
    }

    private var peers: [String: PeerRecord] = [:]  // peerID -> record
    private var nameIndex: [String: String] = [:]   // name -> peerID
    private let peerTTL: TimeInterval

    public init(peerTTL: TimeInterval = BooDefaults.peerTTL) {
        self.peerTTL = peerTTL
    }

    /// Register a peer. If a peer with the same name exists, it's replaced.
    @discardableResult
    public func register(name: String, role: String? = nil, machine: String = "", terminalID: String? = nil) -> String {
        // Remove old registration with same name
        if let oldID = nameIndex[name] {
            peers.removeValue(forKey: oldID)
        }

        let id = UUID().uuidString.lowercased()
        let record = PeerRecord(peerID: id, name: name, role: role, machine: machine, terminalID: terminalID, lastSeen: Date())
        peers[id] = record
        nameIndex[name] = id
        return id
    }

    /// List all registered peers.
    public func listPeers() -> [PeerInfo] {
        let now = Date()
        return peers.values.map { r in
            PeerInfo(
                peerID: r.peerID, name: r.name, role: r.role,
                machine: r.machine, terminalID: r.terminalID,
                status: now.timeIntervalSince(r.lastSeen) < peerTTL ? .active : .stale,
                lastSeen: r.lastSeen
            )
        }
    }

    /// Get status of a specific peer.
    public func getStatus(peerID: String) -> PeerInfo? {
        guard let r = peers[peerID] else { return nil }
        let now = Date()
        return PeerInfo(
            peerID: r.peerID, name: r.name, role: r.role,
            machine: r.machine, terminalID: r.terminalID,
            status: now.timeIntervalSince(r.lastSeen) < peerTTL ? .active : .stale,
            lastSeen: r.lastSeen
        )
    }

    /// Find peer ID by name.
    public func peerID(forName name: String) -> String? {
        nameIndex[name]
    }

    /// Update heartbeat for a peer.
    public func heartbeat(peerID: String) {
        peers[peerID]?.lastSeen = Date()
    }

    /// Remove stale peers (last seen > TTL).
    public func evictStalePeers() {
        let now = Date()
        let staleIDs = peers.filter { now.timeIntervalSince($0.value.lastSeen) >= peerTTL }.map(\.key)
        for id in staleIDs {
            if let name = peers[id]?.name {
                nameIndex.removeValue(forKey: name)
            }
            peers.removeValue(forKey: id)
        }
    }

    /// Remove a specific peer.
    public func remove(peerID: String) {
        if let name = peers[peerID]?.name {
            nameIndex.removeValue(forKey: name)
        }
        peers.removeValue(forKey: peerID)
    }
}
