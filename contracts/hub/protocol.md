# orch hub protocol, contract H0 (`orch.hub/0`)

This is the wire contract between the orch hub, orch core (the workspace host), orch-publish, orch-relay and, from
phase 4, orch-mobile. It fixes the exact bytes of every hub object and what each verifier does with an input that is
not right.

- **Spec:** orch-core `docs/architecture/orch-v2-hub.md` (D67–D82), written *spec §n* below.
- **Plan:** orch-core #838. The V3 DECISIONS (V3-D3, V3-D4, V3-D8) and the SHORTCUTS table apply.
- **Base contract:** `contracts/relay/protocol-v2.md`, written *v2 §n*. This contract reuses its suite, encodings,
  signed-object shape and label rules unchanged. There is no second encoding.
- **Vectors:** [`vectors_hub.json`](vectors_hub.json), one file shared by hub, core and publish
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
| 8.8 | phase 5 (C8/C10, V3-D29): custodial grants, device exclusion, policy and settings; break-glass leave with the hub down | `UK` + channel key; `WSK` | host, orch-publish | 5 |

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
| `h_hub_market_policy` | `orch/v2/hub-market-policy\|` | hash | the hub marketplace policy (`policy_hash`, the `market_policy` audit event) | 7.1 |
| custodial family | `orch/v2/hub-custodial\|<type>\|` | `UK` | a custodial event (`type` is the event type) | 8 |
| `sig_hub_channel_proof` | `orch/v2/hub-channel-proof\|` | channel key | the event digest | 8 |
| `sig_hub_channel_register` | `orch/v2/hub-channel-reg\|` | the new channel key | `cj(o)` of `hub.channel_register` (proof of possession) | 6.3 |
| `sig_hub_api_origin` | `orch/v2/hub-api-origin\|` | `HK_op` | `cj(o)` of `hub.api_origin` (the hub's public API origin) | 6.4 |
| `sig_hub_channel_auth` | `orch/v2/hub-channel-auth\|` | channel key | `cj(o)` of `hub.channel_auth` (binds a pairing session to the channel) | 6.4 |
| `sig_hub_channel_push` | `orch/v2/hub-channel-push\|` | channel key | `cj(o)` of `hub.channel_push` (names the channel's push key) | 6.4 |
| `sig_hub_channel_relay_req` | `orch/v2/hub-channel-relay-req\|` | channel key | the bytes of a channel → relay request | 10.8.4 |
| `h_hub_session_ref` | `orch/v2/hub-session-ref\|` | hash | names a pairing session inside `hub.channel_auth` | 6.4 |
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
| `sig_hub_owner_proof` | `orch/v2/sig/hub-owner-proof\|` | owner's `PK` | `cj(o)` of `hub.owner_proof` (the owner proof of a hub op) | 9.6 |
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
`hub-space-proposal|`, `hub-relay-req|` and `hub-api-origin|`), `UK` under the custodial family, a channel
key under `hub-channel-proof`, once `hub-channel-reg` (its own registration), and (§6.4, §10.8.4) `hub-channel-auth`,
`hub-channel-push` and `hub-channel-relay-req` for its own channel only, `WSK` under its four `sig/hub-*` labels, `sig/share-item|`, `sig/space-item|`, `sig/space-accept|` and `orch/v2/publish-hub|`, the owner's
device key under `sig/hub-publish-approval|`, the person key `PK` under `sig/hub-owner-proof|` (§9.6; the form of v2's
other `PK` labels, `sig/device-cert|` and `sig/ws-delegation|`).

**Enforced in code, not by convention (L2-4).** `orch_hub.contract.keystore` is the contract's only signing API: one
signer per key kind (`RootSigner`, `OpSigner`, `UkSigner`, `ChannelSigner`, `WskSigner`, `OwnerDeviceSigner`,
`PersonSigner`). Each
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
              "tls_spki_sha256", "card_seq", "reader", "issued_ms", "dev"},
        "op_cert": <hub.op_cert>,
        "sig": b64u(Sign(HK_op, "orch/v2/hub-card|" || cj(o)))}
```

| Field | Value |
|---|---|
| `hxk_pub` | b64u, the hub's key-agreement key (receives `WK_e` wraps in full mode, shared items in selective mode) |
| `relay_url` | `null`, or v2 §7.1's form; `null` until the relay has hub accounts (phase 4) |
| `tls_spki_sha256` | b64u of `H(SubjectPublicKeyInfo DER)` of the hub's TLS key: the pin of spec §4a |
| `card_seq` | integer ≥ 1; a holder keeps the highest it verified |
| `reader` | integer, `0 ≤ reader ≤ 2^31 − 1`: the hub's reader level (below, #1198 C8). This hub declares `1` |

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
   replaces the held card (and with it the TLS pin and `hxk_pub`). After those order checks, a `reader` below the
   held card's → `reader_downgrade`, under the same or a newer op cert (a hub never lowers its level).

**Reader level (#1198 C8).** The card's `reader` is the hub's signed declaration of the events it can read, in the
levels of orch-v2-host §0.8.6 item 9 (the levels a phone or another member's core declares, and the relay in
`GET /v2/relay-info`): level 1 means the hub understands `auth: "convenience"` (One click) on every event it reads
and refuses an unknown `auth`. orch-hub is level 1: it checks `auth` only on custodial events (§8.3) and treats every
other event as data. The level sits inside the `HK_op`-signed card, so a holder trusts it exactly as far as it
trusts the card, and it only goes up (`reader_downgrade`).

The host's gate, before it binds (C9; reference `check_hub_reader`, vectors `hub_reader_*`): a selective join makes
the hub a reader of the shared tickets, and a full join a reader of everything. `hub.join` and `hub.mode` are refused
when the card's `reader` is below the workspace's `required_reader` (orch-v2-host §0.8.6 item 9.4: `1` while One
click is on, the durable mark is set or the history holds a convenience event, else `0`) → `hub_too_old`; the host
reports it as `sign.convenience_readers` with reason `hub_too_old`, like `relay_too_old`. A level-0 card is a
declaration, not a refusal (vector `card_reader_level_0`): the host decides.

**Check at use (L2-1).** Every use of a held card (§6, §7, §8, §10) first checks the held card's op cert against the
**current** revoked set → `revoked`. A revocation that arrives after the card was admitted stops it at once; no caller
relies on having re-verified the card.

**Workspace invite form (D12c).** The workspace owner passes the invite as `<hub_id>:<code>`, or as `--hub-id <hub_id>` plus
the bare `<code>`. `hub_id` is the 128-bit hash of the hub root key (`H(hk_root_pub)[:16]`, 32 lowercase hex), delivered
out of band by the hub admin; `orch-hub serve` prints it as `invite <hub_id>:<code>` (never with the `=USER_ID` alias). The
host sends only `<code>` to the hub (the hub never sees the prefix) and compares the card's `hub_id` with the invite's
before anything is signed: a mismatch is refused `other_hub`, a card whose root does not hash to its `hub_id` is refused
by `verify_card`. This is not the phone pair link `h1.<hub_id>.<user_id>.<code>` (§6.4): that form names a hub user and
authorises a phone, while a workspace invite names no user (an operator-chosen `--invite CODE[=USER]` carries the alias
on the hub side only) and carries no authority beyond one join.

**Pinning.** The host pins `hk_root_pub` at `hub join` (the card arrives with the join, is verified against the invite's
`hub_id`, and is signed into `hub.joined`, §9.1).
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

### 6.4 The phone's hub session, push key and pairing link (phase 4, B stream, orch-core #861–#866)

**Purpose.** What a phone channel needs from the hub beyond §6.3, fixed here so the hub (D13 web API) and orch-mobile
build the same thing (design note `state/design-b-phone.md`). Reference `orch_hub.contract.channelapi`; vectors
`pair_link_*`, `channel_auth_*`, `channel_push_*` and the section `phase4.channel_api`.

**The pairing link (the operator's QR).** `orch://hub-pair/<authority>#h1.<hub_id>.<user_id>.<code>`, made at the hub's
terminal by the state-dir owner for one user, with a fresh §6.3 pairing code (`xxxx-xxxx-xxxx`, 12 lower-case hex). The
API origin is `https://<authority>`. A loopback host (`localhost`, `127.0.0.1`, `[::1]`) means plain `http://` and is
accepted **only by a dev verifier**; a real build is https only and refuses it (`malformed`). Strict: ASCII, ≤ 512 bytes, lower-case
host, port 1–65535 without a leading zero, no userinfo, path, query, percent-encoding or second `#`, exactly `h1` and
lower-case hex; anything else is `malformed` (reference `parse_pair_link`; a dev verifier is a compile-time DEBUG
build, never a value from the link or the card). **`hub_id` is the phone's pin:** the phone fetches the card and the
hub's signed API origin (`GET /api/v1/hub/card`, no session) and verifies the card as a fresh holder (§5 steps 1–6)
with `hub_id(hk_root_pub)` equal to the link's `hub_id` (`other_hub`).

**The API origin (L2B-1).** The card is public and replayable, so a forged link naming a phishing host and the real
`hub_id` would pass the pin alone. The hub therefore signs its own public API origin:

```
hub.api_origin {v, suite, kind: "hub.api_origin", hv, hub_id, op_cert_id, origin, issued_ms, dev}
sig = Sign(HK_op, "orch/v2/hub-api-origin|" || cj(o))
```

`origin` is `https://<host>[:port]` (plain `http://` only for a loopback host at a dev verifier). The phone verifies it
under the card it just pinned (§6.1 rules: `revoked`, `bad_signature`, `other_hub`, `stale_op_cert`, dev marker;
`issued_ms` not in the future, `cert_invalid`) and refuses with **`other_origin`** unless `origin` is exactly the link's
API origin, **before it sends a password** (reference `verify_api_origin`, vectors `api_origin_*`). A phishing host can
relay the object, but it names the real origin. The hub reissues it with every new `HK_op`. **It has no expiry of its
own: changing the API origin requires a new `HK_op` (a new op cert) and revoking the old op cert (§4.2)**, so an object
naming the old origin dies with it. The hub signs `origin` from the same configuration value its web API takes its
allowed origins from, never from a request. A paired phone keeps the origin it verified and refuses a link to another
origin for the same `hub_id`. It is a separate object,
not a card field, because orch-core, orch-publish and the relay check the card's exact field set. The pin comes out of
band from the operator; the phone shows it as the fingerprint (`hub_id` in 8 groups of 4, reference `fingerprint`) and
the admin console shows the same next to the QR. A later card follows §5 step 7 against the held one; another root is
`other_hub` and the phone never re-pins (only leave and pair again).

**Sessions (spec §7).** A password + TOTP sign-in with `client: "app"` gives a **pairing session** (15 min) that may only
read the card, register a `phone` channel (§6.3, with the pairing code), bind, and sign out. The channel key then binds
that session to itself:

```
hub.channel_auth {v, suite, kind: "hub.channel_auth", hv, hub_id, user_id, channel_id,
                  session_ref: b64u(H("orch/v2/hub-session-ref|" || token)), ts_ms, dev}
sig = Sign(channel_key, "orch/v2/hub-channel-auth|" || cj(o))
```

`token` is the pairing session's bearer token (ASCII), so the binding cannot be carried to another session and the token
itself is never in a signed object. The hub checks, in order (reference `verify_channel_auth`): envelope and header;
`channel_id` is a channel of the session's user (`unknown_channel`, also for another user's id); the signature under that
channel's certificate key (`bad_signature`); the field set; `hub_id` (`other_hub`); `user_id` is the session's
(`other_user`); the channel is a `phone` (`not_allowed`); `session_ref` (`challenge_invalid`); `|now − ts_ms| ≤ 120 s`
(`stale_timestamp`); the dev marker; then, told only to the key holder, `channel_revoked` and `cert_expired`. On success
the pairing session is consumed and a **channel session** issued: bound to that channel, expiring **12 h after the
password sign-in** (`CHANNEL_SESSION_MS`), after which the person signs in again and binds again.

**Channel session scope.** It may read the person's views (`/me/*`, items), `prepare`, read its own prepared requests
and `sign` **for its own channel only**, register its push target, revoke its own channel, and sign out. It may not
register any channel or use admin routes (`forbidden_role`). Every request re-checks the channel: a revoked channel
answers `channel_revoked`, an expired certificate `cert_expired` (both final for the phone). So a stolen bearer token can
show and route for at most 12 h and can never add a channel that signs.

**The push key.** The phone's notification extension must open hints while the phone is locked, where the Secure
Enclave channel key cannot be used, and one key never does both ECDSA and ECDH. So each phone channel names a separate
P-256 **push key** that the hub seals every hint to (§10.8.2):

```
hub.channel_push {v, suite, kind: "hub.channel_push", hv, hub_id, channel_id, push_pub, issued_ms, dev}
sig = Sign(channel_key, "orch/v2/hub-channel-push|" || cj(o))
```

Reference `verify_channel_push`, in order: envelope; the signature under the channel's certificate key; the field set;
`hub_id` (`other_hub`); `channel_id` is the session's (`other_user`); `|now − issued_ms| ≤ 120 s` (`stale_timestamp`); the
dev marker; `push_pub` is not the channel key (`malformed`); `issued_ms` newer than the held push key's (`replay`).

**Hub web API additions** (`/api/v1`, same rules as D13: `X-Orch-Csrf` on writes, bearer for the phone, §14 codes):

| Method | Path | Session | Body → answer |
|---|---|---|---|
| GET | `/hub/card` | none | → `{card, api_origin}` (the held, signed card and the signed `hub.api_origin`) |
| POST | `/session/channel` | pairing | `{auth: <signed hub.channel_auth>}` → `{token, user_id, channel_id, expires_ms, channel_cert, card}` |
| PUT | `/channels/{channel_id}/push` | channel (that channel) | `{env, topic, token, push_key: <signed hub.channel_push> \| null}` → `{registered: true}`; `null` keeps the held push key (an APNs token change needs no new key; without a held key it is `malformed`). The hub then registers the target at the relay (§10.8.1) |

**Not here.** App Attest (SHORTCUT of §6.3 stays). Renewing a channel certificate for the same key (the `renew` audit change exists, §10.5, but no hub renews in V3: the certificate lasts 31 days, then the phone pairs again). Admin-floor custodial types from a phone (spec §7 wants
a sign-in within 5 min for them; a V3 phone offers only the four decision types).

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
| `market_policy` | the hub marketplace policy of §7.1; **required, never `null`** |
| `dev` | boolean |

