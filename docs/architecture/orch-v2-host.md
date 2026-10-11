# orch v2 P2: the host process (#325, #271, #287), v1

Status: Decided 11 Oct 2026 (draft by orch-52, Codex challenge, Opus tie-break on two points). The socket API contract §0 is v1 (provisional): changes go through the coordination log.

Built on `origin/develop` 908f2511. Decisions are numbered **D-P2-n**, each with a one-line reason. Numbers that
existed in v0 keep their meaning where the decision survived.

---

## 0. Socket API contract v1 (provisional)

This section is complete on its own: streams B (dashboard), C (mobile, from P3) and D (relay bridge) can build on it
without reading the rest. Changes go through the cross-stream log.

### 0.1 Transport and framing

- **Sockets.** One per workspace, `<run>/<workspace_id>/host.sock`, for every workspace op. One control socket,
  `<run>/control/host.sock` (directory 0700, host UID only), for `host.*` admin and bootstrap.
- **Frame.** A 4-byte unsigned big-endian length `n`, then `n` bytes, with `1 ≤ n ≤ 2 097 152`. A reader must handle
  partial and coalesced reads.
- **Payload.** The payload is strict UTF-8 JSON, one object per frame. Any of the following is `frame.invalid`:
  - invalid UTF-8;
  - duplicate keys;
  - `NaN` or `±Infinity`;
  - an integer outside ±2^53;
  - nesting deeper than 32;
  - a top-level value that is not an object;
  - `n = 0`.
- **Fatal errors.** A framing error, or any error that belongs to no request, gets one
  `{"t":"fatal","error":{…}}` frame. Then the side that detected it closes the connection.
- **Frame types:**
  - `hello`, `welcome`: the handshake. `hello` must arrive within 2 s.
  - `req`, `res`: a request and its response.
  - `cancel`: cancels a request.
  - `evt`: a pushed subscription item.
  - `fatal`: see above.
- **Request ids.** A string matching `^[A-Za-z0-9_-]{1,32}$`, unique among the connection's open requests. Reusing an
  open id is fatal.
- **Order.**
  - Responses may arrive in any order.
  - Writes from one connection run in the order they were received.
  - All writes to a workspace go through one worker queue.
  - A read may overtake a queued write.
- **Cancellation.** `{"t":"cancel","id":"r7"}`.
  - A queued request, a read or a `wait` stops with `cancelled` (exit 9, retryable).
  - A write that has started to commit is never undone. Its response is the real result, or `outcome.unknown`
    (exit 9). The client recovers the result with `outcome.get` (§0.5).
  - A timeout behaves the same way: it never implies a rollback.

### 0.2 Handshake, versioning and capabilities

```
→ {"t":"hello","proto":[1],"client":"orch-cli/2.0.0","caps":["ops/1","human/1","sub/1","blob/1","outcome/1"],
   "ops":{"status":1,"show":1,"task.done":2}}
← {"t":"welcome","proto":1,"host":"orch-host/2.0.0","host_uid":501,
   "workspace":{"id":"6f1c0d2e8b4a4e1f9c3d2a7b5e9f0c11","prefix":"DEMO"},
   "peer":{"uid":502,"class":"agent","person":null},
   "caps":["ops/1","human/1","sub/1","blob/1","outcome/1","query/1","diff/1"],
   "ops":{"status":1,"show":1,"task.done":2,"ticket.query":1,"…":1},
   "registry":"sha256:9f2c…e1",
   "limits":{"frame":2097152,"inflight":8,"timeout_ms":30000,"blob_bytes":67108864}}
```

- **`proto`** is the major version of framing and envelopes. If no major is shared, the host answers
  `host.version_mismatch` and closes.
- **`caps`** are named features with a version. **`ops`** maps each op to its contract version. A contract version is
  bumped only when the op's input, output or errors change incompatibly.
- **Unsupported ops.** If the client calls an op the host lacks, or one whose contract version differs from the
  client's, the request gets `op.unsupported` (exit 1). Other ops keep working.
- **`registry`** is a diagnostic digest only. Nothing is refused because of it.
- **Unknown keys.** Clients ignore keys they do not know in results and in `evt` frames. The host refuses unknown keys
  in `args`: op schemas are `additionalProperties:false`, because inputs are security-relevant.

### 0.3 Requests, results and errors

```
req     {"t":"req","id":"r1","op":"<name>","args":{…},
         "auth":{"grant":"gr_…","session":"s_…"},                 // both optional
         "opts":{"dry_run":false,"idem":"a_<ULID>","timeout_ms":30000},
         "files":{"<arg>":{"b64":"…"} | {"blob":"b_…"}}}          // file contents; never paths
res ok  {"t":"res","id":"r1","ok":true,"result":{"data":…,"key":…,"seq":…,"cursor":…,"hints":[…],"lines":[…],
         "duplicate":false,"exit":0,"head":null}}
res err {"t":"res","id":"r1","ok":false,"exit":3,
         "error":{"code":"human_only","message":"approve: human only","hint":"orch ask or orch wait","retryable":false}}
```

- **`result`** is `orch.ops.Result` in full. The CLI renders text from it with its own templates, so P1's golden output
  is unchanged.
- **`error`** is exactly the error body of F1 §10.4 item 4. Codes come from `orch.ops.errors` and §10.4a. Each op's
  `errors` list declares which codes it can return (core §3).
- **`retry_after_ms`** is a member of `error` when the code is `rate.limited` or `host.restarting`.
- **`opts.idem` is required on every write.** The client makes one id per logical call and reuses it on retry.
- **`opts.timeout_ms`** defaults to 30 000 and may go up to 600 000. `wait` is exempt from the default: its own
  `timeout` argument applies (default 540 s, at most 3600 s).

### 0.4 Peer classes and identity

The host reads the peer's UID from the kernel. It never takes a UID from a message.

| Class | Who | May call |
|---|---|---|
| `person` | UIDs mapped to a person in the host's registry (on a Mac: the human's own UID, i.e. the human's CLI and the dashboard addon) | reads; `human.*`; `sub.*`; `blob.*`; `outcome.get`; `whoami`; `host.status`; agent ops only with `auth.grant` (they then run as that agent) |
| `agent` | the workspace's agent UIDs (#287) | reads except `doctor` and `instructions.sync`; `agent` and `unattended` ops under the grant rules; `sub.*`; `blob.*`; `outcome.get`; `whoami` |
| `worker` (H10) | the worker UID | `job.next`, `job.done`, `whoami` |
| `bridge` (P3, reserved) | the relay bridge client | defined in P3. It has no person mapping, and every remote person request carries that person's own signature. |
| anything else | | one `fatal` `peer.denied`, then the host closes the connection |

