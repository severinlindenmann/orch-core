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

## 2026-10-08 Addon manager review fixes: grants bind the package, ops are verified, inactive addons are inert

- **Decision:** (1) A grant is valid only if `version` and `package_sha256` match the installed package and every installed capability is in the grant (`grantCovers`); `WorkspaceAddon` now stores `package_sha256` and `capabilities` from `addon.installed`/`addon.updated`. (2) `grant` and `update` requests carry the displayed `{version, package_sha256, capabilities}`; any difference from current values is 409 `addon.changed` (wrong version on a grant stays 409 `addon.version_mismatch`). (3) Actions (`POST /api/addons/:name/actions/:id`) resolve the workspace as before and return 409 `addon.inactive` unless the addon is active there; decisions moved to `GET /api/workspaces/:ws/addons/decisions` (active addons only; viewers get none), Today uses it. (4) `canUsePty(manifest, workspaceAddon)` reads the per-workspace state; `selectContributions` returns nothing without a workspace; catalog entries are `enabled:false` and become live global manifests (`enabled:true`) when a workspace installs them.
- **Why:** Security review: a version-only grant could be replayed on swapped bytes, the dialog and the signed op could diverge, and disabled addons still acted.
- **Revert:** Restore the old `addonStatus(version, granted, enabled)`, drop the sha/capabilities checks in `addonOp`, and the per-workspace decisions route.

## 2026-10-08 Per-addon settings forms (Task 12)

- **Decision:** (1) `/settings/addon/:name` renders the addon's `settings` form node in `AddonFrame`; the fixtures bind `formData` to `{"$ref":"addon.settings"}`, so saved values come back through the addon state. `save_settings` (all six mock modules, wiki included) stores `body.formData` in `state.settings` and returns "Settings saved." Seeds hold the old fixture defaults; estimate's default scale is now `linear` and its enum gained `linear`. (2) The mock refuses `save_settings` with 403 `forbidden` unless the viewer is an owner; the UI passes `readOnly` to `AddonNode`, which disables every field and the Save button for non-owners. (3) An inactive addon shows "Enable <Title> to change its settings." (4) The rjsf theme's enum widget is now a native `<select>` (was a Radix Select) and its submit button honours `disabled`; the page renders the form only after the addon state has loaded, so the form is not remounted while the user edits. (5) `renderApp` gained `opts.setup(store)`.
- **Why:** The brief fixes the behaviour but not the seeds, the owner check, or the widget; a native select is accessible and testable.
- **Revert:** Restore the Radix `SelectWidget`, drop `readOnly` and the 403 check in `runAddon`, and put the literal `formData` back in `addons.json`.

## 2026-10-08 Disabled-addon behaviour everywhere (Task 13)

- **Decision:** (1) Inactive means disabled, needs_grant or uninstalled in the current workspace, judged by `addonActive()` on every surface. (2) A ticket with `addons.<name>` data for an inactive addon shows a collapsed, greyed "<Title> · inactive" item (with the A badge) in the rail; opening it shows the raw JSON read-only. The Raw tab keeps all data and adds a note naming inactive addon keys. (3) `/addon/<name>/<page>` for an inactive addon says "<Title> is not enabled in <workspace>." with an "Open the addon manager" link for owners, and "Ask an owner to enable it." for everyone else. (4) `useSlot` and `AddonPage` fetch addon state only for active addons. (5) Review fixes: board lane import passes `ws`; `sameSet` compares as sets in both directions; a regression test pins the 404 for an unknown `ws` on the action route.
- **Why:** Hiding an addon must not hide the data it wrote, and a dead link should explain itself.
- **Revert:** Remove `InactiveAddonData` from `Rail.tsx`, the inactive branch in `AddonPage.tsx` and the note in `Raw.tsx`.

## 2026-10-08 Shared API error toasts, viewer reasons, empty columns (Task 14)

- **Decision:** (1) `toastApiError(err, fallback?, id?)` in `src/app/toast.ts` is the only way a failed request becomes a toast: ApiError message plus `hint` as description; anything else gets the caller's fallback (default "Something went wrong."). All ad-hoc `toast.error` calls (Today, Board, addon lane, tickets, saved views, settings, palette, topbar, SignPrompt) use it. Inline dialog errors (SignDialog, DangerZone, New ticket server error) stay inline. (2) The ticket page has no human Claim action (claims are agent-only), so Claim/Release stay disabled for everyone; for viewers the tooltip and an accessible description read "Viewers cannot change tickets." (the viewer reason wins over the agents-only text). (3) Board empty column: "Nothing here", plus "Create a ticket (c)" for backlog, or the filter message when filtering. (4) New ticket: a ref guard stops a second Cmd+Enter while a create is in flight.
- **Why:** Brief's standard for the new pages; the guard was a Task 7 review carry-over.
- **Revert:** Inline the old `toast.error` calls, restore the old ClaimBox tooltip text, the old column message, and drop `submitting` in `new-ticket/index.tsx`.

