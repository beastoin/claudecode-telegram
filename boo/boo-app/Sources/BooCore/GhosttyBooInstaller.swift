import Foundation

/// Shared installer for Ghostty Boo — used by both Settings and Onboarding.
/// Downloads, unzips, installs to /Applications (or ~/Applications), and configures control-socket.
public actor GhosttyBooInstaller {

    /// Download from a pinned release tag so Boo app releases don't need to bundle Ghostty Boo.
    /// Update this tag when a new Ghostty Boo version ships.
    public static let downloadURL = "https://github.com/beastoin/claudecode-telegram/releases/download/boo-v0.2.4/GhosttyBoo-macOS-arm64.zip"
    public static let systemInstallPath = "/Applications/Ghostty Boo.app"
    public static let userInstallPath = NSString("~/Applications/Ghostty Boo.app").expandingTildeInPath

    public enum Status: Sendable {
        case idle
        case downloading
        case unzipping
        case installing
        case configuring
        case done(path: String)
        case error(String)
    }

    public init() {}

    /// Check if Ghostty Boo is already installed.
    public func isInstalled() -> Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: Self.systemInstallPath) || fm.fileExists(atPath: Self.userInstallPath)
    }

    /// Full install: download → unzip → move to /Applications → configure control-socket.
    /// Calls `onProgress` on MainActor with status updates.
    public func install(onProgress: @MainActor @Sendable (Status) -> Void) async throws {
        let fm = FileManager.default

        // 1. Download (30s timeout)
        await onProgress(.downloading)
        guard let url = URL(string: Self.downloadURL) else {
            throw GhosttyBooInstallerError.message("Invalid download URL")
        }
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 120
        let session = URLSession(configuration: config)
        let (zipFileURL, response) = try await session.download(from: url)
        if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
            throw GhosttyBooInstallerError.message("Download failed with HTTP \(httpResponse.statusCode)")
        }

        let zipPath = NSTemporaryDirectory() + "GhosttyBoo-macOS-arm64.zip"
        if fm.fileExists(atPath: zipPath) {
            try fm.removeItem(atPath: zipPath)
        }
        try fm.moveItem(at: zipFileURL, to: URL(fileURLWithPath: zipPath))

        // 2. Unzip
        await onProgress(.unzipping)
        let extractDir = "/tmp/ghostty-boo-install"
        if fm.fileExists(atPath: extractDir) {
            try fm.removeItem(atPath: extractDir)
        }
        try fm.createDirectory(atPath: extractDir, withIntermediateDirectories: true)

        let unzipProcess = Process()
        unzipProcess.executableURL = URL(fileURLWithPath: "/usr/bin/unzip")
        unzipProcess.arguments = ["-o", zipPath, "-d", extractDir]
        unzipProcess.standardOutput = FileHandle.nullDevice
        unzipProcess.standardError = FileHandle.nullDevice
        try unzipProcess.run()
        unzipProcess.waitUntilExit()

        guard unzipProcess.terminationStatus == 0 else {
            throw GhosttyBooInstallerError.message("Unzip failed (exit code \(unzipProcess.terminationStatus))")
        }

        let extractedItems = try fm.contentsOfDirectory(atPath: extractDir)
        guard let appItem = extractedItems.first(where: { $0.hasSuffix(".app") }) else {
            throw GhosttyBooInstallerError.message("No .app found in downloaded archive")
        }
        let extractedAppPath = extractDir + "/" + appItem

        // 3. Install to ~/Applications (preferred — macOS restricts unsigned apps in /Applications)
        await onProgress(.installing)
        let userAppsDir = NSString("~/Applications").expandingTildeInPath
        if !fm.fileExists(atPath: userAppsDir) {
            try fm.createDirectory(atPath: userAppsDir, withIntermediateDirectories: true)
        }
        if fm.fileExists(atPath: Self.userInstallPath) {
            try fm.removeItem(atPath: Self.userInstallPath)
        }
        let mvProcess = Process()
        mvProcess.executableURL = URL(fileURLWithPath: "/bin/mv")
        mvProcess.arguments = [extractedAppPath, Self.userInstallPath]
        mvProcess.standardOutput = FileHandle.nullDevice
        mvProcess.standardError = FileHandle.nullDevice
        try mvProcess.run()
        mvProcess.waitUntilExit()
        guard mvProcess.terminationStatus == 0 else {
            throw GhosttyBooInstallerError.message("Failed to install to ~/Applications")
        }
        let finalInstallPath = Self.userInstallPath

        // 4. Configure control-socket
        await onProgress(.configuring)
        try ensureGhosttyControlSocket()

        // 5. Cleanup
        try? fm.removeItem(atPath: zipPath)
        try? fm.removeItem(atPath: extractDir)

        let displayPath = finalInstallPath.hasPrefix(NSHomeDirectory())
            ? "~/Applications" : "/Applications"
        await onProgress(.done(path: displayPath))
    }

    /// Ensure control-socket is configured in ~/.config/ghostty/config.
    public func ensureGhosttyControlSocket() throws {
        let configDir = NSString("~/.config/ghostty").expandingTildeInPath
        let configPath = configDir + "/config"
        let fm = FileManager.default
        let controlLine = "control-socket = /tmp/ghostty-boo.sock"

        if !fm.fileExists(atPath: configDir) {
            try fm.createDirectory(atPath: configDir, withIntermediateDirectories: true)
        }

        if fm.fileExists(atPath: configPath) {
            let contents = try String(contentsOfFile: configPath, encoding: .utf8)
            if contents.contains("control-socket") {
                return
            }
            let newContents = contents.hasSuffix("\n")
                ? contents + controlLine + "\n"
                : contents + "\n" + controlLine + "\n"
            try newContents.write(toFile: configPath, atomically: true, encoding: .utf8)
        } else {
            try (controlLine + "\n").write(toFile: configPath, atomically: true, encoding: .utf8)
        }
    }
}

public enum GhosttyBooInstallerError: LocalizedError {
    case message(String)

    public var errorDescription: String? {
        switch self {
        case .message(let msg): return msg
        }
    }
}
