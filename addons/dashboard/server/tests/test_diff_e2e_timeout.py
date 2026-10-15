"""#561 (Opus probes 5 and 8): a REAL subprocess timeout (the operation's argv swapped for `sleep`, observe._TIMEOUT
lowered) of a diff operation OR of a revision lookup travels devhost reads.diff -> server /changes route -> 409
observe.unavailable "diff unavailable"; never a 200 with an empty, partial or other-base diff."""

import pytest
from test_dashboard_ticket import headers, url


@pytest.mark.parametrize("op", ["diff_numstat", "diff_names", "diff_patch"])
def test_real_timeout_reaches_the_route_as_diff_unavailable(client, token, stub, rich, monkeypatch, op):
    from devhost import reads
    from orch import gitsafe

    ok = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert ok.status_code == 200 and ok.json()["files"], ok.text  # positive control: a real diff with files
    real = gitsafe.command

    def command(repo, o, own, **kw):
        argv, env = real(repo, o, own, **kw)
        return (["sleep", "3"] if o == op else argv), env

    monkeypatch.setattr(gitsafe, "command", command)
    monkeypatch.setattr(reads.observe, "_TIMEOUT", 0.5)
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "observe.unavailable"
    assert "diff unavailable" in res.json()["error"]["message"]


@pytest.mark.parametrize("op", ["commit", "ref", "merge_base"])
def test_a_timed_out_base_lookup_is_diff_unavailable(client, token, stub, rich, monkeypatch, op):
    """Probe 8 (R8-L2): a timed-out base lookup is no longer read as "no default branch / no merge base" with a diff
    against the head's parent: it is 409 "diff unavailable"."""
    from devhost import reads
    from orch import gitsafe

    ok = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert ok.status_code == 200 and ok.json()["files"], ok.text  # positive control
    real = gitsafe.command

    def command(repo, o, own, **kw):
        argv, env = real(repo, o, own, **kw)
        return (["sleep", "3"] if o == op else argv), env

    monkeypatch.setattr(gitsafe, "command", command)
    monkeypatch.setattr(reads.observe, "_TIMEOUT", 0.5)
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "observe.unavailable"
    assert f"diff unavailable: git did not answer ({op})" in res.json()["error"]["message"]


@pytest.mark.parametrize("script", ["exit 128", "kill -9 $$"], ids=["128", "signal"])
def test_a_merge_base_without_an_answer_is_diff_unavailable_not_a_parent_diff(
    client, token, stub, rich, monkeypatch, script
):
    """Opus R9-L1: merge-base exiting 128 (or killed) is no answer: 409 "diff unavailable", never a 200 diff against
    the head's parent (only its exit 1, no common ancestor, falls back to the parent)."""
    from orch import gitsafe

    ok = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert ok.status_code == 200 and ok.json()["files"], ok.text  # positive control
    real = gitsafe.command

    def command(repo, o, own, **kw):
        argv, env = real(repo, o, own, **kw)
        return (["sh", "-c", script] if o == "merge_base" else argv), env

    monkeypatch.setattr(gitsafe, "command", command)
    res = client.get(url(stub, "DEMO-0001/changes"), headers=headers(token))
    assert res.status_code == 409, res.text
    assert "diff unavailable: git did not answer (merge_base)" in res.json()["error"]["message"]
