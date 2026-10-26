import CryptoKit
import Foundation
import OrchTestVectors
import Testing
@testable import OrchProtocol

/// The relay's HTTP examples (`contracts/relay/http_fixtures/`) are pinned by their own `MANIFEST.json`. This test makes the phone
/// notice a fixture revision it has not looked at: a change to any file, or a new revision, fails here until the number below is
/// bumped on purpose (#1301). The phone's own replay of the examples covers the routes the hub path still uses.
@Suite struct FixtureManifestTests {
    static let revisionSeen = 19

    private static func sha256Hex(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }

    @Test func everyFixtureFileMatchesTheManifestAndTheRevisionIsTheOneWeRead() throws {
        let manifest = try JSONDecoder().decode(Fixtures.JSON.self, from: Fixtures.rawData("MANIFEST.json"))
        let files = try #require(manifest["files"]?.object)
        let revision = try #require(manifest["revision"]?.int)
        #expect(revision == Int64(Self.revisionSeen), "the relay fixtures moved to revision \(revision): read the changes, then bump revisionSeen")
        let onDisk = Set(try Fixtures.allFileNames().filter { $0 != "MANIFEST.json" })
        #expect(onDisk == Set(files.keys), "fixture files and MANIFEST.json disagree")
        for (name, want) in files {
            #expect(Self.sha256Hex(try Fixtures.rawData(name)) == want.string, Comment(rawValue: name))
        }
        #expect(files.count >= 50)
    }

    @Test func everyJSONFixtureParsesInTheSchemaTheLoaderExpects() throws {
        let all = try Fixtures.loadAll()
        #expect(all.count >= 40)
        for f in all {
            #expect(f.contract == "orch-relay-http-v2" && f.suite == 2 && !f.cases.isEmpty, Comment(rawValue: f.fileName))
        }
    }
}
