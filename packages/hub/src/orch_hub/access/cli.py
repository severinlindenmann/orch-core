"""``orch-hub users|groups|grants|channels``: the dev-only admin CLI of the access model (no web API before D13).

Every command first requires the state dir to be owned by the running user (``require_state_owner``); the access
store lives in ``<state>/access.db`` (0600). ``users add`` creates the user's UK through the key store, so the user id
is the same one the UK certificate names. Output is JSON of public data only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ..keys import HubKeys, KeysError, default_state_dir
from .store import Access, AccessError, require_state_owner

ACCESS_FILE = "access.db"


def add_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    def grp(name: str, help_: str):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
        return p, p.add_subparsers(dest="cmd", required=True)

    _, u = grp("users", "dev: hub-local users")
    a = u.add_parser("add", help="create a user (and its UK) with a display name")
    a.add_argument("name")
    u.add_parser("list")
    r = u.add_parser("remove", help="delete a user; they disappear from the next snapshots")
    r.add_argument("user_id")
    _, g = grp("groups", "dev: groups and their members")
    g.add_parser("list")
    a = g.add_parser("add")
    a.add_argument("name")
    r = g.add_parser("remove")
    r.add_argument("name")
    m = g.add_parser("add-member")
    m.add_argument("name")
    m.add_argument("user_id")
    m = g.add_parser("remove-member")
    m.add_argument("name")
    m.add_argument("user_id")
    _, w = grp("grants", "dev: workspace grants {workspace, group, role}")
    s = w.add_parser("list")
    s.add_argument("--workspace")
    s = w.add_parser("set", help="give a group read, edit or admin in a workspace")
    s.add_argument("workspace_id")
    s.add_argument("group_name")
    s.add_argument("role", choices=("read", "edit", "admin"))
    s = w.add_parser("revoke")
    s.add_argument("workspace_id")
    s.add_argument("group_name")
    s = w.add_parser("roles", help="the roles a workspace's snapshot will carry")
    s.add_argument("workspace_id")
    _, c = grp("channels", "dev: channel revocation (goes into every snapshot)")
    s = c.add_parser("revoke")
    s.add_argument("channel_id")
    s = c.add_parser("unrevoke")
    s.add_argument("channel_id")
    c.add_parser("list")


def run(args: argparse.Namespace, out=None) -> int:
    out = out or sys.stdout
    try:
        state = Path(args.state) if args.state else default_state_dir()
        require_state_owner(state)
        acc = Access(str(state / ACCESS_FILE))
        try:
            res = _dispatch(args, state, acc)
        finally:
            acc.close()
    except (AccessError, KeysError) as e:
        print(f"orch-hub: refused: {e.code}" + (f" ({e})" if str(e) != e.code else ""), file=sys.stderr)
        return 1
    print(json.dumps(res, sort_keys=True), file=out)
    return 0


def _dispatch(args: argparse.Namespace, state: Path, acc: Access):
    g, c = args.group, args.cmd
    if g == "users":
        if c == "add":
            user_id = HubKeys.open(state).issue_uk(None)["o"]["user_id"]
            acc.add_user(user_id, args.name)
            return {"user_id": user_id, "name": args.name}
        if c == "list":
            return acc.users()
        acc.remove_user(args.user_id)
        return {}
    if g == "groups":
        if c == "list":
            return acc.groups()
        if c == "add":
            acc.add_group(args.name)
        elif c == "remove":
            acc.remove_group(args.name)
        elif c == "add-member":
            acc.add_member(args.name, args.user_id)
        else:
            acc.remove_member(args.name, args.user_id)
        return {}
    if g == "grants":
        if c == "list":
            return acc.grants(args.workspace)
        if c == "set":
            acc.grant(args.workspace_id, args.group_name, args.role)
        elif c == "revoke":
            acc.revoke(args.workspace_id, args.group_name)
        else:
            return acc.roles(args.workspace_id)
        return {}
    if c == "revoke":
        acc.revoke_channel(args.channel_id)
    elif c == "unrevoke":
        acc.unrevoke_channel(args.channel_id)
    else:
        return acc.revoked_channels()
    return {}


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - convenience for tests
    p = argparse.ArgumentParser(prog="orch-hub")
    add_parsers(p.add_subparsers(dest="group", required=True))
    return run(p.parse_args(argv))
