# remote-key: Secure Enclave presence helper (R5 T2, #1593)

`orch-remote-key` holds the **presence key** of a computer used for remote access: a Secure Enclave P-256 key with
`.userPresence` (Touch ID or the login password), non-exportable, created with `ThisDeviceOnly`. It signs **only** the two
remote messages of protocol 10.2c and refuses everything else before any key is loaded or any prompt is shown:

- `orch/v2/remote-open|` + ws(16) + device(16) + hh(32) + challenge(32)
- `orch/v2/remote-presence|` + ws(16) + device(16) + hh(32) + nonce(32) + n(1) + n sorted unique 32-byte digests (1 <= n <= 16)

No software fallback exists in the product: without a Secure Enclave it answers `unavailable` with `detail: no_secure_enclave`.
No private key is ever written to disk (the file under the store directory is the SE-wrapped blob, mode 0600).

## Protocol (one JSON request on stdin, one reply on stdout; base64url without padding)

| request | reply |
|---|---|
| `{"op":"create","label"?}` | `{"pub":b64u(65 bytes x9.63),"ref":hex32}` |
| `{"op":"sign","ref","msg":b64u,"reason"}` (reason: 1-200 bytes, no control characters) | `{"sig":b64u(64 bytes raw r||s)}` |
| `{"op":"delete","ref"}` | `{"ok":true}` |

Errors: `{"error":"cancelled|unavailable|refused|malformed"}` (`unavailable` adds `"detail":"no_secure_enclave"` when
there is no Secure Enclave). `refused` = wrong label or length, unknown ref. Extra or missing keys are `malformed`.
Every signature builds a fresh `LAContext` with reuse duration 0, so every signature prompts. Option: `--store DIR`
(default `~/Library/Application Support/orch/remote-key`).

## UNBOUND prototype, and what BOUND needs

This build is **unbound**: the key has no keychain access group, so any process of the same OS user that can run this binary
(or load the blob) can ask the Secure Enclave to sign; a prompt still appears, with text chosen by that process. Bound mode
(release) needs, from the owner: a **Developer ID Application** certificate (Account Holder), the bundle id
`io.severin.orch.remote-key`, a Developer ID provisioning profile, an app bundle, hardened runtime, notarization, and the
entitlement in `entitlements.plist` (`77D4THRYUN.io.severin.orch.remote-key`); the key is then created under that access
group (marked in `SecureEnclaveSigner.swift`). It will ship as a hash-pinned release asset.

## Tests

`swift test` (software signer in the test target only). `scripts/build-sign.sh` builds, ad-hoc signed unless
`ORCH_REMOTE_KEY_IDENTITY` names an installed certificate. `ci/remote-key.yml` is a proposed workflow (copy to
`.github/workflows/`). Manual owner check on a Mac with Touch ID: `create`, then `sign` an open message and see the
Touch ID sheet show the reason; cancel it and expect `cancelled`; verify the signature against `pub`.
