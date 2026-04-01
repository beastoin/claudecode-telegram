import SwiftUI
import BooCore

struct BooApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // Empty — menu bar app has no main window
        Settings {
            SettingsView()
                .environment(appDelegate.appState)
        }
    }
}
