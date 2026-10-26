# Dashboard server — M2 reads

`orch-dashboard` is the addon-local launcher and FastAPI server. It holds bearer sessions,
never signing keys, and connects to a host as the current UID's mapped person. M0 is the shell
(workspace switcher, identity), M1 adds live updates over authenticated fetch-streamed SSE, M2
(#305 reads) serves the mockup's real pages from the host: Today, board, Tickets list, ticket
page (sections, tasks, questions, evidence, log) and the Changes tab. Everything is read only.
Signing (hand-off), settings, artifacts, agents and addon pages belong to later milestones.

## Real host (H6b and later)

The server needs no stub: it connects to a real `orch host run` (control socket `<run>/control/host.sock`, workspace sockets
`<run>/<uuid>/host.sock`) and the registry the host keeps in `<state>/host/workspaces.json`. Bring up a disposable one with
`tests/realhost_kit.py up BASE` (see REVIEW "Try the dashboard on a REAL host"); run the integration suite with
`uv run pytest -m realhost -q`. Keep the state directory short (unix socket paths are about 100 bytes) and outside /tmp.
The real host lists `human.*` ops without the `human/1` cap and serves no `diff` yet; the server handles both (HANDOVER "P2 on the real host").

## Install and build

From the checkout (Node 24, uv, Python 3.14+):

```sh
cd packages/addons/dashboard
npm ci
npm run build
cd server
uv sync --locked
uv build --wheel
```

`npm run build` selects `src/main.prod.tsx`, fails if a mock module enters the module graph,
and copies `dist/` into `server/src/orch_dashboard/static/`. Build the frontend **before** the
wheel. The wheel includes that static tree and only `orch_dashboard`, never `devhost`. **orch-core is a runtime dependency**
(`orch-core>=2.0.0.dev0`, editable path source while it is a dev version): the click-to-sign signer child is core's own
`python -I -m orch.click`, started by this server from its own venv. Never run the server with a `PYTHONPATH` that points at another
core.

## Run with a dev stub (CI and the mock app; differs from the real host, see HANDOVER)

Use a disposable P1 workspace and its existing host state, not a production workspace.
The stub uses the P1 signed store and registry; it does not seed mock tickets. Initialize a
disposable workspace through P1's `orch setup` (prefix DEMO) in your own terminal (or use a
workspace prepared by the dev-kit). The terminal handles its signing factors.

1. Explicitly mark that workspace and its separate dev state directory with an empty
   `.orch-dev` file in each. The stub refuses the default host state directory and socket
   overrides outside the marked state. Export
   `ORCH_DEV=1`, `ORCH_STATE_DIR` pointing to its host state, and `ORCH_WORKSPACE` pointing to
   the workspace. All terminals below must use the same state directory.
2. Install this addon's `orch-addon.json` at `$ORCH_WORKSPACE/addons/dashboard/orch-addon.json`.
   In the workspace, run P1 `orch addon grant dashboard` in your terminal. It signs the real
   `serve_http` policy grant. The server refuses absent, disabled, changed or ungranted packages.
3. Find your person id in the workspace roster (the owner id printed by init); do not use a
   device/key filename as an identity. Start the stub, from `packages/addons/dashboard`:

   ```sh
   export ORCH_DEV=1
   # Set these to your disposable workspace and its host state:
   export ORCH_WORKSPACE=/absolute/path/to/dev-workspace
   export ORCH_STATE_DIR=/absolute/path/to/dev-state
   mkdir -p "$ORCH_STATE_DIR"
   touch "$ORCH_STATE_DIR/.orch-dev"
   server/.venv/bin/python -m devhost "$ORCH_WORKSPACE" --person p_YOUR_PERSON_ID
   ```

   The stub creates a 0600 registry only if none exists. An existing registry must already
   contain the exact mapping. It never overwrites or adopts real registry entries. It opens
   `<state>/run/control/host.sock` and `<state>/run/<uuid>/host.sock`. Keep Unix socket paths
   short (under 100 UTF-8 bytes). `ORCH_HOST_RUN_DIR` must be a short **real directory**, with no symlink aliases; for the stub it
   must remain inside the marked dev state (for example `/private/tmp/orch-m1/state/run`; macOS `/tmp` is a symlink
   to `/private/tmp`, so do not use it). The `orch` CLI needs the workspace signing key (WSK) under
   `$ORCH_STATE_DIR/hosts/<workspace-id>/keys` to append; a workspace not made by `orch setup` has none and stays read-only.
