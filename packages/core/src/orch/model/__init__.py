"""Pure derivation of state from events (core doc §2, ticket-format §5).

``model/`` has no filesystem, no clock and no crypto: events come in already parsed (schema-valid and chain-checked
by the store), ``now`` comes in as a parameter and signature checks are injected (:class:`Verifier`).

Modules: ``lifecycle`` (status table, people, restore), ``gates`` (gate hash input, decisions, done rule),
``generations`` (the §5.7 raise table), ``policies`` (effective policy), ``source`` (source list, D58), ``claims``,
``tasks`` (tasks, leases, artifacts, evidence), ``questions``, ``edits``, ``visibility``, ``needs``, ``authz``
(authorization replay), ``workspace`` (members, devices, grants, addons), ``engine`` (one code path for append and
replay), ``views``/``state`` (the frozen output and the public functions).
"""

from .codes import OK, Code, Ok, Refusal
from .needs import Need
from .state import ChainError, State, admit, advance, at, external_edit_voids, preview, replay
from .types import WORKSPACE
from .verifier import SigContext, Verifier
from .views import TicketView, WorkspaceView

__all__ = [
    "OK",
    "WORKSPACE",
    "ChainError",
    "Code",
    "Need",
    "Ok",
    "Refusal",
    "SigContext",
    "State",
    "TicketView",
    "Verifier",
    "WorkspaceView",
    "admit",
    "advance",
    "at",
    "external_edit_voids",
    "preview",
    "replay",
]
