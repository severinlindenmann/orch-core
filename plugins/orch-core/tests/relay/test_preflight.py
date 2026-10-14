"""Round 6 (orch-52 #1): one local preflight (instance binding, ``pending``, (a), (b), a stored ``gen``) runs before ANY
network call, on every entry point: ``first_link`` (before the session-less ``PUT card``), ``Session.hold``, ``login``,
``challenge``, and every directory call. When it fails, not one request leaves, and the session stays refused."""

from __future__ import annotations

import shutil

import pytest

from orch.relay.link import first_link
from orch.relay.session import Fenced, TakeoverRefused
from orch.relay.state import FileHolderStore, HolderRecord, StateDir

from .helpers import GENESIS, GENESIS_CERT, GENESIS_PIN, OWNER_PK, WS, Alarms, Clock, Holder, directory, make_card
from .helpers import retrier as make_retrier
from .helpers import session as make_session
from .mock import MockRelay
from .sim import SimRelay
from .test_adversarial import Steps

IID = "0" * 32


class CopiedHolder(Holder):
    def binding_problem(self):
        return "this state directory was copied or restored (its instance record names another directory)"


def _no_gen():
    h = Holder()
    h.rec = HolderRecord(True, None, IID, None)
    return h


FAILING = {
    "copied": lambda: CopiedHolder(gen=3),
    "copied_never_held": lambda: CopiedHolder(),
    "pending": lambda: Holder(gen=3, pending=4),
    "not_last_holder": lambda: Holder(gen=3, last_holder=False),
    "other_instance": lambda: Holder(gen=3, instance_id="1" * 32),
    "no_gen": _no_gen,
}


@pytest.fixture
def env(tmp_path):
    m = MockRelay()
    m.store = StateDir(tmp_path / "relay")
    sim = SimRelay(m.origin(), owner_pk=OWNER_PK)
    sim.cards[WS] = make_card(m.origin())
    sim.holders[WS] = (3, sim.now + 40_000)  # the original's lease is live
    m.fallback = sim
    yield m, sim
    assert m.errors == []
    m.store.close()
    m.close()


def _entry_points(m, s, d, clock):
    card = make_card(m.origin())
    return {
        "first_link": lambda: first_link(Steps(card), s, d, make_retrier(clock), store=m.store),
        "hold": s.hold,
        "login": s.login,
        "challenge": s.challenge,
        "put_card_first": lambda: d.put_card(card, first=True),
        "put_card_wg": lambda: d.put_card(card, first=False),
        "get_card": d.get_card,
        "get_members": d.get_members,
        "get_checkpoint": lambda: d.get_checkpoint(genesis=GENESIS),
        "put_genesis": lambda: d.put_genesis(GENESIS_PIN, GENESIS_CERT, genesis=GENESIS),
    }


ENTRIES = [
    "first_link", "hold", "login", "challenge", "put_card_first", "put_card_wg", "get_card", "get_members",
    "get_checkpoint", "put_genesis",
]  # fmt: skip


@pytest.mark.parametrize("why", sorted(FAILING))
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_failed_preflight_sends_nothing_on_any_entry_point(env, entry, why):
    m, sim = env
    clock = Clock(sim)
    alarms = Alarms()
    s = make_session(m.origin(), holder=FAILING[why](), clock=clock, alarms=alarms)
    d = directory(s)
    calls = _entry_points(m, s, d, clock)
    with pytest.raises(TakeoverRefused):
        calls[entry]()
    assert m.seen == [], entry
    assert alarms.codes() == ["relay_takeover_refused"]
    for other in ENTRIES:  # the refusal sticks: every later entry point is refused locally too (Fenced or a subclass)
        with pytest.raises(Fenced):
            calls[other]()
    assert m.seen == [] and len(alarms) == 1


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_passing_preflight_lets_each_entry_point_through(env, entry):
    """The control: the same calls on a directory that passes the preflight do reach the relay."""
    m, sim = env
    clock = Clock(sim)
    s = make_session(m.origin(), holder=Holder(gen=3), clock=clock)
    calls = _entry_points(m, s, directory(s), clock)
    try:
        calls[entry]()
    except Exception:  # noqa: BLE001 - a relay refusal is fine here; only "did it reach the relay" matters
        pass
    assert m.seen != [], entry


def test_a_copied_linking_directory_makes_no_request(env, tmp_path):
    """orch-52's case: a ``linking`` state directory copied with ``cp -r`` (instance record and ``holder.json``
    verbatim) resumes the first link: nothing is sent, the session-less ``PUT card`` included."""
    m, sim = env
    orig = m.store
    h = FileHolderStore(orig)
    h.save(HolderRecord(True, 3, h.instance_id(), None))
    copy_path = tmp_path / "copy"
    shutil.copytree(orig.path, copy_path)
    copy_path.chmod(0o700)
    with StateDir(copy_path) as copy:
        clock = Clock(sim)
        alarms = Alarms()
        s = make_session(m.origin(), holder=FileHolderStore(copy), clock=clock, alarms=alarms)
        with pytest.raises(TakeoverRefused, match="copied or restored"):
            first_link(Steps(make_card(m.origin())), s, directory(s), make_retrier(clock), store=copy)
        assert copy.read("link-card.json") is None  # not even the local card is persisted
    assert m.seen == [] and alarms.codes() == ["relay_takeover_refused"]


def test_first_link_checks_the_link_state_before_the_preflight(env):
    """An unknown ``link.json`` state is refused before anything else, with no alarm and no request."""
    from orch.relay.link import LinkRefused

    m, sim = env
    clock = Clock(sim)
    alarms = Alarms()
    s = make_session(m.origin(), holder=CopiedHolder(gen=3), clock=clock, alarms=alarms)
    steps = Steps(make_card(m.origin()))
    steps.state = "unlinked"
    with pytest.raises(LinkRefused):
        first_link(steps, s, directory(s), make_retrier(clock), store=m.store)
    assert m.seen == [] and alarms == []


def test_operator_takeover_stands_in_for_the_preflight(env):
    """After a refusal only the explicit operator takeover reaches the relay again."""
    m, sim = env
    clock = Clock(sim)
    s = make_session(m.origin(), holder=Holder(gen=3, pending=4), clock=clock)
    with pytest.raises(TakeoverRefused):
        s.login()
    assert m.seen == []
    got = s.operator_takeover()
    assert got.gen == 4 and m.seen != []
    s.login()  # the session works again
