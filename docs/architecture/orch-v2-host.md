# orch v2 P2: the host process (#325, #271, #287)

Status: Decided 11 Oct 2026 (draft by orch-52, Codex challenge, Opus tie-break on two points), revised the same day
after the Opus security review (F1–F15), the Codex clarity review (14 points) and the dashboard stream's requests
(R1–R11), and again after the security re-review (N1–N5), the Codex clarity re-review (12 points) and orch-relay's P3
host interface (§11), and finally after the Opus diff check (M1, L1–L3) and the Codex re-check (8 points). The socket
API contract §0 is **v1.5 (provisional draft)**: v1.4 added the dashboard's read fields, the activity feed and the
closed-ticket rules (amendment of 11 Oct 2026), and v1.5 answers §9 item 8 (settings read, full texts, bound hashes,
grant and session fields, the live-tail payload with its workspace-topic filter fix, artifact and commit listings;
§0.8.5). §0.8.6 adds click-to-sign (H5c, D-P2-23, owner decisions of 12 Oct 2026; built, off by default per workspace). **v1.6** (§12, decided 12 Oct 2026, with three owner decisions) adds the phase-2
addons on the host side: the runner, terminals, start agent, worktrees, records and quick tasks (caps `run/1`, `term/1`,
`worktree/1`, `records/1`, `quick/1`, and `runner/1` on a new runner socket). Changes go through the coordination log.

Built on `origin/develop` 908f2511. Decisions are numbered **D-P2-n**, each with a one-line reason.

---

## 0. Socket API contract v1.5 (provisional draft)

§0 is complete on its own. Streams B (dashboard), C (mobile, from P3) and D (relay bridge) build on it without reading
the rest. Everything in §0 is normative: a host or client that differs from it is wrong.

### 0.1 Notation

The schemas below use a compact notation:

| Notation | Meaning |
|---|---|
| `k: T` | required key |
| `k?: T` | optional key: it may be absent and is never `null` |
| `k?: T \| null` | optional key; when present it may be `null` (v1.5) |
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
    full text is read with `ticket.text` (v1.5, cap `text/1`, §0.8.5), addressed by the event's `seq` and the field name.
  - `Source` = `{repo:str, ref:str, sha:str}`: one entry of the F1 §5.7 source list (`repo` is the repo identity)
  - `Sha` = `str/[0-9a-f]{40}|[0-9a-f]{64}/`: a git commit id (SHA-1 or SHA-256 repository) (v1.5)
  - `RepoName` = `str/[a-z0-9][a-z0-9._-]{0,63}/`: a `settings.repos` name (v1.5; the `settings.set` key rule)
  - `ActivityItem` = §0.8 `activity.list` (v1.5 names it here because `sub.open {detail:true}` delivers it too)
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
  - One runner socket, `<run>/runner/host.sock` (v1.6, §12.3.3; tier A: directory 0700, host UID; tier B: 0750, group
    `orch-runner`, socket 0660), for one runner connection per runner UID, accepted only from the runner unit's main
    PID. The runner's own terminal socket `<run>/term/<runner uid>/term.sock` is not a host socket (§12.6.1).
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
           fix?:{argv:[str]{1..32}}, retryable:bool, retry_after_ms?:int 1..3600000,
           detail?:{state?:str, which?:str, reason?:str, missing?:str, next_key?:Key}}   // v1.9, closed per code (below)
Evt     = {t:"evt", sub:SubId, cursor:Cursor, items:[Item]{0..64}, end?:EndReason}
Fatal   = {t:"fatal", error:Error}
OpName  = str/[a-z][a-z_]*(\.[a-z_]+)*/      ReqId = str/[A-Za-z0-9_-]{1,32}/
```

- **`Result`** is `orch.ops.Result` in full. The CLI renders text from it with its own templates, so P1's golden
  output is unchanged.
- **`Error.detail` (v1.9, #479)** is optional and **closed per code**: only the five codes below may carry it, only the
  keys listed for the code, and only a closed value or a `Key`-format string; never free text, a path or a secret. The
  host drops any other code, key or value before it sends. A client ignores unknown keys (§0.4), so this is additive.

  | Code | `detail` keys (all optional; a missing key means the host did not know) |
  |---|---|
  | `term.not_attachable` | `state`: `starting`, `running`, `exited` or `lost` |
  | `run.limit` | `which`: `agent_ws`, `agent_person`, `shell_person` or `runner` |
  | `launch.no_grant` | `reason`: `none`, `expiring`, `scope` or `revoked` |
  | `runner.disabled` | `missing`: `addon`, `machine` or `terminals` |
  | `ticket.key_not_next` | `next_key`: the store's next ticket key (advisory, never a reservation) |

  The `blocked` object of a preview (§12) keeps its own `detail` with the same keys; it is part of the result, not of
  an `Error`.
- **`Error`** is the F1 §10.4 item 4 body plus the optional `retry_after_ms` and `detail`. The `exit` member of an error `Res` is
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
| `read_ext/2` | no ops of its own (v1.5): a host that lists it sends **every** optional field that v1.5 adds to `ticket.query`, `ticket.get` and `grant.list` (§0.8.5), and also lists `read_ext/1` | workspace |
| `settings/1` | `workspace.settings` (1) (v1.5) | workspace |
| `text/1` | `ticket.text` (1) (v1.5) | workspace |
| `sub_ext/1` | no ops of its own (v1.5): `sub.open` accepts `detail` (§0.9) | workspace |
| `artifact_query/1` | `artifact.query` (1) (v1.5, later than H6b) | workspace |
| `diff_ext/1` | `commit.list` (1); `diff` accepts `since`, `head` and `base` (v1.5, later than H6b). H6c lists the cap and serves the `diff` arguments only: a client checks `welcome.ops` for `commit.list` | workspace |
| `refusals/1` | `session.refusals` (1) (v1.5, later than H6b) | workspace |
| `activity/1` | `activity.list` (1) | workspace |
| `diff/1` | `diff`, `artifact.get` (1) | workspace |
| `quick/1` | `quick.list` (1) (H11f) | workspace |
| `human/1` | `human.prepare`, `human.get`, `human.submit`, `human.pending`, `human.cancel` (1) | workspace |
| `sub/1` | `sub.open`, `sub.close` (1) | workspace |
| `blob/1` | `blob.put`, `blob.drop` (1) | workspace |
| `outcome/1` | `outcome.get` (1, workspace socket); `host.outcome` (1, control socket) | workspace, control |
| `job/1` | `job.next`, `job.heartbeat`, `job.done` (1) | worker |
| `control/1` | `host.list`, `host.register`, `host.unregister`, `host.wsk_create`, `host.bootstrap`, `host.restart` (1) | control |
| `run/1` | `run.sub`, `run.list`, `agent.preview`, `agent.start`, `run.stop`, `grant.deposit`, `grant.deposit_status`, `grant.forget` (1) (v1.6, §12.3) | workspace |
| `term/1` | `term.preview`, `term.open`, `term.attach`, `attach.cancel` (1) (v1.6, §12.6) | workspace |
| `worktree/1` | `worktree.list`, `worktree.create`, `worktree.remove` (1) (v1.6, §12.7) | workspace |
| `records/1` | `records.status`, `records.snapshot`, `records.export_check`, `records.note`, `records.remote_private` (1) (v1.6, §12.8) | workspace |
| `runner/1` | `runner.hello`, `runner.report`, `runner.exit` (1) (v1.6, §12.3.3) | runner |


  - **`welcome.ops` decides per op.** A cap may be listed while only some of its ops are served (H4a2 lists `diff/1`
    for `artifact.get` before `diff` is served); a client checks both the cap and the op in `welcome.ops`.
  - A call to an op the host does not list answers `unknown_command`.
  - A call to an op listed at a different version than the client's answers `op.unsupported`. Other ops keep working.
  - A version is bumped only when the op's input, output or errors change incompatibly.
  - **v1.4 adds only optional keys** to results that existed in v1.3 (a client ignores unknown keys, so an older client is
    unaffected), so `ticket.query`, `ticket.get` and `needs` stay at version 1. A client that wants the new fields checks
    for the cap `read_ext/1`; a host without it may omit them, and the client then shows the field as unknown, not as zero
    or empty. `activity.list` is a new op at version 1.
  - **v1.5 is additive too** (§0.8.5). New result keys are optional and sent under `read_ext/2`; new ops start at version 1
    under their own caps; a new **argument** of an existing op (`sub.open {detail}`, `diff {head, base}`) may be sent only
    to a host that lists the cap naming it (`sub_ext/1`, `diff_ext/1`), because a host without it refuses the unknown key
    with `invalid.input` (§0.3). No existing key changes type: `grant.list` keeps `sessions:int` and `revoked_at` and adds
    `session_ids`, `sessions_more` and `revoked` beside them. The one behaviour change, the per-type filter of `sub.open
    {topics:["workspace"]}` (§0.9), only withholds items, so a v1.4 client keeps working and sees fewer items. A v1.4 client
    connected to a v1.5 host sees no difference except that.
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
| `runner` | the runner UID, on the runner socket only (v1.6, §12.3.3) | **one** connection per runner UID, only from the runner unit's main PID (as the worker's N1 rule); runner ops only; any other socket is `fatal` `peer.denied`. Its reports are untrusted and never establish authorization (D-P2-24). |
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
| `workspace.settings` (v1.5) | ✓ | ✓ | ✓ (fewer fields, §0.8.5) | — | — |
| `ticket.text`, `artifact.query`, `commit.list` (v1.5) | ✓ | ✓ | ✓ | — | — |
| `session.list`, `grant.list` | ✓ | ✓ (own person's only) | — | — | — |
| `session.refusals` (v1.5) | ✓ | ✓ (own person's only) | — | — | — |
| `job.next`, `job.heartbeat`, `job.done` | — | — | — | ✓ | — |
| `host.list`, `host.register`, `host.unregister`, `host.wsk_create`, `host.bootstrap`, `host.outcome`, `host.restart` | — | — | — | — | ✓ |
| `policy.set`, `settings.set`, `move` and the human registry ops | through `human.*` only (owners, or owners and maintainers for `move`) | — | — | — | — |
| v1.6 (§12.3.1): `run.sub`, `run.list` | ✓ (own person's runs) | ✓ (agent runs in the grant's scope) | — | — | — |
| v1.6: `agent.preview`, `agent.start`, `run.stop`, `term.preview`, `term.open`, `term.attach`, `attach.cancel`, `grant.deposit_status`, `grant.forget` | ✓ (own runs and grants) | — | — | — | — |
| v1.6: `grant.deposit` | ✓ **with** a verified `auth.grant` of a `label:"runner"` grant (§12.4.2) | — | — | — | — |
| v1.6: `worktree.list`, `worktree.create`, `worktree.remove` | ✓ (`force` P only) | ✓ (grant scope; create and remove only on the session's claimed ticket) | — | — | — |
| v1.6: `records.status`, `records.snapshot`, `records.export_check`, `records.note` | ✓ (owners, maintainers) | — | — | — | — |
| v1.6: `quick.list` | ✓ | ✓ | ✓ (workspace tickets) | — | — |
| v1.6: `runner.hello`, `runner.report`, `runner.exit` | — | — | — | — (runner class only) | — |


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
- `diff {repo}` and `commit.list {repo}` (v1.5) accept only a repo named in the ticket's `links.repos`.
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
- **What is returned is what F1 stores.** (v1.5 names three exceptions, each labelled as such: `diff` and `commit.list`
  read git through the host reader, `session.refusals` and `grant.list` `session_ids` read host records, and
  `workspace.settings` serves workspace-log configuration to N as an explicit exception; §0.8.5.) Nothing is derived from git beyond the `branch.pushed` events (no commit
  messages, authors or file lists: `diff` is the op for those), and nothing from a previous text of a section (F1 keeps
  hashes in the log, not old texts). No `sig`, `host_sig`, `secret_hash`, key, grant secret or signing byte is returned.
  A grant is named by its public id only.
- **The activity feed** (`activity.list`) filters each item on its own, as stated there.

- `session.list` and `grant.list`: a person-class owner sees every entry. Any other P sees their own. G sees its own
  person's. Claims on tickets the viewer cannot see are left out.

**v1.5 rules (§0.8.5).** Every v1.5 field and op follows the rules above (filter first, F1 data only, no secrets). In
addition:

- **Per-ticket reads** (`ticket.text`, `artifact.query`, `commit.list`, `diff {head, base}`) use exactly `ticket.get`'s
  visibility: a ticket the viewer may not see contributes nothing, and naming it is `not_found`.
- **`workspace.settings`**: P and G get the durations, policies, `*_at` and every repo's name and `default_branch`; repo
  `path` and `remote` only a person-class owner or maintainer (§0.8.5). N receives the three effective durations and four workspace default
  gate policies as an explicit exception permitting these workspace-derived configuration values. Its `repos` contains
  only repositories named in `links.repos` of at least one ticket currently visible to the viewer, each with only
  `default_branch`; `path`, `remote`, `settings_at` and `policy_at` are absent. Filtering occurs before serialization and
  counting. N receives no workspace-log events.
- **Session ids** (`grant.list` `session_ids`, `session.refusals`) follow the `grant.list` rule: a person-class owner sees
  every grant's sessions, any other P their own grants', G its own person's, N nothing. A session id is a public label (as
  in `session.list` and `Claim.session`), but the list of a grant's sessions is per-person data.
- **Refusals** (`session.refusals`) follow the same person rule; a ticket-scoped refusal is returned only if the viewer
  may currently see every associated ticket (§0.8.5); `key:null` never bypasses the ticket filter.
- **Subscriptions.** The `workspace` topic is filtered per event type exactly as `activity.list` (§0.9): this applies to
  every subscription, with or without `detail`.

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
| `peer.denied` | 3 | no | close | unmapped UID; second worker connection; a runner that is not the runner unit's main PID, or a second runner connection (H11a) | — |
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
| `lock.busy` | 9 | yes | stays | register waited 60 s for the ownership lock, or a hand-off prepare or submit / `orch host convenience on|off` waited 2 s for the per-workspace convenience lock (existing code; exit 9 on the socket, while P1's CLI catalog keeps exit 4 for its own store lock: H4b maps the socket's exit as sent) | yes |
| `host.busy` | 4 | no | stays | a second host on the same state or run directory (at start); `register` or `bootstrap` of a root or id that is already registered, has a genesis, or is being bootstrapped (N5) | — |
| `worker.lease` | 3 | no | stays | `job.*` with a lease that is unknown, expired or of another connection, or a `job.done` that differs from the stored completion (§0.8.2) | — |
| `control.rolled_back` | 9 | yes | stays | a stored answer only: a control intent rolled back at host start (§0.7); retry with a new idem | — |
| `repo.remote_mismatch` | 3 | no | stays | the observed identity differs from the declared `remote` (H0) | — |
| `sign.convenience_unavailable` | 3 | no | stays | `auth: "convenience"` at `human.prepare`, a `human.get` binding or `human.submit` while the workspace's flag is off or a reader is outside the condition; the message starts with the reason `disabled`, `relay_paired`, `other_member`, `phone_device` or `reader_version` (§0.8.6 item 1) | — |
| `sign.click_only` | 3 | no | stays | a hand-off request (`human.prepare` with `handoff:true`, the code for `orch sign`) on a workspace whose click-to-sign flag is on; also `human.get` binding and `human.submit` of a hand-off request prepared before the flag was switched on, and `orch host convenience on` while hand-off requests are pending (the message lists them). The fix for a refused prepare, bind or submit is `orch host convenience off --workspace W`; for the refused `convenience on` it is to let the pending requests expire (10 minutes; a bound one counts a minute past its expiry) (§0.8.6 item 8) | — |
| `sign.convenience_readers` | 3 | no | stays | adding a reader (`member.add`; later relay pairing or a device; a records push to an unmarked URL, D-P2-34) to a workspace whose history has `auth: "convenience"` events or whose durable `convenience_seen` mark is set (#441); the fix names the readers to update (§0.8.6 item 1) | — |
| `runner.unavailable` | 9 | yes | stays | no runner is connected to the host (H11a, addons design D-P2-24) | 1000 |
| `runner.disabled` | 3 | no | stays | dashboard execution is off for the workspace on this machine, or the addon is not granted (D-P2-31) | — |
| `launch.cwd_changed` | 3 | no | stays | the pinned working directory of a run no longer matches (D-P2-29) | — |
| `harness.not_found` | 3 | no | stays | the program of a run does not exist (D-P2-28) | — |
| `run.spawn_failed` | 1 | no | stays | the runner's trampoline failed; the message names the step, never an environment value | — |
| `launch.invalid` | 5 | no | stays | a harness, mode, model or subagent model not allowed; the message names the field (exit 5 like `invalid.input`: the client sent a value the registry refuses) | — |
| `launch.changed` | 5 | no | stays | `agent.start`'s recomputed `spec` differs from the preview's (exit 5 like `idem.conflict`: the request no longer matches what was shown; preview again) | — |
| `harness.shell_unsupported` | 3 | no | stays | `$SHELL` is not POSIX-compatible and `login_shell` is on | — |
| `term.not_attachable` | 3 | no | stays | `term.attach` on a run that is `starting` or `lost`, or `exited` with no ring kept; `detail.state` names it | — |
| `run.limit` | 9 | yes | stays | a run limit of §0.11; `detail.which` is `agent_ws`, `agent_person`, `shell_person` or `runner` | when a run ends |
| `attach.limit` | 9 | yes | stays | an attachment limit (4 per run, 16 per person, 64 per runner) | when one ends |
| `term.writer_busy` | 3 | no | stays | a write attach while another attachment holds the lease and `take` is false (the term socket's `{t:"end", reason:"writer_busy"}` has the same meaning) | — |
| `host.limit` | 3 | no | stays | `relay.link` above the relay-linked workspace cap (8 per host, §11A.7; H12a). Not retryable: a slot frees only when another workspace is unlinked | — |
| `launch.claimed` | 3 | no | stays | `agent.start` on a ticket with a live claim | — |
| `launch.no_grant` | 3 | no | stays | no usable runner grant; `detail.reason` `none`, `expiring`, `scope` or `revoked` (v1.9: on the `Error` only where the host knows the reason; a preview's `blocked.detail` always has it) | — |
| `grant.forgotten` | 3 | no | stays | `grant.deposit` of a forgotten or replaced runner grant id | — |
| `grant.not_runner` | 3 | no | stays | `grant.deposit` of a grant whose `grant.issued` lacks `label:"runner"` | — |
| `grant.verbs` | 3 | no | stays | `grant.deposit` of a grant whose `verbs` is not `"agent"` (a narrower grant cannot run a harness) | — |
| `grant.expiring` | 3 | no | stays | `grant.deposit` of a grant with less than 15 minutes left | — |
| `event.expired` | 3 | no | stays | `relay.submit`: the event id's ULID time is outside [host now − 900 s, host now + 300 s] at job time (§11A.14 step W, after L3: amendment A; H12e `admission.known_or_fresh`). Definite, nothing landed (class C-review) | — |
| `event.abandoned` | 3 | no | stays | `relay.submit`: a restore abandoned the event with this id (in the replayed log's ids, not in its lines; §11A.14 L3; H12e `admission.dedup`). Class C-review | — |
| `ticket.key_not_next` | 3 | no | stays | `relay.submit` of a `ticket.created` whose `key` is not exactly the store's next key (§11A.14 L5; H12e `admission.check_target`); the `Error` carries the advisory `detail.next_key` (v1.9, §0.3; R2's relay answer may carry it too), never a reservation. Class C-auto | — |

`host.unavailable` (exit 9, retryable) is raised **only by the client**, when it cannot connect or the connection
drops. The host never sends it.

**v1.6 codes (§12).** Planned with the phase-2 groups H11a–f; each moves into the table above when its group is built
(the catalog test reads that table against `orch.host.errors`).

| Code | Exit | Retryable | Fate | When | `retry_after_ms` |
|---|---|---|---|---|---|
| `worktree.none` | 3 | no | stays | `worktree.remove` for a (ticket, repo) with no registered worktree | — |
| `worktree.exists` | 3 | no | stays | the ticket already has a worktree for that repo, or its path is not free (§12.7) | — |
| `worktree.no_branch` | 3 | no | stays | the ticket links no branch for that repo (§12.7) | — |
| `worktree.branch_in_use` | 3 | no | stays | the branch is checked out in another worktree (§12.7) | — |
| `worktree.dirty` | 3 | no | stays | the worktree has changes (or its state is unknown) and `force` is not set (§12.7) | — |
| `worktree.in_use` | 3 | no | stays | a live run has that worktree as its cwd (§12.7) | — |
| `worktree.unpinned` | 3 | no | stays | the registration exists but could not be pinned; remove it, then create again (§12.7) | — |
| `worktree.git_failed` | 1 | no | stays | git exited non-zero (stderr only in the runner log) | — |
| `records.snapshot_changed` | 9 | yes | stays | a file changed after the snapshot (client-side, the records tool) | — |
| `records.not_dedicated`, `records.git_state`, `records.unsafe_config`, `records.not_records_only`, `records.too_large`, `records.push_rejected` | 3 | no | stays | §12.8 (client-side, the records tool) | — |
| `unsupported.platform` | 3 | no | stays | `orch worktree` or `orch records` on Windows (D-P2-38) | — |

Other v1.6 refusals reuse existing codes: an unknown or invisible run (also `run.stop` on a run already gone),
attachment or `deposit_id` is `not_found` (§0.5); the start rate (§0.11) is `rate.limited` with `retry_after_ms`;
`grant.deposit` while the runner is not connected is `runner.unavailable`, and while this machine has not enabled
dashboard execution for the workspace `runner.disabled` (`detail.missing:"machine"`). A runner reconnecting after a
host restart shows as `RunnerInfo.connected:false` and, for ops that need it, `runner.unavailable`. Exit convention:
state refusals are 3, input refusals 5 (as `invalid.input` and `idem.conflict`), transient conditions 9. Every
`message` is host-authored and untrusted: clients render it as plain text.

**Store codes of the relay publisher (H12b).** These are raised by the store API that R3 calls (§11A.5 "As built in
H12b") and are never sent on a socket. A workspace op that meets one maps it to `internal` (`cli/store_errors.py`).

| Code | Exit | Retryable | Fate | When | `retry_after_ms` |
|---|---|---|---|---|---|
| `checkpoint.prefix_invalid` | 1 | no | stays | `Checkpoints.sign_prefix` refuses a prefix (a number at or below an existing one, an entry that is not a point of its log, a prefix that drops its base, a ticket event resting on a later workspace head, a prefix that is not downward closed, an `at` before the base), or the catch-up source does not verify | — |
| `relay.too_large` | 1 | no | stays | `Store.mint_catchup` cannot split: a restore is queued (an anchor), or one event's step set is above the cap. R3 alarms it (`host.status`); the 11A.5 oversized-restore refusal uses the same code | — |
| `outbox.mismatch` | 1 | no | stays | `Store.outbox_delete(n, hash)` for an entry `n` that holds another checkpoint | — |
| `relay.link_exists` | 1 | no | stays | `Store.relay_link` while `relay/link.json` already exists | — |
| `relay.link_mismatch` | 1 | no | stays | `Store.relay_mark_linked(expect)` when `link.json` is missing or is not exactly `expect` in state `linking` | — |

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
| `quick.list` (H11f) | `page.expired` (a `page` token that is not a live token of this caller: the same as `ticket.query`), `invalid.input` (a valid token with other arguments than the first page's, any other key, `dry_run`), `blob.invalid` (`files`), `quota.host` (one `diff` or `quick.list` in flight per caller, and the host-wide diff cap, shared with `diff`), `observe.unavailable` (only when the reader phase hangs past its budget plus 5 s); a count the reader cannot give is `null`, not an error |
| `workspace.settings` (v1.5) | `page.expired`, `invalid.input` (a `page` token with other arguments) |
| `ticket.text` (v1.5) | `not_found` (ref, `seq`, a field the event does not have, or an `event_id` that is not the event at `seq`), `ambiguous_ref`, `invalid.input` (`offset` past the text or not on a scalar-value boundary; `offset > 0` without `event_id`) |
| `artifact.query` (v1.5) | `page.expired`, `not_found` (`ticket`), `invalid.input` (a `page` token with other arguments, including `counts`) |
| `commit.list` (v1.5) | `not_found` (ref, repo, or a `head`/`base` the ticket does not allow), `observe.unavailable`, `repo.remote_mismatch`, `page.expired`, `invalid.input` (a `page` token with other arguments) |
| `diff` with `head`/`base` (v1.5) | as `diff`; a `head` or `base` the ticket does not allow is `not_found`; `since` with `head` or `base` is `invalid.input`; `invalid.input` for `head`/`base` without `repo`, and for either on a host without `diff_ext/1` (the §0.3 unknown-key rule) |
| `session.refusals` (v1.5) | `page.expired`, `not_found` (`ticket`, or a `session` the viewer may not see), `invalid.input` (a `page` token with other arguments) |
| `outcome.get`, `host.outcome` | `outcome.unknown_idem` (no record, or another principal's), `idem.expired` (only a tombstone is left) |
| `human.prepare` | `device.unknown`, `device.invalid`, `device.scope`, `observe.unavailable`, the inner op's declared errors, `sign.click_only`, `lock.busy` (the workspace lock of a hand-off prepare, waited for 2 s; retryable) |
| `human.get` | `not_found`, `human.request_expired`, `human.request_state`, `device.unknown`, `device.invalid`, `device.scope`, `observe.unavailable` (N3 re-check), `sign.click_only` |
| `human.submit` | `not_found`, `human.request_expired`, `human.request_state`, `human.request_mismatch`, `sig.invalid`, `gate.stale`, `members.stale`, `question.stale`, `observe.unavailable`, the inner op's model codes, `sign.click_only`, `lock.busy` (the workspace lock of a hand-off submit, waited for 2 s; retryable) |
| `human.pending` | none (an unknown code gives an empty list) |
| `human.cancel` | `not_found` |
| `sub.open` | `not_found` (`ticket:<ref>`), `sub.reset`; `invalid.input` for `detail` on a host without `sub_ext/1` (the §0.3 unknown-key rule) |
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
| **idempotent create** | `human.prepare` (v1.5) | optional | an outcome record that names the request id before the request is stored (`committed` once it is; a crash or failed commit in between leaves a `pending` record that a retry, `outcome.get` or the host start resolves to that request; one whose request is gone after its retention becomes a tombstone) | a retry of the same call, `human.get` or `human.pending {request_id}` (not `outcome.get`: it holds the first answer, frozen at prepare time) |
| **job completion** | `job.done` | not used | a persisted completion record per job (§0.8.2); a repeat is compared with it canonically | none needed (the repeat is the lookup) |
| **non-idempotent create** | `host.wsk_create` | not used | none: a retry creates a new key, and a key with no genesis after 1 h is deleted | none |
| **reads and side-effect-free ops** | reads (v1.5: `workspace.settings`, `ticket.text`, `artifact.query`, `commit.list`, `session.refusals` are reads); `human.get`, `human.pending`, `human.cancel`; `blob.put` (idempotent by offset and `upload_key`, §0.10), `blob.drop`; `sub.*`; `outcome.get`, `host.outcome`; `job.next`, `job.heartbeat`; `host.list`, `host.status`, `host.restart` | ignored | none needed | none |

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
- **`human.prepare` (v1.5)** honours an optional `opts.idem` in the same store, bound to
  `{op:"human.prepare", args_normalised:{op, args, signer, handoff, person}, files, session}`, where `args` are the inner args **as given** (not normalised: a REF and its uid are two bindings, which can only cause a false `idem.conflict`):
  - the same principal, idem and binding return **the same request** (same `request.id` and `code`) while it is
    `prepared` or `bound`, with its current state; the idem step comes before the 20-pending quota, so a request counts
    once and a retry at the quota still finds it;
  - the same idem with other arguments is `idem.conflict`;
  - once the request is terminal (`submitted`, `cancelled`, `expired`), the same idem answers that request in its terminal
    state (never a new one) while the outcome record is kept (24 h from the prepare); after that, or when the request is
    no longer stored, it answers `idem.expired`;
  - a refused prepare stores nothing, so a retry runs again; `dry_run` never stores or looks up (above);
  - without `opts.idem` every call creates a request, as before.

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
| `bootstrap` | no genesis | the store's files in the root (the person's empty directory stays), its key directory and the workspace id are removed | `failed` | `{state:"failed", error:{code:"control.rolled_back", retryable:true}}` |

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
  receipt (D-P2-19). As built, the receipt is the `receipt` argument and the output file is the separate `x-file`
  argument `receipt_output` (a `files` key is an `ArgName`, so it cannot be nested; its value is `"-"`). Both are host-mode
  only: the CLI has no flag for them. The host refuses `run` with `op.not_allowed`, a `cmd` other than the task's
  `verify.cmd` (exact string) or a non-zero `exit` with `invalid.input` / `verify.failed`, and stores the output as a
  `receipt` artifact `<task>-receipt.log` named by `log`. A ticket that links repos takes a receipt only with
  `repo` one of the linked repos and a 40/64-hex `commit` (F1 §6: evidence follows the source `sha`); a ticket that links
  none takes `repo: null` and `commit: null` only. Two `files` arguments with the same logical name are `blob.invalid`.
  A direct-mode receipt (`--run`) carries `attested_by: "agent"` too: the agent's environment ran it, not orch. The
  verify-gate review (terminal text and the dashboard dialog) says "attested by the agent".
  `task.list` data gives each task's exact `verify` command (when it has one), `verify_runnable` (whether the caller may
  run it, below) and `verify_author` (who changed it last, for display), and the ticket's linked `repos`, so a client
  can run it. The thin CLI (`orch task done T --run`) reads them, refuses with `verify.unapproved` unless
  `verify_runnable` is true, sends a dry-run `task.done` for the same task (the claim, the lease and the task state,
  checked exactly as the real request will; idempotency does not apply to a dry run, §0.7) and only then runs the
  command with the P1 runner (allow-listed
  environment, timeout, output capped at 1 MiB), refuses a dirty working tree of a linked repo (the receipt must name its
  commit), picks the only linked repo or, with several, the one whose directory name matches the working copy it is in,
  and sends nothing when the command fails. `orch describe` does not list the `x-host` arguments.
  **Trust of the command (host mode).** The command the CLI runs is the `verify` of the one task asked for, from the
  host's `task.list` answer, which is the host's view of the signed ticket. The CLI runs it only if the answer names the
  ticket asked for (the key, or the number given) and a task with exactly that id, and `verify` is a non-empty string of
  at most 4096 bytes without NUL; otherwise nothing runs. No other host field, error text or argument is ever executed.
  It then runs through the same runner as direct mode (same argv splitting without a shell, allow-listed environment,
  timeout, output cap, working copy as cwd) and the `task.done` goes to the key it was read from. It equals direct
  mode's trust, which is enforced (F1 "Receipts"): the command runs only if the plan gate applies and is approved at its
  current generation, or the caller's own person (the person of its grant) made the last change to that task's
  `verify` under a grant that is not revoked (revoking a leaked grant withdraws trust in what it wrote, also for the
  person's other grants; an expired grant stays trusted). The approval binds the command string only, not the repository
  content it runs: an approved `make test` or `./run.sh` executes whatever the working copy holds. **Without an
  applicable plan gate only the caller's own command runs**; an unattended agent runs none that
  is not approved. A host that lied could only make the client run a command of its own choosing, as any program the
  user's own host-side state could already name. The host re-checks that the receipt's `cmd` equals the signed
  `verify.cmd` byte for byte and applies the same trust rule again when it takes the receipt (`verify.unapproved`
  otherwise), so a stale or modified client cannot get around it.
  The CLI prints the exact command (escaped) before running it. **Trust direction (D-P2-19):** in host mode the command
  reaches the agent's machine through the host's answer; a compromised host that passes the pin could make the CLI run an
  arbitrary command as the agent user. That is accepted in tier A, where the host runs as the same UID. Phase 3 (tier B)
  must revisit it, for example by verifying the command against the signed ticket log locally.
  **"Own" is per person, which is safe only in tier A.** In tier A and in direct mode all of a person's agents, the
  person and the host share one UID, so "the caller's own person wrote it" adds no reach the UID did not already have.
  In tier B (agent UIDs, #287) it would let one agent UID plant a command another agent UID of the same person (or the
  person, with a grant) runs, crossing the boundary tier B enforces: there "own" must mean the same grant (or the same
  agent UID class), not the same person. A phase-3 follow-up (§9 item 10).
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
Closed = {resolution:"wont_do"|"duplicate"|"obsolete"|"other", duplicate_of:Key | null, at:Time, by:Person,
          seq?:int, event_id?:ULID}                                                     // v1.5, read_ext/2
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
  (H6b) The snapshot fixes **membership and order** (each row's uid, sort key and visibility at the snapshot); a later
  page's row **contents** are read from the current state, for the current viewer, so a large matching set never
  makes a snapshot too large to keep.
- **Page tokens** (`next_page`, here and in `activity.list`) are opaque and authenticated by the host (a MAC under a
  host-only key, or a host-side record). A token binds the workspace, the principal hash, the history epoch, the snapshot
  position, the paging position and a digest of the first page's other arguments. The checks run in this order:
  authenticity, then principal, then expiry, then arguments. A token that fails the check, or was issued to another
  principal, is `page.expired`; a valid token of the caller used with other arguments is `invalid.input`. Tokens expire
  after 10 min (`page.expired`). **Every page is filtered for the viewer of its own request** (§0.5, rechecked per
  request, so a revoked grant or a removed person changes the viewer at once), and a row is returned only when its
  ticket is visible to that viewer both at the snapshot and now. A change of the principal's viewer, to or from
  `no-person`, is `page.expired` (H6b: nothing computed for the first viewer carries over).
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
 closed?:{resolution:str, duplicate_of:Key | null, at:Time, by:Person, text:str | null, seq?:int, event_id?:ULID},
 tasks?:[Task], verdict?:Verdict | null,
 artifacts?:[Artifact]{0..1000}, artifacts_more?:bool,
 claim?:ClaimFull | null,
 source?:[{name:str, repo:str, ref:str, sha:str}], pushes?:[Push]{0..50}, pushes_more?:bool,
 sections?:{str: [SectionRev]{0..20}}, sections_more?:{str:bool},
 // v1.7 (§11A.14, H12e), always sent:
 members_v:int}

LogItem  = {seq:int, type:str, at:Time, actor:Actor, summary:str{0..200}, data?:LogData}
Question = {id:str, state:"open"|"answered"|"stale", to:[Person], blocking:bool, answered_by:Person | null, answered_seq:int | null,
            text?:str, why?:str, to_ref?:str, options?:[{key:str, label:str, cost?:str}], recommended?:str,
            asked_at?:Time, asked_by?:Actor, asked_seq?:int,
            answer?:{by:Person, at:Time, seq:int, option:str | null, text:str | null, auth?:str,  // auth: v1.5
                     id:ULID},                                                       // id: v1.7 (§11A.14)
            qid?:str/[0-9a-f]{32}/, hash?:Hash}                                          // v1.5, read_ext/2 (§0.8.5)
Criterion = {id:str, text:str, evidence:[{artifact:str, digest:Hash, kind:str, attested_by:"agent"|"person"|"unattended"}],
             covered?:bool, tasks?:[{id:str, evidence:bool}]}
GateView  = {state:"open"|"approved"|"n/a", gen:int, hash12:str/[0-9a-f]{12}/ | null,
             applies?:bool, waiting?:bool, blocked?:bool, count?:int, approved_by?:[Person], eligible?:[Person],
             policy?:Policy, missing?:[str], decisions?:[Decision]{0..100}, decisions_more?:bool, revoked_decisions?:[ULID],
             hash?:Hash | null, policy_hash?:Hash | null}                                // v1.5, read_ext/2 (§0.8.5)
Decision  = {id:ULID, kind:"approved"|"changes_requested"|"pass"|"fail", person:Person, at:Time, seq:int, gen:int,
             counting:bool, voided:bool, auth?:str, text?:Snip, source_sha?:[Source],
             hash?:Hash, policy_hash?:Hash}                                              // v1.5, read_ext/2 (§0.8.5)
Task      = {id:str, text:str, state:"open"|"done"|"skipped"|"blocked", proves:[str], leased_by:str | null,
             by:Actor | null, at:Time | null, reason:str | null, receipt:Receipt | null}
Receipt   = {cmd:str, exit:int, ms:int, repo:str | null, commit:str | null, log:str | null, seq:int, at:Time,
             evidence:bool, attested_by?:"agent"}
Verdict   = {outcome:"pass"|"fail", person:Person, at:Time, seq:int, gen:int, text:Snip | null, source_sha:[Source],
             current:bool, counting:bool, voided:bool, auth?:str}
Artifact  = {name:str, kind:str, digest:Hash | null, bytes:int | null, ac:str | null, task:str | null, label:str | null,
             addon?:str, ref?:str, evidence:bool, by:Actor, at:Time, seq:int, replaced:int}
ClaimFull = {session:str, agent:str, for:Person, grant:str, since:Time, last_at:Time, live:bool, lapsed:str | null,
             takeovers:[{at:Time, session:str, from_session:str, reason:str}]{0..20}, takeovers_more?:bool,
             idle_until?:Time}                                                           // v1.5, read_ext/2 (§0.8.5)
Push      = {seq:int, at:Time, repo_name:str, repo_id:str, ref:str, sha:str, before:{repo_id:str, ref:str, sha:str} | null}
SectionRev = {seq:int, at:Time, hash:Hash | null, actor:Actor, via:"edit"|"external", refs:[str]}
```

- `TicketDoc` is the F1 §7 ticket document, plus (v1.7, §11A.14, H12e) `head:{seq:int, head:Hash}`: the `seq` and
  event head of the ticket log's last event.
- **One snapshot (v1.7, H12e).** `doc.head`, `questions[].answer.id` (the id of the answer event that counts) and
  `members_v` (the roster version a person event signs as `roster_v`) are read under the same store lock as every
  other field, so a phone review needs one `ticket.get`. They are viewer-filtered like the rest: an invisible
  ticket is `not_found`, an answer appears only where `answer` already did (never for `open` or `stale`).
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
  | `task.done` | `{task, receipt?:{cmd, exit, ms, repo, commit, attested_by?:"agent"}, log?, text?:Snip}` |
  | `task.skipped`, `task.blocked` | `{task, reason}` |
  | `log.added` | `{text:Snip}` |
  | `question.asked` | `{question, blocking, to}` |
  | `question.answered` | `{question, option?, text?:Snip}` |
  | `gate.approved` | `{gate, gen}` |
  | `gate.changes_requested` | `{gate, gen, text:Snip}` |
  | `verdict.given` | `{outcome, gen, text?:Snip, source_sha:[Source]}` |
  | `gate.invalidated` | `{gate, cause, voided:[ULID]}` |
  | `branch.pushed` | `{repo_name, repo_id, ref, sha, before_sha:str \| null}` |
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
  with the same ids, so a client dedupes by `id` (§0.9). With `detail:true` (v1.5, `sub_ext/1`) each event item is the
  `ActivityItem` itself, so the client merges it instead of refetching. The workspace topic applies this op's per-type rule
  (§0.9, v1.5).

Errors: `invalid.input`, `page.expired`, `not_found` (`ticket`).

`diff {ref:Ref, repo?:str, context?:int 0..10 = 3, max_bytes?:int 1024..1048576 = 262144, since?:"my_approval",
head?:Sha, base?:Sha}` (read; `head` and `base` are v1.5, cap `diff_ext/1`, §0.8.5) →

```
{sources:[{repo:str, branch:str, base_sha:str, head_sha:str,
           base_source:"host_pinned"|"local_ref"|"first_parent"|"approved"|"pushed", base_moved:int,   // "pushed": v1.5
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
  without such a decision it is `not_found`. A verdict's `source_sha` is never a base. (As built in H6c: see below.)
- `sources[]` also carries `commits:[{sha, subject}]` for `base..head` (newest first, at most 50, `commits_more:int`),
  as display only; subjects are cleaned of control and bidi characters.
- A client that prompts for a code approval shows, per repo, `base_sha`, `base_source`, `base_moved` when non-zero,
  and the commit list. The approval itself still signs only the head `source_sha` (D58); the diff is a display.
- **H3b1 implements** `local_ref` and `first_parent` (`base_moved` is always 0) and the commit list. `host_pinned`
  and `since:"my_approval"` were follow-ups (§9 Q9), built in H6c ("As built in H6c" below). A base the caller chose (`declared`, a later option) is never the
  approval screen's default: the approving client shows it with its sha and whether it is an ancestor of head, and
  the signing path never takes a base from the agent's side.
- **Served** on the workspace socket since the H3b1 follow-up (`orch.host.diff`, after H10a wired the reader): the
  ticket through the viewer's visibility, `repo` through the ticket's `links.repos`, then the reader under the caller's
  principal, off the workspace's worker thread: one `diff` in flight per principal and two host-wide, so two reader
  children stay free for `show`, observe and the tree checks (else `quota.host`), 30 s for all repos of one request,
  and the ticket's visibility checked again before the answer. A reader's reason (it may name a host path) reaches only a person-class owner or maintainer;
  everyone else gets `repo <name>: not readable`. Until `since` lands it is an unknown key there (`invalid.input`,
  §0.3). Since H6c `since`, `head` and `base` are served as well.
- **As built in H6c** (`diff` only; additive, the result shape is unchanged; `commit.list`, `session.refusals` and
  `artifact.query` of the H6c row are separate work):
  1. **`host_pinned`.** Only a call of column P (a person) records a pin; G and N never do. The host keeps `{ticket uid: {repo identity: sha}}` in `hosts/<workspace>/diff-bases.json`
     (0600, atomic replace, at most 20 000 entries). A served `diff` records the local merge base of the observed head
     the first time it sees a (ticket, identity); a recorded base is never replaced. While it is an ancestor of the head
     **and of the current tip of the default branch** (else it is ignored: an agent that moved the default branch, got a
     pin recorded and moved it back must not hide its commits) it is the base (`base_source:"host_pinned"`), and `base_moved` is the number of commits between it and the current
     local merge base (0 when there is none). A pin that is gone from the repo or is no ancestor is ignored (the
     rules fall through to `local_ref`). Because the first diff pins the then-current merge base, a later diff of a host
     answers `host_pinned` where direct mode, which keeps no host state, answers `local_ref`. P3 replaces first
     observation with a base pinned at claim.
  2. **`since:"my_approval"`.** The plan phase (worker thread) lists the source entries of the caller's own `code`
     approvals, newest first (`approvals_of`); the reader keeps those whose repo identity is the observed one and takes
     the newest that is an ancestor of the head (`base_source:"approved"`, `base_moved` 0). Only the caller's person
     counts: a grant (G) acts for its grant's person, N (no-person) has none, and another person's approval is never
     read. An approval is used only while its person is still a member, its decision is not voided, and its signing
     device is that person's own, present and not revoked (nor flagged on a settled ticket). The newest approval that
     fails this ends the search: an older approval never replaces it. A push or a change request that raised the gate's
     generation does **not** disqualify an approval (that is exactly the "what changed since I approved" case).
     Answer without a usable approval, or none that is an ancestor of the head: `not_found`, "no approval of yours covers
     this repo's history". A verdict's `source_sha` is never read.
  3. **`head`, `base`** (cap `diff_ext/1`, listed since H6c; `welcome.ops` lists `diff` only, `commit.list` is not
     served). 40 or 64 lowercase hex only; `repo` required; `since` with either is `invalid.input`. The plan phase
     passes the ticket's last 100 `branch.pushed` events of the repo name; the reader keeps those with the observed
     identity (a `before` also needs its own identity and the push's ref). Root heads are the observed head and at most
     20 qualifying pushed heads that exist; each root's range is `default base..root` by the section 0.8 rule
     (including `host_pinned`). A `head` must lie in a range (at most 10 000 commits in all, else refused). A `base`
     must be an ancestor of the selected head and one of: the base of a range holding the head (labelled by that
     rule), the head's first parent (`first_parent`), a qualifying pushed `sha` or `before.sha` (`pushed`). Everything
     else, existing or not, is `not_found` with one fixed message ("<repo>: not found for this ticket").
  4. **Limits and rechecks are unchanged and shared**: one diff per principal, `MAX_CHILDREN - 2` host-wide, 30 s for the
     request, and the phase-3 recheck on success and on error. For `since` the recheck resolves the approvals again
     from the refreshed store and refuses (`not_found`, the "no approval" text) when they differ from the ones used;
     an exception while rechecking refuses too (the transport answers `internal`, never the diff).
  5. **Left out of H6c**: `commit.list` and `diff` combining several bases in one call, the P3 base pinned at claim,
     rename detection, and a `host_pinned` base in direct mode (no host state there).
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

`workspace.show {}` (read; unchanged in v1.5, the settings and policies are read with `workspace.settings`, §0.8.5) →

```
{id:str, prefix:str, name:str, genesis:Hash, epoch:int, wsk_fingerprint:str, members_v:int, state:"ok"|"degraded",
 next_key:Key | null}                                                                // next_key: v1.7 (§11A.14)
```

`next_key` (v1.7, H12e) is the store's exact next key (`_next_key()` after the directory scan, under the lock),
advisory and never a reservation. It is `null` for a principal that may not create a ticket: a viewer, no-person,
column G (judgement call of H12e: an agent's `new` allocates its own key), and a column-R device without a valid
`decide` certificate on the replayed roster.

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
{grants:[{id:str, person:Person, scope:"all"|"workable", verbs:"agent" | [OpName], label:str | null,   // label: the grant.issued label (H11c-2)
          issued_at:Time, expires_at:Time, revoked_at:Time | null, sessions:int,
          // v1.5, read_ext/2 (§0.8.5):
          revoked?:{by:Person, at:Time, seq:int}, session_ids?:[str]{0..100}, sessions_more?:bool}]}
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

**`policy.set` and `settings.set`.** Person ops run through `human.prepare`: `settings.set` and `policy.set` without
`ref` (the workspace default) by owners only; `policy.set {ref}` (a ticket override) by those who may manage the ticket
(the ticket owner, workspace owners and maintainers, F1 §5.4.2), the people `needs` sends a `no_eligible` item to. Each emits one
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
the dashboard addon's own data. (`artifact.query`, v1.5, lists the manifests' metadata; it is not `artifact.search`, which
would search content.)

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
                submitted_at:Time | null, state:State, event_ids:[ULID], seq:int | null,                     (R6; seq of the last event once submitted)
                error?:{code:str}}]}       // H5a: the catalog code of the last failed human.submit, never the message
human.cancel   {request_id:ReqHr} → {state:"cancelled"|"submitted"}
Prepared = {request:{id:ReqHr, code:Code | null, expires_at:Time, state:State,
                     submitted_at:Time | null} | null,   // submitted_at: when human.submit was accepted; null until then      // null only for dry_run
            result?:Result,                     // H7: dry_run only, the inner op's would-be Result (direct-mode parity)
            op:OpName, workspace_id:WsId, log:Uid | "workspace", events:[Event]{1..16}, material:Material,
            review_digest:Hash, fingerprint:str/[0-9a-f]{12}/,
            origin:{uid:int, pid:int, client:str{1..64}, created_at:Time},
            checks?:[{repo:str, clean:true, attested_by:"worker", at:Time}]}               // not signed, not in material
State    = "prepared"|"bound"|"submitted"|"cancelled"|"expired"
ReqHr    = str/hr_[0-7][0-9A-HJKMNP-TV-Z]{25}/          Code = str/[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}/
Signer   = {device:Device, auth:"passphrase"|"secure-enclave"|"convenience"}   // convenience: §0.8.6 (H5c)
Material = {kind:"gate", gate:Gate, input:<orch.model.gates.gate_input>, ticket:{key:Key, uid:Uid}, review:[Review]{1..16},
            policy:Policy,                      // v1.5: the canonical effective policy (§0.8.1 "Bound hashes"); always
                                                // present, because a blocked gate has no policy hash and gets no prompt
            texts:{str: str}}                   // H5a: the full text of every section in input.sections; the signer
                                                // checks each against input.sections and shows it (P1 parity)
         | {kind:"question", ticket:{key:Key, uid:Uid}, question:<the question object>, review:[Review]{1..16}}
         | {kind:"payload", ticket:{key:Key, uid:Uid} | null, review:[Review]{1..16},
            texts?:{str: str},                          // section id → full text, for every section the events reference
            before?:{str: any}}                         // prior values of the paths a set/section.set/settings.set changes
Review   = {event_id:ULID, type:str, payload:object, derived:{str: any}}
```

**The canonical review payload (exact).** For every event, `Review` is computed by `orch.host.review.of(event,
material)` and recomputed by the signer, which refuses any difference:

- **`payload`** = the event **minus** the envelope keys `v`, `id`, `type`, `actor`, `auth`, `hash_v`, `roster_v`,
  `based_on` and `sig`, and, on the three decision types only, `gate_gen` (shown on its own line as a host attestation),
  verbatim and complete. Every payload key is shown, including keys a future F1 version adds (L2); a `gate_gen` on any
  other type stays in the payload. Nothing can be signed without being shown.
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
sha256(cj({"op": op, "review_digest": review_digest, "events": [each event without sig, actor.device, auth and,
           for grant.issued, secret_hash]}))
```

**Exactly** (H5a): `fingerprint = hex(sha256(cj({"op": op, "review_digest": review_digest, "events": [V(e) for e in
events]})))[:12]`, where `cj` is F1's canonical JSON (JCS) and `V(e)` is `e` without `sig` and `auth`, with
`actor` without `device`, and, for `grant.issued` only, without `secret_hash`. Test vector
(`tests/host/test_human_fixes.py::test_the_fingerprint_test_vector_of_the_doc`):

```
op            = "log"
review_digest = "sha256:abab…ab" (64 hex: "ab" × 32)
events        = [{"v":2, "id":"01J9ZP0000000000000000000B", "type":"log.added",
                  "actor":{"kind":"person", "id":"p_1111…11" (32 × "1"), "device":"d_2222…22" (32 × "2")},
                  "auth":"passphrase", "hash_v":1, "roster_v":1, "based_on":"sha256:cdcd…cd" ("cd" × 32),
                  "text":"hello", "sig":"x"}]
cj input      = {"events":[{"actor":{"id":"p_1111…11","kind":"person"},"based_on":"sha256:cdcd…cd","hash_v":1,
                 "id":"01J9ZP0000000000000000000B","roster_v":1,"text":"hello","type":"log.added","v":2}],
                 "op":"log","review_digest":"sha256:abab…ab"}
fingerprint   = "d8de03967462"            (the same with op "set": "37cf21ef169c")
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
| `bound` | `human.submit` refused before any append (`gate.stale`, `members.stale`, `question.stale`, `sig.invalid`, `human.request_mismatch`…) | `bound` (not terminal; `human.pending` shows `error:{code}`; it expires as usual) |
| `bound` | `human.submit` failed after an event landed (a roll-forward refused, tier A only) | `submitted`, outcome `failed` |
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

     Then run the checks of the table "Bound hashes" below (recomputations, consistency checks and host attestations,
     each named as what it is), recompute the `Review` of every event, `review_digest` and `fingerprint`, and compare
     each.
  2. Render its own review from `material` and the events, never from host text. It opens with the header below and
     lists **every event, one block each**. For `payload` material the blocks are the events' review fields, which are
     their signing bytes. `checks` are shown as **attested by the worker, best effort**.
  3. Get the op-specific confirmation word: the ticket key for decisions, `GRANT` for grants, the person id for member
     ops; for the host person ops (H6b) `KEY/FRAG` for `move` and a ticket `policy.set`, `<PREFIX>~<WS8>/POLICY` for a
     workspace `policy.set` and `<PREFIX>~<WS8>/SETTINGS` for `settings.set`, `PREFIX` being the workspace prefix and
     `WS8` the last eight characters of the workspace id, both from the signer's **own** registry (a workspace not in it
     is refused), so two workspaces with one prefix never share a word; the header still shows the full id. The signer also checks what each of these ops may
     change: `move` one `ticket.moved` to `backlog` or `open` on a ticket log; `policy.set` one `policy.changed` naming
     1 to 4 gates, on the ticket log with a ticket and the workspace log without; `settings.set` one `settings.changed`
     on the workspace log whose `set` names only `grant_hours`, `claim_ttl_min`, `lease_ttl_min` and `repos`.
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
  question hash, each checked by the signer as the "Bound hashes" table says (recomputed, consistency-checked or
  displayed as a host attestation). **The dashboard renders its preview from
  `material` and `fingerprint`, never from its cached ticket view.**
- **Bound hashes (v1.5).** These are the hash-bearing fields of the decision and edit events. They are in
  `Prepared.events` in full (`sha256:` plus 64 hex), so the dashboard may show each of them in full or as its first 12 hex
  digits; it takes them from `Prepared.events`, never from `ticket.get`. **The signer distinguishes recomputation from
  consistency checks and host attestations**, and refuses any failed check before any prompt.

  | Event type | Field | What it binds | The signer's check | Kind |
  |---|---|---|---|---|
  | `gate.approved`, `gate.changes_requested`, `verdict.given` | `policy_hash` | the effective policy of the gate | computes `H("orch/v2/policy\|" \|\| cj({"gate": material.gate, "policy": material.policy}))` and requires equality with both `material.input.policy_hash` and the event's `policy_hash` | recomputation |
  | the same | `hash` | the gate hash `H("orch/v2/gate\|" \|\| cj(G))` (F1 §5.6, §5.7) | recomputed from `material.input` (= `G`, which holds `policy_hash`, not the policy) | recomputation |
  | `verdict.given`; `gate.approved` on `code` | `source_sha` | the source list, D58/D59 | must equal `material.input.source_sha` and is displayed exactly as signed; the host observes git and revalidates the source list at submit | consistency check; host attestation |
  | `gate.approved`, `gate.changes_requested`, `verdict.given` | `gate_gen` | the gate's generation | displayed; checked by the host at submit (`gate.stale`), not recomputed by the signer | host attestation |
  | `question.answered` | `hash` | the question hash `H("orch/v2/question\|" \|\| cj({question_id: qid, ticket: uid, text, options}))` | recomputed from `material.question`, the `qid` derived from the workspace id, `material.ticket.uid` and the question id (F1 §5.6) | recomputation |
  | `ticket.updated`, `ticket.created`, `question.asked` | section hashes | each referenced section's text | `derived.sections` (above) | recomputation |
  | `ticket.updated` | `base_rev` | the prior values | `before` (above) | consistency check |
  | `gate.*`, `question.answered`, `verdict.given` | the gate's section texts | each section in `material.input.sections` | `section_hash(material.texts[sid]) == input.sections[sid]` for every section, and shown (H5a) | recomputation |
  | every ticket op | (none: the key is in no signed field) | `material.ticket.key` | displayed as "ticket key per host" next to the full uid of the signed log; the confirmation word is `KEY/FRAG`, `FRAG` from that uid (H5a, item 7 below) | host attestation |

  This table describes decision bindings and section-edit checks; it is not an exhaustive list of hashes in all prepared
  event types. Hashes without supplied preimages (artifact and package digests, event references and others) are
  displayed as signed commitments, not described as independently recomputed.

  `ticket.get` (v1.5, `read_ext/2`) also returns the current `GateView.hash`, `GateView.policy_hash`, `Decision.hash` and
  `Question.hash` (§0.8.5) for display and comparison. When a value there differs from the prepared event's, the dashboard
  says the ticket changed since it was read; it never substitutes one for the other. `hash12` stays the first 12 hex digits
  of the gate hash.
- **Hand-off (F5).**
  - The 8-character code (40 bits) **names** the request. It is not a secret and authorises nothing.
  - `human.pending {code}` finds only the same person's requests. Failed lookups are limited to 5 per minute **per
    connection**.
  - `orch sign CODE` resolves the code with `human.pending`, then fetches and binds with `human.get`. It never calls
    `human.prepare` a second time.
  - **In tier A any process of the person's UID can create a request**, so `orch sign` says "only sign a code you
    created yourself" every time it runs.

**As built in H5a** (resolutions of points the text above leaves open, and the #391 review round; the wire schemas
change only where marked: gate `material.texts`, and `op` in the fingerprint input):

1. `human.cancel` on an `expired` request answers `{state:"cancelled"}`; the stored state stays `expired`.
2. A prepared `grant.issued` keeps `"secret_hash": null` in every answer (the host's redaction drops a secret-named key
   only when its value is not `null`).
3. A person's `new` emits `ticket.created` only (`EMITS_V1["new"]`; F1's `ticket.created` is closed): only the key,
   `ticket_type`, `title` and the owner (the person) travel. `priority`, `size`, `label`, `parent` and `message`/`file`
   (the description) answer `invalid.input` on `new`; they are never dropped silently. A client sets them in a separate
   `set` or `section.set` request after the creation (a second hand-off). The key is the one the store would allocate
   at prepare; a key taken before the submit answers `ticket.exists`.
4. "One `log`": the events carry no log of their own, so the signer checks that the request's `log` is the ticket of
   `material.ticket` (or `workspace` with no ticket).
5. `material.before` keys are the `base_rev` paths (`ticket.<field>`, `body.<section>`) for `set` and `section.set`, and
   the keys of `set` for `settings.changed`.
6. Number and order of events: one for a `who: human` op, one `ticket.created` for `new`, 1 to 16 events of the op's
   `EMITS_V1` types otherwise. **The op is bound:** it is in the fingerprint input, shown in the header, and the signer
   checks what each op's `ticket.updated` may touch (`ac.add`/`ac.edit`: `ticket.acceptance` only; `section.set`: one
   section and no `set`; `set`: the fields of `set` only).
7. **The confirmation word.** A ticket op's word is `KEY/FRAG`: the key as the host gives it, and `FRAG`, the **last
   eight characters** of the ticket uid in the request's `log` (the log the signature names; the signer takes it from
   there, never from host text). Example: `DEMO-0042/2W0KFDM3`. The header the signer prints, in this order:

   ```
   workspace: <name> (<prefix>) <workspace_id>              name and prefix from the signer's own registry
   op: <op>: <op in plain words>                            e.g. "op: approve: approve the plan gate of DEMO-0042/2W0KFDM3"
   ticket: <key> (ticket key per host, not proven) uid <uid> (signed; you type its last eight: <FRAG>)   ticket ops only
   requested by uid <uid> pid <pid> at <created_at>; client <client> (self-declared, unverified)
   fingerprint: <12 hex>
   ```

   The key is **not proven** in H5a: the header and the review
   label it "ticket key per host" and show the full uid. (Follow-up: prove it by carrying the key-defining events,
   `ticket.created` and `ticket.moved`, in `material`: #394.) `GRANT` for grants, the person id for member ops, the addon name
   for addon ops, else the op name in capitals. Every identifier the signer shows or the person types (key, uid,
   person, device, section, addon, question id, `before` path, origin) is checked against its F1 format before
   rendering, every other host string is escaped (control and bidi characters become visible markers), and a review
   that would still carry a control character is refused.
8. Hand-off codes and `orch sign CODE` are H5b (see "As built in H5b" below); `human.get` with `signer` binds a
   hand-off request (device checks included). `human.pending` is served: without arguments it lists the caller's
   requests (any state, within retention); `{request_id}` gives that one (or an empty list); `{code}` the pending
   request the code names (H5b). A failed submit shows `error:{code}` (the catalog code only).
8a. **`human.prepare` honours `opts.idem`** (v1.5, §0.7): the same principal, idem and arguments return the same request
   in its current state; other arguments are `idem.conflict`; a terminal request answers as for other idems (24 h); no
   idem creates a request each call; `dry_run` never stores. The idem namespace is shared with `human.submit`: use a new
   idem for each call (the prepare's idem is not the submit's), and read a prepared request's state with `human.pending
   {request_id}`, not `outcome.get`. A client without an idem that lost a prepare's answer finds
   its open requests with `human.pending` (no arguments) and cancels the ones it will not use (`human.cancel`); unused
   requests expire after 10 minutes (a grant: 4), and at most 20 are pending per person (`quota.host` beyond).
9. `human.prepare` takes inner-op files as `{b64}` or `{blob}` (H4a2): a blob must be a complete blob of the preparing
   principal in this workspace; the prepare consumes it and the request's file bytes count against the preparing UID
   until the request is terminal (§0.10). The composite
   (`orch.host.signer.run`) compares each `artifact.*` event's `sha256` and `bytes` with the bytes it uploaded itself.
10. **A grant request expires after 4 minutes**, not 10: its `issued_at` is the prepare time, and F1 refuses a
    `grant.issued` more than 300 s from its append.
11. **D58 at submit.** Prepare appends nothing (no `branch.pushed` either). At submit, under the store lock, for every
    event that signs a `source_sha`, the host reads each linked repo's head again through its git reader (under the
    caller's principal) and refuses with `gate.stale` when it differs from the signed source list, or with
    `observe.unavailable` when the reader cannot answer (fail closed). The working tree's cleanliness is
    `tree.check` at prepare and at binding (N3); in tier A the host runs it itself (H10a, below D-P2-19), in tier B
    `verdict` and `approve code` answer `observe.unavailable` until the worker (H10) takes the job over.
12. **Gate texts.** Gate material carries `texts` for every section in `input.sections` (next to `material.policy`);
    the signer checks each against `input.sections` and shows them with the criteria, tasks, links and policy, as P1's
    review did. `gate_gen` and `source_sha` are shown labelled as host attestations; the policy hash is recomputed from
    `material.policy` and must equal both `input.policy_hash` and the event's.
13. **An interrupted submission is rolled forward, never re-judged.** A request whose signed events are being appended
    (some landed, no outcome yet) neither expires nor cancels (`human.cancel` answers `submitted`). A `human.submit`
    with the same signed events, or `outcome.get` / a retry of the interrupted idem, finishes the appends and gives the
    interrupted idem the same terminal state; other events answer `human.request_state`.
14. The CLI composite is the library function `orch.host.signer.run`; routing `orch approve` and the other person
    commands to it is H4b's host-mode CLI.

**As built in H5b** (hand-off; no wire schema changes):

1. **Codes.** A request prepared with `handoff:true` gets a code at prepare: 8 characters of Crockford base32
   (`0-9A-HJKMNP-TV-Z`, 40 bits) from a CSPRNG, shown `XXXX-XXXX`. A code is never issued twice while a request carrying
   it is stored in the workspace (24 h after it ends). A request prepared with `signer`, and a `dry_run`, has `code: null`.
2. **`human.pending {code}`** returns the request only to the same principal and the same mapped person, only in the
   workspace whose socket is asked (each workspace has its own request directory), and only while the request is
   `prepared` or `bound`: the code expires with the request and no longer resolves once it is submitted, cancelled or
   expired (`{request_id}` still reports those states, R6). A lookup that finds nothing is a failed lookup.
3. **The lookup limit.** After 5 failed code lookups within 60 s on one connection, every code lookup on that
   connection answers `rate.limited` (retryable, `retry_after_ms` until the oldest failure leaves the window), the right
   code included. The count belongs to the connection: another connection of the same person is not affected, and
   `{request_id}` lookups are not counted (an id carries 80 random bits).
4. **`orch sign CODE [--workspace ID|PREFIX] [--file NAME=PATH]…`** (`orch.host.handoff`): prints the notice "only
   sign a code you created yourself" on every run (stderr, and first in the review on `/dev/tty`); refuses when
   `ORCH_GRANT` is set (`human_only`); asks `human.pending {code}` on the socket of each workspace of the person's own
   registry (`<state>/host/workspaces.json`, the entries mapping the caller's UID to a person), or only the one named;
   then `human.get` with the device of the key in `<state>/hosts/<ws>/person/`, and the tail every signing client
   shares (`orch.host.signer.finish`: the signer's `check`, the review (notice, header, content), the confirmation
   word, the passphrase, `human.submit`, a grant's secret on the terminal after the commit). A refusal, a declined
   review or a failed passphrase cancels the request, so the preparer sees `cancelled`. The header's workspace name is
   the last path component of the registry entry's `root`, its prefix the entry's `prefix`. `orch sign` and
   `orch host` are host-client commands routed before the registry ops: they are not registry ops, so `orch help` and
   `orch describe` do not list them.
   - **The search fails closed.** Nothing is signed unless exactly one workspace answered with the request. A pin
     failure (the socket's process is not the registry's host UID, or the welcome names another workspace) is a hard
     refusal, `host.untrusted`, naming the socket, never "not served". A workspace that is not served (no socket,
     connection refused) is skipped. Any other failure of one workspace (a refusal, `rate.limited`,
     `workspace.unavailable`, a broken connection) is collected and the search goes on; the failures are named on
     stderr when another workspace holds the code, and in the `not_found` message when none does. Several matches:
     `invalid.input` (use `--workspace`).
   - **The client.** It talks to the workspace socket with the minimal `orch.host.client.Connection` (pinned
     handshake, one request at a time, errors as `HostError`), not with H4b's host-mode CLI. `Connection` is meant as
     H4b's base: H4b adds a per-call timeout (now one socket timeout for every call) and subscription frames (now
     skipped).
5. **Artifact digests in a hand-off.** A hand-off request's artifact events (`artifact.added`, `artifact.replaced`)
   carry digests of bytes `orch sign` did not upload. Without `--file` the review labels their `sha256` and `bytes`
   "signed commitment: orch sign has not seen these bytes; compare with the file you meant". With
   `--file NAME=PATH` for every artifact of the request, each is compared with the local file (name, digest, size;
   then `check_uploads`) before the review, a mismatch or an artifact without a file is refused (and cancels), and the
   label reads "checked against your local file". The composite (`orch.host.signer.run`) checks the bytes it uploaded
   itself and keeps its review unchanged.
6. **Notes for later.** Code lookups match the same principal and person, and the caller can already list its own
   requests with codes (`human.pending` without arguments), so the per-connection limit protects nothing more than
   F5 asks. If the lookup is ever widened beyond the same principal (for example to the same person across
   principals), the limit becomes the only control and needs a per-person bucket as well. The "own registry" is the
   host registry read as the caller (owner-checked, no symlinks); once the host runs under its own service UID that
   file is the host's, and `orch sign` needs a person-side registry (follow-up).

**As built in H4b** (the thin CLI, `orch.hostclient`; no wire schema changes):

1. **Mode by registration.** Per call, for the workspace the CLI found: in the person's own registry or in this agent
   UID's root-owned pin means host mode, otherwise direct mode. The workspace id from `config.json` must be 32 hex
   characters, and for a registry entry its `root` must be the directory the CLI found (or the parent of its
   `orchestrator/`): a `config.json` that names another registered workspace is `host.unavailable`, never routed there.
   An unreadable registry or pin counts as registered. A
   registered workspace whose host is down answers `host.unavailable` (exit 9, retryable, the catalog entry that already
   existed for `init`), never a direct write for the same state dir (an agent pointed at another `ORCH_STATE_DIR` is
   the accepted H2a limit: it holds no workspace key there). `help`, `describe`, `init` and `import v1` are always local (the host does
   not serve them; the direct-mode guard still answers `host.required` for the last two on a registered workspace).
   `task done --run` in host mode runs `verify.cmd` on the client (see §0.8, `task.done`) and sends a receipt; it never
   forwards `run`.
   A socket whose owner or welcome does not match the pin is `host.untrusted` (exit 3, in the catalog, never retried).
2. **Rendering.** A registry op is sent with its parsed arguments; `result` is rebuilt as `orch.ops.Result` and rendered
   by the unchanged renderer, so text and `--json` are byte-identical to direct mode for every non-human op, `wait`
   included. A refusal keeps the exit the socket sent (so `lock.busy` is 9). Input, `human_only` and grant-shape checks run
   locally first (same errors, no round trip); the stop rule and session dedup are the host's.
3. **Idempotency.** Every write carries a fresh `opts.idem` (`a_<ULID>`). A lost answer (timeout, dropped connection,
   `outcome.unknown`) is sent again with the same key on a new connection (at most three attempts in all, `RETRIES`); a blob-sourced file is then
   named by `{digest}` and falls back to the blob when the host has no record. `Connection.call` gained `auth`, `dry_run`
   and a per-call socket `timeout`; `wait` uses its `--timeout` plus 30 s. It still skips `evt` frames: nothing
   in the CLI subscribes.
4. **Files.** `x-file` arguments are read by the CLI (path or `-`); the argument keeps what the user typed as its logical
   name. Up to 512 KiB in all they go inline, else one blob each (`blob.put` in 768 KiB chunks, with the call's `auth`).
5. **Person commands** run `orch.host.signer.run` with the terminal signer (the person's registry entry, the device key
   in `<state>/hosts/<ws>/person/`, the review on `/dev/tty`); an agent UID or `ORCH_GRANT` gets `human_only`. A declined
   review is `invalid.input` "not confirmed: nothing was signed", as in direct mode. A `human.submit` whose answer is lost
   is sent again with the same idem and identical events (three attempts); if still uncertain the error names the idem
   (never a silent new request) and says a grant's secret cannot be shown again. Files signed this way are inline
   only (512 KiB). `--dry-run` calls `human.prepare` with `dry_run`. Since H7 the host answers it with the inner op's
   would-be `result` and the CLI renders that as direct mode renders its dry run; against an older host (no `result`)
   it prints `ok <op> dry-run`, `dry-run: nothing was written` and `would sign: <event types>`.
6. **Run directory.** `ORCH_HOST_RUN_DIR` when set, else the service run directory when a host serves there, else
   `<state>/run`. It says only where to look and is not a trust input: the registry or the pin and the handshake decide
   whom the socket may belong to.

**As built in H11f** (quick tasks, addon #308, D-P2-35; additive, cap `quick/1`, no new event type and no new store):

1. **A quick task is a ticket** with `type: chore` and the label `quick`. Nothing records "quick" except that label, so
   claims, grants, evidence, visibility, approval gates and the log work unchanged, and a task that outgrows the idea is
   made an ordinary ticket by removing the label (`orch set KEY labels=…` with the other labels, or `orch set KEY
   labels=` for none: an empty value is the empty label set).
2. **`quick.list`** (read; columns P, G and N, like `ticket.query`: N sees `workspace` tickets only; registry op in the
   CLI, so `orch describe quick.list` documents it) takes `{status?:[Status], limit?:int 1..200 = 100, page?:str,
   max_commits?:int 1..100 = 1, max_files?:int 1..1000 = 3}` and answers `{rows, next_page, total}`. The limits are
   flat arguments instead of the draft's `limits{}` object so the CLI can spell them (`--max-commits`, `--max-files`).
   - On the host the rows are `ticket.query {label:["quick"], sort:"key", order:"desc"}` for the same viewer (one
     code path: restricted tickets are never listed or counted, a later page rebuilds from the current state, closed
     tickets only when `status` asks) and each carries `quick: {commits, files, over}`. Direct mode returns the same
     fields minus the ones only the host's `ticket.query` rows have (`gates`, `needs`, `claim`, ...); its `page` token
     is an offset (`o:<n>`), the host's is the opaque `ticket.query` token. Text and `--json` of the CLI are the same
     in both modes except for the token and those extra row fields.
   - `commits` and `files` come from the ticket's linked branches (the `diff` plan and reader, `context 0`: every
     commit of `base..head` per linked repo including `commits_more`, and the changed files). `null` for all three
     (`over` too) when the ticket links no repo, has no branch or working copy, or the reader fails or is out of time:
     the numbers are a display and never an error. `over` is `commits > max_commits or files > max_files`; **the limits
     are display only**, nothing refuses a quick task that is over them.
   - The host answers in the three phases of `diff` (worker thread: rows and plans; reader executor: at most 20 s for
     all rows together, one in flight per principal shared with `diff` and counted in the host-wide diff cap, so
     `quota.host` when a diff or list of the same caller is running; worker thread again: column and visibility
     re-checked, a row hidden meanwhile is dropped from the page and `total` is reduced by the rows dropped **on this
     page only**: a ticket on another page hidden during the read stays counted until the next call). Direct mode has
     the same 20 s total budget for its reader calls, and answers a bad `page` token `invalid.input`. `welcome.ops` lists `quick.list` at 1 and `welcome.caps` lists `quick/1`.
3. **`orch quick add "TITLE"`** is a CLI composite, not an operation: `new TITLE --type chore`, then `set KEY
   labels=quick`, each an ordinary call in direct or host mode (one title, one line, 1 to 120 characters, else
   `invalid.input`). It prints `ok KEY quick.add created|labelled seq=N` (or the `--json` envelope with `{key, title,
   type, labels, created}`). The draft's single-request person click flow (host spec §9 Q8) stays open: the dashboard
   signs `new` and then `set`.
4. **A failure between the two steps** leaves an ordinary chore ticket and answers the new catalog code
   **`quick.unlabelled`** (exit 9, retryable; the message names the key, the step's own code and the manual fix
   `orch set KEY labels=quick`). Running the same `quick add` again labels that ticket instead of creating a second
   one. It adopts a ticket only when **all** hold: it is among the 50 newest, an exact title match, a `chore` with no
   label, no claim and exactly one event (`ticket.created`); that event's actor is the caller's own (an agent with the
   same grant id and the same session as the call, read from `show KEY --log --json`, whose `ticket.created` object
   now carries `{at, actor:{kind, for, grant, session}}`); and it was created within the last 10 minutes. Anything else
   (another person's or another grant's or session's chore, an older one) is never taken: a new ticket is made. Within
   the CLI's own 15-minute dedup window an identical `new` in the same session is answered from its record, so a retry
   of the whole command may also come back through that path with the same key. `quick.unlabelled` is raised by the
   CLI only (no operation declares it).
5. **Not built:** the quick-tasks addon manifest and its owner `needs` rule (manifest-only addons are D-P2-36 and
   belong to the addon runner work) and the dashboard UI.

**As built in H7** (host mode on by default for registered workspaces, phase 1, tier A):

1. **The per-user service (`orch.host.enable`).** `orch host enable` installs the release (D-P2-4), writes the unit and
   starts it through the person's own service manager: a LaunchAgent `~/Library/LaunchAgents/io.orch.host.plist`
   (`launchctl bootstrap gui/<uid>`, `kickstart` when it is loaded and unchanged, `bootout` + `bootstrap` when it
   changed) or a systemd user unit `~/.config/systemd/user/orch-host.service` (`daemon-reload`, `enable`,
   `start`/`restart`). The unit goes where the user's systemd manager searches: `~/.config` of the account, not the
   CLI's `XDG_CONFIG_HOME` (a dev kit that sets one would put it where `systemctl --user` never looks). Windows and other platforms get no service: `mode: "direct"`, nothing installed. The unit runs
   `python -I <release>/current/bin/orch-host run --state <state> --run <state>/run`: in tier A the run directory is
   `<state>/run`, where the CLI, `orch sign` and the dashboard look; the root-owned tier-B directories come with H3c
   (phase 3). On macOS the host's output goes to `<state>/host/host.log` (`<state>/host` is created 0700 before the
   bootstrap: launchd opens the log before exec); on Linux it goes to the journal (`journalctl --user -u
   orch-host.service`). `enable` then waits up to 20 s for the control socket and answers `host.unavailable` naming
   the log or the journal command when nothing answers.

   Refused before anything is written (review of #422): a run directory whose socket paths would pass 100 bytes; a
   control character in any unit value (the systemd `ExecStart` uses systemd's own quoting: double quotes, `\` and `"`
   escaped, `%` as `%%`, `$` as `$$`); an interpreter or `orch` package inside a git work tree, a `.worktrees` checkout
   or a registered workspace (the service would keep running whatever that tree holds next) unless `--dev`; and a unit
   already installed for another state dir, run dir or interpreter (one label per UID): `host.busy` naming the fields,
   unless `orch host enable --replace`. `register` never replaces. The default command runner refuses to call
   `launchctl` or `systemctl` while `PYTEST_CURRENT_TEST` is set or `ORCH_NO_SERVICE_MANAGER=1`.
2. **Registration turns host mode on.** `orch host register DIR` first checks the control socket; a host that answers
   (the service, or a dev host from `orch host run --foreground`) is used as it is, otherwise the service is enabled as
   above (`--no-service` never touches it). `--peer` is now optional: by default this UID maps to the person whose device
   key for the workspace is on this machine, in either custody (the passphrase key `orch init` made, or the convenience
   key after click-to-sign: `custody.select.device_backend`, public key only), matched against the workspace's devices. A
   removed, revoked or expired key, one without `decide`, or one whose person left counts as none: then `--peer` is
   required. The host checks the mapping again (F8). From then on the thin CLI (H4b) and the dashboard serve the workspace through the
   host; `orch init` itself stays direct (see §3), and `orch init --host` remains for an empty directory.
3. **`orch host status`** (alias `doctor`, `--json` for the document): the service (unit present, current, loaded,
   running, or none on this platform), whether a host answers on the control socket, every registered workspace (the
   host's state, whether its socket answers, the mapped person, click-to-sign on or off), the mode the CLI uses for the
   current directory (exactly `hostclient.locate`), and the problems with their fix (`orch host enable`,
   `orch host restart`, a socket path over 100 bytes). An installed unit that differs is reported by field (`python`,
   `state`, `run`); a service for another state or run dir is a problem, but status never proposes replacing it (that
   is the person's decision, `enable --replace`), and an interpreter that differs alone is no problem. Every printed
   value has its control characters escaped. It changes nothing. Exit 0 without problems, else 1.
   **Run directory, tier A:** `orch host`, the thin CLI and `orch sign` all use `--run`, else `ORCH_HOST_RUN_DIR`, else
   `<state>/run` (`enable.run_dir_for`). A path that merely exists under the tier-B `/Users/Shared/orch/run/<uid>` is
   no longer preferred: any local account can create it (H4b preferred it). **`ORCH_HOST_RUN_DIR` is the only
   environment name for the run directory**: core reads it in `enable.run_dir_for` alone (host, runner, thin CLI,
   `orch sign`, the runner unit's environment) and never reads `ORCH_RUN_DIR`; a client in another package (the
   dashboard server) uses the same name and the same fallback to `<state>/run`.
   **`orch host disable`** is the reverse of `enable`, through the same unit plan and `Manager`: it stops and unloads the
   service (`launchctl bootout gui/<uid>/io.orch.host`; `systemctl --user disable --now orch-host.service`, then a
   `daemon-reload`) and removes the unit file. The state dir always stays. `--purge-releases` also deletes the release
   copies under the release dir (the runner service runs from them too): they are read-only, so each directory gets
   `u+rwx` first; nothing outside the release dir is touched and no symlink is followed (`current` is unlinked itself;
   a release dir that is a link is refused). A unit installed for another state dir or run dir (or one that cannot be
   read, or a loaded label with no unit file here) is refused with `host.busy`, as `enable` refuses to replace one,
   unless `--force`. Nothing installed is a success (`removed: false`). Registered workspaces then answer
   `host.unavailable` until `orch host enable`; `status` names that fix.
   **`orch dashboard [args]`** (`orch.cli.dashboard`) replaces the process with the dashboard addon's entry point and
   passes the arguments through (`orch dashboard open`): the `orch-dashboard` script beside this interpreter, on `PATH`,
   else `python -m orch_dashboard`. Not installed: exit 3 and a message that names how to install it.
4. **Parity.** Every case of the agent and person golden suites (`tests/ops/golden`, `golden_human`, `wait` included)
   runs in host mode on the same fixed workspace, built through a host from the start, and matches the same golden file
   (`tests/host/test_golden_host.py`). The one designed difference: `task done --run` in host mode names on stderr the
   command it runs and adds the "attested by you" note (H3b2). To get there: the request record keeps the inner op's
   `lines` (a grant's "the secret was shown" line), a grant prepared through the host carries the direct-mode hint, the
   CLI orders a person op's `data` by the op's output schema (the record is stored as canonical JSON), and a dry-run
   `human.prepare` returns `result` (above). Parity that needs a group not merged yet is `xfail(strict)` with its PR
   (`tests/host/test_parity_pending.py`: `verdict`/`show` on a linked repo and `diff`, which waited for #413 and #417, pass).
5. **Op versions on the wire.** `welcome.ops` lists `task.done` at 2, as §0.4 says (it said 1 until H7), and the thin
   CLI's hello sends `ops: {"task.done": 2}`. A client that does not list it (the v1.4 CLI) gets `op.unsupported` for
   `task.done`, never a contract-1 run: see the fixtures (D-P2-14).
6. **Cross-version fixtures (D-P2-14).** `tests/host/fixtures/cross_version/v1_4_clients.json` holds the hello and
   request frames of the v1.4 clients (the H4b CLI, the dashboard's host link at 8873a4f8). They are written by hand from
   those sources (the hello frames copied from the client code at those commits, the requests in the exact shape
   `Connection.call` builds), not captured off a socket, and the rules each must meet
   on the current host (`tests/host/test_cross_version.py`): no shared major is `fatal host.version_mismatch` before any
   `welcome`; another op version is `op.unsupported` for that op only, nothing appended, the connection keeps working;
   an unknown envelope key is `req.invalid`; every key an old client reads is still sent with the same JSON type.
7. **Session notes do not migrate.** A workspace used in direct mode and then registered keeps its logs, but an agent
   session's notes (cursor, claims) are keyed by session alone in direct mode and by (principal, session) in host mode
   (F4); the host does not adopt the old ones, because a session label is public. A running agent session sees no
   current claim after registration and claims again (`orch claim KEY`). Register before starting agents.

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

#### 0.8.5 v1.5 additions (provisional draft)

v1.5 answers §9 item 8 (the dashboard stream's v1.5 list). Every change is a new op under its own cap, an optional result
key under `read_ext/2`, a new argument behind a cap, or the workspace-topic filter of §0.9 (§0.4). Nothing here is
derived from data F1 does not store, except the named exceptions: **host records** (`session.refusals`, `grant.list`
`session_ids`; not signed, not in a log), **git reads** (`diff`, `commit.list`), and the configuration values
`workspace.settings` gives N.

| §9 item 8 | v1.5 answer | Cap | Group |
|---|---|---|---|
| settings and policy read | `workspace.settings` (new op) | `settings/1` | H6b |
| full texts beyond the `Snip` | `ticket.text` (new op) | `text/1` | H6b |
| bound hashes | §0.8.1 "Bound hashes" (what `human.prepare` binds); `ticket.get` hash fields | `read_ext/2` | H5a (the signer table), H6b (the fields) |
| `revoked.by`, grant `session_ids`, claim `idle_until`, answer `auth`, `Closed.seq`/`event_id` | `grant.list`, `ticket.get`, `ticket.query` fields | `read_ext/2` | H6b |
| member and grant ops through `human.*`; `last_seen` | registry ops named (no new op); `last_seen` not served | — | — |
| `via: factory_charter` | **not expressible**: F1 stores no `via` (below) | — | P2-later, after an F1 amendment |
| agent refusals | `session.refusals` (new op, host records) | `refusals/1` | later than H6b (H6c) |
| workspace-wide artifact listing | `artifact.query` (new op) | `artifact_query/1` | later than H6b (H6c) |
| live-tail payload | `sub.open {detail:true}` delivers `ActivityItem`s | `sub_ext/1` | H6b |
| workspace-topic filter (security, Low) | §0.9 per-type rule, every subscription | none: a fix | **H6a, required** |
| several heads and bases per repo; commits | `diff {head, base}`; `commit.list` (new op) | `diff_ext/1` | later than H6b (H6c, with §9 Q9) |

A client must not call an op or send an argument whose cap it did not see (§0.4). A host may list `settings/1`, `text/1`,
`read_ext/2` and `sub_ext/1` without the H6c caps.

**`workspace.settings {limit?:int 1..32 = 32, page?:str{1..512}}`** (read, cap `settings/1`) →

```
{settings:{grant_hours:int 1..24, claim_ttl_min:int 15..1440, lease_ttl_min:int 5..1440,
           repos:{RepoName: RepoSetting}{0..32}},
 next_page:str | null,
 gates:{Gate: Policy},
 defaults:["grant_hours"|"claim_ttl_min"|"lease_ttl_min"]{0..3},
 settings_at?:{seq:int, at:Time, by:Person} | null,
 policy_at?:{seq:int, at:Time, by:Person} | null,
 sub_cursor:Cursor}
RepoSetting = {default_branch:str | null, path?:str, remote?:str | null}      // path, remote: owners and maintainers (P)
```

- **What it returns.** The workspace settings and default gate policies as the verified workspace log defines them at
  `sub_cursor` (F1 §2 `config.json` mirrors the same events, F1 §5.4.2). A duration that no `settings.changed` has set has
  the F1 §2 config default (`grant_hours` 8, `claim_ttl_min` 120, `lease_ttl_min` 60) and is listed in `defaults`.
- **`gates`** holds all four gates: the workspace default policy of each, from the latest workspace-log `policy.changed`
  naming it, else the initial default (F1 §5.11), in canonical form (F1 §5.7). A ticket's override and its effective policy
  stay in `ticket.get` (`GateView.policy`).
- **`repos`.** For P and G: every repo, with `default_branch` or `null`. `path` and `remote` are served **only to
  person-class owners and maintainers** (column P with that role; a G caller acting for one is not served them): `path` as
  stored (F1 does not normalise it), `remote` as declared or `null`. A declared `remote` is an F1 §5.7 canonical identity
  (`https://host[:port]/path` or `local:<name>`), which by F1's rule carries no userinfo (scheme://host/path only); a
  stored value that does not parse as such a canonical identity is omitted (the key is absent), never repaired. Other
  persons get the name and `default_branch` only. **For N** (§0.5): N receives the three effective durations and four workspace default gate
  policies as an explicit exception permitting these workspace-derived configuration values. Its `repos` contains only
  repositories named in `links.repos` of at least one ticket currently visible to the viewer. Each entry contains only
  `default_branch`; `path`, `remote`, `settings_at` and `policy_at` are absent. Filtering occurs before serialization and
  counting. N receives no workspace-log events. The observed identity of a repo is not a setting (it is in `ticket.get`
  `source` and in `diff`).
- **`settings_at`, `policy_at`**: the `seq`, `at` and signer of the latest workspace-log `settings.changed` and
  `policy.changed`. Absent for N; for P and G always present, `null` when none followed the genesis.
- **Not in v1.5:** `agents.run_for` and the addon table (`addon.list` serves addons). A claim's inactivity expiry is
  computed by the host (`ClaimFull.idle_until`, below), so a client need not combine `claim_ttl_min` itself.
- **Live updates.** A P or G viewer re-reads on a `settings.changed` or `policy.changed` item of the `workspace` topic.
  N may poll `workspace.settings`; reconnect is not a configuration-change notification.
- **Paging.** The 32-entry bound on `settings.set.set.repos` limits one update, not accumulated settings.
  `workspace.settings` returns repository entries through authenticated snapshot paging, at most 32 per page, ordered by
  repo name. Page tokens bind the principal, workspace epoch, snapshot and arguments; each page reapplies current
  visibility. Scalar settings and policies refer to the same snapshot (every page repeats them). No entries are silently
  truncated. Errors: `page.expired`, `invalid.input` (a token with other arguments).

**`ticket.text`** (read, cap `text/1`) args and result:

```
{ref:Ref, seq:int 1.., field:"text"|"reason", event_id?:ULID /* required when offset > 0 */, offset?:int 0.. = 0,
 length?:int 4..262144 = 262144}
→ {uid:Uid, event_id:ULID, type:str, at:Time, actor:Actor, field:"text"|"reason", text:str, bytes:int, offset:int,
   next:int | null}
```

- **What it serves.** The full stored text of one field of one event of the ticket's verified log, for exactly these pairs
  (every place where v1.4 returns a `Snip`, plus the reasons that `LogItem.data` returns as stored):

  | Event type | `field` |
  |---|---|
  | `log.added` (comments and notes), `handoff.written`, `ticket.closed`, `ticket.reopened`, `question.answered`, `gate.changes_requested`, `verdict.given`, `task.done` | `text` |
  | `task.skipped`, `task.blocked`, `task.reopened`, `status.changed`, `ticket.moved` | `reason` |

  `Decision.text`, `Verdict.text` and every `Snip` in `LogItem.data` name their event by `seq`, so the client calls
  `ticket.text {ref, seq, field:"text"}` for the rest. An event at `seq` of another type, or without that field, is
  `not_found`.
- **Workspace-log texts are not served** (`grant.revoked` and `device.removed` reasons, `restore` notes,
  `invalid.acknowledged` reasons): the workspace-log rule of `activity.list` returns no free text.
- **Chunks.** `offset`, `length`, `bytes` and `next` are counts of UTF-8 bytes, not Unicode scalar values or UTF-16 code
  units. `length` is an integer from 4 through 262144. The returned text is the longest prefix of the requested byte
  interval ending on a Unicode scalar-value boundary. For `offset < bytes`, every successful response returns nonempty
  text and `next`, when non-null, is strictly greater than `offset` (a scalar value is at most 4 bytes). The scalar
  boundary may split a grapheme cluster; clients concatenate chunks before rendering. `offset = bytes` gives `""` with
  `next: null`; an `offset` past the text or inside a scalar value is `invalid.input`. F1 caps an event line at 524 288
  bytes (the whole signed line, so a stored text is shorter), so with the default length a text takes at most two chunks;
  smaller requested lengths require more.
  `log.added` (4 096 bytes) and `handoff.written` (2 048) take one at the default length. 262 144 bytes stay under the
  frame even when every byte is JSON-escaped (6 × 256 KiB).
- **Event identity.** Each response includes `uid` and `event_id`. A request with `offset > 0` without `event_id` is
  `invalid.input`. Requests continuing a prior response must provide its `event_id` and address its returned `uid` (as `ref`). The host verifies that the event currently at `seq` has that id;
  an absent, abandoned or replaced event returns `not_found` (F1 §5.10: a `restore` appends at `from_seq + 1`, so later
  events can reuse abandoned `seq` positions). Visibility is checked on every request. A client discards partial text on
  failure and never concatenates chunks with different event identities.
- **Visibility** is `ticket.get`'s (§0.5): a ticket the viewer may not see is `not_found`. The text is the actor's stored
  content: a client renders it as untrusted text attributed to `actor`, as `LogItem.data`.

**`ticket.get` fields (v1.5, `read_ext/2`).**

- **`Question.qid`, `Question.hash`**: the `qid` and question hash of the `question.asked` that defines the question now
  (F1 §5.6). `Question.answer.auth?:str` is the custody label of the counting `question.answered` (F1 §5.3, metadata, not
  proof; F1 replaced `presence` with `auth`).
- **`GateView.hash`** is the full current gate hash (`H("orch/v2/gate|" || cj(G))`), and `hash12` its first 12 hex
  digits; it is `null` exactly when `hash12` is `null`. **`GateView.policy_hash`** is the effective policy's hash as F1
  §5.6 defines it; F1 makes it undefined for a **blocked** gate, so it is `null` there (and for a gate whose `hash12` is
  `null`).
- **`Decision.hash`, `Decision.policy_hash`**: the hashes the decision event signed.
- **`ClaimFull.idle_until`** = `last_at + claim_ttl_min` (the setting at the read). The claim lapses earlier when its grant
  ends; the grant's end is not shown here (it is `grant.list` data), and `live:false` reports the lapse.
- **No `via`.** F1 stores no `via` field and no charter or mandate event (F1 §12 N10 and decisions record entry 55: D61
  and D62 are not in the format). Every decision is a person's signature, so v1.5 adds no `via` and a client shows none.
  When F1 adds charter or mandate decisions (P2-later), `Decision.via?` and `Verdict.via?` come with a later `read_ext`.

**`grant.list` fields (v1.5, `read_ext/2`).**

- **`revoked`** is present exactly when an actual `grant.revoked` event names the grant: `by` is its signer, `at` its time,
  `seq` its workspace-log `seq`. A grant that ended for another reason (expiry, member removal, a device revocation) has
  no `revoked`. Its free-text `reason` is not returned.
- **`sessions`** is the total number of sessions matching this grant in the same host session-record snapshot used to
  construct `session_ids`, before the 100-entry output cap. `session_ids` contains the first 100, ordered by `last_at`
  descending and session id ascending. `sessions_more` is true exactly when `sessions > len(session_ids)`. The list follows
  `grant.list` visibility; embedded claims and leases (in `session.list`) retain their ticket visibility filters. These are
  host session records (the ones `session.list {active:false}` lists), not sessions reconstructed from F1 events.

**`session.refusals`** (read, cap `refusals/1`, later than H6b) args and result:

```
{session?:str{1..64}, ticket?:Ref, since?:Time, until?:Time, limit?:int 1..200 = 50, page?:str{1..512}}
→ {items:[Refusal]{0..200}, next_page:str | null, as_of:Time}
Refusal = {id:str/r_[0-7][0-9A-HJKMNP-TV-Z]{25}/, at:Time, session:str, person:Person, grant:str, agent:str | null,
           op:OpName, code:str, exit:int 1..9, retryable:bool, hint:str{1..400} | null, key:Key | null, stop:bool}
```

- **Host records, not F1 data.** F1 stores no event for a refusal ("Not events", F1 §5.4.2); refusals live in session
  records. The host writes one **refusal record** for every answer with `ok:false` and `retryable:false` to a request in
  column G that carries `auth.session`. Requests without a verified grant or without a session are not recorded (no
  person or session to attribute them to). A record is host-attested and unsigned, it is in no log, `activity.list`, count
  or subscription, and a client labels it "from the agent session, not part of the signed log".
- **What a record holds**, and nothing else: the time, the grant and its person, the session, the op name, the error code
  and exit, the scope and associated ticket uids (below; `key` is the current key of the ticket for a refusal with exactly
  one associated ticket, else `null`), and `stop` (the stop rule answered). Stored `op` and `code` are validated
  registry/error identifiers, never arbitrary request strings. `retryable` is the answer's flag (always `false` for a
  recorded refusal today; kept so the rule may widen). `hint` is the error catalog's **static** hint for the code
  (`orch.ops.errors`, F1 §10.4a; its `{cmd}` placeholder filled with the validated op name), or `null` when the catalog
  has none; it is looked up at read time, never stored from the
  answer. **Never** `args`, `files`, the runtime error `message`, runtime `hint` or `fix` (they may echo arguments), and no
  secret. `agent` is `session.list`'s agent id for the session, or `null`.
- **Ticket association.** The dispatcher classifies each refusal as workspace-scoped or ticket-scoped. A ticket-scoped
  record retains internally every ticket uid resolved while processing the request, including nested operations and
  alternate ticket selectors. It is returned only if the viewer may currently see every associated ticket. A ticket-scoped
  refusal for which complete association cannot be established is not recorded. `key:null` must not be used to bypass
  ticket filtering.
- **Privacy.** The `grant.list` rule: a person-class owner sees every person's refusals, any other P their own, G its own
  person's (a G caller acting for an owner is not a person-class owner), N none (`op.not_allowed`). A `session` or
  `ticket` the viewer may not see is `not_found`.
- **Retention and budgets.** 7 days, at most 1 000 per (principal, session) and 100 000 per workspace, the oldest dropped
  first. Recording is subject to bounded per-principal and workspace ingestion budgets. Excess records may be dropped;
  this diagnostic listing is not a complete audit trail, and recording must not block request dispatch.
- **Paging.** Newest first. Refusal rows are filtered before pagination. The `page` token is authenticated and binds the
  workspace epoch as well as the principal, snapshot, arguments and position by `(at, id)`; restore or scope changes
  invalidate incompatible tokens (`page.expired`). Tokens expire after 10 minutes, and every page is filtered for its own
  request's viewer. No `total`; records written after the first page are not in later pages. No live tail: a client
  re-reads when the session's events arrive or on demand.

**`artifact.query`** (read, cap `artifact_query/1`, later than H6b) args and result:

```
{ticket?:Ref, status?:[Status]{1..8}, kind?:[str{1..32}]{1..16}, evidence?:bool, addon?:bool,
 actor_kind?:["person"|"agent"|"host"|"addon"]{1..4}, unattended?:bool, q?:str{1..200}, counts?:bool = false,
 sort?:"at"|"name"|"key" = "at", order?:"asc"|"desc" = "desc", limit?:int 1..200 = 50, page?:str{1..512}}
→ {rows:[ArtifactRow]{0..200}, next_page:str | null, total:int, sub_cursor:Cursor, as_of:Time,
   counts?:{str: int}{0..256}, counts_more?:bool}
ArtifactRow = {key:Key, uid:Uid, title:str, status:Status, restricted:bool, artifact:Artifact}
```

- **What it lists.** The entries of every visible ticket's replayed manifest (F1 §6), one row per current name, with
  `ticket.get`'s `Artifact` (its `evidence` flag, `replaced` count and `by`). It lists metadata only; it is not the reserved
  `artifact.search` and never reads content (`artifact.get` does).
- **Filters** combine with AND, values inside one filter with OR. `addon:true` keeps addon artifacts, `false` file
  artifacts; `unattended` tests `by.unattended`; `q` is a case-insensitive substring (NFC, casefold) over `name` and
  `label`. With no `status`, closed tickets are left out, as in `ticket.query` (§0.8.4).
- **Paging and `total`**: `ticket.query`'s snapshot rules, page tokens and per-request viewer filter. Ties break by key,
  then name, ascending.
- **`counts`** (only with `counts:true`, first page only): the number of matching rows per artifact `kind`, computed
  after the visibility filter, on the same snapshot and with the same filters as `total` (so the counts sum to `total`).
  At most 256 kinds, the largest counts first (then kind ascending), with `counts_more:true` when more kinds exist. Later
  pages leave `counts` out; `counts:true` with a `page` token of a first page without it is `invalid.input`.

**`diff {head, base}`** (cap `diff_ext/1`, later than H6b). `repo` is required when either is given (`invalid.input`
otherwise), and `sources` then holds that one repo. `since` is mutually exclusive with `head` and `base`; combining them
is `invalid.input`. `sources[].base_source` gains the value `"pushed"` (§0.8 schema), sent only when the client gave
`base`.

- **Authorization** is scoped to `(ticket uid, current linked repo name, verified repo identity)`. Historical
  `branch.pushed` entries qualify only when their `repo_id` matches that identity. A `before` endpoint qualifies only when
  its own `repo_id` matches and its recorded ref matches the push's ref.
- **Root heads** are the observed head and the qualifying pushed heads (`sha` of a qualifying `branch.pushed`). For each
  root head, the host chooses its default base using §0.8 independently of caller-supplied `head` or `base`. Additional
  permitted heads are commits in those fixed `base..root_head` ranges. Caller-selected endpoints never enlarge these
  ranges. The existing reader budgets (§0.11) apply when establishing membership; a membership that cannot be established
  within them is `not_found`.
- **Permitted bases**, each of which must be an ancestor of the selected head: a qualifying pushed `sha` or `before.sha`
  (`base_source:"pushed"`), the head's first parent (`"first_parent"`), or the §0.8 base of the head's root range.
  Consequently, a push's `before.sha` is not necessarily an admissible base after a force-push. Without `base`, the §0.8
  rule picks it. Anything else, including a commit that exists in the repo, is `not_found`.
- **Disclosure boundary.** This rejects arbitrary repository objects outside the authorized history ranges. It does not
  establish exclusive ticket ownership of commits: shared or merged history may contain changes associated with other
  tickets. Git content reachable through these authorized ranges is disclosed to viewers of this ticket (F1 records no
  commit provenance that would allow a stronger promise).
- **Several heads or bases per repo** are several calls, one pair each; each call is one git reader child under the
  §0.11 queue limits. A per-push view is `{head: <push sha>, base: <its before.sha>}` when that base is admissible, a
  per-commit view `{head: <sha>, base: <first parent>}`.
- **Display only.** The approval screen's default stays the observed head and the §0.8 base. An approval signs only the
  observed head (D58); a client labels any other head "an earlier push" or "one commit".

**`commit.list`** (read, cap `diff_ext/1`, later than H6b) args and result:

```
{ref:Ref, repo:RepoName, head?:Sha, base?:Sha, limit?:int 1..500 = 100, page?:str{1..512}}
→ {repo:RepoName, branch:str | null, head_sha:Sha, base_sha:Sha,
   base_source:"host_pinned"|"local_ref"|"first_parent"|"pushed",
   commits:[CommitItem]{0..500}, next_page:str | null, walked:int, walk_cut:bool}
CommitItem = {sha:Sha, parents:[Sha]{0..16}, parents_more?:bool, time:Time, subject:str{0..200}, subject_cut?:bool,
              author:str{0..200} | null, author_cut?:bool,
              pushed:[{seq:int, at:Time, ref:str}]{0..20}, pushed_more?:bool}
```

- **What the host can serve.** F1 stores only `branch.pushed` (a ref's `sha` and its `before`), never commits. So a
  commit list is read from git by the host reader, as `diff`'s v1.4 `commits` list is: it is an explicit exception to
  §0.5's "what F1 stores" rule, and display only. The range is `base..head`, with `head` and `base` authorized and chosen
  exactly as for `diff {head, base}` above (`base_sha`, `base_source` as in `diff`'s `sources`).
- **Per commit:** the id; at most 16 parent ids in stored order, with `parents_more:true` when additional parents exist;
  the committer time; the subject (the message's first line, cleaned of control and bidi characters as in `diff`, at most
  200 characters with `subject_cut`); and `author`, the git author **name** only (no email, no time), cleaned the same way,
  at most 200 characters with `author_cut`, `null` when the header has no parseable name. Subject and author are
  agent-writable repository content: untrusted display data, never an identity (a client never maps `author` to a
  person). The reader parses only the author name for this (H6c adds it); no body and no file list. A per-commit file list
  is `diff {head:<sha>, base:<first parent>}`.
- **`pushed`** joins F1's data: the `branch.pushed` events of this ticket (with a matching `repo_id`) whose `sha` is this
  commit, at most 20 from the fixed ticket-log snapshot, newest `seq` first, with `pushed_more:true` when truncated.
- **Snapshot and order.** The first request fixes the authorized repo identity, endpoint SHAs, ticket-log snapshot used
  for `pushed`, and a deterministic commit order: committer time descending, then SHA ascending. Subsequent pages use
  those fixed values and recheck current ticket visibility. Reader work is bounded, including authorization and ordering
  work: one request walks at most 10 000 commits (§0.11), and `walked` counts them.
- **Continuation.** Pagination must either resume a bounded traversal from authenticated host state or terminate with
  `walk_cut:true` and `next_page:null` when the walk budget prevents continuation. It must not repeatedly rescan an
  unreachable prefix or imply that a cut result is complete. The page token is authenticated and binds the principal,
  epoch, arguments and the fixed values above; it expires after 10 minutes (`page.expired`).
- `diff`'s v1.4 `commits` list (the newest 50, `{sha, subject}`) stays unchanged.

**Closing reasons (v1.5, `read_ext/2`).** `Closed` (in `ticket.query` rows and `ticket.get`) gains `seq?:int` and
`event_id?:ULID`, the closing `ticket.closed` event's, so a client fetches the full reason with `ticket.text {ref:<uid>,
seq, field:"text", event_id}`. (`ticket.get` `closed.text` is already full; the row's `Closed` has no text.)

**Member and grant ops through `human.*` (v1.5 clarification).** The dashboard prepares these registry ops, all
`who: human`, with `human.prepare {op, args}`; each emits one existing F1 event, so nothing new is defined:

| Registry op | F1 event (§5.4.2) | Who may sign |
|---|---|---|
| `member.add` | `member.added` | owners; maintainers for the roles member and viewer |
| `member.role` | `role.changed` | owners |
| `member.remove` | `member.removed` | owners; maintainers for the roles member and viewer |
| `grant` | `grant.issued` | §0.8.1 (the signer draws the secret) |
| `grant.revoke` | `grant.revoked` | F1 §10.1 A3 (owners any grant, others their own) |

`member.add` needs the invitee's own `pk_pub` and first `device_cert` (F1: the invitee makes their own key); the owner
signs only the roster change.

**`member.list` `last_seen`: not served in v1.5.** F1 records no presence, and the host's only honest source would be its
own last-connection time per person-class UID. That says when a UID connected, not that a person was present (any
process of the UID connects in tier A), and it would expose activity timing to every viewer, `no-person` included, under
`member.list`'s rule. A later version may serve it as a host record, display only, under the `grant.list` person rule.

**Not in v1.5** (outside §9 item 8, still unserved): search snippets, per-status counts on `ticket.query`, `url` and
`preview` for link and inline artifacts, `last_seen`, and charter or mandate decisions (above).

#### 0.8.6 Click-to-sign (convenience custody, H5c, D-P2-23)

**Status: built in H5c, off by default** (12 Oct 2026; design challenged by Codex and Opus, amendments adopted by
orch-52). Owner decisions of 12 Oct 2026 (coordination log "OWNER RE-PLAN P2" and "D65 amendment", #264 comment
6121460551, and the orch-d6 decisions on the card text and the per-workspace condition): in phase 1 the dashboard signs
with one click. The person's device key sits in a store that needs no user presence (the `convenience` backend), and a
separate **person-class signer process** next to the dashboard holds it. The host still holds no device key and never
prompts (D-P2-15); the prepare → check and recompute → sign → submit contract of §0.8.1, the review, `review_digest`
and the fingerprint are unchanged; `orch sign CODE` (H5b) stays available **while the flag is off** (one signing path per workspace, item 8).
**Accepted risk, shown on the workspace
card, on every sign panel and in the docs: "Convenience signing: agents running as you, or anything that takes over
this dashboard, could sign."** This supersedes D41's per-signature presence for phase 1; H9 (Touch ID) and tier B bring
isolation back in phase 3, and only after migration, UID separation and removal of the old convenience devices.

**0. The trust boundary (read this first).** The signer constructs and validates the review data and binds signing to
the corresponding immutable signing bytes. **The dashboard HTTP process and its browser application are trusted to
display that review and request signing only after confirmation.** The signer cannot verify display or human
confirmation. Control of the dashboard's authenticated signing workflow (an XSS, a stolen bearer token, a compromised
HTTP process) can obtain signatures without showing a review, without reading the key or executing code in the signer.
Code execution in the HTTP process runs as the person, in the same interpreter the keychain ACL trusts, and can read the
key itself. **Process separation protects the key against passive disclosure** (a log, a crash or memory dump of the
HTTP process, a dependency bug that reads files or bodies); **it does not isolate signing authority from the
dashboard.** This is a separate exposure from the same-UID agent risk, which is why the card text names both.

**1. The per-workspace flag and the readers condition (owner decision).**

- Click-to-sign is enabled per workspace by the host-state file `<state>/host/convenience.json` (0600, host UID, in
  the D-P2-17 custody list): `{"v":1, "workspaces":{"<ws id>":{"enabled":true, "at":Time}}}`, written by
  `orch host convenience on|off --workspace W` (a host-UID CLI command, not a socket op). **It is off for every
  workspace until switched on**: the compatibility gate (item 6) is met first.
- **The condition.** Convenience signing is allowed only while every reader of the workspace's log is local and
  updated: no relay pairing, no phone device, no other member's devices or cores, and a model that knows the value. The
  host checks it wherever `auth: "convenience"` enters a request: `human.prepare` with that signer, `human.get` binding
  with it, and `human.submit` of such events. It refuses with **`sign.convenience_unavailable`** (exit 3, not
  retryable) and a reason, first match wins:

  | Reason | When |
  |---|---|
  | `disabled` | the flag is off for the workspace |
  | `relay_paired` | the workspace has a relay pairing (P3: the host's bridge configuration; none exists in phase 1, so the check is a hook the P3 pairing must fill) |
  | `other_member` | the member list holds a person other than the signer's person |
  | `phone_device` | the signer's person has a valid device other than the signing device (the host cannot tell a phone from a second machine, so every other device counts as an outside reader) |
  | `reader_version` | the host's own model does not accept `auth: "convenience"` (an old install behind a new client) |

  `orch sign CODE` with a passphrase key stays available whatever the reason.
- **Upgrade path (owner decision of 13 Oct 2026):** a reader-version handshake is planned for phase 2, with the relay/phone integration. A reader (phone, relay link, member's core) that declares and proves it understands `auth:"convenience"` may then be added; older readers stay refused. Until it exists, use click-to-sign only on workspaces that won't get a phone, relay link or other member yet.
- **Adding a reader to a workspace that has convenience events is blocked** (owner decision): `member.add` (and, when
  they exist, relay pairing and any op that adds a device: one list, `orch.ops.readers.READER_OPS`) is refused with
  **`sign.convenience_readers`** (exit 3, not retryable) and a fix text, in host and direct mode alike, while any event
  in the workspace's history carries `auth: "convenience"`, until the corresponding readers understand the value. The
  host also refuses these ops **while the flag is on**, even before any convenience event exists. Both checks run at
  prepare, again at every `human.get` binding, and again at `human.submit` under the store lock, so a request prepared
  earlier cannot add a reader after convenience events landed. **Switching back to terminal signing
  does not make it safe**: the history already contains convenience events and the new reader must parse them. A
  workspace with convenience enabled but no convenience events yet may switch convenience off first and then add the
  reader. The check scans the merged history (`Store.merged_events`); it runs only for these rare ops.
- **The durable mark `convenience_seen` (#441).** An owner repair (`abandon_tail`, then `restore`) can drop every
  convenience event from the merged history while the record files and any records repo still hold them. So the
  workspace keeps a monotonic mark, `<state>/hosts/<ws>/convenience_seen.json` (0600). It is written at the append
  choke point before the first convenience event is written: the host's `human.finish` (under the store lock; a mark
  that cannot be written fails the request with `custody.unsafe_mode`, nothing appended) and direct mode's `Human`
  append (in the append's `precommit`; refused with `internal`, nothing appended). `Store.abandon_tail` writes it
  too, under the store lock and before the cut, when the cut lines hold a convenience event (only a line
  `canon.parse_event_line` accepts, with `auth` `"convenience"`; torn or non-canonical bytes never count; a mark that
  cannot be written refuses the cut). The host's own scan of the merged history, which also reads the cut tails kept
  in `<root>/.state/abandoned/` by the same canonical rule (legacy workspaces), sets it when it finds one, so an
  existing workspace counts as seen at its first check. Verifying the signatures of cut lines, or an owner-audited
  way to clear the mark, is follow-up #458. **Only these host-side observations write the mark**: the
  records tool's report on the pushed history refuses that one push and never writes it (#454 review: free text an
  agent writes must not block `member.add` for good). Persisting is checked separately from reading
  (`ensure_convenience_mark`: a regular file must be there, else it is written; any error refuses). Nothing clears
  it, a restore included. Every reader-adding check asks it: the host at prepare, binding and submit
  (`refuse_reader_op`), direct `member.add`, and the records export check (D-P2-34). Reading fails closed: anything
  at that path, or an error looking, counts as seen. In tier A the direct-mode and the host state directory are one,
  so a mark set in direct mode holds after `orch host register`.
- **Phase 2 (H12d, draft): the reader-version handshake** (item 9) admits readers that declare and prove level 1
  (they understand `convenience`), with convenience history and while the flag is on, in host and direct mode, and
  relaxes `relay_paired`, `other_member` and `phone_device` to count only readers below level 1. Until H12d merges, this
  item holds as written.

**2. The `convenience` custody backend** (`orch.custody.convenience`, next to `passphrase` and `secure-enclave`; never
imported by `orch.custody`'s package init, the host or the dashboard's HTTP process).

- **What it is.** A person-capable backend (`auth = "convenience"`) that holds only device keys (`dk_sig`, the device
  role) and signs without a factor: any process of the person's UID that calls it gets a signature. It never holds a
  person key (`PK` stays derived from the recovery code, C8), never the `workspace` role, and keeps the label check of
  every backend (`label_allowed`: person events and device requests only). Every load checks the key's public key
  against the device the selection names. It is not the `file` tier (host keys, never a person event).
- **macOS: the traditional login keychain** (`orch.custody.keychain`, ctypes). Phase 1 uses the traditional file-based
  login keychain. It explicitly selects the intended keychain (`~/Library/Keychains/login.keychain-db`, opened by path)
  for creation, lookup and deletion, rather than relying on the default keychain or the search list. One
  generic-password item per (workspace, device): service `io.orch.device-key`, account `<workspace_id>/<device_id>`,
  label `orch device key <prefix> <device_id>`, description "orch convenience signing key (no presence)", not
  synchronizable. The item uses the application ACL the keychain gives the interpreter that created it (the CLI's), with
  no per-signature presence requirement. This implementation does not claim `AfterFirstUnlockThisDeviceOnly`
  semantics, backup exclusion or non-migration: the data-protection keychain needs entitlements an unsigned interpreter
  lacks. The item is encrypted at rest and avoids plaintext key files in the orch state directory; copies of the
  keychain may exist in Time Machine backups and migrations. Keychain locking, ACL denial or an interpreter change may
  require separate user repair; the click path disables user interaction and returns `custody.unavailable` when
  noninteractive access is unavailable. It does not promise that every interpreter upgrade causes exactly one repair
  prompt. **The ctypes wrapper** declares all argument and return types, obtains exported CF constants correctly,
  manages CF ownership, checks every OSStatus, validates returned types and lengths, and the backend verifies the loaded
  P-256 public key against the selected device. Duplicate items and ambiguous matches fail closed. Real integration
  tests use a throwaway file keychain (never the login keychain); they do not establish data-protection behaviour. No
  secret bytes enter subprocess arguments, logs or error messages.
- **Linux and other Unix platforms: a file.** Device keys are regular files owned by the person, mode 0600, at
  `<person state>/person/<workspace_id>/devices/<device_id>.key`, beneath person-owned 0700 private directories.
  Creation and access validate ownership, file type, mode and symlink-free traversal, using descriptor-relative
  operations (`O_NOFOLLOW`, `fstat` on the opened descriptor) to preserve validation through use. Ancestors may be
  root-owned as allowed by `check_tree`. Paths and device identifiers come from validated person-side configuration.
  `<person state>` is the signer's own state directory (`ORCH_STATE_DIR`, else `$XDG_CONFIG_HOME/orch`, else
  `~/.config/orch`), never `<state>/hosts/<ws>/`. A file in `~/.config` is in the person's backups.
- **Windows:** convenience custody is unsupported in phase 1 (Windows stays in direct mode, D63). It needs a specified
  and tested restrictive DACL implementation; POSIX mode bits alone are not its security contract.
- **The selection.** `<person state>/person/<workspace_id>/custody.json` (0600):
  `{"v":1, "custody":"convenience"|"passphrase", "device":"d_…", "pub":"<b64u>", "gen":int, "transition":null|{…}}`.
  No file means the P1 default: the passphrase key at `<state>/hosts/<ws>/person/dk.key.json` (H5b's location, kept so
  existing keys and `orch sign` keep working). One resolver (`orch.custody.select.device_backend(state, ws)`) is used by
  `orch sign` (reconciling H5b's lookup), the click signer and `orch keys`. `gen` increases at every transition.
- **The host boundary.** Host and HTTP code do not intentionally access person keys; import and audit tests enforce
  this implementation invariant: in host mode the H3a audit hook refuses an open or path event of a path with a
  `person/<ws>/devices/` or `hosts/<ws>/person/` component (the latter is H5b's passphrase device key) or under
  `<state>/person/`, a relative name that is a person-custody file (`d_….key`, `custody.json`, `click.jsonl`,
  `dk.key.json`; a `dir_fd` walk cannot be resolved from the audit event), a working directory inside those places, a
  `ctypes.dlopen` of `Security.framework`, and an import of `orch.click`, `orch.custody.convenience`,
  `orch.custody.keychain`, `orch.custody.select` or `orch.custody.cli`. In tier A this is not an OS security
  boundary. In tier B, permissions separate the host UID from person custody.

**3. Lifecycle** (`orch keys`, terminal CLI only). Creation and custody changes are terminal CLI workflows and are not
exposed through dashboard or host APIs. `ORCH_GRANT` refuses every `orch keys` subcommand but `status`, and the changing
ones (`custody convenience|passphrase`, `forget`) also need the person's terminal (`/dev/tty`, `custody.no_prompt`
otherwise). These checks prevent accidental use through supported agent workflows; they do not authenticate human
presence against a same-UID process. `orch keys` and `orch host convenience` report errors in the CLI's catalog
envelope.

- `orch keys custody convenience --workspace W` converts the existing passphrase key: one passphrase prompt on
  `/dev/tty`, then the destination is written durably, read back, its public key and a test signature are verified,
  the selection is updated recoverably (`transition` records the step), and only then the passphrase file is removed.
  The device id and certificate do not change (custody is per event, F1 O1). It refuses unless the workspace's flag is
  on; the host checks the readers condition at every convenience signature. Creating a convenience key at `orch init`/`join`/pairing is not offered in phase 1:
  a workspace is registered (and its flag set) after `init`.
- `orch keys custody passphrase --workspace W --in-place` moves the same key back under a new passphrase (14+
  characters, the strength check). It warns: "Later signatures will say passphrase, but anyone who copied the key can
  still sign with any label; use --rotate for a clean break." Without `--in-place` it refuses and names `--rotate`.
- Direct-mode human ops (an unregistered workspace, P1's CLI) keep signing with the passphrase key only: a registered
  workspace answers `host.required` to them anyway (D-P2-18), and a converted key is used through the host.
- Interrupted transitions report their state (`orch keys custody status`) and are resumable by running the command
  again. Click signers re-read the selection before every `view` and `sign` and refuse when `gen` changed, so pending
  approvals are invalidated before a transition can be used.
- Moving to passphrase in place restricts future access to the managed copy only. Deletion is best effort and does not
  erase backups, snapshots or exported copies (copy-on-write file systems and the keychain file in Time Machine keep
  old copies). `orch keys forget --workspace W` removes local custody; it is not device revocation.
- **`--rotate`** (designed, not built in H5c: no op adds a device yet). Rotation creates and durably verifies a new key,
  obtains its certificate, registers it, and commits removal of the old device in every affected workspace. It reports
  per-workspace completion and supports retry after interruption. The clean break applies only to workspaces whose
  authoritative history includes the old device's removal; previous signatures and their effects remain. The command
  does not report complete revocation while any workspace remains outstanding.

**4. `auth: "convenience"` and F1** (amended in H5c, F1 §5.3, §11.4, T16, §13).

- `auth` is a signed declaration of custody by the signing implementation, not verifier-proven user presence or device
  attestation. Conforming convenience clients emit `auth: "convenience"`. Possession of an exportable device key permits
  signing another supported `auth` value. Rotation retires that key's authority where revocation is enforced; it does
  not make custody labels independently verifiable. One value covers keychain and file: it names the custody class.
- Every person event records declared custody in `auth`. `convenience` requires no human factor. Other person-capable
  backends follow their documented factor and interaction requirements. The `file` tier is not person-capable.
- The signed bytes and `contract` 1 are unchanged. **Compatibility:** this is a reader-compatibility change despite
  retaining contract 1 and the signing-byte construction. Before a workspace emits convenience events, every supported
  reader and writer must support the new value, or older versions must be explicitly excluded from that workspace.
  Host-local schema support is necessary but does not prove reader compatibility. The readers, all of which must be
  released (and the relay deployed) before the flag is switched on for a workspace that has them:
  - orch-core: `schemas/common.json`, `orch.custody.AUTH_VALUES`, the `Signer` schema in `host/human.py` (H5c);
  - orch-relay: `AUTH_KINDS` in `src/orch_relay/checkpoints.py` and `ref/checkpoints.py`, and its F1 vectors;
  - the dashboard: `server/src/orch_dashboard/adapters/tickets.py` must show `convenience` as "no presence" and never
    map an unknown value to "password"; `devhost/humanops.py`;
  - other members' cores in a shared workspace (excluded by the readers condition in phase 1);
  - later the mobile Core package.
  Rollout includes replay fixtures, refusal behaviour for old readers (an old core refuses the event on schema:
  `auth.invalid_event` on replay), and an explicit supported-version cutover. The per-workspace flag and the readers
  condition of item 1 are how phase 1 meets this.
- **The socket API** (§0.8.1): `Signer.auth` becomes `"passphrase"|"secure-enclave"|"convenience"` (additive,
  changelog item 11).

**5. The click signer process** (`python -I -m orch.click`, core package `orch.click`).

- **One process per dashboard server**, started by the dashboard server as its child under the same UID
  (`orch.click.spawn.start()`), with a **`socketpair`** as its only channel: created with `SOCK_CLOEXEC`, the signer's
  end passed only to the signer (`pass_fds`) and closed in the parent after spawn. Only the designated signer and
  dashboard endpoints retain the socketpair descriptors; all unrelated descriptors are closed and the endpoints are not
  inherited by later children. The signer terminates on channel loss or verified parent-lifetime loss (it records
  `getppid()` at start and exits when it changes). Core-dump and debugger restrictions (`RLIMIT_CORE` 0,
  `PT_DENY_ATTACH`/`PR_SET_DUMPABLE`) are defence in depth, not a same-UID isolation guarantee. Phase 2 note: an addon
  page or any addon-driven path must not be able to make the dashboard call the signer's `prepare`.
- **It alone holds the custody handle**; the key never crosses the socketpair or the workspace socket. It talks to the
  host itself, as the person, with its own pinned `orch.host.client.Connection` per workspace (H5b's client), and reads
  the person's own registry for the header, as `orch sign` does.
- **Starting and stopping (normative).** The dashboard server calls `orch.click.spawn.start(env=None) -> ClickClient`.
  It creates `socket.socketpair(AF_UNIX, SOCK_STREAM)` (both ends close-on-exec), spawns
  `[sys.executable, "-I", "-m", "orch.click", "--fd", "<n>"]` with `pass_fds=(n,)` for the child's end only,
  `close_fds=True`, `stdin`/`stdout` to `/dev/null` and stderr inherited, then closes the child's end in the parent. The
  child inherits the environment (the person's state directory is resolved from `ORCH_STATE_DIR`, `XDG_CONFIG_HOME`
  or `HOME`, as every orch client does) and is ready when it answers `status`. `ClickClient.call(op, args, timeout=…)`
  sends one request and returns its `result` or raises `ClickError(code, message, reason, retry_after_ms)`;
  `ClickClient.close()` closes the parent's end, which makes the child exit (EOF), waits up to 2 s and then kills that
  child's own PID (never anything else). The child also exits when its parent PID changes. One child per dashboard
  server; restart it on exit (the registry is lost: open approvals that were not signed become terminal-only, which is safe; signed ones keep their durable sign record, so a retried `sign` resends the identical events).
- **Packaging and versions (normative).** The dashboard depends on orch-core at **runtime** (`orch-core>=2.0.0.dev0`
  in `[project].dependencies`, from the repo path source while it is a dev version; once published, at least the first
  release with `orch/click/`). No extra is needed: `orch.click.spawn` and `orch.click.client` import only the standard
  library and `orch.host.frames`; the signer needs `cryptography` (a core dependency) and, on macOS, only ctypes.
  `spawn.start()` runs `sys.executable`, so the child uses the dashboard venv's own orch-core; `-I` ignores
  `PYTHONPATH` and the user site, so nothing outside the venv can shadow `orch`. On each workspace connection the
  signer requires `welcome.proto` to hold 1, `welcome.ops` to name `human.prepare`, `human.get`, `human.submit`,
  `human.pending` and `human.cancel` at version 1, and `welcome.registry` to equal its own registry digest
  (`sha256(cj(sorted served_ops()))`); a mismatch refuses that workspace with `host.version_mismatch` ("the signer's
  orch-core differs from the host's") and `status` shows `available:false, problem:"version_mismatch"`. Algorithm
  drift the registry digest does not see still fails closed in the signer's checks (`check_failed`). `status` also
  answers `core` (the orch-core version) and `contract`: **1** (H5c to H11b) or **2** (H11c-2, deposit support: `prepare {deposit}` and the
  `deposit`, `deposit_id`, `grant`, `deposit_reason` answers of `sign`). A contract-1 signer reads `deposit` as an unknown
  argument and answers `invalid.input`, and refuses every `grant` as `terminal_only`; a client reads `contract` to tell
  the two apart. A client written for contract 2 accepts 1 and 2 (1: no click grants) and refuses any other value.
- **Frames (normative)** on the socketpair: §0.2's codec (`orch.host.frames.encode` and `Decoder`: a 4-byte big-endian
  length, then strict JSON, at most 2 MiB; parsing and nesting limits before any validation), one request at a time:
  - request: `{"t":"req", "id":str/[A-Za-z0-9_-]{1,32}/, "op":"prepare"|"view"|"sign"|"cancel"|"status", "args":{…}}`;
  - answer: `{"t":"res", "id":<same>, "ok":true, "result":{…}}` or
    `{"t":"res", "id":<same>, "ok":false, "error":{"code":str, "message":str, "retryable":bool, "reason"?:str,
    "retry_after_ms"?:int}}`. `message` is display text; clients branch on `code` and `reason` only.
  - A malformed frame, an unknown `op` or bad `args` answers `invalid.input` (a framing error closes the channel).

```
prepare {workspace_id:WsId, op:OpName, args:object, idem:Idem, files?:{name: {b64:str}},
         deposit?:bool = false}                       // v1.6: only with op "grant" (§12.4.2)
  → {request_id:ReqHr, expires_at:Time, op:OpName, confirm:"click"|"word"}
view    {workspace_id:WsId, request_id:ReqHr} → {view:View, view_digest:Hash}
sign    {workspace_id:WsId, request_id:ReqHr, view_digest:Hash, word?:str{1..128}}
  → {result:<human.submit's result: {data, key, hints, seq, …}>, duplicate:bool,     // never a secret
     deposit?:"confirmed"|"rejected"|"unknown", deposit_id?:ULID, grant?:str,           // v1.6: deposit grants only
     deposit_reason?:DepositReason}                  // contract 2: only when deposit is "rejected"
DepositReason = "grant.forgotten"|"grant.not_runner"|"grant.verbs"|"grant.expiring"|"rate.limited"|"runner.unavailable"|"other"
               // a static code of this closed set, never host text; any other host refusal is "other"
cancel  {workspace_id:WsId, request_id:ReqHr} → {state:"cancelled"|"submitted"|"expired"}
status  {} → {core:str, contract:1|2, workspaces:[{workspace_id:WsId, prefix:str,
                           custody:"convenience"|"passphrase"|"none", device:Device|null, enabled:bool,
                           available:bool, problem?:str}]}
View = {risk:"Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.",
        op:OpName, workspace:{id:WsId, name:str, prefix:str}, fingerprint:str/[0-9a-f]{12}/,
        lines:[str],                                       // the terminal review of orch sign, line for line, escaped
        checks:[{repo:str, clean:true, attested_by:"worker", at:Time}],   // shown; `at` is not in view_digest
        confirm:{kind:"click"} | {kind:"word", word:str}, expires_at:Time, auth:"convenience", device:Device,
        artifacts:"checked"|"unseen"|null,
        deposit?:true,                 // v1.6: set only for a deposit grant (§0.8.6 item 6); the line below is then in lines
        deposit_line?:"The secret goes to your orch runner on this machine and is never shown"}
view_digest = "sha256:" + hex(sha256(cj(View with every checks[].at removed)))
Idem = str/a_[0-7][0-9A-HJKMNP-TV-Z]{25}/                  // the dashboard's, one per click, reused on retry
```

  **Error codes on the channel.** `invalid.input`; `custody.no_key` (no device key for the workspace on this machine);
  `custody.unavailable` (keychain locked, access denied, an unsupported platform); `sign.convenience_unavailable`
  with `reason` `disabled`, `relay_paired`, `other_member`, `phone_device` or `reader_version` (from the host);
  `sign.convenience_readers` (from the host, on `prepare` of a reader-adding op); `rate.limited` with
  `retry_after_ms` (the event-signature budget); `human.request_state`, `human.request_expired`, `not_found` and other
  host codes passed through; `host.unavailable` (the workspace socket cannot be reached); and
  **`human.click_refused`** with `reason`:

  | `reason` | When |
  |---|---|
  | `terminal_only` | `grant` without `deposit:true` (v1.6: a deposit grant whose `grant.issued` lacks `label:"runner"`, asks for more than 12 h, or is prepared while this machine has no dashboard execution enabled for the workspace), an op outside the allowlist, or a request too large for the View (more than 16 events or 1 MiB): use the terminal |
  | `not_prepared_here` | `view`/`sign`/`cancel` of a request this signer did not prepare |
  | `expired` | the approval's deadline passed |
  | `view_changed` | `view_digest` differs from the stored View, or the host's answer differs from the approval record |
  | `too_soon` | `sign` less than 1 s after the View was built |
  | `word` | the word is missing or wrong |
  | `custody_changed` | the custody selection changed since `view` (`gen`) |
  | `busy` | another `sign` is in flight |
  | `check_failed` | a §0.8.1 signer check refused the host's answer (the message names the check) |
  | `uncertain` | a submit's outcome is unknown and could not be recovered; `human.pending` shows the request |
  | `already_signed` | `sign` names another `view_digest` than the one this request was signed for (the durable sign record, below): a request is signed at most once, also after a restart |
  | `record_unavailable` | the durable sign record of the request cannot be written (nothing is submitted, the signatures are dropped) or exists but cannot be read or validated (fail closed: this signer does not sign that request again; `orch sign` cannot either, the request is bound to `convenience`, so prepare a new one) |

- **`prepare`** is the only way a request becomes click-eligible. The signer forwards it on its **own** host
  connection as `human.prepare {op, args, signer:{device, auth:"convenience"}}` with the dashboard's idem (#402: a
  retry returns the same request), so the host checks the flag and the readers condition there; it validates the
  answer (all §0.8.1 checks, below, and that the host prepared the op it was asked for) and records the request id, workspace and immutable binding in its local registry.
  **`view` and `sign` refuse anything not in that set** (`human.click_refused`). Requests prepared elsewhere (the CLI,
  an agent, an older dashboard session) stay terminal-only: `orch sign CODE` for a hand-off request. `origin.uid` and
  `origin.pid` are host-attested provenance for display and diagnostics, not authorization evidence. This prevents
  accidental adoption of old or foreign requests; it does not protect against a compromised dashboard invoking
  `prepare` itself.
- **Approval records** are keyed by `(workspace_id, request_id)` and contain immutable canonical signing inputs for
  every event, the validated material and binding fields (`op`, `workspace_id`, `log`, `material`, `review_digest`,
  `fingerprint`, `origin`, the signer tuple), the View digest, the custody `gen`, and a local expiry deadline (the
  request's).
- **`view`** = `human.get {request_id, signer}` (the request is already bound to this signer at prepare, so nothing is
  bound here), then `orch.click.view.build(prepared)`, which runs **every signer check of §0.8.1** before rendering (N4
  shape, formats, the "Bound hashes" recomputations, `Review` per event, `review_digest`, `fingerprint`, `before`, the
  op's touches, artifact uploads when the dashboard passed the bytes) and the op rules of item 6, then compares the
  answer byte for byte with the approval record and renders the View: `lines` is exactly the terminal review of
  `orch.host.signer.render` (same escaping, refusing control characters). The approval projection distinguishes
  invariant reviewed content from freshness metadata: worker rechecks must succeed (`clean: true`), a changed
  substantive attestation (another repo set, `clean`, `attested_by`) requires a new review, and a timestamp-only update
  of `checks[].at` does not change the digest. The UI shows the time of the displayed attestation.
- **`sign`**: the request must be in the registry and its approval unexpired (the local deadline is the host's
  `expires_at`, capped at 10 minutes); `view_digest` must equal the stored one;
  at least 1 s must have passed since the signer built that View (a misclick guard, not security); the custody `gen` is
  unchanged; `word` is checked when the View asks for one. The signer then calls `human.get` again, revalidates the host
  answer and compares the full signing inputs and invariant fields byte for byte with the approval record, rebuilds the
  View from that answer and requires its digest to equal the one shown (a changed substantive check or registry name is
  `view_changed`); it signs
  **the stored immutable inputs** (neither the View nor the 12-hex fingerprint substitutes for this comparison).
  **Retries:** before the first submit the signer writes the exact signed event array and a submit idem to the
  **durable sign record** (below) and syncs it. An uncertain submit (a lost answer, a broken connection, a signer crash
  or restart) is retried with those identical events and idem, without fetching a new binding or signing again.
  Completed responses are recorded for duplicate `sign` calls. On `idem.conflict` or any other uncertainty the signer
  reads `human.pending {request_id}` and reports the request's state instead of signing again.
- **Durable sign records (H5c follow-up, normative).** `sign` signs a request at most once per
  `(request_id, view_digest)`, across signer crashes and restarts:
  - **Where.** `<person state>/person/click/<workspace_id>/<request_id>.json`: person-owned directories 0700 and files
    0600, walked descriptor-relative with `O_NOFOLLOW` (`secure_dir`), each file a regular single-link file checked on
    its own descriptor. One window has two links: a crash between linking the record into place and unlinking its
    temporary file. A read that finds exactly that (the second link is the record's own `.<name>.tmp*` sibling with
    the same inode) removes the temporary file and reads again; any other second link fails closed. Under `<state>/person/`, so the host's audit hook refuses it like the rest of person custody.
  - **What.** A completed record is `{"v":1, workspace_id, request_id, view_digest, op, idem, events, expires_at, signed_at, result}`:
    `events` is the exact signed event array (its signatures are what the workspace log shows anyway), `idem` the submit
    idem, `result` null until `human.submit` answered, then its result. **Never** a key, a factor, keychain material
    or a grant secret (`grant` is terminal-only and refused before signing). A **claim** is the same record without
    `events`, `signed_at` and `result` and with `claimed_at`; a record is a claim or a completed record, anything else
    fails closed.
  - **When.** A claim is written and synced **before the crypto signing**: exclusively (a temporary file opened
    `O_CREAT|O_EXCL|O_NOFOLLOW`, written, `fsync`ed, linked into place, then the directory `fsync`ed). If that fails,
    nothing is signed (`record_unavailable`). After signing, and **before the signature leaves the signer** (before
    `human.submit`), the claim is replaced by the completed record (temporary file, `fsync`, rename, directory
    `fsync`); if that fails, nothing is submitted (`record_unavailable`) and a `sign` in the same process retries the
    completion with the identical events, held in memory right after signing. A record already in place (another
    signer process of the same person won the race) wins: a completed one's events are used, a claim refuses. A claim
    with no completed record and no in-memory events (a crash or failure sat between the claim and the completion)
    is `record_unavailable` for that `view_digest` and `already_signed` for another: the request is never signed
    again. Once signing has started the claim is never released; a failure fails that request closed (the person
    prepares a new request). After the commit the record is replaced (temporary file, `fsync`, rename, directory `fsync`) by one that holds the result; failing that
    only costs a resubmit under the same idem.
  - **Every `sign` reads the record first**, before the in-memory registry, and **the in-memory guard stays as
    well**: a request this process signed answers its cached result or resends the identical events and idem from
    memory (and writes the record again, best effort) even when the record was lost. With the record: the same `view_digest` answers the recorded
    result (`duplicate: true`) or resends the recorded events under the recorded idem (also when the registry was lost
    in a restart: no `prepare` or `view` is needed, and nothing is signed); another `view_digest` is
    `human.click_refused`/`already_signed`. A record that exists but is not a private regular file of the person, does
    not parse or does not validate (shape, identifiers, its own request) fails closed with `record_unavailable`.
  - **Retention.** A record is pruned once its request's `expires_at` plus the host's request retention
    (`orch.host.requests.KEEP_SECONDS`, 24 h) has passed (an unreadable file: its modification time plus the 10-minute
    request lifetime plus the retention); pruning runs at signer start and at `prepare` (never on the `sign` path), at most once a
    minute, and never stops a signature; temporary files a crash left behind (`.<request_id>.json.tmp*`) are removed
    after 10 minutes. By then the host has no request to accept a submit for. `cancel` keeps the record: a cancelled
    request is still never signed again.
- **Refusals do not cancel.** A click-path eligibility, availability, size or rate refusal does not cancel the host
  request; it invalidates the local approval. Explicit `cancel` cancels it: `cancel` is serialized with signing and
  invalidates the local approval before further signing can start; it reports already-signed or submitted state
  honestly and cannot retract a produced signature. Validation failures are reported as refusals and are never described
  as safe to bypass.
- **Binding** selects both device and auth. A click request is bound to `{device, auth:"convenience"}` at prepare; it
  has no hand-off code. The terminal path is a separate hand-off request (`human.prepare {handoff:true}`, shown as
  `orch sign XXXX-XXXX`), which `orch sign` binds with the custody the selection names (with a convenience selection,
  `orch sign` binds `convenience` and the readers condition applies).
- **`status`** reads custody metadata (the selection and the flag) for validated registry workspaces without loading a
  private key, prompting or connecting, and distinguishes unavailable custody from absent custody: `problem` is an
  unfinished transition, `not_convenience`, `disabled`, or the last problem this signer met on that workspace
  (`version_mismatch` at a connection, `custody_unavailable` when the keychain was locked or denied access). A problem
  not met yet surfaces at the next `prepare`.
- **Limits.** Limits reduce accidental repetition and abuse through one honest signer instance. They do not prevent
  unauthorized signing or bound a same-UID adversary. Budgets count actual event signatures: 30 per minute and 600 per
  hour **per signer instance** (they reset with the process; several dashboards have several budgets), reserved for the
  whole request before signing, on monotonic time; exact submit retries consume nothing. At most one `sign` in flight,
  16 approval records, a View of at most 1 MiB canonical and 16 events (larger requests: `human.click_refused`, "use the
  terminal").
- **The log.** `<person state>/person/click.jsonl` (0600, rotated at 1 MiB, 4 files, symlink targets refused, rotation
  keeps the mode). The bounded private log distinguishes review generation, signature production, submit attempt,
  confirmed commit, refusal and uncertain outcome. It contains validated identifiers and static error codes only. It is
  diagnostic evidence, not proof of human approval or a tamper-resistant audit trail against the person's UID.
- **Errors** reuse the catalog (`custody.no_key`, `custody.unavailable`, `rate.limited`, `human.request_state`,
  `human.request_expired`, `sign.convenience_unavailable`) plus `human.click_refused` (exit 3, not retryable,
  client-side only: the host never sends it).

**6. Which ops are click-signed.** The signer enforces an explicit operation allowlist and the complete emitted-event
checks. Unknown operations and future additions fail closed (terminal-only). `grant` and any operation returning a
bearer secret are refused before signing, irrespective of the secret field's initial value, **with one exception
(v1.6, D-P2-27, owner decision of 12 Oct 2026):** a `grant` prepared with `deposit:true` is click + word
`<PREFIX>~<WS8>/GRANT` when its prepared `grant.issued` carries `label:"runner"`, `hours` 1..12 (default 8) and
`verbs:"agent"`, and this machine has dashboard execution enabled for the workspace. `check_prepared` admits exactly
that case, and `orch.click.view.build` sets the View's `deposit:true` and `deposit_line` (the constant "The secret goes
to your orch runner on this machine and is never shown") only then, with the person, scope, hours and until in
`lines`. The dashboard requires `args.deposit` and `view.deposit` to agree, else no Sign button. `sign` draws the secret, signs, submits, and only after the commit calls
`grant.deposit {deposit_id}` with `auth.grant` on its own host connection, wipes the secret and answers `deposit` and
`deposit_id`; the secret is never in a result, a frame to the dashboard or a log. On `deposit:"unknown"` it asks
`grant.deposit_status` once (§12.4.2).

| Class | Ops |
|---|---|
| **Click** | `approve`, `request_changes`, `verdict`, `answer`, `close`, `reopen`, `new`, `set`, `section.set`, `ac.add`, `ac.edit`, `task.add`, `task.block`, `task.done`, `task.reopen`, `task.skip`, `task.start`, `log`, `ask`, `claim`, `release`, `handoff`, `submit`, `move`, `artifact.add`, `artifact.replace`, `apply`, `grant.revoke`, `addon.disable`, `settings.set` without `repos` |
| **Click + typed word** | `member.add`, `member.role`, `member.remove` (word: the person id, unique already), `addon.grant`, `addon.purge` (`<name>~<WS8>`: addon names repeat across workspaces), workspace `policy.set` (`<PREFIX>~<WS8>/POLICY`), `settings.set` with `repos` (`<PREFIX>~<WS8>/SETTINGS`: it changes the repo path and remote that D58's source checks read); a ticket-scope op keeps `KEY/FRAG`. `WS8` is the last 8 characters of the workspace id and `PREFIX` the prefix, both from the signer's own registry (orch-52 decision, 12 Oct 2026, shared with #404's terminal signer) |
| **Click + word, deposit only** (v1.6) | `grant` with `deposit:true` and `label:"runner"`, at most 12 h (word `<PREFIX>~<WS8>/GRANT`; the secret goes only to the runner; owner decision of 12 Oct 2026, §12.4.2) |
| **Terminal only** | `grant` without `deposit:true`, and every op not listed above |

Daily operations use click confirmation as a phase-1 usability decision. A later event may supersede their recorded
state but may not reverse external effects. Typed words on member, addon-grant/purge, policy and repo-settings
operations reduce accidental confirmation; they are neither authentication factors nor protection against
script-driven signing (the word is in the View). Framing defences address clickjacking separately. The grant secret
stays off the HTTP path (F1, D-P2-16): a terminal `grant` shows it only on the terminal, and a click deposit grant
(v1.6) hands it from the signer to the runner through `grant.deposit`, never through the dashboard. This protects
against passive disclosure only; a click deposit grant is dashboard-controlled issuance of standing agent authority
(up to 12 h, owner decision of 12 Oct 2026), and nothing here prevents every authority escalation through the allowed
member, policy or addon operations.

**7. What the dashboard does** (dashboard stream; not in this core PR).

- **Flow.** Click on an action → the signer's `prepare` (one idem per click, reused on retry) → `view` → the sign panel
  shows the View → the person clicks **Sign** (or types the word) → `sign` → the result. "Sign in the terminal instead"
  prepares a separate hand-off request.
- **It displays exactly the View** (`lines` as text through `visible.tsx`, never HTML; the risk line; the checks with
  their time). It does not show its own preview, the host's `fingerprint`, its cached ticket view or `ticket.get` hashes
  as "what you sign".
- **Workspace card**: when `status` reports `custody:"convenience"`, the card shows "Convenience signing: agents running
  as you, or anything that takes over this dashboard, could sign." The same line opens every sign panel.
- **HTTP.** Bind only the intended loopback listener and validate the exact Host on every route. Require the bearer on
  every authenticated API and never accept it through cookies or URLs; keep it in tab memory or `sessionStorage`, never
  `localStorage`. Apply exact Origin validation, reject null or missing Origin, enforce Fetch Metadata when present, and
  require bounded JSON bodies on every browser mutation endpoint, including prepare, view, sign, cancel and session
  establishment as applicable. Do not enable CORS. Launch codes are short-lived, single-use and unguessable. Session and
  launch responses use `Cache-Control: no-store`; token-bearing URLs are not used for ongoing sessions. Views and
  credential-bearing responses are not logged. Serve only trusted application code as same-origin executable content.
  Untrusted artifacts are downloads or use an isolated origin. Send `Content-Security-Policy: default-src 'self';
  script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`. No URL or query
  parameter opens a sign panel or prepares a request. Each new View resets confirmation state. Sign is armed after
  1 s of visible presentation (the timer restarts on blur or when `visibilityState` is not `visible`); the signer's own
  1 s dwell since it built the View is the authority (a sign earlier is refused with `too_soon`), and the dashboard arms
  no earlier. Activation begun before presentation is ignored for pointer and keyboard input. This reduces accidental activation;
  it proves neither review nor human presence and does not constrain a compromised client.

**8. Threats.**

| Threat | Answer |
|---|---|
| Same-UID agent | Accepted: can acquire or use convenience signing authority. H9/tier B improve isolation only after migration, applicable UID separation and revocation of old convenience devices. |
| Host | Honest host code never handles person keys. A compromised tier-A host has the accepted same-UID access. A separate-UID host cannot directly read person custody, but its reported origin and attestations remain untrusted. Signer checks establish specified consistency properties, not truth of every host assertion. |
| Another local user | Cannot directly read correctly protected person custody under ordinary OS permissions. Can reach loopback HTTP; signing additionally depends on bearer and session security. A stolen bearer crosses this boundary. |
| Remote web page | Without credentials, same-origin execution or another vulnerability, browser requests are constrained by bearer, Host/Origin validation, CORS and framing controls. A page gaining same-origin script execution can drive signing. |
| HTTP bug or compromise | A passive disclosure of HTTP memory does not directly disclose signer-resident keys. Disclosure of a bearer can disclose signing authority. Active control of the HTTP workflow can obtain unseen signatures through the signer API. Code execution in it can read the key. |
| Stolen bearer | Can request unseen signatures for click-eligible operations while valid, including (v1.6) a deposit-only runner grant of up to 12 h. Typed words do not prevent this. Terminal-only secret issuance remains unavailable through the click API. |
| Agent-prepared request | Signer-local preparation registration excludes foreign or historical pending requests from click eligibility. Host-reported PID equality alone does not. |
| Host lies in material | Signer recomputation rejects mismatches between supplied material and signing inputs. It does not prove the truth of mutually consistent fabricated input or fields explicitly classified as host attestations. |
| View swap | Full signing-input binding rejects changes to an approved record. It does not detect the dashboard displaying one record while requesting a signature for another. |
| Backup or sync | File custody may be backed up. Traditional login-keychain custody may also be backed up. Data-protection non-migration claims apply only to a correctly implemented and tested data-protection backend. |
| A reader that does not know `convenience` | The per-workspace flag, the readers condition (`sign.convenience_unavailable`) and the block on adding readers (`sign.convenience_readers`). |
| A same-UID process pre-claims a click idem | `idem.conflict`; the signer reports the request state and signs nothing again. A denial of service by an already accepted attacker. |
| Signer crash or restart mid-sign; a dashboard retry or a second View after it | The durable sign record is synced before the signature leaves: a retry resends the identical events and idem, another View is `already_signed`. A same-UID process can delete or corrupt the record (accepted: it can sign anyway); corruption fails closed. A record can also be lost without an attacker (a cleanup of the state directory, a backup restore, a wall-clock jump of more than 24 h before a prune, file-system loss): the live signer's in-memory guard still never signs again; only after the signer has also restarted can a lost record allow one fresh signature, and the host still commits a request once. |

**8. One signing path per workspace (owner decision of 12 Oct 2026, with the dashboard stream's clarification).** While
click-to-sign is on for a workspace, the only way to sign is the click. The terminal hand-off (a request with a code,
`human.prepare {handoff:true}`, signed with `orch sign CODE`) does not exist for that workspace:

- `human.prepare` with `handoff:true` is refused with **`sign.click_only`** (exit 3, not retryable). The message says to
  use Sign in the dashboard, or turn convenience off with `orch host convenience off --workspace W` (the `fix`).
- `orch host convenience on` is **refused** (`sign.click_only`, the message lists each pending request's id, op and code)
  while hand-off requests of that workspace are pending (`prepared` or `bound`, not expired). The owner lets them expire (10 minutes), then switches on. Refusing was chosen over cancelling them: nothing is
  cancelled silently. A request file that cannot be read refuses too, naming its id. Requests prepared with a signer (not a hand-off) never block it.
- A hand-off request that exists anyway (the flag was switched on by another path, e.g. editing the state file) cannot be
  bound (`human.get` with `signer`) or submitted (`human.submit`, again under the store lock): `sign.click_only`.
- **The race.** The host holds an exclusive `flock` on `<state>/host/convenience-<ws>.lock` from the flag check of a
  hand-off prepare to the write of its request file; `orch host convenience on|off` holds the same lock across its pending
  scan and the flag write. A lock is waited for at most 2 s (`lock.busy`, exit 9, retryable), and its file must be a regular 0600 file of the host UID, opened without following a link (`custody.unsafe_mode`). `set_enabled` also takes `convenience.lock`, so concurrent flag writes for different workspaces lose nothing. `human.submit` of a hand-off request holds the same lock (inside the store lock) from its flag check to the write of its journal record, so `on` cannot interleave with a submit whatever the timing; the CLI never takes the store lock, so there is no lock cycle. A bound request also counts as pending until a minute past its expiry, as defence in depth. A request record with no valid `expires` refuses `on` like an unreadable file (`custody.unsafe_mode`, naming its id). So exactly one wins: the prepare lands first (and `on` is refused, listing it), or the flag
  lands first (and the prepare is refused).
- `orch host convenience off` removes the flag and hand-off works again: the owner's CLI fallback.
- Direct mode has no host and no convenience flag, and is unaffected.

**9. Reader-version handshake (H12d, design draft).**

**Status: draft revised after Codex challenge and Opus tie-break (13 Oct 2026); for orch-52's decision.** Nothing here
is built; items 1 to 8 hold unchanged until H12d merges. **Owner decision of 13 Oct 2026:** a reader (a phone, a relay
link, another member's core) that declares **and** proves that it understands `auth: "convenience"` may be added to a
workspace with convenience-signed history; older readers stay refused. Phase 2, together with the relay and phone
integration (R6, orch-f1 pairing). Inputs: orch-f1 (orch-mobile `aa13ca6`), orch-29 (relay protocol v2 §6.1, §6.3,
§8.3, §8.6, §19; P3-D6, P3-D16), the Codex challenge of the first draft (`affe5257`) and both streams' objections to it
(9.14).

**As built in H12d-1** (core identity and core-owned vectors): the label (9.11), `verify_decl` (9.3) and the KAT
runner (9.2) with `reader_kat_1` and the `checkpoint.json` convenience cases (9.12). Nothing is wired into an op yet;
records, `check_readers`, `member.add --reader-decl` and `reader.declare` are H12d-2. Notes per sub-item below.

**9.0 What this can and cannot do.**
- **A malicious reader can always lie.** It can hard-code any answer and misparse anyway. A person's compromised device
  is already within that person's authority (11A.9). Nothing here defends against it.
- **An old program cannot obey a new rule.** The design never relies on one. The honest old reader fails closed through
  its **existing** behaviour or through the host, never through a new marker it would have to honour:
  - an old orch-core refuses a convenience event on replay (`auth.invalid_event`, F1 row 153);
  - every shipped phone build parses events and views with exact field sets (orch-f1): an event with `auth` or a view
    with `signer` is refused as malformed;
  - the host serves a phone only against a current declaration (9.5), which an old build cannot produce.
  The handshake adds what is missing today: **admission** of readers that do understand the value, and an
  **explanation** instead of an unexplained parse failure.
- **What remains:** a lenient reader (it maps an unknown `auth` to a known one, the dashboard bug class of item 4) that
  is admitted once and never re-checked. The known-answer test (9.2) catches that build at its declaration.
- **The host cannot gate what a reader already holds**, nor what the relay serves to member devices on its own (9.6).
- **A rollback of the host's state directory** is outside the guarantee, as for exactly-once (11A.4, "Scope"); serving
  stays bound to the reader's current declaration (9.5).

**9.1 Where the declaration lives: in what the reader itself sends, verified by the appending writer, kept verbatim.**

| Option | For | Against | Verdict |
|---|---|---|---|
| (a) `reader` in the device certificate (orch-f1) | enforceable from the cert alone; every core sees it | the cert is relay protocol §6.1 with an exact field set, verified by the relay and parsed by every core and phone in `member.added`/`device.added`: **every old reader breaks as soon as any new device is certified**, in every workspace; the person key would vouch for a software property it cannot see; a second breaking cert change soon after the PoP one (11A.12) | **not in phase 2**; fold into the PoP certificate change (Q3) |
| (b) a field in the relay-visible member-list entry | every device sees it | relay schema change and protocol-v2 bump; the relay is blind; the list's scopes are frozen at the join anchor (orch-v2 D6 step 5) | rejected |
| (c) a device-signed claim in what the reader sends, verified by the host (orch-29) | no relay change; bound to the attempt | host-local: lost with host state, invisible to other cores | **adopted, extended** (below) |
| a new F1 event or an extra field in `device.added`/`member.added` | in the signed log | F1 objects are exact-field-set: an old reader in a workspace **without** convenience breaks on the first declaration (`event.unknown_type`, `chain.broken`): a global format cutover for a convenience-only rule | rejected |

**Decision: (c), one carrier per reader, the signed object kept verbatim.**

| Reader | Carrier of `reader_decl` |
|---|---|
| a phone, pairing (`via:"pair"`) | the **pair-request meta** (protocol v2 §8.3: phone → host, sealed under `K_pair`, signed by `dk_sig`, exact field set, checked by the host in §8.3 step 3) gains `reader_decl`. **Not** `CertRequestInner`: that is the host's request sealed to the primary, and the phone never writes it. The primary needs nothing: the host enforces, and `CertRequestInner` stays unchanged |
| a member's core (`member.add`, host and direct mode) | the invitee's orch writes `reader_decl` next to its device certificate; `member.add` gains the argument `reader_decl` (`--reader-decl PATH`). It is an argument, not material: material is generated output whose key set the signer checks exactly (`host/signer.py` `_hashes`) |
| another device of a member (`device.added`) | no op emits `device.added` today (`READER_OPS` is `{member.add}`). R6's pairing is the only planned path. Any later path that emits it carries a declaration in its own input, signed by the new device's key |
| a reader already admitted (an update, a withdrawal, a lost record) | `reader.declare` (9.11), from the device itself |
| the relay software (it parses restore evidence) | `GET /v2/relay-info` (9.8), an operator statement, not a signed declaration |

- **The appending writer enforces it** (a relay check is untrustworthy, and the relay is blind). It verifies the
  signature against the key that the same request carries or certifies, then the binding and the known-answer digests
  (9.2, 9.3), at the gate (9.4).
- **Kept verbatim** in a reader record (9.10). The record can be checked by anyone who holds the log: the key is in the
  device certificate in the signed log, and the declaration names the workspace, device, keys and person. It is
  **verifiable against the signed log, not carried in it.**
- **Members' own cores** are readers. Each declares at its admission (`member.add`, later device paths) and is gated
  like a phone. In phase 2 a workspace has **one appending writer**: the host (ownership lock; with a relay, the fenced
  holder, 11A.4) or, without a host, the direct CLI under the store lock. Another member's core never appends roster
  events: its writes reach the host as device-signed events (`relay.submit`), whose closed table has no roster event.
  It needs reader records only when it becomes the host; `register` imports and verifies them (9.10). **P8
  constraint:** a design with a second appender must replicate the records and enforce 9.4 at each appender.

**9.2 What "proves" means: signed runtime self-test evidence, per parsing surface; no interactive challenge.**
- **The known-answer test (KAT).** The goal is narrow and achievable: an honest reader whose build is old or buggy must
  not be admitted and then misparse convenience events. The core-owned vector set gains `reader_kat_1`, with one
  variant per **surface**, the code path a reader really uses:
  - `log`: three complete workspace-log person events of a vector workspace that differ only in `auth`
    (`convenience`, `passphrase`, `bogus`). **Each is independently valid**: re-signed by the vector device and
    replayable on its own fixture state with a pinned verification context. So `bogus` fails only on `auth`, never on
    its signature. The reader runs its production event parser and verifier and records per event
    `{"head":<head(e)>, "accepted":bool, "auth":<value>|null}`.
  - `view` (orch-f1 O1): for each view in `READER_VIEWS_1` (9.11), three view objects that differ only in the
    signer's `auth` (for `ticket.get.questions.answer`: a Question whose `answer.signer.auth` differs). The reader runs
    its production view parser and records `{"view":<id>, "accepted", "auth"}`.
  - Expected results: accepted, accepted, refused with `auth: null`. The `bogus` case pins the second half of level 1:
    **an unknown `auth` value is refused, never mapped to a known one.**
  - The digest is `"sha256:" + hex(sha256(cj(results)))`, results in corpus order. The construction is normative in
    F1 §11.5 (core-owned) and pinned by vectors.
- **Which surfaces:** a reader declares a digest for every surface it parses. The host requires `log` from a core
  (`member.add`) and `view` from a phone (pairing). Q8 answered by orch-f1 (13 Oct 2026): in phase 2 the phone verifies
  no F1 events itself, so `view` suffices. **A reader runs the KAT variant of every code path that renders or verifies
  `auth`; a build that adds F1 parsing must also pass `log` before it declares a level.**
- **What it is:** signed evidence that the declaring build's production parser classified these cases, made at
  runtime. It is **not** proof of correct display, of ticket-event handling or of restore handling, and it is **not**
  independently verifiable proof of execution (a liar can copy the constant). Display and restore handling are
  client conformance tests (9.12). A reader MUST NOT declare a level whose KAT fails at runtime; it declares the
  highest level that passes, or level 0.
- **As built in H12d-1:** `tests/vectors/f1/reader_kat_1.json`, written by the independent oracle
  (`tests/canon/oracle_f1_signed.py` `reader_kat_vector`, regenerated byte for byte by `tests/canon/test_f1_vectors.py`).
  `log`: a fixture (the vector workspace's genesis and `member.added` of mara) and three `settings.changed` events of
  the owner at seq 3 that differ only in `auth` (and so in `sig` and `host_sig`), each re-signed by device `sev1` and
  the vector WSK; context `{workspace_id, genesis, log:"workspace", now}`. `view`: `READER_VIEWS_1 =
  ("ticket.get.questions.answer",)` (owner decision with orch-f1, #463 round 1), three `ticket.get` Questions in state
  `answered` that differ only in `answer.signer.auth` (no `answer.auth` in them). `orch.identity.reader.run_kat(corpus, surface)` runs core's production parsers:
  `orch.store.logs.parse_line` (the line parser of every log read) plus `orch.model.replay` with `CryptoVerifier` for
  `log`, `parse_signer` for `view`; it reproduces `expected` (pinned as `KAT_DIGESTS`). Level-1 digests: `log`
  `sha256:70b450df574b6c372005b7504a7f9efe80552cb2841193b960fe538c6aa6fc0e`, `view`
  `sha256:d76e4593ec191e91ca2f6bddef570cbfa1af14dae702bb5ded0572eebe1d9085`. The view digest depends only on the view
  ids and the outcomes, not on the entry shapes, so H12d-2 may still settle the Question's exact fields. **A later
  phone build that renders a signer in another view needs `READER_VIEWS_2`** (a new corpus and a new level), never an
  edit of `READER_VIEWS_1`. Shipping the corpus as package data for a runtime self-test (`orch keys reader declare`) is
  H12d-2.
- **An interactive challenge** (a fresh convenience event sent to the reader, which returns its classification) proves
  nothing more against a liar (it can echo the `auth` field) and little more against an honest reader. It would add a
  round trip to a pairing that today is one sealed pair request. **Not adopted in phase 2** (Q1).

**9.3 The object and its rules.**

```
reader_decl = {"o": {"v": 1, "kind": "reader_decl", "suite": 2, "workspace_id": WsId,
                     "device_id": <derived from dk_sig_pub>, "dk_hash": <dk_hash of both device keys>,
                     "who": {"person_id": PersonId} | {"pk_pin": str},  // pk_pin: a phone pairing without a cert
                     "reader": uint,                                     // 0..2^31-1; 0 withdraws
                     "kat": {"1": {"log"?: "sha256:…", "view"?: "sha256:…"}, …},  // levels 1..min(reader, known)
                     "bind": {"via": "pair", "offer_id": str, "attempt": hex32}
                           | {"via": "enrol"} | {"via": "declare"},
                     "created_ms": int},
               "sig": b64u(Sign(dk_sig, "orch/v2/sig/reader-decl|" || cj(o)))}
```

- **Levels.** `reader` is an integer; **an absent declaration means 0, an old reader.** Level 1 = understands
  `auth:"convenience"` (F1 §11.4 with the H5c amendment) and refuses unknown `auth` values. A higher level implies every
  lower one. A **higher unknown level is accepted** as at least the host's highest known level: the host checks
  `kat[k]` for every `k` from 1 to `min(reader, host_max)`, for the surfaces it requires, and ignores higher keys.
  **`reader: 0` is valid**: an H12d-aware build of a lower level, or a withdrawal. A missing or wrong `kat[k]` is
  `reader.decl_invalid`.
- **Bound to keys, device, person and workspace (Codex 2).** `device_id` derives from the request's `dk_sig_pub`, and
  `dk_hash` is that of the request's keys (for `member.add` the `device_cert`'s, for a pairing the pair meta's, for
  `reader.declare` the requesting device's certificate). `workspace_id` is the workspace of the request (for a pairing,
  the offer stream's, which the pair meta does not name, orch-29 #2). `who` names the person the device is certified
  for: `person_id` for `member.add` (`person_ref(pk)`), for `reader.declare` (the request's verified person) and for a
  phone that presents a certificate (its `person_id`). A phone pairing **without** a certificate does not know the
  person key yet, only the QR's `pk_pin`. It signs `pk_pin`, and the host checks in the R6 allocation that the
  certificate it got is under the key that pin names. F1 lets any person certify any public key (rows 151, 152), so a
  record counts only for its exact pair `(device_id, person_id)`, with `dk_hash` equal to that certificate's.
- **Attempt binding (pairing, orch-29 #2).** `bind.offer_id` and `bind.attempt` must equal the pair meta's
  `offer_id` and `attempt`. With `dk_hash`, the declaration cannot be lifted into another offer or attempt.
- **Freshness is checked once, at admission (Codex 8, orch-f1 O4):**
  - `via:"pair"`: no clock window. The host-issued, single-use, 10-minute offer and the attempt bound it, and a
    pairing can wait 10 minutes in `cert_pending`.
  - `via:"enrol"`: `created_ms` at most 300 s ahead of the host clock and at most 30 days old (the invitation travels
    out of band).
  - `via:"declare"`: the request's own ±300 s window (11A.8), and `created_ms` inside it.
  - A refusal states the host's time.
  - After admission the declaration belongs to the prepared request (bound into `review_digest`). At each `human.get`
    binding and at `human.submit`, the host rechecks the signature, the binding and the level against the current
    `required_reader`, never the clock. Validating a declaration never consumes it.
- **No attempt ledger.** A replayed declaration is the same statement by the same key. Under "latest wins" it loses to
  anything newer, and serving needs the current one (9.5).
- **Latest wins**, by `created_ms` and then by arrival; a lower declaration lowers the level (the fail-safe
  direction). A `created_ms` more than 300 s in the future is refused, so a device cannot pin its own future.
- **Revoked with the device:** a record counts only while its exact pair is a current, valid device in the F1 state
  (not removed, not revoked, no held revocation for that pair in `HostPins`).
- **Renewal and rotation (orch-29 #6, Codex 9).** F1 has no same-id renewal: `device.added` of an existing id is
  `DEVICE_EXISTS` (`model/workspace.py`). New keys mean a new device id, admitted by its own gated path. A relay
  certificate renewed with the **same** keys keeps the record, which is keyed by those keys, and the software did not
  change. An epoch rotation changes `WK_e`, not the reader. **A software change triggers a new declaration, not a
  renewal or a rotation:** the first launch of a new phone build (O5), or an orch upgrade on a core (`orch keys reader
  declare`, run by the upgrade). If F1 ever gains a same-id renewal, it MUST carry a fresh declaration.

- **As built in H12d-1** (`orch.identity.reader.verify_decl`, pure; the strictest reading where this text leaves room):
  refusal order and reasons `shape`, `signature`, `workspace`, `device`, `keys`, `who`, `bind`, `time`, `kat_missing`,
  `kat_mismatch` (the relay reference's names, plus core's `time`); the 2 KiB cap is on `cj` of the whole signed object;
  ids are the protocol's bare hex (no `p_`/`d_`); a `kat` level key is `[1-9][0-9]*` and at most `reader`; a
  `created_ms` more than 300 s ahead is refused for every `via`, `enrol` also beyond 30 days old, `declare` outside
  ±300 s; a recheck reads no clock and must say so: `now_ms=None` **with** `recheck=True` (either alone is a
  `ValueError`, #463 review L2); the expectation defaults to core's own `KAT_DIGESTS`; a bad context (not the
  declaration) is a `ValueError`. Checked locally against all 28 relay `reader_decl` cases of orch-relay develop
  (`446d247`); the vendored-corpus test runs once the rev-12 corpus is synced (after #442).
- **Custody (H12d-1, #463 review L1):** `sig_reader_decl` is in `DEVICE_KEY_LABELS`, but no backend signs it yet. The
  passphrase prompt refuses it (no review rendering: "not prompted for in P1"), and the `convenience` backend, which
  signs without any review, refuses it explicitly with the same message before reading its key. H12d-2 decides
  whether and how a declaration is signed (`orch keys reader declare`, the phone).
- **Notes for H12d-2 (#463 Opus review):**
  - **I1:** ECDSA signatures are malleable (high-s is accepted, `crypto.verify`), so the same declaration can arrive
    with two `sig` values and two `rd`. Reader records deduplicate and compare declarations on `sha256(cj(o))`, never
    on `rd`; `rd` stays the serving binding of the exact bytes the device holds.
  - **I2:** where a declaration arrives as bytes (`--reader-decl PATH`, the pair meta), parse it with `loads_strict`
    behind a byte cap checked **before** parsing (2 KiB plus a small margin), not after.
  - **I4:** `labels.json` gained a top-level `pins` key with this change (its `INDEX` entry is no longer
    `[shared, unchanged]`); consumers that read the file must take `labels` and ignore other keys.

**9.4 Gate semantics: one invariant, enforced at the append choke point.**

`required_reader(ws)` is 1 while the per-workspace flag is on, the merged history holds an `auth:"convenience"` event,
or the durable mark `convenience_seen` is set (9.7). Otherwise it is 0. It is computed, never trusted from a client.

**Invariant I** (under the store lock, after every append): if `required_reader(ws)` is at least 1, every current
reader has at least that level. The current readers are:
- every current valid device pair, except the signing device of a convenience check, which this core itself reads
  for and which is qualified by `reader_version` (item 1: this host's model). A device without a record counts as 0;
- the relay, linked or retained (11A.6), at its last verified level.

**One check, one place (Codex 1, 4, 9).** `orch.ops.readers.check_readers(store, levels, events)` runs inside the
append transaction of every path that can break I:
- events that add or revive a reader: `member.added`, `device.added`, and a `restore` (cutting a `device.removed` or
  `member.removed` revives a reader);
- an event with `auth:"convenience"`.

The two append paths are the host's `append_signed` (11A.4: shared by `human.finish`, `relay.submit` and the R6
allocation) and direct mode's `Human.run`. A table test lists every append path (as `test_column_r_sites`), and a new
path fails the test until it is listed. The checks below are early refusals with a good message; the append check is
authoritative and recomputes the level and `required_reader`. Whichever of a convenience event and a reader event
appends first, the other refuses.

| Reader added | Declaration | Early check | Authoritative |
|---|---|---|---|
| a member (`member.add`) | `args.reader_decl` | host: at prepare, at every binding; direct: in the handler | the append (both modes) |
| a phone (`relay.pair`: the `approved` answer and any member list that adds a device) | the pair meta's `reader_decl`, or, beside a presented certificate, the same pair declaration (`via:"pair"`); a `via:"declare"` declaration is **not** accepted there (narrowing below) | when the host opens the pair request: before the fingerprint and before the primary is asked | the R6 allocation's append, under the store lock |
| another device (`device.added`, any later path) | that path's input | the path's prepare | the append |
| the relay (`relay.link`) | `GET /v2/relay-info` (9.8) | link step 1 and every R1 login | the critical section that writes `link.json` |
| a records push to a non-private remote | none is possible | — | the export check (9.7): refused while `required_reader` is 1, except D-P2-34's private remote |

- **Narrowing for pairing (C7, DECIDED 15 Oct 2026, orch-52 with orch-29).** At pairing, a `via:"declare"` declaration
  is **not** accepted beside a presented certificate (path 7a), and not on path 7b either. Only the pair declaration,
  `via:"pair"` (bound to the offer and the `attempt`, `who` the link's `pk_pin` or the presented cert's person), counts
  there, matching 11A.15.6 step 4 and protocol §8.3. `via:"declare"` stays what it is for `member.add` and the other
  paths listed above. The first stored declaration wins on a resend.
- **The rule.** A reader-adding op is refused with `sign.convenience_readers` when the new reader's level is below
  `required_reader(ws)`. Old readers stay refused. Qualifying readers are admitted **with** convenience history and
  **while the flag is on**; this replaces item 1's unconditional refusal in those states.
- **Direct mode (Codex 1).** It has no flag, so there `required_reader` comes from the history and the mark.
  `member.add --reader-decl PATH` is verified by the same `orch.identity.reader.verify_decl`, and the record goes to
  `<workspace>/.state/readers/`. Direct mode creates no convenience events (no click signer). `register` imports the
  direct records after verifying each against the log; `unregister` exports the host's. A record that does not verify
  is dropped and its device counts as 0.

**9.5 Serving is bound to the reader's current declaration (Codex 3, 4; orch-f1 O3, O5).**
- **The phone sends it on every request.** Every column-R request's sealed meta carries `rd`, the digest
  `sha256(cj(reader_decl))` of the device's current declaration. A pre-H12d build cannot send it. This is a
  protocol-v2 §8.6 field: sealed and relay-blind, added by orch-29 to the protocol text and the vectors.
- **The host checks it while `required_reader` is at least 1.** It serves a request only when `rd` names the device's
  current record and that record's level is at least the required one. Otherwise it refuses with
  `sign.convenience_readers`:
  - `no_declaration`: `rd` is absent, a pre-H12d build;
  - `reader_too_old`: the record is below the required level (the refusal carries `required` and `declared`);
  - `declaration_unknown`: `rd` is not the current record (host state lost or rolled back, or an older H12d-aware
    build).
  The phone answers `declaration_unknown` by re-declaring and retrying.
- **`reader.declare` is exempt:** it is how a device gets an `rd`. Column R serves it while every other op is refused.
  It needs no person-class authority: its kind is `device_signed`, and the declaration's device and person must be the
  request's verified identity (O5).
- **What this binds (Codex 3).** The binding is to what the **running build** states now, not to what the host
  remembers.
  - An older build installed over a newer one is caught. A pre-H12d build sends no `rd`. An H12d-aware lower build
    sends its own lower declaration.
  - A restored, older record cannot serve a build that no longer matches it.
  - On the phone, orch-f1 persists the highest level it declared per workspace **with the device key**, in the same
    Keychain/Secure Enclave realm (O3). A reinstall drops both; an install-over downgrade is detected, and the phone
    refuses to open that workspace's local data ("Update orch to open this workspace").
  - The phone re-declares on the first launch of a new build (O5).
- **Offline caches and app-data backups** are outside host control. A pre-H12d build refuses cached objects that carry
  `auth` or `signer` (exact parsers), and an H12d-aware build applies O3. That is the whole defence, stated as such.
- P-class callers (the owner's CLI and dashboard on the host) are the host's own core and are not gated by `rd`.

**9.6 What the host cannot gate (Codex 5).** The relay serves member devices on its own:
- the card and the member list;
- checkpoints (heads only);
- restore publications, whose evidence holds complete signed events (F1 §5.10);
- the 11A.4 recovery replay of stored answer chunks.

The host does not see these reads. Column R is the host's surface, not the whole read surface.
- **A device whose record was lost** but whose build is level 1 reads correctly: no harm.
- **A downgraded build** fails closed on its own parsers: pre-H12d builds refuse events with `auth` (orch-f1), and
  H12d-aware builds apply O3.
- **Replayed answer chunks** are the exact bytes already answered to the same device while it was served: no new
  disclosure.
- **The only way to stop relay delivery** to a device is to remove it from the member list (device removal, then the
  epoch rotation). The fix text for a device below level 1 says so.
- **The relay's level covers only the relay software** (`AUTH_KINDS`). It never stands in for its devices' levels.

**9.7 Records push: a durable mark (Codex 6; a pre-existing gap).** `records.export_check` (`host/records.py`
`convenience_on`) asks for the flag or the merged history today. Record paths include `events/workspace.jsonl` and
`tickets/*/events.jsonl` (`records.RECORD_PATH`). An owner repair (`abandon_tail`, then `restore`) can remove the last
convenience event from the merged history while the records history still carries it. A push of that commit, or of a
descendant, then exports convenience events to unknown readers. The fix has two parts:
1. The host keeps a monotonic mark, `<state>/hosts/<ws>/convenience_seen.json`. It is written in the append
   transaction of the first convenience event, and whenever a scan finds one. A restore never clears it.
2. The records tool already walks the pushed history (`validate_history`). It also reports whether any blob reachable
   from the pushed commit carries `"auth":"convenience"`. The export check refuses on the mark or the report, so part
   2 still holds after the host's state is lost.

This is independent of H12d and should land first, as its own fix (Q10). The same rule covers publications the relay
retains: 11A.6's permanent reservation already counts them. **Built in #441** (§0.8.6 item 1, §12.8 "As built"): the
mark is also written in direct mode's append and by `abandon_tail`; the tool's report is `history_convenience` on
`records.export_check` and, unlike part 1's "whenever a scan finds one", it never writes the mark (only the host's own
scans do; #454 review); under H12d, `required_reader` reads the same mark.

**9.8 The relay (orch-29 #3, #4; Codex 11).**
- **`GET /v2/relay-info`** → `{"v":1, "reader":N}`, optionally with `max_publication_bytes`.
  - `N` is a code constant; an absent route (404) means 0.
  - The route is throttled like the other unauthenticated GETs and carries no workspace data.
  - It is an **honest-operator statement**, authenticated only by TLS to the stored origin, **never a security
    control**. A hostile relay can lie. That is out of scope (11A.9): the relay parses only restore evidence, so its
    worst case is a refused restore publication, which R3 alarms.
  - The weaker assurance (no runtime KAT) is accepted explicitly (Q6).
- **What `N = 1` means.** The relay's `AUTH_KINDS` holds `convenience` for checkpoints, refs and restore evidence, and
  relay CI runs the core-owned convenience cases of the checkpoint vectors.
- **Rollout order:**
  1. core adds the convenience cases to the F1 vectors (`checkpoint.json`);
  2. the relay vendors them and deploys with `N = 1`;
  3. only then may a relay-linked workspace switch the flag on. The relaxed `relay_paired` requires a last level of at
     least 1, and so does a link while `required_reader` is 1.

  Otherwise a restore publication fails permanently.
- **When it is checked.** At link step 1 and at every R1 login: a reconnect or a new deployment shows at the next
  login. An unlinked relay keeps its last level, because the host sends it nothing more and so nothing new is parsed.
- **When the level drops.** A relay whose level falls below `required_reader` raises the alarm `relay_reader_old`, and
  R3 holds every restore whose evidence carries a convenience event. **Publications are one ordered chain (11A.5): a
  held restore blocks every later publication, plain checkpoints included, until the relay is updated or the restore
  is resolved.** (The first draft wrongly said plain checkpoints keep flowing.)

**9.9 Interaction with the readers condition (item 1).** **Decided: relax `phone_device`, `other_member` and
`relay_paired` to count only readers below level 1** (`other_member` confirmed by the owner on 13 Oct 2026, Q2: allowed
only when every other member's devices **and** core have declared level 1; older setups still block):

| Reason | Today | With H12d |
|---|---|---|
| `relay_paired` | any relay link, ever (the permanent reservation, 11A.6) | the relay's last verified level is below 1, or was never checked; an unlinked relay counts with its last level |
| `other_member` | the member list holds another person | another member's core (its `member.add` declaration) or any of its current valid devices is below level 1 (no record counts as 0) |
| `phone_device` | the signer's person has any other valid device | the signer's person has another current valid device below level 1 |
| `disabled`, `reader_version` | unchanged | unchanged; `reader_version` qualifies the signing device itself |

The answer gains `devices: [device id]`, the readers below level 1, so the fix can name them.

**Grandfathering** (orch-f1's open question). Today no reader can be paired while convenience could be used, so no
grandfathered reader exists. Once relaxed, a device paired at level 0 while `required_reader` was 0 is **neither cut
off nor grandfathered into convenience**:
- it keeps working;
- while it is a current reader, the flag can still be switched on, but every convenience signature is refused with the
  relaxed reason naming it, so no convenience event can enter;
- it qualifies by `reader.declare` after an update, or the owner removes it;
- `orch host convenience on` stays allowed and prints the readers below level 1.

**9.10 State.**
- `<state>/hosts/<ws>/readers/<device_id>.json` (0600, host UID, the D-P2-17 custody list) holds `{"v":1,
  "device_id", "person_id", "dk_hash", "reader", "digest", "decl":<reader_decl verbatim>, "source":
  "pair"|"member.add"|"device.added"|"declare", "at"}`. It is never a secret.
- **Writes.** For `member.add`, `device.added` and a pairing, the record is written in the append transaction (the
  D-P2-3 hook, next to the outbox entry; for a pairing, the R6 allocation). For `reader.declare`, it is written in that
  op's own job under the store lock.
- **`reader.declare` (Codex 9).**
  - It is idempotent on `digest`: the same declaration again gives the same answer.
  - A crash before the write leaves nothing recorded, and the device resends.
  - It is authorized by column R (`device_signed`, 9.5), or for the person's own local devices by person class.
- **Host-state loss fails closed.**
  - With no records, every device counts as 0: convenience signing is refused, and phones get `declaration_unknown`
    and re-declare on their own.
  - Cores re-declare with `orch keys reader declare`, and a `needs` item "re-declare readers" lists the devices.
  - Adding a reader still works, because the gate checks the **new** reader's own declaration.
- **A known state restore.** The 11A.4 operator procedure gains `orch host readers reset --workspace W`, which drops
  the records (fail closed).
- **An undetected rollback** can bring back an older valid record, which may be optimistic for the readers condition.
  This is the accepted residual of 11A.4; serving stays bound by `rd`.

**9.11 Ops, codes, compatibility, contract v1.8.**
- **`member.add`** takes `args.reader_decl` (an object; `--reader-decl PATH`).
  - It is optional while `required_reader` is 0 and required while it is 1, in both modes.
  - No material key is added. The review gains the line `Reader: understands convenience signing (level N)` or
    `Reader: old (level 0)`, bound into `review_digest`.
  - A signer of an older version cannot reproduce the review and refuses: it fails closed, and the fix is to update
    orch.
- **`reader.declare {reader_decl}`** (new, v1): a write of host state only, with no event.
  - In column R (the closed table of 11A.4, kind `device_signed`), the declaration's device and person must be the
    requesting identity.
  - In person class (P), it covers the person's own local devices (`orch keys reader declare`).
  - It answers `{device, reader, digest}`.
- **`reader.list {}`** (new, read, P and R) answers `[{device, person, reader, digest, decl}]` for current devices.
  `member.list` and `workspace.show` gain `reader` per device and `required_reader`.
- **Views (orch-f1 O1, O2).** The views that attribute a decision or a history entry gain `signer: {device, auth}`
  (`READER_VIEWS_1`): the items of `activity.list`, the history and gate-decision entries of `ticket.get`, and the
  `needs` items that show a past decision. **Decided (owner with orch-f1, #463 round 1, as built in H12d-1):
  `READER_VIEWS_1 = ("ticket.get.questions.answer",)`**, the signer on a question's answer in `ticket.get`
  (`questions[].answer.signer`), the only place the phone shows a signer; H12d-2 gives that answer `signer: {device,
  auth}`. Other views may still gain `signer`, but a phone that renders it there needs `READER_VIEWS_2`. The view
  variant of the KAT covers each entry. These fields MUST ship before the first phone release. After one, adding a view field to an
  exact-field parser needs a new op version (§0.4).
- **Codes.**
  - `sign.convenience_readers` (existing, exit 3, not retryable) gains `reason`: `no_declaration`, `reader_too_old`
    (with `required` and `declared`), `declaration_unknown`, `relay_too_old`, `unknown_readers` (a records push).
  - New: **`reader.decl_invalid`** (exit 3, not retryable), for a bad signature, binding, time window or `kat`. It joins
    the §0.6 table with H12d-2's implementation (the table lists only codes the host emits).
  - `sign.convenience_unavailable` keeps its reasons with the relaxed meaning and the `devices` detail.
  - The click signer passes them through; its `status` gains the problem `readers_old`.
- **Compatibility (Codex 7).** Contract v1.8 is additive for P clients in a workspace whose `required_reader` is 0.
  **Convenience admission requires upgraded participants**; it is not additive for them.

  | Participant on an old version | What happens |
  |---|---|
  | the primary (certifies devices) | unaffected: `CertRequestInner` and the certificate are unchanged |
  | the CLI signer of `member.add` behind a v1.8 host | refuses the review it cannot reproduce; update orch |
  | an old core as a new member | has no declaration: refused when `required_reader` is 1; if present from before, refuses convenience events on replay |
  | an old phone | has no `reader_decl` in its pair meta: refused (`no_declaration`; the host screen explains, Q4); if installed over a newer build, has no `rd` and is not served, and its parsers refuse the new views |
  | an old relay | `relay-info` 404 = level 0: no link while `required_reader` is 1, and no flag on a relay-linked workspace |
  | an old host behind a new phone | cannot occur if the pair meta field and `rd` ship in R6's first release (Q7); otherwise needs meta version negotiation |
- **Changes to F1 and the protocol.**
  - **F1** keeps the log, the signed-event contract and the certificate unchanged. It gains one label,
    `orch/v2/sig/reader-decl|` (`sig_reader_decl`), prefix-free with the protocol's labels (the label table taken from
    the corpus, O6) and added to `DEVICE_KEY_LABELS`. F1 §11.5 gains the KAT digest construction, F1 §5.3 a pointer
    here, and F1 §13 the note that `reader` moves into the certificate with the PoP change (Q3).
  - **The protocol (orch-29)**: protocol v2 §8.3 (`reader_decl` in the pair meta) and §8.6 (`rd` in the sealed request
    meta).

**9.12 Vectors and tests (ownership: orch-29 #5).**
- **Core-owned** (F1 vectors, which the relay and orch-f1 vendor):
  - `reader_kat_1`: the `log` variant (three independently signed, separately replayable events), the `view` variant,
    and the expected digests;
  - the convenience cases of `checkpoint.json` (for `AUTH_KINDS`).
- **Relay-owned** (`vectors_v2.json`):
  - `labels` gains `reader-decl`;
  - `reader_decl` signature vectors: a valid pairing declaration; the wrong workspace; the wrong `device_id`; the
    `dk_hash` of another key; the `offer_id`/`attempt` of another attempt; the wrong `who`; `reader: 0`; a higher
    unknown level (7, with `kat["1"]` only) accepted; a wrong `kat["1"]`; a signature by another key;
  - `pair_request` meta with `reader_decl`;
  - the request meta with `rd`.
- **Size (orch-29 #7).** A declaration is about 0.8 KiB as JSON (two digests, a b64u signature); the cap is 2 KiB.
  orch-29 confirms the pair-request body cap and the `K_pair` message size against it.
- **Host tests:**
  - the gate rows: each `READER_OPS` row refuses a level-0 reader with convenience history, with the flag on and with
    only the mark set, and admits a level-1 reader;
  - prepare, binding and submit each refuse when a convenience event landed in between;
  - a convenience submit racing a level-0 `member.add`: in either order, one refuses;
  - a `restore` that revives a removed level-0 device is refused;
  - the append-path table test;
  - direct-mode `member.add` with and without a declaration, and `register` importing the direct records;
  - `rd` absent, unknown or below the required level, each refused with its reason; `reader.declare` served while
    everything else is refused;
  - a lowering and a `reader: 0` declaration;
  - host records deleted: fail closed, then a re-declaration restores; `readers reset`;
  - revoked and removed devices stop counting; a foreign pair with the same device id inherits nothing; a `pk_pin`
    declaration with a certificate under another key is refused;
  - the relay level below 1 at link and at a later login, with a held restore blocking later publications;
  - the relaxed reasons name exactly the devices below level 1;
  - the records push refused on the mark and on a reachable convenience blob.
- **Client conformance** (the KAT cannot prove it): the phone shows "convenience (no presence)" and refuses unknown
  `auth` on every `READER_VIEWS_1` view; core's render shows the convenience line.

- **As built in H12d-1:** `reader_kat_1.json` (9.2) and, in `checkpoint.json` `transition_cases`,
  `restore_auth_convenience_accepted` (`ok`) and `restore_auth_unknown_refused` (`chain.diverged`), re-signed restore
  transitions appended last so no other case changed. `labels.json` gains `sig_reader_decl`.

**9.13 Streams and groups.**

| Stream | Changes | Group |
|---|---|---|
| core (orch-52) | the records-push mark and history report (9.7, first, its own fix); `orch.identity.reader` (`verify_decl`, the KAT runner over core's own replay parser); the label; the vectors it owns | **H12d-0** (9.7), **H12d-1** identity and vectors |
| core (orch-52) | reader records; `check_readers` at the append choke point (host and direct); `member.add` argument, signer review line; `reader.declare`, `reader.list`; `signer` in the views; the relaxed readers condition; `register`/`unregister` import and export | **H12d-2** (Opus, orch-52) |
| core, with R6 | the pair-meta `reader_decl`, the gate in the R6 allocation, `rd` checks in column R, the relay-info check in link and R1, `relay_reader_old`, `readers reset` | **H12d-3**, with R6's implementation |
| relay (orch-29) | `GET /v2/relay-info`; `AUTH_KINDS` with `convenience`; the relay-owned vectors; protocol v2 §8.3 and §8.6 text | relay PR (needs orch-d6's go for the relay scope) |
| mobile (orch-f1) | parse `auth` and `signer` (show "convenience (no presence)", refuse unknown values); the runtime KAT (`view`, and `log` if Q8); `reader_decl` in the pair meta; `rd` on every request; O3 persistence with the device key; O5 re-declaration on a new build | P4 groups |
| dashboard | per-device `reader`, the reasons and their device lists, the `on` warning | dashboard stream |

**9.14 Challenge and tie-break record.** The first draft (`affe5257`, Opus) was challenged by Codex (11 findings, a
static review: "flawed as written") and by both streams (orch-f1 O1–O6, orch-29 1–7). The Opus tie-break checked each
against the code (`ops/readers.py`, `ops/commands/member_add.py`, `host/convenience.py`, `host/records.py`,
`host/signer.py`, `host/human.py`, `records/`, `model/workspace.py`, `store/store.py`) and the specs (F1, orch-v2 D6,
§11A).

| # | Finding or objection | Verdict | Evidence and change |
|---|---|---|---|
| C1 | Host-only enforcement leaves direct mode and members' cores unspecified | **Confirmed** | `member_add.py` calls `refuse_new_reader` in both modes and has no declaration input. Now: one appending writer per workspace; the same check in host and direct mode; `--reader-decl`; direct records, imported at `register`; a P8 constraint for a second appender (9.1, 9.4) |
| C2 | The declaration does not sign its person or certificate binding | **Partly** | The same keys mean the same reading software, so the practical risk is small, but F1 lets anyone certify any key (rows 151, 152). `who` (`person_id`, or `pk_pin` before a certificate exists) is now signed, and records are per pair. No `cert_digest`: there is none before the certificate exists, and same-key renewal keeps the reader (9.3) |
| C3 | Downgrade protection assumes the old program cooperates | **Confirmed** | The phone-side marker binds only H12d-aware builds. Now: `rd` on every R request (an old build cannot send it); 9.0 states that old cores and phones fail closed on their own parsers (F1 row 153; orch-f1); caches and backups stated as outside host control (9.5) |
| C4 | State loss and rollback are different; a restore revives devices; the signer-device exclusion | **Confirmed** | `restore` can cut a `device.removed`; `reason()` skips the signing device. Now: the append check covers `restore`; `rd` makes rolled-back records harmless for serving; `readers reset` after a known restore; the residual is stated; the signing device is qualified by `reader_version` (9.4, 9.10) |
| C5 | Column R is not the whole relay read surface | **Partly** | True (restore evidence, chunk replay). No new gate is possible: the host cannot see relay reads. Shipped old parsers fail closed, replayed chunks are not a new disclosure, removal is the only stop. Stated in 9.6 |
| C6 | Current merged history is not enough for records export | **Confirmed** | `RECORD_PATH` exports the event logs; `abandon_tail` plus `restore` drops events from `merged_events`. Now: the durable mark plus a reachable-history report, as its own fix ahead of H12d (9.7) |
| C7 | Option (c) keeps the certificate, not universal compatibility | **Partly** | Moot for `CertRequestInner`, which is no longer changed (orch-29 #1). `_MATERIAL_KEYS` is exact, so the declaration is an argument, not material. A compatibility matrix is added, and "additive" is limited to non-convenience workspaces (9.11) |
| C8 | ±300 s against the 10-minute pairing; validating vs consuming; replay | **Confirmed** | `cert_pending` lasts up to 10 minutes (orch-v2 D6). Now: no clock window for a pairing; a window per `via`; freshness checked once, at admission; rechecks never consume; no ledger, and replay loses under "latest wins" and `rd` (9.3) |
| C9 | Lifecycle ops are not implementable as written | **Confirmed** | No op emits `device.added` today; `DEVICE_EXISTS` rules out same-id renewal; `human.prepare` takes `op/args/signer/handoff`. Now: the gate is by event at the append choke point; renewal is defined (9.3); `reader: 0` is allowed; `reader.declare` authorization and idempotency are specified (9.10) |
| C10 | The KAT adds regression coverage, not the stated proof | **Confirmed** | Each case is now independently signed and replayable, with a pinned context; a `view` surface; it is called "runtime self-test evidence"; display and restore handling are client conformance tests (9.2, 9.12) |
| C11 | Relay qualification is weak; a cached level survives unlink; "checkpoints keep flowing" | **Partly** | The weaker assurance is accepted explicitly (an operator statement, relay CI runs the vectors); it is rechecked at every login. A cached level after unlink is harmless: nothing more is sent. The ordered-chain blocking was **confirmed** and corrected (9.8) |
| F-O1 | The phone reads views, not F1 logs; the KAT tests a path it lacks | **Confirmed** | Option (a) adopted: per-surface KAT, with a `view` variant over the phone's exact view parser (9.2) |
| F-O2 | Level 1 needs a surface: views need `signer.auth` | **Confirmed** | `READER_VIEWS_1` gains `signer: {device, auth}` in v1.8, before the first phone release (9.11) |
| F-O3 | Persist the highest level with the device key | **Confirmed** | Adopted as the phone half of the downgrade defence; `rd` is the host half (9.5) |
| F-O4 | No ±300 s window for a pairing | **Confirmed** | Same as C8 (9.3) |
| F-O5 | Auto re-declare; `reader.declare` allowed alone in column R, with no person authority | **Confirmed** | Yes: exempt from `rd`, kind `device_signed` (9.5, 9.11) |
| F-O6 | The label goes in the prefix-free table from the corpus | **Confirmed** | 9.11 |
| R-1 | Wrong carrier: `CertRequestInner` is host → primary | **Confirmed** | orch-v2 D6 steps 2 and 4: the phone writes the pair request; the primary gets `CertRequestInner` from the host. The carrier is now the pair meta, and `CertRequestInner` is unchanged: the host enforces, and the primary needs no copy (9.1) |
| R-2 | Bind to `offer_id` + `attempt` + `dk_hash`; the workspace comes from the offer stream | **Confirmed** | 9.3; `workspace_id` stays signed, since the phone knows it from the QR |
| R-3 | `relay-info` is an honest-operator statement | **Confirmed** | Shape, 404 = 0, throttling, no workspace data (9.8) |
| R-4 | `AUTH_KINDS` += convenience sets the rollout order | **Confirmed** | `N = 1` means `AUTH_KINDS` has it; the rollout order is stated (9.8) |
| R-5 | Vector ownership | **Confirmed** | Core owns `reader_kat_1` and the digest text (F1 §11.5); the relay owns the `reader_decl` and meta vectors (9.12) |
| R-6 | Re-declare at renewal and rotation; keep the previous level until the old one expires | **Partly** | A software change is the trigger, not a renewal or rotation: same keys keep the record, and new keys are a new device. No expiry is needed (9.3) |
| R-7 | Size | **Open** | About 0.8 KiB, cap 2 KiB; orch-29 confirms the caps (9.12) |

**9.15 Open questions, with recommended answers.**
1. **An interactive challenge on top of the KAT?** *No in phase 2* (9.2). Revisit if a third-party reader appears.
2. **Relax `other_member` as well as `phone_device`?** *Yes, when every reader qualifies.* This extends the accepted
   risk to other members' trust in decisions. **Owner decision of 13 Oct 2026 (~18:15): relaxed, when every other
   member's devices and core have declared level 1; older setups still block.** A member who does not accept
   convenience decisions gets D66's policy (refuse named factors for named verbs), not a global block.
3. **Fold `reader` into the certificate later?** *Yes,* with the PoP certificate change (11A.12), copied by the
   primary from the device's declaration; the declaration stays as the device's own evidence.
4. **Tell a refused phone why?** *Not on the wire in phase 2:* the short `rejected` form has no reason field. The host
   screen, where the human confirms the fingerprint anyway, shows "This phone's orch app is too old for this workspace
   (convenience signing). Update it and scan again."
5. **Records push to an ordinary remote?** *Stays refused* while `required_reader` is 1; D-P2-34's private remote is
   unchanged.
6. **Trust in the relay's level without a runtime KAT?** *Accepted as an operator statement,* backed by relay CI on the
   core vectors; the worst case is a held restore, which R3 alarms.
7. **Ship `reader_decl` in the pair meta and `rd` in the request meta with R6's first release,** so that no host ever
   speaks the older meta? *Yes.* If R6 ships first, a meta version and a phone fallback are needed (orch-29, orch-f1).
8. **Does the phone verify F1 events (restore evidence) and so owe the `log` surface too?** *orch-f1 to answer;* if
   yes, the phone declares both surfaces.
9. **A phone paired at level 0 before convenience history** (orch-f1)? *Neither cut off nor grandfathered* (9.9).
10. **Land the records-push mark (9.7) separately and first?** *Yes:* it is a gap in today's code.
11. **A second appender in P8?** *A constraint, not a phase-2 question:* it must replicate the reader records and
    enforce invariant I.

**As built in H5c** (the core PR #407 and its follow-ups; the wire schemas above are unchanged):

1. The click signer is `orch.click` (`signer`, `view`, `server`, `spawn`, `client`, `protocol`, `log`), run as
   `python -I -m orch.click --fd N`; the dashboard imports only `spawn`, `client` and `protocol`.
2. **Durable sign records (H5c follow-up, changelog item 18)** are `orch.click.records.Records`, as item 5 specifies:
   `claim` (exclusive, before the signing), `complete` (the signed events, before `human.submit`), `create`
   (exclusive, a complete record at once), `get` (fail closed: `RecordError` → `record_unavailable`), `settle`
   (the result, after the commit) and `prune`. They reuse the custody helpers `secure_dir`, `read_private` and
   `write_private` (`orch.custody.convenience`). A record is at most 8 MiB. Two `human.click_refused` reasons are new:
   `already_signed` and `record_unavailable`; the log gains `adopted` (another signer process's record was used) and
   `record_failed` (a record could not be written again or settled after the commit; never escapes). Tests (`tests/host/test_click_records.py`) kill a real signer process
   with `SIGKILL` after the record and before the submit, and after the host committed but before the reply, restart
   it and get the identical events and idem on retry; a second View of the same request after a restart is refused.

3. **One signing path (changelog item 19, item 8 above)** is `orch.host.convenience.workspace_lock`, `refuse_handoff` and `pending_handoffs`, called from `HumanOps.prepare` (under the lock), `get` and `submit`, and from `orch host convenience on`. Tests: `tests/host/test_click_only.py`.


### 0.9 Subscriptions

```
sub.open  {topics:["workspace"|"tickets"|"needs"|"ticket:<Ref>"]{1..8}, after?:Cursor,
           detail?:bool = false}                                  // detail: v1.5, only to a host listing sub_ext/1
          → {sub:SubId, cursor:Cursor}
sub.close {sub:SubId} → {}
Item      = {id:ItemId, jpos:int, log:Uid | "workspace", key:Key | null, uid:Uid | null, seq:int, ws_seq:int, type:str,
             at:Time, actor:{kind:str, id:str | null}}
          | ActivityItem                                          // v1.5: an event item when detail is true
          | {id:ItemId, jpos:int, uid:Uid, gone:true}
          | {id:ItemId, jpos:int, type:"needs.changed"}
ItemId    = str/[0-9]{1,15}\.[evgn]/        // "<jpos>.e" event (incl. visibility.changed), ".g" gone, ".n" needs.changed
EndReason = "closed"|"grant.expired"|"person.removed"|"overflow"|"reset"|"host.restarting"
SubId     = str/sub_[0-9a-z]{1,16}/
```

- **Principal.** A subscription keeps the opening request's principal and viewer (§0.5).
- **Workspace-log events, per type (v1.5 fix, every subscription).** An event of the workspace log is delivered only under
  `activity.list`'s per-type rule (§0.8): never to `no-person`; `grant.issued` and `grant.revoked` only to a person-class
  owner and to the grant's own person (the `grant.list` rule); the other listed types to every viewer with a person; a type
  not in that rule's list (including any future one) to nobody. The rule is applied at every delivery with the viewer's
  current role, queued items included. A withheld event still advances the cursor, as any filtered `jpos` does. Before
  v1.5, §0.9 filtered by ticket visibility only, so `topics:["workspace"]` would have delivered `grant.*` items (type and
  actor) to any viewer, `no-person` included; v1.5 closes this for v1.4 clients as well (§0.4). **Required in the group that
  first serves `sub.open` (H6a); a blocker, not optional.**
- **`detail:true` (v1.5, cap `sub_ext/1`).** An event item is delivered as the `ActivityItem` of the same event (`actor` in
  full, `title`, `summary` and `data`, with the `Snip` and `_cut` rules), computed for the subscription's viewer at
  delivery, exactly as `activity.list` would return it at that moment. Its `id` equals the feed's `id`, so a client merges
  the live tail into a feed page without refetching. `gone` and `needs.changed` items are unchanged. Without `detail` the
  item is the v1.4 `Item`. `title` is the ticket's title at delivery, not at the event. The 1 MiB queue limit is not a
  frame size: items are sent as bounded `evt` frames (at most 64 items, each frame within the §0.2 frame limit), never as
  one frame holding the queue.
- **Revalidation before every delivery**, queued items included:
  - the grant must still be verified (not expired or revoked) and the person still a member, and a person-class
    mapping must still hold;
  - each item's ticket must still be visible.

  A failing item is dropped. If the principal fails, the host sends `evt {end:"grant.expired"}` or
  `{end:"person.removed"}` and the subscription ends.
- **`jpos`** is the event's position in the merged order (F1 §5.5) since the genesis. It is deterministic, so it
  survives a restart, because the journal is rebuilt from the logs.
- **Cursors** are opaque, at most 256 bytes. They encode `{workspace, principal hash, history epoch (genesis plus the
  last restore head), jpos}` and a short hash of the event at that `jpos`, and are authenticated with a host key kept
  in the host's state directory (so they survive a restart). A cursor the host did not issue, or whose event is no
  longer at its `jpos` after a journal rebuild, answers `sub.reset`. The principal binding is a scope check, not an
  access check: every item is filtered for the viewer at delivery. Page tokens do not reuse this encoding.
- **Snapshot and resume.**
  - Every snapshot read (`ticket.query`, `ticket.get`, `needs`) returns the `sub_cursor` of the jpos its state was
    read at, taken atomically on the workspace worker.
  - `sub.open {after}` registers the subscription and fixes the live boundary in one worker step, replays the items
    after `after`, then goes live, with no gap.
  - Its `res` is sent before any `evt` of that subscription. For a write from the same connection, its `res` is sent
    before any `evt` carrying its own events.
- **Item identity.** One journal position (one appended event) yields up to three items across its kinds, the event
  (`.e`), `gone` (`.g`) and `needs.changed` (`.n`), and for one subscription either `.e` and `.n` (the ticket is
  visible to its viewer) or `.g` and `.n` (the ticket was just hidden from it), never `.e` and `.g` together. They are
  told apart by `id`, which is unique per subscription and ordered by `(jpos, e < g < n)`. Clients dedupe by `id`,
  never by `jpos` alone (H6a review of #396).
- **Delivery and cursor.**
  - Items arrive in `id` order, batched up to 64 per `evt`. All items of one `jpos` go in the same `evt`.
  - An event that matches several topics of one subscription is delivered once.
  - `evt.cursor` points after the last `jpos` that frame completes. A client that stores it and reconnects with
    `after` gets everything later, and duplicates only if it stored an older cursor.
  - A `jpos` whose items are all filtered out still advances the cursor: the next `evt` that is sent carries it.
- **Visibility changes.**
  - A ticket that becomes visible to the viewer yields its event with `type: "visibility.changed"`. A `detail` item
    (v1.5) is the `activity.list` item of the same id unchanged (its own `type` and `data`); the client knows the ticket
    became visible because it had not seen it.
  - A ticket that becomes invisible yields `{id, jpos, uid, gone:true}`, sent only when the ticket was visible to this
    viewer at the previous `jpos`. This is the **one** item exempt from the visibility filter. It carries only the
    uid the subscriber already knew. The event that hid the ticket is not delivered to that viewer (it fails the filter
    like every later item about that ticket).
- **Accepted metadata (F13).** `jpos` and `ws_seq` are global, so their gaps show that invisible tickets changed, and
  `needs.changed` fires for every change to `needs`. Visibility in §0 is a display rule, not secrecy (F1 §9):
  workspace files are group-readable in tier B.
  `jpos` is also contained in `ItemId` (`"<jpos>.e"`) and may be encoded in `Cursor` and page tokens. A client that hides
  `jpos` from a further consumer maps `id`, `sub_cursor` and `next_page` to its own values as well.
- **End.** `evt {end}` closes the subscription:
  - `reset`: a cursor that is too old (outside the 10 000-entry journal), of another scope or epoch, or a journal
    rebuild (D-P2-12). `sub.open` with such a cursor answers `sub.reset` instead.
  - `overflow`: the queue exceeds 256 items or 256 KiB (1 MiB for a `detail` subscription, v1.5), or a slow reader lags
    more than 30 s. The client takes a
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
  - An unused or incomplete blob expires 10 minutes after its last chunk that changed it (a resend without effect
    does not extend it).
  - `human.prepare` copies the inner op's file bytes into the request, so it consumes the blobs it used when the
    request is stored (naming one again answers `blob.invalid`; a retry of the prepare with the same `opts.idem` names
    the file by `{digest}`). The request's file bytes count against the preparing UID until the request is terminal;
    a terminal request keeps no file bytes.
  - Blobs survive a reconnect by the same principal.
- **Names.** A blob has no file name. The artifact name or other logical name comes from `args` (F1 §6 name rule).
- **Limits.** Quotas count decoded bytes, reserved at the first chunk.
  - 64 MiB per blob.
  - 256 MiB per **UID** across all its principals and workspaces. The file bytes of prepared human requests count
    against the preparing UID.
  - 256 live blobs per UID, and every blob counts at least 4 KiB.
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
| blobs (§0.10) | 64 MiB each, counted at least 4 KiB; 256 MiB and 256 live blobs per UID; 1 GiB per host, 256 MiB of it for person-class UIDs |
| `workspace.settings` repos (v1.5) | 32 per page |
| `ticket.text` chunk (v1.5) | 4..262 144 bytes of text per response; nonempty progress |
| `commit.list` (v1.5) | 500 commits per page; 10 000 commits walked per request |
| `artifact.query` (v1.5) | 200 rows per page; 256 kinds in `counts` |
| `session.refusals` page (v1.5) | 200 items per page |
| refusal records (v1.5) | 7 days; 1 000 per (principal, session); 100 000 per workspace |
| `detail` subscription queue (v1.5) | 256 items or 1 MiB (§0.9 `overflow`) |
| audit log | `audit.jsonl` rotated at 10 MiB, 20 files kept. `human.*`, writes, `peer.denied` and control ops go to `audit-security.jsonl`, which read traffic never rotates away. |
| idle connection | closed after 10 min unless it has a subscription or a request in flight |
| runs (v1.6) | 8 agent runs per person per workspace; 16 agent runs and 16 shells per person; 64 per runner (`run.limit`); 10 starts per minute per person, burst 5 (`rate.limited`) |
| run ring (v1.6) | 1 MiB per run; an exited run's ring kept 1 h, at most 64 exited runs |
| term attachments (v1.6) | 4 per run, 16 per person, 64 per runner (`attach.limit`); capability 10 s unused (pending capabilities count toward no limit); 10 failed presentations per minute per runner |
| term output and input (v1.6) | 64 KiB per frame; 512 KiB queue per attachment (overrun: `gap`); input 256 KiB/s and 1 MiB per 10 s per run; sizes 20..500 × 5..200 |
| runner grants (v1.6) | 1 per (workspace, person), 64 per runner; click-issued at most 12 h |
| worktrees (v1.6) | 200 registered per workspace; create/remove 10 per minute per person; one git command per repo at a time, 30 s each; `worktree.list {check}` 8 checks per call |
| records (v1.6) | one run per workspace at a time; snapshot pages of 2 000 files; 64 MiB per file, 1 GiB per snapshot; 10 s local / 20 s network per git call, 60 s per run |
| run records (v1.6) | `<state>/hosts/<ws>/runs/` (0700/0600), exited runs kept 7 days, at most 1 000 |

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
   "outcome":"pass","hash":"sha256:81ab…","policy_hash":"sha256:5d07…",
   "source_sha":[{"repo":"https://github.com/acme/energy-dbt","ref":"refs/heads/t/DEMO-0043","sha":"4be1…"}]}],
   "material":{"kind":"gate","gate":"verify","input":{…},"policy":{…},"review":[{"event_id":"01J9ZT…","type":"verdict.given","fields":{…}}]},
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

**v1.4 → v1.5 (provisional draft).** Answers §9 item 8 (the dashboard stream's list and its comment on #378; addons/dashboard
HANDOVER "Still not served"), amended after the Codex challenge and the dashboard check of #390 (decided by orch-52).
Additive: new ops under new caps, optional keys under `read_ext/2`, new arguments behind caps; no op changes its version
(§0.4).

1. **Settings read:** new op `workspace.settings` (cap `settings/1`): the three durations with their F1 defaults marked,
   the four default gate policies, the last change of each, and the repos paged 32 per page under authenticated snapshot
   tokens. Repo `path` and `remote` (an F1 canonical identity, no userinfo; an unparseable one is omitted) only for
   person-class owners and maintainers. N gets durations and policies as an explicit exception, and only repos linked by a
   ticket it can see, with `default_branch` only; N polls.
2. **Full texts:** new op `ticket.text` (cap `text/1`), addressed by `(ref, seq, field)` and bound to `uid` and `event_id`
   (a replaced or abandoned event is `not_found`); UTF-8 byte offsets, `length` 4..262144, nonempty progress guaranteed;
   `ticket.get` visibility; workspace-log free text stays unserved. `Closed` gains `seq` and `event_id` for the closing
   reason.
3. **Bound hashes:** §0.8.1 tells recomputation (gate hash, policy hash from the new `material.policy`, question hash,
   section hashes) from consistency checks (`source_sha`, `base_rev`) and host attestations (`gate_gen`, `source_sha` at
   submit); the table is not exhaustive. `ticket.get` adds `GateView.hash` and `policy_hash` (undefined, so `null`, for a
   blocked gate), `Decision.hash` and `policy_hash`, `Question.qid` and `hash`, for display and comparison only.
4. **Fields:** `grant.list` gains `revoked {by, at, seq}` (only for an actual `grant.revoked`) and `session_ids` (first
   100 by `last_at` desc, id asc; `sessions` is the full count; `sessions_more`); `ticket.get` gains
   `ClaimFull.idle_until` and `Question.answer.auth`. `via: factory_charter` is **not** added: F1 stores no `via` (F1 §12
   N10, decisions entry 55), so it is P2-later. New op `session.refusals` (cap `refusals/1`) from host refusal records:
   ticket- or workspace-scoped with complete ticket association (else not recorded), validated `op`/`code`, `retryable`,
   the catalog's static `hint`, never args or runtime message/hint; filtered before paging; epoch-bound tokens; ingestion
   budgets. New op `artifact.query` (cap `artifact_query/1`) with optional per-kind `counts`. The member and grant ops the
   dashboard prepares through `human.*` are named; `member.list` `last_seen` is not served.
5. **Live tail:** `sub.open {detail:true}` (cap `sub_ext/1`) delivers each event item as its `ActivityItem` in bounded
   frames, so feeds merge. **Security fix (Low, from #378):** the `workspace` topic applies `activity.list`'s per-type
   rule for every subscription and client version.
6. **Git:** `diff {head, base}` and `commit.list` (cap `diff_ext/1`), authorized by `(ticket uid, repo name, verified repo
   identity)` from root heads (observed and matching pushed heads) and their fixed default ranges; bases must be ancestors;
   `since` excludes `head`/`base`; `base_source:"pushed"`; an explicit disclosure boundary for shared or merged history.
   `commit.list`: fixed snapshot, deterministic order, resumable or cut traversal, `parents_more`, `pushed_more`, and the
   author **name** as untrusted display data.
7. **Errors:** no new code. Per-op rows for the new ops; `sub.open {detail}` without `sub_ext/1` is `invalid.input`.
8. **Groups:** H6a must ship the workspace-topic filter; H6b ships `settings/1`, `text/1`, `read_ext/2` and `sub_ext/1`;
   `refusals/1`, `artifact_query/1` and `diff_ext/1` move to H6c.
9. **Consistency review of #390:** `material.policy` is required in gate material; the v1.5 accepted risks are in §7;
   per-op error rows, limits and the H5a, H6b and H6c acceptance cover every stated v1.5 rule.
10. **`human.prepare` honours `opts.idem` (v1.5, additive; orch-52 decision for the dashboard stream):** §0.7 moves
    `human.prepare` from "side-effect free, idem ignored" to "optional idem": a retry returns the same request, other
    arguments are `idem.conflict`, a terminal request answers as for other idems, no idem is unchanged and `dry_run`
    never stores (§0.8.1 item 8a).
11. **Click-to-sign (H5c, additive):** `Signer.auth` gains `"convenience"` (§0.8.6, D-P2-23). No new op and no new
    frame on the workspace socket: the click signer is a client like `orch sign`. New host codes
    `sign.convenience_unavailable` (with a reason) and `sign.convenience_readers` (§0.6). `human.click_refused` is a
    client-side code of the click signer, never sent by the host.
12. **Fixes (RH-1, RH-3):** `welcome.caps` lists `human/1` (it was in §0.4's table but missing from the welcome), and
    `human.prepare {idem}` also works for the host person ops `move`, `policy.set` and `settings.set` (same binding
    and `idem.conflict` rules as for registry ops).
13. **Sign dwell (§0.8.6 item 7, decided with the dashboard stream):** Sign is armed after 1 s of visible presentation
    instead of 500 ms; the signer's own 1 s dwell since it built the View is the authority (`too_soon` before it) and
    the dashboard arms no earlier.
14. **H7 (additive):** a dry-run `human.prepare` also returns `result`, the inner op's would-be `Result` (§0.8.1
    `Prepared`); `welcome.ops` lists `task.done` at 2 as the table above always said (a client must list it in
    `hello.ops`); a committed person request's `Result` keeps the inner op's `lines`.
    **Release note:** an older CLI (H4b, before H7) does not list `task.done` in its hello, so this host refuses its
    `orch task done` with `op.unsupported` ("served at version 2, not 1"); update the CLI. Every other op still works.
15. **H6c (additive, `diff` only):** `diff` serves `since:"my_approval"` and, under `diff_ext/1` (now listed in
    `welcome.caps`), `head` and `base`; `sources[].base_source` may be `host_pinned`, `approved` and `pushed`. No new
    error code: an unusable approval or an unauthorized `head`/`base` is `not_found`. See "As built in H6c" in §0.8.

**v1.5 → v1.6 (decided 12 Oct 2026, §12).** Additive: new caps and ops, a new peer class and socket, new error codes; no
existing op changes its version.

16. **Phase-2 addons on the host side:** caps `run/1`, `term/1`, `worktree/1`, `records/1`, `quick/1` on the workspace
    socket and `runner/1` on the new runner socket (peer class `runner`); `whoami` carries
    `runner` info; the click signer's deposit-only grant (amends §0.8.6 item 6); D-P2-32 amends D-P2-19 and H10a (a
    registered worktree is the ticket's working copy for D58 and `tree.check`); new codes in §0.6 and limits in §0.11.
17. **H11f (additive):** cap `quick/1` and op `quick.list` (read, columns P, G and N); no new event type, no new store
    (see "As built in H11f"). Catalog code `quick.unlabelled` (CLI only).
18. **Durable click-sign records (H5c follow-up, additive, client-side only):** the click signer writes and syncs a
    per-request record of the signed events and submit idem before a signature leaves it, so it signs at most once per
    `(request_id, view_digest)` across crashes and restarts (§0.8.6 item 5, "Durable sign records"). New
    `human.click_refused` reasons `already_signed` (another `view_digest` for a signed request) and
    `record_unavailable` (the record cannot be written, or exists but cannot be read: fail closed). A repeated `sign`
    after a signer restart needs no new `prepare` or `view`. No host change and no wire change on the workspace socket.
19. **One signing path per workspace (H5c follow-up, additive, owner decision of 12 Oct 2026):** new catalog code
    `sign.click_only` (exit 3, not retryable). With click-to-sign on, `human.prepare {handoff:true}` is refused,
    `orch host convenience on` is refused while hand-off requests are pending, and a hand-off request cannot be bound or
    submitted after the flag is on (§0.8.6 item 8). Prepare and the flag are serialized by a per-workspace file lock (2 s wait, then `lock.busy`). A hand-off submit holds that lock from its flag check to its journal write.
    No wire schema change; a client that does not use `handoff:true` sees nothing new.
20. **Claim before signing (follow-up to item 18, client-side only):** the click signer syncs a claim record
    (`view_digest`, op, idem, expiry, no events) before the crypto signing and replaces it with the completed record
    before the submit, so a crash or write failure between signing and recording can no longer lead to a second
    signature. A claim that never completed is refused as `record_unavailable` (`already_signed` for another
    `view_digest`); an in-process retry resends the identical in-memory events. Once signing has started the claim is never
    released; a failure fails that request closed. No new reason, no wire change.

21. **Click follow-ups (additive, after item 20):**
    the 2 s wait for the per-workspace lock raises `lock.busy` (exit 9, retryable) instead of `host.busy`, and is listed
    under `human.prepare` and (for a hand-off) `human.submit`; a hand-off `human.submit` holds the workspace lock from its flag check to its journal write
    (§0.8.6 item 8); `human.get` (in `request`) and `human.pending` carry `submitted_at:Time | null`, the host's time
    when `human.submit` was accepted for the request (null until then, and for records stored before the field).

**v1.6 → v1.7 (draft, 12 Oct 2026, revised 13 Oct after the Codex challenge and Opus tie-break, §11A.13).** The host
bridge client. On the wire it is additive: no op on any socket changes its version, and no new error code is added (an
oversized relay answer reuses `frame.too_large`). It adds **new internal contracts**, each named here:

22. **Column R, principal class `relay`** (§11A.4): principal `r:<device_id>`, viewer the device's person, built only
    from a verified sealed request as an immutable `RelayIdentity`. Relay requests use a new relay entry over the same
    op backends, not the socket dispatcher: `Call.authorize` keeps the relay principal, `columns.resolve_relay` and
    `_MATRIX["R"]` are new, `resolve` fails closed for an unlisted peer class, and every `column.name` site is audited.
    The exact device/person association, certificate and member-list scopes, and held evidence for that pair are
    checked again inside the job. R gets the P reads, `whoami`, `outcome.get`, and device-signed events through a closed
    event/scope table with a required idem. `decision` waits for its F1 evidence carriage.
23. **Exactly-once over the relay** (§11A.4): a rid state machine (`admitted` → `answered` → tombstone) with a durable
    submission journal of the device-signed events (the one stated exception to "no request plaintext persisted"),
    roll-forward through the outcome store (`relay.submit` in `NO_PARTIAL`, `append_signed` shared with
    `human.finish`), chunk bytes frozen at the first attempted POST, a start fence on `ts_ms`, and an explicit scope:
    it holds while the host state is not rolled back.
24. **State** `<state>/hosts/<ws>/relay/` (link with `max_publication_bytes`, unlink markers, holder, rid store,
    staging resume, publication history, unpublished cursor, alarms) joins the D-P2-17 custody list; `rids.sqlite` and
    its sidecars are opened by a checked procedure. Session tokens are never persisted.
25. **Readers:** `READER_OPS` gains `relay.link` and `relay.pair` (`sign.convenience_readers`). The reservation is
    written before any network exposure and is permanent: `_relay_paired` also reads `relay/link.json` and
    `relay/unlinked-*.json`; the history is rechecked after foreign appends (§11A.6).
26. **Publication** follows the relay's P3-D37 maximum: catch-up checkpoints minted by `Store.mint_catchup` (fresh
    final snapshot, new numbers above every existing `n`, restore anchors untouched, exact byte counting), relay
    rollback handled from a publication history, and the host-side permanent `relay.too_large` for an oversized restore
    (a `host.status` alarm, never a socket error).
27. **Evidence intake** (§11A.9): `Store.note_forwarded_revocation`, facts derived from the signed objects only,
    `chain=False`, the ack only after a durable result.
28. **Signing** (§11A.2): `RelaySigner` with typed methods is the bridge's only WSK path; this is an API rule, not
    isolation (bridge code execution is WSK compromise). Genesis-pin and restore reviews bind their exact material.
30. **Worktrees (H11d, addon #308, D-P2-32, §12.7; additive, cap `worktree/1`):** ops `worktree.list`, `worktree.create`,
    `worktree.remove`; effective-column kind `pg_write` (P and G, never N); catalog codes `worktree.none`, `worktree.exists`,
    `worktree.no_branch`, `worktree.branch_in_use`, `worktree.dirty`, `worktree.in_use`, `worktree.unpinned`,
    `worktree.git_failed`, `unsupported.platform`; runner command `git` (runner codes add `observe.unavailable` for the
    `orch.gitsafe` gate: an allow-list of repository config, owned config files, no populated submodule); `git-pins.json` gains `worktrees` (registrations with generations); `human.get` with a signer
    checks the copy bindings and the current branch on every call. *(Belongs to the v1.5 → v1.6 additions; numbered
    after item 29 (#440, H12d), the last number on develop or in flight when written. It may need renumbering at
    merge.)*

**v1.7 → v1.8 (draft, 13 Oct 2026, H12d; revised after Codex challenge and Opus tie-break, for orch-52's decision).**

29. **Reader-version handshake** (§0.8.6 item 9): a device-signed `reader_decl` (level, binding to keys, device,
    person, workspace and pairing attempt, per-surface known-answer digests), carried by `member.add`'s argument
    `reader_decl`, the pair-request meta (protocol v2 §8.3) and `reader.declare`; `rd` in every column-R request meta
    (§8.6) binds serving to the current declaration; one invariant checked at the append choke point (host
    `append_signed`, direct `Human.run`), for member, device, restore and convenience events; reader records under
    `<state>/hosts/<ws>/readers/` (direct mode: `.state/readers/`); new ops `reader.declare` and `reader.list`; `signer`
    in the decision and history views; `reader` and `required_reader` in `member.list` and `workspace.show`;
    `sign.convenience_readers` gains reasons, `reader.decl_invalid` is new; `relay_paired`, `other_member` and
    `phone_device` count only readers below level 1; a durable `convenience_seen` mark for records pushes. Additive
    only where `required_reader` is 0. No F1 log or certificate change; `CertRequestInner` unchanged; one new label
    `sig_reader_decl`.
31. **First-use gaps (H7 follow-up):** `orch host register` without `--peer` finds the person's device key in either
    custody (it failed once the key moved to convenience custody); new `orch host disable [--purge-releases]
    [--force]`, the reverse of `enable` (service stopped and unloaded, unit file removed, state dir kept); new
    `orch dashboard [args]` launcher for the dashboard addon. No wire, log or certificate change.
32. **Terminals (H11b, contract v1.6):** `term/1` (`term.preview`, `term.open`, `term.attach`, `attach.cancel`) and
    `run.stop` (`run/1`) are P-only; the runner reports `runner.term`; `whoami` gains `runner` for P and G; the runner's
    term socket re-checks its authorization at every use, and the host ends live attachments when a kill switch is
    pulled ("As built in H11b", §12.6).
33. **Records: the durable `convenience_seen` mark and smaller records items (#441):**
    `<state>/hosts/<ws>/convenience_seen.json`, set at the append choke point (host `human.finish`, direct `Human`
    append) before the first convenience event and whenever a scan finds one, never cleared, also set by `abandon_tail`
    for cut convenience events and by the host's scan of the merged history and the cut tails (never by the tool's
    report), asked by every reader-adding check (§0.8.6 item 1); `records.export_check` gains the required
    `history_convenience` (the tool scans the event logs of the history it pushes, before any network call,
    `GIT_NO_LAZY_FETCH=1`; D-P2-34); `orch records status` (the dashboard's child) writes nothing to the records repo
    and makes no network call; `records.status.configured` now means the workspace's records repo exists;
    `records.status` takes `current_url` and answers `remote.current_url`, `remote.current_private` and
    `remote.matches_last` (§12.8); `orch set REF labels=` sets the empty label set. Additive on the wire except
    `history_convenience`, which tool and host ship together. No log or certificate change.
34. **Store hooks for the relay (H12b, §11A.5 "As built in H12b"):**
    - In a relay-linked workspace every append writes its exact workspace checkpoint into
      `<state>/hosts/<ws>/outbox/` in the same commit (the pending directory). A restore's entry carries T and the full
      evidence set. Checkpoints outside an append are queued too.
    - R3's store API: `Store.outbox_entries`, `Store.outbox_delete(n, hash)` and `Store.mint_catchup(source, cap,
      measure)`. Catch-up uses dense numbers above every existing `n` and one `at`, with an atomic journalled
      replacement. A queued restore is an anchor and blocks catch-up. Prefixes are checked by
      `Checkpoints.sign_prefix`.
    - R4's store API: `Store.note_forwarded_revocation(item) -> held | pending | dropped`, with facts derived only from
      the signed objects.
    - New store codes `checkpoint.prefix_invalid`, `relay.too_large` (also R3's alarm code), `outbox.mismatch`,
      `relay.link_exists` and `relay.link_mismatch`. They are never sent on a socket (§0.6).
    - Review round 1:
      - a durable numbering reservation: no `n` is signed unless reserved, and none is reused;
      - outbox coalescing, `outbox_first`/`outbox_entries(limit)`;
      - `Store.relay_link(link, *, guard)` and `Store.relay_mark_linked`;
      - bounded forwarded evidence (F1 caps updated).
    - No wire, log or certificate change.
35. **Column R (H12a, contract v1.7 draft):** the relay peer class (`Peer.kind "relay"`, uid −1, `RelayIdentity`),
    column R with the closed op list `RELAY_OPS` and `OpKind "device_signed"` (`relay.submit`, never served on a
    socket), a fail-closed `resolve`, `Call.relay`; `principal()` and `principal_of` refuse a relay peer; `relay.submit`
    is never `partial` (the `relay_fields` hook); `append_signed` is the single append of signed person events (and of
    the `convenience_seen` mark, #441), `append_relay` the relay's only entry; `_relay_paired` reads `relay/link.json`
    and `relay/unlinked-*.json`; `READER_OPS` gains `relay.link` and `relay.pair`; at most 8 relay-linked workspaces per
    host (new code `host.limit`); control ops `host.relay_stop`, `host.relay_start` and `orch host relay stop|start`. No
    socket-visible change for P, G or N; no F1 log or certificate change ("As built in H12a", §11A.4).
36. **Reader declaration verify, KAT runner and vectors (H12d-1, §0.8.6 item 9):** the F1 label
    `orch/v2/sig/reader-decl|` (`sig_reader_decl`) in `orch.canon.LABELS` and `DEVICE_KEY_LABELS`;
    `orch.identity.reader.verify_decl` (code `reader.decl_invalid` with a `reason`) and `run_kat`; `crypto.dk_hash`;
    `orch.store.logs.parse_line` (the production line parser, extracted); core-owned vectors
    `tests/vectors/f1/reader_kat_1.json` and two convenience cases in `checkpoint.json`. No op, no wire, no log or
    certificate change: nothing calls the verifier yet (H12d-2).
37. **Phone person events over `relay.submit` (§11A.14, decided 15 Oct 2026, built with R2):** `question.answered` and
    `ticket.created` signed by the phone, one per `relay.submit`; `opts.idem` must equal the event `id`; admission
    only within the ULID window [now − 900 s, now + 300 s] (`event.expired`); dedup by signed-content digest and actor
    (`event.abandoned` for an id a restore abandoned); column-R `auth` ∈ {`secure-enclave-unlocked`, `passphrase`};
    exact next key (`ticket.key_not_next` with an advisory `next_key`); `ticket.get` gains `doc.head`,
    `questions[].answer.id` and `members_v`, `workspace.show` gains `next_key` (`null` for non-creators); the relay
    hold-off after a state restore becomes backup + 1 500 s. F1 §5.1 `id` amended; no hash, contract or schema change.
39. **Phone person events, core side (H12e, §11A.14 "As built in H12e"; the number may be renumbered when H11c
    lands):** `judge_events` shared by `human.submit` and `relay.submit`; `orch.host.admission` (`known_or_fresh`,
    `dedup`, `lookup`, `signed_digest`, `check_window`, `check_target`, `next_key`, `COLUMN_R_AUTH`); codes
    `event.expired`, `event.abandoned`, `ticket.key_not_next` in §0.6; `ticket.get` gains `doc.head`,
    `questions[].answer.id`, `members_v` (one snapshot); `workspace.show` gains `next_key`; vectors
    `phone_events.json` and a changed `text.json`; amendments A–D to §11A.14 (known identity before freshness,
    re-seal under a new rid, the retry table authoritative, a same-rid resend after the chunk expired). No hash,
    contract or schema change.
40. **Start agent and the runner grant (H11c, contract v1.6, cap `run/1`):** ops `agent.preview`, `agent.start` (P only), `grant.deposit` (a person-class caller
    with a verified `auth.grant` of their own `label:"runner"` grant; `OpKind "deposit"`, column G at the matrix),
    `grant.deposit_status` and `grant.forget` (P only); `whoami.runner.grant` and `run.list.runner.grant` carry the
    caller person's runner grant; `term.preview`/`term.open` take `harness:"claude-code"|"codex"` (person-run, kind
    `shell`) and `cwd`; `GrantView` gains `label` and `issued_at`; the click signer's `prepare {deposit:true}` and the
    `sign` answer's `deposit`, `deposit_id`, `grant` (§0.8.6 item 6); `orch grant --runner`; `orch sign CODE` asks
    show or hand to the runner; the runner commands `grant.put`, `grant.drop` and the `spawn` fields `grant`,
    `session`, `person`; `<state>/runner.json` (`login_shell`, `programs`, `pass_env`); first-party manifest
    `addons/start-agent`. No F1 log or certificate change ("As built in H11c", §12.4).
41. **Terminal follow-ups (after #469):** `orch host run --foreground
    --dev-runner` admits a foreground runner of the host UID announced in `<run>/term/<uid>/dev-runner.json` (dev and
    tests only; never a service run directory or another UID; §12.3.3 "As built in H11a"); the retryable refusals of
    `term.attach`/`term.open` carry `retry_after_ms` (bucket-derived for `rate.limited`; the runner's `attach.limit`
    keeps its code); `ORCH_HOST_RUN_DIR` is the one run-dir env name (core never reads `ORCH_RUN_DIR`);
    `TermClient.send` has its own write deadline (`send_timeout`). No wire schema change.
42. **Device liveness and judge hardening (H12e-2, §11A.14 "As built in H12e-2"):** `orch.host.admission.device_standing` (the API for column R's `standing()`); `judge_events` maps
    unexpected model exceptions to `validation.event`; §11A.14 amended: a `failed` outcome with nothing landed counts as
    `refused` (14.4), the identity check lives in the result builder with no digest in `Planned` (14.6 L7, 14.9),
    start-up recovery runs in R2's `Bridge.start` (H12a R5). No hash, contract or schema change.
43. **Start-agent dashboard follow-ups (H11c-2):** `grant.list` items gain `label`
    (`issued_at` was already there); the click signer's `sign` answer gains `deposit_reason` when `deposit` is
    `rejected` (closed set `grant.forgotten`, `grant.not_runner`, `grant.verbs`, `grant.expiring`, `rate.limited`,
    `runner.unavailable`, `other`; never host text) and `grant.deposit_status` gains `reason`; the click signer
    `contract` goes from 1 to 2 (deposit support; a client for contract 2 accepts 1 and 2); `run.sub` gains the
    `{t:"runner", grant}` item (§12.3). No F1, schema or capability change.
44. **Read-time worktree back-pointers (#486, after #485):** every reader open (`gitdir` and `worktree` profiles,
    and direct mode) checks git's back-pointers against the pins: a linked gitdir is `<commondir>/worktrees/<id>`
    whose `gitdir` file names the pinned `<working copy>/.git`, a main gitdir is `<working copy>/.git`, and the
    working copy's `.git`, read through its pinned fd, names the pinned gitdir (linked) or is it (main); the
    `gitdir` profile's allow-list gains exactly `<working copy>/.git`. Relative back-pointers (git 2.48+
    `worktree.useRelativePaths`) are refused, as at pin time. A main repo that a compromised batch pin child passed off as another declared repo's worktree,
    or a worktree moved since it was pinned, is observe.unavailable. No wire, schema or capability change.

**v1.8 → v1.9 (additive, #479; item numbers may be renumbered when a parallel change lands first).**

44. **`Error.detail`** (§0.3): an optional object on the socket `Error`, closed per code. `HostError.error()` sends it
    and `frames._ERROR` allows it. Only `term.not_attachable` (`state`), `run.limit` (`which`), `launch.no_grant`
    (`reason`), `runner.disabled` (`missing`: `addon`, `machine` or `terminals`) and `ticket.key_not_next` (`next_key`) may carry it; any other code, key
    or value is dropped before sending, and no free text or secret is ever sent. The §0.6 rows that promised
    `detail.*` are now true; `ticket.key_not_next` carries `detail.next_key`. A client ignores unknown `Error` keys
    (§0.4), so no cap or op version changes.

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
| dev and tests | `orch host run --foreground` (`--dev-runner`: see §12.3.3 "As built in H11a") | `<state>/run` (no agent UIDs) |

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

**Declared paths** (v1.6: a worktree registered by `worktree.create` is the ticket's working copy for D58 and
`tree.check`, bound by its registration generation, §12.7). Repos are opened by an `O_NOFOLLOW` walk of the path the host joins from `settings.repos`,
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

  `orch init --host` runs it (H3b3). Since H7 host mode is the default for a registered workspace, and registration
  starts the per-user service; `orch init` itself still creates the workspace in direct mode, then `orch host register`
  hands it to the host (see "As built in H7"). The directory must be empty; the peers map
  is the caller's UID as the genesis owner (agents and the worker are added later by `unregister` + `register`). The
  device key is kept under `hosts/<ws>/person` as in direct mode. Once the genesis is in the log, a failure to finish
  the bootstrap is answered `outcome.unknown`, never a refusal. The client treats `outcome.unknown`, `host.restarting`
  and a lost connection as undecided and asks again with the same idem for up to 30 s. Any other refusal is checked
  with `host.outcome`. Only no stored record (refused before any write) or a `failed` record drops the device key and
  tells the person that nothing was created. `control.rolled_back` starts again from step 2 after one confirmation.
  Without a definite answer the result is `host.unavailable`, which names the idem for `orch host outcome` and keeps the
  device key. A rollback removes the store's files from the root but never the person's own directory. The root guard
  and `by_root` compare directories by `(st_dev, st_ino)`, not by spelling (APFS case and normalisation variants).
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

H6a checks cheaply before every append (the store's own count of logs it found changed on disk, and one `stat` of
`tickets/`), compares every log in the 60 s check and at `sub.open`, and compares a log's size with what the store
wrote right after each commit. A foreign append to a **ticket** log changes no authentication snapshot, so until one of
these checks runs (at most 60 s, or the next append) the bus may still deliver that ticket's pending items with its
previous visibility. This is tier A only, within F11. A rebuild that fails leaves the workspace `degraded` with no
journal (`sub.open` answers `workspace.unavailable`, every subscription ends with `reset`) until the next 60 s check
builds it.

**D-P2-13. Limits** (§0.11). Audit lines record time, UID, class, op, principal, session, code and duration, never a
secret.

**D-P2-14. Versioning by capabilities** (§0.4). Older clients are kept honest by recorded fixtures (H7:
`tests/host/fixtures/cross_version/`, one file per previous contract version; a new version adds its clients there).

## 4. Human signing

**D-P2-15. The client signs. The host never prompts and holds no device key.** A daemon has no terminal, and a host
that could prompt would let any peer raise prompts on the person's screen. C7's rules stay with the signer.

**D-P2-16. The grant secret is drawn by the signing client, never by the preparer (§0.8.1).** Later calls made under
the grant send the secret in `auth.grant`.

**D-P2-21. The signer recomputes hashes from structured material and renders its own review**, opening with its own
header and the shared fingerprint. A host bug must not be able to make the person sign content they did not see.

**D-P2-22 (manager ruling, orch-89). The Touch ID signer H9 is in P2 and must land before the cutover (D44).**
*Re-planned by the owner on 12 Oct 2026: H9 moves to phase 3; phase 1 signs with D-P2-23.*

**D-P2-23 (owner decisions, 12 Oct 2026; coordination log "OWNER RE-PLAN P2" and "D65 amendment", #264 comment
6121460551, and the orch-d6 decisions). Phase 1 signs from the dashboard with one click** (§0.8.6, group H5c): the
device key is held by the `convenience` custody backend (the traditional login keychain on macOS, a 0600 file in the
person's own state dir on other Unix platforms, unsupported on Windows), with no user presence, by a separate
person-class signer process beside the dashboard (`python -I -m orch.click`, a child of the dashboard server over a
socketpair). The signer prepares click requests itself, runs every §0.8.1 check, renders the View the dashboard shows,
and signs the stored immutable signing inputs of requests it prepared; events carry `auth: "convenience"` (F1
amended). The host still never prompts and holds no device key. **The dashboard (its HTTP process and browser app) is
trusted with signing authority**: the separation protects the key against passive disclosure, not against an active
compromise, an XSS or a stolen bearer driving prepare, view and sign, and code execution in the HTTP process can read
the key. **Accepted risk, on the workspace card and in the docs: "Convenience signing: agents running as you, or anything
that takes over this dashboard, could sign."** The flag is **per workspace** and off until switched on; the host allows
`auth: "convenience"` only while every reader of the workspace is local and updated (no relay pairing, no other
device, no other member, a model that knows the value: `sign.convenience_unavailable`), and refuses adding a reader to
a workspace whose history has convenience events (`sign.convenience_readers`), because switching back to terminal
signing would not help: the new reader must still parse the old events. `orch sign CODE` stays available, and `grant`
stays terminal-only. Supersedes D41's per-signature presence for phase 1 and amends the dashboard's D65 rule ("the
browser never handles a factor" stays true; "signing only in the terminal or H9" does not). Reason: the owner wants
phase 1 usable every day in tier A, where an agent of the same UID can already read a passphrase typed into a terminal
it can see (D65's stated limit); presence returns with H9 and tier B in phase 3.

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
| H5c click signer (`python -I -m orch.click`, beside the dashboard, auth `convenience`, phase 1) | its own `human.prepare` with `signer` (bound at prepare), then `human.get` | the dashboard's sign panel shows the signer's View (the terminal review line for line, the risk line), §0.8.6 | a click; a typed word for member, addon-grant/purge, policy and repo-settings ops; none on the key |
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
| Click-to-sign (phase 1, D-P2-23) | §0.8.6 item 8. Accepted: a same-UID agent, and anything that takes over the dashboard (XSS, stolen bearer, code execution in the HTTP process), can sign (shown on the card). The signer signs only requests it prepared itself, binds the full signing inputs, and keeps the key out of the HTTP process against passive disclosure. Readers that do not know `convenience` are excluded per workspace. |
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
    - **As built (#471):** the repos to pin are pinned in **one** reader child per 64 of them (`pin_many`, profile
      `pin`, the same declared roots and audit hook as a single `pin`), answered per repo: one repo's refusal never
      touches the others. A batch that fails as a whole (the child dies, overruns a limit or answers malformed) decides
      nothing and each of its repos is pinned again in its own child; `quota.host` is not retried repo by repo. The
      host checks every answered pin against git's layout under the declared repos (the working copy is the asked
      path; the commondir is a declared repo's `.git`; the gitdir is that commondir or its `worktrees/<id>`) before it
      is stored, so a child forging pins fails its batch. One child per repo had made the first request after such a
      `settings.changed` cost ~70 ms per repo (#468).
      The layout check cannot tell a main repo B passed off as `<A>/.git/worktrees/<id>` of another declared repo A
      from a real worktree of A; every read therefore re-checks git's back-pointers (#486): the worktree's `gitdir`
      file must name the pinned `<working copy>/.git` (read with `O_NOFOLLOW`), its `commondir` the pinned commondir,
      and the working copy's `.git`, read through the pinned working copy's fd (`O_NOFOLLOW` walk, device and inode),
      must be a `gitdir: <path>` file naming the pinned gitdir (or be the pinned gitdir, for a main repo). This runs
      on every open, in both profiles and in direct mode; the `gitdir` profile admits exactly `<working copy>/.git`
      for it, the working copy only as an ancestor (#498). A forged or moved pin is refused when it is used.
      Relative back-pointers (git 2.48+ `worktree.useRelativePaths`) are refused, as they already are at pin time.
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

  **As built in H10a (phase 1, tier A, no containment).**
  - There is no worker UID in tier A, so the host runs the `tree.check` job itself: `TreeCheckInput` from the repo's
    pins, one `python -I -m orch.gitread` child through the launcher (fixed argv, empty environment, the §0.11 limits,
    the child's audit hook) under the `worktree` profile, and the child's raw answer checked and escaped into
    `TreeCheckResult` by the host (`orch.gitread.treecheck`, `orch.gitread.treejob`). The phase-3 worker takes over
    the same job kind and input; only the caller of the child changes. A tier-B workspace has no check until then
    (fail closed).
  - It also compares the index with `HEAD`'s tree, so a staged change, an unmerged entry or an `intent-to-add` entry
    is a change (`why:"modified"`; a path of `HEAD` missing from the index: `"deleted"`).
  - **Clean at the signed commit.** `TreeCheckResult` also carries `head` and `head_ref` (host-side, never shown):
    the child reads `HEAD` and the tree in one call, and at prepare and at binding they must equal the signed
    `source_sha` entry of that repo's identity. A switch to another branch or commit between prepare and binding is
    refused even when that tree is clean.
  - Unknown, never clean: a split or sparse index (or any required index extension, or a directory entry of mode
    040000), a bad checksum, a populated submodule (its own `HEAD` is not checked), a detached `HEAD`, a walk deeper
    than 32 directories, and the §0.11 bounds (`INDEX_MAX`, `INDEX_ENTRIES`, `WALK_ENTRIES`) all answer
    `observe.unavailable`. The `FSMN` and `UNTR` index extensions are skipped unread.
  - **Practical ceiling.** The walk is pure Python under the child's 10 s CPU and 30 s wall limits, which are reached
    long before `WALK_ENTRIES` (200 000) or `INDEX_ENTRIES` (1 000 000): a clean tree of 10 000 tracked files takes
    about a second, and the practical bound is tens of thousands of files. A bigger tree, or one with very large
    tracked files whose stat does not match, answers unknown (fail closed). The index entries and `HEAD`'s flattened
    tree are held at once, under the 512 MiB resident limit (a larger repo is killed and unknown).
  - `skip-worktree` and `assume-unchanged` are dirty (`flag`), never trusted, and so is a sparse checkout (its entries
    carry `skip-worktree`); `extensions.worktreeConfig` (written by `git sparse-checkout`) or a sparse index is
    unknown.
  - **Best effort, by design (owner decision, phase 1).** The index and the ignore files are agent-writable, as they
    are for git:
    - *Forged stat data*: an entry whose stat fields are rewritten to match a file changed before the index was
      written is trusted (`git status` is fooled the same way). The racy rule (an entry or file mtime/ctime in or
      after the index's second is hashed) protects against accidents, not adversaries. Accepted for phase 1:
      uncommitted bytes never enter what is approved (the decision signs a commit, re-read at submit). **H10 decision
      for later:** "hash every tracked file at the binding check, unknown above a byte budget".
    - *Hidden untracked files*: an untracked `.gitignore` (for example `*`, which also ignores itself) or
      `.git/info/exclude` can hide untracked files, as from git. Always reporting an untracked `.gitignore` is not an
      option (`.venv/`, `.pytest_cache/` and other tool caches ship one). `core.excludesFile` is config and is not
      read, so a file only it ignores counts as untracked.
  - Every repo a prepared event binds (each `source_sha` entry, by identity read again through the reader) must have
    a clean check, at prepare and at binding; a missing one is `observe.unavailable`. The binding re-run reads the
    tree again; the stored `checks` are never trusted.
  - `human.prepare` returns `checks` for each linked repo found clean; `human.get` with `signer` runs the check again
    when it binds and at later binding gets, refuses a changed, moved or unknown tree with `observe.unavailable`
    (nothing is bound) and refreshes `checks[].at`. A bound request's later gets reuse a check younger than 5 s (a
    polling client does not re-walk the tree each time); binding itself always checks. `attested_by` stays
    `"worker"`: in tier A the host's reader child plays the worker's part. A dry run runs no `tree.check` job and
    carries no `checks` (as above).
  - Every check runs on the workspace's worker thread (other requests of that workspace wait, up to the child's 30 s)
    and holds one of the host's four reader children. It fails closed and keeps the F10 per-principal rules; in
    phase 3 the worker owns the job and the host thread only waits for `job.done`.
  - **Wiring and pins.** The served workspace gets H3b1's reader (`Capabilities.host(git=...)`, pins in
    `<state>/hosts/<ws>/git-pins.json` (0600), one `ReaderPool` per host). Pins are taken centrally by the workspace
    service: at workspace open and whenever the declared `settings.repos` set changes (a `settings.changed`, checked
    at each store refresh and before each request's capabilities), under the workspace's own principal
    `host:<workspace id>`, never host-wide. An existing pin is never replaced. A repo that cannot be pinned is
    remembered and retried only after a backoff (1 minute, doubling to 1 hour) or the next change of the declared
    set; until then its reads are `observe.unavailable`. The pin file is written (a fresh 0600 temporary file,
    fsynced, renamed) before the pins change in memory; an unreadable pin file denies git to the workspace (fail
    closed), never fresh pins.
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
- **Accepted in v1.5 (§0.8.5; Codex challenge and consistency review of #390).**
  - **Shared or merged history.** `diff {head, base}` and `commit.list` reject objects outside a ticket's authorized
    ranges, but a range may contain commits of other tickets (a merged branch, shared history), including content later
    deleted. That git content is disclosed to viewers of this ticket. F1 records no commit provenance, so no stronger
    promise is possible; the approval default (observed head, §0.8 base) is unchanged.
  - **The linked-repo check is not a confidentiality boundary.** `diff {repo}` (and `commit.list {repo}`) accept only a
    repo in the ticket's `links.repos`, and the branch comes from `links.branches`. Both are writable by anyone who may
    `set` the ticket's links, including an agent on its own visible ticket. So such a caller can `diff` any declared
    repo and any branch of it through that ticket, including a branch that belongs to a restricted ticket. The check
    scopes a request; it does not hide repositories. A person-class owner or maintainer gets the reader's reasons, which
    show whether a branch exists. Any other caller gets `repo <name>: not readable` (the reason goes to the host log),
    so it learns only whether the diff succeeded.
  - **N's configuration exception.** `no-person` sees the three durations and the four default gate policies, which come
    from the workspace log, and the names and default branches of repos linked by tickets it can see.
  - **Refusal records are host-attested and incomplete.** They are unsigned, not in any log, dropped past the ingestion
    budgets and retention, and a ticket-scoped refusal without complete ticket association is not recorded. They are a
    diagnostic, not an audit trail (`audit-security.jsonl` remains the host's record).
  - **Git display data is agent-writable.** Commit subjects, author names and committer times in `commit.list` and
    `diff` are repository content: cleaned of control and bidi characters, never an identity or a time the host vouches for.
  - **Display hashes.** `ticket.get`'s gate, policy, decision and question hashes are the host's view at the read; the
    signer relies only on `Prepared` and its own checks (§0.8.1), so a stale display hash can mislead only until the
    prepare.

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
| **H5a Human contract** | `human.prepare`/`get`/`submit`/`cancel`, multi-event requests, material, signer recomputation and header, CLI composites, person versions of agent ops, `opts.dry_run` | mismatch, expiry, single use, tombstone; stale between prepare and submit; prompt fields equal `describe_payload`; a grant prepared with a non-null `secret_hash` is refused, and the preparer cannot obtain the secret (F1); `human.get` returns every invariant byte (§0.8.1) identical to `human.prepare`'s, and differs only in `actor.device` and `auth` after binding (F5); `verdict pass` and `verdict fail` on one gate have different fingerprints (N2); a prepared request with an event type outside `EMITS_V1[op]`, a second `log`, another `based_on` or a foreign `actor.id` is refused by the signer before any prompt (N4); `secret_hash` is set aside only on `grant.issued`; replay precedence (identical signed events give the stored result, other events give `human.request_state`); every state transition of the table; the `Review` (payload and derived) recomputed by the signer for every person event type; an unknown payload key is shown (L2); a section text whose `section_hash` differs from the event is refused; a `settings.set` `before` key outside `set` is refused; a crash between two appends of a multi-event submit is rolled forward at start and ends `committed`; human `submit` replay precedence against missing, conflicting, expired and fresh idems, with the fresh idem getting its own record; submit from another workspace's socket gives `not_found`; a multi-event request is all or nothing; **v1.5 "Bound hashes":** gate material always carries `policy`; the signer recomputes `policy_hash` from `material.policy` and the gate hash from `material.input`, and refuses a `policy_hash` that differs from either `material.input.policy_hash` or the event's; `source_sha` is checked only against `material.input.source_sha`; `gate_gen` is only displayed and checked by the host (`gate.stale`) | Opus |
| **H5b Hand-off** | codes, `human.pending` (by code and by request id), device binding in `human.get`, `orch sign CODE` | a successful hand-off sign end to end; `orch sign` shows the workspace from its own registry, the op header and the fingerprint before content; binding to a second device is refused; failed lookups on one connection do not lock out another (F5); the `pending` states `prepared`, `submitted`, `cancelled` and `expired` (R6) | Opus |
| **H6a Live updates** | journal, `jpos`, `sub.*`, revalidation, cursors, end reasons, `wait` on the bus, foreign-append rebuild | a revoked grant and a removed person stop delivery, queued items included; resume with no gap after a host restart; overflow, then snapshot, then cursor; a write's `res` before its `evt`; the journal is rebuilt after a foreign append (F11); one `jpos` yields up to three distinct ids, `.e` and `.n` to a viewer who still sees the ticket and `.g` and `.n` to one who lost it (two subscriptions, one `evt` each), never `.e` with `.g`; a fully filtered `jpos` still advances the cursor; `gone` is sent only to a viewer who saw the ticket; **v1.5:** `topics:["workspace"]` gives `no-person` no workspace-log item and a non-owner no `grant.*` item of another person, rechecked at delivery after a role change, and the withheld positions still advance the cursor | Opus |
| **H6b Dashboard reads** | `ticket.query`, `ticket.get`, `needs`, `activity.list`, `workspace.show`, `member.list`, `session.list`, `grant.list`, `move`, `policy.set`, `settings.set`; v1.5: `workspace.settings`, `ticket.text`, `sub.open {detail}`, the `read_ext/2` fields | paging stays consistent under appends (no repeat, no gap); restricted rows hidden; `needs` equals `model.needs`; `ticket.get` evidence digests match the manifest; `grant.list` hides `secret_hash`; `move` allowed and refused per its rules; **v1.4:** `restricted`, the task and criteria counts, `blocking_questions`, `claim` and `closed` on rows are absent or never counted for a restricted ticket the viewer cannot see (`total` unchanged by it); `restricted` is `true` exactly for non-`workspace` rows the viewer may see; the new `needs` fields for `question`, `approve`, `no_eligible`, `acknowledge` and `addon` equal `model.needs` (with `who` limited as above) and the question or rule text, and `since` follows the table in §0.8; every new `ticket.get` field equals the model view (a question's options and answer, a receipt's `evidence`, gate `approved_by` and `decisions`, `verdict.current`, the manifest against `artifact.get` digests, `claim.live`, `source` against the latest `branch.pushed`), and `LogItem.data` holds only the fields in its type table (no `sig`, `host_sig`, secret or text beyond `Snip`); `sections` holds hashes only; a closed ticket: `ticket.get`, `diff`, `artifact.get` and `sub.open {ticket}` succeed for a viewer who may see it and give `not_found` for one who may not, `ticket.query` leaves it out without a `status` and returns it with `["closed"]`, `needs` has no `approve` for it, and a reopened ticket loses `closed`; `activity.list`: every item filtered on its own (an agent grant or `no-person` never gets a restricted ticket's events; `no-person` gets no workspace-log event; G gets its own person's `grant.*` and the person-type events per the op text; a non-owner gets only their own `grant.*` events and none of another person's), `next_page` and page fullness without holes, no `total`, a token with other arguments is `invalid.input`, an expired token is `page.expired`, a ticket that becomes visible between pages adds nothing to the snapshot, and one that becomes invisible is dropped from later pages; a revoked grant or a removed person on page 2 is filtered as the new viewer (H6b: answered `page.expired`, a change of the principal's viewer); a forged, altered or other-principal `page` token (also for `ticket.query`) is `page.expired`, and a token with other arguments is `invalid.input`; workspace-log `data` holds only the fields of its table; a `viewer`-role person gets member, role, device, policy and settings items with the table's fields only, and no `grant.*` item; `needs {person: X}` by an owner who is not on a restricted ticket's list returns none of X's items on it; `who` and `eligible` never name a person who cannot see the ticket, and a gate whose approver tokens no visible person holds is `no_eligible` with `cause:"no_visible_person"`, a gate with no token has `cause:"no_token"`, and `blocked` stays F1's token test; `duplicate_of` naming a ticket the viewer cannot see is returned as the key only, in `closed`, `LogItem.data` and the feed, and `ticket.get` on it is `not_found`; the `_more` flags and `_cut` siblings are set exactly when something was cut, the first page's `sub_cursor` joined to `sub.open` loses and repeats nothing, `settings.changed` shows key names only; `read_ext/1` and `activity/1` listed in `welcome.caps`; **v1.5:** `workspace.settings` gives N no `path`, `remote`, `settings_at` or `policy_at`, and marks defaults; `ticket.text` returns the exact stored text in chunks that never split a scalar value, `not_found` for a restricted ticket, a field the event lacks, or a workspace-log event; the `read_ext/2` hashes equal the model's (`hash12` is a prefix of `GateView.hash`) and `Decision.hash` equals the signed event; `grant.list` `revoked.by` is the `grant.revoked` signer and `session_ids` follow the `grant.list` rule; a `detail` subscription item equals the `activity.list` item of the same event for the same viewer; `settings/1`, `text/1`, `read_ext/2` and `sub_ext/1` appear in `welcome.caps`, and `read_ext/2` also lists `read_ext/1`; `detail` without `sub_ext/1` answers `invalid.input`; `workspace.settings` pages 32 repos per page on a stable snapshot, with `page.expired` and `invalid.input` for a token with other arguments; `path` and `remote` only for a person-class owner or maintainer (not for G acting for one), and an unparseable `remote` is omitted; N's `repos` hold only repos linked by a visible ticket, with `default_branch` only; `ticket.text` gives `not_found` for an `event_id` that is not the event at `seq` (a restore-abandoned event), byte offsets with nonempty progress at `length` 4 on 4-byte scalars, `invalid.input` for an offset inside a scalar value and for `offset > 0` without `event_id`; `Closed.seq`/`event_id`, `ClaimFull.idle_until`, `Question.qid`/`hash`/`answer.auth` and `sessions_more` match the model | Codex, Opus (visibility of the new fields) |
| **H6c v1.5 listings** (v1.5) | `session.refusals` and the dispatcher's refusal records, `artifact.query`, `diff {head, base}`, `commit.list` | refusal records hold no args, message, hint or secret, only for column G with a session, and follow the `grant.list` person rule (an N caller gets `op.not_allowed`, a non-owner none of another person's), with a restricted ticket's refusal left out; `artifact.query` rows equal the union of visible manifests (`total` unchanged by a restricted ticket); `diff` and `commit.list` refuse with `not_found` a `head` or `base` outside the authorized ranges; **a repo identity change** (an old push with another `repo_id`, a `before` with another identity or ref) authorizes nothing; **merged history** (a visible ticket that merged another ticket's branch) discloses only what the documented boundary says; a caller-chosen endpoint never enlarges a range; **bounded continuation**: a range over 10 000 commits either resumes from host state or ends with `walk_cut:true` and `next_page:null`, never rescanning a prefix; `parents_more` and `pushed_more` set exactly when cut; subjects and author names cleaned; refusal records: complete ticket association or none, static catalog hint only, ingestion budget never blocks dispatch; `session.refusals`: `not_found` for a `session` or `ticket` the viewer may not see, epoch-bound tokens (`page.expired` after a restore), no `total`; `artifact.query` `counts` sum to `total`, appear on the first page only, and `counts:true` with a `page` token is `invalid.input`; `diff {head, base}`: `since` with either is `invalid.input`, and `repo` is required; `commit.list`: the author is the name only (no email), and `base_source:"pushed"` for a pushed base | Opus |
| **H7 Turn host mode on** | the default for registered workspaces; full parity; cross-version fixtures (an older client against a newer host) | full golden parity including human ops and `wait`; T2 in both modes. Needs H10 for `verdict` and `approve code` parity (clean-tree check). | Opus |
| **H5c Click-to-sign custody** (D-P2-23, phase 1) | F1 amendment (`auth: "convenience"`), the `Signer` schema, the per-workspace flag (`orch host convenience`) and the readers condition (`sign.convenience_unavailable`, `sign.convenience_readers`), `orch.custody.convenience` (keychain store via ctypes, file store, a fake store for tests, the selection), `orch keys custody` and `orch keys forget`, `orch.click` (signer process: `prepare`/`view`/`sign`/`cancel`/`status`, approval records, limits, log), the audit-hook rules; the dashboard's sign panel, card line and HTTP headers are the dashboard stream's | the flag off refuses; each readers reason refuses at prepare, at binding and at submit; `member.add` refused with convenience events in the history, allowed after switching off when there are none; the HTTP-side client never imports custody or the keychain (import graph); the host audit hook refuses person-key opens, `Security.framework` and the signer imports; tests use a throwaway keychain or a fake store, never the login keychain; a View's `lines` equal the terminal review; `view`/`sign` refuse a request the signer did not prepare; `sign` refuses another `view_digest`, a changed host answer, an expired approval, a missing word, a changed custody `gen`, and a click within 1 s; a `checks[].at` change keeps the digest; a lost submit answer is retried with the identical events and idem, and a repeated `sign` returns the cached result; refusals do not cancel, `cancel` does; `grant` and unknown ops refused; event-signature budgets; conversion keeps the device id and public key and resumes after an interruption; downgrade needs `--in-place`; `auth: "convenience"` round-trips through the model and an old schema refuses it; no key byte in any log, frame or error | Opus |
| **H9 Touch ID signer** (D-P2-22) | Swift helper, `secure-enclave` backend, native header and review, dashboard integration | recomputes hashes; refuses non-person labels; F1 vector interop; cancellation (window closed or Touch ID refused) submits nothing and leaves the request `prepared`; same fingerprint as the dashboard | Opus |
| **H10 Worker and jobs** | `orch worker`, `job.*`, leases, containment, addon runner, `tree.check` | the addon cannot read the WSK; a double-forked escapee dies within 30 s while a sibling job completes; a reparented child in a live job's group survives the sweep (F14); a repo with `filter.*.clean`, `core.fsmonitor` or `core.hooksPath` spawns no process in `tree.check` (F2); a second process under the worker UID is refused while the worker is connected, and a `job.done` with another connection's lease is refused (F3); lease expiry and retry at most twice; cancellation through heartbeat; a job that kills the worker and connects as worker class during the restart throttle is refused, and the restarted worker gets the role (N1); `job.done` replayed after a worker reconnect and after a host restart answers `{}` when lease and canonical digest match the stored completion, and `worker.lease` otherwise (including a different error message); a `void` lease is refused; a worker-UID connection to a workspace socket is refused; jobs of two workspaces are served round-robin over the one worker connection; a FIFO at a tracked path counts as changed without blocking, and a symlink is compared by its target (N3); a tree dirtied between prepare and `human.get` is refused at `human.get` (N3) | Opus |

- **Order:** H0, then H1a → H1b → H2a → H2b → H3a → H3b1 → H3b2 → H3b3 → H3c → H4a1 → H4a2 → H4b → H5a → H5b ∥
  H6a → H6b → H7. H6c (v1.5 listings) follows H6b and is not on the cutover path. H9 follows H5b. H10 runs in parallel from H4a1.
- **Phase 2 addons (§12.13):** H11a Runner core, H11b Terminals, H11c Start agent and runner grant, H11d Worktrees,
  H11e Records, H11f Quick tasks; order H11a → H11b ∥ H11d → H11c, H11e may start first, H11f any time; H6c is a
  second track. All Opus-reviewed except H11f (Codex).
- **Owner re-plan, 12 Oct 2026** (phases: orch-v2.md §13 note): phase 1 = core + dashboard in tier A, with H5c
  (click-to-sign) right after H6b and H4a2, then H4b, H3b2, H10's `tree.check`, H7. H9 and tier B (H3c's agent users)
  move to phase 3. orch-52 records the new group order and cutover gate.
- **Cutover gate (D44):** H7, H9 and H10. **Phase-1 gate (owner re-plan, 12 Oct 2026; replaces this gate for phase 1):**
  H4b, H5c, H3b2, H10's `tree.check` and H7, with click-to-sign on only for workspaces that meet §0.8.6 item 1 and
  only after the item 4 readers are released (the relay deployed). H9 and H3c join the gate again in phase 3.
- **Phase-1 gate state (H7, 12 Oct 2026):**
  - [x] H4b thin CLI (#409), H5c click-to-sign custody (#407), H3b2 client receipts (#412): merged.
  - [x] H7: host mode on for registered workspaces, the per-user service, `orch host status`, golden parity (agent and
    person ops, `wait`) and the cross-version fixtures (this group's PR).
  - [x] H10's `tree.check` (#413): merged; `verdict` and `show` on a linked repo match direct mode in host mode
    (`tests/host/test_parity_pending.py`, its xfail removed).
  - [x] The host serves `diff` (#417): merged; `diff` on a registered workspace equals git
    (`tests/host/test_parity_pending.py`, its xfail removed).
  - [ ] Click-to-sign switched on for a workspace only once the §0.8.6 item 4 readers are released (the relay is not
    deployed yet); the flag stays off by default.
  - [ ] T2 in both modes: the golden suites run in both modes in every full run; the orch-dev-kit e2e scenarios
    against a registered workspace are still to do.
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
8. **v1.5 (from the dashboard stream's M2 gap table and its comment on #378; not in v1.4).** *Answered by the v1.5 draft
   (§0.8.5, changelog §0.13); only `via: factory_charter` stays open, waiting for F1 to store it (D61, D62).* The list as
   asked:
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
   - **`host_pinned` base** (built in H6c, §0.8) (§10 entry 6, hole 4): the host keeps the first-seen merge base per (ticket uid, repo
     identity) in its state dir (0600, never moved forward automatically) and reports `base_moved`. P3 replaces
     first observation with a base pinned at claim.
   - **`since:"my_approval"`** (holes 1, 2): only the caller's own code-gate approve decisions, in the dispatcher
     of the served `diff` (`orch.host.diff`, registered without it); the default view stays the full change.
   - **A `declared` base is never the approval screen's default** (hole 5): a client rule, noted in §0.8.
   - **SHA-1 collision detection:** the reader checks SHA-1 ids with plain SHA-1, not git's SHA-1DC, so a
     SHAttered-style colliding object would be accepted. Accepted risk at today's cost; sha256 repos are unaffected.
     Add the known collision-disturbance check, or prefer sha256 repos.
   - **Rename detection** (§0.8 keeps `R` and `old_path` for a later version).
10. **Phase-3 follow-ups (tier B).**
   - **Verify trust "own" becomes "same grant"** (#420 review): the rule of §0.8 "Trust of the command" keys "own" on the
     person, which is sound only while all of a person's agents share one UID; tier B (#287) narrows it to the author's
     grant (or agent UID class).
   - **`diff`'s linked-repo check becomes a confidentiality boundary.** In tier B the check that `repo` is in the
     ticket's `links.repos` must be a real boundary (commit provenance, or per-person repo visibility in
     `settings.repos`), because agent UIDs then cannot read the disk themselves. In phase 1 it is accepted (§7 v1.5).
11. **Tier B for the phase-2 addons (§12).**
   - **Runner:** one per agent UID as a system service (`UserName=`/`User=`), installed by the H3c helper; runner socket
     group `orch-runner`; the term socket directory 0750 with a group holding the person, socket 0660.
   - **Shells from the dashboard run as the agent UID** ("this terminal runs as orch-agent-…; it cannot read your keys");
     no "shell as me" from the dashboard. This is #307's original "terminals behind the agent UID".
   - **Columns become real:** an agent-UID process is class `agent`, never P, so `term.*`, `agent.start` and
     `grant.deposit` are refused to it. Owners may need to see other persons' runs on a shared host.
   - **Runner grants** per (workspace, person, agent UID); `grant.deposit` names the runner UID; a UID-restricted grant
     (item 4) becomes useful; "own" means the same grant (item 10).
   - **Grant signing:** H9 or the terminal; click-to-sign ends after migration (§0.8.6).
   - **Environment:** agent UIDs get their own home and credentials; nothing of the person's (`SSH_AUTH_SOCK`, API keys)
     is carried by default.
   - **Resource limits and an orphan sweep per agent UID** become possible and are reconsidered.
   - **Worktrees** are owned by the agent UID and must be group-readable by the worker UID for `tree.check` (runner
     umask 002, directory group `orch-<ws>`).
   - **Terminal rendering** moves to a separate restricted origin (deferred from D-P2-30).
   - **Records** stay person-side (on a Mac the person UID is the host UID); the store files are host-owned and
     group-readable.
12. **Person `new` with labels in one signed request** (quick add by click, §12.9): extend `EMITS_V1["new"]` for
    persons to `ticket.created` plus one `ticket.updated` (labels, priority, size), reviewed in H5a's style; until then
    the click flow signs twice.

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


**7. Click-to-sign custody (D-P2-23, H5c, 12 Oct 2026).** The owner decided the goal (one-click signing in phase 1, no
presence, the risk shown, a per-workspace flag with a readers condition). The design challenge (Codex: adopt with
amendments; Opus: pass with required changes R1–R13) was adopted in full by orch-52; the choices, each with its
alternative:

- **Signer process, not a library in the HTTP server.** A `python -I -m orch.click` child over a socketpair holds the
  key. *Alternative:* the dashboard server calls the custody library itself. Rejected for the owner's "factor stays out
  of the dashboard server". *Stated honestly (Codex 1, Opus R1):* this protects against passive disclosure only; the
  dashboard is trusted with signing authority, which the card text now says.
- **Eligibility by a signer-local prepare registry** (Codex 8). *Alternative (the first draft):* the host-reported
  `origin.pid` equals the signer's parent. Rejected: a PID does not identify a process lifetime, and the host reports it.
- **Bind the full signing inputs, sign the stored ones** (Codex 6). *Alternative:* re-derive the View and compare its
  digest. Kept only for the click itself; freshness (`checks[].at`) is outside the digest (Opus R3).
- **Bound at prepare, no hand-off code on a click request.** *Alternative:* a hand-off request bound at `view`. Rejected:
  the owner's condition is checked at prepare with the signer's auth, and a `view` that binds before its checks changes
  the custody label of a foreign request (Opus R4). The terminal path is a separate hand-off request.
- **Retries resend the identical signed events and idem** (Codex 7, Opus R7). *Alternative:* a deterministic idem and a
  re-sign. Rejected: ECDSA signatures are randomized, so a re-sign is an `idem.conflict`.
- **The traditional login keychain on macOS** (Codex 3, Opus R2), with no data-protection claims. *Alternative:* the
  data-protection keychain with `ThisDeviceOnly`. Needs a signed helper with entitlements; phase 3's H9 is that helper.
- **One `auth` value `convenience`** for keychain and file. *Alternatives:* `keychain` and `file` (store names, not
  guarantees; `file` collides with the file tier), or reusing `passphrase` (would hide the missing factor).
- **Same device on conversion; downgrade in place only with `--in-place`** (Codex 4, Opus R10). *Alternative:* a new
  device for every custody change. That is `--rotate`, designed for when device ops exist.
- **Click for daily ops; a typed word for member, addon-grant/purge, policy and repo-settings ops; `grant` and unknown
  ops terminal-only** (Codex 10, Opus R8; v1.6 adds the deposit-only runner grant, click + word, §10 entry 8 and
  D-P2-27). *Alternatives:* click for everything (the grant secret would cross the HTTP
  process); a word for every op (the owner wants it easy).
- **Per-workspace readers condition and blocking new readers** (owner, orch-d6). *Alternative:* allow adding a reader
  after switching back to terminal signing. Rejected by the owner: the history already holds convenience events, which
  the new reader must parse.

**8. Phase-2 addons (D-P2-24..39, §12; 12 Oct 2026).** Draft by stream A, Codex challenge, Opus tie-break adopted in
full by orch-52, the dashboard stream's nine asks adopted, three owner decisions. The choices, each with its
alternative:

- **Terminal bytes direct between the dashboard server and the runner, under host-minted capabilities** (tie-break).
  *Alternatives:* relay through the host (the draft; rejected: terminal floods in the key-holder's failure domain and
  connection budget, no answer for a revoke under load); a direct channel without the host (rejected: no enforcement
  point).
- **Deposit-only click grants, P class plus the secret, `label:"runner"` only** (Codex found the draft's "deposit by
  secret needs no new trust" false: an agent could re-point the runner to its own grant). *Alternative:* terminal-only
  grants (Codex); rejected by the owner (decision 2).
- **One machine switch for dashboard execution** (Codex): agent start alone already makes a takeover code execution, so
  a terminals-only switch was no boundary. Owner decision 1.
- **Worktree registration generations bound into prepare, binding and precommit** (Codex): closes "prepare against
  worktree A, swap the registration".
- **Records from an empty index with a full-tree check, validated history, compare-and-swap index refresh** (Codex):
  correctness fixes, not hardening. No rebase (the draft, kept).
- **Per-URL private remote and an export check on every push path** (Codex); owner decision 3.
- **Read-only attach by default, explicit take, input ids and lease generations at the runner** (Codex and the
  dashboard): fixes double input and lease ping-pong.
- **No resource caps, no separate terminal origin in phase 2** (tie-break): tier-B topics; recorded in §9 item 11.

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
    `<state>/hosts/<ws>/outbox/` in the D-P2-17 custody list. Built in H12b (§11A.5 "As built in H12b"). Store-side
    coalescing supersedes unaccepted middle plain entries; the relay needs no consecutive `n`.
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

## 11A. Host bridge client (P3, contract v1.7 draft)

Status: **decided 13 Oct 2026 by orch-52 (see PR #435 comment)**; drafted 12 Oct 2026 and revised 13 Oct 2026 after an
independent Codex challenge and an Opus tie-break, recorded in 11A.13. Section 11 lists what the host owes the relay; this section is the design of the
core-side client that pays it: the code that makes a served workspace reachable through orch-relay. It is numbered 11A
so that the §12 references stay valid. Ground truth for the relay side is orch-relay `develop` @ `772d7b0`
(`docs/protocol-v2.md` §8, §12.2, §13, §19; `docs/p3-relay-design.md` §3, §9, §10 "G4 as built", P3-D1 to P3-D37;
P3-D37 merged in relay PR #62, `7c0406a`). Where the relay and core disagree, the text says so; §11A.12 collects those
points. "MUST" is normative for the implementation groups below.

### 11A.1 Scope, layout and groups

Package `orch.relay` (`plugins/orch-core/src/orch/relay/`), tests under `plugins/orch-core/tests/relay/`. The relay
corpus is vendored under `tests/relay/vectors/` (11A.10).

| Module | Contents |
|---|---|
| `relay/wire.py` | pure codecs: `auth_bytes`, the ORB2 header (108 bytes) and envelope parse/build, strict JSON bodies (`canon.jcs`, depth ≤ 32), staging page and manifest builders with an exact byte counter, per-route size caps; no I/O, no keys |
| `relay/sign.py` | `RelaySigner`: the only holder of the WSK `BackendSigner` in `orch.relay`; typed, purpose-specific signing methods (11A.2) |
| `relay/http.py` | the stdlib transport (11A.7): one `RelayConn` per thread, total deadlines |
| `relay/retry.py` | R1: the refusal classes, `Retry-After`, backoff and quota rules of 11A.7 |
| `relay/session.py` | R1: challenge, login, logout, single-flight re-login, `host/hold` |
| `relay/card.py` | R1: the proto §7.1 card checks in the relay's order, its update rules, the sealed part |
| `relay/directory.py` | R1: `PUT card`, `PUT genesis` and the `GET card`/`members`/`checkpoint` readers that verify what they return |
| `relay/link.py` | R1: the first-link sequence (11A.5) |
| `relay/bridge.py` | R2: the poll thread, the responder thread, the rid store and its state machine (11A.4). As built (15 Oct 2026, J1): the loop and the §8.6 checks in `bridge.py`, the rid store in `rids.py`, the closed tables and the sealed framing in `framing.py`, `BridgeKeys`/`R6View` in `keys.py` |
| `relay/submit.py` | R2: the durable remote-submission adapter (device-signed events into the store, recovery) |
| `relay/publish.py` | R3: the publisher thread, staging, catch-up planning, final `PUT checkpoint`, publication history |
| `relay/unpublished.py` | R4: the P3-D31 consumer (on the aux thread) |
| `relay/state.py` | the files of 11A.3. As built in R1: a stdlib private directory (0700, files 0600, `O_NOFOLLOW` against a directory fd, temp + fsync + rename + directory fsync), the last-signed ledger and the write-once first-link files; `orch.host` is import-banned in `orch.relay`, so R5 may swap it for the host custody helpers behind the same interfaces |
| `relay/worker.py` | R5: `RelayWorker`, started and stopped by `WorkspaceService` |
| `relay/r6/` | R6 (11A.15): `keys.py` (WK, WXK, CK custody, `EpochKeySource`), `members.py` (membership publication, `R6View`), `pair.py` (`R6Pairing`, the offer state machine), `card.py` (WXK rotation, card keys) |

Groups (each one PR into `develop`, first commit its failing vector or test, merged on green CI):

| Group | Consumes | Produces | Review |
|---|---|---|---|
| **R1 session and directory client** | `link.json`, `RelaySigner` (11A.2), `relay_auth` vectors, `session_*`/`host_hold`/`card_*` fixtures | a logged-in `Session` (token in memory), the holder generation, the first-link sequence (11A.5), `GET card`/`GET checkpoint`/`GET members` readers that verify what they return | Opus (auth, crypto), orch-52 |
| **R2 bridge loop** | `Session`, the epoch keys through `BridgeKeys` (11A.2), R6's member list and certificates (read-only), the relay entry (11A.4) | the rid store and submission journal, sealed and signed response chunks, the cursor `after`, `already_done`/`rid_conflict`/`stale_epoch` answers | Opus, orch-52 |
| **R3 checkpoint publisher** | the outbox (§11, written by the store), `.state/checkpoints/`, the logs, `HostPins`, `Store.mint_catchup` | staged sets, final publications including restores, the publication history, the startup classification and `chain.diverged` handling | Opus, orch-52 |
| **R4 unpublished consumer** | `Session`, `notices.unpublished`, `Store.note_forwarded_revocation` | verified evidence held by the store, the ack cursor, a `needs` item for the owner | Opus, orch-52 |
| **R5 integration hook** | R1 to R4 | `RelayWorker` lifecycle in `WorkspaceService`, column R and the relay entry (11A.4), `host.status.relay`, the reader reservation (11A.6) | Opus, orch-52 |

There is no `relay/client.py`. The owner's relay account (`POST /v2/accounts`) and device registration
(`PUT /v2/devices`) stay with the owner client (they need a device or person key the host does not hold; R6/H12c).

**Not in R1 to R5:** membership publication (§11 H1 to H8, `PUT membership`, `card-keys`, WK custody), pairing
(proto §8.3/§8.4, cert requests, baselines) and the WXK card rotation. They are group **R6**. R2 reads their state (the
current epoch, `WK_e`, the accepted member list, device/person association, scopes, burned epochs, the reader
reservation for a pairing) through `BridgeKeys` and an `R6View` interface, and never writes it.

**R6 is a security dependency of R2, not a fixture.** R6's security contract (what `R6View` returns, how the member list
and its scopes are verified, burned epochs, pairing-attempt binding, the reader reservation of 11A.6) MUST be designed
and reviewed before R2 merges (11A.11 Q3). The contract is 11A.15 (revision 1 of 15 Oct 2026, after the Opus, Codex, orch-52 and phone reviews). R2 may merge tested against a fixture `R6View`, but `RelayWorker` does not
start the poll thread for a workspace until R6 is merged and its integration tests against the real relay pass.

Order: R1 → R6 design → (R2 ∥ R3 ∥ R4) → R5 → R6 implementation → first real device. (11A.15.14 puts R6a, the key
custody, before R5, because R1's first link needs the card's keys, and has R5's T2 create the owner account through a
test owner client; 11A.15.15 is the accelerated order to a first Simulator pairing.)

### 11A.2 Keys and signing

**Rule (an API rule, not isolation).** Bridge code receives no raw private key and no raw `WK_e`, and it signs only
through purpose-specific methods.
- The WSK is used only through `relay/sign.RelaySigner`, which wraps `orch.store.store.BackendSigner` over
  `custody.FileBackend` (`workspace` role). No other `orch.relay` module imports `BackendSigner`, `FileBackend` or
  `orch.custody` (an import test enforces it). Each method builds its label and bytes itself and validates the context
  first; there is no `sign(payload)`:

  | Method | Checks before signing | Label |
  |---|---|---|
  | `login(challenge)` | origin and workspace id from `link.json`, kind `workspace`, challenge 32 bytes | `sig_relay_auth` |
  | `response_chunk(header, body)` | ORB2 direction 2, the header's workspace id, an epoch `BridgeKeys` holds, the whole envelope ≤ 256 KiB (the relay's cap counts the raw envelope, `bridge.py` `envelope`) | `sig_bridge` |
  | `card(o)` | exact field set, workspace id, WSK public key = own, `relay_url` = the stored origin, `card_seq` > the last accepted one and ≥ the highest signed one; a different card at a signed `card_seq` is refused, the same one gets the recorded signature back (durable last-signed ledger written before the signature is returned; ECDSA signatures are randomised, so nothing is re-signed). The highest-signed rule is checked first: once a later `card_seq` was signed, an older card is refused even if its signature is in the ledger (round 6, orch-52 #5) | `sig_card_wsk` |
  | R6: `member_list`, `wk_grant`, `cert_request`, `pair_answer` (`wk_commit` is a hash, built by H12c) | 11A.15.7 | R6 labels |

  The `HOST_LABELS` role check (`custody/base.py`) stays as a backstop. It does not separate directions by itself:
  `sig_bridge` and `sig_relay_auth` are in both `HOST_LABELS` and `DEVICE_KEY_LABELS`; the typed methods do.
- `RelaySigner` holds the key in a closure, not in an attribute, and exposes only its purpose-specific methods. An AST
  test over `orch.relay` bans private names on other objects, dynamic attribute access (`getattr`, `vars`, `globals`,
  `eval`, `importlib`, `sys.modules`, introspection attributes) and every `orch.crypto` signing entry point under any
  alias, frame and interpreter reach (`logging.currentframe`, `traceback.walk_stack`, `f_builtins`), and assignment to
  an attribute of an imported module or class. Relative imports are resolved with their level against the module's own
  package (`from ..crypto import sign` is `orch.crypto.sign`), and every `.py` under `orch/relay/` is scanned,
  subpackages included (round 6, orch-52 #4). This is defence in depth, not isolation: same-process code can always
  traverse objects, and reassigning a module function (say `wire.auth_bytes`) before calling the public `login` signs
  attacker bytes. Known gaps of the AST rules (round 7, R6-6; same class, not closed): attribute chains through an
  allowed import are not matched against the banned modules (`import orch; orch.host.custody.x`, `import orch as o;
  o.custody.x`), and neither are value aliases (`c = crypto; c.sign(...)`, `for m in [crypto]: m.sign`, a module
  passed as an argument). The real boundary is the reserved out-of-process `bridge` class under a distinct UID
  (11A.11 Q1).
- Epoch keys are used only through `BridgeKeys`, which opens a request and seals a response and returns only
  plaintext or envelopes.
- Device and person signatures are made only on the person path: the click signer subprocess (`orch.click`) or the
  terminal signer (`orch sign`, `host/signer.py`). The host holds no device key (D-P2-15).

**What this is not.** It is not key isolation. `BackendSigner` keeps its backend as a Python attribute and
`FileBackend._load` (`custody/file.py`) returns the private key, so same-process code can reach the WSK; the key file is
readable by the host UID, so a same-UID agent can read it without any bridge bug (the §7 exposure, unchanged by the
relay). Consequences: **code execution in the bridge (for example through a parser bug on relay bytes) is a WSK
compromise**; a separate process under the same UID would be fault separation, not a security boundary. A boundary needs
a distinct UID and a restricted signing service (the reserved out-of-process `bridge` class, 11A.11 Q1).

| Object | Label | Key | Call | Status |
|---|---|---|---|---|
| relay login (`POST /v2/session/login`, kind `workspace`, id = workspace id) | `orch/v2/sig/relay-auth\|` over `auth_bytes(origin, challenge, "workspace", ws)` | WSK | `RelaySigner.login` | signer **exists**; `RelaySigner` and `relay/wire.auth_bytes` **must be added** (byte-equal to orch-relay `proto.auth_bytes`, pinned by the `relay_auth` vectors) |
| response chunk (ORB2 direction 2) | `orch/v2/sig/bridge\|` over `header \|\| body` | WSK | `RelaySigner.response_chunk` inside `BridgeKeys.seal_response` | signer **exists**; ORB2 seal and open (`K_bridge(e)`, `K_msg`, AES-GCM with the header as AAD) **must be added** (the `crypto.hkdf` and `aes_gcm_*` primitives exist) |
| device request (direction 1) | `orch/v2/sig/bridge\|` | the device's `dk_sig` (verified, not made) | `crypto.verify` against the certificate's `dk_sig_pub` | primitive **exists**; check order of proto §8.6 **must be added** |
| workspace checkpoint | `orch/v2/sig/checkpoint\|` | WSK | written by the store (`store/checkpoints.py:sign_object`); R3 publishes stored bytes and never signs | **exists** |
| catch-up checkpoints (11A.5, P3-D37) | same | WSK | `Store.mint_catchup` → `Checkpoints.sign_prefix`, under the store lock | **built in H12b** (`write_workspace` cannot: it refuses a prefix behind its baseline and keeps later ticket entries) |
| card | `orch/v2/sig/card-wsk\|` | WSK | `RelaySigner.card` (first link) | **must be added** |
| member list, WK grants, cert requests | `sig_member_list`, `sig_wk_grant`, `sig_cert_request` | WSK | R6 builders; `wk_commit` (label `h_wk_commit` exists, no function) | **must be added** (R6) |
| genesis pin (`PUT genesis`, P3-D18) | `orch/v2/sig/genesis-pin\|` | an owner device's `dk_sig` | person path: `human.prepare` of a new non-event material kind `relay.genesis_pin`, `host/signer.check` and `render` for it, signed by the click or terminal signer | **must be added**; `sig_genesis_pin` is in **no** role list today, so it must be added to `DEVICE_KEY_LABELS` (`custody/base.py`) |
| restore line (owner policy, P3-D23/D24) | `sig_ws_event` or `sig_ticket_event` (person), `sig_host_event` (host) | owner `dk_sig`, then WSK | person path through `human.*`; the store's transition machinery exists (`store/pins.py` `pending-transitions/`, `transitions/`) | the person op (prepare, review, submit of `restore`) is **not** in `ops/commands`: **must be added** |
| reader declaration (H12d, §0.8.6 item 9.3) | `orch/v2/sig/reader-decl\|` (`sig_reader_decl`, an F1 label in `orch.canon.LABELS`) | the declaring device's `dk_sig` | made by the reader (a phone, or `orch keys reader declare`, H12d-2); the host only verifies (`orch.identity.reader.verify_decl`) | label in `DEVICE_KEY_LABELS` **exists** (H12d-1); the signing path **must be added** (H12d-2) |
| workspace delegation | `orch/v2/sig/ws-delegation\|` | owner PK | made at `init` (`identity/certs.make_delegation`); R1 and R3 only carry the stored one | **exists** |
| device certificate, revocation, cert challenge, cert rejection | PK labels | the person's PK | owner clients only; the host never makes them and only carries or verifies them (`identity/certs.check_cert`, `verify_cert`, `verify_revocation`, `check_delegation`) | **exists** (verify side) |

**Review binding of the new person-path kinds.** A custody label alone is not enough.
- `relay.genesis_pin`: the signer renders, and `host/signer.check` binds, the workspace id, the pinned genesis hash,
  the WSK public key (fingerprint) and the relay origin. The signer recomputes the signed bytes from those fields; it
  never signs bytes the host supplies.
- `restore`: the review shows and binds the source checkpoint (`n`, hash), per log the retained `seq` and the
  abandoned range, and every carried revocation pair.
- `check` refuses an unknown material kind, a material kind whose label is not the one being signed (cross-kind
  substitution: a genesis review used for a restore and the reverse), and any extra field.
- `review_digest` (`host/signer.py`) is a consistency check of what the signer rendered, not proof of human intent: a
  compromised phone signs as its person within that person's authority.

**Origin.** `auth_bytes` covers `lp16(origin)`, and the relay compares its configured `ORCH_RELAY_ORIGIN` as a plain
ASCII string (`directory.py` `login` and `auth_bytes`). The host therefore stores the origin **verbatim** in `link.json`
(`scheme://host[:port]`, no path, no trailing slash, lower-case scheme and host, ASCII), signs that exact string and
connects to that same origin. It never derives the origin from a response, a redirect or DNS. `https` is required, except
for `http://127.0.0.1` and `http://localhost` (tests only). A wrong origin shows up only as 401 `bad_signature` on every
login. After 3 in a row the worker stops and alarms with `origin_mismatch_suspected`, naming the stored origin.
Origin binding separates differently configured relays only: instances that share an origin and challenge state are one
authentication domain (`login` consumes the challenge, `directory.py`).

### 11A.3 State

Everything lives under `<state>/hosts/<ws>/relay/` (0700, host UID; added to the D-P2-17 custody list and to
`check_workspace_files`). JSON files are written with `host/custody.write_private` (0600, `O_NOFOLLOW`, temp + fsync +
rename + dir fsync) and read with `read_json`.

**Opening `rids.sqlite`** (SQLite opens by path, so this is a checked open, not an fd-bound one):
1. `ensure_private_dir` and `check_tree` on `relay/`;
2. for `rids.sqlite`, `rids.sqlite-wal` and `rids.sqlite-shm`: `os.open(O_RDWR | O_CREAT | O_NOFOLLOW, 0o600)`, then
   `fstat`: a regular file, owned by the host UID, mode 0600, `nlink == 1`; the fds stay open;
3. `sqlite3.connect("file:<path>?mode=rw", uri=True)`, `journal_mode=WAL`, `synchronous=FULL`;
4. `stat` each path again and compare `(st_dev, st_ino)` with the held fds; any mismatch refuses the start (alarm
   `relay_state_unsafe`). Only the host UID can write `relay/`, so the remaining race is a same-UID one (§7).

| File | Contents | Secret | Crash rule |
|---|---|---|---|
| `link.json` | `{v:1, workspace_id, origin, ca_file?, proxy?, max_publication_bytes, state:"linking"\|"linked", linked_at, card_seq}` | no | written (state `linking`) before any network write; it is the reader reservation of 11A.6 |
| `unlinked-<linked_at>.json` | the former `link.json`, renamed at unlink | no | never deleted: the reservation is permanent (11A.6) |
| `holder.json` | `{last_holder, gen, instance_id, pending}`: whether this state directory was the last holder, its `gen`, the instance id it was written under, and the expected `gen` of a takeover probe in flight (DECIDED by orch-52, 13 Oct 2026) | no | written durably: `pending` before the probe, the new `gen` with `pending` cleared before any other write; the restart rule is 11A.5 "Restart takeover" |
| `instance.json` | `{instance_id, dev, ino}`: the state directory's instance id (random 128 bits, created once with the state) bound to the `st_ino` of the locked directory fd (decided by orch-52, 13 Oct 2026; `dev` is recorded for information only since 14 Oct, below). `cp -r` or rsync copies the id but not the inode, and a restore through a backup tool recreates the directory: both show as a mismatch, which alarms `relay_takeover_refused`. A process restart or a rename within one filesystem keeps the inode; a move across filesystems (or a tool that recreates the directory) needs the operator path. `st_dev` is not compared: it is assigned per mount and can change across a reboot or remount (APFS volume order, btrfs subvolumes, LVM, an external volume), so a changed `dev` with the same inode is logged at info level, not alarmed. Not detected (residuals, same class, inherent to local state): a block-level clone (VM image, `asr`/`dd`, a block-level disk clone booted on a second machine) keeps the inode and every byte, a writable filesystem snapshot or clone (btrfs/ZFS snapshot, an `asr`/`dd` clone attached as a second volume on the SAME machine) keeps the inode number by design and gets a new `st_dev`; since the device check was dropped (14 Oct 2026, review round 5 P1) it passes and renews the live `gen`. Read-only snapshots (APFS, Time Machine) fail with EROFS before any network call, and a copy onto another volume can land on the same inode number by chance; no local check can prove a state directory is unique, and the relay's holder `gen` is the backstop. Never run a clone while the original is live. Only the operator path rebinds: `regenerate_instance_id(state)` (new id and current `dev`/`ino`), called by the restore procedure and by `orch host relay takeover` on a directory bound elsewhere. A round-3 `instance.json` (`{instance_id}` only, never released) is refused with a `StateError` naming `orch host relay takeover`, which regenerates it | no | claimed once (`os.link`) at creation; rebinding is an atomic write |
| `sign-ledger.json` | the last-signed ledger: `{kind: {key: {digest, sig}}}` (cards by `card_seq`) | no | written durably before a signature is returned |
| `link-card.json`, `link-genesis.json` | the card and the genesis pin (with its certificate and genesis hash) of the first link, write-once (claimed with `os.link`, read back if another writer won) | no | persisted before their first PUT; a resume re-sends these bytes |

**The state directory's ancestors (round 6, orch-52 #2).** `StateDir` runs the custody tree check itself (`orch.host`
is import-banned in `orch.relay`, so it mirrors `check_tree`): every ancestor is opened from `/` one component at a
time with `O_NOFOLLOW | O_DIRECTORY` (a symlinked ancestor is refused), and each must be owned by root or the host UID
and not writable by group or other unless it is a root-owned sticky directory (`/tmp`). The directory itself is then
created (0700) and opened relative to its checked parent, never by path, and must be a 0700 directory of the host UID.
Gap, as documented for the relay state elsewhere: ACLs are not inspected here (the host's ACL check needs `ctypes`,
which the import ban refuses); R5 can swap in `host/custody.check_tree` behind the same interface. **R5 MUST pass a
resolved state path** from trusted config, never one built from `$TMPDIR` (round 7, R6-4): on macOS `/tmp`, `/var`
(so `/var/folders/...`) and the usual `$TMPDIR` sit under symlinks (`/tmp -> private/tmp`, `/var -> private/var`), so
`StateDir("/tmp/x")` is refused ("a symlink or not a directory ...; pass the resolved path") before anything is
created, while `/private/tmp/x` is accepted. The production default under `/Users/<u>` has no symlinked ancestor.
`test_macos_tmp_and_tmpdir_prefixes_are_refused` documents it.

One live instance per state directory: `StateDir` holds an exclusive non-blocking `flock` on the directory for its
lifetime (close-on-exec); a second instance, in this process or another, is refused "in use". All I/O goes through the
locked directory fd, never the path again (a directory renamed aside keeps receiving its owner's writes), and a forked
child refuses every operation (a fork inherits the lock; close-on-exec covers only exec). An `os.register_at_fork`
child hook closes the child's copy of the lock fd with a plain `close`, never `LOCK_UN` (the `flock` belongs to the
open file description the parent shares, so an unlock would release the parent's lock), and marks the instance
unusable; a long-lived forked child therefore does not keep the directory locked after the parent closes it (except in a narrow race: a fork by another thread inside `close()` or `__init__`, between the fd change and the registry update, leaves the child holding the lock until it exits: availability only, review round 5 P2), and its
`close()` does nothing (14 Oct 2026, review round 4 N3). A ledger restored together with an older `link.json` from the
same backup cannot be detected (inherent to any local state), nor can a block-level clone of the state directory (see
`instance.json`).
| `rids.sqlite` | `rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem)`, `submission(device, rid, log, events)`, `chunk(device, rid, idx, epoch, bytes, state)`, `window(device, high, bitmap)`, `cursor(after)`, `meta(start_ms)` | chunk bytes are ciphertext; `submission.events` are device-signed events (below) | the state machine of 11A.4 |
| `publish.json` | `{entry, source_hash, target_hash, pages:[{i, digest, count, put}], manifest_put, cap}` | no | staging resumes from it: pages are immutable and an identical PUT is a free 200 |
| `published.jsonl`, `published/` | append-only: one line `{n, hash, kind, at_ms}` per accepted publication; `published/restore-<n>.json` (the full final body of every accepted restore, kept forever) and `published/last.json` (the last accepted plain checkpoint) | no | appended after a 200 and before the outbox entry is deleted; used to rebuild after a relay rollback (11A.5) |
| `unpublished.json` | `{cursor, last_full_read_ms}` | no | the cursor is acked only after every item up to it is settled by the store (11A.9) |
| `alarms.json` | the H8 counters and open alarms (`host.status`) | no | rewritten atomically |

**Read through the host store, never copied.**
- Per-pair revocation and binding evidence (core #397, P3-D30/D31) is `HostPins` (`store/pins.py`,
  `revocations.jsonl`, keyed by `(device_id, person_id)`). R4 writes only through `Store.note_forwarded_revocation`
  (11A.9), on the worker thread under the store lock (`HostPins` has no lock of its own); R2 reads only through the
  store. `HostPins.pair()` is the key of a stored record, **not** an association lookup: the association of a device
  with a person is model state, `store.state.device_person(device)` (as `Store._carries` uses it).
- Accepted checkpoints and the `n` high-water mark (`HostPins.accept_checkpoint`, `high_n`) and the outbox
  (`hosts/<ws>/outbox/`, §11) belong to the store; R3 reads and deletes outbox entries and mints catch-up entries only
  through the store's API.
- Membership and `WK_e` (`hosts/<ws>/membership/`, `keys/`) belong to R6 (layout and crash rules: 11A.15.3).

**Never persisted:** session tokens (memory only: 15 min, a login is cheap, and no bearer secret lands on disk);
challenges; response plaintext; request plaintext **except** the one exception below; derived `K_bridge`/`K_msg`;
`Authorization` headers; raw relay response bodies other than the verified objects R1 keeps (the held checkpoint, the
accepted member list).

**The exception (durable execution).** For an admitted write, the device-signed events it carries are persisted in
`submission.events` (0600, host UID) in the admission transaction, so a crash can roll the write forward without the
envelope. They are exactly what the log will hold and what every member can read once appended. They are deleted when
the rid is answered (applied or refused). No other request field, no read request and no response plaintext is ever
written.

**Retention (wall clock, 11A.8).**
- A rid record lives 24 h after `first_ms`, then becomes a tombstone `{device, rid, req_digest}` for 7 days (the
  outcome-store rule of §0.7).
- Chunk bytes are dropped at `first_ms + 960 s`: the relay refuses any re-store after 900 s (410 `expired`).
- The seq window row of a device is **never** dropped with its rids: a replay whose rid record has expired still meets
  a used `seq` (`stale_sequence`).

### 11A.4 Host integration: the relay entry, column R, exactly-once

**The hook.** `WorkspaceService.open` (on its worker thread) ends with `relay.worker.start(service)` when `link.json`
exists. `WorkspaceService.close` calls `stop()` first (11A.7 shutdown). Nothing else in the host knows about the relay.
- `RelayWorker` owns four daemon threads per workspace, each with its own connection: `orch-relay-poll-<id8>`,
  `-resp-`, `-pub-` (R3) and `-aux-` (lease renewal and R4, 11A.5). None of them is the workspace's worker thread
  (`orch-ws-<id8>`).
- A relay thread reaches the store only through `RelayWorker.submit(job)`, a wrapper of `service.submit` that refuses
  once `stop()` has begun. The store lock is never held across networking.

**Not the socket dispatcher.** The socket path (`Dispatcher._early`, `_column`, `_work` in `host/workspace.py`) is built
on a kernel UID: `Call.authorize()` recomputes `call.principal` as `principal(peer.uid, verified)`, and
`columns.resolve()` has no relay branch. A relay request therefore never enters it. It enters a new
`WorkspaceService.relay_call(identity, op, args, opts)` that builds a `Call` and runs **the same `OpBackend`** ops; the
column, the principal and the viewer come from the relay identity alone.

**Host code changes required (R5, reviewed with R2):**

| Where | Change |
|---|---|
| `host/relayid.py` (new) | `RelayIdentity`, frozen: `workspace_id, device_id, person_id, cert_digest, epoch, list_hash` (the accepted member list the request was checked against) |
| `host/peer.py` `Peer.kind` | gains `"relay"`; a relay `Peer` has `uid = -1` (never a kernel UID) and `person = person_id` |
| `host/columns.py` | `Column.name` gains `"R"` (viewer `"person"`); new `resolve_relay(ok: bool) -> Column` (R, or N with every op refused); `resolve()` raises for `peer == "relay"` and for any peer class it does not list (fail closed); `_MATRIX["R"] = {"read", "device_signed"}`; `OpKind` gains `"device_signed"` (no other column has it) |
| `host/workspace.py` `Call` | gains `relay: RelayIdentity \| None`; `authorize()` with `relay` set recomputes through `relay_columns()` and keeps `principal = "r:<device_id>"`; it never calls `principal(self.peer.uid, …)` for a relay call |
| `host/workspace.py` `principal()`, `principal_uid()` | `principal()` raises for a relay peer; `principal_uid("r:…")` stays `None` (the held-request charge loop at open logs and skips, which cannot occur: R has no `human.prepare`) |
| `host/outcomes.py` | `principal_of()` raises for column R; `NO_PARTIAL` gains `relay.submit`; a reconciler hook `relay_fields` (as `human.outcome_fields`) registered at open |
| `host/human.py` | the append loop of `finish()` is extracted as `append_signed(service, log, events, extras) -> (landed, error)` and shared by `finish()` and `relay/submit.py` |
| every `column.name` comparison (24 sites today in `host/{dispatch,sessions,runners,diff,visibility,workspace}.py`) | each site is listed in a table test, `test_column_r_sites`, stating R's answer; a new site fails the test until listed |
| `WorkspaceService.open` | ~~`relay.submit.recover(service)` after `human.recover`~~ amended H12e-2: recovery runs in R2's `Bridge.start` (honours the restore hold-off), not in `open` |

**The `rd` field (H12d).** A request's signed meta (inside the seal) carries `rd`, the digest of the device's current
declaration. On a replay or a byte-exact resend, `rd` is the one of the **first** send: the host matches a replay by its
rid record, never against the current declaration; only **new** requests are checked against the current one.

**Identity (proto §8.6 steps 1 to 4).** A bridge request has no kernel UID. Its identity comes only from the verified
request:
1. the header's `device` is in the current member list the relay accepted (`R6View`), at the request's epoch;
2. that device's certificate verifies under its person's PK (`identity/certs.verify_cert` at the host's clock), and
   the person is the list entry's `person_id`;
3. the person is a member of the F1 workspace state, `store.state.device_person(device) == person` (the exact
   association, never the device id alone), the device is neither removed nor revoked in the F1 state, and `HostPins`
   holds no revocation for that exact pair `(device_id, person_id)`;
4. the `dk_sig` signature over the envelope verifies.

Then `RelayIdentity` is built, `principal = "r:<device_id>"` and `viewer = <person_id>`. Inside the op's own job on the
worker thread, `relay_columns()` repeats steps 1 to 3 **and** checks the op against both the certificate's scopes and
the current member list's scopes for that device. A device removed, revoked, expired or reduced in scope between
receipt and run is refused there. Evidence for another pair with the same device id (a foreign self-certified pair)
does not refuse this pair and does not authorize anything.

| Class | `auth.grant` | Person mapping | Column | Viewer | Writes |
|---|---|---|---|---|---|
| relay (v1.7) | must be absent: an `auth` key in the sealed meta is `req.invalid`; a grant-shaped string anywhere is `grant.secret_in_args`; both before any column is resolved | the verified device's person, checked again per request | **R** | the device's person | only device-signed person events (below) |

| Ops | R |
|---|---|
| registry `read` ops except `doctor`, `instructions.*`; `ticket.query`, `ticket.get`, `needs`, `activity.list`, `diff`, `artifact.get`, `workspace.show`, `member.list`, `ticket.text`, `artifact.query`, `commit.list`, `workspace.settings` (P fields), `quick.list` | ✓, filtered for the viewer exactly as P (F6) |
| `whoami`, `outcome.get` | ✓ (own principal `r:<device>`) |
| `relay.submit` (internal, kind `device_signed`): F1 person events signed by the requesting device, under a closed event/scope table in `relay/submit.py` (`new_ticket` → `ticket.created`; answers → `question.answered`; …) | ✓: every signature verified against **this** device, `actor` the device's person, the scopes checked first (§8.6 step 11), then `HumanOps._judge` under the store lock. No `human.prepare`/`get`/`pending`/`cancel`. An event type outside the table is `forbidden_scope` |
| proto §8.7 `decision` (a detached `sig_decision` object, recorded by the host as F1 `evidence`, relay §13) | — until orch-52 defines its F1 carriage (11A.12); then a separate handler, with `decision_id` single-use forever across restores |
| registry `unattended` ops (`ask`, `log`, `artifact.add`) | — (`op.not_allowed`: an unsigned write attributed to a phone is not P1 parity; no fallback) |
| registry `agent` ops, `grant.*`, `session.*`, `blob.*`, `sub.*`, `doctor`, `records.*`, `run.*`, `term.*`, `worktree.*`, `agent.*`, job and control ops | — |

The relay request ops map to backend ops through one closed table in `relay/bridge.py`. An op outside it is answered
with a sealed `forbidden_scope`. Refusals use the §0.6 codes inside the sealed refusal meta.

**Idempotency layers (§0.7).** Every R write requires `idem` in the sealed meta, else a sealed `idem.required`. The
outcome key is `(ws, "r:<device>", idem)`: two devices using the same idem never collide. Three layers, in order:
1. **The rid store.** The same `(device, rid)` is admitted once. A finished answer is replayed as the exact stored chunk
   bytes; a rid not yet answered gets **no** answer (proto §8.6 step 5).
2. **The seq window.** A new rid with a used `seq` gets `stale_sequence`.
3. **The outcome store.** A new rid that carries an idem already stored gets the stored result with
   `duplicate: true`. This covers a device resend after a host-signed "not run" refusal.

**The rid state machine.** Every transition is one SQLite transaction; the store side uses the outcome store.

| State | Entered by | Holds | Recovery after a crash |
|---|---|---|---|
| `admitted` | T1: the seq window, the rid record (`req_digest = sha256(envelope)`, epoch, seq, principal, idem) and, for a write, `submission.events`, in one transaction, after steps 1 to 11 of §8.6 | nothing has run | a write: `relay.submit.recover` looks up `(principal, idem)` in the outcome store. None, or `pending` that reconciles to nothing landed: the job runs **from the submission row** (identity rechecked, no new seq admission, no ts window). `pending` with landed events: rolled forward through `relay_fields` (`append_signed`, never judged again). Terminal: the answer is built from it. A read: answered `already_done {status:"unknown"}`; the device re-issues it |
| `answered` | T2: the answer chunks (ciphertext, epoch, `state = stored`), the rid state, and the deletion of `submission.events`, in one transaction, before any POST | the exact answer bytes | nothing to do; posting resumes |
| `tombstone` | retention | `{device, rid, req_digest}` | none |

- An admitted rid is **never deleted** and never re-enters admission: deleting it while its `seq` stays consumed would
  turn the device's identical resend into `stale_sequence`.
- **Outcomes, not "no partial".** An R write ends `committed`, `refused` (nothing landed; the answer is the refusal) or
  `failed` naming the landed `seq`s (`append_signed` can refuse after the first event landed, as `human.finish` does).
  A `failed` write is never run again; its landed events stand. `partial` is not used.
- **Chunk states.** `stored` → `attempted` (committed before the first byte of the first POST of that chunk) →
  `posted` (202). **Any attempted POST, a timeout included, freezes the chunk's bytes:** that index is only ever
  re-POSTed with identical bytes. Recovery replays persisted chunks before anything else and never rebuilds an answer
  that has a chunk.
- **Start fence.** `meta.start_ms` is the wall clock at worker start. A request with `ts_ms < start_ms` that has no rid
  record is answered `already_done {status:"unknown"}` (recorded, not run): the device re-issues it under a new rid
  (a write keeps its idem). This fences replays of requests the host may have executed before a crash or a state
  restore (below). Cost: requests in flight across a restart, or from a device clock behind the host's for up to the
  300 s window, are re-issued once.

**rid_conflict.** The same `(device, rid)` with **other** bytes never runs (the phone re-sealed under the same rid after
an unsigned `stale_epoch` hint; orch-mobile Q14).
- Any chunk of the original answer `attempted`: re-POST those exact bytes. The phone keeps the original attempt.
- The original still `admitted` (not answered): no answer now; the original's answer goes out when it exists.
- The original `answered`, nothing attempted, and its epoch key still held: POST the original answer (it is now
  frozen).
- Otherwise: a sealed `LAST | REFUSAL rid_conflict` under the new copy's epoch at index 0. The original answer is then
  superseded; a write's result stays retrievable through `outcome.get(idem)`.

**Scope of exactly-once.** It holds against a hostile relay (drop, delay, reorder, replay, re-delivery) while the host's
state directory (`rids.sqlite`, the outcome store, the logs) is not rolled back. A restore of the whole state directory
from a backup taken before an execution loses the rid, seq, outcome and log evidence; the hostile relay cannot be the
witness. What then prevents a re-run is the request's own `ts_ms`: the start fence refuses every request stamped before
the restarted worker, and the ±300 s window refuses older ones. A host clock set back across a restore defeats both; the
operator procedure after any state restore is to keep the relay worker stopped (`orch host relay stop`, a control op added with R5) until the host
clock is past the backup's time + 1 500 s (600 s before §11A.14 added the 900 s event window).

**As built in H12a** (host side only; the relay modules `orch/relay/` are R1 to R5's). Judgement calls are marked
(J), each the fail-closed reading.
- `host/relayid.py`: `RelayIdentity(workspace_id, device_id, person_id, cert_digest, epoch, list_hash, rd=None)`,
  shape-checked at construction (ids, `sha256:` digests, `epoch` a non-negative int); `.principal` is `r:<device_id>`,
  `.peer()` the relay `Peer`. `rd` is the H12d hook (§0.8.6 item 9) and is unused in v1.7: its presence qualifies
  nothing.
- `host/peer.py`: `Peer.kind "relay"` requires `uid == -1` and a person; every other kind requires a kernel UID ≥ 0.
- `host/columns.py`: `resolve()` raises for any peer class but person, agent, worker and control; `resolve_relay(ok)`
  takes only a real bool and gives R (viewer `person`) or an N row whose new field `Column.error` refuses **every**
  op. `refusal(column, kind, op)` serves R only an op of the closed `RELAY_OPS` whose kind is in `_MATRIX["R"] =
  {"read", "device_signed"}`, and `device_signed` only for `relay.submit` (`DEVICE_SIGNED_OPS`): the kind alone never
  serves R. (J) `RELAY_OPS` is a static list (today's registry reads minus `doctor`, `instructions.*` and `wait`,
  plus the host reads of the table above, `whoami`, `outcome.get`, `relay.submit`); `wait` is left out because it
  parks on a socket session's bus; `host.status`, `sub.*` and `blob.*` are not in it although their kind is `read`.
  A test fails when a new registry read op appears, so R gets it only by a deliberate change.
- `host/workspace.py`: `Call.relay`; `authorize()` with a relay identity or a relay peer requires both, a peer whose
  person is the identity's, a recompute function (`relay_columns`, passed as `Call.columns`) and an op kind; the
  recompute must answer R with the identity's person as viewer, or the refuse-all row; the principal stays
  `r:<device_id>` and the grant `absent`. A socket call whose recompute answers R is refused. (Review round 1) a
  column R without a relay identity is refused before any early return; the identity's `workspace_id` must be the
  service's; the op kind is derived inside (`device_signed` for `relay.submit`, else `dispatch.op_kind`), and a
  caller's kind that differs, or an op with no kind, is refused. `principal(uid, …)` raises for a UID below 0.
  `WorkspaceService` gains `relay` (R5's worker), `relay_starter` (R5's start hook), `relay_lock`/`relay_cond`,
  `relay_stop()`, `relay_start()`. The worker contract is `stop()` plus `join(timeout) -> bool` (true when every
  relay thread ended). `relay_stop()` answers only after `stop()` and the bounded `join` (`relay_join_seconds`,
  120 s) returned (`stopped`, `stuck` or `none`); `relay_start()` waits for a stop in progress and refuses with
  `lock.busy` while one has not finished or a stuck worker's threads still run; `close()` stops the worker and
  waits for its threads (bounded by 120 s) before it queues the job that closes the store.
- `host/outcomes.py`: `relay.submit` is a log write (idem required, no dry run), in `NO_PARTIAL`;
  `OutcomeStore.relay_fields` is the reconciler hook for a pending `relay.submit` with landed events. (J) Until R5
  registers it such a record stays `pending`: never `partial`, never handed to the human roll-forward, never run
  again. `principal_of` raises for R, for any unknown column and for a UID below 0.
- `host/human.py`: `append_signed(service, log, events, extras, *, signed_by) -> (landed, error)` is the append loop
  of `finish()` (`signed_by` keyword-required: `None` on the person path). It also writes the durable
  `convenience_seen` mark (#441) under the store lock before the first convenience event, for every caller; a test asserts it is the only
  `store.append` in the module and that `finish` uses it. The one other append of a signed event in the host is the
  owner-signed genesis of `host.bootstrap` (`control.py`), on the control socket before the workspace is served.
  The relay path's only entry is `append_relay(service, log, events, extras, *, person_id, device_id)`, which always
  passes `signed_by`; a test refuses any use of `append_signed` under `orch/relay/`. (J) With `signed_by` every event
  must carry exactly that person actor and device, a `sig`, and no `auth:"convenience"`, checked before anything
  lands (`op.not_allowed`), and an id already in the log counts as landed only when its `sig` and `actor` are the
  submitted ones (else `op.not_allowed`, never a false `committed`); the store still verifies each signature. The
  closed event/scope table stays `relay/submit.py`'s (R2).
- Column sites: `test_column_r_sites` inventories, by AST, over `orch/host/**` and `orch/relay/**`, every comparison
  with a column-name literal, every read of `….column.name` or `getattr(….column, "name")`, local aliases of a column
  object or name and their comparisons, and every arm of a `match` on a column (70 at H12a round 1) and states R's answer for each. Code changes for R: `Viewer` reads R
  exactly as P (owner, manager, `needs {person}`); `Bus.principal_end` ends a subscription of any column other than
  P, G and N (it returned `None` before, a latent fail-open for an unknown column). (J) `diff` pins no base for R and
  `whoami` gives R no `runner` field.
- `host/convenience._relay_paired` is true for `membership/`, `relay/link.json` (a dangling link too), any
  `relay/unlinked-*.json`, or a `relay/` that cannot be listed.
- `host/relaylink.py`: `MAX_LINKED = 8`; `linked_workspaces(state)` counts every `hosts/*/relay/link.json` (state
  `linking` or `linked`; (J) registered or not, and an entry that cannot be inspected counts);
  the only writer of `link.json` is `store.relay_link(link, guard=lambda: relaylink.link_reservation(service))`
  (H12b), and the only rewrite `store.relay_mark_linked(expect)` (`linking` to `linked`). `link_reservation(service)`
  is that guard: the store lock (re-entrant), then a host-wide `flock` (`host/relay-link.lock`), then
  `refuse_reader_op(service, "relay.link", history=True)` (`sign.convenience_readers`: convenience history, the
  durable `convenience_seen` mark or the flag; no exception, 11A.11 Q4), then `host.limit` at 8 links; an existing
  link is the store's refusal (`relay.link_exists`), so the guard has no relink case. An integration test drives
  `relay_link` with this guard through all three refusals. The stop marker `relay/stopped.json` (0600) is written by `mark_stopped`, cleared by
  `clear_stopped`; `is_stopped` is true when the marker exists whatever its content, when `relay/` is not a real
  directory, or when either cannot be inspected; `may_start(state, ws, uid)` (a private `relay/` checked with
  `ensure_private_dir(create=False)`, a private `link.json`, not stopped; any doubt is false) is what R5's start must
  pass, under `service.relay_lock`.
- Control ops `host.relay_stop {workspace_id}` and (J) `host.relay_start {workspace_id}` (idempotent, no idem;
  `not_found` for an unregistered workspace). `orch host relay stop --workspace W` writes the marker itself first, so
  it works with the host down, then stops a live worker through the host. Only a missing socket or a refused
  connection counts as "host not running"; any other failure (timeout, protocol error, unsafe socket path, a host
  that is not the pinned one) keeps the marker and exits 9 (`host.unavailable`, "the marker is set, a running worker
  may continue until the host restarts"). `orch host relay start` clears the marker only through the host or with the
  host confirmed absent, and is refused under `ORCH_GRANT` (`human_only`).
- R5 interface: register `service.outcomes.relay_fields`; start-up recovery (`relay.submit.recover(service)`) is **not**
  run in `open` (amended H12e-2): R2 runs it in `Bridge.start`, honouring the restore hold-off (a stopped worker runs
  nothing); start the worker through `service.relay_starter` (or in `open`) only under `service.relay_lock`
  after `relaylink.may_start`, setting `service.relay` to a worker with `stop()` and `join(timeout) -> bool`.
  `relay_starter(service)` runs **under** `relay_lock`: it must not take that lock, wait for a job of the workspace's
  worker thread, or block on anything that does (`close()` and `relay_stop()` take the lock; a starter that waits
  for the worker thread deadlocks a close queued behind it). `close()` sets `closing` under the lock before it
  takes the worker, so a start racing a close is refused; a stop answers `stuck` while a retained stuck worker's
  threads live, and `close()` joins that worker too (bounded by 120 s); build
  relay calls with `Call(service, …)` for the identity's own workspace, `Call.relay = identity`,
  `peer = identity.peer()`, `columns = relay_columns` (the kind is derived by `authorize`); append only through
  `append_relay(service, log, events, extras, person_id=…, device_id=…)`; R1 links only through
  `service.store.relay_link(link, guard=lambda: relaylink.link_reservation(service))` and marks it linked only
  through `store.relay_mark_linked(expect)`.

### 11A.5 The bridge loop (R2) and the publisher (R3)

**First link (R1).** `relay.link` runs, in order:
1. a worker job: `refuse_reader_op(history=True)` and `link.json` with `state:"linking"` under the store lock (the
   reader reservation, 11A.6), with the operator's `--max-publication-bytes` (required for a non-default relay; the
   internal instance is 4 MiB, the public one 16 MiB);
2. the owner's account and delegation on the relay (owner client, P3-D34) and the owner-signed genesis pin (person
   path, 11A.2);
3. `PUT card` without a session (the only unfenced workspace write, P3-D4: a workspace login needs a card). Before
   it, and before any other request, the `link.json` state check and the local preflight (steps 1 to 3 of the restart
   takeover below) run: a copied or restored `linking` directory sends nothing (round 6, orch-52 #1);
4. challenge, login, `host/hold`, `PUT genesis` (a W(g) write, so after the hold; the relay refuses every checkpoint
   without it), the first publication (below, with catch-up), then `state:"linked"`.
A crash leaves `linking`; the next start runs the sequence again (every step is idempotent on the relay). The card and
the genesis pin are persisted before their first PUT and re-sent byte for byte: the relay compares the whole pin,
signature included (a re-signed pin is a permanent 409 `genesis_mismatch`), and a rebuilt card at the same `card_seq`
is `stale_card`.

**Session and holder (R1).**
- Startup: `POST session/challenge`, `POST session/login {challenge, kind:"workspace", id, sig}`, then
  `POST host/hold {gen: holder.gen or null, take_over:false}`.
  - On 409 `holder_conflict`: wait out the lease (≤ 40 s, monotonic) and retry.
  - On `fenced` while running: stop writing and alarm `fenced`. At start, `fenced` on the renewal leads to the restart
    takeover below, whose probe is `{gen: null, take_over: false}`. Only the explicit operator command
    `orch host relay takeover --workspace W` (control socket) sends `take_over: true`.
- Re-login is single-flight (one lock per worker): 120 s before the protocol's 15-minute token lifetime, counted on the
  monotonic clock from the start of the login (the relay's `expires_ms` is never trusted, 11A.8), or once on a 401
  `unauthenticated`/`expired`, which also covers a relay with a shorter lifetime; the identical bytes are then resent.
- **Restart takeover (DECIDED by orch-52, 13 Oct 2026; core-only, fail closed, no relay change).** At start, before
  any network call (steps 1 to 3 are `Session.preflight`, one local check that gates **every** entry point since round
  6, orch-52 #1: the first request of a session of any kind, login, challenge, the session-less `PUT card` and every
  directory read or write, runs it, `hold()` at start runs it again, and `first_link` runs it right after its
  `link.json` check; a failure refuses every later call locally until the operator takeover, **whatever passed
  before**: `call()` re-runs the preflight while a refusal is recorded, so a refusal raised after an earlier pass (a
  `pending` record found by `hold()`, a probe granted a `gen` other than stored+1) stops login, challenge, the
  session-less `PUT card`, every directory call and the logout too (round 7, R6-1)):
  1. the instance record must be bound to this very directory (its inode; `dev` is informational, 11A.3); else alarm
     `relay_takeover_refused` ("this state directory was copied or restored") and stop;
  2. `holder.json` has `pending` (a probe was in flight when the host stopped): alarm ("possibly our own interrupted
     takeover; confirm with `orch host relay takeover`") and stay stopped;
  3. (a) `holder.json` says this state directory was the last holder and (b) its instance id is this directory's;
     either failing: alarm naming the condition and stop. No `holder.json` is a first link only while `link.json`
     is `linking` (or absent): a `link.json` already `linked` (any state but `linking`, or not a JSON object) with
     `holder.json` and/or `instance.json` missing is a restored or copied linked directory (a selective restore,
     `rsync --exclude`) and is refused the same way, naming `orch host relay takeover` (which rebinds the instance
     record and records the new `gen`); it is never treated as a fresh first link (round 7, R6-3). **Absent** means
     only that the file does not exist: a state file that is unreadable, not JSON, or the JSON literal `null` is a
     `StateError` (nothing is sent), never "absent" (round 8, R7-1). A `StateError` after an earlier pass is not
     sticky, but the pass is dropped: every later call runs the preflight again and fails while the state stays
     unreadable (round 8, R7-2). The whole preflight runs under a per-`Session` lock (round 9), so this holds for
     threads sharing the `Session` too: a concurrent passing run cannot set the pass after another thread's
     `StateError` (it no longer depends on one-thread discipline; the lock is never held across network I/O). One
     accepted exception: `logout()` may still send after a preflight `StateError` (never after a refusal), since
     releasing our own token is not acting as holder.
  The pass and the sticky refusal are **per `Session` object** (round 8, R7-4): R5 keeps ONE `Session` per state
  directory, shared by the workspace's relay threads (poll, resp, pub, aux), or shares the refusal state between
  them; otherwise a refusal raised by one thread's `hold()` does not stop another thread's session that already
  passed. **R5 follow-up (round 8, R7-3, not built):** a `linking` directory copied mid-link without `holder.json`
  and `instance.json` still passes as a first link. Proposed rule: refuse when `instance.json` was just created by
  this open and `link-card.json`, `link-genesis.json` or `sign-ledger.json` exists (no legitimate run produces that
  combination: `instance.json` is claimed before the first link can write any of them).
  Then:
  4. Renew with the stored `gen` (`{gen, take_over:false}`): a renewal is not a probe. Success: run.
  5. Only when that renewal is `fenced`: write `pending = gen + 1` durably, then probe `{gen: null, take_over: false}`.
     `holder_conflict` means a live lease: wait it out (`stop.wait`), never steal. `take_over: true` is never sent
     automatically.
  6. The granted `gen` must equal `stored + 1` (the relay's row is never deleted and each new hold is +1): persist it
     and clear `pending` atomically before any other write or poll. Any other value: alarm `relay_takeover_refused`
     ("relay gen X != stored+1: another holder took in between, or a restored state"), write nothing under it, poll
     nothing, stop; the lease lapses after 40 s.
  7. A relay answer without a `gen`: alarm.
  **Order changed 13 Oct 2026 (review R1, approved by orch-52):** steps 1 to 3 used to follow the renewal; a copied or
  restored directory could then renew into the original's live lease and become a second holder (both writing W(g),
  both signing). Checking first costs nothing on a real restart or reboot (the id and the inode match; a changed
  `st_dev` after a remount is only logged, 14 Oct 2026, review round 4 N2). It stops a file-level copy or restore,
  not a perfect clone: see residual (v) below.
  The explicit operator takeover (`orch host relay takeover`, R5 wires `Session.operator_takeover`) sends
  `take_over: true` and records the new `gen` as ours. A running renewal answered `fenced` still stops the worker.
  **Costs (13 Oct 2026, accepted by orch-52):** (i) a refused probe still bumps the relay's `gen`, so a stale or
  restored copy can knock the real host into the fail-closed manual-takeover state (availability only; mitigated by
  checking (a) and (b) before any probe); (ii) the probe resets the relay's `holder_delivery`, so undelivered notices
  can drop; (iii) a relay route that exposes the current `gen` stays a later option; (iv) a transient error during the
  probe (503, timeout, reset) leaves `pending` set, so even an in-process retry needs the operator: a probe that timed
  out may have been granted, and failing closed is deliberate; (v) **14 Oct 2026 (review round 4 N1, accepted by
  orch-52):** a block-level clone of the state directory (a cloned VM image, `asr`/`dd`, a block-level disk clone
  booted on a second machine) keeps the same inode and bytes; so does a writable filesystem snapshot or clone attached on the SAME machine (btrfs/ZFS snapshot, an `asr`/`dd` clone as a second volume), which gets a new `st_dev` and, since the device check was dropped, is no longer caught (review round 5 P1; read-only APFS/Time Machine snapshots fail with EROFS before any network call); and a copy onto another volume can land on the same
  inode by chance. Steps 1 to 3 pass, the clone renews the original's live `gen`, and both hosts would write and sign
  as one holder. Nothing local can tell a perfect clone apart; the relay's holder `gen` is the only backstop, and the
  operator rule is: never run a clone while the original is live. The relay asserts `gen` itself:
  this guards against honest concurrency (a second instance, a restored backup, a relay reset), not a lying relay.
  One live instance per state directory is enforced by an exclusive `flock` on the directory (11A.3).
- A 401 `bad_signature` on login is not retried within 60 s, and it counts toward the origin alarm (11A.2).

**Lease renewal.** The relay renews the lease (40 s) only when a poll **starts** (`bridge.py` `host_poll`), and the poll
thread is blocked for up to 25 s in each poll or on a full queue, so it cannot also be the heartbeat. The aux thread
owns renewal: every 15 s (monotonic) without a poll start, it sends `host/hold {gen}`. Its other work (R4 pages) has a
10 s total deadline per call, so a renewal is never later than 25 s after the last one. A renewal answered `fenced`
stops the worker.

**Poll and serve.**
- The poll thread runs `GET host/poll?after=<cursor>&wait=25`. Items go to a queue bounded by 16 items **and** 4 MiB of
  envelope bytes; while it is full the poll thread does not poll.
- The responder applies proto §8.6 in its normative order:
  - silence (drop) for steps 1 to 3;
  - a sealed, WSK-signed refusal for every check after the tag, entered as `answered` before it is sent.
  It then admits (T1), dispatches through `relay_call` with at most 2 jobs in flight per workspace, chunks the answer at
  ≤ 256 KiB (an answer above 4 MiB of plaintext is refused `frame.too_large`), commits T2 and POSTs.
- `after` advances only to the end of the contiguous prefix of `n`s whose rid record is durable (§11, P3-D10). A
  `pair` item is handed to R6 (`R6Pairing`, 11A.15.2). Until R6 exists, it is dropped and never acknowledged out of
  order.

**Epochs (proto §8.5, P3-D22).**
- A lower epoch whose key `BridgeKeys` still holds, burned keys included: record, then a `stale_epoch` refusal sealed
  under that old key.
- A higher or deleted epoch: drop.
- On rotation (an R6 signal): an open answer ends with `LAST | REFUSAL stale_epoch` at its **next unused** index; a
  request that ran but has no chunk `attempted` gets `already_done {status:"unknown"}`. An `attempted` chunk is never
  replaced (the relay checks a byte conflict before the epoch, `bridge.py`): it is re-POSTed as is.
- **Nothing new is sealed under an old epoch, except refusals:** the `stale_epoch` refusal above and the rotation's
  closing refusals are the only bytes sealed under an old epoch.

**Chunk POST outcomes.**

| Relay answer | Host action |
|---|---|
| 202 | mark `posted` |
| lost response or timeout | the chunk stays `attempted`; resend the same bytes (the relay coalesces) |
| 410 `expired` (unknown rid, or past `first_ms + 900 s`) | stop; keep the record (the device's resend is the recovery) |
| 409 `retry_exhausted` (8 restores of this chunk, P3-D27) | stop for this chunk; terminal |
| 409 `chunk_conflict` | a host bug or a lost store: alarm, never post other bytes |
| 409 `stale_epoch` | the epoch moved: the rotation rule above |
| 403 `not_eligible` / `revoked` | drop the answer; R6 learns the revocation |
| 429 / 503 | 11A.7 |

The re-store of an expired chunk happens only when the device resends: an identical resend replays the stored bytes,
and the relay stores them again within its 900 s deadline and its 8-restore cap. The host never re-posts on a timer.

**Publisher (R3).**
- Order: outbox entries, oldest first. A restore's entry follows its source, and the normal entries behind it wait
  (P3-D21). After a 200 `accepted` or `ignored` whose `{n, hash}` equals the entry's checkpoint, the publication history
  is appended (11A.3), then the entry is deleted.
- One set per entry: `PUT staging/{source_hash}/{target_hash}/pages/{i}` (≤ 4 MiB each), `PUT manifest`, then
  `PUT checkpoint {checkpoint, source_hash[, restore:{line, cert, authz:{delegation}, log}]}`. `source_hash` is the
  relay's held H from R1's verified `GET checkpoint`.
- Pages are built from the logs one page at a time and streamed: at most one page plus the manifest in memory. Every
  body is `cj` and its digest is recorded before the PUT.

**Maximum publication size (P3-D37).** The relay refuses a set above `ORCH_RELAY_MAX_PUBLICATION_BYTES` (default 16 MiB;
the internal deployment sets 4 MiB) at a page (413, running total), at the manifest (413, implied size) and at the final
PUT (400, before any fold). The relay does not announce it, so `link.json` carries `max_publication_bytes` (the cap C).
- **Exact size.** Before staging, R3 runs the page and manifest builders in counting mode and sums the exact `cj` bytes
  the relay's staging caps count. The 74-bytes-per-head bound is only a fast pre-check.
- **Catch-up (ordinary growth, mainly the first link).** When the set from H to the next plain entry exceeds C:
  1. a worker job `Store.mint_catchup(source=H, cap=0.9·C)` takes the store lock and loads every ticket log;
  2. it chooses prefixes Q1 ⊂ Q2 ⊂ … ⊂ Qk, each a per-log cut `(seq, head, acc)`, so that every step set (H→Q1,
     Q1→Q2, …) fits under the cap by the exact counter; **Qk is a fresh snapshot of the current logs**, so it contains
     every pending plain entry;
  3. it numbers them `n = m+1 … m+k`, where `m = max(Checkpoints.highest_n(), HostPins.high_n(), every outbox n)`,
     with one `at = max(clock, last at)` for all, and signs each with `Checkpoints.sign_prefix`;
  4. in one store transaction it writes Q1 … Qk as outbox entries, deletes the superseded plain entries, and writes Qk
     as `.state/checkpoints/workspace.json` (the newest checkpoint, so `write_workspace`'s baseline stays valid and the
     next ordinary checkpoint gets `n > m+k`). Q1 … Q(k-1) exist only in the outbox.
  The relay's `n` never exceeds the host's numbering (H7). A crash before step 4 changes nothing; after it, the
  publisher resumes from the outbox. Appends wait on the store lock and produce later entries.
- **Restore anchors.** A restore entry and its source are anchors: never superseded, renumbered or re-signed (the
  restore line signs its source). Catch-up runs only over plain entries strictly before the next anchor, and needs free
  numbers there (`anchor.n − H.n − 1 ≥ k`); without them, or across a restore, it does not synthesize anything: the
  anchor is unpublishable, `relay.too_large` is alarmed, and the owner's exit is migration (P3-D37).
- **A restore cannot be split.** A restore set above C is refused before staging with the permanent host code
  `relay.too_large`, alarmed, and never sent. Where possible this is surfaced at the restore's review, before the owner
  signs (the reviewed material includes the exact set size against C).
- **Unexpected `too_large`** for a plain set (the relay's cap is lower than C): never retried unchanged. C becomes
  `min(C, ½ · the refused set's bytes)` in memory and catch-up replans (planned entries not yet accepted are superseded
  again), at most 4 times per hour; then alarm `relay_too_large` until the operator corrects `link.json`.

**Startup classification** (§11, F1 §5.10). R3 verifies the held H (the WSK signature `store/checkpoints.verify_object`,
the pinned genesis and workspace, the genesis pin's owner-device signature) and classifies it:

| H is | Then |
|---|---|
| `n` above the host's numbering | H7 host-state loss: alarm, publication stops |
| contained in the current logs (`find_divergence` reports nothing for H) | an ancestor: publication backlog. If H is older than `published.jsonl`'s last accepted entry, also alarm `relay_rolled_back` (a false success or a relay restore) and republish from the logs |
| not contained, but explained by an accepted restore: H is in `published.jsonl` or `.state/checkpoints/abandoned/`, `H.n` is below an accepted restore's `n`, and H is that restore's source or an ancestor of it | a relay rolled back across the restore: not divergence. Republish the plain extension from H to the restore's source (if needed), then the restore body from `published/restore-<n>.json`, then the current entries. Missing bytes: alarm `relay_rolled_back`, no degrade |
| anything else verified but not contained | an unexplained fork: `chain.diverged`, degraded as in D-P2-12 until an owner restore |

- A mismatch with an entry still in the outbox is not divergence; the outbox rules decide it.
- **A relay refusal is never evidence by itself.** 409 `chain_diverged` from the relay makes R3 re-read and classify H as
  above. An unverifiable claim raises `relay_misbehaving` and the publisher backs off.
- **Other refusals.**
  - `restore_source_mismatch`: re-read H, then the owner signs again.
  - `restore_missing_revocation`: R4 fetch, then the owner signs again.
  - `checkpoint_time` (P3-D25): retry with backoff and alarm `clock_skew` after 3. The checkpoint bytes are never
    re-signed.
  - `staging_conflict`: drop the set (sets expire after 1 h) and restage under the same `(source, target)` only with
    identical pages. Otherwise alarm.

#### As built in H12b (store side: outbox, catch-up, evidence intake)

H12b built the store half of R3 and R4 in `orch.store` (`store.py`, `outbox.py`, `checkpoints.py`). Nothing in
`orch.relay` is part of it. Every method below takes the store lock, so R3 and R4 call them only as worker jobs
(`RelayWorker.submit`), never across networking.

**When the outbox is on.** While `<state>/hosts/<ws>/relay/link.json` exists (`Store.outbox_active`). The file is
written only by `Store.relay_link(link, *, guard)`. That method takes the store lock, then enters `guard()` (R5 passes
`lambda: orch.host.relaylink.link_reservation(service)`, H12a: the host-wide lock plus the `sign.convenience_readers`
and `host.limit` refusals, held across the write). It refuses `relay.link_exists` when a link already exists, and
otherwise writes `{**link, "state": "linking"}` (atomic, fsynced, 0600, directory 0700). If `guard()` raises,
nothing is written. `Store.relay_mark_linked(expect)` rewrites `linking` to `linked` under the lock only when the file
holds exactly `expect` (`relay.link_mismatch` otherwise). Appends take the same lock, so every checkpoint after the
write is queued. Before returning, `relay_link` fsyncs `relay/` and its parent `hosts/<ws>/` while both locks are
still held (review round 2), so a new `relay/` survives a crash.

**R5 lock order.** The store lock is taken first, then the guard's host-wide lock. R5's guard must never take a store
lock (of this or another workspace) while it holds the host-wide lock, or two workspaces linking at once deadlock. Before the
link nothing is queued: the first publication is a catch-up from the relay's H. After unlink nothing new is queued and
the existing entries stay.

**Outbox layout** (`<state>/hosts/<ws>/outbox/`, 0700, files 0600, in the D-P2-17 custody list per §11): one file per
entry, `<n, 20 digits>.json`, holding `cj({v:1, kind:"plain"|"restore", n, hash, checkpoint, restore, catchup})`.
`checkpoint` is the exact WSK-signed workspace checkpoint (a restore's target T) and `hash` its `checkpoint_hash`.
A restore's `restore` holds the full evidence set as the pending transition holds it: `source` (C), `event` (the
signed restore line), `log`, `device_cert`, `pk_pub`, `delegation`, `proofs`, `acc_from`, `acc_before`,
`forward_heads`, `abandoned_heads`, `publication_complete`. `catchup.json` in the same directory is the catch-up
journal (not an entry).

**The append hook (P3-D21).** In a linked workspace **every** append writes a workspace checkpoint, not every 50th
ticket append. Each checkpoint is the append's outbox entry. The store signs the checkpoint *before* the commit, from
the logs as the append will leave them, and writes the entry into the event's pending directory beside the
projection files (manifest field `outbox`, its sha256). The log line is the commit, so the existing crash rules
decide the entry with the event: a crash before the line deletes both, and after it recovery installs both. The
install checks the entry (WSK signature, workspace, genesis, and an entry for the event's log at exactly the event's
`seq` and head). It then adopts a plain entry's checkpoint as `workspace.json` and the held checkpoint, so the outbox
is never ahead of `workspace.json`. In recovery a pending entry that fails the check is reported `store.torn_write`
and left out; it is never installed. A restore's entry (T plus evidence) is built from the transition staged in the
same commit. Checkpoints written outside an append (`Store.checkpoint`, the eager open) are queued too, entry first.
**Numbering invariant (review round 1, Opus F1).** *No checkpoint `n` is signed unless it is reserved first, and a
reserved `n` is never reused.*
- `HostPins.reserve_n(n)` writes the high-water mark `checkpoint-n` durably (fsynced) before anything numbered `n` is
  signed: an append's planned checkpoint, any other workspace checkpoint, a restore target, and catch-up (`m+k` before
  the first prefix is signed).
- `_install_outbox` and journal recovery reserve again (idempotent).
- The next number is `max(highest_n, baseline n, high_n) + 1`, read from that one durable value. There is no outbox
  scan per append.
- A crash anywhere at most burns a number.
- Adoption repairs the host pin and `workspace.json` independently (Codex P1).

**Coalescing (review round 1, Opus F3).** The relay accepts any higher `n` that extends its held checkpoint: `n` need
not be consecutive (orch-relay `checkpoints.py`: `n > held n`, an exact extension, `at` not earlier). So when a plain
entry is written, the store deletes the entry just below it if that entry is plain, not catch-up and not the lowest.
- Always kept: the lowest entry (R3 publishes oldest first and stages only the lowest), every restore, the plain entry
  just before a restore (its source), every catch-up entry and the newest entry.
- The queue stays at about 2 + 2 × (queued restores) entries.
- R3 must stage only the lowest entry (`Store.outbox_first()`).

**Interface for R3 and R4 (orch-29):**

| Call | Returns | Refuses |
|---|---|---|
| `Store.outbox_active` | `bool` | — |
| `Store.relay_link(link, *, guard)` / `Store.relay_mark_linked(expect)` | `None` | `relay.link_exists`, `validation.relay_link` (another workspace, or a state past `linking`), whatever `guard()` raises (nothing written) / `relay.link_mismatch` |
| `Store.outbox_first()` | the lowest entry, verified, or None | `store.torn_write` |
| `Store.outbox_entries(limit=None)` | `list[OutboxEntry]` (only the first `limit` when given), lowest `n` first, each verified again on read: `n`, `hash`, `kind`, `checkpoint`, `restore` (`dict` or None), `catchup` | `store.torn_write` for any entry that does not verify (the publisher stops; it never skips) |
| `Store.outbox_delete(n, hash)` | `True` (deleted) or `False` (no entry `n`: idempotent) | `outbox.mismatch` when entry `n` holds another checkpoint (nothing deleted); `store.torn_write` for a symlinked entry |
| `Store.mint_catchup(source=H, cap=int, measure=f)` | the new `OutboxEntry` list Q1 … Qk (`catchup: true`), or `[]` when nothing lies past H | `relay.too_large` (a restore is queued, or one event's step exceeds `cap`), `checkpoint.prefix_invalid` (H does not verify), `chain.diverged` (H is not in the logs, or its `n` is above the host's numbering), `chain.broken`/`chain.diverged`/`checkpoint.missing` while any log is broken, unverified or diverged |
| `Store.note_forwarded_revocation(item)` | `"held"`, `"pending"` or `"dropped"` | nothing: an item that does not verify is `"dropped"`; only a store fault (`store.busy`, `store.torn_write`) raises, and then there is no result, so no ack |

- `measure(segments) -> int` is R3's exact counter (11A.5). It receives a tuple of
  `orch.store.outbox.Segment(log, after_seq, from_acc, heads)`, one per log that grows in the step, the workspace log
  first and then the tickets by uid. It returns the `cj` bytes the relay's staging caps count for that step.
- `source` is the relay's held checkpoint object `{o, sig}` as R1 verified it, or None when the relay holds none.
- `OutboxEntry.to_json()` gives the stored form.
- `held` tells R4 to raise the owners' `needs` item: the store has no needs items.

**Catch-up as built.**
- The store loads every ticket log first and refuses if anything is broken or a ticket's checkpoint is missing
  (`checkpoint.missing`, review F4).
- The steps follow the merged order of §5.5 past H. Each step takes the longest run whose set fits (exponential,
  then binary search over `measure`), so every prefix is downward closed in the merged order.
- Numbers are `m+1 … m+k`, with `m = max(highest_n, high_n, outbox n, held n)`. There is one `at`: the maximum of
  the clock, every checkpoint `at` in play (`workspace.json`, held, H, the outbox) and the last event `at` of every
  log.
- `Checkpoints.sign_prefix` re-checks every prefix before signing:
  - `n` is above every existing number, and `at` is not before the base;
  - each entry is a point of its log (head and accumulator);
  - the prefix contains its base (H, then the previous prefix);
  - every added ticket event rests on a workspace `seq` inside the prefix;
  - the added events are downward closed.
  Any failure is `checkpoint.prefix_invalid`. Nothing is carried from an older checkpoint, so a prefix never lists a
  later ticket entry.
- R3's `measure` callback runs under the store lock. After the plan, the store refuses with
  `checkpoint.prefix_invalid` when the logs changed (in memory or on disk) or the numbering moved while `measure` ran,
  and R3 re-plans; otherwise it reserves `m+k`. `measure` must be a pure counter that never calls the store.
- The replacement is atomic through `outbox/catchup.json` (Q1 … Qk and the plain entries they supersede). The
  journal is written first. Then the entries are written, the superseded ones deleted, Qk written as `workspace.json`
  and accepted, and the journal deleted. Every lock finishes a journal it finds, before anything numbers a checkpoint
  or reads the outbox.

**Judgement calls (fail closed).**
1. *Anchored catch-up mints nothing.* 11A.5 allows numbers between H and a queued anchor when they are free. With
   dense numbering none is provably free: the store keeps no record of every checkpoint it once signed under a number,
   and a second signature under a used `n` is an equivocation. So any queued restore makes `mint_catchup` refuse
   `relay.too_large` and change nothing (the 11A.5 "anchor is unpublishable" exit).
2. *Every append is checkpointed while linked.* "An outbox entry per append, the exact checkpoint" (§11) is read
   literally: the P1 "every 50 ticket appends" rule applies only to unlinked workspaces. This costs O(tickets) per
   append, bounded by the P3-D20 ticket capacity.
3. *Outbox growth* is bounded by coalescing (above, review F3). It replaces round 0's "no coalescing".
4. *A forged or torn pending entry* is reported and dropped in recovery, not installed. The committed event then has
   no entry of its own, and the next checkpoint covers it.
5. *Forwarded evidence* is judged by 11A.9 exactly, with these additions (review F2 and F8):
   - the item is `dropped` when it has another key set (exactly the 8 relay keys, `provenance` exactly
     `{cursor, digest}`), is above 4 KiB in `cj`, or fails `schema.validate_def` (`deviceCert`, `revocation`), or its
     `label_sealed` is above 1024 characters;
   - stored records are at most 6 KiB;
   - at most 256 forwarded records per person, the owner included, and at most 1536 non-owner records in total,
     members included; the owner is exempt from the total (review round 2, D1); the eviction cap is 1024 (F1 §5.10's
     runtime numbers are updated to match);
   - the forwarded worst case is (1536 + 256) × 6 KiB, about 10.5 MiB;
   - `revocations.jsonl` is read without a size limit (review round 2, D2): only the host writes it, forwarded records
     are capped as above, and chain-learned records grow only with accepted history (one per pair, each bounded by
     the event line cap);
   - anything unparseable is `dropped` and logged by failure class, never with key material;
   - the pinned owner passed to `note_revocation` is the genesis delegation's `owner_person_id`.
6. *Planning keeps the post-commit exclusions* (review F5): an excluded log, or a ticket checkpoint already at or past
   the event, gives no planned checkpoint. Such a linked append still commits, with **no outbox entry** (fail closed:
   nothing is certified for a log the post-commit rule would not certify).
7. *A pending linked append* blocks a store opened without host state (`validation.host_state`, naming the event).
   The doctor names it as `outbox.pending`, and the next host open finishes it (review F6).
8. *A restore before the first accepted publication* (review F7): the restore entry is an anchor ahead of the
   first-link catch-up, so nothing can be published until migration. This is accepted for v1.7 and documented here.
   The owner should complete the first publication before signing a restore.
9. *The pending outbox file* is created `O_EXCL | O_NOFOLLOW`. A symlinked pending file or entry is refused (F8).

Tests: `tests/store/test_outbox.py`, `tests/store/test_catchup.py`, `tests/store/test_forwarded_revocation.py`.

#### As built in R2 (bridge loop and durable remote submission, 15 Oct 2026)

Code: `relay/bridge.py` (`Bridge`: §8.6 in order, the rid state machine, posting, rotation; `BridgeLoop`: the poll,
responder, two job and aux threads, `start()`/`stop()`/`join(timeout) -> bool`), `relay/rids.py` (`rids.sqlite`),
`relay/framing.py` (the closed tables and the sealed framing), `relay/keys.py` (`BridgeKeys`, `R6View`, fixtures only
until R6), `relay/submit.py` (the event/scope table, the lock-free checks, `recover`, `make_relay_fields`) and one
host adapter, `host/relayentry.py` (`RelayEntry`, the `HostPort` the bridge calls; `register(service)` for R5), with
`host/relayadmit.py` (the one seam to core's H12e helpers) and `host/relaybuild.py` (the `relay.submit` result
builder, registered once when `host/outcomes.py` is imported; round 3, M1). Two small R1 changes: `Session.call(raw=, transport=)` (an ORB2 body, and one connection per thread with one shared
Session) and `StateDir.open_private`/`file_path` (the checked SQLite open of 11A.3). One host change: a result
builder's `HostError` is stored as that `failed` outcome (`outcomes._fields`).

**R2 framing (the sheet orch-mobile codes against; data: `tests/relay/r2_vectors/r2_framing.json`, generated by
`scripts/gen_r2_framing_vectors.py`, never edited by hand; round 3, 15 Oct 2026: 37 cases).**

- Request meta, strict JSON inside the seal, `data` empty: `{"op", "args", "idem"?, "rd"?}`. Checked in this order
  before any column: an `auth` key → `req.invalid`; a grant-shaped string anywhere → `grant.secret_in_args`; an op
  outside the table → `forbidden_scope`; another key, non-object `args`, or non-empty `data` → `req.invalid`; the idem
  rules (a write without `idem` → `idem.required`; a malformed one → `idem.invalid`; one that is not the event id →
  `invalid.input`, 14.6 P1; an `idem` on a read → `req.invalid`); `rd` must be `sha256:<hex>` (carried into the identity, never a qualification in
  v1.7; vector `request_with_rd`).
- The one closed op table (`framing.OPS`):

  | Relay op | Backend op | Kind | Scope (certificate AND member list) | Event |
  |---|---|---|---|---|
  | `ticket.query`, `ticket.get`, `ticket.text`, `needs`, `activity.list`, `workspace.show`, `workspace.settings`, `member.list`, `whoami`, `outcome.get` | the same | read | `look` | — |
  | `new_ticket` | `relay.submit` | write | `decide` | `ticket.created` |
  | `answer` | `relay.submit` | write | `decide` | `question.answered` |

  Every other op (the proto §8.7 `http`, `decision`, `member_remove`, `revocation`, `drop_*`, `pair*`, `cancel`, and
  every socket op) → `forbidden_scope`.
- **Read schemas.** A read's `args` and its Result `data` are exactly the host op's, filtered for the viewer as for a
  person (§0.5): `needs`, `ticket.query`, `ticket.get`, `member.list`, `workspace.show`, `activity.list`, `whoami`
  in §0.8 (and the `read_ext` fields of §0.4), `ticket.text` and `workspace.settings` in §0.8.5, `outcome.get` in §0.7
  (`{state, result?, error?, exit}`; `pending`, `committed`, `failed`, `partial`). H12e adds `ticket.get` `doc.head`,
  `questions[].answer.id` and `members_v` (one snapshot) and `workspace.show` `next_key` (11A.14). The vectors' read
  results come from a test fake: only the framing is normative there.
- A write's `args` is `{"log": <ticket uid>, "events": [<one F1 person event>]}` signed by the requesting device over
  `person_signing_bytes(ws, log, E)`; `idem` = the event id. Both write ops need `decide` (11A.14 L2 and the 14.8 phone
  profile `["look","decide"]`; round 3, I1). Lock-free checks (`submit.check_events`), 14.6's order and codes
  (round 3, M2): P1 shape, exactly one event, `log` a ticket uid, idem = event id (`invalid.input`); P2 the event type
  of the table (`forbidden_scope`); P3 the submitted-subset schema (`submit.check_schema`: no host-stamped field and no
  `source_sha`, the id a ULID, `v` 2, `hash_v` 1, a creation's `based_on` null, then the F1 event schema on the event
  with a probe stamp: exact field set, `cj`, the type's own rules; `validation.event`, and the F1 §11.3 text rules
  `validation.text`); P4 the actor exactly `{person, the device's person, the device}` and `sig` present, P5 `auth` in
  `{secure-enclave-unlocked, passphrase}` (`op.not_allowed`); P7 the signature under this device's `dk_sig_pub`
  (`sig.invalid`). All D. P3 makes a malformed event a definite refusal before the lock, never a stuck write (round
  3, I3; vectors `write_host_field`, `write_schema`, `write_text_rule`, `write_sig_invalid`). **No freshness here**
  (ruling A, below).
- Answer: success = chunk 0 meta exactly `{"status":"ok"}`, later chunks exactly `{}`, `data` across the chunks =
  `cj(Result)`, `LAST` on the final chunk; each chunk ≤ 256 KiB. The whole answer's chunk envelopes must fit the
  relay's per-device budget of undelivered response bytes (2 MiB, proto §19 "Per workspace", P3-D17), so `cj(Result)`
  ≤ `framing.MAX_ANSWER` = 2 095 587 bytes (8 full chunks); above it `frame.too_large` (D), refused before anything
  but that refusal is stored (vector `answer_over_the_relay_budget`; round 1's 4 MiB answer could never be delivered:
  the relay answers 429 above 2 MiB).
- A refusal = one `LAST | REFUSAL` chunk whose meta has EXACTLY these fields (`framing.refusal_meta`, the only
  builder; the vectors carry the table as `tables.refusal_fields`):

  | Code | Fields |
  |---|---|
  | `stale_sequence` | `high` |
  | `stale_timestamp` | `host_ms` (host time) |
  | `stale_epoch` | `epoch` (the committed one; for a burned or gated epoch the pending one, else `EHW + 1`) |
  | `already_done` | `status: "unknown"` |
  | `not_member`, `revoked`, `bad_signature`, `busy`, `malformed`, `forbidden_scope`, `rid_conflict` | none |
  | every host §0.6 code (`pending` included) | `retryable` (always; DERIVED from the class: A, B true; C-auto, C-review, D false); `seqs` only for a write whose events LANDED (`failed`/`partial`: never for "not run"); `next_key` only for `ticket.key_not_next` (advisory) |

  A refusal never carries a message or request text.
- **Retry classes** (`framing.RETRY`, authoritative; ruling C): **A** nothing ran and no seq was consumed, or refused
  before anything could run: resend the same signed event and idem (`busy`, `store.busy`, `stale_*`, …); **B**
  unknown, something may have run: settle by reading (`outcome.get` with the idem, or the ticket): `already_done`,
  `rid_conflict`, `pending`, `idem.expired`; on a READ `already_done {status: unknown}` means re-issue under a new rid
  (vector `already_done_on_a_read`); **C-auto** rebuild without review under a NEW event id (`ticket.key_not_next`
  with `next_key`, `members.stale`); **C-review** review again (`event.bad_base`, `question.stale`, `event.expired`,
  `event.abandoned`, `outcome.unknown_idem`); **D** definite (round 3 adds `validation.event`, `validation.text`,
  `sig.invalid`). An unknown code is D; a host code outside the closed refusal table is sealed as `internal` in its
  context (round 3, D2-7), never raised into the loop. Context rules
  (`framing.RETRY_CONTEXT`, data): `internal` is B after a WRITE was admitted (effects possible), else D;
  `outcome.unknown_idem` is B before the bound (the event id's ULID time + 1 200 s, when the bridge answers `pending`
  instead) and C-review after it. Notes (`framing.RETRY_NOTES`, data): `ticket.exists` after a C-auto rebuild means
  "check `outcome.get` for the first idem"; `event.abandoned` comes from core's `known_or_fresh` (round 3: wired);
  `outcome.get` `state: failed` without `seqs` means nothing landed: classify by `error.code`, never by the state
  (orch-52's M3 amendment: no `refused` step).
- `outcome.get`: the host's record first (a terminal one is answered as such: vectors `outcome_get_failed`,
  `outcome_get_partial`, `outcome_get_idem_expired`); `{"state":"pending"}` while this device's rid for that idem is
  admitted (bounded, J8) and, without a record, until the idem's ULID time + 900 s + 300 s
  (`framing.PENDING_NO_RECORD_MS`); after that `outcome.unknown_idem` (C-review, `outcome_get_unknown_idem`). A
  record the HOST still holds as `pending` is answered `pending` (B) at any age, never `outcome.unknown_idem`: it may
  still reconcile as landed (round 3, A31).
- Event age is checked against HOST time (the host clock at job time), never the phone's or the relay's.

**`relay.submit` on the host (`RelayEntry.submit`; orch-52's final #465 decisions, ruling A).** After the lock-free
checks, ONE `store.locked()` span: the refresh, the identity (`Call.authorize` with the recheck of steps 1 to 3 and the
scope), the outcome record (`OutcomeStore.execute`, key `(ws, r:<device>, event id)`, binding = the event without
`sig`: a known idem replays its outcome), then the log dedup by event identity (`signed_digest` and actor: an
identical landed event → `committed`, `duplicate: true`; the same id with other content or actor →
`event.duplicate_id`), and ONLY THEN, for a never-seen event, the ULID window [host now − 900 s, host now + 300 s]
(`event.expired`, C-review), the target and key (`ticket.exists`; `not_found` for an answer to a missing ticket; the
owner is the signer's person, else `invalid.input`; the key exactly the next one, else `ticket.key_not_next` with
`detail.next_key`), `judge_events`, `outcomes.plan` (fsynced), `append_relay`, and a log check before the outcome is
classified. Store errors (`store.busy` from the lock) are sealed refusals (A), never a raw exception; a model
exception that is not a refusal (an event the model cannot judge) is `validation.event` (D), never a stuck write
(round 3, I3b). Every core helper of these steps is reached through ONE seam, `orch/host/relayadmit.py`: **round 3:
core's real H12e helpers** (#474: `known_or_fresh` with `event.abandoned`, `check_target` with the visibility filter
and the uniform `not_found`, `check_window`, `next_key`, `signed_digest`, `judge_events`, `COLUMN_R_AUTH`; D2-5
closed) plus ONE stand-in, `device_standing(store, person_id, device_id) -> "ok" | "removed" | "unknown"` with
H12e-2's exact signature (#482; TODO: import it once merged). `tests/relay/test_relayadmit_seam.py` runs the contract
against both. `RelayEntry.standing` refreshes the store and answers `not_member` unless the person is a current member
and `device_standing` is `ok` (a removed device, a removed member's device; round 3, I2, tested with a real F1
`device.removed` for a read and a write).

**Epochs and the seal gate (G1, C5).** Every chunk is sealed inside `BridgeKeys.sealing()`: the seal lock and ONE
snapshot (`SealState`: the committed epoch, the rotation gate, the epoch a `stale_epoch` names). The bytes are
committed (T2 or the refusal record) inside that span; R6 raises the gate or commits a rotation inside
`BridgeKeys.serialised()` (the same lock). `Sealer.seal_response` (data) refuses any epoch but the snapshot's
committed one and any epoch while the gate is up; `Sealer.seal_refusal` is the only path under an old, burned or
gated epoch and builds the plaintext itself from the closed fields. Round 3 (D2-7): the class is private
(`keys._Sealer`) and its constructor takes a module-private token, so only `BridgeKeys.sealing()` builds one, with
its own snapshot (pinned by an AST test). **Lock order** (round 3, D2-4): the workspace store lock first, then the
seal lock, then the RidStore lock (innermost; it never calls out). Inside a `sealing()` span the bridge never takes the
store lock and never calls `job()`; no `EpochKeySource` method (`current_epoch`, `wk`, `seal_state`, `refusal_key`)
may take the store lock or wait for the worker, because R6 commits a rotation under the store lock inside
`serialised()`. A lock-tracking test runs reads, writes, refusals, replays and a rotation against that rule. A burned epoch above the committed one
(`EpochKeySource.refusal_key(e)`, proto §7.2 H4 / §8.5 G2b) is opened only to answer, after steps 3 to 8, a signed
`stale_epoch` naming the pending epoch (else `EHW + 1`); every other outcome under it is silence. Vector
`stale_epoch_under_a_burned_key`; core test `test_request_under_burned_epoch_key_gets_signed_stale_epoch` (the relay
corpus has no vector of that name at orch-relay `438b719`; port it when it lands).

**Bounds (round 2, R2-3/R2-4).** Rows: per device ≤ `PER_DEVICE + BUSY_ALLOWANCE` (1 024 + 64) per 15 min and
≤ 16 384 per 24 h; host-wide ≤ 16 384 per 15 min and ≤ 262 144 retained (24 h); beyond any cap the request is dropped,
never recorded. Step-4 (`unverified`) records ≤ 10 per minute host-wide, keyed `u:<device>`. Tombstones ≤ 262 144
(oldest first). Stored answer bytes: per device ≤ 16 MiB and host-wide ≤ 128 MiB, each admitted read reserving one
whole answer (2 MiB); above them `busy` (A) before admission. **Round 3 (D2-3):** the byte budget applies to READS
only (checked after step 11, the seq not used); a write's answer is one small chunk, bounded by the row caps, so writes
are never refused for bytes. A device whose bytes (with this answer) exceed its fair share of 4 MiB sees only the
shared 96 MiB of the host budget; the remaining 32 MiB stay open to devices within their share. Starving every read on
the host therefore takes at least 6 heavy (96 MiB / 16 MiB) plus 8 light (32 MiB / 4 MiB) member devices, 14 in all
(round 2: 8, and writes too); filling the row caps takes at least 16 (16 × 1 088 per 15 min, 16 × 16 384 per day).
The host-wide live-row count and the stored bytes (host-wide and per device) are kept in memory, loaded at open and
updated with every transaction, so the per-request caps never scan the table (round 3, D2-6; round 2 measured 167 ms
per host-wide count at the cap); the sweep runs in batches of 2 048 rows, one short transaction each, and the vacuum
returns 4 096 pages per sweep 256 at a time, each step its own lock hold. `auto_vacuum=INCREMENTAL`. Worst case at about 720 B per row and 250 B per tombstone: about 190 MB of rows + 65 MB of tombstones +
128 MiB of answer bytes, about 0.4 GB in all (round 1: about 3.9 GB of rows plus unbounded answer bytes, all of it
reachable from one device). A chunk POST answered "retry" waits 1 s doubling to 60 s per rid.

**Judgement calls (fail closed, 15 Oct 2026; round 2 changes marked).**
- J1 Module split as in the 11A.1 row above (the design put everything in `bridge.py`). Round 2 adds the host seam
  `host/relayadmit.py`.
- J2 Step-4 refusals (`revoked`, `not_member`, `bad_signature`) are recorded (kind `unverified`, no seq) within the
  budget, so a resend gets the same bytes; a resend whose step 4 now fails is silence unless the stored record is itself
  a step-4 refusal: a stored answer is never re-posted to a device that no longer holds. A store that cannot answer
  step 4 (`unavailable`) is silence, nothing recorded. **Round 2:** keyed `u:<device>` (R2-11), so a forged header
  never answers the genuine request on that rid; the certificate and signature are checked before the host's
  standing, and an unsigned request reaches the store lock only while the budget could answer it. **Round 3:** a
  `u:` record's answer header names the device itself (D2-1: the round-2 code raised on `u:` and stopped the loop); a
  swept step-4 refusal is silence, and rotation leaves `u:` records alone. The SAME bytes (same digest, so the
  device's own signature) from a device that has since become a member replay the stored step-4 refusal and never
  run: a definite answer stays definite (D2-2); other bytes on that rid are not squatted.
- J3 R2 serves no stream: a `STREAM` flag or a non-zero stream id is `forbidden_scope` (step 10). The start fence runs
  after step 10 (the seq is consumed) and is recorded `already_done {status: unknown}`.
- J4 Reads are only the host reads served on the worker thread (table above). The registry reads and the reads with a
  reader phase (`diff`, `artifact.get`, `commit.list`, `quick.list`, `list`, `show`, …) are deferred.
- J5 One event per request and no `extras` (no ticket body, no artifact through the relay in v1.7).
- J6 `rid_conflict` case (c) needs every chunk's bytes still stored; else case (d).
- J7 Replays of a stored answer are re-posted at most once per 10 s and 8 times per rid (P3-D27), so a replaying relay
  cannot make the host post without end; a poll page stops filling the queue once it is full. **Round 2:** a
  same-rid resend after the bytes were swept (960 s to 24 h) gets `already_done {status: unknown}` at the next unused
  index, within the same bounds (ruling D), never silence.
- J8 **Changed in round 2 (R2-6).** A write whose run fails with anything but a refusal is retried by the aux thread
  (`submit.recover_one`) after 10 s, 60 s and 300 s, then answered `already_done {status: unknown}` (B): an idem is
  never admitted (`pending`) longer than about 370 s plus the run, inside the pending bound. A read that fails so gets
  `internal` (D). A request whose answer may not be sealed as data (rotated or gated while it ran, or answered after a
  restart under an older epoch) gets `already_done {status: unknown}` (G1).
- J9 Start-up recovery (`Bridge.start`, on the poll thread; orch-52's M5: accepted, so R5 does NOT call it in
  `WorkspaceService.open`, and a stopped worker runs nothing during the restore hold-off): the start fence, persisted chunks first, `submit.recover` (no record or a
  pending one with nothing landed runs again from the submission row with the identity **and the op's scope**
  rechecked (round 2, R2-10); landed events roll forward through `relay_fields`; terminal: nothing), then every
  admitted write is answered from `outcome.get` under the seal gate, every admitted read `already_done`.
- J10 The cursor acknowledges every processed item, a dropped `pair` item included (R6's until it exists); a poll
  item that is not well formed is skipped (round 2: logged once per page).
- J11 The signed events of a write are noted on its outcome record (`signed_events`, fsynced before the append) for
  `relay_fields`; the binding itself never contains signature bytes.
- J12 (new) Ruling B: the same idem on a new rid while a rid of this device for it is admitted gets the sealed refusal
  `pending` (B, seq consumed, recorded); nothing joins or runs twice. Once terminal, a resend replays the outcome.
- J13 (new) A gated epoch refuses new requests at step 9 with `stale_epoch` naming the pending epoch (else
  `EHW + 1`); a refusal is the only thing sealed under it.
- J14 (new) `stop()` never leaks a thread on a full job queue: the responder and job threads wait at most 0.25 s at a
  time; admitted rids left queued are recovered at the next start. `join()` is false only while a job is still in
  the host.
- J15 (new) Residual (R2-11): a member holding `WK_e` plus a colluding relay can still DoS one request by delivering a
  forged copy first (the relay's mailbox then holds the forged chunk); a colluding relay can withhold anyway.
- J16 (new) Residual: a hostile relay's poll page can name a huge `n`, which advances the durable cursor and skips
  later items; reliability only (it can withhold anyway).
- J17 (round 3, D2-1/D2-7) One poll item, one job or one aux step (sweep, stuck retry, flush) that raises is logged
  (the first 10, then every 1 000th, by class only), alarmed once (`relay_item_failed`) and acknowledged; it never
  stops the loop or the lease renewal. A job that raises leaves its rid admitted (recovered at the next start). The
  session's own errors (fenced, lost) still stop the worker.
- J18 (round 3, D2-7) A refusal code outside the closed table becomes `internal` in the same context; a signer
  refusal is silence for that chunk; a settle whose `lookup` raises answers `already_done {status: unknown}`.
- J19 (round 3, N1) The replay and POST-backoff maps are guarded by one lock, and one rid is POSTed by one thread at a
  time (a second caller leaves it to the next aux flush): no double POST of the same bytes.
- J20 (round 3, M4) The O6 identity check stays in the result builder (`relaybuild.rebuild_relay_submit`), not in
  `Planned`; the builder keeps no state: the workspace is the record's `namespace`.

**Deferred** (not R2): R3, R4, R5 (`RelayWorker`, the hook in `WorkspaceService.open`, `relay_columns` as a host
function: today `RelayEntry` builds the recompute), R6 (pairing at epoch 0, member lists, `WK_e`, the real
`EpochKeySource` with `seal_state()` and `refusal_key()`), `decision`, streams, the registry and git reads, ticket
bodies, the `device_standing` import from #482 (H12e-2), the T2 real-relay job.

**For R3/R5/R6.** One `Session` and one `StateDir` per state directory: the `RidStore` opens `rids.sqlite` through the
same `StateDir` the holder store uses. R5 builds `RelayEntry(service, backend)` with the socket backend, calls
`relayentry.register(service)` at open after `human.recover` (it sets that service's `relay_fields` hook; the result
builder needs nothing: `host/relaybuild.py` registers itself when `host/outcomes.py` is imported, so it is in place
before any `reconcile_all`), does NOT run `submit.recover` in `open` (the bridge runs it at worker start, J9), and runs
`BridgeLoop(bridge, session, conn=…)`; its
threads are `orch-relay-{poll,resp,job0,job1,aux}-<id8>`, the aux thread renews the lease, sweeps, retries stuck
writes and flushes (R4 adds its pages there), `job` must be `RelayWorker.submit(fn).result()`. R6's
`EpochKeySource` must implement `seal_state()` and `refusal_key(e)` and raise the gate / commit a rotation inside
`BridgeKeys.serialised()`, and none of its methods may take the store lock (the lock order above). R3 adds its publisher thread beside these; the store side of the outbox is H12b's.

### 11A.6 Convenience rule

A relay link and every device pairing add readers of the log (§0.8.6 item 1, D-P2-23).
- `READER_OPS` (`ops/readers.py`) gains `relay.link` and `relay.pair`. `relay.pair` is the host's `approved` answer to
  a pairing and any member list that adds a device.
- **The check.** Under the store lock, in the same critical section that writes `link.json` (link) or the R6 allocation
  (pair), the host calls `host/convenience.refuse_reader_op(service, op, history=True)`. It refuses with
  **`sign.convenience_readers`** (exit 3, not retryable, existing code) when `has_convenience_events(store)` finds any
  `auth:"convenience"` event in the merged history, or the per-workspace convenience flag is on.
- **The reservation comes first.** `link.json` (state `linking`) and R6's pairing record are written, durably, before
  any network write that exposes the workspace (card, genesis, publication, a pair answer), and stay through pending
  operations and crash recovery.
- **The reservation is permanent.** Readers keep what they read, so unlink never re-opens convenience: `orch host relay
  unlink` stops the worker and renames `link.json` to `unlinked-<linked_at>.json`. `host/convenience._relay_paired`
  MUST return true when `relay/link.json` **or** any `relay/unlinked-*.json` exists, as well as `membership/` (today it
  tests only `membership/`, which nothing creates yet). A relink is allowed and changes nothing for convenience.
- **Invariant.** While `_relay_paired` is true, `sign.convenience_unavailable relay_paired` blocks every new
  convenience event at prepare, binding and submit under the store lock. Every path that can create a convenience event
  (host and direct mode) MUST call it.
- **Foreign appends.** A start-up scan alone misses history added later. `RelayWorker.start` runs
  `has_convenience_events` once, and again after every store refresh that loaded events this host did not append (the
  `_build_journal(audit=True)` path), before R2 serves or R3 publishes anything further. A hit stops the worker and
  alarms `convenience_events_present`.
- **No owner exception in v1.7.** The D-P2-34 private-remote mark has no relay analogue: a relay's devices are other
  readers by definition, and a "private relay" label cannot establish what every retained reader understands
  (11A.11 Q4).
- **Phase 2: the reader-version handshake** (§0.8.6 item 9, H12d, draft). A phone's pairing carries a device-signed
  `reader_decl` in its pair-request meta (protocol v2 §8.3; `CertRequestInner` is unchanged). The host checks it when it
  opens the request and, authoritatively, in the R6 allocation's append under the store lock. Every column-R request
  then carries `rd`, the digest of the device's current declaration (§8.6), and the host serves a convenience workspace
  only against it. `relay.link` checks the relay's `GET /v2/relay-info` level at link and at every R1 login, an
  operator statement. Qualifying readers are admitted with convenience history; `relay_paired` then counts only a relay
  below level 1 (the permanent reservation above stays, with the relay's last level). Group H12d-3, with R6.

### 11A.7 Transport

**Stdlib only.**
- `http.client.HTTPSConnection` with `ssl.create_default_context()` (`CERT_REQUIRED`, hostname check, TLS ≥ 1.2).
  `http.client` reads no proxy variables from the environment, and the bridge never uses `urllib`.
- A proxy is used only when `link.json` names one, through `set_tunnel`.
- **Trust store** (decided by orch-52, 13 Oct 2026). `ssl.create_default_context()` (OpenSSL's default paths, a capath
  directory included), or only the `ca_file` that `link.json` names. There is no emptiness probe (`get_ca_certs()` is
  empty for capath-only stores that verify fine): a store that trusts nothing fails closed at the handshake. On macOS
  python.org builds the default paths may hold no CA; `ca_file` is the fix. There is never an unverified fallback.
- OpenSSL's `SSL_CERT_FILE`/`SSL_CERT_DIR` are honoured by the default context; acceptable, since controlling the host's
  environment already means being the host user.
- Name resolution (`getaddrinfo`) runs in a helper thread bounded by the call's total deadline and by `shutdown()`
  (a stuck resolver would otherwise sit outside both the socket timeout and the watchdog); the connection goes to the
  resolved address and TLS still checks the origin's hostname.
- A proxy from `link.json` is refused for an `http` origin (the login and the token would cross it in cleartext).

- The deadline watchdog and `shutdown()` act on a dup of the connection's socket, registered **before** `connect`
  blocks (round 6, orch-52 #3); when it is installed, a deadline that already passed or a `shutdown()` that already
  happened (both found nothing to kill) fails the call at once, so nothing is connected or sent. A `shutdown()` (or
  the watchdog) that lands after that check but before or during `connect` acts on a socket that is not connected
  yet; so the flags are checked again right after `connect` returns and before any byte is sent, and the call fails
  (kind `shutdown`, or `deadline`) with the socket closed (round 7, R6-2). Platform note: Linux ends a
  still-connecting socket on `shutdown(SHUT_RDWR)`; on Darwin a `shutdown()` on a pending connect can instead make
  `connect` return **success** shortly after (the socket then looks connected), and a `shutdown()` just before
  `connect` fails with ENOTCONN and is swallowed. In both cases the post-connect recheck is what keeps the request
  from being sent; any later `shutdown()` finds a connected socket and ends it.

**Deadlines and caps.** Every call has a **total** deadline on the monotonic clock, covering connect, headers and body.
Before each bounded read the socket timeout is set to the time remaining, so a relay that drips bytes ends at the
deadline, not after a per-read timeout per byte.

| Call | Total deadline | Response body cap |
|---|---|---|
| small JSON routes (session, hold, card, checkpoint final, ack) | 30 s (`host/hold` on the aux thread: 10 s) | 16 KiB (card 64 KiB) |
| long poll | `wait` + 15 s (40 s) | 4 MiB |
| reads (`GET checkpoint`, `members`, unpublished pages) | 30 s (R4 pages: 10 s) | 4 MiB |
| one staging page or manifest PUT | 120 s | 16 KiB |
| one chunk POST | 30 s | 16 KiB |

- Bodies are read in bounded pieces against the cap, whatever `Content-Length` or chunked framing says; over the cap
  the connection is closed and the call counts as transient.
- Decoded objects are bounded too: strict JSON with depth ≤ 32, a base64 field is size-checked before decoding, and a
  request envelope is ≤ 1 MiB.
- Request bodies are built at or under the relay's caps (11A.5), else refused locally.

**Redirects and connections.**
- No redirect is followed. Any 3xx is a permanent `relay_redirect` alarm.
- Per workspace: 4 connections (poll, responder, publisher, aux); 6 with R6's two threads (11A.15.2, 11A.15.16 Q7). Relay connections are outbound and do not count
  against the host's socket cap of 64. Instead, v1.7 caps relay-linked workspaces at **8 per host** (32 relay threads):
  `relay.link` is refused with `host.limit` above it (decided by orch-52, 13 Oct 2026).
- One long poll per generation (the relay cancels the older one).
- Connections are kept alive and reopened after any error.

**Retries and backoff.**

| Class | Answers | Rule |
|---|---|---|
| Transient | connection errors, deadlines, 5xx, 503 `overloaded`, 429 `rate_limited` | exponential backoff with full jitter, 1 s to 5 min. `Retry-After` is a floor (integer seconds, monotonic, capped at 1 h). Transient answers never count toward H8 |
| 429 `quota_exceeded` | membership | never retried unchanged: P3-D31's ever-device quota is permanent. An R6 alarm |
| 429 `quota_exceeded` | elsewhere | the relay cannot yet tell permanent caps from transient ones (planned detail `{"quota": …}`). At most 5 retries with backoff, then alarm `relay_quota` and wait 1 h between attempts. Once `quota` names `nodes`, `workspaces` or `evidence`, it is permanent at once |
| Permanent | 400 `too_large`, 413 | never retried unchanged (11A.5) |
| Body | every retry | the identical `cj` bytes: exact-body idempotency (§19.6) |

**Shutdown.** `stop()`:
1. sets `stopping` under the worker lock: `RelayWorker.submit` refuses from then on, so no new store job is queued;
2. calls `socket.shutdown(SHUT_RDWR)` on every live connection (closing a socket from another thread is not a portable
   way to end a blocked `recv`), then sets the stop event;
3. joins each thread with a 5 s bound and logs out best-effort. It does not release the lease: the lease runs out
   (§11).
A thread still alive after the join is alarmed `relay_thread_stuck`, and `WorkspaceService.close` waits for it (bounded
by the longest total deadline, 120 s) before it queues the job that closes the store. Jobs queued before `stopping` run
before that job (the worker FIFO). A drain (D-P2-4) stops the worker after the socket drain.

**Memory bound per linked workspace** (sum of the named caps, not an estimate): poll page 4 MiB, queue 4 MiB, 2 jobs in
flight × (4 MiB plaintext + 4 MiB sealed), publisher page 4 MiB + manifest 4 MiB, R4 page 4 MiB: **≤ 36 MiB**, so the 8-workspace cap bounds a host at ≤ 288 MiB. The
relay's largest set is never held in memory: it is built and sent in pieces.

### 11A.8 Clock

- `RelayWorker` takes the host's injected `clock` (wall, as `WorkspaceService.clock`) and a `mono` (monotonic) seam.
  Tests use fakes for both.
- **Wall clock** is used only for:
  - ORB2 `ts_ms` on responses;
  - the ±300 s request window (§8.6 step 8) and the start fence (11A.4);
  - certificate expiry (`verify_cert`) for live requests; evidence certificates are judged at their own `created_ms`
    (11A.9);
  - retention deadlines, stored as wall times at admission (`first_ms`). A record goes only when
    `clock() ≥ first_ms + period`: a clock set back delays deletion; a clock set forward can delete early, which is safe
    because the seq window is never deleted (11A.3);
  - the store's checkpoint `at` (`max(clock, last at)`, F1 §5.10) and restore stamping (`max(clock, C.o.at, last at)`,
    row 145).
- **Monotonic clock** is used for leases, renewal, re-login, total deadlines, `Retry-After`, backoff and the alarm timers.
  It is never persisted.
- **Never trusted from the relay:** time is never taken from a relay response, its `Date` header, `expires_ms`,
  `until_ms` or a checkpoint the relay serves (F1 C2).
- The host is the checkpoint writer, not a receiver of advancing offers. The +300 s ceiling (C2) applies to it only
  through the existing check of a computed restore `at` before append. The relay's `checkpoint_time` refusal is an alarm
  about the host's clock, never a reason to change `at`.

### 11A.9 Failure and threat model

| Actor | Can | Cannot (what the host verifies) |
|---|---|---|
| Hostile or compromised relay | drop, delay, reorder, replay within its window, **withhold** notices and evidence indefinitely, answer false refusal codes, serve an old H or an abandoned-branch H, cancel polls, serve different views to different devices | forge a request (sealed under `WK_e` and signed by `dk_sig`; the §8.6 order); make a request run twice while the host's state is not rolled back (11A.4); make the host degrade (needs a verified H that is neither contained nor explained by an accepted restore); make the host hold evidence it did not verify; obtain a person-label signature through the bridge's API; learn plaintext |
| Stale or forked relay (a restored backup, a split brain) | serve an older H or another branch | make the host lose data or degrade by itself: an ancestor H is a backlog, a pre-restore H is republished from the publication history (11A.5) |
| Host state rolled back (a backup restore) together with a consistent relay | everything both agree on | **not detectable by the host**: no independent witness exists in v1.7. Request replays are fenced by `ts_ms` (11A.4); devices that saw later checkpoints detect the rollback on their side (relay §7.6) |
| Compromised member device | read and act within its person's scopes until revoked; sign within its person's authority | act after the host holds verified evidence of its revocation (checked per request and again inside the job); get anything but its person's view. Before the host learns the revocation (the relay may withhold it), it is a live device |
| Code execution in the bridge, or a same-UID process | everything the WSK signs | out of scope here: §7 and 11A.2 "What this is not" |

**Unpublished evidence (R4, P3-D31 host MUSTs, relay protocol §19.5).**
- When to read: from the start after each new holder generation, after `cursor_ahead`, and daily; otherwise when
  `notices.unpublished` changes. R4 runs on the aux thread, never behind the publisher's staging.
- Each item is verified on its own, and every fact is **derived from the signed objects**, never from the item's outer
  fields:
  - `pk_pub` parses and `person_id == id(pk_pub)`;
  - the certificate verifies under `pk_pub` with `check_cert`, and with `verify_cert(cert, pk_pub, now_ms =
    cert.o.created_ms)` (valid at its own creation; an expired or old certificate is still evidence);
  - the revocation verifies under `pk_pub` against that certificate (`verify_revocation`);
  - `device_id = cert.o.device_id = revocation.o.device_id`, `person = id(pk_pub) = revocation.o.person_id`,
    `reason = revocation.o.reason`; the item's outer `device_id`, `person_id` and `reason` MUST equal them;
  - `provenance.digest == "sha256:" + sha256(cj(revocation))`.
- Holding is one worker job, `Store.note_forwarded_revocation(item) -> "held" | "pending" | "dropped"` (built in
  H12b, 11A.5 "As built in H12b"), under the store lock:
  - `store.state.device_person(device) == person` (associated): `HostPins.note_revocation(device, reason, revocation,
    at, cert, pk_pub, chain=False, is_member=state.is_member, owner=<pinned owner>)`; result `held` and a `needs` item;
  - not associated, but the person is a current or past member: held the same way as pending forwarded evidence (it
    refuses only that exact pair and is carried by no restore, as `Store._carries` already decides); result `pending`,
    no `needs` item;
  - otherwise, or when `note_revocation` returns `False` (its caps dropped it): `dropped`, logged with the pair.
- The ack of a page is posted only after every item up to its cursor has a result. `held` and `pending` mean
  `note_revocation` returned `True` and its write is durable; a `dropped` item is never reported as retained.
- **Effects.** Held evidence refuses that exact pair at once on the bridge (11A.4 step 3), and for a current member R6
  raises the rotation gate. The host **never** appends `device.revoked` or a restore by itself. It raises a `needs`
  item for the owners ("publish this revocation"), and publication follows only F1 §5.3, through the person path.
- A failed item is logged with its pair and the reason, and skipped. Unverified data never changes host state.

**Authenticity, not currency.** R1's readers (`GET card`, `members`, `checkpoint`) prove that an object was signed by
the right key for this workspace, not that it is the newest: a relay can serve an older card, an older WSK-signed member
list or a replayed older checkpoint, and `transitions_head` is an unauthenticated claim. R3 and R6 compare what they read
with local state (`link.json` `card_seq`, the host's own member list, the logs and the publication history).

**Log redaction.**
- Never logged: the token, the `Authorization` header, a challenge, a signature, an envelope, plaintext, key material,
  a page's heads, or an exception message that embeds any of them (exceptions from parsing are logged by class and
  route only).
- Logged: route, status, error code, `n`, `rid` (public), `device_id`, sizes and timings. URLs are logged as path
  templates.
- A test (the host's existing "no key byte in any log" pattern) asserts this for every R group.

### 11A.10 Tests and conformance

**Vendoring.**
- `tests/relay/vectors/` holds `vectors_v2.json.xz`, `http_fixtures/` with its `MANIFEST.json`, and a `SOURCE` file
  naming the relay commit, the MANIFEST revision and the sha256 of `vectors_v2.json` (the vector file has no revision
  field).
- The vector file is committed **xz-compressed only** (~0.4 MB instead of 23 MB; round 6, orch-52 #6), so git history
  does not keep another raw copy at each corpus bump. The sha256 in `SOURCE` is over the **decompressed** bytes, which
  equal the relay's `tests/vectors_v2.json`; `tests/relay/corpus.py` decompresses it and is the only reader (the crypto,
  canon and skeleton tests load through it). It checks that pin at every load, before any test uses a vector, and
  bounds the decompressed size at 64 MiB (a truncated stream or trailing data is refused too; round 7). `scripts/sync-vectors.sh <ref>` regenerates the `.xz`, the fixtures and
  `SOURCE` from an orch-relay clone.
- The drift guard copies the relay's `test_http_fixtures_manifest.py` checks: the shape; every file hashed, nothing
  added or removed; the aggregate equals the sha256 of the sorted `name\thash\n` lines; the canonical rendering.
- **Disagreement (resolved):** core vendored `tests/vectors/vectors_v2.json` at relay `04da0e7`, older than the
  relay's current file. R1 moves every loader to the one copy under `tests/relay/vectors/` and drops the old copy
  (round 6; its `encodings` and `labels` sections are identical in the new file). The
  deferred sections in `tests/crypto/test_vectors.py` (`bridge`, `relay_auth`, `p3`, `checkpoints`, `unpublished`, …)
  move to "consumed" as each group lands.
- Updating the corpus is one PR that bumps `SOURCE` and changes nothing else.

**Harness.** Two layers.
1. **Recorded-fixture mock (every PR, T0/T1).** An in-process `http.server` on loopback replays the vendored HTTP
   fixtures (request matched byte-exactly) and adds scripted faults.
2. **The real relay (required CI job, T2).** `uv run orch-relay serve --host 127.0.0.1 --port <port>` on a free port
   chosen first (the origin is fixed at start, so `--port 0` cannot work; 11A.15.12 C4; the started relay is identified by `ORCH_RELAY_INSTANCE` on `/healthz`), from the pinned
   orch-relay commit of `SOURCE`, with `ORCH_RELAY_ORIGIN=http://127.0.0.1:<port>` and a temp `ORCH_RELAY_DATA`, run
   **twice**: `ORCH_RELAY_MAX_PUBLICATION_BYTES=4194304` (the internal deployment) and the 16 MiB default. It runs
   link, a device request built by the relay's reference client (`ref/`), a publication with catch-up, a restore, and
   unpublished evidence. Mocks alone would only test core against core's reading of the relay.

The T2 real-relay job is not in R1; it MUST land before R2 or R5 merges. The R1 drift guard pins the relay commit in
`SOURCE` and checks the vendored copy against itself; it cannot notice the relay moving on (only T2, or the guard's
optional `ORCH_RELAY_DIR` cross-check, compares with the relay).

**Release gate: adversarial tests** (required for the group named; mock plus a proxy in front of the real relay).

| Group | Tests |
|---|---|
| R1 | wrong origin, kind and workspace in `auth_bytes`; a consumed challenge replayed; first link interrupted after each step (11A.5); `fenced` mid-publication; a second host instance taking over |
| R2 identity | two devices with the same idem; `outcome.get` across devices; a grant injected in the sealed meta; every forbidden op through every alias of the closed table; device removal, scope reduction (certificate and member list), certificate expiry and held evidence while the request is queued; a foreign self-certified pair for an existing device id; every `column.name` site (`test_column_r_sites`) |
| R2 exactly-once | a crash at every boundary of the state machine (before T1, after T1, after the outcome `begin`, between two appends, after T2, after `attempted`, after the 202); a write that fails with landed events; rid-only loss versus whole-state rollback with a replay (start fence); resend after tombstone expiry; exact replay after the relay's chunk expiry; a POST accepted with its response lost; a rotation after an uncertain POST; `rid_conflict` in each of its four cases |
| R3 | first-link histories above 4 MiB and above 16 MiB; a set crossing the cap only through framing; a crash after each intermediate publication; appends during catch-up; a queued restore (anchor); the cap lowered mid-publication; an oversized restore refused before signing where possible; a stale H before and after an authorized restore; a replayed abandoned-branch checkpoint; a false publication success followed by an empty relay; a relay returning `chain_diverged` falsely |
| R4 | outer `device_id`, `person_id` and `reason` substituted; an expired historical certificate; a cap rejection (`note_revocation` returns `False`); an item for an unassociated pair; concurrent evidence and appends; a crash before the ack; a forged item |
| R5 | pair versus convenience submit racing; a crash before and after the reservation; unlink and relink; a convenience event imported after the worker started; idle and busy shutdown; a stuck thread at close |
| transport | slow-drip headers and body; deep JSON; queue saturation; disk full and WAL recovery; a symlinked `rids.sqlite` or sidecar; 429 with and without `Retry-After`; a 3xx; an oversize body; a TLS failure; secrets in exception messages |
| signer | genesis and restore reviews swapped; an unknown material kind; a prohibited label; a permitted label in the wrong context (`RelaySigner` checks) |

**Review per group.** CI green (only `SUCCESS` counts), an Opus security review of every R group (auth, crypto,
exactly-once, evidence, the principal class), an Opus delta pass on every security fix commit before merge, and
orch-52's review of every group.

### 11A.11 Open questions (for orch-52 / orch-d6), with recommended answers

The recommendations came from the tie-break (11A.13). **orch-52 decided on 13 Oct 2026: all seven are accepted as
recommended** (PR #435 comment). Decided additions: relay-linked workspaces are capped at 8 per host (11A.4, 11A.7);
`decision` evidence is not served in v1.7 (orch-52 owns its F1 carriage as a later amendment); the start fence and the
600 s hold-off after a state restore are accepted; the convenience history rule stays strict in v1.7 (a workspace that
used click-to-sign cannot get a phone in v1.7; lifting it needs a reader-version handshake and an owner decision; flagged
to orch-d6, see §0.8.6 item 1; the owner decided it on 13 Oct 2026, design draft in §0.8.6 item 9); every R group gets
an Opus review plus an orch-52 review. Q5 is to be confirmed with
orch-mobile before R2 merges.

1. **L3 (§11): the bridge in the host process?** *Recommended: accept for v1.7, with the exposure stated:* bridge code
   execution is WSK compromise (11A.2 "What this is not"), mitigated only by the pure, size-capped codecs and the
   adversarial parser tests. A real boundary is the reserved out-of-process `bridge` class under a distinct UID with a
   signing service; same-UID separation would be fault isolation only.
2. **Catch-up checkpoints?** *Recommended: yes*, as specified in 11A.5 (exact counting, fresh final snapshot, dense new
   numbers above every existing `n`, atomic outbox replacement, anchors untouched). F1 §5.10 does not forbid
   intermediate prefixes; `Store.mint_catchup` and `Checkpoints.sign_prefix` must be added.
3. **R6 before R2 merges?** *Recommended: yes,* at least R6's complete security contract (11A.1), and no real device
   before R6 is implemented and integration-tested. The contract is 11A.15 (revision 1 of 15 Oct 2026).
4. **Convenience exception?** *Recommended: no* in v1.7 (11A.6).
5. **`rid_conflict` placement?** *Recommended:* once any POST of the original was attempted (a timeout included), its
   exact bytes are replayed; silence while the original has no answer; the unattempted cases as in 11A.4. To confirm
   with orch-mobile (Q14(2)). **orch-f1's answer (13 Oct 2026, the phone):** an old-epoch answer verifies through
   `readKey(e)` when the phone holds that key, else the request stays uncertain and is never re-signed; the phone seals
   under the head epoch only; on `rid_conflict` it restores the **original** attempt (bytes, `seq`, epoch), marks it
   durably uncertain and reconciles by reading state; it sends `idem = logical.hex` (mandatory for writes) and polls
   `outcome.get(idem)` before reconciling by state; `bridge_requests` are persisted in the transaction that allocates
   the `seq`; the resend window is 900 s minus a 600 s margin; a same-rid re-seal after a `stale_epoch` hint happens
   only within 240 s of the first copy.
6. **Column R writes: device-signed events only?** *Recommended: yes,* through a closed event/scope table, no fallback to
   unattended writes; `decision` evidence is a separate handler once F1 carries it (11A.12).
7. **Real-relay CI job?** *Recommended: required,* pinned to `SOURCE`, at both 4 MiB and 16 MiB.

### 11A.12 Gaps: what the relay expects that core cannot yet satisfy

- **Column R in the host:** the changes listed in 11A.4 (`RelayIdentity`, `Peer.kind "relay"`, `resolve_relay`,
  `Call.authorize`, `principal_of`, `NO_PARTIAL`, `append_signed`, the column-site audit).
- **Durable remote submission:** `relay/submit.py`, the `submission` journal, `relay_fields`, `relay.submit.recover`.
- **Forwarded evidence intake:** `Store.note_forwarded_revocation`. *Built in H12b* (11A.5 "As built in H12b").
- **Catch-up:** `Store.mint_catchup`, `Checkpoints.sign_prefix`. *Built in H12b.*
- **Decision evidence:** relay §13 has the host record a verified detached `decision` verbatim as F1 `evidence`, with
  every `decision_id` single-use forever across restores. Core's F1 model has no evidence carriage and nothing reads
  `sig_decision`. orch-52 decides the F1 shape; until then `decision` is not served.
- **First link:** the owner's relay account and delegation (P3-D34) come from an owner client; core has no client for
  them.
- **PoP on `PUT /v2/devices` and a device self-signature:** not required by the relay today (G1 recommendation). It
  needs an F1 certificate format change, planned with orch-52.
- **Genesis pin:** no person-path material kind exists, and `sig_genesis_pin` is in no custody role list.
- **Restore person op:** the store's transition machinery exists, but there is no `restore` op through `human.*`.
- **`dk_kx` private key:** it is discarded at `init` (`ops/workspace_init.py`), so the owner's existing device cannot
  receive sealed `WK` grants or card keys. R6 needs a new device key or a re-certification.
- **WK custody, `wk_commit`, member-list, grant, card and cert-request builders:** none exist (labels only).
- **ORB2 codec and `auth_bytes`:** not in core.
- **Outbox:** *built in H12b*: the hook in the append transaction, `Store.outbox_entries` and `Store.outbox_delete`.
- **`_relay_paired`:** probes `membership/` only (11A.6).
- **Relay 429 detail:** permanent and transient quotas are indistinguishable until the relay adds `quota`.
- **P3-D37:** merged (relay `7c0406a`). Since P3-D38 `relay-info` advertises `max_publication_bytes` (relay `438b719`
  `relay_info.py`); `link.json` keeps the configured cap (updated 15 Oct 2026 in the R6 revision, Opus L14).
- **Independent rollback witness:** none in v1.7 (11A.9).
- **Q14:** the number exists only in the orch-mobile/orch-29 numbering (`docs/gaps.md`), not in the relay docs.
- **R6:** the conflicts and gaps of pairing, membership and custody (`PK` not stored, `dk_kx` discarded, the rotation
  gate in R2, the order of R6a and R5) are listed in 11A.15.12.

### 11A.13 Challenge and tie-break record

The first draft (Opus) was challenged by Codex (a static review, not a demonstrated exploit); an Opus tie-break checked
each finding against the code (core `d020aea6`, relay `772d7b0`).

| # | Codex finding | Verdict | Evidence and change |
|---|---|---|---|
| 1 | Column R does not survive the dispatcher | **Confirmed** | `Call.authorize` resets `principal(peer.uid, …)`; `columns.resolve` and `Column.name` have no R. "Same dispatcher" withdrawn: a relay entry over the same backends, with the code changes of 11A.4. Codex's "unknown peer with a valid mapping becomes P" does not hold today (`_column` maps only `person` peers); `resolve` fails closed anyway |
| 2 | Unpublished evidence: outer-field substitution | **Confirmed** | `note_revocation` takes `device`/`reason` from the caller, defaults `chain=True`, returns a bool, has no lock; `pair()` is a record key. Facts now derived from signed objects, certificate judged at its `created_ms`, `chain=False`, new `Store.note_forwarded_revocation`, association from model state |
| 3 | Exactly-once overstated | **Confirmed** | `human.finish` ends `failed` with landed events; roll-forward needs a durable record. New state machine, submission journal (stated exception to the plaintext rule), no rid deletion, attempted-POST freeze, scope stated. Partly disagreed: backup replay is fenced by `ts_ms` (start fence), no witness needed for that case |
| 4 | Catch-up conflicts with high-water and outbox | **Confirmed** | `write_workspace` refuses a prefix behind its baseline; the relay refuses a lower-`n` incompatible target (`checkpoints.py`). Algorithm specified (11A.5), exact counting, `max_publication_bytes` in `link.json`, 4 MiB internal |
| 5 | Rollback/fork claims too strong | **Confirmed** | `find_divergence` would flag an abandoned-branch H after a restore. Classification table, publication history, limits stated |
| 6 | Key wrappers are policy | **Confirmed** | `BackendSigner._backend`, `FileBackend._load`. Claim qualified; `RelaySigner` typed methods; review binding for genesis and restore |
| 7 | Transport does not bound time, memory, shutdown | **Confirmed** | the relay renews the lease only at a poll start; `check_tree` returns a path and closes its fds. Total deadlines, aux renewal, byte bounds (≤ 36 MiB), checked SQLite open, shutdown sequence |
| 8 | Convenience lifecycle | **Partly** | the draft already checked in the critical section that writes `link.json`; ordering against network writes, unlink and post-start imports were missing and are added. No owner override |
| 9 | Contract gaps | **Confirmed** | changelog no longer claims "additive" without listing the internal contracts; decision evidence, R6 dependency, first card before login (`directory.py` `login` needs a card), old-epoch refusal exception, `max_publication_bytes`, retention clock all added |

### 11A.14 Phone person events over `relay.submit`

**Status: decided 15 Oct 2026 by orch-52** (#465). Drafted 15 Oct 2026, challenged by Codex, reviewed by orch-f1 and
orch-29, settled by an Opus tie-break whose recommendations were adopted in full (14.10). The core side is built (H12e, "As built in H12e" after 14.6); R2
builds it. Facts come from F1 §5.1–§5.6, §5.10, §11 and core at `9823cc69` (`model/engine.py`, `model/questions.py`,
`model/authz.py`, `model/lifecycle.py`, `canon/hashing.py`, `host/human.py`, `host/outcomes.py`, `store/store.py`,
`host/reads_ticket.py`, `host/reads_ws.py`). "MUST" is normative for R2 and for the phone.

**Scope.** Two event types, one event per `relay.submit`: `question.answered` and `ticket.created`. Both are ordinary
F1 person events under signed-event contract 1. Hashes, the signed-event contract, the schemas and the certificate are
unchanged. The only F1 change is the `id` row of §5.1 (14.7). The detached protocol §13 `decision` is not served
(11A.12): phones answer with `question.answered`. The bridge maps one relay op, `relay.submit` (route scope `decide`,
`relay/bridge.py`); `new_ticket` and `decision` are not in the bridge table (sealed `forbidden_scope`, 11A.4).

**14.1 The envelope a phone builds.** A person event always carries `sig`, `auth` and `roster_v` (F1 §5.2). The phone
sends the event **without** `seq`, `at`, `prev`, `ws_seq` and `host_sig`. The host chooses those at append, and the
person's signature does not cover them (F1 §5.3, §5.5, §12 N4). **Trust boundary:** the person's signature
authenticates the content and the target (workspace, ticket, type, payload); it does not authenticate the append time,
the placement in the log, consent to the latest view, or the surrounding authorization facts.

| Field | Value for a phone | Source |
|---|---|---|
| `v`, `hash_v` | `2`, `1` | constants |
| `id` | a fresh ULID (`^[0-7][0-9A-HJKMNP-TV-Z]{25}$`): 48-bit ms time from the phone clock, 80 random bits from the OS CSPRNG; admitted only within the window of 14.4 | the phone |
| `type` | `question.answered` or `ticket.created` | the action |
| `actor` | `{"kind":"person","id":"p_<32 hex>","device":"d_<32 hex>"}`, exactly the phone's certificate | the certificate |
| `auth` | `"secure-enclave-unlocked"`, or `"passphrase"` for Simulator/dev builds (14.3) | the custody |
| `roster_v` | the member-list version | host-sourced, person-signed: `ticket.get` → `members_v` |
| `based_on` | answer: the reviewed log head; creation: `null` (always `seq` 1) | host-sourced, person-signed: `ticket.get` → `doc.head.head` |
| `sig` | b64u of the raw 64-byte P-256 signature `r ‖ s` (CryptoKit `rawRepresentation`; high-s is accepted) | 14.2 |

**`question.answered`** (F1 §5.4.1, actor P). Exact payload: `question`, `hash`, then `option?` and/or `text?`, at
least one. Log: the ticket's `uid`. Who may answer: the `to` person or a holder of the `to` role, or an owner or
maintainer (`may_answer`); a viewer may answer (`VIEWER_MAY`).

| Field | Value | Source |
|---|---|---|
| `question` | `Q<n>` | `ticket.get` → `questions[].id` |
| `hash` | the question hash, `sha256:` + 64 hex | recomputed by the phone (14.5) |
| `option?` | an option `key` of that question | `questions[].options[].key` |
| `text?` | prose under F1 §11.3, at most 4 096 UTF-8 bytes | the person |

```json
{"actor":{"device":"d_9f3c…","id":"p_6aae…","kind":"person"},"auth":"secure-enclave-unlocked","based_on":"sha256:215b…","hash":"sha256:5d0e…","hash_v":1,"id":"01JA7Q…","option":"csv","question":"Q1","roster_v":3,"sig":"<b64u, 86 chars>","type":"question.answered","v":2}
```

**`ticket.created`** (F1 §5.4.1, actor A, signed when a person). Exact payload: `key`, `ticket_type`, `title`,
`owner`. **R2 is title-only**, as a person's `new` (H5a item 3): the phone hides Details, description and epic. A
`ticket.updated` table entry (summary, with `base_rev` and section hashes) is **R2.x**, tracked in an issue, not an R2
dependency.

| Field | Value | Source |
|---|---|---|
| `key` | exactly the key the store allocates next (14.6 L5) | `workspace.show` → `next_key` |
| `ticket_type` | `feature`, `bug`, `chore`, `spike` or `epic` | the person |
| `title` | one line, 1–200 Unicode scalars, F1 §11.3 | the person |
| `owner` | the actor's person id | the certificate |

Log: a ULID the phone makes **once per review**, the new ticket's `uid`. It is in the signed bytes (`log`), and every
attempt of that review keeps it (14.4).

```json
{"actor":{"device":"d_9f3c…","id":"p_6aae…","kind":"person"},"auth":"secure-enclave-unlocked","based_on":null,"hash_v":1,"id":"01JA7R…","key":"DEMO-0013","owner":"p_6aae…","roster_v":3,"sig":"<b64u, 86 chars>","ticket_type":"feature","title":"Check the tariff seeds","type":"ticket.created","v":2}
```

Both examples are `cj`, with ids shortened with `…` as in F1. The vectors (14.8) pin the real values.

**14.2 Signing.** One label, `orch/v2/sig/ticket-event|` (`sig_ticket_event`, in `DEVICE_KEY_LABELS`):

```
signed = "orch/v2/sig/ticket-event|" || cj({"contract":1, "event":E, "log":<ticket uid>, "suite":2, "workspace_id":W})
sig    = ECDSA-P256-SHA256(dk_sig, signed)   // raw r || s, b64u
```

`E` is the submitted event without `sig`. The signature covers `id`, `type`, `actor`, `auth`, `based_on`, `roster_v`,
`hash_v` and the payload, and binds the workspace and the ticket (`signatures.json`). `cj` is protocol §2.3 with F1's
depth limit of 16. Every string MUST already satisfy F1 §11.3: UTF-8, NFC with **Unicode 16.0** tables, LF and TAB
allowed, no CR in signed text, no other C0 control, no DEL, C1 or bidi control, and no code point unassigned in 16.0.
The host refuses with `validation.text` and never normalises a signed string. The phone runs the vendorable
`text.json` against its own normaliser and refuses a character newer than 16.0 before the sign sheet.

**14.3 `auth` on column R.** F1 §11.4 lists `passphrase`, `secure-enclave`, `secure-enclave-unlocked`, `convenience`,
`webauthn`, `tpm`, `windows-hello` (`AUTH_VALUES`, `common.json`). **Column R accepts exactly
`{"secure-enclave-unlocked", "passphrase"}`.**
- **The iPhone:** its `dk_sig` is a Secure Enclave key, `WhenPasscodeSetThisDeviceOnly`, no `.userPresence` (D49;
  orch-mobile P4, A6). F1 §5.3 names exactly this custody, `secure-enclave-unlocked`. `secure-enclave` would claim a
  presence check per signature (D41). The human act is the sign sheet (orch-mobile U7).
- **Simulator and dev builds:** a software P-256 `dk_sig` encrypted under a passphrase, decrypted for one signature at
  the sign sheet, never cached. This is F1 §5.3's `passphrase` custody, honestly declared. XCUITest types the
  passphrase. Such builds never claim `secure-enclave-unlocked`. Production builds compile only Secure Enclave custody
  (a provisioning rule: `auth` is a declaration, not proof, F1 §12 O1). Demo stays a separate realm (orch-mobile P3).
- **Custody is chosen at compile time (orch-f1).** The phone's Live realm uses the Secure Enclave on a device and the
  passphrase custody only in Simulator builds, behind that compile-time switch. A TestFlight or App Store build always
  signs `secure-enclave-unlocked`.
- **`convenience` is refused, also under any dev opt-in** (owner rule Q4 of 11A.11). `_signed_by` refuses it.
  `convenience.check` refuses it on every relay-paired workspace. One accepted convenience event writes the
  permanent `convenience_seen` mark and blocks every later reader. The host behaves the same in dev and production.
- Neither value needs the reader handshake (§0.8.6 item 9 concerns `convenience` only), and neither sets a mark.
- **`signer {device, auth}`** is not an event field. It is the H12d view field (§0.8.6 item 9.11). For a phone event it
  reads `{"device":"d_…","auth":"secure-enclave-unlocked"}`.

**14.4 Identity, freshness, outcomes and retries.**
- **No head race.** The phone never knows `seq`, `prev` or `ws_seq`. `based_on` must be the head of an existing earlier
  event of the log (or `null` exactly on `seq` 1), not the current head (F1 §5.1, `_envelope_check`). A head stays
  valid until a restore abandons its event.
- **idem = event id (host-enforced).** `relay.submit` requires `opts.idem` equal to the submitted event's `id`;
  otherwise `invalid.input`. The outcome key is `(ws, "r:<device>", id)`. With `actor.device` = the device (P4),
  the key **is** the event's identity per device. The same id with other content is `idem.conflict`. An identical
  retry gets the stored outcome (`duplicate:true`).
- **Re-seal under a new rid (amendment B, decided 15 Oct 2026 by orch-52).** The phone may re-seal the same signed
  event with the same idem under a new rid; the bridge is idempotent by idem. While that idem is admitted or running
  the answer is `pending`: no join, no second execution. The stored outcome replays later.
- **A same-rid resend after the chunk bytes expired (amendment D).** While the rid record is still alive, a resend
  with the same rid whose chunk bytes have expired is answered `already_done{status:"unknown"}` (class B).
- **Dedup by content, never by signature bytes.** P-256 signatures are randomized and high-s is accepted, so the host
  compares `digest = sha256(person_signing_bytes(ws, log, event))` plus `actor`. Under the lock (L3): the id logged with
  the same digest and actor → `committed`, `duplicate:true`, with its `seq`; with other content →
  `event.duplicate_id`; the id abandoned by a restore (in `lc.ids`, not logged) → `event.abandoned`. The outcome plan
  does not record the digest: the result builder (`relay_fields`) checks the identity (H12e-2).
- **Known identity first (amendment A, decided 15 Oct 2026 by orch-52).** A known identity is answered **before**
  the freshness check: first the outcome store by `(ws, "r:<device>", idem = id)`, then the log dedup by `(log, id)`
  plus the signed-content digest and `actor` (L3). Only a never-seen event gets the ULID window check
  (`event.expired`). Freshness never turns a committed write into a re-review.
- **Freshness window.** At job time (step W, after L3) the id's ULID time MUST lie in **[host now − 900 s, host now + 300 s]**;
  otherwise `event.expired`. 900 s covers delivery (signing needs a fresh `ticket.get`) and equals the relay's chunk
  deadline. A recovered admitted submission that runs after the window also gets `event.expired`. The phone does not
  send an outbox item older than 840 s; it re-reviews. Replay and readers never check the ULID time.
- **The invariant.** Outcome records live 24 h, much longer than the window. For the whole admissible life of an id,
  its outcome record, its log line or its abandoned id exists. So an id is single-use while the host's state directory
  holds its log line, its abandoned tail (`.state/abandoned/`, never deleted) or its outcome record. This replaces
  "single-use forever across restores" (orch-29 rewords protocol §13). After a state-directory rollback, the window,
  the start fence and the operator hold-off bound the exposure. **The 11A.4 hold-off after a state restore becomes:
  keep the relay worker stopped until the host clock is past backup time + 1 500 s** (+ 900 s window + 600 s). R2
  needs no new store. A store-level index of every admitted person id (F1 §5.10 follow-up) is an issue for the CLI
  path, not an R2 dependency.
- **One review = one uid.** A creation keeps its reviewed `uid` across every attempt. If an attempt that looked unlanded
  did land, the next one gets `ticket.exists` and the phone shows the existing ticket. A misclassification can never
  create a second ticket.
- **The four classes.** Only C and D are **definite**. An unknown code is D. The table is closed: a shared data file
  that both the R2 tests and the phone read, with a test asserting that every code R2 can emit is in it.
- **The table is authoritative (amendment C, decided 15 Oct 2026 by orch-52).** `retryable` on a relay answer is
  derived from it: A and B → `true`; C-auto, C-review and D → `false`. Placements it fixes:
  - `busy` with nothing run and no seq consumed → A;
  - `outcome.unknown_idem` → B before the bound (ULID time + 900 s + 300 s), C-review after it;
  - `already_done{status:"unknown"}` on a read → re-issue the read under a new rid;
  - `internal` → D, except after admission where effects are possible → B;
  - `event.expired` → C-review.

  | Class | Meaning | Codes | The phone |
  |---|---|---|---|
  | **A** not run | resend the same idem and the same signed bytes (a new rid and seq where the protocol needs them) | relay: unsigned 409 `stale_epoch` hint, sealed `stale_epoch`, `stale_timestamp`, `stale_sequence` (new rid), §8.6 step-4 silence or no answer; host: `timeout`, `cancelled`, `store.busy`, `lock.busy`, `rate.limited`, `quota.host`, `host.restarting`, `conn.overloaded`, `outcome.unknown` | resend |
  | **B** unknown | same idem only; resolve with `outcome.get`, then by a log read | `already_done{status:"unknown"}`, `rid_conflict`, `outcome.get` = `pending` (admitted, not run), `outcome.unknown_idem` before ULID time + 900 s + 300 s; `idem.expired` (resolve by the log only: answer → `questions[].answer.id` = own id; creation → `ticket.get(uid)` exists) | resolve; never re-sign |
  | **C** definite, zero landed | a stored `refused` (never `failed`; but see the H12e-2 note below: core records a refusal as `failed` with an empty landed list, and that counts as `refused`); a new signature is allowed | **C-auto** (14.4 re-sign may skip the review): `members.stale`, `ticket.key_not_next`. **C-review** (new review first): `event.bad_base`, `question.stale`, `event.expired`, `event.abandoned`, and `outcome.unknown_idem` after the bound when the log read shows the event absent | re-sign (C-auto) or re-review |
  | **D** terminal or landed | never re-sign for this review | landed: `committed` (incl. `duplicate:true`), any `failed` with landed events; question: `question.answered`, `question.unknown`, `answer.not_allowed`, `answer.bad_option`; authority and visibility: `role.denied`, `member.unknown`, `device.scope`, `device.invalid`, `device.unknown`, `ticket.frozen`, `freeze.active`, `ticket.not_visible`, `not_found`; collisions: `ticket.exists` (uid), `ticket.bad_reference`, `event.duplicate_id`; request: `forbidden_scope`, `op.not_allowed`, `sig.invalid`, `validation.event`, `validation.text`, `invalid.input`, `idem.conflict`, `idem.required`; the 11A.4 identity codes | show; stop |

- **`refused` is a reading, not a state (H12e-2, decided by orch-52).** There is no `refused` outcome step. A `failed`
  outcome whose landed list is empty and which was recorded under the lock after the log check counts as "refused":
  the class is decided by the error code (C or D by the table above), not by the state name. A `failed` with landed
  events is D. No new outcome state.
- **Auto re-sign (no new review), only when all hold:**
  1. the last attempt is C-auto: a host answer in state `refused` for that exact idem, and no earlier idem of the same
     review is unresolved (A or B);
  2. one fresh `ticket.get` snapshot (plus `workspace.show` for a creation) shows every **displayed** field byte-equal
     to the review: the recomputed `qid`/`hash`, the option or text, **and** `to`, `why`, `recommended`, `blocking`
     (stricter than the hash, because nobody re-reviews); for a creation the type, title and owner;
  3. only `roster_v`, `id` (and so the idem) and, for a creation, `key` change. `based_on` stays the reviewed one, and
     the creation keeps its `uid`;
  4. a creation's sign sheet said beforehand "the key is assigned when sent" (the consent for a changed `key`);
  5. the app stayed foreground and unlocked since the Sign tap;
  6. at most **3** auto re-signs per review, key races included. Then the sheet is re-armed. Shown as "The workspace
     changed; sent again (DEMO-0014)".
- **Phone persistence.** Before the first send the phone atomically persists `(idem = id, the exact bytes, uid, key,
  review id, first-send time)`. U8 (orch-mobile `decisions.md`) stays a phone rule: a changed answer, title or type is
  never re-signed without a new review. The outbox is dropped on re-pair.
- After a `duplicate:true`, the phone re-reads `ticket.get`: a stored `committed` can be stale after an F1 restore.

**14.5 Binding the answer to the question's content.** `question.answered.hash` **is** F1 §5.6's question hash. At
append the model compares it with the current hash of that question id; a difference is `question.stale`
(`model/questions.py answered`). The phone computes:

```
qid  = hex(SHA-256("orch/v2/question-id|" || cj({"question":"Q1", "ticket":uid, "workspace_id":W}))[0:16])
hash = "sha256:" + hex(SHA-256("orch/v2/question|" || cj({"options":options, "question_id":qid, "text":text, "ticket":uid})))
```

- `ticket` is the ticket **uid**, not the key. `options` is the question's list in its asked order, each
  `{key, label}` plus `cost` when present (a string). When the question has no options, `ticket.get` omits the field
  and the hash uses `[]`.
- The phone recomputes `qid` and `hash` from `doc.uid`, its paired workspace id and the displayed `text` and
  `options`. It shows the sign sheet only when both equal `ticket.get`'s `questions[].qid` and `hash`, and it signs
  the recomputed value. It renders every bound field, `cost` included, and never signs what it did not render.
  Labels it cannot render safely are refused (fail closed; the confusables UX gap is tracked).
- **Only `text` and `options` are bound.** `why`, `recommended`, `to` and `blocking` are not. A re-ask that changes only
  those keeps the hash and an existing answer stands (`asked`). The sheet shows them as unbound context, apart from the
  "Signed details" (`qid`, `hash`, `uid`).
- **An answer means "answer this content whenever eligible".** `asked` keeps an answer only while the hash is equal,
  so A → B → A clears it, and a delayed answer to A is then accepted if it is still eligible (`answered` rechecks
  authority). This is F1 semantics, not a forgery. It is bounded by the 900 s window. A question revision in the hash
  would need a new `hash_v`. Auto re-sign never crosses a context change (14.4 item 2).
- **Protocol §13** uses the same label and formula over F1 values, but core's values differ from host-api v0's: the
  uid (not the key), options `{key, label, cost?}` (not `{id, label}`), and `sha256:` hex (not b64u). The phone needs
  an F1 question parser.

**14.6 Admission (`relay/submit.py`).** Bridge T1 has already run (envelope, seq window, ±300 s, start fence, rid and
submission row, route scope `decide`, outcome `begin`). `HumanOps._judge` is factored into
`judge_events(store, log, events, *, git=None)`, shared with `human.submit`, with one shared test.

Checks before the lock (no state read):

| # | Check | Refusal |
|---|---|---|
| P1 | args `{"log": uid, "events": [exactly one]}`; `opts.idem` == `events[0].id` | `validation.event` / `invalid.input` |
| P2 | the type is `question.answered` or `ticket.created` | `forbidden_scope` |
| P3 | submitted-subset schema: `cj`-canonical, exact field set, no `seq`/`at`/`prev`/`ws_seq`/`host_sig`, `v` 2, `hash_v` 1, a creation's `based_on` null; F1 §11.3 text | `validation.event` / `validation.text` |
| P4 | `actor` = `{person, identity.person_id, identity.device_id}`, `sig` present | `op.not_allowed` |
| P5 | `auth` ∈ {`secure-enclave-unlocked`, `passphrase`} | `op.not_allowed` |
| P7 | `sig` verifies under the identity's `dk_sig_pub` over 14.2's bytes | `sig.invalid` |

One `store.locked()` span (re-entrant), held through the append and the outcome:

| # | Step | Refusal |
|---|---|---|
| L1 | `store.refresh()` | — |
| L2 | identity and scopes again on fresh state: the device is valid; member-list scopes ∩ `scopes_max` ⊇ `decide` (both types) | 11A.4 codes / `forbidden_scope` |
| L3 | dedup by event identity (14.4); the outcome store by idem has already answered a known idem (amendment A) | `committed` + `duplicate:true` (stop) / `event.duplicate_id` / `event.abandoned` |
| W | only a never-seen event: the ULID window (14.4, amendment A; was P6 before the lock) | `event.expired` |
| L4 | answer: the ticket exists and the person may see it; creation: the uid is unused | `not_found` (uniform for missing and invisible) / `ticket.exists` |
| L5 | creation: `owner` = the actor's person; `key` == `store._next_key()`, computed here after `refresh` | `invalid.input` / `ticket.key_not_next` |
| L6 | `judge_events(store, log, [ev])`: `model.preview` with `peek_stamp` (roster, base, hash, authority, scopes at the position) | the model's code |
| L7 | `outcomes.plan(intent, [Planned(log, id, type, actor)], key)`, fsync (no digest in `Planned`: H12e-2) | — |
| L8 | `append_relay(...)`: re-checks `_signed_by` and `logged_event`; the store re-verifies the signature and the certificate | its codes |
| L9 | classify: on an error, ask `logged_event(log, id)` **before** recording (landed → `committed`, else `refused` + code); `outcomes.commit` / `refuse` | — |

Then unlock, then bridge T2. `outcome.get` for an `r:` principal reports admitted-not-run as `pending`. The host never
fills in or changes a submitted field: what lands is what the phone signed, plus the host's stamp.

- **Keys.** A taken key is never the next key, so a key race is always `ticket.key_not_next` (exit 3, not retryable,
  C-auto). It carries `detail.next_key`, advisory and never a reservation. `ticket.exists` means only that the uid
  exists; for our own landed event L3 has already answered. `workspace.show.next_key` is `null` for a principal that
  may not create (a viewer, or no `decide`).

**As built in H12e-2 (15 Oct 2026, decided by orch-52 after the core review of #467).**
- **`orch.host.admission.device_standing(store, person_id, device_id) -> "ok" | "removed" | "unknown"`**: the
  liveness of a device in the F1 log (`device.added`, `device.removed`, `device.revoked` and tombstones, and a
  `member.removed` that removes the person's devices). R2's column-R `standing()` **must** use it (never
  `state._core`, never `device_person`, which also answers for a removed device); `"removed"` and `"unknown"` both end
  the standing. It takes the store lock itself (re-entrant), so it is safe inside R2's `store.locked()` span. Certificate
  expiry is not part of it (`verify_cert`). Every entry into the store lock re-checks freshness.
  **A removal is not durable across a restore:** a restore that abandons a `device.removed` revives the device (existing F1
  behaviour, bounded by the 11A.4 hold-off and `readers reset`), so a lost phone must be **revoked**, not just removed.
- **`judge_events` never lets a model exception escape**: any exception other than a model `Refusal` (`KeyError`,
  `TypeError`, `ValueError`... on malformed but signed input) is `HostError("validation.event")`, a definite D. The
  model's own refusal codes are unchanged, and `HumanOps._judge` behaves as before for valid input.
- **Identity check (L7, 14.9):** the O6 identity check lives in the `relay.submit` result builder (`relay_fields`);
  `Planned` carries no digest or key.
- **Recovery (R5):** `relay.submit.recover` runs in R2's `Bridge.start`, not in `WorkspaceService.open`.

**As built in H12e (core side, 15 Oct 2026).** Core now has everything the R2 handler calls from the step table;
R2 (orch-29, `orch/relay/submit.py`) owns the order, the outcome plan, `append_relay` and the classification.
- **`orch.host.human.judge_events(store, log, events, *, git=None, copies=None) -> None`** (L6), factored out of
  `HumanOps._judge`, which is now a thin wrapper (the prepared log and the request's bound copies); the person path
  behaves exactly as before. The caller holds `store.locked()` after `store.refresh()`; nothing is appended. Raises
  `HostError` with the model's code. A `source_sha` list needs `git` and fails closed without it (`internal`); the
  relay's two types carry none.
- **`orch.host.admission`** (re-exports `judge_events`), every helper re-entrant under the store lock:
  - `known_or_fresh(store, log, event, now) -> int | None`: L3 then W in that order (amendment A). Returns the
    `seq` of an identical landed event (answer `committed`, `duplicate:true`); raises `event.duplicate_id` or
    `event.abandoned` for a known id; checks the window only for a never-seen event (`event.expired`).
  - `dedup(store, log, event) -> int | None` and `lookup(store, log, id) -> Logged(seq, digest, actor) |
    "abandoned" | None`: the parts of L3. `signed_digest(ws, log, event)` is `"sha256:" + hex(sha256(
    person_signing_bytes))`, the digest R2's result builder compares for the O6 identity check (not recorded in `Planned`, H12e-2).
  - `check_window(id, now)` and `ulid_ms(id)`: the window, both edges included, in ms (`WINDOW_PAST_S` = 900,
    `WINDOW_FUTURE_S` = 300); a malformed id is `validation.event`.
  - `check_target(store, log, event)`: L4 and L5 (`not_found` with one message for missing and invisible;
    `ticket.exists`; `invalid.input` for another owner; `ticket.key_not_next` against `next_key(store)`, which is
    `Store.peek_key()`, `_next_key()` after the directory scan).
  - `COLUMN_R_AUTH` = {`secure-enclave-unlocked`, `passphrase`} (P5).
  - `lookup`, `dedup` and `known_or_fresh` take a ticket uid or `"workspace"`; `check_target` takes a ticket uid
    only. Anything else (a key, a number) is `validation.event` (review #474).
- **Order note (review #474, I-1).** L3 runs before L4, so a caller who already holds an event id of a ticket it may
  no longer see learns `committed`, `event.duplicate_id` or `event.abandoned` instead of `not_found`. This confirms
  only a uid the caller already knew (the id has 80 random bits); the order is decided (amendment A).
- **`ticket.key_not_next`** carries `HostError.detail = {"next_key": …}`. `detail` is an in-process attribute, not
  part of the socket `Error` (§0.3 has no `detail`): R2 decides how its relay answer carries it.
- **Codes.** `event.expired`, `event.abandoned`, `ticket.key_not_next` are in the §0.6 table and `HOST_CODES`
  (exit 3, not retryable: class C).
- **Reads.** `ticket.get` sends `doc.head`, `questions[].answer.id` and `members_v`, all under one store lock;
  `workspace.show` sends `next_key` (`null` for a viewer, no-person, column G and a column-R device without a valid
  `decide` certificate) (§0.8).
- **Vectors.** `tests/vectors/f1/phone_events.json` (`phone_events_vector()` in `tests/canon/oracle_f1_signed.py`,
  registered in `oracle_f1.py` and `index.json`, byte-checked by `tests/canon/test_f1_vectors.py`, its scenarios
  replayed through the real model by `tests/model/test_f1_scenarios.py`). `World.ev` gained `auth` (default
  `passphrase`) and `based_on`; every older file is byte-identical. `text.json` gained `lf_kept` and two code
  points unassigned in 16.0 but assigned in 17.0 (U+323B0 CJK Extension J, U+10940 Sidetic).

**14.7 F1 amendment (the only one).** F1 §5.1 `id`: "In P2 the host prepares it on the person path. On `relay.submit`
the signing device makes it (CSPRNG, 80 random bits), and the host admits it only within the window of host §11A.14.
Replay and readers never check the ULID time, and no rule orders events by it." The editorial §5.3 touch: "decrypted
only for one signature at the signer's prompt (a TTY or the phone's sign sheet)". Neither is a contract change.

**14.8 Vectors and tests.**
- **`tests/vectors/f1/phone_events.json`**, made by `phone_events_vector()` in `tests/canon/oracle_f1_signed.py`,
  registered in `oracle_f1.py` and `index.json`, and replayed through the real model by `test_f1_vectors.py`.
  `World.ev` gains an `auth` argument (default `passphrase`, so every existing file stays byte-identical). The file has
  its own `keys` block (TEST KEYS ONLY): `dk_sevp1`, a phone of `sev` with `scopes_max: ["look","decide"]`, added by
  `device.added` signed by `sev1`.
  - Accepted:
    - an answer with non-ASCII question text and labels, and an option with `cost`;
    - a text answer to a question with no options (`options` absent; the hash uses `[]`);
    - a creation (`DEMO-0002`, `based_on` null);
    - an answer with `auth: "passphrase"` from the phone device;
    - a high-s signature variant;
    - a TAB in answer text.
  - Each accepted case pins the submitted form, `person_signing_hex`, the signed-content digest, `sig`, the appended
    event and its `head`.
  - Refusals: `question.stale`, `members.stale`, `event.bad_base`, `sig.invalid`, `device.scope`, the same id twice
    (`event.duplicate_id`), a taken key at model level (`ticket.exists`), and an abandoned answer replayed
    (`event.duplicate_id`, cross-referenced with `questions.json`).
  - Semantics pins: a re-ask that changes only `why` keeps the answer; **A → B → A, then a delayed A answer, is
    accepted**.
- **`text.json`** is published as vendorable, with these cases:
  - refused: NFD input (never normalised), DEL, a code point unassigned in 16.0 but assigned later, bidi controls, CR
    in signed text;
  - allowed: LF and TAB.
- **Host tests (`tests/relay/`, R2):**
  - idem ≠ id;
  - the window edges (−900 s, +300 s);
  - `convenience` refused and `passphrase` accepted;
  - an extra host field, two events, a type outside the table;
  - `key_not_next` with `next_key`, and `next_key` `null` for a viewer;
  - `not_found` for an invisible ticket;
  - the same event under a new rid → `duplicate:true`; the same id with other content → `event.duplicate_id`; an
    abandoned id → `event.abandoned`;
  - admitted-not-run → `pending`; a crash after the plan and after the append, reconciled by digest;
  - a CLI `device.removed` between P7 and L2 → refused;
  - a re-sign with the same uid after a landed first attempt → `ticket.exists`;
  - the class table as data, asserted against every emitted code.

**14.9 What core adds with R2.**
- `ticket.get` gains `doc.head` (`{"seq", "head"}` of the log's last event), `questions[].answer.id` (the counting
  event's id) and `members_v`, all from one snapshot, so a review needs one read. `workspace.show` gains `next_key`.
  These view fields ship before the first phone release (§0.4).
- `relay/submit.py` (14.6), `judge_events`, the identity check in the `relay_fields` result builder (no digest in `Planned`).
- New codes: `event.expired`, `event.abandoned`, `ticket.key_not_next`.
- The +1 500 s hold-off (11A.4), the F1 §5.1 amendment (14.7), and the vectors and the class table (14.8).
- orch-29: protocol §8.7 (the one op) and §13 (single-use scope). orch-f1: the F1 question parser, `passphrase`
  custody for Simulator/dev builds, the persistence rule.

**14.10 Decision record.** Decided by orch-52 on 15 Oct 2026, after a Codex challenge (verdict FLAWED: no permanent
replay index, `failed` treated as zero effects, auto re-sign over signed fields, A → B → A, host stamps, no single
admission path, `next_key` atomicity, facts), the orch-f1 review (B1, B2, S3–S8) and the orch-29 review (O1–O10), and
an Opus tie-break adopted in full. Two structural fixes close most findings: **idem = event id** and **one review = one
uid**, both with the 900 s freshness window. Three conflicts were settled:
1. **Simulator `convenience` under a dev opt-in (orch-f1 B2): rejected.** It contradicts `_signed_by`, owner rule Q4
   and the permanent `convenience_seen` mark. The Simulator uses honest `passphrase` custody, accepted on column R.
2. **A window around `next_key` (orch-f1 S5): rejected.** A race makes the phone's key taken, and a window only allows
   counter jumps. Exact next key, `ticket.key_not_next` with an advisory `next_key`, and a fast auto re-sign that
   counts against the cap of 3.
3. **"The host must not check the ULID time" (orch-f1): rejected.** Without a schema change, the id's time is the only
   signed freshness datum. The host admits by it; replay and readers never check it.

### 11A.15 R6: pairing, membership, cards, WK custody (security contract)

Status: **revision 1 of 15 Oct 2026 (orch-29), docs only; orch-52 decides; Q1 is DECIDED B (owner, 15 Oct 2026, through orch-d6).**
The first draft (15 Oct 2026, `c00b4991`) was reviewed by Opus (SOUND WITH CHANGES), challenged by Codex (flawed as
written: durable authorization state, rotation/seal synchronisation, dev-script cleanup), reviewed by orch-52 (CHANGES,
docs only) and by orch-f1 for the phone (three blockers, eight should-fix items). This revision folds in all four and
orch-52's decisions of 15 Oct 2026. The record of who asked for what, and where reviewers disagreed, is 11A.15.17.

**Amendments of 15 Oct 2026 (decided; docs only).** Q1 = B (11A.15.16); Q16 = no; C7 resolved (§0.8.6 item 9.4
narrowed); Q15 = no relay change now (a pre-public item, 11A.15.14); dashboard pairing under the CLI's link rules and
the worker's internal API (11A.15.9A); one label rule (11A.15.10 item 4, 11A.15.13); three notes from #476's review
(11A.15.10 end).

This is the R6 security contract that 11A.1 and 11A.11 Q3 require before R2 merges. Ground truth:
- relay: orch-relay `develop` @ `438b719` (protocol v2 §4, §5.2, §5.3, §6.1, §6.2, §7, §8.3 to §8.6, §19.1 to §19.3
  and §19.8; P3 design §5, §8 and §9; P3-D3 to D11, D26 to D28, D30, D31 and D38; code `bridge.py`, `cert_queue.py`,
  `directory.py`, `members.py`, `checkpoints.py`, `relay_info.py`, `app.py`);
- core: `develop` @ `7ccbc46e` (with 11A.14 merged);
- R2: PR #467 @ `b00103ac` (`relay/keys.py`: `EpochKeySource`, `BridgeKeys`, `R6View`);
- the phone: orch-mobile @ `04da0e7` (orch-f1's review).

Where the relay protocol says nothing, the text says so and proposes a rule (11A.15.13). Where core and relay disagree,
the conflict is listed in 11A.15.12. **JUDGEMENT CALL** marks a choice this contract makes, always the fail-closed
reading. **DECIDED** marks a decision with its date and who made it. "MUST" is normative for R6, R2 and the H12c
builders.

#### 11A.15.0 Scope and the decided split

The split was decided by orch-52 and orch-f1 on 15 Oct 2026 (coordination log, 03:50). This contract confirms it,
with one correction (below):
- **R6 (stream D, `orch/relay/`)** runs the host side of pairing in the host's relay worker:
  - the offer and the invitation link, emitted **through a host CLI** (11A.15.9) and, under the same rules, **through the dashboard** (11A.15.9A,
    decided 15 Oct 2026, orch-52 with orch-fa);
  - the pair-request handling (proto §8.3), the cert request (§8.4 step 1) and the R6 allocation;
  - membership publication (§11 H1 to H8), WK custody, card keys and the WXK card rotation;
  - the `R6View`, `EpochKeySource` and seal gate that R2 reads.
- **The primary is a new person-level key pair, not the Mac's device key.** The coordination wording of 03:51 ("the
  person's Mac device key is the primary") is corrected here (orch-52 review): core's device keys are per workspace
  (`hosts/<ws>/person/dk.key.json`), so there is no single Mac device key, and the existing key's `dk_kx` is discarded
  at `init` (C2). The primary (11A.15.1) runs the primary steps of §8.4: it checks the request, signs the challenge,
  the certificate or the rejection, and uploads the certificate. It is an **owner client** on the person path, never
  code in the host daemon (D-P2-15: the host holds no device or person key). Where `PK` comes from is the owner
  decision Q1 (11A.15.16).
- **H12c (orch-52)** provides the F1, custody and object builders (11A.15.10). R6 schedules on them.
- **orch-mobile (the phone, orch-f1) pairs only as a non-primary device** of the workspace owner. In P3 every member is
  the owner (P3-D6, "lists have one person in practice"). A phone of another person cannot pair through a link: proto
  §8.3 step 3.5 refuses a certificate whose person is not the card owner (`other_person`), and the link pins the
  owner's `PK`.

**Not in R6:**
- scoped agent enrolment (§6.4);
- `drop_rewrap` (§7.5);
- the `vault`/PVK distribution;
- push;
- a second person (P8);
- the bridge op `member_remove` from a device (not in `RELAY_OPS` in v1.7: a removal is made on the Mac, through F1
  `device.removed`).

#### 11A.15.1 Parties, keys and who holds them

| Key | Holder in core | Used by R6 for | Status on `develop` |
|---|---|---|---|
| `WSK` | the host (`file` tier, `workspace` role) | signing lists, grants, cert requests, pair answers and cards through `RelaySigner` (11A.15.7) | exists |
| `WK_e` (one per epoch, ever) | R6, `membership/keys/` (11A.15.3) | grants (sealed), `K_bridge(e)` through `EpochKeySource` | **must be added** |
| `WXK_v`, `CK_s` | R6, `membership/keys/` | the card (`wxk_pub`, `ck_commit`) and the card-key wraps | **must be added**; R1's `LinkSteps.card()` already needs them (conflict C6) |
| `S`, `sas_nonce` (per offer) | R6, the offer record | `K_pair`, the SAS | **must be added** |
| `PK` (person) | **not stored**: `orch init` derives it from the recovery code and drops it (C8, `ops/workspace_init.py`) | the primary signs certificates, cert challenges and rejections with it (§5.2, §8.4), and derives the label key from it | **conflict C1**; the source is the owner decision Q1 (11A.15.16) |
| primary `dk_sig` | the person path (passphrase backend now, Secure Enclave later) | relay login as a device (D(p) session on the cert queue) | **must be added** (H12c item 9) |
| primary `dk_kx` (private) | the person path, **kept**, key type `kx` | opening the sealed `cert_request` inner (purpose `cert-request` only) | **must be added** (conflict C2) |
| the Mac's per-workspace `dk_sig` (F1 device) | the person path | signing F1 `device.added` for the phone (F1 §5.3: an existing device of the same person) | exists |

**The primary identity (DECIDED 15 Oct 2026, orch-52, Q2).** The primary is a **person-level** key pair, made once per
person by `orch keys primary init` (H12c item 9), or held by a separate trusted device (the "separate primary" option
of Q1). It has:
- `dk_sig` and `dk_kx` under person-path custody. The private `dk_kx` is **kept**, with its own key type `kx` and the
  fixed purpose set `["cert-request"]` in the authenticated file header, chosen at creation. It never opens `wk` or
  `card` wraps: the primary is never a member. A signing key never does ECDH, and the reverse holds too;
- a `PK`-signed certificate with `scopes_max: ["look"]` (least privilege: the relay checks only that the addressee is a
  registered, valid, non-drop device of the person, `cert_queue.py`) and `expires_ms: null` in v1.7 (11A.15.6
  "Certificate policy");
- registration on the relay as the account's device (`POST /v2/accounts` the first time, `PUT /v2/devices` after).

It is **not** an F1 member of any workspace and not in any relay member list. It only receives cert requests (D(p),
addressee) and signs with `PK`. The phone is the first owner member device of the first list (§7.2 check 4).

**How R6 learns and verifies the primary (Opus M7).** The host must address the request (`primary_device_id`) and seal
its inner to the primary's `dk_kx_pub`. If either came unverified (from the relay or an unchecked file), the inner
(the phone's keys, label, scopes, offer and attempt) could be sealed to a key the relay controls. So:
- the human hands the primary's `PK`-signed certificate to the host with the control op `relay.primary_set`
  (human-only, refused under `ORCH_GRANT`; `orch keys primary init` calls it, and for a separate primary the human
  passes the exported certificate file);
- R6 stores it verbatim in `membership/primary.json` with a **cleaned display label** given by the human (this is the
  `primary_label` of the `cert_pending` answer: local, unauthenticated display text, shown with "named by you");
- before **every** cert request R6 re-runs `check_cert` under the genesis `PK`, checks `person_id(pk_pub)` is the
  owner, that the cert is not `drop:` scoped, and that no revocation of it is known (relay feed and `HostPins`);
- `primary_device_id` and `primary_kx_pub` are taken **only** from that certificate. Without a valid
  `membership/primary.json`, `relay.pair_offer` refuses with `relay.no_primary`.

#### 11A.15.2 What R6 gives R2 (`EpochKeySource`, `BridgeKeys`, `R6View`) and how each is verified

R2 receives no raw key and writes no R6 state (11A.1, 11A.2). R6 builds the objects below and hands them to R2 at
worker start. **Only `relay/keys.BridgeKeys` may hold an `EpochKeySource`:** an AST test over `orch/relay/` refuses any
call of `.wk(` or `.refusal_key(` outside `keys.py`, and any import of R6's key store outside `r6/`.

**`EpochKeySource` (R6 implements; R2 PR #467 defines the protocol):**

| Call | Returns | Verified how |
|---|---|---|
| `current_epoch() -> int` | the **committed** epoch `e_c`: the epoch of the last list the relay accepted with a 2xx, for which R6 holds `WK_e_c` and whose `wk_commit` equals `H(ws, e_c, WK_e_c)` recomputed at load. `0` before the first committed list (R2 then has no members and serves nothing). Never `EHW`, never an exposed or pending epoch. **Informational only:** sealing uses the snapshot below, never this value | at load: `head.json` `committed` against the key file and `wk_commit`; any mismatch is H7 host-state loss (§11) and R6 refuses to start the worker (alarm `membership_state_unsafe`) |
| `wk(e) -> bytes \| None` | the key of an epoch `e ≤ e_c` that R6 holds: active, old or burned below `e_c`. `None` for `e > e_c` (burned or not), for an exposed but uncommitted epoch, and for a key past retention | the key file's epoch equals `e`; a held key's `wk_commit` must match every commit R6 recorded for `e` |
| **new: `refusal_key(e) -> bytes \| None`** | the key of an epoch R6 holds **only for a refusal**: an old epoch `e < e_c`, `e_c` itself while the gate is up, or a **burned** epoch above `e_c` (proto §8.5 G2b: "a burned key counts as held"). `None` otherwise | as `wk(e)`. Callable only from the typed refusal builders inside `BridgeKeys` (the AST test above) |
| **new: `seal_snapshot() -> ContextManager[SealSnapshot]`** | holds R6's **seal lock** for the duration of one seal, and yields `SealSnapshot(epoch, sealable, announce, burned_above)`: `epoch` = `e_c`; `sealable` = gate down and `e_c ≥ 1`; `announce` = the epoch a `stale_epoch` refusal names (`e_c` with the gate down, the rotation target with it up); `burned_above` = the set of burned epochs above `e_c` with the epoch each one's refusal names | the gate is raised **under the same lock** (below), so a snapshot can never be older than the gate |

**The seal gate, synchronised at seal time (DECIDED 15 Oct 2026, orch-52, G1; Opus M3, Codex 3).** The first draft
had a `seal_state()` that R2 read before sealing. That is a check-then-use race: R2 reads `sealable`, R6 raises the
gate, R2 seals new plaintext with its stale answer. And the gate lived only in R6's worker job, so an F1
`device.removed` or held evidence written by R4 left a window in which R2 still sealed under `e_c` for a device that
must no longer read it (proto §8.5 "DECIDED HERE", §7.2 step 4). The rule now:
- **One lock.** R6 owns a seal lock (a plain mutex: a seal is one chunk ≤ 64 KiB, so holding it is short). Gate
  activation takes it; every seal takes it through `seal_snapshot()`. `BridgeKeys` validates the request, seals the
  chunk **and commits the replayable response bytes** (11A.5's stored chunk) inside one `with seal_snapshot()`. A seal
  completed before the gate is an existing response (replayed byte for byte, never re-sealed); one that begins after
  the gate fails.
- **The gate goes up where the cause is written.** The store calls an R6 hook, `R6Gate.raise_for(device, reason)`,
  **inside** the critical sections that append F1 `device.removed`/`device.revoked` and that write held evidence
  (`Store.note_forwarded_revocation`), before the store lock is released. The hook takes the seal lock, sets the
  in-memory gate, and queues the durable write. Lock order: store lock, then seal lock; nothing takes them the other
  way round (a test pins it). **The converse direction (orch-52 re-check of beafc6c8, N5, 15 Oct 2026):** nothing
  inside `with seal_snapshot()` (the seal and R2's commit of the replayable bytes) takes the store lock. If R2's
  response store writes under the store lock, R2 takes the store lock **before** entering the snapshot, never inside
  it (R2 test `seal_snapshot_body_never_takes_store_lock`).
- **Durable before anything else.** The R6 membership thread writes the gate into `head.json` before any publication
  work. A crash between the in-memory gate and the durable write is safe: at load, before the worker starts, R6
  recomputes `desired()` from the store and raises the gate durably when `holders[e_c] ⊄ desired` (11A.15.4).
- **Relay-learned causes** (a revocation notice, a 403 `revoked`, `rotation_due`) raise the gate through the same hook
  from the R6 thread.
- **Thread-safety contract:** the in-memory gate is authoritative for sealing; the durable gate is authoritative across
  restarts; the in-memory gate is raised first, and lowered only after the durable write of the committed rotation.

**`BridgeKeys` invariant (DECIDED 15 Oct 2026, orch-52; Opus M4, Codex 3).** Enforced in one place, with **no flag**
that admits arbitrary payload:
- `BridgeKeys.seal_response(request, idx, body)` seals **data only**, and only when, inside the snapshot,
  `request.epoch == snap.epoch` and `snap.sealable`. It never accepts a `REFUSAL` flag: a body that would carry one is
  refused (`ValueError`, a programming error).
- Refusals are built by **typed refusal builders** inside `BridgeKeys`, which build the refusal body themselves from
  typed arguments and accept no caller bytes:
  - `seal_stale_epoch(request, idx)`: the `stale_epoch {epoch}` refusal (also the rotation's closing `LAST | REFUSAL
    stale_epoch`), with `epoch` taken from the snapshot (`announce`, or the burned epoch's named epoch below);
  - `seal_already_done_unknown(request, idx)`: `already_done {status:"unknown"}` (11A.5 rotation rule).
  These are the only paths that may use `refusal_key(e)`, and each validates its exact field set.
- Byte-exact replays of stored chunks are not new sealing. They never pass through a seal call.
- While the gate is up, R2 applies the rotation rule of 11A.5 with `epoch = announce`: open answers end with
  `seal_stale_epoch`; a request that ran without an attempted chunk gets `seal_already_done_unknown`; new requests at
  `e_c` get `seal_stale_epoch`. The phone then demands a list at `≥ announce` (§7.3 D5). If `announce` is later burned,
  the next commit is above it, so the phone's demand is still met.
- **JUDGEMENT CALL:** a `stale_epoch` names the pending target, never `e_c`: the device drops a `stale_epoch` that is
  not higher than its request's epoch (§8.5).

**A request under a burned epoch above `e_c` (C5, DECIDED 15 Oct 2026, orch-52: answer, do not drop).** The case is
reachable with an honest relay (a body at epoch E is exposed, the 2xx is lost, a list change supersedes it and burns E,
and phones already fetched E grants) and is G2b's explicit hostile case ("a phone pinned to a burned epoch by a
withholding relay gets the host-signed `stale_epoch`"). Rule:
- R2 opens it with `refusal_key(E)`, verifies the signature, and answers **only** `seal_stale_epoch` under E. Nothing
  else is ever sealed under E;
- the refusal names `exposed.new_epoch` when a body is exposed with an epoch above E, else `EHW + 1`. Either is met by
  the next commit (the H4 epoch floor: every list after a burn is above it), and it is higher than E, as §8.5 requires;
- §8.6 step 2 ("epoch > current: drop") is read as "above current **and not burned-held**: drop";
- host vector `request_under_burned_epoch_key_gets_signed_stale_epoch` (§11) and relay vector of the same name;
- orch-29 files the one-sentence clarification in protocol §8.5 with this reading (11A.15.12 relay doc items).

The first draft said "drop". Opus agreed with drop and noted the answer as the better-liveness alternative; Codex
called drop confidentiality-safe with an unbounded availability delay under a withholding relay. orch-52 chose the
answer; the dissent is recorded in 11A.15.17.

**What R2 must provide before #467 merges (DECIDED 15 Oct 2026, orch-52).** So that #467 is not reopened:
1. `EpochKeySource` gains `refusal_key(e)` and `seal_snapshot()` (the table above); `seal_state()` of the first draft
   is dropped.
2. `BridgeKeys.seal_response` refuses unless `request.epoch == snap.epoch and snap.sealable` inside the snapshot, and
   never accepts a refusal body or flag.
3. `BridgeKeys.seal_stale_epoch` and `BridgeKeys.seal_already_done_unknown`, typed, with exact fields, the only users
   of `refusal_key`.
4. The response store's commit of the replayable bytes happens inside the same `with seal_snapshot()` as the seal;
   nothing inside the snapshot takes the store lock (a store-lock write takes it before the snapshot, N5).
5. The burned-above rule: a request at an epoch in `snap.burned_above` gets `seal_stale_epoch` naming the recorded
   epoch; any other epoch above `e_c` is dropped.
6. The `R6Gate.raise_for` hook signature and the store-lock-then-seal-lock order, with a fixture `R6Gate` for R2's
   tests (R6 implements it).
7. Tests: deterministic gate/seal interleavings (the gate raised between validation and seal, between seal and commit);
   arbitrary payload with a refusal marker refused; data under a burned or old key refused; a burned future-epoch
   request answered `stale_epoch` with the named epoch; the AST test for `.wk(`/`.refusal_key(`;
   `seal_snapshot_body_never_takes_store_lock` (N5).

**`R6View.member_list() -> MemberList | None`.** R2 reads `epoch`, `list_hash` and `members[device] = Member(device_id,
person_id, scopes, cert, pk_pub)`. Each field is verified as follows, **never against what the relay serves**:
- **The list** is the host's own committed list. It is the exact `cj(o)` that R6 exposed (H2) and that the relay
  answered 2xx, re-verified at load: the WSK signature, `workspace_id`, `list_seq == committed.list_seq`, `epoch == e_c`,
  `wk_commit` against the local key, and `list_hash == "sha256:" + hex(sha256(cj(signed list)))`.
  - `GET members` is read only to detect divergence (H3, H7). A relay-served list never becomes the `R6View`. A
    relay can serve an older or another WSK-signed list (11A.9 "Authenticity, not currency"); the host never adopts
    one.
  - An empty committed list (11A.15.4) gives a `MemberList` with no members: R2 serves nothing.
- **`scopes`** are the list entry's. At build time, the builder checked that they are a non-empty prefix of
  `["look","decide","operate","type"]`, no longer than the certificate's `scopes_max` and equal to the offer's
  `scopes` (11A.15.6). R2 checks the op against both the list's scopes and the certificate's (11A.4).
- **`cert`** is the certificate held in `membership/devices/<device_id>.json`, verbatim. It was verified at the
  allocation: `check_cert` under `pk_pub`; `device_id(dk_sig_pub)` equals the entry's id, and `person_id(pk_pub)`
  equals the entry's `person_id`; its keys equal the pending pairing's keys; not a `drop:` scope. R2 re-runs
  `verify_cert` at its own clock per request (expiry, revocations).
- **`pk_pub`** is the owner's `PK` from the **genesis delegation** in the workspace log (`check_delegation`), never
  from the relay, the account or the pair answer. In P3 every entry's `person_id` is the owner's. An entry of another
  person makes `member_list()` raise (fail closed) until P8 defines multi-person membership.
- **Device and person association.** R2 adds the F1 check (`store.state.device_person(device) == person`, not removed,
  not revoked, no `HostPins` evidence for the exact pair; 11A.4 step 3). R6 includes a device in a list only when that
  F1 check passes at build time (11A.15.4). R2's check is the per-request recheck, not the only one.
- **Burned epochs** are not in `R6View`. They reach R2 only through `refusal_key(e)` and `snap.burned_above`.
- **The reader reservation for a pairing (11A.6)** is not an R2 concern. R2 hands every `route:"pair"` poll item to
  `R6Pairing.handle(item)` (below) and never opens epoch 0 itself. Until R6 is wired, R2 keeps dropping pair items and
  never acknowledges them out of order (11A.5).

**`R6Pairing` (new interface, R6 implements, R2 calls):**
- `handle(item: PollItem) -> None` puts a pair item on the **pair thread's** bounded queue (below) and returns at once.
- `completed(n) -> bool` tells R2's cursor rule whether item `n` is durably handled. A pair item is handled once its
  offer record transition (or its "dropped" decision) is durable. A **silent drop** (unknown offer past its tail, bad
  tag, queue overflow) counts as completed at once, with no write: the drop is counted in memory and in the
  `pairing_dropped` alarm counter (durably, at most once per minute). `after` advances only over completed items (§11,
  P3-D10).

**Threads (DECIDED 15 Oct 2026, orch-52, Q7 as revised; Opus M5).** The first draft put pairing, membership I/O and
cert-request polling on one thread. A pair request arriving during a 5-minute H8 backoff or a 2 MiB `PUT membership`
retry would then get no answer within 60 s, and the honest phone would show "this link was used by someone else"
(proto §8.3 step 4): a false alarm that trains people to ignore the one alarm that matters. R2's cursor would also
stall behind the pending item. So R6 runs **two** threads per workspace, each with its own relay connection:
- **`orch-relay-r6m-<id8>` (membership):** publication (H1 to H8), backoff, the card rotation, revocation polling.
  It may block on long I/O.
- **`orch-relay-r6p-<id8>` (pairing):** consumes pair items and drives the offer state machine. It is **event-driven**:
  no sleep, timers instead of waits; every relay call has a total deadline of 5 s; cert-request progress is checked on
  a `notices.cert_requests` wake (the relay lists request ids with a challenge, cert or rejection, `bridge.py`) with a
  fallback `GET cert-requests/{id}` at most every 10 s. **Latency budget:** an item is answered (or silently dropped)
  within 10 s of reaching the queue, far under the phone's 60 s. Its queue holds at most 64 items; overflow is a
  silent, counted drop (the relay's own budgets cannot be credited against a hostile relay, Codex 2). Parsing work per
  item is bounded by the 2 KiB-plus-margin meta cap before parsing.
- The pair thread never waits on the membership thread: it posts "publish the addition of D for offer O" to the
  membership thread's queue and receives "committed `{list_seq, epoch, adds}`" or "refused" back as events.
- Consequences: 6 connections per workspace and 48 relay threads at the 8-workspace cap (11A.7); the memory bound gains
  2 MiB (one membership body) plus 64 × 64 KiB for the pair queue per workspace: ≤ 42 MiB, so ≤ 336 MiB per host.

#### 11A.15.3 State (extends 11A.3)

R6 owns `<state>/hosts/<ws>/membership/` (0700, host UID, in the D-P2-17 custody list and `check_workspace_files`).
Its existence already makes `_relay_paired` true (11A.6). Files are written with `host/custody.write_private`. Key
files and signed objects are **claimed write-once** (`os.link` of a fsynced temp file, as R1's `PersistedOnce`).
**Writers:** `head.json`, `keys/`, `grants/` and `cards/` only by the membership thread; `pairing/` only by the pair
thread, except the allocation critical section (11A.15.6 step 9, under the store lock, which the pair thread waits
for) and the load-time recovery (before both threads start).

| File | Contents | Secret | Crash rule |
|---|---|---|---|
| `head.json` | `{v:1, lhw, ehw, committed:{list_seq, epoch, list_hash, wk_commit, list, at_ms} \| null, exposed:{list_seq, epoch, new_epoch, body (exact cj bytes, b64u), digest, adds:[device_id], first_send_ms, attempts} \| null, burned:{epochs:{"<E>": named_epoch}, list_seqs:[…]}, holders:{"<e>":[device_id…]}, excluded:{device_id: {reason, at_ms}}, gate:{up, reasons:[…], since_ms, target} \| null, h8:{count, alarm}, unmatched_rotation_ms, card:{card_seq, wxk_version, ck_seq, pending_card_seq, wxk_retired:[{version, retired_ms}]}}` | no | the **one** atomic write of H1/H2: the allocation, the signed list, the holder-set additions, the exclusions and the burns land together. The rest of the rows say what precedes it |
| `keys/wk-<e>.json` | `{v:1, epoch, wk, created_ms}` | **yes** | claimed **before** the `head.json` write that allocates `e`. **Recovery:** at load, `ehw := max(head.ehw, every wk-* epoch)`. A key file whose epoch `head.json` never exposed or committed is an **orphan** and is treated as burned (never activated, never re-generated) |
| `grants/<e>/<device_id>.json` | the exact signed `wk_grant` for that device and epoch | no (sealed) | claimed before the `head.json` write that exposes a body containing it. **A same-epoch addition reuses every persisted grant and creates only the missing one** (Codex 2): the signer ledger refuses other bytes for `(device, epoch)`, and the relay compares exact stored grant bytes (`members.py`) |
| `keys/wxk-<v>.json`, `keys/ck-<card_seq>.json` | the WXK private key of version `v`; the `CK` of a card | **yes** | claimed before the card that names them is signed. An orphan `ck` or `wxk` file is never reused for another card |
| `cards/card-<s>.json` | the exact signed card of `card_seq` `s` (Opus L3) | no | claimed **before** the first `PUT card` and resent byte for byte (as R1's `link-card.json`). A crash between the signature and `head.json` resumes with these bytes; `RelaySigner.card` would refuse a rebuild |
| `devices/<device_id>.json` | `{v:1, device_id, person_id, cert, label, offer_id, paired_ms, decl_digest}`: the relay-reachable devices (paired through R6) | no | write-once at the allocation (11A.15.6 step 9) |
| `primary.json` | the primary's verbatim certificate and the human's display label (11A.15.1) | no | written by `relay.primary_set` only |
| `pairing/<offer_id>.json` | the offer record (11A.15.6): `state`, `S`, `sas_nonce`, `scopes`, `created_ms`, `expires_ms`, `phone_deadline_ms`, the consumed request's `{device_id, dk_sig_pub, dk_kx_pub, attempt, label, cert?, decl?}` (the **first** stored declaration wins, phone S4), `request` (`{request_id, bytes, expires_ms}`), `challenge` (exact bytes, once stored never changed), `cert`, `intent` (11A.15.6 step 9), `event_id`, `publication` (`{list_seq, epoch, list_hash}` of the acknowledged body), `baseline`, `answer` (the approved fields) | **yes** (`S`, `sas_nonce`) | rewritten atomically **before** every answer or network write that depends on the transition. `S` and `sas_nonce` are dropped at a terminal state plus 10 min. The record shrinks to a tombstone `{offer_id, state, device_id}` after 7 days (the baseline life) |
| `pairing/<offer_id>.answers` | an append-only side file of `{rid, at_ms, chunk bytes}` for every answer and refusal sent (Opus L1) | no (sealed) | appended and fsynced **before** the POST; entries older than 900 s (the relay's rid lifetime) are pruned when the file is rewritten at a state change. Refusal bytes are kept too, so a resend after a crash replays them instead of re-sealing (which the relay would answer `chunk_conflict`) |
| `pairing/outstanding.json` | the open relay cert request of this workspace, if any: `{request_id, offer_id, expires_ms}` (Codex 2, Opus L2) | no | written before the cert-request POST; cleared when the request is signed, rejected or past `expires_ms + 60 s` |
| `alarms.json` | the R6 alarms (`membership_*`, `pairing_*`, `card_*`) shown in `host.status.relay` | no | rewritten atomically |

**Retention of epoch keys (JUDGEMENT CALL, Q8).** In v1.7 R6 **never deletes** an old or burned `WK_e`. The protocol
keeps old keys "to read old content" and holds burned ones like old ones (§7.2), and a deleted key turns a phone's
`stale_epoch` into silence (C5 needs them). The cost is 32 bytes per epoch, bounded by the clamp. `WXK_v` is deleted at
`retired_ms + 14 days` (§7.4). `CK_s` is deleted once a card with a higher `card_seq` is accepted.

**Never persisted:** derived `K_pair` and `K_bridge`, the cert-request inner plaintext, a device's label before
cleaning, session tokens.

#### 11A.15.4 Membership publication from R6's side (§11 H1 to H8)

§11 states H1 to H8. Here is how R6 applies them, and what the list contains.

**The desired list (JUDGEMENT CALL, Q12: relay-reachable devices only).** `desired(state)` is computed under the store
lock, in a membership-thread job, from:
- the devices in `membership/devices/`;
- minus every device that is not a current valid F1 device of its person (removed, revoked, or `HostPins` holding
  evidence for the exact pair);
- minus every device the relay reported revoked (poll `notices.revocations_cursor`, `rotation_due`, R4's unpublished
  feed, a `revoked` refusal);
- minus every device whose certificate has expired at the host clock;
- **minus every device in `excluded`** (Opus M2, Codex 2).

**The exclusion set (quarantine).** `head.json` `excluded` is durable relay eligibility, separate from F1 membership.
A device enters it in the same atomic write as the event that caused it:
- a content refusal of the publication that added it (`other_person`, `scope_exceeded`, `cert_invalid`, `revoked`,
  `cert_expired`, including a hostile relay's false refusal), with the burn and the offer's `failed` transition;
- a pairing abandoned or expired after the allocation (11A.15.6 deadlines and `abandon`).

`desired()` **never re-adds** an excluded device. It leaves `excluded` only by an F1 `device.removed` (then it is gone
anyway), by a **fresh pairing** with new keys (a new device id), or by **re-admission through a new approved offer**
(below). A quarantined device never reappears without new human authorization, and the owner gets the `needs` item
"pairing of <label> failed after it was added: remove the device".

**Re-admission (DECIDED 15 Oct 2026, orch-52 re-check of beafc6c8, N2).** Without it, one false content refusal from a
hostile relay would bar a phone from the workspace for good: step 9 refuses a removed id (`revoked`), and a phone that
holds any other workspace cannot draw fresh keys. So a **new offer** for the same workspace, approved by the human
through the typed SAS (step 6), with the phone **presenting its existing certificate** (the same keys, path 7a), clears
the exclusion inside the allocation critical section (step 9): the "Already in F1" rule makes the allocation idempotent,
and the same atomic write removes the device from `excluded`. This is new human authorization, as the quarantine rule
requires; the holder invariant still forces a rotation when it applies. The same path clears an exclusion caused by
`abandon` or an `expired` offer. Vector `excluded_device_readmitted_only_by_new_approved_offer`.

**The holder invariant (Codex 3).** On **every** publication, including a supersession and a failed pairing,
`holders[e_c] ⊆ desired` must hold for the body's epoch. If any holder of `e_c` (a device that was ever sent a grant of
`WK_e_c` in any body R6 exposed, accepted or not) is not in `desired`, the body is a **rotation**, never a same-epoch
list. This replaces the first draft's list of refusal examples: it is the one rule, enforced in the planner and
re-checked by `member_list_o`'s caller before signing.

The Mac's own F1 devices are **not** listed: they are reached through the socket, their `dk_kx` may not exist, and
granting them `WK` would only add holders. `persons` is the set of the listed persons (in P3, `[owner]`).

**Empty lists (corrected, Opus M1).** The first draft said the relay refuses an empty list. That holds only for the
**first** list (`members.py`: the owner-device check runs only when there is no previous list). So:
- the **first** publication needs at least one device: R6 never publishes an empty first list;
- **after** the first list, when `desired` becomes empty (the last phone is removed or lost), R6 publishes the
  **empty rotation**: a fresh epoch, `persons: []`, `members: []`, no grants. This makes the relay evict the last
  device (its D(m) reads of checkpoints, transitions and proofs, its mailbox and grants) instead of leaving it a member
  until some later list;
- H12c builder 6 takes `first: bool` and refuses an empty list only when `first` is true.

**When R6 publishes.** One body is exposed at a time (H2). A **list change** is a difference between `desired` and the
committed list:
- **the first list** (Opus L7): a fresh `WK` at `EHW + 1` (in `[1, 2^16]`), the rotation path, with no gate;
- **an addition only** (a pairing) and the holder invariant holds: same epoch `e_c`. The persisted grants of the
  existing members are reused; only the new device's grant of `WK_e_c` is made, and the device joins `holders[e_c]`;
- **any removal or exclusion** (F1 `device.removed`/`device.revoked`, relay revocation, held evidence, certificate
  expiry, a quarantine): the gate is already up (11A.15.2). The list rotates to the target `min(EHW + 1, committed +
  2^16)`, with a fresh `WK`, grants for every remaining device, and card-key wraps for the new devices;
- **the 90-day age, `orch keys rotate`, or no local key for `e_c`**: rotation (§7.2), plus H7 when the key is missing;
- **an unverified `rotation_due`** (one R6 cannot match to a verified revocation; Opus L6): a rotation, but at most one
  per 24 h (`unmatched_rotation_ms`); more are alarmed `membership_rotation_due_unmatched` and ignored, so a hostile
  relay cannot burn through the clamp margin;
- **a change while a body is exposed** supersedes it (H4). The exposed body is abandoned and its `list_seq` burned,
  and its epoch too if it introduced one. The next body is planned from the new `desired` under the holder invariant.

**Write-ahead (H1/H2), in this order:**
1. under the membership thread (one writer): compute `desired`, the allocation `list_seq = LHW + 1`, and, for a
   rotation, `E = target`;
2. for a rotation, generate `WK_E` and **claim `keys/wk-<E>.json`** (fsynced);
3. build and sign: `wk_commit` (H12c), the list `o` (H12c) signed by `RelaySigner.member_list` (11A.15.7), the
   missing `wk_grant`s (sealed to each `dk_kx`, claimed in `grants/<E>/`), and the card-key wraps for devices new to
   the current card;
4. **one atomic `head.json` write**: `lhw := list_seq`, and for a rotation `ehw := E`; `exposed` = the exact body
   bytes; the holder-set additions; any burn this body supersedes (with its named epoch, 11A.15.2 C5);
5. only then the first send (`PUT membership`, W(g), `Orch-Holder`).

**Outcomes (H3/H4, as §11; R6 specifics):**
- **2xx** (`{list_seq, epoch}` equal to the body's; any other answer is `relay_misbehaving`, treated as transient and
  alarmed): one atomic `head.json` write sets `committed` to the body, clears `exposed`, lowers the gate if the
  rotation excluded every gated device, and resets the H8 count. Under the seal lock, the in-memory gate is lowered and
  `e_c` moves. The pair thread receives "committed" with the exact `{list_seq, epoch, list_hash}`.
  - **A 2xx is not proof against a hostile relay** (Codex 2): it may have kept nothing. The approval names the exact
    locally persisted publication that the relay acknowledged, and nothing stronger is claimed.
  - A late 2xx for a body R6 already abandoned is ignored: a burned epoch is never activated on a late
    acknowledgement (§11, Codex 3).
- **Transient** (a lost response, a connection error, a timeout, 429 `rate_limited`, 5xx, a 401 then re-login,
  `fenced` then an explicit re-hold): resend the exact bytes. After a re-login or a re-hold, first `GET members`,
  verified with the WSK. If the list is above `LHW`/`EHW`, or not a body R6 exposed, apply H7.
- **429 `quota_exceeded`** (4 096 ever-devices, P3-D31): permanent. Abandon the body (it burns, as a content refusal),
  alarm `membership_quota`, and refuse new pairings for this workspace (`relay.quota`).
- **Container-only** (`stale_card`, `wrap_conflict`): rebuild only the wraps (for `stale_card`, after R1's verified
  `GET card`) and resend the **same** signed list and grants. Nothing is burned.
- **Content or divergence refusal** (the H4 list): burn, then plan again. Every device the refused body newly granted
  is already in `holders[e]` (H5), because a relay may have stored and distributed the body before reporting failure.
  A device added by that body enters `excluded`; the next plan rotates without it by the holder invariant
  (`same_epoch_add_refused_revoked_forces_rotation`), and the pairing ends `failed` (11A.15.6).
- **H8:** the refusal counts and the 10-minute uncommitted-rotation alarm as §11, in `head.json` `h8`. Backoff never
  lowers the gate. The clamp freezes instead of reusing an allocated target (§11).

**H7 (host-state loss) from R6's side.** R6 treats each of these as host-state loss:
- `head.json` missing, unreadable or torn while the WSK and `link.json` exist;
- a key file for `e_c` missing;
- a verified relay list above `LHW`/`EHW`;
- a relay list at a `list_seq` R6 exposed but with other content;
- R1's preflight reporting a restored or copied state directory.

R6 then does exactly §11 H7: a verified `GET members`, then `LHW`/`EHW` raised by 1024 above the larger value, then an
immediate rotation. `LHW` is also raised above every `member_list` entry in the signer ledger, so signatures made before
a lost head write are covered. **The device registry survives a lost `head.json` only if `devices/` survived.** When
`devices/` is lost too, R6 publishes nothing, alarms `membership_devices_lost`, and the owner re-pairs the phones. A
lost `excluded` set is rebuilt conservatively: every device whose offer record is not `approved` is excluded.

**Revocation.** R6 learns a revocation from:
- the poll notices `revocations_cursor` and `rotation_due` (read through R1's session: `GET persons/{p}/revocations`,
  verified under the pinned `PK` with `verify_revocation`);
- R4's held evidence (the store hook, 11A.15.2);
- the F1 log (`device.revoked`, `device.removed`; the store hook);
- a 403 `revoked` on a chunk POST (R2 reports it to R6 through the hook).

Each one, verified, **raises the gate** (11A.15.2) and triggers a rotation. R6 **never appends** `device.revoked` or
`device.removed`. A revocation that F1 does not yet carry gets the `needs` item of 11A.9 ("publish this revocation").
There is no un-revoke: the device pairs again with new keys.

**What rotation cannot do (Codex 3).** A compromised phone keeps every key it received; rotation protects future host
ciphertext, not past. A colluding relay can keep other phones on an old epoch by withholding updates (P3 design §8).
Persisted floors prevent rollback below learned state, not indefinite staleness.

#### 11A.15.5 Cards: WXK rotation and card keys

- **The first card** (R1 first link, `LinkSteps.card()`): `WXK_1` and `CK_1` are claimed in `keys/` before the card
  is signed. The card's sealed part is `{name, description, capabilities, hosted_by: null}`, with `client_hosted:
  false` from the delegation. This custody is needed **before R5** links anything (conflict C6, R6a).
- **Card-key wraps.** `CK_s` for the current `card_seq` is sealed (purpose `card`) to each device new to that card,
  inside its membership body (`card_key_wraps`, P3-D7). The relay accepts wraps only for current-list members and the
  current `card_seq`.
- **WXK rotation (§7.4: with the 90-day epoch).** Each step is one durable state of `head.json` `card`:
  1. the membership rotation commits first (it may carry wraps for the **current** card);
  2. claim `wxk-<v+1>` and `ck-<s+1>`, build the card (`wxk_version + 1`, `card_seq + 1`, a fresh `CK`, `ck_commit`,
     the sealed part), sign it with `RelaySigner.card` (its ledger refuses a second card at a `card_seq`), **claim
     `cards/card-<s+1>.json`**, record `pending_card_seq`, then `PUT card` (W(g)) with those bytes;
  3. `PUT card-keys {card_seq: s+1, wraps}` for every current member, in batches under 64 KiB;
  4. record `retired_ms` for `WXK_v` (deleted after 14 days) and drop `CK_s`.
  A crash anywhere resumes from `pending_card_seq` and the stored card bytes (Opus L3).
- **Card outcomes.**
  - `stale_card` on `PUT card`: verified `GET card`. If it holds our `s+1` card, continue. Otherwise alarm
    `card_diverged` and stop card work. **JUDGEMENT CALL:** the relay must never show a card R6 did not sign, and the
    ledger keeps us from signing two.
  - `stale_card` on `card-keys`: reread the card, then rebuild the wraps for the relay's current `card_seq`, but only
    if it is ours.
  - `wrap_conflict`: other bytes for a wrap that is already stored. Keep the stored ones (the wraps are sealed, so the
    old wrap still opens `CK`).
- **Gap (relay silent):** between step 2 and step 3, devices that fetch the new card cannot open its sealed part
  (404 `card-key`). The device keeps showing the previous card's names until its wrap arrives (an orch-f1 rule,
  11A.15.13).

#### 11A.15.6 Pairing, host side, step by step

**The offer record's state machine.** Each transition is one atomic write of `pairing/<offer>.json`, made **before**
the answer or network write it enables:

`open → pending → confirmed → cert_pending → allocated → published → approved`, plus the terminal states `rejected`,
`expired`, `cancelled` and `failed`. A presented certificate (7a) skips `cert_pending`.

**At most one live offer per workspace (JUDGEMENT CALL, Q6):** "live" means any non-terminal state, **or** an
outstanding relay cert request in `pairing/outstanding.json`. The relay allows one open or challenged cert request per
workspace (`request_open`, `cert_queue.py`), and one pairing at a time keeps the human's comparison unambiguous.

**The phone's deadlines (orch-f1 B2).** The phone gives up, and **erases its fresh keys**, at:
- path 7b: the challenge's `expires_ms + 60 s` (the challenge carries the request's `expires_ms`);
- path 7a and the SAS phase: 10 minutes after its first send.

R6 records `phone_deadline_ms` in the offer record: at consumption, the meta's signed time + 10 min; at the cert
request, `min(that, request.expires_ms + 60 s)`. **R6 refuses to enter step 9 when `now > phone_deadline_ms − 120 s`**
(**JUDGEMENT CALL:** 2 minutes covers a human's `device.added` signature and the publication). The offer then ends
`expired` and the phone is told `expired`. The append job re-checks the deadline inside the critical section. This is
also the deadline Opus M6 asked for: no offer can sit in `cert_pending` with a certificate forever.

**Answers per state (orch-f1 B1, Opus M6).** Every answer is a pure function of the record and the persisted challenge,
so a resend or re-poll is consistent:

| Offer state | `op:"pair"` resend (same keys and `attempt`) | `pair_status` (path 7b) | `pair_status` (path 7a) |
|---|---|---|---|
| `open` | (consumes, step 5) | silence (no consumed request) | silence |
| `pending`, `confirmed` | `pending` | `pending` | `pending` |
| `cert_pending` | `pending` | `cert_pending` with `challenge: null` until the challenge is stored; from then on **the same stored challenge, byte-identical**, never `null` again | — |
| `allocated`, `published` | `pending` | `cert_pending` with the same stored challenge | `pending` |
| `approved` | `pending` | `approved` (step 12) | `approved` |
| `rejected` | the stored rejection (short, or long with the primary's rejection) | same | same |
| `expired`, `failed` | short `expired` / short `rejected` | same | same |
| `cancelled` | from `open`: `expired` (orch-f1 S6); later: short `rejected` | same | same |

The phone keeps sending `op:"pair"` until it sees `pending`, and alarms `regression` on a `cert_pending` before
`pending` or a `null` challenge after a shown one; the table excludes both. A pair request with **other** keys or
another `attempt` on a consumed offer gets `pairing_closed`.

**Refusal codes (orch-f1 S5).** R6 sends only the answer states and refusal codes of proto §8.3 and the relay corpus
(`pair_answers`, `rejected_forms`): anything else is dropped by the phone and shown as "link used by someone else".
Every internal condition maps to one of them (most to short `rejected`). A vector checks every R6 answer against the
set.

| # | Step | Persisted first | Failure, retry, crash |
|---|---|---|---|
| 1 | **Offer creation** (`orch host relay pair`, control op `relay.pair_offer`, human_only, refused under `ORCH_GRANT`). The preconditions are checked in one worker job **under the store lock**: `link.json` is `linked`; the worker is running and holds a gen; a valid `membership/primary.json` (11A.15.1); no live offer and no outstanding cert request; the early reader check (`refuse_reader_op(service, "relay.pair", history=True)` before H12d-2, then `check_readers` against the offer's `required_reader`); no `membership_quota` alarm; the device count is below the cap. R6 then draws `offer_id` (16 bytes), `S` (32), `sas_nonce` (32) and the scopes (default `["look","decide"]`, Q4; a subset of `look,decide`: `operate` and `type` are refused for a phone offer in v1.7, N1), and sets `expires_ms = now + 10 min` | `pairing/<offer>.json` state `open` (this is the pairing reservation of 11A.6, written before the link exists) | any refusal writes nothing. A crash after the write leaves an `open` offer that expires on its own. The CLI shows the link **only after** the write returns |
| 2 | **The link and QR** (11A.15.9): the native `orch://pair?relay=<pct origin>#v2.<ws>.<offer>.<b64u S>.<b64u wsk_pin>.<b64u pk_pin>` (in the QR, Q5) and the HTTPS fallback `https://<relay>/pair#v2.<same five fields>`, both from H12c `pair_link`. `pk_pin` is the pinned owner `PK` from the genesis delegation; `wsk_pin` is the own WSK. The `relay` origin is **byte-equal** to the card's `relay_url` (orch-f1 S7: else the phone refuses `card_origin`) | nothing new | `S` is shown on the person's terminal only. It is never logged, never sent through the control socket's logs and never in `host.status` |
| 3 | **A pair request** (a poll item, `route:"pair"`). The pair thread runs §8.6 steps 1 to 3 (silence for an unknown offer or one past its tail, or a bad tag under `K_pair`), then §8.3 step 3 in order: meta shape and exact field set (meta v2: the six fields, or the six plus the optional seventh `reader_decl`, protocol §8.3; H1), from the **raw plaintext bytes** with a byte cap of 2 KiB plus margin **before** parsing (H12d-2 note I2); the offer `open` and unused (else the answer of the table above); `device_id(dk_sig_pub)` equal to the header's, and the signature, verified over the **original** meta bytes, the label as the phone sent it (no cleaning or normalisation before verification; D1); ±300 s; a presented `cert` (owner's person, valid, not revoked, these two keys) | none for a refusal. Each refusal is charged to the offer's own budget of 5 per minute (in memory; past it, silence), carries `wsk_pub`, and is sealed and signed (`RelaySigner.pair_answer`) | the answer bytes are appended per `rid` to the `.answers` file **before** the POST. An identical resend of that `rid` replays them byte-exact (the relay refuses other bytes with `chunk_conflict`, `bridge.py`). Pair answers are never re-stored after their 60 s copy expires, so the device re-polls with a new `rid` |
| 4 | **The reader gate (H12d, early).** `verify_decl` with `via:"pair"` only, on both paths (**JUDGEMENT CALL**, orch-f1 S4 and orch-52: §0.8.6 item 9.4 no longer allows a `via:"declare"` declaration beside a presented cert: conflict C7 resolved by narrowing 9.4). `who` is the link's `pk_pin`, or the presented cert's person; `bind` this offer and this `attempt`; `dk_hash` of the meta's keys. The level is checked against `required_reader`; absent means 0. On resends the **first stored declaration wins** (ECDSA signatures differ per resend) | — | a failure answers the short `rejected` and moves the offer to `rejected` (the host screen gives the reason, §0.8.6 item 9.15 Q4). This happens **before** the SAS is shown and before the primary is asked (§0.8.6 item 9.4) |
| 5 | **Consume** the offer | state `pending` with the keys, `attempt`, the cleaned label (11A.15.13), the cert if presented, the declaration verbatim, and `phone_deadline_ms` | the write **precedes** the `pending` answer (`{state, wsk_pub, sas_nonce, dk_sig_pub, dk_kx_pub}`, exact). A second device with a valid tag (it holds `S` too) then finds the offer used: `pairing_closed`, and the CLI alarms "someone else has this link: reject" (R6 holds `S`, so it knows a second tag-valid key used the offer; Opus L10) |
| 6 | **The human compares the SAS** on the Mac. The CLI shows the cleaned label ("named by the device") and the requested scopes, and asks the human to **type the six characters the phone shows** (Opus L10: entry, not a y/N prompt). Approval only when the typed code equals `sas(ws, offer, dk_sig_pub, dk_kx_pub, wsk_pub, sas_nonce)`. **Up to 3 typed attempts per offer** (DECIDED 15 Oct 2026, orch-52 re-check of beafc6c8, N3c: a human typo cannot be ground by an attacker, so this is safe and kinder): a wrong code re-prompts, and the third wrong code is Reject; an empty line or an explicit reject is Reject at once. The attempt count is in the offer record | `confirmed`, or `rejected` (the attempt count with each wrong code) | a reject answers the short `rejected`. An unanswered prompt expires with the phone's deadline: state `expired`. A crash keeps `pending`, and the CLI re-attaches with `orch host relay pair --resume` |
| 7a | **A presented certificate** (the phone is already the owner's device): skip to step 8 with that cert. Its `scopes_max` caps the list scopes. This path needs no `PK` (device ids are global, §4), so every later workspace pairing of the same phone needs no recovery code | — | — |
| 7b | **The cert request** (§8.4 step 1): H12c `cert_request_o` with `request_id` (16 bytes), `primary_device_id` and `primary_kx_pub` from the verified `primary.json` (11A.15.1), the clear binding `offer_id`, `attempt`, `device_id`, `dk_hash`, `expires_ms = created + 10 min`, and `inner` sealed to the primary's `dk_kx` with the **cleaned** label (the one the human saw at the SAS) and `scopes_max` per "Certificate policy" below. Signed by `RelaySigner.cert_request`, then `POST /v2/persons/{owner}/cert-requests` (W(g)) | `cert_pending` with the **exact request bytes**, and `outstanding.json`, before the POST | transient: resend the same bytes (the relay answers the same request 200). `request_open`: R6 never creates it (one outstanding request), so it is a foreign or stale request: alarm `pairing_request_open`, answer `cert_pending` with `challenge: null`, and **retry the POST** until the old request's known `expires_ms` (if R6 knows it) or the phone's deadline; then `expired`. `replayed`: alarm and fail. `expired`: state `expired` |
| 7c | **The primary's challenge.** On a `notices.cert_requests` wake (fallback: `GET /v2/cert-requests/{id}` at most every 10 s, 5 s deadline, never blocking the pair thread) R6 reads the request. It verifies the PK-signed challenge with H12c `check_cert_challenge(signed, pk_pub, request_o)` (under the pinned `PK` and against the stored request's binding; the phone verifies again, §8.4 step 5) and relays it **unchanged** in `cert_pending`. Unverifiable relay data is dropped and alarmed `relay_misbehaving` | the challenge bytes, in the record, **before** the first answer that carries them; never replaced | a relay that withholds the challenge only delays: the phone's deadline gives `expired` |
| 7d | **The primary signs or rejects** (owner client, 11A.15.9): it uploads the certificate (`PUT /v2/devices/{device}`) and the relay marks the request `signed`; or it posts a `cert_rejection`. The inline CLI also hands the certificate it signed (or the rejection) straight to R6 through the control op `relay.pair_cert` (Opus L4): the relay keeps a request only until `expires_ms + 60 s`, and a workspace session cannot `GET devices/{id}` before the device is in `ever_devices` (`directory.py`), so a host down at that moment would otherwise lose a signed cert. R6 still verifies it itself (step 8) | — | `rejected` with a rejection verified by `check_cert_rejection(signed, pk_pub, request_o)`: R6 relays the long form `{state, wsk_pub, pk_pub, rejection}` unchanged (§8.3 G3b). `expired`: `expired` |
| 8 | **Certificate in hand** (from 7a, from `relay.pair_cert`, or from `GET cert-requests/{id}` `state:"signed"`). R6 verifies it itself, never trusting the relay's state: `check_cert` under the pinned owner `PK`; the keys equal the pending keys; `person_id` is the owner's; `verify_cert` at now; no revocation known (relay feed and `HostPins`); for 7b `scopes_max` equals what was requested, for 7a it covers the offer's scopes. The offer must still be `cert_pending`/`confirmed` (a cancelled offer's cert is refused here) | the cert, in the record; `outstanding.json` cleared | any failure ends the offer `failed` (short `rejected`, alarm `pairing_cert_invalid`) |
| 9 | **The R6 allocation (the authoritative gate).** Refused when the phone's deadline is near (above). The CLI drives the person path: `human.prepare` of the material kind `relay.pair_device` (H12c item 12) renders `device.added {device, cert}` with the device id, the label (the cleaned label as `clean_label` returned it, shown for review only; there is no F1 copy of the label and no NFC step, 11A.15.13), scopes, `cert_code` (7b) or SAS (7a), and the cert digest. The Mac's workspace device signs it with the **terminal hand-off (passphrase) only** (N4, 15 Oct 2026: never the click signer, never `auth:"convenience"`, below. With the click-to-sign flag on, the hand-off is refused, `sign.click_only`, so no branch works and pairing needs the flag off; `pair.preview` refuses that case up front, 11A.15.9A), then `human.submit`. The submit's append job, **under the store lock**, in one critical section: (i) the record is `confirmed`/`cert_pending` with this cert and within its deadline; (ii) **the allocation intent** `{offer_id, attempt, event digest (the reviewed prepared material), decl_digest, device_id}` is written into the offer record (Codex 2); (iii) the authoritative reader check (`refuse_reader_op`, later `check_readers` over the event, §0.8.6 item 9.4); (iv) `append_signed(...)` of `device.added`; (v) the reader record (§9.10, `source:"pair"`); (vi) `membership/devices/<id>.json`; (vii) the offer state `allocated` with the event id | the intent before the append; the F1 log line is the commit; then the reader record, devices and offer, under the same lock before it is released | **crash recovery** (at open, before either R6 thread starts, before anything is served): an offer whose intent's event digest matches a `device.added` in the log is rolled forward by recovering **all** derived records: the reader record, `devices/<id>` and `allocated`. Matching is by the intent (offer, attempt, reviewed event, declaration), never by "same cert, same device". A `device.added` without an intent is a plain F1 device (not relay-reachable). The gate refusal ends the offer `failed`: nothing appended, short `rejected`. **Already in F1** (orch-f1 S3): if F1 already holds this exact device with the same keys and certificate and it is valid, the allocation is idempotent (no second append; the intent names the existing event), and if the device is in `excluded` the same critical section clears it (re-admission, N2, 11A.15.4); if the id was removed or revoked, refuse `revoked` |
| 10 | **Publication.** `PUT /v2/devices/{device} {cert}` (idempotent: an identical `cj(o)` is a free 200; `cert_conflict` is alarmed `pairing_cert_conflict` and fails the offer), then the pair thread asks the membership thread for the body that **adds** the device (11A.15.4; a rotation if the gate is up). **No WK is sealed before steps 8 and 9** (§8.3 step 5) | H1/H2 `head.json` write-ahead | as 11A.15.4. A content refusal of the add ends the offer `failed`, puts the device in `excluded` and forces the rotation (holder invariant). The device stays an F1 device: R6 raises the `needs` item "pairing of <label> failed after it was added: remove the device" and never appends `device.removed` itself (Q13) |
| 11 | **The baseline.** After the add committed: R1's verified `GET checkpoint` gives the held `{n, hash}`. `POST /v2/workspaces/{ws}/baselines {hash, offer_id}` (W(g), 7 days). **JUDGEMENT CALL:** a linked workspace always has a held checkpoint, so a missing one while `published.jsonl` has an accepted entry is `relay_rolled_back` (11A.5): R6 does not approve with `baseline: null` and retries until the phone's deadline | `published` with the acknowledged publication `{epoch, list_seq, list_hash}`, the current accepted `card_seq`, and the baseline | `404 not_found` or `410 checkpoint_gone`: reread H and pin the new one. `quota_exceeded` (16 pins): alarm, retry every 30 s until the deadline. A baseline is never downgraded |
| 12 | **Approved.** Every later `pair_status` gets `{state:"approved", scopes, cert, pk_pub, epoch, list_seq, card_seq, baseline, wsk_pub}` from the record. `scopes` are **byte-equal to the accepted list entry's** (orch-f1 S7: else the phone refuses `scopes_mismatch`); `epoch` and `list_seq` name the exact locally persisted publication the relay acknowledged | `approved` | the pin is released (`DELETE baselines/{hash}?offer_id`) on the device's first verified column-R request, but only if no other non-tombstoned offer names the same hash (Opus L5: the relay keys pins by `(ws, hash)`); else left to expire. The record becomes a tombstone after 7 days |

**Ending a stuck offer (Opus M6, orch-f1 B2).**
- An offer past its phone deadline in `allocated` or `published` (the relay answers 5xx forever, the clamp freezes
  membership, the baseline quota is full) ends `expired`. In the same write the device enters `excluded` (if a body
  granted it a key, the holder invariant then rotates it out), and R6 raises the `needs` item "pairing of <label> did
  not finish: remove the device".
- `orch host relay pair abandon --offer O` does the same on demand for `allocated`/`published` (state `failed`).
- An offer that reached `approved` but whose device never makes a verified column-R request within 24 h raises the
  `needs` item "<label> was paired but never connected". Nothing else changes.

**Cancellation.** `orch host relay pair cancel --offer O` writes `cancelled` (any state before `allocated`):
- an `open` offer then answers every tag-valid request `expired` (orch-f1 S6);
- a consumed offer answers the short `rejected` (the phone shows "the pairing was refused" and erases its keys);
- **an outstanding cert request** stays tracked in `outstanding.json`. The relay lets the addressee withdraw it: a
  primary's `cert_rejection` moves it from open or challenged to `rejected` (`cert_queue.py`; Opus L2, Codex 2). The
  attached CLI posts the PK-signed rejection when it can (it holds `PK` for the pairing under Q1 option B, or the
  separate primary does it). Otherwise the next offer **waits** until the old request's `expires_ms` instead of failing
  with `request_open`. R6 also refuses a cert for a cancelled offer at step 8;
- from `allocated` on, cancellation is refused (`relay.pair_closed`): use `abandon`, or remove the device through
  `device.removed`, which is a list change and a rotation.

`S` is kept until the terminal state plus 10 minutes, so late requests get an answer, not silence. Silence would make
the phone show "link used by someone else" after 60 s, the wrong message for an expired link.

**Certificate policy (Opus L11, orch-f1 S2; JUDGEMENT CALL).**
- **The phone has one identity across workspaces**, and the relay stores a device's certificate **once**:
  `directory.py` `store_device` inserts, and a different certificate for the same id is `cert_conflict`. So a phone
  certificate can be neither widened nor renewed for the same keys, and the phone cannot draw fresh keys while it
  holds a workspace.
- Therefore the phone certificate's `scopes_max` is the **ceiling over the person's workspaces**. **DECIDED 15 Oct
  2026 (orch-52 re-check of beafc6c8, N1; supersedes the ceiling `["look","decide","operate"]` of beafc6c8):** the
  ceiling is **the first pairing offer's scopes** (default `["look","decide"]`). Reason: F1 §5.3 gates admin events
  (member management, policy, settings, grant, addon, `restore`, `invalid.acknowledged`) on `operate` in the
  **certificate's** `scopes_max`, and `device.added {device, cert}` embeds that cert, so an `operate` ceiling would make
  every phone an F1 device able to sign admin events in each workspace it joins, whatever its list scopes. Least
  privilege until the renewal route exists (Q15, 11A.15.14 step 11).
  - **No `operate` for phones in v1.7** until that route exists: an offer with `operate` is refused (step 1); `type` is
    never put in a phone cert.
  - Widening a phone beyond its first offer's scopes needs new keys (a new device id); a later workspace pairing on
    path 7a gets at most the cert's ceiling (step 8 refuses an offer above it).
  - The `cert_code` screen names the ceiling ("this certificate allows look, decide in every workspace"). The **list
    entry** carries the offer's scopes, and R2 checks both (11A.4).
  - Tests: `phone_cert_ceiling_equals_first_offer_scopes`, `pair_offer_operate_refused_v17`,
    `cert_code_screen_names_ceiling`.
- `expires_ms: null` for phone and primary certificates in v1.7: an expiring certificate with no renewal path is a dead
  end for every workspace the phone holds (`fresh_keys_unavailable`). The end of a device's life is revocation.
- **Dissent (Opus L11):** give both an expiry and a renewal path. Recorded; it needs a relay rule for certificate
  renewal of an existing device id (Q15: decided 15 Oct 2026, no relay change now; a pre-public relay item).

**Convenience custody is refused for pairing (DECIDED 15 Oct 2026, orch-52; owner rule 11A.11 Q4, §11A.6 "no owner
exception", 11A.14.3).** Adding a reader is the authority-expanding act, and the human is at the TTY anyway:
- **`relay.pair_device` refuses `auth:"convenience"`** with `sign.convenience_unavailable`, new reason `pair_device`,
  whatever H12d relaxes and under any dev opt-in. A click-to-sign workspace whose passphrase key was converted away
  must run `orch keys custody passphrase --in-place` first; the refusal's fix text names it.
- **The primary's keys (`dk_sig`, `kx`, and any stored `PK`) can never enter convenience custody:**
  `orch keys custody convenience` refuses the primary role. The convenience backend already refuses every
  `PERSON_KEY_LABELS` payload structurally (`convenience.py` `sign`: `DEVICE_KEY_LABELS` only).
- Tests: `pair_device_refuses_convenience_auth`, `pair_device_refuses_convenience_under_dev_opt_in`,
  `custody_convenience_refuses_primary_role`, `convenience_sign_refuses_person_key_labels` (exists; cited),
  `pair_device_refusal_fix_text_names_custody_passphrase`.

**What R6 never does in pairing:**
- it signs nothing with `PK` or a device key (only WSK objects through `RelaySigner`);
- it seals no `WK` before a verified certificate and the F1 allocation;
- it uses no number in an `approved` answer other than those of the locally persisted publication the relay
  acknowledged;
- it never re-signs a cert request, a list, a grant, a card or a pair answer: it resends stored bytes.

#### 11A.15.7 `RelaySigner` methods R6 adds (11A.2 table, "R6 labels")

Each method validates its context, builds the label and bytes itself, and records a ledger entry **before** returning
a signature (the `sign-ledger.json` pattern of `card`; ECDSA is randomised, so a ledger hit returns the recorded
signature).

| Method | Checks before signing | Ledger (refuses a second, different object) | Label |
|---|---|---|---|
| `member_list(o)` | exact field set; the link's workspace; `1 ≤ epoch < 2^32`, `1 ≤ list_seq < 2^53`; sorted unique ids; every `persons` entry has a member; `wk_commit` well formed. **The H4 epoch floor (Opus L8):** refuses an epoch that is burned, or below the highest epoch ever signed; equal is allowed only with the recorded `wk_commit` | `("member_list", list_seq)` (H6), and `("wk_commit", epoch)`: two different `wk_commit` at one epoch are refused, the structural guard of "one WK per epoch" | `sig_member_list` |
| `wk_grant(o, *, list_seq)` | exact fields; workspace; `epoch` has a `wk_commit` ledger entry; `sealed` length; the device is in the list signed at that `list_seq` (the context parameter Opus L8 asked for) | `("wk_grant", device, epoch)`: other bytes are refused (`grant_conflict` can never come from us) | `sig_wk_grant` |
| `cert_request(o)` | exact fields with the clear binding; workspace; `person_id` = the pinned owner; `primary_device_id` = the verified `primary.json`'s; `expires_ms − created_ms ≤ 600 000` | `("cert_request", request_id)` | `sig_cert_request` |
| `pair_answer(header, body)` | ORB2 direction 2, **epoch 0**, `stream` = an offer R6 holds in a non-terminal state or within its 10-minute tail (through a narrow liveness callback from R6, not an import of R6 state; orch-52 G3); the whole envelope ≤ 64 KiB | none (answers are stored per `rid` instead) | `sig_bridge` |

`response_chunk` keeps refusing epoch 0 (11A.2), so a pair answer can never pass as a bridge chunk, nor the reverse.

#### 11A.15.8 Threat model deltas (extends 11A.9 and P3 design §8)

| Actor or event | Can | Cannot (what stops it) |
|---|---|---|
| **Hostile relay** | see offer ids, rids, timing and sizes; drop, delay or replay pair requests and answers; withhold the challenge, the cert or the accepted list; serve an old list or card; claim a request `signed` or `rejected`; answer false membership refusals (they burn numbers; H8 alarms, the clamp bounds it); keep and distribute a body it then reports refused; acknowledge a body with 2xx and keep nothing | read pair traffic (`K_pair` needs `S`, which is only in the link fragment); learn `WK` (sealed to `dk_kx`); forge the challenge, cert or rejection (PK-signed, verified by R6 and the phone); make R6 adopt its list (`R6View` is the host's own committed body); make R6 seal `WK` to a key the human did not compare (SAS plus `cert_code`); get a refused device re-added (`excluded`; only a new offer the human approves re-admits it, N2); keep a device that was ever granted the current key after it leaves `desired` (the holder invariant rotates); make R6 seal the cert-request inner to its own key (`primary.json` is verified under the genesis `PK`) |
| **Hostile relay, through the HTTPS link** | if a person opens `https://<relay>/pair#…` in a browser, the relay's page script reads the fragment and learns `S` (the fragment is not sent in HTTP, but the page sees it). The page can also show fabricated comparison instructions | with `S` alone it can race a pair request with its own keys; the SAS differs, so a human who compares in the installed app rejects. **The hostile-relay guarantee holds only for the native paths** (Codex 2): the in-app scanner and `orch://`. The HTTPS form is printed labelled "fallback: your relay operator's page can read it"; the comparison is restricted to the installed app and the host CLI |
| **Another iOS app claims `orch://`** (orch-f1 S1, orch-52 G2, Opus L13) | an app that registered the same custom scheme receives the link, and with it `S` | the same outcome as a leaked link (below): one try, the SAS and `cert_code` comparisons, `pairing_closed` on the honest phone. Mitigations (owner decision Q5): the in-app scanner is the primary path (no OS dispatch); every `orch://` open lands on a review screen with no auto-connect; the honest phone never offers "open in browser" for a link it cannot handle |
| **Hostile invitee** (holds the link legitimately) | send malformed meta, oversized `reader_decl`, a foreign cert, another key's cert, control characters in the label, floods | pass the exact-field and byte-cap checks before parsing; get scopes above the offer's (the list scopes come from the offer); get a cert for keys other than those it proved; reuse a revoked device id (`revoked`); exceed 5 refusals per minute per offer, the 64-item pair queue or the relay's pair budgets; stall pairing for other offers (one live offer; the pair thread's deadlines) |
| **Link leaked, QR shoulder-surfed, or scheme hijacked** | consume the offer first (one try) | succeed without the human typing a SAS that matches the attacker's own screen. The human sees the attacker's label and SAS on the Mac, the CLI warns when a second tag-valid key used the offer, and the honest phone shows "link used by someone else". Default Reject; the offer lives 10 min and is single-use; `pair cancel`. **Residual:** a human who copies the code from the attacker's screen admits the attacker with the offer's scopes (typed entry, not y/N, makes it harder; phone offers and certificates exclude `operate` in v1.7, N1) |
| **Replay of an old offer or request** | resend captured envelopes | open a new pairing: the offer is single-use and expired offers are silent after their tail; a replayed `rid` gets the stored bytes and no new state; the ±300 s window; `attempt` and `dk_hash` bind the declaration, the challenge and the rejection to one attempt (G3b) |
| **Two devices racing one offer** | both hold `S` (a leak) | both win: consumption is one durable write on the single pair thread before any `pending` answer, and the second gets `pairing_closed`. The relay's one-open-request-per-workspace rule backs this at the cert queue |
| **A compromised host** (tier A, same UID) | everything the person's UID can do: read and copy every file under the UID, alter executables and the terminal environment, capture what the person types | the cert challenge still binds keys chosen by the phone. In tier B (phase 3) core never takes protocol §8.3 step 5's local-sign shortcut, so a host cannot grind a 30-bit SAS against substituted keys (§8.4 "Why this closes R1") |
| **Person key custody (Q1)** | **same-UID code defeats every same-Mac custody of `PK`** (Codex 1). Under option B it captures the recovery code when it is typed and derives `PK`; under A or A' it copies the encrypted file for offline guessing and captures the passphrase. `PK` **does** enter a same-UID process: the owner client (the attached CLI or `orch keys primary`), for one command | `PK` never enters the host **daemon** or the convenience backend; no host op asks for a `PK` signature; the owner client signs only for the request R6 created for its own offer, after the human confirms `cert_code`. A stolen `PK` is **permanent**: `person_id = id(PK)`, there is no rotation, and recovery restores a lost `PK` but does not invalidate a stolen copy. Only the separate-primary option (a different trusted device with its own confirmation display) protects `PK` against a compromised Mac (11A.15.16) |

#### 11A.15.9 The host CLI surface (the dashboard follows the same link rules, 11A.15.9A)

All the commands below are human-only (refused under `ORCH_GRANT`) and go through the control socket. The link is
printed on the person's terminal only.

| Command | Does | Output |
|---|---|---|
| `orch host relay pair --workspace W [--scopes look,decide] [--ttl 10m] [--json] [--no-qr] [--resume [--offer O]] [--primary inline\|external]` | creates the offer (11A.15.6 step 1), prints the link and QR, then **stays attached** (`--resume --offer O` attaches to an existing offer instead, for example one created from the dashboard, 11A.15.9A, and runs the remaining steps from its state): it shows the incoming request (label, scopes) and asks the human to type the SAS; with `--primary inline` (default) it runs the primary steps itself (below); with `--primary external` it waits while a separate primary (another device, or the dev path's test owner client) does them; it drives the `relay.pair_device` signature; it shows progress to `approved` | the native link (in the QR), the HTTPS link labelled "fallback: your relay operator's page can read it", `offer_id`, `expires_at`, then one line per state. `--json`: one JSON object per state change (`{"offer_id","state","link","native_link","expires_ms"}`), written to stdout only on a TTY, otherwise to the file descriptor given by `--json-fd` (the dev path passes a private pipe; Opus L12b). `--ttl` is at most 10 min |
| `orch host relay pair list --workspace W [--json]` | offers, their states and the outstanding cert request, without secrets | table or JSON |
| `orch host relay pair cancel --workspace W --offer O` | 11A.15.6 "Cancellation" | the new state |
| `orch host relay pair abandon --workspace W --offer O` | 11A.15.6 "Ending a stuck offer" | the new state and the `needs` item |
| `orch host relay primary set --workspace W --cert FILE --label TEXT` | control op `relay.primary_set` (11A.15.1) | the primary's device id |
| `orch keys primary init --relay ORIGIN --invite CODE` (H12c, after Q1) | creates the primary key pair (`dk_sig`, `kx`), obtains `PK` from the Q1 source, signs the primary's cert, creates the relay account (`POST /v2/accounts`) or registers the device (`PUT /v2/devices`), and calls `relay.primary_set` for each registered workspace | the primary's device id and label |
| `orch keys primary status` | the primary's device id, certificate and relay registration | text or JSON |

**Inline primary steps (owner client, never in the host daemon).** After step 7b, the CLI:
1. logs in to the relay as the primary (`kind:"device"`, `dk_sig` through the person path; one unlock for the whole
   command, H12c item 9);
2. reads `GET /v2/persons/{p}/cert-requests` (the relay returns **open** requests only; there is no long-poll
   parameter, so it polls at most every 2 s with a total deadline), picks the request whose `request_id` R6 reported
   for this offer, and runs §8.4 step 2 in order (H12c `check_cert_request`): the card (R1's card checks), the WSK
   signature, the addressee, the time window, `request_id` not already terminal in the primary journal (below), at
   most one outstanding request, the exact fields and binding, not a `drop:` scope;
3. opens the inner with the primary's `kx` key (purpose `cert-request` only), draws `primary_nonce`, obtains `PK` from
   the person-key source (Q1) and signs the challenge;
4. posts the challenge, shows `cert_code` with the phone's label and the requested scopes, and asks the human to type
   `cert_code` as the phone shows it (default Reject);
5. signs the certificate (`make_device_cert`, `label_sealed` under the label key derived from `PK`) or a rejection,
   uploads it, and hands it to R6 (`relay.pair_cert`).

**The primary's durable per-request journal (Codex 1).** One state machine per request, each transition written before
its effect. Its storage (DECIDED 15 Oct 2026, orch-52 re-check of beafc6c8, N7):
- **Location:** the primary is person-level, while core's person directory is per workspace, so the journal has its own
  path: `<state>/person/primary/requests/<request_id>.json` (directories 0700, files 0600, host UID), in the D-P2-17
  custody list.
- **Concurrency:** every transition holds an exclusive `flock` on `<state>/person/primary/requests/.lock` from the read
  of the current state to the durable write, so two owner clients cannot both act on one request (the test
  `primary_concurrent_owner_clients` uses it).
- **Same `request_id` with other bytes:** refused against the stored `verified` bytes, with the alarm
  `primary_request_conflict`.
- **Retention:** a record is pruned after its request's `expires_ms` plus 7 days, keeping a tombstone
  (`{request_id, final state, expires_ms}`) so the id stays terminal.

The states:
- `verified`: the immutable verified request bytes and the card used;
- `challenged`: the immutable challenge bytes (with `primary_nonce`), written **before** the challenge POST;
- `decided`: terminal, either `sign` with the exact certificate bytes or `reject` with the exact rejection bytes,
  written **before** the upload;
- `uploaded`: the relay's 2xx was seen; also terminal: `expired`, `cancelled` (the offer was cancelled), `superseded`.

On start, the owner client resumes from **its own journal first**, not from the relay's queue: the relay's primary
queue returns open requests only (`cert_queue.py`), so a challenged request is recoverable only locally. A resume
resends the exact stored bytes; it never re-signs a challenge for a request already `challenged`, and never signs after
`expired`, `cancelled` or `rejected`. Expiry, cancellation and one outstanding request are enforced locally,
independently of what the relay asserts. Under Q1 option B a resume that still needs a signature asks for the recovery
code again.

The challenge, cert and rejection are made only for the request R6 created for **this** CLI's offer. Any other open
request is left alone and shown as a warning: "a request this command did not create is open".

**Exposure rules for the link:**
- it is never written to a file by the CLI (the dev script keeps its own sandbox copy, 11A.15.11);
- it is never passed as an argument to another process (a QR library gets it in memory); the dev path's `simctl
  openurl` is a declared sandbox-only exception (11A.15.11);
- it is never shown in `host.status`, any `sub` item or any log; the dashboard may show it only as 11A.15.9A allows
  (once, in the direct answer of `pair.offer`);
- `--json` writes it only to a TTY or to the caller's private descriptor.

**QR (Q11).** A terminal QR (Unicode half blocks) of the native link, from an optional pure-Python dependency (`orch[qr]`,
`segno`, BSD-3, compatible with PolyForm Noncommercial distribution). Without it, the CLI prints the links and "install
orch[qr] for a QR".

**New host codes** (added to the §0.6 table with R6's implementation): `relay.pair_open` (a live offer or an outstanding
cert request exists), `relay.not_linked`, `relay.no_primary`, `relay.quota` and `relay.pair_closed` (cancel of an
allocated offer).

#### 11A.15.9A The worker's internal pairing API and dashboard pairing (DECIDED 15 Oct 2026)

**DECIDED 15 Oct 2026 (orch-52 with orch-fa; dashboard contract v1.9).** The dashboard MAY show the pairing link once,
under the same rules as the CLI's stdout (11A.15.9). The host wrappers (the control ops) are orch-52's. They call R6's
offer and answer internals, so R6 exposes those as one **INTERNAL API** (Python, in `orch/relay/r6/pair.py`, called
from the host process only; not a control op, not on the socket). The CLI ops of 11A.15.9 and the dashboard ops below
are both thin wrappers over it, so the link rules live in one place. It is implemented with R6c (11A.15.14 step 7).

**The internal API.** Each call runs as a job on the pair thread (11A.15.14 "Threads"); the preconditions and
refusals are those of 11A.15.6, with the host codes of 11A.15.9.

| Function | Contract |
|---|---|
| `offer(scopes, ttl) -> OfferHandle` | step 1 and 2 of 11A.15.6: all preconditions under the store lock; `scopes` a subset of `look,decide` (`operate` and `type` refused for phones in v1.7, N1), `ttl` at most 10 min. `OfferHandle` = `{offer_id, state, scopes, expires_ms, link: PairLinks(native, https)}`. **The link exists only in this return value:** the handle's `repr`, `str`, pickling and JSON forms leave it out, it is derived from `S` on demand and not stored, and a second `offer` refuses `relay.pair_open` (a lost link means `cancel` and a new offer). The caller (wrapper) must not persist, log, cache or key it |
| `answer(offer_id, typed_code, approve) -> state` | step 6: `approve` counts only when `typed_code` (the phone's six SAS characters, case-folded) equals `sas(...)`. A wrong code counts one of the offer's **3 attempts** (N3c; the offer stays `pending` until the third, then Reject); an empty code or `approve=False` is Reject at once (default). Returns the new offer state and the attempts left. `typed_code` is never stored or logged |
| `cancel(offer_id) -> state` | 11A.15.6 "Cancellation", with its `relay.pair_closed` refusal for an allocated offer |
| `list_offers() -> list[OfferSummary]` | `{offer_id, state, scopes, expires_ms, cleaned label, outstanding_request}`; **no secrets** (never `S`, the link, the SAS, `sas_nonce` or a challenge) |

**The dashboard ops (orch-52's wrappers; cap `pair/1` on the workspace socket, class P, `human_only`: refused with a
grant).**
- `pair.preview`: the blocked codes and warnings, without creating an offer. **Its convenience basis (DECIDED 15 Oct
  2026, orch-52 re-check of beafc6c8, N3a):**
  - **blocked** when the workspace's click-to-sign flag is on **or** the Mac device's custody selection is
    `convenience` (step 9 is the terminal hand-off only, and `relay.pair_device` refuses `auth:"convenience"`,
    11A.15.6). The fix text names both `orch host convenience off --workspace W` and
    `orch keys custody passphrase --workspace W --in-place`;
  - **not blocked** on convenience **history**: that is H12d's reader gate (§0.8.6 item 9; the owner decision of 13 Oct
    admits a qualifying phone, level-1 readers may pair, on a workspace with convenience history). It is shown as a
    warning naming the requirement (the phone must declare the required reader level);
- `pair.offer`: calls `offer`. **The link is returned ONLY in its direct answer.** It is never persisted, logged or
  cached; the op has no idem key; the link is not in outcome records and not in any `sub` item. The QR is drawn
  client-side, from memory, from the native `orch://` link. The response carries `Cache-Control: no-store`. The page
  drops the link at the first `pending`, on expiry or on cancel;
- `pair.list`: calls `list_offers` (no secrets);
- `pair.answer`: the human types the phone's six SAS characters; calls `answer`; up to 3 attempts per offer, default
  reject;
- `pair.cancel`: calls `cancel`;
- a `pair` sub topic with offer states and no secrets.

The cert and primary step (7b to 7d) stays with the CLI or the owner client; the dashboard does not hold `PK`.
**Entry points for the two acts outside the dashboard (N3b, 15 Oct 2026).** Otherwise a dashboard pairing of a new
phone stalls at `cert_pending` until the phone's deadline:
- **the primary step** (path 7b: the recovery code and `cert_code`): `orch host relay pair --workspace W --resume
  --offer O`, which attaches to the dashboard-created offer and runs the inline primary mode (11A.15.9). The dashboard
  shows this command once the offer is `confirmed` on path 7b;
- **the `relay.pair_device` signature** (step 9, `device.added`): only the terminal hand-off, `orch sign CODE`
  (convenience is refused). The dashboard shows the hand-off code once the request is prepared; the attached CLI of the
  previous bullet signs it inline instead when it is running.
**JUDGEMENT CALL:** the HTTPS fallback link is shown with the same label as in the CLI, and the same rules apply to it.

#### 11A.15.10 H12c builders R6 needs, in dependency order (for orch-52)

Pure functions are in `orch.identity` (no I/O, no keys held). Person-path pieces are in the owner client and custody,
never imported by `orch.relay` (the import ban stays). Every `check_*` returns a typed frozen object or raises one
exception class, never a bool (Opus L9). Each builder comes with its vectors from the relay corpus (in brackets).

**Status (orch-52, 15 Oct 2026): items 1 to 4 are being built now.** Items 5 to 8, the pure half of 10 and the
transport of 11 can start now. Item 9 waits on Q2's final shape (decided: the `kx` type) and the one-command unlock;
the person-key source of item 10 waits on Q1; item 12 waits on H12d-2. **Q1 is decided (B), so items 9 to 11 move into
S1/S2 (N6, 15 Oct 2026; 11A.15.15).**

| # | Builder (signature-level contract) | Needs | Vectors |
|---|---|---|---|
| 1 | `wk_commit(workspace_id: str, epoch: int, wk: bytes) -> str`: b64u of `H(h_wk_commit ‖ ws ‖ u32(epoch) ‖ WK)`, `workspace_id` as 32 hex → 16 bytes in the hash (§7.2); refuses `epoch ∉ [1, 2^32−1]`, `len(wk) != 32` | — | [`wk_commit`] |
| 2 | `sas(ws, offer, dk_sig_pub, dk_kx_pub, wsk_pub, sas_nonce) -> str` (6 base32 chars) and `cert_code(challenge_o) -> str` | — | [`bridge.sas`, `cert_code`] |
| 3 | `pair_link(origin, ws, offer, s, wsk_pub, pk_pub) -> PairLinks(native, https)` and the strict `parse_pair_link(text) -> PairLink` | origin rules of 11A.2 | [`bridge.links`] |
| 4 | `check_pair_meta(raw: bytes) -> PairMeta`: enforces the byte cap **on the raw plaintext before** JSON parsing, then **exactly the six fields, or the six plus the OPTIONAL seventh `reader_decl`** (meta v2, protocol §8.3; H1, DECIDED 15 Oct 2026, orch-52 re-check of beafc6c8); `reader_decl` is returned **raw** (bytes), for step 4's `verify_decl`; `attempt` as 32 lower-case hex, valid keys. The label is returned **as sent** (the original bytes, over which the signature is verified, step 3); and the **one** `clean_label(text) -> str` of protocol §2.5 exactly as #476 implements it (D1, DECIDED 15 Oct 2026): remove Cc (except LF), Cf, Zl, Zp, Co, Cn and Default_Ignorable code points; **no normalisation**; truncate to **80 code points** (protocol §8.3: "`label` is at most 80 code points"). `clean_label` itself does no NFC. **#476 is fixed to the meta v2 field set before it merges** (its `set(meta) != PAIR_META_FIELDS` refuses a seventh key); the `bridge.host_cases` meta v2 cases must pass | — | [`bridge.host_cases` `pair_*`, meta v2] |
| 4b | `k_pair(ws, offer, s) -> bytes` and the epoch-0 ORB2 seal/open of pair requests and answers (Opus L9; G3 covers only the signature) | 2 | [`bridge.host_cases` `pair_*`, `pair_answers`] |
| 5 | pair answers: `answer_pending/cert_pending/approved/rejected_short/rejected_long/expired(...) -> dict`, the exact shapes of §8.3, with `wsk_pub` always present | — | [`pair_answers`, `rejected_forms`, `approved_anchor.answers`] |
| 6 | `member_list_o(ws, epoch, list_seq, issued_ms, wk_commit, members, *, first: bool) -> dict` (sorts, derives `persons`, refuses an empty list only when `first`, refuses a person without a member) and `check_member_list(signed, wsk_pub, ws) -> MemberList` | 1 | [`member_lists`, `member_list_snapshots`] |
| 7 | `seal_wk(dk_kx_pub, device_id, ws, epoch, wk) -> bytes`, `wk_grant_o(ws, device_id, epoch, sealed) -> dict`, `check_wk_grant(signed, wsk_pub, ws) -> Grant`; `card_key_wrap(dk_kx_pub, device_id, ws, card_seq, ck) -> bytes`; the card rotation's `card_o` with `ck_commit` and the sealed part (Opus L9); `dk_hash` if not already exposed. The CSPRNG seam is test-only | `crypto.sealing` | [`wk_grants`, `seal`, `card.cases`] |
| 8 | `cert_request_o(*, request_id, ws, person_id, primary_device_id, primary_kx_pub, offer_id, attempt, dk_sig_pub, dk_kx_pub, label (the cleaned label), scopes_max, created_ms) -> dict` (seals the inner; `expires = created + 600 000`); for the primary `check_cert_request(signed, card, me, journal) -> Request` (§8.4 step 2 order) and `open_cert_request(req, unseal) -> Inner`; **for the host `check_cert_challenge(signed, pk_pub, request_o) -> Challenge` and `check_cert_rejection(signed, pk_pub, request_o) -> Rejection`** (steps 7c and 7d) | 7 | [`cert_request.cases`, `open_cases`, `cert_binding`, `attack_challenge`] |
| 9 | **custody:** the key type `kx` with per-key purposes in the authenticated header (the primary's: `cert-request` only; never `ws-envelope`), `Backend.unseal(name, purpose, object_id, epoch, extra, sealed) -> bytes`; a **one-command unlock** for the primary role (otherwise the inline steps prompt 4 to 5 times; `PassphraseBackend.unlocked` is device-labels-only and test-pinned to `ops/import_run.py`, so the second caller and its label set are named explicitly); `orch keys primary init/status` | 7; Q2 | [`seal_open`] |
| 10 | **split.** Pure, **start now:** `make_cert_challenge(pk_sign, req_o, inner, primary_nonce)`, `make_cert_rejection(pk_sign, req_o)`; reuse `make_device_cert` and `seal_device_label`. **After Q1:** the pluggable `PersonKeySource` (11A.15.16): `sign(label, payload)` for `PERSON_KEY_LABELS` only and a purpose-limited `derive_label_key()` (`HKDF(pk_scalar, "orch/v2/pvk\|p1")`, as `workspace_init._label_key`); implementations per the Q1 outcome | pure half: —; source: 9, **Q1 (owner)** | [`cert_request.challenge`, `signed_cert`, `sign_again`, `attack_challenge`] |
| 11 | the owner relay client: device login (`kind:"device"`), `POST /v2/accounts`, `PUT /v2/devices/{d}`, the cert-queue routes (D(p)). Stdlib transport, as in 11A.7, in the owner client, not in `orch.relay`. **Transport starts now** against fixtures behind signer interfaces; it is independent of Q1 | 9, 10 for the product path | relay HTTP fixtures `accounts`, `devices`, `cert_*` |
| 12 | the person-path material kind `relay.pair_device`: `human.prepare` renders and `host/signer.check` binds the workspace id, the device id, the label (the cleaned label exactly as `clean_label` returned it, shown for review only: `device.added {device, cert}` carries no label, and no NFC is applied), the scopes, the certificate digest and `cert_code` (7b) or the SAS (7a). It produces `device.added {device, cert}` signed by the Mac's F1 device, refuses `auth:"convenience"` (11A.15.6), a cross-kind substitution and any extra field | F1 `device.added`; H12d-2 records | core F1 vectors `ws_event_sig` (`device.added` cases) |
| — | already planned for R1 (11A.2), and a prerequisite: `relay.genesis_pin` and `sig_genesis_pin` in `DEVICE_KEY_LABELS` | — | [`genesis_pin`] |

**Notes from the review of #476 (H12c-a builders 1 to 4; 15 Oct 2026).**
- Pair meta v2 has the six fields, or the six plus the optional seventh `reader_decl` (item 4; corrected 15 Oct 2026,
  H1: the earlier "exactly six fields" shut out `reader_decl`). #476 is fixed before it merges.
- `clean_label` does no normalisation (protocol §2.5, D1); there is no NFC step for the label (11A.15.13: correction of 15 Oct 2026).
- The canonical link path is `/pair`. The phone must align from `/app/pair`: a **phone alignment item** for orch-f1.
- `orch.identity` imports `orch.relay.wire.check_origin` (no objection). This makes `orch.identity` depend on
  `orch.relay.wire`, which must therefore **stay import-light** (no I/O, no threads, no heavy imports at module level);
  a test pins it (`relay_wire_stays_import_light`, N9).
- **R6 TODO:** `tests/relay/test_coverage.py` still lists `wk_commit` as an R6 item; update it when #476 merges.

#### 11A.15.11 Deliverable: the scripted dev path (one command)

`plugins/orch-core/scripts/dev-pair` (Python, stdlib only, run with `uv run`) sets up a **throwaway** host, workspace and
primary, links them to a relay, and emits a pairing link.

**Who uses which relay (orch-f1 B3, corrected).** The first draft claimed a Simulator end-to-end against a local
relay. That fails today: the phone has no live pairing yet (phone PR B), `RealmContext.live` is https-only, loopback
admission exists only in DEBUG/package builds (the OrchKit tests, not the app), no `orch` URL scheme is registered,
and the app writes nothing to a FIFO. So:
- **`--relay local`** is for the Python reference test phone (`tests/relay/phone.py`) in CI and for orch-f1's OrchKit
  tests;
- **the Simulator** uses `https://relay.dev.severin.io`, after phone PR B lands;
- a Debug-only loopback admission in the app: DECIDED NO (Q16, 15 Oct 2026).

```
uv run scripts/dev-pair up   --root DIR [--relay local|https://relay.dev.severin.io] [--invite-file F]
                             [--scopes look,decide] [--open-simulator] [--expect-code-from FIFO] [--json-fd N]
uv run scripts/dev-pair down --root DIR [--keep]
```

**The sandbox identity (no dependency on Q1, Q2 or H12c 9 to 11).** The dev path never uses the production primary.
Its owner is a throwaway person whose recovery code and person key live in the sandbox, under a sandbox passphrase
(`<root>/secrets/`, 0600). The primary role is played by the **test owner client** `tests/relay/owner.py` (built on
the relay's `ref/` client shapes and the H12c pure builders), which the host CLI drives through `--primary external`.
`tests/relay/owner.py` lives in the test tree, is never packaged, and refuses to run unless it finds the sandbox's
manifest (below). This is the same test owner client R5's T2 uses to create the owner account (11A.15.14). **Shared
code (N6, 15 Oct 2026):** `tests/relay/owner.py` and the product inline primary (11A.15.9) use the same
`check_cert_request`, `open_cert_request` and journal logic; only the person-key source and the packaging differ.

**`up`, in order. Each step is checked; any failure tears down what it started (fail-closed cleanup, below):**
1. **Sandbox (Codex 4).**
   - `--root` is **created exclusively** (`mkdir` with mode 0700; an existing path, empty or not, is refused, so a real
     directory can never be adopted as a sandbox). Its parent must be an existing directory owned by the user, not
     group- or world-writable, with no symlinked ancestor (`/tmp` is refused, `/private/tmp` is accepted). The path is
     kept short enough for the 104-byte socket limit.
   - The script records a **manifest** `<root>/.orch-dev-sandbox` (0600): a random id, the root's `st_dev`/`st_ino`,
     and the identities of everything it starts. Every later open under the root goes through a directory file
     descriptor of the pinned root with `O_NOFOLLOW` traversal; a root whose `st_dev`/`st_ino` differs is refused.
   - **The marker is accident prevention, not containment:** same-UID malicious code can forge it. It stops a mistyped
     `--root` from deleting real data; it does not defend against an attacker running as the user.
2. **Environment (Codex 4).** Children get an **allow-list** environment, never the parent's: `PATH` set to pinned
   absolute executables (`uv`, the pinned orch-relay checkout from `tests/relay/vectors/SOURCE`, the pinned orch
   checkout), `HOME=<root>/home`, `ORCH_STATE_DIR=<root>/state`, `ORCH_HOST_RUN_DIR=<root>/run`, the forced
   **passphrase** custody backend (no keychain, no convenience, Opus L12e), `LANG=C.UTF-8`, and sandbox-local working
   directories. No `PYTHONPATH`, no inherited configuration. The script refuses to start when `ORCH_GRANT` is set in
   its own environment.
   - **How the passphrase backend is forced (N8a, 15 Oct 2026):** by the fresh state alone. A new `HOME` and
     `ORCH_STATE_DIR` hold no keychain and no convenience selection, so the default passphrase custody applies; no
     environment variable selects it. **Rule:** any environment variable read by production code may only **narrow**
     (refuse a backend or an op), never select a weaker or a test backend. **JUDGEMENT CALL (ACCEPTED by
     orch-52, 15 Oct 2026, narrowing-only, on these conditions):** to make the sandbox refuse convenience outright, the
     script sets `ORCH_NO_CONVENIENCE=1`. (a) **Any non-empty value refuses**; only an unset variable means no extra
     refusal (`0` and `false` refuse too). (b) The check lives **inside the two core functions**,
     `select.convert_to_convenience` and the flag write (`orch host convenience on`), not only in the CLI, and uses an
     existing code: `sign.convenience_unavailable`, reason `env`. (c) It **never blocks the way back**: `orch host
     convenience off` and `orch keys custody passphrase` stay available. (d) A test shows every other op is unchanged
     with it set. Tests `dev_pair_sandbox_host_refuses_convenience_on`, `no_convenience_env_any_nonempty_value_refuses`,
     `no_convenience_env_checked_in_core_functions`, `no_convenience_env_way_back_stays_available`,
     `no_convenience_env_leaves_every_other_op_unchanged`.
3. **Relay.**
   - `local`: pick a free loopback port, then start the pinned relay (`uv run orch-relay serve --host 127.0.0.1 --port
     P`) with `ORCH_RELAY_ORIGIN=http://127.0.0.1:P`, `ORCH_RELAY_DATA=<root>/relay-data`,
     `ORCH_RELAY_MAX_PUBLICATION_BYTES=4194304` and **`ORCH_RELAY_INSTANCE=<random>`**. The port race (another local
     process binds P first) is closed by identity, not by luck (Opus L12a): the script waits for `GET /healthz` to
     answer `instance` equal to its random value (`app.py`), and checks that its child is alive. Anything else on the
     port is refused; at most 3 retries on another port. It then runs `orch-relay invite create --ttl 1h` against the
     same data directory.
   - `https://relay.dev.severin.io`: an invite is required, read from `--invite-file` (0600; never argv), issued by
     the relay operator; the script never creates invites remotely. The origin must be in the allow-list
     `{http://127.0.0.1:*, http://localhost:*, https://relay.dev.severin.io}`.
   - The origin stored in `link.json`, the one the host signs and the one the relay was started with are the **same
     literal** (`http://127.0.0.1:P`, not `localhost`).
4. **Identity and workspace.** `orch init` runs in `<root>/ws` through a **pseudo-terminal** (stdlib `pty`). The script
   feeds a fresh random passphrase and confirms the generated recovery code, both kept in `<root>/secrets/` (0600). No
   test seam is added to production code: the dev path drives the real TTY prompts. **pty transcripts are captured
   privately** (into memory, never echoed or logged, also on failure; Opus L12c, Codex 4).
5. **Host.** `orch host run` in the foreground under the sandbox environment (no launchd/systemd unit), then `orch host
   register`. Its control socket is under `<root>/run`.
6. **Owner account and primary.** `tests/relay/owner.py` creates the account (`POST /v2/accounts` with the invite, the
   sandbox `PK` and a sandbox primary device), then `orch host relay primary set` with its certificate.
7. **Link.** `orch host relay link --workspace W --relay ORIGIN --max-publication-bytes 4194304` (R5), with the genesis
   pin signed through the pty.
8. **Pair.** `orch host relay pair --workspace W --primary external --json-fd N --scopes …` through the pty, with
   `tests/relay/owner.py` running the primary steps against the sandbox account. The script writes the
   `{link, native_link, offer_id, expires_ms, origin, workspace_id, root}` object to `<root>/pair.json` (0600) and,
   with `--json-fd`, to that descriptor; **never to stdout** (Opus L12b).
   - `--open-simulator` runs `xcrun simctl openurl booted '<native link>'`: only the booted Simulator, never a device.
     **Declared exception:** this puts the link, with `S`, in a process argument list, readable by same-UID processes
     for the call's lifetime (Opus L12d, orch-f1, Codex 4). It is allowed only in the sandbox, for a throwaway
     identity; the alternative is scanning the terminal QR with the in-app scanner.
   - The SAS and `cert_code` prompts are typed by a human at the script's terminal (default) or, with
     `--expect-code-from FIFO`, by a code the test phone writes. The FIFO must be a FIFO (`S_ISFIFO`), owned by the
     user, inside `<root>`, and each code written is bound to the offer id and the phase (`sas` or `cert_code`): the
     script types it only when it is for this offer and this phase, so a comparison still happens. Refused unless the
     manifest exists and the origin is in the allow-list.
9. The script stays in the foreground until the pairing reaches `approved` (exit 0), `rejected`, `expired` or `failed`
   (exit 1). The host and the relay keep running for the session.

**`down` (fail-closed, ownership-based):**
- reads the manifest and checks the root's `st_dev`/`st_ino`; a missing, different or mismatched manifest means it
  deletes nothing;
- signals **only** the processes it recorded, after re-validating each immediately before signalling: same PID, same
  **process start time** (birth identity, so a reused PID is not signalled), same executable, and a working directory
  inside the root. `SIGTERM`, then `SIGKILL` after 10 s;
- logs out nothing on the remote relay (tokens expire);
- deletes the root by descending through pinned directory descriptors without following links (`unlinkat`), and
  stops with an error at anything it did not create;
- `--keep` stops the processes and keeps the root for debugging (Opus L12f).

It never uses `pkill`, `simctl erase` or `simctl delete`. Data on `relay.dev.severin.io` stays: a throwaway account and
workspace, which the operator can purge.

**What it refuses to touch:** any `ORCH_STATE_DIR`, `HOME` or workspace outside `<root>`; the person's real identity
(it never reads an existing recovery code, keychain item or person directory, and never runs `orch keys primary init`);
owner relay accounts other than the throwaway one; any relay origin outside the allow-list and any production
deployment; running under `ORCH_GRANT`, as root, or with a host already running against `<root>`.

**CI (the T2 real-relay job, 11A.10).** A step after the existing T2 runs:
- `dev-pair up --relay local --json-fd 3 --expect-code-from <fifo>` against the pinned relay (Linux runner: the pty
  works, and the passphrase backend needs no keychain);
- the **reference test phone** (`tests/relay/phone.py`): it parses the native link strictly, sends the pair request
  with a `reader_decl`, checks `pending`, writes the SAS to the FIFO, verifies the challenge and writes `cert_code`,
  then verifies `approved`, the join anchor (§8.3), `scopes` against the accepted list entry, and the link origin
  against the card's `relay_url`;
- the phone then fetches the member list and its grant, checks `wk_commit`, sends one column-R `whoami`, and expects
  the WSK-signed answer;
- `dev-pair down`;
- assertions: no leftover recorded process, the sandbox removed, and **no link or secret in stdout, stderr, pty
  captures or uploaded artifacts**, for passing and failing runs (the redaction test of 11A.9 runs over all of them,
  and `<root>/pair.json`, which holds `S`, is in the scan: its link and `S` are among the secrets searched for, and the
  file itself is never uploaded, N8b).

The job runs at the 4 MiB cap only. It is required before R6c merges (11A.1: no poll thread before R6's real-relay
tests pass). Dev-path tests (Codex 4): an existing directory (a workspace, a home) as root; symlink swaps during setup
and teardown; a forged or copied manifest; PID reuse; inherited configuration and a keychain selection in the parent;
a startup failure at every step; an unexpected listener on the chosen port; stale or wrong-phase FIFO codes.

#### 11A.15.12 Gaps and conflicts found

| # | Where | Conflict or gap | Resolution |
|---|---|---|---|
| C1 | core C8 (`PK` derived from the recovery code, never stored) vs protocol §5.2/§8.4 (the primary holds `PK`) | the primary cannot sign a cert, challenge or rejection without `PK` | **DECIDED Q1 = B** (15 Oct 2026, 11A.15.16): the recovery code typed at add or revoke; `PersonKeySource` stays pluggable |
| C2 | core `init` discards the private `dk_kx` (11A.12) vs §8.4 (the inner is sealed to the primary's `dk_kx`) | the Mac's existing device cannot open a cert request; re-certifying it is impossible (same id, other cert: `cert_conflict`) | **DECIDED 15 Oct 2026 (orch-52):** a new person-level primary key pair (11A.15.1), key type `kx`, purpose `cert-request` only |
| C3 | protocol §8.3 step 5 ("the host *is* the owner's primary device, it signs locally") vs D-P2-15 | core narrows: never the local shortcut, always `cert_pending` | **DECIDED (orch-52):** no relay change. Consequences: the human compares **two** codes on one Mac (the SAS, then `cert_code`); pairing needs the owner's relay account and primary first |
| C4 | 11A.10 ("`--port 0` with `ORCH_RELAY_ORIGIN=http://127.0.0.1:<port>`") vs the relay (the origin is fixed at start) | with port 0 the origin cannot name the port | pick the port first, verify `/healthz` `instance` (11A.15.11); 11A.10 is edited |
| C5 | protocol §8.5 G2b ("a burned epoch's key counts as held") vs §8.6 step 2 ("epoch > current: drop") | a burned epoch **above** the committed one | **DECIDED 15 Oct 2026 (orch-52): answer** a signed `stale_epoch` under the burned key, naming the pending epoch or `EHW + 1` (11A.15.2) |
| C6 | 11A.1 order (R5 before R6) vs R1's `LinkSteps.card()` (needs `WXK_1`/`CK_1` custody) | R5 cannot link without R6 key custody | **R6a** before R5 (11A.15.14) |
| C7 | §0.8.6 item 9.4 (a `via:"declare"` declaration beside a presented cert) vs proto §8.3 and R6 (`via:"pair"`) | two declaration shapes for path 7a | **RESOLVED 15 Oct 2026** (orch-52 with orch-29): R6 accepts `via:"pair"` only (step 4); §0.8.6 item 9.4 is narrowed in place ("Narrowing for pairing") |
| C8 | phone decision D-3 ("the QR always carries the universal link") vs Q5 (native `orch://`) | the QR content | **owner decision of 15 Oct 2026: Q5 yes**; D-3 is amended (11A.15.16) |
| G1 | R2 PR #467 `EpochKeySource`/`BridgeKeys` | no gate, no seal-time synchronisation, and a refusal bypass | 11A.15.2 "What R2 must provide before #467 merges" |
| G2 | protocol §8.3 ("the fragment never reaches a server") | true for HTTP, but a relay-served page sees the fragment; another app can claim `orch://` | native paths only for the hostile-relay guarantee; HTTPS labelled fallback; review screen, no auto-connect (11A.15.8) |
| G3 | `RelaySigner.response_chunk` refuses epoch 0 (11A.2) | pair answers need a signer path | `pair_answer` (11A.15.7) |
| G4 | 11A.3 "Membership and `WK_e` belong to R6" without a layout | the location was ambiguous | `membership/keys/` (11A.15.3) |
| G5 | R5's first real link vs the owner's relay account (Opus H2, orch-52) | `card_put` refuses `other_person` without a usable owner account; `POST /v2/accounts` needs a PK-signed device cert | **DECIDED 15 Oct 2026 (orch-52, stream D):** R5's T2 creates the account through the test owner client (11A.15.14) |
| G6 | the relay stores one certificate per device id (`directory.py`) | no certificate renewal or widening | the certificate policy of 11A.15.6; Q15 decided: no relay change now, a pre-public relay item (11A.15.14) |

**Relay doc items for orch-29** (none changes relay behaviour): §8.5, the C5 sentence with the reading above; §8.3 step
6 lists the cert, the grant and the list as three uploads, but the relay takes the grants inside the list body; §8.4,
a primary's rejection withdraws an open or challenged request; §7.2, an empty list is accepted after the first.

#### 11A.15.13 Where the relay protocol is silent (proposed rules)

- **Offer state on the relay:** none. The relay forwards `route:"pair"` items for any offer id (`bridge.py`). Offers,
  their expiry and their cancellation are host-local. An unknown offer is silence (§8.6 step 2).
- **How long `pending` waits for the host's human:** until the phone's deadline (11A.15.6), then `expired`.
- **Label cleaning** ("records the cleaned label", §8.3; DECIDED 15 Oct 2026, orch-52 re-check of beafc6c8, D1): the
  one `clean_label` of H12c item 4 is protocol §2.5 exactly as #476 implements it: remove Cc (except LF), Cf, Zl, Zp,
  Co, Cn and Default_Ignorable; **no normalisation**; at most 80 code points. One function, no second cleaner. The order:
  1. **verify** the phone's signature over the **original** label bytes (no cleaning, no NFC before verification);
  2. **`clean_label`** for relay-side display and the pair state (the offer record, the cert-request inner, `list`);
     the recorded value is `clean_label`'s output byte for byte (vector `pair_with_a_label_full_of_controls`);
  3. **No NFC step.** The cert's `label_sealed` carries the `clean_label` output **unchanged**.
  **CORRECTION of orch-52's 15 Oct 2026 label ruling (docs only, 15 Oct 2026, by orch-52):** the ruling had an NFC step
  in "the F1 copy" of the label, in `device.added` and the member list entries. Neither carries a label: F1
  `device.added` is exactly `{device, cert}` and the protocol §7.2 member-list entries are `{device_id, person_id,
  scopes}`. The only signed label is the certificate's `label_sealed`. So there is no NFC step for the label; the order
  is: verify over the original bytes, `clean_label` (protocol §2.5, no normalisation) for relay-side display and pair
  state, and the cert's `label_sealed` carries the cleaned output unchanged. NFC can at most apply to the review
  rendering of `relay.pair_device`, which signs nothing of the label.
  "Empty becomes phone" is **display-only**, never the recorded label. Shown with a "named by the device" prefix, never
  as an authenticated name. Cn follows the running interpreter's Unicode database: acceptable because the cleaned label
  is display-only and never authenticated.
- **When the host releases a baseline:** on the device's first verified column-R request, unless another live offer
  names the same hash; else at expiry.
- **Default scopes for a phone:** `["look","decide"]` in the list; the phone certificate's ceiling is the first pairing
  offer's scopes, no `operate` in v1.7 (N1, 11A.15.6).
- **Card fetch between `PUT card` and `PUT card-keys`:** the device keeps the previous card's sealed names until its
  wrap arrives (an orch-f1 rule).
- **More than one pairing per workspace:** one live offer and one outstanding cert request.
- **Host-side deadlines:** derived from the phone's (11A.15.6), with a 2-minute margin before step 9.

#### 11A.15.14 Tests, vectors, implementation order and effort

**Vectors consumed** (relay corpus, re-pinned to `438b719`, orch-f1 S8; the phone is at `04da0e7`):
- `wk_commit`, `member_lists` (34), `member_list_snapshots`, `wk_grants`, `seal`/`seal_open`;
- `cert_request` (`cases`, `open_cases`, `challenge`), `cert_code`, `signed_cert`, `sign_again`, `attack_challenge`,
  `cert_binding`;
- `bridge.sas`, `bridge.links`, `bridge.host_cases` (`pair_*`, `pair_status_*`), `pair_answers`, `rejected_forms`,
  `approved_anchor`, `reader_decl` (28, with the real `reader_kat_1` digests of `438b719`);
- `card.cases`, `revocations`, `revocation_op`;
- `request_under_burned_epoch_key_gets_signed_stale_epoch`;
- the HTTP fixtures `membership`, `card_keys`, `cert_requests_post`, `cert_request`, `baselines`, the `pair` routes.

**Host vectors (new, core-owned; §11's 23 membership vectors are R6's too):**
- pairing: `pair_offer_consumed_once_second_device_pairing_closed`, `pair_second_tag_valid_key_warns_cli`,
  `pair_resend_same_rid_replays_exact_answer_bytes`, `pair_refusal_bytes_replayed_after_crash`,
  `pair_status_new_rid_answers_current_state`, `pair_status_answer_per_state_table` (every row of 11A.15.6),
  `cert_pending_challenge_byte_identical_never_null_again`, `pair_resend_answers_pending_in_every_later_state`,
  `presented_cert_path_answers_pending_until_approved`, `pair_answer_codes_within_phone_set`,
  `pair_reader_decl_rejected_before_sas_and_primary`, `pair_reader_decl_other_attempt_or_offer_refused`,
  `pair_reader_decl_first_stored_wins`, `pair_reader_decl_declare_shape_refused`,
  `pair_cert_presented_other_person_refused`, `pair_cert_for_other_keys_refused`,
  `pair_cert_scopes_above_offer_refused`, `sas_typed_code_mismatch_rejects`, `sas_third_wrong_code_rejects_offer`
  (N3c), `pair_meta_v2_with_reader_decl_accepted`, `pair_meta_signature_over_original_label_bytes` (H1, D1),
  `pair_preview_blocks_click_flag_or_convenience_custody`, `pair_preview_convenience_history_warns_only` (N3a),
  `no_convenience_env_any_nonempty_value_refuses`, `no_convenience_env_checked_in_core_functions`,
  `no_convenience_env_way_back_stays_available`, `no_convenience_env_leaves_every_other_op_unchanged` (N8a),
  `excluded_device_readmitted_only_by_new_approved_offer` (N2), and the three cert-ceiling tests of 11A.15.6 (N1);
- deadlines and ends: `step9_refused_near_phone_deadline_offer_expired`, `allocated_past_deadline_expired_excluded`,
  `abandon_published_offer_excludes_and_rotates`, `approved_never_connected_raises_needs`,
  `cancelled_open_offer_answers_expired`, `cancelled_consumed_offer_answers_rejected`,
  `cancel_after_allocated_refused`, `cancel_cert_pending_then_repair_waits_or_rejects_outstanding`,
  `expired_offer_silent_after_tail`;
- allocation: `no_wk_sealed_before_cert_and_allocation`, `allocation_gate_refuses_old_reader_nothing_appended`,
  `crash_after_device_added_recovers_reader_record_devices_and_allocated`, `crash_recovery_matches_by_intent_not_cert`,
  `restart_at_every_allocation_write`, `allocation_idempotent_same_device_same_keys`,
  `allocation_removed_device_id_refused_revoked`, `pair_device_refuses_convenience_auth` (and the four convenience
  tests of 11A.15.6);
- publication: `approved_numbers_are_the_acknowledged_publication`, `approved_scopes_equal_accepted_entry`,
  `link_origin_equals_card_relay_url`, `approved_baseline_never_null_when_published`,
  `add_refused_revoked_forces_rotation_and_fails_offer`, `false_content_refusal_then_success_device_stays_excluded`,
  `holders_subset_of_desired_on_every_publication`, `second_same_epoch_addition_reuses_persisted_grants`,
  `last_device_removed_publishes_empty_rotation`, `first_list_never_empty`, `first_list_rotation_path_no_gate`,
  `lost_publication_response_resends_exact_bytes`, `reordered_obsolete_2xx_ignored`,
  `late_2xx_for_burned_body_never_activates`, `unmatched_rotation_due_rate_limited`,
  `baseline_release_keeps_other_offers_pin`, `baseline_withheld_until_deadline_expires`;
- keys and gate: `orphan_wk_key_file_counts_as_burned`, `two_wk_commit_same_epoch_refused_by_signer_ledger`,
  `signer_refuses_list_below_epoch_floor`, `seal_response_refuses_data_under_gated_or_old_epoch`,
  `seal_response_refuses_refusal_payload`, `gate_raised_inside_store_append_before_lock_release`,
  `gate_seal_interleavings_deterministic`, `gate_up_stale_epoch_names_target`,
  `burned_above_named_epoch_is_pending_or_ehw_plus_one`, `lhw_recovered_from_ledger_after_lost_head_write`,
  `r6view_list_is_own_committed_body_not_relay_served`, `pk_pub_from_genesis_delegation_not_relay`,
  `primary_cert_verified_before_every_cert_request`;
- threads: `pair_answered_within_budget_during_membership_backoff`, `pair_queue_overflow_counted_drop_completed`;
- cards: `card_rotation_order_card_then_card_keys`, `card_rotation_resumes_from_stored_card_bytes`,
  `stale_card_foreign_card_alarms`;
- primary journal (Codex 1): `primary_crash_before_after_each_journal_write`,
  `primary_resumes_challenged_request_absent_from_open_queue`, `primary_concurrent_owner_clients`,
  `primary_refuses_altered_keys_after_confirmation`, `primary_refuses_sign_after_cancel_expiry_rejection`,
  `primary_same_request_id_other_bytes_refused_alarmed`, `primary_journal_pruned_after_expiry_plus_7d_tombstone_kept`
  (N7);
- same-UID boundary: a documented test that shows a same-UID process can read the person-key source's material under
  the chosen Q1 option (the boundary is stated, not hidden).

**Adversarial release gate** (11A.10 table, new row R6): every hostile-relay case of 11A.15.8 through the proxy in
front of the real relay; a crash at every durable step of 11A.15.6 and 11A.15.4; two phones racing; a stolen link with
a correct and with a skipped comparison; old offer, attempt and rid replays; the H7 triggers; a relay answering a
different `{list_seq, epoch}` on 2xx; the 2 MiB membership body cap with 1 024 members.

**R5 and the owner account (DECIDED 15 Oct 2026, orch-52 for stream D; Opus H2).** R5's first real link needs the
owner's relay account: `card_put` refuses `other_person` without a usable owner account, and `POST /v2/accounts` needs
a PK-signed device cert and an invite. So:
- **R5's real-relay (T2) test creates the owner account** through the relay admin invite (`orch-relay invite create`),
  then `POST /v2/accounts` with test keys through the reference test owner client (`tests/relay/owner.py`);
- **H12c items 9 to 11** (the real owner client and person-key custody) run in S1/S2, in parallel with R5 and R6b/R6c,
  off the S3 path (N6, 15 Oct 2026; 11A.15.15), and **before the first real device**. The product `relay.link` is not
  used on a real relay before then (release gate, 11A.10).

**Order and effort (estimates, one implementer; review rounds NOT included):**

| Step | Group | Owner | Effort (working days) |
|---|---|---|---|
| 1 | H12c items 1 to 8, 4b, the pure half of 10, the transport of 11 (items 1 to 4 under way) | orch-52 | 2 to 2.5 |
| 2 | **R6a** key custody (`membership/keys/`, `wk`/`wxk`/`ck`, `cards/`, EHW recovery, orphan rule), `EpochKeySource` with `refusal_key` and `seal_snapshot`, the seal lock and `R6Gate`, the `RelaySigner` methods and ledger (epoch floor), the first card's keys for R1/R5 | orch-29 | 1.5; **before R5** (C6) |
| 3 | R2 gap G1 (the seven items of 11A.15.2) | orch-29, in #467 | 1 |
| 4 | R5 with the T2 owner account through `tests/relay/owner.py` | stream D | as planned for R5 |
| 5 | **R6b** membership publisher (H1 to H8, desired list, exclusion set, holder invariant, empty rotation, revocation triggers, H7, `R6View`) on the membership thread | orch-29 | 2.5 to 3 |
| 6 | H12c item 12 (`relay.pair_device`, convenience refused) with H12d-2 records | orch-52 | 1 |
| 7 | **R6c** pairing host side (11A.15.6, the pair thread, deadlines, answers table, outstanding request, allocation intent) and the CLI (11A.15.9, `--primary external` first), with H12d-3 | orch-29 | 3 to 3.5 |
| 8 | **R6e** the dev script (hardened, 11A.15.11) and the T2 pairing step, with the reference test phone and test owner client | orch-29 | 1.5 to 2 |
| 9 | **R6d** WXK card rotation (11A.15.5) | orch-29 | 0.5 to 1 |
| 10 | H12c items 9 to 11 product path (custody `kx`/`unseal`, one-command unlock, `PersonKeySource` = `RecoveryCodeSource` per Q1 = B, `orch keys primary`), then the inline primary mode and the primary journal; **runs in parallel with steps 4 to 8** (S1/S2, N6), sharing `check_cert_request`/`open_cert_request`/the journal logic with `tests/relay/owner.py` | orch-52 + orch-29 | 3 to 3.5 |
| 11 | **PRE-PUBLIC relay item (Q15):** the **same-keys certificate renewal route** in orch-relay (a later `expires_ms`, same keys, same or narrower scopes, for an existing device id). Not needed for the first device; needed before the relay is public | orch-29 (relay) | not estimated |

Total implementation: about 17 to 20 working days, plus R5. **In calendar time, plan 2 to 3 times that** (Opus L16):
R1 alone took seven review rounds, and every R6 group and the H12c custody items need an Opus review, an Opus delta
pass on every security fix commit, and orch-52's review. The accelerated order below shortens the path to the first
Simulator pairing, not the total.

**Reviews:** Opus for every R6 group and the H12c custody items (auth, crypto, custody); an Opus delta on every
security fix commit; orch-52 for every group.

#### 11A.15.15 Accelerated order (owner ask of 15 Oct 2026, through orch-d6)

The owner wants an end-to-end pairing as early as possible. The plan: land the **scripted dev path first**, on
sandbox-only keys, and keep production person-key custody (Q1) off that path but gated before any real device.

**What makes it possible.** The dev path (11A.15.11) uses a throwaway sandbox identity whose recovery code and person
key live under a sandbox passphrase in the sandbox, and a test owner client in the test tree as the primary
(`--primary external`). It therefore does **not** depend on Q1, on Q2's custody work, or on H12c items 9 to 11. The
host side it exercises is the product code (R6a, R6b, R6c, `relay.pair_device`).

**Slices, in order, and what each gives orch-f1 (the phone):**

| Slice | Contents | Can run in parallel with | What the phone can test then |
|---|---|---|---|
| **S0, now** | H12c 1 to 8 + 4b + pure half of 10 (orch-52; 1 to 4 under way); R6a (orch-29); G1 in #467; R3 and R4 continue in parallel with R2's review rounds; owner-client transport (item 11) against fixtures | everything in S0 is independent | cross-check its own builders against core's H12c vectors (SAS, `cert_code`, links, meta, answers, member lists, grants) |
| **S1** | R2 merges (with G1); R5 merges with the T2 owner account through `tests/relay/owner.py` | H12c item 12 (after H12d-2); **H12c 9 to 11** (custody `kx`, the unlock, `RecoveryCodeSource`, `orch keys primary`, then the inline primary mode and the journal), started now that Q1 = B (N6) | a linked dev workspace exists on a relay (card, held checkpoint); nothing the phone can read as a member yet |
| **S2** | R6b minimal (first list, same-epoch addition, exclusion set, holder invariant, empty rotation, gate) and R6c with `--primary external` | R6d (card rotation) is deferred to after S3; H12c 9 to 11 continue (N6) | none on the Simulator yet: CI runs the Python reference phone end to end on `--relay local` |
| **S3: first E2E** | R6e (hardened `dev-pair`, the T2 pairing step) | phone PR B (live pairing, `orch://` handling, review screen, in-app scanner) | **the Simulator pairs against `relay.dev.severin.io`** with a `dev-pair` link (in-app scanner or `--open-simulator`), sees `approved`, fetches its grant and makes column-R reads (`whoami`, then the 11A.14 phone events it is scoped for); OrchKit tests can use `--relay local` |
| **S4: production gate** | H12c 9 to 11 landed (moved into S1/S2, N6); the T2 pairing **rerun with the product inline primary**; R6d; the adversarial release gate | — | the **first real device**, paired by the owner's real primary. This gate is not skipped: the dev path's test owner client is never packaged and refuses outside a sandbox manifest |

**The critical path to the first Simulator pairing (S3)** is: R2's remaining review rounds with G1 → R5 (with the T2
owner account) → R6b minimal → R6c → R6e, joined by H12d-2 → H12c item 12 before R6c merges, and by phone PR B. R6a and
H12c 1 to 8 run before R5 in parallel. The R6 part of the path is about 8 to 10 working days of implementation
(R6b minimal 1.5 to 2, R6c 3 to 3.5, R6e 1.5 to 2, plus R6a and G1 2.5 days before R5), plus R5 and R2's remaining
rounds; H12c 9 to 11 (3 to 3.5 days) and R6d (0.5 to 1) are off it. **Honest calendar estimate: 2 to 3 times the
working-day figure, so about 3 to 5 calendar weeks to S3** from the start of R6a, assuming R2 and R5 do not slip and
phone PR B is ready by then. **N6 (DECIDED 15 Oct 2026, orch-52 re-check of beafc6c8; the owner wants the real device
soon):** with Q1 = B decided, nothing blocks H12c 9 to 11, so they run in S1/S2 in parallel, off the S3 path, instead of
adding 2 to 3 calendar weeks after S3. `tests/relay/owner.py` and the product inline primary share
`check_cert_request`, `open_cert_request` and the journal logic, and the S4 gate reruns the T2 pairing with the
**product** primary.

**What must not be shortened:** the Opus review of every R6 group and the custody items; the deadline, answers and
exclusion rules (they decide whether the phone shows the right message and whether a refused device can return); the
seal gate in #467.

#### 11A.15.16 Open questions and decisions (for orch-52, orch-d6 and the owner)

**Q1. Where does the person's master key (`PK`) live once phones pair through the relay? DECIDED B (owner, 15 Oct 2026, through orch-d6).**
**Decision:** the recovery code is typed **only** when a device is added or revoked; **nothing new is stored on disk**;
the Secure Enclave takes over in phase 3; `PersonKeySource` stays pluggable. The options list below is kept as the
record: A, A' and the separate primary were **not chosen**. (orch-52 and Opus recommended B; Codex names the separate
primary as the only protection against a compromised Mac, which stays reachable through `--primary external`.)

In plain language. Your 24-word recovery code *is* your person key. It proves every device is yours, everywhere, and
it cannot be changed: if someone gets it, they can add their own devices as you and remove yours, and peers keep
trusting it. Today it exists only on your paper (core rule C8). Phone pairing needs it to sign a certificate for each
**new** device (an already-paired phone presents its certificate, so a second workspace needs no code), and to revoke
a lost device.

| Option | What you do | What it protects | What it risks |
|---|---|---|---|
| **B (CHOSEN, 15 Oct 2026): type the recovery code when you add or revoke a device** | type the code on the Mac's terminal when a brand-new device is added (once per device, not per workspace) or one is revoked | nothing new on disk; the key sits in memory for that one command and is wiped (best effort) | a program running as you that records your typing at that moment gets the key. Fetching the paper code is the cost. In phase 3 the key moves into the Secure Enclave and you stop typing it |
| **A' (not chosen): a copy on the Mac under a separate "identity passphrase"** | type a second passphrase, different from your daily one, only when you add or revoke a device | no paper fetch; a captured daily passphrase does not open it (at creation, the passphrase is refused if it opens any device key file) | a program running as you can copy the file and try to guess that passphrase offline, forever; or record it when you type it. Was the fallback if B were unacceptable |
| **A (the first draft; not chosen): a copy under your daily signing passphrase** | nothing extra | convenience only | that passphrase is typed many times a day; a program that captures it once and copies the file gets your permanent identity |
| **Separate primary (Codex; not chosen as the default): the key lives on another trusted device with its own screen** | confirm each new device on that other device (for example a second machine you keep clean, later a phone with Secure Enclave) | the only option where a compromised Mac cannot get the key; the confirmation is shown on a screen the Mac does not control | needs a second trusted device and its software (the owner client on it); more setup. Protocol-compatible today (`--primary external`) |

**What every same-Mac option shares (stated honestly, Codex 1):** a program running as your user can defeat all of
them, by copying files and recording what you type. The key never enters the orch host daemon, but it does enter the
`orch` command you run. Recovery restores a lost key; it does not invalidate a stolen copy.

**What the design does for each:** a `PersonKeySource` with `sign` (person labels only) and `derive_label_key`:
`RecoveryCodeSource` (B; asks on `/dev/tty` only when step 7b is reached or for a revocation, holds the key hardened for
the one command, then zeroises it), `StoredPersonKey` (A': its own file, strong scrypt parameters recorded in the file,
excluded from backups, signing restricted to `sig_cert_challenge`, `sig_device_cert`, `sig_cert_rejection` and
`sig_revocation`, never `sig_ws_delegation`), `SecureEnclaveKey` (phase 3), and `--primary external` for the separate
primary. Option A is not implemented. **Under the decision:** R6 and H12c build `RecoveryCodeSource` (B); `StoredPersonKey`
(A') is not built; `SecureEnclaveKey` is phase 3; the interface stays pluggable. The other 11A.15 mentions of "the Q1
option" read "B".

**Q2. The primary is a new person-level key pair, not an F1 member.** DECIDED 15 Oct 2026 (orch-52): yes, with the
`kx` key type, purpose `cert-request` only, `scopes_max ["look"]`.

**Q3. Always `cert_pending`, never the local-sign shortcut.** DECIDED (orch-52): yes; two comparisons on one Mac.

**Q4. Default phone scopes.** DECIDED (orch-52): `look, decide` in the list (11A.14 needs `decide`); `type` refused.
**Revised 15 Oct 2026 (orch-52 re-check of beafc6c8, N1):** `operate` is refused for phones in v1.7 until the
renewal route exists (Q15), and the phone certificate's ceiling is the first pairing offer's scopes (11A.15.6).

**Q5. QR content.** **OWNER DECISION, 15 Oct 2026 (through orch-d6): yes**, the QR and the link carry the native
`orch://pair?relay=…#v2…`, with orch-f1's mitigations: the in-app scanner is the primary path; every `orch://` open
lands on a review screen with no auto-connect; the HTTPS form is printed as a labelled fallback. **Amendment of phone
decision D-3** ("the QR always carries the universal link"): D-3 now reads "the QR carries the native `orch://` link;
the universal link is a printed fallback". Universal links cannot cover self-hosted relays and would be served by the
relay operator.

**Q6. One live offer per workspace.** DECIDED (orch-52): yes, with deadlines, `abandon` and the outstanding-request
rule.

**Q7. R6 threads.** DECIDED (orch-52, revised): two threads (membership, pairing), event-driven pairing; 48 threads and
≤ 336 MiB at the 8-workspace cap.

**Q8. Retention of old and burned `WK`.** DECIDED (orch-52): never delete in v1.7; a policy with Drop (P4).

**Q9. While the gate is up, R2 answers `stale_epoch` naming the target.** DECIDED (orch-52): yes, with the seal-time
snapshot and the typed refusal builders.

**Q10. R6a before R5.** DECIDED (orch-52): yes, and R5's T2 creates the owner account through the test owner client.

**Q11. QR dependency.** DECIDED (orch-52): optional `orch[qr]` with `segno` (BSD-3), link-only fallback.

**Q12. The member list holds relay-paired devices only.** DECIDED (orch-52): yes; the empty rotation when the last one
leaves.

**Q13. A pairing that fails after `device.added`.** DECIDED (orch-52): a `needs` item, never an automatic
`device.removed`, with the immediate local exclusion; extended to the deadline cases (orch-f1 B2).

**Q14. C5.** DECIDED 15 Oct 2026 (orch-52): answer a signed `stale_epoch` (11A.15.2).

**Q15. Certificate renewal. DECIDED 15 Oct 2026: no relay change now.** The relay stores one certificate per device id, so
v1.7 uses `expires_ms: null` and one certificate per device id. The route that accepts a renewed certificate for the
same keys (a later `expires_ms`, the same or narrower scopes), so phone certificates could expire, is logged as a
**PRE-PUBLIC relay item** (11A.15.14, step 11): it is needed before the relay is offered to anyone else, not before the
first device.

**Q16. A Debug-only loopback relay in the app (raised by orch-f1). DECIDED NO (owner, 15 Oct 2026, through orch-d6).**
The Simulator uses `relay.dev.severin.io`; `--relay local` stays for the Python reference phone and OrchKit tests. Closed.

#### 11A.15.17 Review record (15 Oct 2026)

| Input | Verdict | Adopted | Not adopted, or changed (dissent recorded) |
|---|---|---|---|
| Opus review (for orch-52) | SOUND WITH CHANGES | H1 → Q1 rewritten (B recommended); H2 → R5 T2 owner account; M1 empty rotation; M2 exclusion set; M3 seal-time gate with the store hook; M4 typed refusal builders; M5 split threads; M6 answers table, deadlines, `abandon`; M7 `primary.json`; L1 to L16 (side file, outstanding request, stored card bytes, `relay.pair_cert`, pin release, `rotation_due` limit, first list, signer floor, builders, typed SAS entry, cert policy, dev path, G2, rebase and 11A.12, relay doc items, estimates) | C5/Q14: Opus agreed with drop; orch-52 decided answer. L11: Opus wanted certificate expiry and renewal; v1.7 uses `null` because the relay stores one cert per device id (Q15). M3: Opus offered two forms; the store hook was chosen, the R2-side computation from `holders` and F1 state was not |
| Codex challenge | flawed as written | primary journal; same-UID boundary stated, "never enters the host process" corrected; native paths only for the guarantee; quarantine and the holder invariant; allocation intent and full recovery; outstanding cert request; grant reuse; "approval names the acknowledged publication"; seal/gate serialisation; typed refusals; no late activation of burned epochs; bounded pair queue; dev path hardening (exclusive root, pinned identity, no-follow, birth identity, env allow-list, private captures, argv exception, marker is not containment) | Q1: Codex's safest option (separate primary) is presented and supported (`--primary external`) but not recommended as the default: it needs a second trusted device. C5: Codex accepted drop as safe; orch-52 decided answer |
| orch-52 review and decisions | CHANGES, docs only | everything listed in the brief: 11A.15.0 primary wording; C1 reframed; C5 answer with `refusal_key`; convenience refused; G2 additions; builders 4, 8, 9, 10, 11; ordering; 11A.14 and H12d consistency | none |
| orch-f1 phone review | 3 blockers, 8 should-fix | B1 answers table; B2 deadlines; B3 Simulator on `relay.dev`; S1 scheme hijack and D-3 amendment; S2 cert ceiling and no expiry; S3 idempotent allocation, `revoked`; S4 first declaration wins, `via:"pair"` only; S5 refusal codes; S6 cancelled open offer `expired`; S7 scopes and origin equality; S8 re-pinned vectors; nice: `primary_label` source, `simctl` argv | S6 was applied to `open` offers; a cancelled **consumed** offer answers short `rejected` (JUDGEMENT CALL: the human refused it) |
| Owner (through orch-d6) | — | Q5 yes with orch-f1's mitigations; the accelerated order (11A.15.15) | Q1 was open; decided B on 15 Oct 2026 (below) |
| Decided amendments (15 Oct 2026) | docs only | Q1 = B (owner, via orch-d6); Q16 = no (owner); C7 narrowed in 9.4 (orch-52 with orch-29); Q15 = no relay change now, pre-public item; dashboard pairing and the internal API (orch-52 with orch-fa, contract v1.9; 11A.15.9A); one `clean_label` (#476); notes from #476's review | none |
| orch-52 re-check of `beafc6c8` (15 Oct 2026) | CHANGES, docs only (20 items: 15 covered; 11, 13, 20 partly or regressed) | H1 meta v2 = six fields plus the optional `reader_decl` (item 4, step 3, #476 notes; #476 fixed before merge); D1 label rule = §2.5 as #476, no NFC in `clean_label`, order verify-original → `clean_label` → `label_sealed` carries the cleaned output unchanged, "empty → phone" display-only (11A.15.13, items 4 and 12, steps 3 and 9; the NFC step in an "F1 copy" was a mistake and is corrected in 11A.15.13: neither `device.added` nor a member-list entry carries a label); N1 cert ceiling = first offer's scopes, no `operate` for phones in v1.7 (11A.15.6, Q4, the 11A.14 consistency line); N2 re-admission through a new approved offer (11A.15.4, step 9); N3a `pair.preview` blocks on the flag or convenience custody, history is a warning; N3b entry points `orch host relay pair --resume --offer O` and `orch sign CODE`; N3c 3 SAS attempts per offer (JUDGEMENT CALL taken by orch-52); N4 step 9 terminal hand-off only; N5 nothing in the snapshot takes the store lock; N6 H12c 9 to 11 into S1/S2, shared owner-client code, S4 reruns T2 with the product primary; N7 journal path, `flock`, same-id check, retention; N8 passphrase forcing (narrowing-only env rule) and `pair.json` in the redaction scan; N9 import-light test | N8a's `ORCH_NO_CONVENIENCE` narrowing variable was a JUDGEMENT CALL of this revision, ACCEPTED by orch-52 (15 Oct 2026) on the four conditions in 11A.15.11 (any non-empty value refuses; checked inside the core functions with `sign.convenience_unavailable` reason `env`; never blocks `convenience off` or `custody passphrase`; test that every other op is unchanged) |

**Consistency with 11A.14.** Phone person events need `decide` (F1 §5.3): the default list scopes include it.
`operate` stays out of both the list entry **and the phone certificate's `scopes_max`** (N1: the ceiling is the first
offer's scopes, never `operate` in v1.7), and F1 §5.3 gates admin events on the certificate's `operate`, so a phone signs
no admin event. `auth ∈ {secure-enclave-unlocked, passphrase}` matches the phone's
certificate. R6 appends `device.added` (step 9) before the phone's first `relay.submit` can verify (step 10 follows).

## 12. Phase 2: the addons on the host side (#307, #308)

Status: decided 12 Oct 2026 by orch-52 (draft by stream A, Codex challenge, Opus tie-break adopted in full, the
dashboard stream's nine asks adopted) with three **owner decisions of 12 Oct 2026** (D-P2-27, D-P2-31, D-P2-34). It
covers #307 (start an agent from the dashboard, D09; terminals, D16) and #308 (worktrees, C18; quick tasks, C16;
records commit and push, C29/#253, the closed #254 as reference). Contract additions are **v1.6** (§0.4, §0.13 item
16). **[SC]** marks a security-critical decision: its implementation needs the Opus review, and every change to it an
Opus delta pass.

**Phase 2 is tier A.** Agents, the runner, the dashboard server, the click signer and the person are one UID. No column,
switch or process split below is a boundary against a same-UID process; each says what it is (policy, fault separation,
protection against passive disclosure). §9 item 11 lists what tier B (phase 3) changes. Windows stays in direct mode.

### 12.1 Overview

```
Browser (xterm.js) ──fetch, fetch-SSE──► Dashboard server ──socketpair──► click signer (orch.click)
        └──WebSocket /term (ticket)──►          │  (person UID)                    │ grant.deposit (P + secret)
                                                 │ workspace socket, person class   ▼
                                                 ├──────────────────────────► Host (never spawns anything but orch.gitread)
                                                 │                                 │ runner socket <run>/runner/host.sock
                                                 │ term socket (attach capability) ▼  (class `runner`)
                                                 └──────────────────────────► Runner (orch-runner; tier A: person UID)
                                                                                   ├─ PTY runs: agents, shells
                                                                                   └─ git for worktrees
Records tool (python -I -m orch.records, one shot, person UID): from `orch records`, the dashboard server, or a timer.
```

- **Control goes through the host** (column, person, visibility, grant, claim, quotas, audit). **Terminal bytes go
  directly** between the dashboard server and the runner, on a runner-owned socket, under a capability the host mints
  and can end at any time (D-P2-25).
- The host still never runs git, never opens a repository file except through its reader child (D-P2-19), and holds no
  grant secret beyond the moment it forwards a deposit.
- Grant secrets never cross the browser, the dashboard HTTP process or any log (protection against **passive**
  disclosure, as §0.8.6 item 0). A dashboard takeover can still make a started agent print its environment.

### 12.2 Decisions

| # | Decision | Reason | |
|---|---|---|---|
| **D-P2-24** | **A separate per-UID runner service** (`python -I -m orch.runner`) supervises agent runs, terminals and worktree git; never the host (its only spawn stays `orch.gitread`, D-P2-9) and never the dashboard server. Tier A: LaunchAgent `io.orch.runner` / systemd user unit `orch-runner.service`, person UID, installed by `orch runner enable` like H7's unit. Tier B: one system service per agent UID. **Same-UID separation is not containment: runner reports are untrusted and cannot establish authorization; reports are accepted only for runs assigned to that runner.** The runner accepts commands (and `grant.put`) only after checking that the peer UID of `<run>/runner/host.sock` is the host UID (`getpeereid`/`SO_PEERCRED`); one runner connection per runner UID, bound to the runner unit's main PID and to the runner's generation (§12.4). The runner concentrates every deposited grant: compromising it (or, in tier A, any agent) yields all of them; tier B confines this per agent UID. | Sessions outlive host and dashboard restarts; the key-holding host never parents workspace code; tier B needs the spawner to be the agent UID. | **SC** |
| **D-P2-25** | **Control through the host, terminal bytes direct.** Clients call host ops (§12.3). The host mints an attach capability, registers it at the runner over the command stream (`attach.allow`) and can end it (`attach.end`); the dashboard server connects to the runner's term socket and presents the ticket once (§12.6). The host relays no terminal byte and has no stream router. | Keeps terminal floods out of the failure domain of signing, revocation and the D58 precommit; the dashboard server is already trusted (it can click-sign); the enforcement point does not move. | **SC** |
| **D-P2-26** | **Columns** (§12.3.1): start, stop, open, attach, write, resize are **P only**, attach/write/resize only for the run's own person; **G never** (F1 §8: `pty` is never granted to agents); **N never**. `run.list`/`run.sub`: P sees its own person's runs; G sees only `kind:"agent"` runs on tickets inside its grant's scope, never shells; owners and maintainers do not see other persons' runs; viewers see none. Worktrees: P on a visible ticket; G only on the ticket its session holds the live claim on, rechecked when the runner executes. Records: owners and maintainers. Authorization is rechecked at execution and before every delivery of queued data. Recovery without the host or membership: `orch runner stop RUN\|--all` (terminal, local). | Typing into a PTY is code execution; agents must not drive each other or the person's shell; a removed person must still be able to stop their own processes. | **SC** |
| **D-P2-27** | **The runner grant** (**owner decision, 12 Oct 2026: click-issued agent grants allowed**, at most 12 h, the typed word `<PREFIX>~<WS8>/GRANT`, handed straight to the runner, never shown in the browser). The runner holds at most one runner grant per (workspace, person), in memory only, and reuses it for every agent it starts (F1 A3 standing grants). It gets it only through `grant.deposit`, a person-class op that also carries the secret in `auth.grant` (**amended in H11c review, owner decision of 15 Oct 2026:** at the permission matrix it is column G, a verified `auth.grant`; the op itself then requires a person-class peer whose UID maps to a valid member and a grant that is that same person's `label:"runner"` grant, which is equivalent to "P-class plus `auth.grant`"), sent by the process that drew the secret: the click signer in **deposit-only grant mode** (amends §0.8.6 item 6), `orch grant --runner`, or `orch sign CODE` (it asks "show or hand to the runner"). Only a grant whose `grant.issued` has `label:"runner"` can be deposited; a forgotten or replaced runner grant id cannot be deposited again (`grant.forgotten`). Every deposit has a `deposit_id` with outcome `confirmed`, `rejected` or `unknown`. Owner's plain-language risk: *"Anything that takes over the dashboard can give agents standing authority to act for you on tickets for up to 12 hours."* | Start-from-dashboard as one daily flow, while the secret stays out of the HTTP process and the browser; an agent cannot re-point the runner to its own grant. | **SC** |
| **D-P2-28** | **Launch specs are computed by core** from a closed harness registry (`claude-code`, `codex`, `shell`; the F1 `actor.id` values) and validated values (ticket, mode, model, subagent model). Argv only; the prompt is one element ("data to the OS, instructions to the harness"). `agent.preview` returns a `spec` digest; `agent.start {…, spec}` recomputes it and refuses `launch.changed` on any difference; preview has no side effects. **Agents start through the person's login shell** in exec form `[$SHELL, "-l", "-c", 'exec "$0" "$@"', argv…]` (a POSIX shell is required, else `harness.shell_unsupported`); the preview says "the program is found and started through your login shell; your shell profile runs first and can change what runs". Opt-out: `runner.json {login_shell:false, programs:{…absolute paths…}}`. Environment allow-list (§12.5.3) with a `pass_env` deny-list. | The client chooses values, never a command; without the login shell launchd-started harnesses are not found on a normal Mac. | **SC** |
| **D-P2-29** | **Supervision.** Every child starts through a fixed trampoline (`python -I -m orch.runner.exec`). Linux: each run enters its own cgroup **before** `exec` and stops with `cgroup.kill` until `populated=0`. macOS: process group plus a sweep of the run's session id, **best-effort cleanup, not containment** (a `setsid()` descendant escapes). `run.stop` answers `cleanup:"complete"\|"incomplete"\|"unknown"`. Every signal targets a (pid, birth time) pair re-read right before `kill`; the runner kills only its own runs, never by name, never UID-wide. **No CPU, memory or process-count limits in phase 2** (agents are the person's own programs; revisit with tier B). | Honest claims; "kill only own PIDs" on a shared machine. | **SC** |
| **D-P2-30** | **Terminal transport.** The runner reads every PTY continuously into a 1 MiB ring; a slow or absent viewer never blocks an agent. **Attach is read-only by default**; the write lease is taken only when the capability is consumed, only if nobody holds it, or with explicit `take:true`; a reconnect or remount never takes it implicitly. Writes and resizes carry the **lease generation** and an **input id**, both checked at the runner; replay never generates input; a `gap` resets the client's parser. Control frames are never dropped. xterm.js runs with the hardened configuration of §12.6.7 (no links in phase 2). A separate restricted origin for terminal rendering is **deferred to phase 3** (accepted risk, owner FYI). | Fixes double input, lease ping-pong and answerback injection. | **SC** |
| **D-P2-31** | **Dashboard execution** (**owner decision, 12 Oct 2026: accepted**; start agent and terminals may be switched on per workspace from the terminal; tier B narrows it in phase 3). One machine switch, `orch runner enable --workspace W` (terminal only: `/dev/tty`, refused under `ORCH_GRANT`, prints the risk, requires typing the workspace prefix, records time and text), gates agent start, worktree git and terminals; the workspace also needs the addon granted (`addon.granted`). `--no-terminals` is a UX preference only and does not make agent execution safe. The dashboard has no op that turns it on; a same-UID process can. **Card line** (workspace card, Start dialog, terminal header): **"Dashboard execution: agents and terminals started here run as you. Anything that takes over this dashboard, or any agent it starts, can run commands and read your files and keys."** Owner's plain-language risk: *"Anything that takes over the dashboard (a bug in the web page, a stolen login token, a malicious dependency), or any agent it starts, can run any command as you and read your files, SSH keys and orch keys. Today a takeover can already sign as you; after this it can also run code."* | As soon as the dashboard can start an agent, a takeover is code execution as the person, terminals or not. | **SC** |
| **D-P2-32** | **Worktrees.** One per (ticket, linked repo) at the host-chosen path `<repo path>.worktrees/<WS8>-<KEY>` (`WS8` = the first 8 characters of the workspace id), on the ticket's current `links.branches` branch, created and removed by the runner's git under a per-repo lock. The host then pins it and records a **registration generation**. Prepared requests, every `human.get` binding and the D58 precommit bind (workspace, ticket uid, repo, branch ref, source sha, worktree pin, generation); a new registration invalidates outstanding preparations, and nothing ever falls back to the declared working copy. HEAD is rechecked against the current branch; one ticket per branch (`worktree.branch_in_use`). A clean tree is an observation of that directory at that moment, not proof that tests ran on those bytes. Amends D-P2-19 "Declared paths" and H10a. | As built, D58 and `tree.check` read only the declared working copy; binding the registration closes "prepare against worktree A, swap the registration". | **SC** |
| **D-P2-33** | **Records** are the store's own files (never `.state/`), committed by a person-side one-shot tool `python -I -m orch.records`, never by the host or the runner, into a **dedicated records repo** only. The tool commits exactly the manifest the host names (`records.snapshot`): a temporary index built **from empty**, the written tree checked against the manifest before `commit-tree`; history validated once (or a fresh orphan history); the real index refreshed only by compare-and-swap; single-read hashing under a pinned root; byte limits. Push is **fast-forward only**: the lease guards the race, it never authorizes a non-fast-forward; no rebase. Git commits are unsigned transport: authority comes only from F1 replay and checkpoints. | Correctness (no `.state/` in history, no clobbered index, no torn line); no git in the host; push credentials are the person's. | **SC** |
| **D-P2-34** | **A records push adds a reader** (**owner decision, 12 Oct 2026: the private remote is allowed**; one exact remote URL marked "only I read this"; the docs and the UI state that orch trusts the owner's word). Every push path calls `records.export_check {manifest, sha, url, ref}` right before pushing; while the convenience flag is on or the history holds `auth:"convenience"` events it answers `sign.convenience_readers` unless that exact normalized URL is in this machine's `private_remotes` (set in the terminal only). **As built (#441):** "the history" is the merged history, **or** the durable `convenience_seen` mark (§0.8.6 item 1; never cleared by a restore), **or** the records tool's scan of the event logs reachable from the pushed commit (`history_convenience`, which refuses that push only and never writes the mark), reported before any network call; any error fails closed. The tool resolves the effective push URL once and uses that literal string for `ls-remote` and `push`. This is a policy orch enforces on its own export paths, not information-flow control (manual git can still push). Owner's plain-language risk: *"orch trusts your word; if someone else can read that remote, they get your convenience-signed history."* | A records remote is a copy of the workspace log that other cores may read; a per-workspace boolean would silently follow a changed remote. | **SC** |
| **D-P2-35** | **Quick tasks are tickets** (`type: chore`, label `quick`), created through the existing `new` paths; the addon is a view (`quick.list`) plus UI; limits are display only. If the label `set` fails after `new`, the retry labels the created ticket, never a second one. | T14: events are the only truth. | |
| **D-P2-36** | **Phase-2 first-party addons are manifest-only** (`addons/{start-agent,terminals,worktrees,quick,records}/orch-addon.json`). Manifests cannot introduce scripts, executable UI components, command templates or op messages; core dispatch enforces capabilities independently of UI visibility; addon frames never gain run actions. No addon runner code runs in tier A (D-P2-20 stands). | Tier A has no worker; core code is reviewed as core. | |
| **D-P2-37** | **Run lifecycle is host state, not F1** (no `agent.started`/`agent.stopped` events). Clients subscribe first (`run.sub`), then list (`run.list {since?}`); every `Run` has a host-monotonic `rev`; `{t:"gone", run}` when a run leaves the caller's view. The launch intent (run id, idem, spec) is persisted **before** the spawn command; the runner dedupes `spawn` by run id; a restart between intent and ack reconciles at `runner.hello` instead of spawning again. `Run.session` equals the `actor.session` of that agent's F1 events. Run state in 0700 directories, files 0600. | A run is a local process fact; list-then-subscribe loses exits; a lost ack must never double-launch. | |
| **D-P2-38** | **Direct mode:** POSIX direct mode ships `orch worktree` and `orch records` in phase 2 with the same safety contract (locking, manifest, export check, registration); they refuse while a host serves the workspace. On Windows both answer `unsupported.platform` until reparse-point-safe resolution exists. No runner, terminals or start-agent without a host. | Windows stays direct (D63) without building Windows filesystem code now. | |
| **D-P2-39** | **Restarts.** Host restart is a new authorization epoch: the runner ends every attachment (`host.restarting`), accepts nothing new until `runner.hello`, the host revalidates every reported grant and drops revoked or expired ones; spawns are idempotent by run id. Runner crash: at restart it marks runs `lost` with `cleanup:"unknown"`, sweeps recorded processes that still match their birth identity, and reports what it found (persisted table `<state>/runner/runs.json`). Controlled restart `orch runner restart --stop-runs` stops and verifies first, and refuses with live runs otherwise. Dashboard restart changes nothing in the runner. A reboot ends processes, not cron jobs, launch agents or other persistence an agent may have created as the person. | Honest claims, cheap given the persisted run table. | |

### 12.3 Ops (contract v1.6)

Every op follows §0.5 (filter first, rechecked per request) and the §0.6 precedence. New ops are at version 1 under
new caps (§0.4). "Idem create" is the idempotent-create category of §0.7 (an optional `opts.idem` returns the same
object on a retry). `dry_run` is refused by all of them (`invalid.input`). A request with `auth.grant` on a P-only op
answers `op.not_allowed`, never `human_only`. `grant.deposit` is the one op that requires both a person and a grant. **As amended (H11c review, owner decision of 15 Oct
2026):** the permission matrix admits it in column G only (a verified `auth.grant`; P without a grant, N, W, C and R
are `op.not_allowed`, an unverified grant `grant.expired`), and the op then requires a person-class peer, the UID's
valid mapping to a member, and that the grant is that person's own `label:"runner"` grant (an agent-class peer is
`op.not_allowed` there). This is equivalent to "person class evaluated as P plus a verified `auth.grant`".

#### 12.3.1 Caps and columns

| Cap | Op | P | G | N | Category |
|---|---|---|---|---|---|
| `run/1` | `run.sub`, `run.list` | ✓ own person's | ✓ agent runs in grant scope | — | read |
| `run/1` | `agent.preview` | ✓ | — | — | read (no side effects) |
| `run/1` | `agent.start` | ✓ | — | — | idem create |
| `run/1` | `run.stop` | ✓ own runs | — | — | side effect |
| `run/1` | `grant.deposit` | ✓ with `auth.grant` (matrix: G with a verified grant; the op: person class, valid member, own `label:"runner"` grant) | — | — | side effect, `deposit_id` |
| `run/1` | `grant.deposit_status`, `grant.forget` | ✓ own grants | — | — | read / side effect |
| `term/1` | `term.preview` | ✓ | — | — | read (no side effects) |
| `term/1` | `term.open` | ✓ | — | — | idem create |
| `term/1` | `term.attach`, `attach.cancel` | ✓ own runs | — | — | side effect (mints a capability) |
| `worktree/1` | `worktree.list` | ✓ | ✓ grant scope | — | read |
| `worktree/1` | `worktree.create` | ✓ visible ticket | ✓ own claimed ticket | — | idem create |
| `worktree/1` | `worktree.remove` | ✓ (`force` too) | ✓ own claimed ticket, no `force` | — | side effect |
| `records/1` | `records.status`, `records.snapshot`, `records.export_check`, `records.note` | ✓ owner, maintainer | — | — | read / side effect |
| `quick/1` | `quick.list` | ✓ | ✓ | ✓ (workspace tickets only) | read |
| `runner/1` (runner socket) | `runner.hello`, `runner.report`, `runner.exit` | — | — | — | runner class only |

Writes and resizes on a terminal are not host ops: they travel on the term socket (§12.6) under the capability.

#### 12.3.2 Schemas

```
Harness   = "claude-code"|"codex"                 TermHarness = Harness | "shell"
Mode      = "refine"|"work"|"fix"|"continue"|"context"
Model     = str/[A-Za-z0-9][A-Za-z0-9._:\[\]\/-]{0,63}/          (never starts with "-")
RunId     = str/r_[0-7][0-9A-HJKMNP-TV-Z]{25}/                     AttId = str/at_[0-7][0-9A-HJKMNP-TV-Z]{25}/
Gen       = str/[0-9a-f]{32}/                                      (a runner generation, drawn at runner start)
Cwd       = {kind:"worktree", repo:RepoName, ticket:Key} | {kind:"repo", repo:RepoName} | {kind:"workspace"}
// Run.kind "shell": everything run as the person (shells and person-run harnesses)
Run       = {run:RunId, rev:int, kind:"agent"|"shell", person:Person, ticket:Key | null, harness:TermHarness,
             mode:Mode | null, model:Model | null, subagent_model:Model | null, label:str{1..40} | null,
             session:str | null,             // = actor.session of the agent's F1 events; null for shells
             grant:str | null, grant_state:"ok"|"ended" | null, as_person:bool,    // true: no ORCH_GRANT (acts as you)
             cwd:Cwd, state:"starting"|"running"|"exited"|"lost", started_at:Time,
             exit:{code:int | null, signal:str | null, at:Time,
                   reason:"exited"|"stopped"|"spawn_failed"|"runner_restarted"|"lost",
                   cleanup:"complete"|"incomplete"|"unknown"} | null,
             viewers:int 0..4, writer:bool, cols:int, rows:int,
             replay_until:Time | null}       // until when attach can replay the ring; null: running, or no ring kept
RunnerInfo = {connected:bool, enabled:bool, terminals:bool, gen:Gen | null,
              grant:{id:str, until:Time, scope:"all"|"workable"} | null}     // the caller person's runner grant

run.sub        {} → {sub:SubId}        evt items: {id, t:"run", run:Run} | {id, t:"gone", run:RunId}
                                                | {id, t:"runner", grant:RunnerGrant | null}   // H11c-2: see below
run.list       {state?:"live"|"all" = "live", ticket?:Ref, kind?:"agent"|"shell", since?:int, page?:str{1..512}}
               → {runs:[Run & {out_bytes:int}]{0..256}, next_page:str | null, runner:RunnerInfo}
whoami         (v1.6, with run/1) gains runner?:RunnerInfo for P and G of the person (the card reads it before any
               run.list)
agent.preview  {ticket:Ref, harness:Harness, mode:Mode, model?:Model, subagent_model?:Model, label?:str{1..40},
                worktree?:bool = true}
               → {ticket:Key, spec:Hash, argv:[str]{1..16}, display:str, login_shell:bool, program:str | null,
                  cwd:Cwd, cwd_path:str, will_create_worktree:bool, env:[str], grant:{id, until, scope} | null,
                  blocked:{code:str, detail?:object, message:str} | null, warnings:[str]{0..8}}
agent.start    {…the preview's arguments…, spec:Hash, cols?:int 20..500 = 120, rows?:int 5..200 = 40} + opts.idem?
               → {run:Run}
term.preview   {harness?:TermHarness = "shell", ticket?:Ref, mode?:"context", model?:Model, cwd?:Cwd,
                label?:str{1..40}}
               → {spec:Hash, argv:[str]{1..16}, display:str, login_shell:bool, program:str | null, cwd:Cwd, cwd_path:str,
                  env:[str], as_person:true, notice:"acts as you, not as an agent",
                  blocked:{code:str, detail?:object, message:str} | null, warnings:[str]{0..8}}
term.open      {…the preview's arguments…, spec:Hash, cols?, rows?} + opts.idem? → {run:Run}
               // a person-run harness or shell: kind "shell", as_person:true, session:null, ORCH_TICKET only
               // (no ORCH_GRANT, no ORCH_SESSION); no claim precheck, no runner grant; `ticket` is checked for
               // visibility only; `mode:"context"` needs a ticket. Same spec digest rules as agent.start.
               // Without `mode` a harness starts with no prompt argument (`claude [--model M]`, `codex [--model M]`,
               // interactive). Without `cwd` and `ticket` the cwd is the workspace root; with a ticket, §12.4.4.
               // blocked.code: runner.disabled, runner.unavailable, harness.not_found, harness.shell_unsupported,
               // launch.invalid, launch.cwd_changed, not_found (ticket), worktree.* (a named worktree cwd).
run.stop       {run:RunId, grace_ms?:int 0..30000 = 10000}
               → {state:"exited"|"stopping", cleanup:"complete"|"incomplete"|"unknown"|null}
grant.deposit  {deposit_id:ULID} + auth.grant → {deposit:"confirmed"|"rejected"|"unknown", grant:str, until:Time,
                                                 scope:str, reason?:str}
grant.deposit_status {deposit_id:ULID} → {deposit:"confirmed"|"rejected"|"unknown"|"none", reason?:str}   // reason: only with rejected (H11c-2)
grant.forget   {grant:str} → {forgotten:bool}
term.attach    {run:RunId, mode:"read"|"write" = "read", take?:bool = false, from?:int 0..}
               → {att:AttId, ticket:str/[A-Za-z0-9_-]{43}/, expires_at:Time, socket:str, runner:{uid:int, pid:int, gen:Gen},
                  start:int, offset:int, cols:int, rows:int, lease_gen:int}
attach.cancel  {att:AttId} → {}
```

- **Runs and revisions.** `rev` is host-monotonic per workspace and persisted with the run records, so it survives
  host restarts. `run.list` returns runs newest `rev` first, paged (`next_page`); `since` returns runs with
  `rev > since` and is only for the gap between `run.sub` and `run.list` (it cannot report gone runs). After any
  reconnect or `end` the client replaces its state with a full `run.list`. `{t:"gone"}` is sent when a run leaves the
  caller's view, also when it is evicted by retention. The host coalesces `run` items that only change `viewers`,
  `writer`, `cols` or `rows` to at most one per run per 250 ms. `run.sub` ends as `sub/1` subscriptions do (§0.9:
  `person.removed`, `grant.expired`, `overflow`, `host.restarting`).
- **The `runner` item (H11c-2).** `{id:"<rev>.g<n>", t:"runner", grant:RunnerGrant | null}` with `RunnerGrant = {id, until,
  scope}` (the `whoami.runner.grant` shape; never the secret) is sent to a subscriber whenever **its own person's**
  runner grant state changes: a deposit confirmed (also a pending one settled at `runner.hello`), a grant replaced,
  forgotten, revoked, expired, dropped by the switch or the watcher, cleared because a restarted runner no longer holds
  it, and the runner connecting or disconnecting (`grant:null` while no runner is connected). Another person's change
  sends nothing. The item is checked like a `run` item (fresh authentication snapshot, principal still valid); a G
  subscriber gets its grant's person's. It is not replayed: after a reconnect read `whoami.runner.grant`.
- `kind:"agent"` runs appear on the Agents page; the dock lists both kinds. `label` is client-chosen untrusted text
  (rendered through `visible.tsx`; null shows the harness and ticket).
- `runner.disabled` with several things missing reports the first of `addon`, `machine`, `terminals`. Whether the
  addon is granted is read from `addon.list`, not from `RunnerInfo`.
- `display` is the POSIX-quoted rendering of `argv`, for display only. `cwd_path` is a host path on the person's own
  machine, shown to that person only.
- The `spec` digest covers harness, mode, model, subagent model, argv, the cwd pin and its registration generation, the
  environment names and the grant id. `will_create_worktree:true` means `agent.start` creates it (under the per-repo
  lock); `agent.preview` never creates anything.
- `subagent_model` is `claude-code` only, sent as `CLAUDE_CODE_SUBAGENT_MODEL`; `launch.invalid` names the field.
- `term.attach` answers `start` (oldest offset in the ring), `offset` (the current end), the size and the lease
  generation at minting; for `take:true` it is informational only (the take ignores it, §12.6.2). The capability itself is §12.6.2.

#### 12.3.3 The runner protocol (runner socket, cap `runner/1`)

The runner is a client of the host. After `welcome` (`peer.class:"runner"`, `workspace:null`) and its own check of the
host's peer UID:

```
runner.hello  {version:str, gen:Gen, pid:int, term_socket:str,
               runs:[{run:RunId, workspace_id:WsId, pid:int, birth:int, pgid:int, sid:int, cgroup:str | null,
                      started_at:Time, state:"running"|"exited"|"lost", out:int, exit?:object}]{0..256},
               grants:[{workspace_id:WsId, person:Person, grant:str, until:Time}]{0..64}}
              → {sub:SubId}                          // the host's command stream to this runner
runner.report {cmd:ULID, outcome:"ok"|"error", result?:object, error?:{code:str{1..64}, message:str{1..400}}} → {}
runner.exit   {run:RunId, code:int | null, signal:str | null, at:Time, cleanup:str} → {}
```

Host→runner command items (`evt`): `spawn {run, workspace_id, kind, argv, env_names, env_fixed, grant | null,
session | null, cwd:Pin, cols, rows, login_shell}` (deduped by `run`), `signal {run, sig, sweep}`, `attach.allow`
(§12.6.2), `attach.end {att, reason}`, `grant.put {deposit_id, workspace_id, person, grant, secret, until}`,
`grant.drop {grant}`, `git {op:"worktree.add"|"worktree.remove", repo_pin, path, branch, base, force, generation}`.

- `env_fixed` holds only host-chosen values (`ORCH_TICKET`, `ORCH_WORKTREE`, `ORCH_WORKTREES`, `ORCH_HARNESS`,
  `CLAUDE_CODE_SUBAGENT_MODEL`); `grant` is an id the runner resolves in its own store. The host never sends a secret
  except once in `grant.put`.
- The runner validates every command as defence in depth (registry program or `$SHELL`, pin, sizes). `runner.hello`
  is evidence only about runs the host already knows; an unknown run in it is reported in the audit and never adopted.
- One runner connection per runner UID, accepted only from the runner unit's main PID (`launchctl print
  gui/<uid>/io.orch.runner`, systemd `MainPID`); a second one is `peer.denied`. The host records `gen` and `pid`; they
  bind the term socket (§12.6.1).

**As built in H11a** (#428, #432). Where the code differs from the text above, the code is this:

- **Commands and reports.** A command item is `{id:"<n>.c", cmd:ULID, op, args}`: `op` names the command, `cmd` is
  its id, and `runner.report {cmd}` names that id. The stop command is **`stop {run, grace_ms}`**, not `signal {run,
  sig, sweep}`: the runner runs the whole §12.5 sequence itself (it holds the birth identities) and answers `{state,
  cleanup}`; `state` stays `running` when even `SIGKILL` did not end the leader (then `cleanup:"incomplete"`, the run
  stays supervised and is killed again on every scan; it is never reported ended or dropped).
- **`runner.exit`** is sent once the clean-up has a result, and again when an `incomplete` clean-up completes; the host
  sets `exit.reason` (`stopped` after a `run.stop`, else `exited`).
- **`runner.hello`** answers `{sub, epoch}`; `gen` is drawn at every runner start (not persisted). The runner tags every
  command with the connection it arrived on and drops, unrun and unreported, any command of a lost connection (a
  stale `spawn` never starts after the reconnect's hello; the check and the spawn are one step against the hello's
  snapshot). Commands that arrive before the hello's answer are held until it. The hello lists every running run
  first, then the newest ended ones, 256 in all. A runner of another `gen` can report an older generation's run only
  as ended (`runner_restarted`), never `running`. A successful command result is validated before it is stored.
- **`run.sub`** answers `{sub, rev}`: it is registered first, then the visible runs are taken, so no change falls
  between them. `rev` is one counter for the whole host (persisted, so monotonic per workspace too). Every delivery
  revalidates the principal on a fresh authentication snapshot. Evicted records (7 days, 1 000 ended) publish `gone`. `grants` is
  revalidated since H11c (§12.4 "As built in H11c"). A spawn that is half-done while the hello is built cannot happen (the runner holds its
  spawn lock); a spawn that failed after the fork is in the table with its pid and birth time.
- **Trampoline.** The runner starts `python -I <release>/orch/runner/exec.py SPEC_FD STATUS_FD`, not `-m`: under `-I`
  a release's `orch` is not importable, and `-I` implies `-P`, so the file imports only the standard library. The status
  pipe is close-on-exec: EOF means `execve` happened; otherwise it names the failed step.
- **Machine switch.** `orch runner enable` records the switch in `<state>/host/dashboard-execution.json` (0600), with
  the time and the exact text shown; a corrupt file reads as off.
- **macOS `cleanup:"complete"`** means: nothing of the run's group or session and no tracked descendant is left, and no
  descendant was ever seen outside the session. The escape scan runs every 2 s and right before a stop; a descendant
  that calls `setsid()` and whose parent exits between two scans cannot be seen. That is the stated best effort.
- **Signals per member, never to a group.** The stop sequence signals each member by `(pid, birth)` (Linux through a
  pidfd), never `kill(-pgid)`. Members are found over every UID; only the runner's UID is signalled, and a member of
  another UID (`sudo`, a setuid helper) makes the clean-up `incomplete`. A session or group is matched only for a run
  live in this runner's lifetime; an ended or crash-swept record matches exact identities only (the leader, the
  escapees and the member set saved at each scan), and only on the boot it was recorded on (`boot_id`,
  `kern.boottime`). With a cgroup, `complete` means nothing is left in the run's cgroup and nothing was seen leaving it.
- **umask.** The units set none; runs start with the person's umask, captured by `orch runner enable` in
  `<state>/runner/settings.json` (default 022). The runner's own files are written 0600 in 0700 directories.
- **Logs.** The service manager writes the runner's output to `<state>/runner/runner.out` without rotation (the
  rotated `runner.log` of §12.5 comes with H11b). One runner per state directory (a lock file); `SIGTERM` is a
  controlled stop: every managed run is stopped and verified before the runner exits.
- **Dev-only foreground runner** (dev and tests; terminal follow-ups after #469). Without a service manager the
  main-PID check can never pass, so a host and a runner run as separate foreground processes need an explicit seam:
  `orch host run --foreground --dev-runner` (`Host(dev_runner=True)`) also admits a runner that
  `orch runner run --foreground` announced in `<run>/term/<uid>/dev-runner.json` (`orch.runner.devmode`: 0600 in the
  runner's 0700 terminal directory, `{v:1, pid, birth, uid}`, removed at exit). Admitted only when the kernel peer UID
  is the host UID, the file is a private regular file of that UID (no link) in a private directory, it names that
  UID and the peer PID, and that PID is still the process born at the recorded time (a stale file whose PID was reused
  is refused); every error denies. The main-PID check still applies first. Refused at start (`invalid.input`): without
  `--foreground` (the units never pass it), under the service manager (`XPC_SERVICE_NAME` is `io.orch.host` or
  `io.orch.runner`), and for a service run directory (`/Users/Shared/orch/run`, `/run/orch`; decided by identity:
  as written and resolved, case-folded on macOS, and by `(st_dev, st_ino)` of every existing ancestor against each
  existing root; also in `Host` itself). The file is opened `O_NONBLOCK | O_NOFOLLOW` and must be a regular file
  (`custody.read_json`), so a FIFO in its place never blocks the accept loop. Any other run directory (`<state>/run`, `--run`, `ORCH_HOST_RUN_DIR`) is a dev one:
  a short `ORCH_HOST_RUN_DIR` is what tests use for the socket-path limit. The host logs a warning (and the CLI prints
  one) at start. Tier A: like the main-PID check this is fault separation, not a boundary (any process of the host
  UID can write the file); it keeps a stale or misdirected process from being taken for the runner.
- **Term socket.** The runner caps every capability at 10 s after `attach.allow` arrived, whatever `expires_at` says. The
  ticket must be exactly 43 characters; at most 96 term-socket connections are handled at once; the lockout is per
  runner, so one client replaying seen tickets locks every attachment out for 60 s.
  The byte path after `ready` is H11b (below).

**As built in H11b** (terminals). Where the code differs from §12.3 and §12.6 above, the code is this:

- **Authorization is checked at use, not only at consumption** (`orch.runner.term.TermListener._authorized`, the one
  function every privileged action calls: each non-blocking `send` of an output frame, each input write and each retry
  of a partial write, each resize; the take is checked under the same lock as its registration). An attachment must
  still be in the runner's table, not revoked, not closed, in the current host epoch with the host present; for a write
  action it must also hold the lease at the generation the frame names. The output `send`, the PTY write and the resize
  run non-blocking *inside* the check, under the lock that `attach.end`, `end_run`, `host_lost`, a new epoch and an
  overflow take to revoke, so no output byte leaves and no input byte or resize is applied after a revocation. An output
  frame that a revocation cuts short is never completed (the socket is shut without another byte). That client gets
  no `end` reason: the frame's length prefix already commits to the revoked bytes, so nothing can follow it;
  `TermClient` reports such a stream as ended `closed` (the page shows "ended"). There is no
  in-place downgrade: a lease moves only by a take or by the holder's close. A revoked attachment is ended by whoever
  notices first (`end` with the reason, queued output discarded); a lease that only moved gives `writer:false` and
  keeps the viewer. An `attach.cancel`/`attach.end` that
  arrives between the consumption of a ticket and the registration of the attachment is remembered and the attachment
  never registers (`cancelled`). A write capability for a run that is no longer running is `ticket_invalid` at redeem.
  The capability stays a bearer secret between the host and the dashboard server (single use, 10 s, bound to run, mode,
  epoch and generation); it is not bound to the redeeming principal, which the runner cannot see.
- **The host ends attachments by itself** (`RunnerHub._watch`, every 0.5 s while any exists): a D-P2-31 kill switch
  was pulled (the `terminals` addon revoked or disabled, `orch runner disable`, `orch runner terminals off`:
  `runner.disabled`), the person left or lost the mapping (`person.removed`), the grant ended, or the run's ticket
  became invisible (`not_visible`); the run goes on. The watcher never dies on an item: any exception is logged (its
  type only) and counts as undecided. Each attachment's check runs as its own task, bounded at 2 s (a timeout counts
  as undecided), so a slow or hung workspace worker never delays the checks of another workspace. An attachment whose
  last decided check is older than 2.5 s (no fresh authentication snapshot, a failing read, a check still waiting on
  its worker) is ended with `host.restarting` (fail closed). Every `term.attach` and every `runner.term`
  restarts a watcher that stopped. Attachments are recorded before `attach.allow` is sent, so an end between mint and
  redeem is caught.
- **Invariant: the runner holds only attachments the host's table holds.** The host removes its entry only after the
  runner confirmed `attach.end` (until then the entry is `ending` and the end is sent again on every pass). An
  `attach.end` the runner does not confirm within 5 s drops the runner session, so the runner's `host_lost` ends
  every attachment; the runner handles `attach.end` inline on its link thread, never behind `stop` or `spawn` in its
  four-thread command pool. An attachment the runner reports in `runner.term` that the host does not hold is ended there
  (`cancelled`). `runner.term` carries `seq`, taken under the runner's lock together with the snapshot; the host
  ignores a report whose `seq` is not newer than the last one it applied for that run, so a stale snapshot that
  arrives late never takes a live attachment out of the host's table.
- **Ops.** `term.*` and `attach.cancel` are in `term/1`, `run.stop` in `run/1`: all P only (G and N: `op.not_allowed`
  before any run is looked at). `term.preview`/`term.open` start the person's own `$SHELL -l` (harness `shell` only;
  `claude-code` and `codex` are `blocked: harness.not_found` until H11c; a ticket is checked for visibility and becomes
  `ORCH_TICKET`; cwd is the workspace root until H11d). The runner replaces `argv[0] == "$SHELL"` by its own `SHELL`
  (absolute path to a POSIX shell, else `harness.shell_unsupported`). The run id is derived from `opts.idem`
  (`r_` + the idem's ULID), so a retry returns the same run. Order of `blocked`/`runner.disabled`: addon, machine,
  terminals, then the runner connection. `term.attach` answers `nonce` as well (the dashboard server presents it with
  the ticket); `runner.disabled` carries what is missing in its message (no `detail` field in `Error`).
  Another person's run, or a run on a ticket the person cannot see, is `not_found`. The machine switch is read again on
  the event loop at `term.open` and `term.attach`; `run.stop` and `attach.cancel` never need it.
- **Limits.** The host refuses `attach.limit` from the runner's `runner.term` counts (pending capabilities count
  toward none) and `run.limit` (16 shells per person, 64 runs per runner), `rate.limited` (10 starts per minute,
  burst 5; 30 `term.attach` mints per minute per person, burst 10). Each of these refusals carries
  `retry_after_ms` as the `Error` member of §0.2 (there is no `detail` field; terminal follow-ups after #469):
  `rate.limited` the time until the person's next token in that bucket (at most 6 s for starts, 2 s for mints),
  `attach.limit` 2 000, `run.limit` 5 000. The runner's own `attach.limit` (256 pending capabilities) reaches the
  caller as `attach.limit` with `retry_after_ms` 2 000, not `run.spawn_failed`. The runner's connection cap is 96 (64 attachments
  plus handshakes); at most 256 capabilities wait to be presented (`attach.allow` beyond: `attach.limit`). Per
  attachment the runner queues at most 4 096 control frames and reads ahead at most 64 frames while a PTY write waits
  for room; beyond either the attachment is revoked, its write lease is released at once, and it ends with
  `overflow`. Outside a stalled write there is no frame-count cap but a bound by backpressure: the runner reads at most
  64 KiB at a time and reads again only once every frame of the last read was handled, so a coalesced burst of small
  frames (resizes during a window drag) never ends a healthy attachment; of a run of consecutive `resize` frames only
  the last is applied (one PTY resize, one note per viewer, one `runner.term`). A length prefix above 128 KiB (longer
  than any valid frame) ends the attachment with `protocol` before the body is buffered.
- **Term socket I/O.** The socket is non-blocking once the attachment exists, with separate deadlines: the sender
  gives up after 30 s without write progress, the reader has no deadline (an idle viewer stays attached). While a PTY
  write waits for room the reader keeps watching the socket, so a disconnect cancels the pending write and releases the
  lease at once. `ready` is queued under the registration lock before the attachment is visible, so it is always the
  first frame after `hello`, whatever takeover or resize races the attach.
- **Input ids are consecutive.** The runner writes only `last + 1`; a lower or equal id is acknowledged again and never
  written; a higher id (a gap) is answered with `throttle` and never acknowledged, so a throttled frame can never be
  skipped by a later one. The dashboard server keeps one input frame in flight (the next after the previous `ack`)
  and resends the same frame after a `throttle`.
- **`Run`.** `viewers`, `writer`, `cols` and `rows` come from `runner.term` (items coalesced to one per run per 250 ms);
  `replay_until` is the end time plus one hour for `exited`/`stopped` runs. Attach to an exited run is read-only.
- **Client.** `orch.runner.termclient.TermClient` does the §12.6.1 checks before a ticket is sent (path computed from
  the client's own run directory, directory owner and mode, peer UID and PID, `hello.gen`), owns `input_id` and
  `lease_gen`, keeps one input frame in flight, resends on `throttle` with the same id and reports unacknowledged ids
  as `lost`. A runner frame of the wrong shape ends the stream with `protocol`; a stream that closes without `end`
  ends with `closed`; `send`/`resize` after the end raise `TermEnded`. `send` writes non-blocking under its own
  deadline (`send_timeout`, default 5 s per frame), independent of `recv`'s timeout, so the next input frame that an
  `ack` puts on the wire from inside `recv` is never cut by a short read timeout; a runner that takes no input for that
  long ends the stream with `send_timeout` (socket closed; the unacknowledged ids are `lost`). The dashboard server
  may treat `send_timeout` like `runner.lost` (4503, retry with backoff). Terminal follow-ups after #469.
- **Terminals addon.** `addons/terminals/orch-addon.json` (capability `pty`, no fields); the host requires it granted
  and enabled. `orch runner terminals on|off --workspace W`: `on` needs the person's own terminal and the prefix (never
  under `ORCH_GRANT`), `off` needs none; both only with dashboard execution already on.
- **Audit** (`audit-security.jsonl`): `term.attach`, `term.end`, `term.detach`, `term.takeover`, `run.stop`; never
  terminal content.
- **Not built here.** The flood acceptance measures `run.list` round trips (a workspace-worker read) instead of
  `human.prepare`, with the runner as a separate process: p99 under 50 ms with 8 flooding runs and two silent
  viewers (120 ms when pytest runs in parallel). The browser-facing parts (WebSocket `Security`, xterm, close codes)
  are orch-fa's.

### 12.4 Start an agent (#307)

1. `agent.preview`: the host checks the column, the ticket's visibility, the addon and the machine switch
   (`runner.disabled` with `detail.missing`), the runner connection, the claim precheck (`launch.claimed`), harness,
   mode, model and shell, the cwd, and the runner grant; it returns the spec digest and every value it will use.
2. Without a usable runner grant the dashboard runs the grant flow (§12.4.2) and previews again.
3. `agent.start {…, spec}` with an idem: the host recomputes the spec (`launch.changed` on a difference), persists the
   intent, creates the worktree when needed, sends `spawn`, and answers with the run. The agent claims the ticket
   itself (`orch status`, then `orch claim KEY`).

#### 12.4.1 The harness registry

| Id (`ORCH_HARNESS`) | Program | Argv | Prompt (one element) |
|---|---|---|---|
| `claude-code` | `claude` | `claude [--model M] <prompt>` | `/orch:<mode> KEY` (the plugin's skills; until they exist, the plain text of the codex row) |
| `codex` | `codex` | `codex [--model M] <prompt>` | `Run orch status, then <mode text> ticket KEY. Follow AGENTS.orch.md.` |
| `shell` | `$SHELL` | `$SHELL -l` | none |

Mode `context`: `Run orch show KEY --section current_state, then wait for my instructions.` (for `agent.start` and
`term.open`; the dashboard's `withContext` `$(…)` form is gone). `KEY` comes from the host's lookup. A new harness is a
reviewed code change, never configuration. The dashboard's `harnesses.ts` keeps labels and views only.

#### 12.4.2 The runner grant flow (D-P2-27)

- **Click** (dashboard execution enabled for the workspace on this machine): `prepare {workspace_id, op:"grant",
  args:{hours:1..12 (default 8), scope}, idem, deposit:true}` on the click signer. `check_prepared` has one exception: a
  `grant.issued` is click-eligible only with `deposit:true` and `label:"runner"`. The View's confirm kind is `word`,
  word `<PREFIX>~<WS8>/GRANT`; the View carries the person, scope, hours, until, the tier-A sentence and the line
  **"The secret goes to your orch runner on this machine and is never shown"** (the dashboard does not arm Sign without
  that line when `deposit:true`). `sign` draws the secret, signs, submits, and only after the commit calls
  `grant.deposit {deposit_id}` with `auth.grant` on its own host connection, then wipes the secret. The result is
  `{result, duplicate, deposit, deposit_id}` and **never holds the secret**. On `unknown` the signer asks
  `grant.deposit_status` once; if still unknown the panel says "The grant may or may not be with your runner. Revoke it
  to be sure." (click `grant.revoke`).
- **Terminal:** `orch grant --runner [--hours N]` (label `runner`, deposits instead of printing); `orch sign CODE` for a
  grant request asks `[r] hand to the orch runner  [s] show ORCH_GRANT here`. The dashboard's fallback when click is
  off: `human.prepare {op:"grant", handoff:true}` and "Sign in your terminal: `orch sign XXXX-XXXX`".
- `grant.deposit` refuses: a grant without `label:"runner"`, `verbs` other than `"agent"`, under 15 minutes left, a
  forgotten or replaced id (`grant.forgotten`), the runner not connected or not enabled. The host forwards the secret
  once (`grant.put`), keeps no copy and redacts it (D-P2-10). A newer runner grant replaces the older one for new
  launches; running agents keep theirs.
- On `grant.revoked`, member removal or expiry the host sends `grant.drop`. Running agents keep the variable, the host
  refuses their writes (`grant.expired`), their claims lapse (`grant_ended`); the runner does not kill them;
  `grant_state:"ended"` and the dashboard offers Stop.
- `launch.no_grant` with `detail.reason` `none`, `expiring`, `scope` or `revoked`.

#### 12.4.3 Environment

| Variable | Agent run | Shell or person-run harness | Source |
|---|---|---|---|
| `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, `LC_ALL`, `LC_CTYPE`, `PATH`, `SSH_AUTH_SOCK` | ✓ | ✓ | the runner's own environment (the service manager's); the login shell then applies the person's profile |
| `TERM=xterm-256color`, `COLORTERM=truecolor` | ✓ | ✓ | fixed |
| `ORCH_STATE_DIR`, `XDG_CONFIG_HOME`, `ORCH_HOST_RUN_DIR` | if set | if set | the runner unit's (written by `orch runner enable` from the host unit's values) |
| `ORCH_SESSION` | `s_<ULID>` drawn by the runner | never | runner |
| `ORCH_GRANT` | the runner grant | **never** | the runner's grant store |
| `ORCH_HARNESS`, `ORCH_TICKET`, `ORCH_WORKTREE`, `ORCH_WORKTREES`, `CLAUDE_CODE_SUBAGENT_MODEL` | as given | `ORCH_TICKET` only | host (`env_fixed`) |
| `pass_env` names from `<person state>/runner.json` | ✓ | ✓ | terminal-only config; values from the runner's environment |

- `pass_env` refuses names matching `LD_*`, `DYLD_*`, `PYTHON*`, `GIT_*`, `ORCH_*`, `BASH_ENV`, `ENV`, `ZDOTDIR`,
  `PATH`, `HOME`, `SHELL`, `IFS`, `PS4`, `NODE_OPTIONS`, `PERL5OPT`, `RUBYOPT`. Nothing else passes.
- Values never reach the host, the dashboard or a log; previews and `run.list` show names only.
- Every descendant of an agent inherits `ORCH_GRANT` (F1 A3); tier B confines it to the agent UID.

#### 12.4.4 Working directory

In order: the ticket's registered worktree for its primary repo (created by `agent.start` when `worktree:true` and the
ticket has a branch for it), the declared working copy (labelled "shared working copy"), the workspace root. Sent as a
pin `{path, dev, ino}`; the trampoline opens it component-wise with `O_NOFOLLOW`, compares, and `fchdir`s
(`launch.cwd_changed` on a mismatch). `ORCH_WORKTREES` lists the other repos' worktrees as `repo=path` pairs separated
by `:` (a path containing `:` is left out and named in `warnings`).

#### As built in H11c (start agent, runner grant)

Where the code differs from or adds to §12.3, §12.4 and §0.8.6 item 6, the code is this:

- **Modules.** `orch.host.launch` (the closed registry and argv, pure), `orch.host.agents` (`agent.preview`,
  `agent.start` and the launch plan `term.*` shares), `orch.host.runnergrants` (deposit, status, forget, the hello
  revalidation and the watcher), `orch.runner.config` (`runner.json`), `orch.runner.grants` (the runner's in-memory
  store and its log redaction filter).
- **Prompts.** The `/orch:<mode>` skills do not exist, so `claude-code` gets the codex text too: `Run orch status, then
  <mode text> ticket KEY. Follow AGENTS.orch.md.` with mode texts `refine`, `work on`, `fix the requested changes
  on`, `continue`; `context` as §12.4.1. The prompt is one argv element built from the host's key lookup only.
- **The login shell** runs only the fixed script `exec "$0" "$@"`. The runner accepts a `spawn` argv only in a registry
  form: `["$SHELL","-l"]`, `["$SHELL","-l","-c",'exec "$0" "$@"',"claude"|"codex",…]` (only while `runner.json`
  leaves `login_shell` on) or an absolute program `runner.json` names in `programs`; anything else is
  `invalid.input`. `$SHELL` is the runner's own and must be an executable POSIX shell (`sh`, `bash`, `zsh`, `dash`,
  `ksh`, `mksh`, `ash`), else `harness.shell_unsupported` at spawn. The preview checks the host's own `SHELL` as a
  hint only (when it is set); the runner is authoritative. With `login_shell:false` the preview checks that the
  program is executable (`harness.not_found`).
- **`runner.json`** (`<state>/runner.json`, 0600 of the person, terminal-only): `{v:1, login_shell, programs,
  pass_env}`, every key optional; only a missing file means defaults (`null` or anything but an object is invalid). A `pass_env` name on the deny-list (§12.4.3, plus `BASH_FUNC_*`, `TERM`,
  `COLORTERM`, `CLAUDE_CODE_SUBAGENT_MODEL`), an unknown key or a bad path makes the **whole file invalid**: every
  launch is `launch.invalid` until it is fixed (never a fallback to defaults). The host reads it for the plan, the
  runner again at every spawn.
- **Run ids and sessions.** `agent.start` needs `opts.idem`; the run id is `r_` + the idem's ULID, so a retry with the
  same idem answers the recorded run without recomputing or starting anything (another person's, workspace's,
  kind's or spec's run under that id is `idem.conflict`). The identity check, the limits, the start-rate charge and
  the reservation of the run id are one step on the event loop, so two concurrent requests with one idem record one
  session and charge one start. The **host** draws the agent's `s_<ULID>` session and sends it in
  `spawn` (§12.3.3 lists it; §12.4.3's "drawn by the runner" is read as "set by the runner"), so `Run.session` is
  known at the intent.
- **Intent first.** `agent.start` persists the intent (state `starting`, audit `run.intent` with idem and spec), then
  creates the ticket's worktree when the plan says so (H11d's create under the per-repo lock; a failure ends the run
  `spawn_failed` with the code), then sends `spawn` (`run.dispatch`, `run.outcome`). A host restart in between
  reconciles at `runner.hello` (`lost`, never spawned).
- **Working directory.** The primary repo is the first of `links.repos`. A worktree is created only when `worktree`
  is true, the ticket links a branch for it **and the `worktrees` addon is granted**; without the addon the plan uses
  the shared working copy and says so in `warnings`. A ticket that links a repo while git cannot be read is
  `observe.unavailable` (no silent root). `term.*` with a ticket uses its registered worktree when there is one and
  never creates one; `cwd:{kind:"worktree"}` needs a registration (`worktree.none`), `{kind:"repo"}` a declared repo.
- **The digest** (64 hex digits, `v:2`) covers kind, harness, mode, model, subagent model, argv, `login_shell`, the
  cwd and its pin (or, for a worktree to be created, its path, generation, branch and base), the environment names and
  fixed values and the grant id. `label` is not in it.
- **Order of `blocked`** (agent): visibility, `runner.disabled` (`detail.missing` `addon` for `start-agent`, then
  `machine`; the terminals switch is not asked), `runner.unavailable`, `launch.claimed`, `launch.invalid`
  (`runner.json`, model, subagent model), harness and shell, cwd, `launch.no_grant` (`detail.reason`: `none`, no
  current runner grant or no runner; `expiring`, under 15 minutes left; `scope`, a `member`'s grant that says `all`;
  `revoked`, revoked, expired or its person may no longer run agents). `agent.start` refuses `launch.changed` also
  when the runner grant changed since the preview. Limits: 8 agent runs per person per workspace, 16 in all, 64 per
  runner, 10 starts a minute (§0.11).
- **`grant.deposit`** refuses with `op.not_allowed` a caller that is not person class, a UID without a valid mapping
  or mapped to another person than the grant's, and a grant longer than 12 hours; with `grant.not_runner` a grant
  without `label:"runner"`, `grant.verbs` one with `verbs` other than `"agent"`, `grant.expiring` one with under 15
  minutes left; `grant.forgotten`; `runner.disabled` (machine switch); `runner.unavailable`; `rate.limited` (10 a
  minute per person, burst 5). Deposits, forgets and the watcher's drops of one (workspace, person) run one at a time
  (a lock); a deposit whose grant was forgotten or replaced while its `grant.put` was in flight is `rejected`
  (`grant.forgotten`) and dropped at the runner. **Every grant the host drops is forgotten for good** (owner decision
  of 15 Oct 2026): replaced, forgotten, revoked or expired, its member removed, dashboard execution switched off, or
  cleared because the runner no longer holds it (a runner restart); re-enabling never brings an old grant back, a
  new one has to be issued. At every `runner.hello` the persisted table of every served workspace is settled, loaded
  or not. Pending deposit records are never pruned. The outcome per `deposit_id` is kept in `<state>/hosts/<ws>/runner-grants.json` (0600; never a
  secret): `pending` is written before `grant.put` is sent and reads as `unknown`; a runner refusal is `rejected`
  with its code; the next `runner.hello` settles `pending` (reported: `confirmed`; not reported: `rejected`,
  `runner_restarted`; a pending deposit of the grant that is already current settles `confirmed` too). The same
  `deposit_id` again answers the recorded outcome. Dashboard execution switched off for the workspace drops its runner
  grants at the runner (fail closed; §12.4.2 names revocation, removal and expiry).
- **The click signer** derives `deposit_id` from the request (`hr_<ULID>` → the ULID), so it is stable across
  restarts without a new record field; the secret lives only in the `sign` call's frame (never in the approval, the
  durable record, the log or a result). The signer sets `label:"runner"`, `verbs:"agent"` and `runner:true` itself
  (a dashboard `label` or `verbs` is `invalid.input`), so the op is a runner grant with no `ORCH_GRANT` hint; more than 12 hours or no dashboard execution is `terminal_only`. The View's
  `lines` end with the tier-A sentence ("Anything that takes over the dashboard can give agents standing authority to
  act for you on tickets for up to 12 hours.") and the deposit line. A repeated `sign` answers the settled outcome or
  asks `grant.deposit_status` once (after a restart the secret is gone: `unknown` unless the host says `confirmed` or
  `rejected`). Any failure of the hand-over after the commit (a host error, a broken or garbled connection) is
  `unknown`, never a failed `sign`.
- **Terminal.** `orch grant --runner` (host mode only; direct mode is `invalid.input`) prepares a grant with
  `runner:true` (label `runner`, at most 12 h, verbs `agent`) and hands the secret over after the commit; `orch sign
  CODE` asks `[r] hand to the orch runner  [s] show ORCH_GRANT here` on `/dev/tty` only for a grant whose
  `grant.issued` says `label:"runner"` (any other grant is shown as before; no terminal to ask on shows it).
- **Runner side.** One grant per (workspace, person) in memory (at most 64), replaced by a newer `grant.put`; an
  expired one is never handed out and is dropped from `runner.hello`'s `grants`. A shell `spawn` with a grant or a
  session is `invalid.input`; an agent's needs its person's current grant (`launch.no_grant`). The runner's log
  handlers carry a filter that redacts every held secret and any grant-shaped string.
- **Not built here.** The e2e `start_agent.spec` (dashboard, orch-fa). The H11a/H11b mechanics tests now spawn
  `/bin/sh` through a test `runner.json` that names it as a program.

### 12.5 Supervision in the runner (D-P2-29)

- **Trampoline:** `os.openpty()`; the child gets the slave as fd 0–2 and a spec pipe as fd 3, closes every other fd
  (and the spec pipe before `execve`), `setsid()`, `TIOCSCTTY`, `RLIMIT_CORE` 0, `RLIMIT_NOFILE` soft raised to
  min(hard, 8192), the person's `umask`, Linux cgroup entry before `exec`, `fchdir(pin)`, `execve`. The runner keeps
  only the master (non-blocking, `O_CLOEXEC`).
- **Run end:** the leader exits, the runner drains the PTY for at most 2 s and closes the master (never waits for EOF);
  the rest of the run gets the stop sequence.
- **Stop:** `SIGHUP` and `SIGTERM` to the group, `grace_ms` (default 10 s), `SIGKILL`; Linux `cgroup.kill` until
  `populated=0`; macOS group and session sweep (stop-check-kill against birth identity, as H10). The result names
  `cleanup`.
- **Worktree git** runs as a supervised child in its own process group with a 30 s timeout that kills the group.
- **Audit (D-P2-37).** The host's `audit-security.jsonl` gets one line each for: the launch **intent** (run id, idem,
  spec digest), the **dispatch** of `spawn` to the runner, the **outcome** (pid or the spawn error), **reconciliation**
  at `runner.hello` (adopted, lost, unknown runs reported), every **stop** and its cleanup result, **attach** and
  detach with mode, every **lease takeover** (old and new attachment), **enablement changes** (machine switch on or
  off, `--no-terminals`, with time and the text shown, written by `orch runner enable`), **grant deposit, replace and
  forget** (grant id, person, `deposit_id`, outcome; never the secret), worktree **create and remove** (ticket, repo,
  generation, force), and every **records export** (manifest, commit, and the destination identity: the exact push URL
  and ref, and whether a private mark allowed it). Fields as D-P2-13; **never terminal content**, never environment
  values.
- **Logs:** `<state>/runner/runner.log` (0600, 10 MiB × 5): spawn (run id, `argv[0]`, pid, pgid, sid, env names),
  exit, signals, sweeps, git commands, failed git stderr capped at 4 KiB. The runner redacts every grant secret it
  holds. **Terminal content is never logged** by the host or the runner; there are no transcripts in phase 2 (an
  exited run keeps its ring for 1 h, read-only).

### 12.6 Terminals (#307) [SC]

#### 12.6.1 The term socket: location and verification

- **Location.** The runner listens on `<run>/term/<runner uid>/term.sock`. `<run>` is the same run directory the host
  and its clients use (`--run`, else `ORCH_HOST_RUN_DIR`, else `<state>/run`: `orch.host.enable.run_dir_for`). The
  directory `<run>/term/<uid>/` is created by the runner, owned by the runner UID, mode 0700 in tier A (tier B: 0750
  with a group holding the person, §9 item 11); the socket is 0600 (tier B 0660). The same ancestor rules as D-P2-6
  apply; the path stays under 100 bytes.
- **Discovery.** The runner reports `term_socket` in `runner.hello`; the host accepts it only if it equals
  `<run>/term/<runner uid>/term.sock` and returns it in every `term.attach` answer as `socket`, with
  `runner:{uid, pid, gen}`.
- **Verification by the dashboard server**, before sending a ticket:
  1. it computes the expected path itself from its own run directory and `runner.uid`, and refuses any other `socket`
     string;
  2. `lstat` of `<run>/term/<uid>/`: a directory owned by `runner.uid`, no write for group or other, no ACL granting
     write;
  3. after `connect`: the peer UID (`getpeereid`/`SO_PEERCRED`) equals `runner.uid` and the peer PID
     (`LOCAL_PEERPID`/`SO_PEERCRED.pid`) equals `runner.pid`. The host checked that PID against the service manager's
     main PID when the runner connected (D-P2-24), so the binding is transitive;
  4. the runner's first frame is `{t:"hello", gen, pid}`; `gen` must equal `runner.gen`.
  Any mismatch: the dashboard closes, sends `attach.cancel`, and shows "the runner changed; try again". In tier A all
  of this is hygiene against mistakes and stale sockets, not a boundary. The ACL part of step 2 is best effort
  (macOS `acl_get_link_np`, Linux the `system.posix_acl_access` attribute); the peer PID comes from
  `getsockopt(SOL_LOCAL, LOCAL_PEERPID)` on macOS and `SO_PEERCRED` on Linux. `orch runner enable` refuses, naming the
  path, when `<run>/term/<uid>/term.sock` would pass 100 bytes.

#### 12.6.2 The attach capability

The dashboard server calls `term.attach` **only when the browser's WebSocket has arrived with a valid browser ticket**
(lazy attach, §12.6.6), on a host connection of its own. The host mints the capability and registers it at the runner
before answering:

```
attach.allow  {att:AttId, ticket_hash:Hash,            // sha256 of the 32-byte ticket; the runner never sees the ticket
               workspace_id:WsId, person:Person, run:RunId, session:str | null,
               mode:"read"|"write", take:bool, lease_gen:int,   // the generation seen at minting (take:false only)
               runner_gen:Gen, from:int | null, nonce:str/[0-9a-f]{32}/, expires_at:Time}
```

- **Ticket:** 32 random bytes (base64url, 43 characters), returned only to the dashboard server. The host keeps only
  its hash, the runner only `ticket_hash`. **The host ticket, `nonce`, the socket path, `runner.pid`/`gen`, `lease_gen`
  and `input_id` never reach the browser.**
- **Lifetime:** 10 s unused, then the runner drops it. Minting allocates nothing but the pending record: **the ticket
  never holds the lease**, and a pending capability counts toward no attachment limit. `attach.cancel` releases it.
- **Consumption, single use:** the dashboard server sends `{t:"attach", ticket, nonce}` as its first frame on a new
  term socket connection. The runner atomically consumes the record whose hash matches and checks `runner_gen`,
  `nonce`, expiry and `run`. For `write`:
  - `take:false`: refused with `{t:"end", reason:"writer_busy"}` when the lease is held or its generation is no longer
    `lease_gen`;
  - `take:true`: takes the lease whatever its generation;
  - on success the generation increments, the old holder gets `{t:"writer", writer:false, lease_gen}`, and the runner
    sends `SIGWINCH` (a resize to the current size) so TUIs redraw.
  A read attachment cannot be upgraded on the same connection: writing needs a new `term.attach {mode:"write"}`.
- **Lease release on close:** when the connection holding the lease closes (any reason), the lease is released at once,
  the generation increments and every other viewer gets `{t:"writer", writer:false, lease_gen}` (nobody holds it). A
  reconnect asks for `read`; the page offers "Take control" and never re-requests `write` by itself.
- **Bad tickets:** a consumed, expired, unknown or other-generation ticket closes the connection with
  `{t:"end", reason:"ticket_invalid"}` and nothing else (a replay learns nothing). The lockout counts only
  presentations of a hash the runner **has seen** (a consumed or expired capability), at most 10 per minute per
  runner, then 60 s of refusals; an unknown hash closes the connection without counting, so no process can lock
  terminals out with random tickets.
- **Ending it:** the host sends `attach.end {att, reason}` when the person loses the mapping or membership, the ticket
  becomes invisible to them, a D-P2-31 kill switch is pulled (the addon revoked or disabled, the machine switch or the
  terminals switch off), the person stops the run, a host restart begins, the host cannot decide for 2 s whether the
  attachment may continue, or `attach.cancel` arrives; the runner closes that connection with `{t:"end", reason}` and
  discards its queue. Reasons: `person.removed`, `not_visible`, `runner.disabled`, `run.stopped`, `host.restarting`,
  `cancelled`, `runner.restarting`, `exited` (below), and `overflow` from the runner itself (a viewer that does not
  read). The runner holds only attachments the host's table holds: an end the runner does not confirm drops the
  runner session (ending every attachment), and one the runner reports that the host does not hold is ended.

#### 12.6.3 Frames on the term socket (dashboard server ↔ runner)

§0.2's codec (4-byte length, at most 2 MiB). After the attach frame:

- runner → server: first `{t:"ready", start:int, offset:int, cols:int, rows:int, writer:bool, lease_gen:int,
  state:"running"|"exited"}`; then `{t:"out", off:int, b64}` (≤ 64 KiB decoded), and control frames that are **never
  dropped**: `{t:"gap", from, to}`, `{t:"resize", cols, rows}`, `{t:"writer", writer:bool, lease_gen}`,
  `{t:"exit", code, signal}`, `{t:"ack", input_id}`, `{t:"throttle", retry_ms:int}`, `{t:"end", reason}`.
- server → runner (write lease only): `{t:"in", input_id:int, lease_gen:int, b64}` (≤ 64 KiB), `{t:"resize",
  lease_gen, cols:20..500, rows:5..200}`.
- **The dashboard server owns `input_id` and `lease_gen`.** It numbers input per attachment (monotonic), splits it into
  frames of at most 64 KiB, keeps one frame in flight (the next after the previous `ack`), and updates `lease_gen`
  from `ready` and `writer` frames. Ids are consecutive: the runner writes only the next id, acknowledges a repeated or
  lower id again without writing it, answers a higher id (a gap) with `throttle` without writing or acknowledging it,
  acknowledges each frame after the full write to the master and retries a partial write itself. A stale
  `lease_gen` is refused with `{t:"writer", writer:false}`. **Unacknowledged input is never replayed across a new
  attachment**: the server reports `input_lost` to the browser.
- **After `exit`** the runner sends `{t:"end", reason:"exited"}` and closes, so every attachment has one terminal
  state. Attaching to an exited run whose ring is still kept gives `ready {state:"exited"}`, the ring, `exit`, `end`.
- **Backpressure:** the ring (1 MiB per run) never blocks the PTY; each attachment has a 512 KiB queue of output frames
  at the runner; on overrun the oldest output frames are dropped and replaced by one `gap`. The dashboard server reads
  the runner socket continuously and keeps its own 512 KiB (decoded) queue per WebSocket with the same rule, emitting
  `gap` with the runner's offsets; control frames are never dropped on either side.
- **Input rate:** 256 KiB/s and 1 MiB per 10 s per run. Over it, the runner answers `{t:"throttle", retry_ms}` and
  does not write that frame (no `ack`); the server pauses reading the browser for `retry_ms` and resends the frame
  with the same `input_id`; the page shows "Input paused". Nothing is dropped silently.

#### 12.6.4 Resize

- **Only the current writer's resize applies.** A read-only viewer sends no resize (the server drops any, the runner
  ignores any) and renders at the writer's size: the `ready` frame carries the size and the runner sends
  `{t:"resize"}` to every viewer whenever the PTY size changes; the viewer's xterm keeps those columns and rows and
  scrolls in its container (it never fits or reflows to its own size).
- With **no writer**, the last size stays; a new writer's first resize after taking the lease applies.
- The dashboard server coalesces the writer's resizes to at most 4 per second; the runner sends one `resize` per
  applied size. Sizes outside 20..500 × 5..200 are refused.

#### 12.6.5 Viewers and the lease

- At most 4 attachments per run, 16 per person, 64 per runner (`attach.limit`).
- Attach is read-only by default; a remount or reconnect never takes the lease implicitly; the previous holder gets
  `writer:false` and a "Take back" banner.
- **Several tabs** of the same person attach independently and read-only by default; a tab that wants to type asks
  with `take:true`, which moves the lease to it (the others show "Another tab or device is controlling this
  terminal" with "Take control"); a tab never takes the lease on its own after a reconnect, a remount or a focus
  change.
- Only the run's own person may attach. Exited runs: the ring and an `exit` frame, read-only, until
  `Run.replay_until`; after that the dashboard shows **"Output no longer kept"** without trying to attach.
- When `run.stop` answers `cleanup:"incomplete"` or `"unknown"`, the dashboard shows **"Some processes may still be
  running"** next to the run.

#### 12.6.6 The browser path (dashboard server, orch-fa)

**Attach (lazy).**

1. `POST /api/w/<ws>/runs/<run>/attach {mode, take?, from?}` (bearer) stores the intent and returns a **browser
   ticket**: 128 random bits, stored hashed, single use, bound to the workspace, run, mode, take, from, the bearer
   session and the server generation, valid at most 30 s (the dashboard server's choice), 10 attempts per minute. It
   calls nothing on the host. (React StrictMode's double mount leaves one unused ticket that simply expires.)
2. The browser opens the WebSocket `GET /term` and sends `{"ticket":"…"}`.
3. Only then the server calls `term.attach`, verifies the runner (§12.6.1), connects and presents the host ticket at
   once. So the 10 s host capability and the runner's lockout can never be burned by a slow browser.

**WebSocket security.** The path is `/term` (outside `/api/**`, so the bearer rule there stays unconditional). The
`Security` middleware handles the `websocket` scope and closes before `accept` unless: `Host` is exactly
`127.0.0.1:<port>` (`localhost` refused; cookies, which other loopback apps may set, are ignored and never read), `Origin` is exactly `http://127.0.0.1:<port>` (missing and `null` refused), no
`Sec-WebSocket-Protocol`, `permessage-deflate` off, `max_size` 64 KiB. The first message must be text, at
most 4 KiB, within 5 s; any binary message before the attachment is bound closes the socket. The bearer session is
rechecked at least every 250 ms (as `live.py`); after logout or expiry the socket closes within about a second with
close code 4401 and no `end` message (`session.ended` is the close reason only). `connect-src` is `'self' ws://127.0.0.1:<port>` with the dashboard's own port only (no `ws:`
wildcard, no `localhost`); `script-src 'self'` (no inline script); `style-src` is not loosened for xterm.

**Messages, browser ↔ server.**

```
browser → server:  text {"ticket":str} (first, once) | text {"t":"resize","cols":int,"rows":int}
                   | binary: 1-byte tag, then bytes: tag 0 = from a key or paste event, tag 1 = a terminal reply
                     the page allowed (CPR or DA1 only); dropped on a read attachment
server → browser:  text {"t":"ready","start":int,"offset":int,"cols":int,"rows":int,"writer":bool,
                         "state":"running"|"exited"}                       (first message after the ticket)
                   | binary: 8-byte big-endian offset, then output bytes    (every output chunk carries its offset)
                   | text {"t":"gap","from":int,"to":int}
                   | text {"t":"resize","cols":int,"rows":int}
                   | text {"t":"writer","writer":bool}
                   | text {"t":"input_lost"} | text {"t":"input_paused","retry_ms":int}
                   | text {"t":"exit","code":int|null,"signal":str|null}
                   | text {"t":"end","reason":str,"code"?:str,"message"?:str,"retry_after_ms"?:int}
                                                                 (last message; then the close below)
```

- **Replay versus live:** every output chunk carries its offset; chunks with an offset below `ready.offset` are
  replay. The browser drops all `onData` until the `write` callback of the last replay chunk has fired (completion is
  counted in xterm's write callback, not at receipt). With no replay (`ready.offset` equals `start`, or `from` was the
  current end) `onData` is enabled right after `ready`.
- **`end` codes:** `code` is the catalog code behind the reason (for `host.error` the code `term.attach` answered,
  for example `term.not_attachable`, `attach.limit`, `run.limit`, `rate.limited`, `runner.unavailable`); `message` is
  untrusted plain text; `retry_after_ms` comes with retryable codes.
- **End reasons the browser sees:** the runner's (`person.removed`, `not_visible`, `run.stopped`, `host.restarting`,
  `cancelled`, `runner.restarting`, `exited`, `writer_busy`, `ticket_invalid`) and those the server synthesizes:
  `runner.lost` (the runner socket closed without `end`), `runner.changed` (the §12.6.1 check failed: "the runner
  changed; try again"), `session.ended` (logout or bearer expiry), `overflow` (the browser did not read), `protocol`
  (a malformed message), `host.error` (`term.attach` refused; the message carries its code).
- **Close codes** (the page decides retry by the close code alone):

  | Close | Retry | Reasons and codes |
  |---|---|---|
  | 1000 | no | `exited`, `run.stopped`, `cancelled` |
  | 4400 | no | `protocol`, `invalid.input` |
  | 4401 | new ticket | browser ticket refused, `session.ended` (no `end` message) |
  | 4403 | no | `person.removed`, `not_visible`, `ticket_invalid`, `op.not_allowed`, `not_found`, `runner.disabled` |
  | 4409 | no (offer "Take control") | `writer_busy`, `term.writer_busy` |
  | 4422 | no | `term.not_attachable` |
  | 4429 | after `retry_after_ms` | `overflow`, `rate.limited`, `run.limit`, `attach.limit`, `quota.host` |
  | 4503 | later, with backoff | `runner.lost`, `runner.changed`, `runner.restarting`, `host.restarting`, `runner.unavailable`, `host.unavailable` |
  | 4500 | no | `internal` and any other non-retryable code (a retryable code not listed maps to 4503) |
- **Dashboard server hygiene:** it never logs, stores, echoes or returns terminal output, input, tickets or frame
  contents (also not in exceptions or validation errors), and no HTTP route reads them. WebSockets exist only for
  mounted terminals and count toward the 16 open streams per session; each attachment uses one host connection for
  `term.attach` (counted in the §0.11 per-UID connection limit) and one term-socket connection.

#### 12.6.7 xterm.js configuration (testable)

- `allowProposedApi:false`; `windowOptions:{}`; no `onTitleChange` consumer (`document.title` and tab labels come only
  from `Run.label` or the ticket, through `visible.tsx`); `linkHandler` is the explicit no-op handler `NO_LINKS`
  (xterm's default OSC 8 behaviour would `confirm()` and `window.open`); addons Fit and Search only (no Clipboard,
  WebLinks, Image, Serialize, Unicode11); copy and paste only through the user's own gesture; `scrollback:5000`;
  `convertEol:false`; xterm pinned by the lockfile.
- **Replies are a property, not a hand list.** xterm answers queries by emitting `onData`: DA1, DA2 (`CSI > c`), DA3
  (`CSI = c`), DECRQM (`CSI Ps $ p`, `CSI ? Ps $ p`), DSR (`CSI 5 n`, CPR `CSI 6 n`, `CSI ? 6 n`, `CSI ? 15 n`,
  `CSI ? 25 n`, `CSI ? 26 n`), DECRQSS (`DCS $ q`), XTGETTCAP (`DCS + q`), OSC 4/10/11/12 colour queries,
  `CSI > q`, and window reports (off). **The filter lives in the page**, because the dashboard server sees only opaque
  input bytes: the `onData` handler forwards bytes from a key or paste event with tag 0, forwards a terminal-generated
  reply with tag 1 only when it is CPR or DA1, and swallows every other terminal-generated reply. The server never
  interprets input; it only rate-limits (tag 1 at most 4 per second, burst 8, the rest dropped; all input per
  §12.6.3). The property tested **against the page**: "for every sequence of the canary corpus plus a fuzz run, the
  page sends nothing with tag 0 without a key or paste event, and everything it sends with tag 1 is a CPR
  `ESC [ r ; c R` or a DA1 `ESC [ ? … c`". An xterm upgrade that adds a query type fails this test.
- Read attachments: `disableStdin`, and every `onData` dropped at the sink. During replay: every `onData` dropped
  (§12.6.6).
- On `gap` and at the ring start of a replay: `term.reset()` and a dimmed marker written by the page (never composed
  from host strings).
- **Canary corpus:** OSC 52 read and write; OSC 8 with `javascript:` and `data:`; OSC 0/1/2 titles; DA/DSR/DECRQM
  floods; XTGETTCAP and DECRQSS floods; 1 MB of random bytes; split UTF-8. Asserted: clipboard untouched, no
  `window.open`, no `document.title` change, the page's tagged replies within the CPR/DA1 allow-list.

### 12.7 Worktrees (#308) [SC]

```
Worktree = {ticket:Key, repo:RepoName, branch:str, path:str, generation:int, created_at:Time, created_by:Actor,
            head:Sha | null, state:"ok"|"missing"|"moved"|"unpinned"|"stale_branch", dirty:int | null,
            checked_at:Time | null}
worktree.list   {ticket?:Ref, check?:bool = false} → {worktrees:[Worktree]{0..200}}
worktree.create {ticket:Ref, repo?:RepoName, base?:"default"|"current" = "default"} + opts.idem? → {worktree}
worktree.remove {ticket:Ref, repo?:RepoName, force?:bool = false} → {removed:true}
```

- `repo` defaults to the only linked repo (required with several). The branch is the current `links.branches.<repo>`
  (none: `worktree.no_branch`; the host never invents one). `base` applies only when git must create the branch.
- **Create:** column, visibility, claim (G), the declared repo's pin, no registration yet (`worktree.exists`), limits;
  path `<declared repo path>.worktrees/<WS8>-<KEY>`; runner git `-c core.hooksPath=/dev/null -c core.fsmonitor=false
  worktree add …` with no network (`GIT_ALLOW_PROTOCOL` empty), under the per-repo lock, claim rechecked at execution.
  git refusing a branch already checked out maps to `worktree.branch_in_use`. Then the host pins it through its reader
  (`.git` file → `<C>/worktrees/<id>` with `<C>` the declared repo's pinned commondir; the back-pointer names this
  worktree; `HEAD` is `ref: refs/heads/<branch>`), writes the pin and a new generation to `git-pins.json`
  (`worktrees.<ticket uid>.<repo>`) before use; a failed pin is `worktree.unpinned`.
- **D58 and `tree.check`** (amends D-P2-19 and H10a): for a ticket with a registration for repo `r`, the branch check
  and the clean-tree check read that worktree's pins; refs and objects still come from the common dir (`branch.pushed`,
  `diff`, `commit.list` unchanged). The prepared request, each binding and the precommit carry the generation; a
  changed generation, pin, or a HEAD not on the current branch is `observe.unavailable`, never a fallback.
- **Remove:** `tree.check` first; dirty is `worktree.dirty` unless `force` (P only); a live run with that cwd is
  `worktree.in_use`; no registration is `worktree.none`; the branch is never deleted; the registration and pin go
  after the runner reports success, and the removal bumps the registration generation, so any request prepared against
  the removed worktree is invalidated (`observe.unavailable`).
- **Filters and other programs (the invariant, Opus #439 F1 and D1):** orch never runs git against a repository whose
  local/worktree config or attributes could make git execute a program or read config from elsewhere. The rule is an
  **allow-list**:
  - **The gate.** Before every worktree git command, `orch.gitsafe.gate` lists the repository's local and worktree
    config with `git config --no-includes --list --show-scope`, which executes nothing. If any key is not on the
    allow-list (`gitsafe.ALLOWED`, `ALLOWED_SUB`), git is not started and the answer is `observe.unavailable`, naming
    the key, never its value.
  - **What the list holds:** exactly what an ordinary repository needs.
    - `git init`'s `core.*` keys and the other plain `core` settings.
    - `remote.<n>.{url,pushurl,fetch,push,tagopt,prune,mirror,…}`.
    - `branch.<n>.{remote,merge,rebase,pushremote,description}`.
    - `user.*`, `init.defaultBranch`, `extensions.{objectFormat,worktreeConfig,refStorage,preciousObjects,partialClone}`.
    - The tuning keys of `gc`, `pack`, `fetch`, `pull`, `push`, `merge`, `rebase`, `submodule`, `status`, `diff`,
      `color`, `column`, `advice`, `i18n`, `blame`, `maintenance`, `log`, `rerere`, `index`, `feature`, `checkout`,
      `worktree` and `transfer`.
    - The signing switches only: `commit.gpgSign`, `tag.gpgSign`, `gpg.format`.
    - LFS's own `lfs.repositoryformatversion` and `lfs.<url>.access`.
  - **What it never holds:** anything that names a program, a command, a driver, a hook or another config file. That
    excludes `filter.*`, `diff.<driver>.*`, `merge.<driver>.*`, `hook.*`, `include*`, `alias.*`, `pager.*`,
    `credential.*`, `gc.recentObjectsHook`, `remote.<n>.{vcs,uploadpack,receivepack}`, `*.cmd` tools, editors, gpg
    programs, `core.attributesFile` and any unknown section. A non-boolean `core.fsmonitor` is refused.
  - **Why an allow-list.** The class "a config key that runs a program" is open-ended, and each git release can add
    one. Four slipped through one review cycle: hooks, fsmonitor, filters, then git 2.54's `hook.<name>.command`,
    which `-c core.hooksPath` does not turn off. The deny-list of known keys stays only as documentation
    (`gitsafe.KNOWN_EXEC`) and as tests.
  - **Records.** The records tool's `check_local` applies the same function before its stricter rules.
  - **Config file ownership (Opus D3).** `<common dir>/config`, `config.worktree` and the git dirs holding them must
    be owned by the runner's effective UID and writable by no group or other. A symlinked config file is refused. git
    itself checks only the directory's owner.
  - **Local filters are refused.** A repository with `git lfs install --local` or git-crypt has its filters in the
    local config, so it is refused. Install such filters globally.
  - **Attributes only *select* drivers.** After the gate, the repo's config defines no program. An attribute can
    therefore select only a built-in behaviour or a driver the person configured globally (LFS after
    `git lfs install`). That driver runs as the runner's UID, the person in tier A, as when they run git.
  - **Overridden keys.** A local `core.hooksPath` (husky) and a boolean `core.fsmonitor` are on the list, because
    `-c core.hooksPath=/dev/null` and `-c core.fsmonitor=false` override them on every command.
- **Submodules (Codex #439):** orch does not inspect submodules.
  - **Why.** A working copy with a **populated** submodule (a gitlink in the index whose directory is not empty)
    contains a second repository with its own config. The gate does not list that config. `git status` would run the
    submodule's filters, and `--ignore-submodules` would instead hide its changes.
  - **What is refused.** Every cleanliness verdict refuses such a tree (`gitsafe.submodules`):
    - direct-mode D58 on a registered worktree gives `observe.unavailable`;
    - direct `list --check` gives `dirty: null` (unknown, never `0`);
    - direct `remove` without `--force` gives `observe.unavailable`.
  - **Host mode.** The host's reader (`tree.check`) already refuses a checked-out submodule.
  - **Not checked out.** An empty gitlink directory counts as clean, as git sees it.
- Done or closed tickets keep their worktrees; the Worktrees page offers removal.

#### As built in H11d (worktrees)

Where the code differs from or adds to §12.7, the code is this:

- **Registrations** live in `<state>/hosts/<ws>/git-pins.json` under `worktrees.<ticket uid>.<repo>` as
  `{gen, reg}`; `reg` is `{path, branch, pins | null, created_at, created_by, idem}` (`pins: null` is an `unpinned`
  registration). Create and remove each bump `gen` (compare-and-swap). `Git.ticket_copy` returns the registered worktree or
  the declared copy and **never** falls back from a registration; prepared requests carry `copies` (the binding: repo,
  ticket, generation, registered, pin), checked at prepare, at every `human.get` binding and at the D58 precommit
  (`human.bound_copy`), always with the ticket's **current** branch against `HEAD`.
- **Re-pointed `.git` file.** Reads use the pins, not the `.git` file, so `bound_copy` and `worktree.list {check}` also call
  `HostGitReader.verify_worktree`, which pins the path again from the commondir's root and compares worktree, gitdir and
  commondir (device and inode): a re-pointed file, a swapped or moved directory is `observe.unavailable` (`moved` in the list).
- **Runner `git`** (`orch.runner.wtgit`): the `orch.gitsafe` gate on the repo (and, on remove, on the worktree, whose
  status git runs) before git starts; hooks, fsmonitor, network and submodule recursion off; built-from-scratch
  environment, 30 s, own process group. A gate refusal is the runner code `observe.unavailable`. **Confinement:** the
  path must be exactly `<repo>.worktrees/<WS8>-<KEY>` (strict regex); every directory from `/` to `<repo>.worktrees`
  is opened with `O_NOFOLLOW|O_DIRECTORY` per component and must be owned by root or the runner's effective UID and not
  group- or other-writable unless sticky (Opus D2); the parent of the repo and `<repo>.worktrees` must be owned by the
  effective UID and not group- or other-writable at all (else `launch.cwd_changed`); the target is `lstat`ed through that fd, a symlink anywhere is
  `launch.cwd_changed` and nothing behind it is touched or removed. git gets absolute paths (`-C <repo>`, `-- <path>`)
  and runs with cwd `/`, so there is no cwd pin: the walk from `/` is repeated right before git (pre-check) and after
  it (post-check), each comparing device and inode with the held fd. Residual race: a writer of those two directories
  can swap names between the pre-check and git's own `mkdir` or removal; the post-check detects the swap, it cannot
  undo git's write. The ownership and mode check of every directory on the path makes that writer the person's own
  account (or root). D2 and D3 check POSIX mode bits only; extended ACLs (macOS `com.apple.acl`, Linux
  `system.posix_acl_access`) are a known gap, not evaluated.
- **Gate:** the addon view is `addons[name]["enabled"]` (a frozen mapping), `runner.disabled` names the first missing piece
  (addon, then machine). The claim of a G caller is rechecked under the per-repo lock right before the runner runs git.
- **Direct mode** (`orch worktree list|create|remove`, POSIX; `orch.worktree_cli`): the same rules without a host. The
  ownership lock is held shared for the whole command; a served workspace is `host.required`; `ORCH_GRANT` is
  `op.not_allowed`; Windows is `unsupported.platform`. `create` and `remove` hold an exclusive per-workspace `flock`
  (`<state>/hosts/<ws>/worktrees.lock`, 10 s, then `lock.busy`) from loading `git-pins.json` through git and the write,
  so concurrent direct commands never lose a registration. Git runs through the same gated, confined `run_git` and
  `read_git`; `create` refuses a path that is a declared repository (as the host); the new worktree is pinned in
  process (`pin_repo`) and registered in the same pin file, which direct-mode D58 (`orch verdict`, `approve code`)
  reads. `list --check` observes as the host: the pins verified again (`moved`), `HEAD` read from the pinned gitdir
  without git and the ticket's current branch (`stale_branch`). `remove` refuses a dirty tree without `--force` (the
  person is at the terminal); the branch is never deleted. `remove` (host and direct) checks cleanliness through the
  registration's path without re-verifying the pins first; git's own non-`--force` remove refuses a dirty or untracked
  tree as the backstop, and G can never force.
- **Fail closed.** `bound_copy` refuses (`observe.unavailable`) with no reader, a missing or mismatched binding, generation,
  registration or pin, and an unpinned registration. Host mode checks the bindings (`check_bindings`: `bound_copy` for
  every linked repo plus the ticket's **current** `links.branches`, loaded fresh, against the bound ref) on every
  signer-bearing `human.get`, independent of the 5 s tree-check throttle, so a relink, removal or recreation after
  prepare or binding is refused there, not only at the precommit. Direct-mode D58 records the binding (generation,
  registration, pin) at its first look and the precommit must find the same one (a worktree removed or recreated while
  the person decides is refused, never read in its new or the declared place); it re-pins the registered worktree in
  process (`verify_worktree_pins`), runs the `orch.gitsafe` gate on it, and refuses a malformed pin file, a registration
  without pins, a re-pointed `.git` file or a missing directory. Without a `git-pins.json` the declared copy is read only
  when no directory exists at the ticket's worktree path (`<repo>.worktrees/<WS8>-<KEY>`); one on disk is refused (a
  deleted pin file never becomes a fallback). Direct-mode D58 on the **declared** copy still uses P1's
  `orch.store.observe` (the person's own terminal, unchanged by H11d).
- **`git worktree prune`** (remove of a directory that is already gone) drops git's record of every stale worktree of
  that repository, not only this ticket's; other registrations whose directory is gone then fail closed (gitdir
  mismatch) until removed.
- **Not built:** `worktree.in_use` is only as good as the runner's run records (a run started outside the host is not
  seen); there is no addon check in direct mode.

### 12.8 Records commit and push (#308) [SC]

**What records are:** `events/workspace.jsonl`, `keys.jsonl`, `config.json`, `AGENTS.orch.md`, and per ticket
`ticket.json`, `body.md`, `events.jsonl`, `artifacts/*`; never `.state/` or `addons/`. Git metadata (trailer, author)
is transport metadata, never authorization or freshness: consumers verify by F1 replay and checkpoints, and
`AGENTS.orch.md` and `config.json` get no authority from sitting in the repo.

```
records.status   {current_url?:str} → {configured:bool, repo:{state:"ok"|"missing"|"not_dedicated"|"unsafe"|
                       "index_differs", problem?:str} | null, pending:{events:int, tickets:[{key:Key, events:int}]
                       {0..200}, more:bool, workspace_events:int}, last:{commit:Sha, at:Time, by:Person, ws_seq:int,
                       pushed_at:Time | null} | null, auto:{on:bool, every_min:int, push:bool} | null,
                       remote:{url:str | null, private:bool, export:"ok"|"blocked_convenience",
                       current_url:str | null, current_private:bool | null, matches_last:bool | null} | null}
                       // remote: the push URL git resolves now (§12.8), shown through visible.tsx
                       // configured: the workspace's records repo exists (#441 follow-up; see "As built")
records.snapshot {page?:str} → {manifest:Hash, ws_seq:int, heads:{str:Hash}, taken_at:Time,
                       files:[{path:str, size:int, sha256:Hash}]{0..2000}, next_page:str | null}
records.export_check {sha:Sha, url:str{1..512}, ref:str, history_convenience:bool} → {ok:true}
records.note     {manifest:Hash, commit:Sha, pushed:bool, url?:str} → {}
```

- `manifest` = digest over (workspace id, `ws_seq`, chain heads, file list); pages belong to it and expire after
  10 min. The host may cache file hashes by (path, size, inode, mtime) so the store lock is not held for re-hashing
  immutable artifacts. `pending.tickets` lists only visible tickets; counts are aggregates.
- **Commit:** a dedicated repo only (the record root is its top level; `orch records init` creates it), else
  `records.not_dedicated`; no `MERGE_HEAD`, `REBASE_HEAD`, `CHERRY_PICK_HEAD` or detached `HEAD`
  (`records.git_state`). Local and worktree config (includes counted) may not set `url`, `http`, `credential`,
  `protocol`, `filter`, `core.sshCommand`, `core.askPass`, `core.gitProxy`, `core.fsmonitor`, a foreign
  `core.hooksPath`, or anything for the remote in use but `url`, `fetch` and #254's harmless keys; one URL per remote
  (`records.unsafe_config` names the key). Each path is resolved component-wise under the record root fd with
  `O_NOFOLLOW` (regular files only), read **once**, exactly `size` bytes, hashed, and those bytes go to `git
  hash-object -w --stdin`; 64 MiB per file, 1 GiB per snapshot (`records.too_large`); a mismatch retries the snapshot
  once (`records.snapshot_changed`). `GIT_INDEX_FILE=<tmp>`, `git read-tree --empty`, add exactly the manifest,
  `write-tree`, `ls-tree -r` must equal the manifest, `commit-tree -p HEAD` (message `orch: records <PREFIX> ws_seq
  <n>`, trailer `Orch-Records: <workspace id>`, author and committer `orch records <records@orch.invalid>`),
  `update-ref` compare-and-swap. The real index is refreshed only if it equalled the old `HEAD^{tree}`; otherwise
  `records.status` says "index differs, run `orch records sync-index`". The working tree is never written.
- **History:** at `orch records init` on an existing repo, or before the first push, every commit reachable from the
  branch must be a records commit of this workspace with a records-only tree, else the tool refuses and offers a fresh
  orphan history. The validated tip is kept in `records.json`; later pushes validate `tip..HEAD`.
  `GIT_NO_REPLACE_OBJECTS=1`; `info/grafts` and shallow repos are refused.
- **Push:** fast-forward only. The tool resolves the effective push URL once (`pushurl`, else `url`; `insteadOf` and
  `pushInsteadOf` already refused), validates the history, scans the event logs reachable from S for an event with
  `auth:"convenience"` (#441, see "Push rule"), calls `records.export_check` with that literal URL and the scan's report, all before any
  network call, then `ls-remote`s it, checks that the
  remote tip T is an ancestor of the commit S, and runs `git -c push.followTags=false push --no-recurse-submodules
  --force-with-lease=refs/heads/<b>:T <url> S:refs/heads/<b>`. A missing remote branch only with `orch records push
  --initial` (terminal). Divergence or a lease failure: `records.push_rejected`, never a rebase.
- **Every git call:** argv only, `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, environment `{HOME, PATH,
  SSH_AUTH_SOCK, LANG=C, LC_ALL=C, GIT_TERMINAL_PROMPT=0, GIT_ALLOW_PROTOCOL=https:ssh, GIT_NO_REPLACE_OBJECTS=1}`,
  `ssh -o BatchMode=yes` unless a global `core.sshCommand`, stdin closed, 10 s local, 20 s network, 60 s per run.
  System and global config are the person's and are read.
- **Who:** owners and maintainers (a push exports every ticket, restricted ones included; the UI says so). Agents get
  `op.not_allowed` (`records.snapshot` is P only).
- **Starting it:** `orch records commit|push|status|init|sync-index`, the dashboard server as a fixed-argv child
  (`python -I -m orch.records commit [--push] --workspace <id> --json`; it reads the root from the person's own
  registry), or the timer unit `io.orch.records` / `orch-records.timer` every 5 min for auto mode. One run per
  workspace at a time (a lock in the person state dir).
- **Local config** `<person state>/records.json` (0600): per workspace `{auto, every_min:5..1440, push,
  private_remotes:[url], validated_tip}`. `orch records auto on|off [--push] [--every N]` and `orch records remote
  private URL` are terminal only; the dashboard may switch `auto` off, never on. **The private mark is the URL git
  itself resolves for the push** (`git remote get-url --push <remote>`, after `insteadOf`/`pushInsteadOf` are refused):
  that exact string is stored and later compared byte for byte with the URL git resolves at push time; no other
  normalization (case, trailing `/` or `.git`) is applied, so any change of the URL needs a new mark. **Private remote (owner decision,
  12 Oct 2026):** the terminal prompt and the Records page say "orch trusts your word that only you read <url>".
- **Log:** `<person state>/person/records.jsonl` (0600, 1 MiB × 4): start, commit, push result, refusal codes; never
  contents or credentials. #309's hook installer never touches the records repo.

#### As built in H11e (#430 and part 2)

Where this differs from the text above, the code follows the tie-break (D-P2-33 Codex in full, D-P2-34) and the owner
decision of 12 Oct 2026 on the private remote; this list is what is built.

- **Cap and ops.** `records/1` with `records.snapshot`, `records.export_check`, `records.status` (reads, op kind
  `person_read`: column P, owner or maintainer) and `records.remote_private`, `records.note` (writes, op kind
  `person_write`, column P only: `opts.idem` required, no `dry_run`, an outcome record, a retry replays it).
  `records.remote_private` is for **owners only**. G and N get `op.not_allowed`.
  - `records.snapshot {page?}` → `{workspace, manifest, ws_seq, heads, taken_at, count, bytes, files, next_page}`
    (no `for` argument; the export decision is `records.export_check`).
  - `records.export_check {sha, url, ref, history_convenience}` → `{workspace, sha, url, ref, history_convenience,
    policy:"none"|"private_remote", convenience, check}` or `sign.convenience_readers`; it takes no `manifest` (the
    push may be of an older commit). `history_convenience` (required, #441) is the tool's report that an event log
    reachable from `sha` holds an event with top-level `auth:"convenience"`; `convenience` is true when the flag is
    on, the durable `convenience_seen` mark is set, the merged history (or a cut tail) holds such an event, or the
    report says so. A merged-history or cut-tail hit sets the mark; **the report never does**: it refuses this push
    only.
  - `records.remote_private {url?, on}` → `{private_remote:{url, at, by} | null, text}`.
  - `records.note {manifest?, commit?, pushed, url?, repo?:{state, problem?}, auto?}` → `{}`; a `manifest` must be
    one this caller took and the host still holds (`page.expired` otherwise). Notes are kept **per person**
    (`lasts[<person>]`, at most 64), with `last_noted {by, at}` workspace-wide (#433 review, Opus finding 1).
  - `records.status {current_url?}` → as in the block above, for **the caller's own** last note (another manager's
    note never changes it), plus `last_noted`. `repo` and `remote.url` come from the caller's last commit or push
    note (the host runs no git, D-P2-19). `remote.url` is **display only**: a client never passes it to
    `records.remote_private` (the tool resolves the URL to mark from git itself). The tool's `status` adds `local
    {repo, url}` as it sees them now and writes nothing.
  - **`configured`** means **the workspace's records repo exists** (not "a note exists"): the last repo state any
    tool reported for the workspace (`repo {state, at, by}` in the host's `records.json`, from any manager's note;
    a record from before it falls back to any person's last report) is `ok`, `index_differs` or `unsafe` (`missing`
    and `not_dedicated` are false). `orch records init` reports the repo. The tool's own `status` replaces it with
    what it sees now (`local.repo`). Same in host and direct mode.
  - **The current push URL** (for the dashboard's push confirmation): the tool's `status` passes the effective push
    URL it resolved now as `current_url` (refused like any URL that can hide a secret). The answer's `remote` then
    has `current_url` (for display: userinfo stripped; for anyone but an owner only the host, `local` for a path or
    `file://`), `current_private` (that exact string carries the private-remote mark) and `matches_last` (it equals
    the caller's last pushed destination, `remote.url`). Without `current_url` the three are `null`; `remote` is
    `null` only when there is neither a last URL nor a current one (`url` is then `null` when only the current is
    known, and `private`/`export` describe the current URL). Display only: the push resolves the URL and asks
    `records.export_check` again at push time.
- **Limits.** 64 MiB per file, 1 GiB and 200 000 files per snapshot (`records.too_large`); pages of 2 000 files bound
  to the manifest and the caller's principal for 10 min, at most 4 manifests per workspace; 50 export and 50 mark
  audit entries; `pending.tickets` at most 200. Hash cache by (path, size, inode, mtime, ctime).
- **Manifest id v1.** `m_` + SHA-256 over the canonical JSON (sorted keys, no spaces, ASCII) of `{"v":1,
  "workspace":<id>, "ws_seq":<n>, "heads":{log: head}, "files":[[path, size, sha256], …] sorted by path}`. The tool
  recomputes it from the pages.
- **Two state files.** The host's `<state>/hosts/<ws>/records.json` (0600): `private_remote` (the one mark),
  `marks` (audit: action, redacted URL, person, time), `exports` (audit of every export check), `last` (the note).
  The host's `<state>/hosts/<ws>/convenience_seen.json` (0600, #441): the durable mark (§0.8.6 item 1), never
  cleared. The host's `records.json` also keeps `repo` (the workspace's last reported repo state, for `configured`).
  The person's `<state>/records.json` (0600): `validated` (per workspace and branch, the last validated tip), `clean`
  (per workspace and branch, the last tip scanned clean of convenience events) and
  `auto` (per workspace `{on, every_min, push, root?, last_run?}`; `root` only for a direct-mode workspace,
  `last_run` the timer's own clock). In tier A both live
  in the same state directory. The mark is in the host file, not in the person's (`private_remotes:[url]` above is
  not built): one exact URL per workspace, set through a P op.
- **Tool flow** (`python -I -m orch.records`, also `orch records`). Resolve the workspace (host mode when the person's
  registry names it, else direct mode); one run per workspace (`<state>/person/records-<ws>.lock`, `records.busy`);
  60 s for the whole run, git and host calls together (`records.budget`). Commit: dedicated repo and branch checks,
  the local config deny-list, every page of `records.snapshot` (id recomputed), each file read once beneath the root
  fd with `O_NOFOLLOW` and streamed into one `git fast-import` as it is checked (one file in memory at a time),
  `read-tree --empty` into a private temporary index, `ls-tree -r` must equal the manifest, `commit-tree
  --no-gpg-sign` with the message `orch: records ws_seq <n>` + `Orch-Manifest: <id>` + `Orch-Records: <ws>`, author
  and committer `orch records <records@orch.invalid>`, `update-ref` compare-and-swap, the real index refreshed only by
  compare-and-swap (under `index.lock`, beneath the git-dir fd) when it equalled the old HEAD tree, then
  `records.note`. The commit message has no `<PREFIX>` (the trailer names the workspace).
- **History rule.** Every commit reachable from S and not from the validated tip: at most one parent, only
  `tree`/`parent`/`author`/`committer` headers, exactly the message shape above, and changes to record paths only, as
  mode 100644 (`records.not_records_only`, `records.history_invalid`). `GIT_NO_REPLACE_OBJECTS=1`,
  `core.commitGraph=false`; grafts and shallow repos refused. `commit --fresh-history` makes a root commit.
- **Push rule.** S resolved once; the effective push URL resolved once by git (`remote get-url --push`, exactly one,
  https or ssh only, no userinfo, query or fragment); `ls-remote` and `push` run in a throwaway config-free bare repo
  whose alternates name the records repo's objects, keeping only `credential.*`, `http.*`, `ssh.*` and
  `core.sshCommand` of the person's frozen system and global config (no `url.*`, no `remote.*`); any `remote.<x>.*`
  in the records repo's own config other than the remote in use is refused. **Before any network call** (#441): the
  history check, the convenience scan and `records.export_check(S, URL, ref, history_convenience)`. Then T from
  `ls-remote` must exist locally and be an ancestor of S (an up-to-date remote ends the run here), a recheck of the
  local config and the URL, and `git push --no-verify --porcelain --no-recurse-submodules --no-follow-tags
  --force-with-lease=<ref>:<T> <url> <S>:<ref>`.
- **The convenience scan** (#441, #454 review). Only event logs count: blobs at `events/workspace.jsonl` or
  `tickets/<ulid>/events.jsonl` that a commit reachable from S (and not from the tip last scanned clean) added or
  changed (`diff-tree`; the history is linear). Bodies and artifacts are free text agents write and are never read.
  A line counts when it parses as JSON, leniently (`\u` escapes, duplicate keys, any order or spacing), with
  top-level `auth` `"convenience"`; a line that does not parse as a JSON object counts too (fail closed: agents cannot
  write raw event-log lines); any parse error, `RecursionError` included, is caught. Every records git call runs with
  `GIT_NO_LAZY_FETCH=1`, so a partial clone never fetches behind the scan; as a backstop for git older than 2.45, a
  records repo whose own config sets `extensions.partialClone` or `remote.*.promisor`/`partialclonefilter` is
  `records.unsafe_config`. A tip scanned clean is kept as `clean
  {sha, scanner}` per workspace and branch in the person's `records.json`; an entry of another scanner version means
  a full rescan. Never a non-fast-forward. A missing remote branch needs
  `push --initial` (terminal). **The mark binds that URL string; the person's own settings (ssh config, credential
  helpers, DNS) decide how it is reached.**
- **Codes and exits** (`orch.records.RecordsError`; the host op passes the same code and exit):
  `records.snapshot_changed` 9 (retryable), `records.busy` 9, `records.budget` 9, `records.snapshot_invalid` 5,
  `records.invalid` 5, `records.timer_failed` 1, and 3 for `records.timer_conflict`, `records.not_dedicated`, `records.git_state`,
  `records.unsafe_config`, `records.not_records_only`, `records.history_invalid`, `records.tree_mismatch`,
  `records.too_large`, `records.unsafe_tree`, `records.push_rejected`, `records.remote_invalid`,
  `records.remote_missing`, `records.git_failed`, `records.unsupported` (Windows and other unsupported setups; the
  `unsupported.platform` row of §0.6 is not used), `records.unconfirmed`.
- **CLI synopsis.**
  ```
  orch records init|commit [--push] [--fresh-history]|push [--initial [--yes]]|status|sync-index
               |remote-private [--off] [--yes]|auto on [--push] [--every MIN] [--dev]|auto off|auto [--state DIR]
               [--workspace ID] [--remote NAME] [--json]
  ```
  Terminal only (asked on `/dev/tty`): `auto on`, `push --initial` and `remote-private` (the last two skip the
  question with `--yes`: the boundary is the host op, an owner-only write; the question is UX). The time spent at a
  question does not count against the 60 s budget (it restarts after the answer). `--state` is the timer's own
  option (`auto` without a switch); anywhere else it is `records.invalid`. Agents (`ORCH_GRANT`) get
  `op.not_allowed`.
- **Dashboard child.** `python -I -m orch.records commit [--push] --workspace <id> --json` and `… auto off
  --workspace <id> --json` print exactly one JSON line valid against `orch.records.cli.CHILD_SCHEMA`:
  `{ok:true, commit?, pushed?, url?, detail?}` or `{ok:false, code, message, hint?, commit?}` (`commit` when a push
  failed after the commit landed); exit 0 or the code's exit. A third argv reads the status:
  `python -I -m orch.records status --workspace <id> --json` resolves the effective push URL as a push does, calls
  `records.status {current_url}` and prints `{ok:true, detail:{status}}` (the host's answer with `local` and
  `configured` as the tool sees them now, and `remote.current_url`, `current_private`, `matches_last`). What it does
  exactly: **no writes to the records repo and no network** (`ls-remote --get-url` only, `GIT_NO_LAZY_FETCH=1`; the
  index is compared with `diff-index --cached`, never `write-tree`), no records lock file and no run log, and a
  throwaway temporary transport directory (the one push uses to check the literal URL). It **may record the
  convenience mark** (when the host's scan of the merged history or cut tails finds a convenience event: a host
  observation, as for any `records.status`), and in direct mode it opens the store, which takes the store lock and
  may run its recovery, like any direct-mode read. Same gitsafe and
  environment rules as push (`HOME`, `PATH`, `SSH_AUTH_SOCK` at most); it fits the dashboard's bounded child (60 s,
  minimal environment, `python -I`).
- **Auto mode and timer.** `auto on` asks on the terminal, writes the person's `auto` entry and installs a per-user
  timer: LaunchAgent `io.orch.records` (`StartInterval` 300) or systemd user units `orch-records.service`
  (`Type=oneshot`) and `orch-records.timer` (every 5 min), running `python -I -m orch.records auto --state <state>
  --json`. A source checkout is refused as the timer's program unless `--dev`. `auto off` removes the entry and,
  when no workspace is left, the timer, and only a timer installed for this state dir. An installed timer that
  differs (state dir, interpreter, dev versus release) is never replaced silently: `records.timer_conflict` names
  the fields, `auto on --replace` overrides (H7 parity). The timer's run decides on its own clock only (`last_run`
  in the person's file, never a host note): a due workspace is committed (a no-op when nothing changed) and pushed
  when `push` is on; one failing workspace never stops the others, and the run answers `ok:false` with the first
  code when any failed. It rotates `records-timer.log` (launchd's stdout) like the person log. Same fences as H7: the default
  runner refuses `launchctl`/`systemctl` under pytest or `ORCH_NO_SERVICE_MANAGER=1`, the test session guard refuses
  units under the real home and checks the real unit paths are unchanged.
- **Direct mode** (D-P2-38). A workspace not in the person's registry: the tool opens the store read-only under
  `DirectGuard` and runs the same ops on the same files (`host.required` once a host serves it). The host writes
  `<root>/.state/host-served` (content `{"v":1}`, created exclusively) when it starts serving a workspace and
  removes it at unregister; direct mode refuses
  while it exists, whatever state directory the tool was given. POSIX only. No role check in direct mode (the person
  owns the files; audit entries name `direct:<uid>`); the export check still applies.
- **Person log.** `<state>/person/records.jsonl` (0600, rotated at 1 MiB, 4 files): `start`, the command's result
  (commit, pushed, redacted URL), `refused` with the code; never file contents or credentials.
- **Not built.** Hashing outside the store lock (the cache bounds re-hashing; a first snapshot of a large workspace
  still hashes under the lock); validating history at `orch records init` (it runs before the first push instead).

### 12.9 Quick tasks (#308)

**Built in H11f (#429):** "As built in H11f" (§0.8) is authoritative where it differs from the design text below.

`quick.list {status?:[Status], limits?:{commits:int 1..100, files:int 1..1000}, limit?:int 1..200 = 100, page?:str}` →
`{rows:[Row & {quick:{commits:int | null, files:int | null, over:bool | null}}], next_page, total}`: exactly
`ticket.query {label:["quick"]}` for the viewer, plus per row with a linked repo and branch the counts from the host
reader (`null` when unreadable). Adding is `new` (`type: chore`) plus a label `set` (an agent: `orch quick add "…"`;
a person: two click signatures until person `new` may carry labels, §9 item 12). Closing and converting use existing
ops. No quick-specific state exists.

### 12.10 Trust boundary

| Action | Path | Signature | A dashboard takeover can do it (tier A) |
|---|---|---|---|
| Start an agent with a deposited runner grant | `agent.start` | none | yes |
| Issue a runner grant | click, deposit-only, word `<PREFIX>~<WS8>/GRANT`, ≤ 12 h; or terminal | **yes** | yes when click-to-sign is on |
| Open a shell, attach, type, stop | `term.*`, `run.stop` | none | yes: code execution as the person |
| Create or remove a worktree (force too) | `worktree.*` | none | yes |
| Commit and push records | records child | none | yes (owner, maintainer; only to a URL the export check allows) |
| Turn on dashboard execution, auto push, a private remote | terminal CLI | — | the dashboard has no op for it; a same-UID process can |
| Grant an addon in the workspace | `addon.grant` | **yes** | yes when click-to-sign is on |

- The launched agent has the person's UID: it can read the WSK, the convenience key, ssh keys, the runner's memory
  (every deposited grant), and call the host as column P without `ORCH_GRANT`, including terminals and launches.
  Columns are policy against honest clients, not a boundary against same-UID agents (host §7, §0.8.6 card text).
- The secret is kept out of the HTTP process, the browser and all logs against **passive** disclosure; a takeover can
  make a started agent print it.
- Runs, terminals and worktrees add no reader of the workspace log. The click grant follows the readers condition like
  every click op. A records push adds a reader (D-P2-34). §0.8.6 item 5's rule holds: no addon page can make the
  dashboard call `prepare`, `agent.start`, `term.*` or `run.stop`.
- A person-run Claude or Codex in the dock (`term.open {harness}`) acts as the person, like running it in their own
  terminal (owner FYI).
- Accepted for phase 2 (owner FYI): terminal rendering in the main dashboard origin; an xterm parser bug fed by agent
  output could take over the dashboard. Mitigated by the configuration above; an isolated origin is a phase-3 item.

### 12.11 UI contract for the dashboard (orch-fa)

| Feature | Host ops and items | Dashboard server |
|---|---|---|
| Card line, enablement | `whoami.runner` (`RunnerInfo`, cap `run/1`); `runner.disabled` `detail.missing:"addon"\|"machine"\|"terminals"` | shows the D-P2-31 card line when `enabled` |
| Agents page, dock | `run.sub` first, then `run.list {since}`; keep the higher `rev`; `{t:"gone"}` removes | SSE fan-out like `live.py` (replaces the mockup's `agent.started`/`agent.stopped`) |
| Start dialog | `agent.preview` → show exactly `display`, cwd, env names, grant, warnings, the login-shell line → `agent.start {…, spec}` with idem; `blocked.code` is one of `runner.disabled`, `runner.unavailable`, `launch.claimed`, `launch.no_grant`, `launch.invalid`, `harness.*`, `worktree.*` | core component `SpawnConfirm`; never from an addon frame |
| Dock: person-run shell, Claude, Codex | `term.preview` (shows `display` and the notice "acts as you, not as an agent") → `term.open {…, spec}` with idem | runs as `kind:"shell"` |
| Runner grant | click `prepare {op:"grant", deposit:true}` → `view` (word `<PREFIX>~<WS8>/GRANT`, the deposit line) → `sign` → `deposit`; `unknown` → `grant.deposit_status`; fallback hand-off | never receives a secret |
| Terminal | `term.preview`, `term.open`, `term.attach {mode, take, from}`, `attach.cancel`, `run.stop` | §12.6.1 verification, §12.6.6 browser path, §12.6.7 xterm configuration |
| Worktrees | `worktree.list {check}`, `worktree.create`, `worktree.remove {force}` | destructive confirm for `force`; "Open terminal here" = `term.open {cwd:{kind:"worktree", …}}` |
| Records | `records.status` (owners, maintainers) | spawns the records child (one per workspace), shows the export warning, `auto` off switch only |
| Quick tasks | `quick.list {limits}`, existing `new`/`set`/sign paths | limits are a local setting |

Click-eligible versus other ops (G12): click with a word: `grant` with `deposit:true`, `addon.grant`. No signature,
dashboard-direct: `agent.start`, `run.stop`, `term.*`, `worktree.*`, the records child. Terminal only: `orch runner
enable`, `orch records remote private`, `orch records auto on`/`--push`, `orch records push --initial`.

**Rules and strings for the dashboard.**

- **Constants in the bundle** (client text, never host text): the D-P2-31 line "Dashboard execution: agents and
  terminals started here run as you. Anything that takes over this dashboard, or any agent it starts, can run
  commands and read your files and keys." (card, Start dialog, terminal header, whenever `runner.enabled`, whatever
  `terminals` says); the deposit line "The secret goes to your orch runner on this machine and is never shown"; the
  export warning "Push publishes every ticket in this workspace to <url>, restricted ones included."; the private
  remote line "orch trusts your word that only you read <url>."; "Output no longer kept"; "Some processes may still be
  running"; "Another tab or device is controlling this terminal" with "Take control"; "Take back"; "the runner changed; try
  again"; "Input paused"; "Input may not have arrived".
- **Idem:** the browser draws one idem per dialog opening (stable across retries), the server forwards it. After a
  timeout the dashboard reads `run.list` and retries with the same idem, never a new one. `launch.changed` reopens the
  dialog with a fresh preview; the dashboard posts exactly the preview's values plus `spec`.
- **Grant states:** `grant_state:"ended"` is not a run state: the agent's next write fails with `grant.expired` and its
  claims lapse (`grant_ended`); the dashboard offers Stop. Exit reasons `spawn_failed`, `lost`, `runner_restarted` are
  errors; `exited` and `stopped` are normal. `grant.list` shows runner grants with label `runner`; the dashboard offers
  "Forget the runner grant on this machine" (`grant.forget`) next to Revoke, and Revoke uses the sign result's `grant`
  in the `unknown` deposit case.
- **Records child:** fixed argv `python -I -m orch.records commit [--push] --workspace <id> --json` or
  `… auto off --workspace <id> --json`; environment `HOME`, `PATH`, `SSH_AUTH_SOCK`, `LANG`, and the orch state
  variables only; 60 s limit enforced by the dashboard. Output: one JSON line `{ok:bool, code?:str, message?:str,
  commit?:Sha, pushed?:bool, url?:str}`; exit 0 on success, the catalog exit of `code` otherwise. `url` and `message`
  are rendered through `visible.tsx`.
- **Worktree states:** `missing` and `moved`: offer Remove (the registration only) and Create; `unpinned`: Remove and
  retry Create; `stale_branch`: Remove and Create on the current branch; `dirty:null` shows "unknown";
  `worktree.in_use` offers "Stop the run"; `created_by` through `visible.tsx`.
- **Quick tasks:** one flow with two signatures until §9 item 12 is built, said in the UI.

### 12.12 Limits and errors

Limits are in §0.11 (rows marked v1.6) and errors in the v1.6 table of §0.6.

### 12.13 Groups H11a–H11f

| Group | Content | Acceptance (T0 hints) | Review |
|---|---|---|---|
| **H11a Runner core** | `orch.runner`, `orch runner enable/disable/status/stop/restart`, units, the runner socket and class `runner` (main-PID, generation, the host peer-UID check by the runner), `runner.hello/report/exit` and commands, trampoline, PTY, ring, stop and sweep with birth identity, cgroup before exec, fd closing, bounded drain, persisted run table and crash sweep, spawn dedupe, env allow-list and `pass_env` deny-list, the machine switch (D-P2-31), the term socket listener and `attach.allow/end`, the first-party manifests | a second runner connection and a non-main-PID runner are `peer.denied`; the runner refuses commands from a socket whose peer is not the host UID; the child env equals §12.4.3 (property test); a swapped cwd is `launch.cwd_changed`; a `setsid` escapee on macOS yields `cleanup:"incomplete"`, Linux `cgroup.kill` empties the cgroup; a reused pid is never signalled; a process outside the run is never signalled; a lost spawn ack yields one process; the ring never blocks 50 MiB of output with no viewer; host restart keeps runs and ends attachments; runner crash marks `lost`/`unknown` and sweeps; `restart` refuses with live runs; `orch runner stop RUN` and
`--all` work from the person's terminal with the host down and for a person no longer a member (no host call needed). T0 `tests/runner/test_trampoline.py`, `tests/runner/test_supervise.py`, `tests/host/test_runner_socket.py` | Opus |
| **H11b Terminals** | `term.preview`, `term.open/attach`, `attach.cancel`, `run.list/sub` with `rev`/`gone`, `run.stop`, capabilities, lease generation, input ids, gaps, resize rules; the dashboard server's WebSocket `Security` path (S1/S2) and the xterm list, as joint acceptance with orch-fa | only the run's person attaches (G, N, another person refused); an unused ticket never takes the lease and expires in 10 s; a replayed or other-generation ticket ends with `ticket_invalid`; `take` is needed against a holder; a retried input id types once; unacked input is not replayed after reconnect; replayed `ESC[6n` produces no input; a read viewer's resize is ignored and the writer's applies to all; a slow viewer gets `gap` and control frames still arrive; `attach.end` closes on person removal while the run continues; the host stays responsive (`human.prepare` round trip under 50 ms p99) while 8 runs flood output; WebSocket without Origin, with `Origin: null`, wrong Host, ticket after logout, slow or oversize first message, binary before the ticket are all refused; the canary corpus of §12.6.7 leaves clipboard, `window.open` and `document.title` untouched and the forwarded replies stay within the CPR/DA1 allow-list; the browser ticket is lazy (no host call before the WebSocket), a closed writer connection releases the lease at once, replay chunks (offset below `ready.offset`) produce no input, every end reason maps to its close code, `throttle` pauses without dropping input, and the dashboard server logs no terminal bytes or tickets. e2e `terminal_attach.spec` | Opus |
| **H11c Start agent and runner grant** | harness registry, `agent.preview/start` with `spec`, `context` mode, `subagent_model`, `term.preview` and `term.open {harness}`, POSIX-shell check, `grant.deposit` (P + secret, `label:"runner"`, `deposit_id`, forgotten ids), `grant.deposit_status/forget`, the click signer's deposit-only grant (`check_prepared` exception, word, View line, ≤ 12 h), `orch grant --runner`, `orch sign` show-or-runner | preview argv equals the spawned argv; a changed value between preview and start is `launch.changed`; a model starting with `-` or with a space is `launch.invalid`; a live claim refuses; a canary secret appears in no dashboard frame, host or runner log or result, only in the agent's environment; an agent depositing its own non-runner grant is refused; a forgotten id is `grant.forgotten`; a lost deposit ack yields `unknown`, never a false failure; the click signer refuses `grant` without `deposit:true`; a person-run harness (`term.preview`/`term.open`) has no `ORCH_GRANT`, no claim precheck and kind `shell`; the View's `deposit` field is set only for a deposit grant; idem retry of `agent.start` starts one process; fish as `$SHELL` is `harness.shell_unsupported`. e2e `start_agent.spec` (grant by click, start a stub harness that runs `orch status` and claims) | Opus |
| **H11d Worktrees** | `worktree.*`, runner git under the per-repo lock, host pins and generations, D58 and `tree.check` on the registered worktree, `orch worktree` in POSIX direct mode | create on the linked branch at `<repo>.worktrees/<WS8>-<KEY>`; no branch is `worktree.no_branch`; a branch checked out elsewhere is `worktree.branch_in_use`; re-registration between prepare and binding invalidates the request; a re-pointed `.git` file, a moved worktree or a relinked branch is `observe.unavailable` and never falls back; a verdict on a ticket worked in its worktree passes; dirty remove refuses without `force` and G cannot force; the branch survives removal; a planted `post-checkout` never runs; G only on its own claimed ticket, rechecked at execution; direct-mode tools refuse while a host serves. T0 `tests/host/test_worktrees.py` | Opus |
| **H11e Records** | `records.status/snapshot/export_check/note`, `orch.records` (commit, push, init, sync-index, auto, remote private), timer units, the dashboard child contract | a snapshot during appends commits only whole lines; `.state/` never committed, and a historical `.state/` file blocks push until fresh history; the written tree equals the manifest; the person's index is untouched unless it equalled HEAD; local `url.*.insteadOf`, `credential.helper`, `filter.*`, `core.sshCommand` or two URLs refuse and name the key; `ext::`/`file://` refused; a non-records commit in `tip..HEAD` refuses; divergence refuses without rebase or force; `MERGE_HEAD` refuses; push with convenience events and no private mark is `sign.convenience_readers`; a changed push URL after a private mark refuses; G gets `op.not_allowed`; auto mode commits only when pending and due. T0 `tests/test_records_sync.py`, `tests/host/test_records_ops.py` | Opus |
| **H11f Quick tasks** | `quick.list`, `orch quick add/list`, the manifest | `quick.list` equals `ticket.query {label:["quick"]}` for every viewer; counts `null` when unreadable; a failed label `set` is retried on the created ticket. T0 `tests/host/test_quick.py` | Codex |

Order: H11a → H11b ∥ H11d → H11c; H11e may start first; H11f any time. Everything is built behind the
off-by-default machine switch. The dashboard stream builds §12.11 against a stub of these ops.
