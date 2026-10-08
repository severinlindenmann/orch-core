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

## 2026-10-09 Settings: one shared signing primitive (`SignPrompt` + `useSignedAction`)

- **Decision:** Ticket-independent signatures (grants, workspace settings) share `src/components/sign/SignPrompt.tsx`: `SignPrompt` (the "what you sign" dialog, button "Sign with Touch ID", closes on Sign) and `useSignedAction` (loading toast, 600 ms simulated Touch ID, request, refetch, success or error toast). `GrantDialog` was refactored onto it; `SignDialog` (ticket-bound, phases inside the dialog) only imports the shared `TOUCH_ID_MS`.
- **Why:** The brief forbids a third copy of the Touch ID simulation. `SignDialog` keeps its own phases because its tests and error display live inside the dialog.
- **Revert:** Inline the prompt back into `GrantDialog.tsx` (git history before "settings:") and inline `useSettingsSign` in the settings tabs.

## 2026-10-09 Settings: gate approvers are `owner`, `maintainer` (owners or maintainers) or `reviewers`; no per-person approvers

- **Decision:** The gate policy UI offers three approver groups. `maintainer` means "owners or maintainers" (`roleMeets` in `store.ts`, mirrored in `ticket/actions.ts`); the fixtures' `owner` and `reviewers` keep their meaning. Approving by named people is not built. The policy sentence is built from the policy (`Plan needs 1 approval from owners or maintainers, not the assignees.`). Each change (count, approvers, "Not the assignees") opens its own sign prompt; the sentence shows the saved policy and updates after signing. The "Open approvals stay valid; new approvals use the new policy." note is shown once at the top of the page.
- **Why:** The fixture shape is `{approvers, count, not?}` with a role string; a role group needs no new data. People-based approvers would need a list field on the gate and the ticket UI.
- **Revert:** Drop `maintainer` from `APPROVERS` in `Gates.tsx` and `APPROVERS` in `mocks/router.ts`; restore `role !== policy.approvers` in `store.canApprove` and `ticket/actions.ts`.

## 2026-10-09 Settings: owner-only is enforced in the mock, last-owner and self rules return 409

- **Decision:** `POST /api/workspaces/:ws/settings` returns 403 `forbidden` ("Only owners change settings.") to any non-owner, for every op. Demoting the last owner is 409 `member.last_owner`, removing yourself 409 `member.self`, `member.add` with role `owner` 400 (promote afterwards with `member.role`), `gate.policy` count outside 1..3 is 400. In the UI non-owners see all controls disabled plus the text "Only owners change settings."; the last owner's Role select is disabled with a tooltip ("You cannot demote the last owner. ..."). Rename is not signed (the brief signs every op except rename).
- **Why:** The UI is a convenience; the mock stands in for the host that must refuse regardless.
- **Revert:** Remove `postSettings` and the `/settings` and `/identity` routes in `src/mocks/router.ts`.

## 2026-10-09 Settings: extra `archive` op, member devices and last seen from fixtures, fake identity

- **Decision:** (1) Archiving is `{op:'archive', prefix}`: wrong prefix is 400, right prefix is always 409 `cli_only` with "Archiving is CLI-only: `orch workspace archive`" (the UI types the prefix first). (2) `Member` got optional `devices` and `last_seen`; the fixture sets them for the three people, members added in the UI start at 0 and "never". (3) Identity: `uuid` is the workspace id, `created_at` is fixed `2026-08-14T07:42:10Z`, `epoch` 1, `key_fingerprint` a stable fake `SHA256:...` derived from the id. (4) Export workspace builds a JSON download in the browser. (5) `/settings` redirects to `/settings/general`; `/settings/addon/:name` is a placeholder until Task 12; the Addons tab is a placeholder "Addon manager" until Task 11.
- **Why:** The brief lists the table columns and the archive behaviour but no data source for them.
- **Revert:** Remove the `archive` case and `Member.devices/last_seen`; edit the fixture lines in `workspaces.json`.

## 2026-10-08 Addon manager: per-workspace addon state, signed grant and update, enable keeps intent

- **Decision:** (1) `workspace.addons[name]` is now `{enabled, status, installed, granted, version}`, folded from `addon.*` events; `status` is `needs_grant` unless `granted.version === version`, else `active`/`disabled` (`src/api/addons.ts`). `enabled` is the owner's intent and survives an update, so re-granting an updated addon turns it back on ("grant again to turn it back on"). A needs_grant addon counts as inactive everywhere (slots, nav, palette commands, board fields, addon-state 404) through `addonActive()`. (2) New routes: `GET /api/workspaces/:ws/addons` (installed, with this workspace's version/grant/status), `GET .../addons/catalog`, `POST .../addons/:name {op}`. `GET /api/addons` stays global; installing a catalog addon adds its manifest to the global list. (3) Install, enable, disable and uninstall are unsigned; grant and update are signed (events carry `presence:'touchid'`, the grant carries `package_sha256` and the capabilities). A grant must name the installed version (409 `addon.version_mismatch`); enable while needs_grant is 409 `addon.needs_grant`; non-person actors get 403 `human_only`. (4) The global `canUsePty` reads the global manifest's `granted`; the first_party fallback is gone. (5) Catalog ids: worktrees, quick, records, activity, models, factory, schedules (version 0.1.0, fake sha256). Quick tasks has a minimal nav. (6) `SignPrompt` got `confirmLabel` (default "Sign with Touch ID"); the grant prompt says "Grant and sign", the update prompt "Update". Uninstall is a plain confirm dialog, not signed.
- **Why:** The brief fixes the interfaces but not where per-workspace version/grant live or whether re-grant re-enables.
- **Revert:** Remove the addon routes and `addonOp` in the mock, restore `{enabled}` in `workspace-log.ts`/`Workspace`, and swap `addonActive` back to `.enabled`.