**SHORTCUT** `Snapshots by version only (no generations)` (#910): `generation` is present and MUST be `0`; any other
value is `malformed`. Hardening (#910) defines `HK_root`-signed generations (spec §6) as a new `hv`.

**Verification by the host** (the newest snapshot it holds is `pinned`):

1. §2.2 steps 1–3 under the card's `HK_op`; `hub_id` → `other_hub`; `op_cert_id` → `stale_op_cert`; dev marker;
2. `roles` keys and values, `revoked_channels` sorted and unique, `market_policy` valid (§7.1) → `malformed`;
3. `workspace_id` is this workspace → `other_workspace`;
4. `version < pinned.version` → `stale_snapshot` (a replayed snapshot cannot bring back a removed user);
5. `version == pinned.version`: identical `cj(o)` is a no-op; anything else → `snapshot_conflict`;
6. hold it as the new pin, and store it in the workspace ledger (spec §6) so readers can verify old events.

### 7.1 The hub marketplace policy `orch.hub-policy/1` (D14, orch-core#927)

**Purpose.** What the company allows its workspaces to install and run: which marketplaces, which add-ons, how much
capability. The hub admin sets it; `HK_op` signs it inside every snapshot; orch-core reads it only from a verified
snapshot and can only **refuse** by it (a human grant is still needed; core runs standalone without a hub, D79).
Enforcement is core's (orch-core#928); this section is the shared contract. Vectors: `vectors_hub_policy.json`
(`check: "policy"` and `"evaluate"`) and the snapshot and audit cases of `vectors_hub.json`.

```
market_policy = { "markets": [ {"url": <canonical URL>, "class": "official" | "company", "keys": [<b64u P-256 point>, …]}, … ],
                  "addons":  {"allow": [<id>, …] | null, "block": [<id> | <id>@<semver>, …]},
                  "ceiling": [<capability>, …] | null }
```

- Exact key sets at every level; an unknown or missing key or a wrong type is `malformed`. **Malformed anywhere is the
  whole policy malformed** (a verifier never reads part of it).
- `markets`: 0 to 16 entries, sorted by `url`, unique. `url` is a canonical marketplace git URL (`https`, no `.git`, no
  port, no query; lower-case on case-insensitive hosts such as `github.com`), never repaired. `class` is cross-checked,
  never trusted: `official` exactly when `url` is the official marketplace (`https://github.com/severinlindenmann/orch-marketplace`)
  and then `keys` is `[]` (core pins the official key; a hub can neither add nor replace it); `company` for any other
  URL, with 1 or 2 sorted unique valid P-256 points (the current and the next key).
- `addons.allow`: `null` = any id from an allowed marketplace, else 0 to 256 sorted unique ids (`[a-z][a-z0-9-]{0,39}`).
  `addons.block`: 0 to 256 sorted unique items, each an `id` (every version) or `id@x.y.z`. Ids are global across
  marketplaces.
- `ceiling`: `null` = none, else a sorted unique subset of core's capabilities (`git_push`, `network`, `pty`,
  `serve_http`, `spawn_agent`); `[]` = capability-free add-ons only.
- Lists must already be sorted (one form, one hash). `policy_hash = b64u(H("orch/v2/hub-market-policy|" || cj(policy)))`.
- **A new hub** starts at policy version 1: the official marketplace only, `allow: null`, `block: []`, `ceiling: null`.
- **Versioning.** A change of the policy changes every workspace's snapshot content, so every workspace's snapshot
  `version` rises; `hv` stays `0` (nothing ships an older shape, V3-D36). Selective workspaces (D80) receive the policy
  too: it only restricts.
- **Who changes it.** Only a hub-admin, in the admin console: `GET /api/v1/admin/market-policy` returns
  `{version, policy, hash, official_url}` (the last is display data for the console); `PUT /api/v1/admin/market-policy {base_version, policy}` replaces the whole object, answering
  `malformed` (400) for an invalid policy, `conflict` (409) when `base_version` is not the stored version (no lost update),
  `forbidden_role` (403) for anyone else. There is no partial edit. Each change appends one audit record of type
  `market_policy` (§10.5) **before the change commits**; if the chain cannot be written the change is refused. The audit chain and the policy row live in two databases and cannot share one commit, so the order is fail-safe: if the commit fails after the record was written, the chain holds a record for a change that did not happen (an extra record is possible, a missing one never).
- **Evaluation** (core, orch-core#928) is specified by the `evaluate` vector cases: unlisted market `market.not_allowed`,
  company index without a pinned signature `market.index_unsigned`, `id` or `id@version` blocked `market.blocked`,
  `allow` set and id not in it `market.addon_not_allowed`, publisher key not pinned `market.publisher_unpinned`,
  capabilities beyond `ceiling` `market.capability_ceiling`; an unreadable or malformed policy is `market.not_allowed`. The `run` stage (before each run, from the pinned snapshot) checks block and ceiling for every add-on and, for a marketplace add-on (`market_url` not empty), also that the origin is listed, `allow`, and for a `company` origin that the key its install record holds is still pinned (no key = `market.publisher_unpinned`).

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
| `item_hash` | **selective mode only** (C3b addendum): the `item_hash` (§10.2b) of the shared item the event was built on (§8.6); absent in full mode. Under both signatures like every other field |

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
   fields, `item_hash` (a hash) present exactly in selective mode → `malformed`; selective mode: `type` one of the
   four decisions of §8.6 and `log` a ticket → else `not_custodial`, a `question.answered` without `option` → else
   `malformed`; a `log` key in the event body (the log is the signed context's) → `malformed`; for a `(type, log)` of the §8.5
   table, the envelope and payload are exactly the event side of §8.5 ("The event side") → `malformed` (reference
   `check_custodial_payload`, vectors `custodial_event_*`);
2. `hub_id` is the hub this workspace is bound to → `other_hub`;
3. `uk_cert` verifies under the held card (§6.1: `bad_signature`, `other_hub`, `stale_op_cert`, `dev_*`, `cert_*`), and
   names the actor's user → `other_user`. The held card was verified through the pinned `HK_root` with the revoked op
   certs applied (§5), which is the chain `UK → HK_op cert → pinned HK_root` with no revoked `HK_op` (spec §5);
4. `channel_cert` verifies the same way, names the same user and `channel` → `other_user`; its `factor` equals
   `channel_factor` → `factor_mismatch`;
5. `sig` under `uk_pub` with the custodial label of `type` → `bad_signature`;
6. `channel_proof` under the channel certificate's `key_pub` over `digest` → `bad_channel_proof`;
7. snapshot: `snapshot_version` greater than the newest snapshot the host holds → `unknown_snapshot` (the host syncs
   and retries); (C3b) the user id belongs to this hub in this workspace (§9.1) → `foreign_user`, checked only after
   the certificates so an unauthenticated event learns nothing of the id map; `channel` in that snapshot's
   `revoked_channels` → `channel_revoked`; full mode: the user has a role in
   it → else `forbidden_role` (which verbs each role may use is the role table of spec D70, applied after this);
   selective mode: the user is explicitly granted by the owner → else `not_granted`, and **the item check at
   admission** (§8.6, core §15.7), in order: (i) `item_hash` is the current hash of a live item the host shared → an
   earlier version of a live item is `event_changed` (a re-put raced the decision; the person may sign again), else
   `not_granted`; (ii) that item's `chain.log` is the event's log, and the decision's target is in that item's `f1`
   (`gates[gate]`, `gates.verify` for `verdict.given`, `questions[question]`) → else `not_granted` (a question item
   never admits a gate decision); the event carries exactly that item's values: `hash` = the entry's `hash`, on gate
   decisions `gate_gen` = `gen`, `policy_hash` = `policy_hash` and (where present) `source_sha` = `source_sha`, and
   always `based_on` = `chain.head` and `roster_v` = `f1.roster_v` → else `event_changed` (vectors
   `custodial_selective_value_*_differs`); (iii) `chain.head` is the head of `chain.log` at `chain.seq` in the host's own log
   → else `view_fork`. Reference inputs: `live_items = {item_hash: {chain, f1}}` (current versions),
   `superseded_items` (earlier hashes of live items), `log_heads = {log: [head of seq 1, …]}`; vectors
   `custodial_selective_*` use the real `share_item_f1` item;
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
   workspace, ticket, gate and hash, `gate_gen`, `source_sha`, `from`, outcome, text, people, settings), and any field it does not know makes it fall back
   to the full field list (WYSIWYS). It signs only the digest it computed itself.
   **View-derived text is escaped** (L2P4-2d): titles, question texts and option labels come from the workspace's
   events, which agents write and which have no text rule in core; a channel renders every character the §8.5 text
   rule refuses by stripping it (reference `render_text`, vectors `render_*`). **Questions** (#1123): the needs content of a question carries `text`, `options` and `spec` VERBATIM (a client hashes exactly `{question_id, ticket, text, options, spec}` and compares with the row `hash`); a question the hub cannot verify (hash mismatch, unknown kind, spec or option key, or a character `render_text` would escape) carries only its escaped `text` and `unverifiable: true`, stays in its ticket, and is never answerable through the hub. The hub web checks the hash itself and shows the selected option (title, text, facts, cost, question, details) in the sign prompt; it offers no Answer button for a question it cannot render or verify. **Tab exception (#1123):** core allows a TAB in the multi-line question fields (`text`, `why`, `details`, answer text) and refuses it in one-line fields (title, option text, cost, fact label and value); the hub's verbatim content keeps the tab, and a channel shows it as spaces in multi-line fields only. The hub also marks unverifiable a question that breaks a cheap core limit (code-point and byte limits, NFC, blank, LF/tab in one-line fields, boolean `blocking`, titles and fact labels unique under `casefold(NFC(.))`); only core checks the exact fold tables and the leading-mark rule. A channel signs `question.answered` only against the verified question it answers (same `hash`) and the kind's answer rule. **Content-bound
   decisions** (L2P4-2c): body text and artifacts are not synced (§10.7), so a channel that offers `gate.approved`,
   `gate.changes_requested` or `verdict.given` shows the decision's hash in short form next to a link to where the
   content was reviewed, and Mission Control shows the same short hash; a channel that cannot do that does not offer
   content-bound decisions. **Projection-derived fields** (D15b-2): on the three decisions the channel shows the gate,
   the short hash, `gate_gen` and, on a `code` approval or a verdict, each `repo` with its short `sha`; on
   `status.changed` and `ticket.moved` it shows "`from` → `to`".
   **Short hash form** (orch-hub#55, pinned): wherever a person compares a content hash between screens (the phone, the
   browser home, the API `shown.facts` and needs-you `short_hash`, Mission Control), it is shown as the first **12
   lower-case hex digits after `sha256:` in three groups of 4**, `abcd ef01 2345`, never upper case, never 8 or 10
   digits. The input per kind is the hash the person decides on: `gate.approved` and `gate.changes_requested` use the
   gate's projection `hash`, `verdict.given` the verify gate's `hash`, `question.answered` the question's `hash`; a
   status move (`status.changed`, `ticket.moved`) has no hash and shows "`from` → `to`" only. Anything that is not
   `sha256:` plus 64 lower-case hex digits is `malformed`; the channel then shows the full field (WYSIWYS), never a
   shortened guess. Reference `short_hash` (phase4); vectors `short_hash_*`. The "signature reference" (the digest of
   `ctx`) uses the same grouping over its first 12 hex digits (reference `short_digest`, vectors `short_digest_*`).
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
| `admin` | `ticket.moved`, `people.changed`, `visibility.changed` | `addon.disabled`, `addon.purged`, `addon.requested` (§8.8b) |

The role is the floor; the F1 rules decide the rest (eligible approvers, ticket owner, D59 independence; §8.3 step 9).
**Phase 5 (§8.8, V3-D29):** `grant.requested`, `grant.revoked`, `device.excluded`, `policy.changed` (workspace and
ticket) and `settings.changed` (three integer keys only) join the table with exact schemas; a host before C10 refuses
them as `not_custodial`. People and visibility changes are `admin`-only through a channel, with exact schemas. **Not custodial in hv 0, and
why:** `workspace.created`, `member.*`, `role.changed` (members and roles come from
grants and snapshots); `device.*` except `device.excluded` (§8.8) (hub users have no device keys); `hub.joined`, `hub.left` (owner's local key at the
terminal, break-glass); `grant.issued` (the host makes the secret: §8.8 `grant.requested`); `addon.granted` (the host derives the
manifest fields; a full-mode workspace gets it from the host as the answer to `addon.requested`, §8.8b); `restore`, `invalid.acknowledged` (owner recovery); `artifact.*` and `ticket.updated` with
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
| `question.asked` | `question`: `{to, text, blocking, kind}` plus `why?`, `details?` and, per kind: `choice` = `options` (2–6 of `{key, label, text?, facts?, cost?}`, `facts` 1–3 `[label, value]` pairs, token keys unique) with optional `recommended` (one of the keys) and `allow_other: true`; `text` = no options, no `recommended`, no `allow_other`. Text 1–140 code points, `why` ≤ 4096, `details` ≤ 1500; no `id` (the hub assigns it). The host re-checks every limit (core `validate_question`). The hub hashes the raw question: `H("orch/v2/question|", cj({question_id, ticket, text, options or [], spec}))`, `spec` = every key except `id, to, blocking, text, options` (frozen core fixtures `tests/vectors/f1/{hashes,questions}.json`, core PR #1135 commit e0525503) |
| `question.answered` | `question` (`Q<n>`), `hash`, `option?` (an option key), `text?` (1–1000 code points; at least one of the two). Per kind: a `choice` takes an `option` (plus an optional note) or `text` alone only when `allow_other`; a `text` question takes `text` only |
| `gate.approved` | `gate` (`requirements`, `plan`, `code`), `hash` |
| `gate.changes_requested` | `gate`, `hash`, `text` |
| `verdict.given` | `outcome`, `hash`, `text?` (required on `fail`) |
| `addon.disabled`, `addon.purged` | `name` (addon name) |
| `addon.requested` | `name`, `version`, `package_sha256`, `capabilities`, `handoff`, `runtime?` (§8.8b) |

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

**The event side (C3b addendum, L2-C3-1).** A custodial event's payload is **exactly** the action's fields (same
nested schemas, same text rule) plus the fields the hub derives, nothing else at any depth; the envelope is `v: 2`,
`id` (a ULID), `type`, `actor`, `based_on` (a hash; `null` exactly on `ticket.created`), `roster_v` (integer ≥ 0),
`hash_v: 1`, then the §8.1 custodial fields (and `item_hash` in selective mode). The derived payload fields per type:

| Type | Derived by the hub (from its view, §10.7, or the shared item, §8.6) |
|---|---|
| `ticket.created` | `key` (the host's `next_key`), `owner` (`u_` + the signer's user id); `based_on: null`; the log is the new uid |
| `ticket.updated` | `base_rev`: **an object** `{path: value hash}` whose keys are **exactly** the keys of `set` (ticket-format §5.8, value hash §5.6). Never a single hash string |
| `status.changed`, `ticket.moved` | `from` (the current status, status list; full mode: `P.status`, §10.7 D15b) |
| `question.asked` | `question.id` (`Q<n>`), `qid` (32 hex), `hash` (question hash) |
| `gate.approved` | (full mode: from the projection `P`, §10.7 D15b; selective: from `f1`) `gate_gen` (integer ≥ 0), `policy_hash`; `source_sha` (the source list `[{repo, ref, sha}]`, sorted by `repo`) **only and always** on `gate: "code"` |
| `gate.changes_requested` | `gate_gen`, `policy_hash` (no `source_sha`) |
| `verdict.given` | `gate_gen`, `policy_hash`, `source_sha` (always) |
| every other type | nothing (`ticket.submitted`, `ticket.closed`, `ticket.reopened`, `claim.released`, `log.added`, `handoff.written`, `question.answered`, `visibility.changed`, `people.changed`, `addon.*`) |

The host runs this check in §8.3 step 1, before any certificate or signature (reference `check_custodial_payload`;
it maps the event back to its action and runs `check_action`), so a buggy or compromised hub cannot plant a field a
local person event could carry (a `sections` edit, a `ticket.tasks` `verify.cmd`, a question `sig`). Vectors: one
valid signed event per custodial type (`custodial_event_<type>`, the payload exactly as the hub builds it) and the
smuggling cases `custodial_event_base_rev_*`, `…_ticket_updated_with_sections`, `…_set_of_ticket_tasks`,
`…_question_with_sig`, `…_gate_payload_under_people_changed`, … (all `malformed`).

### 8.6 Selective custodial events (C3b addendum; D9 hub, C6 core)

A selective hub holds no `WK_e` view (§10.7 is full mode only). It builds a custodial event from **one shared item**
(§10.2b) that carries an `f1` block, and only for these types (reference `SELECTIVE_TYPES`): `question.answered`,
`gate.approved`, `gate.changes_requested`, `verdict.given`, on the ticket log of the item. Every other type is
`not_custodial` in selective mode (nothing on the workspace log, nothing that needs the ticket's own fields).

- **The action** is the §8.5 action plus `item` (the `item_id`, 32 hex): `{type, log, item, <fields>}`. `item` in a
  full-mode action is `malformed`. In V3 a selective `question.answered` carries `text` only: question options are not
  shared, so an `option` is `malformed` (the person cannot have seen them).
- **Who.** Only the hub user the admin aliased as the workspace owner reads selective items (§10.2b), so only that
  user prepares (`not_granted` otherwise). The host checks the real grant and member role (core §15.7, §8.3 step 7).
- **Build** (reference `build_selective`, vectors `selective_build_*`), on the live item the hub holds under `item`:
  1. the action's shape, the type in the selective list (`not_custodial`), `check_action` with the `edit` floor;
  2. the item is held, live (not revoked) and its `chain.log` is the action's `log` → else `unknown_item`; the item
     has an `f1` block → else `unavailable` (the host shared it without one);
  3. a question answer: `f1.questions[question]` exists → else `unknown_item`; its `hash` equals `action.hash` →
     else `event_changed`. A gate decision: `f1.gates[gate]` (`verify` for `verdict.given`) exists → else
     `unknown_item`; its `hash` equals `action.hash` → else `event_changed`;
  4. the event core: `based_on = chain.head`, `roster_v = f1.roster_v`, `hash_v: 1`, `item_hash` = the item's
     §10.2b hash, and per type the fields of §8.5's event side from the `f1` entry (`gate_gen = gen`,
     `policy_hash`, `source_sha` on a `code` approval and on a verdict); every other field is the action's;
  5. `check_built` (§8.4), then the custodial fields, prepare, proof and sign exactly as §8.4, with the workspace's
     snapshot (§7). `sign` rebuilds from the item held **then**: a newer `item_version` (another hash) is
     `event_changed`, a revoked item `unknown_item`.
- **What binds what.** `item_hash` is under both signatures (§8.1), and it covers the whole signed item including
  `f1`, so the proof binds exactly the values the person was shown. The host admits the event only while that hash is
  the current hash of a live item it shared, of the event's log, with a matching chain head (core §15.7:
  `not_granted`, `view_fork`), and then runs every F1 rule against its own log (§8.3 step 9): a stale `f1` value is
  refused there (`event_changed` in the reply, §8.7).

### 8.7 Delivery and acknowledgement (`event_deliver`, C3b addendum, L2-C3-4)

The hub delivers each signed custodial event with `event_deliver` (§10.2) and keeps it (persisted) until the host's
reply decides it. The host's reply is a signed w2h reply; its body decides (reference `deliver_next`, vectors
`deliver_*`):

| Reply | The hub |
|---|---|
| `{}` | **done**. Also the reply to a duplicate: the host dedups by event `id` before it verifies anything (core §15.5 step 2) |
| `{"error": "unavailable"}`, `{"error": "unknown_snapshot"}` | **resend** |
| no reply (link drop, timeout), or a body that is neither `{}` nor `{"error": <string>}` | **resend** (not provably refused) |
| `{"error": <any other code>}` (`malformed`, `not_bound`, `wrong_mode`, `replay`, `event_changed`, `forbidden_role`, `not_granted`, `foreign_user`, `channel_revoked`, `not_custodial`, `factor_too_weak`, `view_fork`, `internal`, …) | **final**: drop it and tell the channel the code; on `event_changed` the person may prepare and sign again |

- **Resend** means the **same event bytes** (same `id`, same signatures), never a rebuilt or re-signed event; only
  that makes the host's dedup by `id` safe.
- **Backoff:** attempt `n` (1, 2, …) is followed by a wait of `min(1 s · 2^(n−1), 5 min)`; a resend after a link drop
  goes out on the next link, still under that wait. At most 24 h after the first send the event is dropped and the
  channel told the last code (`unavailable` for no reply).
- One event in flight per link (§10.2); later events wait behind it in delivery order.
- The host replies `unavailable` only when it cannot decide now (lock timeout, `freeze.active`, a snapshot it could
  not fetch), never for a refusal on the event's content, so a resend can never turn a refusal into an admission.

### 8.8 Phase-5 additions: grants, devices, policy and settings through a channel (C10 M6, V3-D29)

**Status: decided (owner, V3-D29, 23 Oct; orch-core #838).** Closes the open points "agent grants" and "policy and
settings" of §16: exactly the five hub-signed custodial types below, no fourth break-glass power. Design:
`~/orch-dev/state/design-c10.md` (M1, M6), `design-c8.md` (M2.2).

Why: these are the acts a person may do remotely (phone, hub web) as an additional path. Local signing stays valid in every
mode (D83); the hub path exists so a person who is away can still issue or revoke a grant, exclude a lost device or change policy.

| Type | Log | Lowest role | Action fields (exact) | Derived by the hub |
|---|---|---|---|---|
| `grant.requested` | workspace | `edit` | `scope` (`all` \| `workable`), `verbs` (`"agent"` or 1–32 sorted unique operation names `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?`), `hours` (1–168), `label?` (one line ≤ 200 bytes), `handoff` (host-created ULID) | nothing |
| `grant.revoked` | workspace | `edit` | `grant` (`gr_` + ULID), `reason?` (one line) | nothing |
| `device.excluded` | workspace | `admin` | `device` (`d_` + 32 hex), `reason` (`lost` \| `retired` \| `compromised`) | nothing |
| `policy.changed` | workspace | `admin` | `gates`: 1–4 of `requirements`, `plan`, `verify`, `code`, each exactly `{approvers, count, not, applies, independent}` (below) | nothing |
| `policy.changed` | ticket | `edit` | as above (a ticket override; F1 decides who: ticket owner, owners, maintainers) | nothing |
| `settings.changed` | workspace | `admin` | `set`: 1–3 of `grant_hours` (1–168), `claim_ttl_min` (15–1440), `lease_ttl_min` (5–1440) | nothing |

- **Gate policy** (core `gatePolicy`, lists sorted and unique so a channel shows one form): `approvers` 1–7 of
  `owner`, `maintainer`, `member`, `ticket_owner`, `assignees`, `reviewers`, `watchers`; `count` 1–16; `not` 0–7 of the
  same tokens; `applies` `"all"`, `"off"` or 1–5 sorted unique ticket types; `independent` boolean. `code` additionally
  has `assignees` in `not` and `independent: true` (D59). **No `factor` key** (D66 floors stay off the channel in V3).
  **A channel never changes a factor floor:** the host applies each named gate as the five keys above and keeps the
  gate's effective `factor` (the stored one, else the inherited workspace floor; a ticket override inherits the
  workspace floor and can never lower it). Core vector `policy_changed_channel_keeps_factor`.
- **Settings link (27 Oct).** Mission Control's Save of one of the three keys is a host read `hub.handoff {kind: settings, set}` (nothing prepared or logged); it returns ONE link `https://<hub>/#/w/<workspace_id>/settings/<b64u(JSON {set, exp})>` (`exp`: epoch seconds, 10 minutes; the page refuses an expired link so an old link cannot roll back a later change). Who may sign is not the link's business: the hub signs only for its role `admin` on the workspace log, and the host's model applies `settings.changed` only for an owner and only the three keys, at append and on replay (core `test_h5_custodial`). The hub web parses the set strictly (the three keys, in range), shows it in its own words and signs `settings.changed` after a click; Mission Control follows the host's settings until they read as asked. Any other key (`repos`, `models`, icon) is said plainly as not changeable from the hub.
- **Off the channel in V3 (V3-D29)** (`malformed` inside `set` or a gate, `not_custodial` as types): 
  (how signatures are made), `repos` (host file system paths), `models`, the D66 `factor` key; `grant.issued` itself,
  including a terminal-printed grant for a hand-started agent (the owner's local act, standalone or after a leave);
  the person's PK-signed `device.revoked`. Vectors `action_settings_changed_{repos,models,factor}`,
  `action_policy_changed_factor_key`, `action_{grant_issued,device_revoked}_ws_not_custodial`.
- **The grant secret stays in the host.** The event carries no `grant`, `secret_hash`, `issued_at` or `expires_at`
  (`malformed`, vectors `action_grant_requested_smuggles_*`, `custodial_event_grant_requested_with_*`). After admitting
  `grant.requested`, the host appends, in the same store-lock hold, a **host-actor** `grant.issued` with the same
  terms, `request` = the request's event `id`, `issued_at` = min(the request's `at`, host now), and a fresh
  `secret_hash` (core C10). Core's model admits a host-actor `grant.issued` only with a `request` naming an earlier
  admitted `grant.requested` of equal terms, used once, `|at − request.at| ≤ 300 s`. The grant's person is the request's
  canonical signer (F1: a grant is always for its signer); its role is that signer's, its device none (the runner
  binding instead). The secret never travels on the link and is never shown or logged.
  **The host gives the secret only to its runner, and only for a pending local hand-off (V3-D29):** a Mission Control
  "Start agent" that a person started **on that host**, recorded host-locally as a hand-off of kind `grant` for the
  same canonical person and exact terms, at most 10 min old, used once. The signed `handoff` must equal that
  grant record’s id; a wrong, expired, used or add-on hand-off id cannot release a secret. `hub.handoff {kind: grant}`
  returns this id and a pinned-hub `/#/w/<workspace>/access/<b64u(canonical JSON terms)>` link with
  `{scope, verbs: "agent", hours, label?, handoff}`. The fragment is display and signing input only; the host’s
  record is authoritative. The console requires workspace role `edit` or higher, and the model re-checks the
  signer’s capability and workspace duration cap at landing, refusing excess capability without narrowing. The record is bound to the workspace id and
  the current hub binding (`hub_id`, `binding_seq`), is matched and consumed inside the same store-lock hold that
  appends `grant.issued`, and is dropped when the person cancels it, at `hub.left` and at any new `hub.joined`.
  Otherwise the host still appends `grant.issued`
  (the log stays honest) and destroys the secret at once: the grant is inert, shown as "requested from the hub, not
  started here". So **a phone (or any channel) alone cannot start an agent**, and a hub (the channel CA) cannot mint
  usable grants nobody asked for (L2-P5-3). The hand-off record is not a log line and makes no link call.
- **`grant.revoked`:** F1 at the host decides ownership: the signer's own grant (compared as the **canonical** person,
  alias-resolved) unless the mapped role is owner (`admin`). A hub-side check is advisory (a friendly error only).