- **Who a call acts for:**
  - an agent is the person who issued its grant;
  - a person-class reader is the person mapped to its UID (`peers.<uid>.person` in the registry; never derived from a
    key file);
  - a person *event* names its device in `human.prepare`, and the host checks that this device belongs to the mapped
    person (§0.6).
- **Sessions.** `auth.session` is a label, not authentication. Session notes, dedup records and the stop rule are
  keyed by `(principal, session)`. The principal is the grant id, or `uid:<n>` when there is no grant.
- **Invalid grants on reads.** A read with no grant, or with one that does not check out, runs as "no grant" and sees
  only `workspace` tickets (F1 §10.7). A write is refused with `grant.required` / `grant.expired`. A malformed grant
  is refused on writes and ignored on reads (§10.4 item 12).
- **Secrets.** The grant secret goes in `auth.grant` only. If a grant-shaped string appears anywhere in `args`, the
  request is refused with `grant.secret_in_args`.

### 0.5 Idempotency and outcomes

- **Namespace.** Outcome records are keyed by `(workspace, principal, idem)`.
- **Binding.** A record is bound to `sha256(cj({op, args_normalised, file_digests}))`. The same `idem` with a different
  binding is `idem.conflict` (exit 5).
- **States.** A record is `pending`, `committed` (it stores the response, redacted), `failed` (it stores the error) or
  `partial` (an `apply` or a composite whose first events landed: it lists their `seq`s, and a retry is refused as in
  P1).
- **Retention.** Records live in host-owned `<state>/hosts/<ws>/outcomes/`, survive restarts and are kept for 24 h.
  When a completed record expires, a tombstone (key, binding, state) stays for 7 days. A late retry then gets
  `idem.expired`, never a second append.
- **Recovery.** `outcome.get {idem}` returns the stored `res`, `{"state":"pending"}`, or `outcome.unknown_idem`. The
  F1 §10.4 item 9 session dedup (15 minutes, `duplicate:true`) stays on top as the CLI-level behaviour.

### 0.6 Operations

**Registry ops.** All 54 registry operations of `orch.ops` are served, with the same names, `input` schemas, handlers
and `Result`. That covers `status`, `show`, `list`, `search`, `next`, `inbox`, `wait`, `task.*`, `artifact.*`, `new`,
`claim`, `release`, `handoff`, `submit`, `set`, `section.set`, `ac.*`, `log`, `ask`, `apply`, `check`, `doctor`,
`instructions.*` and `addon.list`.

- `who: human` ops are **not** callable as a plain `req`: they give `human_only` and go through `human.*`.
- `init` and `import.v1` are client composites (§3, D-P2-9). `help` and `describe` are answered by the client and
  also served.

**New ops in v1:**

| Op | Args → `data` | Class |
|---|---|---|
| `whoami` | `{}` → `{peer:{uid,class}, person, grant:{id,until,verbs,scope}\|null, session, tier:"A"\|"B"}` | all |
| `ticket.query` | `{status?:[…], type?:[…], label?:[…], assignee?, owner?, parent?, epic?, q?, updated_since?, needs_me?:bool, sort?:"updated"\|"priority"\|"key", limit?:1–200 (50), page?}` → `{rows:[{key,uid,title,type,status,priority,size,labels,assignees,owner,parent,epic,claim:{session,agent}\|null,gates:{<gate>:"open"\|"approved"\|"n/a"},needs:[kind],updated_at,head_seq}], next_page:string\|null, total:int, sub_cursor}` | person, agent |
| `needs` | `{person?:<self only, or any for owners>}` → `{items:[{kind:"question"\|"approve"\|"no_eligible"\|"acknowledge"\|"addon", key, uid, ref, detail, who:[person]}], sub_cursor}` | person, agent |
| `diff` | `{ref, repo?, context?:0–10 (3), max_bytes?:≤1 MiB (256 KiB)}` → `{sources:[{repo,branch,base_sha,head_sha}], files:[{repo,path,status,add,del,binary}], unified:string, truncated:bool}` | person, agent |
| `artifact.get` | `{ref, name, offset, length?:≤786432}` → `{b64, size, digest, last}` | person, agent |
| `workspace.show` | `{}` → `{id, prefix, name, genesis, epoch, wsk_fingerprint, members_v}` | person, agent |
| `member.list` | `{}` → `{members:[{person,name,role,devices:[{id,label?,auth}]}]}` | person, agent |
| `session.list` | `{active?:bool}` → `{sessions:[{session,agent,person,grant_id,claims,leases,last_at}]}` | person; agent: its own person's only |
| `human.prepare` | `{op, args, signer:{device, auth}, handoff?:bool, dry_run?:bool}` → `{request:{id, code\|null, expires_at}, workspace_id, log, event, material, review_digest}` | person |
| `human.submit` | `{request_id, event}` → the op's Result | person |
| `human.pending` | `{code?}` → `{requests:[{id, code, op, key, created_at, expires_at}]}` (own person only) | person |
| `human.cancel` | `{request_id}` → `{}` | person |
| `blob.put` | `{blob?, offset, b64, last, sha256}` → `{blob, size}` | all |
| `blob.drop` | `{blob}` → `{}` | all |
| `sub.open` | `{topics:["workspace"\|"tickets"\|"needs"\|"ticket:<ref>"], after?:cursor}` → `{sub, cursor}` | person, agent |
| `sub.close` | `{sub}` → `{}` | person, agent |
| `outcome.get` | `{idem}` → stored `res` \| `{state:"pending"}` | all |
| `host.status` | `{}` → `{version, uptime_s, workspace, state:"ok"\|"degraded"\|"unavailable", tier}` (agents: `version` and `tier`) | all |
| control socket: `host.list`, `host.register`, `host.unregister`, `host.wsk_create`, `host.restart` | see §1 | host UID |

**Person versions of agent ops (signed).** `new`, `set`, `section.set`, `ac.add`, `ac.edit`, `log`, `ask`,
`artifact.add` and `artifact.replace` can be called by a person through `human.prepare {op:"new", args:{…}}`. This is
allowed exactly when every event type the op emits allows actor P or A (`orch.ops.actors.allows("human", t)`). F1
already allows it: an A type may be appended by a signed person. Other ops get `op.not_allowed`.

**`move` (provisional; needs the F1 amendment H0).** A person op, owner or maintainer, through `human.prepare`:
`{ref, to:"backlog"|"open", reason?}`. It emits a new person event `ticket.moved {from, to, reason?}`.

