import Testing
import Foundation
@testable import BooCore

@Suite("MessageRelay")
struct MessageRelayTests {

    @Test("Send message between peers")
    func sendMessage() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)

        let idA = await registry.register(name: "agent-a", role: nil)
        let idB = await registry.register(name: "agent-b", role: nil)

        try await relay.send(from: idA, to: "agent-b", content: "hello from a")

        let messages = await relay.receive(peerID: idB)
        #expect(messages.count == 1)
        #expect(messages[0].from == "agent-a")
        #expect(messages[0].content == "hello from a")
        #expect(!messages[0].id.isEmpty)
    }

    @Test("Receive drains messages")
    func receiveDrains() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)

        let idA = await registry.register(name: "agent-a", role: nil)
        let idB = await registry.register(name: "agent-b", role: nil)

        try await relay.send(from: idA, to: "agent-b", content: "msg1")
        let first = await relay.receive(peerID: idB)
        #expect(first.count == 1)

        // Second receive should be empty
        let second = await relay.receive(peerID: idB)
        #expect(second.isEmpty)
    }

    @Test("Broadcast delivers to all peers except sender")
    func broadcast() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)

        let idA = await registry.register(name: "agent-a", role: nil)
        let idB = await registry.register(name: "agent-b", role: nil)
        let idC = await registry.register(name: "agent-c", role: nil)

        let delivered = try await relay.broadcast(from: idA, content: "hello all")
        #expect(delivered.count == 2)
        #expect(delivered.contains("agent-b"))
        #expect(delivered.contains("agent-c"))

        let msgsB = await relay.receive(peerID: idB)
        #expect(msgsB.count == 1)
        #expect(msgsB[0].content == "hello all")

        let msgsC = await relay.receive(peerID: idC)
        #expect(msgsC.count == 1)

        // Sender shouldn't get own broadcast
        let msgsA = await relay.receive(peerID: idA)
        #expect(msgsA.isEmpty)
    }

    @Test("Send to unknown peer throws")
    func sendToUnknownPeer() async {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)
        let idA = await registry.register(name: "agent-a", role: nil)

        await #expect(throws: MessageRelayError.self) {
            try await relay.send(from: idA, to: "nonexistent", content: "hello")
        }
    }

    @Test("Send from unknown peer throws")
    func sendFromUnknownPeer() async {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)
        let _ = await registry.register(name: "agent-b", role: nil)

        await #expect(throws: MessageRelayError.self) {
            try await relay.send(from: "fake-id", to: "agent-b", content: "hello")
        }
    }

    @Test("Message TTL eviction")
    func messageTTLEviction() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry, messageTTL: 0.1) // 100ms

        let idA = await registry.register(name: "agent-a", role: nil)
        let idB = await registry.register(name: "agent-b", role: nil)

        try await relay.send(from: idA, to: "agent-b", content: "will expire")
        try await Task.sleep(for: .milliseconds(150))
        await relay.evictStaleMessages()

        let messages = await relay.receive(peerID: idB)
        #expect(messages.isEmpty)
    }

    @Test("Multiple messages ordered by time")
    func multipleMessagesOrdered() async throws {
        let registry = PeerRegistry()
        let relay = MessageRelay(registry: registry)

        let idA = await registry.register(name: "agent-a", role: nil)
        let idB = await registry.register(name: "agent-b", role: nil)

        try await relay.send(from: idA, to: "agent-b", content: "first")
        try await relay.send(from: idA, to: "agent-b", content: "second")
        try await relay.send(from: idA, to: "agent-b", content: "third")

        let messages = await relay.receive(peerID: idB)
        #expect(messages.count == 3)
        #expect(messages[0].content == "first")
        #expect(messages[1].content == "second")
        #expect(messages[2].content == "third")
    }
}
