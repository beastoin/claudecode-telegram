import Foundation

/// Provision step in the remote setup pipeline.
public enum ProvisionStep: String, Sendable {
    case connecting = "CONNECTING"
    case detecting = "DETECTING"
    case installing = "INSTALLING"
    case configuring = "CONFIGURING"
    case tunnel = "TUNNEL"
    case discovered = "PEER DISCOVERED"
}

/// Result of a provisioning step.
public enum StepResult: Sendable {
    case inProgress(ProvisionStep)
    case completed(ProvisionStep, String)
    case failed(ProvisionStep, String)
}

/// Actor that handles SSH discovery and one-click remote provisioning.
public actor RemoteProvisioner {

    /// Default Ghostty Boo release URL. Override via init parameter.
    public static let defaultReleaseURL = "https://github.com/beastoin/claudecode-telegram/releases/download/boo-v0.2.0/GhosttyBoo-macOS-arm64.zip"

    public init() {}

    /// Test SSH connectivity to a host.
    public func testConnection(host: SSHHost) async -> Bool {
        let result = await runSSH(host: host.alias, command: "echo ok", timeout: 5)
        return result?.trimmingCharacters(in: .whitespacesAndNewlines) == "ok"
    }

    /// Check if Ghostty is installed on the remote machine and whether control-socket is configured.
    public func detectRemoteGhostty(host: SSHHost) async -> (installed: Bool, hasSocket: Bool, variant: String?) {
        // Check for Ghostty apps
        let appCheck = await runSSH(
            host: host.alias,
            command: "ls -d /Applications/Ghostty\\ Boo.app /Applications/Ghostty.app ~/Applications/Ghostty\\ Boo.app ~/Applications/Ghostty.app 2>/dev/null; true"
        )

        let apps = appCheck?.components(separatedBy: "\n").filter { !$0.isEmpty } ?? []
        let hasBoo = apps.contains { $0.contains("Ghostty Boo") }
        let hasGhostty = !apps.isEmpty

        guard hasGhostty else { return (false, false, nil) }

        // Check for control-socket in config
        let configCheck = await runSSH(
            host: host.alias,
            command: "grep -l control-socket ~/.config/ghostty/config ~/Library/Application\\ Support/com.mitchellh.ghostty/config 2>/dev/null; true"
        )
        let hasSocket = !(configCheck?.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ?? true)
        let variant = hasBoo ? "Ghostty Boo" : "Ghostty"

        return (true, hasSocket, variant)
    }

    /// Run the full provisioning pipeline for a host.
    public func provision(
        host: SSHHost,
        tunnelManager: SSHTunnelManager,
        releaseURL: String
    ) -> AsyncStream<StepResult> {
        AsyncStream { continuation in
            Task {
                // Step 1: Connect
                continuation.yield(.inProgress(.connecting))
                let connected = await testConnection(host: host)
                guard connected else {
                    continuation.yield(.failed(.connecting, "Connection failed — check SSH key"))
                    continuation.finish()
                    return
                }
                continuation.yield(.completed(.connecting, "SSH connected"))

                // Step 2: Detect
                continuation.yield(.inProgress(.detecting))
                let (installed, hasSocket, variant) = await detectRemoteGhostty(host: host)

                if installed && hasSocket {
                    continuation.yield(.completed(.detecting, "\(variant ?? "Ghostty") with control-socket"))
                    // Skip install and configure
                } else if installed {
                    continuation.yield(.completed(.detecting, "\(variant ?? "Ghostty") found (no control-socket)"))

                    // Step 4: Configure control-socket
                    continuation.yield(.inProgress(.configuring))
                    let configOK = await configureRemoteSocket(host: host)
                    guard configOK else {
                        continuation.yield(.failed(.configuring, "Failed to configure control-socket"))
                        continuation.finish()
                        return
                    }
                    continuation.yield(.completed(.configuring, "control-socket enabled"))
                } else {
                    continuation.yield(.completed(.detecting, "No Ghostty found"))

                    // Step 3: Install
                    continuation.yield(.inProgress(.installing))
                    let installOK = await installGhosttyBoo(host: host, releaseURL: releaseURL)
                    guard installOK else {
                        continuation.yield(.failed(.installing, "Installation failed"))
                        continuation.finish()
                        return
                    }
                    continuation.yield(.completed(.installing, "Ghostty Boo installed"))

                    // Step 4: Configure
                    continuation.yield(.inProgress(.configuring))
                    let configOK = await configureRemoteSocket(host: host)
                    guard configOK else {
                        continuation.yield(.failed(.configuring, "Failed to configure control-socket"))
                        continuation.finish()
                        return
                    }
                    continuation.yield(.completed(.configuring, "control-socket enabled"))
                }

                // Step 5: Tunnel
                continuation.yield(.inProgress(.tunnel))
                let tunnelConfig = TunnelConfig(
                    name: host.alias,
                    sshHost: host.alias,
                    remoteSocket: "/tmp/ghostty-boo.sock"
                )
                await tunnelManager.startTunnel(tunnelConfig)

                // Wait for socket to appear (up to 5s)
                let localSocket = "/tmp/ghostty-\(host.alias).sock"
                var socketReady = false
                for _ in 0..<10 {
                    try? await Task.sleep(for: .milliseconds(500))
                    if FileManager.default.fileExists(atPath: localSocket) {
                        // Validate connectivity
                        let client = GhosttyClient(socketPath: localSocket)
                        if let _ = try? client.listTerminals() {
                            socketReady = true
                            break
                        }
                    }
                }

                guard socketReady else {
                    continuation.yield(.failed(.tunnel, "Tunnel connected but socket not responding"))
                    continuation.finish()
                    return
                }
                continuation.yield(.completed(.tunnel, "Tunnel active"))

                // Step 6: Peer discovered
                continuation.yield(.inProgress(.discovered))
                let client = GhosttyClient(socketPath: localSocket)
                let terminalCount = (try? client.listTerminals().count) ?? 0
                continuation.yield(.completed(.discovered, "\(terminalCount) terminal\(terminalCount == 1 ? "" : "s") on \(host.alias)"))

                continuation.finish()
            }
        }
    }

    // MARK: - Private SSH helpers

    private func configureRemoteSocket(host: SSHHost) async -> Bool {
        let result = await runSSH(
            host: host.alias,
            command: "mkdir -p ~/.config/ghostty && grep -q 'control-socket' ~/.config/ghostty/config 2>/dev/null || echo 'control-socket = /tmp/ghostty-boo.sock' >> ~/.config/ghostty/config"
        )
        return result != nil
    }

    private func installGhosttyBoo(host: SSHHost, releaseURL: String) async -> Bool {
        // Download and unzip
        let downloadCmd = """
        curl -fSL -o /tmp/GhosttyBoo.zip '\(releaseURL)' && \
        unzip -o /tmp/GhosttyBoo.zip -d /tmp/GhosttyBoo && \
        mv /tmp/GhosttyBoo/*.app /Applications/ && \
        rm -rf /tmp/GhosttyBoo /tmp/GhosttyBoo.zip
        """
        let result = await runSSH(host: host.alias, command: downloadCmd, timeout: 60)
        return result != nil
    }

    /// Run a command on a remote host via SSH. Returns stdout or nil on failure.
    private func runSSH(host: String, command: String, timeout: Int = 10) async -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = [
            "-o", "ConnectTimeout=\(timeout)",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            command,
        ]

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        do {
            try process.run()
        } catch {
            return nil
        }

        // Wait with timeout
        let deadline = Date().addingTimeInterval(TimeInterval(timeout + 5))
        while process.isRunning && Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }

        if process.isRunning {
            process.terminate()
            return nil
        }

        guard process.terminationStatus == 0 else { return nil }
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)
    }
}
