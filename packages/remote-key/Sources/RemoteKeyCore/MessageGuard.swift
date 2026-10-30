import Foundation

/// Domain limit of the presence key (spec section 5, protocol 10.2c): it signs ONLY
///   open     = "orch/v2/remote-open|"     || ws(16) || device(16) || hh(32) || challenge(32)
///   presence = "orch/v2/remote-presence|" || ws(16) || device(16) || hh(32) || nonce(32)
///              || n(1) || digest(32) * n      (1 <= n <= 16, digests sorted ascending, unique)
/// Anything else is refused BEFORE any key is loaded or any prompt is shown.
public enum MessageGuard {
    public static let openLabel = Array("orch/v2/remote-open|".utf8)
    public static let presenceLabel = Array("orch/v2/remote-presence|".utf8)
    static let fixed = 16 + 16 + 32 + 32

    public static func isAllowed(_ msg: [UInt8]) -> Bool {
        if msg.starts(with: openLabel) {
            return msg.count == openLabel.count + fixed
        }
        if msg.starts(with: presenceLabel) {
            let head = presenceLabel.count + fixed
            guard msg.count > head else { return false }
            let n = Int(msg[head])
            guard (1...16).contains(n), msg.count == head + 1 + 32 * n else { return false }
            var prev: ArraySlice<UInt8>? = nil
            for i in 0..<n {
                let start = head + 1 + 32 * i
                let d = msg[start..<start + 32]
                if let p = prev, !p.lexicographicallyPrecedes(d) { return false }  // strictly ascending = sorted + unique
                prev = d
            }
            return true
        }
        return false
    }

    /// Prompt text: at most 200 bytes, printable (no control characters).
    public static func isValidReason(_ s: String) -> Bool {
        guard !s.isEmpty, s.utf8.count <= 200 else { return false }
        return !s.unicodeScalars.contains { $0.properties.generalCategory == .control || $0.value == 0x2028 || $0.value == 0x2029 }
    }
}
