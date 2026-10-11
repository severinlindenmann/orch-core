# P2 dashboard addon: design

Status: **draft for review**, 11 Oct 2026. Branch `docs/dashboard-p2-design`. This document turns the Mission Control
mockup (this folder) into the real dashboard addon of phase P2 (orch-v2.md §13), covering issues #304, #305 and #306.
It builds on the host process of stream A (#325, #271; orch-52), which is still being designed. Where the spec is
silent, this document says so and proposes; **proposals for stream A are marked NEW**. Open questions are in §12.

Sources: orch-v2.md (§4, §5.5, §13, §16, §23, D33–D62), orch-v2-core.md (§1–§4, §7), orch-v2-portable-custody.md
(D63–D66), orch-v2-ticket-format.md (§5.2–§5.9, §7, §8, §9, §10.3–§10.4, §10.8), orch-v2-carryover.md (D01–D22),
`HANDOVER.md` (mock backend contract), `plugins/orch-core/src/orch/ops` (the 52 P1 operations).

## 1. Goal and scope

**Goal.** Daily work runs in the dashboard against a real workspace, so the cutover can happen (D44). The React app
from the mockup is kept as is. Only the transport changes, from the in-process mock to HTTP against a local dashboard
server, and that server talks to the host socket. As HANDOVER says, components only use `src/api/client.ts`.

| Issue | Carry-over | In P2 scope |
|---|---|---|
| #304 | D01 shell, D03 live updates | Shell, sidebar, workspace context, browser login with v2 identity (replaces D02 token auth), live updates |
| #305 | D04–D08 | Today (needs you), board and backlog, ticket page (sections, tasks, evidence, log, changes), ticket actions (approve, request changes, verdict, answer, comment, label, move, close/reopen), new ticket (ticket mode) |
| #306 | D20, D22 | Reduced settings (general, members, gate policies, grant length, grants list/revoke), workspace switcher by UUID |

**Not in P2 for this addon** (they stay behind the mock transport, or are hidden on the real one): the agents page
(D09, D11), terminals (D16), widgets (D17), guide (D19), and every addon page and action (each has its own P2 issue
and reuses this server later). Also out: artifacts page (proposal `artifact.search`), relay and devices (P3), skills,
connections and doctor (D55–D57, C25d), mandates (D62, preview only), Factory modes of New ticket (Phase 2), shared
saved views, undo-create, workspace rename/archive, the review tour and Demo data. Phone signing (D49) is P4.

**Spec conflict to settle:** #304's acceptance says "light and dark"; the owner's mockup decision (HANDOVER, 8 Oct) is
**dark only**. This design follows dark only, and #304's checkbox should be amended (§12 Q14).

## 2. Architecture

```
Browser (React app, static build)
   │  HTTP + SSE, http://localhost:<port>, session cookie
   ▼
Dashboard server  (orch_dashboard, FastAPI + uvicorn, one per OS user, human UID, loopback only)
   │  host socket frames (#271), one connection pool per workspace, human-UID peer class
   ▼
Host process per workspace  (orch serve / orch host: registry, WSK, custody, Store.append)
```

**Process and user model.**
- The dashboard server runs as the **person's own UID**. It is never the agent UID (#287). At start-up it reads the
  configured agent UID of each workspace from the host (or the state dir) and refuses to start if they are equal. The
  host refuses the human-peer op list from the agent UID anyway (§5.5 peer check), so this check is defence in depth.
- It holds **no key**: no WSK, no `dk_sig`, no grant secret. Every write is an operation on the host socket, and the
  host builds and signs the event (D3, §5.5: no raw `sign`).
- **One server per OS user, serving every workspace.** The switcher (D22) is cross-workspace, but sockets are per
  workspace (`<state>/hosts/<workspace_id>.sock`). So the server is not a child of any single host. It finds
  workspaces from the state dir's host registry and opens each socket.
- **Started by `orch dashboard`**, which the person runs in their own terminal. It starts the server if no server is
  running (pid and lock files in `<state>/dashboard/`, mode 0700), asks it for a launch URL and opens the browser. A
  login item can come later. The host does not spawn it: on a VPS the host runs as `orch-host-<ws>` (§5.5), and the
  dashboard is desktop only (§13 P2). `orch dashboard --stop` stops it.
- It is still an **addon** (format §8, C9): `addons/dashboard/orch-addon.json`, capability `serve_http`. The host
  answers dashboard-class requests for a workspace only while the `dashboard` addon is granted and enabled there
  (`addon.granted`, D40). This is how `serve_http` is enforced (format §8.1: enforced by the P2 host).

**Binding.** The server listens on loopback only: 127.0.0.1 and ::1, never 0.0.0.0. The canonical origin is
`http://localhost:<port>`. It uses `localhost` because WebAuthn needs a host name and refuses IP addresses (§4 option
c), and the mockup's dev setup already uses localhost. The port is a **stable preferred port** from the dashboard
config (default proposed: 4747), so copied links (permanent URLs, G2) keep working. If that port is taken, the server
uses a random free port and `orch dashboard` prints the URL. Copied links are only valid on the same machine.

**Why the browser never talks to the socket.** Browsers can't open Unix sockets or named pipes. A peer-UID check
can't tell a browser tab from any other process of the same user. The socket's frames are built for trusted local
clients, not for web origins that face DNS rebinding, CSRF and cross-origin reads. The dashboard server is the one
place that does web security (§3). It turns REST/SSE into socket operations, filters what the session may see, and
serves the static app.

**Before #287 (agent as its own OS user).** If an agent runs under the person's UID, it can read the dashboard's state,
call the dashboard's control socket and reach the host socket as the person. This is the same accepted limit as
core §4 and portable-custody §3. Human signatures still need the factor (§4). The dashboard shows "agents share your
user account" in Settings → General while the host reports it (from `doctor`'s tier line).

## 3. Browser authentication (replaces D02)

The threats are another local user or the agent UID reaching the loopback port, a web page in the same browser
(CSRF, DNS rebinding, cross-origin reads), and a leaked URL.

| Option | Verdict |
|---|---|
| Static token in a file or URL (v1, D02) | Rejected: portable-custody §3 forbids a bearer token in a file an agent can read; URL tokens leak into history |
| No auth on loopback | Rejected: the agent UID and other users can connect to 127.0.0.1 |
| **One-time launch token → session cookie** | **Recommended** |
| WebAuthn login per session | Later, together with option (c) in §4. It authenticates the browser well, but adds nothing over the launch flow while the server is per-UID |

**Recommended flow.**
1. `orch dashboard` connects to the server's control socket `<state>/dashboard/ctl.sock`. The socket is mode 0600 in
   a 0700 directory, and the server checks that the peer UID equals its own. The client asks for a launch.
2. The server mints a 256-bit launch token. It is single use, valid for 60 s, kept only in memory and bound to the OS
   user. The client opens `http://localhost:<port>/auth/launch#t=<token>`. The token is in the **fragment**, so it
   never reaches server logs, `Referer` or the proxy. A tiny first-party script on that page (an external file, so the
   CSP needs no inline script) POSTs the token to `/auth/session` and replaces the history entry.
3. The server checks and consumes the token, then sets `orch_session`: 256-bit random, `HttpOnly`, `SameSite=Strict`,
   `Path=/`, plus `Secure` where the browser accepts it on `http://localhost` (to verify per browser; §12 Q9). Sessions
   live **in memory only**. They expire after 8 h idle or 24 h at most, and a server restart means running
   `orch dashboard` again. Nothing goes to disk (portable-custody §3).
4. The session is bound to the OS user, not to a typed person id. For each workspace the server asks the host
   `whoami` (NEW). The host derives the person from the device key on this machine and the replayed roster, as
   `orch.ops.human` does (§10.8 "Who signs"). The browser never names the person. The mock's `setViewer` does not
   exist on the real transport.

**Every request** (GET included, because the data is private):
- **Host header allowlist**: `localhost:<port>`, `127.0.0.1:<port>`, `[::1]:<port>`. Anything else gets 421 before
  routing. This is the DNS-rebinding defence.
- A valid session cookie, otherwise 401 `session.required` (the app shows "Run `orch dashboard` in your terminal").
- On writes (POST): `Origin` must be the canonical origin (or one of the allowlisted spellings), `Sec-Fetch-Site` must
  be `same-origin` when present, and the header `X-Orch-CSRF` must equal the per-session token from `GET /api/session`.
  A custom header forces a CORS preflight, and the server answers no CORS at all.
- Response headers: CSP `default-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'self'`, plus `frame-src`/`child-src` as the inert addon frames and pinned widget
  templates need (review point §10). Also `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and
  `Cache-Control: no-store` on `/api`.

`createFetchTransport` changes in two places: base URL `''` (same origin), and the CSRF header on POSTs. `credentials`
stays `same-origin`.

## 4. Human-signed actions

Every person event is signed (format §5.2, "A person event is always signed", §12 N1). In the dashboard that means
approve, request changes, verdict, answer, close and reopen, and also **comment, label, move, new ticket, settings,
member changes and grant revoke**. Every one needs the person's factor (D41, D65, D66, T16). The mockup's SignDialog
already shows each signed value in full through `visible.tsx`, and the real flow keeps that.

| Option | Custody (D3, §5.5, D64) | What you see is what you sign | Verdict |
|---|---|---|---|
| **(a) Host signs after its own check and prompt.** Dashboard sends intent + expected values; host builds the event from verified state, renders the review, prompts through the custody backend, signs, appends | Keys stay in the host; no new signing path | The host renders the review. With `secure-enclave`, the OS Touch ID dialog shows action + short hash on a channel the browser can't forge (D41) | **Recommended** |
| (b) Dashboard server signs with a client library on the human UID | `dk_sig` would be opened by a second process: breaks D3 ("loaded only by the host") and doubles the key code to review | The server decides the bytes; nothing checks it | Rejected |
| (c) WebAuthn/passkey in the browser | The key never leaves the authenticator; works on every OS (D63) | The OS dialog shows only the site, not the action, so the browser page is the only display. The assertion signs `authenticatorData ‖ SHA-256(clientDataJSON)`, not `sig/ticket-event` bytes, so it needs a **new signed-event contract** (format §5.3: "a change to these bytes is a new contract") and verifier and vector changes in core and orch-relay | Later, as a backend inside (a) (§12 Q3) |

**Recommended protocol (two steps, both host ops; NEW for stream A).**

1. `sign.prepare {op, args, expect}`, where `op` is a human operation name from the registry (`approve`, `verdict`,
   `answer`, …). `args` are the CLI arguments. `expect` holds what the person saw on the page: gate `hash`, question
   `hash`, `source_sha`. The host runs everything the CLI runs before the prompt (§10.8): visibility, eligibility,
   `refuse_repeat`, artifact bytes, `observe` for D58/D59, `model.preview`. If `expect` differs from the verified state
   it refuses with `gate.stale` (or the model's question refusal), so the dialog the person read is the dialog that is
   bound. This fills HANDOVER's `hash`/`source_sha` contract. The CLI has no such argument, because there the review
   is shown in the same step. The host returns:
   `{request, review, digest, short_hash, summary, factor, expires_at}`.
   - `review` is the **same content the CLI review prints** (§10.8: section texts checked against their hashes,
     criteria, tasks with verify commands, links, source list, artifact names and digests, receipts for `verify`), in
     structured JSON.
   - `digest` is the SHA-256 of the exact signed bytes (`sig/ticket-event|…` or `sig/ws-event|…`).
   - `summary` is the one-line action, for example `approve plan of DEMO-0043`.
   - `request` is single use, lives 120 s, and is bound to the dashboard session, op, normalised args and digest. It
     replaces the mock's boolean `confirmed` (HANDOVER "Known gaps").
2. The SignDialog draws `review` through `visible.tsx`, with the 12-hex short gate hash (the same as `orch show`'s
   `plan:open#8163ab12cd34`) and the digest under Details. The person confirms.
3. `sign.commit {request, factor}`. The host unlocks the factor, signs the event it built in step 1, and appends with
   `Store.append`, which judges again. A change in between gives `gate.stale` or `members.stale`, nothing is written,
   and the dialog reopens with a fresh prepare. The answer carries the appended event and the new ticket document.

**The factor, per backend (D64).**
- `secure-enclave` (macOS, P1b). The host calls the Touch ID dialog itself, with the reason text
  `"<summary> · <short_hash>"`, and `factor` is empty. This is the strongest end-to-end guarantee: a compromised page
  can lie on screen, but not in the OS dialog.
- `passphrase` (D65, every OS; the P1 default). The passphrase is typed into the SignDialog, travels once over the
  loopback session to the dashboard server, goes in the `sign.commit` frame to the host, is used for one signature and
  is zeroised. It is never logged, cached or stored. This **deviates from D65's "real terminal only" rule**. The
  equivalent guard is the human-peer class, the session and the single-use request. The weakness is the same one D65
  already accepts for a terminal: anything that can read the page or the person's typing can capture it. This needs the
  owner's decision (§12 Q2). The fallback that keeps D65 literal is `orch sign <request>` in the person's terminal: the
  terminal prints the host's review and prompts on `/dev/tty`, and the dashboard waits for the result over the change
  stream.
- `webauthn` (later; option c inside a). `prepare` returns a challenge equal to `digest`, the browser calls
  `navigator.credentials.get`, and `commit` carries the assertion. This needs the contract amendment.
- `auth` in the event records the backend actually used (D66). The SignDialog's toast says which one ("signed with
  passphrase", not "Touch ID", as the mock says today).

**Batches.** New ticket (`ticket.created` plus sections, people and visibility) and settings forms produce several
events. §5.3 allows one decryption per signature; the only exception is `import v1` (§14.2.2). So this design proposes
`sign.prepare` with an ordered list of events, **one factor unlock and N signatures**, all reviewed in one dialog (§12
Q4). Without that, New ticket costs up to four prompts.

**Grant issue stays in the CLI.** `grant` writes the secret only to `/dev/tty` (§10.8). The dashboard lists grants
and revokes them (a signed revoke). Issuing one with a hand-over to an agent arrives with start agent (D09), where the
host gives the secret straight to the agent it spawns.

## 5. Live updates (D03)

There is no change-stream operation yet (`wait` is per ticket and per agent session; `status` shows a session
cursor). This design proposes:

- **NEW `changes.watch {after}`**, a streaming host op on the human-peer class. Frames are
  `{cursor, log: "workspace" | <uid>, key?, seq, type, ws_seq}`. The host filters by visibility (§9): events of a
  restricted ticket the person can't see are omitted, and when visibility changes they are replaced by a frame that
  only invalidates. Frames carry no payload. The client refetches through the normal reads, so one renderer and one
  visibility check stay authoritative.
- **The cursor is opaque** (`"<host_run>:<n>"`, where `n` is a host-wide counter that only increases with every append
  in merged order, §5.5). If the host restarts or `after` is unknown, it sends `{resync: true}`.
- **Dashboard server → browser over SSE.** `GET /api/workspaces/:ws/changes`, using `EventSource` with the cookie.
  The event `id` is the cursor, so a reconnect sends `Last-Event-ID` and the server resumes with `after`. The server
  keeps **one host subscription per workspace** and fans out to tabs. It sends a heartbeat every 15 s, and the browser
  reconnects with backoff (1, 2, 5, 10 s). After three failures the app falls back to the mock's existing poll of
  `getCursor` (NEW `changes.head`) every 10 s. Order is the host's merged order, and frames are never reordered.
  SSE is chosen over WebSocket because traffic is one way and it reconnects with resume for free.
- **`useLiveUpdates` (`src/app/live.ts`)** now subscribes to SSE and invalidates by event instead of invalidating all
  `LIVE_KEYS` on any cursor change. The keys are those of `src/api/queries.ts`:

| Frame | Invalidate |
|---|---|
| Any ticket-log event of `key` | `['ticket', key]`, `['tickets', ws]` (all params), `['board', ws]`, `['today', ws]` |
| `question.*`, `gate.*`, `verdict.given`, `ticket.submitted`, `branch.pushed` | as above, plus the workspace's needs-you count in `['workspaces']` |
| `artifact.*` | as above (the ticket page reads artifacts from the ticket document) |
| `member.*`, `role.changed`, `device.*` | `['people', ws]`, `['identity', ws]`, `['workspaces']`, `['me']` |
| `policy.changed`, `settings.changed` | `['identity', ws]`, `['workspaces']`, `['today', ws]`, `['tickets', ws]` |
| `grant.*` | `['grants', ws]` |
| `addon.*` | `['workspace-addons', ws]`, `['addons']` |
| `resync`, or a reconnect gap | every key of `ws` |

The board's optimistic move rolls back when the server answers with an error. An open SignDialog keeps its snapshot,
as it does today; the host's stale refusal (§4) is what protects the person.

## 6. API mapping

The baseline is the P1 operation names (52 ops in `plugins/orch-core/src/orch/ops/commands`). Following stream A's
working assumption, the CLI op names, args and Result/error envelope (§10.4) carry over 1:1 to the socket. The
dashboard server is thin: one route calls one op (or a fixed short list). "As person" means the op runs from the
human peer through `sign.prepare`/`commit` (§4).

| Client method (`client.ts`) | Host op | Notes |
|---|---|---|
| `getMe` | NEW `whoami` | `{person, name, roles: {ws: role}, factor}`; server merges across workspaces |
| `getWorkspaces` | server's host registry + NEW `workspace.show` per ws | uuid, prefix, name, role, needs-you count (from `needs`), host status |
| `getIdentity` | NEW `workspace.show` | uuid, prefix, epoch, WSK fingerprint, `hosted by` (§4 threat model), custody tier |
| `getCursor` | NEW `changes.head` | fallback only (§5); live through NEW `changes.watch` |
| `getAddons`, `getWorkspaceAddons` | `addon.list` | shell nav only; addon pages hidden in P2 (§1) |
| `getToday` | NEW `needs` | needs-you for the person: questions to them, gates and verdicts they are eligible for, C9 `needs` rules, plus `waiting_on_others`. `inbox` is peer hand-over plus session decisions, not this |
| `listTickets` | `list`, `search` | **Deviation:** `list` has only `status`, `mine`, `label`, `limit ≤ 200`. Board and Tickets need `type`, `priority`, `parent`, `person`, `needs`, `sort`, `repo`, `restricted`, and summary rows with turn and gate state. Proposal: extend `list` (or NEW `ticket.query`) |
| `listPeople` | NEW `member.list` | members, roles, devices (no keys) |
| `getTicket` | `show {ref, full: true}` (JSON) | the ticket document (§7). Needs open-gate hashes (full and 12-hex), question hashes, the branch head and diffstat (D58) |
| `getEvents` | `show {ref, log: true, since}` | JSON events; agent text fenced (§10.4.11) |
| `getChanges` | NEW `diff` | host reads git, never agent-reported SHAs (D58) |
| `postAction` `answer` | `answer {ref, question, option?, message?}` as person | `expect.hash` = question hash |
| … `approve` | `approve {ref, gate}` as person | `expect.hash` (requirements, plan) or `expect.source_sha` (code, D59) |
| … `request_changes` | `request_changes {ref, gate, message}` as person | `expect.hash` |
| … `verdict` | `verdict {ref, outcome, message?}` as person | `expect.hash` + `expect.source_sha` (D58) |
| … `comment` | `log {ref, message}` as person | **Deviation:** `log` is `who: unattended`; `log.added` allows a person (A), signed |
| … `ask` | `ask` as person | signed `question.asked` |
| … `add_label` | `set {ref, pairs: ["labels=…"]}` as person | signed `ticket.updated` |
| … `set_status` | NEW `move {ref, to: backlog \| open, reason?}`; `close`, `reopen` | `status.changed` exists (§5.4.1), no op emits it. **UI change:** §5.9 allows manual moves only between backlog and open. In progress, testing and done are derived (claim, submit, verdict), so the board limits drop targets accordingly |
| … `claim`, `release` | not offered | agents only; a person releasing a claim (§5.2) is not in P2 |
| `createTicket` | `new` as person (+ `section.set`, `ac.add`, people, visibility) | **Deviation:** `new` is `who: agent` (`grant_valid`); `ticket.created` allows a person. Proposal: `new` from the human peer accepts the full `NewTicketRequest` as one batch (§4). `people.changed` and `visibility.changed` have no op yet (§10.8): NEW `people.set`, `visibility.set` |
| `postSettings` `member.add` | `member.add {name, role, pk, cert}` as person | **UI change:** the mock takes `{person, name, role}`. Core takes the invitee's key and first device certificate, and the owner compares the person id out of band (§10.8) |
| … `member.role`, `member.remove` | `member.role`, `member.remove` as person | `members.last_owner` → refusal |
| … `gate.policy` | NEW `policy.set {gates}` as person (+ NEW `policy.preview` for the code gate, `previewCodeReview`) | event `policy.changed` exists, no op (§10.8) |
| … `grant.hours` | NEW `settings.set {grant_hours}` as person | event `settings.changed` (D60) exists, no op |
| `listGrants`, `revokeGrant` | NEW `grant.list`; `grant.revoke` as person | issue stays CLI (§4) |
| `issueGrant` | hidden | secret only to `/dev/tty` (§10.8) |
| `postSettings` `rename`, `archive`, `repos` | hidden | no format event for rename; archive is CLI only; repos is the Repos addon |
| `listViews`, `saveView`, `deleteView` | dashboard server state (`<state>/dashboard/views.json`, per person, 0600) | personal views only; `shared` hidden (not ticket data; no format event) |
| `undoCreateTicket` | hidden | logs are append-only; proposal `ticket.discard` has no format basis |
| `getTicketRefusals`, `getAgents`, `getAgentActivity`, `previewLaunch` | hidden | agents page and refusals: D09/D11 issues (proposals `session.*`) |
| `listArtifacts`, relay, skills, connections, secrets, doctor, addon state/actions/decisions/catalog/op | hidden | later issues (§1) |
| `simulatePush`, `simulateRelay`, `setViewer`, `resetDemo`, `getDataset`, mandates preview | **mock only** | not served by the real server (it answers 404); UI hides them when `transport.demo` is false |

**Feature gating.** `GET /api/session` returns `{csrf, features: [...]}`. The shell hides nav entries, Demo data and
the review tour that the real server does not list. The mock transport lists everything, so the mockup and its 1,790
tests stay as they are.

## 7. Error envelope

The host returns the core envelope `{"ok":false,"error":{code, message, hint?, fix?, retryable}}` (§10.4, the
`error_envelope` in `orch/cli/render.py`). The dashboard server passes `code`, `message`, `hint` and `retryable`
through **unchanged** and drops `fix.argv` (a CLI command). It shows `fix.argv` only on the CLI-fallback screens as "In
a terminal: `orch …`". It picks the HTTP status from the code family, so `client.ts` (`res.status >= 400` →
`ApiError`) needs no change:

| Core code (exit) | HTTP | UI |
|---|---|---|
| `not_found` (2), incl. tickets the person can't see (§9) | 404 | existing missing-ticket state. The mock's `not_visible` is renamed to `not_found` |
| `usage`, `unknown_command`, `invalid.input`, `parse.*`, `grant.secret_in_args` (2, 5, 6) | 400 | inline form error with `hint` |
| `human_only`, `role.denied`, `grant.*`, `device.scope` (3) | 403 | refusal toast; "Hiding a button is not authorization" stays true |
| `transition.refused`, `gate.already_approved`, `claim.*`, `lease.*` (3, 4) | 409 | refusal with hint |
| `gate.stale`, `members.stale`, `artifact.mismatch`, `observe.unavailable`, `source.missing`, `conflict.*` (5, 8), model refusals of §10.4a (`question.stale` …) | 409 | **close the SignDialog, refetch, reopen it with a fresh prepare** (the existing decision-refresh pattern) |
| `custody.wrong_passphrase` | 401 | stay in the dialog, clear the field; nothing was written |
| `custody.no_key`, `custody.no_prompt`, `custody.no_batch` | 409 | "This machine has no device key for this workspace: run `orch init`/`keys`" |
| `lock.busy`, `retry.later` (4, 9) | 503 | automatic retry with backoff when `retryable` |
| `stop` | not expected | no session dedup or stop rule for the human peer |
| `internal`, `not_implemented` (1) | 500 | error boundary; `not_implemented` names the op |
| dashboard's own: `session.required` 401, `csrf.bad` 403, `host.unavailable` 503 (socket down), `addon.disabled` 409 (dashboard not granted in that workspace) | — | full-page states |