- **`device.excluded`** is workspace-local: the device signs nothing more in this workspace; `compromised` voids open
  decisions as a compromised device does (ticket-format §5.7). **The host refuses it for a device of a workspace
  owner** (owner devices go only through the PK-signed `device.revoked`), and an exclusion never applies to
  break-glass verification, so the hub cannot block `orch hub leave` (L2-P5-2). The global, PK-signed `device.revoked` stays the
  person's own act and is not custodial.
- F1 runs after the floor as for every custodial type (§8.3 step 9): grant terms per mapped role (member: `workable`
  only, at most `grant_hours`), own grants only unless `admin`, workspace policy and settings owner-only (`admin`).
- `admin` verbs need a sign-in within the last 5 min (spec §7); a channel shows every policy key and value
  (WYSIWYS, §8.4 step 3). A policy with `applies: "off"` shows the fixed text "turns off the <gate> gate"; a `count`
  above the people eligible shows "no one can approve this"; the host also returns a warning on admission.

**Break-glass with the hub down (C8; text only, no new object).** `hub.left` (§9.1) is the owner's local act and needs
no hub. The host then sends `hub.unbound` (§9.3) **directly to orch-publish** (`PUT /v2/hub/bindings/{ws}`, V3-D16),
which ends hub-forwarded publish and the hub admin read (§10.6), and offers it to the hub over the link whenever one
comes up. The relay keeps no binding: a `pending` cut-over row (§10.8.3) is deleted by the workspace at the leave; a
`cut_over` row stays closed. On `unbound` an honest hub also fails its open prepares and queued `event_deliver`s for
the workspace with `not_bound` and drops the workspace from its users' channel lists. **Every leave voids the open
hub-signed decisions (V3-D29):** `hub.left` has no reason field in hv 0, so each leave is treated as a compromise of the
hub; at the `hub.left` position core voids every decision signed `hub:<hub_id>/*` in a still-open generation, whether
it counts today or not, and a later policy change or a rejoin never revives it (consumed generations stay and are
listed for the owner, design-c8 M5). **Hub-requested grants end at `hub.left`** too
(every grant whose `request` is a line of that hub is revoked at that position and its secret dropped; design-c8
M2.2). The one break-glass set (`hub.leave`, `hub.repin` PK only, `keys.rotate`) is defined in core (C8) and imported
by C11.

