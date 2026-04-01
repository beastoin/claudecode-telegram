import Foundation
import AppKit
import BooCore

// Branch: --mcp runs headless MCP stdio server, otherwise launch GUI app
if CommandLine.arguments.contains("--mcp") {
    let machineName = Host.current().localizedName ?? "unknown"
    let server = MCPServer(machineName: machineName)
    let runner = MCPStdioRunner(server: server)

    // Run async stdio loop on a background thread; block main until done
    let semaphore = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        Task {
            await runner.run()
            semaphore.signal()
        }
    }
    semaphore.wait()
} else {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
