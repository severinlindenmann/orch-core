"""L2 security review of D15 (orch-hub#31 @ 8d9389c), Opus l2-d15. Each strict xfail names one finding and flips when
it is fixed (then drop the marker). The last tests pin invariants that hold."""

from __future__ import annotations

import pytest
from custodial_helpers import LOG, WK, WS
from test_view_build import (
    code,
    env,  # noqa: F401 (fixture)
)
from test_view_needs import USER, Acc, Reg
from test_view_service import make_seals
from view_helpers import OWNER, WSK_PUB, Host, build_view_services

from orch_hub.contract.hub import Refused

OTHER = "p_" + "22" * 16


@pytest.fixture
def kit(tmp_path):
    seals = make_seals(tmp_path, WS, {1: WK})
    (tmp_path / "v").mkdir(mode=0o700)
    reg, acc = Reg(alias=USER), Acc()  # USER is aliased to OWNER, the ticket owner
    vs = build_view_services(tmp_path / "v", wk_seals=seals, registry=reg, access=acc)
    h = Host()
    h.genesis()
    h.ticket()
    k = type("K", (), {})()
    k.vs, k.host, k.reg, k.r, k.seals = vs, h, reg, vs.reader, seals
    k.push = lambda: h.push_all(vs)
    yield k
    vs.close()
    seals.close()


# --- L2-D15-1 (P1): the hub's visibility rule is wider than the host's -------------------------------------------------
# core model/visibility.can_see: person in ws.members and (visibility == "workspace" or person in restricted).
# There is no ticket-owner clause: an owner or maintainer may restrict a ticket to a list without its ticket owner.


def test_d15_1_needs_hides_restricted_ticket_from_off_list_ticket_owner(kit):
    kit.host.ask(text="secret plan?")
    kit.host.append(LOG, "visibility.changed", visibility={"restricted": [OTHER]}, roster_v=1)
    kit.push()
    # host: OWNER is not on the list -> not_found everywhere. Hub must agree.
    assert kit.r.needs_you(WS, USER) == []


def test_d15_1_build_refuses_off_list_ticket_owner(env):  # noqa: F811
    me = "u_" + env.user
    env.host.append(LOG, "people.changed", role="owner", add=[me], remove=[OWNER], roster_v=2)
    env.host.append(LOG, "visibility.changed", visibility={"restricted": [OTHER]}, roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    assert code(env.prepare, {"type": "log.added", "log": LOG, "text": "probe"}) == "unknown_item"


# --- L2-D15-2 (P2): unexpected exception types escape the "any failure is nothing / Refused" boundary -------------


def test_d15_2_non_object_option_reads_as_nothing(kit):
    kit.host.ask(options=["csv"])  # host-signed, hash consistent, but not an option object
    kit.push()
    kit.r.needs_you(WS, USER)  # must not raise (the Protocol has no error channel)


def test_d15_2_unhashable_restricted_entry_is_refused(env):  # noqa: F811
    env.host.append(LOG, "visibility.changed", visibility={"restricted": [{"p": OTHER}]}, roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    with pytest.raises(Refused):
        env.prepare({"type": "log.added", "log": LOG, "text": "x"})


# --- invariants that hold ---------------------------------------------------------------------------------------------


def test_holds_unbind_drop_and_retired_epoch(kit):
    kit.host.ask()
    kit.push()
    assert kit.r.needs_you(WS, USER)
    kit.seals.retire(WS)
    with pytest.raises(Refused) as r:
        kit.seals.open_for_view(WS, 1)  # a retired epoch never opens
    assert r.value.code == "unavailable"
    assert kit.r.needs_you(WS, USER) == []
    kit.vs.store.drop(WS)
    assert kit.vs.store.logs(WS) == [] and kit.vs.service.head(WS, "workspace") is None


def test_holds_forged_line_and_fork_are_refused(kit):
    kit.push()
    other = Host(key=__import__("view_helpers").G.K.priv["wsk_b"])  # same WK, another host key
    other.genesis()
    other.append("workspace", "member.added", roster_v=1)
    with pytest.raises(Refused) as r:
        kit.vs.service.accept(WS, WSK_PUB, other.page("workspace", 2))
    assert r.value.code in ("bad_signature", "view_fork")


def test_keyring_close_wipes_its_copy_and_never_copies_a_borrowed_key(kit):
    from orch_hub.view.service import Keyring

    borrowed = bytearray(b"k" * 32)
    ring = Keyring(kit.seals, WS, {7: borrowed})
    assert ring.get(7) is borrowed  # borrowed, not copied
    opened = ring.get(1)
    assert any(opened)
    ring.close()
    assert not any(opened)  # the copy it opened is wiped
    assert borrowed == bytearray(b"k" * 32)  # the caller's key is the caller's to wipe
