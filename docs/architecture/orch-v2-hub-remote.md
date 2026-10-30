# orch v2: remote access through the hub, D84–D99

Status: design draft, round 3 (29 Oct 2026). Security reviewer (Opus): round 1 rework, round 2 approve with changes;
all findings taken in (§13). R0 may start. **Amendment A1 (30 Oct 2026, owner decision, answers open question 1 /
#1597): the browser device class, D94–D99, §6a. Opus design review: approve with changes, all taken in (§13); the
points from the design review were confirmed on 30 Oct (manager, owner informed).** Extends `orch-v2-hub.md` (D67–D83), `orch-v2-host.md` 0.8.6 item 10 (remote mode:
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

**Scope of the guarantee.** For a workspace joined in **selective** mode (D80) a compromised hub can neither open a
session nor steer agent work, **except decisions on items the owner shared with the hub** (selective still accepts
`UK` custodial decisions on shared items, hub §8a; open question 2 closes that). For a workspace joined in **full** mode the hub is a
trusted operator by D72/D74 (custodial `policy.changed`, gate decisions, `settings.changed`, §8.8 of the protocol) and
can steer work that is already allowed to run, without any session. Remote access adds no power to a full-mode hub,
but it cannot take that power away. That is why D93 makes selective join a condition for customer workspaces.

**Browsers (D94–D99, owner decision 30 Oct 2026).** The rule above is unchanged and still holds for the hub **alone**:
the hub cannot make a browser key, enroll one, widen what one may do, or turn browser access on. What the owner accepted
on purpose is one bounded addition: a browser device gets its page from the hub (§6a.7), so a compromised hub, once
the owner has enrolled a browser at the workspace, can act **as that browser** — that is, exactly the fixed browser
allowlist (D96: view, edit and create tickets, and answer an agent question where D98 allows it), as that browser's
person, while remote access is on, every act signed, checked by the workspace and logged "via browser" (D99). It can
never start an agent, approve a plan or a verdict, grant agent access, touch secrets, shares, publish or remote
settings, or open a terminal: those stay with the remote client (§6), the phone app or the workspace itself. Agents,
runs and terminals keep needing keys that only the workspace and a §5 device hold, as the rule says. Answering an agent
question, and writing to a ticket an agent is working on, are the browser acts that steer agent work; they are checked
by the workspace on every act and allowed only while D98 is on, which is off by default for customer workspaces. On a
remote device, answering needs presence (D88). On a browser it needs none. The owner accepts this for own workspaces
only. An answer never satisfies a gate, a grant or a permission request (§6a.4). On a customer workspace browsers are
not allowed at all by default (D98a). The rule's "no more control than the hub has today" therefore reads, for a
workspace with an enrolled browser: no more than today **plus the browser allowlist of D96**, which the owner chose,
can see in the log and can revoke at once.

A design change that breaks this rule is a reopened decision, not an implementation detail.

## 3. Decisions

| # | Name | Decision |
|---|---|---|
| D84 | Opt-in at the workspace | Remote access is **off** for every workspace, also after `hub join`. Only the workspace's owner turns it on, **at the workspace**: a click in Mission Control in a session opened on that computer (loopback) or `orch remote on` at the host terminal, signed by the owner's local key and carrying the owner proof of #1198 C8 (person key `PK`). It is a human-only verb. A remote session, the hub, a hub admin, a channel or an agent can never turn it on, widen it, change its level or end date, or enroll a device. Turning it **off** works from the same places and also from any open remote session of the owner (turning off never needs more than turning on). |
| D85 | Opted out still dials out | Off means: **no remote session can be opened into the workspace.** The link stays up and everything the workspace starts itself keeps working: `log_sync`, handoffs it sends, shares, custodial approvals within its hub mode, publish, the phone. Handoffs it receives arrive as `from-peer` tickets and start on their own only where the workspace's charter has an `auto_start` peer rule, under a Dark-style profile (D13, D28, `orch-v2.md` §9 "Receiving"), unchanged. The host refuses every `stream_open` (§7) with `remote.off` before any byte reaches the dashboard. The hub cannot tell an opted-out workspace to open one. Device enrollment (§5) is the one stream kind allowed while off, and it only adds a device the owner confirms at the workspace. |
| D86 | End to end, the hub forwards ciphertext | A remote session is an end-to-end encrypted, mutually authenticated channel between the person's **remote client** (§6) and the workspace host. The hub and the relay machine forward opaque bytes. The hub never holds a key of the session, never sees the content, and cannot inject or change a byte without the session failing. |
| D87 | Keys the hub cannot make | A session is opened with a **remote device** enrolled directly at the workspace (§5) and verified by the workspace against its own `WSK`-signed device list. Hub users, hub roles, `UK` certificates, channel certificates, hub sessions and switch tickets grant nothing here. A hub role can hide a workspace from a person, never grant access. |
| D88 | Presence for every agent-work action | Opening a session needs a fresh **presence signature** (§5) over a host challenge bound to the session transcript. On a `remote-device` session the same presence signature is needed for every request that starts or changes agent work (the `presence` rule of D90), not only for host 10.5's step-up list. A batch is a fixed list of `view_digest`s confirmed in one prompt; it authorizes nothing else and expires 5 min after the touch. The prompt, drawn by the remote client, names the workspace and the actions. Each terminal open or attach needs its own presence signature, and a remote terminal closes after 10 min idle. |
| D89 | Ends in-process, never on a socket | The host terminates a stream **in-process** at the dashboard app, as an ASGI scope with `orch_listener = "remote-device"` and the session identity (device, person, level, `stream_id`) set from the handshake, never from a header or a token. It never opens a TCP or Unix connection to any listener for a stream. **In-process, precisely** (owner, 30 Oct 2026): the decrypted bytes of a stream may cross only the host's local connection to the dashboard (the user-owned 0600 Unix socket the dashboard already uses to reach the host, through `remote.attach`), never a TCP listener, never any network socket and never a listener of the dashboard. Inside a stream, every `pre_login` route (passkey login and enrollment, `/auth/switch`) and every outside bearer is refused. A token minted inside a stream is bound to its `stream_id` and is refused on every listener and every other stream. The click signer treats every call from a `remote-device` session as `remote: true` and learns that from the stream terminator over its own channel, never from the dashboard (host 10.5 trusts a call without `remote` as local). The link gets no port forward and no operation that runs anything. |
| D90 | Its own route column, refused by default | The route table gets a **`remote-device` column**, separate from the front listener's, **`refused` unless listed**, with a test that walks the app's real routes (like `tests/test_remote_route_gate.py`). Values: `allowed` (reads and plain actions), `presence` (D88), `refused`. The owner picks a level at opt-in: **View** (reads only), **Work** (View plus `allowed` and `presence` routes), **Terminals** (Work plus terminal routes, each with presence). Terminals is a separate, explicit choice, never a default. **Refused at every level**, explicitly: hub connect, mode and leave; passkey enroll and remove; device add and remove; the remote policy; custody on (One click); runner on; grant reveal and deposit; add-on install, stage and settings (`market/*`); claude-trust; share rule add and sync; `shares/register`; records auto-on; guard, permissions and charter; setup. A new route is refused on remote until someone decides otherwise. **Routes that sign any op kind** (`click/prepare`, `click/sign`) are classified by the **signed op kind**, not the route: refused at every level are roster and member changes, `policy.set`/`policy.changed`, `settings.set`, `grant.issued` with reveal or deposit, `device.*`, `keys.*` and every hub op; `presence` are decisions, approvals and `agents/start`; plain `allowed` is only a closed allow list of op kinds; any other op kind is refused. |
| D91 | Time-boxed and visible | Remote access has an end date picked at opt-in: 1, 3 or 7 days; "until I turn it off" only when the premises answer of D93 is **no** (never inferred; not offered for Terminals). Sessions live at most 12 h and close after 2 h idle. Turning off, expiry, a level change, removing a device, **removing a person or taking away their owner role** (their devices are removed with `remote.device_removed` reason `member`, and a device of a person who is not a current owner never passes the gate or a request, even before that line is written) closes every affected stream and revokes its tokens **in the same lock hold** that changes the policy; the level and the device are checked on every request, never cached per stream. The workspace shows every open remote session (person, device, since, level) with **End**, and writes every session open, presence signature and close to its own hash-chained log, which the hub cannot suppress or rewrite. |
| D92 | No downgrade on the link | The link keeps TLS 1.3 with the card's SPKI pin and the `WSK` signature bound to the TLS exporter (protocol §10.1). It may also run as a WebSocket on 443 and through an HTTP CONNECT proxy, **with that TLS end to end between host and hub**: when a WebSocket hop is terminated by a front proxy, the link's own TLS runs inside it. The public passthrough never terminates TLS. A TLS-inspecting proxy makes the pin fail and the link stay down; the host never falls back to an unpinned, unbound or plain connection. The fix is an inspection exemption for the hub's name at the customer. |
| D93 | Customer workspaces join selective | At opt-in the owner answers one question: does the workspace run **on someone else's premises**? Changing the answer is the D84 verb and is logged. If yes, remote access can be turned on only while the workspace is joined selective (D80), and **any** `hub.joined` with mode full (a mode switch, a leave followed by a new full join, a join to another hub) turns remote access off in the same lock hold. When the answer is yes or the join is selective, the host refuses `/auth/switch` (a hub-minted switch ticket would otherwise open a read session over every workspace read, beyond the owner's shares), and in the private-network transport (§4) the front listener is never bound to the VPN interface. For a full-mode workspace, opt-in shows plainly: "The hub can already act for people in this workspace. Remote access does not change that." No custodial type, in any mode, can: create a usable grant, start a run, open a terminal, or touch the remote policy or devices (protocol §8.5 gets this as a normative list, each item with a refusal vector). |
| D94 | Browser device class | Besides the §5 remote device there is a **browser**: a plain web browser with **one non-extractable WebCrypto key** (ECDSA P-256, made with `extractable: false`, kept in IndexedDB of the browser page's origin). It is a device of its own class (ids `rb_` + 32 hex), on its own `WSK`-signed **browser list** at the workspace, never on the device list. It is enrolled **at the workspace** like a device (§6a.2): a one-time code made there, and the owner confirms "Add browser <name>, <six words>?" at the workspace (loopback session or host terminal), never from the hub, a remote session, a browser or the phone. A browser belongs to one person: in v1 the person the workspace's click signer signs for, who must be a current owner (D91, same rule as R3). It opens a session only while remote access is on and not expired (D84, D85, D91) and "Browsers allowed" is on (D98a); browser enrollment, like device enrollment, works while remote is off, but never while "Browsers allowed" is off. A browser never has a View/Work/Terminals level: what it may do is the `remote-browser` column (D96) and nothing else. Its channel is browser-specific (P-256, AES-GCM, §6a.3), because a Noise X25519 static key cannot be a non-extractable key in every browser. |
| D95 | The hub may be public | The hub may be reachable from the internet through the edge (VPN optional): its link port (TLS passthrough by name, D92 unchanged) and the **browser endpoint** (the browser page and a WebSocket per browser session, §6a.6). The console is public only as a separate admin choice, off by default; admin routes are never public (§4). Path: browser → edge → hub → workspace, the hub relaying ciphertext only (D86 holds for browsers). The private-network transport of §4 stays the alternative. A public hub changes nothing a workspace accepts: everything a workspace does is still checked at the workspace (D87, D88, D96, D97); the design already treats the hub as possibly compromised. Whoever terminates the page's TLS (the hub, or an edge that terminates it) counts as "the hub" here (§6a.7). The browser page has its **own origin**, on its own port or its own site (registrable domain), never the console's origin and never a sibling subdomain of the console's name (which would share the site for cookies and same-site checks), so a flaw in the console cannot use a browser key. WebCrypto needs a secure context: a browser device needs the page over HTTPS (or `localhost`). |
| D96 | The `remote-browser` column, a fixed allowlist | The route table gets a third column, **`remote-browser`**, `refused` unless listed, walked by a test over the app's real routes and the op catalog, like D90. It allows four things, plus removing itself (§6a.4 lists the routes and op kinds): **ticket view**, **ticket edit**, **ticket create**, **answer an agent question** (answers, and any write to a ticket an agent is working on, only where D98 is on). Everything else is refused for every browser, whatever the owner wants, among them: starting, stopping, linking or attaching agents and runs; approve, request changes, verdict, close, reopen, move; agent access (`grant`); secrets and grants; shares; publish; members, roles, people; schedules and follow-ups; add-ons; terminals; explorer, code changes and run transcripts; settings and every remote setting (including turning remote off, D99 has the reduce-only paths); every D90 "refused at every level" route. Widening the list is a spec change (a reopened decision), never a setting. A ticket a browser creates or edits never starts an agent by itself: no auto-start, schedule, charter, share tag or peer rule fires on **any** `via: browser` event (a creation, an edit, a label change that would match a share tag rule or a peer rule, an answer), and the "Create and start agent" variant is refused. |
| D97 | Every write signed in the browser, verified at the workspace | Every browser write is one click review of the workspace's click signer (`click/prepare` → `click/sign`), and `click/sign` needs a fresh **browser op signature** by the browser key over `orch/v2/browser-op|` with the workspace, the browser, the session hash, a single-use signer nonce and that one review's `view_digest` (§6a.5). The **click signer** verifies it itself against the `WSK`-signed browser list and the `remote-browser` op-kind table; the dashboard never vouches for it and the hub only relays ciphertext. One signature authorizes one review (no batches). Session-open signatures, op signatures and the §5 presence signatures use different labels, so none stands in for another. |
| D98 | "Browser may write to tickets an agent is working on, and answer its questions" | A per-workspace setting in the remote policy, changed like D84 (owner, at the workspace, owner proof, `remote_v`). Default **on** when the D93 premises answer is **no** (own workspace), **off** when it is **yes** (customer, which D93 makes selective). Changing the premises answer to yes turns it off in the same lock hold; it is never turned on by a premises change. Turning it **off** also works from the phone (reduce-only, D99). With the setting off, `answer` and every browser write to a ticket with an active claim, run or open question are refused (`remote.refused`) and the page does not offer them. |
| D98a | "Browsers allowed" | A per-workspace setting in the remote policy, changed like D84. Default **off** when the D93 premises answer is **yes** (customer, selective), **on** otherwise. Changing the premises answer to yes turns it off in the same lock hold. It is turned on only at the workspace, and turning it on shows: **"While a browser is added here, a hacked hub can read every ticket of this workspace."** With it off, a browser enrollment (`browser_enroll`) and a browser session are refused (`remote.refused`), and turning it off by any path (the switch or premises → yes) removes every browser of the workspace (reason `owner`, or `premises`) and voids every outstanding browser code, in the same lock hold. *Confirmed 30 Oct (manager, owner informed): it narrows the owner's decision for customer workspaces.* |
| D99 | "Via browser" and instant revocation | Every browser session open and close and every browser write is written to the workspace's hash-chained log, and every event a browser write produces carries `via: "browser"` with the `browser_id`; Mission Control, the ticket history and the hub's `log_sync` view say **"via browser <name>"**. A browser is removed **at the workspace** (Mission Control on that computer, `orch remote browser remove`) or **from the phone** (and from an open §5 remote-device session of the owner). Removal closes the browser's streams and revokes its tokens in the same lock hold (D91) and is never undone: a removed `browser_id` or key is refused for ever. **Remove all browsers** is one click at the workspace and on the phone (one lock hold, one line per browser); the runbook for a suspected hub incident is: remove all browsers. Removing or turning off is reduce-only, so the workspace accepts it from the phone through the hub (§6a.8): the one `remote.*` request a hub-delivered channel may make, which can take power away and never add any. |

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
  forwards TLS by name and terminates nothing. Console and admin are on the private network only. **Since D95 the
  hub may instead be public on the edge**: the browser endpoint (§6a.6, own origin) may be public; the console is
  public only as a separate admin choice (off by default); admin routes are never public, whatever is chosen. The
  link port stays TLS passthrough. Path for a browser: browser → edge → hub → stream link → workspace, the session end
  to end between the browser and the workspace (§6a.3).
- **The browser key authenticates at the hub endpoint.** R4's rule stands unchanged: a hub session of scope `remote`
  (the remote client's token) is minted only on the private-network web API, and the browser endpoint never accepts
  it. A browser on the public edge has no hub account and no password. The workspace already pushes its `WSK`-signed
  `remote.browser_list` over the link and the hub keeps the rising `version`; the browser endpoint gates with a
  challenge-response `sig_browser("orch/v2/browser-hub|" ‖ hub_id ‖ origin ‖ challenge 32)`, accepted only for a key
  on a current signed list. Enrollment streams are gated by the code: the workspace announces the code ids it has
  outstanding on the link, the code reaches the page only in the URL fragment, and the 3-per-minute limit stays. No
  password or TOTP on a public page, no account to enumerate or lock out; R4's "no sign-in on the public port" holds
  everywhere. Nothing the workspace accepts changes: the gate only protects the stream budget, and a forged list is
  caught at the workspace (D87).
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
  would pass). A key file is refused. The D64 backend list gets a `remote-presence` kind, which signs only the
`orch/v2/remote-*` domains and is never a D64 signer of person events. Only the remote client binary
  can use the key (keychain access group and entitlement on Apple platforms).

**Enrollment** (an enrollment stream, allowed while remote access is off, D85):

1. In Mission Control opened on that computer, or `orch remote device add` at the terminal, the owner makes a one-time
   code: at least 128 random bits, single use, 15 min, 5 wrong tries burn it. The code carries the fingerprint of the
   workspace's **remote key** `WRK` (an X25519 key of the host, used only as the Noise responder static, separate from
   `WSK`/`WXK`, rotated by the owner; rotation ends every session). Hand the code over out of band. Setup says: never
   send it through a hub channel.
2. The device opens an enrollment stream (`stream_open` with `kind: "enroll"` and `device_id` = the code id; the
   prologue carries the code id in place of a device id). The host refuses it with `remote.no_code` unless that code
   is outstanding, and rate-limits enroll streams (3 per minute per workspace). Inside Noise with the pinned `WRK`,
   the device proves the code with `HMAC(code, transcript_hash | transport_pub | presence_pub)` and a presence
   signature over the same transcript. The host creates no state and shows no prompt until both verify.
3. The host assigns `device_id` and shows **"Add device <name>, fingerprint <words>?"** on the workspace. The owner
   confirms there (loopback or terminal) and sets the level cap and expiry in that confirmation, never from the
   device; the device shows the same fingerprint. Without that confirmation the device is not added.

**The device list** is in the host state, signed with `WSK`, mode 0600, out of every agent's reach once #1198 H1 is
active. Each entry: `device_id`, person, transport and presence public keys, level cap, expiry, added at, confirmed by.
Removing a device ends its sessions at once (D91).

**Replay rule (R2).** The remote policy and the device list are folded from signed lines of the workspace log (the model
is the authority, the file is an export). The owner's lines (`remote.changed`, `remote.device_added`) carry the owner
proof of the backup key, which covers a **remote counter** (`remote_v`) the model keeps: a line applies only when it
names exactly the model's current counter, and every state-changing remote line (an owner's policy or device, a
disable, a removal, a key rotation, a full hub) raises it. A proof also has a request id that can be used once, and a
device id or an enrollment code id that has ever appeared in the log is never accepted again, even after the device
was removed. So an old valid proof cannot be replayed to switch remote access back on or to re-add a removed device.

