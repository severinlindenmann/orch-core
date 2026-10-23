# ruff: noqa: F811
"""V3 C10 (design-c10 M1 to M4): the H5 switch on the host, relay-less (the relay half has its own test file)."""

from __future__ import annotations

from typing import Any

import pytest

from orch.host import cutover, directpath, hubops, relayhook, relaylink
from orch.host.errors import HostError
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.test_hub_deliver import ALICE, Env, hub_snapshot
from tests.host.test_hub_ops import dev_workspace, stub  # noqa: F401

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def state_of(env: Env) -> Any:
    return hubops._load_state(env.service)  # noqa: SLF001


def pin_snapshot(env: Env, roles: dict[str, str] | None = None) -> None:
    st = state_of(env)
    st.snapshot = hub_snapshot(env.wid, 3, roles or {ALICE: "admin"})
    env.service.submit(hubops._save_state, env.service, st).result()  # noqa: SLF001


@pytest.fixture
def env(served, human, stub):
    e = Env(served, human, alias=True)
    e.human = human  # type: ignore[attr-defined]
    return e


def test_without_a_snapshot_with_an_admin_the_workspace_stays_open_and_says_why(env):
    pin_snapshot(env, {ALICE: "edit"})
    assert cutover.run(env.service, force_start=True) == "open"
    st = state_of(env)
    assert st.direct == "open" and "admin" in st.direct_note
    assert not directpath.closed(env.service.state_dir, env.wid, env.service.host_uid)


def test_the_switch_rotates_to_an_empty_epoch_seals_it_and_closes(env):
    pin_snapshot(env)
    before = hubops.epochs_for(env.service).current_epoch()
    assert cutover.run(env.service, force_start=True) == "closed"
    st = state_of(env)
    assert st.direct == "closed" and st.direct_note is None
    assert st.cut_epoch == before + 1 and st.sealed["epoch"] == st.cut_epoch
    epochs = hubops.epochs_for(env.service)
    assert epochs.current_epoch() == st.cut_epoch
    assert epochs._mk.listed_devices() == []  # noqa: SLF001 - I4: no direct device in the epoch the hub alone holds
    assert directpath.direct_state(env.service.state_dir, env.wid, env.service.host_uid) == "closed"
    fields = cutover.status_fields(env.service, st)
    assert fields["direct"] == "closed" and fields["direct_devices"] == [] and fields["cut_epoch"] == st.cut_epoch


def test_a_closed_workspace_starts_no_relay_worker_and_takes_no_pairing(env, monkeypatch):
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    assert relaylink.start_blocker(env.service.state_dir, env.wid, env.service.host_uid) == "cut_over"
    with pytest.raises(HostError) as e:
        relayhook.start_worker(env.service, None, relayhook.SEAMS)
    assert e.value.code == "relay.cut_over"
    from orch.host import relaypair

    with pytest.raises(HostError) as e:
        relaypair.refuse_if_cut_over(env.service)
    assert e.value.code == "relay.cut_over"
    with pytest.raises(HostError) as e:
        relaypair.api(env.service, "offer", ("look",), 1000)
    assert e.value.code == "relay.cut_over"


def test_a_crash_after_closing_is_resumed_at_the_next_start(env):
    pin_snapshot(env)
    st = state_of(env)
    st.direct = "closing"  # the host died right after saving `closing`
    env.service.submit(hubops._save_state, env.service, st).result()  # noqa: SLF001
    assert directpath.closed(env.service.state_dir, env.wid, env.service.host_uid)  # behaves as closed at once
    model = cutover._model(env.service)  # noqa: SLF001
    assert cutover.needs_run(env.service, model, state_of(env))
    assert cutover.run(env.service, model) == "closed"
    assert state_of(env).direct == "closed"


def test_a_rotation_made_before_the_crash_is_not_made_twice(env):
    pin_snapshot(env)
    st = state_of(env)
    st.direct = "closing"
    env.service.submit(hubops._save_state, env.service, st).result()  # noqa: SLF001
    epochs = hubops.epochs_for(env.service)
    epochs.rotate_cut_over()
    st = state_of(env)
    st.cut_epoch = epochs.current_epoch()
    env.service.submit(hubops._save_state, env.service, st).result()  # noqa: SLF001
    assert cutover.run(env.service) == "closed"
    assert hubops.epochs_for(env.service).current_epoch() == st.cut_epoch  # sealed, not rotated again


def test_a_pre_c10_full_join_is_cut_over_only_for_a_dev_workspace_with_a_dev_card(env, monkeypatch):
    pin_snapshot(env)
    st = state_of(env)
    st.direct = None  # as saved before C10
    env.service.submit(hubops._save_state, env.service, st).result()  # noqa: SLF001
    model = cutover._model(env.service)  # noqa: SLF001
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: False)
    assert not cutover.needs_run(env.service, model, state_of(env))  # pre_c10_non_dev_full_join_not_cut_over
    assert cutover.run(env.service, model) == "open"
    assert state_of(env).direct is None and not directpath.closed(env.service.state_dir, env.wid, env.service.host_uid)
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    assert cutover.needs_run(env.service, model, state_of(env))  # a dev workspace and a dev hub card
    assert cutover.run(env.service, model) == "closed"


