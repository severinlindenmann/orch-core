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
