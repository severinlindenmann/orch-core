"""``orch approve`` and ``orch request-changes``: a person's signed decision on a gate, from the verified state."""

from __future__ import annotations

from orch import canon, crypto
from tests.ops.humans import make_ticket


def last(ws, ref="1", typ=None):
    evs = [e for e in ws.events(ref) if typ is None or e["type"] == typ]
    return evs[-1]


def test_approve_requirements_appends_a_signed_decision_built_from_the_model(hws, agent, me):
    key = make_ticket(agent)
    r = me("approve", "requirements", "--ref", key)
    assert r.code == 0, r.err
    assert r.first.startswith(f"ok {key} gate.approved requirements seq=")
    e = last(hws, typ="gate.approved")
    view = hws.view("1")
    g = view.gates["requirements"]
    assert e["actor"] == {"kind": "person", "id": hws.owner.ref, "device": hws.owner.device}
    assert (e["gate"], e["gate_gen"], e["hash"], e["policy_hash"]) == ("requirements", g.gen, g.hash, g.policy_hash)
    assert e["auth"] == "passphrase" and e["roster_v"] == 1
    assert "source_sha" not in e and g.approved
    # the signature is the device key's, over the signing bytes, and a fresh replay accepts the log
    signed = canon.person_signing_bytes("705d40abbb8c1c90354a1acaa94c935c", view.uid, e)
    assert crypto.verify(hws.owner.sig_pub, crypto.unb64u(e["sig"], 64), signed)
    fresh = hws.other()
    try:
        assert fresh.chain_errors() == [] and fresh.ticket(key).gates["requirements"].approved
    finally:
        fresh.close()


def test_what_binds_the_decision_comes_from_the_model_not_the_arguments(hws, agent, me):
    key = make_ticket(agent)
    assert me("approve", "requirements", "--ref", key).code == 0
    r = me("approve", "plan", "--ref", key)
    assert r.code == 0, r.err
    for flag in ("--hash", "--gate-gen", "--policy-hash", "--source-sha", "--person", "--actor"):
        r = me("approve", "plan", "--ref", key, flag, "x")
        assert r.code == 2, flag  # no such option: nothing a caller can say binds the decision


def test_a_stale_gate_is_refused_after_the_prompt_and_nothing_is_written(hws, agent, me):
    key = make_ticket(agent)
    before = len(hws.events("1"))

    def edit_while_typing(_request):
        assert agent("section", "set", "context", "-m", "changed under the decision").code == 0

    hws.provider.on_prompt = edit_while_typing
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "gate.stale", r.out
    assert [e["type"] for e in hws.events("1")][before:] == ["ticket.updated"]
    hws.provider.on_prompt = None
    assert me("approve", "requirements", "--ref", key).code == 0  # looking again works


def test_approve_is_refused_for_a_viewer_and_does_not_prompt(hws, agent, me):
    key = make_ticket(agent)
    viewer = hws.add_member("vera", "viewer")
    hws.act_as(viewer)
    hws.provider.requests.clear()
    r = me("approve", "requirements", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "role.denied"
    assert hws.provider.requests == []  # judged before the prompt: no passphrase asked for a refused event


def test_unknown_ticket_and_missing_ref(hws, me):
    assert me("approve", "requirements", "--ref", "99", "--json").err_code == "not_found"
    assert me("approve", "requirements", "--json").err_code == "ambiguous_ref"


def test_dry_run_checks_everything_and_signs_nothing(hws, agent, me):
    key = make_ticket(agent)
    n = len(hws.events("1"))
    hws.provider.requests.clear()
    r = me("approve", "requirements", "--ref", key, "--dry-run")
    assert r.code == 0 and "dry-run: nothing was written" in r.out
    assert hws.provider.requests == [] and len(hws.events("1")) == n


def test_request_changes_needs_a_reason_and_raises_the_generation(hws, agent, me):
    key = make_ticket(agent)
    assert me("approve", "requirements", "--ref", key).code == 0
    r = me("request-changes", "requirements", "--ref", key, "--json")
    assert r.code == 2 or r.err_code in ("invalid.input", "usage")  # -m is required
    gen = hws.view("1").gates["requirements"].gen
    r = me("request-changes", "requirements", "--ref", key, "-m", "tighten the out-of-scope section")
    assert r.code == 0, r.err
    e = last(hws, typ="gate.changes_requested")
    assert e["text"] == "tighten the out-of-scope section" and e["gate_gen"] == gen and e["auth"] == "passphrase"
    v = hws.view("1")
    assert v.gates["requirements"].gen > gen and not v.gates["requirements"].approved


def test_the_agent_hears_the_decision(hws, agent, me):
    from tests.ops.helpers import wait_for

    key = make_ticket(agent)
    assert me("request-changes", "plan", "--ref", key, "-m", "split task 1").code == 0
    r = wait_for(agent, "changes_requested")
    assert r.data["gate"] == "plan" and r.data["text"] == "split task 1" and r.data["by"] == hws.owner.ref


def test_the_code_gate_binds_the_source_list_and_is_for_someone_who_did_not_work_on_it(hws, agent, me):
    from tests.ops.humans import to_testing
    from tests.ops.test_verdict import CODE_POLICY, LINKS

    hws.repo()
    head = hws.git("rev-parse", "HEAD")
    hws.store.append(
        hws.person_event(hws.owner, "workspace", "policy.changed", gates={"code": CODE_POLICY}), log="workspace"
    )
    key = make_ticket(agent)
    assert agent("set", key, LINKS).code == 0
    to_testing(agent, me, hws.tmp, key)
    assert me("verdict", "pass", "--ref", key).code == 0
    # the owner's own agent worked on it: independent, so the owner may not approve the code
    r = me("approve", "code", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "role.denied", r.out
    reviewer = hws.add_member("rita", "maintainer")
    hws.act_as(reviewer)
    r = me("approve", "code", "--ref", key)
    assert r.code == 0, r.err
    e = last(hws, typ="gate.approved")
    assert e["gate"] == "code" and e["source_sha"] == [{"repo": "local:proj", "ref": "refs/heads/feat/x", "sha": head}]
    assert hws.view("1").status == "done"
