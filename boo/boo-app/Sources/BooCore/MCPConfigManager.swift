import Foundation

/// Manages Claude Code MCP configuration for boo-app auto-setup.
/// Reads/writes ~/.claude/settings.json to register boo-app as an MCP server.
public struct MCPConfigManager: Sendable {
    public let configPath: String

    public init(configPath: String? = nil) {
        self.configPath = configPath ?? NSHomeDirectory() + "/.claude/settings.json"
    }

    /// Check if boo-app is already registered as an MCP server.
    public func isInstalled() -> Bool {
        guard let config = readConfig() else { return false }
        guard let servers = config["mcpServers"] as? [String: Any] else { return false }
        return servers["boo"] != nil
    }

    /// Register boo-app as an MCP server in Claude Code config.
    /// Returns true if config was modified, false if already installed.
    @discardableResult
    public func install(binaryPath: String) throws -> Bool {
        var config = readConfig() ?? [:]
        var servers = config["mcpServers"] as? [String: Any] ?? [:]

        if servers["boo"] != nil {
            return false // Already installed
        }

        servers["boo"] = [
            "command": binaryPath,
            "args": ["--mcp"],
        ] as [String: Any]

        config["mcpServers"] = servers
        try writeConfig(config)
        return true
    }

    /// Remove boo-app from Claude Code MCP config.
    /// Returns true if config was modified, false if not installed.
    @discardableResult
    public func uninstall() throws -> Bool {
        guard var config = readConfig() else { return false }
        guard var servers = config["mcpServers"] as? [String: Any] else { return false }

        guard servers.removeValue(forKey: "boo") != nil else { return false }

        config["mcpServers"] = servers
        try writeConfig(config)
        return true
    }

    /// Get the current boo-app MCP server config entry, if installed.
    public func currentEntry() -> [String: Any]? {
        guard let config = readConfig() else { return nil }
        guard let servers = config["mcpServers"] as? [String: Any] else { return nil }
        return servers["boo"] as? [String: Any]
    }

    /// Check if the registered binary path matches the current bundle.
    public func isStale() -> Bool {
        guard let entry = currentEntry(),
              let registeredPath = entry["command"] as? String else { return false }
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

    private func readConfig() -> [String: Any]? {
        guard let data = FileManager.default.contents(atPath: configPath),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return json
    }

    private func writeConfig(_ config: [String: Any]) throws {
        let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
        let dir = (configPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)

        // Write atomically via temp file
        let tempPath = configPath + ".tmp"
        try data.write(to: URL(fileURLWithPath: tempPath))

        // Set secure permissions (0600)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tempPath)

        // Atomic rename
        if FileManager.default.fileExists(atPath: configPath) {
            _ = try FileManager.default.replaceItemAt(URL(fileURLWithPath: configPath), withItemAt: URL(fileURLWithPath: tempPath))
        } else {
            try FileManager.default.moveItem(atPath: tempPath, toPath: configPath)
        }
    }
}