**Same-UID agents on the host.** Until H1 is active on the workspace host, an agent there could add its own device,
as it can already sign with the One-click key. Turning remote access on then shows "Agents on this computer can act as
you" and is not offered by default (host 10.x, same rule).

## 6. The remote client

A small part of the orch install on the person's device. It serves Mission Control's UI on loopback **from the code
installed on that device**, never from the hub and never from the workspace, and opens sessions (§7) through the hub.
If the hub served the page, a compromised hub could serve a page that drives an open session, and no transport
protection helps against that. The browser-only variant (open question 1, answered 30 Oct) is the browser device of
§6a: it accepts exactly that page risk and is therefore limited to the D96 allowlist.

- The loopback server checks Host and Origin exactly, needs a per-launch token, sends no CORS headers and serves only
  this UI.
- It draws every presence prompt itself, naming the workspace and the action(s); a page cannot draw it.
- It states the workspace API version range it supports; the host refuses a session outside its own range
  (`remote.version`).

The remote client stays the only way to drive agents, runs and terminals from elsewhere. A browser with no install is
the browser device class of §6a, which can do much less (D96).

## 6a. The browser device (D94–D99, owner decision 30 Oct 2026)

### 6a.1 What it is for, and what it is not

A person away from their notebook (any computer, any browser) opens the hub's browser page, picks a workspace, reads
its tickets, writes and edits tickets, and answers an agent that asked a question. Nothing else. Everything that
starts or approves agent work, grants agent access, touches secrets, shares, publish or remote settings stays with the
phone app, the remote client (§6) or the workspace itself.