4. In another terminal, launch the server and browser:

   ```sh
   cd packages/addons/dashboard/server
   uv run python -m orch_dashboard open
   # Equivalent: uv run orch-dashboard open
   # Reopen a copied link after a server restart:
   uv run orch-dashboard open /w/DEMO/tickets
   # Stop through the checked control socket:
   uv run orch-dashboard --stop
   # Stop, wait until the old server has let go of its control socket, open a new one (after an update):
   uv run orch-dashboard restart
   ```

   `serve` runs in the foreground if preferred. `open` starts or locates one server per OS
   user and opens the browser only after verifying an HMAC proof on its actual bound port.
   Launch URLs/tokens are not printed by default. If the browser cannot open the redirect file,
   use `uv run orch-dashboard open --print-url` in an interactive terminal. The opt-in link
   is single-use and expires after 30 seconds; redirected output is refused. `orch dashboard` is the intended host CLI integration;
   M0 deliberately adds no command to `packages/core`.
5. For UI development start Vite in another terminal:

   ```sh
   cd packages/addons/dashboard
   npm run dev -- --port 5204 --strictPort
   ```

   Vite remains the mock app, as do Vitest and the layout guard. To review changes against
   the stub, rerun `npm run build` and reload the server's browser tab. This avoids a second
   origin for real credentials; no dev proxy or CORS setting ships in the server.

The production server uses the same socket client for stub and real hosts. It has no stub
mode. `ORCH_HOST_SOCKET` overrides the workspace socket location but never the trusted
workspace-id/host-UID pin; control discovery still uses `ORCH_HOST_RUN_DIR/control/host.sock`.
Do not set a single-workspace override for a multi-workspace host.

## Trust, auth and routes

The trusted registry is `<state>/host/workspaces.json`, owned by the current UID with mode
0600. The provisional container is `{"workspaces":[{"workspace_id":"…","host_uid":501,
"root":"…","peers":{"501":{"class":"person","person":"p_…"}}}]}`. Browser data never
selects socket paths. Kernel peer UID, welcome host UID/workspace UUID, peer class and person
mapping must all match. Missing capabilities/op versions hide the corresponding feature.
A disabled dashboard addon is refused by server policy; it is not a host security boundary.

The HTTP listener is exactly `127.0.0.1:0`. Host must exactly match its chosen port. There are
no cookies, CORS, trusted forwarding headers, API docs, or generic operation forwarding.
The private 0600 control socket is inside an owned 0700 directory opened with `O_NOFOLLOW`;
startup is flock-serialized and both IPC peers check UID. A stale socket is unlinked only
under that lock after a refused connection, with type and owner checked.

Launch codes and bearer tokens each have 256 bits of entropy. Codes last 30 seconds and are
atomically single-use; tokens live in memory (12-hour idle, 24-hour absolute lifetime). The
browser strips the fragment before fetching, keeps the token in memory/sessionStorage, and
uses `Authorization`. Same-origin tabs can share a token with BroadcastChannel; logout
revokes it and clears other tabs holding that same token. Restart ends every session. Browser extensions/crash
restore may see a launch fragment before scrub or restore sessionStorage; expired/revoked
credentials are useless. Same-UID processes are outside this browser threat model.

Closed route surface (M1, extended by M2):