- Allowed from `backlog`, `open`, and from `in_progress` with no live claim.
- Never into `in_progress`, `testing`, `done` or `closed`: those statuses come from `claim`, `submit`, verdicts and
  `close`/`reopen`.
- The dashboard's other moves map to those ops.

**Reserved, not served until F1 defines their events:** `policy.set`, `settings.set`, `comment`, `ticket.discard`,
`artifact.search`, `view.save`, `view.delete`. Dashboard-only data such as saved views belongs in the dashboard addon's
own storage, not in core.

**`human.*` in detail (D-P2-15, D-P2-21, D-P2-22).** The host never prompts and holds no device key. Every person event
is prepared by the host, signed by a client, and submitted.

- **`human.prepare` result:**
  - `event`: the unsigned canonical envelope, with `id`, `actor:{kind:"person",id,device}`, `auth`, `hash_v`,
    `roster_v`, `based_on`, `gate_gen`, `type` and payload;
  - `material`: the structured input from which the signer recomputes every bound hash:
    - for gates, `orch.model.gates.gate_input`: section texts, criteria, tasks, links, `source_sha`, artifacts with
      their digests, policy and people;
    - for answers, the question;
    - for other ops, the event payload itself;
  - `review_digest`: `sha256(cj(material))`.
- **What the signer must do:**
  1. recompute the gate or question hash from `material` and compare it with the hash in `event`;
  2. render its own review from `material`, never from host text;
  3. get the person's confirmation;
  4. build the signing bytes itself (`identity.sign_person_event`) and show the prompt fields from those bytes;
  5. sign, and keep `material` frozen from confirmation to signature.
- **Binding.** A request is bound to `(workspace, mapped person, signer.device, op, review_digest)` and is valid for
  10 minutes. Expiry is checked again at append, under the lock.
- **`human.submit`.**
  - It compares `cj(event without sig)` with the prepared event. A difference is `human.request_mismatch`.
  - The host verifies the signature, runs the prepared `precommit` (git refs re-read under the lock, D58), and
    re-judges with the model. Anything stale gets `gate.stale`, `members.stale` or `question.stale`.
  - Submit and cancel are serialized per request.
  - A completed request leaves a tombstone holding its result. A repeated submit gets that result with
    `duplicate:true`, never "expired".
  - Blobs the request refers to are kept with the request.
- **Hand-off.** `handoff:true` returns an 8-character code (40 bits). `human.pending {code}` finds only the same
  person's requests, and a person may make at most 5 failed lookups per minute.
- **No separate `expect` argument is needed.** The signed event carries the gate hash, `gate_gen`, `source_sha` and the
  question hash, and the signer recomputes each from the material it displays. The signature therefore binds exactly
  what the person saw. **The dashboard renders its review from `material`, never from its cached ticket view.**
- **`grant`.** The client draws the secret and passes `secret_hash` in `args`. The signer checks that the event's
  `secret_hash` equals the hash of its own secret, and shows `ORCH_GRANT=…` only after the `human.submit` result
  confirms the commit.

### 0.7 Subscriptions

- **Principal.** `sub.open` keeps the caller's principal: UID class, mapped person, grant id.
- **Revalidation before every delivery** (queued items too):
  - the grant must still be active (not expired or revoked), and the person still a member;
  - each item's ticket must still be visible.

  An item that fails is dropped. If the grant has ended, the host sends `{"t":"evt","sub":…,"end":"grant.expired"}` and
  closes the subscription.
- **Items** carry metadata only: `{jpos, log, key, uid, seq, ws_seq, type, at, actor:{kind,id}}`. Clients refetch
  content with ops.
  - A ticket that becomes visible yields `{…,"type":"visibility.changed"}`.
  - A ticket that becomes invisible yields `{jpos, uid, "gone":true}` (the subscriber already knew the uid).
  - The `needs` topic yields `{jpos, "type":"needs.changed"}`.
- **Cursors** are opaque strings of at most 256 bytes. They are scoped to the workspace, the principal and the history
  epoch (the genesis plus the last `restore`) and point into the host's append journal (`jpos`, a monotonic position
  in the merged order).
- **Opening.** `sub.open` registers the subscription and reads the journal position in one step on the workspace
  worker. With `after`, it first replays items after that cursor, then goes live, with no gap.
- **Ordering.** Items arrive in `jpos` order. Duplicates happen only after a reconnect with an older cursor, and clients
  dedupe by `jpos`.
- **Invalidation.** A cursor that is too old (outside the 10 000-entry journal), has the wrong scope or comes from
  another epoch gets `sub.reset`.
- **Overflow.** A subscription queue holds at most 256 items or 256 KiB, and a slow reader may lag at most 30 s. Past
  that, the host sends `{"t":"evt","sub":…,"invalidated":"overflow"}` and closes the subscription. The client takes a
  snapshot (`ticket.query`, `needs` or `show`; each returns `sub_cursor`) and opens again with `after` set to that
  cursor.

### 0.8 Files and blobs

- **The host never opens a path a client names.** File contents come inline (`files.<arg>.b64`, up to 512 KiB) or as a
  blob. Any argument that looks like an unresolved path gets `blob.invalid`.
- **Blob uploads.**
  - Offsets must be contiguous from 0, in chunks of at most 768 KiB.
  - `last:true` must carry the `sha256` of the whole blob, and after that the blob cannot change.
  - A blob is used once, then deleted.
  - Unused blobs are deleted after 10 minutes, unless a prepared request holds them.
  - Blobs survive a reconnect by the same principal.
  - Limits: 64 MiB per blob, 256 MiB per principal, 1 GiB per host.

### 0.9 Limits

| What | Limit |
|---|---|
| frame | 2 MiB |
| bytes per connection | 64 MiB/min in each direction |
| connections | 32 per UID |
| requests in flight | 8 per connection |
| subscriptions | 8 per connection, 64 per UID |
| pending human requests | 20 per person |
| agent request rate | token bucket of 20/s, burst 100 per agent UID |
| workspace fairness | round-robin across principals on the worker queue |
| idle connection | closed after 10 min unless it has a subscription or a request in flight |

### 0.10 Error codes added by the host

All are in the catalog with exit code and retryability.

