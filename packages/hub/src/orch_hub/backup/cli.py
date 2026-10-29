"""``orch-hub backup-key``: set up (or, with ``--replace``, replace) the hub backup key at the server's own terminal. The 24
words are printed once to this terminal and are in no file or log. This is the only way to get them on a console that is
reached over plain http on an office or public address (the web page refuses to show them there)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import dev_of
from ..keys import HubKeys, KeysError, default_state_dir
from .service import BackupError, BackupService


def add_parser(sub: Any) -> None:
    p = sub.add_parser("backup-key", help="set up the hub backup key: prints 24 words once; makes the first backup")
    p.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    p.add_argument(
        "--replace", action="store_true", help="replace an existing key (backups under the old key are deleted)"
    )


def run(
    args: Any,
    out: Callable[[str], None] = print,
    now_ms: Callable[[], int] | None = None,
    tty: Callable[[], bool] = sys.stdout.isatty,
    ask: Callable[[str], str] = input,
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
        try:  # older backups under another key go only once the person says the words are saved
            ask("\nPress Enter once the words are saved (Ctrl-C to keep the older backups for now): ")
            svc.confirm("cli")
        except EOFError, KeyboardInterrupt:
            print("\norch-hub: not confirmed; older backups are kept until you confirm in the console", file=sys.stderr)
            return 0
        except BackupError as e:
            print(f"orch-hub: could not confirm the new words ({e.code}); older backups are kept", file=sys.stderr)
            return 1
        return 0
    finally:
        activity.close()
        audit.store.close()
