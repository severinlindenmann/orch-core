# orch hub protocol, contract H0 (`orch.hub/0`)

This is the wire contract between the orch hub, orch core (the workspace host), orch-publish, orch-relay and, from
phase 4, orch-mobile. It fixes the exact bytes of every hub object and what each verifier does with an input that is
not right.

- **Spec:** orch-core `docs/architecture/orch-v2-hub.md` (D67–D82), written *spec §n* below.
- **Plan:** orch-core #838. The V3 DECISIONS (V3-D3, V3-D4, V3-D8) and the SHORTCUTS table apply.
- **Base contract:** orch-relay `docs/protocol-v2.md`, written *v2 §n*. This contract reuses its suite, encodings,
  signed-object shape and label rules unchanged. There is no second encoding.
- **Vectors:** [`vectors/vectors_hub.json`](../vectors/vectors_hub.json), one file shared by hub, core and publish
  (V3-D8). The reference code that produces it is `src/orch_hub/contract/`; `uv run pytest tests/test_vectors.py`
  checks that the reference rebuilds the file byte for byte and that every case replays from its JSON. Where the
  reference and this document disagree, this document wins.
- **Status:** H0 draft for the L2 gate (one Opus security pass, orch-core #847). Choices this document had to make
  are marked **DECIDED HERE** and listed in §16.

Conventions are v2's: `||` concatenation, `H` = SHA-256, ids are 16 bytes (32 lower-case hex in JSON), every other
byte string is canonical unpadded b64u, `lp16(x)` is a 2-byte big-endian length then `x`, `u64` is 8 bytes big-endian.
"MUST" is a requirement.

## 1. Scope and phases

| § | Object | Signer | Verifier | Phase |
|---|---|---|---|---|
| 4 | `hub.op_cert`, `hub.op_revoked` | `HK_root` | host, publish, relay, phone | 2 |
| 5 | hub card `hub.card` | `HK_op` | host (pins `HK_root`), publish, relay | 2 |
| 6 | `hub.uk_cert` | `HK_op` | host | 4 (format fixed now) |
| 6 | `hub.channel_cert` | `HK_op` | host, hub | 4 |
| 7 | group snapshot `hub.snapshot` | `HK_op` | host | 3 (selective: 2, roles unused) |
| 8 | custodial event | `UK` + channel key | host | 4 |
| 9 | `hub.joined`, `hub.left` (workspace-log events) | owner's device key | host, readers | 2 |
| 9 | `hub.binding`, `hub.unbound` | `WSK` | publish, relay, hub | 2 |
| 10 | link messages `hub.link`, link auth | `WSK` (w2h), `HK_op` (h2w) | hub, host | 2 |
| 11 | publish through the hub, `hub.publish_approval` | `WSK`, `HK_op`, owner's device key | orch-publish | 2 |

Not in H0 (named so nobody builds them from guesswork): the hub audit chain object (D4; label reserved), snapshot
generations (#910), the standalone signed alias (C4), the `webauthn` passkey channel contract (#908), hub↔hub
objects (H7), relay hub accounts (E1).

## 2. Common rules

### 2.1 Suite and encodings

Suite 2 only (v2 §1.1, D46): ECDSA P-256/SHA-256 with raw 64-byte `r || s`, `1 ≤ r, s ≤ n−1`, high-s accepted (so
no signature is ever an identifier); public keys are 65-byte uncompressed points on the curve. Every JSON object that
is signed or hashed is in the v2 §2.3 subset and is hashed or signed as `cj(o)`. Receivers parse strictly (duplicate
keys, floats, NaN, unsafe integers, non-ASCII keys, invalid UTF-8 and nesting over 16 are refused) and verify over
`cj` of the parsed object, never over the received bytes. b64u and hex are canonical (v2 §2.2): padding, the standard
alphabet, non-zero trailing bits, upper-case hex and wrong lengths are refused.

### 2.2 Signed objects

Exactly v2 §2.4: `{"o": <object>, "sig": <b64u>}` with exactly those two keys (the card and the approval carry named
extra members, §5 and §11.3), and

```
sig = Sign(key, label || cj(o))
```

Every hub object carries `"v": 2`, `"suite": 2`, `"kind": "hub.<name>"` and **`"hv": 0`**, the hub contract version.
**DECIDED HERE:** `hv` lets a later hub contract change a field set without reusing a kind; a verifier refuses any
`hv` other than the one it implements (`malformed`).

**Verification order for every signed object** (v2 §2.4), then the object's own steps:

1. the envelope keys, `kind` → `malformed`; `suite` → `wrong_suite`; `v`, `hv` → `malformed`;
2. the signature, under the object's one label and the key the context names → `bad_signature`. A field that names
   the verifying key is read first and must itself be a valid key (`malformed`);
3. the exact field set and every field's type → `malformed`. An extra or a missing field is refused;
4. the object's semantic steps, in the order given in its section;
5. the dev marker (§2.4) where the object has one.

All checks fail closed: an input a verifier cannot read is refused, never skipped. A refusal carries its code and the
fields listed in §14 only, and never echoes the input.

### 2.3 Ids

```
hub_id     = H("orch/v2/id/hub|"     || suite || hk_root_pub)[0:16]
channel_id = H("orch/v2/id/channel|" || suite || channel_key_pub)[0:16]
person_id, device_id: v2 §4 (unchanged)
user_id, cert_id, workspace_id: 16 CSPRNG bytes
```

**DECIDED HERE:** `hub_id` is derived from `HK_root` (like `person_id` from `PK`), so a hub id cannot be claimed
without the root key, and every holder recomputes it (`hub_id_mismatch`). `user_id` is pseudonymous and random (spec
§5). In events a user appears as `"u_" + user_id`, a person as `"p_" + person_id` (ticket-format §5.2).

### 2.4 The dev marker (all of V3)

Every `hub.op_cert`, `hub.card`, `hub.uk_cert`, `hub.channel_cert`, `hub.snapshot`, `hub.binding`, `hub.unbound` and
`hub.op_revoked` carries a required boolean `dev`. **In V3 every one of them is `dev: true`** (V3-D2: all hub work is
dev-only).

- `dev` is a required field: an object without it is `malformed`.
- **A real verifier (a real workspace, a real hub, a real orch-publish) refuses `dev: true` → `dev_refused`.** This
  is spec §6 "Dev hubs": a real workspace refuses a dev hub and dev certificates; a real hub refuses a dev workspace
  join. The check runs on every object, not only on the card, so a dev certificate under a real card is refused too
  (vector `uk_cert_dev_at_real`).
- A dev verifier refuses `dev: false` → `dev_mismatch`, so a dev setup never mixes in real objects.
- Whether a verifier is real or dev is its own configuration (the host state dir marks dev workspaces), never a
  field of the object it checks. The test human override (D34, D51) never applies on the hub path. The orch-core test
  that proves the override never works on a real workspace is extended to these refusals (C1).

### 2.5 Clocks

Every check uses the verifier's own clock. Certificates: `issued_ms ≤ now + 300 s` else `cert_invalid`;
`expires_ms > issued_ms` else `cert_invalid`; `now ≥ expires_ms` → `cert_expired`. Link messages and publish requests:
`|now − ts| ≤ 120 s` → `stale_timestamp`.

## 3. Signing domains

Every label is new, ends in `|`, and the union with every v2 label (v2 §3, ticket-format §5.6) is prefix-free
(`tests/test_labels.py`, and `tests/test_core_crosscheck.py` against the live orch-core table). **DECIDED HERE:** the
spec writes the `HK_op` domains without the trailing `|` (`orch/v2/hub-card`, …); they get it, as v2 §3 did for
`orch/v2/publish`. Domains of a workspace key follow v2's `orch/v2/sig/…|` form; domains of hub keys keep the spec's
literal `orch/v2/hub-…|` names.

| Name | Label | Signer | Covers | § |
|---|---|---|---|---|
| `sig_hub_op_cert` | `orch/v2/hub-op-cert\|` | `HK_root` | `cj(o)` of `hub.op_cert` | 4 |
| `sig_hub_op_revoked` | `orch/v2/hub-op-revoked\|` | `HK_root` | `cj(o)` of `hub.op_revoked` | 4 |
| `sig_hub_card` | `orch/v2/hub-card\|` | `HK_op` | `cj(o)` of `hub.card` | 5 |
| `sig_hub_uk_cert` | `orch/v2/hub-uk-cert\|` | `HK_op` | `cj(o)` of `hub.uk_cert` | 6 |
| `sig_hub_channel_cert` | `orch/v2/hub-channel-cert\|` | `HK_op` | `cj(o)` of `hub.channel_cert` | 6 |
| `sig_hub_snapshot` | `orch/v2/hub-snapshot\|` | `HK_op` | `cj(o)` of `hub.snapshot` | 7 |
| `sig_hub_route` | `orch/v2/hub-route\|` | `HK_op` | `cj(o)` of a hub→host `hub.link` | 10 |
| `sig_hub_publish` | `orch/v2/hub-publish\|` | `HK_op` | the transport signature of a forwarded publish request | 11 |
| `sig_hub_audit` | `orch/v2/hub-audit\|` | `HK_op` | reserved for the audit chain (D4), no object in H0 | — |
| custodial family | `orch/v2/hub-custodial\|<type>\|` | `UK` | a custodial event (`type` is the event type) | 8 |
| `sig_hub_channel_proof` | `orch/v2/hub-channel-proof\|` | channel key | the event digest | 8 |
| `sig_hub_binding` | `orch/v2/sig/hub-binding\|` | `WSK` | `cj(o)` of `hub.binding` | 9 |
| `sig_hub_unbound` | `orch/v2/sig/hub-unbound\|` | `WSK` | `cj(o)` of `hub.unbound` | 9 |
| `sig_hub_link` | `orch/v2/sig/hub-link\|` | `WSK` | `cj(o)` of a host→hub `hub.link` | 10 |
| `sig_hub_link_auth` | `orch/v2/sig/hub-link-auth\|` | `WSK` | the link-open challenge, bound to TLS | 10 |
| `sig_publish_hub` | `orch/v2/publish-hub\|` | `WSK` | a publish request on the hub path, with `hub_id` and the approval (L2-2) | 11 |
| `sig_hub_publish_approval` | `orch/v2/sig/hub-publish-approval\|` | owner's `dk_sig` | `cj(o)` of `hub.publish_approval` | 11 |
| `h_hub_id` | `orch/v2/id/hub\|` | hash | `hub_id` | 2.3 |
| `h_channel_id` | `orch/v2/id/channel\|` | hash | `channel_id` | 2.3 |
| `h_hub_event_digest` | `orch/v2/hub-event-digest\|` | hash | what the channel key signs | 8 |
| `h_hub_publish_request` | `orch/v2/hub-publish-request\|` | hash | what the owner's approval names | 11 |

Reused unchanged: `orch/v2/publish|` (`WSK`, v2 §12.1, the direct path only), `orch/v2/sig/ws-event|` (owner device, ticket-format §5.3),
`orch/v2/sig/device-cert|` (`PK`, v2 §6.1).

**The custodial family.** `UK` signs only under `"orch/v2/hub-custodial|" || type || "|"`, and `type` must match
`[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}` (so it never contains `|`; anything else is `malformed`). All
members are prefix-free among themselves and with every fixed label. **A key signs under its own labels only:**
`HK_root` under the two root labels, `HK_op` under the seven `hub-*` labels, `UK` under the custodial family, a channel
key under `hub-channel-proof`, `WSK` under its four `sig/hub-*` labels and `orch/v2/publish-hub|`, the owner's
device key under `sig/hub-publish-approval|`.

**Enforced in code, not by convention (L2-4).** `orch_hub.contract.keystore` is the contract's only signing API: one
signer per key kind (`RootSigner`, `OpSigner`, `UkSigner`, `ChannelSigner`, `WskSigner`, `OwnerDeviceSigner`). Each
takes only the objects (by `kind`, and for `hub.link` by direction) or the exact fields it may sign, builds its label
itself, and raises `KeyUseError` for anything else; none has a "sign these bytes under this label" call. A hub key
store wraps each private key in its signer once and never hands out the raw key. `tests/test_keystore.py` checks every
(key kind, object kind) pair. The vector generator's raw signer exists only to make the wrong-domain and wrong-signer
negative vectors.

## 4. Hub identity: `HK_root`, `HK_op`

**Purpose.** `HK_root` is derived from the hub's recovery code and kept offline; workspaces pin it (spec §5).
`HK_root` certifies the online `HK_op`. Rotating `HK_op` needs no re-pin.

**SHORTCUT** `HK_root and HK_op both online, keys as 0600 files` (#909): in V3 both keys live online. The objects and
checks are the final ones; only custody is short.

### 4.1 `hub.op_cert`

Signed by `HK_root` under `orch/v2/hub-op-cert|`. Fields:

| Field | Value |
|---|---|
| `v`, `suite`, `kind`, `hv` | `2`, `2`, `"hub.op_cert"`, `0` |
| `hub_id` | hex; MUST equal `hub_id(HK_root)` |
| `cert_id` | hex, 16 random bytes |
| `op_seq` | integer ≥ 1, strictly increasing per hub: each new `HK_op` gets the next one (L2-1) |
| `hk_op_pub` | b64u, valid key |
| `issued_ms`, `expires_ms` | integers, `expires_ms > issued_ms` |
| `dev` | boolean |

Verification (given the root key from the card, checked against the pin, §5): §2.2 steps 1–3; `hub_id` → `other_hub`;
dev marker; time (§2.5); `cert_id` in the verifier's revoked set → `revoked`.

**Rotation order is root-signed (L2-1).** `op_seq` is the only order between op certs; the `HK_op`-signed `card_seq`
never is. A holder that accepted `op_seq` N refuses every op cert below N (`stale_op_cert`): accepting a rotation is
an implicit revocation of every older `HK_op`, so a withheld `hub.op_revoked` cannot keep an old key alive.

### 4.2 `hub.op_revoked`

Signed by `HK_root` under `orch/v2/hub-op-revoked|`. Fields `{v, suite, kind: "hub.op_revoked", hv, hub_id, cert_id,
revoked_ms, dev}`. Verification: §2.2 steps 1–3 under the pinned root; `hub_id` → `other_hub`; dev marker. The host then
adds `cert_id` to its revoked set for this hub, permanently. Every `UK` and channel certificate under that op cert dies
with it (spec §5). Sources (spec §5): every sync from the hub, `orch hub revoke-op <file>` at the host terminal, and
(phase 4) the relay by hub card. A revocation can only be added, never removed.

## 5. The hub card

**Purpose.** What workspaces, publish and the relay know about a hub (spec §5): `{hub_id, hk_root_pub, hk_op_cert,
hxk_pub, relay_url, dev}`, plus the pinned TLS key (spec §4a).

```
card = {"o": {"v": 2, "suite": 2, "kind": "hub.card", "hv": 0, "hub_id", "hk_root_pub", "hxk_pub", "relay_url",
              "tls_spki_sha256", "card_seq", "issued_ms", "dev"},
        "op_cert": <hub.op_cert>,
        "sig": b64u(Sign(HK_op, "orch/v2/hub-card|" || cj(o)))}
```

| Field | Value |
|---|---|
| `hxk_pub` | b64u, the hub's key-agreement key (receives `WK_e` wraps in full mode, shared items in selective mode) |
| `relay_url` | `null`, or v2 §7.1's form; `null` until the relay has hub accounts (phase 4) |
| `tls_spki_sha256` | b64u of `H(SubjectPublicKeyInfo DER)` of the hub's TLS key: the pin of spec §4a |
| `card_seq` | integer ≥ 1; a holder keeps the highest it verified |

The card has exactly the three members `o`, `op_cert`, `sig` (an exception to §2.2, like v2's card).

**Verification** (host at join and on every sync; publish and relay when the hub registers):

1. members and header (§2.2 step 1) → `malformed` / `wrong_suite`;
2. `hk_root_pub` is a valid key → `malformed`; if the verifier has a pin, `hk_root_pub == pin` → `other_hub`;
3. `hub_id == hub_id(hk_root_pub)` → `hub_id_mismatch`;
4. `op_cert` verifies under `hk_root_pub` (§4.1, including `revoked`, `cert_expired`, dev marker);
5. `sig` under `op_cert.hk_op_pub` with `orch/v2/hub-card|` → `bad_signature`;
6. field set → `malformed`; dev marker; `issued_ms ≤ now + 300 s` → `cert_invalid`;
7. against the card the holder already holds, if any (L2-1): another `hk_root_pub` → `other_hub`; a lower `op_seq`,
   or the same `op_seq` under another `cert_id` → `stale_op_cert` (whatever its `card_seq`); within one op cert a
   lower `card_seq`, or the same `card_seq` with other content → `stale_card`; a higher `op_seq` is accepted and
   replaces the held card (and with it the TLS pin and `hxk_pub`).

**Check at use (L2-1).** Every use of a held card (§6, §7, §8, §10) first checks the held card's op cert against the
**current** revoked set → `revoked`. A revocation that arrives after the card was admitted stops it at once; no caller
relies on having re-verified the card.

**Pinning.** The host pins `hk_root_pub` at `hub join` (it arrives in the invite and is signed into `hub.joined`, §9.1).
Only `orch hub repin` (PK only, spec §8) changes the pin. A new `HK_op` reaches the host as a new card (higher
`op_seq`, new `op_cert`). The TLS pin follows the card: the host takes `tls_spki_sha256` only from a card it verified
through the pinned root, and drops a pin that came from a revoked op cert.

**H0 rule: one op cert at a time. DECIDED HERE.** Objects signed by `HK_op` (§6, §7, §10, §11) name
`op_cert_id`, and a verifier accepts them only under the op cert of the newest card it holds (`stale_op_cert`). After a
routine `HK_op` rotation the hub reissues every `UK` and channel certificate; old ones stop working. This is the
strict reading of spec §5 ("the hub creates new UKs and certifies them under the new HK_op") and avoids keeping a set
of old op keys.

## 6. User and channel certificates

### 6.1 `hub.uk_cert`

**Purpose.** Binds a user's custodial signing key `UK_user` (P-256, created and held by the hub, spec §5) to the user.
Signed by `HK_op` under `orch/v2/hub-uk-cert|`.

Fields `{v, suite, kind: "hub.uk_cert", hv, hub_id, op_cert_id, user_id, uk_pub, issued_ms, expires_ms, dev}`.

**DECIDED HERE:** UK certificates expire (`expires_ms`); the hub reissues before expiry. The spec lists
`{user_id, uk_pub, hub_id, dev}`; `op_cert_id` and the times are added so the chain and expiry are checkable.

Verification, under a card the verifier already verified (§5): the held card's op cert is not revoked now →
`revoked`; §2.2 steps 1–3 under `HK_op`; `hub_id` equals the
card's → `other_hub`; `op_cert_id` equals the card's op cert → `stale_op_cert`; dev marker; time (§2.5).

### 6.2 `hub.channel_cert`

**Purpose.** Binds a channel key (iPhone Secure Enclave key; browser key) to one user (spec §5, §7). Signed by
`HK_op` under `orch/v2/hub-channel-cert|`. **DECIDED HERE:** the spec names no domain for channel certificates; it
gets its own, `orch/v2/hub-channel-cert|`.

Fields `{v, suite, kind: "hub.channel_cert", hv, hub_id, op_cert_id, channel_id, user_id, key_pub, factor, issued_ms,
expires_ms, dev}`. `factor` is one of `se-unlocked`, `passkey`, `password+totp` (spec §5, closed list).
`channel_id == channel_id(key_pub)` else `cert_invalid`. Verification as §6.1.

**SHORTCUT** `Browser channel = WebCrypto key instead of a passkey` (#908): the browser channel's key is a
non-extractable WebCrypto P-256 key signing raw bytes like the phone's. Its certificate carries `factor:
"password+totp"` (the browser sign-in), never `passkey`, so a D66 policy never mistakes it for user verification. The
`passkey` factor and its WebAuthn assertion format come with #908 as their own contract.

Channel revocation is by the snapshot's `revoked_channels` (§7).

## 7. Group snapshot

**Purpose.** The roles of one workspace's users, as the hub computes them from grants and memberships (spec §6).
Signed by `HK_op` under `orch/v2/hub-snapshot|`.

| Field | Value |
|---|---|
| `v`, `suite`, `kind`, `hv` | `2`, `2`, `"hub.snapshot"`, `0` |
| `hub_id`, `op_cert_id` | as §6.1 |
| `workspace_id` | hex |
| `generation` | integer, **always `0` in H0** |
| `version` | integer ≥ 1 |
| `issued_ms` | integer |
| `roles` | object `{user_id hex: "read" \| "edit" \| "admin"}`; a user without a role is absent |
| `revoked_channels` | sorted list of unique `channel_id` hex |
| `dev` | boolean |

**SHORTCUT** `Snapshots by version only (no generations)` (#910): `generation` is present and MUST be `0`; any other
value is `malformed`. Hardening (#910) defines `HK_root`-signed generations (spec §6) as a new `hv`.

**Verification by the host** (the newest snapshot it holds is `pinned`):

1. §2.2 steps 1–3 under the card's `HK_op`; `hub_id` → `other_hub`; `op_cert_id` → `stale_op_cert`; dev marker;
2. `roles` keys and values, `revoked_channels` sorted and unique → `malformed`;
3. `workspace_id` is this workspace → `other_workspace`;
4. `version < pinned.version` → `stale_snapshot` (a replayed snapshot cannot bring back a removed user);
5. `version == pinned.version`: identical `cj(o)` is a no-op; anything else → `snapshot_conflict`;
6. hold it as the new pin, and store it in the workspace ledger (spec §6) so readers can verify old events.

**Selective mode (D80).** The host verifies and stores snapshots, but hub roles grant nothing in a selective
workspace: it accepts `UK` signatures only from users the owner grants explicitly at the host terminal (by default only
the aliased owner, spec §8a). `revoked_channels` still applies.

**Phase 3 (V3-D3).** Until custodial signing exists (phase 4), the host uses roles for hub-side reads, routes and
shared spaces only; human signatures stay local.

## 8. Custodial events

**Purpose.** A person event signed by the hub with the user's `UK`, after a fresh proof from a strongly bound channel
(spec §5 "prepare → recompute → sign", §7). Phase 4: the fields and bytes are fixed now so core (C3) and the hub (D6)
build against them; **every verification step below is defined now, and core enforces all of them from phase 4.**
Before phase 4, a host refuses any event with `auth: "hub"` (V3-D3: owner local signing).

### 8.1 Fields

A custodial event is a ticket-format §5.1 person event with:

| Field | Value |
|---|---|
| `actor` | `{"kind": "person", "id": "u_" + user_id, "via": channel_id}` |
| `auth` | `"hub"` |
| `channel` | `channel_id` hex; MUST equal `actor.via` |
| `channel_factor` | the channel certificate's `factor` |
| `hub_id` | hex |
| `snapshot_version` | the snapshot `version` the hub built the event on |
| `channel_cert` | the signed `hub.channel_cert` |
| `uk_cert` | the signed `hub.uk_cert` |
| `channel_proof` | b64u, the channel key's signature (§8.2) |
| `sig` | b64u, the `UK` signature (§8.2) |

and the event type's own payload, `id`, `type`, `based_on`, `roster_v`, `hash_v` (and `gate_gen` on decisions) exactly
as ticket-format §5.1–§5.4. **DECIDED HERE:** the spec's "`workspace_id`" field is not repeated in the event; it is in
the signed context below, which puts it under both signatures, as the spec requires. The `actor` shape with `via` in
place of `device` is the core amendment C3 makes to ticket-format §5.2 (spec §5: `Actor("human", user_id,
via=<channel>)`).

### 8.2 Bytes

Let `E` be the event without `seq`, `at`, `prev`, `ws_seq`, `sig`, `host_sig` (ticket-format §5.3), and
`ctx(E) = {"contract": 1, "suite": 2, "workspace_id": W, "log": L, "event": E}` (ticket-format §5.3's context; `L` is
the ticket uid or `"workspace"`).

```
digest        = H("orch/v2/hub-event-digest|" || cj(ctx(E without "channel_proof")))
channel_proof = Sign(channel_key, "orch/v2/hub-channel-proof|" || digest)
sig           = Sign(UK_user,     "orch/v2/hub-custodial|" || type || "|" || cj(ctx(E)))
```

So the channel proof covers the whole event including the workspace, the log, the type, the factor, the hub, the
snapshot version and both certificates; the `UK` signature covers all of that plus the proof. Neither can be moved
into another workspace, ticket, log or type (vectors `custodial_replayed_in_other_workspace`, `…_other_ticket`,
`custodial_type_changed`). The host adds `seq`, `at`, `prev`, `ws_seq` and `host_sig` when it appends, as for every
event.

**Who builds what.** Only a channel starts a signature. The hub builds `E` from its own view (spec §5), sends the
digest to the channel, which shows the event and signs the digest; the hub checks the proof, then signs with `UK`. The
host↔hub link has no operation that asks the hub to sign (§10).

### 8.3 Verification by the host (in order)

1. shape: `auth == "hub"`, every field of §8.1 present, `type` in the custodial grammar, `actor` exactly
   `{kind: "person", id: "u_<32 hex>", via}` with `via == channel`, `channel_factor` in the closed list, no host
   fields → `malformed`;
2. `hub_id` is the hub this workspace is bound to → `other_hub`;
3. `uk_cert` verifies under the held card (§6.1: `bad_signature`, `other_hub`, `stale_op_cert`, `dev_*`, `cert_*`), and
   names the actor's user → `other_user`. The held card was verified through the pinned `HK_root` with the revoked op
   certs applied (§5), which is the chain `UK → HK_op cert → pinned HK_root` with no revoked `HK_op` (spec §5);
4. `channel_cert` verifies the same way, names the same user and `channel` → `other_user`; its `factor` equals
   `channel_factor` → `factor_mismatch`;
5. `sig` under `uk_pub` with the custodial label of `type` → `bad_signature`;
6. `channel_proof` under the channel certificate's `key_pub` over `digest` → `bad_channel_proof`;
7. snapshot: `snapshot_version` greater than the newest snapshot the host holds → `unknown_snapshot` (the host syncs
   and retries); `channel` in that snapshot's `revoked_channels` → `channel_revoked`; full mode: the user has a role in
   it → else `forbidden_role` (which verbs each role may use is the role table of spec D70, applied after this);
   selective mode: the user is explicitly granted by the owner → else `not_granted`, and the item is shared (C6);
8. D66 policy on `channel_factor`: a policy requiring `secure-enclave` (D41) is met only by `passkey`; a D49-level
   policy by `se-unlocked` or `passkey`; `password+totp` meets neither → `factor_too_weak`;
9. then every ticket-format check of the event type, as for any person event (canonical identities: `u_x` and its
   aliased `p_x` count as one person, §9.1).

## 9. Join, binding, leave

### 9.1 `hub.joined` and `hub.left` (workspace log)

**Purpose.** The owner's signed record that the workspace joined (spec §8 step 3) or left (break-glass) a hub.
These are workspace-log person events (ticket-format §5.4.2 additions), signed by the owner's current device key under
`orch/v2/sig/ws-event|` exactly like any person event (ticket-format §5.3 bytes; vector `hub_joined`). Human-only
verbs at the host terminal (`orch hub join`, `orch hub leave`); never an agent, never the hub.

`hub.joined` payload, exactly: `{hub_id, hk_root_pub, mode: "selective" | "full", binding_seq, aliases}`.
`hub.left` payload, exactly: `{hub_id, binding_seq}`.

- `hub_id == hub_id(hk_root_pub)` → `hub_id_mismatch`; this is where the host pins `HK_root`.
- `binding_seq` is the `binding_seq` of the `hub.binding` the host signs next (§9.2); `hub.left` names the binding it
  ends.
- `aliases` is a list of `{person_id, user_id}` sorted by `person_id`, unique. **SHORTCUT** `Aliases owner-signed at
  join only` (C4): an alias exists only inside `hub.joined`, under the owner's signature. **An alias is never
  re-pointed:** a `person_id` already aliased to another `user_id`, or a `user_id` already aliased to another
  `person_id`, in any earlier `hub.joined` of this workspace → `alias_repointed`.
- Full mode also adds the member of kind `hub` to the `WSK`-signed member list and seals `WK_e` to `HXK` over the link
  (`wk_seal`, C2b, phase 3). Selective mode adds no member and seals nothing (D80).
- **Mode switch.** Selective → full is a new `hub.joined` with `mode: "full"` and a higher `binding_seq`, by the owner
  at the host terminal (spec §8a; never a hub admin). Full → selective is `hub.left`, rotation, then a new selective
  `hub.joined`.

### 9.2 `hub.binding`

**Purpose.** The workspace's `WSK`-signed statement that it is served by this hub (spec §8 step 4, D76). orch-publish
accepts hub-forwarded requests for the workspace only while it holds a live binding (§11). Signed by `WSK` under
`orch/v2/sig/hub-binding|`.

| Field | Value |
|---|---|
| `v`, `suite`, `kind`, `hv` | `2`, `2`, `"hub.binding"`, `0` |
| `workspace_id` | hex |
| `wsk_pub` | b64u; the workspace's current `WSK` |
| `hub_id`, `hk_root_pub` | the hub; `hub_id == hub_id(hk_root_pub)` |
| `mode` | `"selective"` or `"full"` |
| `binding_seq` | integer ≥ 1, strictly increasing per workspace across joins, mode switches and rejoins |
| `valid_from_ms` | integer |
| `dev` | boolean |

**Verification by orch-publish** (and the relay from phase 4; the hub checks the same before it forwards):

1. the workspace's card is known → `unknown_workspace` (the same store and trust as v2 §12.1 step 3: the card from the
   directory, verified through the owner's `ws_delegation`, cached until it changes or is revoked);
2. §2.2 steps 1–3 under that card's `wsk_pub` → `bad_signature` / `malformed`;
3. `workspace_id` is the workspace it is presented for (the route) → `other_workspace`;
4. `wsk_pub` equals the card's → `wsk_mismatch`;
5. `hub_id == hub_id(hk_root_pub)` → `hub_id_mismatch`;
6. dev marker; `valid_from_ms ≤ now + 300 s` → `not_yet_valid`;
7. the store transition (§9.4).

### 9.3 `hub.unbound`

Signed by `WSK` under `orch/v2/sig/hub-unbound|`. Fields `{v, suite, kind: "hub.unbound", hv, workspace_id, hub_id,
binding_seq, unbound_ms, dev}`. Made by `orch hub leave` (break-glass, works with the hub down; spec §8) and handed to
publish and the relay by the host directly when the hub is unreachable (the hub has no say in it). Verification:
steps 1–3 and 6 of §9.2, then §9.4. Only `WSK` can unbind; a hub-signed unbound is `bad_signature`.

### 9.4 The binding store (per workspace)

State: `{binding_seq, status: bound | unbound, binding}`.

| Input | State | Result |
|---|---|---|
| binding `s` | none | bound at `s` |
| binding `s` | bound at `t < s` | bound at `s` (a mode switch or rebind) |
| binding `s` | bound at `s`, identical `cj(o)` | no-op |
| binding `s` | bound at `s`, other content | `binding_conflict` |
| binding `s` | bound at `t > s` | `stale_binding` |
| binding `s` | unbound at `t ≥ s` | **`unbound`**: a binding never comes back after its leave |
| binding `s` | unbound at `t < s` | bound at `s` (rejoin) |
| unbound `s` | none, or any state at `t ≤ s` | unbound at `s` (also ends bindings not yet seen) |
| unbound `s` | bound at `s` for another `hub_id` | `other_hub` |
| unbound `s` | any state at `t > s` | `stale_binding` |

Publish evicts a binding "like a card" (spec §8): the store keeps the `unbound` record, so a replayed binding is refused
(vectors `binding_replayed_after_leave`, `publish_after_leave`).

### 9.5 The hub's trust in a workspace at join (L2-3)

A `hub.binding` is signed by the key it names, so on its own it proves nothing about which workspace that key speaks
for. **At join the host hands the hub its v2 directory card** (v2 §7.1: the owner's `PK`-signed `ws_delegation` naming
`WSK` once, and the `WSK`-signed card) and the owner's `pk_pub`, in the link op `join` (§10), together with the
binding. The hub checks, in order:

1. card and `pk_pub` present → `unknown_workspace` (vector `hub_join_binding_without_card`);
2. the card (v2 §7.1): members and field sets → `malformed`; suite → `wrong_suite`; `person_id(pk_pub) ==
   delegation.owner_person_id` → `other_person`; the delegation under `PK` with `orch/v2/sig/ws-delegation|` →
   `bad_signature`; `workspace_id`, `wsk_pub` and `owner_person_id` of card and delegation equal →
   `delegation_mismatch`; the card under the delegation's `WSK` with `orch/v2/sig/card-wsk|` → `bad_signature`;
   `sealed_hash == H(sealed)` → `malformed` (the sealed part is not opened);
3. the invite code is bound to one `workspace_id` when the hub admin made it for an existing workspace: the card's
   must equal it → `other_workspace`;
4. the binding verifies under the card's `WSK` for the card's `workspace_id` (§9.2 steps 2–6) → `bad_signature`,
   `other_workspace`, `wsk_mismatch`, …; its `hub_id` is this hub → `other_hub`;
5. **first come per `workspace_id` (TOFU):** the hub pins `workspace_id → (owner_person_id, wsk_pub)` at the first
   successful join. A later join for the same `workspace_id` under another owner → `other_person`; under the same
   owner with another `WSK` → `wsk_mismatch` (v2: a workspace's `WSK` never changes).

From then on the hub verifies link auth (§10.1), every w2h link message and every binding it forwards under the pinned
`WSK`. The residual is the same as v2's first publication: whoever joins a never-seen `workspace_id` first owns it on
this hub (vector `hub_join_squat_before_real_owner`); the real owner's join is refused, visibly, and a
`workspace_id` is 16 random bytes that only the host knows before it publishes its card.

## 10. The host↔hub link

**Purpose.** The only channel between a host and its hub (D68). The host dials out; both sides authenticate; every
message is signed as well (spec §4a).

**SHORTCUT** `Link messages signed, no replay store; TLS pinned only (no socket-UID transport)` (L7): in V3 the link
is TLS with the pinned SPKI on every layout, messages carry `seq` but receivers keep no replay store; the socket peer-UID
transport and the persisted `seq` check come with L7. Everything else here is enforced in H0.

### 10.1 Opening the link

TLS 1.3. The host checks that the hub's certificate's SPKI hashes to the card's `tls_spki_sha256` (no WebPKI). The hub
sends a 32-byte random `challenge`; the host answers with

```
Sign(WSK, "orch/v2/sig/hub-link-auth|" || suite || hub_id (16) || workspace_id (16) || challenge (32) || exporter (32))
```

where `exporter` is the TLS exporter with label `"EXPORTER-orch-hub-link"`, empty context, 32 bytes. The hub verifies it
against the `WSK` it pinned for that workspace at join (§9.5; the first link of a join takes it from the verified
card in the `join` message) (vector `link_auth`). One link serves one `workspace_id`.

### 10.2 `hub.link` messages

```
{"o": {"v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id", "workspace_id", "dir": "w2h" | "h2w", "seq",
       "ts_ms", "op", "re", "body"},
 "sig": b64u(Sign(WSK, "orch/v2/sig/hub-link|" || cj(o)))          (dir w2h)
        b64u(Sign(HK_op, "orch/v2/hub-route|" || cj(o)))           (dir h2w)}
```

`seq` ≥ 1 per direction and link; `re` is `null` on a request and the request's `seq` on its reply; `body` an object.

**The closed operation list** (spec §8, §8a). There is **no operation that asks the hub to sign anything**; an
unknown `op` is `unknown_op`.

| `op` | Request direction | Mode | Request body (exact) | Reply body |
|---|---|---|---|---|
| `snapshot_get` | w2h | both | `{}` | `{"snapshot": <hub.snapshot>}` |
| `event_deliver` | h2w | both | `{"log", "event"}`: a custodial event (§8) | `{}` or `{"error"}` |
| `route_out` | w2h | both | `{"envelope": b64u}`: a v2 §11 ws→ws envelope, as is | `{}` |
| `route_in` | h2w | both | `{"envelope": b64u}` | `{}` |
| `wk_seal` | w2h | full | `{"epoch", "wrap": b64u}`: `WK_e` sealed to `HXK` (v2 §5.3) | `{}` |
| `publish_out` | w2h | both | `{"request": {method, host, target, headers, body_hex}}` (§11) | `{"status", "body_hex"}` |
| `join` | w2h | both | `{"invite", "card", "pk_pub", "binding"}`: the v2 card, the owner's PK, the binding (§9.5) | `{}` |
| `card_put` | w2h | both | `{"card", "pk_pub"}`: the workspace's v2 card, opaque to the hub, forwarded to publish and the relay (§16) | `{}` |
| `binding_put` | w2h | both | `{"object"}`: a `hub.binding` or `hub.unbound`, forwarded as is | `{}` |
| `audit_ack` | w2h | both | `{"seq", "head": b64u(32)}` | `{}` |
| `share_put` | w2h | selective | `{"item_id", "sealed": b64u}` | `{}` |
| `share_revoke` | w2h | selective | `{"item_id"}` | `{}` |

A reply's body is either the reply body above or `{"error": <code>}`; H0 fixes request bodies only (reply bodies are
built in D5/C5 against this table). Phases: `snapshot_get`, `publish_out` and the selective ops in phase 2 (share ops
carry needs-you summaries only until C6); `route_*`, `wk_seal`, `audit_ack` in phase 3; `event_deliver` in phase 4.

**Verification by the receiver** (hub for w2h with the binding's `WSK`, host for h2w with the held card's `HK_op`):

1. §2.2 steps 1–3 under the direction's key and label → `bad_signature` / `malformed`; `dir` is the expected one →
   `malformed`;
2. `op` in the table, and `re == null` exactly when `dir` is the op's request direction → `unknown_op`;
3. the op exists in this workspace's mode → `wrong_mode` (no `wk_seal` in selective mode, D80; no share ops in full
   mode);
4. `hub_id` → `other_hub`; `workspace_id` is the link's → `other_workspace`;
5. `|now − ts_ms| ≤ 120 s` → `stale_timestamp`;
6. the request body has exactly the table's fields with their types → `malformed`;
7. (L7) `seq` greater than the last accepted in this direction.

## 11. Publish through the hub (phase 2, D76, V3-D4)

**Purpose.** A workspace publishes through its hub; orch-publish accepts it only with the workspace's live binding
(spec §8 "Publish (D76)"). In a selective workspace the owner's local key authorises public access inside the request
(V3-D4).

### 11.1 The request

The hub path has **its own `WSK` signed bytes** (L2-2). The host builds the request with the v2 §12.1 headers
(`Orch-Workspace`, `Orch-Ts`, `Orch-Nonce`, `Orch-Signature`, same syntax) and, when the request grants public
access, `Orch-Owner-Approval: b64u(cj(approval))` (§11.3), and signs

```
wsk_signed_bytes = "orch/v2/publish-hub|" || suite || workspace_id (16) || u64(ts_ms) || nonce (16)
                   || lp16(METHOD) || lp16(host) || lp16(request-target) || H(body)
                   || hub_id (16) || approval_hash
approval_hash    = H(cj(approval)), or 32 zero bytes when the request carries no approval
```

So the host's signature commits to the hub that forwards the request and to the approval exactly as attached: nobody
after the host can add, swap or strip an approval (`bad_signature`), and a hub-path signature never verifies as a v2
§12.1 direct request (its label differs), so a hub cannot replay a hub request on the direct path. The host attaches
an approval only to the request the owner signed it for; **the agent `publish(...)` path never carries one**. It
sends the request over `publish_out`. The hub forwards it **unchanged** and adds two headers:

```
Orch-Hub:           <hub_id hex>
Orch-Hub-Signature: b64u(Sign(HK_op, "orch/v2/hub-publish|" || suite || hub_id (16) || H(wsk_signed_bytes)))
```

The hub's signature is transport only (spec §8): it adds no authority, it only proves which hub delivered the request.

### 11.2 What orch-publish checks, in order

| # | Check | Refusal |
|---|---|---|
| 1 | header syntax (v2 §12.1 plus `Orch-Hub`, `Orch-Hub-Signature`, and `Orch-Owner-Approval` strictly parsed when present); method, host and target ASCII | 400 `malformed` |
| 2 | `\|now − ts\| ≤ 120 s` | 401 `stale_timestamp` (`server_ms`) |
| 3 | the workspace's card is known (v2 §12.1 step 3) | 401 `unknown_workspace` |
| 4 | `Orch-Signature` under the card's `WSK` over the hub-path `wsk_signed_bytes` (§11.1), with `Orch-Hub` and the attached approval | 401 `bad_signature` |
| 5 | a binding for this workspace, status `bound`, signed by the card's current `WSK` | 403 `not_bound` |
| 5b | the binding's `hub_id` equals `Orch-Hub` | 403 `other_hub` |
| 6 | a hub card for that `hub_id`, verified (§5) with `hk_root_pub` equal to the binding's, dev marker included | 401 `unknown_hub` (403 `dev_refused` at a real publish) |
| 7 | `Orch-Hub-Signature` under that card's `HK_op` | 401 `bad_hub_signature` |
| 8 | the nonce is unseen for this workspace; then recorded for 240 s | 401 `replay` |
| 9 | public access: an approval is present and verifies (§11.3); an approval that is present is always verified | 403 `approval_required`, `bad_approval`, `other_person`, `other_workspace`, `other_hub`; 400/403 certificate codes |

Every signature comes before the nonce store (v2 §12.1). Policy checks of orch-publish that are not part of this table
(blocked workspace, owner allowlist, path workspace) come after step 8 as today. **Which requests grant public access**
is orch-publish's route definition (`access: "public"` of a share or app); F1 fixes the list, and every such request
needs step 9.

**Direct requests.** A request without `Orch-Hub` is the v2 direct path, signed under `orch/v2/publish|` and checked as
v2 §12.1 until the H6 switch, binding or not (SHORTCUT `Direct relay / publish paths behind a flag until H5/H6`). A
hub-path signature presented there is `bad_signature` (vector `publish_hub_request_replayed_direct`). From H6 on,
orch-publish refuses direct requests for a workspace with a live binding.

### 11.3 `hub.publish_approval` (V3-D4)

The owner's local-key authorisation of **one exact request**, signed at the host terminal by the owner's device key
(`dk_sig`, the key that signs the owner's person events; D64 custody):

```
approval = {"o": {"v": 2, "suite": 2, "kind": "hub.publish_approval", "hv": 0, "workspace_id", "hub_id",
                  "device_id", "request_hash", "access": "public"},
            "device_cert": <v2 §6.1 device_cert, signed by the owner's PK>,
            "pk_pub": <b64u, the owner's PK>,
            "sig": b64u(Sign(dk_sig, "orch/v2/sig/hub-publish-approval|" || cj(o)))}

request_hash = b64u(H("orch/v2/hub-publish-request|" || suite || workspace_id (16) || u64(ts_ms) || nonce (16)
                      || lp16(METHOD) || lp16(host) || lp16(request-target) || H(body) || hub_id (16)))
```

The host draws `ts` and `nonce` first, shows the owner the request, and the owner signs after (L2-2). An approval is
therefore good for exactly one request: its nonce is single-use in the publish nonce store and its `ts` must be inside
the 120 s window. The approval has no lifetime of its own; if the owner takes longer, the host draws a fresh `ts` and
`nonce` and asks again. (`request_hash` cannot include the WSK-signed bytes, which contain `H(approval)`; it names the
same fields instead.)

**Verification (step 9), in order:**

1. members `{o, sig, device_cert, pk_pub}` and header → `malformed` / `wrong_suite`;
2. `pk_pub` is a valid key → `malformed`; `person_id(pk_pub)` equals the `owner_person_id` of the workspace's card
   delegation (v2 §7.1) → `other_person`;
3. `device_cert` verifies under `pk_pub` as v2 §6.1 (scopes include `decide`) → `cert_invalid`, `cert_expired`;
   its `device_id` is not revoked (the revocations orch-publish learns from the directory) → `revoked`;
4. `sig` under the certificate's `dk_sig_pub` → `bad_approval`;
5. field set → `malformed`; `device_id` equals the certificate's → `bad_approval`;
6. `workspace_id` is the request's → `other_workspace`; `hub_id` is `Orch-Hub` → `other_hub`;
7. `request_hash` equals the hash of this request (its `ts`, `nonce`, method, host, target, body and hub), and
   `access` equals what the request grants → `bad_approval`.

**How this meets V3-D4** ("the owner's local key authorises publish inside the WSK-signed request"). The approval is
inside the `WSK`-signed bytes (`approval_hash`, §11.1), and the approval in turn names the one exact request, nonce
included. Vectors: `publish_approval_reused_on_second_request` (same body, fresh nonce → `bad_approval`),
`publish_approval_replayed_with_its_request` (`replay`), `publish_approval_attached_by_hub` and
`publish_approval_stripped_by_hub` (`bad_signature`), `publish_hub_request_replayed_direct` (`bad_signature`).

Requests for `secret` or `sealed` access keep the v2 path: the host evaluates the charter (spec §5.5, §8 "the agent
`publish(...)` path is unchanged") and no approval is needed. An approval on such a request is still verified, and its
`access` (`"public"`) then mismatches → `bad_approval`.

**Full mode** (phase 3+): until custodial signing (phase 4), a dev full-mode workspace uses the same owner approval
(V3-D3); from phase 4 the approval is a custodial signature carried the same way (spec §8), defined then as a new `hv`.

## 12. Who verifies what (summary)

| Verifier | Verifies | Holds |
|---|---|---|
| Workspace host | card (pinned root), op revocations, snapshots, custodial events, h2w link messages | pin of `HK_root`, newest card, revoked op certs, newest snapshot (and all, in the ledger), alias map |
| orch-publish | bindings, unbounds, hub cards (by binding), forwarded requests, owner approvals | workspace cards (v2), binding store, hub cards, nonces |
| orch-relay (phase 4) | hub cards, bindings | hub accounts by card |
| orch hub | the v2 card and binding at join (§9.5), link auth, w2h link messages, bindings it forwards, channel proofs before it signs | its keys, users, channels, snapshots, audit, the `workspace_id → (owner, WSK)` pins |

## 13. Threat notes for the reviewer

- A **rogue hub** without the workspace's `WSK` can make no binding (§9.2 step 2, vectors
  `binding_signed_by_other_wsk`, `publish_other_hub_header`), so it cannot claim a publish namespace (spec §9).
- A **hostile host** cannot make the hub sign: the link has no signing operation (vector `link_sign_request_refused`),
  and custodial events are built by the hub from its own view (spec §5). It can send `publish_out` for its own
  workspace only (the link is bound to one `workspace_id` and one `WSK`).
- A **compromised hub** can sign as any user in full mode (accepted, spec §9) but cannot forge a channel proof
  (vector `custodial_proof_not_by_channel`) without a channel key it does not hold; it can drop or delay events. It
  cannot revive a binding after leave (§9.4) and cannot unbind a workspace (only `WSK` can).
- **Replays.** Snapshots are monotonic (§7); custodial events are bound to workspace, log and type and go through the
  F1 id and generation checks; publish requests through the v2 nonce store; link messages only through the 120 s
  window in V3 (SHORTCUT).

## 14. Refusal codes

v2 codes keep their meaning (v2 §14). New in H0:

| Code | Where | Meaning |
|---|---|---|
| `hub_id_mismatch` | card, binding, `hub.joined` | `hub_id` is not `hub_id(hk_root_pub)` |
| `other_hub` | everywhere | names another hub than the pinned / bound one |
| `other_user` | custodial | a certificate names another user or channel |
| `stale_op_cert` | uk/channel cert, snapshot | signed under another op cert than the newest card's |
| `dev_refused` | everywhere | `dev: true` at a real verifier |
| `dev_mismatch` | everywhere | `dev: false` at a dev verifier |
| `stale_snapshot`, `snapshot_conflict`, `unknown_snapshot` | snapshot, custodial | §7, §8.3 |
| `forbidden_role`, `not_granted`, `channel_revoked` | custodial | §8.3 step 7 |
| `bad_channel_proof`, `factor_mismatch`, `factor_too_weak` | custodial | §8.3 |
| `wsk_mismatch`, `not_yet_valid`, `binding_conflict`, `stale_binding`, `unbound` | binding store | §9 |
| `alias_repointed` | `hub.joined` | §9.1 |
| `unknown_op`, `wrong_mode` | link | §10 |
| `not_bound` (403), `unknown_hub` (401), `bad_hub_signature` (401) | publish | §11.2 |
| `approval_required`, `bad_approval` (403) | publish | §11.3 |

Reused: `malformed`, `wrong_suite`, `bad_signature`, `cert_invalid`, `cert_expired`, `revoked`, `other_workspace`,
`other_person`, `stale_timestamp`, `unknown_workspace`, `replay`, `stale_card`. HTTP statuses for publish are in §11.2;
link errors travel as `{"error": code}` reply bodies; host-side refusals are local (logged, never sent back as content).

## 15. Test vectors

`vectors/vectors_hub.json` (FAKE keys, test-only: `seed = SHA-256("FAKE orch v2 test vector: hub/" || name)`, scalar
`int(seed) mod (n−1) + 1`, RFC 6979 signatures only for reproducibility):

| Section | Content |
|---|---|
| `keys`, `ids`, `labels` | every key with seed and public key; derived ids; the label table |
| `objects` | one valid instance of every object |
| `signatures` | per object: signer, label, `canonical_hex` = `cj(o)`, `signed_hex` = label ‖ `cj(o)`, `sig` |
| `custodial` | an event with `digest_hex`, the proof's and the UK's signed bytes |
| `hub_joined` | the owner-signed workspace-log event and its ticket-format §5.3 bytes |
| `publish` | a forwarded request: `wsk_signed_hex` (hub-path bytes), `approval_hash_hex`, `hub_signed_hex`, `request_hash` |
| `link_auth` | challenge, exporter, signed bytes, signature |
| `cases` | 125 cases (24 positive, 101 negative): `check`, `args`, `expect` (`{"ok": …}` or `{"error": code}`), `phase` |

The negative cases cover, per object where it applies: wrong domain, wrong signer, expired, binding for another
workspace, unbound after leave, missing dev marker, dev at a real verifier, extra or unknown field, non-canonical
encoding (b64u padding, upper-case hex, duplicate key, float), wrong suite, future `hv`, replays and stale versions.

Every implementation runs the cases of the checks it implements: the hub all of them; orch-core `card`, `op_*`,
`snapshot`, `uk_cert`, `channel_cert`, `custodial`, `joined_payload`, `link` (h2w); orch-publish `binding_store`,
`publish`, `card`, `v2_card`. The hub also runs `hub_join`. The cross-repo contract job (V3-D8) runs from phase 2.

## 16. Decided here, and open points for L2

| # | Point | Resolution | Basis |
|---|---|---|---|
| 1 | Spec domains lack the trailing `\|` | Added, as v2 §3 did | v2 §3 rule |
| 2 | No domain named for `HK_root` signatures, channel certs, channel proofs, WSK hub objects | New labels §3; WSK ones in v2's `sig/` form | spec §5 "separate domain per object" |
| 3 | `hub_id` origin | `H(label ‖ suite ‖ hk_root_pub)[0:16]` | like `person_id` (v2 §4) |
| 4 | Contract version | `hv: 0` in every object | V3-D8 shared vectors need a version |
| 5 | UK/channel cert lifetime and chain | `op_cert_id` + `expires_ms`; only the newest card's op cert accepted | spec §5 reissue on rotation |
| 6 | Snapshot generations | field present, must be 0 | SHORTCUT #910 |
| 7 | `workspace_id` in the custodial event | in the signed context, not repeated | spec §5 "inside the signed material" |
| 8 | Custodial actor | `{kind: person, id: "u_…", via}` | spec §5 `Actor("human", user_id, via)`; C3 amends F1 |
| 9 | Browser channel factor before passkeys | `password+totp` | SHORTCUT #908, D66 conservative |
| 10 | Where publish gets `WSK` | the v2 card store (§12.1 step 3) | D76 "verifies WSK"; namespaces per workspace |
| 11 | V3-D4 "inside the WSK-signed request" | hub-path WSK bytes `orch/v2/publish-hub\|` cover `hub_id` and `H(approval)`; the approval names the exact request incl. ts and nonce (single use) | V3-D4; L2-2 (the first H0 draft bound only by a ts-less hash: rejected) |
| 12 | Which publish requests need the approval | public access only; secret/sealed keep the charter path | spec §8 "public access still needs a human signature", §5.5 |
| 13 | Owner key for the approval | owner's device key with its PK-signed certificate; PK must be the card delegation's owner | D64 local key; v2 §6.1, §7.1 |
| 14 | Link replay | `seq` carried, not enforced | SHORTCUT (L7) |
| 15 | Binding replay after leave | store keeps the unbound record (§9.4) | spec §8 "no later hub.unbound" |
| 16 | Unbound delivery with the hub down | the host sends it to publish directly | spec §8 break-glass "none needs the hub" |
| 17 | The hub's trust in a workspace's `WSK` at join | the v2 directory card + owner `PK` in `join`; first come per `workspace_id` | L2-3 |
| 18 | `WSK` source after H5 | the v2 directory card, carried opaquely through the hub (`card_put`); publish never pins bindings first-come | L2 recommendation, adopted |

**Open / decided after L2:**

- (Decided, L2 recommendation adopted by the orchestrator.) **The one source of a workspace's `WSK`, for orch-publish,
  the relay and the hub, is the v2 directory card**, with the v2 update rules (`workspace_id`, owner and `WSK` never
  change; `card_seq` strictly increases). After H5 the host no longer talks to the relay directly, so it sends its
  card through the hub as an opaque object (`card_put`, §10) and the hub forwards it to the relay and orch-publish. The
  hub cannot forge or replace it (`PK`-signed delegation, `WSK` signature, never-change rules); it can only withhold
  it, which only stops publishing. **orch-publish never pins a binding first-come:** that would make the hub's choice
  of `WSK` authoritative. A binding counts only under the `WSK` of the card publish already holds (§9.2, §11.2 step 5).
- **Relay accounts by hub card** (E1) and hub↔hub objects (H7) are not in H0.
