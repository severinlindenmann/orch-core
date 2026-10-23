"""The needs-you source for D13 (matching protocol ``ViewReader``): open questions addressed to the user, from the
real view; text escaped through ``render_text``; selective / no-role / unknown users see nothing."""

from __future__ import annotations

import pytest
from custodial_helpers import LOG, WK, WS
from test_view_service import make_seals
from view_helpers import OWNER, TK2, WSK_PUB, Host, build_view_services

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
    short = kit.r.item(WS, USER, row["item_id"])["content"]["short_hash"]  # orch-hub#55: the question's own hash
    assert short == " ".join(h[7 + i : 11 + i] for i in (0, 4, 8))


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
    # core's can_see: only the persons on the list; being the ticket owner (OWNER, aliased to USER) does not count
    assert kit.r.needs_you(WS, USER) == []
    kit.host.append(LOG, "visibility.changed", visibility={"restricted": ["p_" + "22" * 16, OWNER]}, roster_v=1)
    kit.push()
    assert [r["title"] for r in kit.r.needs_you(WS, USER)] == ["CSV or API?"]
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


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def test_recommended_only_if_it_is_an_option_key(kit):
    opts = [{"key": "csv", "label": "CSV"}]
    for rec, want in (("csv", "csv"), ("api", None), (5, None), ({"x": 1}, None)):
        q = {"id": "Q1", "to": OWNER, "text": "t", "blocking": True, "options": opts, "recommended": rec}
        qid = PR.question_id(WS, LOG, "Q1")
        h = PR.question_hash(qid, LOG, "t", opts)
        kit.host.append(LOG, "question.asked", question=q, qid=qid, hash=h, roster_v=1)
        kit.push()
        assert kit.r.item(WS, USER, qid)["content"]["recommended"] == want


def test_every_string_that_leaves_the_view_is_render_text_safe(kit):
    bad = f"a{RLO}b{NEL}c"
    kit.host.append(LOG, "ticket.updated", base_rev={}, set={"ticket.title": bad}, roster_v=1)
    opts = [{"key": "k", "label": bad, "cost": bad}]
    q = {"id": "Q1", "to": OWNER, "text": bad, "why": bad, "blocking": True, "options": opts, "recommended": "k"}
    qid = PR.question_id(WS, LOG, "Q1")
    kit.host.append(LOG, "question.asked", question=q, qid=qid, hash=PR.question_hash(qid, LOG, bad, opts), roster_v=1)
    kit.push()
    from orch_hub.contract import phase4 as P4

    rows = kit.r.needs_you(WS, USER)
    out = rows + [kit.r.item(WS, USER, rows[0]["item_id"])]
    seen = list(_strings(out))
    assert seen and all(s == P4.render_text(s) for s in seen)
    assert any("<U+202E>" in s for s in seen)


# --- D15b-3: gate and verdict rows from P --------------------------------------------------------------------------------

SRC = [{"repo": "github.com/acme/app", "ref": "refs/heads/x", "sha": "c0" * 20}]
H_PLAN, H_VER = "sha256:" + "a2" * 32, "sha256:" + "a4" * 32


def push_proj(kit, status="open", log=LOG):
    kit.push()
    gates = {"plan": kit.host.gate(2, H_PLAN), "verify": kit.host.gate(4, H_VER, SRC)}
    n = len(kit.host.logs[log])
    kit.vs.service.accept(WS, WSK_PUB, kit.host.page(log, n, 1, proj=kit.host.proj(log, status, gates)))


def test_gate_row_from_P_keeps_the_hash_it_was_listed_with(kit):
    push_proj(kit, "open")
    rows = kit.r.needs_you(WS, USER)
    assert [(r["kind"], r["hash"]) for r in rows] == [("gate", H_PLAN)]  # verify only while testing
    got = kit.r.item(WS, USER, rows[0]["item_id"])
    assert got["content"]["gate"] == "plan" and got["content"]["gate_gen"] == 2
    assert got["content"]["short_hash"] == "a2a2 a2a2 a2a2" and rows[0]["key"] == "DEMO-0001"


def test_verdict_row_only_while_testing_with_sources(kit):
    push_proj(kit, "testing")
    rows = {r["kind"]: r for r in kit.r.needs_you(WS, USER)}
    assert set(rows) == {"gate", "verdict"} and rows["verdict"]["hash"] == H_VER
    c = kit.r.item(WS, USER, rows["verdict"]["item_id"])["content"]
    assert c["gate"] == "verify" and c["sources"] == [{"repo": "github.com/acme/app", "sha": "c0c0c0c0"}]


def test_a_new_hash_is_a_new_row(kit):
    push_proj(kit)
    old = kit.r.needs_you(WS, USER)[0]
    kit.vs.service.accept(
        WS,
        WSK_PUB,
        kit.host.page(LOG, 1, 1, proj=kit.host.proj(LOG, "open", {"plan": kit.host.gate(3, "sha256:" + "ee" * 32)})),
    )
    new = kit.r.needs_you(WS, USER)[0]
    assert new["hash"] != old["hash"] and new["item_id"] != old["item_id"]


def test_no_rows_without_P_with_stale_P_or_a_stale_roster(kit):
    kit.push()
    assert kit.r.needs_you(WS, USER) == []
    push_proj(kit)
    assert len(kit.r.needs_you(WS, USER)) == 1
    kit.host.append(LOG, "log.added", text="moved", roster_v=1)
    kit.push()
    assert kit.r.needs_you(WS, USER) == []  # P describes seq 1, the view is at seq 2
    push_proj(kit)
    kit.host.append("workspace", "member.added", roster_v=1)
    kit.push()
    assert kit.r.needs_you(WS, USER) == []  # the hub counts another roster_v than P


def test_gate_rows_only_for_visible_tickets(kit):
    kit.host.append(LOG, "visibility.changed", visibility={"restricted": ["p_" + "22" * 16]}, roster_v=1)
    push_proj(kit)
    assert kit.r.needs_you(WS, USER) == []
    kit.host.append(LOG, "visibility.changed", visibility={"restricted": ["u_" + USER]}, roster_v=1)
    push_proj(kit)
    assert len(kit.r.needs_you(WS, USER)) == 1


def test_a_gate_this_person_already_decided_is_not_listed(kit):
    kit.host.append(
        LOG, "gate.approved", gate="plan", hash=H_PLAN, actor={"kind": "person", "id": "u_" + USER}, roster_v=1
    )
    push_proj(kit)
    assert kit.r.needs_you(WS, USER) == []


def test_someone_elses_decision_does_not_hide_the_gate(kit):
    kit.host.append(
        LOG, "gate.approved", gate="plan", hash=H_PLAN, actor={"kind": "person", "id": "u_" + "ee" * 16}, roster_v=1
    )
    push_proj(kit)
    assert [r["kind"] for r in kit.r.needs_you(WS, USER)] == ["gate"]
