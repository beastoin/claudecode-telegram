import Foundation

/// Manages Claude Code MCP configuration for boo-app auto-setup.
/// Writes to ~/.claude.json (user-scope mcpServers) which is where Claude Code
/// reads MCP server configs from. Also supports legacy ~/.claude/settings.json.
public struct MCPConfigManager: Sendable {
    /// Primary config: ~/.claude.json (Claude Code's user-level config)
    public let configPath: String
    /// Legacy config: ~/.claude/settings.json (older Claude Code versions)
    public let legacyConfigPath: String

    public init(configPath: String? = nil, legacyConfigPath: String? = nil) {
        self.configPath = configPath ?? NSHomeDirectory() + "/.claude.json"
        self.legacyConfigPath = legacyConfigPath ?? NSHomeDirectory() + "/.claude/settings.json"
    }

    /// Check if boo-app is already registered as an MCP server.
    public func isInstalled() -> Bool {
        // Check primary (.claude.json user-scope mcpServers)
        if let config = readConfig(configPath),
           let servers = config["mcpServers"] as? [String: Any],
           servers["boo"] != nil {
            return true
        }
        // Check legacy (~/.claude/settings.json)
        if let config = readConfig(legacyConfigPath),
           let servers = config["mcpServers"] as? [String: Any],
           servers["boo"] != nil {
            return true
        }
        return false
    }

    /// Register boo-app as an MCP server in Claude Code config.
    /// Writes to ~/.claude.json (user-scope) so it's available from any directory.
    @discardableResult
    public func install(binaryPath: String) throws -> Bool {
        var config = readConfig(configPath) ?? [:]
        var servers = config["mcpServers"] as? [String: Any] ?? [:]

        if servers["boo"] != nil {
            return false // Already installed
        }

        servers["boo"] = [
            "type": "stdio",
            "command": binaryPath,
            "args": ["--mcp"],
            "env": [:] as [String: String],
        ] as [String: Any]

        config["mcpServers"] = servers
        try writeConfig(config, to: configPath)

        // Also write to legacy path for older Claude Code versions
        installLegacy(binaryPath: binaryPath)

        return true
    }

    /// Remove boo-app from Claude Code MCP config.
    @discardableResult
    public func uninstall() throws -> Bool {
        var removed = false

        // Remove from primary
        if var config = readConfig(configPath),
           var servers = config["mcpServers"] as? [String: Any],
           servers.removeValue(forKey: "boo") != nil {
            config["mcpServers"] = servers
            try writeConfig(config, to: configPath)
            removed = true
        }

        // Remove from legacy
        if var config = readConfig(legacyConfigPath),
           var servers = config["mcpServers"] as? [String: Any],
           servers.removeValue(forKey: "boo") != nil {
            config["mcpServers"] = servers
            try writeConfig(config, to: legacyConfigPath)
            removed = true
        }

        return removed
    }

    /// Get the current boo-app MCP server config entry, if installed.
    public func currentEntry() -> [String: Any]? {
        // Check primary first
        if let config = readConfig(configPath),
           let servers = config["mcpServers"] as? [String: Any],
           let entry = servers["boo"] as? [String: Any] {
            return entry
        }
        // Fallback to legacy
        if let config = readConfig(legacyConfigPath),
           let servers = config["mcpServers"] as? [String: Any],
           let entry = servers["boo"] as? [String: Any] {
            return entry
        }
        return nil
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

    /// Install to legacy settings.json (best-effort, errors ignored).
    private func installLegacy(binaryPath: String) {
        var config = readConfig(legacyConfigPath) ?? [:]
        var servers = config["mcpServers"] as? [String: Any] ?? [:]
        servers["boo"] = [
            "command": binaryPath,
            "args": ["--mcp"],
        ] as [String: Any]
        config["mcpServers"] = servers
        try? writeConfig(config, to: legacyConfigPath)
    }

    private func readConfig(_ path: String) -> [String: Any]? {
        guard let data = FileManager.default.contents(atPath: path),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return json
    }

    private func writeConfig(_ config: [String: Any], to path: String) throws {
        let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
        let dir = (path as NSString).deletingLastPathComponent
        if !dir.isEmpty && dir != "." {
            try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        }

        // Write atomically via temp file
        let tempPath = path + ".tmp"
        try data.write(to: URL(fileURLWithPath: tempPath))

        // Set secure permissions (0600)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tempPath)

        // Atomic rename
        if FileManager.default.fileExists(atPath: path) {
            _ = try FileManager.default.replaceItemAt(URL(fileURLWithPath: path), withItemAt: URL(fileURLWithPath: tempPath))
        } else {
            try FileManager.default.moveItem(atPath: tempPath, toPath: path)
        }
    }
}
