# orch v2: remote access through the hub, D84–D93

Status: design draft (29 Oct 2026), for the security reviewer (Opus) before any build. Extends `orch-v2-hub.md`
(D67–D83), `orch-v2-host.md` 0.8.6 item 10 (remote mode: passkeys, route gate, step-up) and
`contracts/hub/protocol.md` §10 (the host↔hub link).

## 1. Why

A person works on a notebook and has three workspaces. A and B are on the same private network (VPN) as the notebook.
C runs at a customer: it can open outbound HTTPS, nothing can reach it from outside, and the customer will not join
the private network. The person wants to open the hub and move between A, B and C, and hand work between them.

What already works for C with no inbound route (D68: the host dials out, nothing dials into a workspace): needs-you,
tickets and status via `log_sync`, custodial approvals, ws→ws handoffs via `route_out`/`route_in`, shares, spaces,
publish and the phone.

What does not work: the workspace's own Mission Control (detail views, runs, terminals). The hub's switch ticket
(`orch_hub/switch`) sends the browser to the workspace's own address, which the notebook can reach for A and B over
the private network and never for C.

This document adds **remote access**: Mission Control of a workspace, used from the person's device, carried over the
link the workspace already dials out.

## 2. The rule this design is built around

> **Taking over the link, the hub, the relay machine or the network gives no more control over a workspace than
> the workspace already gives the hub today. Remote access to agents, runs and terminals needs keys that only the
> workspace and the person's device hold, is checked by the workspace on every session and every step-up, and is
> off unless the workspace's owner turned it on at the workspace itself.**

Everything below follows from that rule. A design change that breaks it is a reopened decision, not an
implementation detail.

## 3. Decisions

