# Orch Remote: checking it on a real phone

The automated run (below) proves the path with one Chromium and two dashboards on one computer. It cannot prove what
needs a real person and a real device: Face ID, a phone's browser and its home-screen app, the phone's network. This
page is the short list to repeat by hand, with what to expect at each step and what to report.

## What the automated run already proves

Run against a local TIX server, the real relay tool, two workspaces started with `--remote`, and one Chromium:

| Step | Proved by the run |
| --- | --- |
| Pair: link, fingerprint on both sides, owner approves in the Remote tab | yes (a spent link pairs nothing) |
| Open a workspace, click through pages and a ticket, switch between two workspaces | yes |
| Presence and the three counts (sessions, in progress, needs you) follow the workspace | yes |
| A POST sent twice with the same bytes runs once | yes |
| Look and Decide devices get the readable refusal on a form, a terminal and keys | yes |
| Operate watches a terminal live and cannot type | yes |
| Type without an unlock is refused and types nothing | yes |
| Revoke mid-stream: the browser shows "removed from that computer", the host stops answering it | yes |
| Stop (goodbye), restart, take over after a crash, "Not answering" and "Lost" in words | yes |
| Typing after an unlock, the AI Factory permission card and Start with a fresh confirmation | **not yet**: written and skipped until the unlock sheet is in the orch-tix checkout |
| A real Face ID, Touch ID, Windows Hello or PIN answer | **needs a real device** |
| The phone's own browser, home-screen app, cellular network, lock screen | **needs a real device** |

### Next, when the unlock sheet and the streams are in orch-tix

Two tests in `tests/e2e_remote/test_remote_e2e.py` are written as skips: `test_typing_after_an_unlock` and
`test_the_factory_start_needs_a_fresh_confirmation`. Their skip check looks for an `unlock*.js` file in the orch-tix
checkout, so once the unlock sheet (orch-tix #97) and the streams work (orch-tix #96) are on orch-tix main they stop
skipping by themselves and fail until their bodies are written: give the Type device a Chromium virtual authenticator
(CDP `WebAuthn.addVirtualAuthenticator`, platform, user-verifying) before pairing so the host registers a credential,
answer the sheet, then type into the `BRAVO-1` session and read it back from the private tmux server; for the Factory,
make a Factory epic in a workspace (the repo's fakes, no API credit), and answer the sheet for the permission card and
for Start. Also switch `test_a_type_device_without_an_unlock_is_refused_and_types_nothing` to expect `lease_required`
once a credential exists (today the browser has none, so the host answers `assertion_failed`). Then steps 6 and 7
below are proved by the run, and only the real Face ID prompt stays for a person.

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

1. **Pair.** In the dashboard on the computer: Workspace & addons, the Remote tab, scope **Operate**, "New pairing
   link". Open that link on the phone (type it or send it to yourself; it works once and lives 10 minutes). Tap **Pair**.
   - Expect: the phone shows a fingerprint of four-character groups.
   - Compare the **last group** with the one the Remote tab shows for the waiting device; type it into the Remote tab
     and approve.
   - Expect: the phone says **Paired**; the Remote tab lists the device with scope Operate.
   - Report if: the groups differ (reject it), the phone says the link was used by someone else, or nothing happens in
     a minute.
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
   - Expect: the screen, updating while the agent works. Lock the phone and unlock it: it should reconnect.
   - Report: how long the stream took to come back after unlocking.
6. **Type, with Face ID.** *Needs the unlock sheet in the TIX app (not in the current orch-tix main).* On the computer,
   raise the device to scope **Type** (tick the Type switch). On the phone, type a few characters into the terminal.
   - Expect: a sheet showing the exact action; Face ID (or the passcode); then the characters appear in the
     session on the computer. The unlock lasts 15 minutes, a restart of the dashboard ends it.
   - Without Face ID, expect a readable refusal and nothing typed. Report if text appeared without a prompt.
7. **Start the AI Factory, with Face ID.** *Same unlock sheet.* On a ticket that is a Factory epic, the permission card
   and **Start** each ask for a fresh Face ID showing the exact text.
   - Expect: refused without it ("needs your phone to confirm"), done with it, and the computer's Remote tab listing
     "Confirmed on <device>".
8. **A form at the wrong scope.** Lower the device to **Look** on the computer. On the phone, try to add a comment.
   - Expect: "This browser is not allowed to do that on that computer." and no comment on the computer.
9. **Revoke.** Keep a terminal open on the phone. On the computer, Revoke the device in the Remote tab.
   - Expect: within seconds the phone says "This browser was removed from that computer", the terminal stops, and
     reopening the workspace on the phone does not work until it is paired again.
10. **The computer goes away.** Close the laptop lid (or turn off Wi-Fi) for one minute, with the workspace open on
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

`plugins/orch-core/scripts/e2e-remote.sh` does the same, building the scratch environment (`.e2e-venv`) on first use. Needs `git`, Chromium for Playwright, `tmux`
(without it the terminal tests skip) and `uv`. The run takes about ten minutes and writes pictures of failures to
`/tmp/orch-e2e-remote-snaps` (`ORCH_E2E_SNAPS`). It starts TIX on a free loopback port as `localhost`, two dashboards
and a private tmux server (never your own `orch` one), and removes them at the end.

Everything the run needs that it cannot find skips it with a sentence naming what is missing.