The mock's own codes (`verdict.stale`, `question.stale`, `validation.*`, `cli_only`, …) become the core codes above.
A contract test fails if the UI keys on a code that is not in `ops/errors.py` or the dashboard list.

## 8. Packaging and layout

```
addons/dashboard/
├── orch-addon.json          addon manifest: name "dashboard", entry {cmd: ["orch-dashboard", "serve"]}, capabilities ["serve_http"]
├── pyproject.toml, uv.lock  package "orch-dashboard": fastapi, uvicorn (exact pins); orch-core as a uv path source (../../plugins/orch-core)
├── server/
│   ├── src/orch_dashboard/
│   │   ├── __main__.py      serve / launch / stop (the `orch dashboard` command calls these)
│   │   ├── app.py           FastAPI app, static files + SPA fallback (HANDOVER Routes), /assets
│   │   ├── security.py      Host allowlist, Origin/Sec-Fetch/CSRF, CSP and headers
│   │   ├── auth.py          control socket, launch tokens, in-memory sessions
│   │   ├── hostlink.py      HostLink interface: SocketHostLink (#271 frames, orch.hostclient) and InProcessHostLink
│   │   ├── routes/          one module per screen group: shell, today, tickets, ticket, sign, settings
│   │   ├── live.py          one changes.watch per workspace, SSE fan-out
│   │   ├── errors.py        core envelope → HTTP (§7)
│   │   └── static/          the Vite build output (gitignored, filled by the build)
│   └── tests/               test_dashboard_*.py
├── package.json, src/, vite.config.ts   the React app, unchanged location
└── docs/
```

