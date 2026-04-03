import Foundation

/// Unified query facade over PeerRegistry (in-memory, per-process) and
/// SharedPeerStore (file-based, cross-process). Eliminates duplicated
/// merge logic across tool handlers.
public actor PeerDirectory {
    private let registry: PeerRegistry
    private let sharedStore: SharedPeerStore

    public init(registry: PeerRegistry, sharedStore: SharedPeerStore) {
        self.registry = registry
        self.sharedStore = sharedStore
    }

    /// List all peers, merging local registry with shared store (local wins on name collision).
    public func listAll() async -> [PeerInfo] {
        let localPeers = await registry.listPeers()
        let localNames = Set(localPeers.map(\.name))

        let sharedPeers = await sharedStore.listPeers()
        var all = localPeers
        for sp in sharedPeers where !localNames.contains(sp.name) {
            all.append(PeerInfo(
                peerID: sp.peerID, name: sp.name, role: sp.role,
                machine: sp.machine, terminalID: sp.terminalID,
                status: .active, lastSeen: sp.lastSeen
            ))
        }
        return all
    }

    /// Look up a peer by ID — local registry first, then shared store.
    public func lookup(peerID: String) async -> PeerInfo? {
        if let info = await registry.getStatus(peerID: peerID) {
            return info
        }
        if let sp = await sharedStore.lookupPeer(peerID: peerID) {
            return PeerInfo(
                peerID: sp.peerID, name: sp.name, role: sp.role,
                machine: sp.machine, terminalID: sp.terminalID,
                status: .active, lastSeen: sp.lastSeen
            )
        }
        return nil
    }

    /// Look up a peer by name — local registry first, then shared store.
    public func lookup(name: String) async -> PeerInfo? {
        if let id = await registry.peerID(forName: name),
           let info = await registry.getStatus(peerID: id) {
            return info
        }
        if let sp = await sharedStore.lookupPeer(name: name) {
            return PeerInfo(
                peerID: sp.peerID, name: sp.name, role: sp.role,
                machine: sp.machine, terminalID: sp.terminalID,
                status: .active, lastSeen: sp.lastSeen
            )
        }
        return nil
    }

    /// Resolve peer name from ID — convenience for message sender resolution.
    public func resolveName(peerID: String) async -> String {
        if let info = await lookup(peerID: peerID) {
            return info.name
        }
        return "unknown"
    }
}
