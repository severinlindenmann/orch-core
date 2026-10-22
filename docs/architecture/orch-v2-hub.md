# orch v2: orch hub, D67–D78

Status: **draft for the owner's review**, 22 Oct 2026. Numbers continue after D63–D66. Decisions taken with the owner
on 22 Oct 2026; the visual draft is `~/orch-dev/mockups/hub-vision/index.html` (v3). Nothing here is built yet.

This amends, in hub mode only: D2 and D3 (a second holder of `WK_e`), D6, D11, D41, D49, D66 (a new factor), §4
(threat model: a hub row), orch-v2-ticket-format (the delegation check and `member.added`: aliases and member kind
`hub`), §5.4 (epochs: `WK_e` goes to the hub over the hub link, and removing a person no longer
rotates), §6.1 (directory), §9 (linked workspaces) and §10 (publish acceptance), as noted per decision. Direct mode, a
workspace with no hub, keeps the current spec unchanged.

Opus design check, 22 Oct 2026: "design OK with fixes". Findings B1–B5 and S1–S7 are folded in below (§5, §6, §7, §8,
§9, §10, §11).

## 1. Why

v2 makes every workspace its own island that dials the relay and publish itself. A person with three workspaces pairs
the phone three times, there is no place to say "group marketing may read workspace Product", and the enterprise
picture needs a separate admin console. The owner wants one management centre per company: workspaces connect to it,
people sign in to it once, and admins work in it.

## 2. Names

| Name | What it is |
|---|---|
| **orch workspace** | A team's or a person's space: tickets, agents, skills, memory, its own keys. |
| **orch core** | The software that runs a workspace (this repo: CLI, host, Mission Control). |
| **orch hub** | The company's control panel. Its own component, new repo `orch-hub`, one per company. |
| **orch-relay**, **orch-publish** | The edge components, as today. |

"Mission Control" stays the name of a workspace's dashboard.

## 3. Decisions