| # | Name | Decision |
|---|---|---|
| D84 | Opt-in at the workspace | Remote access is **off** for every workspace, also after `hub join`. Only the workspace's owner turns it on, **at the workspace**: a click in Mission Control opened on that computer (loopback session) or `orch remote on` at the host terminal, signed by the owner's local key and carrying the owner proof of #1198 C8 (person key `PK`). It is a human-only verb. A remote session, the hub, a hub admin, a channel or an agent can never turn it on, widen it, or enroll a device. Turning it off works from the same places and also from any open remote session of the owner (turning off never needs more than turning on). |
| D85 | Opted out still dials out | Off means: **no remote session can be opened into the workspace.** The link stays up and everything the workspace starts itself keeps working: `log_sync`, handoffs it sends, handoffs it receives as items for its people to accept, shares, custodial approvals within the hub mode it joined in, publish, the phone. The host refuses every `stream_open` (§7) with `remote.off` before any byte reaches a local socket. The hub cannot tell an opted-out workspace to open one. |
| D86 | End to end, the hub forwards ciphertext | A remote session is an end-to-end encrypted, mutually authenticated channel between the person's **remote client** (§6) and the workspace host. The hub and the relay machine forward opaque bytes. The hub never holds a key of the session, never sees the content, and cannot inject or change a byte without the session failing. |
| D87 | Keys the hub cannot make | A session is opened with a **remote device key** enrolled directly at the workspace (§5) and verified by the workspace against its own device list. Hub users, hub roles, `UK` certificates, channel certificates and hub sessions grant nothing here. A hub role can narrow (a person without a hub role for this workspace is not shown the workspace), never widen. |
| D88 | Presence per session and per step-up | Every session open and every step-up action needs a fresh signature by the device key that requires the person's presence (fingerprint, device PIN or security key), over a host challenge bound to the session transcript (§7.2). A key file without presence is not a remote device key. |
| D89 | Ends at the front listener | The host delivers a remote session only to the dashboard's front (remote) listener and its route gate (host 0.8.6 item 10: `allowed`, `refused`, `step_up`, `pre_login`, `terminals`). Never the loopback listener, never any other socket, port or process. The link gets no generic port forward and no operation that runs anything. |
| D90 | Capability levels chosen at opt-in | The owner picks a level when turning remote access on: **View** (reads only), **Work** (default: View plus the `allowed` and `step_up` routes, which includes approving and starting runs inside the charter), **Terminals** (Work plus `terminals`). Terminals is a separate, explicit choice, never a default. Changing the level is the same human-only verb as D84. Guard, permissions, charter, the remote policy itself, devices and keys stay `refused` on every remote session at every level. |
| D91 | Time-boxed and visible | Remote access has an end date picked at opt-in (1, 3 or 7 days, like agent access, or "until I turn it off" for the owner's own workspaces). Sessions live at most 12 h and close after 2 h idle (host 10.x). The workspace shows every open remote session (person, device, since, level) with **End**, writes every session open, step-up and close to its own hash-chained log, and the hub cannot suppress or rewrite that entry. |
| D92 | No downgrade on the link | The link keeps TLS 1.3 with the card's SPKI pin and the `WSK` signature bound to the TLS exporter (protocol §10.1). It may additionally run as a WebSocket on port 443 and through an HTTP CONNECT proxy, with the same TLS end to end. A TLS-inspecting proxy makes the pin fail and the link stay down; the host never falls back to an unpinned, unbound or plain connection. The fix is an inspection exemption for the hub's name at the customer. |
| D93 | Customer workspaces join selective | Setup recommends **selective** join (D80) for a workspace on someone else's premises, and says why: in full mode the hub is a trusted operator for custodial actions (D72, D74), which remote access does not change and this document does not narrow. |

## 4. Components and flow

```
person's device (notebook)                     public VPS                       hub (private network)
┌──────────────────────────┐            ┌───────────────────────┐          ┌──────────────────────────┐
│ remote client            │  TLS/443   │ passthrough by name   │  VPN     │ link server              │
│  · Mission Control UI    ├───────────►│ (ciphertext only)     ├─────────►│ stream router            │
│    from the local install│            │ relay · publish       │          │  forwards opaque frames  │
│  · device key (presence) │            └───────────▲───────────┘          │ console/admin: VPN only  │
└──────────────────────────┘                        │                      └────────────▲─────────────┘
        ▲  end-to-end session (D86): only device ⇄ workspace can read it                │
        │                                           │ outbound only (TLS/443,           │ link over VPN
        │                                           │ WebSocket or CONNECT)             │
        │                              ┌────────────┴────────────┐          ┌───────────┴────────────┐
        └──────────────────────────────┤ workspace C (customer)  │          │ workspace A, B         │
                                       │ host: device list,      │          │ same, link over VPN    │
                                       │ remote policy, front    │          └────────────────────────┘
                                       │ listener + route gate   │
                                       └─────────────────────────┘
```

- The hub's link port is the only hub port reachable from the internet, through a passthrough on the public VPS that
  forwards TLS by name and terminates nothing. The console and admin are reachable on the private network only.
- The notebook reaches the hub over the private network. A workspace on the private network (A, B) may still be opened
  directly as today; going through the hub is the same session either way.

## 5. Devices

- **Enrollment happens at the workspace.** In Mission Control opened on that computer (or `orch remote device add` at
  the terminal), the owner makes a one-time enrollment code: at least 128 random bits, single use, 15 min, first
  device wins (same rules as host 10.x enrollment links). The code carries the workspace's **remote key** fingerprint
  (`WRK`, an X25519/Ed25519 pair of the host, separate from `WSK`/`WXK`, rotated by the owner) so the device pins the
  workspace. The code is handed over out of band. Setup says: never send it through a hub channel.
- **The device key** is non-exportable where the platform allows it and always needs presence per use: Secure Enclave
  with a user-presence access control on macOS and iOS, TPM with Windows Hello on Windows, a FIDO2 security key
  (`hmac-secret`/`sign` extension) on Linux and as a fallback everywhere. The D64 backend list gets a `remote-device`
  kind. A key file is refused.
- **The device list** lives in the host state, signed with `WSK`, mode 0600, outside every agent's reach once #1198 H1
  is active. Each entry: device id, person, public key, level cap, expiry, added at, added by. Removing a device ends
  its sessions at once.
- **Same-UID agents on the host.** Until H1 is active on the workspace host, an agent there could add its own device,
  as it can already sign with the One-click key. Turning remote access on then shows "Agents on this computer can act
  as you" and is not offered by default (host 10.x, same rule).

## 6. The remote client

A small part of the orch install on the person's device. It serves Mission Control's UI on loopback **from the code
installed on that device**, never from the hub and never from the workspace, and opens sessions (§7) through the hub.
Reason: if the hub served the page, a compromised hub could serve a page that drives the session once it is open,
and no transport protection helps against that. A browser-only variant is in §12.

The client lists the workspaces the hub shows the person and, for each, whether this device is enrolled. Switching is
one click; a step-up asks for the fingerprint or the key touch.

## 7. The session

### 7.1 On the link

The link gets three operations, outside the closed list of protocol §10.2 that asks the hub to sign nothing:
`stream_open {stream_id, device_id}` (hub → host), `stream_data {stream_id, seq, bytes}` (both ways), `stream_close
{stream_id, reason}` (both ways). The host:

1. refuses `stream_open` with `remote.off` unless remote access is on and not expired (D84, D85), with `remote.device`
   unless `device_id` is in its device list and not expired, and with `remote.busy` past its limits (4 streams per
   device, 16 per workspace, a handshake rate of 10 per minute per device, a byte budget per stream);
2. passes `stream_data` bytes only to its own session state machine (§7.2), never to a socket, until the handshake
   has finished and verified;
3. after the handshake, connects the decrypted stream to the front listener only (D89), as a session of kind
   `remote-device` with the device's level as the listener policy.

The hub sees stream ids, sizes and timing. That is the full extent of what it learns.

### 7.2 Handshake

Noise `IK` (X25519, ChaCha20-Poly1305, SHA-256), initiator = device, responder = workspace, with the workspace's
static key `WRK` pinned on the device at enrollment and the device's static key in the host's device list. The
prologue binds `workspace_id`, `device_id`, `stream_id`, the hub id and the protocol version, so a handshake cannot be
replayed into another workspace, device or stream. Then:

- the host sends a 32-byte challenge;
- the device signs `orch/v2/remote-open | workspace_id | device_id | handshake_hash | challenge` with the device key,
  which asks for presence;
- the host verifies against its device list, then opens the session.

A step-up action (route gate `step_up`) runs the same exchange with domain `orch/v2/remote-stepup` and the action's
`view_digest` appended, consumed once by the click signer (host 10.5). Neither signature can be used in
another session, since both include `handshake_hash`.

### 7.3 What the host checks, always

Remote access on and not expired; device listed, not expired, level ≤ the workspace level; presence signature over this
handshake; route allowed for the level by the route gate; step-up for `step_up` routes; guard, charter and permissions
as for every local action. None of these checks asks the hub anything.

## 8. The link transports (D92)

- **Direct:** TLS 1.3 to the hub's link port, as today.
- **WebSocket on 443:** the same framed link inside a WebSocket over TLS, for networks that only pass HTTPS. The SPKI
  pin and the exporter binding apply to that TLS connection unchanged.
- **HTTP CONNECT proxy:** a `CONNECT` tunnel to either of the above, TLS inside it. Proxy credentials come from the
  host config, never from the hub.
- **Closing the L7 shortcut first:** the link's `seq` must get a replay store (today carried and type-checked only,
  `orch_hub/link/server.py` "SHORTCUT (L7)") before remote access ships, so a recorded link message cannot be played
  again into a new connection.

