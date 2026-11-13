// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ChannelsWatch",
    platforms: [.macOS(.v13)],
    targets: [.executableTarget(name: "ChannelsWatch", path: "Sources")]
)
