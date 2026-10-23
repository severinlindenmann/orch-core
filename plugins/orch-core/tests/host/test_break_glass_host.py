"""V3 C8 on the host: the leave with the hub dead or hostile (publish told directly, the
new epoch local), ``keys.rotate`` and the review. The model rules are in tests/model/test_break_glass.py."""

# ruff: noqa: F811  (fixtures imported from test_hub_ops are redefined as arguments)
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orch.host import breakglass, convenience, hubops
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_ops import (  # noqa: F401 - fixtures
    CARD,
    HUB_ID,
    connect_args,
    dev_workspace,
    join_args,
    keystore,
    stub,
)

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


# --- publish is told directly --------------------------------------------------------------------------------------


def _service(server: str | None = "https://publish.example"):
    ws = SimpleNamespace(settings={"publish": {"server": server, "share": "https://s.example"}} if server else {})
    store = SimpleNamespace(state=SimpleNamespace(workspace=ws))
    return SimpleNamespace(store=store, workspace_id="a" * 32)


class Net:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, scheme, netloc, method, target, headers, body, timeout):
        self.calls.append((scheme, netloc, method, target, bytes(body)))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


UNBOUND = {"o": {"kind": "hub.unbound", "binding_seq": 1}, "sig": "x"}


def test_publish_put_is_a_put_to_the_binding_route_and_retries_a_5xx_and_a_lost_answer(monkeypatch):
    net = Net((503, b""), OSError("lost"), (204, b""))
    monkeypatch.setattr(breakglass, "TRANSPORT", net)
    monkeypatch.setattr(breakglass, "_SLEEP", lambda s: None)
    put = breakglass.publish_put(_service())
    assert put(UNBOUND) is True
    assert [c[2:4] for c in net.calls] == [("PUT", f"/v2/hub/bindings/{'a' * 32}")] * 3
    assert net.calls[0][0:2] == ("https", "publish.example")


def test_publish_put_gives_up_after_the_attempts_and_a_refusal_is_final(monkeypatch):
    monkeypatch.setattr(breakglass, "_SLEEP", lambda s: None)
    monkeypatch.setattr(breakglass, "TRANSPORT", Net(*[(503, b"")] * 3))
    assert breakglass.publish_put(_service())(UNBOUND) is False
    monkeypatch.setattr(breakglass, "TRANSPORT", Net((400, b'{"error":"bad_signature"}')))
    assert breakglass.publish_put(_service())(UNBOUND) is False
    monkeypatch.setattr(breakglass, "TRANSPORT", Net((409, b'{"error":"stale_binding"}')))
    assert breakglass.publish_put(_service())(UNBOUND) is True  # publish holds a newer binding: nothing to unbind


def test_no_publish_server_means_nothing_to_tell():
    assert breakglass.publish_put(_service(None)) is None


def _leave_full(human, served, stub, keystore):
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})


def test_leave_with_a_dead_hub_tells_publish_directly_and_keeps_the_hub_owed(
    human, served, stub, keystore, monkeypatch
):
    told = []
    monkeypatch.setattr(breakglass, "publish_put", lambda service: lambda u: told.append(u) or True)
    _leave_full(human, served, stub, keystore)
    monkeypatch.setattr(hubops, "TRANSPORT", None)  # the hub is dead
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["publish_told"] is True and not got["delivered"]
    (u,) = told
    assert u["o"]["kind"] == "hub.unbound" and u["o"]["hub_id"] == HUB_ID  # signed by WSK, made on this machine
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["unbound_pending"] is True
    assert got["review"]["hub_id"] == HUB_ID and got["review"]["first"] == []  # the hub did nothing here