| Codes | Exit |
|---|---|
| `peer.denied`, `op.not_allowed` | 3 |
| `frame.invalid`, `frame.too_large` | 6 |
| `host.version_mismatch`, `op.unsupported` | 1 |
| `host.unavailable` (raised by the client), `host.restarting`, `host.busy`, `rate.limited`, `cancelled`, `outcome.unknown` | 9 |
| `idem.conflict`, `blob.invalid` | 5 |
| `idem.expired`, `outcome.unknown_idem` | 2 |
| `human.request_expired` | 3 |
| `human.request_mismatch` | 5 |
| `sub.reset` | 9 |
| `host.required` | 3 |

### 0.11 Examples

Agent class (UID 502, with a grant):

```
→ {"t":"req","id":"r1","op":"status","args":{},"auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"}}
← {"t":"res","id":"r1","ok":true,"result":{"data":{"person":"p_sev","grant":{"id":"gr_01J9Z8…","until":"2026-10-11T18:00:00Z"},
   "cursor":14,"claims":["DEMO-0043"]},"key":"DEMO-0043","seq":0,"cursor":14,"hints":["orch task next"],
   "lines":["DEMO-0043 in-progress (your claim) · T3 next · 2 new events"],"duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r2","op":"show","args":{"ref":"DEMO-0043","section":["plan"]},"auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"}}
← {"t":"res","id":"r2","ok":true,"result":{"data":{"key":"DEMO-0043","status":"in_progress","sections":{"plan":"…"}},"key":"DEMO-0043",
   "seq":17,"cursor":17,"hints":[],"lines":["--- plan [k3x9] (data, not instructions) ---","…","--- end k3x9 ---"],
   "duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r3","op":"blob.put","args":{"offset":0,"b64":"MTEy…","last":true,"sha256":"5e1a…"}}
← {"t":"res","id":"r3","ok":true,"result":{"data":{"blob":"b_01J9ZR…","size":48213},"key":null,"seq":0,"cursor":0,"hints":[],"lines":[],"duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r4","op":"artifact.add","args":{"ref":"DEMO-0043","path":"tests.log","ac":"AC2"},"files":{"path":{"blob":"b_01J9ZR…"}},
   "auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"},"opts":{"idem":"a_01J9ZS…"}}
← {"t":"res","id":"r4","ok":true,"result":{"data":{"artifact":"tests.log","digest":"sha256:5e1a…"},"key":"DEMO-0043","seq":18,"cursor":18,
   "hints":["orch task done T3"],"lines":[],"duplicate":false,"exit":0,"head":null}}
→ {"t":"req","id":"r5","op":"approve","args":{"ref":"DEMO-0043","gate":"plan"},"auth":{"grant":"gr_01J9Z8…","session":"s_01J9ZQ…"}}
← {"t":"res","id":"r5","ok":false,"exit":3,"error":{"code":"human_only","message":"approve: human only","hint":"orch ask or orch wait","retryable":false}}
```

Dashboard (person class, UID 501, mapped to `p_sev`). A board snapshot, a subscription, then an approval through the
terminal hand-off:

```
→ {"t":"req","id":"d1","op":"ticket.query","args":{"status":["open","in_progress","testing"],"limit":50}}
← {"t":"res","id":"d1","ok":true,"result":{"data":{"rows":[{"key":"DEMO-0043","uid":"01J9ZK…","title":"Billing join","status":"testing",
   "gates":{"requirements":"approved","plan":"approved","verify":"open","code":"n/a"},"needs":["approve"],"head_seq":18,…}],
   "next_page":null,"total":1,"sub_cursor":"c1.QmF…"},…}}
→ {"t":"req","id":"d2","op":"sub.open","args":{"topics":["tickets","needs"],"after":"c1.QmF…"}}
← {"t":"res","id":"d2","ok":true,"result":{"data":{"sub":"sub_1","cursor":"c1.QmF…"},…}}
← {"t":"evt","sub":"sub_1","cursor":"c1.QmG…","items":[{"jpos":5121,"log":"01J9ZK…","key":"DEMO-0043","uid":"01J9ZK…","seq":19,"ws_seq":31,
   "type":"task.done","at":"2026-10-11T15:02:11Z","actor":{"kind":"agent","id":"claude"}}]}
→ {"t":"req","id":"d3","op":"human.prepare","args":{"op":"verdict","args":{"ref":"DEMO-0043","outcome":"pass"},
   "signer":{"device":"d_7Q…","auth":"passphrase"},"handoff":true}}
← {"t":"res","id":"d3","ok":true,"result":{"data":{"request":{"id":"hr_01J9ZT…","code":"7F3K-Q2MA","expires_at":"2026-10-11T15:14:00Z"},
   "workspace_id":"6f1c…","log":"01J9ZK…","event":{"v":2,"id":"01J9ZT…","type":"verdict.given","actor":{"kind":"person","id":"p_sev","device":"d_7Q…"},
   "auth":"passphrase","hash_v":1,"roster_v":4,"based_on":"sha256:…","gate_gen":2,"outcome":"pass","hash":"sha256:81ab…",
   "source_sha":[{"repo":"https://github.com/acme/energy-dbt","branch":"t/DEMO-0043","sha":"4be1…"}]},
   "material":{"gate":"verify","input":{…gate_input…}},"review_digest":"sha256:…"},…}}
   (the person runs `orch sign 7F3K-Q2MA`; that CLI calls human.pending, recomputes, reviews, signs, and then:)
→ {"t":"req","id":"c1","op":"human.submit","args":{"request_id":"hr_01J9ZT…","event":{…same…,"sig":"MEUCIQ…"}},"opts":{"idem":"a_01J9ZU…"}}
← {"t":"res","id":"c1","ok":true,"result":{"data":{"gate":"verify","outcome":"pass"},"key":"DEMO-0043","seq":20,"cursor":20,"hints":[],
   "lines":[],"duplicate":false,"exit":0,"head":null}}
```

### 0.12 Changelog v0 → v1

1. String request ids; `fatal` frames; strict-JSON rules; `cancel`; `outcome.unknown`; response reordering and write
   ordering defined.
2. `opts.idem` is required on writes, namespaced by workspace and principal and bound to op, args and file digests.
   Outcome records with tombstones; new `outcome.get`, `idem.conflict` and `idem.expired`.
3. The exact `registry` match is replaced by `caps` plus per-op contract versions (`op.unsupported`). `registry` is now
   a diagnostic.
4. Peer classes `worker` and `bridge` (reserved) added. The person is an explicit UID→person map. `human.prepare` takes
   `signer:{device,auth}`. Sessions are keyed by principal.
5. Reads with an invalid grant behave as in F1 §10.7 (workspace tickets only) instead of being refused.
6. `human.prepare` returns structured `material` and `review_digest`; the signer recomputes hashes and renders its own
   review. Submit compares canonical JSON, keeps tombstones and scopes hand-off codes. The dashboard uses
   prepare → sign → submit; there is no host-side prompt.
