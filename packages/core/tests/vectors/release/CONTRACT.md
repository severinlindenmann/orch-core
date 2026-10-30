# Signed-release contract (`orch.release-envelope/1`)

Cross-repo contract for install-update.md §4 and addon-platform.md §3. Core (`orch/update/verify.py`), the verifier
embedded in install.sh, and the release tooling (`scripts/release/`) must all pass `core.json` and `catalog.json`.
The keys in `keys.json` are TEST keys from public seeds: never embed them in a shipped verifier.
Regenerate with `uv run python tests/vectors/release/gen_release_vectors.py`; `test_vectors_shape.py` holds a
reference verifier written from this file.

## 1. Encoding

- **Signed documents travel as bytes.** The manifest/catalog, the key certificate and the revocation list are each
  carried as base64 of their exact bytes, and each signature covers `domain + bytes`. The verifier never
  canonicalises; it parses the verified bytes and refuses them unless they are canonical (`malformed`).
- **Canonical** = RFC 8785 (JCS) over this value domain: objects with ASCII keys, strings, integers in
  `0..2^53-1`, booleans, lists. No floats (`42.0` is refused), no `NaN`/`Infinity`, no duplicate keys.
  For this domain, Python's `json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` in UTF-8 is JCS.
- **Exact key sets.** Every object listed below has exactly the listed keys; an unknown or missing key is `malformed`.
  A new field needs a new format version. Exception: entries under the catalog's `addons` (checked by A2, not here).
- **base64** is RFC 4648 §4 with padding, no whitespace, and must re-encode to the same string. Ed25519 public keys
  are 32 raw bytes, signatures 64 raw bytes.
- **Times** are `YYYY-MM-DDTHH:MM:SSZ` (UTC, seconds). **Key ids** match `[a-z][a-z0-9-]{0,31}`.
  **Channels** match `main|develop|test-[a-z0-9][a-z0-9-]{0,30}` (the pilot releases publish only `main` and `test-*`; a production verifier refuses `test-*`). **Versions** match `\d+\.\d+\.\d+(\.dev\d+)?`.
- The outer envelope is not signed: it must be strict JSON with the exact key set, but need not be canonical.

## 2. Formats

```
envelope   {"format":"orch.release-envelope/1",
            "payload":"<b64 manifest or catalog bytes>",
            "sig":{"alg":"ed25519","key_id":"rk-3","value":"<b64 64>"},
            "key_cert":{"cert":"<b64 cert bytes>","root_key_id":"root-1","sig":"<b64 64>"},
            "revocations":{"doc":"<b64 revocation bytes>","root_key_id":"root-1","sig":"<b64 64>"}}
cert       {"format":"orch.release-key/1","key_id","public_key":"<b64 32>","serial":int>=1,
            "purpose":"core"|"addon-catalog","channels":[unique channel | "test-*"],"not_before","not_after"}
revocations {"format":"orch.revocations/1","min_serial":int>=1,"revoked":[sorted unique ints],"issued_at"}
manifest   {"format":"orch.release/1","channel","version","seq":int>=1,"commit":"<40 lowercase hex>",
            "issued_at","expires_at","files":[file list, 2..6 entries],"changelog":"<= 4096 UTF-8 bytes","min_from":"<version>"}
file       {"role":"core"|"dashboard"|"hub"|"relay"|"publish"|"constraints","name":"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}",
            "sha256":"<64 lowercase hex>","size":int>=1,"url":"https://… (<= 512 chars, printable, no spaces)"}
           The file list: every role at most once (duplicate role is `malformed`), no other role (unknown role is
           `malformed`), and `core` and `constraints` are mandatory (missing is `malformed`). `constraints` is the
           hash-locked pin file the installer uses with `--require-hashes`. File names are unique, also ignoring case (`malformed`). More than 6 entries, or a role that is not a string, is `malformed`.
catalog    {"format":"orch.addon-catalog/1","channel","seq":int>=1,"issued_at","expires_at","addons":{…}}
```

Domains (signature input = domain + bytes):

| Signed bytes | Domain |
|---|---|
| core manifest | `orch-release/1\n` |
| add-on catalog | `orch-addon-catalog/1\n` |
| key certificate | `orch-release-key/1\n` |
| revocation list | `orch-revocations/1\n` |

## 3. Verification (inputs: envelope bytes, expected purpose, requested channel, now, stored state)

Stored state: trusted roots, `seq_seen` for this channel and purpose (or none), stored `revocations_issued_at` (or
none). Run in this order; the first failing rule gives the reason. Every rule fails closed.

1. Outer envelope: strict JSON, exact keys, `format`, `alg == "ed25519"`, all base64 well formed → else `malformed`.
2. `key_cert.root_key_id` trusted → `unknown_root`; root signature over the cert bytes → `bad_cert_sig`; cert bytes
   canonical and well formed → `malformed`.
3. `revocations.root_key_id` trusted → `unknown_root`; root signature → `bad_revocations_sig`; doc canonical and well
   formed → `malformed`; doc `issued_at` < stored → `revocations_rollback` (equal is fine: every envelope repeats it).
4. Cert: `purpose` == expected → `wrong_purpose`; channel listed (`test-*` covers every `test-<name>`) →
   `channel_not_allowed`; `not_before <= now` → `cert_not_yet_valid`; `now < not_after` → `cert_expired`;
   `serial >= min_serial` and not in `revoked` → `key_revoked`.
5. `sig.key_id == cert.key_id` → `key_mismatch`; Ed25519 over `domain(expected purpose) + payload` with
   `cert.public_key` → `bad_sig`.
6. Payload bytes canonical → `malformed`; `format` is the one for the expected purpose → `wrong_format`; rest well
   formed → `malformed`; `channel` == requested → `wrong_channel`; `now < expires_at` → `expired`;
   `seq >= seq_seen` → `rollback` (equal is fine: reruns and weekly refreshes).
7. Core only, when files are downloaded (absent in a case = `--check`, no files yet): for each manifest file, the
   file with that `name` has `size` → `size_mismatch` (also when missing), then `sha256` → `hash_mismatch`.

On success the caller stores `seq_seen = max(seq_seen, seq)`, the revocation list's `issued_at` and `min_serial`.

## 4. Reasons and catalog codes

| Reason | Code |
|---|---|
| `ok` | none (exit 0) |
| `revocations_rollback`, `expired`, `rollback` | `update.stale` |
| every other reason | `update.unverified` |

Vector case fields: `name`, `pins` (what it pins), `purpose`, `channel`, `now`, `state`, `envelope` (exact bytes as
a string), optional `files` (`name`, `content_b64`), `expect` = `{reason, code}` plus, for `ok`, `accept` =
`{channel, seq, version, min_serial, revocations_issued_at}` (`version` is null for a catalog).
