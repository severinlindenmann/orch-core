"""The shared runtime of the agent operations (C6): find and open the workspace, build the actor from
``ORCH_GRANT`` and ``ORCH_SESSION``, resolve a ``REF``, read text and files safely, plan and append events.

Design, in the order a handler uses it:

* :class:`Workspace` is per CLI call. It walks up from the working directory (or takes ``ORCH_WORKSPACE``) to the
  directory whose ``config.json`` says ``orch.workspace/2``, and opens the :class:`~orch.store.Store` on first use. The
  workspace key is the file-tier key ``<state dir>/hosts/<workspace id>/keys/wsk`` (state dir: ``ORCH_STATE_DIR``, else
  ``$XDG_CONFIG_HOME/orch``, else ``~/.config/orch``); without it the store is read-only and a write fails.
  ``config.json`` is **unsigned**: it only names the workspace id, the genesis pin decides what is trusted.
* :class:`Call` is per handler run. ``actor()`` is an agent with ``for`` and ``grant`` when ``ORCH_GRANT`` is set, an
  unattended agent otherwise (only for operations that allow it); ``resolve()`` turns a ``REF`` (or "my claim") into a
  ticket view the actor may see; nothing here decides authorization, ``orch.model.admit`` does, at append.
* :class:`Projection` plans the events of one call on top of the current state with ``orch.model.preview`` (the same
  rules as ``admit``, each event judged after the ones before it) while the workspace lock is held, and appends them
  one by one only when every one is admitted: a call that writes several events (``handoff``, ``task done``,
  ``apply``) is all or nothing, short of a crash between two appends (each carries its own idempotency key, so the
  retry finishes the call instead of repeating it).
* :class:`Notes` is what ``orch`` remembers per session and ticket in ``.state/sessions``: the ``base_rev`` of every
  section and field the session was shown or wrote (agents never pass it, F1 10.4 item 8), the cursor (the highest
  ``seq`` it was shown) and where its own last write is (what ``wait`` waits after).
* Text from ``--file``/stdin is size-capped, strictly UTF-8, normalised (F1 11.3) and refused if it holds a grant
  secret shape.

Errors a handler raises are :class:`OrchError`; :func:`guarded` maps what the store raises (``StoreError``) through
``orch.cli.store_errors`` onto the operation's declared codes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import canon
from orch.canon.text import TextError, clean_line, normalize_text
from orch.ops.base import Context, Handler, Result
from orch.ops.errors import GLOBAL_ERRORS, OrchError

__all__ = [
    "Call",
    "Notes",
    "Projection",
    "Workspace",
    "find_workspace",
    "guarded",
    "records_dir",
    "state_dir",
]

CONFIG_SCHEMA = "orch.workspace/2"
GRANT_SHAPE = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
_AGENT_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
_TASK_ONLY = re.compile(r"T[1-9][0-9]*")
_TEXT_LIMIT = 4096
FILE_LIMIT = 1 << 20  # text read from --file or stdin: more than 1 MiB is not a note, a section or an answer
ARTIFACT_LIMIT = 64 << 20
_EMPTY = canon.section_hash("")


# --------------------------------------------------------------------------------------------------- the workspace


def state_dir(env: Mapping[str, str]) -> Path:
    """The host state directory (genesis pins, noted revocations, workspace keys)."""
    if env.get("ORCH_STATE_DIR"):
        return Path(env["ORCH_STATE_DIR"])
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]) / "orch"
    return Path(env.get("HOME") or os.path.expanduser("~")) / ".config" / "orch"


def _config(path: Path) -> dict[str, Any] | None:
    """``config.json`` of a workspace directory, or ``None`` if ``path`` is not one (a symlink is not read)."""
    cfg = path / "config.json"
    try:
        if cfg.is_symlink() or not cfg.is_file():
            return None
        raw = cfg.read_bytes() if cfg.stat().st_size <= (1 << 20) else b""
        doc = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    ws = doc.get("workspace") if isinstance(doc, dict) else None
    if doc.get("schema") != CONFIG_SCHEMA or not isinstance(ws, dict) or not isinstance(ws.get("id"), str):
        return None
    return doc


def find_workspace(env: Mapping[str, str], cwd: str | os.PathLike[str] | None = None) -> Path | None:
    """``ORCH_WORKSPACE`` if it names a workspace, else the nearest directory above ``cwd`` that is one or holds
    ``orchestrator/`` that is. ``None`` when there is none."""
    explicit = env.get("ORCH_WORKSPACE")
    if explicit:
        p = Path(explicit)
        return p if _config(p) else None
    here = Path(cwd or os.getcwd()).resolve()
    for d in (here, *here.parents):
        for cand in (d, d / "orchestrator"):
            if _config(cand):
                return cand
    return None


def records_dir(root: Path) -> Path:
    return root / ".state" / "sessions"


class Workspace:
    """One CLI call's view of the workspace: found and opened lazily, once."""

    def __init__(self, env: Mapping[str, str], now: Callable[[], float] = time.time, cwd: str | None = None) -> None:
        self.env = env
        self.now = now
        self._cwd = cwd
        self._root: Path | None = None
        self._looked = False
        self._store: Any = None

    @property
    def root(self) -> Path | None:
        if not self._looked:
            self._root, self._looked = find_workspace(self.env, self._cwd), True
        return self._root

    def require_root(self) -> Path:
        root = self.root
        if root is None:
            raise OrchError("not_found", "no orch workspace here", hint="run orch init in the workspace directory")
        return root

    @property
    def store(self) -> Any:
        if self._store is None:
            self._store = self._open(self.require_root())
        return self._store

    def _open(self, root: Path) -> Any:
        from orch.custody import FileBackend
        from orch.store import BackendSigner, Store

        cfg = _config(root) or {}
        wid = cfg["workspace"]["id"]
        sd = state_dir(self.env)
        host = None
        keys = sd / "hosts" / wid / "keys"
        if keys.is_dir():
            backend = FileBackend(keys)
            if backend.exists("wsk"):
                host = BackendSigner(backend, "wsk")
        return Store.open(
            root,
            expected_workspace_id=wid,
            host=host,
            host_state_dir=sd,
            clock=self.now,
            workspace_name=cfg["workspace"].get("name"),
            load="lazy",
        )


