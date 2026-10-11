"""``orch verdict``: the verify decision binds the commits git shows right before the prompt (D58)."""

from __future__ import annotations

from tests.ops.humans import make_ticket, to_testing

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'
CODE_POLICY = {
    "approvers": ["maintainer", "owner"],
    "count": 1,
    "not": ["assignees"],
    "applies": "all",
    "independent": True,
}


def verdicts(ws):
    return [e for e in ws.events("1") if e["type"] == "verdict.given"]


def repo_ticket(hws, agent, me):
    repo = hws.repo()
    key = make_ticket(agent)
    assert agent("set", key, LINKS).code == 0
    return repo, key


def test_a_pass_verdict_binds_the_current_source_list(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    head = hws.git("rev-parse", "HEAD")
    to_testing(agent, me, hws.tmp, key)
    r = me("verdict", "pass", "--ref", key)
    assert r.code == 0, r.err
    assert r.first.startswith(f"ok {key} verdict.given pass seq=")
    (e,) = verdicts(hws)
    g = hws.view("1").gates["verify"]
    assert e["outcome"] == "pass" and e["source_sha"] == [
        {"repo": "local:proj", "ref": "refs/heads/feat/x", "sha": head}
    ]
    assert (e["gate_gen"], e["hash"], e["policy_hash"]) == (g.gen, g.hash, g.policy_hash)
    assert e["actor"]["id"] == hws.owner.ref and e["auth"] == "passphrase"
    assert hws.view("1").status == "done"


def test_the_prompt_shows_the_commit_the_verdict_binds(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    head = hws.git("rev-parse", "HEAD")
    to_testing(agent, me, hws.tmp, key)
    hws.provider.shown.clear()
    assert me("verdict", "pass", "--ref", key).code == 0
    (shown,) = hws.provider.shown
    assert "type: verdict.given" in shown and "outcome: pass" in shown
    assert f"source_sha[1].sha: {head}" in shown and "source_sha[1].repo: local:proj" in shown


def test_a_new_commit_after_the_verdict_voids_it(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)
    assert me("verdict", "pass", "--ref", key).code == 0 and hws.view("1").status == "done"
    (repo / "impl.txt").write_text("changed after the verdict\n")
    hws.git("commit", "-qam", "late")
    assert agent("show", key).code == 0  # the host observes the new commit
    types = [e["type"] for e in hws.events("1")]
    assert types[-2:] == ["branch.pushed", "gate.invalidated"]
    assert hws.events("1")[-1]["cause"] == "new_commits"
    v = hws.view("1")
    assert v.status == "testing" and not v.gates["verify"].approved


def test_a_dirty_working_tree_is_refused_and_nothing_is_signed(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)
    (repo / "scratch.txt").write_text("not committed\n")
    hws.provider.requests.clear()
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "observe.unavailable" and "uncommitted" in r.doc["error"]["message"]
    assert hws.provider.requests == [] and verdicts(hws) == []


def test_an_unobservable_repository_is_refused(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)
    hws.git("branch", "-m", "feat/x", "feat/y")  # the linked branch no longer exists
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 5 and r.err_code in ("observe.unavailable", "source.missing"), r.out
    assert verdicts(hws) == []


def test_a_commit_made_while_the_person_types_is_refused(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)

    def commit(_request):
        (repo / "impl.txt").write_text("raced\n")
        hws.git("commit", "-qam", "raced")

    hws.provider.on_prompt = commit
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 5 and r.err_code == "gate.stale", r.out
    assert verdicts(hws) == []
    hws.provider.on_prompt = None
    assert me("verdict", "pass", "--ref", key).code == 0  # the person looks again and decides on the new commit


def test_fail_needs_a_reason_and_sends_the_ticket_back(hws, agent, me):
    from tests.ops.helpers import wait_for

    repo, key = repo_ticket(hws, agent, me)
    to_testing(agent, me, hws.tmp, key)
    r = me("verdict", "fail", "--ref", key, "--json")
    assert r.code != 0 and verdicts(hws) == []
    r = me("verdict", "fail", "--ref", key, "-m", "the join drops rows")
    assert r.code == 0, r.err
    assert verdicts(hws)[0]["text"] == "the join drops rows"
    assert hws.view("1").status == "in_progress"
    d = wait_for(agent, "verdict")
    assert d.data["outcome"] == "fail" and d.data["text"] == "the join drops rows" and d.code == 3


def test_a_verdict_outside_testing_is_refused(hws, agent, me):
    repo, key = repo_ticket(hws, agent, me)
    r = me("verdict", "pass", "--ref", key, "--json")
    assert r.code == 3 and r.err_code == "transition.refused", r.out


def test_a_ticket_without_a_repository_has_an_empty_source_list(hws, agent, me):
    key = make_ticket(agent)
    to_testing(agent, me, hws.tmp, key)
    assert me("verdict", "pass", "--ref", key).code == 0
    assert verdicts(hws)[0]["source_sha"] == []