**Cut-over of full-mode workspaces joined before C10 (V3-D29, design-c10 M1).** A full `hub.joined` signed by a host
with C10 authorizes the cut-over (its review text says "your phones' direct pairing ends; the hub becomes the only
path"). A full join made before C10 never showed that text, so at the host's first start with C10 it is cut over
automatically **only** when the workspace is marked dev in the host state dir, the held hub card has `dev: true` and
the cut-over preflight passes (link authenticated, card verified, a pinned snapshot with an `admin`); every other such
workspace keeps its direct path (`direct: open`) until the owner runs `orch hub mode full` again and signs the new
text. `orch hub status` says which applies.

**Host-side conformance (core vectors, required for C8/C10).** The hub vectors above fix only the channel action and
the event a host admits. The host guarantees of this section are core's own vectors and tests and must exist before
C8/C10 merge: `host_grant_issued_{needs_request,request_reused,other_terms,late}`, `hub_grant_without_handoff_inert`,
`hub_grant_with_handoff_once`, `handoff_other_workspace_or_binding_refused`, `grant_revoked_own_via_alias`,
`device_excluded_owner_device_refused`, `policy_changed_channel_keeps_factor`, `left_hub_events_refused_after_leave`,
`leave_voids_open_hub_decisions_only` (incl. non-counting, no revival after rejoin), `leave_ends_hub_requested_grants`,
`rejoin_refuses_pre_leave_custodial`, `pre_c10_non_dev_full_join_not_cut_over` (design-c8 Tests, design-c10 Tests).

### 8.8b Turning an add-on on through a channel: `addon.requested` (FB15, 27 Oct)

Why: a person away from the workspace may turn an add-on on from the hub (local signing stays valid, D83), and `addon.granted` carries declarations (`fields`, `sections`,
`artifact_kinds`, `runtime`) that only the host can derive from the package, so the hub cannot build it. Same pattern as
`grant.requested` (§8.8): the channel signs flat terms, the host answers with its own event.

| Type | Log | Lowest role | Action fields (exact) | Derived by the hub |
|---|---|---|---|---|
| `addon.requested` | workspace | `admin` | `name` (`[a-z][a-z0-9-]{0,39}`, not a name core reserves: core schema `common#/$defs/addonName`, one list), `version` (strict semver), `package_sha256` (`sha256:` hash), `capabilities` (sorted unique tokens of the closed list `serve_http`, `spawn_agent`, `pty`, `network`, `git_push`, `choose_model`, `credential`), `handoff` (`hr_` + ULID: the id of the host's prepared request), `runtime?` (exactly `{kind, lock, packages}` as signed in `addon.granted`) | nothing |

- **The link.** The owner prepares the grant at the host as a hand-off (`human.prepare {op: addon.grant, handoff}`: nothing is
  signed, no local signature will ever land) and the host read `hub.handoff {kind: addon, request}` returns the terms and ONE link,
  `https://<hub>/#/w/<workspace_id>/addon/<b64u(JSON terms)>` (a fragment). The hub web parses the terms strictly, shows them
  in its own words (capability words come from the page's table; the fingerprint in full), and signs through the normal
  prepare -> review -> sign prompt only after a click, against the action it sent. The link is a convenience, never authority.
- **The host's answer.** After admitting `addon.requested`, the host appends in the same store-lock hold a **host-actor**
  `addon.granted` with `request` = the request's id, whose `name`, `version`, `package_sha256`, `capabilities` and `runtime` equal the
  request's; `fields`, `sections` and `artifact_kinds` are the host's, from the package the digest names (re-checked under the lock,
  `marketinstall.recheck`). The model admits a host-actor `addon.granted` only with `request` naming an admitted unused
  `addon.requested` of equal terms, `0 <= at - request.at <= 300 s`, whose signer is still an owner, and only once per `handoff`;
  a person never writes `request`.
- **Only for a live hand-off.** The host answers only when the prepared request named by `handoff` is the same canonical person's,
  still open on the host's clock (10 min), of op `addon.grant`, and carries exactly the requested terms; then the request ends
  `submitted` and the staged bytes are installed. Otherwise the request stays in the log and turns nothing on. A hub alone cannot
  turn an add-on on; a compromised custodial hub can sign for exactly what the owner prepared (accepted, as in §8.8).
- **One identity for an unaliased hub user (D70, prototype).** A console-link join signs `hub.joined` with no alias, so the hub user is `u_…` (owner by the hub role). The host then asks the hub over its OWN link (`owner_get`, never from a caller) who it names as the owner, writes that user ONCE on the prepared hand-off record (never rebound) and answers an `addon.requested` only from that user, for a hand-off prepared by a workspace owner of this host. This binding is host-local state, not replayable log state.
- **Not on the channel (hv 0):** the add-on settings step (`settings.set` of the `addons` key) of a sequence; an add-on that needs it
  is turned on at the terminal or after leaving. The phone has no screen for this type.
- Vectors: `action_addon_requested_*` (incl. `reserved_name_<name>` for every reserved name, smuggling of `fields`, `sections`,
  `request`), `action_addon_granted_ws_not_custodial`, `custodial_event_addon_requested`; core replays them in
  `tests/hub` and the host side in `tests/host/test_hub_addon.py`.

## 9. Join, binding, leave

### 9.1 `hub.joined` and `hub.left` (workspace log)

**Purpose.** The owner's signed record that the workspace joined (spec §8 step 3) or left (break-glass) a hub.
These are workspace-log person events (ticket-format §5.4.2 additions), signed by the owner's current device key under
`orch/v2/sig/ws-event|` exactly like any person event (ticket-format §5.3 bytes; vector `hub_joined`). Human-only
verbs at the host terminal (`orch hub join`, `orch hub leave`); never an agent, never the hub.

`hub.joined` payload, exactly: `{hub_id, hk_root_pub, mode: "selective" | "full", binding_seq, aliases, owner_proof}`.
`hub.left` payload, exactly: `{hub_id, binding_seq, owner_proof}`.

- `owner_proof` (#1198 C8) is a required member: the signed `hub.owner_proof` of §9.6. Every hub op carries it,
  whoever signs the event and whatever its `auth` (the dashboard click and the terminal alike: the owner decided
  that hub steps take the long backup key). `null` → `proof_missing`; any other non-object → `malformed`. There is no
  exemption by label or by key: nothing in production predates this rule (no legacy events to accept).

- `hub_id == hub_id(hk_root_pub)` → `hub_id_mismatch`; this is where the host pins `HK_root`.
- `binding_seq` is the `binding_seq` of the `hub.binding` the host signs next (§9.2); `hub.left` names the binding it
  ends.
- `aliases` is a list of `{person_id, user_id}` sorted by `person_id`, unique. **SHORTCUT** `Aliases owner-signed at
  join only` (C4): an alias exists only inside `hub.joined`, under the owner's signature. **An alias is never
  re-pointed:** a `person_id` already aliased to another `user_id`, or a `user_id` already aliased to another
  `person_id`, in any earlier `hub.joined` of this workspace → `alias_repointed`.
- **A `u_` id belongs to the hub under which it first appeared in the workspace** (C3b, L2P4-C1, L2-C3-2): as the
  actor of an admitted custodial event, as a `u_` reference in a person event's payload (under the hub joined at that
  position) or as an alias in a `hub.joined`. The workspace keeps that record for good (core `user_hub`, never
  re-pointed). **A hub must never present another hub's user id**: it issues `uk_cert`s only for its own users and
  never builds, signs or delivers an event whose actor is a user id another hub owns in that workspace. The host
  refuses one with `foreign_user` (§8.3 step 7), unless this hub's own alias book names the id for the same person as
  every other book that names it (C4: one person may keep one id on two hubs). Reference `check_user_hub`, vectors
  `user_id_*`, `unaliased_user_id_first_recorded_under_another_hub`, `same_person_aliased_on_both_hubs`.
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

### 9.6 The owner proof of a hub op (#1198 C8)

**Purpose.** Binding a workspace to a hub (or switching it to full mode, or leaving) is done in the dashboard with
a click, and the click is signed by the One-click device key (`auth: "convenience"`). That key may not bind the
workspace to a hub by itself: a full join hands the hub `WK_e` and from then on the hub signs for the workspace, so a
taken-over dashboard could hand the workspace to its own hub, and that outlives switching One click off. The op
therefore also carries an **owner proof**, signed by the person key `PK`. `PK` is derived from the owner's recovery
code (the long backup key, orch-core `identity/recovery.py`: scrypt, then FIPS 186-5 A.2.1). The person types the
code once; the signer next to the workspace derives `PK`, signs the proof and wipes the code and `PK` (C10). The
host, the hub and every reader of the log check the proof; none of them ever sees the code.

```
proof = {"o": {"v": 2, "suite": 2, "kind": "hub.owner_proof", "hv": 0, "workspace_id", "person_id", "op",
               "payload", "request_id", "issued_ms", "expires_ms", "dev"},
         "sig": b64u(Sign(PK, "orch/v2/sig/hub-owner-proof|" || cj(o)))}
```

| Field | Value |
|---|---|
| `workspace_id` | hex; the workspace the op runs in |
| `person_id` | hex; `person_id(PK)` (v2 §4), the owner who signs |
| `op` | `"hub.join"`, `"hub.mode"` or `"hub.leave"` |
| `payload` | the exact payload of the event the op appends, **without** its `owner_proof` member: for `hub.join` and `hub.mode` a `hub.joined` payload `{hub_id, hk_root_pub, mode, binding_seq, aliases}` (§9.1 rules: `hub_id == hub_id(hk_root_pub)`, aliases sorted and unique; `hub.mode` only with `mode: "full"`), for `hub.leave` a `hub.left` payload `{hub_id, binding_seq}` |
| `request_id` | the host request the proof is made for: `hr_` and a ULID (`hr_[0-7][0-9A-HJKMNP-TV-Z]{25}`) |
| `issued_ms`, `expires_ms` | integers, `issued_ms < expires_ms ≤ issued_ms + 600 000` (a host request lives at most 600 s) |
| `dev` | boolean (§2.4) |

What it binds, and why: the workspace and the person (no other workspace or person can use it); the op and the full
payload (the hub, its root, the mode, the `binding_seq` and the aliases are exactly what the person reviewed; the
payload is shown verbatim in the review, orch-v2-host §0.8.1); the request id (one proof, one request: the host
refuses it on any other request); a short lifetime. The payload is named without the proof member, so the proof and
the event never refer to each other in a circle. It does not bind the device key that signs the event: the proof is
the authority, the device signature only carries it.

**Bytes.** `signed = "orch/v2/sig/hub-owner-proof|" || cj(o)` (the `owner_proof` entry of `signatures`; the event
`hub_joined` carries one).

**Verification, steps 1–4 (everyone).** Given the owner's `pk_pub` (from the roster at the host and at readers, from
the join's §9.5 card at the hub):

1. envelope, header, signature under `pk_pub` with `orch/v2/sig/hub-owner-proof|`, exact field set and types;
2. `person_id == person_id(pk_pub)`; `payload` has the exact shape its `op` names (above);
3. `issued_ms < expires_ms ≤ issued_ms + 600 000`;
4. the caller's bindings (below).

**Every failure of steps 1–4 is the one code `proof_invalid`** (the person is never asked to tell a bad signature
from a bad field, and nothing is echoed). Then, where the verifier has a clock: `issued_ms > now + 300 s` →
`proof_invalid`, `now ≥ expires_ms` → `proof_expired`; last, the dev marker (§2.4: `dev_refused`, `dev_mismatch`).

**At the host** (C9; reference `check_owner_proof`, vectors `owner_proof_*`), at binding and again at submit, under
the store lock: `workspace_id` is this workspace, `op` is the op being run, `cj(payload)` equals the payload the
op's event carries (without `owner_proof`), `request_id` is the request being bound or submitted; `pk_pub` is the
acting owner's `PK` from the roster (the actor must be an owner, as for every hub op). With the host's clock. A hub
op without a proof is refused before anything is signed (`proof_missing`), on every path.

**At the hub** (reference `check_join_proof`, vectors `join_proof_*`): the hub **always** requires a valid proof with
a link `join` (`op` `hub.join`) and with a `binding_put` whose binding is new or switches the mode (`op` `hub.mode`
for selective to full on the same hub, else `hub.join` for a rejoin after `hub.unbound`), whatever the host sends. A
`binding_put` of an object the hub already holds, byte for byte, and every `hub.unbound` take `null`. Otherwise a
missing or `null` proof is `proof_missing` (vectors `join_proof_null_*`), so the hub's check holds even against a host
that skipped its own. After §9.5: `workspace_id` is the card's, `op` is the link op's, and the payload's `hub_id` is
this hub's and its `hub_id`, `hk_root_pub`, `mode` and `binding_seq` are the binding's. `pk_pub` is the join's owner
key (§9.5 step 2), for a `binding_put` the pinned owner's. With the hub's clock. A leave does not pass through the hub
(`hub.unbound` goes to publish and the relay directly, §9.3); the hub sees the `hub.left` and its proof only as a
reader. The hub has no request id and no alias book to compare, and does not need them: the host and the readers do.

**Readers** (the host model, another member's core, the hub's `log_sync` view; reference
`check_recorded_owner_proof`, vectors `recorded_*`), for every `hub.joined` / `hub.left` on the workspace log. The
inputs that decide are the reader's own facts, never fields the signer or a deliverer chose: the actor's `PK` from
the roster, `prior` (the reader's own record of the last `hub.joined` / `hub.left` before this event:
`{type, hub_id, mode, binding_seq}`, `mode` `null` after a leave, or none) and `at_ms`. **`at_ms` is the append
time `at` of the host's chained, host-signed log line** (ticket-format §5.5, `host_sig` over the chain): never a
time taken from the event body the person's key signed, and never one supplied by whoever delivered the event (a
relay, the hub, a page of `log_sync`) outside that signed record. In order:

1. the `owner_proof` member (§9.1: `null` → `proof_missing`); the proof passes steps 1–4 with `pk_pub` the actor's
   `PK` (unknown → `proof_invalid`), `workspace_id` this workspace, `person_id` the actor's, `op` `hub.join` or
   `hub.mode` on `hub.joined` and `hub.leave` on `hub.left`, and `cj(payload)` equal to the event's payload without
   `owner_proof`;
2. the event's place, from `prior` → `stale_binding`: `hub.join` follows nothing or a `hub.left` and names
   `prior.binding_seq + 1` (0 + 1 with nothing before); `hub.mode` follows a selective `hub.joined` of the same
   `hub_id` and names `prior.binding_seq + 1`; `hub.leave` follows a `hub.joined` of the same `hub_id` and names its
   `binding_seq`. A `prior` that is not one of these shapes → `malformed`;
3. the proof covers the event's time without a reader clock: `issued_ms − 300 s ≤ at_ms ≤ expires_ms` →
   `proof_expired` (append times are whole seconds and the signer's clock may lead the host's);
4. the dev marker.

**Replay.** Steps 2 and 3 together close it without a clock: a proof that landed is dead, because its place is taken
(a later join must follow a leave and name a higher `binding_seq`, a second leave cannot follow a leave: vectors
`recorded_replayed_proof_on_later_event`, `recorded_left_after_left`); a proof for an abandoned attempt dies with its
≤ 600 s life, because the host-signed append time must fall inside it (vector
`recorded_abandoned_proof_replayed_after_expiry`); at the host the `request_id` also ties it to one request.

**The terminal path.** `orch hub join / mode / leave` at the terminal (the CLI backup) asks for the recovery code
too, derives `PK`, attaches the proof and wipes both, exactly like the dashboard's signer. A passphrase device key is
not a substitute: the proof is required on every path.

**Rollout.** The card's `reader` ships now on both sides (it changes the signed card, so hub and core deploy together;
a dev hub re-issues its card, `reissue_card`, to get one). The `owner_proof` payload member, the `join` and
mode-switch `binding_put` bodies that carry the proof (§10.2), the host checks and the signer ship together with
orch-core C9/C10, because they change what core sends (the orch-core C9 host PR is merged together with this one). A
proof lives at most 600 s, so the host connects to the hub in the same flow as the signing (`orch hub join` and `orch
hub mode` do). If the hub still answers `proof_expired` (it was down; the log already says joined but the hub never
took the binding), the way out is `orch hub leave`, then `orch hub join` again with a new proof; the host drops the
unfinished binding and the new join starts clean.

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

`seq` ≥ 1 per direction and link, one counter for requests and replies alike. The hub enforces it on w2h frames (D12b): the first frame of a link sets the baseline (a host numbers across reconnects), every later frame must be exactly the previous + 1, otherwise the hub answers `malformed` and closes the link before any side effect. The seq is not bound to the link: replay of a frame onto another link needs a TLS break first (L7 SHORTCUT). `re` is `null` on a request and the request's `seq` on its reply; `body` an object.

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
| `join` | w2h | both | `{"invite", "card", "pk_pub", "binding", "owner_proof"}`: the v2 card, the owner's PK, the binding (§9.5), the owner proof (§9.6: the member is always present; `null` → `proof_missing`) | `{}` |
| `card_put` | w2h | both | `{"card", "pk_pub"}`: the workspace's v2 card, checked against the join pin, forwarded to orch-publish (§11.2 "Admission through the hub"); refusals: `other_person`, `other_workspace`, `wsk_mismatch`, `stale_card`, `not_bound`, and orch-publish's (`unknown_workspace`, `owner_not_allowed`, `workspace_blocked`, `delegation_changed`, `delegation_refused`, `delegation_mismatch`, `bad_signature`, `unavailable`) | `{}` |
| `binding_put` | w2h | both | `{"object", "owner_proof"}`: a `hub.binding` or `hub.unbound`, forwarded as is; `owner_proof` is always present: the proof (§9.6) for a binding that is new or switches the mode, else `null` | `{}` |
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
| `switch_ticket` | w2h | both | `{"origin", "return_to", "kind": "open"\|"signout"}` | `{"ticket", "url"}` (hub one-app, below) |
| `switch_redeem` | w2h | both | `{"ticket"}` | `{"user_id", "name", "return_to", "kind"}` (hub one-app) |
| `rail_get` | w2h | both | `{"origin"}` | `{"hub": {name, origin, network_origin}, "user_id", "workspaces": [{workspace_id, name, role, online, badge, origin, current}]}` (hub one-app) |
| `profile_put` | w2h | both | `{listed, tags, description, handles, not_handled, accepts, contact, languages, hours}` | `{}` |
| `directory_get` | w2h | both | `{query?: string}` | `{entries: [...]}` (at most 50) |
| `status_put` | w2h | both | `{"version", "commit", "host"}` | `{}` |
| `addon_request` | w2h | both | `{"name", "version", "actor"}` | `{}` (M3 #1450) |
| `owner_get` | w2h | both | `{}` | `{"user_id"}`: the hub user this hub names as the owner of the calling workspace (V3-D19 owner alias; FB15 §8.8b) |
| `log_sync` | w2h | full | `{"epoch", "log", "from_seq", "count", "head", "reset", "sealed": b64u}` | `{"seq", "head"}` (p4, §10.7) |

A reply's body is either the reply body above or `{"error": <code>}`; H0 fixes request bodies only (reply bodies are
built in D5/C5 against this table). Phases: `snapshot_get`, `publish_out` and the selective ops in phase 2 (share ops
carry needs-you summaries only until C6); `route_*`, `wk_seal`, `audit_ack` in phase 3; `event_deliver`, `log_sync`, `space_proposals` in phase 4.
`log_sync` carries the workspace's own log **to** the hub; like every other op it asks the hub to sign nothing, and
nothing in it can become signed bytes: the hub builds custodial events only from an action a channel started (§8.4).

`profile_put` replaces the pinned, bound workspace's display profile (exact keys): `listed` is boolean;
`tags` and `accepts` are lists of at most 8 strings of at most 24 characters; `handles` and `not_handled`
are lists of at most 8 strings of at most 80 characters. `description` is at most 300 characters,
`contact` 80, `languages` and `hours` 40. All strings pass through `render_text`, stripping controls,
bidi controls and zero-width characters. Profile text is untrusted display data, never instructions,
and is never used for authorization or routing. Unbinding, revocation and forgetting delete the profile.
`directory_get` accepts an optional query of at most 80 characters, matched case-insensitively against
name, tags, description and handles. Entries contain `workspace_id`, `name`, `online` and all profile
fields except `listed`. Only other bound, explicitly listed workspaces for which the live routing
`Authz.may_route(caller, other)` allows access appear; authorization is evaluated on every request.
Selective mode does not imply listing or bypass routing authorization.

`addon_request` (M3, #1450): a host says an add-on install was refused by the hub policy (`market.addon_not_allowed`): `name` `[a-z][a-z0-9-]{0,39}`, `version` `[0-9A-Za-z.+-]{0,32}` or empty, `actor` `person` | `agent` (known at the host, shown, never trusted). Display data only: one row per (workspace, add-on) with a counter, at most 50 add-ons per workspace. It grants nothing; an admin's approval is an audited policy change and the owner's local grant is still needed. The per-workspace allow list narrows the hub-wide policy in that workspace's snapshot (intersection; `block` and `ceiling` never change).

`status_put` reports display-only workspace metadata over its authenticated WSK link, once per host process/hub.
Its exact body has a version matching `[0-9A-Za-z.+-]+` (at most 32 characters), a hex commit (at most 40,
possibly empty), and a host string (at most 80); bad shapes are `malformed`, unknown pins `unknown_workspace`.
The hub sanitizes these fields and records its own last-seen clock and socket peer address. These values never
influence authorization, routing, signing or mode, and are not written to the workspace log.

**Hub one-app ops (27 Oct, prototype).** `switch_ticket`, `switch_redeem` and `rail_get` let the workspace dashboard and the
hub console feel like one app. The hub names the user itself: always the link workspace's owner alias (`no_owner_alias`
otherwise), so the host can never ask for another person. `origin` is the dashboard origin as the browser sees it
(`scheme://host[:port]`, nothing else; `malformed` otherwise); the hub keeps the last one per workspace. A ticket is an opaque
128-bit id the hub stores in memory only: single use, 30 s, bound to a user and a direction. `switch_ticket` returns a
hub-console ticket (`url` = `<console origin>/#t=<ticket>`, fragment only); `switch_redeem` spends a ticket the hub made for
THIS link's workspace (`<workspace_id>.<hex>`) and answers `bad_ticket` for anything else (unknown, used, expired, other
workspace, user no longer the owner alias). A ticket for this workspace whose user is not the owner alias is refused
WITHOUT being used; one presented at another workspace's link is spent. `return_to` is a relative path (`/`, no `//`, `\\`, `..`), else `/`. `kind`
`signout` ends the browser's session at the target instead of opening one. These ops sign nothing.

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
            item_version ≥ 1, issued_ms, chain: {log, seq, head}, content: <exact per item_kind>, f1?, dev}
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
  `view_fork`: the hub or the link changed the item). The field in the event is `item_hash` (§8.1, §8.6).
