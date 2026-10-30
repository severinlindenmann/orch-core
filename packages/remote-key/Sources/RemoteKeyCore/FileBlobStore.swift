import Foundation

/// Unbound (prototype) storage: the Secure Enclave key blob in a 0600 file under a 0700 directory.
/// The blob is the SE-wrapped key reference, never private key material. In bound mode (Developer ID
/// signed build) this becomes a data-protection keychain item under access group
/// 77D4THRYUN.io.severin.orch.remote-key (see README), and ONLY that signed binary can load the key.
public struct FileBlobStore: BlobStore {
    let dir: URL
    public init(dir: URL) { self.dir = dir }

    static func validRef(_ r: String) -> Bool {
        r.utf8.count == 32 && r.utf8.allSatisfy { ($0 >= 48 && $0 <= 57) || ($0 >= 97 && $0 <= 102) }
    }
    func path(_ ref: String) throws -> URL {
        guard Self.validRef(ref) else { throw KeyError.refused }
        return dir.appendingPathComponent(ref + ".key")
    }
    public func put(ref: String, blob: [UInt8]) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let p = try path(ref)
        guard fm.createFile(atPath: p.path, contents: Data(blob), attributes: [.posixPermissions: 0o600]) else {
            throw KeyError.unavailable
        }
    }
    public func get(ref: String) throws -> [UInt8]? {
        guard let d = FileManager.default.contents(atPath: try path(ref).path) else { return nil }
        return [UInt8](d)
    }
    public func delete(ref: String) throws {
        let p = try path(ref)
        if FileManager.default.fileExists(atPath: p.path) { try FileManager.default.removeItem(at: p) }
    }
}
