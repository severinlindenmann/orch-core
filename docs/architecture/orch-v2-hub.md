# orch v2: orch hub, D67–D78

Status: **draft for the owner's review**, 22 Oct 2026. Numbers continue after D63–D66. Decisions taken with the owner
on 22 Oct 2026; the visual draft is `~/orch-dev/mockups/hub-vision/index.html` (v3). Nothing here is built yet.

This amends D3, D6, D11, D41 and D49 (in hub mode only), §6.1 (directory), §9 (linked workspaces) and §10 (publish
signing), as noted per decision. Direct mode, a workspace with no hub, keeps the current spec unchanged.

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
| D72 | Custodial signing | The hub holds each user's signing key, bound to that user, and signs for them after the user authenticates to the hub through a channel. Workspace keys (`WSK`, `WXK`, `WK_e`) stay in the workspace host (D3 unchanged for them). Amends D41 and D49 in hub mode. |
| D73 | Channels | People reach orch through channels that end at the hub: the iPhone app now, the browser, later Slack and others. The phone pairs **once with the hub** by QR; it never pairs with a workspace. Amends D6 in hub mode. |
| D74 | Trusted hub | The hub is a member of every workspace it serves: each workspace seals its `WK_e` to the hub, so the hub can render content for channels and act for users. The hub is a **trusted operator**, like an identity provider; the edge relay stays blind. |
| D75 | Shared spaces | Shared spaces live on the hub, keyed `SK_e`, held by the hub. Workspaces put filtered slices into them under their share rules (option B of the draft). |
| D76 | Relay and publish behind the hub | The relay's accounts are hubs, not workspaces. It carries hub↔phone push and hub↔hub traffic, ciphertext only. Publish accepts requests signed by the hub that carry the workspace admin's signature; namespaces stay per workspace. Amends §6.1 and §10. |
| D77 | Hub to hub | One hub per company. Companies, and subsidiaries of one group, link hub to hub through a relay. Only hub-to-hub objects cross (handoffs, shared spaces); nobody gets direct access to another hub's workspaces. |
| D78 | Hub down | Workspaces keep working locally. Channels, publish and handoffs wait and drain when the hub is back. No second hub instance for now. |

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
| orch hub | Its own hub key `HK` (signs cards, routing, publish requests), each user's signing key `UK`, `SK_e` of the shared spaces, a member copy of each served workspace's `WK_e` |
| orch-relay | None: routing ids and ciphertext |
| orch-publish | Its TLS keys; verifies `HK` against the hub card |
| iPhone app | A channel key in the Secure Enclave, bound to one hub session; no `UK`, no `WK_e` |

## 5. Identity and signatures in hub mode

- **Hub identity.** `HK` is created at hub install; its public half and the hub's relay address form the **hub card**.
  The relay registers hubs by card. The install prints a recovery code for `HK`, as for `PK` (D50).
- **Users.** A user is `{user_id, display, groups, status}` on the hub. The hub creates `UK_user` (P-256) in its key
  store at user creation and signs `{user_id, uk_pub}` with `HK`. In hub mode `user_id` takes the place of
  `person_id`, and `Actor("human", user_id, via=<channel session>)` the place of a device.
- **Custodial signature.** A human-only verb from any channel produces the same signed event as today, with:
  `auth: "hub"`, `channel` (`ios`, `browser`, `slack`, …), `session_id`, `hub_id`, signed by `UK_user`. The hub signs only
  after a fresh channel authentication (§7) and only for verbs the user's role allows in that workspace (D70).
- **Verification in the workspace.** The host verifies `UK_user` through the `HK` certificate it pinned when it joined
  the hub, checks the role from the hub-signed group snapshot (§6), and records the event. D66 workspace policy can
  still require a stronger factor for named verbs; `hub` is a factor value like `passphrase`.
- **Audit.** Every custodial signature is logged on the hub with channel and session, and the workspace keeps the
  signed event, so each side can check the other.

## 6. Access model

- **Grants** are `{workspace_id, group_id, role}`, set by workspace admins or hub admins. **Memberships** are
  `{user_id, group_id}`, set by hub admins.
- The hub publishes a **group snapshot per workspace**: `{workspace_id, version, roles: {user_id: role}}`, signed by
  `HK`. The host pins the latest version and refuses an older one, so a replayed snapshot cannot bring back a removed
  user.
- **Removing a user** from every group that reaches a workspace makes the hub drop their channel sessions for it;
  the workspace sees the new snapshot. No `WK_e` rotation is needed, because the user never held `WK_e` (D73, D74).
  Removing the **hub** from a workspace, or a hub key compromise, rotates the epoch (§5.4).