The browser page is **untrusted** (§6a.7): it comes from the hub. The security of a browser rests on the
workspace-side allowlist (D96) and the per-write signature checked at the workspace (D97), never on the page.

### 6a.2 The key and its enrollment

- The page makes **one key per browser**: `crypto.subtle.generateKey({name: "ECDSA", namedCurve: "P-256"},
  false, ["sign"])`, stored as a `CryptoKey` in IndexedDB of the page's origin (D95). It is never exported; the page
  sends only the public key. The same key is enrolled separately at each workspace the person wants; each workspace
  holds its own browser list. Clearing the browser's site data loses the key (the person enrolls again; the old entry
  expires or is removed).
- **Enrollment code.** At the workspace (Mission Control on that computer, "Add a browser", or `orch remote browser
  add` at the terminal) the owner makes a code with the same rules as a device code (§5 step 1: 128-bit secret,
  single use, 15 min, 5 wrong tries burn it, never through a hub channel) and the fingerprint of the workspace's
  **browser key `WBK`**: a P-256 ECDSA key of the host used only to sign the browser handshake (§6a.3), separate from
  `WSK`, `WXK` and `WRK`, rotated by the owner (rotation ends every browser session). Text form (R8 fixes it):
  `orch-rb1.<workspace_id>.<b64u(code_id 16 ‖ secret 16 ‖ wbk_fp 16)>.<b64u(WBK_pub 65)>`. The person pastes it
  into the browser page (or the page reads it from a link the owner opens on the browser). Such a link carries the
  code **only in the URL fragment** (`#…`), never in the path or query, so it never reaches the hub, the edge or a log
  in a request; the page clears the fragment once it has read it. Each code record carries its **class** (`device` or
  `browser`): a device code is refused on `browser_enroll` and a browser code on `enroll`.
