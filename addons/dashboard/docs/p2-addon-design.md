# P2 dashboard addon: design

Status: **draft for review, revision 2**, 11 Oct 2026. Branch `docs/dashboard-p2-design`. This document turns the
Mission Control mockup (this folder) into the real dashboard addon of phase P2 (orch-v2.md §13), covering issues #304,
#305 and #306. Revision 2 answers the Codex challenge of revision 1 and aligns with stream A's **socket API contract
v1** (provisional; `p2-host-design-v1.md` §0, orch-52, including the manager's H9 ruling D-P2-22). §15 maps every Codex finding to its resolution. What this
addon needs from stream A is collected in §13.

Sources: orch-v2.md (§4, §5.5, §13, §16, §23, D33–D62), orch-v2-core.md (§1–§4, §7), orch-v2-portable-custody.md
(D63–D66), orch-v2-ticket-format.md (§5.2–§5.9, §7, §8, §9, §10.3–§10.4, §10.8), orch-v2-carryover.md (D01–D22),
stream A's host design v1 (contract v1 §0, D-P2-1…22, H0–H10), `HANDOVER.md` (mock backend contract),
`plugins/orch-core/src/orch/ops` (the P1 operations).

## 1. Goal and scope

**Goal.** Daily work runs in the dashboard against a real workspace, so the cutover can happen (D44). The React app
from the mockup stays. The in-process mock is replaced by HTTP to a local dashboard server, which is a person-class
client of the host socket. "Only the transport changes" was wrong: P1 results do not have the mockup's shapes, so the
server has an explicit **adapter layer** (§7, Appendix B), and the signing surface changes from a dialog that signs to
a **hand-off** to a trusted signer (§4).

| Issue | Carry-over | In P2 scope |
|---|---|---|
| #304 | D01 shell, D03 live updates | Shell (dark only, §14 Q1), sidebar, workspace context, browser login bound to the v2 identity the host derives (replaces D02 token auth), live updates |
| #305 | D04–D08 | Today (needs you), board and backlog, ticket page (sections, tasks, evidence, log, changes), ticket actions (approve, request changes, verdict, answer, comment, label, move, close/reopen), new ticket (ticket mode, one signed event) |
| #306 | D20, D22 | Reduced settings (members, gate policy, grant length, grants list/revoke), workspace switcher keyed by UUID |

**Not in P2 for this addon** (hidden on the real transport, still available on the mock): agents page (D09, D11),
terminals (D16), widgets (D17), guide (D19), every addon page and action (own P2 issues; they reuse this server
later), artifacts page, relay and devices (P3), skills, connections and doctor (D55–D57, C25d), mandates (D62),
Factory modes of New ticket, shared saved views, undo-create, workspace rename/archive, the review tour, Demo data,
**multi-event batches** (§4.5), **restricted visibility at creation** (§4.5), grant issue (CLI only), phone signing
(D49, P4), WebAuthn (§4.6), Windows (§14 Q3). Gate policy and grant length wait for F1 events behind the
reserved `policy.set` and `settings.set` (§13 R5).

## 2. Architecture

```
Browser (React app, static build)
   │  HTTP + fetch-streamed SSE, http://127.0.0.1:<random port>, Authorization: Bearer <session>
   ▼
Dashboard server  (orch_dashboard, FastAPI + uvicorn, person's UID, one per OS user, loopback only, holds no key)
   │  contract v1 frames, one connection per workspace socket <run>/<workspace_id>/host.sock, peer class `person`
   ▼
Host process (one per host UID; registry, WSK, Store.append; never prompts, holds no device key)

Trusted signers (outside the browser):  `orch sign CODE` in the person's terminal  |  Touch ID helper (H9)
   │  fetch the prepared request themselves, recompute hashes from `material`, render their own review, sign, human.submit
```

**Process and user model.**
- The dashboard server runs as the **person's UID**. After `hello`, it checks `welcome.peer.class == "person"` and
  a non-null `welcome.peer.person` on every workspace socket and refuses to serve that workspace otherwise. It checks
  the server side as v1 requires (D-P2-6): the server's UID and `welcome.workspace.id` must match the trusted pin in
  its own host registry; `config.json` only names the id. It also checks `welcome.caps` (`ops/1`, `human/1`, `sub/1`,
  `query/1`, `diff/1`) and the per-op versions it uses; a missing one hides that feature with "host too old".
- It **holds no key** and **never handles a factor**: no WSK, no device key, no passphrase, no grant secret (D3, D65).
  It can read and create unsigned sign requests (`human.prepare`); it cannot sign.
- **One server per OS user, serving every workspace** registered with the host. Workspaces are found with
  `host.list` on the host's control socket (`<run>/control/host.sock`, host UID only; the dashboard is the host UID on
  a Mac and on a Linux desktop), then each workspace socket is opened by id and `workspace.show` gives its name and
  prefix. The dashboard never proxies requests for another principal (D-P2-6).
- **Started by `orch dashboard`** in the person's terminal (§3.1). The host does not spawn it (D-P2-7). On a VPS the
  host runs as `orch-host-<ws>`, and the dashboard is desktop only (§13 P2).
- It is packaged as an **addon** (`addons/dashboard/orch-addon.json`, capability `serve_http`). The host cannot tell
  the dashboard from the person's CLI (both are `person` class), so `serve_http` is **not a host-enforced boundary**.
  The server itself refuses to serve a workspace where the `dashboard` addon is disabled. That is a policy switch,
  not security, and the person's signing authority does not depend on it. Format §8.1's "enforced by the P2 host"
  needs a one-line amendment (§14 Q6).

**Who trusts what.** This is the claim the design makes, stated plainly:
- **The dashboard is not a trusted display.** A compromised page or server can show anything. It can show the
  genuine code next to fake prose. So nothing shown in the browser is what the person relies on when signing.
- **What you see is what you sign lives in the signer.** The terminal (`orch sign CODE`) and the Touch ID helper
  (H9) each fetch the prepared request themselves, recompute every bound hash from its `material`, render their own
  review from it and build the signing bytes themselves (C7, D-P2-21). A compromised dashboard can at most ask for a
  signature on something the signer then shows truthfully.
- **A compromised dashboard can read** everything its person can read in each workspace, and can create, cancel and
  observe sign requests. It cannot sign, cannot read keys through the host, and cannot make the host open a path
  (v1 §0.8).
- **Same-UID attackers are out of scope** for the browser layer. A process running as the person can read memory and
  files of the dashboard and, on a Mac without a dedicated host user, the WSK file (D-P2-7 "honest limit"). Before
  #287, agents under the person's UID are such processes. Settings → General shows "agents share your user account"
  while `host.status` reports tier A.

**Why the browser never talks to the socket.** Browsers cannot open Unix sockets. The socket's peer-UID check cannot
tell a tab from any other process. Web origins face DNS rebinding, CSRF and cross-origin reads, which the socket was
not built for. The dashboard server is the one place that does web security (§3), adapts results (§7) and serves the
static app.

## 3. Browser authentication (replaces D02)

Threats: another local user or the agent UID reaching the loopback port; another local web service on a different
port (cookies are not port-isolated, RFC 6265 §8.5); a web page in the same browser (CSRF, DNS rebinding, cross-origin
reads); a leaked URL; a process that already owns a port the browser is pointed at.

