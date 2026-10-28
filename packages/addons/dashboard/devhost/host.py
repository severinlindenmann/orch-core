import base64
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
from orch.ops.caps import Files
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, Workspace
from orch.ops.views import verified

from orch_dashboard.errors import HostError, fail
from orch_dashboard.gateway import run_dir
from orch_dashboard.hostlink import check_uid, encode, receive

from . import reads, settingsops
from .humanops import Requests
from .reserved import HUMAN_OPS, OPS, WRITE_OPS

# Registry reads that do not accept filesystem paths or need a P1 agent grant.
READS = ("addon.list", "addon.preview", "list")


class HostWorkspace(Workspace):
    """The stub plays the host, so it opens the store without the direct-mode guard (D-P2-18).

    Core's `Workspace` guards every store with `DirectGuard`, which answers `host.required` once the workspace is in
    `<state>/host/workspaces.json`. The dev registry pin lives in that same file, so the stub must be the host here.
    """

    def _open(self, root):
        from orch.custody import FileBackend
        from orch.ops.runtime import _config, state_dir
        from orch.store import BackendSigner, Store

        cfg = _config(root, self.files) or {}
        wid = cfg["workspace"]["id"]
        sd = state_dir(self.env, self.files)
        host = None
        keys = sd / "hosts" / wid / "keys"
        if keys.is_dir():
            backend = FileBackend(keys)
            if backend.exists("wsk"):
                host = BackendSigner(backend, "wsk")
        self.pin_created = not (sd / "hosts" / wid / "genesis").exists()
        return Store.open(
            root,
            expected_workspace_id=wid,
            host=host,
            host_state_dir=sd,
            clock=self.now,
            workspace_name=cfg["workspace"].get("name"),
            load="lazy",
        )