- **No service worker.** The page registers no service worker, and on every load it unregisters any it finds for its
  origin (one that a compromised hub registered earlier would otherwise keep running a page after the hub is fixed).
- **Enroll stream** (`stream_open kind: "browser_enroll"`, code id `rc_…`; allowed while remote is off, D85; same
  3-per-minute limit as device enrollment, shared). The browser checks `WBK_pub` against `wbk_fp`, runs the handshake of
  §6a.3 and sends `{"t": "enroll", "name", "browser_pub": b64u(65), "mac", "sig"}` with
  `mac = HMAC-SHA256(secret, "orch/v2/browser-enroll|" ‖ th ‖ browser_pub)` and `sig` the browser key's open
  signature (§6a.3) with the code id in place of the browser id. The host stores nothing and shows nothing before
  shape, MAC and signature verify.
- **Presence at the workspace.** The host then shows **"Add browser <name> for <person>, <six words>?"** at the
  workspace, with `browser_fp = SHA-256("orch/v2/browser-fp|" ‖ browser_pub)` rendered as six words (protocol §16 row
  68 rule); the page shows the same words. Only the owner's confirmation at the workspace adds the browser: a
  `remote.browser_added` person line with the owner proof (§5 replay rule: `remote_v`, single-use request id, a
  `browser_id` or code id never reused). The owner sets the expiry there (at most the remote end date, D91; at most
  90 days); there is no level to set.
- **What the words protect, and what not.** They stop a third party who got the code. They do not stop a compromised
  hub: its page receives the code and can enroll a key of its own and show that key's words, which the person then
  confirms. **The hub then holds that browser key itself, headless, until the entry expires**: it can use it at any
  time while remote access is on, with no page open and no person present, and it keeps it after the hub has been
  cleaned up, because the key lives wherever the attacker put it. The person's own browser simply does not work and
  enrolls again. The bound is still the allowlist (§6a.7), but for the whole entry lifetime, not one page visit. What
  limits it: **Remove all browsers** at the workspace and on the phone, one click (runbook: hub incident → remove all
  browsers, D99); each browser's **last use and session count** in Mission Control; a **needs-you at the workspace on a
  browser's first session** ("Browser <name> opened its first session"), so a key used from somewhere else shows up;
  the expiry (§12 point c). Setup says so in one plain line: "Only add a browser while you trust your hub."

**The browser list** is a second `WSK`-signed object beside the device list (`remote.browser_list`, R8): `browser_id`
(`rb_` + 32 hex), `name`, `person`, `browser_pub` (P-256, 65 bytes), `expires_ms`, `added_ms`, `confirmed_by`; at most
32 entries; a rising `version` (rollback refused). Folded from the log like the device list (§5 replay rule): the owner's
`remote.browser_added` line adds, the host's `remote.browser_removed` line (reason `owner`, `phone`, `self`,
`expired`, `key_rotated`, `member`) removes. For Mission Control the host keeps, beside the signed list (not in it, so
the `version` does not move), each browser's `last_used_ms` and `sessions` (count), from the logged session opens.

### 6a.3 The channel: relay → hub → workspace, ciphertext only

**Why not the R0/R3 Noise session.** `Noise_IK_25519` needs an X25519 static key on the initiator. The authenticating
key must be a **non-extractable** WebCrypto key, and X25519/Ed25519 reached WebCrypto only recently and not in every
browser and version orch must support; a key made in page JavaScript instead would be extractable by any script on the
page. P-256 ECDSA/ECDH, HKDF and AES-GCM are in every browser's WebCrypto. Noise defines no P-256 DH and WebCrypto has
no ChaCha20-Poly1305. So the browser gets its own small handshake with the **same shape and the same rules** as §7.2:
an ephemeral key exchange, a host challenge, a signed open bound to the transcript, and a finished handshake that is
never a login on its own. Everything after the handshake (app frames, exchanges, limits, timeouts, the per-request
identity stamp, `route_digest`) is the R3 contract unchanged.

**Suite `orch-browser-1`:** ECDH P-256 (ephemeral both sides), ECDSA P-256/SHA-256 (raw `r ‖ s`), HKDF-SHA256,
AES-256-GCM, SHA-256.

```
prologue P = "orch/v2/browser-prologue|" || bv (1 byte, 1) || kind (1 byte: 3 browser, 4 browser_enroll)
             || workspace_id (16) || browser id or code id (16) || stream_id (16) || hub_id (16)
             || wbk_fp (16) || bfp (32: browser_fp for kind 3; code_id || 16 zero bytes for kind 4)
msg1 (browser → host) = eb_pub (65, uncompressed) || nb (32 random)
msg2 (host → browser) = eh_pub (65) || challenge (32) || AEAD_h2b(0, sig_WBK("orch/v2/browser-host|" || th))
th   = SHA-256(P || msg1 || eh_pub || challenge)
keys = HKDF-SHA256(ikm = ECDH(eb, eh), salt = th, info = "orch/v2/browser-keys|") -> k_b2h (32) || k_h2b (32)
AEAD_dir(n, x) = AES-256-GCM(k_dir, iv = 4 zero bytes || n (8, big-endian), aad = stream_id || dir byte, x)
msg3 (browser → host) = AEAD_b2h(0, cj({"t": "open", "api": [min, max], "sig"}))
open = "orch/v2/browser-open|" || workspace_id (16) || browser_id (16) || th (32) || challenge (32)
```

- The browser verifies the host's `WBK` signature against the `WBK_pub` pinned at enrollment before it sends msg3; a
  wrong one closes the stream (`handshake`). The ephemeral keys may be non-extractable too; they live for one stream.
- The host verifies `sig` (the browser key over `open`) against the browser list under the store lock, as §7.2 does
  for presence: browser listed, unexpired, its person a current owner and the attached signer's person, remote on,
  "Browsers allowed" on (D98a); else close `presence` / `remote.device` / `remote.off` / `remote.refused`. The
  challenge is single use, valid 60 s. On a browser's first session ever the host raises a needs-you at the workspace
  (§6a.2) and counts every session open into the browser's entry for Mission Control (last use, session count).
- Every later frame is AEAD with a per-direction counter from 1; a changed, reflected, reordered or replayed frame
  fails and closes the stream (`handshake`), exactly as the Noise transport rule. No rekey; a stream lives at most 12 h.
- `th` plays the role of `hh` everywhere R3 uses it (the signer binds op signatures to it, §6a.5).
- For kind 4 `bfp` repeats the code id; the length stays fixed so the transcript layout is the same for both kinds.
- A replayed msg1 costs only handshake budget (fresh `eh`, fresh challenge); msg1 under another prologue does not
  open. Without `WBK`'s private key nobody can answer msg2, so the hub cannot stand in for the workspace.
- **Strict encodings (R8 vectors for each).** Public keys (`eb_pub`, `eh_pub`, `browser_pub`, `WBK_pub`) are accepted
  only as 65-byte **uncompressed** points (`0x04` prefix), checked to be on P-256, and the point at infinity (identity)
  is refused. The ECDH output used as `ikm` is the 32-byte x-coordinate of the shared point, nothing else. Signatures
  are raw `r ‖ s`, 64 bytes, `0 < r, s < n`; **high-s values are accepted on both sides** (WebCrypto produces them and
  does not normalize), with a vector, so no side refuses a valid browser signature.
