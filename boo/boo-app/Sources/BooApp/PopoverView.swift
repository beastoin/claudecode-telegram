import SwiftUI
import BooCore

/// Main popover content — agent list grouped by machine with section headers.
struct PopoverView: View {
    @Environment(AppState.self) private var appState
    @State private var refreshTimer: Timer?

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Boo")
                    .font(.headline)
                Spacer()
                connectionIndicator
                Button {
                    if let delegate = NSApp.delegate as? AppDelegate {
                        delegate.openSettingsWindow()
                    }
                } label: {
                    Image(systemName: "gear")
                }
                .buttonStyle(.borderless)
                .help("Settings")
                Button {
                    NSApp.terminate(nil)
                } label: {
                    Image(systemName: "power")
                }
                .buttonStyle(.borderless)
                .help("Quit Boo")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)

            Divider()

            // Agent list grouped by machine
            if appState.discoveredTerminals.isEmpty && appState.peers.isEmpty && appState.tunnelStatuses.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        // Terminals section
                        if !appState.discoveredTerminals.isEmpty {
                            machineSection(
                                name: machineName,
                                terminalCount: appState.discoveredTerminals.count,
                                isConnected: appState.connectionStatus == .connected
                            )
                            ForEach(appState.discoveredTerminals, id: \.id) { terminal in
                                TerminalRow(terminal: terminal)
                            }
                        }

                        // Peers grouped by machine
                        ForEach(peerMachineGroups, id: \.machine) { group in
                            machineSection(
                                name: group.machine.isEmpty ? "Unknown" : group.machine,
                                terminalCount: group.peers.count,
                                isConnected: group.peers.contains { $0.status == .active }
                            )
                            ForEach(group.peers, id: \.peerID) { peer in
                                PeerRow(peer: peer)
                            }
                        }

                        // SSH tunnels
                        if !appState.tunnelStatuses.isEmpty {
                            sectionHeader("Tunnels")
                            ForEach(appState.tunnelStatuses, id: \.name) { tunnel in
                                TunnelRow(name: tunnel.name, status: tunnel.status)
                            }
                        }
                    }
                }
            }
        }
        .frame(width: 320, height: 400)
        .onAppear {
            // Refresh all data immediately when popover opens
            Task {
                await appState.refreshTerminals()
                await appState.refreshPeers()
                await appState.refreshTunnelStatuses()
            }
            // Auto-refresh every 5 seconds
            refreshTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { _ in
                Task { @MainActor in
                    await appState.refreshTerminals()
                    await appState.refreshPeers()
                    await appState.refreshTunnelStatuses()
                }
            }
        }
        .onDisappear {
            refreshTimer?.invalidate()
            refreshTimer = nil
        }
    }

    private var peerMachineGroups: [PeerMachineGroup] {
        let grouped = Dictionary(grouping: appState.peers, by: \.machine)
        return grouped.keys.sorted().map { machine in
            PeerMachineGroup(machine: machine, peers: grouped[machine]!)
        }
    }

    private var machineName: String {
        Host.current().localizedName ?? "This Mac"
    }

    @ViewBuilder
    private var connectionIndicator: some View {
        Circle()
            .fill(appState.connectionStatus == .connected ? .green : .secondary)
            .frame(width: 8, height: 8)
    }

    @ViewBuilder
    private func machineSection(name: String, terminalCount: Int, isConnected: Bool) -> some View {
        HStack {
            Circle()
                .fill(isConnected ? .green : .secondary)
                .frame(width: 6, height: 6)
            Text(name)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
            Spacer()
            Text("\(terminalCount)")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    @ViewBuilder
    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(.subheadline.weight(.medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12)
            .padding(.top, 12)
            .padding(.bottom, 4)
    }

    @ViewBuilder
    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            BooIcons.appIcon(size: 48)
                .opacity(0.4)
            Text("No agents found")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text("Connect to a Ghostty socket to discover terminals")
                .font(.caption)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
            Spacer()
        }
        .padding()
    }
}

/// A row showing a discovered terminal.
struct TerminalRow: View {
    let terminal: TerminalInfo