- **Build.** `npm run build` writes `dist/`. A Python build step copies it to `orch_dashboard/static/`, so the wheel is
  self-contained and `orch dashboard` needs no Node at run time.
- **Python deps stay in the addon** (core §2: FastAPI belongs to the addon package, not to the core).
- **Transport selection.** `client.ts` picks the transport from `import.meta.env.VITE_ORCH_TRANSPORT`: `mock` is the
  default for `npm run dev`, vitest and the layout guard, and `fetch` is used for the production build served by the
  server. In dev, Vite proxies `/api` and `/auth` to a running dashboard server.
- **Development before stream A lands: `InProcessHostLink`.** It runs the P1 registry in-process against a dev
  workspace (`bin/ws1`, the same dispatch `orch.cli.main` uses) and maps ops 1:1. NEW ops that core does not have yet
  (`whoami`, `needs`, `changes.watch`, `sign.*` …) are stubbed in `orch_dashboard.testing.stubhost` on top of the
  registry's reads, and marked so the stubs are deleted when the socket offers them. Signing in-process uses the D51
  dev override's test human, and only in dev-marked workspaces (§19). The server refuses `InProcessHostLink` unless
  `ORCH_DEV=1` and the workspace is marked dev.
- **CI** (new workflow `dashboard.yml` on `addons/dashboard/**`, PRs into `develop`):
  - node job: `npm ci`, `npm run typecheck`, `npx vitest run`, `npm run build`;
  - python job: `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest -q -m "not slow"`, and the slow tests.
  - Today CI doesn't run the dashboard suite (HANDOVER); P2 changes that. The layout guard and e2e stay local or T2
    (D32).

