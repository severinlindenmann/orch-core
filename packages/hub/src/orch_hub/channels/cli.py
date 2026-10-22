"""Dev CLI of the channel service: ``orch-hub channels list|revoke|pair-code`` and ``orch-hub users
set-password|totp``. Registered through ``access.cli`` (one ``access.db`` opener, one state-owner check). Output is JSON
of public data; ``users totp`` and ``channels pair-code`` print a secret ONCE by design, for the operator's terminal.
The password is read from stdin (never argv)."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

from ..audit import open_log
from ..keys import HubKeys
from .service import CHANNELS_FILE, ChannelError, Channels


def add_users_parsers(u: argparse._SubParsersAction, J: argparse.ArgumentParser) -> None:  # type: ignore[type-arg]
    p = u.add_parser("set-password", parents=[J], help="set the hub sign-in password (read from stdin, not argv)")
    p.add_argument("user_id")
    p = u.add_parser("totp", parents=[J], help="create a new TOTP secret for a user and print it once")
    p.add_argument("user_id")


def add_channels_parsers(c: argparse._SubParsersAction, J: argparse.ArgumentParser) -> None:  # type: ignore[type-arg]
    p = c.add_parser("list", parents=[J], help="registered channels with their status")
    p.add_argument("--user")
    p = c.add_parser(
        "revoke", parents=[J], help="revoke a channel for good (goes into the next snapshots; there is no undo)"
    )
    p.add_argument("channel_id")
    p = c.add_parser("pair-code", parents=[J], help="one-time code for a phone to pair with the hub")
    p.add_argument("--user", required=True)
    c.add_parser("revoked", parents=[J], help="the revoked channel ids (what snapshots carry)")
    c.add_parser("events", parents=[J], help="the channel event log")


def dispatch(args: argparse.Namespace, state: Path, acc: Any) -> Any:
    keys = HubKeys.open(state)
    svc = Channels(keys, acc, state / CHANNELS_FILE, audit=open_log(state, keys))
    try:
        c = args.cmd
        if args.group == "users":
            if c == "set-password":
                pw = getpass.getpass("password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
                svc.set_password(args.user_id, pw)
                return {}
            return svc.new_totp(args.user_id)
        if c == "list":
            return svc.channels(args.user)
        if c == "revoke":
            svc.revoke(args.channel_id)
            return {}
        if c == "pair-code":
            return svc.create_pairing_code(args.user)
        if c == "events":
            return svc.events()
        return acc.revoked_channels()
    finally:
        svc.close()


__all__ = ["ChannelError", "add_channels_parsers", "add_users_parsers", "dispatch"]
