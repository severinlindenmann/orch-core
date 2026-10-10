# orch dashboard (mockup)

Mission Control for orch: a first-party addon, desktop only (1024 px and wider), dark only (the "Orbit" brand).
This is a **clickable mockup with simulated data**. The mock transport will later be swapped for calls to the orch host; components only talk to `src/api/client.ts`.

Read [AGENTS.md](AGENTS.md) before changing this app. The owner reviews locally; the hosted claude.ai preview is frozen and unmaintained.

## Run

Use Node 24.15+ on the 24.x line for the locked dependencies. From the repository root:

```
cd addons/dashboard
npm ci
npm run dev      # open the localhost address printed by Vite
# In another terminal, in addons/dashboard:
npx vitest run --maxWorkers=3
npm run typecheck
npm run build    # dist/, base '/', assets served from /assets/
npm run layout:guard -- --url http://localhost:5173 --docks min,max
```

Use your dev server's actual port for the guard; it needs Chrome/Chromium. Run it without piping.
Vite binds localhost on IPv6 here. Deep links work on reload with Vite; production hosting must
serve `index.html` for app paths. See AGENTS.md for worktree ports and cache troubleshooting.

## What is mocked

- **Transport:** `createMockTransport(store)` routes requests in-process (`src/mocks/router.ts`, 120 ms per burst; `?jitter=1` for 120–300 ms per request, `?latency=800` for a slower host).
  No MSW, no service worker. `createFetchTransport(baseUrl)` has the same contract and is unused for now.
- **Data:** `src/mocks/fixtures/*.json` is a fictional "Acme energy data" workspace (DEMO), plus INT and CLI.
  Tickets are definitions (`ticket.json`) plus events; status, tasks, acceptance state, questions, gates and
  claims are derived from events (`src/mocks/derive.ts`), as in `docs/architecture/orch-v2-ticket-format.md`.
- **Mutations:** `postAction` appends an event; the appended events persist in `localStorage` (guarded; falls back to memory).
  `POST /api/dev/reset` restores the seed, `POST /api/dev/viewer {person}` switches the demo identity (Mara is a maintainer; Tom is a viewer).
- **Addons:** `src/mocks/fixtures/addons.json` declares installed addons and their UI contributions as JSON
  (slots `nav`, `today.card`, `ticket.panel`, `board.lane`, `settings`). Anything an addon contributes is marked
  with the orange `A` badge (`src/addon-ui`).
- **Routing:** TanStack Router with browser history and permanent `/w/<PREFIX>/…` URLs (tickets at `/w/<PREFIX>/ticket/<KEY>`); tests use memory history.
- **Clock:** the mock "now" starts at 2026-10-09 11:30 UTC to match the fixtures.

## Layout

```
src/app/        router, shell (Sidebar, Topbar), pages
src/api/        types, client (typed `api`), transport (mock + fetch)
src/mocks/      store, derive, router, fixtures
src/addon-ui/   AddonBadge, AddonFrame
src/brand/      OrbitMark
src/styles/     tokens.css (Orbit tokens -> Tailwind @theme + shadcn variables)
src/components/ui/  shadcn/ui components
```

Token naming: brand teal is `brand` (`bg-brand`, `text-on-brand`); shadcn's `accent` is the neutral hover surface.
