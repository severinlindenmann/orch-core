# orch v2: remote access through the hub, D84–D93

Status: design draft, round 2 (29 Oct 2026), after the security reviewer's (Opus) first pass (verdict: rework; all 15
findings taken in, see §13). Extends `orch-v2-hub.md` (D67–D83), `orch-v2-host.md` 0.8.6 item 10 (remote mode:
passkeys, route gate, step-up 10.5) and `contracts/hub/protocol.md` §10 (the host↔hub link).

## 1. Why

A person works on a notebook and has three workspaces. A and B are on the same private network (VPN) as the notebook.
C runs at a customer: it can open outbound HTTPS, nothing can reach it from outside, and the customer will not join
the private network. The person wants to open the hub, move between A, B and C, and hand work between them.

What already works for C with no inbound route (D68: the host dials out, nothing dials into a workspace): needs-you,
tickets and status via `log_sync`, custodial approvals, ws→ws handoffs via `route_out`/`route_in`, shares, spaces,
publish and the phone.

What does not work: the workspace's own Mission Control (detail views, runs, terminals). The hub's switch ticket
(`orch_hub/switch`) sends the browser to the workspace's own address, which the notebook reaches for A and B over the
private network and never for C.

This document adds **remote access**: Mission Control of a workspace, used from the person's device, carried over the
link the workspace already dials out.

## 2. The rule this design is built around

> **Taking over the link, the hub, the relay machine or the network gives no more control over a workspace than
> the workspace already gives the hub today. Remote access to agents, runs and terminals needs keys that only the
> workspace and the person's device hold, is checked by the workspace on every session and every agent-work action,
> and is off unless the workspace's owner turned it on at the workspace itself.**

**Scope of the guarantee.** For a workspace joined in **selective** mode (D80) the guarantee is complete: a
compromised hub can neither open a session nor steer agent work. For a workspace joined in **full** mode the hub is a
trusted operator by D72/D74 (custodial `policy.changed`, gate decisions, `settings.changed`, §8.8 of the protocol) and
can steer work that is already allowed to run, without any session. Remote access adds no power to a full-mode hub,
but it cannot take that power away. That is why D93 makes selective join a condition for customer workspaces.

A design change that breaks this rule is a reopened decision, not an implementation detail.

## 3. Decisions

