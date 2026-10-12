"""``diff`` on the workspace socket (§0.8, contract v1.4; the H3b1 follow-up): the branch changes of a ticket the
viewer may see, read by the host's git reader under the caller's principal.

``diff {ref, repo?, context? = 3, max_bytes? = 262144}`` → ``{sources, files, unified, truncated}``, exactly the
:func:`orch.ops.diff_core.diff_result` that direct mode computes (no field added or renamed):

* **Visibility first** (§0.5, F6). The ticket goes through :meth:`Viewer.ticket`, so a ticket the viewer may not see
  is ``not_found`` exactly as one that does not exist; then ``repo`` through :meth:`Viewer.linked_repo`, so a repo
  that is not in the ticket's ``links.repos`` (declared in ``settings.repos`` or not) is ``not_found`` as well. Nothing
  is read from git before both passed.
* **The reader** (H3b1, wired per workspace by H10a): the declared repos are pinned first (an existing pin is never
  replaced, :meth:`WorkspaceService.sync_git`), then every read is a reader child under the request's principal
  (``Capabilities.with_principal``: one child per principal, at most four per host, F10).
* **Errors** (§0.6, §0.8): ``observe.unavailable`` (no branch, no working copy, an unpinned repo, git denied, a reader
  failure), ``repo.remote_mismatch`` (F7), ``quota.host`` (the reader pool's queue bounds, §0.11; retryable).
* **Not here:** ``since:"my_approval"`` and the ``host_pinned`` base (§9 Q9 follow-ups), and v1.5 ``head``/``base``
  (``diff_ext/1``, H6c): each is an unknown key, so ``invalid.input`` (§0.3).
* Subjects, paths and hunks are repository content (untrusted display strings, cleaned or raw as §0.8 says); the
  dispatcher's redactor still runs over the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["SCHEMA", "VALIDATOR", "diff"]

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


def diff(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    """The worker-thread part of ``diff`` (args already passed :data:`SCHEMA`)."""
    from orch.ops.diff_core import diff_result
    from orch.ops.errors import OrchError

    service = call.service
    store = service.store
    view = Viewer.of(call).ticket(store, args["ref"])  # not_found for a ticket the viewer may not see (F6)
    Viewer.linked_repo(view, args.get("repo"))  # not_found for a repo the ticket does not link
    service.sync_git()  # pins a declared repo that first appears; never replaces a pin
    caps = service.capabilities().with_principal(call.principal)  # reader children under the caller (F10)
    try:
        return diff_result(
            caps,
            store,
            view,
            repo=args.get("repo"),
            context=args.get("context", 3),
            max_bytes=args.get("max_bytes", 262144),
        )
    except OrchError as e:
        raise HostError(e.code, e.message) from None
