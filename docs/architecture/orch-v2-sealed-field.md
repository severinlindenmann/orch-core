# The sealed field (secrets between the browser and the click signer)

Contract for #1198 C5 (core side) and C6 (browser side). Written by the Opus security reviewer, 24 Oct 2026, from
`design-dashboard-first.md` §3 and `design-r1-remote-dashboard.md` §5. Code: `plugins/orch-core/src/orch/click/sealed.py`.
Vectors: `plugins/orch-core/tests/click_sealed_vectors.json` (both sides run the same file). Wiring into the signer
and the pages is C7 (grant reveal), C10 (hub backup key), C12 (passphrase), C13 (recovery code), C16 (GitHub token).

## 1. What it is for

Some dashboard actions need a secret the person types (the hub backup key, the recovery code, the passphrase for
"One click off", a GitHub token), and one shows a secret (an agent grant for a tool outside the dashboard). The
sealed field keeps these secrets out of the dashboard HTTP server, out of any proxy or TLS terminator in between, and
out of their logs, crash reports and memory dumps. Only the browser tab and the click signer process ever hold them.

**What it does not do (stated limits):**

- It is **not authentication**. The signer's public key is in the View, so anyone who sees the View (the dashboard
  server included) can seal a value to it. Who may act is decided as today: the session and the click on the shown
  review. The sealed field gives confidentiality and binding to one request, nothing more.
- Whoever **serves the page's JavaScript** (the dashboard server, the front, any TLS terminator) can change the page,
  read the secret before it is sealed, or swap the signer key in the View. The sealed field protects against passive
  exposure and byte relays, not an active server. That is why R1 §1 forbids third-party TLS termination.
- Script running in the page (XSS, extensions) sees what the person types. CSP stays strict.

**Deviation from the design note:** the design said X25519. The contract uses **P-256** because WebCrypto ECDH on
P-256 works in every supported browser, while X25519 in WebCrypto is recent. Security is equivalent for this use.

## 2. Purposes and directions (closed set)

| Purpose | Direction | Who seals | Who opens | Task |
|---|---|---|---|---|
| `hub.backup_key` | `in` | browser | signer | C10 |
| `device.recovery_code` | `in` | browser | signer | C13 |
| `custody.passphrase` | `in` | browser | signer | C12 |
| `github.token` | `in` | browser | signer | C16 |
| `grant.secret` | `out` | signer | browser | C7 |

A new purpose is a contract change: this table, `sealed.PURPOSES`, the browser's list and the vectors move together.
The direction is part of the AAD and the opener checks it structurally: the signer's key never opens an `out` value,
the browser's key never opens an `in` value (`context`).

## 3. Keys: who holds them, how long they live