| # | Name | Decision |
|---|---|---|
| D84 | Opt-in at the workspace | Remote access is **off** for every workspace, also after `hub join`. Only the workspace's owner turns it on, **at the workspace**: a click in Mission Control in a session opened on that computer (loopback) or `orch remote on` at the host terminal, signed by the owner's local key and carrying the owner proof of #1198 C8 (person key `PK`). It is a human-only verb. A remote session, the hub, a hub admin, a channel or an agent can never turn it on, widen it, change its level or end date, or enroll a device. Turning it **off** works from the same places and also from any open remote session of the owner (turning off never needs more than turning on). |
| D85 | Opted out still dials out | Off means: **no remote session can be opened into the workspace.** The link stays up and everything the workspace starts itself keeps working: `log_sync`, handoffs it sends, shares, custodial approvals within its hub mode, publish, the phone. Handoffs it receives arrive as `from-peer` tickets and run only under the workspace's own peer rule (D28), unchanged. The host refuses every `stream_open` (§7) with `remote.off` before any byte reaches the dashboard. The hub cannot tell an opted-out workspace to open one. Device enrollment (§5) is the one stream kind allowed while off, and it only adds a device the owner confirms at the workspace. |
| D86 | End to end, the hub forwards ciphertext | A remote session is an end-to-end encrypted, mutually authenticated channel between the person's **remote client** (§6) and the workspace host. The hub and the relay machine forward opaque bytes. The hub never holds a key of the session, never sees the content, and cannot inject or change a byte without the session failing. |
| D87 | Keys the hub cannot make | A session is opened with a **remote device** enrolled directly at the workspace (§5) and verified by the workspace against its own `WSK`-signed device list. Hub users, hub roles, `UK` certificates, channel certificates, hub sessions and switch tickets grant nothing here. A hub role can hide a workspace from a person, never grant access. |
| D88 | Presence for every agent-work action | Opening a session needs a fresh **presence signature** (§5) over a host challenge bound to the session transcript. On a `remote-device` session the same presence signature is needed for every request that starts or changes agent work (the `presence` rule of D90), not only for host 10.5's step-up list. The device may confirm one batch of at most 5 minutes, and the prompt, drawn by the remote client, names the workspace and the actions. Each terminal open or attach needs its own presence signature. |
| D89 | Ends in-process, never on a socket | The host terminates a stream **in-process** at the dashboard app, as an ASGI scope with `orch_listener = "remote-device"` and the session identity (device, person, level, `stream_id`) set from the handshake, never from a header or a token. It never opens a TCP or Unix connection to any listener for a stream. Inside a stream, every `pre_login` route (passkey login and enrollment, `/auth/switch`) and every outside bearer is refused. A token minted inside a stream is bound to its `stream_id` and is refused on every listener and every other stream. The link gets no port forward and no operation that runs anything. |
| D90 | Its own route column, refused by default | The route table gets a **`remote-device` column**, separate from the front listener's, **`refused` unless listed**, with a test that walks the app's real routes (like `tests/test_remote_route_gate.py`). Values: `allowed` (reads and plain actions), `presence` (D88), `refused`. The owner picks a level at opt-in: **View** (reads only), **Work** (View plus `allowed` and `presence` routes), **Terminals** (Work plus terminal routes, each with presence). Terminals is a separate, explicit choice, never a default. **Refused at every level**, explicitly: hub connect, mode and leave; passkey enroll and remove; device add and remove; the remote policy; custody on (One click); runner on; grant reveal and deposit; add-on install, stage and settings (`market/*`); claude-trust; share rule add and sync; `shares/register`; records auto-on; guard, permissions and charter; setup. A new route is refused on remote until someone decides otherwise. |
| D91 | Time-boxed and visible | Remote access has an end date picked at opt-in: 1, 3 or 7 days; "until I turn it off" only when the owner marks the workspace as **their own** at opt-in (an explicit choice, never inferred; not offered for Terminals). Sessions live at most 12 h and close after 2 h idle. Turning off, expiry, a level change or removing a device closes every affected stream and revokes its tokens **in the same lock hold** that changes the policy; the level and the device are checked on every request, never cached per stream. The workspace shows every open remote session (person, device, since, level) with **End**, and writes every session open, presence signature and close to its own hash-chained log, which the hub cannot suppress or rewrite. |
| D92 | No downgrade on the link | The link keeps TLS 1.3 with the card's SPKI pin and the `WSK` signature bound to the TLS exporter (protocol §10.1). It may also run as a WebSocket on 443 and through an HTTP CONNECT proxy, **with that TLS end to end between host and hub**: when a WebSocket hop is terminated by a front proxy, the link's own TLS runs inside it. The public passthrough never terminates TLS. A TLS-inspecting proxy makes the pin fail and the link stay down; the host never falls back to an unpinned, unbound or plain connection. The fix is an inspection exemption for the hub's name at the customer. |
| D93 | Customer workspaces join selective | At opt-in the owner says whether the workspace runs **on someone else's premises**. If yes, remote access can be turned on only while the workspace is joined selective (D80), and switching to full mode turns remote access off. For a full-mode workspace, opt-in shows plainly: "The hub can already act for people in this workspace. Remote access does not change that." No custodial type, in any mode, can: create a usable grant, start a run, open a terminal, or touch the remote policy or devices (protocol §8.5 gets this as a normative list, each item with a refusal vector). |

## 4. Components and flow

```
person's device (notebook)                     public VPS                       hub (private network)
┌──────────────────────────┐            ┌───────────────────────┐          ┌──────────────────────────┐
│ remote client            │  TLS/443   │ passthrough by name   │  VPN     │ link server              │
│  · Mission Control UI    ├───────────►│ (never terminates TLS)├─────────►│ stream router            │
│    from the local install│            │ relay · publish       │          │  forwards opaque frames  │
│  · transport + presence  │            └───────────▲───────────┘          │ console/admin: VPN only  │
│    keys                  │                        │                      └────────────▲─────────────┘
└──────────────────────────┘                        │ outbound only (TLS/443,           │ link over VPN
        ▲  end-to-end session (D86)                 │ WebSocket or CONNECT)             │
        │  only device ⇄ workspace can read it      │                                   │
        │                              ┌────────────┴────────────┐          ┌───────────┴────────────┐
        └──────────────────────────────┤ workspace C (customer,  │          │ workspace A, B         │
                                       │ selective join)         │          │ same, link over VPN    │
                                       │ host: device list,      │          └────────────────────────┘
                                       │ remote policy, stream   │
                                       │ terminator → dashboard  │
                                       │ app (remote-device)     │
                                       └─────────────────────────┘
```

