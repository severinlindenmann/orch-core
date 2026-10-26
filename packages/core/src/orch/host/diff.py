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
   workspace's one worker (D-P2-1) and other principals' requests go on. At most one ``diff`` per principal and
   :data:`MAX_DIFFS` host-wide are in flight (:class:`InFlight`, else ``quota.host``), and one request has
   :data:`BUDGET_S` seconds for all its repos together (each reader call gets what is left).
3. **Back on the worker thread** (:func:`recheck`): the column and the ticket's visibility again, from the refreshed
   store, before the answer is sent, and also before a reader error is (``observe.unavailable``,
   ``repo.remote_mismatch``): a ticket restricted while its diff ran is ``not_found``, as one that does not exist.

**Errors** (§0.6, §0.8): ``observe.unavailable``, ``repo.remote_mismatch`` (F7), ``quota.host`` (retryable). A
reader's reason can name a host path (``/Users/…/proj/.git does not exist``); §0.5 and §0.8.5 show a repo's path only
to a person-class owner or maintainer (:attr:`Viewer.manager`), so every other caller gets ``repo <name>: not
readable`` and the reason goes to the host log only.

**H6c additions** (all optional keys, the result shape unchanged):

* ``since:"my_approval"``: the base is the newest still-counting ``code`` approval **of the caller's own person**
  (never another person's, never a verdict) whose signed ``source_sha`` names this repo identity and is an ancestor of
  the head (``base_source:"approved"``); none is ``not_found``.
* ``head`` / ``base`` (``diff_ext/1``): object ids, ``repo`` required, ``since`` excludes them (``invalid.input``);
  authorized by the reader from the ranges the host derives itself (§0.8.5), else ``not_found``.
* the ``host_pinned`` base: :class:`BaseStore` keeps the first merge base the host saw per (ticket uid, repo
  identity) in the state directory (0600) and never moves it forward.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.gitread import limits

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = [
    "BUDGET_S",
    "EXECUTOR",
    "IN_FLIGHT",
    "MAX_DIFFS",
    "SCHEMA",
    "BaseStore",
    "VALIDATOR",
    "Pending",
    "prepare",
    "recheck",
    "run",
]

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
        "since": {"const": "my_approval"},
        "head": {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"},
        "base": {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"},
    },
    "required": ["ref"],
    "additionalProperties": False,
}
VALIDATOR = Draft202012Validator(SCHEMA)


#: diffs in flight host-wide: two of the reader pool's children stay free for ``show``/observe and the approval
#: tree checks, whatever the diffs do
MAX_DIFFS = limits.MAX_CHILDREN - 2


class InFlight:
    """At most one ``diff`` in flight per principal and :data:`MAX_DIFFS` host-wide; past either, ``quota.host`` at
    once (retryable)."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._held: set[str] = set()

    def take(self, principal: str) -> None:
        with self._mu:
            if principal in self._held:
                raise HostError("quota.host", "a diff of this caller is still running; retry when it is done")
            if len(self._held) >= MAX_DIFFS:
                raise HostError("quota.host", "the host is running as many diffs as it allows; retry shortly")
            self._held.add(principal)

    def give(self, principal: str) -> None:
        with self._mu:
            self._held.discard(principal)


IN_FLIGHT = InFlight()

#: first-seen merge bases kept per workspace: at most this many (ticket, repo identity) entries; past it a new one is
#: simply not recorded (the diff then reports ``local_ref`` as before)
BASES_MAX = 20_000


class BaseStore:
    """The ``host_pinned`` bases (§0.8, §9 Q9): ``{ticket uid: {repo identity: sha}}`` in a 0600 file of the host's
    state directory. The first merge base seen for a (ticket, identity) is kept and never replaced; an unreadable
    file reads as no pins (the diff then shows the local base, labelled so) and is not overwritten.

    One instance per file path (:meth:`at`), so every request of a workspace shares one lock: two recorders of different
    tickets never overwrite each other's entry with a stale copy."""

    _registry: dict[str, BaseStore] = {}
    _registry_mu = threading.Lock()

    @classmethod
    def at(cls, path: Path) -> BaseStore:
        key = os.path.realpath(path)
        with cls._registry_mu:
            got = cls._registry.get(key)
            if got is None:
                got = cls._registry[key] = cls(Path(key))
            return got

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._mu = threading.Lock()

    def _read(self) -> dict[str, dict[str, str]] | None:
        try:
            doc = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        return {
            u: {r: v for r, v in m.items() if isinstance(r, str) and isinstance(v, str)}
            for u, m in doc.items()
            if isinstance(u, str) and isinstance(m, dict)
        }

    def get(self, uid: str) -> dict[str, str]:
        with self._mu:
            return dict((self._read() or {}).get(uid, {}))

    @staticmethod
    def _same(a: str, b: str) -> bool:
        from orch import canon

        try:
            return canon.same_repo_identity(a, b)
        except Exception:  # noqa: BLE001 - an identity that does not parse names no repo
            return False

    def record(self, uid: str, identity: str, sha: str) -> None:
        """Keep ``sha`` as the base of (``uid``, ``identity``) unless one is kept already."""
        with self._mu:
            doc = self._read()
            if doc is None or sum(map(len, doc.values())) >= BASES_MAX:
                return
            mine = doc.setdefault(uid, {})
            if any(self._same(r, identity) for r in mine):
                return
            mine[identity] = sha
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(prefix=".diff-bases.", suffix=".tmp", dir=self.path.parent)  # 0600, O_EXCL
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(doc, sort_keys=True))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                tmp = None
            except OSError:
                LOG.warning("diff: the host-pinned bases could not be written")
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass


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
    since: bool = False
    head: str | None = None
    base: str | None = None
    #: records the first merge base seen for (ticket, repo identity); host mode only
    bases: BaseStore | None = None
    uid: str = ""


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
    since, head, base = args.get("since") == "my_approval", args.get("head"), args.get("base")
    if since and (head is not None or base is not None):
        raise HostError("invalid.input", "diff: since cannot be combined with head or base")
    if (head is not None or base is not None) and args.get("repo") is None:
        raise HostError("invalid.input", "diff: head and base need repo")
    bases = BaseStore.at(service.state_dir / "hosts" / service.workspace_id / "diff-bases.json")
    try:
        jobs = diff_plan(
            store,
            view,
            caps,
            repo=args.get("repo"),
            person=viewer.person,
            since=since,
            extended=head is not None or base is not None,
            pins=bases.get(view.uid),
        )
    except OrchError as e:  # no branch, no working copy: path-free messages
        raise HostError(e.code, e.message) from None
    context, max_bytes = args.get("context", 3), args.get("max_bytes", 262144)
    return Pending(
        call.principal,
        caps,
        jobs,
        context,
        max_bytes,
        viewer.manager,
        since,
        head,
        base,
        bases if call.column.name == "P" else None,  # only a person's call may pin a base
        view.uid,
    )