- **The `f1` block (C3b addendum, optional, additive).** The F1 values a selective custodial build needs that the item
  does not already carry (`based_on` is `chain.head`). Filled by the host at share time from the same log position as
  the item. An item without `f1` is valid and unchanged (every earlier `share_item_*` vector stays valid); the hub
  cannot build on it (`unavailable`, §8.6). Exact keys at every depth:

  ```
  f1 = {"roster_v": int ≥ 0,
        "gates":     {<gate>: {"gen": int ≥ 0, "hash", "policy_hash", "source_sha"?}},   gate ∈ requirements, plan, code, verify
        "questions": {"Q<n>": {"hash"}}}                                                   at most 64
  ```

  `hash`, `policy_hash` are hashes (`sha256:` + 64 hex): the gate hash and policy hash (ticket-format §5.6) of the
  current generation, and the question hash. `source_sha` is present **exactly** on `code` and `verify`: the source
  list (ticket-format §5.7) `[{repo (one line, ≤ 512 bytes), ref ("refs/heads/…"), sha (40 or 64 hex)}]`, sorted by
  `repo`, one entry per repo, at most 32. `f1` is allowed only on an item whose `chain.log` is a ticket (`malformed`
  otherwise). A host includes only the gates and questions the item is about (a needs-you gate item: that gate). **The
  host puts the item again with a higher `item_version` whenever a value in its `f1` changes** (a member change
  raises `roster_v`, a new generation, a changed policy or source list, a re-asked question); until then events built
  on the old values are refused at admission (§8.6). Reference `check_f1`, vectors `share_item_with_f1`,
  `share_item_f1_*`.
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
  (`contracts/relay/protocol-v2.md`; to be applied there with this contract): `space-sk` | recipient: workspace (`WXK`)
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
           type: "custodial" | "grant" | "membership" | "space" | "channel" | "share" | "market_policy", scope_tag: b64u(32) | null,
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
  otherwise (vectors `audit_share_*`); `channel` (p4) `{channel_id, user_id, change: register | revoke | renew, factor,
  actor_user_id | null, nonce}`, hub-wide (`scope_tag` null; `actor_user_id` null = the operator at the hub terminal,
  there is no `unrevoke`, §6.2; `renew` = a new certificate for the same channel key, the factor unchanged, recorded BEFORE it is issued and refusing the renewal when the chain cannot be written. **Contract text only in V3: no hub renews**, the certificate lasts 31 days and then the phone pairs again); `grant` `{group, workspace_id, role | null, actor_user_id, nonce}`; `membership`
  `{group, user_id, added, actor_user_id, nonce}`; `market_policy` (D14, §7.1) `{version ≥ 1, policy_hash: b64u(32), actor_user_id, nonce}`, hub-wide (`scope_tag` null; the version the change produced and the hash of the new policy, the actor is always a hub-admin); `space` `{space_id, epoch, change: create | add | remove | unbind | delete,
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
holds no `WK_e` (D80) and builds only from shared items (§8.6).

**The page.** Body `{"epoch", "log", "from_seq", "count", "head", "reset", "sealed"}`: `log` is `"workspace"` or a
ticket ULID; the lines `from_seq … from_seq + count − 1` (1 ≤ `count` ≤ 256) of that log, each **exactly** the
logged event (ticket-format §5.1, with `seq`, `prev`, `ws_seq` and `host_sig`); `head` = the event head (ticket-format
§5.5, `"sha256:" + hex(H("orch/v2/event|" || cj(event)))`) of the last line; `reset` (below).

```
K_view(e) = HKDF(WK_e, salt = "", info = "orch/v2/hub-view|" || hex(workspace_id) || "|" || dec(e))
AAD       = "orch/v2/hub-view-aad|" || suite || workspace_id (16) || u32(epoch) || lp16(log) || u64(from_seq)
            || u32(count) || head (32 raw bytes) || u8(reset)
sealed    = SALTED-AEAD(K_view(epoch), "orch/v2/hub-view-msg", AAD, cj({"lines": [event, …], "proj"?: P}))  (v2 §5.1)
```

Sealed although the link is TLS (DECIDED HERE): the hub stores the pages sealed and opens them with `WK_e` only inside a
build (D6's `WorkspaceView.build(wk_e, …)`), so its database alone reads nothing; and a host that rotated the hub out
(leave, full → selective) cannot leak later content to it by mistake: the hub has no wrap for the new epoch
(`bad_epoch`). At most 1 MiB sealed per page: **counted on the raw bytes** of `sealed` after base64url decoding
(`salt ‖ ct`, ≤ 1 048 576), so its text is at most 1 398 102 characters and the whole message fits the 1.5 MB link
frame (§10.3). A sender may count the text (stricter).

**The hub's check, in order** (reference `apply_view_page`, vectors `log_sync_*`):

1. the body's exact fields and types, `log` grammar, `1 ≤ count ≤ 256`, `head` a hash, `reset` only with `from_seq =
   1` → `malformed`;
2. `epoch` is the newest epoch the hub holds a `wk_seal` wrap for → else `bad_epoch` (the host sends `wk_seal` first);
3. position: `from_seq ≤ held + 1` for that log (with `reset`, held = 0) → else `view_gap`;
4. size → `too_large`; the seal opens under `K_view(epoch)` with that AAD → else `bad_wrap` (a page moved to another
   log, range, workspace or epoch, or a lying `head`); the plaintext is exactly `{"lines": [...]}` or `{"lines": [...],
   "proj": P}` (D15b, below) with `count` lines → `malformed`;
5. per line, in order: `seq = from_seq + i` and `host_sig` present → `malformed`; `host_sig` under the **pinned**
   `WSK` (§9.5) over `"orch/v2/sig/host-event|" || cj({contract: 1, suite: 2, workspace_id, log, event without
   host_sig})` → `bad_signature`; `prev` is the head of the line before (`null` at `seq` 1), and a line at a `seq`
   the hub already holds has the same head → else `view_fork`; a ticket line's `ws_seq` is an integer ≤ the
   workspace-log `seq` the hub holds → else `view_gap` (sync the workspace log first);
6. the last head equals `head` → `malformed`; then the view of that log is the held one extended (an overlap that
   matched changes nothing: resends are idempotent). Reply `{"seq", "head"}` of the hub's view after the page.
7. (D15b) a `proj`, after the lines, against the view after this page (reference `check_view_proj`): the exact shape
   below and a ticket log → `malformed`; `P.seq` and `P.head` are the page's last line, and `P.ws_seq` is not
   before that line's `ws_seq` → `malformed`; `P.ws_seq` is held in the workspace view → `view_gap`; `P.ws_head` is
   the held head at `P.ws_seq` → `view_fork`. The hub keeps, per ticket log, the newer `P` by `(seq, ws_seq)` (a tie:
   this page's); a page without `proj` keeps the held one. A reset of a log drops its `P`; a reset of the workspace
   log drops every `P` (with the ticket views, L2P4-9). **Any refusal here refuses the whole page**: the view stays as
   it was before the page (step 6 is not committed, D15b-4). Reference `apply_view_page_proj`, vectors
   `log_sync_proj_*`.

**The ticket projection `P` (D15b, orch-core #1043).** The five full-mode types whose derived fields need the F1
engine (§8.5: `gate.approved`, `gate.changes_requested`, `verdict.given` need `gate_gen`, `policy_hash`, the gate
`hash` and `source_sha`; `status.changed`, `ticket.moved` need `from`) take them from the host, never from a hub
computation. The lines hold every input (section hashes and `refs`, artifact digests, receipts, `branch.pushed`,
policies, people, addon bindings), but the values come from core's whole F1 replay (generations with cascades and
voids, canonical identities, effective policies, the done rule, the status table); a second engine in the hub could
drift, and then the person would see values the host refuses. So the host sends its own:

```
P = {"seq", "head",          the ticket log's last line in this page (int ≥ 1, hash)
     "ws_seq", "ws_head",    the workspace-log position P was computed at (int ≥ 1, hash)
     "status",               the ticket's status there (status list, §10.2b)
     "roster_v",             int ≥ 0
     "gates": {<gate>: {"gen", "hash", "policy_hash", "source_sha"?}}}   exactly the §10.2b f1 gate entries
