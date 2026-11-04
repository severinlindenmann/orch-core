import Foundation

/// One JSON request in, one JSON reply out (design 2.3).
///   {"op":"create"}                          -> {"pub":b64u(65),"ref":hex32}
///   {"op":"sign","ref","msg":b64u,"reason"}  -> {"sig":b64u(64)}
///   {"op":"delete","ref"}                    -> {"ok":true}
/// Errors: {"error":"cancelled|unavailable|refused|malformed"}; unavailable adds
/// "detail":"no_secure_enclave" when the Mac has no Secure Enclave.
/// The design lists an optional "label" on create; it is accepted (a text of at most 64 bytes) and ignored.
public struct Handler {
    let signer: PresenceSigner
    let store: BlobStore
    let makeRef: () -> String

    public init(signer: PresenceSigner, store: BlobStore,
                makeRef: @escaping () -> String = { (0..<16).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined() }) {
        self.signer = signer; self.store = store; self.makeRef = makeRef
    }

    static func err(_ e: String, detail: String? = nil) -> [String: Any] {
        var d: [String: Any] = ["error": e]
        if let detail = detail { d["detail"] = detail }
        return d
    }
    static func map(_ e: Error) -> [String: Any] {
        switch e as? KeyError {
        case .noSecureEnclave: return err("unavailable", detail: "no_secure_enclave")
        case .cancelled: return err("cancelled")
        case .refused: return err("refused")
        default: return err("unavailable")
        }
    }

    public func handle(requestBytes: [UInt8]) -> [UInt8] {
        let reply = handle(requestBytes)
        return (try? JSONSerialization.data(withJSONObject: reply, options: [.sortedKeys])).map { [UInt8]($0) } ?? Array("{\"error\":\"malformed\"}".utf8)
    }

    func handle(_ bytes: [UInt8]) -> [String: Any] {
        guard bytes.count <= 4096,
              let obj = try? JSONSerialization.jsonObject(with: Data(bytes)) as? [String: Any],
              let op = obj["op"] as? String else { return Self.err("malformed") }
        switch op {
        case "create":
            let extra = Set(obj.keys).subtracting(["op", "label"])
            if !extra.isEmpty { return Self.err("malformed") }
            if let l = obj["label"] { guard let s = l as? String, s.utf8.count <= 64 else { return Self.err("malformed") } }
            do {
                let (pub, blob) = try signer.create()
                guard pub.count == 65, pub.first == 4 else { return Self.err("unavailable") }
                let ref = makeRef()
                try store.put(ref: ref, blob: blob)
                return ["pub": B64U.encode(pub), "ref": ref]
            } catch { return Self.map(error) }
        case "sign":
            guard Set(obj.keys) == ["op", "ref", "msg", "reason"],
                  let ref = obj["ref"] as? String, FileBlobStore.validRef(ref),
                  let msgS = obj["msg"] as? String, let msg = B64U.decode(msgS),
                  let reason = obj["reason"] as? String, MessageGuard.isValidReason(reason)
            else { return Self.err("malformed") }
            // Domain limit first: no key load, no prompt for anything but the two remote labels.
            guard MessageGuard.isAllowed(msg) else { return Self.err("refused") }
            do {
                guard let blob = try store.get(ref: ref) else { return Self.err("refused") }
                let sig = try signer.sign(blob: blob, message: msg, reason: reason)
                guard sig.count == 64 else { return Self.err("unavailable") }
                return ["sig": B64U.encode(sig)]
            } catch { return Self.map(error) }
        case "delete":
            guard Set(obj.keys) == ["op", "ref"], let ref = obj["ref"] as? String, FileBlobStore.validRef(ref)
            else { return Self.err("malformed") }
            do { try store.delete(ref: ref); return ["ok": true] } catch { return Self.map(error) }
        default:
            return Self.err("malformed")
        }
    }
}