def guarded(handler: Handler, name: str, declared: list[str]) -> Handler:
    """``handler`` with what the store and the key custody raise turned into the operation's error codes."""

    def run(ctx: Context, args: dict[str, Any]) -> Result:
        from orch.cli.store_errors import to_orch_error
        from orch.custody import CustodyError
        from orch.store import StoreError

        try:
            return handler(ctx, args)
        except StoreError as e:
            raise to_orch_error(e, declared) from e
        except CustodyError as e:
            raise OrchError("internal", f"the workspace key: {e}") from e

    run.__name__ = f"handle_{name.replace('.', '_')}"
    return run


# --------------------------------------------------------------------------------------------------- session notes


class Notes:
    """What a session was shown or wrote, per ticket: ``base`` (path -> hash), ``cursor`` and ``since``.

    One ``<session>.notes.json`` under ``.state/sessions``, replaced atomically under the sessions lock. It is advisory:
    an unreadable file counts as empty, which only makes the next edit ask the agent to read first (a conflict), never
    lets a stale write through. At most 100 tickets are kept."""

    KEEP = 100

    def __init__(self, directory: Path | None, session: str | None) -> None:
        self.directory = directory
        self.session = session
        self._mem: dict[str, dict[str, Any]] = {}

    def _path(self) -> Path | None:
        if self.directory is None or not self.session:
            return None
        return self.directory / (re.sub(r"[^A-Za-z0-9_-]", "_", self.session)[:120] + ".notes.json")

    def _load(self) -> dict[str, dict[str, Any]]:
        p = self._path()
        if p is None:
            return self._mem
        try:
            doc = json.loads(p.read_bytes())
            tickets = doc["tickets"]
            if isinstance(tickets, dict) and all(isinstance(v, dict) for v in tickets.values()):
                return tickets
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            pass
        return {}

    def _store(self, tickets: dict[str, dict[str, Any]]) -> None:
        p = self._path()
        if p is None:
            self._mem = tickets
            return
        from orch.store.fsio import write_atomic

        newest = sorted(tickets, key=lambda u: tickets[u].get("at", 0), reverse=True)[: self.KEEP]
        write_atomic(p, json.dumps({"tickets": {u: tickets[u] for u in newest}}).encode(), mode=0o600, durable=False)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if self.directory is None or not self.session:
            yield
            return
        from orch.store.lock import FileLock

        self.directory.mkdir(parents=True, exist_ok=True)
        with FileLock(self.directory / ".lock"):
            yield

    def get(self, uid: str) -> dict[str, Any]:
        with self._locked():
            e = self._load().get(uid) or {}
        return {"cursor": int(e.get("cursor", 0)), "since": e.get("since"), "base": dict(e.get("base", {}))}

    def update(
        self,
        uid: str,
        *,
        now: float,
        base: Mapping[str, str] | None = None,
        cursor: int | None = None,
        since: int | None = None,
    ) -> None:
        if not self.session:
            return
        with self._locked():
            tickets = self._load()
            e = tickets.setdefault(uid, {})
            if base:
                e.setdefault("base", {}).update(base)
            if cursor is not None:
                e["cursor"] = max(int(e.get("cursor", 0)), cursor)
            if since is not None:
                e["since"] = since
            e["at"] = int(now)
            self._store(tickets)


