# Hub service helper (HUB-SV-02 #1396, HUB-SV-03 #1397)

Status: prototype, 30 Oct 2026. Start, stop and restart the hub's parts from the Services page, and read their logs.
Linux VPS first (systemd); Mac and Windows later. The hub stays optional; nothing here needs a domain.

## What runs where

- `orch-hub helper` is a separate process (own unit `orch-hub-helper`, NOT `PartOf` or `BindsTo` `orch-hub`, so it
  survives a hub restart). Same user as the hub; no extra rights beyond the sudoers lines below.
- It manages only the parts on its own machine, listed in `<state>/helper.json` (0600, closed shape):
  `{"v":1,"host_id":"vps1","parts":{"relay":{"label":"Relay","kind":"systemd","unit":"orch-relay.service"}}}`.
  `kind` is `systemd` (system unit through `sudo -n`) or `systemd-user`. A part that is not listed, or runs on another
  machine, keeps today's "run this command" help with Copy.
- Transport: one unix socket `<state>/helper.sock` (0600), peer uid must equal the helper's uid. Never a network port.

## Signed action

Routes (all hub-admin, `webapi/services_api.py`, registered in `features.py`):
`POST /admin/services/{part}/prepare`, `POST /admin/services/{part}/run`, `GET /admin/services/{part}/logs`.

1. `prepare {action, channel_id}`: the helper checks the person is a hub admin now, makes `{nonce, iat}` (single use,
   60 s) and returns `{ctx, digest, text}`. The client never supplies a nonce or time.
2. The console recomputes `digest = SHA-256("orch.hub.service.action/v1\n" || cj(ctx))` from the shown context and checks
   `text_sha256` equals the hash of the sentence on screen (WYSIWYS). `ctx` binds hub_id, host_id, part, action,
   target_version (null unless update), nonce, iat, channel_id, user_id, text_sha256.
3. The admin's own channel key (this browser) signs `"orch/v2/hub-channel-proof|" || digest`; `run {nonce, channel_id, proof}`.
4. The helper consumes the nonce first (any outcome), then verifies: admin membership and revoked channels read straight
   from the access and channel SQLite stores, read only, fail closed; the channel certificate against the CURRENT card
   (rotated or revoked `HK_op` respected); the signature. Only then it runs one fixed argv. Every run and log read is
   appended to `<state>/helper-audit.log` first; an unwritable audit refuses the call.

## Fixed commands and sudoers

Only these argv are ever run (`orch-hub helper --print-sudoers --user orch-hub` prints the exact lines, no wildcards,
`env_reset`, never `status` or `edit`):
`/usr/bin/systemctl {start,stop,restart} <unit>` and
`/usr/bin/journalctl -u <unit> --since -1h -n 500 -o short-iso --no-pager`.
The owner installs the drop-in (`visudo -cf`); the helper never edits sudoers.

## Failure modes

- Stop or restart of the Hub itself: the helper answers `will_restart` first and acts about 1 s later; one global lock
  (`busy`) while it runs; the page polls and shows the command to run by hand after 60 s.
- Per-part lock: `busy` (409). Helper not running: buttons off and a note. Command failed: `action_failed` with 200
  redacted characters.
- Update: refused (`update_unavailable`). Missing: signed releases (`docs/design/install-update.md` §4 and §7; no
  `orch/update/verify.py` yet). Later `target_version` must equal the verified manifest version.

## Logs (SV-03)

Last hour, at most 500 lines, 256 KB, 5 s. Redacted in the helper before anything leaves it (control and bidi characters,
PEM blocks, Authorization and cookie lines, Bearer values, secret-named key/value pairs in every form, URL userinfo and
query strings, 24+ character runs). Best effort, over-redaction preferred. Admin only; the console shows text with
`textContent`. Hardening backlog: a separate helper user, launchd and pidfile backends, helpers on other machines.
