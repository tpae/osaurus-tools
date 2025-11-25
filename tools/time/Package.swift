// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OsaurusTime",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "OsaurusTime", type: .dynamic, targets: ["Plugin"])
    ],
    targets: [
        .target(
            name: "Plugin",
            path: "Sources/Plugin"
        )
    ]
)
