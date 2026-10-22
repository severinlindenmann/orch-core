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
| 10.5 | hub audit chain `hub.audit` (D4) | `HK_op` | workspaces, admins | 3 |
| 10.6 | hub admin read (F3) | `HK_op` | orch-publish | 3 |
| 10.4 | shared spaces: `hub.space`, `hub.space_wrap`, `space.item` (V3-D8) | `HK_op`, `HK_op`, `WSK` | host, hub | 3 |
| 6.3 | channel registration `hub.channel_register` (V3-D23) | the new channel key | hub | 4 |
| 8.4–8.5 | prepare (what a channel shows and signs), the custodial type table and actions (V3-D23) | — | channel, hub, host | 4 |
| 10.4 | `hub.space_proposal` (#1020) | `HK_op` | invited host | 4 |
| 10.7 | the hub's workspace view, `log_sync` (V3-D23) | `WSK` (each line's `host_sig`) | hub | 4 |
| 10.8 | hub → relay requests (E1) | `HK_op` | orch-relay | 4 |

Not in H0 (named so nobody builds them from guesswork): snapshot
generations (#910), the standalone signed alias (C4), the `webauthn` passkey channel contract (#908), hub↔hub
objects (H7). Relay hub accounts (E1): registration by card is §5 as it stands (self-authenticating, like V3-D16);
the hub's signed requests to the relay are §10.8 (phase-4 extension).

**Phase-4 extension (V3-D23, `p4-contract`).** §6.3, §8.4, §8.5, §10.4 (proposals), §10.5 (`channel` events),
§10.7 and §10.8 close the gaps the phase-4 prep code hit (D6 custodial engine, D7 channels, C13 #1020, E1). Same `hv:
0`: every addition is a new kind, label, op or event type, so no H0 verifier reads an old object differently. Choices
are listed in §16 (#24–#31).

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

**Unicode.** Every text rule of this contract (§8.5) is evaluated under the Unicode **16.0.0** tables (General_Category
and `Default_Ignorable_Code_Point`), whatever the platform's own Unicode version (L2P4-3): an implementation whose
runtime tables are another version (Python builds, Swift, JS engines differ) uses a bundled 16.0.0 table, so hub,
core, relay and phone refuse exactly the same strings, including what "unassigned" (`Cn`) means. The reference refuses
to load under another version rather than judge differently.

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
| `sig_hub_audit` | `orch/v2/hub-audit\|` | `HK_op` | `cj(o)` of `hub.audit` | 10.5 |
| `sig_hub_admin_read` | `orch/v2/hub-admin-read\|` | `HK_op` | the bytes of a hub admin read request | 10.6 |
| `h_hub_audit_chain` | `orch/v2/hub-audit-chain\|` | hash | one signed audit record (the chain link) | 10.5 |
| `h_hub_audit_scope` | `orch/v2/hub-audit-scope\|` | hash | the blinded workspace tag of an audit record | 10.5 |
| `h_hub_audit_event` | `orch/v2/hub-audit-event\|` | hash | an audit event, signed into its record | 10.5 |
| custodial family | `orch/v2/hub-custodial\|<type>\|` | `UK` | a custodial event (`type` is the event type) | 8 |
| `sig_hub_channel_proof` | `orch/v2/hub-channel-proof\|` | channel key | the event digest | 8 |
| `sig_hub_channel_register` | `orch/v2/hub-channel-reg\|` | the new channel key | `cj(o)` of `hub.channel_register` (proof of possession) | 6.3 |
| `sig_hub_space_proposal` | `orch/v2/hub-space-proposal\|` | `HK_op` | `cj(o)` of `hub.space_proposal` | 10.4 |
| `sig_hub_relay_request` | `orch/v2/hub-relay-req\|` | `HK_op` | the bytes of a hub → relay request | 10.8 |
| `aad_hub_view` | `orch/v2/hub-view-aad\|` | AAD | one sealed `log_sync` page | 10.7 |
| `sig_share_item` | `orch/v2/sig/share-item\|` | `WSK` | `cj(o)` of `share.item` (selective `share_put`) | 10.2b |
| `aad_hub_share` | `orch/v2/hub-share-aad\|` | AAD | one sealed shared item | 10.2b |
| `h_share_item` | `orch/v2/hub-share-item\|` | hash | a signed shared item (what a custodial event about it binds) | 10.2b |
| `sig_hub_binding` | `orch/v2/sig/hub-binding\|` | `WSK` | `cj(o)` of `hub.binding` | 9 |
| `sig_hub_unbound` | `orch/v2/sig/hub-unbound\|` | `WSK` | `cj(o)` of `hub.unbound` | 9 |
| `sig_hub_link` | `orch/v2/sig/hub-link\|` | `WSK` | `cj(o)` of a host→hub `hub.link` | 10 |
| `sig_hub_link_auth` | `orch/v2/sig/hub-link-auth\|` | `WSK` | the link-open challenge, bound to TLS | 10 |
| `sig_publish_hub` | `orch/v2/publish-hub\|` | `WSK` | a publish request on the hub path, with `hub_id` and the approval (L2-2) | 11 |
| `sig_hub_publish_approval` | `orch/v2/sig/hub-publish-approval\|` | owner's `dk_sig` | `cj(o)` of `hub.publish_approval` | 11 |
| `sig_hub_space` | `orch/v2/hub-space\|` | `HK_op` | `cj(o)` of `hub.space` | 10.4 |
| `sig_hub_space_wrap` | `orch/v2/hub-space-wrap\|` | `HK_op` | `cj(o)` of `hub.space_wrap` | 10.4 |
| `sig_space_item` | `orch/v2/sig/space-item\|` | `WSK` | `cj(o)` of `space.item` | 10.4 |
| `sig_space_accept` | `orch/v2/sig/space-accept\|` | `WSK` | `cj(o)` of `space.accept` | 10.4 |
| `h_space_members` | `orch/v2/hub-space-members\|` | hash | the member list of one space epoch, signed into an item | 10.4 |
| `h_sk_commit` | `orch/v2/hub-sk-commit\|` | hash | the commitment to `SK_e` in `hub.space` | 10.4 |
| `aad_space` | `orch/v2/space-aad\|` | AAD | one sealed space item | 10.4 |
| `h_hub_id` | `orch/v2/id/hub\|` | hash | `hub_id` | 2.3 |
| `h_channel_id` | `orch/v2/id/channel\|` | hash | `channel_id` | 2.3 |
| `h_hub_event_digest` | `orch/v2/hub-event-digest\|` | hash | what the channel key signs | 8 |
| `h_hub_publish_request` | `orch/v2/hub-publish-request\|` | hash | what the owner's approval names | 11 |

HKDF labels (not signed bytes, so outside the prefix-free table; `info` = label followed by `|` fields, v2 §3): `orch/v2/space-item` (item key of a space and epoch), `orch/v2/space-item-msg` (one item), `orch/v2/hub-view` (the view key of a workspace and epoch, §10.7) and `orch/v2/hub-view-msg` (one view page) and `orch/v2/hub-share-msg` (one shared item under its item key). Reused unchanged: `orch/v2/sig/host-event|` and `orch/v2/event|` (ticket-format §5.5, verified by the hub in §10.7, never signed by it), `orch/v2/seal-aad|`, `orch/v2/publish|` (`WSK`, v2 §12.1, the direct path only), `orch/v2/sig/ws-event|` (owner device, ticket-format §5.3),
`orch/v2/sig/device-cert|` (`PK`, v2 §6.1).

**The custodial family.** `UK` signs only under `"orch/v2/hub-custodial|" || type || "|"`, and `type` must match
`[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}` (so it never contains `|`; anything else is `malformed`). All
members are prefix-free among themselves and with every fixed label. **A key signs under its own labels only:**
`HK_root` under the two root labels, `HK_op` under the `hub-*` labels the table gives it (now including
`hub-space-proposal|` and `hub-relay-req|`), `UK` under the custodial family, a channel
key under `hub-channel-proof` and, once, `hub-channel-reg` (its own registration), `WSK` under its four `sig/hub-*` labels, `sig/share-item|`, `sig/space-item|`, `sig/space-accept|` and `orch/v2/publish-hub|`, the owner's
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
revoked_ms, dev}`. Verification: §2.2 steps 1–3 under the pinned root; `hub_id` → `other_hub`. **No dev-marker
check** (L2-8): a revocation only removes authority, so refusing one for its marker would fail open. The host then
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

Channel revocation is by the snapshot's `revoked_channels` (§7). **A channel revocation is permanent** (p4,
like an op revocation, §4.2): a revoked `channel_id` never comes back, and since `channel_id` is derived from the key, the
same key can never be registered again (D7's operator "unrevoke" is not a contract operation and must go). A person
whose channel was revoked by mistake registers a new key.

### 6.3 Channel registration and attestation (phase 4, V3-D23)

**Purpose.** Before the hub issues a `hub.channel_cert`, the new channel key proves it holds the private key, and the
hub decides the certificate's `factor` from the gate it checked itself, never from what the caller says.

```
hub.channel_register {v, suite, kind: "hub.channel_register", hv, hub_id, user_id,
                      channel_kind: "phone" | "browser", key_pub, attestation: "pairing-code" | "session",
                      nonce: b64u(32), expires_ms, dev}
sig = Sign(channel_key, "orch/v2/hub-channel-reg|" || cj(o))        signed by the key in key_pub
```

**Its own label (DECIDED HERE, gap 3).** D7's prep code reused `orch/v2/hub-channel-proof|` over a hash of a
differently-shaped object. That makes one domain of the channel key mean two things and relies on two object shapes never
colliding inside a hash. A dedicated label keeps "one label, one meaning" (spec §5) and is checked by the prefix-free
table: a registration can never be presented as a custodial proof, nor the reverse (vector
`channel_register_under_proof_label`). `ChannelSigner` signs `hub.channel_register` only when `key_pub` is its own key.

**Flow.** (1) The hub opens a registration for **one key** after a gate and stores the challenge `{user_id, key_pub,
nonce, expires_ms (≤ now + 120 s), gate, session}`: `gate = "session"` for a browser inside a live password + TOTP
session; `gate = "pairing-code"` for a phone that presented a valid one-time operator pairing code **and** has a live
session of the same `user_id` (L2P4-D7a: the code alone is no bearer credential for `se-unlocked`); `session` records
that a live session of `user_id` opened it, for both gates. (2) The channel signs the object with exactly those values.
(3) The hub checks, in order: envelope and header; `key_pub` is a valid key (`malformed`); the signature under
`key_pub` (`bad_signature`); the field set (`malformed`); `hub_id` is this hub (`other_hub`); `user_id` is the
challenge's (`other_user`); `nonce`, `key_pub` and `expires_ms` are the challenge's and `now < expires_ms`
(`challenge_invalid`: nobody who sees the nonce can race their own key into the gate); `(channel_kind, attestation)`
is `(phone, pairing-code)` or `(browser, session)` (`malformed`); `attestation` equals the gate the hub checked
(`factor_mismatch`); the challenge was opened inside a live session (`no_session`); the dev marker; the key's
`channel_id` was never revoked (`channel_revoked`, permanent, L2P4-6) and is not registered to another user
(`key_in_use`: one channel id, one user). The challenge is consumed by the first attempt, good or bad. (4) The factor is **derived**: `pairing-code` → `se-unlocked`, `session`
→ `password+totp` (reference `verify_channel_register`; vectors `channel_register_*`). Then the hub issues the
certificate and writes a `channel` audit event (§10.5). Nothing is issued when the audit write fails (fail closed);
on a contract surface that failure is `internal`, there is no `audit_failed` code.

**Revoking.** A revocation names a `channel_id` this hub registered; an unknown id is refused (`unknown_channel`, a
local refusal of the hub's CLI/API, L2P4-D7b): keys are fresh per channel, so pre-revoking has no use, and a
revocation (permanent) is always on the chain as a `channel` event.

**Phone key attestation (gap 5).** `se-unlocked` claims a Secure Enclave key on an unlocked phone. Nothing the hub
receives in hv 0 proves that: the pairing code proves only that **the operator approved this pairing for this user**.

- **Dev rule (V3).** The hub issues `se-unlocked` only through `gate = "pairing-code"`: a code made at the hub's
  terminal by the state-dir owner for one `user_id`, at most 10 minutes valid, single use (consumed by the registration
  it opened), rate-limited per user, never derivable from a password session (vector
  `channel_register_phone_from_password_session`). A phone with a password session only is a `password+totp` channel.
- **SHORTCUT** `Phone key attestation by operator pairing code (no App Attest)`: the factor is the operator's word,
  not hardware evidence. An attacker with a pairing code (shoulder-surfed, forwarded) and the user's sign-in can
  register a software key as `se-unlocked`.
- **Hardening path** (its own contract and `attestation` value, not built): `app-attest`. The phone's App Attest key
  (Apple `DCAppAttestService`) attests the app instance once at pairing (attestation object verified against Apple's
  App Attest root, `rpId` = the orch bundle's App ID, counter 0, `clientDataHash` = `H(cj(o))` of the registration);
  every later registration and, optionally, every custodial proof carries an App Attest assertion over the same hash.
  This binds the channel key to a genuine orch app instance; that the channel key itself is in the Secure Enclave
  stays the app's own guarantee (App Attest cannot attest an arbitrary key). DeviceCheck alone (two bits per device)
  is not enough and is not used. Until then a D66 policy that needs presence keeps requiring `passkey` (§8.3 step 8).

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
   7b. (p4) the event's `(type, log)` is in the custodial table of §8.5 → else `not_custodial`; the user's role
   reaches the type's lowest role → else `forbidden_role`. **Selective mode (L2P4-1):** there is no hub role; the
   floor is the F1 member role of the user's canonical person (its alias to a member `p_`, §9.1) mapped `owner` →
   `admin`, `maintainer`/`member` → `edit`, `viewer` → `read`; a granted user without an alias to a member is a
   `viewer` and signs nothing (`H.MEMBER_ROLE_FLOOR`, vectors `custodial_selective_*`);
8. D66 policy on `channel_factor`: a policy requiring `secure-enclave` (D41) is met only by `passkey`; a D49-level
   policy by `se-unlocked` or `passkey`; `password+totp` meets neither → `factor_too_weak`;
9. then every ticket-format check of the event type, as for any person event (canonical identities: `u_x` and its
   aliased `p_x` count as one person, §9.1). For "who may sign what" (ticket-format §5.4.2), in full mode the hub role
   stands for the member role: `admin` → `owner`, `edit` → `member`, `read` → `viewer` (D70: `admin` is today's
   owner); in selective mode the canonical person's own member role applies (unaliased: `viewer`). In
   particular the F1 checks of `based_on`, `roster_v`, `gate_gen`, the gate `hash` and `policy_hash` run against the
   host's own log: an event the hub built from a stale or false view (§10.7) is refused there.

### 8.4 Prepare: what a channel shows and signs (phase 4, V3-D23)

The channel API itself (sessions, HTTP, push) is the hub's (D13); these rules are the contract part every channel
(orch-mobile, the browser home) implements against the vectors.

1. The channel sends an **action** (§8.5) for one workspace, never event bytes.
2. The hub checks the action (`check_action`), builds `E` from its view (§10.7) and its own certificates and
   snapshot, runs **`check_built(action, log, E)`, always** (L2P4-2b: same type, same log, and on the four decision
   types `E.hash` present (`malformed`) and byte-equal to `action.hash` (`event_changed`); vectors `built_*`), and
   replies `{"prepared_id", "ctx", "digest", "expires_ms"}` where `ctx = ctx(E without
   "channel_proof")` (§8.2) and `digest` its hex. `prepared_id` is single use and lives at most 120 s.
3. **The channel recomputes** `digest = H("orch/v2/hub-event-digest|" || cj(ctx))` from the `ctx` it received and
   refuses (`event_changed`) when it differs from `digest`; `ctx` must have exactly `{contract: 1, suite: 2,
   workspace_id, log, event}`, `event.auth == "hub"` and no `channel_proof` (`malformed`; reference
   `check_prepared`, vectors `prepared_*`). It renders **every** authority-bearing field of `ctx.event` (type,
   workspace, ticket, gate and hash, outcome, text, people, settings), and any field it does not know makes it fall back
   to the full field list (WYSIWYS). It signs only the digest it computed itself.
   **View-derived text is escaped** (L2P4-2d): titles, question texts and option labels come from the workspace's
   events, which agents write and which have no text rule in core; a channel renders every character the §8.5 text
   rule refuses (other than LF) as `<U+XXXX>` (reference `render_text`, vectors `render_*`). **Content-bound
   decisions** (L2P4-2c): body text and artifacts are not synced (§10.7), so a channel that offers `gate.approved`,
   `gate.changes_requested` or `verdict.given` shows the decision's hash in short form next to a link to where the
   content was reviewed, and Mission Control shows the same short hash; a channel that cannot do that does not offer
   content-bound decisions.
4. The hub's `sign` consumes `prepared_id`, rebuilds `E` from its **current** view and refuses with `event_changed`
   unless it is byte-identical to the prepared one (the view moved, a role or certificate changed), checks the proof,
   signs with `UK`, verifies its own output with §8.3, writes the `custodial` audit record, and only then delivers it
   (`event_deliver`).

### 8.5 Custodial types, roles and actions (phase 4, V3-D23, D70)

**The closed list** (reference `H.CUSTODIAL_TYPES`, `vectors.phase4.custodial_types`): `(type, log) → lowest hub
role`. The hub builds only these and the host accepts `auth: "hub"` only for these (§8.3 step 7b); `read` starts
nothing (D70: read = see). Unknown or unlisted → `not_custodial`.

| Lowest role | Ticket log | Workspace log |
|---|---|---|
| `edit` | `ticket.created`, `ticket.updated` (`set` of the paths below only), `status.changed`, `ticket.submitted`, `ticket.closed`, `ticket.reopened`, `claim.released`, `log.added`, `handoff.written`, `question.asked`, `question.answered`, `gate.approved`, `gate.changes_requested`, `verdict.given` | — |
| `admin` | `ticket.moved`, `people.changed`, `visibility.changed` | `addon.disabled`, `addon.purged` |

The role is the floor; the F1 rules decide the rest (eligible approvers, ticket owner, D59 independence; §8.3 step 9).
**Not channel-startable in V3:** `policy.changed` (ticket override and workspace) and `settings.changed`: they
change who may approve and how signatures are made, and their payloads are free-form policy and settings objects.
People and visibility changes are `admin`-only through a channel, with exact schemas. **Not custodial in hv 0, and
why:** `workspace.created`, `member.*`, `role.changed` (members and roles come from
grants and snapshots); `device.*` (hub users have no device keys); `hub.joined`, `hub.left` (owner's local key at the
terminal, break-glass); `grant.issued`, `grant.revoked` (see §16 open points); `addon.granted` (the host derives the
manifest fields); `restore`, `invalid.acknowledged` (owner recovery); `artifact.*` and `ticket.updated` with
`sections` (the file bytes and body text are the host's; no transport for them yet); agent-only types (`claim.taken`,
`task.*`); host types; addon types. Adding a type later needs no new `hv`: a host that does not list it refuses it,
which fails closed.

**Actions.** What a channel asks for: `{"type", "log", <fields>}` (`ticket.created` has no `log`: the hub makes the new
ticket's uid). `log` is `"workspace"` or a ticket ULID. The fields are exactly (`?` optional):

| Type | Fields |
|---|---|
| `ticket.created` | `ticket_type` (ticket type list), `title` (one line, ≤ 200 bytes) |
| `ticket.updated` | `set`: 1–7 of exactly these paths: `ticket.title` (one line), `ticket.priority` (priority list), `ticket.size` (size list or null), `ticket.labels` (sorted unique label tokens, ≤ 32), `ticket.due` (`YYYY-MM-DD` or null), `ticket.parent` (key or null), `ticket.blocked_by` (sorted unique keys, ≤ 32) |
| `status.changed` | `to` (`backlog`, `open`), `reason?` (one line) |
| `ticket.moved` | `to`, `reason` |
| `ticket.submitted` | — |
| `ticket.closed` | `resolution` (close list), `duplicate_of?` (a key; only and always with `duplicate`), `text?` |
| `ticket.reopened` | `text?` |
| `visibility.changed` | `visibility`: `"workspace"` or exactly `{"restricted": [1–64 sorted unique person ids]}` |
| `people.changed` | `role` (ticket people list), `add`, `remove` (sorted unique person ids `p_…`/`u_…`, ≤ 16 each, disjoint, not both empty; `owner`: `add` exactly one, `remove` empty) |
| `claim.released` | `session`, `reason` (`released`) |
| `log.added`, `handoff.written` | `text` (≤ 4096 / 2048 bytes) |
| `question.asked` | `question`: exactly `{to, text, why?, options?, recommended?, blocking}` (no `id`: the hub assigns it); `to` a person id or a question role; `options` 1–8 of exactly `{key, label, cost?}` with unique token keys; `recommended` one of the keys |
| `question.answered` | `question` (`Q<n>`), `hash`, `option?` (token), `text?` (at least one of the two) |
| `gate.approved` | `gate` (`requirements`, `plan`, `code`), `hash` |
| `gate.changes_requested` | `gate`, `hash`, `text` |
| `verdict.given` | `outcome`, `hash`, `text?` (required on `fail`) |
| `addon.disabled`, `addon.purged` | `name` (addon name) |

**A new ticket's uid and key (L2-D15-4).** For `ticket.created` the hub proposes the `key` and the new uid. The uid is
any fresh ULID (its derivation is not normative). The host accepts only `key == next_key` (its own allocator, which may
have seen numbers the hub's view does not hold) and an unused uid; otherwise it refuses with `ticket.exists` or
`ticket.key_not_next`. On `ticket.key_not_next` the channel prepares again (a new digest) and the person sees the new
key. The key number counts restricted tickets too, as on the host (restricted is not secret), so this leaks nothing.
A hub that cannot read every ticket log answers `unavailable` to `ticket.created`.

**No free-form object anywhere in an action** (security review of 28e767d, field smuggling): every nested value has
an exact schema, so no action can carry a reserved field or a field owned by another type at any depth (vectors
`action_smuggle_*`: `set` of `ticket.visibility`, `ticket.tasks` with a `verify.cmd`, `ticket.acceptance`, `ticket.type`,
`ticket.addons`, a reserved path, a nested object, a question with its own `id` or a `sig`, extra option or visibility
keys, …).

**One text rule for every title and text field of the contract** (actions, shared items, summaries), evaluated under
**Unicode 16.0.0** (L2P4-3): 1 to n UTF-8 bytes, no leading or trailing Unicode `White_Space`, **no character of the
categories `Cc`, `Cf`, `Zl`, `Zp`, `Cs`, `Co`, `Cn`** (controls including C1 such as U+0085 NEL; format characters such
as the bidi overrides U+202A–202E and U+2066–2069, U+200E/200F and U+200B; the separators U+2028/2029; lone surrogates;
private use such as U+F8FF; unassigned code points, so verifiers on newer tables cannot diverge) and **no
`Default_Ignorable_Code_Point`** (blank look-alikes such as U+3164, U+115F, U+FFA0, U+034F and the variation
selectors); LF is the one exception, in multi-line texts only. A lone surrogate is a refusal, never a crash. A one-line value (a title, a reason) never contains LF. Anything else is `malformed` (vectors
`action_text_*`, `share_item_title_*`): a phone or browser shows these texts next to a signature prompt, so a
display-spoofing character could make one text look like another.

**A decision names the hash the person reviewed** (`hash` on `gate.approved`, `gate.changes_requested`,
`verdict.given`, `question.answered`; **`action.hash` becomes `event.hash`, byte-equal**, L2P4-2a): the hub refuses with
`event_changed` when its view's current hash differs (and `check_built` checks the built event, §8.4), so a decision never lands on content the person did
not see. **Everything else in the event is the hub's**: an action that carries any envelope, custodial or host field
(`v`, `id`, `actor`, `based_on`, `roster_v`, `hash_v`, `gate_gen`, `policy_hash`, `source_sha`, `qid`, `base_rev`,
`sections`, `auth`, `channel*`, `hub_id`, `snapshot_version`, `*_cert`, `sig`, `seq`, `at`, `prev`, `ws_seq`,
`host_sig`) or any field not listed is `malformed` (vectors `action_smuggles_*`). The hub derives `based_on` (the log
head of its view), `roster_v`, `gate_gen`, `policy_hash`, `source_sha`, `qid` and `base_rev` from its view. Order of the
check: shape and `log` → `malformed`; type listed → `not_custodial`; role → `forbidden_role`; fields → `malformed`
(reference `check_action`; vectors `action_*`).

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

1. card and `pk_pub` present → `unknown_workspace` (vector `hub_join_binding_without_card`). **On the link** a `join` body without `card` or `pk_pub` never gets here: it fails the exact field check of §10.2 step 6 first and is `malformed`, before any registry lookup; `unknown_workspace` is the code of the object check itself;
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
| `audit_ack` | w2h | both | `{"seq", "head": b64u(32)}` | `{"acked": seq}` (§10.5) |
| `audit_get` | w2h | both | `{"since"}` | `{"records": [{"record": <hub.audit>, "event"?}], "head", "op_certs": [<hub.op_cert>]}` (D4, §10.5; added) |
| `share_put` | w2h | selective | `{"item_id", "sealed": b64u}` | `{}` |
| `share_revoke` | w2h | selective | `{"item_id"}` | `{}` |
| `space_list` | w2h | both | `{}` | `{"spaces": [<hub.space>]}` (V3-D8, §10.4) |
| `space_accept` | w2h | both | `{"accept": <space.accept>}` | `{}` (L2P-2) |
| `space_put` | w2h | both | `{"space_id", "epoch", "item_id", "sealed": b64u}` | `{"seq"}` |
| `space_revoke` | w2h | both | `{"space_id", "item_id"}` | `{"seq"}` |
| `space_get` | w2h | both | `{"space_id", "since"}` | `{"space", "wraps", "entries", "next"}` |
| `space_proposals` | w2h | both | `{}` | `{"proposals": [<hub.space_proposal>]}` (p4, §10.4) |
| `log_sync` | w2h | full | `{"epoch", "log", "from_seq", "count", "head", "reset", "sealed": b64u}` | `{"seq", "head"}` (p4, §10.7) |

A reply's body is either the reply body above or `{"error": <code>}`; H0 fixes request bodies only (reply bodies are
built in D5/C5 against this table). Phases: `snapshot_get`, `publish_out` and the selective ops in phase 2 (share ops
carry needs-you summaries only until C6); `route_*`, `wk_seal`, `audit_ack` in phase 3; `event_deliver`, `log_sync`, `space_proposals` in phase 4.
`log_sync` carries the workspace's own log **to** the hub; like every other op it asks the hub to sign nothing, and
nothing in it can become signed bytes: the hub builds custodial events only from an action a channel started (§8.4).

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

Reply rules:

- A reply echoes `op` only if it is in the closed op list above; otherwise `op` is the fixed literal `"error"`. A sender never gets a signature over a string it chose; the requester correlates by `re`.
- A refusal at steps 1–6 is not a reply: the hub sends an unsigned `{"t":"error","error":code}` frame and drops the link, never a signed reply. This holds above all for a peer that has not authenticated, whose input must not be answered with anything signed.
- After the link is authenticated, a request is accepted only while the link's key is the workspace's current pinned `WSK`; a link opened before the pin (the first link of a join) is closed with `wsk_mismatch` as soon as another link pinned a different key.
- After `hub.unbound`, every op except `join` and `binding_put` is refused with `not_bound`.

#### 10.2b The content of a selective `share_put` (phase 4, C6/D9)

The `share_put {item_id, sealed}` body of H0 left `sealed` opaque. Its content, merged from C6 (orch-core #1022) and D9
(orch-hub #23) (DECIDED HERE where they differed):

```
share.item {v, suite, kind: "share.item", hv, hub_id, workspace_id, item_id, item_kind: "needs_you" | "ticket" | "question",
            item_version ≥ 1, issued_ms, chain: {log, seq, head}, content: <exact per item_kind>, dev}
            signed by the workspace's WSK, label sig_share_item
IK        = 32 fresh random bytes per put
wrap      = seal_to(HXK, purpose "hub-share", recipient id hub_id, object_id item_id, epoch item_version,
                    extra workspace_id (16); IK)                                         (v2 §5.3, 113 bytes)
ct        = SALTED-AEAD(IK, "orch/v2/hub-share-msg",
                        AAD = "orch/v2/hub-share-aad|" || suite || hub_id || workspace_id || item_id || u32(item_version),
                        cj(signed share.item))                                           (v2 §5.1)
sealed    = b64u(u32(item_version) || wrap || ct)
item_hash = "sha256:" + hex(H("orch/v2/hub-share-item|" || cj(signed share.item)))
```

- **Sealing (C6 1, D9 seal).** The per-item key is sealed to the hub's `HXK` with the v2 §5.3 construction; its AAD binds
  the purpose, hub, workspace, item id and version, and the content AAD binds them again. **Amendment to v2 §5.3's
  closed purpose list** (with `space-sk`): `hub-share` | recipient: the hub (`HXK`), recipient id `hub_id` | `object_id`
  item_id | `epoch` item_version | `extra` workspace_id | plaintext `IK` (32). D9's `{v, epk, nonce, ct}` JSON seal is
  replaced by this binary form: the contract has one sealing construction (v2), and the zero nonce is safe because `IK`
  and the ephemeral key are fresh per put.
- **The signed item (C6 2, D9 plaintext).** `WSK` signs the whole item including its content (stronger than signing a
  digest of it). `item_hash` is computed by both sides, never carried. `issued_ms` is the host's clock at share time.
- **The chain head (C6 3).** `chain` = `{log, seq, head}`: the log the item comes from (`"workspace"` or the ticket
  ULID), its `seq` and event head (ticket-format §5.5) when the host made the item. **When a custodial event about a
  shared item comes back** (selective mode, C6), the event carries the `item_hash` inside its signed payload (so the
  channel proof binds it), and the host checks before §8.3 step 9: the hash is of an item it shared and has not
  revoked (`not_granted` otherwise), and `chain.head` is the head of `chain.log` at `chain.seq` in its own log (else
  `view_fork`: the hub or the link changed the item). The exact field name in the event comes with C6's event types.
- **Content per kind (C6 4, D9).** Exact keys, no free objects, the text rule of §8.5:
  - `needs_you` `{type: question | gate | verdict | review | handoff, title, deadline}`;
  - `ticket` `{key, type (ticket type), title, status (status list), labels (sorted unique label tokens ≤ 32),
    deadline}`;
  - `question` `{key, type (the ticket's type), id (Q<n>), title (the ticket's title), text (≤ 2000 bytes, multi-line),
    deadline}`;
  - `title` one line ≤ 200 bytes; `deadline` `YYYY-MM-DD` or null (C6's date; D9's `deadline_ms` is replaced: the
    source is the ticket's `due` date). C6's kind names win (`ticket`, not D9's `item`); D9's `item_type` is `content.type`.
- **Replace and revoke (C6 5, D9).** `item_id` is 16 host-chosen bytes. A put with a **higher** `item_version` replaces
  the held item; the same version with the same bytes is an idempotent `{}` (a resend); the same version with other
  bytes or a lower one is `stale_version` (D9's monotonic counter wins over C6's timestamp: no clock in the order).
  `share_revoke {item_id}` deletes the item and is idempotent: an unknown or already revoked id is `{}` too (D9). The
  hub keeps a tombstone `{item_id, item_version}`, so the id may be **shared again** only with a higher
  `item_version`, and a revoked version never comes back.
- **Limits (C6 6, D9).** 16 KiB sealed per item (`too_large`; summaries are small, D9's 256 KiB is not needed), at most
  **1000** live items per workspace (`too_many_items`; C6's 500 and D9's 1000: one number), so at most 16 MiB sealed per
  workspace.
- **The hub's check, in order** (reference `open_share_item`, vectors `share_item_*`, `share_revoke_*`): body fields
  (`malformed`); size (`too_large`); the wrap opens under `HXK` and the content under `IK` with their AADs for this hub,
  the link's workspace and the item id (`bad_wrap`); strict parse; `WSK` signature under the pinned key
  (`bad_signature`); field set (`malformed`); `hub_id` (`other_hub`); `workspace_id` (`other_workspace`); `item_id` and
  `item_version` equal the body's and the prefix (`malformed`); chain and content (`malformed`); dev marker; the version
  rule; the item cap. Each accepted put or revoke is a `share` audit event (§10.5).

#### 10.2a `wk_seal` (phase 3, hub-wkseal)

Served only while the workspace's live binding is `full`; the body is exactly `{"epoch", "wrap"}` (`epoch` 1..2^32−1,
`wrap` canonical base64url). The wrap is v2 §5.3 `seal_to(HXK, purpose "wk", recipient = hub_id, object = workspace_id,
epoch, WK_e)`, 113 bytes (`eph_pub || AES-256-GCM(32-byte key)`); the hub opens it with `HXK` only to check the binding
(hub, workspace, epoch → else `bad_wrap`), drops the plaintext at once and stores the SEALED wrap per (workspace, epoch)
in its private `wk-seal.db`. Per workspace epochs only grow: the same epoch with identical bytes is an idempotent `{}`,
the same epoch with other bytes `bad_wrap`, a lower epoch `bad_epoch`. On `hub.unbound` or a switch to selective the
stored wraps are retired (kept, never served, not counted); a later full binding may seal any epoch again. The hub never
logs key material; `orch-hub inspect` shows `wk_epochs` (numbers only) per workspace.

### 10.3 Routing (`route_out`, `route_in`; phase 3, V3-D8)

The envelope is the v2 §11.1 layout, forwarded as the sender's exact bytes (the hub never opens, re-signs or edits it,
and never logs it). `route_out` checks, in order: size ≤ 1 MiB (`too_large`); layout, magic, version, suite, flags,
reserved byte, `COSIGNED` block (`malformed`); `from_ws` is the link's workspace (`other_workspace`); `to_ws` is not
the sender (`malformed`); the sender's pinned `WSK` signature over `"orch/v2/ws-envelope|" || header || body`
(`bad_signature`); the recipient is joined, bound to this hub and the route is allowed (`not_allowed`: one code for
"not here", "unbound" and "denied", so a member learns nothing about other workspaces); the recipient's queue has room
(`queue_full`: per recipient 256 entries / 16 MiB / 64 per sender); the same `(from_ws, id)` with other bytes
(`replay`). The sender's own state is checked by the generic rules (`unknown_workspace`, `not_bound`).

Reply body: `{"state": "forwarded" | "queued"}` (additive to the H0 `{}`; `forwarded` = pushed to the recipient's live
link, `queued` = held). A route is allowed in phase 3 when both workspaces are bound to this hub and the hub's access
layer (D3) does not deny it.

`route_in` is an h2w request `{"envelope"}` pushed on the recipient's link, one unacknowledged request per link, in
enqueue order. The host answers with a w2h reply (`op: "route_in"`, `re` = the request's `seq`, body `{}`, or
`{"error": code}` if its receiver refused it); only the answer removes the entry, so delivery is at least once and the
receiver deduplicates on `(from_ws, id)` (v2 §11.4). Queued envelopes are persisted, delivered in order on reconnect and
expire after 31 days. Frames may be up to 1.5 MB (an envelope of 1 MiB as base64url).

### 10.4 Shared spaces (phase 3, V3-D8, spec D75)

**What a space is.** A hub-hosted set of items that several member workspaces of this hub put into and read from.
It has a `space_id`, a member list (workspaces of this hub) and, per **epoch**, a 32-byte key `SK_e` that the hub
generates and holds. **Option B (spec §2, D75): the hub holds every `SK_e`, opens every item it stores and may index
it.** What the hub never holds is a workspace's own keys: items enter only because a member **host** put them (below),
and workspace traffic outside spaces stays E2E sealed (§10.3 envelopes).

**Who creates and changes a space, and who consents (L2P-2).** Hub-side policy decides who may *ask* for a change: a
hub-admin, or a user with `admin` in **every** member workspace (D3's `Access`). That is not consent to *join*:

- **A selective workspace is added only by its owner.** D71: the hub-admin has no role in a selective workspace; §8a: only
  its owner, at the host terminal, decides what is shared. The owner's host signs a `space.accept`
  `{hub_id, space_id, workspace_id, epoch, prev_members_hash, members_hash, dev}` with its `WSK` (reference label
  `sig_space_accept`; made only by a human-only host verb, never by an agent) for exactly one transition: the `epoch` the
  addition creates, the member set before it (`prev_members_hash`; the hash of the empty list for a new space) and the
  set after it. An old accept cannot be replayed when the same set recurs, so a workspace that was removed and is listed
  again needs a new owner decision (vector `space_add_selective_accept_replayed_at_later_epoch`), and sends it with `space_accept
  {accept}`. The hub must hold it before it signs a `hub.space` that lists that workspace (`not_accepted` otherwise;
  `check_added_members`). A hub-admin can **remove** a selective workspace and can never add one. A workspace whose mode
  is unknown is treated like a selective one.
- **A host puts only into a space whose member set its owner approved.** The owner's share rule pins
  `{space_id, members}`, not just the `space_id`. Before **every** `space_put` the host verifies the current `hub.space`
  (signature, epoch not older than the one it holds) and refuses to put when the member set holds anyone the owner did
  not approve: `members_changed` (local, never sent). A smaller set needs nothing; a grown set needs the owner to
  re-approve at the host (an `admin` of that workspace in full mode), which replaces the rule (`check_host_put`,
  vectors `host_put_*`). So a member added later by a compromised or careless hub reads **nothing** the host puts
  after that, unless the owner approved the new set.
- **Items of a space are untrusted input** to the agents of a member workspace (D29 rules for foreign content): an item
  is data from another workspace, never an instruction.

**Creating a space or adding a member pulls no data.** An item exists only if a member host chose to `space_put` it
under the rules above. In a selective workspace `space_put` is accepted (the op exists in both modes) and what may be
put is the owner's share rules at the host; the hub cannot check them.

**Epochs and rotation.** `epoch` starts at 1 and **rises on every membership change** (add, remove, a workspace that
unbinds). The hub makes a fresh `SK_e`, signs a new `hub.space` and issues one `hub.space_wrap` per current member. A
member host holds the `SK_e` of every epoch it was a member of. **A workspace that leaves loses all access to later
epochs**: it gets no later wrap, and the hub answers its `space_get` / `space_put` / `space_revoke` with `not_member`.
**Revocation cannot take back what was read:** a leaver (or a revoked item's earlier readers) keeps the plaintext and
the old `SK_e` it already held; rotation only ends *future* access. A member added later gets wraps from its first epoch
on and **no backfill**: items of earlier epochs are not returned to it (the hub could re-seal them; V3 does not).
A workspace that leaves and returns reads the epochs it was in, not the gap.

**Unbinding ends membership in the same step (decided).** When a workspace's `hub.unbound` is accepted (§9.4), the
hub removes it from **every** space it is in before it answers anything else for that workspace: one new epoch per
space, a fresh `SK_e`, a new `hub.space` that no longer lists it. The record must be exactly the old one at
`epoch + 1` minus that workspace; anything else is `malformed` (`apply_unbind`, vectors `space_unbind_*`). A space whose
last member unbinds ends (audited as `delete`). A workspace that is not a member needs no record.

#### The objects

```
hub.space      {v, suite, kind: "hub.space", hv, hub_id, op_cert_id, space_id, epoch ≥ 1,
                members: [workspace_id]  (sorted, unique, 1..64), sk_commit: b64u(32), issued_ms, dev}
                signed by HK_op, label sig_hub_space
hub.space_wrap {v, suite, kind: "hub.space_wrap", hv, hub_id, op_cert_id, space_id, epoch, workspace_id,
                wxk_version ≥ 1, sealed: b64u, dev}                        signed by HK_op, label sig_hub_space_wrap
space.item     {v, suite, kind: "space.item", hv, hub_id, space_id, epoch, members_hash: b64u(32), item_id,
                workspace_id (the author), created_ms, body: object (opaque to the contract), dev}
                                                                            signed by the author's WSK, sig_space_item
```

- `sk_commit = b64u(H("orch/v2/hub-sk-commit|" || suite || space_id || u32(epoch) || SK_e))`: in the signed record, so
  every member opens a wrap to the same key (like v2's `ck_commit`).
- **The wrap** is v2 §5.3 `seal_to` with a new purpose `space-sk`. **Amendment to v2 §5.3's closed purpose list**
  (orch-relay `docs/protocol-v2.md`; to be applied there with this contract): `space-sk` | recipient: workspace (`WXK`)
  | `object_id` space_id | `epoch` space epoch | `extra` `hub_id || u32(wxk_version)` | plaintext `SK_e` (32). It is the
  workspace-recipient counterpart of the device purpose `sk`: recipient = the member's `WXK` of `wxk_version`, recipient id = `workspace_id`, `object_id = space_id`,
  `epoch = space epoch`, `extra = hub_id || u32(wxk_version)`, plaintext `SK_e`. A sealed blob proves nothing about its
  sender (v2 §5.3); that is why the wrap object is signed by `HK_op`.
- **An item signs the hub, the epoch and the member set it is shared with (L2P-1).** `members_hash =
  b64u(H("orch/v2/hub-space-members|" || cj(sorted member list of that epoch)))`. The AAD alone is not enough: the hub
  writes the AAD (it holds every `SK_e`), so only the author's signature can carry the author's consent to one epoch's
  member set. The hub (at `space_put`) and every reader check `hub_id` (`other_hub`), `epoch`, `space_id`, `item_id` and
  `members_hash` against the `hub.space` of that epoch (`malformed`). An item re-sealed by the hub into a later epoch,
  into a space of another hub, or shown to another member set fails (vectors `space_item_resealed_into_later_epoch`,
  `space_item_from_other_hub`, `space_item_members_hash_mismatch`); a hub that shows two member lists for one epoch is
  caught by the author's own hash. "No backfill" is therefore enforced by the authors' signatures, not only by hub policy.
- **An item** is sealed with SALTED-AEAD (v2 §5.1) under a key derived through HKDF, never `SK_e` itself:
  `base = HKDF(SK_e, salt = 32 zero bytes, info = "orch/v2/space-item|" space_id "|" epoch)`; per item
  `key = HKDF(base, salt16, "orch/v2/space-item-msg")`; `sealed = salt16 || AES-256-GCM(key, 12 zero bytes,
  cj(signed space.item), AAD)` with `AAD = "orch/v2/space-aad|" || suite || space_id || u32(epoch) || item_id`.
  The AAD binds space, epoch and item id: an item moved to another space, epoch or id fails (vectors
  `space_item_wrong_epoch`, `…moved_to_other_item_id`, `…moved_to_other_space`). The signed inner object carries the
  author, so a reader (or a later reader of the hub's store) can tell who wrote it, and a hub or a member cannot
  plant an item in another workspace's name (vectors `space_item_signed_by_other_member`, `…author_not_member`).
  An item is **immutable**: a change is a new `item_id` plus a `space_revoke` of the old one.

`space.accept` is the fourth object: see the consent rules above (signed by the workspace's `WSK`).

**The proposal an invited workspace verifies (p4, #1020).** A workspace that is not yet a member cannot `space_get`,
so before this extension the epoch and member lists of its `space.accept` came from the hub admin's text. Now the hub
signs the transition it will make:

```
hub.space_proposal {v, suite, kind: "hub.space_proposal", hv, hub_id, op_cert_id, space_id, epoch,
                    prev_members: [workspace_id] (sorted, unique, 0..64), members: [workspace_id] (sorted, unique, 1..64),
                    workspace_id (the invitee), issued_ms, expires_ms, dev}       signed by HK_op, label sig_hub_space_proposal
```

The hub makes one per invited workspace when an authorized requester (hub-admin or admin in every member workspace)
asks to add it, and serves the pending ones that name the link's workspace on `space_proposals {}`. It never makes one
for a workspace without such a request, and a later `hub.space` at that epoch must list exactly `members` (the hub's
`check_added_members` already needs the accept, whose `members_hash` is over this list). **The invited host checks**
(`verify_space_proposal`, in order): the held card's op cert not revoked, signature under `HK_op` with its own label,
field set, `hub_id`, `op_cert_id`, dev marker (`revoked`, `bad_signature`, `malformed`, `other_hub`, `stale_op_cert`,
`dev_*`); both lists sorted, unique, in range, `prev_members` empty exactly at `epoch` 1, the invitee in `members` and
not in `prev_members` (`malformed`); `workspace_id` is this workspace (`other_workspace`); `issued_ms ≤ now + 300 s`,
`expires_ms > issued_ms` (`cert_invalid`), `now < expires_ms` (`cert_expired`); if it holds a `hub.space` of that space
(a rejoiner), `epoch` is higher (`stale_epoch`). Then the host shows the owner the space, the epoch and every member
workspace (by the names of its own pins; an unpinned id is shown as such), and on the owner's confirmation at the
terminal builds `space.accept` **from the proposal** (`accept_from_proposal`: `prev_members_hash` and `members_hash`
over the signed lists) and sends it with `space_accept`. Vectors `space_proposal_*`. **Disclosure, stated:** the invitee
learns the current members' workspace ids before it decides; it needs them to consent.

**Where a reader gets a co-member's `WSK` (L2P-7).** Only from that workspace's v2 directory card, verified through its
`ws_delegation` (the same card store and never-change rules as orch-publish, §16), **pinned per `workspace_id` by the
reader itself**; never from a map the hub supplies (the hub holds every `SK_e` and can seal anything, so a hub-supplied
key would let it plant items "from Marketing"). The `wsk_pubs` argument of `verify_item` is that pin. A member with no
pin gives an item that is **refused** ("author unverified"); agents never act on it (vectors
`space_item_author_key_from_hub_differs_from_card`, `space_item_author_unknown_key`). **Residual, stated:** the first
card of a co-member is first-come (relay directory), and after H5 cards travel through the hub (`card_put`), so a hub can
introduce a fabricated card for a `workspace_id` the reader has never seen; what a human verifies once is the card's
`owner_person_id`.

#### Operations (§10.2 table; w2h; both modes)

| Op | Rules, in order | Refusals |
|---|---|---|
| `space_list` | returns the current `hub.space` of every space the link's workspace is a member of | — |
| `space_put` | the workspace is a member of the current epoch; `epoch` = the current epoch; size ≤ 256 KiB sealed; the hub opens the item (option B) and checks it is for this space and `item_id`, written and signed by **the link's workspace**; the same `(space_id, item_id)` with the same bytes is idempotent (same `seq`) | `not_member`, `stale_epoch` (older: refresh with `space_get` and re-seal), `malformed` (newer), `too_large`, `malformed` / `bad_signature` / `unknown_workspace` (the item), `other_workspace` (author ≠ link), `replay` (same id, other bytes) |
| `space_accept` | `{"accept": <space.accept>}`: stored if it verifies under the link workspace's pinned `WSK`, names this hub and this workspace (`other_workspace`, `other_hub`, `bad_signature`, `malformed`); used by the next listing of that workspace in a space | as listed |
| `space_revoke` | member; the item exists; the link's workspace is its author (a leaver can no longer revoke: revoke before leaving). The revocation is a new `seq`, so pollers see it. Idempotent | `not_member`, `unknown_item`, `other_workspace` |
| `space_get` | member of the current epoch. Entries with `seq` > `since`, **only items of epochs in which the caller was a member**, oldest first, at most 100 entries and 1 MiB of sealed data per page. Reply `{"space": <current hub.space>, "records": [<hub.space> of each older epoch a wrap or entry names; additive, L2P-12], "wraps": [<hub.space_wrap> of the caller for the current epoch and each epoch an entry belongs to], "entries": [{"seq", "item_id", "epoch", "revoked", "sealed"?}], "next"}` (`sealed` absent when `revoked`; `next` = the last `seq` returned, or `since`) | `not_member` |

**One answer for "no such space" and "not a member"** (`not_member`), so a workspace learns nothing about spaces it
is not in. A host that gets `stale_epoch` or sees a higher `epoch` in a reply verifies the new `hub.space`
(under the pinned hub card, epochs only rise: `stale_epoch` for a replayed older record, `space_conflict` for the same
epoch with other **content**: the identity of a record is `(space_id, epoch, members, sk_commit)`, not its signature, so
the hub's re-signed record after an `HK_op` rotation is no conflict, L2P-3; after a rotation the hub re-signs the
current record of every space and every wrap it may still serve under the new op cert), then its wrap: signature, this workspace is listed, the wrap names this space, epoch,
workspace and its `WXK` version (`bad_wxk_version` otherwise: rotate and ask again), the seal opens (`bad_wrap`) and the
key matches `sk_commit` (`commit_mismatch`).

The reference checks are `space`, `space_wrap`, `space_item` and `space_flow` (put / revoke / get over a space with
rotations) in `orch_hub.contract.spaces`; the negative vectors cover non-member put and get, unknown space, stale and
future epoch, a replayed put, an item from a workspace outside the space, a leaver's read after rotation, a late
joiner's missing backfill and a rejoiner's gap.

**Not in this section** (named so nobody guesses): hub↔hub spaces (D77, H7); re-sealing old items for new members;
the hub-side audit of space reads (D4); the admin CLI and its authorization (D3 / D8 build, against this section).
**Decided by the orchestrator:** `space_list` is part of the op list; `space-sk` is added to v2 §5.3's purposes (above);
unbinding removes the workspace from every space in the same step (above).

### 10.5 The hub audit chain (phase 3, D4)

**What it is.** One hash-chained log per hub of **every custodial signature and every grant, membership and space
change** (spec §5 "Audit"). It makes a hub that edits its own history detectable by anyone who holds a later head:
workspaces verify and acknowledge it (`audit_ack`), admins read it in full.

```
hub.audit {v, suite, kind: "hub.audit", hv, hub_id, op_cert_id, seq ≥ 1, prev: b64u(32), ts_ms,
           type: "custodial" | "grant" | "membership" | "space" | "channel" | "share", scope_tag: b64u(32) | null,
           event_hash: b64u(32), dev}                       signed by HK_op, label sig_hub_audit
```

- **Chain rule.** `chain_hash(r) = b64u(H("orch/v2/hub-audit-chain|" || cj({"o", "sig"})))`, over the whole signed
  record. `seq` starts at 1 with `prev = b64u(32 zero bytes)`; record `n + 1` has `seq = n + 1` and
  `prev = chain_hash(record n)`. Rewriting, removing or reordering any record breaks every later `prev`.
- **The record signs the event's hash, not the event** (`event_hash = b64u(H("orch/v2/hub-audit-event|" || cj(event)))`),
  so a workspace can verify the whole chain, including records about others, and sees the events only of its own
  scope. **The scope is blinded (L2P-4):** `scope_tag = b64u(H("orch/v2/hub-audit-scope|" || workspace_id (16) ||
  nonce (32)))`, where the 32 random bytes `nonce` are a field **inside the event**. Only the workspace the
  event is about receives the event, so only it can compute and check the tag; every other tenant sees an opaque value
  and learns neither the `workspace_id` (also the TOFU key of §9.5) nor which records are the same workspace's.
  `scope_tag` is `null` for hub-wide events (memberships). `audit_mismatch` when the tag does not match. **What stays
  visible to every workspace:** `type`, `ts_ms`, `seq` (so the hub-wide volume of custodial signatures, grants,
  memberships and space changes and when they happened, not whose).
- **Events** (closed types, exact fields): `custodial` `{workspace_id, log, verb, user_id, channel_id, digest, nonce}` (the
  custodial event digest of §8.2); `share` (p4) `{workspace_id, item_id | null, change: put | revoke | purge,
  item_version | null, item_hash | null, count | null, nonce}`, scoped to the workspace (D9's local `share_log`
  stays; the chain record is what other parties can check). Exact per change: `put` names the item, its version and
  `item_hash`; `revoke` names the item and the **tombstoned** `item_version`, `item_hash` null (a revoke of an unknown or
  already revoked id is the idempotent `{}` and writes **no** record); `purge` is one record per workspace at unbind or
  at a mode switch, `item_id`, `item_version`, `item_hash` null and `count` = the live items purged; `count` is null
  otherwise (vectors `audit_share_*`); `channel` (p4) `{channel_id, user_id, change: register | revoke, factor,
  actor_user_id | null, nonce}`, hub-wide (`scope_tag` null; `actor_user_id` null = the operator at the hub terminal,
  there is no `unrevoke`, §6.2); `grant` `{group, workspace_id, role | null, actor_user_id, nonce}`; `membership`
  `{group, user_id, added, actor_user_id, nonce}`; `space` `{space_id, epoch, change: create | add | remove | unbind | delete,
  workspace_id | null, actor_user_id | null, nonce}`. The acting admin's `UK` signature on a grant or membership change is
  the custodial event it came from (spec §10); the audit record points at it, it does not replace it. **Every event of every type carries `nonce`, 32 random bytes
  inside the event; no type is exempt.** Records carry the event's hash to every tenant, so an event without a nonce
  could be confirmed by guessing its low-entropy fields (a user id and a group name). With the nonce the hash reveals
  nothing, and for events with a workspace the same nonce blinds the `scope_tag`. An event without a 32-byte `nonce` is
  `malformed` (vectors `audit_event_without_nonce_*`, `audit_event_short_nonce_*`).

**Verification by a workspace** (reference `verify_record_ex` + `apply_record`). **Key rotation (L2P-3):** a record
verifies under the op certificate **it names** (`op_cert_id`): the held card's current one, or an earlier one that this
hub's root signed (`hub.op_cert`, served in `audit_get`'s `op_certs`). The chain therefore survives a routine `HK_op`
rotation: a new workspace verifies from genesis, an offline one past its head. The verifier does not trust the record's
own date alone:

- `ts_ms` may not be ahead of the verifier's clock plus 300 s (`not_yet_valid`) and **never decreases along the chain**
  (`stale_timestamp`);
- a non-current op cert must have been valid at the record's `ts_ms` and must **not have lapsed before its successor was
  issued** (`stale_op_cert`: nothing could have signed in that gap); and a record **beyond the verifier's trusted head**
  needs a cert that is **not expired at the verifier's own time** (`cert_expired`): an expired old key cannot be made to
  sign a "backdated" new record (vector `audit_expired_key_backdated_into_its_validity`). History up to the trusted head
  is covered by the hash chain and stays valid whatever became of the cert (vector
  `audit_expired_key_old_record_still_verifies`). Consequence, stated: a workspace that was offline longer than an old
  cert's life cannot catch up through records that cert signed beyond its head, and a new workspace cannot verify from
  genesis through expired certs; both start from an anchor head obtained at join (see the L7 note);
- `op_seq` of the op cert never decreases along the chain (`stale_op_cert`). The floor is the head's `op_seq`, or, when
  the head has none (an anchor head), **the held card's current `op_seq`**, so an anchor cannot be used to take an old
  key back (vector `audit_head_without_op_seq_uses_the_held_card`). A non-current cert that is not served, not signed by
  this root, or not lower than the current one is refused (`stale_op_cert`, `bad_signature`);
- a **revoked** op cert (`hub.op_revoked`) signs nothing, not even at a past `seq`: a record under it verifies only if it
  **is** the record the verifier already accepted (same bytes: the verifier keeps the chain hash it accepted per `seq`),
  otherwise `revoked` (vectors `audit_revoked_key_forges_a_passed_seq`, `…_the_head_seq`). A workspace that joins after a
  revocation therefore starts from an anchor head it was given at join (stored, trust-on-first-use for the prefix, see the
  L7 note below) and does not re-verify history signed by the revoked key.

Then the usual checks (`hub_id`, dev marker, exact fields), `seq = 1` ⇒ genesis `prev`;
then, with its head `{seq, hash}` (or none: it expects genesis, or starts from an anchor head it was given):

| Situation | Result |
|---|---|
| `seq = head.seq + 1` and `prev = head.hash` | accept; the head moves |
| `seq > head.seq + 1`, or no head and `seq > 1` | `audit_gap` (a record was dropped, or the hub withholds it) |
| `seq = head.seq + 1` and another `prev`; or `seq = head.seq` with other bytes | `audit_fork` (the hub signed two histories) |
| the same bytes at `seq = head.seq`, or a record older than the head | ignored: the head stays (L2P-10: overlapping `audit_get` pages re-deliver records). If the verifier kept the hash it accepted for that `seq` and the bytes differ: `audit_fork`. It keeps any two valid records with one `seq` and different bytes as fork evidence |
| an event it holds does not hash to `event_hash`, is no valid event of that `type`, or its tag is not the record's `scope_tag` | `audit_mismatch` |

**`audit_get {since}`** (w2h, added so the ack has something to refer to): returns up to 100 records after `since`
in order, each with its `event` only if it concerns the caller (`scope_tag` is the tag of its workspace), the hub's `head`
`{seq, hash}`, and the root-signed `op_certs` the page's records name (reply `{"records", "head", "op_certs"}`). **`audit_ack {seq, head}`**: the workspace says "I verified the chain up to `seq`, whose chain hash is
`head`". The hub checks `head` against its own record `seq` (`seq` beyond its chain, or another hash: `audit_fork`: the
workspace holds a chain the hub did not sign there) and stores the highest acked `seq` per workspace; an older ack
changes nothing. Reply `{"acked": seq}`. Gaps are found by the workspace (`audit_gap`), forks by either side
(`audit_fork`), and a workspace that cares compares its head with the hub's. **SHORTCUT** `Audit acks recorded, not
enforced` (L7): the hub stores acks and never blocks, alerts or truncates on a missing, stale or forked one; a fork is
visible in the vectors' checks and in `orch-hub inspect`, not acted on.

Vectors: `audit_*` (genesis, next, gap, forks, rewritten first record, scope and event mismatches, wrong signer and
domain) and `audit_ack_*`.

### 10.6 Hub admin read at orch-publish (phase 3, F3)

Lets a hub admin read **metadata** about releases of workspaces bound to the hub, through orch-publish, without
holding any workspace key. Read-only; never content or secrets.

```
signed bytes = "orch/v2/hub-admin-read|" || suite (1) || hub_id (16) || u64(ts_ms) || lp16(METHOD) || lp16(path + query)
headers      = Orch-Hub: <hub_id hex>, Orch-Hub-Ts: <ts_ms decimal>, Orch-Hub-Signature: b64u(Sign(HK_op, bytes))
```

`METHOD` is `GET` or `HEAD`; `path + query` is the request target exactly as sent (raw query, ASCII), so a swapped path,
query or method does not verify. The label keeps a publish transport signature (§11) from being read as an admin read
(vector `admin_read_publish_signature_replayed`). **What orch-publish checks, in order:**

| # | Check | Refusal |
|---|---|---|
| 1 | header syntax (`Orch-Hub` hex16, `Orch-Hub-Ts` a decimal without leading zeros, `Orch-Hub-Signature` 64 bytes b64u), method `GET` or `HEAD`; **the target is canonical** (below) | 400 `malformed` |
| 2 | `\|now − ts\| ≤ 120 s` | 401 `stale_timestamp` (`server_ms`) |
| 3 | the **held, current** hub card for `Orch-Hub`, **verified again for this request**: its op certificate valid, not expired, not revoked, `hub_id` of the root, dev marker | 401 `unknown_hub` |
| 4 | `Orch-Hub-Signature` under that card's `HK_op` over the bytes above | 401 `bad_hub_signature` |
| 5 | scope: only workspaces with a **current binding to this hub in `mode: "full"`** (status `bound`, `hub_id` and `hk_root_pub` of the held card) whose `wsk_pub` equals the workspace card's current `WSK`; the workspace the target names (the `workspace=` key), if any, must be in that set, **and so must the workspace of every row the data layer returns** (a row outside it is never emitted) | 403 `not_bound` |
| 6 | data | — |

**The route and canonical targets (L2P-5).** The request is orch-publish's route (F3) `GET|HEAD /v2/hub/releases` with the
optional query `?workspace=<id>`. The target must be exactly that, in canonical form, or it is `malformed`: ASCII, no `%`
(no percent-encoding at all), the path byte for byte `/v2/hub/releases` (so no `//`, `.`, `..`, trailing `/` or upper case)
and either no query or the single key `workspace` with 32 lower-case hex digits (no other key, no repeat, no `#`, `+`,
`;`). The scope applies to the workspace the target names (if any), after routing, and to every row returned
(`check_admin_rows`), so a router that normalises or decodes the target differently cannot reach a workspace outside the
scope (vectors `admin_read_target_*`). Without `workspace=` the data layer returns rows of the in-scope workspaces only.

**No nonce store, by design:** the request is idempotent metadata, so a replay inside the 120 s window returns what the
original did, and the hub's key is online anyway. (If a state-changing admin route is ever added, it needs its own
contract.) **Selective workspaces are out of scope (L2P-6).** D71: the hub-admin has no role in a selective workspace, whose
card says the hub sees only what it shares; release metadata, including releases made on the direct v2 path that the
hub never forwarded, stays hidden from it. Only a later, explicit owner opt-in could add them (not specified).

**Response rows carry exactly** `workspace_id`, `release_id`, `kind`, `state`, `created_ms`, `expires_ms`
(or `null`), `revoked`, `bytes`, `public_url`, which **MUST be `null` in `hv: 0`** (orch-publish has no public kind; a non-null value is a violation, vector `admin_read_row_with_public_url`). A later `hv` may add it for live public releases only, never for a URL that carries a token or grants access; any other field is a
violation (`check_admin_read_row`, vectors `admin_read_row_*`).

### 10.7 The hub's workspace view: `log_sync` (phase 4, V3-D23)

**Why.** The hub builds custodial events "from its own `WK_e` view of the workspace" (spec §5): the log head for
`based_on`, the member-list version, the gate generation, hash material and policy. H0 had no op that gave it that
view. `log_sync` is that op: the host sends its own log lines, sealed under a key only `WK_e` holders derive; the hub
verifies every line under the pinned `WSK` before it keeps it. Full mode only (`wrong_mode` otherwise): a selective hub
holds no `WK_e` and builds nothing (D80; selective custodial signing over shared items is C6, not here).

**The page.** Body `{"epoch", "log", "from_seq", "count", "head", "reset", "sealed"}`: `log` is `"workspace"` or a
ticket ULID; the lines `from_seq … from_seq + count − 1` (1 ≤ `count` ≤ 256) of that log, each **exactly** the
logged event (ticket-format §5.1, with `seq`, `prev`, `ws_seq` and `host_sig`); `head` = the event head (ticket-format
§5.5, `"sha256:" + hex(H("orch/v2/event|" || cj(event)))`) of the last line; `reset` (below).

```
K_view(e) = HKDF(WK_e, salt = "", info = "orch/v2/hub-view|" || hex(workspace_id) || "|" || dec(e))
AAD       = "orch/v2/hub-view-aad|" || suite || workspace_id (16) || u32(epoch) || lp16(log) || u64(from_seq)
            || u32(count) || head (32 raw bytes) || u8(reset)
sealed    = SALTED-AEAD(K_view(epoch), "orch/v2/hub-view-msg", AAD, cj({"lines": [event, …]}))     (v2 §5.1)
```

Sealed although the link is TLS (DECIDED HERE): the hub stores the pages sealed and opens them with `WK_e` only inside a
build (D6's `WorkspaceView.build(wk_e, …)`), so its database alone reads nothing; and a host that rotated the hub out
(leave, full → selective) cannot leak later content to it by mistake: the hub has no wrap for the new epoch
(`bad_epoch`). At most 1 MiB sealed per page.

**The hub's check, in order** (reference `apply_view_page`, vectors `log_sync_*`):

1. the body's exact fields and types, `log` grammar, `1 ≤ count ≤ 256`, `head` a hash, `reset` only with `from_seq =
   1` → `malformed`;
2. `epoch` is the newest epoch the hub holds a `wk_seal` wrap for → else `bad_epoch` (the host sends `wk_seal` first);
3. position: `from_seq ≤ held + 1` for that log (with `reset`, held = 0) → else `view_gap`;
4. size → `too_large`; the seal opens under `K_view(epoch)` with that AAD → else `bad_wrap` (a page moved to another
   log, range, workspace or epoch, or a lying `head`); the plaintext is exactly `{"lines": [...]}` with `count` lines
   → `malformed`;
5. per line, in order: `seq = from_seq + i` and `host_sig` present → `malformed`; `host_sig` under the **pinned**
   `WSK` (§9.5) over `"orch/v2/sig/host-event|" || cj({contract: 1, suite: 2, workspace_id, log, event without
   host_sig})` → `bad_signature`; `prev` is the head of the line before (`null` at `seq` 1), and a line at a `seq`
   the hub already holds has the same head → else `view_fork`; a ticket line's `ws_seq` is an integer ≤ the
   workspace-log `seq` the hub holds → else `view_gap` (sync the workspace log first);
6. the last head equals `head` → `malformed`; then the view of that log is the held one extended (an overlap that
   matched changes nothing: resends are idempotent). Reply `{"seq", "head"}` of the hub's view after the page.

**What the hub trusts, and what it does not.** The hub verifies that every line is the host's (`WSK`) and that the
lines form one chain; it does not replay F1 authorization (devices, grants) over them. That is enough because of the
two rules that bound a hostile host (D29, spec §8a: it can mislead only within its own workspace): (a) the view only
fills fields of an event a **channel** asked for (§8.4: the action names the type and the person's choices; `log_sync`
cannot start a signature, choose a type or pick a user), and the person sees the built event before the proof; (b)
every custodial signature is bound to this `workspace_id`, log and type (§8.2), so nothing built from this view
verifies anywhere else, and the host re-checks the event against its own log (§8.3 step 9). A host that feeds a false
view only gets events about its own workspace refused by itself, or shown to and signed by a person who sees them.

**`reset`.** A host `restore` (ticket-format §5.10) abandons a suffix, so the honest log forks from the hub's view. The
host then resends the log from `seq` 1 with `reset: true`, which replaces the hub's view of that log. **SHORTCUT**
`log_sync reset on the host's word`: the hub does not check that the log it held was abandoned by a valid `restore`;
hardening ties `reset` to the `restore` event (its `abandoned` head must be on the hub's held chain) and audits it.
**A reset of the workspace log drops every ticket view** (L2P4-9): held ticket lines may name a `ws_seq` beyond the
shorter new workspace log; the host resends the ticket logs.

**Visibility (L2-D15-1).** One rule for needs-you and for every build, the same as the host's (orch-core
`model/visibility.can_see`) minus the membership test (covered by the hub role and the snapshot gates): a ticket whose
`visibility` is `"workspace"` is visible to every member; a `{"restricted": [...]}` ticket only to a person on that
list; any other value is invisible. Who owns the ticket, assigns or reviews it, or what role the user has does not
count: an owner who restricts a ticket without its ticket owner hides it from that owner too. The persons a hub user
answers to are their `u_` id and, when the hub aliased the workspace owner to them, the owner's person id (both
canonical, §9.1). A ticket that is not visible reads as `unknown_item` in a build and as nothing in needs-you. Vectors
`visible_*` (check `visible`, args `{visibility, me}`) are run by hub and host alike.

**Size cap.** At most 64 MiB sealed per workspace on the hub; a workspace whose log exceeds it answers `too_large` on
every page, and a `reset` refills to the same cap, so the host must keep its synced logs under it (hardening: a
rate-limited, cached projection and a global cap).

**Not here:** the hub's projection code (F1 replay on the view) is the hub's; body text and artifact bytes are not
synced in hv 0 (custodial `ticket.updated` is `set` only, §8.5), so the channel shows section hashes, not text, for
anything bound to them.

### 10.8 Hub → relay requests (phase 4, E1)

**Purpose.** The relay's accounts are hubs (D76). Registration (`POST /v2/hubs`) and revocations are self-authenticating
objects (the card through `HK_root`, `hub.op_revoked`), like orch-publish's pushes (V3-D16), and need no transport
signature. Every **other** request a hub makes to its relay account (push to a channel, hub↔hub mailbox, account
settings) is signed by `HK_op`:

```
signed bytes = "orch/v2/hub-relay-req|" || suite (1) || hub_id (16) || op_cert_id (16) || u64(ts_ms) || nonce (16)
               || lp16(METHOD) || lp16(target) || H(body)
headers      = Orch-Hub: <hub_id hex>, Orch-Hub-Op-Cert: <op_cert_id hex>, Orch-Hub-Ts: <ts_ms decimal>,
               Orch-Hub-Nonce: <32 hex>, Orch-Hub-Signature: b64u(Sign(HK_op, signed bytes))
```

**From E1's proposal (DECIDED HERE):** E1 proposed an object `hub.relay_request {hub_id, op_cert_id, ts_ms, nonce,
method, target, body_sha256, dev}` signed as `cj(o)`. The label is kept; the bytes follow §10.6/§11.1 instead, so the
relay **rebuilds** them from the request it actually received and has no "verify, then compare each field" step to
forget (a swapped target or body is `bad_hub_signature`). `dev` is not in the bytes: the relay's dev check runs on the
held card at every use (step 3), which covers it. `METHOD` is one of `GET`, `HEAD`, `POST`, `PUT`, `DELETE`.

**What the relay checks, in order** (reference `check_relay_request`, vectors `relay_request_*`):

| # | Check | Refusal |
|---|---|---|
| 1 | header syntax (`Orch-Hub`, `Orch-Hub-Op-Cert` hex16, `Orch-Hub-Ts` decimal without leading zeros, `Orch-Hub-Nonce` 16 bytes hex, `Orch-Hub-Signature` 64 bytes b64u), the method; **the target is canonical** (below) | 400 `malformed` |
| 1b | the target's `{hub_id}` segment is `Orch-Hub` | 403 `other_hub` |
| 2 | `\|now − ts\| ≤ 120 s` | 401 `stale_timestamp` (`server_ms`) |
| 3 | the held, current card of `Orch-Hub`, **verified again for this request**: op cert valid, not expired, not revoked (the relay's revocation store, E1), `hub_id` of the root, dev marker | 401 `unknown_hub` |
| 4 | `Orch-Hub-Op-Cert` is that card's op cert | 401 `stale_op_cert` |
| 5 | `Orch-Hub-Signature` under that card's `HK_op` | 401 `bad_hub_signature` |
| 6 | the nonce is unseen for this hub; then recorded for 240 s | 401 `replay` |
| 7 | the route | — |

Every signature comes before the nonce store. **A nonce store, unlike §10.6:** these requests change state (a push
sent, a message queued), so a replay inside the window must not repeat them.

**Canonical target.** ASCII, at most 2048 bytes; the path is `/v2/hubs/<hub_id>` or below it (32 lower-case hex for
`hub_id`), every segment non-empty and made of unreserved characters `[A-Za-z0-9._~-]` only, never `.` or `..` (so no
`%`, `+`, `#`, `;`, `//` or trailing `/`); an optional query of `&`-separated `key=value` pairs of unreserved characters
with unique keys; a target ending in a bare `?` (or an empty pair) is `malformed`. Anything else is `malformed`; a
target for another hub's account is `other_hub`. The route table
under `/v2/hubs/{hub_id}/` is §10.8.1; every route **below** it needs this signature. **Unsigned
exceptions:** the self-authenticating registration `POST /v2/hubs`, the revocation route, and the **public card read
`GET /v2/hubs/{hub_id}`**, which stays unsigned although it sits under the signed prefix (it returns only the public,
root-signed card and the revoked flag).

**HTTP status per path** (E1): on registration (`POST /v2/hubs`) and the public card read, a card that does not verify
or is unknown is `400 malformed` / `404 unknown_hub`, and a lower op cert is `409 stale_op_cert` (§5 step 7, a state
conflict); on a **signed request** (the table above) `unknown_hub` and `stale_op_cert` are `401` (authentication of the
request failed).

### 10.8.1 The relay routes of a hub account (phase 4, D10 / relay E2)

**Purpose.** What a hub does at its relay account: register it, own the push target of each of its channels, send a push,
and leave a message for a channel. The routes mirror the per-workspace push routes of v2 §19.10 (same APNs
configuration, tokens sealed at rest, same `outer` size cap), with the hub as the account and `channel_id` where v2 has a
device. Reference `orch_hub.contract.relayroutes.route` (vectors `relay_route_*`, section `phase4.relay_routes`); the relay
implements exactly this (relay task E2).

Below `/v2/hubs/{hub_id}/` every request is signed by `HK_op` and checked in the order of the §10.8 table **before** the
route and its body are looked at. `{ch}` and `{id}` are 32 lower-case hex. Bodies are `cj` objects with exactly the listed
members and `"v": 2` (an extra or missing member, another `v`, a non-canonical value is `400 malformed`); `GET` and `DELETE`
have an **empty body** (`H("")` is signed). No route takes a query (`400 malformed`). **Order of refusals** (the reference
`route()`, vectors `relay_route_*`; the relay answers the first that applies): (1) the §10.8 target checks, and a query
(`400 malformed`); (2) under `mailbox/` with one or two further segments, a `{ch}` that is not 32 lower-case hex is `400 malformed`, whatever the method;
(3) a path shape or method not in the table is `404 not_found`: this includes any other first segment or segment count,
`mailbox/{ch}/{id}` with an `{id}` that is not 32 hex, and, under `push/`, a bad `{ch}` with a method or shape the table
does not have; (4) the body, then (under `push/`, for a path and method that are in the table) `{ch}`: `400 malformed`
(`413 too_large` for an oversize `outer` or mailbox message, found only when `{ch}` is valid). A relay without a complete push configuration answers `404 not_found` on every route
of this subsection, as for v2 push (after authentication).

| Route | Signed | Body | Answer |
|---|---|---|---|
| `POST /v2/hubs` | no (the card) | the hub card (§5) | `200 {hub_id, op_cert_id, op_seq, card_seq, created}`; `403 not_allowed` (operator allowlist), `429 quota_exceeded`; the rest per §10.8 |
| `PUT /v2/hubs/{hub}/push/{ch}` | yes | `{v, env, topic, token}` | `200 {channel_id, registered: true}` |
| `DELETE /v2/hubs/{hub}/push/{ch}` | yes | empty | `200 {channel_id, registered: false}` (also when none was held) |
| `POST /v2/hubs/{hub}/push/{ch}/send` | yes | `{v, outer, priority, ttl_s}` | `200 {accepted: 0 or 1}` |
| `PUT /v2/hubs/{hub}/mailbox/{ch}/{id}` | yes | `{v, c, ttl_s}` | `200 {id, stored: true, expires_ms}` |
| `GET /v2/hubs/{hub}/mailbox/{ch}/{id}` | yes | empty | `200 {id, c, expires_ms}`; `404 not_found` |
| `DELETE /v2/hubs/{hub}/mailbox/{ch}/{id}` | yes | empty | `200 {id, deleted: true}` (also when absent) |
| `DELETE /v2/hubs/{hub}/mailbox/{ch}` | yes | empty | `200 {channel_id, deleted: n}` (all messages of the channel) |

**Push target** (`PUT .../push/{ch}`). `env` is `sandbox` or `production`; `topic` is the relay's configured APNs topic
(`[A-Za-z0-9.-]{1,155}`, anything else is `malformed`; a topic or env the relay is not configured for is `malformed`, as
v2); `token` is the b64u APNs device token, 32 to 100 bytes. **One target per `(hub, channel)`**: a `PUT` replaces the held
one (a phone that gets a new token re-registers), an exact repeat is a no-op. The relay stores the token sealed (v2
§19.10) and never returns it. The relay does not know the channel's key or certificate: *which* channels exist and are
live is the hub's business (§6.2, §7); the relay holds at most `max_push_targets_per_hub` targets per hub (`429 quota_exceeded`). That is a relay
configuration value, **default 4096**; the hub does not rely on a number. The per-channel mailbox caps below stay fixed.

**Send** (`POST .../push/{ch}/send`). `priority` is `high` or `normal`, `ttl_s` an integer 0 to 86 400. `outer` is exactly
`{v: 2, hub, ch, id, c}` with `hub` and `ch` equal to the path's, `id` 32 hex (the hub's message id), `c` canonical b64u
(the seal of §10.8.2); `len(cj(outer)) ≤ 3072` else `413 too_large`. The relay puts `cj(outer)` into the APNs payload as
v2 §19.10 does and sends nothing else derived from content. `accepted` is `1` when a send was queued for the channel's
target, `0` when there is no target, the same `(hub, ch, id)` was already accepted in the last 24 h (**a retry with the same
`id` reaches the phone once**, which is what makes the hub's retry safe), or the channel is over its send budget; none of
these is an error. `429 quota_exceeded` / `503 overloaded` carry `Retry-After`. Budgets are per hub and per channel and are
the relay's (the hub does not rely on a number).

**Mailbox** (`PUT .../mailbox/{ch}/{id}`). `c` is the b64u of a sealed message, 1 to 16 384 bytes decoded (`413 too_large`
above), `ttl_s` 60 to 604 800. The relay holds the bytes and cannot read them. **Idempotent by `id`:** the same `c` under
the same `id` again is `200` and changes nothing (the expiry stays); another `c` under a held `id` is
`409 message_conflict`. At most 256 held messages and 4 MiB per `(hub, channel)` (`429 quota_exceeded`). An expired message
is gone (`404 not_found`). **Open point O1 (not decided here):** the *channel's* own read of its mailbox (the phone fetching
what the hub left) is a channel-authenticated request that belongs to the orch-mobile contract; nothing in this subsection
gives a channel, or anyone but the signing hub, a read. Until it exists the mailbox is written and read by the hub only,
and a push carries what the phone needs.

**Revocation.** When the hub revokes a channel (§6.3, §7) it sends `DELETE .../push/{ch}` and
`DELETE .../mailbox/{ch}` for that channel. Both are idempotent, so the hub repeats them until the relay answers `200`.
The hub also stops sending to the channel at once (it asks its own revocation list, not the relay).

**Retry rules for the hub.** Every attempt is a new request: new `ts_ms`, new nonce (a replay is `401 replay`). It retries
on a network error, `429`, `503` and `5xx` with a bounded backoff that honours `Retry-After`, and on
`401 stale_timestamp` once, after correcting its clock by `server_ms`; every other `4xx` is final. All routes are
idempotent (`PUT`/`DELETE` by state, `send` and mailbox `PUT` by `id`).

### 10.8.2 The sealed push payload

**Purpose.** The relay and APNs see ciphertext only; the phone's notification service extension opens the payload with the
channel key. Construction: v2 §5.3 unchanged (`orch/v2/seal|`, `orch/v2/seal-aad|`) with

| Part | Value |
|---|---|
| purpose | `hub-push` |
| recipient | the channel key `key_pub` of the channel's certificate (§6.2); recipient id = `channel_id` |
| object id | the message id `id` (16 bytes, the same as in `outer` and the mailbox path) |
| epoch | `0` |
| extra | `hub_id` (16 bytes) |
| plaintext | `cj({v: 2, hv: 0, kind: "hub.push", type, ts_ms, ref})` |

`sealed = b64u(eph_pub || AES-256-GCM(K, 0-nonce, plaintext, AAD))` with a fresh ephemeral key per seal (vector
`phase4.relay_routes.push_seal`; `open_push` is the reference for the extension). `type` is one of `prepare` (a custodial
prepare is waiting for this user, §8.4) and `needs_you` (a change the user has to look at); `ref` is a 32-hex opaque id
(the prepared id or ticket uid, so the app can open the right item) or `null`. **The payload is a hint, not an authority:**
the seal is anonymous (anyone who knows a channel's public key can make one), so the extension shows generic text at most
and the app acts only on what it fetches from the hub over its authenticated channel, never on this payload. The `id`,
`ch` and `hub` in `outer` are not secret; the `type` and `ref` are.

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
| orch-relay (phase 4) | hub cards, bindings, hub requests (§10.8) | hub accounts by card, op revocations, hub request nonces |
| orch hub | the v2 card and binding at join (§9.5), link auth, w2h link messages, bindings it forwards, channel registrations (§6.3), actions (§8.5), every `log_sync` line (§10.7), channel proofs before it signs | its keys, users, channels, snapshots, audit, the `workspace_id → (owner, WSK)` pins, the sealed views |
| channel (phone, browser; p4) | the prepared context against its digest (§8.4) | its channel key |

## 13. Threat notes for the reviewer

- A **rogue hub** without the workspace's `WSK` can make no binding (§9.2 step 2, vectors
  `binding_signed_by_other_wsk`, `publish_other_hub_header`), so it cannot claim a publish namespace (spec §9).
- A **hostile host** cannot make the hub sign: the link has no signing operation (vector `link_sign_request_refused`),
  and custodial events are built by the hub from its own view (spec §5). It can send `publish_out` for its own
  workspace only (the link is bound to one `workspace_id` and one `WSK`).
- A **compromised hub** can sign as any user in full mode (accepted, spec §9) but cannot forge a channel proof
  (vector `custodial_proof_not_by_channel`) without a channel key it does not hold; it can drop or delay events. It
  cannot revive a binding after leave (§9.4) and cannot unbind a workspace (only `WSK` can).
- **The audit chain, honestly (L7 gap).** A workspace detects a **gap** (a record removed from what it is shown) and a
  **fork** on its own path, and nobody can alter a record without breaking every later `prev`. What is **not** true
  until an off-hub witness exists (L7): that a compromised hub "cannot silently drop entries". It can show workspace A
  one chain and workspace B another (**equivocation**); each verifies consistently, and the acks go to the hub itself,
  so nothing in this contract detects it. What the acks give today: an honest hub holds `WSK`-signed link messages
  saying "W verified up to `seq`/`head`" (evidence against a later rewrite, if someone outside the hub ever looks);
  against a compromised hub, nothing. The L7 proposal: the hub posts `HK_op`-signed checkpoints `{seq, head}` to
  orch-publish (append-only per `hub_id`; two heads at one `seq` are a transferable fork proof) and hosts compare. The
  "anchor head" a workspace starts from (taken at join and stored) is trust-on-first-use for the prefix. The hub also
  chooses a record's `scope_tag` (it can file a record about A under another tag so A never sees the event); hosts
  should cross-check that every custodial event and every `hub.space` epoch they accepted has a matching record within
  the next sync (phase 4). The spec sentence in orch-v2-hub.md §10 ("a hash-chained audit acknowledged by the
  workspaces" as mitigation of a taken-over hub) overstates this and is to be amended to match.
- **Phase 4 (V3-D23).** A **hostile host** can feed the hub any view of its own workspace (`log_sync` lines carry
  its own `WSK` signatures); it cannot start a signature, choose a type or a user, or move a signature out of its
  workspace (§10.7). A **compromised hub** still signs as any user of a full workspace (spec §9) but never without a
  fresh channel proof over a digest the channel recomputed from what it showed (§8.4). A **leaked pairing code** plus a
  user's sign-in registers a software key as `se-unlocked` (SHORTCUT, §6.3). The relay acts only on a request signed by
  the current `HK_op` of the account it targets, once (§10.8).
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
| `bad_wrap` | `wk_seal` reply | the wrap does not open under `HXK`, is not bound to this `hub_id`, this workspace and the stated epoch, or is another wrap for an epoch already sealed (§10.2a) |
| `bad_epoch` | `wk_seal` reply | v2 code reused: the epoch is lower than the newest one held for the live binding (§10.2a) |
| `auth_failed` | link open | §10.1: every failure before the link is authenticated (unknown workspace, key mismatch, bad signature) returns this one code, so an unauthenticated peer learns nothing; the reason stays in the hub's log |
| `not_bound` | link reply | any op after `hub.unbound` except `join` and `binding_put` (§10.2 reply rules); the same code as the publish refusal of §11.2 |
| `not_allowed`, `queue_full`, `too_large` | `route_out` reply | §10.3; `too_large` is the v2 code, reused |
| `audit_gap`, `audit_fork`, `audit_mismatch` | audit chain | §10.5 |
| `not_member`, `not_accepted`, `unknown_item`, `space_conflict`, `bad_wrap`, `commit_mismatch` | shared spaces | §10.4. `not_member` is also the answer for a space that does not exist. Reused there: `stale_epoch`, `replay`, `too_large`, `other_workspace`, `bad_wxk_version` |
| `members_changed` | host, local | §10.4: the member set grew since the owner approved it; never sent on the link |
| `view_gap`, `view_fork` (p4) | `log_sync` reply | §10.7: a page that leaves a hole (or a ticket line ahead of the workspace log the hub holds); a line that contradicts the chain the hub holds. Reused there: `bad_wrap`, `bad_epoch`, `too_large`, `wrong_mode` |
| `not_custodial` (p4) | action, custodial | §8.5: the type is not in the custodial table for that log |
| `event_changed` (p4) | channel, hub `sign` | §8.4: the digest does not match the shown context, or the hub's view moved since `prepare` |
| `stale_version`, `too_many_items` (p4) | selective `share_put` | §10.2b: an equal or lower `item_version` (other bytes, or a revoked one); over 1000 live items. Reused there: `bad_wrap`, `too_large`, `other_hub`, `other_workspace` |
| `no_session`, `key_in_use` (p4) | channel registration | §6.3: the challenge was not opened inside a live session of the user; the key is registered to another user. Reused: `channel_revoked` (a revoked key never registers again) |
| `unknown_channel` (p4) | hub CLI/API, local | §6.3: a revocation of a channel id this hub never registered |
| `challenge_invalid` (p4) | channel registration | §6.3: not the challenge the hub issued, or expired. Reused there: `factor_mismatch` (the attestation is not the gate the hub checked), `other_user`, `other_hub` |
| `bad_invite`, `unavailable`, `internal` | link reply | `join` with an unknown, used or expired invite; an op whose backend (e.g. publish forwarder) is not available; an unexpected hub-side failure (no detail) |
| `not_bound` (403), `unknown_hub` (401), `bad_hub_signature` (401) | publish, admin read, relay request | §11.2, §10.6, §10.8 (with `stale_op_cert`, `replay`, `other_hub`). Statuses are per path: on relay registration and the public card read `unknown_hub` is 404 and `stale_op_cert` 409 (§10.8) |
| `not_found` (404), `message_conflict` (409) (p4) | relay routes | §10.8.1: an unknown route, method or message; another body under a held mailbox `id`. Reused there: `malformed`, `too_large` (413), `quota_exceeded` (429), `overloaded` (503), `not_allowed` (403, registration) |
| `approval_required`, `bad_approval` (403) | publish | §11.3 |

Hub web API (local, `/api/v1`, V3-D13; answers are `{"error": code}`; none of these travel on the link):

| Code | Status | Meaning |
|---|---|---|
| `bad_credentials` | 401 | sign-in: user id, password or TOTP wrong (one code for all, and for an unknown user) |
| `rate_limited` | 429 | sign-in (per user and source, per source, all sources, or all hash slots busy), prepare, sign |
| `unknown_prepared` | 404 | `prepared_id` unknown, used, or another user's (same answer) |
| `prepared_expired` | 410 | the prepared request is past `expires_ms` |
| `too_many_channels` | 429 | the user holds the maximum of active channels |
| `weak_password` | 400 | password policy (admin set-up) |
| `unauthorized` | 403 | a pairing or credential action by someone other than the user |
| `unknown_user`, `unknown_group` | 404 | admin routes naming a user or group that does not exist |
| `exists`, `channel_exists`, `reserved_group` | 409, 409, 400 | a user or group name taken; a channel key already registered; a built-in group name |
| `bad_pairing_code` | 400 | phone registration: wrong, used or expired pairing code |

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
| `audit`, `admin_read` | V3-D4 / F3: a 4-record chain with its events and hashes; the admin read signed bytes, a request and a row |
| `spaces` | V3-D8: the space key per epoch, `sk_commit`, one sealed item (salt, derived key, AAD, plaintext object) and one wrap (ephemeral seed, AAD) |
| `phase4` | p4: the `log_sync` lines, heads, view key, AAD and one sealed page; a prepared context and digest; a relay request and its bytes; the relay routes' request bytes and one sealed push payload (`relay_routes`); the custodial type table; the `space.accept` built from the vector proposal |
| `cases` | 609 cases (127 positive, 482 negative): `check`, `args`, `expect` (`{"ok": …}` or `{"error": code}`), `phase` |

The negative cases cover, per object where it applies: wrong domain, wrong signer, expired, binding for another
workspace, unbound after leave, missing dev marker, dev at a real verifier, extra or unknown field, non-canonical
encoding (b64u padding, upper-case hex, duplicate key, float), wrong suite, future `hv`, replays and stale versions.

Every implementation runs the cases of the checks it implements: the hub all of them; orch-core `card`, `op_*`,
`snapshot`, `uk_cert`, `channel_cert`, `custodial`, `joined_payload`, `link` (h2w), and from phase 4 `space_proposal`
and the host side of `log_sync` (it builds the pages the vectors open); orch-publish `binding_store`,
`publish`, `card`, `v2_card`; orch-relay (p4) `card`, `op_*`, `relay_request`, `relay_route`; orch-mobile and the browser home (p4)
`prepared`, `channel_register` (the object they sign). The hub also runs `hub_join`. Each case carries its `phase`; the L2 cases name their finding in `note`. The cross-repo contract job (V3-D8) runs from phase 2.

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

| 19 | Route subject (V3-D19) | a ws→ws route A→B is authorized as A's owner: the hub user aliased to A's owner at join (`--owner-user-id`, stored with the pin/binding), needing role ≥ `edit` on B via D3's `Access`; no alias → `not_allowed` | orchestrator, 22 Oct |
| 20 | Shared-space ops | `space_list` is part of the op list; the seal purpose `space-sk` is added to v2 §5.3 | orchestrator, 22 Oct |
| 21 | Unbind and spaces | `hub.unbound` removes the workspace from every space in the same step (§10.4) | orchestrator, 22 Oct |
| 22 | Audit | chain over event hashes (workspaces verify all, read their own events); `audit_get` added; acks recorded not enforced | D4, L7 |
| 23 | Admin read | F3's layout (`lp16`, raw query signed), no nonce store, held card re-verified per request | orchestrator, 22 Oct |
| 24 | Workspace view for custodial events (gap 1) | `log_sync` w2h, full mode: exact log lines sealed under `K_view(WK_e, epoch)` with AAD over ws/epoch/log/range/head/reset; every line's `host_sig` under the pinned `WSK` and the event chain verified; gap/fork codes; reset on restore is a SHORTCUT | spec §5 "own WK_e view", D29 |
| 25 | Custodial types and roles (gap 2) | closed `(type, log) → lowest role` table (§8.5), checked by hub and host (step 7b); `read` signs nothing; policy/settings not channel-startable in V3, people/visibility admin-only; actions per type with exact nested schemas (no free-form object), never event bytes; decisions carry the reviewed hash | D70, D72, F1 §5.4.2 |
| 26 | Registration proof (gap 3) | dedicated label `orch/v2/hub-channel-reg\|` over `hub.channel_register` (not D7's reuse of the proof label) | spec §5 one domain per object |
| 27 | Channel audit (gap 4) | audit type `channel` `{channel_id, user_id, change, factor, actor_user_id, nonce}`, hub-wide; revocation permanent | D4, L2P-4 nonce rule |
| 28 | Phone attestation (gap 5) | dev: `se-unlocked` only through an operator pairing code (gate-derived factor); hardening: App Attest under its own value | V3-D23 |
| 29 | Space proposal (gap 6) | `hub.space_proposal` signed by `HK_op`, served by `space_proposals`; the host builds `space.accept` from it | #1020, L2P-2 |
| 30 | Hub → relay auth (gap 7) | E1's label `orch/v2/hub-relay-req\|`, bytes in the §10.6 style plus `op_cert_id` and a nonce store | E1 (orch-relay#85), §10.6 |
| 31 | Prepare / WYSIWYS (gaps 1–2) | the channel recomputes the digest from the context it shows (`event_changed`) | L2-5, D81 |
| 32 | Selective `share_put` content (C6, D9) | `share.item` WSK-signed with exact content per kind, `IK` sealed to `HXK` (new v2 §5.3 purpose `hub-share`), monotonic `item_version`, idempotent revoke with tombstone, 16 KiB / 1000 items | C6 orch-core#1022, D9 orch-hub#23, spec §8a |
| 33 | Text fields | one rule: no `Cc`, `Cf`, `Zl`, `Zp`; LF only in multi-line texts | security review (display spoofing) |
| 35 | Opus pass on orch-hub#22 (l2-review-p4, PASS, 0 P0) | P1s fixed in one follow-up: L2P4-1 selective role floor; L2P4-2 hash pinned on the 4 decision types, `check_built` always, view text escaped, content-bound decisions show the short hash; L2P4-3 text rule adds `Cs`, `Co`, `Cn`, Default_Ignorable, Unicode 16.0 pinned; L2P4-6 revoked key never registers (`channel_revoked`), `key_in_use`; D7: `key_pub` in the challenge, pairing code needs a live session (`no_session`), revoke of an unknown id refused (`unknown_channel`), no `audit_failed`; D9: `share` `purge`, revoke record pinned; L2P4-9 (P2) workspace reset drops ticket views | state/l2-review-p4.md |
| 34 | New audit types | `channel` and `share` are additive `type` values of `hub.audit` in `hv: 0`: **orch-core's audit verifier must accept them** (its type enum and event field table), as the hub does | orchestrator lean, adopted |

**SHORTCUTS added by the phase-4 extension** (for the #838 table): `Phone key attestation by operator pairing code (no
App Attest)` (§6.3, D7 → hardening: App Attest); `log_sync reset on the host's word` (§10.7, D6/C-side → hardening: tie
to the `restore` event); `Custodial ticket.updated without body sections; no artifact custody` (§8.5 → a text/blob
transport in the action).

**Hardening from the phase-4 Opus pass (P2, not built; each a `hardening` row):**

| Item | Where | Hardening |
|---|---|---|
| L2P4-4 core titles with refused characters make a share fail | C6 host | the host escapes or skips, never sends; test `share_item_from_bidi_title_is_escaped_by_host` |
| L2P4-10 value grammars differ from core (`_TOKEN`, `_KEY`, `_PERSON`) | §8.5 | align with core's `token`, `ticketKey`, `personId` (liveness: the hub signs what the host refuses) |
| L2P4-11 relay origin not in the §10.8 bytes | §10.8 | add `lp16(relay_origin)` (a request to relay A replayable to relay B within 120 s) |
| L2P4-12 duplicate `Orch-Hub*` headers | §10.8, §10.6 | exactly one of each, else `malformed` |
| L2P4-13 proposal vs held space | §10.4 | at `epoch == held + 1`, `prev_members == held.members` |
| L2P4-14 `log_sync reset` on the host's word | §10.7 | SHORTCUT above: tie to the `restore` event, audit it |
| L2P4-C2 core actor lookups not through `canonical` | C3 | route every actor lookup through `identity.canonical` plus the role mapping |
| L2P4-C1 aliases not scoped to a hub (P1 for core) | §9.1, C4 | key aliases by `(hub_id, user_id)`; "never re-pointed **for the same hub**"; must land before C3 accepts `auth: "hub"` |
| D9 tombstones unbounded | §10.2b | per-workspace monotonic `item_version` counter + prunable tombstones with a `version_floor`; until then a hard cap (`too_many_items`) |
| D9 `hxk_exchange` raw ECDH oracle | hub keys | purpose-bound `open_seal(purpose ∈ {wk, hub-share}, …)`; the share service gets a `hub-share`-only handle |

**Open points of the phase-4 extension (not decided here):**

- **Agent grants in a full-mode hub workspace.** `grant.issued` carries a `secret_hash` of a secret the host makes and
  hands to the agent; the hub cannot build it from its view, so it is not custodial (§8.5). With D78/D81 (no local
  human signing in full mode after H5) nobody could then start an agent there. Options: (a) the action carries the
  `secret_hash` from the host via the channel (the host shows the person a one-time handle), (b) the host makes the
  secret and the channel only signs; (c) keep owner-local signing for `grant.*` as a fourth break-glass power. Needs an
  orchestrator decision before C10/H5.
- **Policy and settings changes in a full-mode hub workspace.** Not channel-startable in V3 (§8.5); with no local
  human signing in full mode after H5 (D78/D81), they then need either an admin-only custodial type with an exact
  policy/settings schema and a full review screen, or a break-glass power. Decide together with agent grants.
- **Selective custodial signing (C6).** `log_sync` is full mode only. A selective workspace's custodial events over
  shared items (spec §8a "a custodial digest binds the item hash") need the item format of C6; not specified here.

**Open / decided after L2:**

- (Decided, L2 recommendation adopted by the orchestrator.) **The one source of a workspace's `WSK`, for orch-publish,
  the relay and the hub, is the v2 directory card**, with the v2 update rules (`workspace_id`, owner and `WSK` never
  change; `card_seq` strictly increases). After H5 the host no longer talks to the relay directly, so it sends its
  card through the hub as an opaque object (`card_put`, §10) and the hub forwards it to the relay and orch-publish. The
  hub cannot forge or replace it (`PK`-signed delegation, `WSK` signature, never-change rules); it can only withhold
  it, which only stops publishing. **orch-publish never pins a binding first-come:** that would make the hub's choice
  of `WSK` authoritative. A binding counts only under the `WSK` of the card publish already holds (§9.2, §11.2 step 5).
- **Relay accounts by hub card** (E1) and hub↔hub objects (H7) are not in H0.
