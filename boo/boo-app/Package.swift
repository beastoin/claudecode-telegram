// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "BooApp",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "BooCore", targets: ["BooCore"]),
        .executable(name: "BooApp", targets: ["BooApp"]),
    ],
    targets: [
        .target(
            name: "BooCore",
            path: "Sources/BooCore"
        ),
        .executableTarget(
            name: "BooApp",
            dependencies: ["BooCore"],
            path: "Sources/BooApp"
        ),
        .testTarget(
            name: "BooCoreTests",
            dependencies: ["BooCore"],
            path: "Tests/BooCoreTests"
        ),
    ]
)