- The hub's link port is the only hub port reachable from the internet, through a passthrough on the public VPS that
  forwards TLS by name and terminates nothing. Console and admin are on the private network only.
- Alternative transport: C joins the private network itself, with the VPN client running unprivileged on C (userspace
  network stack, outbound 443 only, a one-off setup key, a policy that allows C → hub link port and nothing else).
  Then nothing is public. Every rule here stays the same: whoever controls the VPN's management can add peers and
  change policies, which gives network reach, never a session.

## 5. Devices

**Two keys per device** (one key cannot do both on today's hardware):

- **Transport key:** an X25519 static key in software, used only for the Noise handshake (§7.2). It authenticates
  nothing on its own: if `WRK` leaks, Noise's static-static term falls to key-compromise impersonation, so the host
  never treats a finished handshake as a login.
- **Presence key:** the **only authenticator**. A P-256 key in the Secure Enclave with a `.userPresence` access
  control (macOS, iOS), a TPM key behind Windows Hello (Windows), or a FIDO2 credential used with plain CTAP2
  `getAssertion` with **user verification** required, the sign counter recorded and a decrease refused (Linux, and as a
  fallback everywhere). A touch without user verification is not enough (a stolen notebook with the key plugged in
  would pass). A key file is refused. The D64 backend list gets a `remote-presence` kind. Only the remote client binary
  can use the key (keychain access group and entitlement on Apple platforms).

**Enrollment** (an enrollment stream, allowed while remote access is off, D85):

1. In Mission Control opened on that computer, or `orch remote device add` at the terminal, the owner makes a one-time
   code: at least 128 random bits, single use, 15 min, 5 wrong tries burn it. The code carries the fingerprint of the
   workspace's **remote key** `WRK` (an X25519 key of the host, used only as the Noise responder static, separate from
   `WSK`/`WXK`, rotated by the owner; rotation ends every session). Hand the code over out of band. Setup says: never
   send it through a hub channel.
2. The device opens an enrollment stream: Noise with the pinned `WRK`, then, inside it, proves the code with
   `HMAC(code, transcript_hash | transport_pub | presence_pub)` and a presence signature over the same transcript.
3. The host assigns `device_id` and shows **"Add device <name>, fingerprint <words>?"** on the workspace. The owner
   confirms there (loopback or terminal); the device shows the same fingerprint. Without that confirmation the device
   is not added.

**The device list** is in the host state, signed with `WSK`, mode 0600, out of every agent's reach once #1198 H1 is
active. Each entry: `device_id`, person, transport and presence public keys, level cap, expiry, added at, confirmed by.
Removing a device ends its sessions at once (D91).

**Same-UID agents on the host.** Until H1 is active on the workspace host, an agent there could add its own device,
as it can already sign with the One-click key. Turning remote access on then shows "Agents on this computer can act as
you" and is not offered by default (host 10.x, same rule).

## 6. The remote client

A small part of the orch install on the person's device. It serves Mission Control's UI on loopback **from the code
installed on that device**, never from the hub and never from the workspace, and opens sessions (§7) through the hub.
If the hub served the page, a compromised hub could serve a page that drives an open session, and no transport
protection helps against that. A browser-only variant is open question 1.

- The loopback server checks Host and Origin exactly, needs a per-launch token, sends no CORS headers and serves only
  this UI.
- It draws every presence prompt itself, naming the workspace and the action(s); a page cannot draw it.
- It states the workspace API version range it supports; the host refuses a session outside its own range
  (`remote.version`).

## 7. The session

### 7.1 On the link

The link's closed operation list (protocol §10.2) gets three operations, with the same signing, mode and `ts` (120 s)
rules as the others: `stream_open {stream_id, device_id, kind: "session"|"enroll"}` (hub → host), `stream_data
{stream_id, seq, bytes}` (both ways), `stream_close {stream_id, reason}` (both ways). None of them asks the hub to sign
anything for a person. Frames are at most 64 KiB; the per-frame signature cost is part of the byte budget. The host:

1. refuses `stream_open` with `remote.off` unless remote access is on and not expired (`kind: "enroll"` excepted, §5),
   with `remote.device` unless `device_id` is listed and not expired, and with `remote.busy` past its limits (4 streams
   per device, 16 per workspace, 10 handshakes per minute per device, a byte budget per stream). One hub can use up the
   workspace's 16 streams; that is accepted, the customer's people keep local access;