```

The host computes `P` with the same code it admits custodial events with (core: the C6b `f1` values plus the status),
from one locked read together with the page's lines. `gates` lists only the gates (`requirements`, `plan`, `code`,
`verify`) whose hash the host can build now; `source_sha` exactly on `code` and `verify`. `P` adds no disclosure: every
value follows from lines a full hub already holds. **Build** (reference `build_from_proj`, vectors `proj_build_*`),
after the visibility check: `P.seq`/`P.head` are the held ticket view's last line and `P.roster_v` equals the
`roster_v` the hub counts from its workspace lines → else `unavailable` (stale: the host sends a newer `P`, sender
rule 7); `status.changed`, `ticket.moved`: `from = P.status`; a decision: `P.gates[gate]` (`verify` for
`verdict.given`) exists → else `unknown_item`, its `hash` equals `action.hash` → else `event_changed`, and the event
takes `gate_gen = gen`, `policy_hash`, `hash` and (on a `code` approval and on a verdict) `source_sha` byte for byte,
as §8.6 does from `f1`. The channel shows these values (§8.4 step 3, D15b-2). **Needs-you from `P`** (D15b-3): gate
and verdict rows come only from tickets that pass the one visibility rule above; a row carries the hash it was listed
with and the action sends that hash, never one re-read when the person taps, so a moved hash is `event_changed` and
the person reviews again. `P` does not carry eligibility (D59 author ≠ approver), so the host may still refuse an
offered decision. **`roster_v`** is counted the same on both sides: one per `workspace.created`, `member.added`,
`member.removed`, `role.changed` line (reference `roster_v`, vectors `workspace_roster_v*`, run by hub and core).
`P` authorizes nothing and is not bound into the event: the host checks the values themselves
against its own log (§8.3 step 9), so a stale or false `P` only gets an event refused (`event_changed`).

**What the hub trusts, and what it does not.** The hub verifies that every line is the host's (`WSK`) and that the
lines form one chain; it does not replay F1 authorization (devices, grants) over them. That is enough because of the
two rules that bound a hostile host (D29, spec §8a: it can mislead only within its own workspace): (a) the view only
fills fields of an event a **channel** asked for (§8.4: the action names the type and the person's choices; `log_sync`
cannot start a signature, choose a type or pick a user), and the person sees the built event before the proof; (b)
every custodial signature is bound to this `workspace_id`, log and type (§8.2), so nothing built from this view
verifies anywhere else, and the host re-checks the event against its own log (§8.3 step 9). A host that feeds a false
view only gets events about its own workspace refused by itself, or shown to and signed by a person who sees them.

**`reset`.** A host `restore` (ticket-format §5.10) abandons a suffix, so the honest log forks from the hub's view. The
host then resends the log from `seq` 1 with `reset: true`, which replaces the hub's view of that log. **DECIDED (C3b
addendum): `reset` stays on the host's word and is not tied to the `restore` event.** The view authorizes nothing: it
only fills fields of an event a channel asked for, and the host admits a custodial event only after its own
`check_built` against its own log (§8.3 step 9, core §15.5 step 6). A false reset can only make the host's own events
fail at the host (D29). Tying it to `restore` would add a second verifier of core's restore rules to the hub for no
authority gain.
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

**Sender rules (C3b addendum; answers C14, orch-core #1027).** Simplest safe rules, all on existing ops:

1. **Rotation.** The hub keeps the wrap of every epoch of the live binding (§10.2a) and every page it accepted, sealed
   under its epoch. After a rotation the host **does not resend** old lines: it sends `wk_seal` for the new epoch,
   then continues from its acknowledged position under the new epoch. A page under an older epoch is `bad_epoch`;
   the host reseals the same lines under the newest epoch and sends again (vectors
   `log_sync_rotation_continues_under_new_epoch`, `log_sync_rotation_old_epoch_refused`). A retired epoch (unbind,
   switch to selective) never opens again; after a rebind the host resends every log from `seq` 1.
2. **Size.** 1 MiB raw (above). The host splits by lines (at most 256), halving a page until it fits.
3. **A line bigger than a page** cannot occur in a valid log: a line is at most 524 288 bytes (ticket-format §11.2),
   so a one-line page seals to at most 524 288 + 44 bytes. A host that finds a longer line has a broken log: it sends
   nothing more of that log (it never skips a line: the hub cannot chain past a hole) and reports it (`orch doctor`);
   the hub refuses such a page with `too_large`.
4. **Hub state loss with nothing pending. No head-read op.** At every link open the host sends a **resume probe**:
   the workspace log's last acknowledged line again as a one-line page (`from_seq` = acknowledged `seq`). That is an
   idempotent overlap; the reply `{seq, head}` confirms the hub's view. `view_gap` (the hub lost its view) makes the
   host forget every acknowledged position and resend every log from `seq` 1, workspace log first, **without**
   `reset` (nothing is abandoned). `view_fork` on the probe means the host's log changed (a restore): the reset rule
   above. Any `view_gap` on a later page of a log likewise resends that log from `seq` 1. The host takes its
   positions only from replies (vectors `log_sync_resume_probe_*`).
5. **`reset` and `restore`:** not tied (decided above).
6. **Two links per workspace.** The hub serves one live link per workspace: a newer authenticated link replaces the
   older one, which the hub closes (§10.3 does the same for `route_in`). Each page is checked and applied atomically
   under the view lock against the view as it is then, so pages from a closing link and the new one interleave
   safely (an idempotent overlap or a `view_gap`, never a wrong view). The host runs one sync pass per workspace at a
   time.
7. **The projection (D15b).** Every ticket page that ends at that log's current head carries `P`. After any append
   (workspace or ticket log) the host recomputes `P` of every synced ticket log, and where it changed (compare a
   digest of `cj(P)` without `ws_seq`/`ws_head`) it sends the log's last line again as a one-line overlap page with the
   new `P` (an idempotent overlap that only refreshes `P`; vector `log_sync_proj_refreshed_by_overlap`). The workspace
   log is synced up to `P.ws_seq` first.

**Size cap.** At most 64 MiB sealed per workspace on the hub; a workspace whose log exceeds it answers `too_large` on
every page, and a `reset` refills to the same cap, so the host must keep its synced logs under it (hardening: a
rate-limited, cached projection and a global cap).

**Not here:** the hub's projection code beyond `P` (roster version, questions, ticket fields, people) is the hub's; it
never computes F1 gate values or the status (D15b). Body text and artifact bytes are not synced in hv 0 (custodial `ticket.updated` is `set` only, §8.5), so the channel shows section hashes, not text, for
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
of this subsection, as for v2 push (after authentication and the §10.8 target checks), **except the mailbox purge**
`DELETE /v2/hubs/{hub}/mailbox/{ch}`, which is **not subject to the push-configuration `404`** (E5R-1, the same
wording as the §10.8.3 hub cut-over routes): it is the hub's revocation notice (§10.8.4), and the revocation record never
depends on push. The exemption is decided by method and path shape (`DELETE`, exactly `mailbox/<one segment>`); the
purge then follows the ordinary order of refusals above (a bad `{ch}`, a body or a query is `400 malformed`), records
the revocation and purges what is held. Reference `push_exempt` and `route(..., push_configured=False)`, vectors
`relay_route_nopush_*`.

| Route | Signed | Body | Answer |
|---|---|---|---|
| `POST /v2/hubs` | no (the card) | the hub card (§5) | `200 {hub_id, op_cert_id, op_seq, card_seq, created}`; `403 not_allowed` (operator allowlist), `429 quota_exceeded`; the rest per §10.8 |
| `PUT /v2/hubs/{hub}/push/{ch}` | yes | `{v, env, topic, token}` | `200 {channel_id, registered: true}` |
| `DELETE /v2/hubs/{hub}/push/{ch}` | yes | empty | `200 {channel_id, registered: false}` (also when none was held) |
| `POST /v2/hubs/{hub}/push/{ch}/send` | yes | `{v, outer, priority, ttl_s}` | `200 {accepted: 0 or 1}` |
| `PUT /v2/hubs/{hub}/mailbox/{ch}/{id}` | yes | `{v, c, ttl_s}` | `200 {id, stored: true, expires_ms}` |
| `GET /v2/hubs/{hub}/mailbox/{ch}/{id}` | yes | empty | `200 {id, c, expires_ms}`; `404 not_found` |
| `DELETE /v2/hubs/{hub}/mailbox/{ch}/{id}` | yes | empty | `200 {id, deleted: true}` (also when absent) |
| `DELETE /v2/hubs/{hub}/mailbox/{ch}` | yes | empty | `200 {channel_id, deleted: n}` (all messages of the channel); **also marks the channel revoked for this hub, permanently** (§10.8.4) |

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
is gone (`404 not_found`). **The channel's own read** (open point O1, closed by the B stream) is §10.8.4: a
channel-signed request under its own prefix; nothing in this subsection gives a channel a read. **What the hub leaves
there:** every hint it pushes (§10.8.2), under the same `id` and with the same `c`, so a phone without push (APNs down,
the Simulator) catches up from the mailbox and handles each id once.

**Revocation.** When the hub revokes a channel (§6.3, §7) it sends `DELETE .../mailbox/{ch}`, then
`DELETE .../push/{ch}` for that channel (the revocation record first, E5R-1). The mailbox purge is the hub's signed revocation notice to the relay (B
stream): the relay records `(hub, ch)` as revoked, permanently (like §6.2, a revoked channel id never comes back), and
refuses that channel's own requests from then on (§10.8.4). The hub sends the purge only on revocation. Both are idempotent, so the hub repeats them until the relay answers `200`. The purge is answered and recorded
whether or not the relay has a push configuration (above); the push `DELETE` is `404` on a relay without one, which
the hub treats as done (no push target can be held there).
The hub also stops sending to the channel at once (it asks its own revocation list, not the relay).

**Retry rules for the hub.** Every attempt is a new request: new `ts_ms`, new nonce (a replay is `401 replay`). It retries
on a network error, `429`, `503` and `5xx` with a bounded backoff that honours `Retry-After`, and on
`401 stale_timestamp` once, after correcting its clock by `server_ms`; every other `4xx` is final. All routes are
idempotent (`PUT`/`DELETE` by state, `send` and mailbox `PUT` by `id`).

### 10.8.2 The sealed push payload

**Purpose.** The relay and APNs see ciphertext only; the phone's notification service extension opens the payload with the
channel's push key (§6.4). Construction: v2 §5.3 unchanged (`orch/v2/seal|`, `orch/v2/seal-aad|`) with

| Part | Value |
|---|---|
| purpose | `hub-push` |
| recipient | the channel's registered push key `push_pub` (§6.4, B amendment; not the certificate's signing key); recipient id = `channel_id`. The hub sends no hint to a channel without a held push key |
| object id | the message id `id` (16 bytes, the same as in `outer` and the mailbox path) |
| epoch | `0` |
| extra | `hub_id` (16 bytes) |
| plaintext | `cj({v: 2, hv: 0, kind: "hub.push", type, ts_ms, ref})` |

`sealed = b64u(eph_pub || AES-256-GCM(K, 0-nonce, plaintext, AAD))` with a fresh ephemeral key per seal (vector
`phase4.relay_routes.push_seal`, sealed to `push_alice_phone`; `open_push` is the reference for the extension). `type` is one of `prepare` (a custodial
prepare is waiting for this user, §8.4) and `needs_you` (a change the user has to look at); `ref` is a 32-hex opaque id
(the prepared id or ticket uid, so the app can open the right item) or `null`. **The payload is a hint, not an authority:**
the seal is anonymous (anyone who knows a channel's push key can make one), so the extension shows generic text at most
and the app acts only on what it fetches from the hub over its authenticated channel, never on this payload. The `id`,
`ch` and `hub` in `outer` are not secret; the `type` and `ref` are. **The phone opens a hint only** when `outer.hub` and
`outer.ch` are its own pairing's, the seal opens, `ts_ms` is within [now − 24 h, now + 300 s] and the `id` was not
handled before (push and mailbox share one replay store) (L2 D10-3).

### 10.8.4 A channel's own mailbox read (phase 4, B stream; closes O1)

**Purpose.** The phone reads what its hub left in its mailbox (§10.8.1) without the hub in the path. The relay knows no
channel keys, so the request carries the channel's certificate, which the relay verifies under the held card of the
hub, exactly as a host does (§6.2).

```
signed bytes = "orch/v2/hub-channel-relay-req|" || suite (1) || hub_id (16) || channel_id (16) || u64(ts_ms)
               || nonce (16) || lp16(METHOD) || lp16(target) || H(body)
headers      = Orch-Channel-Cert: b64u(cj(<signed hub.channel_cert>)) (≤ 4096 chars), Orch-Channel-Ts: <ts_ms decimal>,
               Orch-Channel-Nonce: <32 hex>, Orch-Channel-Signature: b64u(Sign(channel_key, signed bytes))