- **One version, one responder.** Only `bv = 1` exists; there is no negotiation, and any other `bv` closes the stream
  (`handshake`). The host picks the responder from `kind` alone, and the id prefix must match the kind: `rb_` only
  with `browser`, `rc_` only with `browser_enroll` (and `enroll` for §5 devices), else `stream_open` is refused before
  any handshake. The code id must name a code of class `browser` (§6a.2).
- **Replay stores** (challenges, op nonces, enrollment request ids) are keyed by the challenge or nonce the host
  issued, never by signature bytes (an ECDSA signature is malleable, e.g. `s` → `n − s`).
- msg3 must arrive within **60 s of msg2**; later, the challenge is spent and the stream closes (`handshake`).

**On the link:** `stream_open` gets the kinds `browser` (id `rb_…`) and `browser_enroll` (id `rc_…`), same gate order
as §10.2c (replay, then id prefix matches kind, then "Browsers allowed" on, then the enroll limit, or remote on → browser listed and unexpired → limits: at most 2 open streams
per browser, counted inside the 16 of the workspace, 10 handshakes per browser per minute). `stream_data` and
`stream_close` are unchanged (frame size, byte budget, `seq`). The host terminates a browser stream in-process exactly
as D89 says, with `orch_listener = "remote-browser"` and the identity `{workspace_id, browser_id, person}` stamped by
the host on every request head from its model at that moment.

### 6a.4 The `remote-browser` column (D96)

Rules: `allowed` (a read), `signed` (a click route; the op kinds below decide, each write needs a D97 signature),
`refused`. Anything not listed is refused. The dashboard enforces it in the one place it enforces the device column
(`Security.route_check`, by `orch_listener`); the signer enforces the op-kind table (`click/browser_kinds.py`, R9) in
its stream branch, from the host's stream identity, never from the dashboard.

