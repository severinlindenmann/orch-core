# Mockups: the visual source of truth

Every screen a person sees in orch has a mockup, and the mockup is the source of truth for how it looks and flows.

## The rule

1. **Same PR.** A change to any UI (layout, wording, a new screen, a new state, an error or empty state) updates the
   matching mockup in the same PR. The PR body says `Mockup: updated <where>` or `Mockup: none needed (<why>)`.
2. **No mockup-only UI.** A mockup never shows a screen, button or state the product does not have.
3. **No product-only UI.** The product never ships a screen or state the mockup lacks.
4. **The one exception: Preview add-ons.** A mockup may show an add-on that does not exist yet, only when:
   - it is an add-on (never a core, hub, publish or setup screen);
   - every place it appears carries a visible **Preview** tag (sidebar entry, add-on list, page header);
   - it is listed under "Preview add-ons" below;
   - product / mockup / draft comparisons (QA) skip it;
   - the PR that ships the add-on removes its Preview tag and its row below.

   Everything else in a mockup must match the product.
5. Visual changes start as a draft against the mockup (only the delta), get challenged, and then the product and the
   mockup change together. QA marks an item verified only when product, mockup and draft agree.

## Inventory

Coverage = share of the real product's screens and states the mockup shows (estimated 31 Oct 2026, see the gap plan).

| Surface | Real UI | Mockup | Start | Covers | Coverage |
|---|---|---|---|---|---|
| Workspace dashboard | `packages/addons/dashboard/src/app` (production build = the same shell and pages on the fetch transport, plus `src/production/`) | the dashboard itself on the mock transport: `src/mocks` | `npm run dev -- --port <port> --strictPort --host 127.0.0.1` in `packages/addons/dashboard`, open `/` | every route of `src/app/router.tsx`, Normal and Busy day (topbar switch), people/roles, hub states `?hub=standalone\|selective\|full\|offline\|nottold\|update\|closing`, latency `?latency=800`, `?jitter=1` | ~85 % |
| Dashboard single states | same | `src/dev/layoutPreview.tsx` | `/__layout/<scenario>` on the dev server (e.g. `signing-on`, `terminal-take`, `wiki-empty`) | signing card, agent pills, drop, quick, records, settings, terminals, wiki edge states | (part of the row above) |
| Setup wizard | `packages/addons/dashboard/src/setup/Setup.tsx` (served by `orch setup`) | `preview-setup.html` + `preview-setup.tsx` | `/preview-setup.html` on the dev server; flags `?grant`, `?nogit`, `?used` | profile, add-ons, method, review, done | ~60 % |
| Remote client home | `packages/addons/dashboard/src/remoteClient` | none (the page needs `/api/remote-client`) | `/remote-client.html` shows only its error state | - | ~5 % |
| Hub web console | `packages/hub/src/orch_hub/web/*` (`index.html`, `app.js`, `pages/*.js`), `connect.html` | `docs/mockups/hub/index.html` (one file, demo data `D`, notes `N`) | `python3 -m http.server <port> --bind 127.0.0.1 --directory docs/mockups/hub` | home, approvals, hand-offs, activity, workspaces (+ one workspace), connections, people, groups, spaces, services, phones, published, add-ons, skills/MCP/secrets, settings, account, look & feel; offline/slow toggles | ~50 % |
| Publish visitor pages | `packages/publish/pubsrv/serve.py`, `appserve.py`, `packages/publish/host/orch_apps_host/` | none | - | - | 0 % |
| Publish in the workspace | dashboard Shares / Publish add-on | dashboard mock (`src/mocks/addons/publish.ts`) | as dashboard | share flow, published list | (dashboard row) |
| Add-on UIs | `packages/addons/dashboard/src/app/addons/*`, `src/app/pages/<addon>` | `src/mocks/addons/<name>.ts`, `src/mocks/fixtures/addons.json`, `catalog.json` | as dashboard | installed + catalog add-ons | see Preview add-ons |
| Mobile app (iOS) | `packages/mobile/App/Hub/*` (pair, home, sign) | DEBUG launch args `--hub-fake home`, `--hub-pair`, `--hub-sign`; `docs/screens/*.png` | Xcode / `xcodebuild` on the iOS Simulator with those arguments | hub home, pairing, sign sheet | ~40 % (screens folder is stale) |
| Relay | `packages/relay` | no UI | - | - | n/a |
| remote-key | `packages/remote-key` | no UI (system Touch ID prompt only) | - | - | n/a |

`scripts/dev/mockups.sh start` starts the dashboard and hub mockups on free ports in 8780-8799 and prints the URLs;
`scripts/dev/mockups.sh stop` stops exactly those processes. Run `npm ci` in `packages/addons/dashboard` once first.

## Preview add-ons

Add-ons the dashboard mockup shows that have no real implementation yet. Each must carry a visible "Preview" tag.

| Add-on | Mock | Real | Tag shown |
|---|---|---|---|
| factory | `src/mocks/addons/factory.ts`, `factory-runs.ts` | none | not yet (gap) |
| land (Landing) | `src/mocks/addons/land.ts` | key store only (`packages/core/src/orch/land`) | not yet (gap) |

To check before the next audit (backend refs exist, UI may be mock-only): `links`, `repos`, `guide`, `widgets`.

## How to add a screen or state

**Dashboard.** UI in `src/app/pages/<area>` or `src/app/addons/<name>`; data only through `src/api/client.ts`.
Seed data: `src/mocks/fixtures/*.json` (DEMO workspace "Acme energy data", mock clock 2026-10-09 11:30 UTC); busy day:
`src/mocks/busy/`; add-on mock: `src/mocks/addons/<name>.ts` with `registerAddon`, imported from `src/mocks/addons/index.ts`;
request handling: `src/mocks/router.ts`. A single hard-to-reach state: a `<area>-<state>` scenario in
`src/dev/layoutPreview.tsx`, reached at `/__layout/<area>-<state>`. Hub-dependent states: add a scenario to
`HUB_SCENARIOS` in `src/mocks/hub.ts` (`?hub=<name>`).

**Setup wizard.** `preview-setup.tsx` holds the `state` and `review` fixtures; add a URL flag (`?<flag>`) per state.

**Hub console.** In `docs/mockups/hub/index.html`: data in `D`, one page function `P.<page>`, the sidebar entry in `NAV`,
requirement notes in `N.<page>` (`["today"|"part"|"new", text]`). Use the real page's title and nav label.

**Publish, mobile, remote client.** No mockup system yet; see the gap plan before adding screens there.

## Comparing product, mockup and draft

- Screenshots at 1280 and 720 px wide for each of product, mockup and draft:
  `~/orch-dev/scripts/shot.sh <url> <dir>/<name>-1280.png 1280` and `... 720` (dev sandbox helper; any headless
  browser works).
- Dashboard layout guard (needs the dev server):
  `npm run layout:guard -- --url http://127.0.0.1:<port> --only <route-regex> --shots <dir>`; `--quick` for one
  configuration.
- Compare title, nav label, button words, order of blocks, empty and error states. Product text never names a VPN vendor.
