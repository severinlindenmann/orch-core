"""``orch-hub backup-key``: set up (or, with ``--replace``, replace) the hub backup key at the server's own terminal. The 24
words are printed once to this terminal and are in no file or log. This is the only way to get them on a console that is
reached over plain http on an office or public address (the web page refuses to show them there)."""

from __future__ import annotations

import getpass
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import dev_of
from ..contract.hub import Refused
from ..keys import HubKeys, KeysError, default_state_dir
from .service import BackupError, BackupService


def add_parser(sub: Any) -> None:
    p = sub.add_parser("backup-key", help="set up the hub backup key: prints 24 words once; makes the first backup")
    p.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    p.add_argument(
        "--replace", action="store_true", help="replace an existing key (backups under the old key are deleted)"
    )


def add_restore_parser(sub: Any) -> None:
    p = sub.add_parser(
        "restore",
        help="restore the hub from a backup file (on a fresh server or over this hub; stop the hub first)",
    )
    p.add_argument("file", help="the .orchbak file")
    p.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    p.add_argument("--real", action="store_true", help="fresh server only: this is a real hub (dev: false)")


def run_restore(
    args: Any,
    out: Callable[[str], None] = print,
    now_ms: Callable[[], int] | None = None,
    tty: Callable[[], bool] = sys.stdin.isatty,
    read_words: Callable[[], str] | None = None,
    confirm: Callable[[str], str] | None = None,
) -> int:
    """``orch-hub restore FILE`` (human only, like ``backup-key``): shows which hub the file is of (a fresh server compares
    its fingerprint), asks for the 24 words WITHOUT echo, then runs the same restore as the console. Every check passes before
    anything is written. The hub must not be running: start it again afterwards."""
    if os.environ.get("ORCH_GRANT") or os.environ.get("ORCH_SESSION"):
        print("orch-hub: refused: this command is for a person, not an agent", file=sys.stderr)
        return 1
    if not tty():
        print(
            "orch-hub: refused: run this at a terminal (the words are asked here, never read from a pipe)",
            file=sys.stderr,
        )
        return 1
    from . import restore as RS

    try:
        state = Path(args.state) if args.state else default_state_dir()
        data = Path(args.file).read_bytes()
        info = RS.preview(data, dev_mode=not args.real, state=state, now_ms=now_ms)
    except (OSError, KeysError) as e:
        print(f"orch-hub: {getattr(e, 'code', 'cannot read the file')}", file=sys.stderr)
        return 1
    except Refused as e:
        print(f"orch-hub: this file is not a backup this hub can restore ({e.code})", file=sys.stderr)
        return 1
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(info["made_ms"] // 1000))
    out(f"Backup of hub {info['fingerprint']}, made {when}.")
    if not info["in_place"]:
        out("This server has no hub yet: check that this is the hub you mean.")
    else:
        out("This replaces the hub on this server. Changes made after the backup are lost.")
    ask = confirm or input
    if ask("Type yes to continue: ").strip().lower() != "yes":
        print("orch-hub: nothing restored", file=sys.stderr)
        return 1
    words = read_words() if read_words is not None else getpass.getpass("The 24 backup words: ")
    audit = activity = None
    keys = None
    try:
        if info["in_place"]:
            from ..activity import ActivityLog
            from ..audit import open_log

            keys = HubKeys.open(state, prune=False, **({} if now_ms is None else {"now_ms": now_ms}))
            audit = open_log(state, keys, dev_mode=dev_of(keys), **({} if now_ms is None else {"now_ms": now_ms}))
            activity = ActivityLog(state / "activity.db") if (state / "activity.db").exists() else None
        try:
            got = RS.restore(
                state, data, words, dev_mode=not args.real, keys=keys, audit=audit, activity=activity, now_ms=now_ms
            )
        except RS.RestoreError as e:
            print(f"orch-hub: restore refused: {e.code}", file=sys.stderr)
            return 1
        finally:
            words = ""
    finally:
        if activity is not None:
            activity.close()
        if audit is not None:
            audit.store.close()
    for admin, token in sorted(got.get("recovery", {}).items()):
        out(f"Admin {admin}: set a new password at  <your hub address>/#invite={token}  (shown only now)")
    if got.get("recovery"):
        out("Passwords and codes of the backup are switched off on a new server: each admin uses the link above once.")
    out("Restored. People other than admins are switched off: turn each one on again in People.")
    out("Every phone and browser must sign in or pair again. Start the hub now.")
    out(f"New op_seq {got['op_seq']}.")
    return 0


def run(
    args: Any,
    out: Callable[[str], None] = print,
    now_ms: Callable[[], int] | None = None,
    tty: Callable[[], bool] = sys.stdout.isatty,
) -> int:
    if os.environ.get("ORCH_GRANT") or os.environ.get("ORCH_SESSION"):  # an agent never gets the words (human only)
        print("orch-hub: refused: this command is for a person, not an agent", file=sys.stderr)
        return 1
    if not tty():  # the words go to a person's screen, never into a pipe, a file or a log
        print(
            "orch-hub: refused: run this at a terminal (the words are not written to a pipe or a file)", file=sys.stderr
        )
        return 1
    try:
        state = Path(args.state) if args.state else default_state_dir()
        keys = HubKeys.open(state, **({} if now_ms is None else {"now_ms": now_ms}))
    except KeysError as e:
        print(f"orch-hub: {e}", file=sys.stderr)
        return 1
    from ..activity import ActivityLog
    from ..audit import open_log

    audit = open_log(state, keys, dev_mode=dev_of(keys), **({} if now_ms is None else {"now_ms": now_ms}))
    activity = ActivityLog(state / "activity.db")
    try:
        svc = BackupService(keys, audit=audit, activity=activity, now_ms=now_ms)
        try:
            if svc.key_info() is not None and not args.replace:
                print("orch-hub: a backup key is already set up (use --replace to make a new one)", file=sys.stderr)
                return 1
            words, made = svc.set_key(None)
        except BackupError as e:
            print(f"orch-hub: backup key refused: {e.code}", file=sys.stderr)
            return 1
        out("Write these 24 words down and keep them somewhere safe. They are shown only now.")
        out("Without them a backup cannot be opened.\n")
        w = words.split()
        for i in range(0, 24, 6):
            out("  ".join(f"{i + j + 1:>2} {x}" for j, x in enumerate(w[i : i + 6])))
        out(f"\nFirst backup: {made['name']}")
        svc.confirm("cli")  # the words were printed to this terminal: older backups under another key can go now
        return 0
    finally:
        activity.close()
        audit.store.close()