| Route | Purpose |
| --- | --- |
| `GET /`, `/w/<prefix>/{today,tickets,identity}`, `/assets/*`, `/favicon.svg` | Public static shell; index no-store, hashed assets immutable |
| `GET /auth/probe?n=…` | Public HMAC only |
| `POST /auth/session` | Code exchange; exact Origin/JSON, 1 KiB, 10 failed exchanges/minute |
| `POST /auth/logout` | Bearer and exact Origin |
| `GET /api/events?ws=<uuid>` | Bearer SSE; optional `Last-Event-ID`, no replay buffer |
| `GET /api/w/<uuid>/head` | Snapshot cursor for polling fallback |
| `GET /api/session` | Features per workspace and union |
| `GET /api/workspaces` | UUIDs, names, members, needs counts, availability |
| `GET /api/me` | Person identity per available workspace |
| `GET /api/w/<uuid>/identity` | Workspace identity plus whoami/tier |
| `GET /api/w/<uuid>/today` | `needs` + `ticket.query` as the UI's Today document (needs with ticket titles, counts, tickets in progress) |
| `GET /api/w/<uuid>/tickets`, `/board` | `ticket.query` rows as the UI's ticket summaries, one page per call: `status` (repeated; `backlog open in-progress testing done`, never `closed`), `type`, `label` (repeated), `assignee`, `owner`, `parent`, `epic`, `q`, `needs_me`, `sort` (`updated priority key`), `order`, `limit` 1–200, `page` (host token, 10 min). Default statuses are the five open ones |
| `GET /api/w/<uuid>/tickets/<key>` | `ticket.get` as the UI's ticket document (v1.4: a closed ticket reads like any other, with `status: closed` and a `closed` object; a host without `read_ext/1` leaves the optional fields out and the document says `read_ext: false`) |
| `GET /api/w/<uuid>/tickets/<key>/events?since=&limit=` | The structured log (`seq`, `type`, `at`, `actor`, host `summary`) as UI events |
| `GET /api/w/<uuid>/tickets/<key>/changes?repo=&context=` | `diff` as the UI's changes: files with their hunks, sources per repo, `truncated`; 409 `observe.unavailable` when the branch moved since the host observed it |
| `GET /api/w/<uuid>/settings`, `/members`, `/grants?active=` | M4: `workspace.settings` (cap `settings/1`, else `available:false`), `member.list` with devices, `grant.list` (what the host gives this person; `read_ext/2` fields or `null`). The writes are hand-off actions on `POST /handoff`: `settings_set`, `policy_set`, `member_add` (files), `member_role`, `member_remove`, `grant_revoke` |
| `GET /api/w/<uuid>/tickets/<key>/artifacts/<name>` | An evidence file, read in chunks up to 3 chunks, digest checked (409 `artifact.mismatch`), returned as text data or flagged binary |

Every API path, including unknown ones, needs a bearer token. Authenticated unknown routes
return 404 (unsupported methods 405). Unsafe requests require exact Origin; `Sec-Fetch-Site`
when present must say same-origin. Foreign/null Origin is refused on reads too. Responses
carry strict CSP, nosniff and no-referrer; API/auth are no-store. Validation errors never
reflect submitted bodies. The launcher disables access logs. No request body exceeds 1 MiB.

