import Foundation

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
    /// Uses FileManager only — NSWorkspace LaunchServices cache persists after deletion.
    public func detectGhosttyBooInstall() -> Bool {
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
        paths.append("/tmp/ghostty-boo.sock")
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

    // MARK: - Agent Identity

    public struct AgentIdentity: Sendable {
        public let name: String
        public let quote: String
        public let source: String  // "claude" or "fallback"
    }

    /// Detect Claude Code CLI and ask for a fun agent name + quote.
    /// Falls back to a random fun name if CLI is unavailable.
    public func suggestAgentIdentity() -> AgentIdentity {
        let hasClaude = FileManager.default.isExecutableFile(atPath: "/usr/local/bin/claude")
            || shellWhich("claude") != nil

        if hasClaude, let result = askCLI(command: "claude", args: [
            "-p",
            "You are naming an AI agent for a macOS app called Boo. Reply with EXACTLY two lines, nothing else. Line 1: a short creative agent name (2-3 words, fun/quirky, no quotes). Line 2: a funny one-liner quote about AI agents (no quotes)."
        ]) {
            return result
        }

        // Fallback: random fun name
        let adjectives = ["Cosmic", "Sneaky", "Turbo", "Lazy", "Quantum", "Pixel", "Neon", "Chill", "Hyper", "Fuzzy"]
        let nouns = ["Panda", "Ghost", "Falcon", "Otter", "Phoenix", "Raccoon", "Penguin", "Fox", "Owl", "Cat"]
        let quotes = [
            "I dream of electric sheep... and fast builds.",
            "sudo make me a sandwich.",
            "Have you tried turning it off and on again?",
            "I'm not a bug, I'm a feature.",
            "01001000 01101001 (that's 'Hi' in binary).",
        ]
        return AgentIdentity(
            name: "\(adjectives.randomElement()!) \(nouns.randomElement()!)",
            quote: quotes.randomElement()!,
            source: "fallback"
        )
    }

    private func shellWhich(_ command: String) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        process.arguments = [command]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            return path?.isEmpty == false ? path : nil
        } catch { return nil }
    }

    private func askCLI(command: String, args: [String]) -> AgentIdentity? {
        let process = Process()
        if let path = shellWhich(command) {
            process.executableURL = URL(fileURLWithPath: path)
        } else {
            process.executableURL = URL(fileURLWithPath: "/usr/local/bin/\(command)")
        }
        process.arguments = args
        process.environment = ProcessInfo.processInfo.environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            // 15s timeout — don't block onboarding
            DispatchQueue.global().asyncAfter(deadline: .now() + 15) {
                if process.isRunning { process.terminate() }
            }
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return nil }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            guard let output = String(data: data, encoding: .utf8) else { return nil }
            let lines = output.trimmingCharacters(in: .whitespacesAndNewlines)
                .components(separatedBy: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            guard lines.count >= 2 else { return nil }
            return AgentIdentity(name: lines[0], quote: lines[1], source: command)
        } catch { return nil }
    }

    /// Generate default peer name from user@machine (legacy fallback).
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
