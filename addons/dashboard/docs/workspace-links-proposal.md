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
  use); the other owner enters it, or, on the same machine, sees the request in their dashboard. Both screens then
  show the same 6-character **comparison code** derived from both workspace keys, as in device pairing (D6). The owners
  compare it (read aloud, chat), then each signs. A mismatch cancels.
- **An incoming pairing request is a decision**, shown on Today to owners only and signed by core. Its id carries the
  comparison code, so the signature covers it.
- **Scopes per link and per direction**; widening a scope is a request the receiving owner signs. Narrowing is
  immediate.
- **Expiry**: 30, 90 or 365 days. An expired link stops; pairing again renews it. The Links tab marks it "expiring" 7 days ahead.
- **Revoke** (owner): unpins the peer at once and tells it with a host-signed revoke envelope; open requests from it
  close. It is a core confirm, not a signature: cutting trust must be fast, and nothing new is authorised by it.
- **Sending is human-signed**: a person co-signs each handoff (§9 "human co-signature"). An agent may send to a
  pinned peer only within the charter (D12); the first send to a new peer always needs a person.
- **Visibility**: restricted tickets never cross. The host refuses to send one (409 `links.restricted`), and a
  log entry or request tied to a local ticket is shown only to people who can see that ticket.
- **Who decides**: pairing and scope requests: owners. Handoffs and questions: owners and maintainers (core's
  `addon.decide`). Members send handoffs; viewers read the links and the log.

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
