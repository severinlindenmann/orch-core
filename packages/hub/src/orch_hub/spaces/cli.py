"""``orch-hub spaces create|add|remove|list``: the dev-only admin CLI of shared spaces (no web API before D13).

The state dir must be owned by the running user. ``--as USER`` names the hub user asking for the change; D3's ``Access``
decides: a hub-admin, or ``admin`` in every full-mode member workspace. A selective workspace is added only with its own
``space.accept`` (the host sends it with ``space_accept``); a refusal prints what the owner has to approve. Output is
JSON of public data only (never a key, wrap or sealed item).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..access.store import Access, AccessError, require_state_owner
from ..audit import open_log
from ..config import dev_of
from ..keys import HubKeys, KeysError, default_state_dir
from .service import SpaceError, SpaceService
from .store import SpaceStore

SPACES_FILE = "spaces.db"


def add_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("spaces", help="dev: shared spaces of the hub (D75)")
    p.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    s = p.add_subparsers(dest="cmd", required=True)
    J = argparse.ArgumentParser(add_help=False)
    J.add_argument("--json", action="store_true", help="accepted for scripts; output is always JSON")
    c = s.add_parser("create", parents=[J], help="create a space with member workspaces")
    c.add_argument("--name", required=True)
    c.add_argument("--workspace", action="append", required=True, help="member workspace id (repeatable)")
    c.add_argument("--as", dest="actor", required=True, help="hub user id asking for the change")
    a = s.add_parser("add", parents=[J], help="add a workspace (a selective one needs its own space.accept)")
    a.add_argument("--space", required=True)
    a.add_argument("--workspace", required=True)
    a.add_argument("--as", dest="actor", required=True)
    r = s.add_parser("remove", parents=[J], help="remove a workspace (new epoch, a fresh key)")
    r.add_argument("--space", required=True)
    r.add_argument("--workspace", required=True)
    r.add_argument("--as", dest="actor", required=True)
    s.add_parser("list", parents=[J])


def run(args: argparse.Namespace, out=None) -> int:
    from ..link.registry import Registry

    out = out or sys.stdout
    store = registry = acc = None
    stores: list = []
    try:
        state = Path(args.state) if args.state else default_state_dir()
        require_state_owner(state)
        keys = HubKeys.open(state)
        registry = Registry(str(state / "registry.db"))
        acc = Access(str(state / "access.db"))
        store = SpaceStore(str(state / SPACES_FILE))
        log = open_log(state, keys, dev_mode=dev_of(keys))
        stores.append(log.store)
        svc = SpaceService(
            store,
            registry,
            acc,
            keys.op_signer,
            keys.hub_id,
            lambda: keys.op_cert()["o"]["cert_id"],
            on_change=log.space_change,
            dev_mode=dev_of(keys),
        )
        if args.cmd == "list":
            res: object = svc.list_all()
        elif args.cmd == "create":
            res = _pub(svc.create(args.name, sorted(set(args.workspace)), args.actor))
        elif args.cmd == "add":
            res = _pub(svc.add(args.space, args.workspace, args.actor))
        else:
            r = svc.remove(args.space, args.workspace, args.actor)
            res = {"deleted": True} if r is None else _pub(r)
    except SpaceError as e:
        print(json.dumps({"error": e.code, **({"need": e.need} if e.need else {})}, sort_keys=True), file=sys.stderr)
        return 1
    except (AccessError, KeysError) as e:
        print(f"orch-hub: refused: {e.code}", file=sys.stderr)
        return 1
    finally:
        for x in (store, registry, acc, *stores):
            if x is not None:
                x.close()
    print(json.dumps(res, sort_keys=True), file=out)
    return 0


def _pub(rec: dict) -> dict:
    return {"space_id": rec["space_id"], "epoch": rec["epoch"], "members": rec["members"]}