- **Agents** stay as they are: workspace-local under the host, or scoped agent devices (D15) granted to one shared
  space on the hub, with an expiry.
- Dev certificates (`dev: true`) are refused by a hub not marked dev, as by real workspaces today.

## 7. Channels

- **iPhone.** Pairing scans the hub's QR once: the app creates a Secure Enclave channel key, the hub binds it to the
  user after the user signs in (hub password + second factor now, Entra later). The app lists every workspace the
  user's roles allow and switches between them. Pushes are sealed by the hub to the channel key and carried by the
  relay (§6.3, with the hub in place of the host). D49's "unlocked phone can sign" becomes "unlocked phone with a live
  hub session can ask the hub to sign".
- **Browser.** The hub serves the person's home (all workspaces, one needs-you list) and the admin console. A
  workspace's Mission Control stays reachable through the hub for users whose role allows it.
- **Slack and others (later).** Same rule: the channel authenticates to the hub, the hub signs. A channel that cannot
  bind a user strongly (a shared Slack channel) may only show and route, never sign.
- **Fresh authentication.** Signing needs a channel session younger than a policy window (default 12 h) and, for
  `admin` verbs, a re-authentication within 5 min. The window is per hub; D66 policy may tighten it per verb.

## 8. Workspaces joining and leaving

1. A hub admin creates an invite code for a new or existing workspace.
2. A person with access to the workspace's host runs `orch hub join <code>` on its terminal (human-only verb).
3. The host pins `HK`, registers its card with the hub, and seals `WK_e` to the hub's member key; the hub becomes a
   member device with full scope.
4. The workspace switches to hub mode: the host stops dialling the relay and publish, and its earlier direct-mode
   device memberships (phones paired per D6) are removed with an epoch rotation.

`orch hub leave` (workspace admin, from the host terminal) removes the hub's membership, rotates, and returns the
workspace to direct mode, local only.

## 9. Threat model in hub mode

| Threat | Result | Mitigation |
|---|---|---|
| Hub server or key store taken over | Attacker can read every served workspace and sign as any user | Hub keys in a hardware or OS key store, hub process as its own OS user, no agents on the hub host, signed audit log the workspaces can check, epoch rotation after recovery |
| Malicious or careless hub admin | Can grant themselves `admin` anywhere (D71) | Accepted for now; every grant and membership change is signed by the acting admin's `UK` and audited; a workspace-side veto is an open question |
| Stolen phone | Can ask the hub to sign while the session lives | Short session window, re-auth for admin verbs, hub admin revokes the channel key |
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
| H0 | Contract first: `orch-hub/docs/protocol.md` + vectors for hub card, `UK` certificate, custodial event, group snapshot, host↔hub link envelopes | orch-hub, orch-core |
| H1 | Hub skeleton: install, `HK`, users, groups, grants, admin console (dashboard look and feel), audit log | orch-hub |
| H2 | Workspace join/leave (§8), group snapshot pinning, `auth: hub` verification in core | orch-core, orch-hub |
| H3 | Router: ws→ws through the hub; shared spaces on the hub | orch-hub, orch-core |
| H4 | Browser channel: person home, needs-you across workspaces, custodial signing | orch-hub |
| H5 | Relay with hub accounts; iPhone pairs with the hub; push from the hub. Remove the direct relay path from core | orch-relay, orch-mobile, orch-core |
| H6 | Publish through the hub (D76) | orch-publish, orch-hub |
| H7 | Hub to hub through the relay (D77) | orch-hub, orch-relay |
| Later | Entra ID / OIDC and group sync; Slack channel | orch-hub |

Every step touching keys, custody, signatures or the protocol goes through the security reviewer (Opus) before its PR.

## 11. Open questions

1. **Mission Control on the person's own laptop in hub mode.** Does a click in a local Mission Control sign with the
   local key (direct custody, D64) or go through the hub? Proposal: through the hub, so one user has one key.
2. **Personal workspaces.** A person's own workspace (Anna / personal) joined to the company hub is readable by the hub
   (D74). Is that wanted, or do personal workspaces stay in direct mode and give up the phone?
3. **Hub agents.** May the hub run agents over shared spaces (summaries, routing)? Option B allows it; the scope and
   guard for hub-side agents need their own section.
4. **Workspace-side veto on admin grants** (D71), to revisit before the first real company.
5. **Hub hosting for one person or a small team.** Same machine as the workspaces, or always a separate host?
