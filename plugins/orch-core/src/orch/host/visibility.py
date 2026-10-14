"""The F6 visibility hooks of the host's reads (§0.5): :class:`Viewer`.

Every row, count, page token, ``needs`` item, session claim, grant, diff, artifact and subscription item is filtered
for the viewer **before** it is counted or returned, and a ref the viewer may not see is ``not_found`` exactly as one
that does not exist. The registry reads already filter through ``Call.sees`` (the same :func:`visible_to` test), with
the viewer the dispatcher passes in. The hooks below serve the reads that later groups add; each must call them:

| Hook | Op | Wired by |
|---|---|---|
| :meth:`Viewer.visible` (filter, then count ``total``/``next_page``) | ``ticket.query``, ``activity.list`` | H6b |
| :meth:`Viewer.needs_person` | ``needs {person}`` | H6b |
| :meth:`Viewer.session_visible`, :meth:`Viewer.claims` | ``session.list``, ``grant.list`` | H6b |
| :meth:`Viewer.ticket` | ``ticket.get``, ``diff``, ``artifact.get``, ``sub.open {ticket}`` | H6b, H3b1, H4a2, H6a |
| :meth:`Viewer.linked_repo` | ``diff {repo}`` | H3b1 (served by :mod:`orch.host.diff`) |
| :meth:`Viewer.artifact` | ``artifact.get {name}`` | H4a2 |
| :meth:`Viewer.ticket`, :meth:`Viewer.manager` | ``ticket.text``, ``workspace.settings``, ``activity.list`` | H6b |

The deny-by-default rule: ``no-person`` sees ``workspace`` tickets only, is never an owner, reads no session and no
other person's needs. ``tests/host/test_dispatch_reads.py`` walks every served read as no-person against a restricted
ticket, so an op that skips these hooks fails CI.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orch.ops.runtime import visible_to

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["Viewer"]

_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # the F1 §6 name rule


@dataclass(frozen=True)
class Viewer:
    """The viewer of one read (§0.5): P the mapped person, G the grant's person, N ``no-person`` (``person`` None),
    which sees ``workspace`` tickets only. R (a relay device, §11A.4) reads exactly as P: its device's person.
    Every row, count, item and subscription item is filtered with
    :meth:`sees` **before** it is counted or returned, and a ref the viewer may not see is ``not_found`` exactly as a
    ref that does not exist."""

    column: str
    person: str | None
    #: a person-class owner (column P): may name another person in ``needs`` and sees every session and grant
    owner: bool = False
    #: the viewer's member role now (``None`` for no-person): ``workspace.settings`` serves ``path`` and ``remote``
    #: only to a person-class owner or maintainer (:attr:`manager`)
    role: str | None = None

    @classmethod
    def of(cls, call: Call) -> Viewer:
        auth = call.service.auth
        owner = call.column.name in {"P", "R"} and auth is not None and auth.owner(call.viewer)
        role = auth.roles.get(call.viewer) if auth is not None and call.viewer is not None else None
        return cls(call.column.name, call.viewer, owner, role)

    @property
    def manager(self) -> bool:
        """A person-class owner or maintainer (column P with that role; a G caller acting for one is not)."""
        return self.column in {"P", "R"} and self.role in ("owner", "maintainer")

    def sees(self, view: Any) -> bool:
        return visible_to(self.person, view)  # the same test as the registry reads' Call.sees

    def visible(self, views: Iterable[Any]) -> list[Any]:
        """The views this viewer may see, in order: filter first, count after (``total``, ``next_page``)."""
        return [v for v in views if self.sees(v)]

    def ticket(self, store: Any, ref: str) -> Any:
        """The ticket ``ref`` names, or ``not_found`` (also for a ticket the viewer may not see: N5, F6)."""

        head = ref.partition("/")[0] if isinstance(ref, str) else ""
        try:
            view = store.ticket(store.normalise_ref(head)) if head else None
        except Exception:  # noqa: BLE001 - a malformed ref is a ref that does not exist
            view = None
        if view is None or not self.sees(view):
            raise HostError("not_found", f"no ticket {' '.join(str(ref).split())[:60]}")
        return view

    def needs_person(self, requested: str | None) -> str | None:
        """``needs {person}``: G always its grant's person; P its own person, or another one only as an owner; N only
        ``workspace`` items (``None``). Naming another person otherwise is ``op.not_allowed``."""
        if self.column == "G":
            if requested is not None and requested != self.person:
                raise HostError("op.not_allowed", "a grant reads only its own person's needs")
            return self.person
        if self.column in {"P", "R"}:
            if requested is None or requested == self.person:
                return self.person
            if self.owner:
                return requested
            raise HostError("op.not_allowed", "only an owner reads another person's needs")
        if requested is not None:
            raise HostError("op.not_allowed", "no-person reads workspace items only")
        return None

    def session_visible(self, entry_person: str | None) -> bool:
        """``session.list`` and ``grant.list``: an owner (P) sees every entry, any other P its own, G its own
        person's; N is outside the matrix (``op.not_allowed``, the gate's)."""
        if self.column in {"P", "R"} and self.owner:
            return True
        return self.column in {"P", "G", "R"} and entry_person is not None and entry_person == self.person

    def claims(self, store: Any, keys: Iterable[str]) -> list[str]:
        """A session's claims with every ticket the viewer may not see left out."""
        out = []
        for key in keys:
            view = store.ticket(key)
            if view is not None and self.sees(view):
                out.append(key)
        return out

    @staticmethod
    def linked_repo(view: Any, repo: str | None) -> None:
        """``diff {repo}``: only a repo named in the ticket's ``links.repos``; any other is ``not_found``."""
        if repo is None:
            return
        repos = ((view.fields.get("links") or {}).get("repos")) or []
        if repo not in repos:
            raise HostError("not_found", "no such repo on this ticket")

    @staticmethod
    def artifact(view: Any, name: str) -> Any:
        """``artifact.get {name}``: the F1 §6 name rule, then the ticket's replayed manifest (read by digest after);
        a client string is never joined to a path, so ``../x`` is simply a name that does not exist."""
        if not isinstance(name, str) or not _ARTIFACT_NAME.fullmatch(name):
            raise HostError("not_found", "no such artifact on this ticket")
        for artifact in view.artifacts:
            if artifact.name == name:
                return artifact
        raise HostError("not_found", "no such artifact on this ticket")