class StubHost:
    def __init__(self, root: Path, state: Path, person: str):
        if os.environ.get("ORCH_DEV") != "1" or not (root / ".orch-dev").is_file():
            raise RuntimeError("stub requires ORCH_DEV=1 and an explicitly marked .orch-dev workspace")
        self.root, self.state, self.person = root, state, person
        config = json.loads((root / ".orch" / "config.json").read_text())
        self.ws = config["workspace"]["id"]
        self.name = config["workspace"].get("name", "Development workspace")
        self.prefix = config["workspace"]["prefix"]
        self.stopped = False  # the dev "host is not running" switch (see __main__.run_stub)
        self.peers = [self]  # the workspaces this stub process serves (one control socket lists them all)
        self.lock = threading.RLock()
        # Person is an explicit local UID mapping, never inferred from a key.
        self.env = {"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(state)}
        # Which M4 surfaces this host serves (tests and `ORCH_DEV_SETTINGS=0` turn them off to see a host without them).
        self.settings_cap = os.environ.get("ORCH_DEV_SETTINGS", "1") != "0"
        self.write_ops = set(WRITE_OPS)
        self.versions = {name: 1 for name in (*OPS, *READS, *HUMAN_OPS, *WRITE_OPS)}
        # H12d-2: `reader.list` (cap `reader/1`) and the reader fields of `member.list`. The levels are the stub's own
        # (tests set them); no declaration is verified here. Off with ORCH_DEV_READERS=0 (a host before H12d-2).
        self.readers_cap = os.environ.get("ORCH_DEV_READERS", "1") != "0"
        self.reader_levels: dict[str, int] = {}
        self.required_reader = 0
        self.versions["reader.list"] = 1  # listed in `welcome.ops` only while `readers_cap`
        if not self.settings_cap:
            self.versions.pop("workspace.settings")
        self.requests = Requests(self)
        self.cursor = secrets.token_urlsafe(24)

        self.history = []
        self.journal = deque(maxlen=10000)
        self.cursors = {self.cursor: 0}
        self.pages = reads.Pages()
        # `read_ext/1` (v1.4): the optional fields. Off only to test a host that lacks them (ORCH_DEV_READ_EXT=0).
        self.ext = os.environ.get("ORCH_DEV_READ_EXT", "1") != "0"
        self.scan_budget = None  # activity.list scan budget per request (tests shorten it)
        self.merged = []
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
        self.merged = events
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

    def reader_view(self):
        """(levels, required) for the H12d-2 reads, or None for a host before H12d-2."""
        return (self.reader_levels, self.required_reader) if self.readers_cap else None

    def welcome(self):
        data = self.call("workspace.show", {})["data"]
        return {
            "t": "welcome",
            "proto": 1,
            "host": "orch-devhost/0.1.0",
            "host_uid": os.getuid(),
            "workspace": {"id": self.ws, "prefix": data["prefix"]},
            "peer": {"uid": os.getuid(), "class": "person", "person": self.person},
            "caps": [
                "ops/1",
                "query/1",
                "sub/1",
                "human/1",
                "diff/1",
                *(["read_ext/1", "read_ext/2", "activity/1"] if self.ext else []),
                *(["settings/1"] if self.settings_cap else []),
                *(["reader/1", "read_ext/3"] if self.readers_cap else []),
            ],
            "ops": {
                k: v
                for k, v in self.versions.items()
                if (k not in WRITE_OPS or k in self.write_ops) and (k != "reader.list" or self.readers_cap)
            },
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

    def call(self, op, args, opts=None, files=None):
        with self.lock:
            # Direct-mode files (core #375): a same-user dev process. Files.host belongs to the real host.
            runtime = HostWorkspace(self.env, files=Files.direct())
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
                if op in HUMAN_OPS:
                    return asdict(self.human(op, store, views, args, opts or {}, files))
                if (op not in OPS and op != "reader.list") or (op == "workspace.settings" and not self.settings_cap):
                    raise fail("unknown_command", exit=2)
                if args and op not in reads.TAKES_ARGS:
                    raise fail("invalid.input", exit=5)
                if op == "host.list":
                    data = {
                        "workspaces": [
                            {
                                "workspace_id": peer.ws,
                                "person": peer.person,
                                "prefix": peer.prefix,
                                "root": str(peer.root),
                                "state": "ok",
                                "tier": "A",
                                "socket": str(run_dir(self.state) / peer.ws / "host.sock"),
                            }
                            for peer in self.peers
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
                        **({"required_reader": self.required_reader} if self.readers_cap else {}),
                    }
                elif op == "member.list":
                    data = settingsops.members(ws, self.reader_view())
                elif op == "reader.list" and self.readers_cap:
                    data = settingsops.reader_list(ws, self.reader_view())
                elif op == "workspace.settings":
                    data = settingsops.settings(self, store, ws, self.person, args)
                elif op == "grant.list":
                    data = settingsops.grants(store, ws, self.person, args, self.ext)
                elif op == "needs":
                    data = reads.needs(self, store, views, ws, self.person, args)
                elif op == "ticket.query":
                    data = reads.query(self, store, views, args, self.person)
                elif op == "ticket.get":
                    data = reads.get(self, store, views, args, self.person)
                elif op == "activity.list":
                    data = reads.activity(self, store, views, ws, self.person, args)
                elif op == "diff":
                    data = reads.diff(store, views, args)
                elif op == "artifact.get":
                    data = reads.artifact(store, views, args)
                else:
                    raise fail("unknown_command", exit=2)
                return asdict(Result(data=data))
            finally:
                if runtime._store:
                    runtime.store.close()

    def human(self, op, store, views, args, opts, files=None):
        """`human.*` (v1.4 section 0.8.1): the stub plays the host and holds no key, so a signer submits."""
        if op != "human.prepare" and opts.get("dry_run") is not None:
            raise fail("invalid.input", exit=5)
        if op == "human.prepare":
            return Result(data=self.requests.prepare(store, views, args, opts, *([files] if files else [])))
        if op == "human.get":
            return Result(data=self.requests.get(store, views, args))
        if op == "human.pending":
            return Result(data=self.requests.pending(args))
        if op == "human.cancel":
            return Result(data=self.requests.cancel(args))
        done = self.requests.submit(store, args, opts)
        return Result(data=done["data"], key=done["key"], seq=done["seq"], duplicate=done["duplicate"])

    @staticmethod
    def inline_files(files, op):
        """`files` of a request (host contract 0.3): only `human.prepare` takes them, inline as `{b64}`."""
        if files is None:
            return None
        if op != "human.prepare":
            raise fail("blob.invalid", f"{str(op)[:40]} takes no files", exit=5)
        if not isinstance(files, dict) or len(files) > 16:
            raise fail("blob.invalid", "files is up to 16 named files", exit=5)
        out = {}
        for name, spec in files.items():
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name)
                or not isinstance(spec, dict)
                or set(spec) != {"b64"}
                or not isinstance(spec["b64"], str)
            ):
                raise fail("blob.invalid", "human.prepare takes each file inline as {b64}", exit=5)
            try:
                out[name] = base64.b64decode(spec["b64"], validate=True)
            except ValueError:
                raise fail("blob.invalid", "a file is not valid base64", exit=5) from None
            if len(out[name]) > 262144:
                raise fail("blob.invalid", "a file is larger than the inline limit", exit=5)
        return out

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
            while not self.stopped:
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
                        set(req) - {"t", "id", "op", "args", "opts", "files"}
                        or not {"t", "id", "op", "args"} <= req.keys()
                        or not isinstance(args, dict)
                        or not isinstance(op, str)
                        or not isinstance(req.get("opts", {}), dict)
                    ):
                        raise fail("req.invalid", exit=6)
                    opts = req.get("opts", {})
                    files = self.inline_files(req.get("files"), op)
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
                    if opts.get("dry_run") is not None and op != "human.prepare":
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
                        result = self.call(op, args, opts, files)
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