## 9. Testing (§16, §23)

| Tier | Dashboard |
|---|---|
| T0 | the issues' files: `tests/test_dashboard_shell.py`, `tests/test_live_updates.py` (#304); `tests/test_dashboard_today.py`, `…_board.py`, `…_ticket.py`, `…_actions.py` (#305); `tests/test_dashboard_settings.py`, `tests/test_workspace_switcher.py` (#306); plus `tests/test_dashboard_auth.py`, `tests/test_dashboard_sign.py`, `tests/test_dashboard_errors.py`. FastAPI `TestClient` over `InProcessHostLink` on a tmp workspace, e.g. `uv run pytest server/tests/test_dashboard_auth.py -x -q`. Front end: `npx vitest run <file>` |
| T1 | `pytest server/tests` + ruff; full vitest, typecheck, build |
| T2 | the above against a real host socket (once #325 lands), golden JSON per route, and the Playwright scenarios in orch-dev-kit `e2e/`: `e2e/dashboard_shell.spec` (launch, shell, live update from a CLI write in another terminal), `e2e/new_ticket.spec`, plus `dashboard_actions.spec` (approve, request changes, verdict, answer with the test human), `dashboard_settings.spec`, `dashboard_switcher.spec` (two workspaces) |
| Layout guard | unchanged on the mock dev build (`npm run layout:guard -- --url … --docks min,max`). One extra run against the real server with the DEMO import, to catch layout from real data |

**Adversarial tests the security reviewer writes** (§15):
- wrong Host → 421; foreign Origin or no CSRF header → 403;
- launch token reused or expired;
- session cookie flags; no CORS headers;
- `/api` without a session → 401;
- agent UID refused by the server and by the host;
- `sign.commit` with another session's request, after expiry, twice, or with changed args → refused;
- `expect` mismatch → `gate.stale`;
- a restricted ticket never shows in the SSE stream, a list, a 404 message or a log line;
- passphrase never in logs or exceptions (log capture assertion).

Per §23, every feature in #304–#306 needs a passing e2e scenario. `src/test/signing-surface.test.tsx` gets a case for
each real signed path, with the displayed values compared against the host's `review`.

## 10. Security review points (Opus, #304 and #305)

1. Launch flow: token entropy, single use, fragment delivery, control-socket peer check, what happens if two
   `orch dashboard` races or another user's process connects.
2. Loopback binding, the Host allowlist and DNS rebinding; IPv6 `::1`; the port fallback must not move to a
   non-loopback interface.
3. CSRF and Origin rules on every write and on SSE; no CORS; `SameSite=Strict` behaviour on `localhost` across
   browsers; the `Secure` flag.
4. Session storage in memory only; logout; expiry; what the server writes to disk (views only, 0600).
5. The process user: never the agent UID; the stated limit before #287; the control socket and state dir modes.
6. Signing: does `sign.prepare` build exactly the bytes `commit` signs; request binding (session, op, args, digest,
   TTL, single use); TOCTOU between prepare, the prompt and `Store.append`; `expect` checks; batches.
7. WYSIWYS: is the dialog drawn only from the host's `review`; is the Touch ID reason built by the host; does the
   page's short hash match the reason; are agent-controlled strings escaped in both (`visible.tsx` and §10.4.11).
8. Passphrase path (if Q2 is accepted): no logging, no caching, zeroised in the server and the host, never in a URL or
   a GET.
9. Visibility through every route and the change stream; error messages that could leak titles; 404 equal for missing
   and hidden.
10. CSP against the inert addon frames and pinned widget templates (`sandbox` iframes, `srcdoc`); no inline script on
    `/auth/launch`.
11. Mock-only routes are absent from the real server (a test lists the app's routes against an allowlist).
12. Dependency pins (FastAPI, uvicorn, starlette) and the static build's integrity: the server serves only files from
    its packaged `static/`.

## 11. Milestones

Each one is a PR into `develop`, mergeable on its own on green CI (D33). The real host is switched on by config once
#325 is there; until then `InProcessHostLink` serves dev workspaces.

| # | Content | Depends on | Review |
|---|---|---|---|
| M0 Skeleton | `pyproject`, `orch-addon.json`, `orch_dashboard` with static serving and SPA fallback, `security.py` (Host/Origin/CSP), the HostLink interface with the in-process stub, transport selection in `client.ts` (mock stays default), feature gating, `dashboard.yml` CI. No data routes | — | code |
| M1 #304 | `orch dashboard`, control socket, launch → session, `/api/session`, `me`, `workspaces`, `identity`, SSE live updates, `useLiveUpdates` on SSE, error mapping, `test_dashboard_shell.py`, `test_live_updates.py`, `test_dashboard_auth.py`, `e2e/dashboard_shell.spec` | M0; stream A `whoami`, `workspace.show`, `changes.*` (stubbed until then) | **Opus security** |
| M2 #305 reads | Today, board, tickets, ticket page, events, changes; extended `list`; board drop targets limited per §5.9 | M1; `needs`, `list` extension, `diff` | code |
| M3 #305 actions | `sign.prepare`/`commit`, SignDialog from the host `review`, approve, request changes, verdict, answer, comment, label, move, close, reopen, New ticket (batch), `test_dashboard_actions.py`, `e2e/new_ticket.spec` | M2; `sign.*`, `move`, person `new`/`log`/`ask`/`set` | **Opus security** |
| M4 #306 | Settings (members with invite material, gate policy, grant length, grants list/revoke), switcher by UUID, `test_dashboard_settings.py`, `test_workspace_switcher.py` | M3 (signing); `member.list`, `policy.set`, `settings.set`, `grant.list` | code (+ security for member/policy signing, which uses M3's path) |

## 12. Open questions and decisions to make

**For stream A (orch-52)**
1. **Human-peer op list.** Confirm a peer class for the human UID with: reads (`show`, `list`, `search`, `addon.list`,
   NEW `whoami`, `workspace.show`, `member.list`, `grant.list`, `needs`, `diff`, `changes.head`, `changes.watch`), the
   human operations through `sign.prepare`/`sign.commit`, and the person variants of `new`, `log`, `ask`, `set`,
   NEW `move`, `people.set`, `visibility.set`, `policy.set`, `settings.set`. On macOS the host UID equals the human
   UID: how does the host tell "the dashboard" from "the CLI as host"? Proposal: it doesn't need to; both are the
   human peer.
2. **Where the factor is collected.** Option (a) with the host prompting (secure-enclave), and the passphrase entered in
   the dashboard and passed through (deviates from D65's TTY rule), or `orch sign <request>` in a terminal as the
   D65-literal fallback. Needs the owner's decision.
3. **WebAuthn (D64 says "webauthn (dashboard, P2)").** Is it in P2? It needs a new signed-event contract for
   assertions (format §5.3), verifier support in core and orch-relay vectors, and a device certificate holding the
   COSE key. Proposal: after M3, as a backend of (a).
4. **Batch signing**: one factor unlock for N events reviewed together (New ticket, settings). This needs a §5.3
   amendment like the `import v1` exception.
5. **`expect` on human ops** (gate hash, question hash, `source_sha`), refused as `gate.stale`. Add it to the CLI too,
   or socket only?
6. **Change stream**: frame shape, the opaque cursor and its counter (persisted in `.state/index.sqlite`?), visibility
   filtering, `resync`.
7. **`list` extension vs a new `ticket.query`**, and the summary row shape (turn, gate state, needs-me).
8. **`needs`** (Today): which rules core owns vs C9 addon `needs` rules; per person, with counts for the switcher.

**For the owner / core**
9. Cookie `Secure` on `http://localhost` and `SameSite` across Safari, Chrome and Firefox. If Safari misbehaves, does
   the dashboard support it in P2?
10. Every comment, label and move is a signed person event (format §12 N1), so each needs the factor. Is that
    acceptable UX in daily use, especially with the passphrase? Or should the spec get a lighter "person note" event
    (a format change)?
11. Preferred fixed port (4747?) and whether `orch dashboard` should install a login item.
12. Windows (D63, portable-custody §7): is the dashboard required on Windows in P2? Named pipe plus ACL for the human
    peer, and no `secure-enclave`.
13. Saved views: personal only (dashboard state) in P2; shared views need a format event.
14. Amend #304's "light and dark" to dark only (owner decision, HANDOVER). Also retitle #304–#306 from "P1a" to "P2"
    (D42).
15. Member invites: the dashboard flow for an invitee's `pk` and `cert` (paste or file), and how the person id is
    compared out of band (§10.8).