## 2026-10-08 Full workspace switcher (Task 15)

- **Decision:** (1) The switcher is a popover (`WorkspaceSwitcher.tsx`) in place of the sidebar dropdown: per workspace a `role="group"` named "PREFIX · Name" with your role, the needs-you count pill, a muted "Relay not connected" dot and the top 2 needs-you items as links. Previews come from `GET /api/workspaces/:ws/today` per workspace (same `['today', ws]` query key as Today), no new endpoint. (2) `switchWorkspace(id, {ticket?})` in `workspace.tsx` is the one switch function used by the popover, the palette's Workspace group and ⌘/Ctrl+1–9 (registered as `workspace.1`..`workspace.9` in `shortcuts.ts`; not while typing or in a dialog). Switching keeps `/board`, `/tickets` (search kept), `/agents`, `/settings/*`; `/ticket/:key` goes to `/tickets` with a toast "<KEY> is in <name of the ticket's workspace>" (found by key prefix; if the target is that workspace the ticket stays); `/addon/:name/:page` stays if `addonActive(target, name)`, else goes to Today with "<Title> is not enabled in <workspace>". (3) The chosen workspace id is stored in `localStorage` (`orch.workspace`, try/catch). (4) The palette's "On this ticket" actions now gate on the viewer's role in the ticket's own workspace (`useViewer`), not the current workspace. (5) Fixture: `INT-0007` got one task (T1) so the INT workspace has a plan-approval item for Severin (INT previously had nothing needing him, so the previews could not be shown). (6) Task 14 carry-over: the `key` on the two Today option-button maps moved to `DisabledReason`.
- **Why:** The brief asks for previews, page-keeping and one shared switch function; INT needed seed data to demonstrate previews.
- **Revert:** Restore the `DropdownMenu` in `Sidebar.tsx`, drop `switchWorkspace` and the `workspace.N` shortcuts, remove T1 from `INT-0007` in `other-workspaces.json`.

## 2026-10-08 Stabilise the test suite under parallel load

- **Decision:** (1) `TOUCH_ID_MS` is 0 when `import.meta.env.MODE === 'test'` (600 ms in dev/build, UX unchanged). (2) `useLiveUpdates` does not start its 2 s poll in tests. (3) vitest `testTimeout` is 10000 (default 5000). Debounces (palette 150 ms, tickets 200 ms) and the 4 s `asyncUtilTimeout` are unchanged.
- **Why:** Each signing test slept 600 ms of real time (alone: 0.9 to 2.5 s per test); with parallel workers on a loaded machine that margin vanished and tests hit 5 s. The poll added refetch churn. Signing tests now take about 0.3 s. No test asserted on the in-progress state.
- **Revert:** Restore `export const TOUCH_ID_MS = 600`, remove the test-mode early return in `live.ts`, and drop `testTimeout` from `vite.config.ts`.

## 2026-10-08 One permissions table for the mock and the UI (phase 1 fix #3)

- **Decision:** (1) `src/api/permissions.ts` holds `can(role, permission)` with one minimum role per permission: `ticket.create`, `ticket.act`, `view.share`, `addon.action` = member; `ticket.move`, `ticket.label`, `grant.issue`, `addon.decide` = maintainer; `question.answer.any`, `grant.revoke.any`, `settings`, `addon.manage` = owner. Plus `atLeast(role, min)`, `canRevokeGrant(role, grantPerson, person)` and `roleOf(workspace, person)`. `useRole(workspaceId?)` (`src/app/useRole.ts`) gives the viewer's role in the current (or a named) workspace. The mock routes, the store and every UI gate use them; the ad-hoc `members.find(...)?.role` checks are gone. (2) Grants: only owners and maintainers issue; a maintainer revokes their own, an owner any. Members are now refused (403) by the mock, as the Agents page and the role explainer already said. (3) `roleMeets` accepts an undefined role (false).
- **Why:** Phase review: the rule was written about 13 times and the mock let members issue grants while the UI and the role help said they could not.
- **Revert:** Delete `permissions.ts`/`useRole.ts` and restore the inline role checks; in `issueGrant`/`revokeGrant` go back to "not a viewer".
