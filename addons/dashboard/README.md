# orch dashboard (mockup)

Mission Control for orch: a first-party addon, desktop only (1024 px and wider), dark only (the "Orbit" brand).
This is a **clickable mockup with simulated data**. The mock transport will later be swapped for real `fetch` calls
to a FastAPI backend; components only talk to `src/api/client.ts`.

## Run

```
npm i
npm run dev      # http://localhost:5173
npm run build    # static build in dist/, works from any folder (base './')
npm test         # vitest (jsdom)
npx tsc --noEmit -p tsconfig.app.json
```

## What is mocked

- **Transport:** `createMockTransport(store)` routes requests in-process (`src/mocks/router.ts`, 120-300 ms latency).
  No MSW, no service worker. `createFetchTransport(baseUrl)` has the same contract and is unused for now.
- **Data:** `src/mocks/fixtures/*.json` is a fictional "Acme energy data" workspace (DEMO), plus INT and CLI.
  Tickets are definitions (`ticket.json`) plus events; status, tasks, acceptance state, questions, gates and
  claims are derived from events (`src/mocks/derive.ts`), as in `docs/architecture/orch-v2-ticket-format.md`.
- **Mutations:** `postAction` appends an event; the appended events persist in `localStorage` (guarded; falls back to memory).
  `POST /api/dev/reset` restores the seed, `POST /api/dev/viewer {person}` switches to Mara or Tom (viewer role).
- **Addons:** `src/mocks/fixtures/addons.json` declares installed addons and their UI contributions as JSON
  (slots `nav`, `today.card`, `ticket.panel`, `board.lane`, `settings`). Anything an addon contributes is marked
  with the orange `A` badge (`src/addon-ui`).
- **Routing:** TanStack Router with memory history (URL fragments do not work in the sandboxed viewer).
- **Clock:** the mock "now" starts at 2026-10-09 11:30 UTC to match the fixtures.

## Layout

```
src/app/        router, shell (Sidebar, Topbar), pages
src/api/        types, client (typed `api`), transport (mock + fetch)
src/mocks/      store, derive, router, fixtures
src/addon-ui/   AddonBadge, AddonFrame
src/brand/      OrbitMark, Wordmark
src/styles/     tokens.css (Orbit tokens -> Tailwind @theme + shadcn variables)
src/components/ui/  shadcn/ui components
```

Token naming: brand teal is `brand` (`bg-brand`, `text-on-brand`); shadcn's `accent` is the neutral hover surface.