The launcher opens a private 0600 HTML redirect file inside an owned 0700
`$XDG_RUNTIME_DIR/orch-dashboard` directory when `$XDG_RUNTIME_DIR` already exists as a real 0700 directory
you own (it is never created), otherwise the 0700 dashboard state
folder. Startup removes owned regular `launch-*.html` files older than 30 seconds (a code's life) from both
locations; it does not follow symlinks. Browser process arguments contain only its file URL, never the launch code.
The server picks the directory and returns it to the launcher in the `launch` reply, so both agree.
The server invalidates the code and deletes the redirect once, the moment the code is redeemed or when it expires
(30 seconds), whichever comes first. The launcher writes no redirect past the code's expiry, and deletes it at once
when no browser can be opened. The page replaces itself (`location.replace`) and the dashboard redeems the code (which
deletes the file) and scrubs the fragment before anything else runs. A `file://` page cannot delete itself; a later
copy of it holds a dead code. ctl.sock cannot tell an agent of your UID from you: an agent with its own socket client
still gets a code (accepted in tier A, D-P2-31).

`orch dashboard` and `orch-dashboard` (`open`, `launch`, `serve`, also `python -m orch_dashboard`) refuse with
`human_only` (exit 3) when `ORCH_GRANT` or `ORCH_SESSION` is set and non-empty: a dashboard session acts as the person.
Only `stop` passes. In tier A this is a speed bump, not a boundary (an agent runs as your UID).
Failed exchanges share a per-minute budget; a valid code always bypasses that budget.

## Live updates and contract v1.3.2

One socket subscription per active workspace is shared by all tabs. The server reads
`ticket.query` then `needs`, subscribes to `workspace`, `tickets`, `needs` after the earlier
snapshot cursor, and sends `ready`. The browser enables workspace reads after `ready`.
Reset, overflow, restart or a lost upstream connection takes new snapshots and reopens;
browsers receive `resync`. Item deduplication uses `id`, not `jpos` (an event and a needs
notification can share a position). The last tab leaving closes the upstream subscription.

SSE uses an Authorization bearer header through fetch, never a query-string token or
EventSource. Each workspace has instance-scoped increasing IDs. A tab reconnects with its
own `Last-Event-ID`; a mismatch gets one resync, without replay. Queues hold 256 messages;
overflow replaces queued messages with resync and ends the stream. Sessions have a cap of
16 streams, 15-second heartbeat comments, and logout/expiry interrupts blocked sends too.
Every drained batch rechecks membership, addon policy and the visibility of the batch's own tickets
(`ticket.get` per ticket; paged `ticket.query` only if that op is unsupported or the batch is large). The answer is
computed once per workspace and shared by all tabs for a 1 s TTL, so host calls do not grow with the number of tabs;
member, role, device and `gone` events invalidate it. The workspace task does the 15-second membership check, once per
workspace. A `gone` is forwarded only for a ticket the person could see; batches that end up empty are not sent.
Membership loss, a disabled addon or `not_found` sends `revoked`; an unavailable host, a timeout or a connection
error sends `resync` and the server retries with 1, 2, 5, 10 s backoff while tabs keep their caches. The browser cancels and removes affected cached details and strips hidden
rows before refetch; production currently has no ticket-detail or hand-off view to close.

`useLiveUpdates` applies the design's per-item invalidation table with workspace UUID keys.
Any received byte (including a heartbeat) resets the failure count. Reconnect delays are 1, 2, 5 and then 10 seconds; the third consecutive failure also starts
the 10-second head poll. A recovered stream stops polling. Poll cursor changes invalidate workspace queries. Mock Vite/test
transport has no stream hook and keeps its existing behavior.

The socket client accepts v1.3.1 strict frames (including the 65,536-value limit), null control
workspace and control peer, and the defined `host.list` person mapping. Trusted local pins
still select paths, never the `socket` string returned by a host. New human operation shapes
are supported by the client only; no human operation or write is exposed by the stub/server.

Dev stub boundaries for stream A:

- Polls the P1 verified store every 200 ms while subscribed and projects metadata-only items.
  Opaque cursors are in-memory and scoped to the stub workspace/person instance; a restart or
  history change resets. More than 256 pending items/256 KiB ends with overflow. Old/out-of-scope
  cursors answer `sub.reset`. It is a development adapter, not the durable real-host journal.
- Visibility transitions discovered in one poll are coalesced. Losing multiple tickets in one
  position resets rather than inventing duplicate `.g` IDs. Exact per-event visibility history,
  restored-history epochs and durable cursors remain real-host acceptance work.
- M2 (v1.4): the stub also serves `activity.list`, the optional `read_ext/1` fields and closed tickets
  (`ORCH_DEV_READ_EXT=0` drops the two caps); page tokens are bound to the person. It serves `ticket.query` with every filter, sort and snapshot paging (the first
  page fixes the rows; tokens are bound to the query and expire after 10 minutes), `ticket.get`
  (document, structured log, question states, evidence digests per criterion, gate states),
  `needs` (own person only), `diff` (git state of the dev workspace, refused unless the branch head
  equals the head the host observed, D58) and `artifact.get` (chunked, digest checked). It still
  has no `grant.list` or `human.*`. Unsupported operations are unadvertised and return
  `unknown_command`. Host capability checks gate calls.
- `workspace.show` epoch/WSK fingerprint and device auth are unavailable in P1 and remain
  explicitly unavailable; the adapter never reads private keys to construct display values.
- Local registry container compatibility still needs stream A confirmation. Control welcome
  and `host.list` output now follow §0.8.3; the former M0 discovery-shape assumption is removed.

## Checks

```sh
# packages/addons/dashboard
npx vitest run --maxWorkers=3
node --test scripts/layout-startup.check.mjs
npm run typecheck
npm run build
# packages/addons/dashboard/server
uv run pytest -q
uv run ruff check .
```

Tests use FastAPI TestClient and socketpair, including real signed P1 workspace fixtures,
so no listener ports are needed. Full browser launch, real cross-origin browser enforcement,
layout guard and S1/S2 against stream A's real host still need the dev-kit outside this
sandbox. Stub-only success does not close #304–#306 or establish real-host acceptance.

## M3: signed actions and the dev stub signer

The stub also serves `human.prepare/get/pending/cancel/submit` (host contract v1.4, section 0.8.1) on the P1 store. The
dashboard server only prepares, reads and cancels; the person signs with the **dev stub signer**, never shipped and
excluded from the wheel:

```sh
cd packages/addons/dashboard
ORCH_DEV=1 ORCH_STATE_DIR=/path/to/dev-state server/.venv/bin/python -m devhost sign 7F3K-Q2MA
```

It resolves the code with `human.pending`, binds with `human.get`, recomputes the digest, the fingerprint and the review,
renders it on the terminal, asks for the ticket key and then the passphrase (both on `/dev/tty`), signs with the dev
workspace's device key (`<state>/hosts/<ws>/person/dk.key.json`) and submits. Routes: `POST /api/w/<uuid>/handoff`,
`GET|DELETE /api/w/<uuid>/handoff/<request_id>`.

## Click-to-sign (H5c)

One signer child per server (`orch.click.spawn.start()`), started on first use, restarted if it exits, stopped on shutdown. It holds the
device key; this process never does. Routes: `POST /api/w/{ws}/click/{prepare,view,sign,cancel}`, `GET /api/w/{ws}/click/status`.
Off per workspace until `orch host convenience on --workspace W`; `ORCH_DASHBOARD_CLICK=off` never starts the child. Tests use a faked
channel (`tests/test_dashboard_click.py`) and, with `-m realhost`, the real host and signer (`tests/test_realhost_click.py`, file custody in
a disposable state; macOS keychain never touched). Card text: "Convenience signing: agents running as you, or anything that takes over
this dashboard, could sign." See HANDOVER "Click-to-sign (H5c)".

## First-use packaging update (runbook gaps 2 and 3)

With core already installed in the same venv, install the dashboard from a revision containing
this packaging fix (use `develop` after merge):

```sh
python -m pip install "orch-dashboard @ git+https://github.com/severinlindenmann/orch-core.git@develop#subdirectory=packages/addons/dashboard/server"
orch-dashboard addon-manifest --write /absolute/path/to/workspace
cd /absolute/path/to/workspace
orch addon grant dashboard
```

Node >= 24 (24.15+ recommended for the locked dependencies), npm and npm registry access are
needed at install time: the wheel hook runs `npm ci` and `npm run build` when
`server/src/orch_dashboard/static/index.html` is absent. An existing UI build is reused.
Missing tools, sources or failed builds now fail packaging with instructions instead of
silently shipping no UI. Prebuilt wheels need no Node at installation time.
The supported source route is a full checkout or the git URL above. Server sdists omit UI
sources; build the UI before creating an sdist to carry its static output, or build the wheel
directly from the checkout. `ORCH_DASHBOARD_SKIP_UI=1` explicitly permits UI-less development
builds; do not use it for deployment. The manual build instructions above remain supported.

The manifest is included from the repository's single `packages/addons/dashboard/orch-addon.json`.
`orch-dashboard addon-manifest` prints its JSON; `--write` replaces the runbook's manual copy
into `<workspace>/addons/dashboard/orch-addon.json`. Identical files are left alone; different
files require `--force`. Symlinks anywhere in the destination path are refused (on macOS use
`/private/tmp`, not the `/tmp` alias). This only copies the manifest; the owner still signs the
grant, including again after a manifest update. It does not start the dashboard or host.

The dashboard and devhost use core's `ORCH_HOST_RUN_DIR`, falling back to `<state>/run`.
A set legacy `ORCH_RUN_DIR` is accepted silently only when its absolute path matches that directory;
otherwise dashboard startup exits 1 with a migration message.
