import Foundation

/// Test-only access to the protocol vectors. They live once, in the repository's `contracts/` folder (and core's package data);
/// this target reads them from there by path, so there is no copy to drift (see PROVENANCE.md next to this file).
/// The tests run on the machine that built them (`swift test`, or the iOS Simulator, which shares the host file system).
public enum Vectors {
    /// The repository root: this file is `packages/mobile/OrchKit/Tests/OrchTestVectors/Vectors.swift`.
    public static let root: URL = {
        var u = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { u.deleteLastPathComponent() }
        return u
    }()
    public static var contracts: URL { root.appendingPathComponent("contracts", isDirectory: true) }

    /// `contracts/relay/vectors_v2.json`: the relay protocol v2 vectors.
    public static var url: URL { contracts.appendingPathComponent("relay/vectors_v2.json") }
    public static var data: Data { try! Data(contentsOf: url) }

    /// `contracts/hub/vectors_hub.json` (hub contract H0, FAKE keys).
    public static var hub: Data { try! Data(contentsOf: contracts.appendingPathComponent("hub/vectors_hub.json")) }

    /// `contracts/origin_http.json`: the shared plain-http origin rule.
    public static var originHTTP: Data { try! Data(contentsOf: contracts.appendingPathComponent("origin_http.json")) }

    /// F1 vectors from `contracts/core/f1/`, by file name without extension. `reader_kat_1` ships inside core's package.
    public static func f1(_ name: String) -> Data {
        let u = name == "reader_kat_1"
            ? root.appendingPathComponent("packages/core/src/orch/identity/reader_kat_1.json")
            : contracts.appendingPathComponent("core/f1/\(name).json")
        return try! Data(contentsOf: u)
    }

    /// Core's Unicode 16 tables (`packages/core/src/orch/canon/_unicode16.py`), the source of the phone's generated tables.
    public static var coreUnicode16: Data {
        try! Data(contentsOf: root.appendingPathComponent("packages/core/src/orch/canon/_unicode16.py"))
    }

    /// `contracts/core/r2_framing.json`: core's R2 bridge framing vectors.
    public static var r2Framing: Data { try! Data(contentsOf: contracts.appendingPathComponent("core/r2_framing.json")) }
}