7. Subscriptions keep their principal and revalidate before delivery. Opaque scoped cursors; the `needs` topic;
   overflow and stale cursors invalidate (snapshot plus cursor); visibility transitions defined.
8. New reads: `ticket.query`, `needs`, `diff`, `artifact.get`, `workspace.show`, `member.list`, `session.list`.
   `move` (provisional). Person versions of `new`, `set`, `log`, `ask` and others through `human.*`. `blob.drop`.
9. Control socket for `host.*` admin and bootstrap (`host.wsk_create`).
10. Blob rules (contiguous offsets, final digest, single use, quotas) and the full limits table.

---

## 1. Process model

**D-P2-1. One host per host UID serves every registered workspace, with one worker thread per workspace.**

- The worker gives an order to writes and keeps a single owner for the mutable `Store`. (v0's reason was wrong:
  `FileLock` already shares a `threading.RLock`, so two threads cannot both pass it.)
- **Registration.** Roots are canonicalised with `realpath`. A duplicate workspace id or root is refused.
- **Workspace states.** Each workspace is `ok`, `degraded` (reads only, for example an unacknowledged invalid event) or
  `unavailable` (missing, chain broken at genesis, unsafe modes). Ops on an unavailable workspace get
  `host.unavailable` naming the cause. v0 promised read-only service here, which a broken workspace cannot give.

**D-P2-2. The OS service manager starts the host, never a CLI call.** If a CLI call could spawn the host, an agent-UID
call would start a host as the agent.

| Platform | Service | Run directory |
|---|---|---|
| macOS | LaunchAgent `io.orch.host` (`RunAtLoad`, `KeepAlive`) | `/Users/Shared/orch/run/<host uid>`, created by the setup helper (H3c) |
| Linux | systemd user or system unit | `/run/orch/<host uid>`, provisioned by `tmpfiles.d` from the privileged setup. A user unit's `RuntimeDirectory` lives under `$XDG_RUNTIME_DIR`, which agents cannot traverse. |
| dev and tests | `orch host run --foreground` | `<state>/run` (no agent UIDs) |

**D-P2-3. Every outcome is recoverable.** The store recovers torn appends. The host adds the outcome records of §0.5,
so a lost response, an interrupted composite or a human submit is recovered by `idem`, never repeated.

**D-P2-4. Upgrades.**

- The service runs a versioned, immutable release. `orch host install` copies it to
  `~/.local/share/orch/releases/<version>`, and the host imports every `orch` module at start.
- An upgrade is: install the new release, then `orch host restart` (control socket) or the service manager's restart.
  A running host is never changed under its feet.
- **Drain.**
  - New requests get `host.restarting` with `retry_after_ms`.
  - Writes in flight finish; there is no forced deadline inside a commit.
  - Parked `wait`s and subscriptions end with `host.restarting`, not with a `timeout` result.
  - Lost responses are recovered with `outcome.get`.
- Version skew is handled by capabilities (§0.2).

**D-P2-5. Several OS users, with an explicit UID map.** The host's registry (`<state>/host/workspaces.json`, 0600)
holds, per workspace:

```json
{"workspace_id":"6f1c…","root":"/Users/sev/acme","host_uid":501,"group":"orch-6f1c0d2e8b4a",
 "peers":{"501":{"class":"person","person":"p_sev"},"502":{"class":"agent"},"503":{"class":"worker"}}}
```

- A person-class UID reads as its mapped person. A person mapping is only accepted if that person is a member and has
  a valid device in the roster.
- **Machines without agent UIDs** (dev, or a Mac before H3c) are tier A: every connection is person class, a request
  with `auth.grant` runs in agent mode, and `human.*` with a grant present is `human_only`. `whoami` reports
  `tier:"A"`.

## 2. The socket

**D-P2-6. One socket per workspace in `<run>/<ws>/` (0750, group `orch-<ws 12 hex>`), the socket itself 0660, and
`<run>` owned by the host UID.** File modes keep other users and other workspaces' agents out before the peer check
runs.

- **Ancestors.** Every ancestor of `<run>` must be owned by root or by the host UID, and nobody else may write it. A
  sticky root-owned directory such as `/Users/Shared` or `/run` is allowed when the next component belongs to the host
  UID. No ACL entry may grant write, `add_subdirectory` or `delete_child` to anyone else (macOS: `acl_get_link_np`).
  The host checks this at start and again every 60 s, and refuses (or stops serving) on a violation.
- **Binding.**
  1. Open each component with `O_NOFOLLOW|O_DIRECTORY` and `fstat` it.
  2. Take `<run>/<ws>/lock`.
  3. `fchdir` into the checked directory fd and bind the **relative** name `host.sock`, under umask 0o177. This happens
     before the host starts any threads, so the bind uses the checked descriptor.
  4. `chmod` 0660 and `chown :group`, then `listen`.

  Stale sockets are removed only while the lock is held and only after a connect fails.
- **Path length.** The UTF-8 encoded absolute path must stay at or under 100 bytes (clients connect by absolute path).
- **Groups.**
  - The setup helper creates the group with a recorded gid and refuses if it already exists with an unknown gid or
    members.
  - The host UID is a member, which `chown` needs.
  - After a membership change the helper restarts the host and the agents' sessions, because group lists are fixed per
    process.
- **Peer credentials, both ways.** `SO_PEERCRED` on Linux, `getpeereid` (ctypes) on macOS.
  - The host refuses an unmapped UID before reading any byte.
  - The client accepts the server only if its UID and `welcome.workspace.id` match a **trusted pin**:
    - for a person-class client, its own registry (same UID as the host);
    - for an agent-class client, `/Library/Application Support/orch/pins/<agent uid>.json` (macOS) or
      `/etc/orch/pins/<uid>.json` (Linux), which is root-owned and written by the setup helper.

    `config.json` only names the workspace id to look up; it is never trusted.
- **The credential is a snapshot of the connecting process.** A descriptor passed on to another process keeps that
  snapshot. That is acceptable between processes of the same UID. **No privileged process forwards connections for
  others**: the dashboard never proxies requests for another principal, and the host never passes client descriptors
  on.
- **Real UID tests.** Two-process tests with real UIDs run in CI (Linux container, `useradd`) and on the dev Mac with a
  test user, in addition to unit tests with fake credentials.

