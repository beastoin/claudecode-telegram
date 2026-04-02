import Foundation

/// Drives the First Contact live demo using real MCP tool calls.
/// Uses AppState's PeerRegistry + MessageRelay (in-memory, no external deps).
public actor OnboardingDemoEngine {
    public struct DemoLine: Sendable {
        public let direction: Direction
        public let tool: String
        public let content: String
        public let timestamp: Date

        public enum Direction: Sendable {
            case request
            case response
            case error
        }
    }

    public init() {}

    /// Run the demo sequence, yielding transcript lines as they complete.
    public func runDemo(
        peerName: String,
        registry: PeerRegistry,
        relay: MessageRelay,
        machineName: String
    ) -> AsyncStream<DemoLine> {
        AsyncStream { continuation in
            Task {
                let guideName = "boo-guide"

                // Step 1: register_peer (user)
                continuation.yield(DemoLine(
                    direction: .request, tool: "register_peer",
                    content: "{ name: \"\(peerName)\", role: \"claude\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(400))

                let userPeerID = await registry.register(name: peerName, role: "claude", machine: machineName)
                continuation.yield(DemoLine(
                    direction: .response, tool: "register_peer",
                    content: "{ peer_id: \"\(userPeerID)\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(600))

                // Step 2: register_peer (guide bot)
                continuation.yield(DemoLine(
                    direction: .request, tool: "register_peer",
                    content: "{ name: \"\(guideName)\", role: \"guide\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(400))

                let guidePeerID = await registry.register(name: guideName, role: "guide", machine: "local")
                continuation.yield(DemoLine(
                    direction: .response, tool: "register_peer",
                    content: "{ peer_id: \"\(guidePeerID)\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(600))

                // Step 3: list_peers
                continuation.yield(DemoLine(
                    direction: .request, tool: "list_peers",
                    content: "{}",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(400))

                let peers = await registry.listPeers()
                let peerList = peers.map { "{ name: \"\($0.name)\", status: \"\($0.status.rawValue)\" }" }.joined(separator: ", ")
                continuation.yield(DemoLine(
                    direction: .response, tool: "list_peers",
                    content: "[ \(peerList) ]",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(600))

                // Step 4: send_message (user → guide)
                continuation.yield(DemoLine(
                    direction: .request, tool: "send_message",
                    content: "{ to: \"\(guideName)\", content: \"hello from boo\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(400))

                do {
                    try await relay.send(from: userPeerID, to: guideName, content: "hello from boo")
                    continuation.yield(DemoLine(
                        direction: .response, tool: "send_message",
                        content: "{ status: \"delivered\" }",
                        timestamp: Date()
                    ))
                } catch {
                    continuation.yield(DemoLine(
                        direction: .error, tool: "send_message",
                        content: "{ error: \"\(error)\" }",
                        timestamp: Date()
                    ))
                }
                try? await Task.sleep(for: .milliseconds(600))

                // Step 5: guide sends reply back
                do {
                    try await relay.send(from: guidePeerID, to: peerName, content: "welcome to boo")
                } catch {
                    // Non-fatal, demo continues
                }
                try? await Task.sleep(for: .milliseconds(300))

                // Step 6: receive_messages
                continuation.yield(DemoLine(
                    direction: .request, tool: "receive_messages",
                    content: "{ peer_id: \"\(userPeerID)\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(400))

                let messages = await relay.receive(peerID: userPeerID)
                if let msg = messages.first {
                    continuation.yield(DemoLine(
                        direction: .response, tool: "receive_messages",
                        content: "[ { from: \"\(msg.from)\", content: \"\(msg.content)\" } ]",
                        timestamp: Date()
                    ))
                } else {
                    continuation.yield(DemoLine(
                        direction: .response, tool: "receive_messages",
                        content: "[]",
                        timestamp: Date()
                    ))
                }
                try? await Task.sleep(for: .milliseconds(600))

                // Step 7: get_peer_status
                continuation.yield(DemoLine(
                    direction: .request, tool: "get_peer_status",
                    content: "{ peer_id: \"\(guidePeerID)\" }",
                    timestamp: Date()
                ))
                try? await Task.sleep(for: .milliseconds(400))

                if let status = await registry.getStatus(peerID: guidePeerID) {
                    continuation.yield(DemoLine(
                        direction: .response, tool: "get_peer_status",
                        content: "{ name: \"\(status.name)\", status: \"\(status.status.rawValue)\" }",
                        timestamp: Date()
                    ))
                }

                // Clean up demo peers
                await registry.remove(peerID: userPeerID)
                await registry.remove(peerID: guidePeerID)

                continuation.finish()
            }
        }
    }
}
