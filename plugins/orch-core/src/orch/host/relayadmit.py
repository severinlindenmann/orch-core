"""The ONE adapter seam between R2's ``relay.submit`` (``orch/host/relayentry.py``) and core's H12e admission helpers.

H12e (orch-52, #474, merged) provides ``orch.host.admission`` and ``orch.host.human.judge_events``: ``relayentry``
calls NOTHING else for the steps of 11A.14 that read core state, and reaches them only through this module. The real
helpers bring ``event.abandoned`` (a restore abandoned the id) and the visibility filter of ``check_target`` (one
``not_found`` for a missing and an invisible ticket), which R2's round-2 stand-ins lacked (review D2-5).

One stand-in remains: :func:`device_standing` (H12e-2, #482, not merged yet), with #482's exact signature and
behaviour. TODO(#482): replace it with ``from .admission import device_standing`` once #482 merges;
``tests/relay/test_relayadmit_seam.py`` then checks the real one against the same contract.

``tests/relay/test_relayadmit_seam.py`` pins the contract (the names, the signatures, the window edges, dedup by
signed digest and actor, the key rule with ``detail["next_key"]``, the device standing).
"""

from __future__ import annotations

from typing import Any, Literal

from .admission import COLUMN_R_AUTH, check_target, check_window, known_or_fresh, next_key, signed_digest
from .human import judge_events

__all__ = [
    "COLUMN_R_AUTH",
    "check_target",
    "check_window",
    "device_standing",
    "judge_events",
    "known_or_fresh",
    "next_key",
    "signed_digest",
]


def device_standing(store: Any, person_id: str, device_id: str) -> Literal["ok", "removed", "unknown"]:
    """STAND-IN for H12e-2's ``orch.host.admission.device_standing`` (#482; TODO: import it once merged). The
    liveness of a device in the F1 state (11A.4 step 3: "neither removed nor revoked"):

    * ``"ok"``: ``device_id`` was added for ``person_id`` and is neither removed nor revoked;
    * ``"removed"``: known for ``person_id`` and removed or revoked, or a revocation of it by ``person_id`` is on
      record (a tombstone);
    * ``"unknown"``: no such device of this person.

    Certificate expiry is ``verify_cert``'s. Takes the store lock itself (re-entrant)."""
    with store.locked():
        state = store.state
        view = state.workspace.devices.get(device_id)
        if view is not None:
            if view.person != person_id:
                return "unknown"
            return "removed" if view.removed or view.revoked else "ok"
        if (device_id, person_id) in state.revocation_evidence():
            return "removed"
        return "unknown"
