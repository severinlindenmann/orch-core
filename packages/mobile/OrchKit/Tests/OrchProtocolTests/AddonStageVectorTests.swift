import Foundation
import Testing
@testable import OrchProtocol

@Suite struct AddonStageVectorTests {
    let vectors = F1Vectors.load("signatures")
    let label = "orch/v2/sig/ws-event|"

    func message(_ event: JSONValue) throws -> Data {
        let excluded: Set<String> = ["seq", "at", "prev", "ws_seq", "sig", "host_sig"]
        let payload = event.object!.filter { !excluded.contains($0.key) }
        return try canonicalJSON(.object([
            "contract": .int(1), "suite": .int(2), "workspace_id": vectors["workspace_id"],
            "log": .string("workspace"), "event": .object(payload)
        ]))
    }

    @Test func stageDigestAndCapsAreSigned() throws {
        let c = vectors["addon_event"], event = c["event"]
        let key = SigningPublicKey.fromB64u(c["cert"]["o"]["dk_sig_pub"].str)
        let bytes = try message(event)
        #expect(Hex.encode(Data(label.utf8) + bytes) == c["person_signing_hex"].str)
        #expect(Signature.verify(key, signature: event["sig"].b64Data, label: label, message: bytes))
        let refused = vectors["person_refused"].items.filter { $0["name"].str.hasPrefix("addon_") }
        #expect(refused.count == 3)
        for bad in refused {
            #expect(!Signature.verify(key, signature: bad["event"]["sig"].b64Data, label: label, message: try message(bad["event"])))
        }
    }
}