    var body: some View {
        HStack {
            Image(systemName: "terminal")
                .foregroundStyle(.secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(terminal.title)
                    .font(.body)
                Text(terminal.workingDirectory)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            if terminal.focused {
                Circle()
                    .fill(Color.accentColor)
                    .frame(width: 6, height: 6)
            }
            Text("\(terminal.columns)×\(terminal.rows)")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onTapGesture {
            focusTerminalInGhosttyBoo(terminalID: terminal.id)
        }
    }

    private func focusTerminalInGhosttyBoo(terminalID: String) {
        // Activate Ghostty Boo window
        activateGhosttyWindow()

        // Send a brief marker to the target terminal so user can identify it
        if let client = Self.findGhosttyClient() {
            // Send an empty Enter to bring the cursor to the target pane's attention
            try? client.sendText(terminalID: terminalID, text: "")
        }
    }

    private static func findGhosttyClient() -> GhosttyClient? {
        for path in ["/tmp/ghostty-boo.sock", "/tmp/ghostty.sock", "/tmp/ghostty-test.sock"] {
            let client = GhosttyClient(socketPath: path)
            if let _ = try? client.listTerminals() { return client }
        }
        return nil
    }

    private func activateGhosttyWindow() {
        for name in ["Ghostty Boo", "GhosttyBoo", "Ghostty"] {
            let script = """
            tell application "System Events"
                if exists process "\(name)" then
                    tell process "\(name)"
                        set frontmost to true
                    end tell
                    return "ok"
                end if
            end tell
            """
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = ["-e", script]
            process.standardOutput = Pipe()
            process.standardError = Pipe()
            try? process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 { break }
        }
    }
}

/// A row showing a registered peer.
struct PeerRow: View {
    let peer: PeerInfo

    var body: some View {
        HStack {
            Image(systemName: "person.circle")
                .foregroundStyle(.secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(peer.name)
                    .font(.body)
                if let role = peer.role {
                    Text(role)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            if peer.terminalID != nil {
                Image(systemName: "terminal")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Circle()
                .fill(peer.status == .active ? .green : .secondary)
                .frame(width: 6, height: 6)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onTapGesture {
            if let terminalID = peer.terminalID {
                focusPeerTerminal(terminalID: terminalID)
            }
        }
    }

    private func focusPeerTerminal(terminalID: String) {
        // Activate Ghostty window
        for name in ["Ghostty Boo", "GhosttyBoo", "Ghostty"] {
            let script = """
            tell application "System Events"
                if exists process "\(name)" then
                    tell process "\(name)"
                        set frontmost to true
                    end tell
                    return "ok"
                end if
            end tell
            """
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = ["-e", script]
            process.standardOutput = Pipe()
            process.standardError = Pipe()
            try? process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 { break }
        }
    }
}

/// A row showing an SSH tunnel status.
struct TunnelRow: View {
    let name: String
    let status: TunnelStatus

    var body: some View {
        HStack {
            Image(systemName: tunnelIcon)
                .foregroundStyle(.secondary)
                .frame(width: 20)
            Text(name)
                .font(.body)
            Spacer()
            Text(statusLabel)
                .font(.caption)
                .foregroundStyle(.secondary)
            Circle()
                .fill(statusColor)
                .frame(width: 6, height: 6)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
    }

    private var tunnelIcon: String {
        switch status {
        case .connected: "network"
        case .connecting, .reconnecting: "network.badge.shield.half.filled"
        case .disconnected: "network.slash"
        }
    }

    private var statusLabel: String {
        switch status {
        case .connected: "Connected"
        case .connecting: "Connecting..."
        case .reconnecting: "Reconnecting..."
        case .disconnected: "Offline"
        }
    }

    private var statusColor: Color {
        switch status {
        case .connected: .green
        case .connecting, .reconnecting: .orange
        case .disconnected: .secondary
        }
    }
}

/// Groups peers by machine for section display.
struct PeerMachineGroup: Identifiable {
    let machine: String
    let peers: [PeerInfo]
    var id: String { machine }
}
