import json
import os
import re
import secrets
import threading
from dataclasses import asdict
from pathlib import Path

from orch import ops
from orch.ops import Context, Result
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace
from orch.ops.views import verified

from orch_dashboard.errors import HostError, fail
from orch_dashboard.hostlink import check_uid, encode, receive

from .reserved import OPS

# Registry reads that do not accept filesystem paths or need a P1 agent grant.
READS = ("addon.list", "list")


class StubHost:
    def __init__(self, root: Path, state: Path, person: str):
        if os.environ.get("ORCH_DEV") != "1" or not (root / ".orch-dev").is_file():
            raise RuntimeError("stub requires ORCH_DEV=1 and an explicitly marked .orch-dev workspace")
        self.root, self.state, self.person = root, state, person
        config = json.loads((root / "config.json").read_text())
        self.ws = config["workspace"]["id"]
        self.name = config["workspace"].get("name", "Development workspace")
        self.lock = threading.RLock()
        # Person is an explicit local UID mapping, never inferred from a key.
        self.env = {"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(state)}
        self.versions = {name: 1 for name in (*OPS, *READS)}
        self.generation = 0
        self.snapshot = None
        self.cursor = secrets.token_urlsafe(24)

    def welcome(self):
        data = self.call("workspace.show", {})["data"]
        return {
            "t": "welcome",
            "proto": 1,
            "host": "orch-devhost/0.1.0",
            "host_uid": os.getuid(),
            "workspace": {"id": self.ws, "prefix": data["prefix"]},
            "peer": {"uid": os.getuid(), "class": "person", "person": self.person},
            "caps": ["ops/1", "query/1", "sub/1"],
            "ops": self.versions,
            "registry": "dev-p1",
            "limits": {"frame": 2097152, "inflight": 8, "timeout_ms": 30000},
        }

    def call(self, op, args):
        with self.lock:
            runtime = Workspace(self.env)
            try:
                store = runtime.store
                store.load_all()
                ws = store.state.workspace
                if self.person not in ws.members or not any(
                    d.person == self.person and not d.removed and not d.revoked for d in ws.devices.values()
                ):
                    raise fail("peer.denied", exit=3)
                ctx = Context(workspace=runtime, env=self.env)
                c = Call(ctx, "list")
                c._person = self.person  # P1 has no person-read Context; local wrapper only, no global patch.
                views = [v for _, v in verified(c, store.state.tickets)]
                snapshot = (store.head_seq("workspace"), tuple((v.uid, store.head_seq(v.uid)) for v in views))
                if snapshot != self.snapshot:
                    self.snapshot = snapshot
                    self.generation += 1
                    self.cursor = secrets.token_urlsafe(24)
                if op in READS:
                    decl = ops.get(op)
                    from jsonschema import ValidationError, validate

                    try:
                        validate(args, decl.input)
                    except ValidationError:
                        raise fail("invalid.input", exit=5) from None
                    return asdict(decl.handler(ctx, args))
                if op not in OPS:
                    raise fail("op.unsupported", exit=1)
                if op != "ticket.query" and args:
                    raise fail("invalid.input", exit=5)
                if op == "host.list":
                    data = {"workspaces": [{"id": self.ws, "mapped": True}]}
                elif op == "whoami":
                    data = {
                        "peer": {"uid": os.getuid(), "class": "person"},
                        "person": self.person,
                        "grant": None,
                        "session": None,
                        "tier": "A",
                    }
                elif op == "host.status":
                    data = {"version": "dev-p1", "uptime_s": 0, "workspace": self.ws, "state": "ok", "tier": "A"}
                elif op == "workspace.show":
                    data = {
                        "id": self.ws,
                        "prefix": ws.prefix,
                        "name": self.name,
                        "genesis": ws.genesis,
                        "epoch": None,
                        "wsk_fingerprint": None,
                        "members_v": ws.roster_v,
                    }
                elif op == "member.list":
                    data = {
                        "members": [
                            {
                                "person": m.person,
                                "name": m.name,
                                "role": m.role,
                                "devices": [
                                    {"id": d.id, "auth": "not available"}
                                    for d in ws.devices.values()
                                    if d.person == m.person and not d.removed and not d.revoked
                                ],
                            }
                            for m in ws.members.values()
                        ]
                    }
                elif op == "needs":
                    items = [
                        {
                            "kind": n.kind,
                            "key": v.key,
                            "uid": v.uid,
                            "ref": n.ref,
                            "detail": n.detail,
                            "who": list(n.who),
                        }
                        for v in views
                        for n in v.needs
                        if self.person in n.who
                    ]
                    items += [
                        {"kind": n.kind, "key": None, "uid": None, "ref": n.ref, "detail": n.detail, "who": list(n.who)}
                        for n in ws.needs
                        if self.person in n.who
                    ]
                    data = {"items": items, "sub_cursor": self.cursor}
                elif op == "ticket.query":
                    from pydantic import ValidationError

                    from orch_dashboard.app import TicketQuery

                    try:
                        query = TicketQuery(**args)
                        start = int(query.page or "0")
                        if start < 0:
                            raise ValueError()
                    except (ValidationError, ValueError):
                        raise fail("invalid.input", exit=5) from None
                    rows = []
                    for v in views:
                        events = store.events(v.uid)
                        updated = events[-1]["at"] if events else None
                        rows.append(
                            {
                                "key": v.key,
                                "uid": v.uid,
                                "title": v.title,
                                "type": v.type,
                                "status": v.status,
                                "priority": v.fields["priority"],
                                "size": v.fields["size"],
                                "labels": list(v.fields["labels"]),
                                "assignees": list(v.people["assignees"]),
                                "owner": v.owner,
                                "parent": v.fields.get("parent"),
                                "epic": None,
                                "claim": {"session": v.claim.session, "agent": None} if v.claim else None,
                                "gates": {
                                    g: "n/a" if not x.applies else "approved" if x.approved else "open"
                                    for g, x in v.gates.items()
                                },
                                "needs": [n.kind for n in v.needs if self.person in n.who],
                                "updated_at": updated,
                                "head_seq": store.head_seq(v.uid),
                            }
                        )
                    if query.sort == "priority":
                        ranks = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
                        rows.sort(key=lambda r: (ranks[r["priority"]], r["key"]))
                    else:
                        rows.sort(
                            key=lambda r: r["updated_at"] or "" if query.sort == "updated" else r["key"],
                            reverse=query.sort == "updated",
                        )
                    end = start + query.limit
                    data = {
                        "rows": rows[start:end],
                        "total": len(rows),
                        "next_page": str(end) if end < len(rows) else None,
                        "sub_cursor": self.cursor,
                    }
                else:
                    raise fail("op.unsupported", exit=1)
                return asdict(Result(data=data))
            finally:
                if runtime._store:
                    runtime.store.close()

    def serve_connection(self, sock):
        subscriptions = {}
        try:
            check_uid(sock, os.getuid())
            sock.settimeout(2)
            hello = receive(sock)
            if hello.get("t") != "hello" or 1 not in hello.get("proto", []):
                raise fail("host.version_mismatch", exit=1)
            sock.sendall(encode(self.welcome()))
            sock.settimeout(0.5)
            while True:
                # Poll only between frames; never time out halfway through a frame.
                import select

                if not select.select([sock], [], [], 0.5)[0]:
                    self.call("whoami", {})  # membership revalidated, including queued invalidations
                    for sub, cursor in list(subscriptions.items()):
                        if cursor != self.cursor:
                            sock.sendall(encode({"t": "evt", "sub": sub, "invalidated": "overflow"}))
                            del subscriptions[sub]
                    continue
                sock.settimeout(2)
                req = receive(sock)
                if req.get("t") == "cancel":
                    continue  # synchronous reads have already finished
                if req.get("t") != "req" or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", req.get("id", "")):
                    raise fail("frame.invalid", exit=6)
                rid, op, args = req["id"], req.get("op"), req.get("args", {})
                try:
                    if op not in self.versions or hello.get("ops", {}).get(op) != 1:
                        raise fail("op.unsupported", exit=1)
                    if op == "sub.open":
                        if (
                            set(args) - {"topics", "after"}
                            or not args.get("topics")
                            or any(t not in ("workspace", "tickets", "needs") for t in args["topics"])
                        ):
                            raise fail("invalid.input", exit=5)
                        self.call("whoami", {})
                        if args.get("after", self.cursor) != self.cursor:
                            raise fail("sub.reset")
                        if len(subscriptions) >= 8:
                            raise fail("host.busy")
                        sub = secrets.token_hex(8)
                        subscriptions[sub] = self.cursor
                        result = asdict(Result(data={"sub": sub, "cursor": self.cursor}))
                    elif op == "sub.close":
                        if set(args) != {"sub"}:
                            raise fail("invalid.input", exit=5)
                        subscriptions.pop(args["sub"], None)
                        result = asdict(Result())
                    else:
                        result = self.call(op, args)
                    sock.sendall(encode({"t": "res", "id": rid, "ok": True, "result": result}))
                except (HostError, OrchError) as exc:
                    if isinstance(exc, OrchError):
                        from orch.ops.errors import spec

                        known = spec(exc.code)
                        error = {
                            "code": exc.code,
                            "message": exc.message,
                            "hint": exc.hint or known.hint,
                            "retryable": known.retryable if exc.retryable is None else exc.retryable,
                        }
                        exit_code = known.exit
                    else:
                        error, exit_code = exc.error, exc.exit
                    sock.sendall(encode({"t": "res", "id": rid, "ok": False, "exit": exit_code, "error": error}))
        except HostError as exc:
            try:
                sock.sendall(encode({"t": "fatal", "error": exc.error}))
            except OSError:
                pass
        except (EOFError, OSError):
            pass
        finally:
            sock.close()
