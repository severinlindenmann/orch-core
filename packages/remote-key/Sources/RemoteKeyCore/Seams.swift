import Foundation

public enum KeyError: Error, Equatable {
    case noSecureEnclave   // wire: error "unavailable", detail "no_secure_enclave"
    case cancelled
    case unavailable
    case refused
}

/// The seam between the protocol and the hardware. Production: SecureEnclaveSigner.
/// Tests: a software signer living in the test target only. There is NO software signer in this module.
public protocol PresenceSigner {
    /// Makes a key. Returns the 65-byte uncompressed P-256 public key and an opaque blob
    /// (for the Secure Enclave: its wrapped dataRepresentation, not the private key).
    func create() throws -> (pub: [UInt8], blob: [UInt8])
    /// Raw r||s (64 bytes) over the message. Must prompt for user presence on every call.
    func sign(blob: [UInt8], message: [UInt8], reason: String) throws -> [UInt8]
}

public protocol BlobStore {
    func put(ref: String, blob: [UInt8]) throws
    func get(ref: String) throws -> [UInt8]?
    func delete(ref: String) throws
}
