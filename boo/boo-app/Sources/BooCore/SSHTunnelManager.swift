import Foundation

/// Configuration for an SSH tunnel forwarding a remote Unix socket to a local path.
public struct TunnelConfig: Sendable, Equatable {
    public let name: String           // Machine name (e.g., "mac-mini")
    public let sshHost: String        // SSH host (e.g., "beastoin-agents-f1-mac-mini")
    public let remoteSocket: String   // Remote Ghostty socket path
    public let localSocket: String    // Local forwarded socket path

    public init(name: String, sshHost: String, remoteSocket: String, localSocket: String? = nil) {
        self.name = name
        self.sshHost = sshHost
        self.remoteSocket = remoteSocket
        self.localSocket = localSocket ?? "/tmp/ghostty-\(name).sock"
    }
}

/// Tunnel connection state.
public enum TunnelStatus: Sendable, Equatable {
    case disconnected
    case connecting
    case connected
    case reconnecting
}

/// Manages SSH tunnels for forwarding remote Ghostty Unix sockets.
/// Uses ssh -NC -L for Unix socket forwarding with auto-reconnect.
public actor SSHTunnelManager {
    private var tunnels: [String: TunnelState] = [:]  // name -> state
    private let reconnectDelay: TimeInterval
    private let maxReconnectAttempts: Int

    private struct TunnelState {
        let config: TunnelConfig
        var process: Process?
        var status: TunnelStatus
        var reconnectCount: Int
        var shouldReconnect: Bool
    }

    public init(reconnectDelay: TimeInterval = 5, maxReconnectAttempts: Int = 0) {
        self.reconnectDelay = reconnectDelay
        self.maxReconnectAttempts = maxReconnectAttempts  // 0 = unlimited
    }

    /// Start a tunnel. Returns immediately; tunnel connects in background.
    public func startTunnel(_ config: TunnelConfig) {
        // Stop existing tunnel for this name
        stopTunnelSync(config.name)

        tunnels[config.name] = TunnelState(
            config: config, process: nil, status: .connecting,
            reconnectCount: 0, shouldReconnect: true
        )

        Task { [weak self] in
            await self?.connectTunnel(config.name)
        }
    }

    /// Stop a tunnel by name.
    public func stopTunnel(_ name: String) {
        stopTunnelSync(name)
    }

    /// Stop all tunnels.
    public func stopAll() {
        for name in tunnels.keys {
            stopTunnelSync(name)
        }
    }

    /// Get status of a specific tunnel.
    public func status(_ name: String) -> TunnelStatus {
        tunnels[name]?.status ?? .disconnected
    }

    /// Get all tunnel statuses.
    public func allStatuses() -> [(name: String, status: TunnelStatus)] {
        tunnels.map { (name: $0.key, status: $0.value.status) }
            .sorted { $0.name < $1.name }
    }

    /// Get the local socket path for a tunnel.
    public func localSocket(_ name: String) -> String? {
        tunnels[name]?.config.localSocket
    }

    /// Check if a tunnel's local socket exists and is connectable.
    public func isSocketReady(_ name: String) -> Bool {
        guard let config = tunnels[name]?.config else { return false }
        return FileManager.default.fileExists(atPath: config.localSocket)
    }

    // MARK: - Internal

    /// Build the ssh command arguments for a tunnel.
    nonisolated func buildSSHArgs(_ config: TunnelConfig) -> [String] {
        [
            "-N",                           // No remote command
            "-C",                           // Compression
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-L", "\(config.localSocket):\(config.remoteSocket)",
            config.sshHost,
        ]
    }

    private func connectTunnel(_ name: String) async {
        guard var state = tunnels[name] else { return }

        // Clean up stale local socket
        let localPath = state.config.localSocket
        if FileManager.default.fileExists(atPath: localPath) {
            try? FileManager.default.removeItem(atPath: localPath)
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = buildSSHArgs(state.config)

        // Suppress output
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        state.process = process
        state.status = .connecting
        tunnels[name] = state

        do {
            try process.run()
        } catch {
            tunnels[name]?.status = .disconnected
            await scheduleReconnect(name)
            return
        }

        // Wait briefly for socket to appear
        for _ in 0..<20 {
            try? await Task.sleep(nanoseconds: 250_000_000) // 250ms
            if FileManager.default.fileExists(atPath: localPath) {
                tunnels[name]?.status = .connected
                tunnels[name]?.reconnectCount = 0

                // Set socket permissions to 0600
                try? FileManager.default.setAttributes(
                    [.posixPermissions: 0o600], ofItemAtPath: localPath
                )

                // Monitor for tunnel death
                await monitorTunnel(name, process: process)
                return
            }
            if !process.isRunning {
                break
            }
        }

        // Socket didn't appear — connection failed
        if process.isRunning {
            process.terminate()
        }
        tunnels[name]?.status = .disconnected
        await scheduleReconnect(name)
    }

    private func monitorTunnel(_ name: String, process: Process) async {
        // Wait for process to exit
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            process.terminationHandler = { _ in
                continuation.resume()
            }
        }

        // Clean up socket
        if let config = tunnels[name]?.config {
            try? FileManager.default.removeItem(atPath: config.localSocket)
        }

        guard tunnels[name]?.shouldReconnect == true else {
            tunnels[name]?.status = .disconnected
            return
        }

        tunnels[name]?.status = .reconnecting
        await scheduleReconnect(name)
    }

    private func scheduleReconnect(_ name: String) async {
        guard var state = tunnels[name], state.shouldReconnect else { return }

        if maxReconnectAttempts > 0 && state.reconnectCount >= maxReconnectAttempts {
            state.status = .disconnected
            state.shouldReconnect = false
            tunnels[name] = state
            return
        }

        state.reconnectCount += 1
        state.status = .reconnecting
        tunnels[name] = state

        try? await Task.sleep(nanoseconds: UInt64(reconnectDelay * 1_000_000_000))

        guard tunnels[name]?.shouldReconnect == true else { return }
        await connectTunnel(name)
    }

    private func stopTunnelSync(_ name: String) {
        guard var state = tunnels[name] else { return }
        state.shouldReconnect = false
        if let process = state.process, process.isRunning {
            process.terminate()
        }
        // Clean up socket
        try? FileManager.default.removeItem(atPath: state.config.localSocket)
        state.status = .disconnected
        state.process = nil
        tunnels[name] = state
    }
}
