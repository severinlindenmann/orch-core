"""The needs-you source for D13 (matching protocol ``ViewReader``): open questions addressed to the user, from the
real view; text escaped through ``render_text``; selective / no-role / unknown users see nothing."""

from __future__ import annotations

import pytest
from custodial_helpers import LOG, WK, WS
from test_view_service import make_seals
from view_helpers import OWNER, TK2, Host, build_view_services

from orch_hub.view import project as PR
from orch_hub.view.needs import ViewReader

USER = "cd" * 16
RLO, NEL = chr(0x202E), chr(0x85)


class Reg:
    def __init__(self, mode="full", alias=None):
        self._mode, self._alias = mode, alias

    def mode(self, ws):
        return self._mode

    def owner_alias(self, ws):
        return self._alias


class Acc:
    def __init__(self, role="edit"):
        self.role = role

    def role_of(self, user, ws):
        return self.role


@pytest.fixture
def kit(tmp_path):
    seals = make_seals(tmp_path, WS, {1: WK})
    (tmp_path / "v").mkdir(mode=0o700)
    reg, acc = Reg(alias=USER), Acc()
    vs = build_view_services(tmp_path / "v", wk_seals=seals, registry=reg, access=acc)
    h = Host()
    h.genesis()
    h.ticket()
    k = type("K", (), {})()
    k.vs, k.host, k.reg, k.acc, k.r = vs, h, reg, acc, vs.reader
    k.push = lambda: h.push_all(vs)
    yield k
    vs.close()
    seals.close()


def test_open_question_to_the_aliased_owner_is_listed(kit):
    h = kit.host.ask(text="CSV or API?")
    kit.push()
    rows = kit.r.needs_you(WS, USER)
    assert len(rows) == 1
    row = rows[0]
    assert row["item_id"] == PR.question_id(WS, LOG, "Q1") and row["kind"] == "question"
    assert row["key"] == "DEMO-0001" and row["title"] == "CSV or API?" and row["log"] == LOG and row["hash"] == h
    assert row["deadline"] is None and len(row["item_id"]) == 32


def test_question_to_someone_else_is_not_listed(kit):
    kit.host.ask(to="p_" + "11" * 16)
    kit.push()
    assert kit.r.needs_you(WS, USER) == []
    kit.reg._alias = None  # without the alias the user is only `u_...`, not the owner person
    assert kit.r.needs_you(WS, USER) == []


def test_question_to_the_hub_user_id_and_to_a_role(kit):
    kit.host.ask(to="u_" + USER, text="direct")
    kit.host.ask(to="ticket_owner", text="by role", qn="Q2")
    kit.host.ask(to="reviewers", text="not mine", qn="Q3")
    kit.push()
    assert sorted(r["title"] for r in kit.r.needs_you(WS, USER)) == ["by role", "direct"]


def test_answered_and_closed_tickets_drop_out(kit):
    h = kit.host.ask()
    kit.host.append(LOG, "question.answered", question="Q1", hash=h, text="csv", roster_v=1)
    kit.push()
    assert kit.r.needs_you(WS, USER) == []
    kit.host.ask(qn="Q2", text="again")
    kit.host.append(LOG, "ticket.closed", resolution="obsolete", roster_v=1)
    kit.push()
    assert kit.r.needs_you(WS, USER) == []


def test_reasked_question_is_open_again_and_stale_answer_does_not_count(kit):
    h = kit.host.ask(text="v1")
    kit.host.append(LOG, "question.answered", question="Q1", hash=h, text="x", roster_v=1)
    kit.host.ask(text="v2")  # re-asking Q1 with new text defines it again
    kit.push()
    assert [r["title"] for r in kit.r.needs_you(WS, USER)] == ["v2"]


def test_text_leaves_through_render_text(kit):
    kit.host.ask(text=f"Pay{RLO} now{NEL}?\nsecond line", options=[{"key": "a", "label": f"Y{RLO}es"}])
    kit.push()
    row = kit.r.needs_you(WS, USER)[0]
    assert row["title"] == "Pay<U+202E> now<U+0085>?"
    got = kit.r.item(WS, USER, row["item_id"])
    assert got["content"]["text"] == "Pay<U+202E> now<U+0085>?\nsecond line"
    assert got["content"]["options"] == [{"key": "a", "label": "Y<U+202E>es"}]