**Decision: no cookies.** The session credential is a random bearer token that the app keeps in memory and in
`sessionStorage`, and sends in an `Authorization` header. It is CSRF-proof by construction: a foreign page cannot set
that header without a CORS preflight, and the server answers no CORS. It is not sent to other localhost ports,
because the browser never attaches it on its own. The cost is that script in the page can read it; strict CSP, no
inline script and React's escaping keep that surface small, and an XSS still cannot sign (§2). This deviates from
D-P2-7's cookie bullet and from portable-custody's cookie wording; both should be amended (§13 R10, §14 Q6).

### 3.1 Server start and origin ownership

1. `orch dashboard` takes an exclusive `flock` on `<state>/dashboard/lock` (directory opened with `O_NOFOLLOW`,
   owned by the person, mode 0700). If a server is running, it talks to it; otherwise it starts one and waits for it.
2. The server binds **one listener, `127.0.0.1:0`** (a random free port; no `::1`, no `localhost` name, so there is
   no IPv4/IPv6 split where another process answers one address). It never binds a non-loopback address.
3. The server opens its control socket `<state>/dashboard/ctl.sock` (umask 0o177, mode 0600). A stale socket is
   removed only if it is a socket owned by the person, nothing answers a connect, and the lock is held.
4. **Both sides check each other** on the control socket with `getpeereid`/`SO_PEERCRED`: the server serves only its
   own UID, and the launcher talks only to a server with its own UID.
5. The launcher asks for a launch. The server answers `{instance, port, probe_key, code}`: a random instance id, the
   **port it actually bound**, a per-instance probe key, and a launch code (below).
6. **The server proves it owns the port.** The launcher sends `GET http://127.0.0.1:<port>/auth/probe?n=<nonce>` and
   checks the answer equals `HMAC(probe_key, nonce ‖ instance)`. Only then does it open the browser. A process
   squatting on a reused port fails the probe and the launcher refuses with a clear error.
7. `orch dashboard --stop` stops the server through the control socket, never by PID.

Random ports mean a copied URL works only while the same server runs. Paths stay readable and stable, so
`orch dashboard open <url-or-path>` reopens any link on the current port with a fresh launch code.

### 3.2 Bootstrap: one-time code in the fragment

- **M0 review ruling (2026-10-11):** the launcher opens a 0600 HTML redirect file in the
  private 0700 dashboard state directory, never a code-bearing URL in process arguments.
  The file uses meta refresh and `location.replace`, and is deleted after exchange or 60 s.
  Codes expire after **30 s** (superseding the original 60 s below); only failed exchanges
  consume the per-minute failure budget. Dev stub state must be explicitly configured,
  independently `.orch-dev` marked, and outside default host state and socket locations.
- The launch code is 256 bits, single use, valid 60 s, held only in memory, bound to the server instance, and
  consumed atomically. It is never printed or logged.
- The launcher opens `http://127.0.0.1:<port>/#launch=<code>&to=<path>`. The fragment never reaches the server,
  logs or `Referer`.
- The first module of the app (an external file; there is no inline script) copies the code into a local variable and
  **immediately** calls `history.replaceState` to strip the fragment, before any network request. It then sends
  `POST /auth/session {code}` and receives `{token, expires_at}`.
- The token is 256 bits. Sessions live in server memory only: 12 h idle, 24 h absolute. A server restart ends every
  session, and the app shows "Run `orch dashboard` in your terminal".
- **New tabs.** `sessionStorage` is per tab. A new tab without a token asks over `BroadcastChannel('orch-session')`,
  which is same-origin, so it is port-isolated; an open tab of the same origin answers. With no open tab, the page
  asks the person to run `orch dashboard` (or `orch dashboard open <path>`).
- **Logout** (`POST /auth/logout`) revokes the session on the server, closes its streams, cancels its pending sign
  requests (§4.3), clears `sessionStorage` and broadcasts to the other tabs.
- Limitation, documented: browser extensions and crash restore can see the URL before it is scrubbed, and
  `sessionStorage` can be restored with a tab. An expired or revoked token is useless.

### 3.3 Every request

| Route | Auth | Extra checks |
|---|---|---|
| `GET /`, SPA paths, `/assets/*` | public (no data; the bundle is the same for everyone) | Host check; `index.html` `no-store`, hashed assets immutable |
| `GET /auth/probe` | public | Host check; answers only the HMAC |
| `POST /auth/session` | launch code in the body | exact `Origin`, `Content-Type: application/json`, body ≤ 1 KiB, 10 attempts per minute, atomic consume |
| `POST /auth/logout` | bearer token | exact `Origin` |
| `GET /api/**` (reads, stream) | bearer token | Host check |
| `POST`/`PUT`/`PATCH`/`DELETE /api/**` | bearer token | exact `Origin`; `Sec-Fetch-Site` must be `same-origin` when present (defence in depth, not authentication) |

- **Host check, before routing**: the `Host` header must be exactly `127.0.0.1:<port>`, otherwise 421. Duplicate or
  malformed `Host` is rejected. `localhost:<port>` is rejected too (one canonical origin). This is the DNS-rebinding
  defence.
- `Origin: null` is rejected. `Forwarded` and `X-Forwarded-*` are ignored (uvicorn `proxy_headers=False`,
  no trusted proxies). The Vite dev proxy is a dev-only setting and never ships in the server.
