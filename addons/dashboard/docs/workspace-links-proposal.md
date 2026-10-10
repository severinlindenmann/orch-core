# Proposal: Workspace links (addon `links`)

Status: proposal from the Mission Control mockup (owner feedback 2026-10-10, item B). The mock is the Preview addon
**Workspace links** (`links`, catalog). It builds on orch v2 §9 "Linked workspaces" (D12, D13), §11 "Networks" (D11)
and the relay modules of D9/D54 (directory, bridge, drop, push). Nothing in `plugins/` changes yet.

## 1. What a link is

A **link** is a pinned, mutual trust between two orch workspaces that lets them exchange named kinds of things. The
other side is a **peer**. It can be:

- another workspace on **this machine** (DEMO ↔ INT),
- a workspace of yours on **another machine** (DEMO ↔ CLI on the client VM),
- a workspace of **another person or organisation** (DEMO ↔ Northwind Grid · OPS).

A link is the address-book entry of §9 (`{workspace_id, wsk_pub, owner_person_pub, card, carrier, pinned_at,
allowed_kinds}`) made visible and manageable: who, over which carrier, what may cross in each direction, since when,
until when, and everything that crossed.

## 2. What can cross (scopes)

Each side decides only what it **accepts**. "We may send them X" is their scope, read from the pairing; "they may send
us X" is ours. Nothing crosses without the receiver's scope.