## 9. Opted out (D85), exactly

| Thing | Off | On |
|---|---|---|
| Link to the hub | up | up |
| `log_sync`, status, needs-you on the hub | yes | yes |
| Handoffs the workspace sends | yes | yes |
| Handoffs to the workspace | arrive as items its people accept, as today | same |
| Shares, spaces, publish, phone, custodial approvals | as the hub mode allows | same |
| `stream_open` | `remote.off`, nothing reaches a socket | checked as in §7 |
| Mission Control from another place | only where the network reaches the workspace directly (its own remote mode, host 10.x) | also through the hub |

## 10. Threat model

| Attacker | Wants | Stopped by |
|---|---|---|
| Network or customer proxy | Read or steer the link | TLS 1.3 with SPKI pin, `WSK` bound to the exporter; inspection breaks the link, no fallback (D92) |
| Whoever controls the public VPS / passthrough | Read or steer sessions or the link | Sees TLS ciphertext only; the link ends at the hub and the sessions end at the workspace |
| Compromised hub, or a malicious hub admin | Open a session into C, drive agents or terminals | Has no device key and no `WRK`; the host checks the device list and presence itself (D87, D88). Inside an open session it sees ciphertext only (D86). It cannot turn remote access on or enroll a device (D84). It keeps what full mode already gives it (D93) |
| Compromised hub | Replay a recorded session or step-up | Handshake prologue and signatures bind `stream_id` and `handshake_hash`; challenges single use |
| Compromised hub | Serve a page that drives a session | The UI comes from the device's own install (§6) |
| Agent on the person's notebook | Use the device key to reach C | Presence is required per session and per step-up; the person sees the prompt. An agent can ride a session the person opened: that is the residual risk, limited by level, 2 h idle, step-up for listed actions and **End** at the workspace |
| Stolen notebook | Same | Presence (device unlock or key touch); remove the device at the workspace |
| Leaked enrollment code | Enroll an attacker device | 128 bits, single use, 15 min, first device wins; the workspace shows "Device added" with Remove |
| Agent on the workspace host | Add its own device | #1198 H1; until then not offered by default (§5) |
| Remote session | Widen its own access | Policy, devices, keys, guard, charter are `refused` on remote at every level (D90) |
| Anyone | Exhaust the workspace | Stream, handshake and byte limits (§7.1); the customer can always cut the outbound connection |

