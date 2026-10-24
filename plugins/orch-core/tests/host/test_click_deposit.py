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


def test_deposit_is_for_grant_only(world):
    served, click, _, _ = world
    execution(served)
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


# -- #1198 C7: a grant without deposit shows its secret once in the page, sealed to the page key ------------
class Page:
    """The browser's side of the sealed field: a fresh key per request, sent at prepare, bound to the request once it
    exists (the page learns the request id from prepare)."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec

        from orch.click import sealed

        self.sealed, self.priv = sealed, ec.generate_private_key(ec.SECP256R1())
        self.public = sealed.encode_public(self.priv.public_key())
        self.key = None

    def bind(self, served, request_id: str) -> Page:
        self.wid, self.rid = served.wid, request_id
        self.key = self.sealed.Recipient._for_vectors("out", served.wid, request_id, self.priv)
        return self

    def open(self, res: dict, view_digest: str) -> str:
        assert res["reveal"]["purpose"] == "grant.secret"
        ctx = self.sealed.Context("grant.secret", self.wid, self.rid, view_digest)
        return bytes(self.key.open(res["reveal"]["envelope"], ctx)).decode()


def reveal_flow(click, served, page=None, **args):
    page = page or Page()
    p = click.prepare("grant", {"hours": 2, "scope": "workable", **args}, reveal_key=page.public)
    v = click.view(p["request_id"])
    click.mono.t += 1.5
    return p, v, page.bind(served, p["request_id"])


def test_reveal_a_grant_secret_is_sealed_to_the_page_and_nowhere_else(world):
    served, click, runner, _ = world
    p, v, page = reveal_flow(click, served, label="codex")  # no dashboard execution needed: nothing goes to a runner
    view = v["view"]
    assert p["confirm"] == "click" and view["confirm"] == {"kind": "click"}
    from orch.click import protocol
    from orch.click import view as cview

    fp = cview.key_fingerprint(page.public)
    assert view["seal"] == {"v": 1, "alg": "P256-HKDF-SHA256-A256GCM", "purposes": ["grant.secret"], "for": fp}
    assert protocol.REVEAL_RISK in view["lines"] and protocol.REVEAL_LINE in view["lines"] and "deposit" not in view
    res = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    secret = page.open(res, v["view_digest"])
    gid, _, tail = secret.partition(".")
    assert res["result"]["data"]["grant"] == gid and tail and "deposit" not in res
    assert tail not in repr({k: x for k, x in res.items() if k != "reveal"}) and tail not in repr(v)
    grant = served.service.store.state.workspace.grants[gid]
    assert not grant.revoked and grant.label == "codex" and runner.grants.report() == []
    for path in Path(served.lab.state).rglob("*"):  # records, the click log, the host's files
        if path.is_file() and not path.is_symlink():
            assert tail.encode() not in path.read_bytes(), path
    with pytest.raises(page.sealed.SealedError) as info:  # shown once: the same envelope opens once per page key
        page.open(res, v["view_digest"])
    assert info.value.reason == "replay"


def test_reveal_key_is_part_of_the_review_and_a_swapped_key_is_refused(world):
    served, click, _, _ = world
    p, v, page = reveal_flow(click, served)
    thief = Page()
    refused(lambda: click.sign(p["request_id"], v["view_digest"], reveal_key=thief.public), "invalid.input")
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "invalid.input")  # none at sign either
    assert click.signatures == 0
    # the same request prepared again (an idem retry) under another key is not the same request
    p2, v2, page2 = reveal_flow(click, served)
    assert v2["view_digest"] != v["view_digest"]  # a different key is a different review
    res = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    assert page.open(res, v["view_digest"])


def test_reveal_keys_are_checked_at_prepare_and_at_sign(world):
    served, click, _, _ = world
    refused(lambda: click.prepare("grant", {"hours": 2}), "invalid.input")  # a shown grant must bring its key
    for bad in (5, "", "AAAA", "A" * 87):
        refused(lambda bad=bad: click.prepare("grant", {"hours": 2}, reveal_key=bad), "invalid.input")
    p, v, page = reveal_flow(click, served)
    refused(
        lambda: click.sign(p["request_id"], v["view_digest"], reveal_key=page.public, sealed={}), "invalid.input"
    )  # a review that asks for no sealed value takes none
    assert click.signatures == 0
    res = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)  # the same request still signs
    assert page.open(res, v["view_digest"]) and click.signatures == 1


def test_reveal_key_is_refused_on_a_request_that_has_no_secret(world):
    served, click, _, _ = world
    execution(served)
    page = Page()
    refused(lambda: click.prepare("grant", {"hours": 8}, deposit=True, reveal_key=page.public), "invalid.input")
    p, v = flow(click, served)  # a deposit grant
    refused(lambda: click.sign(p["request_id"], v["view_digest"], reveal_key=page.public), "invalid.input")
    assert click.signatures == 0
    res = click.sign(p["request_id"], v["view_digest"])
    assert "reveal" not in res and res["deposit"] == "confirmed"


def test_reveal_is_bound_to_the_workspace_request_and_review(world):
    from orch.click import sealed

    served, click, _, _ = world
    p, v, page = reveal_flow(click, served)
    res = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    env = res["reveal"]["envelope"]
    wid, rid, vd = served.wid, p["request_id"], v["view_digest"]
    other_req = "hr_" + ("0" * 25) + "1"
    other_ws = "0" * 31 + "1"
    other_vd = "sha256:" + "0" * 64
    # a page key made for another request or workspace refuses before it decrypts
    for kw in ({"workspace_id": other_ws, "request_id": rid}, {"workspace_id": wid, "request_id": other_req}):
        stray = sealed.Recipient._for_vectors("out", kw["workspace_id"], kw["request_id"], page.priv)
        with pytest.raises(sealed.SealedError) as info:
            stray.open(env, sealed.Context("grant.secret", wid, rid, vd))
        assert info.value.reason == "context"
    # the right private key with other facts does not authenticate (the envelope names nothing, the opener supplies it)
    for ctx in (
        sealed.Context("grant.secret", other_ws, rid, vd),
        sealed.Context("grant.secret", wid, other_req, vd),
        sealed.Context("grant.secret", wid, rid, other_vd),
    ):
        fresh = sealed.Recipient._for_vectors("out", ctx.workspace_id, ctx.request_id, page.priv)
        with pytest.raises(sealed.SealedError) as info:
            fresh.open(env, ctx)
        assert info.value.reason == "open_failed"
    thief = Page().bind(served, rid)  # another page's key
    with pytest.raises(sealed.SealedError) as info:
        thief.key.open(env, sealed.Context("grant.secret", wid, rid, vd))
    assert info.value.reason == "open_failed"
    assert page.open(res, vd)  # and the right one still does


def test_reveal_key_is_used_for_one_request_only(world):
    served, click, _, _ = world
    p, v, page = reveal_flow(click, served)
    click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    refused(lambda: reveal_flow(click, served, page=page), "invalid.input")  # a used key is not accepted again
    assert click.signatures == 1


def test_reveal_a_repeated_sign_gives_the_same_envelope_to_the_same_key_and_signs_nothing_new(world):
    served, click, _, _ = world
    p, v, page = reveal_flow(click, served)
    first = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    again = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    assert again["duplicate"] is True and again["reveal"] == first["reveal"] and click.signatures == 1
    refused(lambda: click.sign(p["request_id"], v["view_digest"], reveal_key=Page().public), "invalid.input")


def test_reveal_after_a_signer_restart_has_no_secret_to_give(world):
    served, click, _, _ = world
    p, v, page = reveal_flow(click, served)
    click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    click.signer.approvals.clear()  # a restart: the approval is gone, the record stays
    res = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    assert res["duplicate"] is True and "reveal" not in res


def test_reveal_grant_is_limited_to_a_plain_agent_grant(world):
    served, click, _, _ = world
    key = Page().public
    refused(lambda: click.prepare("grant", {"hours": 169}, reveal_key=key), "human.click_refused", "terminal_only")
    for bad in ({"runner": True}, {"extend": "gr_" + "0" * 26}, {"verbs": "log"}, {"label": "runner"}, {"label": ""},
                {"hours": "8"}, {"scope": "x"}, {"label": "x" * 41}):  # fmt: skip
        refused(lambda bad=bad: click.prepare("grant", {"hours": 8, **bad}, reveal_key=Page().public), "invalid.input")
    refused(
        lambda: click.prepare("grant", {"hours": 8}, reveal_key=key, sequence={"step": 0, "intent": {}}),
        "invalid.input",
    )


def test_reveal_hours_and_scope_are_the_reviewed_ones(world):
    served, click, _, _ = world
    p, v, page = reveal_flow(click, served, hours=5, scope="all")
    assert any("5" in line for line in v["view"]["lines"]) and any("all" in line for line in v["view"]["lines"])
    other = click.view(p["request_id"])
    assert other["view_digest"] == v["view_digest"]  # stable across views
    refused(
        lambda: click.sign(p["request_id"], "sha256:" + "0" * 64, reveal_key=page.public),
        "human.click_refused",
        "view_changed",
    )
    assert click.signatures == 0


def test_reveal_fingerprint_is_the_full_sha256_and_matches_the_shared_golden_vectors():
    import json

    from orch.click import view as cview

    vectors = json.loads((Path(__file__).parents[1] / "click_reveal_fingerprint.json").read_text())
    assert vectors["cases"]
    for case in vectors["cases"]:
        assert len(case["fingerprint"]) == 64 and cview.key_fingerprint(case["key"]) == case["fingerprint"]


def test_reveal_one_key_reserves_one_request_at_prepare(world):
    served, click, _, _ = world
    page = Page()
    p, v, page = reveal_flow(click, served, page=page)
    # a second request prepared with the same key is refused at prepare (and cancelled at the host): never two seals
    refused(lambda: click.prepare("grant", {"hours": 3, "scope": "workable"}, reveal_key=page.public), "invalid.input")
    res = click.sign(p["request_id"], v["view_digest"], reveal_key=page.public)
    assert page.open(res, v["view_digest"]) and click.signatures == 1
    refused(lambda: click.prepare("grant", {"hours": 4, "scope": "workable"}, reveal_key=page.public), "invalid.input")


def test_reveal_seal_refuses_a_key_reserved_for_another_request(world):
    from orch.click.protocol import ClickError

    served, click, _, _ = world
    p, v, page = reveal_flow(click, served)
    approval = click.signer.approvals[(served.wid, p["request_id"])]
    click.signer._reveal_keys[page.public] = (served.wid, "hr_" + "0" * 25 + "1")  # as if another request held it
    with pytest.raises(ClickError):
        click.signer._seal(approval, page.public, "gr_x.y")
    assert approval.reveal_env is None
