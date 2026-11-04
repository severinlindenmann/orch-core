# End-to-end test: what to try, and what you should see

A checklist for the owner's own end-to-end test of orch v2: fresh install → workspace → dashboard → signing → (later)
the phone over the relay. It doesn't repeat the install commands; those are in [first-use.md](first-use.md), with what
was verified and how. Here each step names the runbook step to follow, then **what to check** and **what counts as a
failure**. Note anything odd with the time (`date`) so it can be matched against `~/.config/orch/host/host.log`.

Written 14 Oct 2026 against `develop`. Parts A–C cover core and the dashboard. Part D (phone and relay) is written
from the decided design and becomes runnable when the relay pairing ships.

> **Choose your signing path first.** Click-to-sign (convenience custody) is refused by the host once the workspace has
> a second device or is paired with a relay (first-use step 6, "Conditions"). If you plan Part D on the same
> workspace, either skip step B4 (click) and sign in the terminal, or use a separate throwaway workspace for B4 and
> turn convenience off (`orch host convenience off --workspace TEST`) before pairing.

## Part A: install and first workspace (first-use steps 1–4)

| # | Do | Check | Failure looks like |
|---|---|---|---|
| A1 | Step 1: venv outside any checkout, `pip install ./packages/core ./addons/dashboard/server` | `pip list` shows `orch-core 2.0.0.dev0` and `orch-dashboard 0.1.0`; no separate `npm run build` was needed | "the dashboard UI could not be built" (Node 24 missing) |
| A2 | Step 2: `orch setup` identity | recovery code shown once; you wrote it down | |
| A3 | Step 3: `orch host enable` (or the foreground host) | `orch host status` says the host is running | `host.busy`, a refusal naming a checkout path |
| A4 | Step 4: `orch setup` (prefix TEST), `orch-dashboard addon-manifest --write .`, `orch addon grant dashboard` | `orch addon list`: `dashboard 0.1.0 active` | the dashboard later says `addon.disabled` |

## Part B: the dashboard (first-use steps 5–7)

| # | Do | Check | Failure looks like |
|---|---|---|---|
| B1 | `orch dashboard open` | the browser opens on Today; the workspace switcher shows `TEST`; a second `orch dashboard open` opens a second tab that is signed in too | a login screen in your own tab; a Python traceback |
| B2 | Make a ticket with a grant (step 7 commands) | it appears in Tickets and on the board **without a reload** (live updates) | it only shows after a reload |
| B3 | Terminal path: press "Approve requirements" | a dialog with a code like `HE6C-XKQH`; `orch sign <code>` in a terminal; the dialog turns to "Signed" by itself | the dialog stays open after the terminal says `signed` |
| B4 | Click path (only if you chose it, see the box above): step 6, then press an action | the card shows the risk line verbatim, the lines being signed and a fingerprint; **Sign** is greyed for about a second, again after you switch windows; one click signs | Sign works instantly; a terminal code is offered while click is on |
| B5 | Press the same action again right after it was signed | "Already done — signed N min ago" with an explicit "Do it again" | a second signature without that screen |
| B6 | Click a link to the dashboard on another site: put `http://127.0.0.1:<port>/w/TEST/tickets` in a GitHub comment (preview) or any web page and click it. Typing or pasting it into the address bar doesn't count; that's your own navigation | the tab says "This page was opened from another site" and is **not** signed in; a reload lands on `/`, not on the link | the foreign tab shows your tickets |
| B7 | Settings: General, Members, Gates, Records; the switcher with a second workspace if you have one | every page loads; settings changes ask for a signature | an error box with a raw host error |
| B8 | Quick tasks (`/w/TEST/quick`): add one (two signatures), "Make a normal ticket", close one | the dialogs show which labels are removed and kept; the list updates live | |
| B9 | Records (Settings → Records): Commit; with a remote, Commit and push | the push confirm shows the export warning with the **last known** destination; a failure shows the tool's own output in a separate box | the push publishes without the warning |
| B10 | Command window (once M7 is merged): open your own shell, type, open the same terminal in a second tab | the second tab is read-only with "Take control"; closing the writer releases control | two tabs both typing; output in the server log |

