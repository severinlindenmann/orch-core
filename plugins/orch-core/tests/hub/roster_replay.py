"""D15b-1: core's own ``roster_v`` count for the contract's ``workspace_roster_v*`` vectors. Each vector line is built
as a REAL workspace event on the model's test world and replayed by the production engine; the answer is the engine's
``WorkspaceView.roster_v``, not a count made here."""

from __future__ import annotations

from typing import Any

from orch.model.codes import Code
from tests.model.test_hubledger import join
from tests.model.world import World, cert, hex32


def replay_roster_v(lines: list[dict[str, Any]]) -> int:
    w = World(validate=False)
    added: list[str] = []
    for i, line in enumerate(lines):
        typ = line["type"]
        if typ == "workspace.created":
            assert i == 0
            w.bootstrap()
        elif typ == "member.added":
            name = f"m{len(added)}"
            w.member(name, "member")
            added.append(name)
        elif typ == "role.changed":
            w.wev("role.changed", "sev", person=w.people[added[0]], role="maintainer")
        elif typ == "member.removed":
            w.wev("member.removed", "sev", person=w.people[added.pop()])
        elif typ == "device.added":
            dev = hex32(f"extra-device-{i}")
            w.wev("device.added", added[0], device="d_" + dev, cert=cert(w.people[added[0]][2:], dev))
        elif typ == "policy.changed":
            w.wev("policy.changed", "sev", gates={})
        elif typ == "hub.joined":
            _, r = join(w)
            assert not isinstance(getattr(r, "code", None), Code), r
        elif typ == "settings.changed":
            w.settings(grant_hours=4)
        else:
            raise ValueError(typ)
    st = w.state()
    assert not st.workspace.invalid, st.workspace.invalid  # every line was admitted: the count is of real events
    return int(st.workspace.roster_v)
