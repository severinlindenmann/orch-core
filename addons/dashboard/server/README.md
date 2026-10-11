# Dashboard server — M0

`orch-dashboard` is the addon-local launcher and FastAPI server. It holds bearer sessions,
never signing keys, and connects to a host as the current UID's mapped person. M0 renders a
dark, read-only shell: workspace switcher, identity, minimal Today/needs, and paged ticket rows.
Signing, full ticket pages, settings, board and browser live updates belong to later milestones.

## Install and build

From the checkout (Node 24, uv, Python 3.11+):

```sh
cd addons/dashboard
npm ci
npm run build
cd server
uv sync --locked
uv build --wheel
```

`npm run build` selects `src/main.prod.tsx`, fails if a mock module enters the module graph,
and copies `dist/` into `server/src/orch_dashboard/static/`. Build the frontend **before** the
wheel. The wheel includes that static tree and only `orch_dashboard`; neither `devhost` nor
`orch-core` is a runtime dependency. Core is a uv path dependency in the dev group only.

## Run with a dev stub

Use a disposable P1 workspace and its existing host state, not a production workspace.
The stub uses the P1 signed store and registry; it does not seed mock tickets. Initialize a
disposable workspace through P1's `orch init --prefix DEMO` in your own terminal (or use a
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
   device/key filename as an identity. Start the stub, from `addons/dashboard`:

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
   short (under 100 UTF-8 bytes). `ORCH_RUN_DIR` can point to a shorter private run directory.
4. In another terminal, launch the server and browser:

   ```sh
   cd addons/dashboard/server
   uv run python -m orch_dashboard open
   # Equivalent: uv run orch-dashboard open
   # Reopen a copied link after a server restart:
   uv run orch-dashboard open /w/DEMO/tickets
   # Stop through the checked control socket:
   uv run orch-dashboard --stop
   ```

   `serve` runs in the foreground if preferred. `open` starts or locates one server per OS
   user and opens the browser only after verifying an HMAC proof on its actual bound port.
   Launch URLs/tokens are never printed. `orch dashboard` is the intended host CLI integration;
   M0 deliberately adds no command to `plugins/orch-core`.
5. For UI development start Vite in another terminal:

   ```sh
   cd addons/dashboard
   npm run dev -- --port 5204 --strictPort
   ```

   Vite remains the mock app, as do Vitest and the layout guard. To review changes against
   the stub, rerun `npm run build` and reload the server's browser tab. This avoids a second
   origin for real credentials; no dev proxy or CORS setting ships in the server.

The production server uses the same socket client for stub and real hosts. It has no stub
mode. `ORCH_HOST_SOCKET` overrides the workspace socket location but never the trusted
workspace-id/host-UID pin; control discovery still uses `ORCH_RUN_DIR/control/host.sock`.
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

Closed M0 route surface:

| Route | Purpose |
| --- | --- |
| `GET /`, `/w/<prefix>/{today,tickets,identity}`, `/assets/*`, `/favicon.svg` | Public static shell; index no-store, hashed assets immutable |
| `GET /auth/probe?n=…` | Public HMAC only |
| `POST /auth/session` | Code exchange; exact Origin/JSON, 1 KiB, 10 failed exchanges/minute |
| `POST /auth/logout` | Bearer and exact Origin |
| `GET /api/session` | Features per workspace and union |
| `GET /api/workspaces` | UUIDs, names, members, needs counts, availability |
| `GET /api/me` | Person identity per available workspace |
| `GET /api/w/<uuid>/identity` | Workspace identity plus whoami/tier |
| `GET /api/w/<uuid>/today` | Minimal needs projection |
| `GET /api/w/<uuid>/tickets?limit=50&sort=updated&page=…` | Typed query, limit 1–200, next-page cursor |

Every API path, including unknown ones, needs a bearer token. Authenticated unknown routes
return 404 (unsupported methods 405). Unsafe requests require exact Origin; `Sec-Fetch-Site`
when present must say same-origin. Foreign/null Origin is refused on reads too. Responses
carry strict CSP, nosniff and no-referrer; API/auth are no-store. Validation errors never
reflect submitted bodies. The launcher disables access logs. No request body exceeds 1 MiB.

The launcher opens a private 0600 HTML redirect file inside the 0700 dashboard state
folder. Browser process arguments contain only its file URL, never the launch code.
The server deletes the redirect after a successful exchange or after 60 seconds.
Failed exchanges share a per-minute budget; a valid code always bypasses that budget.

## Contract v1 seams and M0 limits

- v1 does not define `host.list`'s output container or the control socket welcome's workspace
  and peer.person. The stub uses `{workspaces:[{id,mapped:true}]}` and its sole workspace's
  person welcome on control. These assumptions are isolated in `gateway.py`/`devhost` for
  stream A to confirm; control requires own UID, workspace connections require full pins.
- `workspace.show` fields epoch/WSK fingerprint and device auth are unavailable in the P1
  view: the stub returns null/"not available", never guesses hashes or reads private keys
  to build display values. `created_at` is unavailable too.
- M0's typed projections use `Pick` of `src/api/types.ts` where supported, plus explicit
  nullable fields. They do not synthesize mock progress, turn, question counts or timestamps.
  Needs retains `no_eligible`, `acknowledge` and `addon`, which the old mock union cannot
  represent; approve maps to approval (verify to verdict). `closed` remains closed.
- P1 lacks a person-read Context. The dev-only query wrapper sets its own local Call's
  principal and uses P1 verified views/model needs. Its registry reads are allowlisted
  (`list`, `addon.list`); P1 `list` conservatively returns workspace-visible tickets only.
  All UI reads use the person-aware `ticket.query` wrapper. No global monkeypatch or second
  ticket model. Neither `human.*` nor agent writes are available.
- Stub query supports the M0 subset (limit/page/sort). Stub subscriptions poll verified
  store state and conservatively emit v1 invalidation when it changes; they do not pretend
  to supply the real host's durable journal replay. Browser live updates are M1.
- The socket client serializes requests (within v1's eight-inflight limit), buffers bounded
  interleaved events, preserves error envelopes, supports cancel/outcome lookup, and requires
  a caller-owned idem on writes. It never retries a write. Missing hosts stay visible.

## Checks

```sh
# addons/dashboard
npx vitest run --maxWorkers=3
node --test scripts/layout-startup.check.mjs
npm run typecheck
npm run build
# addons/dashboard/server
uv run pytest -q
uv run ruff check .
```

Tests use FastAPI TestClient and socketpair, including real signed P1 workspace fixtures,
so no listener ports are needed. Full browser launch, real cross-origin browser enforcement,
layout guard and S1/S2 against stream A's real host still need the dev-kit outside this
sandbox. Stub-only success does not close #304–#306 or establish real-host acceptance.