## Part C: stop, restart, undo (first-use step 8)

| # | Do | Check |
|---|---|---|
| C1 | `orch dashboard stop`, then `orch dashboard open` | old tabs ask to open again; the new tab is signed in |
| C2 | Restart the host (`orch host disable` + `enable`, or Ctrl-C the foreground host and start it again) | the dashboard shows the host as unavailable while it is down and recovers by itself |
| C3 | Undo: step 8 table | nothing of orch keeps running (`orch host status`: not running); your workspace folder and its history stay |

## Part D: the phone over the relay

**Status: planned, not run yet.** Written 15 Oct 2026 from the decided design: R6 draft #473 §11A.15, orch-52's
`pair/1` decisions and the dashboard's "Pair a phone" screen (#480). It becomes runnable when three things are in: the
relay link (R5) and pairing (R6) on the host, orch-52's `pair/1` ops, and the orch app with the `orch://pair` handler.
Until then, Settings → Devices says "Pairing a phone is not available on this host yet". Command names marked
*(R6 draft)* may still change; this section gets updated when they land.

**Before you start:**
- Use a workspace where click-to-sign is **off** (`orch host convenience off --workspace TEST`). If you used
  click-to-sign there before, pairing still works, but only with an up-to-date orch app on the phone.
- Your Mac's primary key exists (`orch keys primary init`, once, in a terminal; it asks for your recovery code)
  *(R6 draft)*.
- The workspace is linked to the relay (the R5 link command) *(R6 draft)*.

| # | Do | Check | Failure looks like |
|---|---|---|---|
| D1 | Dashboard: Settings → Devices → "Pair a phone" | no block; if click-to-sign was used before, a warning that only up-to-date apps can pair. "Create pairing link" | "Turn click-to-sign off first" (it's still on); `relay.not_linked` |
| D2 | "Create pairing link", then "Show QR" | a QR and a countdown; nothing secret is visible before "Show QR" | the link visible before the click; a QR that never expires |
| D3 | Scan the QR with the orch app (or open the `orch://pair?…` link on the phone) | the app shows a review screen and connects only after you accept there; the dashboard switches to "compare the code" and the QR disappears | the dashboard keeps showing the QR after the phone connected |
| D4 | Type the six characters the phone shows into the dashboard | Approve is enabled only when the codes match; a wrong code uses up one of 3 attempts | Approve works without a match; a typo ends the pairing at once |
| D5 | Approve, then in a terminal on the Mac: `orch host relay pair --resume --offer <id>` (the dashboard shows the exact command) *(R6 draft)* | the terminal shows a second code; it equals the code the dashboard shows; confirm with your recovery code | the codes differ (reject!) |
| D6 | Then `orch sign <CODE>` with the code the dashboard gives you | the dashboard goes through "adding" to "paired"; the phone shows the workspace | the dashboard stays on "signing" after the terminal says `signed` |
| D7 | Settings → Devices | the phone is listed (label as the phone named itself, paired just now, relay yes) | the phone missing; the Mac listed as a phone |
| D8 | On the phone, approve a gate or answer a question on a ticket | the dashboard shows it live (Today, the ticket's history), signed by the phone's device | it appears only after a reload, or never |
| D9 | Try click-to-sign in the dashboard on this workspace | refused: a workspace with a phone signs with the phone or in the terminal | Sign works by click |
| D10 | Remove the phone: the "Remove this device" disclosure shows the command; run it in a terminal (it asks for your recovery code) | the device leaves the list; the phone loses access | the phone can still act on the workspace |

**Also check (security):**
- While the QR shows, switch to another app and back: the QR may stay, but a lost link is never fetched again; cancel
  and create a new one.
- Let a link expire (or press Cancel): the phone then gets "link used or expired", and the dashboard shows "expired"
  or "cancelled".

## When something fails

Write down the step number, the time, what you saw (a screenshot of the dashboard is fine), and the last lines of
`~/.config/orch/host/host.log`. Don't send the contents of `~/.config/orch` or the recovery code to anyone.