| # | Topic | Decision |
|---|---|---|
| D67 | Hub component | The orch hub is its own component (repo `orch-hub`), not a workspace with an add-on. It runs inside the company network (host or k8s), next to the workspaces. |
| D68 | Only path | In hub mode a workspace host talks only to its hub: ws→ws handoffs, the phone and other channels, publish and the relay all go through it. The host dials out to the hub; nothing dials into a workspace. Amends §9 and §10. |
| D69 | Direct mode | A workspace without a hub is **local only**: `spool:` links on the same machine (D11 *Local*), no phone, no publish, no relay. It joins a hub later with one invite code. Amends D11 (*Internal*/*External* now mean "through a hub"). |
| D70 | Users, groups, roles | The hub holds the users and groups. Each workspace grants a role to groups, never to a person directly: `read` (see tickets, docs, shared spaces), `edit` (work: tickets, agents, answers, handoffs), `admin` (skills, settings, links, releases, approvals: today's owner). A person's role in a workspace is the highest role of their groups. |
| D71 | Hub admin | Members of the `hub-admin` group hold `admin` on every workspace of the hub and manage users and groups. For now there is no workspace-side veto. |
| D72 | Custodial signing | The hub holds each user's signing key, bound to that user, and signs for them. Each signature needs a fresh proof from a strongly bound channel over the event digest (§5, §7); a session alone only shows and routes. The hub signs only payloads it builds itself. Workspace keys (`WSK`, `WXK`, `WK_e`) stay in the workspace host. Amends D41 and D49 in hub mode. |
| D73 | Channels | People reach orch through channels that end at the hub: the iPhone app now, the browser, later Slack and others. The phone pairs **once with the hub** by QR; it never pairs with a workspace. Amends D6 in hub mode. |
| D74 | Trusted hub | The hub is a member of every workspace it serves: each workspace seals its `WK_e` to the hub, so the hub can render content for channels and act for users. The hub is a **trusted operator**, like an identity provider; the edge relay stays blind. |
| D75 | Shared spaces | Shared spaces live on the hub, keyed `SK_e`, held by the hub. Workspaces put filtered slices into them under their share rules (option B of the draft). |
| D76 | Relay and publish behind the hub | The relay's accounts are hubs, not workspaces. It carries hub↔phone push and hub↔hub traffic, ciphertext only. Publish requests stay `WSK`-signed by the workspace and are forwarded by the hub; orch-publish accepts them through a hub only while it holds that workspace's `WSK`-signed `hub.binding` (§8). Namespaces stay per workspace. Amends §6.1 and §10. |
| D77 | Hub to hub | One hub per company. Companies, and subsidiaries of one group, link hub to hub through a relay. Only hub-to-hub objects cross (handoffs, shared spaces); nobody gets direct access to another hub's workspaces. |
| D78 | Hub down | Workspaces keep working locally (agents, tickets, local reads). Channels, publish and handoffs wait and drain when the hub is back. No human-only verb works while the hub is down, except the break-glass operations (§8). No second hub instance for now. |

Later, not now: **Entra ID / OIDC** sign-in and group sync (a must for companies, D70 keeps groups hub-local until
then); Slack and other channels after the phone and browser.

## 4. Components

```
 iPhone app ─┐                                   ┌─ orch-publish  (edge)
 browser ────┤  channel sessions                 │      ▲ hub-signed
 Slack (later)┘          │                        │      │
                         ▼                        │      │
 orch-relay (edge) ◄──── orch hub ────────────────┘      │
  ciphertext only        users · groups · roles         │
  push, hub↔hub          custodial user keys            │
                         directory · router             │
                         shared spaces · audit          │
                         admin console ─────────────────┘
                              ▲  ▲  ▲  workspace links (host dials out)
                              │  │  │
                 orch workspace (orch core host, WSK/WXK/WK_e in its keychain)
```

| Component | Holds keys? |
|---|---|
| orch workspace host | `WSK`, `WXK`, `WK_e` of its workspace (unchanged) |
| orch hub | `HK_op`, its online signing key, certified by the offline `HK_root` (§5); `HXK`, its key-agreement key, which receives `WK_e` and `SK` wraps; each user's signing key `UK`; `SK_e` of the shared spaces; a member copy of each served workspace's `WK_e` |
| orch-relay | None: routing ids and ciphertext |
| orch-publish | Its TLS keys; verifies `HK` against the hub card |
| iPhone app | A channel key in the Secure Enclave, bound to one hub session; no `UK`, no `WK_e` |

## 5. Identity and signatures in hub mode

- **Hub identity: two levels.** At install the hub derives `HK_root` from a recovery code (as `PK`, D50) and keeps it
  offline. `HK_root` certifies an online `HK_op`. Workspaces pin `HK_root`. Rotating `HK_op` (routine, or after a
  suspected compromise of the online host) needs no re-pin. Only a compromise of `HK_root` forces every workspace to
  re-pin. The hub card is `{hub_id, hk_root_pub, hk_op_cert, hxk_pub, relay_url, dev}`. The relay registers hubs by card.
- **Revoking `HK_op`.** `HK_root` signs `hk_op.revoked {cert_id}`. Every `UK` and channel certificate issued under that
  `HK_op` dies with it, because the `UK`s sit in the same key store. The hub creates new `UK`s and certifies them
  under the new `HK_op`. Workspaces pull revocations on every sync and refuse events under a revoked `HK_op`.
- **Signing domains.** `HK_op` signs with a separate domain per object: `orch/v2/hub-card`, `hub-uk-cert`,
  `hub-snapshot`, `hub-route`, `hub-publish`, `hub-audit`. `UK` signs only in `orch/v2/hub-custodial|<verb>`.
- **Users.** A user is `{user_id, display, groups, status}` on the hub. `user_id` is pseudonymous and names live
  only in sealed parts. The hub creates `UK_user` (P-256) in its key store at user creation and certifies
  `{user_id, uk_pub, hub_id, dev}` with `HK_op`. In hub mode `Actor("human", user_id, via=<channel>)` takes the place of a
  device actor.
- **Identity aliases.** A person who already has a `p_` identity in a workspace gets a signed alias
  `{person_id, user_id}` at join. An alias needs the person's own key, or that user's `UK` with a channel proof plus the
  owner's signature. An alias is never re-pointed. Every
  identity check (D59 "never the assignee", gate quorums, the ticket-format delegation check `actor.id ==
  "p_"+owner_person_id`) compares canonical identities, so `p_x` and `u_x` count as one person.
- **Every workspace has an owner `PK`.** A workspace created by a hub user who has no `p_` identity still gets one:
  `orch init` shows the recovery code to the creator, as today. `PK` signs the delegation (§5.3) and is aliased to the
  creator's `user_id`. It is the break-glass key (§8).
- **The hub signs only what it builds (prepare → recompute → sign).** Only a channel can start a signature. The hub
  builds the event from its own `WK_e` view of the workspace: the ticket, the gate hash material, the host-chain head.
  The channel signs the digest with its channel key. The hub checks that proof, then signs with `UK_user`. The
  host↔hub link has a closed operation list (§8). It has no "sign this" request from the host side, so a hostile or
  client-hosted host (D29) cannot get arbitrary events signed.
- **Custodial event.** These fields are added to the event: `auth: "hub"`, `channel_factor` (`se-unlocked`, `passkey`,
  `password+totp`, signed), `channel`, `hub_id`, `workspace_id`, `snapshot_version`, and the channel proof. All of
  them sit inside the signed material, so an event cannot be replayed in another workspace.
- **Channel certificates.** `HK_op` certifies each channel key as `{channel_id, user_id, key_pub, factor}`. Revoked
  channels are listed in the snapshot (§6). The event carries its channel certificate.
- **Verification in the workspace.** The host checks:
  - the chain `UK_user` → `HK_op` cert → pinned `HK_root`, with no revoked `HK_op` along it;
  - the channel proof over the digest, against the channel certificate;
  - the role and channel revocations, both against the newest snapshot it holds when the event arrives (§6).

  Then it records the event. D66 workspace policy maps onto `channel_factor`. A policy that requires
  `secure-enclave` (presence per signature, D41) is met only by `passkey` with user verification. `se-unlocked`
  meets a D49-level policy, and `password+totp` meets neither.
- **Audit.** Every custodial signature and every grant or membership change is in a hash-chained hub audit log.
  Workspaces acknowledge its head on every sync, so a compromised hub cannot silently drop entries. The workspace also
  keeps each signed event.

## 6. Access model

- **Grants** are `{workspace_id, group_id, role}`, set by workspace admins or hub admins. **Memberships** are
  `{user_id, group_id}`, set by hub admins.
- The hub publishes a **group snapshot per workspace**: `{workspace_id, hub_id, generation, version, issued_at,
  roles: {user_id: role}}`, signed by `HK_op`.
  - The host refuses an older `(generation, version)`, so a replayed snapshot cannot bring back a removed user.
  - A hub restored from backup starts a new `generation`, signed under `HK_root`, so it is not locked out for good.
    A `generation` is accepted only if it is strictly higher than the pinned one and signed by `HK_root`.
  - Each snapshot is stored in the workspace ledger, so readers and restores can verify old events.
- Every grant and membership change is signed by the acting admin's `UK`, with a channel proof as in §5.
- **Removing a user** from every group that reaches a workspace makes the hub drop their channel sessions for it;
  the workspace sees the new snapshot. No `WK_e` rotation is needed, because the user never held `WK_e` (D73, D74).
  Removing the **hub** from a workspace, or a hub key compromise, rotates the epoch (§5.4).
- **Agents** stay as they are: workspace-local under the host, or scoped agent devices (D15) granted to one shared
  space on the hub, with an expiry.
- **Dev hubs.** The hub card and every `UK` certificate carry `dev`. A real workspace refuses a dev hub, and a real hub
  refuses a dev workspace join and dev certificates. The test-human override (D34, D51) never applies on the hub path.
  The test that proves the override "never works on a real workspace" is extended to cover all of these.

## 7. Channels

- **iPhone.** Pairing scans the hub's QR once: the app creates a Secure Enclave channel key, the hub binds it to the
  user after the user signs in (hub password + second factor now, Entra later). The app lists every workspace the
  user's roles allow and switches between them. Pushes are sealed by the hub to the channel key and carried by the
  relay (§6.3, with the hub in place of the host). D49's "unlocked phone can sign" becomes "unlocked phone with a live
  hub session can ask the hub to sign".
- **Browser.** The hub serves the person's home (all workspaces, one needs-you list) and the admin console. A
  workspace's Mission Control stays reachable through the hub for users whose role allows it.
- **Strongly bound channel.** A channel holds a non-exportable key bound to one user: the iPhone's Secure Enclave key,
  or a passkey (WebAuthn) in the browser. Every custodial signature needs a fresh signature from that key over the
  event digest (§5). A session token, cookie or Mission Control token only shows and routes. An agent that steals one
  (agents run as the person's UID on macOS) cannot sign. The browser passkey is a new D64 backend (`webauthn`) with its
  own contract.
- **Slack and others (later).** These channels show and route. To sign, they hand off to a strongly bound channel, for
  example a link that opens the passkey prompt or a push to the phone.
- **Sessions.** A channel session lives up to 12 h for showing and routing. `admin` verbs also need a sign-in within the
  last 5 min. Both windows are per hub, and D66 policy may tighten them per verb.

## 8. Workspaces joining and leaving

1. A hub admin creates an invite code for a new or existing workspace.
2. A person with access to the workspace's host runs `orch hub join <code>` on its terminal (human-only verb).
3. The owner's current key signs `hub.joined {hub_id, hk_root_pub, aliases}` into the ledger. The host pins
   `HK_root`, adds a member of the new kind `hub` to its `WSK`-signed member list (D39), and seals `WK_e` to `HXK`
   over the hub link. That link replaces the relay's `PUT membership` (§5.4) for this workspace.
4. The host signs `hub.binding {workspace_id, hub_id, hk_root_pub, valid_from}` with `WSK`. The hub hands it to
   orch-publish and the relay (D76).
5. The workspace switches to hub mode, but **only from step H5 on** (§10). Before H5, `hub join` is a dev and test step
   on dev workspaces. From H5 on, the host stops dialling the relay, refuses any direct-relay envelope, and removes its
   direct-mode phone memberships (paired per D6) with an epoch rotation. The owner's break-glass key is kept. Direct
   publish stops at H6, when publish through the hub exists.

**The host↔hub link** has a closed operation list: `snapshot_get`, `event_deliver` (custodial events in), `route_out`
and `route_in` (ws→ws envelopes), `wk_seal` (epoch wraps to `HXK`), `publish_out` (a `WSK`-signed publish request,
forwarded as is), `audit_ack`. None of them asks the hub to sign anything.

**Linked workspaces through the router (§9).**
- Envelopes keep the sender's `WSK` signature and `WXK` sealing end to end. The receiver checks its pin of the
  sender workspace, not only the hub.
- Across hubs (D77): foreign workspace ids are bound to the foreign hub card, and a hub never re-signs foreign
  envelopes with its own key. D28 "from-peer" applies. A human co-signature verifies as `UK` → foreign `HK_op` cert →
  the pinned foreign hub card.

**Break-glass.** The workspace owner keeps a local key: `PK` from the recovery code, or a passphrase device key (D65).
Break-glass has exactly three powers. Each is a human-only verb from the host terminal, signed by the owner key (not
only by `WSK`), and none needs the hub:
- `orch hub leave` (`PK` or the device key) signs `hub.unbound` with `WSK`, removes the hub member and rotates the
  epoch;
- `orch hub repin` (**`PK` only**, so a stolen device key cannot point the workspace at a hostile hub) pins a new
  `HK_root` after a hub rebuild;
- `orch keys rotate` (`PK` or the device key).

This is no veto on hub admins (D71). It is how a workspace gets away from a lost or compromised hub. Hub compromise
means: break-glass leave, then rotate, then rejoin a rebuilt hub.

**Publish (D76).** orch-publish accepts a hub request for a workspace only while it holds that workspace's
`hub.binding` and no later `hub.unbound`. It evicts the binding like a card. The host still signs every publish request
with `WSK`, as in §10 today, and sends it over `publish_out`. orch-publish verifies `WSK` and `hub.binding`; the hub adds
only a transport signature (`hub-publish`). The agent `publish(...)` path (§5.5) is unchanged: the host evaluates the
charter. Public access still needs a custodial human signature, carried in the request.

## 9. Threat model in hub mode

| Threat | Result | Mitigation |
|---|---|---|
| Hub server or key store taken over | Attacker can read every served workspace and sign as any user | Hub keys in a hardware or OS key store, hub process as its own OS user, no agents on the hub host, a hash-chained audit acknowledged by the workspaces, `HK_op` rotation, break-glass leave and rotate (§8) |
| Hostile or client-hosted workspace host | Could try to get events signed for other workspaces | The hub signs only payloads it builds itself; signatures are bound to `workspace_id`; the link has no signing request (§5, §8) |
| Agent steals a hub session or cookie | Can show and route, cannot sign | Every signature needs a fresh channel-key proof (§7) |
| Malicious or careless hub admin | Can grant themselves `admin` anywhere (D71) | Accepted for now; every grant and membership change is signed by the acting admin's `UK` and audited; a workspace-side veto is an open question |
| Stolen phone | Can sign while the phone is unlocked (as D49 today) | A hub admin revokes the channel key; `admin` verbs need a recent sign-in |
| Rogue hub claims a workspace's publish namespace | Refused | Only with the workspace's `WSK`-signed `hub.binding` (D76) |
| Relay taken over | Sees ciphertext and routing ids only | Unchanged (§4 of the spec) |
| Agent in a workspace | Cannot reach `UK` or the hub's keys | The hub is on another host; the workspace socket API (§5.5) has no hub signing operation |
| Other company's hub (D77) | Sees only objects sent to it | No workspace access crosses hubs |

The honest limit: in hub mode, end-to-end encryption protects workspaces against the **relay and the network**, not
against the hub. The UI says so on the hub's front page and on each workspace card.

## 10. Build order

Nothing below blocks the work in flight (phase-1 e2e, add-ons). The direct path that exists today (relay client
R1–R6 in core, per-workspace phone pairing in orch-mobile, `WSK`-signed publish) stays until the hub path replaces it
at step H5, so daily use keeps working.

| Step | Content | Repos |
|---|---|---|
| H0 | Contract first: `orch-hub/docs/protocol.md` + vectors for the hub card (`HK_root`/`HK_op`), `UK` certificate, signing domains, custodial event with channel proof, group snapshot, `hub.joined`, `hub.binding`/`hub.unbound`, identity alias, member kind `hub`, host↔hub link operations, the `webauthn` channel contract | orch-hub, orch-core |
| H1 | Hub skeleton: install, `HK`, users, groups, grants, admin console (dashboard look and feel), audit log | orch-hub |
| H2 | Workspace join/leave (§8), group snapshot pinning, `auth: hub` verification in core | orch-core, orch-hub |
| H3 | Router: ws→ws through the hub; shared spaces on the hub | orch-hub, orch-core |
| H4 | Browser channel: person home, needs-you across workspaces, custodial signing | orch-hub |
| H5 | Relay with hub accounts; iPhone pairs with the hub; push from the hub. Remove the direct relay path from core (direct publish stays) | orch-relay, orch-mobile, orch-core |
| H6 | Publish through the hub (D76, `publish_out`); then remove direct publish from core | orch-publish, orch-hub, orch-core |
| H7 | Hub to hub through the relay (D77) | orch-hub, orch-relay |
| Later | Entra ID / OIDC and group sync; Slack channel | orch-hub |

Every step touching keys, custody, signatures or the protocol goes through the security reviewer (Opus) before its PR.

## 11. Open questions

1. **Mission Control on the person's own laptop in hub mode.** Proposal (from the Opus check): local Mission Control
   holds no hub session that can sign; a click hands the signature to a strongly bound channel (passkey prompt or
   phone). One user, one key, and no bearer token on a machine where agents run as the person.
2. **Personal workspaces.** A person's own workspace (Anna / personal) joined to the company hub is readable by the hub
   (D74). Is that wanted, or do personal workspaces stay in direct mode and give up the phone?
3. **Hub agents.** May the hub run agents over shared spaces (summaries, routing)? Option B allows it; the scope and
   guard for hub-side agents need their own section.
4. **Workspace-side veto on admin grants** (D71), to revisit before the first real company.
5. **Hub hosting for one person or a small team.** Same machine as the workspaces, or always a separate host?
