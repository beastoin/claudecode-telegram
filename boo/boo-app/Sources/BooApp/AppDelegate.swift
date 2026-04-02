import AppKit
import SwiftUI
import BooCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let appState = AppState()
    private var statusItemController: StatusItemController?
    private var settingsWindow: NSWindow?
    private var onboardingWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Hide Dock icon (LSUIElement behavior)
        NSApp.setActivationPolicy(.accessory)

        statusItemController = StatusItemController(appState: appState)

        // No notification setup needed — PopoverView calls openSettings: via NSApp.sendAction

        // Auto-connect to Ghostty socket if found
        let socketPaths = ["/tmp/ghostty-boo.sock", "/tmp/ghostty-test.sock"]
        Task {
            for path in socketPaths {
                if FileManager.default.fileExists(atPath: path) {
                    await appState.connect(socketPath: path)
                    break
                }
            }
        }

        // Show onboarding on first run
        if !UserDefaults.standard.bool(forKey: "hasCompletedOnboarding") {
            showOnboarding()
        }

    }

    // MARK: - Settings Window

    @objc func openSettings(_ sender: Any?) {
        openSettingsWindow()
    }

    func openSettingsWindow() {
        // If window exists and is visible, just bring it to front
        if let window = settingsWindow, window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let settingsView = SettingsView()
            .environment(appState)
        let hostingController = NSHostingController(rootView: settingsView)
        let window = NSWindow(contentViewController: hostingController)
        window.title = "Boo Settings"
        window.styleMask = [.titled, .closable, .miniaturizable]
        window.setContentSize(NSSize(width: 500, height: 380))
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.settingsWindow = window
    }

    // MARK: - Onboarding

    private func showOnboarding() {
        let onboardingView = OnboardingView(onComplete: { [weak self] in
            self?.completeOnboarding()
        })
        .environment(appState)

        let hostingController = NSHostingController(rootView: onboardingView)
        let window = NSWindow(contentViewController: hostingController)
        window.title = "Welcome to Boo"
        window.styleMask = [.titled, .closable]
        window.setContentSize(NSSize(width: 520, height: 460))
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.onboardingWindow = window
    }

    private func completeOnboarding() {
        UserDefaults.standard.set(true, forKey: "hasCompletedOnboarding")
        onboardingWindow?.close()
        onboardingWindow = nil
    }
}