2. passes `stream_data` bytes only to its stream terminator (§7.2), never to a socket;
3. after the handshake and the presence signature, hands the decrypted stream to the dashboard app in-process (D89).

The hub sees stream ids, sizes and timing, and nothing more.

### 7.2 Handshake

`Noise_IK_25519_ChaChaPoly_SHA256`, initiator = device (transport key), responder = workspace (`WRK`, pinned at
enrollment). The prologue binds `workspace_id`, `device_id`, `stream_id`, `hub_id` and the protocol version.
Message 1 carries an empty payload (no early data); a replayed message 1 costs only handshake budget. Then:

- in message 2 the host sends a 32-byte challenge, single use, valid 60 s;
- the device signs `orch/v2/remote-open | workspace_id | device_id | handshake_hash | challenge` with the
  presence key;
- the host verifies against the device list, then opens the session.

### 7.3 Presence for actions (D88) and step-up

The **click signer** owns presence for actions, as for passkey step-up today (host 10.5): it issues the nonce, the
stream terminator passes the stream's `handshake_hash` to the signer, and the device signs
`orch/v2/remote-presence | workspace_id | device_id | handshake_hash | nonce | view_digest[]` with the presence key.
The signer verifies it itself against the `WSK`-signed device list and consumes the nonce once. A batch (≤ 5 min)
lists every `view_digest` it covers. The dashboard server never vouches for a presence signature.

### 7.4 What the host checks, always

Remote access on and not expired; device listed, not expired, level ≤ the workspace level; presence signature over
this handshake; route allowed for the level in the `remote-device` column; presence for `presence` routes; guard,
charter and permissions as for every local action. None of these checks asks the hub anything.

## 8. The link transports (D92)

- **Direct:** TLS 1.3 to the hub's link port, as today.
- **WebSocket on 443:** the framed link inside a WebSocket. Where the WebSocket hop ends at a front proxy, the link's
  pinned, exporter-bound TLS runs inside it.
- **HTTP CONNECT proxy:** a `CONNECT` tunnel to either of the above, TLS inside it. Proxy credentials come from the
  host config, never from the hub.
- **Close the L7 shortcut first, both directions:** today `seq` is carried and type-checked only
  (`orch_hub/link/server.py` "SHORTCUT (L7)"), and the host does not enforce h2w `seq` (protocol §10.2 step 7). Both
  sides get a replay store before remote access ships.

## 9. Opted out (D85), exactly

| Thing | Off | On |
|---|---|---|
| Link to the hub | up | up |
| `log_sync`, status, needs-you on the hub | yes | yes |
| Handoffs the workspace sends | yes | yes |
| Handoffs to the workspace | `from-peer` tickets, run only under its own peer rule (D28) | same |
| Shares, spaces, publish, phone, custodial approvals | as the hub mode allows | same |
| `stream_open` session | `remote.off`, nothing reaches the dashboard | checked as in §7 |
| `stream_open` enroll | allowed, adds a device only after the owner confirms at the workspace | same |
| Mission Control from elsewhere | only where the network reaches the workspace directly (its own remote mode, host 10.x) | also through the hub |

## 10. Threat model

| Attacker | Wants | Stopped by |
|---|---|---|
| Network or customer proxy | Read or steer the link | TLS 1.3 with SPKI pin, `WSK` bound to the exporter, TLS end to end through WS/CONNECT; inspection breaks the link, no fallback (D92) |
| Whoever controls the public VPS / passthrough | Read or steer sessions or the link | Sees TLS ciphertext only; the link ends at the hub, sessions end at the workspace |
| Compromised hub or malicious hub admin | Open a session, drive agents or terminals | No presence key, no device entry; the host checks the device list and presence itself (D87, D88); switch tickets and `pre_login` routes refused in streams (D89); it cannot enable remote access or enroll a device (D84, §5 step 3) |
| Same, on a full-mode workspace | Steer agent work without a session | Not stopped: D72/D74 trust. Customer workspaces must be selective (D93); custodial types can never start a run, make a grant, open a terminal or touch remote policy (D93) |
| Compromised hub | Replay a recorded session, handshake or presence | Prologue binds `stream_id`; presence signatures bind `handshake_hash` and a single-use nonce; msg1 has no payload |
| Compromised hub | Serve a page that drives a session | The UI comes from the device's own install (§6) |
| Remote session | Get past its level | `remote-device` column, refused by default; hub connect, passkeys, devices, custody, runner, grants, add-ons and the rest refused at every level (D90) |
| Agent on the person's notebook | Use the device to reach C | Presence for session open, every agent-work action and every terminal (D88); the prompt is drawn by the client. Residual: an agent can make reads in a session the person opened, and act inside a batch the person confirmed (≤ 5 min, actions listed) |
| Stolen notebook | Same | Presence with user verification; remove the device at the workspace |
| Leaked enrollment code | Enroll an attacker device | Owner confirms the device fingerprint at the workspace (§5) |
| `WRK` leaks | Impersonate devices to the host | Noise alone is not a login; the presence signature is the only authenticator (§5) |
| Agent on the workspace host | Add its own device | #1198 H1; until then not offered by default (§5) |
| Owner turns remote off during a session | The session keeps going | Streams closed and tokens revoked in the same lock hold; level checked per request (D91) |
| Anyone | Exhaust the workspace | Stream, handshake and byte limits (§7.1); the customer can always cut the outbound connection |