- Missing or bad token: 401 `session.required`, with no detail.
- Response headers: CSP `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self';
  img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` (no frame or widget allowances
  until those features ship), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` on `/api` and `/auth`.
- Limits: request body ≤ 1 MiB (member certificate uploads ≤ 64 KiB), 16 open streams per session, 8 pending sign
  requests per session. Validation errors never echo request bodies; the log redacts `Authorization` and launch
  codes.

The browser transport: `createFetchTransport('')` (same origin) with the `Authorization` header. `EventSource` cannot
send headers, so live updates use `fetch` with a streamed body and a small SSE parser (`src/api/sse.ts`).

## 4. Human-signed actions

Every person event is signed (format §5.2, §12 N1): approve, request changes, verdict, answer, close, reopen, and also
comment, label, move, new ticket, settings, member changes and grant revoke. **The browser never handles a factor**
(D65): there is no passphrase field in the dashboard, ever.

### 4.1 The flow

1. The person clicks an action. The dashboard server calls **`human.prepare {op, args, signer:{device, auth},
   handoff: true}`** with `opts.idem` (one id per click, reused on retry). The host re-reads the content, observes git,
   puts the gate hash, `gate_gen`, `source_sha` or question hash into the unsigned event (so the mockup's `expect` is
   not needed), judges it with `model.preview`, and returns `{request:{id, code, expires_at}, workspace_id, log,
   event, material, review_digest}`. `signer` names the device that will sign: the person's `passphrase` device on
   this machine for the terminal, the `secure-enclave` device for Touch ID (§13 R7).
2. The dashboard shows the **hand-off panel** (it replaces the mockup's SignDialog):
   - "Sign in your terminal: `orch sign 7F3K-Q2MA`" with a copy button, and "Sign with Touch ID" once H9 is there;
   - an **informational preview rendered from `material`**, never from the dashboard's cached ticket view, through
     `visible.tsx` (every value as data), under the heading "Preview. Your terminal shows what you sign.";
   - the request fingerprint (`Prepared.fingerprint`, shown as the host returns it: the first 12 hex of sha256(cj({review_digest, events without sig, actor.device, auth})), which `orch sign` also prints, §13 R8; the dashboard verifies it with exactly this formula and fails closed on a mismatch) and, where a gate
     is involved, the gate hash from `event`, labelled "content hash".
3. **The person signs in a trusted place.** `orch sign CODE` finds the request with `human.pending`, recomputes the
   hashes from `material`, renders its own review on `/dev/tty`, asks for the confirmation word and the passphrase,
   builds the signing bytes, signs and calls `human.submit` (H5a, H5b).
4. **The dashboard learns the outcome** by streaming and polling: an item for that ticket on the subscription
   triggers a check, and while the panel is open the server polls `human.pending` every 2 s. When the request leaves
   the pending list, the server refetches the ticket and shows "Signed" if the event is there. The dashboard never
   calls `human.submit`.

### 4.2 Touch ID helper (H9, in P2 before the cutover)

The terminal is the guaranteed path and works today with the `passphrase` backend on every OS. On macOS the Touch ID
signer H9 (`orch-signer`, D-P2-22) lands before the cutover: the dashboard server starts it with **only the request
code**. The helper fetches the request itself, recomputes the hashes from `material`, renders its own review in its
**own native window** (not the dashboard's rendering), builds the signing bytes, shows the Touch ID prompt with a reason
built from those bytes, signs with the Secure Enclave key (`auth: secure-enclave`) and submits. It is the terminal's
contract with a different display and factor.

### 4.3 Request lifecycle, replay and ambiguous completion

M5 final intent contract (owner decision 13 Oct ~17:20, refined by orch-d6; coordination.md 'M5 'same intent'' and 'M5 intent REFINED'):
**one intent (op + content + gate/question version) → at most one request → at most one signature;
a deliberate repeat only via 'Do it again'.** Content means the exact canonical host arguments and certificate bytes.
Gate decisions use gate generation/hash; answers use question id/hash, never its answered flag or the whole ticket.
All other ticket operations (log, set, section.set, close, reopen, move) and workspace operations
(settings, policy, member, grant) have no base. Unrelated agent edits cannot produce a second request.
The host retains the intent outcome for 24 h. Both panels show server-sourced signed content and signing time
before 'Do it again' advances retry. Neither dashboard link nor signer sends `auth.session`: restarting the
dashboard reconnects with the same binding and returns the same request, not `idem.conflict`.
A conflict is checked against `human.pending`: prepared/bound requests of the same op+key show their expiry;
otherwise `intent.blocked` offers explicit 'Start again', warning that an earlier request may have been signed.
No automatic retry advances the intent. Hand-off expiry is decided by host state, never the browser clock.

**Host API gap:** current `human.get`/`human.pending` expose creation/expiry but not signing time.
The server returns `signed_at: null` and the panels say signing time is unavailable; they never call creation
time signing time. A host `signed_at` field is required for accurate 'N min ago'. The submitted click prepare
loses its request id in the signer error, so the server replays the identical host prepare binding/idem through
its own link to retrieve the exact existing record, not an arbitrary op+key match. Its warning summary comes
from that record's material review. `human.request_state` at sign reads pending/get before offering a repeat.


The host owns the request (v1 §0.6): bound to `(workspace, mapped person, signer.device, op, review_digest)`, valid
10 minutes, submit and cancel serialized, a tombstone with the result after completion, at most 20 pending per
person. The dashboard adds:

- **One action intent -> at most one request -> at most one signature, held by the host.** `human.prepare` honours
  `opts.idem` (host contract 0.7, stream A #402): the same principal, idem and arguments return the SAME request (same id
  and code) in its current state while `prepared` or `bound`, and its terminal state for 24 h after it was prepared; other
  arguments are `idem.conflict`; afterwards `idem.expired`. The dashboard therefore derives the idem from the *action*,
  never from a click, tab or session: `a_` + 128 bits of `sha256(cj({person, workspace_id, op, args, base}))` in the
  host's shape (`a_[0-7]` + 25 Crockford characters; `orch_dashboard/intent.py`). `op` and `args` are exactly what is
  sent to the host (the typed models' `host_call`, never the browser's body); `person` is the workspace's own person;
  `base` is gate generation/hash for gate decisions, question id/hash for answers, and null for every other op. Two tabs, two sessions, a reopened dialog after a lost answer, and logout/login then retry all send the same
  idem with the same arguments and get the same request. The browser sends no key; it sends only `retry` (0 normally),
  which only an explicit click advances. A request obtained this way belongs to the PERSON in that workspace: any session
  of that person may view, poll, sign and cancel it; an id nobody here prepared looks like one that never existed. The
  per-session quota counts a shared request once. This process remembers nothing the invariant depends on.
  What the person sees: a request that is `submitted` is **"Already done"** with an explicit **"Do it again"** (retry + 1,
  so a second identical comment is possible, but only by that click); a `cancelled` or `expired` one offers **"Start
  again"** (also retry + 1); a lost answer is retried with the same `retry` and returns the same request. `idem.conflict`
  means a different path or signer binding. `human.pending` determines whether an op+key request is open:
  if so, show its expiry; otherwise offer explicit "Start again" with a prior-signature warning.
  Neither dashboard nor signer sends `auth.session`, so a restart preserves the host binding.
  **One signing path per workspace: withdrawn (owner, 24 Oct 2026, #1170).** Where click-to-sign is ON the dashboard signs
  by click; the Terminal hand-off stays available for terminal-only ops and as a backup. The intent
  function is the same on both workspace types (no path offset); `retry` advances only on "Do it again" / "Start again".
  Flag flip: before a click prepare, `human.pending` (no arguments, our link) is read for an open (prepared or bound)
  hand-off request that has a code, with the same op and key; if one exists the prepare is refused ("An attempt from the
  terminal is still open; it expires at ..."), as defence in depth for the host's own refusal to flip the flag with hand-offs
  pending. Flag flipped OFF: a click request can no longer be signed (the signer refuses, reason `disabled`); the server
  cancels it and it reads as ended. On
  `idem.expired` (the host forgot the idem after 24 h) the server returns `intent.expired` (HTTP 409) without another
  prepare. Only the person clicking "Start again" advances `retry` and permits a fresh request.
  There is no `outcome.get` for a prepare; a request is read with `human.pending {request_id}`. "Complete details" after a `new` targets only the ticket of the `new` request this dialog
  prepared and watched. Requests expire after 10 minutes (4 for a grant); more than 20 pending per person is `quota.host`.
- **A refused submit stays open.** A refusal before any append (`gate.stale`, `members.stale`, `question.stale`,
  `sig.invalid`, `human.request_mismatch`) leaves the request `bound` until it expires, and `human.pending` shows
  `error:{code}`; the panel says "Refused: <text>. The request stays open until <time>". A failure after an event
  landed ends it `submitted` with a failed outcome.
- **Cancel.** Closing the panel, logout, session expiry, a ticket that goes `gone` (§6), or switching workspace while
  the panel is open calls `human.cancel`. An in-flight socket request is stopped with a `cancel` frame.
- **Ambiguous completion.** On return after a drop, the server checks `human.pending` and the ticket. Event present:
  "Already signed". Request still pending: the panel reopens on it. Neither: "Expired or cancelled. Prepare again".
  The server **never re-prepares or retries automatically**. A per-request state lookup would make this exact (§13 R6).
- **Already done.** A prepare refused with `gate.already_approved`, `transition.refused` or a question that is no
  longer open shows "Already done" and refetches.
- **Stale.** If the state changed between prepare and submit, the signer gets `gate.stale`, `members.stale` or
  `question.stale`; the panel shows "Changed since you started. Prepare again" after a refetch.

### 4.4 Inputs are typed, never CLI forwarding

The server has a **closed allowlist** of (route → op), each with a typed request model (`extra = "forbid"`). It
never passes a filesystem path, a stdin selector, an environment value, a raw event or an op name chosen by the
browser. Bytes (a member's device certificate) are uploaded with `blob.put` and referenced through `files`
(v1 §0.8). The host never opens a path a client names and refuses path-like arguments with `blob.invalid`, which
closes the confused-deputy read in prepare reviews.

### 4.5 No batches in P2

There is no multi-event signing in P2.
- **New ticket** is one signed `new` event (person version through `human.prepare`): title, type, priority, size,
  labels, parent and the description (`message`). Further sections and acceptance criteria are edited after creation
  with the person versions of `section.set` and `ac.add` (later issues).
- **Restricted visibility at creation is hidden** until stream A provides a creation that is restricted from its
  first durable event (§13 R4). Create-then-restrict as two appends would publish the title in between.
- **Settings** save one change per signature (a gate policy change will be one `policy.set` event with the whole
  map, once F1 defines it).

### 4.6 Platforms and backends

| Platform | P2 signing route | Before cutover | Notes |
|---|---|---|---|
| macOS | terminal hand-off (`passphrase`) | Touch ID helper (H9, `secure-enclave`; in P2, D-P2-22) | — |
| Linux desktop | terminal hand-off (`passphrase`) | — | — |
| Windows | not supported in P2 | — | the host has no named-pipe transport in P2 (stream A §9 Q2); owner decision §14 Q3 |
| WebAuthn | not in P2 | — | needs a new signed-event contract (format §5.3) and assertion verification (origin with port, RP ID, challenge); D64's "webauthn (dashboard, P2)" needs amending (§14 Q4) |

## 5. Identity and workspaces

- **Who the person is** comes from the host: the person mapped to the dashboard's UID in the host registry
  (`welcome.peer.person`, `whoami`; v1 §0.4), never a key file and never the browser. All reads, `needs`, counts,
  errors and subscriptions run as that principal. The mock's `setViewer` does not exist on the real transport. An
  unmapped UID is refused by the host; the dashboard shows "this account is not a person of that workspace".
- **Every API call, cache key, stream and sign request is keyed by workspace UUID.** API paths are
  `/api/w/<workspace_uuid>/…`. Query keys become `['ticket', wsUuid, key]`, `['tickets', wsUuid, params]`, and so on.
  Pending sign requests carry the workspace UUID and log UID.
- **Routes keep the readable prefix.** `/ticket/DEMO-0043` resolves the prefix to a UUID through the workspace list
  (`host.list` + `workspace.show`). If two workspaces share a prefix, the app adds `?ws=<first 8 hex of the UUID>` to
  every link it generates, and a link without it asks which workspace. `workspaceOfTicket` returns a UUID, never a
  prefix.
- **Switching** cancels the open hand-off panel of the old workspace and keeps both workspaces' caches apart by key.
  An unavailable host shows that workspace as "host not running" in the switcher; the others keep working.

## 6. Live updates (D03)

Built on contract v1 `sub.open` / `sub.close` (§0.7): metadata-only items, opaque scoped cursors, revalidation of
membership and visibility before every delivery, `gone` items, and invalidation by snapshot plus cursor.

- **Upstream, per workspace.** The server takes a snapshot (`ticket.query`, `needs`; each returns `sub_cursor`) and
  opens `sub.open {topics:["workspace","tickets","needs"], after: sub_cursor}`. All tabs of this server share one
  principal per workspace (one UID, one mapped person), so one subscription is enough. Items are deduplicated by
  `jpos`.
- **Upstream invalidation.** `{"invalidated":"overflow"}`, `sub.reset`, `host.restarting` or a lost connection → the
  server takes a new snapshot, reopens with its `sub_cursor`, and sends `resync` to every tab of that workspace.
- **To the browser.** `GET /api/w/<uuid>/stream`, fetch-streamed SSE with the bearer token. The server stamps every
  forwarded batch with an id `<instance>:<n>`. The first message is `ready {id}`.
- **Subscribe before snapshot.** A tab opens the stream and waits for `ready` before its first data fetch, so no
  item falls between fetch and subscribe.
- **Per-tab resume, simple version.** Each tab keeps its own `Last-Event-ID` and sends it on reconnect. If it equals
  the server's latest id, nothing happened. Otherwise the server sends `resync`, and the tab refetches every query of
  that workspace. There is **no replay buffer in P2**; a bounded per-workspace ring buffer is a later optimisation.
- **Slow tabs.** 256 queued messages per tab; past that the server closes the stream and the tab reconnects (and
  resyncs). A heartbeat every 15 s; reconnect backoff 1, 2, 5, 10 s; after three failures the app polls
  `GET /api/w/<uuid>/head` every 10 s.
- **Authorization over the stream's lifetime.**
  - A stream belongs to a session. Logout or expiry closes it.
  - The host revalidates membership and visibility before every delivery, queued items included.
  - **A ticket that becomes invisible** arrives as `{jpos, uid, gone:true}`. The server forwards `gone {uid}`; the tab
    **purges** every cached query of that ticket (`removeQueries`), its page shows "no longer visible", and its
    pending request is cancelled. A ticket that becomes visible arrives as `visibility.changed` and is fetched.
  - Membership: on `member.*`, `role.changed` or `device.*` the server re-runs `whoami` and `member.list`. If the
    person is no longer a member, it sends `revoked` and closes; the tab purges every query of that workspace and
    closes the hand-off panel.
  - Accepted leakage: the timing and count of items the person may see; nothing about hidden tickets.
- **Invalidation** (coarse on purpose):

| Item | Invalidate |
|---|---|
| any item of a ticket log (`key`) | `['ticket', ws, key]`, `['tickets', ws]`, `['board', ws]` |
| `needs.changed` | `['today', ws]`, `['workspaces']` (needs-you count) |
| `gone` | remove every query of that ticket; invalidate `['tickets', ws]`, `['board', ws]`, `['today', ws]` |
| `member.*`, `role.changed`, `device.*` | every key of `ws` plus `['me']` (after the check above) |
| any other workspace-log item | every key of `ws` |
| `resync` | every key of `ws` |

The board's optimistic move is removed: a move is a signed hand-off, so the card moves when the event arrives.

## 7. API mapping

The dashboard server maps one route to one contract v1 op (or a fixed short list), through an adapter that turns the
op's `result.data` into the UI's types (Appendix B). Adapters are typed, **never parse `lines`**, never synthesize
hashes or state from mock conventions, and show "not available" when a field is missing. "Prepare" means
`human.prepare {op, args, signer, handoff:true}` (§4). Every socket write carries `opts.idem`.

| Client method (`client.ts`) | Contract v1 op | Notes |
|---|---|---|
| `getMe` | `whoami` per workspace + `member.list` | person, name, role and tier per workspace |
| `getWorkspaces` | control `host.list` + `workspace.show` + `needs` (count) | uuid, prefix, name, role, needs-you count, host state |
| `getIdentity` | `workspace.show` + `host.status` | uuid, prefix, genesis, epoch, WSK fingerprint, tier |
| `getCursor` | — | replaced by the stream's `ready` id; fallback `GET …/head` |
| `getAddons`, `getWorkspaceAddons` | `addon.list` | shell nav only; addon pages hidden |
| `getToday` | `needs` | computed by core from `model.needs` (addons only declare rules); kinds `question`, `approve`, `no_eligible`, `acknowledge`, `addon`; live through the `needs` topic |
| `listTickets` (board, backlog, tickets) | `ticket.query` | summary rows (type, status, priority, size, labels, assignees, owner, parent, epic, claim, gate states, needs, `head_seq`), filters, sort, `limit ≤ 200`, `next_page` paging, `sub_cursor` |
| `listPeople` | `member.list` | members, roles, devices (no keys) |
| `getTicket` | `show {ref, full:true}` + the ticket's `ticket.query` row | the F1 §7 document plus gate states from the row; question states and evidence digests per R1 |
| evidence content | `artifact.get {ref, name, offset}` | chunked (≤ 768 KiB); shown as data |
| `getEvents` | `show {ref, log:true, since}` | P1 returns formatted strings; structured events per R1. Until then the log tab shows the fenced lines as data |
| `getChanges` | `diff {ref, repo?}` | bound to D58 `source_sha`; returns `sources[{repo, branch, base_sha, head_sha}]`, files, `truncated`; cache key includes the returned head OIDs; the UI shows them and does not claim the signature binds the diff |
| `postAction` `answer` | prepare `answer {ref, question, option?, message?}` | |
| … `approve` | prepare `approve {ref, gate}` | host binds gate hash or `source_sha` |
| … `request_changes` | prepare `request_changes {ref, gate, message}` | |
| … `verdict` | prepare `verdict {ref, outcome, message?}` | |
| … `comment` | prepare `log {ref, message}` (person version) | `comment` stays a reserved name, not served |
| … `ask` | prepare `ask` (person version) | |
| … `add_label` | prepare `set {ref, pairs:["labels=…"]}` (person version) | |
| … `set_status` | prepare `move {ref, to: backlog \| open, reason?}` (provisional, R3), `close`, `reopen` | `move` allowed from backlog, open, and in progress without a live claim; the board limits drop targets accordingly |
| … `claim`, `release` | not offered | agent ops |
| `createTicket` | prepare `new {title, type, priority, size, label, parent, message}` (person version) | no `file`, no batch, no restricted creation (§4.5) |
| `postSettings` `member.add` | `blob.put` (certificate JSON) → prepare `member.add {name, role, pk}` with `files.cert` (R9) | **UI change**: the invitee's key and certificate, not `{person, name, role}`; the owner compares the person id out of band |
| … `member.role`, `member.remove` | prepare `member.role`, `member.remove` | `members.last_owner` → refusal |
| … `gate.policy` | prepare `policy.set {gates}` | reserved, not served until F1 defines the event (R5); the form is read-only until then |
| … `grant.hours` | prepare `settings.set {grant_hours}` | as above (R5) |
| `listGrants`, `revokeGrant` | `grant.list` (R2); prepare `grant.revoke` | |
| `issueGrant` | hidden | the client draws the secret and writes it to `/dev/tty` (D-P2-16) |
| `listViews`, `saveView`, `deleteView` | dashboard addon storage `<state>/dashboard/views.json` (0600), keyed by workspace UUID | personal views only; v1 keeps views out of core |
| everything listed as out of scope in §1 | hidden | the server answers 404 |
| `simulatePush`, `simulateRelay`, `setViewer`, `resetDemo`, `getDataset`, mandates preview | mock only | absent from the real server |

**Feature gating.** `GET /api/session` returns `{features: [...]}`, derived from what the server serves and from each
host's `welcome.caps`/`ops`; the shell hides what is not listed. The mock transport lists everything, so the mockup and
its tests stay as they are.

## 8. Errors and limits

The host returns contract v1's `res err`: `exit` plus the F1 §10.4 error body `{code, message, hint?, fix?,
retryable, retry_after_ms?}`. The server passes `code`, `message`, `hint`, `retryable` and `exit` through unchanged as
`ApiError`, drops `fix.argv` except on the "do it in a terminal" screens, and picks the HTTP status from the code. A
`fatal` frame closes that workspace connection; the server reconnects and resyncs.

| Code | HTTP | UI |
|---|---|---|
| `not_found` (also for tickets the person cannot see) | 404 | missing-ticket state; the mock's `not_visible` becomes `not_found` |
| `usage`, `invalid.input`, `parse.*`, `blob.invalid`, `frame.invalid`, `frame.too_large` | 400 | inline form error with `hint` |
| `human_only`, `role.denied`, `op.not_allowed`, `peer.denied`, `grant.*`, `host.required` | 403 | refusal; hiding a button is not authorization |
| `gate.already_approved`, `transition.refused` | 409 | "Already done", refetch (§4.3) |
| `gate.stale`, `members.stale`, `question.stale`, `artifact.mismatch`, `observe.unavailable`, `source.missing` and the other §10.4a model refusals | 409 | "Changed since you started", refetch, "Prepare again" |
| `human.request_expired` | 410 | "Expired", "Prepare again" |
| `human.request_mismatch` | 409 | shown as an internal error; never retried |
| `idem.conflict` (a prepare) | 409 | "Could not prepare"; the next click draws a new idem |
| `intent.expired` (host `idem.expired` on prepare) | 409 | "This attempt is too old"; "Start again" on the click, with a new idem |
| `outcome.unknown`, `cancelled` (a prepare) | 503 | "Unknown outcome"; "Try again" retries with the SAME idem and adopts the request returned |
| `op.unsupported`, `host.version_mismatch` | 501 | "host too old for this feature", feature hidden |
| `host.unavailable`, `host.restarting`, `host.busy` | 503 | "host not running" or "restarting" banner; reads retry with backoff |
| `rate.limited`, `lock.busy` | 503 | reads retry after `retry_after_ms` |
| `sub.reset`, `invalidated: overflow` | — | snapshot, reopen, `resync` (§6) |
| `internal`, `not_implemented` | 500 | error boundary |
| dashboard's own: `session.required` 401, `origin.bad` 403, `host.bad` 421, `addon.disabled` 409 | — | full-page states |

- **Only reads retry automatically.** A `human.prepare` is retried only with the same `opts.idem` (the host returns the
  same request); `human.cancel` is idempotent; nothing else is retried.
- Host limits the dashboard respects (v1 §0.9): 8 requests in flight and 8 subscriptions per connection, 20 pending
  human requests per person, 32 connections per UID. The server uses one connection per workspace for requests and the
  subscription.
- The UI keys only on codes from `ops/errors.py`, contract v1's §0.10 codes and the dashboard's list. A contract test
  enforces this.

## 9. Packaging, dev host and layout

```
addons/dashboard/
├── orch-addon.json          name "dashboard", entry {cmd: ["orch-dashboard", "serve"]}, capabilities ["serve_http"]
├── pyproject.toml, uv.lock  package "orch-dashboard": fastapi, uvicorn (exact pins); orch-core as a uv path source
├── server/
│   ├── src/orch_dashboard/
│   │   ├── __main__.py      serve / launch / open / stop
│   │   ├── app.py           FastAPI app, static files + SPA fallback
│   │   ├── security.py      Host check, Origin, headers, limits, redaction
│   │   ├── auth.py          control socket, probe, launch codes, in-memory sessions
│   │   ├── hostlink.py      contract v1 client (frames, hello/caps, req/res, cancel, evt, outcome.get), one connection per workspace
│   │   ├── adapters/        Result data → UI types (Appendix B), one module per shape
│   │   ├── routes/          shell, today, tickets, ticket, handoff, settings (closed allowlist, typed models)
│   │   ├── live.py          one sub.open per workspace, fetch-SSE fan-out, resync
│   │   ├── errors.py        error body → HTTP (§8)
│   │   └── static/          the Vite build output (gitignored)
│   └── tests/
├── devhost/                 stub host for dev and tests only (not in the wheel)
├── package.json, src/, vite.config.ts
└── docs/
```

- **Build.** `npm run build` writes `dist/`; a build step copies it into `orch_dashboard/static/`, so the wheel is
  self-contained and needs no Node at run time. FastAPI stays in the addon package (core §2).
- **Two front-end entry points.** Production (`src/main.prod.tsx`) never imports the mock: no persistent mock store,
  no mock clock. The mock entry stays the default for `npm run dev`, vitest and the layout guard. Today `client.ts`
  builds the mock store at module load; that moves into the mock entry.
- **Stub host (`devhost/`).** Until stream A's H4a lands, a stub speaks **contract v1 frames** on a temporary Unix
  socket and serves the P1 registry in-process: read ops, `whoami`, `workspace.show`, `member.list` from the
  roster, `ticket.query` and `needs` as thin wrappers over the P1 model, and `sub.*` by polling the store. It does
  **not** implement `human.*`: signing waits for the real host
  (H5a/H5b). It runs only on workspaces marked dev and only with `ORCH_DEV=1`, it is not in the wheel, and the server has
  no setting that selects it; it is reached through `ORCH_HOST_SOCKET` like any host. Stubs of reserved ops are listed
  in one file and deleted when the real host serves them. No issue is closed on stub-only tests.
- **CI** (new `dashboard.yml` on `addons/dashboard/**`, PRs into `develop`): node job (`npm ci`, typecheck,
  `npx vitest run`, build) and python job (`uv sync --locked`, ruff, `pytest -q`). Real-browser e2e runs in the
  orch-dev-kit harness (T2, D32).

## 10. Testing (§16, §23)

| Tier | Dashboard |
|---|---|
| T0 | the issues' files: `tests/test_dashboard_shell.py`, `tests/test_live_updates.py` (#304); `tests/test_dashboard_today.py`, `…_board.py`, `…_ticket.py`, `…_actions.py` (#305); `tests/test_dashboard_settings.py`, `tests/test_workspace_switcher.py` (#306); plus `test_dashboard_auth.py`, `test_dashboard_handoff.py`, `test_dashboard_errors.py`, `test_dashboard_adapters.py`. FastAPI `TestClient` against the stub host or, from H4a on, `orch host run --foreground`. Front end: `npx vitest run <file>` |
| T1 | `pytest server/tests` + ruff; full vitest, typecheck, build |
| T2 | everything against a real host (`orch host run --foreground` on a demo workspace) and the Playwright scenarios in orch-dev-kit `e2e/` (Appendix C), in Chromium, Firefox and WebKit |
| Layout guard | unchanged on the mock build; one extra run against the real server with the DEMO import |

**Adversarial tests** (the security reviewer writes or reviews them, §15):
- wrong, duplicate or `localhost` Host → 421; `Origin: null` or a foreign origin on a write → 403;
- a page on another localhost port cannot read data or trigger a write (real browser; no cookie exists to steal);
- launch code reused, expired, from another server instance; the fragment is gone before the first request;
- probe: a squatter on the port is refused by the launcher;
- `/api` without a token → 401; no CORS headers on any response;
- a `person`-class check: the server refuses a socket whose `welcome.peer.class` is not `person`;
- path, stdin or unknown fields in any route body → 400; the host never sees a path from the dashboard;
- a restricted ticket never shows in a stream, list, count, 404 message or log line; hiding a ticket purges it from
  an open tab;
- revocation while a stream is open closes it and purges the cache;
- lost response after submit → "Already signed"; expired request; concurrent prepares reuse one request; logout
  cancels pending requests;
- two workspaces with the same prefix and the same ticket key, with a hand-off panel open during a switch;
- the confirmation word never reaches the browser; `Authorization` and launch codes never appear in logs.

`src/test/signing-surface.test.tsx` changes: the hand-off panel renders its preview only from `material`, labels it a
preview, and has no factor input.

## 11. Security review points (Opus, #304 and #305)

1. Bearer token in memory/`sessionStorage`: XSS surface, CSP, `BroadcastChannel` sharing, logout and expiry.
2. Bootstrap: code entropy, single use, instance binding, fragment scrub before any request, `/auth/session` limits.
3. Origin ownership: single `127.0.0.1` listener, control-socket peer checks both ways, the probe, stale-socket and
   lock handling, stop over IPC.
4. Host check and DNS rebinding; one canonical origin; no proxy headers; every unsafe method checked.
5. The process user and the `person`-class check; the stated same-UID limit before #287.
6. Hand-off: the panel never handles a factor or the confirmation word; the preview is labelled untrusted; cancel and
   replay behaviour; no automatic re-prepare.
7. Closed op allowlist and typed inputs; no paths, no stdin, no raw events; `blob.put` bounds.
8. Visibility through every route, the stream and errors; purge on `gone` and revocation.
9. Workspace UUID through routes, caches, streams and requests; duplicate prefixes.
10. Mock code absent from the production bundle and routes (a test lists routes against an allowlist; a bundle test
    checks no mock module is included).
11. Dependency pins (FastAPI, uvicorn, starlette); the server serves only its packaged `static/`.

## 12. Milestones

Each milestone is one PR into `develop`, mergeable on green CI (D33). "Mergeable" is not "accepted": an issue
checkbox is ticked only when its Appendix C scenario passes against a **real host** in a real browser.

| # | Content | Acceptance | Depends on | Review |
|---|---|---|---|---|
| **M0 Skeleton, auth, read-only shell** | package, `orch-addon.json`, server with static serving, `security.py`, `auth.py` (control socket, probe, launch code, bearer sessions), `hostlink.py` (contract v1), stub host, production entry without mock, feature gating from `caps`, dark shell with sidebar, switcher list and identity (read only), `dashboard.yml` | #304 "auth uses v2 identity" (stub; ticked on the real host from H4a), "shell renders" (dark; after the §14 Q1 amendment). Tests: `test_dashboard_auth.py`, `test_dashboard_shell.py`, adversarial auth tests. e2e S1, S2 | — (stub); H4a for acceptance | **Opus security** |
| **M1 #304 live updates** | `live.py` (snapshot + `sub.open`, invalidation and reopen), fetch-SSE, `ready`/`resync`, per-tab `Last-Event-ID`, coarse invalidation, `gone` purge, revocation, poll fallback | #304 "live updates push ticket changes". Tests: `test_live_updates.py`. e2e S3, S4 | M0; H6a for acceptance | Opus (visibility) |
| **M2 #305 reads** | adapters (Appendix B), Today (`needs`), board and backlog (read, `ticket.query` with paging), ticket page (sections, tasks, evidence via `artifact.get`, log), Changes tab (`diff`), UUID-keyed queries | #305 "Today lists items", "ticket page shows sections, tasks, evidence and log", board half of "show and move". Tests: `test_dashboard_today.py`, `…_board.py`, `…_ticket.py`, `test_dashboard_adapters.py`. e2e S5, S6, S7 | M1; H6b, H3b (`diff`); R1 for the structured log | code |
| **M3 #305 actions via hand-off** | hand-off panel (preview from `material`), `human.prepare`/`pending`/`cancel`, `outcome.get`, approve, request changes, verdict, answer, comment (`log`), label (`set`), move, close, reopen, New ticket (one `new`) | #305 "ticket actions work", move half of "show and move", "new ticket creates a ticket". Tests: `test_dashboard_actions.py`, `test_dashboard_handoff.py`. e2e S8–S11, `e2e/new_ticket.spec` | M2; H5a, H5b; R3 (`move`), R7 | **Opus security** |
| **M4 #306** | reduced settings (members with certificate upload, grants list/revoke; gate policy and grant length read-only until R5), switcher by UUID with duplicate prefixes and an unavailable host | #306 both boxes ("saves" needs R5 for policy and grant length, else the checkbox is ticked on member changes and grant revoke with the owner's agreement, §14 Q8). Tests: `test_dashboard_settings.py`, `test_workspace_switcher.py`. e2e S12, S13 | M3; R2, R5, R9 | code + security for member signing |
| **M5 Touch ID** (P2, before cutover) | "Sign with Touch ID" in the hand-off panel, starting the H9 helper with the request code | e2e S14 (macOS, manual T2 run with a real Secure Enclave) | M3; H9 | Opus security |

Deferred beyond these milestones: batches, restricted creation, a stream replay buffer, WebAuthn, Windows.

## 13. Requests to stream A

| # | Request | Needed by |
|---|---|---|
| R1 | **Structured `show` data**: events for `show --log` as objects (`seq, type, at, actor, summary`), question states and evidence digests per criterion in the `full` document | M2 (log tab falls back to fenced text) |
| R2 | **`grant.list`** (not in v1) | M4 |
| R3 | **`move`**: land the F1 amendment H0 (`ticket.moved`) so the provisional op becomes served | M3 |
| R4 | A **restricted creation** that is restricted from its first durable event (or atomic create-then-restrict) | after P2 |
| R5 | F1 events for the reserved **`policy.set`** and **`settings.set`**, then serve them | M4 |
| R6 | A **per-request state lookup**: `human.pending {request_id}` (own person) returning `prepared`, `submitted` (from the tombstone, with event id and seq), `cancelled` or `expired` | M3 (heuristic in §4.3 until then) |
| R7 | **Signer choice in a hand-off**: `human.prepare` needs `signer:{device, auth}`, but the dashboard does not know which device will sign. Either `whoami` lists this machine's devices of the person with their `auth`, or `handoff:true` lets the signer bind its device when it fetches the request | M3 |
| R8 | `orch sign` and H9 **print the request fingerprint** (`Prepared.fingerprint`: first 12 hex of sha256(cj({review_digest, events stripped of sig/device/auth}))) so the person can match it with the panel | M3 |
| R9 | Confirm that `human.prepare` takes **`files`** for the inner op (the `member.add` certificate as a blob) | M4 |
| R10 | Amend **D-P2-7's HTTP rules**: bearer header instead of cookie; `Host` exactly `127.0.0.1:<port>` (no `localhost`) | M0 |
| R11 | Confirm that **control-socket `host.list`** may be called by the dashboard (host UID) and lists, per workspace, the id and whether the caller's UID is mapped to a person there | M0 |

Answered by contract v1 and removed from this list: `ticket.query` (summary rows, filters, paging), `needs` (core,
plus the `needs` topic), person versions of `new`/`set`/`log`/`ask`, `diff` bound to `source_sha`, identifier-free
loss of visibility (`gone` items), omitting the confirmation word (v1 returns `material`, not review lines), H9 as a
signer with its own native review (D-P2-22), `opts.idem` and outcome recovery.

## 14. Questions for the owner

1. **Dark only.** #304's acceptance says "light and dark"; the owner decided dark only. Proposal: amend the checkbox
   to "Shell renders with the new look and feel (dark)". Also relabel #304–#306 from "P1a" to "P2" (D42).
2. **Daily friction until H9.** Every comment, label and move is a signed person event (N1), so each one is a terminal
   round-trip until the Touch ID helper lands (in P2 before the cutover, D-P2-22). Accept the interim friction?
3. **Windows** (D63): the dashboard is not supported on Windows in P2. Accept?
4. **WebAuthn**: amend D64's "webauthn (dashboard, P2)" to "later, with a new signed-event contract".
5. **Random port**: accept that copied links need `orch dashboard open` after a server restart, instead of a fixed
   port that another process could squat?
6. **Spec amendments**: format §8.1 (`serve_http` enforced by the launcher, not the host) and portable-custody's
   cookie wording (bearer header).
7. **Member invites**: the invitee's `pk` and certificate arrive as an uploaded file or pasted JSON; the person id is
   compared out of band (§10.8). Is that enough for P2?
8. **#306 "saves the reduced settings"** while `policy.set`/`settings.set` wait for F1 events: wait for R5, or accept
   member changes and grant revoke as the saved settings in P2?

## 15. Changes after the Codex challenge

| Codex # | Finding | Resolution |
|---|---|---|
| 1 (Critical) | browser passphrase entry | **Removed.** No factor ever enters the browser; terminal hand-off is the baseline, Touch ID helper before cutover (§4). The zeroisation claim is gone |
| 2 | localhost cookie exposed to other ports | **No cookies**: bearer token in memory/`sessionStorage`, sent in a header, fetch-streamed SSE (§3) |
| 3 | dashboard-rendered review is not trusted | Stated plainly: the dashboard is not a trusted display; the signer recomputes hashes from `material` and renders its own review (§2, §4.1, §4.2) |
| 4 | short hash conflates gate content and request | The panel shows the request fingerprint (`Prepared.fingerprint`, the §0.8.1 formula over `review_digest` and the events) and labels the gate hash "content hash"; the signer prints the same fingerprint (R8) and builds the signing bytes itself (§4.1) |
| 5 | immutable signing object | Contract v1: `human.prepare` freezes the event and `material`; `human.submit` compares canonical JSON; the signer recomputes hashes (H5a, D-P2-21). The dashboard never signs or rebuilds |
| 6 | external-state checks at commit | Stream A's prepared `precommit` (git re-read under the lock) runs at submit; the residual git race is stream A's to state (H5a) |
| 7 | request ownership, replay, ambiguous completion | §4.3: host-owned request with tombstones (v1), one request per action, required `opts.idem` + `outcome.get`, cancel on close/logout/switch/`gone`, "Already signed/done", no auto re-prepare; R6 |
| 8 | human read identity | v1 maps the UID to a person in the host registry and uses that principal for reads, `needs` and subscriptions; the dashboard uses `whoami` and never names a person (§5) |
| 9 | UUID vs prefix | Every API path, cache key, stream and request keyed by workspace UUID; routes keep the prefix, disambiguated with `?ws=` (§5); duplicate-prefix tests (§10, S13) |
| 10 | filesystem-oriented CLI args | Closed allowlist with typed models; no paths or stdin; bytes via `blob.put`; the host never opens client paths (§4.4) |
| 11 | batch creation leaks restricted tickets | No batches in P2; New ticket is one `new`; restricted creation hidden until R4 (§4.5) |
| 12 | SSE authz and revocation | Streams bound to sessions; v1 revalidates before every delivery and sends `gone` for hidden tickets; the tab purges them and cancels their requests; membership re-check with `revoked` (§6) |
| 13 | shared subscription vs per-tab resume | Per-tab `Last-Event-ID`; mismatch → `resync` and refetch; subscribe before snapshot; upstream snapshot + `sub_cursor` reopen on invalidation; slow-tab bound (§6) |
| 14 | bootstrap contradiction | Route-by-route auth table (§3.3) |
| 15 | fragment leakage | Scrub before any request, no-store, instance-bound atomic consume, never printed; limitation documented (§3.2) |
| 16 | Host/Origin precision | One canonical origin `127.0.0.1:<port>`, `localhost` rejected, `Origin: null` rejected, no proxy headers, every unsafe method (§3.3) |
| 17 | launch/server identity | flock startup, single listener, stale-socket rules, two-way peer check, HMAC probe of the port, stop over IPC; same-UID scoped out (§3.1, §2) |
| 18 | addon capability without a dashboard principal | Dropped the claim: `serve_http` is a launcher policy switch, signing independent of it; spec amendment (§2, §14 Q6) |
| 19 | P1 results vs mockup shapes | Adapter layer with a listed shape table (§7, Appendix B); `ticket.query`/`needs` close most gaps, R1 the rest; no parsing of `lines` |
| 20 | review coverage | The trusted review is the signer's, rendered from `material` after recomputing hashes (D-P2-21). The panel's preview is rendered from the same frozen `material`, never from the cached ticket, so it is pinned by construction. Per-op coverage (member key/cert, policy before/after) is a stream A H5a acceptance item |
| 21 | diff base and immutability | v1 `diff` is bound to D58 `source_sha` and returns base/head OIDs and `truncated`; the cache key includes the returned OIDs; the UI does not claim the signature binds the diff (§7) |
| 22 | platform/backend matrix | §4.6: terminal everywhere supported, Touch ID before cutover, Windows and WebAuthn out with amendments (§14 Q3, Q4) |
| 23 | stub host becomes a second host | The stub speaks contract v1, serves reads only, no `human.*`, dev-only, not in the wheel, not selectable by setting; production entry without mock; real-host tests gate acceptance (§9) |
| 24 | acceptance incomplete | Checkbox-to-scenario matrix (Appendix C); "mergeable" vs "accepted" (§12); real-browser tests for browser security |
| 25 | complexity | Coarse invalidation, resync instead of replay, no batches, no widget CSP, query requirements limited to daily screens (§6, §4.5) |
| 26 | error mapping and limits | Codes mapped individually, custody codes gone (the dashboard never touches custody), only reads retry, limits listed (§3.3, §6, §8) |

**Where this revision does not follow Codex exactly:**
- **#20, pinning evidence and diff navigation to the reviewed versions.** Not done in the dashboard. The browser is
  explicitly untrusted, and the preview is rendered from the frozen `material`, so pinning other tabs would add complexity
  without adding a guarantee. The guarantee lives in the signer.
- **#25, granular invalidation.** Kept as a small table instead of "workspace invalidation only": it is a few lines
  and avoids refetching every ticket on each task event.
- **#12, "specify a generic invalidation frame".** Not needed: contract v1's `gone {uid}` names only a ticket the
  subscriber already knew, which reveals nothing new.
- **#2, the trade-off.** Codex presented a header credential as one option needing reconciliation. This revision
  decides it (no cookies) and asks for the spec amendments, accepting script-readable tokens because an XSS still
  cannot sign.

## Appendix A. Route list (real server)

`GET /`, SPA paths, `/assets/*`; `GET /auth/probe`; `POST /auth/session`, `/auth/logout`; `GET /api/session`;
`GET /api/workspaces`; `GET /api/me`; `GET /api/w/<uuid>/identity`, `/today`, `/tickets`, `/board`, `/people`,
`/tickets/<key>`, `/tickets/<key>/events`, `/tickets/<key>/changes`, `/grants`, `/views`, `/stream`, `/head`;
`POST /api/w/<uuid>/handoff` (prepare), `GET /api/w/<uuid>/handoff/<request_id>` (state),
`DELETE /api/w/<uuid>/handoff/<request_id>` (cancel), `POST /api/w/<uuid>/blobs`, `POST /api/w/<uuid>/views`,
`DELETE /api/w/<uuid>/views/<id>`. A test compares the app's routes with this list.

## Appendix B. Shapes to adapt

| Source (contract v1 `result.data`) | UI type (`src/api/types.ts`) | Adaptation |
|---|---|---|
| every `res ok` | endpoint objects | unwrap `result.data`; drop `lines`, `hints`; keep `cursor` only for the stream |
| status `in_progress` etc. | `Status` (`in-progress`) | spelling map |
| `show {full:true}` document + `ticket.query` row | `TicketDocument` | fields map 1:1 where present; gate states from the row; evidence per AC and question states per R1; people, visibility, turn and section revisions → "not available" until served |
| `show {log:true}` (`events` as strings) | `OrchEvent[]` | needs structured events (R1); until then a plain fenced text log |
| `ticket.query` rows | `TicketSummary` | map `assignees`, `claim`, `gates`, `needs`, `head_seq`; `next_page` drives paging |
| `diff` | `TicketChanges` | `sources[]` per repo, `files[]`, `unified`, `truncated` |
| `artifact.get` | evidence viewer | chunked, digest checked, rendered as data |
| `search` hits `{key, where, line}` | palette results | new small type; snippets rendered as data |
| `source_sha` list `[{repo, branch, sha}]` | `TicketBranch` | multi-repo; UI shows one row per repo (UI change) |
| `whoami` + `member.list` | `Me`, `Member[]` | person, name, role per workspace; devices without keys |
| control `host.list`, `workspace.show`, `host.status` | `Workspace`, `WorkspaceIdentity` | UUID primary key, prefix as label, tier |
| `needs` items | `TodayDocument`, `NeedsYouItem` | kinds `question`, `approve`, `no_eligible`, `acknowledge`, `addon` mapped to the UI's kinds |
| `human.prepare` result | new `HandoffRequest` | `{id, code, expires_at, fingerprint (the host's Prepared.fingerprint), material, events, gate_hash?}`; the preview renders `material` per op |
| `human.pending` | new `HandoffState` | pending or not (R6 adds the final state) |
| `res err` | `ApiError` | `{code, message, hint, retryable, exit}` |

## Appendix C. Acceptance: checkbox to scenario

All scenarios run in orch-dev-kit `e2e/` against `orch host run --foreground` on a demo workspace, in a real browser.

| Issue checkbox | Scenario |
|---|---|
| #304 shell renders (dark) | S1 `dashboard_shell.spec`: `orch dashboard` opens the browser, the fragment is scrubbed, the dark shell shows the workspace from `whoami` |
| #304 auth uses v2 identity | S2 `dashboard_auth.spec`: no token → "run orch dashboard"; a page on another localhost port gets nothing; a second tab joins via `BroadcastChannel`; logout ends both tabs |
| #304 live updates | S3 `dashboard_live.spec`: a CLI write in a terminal shows on the open ticket page within 2 s. S4: kill the stream, write, reconnect → resync shows the write; hiding a ticket purges it from the tab |
| #305 Today | S5 `dashboard_today.spec`: a question to the person and an open plan gate appear in Today and disappear after they are handled |
| #305 board and backlog show | S6 `dashboard_board.spec`: tickets in every status appear in the right column; backlog lists backlog tickets |
| #305 ticket page | S7 `dashboard_ticket.spec`: sections, tasks with state, evidence with digests, and the log of a demo ticket |
| #305 actions | S8 approve plan via hand-off (`orch sign CODE` in a pty); S9 request changes and verdict; S10 answer; S11 move backlog → open, comment, label, close, reopen; lost response → "Already signed"; expired request |
| #305 new ticket | `e2e/new_ticket.spec`: New ticket → hand-off → the ticket appears on the board |
| #306 settings | S12 `dashboard_settings.spec`: change grant length and gate policy via hand-off; add a member from a certificate file; revoke a grant |
| #306 switcher | S13 `dashboard_switcher.spec`: two workspaces with the same prefix and the same ticket key; switch with a hand-off panel open (it cancels); one host stopped shows "host not running" |
| M5 (no checkbox) | S14 `dashboard_touchid.spec` (macOS, manual): the helper window shows its own review from `material`; Touch ID signs; the board refreshes |
