import json

import pytest

from orch.cli import run
from orch.config.answers import build_config, parse_agent_may, parse_repo, parse_tracker
from orch.errors import ValidationError


def test_parse_tracker_keeps_url_with_equals_and_key():
    t = parse_tracker("ABC=ABC-\\d+=https://jira.example/browse/{key}?a=b")
    assert t == {"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.example/browse/{key}?a=b"}


@pytest.mark.parametrize("spec", ["ABC", "ABC=ABC-\\d+", "ABC=(=https://x/{key}", "ABC=ABC-\\d+=https://x/no-key"])
def test_parse_tracker_rejects(spec):
    with pytest.raises(ValidationError):
        parse_tracker(spec)


def test_parse_repo_and_agent_may():
    assert parse_repo("hub") == ("hub", None)
    assert parse_repo("hub=repos/my hub") == ("hub", "repos/my hub")
    assert parse_agent_may("commit, review") == {"commit": True, "push": False, "open_review": True}
    assert parse_agent_may("none") == {"commit": False, "push": False, "open_review": False}
    with pytest.raises(ValidationError):
        parse_agent_may("deploy")


def test_build_config_validates():
    cfg = build_config(customer="initech", prefix="DDP", harnesses=["claude-plugin"],
                       trackers=[parse_tracker("DDP=DDP-\\d+=https://jira/browse/{key}")],
                       git_type="gitlab-selfhosted", git_base_url="https://git.example", review_term="MR",
                       agent_may=parse_agent_may("commit"), repos=[("hub", None), ("dbt", "repos/dbt project")])
    assert cfg["git"]["review_term"] == "MR" and cfg["git"]["agent_may"]["commit"] is True
    assert cfg["git"]["repos"] == {"hub": {}, "dbt": {"path": "repos/dbt project"}}
    with pytest.raises(ValidationError):
        build_config(customer="x", prefix="L", harnesses=["claude"], trackers=[], git_type="github",
                     git_base_url="", review_term="XX", agent_may=parse_agent_may("none"), repos=[])


def test_init_with_answer_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = run(["init", "--customer", "initech", "--prefix", "ddp", "--harness", "claude-plugin",
                "--tracker", "DDP=DDP-\\d+=https://jira/browse/{key}", "--git-type", "gitlab-selfhosted",
                "--review-term", "MR", "--agent-may", "commit", "--repo", "hub=repos/my hub"])
    assert code == 0
    cfg = json.loads((tmp_path / "orchestrator" / "config.json").read_text(encoding="utf-8"))
    assert cfg["external_trackers"][0]["prefix"] == "DDP"
    assert cfg["git"]["repos"] == {"hub": {"path": "repos/my hub"}}
    assert cfg["harnesses"] == ["claude-plugin"]


def test_init_rejects_bad_answers_without_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--customer", "x", "--review-term", "XX"]) == 5
    assert not (tmp_path / "orchestrator").exists()


def test_init_adopt_appends_to_existing_claude_md(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    assert run(["init", "--customer", "x", "--adopt"]) == 0
    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("# mine\n") and "@orchestrator/AGENTS.orch.md" in text
