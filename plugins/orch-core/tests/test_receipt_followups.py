"""Follow-ups from the receipts review: a receipt file is never replaced by hand, a hand-edited `run` reaches the
document only as well-typed facts, a receipt says which checkout it ran in, keys inside a URL stay text, and the
`checks` guard looks at the one command that writes the config."""
import subprocess

import pytest

from orch.core import store
from orch.core.artifacts import doc_items
from orch.dashboard.markdown import render_markdown
from orch.errors import UsageError
from orch.hooks.guard import evaluate


@pytest.fixture
def ticket(working, plan_approved):
    plan_approved(working)
    return working


def _receipt(aops, ticket, tmp_path):
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": "cmd: true"}])
    aops.task_start(ticket, ids[0])
    aops.task_done_run(ticket, ids[0], cwd=tmp_path)
    return next(e for e in store.load(aops.ws, ticket)[1].meta["artifacts"] if e["kind"] == "receipt")


def test_a_receipt_file_cannot_be_replaced(aops, ticket, tmp_path):
    entry = _receipt(aops, ticket, tmp_path)
    fake = tmp_path / "other.log"
    fake.write_text("$ npm test\nall green")
    with pytest.raises(UsageError, match="receipt"):
        aops.artifact_add(ticket, fake, entry["name"], replace=True)


def test_hand_edited_run_facts_are_typed_before_they_reach_the_document(ws, aops, ticket, tmp_path):
    _receipt(aops, ticket, tmp_path)
    path, t = store.load(ws, ticket)
    entry = next(e for e in t.meta["artifacts"] if e["kind"] == "receipt")
    entry["run"].update(exit="0", commit="not-hex", check="<b>", at=5, seconds=-3, dirty="yes")
    entry["run"]["steps"] = [{"name": "ok", "status": "pass", "seconds": 1}, {"name": 7, "status": "weird"}]
    item = next(i for i in doc_items(t) if i["kind"] == "receipt")
    run = item["run"]
    assert "exit" not in run and "commit" not in run and "check" not in run and "at" not in run
    assert "seconds" not in run and "dirty" not in run
    assert run["steps"] == [{"name": "ok", "status": "pass", "seconds": 1}]


def test_a_receipt_names_the_checkout_it_ran_in(tmp_path):
    from orch.core.receipts import run_steps
    repo = tmp_path / "my-app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert run_steps([{"name": "v", "run": "true"}], repo, timeout=30, max_bytes=1000).record()["repo"] == "my-app"
    outside = tmp_path / "nowhere"
    outside.mkdir()
    assert run_steps([{"name": "v", "run": "true"}], outside, timeout=30, max_bytes=1000).record()["repo"] is None


def test_a_key_inside_a_plain_url_stays_text():
    html = render_markdown("see https://e.com/L-22 and L-23", key_prefix="L")
    assert '/t/L-22"' not in html and '<a class="lnk key" href="/t/L-23">L-23</a>' in html


@pytest.mark.parametrize("cmd", ["echo hi > notes.txt && grep checks orchestrator/config.json",
                                 "jq .checks tsconfig.json > out.json"])
def test_reading_checks_next_to_an_unrelated_write_is_fine(ws, cmd):
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow, cmd


def test_a_write_to_the_config_that_names_checks_is_still_refused(ws):
    cmd = "jq '.checks = {}' orchestrator/config.json > /tmp/c && mv /tmp/c orchestrator/config.json"
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}}).allow