**D-P2-7. There is no TCP listener in the host. The dashboard is a socket client.** The dashboard addon is a FastAPI
process under the human's UID. It serves `127.0.0.1:<random port>` and is person class on the socket. FastAPI stays out
of the key-holding process, and the host's attack surface stays one peer-checked socket.

Required on the HTTP side (stream B):

- a one-time launch token in the URL fragment, exchanged by a POST with an `Origin` check for an
  `HttpOnly; SameSite=Strict` cookie;
- `Host` must be `127.0.0.1:<port>` or `localhost:<port>`;
- an `Origin` check on every non-GET request, and GET requests have no side effects;
- a strict CSP;
- SSE fed by `sub.open`.

A review in the browser never stands in for the signer's review (§4).

**Limit:** on a Mac the dashboard runs as the same UID as the host. A dashboard that runs attacker code can read the
WSK. This is accepted under §4 ("the human's machine is trusted"), and Q3 would close it.

## 3. Dispatch

**D-P2-8. Frames are length-prefixed JSON with orch's own envelope** (§0.1). The result and error envelopes are
already the contract. JSON-RPC's integer codes add nothing, and the length prefix lets the host enforce the size limit
before parsing.

**D-P2-9. The op list is the registry, and handlers get explicit capabilities.** P1 handlers read `ctx.env`, open
paths, run subprocesses and sign. Before the host serves them, H3a gives `Context` a capability object:

| Capability | What it is |
|---|---|
| `store` | the worker's store |
| `files` | client-supplied bytes only |
| `git` | the content-addressed reader (D-P2-19) |
| `clock` | the time source |
| `exec` | none in the host, a subprocess runner in direct mode |
| `signer` | none in the host, custody in direct mode |

Handlers lose direct access to the environment, paths and subprocesses. A `sys.addaudithook` test in host mode fails on
any `open` outside the workspace root and state directory, and on any `subprocess`, `os.exec*` or `os.posix_spawn`.

- The check-and-run core of `cli.main.run` moves into `orch.host.dispatch` and serves both modes. That covers input
  validation, the who-check, the grant check, the stop rule, dedup and declared errors.
- **Client composites:**
  - `init`: the person's TTY steps, then `host.wsk_create` (control socket; the WSK is made in host custody, and only
    `wsk_pub` comes back), then the delegation and genesis are signed locally and sent with `human.submit`;
  - `import v1`: plan via `import.v1 --dry-run` on the host, review on the TTY, one unlock signing the batch locally,
    then `human.submit` per event with `idem`;
  - `task done --run`: D-P2-19.
- **`--dry-run`** on any op runs `model.preview` on the host, appends nothing and creates no human request.

**D-P2-10. Authentication per call, with the F1 reads rule** (§0.4). The redaction context, meaning the secret and
the parts derived from it, is kept for the whole request after the `auth` member is dropped. Every result, error, audit
line and outcome record is redacted with it.

**D-P2-11. The host never opens a client-named path** (§0.8). Registered repository roots are taken from the signed
`settings.repos`. They are opened with `O_NOFOLLOW` at every component below the root, and nothing outside them is
read.

**D-P2-12. Subscriptions** (§0.7). The journal is in memory, with the last 10 000 entries rebuilt from the logs at
start. Appends the host did not make, by an unmanaged CLI, cannot happen for a registered workspace (D-P2-18), so there
is no need for a stat poll.

**D-P2-13. Limits** (§0.9). Audit lines go to `<state>/hosts/<ws>/audit.jsonl` (0600, rotated at 10 MiB): time, UID,
class, op, principal, session, code, duration, and never a secret.

**D-P2-14. Versioning by capabilities** (§0.2). The registry digest is a diagnostic. Older clients are kept honest by
recorded fixtures (H7).

## 4. Human signing

**D-P2-15. The client signs. The host never prompts and holds no device key.** A daemon has no terminal, and a host
that could prompt would let any peer raise prompts on the person's screen. C7's rules (review the gated content,
prompt from the signing bytes, a fresh signature every time) stay with the signer, which recomputes every bound hash
from `material` (§0.6).

**D-P2-16. The grant secret is drawn by the client.** The host only ever sees `secret_hash` when a grant is issued. A
later call made under the grant does send the secret, in `auth.grant`.

**D-P2-21. The signer recomputes hashes from structured material and renders its own review** (Codex Q7, adopted now).
The host is trusted, but a host bug must not be able to make the person sign content they did not see.

**D-P2-22 (manager ruling, orch-89). The Touch ID signer H9 is in P2 and must land before the cutover (D44).**

- **Order:**
  1. the `human.prepare` → sign → `human.submit` contract first (H5a);
  2. the terminal hand-off `orch sign CODE` as the first signing path (H5b);
  3. H9 as its own group.
- H9 blocks neither the socket core nor the dashboard's early milestones.
- The invariant stays: the host never prompts and holds no device key.
- **Reconciled with stream B:** the dashboard's earlier idea that the host signs or prompts is dropped. The dashboard
  calls `human.prepare`, shows a preview from `material`, and hands the request to a signer: in P2 first `orch sign`,
  then the H9 helper.

| Signer | Review | Prompt |
|---|---|---|
| CLI (`orch approve`, `orch sign CODE`) | on `/dev/tty`, rendered from `material`, confirmation word, `tcflush` (C7) | passphrase, fields from the signing bytes |
| H9 `orch-signer` (macOS, Swift, Secure Enclave P-256, auth `secure-enclave`) | its own native window, rendered from `material` after it recomputes the hashes. The browser preview is only a preview. | Touch ID; the LocalAuthentication reason is built from the signing bytes; non-person labels are refused |

WebAuthn (D64) needs a new signature contract and stays out of P2.

## 5. WSK custody

**D-P2-17. The WSK stays a file-tier key at `<state>/hosts/<ws>/keys/wsk` (0600 in a 0700 directory), owned by the host
UID. P2 adds no keychain.**

- A keychain ACL binds to the `python` binary, which every process of the same UID can run, and a daemon would run
  into unlock prompts. The UID is the boundary.
- This amends the macOS row of orch-v2.md §5.5 (H0).
- **Checks.**
  - `keys/`, `hosts/<ws>/`, the pins, the noted revocations, `outcomes/`, `audit.jsonl` and the workspace's
    `events/`, `tickets/` and `.state/` must be private to the host UID, or (in the workspace) readable by the group
    but writable only by the host.
  - Their ancestors must not be replaceable by anyone else: owned by root or the host UID, no write for group or other
    unless sticky, and no ACL or link tricks.
  - Ordinary readable ancestors such as `/` and `/Users` are fine. Processes of the same UID stay trusted.
