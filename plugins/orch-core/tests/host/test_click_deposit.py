"""H11c: the click signer's deposit-only grant mode (§0.8.6 item 6 amended, D-P2-27, §12.4.2).

A ``grant`` prepared with ``deposit:true`` is a plain click (#1198 C1: no typed word) when its ``grant.issued`` carries
``label:"runner"``, at most 12 hours and ``verbs:"agent"`` and this machine has dashboard execution on for the
workspace. ``sign`` draws the secret, signs, submits and only after the commit hands the secret to the runner through
``grant.deposit``; the secret is never in a result, a frame to the dashboard, a record or the signer's log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from orch.click.protocol import ClickError
from orch.runner import enablement
from tests.host.dispatch_helpers import Served
from tests.host.human_helpers import Human, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_click import Click, refused
from tests.host.test_runner_socket import Runner

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


@pytest.fixture
def world(tmp_path):
    from tests.host.lifecycle_helpers import Lab

    lab = Lab(tmp_path)
    served = Served(lab, runner_identity=lambda cred: cred.pid == os.getpid())
    rstate = tmp_path / "rs"
    rstate.mkdir(mode=0o700)
    runner = Runner(served, rstate)
    click = Click(served)
    try:
        yield served, click, runner, Human(served)
    finally:
        click.close()
        runner.close()
        served.close()
        lab.close()


def execution(served, on: bool = True) -> None:
    enablement.set_enabled(Path(served.lab.state), served.wid, on, UID, terminals=False, text="t")


def flow(click, served, **args):
    p = click.prepare("grant", {"hours": 8, "scope": "workable", **args}, deposit=True)
    v = click.view(p["request_id"])
    click.mono.t += 1.5
    return p, v


def test_grant_without_deposit_stays_terminal_only(world):
    served, click, _, _ = world
    execution(served)
    refused(lambda: click.prepare("grant", {"hours": 8}), "human.click_refused", "terminal_only")
    refused(lambda: click.prepare("grant", {"hours": 8}, deposit=False), "human.click_refused", "terminal_only")
    refused(lambda: click.prepare("log", {"ref": "X-1", "text": "x"}, deposit=True), "invalid.input")


def test_deposit_needs_dashboard_execution_and_at_most_12_hours_and_no_other_label(world):
    served, click, _, _ = world
    refused(lambda: click.prepare("grant", {"hours": 8}, deposit=True), "human.click_refused", "terminal_only")
    execution(served)
    refused(lambda: click.prepare("grant", {"hours": 169}, deposit=True), "human.click_refused", "terminal_only")
    refused(lambda: click.prepare("grant", {"hours": 9}, deposit=True), "invalid.input")  # B8: above the setting (8)
    for bad in ({"label": "mine"}, {"verbs": "log"}, {"hours": "8"}, {"extra": 1}):
        refused(lambda bad=bad: click.prepare("grant", {"hours": 8, **bad}, deposit=True), "invalid.input")


def test_a_click_grant_goes_to_the_runner_and_never_to_the_dashboard(world):
    served, click, runner, _ = world
    execution(served)
    p, v = flow(click, served)
    assert p["confirm"] == "click"
    view = v["view"]
    assert view["deposit"] is True
    assert view["deposit_line"] == "The secret goes to your orch runner on this machine and is never shown"
    assert view["confirm"] == {"kind": "click"}
    assert view["deposit_line"] in view["lines"]
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] == "confirmed" and res["deposit_id"] == p["request_id"][3:]
    gid = res["grant"]
    assert [g["grant"] for g in runner.grants.report()] == [gid]
    assert res["result"]["data"]["grant"] == gid
    secret = runner.grants.secret_for(served.wid, served.lab.ws.owner.ref, gid)
    tail = secret.partition(".")[2]
    assert tail not in repr(res) and tail not in repr(v)
    for path in Path(served.lab.state).rglob("*"):  # records, the click log, the host's files
        if path.is_file() and not path.is_symlink():
            assert tail.encode() not in path.read_bytes(), path
    again = click.sign(p["request_id"], v["view_digest"])
    assert again["duplicate"] is True and again["deposit"] == "confirmed" and again["grant"] == gid
    me = ok(served.client().req("whoami", {}))["data"]["runner"]
    assert me["grant"]["id"] == gid


def test_a_deposit_grant_is_refused_at_sign_when_execution_is_switched_off_after_the_review(world):
    served, click, runner, _ = world
    execution(served)
    p, v = flow(click, served)
    click.mono.t += 2
    execution(served, False)
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "terminal_only")
    assert click.signatures == 0 and runner.grants.report() == []
    execution(served)
    res = click.sign(p["request_id"], v["view_digest"])  # nothing was claimed: the same request still signs
    assert res["deposit"] == "confirmed" and click.signatures == 1


def test_extend_is_one_signature_new_grant_and_revoke_old_then_the_deposit(world):
    """B8: Extend = issue + revoke in ONE prepared request, one click, the runner swaps after the commit."""
    served, click, runner, _ = world
    execution(served)
    p, v = flow(click, served)
    old = click.sign(p["request_id"], v["view_digest"])["grant"]
    p2, v2 = flow(click, served, extend=old)
    assert p2["confirm"] == "click" and v2["view"]["confirm"] == {"kind": "click"}
    assert any(old in line for line in v2["view"]["lines"])  # the revoke leg is in the review, in full
    res = click.sign(p2["request_id"], v2["view_digest"])
    assert res["deposit"] == "confirmed" and res["grant"] != old
    grants = served.service.store.state.workspace.grants
    assert grants[old].revoked and not grants[res["grant"]].revoked
    assert [g["grant"] for g in runner.grants.report()] == [res["grant"]]


def test_extend_is_all_or_nothing_and_only_for_your_own_live_runner_grant(world):
    served, click, runner, _ = world
    execution(served)
    p, v = flow(click, served)
    old = click.sign(p["request_id"], v["view_digest"])["grant"]
    refused(lambda: click.prepare("grant", {"hours": 8, "extend": "gr_" + "0" * 26}, deposit=True), "invalid.input")
    p2, v2 = flow(click, served, extend=old)
    pr = click.prepare("grant.revoke", {"grant": old})  # the old leg dies first, signed by its own request
    vr = click.view(pr["request_id"])
    click.mono.t += 1.5
    click.sign(pr["request_id"], vr["view_digest"])
    before = set(served.service.store.state.workspace.grants)
    with pytest.raises(Exception) as e:  # the whole request is refused (the revoke leg is judged with the issue)
        click.sign(p2["request_id"], v2["view_digest"])
    assert "already revoked" in str(e.value)
    assert set(served.service.store.state.workspace.grants) == before  # the new grant never landed


def test_the_view_has_no_deposit_field_for_any_other_op(world):
    served, click, _, human = world
    execution(served)
    _, key = human.ticket()
    p = click.prepare("log", {"ref": key, "text": "x"})
    view = click.view(p["request_id"])["view"]
    assert "deposit" not in view and "deposit_line" not in view


def test_a_lost_deposit_answer_is_unknown_and_the_status_is_asked_once(world, monkeypatch):
    served, click, runner, _ = world
    execution(served)
    seen = []

    def tamper(op, args, res):
        seen.append(op)
        if op == "grant.deposit":
            raise ConnectionError("lost")
        return res

    click.tamper = tamper
    p, v = flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] in ("unknown", "confirmed") and "grant" in res  # never a false failure
    assert seen.count("grant.deposit_status") == 1


def test_a_deposit_refused_by_the_host_is_rejected_with_the_grant_named(world):
    served, click, runner, _ = world
    execution(served)
    p, v = flow(click, served)
    runner.link.stop()  # the runner goes away between prepare and sign
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] in ("rejected", "unknown") and res["grant"].startswith("gr_")  # Revoke uses it
    assert res["result"]["data"]["grant"] == res["grant"]
    if res["deposit"] == "rejected":
        assert res["deposit_reason"] == "runner.unavailable"
    else:
        assert "deposit_reason" not in res


@pytest.mark.parametrize(
    ("host_reason", "expected"),
    [("grant.forgotten", "grant.forgotten"), ("grant.not_runner", "grant.not_runner"), ("grant.verbs", "grant.verbs"),
     ("grant.expiring", "grant.expiring"), ("rate.limited", "rate.limited"),
     ("runner.unavailable", "runner.unavailable"), ("runner_restarted", "other"),
     ("secret text from the host /Users/x", "other"), (None, "other")],
)  # fmt: skip
def test_a_rejected_deposit_carries_a_static_reason_from_the_closed_set(world, host_reason, expected):
    served, click, _, _ = world
    execution(served)

    def tamper(op, args, res):
        if op == "grant.deposit":
            res["data"] = {"deposit": "rejected", **({"reason": host_reason} if host_reason else {})}
        return res

    click.tamper = tamper
    p, v = flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] == "rejected" and res["deposit_reason"] == expected


def test_a_confirmed_or_unknown_deposit_has_no_reason(world):
    served, click, _, _ = world
    execution(served)
    p, v = flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] == "confirmed" and "deposit_reason" not in res


def test_check_prepared_refuses_a_grant_issued_without_the_runner_label():
    from orch.click import view as views

    with pytest.raises(ClickError) as e:
        views.deposit_checks({"op": "grant", "events": [{"type": "grant.issued", "label": "x", "hours": 8,
                                                         "verbs": "agent"}]})  # fmt: skip
    assert getattr(e.value, "reason", None) == "terminal_only"
    for bad in ({"hours": 169}, {"verbs": ["log"]}, {"label": None}):
        ev = {"type": "grant.issued", "label": "runner", "hours": 8, "verbs": "agent", **bad}
        with pytest.raises(ClickError):
            views.deposit_checks({"op": "grant", "events": [ev]})
    views.deposit_checks({"op": "grant", "events": [{"type": "grant.issued", "label": "runner", "hours": 168,
                                                     "verbs": "agent"}]})  # fmt: skip


def test_any_failure_of_the_hand_over_after_the_commit_is_unknown_never_internal(world):
    """Opus info: a non-ClickError (a framing error from the host client) after the commit reports unknown."""
    from orch.host.frames import FrameError

    served, click, _, _ = world
    execution(served)

    def tamper(op, args, res):
        if op in ("grant.deposit", "grant.deposit_status"):
            raise FrameError("frame.invalid")
        return res

    click.tamper = tamper
    p, v = flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] == "unknown" and res["grant"].startswith("gr_")


def test_a_click_deposit_grant_has_no_set_orch_grant_hint(world):
    served, click, _, _ = world
    execution(served)
    p, v = flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"])
    assert "ORCH_GRANT" not in repr(res["result"].get("hints")) + repr(res["result"].get("lines"))


@pytest.mark.parametrize(
    ("host_reason", "expected"),
    [("grant.expiring", "grant.expiring"), ("runner_restarted", "other"), ("host text /Users/x", "other"),
     (None, "other")],
)  # fmt: skip
def test_the_status_fallback_maps_a_reason_to_the_closed_set(world, host_reason, expected):
    """A lost hand-over answer (or a repeated sign) reads ``grant.deposit_status``; its reason is mapped the same."""
    served, click, _, _ = world
    execution(served)

    def tamper(op, args, res):
        if op == "grant.deposit":
            raise ConnectionError("lost")
        if op == "grant.deposit_status":
            res["data"] = {"deposit": "rejected", **({"reason": host_reason} if host_reason else {})}
        return res

    click.tamper = tamper
    p, v = flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["deposit"] == "rejected" and res["deposit_reason"] == expected
    again = click.sign(p["request_id"], v["view_digest"])  # a repeated sign: the settled answer
    assert again["deposit"] == "rejected" and again["deposit_reason"] == expected


def test_the_contract_rule_is_three_and_documented():
    from orch.click.protocol import CONTRACT, DEPOSIT_REASONS

    assert CONTRACT == 3
    assert DEPOSIT_REASONS == {"grant.forgotten", "grant.not_runner", "grant.verbs", "grant.expiring", "rate.limited",
                               "runner.unavailable", "other"}  # fmt: skip
