import Testing
import Foundation
@testable import BooCore

@Suite("MCPConfigManager")
struct MCPConfigManagerTests {

    private func tempConfigPath() -> String {
        let dir = NSTemporaryDirectory() + "boo-test-\(UUID().uuidString)"
        try! FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        return dir + "/settings.json"
    }

    @Test("isInstalled returns false when config file doesn't exist")
    func notInstalledNoFile() {
        let mgr = MCPConfigManager(configPath: "/tmp/nonexistent-\(UUID().uuidString)/settings.json")
        #expect(!mgr.isInstalled())
    }

    @Test("isInstalled returns false when boo entry missing")
    func notInstalledNoBoo() throws {
        let path = tempConfigPath()
        let config: [String: Any] = ["mcpServers": ["other": ["command": "/usr/bin/other"]]]
        let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted])
        try data.write(to: URL(fileURLWithPath: path))

        let mgr = MCPConfigManager(configPath: path)
        #expect(!mgr.isInstalled())
    }

    @Test("install adds boo entry to empty config")
    func installEmpty() throws {
        let path = tempConfigPath()
        let mgr = MCPConfigManager(configPath: path)

        let modified = try mgr.install(binaryPath: "/usr/local/bin/BooApp")
        #expect(modified)
        #expect(mgr.isInstalled())

        let entry = mgr.currentEntry()!
        #expect(entry["command"] as? String == "/usr/local/bin/BooApp")
        #expect(entry["args"] as? [String] == ["--mcp"])
    }

    @Test("install adds boo entry preserving existing servers")
    func installPreservesOthers() throws {
        let path = tempConfigPath()
        let existing: [String: Any] = [
            "mcpServers": [
                "figma": ["command": "/usr/bin/npx", "args": ["-y", "mcp-figma"]]
            ],
            "otherSetting": true,
        ]
        let data = try JSONSerialization.data(withJSONObject: existing, options: [.prettyPrinted])
        try data.write(to: URL(fileURLWithPath: path))

        let mgr = MCPConfigManager(configPath: path)
        try mgr.install(binaryPath: "/usr/local/bin/BooApp")

        // Verify boo added
        #expect(mgr.isInstalled())

        // Verify figma preserved
        let configData = try Data(contentsOf: URL(fileURLWithPath: path))
        let config = try JSONSerialization.jsonObject(with: configData) as! [String: Any]
        let servers = config["mcpServers"] as! [String: Any]
        #expect(servers["figma"] != nil)
        #expect(servers["boo"] != nil)

        // Verify other settings preserved
        #expect(config["otherSetting"] as? Bool == true)
    }

    @Test("install is idempotent")
    func installIdempotent() throws {
        let path = tempConfigPath()
        let mgr = MCPConfigManager(configPath: path)

        let first = try mgr.install(binaryPath: "/usr/local/bin/BooApp")
        #expect(first)

        let second = try mgr.install(binaryPath: "/usr/local/bin/BooApp")
        #expect(!second) // No modification needed
    }

    @Test("uninstall removes boo entry")
    func uninstall() throws {
        let path = tempConfigPath()
        let mgr = MCPConfigManager(configPath: path)

        try mgr.install(binaryPath: "/usr/local/bin/BooApp")
        #expect(mgr.isInstalled())

        let removed = try mgr.uninstall()
        #expect(removed)
        #expect(!mgr.isInstalled())
    }

    @Test("uninstall returns false when not installed")
    func uninstallNotInstalled() throws {
        let path = tempConfigPath()
        let mgr = MCPConfigManager(configPath: path)

        let removed = try mgr.uninstall()
        #expect(!removed)
    }

    @Test("uninstall preserves other servers")
    func uninstallPreservesOthers() throws {
        let path = tempConfigPath()
        let existing: [String: Any] = [
            "mcpServers": [
                "figma": ["command": "/usr/bin/npx"],
                "boo": ["command": "/usr/local/bin/BooApp", "args": ["--mcp"]],
            ]
        ]
        let data = try JSONSerialization.data(withJSONObject: existing, options: [.prettyPrinted])
        try data.write(to: URL(fileURLWithPath: path))

        let mgr = MCPConfigManager(configPath: path)
        try mgr.uninstall()

        let configData = try Data(contentsOf: URL(fileURLWithPath: path))
        let config = try JSONSerialization.jsonObject(with: configData) as! [String: Any]
        let servers = config["mcpServers"] as! [String: Any]
        #expect(servers["figma"] != nil)
        #expect(servers["boo"] == nil)
    }

    @Test("currentEntry returns nil when not installed")
    func currentEntryNil() {
        let mgr = MCPConfigManager(configPath: "/tmp/nonexistent-\(UUID().uuidString)/settings.json")
        #expect(mgr.currentEntry() == nil)
    }

    @Test("Config file has secure permissions after install")
    func securePermissions() throws {
        let path = tempConfigPath()
        let mgr = MCPConfigManager(configPath: path)
        try mgr.install(binaryPath: "/usr/local/bin/BooApp")

        let attrs = try FileManager.default.attributesOfItem(atPath: path)
        let perms = (attrs[.posixPermissions] as? Int) ?? 0
        #expect(perms == 0o600)
    }
}
