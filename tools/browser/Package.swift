// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OsaurusBrowser",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "OsaurusBrowser", type: .dynamic, targets: ["Plugin"])
    ],
    targets: [
        .target(
            name: "Plugin",
            path: "Sources/Plugin",
            linkerSettings: [
                .linkedFramework("WebKit"),
                .linkedFramework("AppKit"),
            ]
        )
    ]
)
