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

from ..channels import ChannelError
from ..channels.cli import add_channels_parsers, add_users_parsers
from ..channels.cli import dispatch as channels_dispatch
from ..config import dev_of
from ..keys import HubKeys, KeysError, default_state_dir
from .store import Access, AccessError, check_name, require_state_owner, role_rank

ACCESS_FILE = "access.db"


def add_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    J = argparse.ArgumentParser(add_help=False)
    J.add_argument("--json", action="store_true", help="accepted for scripts; output is always JSON")

    def grp(name: str, help_: str):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
        return p, p.add_subparsers(dest="cmd", required=True)

    _, u = grp("users", "dev: hub-local users")
    a = u.add_parser("add", parents=[J], help="create a user (and its UK) with a display name")
    a.add_argument("name", nargs="?")
    a.add_argument("--name", dest="name_opt")
    u.add_parser("list", parents=[J])
    r = u.add_parser("remove", parents=[J], help="delete a user; they disappear from the next snapshots")
    r.add_argument("user_id")
    _, g = grp("groups", "dev: groups and their members")
    g.add_parser("list", parents=[J])
    a = g.add_parser("add", parents=[J])
    a.add_argument("name", nargs="?")
    a.add_argument("--name", dest="name_opt")
    r = g.add_parser("remove", parents=[J])
    r.add_argument("name")
    m = g.add_parser("add-member", parents=[J])
    m.add_argument("name")
    m.add_argument("user_id")
    m = g.add_parser("remove-member", parents=[J])
    m.add_argument("name")
    m.add_argument("user_id")
    for cmd in ("member-add", "member-remove"):
        m = g.add_parser(cmd, parents=[J], help="flag form of add-member / remove-member")
        m.add_argument("--group", dest="name", required=True)
        m.add_argument("--user", dest="user_id", required=True)
    _, w = grp("grants", "dev: workspace grants {workspace, group, role}")
    s = w.add_parser("list", parents=[J])
    s.add_argument("--workspace")
    s = w.add_parser("set", parents=[J], help="give a group read, edit or admin in a workspace")
    s.add_argument("workspace_id")
    s.add_argument("group_name")
    s.add_argument("role", choices=("read", "edit", "admin"))
    s = w.add_parser("add", parents=[J], help="flag form of set")
    s.add_argument("--workspace", dest="workspace_id", required=True)
    s.add_argument("--group", dest="group_name", required=True)
    s.add_argument("--role", required=True, choices=("read", "edit", "admin"))
    s = w.add_parser("revoke", parents=[J])
    s.add_argument("workspace_id")
    s.add_argument("group_name")
    s = w.add_parser("roles", parents=[J], help="the roles a workspace's snapshot will carry")
    s.add_argument("workspace_id")
    ac = sub.add_parser("access", help="dev: would this user be allowed to read / route in a workspace?")
    ac.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    acs = ac.add_subparsers(dest="cmd", required=True)
    k = acs.add_parser("check", parents=[J])
    k.add_argument("--user", required=True)
    k.add_argument("--workspace", required=True)
    k.add_argument("--action", required=True, choices=("read", "route"))
    k.add_argument("--space", help="accepted; shared-space rules arrive with the router (D8)")
    _, c = grp("channels", "dev: channels, pairing codes and revocation (revocations go into every snapshot)")
    add_channels_parsers(c, J)
    add_users_parsers(u, J)


def run(args: argparse.Namespace, out=None) -> int:
    out = out or sys.stdout
    try:
        state = Path(args.state) if args.state else default_state_dir()
        require_state_owner(state)
        acc = Access(str(state / ACCESS_FILE))
        log = None
        try:
            if args.group in ("users", "groups", "grants"):  # D4: grant and membership changes go into the audit chain
                from ..audit import open_log

                log = open_log(state, hk := HubKeys.open(state), dev_mode=dev_of(hk))
                acc.observer = log.access_change
            res = _dispatch(args, state, acc)
        finally:
            acc.close()
            if log is not None:
                log.store.close()
    except (AccessError, KeysError, ChannelError) as e:
        print(f"orch-hub: refused: {e.code}" + (f" ({e})" if str(e) != e.code else ""), file=sys.stderr)
        return 1
    print(json.dumps(res, sort_keys=True), file=out)
    return 3 if args.group == "access" and not res["allowed"] else 0


def _name(args: argparse.Namespace) -> str:
    name = args.name_opt or args.name
    if not name:
        raise AccessError("malformed", "a name is required")
    return name


#: ``read`` needs any role, ``route`` needs edit or admin (spec §6: roles gate hub-side reads and routes).
_NEEDS = {"read": "read", "route": "edit"}


def check(acc: Access, state: Path, user: str, workspace: str, action: str) -> dict:
    """The decision D8 enforces, as data: ``{allowed, role, reason}``. Selective workspaces: hub roles grant nothing
    (contract §7); an unreadable registry fails closed as selective-or-unknown, never as allowed."""
    from ..inspect import workspace_mode

    role = acc.role_of(user, workspace)
    if workspace_mode(state, workspace) != "full":
        return {"allowed": False, "role": role, "reason": "selective_workspace"}
    if role is None:
        return {"allowed": False, "role": None, "reason": "not_granted"}
    if role_rank(role) < role_rank(_NEEDS[action]):
        return {"allowed": False, "role": role, "reason": "forbidden_role"}
    return {"allowed": True, "role": role, "reason": "ok"}


def _dispatch(args: argparse.Namespace, state: Path, acc: Access):
    g, c = args.group, args.cmd
    if g == "access":
        return check(acc, state, args.user, args.workspace, args.action)
    if g == "users":
        if c in ("set-password", "totp"):
            return channels_dispatch(args, state, acc)
        if c == "add":
            name = check_name(_name(args))  # before a UK is issued
            user_id = HubKeys.open(state).issue_uk(None)["o"]["user_id"]
            acc.add_user(user_id, name)
            return {"user_id": user_id, "name": name}
        if c == "list":
            return acc.users()
        return channels_dispatch(args, state, acc)  # revokes the user's channels first, then removes the user
    if g == "groups":
        if c == "list":
            return acc.groups()
        if c == "add":
            name = _name(args)
            acc.add_group(name)
            return {"group_id": name, "name": name}  # a group's id is its name
        elif c == "remove":
            acc.remove_group(args.name)
        elif c in ("add-member", "member-add"):
            acc.add_member(args.name, args.user_id)
        else:
            acc.remove_member(args.name, args.user_id)
        return {}
    if g == "grants":
        if c == "list":
            return acc.grants(args.workspace)
        if c in ("set", "add"):
            acc.grant(args.workspace_id, args.group_name, args.role)
        elif c == "revoke":
            acc.revoke(args.workspace_id, args.group_name)
        else:
            return acc.roles(args.workspace_id)
        return {}
    return channels_dispatch(args, state, acc)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - convenience for tests
    p = argparse.ArgumentParser(prog="orch-hub")
    add_parsers(p.add_subparsers(dest="group", required=True))
    return run(p.parse_args(argv))
