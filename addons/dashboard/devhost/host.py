import hashlib
import json
import os
import re
import secrets
import threading
from collections import deque
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
        self.cursor = secrets.token_urlsafe(24)

        self.history = []
        self.journal = deque(maxlen=10000)
        self.cursors = {self.cursor: 0}
        self.visible = {}

    def refresh(self, store, views):
        visible = {v.uid: v.key for v in views}
        events = [("workspace", e) for e in store.events("workspace")]
        events += [(uid, e) for uid in store.state.tickets for e in store.events(uid)]
        events.sort(
            key=lambda pair: (
                pair[1]["seq"] if pair[0] == "workspace" else pair[1]["ws_seq"],
                0 if pair[0] == "workspace" else 1,
                pair[1]["at"],
                pair[0],
                pair[1]["seq"],
            )
        )
        history = [e["id"] for _, e in events]
        if history == self.history:
            return
        if history[: len(self.history)] != self.history:
            self.journal.clear()
            self.cursors.clear()
            self.history = []
        for pos, (log, e) in enumerate(events[len(self.history) :], len(self.history) + 1):
            items = []
            if log == "workspace" or log in visible:
                items.append(
                    dict(
                        id=f"{pos}.e",
                        jpos=pos,
                        log=log,
                        uid=None if log == "workspace" else log,
                        key=visible.get(log),
                        seq=e["seq"],
                        ws_seq=e.get("ws_seq", e["seq"]),
                        type=e["type"],
                        at=e["at"],
                        actor={"kind": e["actor"]["kind"], "id": e["actor"].get("id")},
                    )
                )
            if pos == len(events):
                # A workspace membership change may remove visibility of several tickets.
                # A reset avoids inventing multiple .g identities for one position.
                removed = self.visible.keys() - visible.keys()
                if len(removed) > 1:
                    self.journal.clear()
                    self.cursors.clear()
                elif removed:
                    items.append(dict(id=f"{pos}.g", jpos=pos, uid=next(iter(removed)), gone=True))
            items.append(dict(id=f"{pos}.n", jpos=pos, type="needs.changed"))
            self.journal.append((pos, items))
        self.history, self.visible = history, visible
        self.cursor = secrets.token_urlsafe(24)
        self.cursors[self.cursor] = len(history)
        minimum = self.journal[0][0] - 1 if self.journal else len(history)
        self.cursors = {c: pos for c, pos in self.cursors.items() if pos >= minimum}

    def pending(self, cursor, topics):
        self.call("whoami", {})
        if cursor not in self.cursors:
            return [], "reset"
        items = [
            item
            for pos, batch in self.journal
            if pos > self.cursors[cursor]
            for item in batch
            if (
                (item.get("gone") and "tickets" in topics)
                or (item.get("type") == "needs.changed" and "needs" in topics)
                or (item.get("log") == "workspace" and "workspace" in topics)
                or (item.get("key") and "tickets" in topics and item["uid"] in self.visible)
            )
        ]
        if len(items) > 256 or len(json.dumps(items).encode()) > 262144:
            return [], "overflow"
        return items, None

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
            "registry": "sha256:" + hashlib.sha256(json.dumps(self.versions, sort_keys=True).encode()).hexdigest(),
            "tier": "A",
            "limits": {
                "frame": 2097152,
                "inflight": 8,
                "timeout_ms": 30000,
                "blob_bytes": 0,
                "inline_bytes": 262144,
            },
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
                self.refresh(store, views)
                if op in READS:
                    decl = ops.get(op)
                    from jsonschema import ValidationError, validate

                    try:
                        validate(args, decl.input)
                    except ValidationError:
                        raise fail("invalid.input", exit=5) from None
                    return asdict(decl.handler(ctx, args))
                if op not in OPS:
                    raise fail("unknown_command", exit=2)
                if op != "ticket.query" and args:
                    raise fail("invalid.input", exit=5)
                if op == "host.list":
                    data = {
                        "workspaces": [
                            {
                                "workspace_id": self.ws,
                                "person": self.person,
                                "prefix": ws.prefix,
                                "root": str(self.root),
                                "state": "ok",
                                "tier": "A",
                                "socket": str(
                                    Path(os.environ.get("ORCH_RUN_DIR", str(self.state / "run")))
                                    / self.ws
                                    / "host.sock"
                                ),
                            }
                        ]
                    }
                elif op == "whoami":
                    data = {
                        "peer": {"uid": os.getuid(), "class": "person"},
                        "person": self.person,
                        "grant": None,
                        "session": None,
                        "tier": "A",
                    }
                elif op == "host.status":
                    data = {
                        "version": "dev-p1",
                        "uptime_s": 0,
                        "workspace": self.ws,
                        "state": "ok",
                        "tier": "A",
                    }
                elif op == "workspace.show":
                    data = {
                        "id": self.ws,
                        "prefix": ws.prefix,
                        "name": self.name,
                        "genesis": ws.genesis,
                        "epoch": None,
                        "wsk_fingerprint": None,
                        "members_v": ws.roster_v,
                        "state": "ok",
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
                        {
                            "kind": n.kind,
                            "key": None,
                            "uid": None,
                            "ref": n.ref,
                            "detail": n.detail,
                            "who": list(n.who),
                        }
                        for n in ws.needs
                        if self.person in n.who
                    ]
                    data = {"items": items, "sub_cursor": self.cursor}
                elif op == "ticket.query":
                    from orch_dashboard.app import TicketQuery
                    from pydantic import ValidationError

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
                        "as_of": store.state.now,
                    }
                else:
                    raise fail("unknown_command", exit=2)
                return asdict(Result(data=data))
            finally:
                if runtime._store:
                    runtime.store.close()

    def serve_connection(self, sock, control=False):
        subscriptions = {}
        try:
            check_uid(sock, os.getuid())
            sock.settimeout(2)
            hello = receive(sock)
            if hello.get("t") != "hello":
                raise fail("handshake.invalid", exit=6)
            if not isinstance(hello.get("proto"), list) or 1 not in hello["proto"]:
                raise fail("host.version_mismatch", exit=1)
            welcome = self.welcome()
            if control:
                welcome.update(
                    workspace=None,
                    peer={"uid": os.getuid(), "class": "control", "person": None},
                    caps=["control/1"],
                    ops={"host.list": 1},
                )
            sock.sendall(encode(welcome))
            sock.settimeout(0.5)
            while True:
                # Poll only between frames; never time out halfway through a frame.
                import select

                for sub, (cursor, topics) in list(subscriptions.items()):
                    try:
                        with self.lock:
                            items, end = self.pending(cursor, topics)
                            current = self.cursor
                    except HostError:
                        items, end, current = [], "person.removed", cursor
                    if end:
                        sock.sendall(encode(dict(t="evt", sub=sub, cursor=current, items=[], end=end)))
                        del subscriptions[sub]
                    elif current != cursor:
                        batches = []
                        for item in items:
                            if not batches or (len(batches[-1]) >= 61 and batches[-1][-1]["jpos"] != item["jpos"]):
                                batches.append([])
                            batches[-1].append(item)
                        for batch in batches or [[]]:
                            # Intermediate cursors point only after completed positions.
                            with self.lock:
                                cursor_out = (
                                    current
                                    if batch == (batches[-1] if batches else [])
                                    else next(
                                        (c for c, pos in self.cursors.items() if pos == batch[-1]["jpos"]),
                                        secrets.token_urlsafe(24),
                                    )
                                )
                                if cursor_out != current:
                                    self.cursors[cursor_out] = batch[-1]["jpos"]
                            sock.sendall(encode(dict(t="evt", sub=sub, cursor=cursor_out, items=batch)))
                        subscriptions[sub] = current, topics
                if not select.select([sock], [], [], 0.2)[0]:
                    continue
                sock.settimeout(2)
                req = receive(sock)
                if req.get("t") == "cancel":
                    continue  # synchronous reads have already finished
                if req.get("t") == "hello":
                    raise fail("handshake.invalid", exit=6)
                if (
                    req.get("t") != "req"
                    or not isinstance(req.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", req.get("id", ""))
                ):
                    raise fail("frame.invalid", exit=6)
                rid, op, args = req["id"], req.get("op"), req.get("args", {})
                try:
                    if (
                        set(req) - {"t", "id", "op", "args", "opts"}
                        or not {"t", "id", "op", "args"} <= req.keys()
                        or not isinstance(args, dict)
                        or not isinstance(op, str)
                        or not isinstance(req.get("opts", {}), dict)
                    ):
                        raise fail("req.invalid", exit=6)
                    opts = req.get("opts", {})
                    if (
                        set(opts) - {"idem", "dry_run", "timeout_ms"}
                        or ("dry_run" in opts and type(opts["dry_run"]) is not bool)
                        or (
                            "timeout_ms" in opts
                            and (type(opts["timeout_ms"]) is not int or not 1000 <= opts["timeout_ms"] <= 600000)
                        )
                    ):
                        raise fail("req.invalid", exit=6)
                    if op not in welcome["ops"]:
                        raise fail("unknown_command", exit=2)
                    if hello.get("ops", {}).get(op, 1) != 1:
                        raise fail("op.unsupported", exit=1)
                    if req.get("opts", {}).get("dry_run") is not None:
                        raise fail("invalid.input", exit=5)
                    if op == "sub.open":
                        if (
                            set(args) - {"topics", "after"}
                            or not isinstance(args.get("topics"), list)
                            or not 1 <= len(args["topics"]) <= 8
                            or (
                                "after" in args
                                and (not isinstance(args["after"], str) or not 1 <= len(args["after"]) <= 256)
                            )
                            or any(t not in ("workspace", "tickets", "needs") for t in args["topics"])
                        ):
                            raise fail("invalid.input", exit=5)
                        self.call("whoami", {})
                        if args.get("after", self.cursor) not in self.cursors:
                            raise fail("sub.reset")
                        if len(subscriptions) >= 8:
                            raise fail("quota.host")
                        sub = "sub_" + secrets.token_hex(8)
                        subscriptions[sub] = (
                            args.get("after", self.cursor),
                            args["topics"],
                        )
                        result = asdict(Result(data={"sub": sub, "cursor": self.cursor}))
                    elif op == "sub.close":
                        if set(args) != {"sub"} or not isinstance(args.get("sub"), str):
                            raise fail("invalid.input", exit=5)
                        subscriptions.pop(args["sub"], None)
                        result = asdict(Result(data={}))
                    else:
                        result = self.call(op, args)
                    sock.sendall(encode({"t": "res", "id": rid, "ok": True, "result": result}))
                    if op == "sub.close":
                        sock.sendall(
                            encode(
                                dict(
                                    t="evt",
                                    sub=args["sub"],
                                    cursor=self.cursor,
                                    items=[],
                                    end="closed",
                                )
                            )
                        )
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
                    sock.sendall(
                        encode(
                            {
                                "t": "res",
                                "id": rid,
                                "ok": False,
                                "exit": exit_code,
                                "error": error,
                            }
                        )
                    )
        except HostError as exc:
            try:
                sock.sendall(encode({"t": "fatal", "error": exc.error}))
            except OSError:
                pass
        except (EOFError, OSError):
            pass
        finally:
            sock.close()