```

`hub_id` and `channel_id` are the target's segments. **Routes** (their own prefix: nothing under `/v2/hubs/` is ever
channel-signed): `GET /v2/hub-channels/{hub}/{ch}/mailbox` → `200 {messages: [{id, expires_ms}]}` (every held message,
at most 256, sorted by `id`); `GET …/mailbox/{id}` → `200 {id, c, expires_ms}` or `404 not_found`; `DELETE
…/mailbox/{id}` → `200 {id, deleted: true}` (also when absent; the phone deletes after it handled the id). Bodies are
empty. Emptying the whole mailbox stays the hub's (`DELETE` of the list is `404 not_found`).

**What the relay checks, in order** (reference `check_channel_request` then `channel_route`, vectors
`channel_request_*`, `channel_route_*`):

| # | Check | Refusal |
|---|---|---|
| 1 | header syntax (as §10.8; each `Orch-Channel-*` header exactly once in any spelling, L2B-9; the certificate b64u of a strict JSON object), method `GET` or `DELETE`, the target canonical (§10.8 rules, no query) and under `/v2/hub-channels/<32 hex>/<32 hex>/` | 400 `malformed` |
| 2 | `\|now − ts\| ≤ 120 s` | 401 `stale_timestamp` (`server_ms`) |
| 3 | the held, current card of the target's hub, verified again for this request (§10.8 step 3) | 401 `unknown_hub` |
| 4 | the certificate under that card (§6.2: signature, `hub_id`, the card's op cert, time, dev marker, `channel_id` of `key_pub`); it names the target's channel | 401 with its code (`bad_signature`, `other_hub`, `stale_op_cert`, `cert_invalid`, `cert_expired`, `dev_*`); 403 `other_user` |
| 5 | the signature under the certificate's `key_pub` | 401 `bad_channel_signature` |
| 6 | the channel is not revoked by its hub (the hub's signed mailbox purge, §10.8.1) | 403 `channel_revoked` |
| 7 | the nonce is unseen for this `(hub, channel)`; then recorded for 240 s | 401 `replay` |
| 8 | the route: another path or method `404 not_found`; a body `400 malformed` | — |

**Revocation (DECIDED HERE).** The relay keeps, per hub, the channel ids the hub revoked: the hub's `HK_op`-signed
`DELETE /v2/hubs/{hub}/mailbox/{ch}` (§10.8.1), which it sends on every revocation and repeats until `200` (and again
at start for every revoked channel, L2 D10-6), records `ch` permanently. Step 6 then refuses the channel at once, not
when its certificate expires (31 days). The check comes after the signature, so only the key holder learns it. **The relay's
`channel_revoked` is unsigned and never wipes the phone** (L2B-2): on it the phone drops its cached mailbox data, stops
using the channel and asks its hub over the authenticated channel (§6.4); it wipes the pairing only on the hub's
answer (B6) or when the user chooses Leave. The record is kept whether or not the relay has a push configuration
(§10.8.1, E5R-1). A relay may rate-limit reads per `(hub, channel)`.

### 10.8.3 Cut-over of a workspace's direct relay path (phase 4, relay E3, orch-core #896)

**Purpose.** Until H5 a workspace keeps its direct (v2) relay path. When it moves behind a hub, the relay closes that
path for it. The relay holds no hub ↔ workspace binding, so **the workspace names the hub first and the hub only
confirms**: a hub alone can never close (DoS) someone's direct path, redirect it or reverse it.

**Workspace side** (v2 relay session of the workspace itself, i.e. its `WSK`; a device or any other session is
`403 not_eligible`, a `GET` also from a member device). **`PUT` and `DELETE` are fenced like every durable workspace
write: they require the current `Orch-Holder` generation, else `409 fenced`; `GET` is unfenced** (L2-E3-1). A `PUT`
naming a `hub_id` that has no account at this relay is `403 unknown_hub` (to the workspace only; reference input
`registered_hubs`, vectors `cutover_workspace_unregistered_hub`, `…_renames_to_unregistered_hub`). A `pending` row
expires 24 h after it was written (it reads as `direct`; the hub then gets `other_hub`), so a hub cannot confirm at a
moment of its choosing long after the owner's act (L2-E3-4).

| Route | Body | Effect / answer |
|---|---|---|
| `PUT /v2/workspaces/{ws}/cutover` | `{v: 2, hub_id, state: "pending" \| "cut_over"}` | `pending`: names (or renames) the hub; `cut_over`: closes the direct path at once (the workspace's own act). `pending` while `cut_over` is `403 cut_over` (naming never weakens a cut-over; `DELETE` first). Answer: the state below |
| `DELETE /v2/workspaces/{ws}/cutover` | empty | back to `direct` (only the workspace reverses) |
| `GET /v2/workspaces/{ws}/cutover` | empty | `{state: "direct" \| "pending" \| "cut_over", hub_id, cut_over_ms, drain_until_ms, purged}` (`direct`: `state` only) |

**Hub side** (signed under §10.8 and checked in its order before the route; then the §10.8.1 body rules; not subject
to the push-configuration `404`):

| Route | Body | Answer |
|---|---|---|
| `PUT /v2/hubs/{hub}/workspaces/{ws}/cutover` | `{v: 2, state: "cut_over"}` | confirms `pending` → `cut_over` for a workspace that named **exactly this hub**; a repeat on `cut_over` is `200` and changes nothing (times kept). Answer: the state |
| `GET /v2/hubs/{hub}/workspaces/{ws}/cutover` | empty | the state, only for a workspace that named this hub |
| `DELETE /v2/hubs/{hub}/workspaces/{ws}/cutover` | empty | always `403 not_allowed` |

- An unknown workspace, one that named no hub (`direct`) and one that named another hub all answer the hub
  `403 other_hub`, so a hub learns nothing about workspaces that did not name it. `{ws}` not 32 lower-case hex, a
  query, or another body is `400 malformed`; another method `404 not_found`. **DECIDED HERE (changes to E3):** the
  hub's `PUT` carries `{v: 2, state: "cut_over"}` (not an empty body), like every other signed `PUT` of §10.8.1, so the
  signed bytes name the act; and a workspace `pending` over a `cut_over` is `403 cut_over` (E3: `malformed`).
- **After `cut_over`: one gate over every per-workspace direct route** (L2-E3-2), with an explicit allow-list.
  Allowed: reads of the direct mailbox and the host's hold/poll **during the drain window** (relay configuration,
  default 7 days); push unregistration (cleanup); the workspace's own cut-over `GET` and `DELETE`. **Everything else**
  under the workspace (bridge and pairing, direct envelopes, push registration and send, hooks, Drop, `cert-requests`,
  checkpoints, card and membership writes, and any route added later) is `403 cut_over` (details `drain_until_ms`,
  `drained`), at once. At the end of the drain the relay purges the workspace's direct mailbox, replay records, push
  registrations, hooks and Drop data; after that the mailbox reads are refused too. The gate runs **after**
  authentication: an anonymous or unauthenticated request (e.g. an anonymous pair request) gets its ordinary refusal
  with no details and never learns the cut-over state or its times (L2-E3-5). Short-lived bridge data may be gone
  long before the drain ends (60 s expiry); the drain only keeps reads possible, it stores nothing extra (L2-E3-6). A
  reversal (`DELETE`) after the purge leaves devices without push until they re-register (core tells them to).
- Reference `relayroutes.route` (`cutover_put`/`cutover_get`), `cutover_hub`, `cutover_workspace`; vectors
  `relay_route_cutover_*`, `cutover_*`, and the signed request bytes `phase4.relay_routes.cutover_put`/`cutover_get`.

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

**Direct requests (V3-D36).** There are none. orch-publish refuses a request without `Orch-Hub` and `Orch-Hub-Signature` as
`403 hub_required` (half a pair is `400 malformed`), and `POST /v1/ws/{ws}/register` no longer exists: a workspace is
admitted only through its card (below).

**orch-publish's hub routes** (no transport authentication, V3-D16: every object authenticates itself): `PUT /v2/hub/card`
(`hub.card`), `PUT /v2/hub/op-revoked`, `PUT /v2/hub/bindings/{ws}` (`hub.binding` / `hub.unbound`, also sent by the host
directly after a leave), `PUT /v2/hub/cards/{ws}` (below) and `GET|HEAD /v2/hub/releases` (§10.6).

**Admission through the hub (R5a, V3-D36).** A workspace reaches orch-publish only through `card_put` (§10): after the
hub's own checks (pin, never-change rules, `card_seq`) it forwards `{card, pk_pub}` to `PUT /v2/hub/cards/{workspace_id}`.
Publish verifies the card itself (owner `PK` delegation, `WSK` card signature, owner = `pk_pub`, workspace = route) and applies
the admin's pre-bind, the owner allowlist and the block, exactly as the former direct register did; what it keeps is the
permanent delegation (`delegation_changed`, 409, if it ever differs). It also keeps the highest `card_seq` ever admitted per
workspace (across an unbind) and refuses a lower one as `stale_card`; an equal or higher one, also the very same card after
an admin re-pre-bind, is admitted. There is no age limit: the card carries nothing publish does not already hold. Refusals
come back through the hub as the `card_put` error: `unknown_workspace`, `owner_not_allowed`, `workspace_blocked`,
`delegation_changed`, `stale_card`, `other_person`, `other_workspace`, `delegation_mismatch`, `delegation_refused`,
`bad_signature`, `malformed`, `wrong_suite`. Vectors `ws_card_admission_*`.

**What the card route authenticates, and what it does not.** The card binds the workspace id, its owner (`PK` signed the
delegation) and its `WSK` (the card is `WSK`-signed). Nothing binds a hub: any party holding the card (a hub, a former hub,
anyone who saw it) can push it, and a hub the workspace never joined can push it. That admits only a workspace the admin
already pre-bound to that owner, with its own genuine `WSK`, and grants **no publish capability**: publishing still needs a
`WSK`-signed `hub.binding` naming the hub and the hub's transport signature (§11.2).

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
`publish_approval_stripped_by_hub` (`bad_signature`).

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
| orch-relay (phase 4) | hub cards, bindings, hub requests (§10.8), channel requests and their certificates (§10.8.4) | hub accounts by card, op revocations, hub and channel request nonces |
| orch hub | the v2 card and binding at join (§9.5), link auth, w2h link messages, bindings it forwards, channel registrations (§6.3), actions (§8.5), every `log_sync` line (§10.7), channel proofs before it signs | its keys, users, channels, snapshots, audit, the `workspace_id → (owner, WSK)` pins, the sealed views |
| channel (phone, browser; p4) | the prepared context against its digest (§8.4); the phone also the card against the pin of its pairing link and its own certificate under it (§6.4) | its channel key; the phone also its push key, pin and held card |

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
| `conflict` | admin console (`PUT /admin/market-policy`) | `base_version` is not the stored version (409) |
| `bad_channel_proof`, `factor_mismatch`, `factor_too_weak` | custodial | §8.3 |
| `wsk_mismatch`, `not_yet_valid`, `binding_conflict`, `stale_binding`, `unbound` | binding store | §9 |
| `alias_repointed` | `hub.joined` | §9.1 |
| `proof_invalid` | owner proof | §9.6: the proof does not verify under the owner's `PK`, names another workspace, person, op, payload or request, lives longer than 600 s, or is issued in the future; one code for all of them |
| `proof_expired` | owner proof | §9.6: `now ≥ expires_ms` at the host or the hub; at readers, the event's append time is outside the proof's life (step 4) |
| `proof_missing` | owner proof | §9.1, §9.6: a `hub.joined` / `hub.left`, a hub op at the host, or a link `join` / mode-switch `binding_put` at the hub without a proof (`null`); no exemption |
| `reader_downgrade` | card | §5 step 7: a newer card declares a lower `reader` than the held one |
| `hub_too_old` | card at join (host) | §5 "Reader level": the card's `reader` is below the workspace's `required_reader`; the host's `sign.convenience_readers` reason |
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
| `foreign_user` (p4, C3b) | custodial, `event_deliver` reply | the event's `user_id` belongs to another hub of this workspace (aliased there, or first recorded under it) and not to the signing hub: a hub may not present another hub's user ids (§9.1, L2P4-C1, L2-C3-2). Checked after the certificates |
| `not_custodial` (p4) | action, custodial | §8.5: the type is not in the custodial table for that log |
| `event_changed` (p4) | channel, hub `sign` | §8.4: the digest does not match the shown context, or the hub's view moved since `prepare` |
| `stale_version`, `too_many_items` (p4) | selective `share_put` | §10.2b: an equal or lower `item_version` (other bytes, or a revoked one); over 1000 live items. Reused there: `bad_wrap`, `too_large`, `other_hub`, `other_workspace` |
| `no_session`, `key_in_use` (p4) | channel registration | §6.3: the challenge was not opened inside a live session of the user; the key is registered to another user. Reused: `channel_revoked` (a revoked key never registers again) |
| `unknown_channel` (p4) | hub CLI/API, local | §6.3: a revocation of a channel id this hub never registered |
| `challenge_invalid` (p4) | channel registration | §6.3: not the challenge the hub issued, or expired. Reused there: `factor_mismatch` (the attestation is not the gate the hub checked), `other_user`, `other_hub` |
| `bad_invite`, `unavailable`, `internal` | link reply | `join` with an unknown, used or expired invite; an op whose backend (e.g. publish forwarder) is not available; an unexpected hub-side failure (no detail) |
| `not_bound` (403), `unknown_hub` (401), `bad_hub_signature` (401) | publish, admin read, relay request | §11.2, §10.6, §10.8 (with `stale_op_cert`, `replay`, `other_hub`). Statuses are per path: on relay registration and the public card read `unknown_hub` is 404 and `stale_op_cert` 409 (§10.8) |
| `unknown_hub` (403) (p4) | relay, §10.8.3 | a workspace cut-over `PUT` names a hub with no account at this relay (to the workspace only) |
| `cut_over` (403) (p4) | relay, §10.8.3 | the workspace's direct relay path is closed (cut over to a hub); also a workspace `pending` over a `cut_over` |
| `not_found` (404), `message_conflict` (409) (p4) | relay routes | §10.8.1: an unknown route, method or message; another body under a held mailbox `id`. Reused there: `malformed`, `too_large` (413), `quota_exceeded` (429), `overloaded` (503), `not_allowed` (403, registration) |
| `approval_required`, `bad_approval` (403) | publish | §11.3 |
| `other_origin` (p4, B) | phone, local | §6.4: the hub's signed API origin is not the pairing link's; nothing is sent |
| `bad_channel_signature` (401) (p4, B) | relay, §10.8.4 | a channel request whose signature does not verify under its certificate's key. Reused there: `malformed`, `stale_timestamp`, `unknown_hub`, the §6.2 certificate codes, `other_user`, `channel_revoked` (403, revoked by the hub's mailbox purge), `replay`, `not_found` |

Hub web API (local, `/api/v1`, V3-D13; answers are `{"error": code}`; none of these travel on the link):

| Code | Status | Meaning |
|---|---|---|
| `bad_credentials` | 401 | sign-in: user id or name, password or TOTP wrong (one code for all, and for an unknown user or name) |
| `rate_limited` | 429 | sign-in (per user and source, per source, all sources, or all hash slots busy), prepare, sign |
| `unknown_prepared` | 404 | `prepared_id` unknown, used, or another user's (same answer) |
| `prepared_expired` | 410 | the prepared request is past `expires_ms` |
| `too_many_channels` | 429 | the user holds the maximum of active channels |
| `weak_password` | 400 | password policy (admin set-up) |
| `unauthorized` | 403 | a pairing or credential action by someone other than the user |
| `unknown_user`, `unknown_group` | 404 | admin routes naming a user or group that does not exist |
| `exists`, `channel_exists`, `reserved_group` | 409, 409, 400 | a user or group name taken; a channel key already registered; a built-in group name |
| `bad_pairing_code` | 400 | phone registration: wrong, used or expired pairing code |

Selective item answers (local, orch-hub#57; no new signed bytes, no new codes). `GET /me/needs-you` rows and
`GET /workspaces/{ws}/items/{id}` of a selective workspace carry, besides `content` and `item_hash`, the verified
`chain` (`{log, seq, head}`) and `f1` of the `share.item` (§10.2b) unchanged (`f1` is `null` when the item has none).
Workspace text goes out through `render_text`. `signable` is `true` only for the owner alias (the only reader of a
selective workspace's items) and only for a ticket-log item whose `f1` names exactly one target (one gate or one
question), the one `build_selective` (§8.6) can build; a `needs_you` summary, an item without `f1`, or an empty `f1` is
never signable. `chain` and `f1` are display data for the phone's comparison with the prepared event; the hub build
and the host stay the authority.

B stream (§6.4) reuses: `unknown_channel` (404, a bind naming no channel of the session's user), `not_allowed` (403, a
bind of a non-phone channel), `challenge_invalid` (400, `session_ref` of another session), `channel_revoked` and
`cert_expired` (403, told only to a holder of the key or a channel session), `forbidden_role` (403, a channel session
outside its scope), `replay` (409, an older push key).

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
| `hub_joined` | the owner-signed workspace-log event and its ticket-format §5.3 bytes; a One-click (`auth: "convenience"`) join that carries its `owner_proof` (§9.6) |
| `publish` | a forwarded request: `wsk_signed_hex` (hub-path bytes), `approval_hash_hex`, `hub_signed_hex`, `request_hash` |
| `link_auth` | challenge, exporter, signed bytes, signature |
| `audit`, `admin_read` | V3-D4 / F3: a 4-record chain with its events and hashes; the admin read signed bytes, a request and a row |
| `spaces` | V3-D8: the space key per epoch, `sk_commit`, one sealed item (salt, derived key, AAD, plaintext object) and one wrap (ephemeral seed, AAD) |
| `phase4` | p4: the `log_sync` lines, heads, view key, AAD and one sealed page; a ticket projection `proj_ticket`, a ticket page carrying it and its plaintext bytes (D15b); a prepared context and digest; a relay request and its bytes; the relay routes' request bytes and one sealed push payload (`relay_routes`); the custodial type table; the `space.accept` built from the vector proposal |
| `share_item_f1` | C3b addendum: a ticket item with an `f1` block and its `item_hash` (§10.2b, §8.6) |
| `phase4.channel_api` | B stream: a pairing link and its fingerprint, a signed `hub.api_origin`, a `session_ref`, a signed `hub.channel_auth` and `hub.channel_push`, a channel → relay request with its signed bytes, the mailbox list answer |
| `cases` | 1064 cases (269 positive, 795 negative; 144 of them phase 5: §8.8, and #1198 C8 §5 and §9.6): `check`, `args`, `expect` (`{"ok": …}` or `{"error": code}`), `phase` |

The negative cases cover, per object where it applies: wrong domain, wrong signer, expired, binding for another
workspace, unbound after leave, missing dev marker, dev at a real verifier, extra or unknown field, non-canonical
encoding (b64u padding, upper-case hex, duplicate key, float), wrong suite, future `hv`, replays and stale versions.

Every implementation runs the cases of the checks it implements: the hub all of them; orch-core `card`, `op_*`,
`snapshot`, `uk_cert`, `channel_cert`, `custodial`, `joined_payload`, `link` (h2w), and from phase 4 `space_proposal`
and the host side of `log_sync` (it builds the pages the vectors open); orch-publish `binding_store`,
`publish`, `card`, `v2_card`; orch-relay (p4) `card`, `op_*`, `relay_request`, `relay_route`; orch-mobile and the browser home (p4)
`prepared`, `channel_register` (the object they sign); B stream: orch-mobile also `pair_link`, `api_origin`, `card`, `channel_cert`,
`channel_request` (it builds them) and the `push_seal` / `render_*` vectors, the hub `channel_auth` and `channel_push`, the
relay `channel_request` and `channel_route`. C3b addendum: orch-core runs `custodial` (now with
`custodial_event_*`, the event-shaped vector per custodial type, for its step-1 exact-payload check), `user_hub`,
`deliver_next` (the host's reading of its own replies) and `share_item` incl. `f1` (it builds them); the hub runs all,
plus `selective_build`. D15b: orch-core builds the `log_sync_proj_*` pages (its seal of `phase4.log_sync.proj_ticket`
must equal `page_ticket_with_proj` byte for byte) and the hub runs `proj_build`; both run `workspace_roster_v*` (hub: `project_workspace`, core: its replay's
`roster_v`). The hub also runs `hub_join`. #1198 C8 (§5, §9.6): orch-core runs `card_reader`, `hub_reader`, `owner_proof`,
`owner_proof_recorded`, `joined_payload` and `left_payload`; the hub runs all of them and `owner_proof_join`. Each case carries its `phase`; the L2 cases name their finding in `note`. The cross-repo contract job (V3-D8) runs from phase 2.

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
| 14 | Link replay | `seq` strictly consecutive per link (D12b); not bound across links | SHORTCUT (L7, residual) |
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
| 36 | `u_` ids and hubs (C3b addendum) | a `u_` id belongs to the hub it first appeared under; `foreign_user` (§9.1, §14) | core #1031 §15.6, L2-C3-2 |
| 37 | `event_deliver` acks (C3b addendum) | `{}` done; `unavailable`, `unknown_snapshot`, no reply → resend the same bytes, 1 s doubling to 5 min, give up after 24 h; every other code final (§8.7) | core #1031 §15.5, L2-C3-4 |
| 38 | Selective custodial events (C3b addendum) | additive `f1` block in `share.item`; four decision types only; action names `item`; text-only answers in V3; `item_hash` in the event (§8.6, §10.2b) | core #1031 §15.7 |
| 39 | Event side of §8.5 (C3b addendum) | exact payload per custodial type incl. derived fields; `base_rev` is a `{path: hash}` object keyed exactly by `set`; host checks it in step 1; one event-shaped vector per type | L2-C3-1, C3 review |
| 40 | `log_sync` sender rules (C3b addendum, C14) | old pages kept across rotation; 1 MiB raw; no oversize line; resume probe instead of a head-read op; `reset` not tied to `restore`; one live link per workspace (§10.7) | orch-core #1027 |
| 42 | Addendum review (l2-add) | L2-ADD-1: selective admission checks the target in the item's `f1` and the chain head (§8.3 step 7); L2-ADD-2: §10.8.3 carries the E3 fixes (fence, one gate with an allow-list, purge of hooks and Drop, `pending` TTL 24 h, registered `hub_id`, no state to anonymous callers); L2-ADD-3: a superseded item version is `event_changed`; L2-ADD-7: vector renamed | state/l2-review-p4.md "Addendum hub#34" |
| 41 | Relay cut-over (E3) | workspace names the hub, the hub only confirms; `other_hub` for every workspace that did not name it; hub `PUT` body `{v: 2, state: "cut_over"}`; `cut_over` (403) (§10.8.3) | orch-relay #88 |
| 43 | The phone side (B stream) | §6.4: operator pairing link pins `hub_id`, and the `HK_op`-signed `hub.api_origin` must equal the link's host before any password (L2B-1); pairing session → channel session by a channel-key-signed, session-bound `hub.channel_auth` (12 h from the password sign-in, scope without registration or admin); a separate software push key named by `hub.channel_push` (the NSE runs locked; one key, one use), §10.8.2 seals to it; §10.8.4 channel-signed mailbox read under `/v2/hub-channels/`, certificate in a header; the hub's signed mailbox purge marks the channel revoked at the relay (`channel_revoked`); a real build refuses loopback/http pairing links; hints also left in the mailbox | state/design-b-phone.md, orch-core #861–#866 |
| 44 | Full-mode gate decisions and status moves (D15b, orch-core #1043) | the host's own projection `P` (F1 gate values, status, roster version at one ticket head and workspace position) inside the sealed `log_sync` page; the hub copies, never computes; stale → `unavailable`; no new op, label or event field (§10.7) | state/design-d15b.md; one engine decides what is shown and what is checked |
| 45 | Phase 5: grants, devices, policy, settings through a channel; break-glass leave (C8/C10) | the five custodial types of §8.8 with exact schemas; the host makes the grant secret and gives it to its runner only for a Mission Control start on that host (a phone alone cannot start an agent); `repos`, `models`, D66 `factor`, terminal grants stay off the channel; every leave voids open hub-signed decisions and ends hub-requested grants; pre-C10 full joins cut over automatically only for a dev workspace with a dev hub card | owner V3-D29 (orch-core #838), design-c8.md, design-c10.md |
| 34 | New audit types | `channel` and `share` are additive `type` values of `hub.audit` in `hv: 0`: **orch-core's audit verifier must accept them** (its type enum and event field table), as the hub does | orchestrator lean, adopted |

**SHORTCUTS added by the phase-4 extension** (for the #838 table): `Phone key attestation by operator pairing code (no
App Attest)` (§6.3, D7 → hardening: App Attest); `log_sync reset on the host's word` (§10.7; **decided, C3b addendum: stays,
not tied to `restore`**); `Custodial ticket.updated without body sections; no artifact custody` (§8.5 → a text/blob
transport in the action).

