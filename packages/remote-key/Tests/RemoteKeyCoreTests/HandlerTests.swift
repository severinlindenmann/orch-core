import XCTest
import CryptoKit
@testable import RemoteKeyCore

/// Test-only software signer. The product has none.
final class FakeSigner: PresenceSigner {
    var prompts: [String] = []
    var cancel = false
    var unavailable = false
    func create() throws -> (pub: [UInt8], blob: [UInt8]) {
        if unavailable { throw KeyError.noSecureEnclave }
        let k = P256.Signing.PrivateKey()
        return ([UInt8](k.publicKey.x963Representation), [UInt8](k.rawRepresentation))
    }
    func sign(blob: [UInt8], message: [UInt8], reason: String) throws -> [UInt8] {
        if unavailable { throw KeyError.noSecureEnclave }
        prompts.append(reason)
        if cancel { throw KeyError.cancelled }
        let k = try P256.Signing.PrivateKey(rawRepresentation: Data(blob))
        return [UInt8](try k.signature(for: Data(message)).rawRepresentation)
    }
}

final class MemStore: BlobStore {
    var m: [String: [UInt8]] = [:]
    func put(ref: String, blob: [UInt8]) throws { m[ref] = blob }
    func get(ref: String) throws -> [UInt8]? { m[ref] }
    func delete(ref: String) throws { m[ref] = nil }
}

func openMsg() -> [UInt8] { MessageGuard.openLabel + [UInt8](repeating: 1, count: 96) }
func presMsg(_ ds: [[UInt8]]) -> [UInt8] {
    MessageGuard.presenceLabel + [UInt8](repeating: 2, count: 96) + [UInt8(ds.count)] + ds.flatMap { $0 }
}
func dig(_ b: UInt8) -> [UInt8] { [UInt8](repeating: b, count: 32) }

final class HandlerTests: XCTestCase {
    let signer = FakeSigner()
    let store = MemStore()
    lazy var h = Handler(signer: signer, store: store, makeRef: { "00112233445566778899aabbccddeeff" })

    func call(_ o: [String: Any]) -> [String: Any] {
        let d = try! JSONSerialization.data(withJSONObject: o)
        let out = h.handle(requestBytes: [UInt8](d))
        return try! JSONSerialization.jsonObject(with: Data(out)) as! [String: Any]
    }
    func create() -> (pub: P256.Signing.PublicKey, ref: String) {
        let r = call(["op": "create", "label": "x"])
        let pub = try! P256.Signing.PublicKey(x963Representation: Data(B64U.decode(r["pub"] as! String)!))
        return (pub, r["ref"] as! String)
    }
    func sign(_ ref: String, _ msg: [UInt8], reason: String = "Open Demo on this computer") -> [String: Any] {
        call(["op": "sign", "ref": ref, "msg": B64U.encode(msg), "reason": reason])
    }

    func testCreateSignOpenVerifies() {
        let (pub, ref) = create()
        let msg = openMsg()
        let r = sign(ref, msg)
        let sig = try! P256.Signing.ECDSASignature(rawRepresentation: Data(B64U.decode(r["sig"] as! String)!))
        XCTAssertTrue(pub.isValidSignature(sig, for: Data(msg)))
        XCTAssertEqual(signer.prompts, ["Open Demo on this computer"])
    }

    func testPresenceBatchSigns() {
        let (_, ref) = create()
        XCTAssertNotNil(sign(ref, presMsg([dig(1), dig(2), dig(9)]))["sig"])
    }

    func testOtherLabelsRefusedBeforePrompt() {
        let (_, ref) = create()
        for label in ["orch/v2/person-sig|", "orch/v2/device|", "", "orch/v2/remote-open", "orch/v2/remote-opens|"] {
            let r = sign(ref, Array(label.utf8) + [UInt8](repeating: 1, count: 96))
            XCTAssertEqual(r["error"] as? String, "refused", label)
        }
        XCTAssertEqual(signer.prompts, [])
    }

