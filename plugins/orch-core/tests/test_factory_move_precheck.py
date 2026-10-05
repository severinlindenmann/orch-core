"""Dark AI Factory: told while the agent still runs (the fifth live run: a commit without its Risk line, made in a
clone that runs no commit-msg hook, surfaced only at the release and cost the human a manual amend). The commit gate
checks a `git commit`'s message with orch's own check (the one the release uses), the clone worker's prompt gives the
format worked, and `orch move <child> testing` refuses what the release would refuse later. Real clones, real git."""
import shutil

import pytest

from orch.core import factory_built as fb, factory_clones as fc, factory_release as fr, factory_runner, permits
from orch.hooks.commit_msg import check_message
from test_factory_clones import (_both, _child, _epic, _g, _msg, _programs, _recipe, _to_testing, bin_dir, fa, fh,  # noqa: F401,E501
                                 fws, remote, run)
from test_factory_release import _not_stopping  # noqa: F401
from test_factory_runner import _behavior

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
LIVE = 'git commit -m "{C} Add elephants data file" -m "What: elephants.json with three entries" -m "Why: the page reads it"'


def _msg_of(hook):
    return (hook or {}).get("hookSpecificOutput", {}).get("decision", {}).get("message", "")


def test_the_live_commit_without_risk_is_refused_by_both_gates_with_the_fix(fws, run):
    cid, clone, b = run["cid"], run["clone"], run["b"]
    guard, hook = _both(fws, b, LIVE, clone)
    assert not guard.allow and _behavior(hook) == "deny"
    for text in (guard.reason, _msg_of(hook)):  # the reason reaches the agent on both paths
        assert "body needs a 'Risk:' line" in text, text
        assert "Your commit message needs: a subject starting with the ticket key, then What:, Why: and Risk: lines" \
            in text
        assert f'Run: git commit -m "{cid} Add the requested file" -m "What: ' in text
    ok, hook = _both(fws, b, LIVE + ' -m "Risk: low, a new file only"', clone)
    assert ok.allow and _behavior(hook) == "allow"


def test_the_refusal_echoes_at_most_80_escaped_characters_of_the_agents_text(fws, run):
    b = run["b"]
    long = "x" * 300 + "‮"
    why = permits.commit_refusal(fws, b, run["clone"], f'git commit -m "{long}" -m "What: y"')
    assert why and "x" * 81 not in why and "‮" not in why


@pytest.mark.parametrize("flags", ["-F /tmp/m", "--file=/tmp/m", "--amend", "--no-verify", "-c abc123",
                                   "--reuse-message=abc123", "--fixup=abc123", "--squash=abc123", "-e",
                                   "--template=/tmp/t", "-C abc123", "-n"])
def test_commit_options_other_than_the_message_ones_are_refused(fws, run, flags):
    cmd = f'git commit {flags} -m "{run["cid"]} x" -m "What: y" -m "Why: z" -m "Risk: low"'
    assert permits.commit_refusal(fws, run["b"], run["clone"], cmd)


def test_the_clone_prompt_carries_a_worked_example_that_passes_the_check(fws, run):
    cid = run["cid"]
    worked = factory_runner.commit_worked(fws, cid)
    prompt = factory_runner.factory_work_prompt(cid, factory_runner.commit_form(fws, cid),
                                                clone_tmp=str(fws.temporary_dir), worked=worked)
    assert f"for example `{worked}`" in prompt and "orch checks the message when you commit" in prompt
    args = __import__("shlex").split(worked)[2:]
    assert check_message(fws, permits.commit_message(args)) == []
    assert permits.commit_refusal(fws, run["b"], run["clone"], worked) is None


# -- orch move <child> testing ------------------------------------------------------------------------------------------

@pytest.fixture
def child(fws, fa, fh, human, remote):
    fr.set_recipe(fws, human, _recipe(remote))
    eid, d = _epic(fa, fh, human, fws, release="merge")
    cid = _child(fa, eid)
    clone, _ = fc.ensure(fws, human, cid)
    return eid, cid, clone


def test_a_move_with_uncommitted_work_is_refused_until_it_is_committed(fws, fa, child, close_tasks):
    from orch.errors import ValidationError
    eid, cid, clone = child
    (clone / "x.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValidationError, match=r"your clone has work that is not committed \(x.json\)"):
        _to_testing(fa, cid, close_tasks)
    _g(clone, "add", "x.json")
    _g(clone, "commit", "-q", *_msg(cid))
    fa.move(cid, "testing")


def test_a_move_with_a_commit_the_release_would_refuse_is_refused(fws, fa, child, close_tasks):
    from orch.errors import ValidationError
    eid, cid, clone = child
    (clone / "x.json").write_text("[]\n", encoding="utf-8")
    _g(clone, "add", "x.json")
    _g(clone, "commit", "-q", "-m", f"{cid} data", "-m", "What: x", "-m", "Why: y")  # the live message, no Risk
    with pytest.raises(ValidationError, match="body needs a 'Risk:' line"):
        _to_testing(fa, cid, close_tasks)


def test_a_move_adding_a_file_another_child_adds_is_refused(fws, fa, fh, human, child, close_tasks):
    from orch.errors import ValidationError
    eid, a, clone_a = child
    b = _child(fa, eid)
    clone_b, _ = fc.ensure(fws, human, b)
    for c, clone in ((a, clone_a), (b, clone_b)):
        (clone / "elefant.json").write_text(f"[{c}]\n", encoding="utf-8")
        _g(clone, "add", "elefant.json")
        _g(clone, "commit", "-q", *_msg(c))
    with pytest.raises(ValidationError, match=f"you add elefant.json, which {b} adds too"):
        _to_testing(fa, a, close_tasks)


def test_a_human_move_is_never_held_by_the_precheck(fws, fa, fh, child, close_tasks):
    eid, cid, clone = child
    (clone / "x.json").write_text("[]\n", encoding="utf-8")
    assert fb.move_refusal(fws, __import__("orch.core.store", fromlist=["x"]).load(fws, cid)[1])
    fa.claim(cid)
    close_tasks(fa, cid)
    fa.set_section(cid, "Verification", "- AC1: ran the suite, green")
    fh.move(cid, "testing")  # the human decides; the release still checks
