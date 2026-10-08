# orch v2: multi-user, multi-workspace, end-to-end encrypted

Status: **draft for review** (8 Oct 2026). Spans three repositories: `orch-core` (this one), `orch-relay` (new,
replaces orch-tix) and `orch-publish` (renamed from orch-apps). Visual overview:
[Orch Hub Blueprint](https://claude.ai/artifact/5xUpP1hoDAt8saWJCbJfbh) (written before the naming decision below;
"orch-hub" there is "orch-relay" here).

Part A is the design. Part B is how we build it: roles, test loop, dev machine, VPS, domain and the checklist that
must be done before the first implementation task starts.

---

# Part A: Design

## 1. Decisions

Settled with the owner on 8 Oct 2026. They are not reopened during implementation; a change goes through a spec
amendment.

| # | Topic | Decision |
|---|---|---|
| D1 | Multi-user scope | **A**: one person, isolated workspaces and devices. **B**: colleagues, each with their own workspaces, handing tickets and files to each other. **C** (several humans in one workspace on a shared server) is out of scope for v2. |
| D2 | Workspace keys | A random workspace key `WK` per epoch, sealed to each member device. Revoking a device starts a new epoch. The account master key no longer exists as a derivation root. |
| D3 | Key custody | Keys live in the OS keychain and are loaded only by the workspace host process (`orch serve`). Agents reach it through a narrow local socket API. |
| D4 | Identity | Each person has an identity key that signs their device keys. Peers pin the person key. |
| D5 | Relay tenancy | One orch-relay instance, many accounts. "Internal" is the same software self-hosted. |
| D6 | Phone onboarding | Scanning a workspace's QR code does both: the first time, it makes the phone one of your devices; every time, it seals that workspace's `WK` to the phone. No passphrase on the phone. |
| D7 | Logout | Logging out on the phone wipes that workspace locally and deregisters the phone. Revoking from another device also rotates the epoch. |
| D8 | Migration | Start fresh. Nothing is imported from TIX. TIX keeps running untouched until it is switched off. |
| D9 | Names | Server: **orch-relay** (new repo) with modules *directory*, *relay*, *drop*, *push*. Phone: **orch mobile** (a PWA served by orch-relay). Publishing: **orch-publish**. User-facing nouns: Workspace, Device, Drop. |
| D10 | Publishing | orch-publish publishes over HTTPS, authenticated by workspace identity. Every share and app has an owner and a `/<workspace>/` namespace. SSH is for admin only. |
| D11 | Networks | Carrier by URL: `spool:` (same machine), an internal relay URL, or the public relay URL. No LAN-direct path. `--lan` is retired. |
| D12 | ws→ws routing | The agent suggests a peer from the local address book. The first send to a new peer needs a human to confirm and pin its key. |
| D13 | ws→ws receiving | An incoming ticket lands in the inbox as untrusted data. The receiver's signed charter can allow auto-start per peer and ticket type, with restricted permissions. Depth is 1. |
| D14 | Shared documents | A Drop *document* is a series of encrypted versions. A write needs the current version (`If-Match`), and an edit lease is optional. |
| D15 | Web agents | A web agent enrolls as a *scoped agent device*: one Drop space, with an expiry, and no workspaces or tickets. |
| D16 | Mobile tech | A PWA now, reusing the TIX vanilla-JS and WebCrypto code. A native shell later, if needed. |
| D17 | In-flight work | Land the core work (Dark Factory stack #79→#143, #237, the Remote fixes). Freeze TIX #96/#97 and port their good parts into orch-relay. |
| D18 | Stack | Python, FastAPI and SQLite (WAL), under systemd behind Caddy. The same as orch-core and TIX. |
| D19 | Process | Spec, then review, then phased issues, then one PR per task group. **The owner merges every PR.** |
| D20 | Repos | orch-relay and orch-publish are public. |

## 2. Goals and non-goals

Goals:

- Each workspace's data is readable only by the devices that are members of it, on every path: phone, relay, drop,
  push and other workspaces.
- A phone joins a workspace with one QR scan and leaves it with one tap, and leaving removes that workspace's
  data from the phone.
- A question answered anywhere disappears everywhere.
- A workspace can hand a ticket to another workspace (yours or a colleague's) and continue with the result.
- Files move between phone, workspaces and scoped web agents through Drop: addressed, claim-once, or as
  versioned shared documents.
- Workspaces publish shares and apps under their own namespace.
- The same software runs on one machine, inside a company network, or on the public internet.

Non-goals for v2:

- Several humans in one workspace (D1 C).
- Forward secrecy for stored content. Revocation protects new content only.
- Protection against a compromised endpoint, or against traffic analysis by the relay.
- A native mobile app.
- LAN-direct transport.

## 3. Components

```
 orch mobile (PWA) ─┐                                 ┌─ orch-publish  (pub API + apps origin)
 web agent ─────────┤   HTTPS, sealed envelopes       │        ▲ HTTPS, workspace-signed
                    ├──────────► orch-relay ◄─────────┤        │
                    │   directory · relay · drop · push│   workspace host (orch serve)
                    │                                 └─ keys in keychain, agents via socket
                    └ every byte the relay stores or forwards is ciphertext, except the routing ids
```

| Component | Repo | Runs on | Holds keys? |
|---|---|---|---|
| `orch` CLI + workspace + Mission Control | orch-core | each workspace machine | Workspace signing key and `WK` epochs, through the host process |
| Host process (`orch serve`, also headless as `orch host`) | orch-core | each workspace machine | Yes: the only process that loads keys |
| orch-relay: directory, relay, drop, push | orch-relay | VPS (public), or a company server (internal) | No. Wrapped blobs and public keys only |
| orch mobile | orch-relay (`/app`) | phone browser, installed to the home screen | Device key (non-extractable) and the `WK`s sealed to it |
| orch-publish: publish API + apps origin | orch-publish | VPS (a separate origin from the relay) | Only the sealed-share loader key, which is in the URL fragment and never on the server |

## 4. Threat model

- **The relay** is honest-but-curious for confidentiality and untrusted for integrity: it may drop, delay, replay or
  reorder. Every envelope is authenticated, sequence-numbered and idempotent.
- **Web-delivered JS** is trusted at load time. This is accepted for v2 and narrowed by: installed PWA only, pairing
  confirmed on the host's screen, and a signed asset manifest as a later option.
- **The workspace host process and the human's machine** are trusted.
- **Agents** are semi-trusted. They use the host socket API and run under a separate UID wherever the platform
  allows: on the VPS always, on a personal Mac from phase P8. **Until P8, agents on macOS run as the host's UID and
  can reach the keychain and the socket. "Agents never read keys" holds only on the VPS until then.**
- **Peer workspaces** are pinned, and everything they send is untrusted data. A valid signature proves origin, not
  safety.
- **Accepted leaks to the relay:** sizes, timing, routing ids (random workspace and device ids), push timing, and
  that a question was resolved.
- **Sealed, not leaked:** device and machine names, project and workspace names and descriptions (orch-tix #75's
  lesson).
- **The one deliberate exception: voice-note transcription.** It is opt-in per person and off by default. When it is
  on, audio uploaded from the phone or browser is sent over HTTPS to Deepgram (as in TIX today). The transcript comes
  back into the object's sealed metadata. The setting and the Drop upload screen both say so.

## 5. Identity and keys

### 5.1 Key types

| Key | Kind | Where it lives | Purpose |
|---|---|---|---|
| Person key `PK` | signing | your primary device's keychain, plus a recovery kit | Signs device certificates. Peers pin it. |
| Device key `DK` | signing + key agreement | each device (keychain, or non-extractable WebCrypto) | Signs requests. `WK`s are sealed to it. |
| Workspace key `WSK` | signing | the workspace host's keychain | Signs envelopes, member lists, directory cards and publish requests |
| Workspace exchange key `WXK` | key agreement | the workspace host's keychain; public half in the card | Receives ws→ws bodies, Drop wraps addressed to the workspace, and `SK` wraps. Rotates with the 90-day epoch; its version is in the card. |
| Workspace content key `WK_e` | symmetric, 256-bit, random per epoch | the host's keychain; sealed copies on the relay, one per member device | Seals everything belonging to the workspace |
| Data key `DEK` | symmetric, per object | wrapped under `WK_e`, or under a Drop space key | File and document content |
| Drop space key `SK_e` | symmetric, per epoch | sealed to the space's member devices | Shared Drop spaces |
| Personal vault key | symmetric | your devices | Personal settings only. Never derives anything else. |

**Algorithms (decided, confirmed by spike S1).** The suite:

- Ed25519 for signatures and X25519 for key agreement, through WebCrypto.
- HKDF-SHA-256, and AES-256-GCM in authenticated chunks (as in TIX today).

If S1 shows Ed25519 or X25519 in WebCrypto is not reliable on the oldest iOS we support, everything uses
ECDSA/ECDH P-256 (proven in the TIX bridge) with the same structure. A single suite is used across all components,
never a mix.

**Sealing to a device** is HPKE-shaped:

- an ephemeral key-agreement key;
- HKDF with context `"orch/v2/seal|" + purpose + "|" + recipient_device_id`;
- AES-256-GCM, with AAD binding the object id, epoch and purpose.

### 5.2 Device certificates and recovery

- A device certificate is `{device_id, person_id, dk_pub, label_sealed, created, expires?, scopes_max}`, signed by `PK`.
- **Revoking a device** (lost phone) is a revocation record signed by `PK`, so only from the primary device. It is
  published to the directory.
  - The relay enforces it at once: a revoked `device_id` gets no mailbox, membership or Drop access.
  - Each host rotates its epoch (§5.4) before it seals anything new after learning of a revocation, including at
    startup.
- **Removing a device from one workspace** is a request signed by any member device with Operate scope, applied by
  that host. It also rotates.
- **Recovery kit:** a 24-word code shown once at person creation. It wraps a copy of `PK`, and the relay stores the
  wrapped blob. The code is stretched with a memory-hard KDF before it unwraps anything. If both the code and every
  device are lost, the person key is gone and peers must re-pin a new one. (Decided: recovery code only, no backup
  device.)

### 5.3 Workspace identity

- `workspace_id` is a random 128-bit id, created once and stored in `orchestrator/config.json`. It replaces the three
  id schemes in use today: `sha256(customer|prefix)`, the resolved path, and the TIX space id. Records keyed by the
  old ids are migrated at first run of the new version.
- The `WSK` key pair is created at the same time, in the host keychain.
- The **directory card** has two parts, both signed by `WSK` and by the owner's `PK`:
  - a cleartext part `{workspace_id, wsk_pub, wxk_pub, wxk_version, owner_person_id, relay_url}`, which the relay
    and orch-publish verify;
  - a sealed part `{name, description, capabilities}`, sealed to the people the owner shares the card with.

  The relay never sees names or descriptions.

### 5.4 Members and epochs

- The host keeps a member list `{epoch, members:[device_id, scopes]}` signed by `WSK`.
- **A new epoch starts** on:
  - revoking a device from another device (D7);
  - removing a person;
  - an explicit "rotate";
  - every 90 days.
- **On a new epoch**, the host:
  1. generates `WK_{e+1}`;
  2. seals it to each remaining device and uploads the sealed blobs;
  3. publishes the signed member list;
  4. starts sealing new content under `WK_{e+1}`.
- Old content keeps its epoch tag. Devices that still hold old epochs can read old content. This is the honest limit,
  and the UI states it.
- A phone logging itself out (D7) deregisters and wipes. That does not rotate.
- **Rotation is automatic.** The host checks the epoch's age at startup and once a day, and rotates when it is due
  (D24) or when it learns of a revocation or removal. The only human steps are deciding to revoke or remove a device,
  and the optional `orch keys rotate`.
- **Devices offline during a rotation** pick up their sealed `WK_{e+1}` from the relay at their next connect. Nothing
  manual is needed.
- **New members get the current epoch only (D26).** A device that joins at epoch `e` is sealed `WK_e` and later epochs,
  never earlier ones. Live views through the bridge (tickets, questions, Factory) are served by the host and show
  the full state anyway. For an older Drop object, the device asks the host, which re-wraps that one object's `DEK`
  under the current epoch if the device's scope allows it.
- **Exchange key overlap (D27).** When `WXK` rotates, the host keeps the previous private key for 14 days and still
  accepts envelopes and Drop wraps sealed to it, then deletes it. Senders pick up the new `wxk_version` from the
  card and, when the receiver answers `stale_wxk`, refetch the card and reseal.

### 5.5 Key custody and the host socket API

The host loads keys through a custody backend:

| Platform | Backend |
|---|---|
| macOS | Keychain. In development: a dedicated keychain file, never the login keychain. |
| Linux desktop | libsecret |
| Linux server / VPS | Files mode 0600, owned by a separate `orch-host-<ws>` user. Agents run as a different UID. |

**The socket.**

- Path: `<state>/hosts/<workspace_id>.sock`, one per workspace. Keep the path under macOS's 104-byte limit.
- It sits in a directory with mode 0750 and group `orch-agents-<ws>`; the socket itself is mode 0660.
- The host checks the peer's UID (`SO_PEERCRED` on Linux, `getpeereid` on macOS) and refuses anything other than
  itself and the configured agent UID.

**Operations offered to agents.** The list is closed. **There is no raw `sign` and no generic `seal`.** The host builds
and signs every envelope itself, with domain-separated signatures (a fixed kind prefix per envelope type), so an
agent can never get a member list, card or revocation signed.

| Operation | Notes |
|---|---|
| `send(peer, ticket)`, `reply_result(envelope_id, result)` | Subject to the address book, D12 and the charter. Rate-limited and logged. |
| `drop_put`, `drop_get`, `drop_claim`, `doc_get`, `doc_put` | Purposes from a closed allowlist. Objects the agent seals are tagged `origin=agent`. |
| `publish(...)` | §10. Public access from an agent needs a charter rule or a human confirmation. |
| `question(...)` | Asks the human; the answer comes back through §8. |

- No operation returns key material, and agents never handle relay tokens.
- The first-send confirmation (D12) is a human action, made in the dashboard or as a device-signed request. It is
  never a CLI flag.
- The guard keeps its deny rules for key paths as defence in depth.

## 6. orch-relay

### 6.1 Directory

Tables: `accounts` (one per person), `devices` (certificates), `workspaces` (cards), `memberships` (sealed `WK`
blobs per device and epoch), `drop_spaces`, `revocations`.

- **Accounts** are created by invite code (owner-issued) or by the first-run owner.
- **Authentication** is a device-key signature over a server challenge, giving a short session.

### 6.2 Relay (bridge v2)

- **Base:** the TIX bridge protocol v1 (`orch-tix/docs/bridge-protocol.md`). Its framing, sequence numbers, idempotency,
  replay store, scopes (Look/Decide/Operate/Type), WebAuthn assertions and typing lease carry over.
- **What changes:**
  - the key source: `K_bridge = HKDF(WK_e, info = "orch/v2/bridge|" + ws + "|" + epoch)` replaces `HKDF(MK, ws)`,
    and `K_msg` is derived from `K_bridge` as in v1. `WK_e` is never used directly for any purpose; every use goes
    through HKDF with its own info label;
  - the header version becomes v2 with an `epoch` field: a new layout and new vectors;
  - the host key is `WSK`;
  - the device key comes from a device certificate;
  - a new mailbox kind `ws→ws`.
- **Mailboxes** are keyed by `(workspace_id, device_id)` or `(workspace_id, peer_workspace_id)`. Hosts and devices dial
  out with long polling. Sealed bodies live 60 s in the bridge (as in v1); ws→ws envelopes live until acknowledged,
  with a deadline (§9).
- **Contract:** `orch-relay/docs/protocol-v2.md` plus `tests/vectors_v2.json`. As in v1, both sides test against
  the vectors, not against each other.

### 6.3 Push

- Web Push (VAPID). A **subscription is per (device, workspace)**, stored with the membership and deleted with it.
- **Payload.** The host seals `{kind, id, label}` under the push subkey `HKDF(WK_e, "orch/v2/push|"+ws+"|"+epoch)`.
  The relay sees only `{v:2, ws, epoch}` plus opaque bytes, and forwards them.
  - The service worker opens the payload and sets `tag = id`.
  - It drops any payload for a `ws` it holds no key for.
- **Removing a workspace** deletes only that workspace's relay record. The browser unsubscribes entirely only when
  no workspace is left.
- Kinds: `question`, `question.closed`, `ticket.update`, `drop.new`, `peer.ticket`, `join`.
- `question.closed` replaces the notification with "Answered on <device label>", rendered from the sealed fetch.
  iOS has no silent push, so a visible replacement is the design.

### 6.4 Drop

- A **space** is personal, workspace (key `WK`) or shared (key `SK`).
- **Objects:**

  | Kind | Behaviour |
  |---|---|
  | `file` | Immutable, with expiry |
  | `document` | A series of versions |

- **Addressing.** An object is put into a space with `recipients`, either a list of workspace or device ids, or `inbox`.
- **Wrapping.** A wrap addressed to a workspace uses its `WXK`, so a sender that is not a member can address it.
  Wraps under `WK_e` are used only when the writer is a member of that workspace.
- **Claim-once.** For inbox objects, the `DEK` is wrapped once for each eligible workspace's `WXK`. The relay holds no
  keys, so the claimer's host does the re-wrap:
  1. It opens its own `WXK` wrap and re-wraps the `DEK` under its current `WK_e`.
  2. It sends that wrap in `POST /drop/{id}/claim`, signed by `WSK`.
  3. In one transaction, the relay checks eligibility and `state=inbox`, stores the new wrap, sets `claimed_by`
     and deletes every other wrap.

  A claim decides who handles the object. It does not take it back from anyone who already downloaded it.
- **Documents.** Each version is a sealed blob plus `{version, parent, author_device}`.
  - `PUT` needs `If-Match: <version>`. A stale writer gets 409 "fetch first".
  - An optional lease (`POST /lease`, default 10 min) shows "editing" to others.
  - There is no merge.
- **Scoped agent devices (D15).** An owner issues a one-time enrollment code, scoped to one shared space with an
  expiry. The agent's certificate carries `scope=drop:<space_id>`, and the relay enforces it on every route.
- **Public links and upload links** carry over from TIX (the key stays in the URL fragment).
- **Voice notes:** opt-in transcription carries over from TIX (§4, the one exception). The Deepgram key is personal
  vault data; the relay never stores it in the clear.

## 7. orch mobile

- A PWA served at `https://<relay>/app`, built from the TIX static code (`fileshare/static/`). The TIX ports are:

  | Port from TIX | Source |
  |---|---|
  | Bridge client, sandboxed dashboard frame, streams viewer | TIX #96 |
  | Unlock sheet and WebAuthn | TIX #97 |
  | Crypto helpers | TIX |
- **Home** is a list of **Workspace cards** plus **Drop**. Each card has the tabs Questions, Tickets/Factory, Files and
  Terminal (scope permitting).
- **Storage** is partitioned per workspace: one IndexedDB database `ws-<id>` and one Cache Storage bucket per
  workspace. Removing a card deletes both, the sealed `WK`s and the push subscription, then deregisters.
- **Pairing (D6):**
  1. The host's Remote tab shows a QR code for `https://<relay>/app/pair#v2.<ws>.<offer>.<secret>.<wsk_pin>`
     (10 minutes, single use).
  2. The phone generates `DK` and sends a join request through the relay.
  3. Both screens show a 6-character fingerprint. The human confirms on the host.
  4. **First time only:** the host asks the owner's primary device to sign a device certificate. When the host *is*
     the primary device, it signs locally. The QR carries a pin of the owner's `PK`, which the phone uses to check
     the certificate.
     - If the primary device is unreachable, pairing waits in `cert_pending` for up to 10 minutes, and both screens
       say "Open orch on <primary>". The primary shows the same fingerprint and needs its own confirmation.
     - **No `WK` is sealed before a valid certificate exists.**
     - A phone that already has a certificate presents it. The host checks
       `cert.person_id == card.owner_person_id` and otherwise refuses with `other_person` (D1 C is out of scope).
  5. The host adds the phone to the member list and seals `WK_e` to it. A platform passkey is registered for
     Type and Factory actions.
- **Starting work:** a "New ticket" action (a sealed request applied by the host), start/move through the Operate
  scope, and the AI Factory through the bridge (as merged in core R13).

## 8. Questions answered elsewhere

- **Source of truth:** the workspace that owns the question.
- Every question has a `question_id` and a `content_hash`. Every decision carries `decision_id`, `question_id` and
  `content_hash`.
- The host applies the first valid decision (compare-and-set in the ticket store) and pushes `question.closed`. It
  emits the `question.closed` envelope to every member device, and to a linked peer only if the question came from
  that peer.
- A late decision gets `already_answered {by_device_label, at}`.
- Clients reconcile on open by fetching the state of every question they show.

## 9. Linked workspaces (ws→ws), building on #175

- **Address book.** Each workspace has a local list of peers `{workspace_id, wsk_pub, owner_person_pub, card,
  carrier, pinned_at, allowed_kinds}`. Peers are added from a card shared through the directory, or by QR between
  two hosts.
- **Sending (D12):**
  - The agent calls `orch peer suggest` and gets ranked candidates from the descriptions.
  - `orch send <peer> <ticket>` needs a human confirmation the first time a peer is used. After that it is
    allowed for pinned peers.
  - The sender's ticket moves to `waiting_on_peer` with a deadline.
- **Envelope.**
  - Cleartext header: `{v, id, from_ws, to_ws, wxk_version}`.
  - Everything else is in the sealed body, sealed to the receiver's `WXK` with an ephemeral key agreement:
    `{kind, in_reply_to, depth, deadline, ticket, attachments}`.
  - The signature (`WSK`, domain `orch/v2/ws-envelope`) covers the header and the ciphertext.
  - **Human co-signature.** When a human authorized the envelope, it is co-signed over the envelope hash with a
    WebAuthn assertion or a phone `DK` signature. Receivers verify the chain `DK → certificate → pinned PK`.
- **Receiving (D13):**
  - The receiver verifies the pin, the signature, `depth ≤ 1` and the deadline, and deduplicates on `id`.
  - The ticket lands in the inbox, marked `from-peer`.
  - The charter rule `auto_start: {peer, kinds, profile}` can start it with the restricted profile: no secrets, no
    push, network only within scope, and no `orch send`.
- **Result.** A signed `result` envelope carries a summary, Drop references and a status. The sender treats it as
  data and its agent continues. When the deadline passes, the ticket goes back to the human; there is no silent
  retry.
- **Carriers:**

  | Carrier | Path |
  |---|---|
  | `spool:<path>` | Same machine, through the filesystem; tests and two local workspaces |
  | `relay:<url>` | Internal or public relay |

## 10. orch-publish

- **API:** `https://pub.<domain>/v1/...`.
  - Every request is signed by the workspace's `WSK` (domain `orch/v2/publish`). The signature covers the method,
    host, path, a body hash, a `ts` within ±120 s, and a nonce. Nonces are stored to refuse replays.
  - The card is verified against the directory (orch-relay) and cached. The cache entry is evicted when the card
    changes or a revocation arrives.
- **Namespaces:** `https://apps.<domain>/<ws-slug>/<app>/` and `/s/<id>/` shares, each with `owner_workspace_id`.
  A slug (random or chosen) is bound to one `workspace_id` on first use.
- **Access levels:**
  - shares: public, secret or sealed (the loader is kept as is);
  - apps: public or secret, with tokens per recipient that can be revoked individually.
- **Runtime:** systemd units with `DynamicUser` (kept from orch-apps). The apps origin is separate from the relay
  origin.
- **Admin:** SSH to an admin user for maintenance only. `install.sh` appends keys and never overwrites
  `authorized_keys`.

## 11. Networks (D11)

- *Local* is two workspaces on one machine using `spool:`, and the phone through any relay.
- *Internal* is an orch-relay installed in the company network (the same package, `orch-relay install --internal`).
- *External* is the public orch-relay.
- A workspace can be registered with more than one relay. Each peer entry names its carrier.
- `orch serve --lan` is removed. Loopback stays the only direct binding.

## 12. Changes in orch-core

| Area | Change |
|---|---|
| Identity | `Actor("human", <person_id>, via=device_id)` replaces `Actor("human","you")`. The workspace UUID replaces the three id schemes. |
| Keys | A custody backend and the host socket API (§5.5). The `orch keys` commands: `init`, `show`, `rotate`, `recovery`. |
| Ledger | Stays HMAC for local entries. Entries that leave the machine (ws→ws, publish, decisions from devices) carry device-key signatures. |
| Remote | The bridge host switches to v2 keys, and pairing v2 replaces both `/pair#` and `/remote/pair#`. |
| Linked workspaces | `orch peer`, `orch send`, `orch inbox`, the `waiting_on_peer` status, and charter `auto_start` rules. |
| Dev mode (#251) | `orch serve --dev` / `--sandbox`. Prerequisite for agentic development (Part B). |
| Fixes carried in | #237 (the permit_grant lock) and #239 (the typing lease bound to a terminal). |

## 13. Phases

Each phase is a set of **task groups**. A group ends with the full test tier T2 (Part B §16), and a phase ends with
T3. Every group is one PR, or a short stack of PRs, which the owner merges.

| Phase | Content | Exit criteria |
|---|---|---|
| **P0 Dev foundations** | The dev environment (Part B), VPS provisioning scripts, orch-core dev mode (#251) with the state-dir dev marker, the guard extended to all `ORCH_*` variables and the sandbox control files, an orch-relay skeleton (health, deploy), the e2e harness, landing the in-flight core work (D17), and spikes S1 (WebCrypto suite on iOS) and S2 (PWA push and passkeys in the iOS Simulator). | An agent can start both demo workspaces, drive Mission Control in dev mode, deploy the relay to the VPS and run an empty e2e scenario from one command. |
| **P1 Identity** | Workspace UUID, person id in Actor, the custody backends, the host socket API, `orch keys`, and device certificates. | Unit and contract tests pass. No key material is readable by an agent-UID process on the VPS. |
| **P2 Relay core** | Directory, bridge v2 with protocol and vectors, members and epochs, and the core bridge host on v2. | Desktop browser ↔ workspace A over the dev relay, with epoch rotation tested. |
| **P3 Mobile + pairing** | The PWA port, pairing v2, workspace cards, partitioned storage, logout and revoke, and WebAuthn. | Simulator e2e passes. **iPhone session 1:** camera QR, Face ID passkey, logout wipes. |
| **P4 Questions + push** | Push per workspace, `question.closed`, reconcile on open. | Answering on the laptop clears the phone. **iPhone session 2:** push arrives, gets replaced, and a late answer is refused. |
| **P5 Drop** | Spaces, recipients, claim, documents, scoped agent devices, links. | Two workspaces race to claim and one wins. A document conflict gives 409. A scoped agent cannot see workspaces. |
| **P6 Linked workspaces** | Address book, `spool:` then relay carrier, envelopes, charter auto-start, `orch wait`, deadlines. | WS1 sends a ticket to WS2 (on the VPS), WS2's stub agent works it, and WS1 resumes with the result. Depth 2 is refused. |
| **P7 orch-publish** | Rename, the signed HTTPS API, namespaces, per-recipient tokens, and the migration from orch-apps branches. | Each demo workspace publishes in its own namespace, and one cannot touch the other's apps. |
| **P8 Colleagues + internal** | Second account (D1 B), cross-person peers and Drop, the internal relay install, `--lan` removal, agent UID on macOS, TIX switch-off plan. | Persona "colleague" on WS2 exchanges a ticket and a document with WS1. **iPhone session 3:** full regression. |

## 14. Former open items (decided 8 Oct 2026)

| # | Item | Decision |
|---|---|---|
| D21 | Recovery | A 24-word recovery code only (§5.2). No backup device. |
| D22 | Crypto suite | Ed25519 and X25519 through WebCrypto. If spike S1 shows they are unreliable on the oldest supported iOS, P-256 everywhere. Always a single suite. |
| D23 | Dev domains | `*.dev.severin.io`: `relay.dev`, `relay-internal.dev`, `pub.dev`, `apps.dev`. |
| D24 | Rotation | Every 90 days, plus every revoke or removal. |
| D25 | Transcription | Kept as an opt-in, off by default, and stated in the threat model (§4) as the one exception. |
| D26 | History for new devices | Current epoch only. Phones are for acting on what is happening now. Older Drop objects are re-wrapped by the host on request (§5.4). |
| D27 | Exchange key rotation | The previous `WXK` is accepted for 14 days, then deleted (§5.4). |

---

# Part B: How we build it

## 15. Roles and models

Development is fully agentic. The owner reviews the spec, merges PRs and joins the iPhone sessions. Everything else
is done by agents.

| Role | Model | Does | Never |
|---|---|---|---|
| **Manager** | Opus | Splits a group into tasks, writes each implementer prompt (scope, files, targeted test command, acceptance), integrates, runs T2/T3, opens PRs, keeps the phase checklist | Writes large amounts of code itself, or merges |
| **Implementer** | Sonnet | One task in its own git worktree: write the targeted test, implement, loop on T0, finish with T1, report the diff and evidence | Runs the full suite, touches files outside its task, changes permissions or the guard |
| **Security reviewer** | Opus | Reviews every change touching crypto, keys, custody, auth, protocol, pairing, the guard, permits or the charter, before the PR. Also writes the adversarial tests for these. | Approves its own fixes. A fix goes back to an implementer, then gets a re-review. |
| **Code reviewer** | Sonnet | General review of every PR: correctness, tests, simplicity, project conventions | Blocks on style alone |

The rules for prompts:

- Every implementer prompt names the exact T0 command. Implementers run in parallel only on tasks that touch
  disjoint files.
- Cross-repo contracts are written first, by the manager or the security reviewer: protocol docs plus test vectors.
  Implementers on both sides code against the vectors.

## 16. Test tiers: the fast loop

The rule: **small change → targeted test in seconds → next small change.** The full suite runs only at the end of a
group.

| Tier | When | What | Budget |
|---|---|---|---|
| **T0 targeted** | After every small change | The one test file or `-k` expression for the behaviour being built, e.g. `uv run pytest tests/test_keys_custody.py -k rotate -x -q` | < 30 s |
| **T1 module** | End of an implementer task | The touched package's tests: `uv run pytest -q -m "not slow" tests/<area>*` plus lint, and for JS `node --test <files>` | < 3 min |
| **T2 group** | End of a task group, before the PR | Every repo's full suite (`-n auto`, including slow), the contract vectors on both sides, and the e2e scenarios against the dev VPS plus Playwright plus the iOS Simulator | < 25 min |
| **T3 phase** | End of a phase | T2, the security reviewer's pass, and the iPhone session checklist where the phase has one | — |

How we keep T0 fast:

- Pure units first. Crypto, envelopes and state machines are pure functions with vectors, tested without servers.
- An **in-process fake relay** (`orch.testing.fake_relay`, generalising today's `tests/fake_sharing.py`) for
  orch-core tests, and an in-process host stub in orch-relay tests. Real network only in T2.
- `pytest -x --lf` while iterating. Mark anything over 2 s `@pytest.mark.slow`, as CONTRIBUTING.md already asks.
- E2E scenarios are named and runnable alone: `e2e/run.sh pair-phone`, `e2e/run.sh ws-handoff`. A group's T2 runs
  all of them; a fix reruns only the failing one, then all of them once more.

When T2 fails, the manager files the failures as fix tasks to implementers, each with the failing scenario as its
T0. It then reruns T2 once everything passes.

## 17. Folder layout

Everything lives in one directory on the implementation machine. Nothing in it touches the owner's real orch
config, real workspaces, real keychain or `tix.severin.io`.

```
~/orch-dev/
├── .envrc                    # direnv: exports the sandbox env below (the owner allows it once)
├── .claude/settings.json     # permissions for the dev session (§19)
├── CLAUDE.md                 # dev rules: this Part B in short, the guardrails, the test tiers
├── bin/
│   ├── dev-env               # prints and checks the sandbox env
│   ├── ws1                   # start demo workspace 1's host in dev mode (local)
│   ├── ws2                   # deploy and start demo workspace 2 on the VPS; `ws2 --local` runs a local spool: peer
│   ├── relay-deploy          # deploy orch-relay to the VPS
│   └── reset                 # wipe sandbox state, demo workspaces and VPS data back to seed
├── state/                    # ORCH_STATE_DIR and XDG_CONFIG_HOME for orch in the sandbox
├── keychain/orch-dev.keychain-db   # dedicated macOS keychain for the custody backend
├── orch-core/                # clone, working branches
├── orch-relay/               # new repo
├── orch-publish/             # renamed from orch-apps
├── orch-tix/                 # read-only reference clone (frozen), for porting
├── orch-demo-workspace-1/    # demo repo: "Laptop" workspace (runs locally)
├── orch-demo-workspace-2/    # demo repo: "Client VM" workspace (source; runs on the VPS as user ws2)
└── e2e/                      # scenarios, Playwright specs, simulator scripts, iPhone checklists
```

The sandbox environment, exported by `.envrc`:

```
ORCH_STATE_DIR=$HOME/orch-dev/state/orch
XDG_CONFIG_HOME=$HOME/orch-dev/state/xdg
ORCH_DEV=1                                  # dev mode allowed only when this is set and the workspace is marked dev
ORCH_KEYCHAIN=$HOME/orch-dev/keychain/orch-dev.keychain-db
ORCH_RELAY_URL=https://relay.dev.severin.io
```

The orch guard refuses agent commands that set `ORCH_STATE_DIR` or `XDG_CONFIG_HOME` (`src/orch/core/permits.py`).
P0 extends that rule to every `ORCH_*` variable, which covers `ORCH_DEV`, `ORCH_KEYCHAIN` and `ORCH_RELAY_URL`.
So the environment must already be set when the Claude Code session starts: start it from a shell inside
`~/orch-dev` with direnv active. Agents never change it.

Demo workspaces:

- A workspace counts as dev only with a marker in the **host state dir** (`$ORCH_STATE_DIR/dev-workspaces.json`),
  written by `bin/ws1` / `bin/ws2`. A marker in the repo is not enough, because an agent can write the repo.
  Dev mode (#251) and the dev permissions apply only to marked workspaces.
- Each comes with seed tickets that cover the scenarios: a question to answer, a Factory epic, a ticket for the
  peer, and a file to drop.
- `bin/reset` restores the seed.

## 18. Dev machine checklist

The owner prepares items marked **Owner**. Agents do the rest in P0 and tick them off in `e2e/CHECKLIST.md`.

| # | Item | Who | Check |
|---|---|---|---|
| M1 | macOS with **Xcode** installed and opened once, and an iOS runtime for the Simulator | Owner | `xcode-select -p` points into Xcode.app, and `xcrun simctl list runtimes` lists iOS |
| M2 | Claude Code installed and logged in. The manager runs on Opus; subagents can use Sonnet. | Owner | `claude --version` |
| M3 | `gh` logged in as **severinlindenmann** and active | Owner | `gh auth status` shows it as the active account |
| M4 | SSH key for the VPS, with a host alias `orch-dev` in `~/.ssh/config` | Owner | `ssh orch-dev true` |
| M5 | direnv installed and hooked into the shell, and `~/orch-dev/.envrc` allowed | Owner (allow once) | `direnv status` |
| M6 | `uv`, Python 3.11+, Node 24, git, tmux | Agent | `bin/dev-env --check` |
| M7 | Playwright with Chromium and WebKit | Agent | `npx playwright --version` |
| M8 | The dedicated dev keychain is created; the login keychain is untouched | Agent | `security list-keychains` shows it, and `bin/dev-env --check` |
| M9 | Repos cloned into `~/orch-dev/`, and the demo workspaces created and seeded | Agent | `bin/dev-env --check` |
| M10 | `~/orch-dev/.claude/settings.json` in place (§19) | Owner writes it, or approves the agent's draft | — |
| M11 | Optional: OrbStack for a local internal relay | Owner | `docker version` |

## 19. Permissions and guardrails

`~/orch-dev/.claude/settings.json` allows, without asking:

- `ssh orch-dev …` and `scp`/`rsync` to `orch-dev`;
- `uv run …`, `npx playwright …`, `node --test …` and `xcrun simctl …`;
- `orch …` and `bin/*`;
- `git` and `gh` (except merge), inside `~/orch-dev`.

It asks first for: `gh pr merge`, anything that writes outside `~/orch-dev`, and DNS changes.

It denies:

- `tix.severin.io`, the real `~/.config/orch`, the login keychain and the owner's real workspace paths;
- edits to the sandbox's own control files: `~/orch-dev/{.claude,.envrc,CLAUDE.md,bin,keychain}` and
  `state/orch/dev-workspaces.json`. Changing these is an owner action.

The dev keychain's password is never stored in a file. It is unlocked by the owner at session start, or kept in
the login keychain under a dedicated item that only `bin/dev-env` reads.

Guardrails that hold whatever the permissions say:

- Agents drive Mission Control only through dev mode (#251) on workspaces marked `dev`. Actions are recorded as
  `agent:<session>`, never as the human. The guard and the human-only rules are not weakened for the real
  workspaces.
- Owner-only actions in the demo workspaces (approve, verdict, close) are done by a **test human**. This is a
  scripted persona with its own person key in the sandbox keychain, used only by e2e scenarios. Its certificates
  carry `dev: true`, and real workspaces refuse to pin a person or device with that flag.
- No secrets in repos. VAPID keys and the like are generated on the VPS and kept there.
- Agents open PRs. Only the owner merges.

## 20. VPS setup (test server)

The owner provides a clean VPS. The agent provisions it with an idempotent script, `orch-relay/infra/dev/provision.sh`,
so it can be rebuilt from scratch at any time.

**Owner provides:** Ubuntu 24.04 LTS, at least 2 vCPU, 4 GB RAM and 40 GB disk, a public IPv4 (IPv6 optional),
ports 22/80/443 reachable, root SSH by key, and, if the provider offers it, a snapshot named `clean` to reset to.

**Provisioning creates:**

| Unix user | Purpose |
|---|---|
| `deploy` | Agent login, sudo for provisioning only |
| `orch-relay` | Runs the relay (systemd), with data in `/srv/orch-relay` |
| `orch-relay-int` | A second relay instance as the "internal" relay, with its own DB and port |
| `orch-publish` | Runs the publish API and the app units, with data in `/srv/orch-publish` |
| `orch-host-ws2` | Holds workspace 2's keys and runs its host process |
| `agent-ws2` | Runs workspace 2's agents (a stub agent by default, Claude Code optionally). It cannot read `orch-host-ws2`'s files and talks to it through the host socket. |

**Packages:** Caddy, uv, Python 3.11+, Node 24 (for node apps in publish), sqlite3, git, ufw, fail2ban,
unattended-upgrades and **chrony** (the bridge checks timestamps, so clocks must be synced).

**Network:** outbound 443 must reach the Apple and FCM Web Push endpoints.

**Certificates:** `/var/lib/caddy` survives `bin/reset`. Let's Encrypt allows 5 duplicate certificates per week, so
re-provisioning must not request new ones every time. Alternatively, use a DNS-01 wildcard certificate if a DNS
token is available.

**Workspace 2 on the VPS:** provisioning installs the orch-core wheel and the `orch-demo-workspace-2` repo for
`orch-host-ws2`.

**VAPID:** keys are generated on the VPS, with a subject email (the owner's, or a dev alias).

**Services:**

- `orch-relay.service` and `orch-relay-int.service`;
- `orch-publish-api.service` and `orch-app@.service`;
- `orch-host-ws2.service`;
- Caddy with one site per hostname.

**Deploy:** `bin/relay-deploy` rsyncs a built wheel, migrates and restarts. It takes seconds and is used inside T2.

**Optional:** an Anthropic API key with a spend limit, for a real Claude Code agent in workspace 2. Without it, e2e
uses the deterministic stub agent, which is the default for T2.

## 21. Domain and DNS

The owner adds one wildcard record:

```
*.dev.severin.io.   A      <VPS IPv4>
*.dev.severin.io.   AAAA   <VPS IPv6>      (optional)
```

Instead of the record, a DNS API token limited to that zone would let agents add records themselves.

Caddy gets certificates automatically (HTTP-01) for:

| Host | Serves |
|---|---|
| `relay.dev.severin.io` | orch-relay (public) and orch mobile at `/app` |
| `relay-internal.dev.severin.io` | the second relay instance (stands in for a company relay) |
| `pub.dev.severin.io` | the orch-publish API |
| `apps.dev.severin.io` | published shares and apps (its own origin) |

Real HTTPS is required, not a convenience: the PWA install, service workers, Web Push and passkeys all refuse an
IP address or a self-signed certificate on the phone.

## 22. Mobile testing

| Layer | Tool | Covers | When |
|---|---|---|---|
| Logic | Playwright WebKit and Chromium, phone viewport, CDP virtual authenticator | Pairing via the link (no camera), storage partitioning, sealed fetches, notification replacement through the service worker API | T1/T2, fully automated |
| iOS behaviour | iOS Simulator (Xcode) with Safari, Add to Home Screen, enrolled Face ID | Installed-PWA behaviour, passkey prompts, Web Push where the Simulator supports it (spike S2), layout | T2 for P3/P4 groups, driven by `xcrun simctl` scripts |
| Real device | The owner's iPhone, guided session | Camera QR scan, Face ID, Web Push delivery and replacement, logout wipe | End of P3, P4 and P8. About 20 minutes each, run from `e2e/iphone/session-N.md` |

Before an iPhone session, the agent:

1. deploys the build;
2. resets the demo data;
3. sends the owner the checklist and the relay URL.

The owner reports pass or fail per step, and screenshots go into Drop.

## 23. Definition of done

- **Task (implementer):** T0 and T1 green, a new test for the behaviour, no unrelated changes, and a report with
  the commands run and their output.
- **Group (manager):** T2 green, the security reviewer passed (if security-relevant), the code reviewer passed, the
  docs updated (protocol docs, `docs/remote.md` and so on), and a PR with test evidence and the e2e scenario list.
- **Phase:** every group merged by the owner, T3 green, the iPhone session passed (where there is one), and the
  phase's exit criteria from §13 shown in a short recorded e2e run.

## 24. Before the first implementation task

1. The owner reviews and approves this spec (the §14 items are decided).
2. The owner completes M1–M5 and M10, the VPS (§20) and DNS (§21).
3. The manager runs P0, group 1: the folder layout, `bin/dev-env --check`, VPS provisioning, an empty relay
   deployed, and the e2e harness with one green "hello" scenario.
4. Phased GitHub issues are created across the three repos from §13. Implementation starts with P0, group 2:
   dev mode #251.