def test_item_detail_and_unknown_item(kit):
    kit.host.ask(options=[{"key": "csv", "label": "CSV"}])
    kit.push()
    rid = kit.r.needs_you(WS, USER)[0]["item_id"]
    got = kit.r.item(WS, USER, rid)
    assert (
        got["item_id"] == rid
        and got["content"]["blocking"] is True
        and got["content"]["ticket_title"] == "Load tariffs"
    )
    assert kit.r.item(WS, USER, "00" * 16) is None


def test_selective_no_role_and_empty_view_list_nothing(kit):
    kit.host.ask()
    kit.push()
    kit.reg._mode = "selective"
    assert kit.r.needs_you(WS, USER) == []
    kit.reg._mode = "full"
    kit.acc.role = None
    assert kit.r.needs_you(WS, USER) == []
    kit.acc.role = "read"
    assert len(kit.r.needs_you(WS, USER)) == 1  # read = see
    assert kit.r.needs_you("ee" * 16, USER) == []  # a workspace with no view


def test_restricted_ticket_is_invisible_to_a_user_not_on_the_list(kit):
    kit.host.ask()
    kit.host.append(LOG, "visibility.changed", visibility={"restricted": ["p_" + "22" * 16]}, roster_v=1)
    kit.push()
    # the question is addressed to OWNER, who is the ticket owner: still visible to them
    assert len(kit.r.needs_you(WS, USER)) == 1
    kit.host.ticket(TK2, "DEMO-0002", "Other")
    kit.host.append(TK2, "people.changed", role="owner", add=["p_" + "33" * 16], remove=[], roster_v=1)
    kit.host.ask(TK2, to="u_" + USER, text="hidden")
    kit.host.append(TK2, "visibility.changed", visibility={"restricted": ["p_" + "22" * 16]}, roster_v=1)
    kit.push()
    assert [r["title"] for r in kit.r.needs_you(WS, USER)] == ["CSV or API?"]


def test_a_corrupt_projection_reads_as_nothing(kit):
    kit.host.ask()
    kit.host.append(
        LOG,
        "question.asked",
        question={"id": "Q2", "to": OWNER, "text": "x", "blocking": True},
        qid="0" * 32,
        hash="sha256:" + "0" * 64,
        roster_v=1,
    )
    kit.push()
    assert kit.r.needs_you(WS, USER) == []  # the host's own qid/hash disagree with the derivation: fail closed


def test_question_hash_vectors_from_orch_core():
    # orch-core tests/vectors/f1/hashes.json (question_id / question_hash)
    assert (
        PR.question_id("0123456789abcdef0123456789abcdef", "01J9ZK4Q7M3R8T2V6X0B5N1C9D", "Q1")
        == "d60478ec5ff8f6ff213cded9c0f8f243"
    )
    qid = "d60478ec5ff8f6ff213cded9c0f8f243"
    t = "01J9ZK4Q7M3R8T2V6X0B5N1C9D"
    opts = [{"key": "csv", "label": "Monthly CSV"}, {"cost": "+1 day", "key": "api", "label": "Tariff API"}]
    assert (
        PR.question_hash(qid, t, "Which tariff export is the source of truth?", opts)
        == "sha256:5a46f6bb79a415be4493b18b1703c42d66b9be0e47e1ca5d99ac5b4158ee6ee6"
    )
    assert (
        PR.question_hash(qid, t, "Ok?", None)
        == "sha256:cc08eb5525f17f7f0d508fc4562e1175493fd8ce2030c903b8076144a36062db"
    )
    assert PR.value_hash(None) == "sha256:50fb42c5ddadf64924917d184566fcff987a5a2b6227cddc9d380e8baa8da223"
    assert (
        PR.value_hash(["a", 1, {"b": True}])
        == "sha256:2bf218b93953be276730b6336ea87cb1f0db41887ef9490e564de248bc644268"
    )


def test_reader_satisfies_d13_view_reader_shape(kit):
    """PR #29's ``webapi.api.ViewReader``: ``needs_you(ws, user)`` and ``item(ws, user, item_id)``."""
    r: ViewReader = kit.r
    assert r.needs_you(WS, USER) == [] and r.item(WS, USER, "00" * 16) is None
