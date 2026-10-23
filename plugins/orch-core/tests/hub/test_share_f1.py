"""V3 C6b: the ``f1`` block of a ``share.item`` (hub contract 10.2b, 8.6): the production builder against the hub's
vectors byte for byte, the refusals, the facts-to-block mapping and the re-share rule (a changed value = a new
``item_version``)."""

from __future__ import annotations

import copy

import pytest

from orch.hubclient import sharefmt as F
from orch.hubclient import shares as SH
from orch.hubclient.errors import HubRefused

from . import share_open as SO
from .test_share_format import _sign_det
from .vec import V

P4 = V["phase4"]["share_item_f1"]
ITEM = P4["item"]
O = ITEM["o"]  # noqa: E741
H = "sha256:" + "ab" * 32
UID = "01J9ZP0000000000000000TKT1"
SRC = [{"repo": "https://github.com/demo/api", "ref": "refs/heads/t-1", "sha": "1" * 40}]


def _build(f1, **kw):
    args = dict(
        hub_id=O["hub_id"], workspace_id=O["workspace_id"], item_id=O["item_id"], item_kind=O["item_kind"],
        item_version=O["item_version"], issued_ms=O["issued_ms"], chain=O["chain"], content=O["content"], dev=True,
        sign=_sign_det, f1=f1,
    )  # fmt: skip
    return F.build_signed_item(**{**args, **kw})


def test_the_production_builder_reproduces_the_f1_vector_byte_for_byte():
    signed = _build(O["f1"])
    assert signed == ITEM
    assert F.item_hash(signed) == P4["item_hash"] == SO.item_hash(signed)


def test_an_item_without_f1_is_unchanged():
    assert "f1" not in _build(None)["o"]


def _mutated(path, value):
    f1 = copy.deepcopy(O["f1"])
    node = f1
    for k in path[:-1]:
        node = node[k]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return f1


@pytest.mark.parametrize(
    "f1",
    [
        _mutated(["roster_v"], -1),
        _mutated(["roster_v"], True),
        _mutated(["gates", "code", "source_sha"], KeyError),
        _mutated(["gates", "plan", "source_sha"], SRC),
        _mutated(["gates", "code", "hash"], "sha256:xyz"),
        _mutated(["gates", "code", "extra"], 1),
        _mutated(["gates", "bogus"], O["f1"]["gates"]["plan"]),
        _mutated(["questions", "q1"], {"hash": H}),
        _mutated(["questions", "Q9"], {"hash": H, "x": 1}),
        _mutated(["gates", "code", "source_sha"], [SRC[0], SRC[0]]),
        _mutated(["gates", "code", "source_sha"], [{**SRC[0], "sha": "ABC"}]),
        _mutated(["gates", "code", "source_sha"], [{**SRC[0], "ref": "main"}]),
        {**O["f1"], "extra": 1},
    ],
)
def test_a_malformed_f1_is_never_signed(f1):
    with pytest.raises(HubRefused) as e:
        _build(f1)
    assert e.value.code == "malformed"


def test_f1_on_a_workspace_chain_is_never_signed():
    chain = {**O["chain"], "log": "workspace"}
    with pytest.raises(HubRefused):
        _build(O["f1"], chain=chain)


def test_at_most_64_questions():
    f1 = {"roster_v": 0, "gates": {}, "questions": {f"Q{i}": {"hash": H} for i in range(1, 66)}}
    with pytest.raises(HubRefused):
        F.check_f1(f1, {"log": UID})


# --- facts -> f1 ------------------------------------------------------------------------------------------------------


def _gate(name, hash_="1", gen=1, sources=None):
    return SH.GateFact(
        name, gen, "sha256:" + hash_ * 64, "sha256:" + "9" * 64,
        tuple((s["repo"], s["ref"], s["sha"]) for s in (sources or ())) if name in ("code", "verify") else None,
    )  # fmt: skip


def _ticket(roster_v=3, gates=None, questions=None, needs=None):
    gates = gates if gates is not None else (_gate("plan"), _gate("code", "2", sources=SRC), _gate("verify", "3"))
    return SH.TicketFact(
        UID, "DEMO-1", "feature", "T", "open", (), None,
        questions if questions is not None else (SH.QuestionFact("Q1", "which?", True, H),
                                                 SH.QuestionFact("Q2", "other", False, "sha256:" + "5" * 64)),
        needs if needs is not None else (SH.NeedFact("approve", "code", True), SH.NeedFact("question", "Q1", True)),
        tuple(gates), roster_v,
    )  # fmt: skip


def test_make_f1_names_only_what_the_item_is_about():
    t = _ticket()
    f1 = SH.make_f1(t, {"code"}, {"Q1"})
    assert f1 == {
        "roster_v": 3,
        "gates": {"code": {"gen": 1, "hash": "sha256:" + "2" * 64, "policy_hash": "sha256:" + "9" * 64,
                           "source_sha": SRC}},
        "questions": {"Q1": {"hash": H}},
    }  # fmt: skip
    F.check_f1(f1, {"log": UID})
    assert SH.make_f1(t, (), ())["gates"] == {} and SH.make_f1(t, {"requirements"}, ())["gates"] == {}  # no such fact


def test_a_code_gate_with_no_source_yet_carries_the_empty_list():
    f1 = SH.make_f1(_ticket(gates=[_gate("verify", sources=())]), {"verify"}, ())
    assert f1["gates"]["verify"]["source_sha"] == []


def test_a_gate_the_contract_cannot_express_is_left_out_not_sent_wrong():
    bad = [{"repo": "r", "ref": "main", "sha": "1" * 40}]  # ref outside refs/heads/
    t = _ticket(gates=[_gate("code", sources=bad), _gate("plan")])
    f1 = SH.make_f1(t, {"code", "plan"}, ())
    assert set(f1["gates"]) == {"plan"}


