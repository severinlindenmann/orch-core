import Foundation
import Testing
@testable import OrchProtocol

/// The phone's existing F1 signing path is ticket-only. Staged workspace grants must never enter it.
@Suite struct AddonStageVectorTests {
    let vectors = F1Vectors.load("signatures")

    @Test func stagedWorkspaceGrantsStayOutsidePhoneSigning() throws {
        let good = vectors["addon_event"]["event"]
        let refused = vectors["person_refused"].items.filter { $0["name"].str.hasPrefix("addon_") }
        #expect(refused.count == 3)
        for event in [good] + refused.map({ $0["event"] }) {
            #expect(event["staged"].str.hasPrefix("st_"))
            #expect(event["package_sha256"].str.hasPrefix("sha256:"))
            #expect(event.object?["capabilities"] != nil)
            #expect(throws: (any Error).self) {
                try PhoneEvents.signingBytes(event: event.object!, log: "workspace",
                                             workspace: ID16(hex: vectors["workspace_id"].str)!)
            }
        }
    }
}
