import Foundation

/// Centralized constants for Boo configuration.
public enum BooDefaults {
    /// Peer time-to-live: 10 minutes.
    public static let peerTTL: TimeInterval = 600

    /// Message time-to-live: 5 minutes.
    public static let messageTTL: TimeInterval = 300

    /// App version — single source of truth for display when Bundle.main has no Info.plist.
    public static let appVersion = "0.2.7"
}
