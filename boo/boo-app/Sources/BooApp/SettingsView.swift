import SwiftUI
import AppKit
import ServiceManagement
import BooCore

/// Settings window — follows macOS System Settings pattern (grouped form).
struct SettingsView: View {
    @Environment(AppState.self) private var appState
    @State private var socketPath = "/tmp/ghostty.sock"
    @State private var selectedTab = "general"

    var body: some View {
        TabView(selection: $selectedTab) {
            GeneralSettingsTab(appState: appState, socketPath: $socketPath)
                .tabItem { Label("General", systemImage: "gear") }
                .tag("general")

            MachinesSettingsTab(appState: appState)
                .tabItem { Label("Machines", systemImage: "desktopcomputer") }
                .tag("machines")

            InstallerSettingsTab()
                .tabItem { Label("Installer", systemImage: "arrow.down.circle") }
                .tag("installer")

            AboutSettingsTab()
                .tabItem { Label("About", systemImage: "info.circle") }
                .tag("about")
        }
        .frame(width: 500, height: 420)
    }
}

// MARK: - General Tab

struct GeneralSettingsTab: View {
    let appState: AppState
    @Binding var socketPath: String
    @State private var launchAtLogin = false
    @State private var showNotifications = true
    @State private var mcpInstalled = false
    @State private var mcpStatusMessage: String?

    var body: some View {
        Form {
            Section("Local Connection") {
                TextField("Ghostty Socket Path", text: $socketPath)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Text("Status:")
                    Circle()
                        .fill(statusColor)
                        .frame(width: 8, height: 8)
                    Text(statusText)
                        .foregroundStyle(statusColor)
                    Spacer()
                    Button("Connect") {
                        Task { await appState.connect(socketPath: socketPath) }
                    }
                }
            }

            Section("MCP Server") {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            Circle()
                                .fill(mcpInstalled ? .green : .secondary)
                                .frame(width: 8, height: 8)
                            Text(mcpInstalled ? "MCP server enabled" : "MCP server disabled")
                                .font(.body)
                        }
                        Text("Lets Claude Code agents discover and message each other")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if mcpInstalled {
                        Button("Disable") { toggleMCP() }
                            .buttonStyle(.bordered)
                    } else {
                        Button("Enable") { toggleMCP() }
                            .buttonStyle(.borderedProminent)
                    }
                }
                if let message = mcpStatusMessage {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Preferences") {
                Toggle("Launch at login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, newValue in
                        setLaunchAtLogin(newValue)
                    }
                Toggle("Show notifications for messages", isOn: $showNotifications)
            }
        }
        .formStyle(.grouped)
        .onAppear {
            mcpInstalled = MCPConfigManager().isInstalled()
            launchAtLogin = checkLaunchAtLogin()
        }
    }

    private var statusText: String {
        switch appState.connectionStatus {
        case .connected: "Connected"
        case .disconnected: "Disconnected"
        case .error: "Error"
        }
    }

    private var statusColor: Color {
        switch appState.connectionStatus {
        case .connected: .green
        case .disconnected: .secondary
        case .error: .red
        }
    }

    private func toggleMCP() {
        let manager = MCPConfigManager()
        let binaryPath = ProcessInfo.processInfo.arguments[0]
        do {
            if mcpInstalled {
                try manager.uninstall()
                mcpInstalled = false
                mcpStatusMessage = "MCP server removed from Claude Code config"
            } else {
                try manager.install(binaryPath: binaryPath)
                mcpInstalled = true
                mcpStatusMessage = "MCP server registered in ~/.claude/settings.json"
            }
        } catch {
            mcpStatusMessage = "Error: \(error.localizedDescription)"
        }
        // Clear message after 3 seconds
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            mcpStatusMessage = nil
        }
    }

    private func checkLaunchAtLogin() -> Bool {
        if #available(macOS 13.0, *) {
            return SMAppService.mainApp.status == .enabled
        }
        return false
    }

    private func setLaunchAtLogin(_ enabled: Bool) {
        if #available(macOS 13.0, *) {
            do {
                if enabled {
                    try SMAppService.mainApp.register()
                } else {
                    try SMAppService.mainApp.unregister()
                }
            } catch {
                // Silently handle — launch at login may require app bundle
                launchAtLogin = checkLaunchAtLogin()
            }
        }
    }
}

