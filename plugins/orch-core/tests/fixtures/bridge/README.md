# Bridge protocol test vectors

`bridge_vectors.json` is the contract of the Orch Remote bridge, version 1. It is a verbatim copy of
`tests/bridge_vectors.json` in severinlindenmann/orch-tix, where the specification
(`docs/bridge-protocol.md`) and the reference implementation that generates the file live.

- Source commit: `881354a87af797dc10b916f0a944ad38a726e14d` (severinlindenmann/orch-tix)
- sha256: `040f154bf62b96bb4e248fe4407e6ec02f43575b190b8f5ad2d2e195b3609191`

`tests/test_bridge_host_vectors.py` checks the sha256 above before it runs a single case, and runs every case
in the file against `orch.remote.bridge_host`. Every key in the file is fake (SHA-256 of a public label).

Never edit the file by hand. To refresh it after the specification changed:

1. In an orch-tix checkout at the new commit, copy the file exactly:
   `git -C <orch-tix> show <commit>:tests/bridge_vectors.json > tests/fixtures/bridge/bridge_vectors.json`
2. Record the new commit and `shasum -a 256 tests/fixtures/bridge/bridge_vectors.json` here and in
   `VECTORS_SHA256` in the test.
3. Run the vector tests. A case that now fails is a change of the contract: fix the implementation, never the
   vector.
