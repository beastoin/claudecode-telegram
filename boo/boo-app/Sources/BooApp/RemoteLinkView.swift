import SwiftUI
import BooCore

/// Screen 3: Remote — SSH discovery and one-click provisioning.
struct RemoteLinkView: View {
    @Bindable var coordinator: OnboardingCoordinator
    @Environment(AppState.self) private var appState
    @State private var machineStates: [String: MachineState] = [:]
    @State private var provisionTasks: [String: Task<Void, Never>] = [:]
    @State private var manualHost = ""
    @State private var showManualAdd = false

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 6) {
                Image(systemName: "network")
                    .font(.system(size: 36))
                    .foregroundStyle(Color.accentColor)
                Text("Remote Machines")
                    .font(.title2.bold())
                Text("Connect to other machines via SSH")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .padding(.bottom, 16)

            // Machine list
            GroupBox {
                if coordinator.sshHosts.isEmpty {
                    emptyState
                } else {
                    ScrollView {
                        VStack(spacing: 0) {
                            ForEach(coordinator.sshHosts) { host in
                                machineRow(host: host)
                                if host.id != coordinator.sshHosts.last?.id {
                                    Divider()
                                }
                            }
                        }
                    }
                    .frame(maxHeight: 200)
                }
            } label: {
                Label("Discovered from ~/.ssh/config", systemImage: "doc.text")
            }
            .padding(.horizontal, 24)

            // Manual add
            manualAddSection
                .padding(.horizontal, 24)
                .padding(.top, 8)

            Spacer()

            // Bottom
            HStack {
                Button("Skip") { coordinator.advance() }
                    .buttonStyle(.bordered)
                Spacer()
                Button("Continue") { coordinator.advance() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 20)
        }
        .padding(.top, 16)
    }

    // MARK: - Empty State

    @ViewBuilder
    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "questionmark.circle")
                .font(.title2)
                .foregroundStyle(.secondary)
            Text("No SSH hosts found")
                .font(.body)
                .foregroundStyle(.secondary)
            Text("Add a machine manually or skip this step.")
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 20)
    }

    // MARK: - Machine Row

    @ViewBuilder
    private func machineRow(host: SSHHost) -> some View {
        let state = machineStates[host.id] ?? .notConfigured

        HStack(spacing: 10) {
            statusIcon(for: state)
                .frame(width: 20)

            VStack(alignment: .leading, spacing: 2) {
                Text(host.alias)
                    .font(.body)

                Text("\(host.user)@\(host.hostname)")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if case .provisioning(let step, _) = state {
                    Text(step.rawValue)
                        .font(.caption2)
                        .foregroundStyle(Color.accentColor)
                } else if case .connected(let detail) = state {
                    Text(detail)
                        .font(.caption2)
                        .foregroundStyle(.green)
                } else if case .failed(let error) = state {
                    Text(error)
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
            }

            Spacer()

            actionButton(host: host, state: state)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
    }

    @ViewBuilder
    private func statusIcon(for state: MachineState) -> some View {
        switch state {
        case .notConfigured:
            Image(systemName: "circle.dotted")
                .foregroundStyle(.secondary)
        case .provisioning:
            ProgressView()
                .controlSize(.small)
        case .connected:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        }
    }

    @ViewBuilder
    private func actionButton(host: SSHHost, state: MachineState) -> some View {
        switch state {
        case .notConfigured:
            Button("Setup") {
                let task = Task { await provision(host: host) }
                provisionTasks[host.id] = task
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

        case .provisioning:
            Button("Cancel") {
                provisionTasks[host.id]?.cancel()
                provisionTasks[host.id] = nil
                machineStates[host.id] = .notConfigured
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

        case .connected:
            Image(systemName: "checkmark")
                .foregroundStyle(.green)

        case .failed:
            Button("Retry") {
                let task = Task { await provision(host: host) }
                provisionTasks[host.id] = task
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        }
    }

    // MARK: - Manual Add

    @ViewBuilder
    private var manualAddSection: some View {
        DisclosureGroup("Add machine manually", isExpanded: $showManualAdd) {
            HStack(spacing: 8) {
                TextField("SSH host (e.g., user@hostname)", text: $manualHost)
                    .textFieldStyle(.roundedBorder)

                Button("Connect") { addManualHost() }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
            .padding(.top, 4)
        }
        .font(.caption)
    }

    // MARK: - Actions

    private func provision(host: SSHHost) async {
        let provisioner = RemoteProvisioner()

        for await result in await provisioner.provision(
            host: host,
            tunnelManager: appState.tunnelManager,
            releaseURL: RemoteProvisioner.defaultReleaseURL
        ) {
            switch result {
            case .inProgress(let step):
                machineStates[host.id] = .provisioning(step, nil)
            case .completed(let step, let detail):
                if step == .discovered {
                    machineStates[host.id] = .connected(detail)
                } else {
                    machineStates[host.id] = .provisioning(step, detail)
                }
            case .failed(_, let error):
                machineStates[host.id] = .failed(error)
            }
        }
    }

    private func addManualHost() {
        let trimmed = manualHost.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        guard !coordinator.sshHosts.contains(where: { $0.id == trimmed || $0.hostname == trimmed }) else { return }

        let parts = trimmed.split(separator: "@", maxSplits: 1)
        let user: String
        let hostname: String
        if parts.count == 2 {
            user = String(parts[0])
            hostname = String(parts[1])
        } else {
            user = NSUserName()
            hostname = trimmed
        }

        coordinator.sshHosts.append(SSHHost(
            id: trimmed, alias: trimmed, hostname: hostname, user: user
        ))
        manualHost = ""
        showManualAdd = false
    }
}

// MARK: - Machine State

private enum MachineState {
    case notConfigured
    case provisioning(ProvisionStep, String?)
    case connected(String)
    case failed(String)
}