| Scope | What crosses | Lands as |
|---|---|---|
| `handoff` | A ticket handed over (the §9 envelope: title, sections, attachments; depth 1, deadline) and its signed `result` | A **request** (accept → a Backlog ticket marked `from-peer`; deny → the sender's ticket goes back to its human) |
| `question` | A question with options, and the answer back | A **request** answered with one option |
| `status` | Status of tickets this workspace handed over (done, waiting, result) | A log entry; the sender's ticket updates |
| `drop` | Files sealed to this workspace's exchange key (`WXK`) | The Drop inbox (needs the Drop addon) |
| `view` | A read-only view of chosen tickets, served live through the bridge | Later (P8; not in the mock); listed so the scope name is reserved |

Incoming handoffs and questions are **untrusted data** (D13): they never run anything by themselves. The charter's
`auto_start {peer, kinds, profile}` rule may later start an accepted handoff under a Dark-style profile; the mock
does not.

## 3. Carriers (transports)

| Carrier | When | Needs |
|---|---|---|
| Same machine (`spool:`) | Two workspaces on one Mac or server | Nothing: works without any relay |
| Relay (`relay:<url>`) | Another machine or another organisation | The workspace registered with that relay. A company can run an **internal relay** (same software, D5/D11) |

**No direct LAN path (D11).** "Direct" is not offered: `--lan` is retired and loopback stays the only direct binding.
For two machines in one network, run an internal relay. Over the relay, a link uses the directory (the peer's card),
the bridge (`ws→ws` mailboxes keyed `(workspace_id, peer_workspace_id)`, envelopes live until acknowledged), drop (files)
and push (the `peer.ticket` notification). The relay stays an API without pages (D54); every screen is in the
dashboard or the app. When the relay is off, relay links keep working on their own side and their messages wait in the
queue (Settings → Relay); same-machine links are not affected.

## 4. Trust model

- **Pairing is mutual and owner-only.** An owner of each workspace signs it (Touch ID / Secure Enclave). One side
  starts it (carrier, peer, the scopes it accepts, an expiry) and gets a one-time **pairing code** (10 minutes, one
  use, rate-limited); the other owner enters it, or, on the same machine, sees the request in their dashboard. The real
  host draws the code from a CSPRNG (at least 40 bits, e.g. 8 Crockford base32 characters); the mock derives it from a
  counter. Both screens then show a 6-character **comparison code** that **each host works out itself** from both
  pinned workspace keys and the pairing transcript, as in device pairing (D6). It is never taken from what the other
  side sends. The owners compare it (read aloud, chat), then each signs within 10 minutes of the other side joining.
  A mismatch or a late signature cancels.
- **The signature names the terms.** "Codes match: sign" covers, in core's words, the pairing, the comparison code,
  the peer, the carrier, what they may send us, what we may send them and the expiry. The host refuses the signature
  when any of these no longer matches (409 `links.stale`).
- **An incoming pairing request is a decision** for owners only (its own action, so Today never folds it with
  routine requests). It carries **decision terms** (a proposed addition to the addon contract, HANDOVER): core shows
  Peer, Comparison code, Carrier, They may send us, We may send them and Expires after as its own lines in the signing
  covers, posts them with the answer, refuses the answer when they changed and records them in `addon.decided`. The id
  (`pair.<request>.<comparison code>.<carrier>.recv-<kinds>.send-<kinds>.<days>d`) still binds the decision. The owner
  may narrow what the peer asked for and pick the expiry (default 90 days) in a form above the decision, never widen
  it; accept is off while that form has unsaved changes, and every change of terms is logged (`links.terms_set`).
  The host drops kinds it does not know when a request arrives, so only known scopes ever reach the terms. Accepting is refused while a link to that peer exists or, for a
  relay carrier, while the relay is offline.
- **Scopes per link and per direction**; widening a scope is a request the receiving owner signs (its own action).
  Narrowing is not in the mock (open point).
- **Expiry**: 30, 90 or 365 days. An expired link stops; pairing again renews it. The Links tab marks it "expiring"
  7 days ahead. Requests on a revoked or expired link are not offered for a decision.
- **Revoke** (owner) **is signed**: the covers name the link and its peer, and the host refuses a mismatch. It unpins
  the peer at once and tells it with a host-signed revoke envelope; open requests from it close, and handoffs still
  waiting there come back to their people. Cutting trust stays one step.
- **Sending is human-signed**: a person co-signs each handoff. The covers name the peer, the carrier, the ticket and
  exactly what crosses (title, the filled sections, the attachments) and the deadline; the host refuses any
  difference (409 `links.stale`).
- **Visibility**: restricted tickets never cross. They are not offered in the handoff form, and the host refuses
  them (409 `links.restricted`). A log entry or request tied to a local ticket is shown only to people who can see it.
  Peer names and owners with invisible, direction or control characters are refused; peer text is escaped wherever
  it is drawn as markdown.
- **Who decides**: pairing and scope requests: owners. Handoffs and questions: owners and maintainers (core's
  `addon.decide`). Members send handoffs; viewers read the links and the log.

### Where this departs from orch v2 (for the owner to confirm)

- **D12** lets an agent send to an already pinned peer after the first human confirmation. This proposal has a
  person co-sign every handoff in the dashboard; agent sends within a charter stay as D12 says (open point 4).
- **§9 receiving**: a peer ticket lands in the inbox and charter auto-start may start it. Here a person accepts it
  into the Backlog first (a request); auto-start is not mocked.
- **§9 sending**: the sender's ticket moves to `waiting_on_peer` with a deadline that returns it to its human. The mock
  shows the handoff as "waiting" in Requests → Sent but does not change the ticket's status.
- **§9 address book**: peers are added from a directory card or by QR between hosts. The 10-minute pairing code
  with two owner signatures and a comparison code is a proposed addition, modelled on device pairing (D6).

## 5. Audit log

Every link has a log, written by the addon (`links.<verb>`, actor `addon:links`) with the person who signed or
decided: `pairing_started`, `paired`, `pairing_cancelled`, `pairing_denied`, `request_received`, `request_accepted`,
`request_denied`, `answered`, `scope_changed`, `sent`, `received`, `status`, `refused`, `revoked`. Live actions also
write the same record to the workspace log, so Activity shows it under Addons. Core
adds its own `addon.decided` and `addon.action_signed` records, which the addon cannot write or hide. Each entry
names the epoch it was sealed under, so a key rotation (a new epoch) is visible in the log.

## 6. Screens (the mock)

**Workspace links** page with four tabs: **Links** (table: peer, carrier with the relay's state, what each side
accepts, expiry, last activity; select a row for details; Revoke), **Requests** (open requests drawn by core as
decisions; what this workspace sent and its state; hand off a ticket), **Log** (filter by link), **Set up** (carrier,
peer, scopes, expiry → pairing code → simulated peer → comparison code → "Codes match: sign"). Today shows the open
requests as core-signed decisions. Everything is simulated: nothing leaves the machine.

## 7. Open points for the owner

1. Read-only views (`view`) across workspaces: worth it before P8?
2. Should an accepted handoff ever auto-start (charter `auto_start`) in the first release, or always wait for a person?
3. Default expiry: 90 days proposed; same-machine links could be "until revoked".
4. D12: may an agent send to a pinned peer under the charter without a person co-signing each handoff?
5. Expiry: renew in place (one signature on both sides) or always pair again?
6. Narrowing scopes on a live link: immediate on one side, and how is the peer told?
7. What exactly the `status` scope carries (status changes only, or also the `result` summary and Drop references).
