import SwiftUI
import BooCore

/// Screen 4: Ready — status dashboard and launch.
struct MissionReadyView: View {
    @Bindable var coordinator: OnboardingCoordinator
    @Environment(AppState.self) private var appState
    let onComplete: () -> Void

    @State private var copiedName = false
    @State private var claudeEnabled = false

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 6) {
                Image(systemName: "checkmark.seal.fill")
                    .font(.system(size: 36))
                    .foregroundStyle(.green)
                Text("You're All Set")
                    .font(.title2.bold())
                Text("Boo is ready to use.")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .padding(.bottom, 20)

            // 2x2 status grid
            LazyVGrid(columns: [
                GridItem(.flexible(), spacing: 12),
                GridItem(.flexible(), spacing: 12),
            ], spacing: 12) {
                terminalsCard
                identityCard
                mcpClientsCard
                remoteCard
            }
            .padding(.horizontal, 24)

            Spacer()

            // Launch
            VStack(spacing: 12) {
                Button("Get Started") {
                    coordinator.complete()
                    onComplete()
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                // Quick actions
                HStack(spacing: 16) {
                    quickAction(label: "Open Boo", icon: "menubar.rectangle") {
                        NotificationCenter.default.post(name: .init("ShowBooPopover"), object: nil)
                    }
                    quickAction(label: "Copy Name", icon: "doc.on.doc") {
                        copyPeerName()
                    }
                    quickAction(label: "Settings", icon: "gear") {
                        NSApp.sendAction(Selector(("openSettings:")), to: nil, from: nil)
                    }
                }
            }
            .padding(.bottom, 20)
        }
        .padding(.top, 16)
    }

    // MARK: - Status Cards

    @ViewBuilder
    private var terminalsCard: some View {
        GroupBox("Terminals") {
            let count = appState.discoveredTerminals.count
            HStack(spacing: 8) {
                Image(systemName: count > 0 ? "checkmark.circle.fill" : "circle.dotted")
                    .foregroundStyle(count > 0 ? .green : .orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(count) terminal\(count == 1 ? "" : "s")")
                        .font(.body)
                    Text(appState.connectionStatus == .connected ? "Connected" : "Disconnected")
                        .font(.caption)
                        .foregroundStyle(appState.connectionStatus == .connected ? .green : .orange)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var identityCard: some View {
        GroupBox("Identity") {
            VStack(alignment: .leading, spacing: 4) {
                Text(coordinator.peerName)
                    .font(.body)
                    .lineLimit(1)

                Button(action: copyPeerName) {
                    Label(copiedName ? "Copied" : "Copy", systemImage: copiedName ? "checkmark" : "doc.on.doc")
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var mcpClientsCard: some View {
        let claudeOK = claudeEnabled || MCPConfigManager().isInstalled()

        GroupBox("MCP Clients") {
            VStack(alignment: .leading, spacing: 4) {
                clientRow(name: "Claude Code", installed: claudeOK) { enableClaudeMCP() }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private func clientRow(name: String, installed: Bool, onEnable: @escaping () -> Void) -> some View {
        HStack(spacing: 6) {
            Image(systemName: installed ? "checkmark.circle.fill" : "xmark.circle")
                .foregroundStyle(installed ? .green : .red)
            Text(name)
                .font(.body)
            if !installed {
                Button("Enable", action: onEnable)
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
            }
        }
    }

    @ViewBuilder
    private var remoteCard: some View {
        let tunnelCount = appState.tunnelStatuses.filter { $0.status == .connected }.count

        GroupBox("Remote") {
            HStack(spacing: 8) {
                Image(systemName: tunnelCount > 0 ? "checkmark.circle.fill" : "circle.dotted")
                    .foregroundStyle(tunnelCount > 0 ? .green : .secondary)
                Text(tunnelCount > 0 ? "\(tunnelCount) tunnel\(tunnelCount == 1 ? "" : "s") active" : "Not configured")
                    .font(.body)
                    .foregroundStyle(tunnelCount > 0 ? .primary : .secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: - Helpers

    @ViewBuilder
    private func quickAction(label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(label, systemImage: icon)
                .font(.caption)
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
    }

    private func enableClaudeMCP() {
        let manager = MCPConfigManager()
        let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
        _ = try? manager.install(binaryPath: binaryPath)
        claudeEnabled = true
    }

    private func copyPeerName() {
        #if canImport(AppKit)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(coordinator.peerName, forType: .string)
        #endif
        copiedName = true
        Task {
            try? await Task.sleep(for: .seconds(2))
            copiedName = false
        }
    }
}
