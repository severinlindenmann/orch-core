// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "remote-key",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "orch-remote-key", targets: ["orch-remote-key"])],
    targets: [
        .target(name: "RemoteKeyCore"),
        .executableTarget(name: "orch-remote-key", dependencies: ["RemoteKeyCore"]),
        .testTarget(name: "RemoteKeyCoreTests", dependencies: ["RemoteKeyCore"]),
    ]
)