def test_publish_stays_owed_until_it_takes_it(human, served, stub, keystore, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(breakglass, "publish_put", lambda service: lambda u: next(answers))
    _leave_full(human, served, stub, keystore)
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["publish_told"] is False
    state = hubops._load_state(served.service)
    assert list(state.publish_owed) == [HUB_ID]  # kept in hub.json, signed
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["publish_told"] is True and hubops._load_state(served.service).publish_owed == {}


# --- the new epoch is local -------------------------------------------------------------


class FakeMk:
    previously_linked = True

    def __init__(self):
        self.calls, self.epoch = [], 1

    def current_epoch(self):
        return self.epoch

    def advance_local(self, owner):
        self.calls.append(("local", owner))
        self.epoch += 1

    def raise_gate(self, *a):
        self.calls.append(("gate",))


def test_rotate_makes_the_epoch_locally():
    local = FakeMk()
    hubops._Epochs(local, "owner").rotate()
    assert local.calls == [("local", "owner")] and local.epoch == 2


# --- keys.rotate ---------------------------------------------------------------------------------------------------


def test_keys_rotate_is_refused_to_agents_and_to_the_click_signer(served):
    c = served.client()
    assert err(c.req("keys.rotate", {}, grant=served.grant, session=SESSION, idem=True)) in (
        "op.not_allowed",
        "human_only",
    )
    with pytest.raises(Exception, match="terminal"):
        convenience.refuse_hub_op("keys.rotate", "convenience")


def test_keys_rotate_makes_the_owner_a_new_epoch(served, monkeypatch):
    mk = FakeMk()
    monkeypatch.setattr(hubops, "epochs_for", lambda service, create=False: hubops._Epochs(mk, "owner"))
    got = ok(served.client().req("keys.rotate", {}, idem=True))["data"]
    assert got == {"epoch_before": 1, "epoch": 2, "rotated": True, "path": "local"}


def test_keys_rotate_without_a_key_store_says_so(served, monkeypatch):
    monkeypatch.setattr(hubops, "epochs_for", lambda service, create=False: None)
    assert err(served.client().req("keys.rotate", {}, idem=True)) == "keys.no_epoch"


def test_keys_rotate_is_refused_while_joined_in_full_mode(served, monkeypatch):
    """L2-C8i-2: an epoch made while the hub is a full member would never be sealed to it: leave first."""
    monkeypatch.setattr(hubops, "epochs_for", lambda service, create=False: hubops._Epochs(FakeMk(), "owner"))
    for mode in ("full", "unknown"):
        monkeypatch.setattr(hubops, "signing_mode", lambda service, mode=mode: mode)
        assert err(served.client().req("keys.rotate", {}, idem=True)) == "keys.joined"
    monkeypatch.setattr(hubops, "signing_mode", lambda service: "selective")
    assert ok(served.client().req("keys.rotate", {}, idem=True))["data"]["rotated"] is True


def test_keys_rotate_is_refused_while_the_key_is_in_convenience_custody(served, monkeypatch):
    from orch.host import hubsign

    monkeypatch.setattr(hubops, "epochs_for", lambda service, create=False: hubops._Epochs(FakeMk(), "owner"))
    monkeypatch.setattr(hubsign, "_click_custody", lambda service: True)
    assert err(served.client().req("keys.rotate", {}, idem=True)) == "sign.convenience_unavailable"


def test_keys_no_epoch_and_keys_joined_are_declared_host_codes():
    from orch.host.errors import HOST_CODES, disposition

    assert HOST_CODES["keys.no_epoch"] == (3, False) and HOST_CODES["keys.joined"] == (3, False)
    assert disposition("keys.no_epoch") == (3, False)


# --- the review ----------------------------------------------------------------------------------------------------


def test_history_needs_a_hub_and_answers_the_owner(human, served, stub):
    assert err(served.client().req("hub.history", {}, idem=False)) == "hub.not_joined"
    human.run("hub.join", join_args())
    got = ok(served.client().req("hub.history", {}, idem=False))["data"]
    assert got["hub_id"] == HUB_ID and got["first"] == [] and got["stuck"] == []
    assert err(served.client().req("hub.history", {}, grant=served.grant, session=SESSION, idem=False)) in (
        "op.not_allowed",
        "human_only",
    )
