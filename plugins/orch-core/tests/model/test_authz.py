# ruff: noqa: E731
"""§5.2, §5.3, §5.11: who may append what, and what an agent may do on behalf of whom."""

from datetime import timedelta

import pytest

from orch.model import Code
from orch.model.testing import FakeVerifier
from tests.model.world import World, refused, stamp
from tests.schema.examples import cert, hex32, revocation


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member", "vera": "viewer"})


# ---- agents: grant x person x visibility
def restricted(w):
    uid = w.ticket()
    w.tev(uid, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"]]})
    return uid


@pytest.mark.parametrize("scope", ["all", "workable"])
def test_agent_of_a_person_without_visibility_is_refused(w, scope):
    uid = restricted(w)
    g = w.grant("mara", scope) if scope == "all" else w.grant("mara", "workable")
    a = w.agent("mara", g)
    assert refused(w, uid, "claim.taken", a) == Code.GRANT_SCOPE
    assert refused(w, uid, "log.added", a, text="x") == Code.GRANT_SCOPE
    assert (
        refused(w, uid, "ticket.updated", a, base_rev={"ticket.title": "sha256:" + "0" * 64}, set={"ticket.title": "t"})
        == Code.GRANT_SCOPE
    )


def test_agent_of_a_person_with_visibility_works_on_a_restricted_ticket(w):
    uid = restricted(w)
    a = w.agent("sev", w.grant("sev", "all"))
    assert refused(w, uid, "claim.taken", a) is None


def test_readers_get_only_what_a_person_may_see(w):
    uid = restricted(w)
    s = w.state()
    assert uid in s.visible_to(w.people["sev"]) and uid not in s.visible_to(w.people["mara"])


def test_unattended_only_on_workspace_visible_tickets(w):
    uid = restricted(w)
    assert refused(w, uid, "log.added", w.unattended(), text="x") == Code.UNATTENDED_DENIED
    open_ = w.ticket()
    assert refused(w, open_, "log.added", w.unattended(), text="x") is None


def test_persons_do_not_write_to_tickets_they_cannot_see(w):
    uid = restricted(w)
    assert refused(w, uid, "log.added", "mara", text="x") == Code.TICKET_NOT_VISIBLE
    assert refused(w, uid, "visibility.changed", "mara", visibility="workspace") is None  # management stays possible


# ---- grants
def test_expired_grant_is_refused(w):
    uid = w.ticket()
    a = w.agent("sev", w.grant("sev", hours=1))
    w.clock += timedelta(hours=2)
    assert refused(w, uid, "claim.taken", a, at=stamp(w.clock)) == Code.GRANT_INVALID


def test_revoked_grant_is_refused(w):
    uid = w.ticket()
    g = w.grant("sev")
    w.wev("grant.revoked", "sev", grant=g)
    assert refused(w, uid, "claim.taken", w.agent("sev", g)) == Code.GRANT_INVALID


def test_grant_of_another_person_or_unknown_is_refused(w):
    uid = w.ticket()
    g = w.grant("mara")
    forged = w.agent("sev", g)  # `for` is not the person who issued it
    assert refused(w, uid, "claim.taken", forged) == Code.GRANT_INVALID
    assert refused(w, uid, "claim.taken", w.agent("sev", "gr_01J9ZK0000000000000000QQQQ")) == Code.GRANT_INVALID


def test_grant_verbs_must_cover_the_event(w):
    uid = w.ticket()
    g = w.grant("sev", verbs=["log.added"])
    a = w.agent("sev", g)
    assert refused(w, uid, "log.added", a, text="x") is None
    assert refused(w, uid, "claim.taken", a) == Code.GRANT_VERB


def test_grant_of_a_removed_member_ends(w):
    uid = w.ticket()
    g = w.grant("tom", "workable")
    w.wev("member.removed", "sev", person=w.people["tom"])
    assert refused(w, uid, "log.added", w.agent("tom", g), text="x") == Code.GRANT_INVALID


def test_demoted_member_cannot_use_an_all_scope_grant(w):
    uid = w.ticket()
    g = w.grant("mara", "all")
    w.wev("role.changed", "sev", person=w.people["mara"], role="member")
    assert refused(w, uid, "log.added", w.agent("mara", g), text="x") == Code.GRANT_SCOPE


def test_d60_grant_terms(w):
    t = lambda who, **kw: refused(
        w,
        "workspace",
        "grant.issued",
        who,
        grant="gr_01J9ZK0000000000000000AAAA",
        secret_hash="sha256:" + "1" * 64,
        **kw,
    )
    now = w.at()
    from datetime import UTC, datetime

    base = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def terms(scope, hours):
        at = stamp(base + timedelta(seconds=10))
        return {
            "scope": scope,
            "verbs": "agent",
            "issued_at": at,
            "hours": hours,
            "expires_at": stamp(base + timedelta(seconds=10, hours=hours)),
            "at": at,
        }

    assert t("tom", **terms("all", 1)) == Code.GRANT_TERMS
    assert t("tom", **terms("workable", 9)) == Code.GRANT_TERMS
    assert t("tom", **terms("workable", 8)) is None
    assert t("vera", **terms("workable", 1)) == Code.ROLE_DENIED  # viewers write nothing
    assert t("mara", **terms("all", 24)) is None


# ---- persons: certificates, scopes, roster_v, signatures
def test_member_event_needs_operate(w):
    w.member("ned", "member")  # fine for an owner device with operate
    w2 = World().bootstrap()
    w2.add_person("lowop")
    w2.wev(
        "member.added",
        "sev",
        person=w2.people["lowop"],
        name="lowop",
        role="owner",
        pk_pub=w2.ws[0]["owner"]["pk_pub"],
        device_cert=cert(w2.people["lowop"][2:], w2.dev["lowop"][2:], scopes=("look", "decide")),
    )
    for typ, kw in (
        (
            "member.added",
            {
                "person": "p_" + hex32("x"),
                "name": "x",
                "role": "member",
                "pk_pub": w2.ws[0]["owner"]["pk_pub"],
                "device_cert": cert(hex32("x"), hex32("dx")),
            },
        ),
        ("settings.changed", {"set": {"grant_hours": 4}}),
        ("policy.changed", {"gates": {}}),
    ):
        assert refused(w2, "workspace", typ, "lowop", **kw) == Code.DEVICE_SCOPE, typ


def test_look_only_and_drop_scoped_certificates_never_enter(w):
    pk = w.ws[0]["owner"]["pk_pub"]
    for scopes in (("look",), ("drop:" + hex32("d"),)):
        c = cert(hex32("q"), hex32("dq"), scopes=scopes)
        assert (
            refused(
                w,
                "workspace",
                "member.added",
                "sev",
                person="p_" + hex32("q"),
                name="q",
                role="member",
                pk_pub=pk,
                device_cert=c,
            )
            == Code.DEVICE_SCOPE
        )
        assert (
            refused(
                w,
                "workspace",
                "device.added",
                "tom",
                device="d_" + hex32("dq"),
                cert=cert(w.people["tom"][2:], hex32("dq"), scopes=scopes),
            )
            == Code.DEVICE_SCOPE
        )


def test_stale_roster_v_is_refused(w):
    uid = w.ticket()
    assert refused(w, uid, "log.added", "sev", text="x", roster_v=1) == Code.MEMBERS_STALE
    assert refused(w, uid, "log.added", "sev", text="x") is None


def test_removed_and_revoked_devices_stop_signing(w):
    uid = w.ticket()
    w.wev("device.removed", "tom", device=w.dev["tom"])
    assert refused(w, uid, "log.added", "tom", text="x") == Code.DEVICE_INVALID
    w.wev(
        "device.revoked",
        "sev",
        device=w.dev["mara"],
        reason="lost",
        revocation=revocation(w.people["mara"][2:], w.dev["mara"][2:], "lost"),
    )
    assert refused(w, uid, "log.added", "mara", text="x") == Code.DEVICE_INVALID


def test_revoked_by_host_or_other_member_but_reason_must_match(w):
    rev = revocation(w.people["tom"][2:], w.dev["tom"][2:], "lost")
    assert refused(w, "workspace", "device.revoked", w.HOST, device=w.dev["tom"], reason="lost", revocation=rev) is None
    assert refused(w, "workspace", "device.revoked", "mara", device=w.dev["tom"], reason="lost", revocation=rev) is None
    # the schema refuses a reason mismatch before the model sees it
    w2 = World(validate=True).bootstrap({"tom": "member"})
    with pytest.raises(Exception, match="reason"):
        w2.wev(
            "device.revoked",
            "sev",
            device=w2.dev["tom"],
            reason="compromised",
            revocation=revocation(w2.people["tom"][2:], w2.dev["tom"][2:], "lost"),
        )


def test_device_added_by_an_existing_device_only_and_recovery_by_the_new_one(w):
    newdev = hex32("newdev")
    c = cert(w.people["tom"][2:], newdev)
    # signed by the new device itself while tom still has a valid device: refused
    new_actor = {"kind": "person", "id": w.people["tom"], "device": "d_" + newdev}
    assert refused(w, "workspace", "device.added", new_actor, device="d_" + newdev, cert=c) == Code.DEVICE_UNKNOWN
    assert refused(w, "workspace", "device.added", "tom", device="d_" + newdev, cert=c) is None
    w.wev(
        "device.revoked",
        w.HOST,
        device=w.dev["tom"],
        reason="lost",
        revocation=revocation(w.people["tom"][2:], w.dev["tom"][2:], "lost"),
    )
    assert refused(w, "workspace", "device.added", new_actor, device="d_" + newdev, cert=c) is None  # D50 recovery


def test_bad_person_signature_and_bad_embedded_object(w):
    uid = w.ticket()
    e = w.build_unappended(uid, "log.added", "sev", text="x")
    w.verifier.bad_person.add(e["id"])
    assert refused(w, uid, "log.added", "sev", text="x", id=e["id"]) == Code.SIG_INVALID
    n = w.build_unappended(
        "workspace",
        "member.added",
        "sev",
        person="p_" + hex32("n"),
        name="n",
        role="member",
        pk_pub=w.ws[0]["owner"]["pk_pub"],
        device_cert=cert(hex32("n"), hex32("dn")),
    )
    w.verifier.bad_embedded.add(n["id"])
    assert (
        refused(
            w,
            "workspace",
            "member.added",
            "sev",
            id=n["id"],
            person="p_" + hex32("n"),
            name="n",
            role="member",
            pk_pub=w.ws[0]["owner"]["pk_pub"],
            device_cert=cert(hex32("n"), hex32("dn")),
        )
        == Code.DEVICE_CERT
    )


def test_actor_kind_table(w):
    uid = w.ticket()
    g = w.grant("sev")
    a = w.agent("sev", g)
    assert (
        refused(
            w,
            uid,
            "gate.approved",
            a,
            gate="plan",
            gate_gen=0,
            hash="sha256:" + "1" * 64,
            policy_hash="sha256:" + "2" * 64,
        )
        == Code.EVENT_BAD_ACTOR
    )
    assert refused(w, uid, "ticket.closed", a, resolution="other") == Code.EVENT_BAD_ACTOR
    assert (
        refused(
            w,
            uid,
            "branch.pushed",
            "sev",
            repo_name="r",
            repo_id="local:r",
            ref="refs/heads/x",
            sha="a" * 40,
            before=None,
        )
        == Code.EVENT_BAD_ACTOR
    )
    assert refused(w, "workspace", "settings.changed", a, set={"grant_hours": 3}) == Code.EVENT_BAD_ACTOR
    assert refused(w, uid, "claim.taken", "sev") == Code.EVENT_BAD_ACTOR
    assert refused(w, uid, "task.started", w.unattended(), task="T1") == Code.EVENT_BAD_ACTOR
    assert (
        refused(
            w,
            uid,
            "ticket.updated",
            w.unattended(),
            base_rev={"ticket.title": "sha256:" + "0" * 64},
            set={"ticket.title": "t"},
        )
        == Code.EVENT_BAD_ACTOR
    )


def test_owner_only_events(w):
    assert refused(w, "workspace", "settings.changed", "mara", set={"grant_hours": 3}) == Code.ROLE_DENIED
    assert refused(w, "workspace", "role.changed", "mara", person=w.people["tom"], role="owner") == Code.ROLE_DENIED
    assert refused(w, "workspace", "policy.changed", "mara", gates={}) == Code.ROLE_DENIED
    assert (
        refused(
            w,
            "workspace",
            "member.added",
            "mara",
            person="p_" + hex32("o"),
            name="o",
            role="owner",
            pk_pub=w.ws[0]["owner"]["pk_pub"],
            device_cert=cert(hex32("o"), hex32("do")),
        )
        == Code.ROLE_DENIED
    )
    assert (
        refused(w, "workspace", "member.removed", "mara", person=w.people["tom"]) is None
    )  # maintainers remove members
    assert refused(w, "workspace", "member.removed", "mara", person=w.people["sev"]) == Code.ROLE_DENIED
    assert refused(w, "workspace", "member.removed", "sev", person=w.people["sev"]) == Code.MEMBERS_LAST_OWNER


# ---- invalid events and the decision freeze
def test_invalid_event_is_absent_reported_and_freezes_decisions_until_acknowledged(w):
    uid = w.ticket()
    w.fill(uid)
    bad = w.tev(uid, "ticket.closed", "tom", resolution="other")  # a member may not close someone else's ticket
    s = w.state()
    assert s.tickets[uid].status == "open"
    assert s.tickets[uid].frozen and any(n.kind == "acknowledge" for n in s.tickets[uid].needs)
    # `validate` is on, so the freeze comes from a refused decision below
    assert w.decide(uid, "sev", "requirements", try_=True)[1].code == Code.FREEZE_ACTIVE
    from orch import canon

    head = canon.event_head(bad)
    assert (
        refused(w, uid, "invalid.acknowledged", "mara", invalid_seq=bad["seq"], invalid_head=head) == Code.ROLE_DENIED
    )
    assert (
        refused(w, uid, "invalid.acknowledged", "sev", invalid_seq=bad["seq"], invalid_head="sha256:" + "0" * 64)
        == Code.ACK_UNKNOWN
    )
    w.tev(uid, "invalid.acknowledged", "sev", invalid_seq=bad["seq"], invalid_head=head)
    s = w.state()
    assert not s.tickets[uid].frozen and s.tickets[uid].status == "open"  # still absent
    assert w.decide(uid, "sev", "requirements", try_=True)[1].__class__.__name__ == "Ok"


def test_invalid_workspace_event_freezes_all_decisions(w):
    uid = w.ticket()
    w.fill(uid)
    bad = w.wev("settings.changed", "mara", set={"grant_hours": 2})
    assert w.state().workspace.frozen
    assert w.decide(uid, "sev", "requirements", try_=True)[1].code == Code.FREEZE_ACTIVE
    from orch import canon

    w.wev("invalid.acknowledged", "sev", invalid_seq=bad["seq"], invalid_head=canon.event_head(bad))
    assert not w.state().workspace.frozen
    assert w.state().workspace.settings["grant_hours"] == 8


def test_chain_break_stops_the_log():
    v = FakeVerifier()
    w = World(v).bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    e = w.tev(uid, "log.added", "sev", text="x")
    v.bad_host.add(e["id"])
    w.tev(uid, "log.added", "sev", text="y")
    s = w.state()
    assert [c.log for c in s.chain_errors] == [uid]
    assert w.state().tickets[uid].frozen is False
    assert refused(w, uid, "log.added", "sev", text="z") == Code.CHAIN_BROKEN


def test_genesis_pin(w):
    from orch import canon
    from orch.model import replay

    ok = replay(
        w.ws,
        w.tl,
        verifier=w.verifier,
        now=w.at(),
        expected_workspace_id=w.workspace_id,
        expected_genesis=canon.event_head(w.ws[0]),
    )
    assert not ok.workspace.invalid and ok.workspace.members
    bad = replay(
        w.ws,
        w.tl,
        verifier=w.verifier,
        now=w.at(),
        expected_workspace_id=w.workspace_id,
        expected_genesis="sha256:" + "0" * 64,
    )
    assert not bad.workspace.members and "pinned" in bad.chain_errors[0].detail
    assert not bad.tickets  # nothing after an untrusted genesis counts
    # a genesis for another workspace id is refused before the verifier ever sees it
    v = FakeVerifier()
    other = replay(w.ws, w.tl, verifier=v, now=w.at(), expected_workspace_id="f" * 32)
    assert not other.workspace.members and v.host_calls == []
    # the workspace id handed to verify_host is the pinned/replayed one, never the event's
    v2 = FakeVerifier()
    replay(w.ws, w.tl, verifier=v2, now=w.at(), expected_workspace_id=w.workspace_id)
    assert set(v2.host_workspace_ids) == {w.workspace_id}


def test_device_revoked_gets_the_roster_certificate(w):
    rev = revocation(w.people["tom"][2:], w.dev["tom"][2:], "lost")
    w.wev("device.revoked", w.HOST, device=w.dev["tom"], reason="lost", revocation=rev)
    w.state()
    (_, cert_given) = [c for c in w.verifier.embedded_certs if c[0] == w.ws[-1]["id"]][0]
    assert cert_given["o"]["device_id"] == w.dev["tom"][2:]


def test_unattended_quotas(w):
    uid = w.ticket()
    s = w.unattended("s_01J9ZK0000000000000000SSSS")
    for _ in range(30):
        w.tev(uid, "log.added", s, text="x")
    assert refused(w, uid, "log.added", s, text="x") == Code.QUOTA_UNATTENDED
    assert refused(w, uid, "log.added", w.unattended("s_01J9ZK0000000000000000TTTT"), text="x") is None
    w.clock += timedelta(hours=2)
    assert refused(w, uid, "log.added", s, text="x", at=stamp(w.clock)) is None


def test_verify_host_always_gets_the_log_and_the_workspace_key_after_genesis():
    import base64

    v = FakeVerifier()
    w = World(v).bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    w.tev(uid, "log.added", "sev", text="x")
    v.host_calls.clear()
    w.state()
    wsk = base64.urlsafe_b64decode(w.ws[0]["wsk_pub"] + "=" * (-len(w.ws[0]["wsk_pub"]) % 4))
    by_id = {e["id"]: e for e in [*w.ws, *w.tl[uid]]}
    assert len(v.host_calls) == len(by_id)
    for eid, log, key in v.host_calls:
        assert log == ("workspace" if eid in {e["id"] for e in w.ws} else uid)
        assert key is None if eid == w.ws[0]["id"] else key == wsk
    # admit (pre-append, no host_sig yet) never asks for a host signature
    v.host_calls.clear()
    refused(w, uid, "log.added", "sev", text="y")
    assert v.host_calls == [] or all(c[0] in by_id for c in v.host_calls)


# ---- background checks 1/2/3 on the verifier seam
def test_embedded_objects_are_checked_under_the_key_the_model_vouches_for():
    from tests.schema.examples import pub

    v = FakeVerifier()
    w = World(v).bootstrap({"mara": "maintainer"})
    newdev = hex32("nd")
    w.wev("device.added", "mara", device="d_" + newdev, cert=cert(w.people["mara"][2:], newdev))
    w.wev(
        "device.revoked",
        w.HOST,
        device=w.dev["mara"],
        reason="lost",
        revocation=revocation(w.people["mara"][2:], w.dev["mara"][2:], "lost"),
    )
    w.state()
    keys = dict((i, k) for i, k in v.embedded_keys)
    by_type = {e["type"]: e["id"] for e in w.ws}
    assert keys[by_type["workspace.created"]] == pub("pk-sev")
    assert keys[by_type["member.added"]] == pub("pk-mara")
    assert keys[by_type["device.added"]] == pub("pk-mara")  # the member's key, not something the event says
    assert keys[by_type["device.revoked"]] == pub("pk-mara")


def test_a_second_workspace_created_is_not_checked_under_its_own_key():
    v = FakeVerifier()
    w = World(v, validate=False).bootstrap()
    second = {
        **w.ws[0],
        "seq": 2,
        "id": "01J9ZK0000000000000000ZZZZ",
        "prev": __import__("orch").canon.event_head(w.ws[0]),
    }
    second["based_on"] = second["prev"]
    w.ws.append(second)
    v.host_calls.clear()
    s = w.state()
    ((eid, log, key),) = [c for c in v.host_calls if c[0] == second["id"]]
    assert key is not None  # the workspace key of the first genesis, not None
    assert [i.code for i in s.workspace.invalid] == ["genesis.invalid"] or s.chain_errors


def test_events_without_signatures_are_refused_not_skipped():
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    e = w.tev(uid, "log.added", "sev", text="x")
    nosig = {k: v for k, v in e.items() if k != "sig"}
    w.tl[uid][-1] = nosig
    s = w.state()
    assert [i.code for i in s._core.logs[uid].invalid] == ["sig.invalid"]
    w2 = World(validate=False).bootstrap()
    nohost = {k: v for k, v in w2.ws[0].items() if k != "host_sig"}
    w2.ws[0] = nohost
    assert w2.state().chain_errors


def test_verifier_exceptions_are_not_success():
    class Boom(FakeVerifier):
        def verify_person(self, event, context):
            raise RuntimeError("backend down")

    w = World(FakeVerifier())
    w.bootstrap({"mara": "maintainer"})
    uid = w.ticket()
    w.tev(uid, "log.added", "sev", text="x")
    with pytest.raises(RuntimeError):
        from orch.model import replay

        replay(w.ws, w.tl, verifier=Boom(), now=w.at(), expected_workspace_id=w.workspace_id)


def test_the_fake_verifier_is_not_part_of_the_public_api():
    import orch.model

    assert not hasattr(orch.model, "FakeVerifier")


def test_replay_and_admit_verify_the_same_person_signature(w):
    uid = w.ticket()
    e = w.build_unappended(uid, "log.added", "sev", text="x")
    w.verifier.bad_person.add(e["id"])
    from orch.model import admit

    assert admit(w.state(), e, log=uid).code == Code.SIG_INVALID
    e["host_sig"] = "x"
    w.tl[uid].append(e)
    assert [i.code for i in w.state()._core.logs[uid].invalid] == ["sig.invalid"]


def test_status_transitions_are_person_gated_and_workers_do_not_change_them(w):
    uid = w.ticket("tom")
    g = w.grant("tom", "workable")
    a = w.agent("tom", g)
    assert refused(w, uid, "ticket.closed", a, resolution="other") == Code.EVENT_BAD_ACTOR
    assert refused(w, uid, "ticket.reopened", a) == Code.EVENT_BAD_ACTOR
    other = w.ticket("sev")
    assert refused(w, other, "ticket.closed", "tom", resolution="other") == Code.ROLE_DENIED
    w.tev(other, "ticket.closed", "sev", resolution="other")
    assert refused(w, other, "ticket.reopened", "tom") == Code.ROLE_DENIED
    assert refused(w, other, "ticket.reopened", "mara") is None
    # being a worker (agent for tom) does not make tom a manager of someone else's ticket
    w.tev(other, "ticket.reopened", "sev")
    assert refused(w, other, "ticket.closed", "tom", resolution="other") == Code.ROLE_DENIED


def test_a_genesis_must_be_seq_1_in_replay_and_in_admit():
    """The trust root does not rely on the schema alone (store review): a workspace.created that is not seq 1 is never
    trusted, even when an earlier (refused) event of the log leaves the workspace without a genesis."""
    from orch import canon
    from orch.model import WORKSPACE, admit, replay

    w = World(validate=False).bootstrap()
    genesis = w.ws[0]
    junk = {**w.wev("settings.changed", "sev", set={"grant_hours": 4}), "seq": 1, "prev": None, "based_on": None}
    late = {**genesis, "seq": 2, "id": "01J9ZK0000000000000000ZZZY", "prev": canon.event_head(junk)}
    late["based_on"] = late["prev"]
    s = replay([junk, late], {}, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)
    assert not s.workspace.members and s.workspace.genesis is None
    empty = replay([], {}, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)
    first_try = {**genesis, "seq": 2, "prev": None}
    assert admit(empty, first_try, log=WORKSPACE).code in (Code.CHAIN_BROKEN, Code.GENESIS_INVALID)
    one = {**genesis, "seq": 1, "prev": None}
    assert not hasattr(admit(empty, {k: v for k, v in one.items() if k != "host_sig"}, log=WORKSPACE), "code")
