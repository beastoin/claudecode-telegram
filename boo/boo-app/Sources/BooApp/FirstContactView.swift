import SwiftUI
import BooCore

/// Screen 2: Demo — live MCP tool call demonstration.
struct FirstContactView: View {
    @Bindable var coordinator: OnboardingCoordinator
    @Environment(AppState.self) private var appState
    @State private var isRunning = false

    var body: some View {
        VStack(spacing: 0) {
            // Header
            VStack(spacing: 6) {
                Image(systemName: "bolt.horizontal.circle")
                    .font(.system(size: 36))
                    .foregroundStyle(Color.accentColor)
                Text("Live Demo")
                    .font(.title2.bold())
                Text("Watch MCP tools in action")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .padding(.bottom, 16)

            HStack(alignment: .top, spacing: 16) {
                // Left: Tool call transcript
                GroupBox("MCP Tool Calls") {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 8) {
                                ForEach(coordinator.demoTranscript) { line in
                                    toolCallRow(line)
                                        .id(line.id)
                                }
                            }
                            .padding(4)
                        }
                        .frame(minHeight: 180)
                        .onChange(of: coordinator.demoTranscript.count) {
                            if let last = coordinator.demoTranscript.last {
                                withAnimation {
                                    proxy.scrollTo(last.id, anchor: .bottom)
                                }
                            }
                        }
                    }
                }

                // Right: Peers discovered
                GroupBox("Peers") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(discoveredPeers, id: \.id) { line in
                            peerRow(name: extractPeerName(from: line))
                        }

                        if discoveredPeers.isEmpty {
                            Text("Waiting for demo...")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .center)
                                .padding(.vertical, 20)
                        }
                    }
                    .frame(minWidth: 150)
                }
            }
            .padding(.horizontal, 24)

            Spacer()

            // Bottom
            HStack {
                if isRunning {
                    ProgressView()
                        .controlSize(.small)
                    Text("Running demo...")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if coordinator.demoComplete {
                    Button("Re-run Demo") {
                        coordinator.demoTranscript = []
                        coordinator.demoComplete = false
                        Task { await runDemo() }
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
                Spacer()
                if coordinator.demoComplete {
                    Button("Continue") { coordinator.advance() }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 20)
        }
        .padding(.top, 16)
        .task {
            await runDemo()
        }
    }

    // MARK: - Tool Call Row

    @ViewBuilder
    private func toolCallRow(_ line: TranscriptLine) -> some View {
        HStack(spacing: 10) {
            Image(systemName: lineIcon(line.direction))
                .foregroundStyle(lineColor(line.direction))
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 2) {
                Text(line.tool)
                    .font(.headline)
                Text(line.content)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
    }

    private func lineIcon(_ direction: TranscriptLine.Direction) -> String {
        switch direction {
        case .request: "arrow.up.circle.fill"
        case .response: "arrow.down.circle.fill"
        case .error: "xmark.circle.fill"
        }
    }

    private func lineColor(_ direction: TranscriptLine.Direction) -> Color {
        switch direction {
        case .request: Color.accentColor
        case .response: .green
        case .error: .red
        }
    }

    // MARK: - Peers

    private var discoveredPeers: [TranscriptLine] {
        coordinator.demoTranscript.filter {
            $0.tool == "register_peer" && $0.direction == .response
        }
    }

    private func extractPeerName(from line: TranscriptLine) -> String {
        if let idx = coordinator.demoTranscript.firstIndex(where: { $0.id == line.id }),
           idx > 0 {
            let request = coordinator.demoTranscript[idx - 1]
            if request.direction == .request {
                let jsonString = request.content
                    .replacingOccurrences(of: "([{,]\\s*)(\\w+):", with: "$1\"$2\":", options: .regularExpression)
                if let data = jsonString.data(using: .utf8),
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let name = json["name"] as? String {
                    return name
                }
            }
        }
        return "unknown"
    }

    @ViewBuilder
    private func peerRow(name: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "person.circle.fill")
                .foregroundStyle(.green)
            VStack(alignment: .leading, spacing: 1) {
                Text(name)
                    .font(.body)
                    .lineLimit(1)
                Text("Active")
                    .font(.caption2)
                    .foregroundStyle(.green)
            }
        }
        .transition(.opacity)
    }

    // MARK: - Demo Runner

    private func runDemo() async {
        isRunning = true
        let engine = OnboardingDemoEngine()
        let machineName = Host.current().localizedName ?? "local"

        // Pass socket path for live terminal demo if Ghostty is connected
        let socketPath = appState.connectionStatus == .connected ? appState.socketPath : nil

        let stream = await engine.runDemo(
            peerName: coordinator.peerName.isEmpty ? "user" : coordinator.peerName,
            registry: appState.registry,
            relay: appState.relay,
            machineName: machineName,
            socketPath: socketPath.flatMap { $0.isEmpty ? nil : $0 }
        )

        for await line in stream {
            let transcriptLine = TranscriptLine(
                direction: line.direction == .request ? .request :
                          line.direction == .response ? .response : .error,
                tool: line.tool,
                content: line.content,
                timestamp: line.timestamp
            )
            withAnimation(.easeInOut(duration: 0.2)) {
                coordinator.demoTranscript.append(transcriptLine)
            }
        }

        isRunning = false
        coordinator.demoComplete = true
    }
}
