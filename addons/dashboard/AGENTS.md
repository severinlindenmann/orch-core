# Dashboard developer guide

## What this is

Develop Mission Control, the orch v2 dashboard mockup. The owner reviews locally with
`npm run dev`. The hosted claude.ai preview is frozen and unmaintained; do not publish to it.
Keep the in-process mock backend: no MSW, service worker or live backend is needed.
Paths below are relative to `addons/dashboard/` unless explicitly called repository paths.

## Run & check

Use a modern Node runtime (Node 24.15+ on the 24.x line meets the locked dependencies' engines).
Check the active runtime in this directory; a shell may select an older Node here.
From the repository root:

```sh
cd addons/dashboard
npm ci
npm run dev
```

Open the localhost address Vite prints. Give each worktree its own unused port, for example:

```sh
npm run dev -- --port 5204 --strictPort
```

Vite binds `localhost` on IPv6 here; use `http://localhost:5204`, not `127.0.0.1`
unless you explicitly bind IPv4. Reload deep links such as `/w/INT/tickets`,
`/w/DEMO/settings/addon/publish` and `/ticket/DEMO-0043?tab=history`.
Keep browser history and Vite's `base: '/'`; production hosting needs an `index.html`
SPA fallback and `/assets/` served from the root.
After big changes, stop Vite, clear `node_modules/.vite`, then restart it: stale optimized
modules can cause “does not provide an export named” errors.

Run targeted tests for changed behavior, then the full checks at the end of the task:

```sh
npx vitest run --maxWorkers=3
npm run typecheck
npm run build
npm run layout:guard -- --url http://localhost:5204 --docks min,max
```

Replace 5204 with your running dev server's port. Run the layout guard **without piping**;
keep its output and exit status. It needs Chrome/Chromium (auto-detected, or pass `--chrome`
or set `CHROME_PATH`) and the dev build, not the production build. See
`scripts/layout-guard.mjs` for options. It checks Normal and Busy day, wide/rail sidebars,
1440×900 and 1470×956 viewports, core/addon pages, ticket tabs, drawers and overlays.
Every page must work at a **720 px page width beside the terminal dock** on a 13" notebook,
without horizontal page scrolling or clipped controls. Only deliberate inner scrollers
(terminals, code, diffs, tab rows, `data-scroll-x`) may scroll sideways.
The dashboard suite and guard run locally; green repository CI does not replace them.

Test slow loading with `?latency=800` or `?jitter=1`; switches persist for the browser tab.
Reset with `?latency=` / `?jitter=0`. Default latency is 120 ms per burst; jitter is
120–300 ms per request (`src/mocks/latency.ts`). Use Demo data → Normal / Busy day / Reset demo
and Review tour for repeatable manual checks. Mock time starts at `2026-10-09T11:30:00Z`.

## Architecture map

- `src/app/router.tsx`, `src/app/urls.ts`: route tree and permanent workspace URLs
  `/w/<PREFIX>/…`, tickets at `/w/<PREFIX>/ticket/<KEY>` (`/ticket/<KEY>` redirects). Keep keys/ids in addresses.
- `src/app/shell/`, `src/app/pages/`: shell and core pages; `src/app/terminal/` owns the dock.
- `src/app/routeData.ts`: loaders warm the page's data; `src/app/pages/skeletons.tsx`
  supplies matching page placeholders; `src/app/pages/lazyPage.tsx` preloads chunks.
- `src/api/client.ts`: typed API entry point. Components use it, never import mocks.
  `src/api/transport.ts` connects it to the mock now and defines the fetch alternative.
- `src/api/queries.ts`: shared query factories. Pages and loaders **must use the same
  factory**, including its key and function; use `addonStateKey` for addon invalidation.
  Keep shared pure rules in `src/api/` (including `sections.ts`, permissions and roles).
- `src/mocks/store.ts`, `router.ts`, `derive.ts`: state/persistence, HTTP-shaped routing,
  and event-derived ticket state. `src/mocks/fixtures/` seeds Normal;
  `src/mocks/busy/` generates Busy day. Persistence is versioned in `src/mocks/persist.ts`.
- `src/mocks/addons/registry.ts`: `MockAddon`, registration, visibility and refusal helpers;
  `src/mocks/addons/index.ts` imports each addon module to register it.
- `src/addon-ui/`: core draws declarative addon nodes. `nodes.ts` defines strict zod schemas;
  bindings, actions and core confirmation dialogs live alongside the renderer.
- `src/components/sign/`: shared signing surface and exact-value rendering;
  `src/app/pages/ticket/SignDialog.tsx` handles ticket signing.

## Hard rules — non-negotiable

- Core renders and signs every signature: approvals, answers, verdicts, grants, revokes,
  capability grants and addon decisions. Agents never sign, hold keys or enable addons;
  never grant agents `pty`. Mandates are a labelled, non-functional preview, not permission
  to bypass this boundary. The existing factory charter is enforced by core.
- Use core words in signing/confirmation titles, covers and buttons. Show addon-controlled
  prose only in a labelled **From the addon** region. Identify addons as `Title (package id)`;
  show exact sent arguments as core-owned lines outside that region.
- Show all signed values in full through `src/components/sign/visible.tsx`
  (`Raw`, `visible`, `plain`): escape bidi/control/format characters, isolate text, expose
  edge spaces. Wrap or scroll; never truncate, fade or substitute values. Post original values.
- Enforce `confirmed: true` in the mock host for sign, decision, destructive, options and
  spawn confirmations. Addon args cannot supply core's flag. A real host must replace this
  boolean with a person/action/argument-bound, single-use confirmation.
- Return refusals as 4xx errors with stable codes; never report `{ ok: true }` for a refusal.
  Use the helpers in `src/mocks/addons/registry.ts`.
- Write addon events as `<addon>.<verb>` with actor `addon:<name>` (structured form:
  `{ kind: 'addon', id: name }`). Never impersonate core events or use a reserved namespace.
- Follow `src/api/addons.ts` charset rules: package names `^[a-z][a-z0-9-]{0,39}$`,
  argument keys `^[A-Za-z][A-Za-z0-9_]{0,31}$`, titles 1–40 characters without
  parentheses, colon, middle dot or invisible characters. Preserve strict validation and
  signed-argument limits; do not loosen schemas to make a fixture pass.
- Declare action roles in manifest `actions[id].minRole` (default member); the mock host
  enforces them before dispatch. Hiding a button is not authorization.
- Filter ticket-related addon data with `canSeeTicket` from the registry. Restricted tickets
  must never leak titles through rows, logs, previews or errors. URLs carry keys/ids only,
  plus validated view state and user-entered search; never titles, credentials or signed values.
- Add a case to `src/test/signing-surface.test.tsx` for every new signed/confirmed path,
  plus host refusal tests. Keep the exact displayed-versus-posted values checked.
- Never seed realistic-looking credentials. Use `*.example.test` hosts and
  `demo-not-a-real-*` secrets, including in tests and terminal output.

## How to add an addon

1. Add its manifest to `src/mocks/fixtures/catalog.json`: capabilities, actions with
   `minRole`/confirmation metadata, contributions and core-drawn nodes. If initially installed,
   follow `src/mocks/fixtures/addons.json` and workspace fixture installation/grant entries too.
2. Create `src/mocks/addons/<name>.ts` calling `registerAddon`; import it from
   `src/mocks/addons/index.ts`. Follow `MockAddon` in the registry and a similar existing module.
3. Return declarative nodes only for UI, never React components or signing UI. Specifically,
   `view()` returns a serializable **state object** (derived fields and node trees such as
   `page`); manifest nodes bind to it. Validate through `src/addon-ui/nodes.ts` and filter
   ticket-scoped fields before returning them. Keep mutations in `actions`.
4. Supply `seed` and representative `seedBusy` data (omitting `seedBusy` uses the normal seed).
   Use deterministic Busy day generation; supply `seedLog` when state implies ticket events.
   Bump `stateVersion` whenever persisted state shape changes so old state is reseeded.
5. Add mock tests beside the module, page tests in `src/app/addons/`, and signing-surface
   coverage for confirmations. Check roles, refusals, hidden tickets, disabled/missing grants,
   Normal/Busy data and reloads; use `src/test/renderApp.tsx` and `src/test/installAddon.ts`.
6. Add a Today Glance item through `today.card`: one calm line, not another dashboard.
   Follow `src/app/pages/today/side.tsx` and its Glance tests.
7. For its own page, use `src/app/pages/AddonPage.tsx`, `src/app/routeData.ts` and the shared
   `queries.addonState` factory; add a shared factory if new data is needed. Preserve loaders,
   node preloading, skeletons and permanent URLs; do not fetch a second independent copy.
8. Record Decision / Why / Revert in `DECISIONS-LOG.md`, how to try it in `REVIEW.md`, and
   endpoint/operation rows plus provisional events in `HANDOVER.md`. Label proposals clearly.
   Run the checks above, including the real-browser layout guard.

## UX conventions

- Keep the UI calm, clean and easy to scan: no card-in-card, one primary action per item,
  links that look like links. Test dense Busy day data, empty/error/loading states and focus.
- Stay desktop-only (≥1024 px overall), dark-only, A · Orbit, Geist / Geist Mono.
  Use `src/styles/tokens.css`; do not put raw hex colors in components. Brand is teal;
  orange is reserved for addons. Use `AddonBadge` and `AddonFrame` for every addon surface
  (once per surface for dense repeated items), and `PreviewChip` for preview features.
- Preserve G4 loading patterns: reserve final sizes, warm queries and lazy node chunks,
  show route skeletons only after 200 ms (minimum 300 ms), cap loader waits at 1.5 s.
  Reuse each page's skeleton for its missing-data state. Never retain another workspace's
  or ticket's data as a loading placeholder.
- Keep page transitions to one 160 ms opacity fade; no movement or View Transitions.
  Respect reduced motion through `src/lib/motion.ts` and `src/app/shell/pageMotion.tsx`.
  Preserve scroll on search-only changes, reset for a new page, restore on Back/Forward.
- Add dependencies with exact versions; preserve the lockfile.

## Process

- Work in one git worktree per task. From the repository root, the pattern is
  `git worktree add ../orch-core-<x> -b <branch> origin/develop` (replace placeholders).
  If assigned an existing worktree/branch, stay on it. Use your own dev port.
- Change and commit only repository paths under `addons/dashboard/**`. Never commit
  `.superpowers/` or `.design-drafts/`. Check the diff and commit early in coherent units.
- Give every decision a dated `DECISIONS-LOG.md` entry with **Decision / Why / Revert**.
  Respect the settled owner decisions in `HANDOVER.md`.
- Obtain review before merge. Merge into develop only on green CI; check `gh pr checks`
  for the task's PR, and run dashboard checks locally too.
- Repository `docs/architecture/` belongs to the core build session: propose changes in
  this app's docs; do not edit core architecture as part of dashboard work.

## Where decisions live

- `DECISIONS-LOG.md`: decisions, rationale and reversals; read the relevant latest entries,
  especially signing hardening, permanent URLs (G2), calm navigation (G4) and mandates (M1).
- `REVIEW.md`: owner questions, decided items and how to try features. Its scenario checklist
  is mirrored by `src/app/review/scenarios.ts`; keep them consistent.
- `HANDOVER.md`: backend contract, endpoint-to-operation mapping, routes, provisional event
  types and known gaps. Mock behavior is not automatically an accepted core contract.
- `docs/concept-mandates.md`: Step 1 pilot concept; later steps remain future decisions.
- `docs/workspace-links-proposal.md`: peer trust, signed terms, scopes and open questions.
- `docs/widgets-v1-proposal.md`: proposed widget additions, not an already changed v1 spec.
- `docs/plans/2026-10-08-complete-mockup.md`: historical implementation plan; current code
  and later decisions supersede old routing/publishing instructions.
- Factory full-run and repos proposals are not present in this checkout. Check `docs/` when
  those tasks land; do not invent filenames or treat planned behavior as implemented.