- **`in` (signer's key).** The signer makes one fresh P-256 key pair per approval whose op asks for a secret, when it
  first builds that View. The public key goes into the View (so `view_digest` covers it); the private key lives only in
  the signer process memory, in the approval, and is never written to disk, never logged, never sent. It is the same
  key for every rebuild of that View (the digest must not change) and is dropped (`Recipient.close()`) when the
  approval ends: signed, cancelled, expired or refused. **The dashboard server never holds a private key.**
- **Binding (structural).** A recipient is made bound to its approval's `workspace_id` and `request_id`
  (`Recipient(direction, workspace_id=, request_id=)`) and pins the `view_digest` of its first successful open. Any
  later open with another workspace, request or view digest is refused `context`, whatever context the caller passes:
  a wiring bug that builds the context from the sign args cannot open a value of request A under request B's key.
- **`out` (browser's key).** The page makes a fresh P-256 key pair per sign call that expects a reveal
  (`generateKey({name:"ECDH", namedCurve:"P-256"}, false, ["deriveBits"])`, private key not extractable), sends the
  public key with the sign call, opens the reveal once, shows the secret with a copy button, then drops the key and
  the secret (on close, navigation or a timeout).
- **Ephemeral keys.** Every seal, in both directions, uses a fresh ephemeral key pair, used once and dropped. The nonce
  is derived from the key agreement, so reusing an ephemeral key would reuse key and nonce (leaking the XOR of the
  plaintexts and the GHASH key). The public API takes no key: `seal()` and `Recipient()` make their own; the
  vectors use the private helpers `_seal_with` and `Recipient._for_vectors`. The browser side does the same (a test
  shows two seals give two different `epk`).

## 4. The construction (version 1, `alg` = `P256-HKDF-SHA256-A256GCM`)

Inputs: the recipient public key `R` (65 bytes, uncompressed SEC1 `0x04||X||Y`), the secret (UTF-8 bytes), the context
`{purpose, workspace_id, request_id, view_digest}`.

1. **Pad.** `padded = u16be(n) || secret || zero bytes`, to the next multiple of 256 bytes. `1 <= n <= 4094`, so
   `len(padded)` is 256..4096. An empty secret is refused (`malformed`), a longer one `too_large`. The length of a
   passphrase is hidden within 256 bytes.
2. **Ephemeral key.** A fresh P-256 key pair `e`, `E` (65 bytes, uncompressed).
3. **ECDH.** `Z` = the 32-byte x coordinate of `e·R` (WebCrypto `deriveBits({name:"ECDH", public:R}, e, 256)`;
   Python `e.exchange(ec.ECDH(), R)`).
4. **AAD.** Each field as a 2-byte big-endian length then its bytes, in this order:
   `"orch-sealed-v1"`, direction (`"in"`/`"out"`), purpose, workspace_id, request_id, view_digest (ASCII, with the
   `sha256:` prefix), `E` (65 raw bytes), `R` (65 raw bytes).
5. **KDF.** `okm = HKDF-SHA256(ikm=Z, salt="orch-sealed-v1", info=AAD, L=44)`; `key = okm[0:32]` (AES-256),
   `nonce = okm[32:44]`.
6. **AEAD.** `ct = AES-256-GCM(key, nonce, padded, additionalData=AAD)` (ciphertext followed by the 16-byte tag, as
   WebCrypto returns it).
7. **Envelope** (JSON, exactly these four keys):
   `{"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "epk": b64url(E), "ct": b64url(ct)}`. base64url without padding.

The envelope does not name the purpose or the request. **The opener builds the context from its own state** (the
approval it holds, the field it asked for), never from anything the envelope or the relaying server says. That is what
makes a value sealed for one request, one View or one field useless for another.

## 5. Opening (both sides must refuse the same way)

In this order; any failure refuses with a static reason (never the secret, never crypto detail):

| Check | Reason |
|---|---|
| the recipient's key was dropped | `closed` |
| the context is invalid (unknown purpose; workspace id not 32 lower hex; request id not `hr_` + 26 Crockford; view digest not `sha256:` + 64 lower hex) or the purpose's direction is not this recipient's | `context` |
| the context's workspace or request is not the one the recipient was made for, or its view digest is not the one pinned by the first successful open | `context` |
| not an object with exactly `v`, `alg`, `epk`, `ct`; `v` not the integer 1 (`true` is not 1); another `alg` | `malformed` |
| `epk` or `ct` not canonical base64url without padding (`=`, `+`, `/`, a non-canonical last character) | `malformed` |
| `epk` not 65 bytes starting `0x04`, not on P-256, or equal to the recipient's own key | `malformed` |
| `len(ct) - 16` not a multiple of 256 in 256..4096 | `malformed` |
| this `epk` was opened already by this recipient | `replay` |
| AES-GCM does not authenticate (wrong key, wrong context, any bit changed) | `open_failed` |
| the padding is wrong (n = 0, n too big, a nonzero tail byte, more blocks than needed) | `malformed` |

A failed open does not mark the `epk` as used (a relay that corrupts a value cannot burn the real one; it could only
drop it anyway). The opened secret is returned as a `bytearray` sliced from the decrypted bytes through a memoryview,
that the caller wipes after use. This is **best effort**: in Python the AEAD's own output is an immutable `bytes` that
only the garbage collector releases; in the browser, overwrite the `Uint8Array` and drop references.

## 6. Replay

- **`in`:** each signer key belongs to exactly one approval, and the AAD carries the request id and the view digest, so
  a value never opens for another request or another View; the recipient's binding (§3) refuses it even before the
  AEAD. Inside the approval, a given `epk` opens once (`replay` after). A retyped value (the person mistyped the
  recovery code and the step failed) is a new seal with a new `epk` for the same View and opens: a retry needs no new
  View. The sign call consumes the approval, and the key is dropped with it. Failed opens are not counted: a GCM
  forgery succeeds with odds 2^-128 per attempt, and a relay that learns `replay` from `open_failed` learns nothing
  of value.
- **`out`:** the browser key is made for one sign call and dropped after the one reveal. The signer seals a grant
  secret at most once per approval (C7 enforces this), and a replayed or recorded reveal is useless once the tab drops
  its key.

## 7. Wire fields (for C7/C10/C12/C13/C16; names fixed here)

- View (only when the op asks for a secret): `"seal": {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "pk": <b64url R>,
  "purposes": [<purpose>, ...]}`. Inside the View, so `view_digest` covers it.
- `sign` args: `"sealed": {<purpose>: <envelope>}` for typed secrets; `"reveal_key": <b64url browser public key>`
  when the op reveals a grant secret. **The signer refuses a `sealed` whose keys are not exactly the View's
  `purposes`**: a purpose the View did not ask for, or a missing one, is `invalid.input` before anything is opened (C10
  and the other wiring tasks test this).
- `sign` result: `"reveal": {"purpose": "grant.secret", "envelope": <envelope>}`. The dashboard server passes it
  through untouched and never logs it.
- The click log and the records never hold an envelope (a ciphertext under a key that is dropped is harmless, but the
  rule keeps the logs simple).

## 8. Test vectors

`plugins/orch-core/tests/click_sealed_vectors.json`, format `orch.sealed-vectors/1`, deterministic (all keys come from
fixed seeds). Keys are JWK (`kty`, `crv`, `d`, `x`, `y`).

- `seal`: one case per purpose (and a UTF-8 passphrase, 254- and 255-byte boundaries). With `ephemeral_jwk` fixed,
  sealing `plaintext` to `recipient_public` for `context` gives exactly `envelope`; every intermediate
  (`shared_secret_hex`, `aad_hex`, `key_hex`, `nonce_hex`, `padded_hex`) is listed.
- `open`: the recipient (`direction` `in` = the signer, `out` = the browser), made bound to `bound.workspace_id` and
  `bound.request_id`, opens `envelope` for `context`; `expect` is
  `ok` (the plaintext of the `base` seal case) or a reason of §5. Adversarial cases for both directions: another
  request, view digest, workspace or recipient; a recipient bound to another request or workspace (`context`);
  flipped ciphertext and tag bits; truncated ciphertext and `epk`; compressed, off-curve and negated `epk` (`-E` gives
  the same `Z`; only `E` in the AAD refuses it); a non-canonical last base64 character in `epk` and `ct`; `epk` equal
  to the recipient key; extra or missing fields; another version or
  algorithm; padded and standard base64; swapped purposes; a purpose of the other direction; four bad paddings.
- Replay and the view-digest pin are stateful and are tested in code, not in the file.

Regenerate only on a contract change: `uv run python tests/host/test_click_sealed.py` (from `plugins/orch-core`). The
Python test fails while the file and the code disagree. The construction was cross-checked against Node's WebCrypto
(every seal vector and every `out` open vector) when the vectors were written.