**Not covered here, said plainly:** a full-mode hub can act for users through custodial events (D72); a person who
opens a session on a notebook that an agent controls lends that session; a compromised workspace host is the
workspace. The first is why D93 recommends selective join for customer workspaces.

## 11. Build order and tests

| Step | What | T0 / adversarial tests |
|---|---|---|
| R0 | Contract: link stream ops, `remote.*` codes, handshake transcript and signing domains, device list format, vectors | vectors replayed by host and hub |
| R1 | L7 replay store on the link | replayed link message on a new connection refused |
| R2 | Host: remote policy (D84, D90, D91), device list, enrollment, `stream_open` gate | `stream_open` while off → `remote.off` and no socket opened; hub-sent policy or device change refused; remote session cannot reach policy/device routes |
| R3 | Host: Noise IK + presence signatures, delivery to the front listener only | handshake from another workspace/device/stream refused; signature from another session refused; no path to the loopback listener (test walks the listeners); key file refused as device key |
| R4 | Hub: stream router, limits | hub never decrypts (fuzz: flipped byte closes the session); limits enforced |
| R5 | Remote client on the notebook (macOS SE, FIDO2 fallback) | UI served only from the local install |
| R6 | Link over WebSocket/443 and CONNECT | TLS-inspecting proxy in e2e → link down, no fallback |
| R7 | e2e: A, B on the private network, C behind an outbound-only firewall; switch A → C → B, handoff C → A, opt out on C while a session is open | session ends on opt-out; handoff still works opted out |

Every step goes through the security reviewer before its PR.

## 12. Open questions

1. **Browser-only variant**, no install on the device: the passthrough forwards the browser's TLS by name straight to
   the workspace, which holds its own certificate and passkeys (RP ID = that name). Needs a domain the hub does not
   control, and a CAA record with `accounturi` pinned to the workspace's ACME account (RFC 8657), because whoever
   receives traffic for a name can otherwise get a certificate for it. Workable for customers with their own DNS,
   against the no-domain rule otherwise. Proposed: later, as an option.
2. Should a hub role at least be **required** (not only able to narrow) so a hub admin can remove someone from
   remote access centrally? Proposed: yes for hiding, no for granting; the workspace list stays the authority.
3. Custodial events for selective customer workspaces: should they also require a device key enrolled at the workspace
   instead of a hub-certified channel key? That would close the D72 gap for customer workspaces. Proposed: yes, as a
   follow-up decision.
