# Orch Remote: checking it on a real phone

The automated run (below) proves the path with Chromium, a virtual platform authenticator and three dashboards on one
computer. It cannot prove what needs a real person and a real device: the Face ID prompt itself, Safari, a synced
passkey, locking and waking the phone, and a 15-minute lease in real time. This page says what the run already proves and
gives the short list to repeat by hand, with what to expect at each step and what to report.

## What the automated run already proves

Run against a local TIX server (from an orch-tix checkout), the real relay tool, workspaces started with `--remote`,
the real TIX app JavaScript in Chromium, and a CDP virtual authenticator standing in for Face ID. No agent is started
and no API credit is spent: the only harness is a fake one that echoes its input.

| Step | Proved by the run |
| --- | --- |
| Pair: link, fingerprint on both sides, owner approves in the Remote tab; a spent link pairs nothing | yes |
| Register the platform credential on the Register button (the computer lists "passkey on this device") | yes |
| Open a workspace, click through pages and a ticket, switch between workspaces | yes |
| Presence and the three counts (sessions, in progress, needs you) follow the workspace | yes |
| A POST sent twice with the same bytes runs once | yes |
| Look and Decide devices get the readable refusal on a form, a terminal and keys; Operate watches and cannot type | yes |
| Watching a terminal draws no sheet, posts no size and shows no typing banner; its screen updates live | yes |
| Typing: one sheet with the host's text ("Type into terminal NAME for 15 minutes ..."), keys arrive in order, a second burst inside the lease needs no sheet, after the lease (shortened to 30 s) a new sheet, a cancelled sheet types nothing and says so | yes |
| A new terminal session and an agent start each have their own sheet (also inside a lease), start exactly once, and nothing starts when the sheet is cancelled or Face ID fails | yes |
| A start whose text is too long for the phone is refused with the fixed sentence and nothing runs | yes (look-alike spacing is covered by orch-tix's own tests: the host turns those in a title into plain spaces) |
| Factory: Grant once needs a sheet showing the command; Deny and Pause need none; the epic verdict needs a sheet naming what it closes; a cancelled sheet changes nothing | yes |
| Revoke mid-stream and mid-typing: the browser says "removed from that computer", the host stops answering, nothing more is typed | yes |
| A revoked browser is told at once that its pairing link was refused, and pairs again only with a new key, a fresh link and the owner's approval | yes |
| Stop (goodbye), restart, take over after a crash, "Not answering" and "Lost" in words | yes |
| A stream that keeps dying does not hammer the host (5 requests in 20 s with every answer refused) | yes |
| Factory Start (the charter sheet), Grant for the epic, a Decide device with a valid assertion trying to close a child | host side only (`tests/test_factory_bridge.py`); not driven in the phone yet |
| The Face ID prompt itself, Safari, a synced passkey, lock and wake, the 15 minutes in real time | **needs a real device** |

Known problems the run found and keeps as expected failures (strict xfail): a page that was left keeps re-posting its
confirmed keys (orch-core#328).

## Before you start

1. On the computer: the relay addon enabled and trusted in the workspace, `sharing_path` set, a space for the
   workspace, this device approved (`docs/remote.md`, "What is checked before anything binds").
2. `orch serve --remote` in your own terminal, in the workspace. It prints `remote: reachable from your paired devices`.
3. Terminals (for the steps below): the `terminals` addon enabled in the workspace, `tmux` installed, one agent
   session running (start one with Start agent on a ticket).
4. On the phone: signed in to TIX in its browser.

## Steps

Report, for every step: what you did, what the phone showed, what the computer showed, and a screenshot of anything
that surprised you. Do not paste a pairing link, a device token or the fingerprint of a device you did not mean to
pair.

1. **Pair, with Face ID.** In the dashboard on the computer: Workspace & addons, the Remote tab, scope **Type** (tick
   the Type switch), "New pairing link". Open that link on the phone (type it or send it to yourself; it works once and
   lives 10 minutes). Tap **Pair**.
   - Expect: the phone shows a fingerprint of four-character groups and a button to register this browser with Face ID
     or device unlock. Press it: the real Face ID prompt (the one thing the automated run cannot show).
   - Compare the **last group** with the one the Remote tab shows for the waiting device (it also says "passkey on
     the device"); type it into the Remote tab and approve.
   - Expect: the phone says **Paired**; the Remote tab lists the device with scope Type.
   - Report if: the groups differ (reject it), no Face ID prompt comes, a synced passkey is offered (say which), the
     phone says the link was used by someone else, or nothing happens in a minute.
2. **Open and look around.** On the phone open TIX, **Workspaces**, then the workspace, **Open**.
   - Expect: the dashboard's Today page in a frame, within a few seconds; the Board and a ticket open when you tap them.
   - Report if: the frame stays empty, shows "download", or any text says the computer is not answering while it is.
3. **Counts and presence.** On the computer, start or end an agent session, or move a ticket to in progress.
   - Expect: within about 10 seconds the workspace row on the phone's Workspaces page shows the new numbers
     (sessions working, in progress, needs you).
4. **Switch.** Pair a second workspace the same way (another `orch serve --remote`, another folder). Open it, then the
   first one again.
   - Expect: one frame at a time, each showing its own tickets.
5. **Watch a terminal.** In the dashboard on the phone open Terminals, then the session.
   - Expect: the screen, updating while the agent works, and no Face ID prompt. Lock the phone and unlock it: it
     should reconnect.
   - Report: how long the stream took to come back after unlocking.
6. **Type, with Face ID.** In the terminal press Type, enter a line, send it.
   - Expect: a sheet that says "Type into terminal NAME for 15 minutes" (and that it covers every terminal on the
     computer), Face ID, then the line appears in the session on the computer. A second line within the 15 minutes
     needs no prompt; after 15 minutes (real time) it asks again. A restart of the dashboard ends the lease.
   - Cancel the sheet or fail Face ID once: expect "Not confirmed, so nothing was done." and nothing typed.
   - Report if text appeared without a prompt, or the prompt came back inside the 15 minutes.
7. **Start a session and an agent, with Face ID.** On the Terminals page press New session; on a ticket press Open in
   Mission Control. Each asks for its own Face ID with the exact text ("Start a new terminal session ...", "Start an
   agent. Harness ..., Ticket REF titled: ..."), even right after step 6.
   - Expect: it starts once after you confirm, and not at all when you cancel.
8. **The AI Factory, with Face ID.** On a Factory epic's Today card: Grant once shows the exact command and asks
   for Face ID; Deny and Pause do not; the epic's verdict names the children it closes and asks.
   - Expect: the computer's Remote tab lists "Confirmed on <device>" for each confirmed action.
9. **A form at the wrong scope.** Lower the device to **Look** on the computer. On the phone, try to add a comment.
   - Expect: "This browser is not allowed to do that on that computer." and no comment on the computer.
10. **Revoke.** Keep a terminal open on the phone and type a line. On the computer, Revoke the device in the Remote tab.
    - Expect: within seconds the phone says "This browser was removed from that computer", the terminal stops, and
      reopening the workspace on the phone does not work. Open the pairing link again: expect "The computer refused the
      pairing link" at once; to use the phone again, sign out and in (a new key), make a fresh link and approve.
11. **The computer goes away.** Close the laptop lid (or turn off Wi-Fi) for one minute, with the workspace open on
    the phone.
    - Expect: "Not answering" in words and Open disabled; after waking, the workspace returns by itself. If you killed
      the dashboard, the next `orch serve --remote` says another host holds the workspace until you add `--take-over`.

## Running the automated run

It is not part of the normal test run: every test carries the `e2e_remote` marker and is skipped unless selected.

```
# once: the packages (in a scratch environment, never machine-wide)
uv venv .e2e-venv --python 3.13
uv pip install --python .e2e-venv/bin/python -e 'plugins/orch-core[dashboard,dev]' pywebpush pytest-playwright
.e2e-venv/bin/python -m playwright install chromium     # or reuse the Chromium you already have

# every time: a checkout of orch-tix (ORCH_TIX_DIR; by default a folder named orch-tix next to this checkout)
ORCH_TIX_DIR=/path/to/orch-tix \
  .e2e-venv/bin/python -m pytest plugins/orch-core/tests/e2e_remote -m e2e_remote -p no:xdist
```

`plugins/orch-core/scripts/e2e-remote.sh` does the same, building the scratch environment (`.e2e-venv`) on first use.
Needs `git`, Chromium for Playwright, `tmux` (without it the terminal tests skip) and `uv`. The run takes about fifteen
minutes and writes pictures of failures to `/tmp/orch-e2e-remote-snaps` (`ORCH_E2E_SNAPS`). It starts TIX on a free
loopback port as `localhost`, three dashboards and a private tmux server (never your own `orch` one), and removes them
at the end. Three limits are shortened for the run only, in the launcher (`tests/e2e_remote/serve_launcher.py`): the
typing lease (30 s instead of 15 minutes, so its end can be watched), the fresh-confirmation rate limit (D9: 6 per 10
minutes, raised because the run confirms more than that; the host's own tests cover it) and the human-only check of the
dashboard command (replaced by the same two functions the repo's tests replace).

Everything the run needs that it cannot find skips it with a sentence naming what is missing.
