// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OsaurusSearch",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "OsaurusSearch", type: .dynamic, targets: ["Plugin"])
    ],
    targets: [
        .target(
            name: "Plugin",
            path: "Sources/Plugin"
        )
    ]
)