def run(pending: Pending) -> dict[str, Any]:
    """Phase 2, off the worker thread: the reader children, within :data:`BUDGET_S`."""
    from orch.ops.diff_core import RepoReadError, diff_run

    try:
        bases = pending.bases
        return diff_run(
            pending.caps,
            pending.jobs,
            context=pending.context,
            max_bytes=pending.max_bytes,
            budget=BUDGET_S,
            since=pending.since,
            head=pending.head,
            base=pending.base,
            pin_base=None if bases is None else lambda job, ident, sha: bases.record(pending.uid, ident, sha),
        )
    except RepoReadError as e:
        if e.code != "observe.unavailable" or pending.manager:
            raise HostError(e.code, e.message) from None
        LOG.info("diff: %s: %s (%s)", e.repo, e.reason, pending.principal)
        raise HostError(e.code, f"repo {e.repo}: not readable") from None


def recheck(call: Call, pending: Pending | None = None) -> None:
    """Phase 3, on the worker thread: the caller may still see the ticket (F8: the column and the viewer from the
    refreshed store), else ``not_found`` exactly as in :func:`prepare`. For ``since`` the approvals phase 1 resolved
    are resolved again from the refreshed store and must be the same list: an approval revoked, voided or replaced
    while the reader ran is ``not_found`` (the "no approval" answer), never the diff. Any error here refuses."""
    call.authorize()
    viewer = Viewer.of(call)
    view = viewer.ticket(call.service.store, call.frame["args"]["ref"])
    if pending is not None and pending.since:
        from orch.ops.diff_core import approvals_of

        now = list(approvals_of(call.service.store, view, viewer.person))
        for job in pending.jobs:
            before = list(job.approvals)
            if before == now:
                continue
            if all(a in now for a in before):  # one was added while the diff ran
                raise HostError("not_found", "your approvals changed while the diff ran; ask again")
            raise HostError("not_found", "no approval of yours covers this repo's history")