**Hardening from the phase-4 Opus pass (P2, not built; each a `hardening` row):**

| Item | Where | Hardening |
|---|---|---|
| L2P4-4 core titles with refused characters make a share fail | C6 host | the host escapes or skips, never sends; test `share_item_from_bidi_title_is_escaped_by_host` |
| L2P4-10 value grammars differ from core (`_TOKEN`, `_KEY`, `_PERSON`) | §8.5 | align with core's `token`, `ticketKey`, `personId` (liveness: the hub signs what the host refuses) |
| L2P4-11 relay origin not in the §10.8 bytes | §10.8 | add `lp16(relay_origin)` (a request to relay A replayable to relay B within 120 s) |
| L2P4-12 duplicate `Orch-Hub*` headers | §10.8, §10.6 | exactly one of each, else `malformed` |
| L2P4-13 proposal vs held space | §10.4 | at `epoch == held + 1`, `prev_members == held.members` |
| L2-ADD-4 re-put fan-out | §10.2b, C6 host | the host coalesces `f1` re-puts (one per item per pass); the hub's `stale_version` path stays cheap |
| L2-ADD-5 `unknown_snapshot` amplification | §8.7, host | rate-limit snapshot fetches per workspace; once a fresh fetch still lacks the version, answer without fetching again |
| L2-ADD-6 alias after an unaliased first record | §9.1, core `hub.joined` | refuse at join an alias of a `u_` id first recorded under another hub, so the owner sees it (today it fails closed later as `foreign_user`) |
| L2-E3-3 cut-over is human-only by nature | §10.8.3, core C10 | cut-over and reversal as human-only signed host ops; optional WSK/PK-signed statement stored by the relay |
| L2P4-14 `log_sync reset` on the host's word | §10.7 | decided (C3b addendum): not needed, the view authorizes nothing (§10.7) |
| L2P4-C2 core actor lookups not through `canonical` | C3 | route every actor lookup through `identity.canonical` plus the role mapping |
| L2P4-C1 aliases not scoped to a hub (P1 for core) | §9.1, C4 | key aliases by `(hub_id, user_id)`; "never re-pointed **for the same hub**"; must land before C3 accepts `auth: "hub"` |
| D9 tombstones unbounded | §10.2b | per-workspace monotonic `item_version` counter + prunable tombstones with a `version_floor`; until then a hard cap (`too_many_items`) |
| D9 `hxk_exchange` raw ECDH oracle | hub keys | purpose-bound `open_seal(purpose ∈ {wk, hub-share}, …)`; the share service gets a `hub-share`-only handle |

**Open points of the phase-4 extension (not decided here):**

- **Agent grants in a full-mode hub workspace.** `grant.issued` carries a `secret_hash` of a secret the host makes and
  hands to the agent; the hub cannot build it from its view, so it is not custodial (§8.5). With D78/D81 (no local
  human signing in full mode after H5) nobody could then start an agent there. Options: (a) the action carries the
  `secret_hash` from the host via the channel (the host shows the person a one-time handle), (b) the host makes the
  secret and the channel only signs; (c) keep owner-local signing for `grant.*` as a fourth break-glass power.
  **Decided (V3-D29): (b)**, as `grant.requested` plus a host-actor `grant.issued`, usable only for an agent a person
  started from Mission Control on that host (§8.8).
- **Policy and settings changes in a full-mode hub workspace.** **Decided (V3-D29):** admin-only custodial types with
  exact schemas, three settings keys; `repos`, `models` and the D66 `factor` stay off the channel (§8.8).
- (Decided, C3b addendum.) **Selective custodial signing** is §8.6 over the `f1` block of §10.2b.

**Open / decided after L2:**

- (Decided, L2 recommendation adopted by the orchestrator.) **The one source of a workspace's `WSK`, for orch-publish,
  the relay and the hub, is the v2 directory card**, with the v2 update rules (`workspace_id`, owner and `WSK` never
  change; `card_seq` strictly increases). After H5 the host no longer talks to the relay directly, so it sends its
  card through the hub as an opaque object (`card_put`, §10) and the hub forwards it to the relay and orch-publish. The
  hub cannot forge or replace it (`PK`-signed delegation, `WSK` signature, never-change rules); it can only withhold
  it, which only stops publishing. **orch-publish never pins a binding first-come:** that would make the hub's choice
  of `WSK` authoritative. A binding counts only under the `WSK` of the card publish already holds (§9.2, §11.2 step 5).
- **Relay accounts by hub card** (E1) and hub↔hub objects (H7) are not in H0.
