import AppKit
import SwiftUI
import BooCore

/// Controls the NSStatusItem (menu bar icon) and its popover.
@MainActor
final class StatusItemController {
    private let statusItem: NSStatusItem
    private let popover: NSPopover
    private let appState: AppState
    private var eventMonitor: Any?

    init(appState: AppState) {
        self.appState = appState
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        self.popover = NSPopover()

        setupButton()
        setupPopover()
        setupEventMonitor()
        setupNotificationObserver()
    }

    private func setupButton() {
        guard let button = statusItem.button else { return }
        button.image = BooIcons.menuBarIcon
        button.action = #selector(togglePopover)
        button.target = self
    }

    private func setupPopover() {
        popover.contentSize = NSSize(width: 320, height: 400)
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = NSHostingController(
            rootView: PopoverView()
                .environment(appState)
        )
    }

    private func setupEventMonitor() {
        // Close popover when clicking outside
        eventMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            if let self, self.popover.isShown {
                self.popover.performClose(nil)
            }
        }
    }

    private func setupNotificationObserver() {
        NotificationCenter.default.addObserver(
            forName: .init("ShowBooPopover"), object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                if let button = self.statusItem.button, !self.popover.isShown {
                    self.popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
        }
    }

    @objc private func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
        } else if let button = statusItem.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            // Bring popover to front
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
