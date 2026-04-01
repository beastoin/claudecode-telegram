import Testing
import Foundation
@testable import BooCore

@Suite("PeerRegistry")
struct PeerRegistryTests {

    @Test("Register peer returns unique ID")
    func registerPeer() async {
        let registry = PeerRegistry()
        let id1 = await registry.register(name: "agent-a", role: "coder")
        let id2 = await registry.register(name: "agent-b", role: nil)
        #expect(id1 != id2)
        #expect(!id1.isEmpty)
    }

    @Test("List peers returns registered peers")
    func listPeers() async {
        let registry = PeerRegistry()
        let id = await registry.register(name: "agent-a", role: "coder")
        let peers = await registry.listPeers()
        #expect(peers.count == 1)
        #expect(peers[0].peerID == id)
        #expect(peers[0].name == "agent-a")
        #expect(peers[0].role == "coder")
        #expect(peers[0].status == .active)
    }

    @Test("Get peer status for known peer")
    func getPeerStatus() async {
        let registry = PeerRegistry()
        let id = await registry.register(name: "agent-a", role: "coder")
        let status = await registry.getStatus(peerID: id)
        #expect(status != nil)
        #expect(status?.name == "agent-a")
        #expect(status?.status == .active)
    }

    @Test("Get peer status returns nil for unknown peer")
    func getUnknownPeerStatus() async {
        let registry = PeerRegistry()
        let status = await registry.getStatus(peerID: "nonexistent")
        #expect(status == nil)
    }

    @Test("Heartbeat updates last seen")
    func heartbeat() async throws {
        let registry = PeerRegistry()
        let id = await registry.register(name: "agent-a", role: nil)
        let before = await registry.getStatus(peerID: id)!.lastSeen

        // Small delay to ensure time difference
        try await Task.sleep(for: .milliseconds(50))
        await registry.heartbeat(peerID: id)

        let after = await registry.getStatus(peerID: id)!.lastSeen
        #expect(after > before)
    }

    @Test("Evict removes stale peers")
    func evictStalePeers() async {
        let registry = PeerRegistry(peerTTL: 0.1) // 100ms TTL
        let _ = await registry.register(name: "agent-a", role: nil)

        // Wait past TTL
        try? await Task.sleep(for: .milliseconds(150))
        await registry.evictStalePeers()

        let peers = await registry.listPeers()
        #expect(peers.isEmpty)
    }

    @Test("Active peer survives eviction")
    func activePeerSurvivesEviction() async {
        let registry = PeerRegistry(peerTTL: 0.5)
        let _ = await registry.register(name: "agent-a", role: nil)

        // Evict immediately (peer is fresh)
        await registry.evictStalePeers()

        let peers = await registry.listPeers()
        #expect(peers.count == 1)
    }

    @Test("Re-register same name returns new ID")
    func reRegisterSameName() async {
        let registry = PeerRegistry()
        let id1 = await registry.register(name: "agent-a", role: "coder")
        let id2 = await registry.register(name: "agent-a", role: "reviewer")

        // Should replace the old registration
        let peers = await registry.listPeers()
        #expect(peers.count == 1)
        #expect(peers[0].peerID == id2)
        #expect(peers[0].role == "reviewer")
        #expect(id1 != id2)
    }

    @Test("Remove peer by ID")
    func removePeer() async {
        let registry = PeerRegistry()
        let id = await registry.register(name: "agent-a", role: nil)
        await registry.remove(peerID: id)

        let peers = await registry.listPeers()
        #expect(peers.isEmpty)
    }
}
