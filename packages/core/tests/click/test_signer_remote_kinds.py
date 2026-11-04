"""R2 T3 (#1590): a call that carries ``remote: true`` (a remote dashboard session) never prepares, views,
signs or cancels any ``remote.*`` kind. The route gate of the dashboard refuses them too; this is the signer's own
check, so a server that skipped it gets no signature. At the loopback (no ``remote`` flag) the two owner-proof steps are
clicks with the backup key sealed to the signer, like a hub step."""

from __future__ import annotations

# ruff: noqa: F811 - pytest fixtures imported by name and used as arguments
from types import SimpleNamespace

import pytest

from orch.click import protocol
from orch.click.protocol import ClickError
from orch.click.signer import ClickSigner

WID = "ab" * 16
RID = "hr_" + "0" * 26
DIGEST = "sha256:" + "1" * 64
REMOTE_KINDS = ("remote.set", "remote.device_confirm", "remote.off", "remote.code", "remote.device_remove")


@pytest.fixture
def signer(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    entry = {"workspace_id": WID, "prefix": "w", "root": str(tmp_path), "peers": {}}
    s = ClickSigner(state, workspaces=lambda: [entry])
    yield s
    s.close()


def refusal(signer, op, args):
    with pytest.raises(ClickError) as caught:
        signer.handle(op, args)
    return caught.value


@pytest.mark.parametrize("kind", REMOTE_KINDS)
def test_signer_refuses_remote_kinds_when_remote_true(signer, kind):
    exc = refusal(
        signer,
        "prepare",
        {"workspace_id": WID, "op": kind, "args": {}, "idem": "a_" + "0" * 26, "remote": True},
    )
    assert exc.code == "human.click_refused" and exc.reason == "terminal_only"
    assert "remote session" in exc.message


@pytest.mark.parametrize("op", ["view", "sign", "cancel"])
def test_a_request_of_a_remote_kind_is_refused_on_every_follow_up(signer, op):
    """The request may have been prepared at the loopback; a remote call still cannot view, sign or cancel it."""
    signer.approvals[(WID, RID)] = SimpleNamespace(op="remote.set", seal=None)
    exc = refusal(signer, op, {"workspace_id": WID, "request_id": RID, "view_digest": DIGEST, "remote": True})
    assert exc.code == "human.click_refused" and exc.reason == "terminal_only"


def test_the_check_runs_before_the_stepup_and_the_op(signer):
    """Even with a step-up proof that would be wrong, the refusal is the kind's, not a step-up or an unknown op."""
    exc = refusal(
        signer,
        "prepare",
        {"workspace_id": WID, "op": "remote.set", "args": {}, "idem": "a_" + "0" * 26, "remote": True, "stepup": {}},
    )
    assert exc.reason == "terminal_only"


def test_other_ops_are_not_touched_by_this_check(signer):
    """A remote session still prepares what it always did: the call goes on to the op (which has no host here)."""
    try:
        signer.handle(
            "prepare", {"workspace_id": WID, "op": "log", "args": {}, "idem": "a_" + "0" * 26, "remote": True}
        )
    except ClickError as exc:
        assert not (exc.reason == "terminal_only" and "remote session" in exc.message)
    except Exception:  # noqa: BLE001, S110 - the bare signer has no host behind it
        pass


def test_at_the_loopback_the_owner_proof_steps_are_sealed_clicks():
    for op in ("remote.set", "remote.device_confirm"):
        assert op in protocol.CLICK_OPS and protocol.SEALED_OPS[op] == ("hub.backup_key",)
        assert protocol.OP_TITLES[op]
    for op in ("remote.off", "remote.code", "remote.device_remove", "remote.key_rotate"):
        assert op not in protocol.CLICK_OPS  # unsigned host ops: the dashboard calls them directly, never signs them


def test_the_remote_steps_are_marked_risky_with_their_own_words():
    changed = {"type": "remote.changed", "agents_risk": True, "hub_mode": "full"}
    lines = protocol.risk_lines("remote.set", [changed], deposit=False, reveal=False)
    assert protocol.REMOTE_RISK in lines and protocol.REMOTE_AGENTS_RISK in lines
    assert protocol.REMOTE_HUB_FULL_LINE in lines
    added = protocol.risk_lines("remote.device_confirm", [{"type": "remote.device_added"}], deposit=False, reveal=False)
    assert added == [protocol.REMOTE_DEVICE_RISK]


# -- the loopback path: remote.set and device confirmation by click, with the backup key sealed to the signer ---------

from tests.host import human_helpers  # noqa: E402
from tests.host.test_click_hub import CODE, click, fast_world, seal_for  # noqa: E402, F401 - fixtures and helpers

lab, served = human_helpers.lab, human_helpers.served


def test_remote_set_by_click_with_the_backup_key(click, served):
    p = click.prepare("remote.set", {"level": "work", "premises": "no", "days": 3, "agents_risk": True})
    v = click.view(p["request_id"])
    click.mono.t += 1.5
    view = v["view"]
    assert view["seal"]["purposes"] == ["hub.backup_key"] and view["backup_key_line"] in view["lines"]
    assert protocol.REMOTE_RISK in view["risk_lines"] and protocol.REMOTE_AGENTS_RISK in view["risk_lines"]
    click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))
    served.service.store.refresh()
    r = served.service.store.state.workspace.remote
    assert r["on"] is True and r["level"] == "work" and r["premises"] == "no"
    assert click.signatures == 1


def test_a_remote_session_cannot_prepare_it_even_with_a_good_signer(click, served):
    with pytest.raises(ClickError) as caught:
        click.signer.handle(
            "prepare",
            {"workspace_id": served.wid, "op": "remote.set", "args": {"level": "work", "premises": "no", "days": 3},
             "idem": "a_" + "0" * 26, "remote": True},
        )  # fmt: skip
    assert caught.value.reason == "terminal_only" and click.signatures == 0


@pytest.mark.parametrize("op", ["view", "sign", "cancel"])
def test_a_swept_request_of_a_remote_kind_is_still_refused_from_its_record(signer, op):
    """R2 review L1: the approval is gone from memory (swept), the durable record still names the kind."""
    signer.records.claim(
        WID, RID, view_digest=DIGEST, op="remote.set", idem="a_" + "0" * 26, expires_at="2099-01-01T00:00:00Z"
    )
    assert (WID, RID) not in signer.approvals
    exc = refusal(signer, op, {"workspace_id": WID, "request_id": RID, "view_digest": DIGEST, "remote": True})
    assert exc.code == "human.click_refused" and exc.reason == "terminal_only"
