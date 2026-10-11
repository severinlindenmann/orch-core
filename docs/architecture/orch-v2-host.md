# orch v2 P2: the host process (#325, #271, #287)

Status: Decided 11 Oct 2026 (draft by orch-52, Codex challenge, Opus tie-break on two points), revised the same day
after the Opus security review (F1–F15), the Codex clarity review (14 points) and the dashboard stream's requests
(R1–R11), and again after the security re-review (N1–N5), the Codex clarity re-review (12 points) and orch-relay's P3
host interface (§11), and finally after the Opus diff check (M1, L1–L3) and the Codex re-check (8 points). The socket
API contract §0 is **v1.4 (provisional)**, which adds the dashboard's read fields, the activity feed and the closed-ticket
rules (amendment of 11 Oct 2026): changes go through the coordination log.

Built on `origin/develop` 908f2511. Decisions are numbered **D-P2-n**, each with a one-line reason.

---

## 0. Socket API contract v1.4 (provisional)

§0 is complete on its own. Streams B (dashboard), C (mobile, from P3) and D (relay bridge) build on it without reading
the rest. Everything in §0 is normative: a host or client that differs from it is wrong.

### 0.1 Notation

The schemas below use a compact notation:

| Notation | Meaning |
|---|---|
| `k: T` | required key |
| `k?: T` | optional key: it may be absent and is never `null` |
| `T \| null` | the key is present and may be `null` |
| `int a..b` | an integer from `a` to `b`, both inclusive |
| `str{a..b}` | a string of `a` to `b` Unicode scalar values |
| `str/re/` | a string matching the regular expression `re` in full |
| `[T]{a..b}` | an array of `a` to `b` items |
| `{K: T}{a..b}` | a map with `a` to `b` keys |
| `= v` | the default when the key is absent |

- Every object is **closed**. A key not listed is an error when a client sends it (§0.3), and clients ignore unknown
  keys the host sends (§0.4).
- **Common types:**
  - `ULID` = `str/[0-7][0-9A-HJKMNP-TV-Z]{25}/`
  - `Hash` = `str/sha256:[0-9a-f]{64}/`
  - `Key` = `str/[A-Z][A-Z0-9]{1,9}-[0-9]{4,}/` (a ticket key)
  - `Ref` = the F1 §10.3 REF forms
  - `Uid` = the ticket uid (`ULID`)
  - `Person` = the F1 person id
  - `Device` = the F1 device id
  - `Time` = an RFC 3339 UTC string with `Z`
  - `Cursor` = `str{1..256}`, opaque
  - `B64` = standard base64 (RFC 4648 §4) with padding and no whitespace
  - `Status` = `"backlog"|"open"|"in_progress"|"waiting"|"testing"|"done"|"closed"` (F1 §5.9)
  - `Gate` = `"requirements"|"plan"|"verify"|"code"`
  - `NeedKind` = `"question"|"approve"|"no_eligible"|"acknowledge"|"addon"` (`orch.model.needs`)
  - `ArgName` = `str/[a-z][a-z_]{0,31}/`, the name of an op argument marked `x-file: true` in the registry
  - `Idem` = `str/a_[0-7][0-9A-HJKMNP-TV-Z]{25}/`
  - `WsId` = `str/[0-9a-f]{32}/`
  - `PeerSpec` = `{class:"person", person:Person} | {class:"agent"} | {class:"worker"}`
  - `Event` = the F1 §5.1 envelope without `seq`, `at`, `prev`, `ws_seq`, `host_sig` and `sig`
  - `SignedEvent` = an `Event` plus `sig: str` (base64url, ECDSA P-256 over the F1 §5.3 signing bytes)
  - `Actor` = `{kind:"person"|"agent"|"host"|"addon", id:str | null, for?:Person, session?:str, device?:Device, auth?:str,
    grant?:str, unattended?:bool}`: the F1 §5.2 actor of an event, without `sig` and `host_sig`. `id` is `null` for the host;
    `for`, `session` and `grant` are present for an agent with a grant, `device` and `auth` for a person, `unattended: true`
    for an unattended agent. The grant id is public (F1 §10.7).
  - `Snip` = `str`: the first 1 024 UTF-8 bytes of a stored text, cut on a scalar-value boundary. A field of type `Snip`
    comes with a sibling `<field>_cut:bool` that is present and `true` only when it was cut; no character is appended. The
    full text is not in this contract's reads (§9, v1.5).
  - `Source` = `{repo:str, ref:str, sha:str}`: one entry of the F1 §5.7 source list (`repo` is the repo identity)
- **Error codes** are listed per op. Every op may also return the universal codes of §0.6, in the precedence given
  there.

### 0.2 Transport and framing

- **Sockets.**
  - One socket per workspace, `<run>/<workspace_id>/host.sock`, for every workspace op.
  - One control socket, `<run>/control/host.sock` (directory 0700, host UID only), for the `host.*` admin and
    bootstrap ops.
  - One worker socket, `<run>/worker/host.sock` (directory 0750, group `orch-worker`, socket 0660), for the single
    worker connection (§0.5). It serves the jobs of **every** workspace, and each job names its `workspace_id`. A
    worker-UID connection to a workspace or control socket is `fatal` `peer.denied`, and so is any other UID on the
    worker socket.
- **Frame.** A 4-byte unsigned big-endian length `n`, then `n` bytes, with `1 ≤ n ≤ 2 097 152`. Readers handle partial
  and coalesced reads. The body must arrive within 10 s of the first byte of its length prefix.
- **Payload.** Exactly one JSON object, strict UTF-8. Each of the following is `frame.invalid`:
  - invalid UTF-8, or an escaped lone surrogate (`\ud800` without its pair);
  - a duplicate key at any level;
  - a number with a fraction or an exponent (no op uses floats), `NaN`, `Infinity`, or an integer outside
    −(2^53−1)..2^53−1;
  - nesting deeper than 32 (the top-level object is depth 1, and each nested object or array adds 1);
  - more than 65,536 JSON values in one frame (count every object, array, string, number, boolean and null,
    including the top-level object; object member names are not values);
  - a top-level value that is not an object;
  - `n = 0`.
- **Frame types.**
  - Client to host: `hello`, `req`, `cancel`.
  - Host to client: `welcome`, `res`, `evt`, `fatal`.
- **Fatal errors.** A framing error, a handshake error, or an error that belongs to no request gets one `fatal` frame.
  The host then closes the connection. A client that detects a framing error from the host just closes.
- **Handshake.** The first client frame must be `hello`, within 2 s of `accept`. The host answers with `welcome` or
  `fatal`. A second `hello`, or a `req` or `cancel` before `welcome`, is `fatal` `handshake.invalid`.
- **Request ids.** `str/[A-Za-z0-9_-]{1,32}/`, unique among the connection's open requests (sent and not yet
  answered). Reusing an open id is `fatal` `frame.invalid`. An id may be reused once its `res` has been sent.
- **Order.**
  - The host answers every `req` with exactly one `res`. Responses may arrive in any order.
  - Writes (§0.7) from one connection run in the order they were received.
  - The workspace worker serves principals round-robin, taking one request from each principal's per-connection FIFO
    in turn. So fairness never reorders one connection's writes.
  - A read may overtake a queued write.
- **Timeouts.**
  - The timer starts when the host has read the complete `req` frame.
  - `opts.timeout_ms` is 1000..600 000, `= 30 000`.
  - `wait` ignores `opts.timeout_ms`; its `timeout` argument governs (F1 §10.4 item 7).
  - A timed-out read or `wait` answers `timeout`. A timed-out write answers its real result if it committed meanwhile,
    otherwise `outcome.unknown` (§0.7). A timeout never implies a rollback.
- **Cancellation.** `cancel = {t:"cancel", id: str}`.
  - A request still in the queue answers `cancelled`.
  - A running read or a parked `wait` answers `cancelled`.
  - A running write answers its real result or `outcome.unknown`; it is never undone.
  - A `cancel` for an unknown or already answered id is ignored without a response.

### 0.3 Envelopes (normative)

```
Hello   = {t:"hello", proto:[int 1..65535]{1..8}, client:str{1..64},
           caps:[str/[a-z][a-z0-9_]{0,23}\/[0-9]{1,4}/]{0..64},
           ops?:{OpName: int 1..1000}{0..256}}
Welcome = {t:"welcome", proto:int, host:str{1..64}, host_uid:int 0..4294967295,
           workspace:{id:WsId, prefix:str{1..10}} | null,                  // null on the control and worker sockets
           peer:{uid:int, class:"person"|"agent"|"worker"|"control", person:Person | null},
           tier:"A"|"B", caps:[str]{0..64}, ops:{OpName:int}, registry:Hash,
           limits:{frame:int, inflight:int, timeout_ms:int, blob_bytes:int, inline_bytes:int}}
Req     = {t:"req", id:ReqId, op:OpName, args:object,
           auth?:{grant?:str{1..128}, session?:str{1..64}},
           opts?:{dry_run?:bool = false, idem?:Idem, timeout_ms?:int 1000..600000},
           files?:{ArgName: {b64:B64} | {blob:BlobId} | {digest:Hash}}{0..16}}
Res     = {t:"res", id:ReqId, ok:true, result:Result}
        | {t:"res", id:ReqId, ok:false, exit:int 1..9, error:Error}
Result  = {data:any, key:Key | null, seq:int 0.., cursor:int 0.., hints:[str]{0..8}, lines:[str]{0..10000},
           duplicate:bool, exit:int 0..9, head:str | null}
Error   = {code:str/[a-z_]+(\.[a-z_]+)*/, message:str{1..400}, hint?:str{1..400},
           fix?:{argv:[str]{1..32}}, retryable:bool, retry_after_ms?:int 1..3600000}
Evt     = {t:"evt", sub:SubId, cursor:Cursor, items:[Item]{0..64}, end?:EndReason}
Fatal   = {t:"fatal", error:Error}
OpName  = str/[a-z][a-z_]*(\.[a-z_]+)*/      ReqId = str/[A-Za-z0-9_-]{1,32}/
```

- **`Result`** is `orch.ops.Result` in full. The CLI renders text from it with its own templates, so P1's golden
  output is unchanged.
- **`Error`** is the F1 §10.4 item 4 body plus the optional `retry_after_ms`. The `exit` member of an error `Res` is
  the CLI exit code (§0.6).
- **Malformed requests.** A `req` with an unknown key, a missing required key or a wrong type answers `req.invalid`
  when its `id` is readable, and is `fatal` `frame.invalid` otherwise. `args` that fail the op's schema answer
  `invalid.input`, as in P1.
- **`files`.** Every argument the registry marks `x-file: true` (for example `artifact add PATH`, `--file`) must come
  through `files`. Such an argument in `args` carries only its logical name (the artifact name, or `-` for stdin), and
  an `x-file` argument without its `files` entry answers `blob.invalid`. The host never treats a client string as a
  path (§0.10).
- **`{digest}`** is accepted only on a **retry**: a write whose `(principal, idem)` already has an outcome record
  (§0.7). It names the file by the digest the record holds, because the blob may already be consumed. Without a
  record, `{digest}` answers `blob.invalid`.
- **A `{digest}` is never resolved to bytes (M1).** It is only ever compared with the record's binding, never looked up
  in blobs, artifacts or anything else. If the op has to run again (no stored result, for example after
  reconciliation removed a `pending` record), a `{digest}` file answers `blob.invalid` and the client uploads again.

### 0.4 Capabilities and versioning

- **`proto`.** The host picks the highest major version it shares with `hello.proto`. If none is shared:
  `fatal` `host.version_mismatch`.
- **`caps`.** Named features with a version. The host lists what it serves, and a client must not call an op whose cap
  it did not see.
- **`ops`.** The host's `welcome.ops` lists every op it serves with its contract version. A client lists the ops it
  will call; an op it omits is assumed to be at version 1.
- **The v1.2 baseline.** v1.2 is the first contract that per-op versions count from. Nothing earlier was
  implemented, so every op starts at version 1, except `task.done` at 2 because it changed from the P1 registry
  shape.

| Cap | Ops (contract version) | Sockets |
|---|---|---|
| `ops/1` | every registry op (1), except `task.done` (2); `whoami` (1); `host.status` (1) | workspace |
| `query/1` | `ticket.query`, `ticket.get`, `needs`, `workspace.show`, `member.list`, `session.list`, `grant.list` (all 1) | workspace |
| `read_ext/1` | no ops of its own: a host that lists it sends **every** optional field that v1.4 adds to `ticket.query`, `ticket.get` and `needs` (§0.8) | workspace |
| `activity/1` | `activity.list` (1) | workspace |
| `diff/1` | `diff`, `artifact.get` (1) | workspace |
| `human/1` | `human.prepare`, `human.get`, `human.submit`, `human.pending`, `human.cancel` (1) | workspace |
| `sub/1` | `sub.open`, `sub.close` (1) | workspace |
| `blob/1` | `blob.put`, `blob.drop` (1) | workspace |
| `outcome/1` | `outcome.get` (1, workspace socket); `host.outcome` (1, control socket) | workspace, control |
| `job/1` | `job.next`, `job.heartbeat`, `job.done` (1) | worker |
| `control/1` | `host.list`, `host.register`, `host.unregister`, `host.wsk_create`, `host.bootstrap`, `host.restart` (1) | control |


  - A call to an op the host does not list answers `unknown_command`.
  - A call to an op listed at a different version than the client's answers `op.unsupported`. Other ops keep working.
  - A version is bumped only when the op's input, output or errors change incompatibly.
  - **v1.4 adds only optional keys** to results that existed in v1.3 (a client ignores unknown keys, so an older client is
    unaffected), so `ticket.query`, `ticket.get` and `needs` stay at version 1. A client that wants the new fields checks
    for the cap `read_ext/1`; a host without it may omit them, and the client then shows the field as unknown, not as zero
    or empty. `activity.list` is a new op at version 1.
- **`registry`** is a diagnostic digest. Nothing is refused because of it.
- **Forward compatibility.**
  - Clients ignore unknown keys in `welcome`, `res`, `result.data`, `evt` and its items, `fatal`, and `Error`; an unknown
    `EndReason` counts as `closed`.
  - The host refuses unknown keys everywhere it reads, because inputs are security-relevant.

### 0.5 Principals, classes, permissions and visibility (authoritative)

**Principal (F4).** Derived once per request from the kernel UID and the grant, never from an id alone.

| Request | Principal |
|---|---|
| `auth.grant` present and its secret verified (id, constant-time secret match, not expired or revoked, person a member who may run agents) | `g:<grant id>@<uid>` |
| anything else (no grant; a malformed, unknown, expired or wrong-secret grant) | `uid:<n>` |

- `auth.session` is a label, not authentication.
- Session notes, dedup records and the stop rule are keyed by `(principal, session)`. Outcome records (§0.7),
  subscriptions (§0.9), blobs (§0.10) and human requests (§0.8) are keyed by principal.

**Classes.** The kernel UID decides the class. The host's registry (§1, D-P2-5) maps each UID.