def test_without_a_roster_version_there_is_no_f1():
    assert SH.make_f1(_ticket(roster_v=None), {"plan"}, ()) is None


def _select(t):
    return {
        i.logical: i
        for i in SH.select([{"kind": "needs_you"}, {"kind": "questions"}, {"kind": "ticket", "ticket": "DEMO-1"}], [t])
    }


def test_each_item_kind_gets_its_own_f1():
    items = _select(_ticket())
    assert set(items[f"summary:{UID}:approve:code"].f1["gates"]) == {"code"}
    assert items[f"summary:{UID}:approve:code"].f1["questions"] == {}
    assert items[f"summary:{UID}:question:Q1"].f1 is None  # V3-D31: a one-line question summary is never answerable
    assert (
        items[f"question:{UID}:Q1"].f1["questions"] == {"Q1": {"hash": H}}
        and items[f"question:{UID}:Q1"].f1["gates"] == {}
    )
    assert items[f"ticket:{UID}"].f1 is None  # a ticket item names no decision


@pytest.mark.parametrize(
    "change",
    [
        lambda: _ticket(roster_v=4),
        lambda: _ticket(gates=(_gate("plan"), _gate("code", "7", sources=SRC), _gate("verify", "3"))),
        lambda: _ticket(gates=(_gate("plan"), _gate("code", "2", gen=2, sources=SRC), _gate("verify", "3"))),
        lambda: _ticket(
            gates=(_gate("plan"), _gate("code", "2", sources=[{**SRC[0], "sha": "2" * 40}]), _gate("verify", "3"))
        ),
        lambda: _ticket(questions=(SH.QuestionFact("Q1", "which?", True, "sha256:" + "6" * 64),)),
    ],
)
def test_any_changed_f1_value_changes_the_digest_but_not_the_plan(change):
    base = _select(_ticket())
    now = _select(change())
    for lg in (f"summary:{UID}:approve:code", f"summary:{UID}:question:Q1", f"question:{UID}:Q1"):
        if base[lg].f1 != now[lg].f1:
            assert base[lg].digest != now[lg].digest
        if base[lg].data == now[lg].data:
            assert base[lg].shown == now[lg].shown  # what the owner is shown did not change
    assert any(base[k].digest != now[k].digest for k in base)


def test_a_roster_change_changes_every_item():
    a, b = _select(_ticket(roster_v=3)), _select(_ticket(roster_v=4))
    assert all(a[k].digest != b[k].digest for k in a if a[k].f1 is not None)
    assert a[f"ticket:{UID}"].digest == b[f"ticket:{UID}"].digest  # a ticket item carries no f1


# --- sync: a changed value is a higher item_version ---


class _Link:
    def __init__(self):
        self.calls = []

    def call(self, op, body):
        self.calls.append((op, body))
        return {}


def test_sync_re_puts_under_a_higher_version_only_when_f1_changed():
    st, link, built = SH.ShareState(), _Link(), []

    def build(item_id, item, version):
        built.append((item.logical, (item.f1 or {}).get("roster_v"), version))
        return "sealed", "sha256:" + "0" * 64

    def run(t):
        n = len(built)
        got = SH.sync(st, list(_select(t).values()), link=link, build=build, save=lambda s: None)
        return got, built[n:]

    first, _ = run(_ticket())
    assert first["put"] == 4 and first["kept"] == 0
    again, none = run(_ticket())
    assert (again["put"], again["kept"]) == (0, 4) and none == []  # nothing changed: nothing sent
    bumped, news = run(_ticket(roster_v=4))  # a member change raises roster_v: every item is put again
    assert (bumped["put"], bumped["kept"]) == (2, 2)  # the ticket item and the question summary carry no f1
    assert {v for _, r, v in news} == {2} and {r for _, r, _v in news} == {4}
    gate, news = run(_ticket(roster_v=4, gates=(_gate("plan"), _gate("code", "7", sources=SRC), _gate("verify", "3"))))
    assert gate["put"] == 1 and {lg for lg, _r, _v in news} == {f"summary:{UID}:approve:code"}
    assert {v for _, _r, v in news} == {3}


def test_an_f1_that_does_not_fit_the_cap_is_shared_without_it_not_dropped():
    t = _ticket(gates=[_gate("code", sources=[
        {"repo": "https://github.com/demo/" + f"r{i:02}" + "x" * 480, "ref": "refs/heads/" + "b" * 200, "sha": "1" * 64}
        for i in range(32)])])  # fmt: skip
    item = next(i for i in SH.select([{"kind": "needs_you"}], [t]) if i.f1 and i.f1["gates"])
    assert item.f1 is not None and len(item.f1["gates"]["code"]["source_sha"]) == 32
    from tests.hub.vec import private_key, pub

    build = SH.make_builder(
        hub_id=O["hub_id"], workspace_id=O["workspace_id"], hxk_pub=pub("hxk"), sign=_sign_det, dev=True,
        now_ms=lambda: 1, chain_of=lambda uid: (2, H),
    )  # fmt: skip
    sealed, _h = build("33" * 16, item, 1)
    opened = SO.open_share_item(
        {"item_id": "33" * 16, "sealed": sealed}, hub_id=O["hub_id"], workspace_id=O["workspace_id"],
        hxk_priv=private_key("hxk"), wsk_pub=pub("wsk_a"), dev_mode=True,
    )  # fmt: skip
    assert "f1" not in opened["item"]  # fail closed: the hub answers `unavailable` to a build on it