    func testBadLengthsAndDigestRulesRefused() {
        let (_, ref) = create()
        var bad: [[UInt8]] = [
            Array(openMsg().dropLast()), openMsg() + [0],
            presMsg([]),                                   // n = 0
            presMsg([dig(2), dig(1)]),                     // unsorted
            presMsg([dig(1), dig(1)]),                     // duplicate
            presMsg((1...17).map { dig(UInt8($0)) }),      // n = 17
        ]
        var short = presMsg([dig(1)]); short.removeLast(); bad.append(short)
        for m in bad { XCTAssertEqual(sign(ref, m)["error"] as? String, "refused") }
        XCTAssertEqual(presMsg((1...16).map { dig(UInt8($0)) }).count > 0, true)
        XCTAssertNotNil(sign(ref, presMsg((1...16).map { dig(UInt8($0)) }))["sig"])
        XCTAssertEqual(signer.prompts.count, 1)
    }

    func testCancelled() {
        let (_, ref) = create()
        signer.cancel = true
        XCTAssertEqual(sign(ref, openMsg())["error"] as? String, "cancelled")
    }

    func testNoSecureEnclave() {
        signer.unavailable = true
        let r = call(["op": "create"])
        XCTAssertEqual(r["error"] as? String, "unavailable")
        XCTAssertEqual(r["detail"] as? String, "no_secure_enclave")
    }

    func testRealSignerNeverCreatesKeysInTests() throws {
        // Never creates a hardware key from a test. Without a Secure Enclave the real signer must refuse cleanly;
        // with one (the owner's Mac, and this VM reports one) the real path is checked by hand, see README.
        #if canImport(CryptoKit)
        if SecureEnclave.isAvailable { throw XCTSkip("Secure Enclave reported available; manual check") }
        #endif
        XCTAssertThrowsError(try SecureEnclaveSigner().create()) { XCTAssertEqual($0 as? KeyError, .noSecureEnclave) }
        XCTAssertThrowsError(try SecureEnclaveSigner().sign(blob: [1], message: openMsg(), reason: "r")) {
            XCTAssertEqual($0 as? KeyError, .noSecureEnclave)
        }
    }

    func testMalformed() {
        XCTAssertEqual(call(["op": "nope"])["error"] as? String, "malformed")
        XCTAssertEqual(call(["op": "sign", "ref": "zz", "msg": "AA", "reason": "r"])["error"] as? String, "malformed")
        let (_, ref) = create()
        let m = B64U.encode(openMsg())
        XCTAssertEqual(call(["op": "sign", "ref": ref, "msg": m, "reason": "r", "extra": 1])["error"] as? String, "malformed")
        XCTAssertEqual(call(["op": "sign", "ref": ref, "msg": m + "=", "reason": "r"])["error"] as? String, "malformed")
        XCTAssertEqual(call(["op": "sign", "ref": ref, "msg": m, "reason": "a\nb"])["error"] as? String, "malformed")
        XCTAssertEqual(call(["op": "sign", "ref": ref, "msg": m, "reason": String(repeating: "a", count: 201)])["error"] as? String, "malformed")
        XCTAssertEqual(call(["op": "create", "x": 1])["error"] as? String, "malformed")
        XCTAssertEqual(h.handle(requestBytes: Array("not json".utf8)).isEmpty, false)
    }

    func testUnknownRefAndDelete() {
        let (_, ref) = create()
        XCTAssertEqual(call(["op": "delete", "ref": ref])["ok"] as? Bool, true)
        XCTAssertEqual(sign(ref, openMsg())["error"] as? String, "refused")
        XCTAssertEqual(call(["op": "delete", "ref": "../../etc/passwd"])["error"] as? String, "malformed")
    }

    func testFileStoreModes() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("rk-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let s = FileBlobStore(dir: dir)
        let ref = "00112233445566778899aabbccddeeff"
        try s.put(ref: ref, blob: [1, 2, 3])
        XCTAssertEqual(try s.get(ref: ref), [1, 2, 3])
        let a = try FileManager.default.attributesOfItem(atPath: dir.appendingPathComponent(ref + ".key").path)
        XCTAssertEqual(a[.posixPermissions] as? Int, 0o600)
        XCTAssertThrowsError(try s.get(ref: "../x"))
    }
}
