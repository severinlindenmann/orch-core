# End-to-end test: what to try, and what you should see

A checklist for the owner's own end-to-end test of orch v2: fresh install → workspace → dashboard → signing → (later)
the phone over the relay. It doesn't repeat the install commands; those are in [first-use.md](first-use.md), with what
was verified and how. Here each step names the runbook step to follow, then **what to check** and **what counts as a
failure**. Note anything odd with the time (`date`) so it can be matched against `~/.config/orch/host/host.log`.

Written 14 Oct 2026 against `develop`. Parts A–C cover core and the dashboard. Part D (phone and relay) is added when
the relay bridge (streams C/D) and the mobile relay client work end to end.

> **Choose your signing path first.** Click-to-sign (convenience custody) is refused by the host once the workspace has
> a second device or is paired with a relay (first-use step 6, "Conditions"). If you plan Part D on the same
> workspace, either skip step B4 (click) and sign in the terminal, or use a separate throwaway workspace for B4 and
> turn convenience off (`orch host convenience off --workspace TEST`) before pairing.

## Part A: install and first workspace (first-use steps 1–4)

| # | Do | Check | Failure looks like |
|---|---|---|---|
| A1 | Step 1: venv outside any checkout, `pip install ./plugins/orch-core ./addons/dashboard/server` | `pip list` shows `orch-core 2.0.0.dev0` and `orch-dashboard 0.1.0`; no separate `npm run build` was needed | "the dashboard UI could not be built" (Node 24 missing) |
| A2 | Step 2: `orch init` identity | recovery code shown once; you wrote it down | |
| A3 | Step 3: `orch host enable` (or the foreground host) | `orch host status` says the host is running | `host.busy`, a refusal naming a checkout path |
| A4 | Step 4: `orch init --prefix TEST … --host`, `orch-dashboard addon-manifest --write .`, `orch addon grant dashboard` | `orch addon list`: `dashboard 0.1.0 active` | the dashboard later says `addon.disabled` |

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
| B10 | Terminals (once M7 is merged): open your own shell, type, open the same terminal in a second tab | the second tab is read-only with "Take control"; closing the writer releases control | two tabs both typing; output in the server log |

## Part C: stop, restart, undo (first-use step 8)

| # | Do | Check |
|---|---|---|
| C1 | `orch dashboard stop`, then `orch dashboard open` | old tabs ask to open again; the new tab is signed in |
| C2 | Restart the host (`orch host disable` + `enable`, or Ctrl-C the foreground host and start it again) | the dashboard shows the host as unavailable while it is down and recovers by itself |
| C3 | Undo: step 8 table | nothing of orch keeps running (`orch host status`: not running); your workspace folder and its history stay |

## Part D: the phone over the relay (to be written)

Added when stream C/D's relay bridge and the mobile relay client work end to end: pairing the phone with the
workspace over the relay, signing on the phone, the dashboard seeing the phone's signatures live, and what the
dashboard shows for a second device (click-to-sign off, see the box at the top).

## When something fails

Write down the step number, the time, what you saw (a screenshot of the dashboard is fine), and the last lines of
`~/.config/orch/host/host.log`. Don't send the contents of `~/.config/orch` or the recovery code to anyone.
