"""D15b (orch-core#1043, hub contract 10.7 sender rule 7): the ticket projection ``P`` rides on every ticket page that
ends at the head, and a changed ``P`` with no new line is sent again as a one-line overlap page. Against the contract's
sealed bytes and the in-process fake hub (test-side port of the hub's step 7)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import unb64u
from orch.hubclient import HubRefused, Position, pending
from orch.hubclient import logsync as LS

from . import logview
from .test_full_join import mk  # noqa: F401 - fixture
from .test_log_sync import TKT, WS, Logs, Sender, full, hub_view  # noqa: F401 - full is a fixture
from .vec import V

P4 = V["phase4"]["log_sync"]
PROJ = P4["proj_ticket"]
H1, H2 = "sha256:" + "a1" * 32, "sha256:" + "b1" * 32


class ProjLogs(Logs):
    """``Logs`` plus the optional ``proj(log)`` of a source: a projection of the current head, values the test sets."""

    def __init__(self) -> None:
        super().__init__()
        self.status = "testing"
        self.gate_gen = 1
        self.broken = False

    def proj(self, log: str) -> dict[str, Any] | None:
        if log == "workspace" or not self.lines.get(log):
            return None
        rows, ws = self.lines[log], self.lines["workspace"]
        p = {
            "seq": len(rows), "head": logview.head_of(rows[-1]), "ws_seq": len(ws), "ws_head": logview.head_of(ws[-1]),
            "status": self.status, "roster_v": 3,
            "gates": {"requirements": {"gen": self.gate_gen, "hash": H1, "policy_hash": H2}},
        }  # fmt: skip
        if self.broken:
            p["extra"] = 1
        return p


def held_proj(host) -> dict[str, Any]:  # noqa: ANN001
    return host.hub.workspaces[WS].view.proj


# --- the contract's bytes ------------------------------------------------------------------------------------------


def test_the_vector_page_with_a_projection_is_reproduced_byte_for_byte():
    k = bytes.fromhex(P4["view_key_e1_hex"])
    page = P4["page_ticket_with_proj"]
    salt = unb64u(page["sealed"])[:16]
    LS.check_proj(PROJ)
    built = LS.build_page(
        k, P4["workspace_id"], 1, TKT, P4["ticket_lines"], proj=PROJ,
        seal=lambda kb, label, aad, pt: S._salted_seal(kb, label, aad, pt, salt),
    )  # fmt: skip
    assert built == page
    aad = LS.page_aad(P4["workspace_id"], 1, TKT, 1, 2, page["head"], False)
    plain = S.salted_open(k, b"orch/v2/hub-view-msg", aad,
                          unb64u(page["sealed"]))  # fmt: skip
    assert plain.hex() == P4["plaintext_hex_ticket_with_proj"]


@pytest.mark.parametrize(
    "mut",
    [
        lambda p: p.pop("status"),
        lambda p: p.update(extra=1),
        lambda p: p.update(status="weird"),
        lambda p: p["gates"].update(
            unknown={"gen": 1, "hash": "sha256:" + "a" * 64, "policy_hash": "sha256:" + "b" * 64}
        ),
        lambda p: p["gates"]["requirements"].update(source_sha=[]),  # source only on code / verify
        lambda p: p["gates"]["code"].pop("source_sha"),
        lambda p: p.update(roster_v=-1),
        lambda p: p.update(ws_seq=0),
    ],
)
def test_check_proj_refuses_what_the_hub_would(mut):
    p = copy.deepcopy(PROJ)
    mut(p)
    with pytest.raises(HubRefused) as e:
        LS.check_proj(p)
    assert e.value.code == "malformed"


def test_a_projection_must_describe_the_last_line_of_a_ticket_page():
    k = bytes.fromhex(P4["view_key_e1_hex"])
    with pytest.raises(HubRefused):
        LS.build_page(k, P4["workspace_id"], 1, TKT, P4["ticket_lines"][:1], proj=PROJ)  # P.seq is 2
    with pytest.raises(HubRefused):
        LS.build_page(k, P4["workspace_id"], 1, "workspace", P4["workspace_lines"], proj=PROJ)


def test_the_digest_ignores_the_workspace_position_only():
    a = LS.proj_digest(PROJ)
    assert a == LS.proj_digest({**PROJ, "ws_seq": 99, "ws_head": "sha256:" + "0" * 64})
    assert a != LS.proj_digest({**PROJ, "status": "done"}) and a != LS.proj_digest({**PROJ, "roster_v": 4})


# --- task 5: attached at the head ----------------------------------------------------------------------------------


def test_a_ticket_page_that_ends_at_the_head_carries_the_projection_and_the_workspace_page_does_not(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created")
    logs.add(TKT)
    s = Sender(full, logs)
    r = s.run()
    assert r.proj_pages == 1 and r.pages == 2
    assert held_proj(full) == {TKT: logs.proj(TKT)}
    assert s.pos.proj[TKT] == LS.proj_digest(logs.proj(TKT))


def test_a_page_that_does_not_end_at_the_head_goes_without_one(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    for _ in range(300):
        logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    p = s.pages()
    assert [(x["log"], x["from_seq"], x["count"]) for x in p] == [("workspace", 1, 1), (TKT, 1, 256), (TKT, 257, 44)]
    assert held_proj(full)[TKT]["seq"] == 300  # only the last page ended at the head


def test_a_projection_that_is_not_well_formed_is_not_sent_but_the_lines_are(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    logs.broken = True
    s = Sender(full, logs)
    s.run()
    assert held_proj(full) == {} and hub_view(full)[TKT]["seq"] == 1


def test_a_projection_from_a_workspace_position_the_hub_lacks_is_held_back_then_refreshed(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)

    # the ticket line names ws_seq 1; the projection is read AFTER another workspace line was committed (ws_seq 2)
    orig = logs.proj

    def late(log: str):
        p = orig(log)
        if p is not None:
            logs.add("workspace", "settings.changed")
            q = orig(log)
            return q
        return p

    logs.proj = late  # type: ignore[method-assign]
    s.run()
    assert held_proj(full) == {}  # ws_seq 2 is not held by the hub: no projection on that page (it would be view_gap)
    logs.proj = orig  # type: ignore[method-assign]
    assert pending(logs, s.pos)
    s.run()
    assert held_proj(full)[TKT]["ws_seq"] == len(logs.lines["workspace"])


# --- task 6: sender rule 7 -----------------------------------------------------------------------------------------


def test_a_changed_projection_without_a_new_line_sends_the_last_line_again_as_a_one_line_page(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    n = len(s.pages())
    assert not pending(logs, s.pos) and s.run().pages == 0
    logs.gate_gen = 2  # a gate decision elsewhere: the ticket log did not move
    logs.add("workspace", "member.added")  # a workspace change
    assert pending(logs, s.pos)
    r = s.run()
    page = s.pages()[n:]
    assert [(x["log"], x["from_seq"], x["count"]) for x in page] == [("workspace", 2, 1), (TKT, 2, 1)]
    assert r.proj_pages == 1
    got = held_proj(full)[TKT]
    assert got["gates"]["requirements"]["gen"] == 2 and got["ws_seq"] == 2
    assert hub_view(full)[TKT] == {"seq": 2, "head": logview.head_of(logs.lines[TKT][-1])}
    assert not pending(logs, s.pos) and s.run().pages == 0  # settled


def test_a_moved_workspace_position_alone_is_not_a_change(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    logs.add("workspace", "settings.changed")  # P's ws_seq moves, its values do not
    r = s.run()
    assert r.proj_pages == 0 and [x["log"] for x in s.pages()][-1] == "workspace"


def test_the_debounce_holds_a_refresh_back_and_says_so(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    logs.status = "done"
    asked: list[tuple[str, str]] = []

    def due(log: str, digest: str) -> bool:
        asked.append((log, digest))
        return False

    r = s.run(proj_due=due)
    assert r.proj_waiting and r.proj_pages == 0 and asked == [(TKT, LS.proj_digest(logs.proj(TKT)))]
    assert held_proj(full)[TKT]["status"] == "testing"
    r = s.run(proj_due=lambda *_: True)
    assert not r.proj_waiting and r.proj_pages == 1 and held_proj(full)[TKT]["status"] == "done"


def test_a_refresh_obeys_the_live_check_like_every_page(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    logs.status = "done"

    def live() -> bool:
        return False

    with pytest.raises(HubRefused) as e:
        s.run(live=live)
    assert e.value.code == "not_joined" and held_proj(full)[TKT]["status"] == "testing"


def test_a_line_committed_after_the_pass_began_is_not_refreshed_into_the_page(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    logs.status = "done"
    orig = logs.proj

    def late(log: str):
        logs.add(TKT)  # committed after the ceiling: P now describes seq 2, the pass may only touch seq 1
        return orig(log)

    logs.proj = late  # type: ignore[method-assign]
    r = s.run()
    assert r.proj_pages == 0 and held_proj(full)[TKT]["seq"] == 1
    logs.proj = orig  # type: ignore[method-assign]
    s.run()
    assert held_proj(full)[TKT]["seq"] == 2


def test_a_hub_that_lost_its_view_makes_the_refresh_forget_the_position_and_the_next_pass_resends(full):  # noqa: F811
    logs = ProjLogs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    full.hub.workspaces[WS].view = logview.View()  # the hub lost everything
    logs.status = "done"
    s.run()  # the overlap is a view_gap: nothing raised, positions dropped
    assert TKT not in s.pos.logs and TKT not in s.pos.proj
    s.run(probe=True)  # a link open starts with the probe, which repairs the lost view
    assert held_proj(full)[TKT]["status"] == "done" and hub_view(full)[TKT]["seq"] == 1


def test_the_position_keeps_the_projection_digest_across_a_restart_and_drops_it_with_the_log():
    d = "sha256:" + "c" * 64
    pos = Position("h", 1, {"workspace": (1, d), TKT: (1, d)}, proj={TKT: d})
    j = pos.to_json()
    assert j["proj"] == {TKT: d}
    assert Position.from_json(j, hub_id="h", binding_seq=1).proj == {TKT: d}
    assert Position.from_json({**j, "proj": {TKT: "bogus"}}, hub_id="h", binding_seq=1).proj == {}
    j.pop("proj")  # an older file
    assert Position.from_json(j, hub_id="h", binding_seq=1).proj == {}


def test_a_source_without_proj_sends_no_projection_and_owes_none(full):  # noqa: F811
    logs = Logs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    r = s.run()
    assert r.proj_pages == 0 and not pending(logs, s.pos)