- **For the CLI:** in host mode it opens neither the WSK nor the store. It keeps only the person's device key, for
  signing. With agent UIDs (tier B), the P1 limits N4, N14 and O7 close, and session notes can no longer be forged.

## 6. Migration and compatibility

**D-P2-18. Host ownership is recorded outside the workspace, and there is no silent fallback.**

| Workspace | CLI behaviour |
|---|---|
| registered with a host (in the person's registry, or in a pin for an agent UID) | must use the host. Host down, timeout or version error: `host.unavailable`, `host.version_mismatch` or `op.unsupported`, never direct mode. |
| unmanaged (no registration, no pin) | direct mode exactly as P1 (CI, tests, the owner's C11 workspace, Windows) |

- **Adoption** (`orch host register <path>`, control socket):
  1. take the exclusive ownership lock `<state>/hosts/<ws>/owner.lock`. Every direct-mode CLI holds it shared for its
     whole command, so this waits for running commands to finish;
  2. write the registration, start serving, then release the lock.

  A direct-mode CLI checks the registration again under its shared lock and refuses with `host.required`.
- **`orch host unregister`** stops serving, removes the registration and so restores direct mode explicitly.
- On a VPS, registering also moves `hosts/<ws>/` and `.state/` to `orch-host-<ws>` and sets the modes.
- `ORCH_HOST` is gone: the registration decides, so neither an agent nor an `.envrc` can choose the mode.
- The format and the logs do not change.

## 7. Security model and threats

| Threat | Answer |
|---|---|
| Another local user | Directory and socket modes, peer map checked before any byte is read, private key directories. |
| An agent under its own UID | Agent class: closed op list, no `human.*`, no admin. Reads follow its grant's person. It cannot read keys or host state. Tier B: the store files can be written only by the host. |
| Workspace code run by the host | Never (D-P2-19, D-P2-20). An audit-hook test enforces it. |
| Client-named paths, env, cwd | Never used (D-P2-10, D-P2-11). |
| Malicious addon | Runs in the worker under the worker UID and returns proposals only. The dashboard is person class: it can read and create requests, but it cannot sign, because the signer recomputes and reviews. |
| Socket hijack or planted socket | Host-owned `<run>`, ancestor checks, bind through a checked fd, trusted pins for the server UID and workspace. |
| Path replacement | Ancestor policy (D-P2-6, D-P2-17), rechecked every 60 s; a two-process replacement test. |
| Replay | Local socket only. `idem` binding plus tombstones; human requests are single use and bound; unique event ids; `gate_gen` and `based_on`. A grant secret is a bearer token (N4, accepted); it never appears in args or logs. |
| Revoked caller keeps watching | Subscriptions revalidate before every delivery (§0.7). |
| Denial of service | §0.9 limits, unattended quotas, round-robin fairness. |
| Version skew or downgrade | Capabilities; the client refuses a missing `proto` or op contract. |

**D-P2-19. Workspace-controlled code never runs as the host UID.**

- **git: no git process in the host.** A content-addressed reader in orch's own Python reads `HEAD`, loose and packed refs, and objects (loose; pack v2 with idx v2, ofs and ref deltas; SHA-1 and SHA-256). It runs in a short-lived child (`python -I -m orch.gitread`, orch code only) with CPU, memory and wall-clock limits.
  - **Config is data, never behaviour.** A strict parser reads only `core.repositoryformatversion`, `extensions.*` and `remote.origin.url`. It ignores `include`/`includeIf`. An unknown extension or `refStorage=reftable` makes the repo observe.unavailable. Attributes, hooks, filters and textconv are never read.
  - **Pinned gitdir.** At registration the resolved gitdir (it follows a `.git` file and `commondir` once) is recorded in the host registry by realpath, device and inode. A later mismatch is observe.unavailable. Every open stays under the pinned gitdir, with `O_NOFOLLOW` and a regular-file check. `objects/info/alternates`, `info/grafts`, `shallow` and `refs/replace/` are ignored. A missing object is observe.unavailable and is never fetched.
  - **Bounds.** Inflated size must equal the object header and stay at or under the §0.9 blob limit. Delta depth is at most 64. Packed-refs and pack index sizes are capped.
  - Every object is checked against its id, so the `diff` op is computed from exactly the commits in `source_sha`. Gitlinks show as commit ids, and LFS files as their pointers, which are what the commit holds.
- **The D58 branch check** (HEAD is the symbolic ref `refs/heads/<branch>` at the bound sha) is done by the host reader before the prompt and again in `precommit`.
- **The D58 uncommitted-changes check** reads the index and the worktree. It runs as a worker job under the worker UID before the prompt. The prompt shows it as **attested by the worker, best effort** (F1 §10.7 already admits that ignored files and filters hide changes). `precommit` does not run it again. No worker means observe.unavailable for `verdict` and `approve code`.
- **`task done --run`:** the agent's client runs `verify.cmd` and uploads `{cmd, exit, commit, repo, output blob}`.
  The host checks that `cmd` equals the approved `verify.cmd` and records the receipt with
  `attested_by: "agent"`.
  - This proves that an authenticated agent **claimed** this result. It does not prove the command ran, its exit code,
    its output or its commit.
  - **The F1 §10.7 amendment (H0)** replaces "the P2 host runs it itself" with this statement. Independent
    verification needs a separate trusted executor and is not in P2.

**D-P2-20. Addons and jobs run under a worker UID, never the host UID, with no development exception.**

- `orch worker` runs as a dedicated worker user, separate from the agent users. It is started by a **LaunchDaemon**
  with `UserName` (system context, installed by the setup helper) or by a systemd system unit with `User=`.
- **Jobs** (`job.next` / `job.done`) carry a lease (60 s, renewed by heartbeat), the principal that caused the job, and
  a cancellation. Retries are explicit, at most twice, keyed by job id. Results are checked by
  `Registry.check_proposal` before the host signs anything.
- **Containment.**
  - **Linux:** the worker's system unit has `Delegate=yes`. Each job runs in its own child cgroup and is killed as a whole (`cgroup.kill`).
  - **macOS:** each job runs in its own process group, killed at its deadline. An **orphan sweep** runs after every job ends, every 30 s and at worker start. It lists worker-UID processes (`sysctl KERN_PROC_UID`) and SIGKILLs every one that does not descend from the running worker: anything that escaped its group has been reparented to launchd. Before each kill it checks UID, parent and start time again, and it repeats until no orphans are left. `RLIMIT_NPROC` on the worker UID bounds fork races. Sibling jobs are never killed. A job that daemonizes is killed, which is intended.
  - **Accepted:** jobs under the worker UID can signal each other. Nothing they return is trusted without `check_proposal` (and the D53 re-check for landing). Per-job users and launchd coalitions are P3 questions.
- Without a worker, addons do not run (`addon.runner_unavailable`).

## 8. Implementation groups

Smaller groups than v0. Execution isolation comes before host dispatch is turned on. Every group adds real
two-process tests where it touches the socket.

| Group | Content | Acceptance | Review |
|---|---|---|---|
| **H0 F1 amendment** | receipts as agent attestation; `ticket.moved`; the macOS custody row; the reserved ops marked; outcome and idempotency notes | text plus vectors for `ticket.moved` | Codex, Opus |
| **H1a Frame codec** | pure codec and limits, `fatal`, cancel state machine. T0 `tests/host/test_frames.py` | partial and coalesced reads, `n=0`, bad UTF-8, duplicate keys, NaN, depth, id reuse | Codex |
| **H1b Unix transport** | bind through a checked fd, ancestor and ACL checks, both-way peer credentials, pins, control socket | real UID refused before any byte; planted socket; ancestor replaced mid-run; path longer than 100 bytes | Opus |
| **H2a Lifecycle** | registry, states, ownership lock, register/unregister, units, release install, drain | second host gets `host.busy`; direct CLI refused after adoption; drain finishes a write in flight | Codex |
| **H2b Outcomes** | idempotency store, tombstones, `outcome.get`, cancel after commit | two-process kill -9 mid-append, then the retry is a duplicate; `idem.conflict`; `idem.expired` | Opus |
| **H3a Handler capabilities** | `Context` capabilities; handlers lose env, path and exec access; direct mode unchanged | goldens unchanged; audit-hook test (no open outside roots, no exec) | Codex |
| **H3b No exec in the host** | content-addressed git reader, `diff`, client receipts, `host.wsk_create` and `init`, `import v1` composite | a malicious `.git/config` runs nothing; receipt labelled; a `.git` file or symlinked `objects/` pointing at another repo is refused; a 1 GB zlib bomb is refused within the limits; a reftable repo is unavailable; diff equals `git diff --no-ext-diff --no-textconv` on fixtures | Opus |
| **H3c Agent and worker users (#287)** | macOS and Linux setup helper: users outside `staff`, groups, `<run>`, pins, workspace modes, LaunchDaemon for the worker | e2e `keys-isolation`, `mac-agent-uid`: the agent cannot read `wsk` or `dk` but can call `status` | Opus |
| **H4a Dispatcher** | policy table, auth and the reads rule, principals, files and blobs, `artifact.get`, `whoami`, error catalog | closed list (registry × class); no key material in any response (property test); grant in args refused; invalid grant on a read gives workspace tickets | Opus |
| **H4b Thin CLI** | `orch.hostclient`, mode by registration, rendering from `result` | goldens byte-identical in both modes for all non-human ops except `wait` | Codex |
| **H5a Human contract** | `human.prepare`/`submit`/`cancel`, material, signer recomputation, CLI composites, person versions of `new`/`set`/… | mismatch, expiry, single use, tombstone; stale between prepare and submit; prompt fields equal `describe_payload`; grant secret not on the socket at issue | Opus |
| **H5b Hand-off** | codes, `human.pending`, `orch sign CODE`, lookup limits | guessing is rate-limited; another person's code is not found | Opus |
| **H6a Live updates** | journal, `sub.*`, revalidation, cursors, overflow, `wait` on the bus | a revoked grant stops delivery, queued items included; resume with no gap; overflow then snapshot then cursor | Opus |
| **H6b Dashboard reads** | `ticket.query`, `needs`, `workspace.show`, `member.list`, `session.list`, `move` | paging is stable under appends; restricted rows hidden; `needs` equals `model.needs` | Codex |
| **H7 Turn host mode on** | the default for registered workspaces; parity including human ops and `wait`; cross-version fixtures (an older client against a newer host) | full golden parity; T2 in both modes | Opus |
| **H9 Touch ID signer** (D-P2-22) | Swift helper, `secure-enclave` backend, native review, dashboard path | recomputes hashes; refuses non-person labels; F1 vector interop | Opus |
| **H10 Worker and jobs** | `orch worker`, leases, containment, addon runner, clean-tree check | the addon cannot read the WSK; a double-forked escapee dies within 30 s while a concurrent sibling job completes | Opus |

- **Order:** H0, H1a → H1b → H2a → H2b → H3a → H3b → H3c → H4a → H4b → H5a → H5b ∥ H6a → H6b → H7 → H9, with H10
  in parallel from H4a on.
- **Cutover gate (D44):** H7, H9 and H10 (landing and addons are daily work).
- **Stream B** uses a stub of §0 now and the real host from H4a, with `orch host run --foreground` on a demo workspace.

## 9. Open questions

1. **Windows (D63).** The proposal is to defer the named-pipe `HostTransport`; Windows stays in unmanaged direct mode
   (tier A).
2. **A dedicated host user on macOS.** The proposal is not in P2; the peer map already supports it.
3. **Own reader. dulwich is rejected (it honours grafts, alternates and shallow; it adds a dependency).**
4. **Issuer-chosen UID restriction on grants** (Codex Q6). Leave it out of P2; it could come later as an optional
   `grant.issued` field.
5. **`move` semantics and the reserved ops** need F1 text (H0) before they are served.
6. **To confirm during H3b/H10:** that macOS has no subreaper and reparents to launchd, and how reftable shows up in config.

## 10. Decisions record

**1. How the host reads git (D-P2-19).**

- **Codex:** Observe the repository in an isolated, unprivileged helper over controlled snapshots.
- **Draft:** Use an in-process Python reader that ignores config; move the best-effort clean-tree check to the worker UID.
- **Tie-break:** Accepted orch's own reader in a bounded child, with config read as data, a pinned gitdir and object limits; the host checks the branch before the prompt and in `precommit`, and the worker checks uncommitted changes before the prompt only.

**2. Containing jobs on macOS (D-P2-20).**

- **Codex:** Give each job its own containment so killing one cannot kill unrelated jobs.
- **Draft:** Use per-job cgroups on Linux; on macOS use process groups and a whole-worker-UID sweep at restart and every 10 minutes, accepting that siblings may die.
- **Tie-break:** Accepted delegated per-job cgroups on Linux; on macOS use process groups and an orphan-only sweep after jobs end, every 30 s and at worker start, preserving siblings; per-job users and launchd coalitions remain P3 questions.
