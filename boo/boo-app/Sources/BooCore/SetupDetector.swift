import Foundation
#if canImport(AppKit)
import AppKit
#endif

/// Probe result for a single setup check.
public enum ProbeState: Sendable {
    case pending
    case running
    case passed(String)    // detail message
    case failed(String)    // error message
    case skipped(String)   // reason
}

/// Result of SSH config parsing.
public struct SSHHost: Sendable, Identifiable {
    public let id: String       // SSH config alias
    public let alias: String
    public let hostname: String
    public let user: String
    public let identityFile: String?

    public init(id: String, alias: String, hostname: String, user: String, identityFile: String? = nil) {
        self.id = id
        self.alias = alias
        self.hostname = hostname
        self.user = user
        self.identityFile = identityFile
    }
}

/// Actor that runs all auto-detection probes for onboarding.
public actor SetupDetector {

    public init() {}

    // MARK: - Ghostty Detection

    /// Check if Ghostty Boo is installed. Only Ghostty Boo counts — original Ghostty is ignored.
    public func detectGhosttyBooInstall() -> Bool {
        #if canImport(AppKit)
        if NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.beastoin.ghostty-boo") != nil {
            return true
        }
        #endif
        let paths = [
            "/Applications/Ghostty Boo.app",
            NSHomeDirectory() + "/Applications/Ghostty Boo.app",
        ]
        return paths.contains { FileManager.default.fileExists(atPath: $0) }
    }

    // MARK: - Socket Probe

    /// Find and validate a Ghostty control socket by actually connecting.
    /// Returns the working socket path and discovered terminals.
    public func probeSocket() -> (path: String?, terminals: [TerminalInfo]) {
        let candidates = collectSocketCandidates()
        for path in candidates {
            let client = GhosttyClient(socketPath: path)
            if let terminals = try? client.listTerminals() {
                return (path, terminals)
            }
        }
        return (nil, [])
    }

    /// Collect candidate socket paths from standard locations and Ghostty config.
    private func collectSocketCandidates() -> [String] {
        var paths: [String] = []

        // Standard locations
        paths.append("/tmp/ghostty.sock")
        paths.append("/tmp/ghostty-test.sock")

        // Glob /tmp/ghostty-*.sock
        if let enumerator = FileManager.default.enumerator(atPath: "/tmp") {
            while let file = enumerator.nextObject() as? String {
                enumerator.skipDescendants()
                if file.hasPrefix("ghostty") && file.hasSuffix(".sock") {
                    let full = "/tmp/" + file
                    if !paths.contains(full) {
                        paths.append(full)
                    }
                }
            }
        }

        // Read from Ghostty config
        let configPaths = [
            NSHomeDirectory() + "/.config/ghostty/config",
            NSHomeDirectory() + "/Library/Application Support/com.mitchellh.ghostty/config",
        ]
        for configPath in configPaths {
            if let contents = try? String(contentsOfFile: configPath, encoding: .utf8) {
                for line in contents.split(separator: "\n") {
                    let trimmed = line.trimmingCharacters(in: .whitespaces)
                    if trimmed.hasPrefix("control-socket") {
                        let parts = trimmed.split(separator: "=", maxSplits: 1)
                        if parts.count == 2 {
                            let socketPath = parts[1].trimmingCharacters(in: .whitespaces)
                            if !paths.contains(socketPath) {
                                paths.insert(socketPath, at: 0) // prioritize config
                            }
                        }
                    }
                }
            }
        }

        return paths
    }

    // MARK: - Claude MCP

    /// Check if Claude Code MCP is configured and whether it's stale.
    public func checkClaudeMCP() -> (installed: Bool, stale: Bool) {
        let manager = MCPConfigManager()
        guard manager.isInstalled() else { return (false, false) }
        return (true, manager.isStale())
    }

    // MARK: - Codex MCP

    /// Check if Codex CLI MCP is configured.
    /// Returns (installed, stale). If Codex CLI is not installed, returns nil (skip).
    public func checkCodexMCP() -> (codexInstalled: Bool, mcpConfigured: Bool, stale: Bool) {
        let manager = CodexConfigManager()
        let codexExists = manager.isCodexInstalled()
        guard codexExists else { return (false, false, false) }
        let configured = manager.isInstalled()
        let stale = configured ? manager.isStale() : false
        return (true, configured, stale)
    }

    // MARK: - Peer Name

    /// Generate default peer name from user@machine.
    public func defaultPeerName() -> String {
        let user = NSUserName()
        let machine = Host.current().localizedName ?? "unknown"
        return "\(user)@\(machine)"
    }

    // MARK: - SSH Config Discovery

    /// Parse ~/.ssh/config for host entries.
    public func detectSSHHosts() -> [SSHHost] {
        let sshConfigPath = NSHomeDirectory() + "/.ssh/config"
        guard let contents = try? String(contentsOfFile: sshConfigPath, encoding: .utf8) else {
            return []
        }
        return parseSSHConfig(contents)
    }

    /// Parse SSH config text into host entries.
    public func parseSSHConfig(_ text: String) -> [SSHHost] {
        var hosts: [SSHHost] = []
        var currentAlias: String?
        var currentHostname: String?
        var currentUser: String?
        var currentIdentityFile: String?

        func flushHost() {
            if let alias = currentAlias, !alias.contains("*") {
                let hostname = currentHostname ?? alias
                let user = currentUser ?? NSUserName()
                hosts.append(SSHHost(
                    id: alias,
                    alias: alias,
                    hostname: hostname,
                    user: user,
                    identityFile: currentIdentityFile
                ))
            }
            currentAlias = nil
            currentHostname = nil
            currentUser = nil
            currentIdentityFile = nil
        }

        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }

            if trimmed.lowercased().hasPrefix("host ") {
                flushHost()
                let value = trimmed.dropFirst(5).trimmingCharacters(in: .whitespaces)
                currentAlias = value
            } else if trimmed.lowercased().hasPrefix("hostname ") {
                currentHostname = trimmed.dropFirst(9).trimmingCharacters(in: .whitespaces)
            } else if trimmed.lowercased().hasPrefix("user ") {
                currentUser = trimmed.dropFirst(5).trimmingCharacters(in: .whitespaces)
            } else if trimmed.lowercased().hasPrefix("identityfile ") {
                currentIdentityFile = trimmed.dropFirst(13).trimmingCharacters(in: .whitespaces)
            }
        }
        flushHost()

        return hosts
    }
}
