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

## 2026-10-08 New ticket page: creation rules, draft handling, section data location

- **Decision:** At creation only Title (3-120 chars) and Requirements are required (epic: also Summary). Sections the type marks "yes" but that are not required at creation show "needed before the plan gate" and may stay empty; `absent` sections are not shown (their text is kept in the draft when switching type, but never sent). `parent` must be an epic in the same workspace; new tickets are `backlog`, key = `max(key)+1` zero-padded to 4; viewers get 403. The per-type section table and labels (spike: "Questions to answer", "Findings") live in `src/api/sections.ts` (Ruling R1), not `src/mocks/`. The mock emits `ticket.created` then `people.set` (only when people were chosen) and persists created tickets in `PersistedV2.created`. The draft autosaves to `localStorage` key `orch.dashboard.new-ticket.draft.<person>.<workspace>`; leaving with unsaved text asks "Discard this draft?" through TanStack `useBlocker`. `c` (no modifiers, not typing) opens the page; `NewTicketDialog` is removed. Priority, size, parent, owner and visibility use native `<select>`s; labels use a Popover + Command combobox.
- **Why:** Keeps the brief's rules in one place the UI and mock share; native selects are accessible and testable without Radix portals.
- **Revert:** Delete `src/app/pages/new-ticket`, the `/tickets/new` route, `createTicket` in `store.ts`, `client.ts`, `router.ts`; restore `NewTicketDialog` from git history (commit before "new ticket: type-aware sections").

## 2026-10-08 New ticket UI test asserts "Backlog", not "backlog"

- **Decision:** The brief's `getByText('backlog')` after creating became `findByText('Backlog')`, because the ticket page's `StatusChip` renders `STATUS_LABEL` ("Backlog").
- **Why:** No lowercase "backlog" text exists on the ticket page.
- **Revert:** Lowercase the chip label (not recommended).

## 2026-10-08 Full command palette: save-view hand-off, ticket actions, test adjustments

- **Decision:** (1) "Save view…" navigates to `/tickets` and opens the dialog through a one-shot request (`src/app/pages/tickets/saveViewRequest.ts`) that `SavedViews` consumes on mount or on an event, not through a `?saveView=1` URL param, because saved views are exactly the search params and a flag would leak into every view. (2) "On this ticket" shows Comment and Ask a question to anyone who is not a viewer, Move to… to owners/maintainers, and Approve/Give verdict from `availableActions`; Claim and Release are never offered, since the ticket page says only agents claim and release. Comment, Ask and Move use sub-prompts inside the palette (type the text, Enter; Backspace on empty goes back). Approve and Give verdict open core's `SignDialog` (rendered by the palette). (3) Shortcuts live in `src/app/shell/shortcuts.ts` (`g t`, `g b`, `g l`, `g a`, `c`, `[` display only). (4) The brief's tests were adapted: DEMO-0043's title is "Load tariff tables as dbt seeds" (found by body text "billing"), the Board page has no h1 (topbar title is asserted), and the sign-dialog test uses DEMO-0041 which has a verdict pending.
- **Why:** Keeps saved views clean; matches the existing permission rules; the brief's assertions did not match the fixtures.
- **Revert:** Delete `saveViewRequest.ts` and its use in `SavedViews.tsx`; restore `CommandPalette.tsx` and `NewTicketShortcut` from the commit before "palette: tickets, ticket actions".

## 2026-10-09 Agents: separate GrantDialog for signing grants

- **Decision:** Issuing and revoking grants are signed in `src/app/pages/agents/GrantDialog.tsx`, not in `SignDialog`. It reuses the same Touch ID simulation (600 ms) and the button text "Sign with Touch ID".
- **Why:** `SignDialog` is bound to a `TicketDocument` and a gate/question action; grants belong to the workspace and have no ticket.
- **Revert:** Generalise `SignDialog` to accept a non-ticket action, then delete `GrantDialog.tsx` and use it from `agents/index.tsx`.

## 2026-10-09 Agents: the signing dialog closes on Sign, progress goes to a toast

- **Decision:** Clicking "Sign with Touch ID" closes the grant dialog at once; `useSignGrant` shows a loading toast, waits for the simulated Touch ID, calls the API, then replaces the toast with success or the error message.
- **Why:** The brief's test queries the grants row right after clicking Sign, and an open Radix modal hides the page from assistive tech (aria-hidden), so the row would not be found; the result also belongs on the page.
- **Revert:** Move the phase/error state back into `GrantDialog` (as in `SignDialog`) and keep it open until the request finishes; adapt the test to wait for the dialog to close.
