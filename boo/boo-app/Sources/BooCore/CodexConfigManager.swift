import Foundation

/// Manages Codex CLI MCP configuration for boo-app auto-setup.
/// Reads/writes ~/.codex/config.toml to register boo-app as an MCP server.
///
/// Codex uses TOML format with [mcp_servers.<name>] tables:
///   [mcp_servers.boo]
///   command = "/path/to/BooApp"
///   args = ["--mcp"]
public struct CodexConfigManager: Sendable {
    public let configPath: String

    public init(configPath: String? = nil) {
        self.configPath = configPath ?? NSHomeDirectory() + "/.codex/config.toml"
    }

    /// Check if Codex CLI binary is available on the system.
    public func isCodexInstalled() -> Bool {
        let paths = [
            "/usr/local/bin/codex",
            NSHomeDirectory() + "/bin/codex",
            NSHomeDirectory() + "/.local/bin/codex",
            "/opt/homebrew/bin/codex",
        ]
        for path in paths {
            if FileManager.default.isExecutableFile(atPath: path) {
                return true
            }
        }
        // Check via PATH using `which`
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        process.arguments = ["codex"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus == 0
        } catch {
            return false
        }
    }

    /// Check if boo-app is already registered as an MCP server in Codex config.
    public func isInstalled() -> Bool {
        guard let contents = readConfig() else { return false }
        return contents.contains("[mcp_servers.boo]")
    }

    /// Register boo-app as an MCP server in Codex CLI config.
    /// Returns true if config was modified, false if already installed.
    @discardableResult
    public func install(binaryPath: String) throws -> Bool {
        var contents = readConfig() ?? ""

        if contents.contains("[mcp_servers.boo]") {
            return false
        }

        let entry = """

        [mcp_servers.boo]
        command = "\(binaryPath)"
        args = ["--mcp"]
        """

        contents += entry
        try writeConfig(contents)
        return true
    }

    /// Remove boo-app from Codex CLI MCP config.
    /// Returns true if config was modified, false if not installed.
    @discardableResult
    public func uninstall() throws -> Bool {
        guard var contents = readConfig() else { return false }
        guard contents.contains("[mcp_servers.boo]") else { return false }

        // Remove the [mcp_servers.boo] section and its keys until next section or EOF
        let lines = contents.components(separatedBy: "\n")
        var newLines: [String] = []
        var inBooSection = false

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "[mcp_servers.boo]" {
                inBooSection = true
                continue
            }
            if inBooSection {
                // New section starts — stop removing
                if trimmed.hasPrefix("[") {
                    inBooSection = false
                    newLines.append(line)
                }
                // Skip lines within boo section (command, args, blank lines)
                continue
            }
            newLines.append(line)
        }

        // Clean up trailing blank lines
        while newLines.last?.trimmingCharacters(in: .whitespaces).isEmpty == true {
            newLines.removeLast()
        }

        contents = newLines.joined(separator: "\n")
        if !contents.isEmpty {
            contents += "\n"
        }
        try writeConfig(contents)
        return true
    }

    /// Check if the registered binary path matches the current bundle.
    public func isStale() -> Bool {
        guard let contents = readConfig(),
              contents.contains("[mcp_servers.boo]") else { return false }

        // Extract command value from the boo section
        guard let registeredPath = extractCommand(from: contents) else { return false }
        let currentPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
        return registeredPath != currentPath
    }

    /// Repair stale registration by re-installing with current binary path.
    @discardableResult
    public func repair() throws -> Bool {
        try uninstall()
        let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
        return try install(binaryPath: binaryPath)
    }

    // MARK: - Private

    /// Extract the command value from the [mcp_servers.boo] section.
    private func extractCommand(from contents: String) -> String? {
        let lines = contents.components(separatedBy: "\n")
        var inBooSection = false

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "[mcp_servers.boo]" {
                inBooSection = true
                continue
            }
            if inBooSection {
                if trimmed.hasPrefix("[") { break }
                if trimmed.hasPrefix("command") {
                    let parts = trimmed.split(separator: "=", maxSplits: 1)
                    if parts.count == 2 {
                        var value = parts[1].trimmingCharacters(in: .whitespaces)
                        // Remove surrounding quotes
                        if value.hasPrefix("\"") && value.hasSuffix("\"") {
                            value = String(value.dropFirst().dropLast())
                        }
                        return value
                    }
                }
            }
        }
        return nil
    }

    private func readConfig() -> String? {
        try? String(contentsOfFile: configPath, encoding: .utf8)
    }

    private func writeConfig(_ contents: String) throws {
        let dir = (configPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        let tempPath = configPath + ".tmp"
        try contents.write(toFile: tempPath, atomically: false, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tempPath)

        if FileManager.default.fileExists(atPath: configPath) {
            _ = try FileManager.default.replaceItemAt(
                URL(fileURLWithPath: configPath),
                withItemAt: URL(fileURLWithPath: tempPath)
            )
        } else {
            try FileManager.default.moveItem(atPath: tempPath, toPath: configPath)
        }
    }
}