def test_an_unreadable_hub_json_reads_as_closed(env):
    path = directpath.hub_json(env.service.state_dir, env.wid)
    path.write_text("{not json")
    assert directpath.direct_state(env.service.state_dir, env.wid, env.service.host_uid) == "closed"


def test_the_gate_allows_exactly_the_cutover_routes_and_the_session(env):
    ws = env.wid
    allow = directpath.allows
    assert allow("open", ws, "POST", f"/v2/workspaces/{ws}/bridge/x")
    for state in ("closing", "closed"):
        assert allow(state, ws, "GET", f"/v2/workspaces/{ws}/cutover")
        assert allow(state, ws, "PUT", f"/v2/workspaces/{ws}/cutover")
        assert allow(state, ws, "DELETE", f"/v2/workspaces/{ws}/cutover?x=1")
        assert allow(state, ws, "POST", "/v2/session/login") and allow(
            state, ws, "POST", f"/v2/workspaces/{ws}/host/hold"
        )
        for m, p in (
            ("POST", f"/v2/workspaces/{ws}/bridge/r1"), ("GET", f"/v2/workspaces/{ws}/host/poll"),
            ("PUT", f"/v2/workspaces/{ws}/card"), ("PUT", f"/v2/workspaces/{ws}/members"),
            ("PUT", f"/v2/workspaces/{ws}/genesis"), ("POST", f"/v2/workspaces/{ws}/push"),
            ("GET", f"/v2/workspaces/{ws}/unpublished"), ("PUT", f"/v2/workspaces/{ws}/checkpoint"),
            ("POST", "/v2/anything/new"), ("GET", f"/v2/workspaces/{'0' * 32}/cutover"),
        ):  # fmt: skip
            assert not allow(state, ws, m, p), (state, m, p)


def test_a_relay_conn_with_the_gate_refuses_before_any_byte_is_sent(env):
    from orch.relay import http

    dial = []
    conn = http.RelayConn(
        "http://127.0.0.1:9", allowed=lambda m, t: False, resolve=lambda *a, **k: dial.append(a) or []
    )  # fmt: skip
    with pytest.raises(http.CutOver):
        conn.request("GET", "/v2/workspaces/x/host/poll", route="r", headers={}, body=None, deadline_s=1, cap=10)
    assert dial == []  # not even a name lookup


def test_hub_status_reports_the_switch(env):
    pin_snapshot(env)
    cutover.run(env.service, force_start=True)
    data = ok(env.served.client().req("hub.status", {}))["data"]
    assert data["direct"] == "closed" and data["direct_devices"] == [] and data["cut_epoch"]


def test_orch_hub_mode_full_again_triggers_the_cut_over_of_a_pre_c10_full_join_and_is_refused_after(env):
    import copy

    from tests.host.test_hub_ops import CARD, connect_args

    pin_snapshot(env)
    st = state_of(env)
    st.direct = None  # joined and connected before C10
    env.service.submit(hubops._save_state, env.service, st).result()  # noqa: SLF001
    binding = cutover._model(env.service)["binding_seq"]  # noqa: SLF001
    _, _, signed = env.human.run("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})
    assert signed.events[0]["binding_seq"] == binding and signed.events[0]["aliases"] == []  # same binding
    assert cutover._model(env.service).get("affirmed_at")  # noqa: SLF001
    assert state_of(env).direct is None  # signing alone starts nothing
    ok(env.served.client().req("hub.connect", connect_args(invite=None) | {}, idem=True)) if False else None
    args = connect_args()
    args.pop("invite")
    got = ok(env.served.client().req("hub.connect", args, idem=True))["data"]
    assert got["direct"] == "closed" and state_of(env).direct == "closed"
    from tests.host.human_helpers import err

    assert (
        err(env.human.prepare_res("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"}))
        == "hub.already_joined"
    )


def test_the_model_accepts_a_re_signed_full_mode_only_for_the_same_hub_binding_and_no_aliases():
    from orch.model import Code
    from tests.model.test_hub_consumers import HUB, alias, hub_joined
    from tests.model.test_hubledger import ROOT
    from tests.model.world import World

    w = World(validate=False).bootstrap({"mara": "maintainer"})
    hub_joined(w, [alias(w, "mara", "2" * 32)])
    base = dict(hub_id=HUB, hk_root_pub=ROOT, mode="full", binding_seq=1, aliases=[])
    _, r = w.try_("workspace", "hub.joined", "sev", roster_v=w.roster_v, **base)
    assert not hasattr(r, "code")
    for over, why in (({"binding_seq": 2}, "next binding"), ({"aliases": [alias(w, "mara", "3" * 32)]}, "alias"),
                      ({"mode": "selective"}, "downgrade"), ({"binding_seq": 0}, "lower")):  # fmt: skip
        _, r = w.try_("workspace", "hub.joined", "sev", roster_v=w.roster_v, **{**base, **over})
        assert getattr(r, "code", None) == Code.HUB_INVALID, why
