import json
import subprocess

import pytest

from orch.cli import run


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def commit(root, subject, files, branch=None):
    if branch:
        git(root, "checkout", "-q", "-B", branch)
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(root, "add", name)
    git(root, "commit", "-q", "-m", subject)


@pytest.fixture
def repo(ws_root):
    git(ws_root, "init", "-q", "-b", "main")
    git(ws_root, "config", "user.email", "t@example.com")
    git(ws_root, "config", "user.name", "t")
    return ws_root


@pytest.fixture
def history(ws, repo, put):
    login = put("done", title="Add login form", sections={"Summary": "Login with email", "Findings": "Keep sessions server side"})
    sess = put("done", title="Session timeout")
    mine = put("in-progress", title="Remember me", parent=None)
    other = put("open", title="Rate limit login")
    commit(repo, f"{login} add login form", {"src/auth/login.py": "1", "src/auth/session.py": "1", "tests/test_login.py": "1"})
    commit(repo, f"{sess} expire sessions", {"src/auth/session.py": "2", "src/auth/login.py": "2"})
    commit(repo, f"{login} fix login redirect", {"src/auth/login.py": "3", "src/auth/session.py": "3"})
    commit(repo, "untracked chore", {"README.md": "x"})
    commit(repo, f"{other} throttle", {"src/auth/login.py": "4"}, branch="feature/rate-limit")
    git(repo, "checkout", "-q", "main")
    commit(repo, f"{mine} remember me cookie", {"src/auth/login.py": "5"}, branch="feature/remember")
    git(repo, "checkout", "-q", "main")
    return {"login": login, "sess": sess, "mine": mine, "other": other}


def test_ticket_finds_history_in_flight_and_co_changed(ws, history):
    from orch.core.related import related
    data = related(ws, history["mine"])
    assert data["paths"] == ["src/auth/login.py"]
    assert [t["id"] for t in data["history"]] == [history["login"], history["sess"]]
    first = data["history"][0]
    assert first["commits"] == 2 and first["summary"] == "Login with email" and first["findings"] == "Keep sessions server side"
    assert [t["id"] for t in data["in_flight"]] == [history["other"]]  # from an unmerged branch
    assert data["co_changed"][0] == {"file": "src/auth/session.py", "commits": 3, "of": 5}
    assert all(c["file"] != "README.md" for c in data["co_changed"])


def test_paths_without_ticket_and_folder_match(ws, history, repo):
    from orch.core.related import related
    data = related(ws, None, [str(repo / "src" / "auth")])
    assert data["paths"] == ["src/auth"] and data["ticket"] is None
    ids = {t["id"] for t in data["history"]} | {t["id"] for t in data["in_flight"]}
    assert ids == set(history.values())
    assert related(ws, None, ["docs/nothing.md"])["commits"] == 0


def test_linked_both_directions(ws, put):
    from orch.core.related import related
    epic = put("open", title="Auth epic", type="epic")
    a = put("open", title="A", parent=epic)
    b = put("backlog", title="B", blocked_by=[a])
    rel = {(t["relation"], t["id"]) for t in related(ws, a)["linked"]}
    assert rel == {("parent", epic), ("blocks", b)}
    assert {(t["relation"], t["id"]) for t in related(ws, epic)["linked"]} == {("child", a)}


def test_cli_text_and_json(ws, history, capsys):
    assert run(["related", history["mine"]]) == 0
    out = capsys.readouterr().out
    assert "history:" in out and "in flight:" in out and "co-changed:" in out and "src/auth/session.py" in out
    assert run(["related", "--path", "src/auth/session.py", "--json", "--limit", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["history"]) == 1
    assert run(["related"]) == 2


def test_no_git_checkout_still_shows_links(ws, put, capsys):
    t = put("open", title="Lonely")
    assert run(["related", t]) == 0
    assert "no git checkout" in capsys.readouterr().out