| Class | UIDs | Notes |
|---|---|---|
| `person` | UIDs mapped to a person (on a Mac: the human's own UID, i.e. the CLI and the dashboard) | The mapping is checked again **on every request** (F8): the person must be a member with a valid device that has `decide`. Otherwise the caller reads as `no-person` and cannot call `human.*`. |
| `agent` | the workspace's agent UIDs (#287) | acts for its grant's person |
| `worker` | the worker UID, on the worker socket only | **one** worker connection per worker UID (F3), and only from the service manager's worker process (N1). At connect the host reads the peer PID (`LOCAL_PEERPID` on macOS, `SO_PEERCRED.pid` on Linux) and compares that process's PID and start time with the main PID of the worker unit: on Linux systemd's `MainPID` over D-Bus; on macOS `launchctl print system/io.orch.worker`, or, if that cannot be read without root, a root-owned pid file written by the setup helper's wrapper (H10 confirms which). Any other worker-UID process is `fatal` `peer.denied`, also while the role is vacant. While a worker connection is live, a second one is `peer.denied` too. |
| `control` | the host UID, on the control socket only | |
| `bridge` | reserved for P3 | |
| any other UID | — | `fatal` `peer.denied` before any byte is read |

**Tier A** (no agent UIDs configured, D-P2-5): every workspace connection is person class. A request with a verified
grant runs in **agent mode** (the "agent, grant" column below). Any request with `auth.grant` present cannot call
`human.*` (`human_only`).

**Effective column.** Each request resolves to exactly one column and one viewer, by class, grant state and person
mapping. *Unverified* covers a malformed, unknown, expired, revoked or wrong-secret grant, and a grant of a person who
may no longer run agents.

| Class | `auth.grant` | Person mapping (§0.8.3, rechecked per request) | Column | Viewer | Writes |
|---|---|---|---|---|---|
| person | absent | valid | **P** | the mapped person | as column P |
| person | absent | invalid (not a member, or no valid `decide` device) | **N** for reads | `no-person` | **every write answers `op.not_allowed`** (L1: a removed member's UID keeps no unattended writes); `human.*` answers `op.not_allowed` |
| person | verified | either | **G** | the grant's person | as column G; `human.*` answers `human_only` |
| person | unverified | either | **N** for reads | `no-person` | every write answers `grant.required` (malformed) or `grant.expired` (unknown, expired, revoked, wrong secret), unattended writes included (F1 §10.4 item 12); `human.*` answers `human_only` |
| agent | absent | — | **N** | `no-person` | as column N |
| agent | verified | — | **G** | the grant's person | as column G |
| agent | unverified | — | **N** for reads | `no-person` | as for person + unverified |
| worker | must be absent (`req.invalid` otherwise) | — | **W** | — | job ops only |
| control | must be absent (`req.invalid` otherwise) | — | **C** | — | control ops only |

**Permission matrix.** Columns:

- **P** = person class, valid mapping, no grant;
- **G** = a verified grant (agent class, or person class in agent mode);
- **N** = no verified grant and no valid person (unattended);
- **W** = worker;
- **C** = control.

| Ops | P | G | N | W | C |
|---|---|---|---|---|---|
| registry `read` ops except `doctor`, `instructions.sync`, `instructions.hook` | ✓ | ✓ | ✓ | — | — |
| `doctor`, `instructions.sync`, `instructions.hook` | ✓ | — | — | — | — |
| registry `agent` ops | — | ✓ (grant verbs) | — | — | — |
| registry `unattended` ops (`ask`, `log`, `artifact.add`) | ✓ (as an unattended actor, quotas; P1 parity) | ✓ | ✓ (quotas) | — | — |
| registry `human` ops as a plain `req` | `human_only` | `human_only` | `human_only` | — | — |
| `human.prepare`, `human.get`, `human.submit`, `human.pending`, `human.cancel` | ✓ | — | — | — | — |
| `whoami`, `host.status`, `outcome.get`, `blob.put`, `blob.drop` | ✓ | ✓ | ✓ | `whoami` only | — |
| `sub.open`, `sub.close`, `ticket.query`, `ticket.get`, `needs`, `activity.list`, `diff`, `artifact.get`, `workspace.show`, `member.list` | ✓ | ✓ | ✓ | — | — |
| `session.list`, `grant.list` | ✓ | ✓ (own person's only) | — | — | — |
| `job.next`, `job.heartbeat`, `job.done` | — | — | — | ✓ | — |
| `host.list`, `host.register`, `host.unregister`, `host.wsk_create`, `host.bootstrap`, `host.outcome`, `host.restart` | — | — | — | — | ✓ |
| `policy.set`, `settings.set`, `move` and the human registry ops | through `human.*` only (owners, or owners and maintainers for `move`) | — | — | — | — |


A call outside the matrix answers `op.not_allowed`. The one exception is human registry ops called as a plain `req`,
which answer `human_only`. `host.status` gives class N only `version` and `tier`.

**Viewer.** Every read is filtered for one viewer:

- P: the mapped person;
- G: the grant's person;
- N, or P with an unmapped or invalid person: `no-person`, which sees `visibility: workspace` tickets only
  (F1 §10.7).

**Visibility for every read (F6).**

- Every row, count, page token, `needs` item, session claim, grant, diff, artifact and subscription item is filtered
  for the viewer **before** it is counted or returned.
- `total` and `next_page` count visible rows only, and `q` matches visible content only.
- A ref the viewer may not see gives the same `not_found` as a ref that does not exist, in `show`, `ticket.get`,
  `diff`, `artifact.get` and `sub.open {ticket:<ref>}`.
- `needs {person}` naming another person is allowed only for a person-class owner. For G it is always the grant's
  person; for N, `workspace` items only.
- `diff {repo}` accepts only a repo named in the ticket's `links.repos`.
- `artifact.get {name}` resolves `name` (F1 §6 name rule) through the ticket's replayed manifest and reads by digest.
  It never joins a client string to a path.
- A visible ticket may name the key of a ticket the viewer cannot see: `Row.parent`, `Row.epic`, `closed.duplicate_of`, and
  `duplicate_of` in the `data` of a `ticket.closed` log or feed item. Nothing else about the named ticket is returned: no
  title, status or `restricted` flag, and every op given that key answers `not_found` as for a key that does not exist
  (N5; restricted is not secret, F13). A client shows such a key as plain text; it may offer a link only after a read of
  that key succeeded.

**Derived fields (v1.4).** Every field that v1.4 adds follows the rules above. In addition:

- **Computed after the filter.** A count, a flag, a claim, a question, a decision, an artifact or any other derived value
  is computed only for a ticket that passed the viewer's filter, and only from that ticket's own logs. No value is ever
  computed across tickets the viewer cannot see, so a restricted ticket the viewer may not see contributes nothing to any
  row, `total`, `needs` item, count or feed item (F6).
- **`restricted`** is present on a row only when the viewer may see the ticket, and then it is `true` exactly when the
  ticket's `visibility` is not `workspace`. It is never present on a ticket the viewer cannot see, and it does not carry
  the list of persons: only `ticket.get` does, in `doc` (F1 §3), for a viewer who may see the ticket. A `no-person`
  viewer sees `workspace` tickets only, so `restricted` is always `false` for it.
- **What is returned is what F1 stores.** Nothing is derived from git beyond the `branch.pushed` events (no commit
  messages, authors or file lists: `diff` is the op for those), and nothing from a previous text of a section (F1 keeps
  hashes in the log, not old texts). No `sig`, `host_sig`, `secret_hash`, key, grant secret or signing byte is returned.
  A grant is named by its public id only.
- **The activity feed** (`activity.list`) filters each item on its own, as stated there.

- `session.list` and `grant.list`: a person-class owner sees every entry. Any other P sees their own. G sees its own
  person's. Claims on tickets the viewer cannot see are left out.

**Secrets.** The grant secret goes in `auth.grant` only. If a grant-shaped string appears anywhere in `args`,
`files` or `opts`, the request answers `grant.secret_in_args`.

### 0.6 Errors and their disposition (authoritative)

Codes from P1 (`orch.ops.errors`, F1 §10.4a) keep their exit codes and retryability. The host adds the codes below.

**Connection fate:**

- **stays**: the connection remains usable;
- **close**: sent as `fatal`, then the connection is closed; `conn.overloaded` has this disposition.
  If a write has already partially sent a frame, close directly because appending a fatal would corrupt framing;
- **drain**: the connection stays only until the drain ends.

| Code | Exit | Retryable | Fate | When | `retry_after_ms` |
|---|---|---|---|---|---|
| `frame.invalid` | 6 | no | close | §0.2 framing rules; an unreadable `req` id; reuse of an open id | — |
| `frame.too_large` | 6 | no | close | `n` > 2 MiB | — |
| `conn.overloaded` | 9 | yes | close | slow reader write deadline (10 s), write queue over 64 frames or 4 MiB, or connection byte budget exhausted | — |
| `internal` | 1 | no | stays for a request whose handler failed or ended without an answer; close (as `fatal`, no request id) when the session itself failed outside any request | an unexpected host error | — |
| `handshake.invalid` | 6 | no | close | missing, late or second `hello` | — |
| `peer.denied` | 3 | no | close | unmapped UID; second worker connection | — |
| `host.version_mismatch` | 1 | no | close | no shared `proto` | — |
| `req.invalid` | 6 | no | stays | malformed `req` envelope with a readable id | — |
| `unknown_command` | 2 | no | stays | op not served (any op) | — |
| `op.unsupported` | 1 | no | stays | op contract version differs | — |
| `op.not_allowed` | 3 | no | stays | outside the §0.5 matrix | — |
| `timeout` | 9 | yes | stays | read or `wait` past its timeout | — |
| `cancelled` | 9 | yes | stays | §0.2 cancellation | — |
| `outcome.unknown` | 9 | yes | stays | a write's fate cannot be reported now; recover with `outcome.get` | — |
| `rate.limited` | 9 | yes | stays | token bucket empty | yes |
| `quota.host` | 9 | yes | stays (close, as `fatal` with no request id, at connect for the connection limit and for connection-attempt throttling: 20 per second, burst 40, per UID) | connections, requests in flight, subscriptions, pending human requests, blobs, outcome records, git reader children over their limit (§0.11) | when a slot frees in time |
| `result.too_large` | 1 | no | stays | the result would exceed the frame; the hint names the paging argument | — |
| `host.restarting` | 9 | yes | drain | the host is draining (D-P2-4) | yes |
| `workspace.unavailable` | 1 | no | stays | the workspace cannot be served; the message names the cause | — |
| `workspace.degraded` | 3 | no | stays | a write while the workspace is degraded (reads work) | — |
| `idem.required` | 5 | no | stays | a write without `opts.idem` | — |
| `idem.conflict` | 5 | no | stays | the same `idem` with a different binding (§0.7) | — |
| `idem.expired` | 2 | no | stays | the `idem` is only a tombstone (§0.7) | — |
| `outcome.unknown_idem` | 2 | no | stays | `outcome.get` for no record of this principal | — |
| `blob.invalid` | 5 | no | stays | §0.10 rules; an unknown or foreign blob | — |
| `page.expired` | 9 | yes | stays | page token older than 10 min, or from another snapshot epoch; restart from the first page | — |
| `sub.reset` | 9 | yes | stays | cursor too old, of another scope or epoch, or a journal rebuild (§0.9) | — |
| `human.request_expired` | 3 | no | stays | request older than 10 min | — |
| `human.request_mismatch` | 5 | no | stays | the submitted events differ from the prepared ones (§0.8) | — |
| `human.request_state` | 3 | no | stays | the request is cancelled, already bound to another device, or already submitted by another call | — |
| `host.required` | 3 | no | stays | a direct-mode CLI on a registered workspace (D-P2-18) | — |
| `lock.busy` | 9 | yes | stays | register waited 60 s for the ownership lock (existing code; exit 9 on the socket, while P1's CLI catalog keeps exit 4 for its own store lock: H4b maps the socket's exit as sent) | yes |
| `host.busy` | 4 | no | stays | a second host on the same state or run directory (at start); `register` or `bootstrap` of a root or id that is already registered, has a genesis, or is being bootstrapped (N5) | — |
| `worker.lease` | 3 | no | stays | `job.*` with a lease that is unknown, expired or of another connection, or a `job.done` that differs from the stored completion (§0.8.2) | — |
| `control.rolled_back` | 9 | yes | stays | a stored answer only: a control intent rolled back at host start (§0.7); retry with a new idem | — |
| `repo.remote_mismatch` | 3 | no | stays | the observed identity differs from the declared `remote` (H0) | — |

`host.unavailable` (exit 9, retryable) is raised **only by the client**, when it cannot connect or the connection
drops. The host never sends it.

**Universal errors.** These may come from any op, beyond the op's own list:

- every op: `req.invalid`, `unknown_command`, `op.unsupported`, `op.not_allowed`, `human_only` (human registry ops,
  and `human.*` with a grant), `invalid.input`, `grant.secret_in_args`, `host.restarting`, `rate.limited`,
  `quota.host`, `timeout`, `cancelled`, `result.too_large`, `internal`;
- every op on a workspace socket: `workspace.unavailable`;
- every write (§0.7): `idem.required`, `idem.conflict`, `idem.expired`, `outcome.unknown`, `grant.required`,
  `grant.expired`, `workspace.degraded`;
- every request with `files`: `blob.invalid`.

**Precedence.** The host checks in this order, and the first failure is the answer:

1. framing and handshake (`fatal`);
2. the envelope (`req.invalid`);
3. the op: `unknown_command`, then `op.unsupported`;
4. drain (`host.restarting`);
5. `grant.secret_in_args`; then, for a request whose `auth.grant` is present but **unverified**, a write that column G
   would allow answers `grant.required` (malformed) or `grant.expired` (otherwise). This is the effective-column rule
   of §0.5, and it wins over the column check;
6. the column (§0.5): `op.not_allowed` or `human_only`;
7. `rate.limited`, `quota.host`;
8. `args` against the schema (`invalid.input`), then `files` (`blob.invalid`), then `dry_run` support
   (`invalid.input`);
9. idempotency (not for `dry_run:true`, §0.7): `idem.required`, then a replay of a stored result, `idem.conflict` or
   `idem.expired`;
10. the workspace state: `workspace.unavailable`, then `workspace.degraded` for writes;
11. the op's own errors (its list in §0.8, in the order its handler meets them);
12. `timeout`, `cancelled` and `outcome.unknown` can end a request at any point after step 11 started; `internal`
    at any point.

**Per-op errors** (beyond the universal ones):

| Op | Errors |
|---|---|
| registry ops | their declared `errors` (core §3), unchanged |
| `whoami`, `host.status`, `workspace.show`, `member.list`, `session.list`, `grant.list`, `sub.close`, `blob.drop`, `host.list`, `host.restart` | none |
| `ticket.query` | `page.expired`, `not_found` (`parent`, `epic`) |
| `ticket.get` | `not_found`, `ambiguous_ref` (never an error for a visible closed or done ticket, §0.8.4) |
| `needs` | none (another person's needs is `op.not_allowed`) |
| `activity.list` | `page.expired`, `not_found` (`ticket`), `invalid.input` (a `page` token used with other arguments than the first page's; `ticket` with `scope:"workspace"`; `since` after `until`) |
| `diff` | `not_found`, `observe.unavailable`, `repo.remote_mismatch` |
| `artifact.get` | `not_found` |
| `outcome.get`, `host.outcome` | `outcome.unknown_idem` (no record, or another principal's), `idem.expired` (only a tombstone is left) |
| `human.prepare` | `device.unknown`, `device.invalid`, `device.scope`, `observe.unavailable`, the inner op's declared errors |
| `human.get` | `not_found`, `human.request_expired`, `human.request_state`, `device.unknown`, `device.invalid`, `device.scope`, `observe.unavailable` (N3 re-check) |
| `human.submit` | `not_found`, `human.request_expired`, `human.request_state`, `human.request_mismatch`, `sig.invalid`, `gate.stale`, `members.stale`, `question.stale`, `observe.unavailable`, the inner op's model codes |
| `human.pending` | none (an unknown code gives an empty list) |
| `human.cancel` | `not_found` |
| `sub.open` | `not_found` (`ticket:<ref>`), `sub.reset` |
| `blob.put` | none beyond `blob.invalid` |
| `job.heartbeat`, `job.done` | `worker.lease` |
| `job.next` | none |
| `host.wsk_create` | none |
| `host.register` | `host.busy` (root already registered), `lock.busy`, `trust.genesis_mismatch`, `custody.unsafe_mode`, `op.not_allowed` (peers map) |
| `host.unregister` | `not_found` |
| `host.bootstrap` | `host.busy` (id already has a genesis or a registration, N5), `not_found` (id not from `wsk_create`), `custody.unsafe_mode`, the genesis model codes |

`custody.unsafe_mode` (exit 3, not retryable, stays): the root or an ancestor fails the D-P2-17 checks.


### 0.7 Idempotency and outcomes

**Write categories.** Every op falls into exactly one row. "Recovery" says what makes a retry safe after a crash or a
lost response; "Lookup" says how a client asks for a result it never received.

| Category | Ops | `opts.idem` | Recovery | Lookup |
|---|---|---|---|---|
| **log writes** | registry ops whose `who` is not `read`; `human.submit` | required | an outcome record, reconciled from the log by its planned event ids (below) | `outcome.get` (workspace socket) |
| **control writes** | `host.register`, `host.unregister`, `host.bootstrap` | required | an intent record in `<state>/host/intents/<idem>` (0600), fsynced before the change and completed after it; replayed at host start | `host.outcome` (control socket) |
| **job completion** | `job.done` | not used | a persisted completion record per job (§0.8.2); a repeat is compared with it canonically | none needed (the repeat is the lookup) |
| **non-idempotent create** | `host.wsk_create` | not used | none: a retry creates a new key, and a key with no genesis after 1 h is deleted | none |
| **reads and side-effect-free ops** | reads; `human.prepare`, `human.get`, `human.pending`, `human.cancel`; `blob.put` (idempotent by offset and `upload_key`, §0.10), `blob.drop`; `sub.*`; `outcome.get`, `host.outcome`; `job.next`, `job.heartbeat`; `host.list`, `host.status`, `host.restart` | ignored | none needed | none |

**Dry run.** `opts.dry_run:true` is accepted only by the log-write registry ops and by `human.prepare`.

- The op is judged with `model.preview`. Nothing is appended, consumed or created, and no outcome record is written.
- The result is the op's normal result with `seq: 0` (for `human.prepare`, `request: null`).
- **`opts.idem` is ignored on a dry run:** it is not required, not looked up and not stored. A dry run never replays a
  stored result, even when a record exists for the same key, and never answers `idem.*`.
- Every other op called with `dry_run:true` answers `invalid.input` ("dry_run is not supported by <op>"). That
  includes reads, `human.submit`, control, job, blob and sub ops.

**Binding** (log and control writes). A record is keyed by `(namespace, principal, idem)` and bound to:

```
sha256(cj({op, args_normalised, files:{arg: Hash}, session}))
```

- The namespace is the workspace id on a workspace socket, and `control:<uid>` on the control socket.
- `args_normalised` is `args` with REFs resolved to the ticket uid and defaults filled in from the schema.
- `files` holds each file's digest. The record keeps these digests **apart from the blobs**, so a retry after the blob
  was consumed names the file with `files.<arg>: {digest}` (§0.3) and the binding can still be computed.
- The same key with another binding answers `idem.conflict`.

**States** (log writes).

| State | Meaning | A retry with the same key |
|---|---|---|
| `pending` | written (fsync) before `Store.append`. It holds the planned event ids. | waits for the first call to finish, then gets its result |
| `committed` | holds the `result`, redacted | gets it, with `duplicate:true` |
| `failed` | a non-retryable refusal; holds the `error` | gets the same error |
| `partial` | a multi-event **registry** call (`apply`, `handoff`, `task.done`) whose first events landed; lists their `seq`s. Never used for `human.submit`, which rolls forward (§0.8.1). | the stored `error`: `invalid.input` "an earlier attempt … was interrupted after N event(s)", exactly as P1's `apply` refuses (F1 §10.7) |

- **Re-enveloping.** A record stores `result` or `error` and `exit`, never a frame. A replay is sent in a fresh `res`
  carrying the **current** request id.
- **Retryable failures** (`store.busy`, `timeout` before any append) store nothing, and the retry runs again.
- **Concurrent duplicates** (the same key while the first call runs) wait for it and get its result.

**Reconciliation.** A crash can leave the append in the log while the record still says `pending`. The host
reconciles every `pending` record at start and at its next lookup, under the store lock:

- all planned event ids are in the verified log: the record becomes `committed`, and the result is rebuilt from the log
  through the op's result builder;
- some are: `partial`;
- none are (with no lock holder left): the record is removed, and the retry runs again.

The planned event ids are the store's idempotency ids (F1 §2.1), so this check is exact.

**Control intents.** Before `host.register`, `host.unregister` or `host.bootstrap` changes anything, the host writes
`{idem, binding, op, args_normalised, step:"started"}` (fsync). After the change it records the terminal state with
its result. At start, a `started` intent is finished or rolled back, and **either way it ends in a durable terminal
state**:

| Op | Found at start | Done | Terminal state | `host.outcome {idem}` and a same-key retry answer |
|---|---|---|---|---|
| `register` | registry entry written | serving starts | `committed` | `{state:"committed", result:{workspace_id}}` |
| `register` | no registry entry | nothing to undo | `failed` | `{state:"failed", error:{code:"control.rolled_back", retryable:true}}`; the client retries with a **new** idem |
| `unregister` | any | serving stops, entry removed | `committed` | `{state:"committed", result:{}}` |
| `bootstrap` | the genesis is in the new log (its planned event id) | registration finished | `committed` | `{state:"committed", result:{workspace_id, seq:1}}` |
| `bootstrap` | no genesis | the half-made root, its key directory and the workspace id are removed | `failed` | `{state:"failed", error:{code:"control.rolled_back", retryable:true}}` |

- **A rolled-back bootstrap restarts from `host.wsk_create`.** The id and key are gone, and the genesis signs both
  `workspace_id` and `wsk_pub`, so the client gets a new id and key, signs a new genesis, and calls `host.bootstrap`
  with a new idem. The CLI's `init` composite does this by itself after one confirmation on the person's terminal,
  because the device key signs again.
- `control.rolled_back` (exit 9, retryable, connection stays) is only ever a stored answer. A same-key retry gets it
  again; it never runs the op a second time.
- A same-key retry of a `committed` intent gets the stored result with `duplicate:true`.

**Retention.** Outcome records live in host-owned `<state>/hosts/<ws>/outcomes/`, and control intents in
`<state>/host/intents/`. Each is kept 24 h, then becomes a tombstone `{key, binding, state}` for 7 days. A tombstone
answers `idem.expired`, never a second append, and `outcome.get` on it answers `idem.expired` too. At most 100 000
records per principal; past that, the oldest completed records become tombstones early.

**Session dedup.** F1 §10.4 item 9 (15 minutes, `duplicate:true`) stays on top as the CLI-level behaviour.

### 0.8 Operations

**Registry ops.** Every registry op of `orch.ops` is served under its name, with its `input` schema as `args` (the
parsed CLI arguments), its handler and its `Result`. The changes from P1:

- `who: human` ops answer `human_only` as a plain `req`. They run through `human.*`.
- `task.done` is at **contract 2** (H3b2). `run:true` is refused by the host. The client runs `verify.cmd` itself and
  sends:

  ```
  receipt?:{cmd:str{1..4096}, exit:int −255..255, duration_ms:int 0..86400000, commit:str/[0-9a-f]{40,64}/ | null,
            repo:str{1..200} | null, output:"-"}
  ```

  `output` comes through `files`, at most 1 MiB. H3b2 (client receipts) adds `attested_by: "agent"` to the stored
  receipt (D-P2-19); the current receipt has no such field.
- `import.v1` and `init` are not served (D-P2-9). `import.v1` runs in direct mode before registration. `init` is the
  `host.wsk_create` + `host.bootstrap` composite.
- `help` and `describe` are answered by the client from its own registry. The host serves them too.
- The `show` data is unchanged, so the goldens stay byte-identical. UIs use `ticket.get` (R1).

**New ops.**

`whoami {}` →

```
{peer:{uid:int, class:str}, person:Person | null, tier:"A"|"B",
 grant:{id:str, until:Time, verbs:"agent" | [OpName], scope:"all"|"workable"} | null, session:str | null}
```

Errors: none beyond any op.

`ticket.query` (read) args:

```
{status?:[Status]{1..8}, type?:[str]{1..16}, label?:[str]{1..16}, assignee?:Person, owner?:Person,
 parent?:Ref, epic?:Ref, q?:str{1..200}, updated_since?:Time, needs_me?:bool,
 sort?:"updated"|"priority"|"key" = "updated", order?:"asc"|"desc" = "desc", limit?:int 1..200 = 50, page?:str{1..512}}
```

→

```
{rows:[Row]{0..200}, next_page:str | null, total:int, sub_cursor:Cursor, as_of:Time}
Row = {key:Key, uid:Uid, title:str, type:str, status:Status, priority:str | null, size:str | null, labels:[str],
       assignees:[Person], owner:Person | null, parent:Key | null, epic:Key | null,
       claim:Claim | null, gates:{Gate: "open"|"approved"|"n/a"}, needs:[NeedKind],
       updated_at:Time, head_seq:int,
       restricted?:bool, tasks?:{done:int, skipped:int, total:int}, criteria?:{done:int, total:int},
       blocking_questions?:int, closed?:Closed}
Claim  = {session:str, agent:str, for?:Person, since?:Time, live?:bool}
Closed = {resolution:"wont_do"|"duplicate"|"obsolete"|"other", duplicate_of:Key | null, at:Time, by:Person}
```

- Filters combine with AND. The values inside one filter combine with OR.
- `q` is a case-insensitive substring match (NFC, casefold) over the key, the title and the section texts.
- The sort is stable. Ties break by key ascending.
- `needs_me` keeps the rows with a need whose `who` contains the viewer.
- **Closed tickets (§0.8.4).** With no `status` filter the result holds every status **except `closed`**. A `status` that
  lists `closed` includes closed tickets, alone or together with others (`["closed"]` is the "closed" list).
- **Row fields added in v1.4** (all optional keys, sent by a host with `read_ext/1`; the visibility rules of §0.5 apply):
  - `claim` is the ticket's current claim record, or `null` when it has none. `agent` is the claiming agent's id (the
    `actor.id` of its `claim.taken`), `for` the person it works for, `since` the `at` of that `claim.taken`, and `live`
    is `false` for a claim that has lapsed (F1 §5.9: no event for `claim_ttl_min`, or its grant ended) but whose
    `claim.released` has not yet been appended. The session id is the same public id as in `session.list`.
  - `restricted`: §0.5.
  - `tasks` counts the ticket's tasks by their derived state: `done` is the tasks in state `done`, `skipped` those in
    state `skipped`, `total` all tasks. `criteria.done` is the acceptance criteria that have evidence by the F1 §6 rule
    (a file artifact added with a grant or by a person naming the criterion, or a done task that proves it with a
    receipt of `exit` 0 on the current source commit), and `total` is all criteria. A ticket with none has `total: 0`.
  - `blocking_questions` is the number of questions in state `open` with `blocking: true`.
  - `closed` is present exactly when `status` is `"closed"`: `resolution` and `duplicate_of` come from the latest
    `ticket.closed` since the last `ticket.reopened`, `at` is its time and `by` its signer. `duplicate_of` may name a ticket the viewer cannot see (§0.5).
- The first page fixes a **snapshot**: its `sub_cursor` and `as_of`. Later pages carry the snapshot and the last row's
  sort key and uid, and they read the state **at that snapshot's journal position**. Only the paging position changes,
  so a row neither repeats nor goes missing across pages, except a row whose ticket became invisible to the viewer.
- **Page tokens** (`next_page`, here and in `activity.list`) are opaque and authenticated by the host (a MAC under a
  host-only key, or a host-side record). A token binds the workspace, the principal hash, the history epoch, the snapshot
  position, the paging position and a digest of the first page's other arguments. The checks run in this order:
  authenticity, then principal, then expiry, then arguments. A token that fails the check, or was issued to another
  principal, is `page.expired`; a valid token of the caller used with other arguments is `invalid.input`. Tokens expire
  after 10 min (`page.expired`). **Every page is filtered for the viewer of its own request** (§0.5, rechecked per
  request, so a revoked grant or a removed person changes the viewer at once), and a row is returned only when its
  ticket is visible to that viewer both at the snapshot and now.
- `total` counts visible matching rows in the snapshot.

Errors: `invalid.input`, `page.expired`, `not_found` (`parent` or `epic`).

`ticket.get {ref:Ref, log_since?:int 0.. = 0, log_limit?:int 1..500 = 100}` (read, R1) →

```
{doc:TicketDoc, log:[LogItem], log_more:bool,
 questions:[Question],
 criteria:[Criterion],
 gates:{Gate:GateView},
 sub_cursor:Cursor,
 // v1.4, all optional keys (sent with read_ext/1):
 status?:Status, settled?:bool,
 closed?:{resolution:str, duplicate_of:Key | null, at:Time, by:Person, text:str | null},
 tasks?:[Task], verdict?:Verdict | null,
 artifacts?:[Artifact]{0..1000}, artifacts_more?:bool,
 claim?:ClaimFull | null,
 source?:[{name:str, repo:str, ref:str, sha:str}], pushes?:[Push]{0..50}, pushes_more?:bool,
 sections?:{str: [SectionRev]{0..20}}, sections_more?:{str:bool}}

LogItem  = {seq:int, type:str, at:Time, actor:Actor, summary:str{0..200}, data?:LogData}
Question = {id:str, state:"open"|"answered"|"stale", to:[Person], blocking:bool, answered_by:Person | null, answered_seq:int | null,
            text?:str, why?:str, to_ref?:str, options?:[{key:str, label:str, cost?:str}], recommended?:str,
            asked_at?:Time, asked_by?:Actor, asked_seq?:int,
            answer?:{by:Person, at:Time, seq:int, option:str | null, text:str | null}}
Criterion = {id:str, text:str, evidence:[{artifact:str, digest:Hash, kind:str, attested_by:"agent"|"person"|"unattended"}],
             covered?:bool, tasks?:[{id:str, evidence:bool}]}
GateView  = {state:"open"|"approved"|"n/a", gen:int, hash12:str/[0-9a-f]{12}/ | null,
             applies?:bool, waiting?:bool, blocked?:bool, count?:int, approved_by?:[Person], eligible?:[Person],
             policy?:Policy, missing?:[str], decisions?:[Decision]{0..100}, decisions_more?:bool, revoked_decisions?:[ULID]}
Decision  = {id:ULID, kind:"approved"|"changes_requested"|"pass"|"fail", person:Person, at:Time, seq:int, gen:int,
             counting:bool, voided:bool, auth?:str, text?:Snip, source_sha?:[Source]}
Task      = {id:str, text:str, state:"open"|"done"|"skipped"|"blocked", proves:[str], leased_by:str | null,
             by:Actor | null, at:Time | null, reason:str | null, receipt:Receipt | null}
Receipt   = {cmd:str, exit:int, ms:int, repo:str | null, commit:str | null, log:str | null, seq:int, at:Time,
             evidence:bool, attested_by?:"agent"}
Verdict   = {outcome:"pass"|"fail", person:Person, at:Time, seq:int, gen:int, text:Snip | null, source_sha:[Source],
             current:bool, counting:bool, voided:bool, auth?:str}
Artifact  = {name:str, kind:str, digest:Hash | null, bytes:int | null, ac:str | null, task:str | null, label:str | null,
             addon?:str, ref?:str, evidence:bool, by:Actor, at:Time, seq:int, replaced:int}
ClaimFull = {session:str, agent:str, for:Person, grant:str, since:Time, last_at:Time, live:bool, lapsed:str | null,
             takeovers:[{at:Time, session:str, from_session:str, reason:str}]{0..20}, takeovers_more?:bool}
Push      = {seq:int, at:Time, repo_name:str, repo_id:str, ref:str, sha:str, before:{repo_id:str, ref:str, sha:str} | null}
SectionRev = {seq:int, at:Time, hash:Hash | null, actor:Actor, via:"edit"|"external", refs:[str]}
```

- `TicketDoc` is the F1 §7 ticket document.
- `summary` is host text, escaped as in F1 §10.4 item 11. The content itself is fetched with `show --log`.
- **Closed and done tickets** are returned like any other (§0.8.4): `status` is `"done"` or `"closed"`, `settled` is `true` for
  both, and `closed` (present only when `status` is `"closed"`) is `Closed` of `ticket.query` plus the `text` of the
  `ticket.closed` event, in full, or `null`.
- **`LogItem.data`** is a structured copy of the event's own fields, for the Overview. It is closed per type
  (table below); a type not in the table, including any future one, has `data: {}`. It holds no signature, no hash
  chain value and no secret. `Snip` fields are cut as defined in §0.1.

  | Event type | `data` |
  |---|---|
  | `ticket.created` | `{ticket_type, title, owner}` |
  | `ticket.updated` | `{paths:[str], sections:[str]}`: the leaf paths in `set` and the section ids, never the values |
  | `status.changed`, `ticket.moved` | `{from, to, reason?}` |
  | `ticket.submitted`, `handoff.written` | `{}` |
  | `ticket.closed` | `{resolution, duplicate_of?, text?:Snip}` |
  | `ticket.reopened` | `{text?:Snip}` |
  | `visibility.changed` | `{restricted:bool}` |
  | `people.changed` | `{role, add:[Person], remove:[Person]}` |
  | `policy.changed` | `{gates:[Gate]}`: the gates it names, not their policies |
  | `claim.taken` | `{takeover?:{from_session, reason}}` |
  | `claim.released` | `{session, reason}` |
  | `task.started` | `{task}` |
  | `task.reopened` | `{task, reason?}` |
  | `task.done` | `{task, receipt?:{cmd, exit, ms, repo, commit}, log?, text?:Snip}` |
  | `task.skipped`, `task.blocked` | `{task, reason}` |
  | `log.added` | `{text:Snip}` |
  | `question.asked` | `{question, blocking, to}` |
  | `question.answered` | `{question, option?, text?:Snip}` |
  | `gate.approved` | `{gate, gen}` |
  | `gate.changes_requested` | `{gate, gen, text:Snip}` |
  | `verdict.given` | `{outcome, gen, text?:Snip, source_sha:[Source]}` |
  | `gate.invalidated` | `{gate, cause, voided:[ULID]}` |
  | `branch.pushed` | `{repo_name, repo_id, ref, sha, before_sha:str | null}` |
  | `artifact.added`, `artifact.replaced` | `{name, kind, digest?, bytes?, ac?, task?, label?, addon?, replaces?}` |
  | `edit.external` | `{sections:[str], voided_gates:[Gate], normalised:bool}` |
  | `projection.repaired` | `{path, cause}` |
  | `restore` | `{from_seq, reason}` |
  | `invalid.acknowledged` | `{invalid_seq}` |

- Text in `data` (`text`, `reason`, `label`, `title`) is the actor's stored content, not host text: it is not escaped like
  `summary`, and a client renders it as untrusted text attributed to `actor`. A `Snip` field `f` has its `f_cut` sibling.
- **`questions`.**
  - `text`, `why`, `options` (each `{key, label, cost?}`) and `recommended` are the question as the last `question.asked`
    for its id defines it (F1 §5.4.1). `to_ref` is the stored `to` (a person id or a ticket role), while `to` stays the
    resolved addressees.
  - `asked_at`, `asked_by` and `asked_seq` come from that same event, so a re-ask with changed text restarts them.
  - `answer` is present exactly when `state` is `"answered"`, from the `question.answered` that counts. `option` and `text`
    are as signed. The host never returns an answer that belongs to an older question hash (that question is `stale`).
- **`criteria`.** `covered` is the F1 §6 evidence test for the criterion, and `tasks` lists the tasks that name it in
  `proves`, with whether each task's receipt counts as evidence now.
- **`tasks`.** One entry per task in `ticket.json` order, with the state the model derives. `by`, `at`, `reason` and `receipt`
  come from the latest `task.*` event for the task (`null` for a task never touched). `leased_by` is the session
  holding an active lease (F1 A4), else `null`. `receipt` is the stored receipt of the latest `task.done`, shown as stored,
  with `seq` and `at` of that event, `log` the name of the log artifact if any, and `evidence` whether it counts for
  F1 §6 now (exit 0 and the commit equals the current source `sha` of its `repo`, or `repo` is `null`). `attested_by` is
  present only when H3b2 stored it (D-P2-19).
- **`gates`.**
  - `applies`, `blocked` (no eligible approver token, `gate.no_eligible`), `waiting` (the gate waits for a person's
    decision now), `count` (the quorum needed) and `policy` (the effective policy in canonical form, F1 §5.7) come from
    the model. `approved_by` lists the persons whose approvals count now, so `approved_by` has fewer than `count`
    entries while the gate is open. `eligible` is the persons who may decide now and can see the ticket (§0.8, `needs`). `missing` is what an approval of
    `requirements` or `plan` still lacks (section ids, `acceptance`, `tasks`), and is empty otherwise.
  - `decisions` is every decision event on the gate (`gate.approved`, `gate.changes_requested`, `verdict.given`), oldest
    first, at most the newest 100. `counting` is true when the decision counts now; `voided` when F1 §5.7 retired it.
    `kind` is `approved` for `gate.approved`, `changes_requested` for `gate.changes_requested`, and `pass` or `fail` for
    `verdict.given`. `auth` is the custody label (F1 §5.3, metadata and not proof). `source_sha` is present on `verify`
    and `code` decisions. `revoked_decisions` is the ids of a settled ticket's decisions of a device revoked as
    compromised that still count (F1 §5.7).
- **`verdict`** is the latest `verdict.given` on the ticket, or `null`. `current` is true when its `gen` equals the verify
  gate's current generation and it counts; a verdict from before a reopen or a new commit is returned with
  `current:false`.
- **`artifacts`** is the replayed manifest (F1 §6), ordered by name, with its `evidence` flag. `digest` and `bytes` are `null`
  for an addon artifact, which has `addon` and `ref` instead of a file. `by` marks an unattended artifact
  (`unattended: true`), which is never evidence. `replaced` is the number of `artifact.replaced` events for the name. The
  content comes from `artifact.get`. `artifacts_more` is true when the ticket has more than 1 000 entries; the first
  1 000 by name are returned.
- **`claim`** is `Claim` of `ticket.query` in full: `since` is the `at` of the `claim.taken`, `last_at` the `at` of the
  session's latest event on the ticket (the claim lapses `claim_ttl_min` after it), `lapsed` is a claim release reason
  (F1 §11.4) for a claim that no longer lives but is not yet released, else `null`, and `takeovers` lists the most
  recent 20 takeovers of this ticket with their stored reason.
- **`source`** is the F1 §5.7 source list as the latest `branch.pushed` per linked repo projects it, with the repo `name`.
  `pushes` is the newest 50 `branch.pushed` events, oldest first. Commit messages and authors are not stored by F1 and are
  not returned (`diff` reads git).
- **`sections`** maps each section id that has been edited to its edits, oldest first, at most the newest 20: the section
  hash after the edit, who made it, whether it was an `edit` (`ticket.updated`/`section.set`) or an `edit.external`, and the
  artifact names the section refers to. The hash is `null` when the edit removed the section. **F1 stores only hashes, not old
  texts**, so a previous text cannot be shown; the current text is in `doc`.
- **Cut lists.** `decisions_more`, `pushes_more`, `sections_more` (present for the section ids that were cut) and
  `takeovers_more` are `true` when older entries were left out; they are absent otherwise. Together with `log_more` and
  `artifacts_more` no list is cut silently.
- `log_more`, `log_since` and `log_limit` are unchanged. If the whole result would pass the frame limit the host answers
  `result.too_large` and the hint names `log_limit`.

Errors: `not_found`, `ambiguous_ref`.

`needs {person?:Person}` (read) →

```
{items:[NeedItem], sub_cursor:Cursor}
NeedItem = {kind:"question"|"approve"|"no_eligible"|"acknowledge"|"addon", key:Key | null, uid:Uid | null,
            ref:str | null, detail:str, who:[Person],
            since?:Time, text?:Snip, status?:Status, payload?:NeedPayload}      // the last three: v1.4, read_ext/1
NeedPayload = {question:str, blocking:bool, to:str, options:[{key:str, label:str, cost?:str}], recommended?:str, why?:str}   // question
            | {gate:Gate, gen:int, count:int, have:int}                                                                    // approve
            | {gate:Gate, cause:"no_token"|"no_visible_person"}                                                           // no_eligible
            | {scope:"ticket"|"workspace"}                                                                                 // acknowledge
            | {addon:str, rule:str}                                                                                        // addon
```

- **Core owns the computation:** `orch.model.needs` over the verified state. Addons only *declare* rules (C9
  `needs_rules`), which core evaluates over addon fields. No addon code runs.
- Addon runtime decisions come with H10 jobs and are out of `needs` in v1.1.
- **The five kinds.** `key` and `uid` are `null` only for a workspace-level item. `who` is always the persons who can act
  now (sorted). `detail` is the model's short text, unchanged.

  | `kind` | `key` / `uid` | `ref` | `detail` | `who` | `text` | `payload` |
  |---|---|---|---|---|---|---|
  | `question` | the ticket | the question id (`Q1`) | `"blocking"` or `""` | the question's current addressees (the `to` person, the holders of the `to` role, or owners and maintainers when it resolves to no member) | the question text, `Snip` | `question` (the id again), `blocking`, `to` (the stored person id or role), `options`, `recommended?`, `why?` |
  | `approve` | the ticket | the gate (`requirements`, `plan`, `verify`, `code`) | `""` | the persons eligible to decide the gate now | absent | `gate`, `gen` (its generation), `count` (the quorum), `have` (persons whose approvals count now, fewer than `count`) |
  | `no_eligible` | the ticket | the gate | `""` | the persons who can see the ticket and may sign its `policy.changed` or `visibility.changed` (the ticket owner, workspace owners and maintainers, F1 §5.4.2) | absent | `{gate, cause:"no_token"\|"no_visible_person"}`. The effective policy of that gate leaves no approver token (F1 §5.7, `gate.no_eligible`); an owner fixes it with `policy.set`. |
  | `acknowledge` | the ticket, or `null` for the workspace log | `null` | `"invalid event in the ticket log"` or `"invalid event in the workspace log"` | the workspace owners | absent | `{scope}`: `"ticket"` or `"workspace"`. An unacknowledged invalid event freezes person decisions there (F1 §12 R5); an owner lifts the freeze with a signed `invalid.acknowledged`. No op for it is served yet (F1 §10.8 lists it as not built). |
  | `addon` | the ticket | `<addon>.<rule id>` | `""` | the holders of the rule's `who` tokens on that ticket who can see it (F1 §8.1) | the rule's `text` (at most 120 characters, F1 §8.1) | `{addon, rule}` |

- **`since`** is the `at` of the event whose append made the need hold, counted from the latest time it began to hold
  without a break: for a question, the `question.asked` that defined its current text (a re-ask with changed text
  restarts it); for `approve`, the event after which the gate waits (for example the submit or a generation raise that
  made it ready); for `no_eligible`, the event that emptied the approver set; for `addon`, the event after which the rule's
  expression became true. It is absent for `acknowledge` (the invalid event is not part of the verified log and its
  `at` is not trusted) and for any need whose beginning the host cannot derive. A client treats absent as unknown.
- **`status`** is the ticket's status, so a client can mark an item whose ticket is `closed` (§0.8.4). It is absent for a
  workspace-level item.
- **Visibility.** The items are those of `person` (or of the viewer when `person` is absent), filtered for the **caller's**
  viewer (§0.5): an item is returned only when its ticket is visible to the caller and, with `person`, also to that person.
  `text`, `since` and `payload` exist only on items that survived. A workspace-level `acknowledge` is returned only to a
  viewer in its `who`. An owner who is not on a restricted ticket's list therefore gets none of another person's items on it.
- **`who` and `eligible` list only persons who can see the ticket (F1 §9)**, for every kind. A gate whose approver tokens are held by
  nobody who can see the ticket is reported as `no_eligible` with `payload.cause: "no_visible_person"`; a gate with no
  approver token at all (F1 §5.7, `gate.no_eligible`) has `cause: "no_token"`. `GateView.blocked` stays F1's token test.
  A `question` whose addressees are all filtered out falls back to the owners and maintainers who can see the ticket.

Errors: `op.not_allowed` (another person's needs, §0.5).

`activity.list` (read, new in v1.4, cap `activity/1`) args:

```
{scope?:"all"|"tickets"|"workspace" = "all", ticket?:Ref, types?:[str/[a-z]+(\.[a-z_]+)*/]{1..16},
 actor_kind?:["person"|"agent"|"host"|"addon"]{1..4}, person?:Person, since?:Time, until?:Time,
 order?:"desc"|"asc" = "desc", limit?:int 1..200 = 50, page?:str{1..512}}
```

→

```
{items:[ActivityItem]{0..200}, next_page:str | null, sub_cursor:Cursor, as_of:Time}
ActivityItem = {id:ItemId, jpos:int, log:Uid | "workspace", key:Key | null, uid:Uid | null, title:str | null,
                seq:int, ws_seq:int, type:str, at:Time, actor:Actor, summary:str{0..200}, data:LogData}
```

- **What it lists.** Events of the verified logs in the merged order (F1 §5.5), newest first by `jpos`. It reads the logs,
  not only the subscription journal, so history is not limited to the 10 000-entry replay window of §0.9. An
  `ActivityItem` is the subscription `Item` of the same event (`id`, `jpos`, `log`, `key`, `uid`, `seq`, `ws_seq`, `type`,
  `at`) plus `actor` (`Actor`), `title` (the ticket's current title, `null` for the workspace log), `summary` and `data`
  as in `ticket.get` (`LogItem`). For a workspace-log event `ws_seq` equals its `seq`.
- **Filters** combine with AND; the values of `types` and `actor_kind` combine with OR. `scope` limits to ticket-log
  or workspace-log events. `ticket` names one ticket (a ref the viewer cannot see is `not_found`, as in `ticket.get`).
  `person` keeps events whose actor is that person or an agent acting `for` that person. `since` is inclusive and `until`
  exclusive, both on `at`.
- **Visibility, per item, before it is counted or returned (§0.5).**
  - A ticket-log event is returned only when its ticket is visible to the viewer at the snapshot **and** at the time of the
    request. A ticket that was restricted when the event happened but is `workspace` at both shows its whole history, as
    `ticket.get` does. A ticket that becomes invisible between pages contributes nothing to later pages, and one that
    becomes visible between pages adds nothing to the snapshot. Visibility is the ticket's visibility **now** (at the
    snapshot and at the request), never its visibility when the event was appended: F1 stores none per event. A viewer who
    may see a ticket now gets its whole history, including any period when it was restricted, exactly as `ticket.get` and
    `show --log` give it; a ticket the viewer may not see now contributes nothing, whatever it was before. `title`, `key`, `summary` and `data` of an event on a
    ticket the viewer cannot see are never computed.
  - A workspace-log event is returned only to a viewer with a person (P, or G for the grant's person), never to
    `no-person`, and only by type:
    - to every such viewer, whatever the person's member role (a `viewer` gets the same items as a `member`: each carries
      no more than `member.list`, `workspace.show` and `ticket.get` already give that person, per the table below): the
      member and role events (`workspace.created`, `member.added`, `member.removed`, `role.changed`), the device events
      (`device.added`, `device.removed`, `device.revoked`), `policy.changed`, `settings.changed`, `addon.granted`,
      `addon.disabled`, `addon.purged`, `restore` and `invalid.acknowledged`;
    - `grant.issued` and `grant.revoked`, whatever the role, only to a person-class owner and to the grant's own person
      (the `grant.list` rule).

    Their `data` is closed per type, as in `LogItem.data`; a type not in this table, including any future one, has
    `data: {}`:

    | Event type | `data` |
    |---|---|
    | `workspace.created` | `{owner:Person}` |
    | `member.added` | `{person, role}` |
    | `member.removed` | `{person}` |
    | `role.changed` | `{person, role}` |
    | `device.added`, `device.removed` | `{person, device}` |
    | `device.revoked` | `{person, device}` (no `reason`, no `revocation`) |
    | `policy.changed` | `{gates:[Gate]}` |
    | `settings.changed` | `{keys:[str]}` (the names in `set`, never repo paths or remotes) |
    | `grant.issued` | `{grant, person, scope, expires_at}` |
    | `grant.revoked` | `{grant, person}` |
    | `addon.granted`, `addon.disabled`, `addon.purged` | `{name}` (`addon.granted` adds `version`) |
    | `restore` | `{from_seq}` |
    | `invalid.acknowledged` | `{invalid_seq}` |

    None carries a public or private key, a certificate, a signed object, `secret_hash`, a free-text `reason` or `label`.
  - **No `total`.** `next_page` is `null` only when no further visible item exists. A page holds `limit` items, or fewer
    when the host's scan budget for one request (at least 10 000 events) ran out, and then `next_page` continues from where
    the scan stopped. Invisible events are skipped, never returned as holes. `jpos` gaps still reveal that invisible
    events happened, which is the accepted metadata of F13.
- **Paging.** As in `ticket.query` (§0.8, page tokens): the first page fixes a snapshot (`sub_cursor`, `as_of`) and later
  pages read the logs up to that position. A `page` token is authenticated by the host and bound to the principal, epoch,
  snapshot, paging position and the first page's other arguments, and **every page is filtered for the viewer of its own
  request**, with the visibility rule above. With `order:"asc"` the first page starts at the oldest event that matches.
- **Live tail.** `sub.open {topics:["tickets","workspace"], after:<sub_cursor>}` delivers the events after the snapshot,
  with the same ids, so a client dedupes by `id` (§0.9).

Errors: `invalid.input`, `page.expired`, `not_found` (`ticket`).

`diff {ref:Ref, repo?:str, context?:int 0..10 = 3, max_bytes?:int 1024..1048576 = 262144, since?:"my_approval"}`
(read) →

```
{sources:[{repo:str, branch:str, base_sha:str, head_sha:str,
           base_source:"host_pinned"|"local_ref"|"first_parent"|"approved", base_moved:int,
           commits:[{sha:str, subject:str}], commits_more:int}],
 files:[{repo:str, path:str, old_path:str | null, status:"A"|"M"|"D"|"R"|"T", add:int, del:int, binary:bool,
         too_large:bool}],
 unified:str, truncated:bool}
```

- **head** is the branch head the host reader observes, which is the `source_sha` a verdict would sign (D58).
- **base** is chosen in this order, and `sources[].base_source` says which rule chose it:
  1. `host_pinned`: the merge base the host recorded the first time it observed this repo for this ticket (host state,
     never moved forward automatically), while it is still an ancestor of head;
  2. `local_ref`: the merge base of head and the repo's `default_branch` in the working copy (`settings.repos`; if
     none, `main`, then `master`). The working copy is agent-writable, so a client labels it "base chosen by the
     working copy (unverified)". When a `host_pinned` base exists and is an ancestor of this one, `base_moved` is the
     number of the branch's commits between them;
  3. `first_parent`: no merge base exists (or no default branch exists and none is declared): head's first parent,
     labelled "only the last commit". A declared `default_branch` that does not exist is `observe.unavailable`, and so
     is a head with no parent.
  An optional `since:"my_approval"` argument instead diffs from the newest **code-gate approve** decision by the
  caller whose signed `source_sha` names this repo identity and is an ancestor of head (`base_source:"approved"`);
  without such a decision it is `not_found`. A verdict's `source_sha` is never a base.
- `sources[]` also carries `commits:[{sha, subject}]` for `base..head` (newest first, at most 50, `commits_more:int`),
  as display only; subjects are cleaned of control and bidi characters.
- A client that prompts for a code approval shows, per repo, `base_sha`, `base_source`, `base_moved` when non-zero,
  and the commit list. The approval itself still signs only the head `source_sha` (D58); the diff is a display.
- **H3b1 implements** `local_ref` and `first_parent` (`base_moved` is always 0) and the commit list. `host_pinned`
  and `since:"my_approval"` are follow-ups (§9 Q9). A base the caller chose (`declared`, a later option) is never the
  approval screen's default: the approving client shows it with its sha and whether it is an ancestor of head, and
  the signing path never takes a base from the agent's side.
- **Too large to diff.** A file whose side is over 8 MiB or 200 000 lines, or that would take the request past
  1 000 000 diffed lines (both sides of every file), is not diffed as text: its `files` entry has `too_large:true`,
  `binary:true`, `add` and `del` 0, and `unified` carries `diff --git a/<p> b/<p>` followed by
  `Too large to diff: <reason>`. The check is made from the blob sizes and line counts before any line is split.
- **Untrusted text.** `files[].path`, hunk bodies and function headers are repository content: they may contain
  control and bidi characters (the `diff --git`, `---` and `+++` names are C-quoted as git quotes them, the rest is
  raw). Clients must render them escaped.
- Renames are not detected in v1.4: `status` is never `R` and `old_path` is always `null` (a rename is a deletion and
  an addition; a file/symlink/gitlink type change is `T`, patched as a deletion then an addition). The `R` value and
  `old_path` stay in the schema for a later version.
- When there are several merge bases, the chosen one is the one with the newest committer time, then the smallest
  id. A detached HEAD gives `null` (no branch) in host mode.
- `unified` is cut at `max_bytes` on a line boundary, and `truncated` says so.

Errors: `not_found`, `observe.unavailable`, `repo.remote_mismatch`, `quota.host`.

`artifact.get {ref:Ref, name:str{1..200}, offset:int 0.., length?:int 1..786432 = 786432}` (read) →

```
{b64:B64, size:int, digest:Hash, offset:int, last:bool}
```

`offset` = `size` returns an empty `b64` with `last:true`. `offset` > `size` is `invalid.input`.

Errors: `not_found`, `invalid.input`.

`workspace.show {}` (read) →

```
{id:str, prefix:str, name:str, genesis:Hash, epoch:int, wsk_fingerprint:str, members_v:int, state:"ok"|"degraded"}
```

`epoch` is the committed epoch, never the highest allocated one (§11, membership publication).

`member.list {}` (read) →

```
{members:[{person:Person, name:str, role:"owner"|"maintainer"|"member"|"viewer",
           devices:[{id:Device, auth:str | null, scopes:[str], valid:bool}]}]}
```

`session.list {active?:bool = true}` (read) →

```
{sessions:[{session:str, agent:str, person:Person, grant_id:str | null, claims:[Key], leases:[str], last_at:Time}]}
```

`grant.list {active?:bool = true}` (read, R2) →

```
{grants:[{id:str, person:Person, scope:"all"|"workable", verbs:"agent" | [OpName], issued_at:Time, expires_at:Time,
          revoked_at:Time | null, sessions:int}]}
```

The grant id is public (F1 §10.7). `secret_hash` is never returned.

`blob.put`, `blob.drop`: §0.10. `sub.open`, `sub.close`: §0.9.

`outcome.get {idem:Idem}` →

```
{state:"pending"} | {state:"committed"|"failed"|"partial", result?:Result, error?:Error, exit:int}   // never a stored frame
```

Errors: `outcome.unknown_idem` (also for another principal's record, F4).

`host.status {}` →

```
{version:str, tier:"A"|"B", uptime_s?:int, state?:"ok"|"degraded"|"unavailable", worker?:bool}
```

Class N gets only `version` and `tier`.

**Human ops** (`human.*`, §0.8.1), **job ops** (§0.8.2), **control ops** (§0.8.3): below.

**Person versions of agent ops (signed).** `new`, `set`, `section.set`, `ac.add`, `ac.edit`, `log`, `ask`,
`artifact.add` and `artifact.replace` run as a person through `human.prepare {op, args}`. The rule is exact: allowed
when every event type the op emits passes `orch.ops.actors.allows("human", t)` (F1: an A type may be appended by a
signed person). Other agent ops answer `op.not_allowed`.

**`move` (F1 `ticket.moved`).**

- A person op, owner or maintainer, through `human.prepare`: `{ref:Ref, to:"backlog"|"open", reason?:str{1..500}}`.
- It emits `ticket.moved {from, to, reason?}`.
- Allowed from `backlog`, `open`, and from `in_progress` with no live claim. Never into `in_progress`, `testing`,
  `done` or `closed`.

**`policy.set` and `settings.set`.** Person ops, owner only (F1 §5.4.2), run through `human.prepare`. Each emits one
existing F1 event, so no format change is needed. H0 records them as ops.

```
policy.set   {ref?:Ref, gates:{Gate: Policy}{1..4}}          → policy.changed {gates}   (ticket log with ref, else workspace log)
settings.set {set:{grant_hours?:int 1..24, claim_ttl_min?:int 15..1440, lease_ttl_min?:int 5..1440,
                   repos?:{str/[a-z0-9][a-z0-9._-]{0,63}/: {path:str{1..1024}, remote?:str{1..512},
                                                           default_branch?:str{1..255}} | null}{1..32}}}
                                                              → settings.changed {set}
Policy = {approvers:[str{1..32}]{1..8}, count:int 1..9, not:[str{1..32}]{0..8}, applies:"all"|"off"|[str{1..32}]{1..16},
          independent:bool}                                   (F1 §5.7; code: not ⊇ ["assignees"], independent:true)
```

**Reserved, not served:** `comment`, `ticket.discard`, `artifact.search`, `view.save`, `view.delete`. Saved views are
the dashboard addon's own data.

#### 0.8.1 Human ops (D-P2-15, D-P2-21, D-P2-22)

The host never prompts and holds no device key. Every person event is prepared by the host, signed by a signing client
(the CLI, `orch sign`, or the H9 helper), and submitted.

```
human.prepare  {op:OpName, args:object, signer?:Signer, handoff?:bool = false}   + req.files for the inner op (R9)
  → Prepared
human.get      {request_id:ReqHr, signer?:Signer} → Prepared
human.submit   {request_id:ReqHr, events:[SignedEvent]{1..16}}                     (a log write: opts.idem required)
  → the inner op's Result
human.pending  {code?:Code, request_id?:ReqHr}                                     (at most one of the two)
  → {requests:[{id:ReqHr, code:Code | null, op:OpName, key:Key | null, created_at:Time, expires_at:Time,
                state:State, event_ids:[ULID], seq:int | null}]}                  (R6; seq of the last event once submitted)
human.cancel   {request_id:ReqHr} → {state:"cancelled"|"submitted"}
Prepared = {request:{id:ReqHr, code:Code | null, expires_at:Time, state:State} | null,      // null only for dry_run
            op:OpName, workspace_id:WsId, log:Uid | "workspace", events:[Event]{1..16}, material:Material,
            review_digest:Hash, fingerprint:str/[0-9a-f]{12}/,
            origin:{uid:int, pid:int, client:str{1..64}, created_at:Time},
            checks?:[{repo:str, clean:true, attested_by:"worker", at:Time}]}               // not signed, not in material
State    = "prepared"|"bound"|"submitted"|"cancelled"|"expired"
ReqHr    = str/hr_[0-7][0-9A-HJKMNP-TV-Z]{25}/          Code = str/[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}/
Signer   = {device:Device, auth:"passphrase"|"secure-enclave"}
Material = {kind:"gate", gate:Gate, input:<orch.model.gates.gate_input>, ticket:{key:Key, uid:Uid}, review:[Review]{1..16}}
         | {kind:"question", ticket:{key:Key, uid:Uid}, question:<the question object>, review:[Review]{1..16}}
         | {kind:"payload", ticket:{key:Key, uid:Uid} | null, review:[Review]{1..16},
            texts?:{str: str},                          // section id → full text, for every section the events reference
            before?:{str: any}}                         // prior values of the paths a set/section.set/settings.set changes
Review   = {event_id:ULID, type:str, payload:object, derived:{str: any}}
```

**The canonical review payload (exact).** For every event, `Review` is computed by `orch.host.review.of(event,
material)` and recomputed by the signer, which refuses any difference:

- **`payload`** = the event **minus** the envelope keys `v`, `id`, `type`, `actor`, `auth`, `hash_v`, `roster_v`,
  `based_on`, `gate_gen` and `sig`, verbatim and complete. Every payload key is shown, including keys a future F1
  version adds (L2). Nothing can be signed without being shown.
- **`derived`** holds values the signer computes itself from the event and `material`. It is never taken from the
  host. The per-type table is frozen in H5a:

| Event type | `derived` |
|---|---|
| `grant.issued` | `{}`; the review shows `secret_hash` as "drawn by this signer" |
| `member.added`, `workspace.created` | `{cert_device, cert_person, cert_scopes_max, cert_expires}` from the embedded `device_cert` (L2) |
| `device.added` | the same, from `cert` |
| decisions (`gate.approved`, `gate.changes_requested`, `verdict.given`) and `question.answered` | `{ticket_key}` from `material.ticket` |
| `ticket.updated`, `ticket.created`, `question.asked` | `{sections:{id: {text, hash}}}`: each section the event references by hash, with its text from `material.texts`. The signer checks `section_hash(text) == hash` (F1 §5.6) and refuses a mismatch. |
| every other type | `{}` |

- **Order.** Events are reviewed in event order, one block each. Inside a block the payload keys are shown in
  canonical (JCS) order, after a one-line summary the signer builds from `type` and `derived`.
- **`before`.**
  - For `set` and `section.set`, each prior value is checked against the event's `base_rev` hash (F1 §5.8).
  - For `settings.set`, `settings.changed` has no `base_rev`. Its values are absolute, and the signed `set` is the
    whole effect. So `before` is shown labelled **"current values according to the host, not signed"**, and the signer
    verifies only that each `before` key is a key of `set`.
  - A `before` key the signer cannot check by either rule is refused.
- `review_digest = sha256(cj(material))`.

**Fingerprint (N2).** The first 12 hex digits of:

```
sha256(cj({"review_digest": review_digest, "events": [each event without sig, actor.device, auth and,
           for grant.issued, secret_hash]}))
```

It covers the op, its outcome and every payload field, so `verdict pass` and `verdict fail` differ. The dashboard,
`orch sign` and H9 show it, and each signer recomputes it from what it received (R8).

**States.**

| From | Event | To |
|---|---|---|
| — | `human.prepare` with `signer` | `bound` |
| — | `human.prepare` with `handoff:true` | `prepared` |
| `prepared` | `human.get` with `signer`: device checked, events completed, `tree.check` re-run (N3) | `bound` |
| `bound` | `human.get` with the same `signer`: `tree.check` re-run | `bound` |
| `bound` | `human.submit` committed | `submitted` |
| `prepared`, `bound` | `human.cancel` | `cancelled` |
| `prepared`, `bound` | 10 minutes after prepare, or found expired at append | `expired` |

- `human.get` **without** `signer` returns the request in any state.
- `human.get` **with** `signer` in `submitted` or `cancelled`, or with another device than the bound one, answers
  `human.request_state`; in `expired` it answers `human.request_expired`.
- `human.submit` on an unbound request answers `human.request_state`.
- Requests live in `<state>/hosts/<ws>/requests/` and survive a host restart. At most 20 are pending per person.
  Terminal states are kept 24 h.

**Invariant bytes.** From prepare to get to submit these never change: `workspace_id`, `log`, `material`,
`review_digest`, `fingerprint`, `origin`, and every byte of `cj(event)` except three fields:

- `actor.device` and `auth`: `null` → a value, once, at binding (a request prepared with `signer` has them from the
  start);
- the `secret_hash` of a `grant.issued` event: `null` at prepare, set by the signer in `human.submit`;
- `sig`: added in `human.submit`.

`checks` may change at each binding.

**Rules.**

- **Event ids are host-prepared**, drawn from a CSPRNG (H0 amends F1 §5.1). A signer changes no field except the
  three above.
- **Multi-event ops** (for example a person's `artifact.add` with evidence) prepare all their events in one request.
  They share one `based_on` (the log head at prepare; F1 allows any earlier head), and they are signed one by one.
  - **Judged together.** At submit, all events are judged together with `model.preview` under one store lock, then
    appended one by one under that same lock.
  - **Crash recovery rolls forward.** Every signature already exists, so a crash between two appends is completed at
    host start, before anything is served: the remaining signed events, which are stored with the request, are judged
    and appended. The request then becomes `submitted`, and its outcome `committed`.
  - **When rolling forward fails.** If a remaining event is refused at that point (only possible in tier A, after a
    foreign append), the request ends `submitted` with outcome `failed`. The stored error names the `seq`s that landed
    and the refusal code. A human submission is therefore never `partial`.
- **`dry_run`** (§0.7): `request: null`, nothing stored, no code, no `tree.check` job.
- **Device binding (R7).**
  - Without `handoff`, `signer` is required.
  - With `handoff:true`, `signer` is absent and the events carry `actor.device: null` and `auth: null` until
    `human.get` binds them.
  - Binding checks that the device belongs to the mapped person and has a valid certificate with `decide` (and
    `operate` where needed).
- **The grant secret (F1).** The secret is drawn by the process that signs, never by the one that prepares.
  - `human.prepare {op:"grant"}` takes no `secret_hash`. The prepared `grant.issued` carries `"secret_hash": null`,
    and `material` says so.
  - The signer draws the 32-byte secret, puts its hash into the event, and builds the signing bytes from that event.
  - Every signer refuses a prepared `grant.issued` whose `secret_hash` is not `null`.
  - The signer shows `ORCH_GRANT=…` on its own terminal or window only after `human.submit` confirms the commit.
  - The preparer (the dashboard, a hand-off creator) never learns the secret.
- **What the signer does** (D-P2-21, N4):
  1. **Shape first:**
     - `op` is a person op the signer knows;
     - every event's `type` is in `EMITS_V1[op]` (the frozen table, F1 §10.7) and allowed for a person;
     - the number and order of the events fit that op;
     - all events have one `log`, one `based_on`, and the `actor.id` of the signer's person;
     - no `grant.issued` unless `op` is `grant`.

     Then recompute every bound hash from `material` (the gate hash from `gate_input`, the question hash), the
     `Review.fields` of every event, `review_digest` and `fingerprint`, and compare each.
  2. Render its own review from `material` and the events, never from host text. It opens with the header below and
     lists **every event, one block each**. For `payload` material the blocks are the events' review fields, which are
     their signing bytes. `checks` are shown as **attested by the worker, best effort**.
  3. Get the op-specific confirmation word: the ticket key for decisions, `GRANT` for grants, the person id for member
     ops.
  4. Build the signing bytes itself (`identity.sign_person_event`), show the prompt fields from those bytes, sign, and
     keep everything frozen from confirmation to signature.
- **The header** is built by the signer before any ticket content:
  - the workspace name and prefix from the signer's **own** registry for `workspace_id` (never host text);
  - the op in plain words with its decisive fields;
  - the `origin` (kernel UID and PID recorded by the host, and the self-declared `client`, labelled unverified);
  - the fingerprint.
- **Expiry** is checked again at append, under the lock.
- **`human.submit`.**
  - Compares `cj(event without sig)` with each bound event, with only the `secret_hash` of a `grant.issued` event set
    aside. A difference is `human.request_mismatch`. For a grant, the hash must be well-formed and new in the
    workspace.
  - Verifies every signature, runs the prepared `precommit` (the D58 branch check under the lock, D-P2-19), and
    re-judges with the model. Anything stale gets `gate.stale`, `members.stale` or `question.stale`.
  - Submit and cancel are serialized per request.
  - **Replay precedence.** The generic idempotency step (§0.6 step 9) comes **first**:
    1. no `opts.idem`: `idem.required`;
    2. a record for `(principal, idem)` with the same binding: its stored answer (`duplicate:true` on success);
    3. a record with another binding: `idem.conflict`;
    4. only a tombstone: `idem.expired`;
    5. a **fresh** idem: the request-level rules below.

    The request-level rules, in order:
    - `not_found`;
    - a `submitted` request whose stored signed events equal the submitted ones (canonical, signatures included): the
      request's stored terminal answer, unchanged. That is its result with `duplicate:true` when its outcome is
      `committed`, or its stored error (exit and retryable included) when its outcome is `failed`, for example after a
      failed roll-forward. **A fresh outcome record is written for this idem with the same outcome state and the same
      answer**, so a later retry with the same idem replays it at step 2 and `outcome.get` reports the same state;
    - `human.request_state` (submitted with other events, cancelled, unbound);
    - `human.request_expired`;
    - the checks above.

    Every answer from the request-level rules is stored under the fresh idem as usual: `committed`, or `failed` for a
    non-retryable refusal.
  - Blobs used by the inner op are held with the request.
- **No separate `expect` argument is needed.** The events carry the gate hash, `gate_gen`, `source_sha` and the
  question hash, each recomputed by the signer from the material it displays. **The dashboard renders its preview from
  `material` and `fingerprint`, never from its cached ticket view.**
- **Hand-off (F5).**
  - The 8-character code (40 bits) **names** the request. It is not a secret and authorises nothing.
  - `human.pending {code}` finds only the same person's requests. Failed lookups are limited to 5 per minute **per
    connection**.
  - `orch sign CODE` resolves the code with `human.pending`, then fetches and binds with `human.get`. It never calls
    `human.prepare` a second time.
  - **In tier A any process of the person's UID can create a request**, so `orch sign` says "only sign a code you
    created yourself" every time it runs.

#### 0.8.2 Job ops (worker socket, H10)

```
job.next      {wait_ms?:int 0..30000 = 0}
  → {job:{id:ULID, lease:str/[A-Z2-7]{26}/, kind:"addon.call"|"tree.check", workspace_id:WsId,
          principal:str{1..64}, deadline_at:Time, input:AddonCallInput | TreeCheckInput} | null}
job.heartbeat {id:ULID, lease:str/[A-Z2-7]{26}/} → {deadline_at:Time, cancel:bool}
job.done      {id:ULID, lease:str/[A-Z2-7]{26}/, outcome:"ok"|"error",
               result?:AddonCallResult | TreeCheckResult, error?:{code:str{1..64}, message:str{1..400}}} → {}
AddonCallInput  = the F1 §8.1 runner request: {addon:str, grant:ULID, package_sha256:Hash, method:str, params:object}
AddonCallResult = the F1 §8.1 runner response (a proposal)
TreeCheckInput  = {repo:str{1..64}, gitdir:Pin, commondir:Pin, worktree:Pin}
TreeCheckResult = {changed:[{path:str{1..4096}, why:"modified"|"untracked"|"deleted"|"type"|"flag"}]{0..200}, more:bool}
Pin = {path:str{1..1024}, dev:int, ino:int}
```

- **Routing.** One worker connection, on the worker socket, serves every workspace. `job.next` hands out jobs
  round-robin across workspaces, and each job carries its `workspace_id`.
- **Leases.** A lease (128 bits) is bound to the worker connection and lasts 60 s, renewed by `job.heartbeat`.
- **Cancellation.** `cancel:true` asks the worker to stop and answer `job.done {outcome:"error",
  error:{code:"cancelled"}}`.
- **Retries.** A job is retried at most twice, under the same job id with a new lease.
- **Disconnect.** When the worker connection drops, every outstanding lease is void (F3), and the host waits for the
  service manager's restarted worker (N1).
- **Completion records.** The host persists one record per job in `<state>/host/jobs/<id>` (0600) when the job is
  handed out: `{id, workspace_id, kind, lease, state:"leased"|"done"|"void", completion?:{outcome, digest}}`. Here
  `digest = sha256(cj({outcome, result}))` for `ok`, or `sha256(cj({outcome, error:{code, message}}))` for `error`.
  Records are kept 24 h after completion.
- **`job.done` precedence:**
  1. Unknown job id: `worker.lease`.
  2. A `done` record: if the request's `lease` equals the record's lease and its canonical digest equals the stored
     one, the answer is `{}` (a replay). This holds **from any worker connection and across a host restart**, because
     it compares with the persisted record. Any other combination is `worker.lease`.
  3. A `leased` record: the lease must be the record's and be bound to **this** connection. Then the completion is
     stored (fsync) before the answer `{}`.
  4. A `void` record (the lease died with its connection; the job was requeued under a new lease): `worker.lease`.
- When a connection drops, its `leased` records become `void`, and their jobs are queued again under new leases
  (retries count, at most twice).
- **`tree.check`** paths come from the agent's worktree. Every shown path is escaped and rendered with F1 §11.3's rules
  (N3).

#### 0.8.3 Control ops (control socket, F8)

The peer UID must equal the host UID (checked like any socket). `welcome.workspace` is `null`.

```
host.list       {} → {workspaces:[{workspace_id:WsId, prefix:str, root:str, state:"ok"|"degraded"|"unavailable",
                                    tier:"A"|"B", socket:str, person:Person | null}]}          // person: the caller UID's mapping (R11)
host.wsk_create {} → {workspace_id:WsId, wsk_pub:str}                                          // base64url SEC1 P-256
host.bootstrap  {workspace_id:WsId, root:str{1..1024}, events:[SignedEvent]{1..1}, peers:{str/[0-9]{1,10}/: PeerSpec}{1..64}}
                → {workspace_id:WsId, seq:1}                                                    (control write: opts.idem)
host.register   {root:str{1..1024}, peers:{str/[0-9]{1,10}/: PeerSpec}{1..64}} → {workspace_id:WsId}   (control write)
host.unregister {workspace_id:WsId} → {}                                                        (control write)
host.outcome    {idem:Idem} → {state:"pending"} | {state:"committed"|"failed", result?:Result, error?:Error, exit:int}
host.restart    {} → {draining:true}
```

| Op | Rules |
|---|---|
| `host.wsk_create` | The host draws a new workspace id and creates `hosts/<id>/keys/wsk` with `O_CREAT\|O_EXCL` in a fresh 0700 directory. It never takes an id from the caller and never replaces a key. A key with no genesis after 1 h is deleted. |
| `host.bootstrap` | The `init` composite. `events[0]` is the signed genesis `workspace.created` (F1 §5.4.2: `workspace_id`, `prefix`, `host_id`, `wsk_pub`, `owner`, `delegation`, `device_cert`; `roster_v: 0`). Its `workspace_id` and `wsk_pub` must equal what `wsk_create` returned. `root` is a new empty directory owned by the host UID that passes the D-P2-17 ancestor check. The host validates the event with the model (the six genesis checks, F1 §5.11), appends it with that workspace's WSK as `seq` 1, writes the files, pins the genesis and registers the workspace as `host.register` does. **Refused (`host.busy`) when the workspace id already has a genesis or a registration (N5).** |
| `host.register` | The one op besides `bootstrap` that opens a client-named path. `root` is resolved with `realpath`, must be owned by the host UID and pass the D-P2-17 ancestor check. The workspace id comes from its verified genesis and must match the pinned genesis. A UID with a root-owned agent or worker pin can never be mapped as `person`. A `person` mapping must name a member with a valid `decide` device. Takes the ownership lock (D-P2-18). |
| `host.unregister` | stops serving and restores direct mode |
| `host.restart` | D-P2-4 |

#### 0.8.4 Closed and done tickets (v1.4)

F1 keeps closed tickets: `ticket.closed` sets the status `closed` (F1 §5.9), and only `ticket.reopened` leaves it. Reads
treat them as follows.

- **A plain read of a visible closed (or done) ticket always succeeds.** `ticket.get`, `show`, `diff`, `artifact.get`,
  `sub.open {ticket:<ref>}`, `activity.list {ticket}` and `needs` answer normally, with no error and no new code. F1 refuses
  *writes* to such a ticket (`ticket.frozen`, F1 §10.4a: bound edits, `artifact.*`, `task.*`, `claim.taken`, named-role
  `people.changed`); it never refuses a read. A ticket the viewer may not see is still `not_found`, closed or not.
- **`ticket.get`** returns `status: "closed"`, `settled: true` (also for `done`) and the `closed` object (§0.8).
  `doc` is the F1 §7 document and its status field says `closed`. The `log` runs to the `ticket.closed` event, and to
  `ticket.reopened` and later events if it was reopened (when reopened, `status` is `open` and `closed` is absent).
- **`ticket.query`** omits closed tickets unless asked: with no `status` the result has every status except `closed`, and
  `status:["closed"]` (or a list that includes it) returns them. `done` is not excluded. A closed row carries the
  `closed` object, and `gates` as the model derives them for a settled ticket (a gate that was approved stays `approved`).
  `total` and paging count visible matching rows as always.
- **`needs`.** A closed ticket has no `approve` or `no_eligible` item (F1: gates of a settled ticket are not waiting). An
  open question on it is still listed, because F1 does not retire it, and its item carries `status: "closed"`.
- **Subscriptions** keep delivering events of a closed ticket, among them `ticket.reopened`.

### 0.9 Subscriptions

```
sub.open  {topics:["workspace"|"tickets"|"needs"|"ticket:<Ref>"]{1..8}, after?:Cursor} → {sub:SubId, cursor:Cursor}
sub.close {sub:SubId} → {}
Item      = {id:ItemId, jpos:int, log:Uid | "workspace", key:Key | null, uid:Uid | null, seq:int, ws_seq:int, type:str,
             at:Time, actor:{kind:str, id:str | null}}
          | {id:ItemId, jpos:int, uid:Uid, gone:true}
          | {id:ItemId, jpos:int, type:"needs.changed"}
ItemId    = str/[0-9]{1,15}\.[evgn]/        // "<jpos>.e" event (incl. visibility.changed), ".g" gone, ".n" needs.changed
EndReason = "closed"|"grant.expired"|"person.removed"|"overflow"|"reset"|"host.restarting"
SubId     = str/sub_[0-9a-z]{1,16}/
```

- **Principal.** A subscription keeps the opening request's principal and viewer (§0.5).
- **Revalidation before every delivery**, queued items included:
  - the grant must still be verified (not expired or revoked) and the person still a member, and a person-class
    mapping must still hold;
  - each item's ticket must still be visible.

  A failing item is dropped. If the principal fails, the host sends `evt {end:"grant.expired"}` or
  `{end:"person.removed"}` and the subscription ends.
- **`jpos`** is the event's position in the merged order (F1 §5.5) since the genesis. It is deterministic, so it
  survives a restart, because the journal is rebuilt from the logs.
- **Cursors** are opaque, at most 256 bytes. They encode `{workspace, principal hash, history epoch (genesis plus the
  last restore head), jpos}`.
- **Snapshot and resume.**
  - Every snapshot read (`ticket.query`, `ticket.get`, `needs`) returns the `sub_cursor` of the jpos its state was
    read at, taken atomically on the workspace worker.
  - `sub.open {after}` registers the subscription and fixes the live boundary in one worker step, replays the items
    after `after`, then goes live, with no gap.
  - Its `res` is sent before any `evt` of that subscription. For a write from the same connection, its `res` is sent
    before any `evt` carrying its own events.
- **Item identity.** One journal position (one appended event) can yield up to three items for one subscription: the
  event (`.e`), `gone` (`.g`) and `needs.changed` (`.n`). They are told apart by `id`, which is unique per
  subscription and ordered by `(jpos, e < g < n)`. Clients dedupe by `id`, never by `jpos` alone.
- **Delivery and cursor.**
  - Items arrive in `id` order, batched up to 64 per `evt`. All items of one `jpos` go in the same `evt`.
  - An event that matches several topics of one subscription is delivered once.
  - `evt.cursor` points after the last `jpos` that frame completes. A client that stores it and reconnects with
    `after` gets everything later, and duplicates only if it stored an older cursor.
  - A `jpos` whose items are all filtered out still advances the cursor: the next `evt` that is sent carries it.
- **Visibility changes.**
  - A ticket that becomes visible to the viewer yields its event with `type: "visibility.changed"`.
  - A ticket that becomes invisible yields `{id, jpos, uid, gone:true}`, sent only when the ticket was visible to this
    viewer at the previous `jpos`. This is the **one** item exempt from the visibility filter. It carries only the
    uid the subscriber already knew, and every later item about that ticket is filtered as usual.
- **Accepted metadata (F13).** `jpos` and `ws_seq` are global, so their gaps show that invisible tickets changed, and
  `needs.changed` fires for every change to `needs`. Visibility in §0 is a display rule, not secrecy (F1 §9):
  workspace files are group-readable in tier B.
  `jpos` is also contained in `ItemId` (`"<jpos>.e"`) and may be encoded in `Cursor` and page tokens. A client that hides
  `jpos` from a further consumer maps `id`, `sub_cursor` and `next_page` to its own values as well.
- **End.** `evt {end}` closes the subscription:
  - `reset`: a cursor that is too old (outside the 10 000-entry journal), of another scope or epoch, or a journal
    rebuild (D-P2-12). `sub.open` with such a cursor answers `sub.reset` instead.
  - `overflow`: the queue exceeds 256 items or 256 KiB, or a slow reader lags more than 30 s. The client takes a
    snapshot and opens again with `after` set to its `sub_cursor`.
  - `host.restarting` (drain) and `closed` (`sub.close`).

### 0.10 Files and blobs

```
blob.put  {blob?:BlobId, upload_key?:Idem, offset:int 0.., b64:B64, last:bool, size?:int 0..67108864, sha256?:Hash}
          → {blob:BlobId, size:int, received:int}
blob.drop {blob:BlobId} → {}
BlobId = str/b_[A-Z2-7]{26}/
```

- **The host never opens a path a client names** (D-P2-11). File contents come inline (`files.<arg>.b64`) or as a
  blob.
- **Inline.** At most 512 KiB decoded per argument and per request in total (`welcome.limits.inline_bytes`).
- **Uploads.**
  - **Allocation.** The first chunk has no `blob` and carries `size` and a client-chosen `upload_key`. The host
    reserves `size` against the quota and returns a new id (`b_` plus 128 random bits, base32).
  - **Allocation recovery.** If that response is lost, the client resends the first chunk with the same
    `upload_key`. Within the blob's lifetime and for the same principal, the host returns the same `blob` and its
    `received` count, provided the bytes at offset 0 are identical; different bytes answer `blob.invalid`.
  - Later chunks carry the `blob`. Offsets are contiguous from 0, and each chunk decodes to at most 768 KiB.
  - Resending a chunk at an offset already written, with identical bytes, is accepted without effect. Different bytes
    answer `blob.invalid`. `received` tells the client where to continue.
  - `last:true` needs the total to equal `size` and `sha256` (`sha256:` plus 64 lowercase hex digits) to match. After
    that the blob is immutable.
  - **Final-chunk retry.** Resending the last chunk of a completed blob with identical bytes and the same `sha256`
    returns `{blob, size, received: size}` again.
  - **Empty blobs.** `size: 0` is one chunk: `offset: 0`, `b64: ""`, `last: true` and the SHA-256 of the empty string.

- **Ownership and storage (F9).** A blob is bound to its workspace and its principal. Use from another workspace or
  principal gives the same `blob.invalid` as an unknown id. Blobs are stored at `<state>/hosts/<ws>/blobs/<id>` (0600,
  host-owned), and the id is checked against `^b_[A-Z2-7]{26}$` before it is used in a path.
- **Consumption.**
  - A blob is consumed when the write that uses it commits.
  - On a refusal or a dry run it stays available until it expires.
  - An unused or incomplete blob expires 10 minutes after its last chunk, unless a prepared human request holds it (it
    then lives as long as the request).
  - Blobs survive a reconnect by the same principal.
- **Names.** A blob has no file name. The artifact name or other logical name comes from `args` (F1 §6 name rule).
- **Limits.** Quotas count decoded bytes, reserved at the first chunk.
  - 64 MiB per blob.
  - 256 MiB per **UID** across all its principals and workspaces. Blobs held by prepared human requests count against
    the preparing UID.
  - 1 GiB per host, with 256 MiB reserved for person-class UIDs.
  - Over a limit: `quota.host`.

### 0.11 Limits

| What | Limit |
|---|---|
| frame | 2 MiB; the body within 10 s of its length prefix |
| bytes per connection | 64 MiB/min in each direction |
| connections | 32 per UID; host-wide configurable cap, default min(128, soft fd limit / 4) |
| connection attempts | configurable token bucket, default 20/s with burst 40 per kernel UID, before registry lookup |
| requests in flight | 8 per connection |
| subscriptions | 8 per connection, 64 per UID |
| pending human requests | 20 per person |
| request rate | token bucket of 20/s, burst 100, per agent UID |
| git reader children | 4 per host, 1 per principal at a time; others queue and count as in flight |
| git reader child limits | CPU 10 s, address space 512 MiB (Linux; macOS has no working `RLIMIT_AS`: the host polls each child's resident memory and kills its group above 512 MiB, §3), wall clock 30 s from when the request is queued, 64 open files |
| git reader queue | 8 waiting per principal, 64 host-wide; over either: `quota.host` |
| git reader diff text | per file side 8 MiB and 200 000 lines; 1 000 000 diffed lines per request; over: "too large to diff" (§0.8) |
| git reader inputs | object inflated ≤ 64 MiB (equal to its header); delta depth ≤ 64; `packed-refs` ≤ 32 MiB; pack `.idx` ≤ 256 MiB; `.git/index` ≤ 128 MiB and ≤ 1 000 000 entries; worktree walk ≤ 200 000 entries |
| outcome records | 100 000 per principal |
| audit log | `audit.jsonl` rotated at 10 MiB, 20 files kept. `human.*`, writes, `peer.denied` and control ops go to `audit-security.jsonl`, which read traffic never rotates away. |
| idle connection | closed after 10 min unless it has a subscription or a request in flight |

### 0.12 Examples

Agent class (UID 502, with a grant):

```
→ {"t":"req","id":"r1","op":"status","args":{},"auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"}}
← {"t":"res","id":"r1","ok":true,"result":{"data":{"person":"p_sev","grant":{"id":"gr_01J9Z8…","until":"2026-10-11T18:00:00Z"},
   "cursor":14,"claims":["DEMO-0043"]},"key":"DEMO-0043","seq":0,"cursor":14,"hints":["orch task next"],
   "lines":["DEMO-0043 in-progress (your claim) · T3 next · 2 new events"],"duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r3","op":"blob.put","args":{"offset":0,"size":48213,"b64":"MTEy…","last":true,"sha256":"sha256:5e1a…"}}
← {"t":"res","id":"r3","ok":true,"result":{"data":{"blob":"b_K7Q2…","size":48213},"key":null,"seq":0,"cursor":0,"hints":[],"lines":[],"duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r4","op":"artifact.add","args":{"ref":"DEMO-0043","path":"tests.log","ac":"AC2"},"files":{"path":{"blob":"b_K7Q2…"}},
   "auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"},"opts":{"idem":"a_01J9ZS…"}}
← {"t":"res","id":"r4","ok":true,"result":{"data":{"artifact":"tests.log","digest":"sha256:5e1a…"},"key":"DEMO-0043","seq":18,"cursor":18,
   "hints":["orch task done T3"],"lines":[],"duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r5","op":"approve","args":{"ref":"DEMO-0043","gate":"plan"},"auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"}}
← {"t":"res","id":"r5","ok":false,"exit":3,"error":{"code":"human_only","message":"approve: human only","hint":"orch ask or orch wait","retryable":false}}
```

Dashboard (person class, UID 501, mapped to `p_sev`). A board snapshot, a subscription, then a verdict through the
hand-off:

```
→ {"t":"req","id":"d1","op":"ticket.query","args":{"status":["open","in_progress","testing"],"limit":50}}
← {"t":"res","id":"d1","ok":true,"result":{"data":{"rows":[{"key":"DEMO-0043","uid":"01J9ZK…","title":"Billing join","status":"testing",
   "gates":{"requirements":"approved","plan":"approved","verify":"open","code":"n/a"},"needs":["approve"],"head_seq":18,…}],
   "next_page":null,"total":1,"sub_cursor":"c1.QmF…","as_of":"2026-10-11T15:01:00Z"},…}}
→ {"t":"req","id":"d2","op":"sub.open","args":{"topics":["tickets","needs"],"after":"c1.QmF…"}}
← {"t":"res","id":"d2","ok":true,"result":{"data":{"sub":"sub_1","cursor":"c1.QmF…"},…}}
← {"t":"evt","sub":"sub_1","cursor":"c1.QmG…","items":[{"id":"5121.e","jpos":5121,"log":"01J9ZK…","key":"DEMO-0043","uid":"01J9ZK…","seq":19,"ws_seq":31,
   "type":"task.done","at":"2026-10-11T15:02:11Z","actor":{"kind":"agent","id":"claude"}}]}
→ {"t":"req","id":"d3","op":"human.prepare","args":{"op":"verdict","args":{"ref":"DEMO-0043","outcome":"pass"},"handoff":true}}
← {"t":"res","id":"d3","ok":true,"result":{"data":{"request":{"id":"hr_01J9ZT…","code":"7F3K-Q2MA","expires_at":"2026-10-11T15:14:00Z","state":"prepared"},
   "workspace_id":"6f1c…","log":"01J9ZK…","events":[{"v":2,"id":"01J9ZT…","type":"verdict.given",
   "actor":{"kind":"person","id":"p_sev","device":null},"auth":null,"hash_v":1,"roster_v":4,"based_on":"sha256:…","gate_gen":2,
   "outcome":"pass","hash":"sha256:81ab…","source_sha":[{"repo":"https://github.com/acme/energy-dbt","branch":"t/DEMO-0043","sha":"4be1…"}]}],
   "material":{"kind":"gate","gate":"verify","input":{…},"review":[{"event_id":"01J9ZT…","type":"verdict.given","fields":{…}}]},
   "review_digest":"sha256:3c9d0e…","fingerprint":"8a41f07c2d93","origin":{"uid":501,"pid":4312,"client":"orch-dashboard/0.1",
   "created_at":"2026-10-11T15:04:00Z"},"checks":[{"repo":"acme-energy-dbt","clean":true,"attested_by":"worker","at":"2026-10-11T15:04:00Z"}]},…}}
```

Then the person runs `orch sign 7F3K-Q2MA`. Its connection:

```
→ {"t":"req","id":"c1","op":"human.pending","args":{"code":"7F3K-Q2MA"}}
→ {"t":"req","id":"c2","op":"human.get","args":{"request_id":"hr_01J9ZT…","signer":{"device":"d_7Q…","auth":"passphrase"}}}
   (it checks the shape, recomputes the hashes and fingerprint 8a41f07c2d93, shows the header and the review, asks for "DEMO-0043", prompts, signs)

→ {"t":"req","id":"c3","op":"human.submit","args":{"request_id":"hr_01J9ZT…","events":[{…bound event…,"sig":"MEUCIQ…"}]},"opts":{"idem":"a_01J9ZU…"}}
← {"t":"res","id":"c3","ok":true,"result":{"data":{"gate":"verify","outcome":"pass"},"key":"DEMO-0043","seq":20,"cursor":20,"hints":[],
   "lines":[],"duplicate":false,"exit":0,"head":null}}
```

The dashboard sees `seq` 20 through its subscription, or polls `human.pending {request_id}` → `state:"submitted"`.

### 0.13 Changelog

**v0 → v1.**

1. String request ids; `fatal` frames; strict-JSON rules; `cancel`; `outcome.unknown`; response and write ordering.
2. `opts.idem` on writes, namespaced and bound; outcome records with tombstones; `outcome.get`, `idem.conflict`,
   `idem.expired`.
3. Exact `registry` equality replaced by `caps` plus per-op contract versions (`op.unsupported`).
4. Peer classes `worker` and `bridge`; an explicit UID→person map; `human.prepare` takes `signer`; sessions keyed by
   principal.
5. Reads with an invalid grant behave as in F1 §10.7.
6. `human.prepare` returns `material` and `review_digest`; the signer recomputes and renders; the dashboard uses
   prepare → sign → submit.
7. Subscriptions keep their principal, revalidate, use opaque scoped cursors, add a `needs` topic, and invalidate on
   overflow.
8. New reads (`ticket.query`, `needs`, `diff`, `artifact.get`, `workspace.show`, `member.list`, `session.list`);
   `move`; person versions of agent ops; `blob.drop`.
9. A control socket.
10. Blob rules and a limits table.

**v1 → v1.1.**

1. **Normative schemas** (§0.1, §0.3, §0.8) for every envelope and op: types, required and nullable keys, bounds,
   defaults and errors. The handshake order is fixed. `welcome` gains `tier` and `limits.inline_bytes`, and
   `workspace` is `null` on the control socket. Parsing precision (depth counting, no floats, lone surrogates, integer
   bounds) and a 10 s frame-body deadline.
2. **One authoritative permission and visibility matrix** (§0.5). The principal comes only from a verified grant
   (F4). The person mapping is rechecked on every request (F8). Visibility rules apply to every read (F6). One worker
   connection per worker UID (F3).
3. **Error disposition table** (§0.6) with exit, retryable, connection fate and `retry_after_ms`. New codes:
   `handshake.invalid`, `req.invalid`, `timeout`, `quota.host`, `result.too_large`, `workspace.unavailable`,
   `workspace.degraded`, `idem.required`, `page.expired`, `human.request_state`, `worker.lease`,
   `repo.remote_mismatch`. `host.unavailable` is client-only.
4. **Idempotency** (§0.7): which ops count as writes; the binding includes the session and file digests; concurrent
   duplicates wait; retryable failures are not stored; `pending` records are reconciled from the log after a crash.
5. **Human ops** (§0.8.1): `event` is now `events` (1..16, multi-event ops). `signer` is optional and a hand-off binds
   its device in the new op **`human.get`** (R7, F5). The grant secret is drawn by the signer, and the prepared grant
   event has `secret_hash: null` (F1). `origin`, an op-specific header and confirmation word, and a 12-hex
   `fingerprint` shared by the dashboard, `orch sign` and H9 (R8). `human.prepare` accepts `files` (R9). `human.pending
   {request_id}` reports `prepared`/`submitted`/`cancelled`/`expired` with event id and seq (R6). The lookup limit is
   per connection. Host-prepared event ids. `dry_run` moves to `opts`, with `request: null`.
6. **New ops:** `ticket.get` (structured ticket data: log summaries, question states, evidence digests per criterion;
   R1), `grant.list` (R2), `job.next`/`heartbeat`/`done` (§0.8.2), control-op schemas including `host.bootstrap` and
   `host.list` with the caller's person mapping (R11, §0.8.3). `task.done` contract 2 takes a client receipt.
   `policy.set` and `settings.set` are provisional through `human.*`. `ticket.query` defines filter combination,
   search, sort and ties, snapshot paging and `total`. `diff` defines base and head selection.
7. **Subscriptions** (§0.9): deterministic `jpos` that survives restarts, snapshot cursors from every snapshot read,
   response-before-event ordering, batching, topic overlap, `end` reasons, the `gone` exception, and accepted metadata
   (F13).
8. **Blobs** (§0.10): base64 alphabet and padding, decoded-byte limits, inline totals, the first chunk reserves
   `size`, chunk retries, consumption on commit only, no file names, quotas per UID and workspace (F9). "Looks like a
   path" is replaced by the registry's `x-file` marker.
9. **Limits** (§0.11): git reader children and their numeric limits, outcome caps, audit retention (F10).

**v1.1 → v1.2.**

1. **Schemas finished.**
   - New types `Status`, `Gate`, `NeedKind`, `ArgName`, `Idem`, `WsId`, `PeerSpec`, `Event` and `SignedEvent`.
   - Typed control-op schemas. The signed bootstrap envelope is one `SignedEvent`, the genesis `workspace.created`.
   - `policy.set` and `settings.set` are now defined (the existing `policy.changed` and `settings.changed` payloads),
     no longer provisional.
2. **An op → cap → contract-version table** (§0.4). v1.2 is the per-op baseline: every op is at 1, `task.done` at 2.
3. **Effective column** for every class × grant state × person mapping (§0.5). Column P may run unattended ops, as in
   P1. A person-class request with an unverified grant reads as N and its writes are refused.
4. **Errors** (§0.6): the universal error list, precedence in 12 steps, a complete per-op error table, and
   `custody.unsafe_mode`.
5. **Idempotency** (§0.7):
   - five write categories, each with its recovery and lookup;
   - control intents with `host.outcome`, and recovery for an interrupted `bootstrap`;
   - `job.done` idempotent by job id;
   - replays re-enveloped with the current request id;
   - file digests kept apart from blobs, so a retry may send `files.<arg>: {digest}`;
   - `dry_run` limited to log writes and `human.prepare`; every other op refuses it.
6. **Human ops** (§0.8.1):
   - `Prepared` carries `origin` from prepare as well as get, and an unsigned `checks` field (N3);
   - `Material` carries a canonical `review` per event (a frozen field table) and `before` values;
   - the fingerprint covers the events (N2);
   - the signer checks the shape against `EMITS_V1[op]` and lists every event (N4);
   - `request.bound` is replaced by `state`, with a transition table and the exact invariant bytes;
   - replay precedence for `human.submit`; the `secret_hash` set-aside applies only to `grant.issued`;
   - `human.pending` returns `event_ids`.
7. **Jobs** (§0.8.2): typed inputs and results; a **worker socket** serving every workspace over one connection,
   accepted only from the service manager's main PID (N1); `tree.check` takes pins for gitdir, `commondir` and
   worktree, and gains `why:"type"`.
8. **Subscriptions** (§0.9): an item `id` distinct from `jpos`, cursor advancement over filtered positions, and `gone`
   as the one item exempt from the visibility filter, sent only to viewers who saw the ticket.
9. **Blobs** (§0.10): `upload_key` for recovering the allocation, `received`, zero-byte blobs, and an idempotent
   final chunk.

**v1.2 → v1.3.**

1. **`{digest}` is never resolved to bytes** (M1). A retry that must run again answers `blob.invalid`.
2. **A person-class UID with an invalid mapping gets no writes** (L1). Dispatcher step 5 now puts the unverified-grant
   refusal before the column check, matching the effective-column table.
3. **Human ops:**
   - `Prepared.op` is always present, and gate material carries the ticket;
   - `Review` is exact: the whole event payload verbatim (L2) plus signer-computed `derived` values per type,
     certificate scopes and expiry included;
   - section texts are checked against the event's `section_hash` (`material.texts`);
   - a `before` rule covers `settings.set` (shown as unsigned host values, keys must lie in `set`);
   - submit replay is ordered: the generic idem step first, then request-level replay with a fresh outcome record;
   - multi-event submissions roll forward after a crash and are never `partial`.
4. **Control intents end in durable terminal states.** A rollback stores `control.rolled_back`, which is also what
   `host.outcome` answers, and a rolled-back bootstrap restarts from `host.wsk_create`.
5. **Job completion records** are persisted, with canonical digests over result or error. `job.done` replays work
   across a reconnect and a restart, and a `void` lease is refused.
6. **Dry runs ignore `opts.idem`:** never required, never looked up, never replayed.
7. **`idem.expired`** is in the `outcome.get` and `host.outcome` error lists.
8. New code `control.rolled_back`.

**v1.3 → v1.3.1.** Frames contain at most 65,536 JSON values; exceeding the cap is fatal `frame.invalid` (§0.2).

**v1.3.1 → v1.3.2.** Add fatal `conn.overloaded` (exit 9, retryable) for connection-level backpressure;
`quota.host` remains request-scoped except at connect. Bound host-wide connections and default per-UID connection
attempts to 20/s, burst 40. Runtime binding uses per-thread cwd on macOS or `/proc/self/fd` on Linux (orch-52).

**v1.3.2 → v1.4 (provisional).** Requested by the dashboard stream (addons/dashboard HANDOVER, "P2 server (M2 reads)").
Every change is an optional key or a new op, so no existing op changes its version.

1. **`ticket.query` rows** gain `restricted`, `tasks` and `criteria` counts, `blocking_questions`, a richer `claim`
   (`agent`, `for`, `since`, `live`) and `closed`.
2. **`needs` items** gain `since`, `text`, `status` and a typed `payload` for every kind. The payloads of `no_eligible`,
   `acknowledge` and `addon` are documented, with `who`, `ref` and `detail` per kind.
3. **`ticket.get`** gains question text, options, `asked_*` and the answer; `tasks` with receipts; per-gate policy,
   quorum, eligible persons and every decision; the verdict; the artifact manifest; the claim in full; the source list
   and `branch.pushed` history; per-section edit history (hashes, because F1 stores no old texts); and a structured
   `data` on every log item, with a closed table per event type. `Actor` and `Snip` are new common types.
4. **Closed tickets** (§0.8.4): a plain read of a visible closed or done ticket never errors; `ticket.get` returns it with
   `status`, `settled` and `closed`; `ticket.query` leaves closed tickets out unless `status` lists `closed`. Closed
   rows may name a duplicate target's key (N5 extended).
5. **New op `activity.list`** (cap `activity/1`): a paged, filtered workspace feed over the verified logs, with
   per-item visibility, no `total`, and the live tail through `sub.open`. Design choice: feed visibility is current
   visibility (at the snapshot and at the request), as in `ticket.get`, never visibility when the event was appended.
6. **Visibility** (§0.5): a block of rules for derived fields (computed after the filter, `restricted` only on visible rows,
   nothing beyond what F1 stores, no secrets).
7. **Capabilities** (§0.4): `read_ext/1` marks a host that sends all the new optional fields; `activity/1` for the new op.
   Error table: `activity.list` added; no new error code is needed.
8. **Review round (Opus security review of #378, and the dashboard stream's visibility questions).** Page tokens are
   host-authenticated and bound to principal, epoch, snapshot, position and arguments, every page is filtered for its own
   request's viewer, and a feed or query item must be visible at the snapshot and now (also for `ticket.query`);
   workspace-log `data` is a closed table, and a `viewer` gets the same workspace-log items as a member (nothing beyond
   `member.list`); `needs {person}` is filtered for the caller and the person; `duplicate_of` follows the `parent`/`epic`
   rule (key shown, `not_found` on any op, plain text in clients); `jpos` inside ids, cursors and tokens is accepted
   metadata; `_more` and `_cut` flags; a scan budget for the feed; `who` and `eligible` limited to persons who can see the
   ticket; accepted risks recorded in §7.


---

## 1. Process model

**D-P2-1. One host per host UID serves every registered workspace, with one worker thread per workspace.**

- The worker gives an order to writes and keeps a single owner for the mutable `Store`. (`FileLock` already shares a
  `threading.RLock`, so locking is not the reason.)
- **Registration.** Roots are canonicalised with `realpath`. A duplicate workspace id or root is refused.
- **Workspace states.**
  - `ok`;
  - `degraded`: reads only, for example an unacknowledged invalid event or a journal rebuild; a write answers
    `workspace.degraded`;
  - `unavailable`: missing, chain broken at genesis, or unsafe modes; every op answers `workspace.unavailable`.

**D-P2-2. The OS service manager starts the host, never a CLI call.** If a CLI call could spawn the host, an agent-UID
call would start a host as the agent.

| Platform | Service | Run directory |
|---|---|---|
| macOS | LaunchAgent `io.orch.host` (`RunAtLoad`, `KeepAlive`) | `/Users/Shared/orch/run/<host uid>` (D-P2-6, F12), created by the setup helper (H3c) |
| Linux | systemd user or system unit | `/run/orch/<host uid>`, provisioned by `tmpfiles.d` from the privileged setup. A user unit's `RuntimeDirectory` lives under `$XDG_RUNTIME_DIR`, which agents cannot traverse. |
| dev and tests | `orch host run --foreground` | `<state>/run` (no agent UIDs) |

**D-P2-3. Every outcome is recoverable.** The store recovers torn appends. The host adds the outcome records and
their reconciliation (§0.7), so a lost response, an interrupted composite or a human submit is recovered by `idem`,
never repeated.

**D-P2-4. Upgrades.**

- The service runs a versioned, immutable release. `orch host install` copies it to
  `~/.local/share/orch/releases/<version>`, and the host imports every `orch` module at start.
- An upgrade is: install the new release, then `host.restart` or the service manager's restart.
- **Drain.**
  - New requests answer `host.restarting` with `retry_after_ms`.
  - Writes in flight finish; there is no forced deadline inside a commit.
  - Parked `wait`s answer `host.restarting`, and subscriptions end with `end:"host.restarting"`.
  - Lost responses are recovered with `outcome.get`.
- Version skew is handled by capabilities (§0.4).

**D-P2-5. Several OS users, with an explicit UID map.** The host's registry (`<state>/host/workspaces.json`, 0600, in
the D-P2-17 custody list) holds, per workspace:

```json
{"workspace_id":"6f1c…","root":"/Users/sev/acme","host_uid":501,"group":"orch-6f1c0d2e8b4a",
 "peers":{"501":{"class":"person","person":"p_sev"},"502":{"class":"agent"},"503":{"class":"worker"}},
 "repos":{"acme-energy-dbt":{"gitdir":{"path":"…","dev":1,"ino":2},"commondir":{"path":"…","dev":1,"ino":3}}}}
```

- The class and mapping rules are §0.5 and §0.8.3. The mapping is checked at registration and again on every request.
- **Machines without agent UIDs** (dev, or a Mac before H3c) are tier A (§0.5).

## 2. The socket

**D-P2-6. One socket per workspace in `<run>/<ws>/` (0750, group `orch-<ws 12 hex>`), the socket itself 0660, and
`<run>` owned by the host UID, mode 0711.** File modes keep other users and other workspaces' agents out before the
peer check runs. With mode 0711, agents can reach their own workspace directory but cannot list workspace ids.

- **Ancestors.**
  - Every ancestor of `<run>` must be owned by root or by the host UID, with no write for anyone else.
  - A sticky root-owned directory (`/Users/Shared`, `/run`) is allowed only as the parent of a directory created by the
    setup helper as root, mode 0755, owned by root (`/Users/Shared/orch`, `/Users/Shared/orch/run`, `/run/orch`).
    The helper refuses, and names the owner, when one of these exists with another owner, other modes or an ACL (F12).
  - No ACL entry may grant write, `add_subdirectory` or `delete_child` to anyone else (macOS: `acl_get_link_np`).
  - The host checks this at start and every 60 s, and refuses to start (or stops serving) on a violation.
    Transient resource errors defer the recheck; transient accept errors back off and retry without stopping the listener.
- **Binding.**
  1. Open each component with `O_NOFOLLOW|O_DIRECTORY` and `fstat` it.
  2. Take `<run>/<ws>/lock`.
  3. Bind through the checked directory fd without changing the process cwd or umask. On Linux use
     `/proc/self/fd/<dirfd>/host.sock`. On macOS verify libSystem exports `pthread_fchdir_np`, then bind the
     relative name `host.sock` in a dedicated thread with that per-thread cwd; restore it with
     `pthread_fchdir_np(-1)` in a `finally` block. Refuse with a clear error if neither primitive is available.
     This supports runtime `host.register` while workspace worker threads do relative-path file work (D-P2-1).
  4. Verify the socket inode, `chmod` 0660 and `chown :group` without following symlinks, then `listen`.
     The checked directory restricts access while the socket is being prepared; it is not yet listening.

  Stale sockets are removed only while the lock is held and only after a connect fails.
- **Path length.** The UTF-8 encoded absolute path must stay at or under 100 bytes.
- **Groups.**
  - The setup helper creates the group with a recorded gid and refuses if it already exists with an unknown gid or
    members.
  - The host UID is a member, which `chown` needs.
  - After a membership change the helper restarts the host and the agents' sessions, because group lists are fixed per
    process.
- **Peer credentials, both ways.** `SO_PEERCRED` on Linux, `getpeereid` (ctypes) on macOS.
  - The host refuses an unmapped UID before reading any byte.
  - The client accepts the server only if its UID and `welcome.workspace.id` match a **trusted pin**:
    - for a person-class client, its own registry (the same UID as the host);
    - for an agent-class client, `/Library/Application Support/orch/pins/<agent uid>.json` (macOS) or
      `/etc/orch/pins/<uid>.json` (Linux), root-owned and written by the setup helper.

    `config.json` only names the workspace id to look up.
- **The credential is a snapshot of the connecting process.** No privileged process forwards connections for others:
  the dashboard never proxies requests for another principal, and the host never passes client descriptors on.
- **Real UID tests.** Two-process tests with real UIDs run in CI (Linux container, `useradd`) and on the dev Mac with a
  test user.

**D-P2-7. There is no TCP listener in the host. The dashboard is a socket client.** The dashboard addon is a FastAPI
process under the human's UID. It serves `127.0.0.1:<random port>` and is person class on the socket.

Required on the HTTP side (stream B; amended per R10):

- **A bearer token in a header, no cookie.**
  - The launch URL carries a one-time token in the fragment. The launch token must never appear in process arguments
    (visible via `ps`) or in an environment readable by other users.
  - The page exchanges it, with an `Origin` check, for a session token held in page memory only.
  - Every API call sends `Authorization: Bearer <token>`.
  - With no ambient credential, a cross-site request carries nothing, so CSRF has nothing to ride on.
  - The token expires when the dashboard process restarts.
- `Host` must be exactly `127.0.0.1:<port>`. `localhost` is refused (against DNS rebinding).
- `Origin` is checked on every non-GET request, and GET requests have no side effects.
- A strict CSP.
- SSE fed by `sub.open` (the token goes in the SSE URL's query, single use, exchanged on connect).

A review in the browser never stands in for the signer's review (§0.8.1).

**Limit:** on a Mac the dashboard runs as the same UID as the host. A dashboard that runs attacker code can read the
WSK. This is accepted under §4 ("the human's machine is trusted").

## 3. Dispatch

**D-P2-8. Frames are length-prefixed JSON with orch's own envelope** (§0.2, §0.3). The result and error envelopes are
already the contract, and the length prefix lets the host enforce the size limit before parsing.

**D-P2-9. The op list is the registry, and handlers get explicit capabilities.** P1 handlers read `ctx.env`, open
paths, run subprocesses and sign. Before the host serves them, H3a gives `Context` a capability object:

| Capability | Host | Direct mode |
|---|---|---|
| `store` | the worker's store | the CLI's store |
| `files` | client-supplied bytes (§0.10) | the CLI reads files |
| `git` | the reader launcher (below) | the same reader, in-process |
| `clock` | host clock | system clock |
| `exec` | none | the subprocess runner (`verify.cmd`) |
| `signer` | none | custody |

**The launcher boundary.**

- In the host, the one permitted process spawn is `orch.host.launcher.spawn_gitread()`. It runs
  `[sys.executable, "-I", "-m", "orch.gitread"]` with a fixed argv, an empty environment, `close_fds`, stdin and
  stdout pipes only, `setrlimit` per §0.11, and a new process group.
- A `sys.addaudithook` installed at host start allows exactly that spawn: its `subprocess.Popen` /
  `os.posix_spawn` event with that argv, from the launcher module. Every other `subprocess`, `os.exec*`, `os.system`
  or `os.posix_spawn` raises.
- The same hook fails any `open` outside the workspace root and the state directory. The host itself never opens a
  repo file: only the reader child does.

**Reader memory (H3b1).** `RLIMIT_AS` is a no-op on macOS, so while a child runs the host polls its resident memory
every 100 ms (`proc_pid_rusage` on macOS, the larger of resident size and physical footprint; `/proc/<pid>/statm`
on Linux, through a `/proc` fd opened before the audit hook) and kills its process group above **512 MiB**; the
request is `observe.unavailable`. Each read runs under the caller's principal (`Capabilities.with_principal`); a
host-mode read without one is refused.

**Declared paths.** Repos are opened by an `O_NOFOLLOW` walk of the path the host joins from `settings.repos`,
byte for byte and never resolved. A path with a symlinked component (a workspace under `/tmp` or `/var` on macOS)
is refused: **declare the real path** (`realpath`) of each repo. A `.git` directory must have no `commondir` file;
a `.git` file must name `<C>/worktrees/<id>` of another declared repo's `.git` `<C>`, with that worktree's
`commondir` naming `<C>` and its `gitdir` back-pointer naming this working copy's `.git`.

**Reader profiles.** `orch.gitread` takes its profile and pins as its first request on stdin and installs its own
audit hook before it reads anything.

| Profile | Started by | Reads | Used for |
|---|---|---|---|
| `gitdir` | the host's launcher, as the host UID | only under the pinned gitdir and `commondir` | refs, objects, `diff`, the D58 branch check |
| `worktree` | the worker's launcher (the same fixed argv, empty environment and limits), as the worker UID | the pinned gitdir, the `commondir` and the pinned worktree | `tree.check` (N3) |

- An `open` outside a profile's pins raises in the child, and the job or request fails with `observe.unavailable`.
- The worker's own audit hook allows the same single spawn. The addon runner (F1 §8.1) is the worker's other spawn,
  and that happens in the worker, never in the host.

**Flows.**

- The check-and-run core of `cli.main.run` moves into `orch.host.dispatch` and serves both modes. That covers input
  validation, the who-check, the grant check, the stop rule, dedup and declared errors.
- **`init`** (composite):
  1. the person's TTY steps (passphrase, recovery code; device key made locally);
  2. `host.wsk_create` → `{workspace_id, wsk_pub}`;
  3. the client signs the delegation and `workspace.created` locally;
  4. `host.bootstrap` appends the genesis and registers the workspace.
- **`import v1`** runs in direct mode only, on an unregistered workspace: `orch import v1`, then `orch host register`.
  A registered workspace answers `host.required` (`unregister`, import, `register`). The importer reads v1 files, which
  the host must never do for a client, and the plan signs a large batch under one unlock (F1 §14.2). Neither fits a
  socket op.
- **`task done --run`:** the client runs `verify.cmd` and sends the receipt (§0.8, D-P2-19).
- **Dry run:** `opts.dry_run` everywhere. For a person op, `human.prepare` with `opts.dry_run` returns `request:null`
  and creates nothing (§0.8.1).

**D-P2-10. Authentication per call, with the F1 reads rule** (§0.5). The redaction context (the secret and the parts
derived from it) is kept for the whole request after `auth` is dropped. Every result, error, audit line and outcome
record is redacted with it.

**D-P2-11. The host never opens a client-named path** (§0.10). The two exceptions are the control-socket ops
`host.register` and `host.bootstrap` (§0.8.3), from the host UID only. Repo working copies are taken from the signed
`settings.repos` and opened under their pins (D-P2-19).

**D-P2-12. Subscriptions** (§0.9). The journal is in memory and rebuilt from the logs at start.

Foreign appends (F11): under the store lock, before every append and at every 60 s check, the host compares each log's
head with the head it last saw. A head the host did not write means someone else appended: in tier A, an older CLI or a
script holding the file-tier WSK. The host then:

- rebuilds the journal from the logs;
- ends every subscription (`end:"reset"`);
- marks the workspace `degraded` until the rebuild ends;
- writes one audit line.

In tier B the store files are writable only by the host, so this cannot happen there.

**D-P2-13. Limits** (§0.11). Audit lines record time, UID, class, op, principal, session, code and duration, never a
secret.

**D-P2-14. Versioning by capabilities** (§0.4). Older clients are kept honest by recorded fixtures (H7).

## 4. Human signing

**D-P2-15. The client signs. The host never prompts and holds no device key.** A daemon has no terminal, and a host
that could prompt would let any peer raise prompts on the person's screen. C7's rules stay with the signer.

**D-P2-16. The grant secret is drawn by the signing client, never by the preparer (§0.8.1).** Later calls made under
the grant send the secret in `auth.grant`.

**D-P2-21. The signer recomputes hashes from structured material and renders its own review**, opening with its own
header and the shared fingerprint. A host bug must not be able to make the person sign content they did not see.

**D-P2-22 (manager ruling, orch-89). The Touch ID signer H9 is in P2 and must land before the cutover (D44).**

- **Order:**
  1. the `human.prepare` → sign → `human.submit` contract (H5a);
  2. the terminal hand-off `orch sign CODE` as the first signing path (H5b);
  3. H9 as its own group.
- H9 blocks neither the socket core nor the dashboard's early milestones. The invariant stays: the host never prompts
  and holds no device key.
- The dashboard calls `human.prepare {handoff:true}`, previews from `material` with the fingerprint, and hands the
  request to a signer.

| Signer | Fetch | Review | Prompt |
|---|---|---|---|
| CLI (`orch approve`) | `human.prepare` with `signer` | `/dev/tty`: header, then review from `material`, confirmation word, `tcflush` (C7) | passphrase, fields from the signing bytes |
| CLI (`orch sign CODE`) | `human.pending` + `human.get` (binds the device) | the same | the same |
| H9 `orch-signer` (macOS, Swift, Secure Enclave P-256, auth `secure-enclave`) | `human.get` (binds the device) | its own native window: header, then review from `material` after recomputing the hashes | Touch ID; the LocalAuthentication reason is built from the signing bytes; non-person labels refused |

WebAuthn (D64) needs a new signature contract and is deferred past P2 (H0 records this).

## 5. WSK custody

**D-P2-17. The WSK stays a file-tier key at `<state>/hosts/<ws>/keys/wsk` (0600 in a 0700 directory), owned by the host
UID. P2 adds no keychain.**

- A keychain ACL binds to the `python` binary, which every process of the same UID can run. The UID is the boundary.
- This amends the macOS row of orch-v2.md §5.5 (H0).
- **The custody list.** These must be private to the host UID, or (in the workspace) group-readable and writable only
  by the host:
  - `keys/`, `hosts/<ws>/`, the registry `<state>/host/` and `workspaces.json` (F8);
  - the genesis pins, the noted revocations, `outcomes/`, `requests/`, `blobs/`;
  - `audit*.jsonl`, `<state>/host/intents/`;
  - the workspace's `events/`, `tickets/` and `.state/`;
  - `hosts/<ws>/membership/` (P3, §11): the `list_seq` and epoch high-water marks, the per-epoch key-recipient sets
    and the body in flight;
  - the burned `WK`s, under `keys/` like old epoch keys (§11).
- **Their ancestors** must not be replaceable by anyone else: owned by root or the host UID, no write for group or
  other unless sticky (D-P2-6 rules), and no ACL or link tricks. Ordinary readable ancestors such as `/` and `/Users`
  are fine. Processes of the same UID stay trusted.
- **For the CLI:** in host mode it opens neither the WSK nor the store, and keeps only the person's device key for
  signing. With agent UIDs (tier B), the P1 limits N4, N14 and O7 close, and session notes can no longer be forged.

## 6. Migration and compatibility

**D-P2-18. Host ownership is recorded outside the workspace, and there is no silent fallback.**

| Workspace | CLI behaviour |
|---|---|
| registered (in the person's registry, or in a pin for an agent UID) | must use the host. Host down, timeout or version error: `host.unavailable`, `host.version_mismatch` or `op.unsupported`, never direct mode. |
| unmanaged | direct mode exactly as P1 (CI, tests, the owner's C11 workspace, Windows, `import v1`) |

- **Adoption** (`host.register`):
  1. take the exclusive ownership lock `<state>/hosts/<ws>/owner.lock`;
  2. write the registration and start serving;
  3. release the lock.
- **A direct-mode CLI** holds the lock shared **per store transaction**, not across a parked `wait`, and checks the
  registration again after every wake. If the workspace has been registered, it answers `host.required`.
- `host.register` waits at most 60 s for the lock, then answers `lock.busy` naming the holders' PIDs (F14).
- `host.unregister` stops serving, removes the registration and so restores direct mode explicitly.
- On a VPS, registering moves `hosts/<ws>/` and `.state/` to `orch-host-<ws>`. The lock and registration marker stay
  at the old path as a root-owned stub that says `host.required`.
- `ORCH_HOST` is gone. The format and the logs do not change.

## 7. Security model and threats

| Threat | Answer |
|---|---|
| Another local user | Directory and socket modes; peer map checked before any byte is read; private key directories. |
| An agent under its own UID | Agent class: the §0.5 matrix, no `human.*`, no admin. It cannot read keys or host state. Tier B: the store files can be written only by the host. Reads over the socket follow its grant's person; the workspace files stay group-readable (F1 §9: restricted is not secret). |
| Workspace code run by the host | Never. The only spawn is the launcher (D-P2-9), enforced by the audit hook. |
| Client-named paths, env, cwd | Never used, except the two control ops from the host UID (D-P2-11). |
| A grant secret leaked to the preparer | The secret is drawn by the signer; the prepared event has `secret_hash: null` (F1). |
| A forged hand-off (tier A) | The signer's own header: workspace from its registry, op and decisive fields, origin, fingerprint, an op-specific confirmation word, and the "only sign a code you created" notice (F5). |
| Grant id reuse | The principal comes only from a verified grant (F4). |
| Malicious addon or escalation into the worker | Runs under the worker UID. The worker role is one connection per UID, with leases bound to that connection (F3). The clean-tree check runs no git process (F2). Proposals are rechecked. |
| Socket hijack, planted socket, path replacement | Host-owned `<run>` under helper-created root directories (F12), ancestor checks every 60 s, bind through a checked fd, trusted pins. |
| An agent steering D58 | Declared `remote` enforced, gitdir and `commondir` pins, ref resolution as git does it, a closed set of extensions (F7). |
| Replay | Local socket only; `idem` binding with tombstones and reconciliation; human requests single use and bound; unique event ids; `gate_gen`, `based_on`. A grant secret is a bearer token (N4, accepted). |
| Revoked caller or removed person keeps watching | Subscriptions revalidate before every delivery (§0.9). |
| Denial of service | §0.11 limits, unattended quotas, round-robin fairness, git reader child caps, a security audit log that read traffic cannot rotate away (F10). |
| Version skew or downgrade | Capabilities; the client refuses a missing `proto` or op contract. |
| Tier A agent (same UID) | Can call every person and control op except signing, because it holds no passphrase or Touch ID. That includes `host.unregister` (back to direct mode) and `host.register`. Accepted, as in P1; tier B closes it (N5). |
| Tier A connection slots (H2a) | Without an `orch-<ws>` group (dev, tier A) the workspace socket directory gets the host's primary group (`staff` on macOS), so any local user can reach it and take accept and handshake slots. Unmapped UIDs are still refused before a byte is read. Accepted for tier A; H3c's groups close it. |
| A direct CLI with another state directory (H2a) | Ownership lives in `<state>/hosts/<ws>`, so a direct-mode CLI with a different `ORCH_STATE_DIR` or `XDG_CONFIG_HOME` does not see the registration and can write to a served workspace (trusting its genesis on first use in that directory). Known limit of D-P2-18 in tier A. Tier B closes it, because only the host can write the store files. A follow-up may have the host hold a lock inside the workspace while it serves. |
| A job takes over the worker role | Worker connections are accepted only from the service manager's main PID (N1). |
| A worktree that blocks or misleads the check | `O_NONBLOCK`, regular files only, symlinks by target, escaped names (N3). |
| A prepared request that smuggles an extra event | The signer checks every event against `EMITS_V1[op]`, one log, one `based_on` and its own person, and lists every event (N4). The fingerprint covers the events (N2). |


**D-P2-19. Workspace-controlled code never runs as the host UID.**

- **git: no git process in the host.**
  - A content-addressed reader in orch's own Python reads `HEAD`, loose and packed refs, and objects (loose; pack v2
    with idx v2, ofs and ref deltas; SHA-1 and SHA-256).
  - It runs in a short-lived child (`python -I -m orch.gitread`, started only by the launcher) within the §0.11
    limits.
  - **Config is data, never behaviour.** A strict parser reads only `core.repositoryformatversion`, `extensions.*` and
    `remote.origin.url`, and ignores `include`/`includeIf`. Attributes, hooks, filters and textconv are never read.
  - **Extensions (F7).** The allowed set is closed: `objectFormat` (`sha1`, `sha256`) and `noop`. Any other extension
    (`worktreeConfig`, `refStorage`, `partialClone`, …) makes the repo observe.unavailable.
  - **Identity (F7).**
    - Where `settings.repos.<r>.remote` is declared, the observed identity must equal it (ASCII case ignored), else
      `repo.remote_mismatch` and observe.unavailable (H0 makes the reserved F1 rule live).
    - Where no `remote` is declared, the prompt labels the identity "from the working copy (agent-writable)", and
      `orch doctor` recommends declaring it.
  - **Pins (F7).**
    - The host pins the gitdir **and** the `commondir` (realpath, device, inode) when a repo first appears: at
      registration, or when a `settings.changed` that adds or moves it is appended.
    - Both must lie under that repo's declared `path`, or the pin must belong to another repo declared in
      `settings.repos` (a linked worktree). Anything else is observe.unavailable.
    - Only `settings.changed` re-pins. A later mismatch is observe.unavailable.
    - Every open stays under the pins, with `O_NOFOLLOW` and a regular-file check.
    - `objects/info/alternates`, `info/grafts`, `shallow` and `refs/replace/` are ignored. A missing object is
      observe.unavailable and is never fetched.
  - **Refs (F7).**
    - A loose ref wins over `packed-refs`. `HEAD` must be exactly `ref: refs/heads/<branch>\n`.
    - A loose ref that is itself a symref, a ref name differing only in case from another ref in either store, or an
      unparsable `packed-refs` line makes the repo observe.unavailable.
  - **Bounds:** §0.11. Every object is checked against its id, so `diff` is computed from exactly the commits in
    `source_sha`. Gitlinks show as commit ids, and LFS files as their pointers.
- **The D58 branch check** (HEAD is the symbolic ref `refs/heads/<branch>` at the bound sha) is done by the host
  reader before the prompt and again in `precommit`.
- **The D58 uncommitted-changes check (F2)** reads the index and the worktree **with orch's own reader, never a git
  process**. It runs as a `tree.check` job, in a `python -I -m orch.gitread` child under the worker UID, with the same
  limits.
  - It parses `.git/index` (v2 to v4).
  - It compares each tracked file's `lstat` with the index entry and, where the stat differs, compares the raw-bytes
    blob id.
  - It lists untracked files by walking the tree minus `.gitignore` and `info/exclude` patterns.
  - It reports the `skip-worktree` and `assume-unchanged` flags as changes.
  - No config, attributes, filter, fsmonitor or hook is read.
  - A file whose raw bytes differ from the index blob counts as changed, even where a git filter would hide the
    difference (conservative).

  **When it runs and what it returns (N3).**
  - The check runs at `human.prepare` and **again at `human.get` when it binds a device**, so a hand-off is checked
    when the person is about to sign. A changed tree answers `observe.unavailable` there.
  - The `human.prepare` and `human.get` results carry `checks:[{repo, clean:true, attested_by:"worker", at}]`. This
    field is not in `material` and is not signed, and the signer shows it as **attested by the worker, best effort**.
  - `precommit` does not run the check again (H0 amends F1 §10.8). No worker means observe.unavailable for `verdict`
    and `approve code`.

  **The walk.**
  - It uses `lstat` and `openat(…, O_NOFOLLOW|O_NONBLOCK)` relative to the pinned worktree fd, and reads regular files
    only.
  - A symlink is compared by its target string, as git stores it.
  - Any other file type (FIFO, socket, device) at a tracked path counts as changed (`why: "type"`).
  - The child runs the `worktree` profile.
  - Paths in `changed` are escaped and rendered with F1 §11.3's rules wherever they are shown.
- **`task done --run`:** the client runs `verify.cmd` and sends the receipt (§0.8). The host checks that `cmd` equals
  the approved `verify.cmd`; H3b2 (client receipts) adds `attested_by: "agent"`, absent from the current receipt.
  - This proves an authenticated agent **claimed** this result. It does not prove the command ran, its exit code, its
    output or its commit.
  - H0 amends F1 §10.7 ("the P2 host runs it itself") to say this. Independent verification needs a separate trusted
    executor and is not in P2.

**D-P2-20. Addons and jobs run under a worker UID, never the host UID, with no development exception.**

- `orch worker` runs as a dedicated worker user, separate from the agent users. It is started by a **LaunchDaemon**
  `io.orch.worker` with `UserName` (system context, installed by the setup helper) or by a systemd system unit with
  `User=`.
- **It connects to the worker socket** and serves the jobs of every workspace over that one connection (§0.8.2).
- **The host accepts the connection only from the service manager's main process** (N1, §0.5): PID and start time
  against systemd's `MainPID`, or on macOS against `launchctl print` or a root-owned pid file (H10 confirms which). A
  job that kills the worker cannot take its place during the restart throttle.
- **Jobs** (§0.8.2).
  - A lease lasts 60 s, renewed by heartbeat, and is bound to the one worker connection (F3).
  - Each job carries the principal that caused it, and a cancellation.
  - Retries are explicit, at most twice, keyed by job id.
  - Addon results are checked by `Registry.check_proposal` before the host signs anything.
  - A job's child processes get no copy of the worker connection or of lease tokens: the descriptor is `O_CLOEXEC`,
    the job reads its input from a pipe and writes its proposal to a pipe.
  - When the worker connection drops, every outstanding lease is void, and the host waits for the restarted worker. It
    never hands leased jobs to a new worker-class connection while the old one is live.
- **Containment.**
  - **Linux:** the worker's system unit has `Delegate=yes`. Each job runs in its own child cgroup and is killed as a
    whole (`cgroup.kill`).
  - **macOS:** each job runs in its own process group, killed at its deadline. An **orphan sweep** runs after every
    job ends, every 30 s and at worker start.
    - It lists worker-UID processes (`sysctl KERN_PROC_UID`) and kills every one that does not descend from the
      running worker: anything that escaped its group has been reparented to launchd.
    - The sweep runs as the worker UID, never as root. It stops a candidate (`SIGSTOP`), checks UID, start time and
      process group again while the candidate is stopped, then sends `SIGKILL`; if the check fails it sends `SIGCONT`.
    - A reparented process whose process group or session is that of a live job is not an orphan: it dies with its
      job at the deadline (F14).
    - `RLIMIT_NPROC` on the worker UID bounds fork races. Sibling jobs are never killed.
  - **Accepted:** jobs under the worker UID can signal each other. Nothing they return is trusted without
    `check_proposal` (and the D53 re-check for landing).
- Without a worker, addons do not run (`addon.runner_unavailable`).
- **Accepted in v1.4 (Opus review R8).**
  - A restricted ticket whose list names none of the persons who may sign its `policy.changed` or `visibility.changed`
    cannot have a blocked gate or a ticket-scope `acknowledge` fixed by anyone (F1 `ticket.not_visible`); its item then
    has `who: []`, and an owner's remedy is outside the ticket (F1 §9).
  - `claim.live:false` and `lapsed:"grant_ended"` tell a viewer who may see the ticket that another person's grant ended,
    which `grant.list` hides from them; the `claim.released` event in the log shows the same shortly after.
  - `Actor.session` and `claim.session` show other persons' agent session ids to anyone who may see the ticket, while
    `session.list` gives a viewer their own only. This was already so for `Row.claim.session` in v1.3. A session is a
    label, not authentication (§0.5).
  - The response time of `activity.list` grows with the number of invisible events skipped (bounded by the scan budget).
    The `jpos` gaps reveal more than that already (F13).

## 8. Implementation groups

Each group is one PR with real two-process tests where it touches the socket. Execution isolation comes before host
dispatch is turned on.

| Group | Content | Acceptance | Review |
|---|---|---|---|
| **H0 F1 amendment** | see the list below | text, plus vectors for `ticket.moved` | Codex, Opus |
| **H1a Frame codec** | pure codec, §0.2 rules, envelope schemas (§0.3), `fatal`, the cancel state machine. T0 `tests/host/test_frames.py` | partial and coalesced reads; `n=0`; bad UTF-8; lone surrogate; duplicate keys; float; depth 33; id reuse; body deadline; unknown envelope key → `req.invalid` | Codex |
| **H1b Unix transport** | bind through a checked fd, ancestor and ACL checks, both-way peer credentials, pins, control socket | a real UID refused before any byte; the control socket refuses an agent UID; planted socket; ancestor replaced mid-run; path longer than 100 bytes; `/Users/Shared/orch` owned by another user refused by the host here; helper-side refusal is covered in H3c (F12) | Opus |
| **H2a Lifecycle** | registry, states, ownership lock, control ops (`list`, `register`, `unregister`, `wsk_create`, `restart`), units, release install, drain | second host gets `host.busy`; direct CLI gets `host.required` after adoption; `register` gives up after 60 s with `lock.busy`; `unregister` restores direct mode; `degraded` refuses writes, `unavailable` refuses all; drain finishes a write in flight and ends `wait` with `host.restarting`; `wsk_create` never replaces a key; `register` refuses a pinned agent UID as person; a removed member's UID reads as no-person (F8); `host.list` shows the caller's mapping (R11); kill -9 during `register`, `unregister` and `bootstrap`, then the intent is finished or rolled back at start and `host.outcome` returns the stored result; the effective-column table (§0.5) row by row (a person-class UID with an invalid mapping gets `op.not_allowed` for every write, L1; an unverified grant on an agent op gives `grant.expired`, not `op.not_allowed`); a `register` and a `bootstrap` rolled back at start answer `control.rolled_back` from `host.outcome` and from a same-key retry; `init` restarts from `wsk_create` after a rolled-back bootstrap, including an unverified grant from person class and an invalid person mapping | Codex, Opus (control ops) |
| **H2b Outcomes** | idempotency store, classification, reconciliation, tombstones, `outcome.get`, cancel after commit | two-process kill -9 between append and record, then the retry is a duplicate rebuilt from the log; concurrent duplicates get one append; `idem.conflict`; `idem.expired`; a retryable failure is re-run; `outcome.get` with another person's grant id and a wrong secret gives `outcome.unknown_idem` (F4); a `{digest}` retry that has to run again answers `blob.invalid`, even when a blob or artifact with that digest exists (M1); `outcome.get` on a tombstone answers `idem.expired`; a dry run with an idem that has a record neither replays nor answers `idem.*`; a replay is re-enveloped with the retry's request id; a retry after the blob was consumed binds through `files.<arg>: {digest}`; `{digest}` without a record is `blob.invalid`; `dry_run` on a read, `human.submit`, control, job, blob or sub op is `invalid.input`; `job.done` repeated for a completed job answers `{}` | Opus |
| **H3a Handler capabilities** | `Context` capabilities; handlers lose env, path and exec access; direct mode unchanged | goldens unchanged; with the audit hook in host mode, no open outside the roots and no spawn except the launcher | Codex |
| **H3b1 git reader and `diff`** | `orch.gitread`, the launcher, pins, refs, extensions, identity, `diff` | a malicious `.git/config` runs nothing; a `.git` file or symlinked `objects/` pointing elsewhere is refused; a 1 GB zlib bomb is refused within the limits; reftable is unavailable; the `gitdir` profile cannot open a worktree file and the `worktree` profile cannot open outside its three pins (audit hook); `diff` is a valid unified diff that `git apply` accepts, reproducing the head exactly; identical to `git diff --no-ext-diff --no-textconv --no-renames` on simple fixtures; hunk placement may differ where git uses its heuristics; the F7 cases (changed `origin` against a declared `remote`, a `.git` file planted before the repo is added, a swapped `commondir`, loose/packed disagreement, case-colliding refs, `worktreeConfig`); at most 4 children under a burst of 100 `diff` calls (F10) | Opus |
| **H3b2 Client receipts** | `task.done` contract 2, client-side `verify.cmd`, `attested_by` | `run:true` refused by the host; a receipt with a `cmd` other than `verify.cmd` refused; the output file goes through `files`; the receipt is labelled | Codex |
| **H3b3 Bootstrap** | `host.bootstrap`, the `init` composite | `init` through the host yields the same genesis bytes as direct mode; `bootstrap` refuses a non-empty root, a foreign owner, or an id not from `wsk_create`; a second `bootstrap` for an id that has a genesis or a registration is `host.busy` (N5); a bootstrap killed after the append is finished at restart, and one killed before it leaves no root, key or id behind | Opus |
| **H3c Agent and worker users (#287)** | the macOS and Linux setup helper: users outside `staff`, groups, root-owned `orch` directories, `<run>`, pins, workspace modes, the worker LaunchDaemon | e2e `keys-isolation` and `mac-agent-uid`: the agent cannot read `wsk` or `dk` but can call `status`; the helper refuses a pre-existing foreign `/Users/Shared/orch` (`needs_os_users`, real foreign owner) | Opus |
| **H4a1 Dispatcher** | policy matrix, principals, auth and the reads rule, `whoami`, `host.status`, error catalog and disposition | closed list (registry × column of §0.5); no key material in any response (property test); grant in args refused; invalid grant on a read gives workspace tickets; another person's grant id with a wrong secret cannot reach outcomes, blobs or notes (F4); the F6 visibility cases (`total`, `next_page`, `needs {person}` from an agent, `session.list` claims, `diff` on an unlinked repo, `artifact.get` with `../x`, `sub.open ticket:<restricted>` gives `not_found`) | Opus |
| **H4a2 Files and blobs** | `files`, `blob.*`, `artifact.get`, quotas | chunk retry with the same bytes accepted, with different bytes refused; a wrong final digest refused; use across principals and workspaces gives `blob.invalid`; the per-UID quota and the person reserve (F9); consumed on commit, kept after a refusal or dry run, expired after 10 min; `x-file` without `files` refused; a lost first-chunk response is recovered with the same `upload_key`; a zero-byte blob; a repeated final chunk returns the same result | Opus |
| **H4b Thin CLI** | `orch.hostclient`, mode by registration, rendering from `result` | goldens byte-identical in both modes for all non-human ops except `wait` | Codex |
| **H5a Human contract** | `human.prepare`/`get`/`submit`/`cancel`, multi-event requests, material, signer recomputation and header, CLI composites, person versions of agent ops, `opts.dry_run` | mismatch, expiry, single use, tombstone; stale between prepare and submit; prompt fields equal `describe_payload`; a grant prepared with a non-null `secret_hash` is refused, and the preparer cannot obtain the secret (F1); `human.get` returns every invariant byte (§0.8.1) identical to `human.prepare`'s, and differs only in `actor.device` and `auth` after binding (F5); `verdict pass` and `verdict fail` on one gate have different fingerprints (N2); a prepared request with an event type outside `EMITS_V1[op]`, a second `log`, another `based_on` or a foreign `actor.id` is refused by the signer before any prompt (N4); `secret_hash` is set aside only on `grant.issued`; replay precedence (identical signed events give the stored result, other events give `human.request_state`); every state transition of the table; the `Review` (payload and derived) recomputed by the signer for every person event type; an unknown payload key is shown (L2); a section text whose `section_hash` differs from the event is refused; a `settings.set` `before` key outside `set` is refused; a crash between two appends of a multi-event submit is rolled forward at start and ends `committed`; human `submit` replay precedence against missing, conflicting, expired and fresh idems, with the fresh idem getting its own record; submit from another workspace's socket gives `not_found`; a multi-event request is all or nothing | Opus |
| **H5b Hand-off** | codes, `human.pending` (by code and by request id), device binding in `human.get`, `orch sign CODE` | a successful hand-off sign end to end; `orch sign` shows the workspace from its own registry, the op header and the fingerprint before content; binding to a second device is refused; failed lookups on one connection do not lock out another (F5); the `pending` states `prepared`, `submitted`, `cancelled` and `expired` (R6) | Opus |
| **H6a Live updates** | journal, `jpos`, `sub.*`, revalidation, cursors, end reasons, `wait` on the bus, foreign-append rebuild | a revoked grant and a removed person stop delivery, queued items included; resume with no gap after a host restart; overflow, then snapshot, then cursor; a write's `res` before its `evt`; the journal is rebuilt after a foreign append (F11); one `jpos` yielding an event, `gone` and `needs.changed` gives three distinct ids in one `evt`; a fully filtered `jpos` still advances the cursor; `gone` is sent only to a viewer who saw the ticket | Opus |
| **H6b Dashboard reads** | `ticket.query`, `ticket.get`, `needs`, `activity.list`, `workspace.show`, `member.list`, `session.list`, `grant.list`, `move`, `policy.set`, `settings.set` | paging stays consistent under appends (no repeat, no gap); restricted rows hidden; `needs` equals `model.needs`; `ticket.get` evidence digests match the manifest; `grant.list` hides `secret_hash`; `move` allowed and refused per its rules; **v1.4:** `restricted`, the task and criteria counts, `blocking_questions`, `claim` and `closed` on rows are absent or never counted for a restricted ticket the viewer cannot see (`total` unchanged by it); `restricted` is `true` exactly for non-`workspace` rows the viewer may see; the new `needs` fields for `question`, `approve`, `no_eligible`, `acknowledge` and `addon` equal `model.needs` (with `who` limited as above) and the question or rule text, and `since` follows the table in §0.8; every new `ticket.get` field equals the model view (a question's options and answer, a receipt's `evidence`, gate `approved_by` and `decisions`, `verdict.current`, the manifest against `artifact.get` digests, `claim.live`, `source` against the latest `branch.pushed`), and `LogItem.data` holds only the fields in its type table (no `sig`, `host_sig`, secret or text beyond `Snip`); `sections` holds hashes only; a closed ticket: `ticket.get`, `diff`, `artifact.get` and `sub.open {ticket}` succeed for a viewer who may see it and give `not_found` for one who may not, `ticket.query` leaves it out without a `status` and returns it with `["closed"]`, `needs` has no `approve` for it, and a reopened ticket loses `closed`; `activity.list`: every item filtered on its own (an agent grant or `no-person` never gets a restricted ticket's events or any workspace-log event, a non-owner gets only their own `grant.*` events and none of another person's), `next_page` and page fullness without holes, no `total`, a token with other arguments is `invalid.input`, an expired token is `page.expired`, a ticket that becomes visible between pages adds nothing to the snapshot, and one that becomes invisible is dropped from later pages; a revoked grant or a removed person on page 2 is filtered as the new viewer; a forged, altered or other-principal `page` token (also for `ticket.query`) is `page.expired`, and a token with other arguments is `invalid.input`; workspace-log `data` holds only the fields of its table; a `viewer`-role person gets member, role, device, policy and settings items with the table's fields only, and no `grant.*` item; `needs {person: X}` by an owner who is not on a restricted ticket's list returns none of X's items on it; `who` and `eligible` never name a person who cannot see the ticket, and a gate whose approver tokens no visible person holds is `no_eligible` with `cause:"no_visible_person"`, a gate with no token has `cause:"no_token"`, and `blocked` stays F1's token test; `duplicate_of` naming a ticket the viewer cannot see is returned as the key only, in `closed`, `LogItem.data` and the feed, and `ticket.get` on it is `not_found`; the `_more` flags and `_cut` siblings are set exactly when something was cut, the first page's `sub_cursor` joined to `sub.open` loses and repeats nothing, `settings.changed` shows key names only; `read_ext/1` and `activity/1` listed in `welcome.caps` | Codex, Opus (visibility of the new fields) |
| **H7 Turn host mode on** | the default for registered workspaces; full parity; cross-version fixtures (an older client against a newer host) | full golden parity including human ops and `wait`; T2 in both modes. Needs H10 for `verdict` and `approve code` parity (clean-tree check). | Opus |
| **H9 Touch ID signer** (D-P2-22) | Swift helper, `secure-enclave` backend, native header and review, dashboard integration | recomputes hashes; refuses non-person labels; F1 vector interop; cancellation (window closed or Touch ID refused) submits nothing and leaves the request `prepared`; same fingerprint as the dashboard | Opus |
| **H10 Worker and jobs** | `orch worker`, `job.*`, leases, containment, addon runner, `tree.check` | the addon cannot read the WSK; a double-forked escapee dies within 30 s while a sibling job completes; a reparented child in a live job's group survives the sweep (F14); a repo with `filter.*.clean`, `core.fsmonitor` or `core.hooksPath` spawns no process in `tree.check` (F2); a second process under the worker UID is refused while the worker is connected, and a `job.done` with another connection's lease is refused (F3); lease expiry and retry at most twice; cancellation through heartbeat; a job that kills the worker and connects as worker class during the restart throttle is refused, and the restarted worker gets the role (N1); `job.done` replayed after a worker reconnect and after a host restart answers `{}` when lease and canonical digest match the stored completion, and `worker.lease` otherwise (including a different error message); a `void` lease is refused; a worker-UID connection to a workspace socket is refused; jobs of two workspaces are served round-robin over the one worker connection; a FIFO at a tracked path counts as changed without blocking, and a symlink is compared by its target (N3); a tree dirtied between prepare and `human.get` is refused at `human.get` (N3) | Opus |

- **Order:** H0, then H1a → H1b → H2a → H2b → H3a → H3b1 → H3b2 → H3b3 → H3c → H4a1 → H4a2 → H4b → H5a → H5b ∥
  H6a → H6b → H7. H9 follows H5b. H10 runs in parallel from H4a1.
- **Cutover gate (D44):** H7, H9 and H10.
- **Stream B** uses a stub of §0 now and the real host from H4a1, with `orch host run --foreground` on a demo
  workspace.

**H0: the F1 and spec amendments.**

1. **§10.7 receipts:** client-run, proving a claim, not execution. H3b2 (client receipts) adds
   `attested_by: "agent"`; H0 adds no receipt field. Replaces "the P2 host runs it itself".
2. **§10.8 clean-tree check:** run once, before the prompt, by the worker's reader, and shown as best effort.
   `precommit` re-checks the branch and commit under the lock.
3. **§5.1 event ids:** host-prepared in P2; the signer never chooses or changes them.
4. **`ticket.moved`:** a person event (owner, maintainer) with `{from, to, reason?}` and the `move` rules (§0.8).
5. **`policy.set` and `settings.set`:** record the two ops of §0.8 (argument schemas fixed there) for the existing `policy.changed` and
   `settings.changed` events.
6. **`repo.remote_mismatch`:** live where `settings.repos.<r>.remote` is declared (F7).
7. **§8.1 `serve_http`:** a launcher policy switch. An addon whose granted manifest has it may be started as a
   long-running local HTTP service under the person's UID (the dashboard). It is not a runner capability.
8. **orch-v2-portable-custody.md §3:** the dashboard's session is a bearer token in a header, not an HttpOnly cookie
   (R10).
9. **D64:** WebAuthn deferred past P2. It needs a new signature contract.
10. **orch-v2.md §5.5:** the macOS custody row becomes a 0600 file owned by the host UID (D-P2-17).
11. **Reserved ops** (`comment`, `ticket.discard`, `artifact.search`, `view.*`) marked as not served.

## 9. Open questions

1. **Windows (D63).** The proposal is to defer the named-pipe `HostTransport`; Windows stays in unmanaged direct mode
   (tier A).
2. **A dedicated host user on macOS.** Not in P2; the peer map already supports it.
3. **Own reader; dulwich is rejected** (it honours grafts, alternates and shallow, and adds a dependency).
4. **An issuer-chosen UID restriction on grants.** Not in P2; it could come later as an optional `grant.issued` field.
5. **To confirm during H3b1/H10:** that macOS has no subreaper and reparents to launchd (H10). **Reftable (H3b1,
   git 2.45+):** `core.repositoryformatversion = 1` with `extensions.refStorage = reftable`, a `reftable/` directory in
   the `commondir`, and a stub `HEAD` (`ref: refs/heads/.invalid`). The reader refuses on the extension and on the
   directory, so either alone is observe.unavailable.
6. **H10 settles** the macOS source of the worker's main PID: `launchctl print system/io.orch.worker` if a non-root
   host can read it, otherwise the root-owned pid file written by the setup helper's wrapper (N1).
7. **H5a settles** that a multi-event submit is judged and appended under one held store lock, with no other append in
   between. The roll-forward recovery of §0.8.1 relies on it, and `Projection` already does this in P1.
8. **v1.5 (from the dashboard stream's M2 gap table and its comment on #378; not in v1.4).**
   - A **settings and policy read op** (or fields on `workspace.show` under `read_ext`): `grant_hours`, `claim_ttl_min`,
     `lease_ttl_min`, `repos`, and the workspace default gate policy, for M4's read-only settings and for a claim's expiry.
   - **Full texts** beyond the `Snip`: comment, change-request and verdict texts (for example `ticket.get {item}` or a
     `log.item` op).
   - **Which hashes `human.prepare` binds** and the dashboard may show: the full question hash, the gate hash and
     `source_sha` (today only `hash12`).
   - **Not expressed yet:** `via: factory_charter` on approvals (D62, P2); `revoked.by` on grants; grant `sessions` as ids
     rather than a count; agent refusals as session data (F1 stores no event for them); a workspace-wide artifact listing.
   - **The live-tail payload:** subscription items carry no actor, summary or data, so a feed refetches instead of merging.
     Security pointer (Low): §0.9 filters items by ticket visibility only, not by workspace-log type, so
     `sub.open {topics:["workspace"]}` would deliver `grant.*` items (type and actor only) to any viewer, `no-person`
     included. The v1.5 live-tail work must apply the per-type rule of `activity.list` (the `grant.list` rule for `grant.*`,
     nothing from the workspace log for `no-person`).
   - `diff` with several heads or bases per repo, and commits (F1 stores only `branch.pushed`).
9. **H3b1 follow-ups (#381 reviews).**
   - **`host_pinned` base** (§10 entry 6, hole 4): the host keeps the first-seen merge base per (ticket uid, repo
     identity) in its state dir (0600, never moved forward automatically) and reports `base_moved`. P3 replaces
     first observation with a base pinned at claim.
   - **`since:"my_approval"`** (holes 1, 2): only the caller's own code-gate approve decisions, in the dispatcher
     when `diff` is registered (H6b); the default view stays the full change.
   - **A `declared` base is never the approval screen's default** (hole 5): a client rule, noted in §0.8.
   - **SHA-1 collision detection:** the reader checks SHA-1 ids with plain SHA-1, not git's SHA-1DC, so a
     SHAttered-style colliding object would be accepted. Accepted risk at today's cost; sha256 repos are unaffected.
     Add the known collision-disturbance check, or prefer sha256 repos.
   - **Rename detection** (§0.8 keeps `R` and `old_path` for a later version).

## 10. Decisions record

**1. How the host reads git (D-P2-19).**

- **Codex:** Observe the repository in an isolated, unprivileged helper over controlled snapshots.
- **Draft:** Use an in-process Python reader that ignores config; move the best-effort clean-tree check to the worker
  UID.
- **Tie-break:** Accepted orch's own reader in a bounded child, with config read as data, a pinned gitdir and object
  limits. The host checks the branch before the prompt and in `precommit`, and the worker checks uncommitted changes
  before the prompt only.
- **Security review (F2, F7):** the worker's check uses orch's own index and worktree reader, never git. Identity,
  pins, refs and extensions are tightened.

**2. Containing jobs on macOS (D-P2-20).**

- **Codex:** Give each job its own containment so killing one cannot kill unrelated jobs.
- **Draft:** Per-job cgroups on Linux; on macOS process groups and a whole-worker-UID sweep, accepting that siblings
  may die.
- **Tie-break:** Accepted delegated per-job cgroups on Linux. On macOS, process groups and an orphan-only sweep that
  preserves siblings.
- **Security review (F3, F14):** one worker connection with leases bound to it; a stop-check-kill sweep that never
  runs as root.

**3. Review round of 11 Oct (security review F1–F15, Codex clarity review 1–14, dashboard R1–R11).** Every point was
adopted, and none is disputed. Two needs were met in a different shape than asked:

- **R1 (structured ticket data)** is the new read `ticket.get`, not a change to `show`, so the `show` goldens stay
  byte-identical.
- **Codex 9 (import flow)** is answered by keeping `import v1` in direct mode before registration, because it reads v1
  files and signs a batch, neither of which fits a socket op.

**4. Review round 2 of 11 Oct (security re-review N1–N5, Codex clarity re-review 1–12, orch-relay §9).** Every point
was adopted, and none is disputed. Two choices were made where the reviews left them open:

- **`policy.set` and `settings.set`** are defined now, instead of being marked "not in v1.2". Both map one-to-one onto
  existing F1 events, so no format change waits on them.
- **Worker routing** uses a dedicated worker socket, instead of letting the worker connect to every workspace socket,
  so "one worker connection per UID" stays literally true.

**5. Final round of 11 Oct (Opus diff check M1, L1–L3; Codex re-check 1–8).** Every point was adopted, and none is
disputed. Two choices were made where the reviews left room:

- **Human multi-event submissions roll forward** after a crash, using the signatures already stored, instead of
  ending `partial`. A person never has to sign the rest again.
- **`settings.set`'s `before`** is shown as unsigned host values. `settings.changed` carries absolute values and no
  `base_rev`, so the signed `set` is the whole effect.

Two items wait for their groups (§9 Q6: H10, Q7: H5a).

**6. Who chooses the diff base (#381 security review, finding 4).**

- **Problem:** §0.8 took the base from the working copy's `main`, which the agent writes. Moving `main` forward, deleting
  it, or committing on an orphan branch made `diff` show only the last commits while a person signed a head containing
  all of them.
- **Draft (orch-52):** prefer the newest signed `source_sha` as base, else the local ref, labelled; show base and count.
- **Security review:** a verdict's sha was judged on tests, not code, and a second reviewer never saw the first one's
  range; the missing-`main` and orphan fallbacks were silent; nothing host-held resisted a per-review move.
- **Decision:** the default base is host-pinned at first observation (host state), else the local merge base labelled
  unverified with `base_moved`, else head's first parent labelled "only the last commit"; a declared default branch
  that is missing is `observe.unavailable`. "Since my approval" is an explicit option over code-gate approvals by the
  caller only. The approval prompt shows the base, its source and the commit list. D58 is unchanged: the signature binds
  the head; the base is display. P3 replaces first observation with a base pinned when the ticket is claimed.


## 11. P3: the host↔relay bridge (input from orch-relay §9)

orch-relay's `docs/p3-relay-design.md` §9 defines what the host owes the relay in P3. None of it is a P2 group, but
**H2 (lifecycle, outcomes) and H4 (dispatch) must not block it**. P2 reserves the following on the host side.

- **A durable outbox per append (P3-D21).**
  - Under the store lock, in the same transaction as an append, the host writes an outbox entry: the exact
    checkpoint, and for a `restore` the full evidence set (the source C, the target T, the signed restore, the
    certificate, the delegation) and the proof material.
  - After the lock is released, a publisher sends entries **in order**, retries with backoff, and deletes an entry
    only after a 200.
  - **The store lock is never held across networking.**
  - A restore's entry comes right after the source it names, and normal checkpoints queued behind it wait.
  - Membership bodies are not outbox entries (see membership publication below).
  - P2 reserves this as a hook in the append transaction of D-P2-3 (next to the outcome record), plus a directory
    `<state>/hosts/<ws>/outbox/` in the D-P2-17 custody list.
- **Person-event and decision ids admitted** are committed to host state in the same transaction as their admission,
  as tombstones that survive a restore. H2b's outcome store must leave room for that table.
- **The holder lease (P3-D10).**
  - The bridge takes a holder generation with `host/hold` and carries `Orch-Holder: <gen>` on every relay write.
  - The lease is 40 s, renewed every 15 s by a poll or by `host/hold`. A stale generation gets `fenced`, and the host
    then stops writing and re-holds only by explicit takeover.
  - In P2 this means one bridge task per workspace inside the host process (D-P2-1), with its own timer that never
    runs on the workspace worker. A drain (D-P2-4) releases nothing early: the lease simply runs out.
- **Staged checkpoint publication.** The bridge sends staged pages (at most 4 MiB each, at most 2 open sets and
  64 MiB per workspace), then one final `PUT checkpoint` that commits atomically. At startup it compares the relay's
  held checkpoint with the logs. A relay checkpoint the host's logs don't contain (the relay is ahead of the host, or
  on another branch) means the host was rolled back: that is `chain.diverged`, and the host refuses appends until an
  owner restore, using the same "degraded" machinery as D-P2-12. A mismatch with a checkpoint still queued in the
  host's own outbox is not that case; the outbox rules below decide it. Devices are shown only checkpoints the relay
  has accepted.
- **Checkpoint capacity (P3-D20).** The host refuses `ticket.created` when the workspace checkpoint nears its size
  capacity. P2 reserves the check; the limit comes from the P3 measurement (G4), about 5000 tickets for now.
- **First link to a relay (P3-D18).** The bridge sends `PUT card` and `PUT genesis`. The genesis pin is signed by an
  owner device, which is one human step when the workspace is first linked. Reauthentication is single-flight.
- **Card changes.** The workspace exchange key (WXK) rotates every 90 days: `PUT card`, then `PUT card-keys`, with
  `card_seq` strictly rising.
- **Serving devices.** The host advances its poll cursor (`after`) only to `n`s recorded in its request-id store; its
  sequence window and that store are committed atomically. Once the rotation gate is up, nothing is sealed under the
  old epoch.
- **The restore flow.** Before preparing a restore, the host fetches the revocations the relay holds, so the relay's
  completeness check (R4) can't refuse the signed restore. No outbox flush is required. When the relay refuses the
  host's own queued checkpoints as diverged, the host shows the conflict and takes the relay's held checkpoint as the
  restore's `source`. On `restore_source_mismatch` the owner signs again.
- **The pairing baseline pin.** The authenticated `approved` pairing answer carries the host's exact held checkpoint
  `{n, hash}` (or `null` only when none exists). The host registers it with `POST baselines` (7 days) and releases it
  when the pairing ends. A baseline is never downgraded. The host keeps the checkpoints that baselines name.
- **Membership publication (P3-D7, Q10).** It runs **outside the checkpoint outbox** and never waits behind it. A
  content-refused rotation must not block checkpoints, and a `chain.diverged` checkpoint must not block a security
  rotation. The signed list carries the required field
  `wk_commit = b64u(H("orch/v2/wk-commit|" || workspace_id || u32(epoch) || WK_E))` (32 bytes, canonical b64u). A list
  without it, or with an ill-formed one, is refused `malformed`. Every list at one epoch carries the same `wk_commit`,
  and the host never signs a different one for an epoch. Rules:
  - **One WK per epoch, ever.** The host never creates a second key for an epoch number. Epochs may skip, never fall. Epochs are u32 (1..2^32-1). The
    relay accepts a later list only with `prev <= epoch <= prev + 2^16`. The host alarms, and clamps: it never allocates
    an epoch above `committed + 2^16`, whether through repeated burns or H7 jumps. The rotation target is `min(EHW + 1, committed + 2^16)`: the clamp wins. When the clamp is at or
    below `EHW` (every epoch up to it is already used or burned), the host cannot rotate without a second key for an
    epoch, which it never creates: it alarms, keeps the rotation gate up and allocates nothing. The workspace's
    membership is then frozen: no rotation and so no member removal. The only ways out are the relay committing a
    body that is already out (which raises `committed`), or an operator action outside this protocol (P3 owner-run
    recovery).
    Liveness limit, accepted in v1.4: reaching it needs about 65,000 burns, or about 63 H7 jumps of 1024 with no
    commit in between. A list that removes a member has a
    higher epoch than the previous list. A list at the same epoch carries the same `wk_commit`, else the relay answers
    409 `wk_commit_changed` (the relay checks list rules before grants, so a same-epoch re-seal with a changed
    `wk_commit` always gets this code; a divergence refusal under H4).
  - **First list.** The first publication for a workspace may use any epoch `1..2^16` and any `list_seq >= 1`. A refused
    first publication burns its epoch and `list_seq` like any other, and the retry uses (2,2) or higher.
  - **Allocation.** The host allocates `list_seq` and epoch from durable high-water marks (`LHW`, `EHW`). It never
    derives `list_seq` from `roster_v` or from the log. `roster_v` can repeat after a log `restore`. `list_seq` never
    falls or repeats. Gaps are legal: after a burn the next list uses `LHW + 1`. `LHW` is the highest `list_seq` ever
    signed, not the committed one.
  - **H1/H2, persistence before the first send.** The host writes the allocation, the signed list, the grants, the
    holder set and `WK_E` in one atomic write under `<state>/hosts/<ws>/membership/`, with keys under `keys/`, both in
    the D-P2-17 custody list. At most one body is exposed at a time. Exactly one writer: the fenced holder.
  - **H3, transient outcomes** (lost response, connection error, timeout, 429, 5xx, 401 `unauthenticated` or `expired` then re-login (a 401 `bad_signature` is a content refusal, below),
    `fenced` then explicit re-hold) resend the exact bytes. A 429 does not burn the body. After a `fenced` and an
    explicit re-hold, or a re-login, first `GET members`. If the verified list is above `LHW` or `EHW`, or is not a body the host
    exposed, apply H7. Otherwise resend the exact bytes.
  - **H3, container-only refusals** (`stale_card`, `wrap_conflict`). The exposed record keeps the signed list and
    grants immutable and the card-key wraps replaceable. The exact-bytes rule applies to the signed parts. The host
    resends the same signed list with new wraps.
  - **H4, burning.** Any other refusal, or a list change while a body is out, supersedes the body.
    Content refusals: `revoked`, `cert_expired`, `cert_invalid`, `other_person`, `scope_exceeded`, `malformed`,
    `bad_signature`, `other_workspace`, `missing_grant`, `not_for_this_device`. Divergence refusals: `stale_list`,
    `bad_epoch`, `rotation_required`, `grant_conflict`, `wk_commit_changed`. A superseded body: its `list_seq` and
    any new epoch are burned. Stop sending the old body before persisting the burn. Persist the burn and the new
    allocation in one atomic write. Never resend a burned body. Do not adopt a burned body that the relay later shows as
    committed: its burn may have been caused by excluding a holder.
  - **H4, the epoch floor.** After burning epoch `E`, every later list has epoch > `E`. If the list needs no rotation,
    it still rotates to `EHW+1`. Otherwise a non-rotating list at the committed `e < E` would be refused `bad_epoch`
    after a body at `E` might have committed.
  - **Burned-WK custody.** A burned `WK_E` is stored under `keys/` like an old epoch key, is never active, and under it
    the host opens requests only to answer the §8.5 `stale_epoch`. It never seals a data response, a `drop_rewrap`
    output or any other content under it. It is deleted under the same retention as old epoch keys. For §8.5 it
    counts as a held key.
  - **H5, holder rule.** A list at epoch `E` includes every device that was sent `WK_E` in any exposed body.
    Otherwise the next list uses a new epoch. The rotation gate (no new sealing under `E`) goes up as soon as the host
    decides to drop such a device. This covers supersession and F1 log restores. A restore reaches the publisher as a list change. If it abandons a `member.added`
    whose device got a grant, the rotation is correct.
  - **H6.** The host never signs two different `cj(o)` under one `list_seq`. It never re-signs: it resends persisted
    bytes.
  - **Rotation gate.** Revocations learned from the relay raise the rotation gate at once, durably.
  - **H7, host-state loss.** The host first does `GET members` and verifies it with the WSK, then sets `LHW := max(local, relay list_seq) + 1024`
    and `EHW := max(local, relay epoch) + 1024`, and rotates at once to `EHW + 1`. A takeover by an instance without the latest membership store is host-state loss too. This is not the F1 `restore` event, which
    does not touch `hosts/<ws>/`. It is the same pattern as the checkpoint `n` high-water mark, with a margin. Treat as
    host-state loss: the membership store missing, unreadable or torn while the WSK exists; the relay's verified list
    above `LHW`/`EHW`, or at a `list_seq` with content the host never exposed; no local `WK` for the relay's committed
    epoch; or the relay's held checkpoint `n` above the durable checkpoint `n` high-water mark (#377; the startup comparison above). Stated limit: a
    self-consistent restore of all of `hosts/<ws>/` whose state still matches the relay goes undetected. The holder
    sets of bodies exposed after the backup are then lost.
  - **H8, alarms.** Count the content and divergence refusals named under H4 only. Transient outcomes neither count nor back
    off. The count resets on a 2xx, and the alarm fires after 3 consecutive such refusals.
    Also alarm when a rotation stays uncommitted for 10 minutes for any reason, including transient errors. A hostile
    relay can 5xx forever. The alarm shows in `host.status`. The host backs off exponentially, up to 5 min, and each attempt burns at most one epoch. Backoff never lowers the rotation gate.
  - **Host vectors** (there is no P3 row in §8). The nine from the relay's Q10 design:
    - `request_under_burned_epoch_key_gets_signed_stale_epoch`
    - `refused_rotation_burns_epoch_and_list_seq`
    - `refused_first_publication_retries_at_2_2`
    - `stale_card_resends_same_signed_list`
    - `same_epoch_add_refused_revoked_forces_rotation`
    - `crash_after_writeahead_resends_exact_bytes`
    - `restore_jumps_1024_and_rotates` (host-state loss, H7)
    - `accepted_but_reported_refused_converges`
    - `three_refusals_alarm` (counts content and divergence refusals only, H8)

    The 14 extra ones from the core check:
    - `list_change_while_exposed_supersedes_once_old_body_never_resent`
    - `burned_epoch_then_add_member_rotates_above_burned`
    - `superseded_same_epoch_add_dropped_forces_rotation`
    - `fenced_rehold_relay_ahead_triggers_state_loss`
    - `store_missing_with_wsk_triggers_state_loss`
    - `relay_checkpoint_n_above_high_water_triggers_state_loss`
    - `burned_key_request_gets_only_stale_epoch` (also checks that no data response is sealed)
    - `wrap_conflict_then_revoked_burns_once`
    - `transient_refusals_do_not_count_toward_alarm`
    - `rotation_uncommitted_10min_alarms`
    - `ws_restore_abandoning_member_added_rotates`
    - `list_seq_independent_of_roster_v_after_log_restore`
    - `workspace_show_epoch_is_committed_not_ehw`
    - `wk_commit` value vectors, consumed by the core signer
- **The `bridge` peer class.** Reserved in §0.5.
  - In P3 the bridge client runs **inside the host process**, so it needs no socket class to reach the store.
  - The `bridge` class is kept for a bridge split out into its own process. That process has no person mapping, and
    every remote person request carries that person's own signature. Remote device requests reach the registry
    through the same dispatcher (D-P2-9) as `human.*`-style signed events, never as a person-class peer.
- **Agents never see relay tokens** (§5.5): the bridge's session tokens live in host-private state, outside every
  agent-readable path.
- **In-host or out-of-process: a deliberate P3 choice (L3).** Running the bridge inside the host means parsing relay
  responses in the process that holds the WSK. D-P2-7 kept FastAPI out of that process for the same reason. P3 chooses
  on purpose between the in-host bridge and the reserved out-of-process `bridge` class, and records why.


