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
from ..config import dev_of
from ..contract import channelapi as CA
from ..contract.hub import Refused
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
    p.add_argument(
        "--link",
        action="store_true",
        help="also print the orch://hub-pair/... link for the phone's QR (needs --api-origin)",
    )
    p.add_argument("--api-origin", help="with --link: the hub's public API origin, as the phone will use it")
    c.add_parser("revoked", parents=[J], help="the revoked channel ids (what snapshots carry)")
    c.add_parser("events", parents=[J], help="the channel event log")


def pair_link(hub_id: str, user_id: str, code: str, api_origin: str | None, dev: bool) -> str:
    """The §6.4 link ``orch://hub-pair/<authority>#h1.<hub_id>.<user_id>.<code>`` for ``api_origin`` (https; plain http
    only for a loopback host at a dev hub). Parsed back with the contract's ``parse_pair_link`` before it is printed."""
    if api_origin is None:
        raise ChannelError("malformed", "--link needs --api-origin")
    scheme, sep, authority = api_origin.partition("://")
    if not sep or scheme not in ("http", "https"):
        raise ChannelError("malformed", "--api-origin must be https://host[:port]")
    link = f"{CA.PAIR_PREFIX}{authority}#h1.{hub_id}.{user_id}.{code}"
    try:
        parsed = CA.parse_pair_link(link, dev_mode=dev)
    except Refused:
        raise ChannelError("malformed", "--api-origin is not a valid pairing authority") from None
    if parsed["api"] != api_origin:  # e.g. http on a real host, or https on a loopback host
        raise ChannelError("malformed", "--api-origin does not round-trip through the link grammar")
    return link


def dispatch(args: argparse.Namespace, state: Path, acc: Any) -> Any:
    keys = HubKeys.open(state)
    svc = Channels(
        keys, acc, state / CHANNELS_FILE, audit=open_log(state, keys, dev_mode=dev_of(keys)), dev_mode=dev_of(keys)
    )
    try:
        c = args.cmd
        if args.group == "users":
            if c == "set-password":
                pw = getpass.getpass("password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
                svc.set_password(args.user_id, pw)
                return {}
            if c == "remove":  # the user's channels are revoked first, then the user goes
                svc.remove_user(args.user_id)
                return {}
            return svc.new_totp(args.user_id)
        if c == "list":
            return svc.channels(args.user)
        if c == "revoke":
            svc.revoke(args.channel_id)
            return {}
        if c == "pair-code":
            got = svc.create_pairing_code(args.user)
            if args.link:
                got["link"] = pair_link(keys.hub_id, args.user, got["code"], args.api_origin, dev_of(keys))
                got["fingerprint"] = CA.fingerprint(keys.hub_id)
            return got
        if c == "events":
            return svc.events()
        return acc.revoked_channels()
    finally:
        svc.close()


__all__ = ["ChannelError", "add_channels_parsers", "add_users_parsers", "dispatch"]
