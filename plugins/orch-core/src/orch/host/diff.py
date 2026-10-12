"""``diff`` on the workspace socket (§0.8, contract v1.4; the H3b1 follow-up): the branch changes of a ticket the
viewer may see, read by the host's git reader under the caller's principal.

``diff {ref, repo?, context? = 3, max_bytes? = 262144}`` → ``{sources, files, unified, truncated}``, exactly the
:func:`orch.ops.diff_core.diff_result` that direct mode computes (no field added or renamed). Two phases:

1. **On the workspace worker thread** (:func:`prepare`): the ticket through :meth:`Viewer.ticket` (a ticket the
   viewer may not see is ``not_found`` exactly as one that does not exist), ``repo`` through
   :meth:`Viewer.linked_repo` (a repo not in the ticket's ``links.repos`` is ``not_found``, declared or not), then the
   repos, branches and paths from the store (:func:`~orch.ops.diff_core.diff_plan`) and the request's capabilities
   (:meth:`WorkspaceService.caps_for`: pins are the service's own business, git reads run under the caller). Nothing
   is read from git here.
2. **Off the worker thread** (:func:`run`, on :data:`EXECUTOR`): the reader children, so a slow diff never holds the
   workspace's one worker (D-P2-1) and other principals' requests go on. At most one ``diff`` per principal is in
   flight host-wide (:class:`InFlight`, else ``quota.host``), and one request has :data:`BUDGET_S` seconds for all
   its repos together (each reader call gets what is left).

**Errors** (§0.6, §0.8): ``observe.unavailable``, ``repo.remote_mismatch`` (F7), ``quota.host`` (retryable). A
reader's reason can name a host path (``/Users/…/proj/.git does not exist``); §0.5 and §0.8.5 show a repo's path only
to a person-class owner or maintainer (:attr:`Viewer.manager`), so every other caller gets ``repo <name>: not
readable`` and the reason goes to the host log only.

**Not here:** ``since:"my_approval"`` and the ``host_pinned`` base (§9 Q9 follow-ups), and v1.5 ``head``/``base``
(``diff_ext/1``, H6c): each is an unknown key, so ``invalid.input`` (§0.3).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["BUDGET_S", "EXECUTOR", "IN_FLIGHT", "SCHEMA", "VALIDATOR", "Pending", "prepare", "run"]

LOG = logging.getLogger(__name__)
#: one request's time for every repo it diffs, reader queueing included (§0.11's 30 s wall, per request)
BUDGET_S = 30.0
#: where the reader phase waits for its children; the reader pool bounds the children themselves (F10)
EXECUTOR = ThreadPoolExecutor(16, thread_name_prefix="orch-diff")

SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "minLength": 1, "maxLength": 64},
        "repo": {"type": "string", "minLength": 1, "maxLength": 200},
        "context": {"type": "integer", "minimum": 0, "maximum": 10},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 1048576},
    },
    "required": ["ref"],
    "additionalProperties": False,
}
VALIDATOR = Draft202012Validator(SCHEMA)


class InFlight:
    """At most one ``diff`` in flight per principal, host-wide: a second one is ``quota.host`` at once."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._held: set[str] = set()

    def take(self, principal: str) -> None:
        with self._mu:
            if principal in self._held:
                raise HostError("quota.host", "a diff of this caller is still running; retry when it is done")
            self._held.add(principal)

    def give(self, principal: str) -> None:
        with self._mu:
            self._held.discard(principal)


IN_FLIGHT = InFlight()


@dataclass(frozen=True)
class Pending:
    """What the worker thread resolved: the reader phase needs neither the store nor the call."""

    principal: str
    caps: Any
    jobs: list[Any]
    context: int
    max_bytes: int
    #: may see repo paths in messages (person-class owner or maintainer, §0.8.5)
    manager: bool


def prepare(call: Call, args: dict[str, Any]) -> Pending:
    """Phase 1, on the worker thread (args already passed :data:`SCHEMA`)."""
    from orch.ops.diff_core import diff_plan
    from orch.ops.errors import OrchError

    service = call.service
    store = service.store
    viewer = Viewer.of(call)
    view = viewer.ticket(store, args["ref"])  # not_found for a ticket the viewer may not see (F6)
    Viewer.linked_repo(view, args.get("repo"))  # not_found for a repo the ticket does not link
    caps = service.caps_for(call.principal)  # pins kept by the service; reader children under the caller (F10)
    try:
        jobs = diff_plan(store, view, caps, repo=args.get("repo"))
    except OrchError as e:  # no branch, no working copy: path-free messages
        raise HostError(e.code, e.message) from None
    context, max_bytes = args.get("context", 3), args.get("max_bytes", 262144)
    return Pending(call.principal, caps, jobs, context, max_bytes, viewer.manager)


def run(pending: Pending) -> dict[str, Any]:
    """Phase 2, off the worker thread: the reader children, within :data:`BUDGET_S`."""
    from orch.ops.diff_core import RepoReadError, diff_run

    try:
        return diff_run(
            pending.caps, pending.jobs, context=pending.context, max_bytes=pending.max_bytes, budget=BUDGET_S
        )
    except RepoReadError as e:
        if e.code != "observe.unavailable" or pending.manager:
            raise HostError(e.code, e.message) from None
        LOG.info("diff: %s: %s (%s)", e.repo, e.reason, pending.principal)
        raise HostError(e.code, f"repo {e.repo}: not readable") from None