| Allowlist item | Routes (dashboard templates, `W = /api/w/{ws}`, the stream's own workspace only) | Op kinds / typed actions (signer) |
|---|---|---|
| Session basics (needed by every item) | `GET /api/session`, `GET /api/me`, `GET W/head`, `GET W/identity`; `GET /api/workspaces` answers only the stream's workspace | none |
| **Ticket view** | `GET W/tickets`, `GET W/board`, `GET W/tickets/{key}`, `GET W/tickets/{key}/events`, `GET W/tickets/{key}/artifacts/{name}` (images pasted into the ticket text only); `GET /api/events` only with the browser filter (ticket and needs-you topics of this workspace, R9) | none |
| **Ticket edit** | `POST W/click/prepare`, `W/click/view`, `W/click/state`, `W/click/sign`, `W/click/cancel` (`signed`) | typed `edit_sequence` without `people` and `links`; typed `comment`, `label`; ops `set` (only `title`, `priority`, `size`, `labels`), `section.set` (only the sections `summary`, `context`, `requirements`, `out_of_scope`), `ac.add`, `ac.edit`, `task.add` and `task.edit` (without a `verify` key), `log`, `artifact.add` of a pasted image inside the sequence |
| **Ticket create** | same click routes (`signed`) | typed `new_sequence` without `agent`, `launch` and `people`; ops `new` (types `feature`, `bug`, `chore`, `spike`; not `epic`), then the edit ops above with the same limits |
| **Answer an agent question** (only while D98 is on) | same click routes (`signed`); the open questions come with the ticket reads above | typed `answer` → op `answer`, only to a question that is open on that ticket: pick one of the question's offered options, or a message for a question of the text kind; no free-text "Other" on a choice question (v1) |
| Remove this browser (§12 point d, confirmed 30 Oct) | `POST W/remote/browser/self_remove` (`allowed`, with a browser op signature over its own review) | none; the host writes `remote.browser_removed` reason `self` (reduce-only) |

Self-remove signing: `browser_id` comes from the terminator only; `view_digest` = `SHA-256(cj({"op":"remote.browser_self_remove","browser_id"}))`; nonce flow as §6a.5.

Everything else is `refused`, including every op kind not in the right-hand column and every argument that is sealed,
shown once or deposited (`secret_arg`). A sequence is as strict as its strictest step. Rules the signer checks on the
concrete review, each its own R9 test:

- `section.set` names one of `summary`, `context`, `requirements`, `out_of_scope`; any other section (among them the
  plan, verify and agent-facing sections) → `remote.refused`.
- `task.add` and `task.edit` carry no `verify` key; any `verify` key, also inside a `new_sequence`, →
  `remote.refused` (a verify command is something an agent runs).
- **A browser write to a ticket with an active claim, run or open question steers agent work; it is allowed only
  while D98 is on.** With D98 off, every browser write to such a ticket (edit, comment, label, answer) →
  `remote.refused`; the signer checks claim, run and questions from the model at sign time.
- **An answer never satisfies a gate, a grant or a permission request.** A question that is a gate, an agent-access
  or a permission request is not answerable from a browser (`remote.refused`); only a plain agent question is. The
  answer event carries `via: browser`, which the agent's `wait` output shows, so the agent knows the answer came
  from a browser. The signer decides by the question's `kind`, written by the host when the question was raised, never
  by the agent's text. Only kind `ask` is answerable; an unknown or missing kind → `remote.refused`. No gate, grant,
  permission or charter check ever reads an `answer` event as consent, whatever its `via`.
- **Free-text answers** (lead decision on the reviewer's recommended route, *confirmed 30 Oct*; it narrows
  the owner's "answer an agent question"): in v1 a browser may pick an offered option or answer a text-kind
  question, but may not send a free-text "Other" on a choice question (`remote.refused`). The walk tests fail when a route
or an op kind has no `remote-browser` entry. Ticket reads return what the person may read locally; there is no
browser-specific widening. Every browser session's ticket reads are logged to the workspace's hash chain (per
session: the ticket keys read and the count, written at least on close and every 15 min), so a key held elsewhere
(§6a.2) leaves a trace of what it read.

### 6a.5 Signing a write (D97)

Flow, the R3 428 flow with one signature per write: the page posts `click/prepare` with the typed action; the signer
builds the review (its `view`) and classifies it with the browser table (refused → `remote.refused`); `click/sign`
without a covering signature is answered `428` with `{"code": "remote.browser_sign_required", "view_digest"}`; the page
asks for a nonce for that click's `request_id` (`W/remote/browser/nonce`, `allowed`), signs, posts
`W/remote/browser/sign` (`allowed`), and retries `click/sign`.

```
op = "orch/v2/browser-op|" || workspace_id (16) || browser_id (16) || th (32) || request_id (16)
     || nonce (32) || view_digest (32)
```

- `request_id` is the click review's own id from `click/prepare`; the signer issues the nonce **for that
  `request_id`** and accepts it for no other review.
- `browser_id` and `th` come from the **stream terminator's channel to the signer**, never from the dashboard or the
  page (as D89 says for `remote`); a `browser_id` or `th` in a request body is ignored.

- `view_digest` is the signer's own review digest `SHA-256(cj(view))` (the same as R3 for signed ops). The signature
  covers the concrete review: the ticket, every field, every section text, the question and the answer.
- The signer issues the nonce (32 bytes), accepts it once, at most 5 min old, and deletes it in the same step
  (`replay` otherwise); it verifies the signature against the browser list entry it reads from the host, bound to the
  stream's `th` (so a signature is valid on that stream only, never on another stream, workspace or browser), and
  then checks the op kinds against the browser table and D98. It verifies; the dashboard never vouches.
- A verified signature authorizes exactly one `click/sign` of exactly that digest, on that stream, once.
- **Re-check at append.** Under the store lock, at the moment the events are appended, the signer checks again: the
  browser still listed (not removed, not expired), the browser list version it verified against still current,
  remote on, "Browsers allowed" on and D98 for the rules that need it. A removal between verification and signing
  therefore signs nothing.
- Then the signer signs the person events with the person's key it already holds (as for every click; a workspace
  without that key for the person makes the browser read-only and the page says so). The host writes a
  `remote.browser_op` line `{browser_id, request_id, view_digest, nonce, sig}` before the events, and each event
  carries `via: "browser"` and `browser_id` **inside the signed event body** (D99), so the log proves which browser
  key authorized which review; it does not prove what the person saw (§6a.7).
- **Second look on the phone.** The phone's approve, verdict and agent-start reviews mark every ticket section last
  written via browser ("changed via browser <name>"), so a requirement or acceptance criterion a browser wrote is
  never approved unseen.

Labels: `browser-open`, `browser-op`, `browser-host`, `browser-enroll`, `browser-fp`, `browser-keys`, `browser-hub` and
`browser-prologue` are new and distinct from every `remote-*` label (R8 adds them to the remote label table with
vectors).

### 6a.6 Through the hub (D95)

The hub serves the browser page on its own origin and accepts one WebSocket per browser stream
(`wss://<page origin>/b/<workspace_id>`). It gates a session open with the browser key's challenge-response
`sig_browser("orch/v2/browser-hub|" ‖ hub_id ‖ origin ‖ challenge 32)`, accepted only for a key on the current `WSK`-signed
browser list the workspace pushed over the link (a cheap gate against strangers using up the workspace's stream budget;
the hub keeps the rising `version`, §4); enrollment streams are gated by the code ids the workspace announced on the
link. A `remote`-scope token (R4) or a console session is refused there. The gate grants nothing at the workspace (D87).
The hub opens a stream only for a key on that workspace's list (or an announced code id). The hub draws a `stream_id`, sends
`stream_open` on the workspace's stream link (R4), and copies frames 1:1 between the WebSocket and `stream_data`; it
sees ids, sizes and timing, and the browser or code id in clear, nothing more. Limits as R4 plus: at most 2 browser
streams per browser key per workspace. When remote is off and no code is outstanding there is no stream link, and the
page says "This workspace does not accept browsers right now".

### 6a.7 The page is untrusted (accepted, bounded)

The browser page is served by the hub. A compromised hub (or whoever terminates the page's TLS) can serve a page that
uses the browser key while the page runs (the page registers no service worker and removes any it finds, §6a.2), and
can show the person anything: the signed review is what the workspace checks, not what the person saw. It can also,
at enrollment, enroll a key of its own and then hold it headless until the entry expires, even after the hub is
cleaned up (§6a.2). **This is accepted** (owner decision 30 Oct 2026) and bounded
by what the workspace checks itself, which no page can change: the D96 allowlist, D98 for answers, one signed review
per write, remote on and unexpired, the browser's expiry, the session limits (12 h, 2 h idle), the log "via browser",
and removal at once from the phone or the workspace. In plain words: a hacked hub can at most read the tickets that
person can read in that workspace, write or change tickets, and, where D98 is on, write to tickets agents are working
on and answer agent questions, as that person, and everyone can see afterwards that it was done via browser. The
reads are logged too (§6a.4). On a customer workspace none of this exists unless the owner turned "Browsers allowed"
on there (D98a). The non-extractable key protects against
someone copying the browser's stored data, not against the hub. A person who needs more (agents, approvals, terminals)
or wants no page trust uses the remote client (§6) or the phone app.

### 6a.8 Revocation and the setting from the phone (D98, D99)

- **At the workspace:** Mission Control on that computer lists the browsers (person, name, added, last used, session
  count) with **Remove** and **Remove all browsers**, and the D98 and D98a switches; `orch remote browser
  remove|remove-all|list`. Removal: `remote.browser_removed` reason `owner`,
  streams closed and tokens revoked in the same lock hold.
- **From the phone:** the phone app lists the person's browsers per workspace (from the workspace through the hub) and
  offers **Remove**, **Remove all browsers** and, per workspace, **turn off "browser may write to tickets an agent is
  working on, and answer its questions"**. The list it shows is the workspace's `WSK`-signed `remote.browser_list`:
  the phone verifies the signature against the workspace's pinned `WSK` and that the `version` never goes back, and
  otherwise shows **"list not verified"** (Remove all still offered). The phone signs a
  `remote.browser_revoke {browser_id}`, `remote.browser_revoke_all` or `remote.browser_answers_off` request with its
  channel key and the hub delivers it on the link. The workspace accepts it because it only takes power away (it
  checks the shape, that the signer is the browser's person or an owner, and that it names a browser on its list),
  applies it in one lock hold, and answers with a `WSK`-signed receipt. Said plainly: the "signer is the person"
  check is **not authentication** (the channel key is hub-certified, so a hub can make one); it is acceptable only
  because these requests are reduce-only; the phone shows "Removed" only after that receipt and otherwise "Not
  confirmed yet: remove it at the workspace". The hub also stops relaying that browser at once (defence in depth; a
  hacked hub can ignore that, never undo the workspace's removal). A hub can forge such a request only to remove a
  browser or turn answers off: a denial of service it can already cause by not relaying.
- **From an open §5 remote-device session** of the owner: the same two reduce-only acts, `allowed` in the device column.
- **From the browser itself** (§12 point d, confirmed 30 Oct): a browser may remove **itself** (a signed
  `remote.browser_removed` reason `self`, `allowed` in the `remote-browser` column); it may not turn remote access off.
- Nothing adds a browser, widens D96, or turns D98 or D98a on except the owner at the workspace (D84).

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

The hub sees stream ids, sizes and timing, and the device or enrollment-code id (`stream_open` carries `device_id` or `rc_…` in clear), and nothing more.

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
| Agent on the person's notebook | Use the device to reach C | Presence for session open, every agent-work action and every terminal (D88); the prompt is drawn by the client. Residual: a same-UID agent can read the client's per-launch token, make reads in a session the person opened, run the listed actions of a batch the person confirmed, and type into a terminal the person opened (closed after 10 min idle) |
| Compromised hub, private-network transport | Reach C's front listener and redeem a switch ticket | `/auth/switch` refused when premises = yes or selective; front listener not bound to the VPN interface (D93) |
| Stolen notebook | Same | Presence with user verification; remove the device at the workspace |
| Leaked enrollment code | Enroll an attacker device | Owner confirms the device fingerprint at the workspace (§5) |
| `WRK` leaks | Impersonate devices to the host | Noise alone is not a login; the presence signature is the only authenticator (§5) |
| Agent on the workspace host | Add its own device | #1198 H1; until then not offered by default (§5) |
| Owner turns remote off during a session | The session keeps going | Streams closed and tokens revoked in the same lock hold; level checked per request (D91) |
| Owner is removed or demoted while their device has a session | The session keeps going | The device's streams close in the same lock hold as the member line; owner-ness is checked on every open and request (D91) |
| Anyone | Exhaust the workspace | Stream, handshake and byte limits (§7.1); the customer can always cut the outbound connection |
| Compromised hub (or edge terminating the page's TLS) | Use an enrolled browser's key through a page it serves | **Accepted, bounded** (D94, §6a.7): at most the D96 allowlist as that person (read tickets, write and edit tickets, write to agent-worked tickets and answer questions where D98 is on), each write signed and checked at the workspace, logged "via browser", reads logged, while remote is on and "Browsers allowed" is on (off by default on customer workspaces, D98a); removal from the phone or the workspace ends it at once |
| Compromised hub | Enroll its own key as a browser (swap the key during an enrollment the owner started) | **Not stopped, said plainly:** a hub page can show its own key's words, the person confirms them, and the hub then holds that key headless until the entry expires: usable with no page and no person, and it survives cleaning up the hub (§6a.2). Bound: D96 allowlist, D98, D98a (off by default on customer workspaces), remote on, expiry. Limited by: **Remove all browsers** at the workspace and on the phone (runbook: hub incident → remove all browsers), last use and session count per browser in Mission Control, a needs-you at the workspace on each browser's first session, ticket reads logged (§6a.4), the code only in the URL fragment, no service worker |
| Anyone on the public browser endpoint | Use up stream budgets, guess at workspaces | The gate needs a key on a current signed list, or an announced code id (code only in the URL fragment); 3/min enrollment limit; no password or account on the public edge; admin routes never public |
| Compromised hub | Serve a forged browser list | The gate only protects the budget; the workspace re-checks everything (D87) |
| Compromised hub | Make the page show one thing and sign another | Not stopped at the page: the workspace checks the signed review, and the log proves which browser key authorized which review, not what the person saw; the phone's approve, verdict and agent-start reviews mark sections last written via browser (§6a.5) |
| Compromised hub | Make a browser do more than D96 | `remote-browser` column and op-kind table at the workspace, refused unless listed; no agent start, approval, verdict, grant, secret, share, publish, terminal or remote setting (D96); only four sections, no `verify` key; answers and writes to agent-worked tickets only while D98 is on; an answer never satisfies a gate, grant or permission request; no rule fires on a `via: browser` event (§6a.4) |
| Compromised hub | Replay a browser write or open | Op signature binds `th`, the click `request_id`, a single-use signer nonce issued for it and one `view_digest`; open signature binds `th` and a single-use challenge; prologue binds `stream_id`, `wbk_fp` and the browser's fingerprint; replay stores keyed by challenge/nonce, never signature bytes (§6a.3, §6a.5) |
| Compromised hub | Race a removal | The signer re-checks the browser list, its version, remote, D98a and D98 under the store lock at event append: a removal between verification and signing signs nothing (§6a.5) |
| Compromised hub | Stand in for the workspace towards a browser | `WBK` signature over `th`, pinned at enrollment (§6a.3) |
| Compromised hub | Turn browser access or answers on, add a browser, block a removal | Only the owner at the workspace adds or widens (D84, D98); a removal at the workspace needs no hub; a phone removal shows "Removed" only with the workspace's `WSK` receipt (§6a.8) |
| Compromised hub | Forge a phone removal | Only removes a browser or turns answers off: denial of service it already has (§6a.8). The "signer is the person" check is not authentication; it is acceptable only because these requests are reduce-only |
| Compromised hub | Show the phone a false browser list (hide its own entry) | The phone verifies the `WSK` signature and the rising `version` of `remote.browser_list` and otherwise shows "list not verified"; Remove all works without the list (§6a.8) |
| Console flaw on the hub (XSS) | Use a browser key | The browser page has its own origin on its own port or site, never a sibling subdomain of the console; the console's origin cannot reach the key (D95) |
| A script on the page, or an export | Take the key elsewhere | Non-extractable key: no API exports it (D94). Residual, said plainly: browsers keep it in the profile on disk, so malware that copies the whole profile on that computer may take it along; the expiry, the log and removal bound that |
| Agent on the workspace host | Add its own browser | As for devices: #1198 H1; until then not offered by default (§5) |

**Not covered here, said plainly:** a full-mode hub can steer allowed work through custodial events (D72); an agent
on the notebook can read what an open session shows; a compromised workspace host is the workspace.

## 11. Build order and tests

| Step | What | T0 / adversarial tests |
|---|---|---|
| R0 | Contract: stream ops in the closed list, `remote.*` codes, Noise string, transcript and signing domains, presence messages, device list format, the normative "no custodial type can" list (D93) | vectors replayed by host and hub; each "no custodial type can" item refused |
| R1 | L7 replay store, both directions | a replayed link message (w2h and h2w) on a new connection refused |
| R2 | Host: remote policy (D84, D90, D91, D93), device list, enrollment stream with owner confirmation, `stream_open` gate | `stream_open` while off → `remote.off`, no dashboard call; hub-sent policy or device change refused; enroll without owner confirmation adds nothing; enroll without an outstanding code → `remote.no_code`, no state, no prompt; turning off mid-session closes the stream and its tokens; every full `hub.joined` (mode switch, leave + rejoin, other hub) turns remote off; `/auth/switch` refused when premises = yes or selective |
| R3 | Host: Noise IK, presence verification in the signer, in-process termination with the `remote-device` column | route walk: every route has a `remote-device` entry and the D90 list is refused at every level; op-catalog walk: every op kind through `click/prepare`/`click/sign` is classified and the D90 refused kinds are refused; the signer sees `remote: true` for every stream call; `/auth/switch` and passkey routes refused in a stream; a stream token refused on every listener; handshake from another workspace/device/stream refused; presence from another session refused; key file and UP-only FIDO2 refused |
| R4 | Hub: stream router, limits | flipped byte closes the session; limits enforced |
| R5 | Remote client (macOS SE, FIDO2 UV fallback), loopback hardening | UI only from the local install; wrong Origin/Host/token refused; version range enforced |
| R6 | Link over WebSocket/443 and CONNECT | TLS-inspecting proxy in e2e → link down, no fallback |
| R7 | e2e: A, B on the private network, C selective behind an outbound-only firewall; switch A → C → B, handoff C → A, agent start on C asks for presence, opt out on C while a session is open | session ends on opt-out; handoff still works opted out |
| R8 | Contract for browsers (D94–D99): `stream_open` kinds `browser`/`browser_enroll`, `rb_` ids, the `orch-browser-1` handshake, the eight `browser-*` labels (incl. `browser-hub`, one new vector for the hub gate), browser code text and `browser_fp` words, `remote.browser_list`, `remote.browser_added`/`_removed`/`_op` lines and owner proof op `remote.browser_confirm`, the browser op bytes, `via: "browser"` on events, the D98 and D98a policy fields, the phone's reduce-only requests (revoke, revoke all, answers off) and the `WSK` receipt, the `remote-browser` route and op-kind lists of §6a.4 | vectors replayed by host (Python), hub (Python) and the browser client (JS, `node --test`): handshake, AEAD frames, open, op, enroll MAC, fingerprint words; each refusal vector (wrong label, other `th`, reused nonce, other browser, removed browser, rollback of the list); negative vectors: invalid point (compressed, off-curve, identity), wrong `bv`, kind/id-prefix mismatch, code-class mismatch (device code on `browser_enroll`, browser code on `enroll`); a high-s signature accepted on both sides (host and JS) |
| R9 | Workspace: browser list and enrollment with owner confirmation, the `orch-browser-1` responder with `WBK`, in-process termination as `remote-browser`, the `remote-browser` column in `routegate.py` and `click/browser_kinds.py` in the signer, op-signature verification in the signer, D98 setting with defaults by premises, "via browser" in the log and the UI, removal at the workspace and from the phone (reduce-only, receipt) | route walk and op-catalog walk: every route and op kind has a `remote-browser` entry, everything outside §6a.4 refused; `click/sign` without a browser op signature → 428, with a signature for another digest/stream/nonce → refused; `answer` refused while D98 is off and for a customer workspace by default; premises → yes turns D98 off; `new_sequence` with `agent` or `launch` refused, no auto-start on a browser-created ticket; enrollment without owner confirmation adds nothing; a removed `browser_id` never comes back; removal closes the streams in the same lock hold; a hub-delivered request can only remove or turn off; `/api/events` for a browser carries ticket and needs-you topics only; H1 refusals, one test per rule (`section.set` outside the four sections, a `verify` key in `task.add`/`task.edit` and inside `new_sequence`, a gate/grant/permission question answered, a free-text "Other" on a choice question); a write to a ticket with a live run while D98 is off refused; a removal between verification and signing signs nothing; browser ticket reads logged to the hash chain; Remove all removes every browser in one lock hold and closes their streams; with "Browsers allowed" off (customer default) a browser enrollment is refused; no rule fires on any `via: browser` event (label edit matching a share tag or peer rule included); turning "Browsers allowed" off by the premises → yes path removes every browser (reason `premises`) and voids outstanding browser codes; a self-remove removes only the stream's own browser; an agent asks "may I …?" and the answer moves no gate (unknown or missing question kind → `remote.refused`) |
| R10 | Hub: browser endpoint on its own origin (page + WebSocket ↔ `stream_open`/`stream_data` on the stream link of R4, protocol §16 row 69, PR #1721), gate by browser key (challenge-response on a current signed list; enroll by announced code ids; no password on the public browser origin), per-browser limits, public deployment on the edge (D95), stop-relaying on a phone removal | flipped byte closes the browser session; a page origin other than the configured one refused; the console origin cannot read the browser key (separate origin test); limits enforced; the hub never sees plaintext (frames are opaque in a capture); admin routes not reachable on the public endpoint; a `remote`-scope token or console session refused on the browser endpoint; a key not on the current list refused at the gate, a forged-list case still refused by the workspace, enrollment gated by announced code ids, no password or hub session on the browser origin |
| R11 | Browser client: non-extractable key (`extractable: false`, IndexedDB), handshake and AEAD in WebCrypto, R3 app frames, the enrollment page with the six words, ticket list/view/edit/create and question answering, the 428 → sign → retry flow; phone app: browser list, Remove, answers off | `exportKey` on the browser key fails; vectors replay in the browser (Playwright); no route outside §6a.4 is called by the page; wrong `WBK` signature closes the stream; the page registers no service worker and unregisters one planted before; the enrollment code is read from the URL fragment only (never sent in a request) |
| R12 | e2e: hub public on the edge, workspace A (own, full) and C (customer, selective, outbound only); with "Browsers allowed" off on C, a browser enrollment on C is refused; the owner turns it on at C (the warning is shown); with it on, reads follow the threat row; enroll one browser at both (owner confirms at each workspace); in the browser: read, create and edit a ticket on A and C, answer an agent question on A, answer refused on C (D98 default off); an agent start, approve and a terminal attempted by a crafted page are refused at the workspace; remove the browser from the phone while a session is open | the session closes at once; the log shows every write "via browser"; C's hub sees no ticket content beyond its shares |

Every step goes through the security reviewer before its PR.

## 12. Open questions

1. **Browser-only variant** — **answered 30 Oct 2026 (owner, #1597): D94–D99, §6a.** A browser device with a
   non-extractable WebCrypto key enrolled at the workspace, the page served by the hub (untrusted, accepted), limited
   to the D96 allowlist checked at the workspace. The earlier idea (TLS passthrough to the workspace with its own
   certificate, passkeys and a CAA `accounturi` pin) is not pursued: it needs a domain per workspace, which the
   no-domain rule does not allow.
2. Custodial events for selective customer workspaces: should they also require a presence key enrolled at the
   workspace instead of a hub-certified channel key? That would close the remaining D72 gap. Proposed: yes, as the
   next decision after this one.
3. **Open points for the owner from amendment A1.** The spec is written to the recommendation in each; all are
   **recommendations pending the owner**:
   (a) owners only in v1: a browser belongs to a current owner of the workspace (as R3), not to e.g. a customer's
   team member;
   (b) no run transcripts and no code changes in the browser's ticket view (least privilege for customer code);
   (c) expiry of a browser entry: 30 days default, 90 days max, never past the remote end date (on a customer
   workspace that is effectively at most 7 days, D91);
   (d) a browser may not turn remote access off, but may remove itself (§6a.8);
   (e) WebAuthn user verification as an opt-in hardening **at session open** (not per write). Reason: it stops
   background or headless use of the browser key inside the person's own browser profile; it does not stop the
   enrollment swap (the hub's own key, §6a.2) or a lying page (§6a.7).
4. **Confirmed on 30 Oct (from the design review)** (lead decisions on the reviewer's recommended routes, which
   narrow the owner's 30 Oct decision): H2, no free-text "Other" answer on a choice question from a browser in v1
   (§6a.4); H3, "Browsers allowed" off by default on customer workspaces (D98a).

## 13. Review log

- Round 1 (Opus, 29 Oct): rework. Taken in: own `remote-device` route column refused by default with the explicit
  list (1); in-process termination, no `pre_login` or switch in streams, stream-bound tokens (2); presence for every
  agent-work action and per terminal (3); presence verified by the signer (4); guarantee scoped to selective, D93 a
  condition for customer workspaces, normative "no custodial type can" list (5); two keys per device, UV required (6);
  revoke in the same lock, per-request checks (7); enrollment stream with owner confirmation (8); Noise details (9);
  stream ops in the closed list, L7 both directions (10); loopback hardening (11); TLS end to end through WS/CONNECT
  (12); explicit "own workspace" choice (13); version range (14); DoS stated (15).
- Round 2 (Opus, 29 Oct): approve with changes. Taken in: op-kind classification for `click/prepare`/`click/sign`
  (N1); `/auth/switch` refused for premises/selective, front listener off the VPN interface (N2); every full
  `hub.joined` turns remote off (N3); scope wording for shared items (N4); enrollment needs an outstanding code, no
  state before verification, rate limit, owner sets the cap (N5); D28 cited (N6); signer learns `remote` from the
  terminator; batch = fixed digest list; 10-min terminal idle; one premises question; `remote-presence` domains;
  residual risks for the loopback token and terminals.
- Amendment A1 (owner decision 30 Oct, #1597; spec author Opus): browser device class D94–D99, §6a, R8–R12, threat
  rows, open question 1 answered, open points 3a–3e.
- A1 design review (Opus, 30 Oct): approve with changes. Taken in: H1 only four sections writable, no `verify` key,
  writes to agent-worked tickets need D98 (renamed); H2 answers need no presence on a browser (own workspaces only),
  an answer never satisfies a gate, grant or permission, `via: browser` shown in `wait`, no free-text "Other" (pending
  owner); H3 D98a "Browsers allowed", off by default on customer workspaces (confirmed 30 Oct); H4 the enrollment swap
  stated honestly, Remove all, last use and session count, first-session needs-you, code in the URL fragment, no
  service worker; M1 `wbk_fp` and browser fingerprint in the prologue, strict points, x-coordinate ECDH, one `bv`,
  kind/prefix and code-class checks, replay keyed by nonce, high-s accepted, msg3 within 60 s; M2 `request_id` in the
  op bytes, identity from the terminator, re-check at append, `via` signed, phone reviews mark browser-written
  sections, the log claim narrowed; M3 public endpoint rules: the browser key authenticates at the hub endpoint (challenge-response on a current
  signed list, enrollment gated by announced code ids, no password on the public edge), R4's `remote` token rule unchanged; M4 the phone verifies the signed list, the person check stated as not
  authentication; M5 reads logged, no rule fires on any `via: browser` event; L1 page origin on its own port or site;
  R8–R12 acceptance; open points restated as recommendations pending the owner.
