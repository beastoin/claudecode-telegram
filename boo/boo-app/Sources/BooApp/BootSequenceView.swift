import SwiftUI
import BooCore

/// Screen 1: Setup — auto-scan probes with native macOS styling.
struct BootSequenceView: View {
    @Bindable var coordinator: OnboardingCoordinator

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 6) {
                Image(systemName: "gearshape.2")
                    .font(.system(size: 36))
                    .foregroundStyle(Color.accentColor)
                Text("Setting Up")
                    .font(.title2.bold())
                Text("Checking your environment...")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .padding(.bottom, 20)

            // Probes
            GroupBox("Connection") {
                VStack(spacing: 0) {
                    ForEach(coordinator.probes) { probe in
                        ProbeRow(probe: probe) {
                            Task { await coordinator.fixProbe(probe.id) }
                        }
                        if probe.id != coordinator.probes.last?.id {
                            Divider()
                        }
                    }
                }
            }
            .padding(.horizontal, 24)

            // Peer name
            if coordinator.probes.allSatisfy({ !$0.state.isRunning && !$0.state.isPending }) {
                GroupBox("Agent Name") {
                    TextField("Agent name", text: $coordinator.peerName)
                        .textFieldStyle(.roundedBorder)
                }
                .padding(.horizontal, 24)
                .padding(.top, 12)
                .transition(.opacity)
            }

            Spacer()

            // Navigation
            if coordinator.probes.allSatisfy({ !$0.state.isRunning && !$0.state.isPending }) {
                HStack {
                    Spacer()
                    if coordinator.isFullyConfigured {
                        Button("Continue") { coordinator.advance() }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.large)
                    } else {
                        Button("Continue Anyway") { coordinator.advance() }
                            .buttonStyle(.bordered)
                            .controlSize(.large)
                    }
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 20)
            }
        }
        .padding(.top, 16)
        .task {
            await coordinator.start()
        }
    }
}

// MARK: - Probe Row

private struct ProbeRow: View {
    let probe: Probe
    let onFix: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            probeIcon
                .frame(width: 20)

            Text(probe.label)
                .font(.body)

            Spacer()

            statusContent
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
    }

    @ViewBuilder
    private var probeIcon: some View {
        switch probe.state {
        case .pending:
            Image(systemName: "circle.dotted")
                .foregroundStyle(.secondary)
        case .running:
            ProgressView()
                .controlSize(.small)
        case .passed:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "xmark.circle.fill")
                .foregroundStyle(.red)
        case .skipped:
            Image(systemName: "minus.circle")
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var statusContent: some View {
        switch probe.state {
        case .pending:
            Text("Waiting")
                .font(.caption)
                .foregroundStyle(.secondary)

        case .running:
            Text("Checking...")
                .font(.caption)
                .foregroundStyle(.secondary)

        case .passed(let detail):
            Text(detail)
                .font(.caption)
                .foregroundStyle(.green)
                .lineLimit(1)

        case .failed(let detail):
            HStack(spacing: 6) {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(1)

                if probe.id == "ghostty" {
                    Button("Install") {
                        if let url = URL(string: "https://github.com/beastoin/claudecode-telegram/releases/latest/download/GhosttyBoo-macOS-arm64.zip") {
                            NSWorkspace.shared.open(url)
                        }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                } else if probe.id == "claude-mcp" || probe.id == "codex-mcp" {
                    Button("Fix", action: onFix)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }

        case .skipped(let reason):
            Text(reason)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }
}