**Not covered here, said plainly:** a full-mode hub can steer allowed work through custodial events (D72); an agent
on the notebook can read what an open session shows; a compromised workspace host is the workspace.

## 11. Build order and tests

| Step | What | T0 / adversarial tests |
|---|---|---|
| R0 | Contract: stream ops in the closed list, `remote.*` codes, Noise string, transcript and signing domains, presence messages, device list format, the normative "no custodial type can" list (D93) | vectors replayed by host and hub; each "no custodial type can" item refused |
| R1 | L7 replay store, both directions | a replayed link message (w2h and h2w) on a new connection refused |
| R2 | Host: remote policy (D84, D90, D91, D93), device list, enrollment stream with owner confirmation, `stream_open` gate | `stream_open` while off → `remote.off`, no dashboard call; hub-sent policy or device change refused; enroll without owner confirmation adds nothing; turning off mid-session closes the stream and its tokens; switching selective → full turns remote off |
| R3 | Host: Noise IK, presence verification in the signer, in-process termination with the `remote-device` column | route walk: every route has a `remote-device` entry and the D90 list is refused at every level; `/auth/switch` and passkey routes refused in a stream; a stream token refused on every listener; handshake from another workspace/device/stream refused; presence from another session refused; key file and UP-only FIDO2 refused |
| R4 | Hub: stream router, limits | flipped byte closes the session; limits enforced |
| R5 | Remote client (macOS SE, FIDO2 UV fallback), loopback hardening | UI only from the local install; wrong Origin/Host/token refused; version range enforced |
| R6 | Link over WebSocket/443 and CONNECT | TLS-inspecting proxy in e2e → link down, no fallback |
| R7 | e2e: A, B on the private network, C selective behind an outbound-only firewall; switch A → C → B, handoff C → A, agent start on C asks for presence, opt out on C while a session is open | session ends on opt-out; handoff still works opted out |

Every step goes through the security reviewer before its PR.

## 12. Open questions

1. **Browser-only variant**, no install on the device: the passthrough forwards the browser's TLS by name straight to
   the workspace, which holds its own certificate and passkeys (RP ID = that name). Needs a domain the hub does not
   control and a CAA record with `accounturi` pinned to the workspace's ACME account (RFC 8657), because whoever
   receives traffic for a name can otherwise get a certificate for it. Proposed: later, as an option.
2. Custodial events for selective customer workspaces: should they also require a presence key enrolled at the
   workspace instead of a hub-certified channel key? That would close the remaining D72 gap. Proposed: yes, as the
   next decision after this one.

## 13. Review log

- Round 1 (Opus, 29 Oct): rework. Taken in: own `remote-device` route column refused by default with the explicit
  list (1); in-process termination, no `pre_login` or switch in streams, stream-bound tokens (2); presence for every
  agent-work action and per terminal (3); presence verified by the signer (4); guarantee scoped to selective, D93 a
  condition for customer workspaces, normative "no custodial type can" list (5); two keys per device, UV required (6);
  revoke in the same lock, per-request checks (7); enrollment stream with owner confirmation (8); Noise details (9);
  stream ops in the closed list, L7 both directions (10); loopback hardening (11); TLS end to end through WS/CONNECT
  (12); explicit "own workspace" choice (13); version range (14); DoS stated (15).