def path_hash(view: Any, path: str) -> str:
    """The hash ``base_rev`` holds for ``path`` in ``view``: the section hash, or the value hash of a ticket field."""
    from orch.store.render import thaw

    if path.startswith("body."):
        return view.sections.get(path[5:], {"hash": _EMPTY})["hash"]
    return canon.value_hash(thaw(view.fields[path[len("ticket.") :]]))


# --------------------------------------------------------------------------------------------------- planning


@dataclass
class Planned:
    event: dict[str, Any]
    body: dict[str, str | None] | None = None
    artifacts: dict[str, bytes] | None = None


class Projection:
    """The events of one call, planned and judged on top of the current state (the workspace lock must be held from
    construction to :meth:`commit`)."""

    def __init__(self, call: Call, view: Any) -> None:
        self.call = call
        self.uid = view.uid
        self.view = view
        store = call.store
        stamp = store.peek_stamp(self.uid)
        self._seq, self._prev = stamp["seq"] - 1, stamp["prev"]
        self._at, self._ws_seq = stamp["at"], stamp["ws_seq"]
        self._state = store.state
        self.planned: list[Planned] = []
        self._probes: list[dict[str, Any]] = []
        self.bases: dict[str, str] = {}
        self.first_seq = stamp["seq"]
        self.last_view = view

    def base(self, path: str) -> str:
        """The ``base_rev`` of ``path``: what this call wrote, else what the session was shown; refused as a conflict if
        the session never read it or it changed since."""
        if path in self.bases:
            return self.bases[path]
        noted = self.call.notes.get(self.uid)["base"].get(path)
        code = "conflict.section" if path.startswith("body.") else "conflict.field"
        what = path[5:] if path.startswith("body.") else path[len("ticket.") :]
        if noted is None:
            raise OrchError(code, f"{what} was not read in this session: orch show --section {what}", retryable=True)
        if noted != path_hash(self.view, path):
            raise OrchError(code, f"{what} changed since you read it", retryable=True)
        return noted

    def add(
        self,
        event: dict[str, Any],
        *,
        body: dict[str, str | None] | None = None,
        artifacts: dict[str, bytes] | None = None,
    ) -> Any:
        """Judge ``event`` (without actor and stamps) after the planned ones; returns the ticket view after it."""
        from orch.model import Refusal, preview
        from orch.store import StoreError

        ev = {**event, "actor": self.call.actor()}
        # a ticket the builder just loaded (a parent, a blocker) can move the merged order: stamp again, never earlier
        stamp = self.call.store.peek_stamp(self.uid)
        self._at = max(self._at, stamp["at"])
        probe = {
            **ev,
            "v": 2,
            "hash_v": 1,
            "id": canon_ulid(),
            "based_on": self._prev,
            "prev": self._prev,
            "seq": self._seq + 1,
            "at": self._at,
            "ws_seq": self._ws_seq,
        }
        r = preview(self._state, probe, log=self.uid)
        if isinstance(r, Refusal):
            raise StoreError.from_refusal(r)
        self._state, self._seq, self._prev = r, self._seq + 1, canon.event_head(probe)
        self._probes.append(probe)
        self.planned.append(Planned(ev, body, artifacts))
        self.last_view = r.tickets[self.uid]
        for path in ev.get("base_rev", {}):
            self.bases[path] = path_hash(self.last_view, path)
        return self.last_view

    def refresh(self) -> None:
        """Judge from the store's current state again, with the events planned so far replayed on top. A builder calls
        this after it loaded another ticket (a parent, a blocker): the state it planned on did not have it yet."""
        from orch.model import Refusal, preview
        from orch.store import StoreError

        state = self.call.store.state
        for probe in self._probes:
            r = preview(state, probe, log=self.uid)
            if isinstance(r, Refusal):
                raise StoreError.from_refusal(r)
            state = r
        self._state = state
        self.last_view = state.tickets[self.uid]

    @property
    def last_seq(self) -> int:
        return self._seq

    def commit(self) -> list[Any]:
        """Append what was planned (nothing under ``--dry-run``), then update the session's notes."""
        c = self.call
        if c.ctx.dry_run or not self.planned:
            return []
        out = []
        for i, p in enumerate(self.planned):
            idem = f"{c.ctx.idem}:{i}" if c.ctx.idem else None
            out.append(c.store.append(p.event, log=self.uid, body=p.body, artifacts=p.artifacts, idem=idem))
        c.wrote(self.uid, self.first_seq - 1, out[-1].event["seq"], self.bases, c.store.state.tickets[self.uid])
        return out


