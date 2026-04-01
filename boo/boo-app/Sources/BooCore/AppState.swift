import Foundation
import Observation

/// Connection status to a Ghostty socket.
public enum ConnectionStatus: Sendable {
    case disconnected
    case connected
    case error
}

/// Shared app state coordinating GhosttyClient, PeerRegistry, MessageRelay, and SSHTunnelManager.
@MainActor
@Observable
public final class AppState {
    public private(set) var connectionStatus: ConnectionStatus = .disconnected
    public private(set) var discoveredTerminals: [TerminalInfo] = []
    public private(set) var peers: [PeerInfo] = []
    public private(set) var socketPath: String = ""
    public private(set) var tunnelStatuses: [(name: String, status: TunnelStatus)] = []

    private var client: GhosttyClient?
    private var remoteClients: [String: GhosttyClient] = [:]  // name -> client for tunneled sockets
    public let registry = PeerRegistry()
    public let relay: MessageRelay
    public let tunnelManager = SSHTunnelManager()

    public init() {
        self.relay = MessageRelay(registry: registry)
    }

    /// Connect to a Ghostty socket and discover terminals.
    public func connect(socketPath: String) async {
        self.socketPath = socketPath
        let newClient = GhosttyClient(socketPath: socketPath)

        do {
            let terminals = try newClient.listTerminals()
            self.client = newClient
            self.discoveredTerminals = terminals
            self.connectionStatus = .connected
        } catch {
            self.client = nil
            self.discoveredTerminals = []
            self.connectionStatus = .error
        }
    }

    /// Refresh terminal list from connected socket.
    public func refreshTerminals() async {
        guard let client else { return }
        do {
            self.discoveredTerminals = try client.listTerminals()
        } catch {
            self.connectionStatus = .error
        }
    }

    /// Register a peer in the registry.
    public func registerPeer(name: String, role: String? = nil) async -> String {
        await registry.register(name: name, role: role)
    }

    /// Refresh peers list from registry.
    public func refreshPeers() async {
        self.peers = await registry.listPeers()
    }

    /// Send a message between peers.
    public func sendMessage(from senderID: String, to recipientName: String, content: String) async throws {
        try await relay.send(from: senderID, to: recipientName, content: content)
    }

    /// Receive messages for a peer.
    public func receiveMessages(peerID: String) async -> [PeerMessage] {
        await relay.receive(peerID: peerID)
    }

    /// Broadcast a message to all peers.
    public func broadcast(from senderID: String, content: String) async throws -> [String] {
        try await relay.broadcast(from: senderID, content: content)
    }

    /// Send text to a terminal via the Ghostty socket.
    public func sendText(terminalID: String, text: String) throws {
        guard let client else { throw GhosttyClientError.connectionFailed }
        try client.sendText(terminalID: terminalID, text: text)
    }

    /// Read screen content from a terminal.
    public func readScreen(terminalID: String) throws -> [String] {
        guard let client else { throw GhosttyClientError.connectionFailed }
        return try client.readScreen(terminalID: terminalID)
    }

    // MARK: - Tunnel management

    /// Start an SSH tunnel to a remote machine.
    public func startTunnel(_ config: TunnelConfig) async {
        await tunnelManager.startTunnel(config)
        await refreshTunnelStatuses()
    }

    /// Stop a tunnel by machine name.
    public func stopTunnel(_ name: String) async {
        await tunnelManager.stopTunnel(name)
        remoteClients.removeValue(forKey: name)
        await refreshTunnelStatuses()
    }

    /// Stop all tunnels.
    public func stopAllTunnels() async {
        await tunnelManager.stopAll()
        remoteClients.removeAll()
        await refreshTunnelStatuses()
    }

    /// Refresh tunnel statuses and connect to ready tunnels.
    public func refreshTunnelStatuses() async {
        self.tunnelStatuses = await tunnelManager.allStatuses()

        // Auto-connect GhosttyClient to ready tunneled sockets
        for (name, status) in tunnelStatuses {
            if status == .connected, remoteClients[name] == nil {
                if let socketPath = await tunnelManager.localSocket(name) {
                    remoteClients[name] = GhosttyClient(socketPath: socketPath)
                }
            } else if status != .connected {
                remoteClients.removeValue(forKey: name)
            }
        }
    }
}
