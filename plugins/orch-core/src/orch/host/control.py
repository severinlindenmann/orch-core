"""The control socket's ops (§0.8.3, F8) and their crash recovery (§0.7 control intents).

* ``host.list``: the registry, each workspace's state and the **caller UID's** person mapping (R11, F8).
* ``host.wsk_create``: a new workspace id and key, ``O_CREAT|O_EXCL`` in a fresh 0700 directory; never replaces a key.
* ``host.register``: adopt a workspace (D-P2-18): ownership lock, registration, serving, unlock.
* ``host.unregister``: stop serving and remove the registration, which restores direct mode.
* ``host.bootstrap``: append a signed genesis with the new key as ``seq`` 1, then register (the ``init`` composite,
  whose client side is :func:`orch.ops.workspace_init.create_workspace_hosted`).
* ``host.outcome``: the stored outcome of a control write.
* ``host.restart``: drain (D-P2-4).

The three writes are control writes: ``opts.idem`` is required, an intent is fsynced before anything changes, and
:meth:`ControlOps.recover` finishes or rolls back every ``started`` intent at host start.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import frames
from .custody import check_tree, check_workspace_files, ensure_private_dir, unsafe, write_private
from .errors import HostError
from .intents import Intent, IntentStore
from .outcomes import CONTROL_WRITES, request_idem
from .ownership import OwnershipLock
from .registry import PERSON, Entry, group_name, validate_peers
from .transport import Session
from .workspace import ok_result, person_ok

if TYPE_CHECKING:
    from .service import Host

__all__ = ["CONTROL_OPS", "ControlGate", "ControlOps", "KEY_TTL_SECONDS", "WSK_MARKER"]

KEY_TTL_SECONDS = 3600
WSK_MARKER = "wsk_create.json"
_ROOT_FILES = ("config.json", "keys.jsonl", "events", "tickets", ".state")

_WS = {"type": "string", "pattern": r"^[0-9a-f]{32}$"}
_ROOT = {"type": "string", "minLength": 1, "maxLength": 1024}
_PEERS = {
    "type": "object",
    "minProperties": 1,
    "maxProperties": 64,
    "propertyNames": {"pattern": r"^[0-9]{1,10}$"},
    "additionalProperties": {
        "oneOf": [
            {
                "type": "object",
                "properties": {"class": {"const": "person"}, "person": {"type": "string", "pattern": PERSON.pattern}},
                "required": ["class", "person"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"class": {"enum": ["agent", "worker"]}},
                "required": ["class"],
                "additionalProperties": False,
            },
        ]
    },
}


def _closed(required: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": required, "required": list(required), "additionalProperties": False}


_SCHEMAS = {
    "host.list": _closed({}),
    "host.wsk_create": _closed({}),
    "host.restart": _closed({}),
    "host.register": _closed({"root": _ROOT, "peers": _PEERS}),
    "host.unregister": _closed({"workspace_id": _WS}),
    "host.outcome": _closed({"idem": {"type": "string", "pattern": r"^a_" + frames.ULID + "$"}}),
    "host.bootstrap": _closed(
        {
            "workspace_id": _WS,
            "root": _ROOT,
            "events": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "object"}},
            "peers": _PEERS,
        }
    ),
}
_VALIDATORS = {op: Draft202012Validator(schema) for op, schema in _SCHEMAS.items()}
CONTROL_OPS = {op: 1 for op in _SCHEMAS}
WRITES = CONTROL_WRITES


def _replay(record: dict[str, Any]) -> dict[str, Any]:
    """A stored terminal record as the answer to a same-key retry."""
    if record["step"] == "committed":
        return {**record["result"], "duplicate": True}
    raise HostError.stored(record["error"])


def _rolled_back(op: str) -> HostError:
    return HostError(
        "control.rolled_back", f"{op} was interrupted and rolled back at host start; retry with a new idem"
    )


class ControlOps:
    """The ops themselves. They block (locks, fsync, store opens) and run in worker threads, never on the loop."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.state_dir = host.state_dir
        self.host_uid = host.host_uid
        self.intents: IntentStore = host.intents
        self.namespace = f"control:{host.host_uid}"
        self.principal = f"uid:{host.host_uid}"

    # -- helpers
    def _hosts(self, workspace_id: str) -> Path:
        return self.state_dir / "hosts" / workspace_id

    def _begin(self, idem: str, op: str, args: dict[str, Any]) -> Intent | dict[str, Any]:
        return self.intents.begin(self.namespace, self.principal, idem, op, args)

    def _guarded(self, intent: Intent, run: Callable[[], dict[str, Any]], finish: Callable[[], bool]) -> dict[str, Any]:
        """Run a control write; store its terminal state. ``finish`` says whether the change landed after an
        unexpected exception (then the intent is completed by recovery-like logic)."""
        try:
            return run()
        except HostError as exc:
            if exc.retryable:
                self.intents.abandon(intent)
            else:
                self.intents.fail(intent, exc)
            raise
        except Exception:
            if not finish():
                self.intents.fail(intent, HostError("internal", f"{intent.op} failed"))
            raise

    # -- host.list
    def list(self, caller_uid: int) -> dict[str, Any]:
        rows = []
        for entry in self.host.registry.entries():
            service = self.host.services.get(entry.workspace_id)
            rows.append(
                {
                    "workspace_id": entry.workspace_id,
                    "prefix": entry.prefix,
                    "root": entry.root,
                    "state": service.state if service is not None else "unavailable",
                    "tier": entry.tier,
                    "socket": str(self.host.socket_path(entry.workspace_id)),
                    "person": self.host.registry.mapping(caller_uid, entry.workspace_id),
                }
            )
        return ok_result({"workspaces": rows})

    # -- host.wsk_create
    def wsk_create(self) -> dict[str, Any]:
        from orch import crypto
        from orch.custody import FileBackend

        self.sweep_keys()
        hosts = ensure_private_dir(self.state_dir / "hosts", self.host_uid)  # 0700, ancestors checked
        for _ in range(8):
            workspace_id = self.host.new_workspace_id()
            directory = hosts / workspace_id
            try:
                os.mkdir(directory, 0o700)  # fresh, or another id: an existing directory is never reused
            except FileExistsError:
                continue
            os.chmod(directory, 0o700)
            write_private(directory / WSK_MARKER, {"created_at": int(self.host.clock())})
            os.mkdir(directory / "keys", 0o700)
            pub = FileBackend(directory / "keys").create("wsk")  # linked into place exclusively (O_EXCL semantics)
            return ok_result({"workspace_id": workspace_id, "wsk_pub": crypto.b64u(pub)})
        raise HostError("internal", "could not draw a fresh workspace id")

    def sweep_keys(self) -> None:
        """A key from ``wsk_create`` with no genesis after 1 h is deleted (only directories ``wsk_create`` made)."""
        hosts = self.state_dir / "hosts"
        now = self.host.clock()
        with contextlib.suppress(FileNotFoundError):
            for name in os.listdir(hosts):
                directory = hosts / name
                marker = directory / WSK_MARKER
                if not re.fullmatch(r"[0-9a-f]{32}", name) or not marker.is_file():
                    continue
                if (directory / "genesis").exists() or self.host.registry.get(name) is not None:
                    continue
                try:
                    created = json.loads(marker.read_bytes())["created_at"]
                except (OSError, ValueError, KeyError, TypeError):
                    created = marker.stat().st_mtime
                if now - created > KEY_TTL_SECONDS and not self.host.bootstrapping(name):
                    shutil.rmtree(directory, ignore_errors=True)

    # -- host.register
    def _inspect(self, root: str) -> tuple[str, str, Any]:
        """Check a root for registration; returns (workspace id, prefix, workspace view)."""
        from orch.ops.caps import Files
        from orch.ops.runtime import _config
        from orch.store import Store, StoreError
        from orch.store.pins import HostPins

        check_tree(Path(root), self.host_uid)
        info = os.stat(root)
        if not stat.S_ISDIR(info.st_mode):
            raise unsafe(f"{root}: not a directory")
        cfg = _config(Path(root), Files.direct())  # the host's own read of a root it is asked to adopt
        if cfg is None:
            raise HostError("invalid.input", f"{root} is not an orch workspace")
        workspace_id = cfg["workspace"]["id"]
        if not isinstance(workspace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", workspace_id):
            raise HostError("invalid.input", "config.json names no valid workspace id")
        pins = HostPins(self.state_dir, workspace_id)
        try:
            pin = pins.genesis()
        except StoreError as exc:
            raise HostError(exc.code, exc.detail) from None
        finally:
            pins.close()
        if pin is None:
            raise HostError("trust.genesis_mismatch", "no genesis pin on this machine for this workspace")
        ensure_private_dir(self._hosts(workspace_id), self.host_uid, create=False)
        check_workspace_files(self.state_dir, workspace_id, Path(root), self.host_uid)
        try:
            store = Store.open(
                root, expected_workspace_id=workspace_id, expected_genesis=pin, host_state_dir=None, load="lazy"
            )
        except StoreError as exc:
            raise HostError(exc.code, exc.detail or exc.code) from None
        try:
            ws = store.state.workspace
            if ws.genesis != pin or ws.workspace_id != workspace_id:
                raise HostError("trust.genesis_mismatch", "the verified genesis differs from the pinned one")
            return workspace_id, ws.prefix, ws
        finally:
            store.close()

    def register(self, args: dict[str, Any], idem: str) -> dict[str, Any]:
        root = os.path.realpath(args["root"])
        begun = self._begin(idem, "host.register", {"root": root, "peers": args["peers"]})
        if isinstance(begun, dict):
            return _replay(begun)
        intent = begun
        if intent.resumed:
            return self._resume(intent)

        def run() -> dict[str, Any]:
            self._refuse_host_paths(root)
            if self.host.registry.by_root(root) is not None:
                raise HostError("host.busy", f"{root} is already registered")
            if self.host.bootstrapping_root(root):
                raise HostError("host.busy", f"{root} is being bootstrapped")
            workspace_id, prefix, ws = self._inspect(root)
            if self.host.registry.get(workspace_id) is not None:
                raise HostError("host.busy", f"workspace {workspace_id} is already registered")
            now = self.host.clock()
            peers = validate_peers(
                args["peers"],
                host_uid=self.host_uid,
                worker_uid=self.host.registry.worker_uid,
                pins=self.host.pins,
                person_valid=lambda person: person_ok(ws, person, now),
            )
            self.intents.update(intent, workspace_id=workspace_id)
            entry = Entry(workspace_id, prefix, root, self.host_uid, group_name(workspace_id), peers)
            with OwnershipLock(self.state_dir, workspace_id).exclusive(self.host.lock_wait):
                self.host.registry.add(entry)
                self.host.serve_from_thread(entry)
            return self.intents.complete(intent, ok_result({"workspace_id": workspace_id}))["result"]

        return self._guarded(intent, run, lambda: self._register_landed(intent))

    def _register_landed(self, intent: Intent) -> bool:
        entry = self._registered(intent)
        if entry is None:
            return False
        self.intents.complete(intent, ok_result({"workspace_id": entry.workspace_id}))
        return True

    def _registered(self, intent: Intent) -> Entry | None:
        """The registry entry **this** intent made, or ``None``. ``run`` records the workspace id durably before it
        adds the entry (and only after its busy checks found none), so an entry with that id and this root is this
        intent's own. An intent with no recorded id never added one: a registration of the same root by another
        request is never credited to it."""
        wid = intent.record.get("workspace_id")
        entry = self.host.registry.get(wid) if wid else None
        if entry is not None and entry.root == intent.args["root"]:
            return entry
        return None

    def _refuse_host_paths(self, root: str) -> None:
        """A workspace root never lies in the host's own state or run directory (custody list, sockets)."""
        for base in (self.state_dir, self.host.run_dir):
            real = os.path.realpath(base)
            if root == real or root.startswith(real.rstrip("/") + "/"):
                raise HostError("invalid.input", f"{root} lies inside the host's {base}")

    # -- host.unregister
    def unregister(self, args: dict[str, Any], idem: str) -> dict[str, Any]:
        workspace_id = args["workspace_id"]
        begun = self._begin(idem, "host.unregister", {"workspace_id": workspace_id})
        if isinstance(begun, dict):
            return _replay(begun)
        intent = begun
        if intent.resumed:
            return self._resume(intent)

        def run() -> dict[str, Any]:
            if self.host.registry.get(workspace_id) is None:
                raise HostError("not_found", f"workspace {workspace_id} is not registered")
            # TODO(H4a1): drain this workspace's requests first, as host.restart does; today a write already on
            # the worker thread finishes but its answer is lost (outcome.get recovers it once H2b lands).
            self.host.unserve_from_thread(workspace_id)
            self.host.registry.remove(workspace_id)
            return self.intents.complete(intent, ok_result({}))["result"]

        return self._guarded(intent, run, lambda: False)

    # -- host.bootstrap
    def bootstrap(self, args: dict[str, Any], idem: str) -> dict[str, Any]:
        root = os.path.realpath(args["root"])
        normalised = {**args, "root": root}
        workspace_id = args["workspace_id"]
        with self.host.bootstrap_guard(workspace_id, root):
            if self.intents.peek(self.namespace, self.principal, idem) is None:
                self._validate_bootstrap(normalised)  # read-only: a refusal here leaves no record and changes nothing
            begun = self._begin(idem, "host.bootstrap", normalised)
            if isinstance(begun, dict):
                return _replay(begun)
            intent = begun
            if intent.resumed:
                return self._resume(intent)
            try:
                peers, backend = self._validate_bootstrap(normalised)  # again, now that the intent is durable
            except BaseException:
                self.intents.abandon(intent)  # nothing was created: the same key may run again
                raise
            # Only now may recovery undo anything, and only what this bootstrap creates from here on.
            self.intents.update(intent, validated=True)
            return self._run_bootstrap(intent, root, peers, backend)

    def _check_new_root(self, root: str) -> None:
        check_tree(Path(root), self.host_uid)
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise unsafe(f"{root}: not a directory")
        if os.listdir(root):
            raise HostError("invalid.input", f"{root} is not empty")

    def _validate_bootstrap(self, args: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], Any]:
        """Every check that needs no change on disk; returns (peers, the key backend)."""
        from orch import crypto
        from orch.custody import FileBackend
        from orch.identity.certs import person_scope_ok
        from orch.store import StoreError
        from orch.store.pins import HostPins

        workspace_id, root, event = args["workspace_id"], args["root"], args["events"][0]
        if not re.fullmatch(r"[0-9a-f]{32}", workspace_id):
            raise HostError("invalid.input", "workspace_id is not a workspace id")
        self._refuse_host_paths(root)
        directory = self._hosts(workspace_id)
        pins = HostPins(self.state_dir, workspace_id)
        try:
            pinned = pins.genesis() is not None
        except StoreError:
            pinned = True
        finally:
            pins.close()
        if pinned or self.host.registry.get(workspace_id) is not None:
            raise HostError("host.busy", f"workspace {workspace_id} already has a genesis or a registration")
        backend = FileBackend(directory / "keys") if (directory / "keys").is_dir() else None
        if not (directory / WSK_MARKER).is_file() or backend is None or not backend.exists("wsk"):
            raise HostError("not_found", f"workspace {workspace_id} was not created by host.wsk_create")
        if self.host.registry.by_root(root) is not None:
            raise HostError("host.busy", f"{root} is already registered")
        self._check_new_root(root)
        if (
            event.get("type") != "workspace.created"
            or event.get("workspace_id") != workspace_id
            or event.get("wsk_pub") != crypto.b64u(backend.public_key("wsk"))
            or not isinstance(event.get("id"), str)
        ):
            raise HostError("invalid.input", "the genesis must be workspace.created for the id and key of wsk_create")
        owner = (event.get("owner") or {}).get("person")
        cert = event.get("device_cert") or {}
        peers = validate_peers(
            args["peers"],
            host_uid=self.host_uid,
            worker_uid=self.host.registry.worker_uid,
            pins=self.host.pins,
            person_valid=lambda person: person == owner and person_scope_ok(cert.get("o") or {}),
        )
        return peers, backend

    def _run_bootstrap(self, intent: Intent, root: str, peers: dict[str, dict[str, Any]], backend: Any) -> dict:
        from orch.store import BackendSigner, Store, StoreError

        workspace_id, event = intent.args["workspace_id"], intent.args["events"][0]
        try:
            store = Store.open(
                root,
                expected_workspace_id=workspace_id,
                host=BackendSigner(backend, "wsk"),
                host_state_dir=self.state_dir,
                clock=self.host.clock,
                workspace_name=Path(root).name or event.get("prefix"),
                load="lazy",
            )
            try:
                store.append(event, log="workspace")
            finally:
                store.close()
        except BaseException as exc:
            if not self._genesis_landed(root, event["id"]):
                self._clean_root(root)  # the root was empty when validated: only store-made names go
                error = HostError(exc.code, exc.detail or exc.code) if isinstance(exc, StoreError) else None
                self.intents.fail(intent, error or HostError("internal", "host.bootstrap failed"))
                if error is not None:
                    raise error from None
                raise
        # The genesis is in the log: from here the bootstrap is only ever finished, never undone.
        try:
            return self._finish_bootstrap(intent, root, event, peers)
        except BaseException:
            self.intents.release(intent)  # stays started: a same-key retry or the next start finishes it
            raise

    def _finish_bootstrap(
        self, intent: Intent, root: str, event: dict[str, Any], peers: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        workspace_id = intent.args["workspace_id"]
        with contextlib.suppress(FileNotFoundError):
            (self._hosts(workspace_id) / WSK_MARKER).unlink()
        if self.host.registry.get(workspace_id) is None:
            entry = Entry(workspace_id, str(event["prefix"]), root, self.host_uid, group_name(workspace_id), peers)
            with OwnershipLock(self.state_dir, workspace_id).exclusive(self.host.lock_wait):
                self.host.registry.add(entry)
                self.host.serve_from_thread(entry)
        result = ok_result({"workspace_id": workspace_id, "seq": 1}, seq=1)
        return self.intents.complete(intent, result)["result"]

    @staticmethod
    def _genesis_landed(root: str, planned_id: str) -> bool:
        from orch import canon

        path = Path(root) / "events" / "workspace.jsonl"
        try:
            with open(path, "rb") as handle:
                line = handle.readline()
            return canon.parse_event_line(line).get("id") == planned_id
        except Exception:  # noqa: BLE001 - a torn or unreadable first line: the genesis did not land
            return False

    @staticmethod
    def _foreign_root(root: str, workspace_id: str, planned_id: str) -> bool:
        """Did another workspace take over ``root`` after the host died? A direct ``orch init`` may have: a
        ``config.json`` that does not name ``workspace_id`` (unreadable counts), or an ``events/workspace.jsonl``
        whose first line is a genesis other than the planned one (``config.json`` is installed only after that line).
        Its files are not ours to delete."""
        path = Path(root) / "config.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            try:
                if json.loads(raw)["workspace"]["id"] != workspace_id:
                    return True
            except (ValueError, KeyError, TypeError):
                return True
        try:
            with open(Path(root) / "events" / "workspace.jsonl", "rb") as handle:
                first = json.loads(handle.readline(1 << 20))
        except (OSError, ValueError):
            return False  # no first line, or a torn one: nothing a direct init finished
        return isinstance(first, dict) and first.get("id") != planned_id

    @staticmethod
    def _clean_root(root: str, *, remove_root: bool = False) -> None:
        """Remove what the store created in a new root; never anything else."""
        for name in _ROOT_FILES:
            path = Path(root) / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()
        if remove_root:
            with contextlib.suppress(OSError):
                os.rmdir(root)

    # -- host.outcome
    def outcome(self, args: dict[str, Any]) -> dict[str, Any]:
        return ok_result(self.intents.outcome(self.namespace, self.principal, args["idem"]))

    # -- recovery: at start, and for a same-key retry of an intent left started
    def _resume(self, intent: Intent) -> dict[str, Any]:
        record = self._recover_one(intent, live=True)
        if record["step"] == "started":
            raise HostError("outcome.unknown", f"{intent.op} could not be finished yet; retry with the same idem")
        return _replay(record) if record["step"] == "failed" else record["result"]

    def recover(self) -> list[tuple[str, str]]:
        """Finish or roll back every ``started`` intent; return (op, step) for each. A bootstrap whose genesis
        landed but that cannot be finished now stays ``started`` and is retried at the next start."""
        return [(intent.op, self._recover_one(intent, live=False)["step"]) for intent in self.intents.started()]

    def _recover_one(self, intent: Intent, *, live: bool) -> dict[str, Any]:
        op, args = intent.op, intent.args
        validator = _VALIDATORS.get(op)
        if (
            op not in WRITES
            or validator is None
            or not isinstance(args, dict)
            or not validator.is_valid(args)
            or (op != "host.unregister" and not str(args.get("root", "")).startswith("/"))
        ):
            return self.intents.fail(intent, HostError("internal", f"unreadable control intent {op!r}; left as is"))
        if op == "host.register":
            entry = self._registered(intent)
            if entry is not None:
                return self.intents.complete(intent, ok_result({"workspace_id": entry.workspace_id}))
            return self.intents.fail(intent, _rolled_back(op))
        if op == "host.unregister":
            if live:
                self.host.unserve_from_thread(args["workspace_id"])
            self.host.registry.remove(args["workspace_id"])
            return self.intents.complete(intent, ok_result({}))
        return self._recover_bootstrap(intent)

    def _recover_bootstrap(self, intent: Intent) -> dict[str, Any]:
        from orch.custody import FileBackend
        from orch.store import BackendSigner, Store
        from orch.store.pins import HostPins

        args = intent.args
        workspace_id, root, event = args["workspace_id"], args["root"], args["events"][0]
        directory = self._hosts(workspace_id)
        if not intent.record.get("validated"):
            # Died before the checks passed: this bootstrap created nothing, so nothing is touched.
            return self.intents.fail(intent, _rolled_back("host.bootstrap"))
        if self._genesis_landed(root, event["id"]):
            try:  # finish the append's projection and pin the genesis the host itself appended
                store = Store.open(
                    root,
                    expected_workspace_id=workspace_id,
                    host=BackendSigner(FileBackend(directory / "keys"), "wsk"),
                    host_state_dir=self.state_dir,
                    clock=self.host.clock,
                    load="lazy",
                )
                store.close()
                peers = {k: dict(v) for k, v in args["peers"].items()}
                self._finish_bootstrap(intent, root, event, peers)
            except Exception:  # noqa: BLE001 - store.busy, lock.busy...: never delete a landed genesis
                self.intents.release(intent)
            return intent.record
        # Validated but not landed: undo only what this bootstrap created, and never a registered workspace.
        registered = self.host.registry.get(workspace_id) is not None or self.host.registry.by_root(root) is not None
        if not registered:
            if not self._foreign_root(root, workspace_id, event["id"]):
                self._clean_root(root, remove_root=True)
            pins = HostPins(self.state_dir, workspace_id)
            try:
                pinned = (directory / "genesis").exists() or pins.genesis() is not None
            except Exception:  # noqa: BLE001 - an unreadable pin: keep everything
                pinned = True
            finally:
                pins.close()
            if (directory / WSK_MARKER).is_file() and not pinned:
                shutil.rmtree(directory, ignore_errors=True)
        return self.intents.fail(intent, _rolled_back("host.bootstrap"))


class ControlGate:
    """The transport :class:`~orch.host.transport.Handler` of the control socket."""

    def __init__(self, host: Host) -> None:
        self.host = host

    def __call__(self, session: Session, frame: dict[str, Any], request: frames.Request) -> Any:
        op = frame["op"]

        def refuse(error: HostError) -> None:
            session.respond(request, error.response(request.id))

        if op not in CONTROL_OPS:
            refuse(HostError("unknown_command", f"{op} is not a control op"))
            return None
        if self.host.draining:
            refuse(self.host.restarting())
            return None
        if "auth" in frame:
            refuse(HostError("req.invalid", "auth is not allowed on the control socket"))
            return None
        if not session.admit(request):
            return None
        return self._work(session, frame, request)

    async def _work(self, session: Session, frame: dict[str, Any], request: frames.Request) -> None:
        import asyncio

        op, args, opts = frame["op"], frame["args"], frame.get("opts") or {}
        try:
            errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
            if errors:
                where = ".".join(map(str, errors[0].absolute_path))
                raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
            idem = request_idem(op, opts)  # dry_run is invalid.input on every control op; a write needs its idem
            if not session.state.start(request, "write" if op in WRITES else "read"):
                return
            ops = self.host.control
            if op == "host.restart":
                session.respond(
                    request, {"t": "res", "id": request.id, "ok": True, "result": ok_result({"draining": True})}
                )
                self.host.begin_drain()
                return
            call: Callable[[], dict[str, Any]] = {
                "host.list": lambda: ops.list(session.peer.uid),
                "host.wsk_create": ops.wsk_create,
                "host.register": lambda: ops.register(args, idem),
                "host.unregister": lambda: ops.unregister(args, idem),
                "host.bootstrap": lambda: ops.bootstrap(args, idem),
                "host.outcome": lambda: ops.outcome(args),
            }[op]
            result = await asyncio.to_thread(call)
            session.respond(request, {"t": "res", "id": request.id, "ok": True, "result": result})
        except HostError as exc:
            session.respond(request, exc.response(request.id))
