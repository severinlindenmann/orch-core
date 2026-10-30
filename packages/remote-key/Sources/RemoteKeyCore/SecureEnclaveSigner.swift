import Foundation
#if canImport(CryptoKit) && canImport(LocalAuthentication)
import CryptoKit
import LocalAuthentication
import Security

/// The real Secure Enclave path. Exercised only on a Mac with a Secure Enclave (owner check with Touch ID);
/// on a VM or an Intel Mac without T2 it returns noSecureEnclave and nothing else. No software fallback.
///
/// UNBOUND PROTOTYPE: the key is created without a keychain access group, and the wrapped blob lives in a
/// 0600 file, so any process of the same OS user that can run this binary can ask the Secure Enclave to
/// use the key (a prompt still appears, with text chosen by that process). BOUND mode (release) needs a
/// Developer ID Application certificate: sign with a provisioning profile and the entitlement
///   keychain-access-groups = 77D4THRYUN.io.severin.orch.remote-key
/// and add `kSecAttrAccessGroup` where the access control is created below, so only the signed binary
/// can load the key. See README.
public struct SecureEnclaveSigner: PresenceSigner {
    public init() {}

    public func create() throws -> (pub: [UInt8], blob: [UInt8]) {
        guard SecureEnclave.isAvailable else { throw KeyError.noSecureEnclave }
        var cfErr: Unmanaged<CFError>?
        // Bound mode: also set the access group of the key's keychain item here.
        guard let ac = SecAccessControlCreateWithFlags(
            nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, [.privateKeyUsage, .userPresence], &cfErr)
        else { throw KeyError.unavailable }
        do {
            let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: ac)
            return ([UInt8](key.publicKey.x963Representation), [UInt8](key.dataRepresentation))
        } catch { throw KeyError.unavailable }
    }

    public func sign(blob: [UInt8], message: [UInt8], reason: String) throws -> [UInt8] {
        guard SecureEnclave.isAvailable else { throw KeyError.noSecureEnclave }
        let ctx = LAContext()  // fresh per signature: every signature prompts
        ctx.localizedReason = reason
        ctx.touchIDAuthenticationAllowableReuseDuration = 0
        do {
            let key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: Data(blob), authenticationContext: ctx)
            let sig = try key.signature(for: Data(message))
            return [UInt8](sig.rawRepresentation)  // raw r||s, no DER
        } catch let e as LAError {
            switch e.code {
            case .userCancel, .appCancel, .systemCancel, .authenticationFailed: throw KeyError.cancelled
            default: throw KeyError.unavailable
            }
        } catch { throw KeyError.unavailable }
    }
}
#else
public struct SecureEnclaveSigner: PresenceSigner {
    public init() {}
    public func create() throws -> (pub: [UInt8], blob: [UInt8]) { throw KeyError.noSecureEnclave }
    public func sign(blob: [UInt8], message: [UInt8], reason: String) throws -> [UInt8] { throw KeyError.noSecureEnclave }
}
#endif
