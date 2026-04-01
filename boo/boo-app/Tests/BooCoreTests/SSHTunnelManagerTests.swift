import Testing
import Foundation
@testable import BooCore

@Suite("SSHTunnelManager")
struct SSHTunnelManagerTests {

    // MARK: - TunnelConfig

    @Test("TunnelConfig defaults local socket to /tmp/ghostty-<name>.sock")
    func configDefaultLocalSocket() {
        let config = TunnelConfig(name: "mac-mini", sshHost: "host", remoteSocket: "/tmp/ghostty.sock")
        #expect(config.localSocket == "/tmp/ghostty-mac-mini.sock")
    }

    @Test("TunnelConfig accepts custom local socket")
    func configCustomLocalSocket() {
        let config = TunnelConfig(name: "mac-mini", sshHost: "host", remoteSocket: "/tmp/ghostty.sock",
                                  localSocket: "/tmp/custom.sock")
        #expect(config.localSocket == "/tmp/custom.sock")
    }

    @Test("TunnelConfig equality")
    func configEquality() {
        let a = TunnelConfig(name: "a", sshHost: "host", remoteSocket: "/tmp/a.sock")
        let b = TunnelConfig(name: "a", sshHost: "host", remoteSocket: "/tmp/a.sock")
        let c = TunnelConfig(name: "b", sshHost: "host", remoteSocket: "/tmp/a.sock")
        #expect(a == b)
        #expect(a != c)
    }

    // MARK: - Manager lifecycle

    @Test("New manager has no tunnels")
    func emptyManager() async {
        let mgr = SSHTunnelManager()
        let statuses = await mgr.allStatuses()
        #expect(statuses.isEmpty)
    }

    @Test("Status returns disconnected for unknown tunnel")
    func unknownTunnelStatus() async {
        let mgr = SSHTunnelManager()
        let status = await mgr.status("nonexistent")
        #expect(status == .disconnected)
    }

    @Test("localSocket returns nil for unknown tunnel")
    func unknownLocalSocket() async {
        let mgr = SSHTunnelManager()
        let socket = await mgr.localSocket("nonexistent")
        #expect(socket == nil)
    }

    @Test("isSocketReady returns false for unknown tunnel")
    func unknownSocketReady() async {
        let mgr = SSHTunnelManager()
        let ready = await mgr.isSocketReady("nonexistent")
        #expect(!ready)
    }

    // MARK: - SSH command building

    @Test("buildSSHArgs produces correct ssh arguments")
    func sshArgs() async {
        let mgr = SSHTunnelManager()
        let config = TunnelConfig(
            name: "mac-mini",
            sshHost: "beastoin-agents-f1-mac-mini",
            remoteSocket: "/tmp/ghostty-test.sock",
            localSocket: "/tmp/ghostty-mac-mini.sock"
        )
        let args = mgr.buildSSHArgs(config)

        #expect(args.contains("-N"))
        #expect(args.contains("-C"))
        #expect(args.contains("beastoin-agents-f1-mac-mini"))
        #expect(args.contains("-L"))
        #expect(args.contains("/tmp/ghostty-mac-mini.sock:/tmp/ghostty-test.sock"))
        #expect(args.contains("ExitOnForwardFailure=yes"))
        #expect(args.contains("ServerAliveInterval=30"))
    }

    // MARK: - Tunnel start/stop with fake host

    @Test("startTunnel then stopTunnel cleans up")
    func startStop() async throws {
        let mgr = SSHTunnelManager(reconnectDelay: 0.1, maxReconnectAttempts: 1)
        let config = TunnelConfig(
            name: "test",
            sshHost: "nonexistent-host-for-test",
            remoteSocket: "/tmp/ghostty.sock",
            localSocket: "/tmp/boo-test-\(UUID().uuidString).sock"
        )

        await mgr.startTunnel(config)

        // Give it a moment to attempt connection (will fail quickly)
        try await Task.sleep(nanoseconds: 500_000_000)

        // Stop should clean up
        await mgr.stopTunnel("test")
        let status = await mgr.status("test")
        #expect(status == .disconnected)

        // Socket should be cleaned up
        #expect(!FileManager.default.fileExists(atPath: config.localSocket))
    }

    @Test("stopAll stops all tunnels")
    func stopAll() async throws {
        let mgr = SSHTunnelManager(reconnectDelay: 60, maxReconnectAttempts: 1)

        let configA = TunnelConfig(name: "a", sshHost: "fake-a", remoteSocket: "/tmp/a.sock",
                                   localSocket: "/tmp/boo-test-a-\(UUID().uuidString).sock")
        let configB = TunnelConfig(name: "b", sshHost: "fake-b", remoteSocket: "/tmp/b.sock",
                                   localSocket: "/tmp/boo-test-b-\(UUID().uuidString).sock")

        await mgr.startTunnel(configA)
        await mgr.startTunnel(configB)
        try await Task.sleep(nanoseconds: 300_000_000)

        await mgr.stopAll()

        let statusA = await mgr.status("a")
        let statusB = await mgr.status("b")
        #expect(statusA == .disconnected)
        #expect(statusB == .disconnected)
    }

    @Test("allStatuses returns sorted list")
    func allStatusesSorted() async throws {
        let mgr = SSHTunnelManager(reconnectDelay: 60, maxReconnectAttempts: 1)

        await mgr.startTunnel(TunnelConfig(name: "zulu", sshHost: "fake", remoteSocket: "/tmp/z.sock",
                                           localSocket: "/tmp/boo-test-z-\(UUID().uuidString).sock"))
        await mgr.startTunnel(TunnelConfig(name: "alpha", sshHost: "fake", remoteSocket: "/tmp/a.sock",
                                           localSocket: "/tmp/boo-test-a-\(UUID().uuidString).sock"))

        try await Task.sleep(nanoseconds: 200_000_000)

        let statuses = await mgr.allStatuses()
        #expect(statuses.count == 2)
        #expect(statuses[0].name == "alpha")
        #expect(statuses[1].name == "zulu")

        await mgr.stopAll()
    }

    @Test("localSocket returns configured path")
    func localSocketPath() async {
        let mgr = SSHTunnelManager(reconnectDelay: 60, maxReconnectAttempts: 1)
        let config = TunnelConfig(name: "test", sshHost: "fake", remoteSocket: "/tmp/r.sock",
                                  localSocket: "/tmp/boo-test-local-\(UUID().uuidString).sock")
        await mgr.startTunnel(config)
        let path = await mgr.localSocket("test")
        #expect(path == config.localSocket)
        await mgr.stopAll()
    }

    // MARK: - TunnelStatus

    @Test("TunnelStatus equality")
    func statusEquality() {
        #expect(TunnelStatus.connected == TunnelStatus.connected)
        #expect(TunnelStatus.connected != TunnelStatus.disconnected)
        #expect(TunnelStatus.connecting != TunnelStatus.reconnecting)
    }
}
