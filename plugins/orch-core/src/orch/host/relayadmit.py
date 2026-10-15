"""The ONE adapter seam between R2's ``relay.submit`` (``orch/host/relayentry.py``) and core's H12e admission helpers.

H12e (orch-52, #474, merged) provides ``orch.host.admission`` and ``orch.host.human.judge_events``: ``relayentry``
calls NOTHING else for the steps of 11A.14 that read core state, and reaches them only through this module. The real
helpers bring ``event.abandoned`` (a restore abandoned the id) and the visibility filter of ``check_target`` (one
``not_found`` for a missing and an invisible ticket), which R2's round-2 stand-ins lacked (review D2-5).

H12e-2 (#482, merged) adds :func:`device_standing`; it is imported here too (no stand-in remains), and
``tests/relay/test_relayadmit_seam.py`` checks the real one against the contract R2 relies on.

``tests/relay/test_relayadmit_seam.py`` pins the contract (the names, the signatures, the window edges, dedup by
signed digest and actor, the key rule with ``detail["next_key"]``, the device standing).
"""

from __future__ import annotations

from .admission import (
    COLUMN_R_AUTH,
    check_target,
    check_window,
    device_standing,
    known_or_fresh,
    next_key,
    signed_digest,
)
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
