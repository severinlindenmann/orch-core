"""The dashboard reads (H6b, §0.8, §0.8.4, §0.8.5): ``ticket.query``, ``ticket.get``, ``needs``, ``activity.list``,
``workspace.show``, ``member.list``, ``session.list``, ``grant.list``, and v1.5 ``workspace.settings`` and
``ticket.text``.

This module holds what every one of them shares: the argument schemas (§0.6 step 8: ``invalid.input``), the op table,
and :class:`ReadCtx`, the one object a handler gets. The handlers live in :mod:`orch.host.reads_query`
(``ticket.query``,
``needs``), :mod:`orch.host.reads_ticket` (``ticket.get``, ``ticket.text``), :mod:`orch.host.reads_feed`
(``activity.list``) and :mod:`orch.host.reads_ws` (``workspace.show``, ``member.list``, ``session.list``,
``grant.list``, ``workspace.settings``).

**Rules every handler follows** (§0.5, F6):

* the viewer is :class:`~orch.host.visibility.Viewer` (``ctx.viewer``), derived for **this** request on the worker
  thread; a ref the viewer may not see is ``not_found`` through :meth:`Viewer.ticket`, exactly as one that does not
  exist;
* every row, count, item and page is filtered **before** it is counted or returned, and a derived value is computed only
  for a ticket that passed the filter, only from that ticket's own logs;
* a snapshot read takes ``ctx.sub_cursor()`` right where it reads its state (the journal head after a foreign-append
  check, H6a), so ``sub.open {after: sub_cursor}`` loses and repeats nothing;
* no ``sig``, ``host_sig``, ``secret_hash``, key, grant secret or signing byte is returned (the dispatcher's
  :class:`~orch.host.auth.Redactor` runs over the result as well).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .pages import Pages
    from .workspace import Call

__all__ = ["CAPS", "READ_OPS", "SCHEMAS", "ReadCtx", "iso", "serve"]

#: the caps the reads add to ``welcome.caps`` (§0.4); each ``read_ext/N`` lists every lower one too (``read_ext/3``,
#: v1.9: the ``member.list`` device fields)
CAPS = ["query/1", "read_ext/1", "read_ext/2", "read_ext/3", "activity/1", "settings/1", "text/1"]

# --- argument schemas (§0.8, §0.8.5) ------------------------------------------------------------------------------

_STATUS = {"enum": ["backlog", "open", "in_progress", "waiting", "testing", "done", "closed"]}
_PERSON = {"type": "string", "pattern": r"^p_[0-9a-f]{32}$"}
_TIME = {
    "type": "string",
    "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$",
}
_REF = {"type": "string", "minLength": 1, "maxLength": 64}
_ULID = {"type": "string", "pattern": r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_PAGE = {"type": "string", "minLength": 1, "maxLength": 512}
_INT = {"type": "integer"}


def _strs(lo: int, hi: int, max_len: int = 64, **item: Any) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": lo,
        "maxItems": hi,
        "items": {"type": "string", "minLength": 1, "maxLength": max_len, **item},
    }


def _obj(props: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


SCHEMAS: dict[str, dict[str, Any]] = {
    "ticket.query": _obj(
        {
            "status": {"type": "array", "minItems": 1, "maxItems": 8, "items": _STATUS},
            "type": _strs(1, 16),
            "label": _strs(1, 16),
            "assignee": _PERSON,
            "owner": _PERSON,
            "parent": _REF,
            "epic": _REF,
            "q": {"type": "string", "minLength": 1, "maxLength": 200},
            "updated_since": _TIME,
            "needs_me": {"type": "boolean"},
            "sort": {"enum": ["updated", "priority", "key"]},
            "order": {"enum": ["asc", "desc"]},
            "limit": {**_INT, "minimum": 1, "maximum": 200},
            "page": _PAGE,
        }
    ),
    "ticket.get": _obj(
        {
            "ref": _REF,
            "log_since": {**_INT, "minimum": 0},
            "log_limit": {**_INT, "minimum": 1, "maximum": 500},
        },
        ("ref",),
    ),
    "needs": _obj({"person": _PERSON}),
    "activity.list": _obj(
        {
            "scope": {"enum": ["all", "tickets", "workspace"]},
            "ticket": _REF,
            "types": _strs(1, 16, pattern=r"^[a-z]+(\.[a-z_]+)*$"),
            "actor_kind": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"enum": ["person", "agent", "host", "addon"]},
            },
            "person": _PERSON,
            "since": _TIME,
            "until": _TIME,
            "order": {"enum": ["desc", "asc"]},
            "limit": {**_INT, "minimum": 1, "maximum": 200},
            "page": _PAGE,
        }
    ),
    "workspace.show": _obj({}),
    "member.list": _obj({}),
    "reader.list": _obj({}),  # H12d-2 (§0.8.6 item 9.11): columns P and R
    "phone.requests": _obj({}),  # B10 Part B: columns P and R (the viewer's own open hints)
    "phone.status": _obj({"id": {"type": "string", "pattern": r"^ph_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}}),  # column P
    "gate.input": _obj({"ref": _REF, "gate": {"enum": ["requirements", "plan", "verify", "code"]}}, ("ref", "gate")),
    "session.list": _obj({"active": {"type": "boolean"}}),
    "grant.list": _obj({"active": {"type": "boolean"}}),
    "workspace.settings": _obj({"limit": {**_INT, "minimum": 1, "maximum": 32}, "page": _PAGE}),
    "ticket.text": _obj(
        {
            "ref": _REF,
            "seq": {**_INT, "minimum": 1},
            "field": {"enum": ["text", "reason"]},
            "event_id": _ULID,
            "offset": {**_INT, "minimum": 0},
            "length": {**_INT, "minimum": 4, "maximum": 262144},
        },
        ("ref", "seq", "field"),
    ),
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in SCHEMAS.items()}

#: the reads only P and G may call (§0.5: N is outside the matrix, ``op.not_allowed``)
PERSON_GRANT_READS = frozenset({"session.list", "grant.list"})
#: op -> contract version (``welcome.ops``)
READ_OPS: dict[str, int] = dict.fromkeys(SCHEMAS, 1)


def validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


# --- the context ----------------------------------------------------------------------------------------------------


@dataclass
class ReadCtx:
    """One read request, on the workspace worker thread."""

    call: Call
    viewer: Viewer
    args: dict[str, Any]
    _cursor: str | None = field(default=None, repr=False)

    @property
    def service(self) -> Any:
        return self.call.service

    @property
    def store(self) -> Any:
        return self.call.service.store

    @property
    def principal(self) -> str:
        return self.call.principal

    @property
    def now(self) -> float:
        return float(self.call.service.clock())

    def as_of(self) -> str:
        return iso(self.now)

    def sub_cursor(self) -> str:
        """The ``sub_cursor`` of this read's snapshot: call it **right before** reading the state (H6a)."""
        if self._cursor is None:
            self._cursor = self.call.service.sub_cursor(self.principal)
        return self._cursor

    @property
    def epoch(self) -> str:
        journal = self.call.service.journal
        return journal.epoch if journal is not None else ""

    @property
    def pages(self) -> Pages:
        from .pages import Pages

        service = self.call.service
        pages = getattr(service, "pages", None)
        if pages is None:
            pages = Pages(service.workspace_id, clock=service.clock)
            service.pages = pages
        return pages

    @property
    def workspace(self) -> Any:
        """The replayed workspace (members, devices, grants, settings, policies) as of the store's last refresh."""
        return self.store.state.workspace

    def ticket(self, ref: str) -> Any:
        """The view ``ref`` names for this viewer, or ``not_found`` (§0.5)."""
        return self.viewer.ticket(self.store, ref)

    def visible_tickets(self) -> list[Any]:
        """Every ticket view this viewer may see now, loaded and verified (filter first)."""
        store = self.store
        store.load_all()
        return [v for v in store.state.tickets.values() if self.viewer.sees(v)]


Handler = Callable[[ReadCtx], dict[str, Any]]
HANDLERS: dict[str, Handler] = {}


def handler(op: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[op] = fn
        return fn

    return register


def _load() -> None:
    from . import (  # noqa: F401 - they register their handlers
        reads_feed,
        reads_phone,
        reads_query,
        reads_ticket,
        reads_ws,
    )


def serve(call: Call) -> dict[str, Any]:
    """A read op on the worker thread (after the column): its args (``invalid.input``), the workspace state, the op."""
    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    validate(op, args)
    request_idem(op, call.frame.get("opts"))  # dry_run is invalid.input on a read; idem is ignored
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    _load()
    fn = HANDLERS.get(op)
    if fn is None:
        raise HostError("unknown_command", f"{op} is not served")
    return ok_result(fn(ReadCtx(call, Viewer.of(call), args)))