// MARK: - Machines Tab

struct MachinesSettingsTab: View {
    let appState: AppState
    @State private var machines: [MachineEntry] = [
        MachineEntry(name: "mac-mini", sshHost: "beastoin-agents-f1-mac-mini",
                     remoteSocket: "/tmp/ghostty-test.sock", isEnabled: true),
    ]
    @State private var showAddSheet = false

    var body: some View {
        Form {
            Section("Remote Machines") {
                if machines.isEmpty {
                    Text("No remote machines configured")
                        .foregroundStyle(.secondary)
                } else {
                    ForEach($machines) { $machine in
                        MachineRow(machine: $machine, appState: appState)
                    }
                    .onDelete { indices in
                        machines.remove(atOffsets: indices)
                    }
                }

                Button {
                    showAddSheet = true
                } label: {
                    Label("Add Machine", systemImage: "plus")
                }
            }

            Section("SSH Tunnels") {
                HStack {
                    Text("Auto-connect tunnels on launch")
                    Spacer()
                    Toggle("", isOn: .constant(true))
                        .labelsHidden()
                }
                HStack {
                    Text("Reconnect on failure")
                    Spacer()
                    Toggle("", isOn: .constant(true))
                        .labelsHidden()
                }
                HStack {
                    Text("Server alive interval")
                    Spacer()
                    Text("30s")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $showAddSheet) {
            AddMachineSheet(machines: $machines, isPresented: $showAddSheet)
        }
    }
}

struct MachineEntry: Identifiable {
    let id = UUID()
    var name: String
    var sshHost: String
    var remoteSocket: String
    var isEnabled: Bool
}

struct MachineRow: View {
    @Binding var machine: MachineEntry
    let appState: AppState

    var body: some View {
        HStack {
            Toggle("", isOn: $machine.isEnabled)
                .labelsHidden()
            VStack(alignment: .leading, spacing: 2) {
                Text(machine.name)
                    .font(.body.weight(.medium))
                Text(machine.sshHost)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Circle()
                .fill(machine.isEnabled ? .green : .secondary)
                .frame(width: 8, height: 8)
        }
    }
}

struct AddMachineSheet: View {
    @Binding var machines: [MachineEntry]
    @Binding var isPresented: Bool
    @State private var name = ""
    @State private var sshHost = ""
    @State private var remoteSocket = "/tmp/ghostty.sock"

    var body: some View {
        VStack(spacing: 16) {
            Text("Add Remote Machine")
                .font(.headline)

            Form {
                TextField("Name", text: $name, prompt: Text("e.g., mac-mini"))
                TextField("SSH Host", text: $sshHost, prompt: Text("e.g., user@hostname"))
                TextField("Remote Socket", text: $remoteSocket)
            }
            .formStyle(.grouped)

            HStack {
                Button("Cancel") { isPresented = false }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Add") {
                    machines.append(MachineEntry(
                        name: name, sshHost: sshHost,
                        remoteSocket: remoteSocket, isEnabled: true
                    ))
                    isPresented = false
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.isEmpty || sshHost.isEmpty)
            }
        }
        .padding()
        .frame(width: 400, height: 280)
    }
}

// MARK: - Installer Tab

struct InstallerSettingsTab: View {
    @State private var installStatus: InstallStatus = .notInstalled
    @State private var installMessage: String?

    private static let downloadURL = "https://storage.googleapis.com/omi-pr-assets/ghostty-boo/GhosttyBoo-macOS-arm64.zip"
    private static let appName = "Ghostty Boo.app"
    private static let systemInstallPath = "/Applications/Ghostty Boo.app"
    private static let userInstallPath = NSString("~/Applications/Ghostty Boo.app").expandingTildeInPath

    enum InstallStatus {
        case notInstalled, installing, installed, error
    }

    var body: some View {
        Form {
            Section("Ghostty Boo") {
                HStack {
                    Image(systemName: "terminal.fill")
                        .font(.title2)
                        .foregroundStyle(.blue)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Ghostty Boo")
                            .font(.body.weight(.medium))
                        Text("Fork of Ghostty with Unix socket control API")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    statusBadge
                }

                if installStatus == .notInstalled {
                    Button {
                        performInstall()
                    } label: {
                        Label("Install Ghostty Boo", systemImage: "arrow.down.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                }

                if installStatus == .installed {
                    Button {
                        openGhosttyBoo()
                    } label: {
                        Label("Open Ghostty Boo", systemImage: "arrow.up.forward.app.fill")
                    }
                    .buttonStyle(.bordered)
                }

                if let message = installMessage {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(installStatus == .error ? .red : .secondary)
                }

                Text("Ghostty Boo is an unofficial fork of Ghostty by Mitchell Hashimoto. It adds a `control-socket` config option. Your existing Ghostty config and data are preserved.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Configuration") {
                HStack {
                    Text("control-socket")
                    Spacer()
                    Text("/tmp/ghostty.sock")
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                HStack {
                    Text("Socket permissions")
                    Spacer()
                    Text("0600 (owner only)")
                        .foregroundStyle(.secondary)
                }
                HStack {
                    Text("Config file")
                    Spacer()
                    Text("~/.config/ghostty/config")
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
        }
        .formStyle(.grouped)
        .onAppear {
            checkIfInstalled()
        }
    }

    @ViewBuilder
    private var statusBadge: some View {
        switch installStatus {
        case .notInstalled:
            Text("Not Installed")
                .font(.caption)
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .background(.secondary.opacity(0.2))
                .clipShape(Capsule())
        case .installing:
            ProgressView()
                .controlSize(.small)
        case .installed:
            Label("Installed", systemImage: "checkmark.circle.fill")
                .font(.caption)
                .foregroundStyle(.green)
        case .error:
            Label("Error", systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(.red)
        }
    }

    // MARK: - Install Logic

    private func checkIfInstalled() {
        let fm = FileManager.default
        if fm.fileExists(atPath: Self.systemInstallPath) || fm.fileExists(atPath: Self.userInstallPath) {
            installStatus = .installed
        }
    }

    private func performInstall() {
        installStatus = .installing
        installMessage = "Downloading..."

        Task {
            do {
                // 1. Download the zip
                guard let url = URL(string: Self.downloadURL) else {
                    throw InstallerError.message("Invalid download URL")
                }
                let (zipFileURL, response) = try await URLSession.shared.download(from: url)
                if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
                    throw InstallerError.message("Download failed with HTTP \(httpResponse.statusCode)")
                }

                // Move to a stable temp path (URLSession download file is ephemeral)
                let zipPath = NSTemporaryDirectory() + "GhosttyBoo-macOS-arm64.zip"
                let fm = FileManager.default
                if fm.fileExists(atPath: zipPath) {
                    try fm.removeItem(atPath: zipPath)
                }
                try fm.moveItem(at: zipFileURL, to: URL(fileURLWithPath: zipPath))

                await MainActor.run { installMessage = "Unzipping..." }

                // 2. Unzip
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
                    throw InstallerError.message("Unzip failed (exit code \(unzipProcess.terminationStatus))")
                }

                // Find the .app inside extracted contents
                let extractedItems = try fm.contentsOfDirectory(atPath: extractDir)
                guard let appItem = extractedItems.first(where: { $0.hasSuffix(".app") }) else {
                    throw InstallerError.message("No .app found in downloaded archive")
                }
                let extractedAppPath = extractDir + "/" + appItem

                await MainActor.run { installMessage = "Installing to /Applications..." }

                // 3. Move to /Applications, fall back to ~/Applications
                var finalInstallPath = Self.systemInstallPath
                var movedSuccessfully = false

                // Try /Applications first
                do {
                    if fm.fileExists(atPath: Self.systemInstallPath) {
                        try fm.removeItem(atPath: Self.systemInstallPath)
                    }
                    let mvProcess = Process()
                    mvProcess.executableURL = URL(fileURLWithPath: "/bin/mv")
                    mvProcess.arguments = [extractedAppPath, Self.systemInstallPath]
                    mvProcess.standardOutput = FileHandle.nullDevice
                    mvProcess.standardError = FileHandle.nullDevice
                    try mvProcess.run()
                    mvProcess.waitUntilExit()
                    if mvProcess.terminationStatus == 0 {
                        movedSuccessfully = true
                    }
                } catch {
                    // Permission denied -- will fall back to ~/Applications
                }

                if !movedSuccessfully {
                    await MainActor.run { installMessage = "Installing to ~/Applications..." }

                    // Ensure ~/Applications exists
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
                        throw InstallerError.message("Failed to install to ~/Applications")
                    }
                    finalInstallPath = Self.userInstallPath
                }

                // 4. Configure control-socket in ghostty config
                await MainActor.run { installMessage = "Configuring control-socket..." }
                try ensureGhosttyControlSocket()

                // 5. Cleanup
                try? fm.removeItem(atPath: zipPath)
                try? fm.removeItem(atPath: extractDir)

                let displayPath = finalInstallPath.hasPrefix(NSHomeDirectory())
                    ? "~/Applications" : "/Applications"
                await MainActor.run {
                    installStatus = .installed
                    installMessage = "Installed to \(displayPath). control-socket configured."
                }

                // Clear message after 5 seconds
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                await MainActor.run { installMessage = nil }

            } catch {
                let errorMsg = (error as? InstallerError)?.localizedDescription
                    ?? error.localizedDescription
                await MainActor.run {
                    installStatus = .error
                    installMessage = errorMsg
                }
            }
        }
    }

    private func openGhosttyBoo() {
        let fm = FileManager.default
        let path: String
        if fm.fileExists(atPath: Self.systemInstallPath) {
            path = Self.systemInstallPath
        } else if fm.fileExists(atPath: Self.userInstallPath) {
            path = Self.userInstallPath
        } else {
            installMessage = "Ghostty Boo.app not found"
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    private func ensureGhosttyControlSocket() throws {
        let configDir = NSString("~/.config/ghostty").expandingTildeInPath
        let configPath = configDir + "/config"
        let fm = FileManager.default
        let controlLine = "control-socket = /tmp/ghostty.sock"

        if !fm.fileExists(atPath: configDir) {
            try fm.createDirectory(atPath: configDir, withIntermediateDirectories: true)
        }

        if fm.fileExists(atPath: configPath) {
            let contents = try String(contentsOfFile: configPath, encoding: .utf8)
            if contents.contains("control-socket") {
                return // Already configured
            }
            // Append the control-socket line
            let newContents = contents.hasSuffix("\n")
                ? contents + controlLine + "\n"
                : contents + "\n" + controlLine + "\n"
            try newContents.write(toFile: configPath, atomically: true, encoding: .utf8)
        } else {
            // Create new config file
            try (controlLine + "\n").write(toFile: configPath, atomically: true, encoding: .utf8)
        }
    }
}

private enum InstallerError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let msg): return msg
        }
    }
}

// MARK: - About Tab

struct AboutSettingsTab: View {
    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                BooIcons.appIcon(size: 56)

                Text("Boo")
                    .font(.title2.weight(.bold))
                Text("v1.0.0")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Text("Cross-machine agent communication.\nNo cloud, no accounts, sub-millisecond latency.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                Divider()
                    .padding(.horizontal, 30)

                // Protocol info
                VStack(spacing: 4) {
                    infoRow("MCP Protocol", "2025-03-26")
                    infoRow("Tools", "6 (register, list, send, broadcast, receive, status)")
                    infoRow("Transport", "JSON-RPC 2.0 / stdio")
                }
                .font(.caption)
                .padding(.horizontal, 30)

                Divider()
                    .padding(.horizontal, 30)

                // Disclaimers
                VStack(alignment: .leading, spacing: 8) {
                    Text("Disclaimers")
                        .font(.caption.weight(.semibold))

                    Text("Boo is an independent open-source project that enables cross-machine agent communication via Ghostty terminals.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)

                    Text("Boo requires Ghostty Boo, a fork of Ghostty with control socket support. Ghostty is created by Mitchell Hashimoto. Boo is not affiliated with or endorsed by the Ghostty project.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)

                    Text("Boo is open source under the MIT License.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)

                    HStack(spacing: 16) {
                        Link("Ghostty", destination: URL(string: "https://ghostty.org")!)
                            .font(.caption2)
                        Link("Boo on GitHub", destination: URL(string: "https://github.com/beastoin/claudecode-telegram/tree/main/boo")!)
                            .font(.caption2)
                    }
                    .padding(.top, 2)
                }
                .padding(.horizontal, 30)
            }
            .padding(.vertical, 16)
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private func infoRow(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label)
            Spacer()
            Text(value)
                .foregroundStyle(.secondary)
        }
    }
}