def canon_ulid() -> str:
    from orch.identity import new_ulid

    return new_ulid()


# --------------------------------------------------------------------------------------------------- one call


class Call:
    """What one handler run needs. Build it with :meth:`of`."""

    def __init__(self, ctx: Context, op: str) -> None:
        import orch.ops as ops

        self.ctx = ctx
        self.op = op
        self.declared = {e["code"] for e in ops.get(op).errors} | set(GLOBAL_ERRORS)
        self.ws: Workspace = ctx.workspace or Workspace(ctx.env, ctx.now)
        self._person: str | None | bool = False
        self._notes: Notes | None = None

    @classmethod
    def of(cls, ctx: Context, op: str) -> Call:
        return cls(ctx, op)

    # -- identity
    @property
    def store(self) -> Any:
        return self.ws.store

    @property
    def now(self) -> float:
        return self.ctx.now()

    @property
    def grant_id(self) -> str | None:
        return self.ctx.grant.partition(".")[0] if self.ctx.grant else None

    @property
    def person(self) -> str | None:
        """The person the grant is for, **after the same check the CLI makes for a write** (existence, secret, expiry,
        revocation): a grant id alone is public (it is in the log), so a read never takes a person from one. ``None``
        without a (valid) grant: reads then see only ``workspace`` tickets."""
        if self._person is False:
            from orch.cli.store_hooks import grant_person

            try:
                self._person = grant_person(self.store, self.ctx.grant) if self.ctx.grant else None
            except OrchError:
                self._person = None
        return self._person  # type: ignore[return-value]

    @property
    def notes(self) -> Notes:
        if self._notes is None:
            root = self.ws.root
            self._notes = Notes(records_dir(root) if root else None, self.ctx.session)
        return self._notes

    def agent_id(self) -> str:
        h = self.ctx.env.get("ORCH_HARNESS", "")
        return h if _AGENT_ID.fullmatch(h) else "orch-cli"

    def actor(self) -> dict[str, Any]:
        """The event actor: an agent with its grant, or an unattended agent when there is no grant."""
        if not self.ctx.session:
            raise OrchError("invalid.input", "ORCH_SESSION is not set: an agent event needs its session id")
        base = {"kind": "agent", "id": self.agent_id(), "session": self.ctx.session}
        if self.ctx.grant:
            person = self.person
            if person is None:
                raise OrchError("grant.expired", "the grant is not a grant of this workspace")
            return {**base, "for": person, "grant": self.grant_id}
        return {**base, "unattended": True}

    @property
    def attended(self) -> bool:
        return bool(self.ctx.grant)

    # -- tickets
    def sees(self, view: Any) -> bool:
        if view.visibility == "workspace":
            return True
        return self.person is not None and self.person in view.visibility["restricted"]

    def mine(self, *, live_only: bool = True) -> list[Any]:
        """Tickets whose claim belongs to this session's family (``s_X`` and ``s_X.1`` share ``s_X``'s claim)."""
        from orch.model.claims import in_family

        session = self.ctx.session
        if not session:
            return []
        self.store.load_all()
        out = []
        for v in self.store.state.tickets.values():
            c = v.claim
            if c is not None and (c.live or not live_only) and in_family(session, c.session) and self.sees(v):
                out.append(v)
        return sorted(out, key=lambda v: v.key)

    def resolve(self, ref: str | None = None, *, live_only: bool = True, need_claim: bool = False) -> Any:
        """The ticket view for ``ref`` (``DEMO-0043``, ``43``, ``DEMO-0043/T3``) or, without one, the session's single
        claim. Refuses a ticket the actor may not see as ``not_found`` (never as "hidden")."""
        part = ref.partition("/")[0] if ref else None
        if part is None or _TASK_ONLY.fullmatch(part):
            mine = self.mine(live_only=live_only)
            if len(mine) == 1:
                return mine[0]
            names = ", ".join(v.key for v in mine) or "none"
            raise OrchError("ambiguous_ref", f"name the ticket REF; your claims: {names}")
        view = self.store.ticket(part)
        if view is None or not self.sees(view):
            raise OrchError("not_found", f"no ticket {clean_line(part)[:60]}")
        if need_claim:
            self.require_claim(view)
        return view

    def require_claim(self, view: Any, *, live_only: bool = True, own: bool = False) -> None:
        """The session (or, unless ``own``, its parent: ``s_X.1`` works under ``s_X``'s claim) holds the claim."""
        from orch.model.claims import in_family

        c, s = view.claim, self.ctx.session
        if c is None or not s or not in_family(s, c.session) or (live_only and not c.live):
            raise OrchError("claim.required", f"you hold no live claim on {view.key}")
        if own and s != c.session:  # the schema lets an agent release only the claim of its own session
            raise OrchError("claim.required", f"{view.key} is claimed by {c.session}; only that session lets go of it")

    def ticket_of_task(self, task: str) -> tuple[Any, str]:
        """``T3`` (my claim) or ``DEMO-0043/T3`` as ``(view, "T3")``."""
        ref, _, tid = task.rpartition("/")
        return self.resolve(ref or None, need_claim=False), tid

    # -- notes
    def shown(
        self, view: Any, *, fields: tuple[str, ...] = (), sections: tuple[str, ...] = (), seq: int | None = None
    ) -> None:
        """The session was shown these fields and sections at ``view``: they are its ``base_rev`` now."""
        base = {f"ticket.{f}": path_hash(view, f"ticket.{f}") for f in fields}
        base.update({f"body.{s}": path_hash(view, f"body.{s}") for s in sections})
        head = self.store.head_seq(view.uid) if seq is None else seq
        self.notes.update(view.uid, now=self.now, base=base, cursor=head)

    def wrote(self, uid: str, before: int, last: int, bases: Mapping[str, str], view: Any) -> None:
        """After an append of events ``before+1 .. last``: the written paths are known, the cursor moves over the
        session's own events only when it had seen everything before them, and ``wait`` waits after ``last``."""
        cur = self.notes.get(uid)["cursor"]
        self.notes.update(uid, now=self.now, base=bases, cursor=last if cur >= before else None, since=last)

    def cursor(self, uid: str) -> int:
        return self.notes.get(uid)["cursor"]

    # -- text and files
    def _fail_text(self, msg: str) -> OrchError:
        return OrchError("parse.text" if "parse.text" in self.declared else "invalid.input", msg)

    def check_secret(self, data: str | bytes) -> None:
        text = data if isinstance(data, str) else data.decode("latin-1")
        secret = (self.ctx.grant or "").partition(".")[2]
        if GRANT_SHAPE.search(text) or (len(secret) >= 8 and secret in text):
            raise OrchError("grant.secret_in_args", "the text contains a grant secret; it was not stored")

    def read_text_source(
        self, args: Mapping[str, Any], names: tuple[str, ...] = ("text", "message", "file")
    ) -> str | None:
        """The one text the call carries: a positional or ``-m`` value, or the content of ``--file PATH|-``."""
        given = [n for n in names if args.get(n) is not None]
        if not given:
            return None
        if len(given) > 1:
            raise OrchError("invalid.input", "give exactly one of " + ", ".join(given))
        name = given[0]
        value = args[name]
        if name != "file":
            self.check_secret(value)
            return value
        return self.read_file_text(value)

    def read_file_text(self, source: str) -> str:
        raw = self._read_limited(source)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise self._fail_text("the file is not UTF-8") from None
        self.check_secret(text)
        return text

    def _read_limited(self, source: str) -> bytes:
        if source == "-":
            stream = getattr(sys.stdin, "buffer", None)
            data = stream.read(FILE_LIMIT + 1) if stream is not None else sys.stdin.read(FILE_LIMIT + 1).encode()
        else:
            try:
                fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                raise OrchError("invalid.input", f"cannot read {clean_line(source)[:80]}: {e.strerror}") from None
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise OrchError("invalid.input", f"{clean_line(source)[:80]} is not a regular file")
                data = f.read(FILE_LIMIT + 1)
        if len(data) > FILE_LIMIT:
            raise OrchError("invalid.input", f"the text is longer than {FILE_LIMIT} bytes")
        return data

    def text(self, value: str, *, one_line: bool = False, limit: int = _TEXT_LIMIT, what: str = "text") -> str:
        """``value`` normalised (CRLF, NFC) and checked against the text rules and ``limit`` bytes."""
        self.check_secret(value)
        try:
            out = normalize_text(value, one_line=one_line)
        except TextError as e:
            raise self._fail_text(f"{what}: {e}") from None
        if len(out.encode("utf-8")) > limit:
            raise OrchError("invalid.input", f"{what} is longer than {limit} bytes")
        return out

    def body_text(self, value: str, *, limit: int = _TEXT_LIMIT, what: str = "text") -> str:
        """Prose for a section or a note: normalised, and without leading or trailing line feeds (F1 section 4)."""
        out = self.text(value, limit=limit, what=what).strip("\n")
        if not out:
            raise OrchError("invalid.input", f"{what} is empty")
        return out

    def read_artifact(self, path: str) -> bytes:
        """The bytes of an evidence file: a regular file, at most 64 MiB, free of grant secrets."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            raise OrchError("invalid.input", f"cannot read {clean_line(path)[:80]}: {e.strerror}") from None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise OrchError("invalid.input", f"{clean_line(path)[:80]} is not a regular file")
            if st.st_size > ARTIFACT_LIMIT:
                raise OrchError("invalid.input", f"{clean_line(path)[:80]} is larger than {ARTIFACT_LIMIT} bytes")
            data = f.read(ARTIFACT_LIMIT + 1)
        if len(data) > ARTIFACT_LIMIT:
            raise OrchError("invalid.input", f"{clean_line(path)[:80]} is larger than {ARTIFACT_LIMIT} bytes")
        self.check_secret(data)
        return data

    # -- planning and results
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with self.store.locked():
            yield

    def projection(self, view: Any) -> Projection:
        return Projection(self, view)

    def result(
        self,
        view: Any,
        data: Any,
        *,
        seq: int | None = None,
        hints: list[str] | None = None,
        lines: list[str] | None = None,
        dry_run_note: bool = True,
    ) -> Result:
        head = self.store.head_seq(view.uid)
        lines = list(lines or [])
        if self.ctx.dry_run and dry_run_note:
            lines.insert(0, "dry-run: nothing was written")
        return Result(
            data=data,
            key=view.key,
            seq=head if seq is None else seq,
            cursor=self.cursor(view.uid),
            hints=hints or [],
            lines=lines,
        )


def short(text: str, n: int = 70) -> str:
    """One line of ticket content, flattened and cut (the caller fences it)."""
    one = clean_line(text)
    return one if len(one) <= n else one[: n - 1] + "…"
