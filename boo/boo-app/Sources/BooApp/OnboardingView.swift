import SwiftUI
import BooCore

/// First-run onboarding wizard — 3 steps to get started.
struct OnboardingView: View {
    @Environment(AppState.self) private var appState
    @State private var step = 0
    @State private var mcpInstalled = false
    @State private var mcpError: String?
    @State private var socketFound = false
    let onComplete: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Step indicator
            HStack(spacing: 8) {
                ForEach(0..<3) { i in
                    Circle()
                        .fill(i <= step ? Color.accentColor : Color.secondary.opacity(0.3))
                        .frame(width: 8, height: 8)
                }
            }
            .padding(.top, 20)

            Spacer()

            // Step content
            switch step {
            case 0: welcomeStep
            case 1: mcpStep
            case 2: doneStep
            default: doneStep
            }

            Spacer()

            // Navigation
            HStack {
                if step > 0 {
                    Button("Back") { withAnimation { step -= 1 } }
                }
                Spacer()
                if step < 2 {
                    Button("Next") { withAnimation { step += 1 } }
                        .keyboardShortcut(.defaultAction)
                } else {
                    Button("Get Started") { onComplete() }
                        .keyboardShortcut(.defaultAction)
                        .buttonStyle(.borderedProminent)
                }
            }
            .padding(20)
        }
        .frame(width: 460, height: 360)
        .onAppear { checkSocket() }
    }

    // MARK: - Step 1: Welcome

    @ViewBuilder
    private var welcomeStep: some View {
        VStack(spacing: 16) {
            BooIcons.appIcon(size: 72)

            Text("Welcome to Boo")
                .font(.title.weight(.bold))

            Text("AI agent communication across machines.\nNo cloud, no accounts, sub-millisecond latency.")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            HStack(spacing: 24) {
                featureItem(icon: "terminal", title: "Terminal Discovery", desc: "See all Ghostty terminals")
                featureItem(icon: "message", title: "Agent Messaging", desc: "Send messages between agents")
                featureItem(icon: "network", title: "Multi-Machine", desc: "SSH tunnel to remote hosts")
            }
            .padding(.top, 8)
        }
        .padding(.horizontal, 30)
    }

    @ViewBuilder
    private func featureItem(icon: String, title: String, desc: String) -> some View {
        VStack(spacing: 6) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundStyle(.blue)
                .frame(height: 30)
            Text(title)
                .font(.caption.weight(.medium))
            Text(desc)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(width: 120)
    }

    // MARK: - Step 2: MCP Setup

    @ViewBuilder
    private var mcpStep: some View {
        VStack(spacing: 16) {
            BooIcons.appIcon(size: 56)

            Text("Enable MCP Server")
                .font(.title2.weight(.bold))

            Text("Register Boo as a Claude Code MCP server.\nThis lets Claude agents discover and message each other.")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            if mcpInstalled {
                Label("MCP server registered", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.body.weight(.medium))
                    .padding(.top, 8)
            } else if let error = mcpError {
                VStack(spacing: 8) {
                    Label("Setup failed", systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Button("Retry") { installMCP() }
                }
                .padding(.top, 8)
            } else {
                Button {
                    installMCP()
                } label: {
                    Label("Enable MCP Server", systemImage: "bolt.circle.fill")
                        .font(.body.weight(.medium))
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .padding(.top, 8)
            }

            // Socket status
            HStack {
                Circle()
                    .fill(socketFound ? .green : .orange)
                    .frame(width: 8, height: 8)
                Text(socketFound ? "Ghostty socket found" : "No Ghostty socket detected")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.top, 4)
        }
        .padding(.horizontal, 30)
    }

    // MARK: - Step 3: Done

    @ViewBuilder
    private var doneStep: some View {
        VStack(spacing: 16) {
            BooIcons.appIcon(size: 56)

            Text("You're All Set!")
                .font(.title.weight(.bold))

            VStack(alignment: .leading, spacing: 10) {
                checkItem("Menu bar icon shows agent status", done: true)
                checkItem("MCP server registered for Claude Code", done: mcpInstalled)
                checkItem("Ghostty socket connected", done: socketFound)
            }
            .padding(.top, 8)

            Text("Click the menu bar icon anytime to see your agents.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 8)
        }
        .padding(.horizontal, 30)
    }

    @ViewBuilder
    private func checkItem(_ text: String, done: Bool) -> some View {
        HStack(spacing: 8) {
            Image(systemName: done ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(done ? .green : .secondary)
            Text(text)
                .font(.body)
        }
    }

    // MARK: - Actions

    private func installMCP() {
        let manager = MCPConfigManager()
        let binaryPath = Bundle.main.executablePath ?? ProcessInfo.processInfo.arguments[0]
        do {
            try manager.install(binaryPath: binaryPath)
            mcpInstalled = true
            mcpError = nil
        } catch {
            mcpError = error.localizedDescription
        }
    }

    private func checkSocket() {
        let paths = ["/tmp/ghostty-test.sock", "/tmp/ghostty.sock"]
        socketFound = paths.contains { FileManager.default.fileExists(atPath: $0) }
        // Also check if MCP is already installed
        mcpInstalled = MCPConfigManager().isInstalled()
    }
}
