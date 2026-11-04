import Foundation
import RemoteKeyCore

// Usage: orch-remote-key [--store DIR]   (one JSON request on stdin, one JSON reply on stdout)
var dir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/orch/remote-key")
var args = Array(CommandLine.arguments.dropFirst())
if args.count == 2, args[0] == "--store" { dir = URL(fileURLWithPath: args[1]); args = [] }
guard args.isEmpty else {
    FileHandle.standardError.write(Data("usage: orch-remote-key [--store DIR]\n".utf8))
    exit(2)
}
let input = [UInt8](FileHandle.standardInput.readDataToEndOfFile())
let handler = Handler(signer: SecureEnclaveSigner(), store: FileBlobStore(dir: dir))
FileHandle.standardOutput.write(Data(handler.handle(requestBytes: input)))
FileHandle.standardOutput.write(Data("\n".utf8))
