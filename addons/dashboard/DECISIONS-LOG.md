# Decisions log

Non-trivial product and engineering decisions made during the autonomous run, for the owner's review.
Format: date, decision, why, how to revert.

## 2026-10-08 Latency off under test; shared `renderApp`

- **Decision:** Latency is off under `MODE === 'test'`; UI tests share `renderApp` (`src/test/renderApp.tsx`), which resets the mock store before every render and optionally sets the viewer.
- **Why:** The 120-300 ms mock latency raced testing-library's 1 s `findBy` timeout under parallel load (the flaky test), and tests shared one store per file.
- **Revert:** In `src/api/client.ts` drop the `isTest` checks (latency back on, persistence on); restore per-file `renderApp` helpers.

## 2026-10-08 Global `asyncUtilTimeout` of 4 s

- **Decision:** `src/test-setup.ts` raises testing-library's `asyncUtilTimeout` from 1 s to 4 s.
- **Why:** With latency off, a rare failure remained: the first render in a file (cold route and module load) occasionally exceeded 1 s under parallel load (seen about 1 in 15 full runs).
- **Revert:** Remove the `configure(...)` call in `src/test-setup.ts`.

## 2026-10-08 Storage key `orch-mock-v2`; seed grant only in the first workspace

- **Decision:** Persistence moved to `orch-mock-v2` (`PersistedV2`); older keys (`orch-mock`, `orch.dashboard.mock.v1`) are removed on load and any non-v2 payload is discarded. `store.grants(ws)` seeds the Severin grant (`gr_01J9Z8`, from `me.json`) in the first workspace (DEMO) only; other workspaces start with no grants.
- **Why:** A versioned key lets later iterations change the shape without crashing on stale state. The demo grant is the only grant the fixtures define; richer grant fixtures belong to Task 9.
- **Revert:** Restore `STORAGE_KEY` in `src/mocks/persist.ts`; drop the seed array in `MockStore.grants`.

## 2026-10-08 Tickets page: status filter client-side, new `add_label` mock action, scrollable table at wide sidebar

- **Decision:** The Tickets page sends every filter except `status` to the mock and filters status in the browser, so the status chips show counts for the other filters and toggle instantly. Bulk "Add label" uses a new mock action `{action:'add_label', label}` (owner/maintainer only, event `labels.changed`, derived into `labels`). The table has `min-w-[800px]` inside its own scroll container: at 1024 px with the wide sidebar it scrolls inside the card instead of squeezing the title to ~100 px; the page itself never overflows.
- **Why:** Counts per status need the unfiltered-by-status list; labels had no endpoint; a 100 px title is unreadable.
- **Revert:** Pass `status` in the `listTickets` params and drop the client filter in `src/app/pages/tickets/index.tsx`; remove the `add_label` case in `src/mocks/router.ts` and `labels.changed` in `src/mocks/derive.ts`; lower `min-w` in `TicketsTable.tsx`.
