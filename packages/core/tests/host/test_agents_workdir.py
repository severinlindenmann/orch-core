"""Design r3 part C: a ticket without a codebase gets a work folder (``<project>/.orch-work/<KEY>/``).

Previewed, pinned, in the spec digest, made by ``agent.start``, named by ``ORCH_WORKDIR``; a link in the way refuses."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from tests.host.dispatch_helpers import ok
from tests.host.test_agents import preview, read_out, ready, start, w  # noqa: F401 - the H11c world

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


def test_a_ticket_without_a_codebase_starts_in_its_work_folder_with_orch_workdir(ready):  # noqa: F811
    world = ready
    c = world.conn()
    _, key = world.ticket()
    path = Path(world.lab.root) / ".orch-work" / key
    pre = preview(c, ticket=key, harness="codex", mode="work")
    assert pre["blocked"] is None and pre["cwd"] == {"kind": "workdir", "ticket": key}
    assert pre["cwd_path"] == str(path) and not path.exists()  # the preview created nothing
    assert pre["will_create_worktree"] is False
    run = ok(start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"]))["data"]["run"]
    assert run["cwd"] == {"kind": "workdir", "ticket": key}
    _, env = read_out(world, "codex")
    assert env["ORCH_WORKDIR"] == str(path) and "ORCH_WORKTREE" not in env
    assert os.path.samefile(env["PWD"], path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert (path.parent / ".gitignore").read_text() == "*\n"
    assert "ORCH_WORKDIR" in pre["env"]


def test_the_work_folder_and_its_pin_are_in_the_spec_digest(ready):  # noqa: F811
    world = ready
    c = world.conn()
    _, key = world.ticket()
    before = preview(c, ticket=key, harness="codex", mode="work")
    path = Path(world.lab.root) / ".orch-work" / key
    path.mkdir(parents=True, mode=0o700)
    after = preview(c, ticket=key, harness="codex", mode="work")
    assert after["cwd_path"] == str(path) and after["spec"] != before["spec"]  # to be made -> present with a pin
    # a start bound to the old preview is refused: the folder it would have made now exists with another identity
    from tests.host.dispatch_helpers import err

    assert err(start(c, ticket=key, harness="codex", mode="work", spec=before["spec"])) == "launch.changed"
    assert world.runner.sup.runs() == []


def test_a_symlinked_work_dir_blocks_the_launch_and_nothing_is_made_through_it(ready, tmp_path):  # noqa: F811  # noqa: F811
    world = ready
    c = world.conn()
    _, key = world.ticket()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (Path(world.lab.root) / ".orch-work").symlink_to(outside)
    pre = preview(c, ticket=key, harness="codex", mode="work")
    assert pre["blocked"]["code"] == "launch.cwd_changed"
    assert list(outside.iterdir()) == []


def test_open_terminal_here_of_a_ticket_without_a_codebase_opens_in_its_work_folder(ready):  # noqa: F811
    from pathlib import Path as P

    from orch.runner import enablement
    from tests.host.lifecycle_helpers import UID
    from tests.host.test_agents import grant_addon

    world = ready
    grant_addon(world, "terminals", ("pty",))
    enablement.set_terminals(P(world.lab.state), world.wid, True, UID, text="t")
    c = world.conn()
    _, key = world.ticket()
    cwd = {"kind": "workdir", "ticket": key}
    pre = ok(c.call("term.preview", {"harness": "codex", "cwd": cwd}))["data"]
    path = P(world.lab.root) / ".orch-work" / key
    assert pre["blocked"] is None and pre["cwd"] == cwd and pre["cwd_path"] == str(path) and not path.exists()
    rid = c.send("term.open", {"harness": "codex", "cwd": cwd, "spec": pre["spec"]}, idem=True)
    assert ok(c.answer(rid))["data"]["run"]["cwd"] == cwd
    _, env = read_out(world, "codex")
    assert os.path.samefile(env["PWD"], path) and stat.S_IMODE(path.stat().st_mode) == 0o700
    assert (
        ok(c.call("term.preview", {"harness": "codex", "cwd": {"kind": "workdir", "ticket": "NOPE-999"}}))["data"][
            "blocked"
        ]["code"]
        == "not_found"
    )


def test_workdir_get_and_delete_touch_only_the_ticket_folder(ready, tmp_path):  # noqa: F811
    world = ready
    c = world.conn()
    _, key = world.ticket()
    other = "DEMO-0002"  # another ticket's folder (any key: the host deletes by the ticket asked for)
    root = Path(world.lab.root)
    assert ok(c.call("workdir.get", {"ticket": key}))["data"] == {
        "codebase": False,
        "exists": False,
        "folder": f".orch-work/{key}",
    }
    for k in (key, other):
        (root / ".orch-work" / k).mkdir(parents=True, exist_ok=True)
        (root / ".orch-work" / k / "a.txt").write_text("12345")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("mine")
    (root / ".orch-work" / key / "link").symlink_to(victim)
    got = ok(c.call("workdir.get", {"ticket": key}))["data"]
    assert (got["exists"], got["files"], got["bytes"]) == (True, 1, 5) and "path" not in got
    (root / "mine.txt").write_text("project file")
    out = ok(c.call("workdir.delete", {"ticket": key}, idem=True))["data"]
    assert out["deleted"] is True and not (root / ".orch-work" / key).exists()
    assert (victim / "keep").read_text() == "mine" and (root / "mine.txt").exists()  # nothing else was touched
    assert (root / ".orch-work" / other / "a.txt").exists()  # another ticket's folder stays
    assert ok(c.call("workdir.delete", {"ticket": key}, idem=True))["data"]["deleted"] is False  # already gone
    import json

    lines = (world.lab.state / "host" / "audit-security.jsonl").read_text().splitlines()
    mine = [json.loads(x) for x in lines if json.loads(x)["event"] == "workdir.delete"]
    assert [(m["ticket"], m["deleted"]) for m in mine] == [(key, True), (key, False)]
    assert all(m["principal"] and m["workspace_id"] for m in mine)


def test_workdir_delete_is_a_persons_op_needs_an_idem_and_refuses_a_running_folder(ready):  # noqa: F811
    from tests.host.dispatch_helpers import err

    world = ready
    c = world.conn()
    _, key = world.ticket()
    (Path(world.lab.root) / ".orch-work" / key).mkdir(parents=True)
    assert err(c.call("workdir.delete", {"ticket": key})) == "idem.required"
    assert err(c.call("workdir.get", {"ticket": "NOPE-999"})) == "not_found"
    assert err(c.call("workdir.get", {"ticket": key, "x": 1})) == "invalid.input"
    assert err(c.call("workdir.delete", {"ticket": key}, grant=world.grant, session="s", idem=True)) == "op.not_allowed"
    pre = preview(c, ticket=key, harness="codex", mode="work")
    ok(start(c, ticket=key, harness="codex", mode="work", spec=pre["spec"]))
    assert err(c.call("workdir.delete", {"ticket": key}, idem=True)) == "worktree.in_use"
    assert (Path(world.lab.root) / ".orch-work" / key).is_dir()


def test_a_claude_agent_in_a_work_folder_gets_a_prompt_without_any_path(ready):  # noqa: F811
    # run 16 P01: a path outside the work folder raised a hidden "read outside the working directories" question
    world = ready
    c = world.conn()
    _, key = world.ticket()
    pre = preview(c, ticket=key, harness="claude-code", mode="work")
    assert pre["cwd"] == {"kind": "workdir", "ticket": key}
    prompt_text = pre["argv"][-1]
    assert prompt_text == f"Run orch status, then work on ticket {key}. Your rules are in your system prompt."
    assert "--append-system-prompt-file" in pre["argv"]
    assert not any(str(world.lab.root) in a for a in pre["argv"][5:])


def test_workdir_get_lists_plain_files_only_when_asked(ready, tmp_path):  # noqa: F811
    c = ready.conn()
    _, key = ready.ticket()
    folder = Path(ready.lab.root) / ".orch-work" / key
    folder.mkdir(parents=True)
    (folder / "hello.txt").write_text("hi")
    (folder / "bad name!.txt").write_text("x")  # a name the server would not take
    (folder / "sub").mkdir()
    (folder / "link").symlink_to(tmp_path)
    assert "items" not in ok(c.call("workdir.get", {"ticket": key}))["data"]
    got = ok(c.call("workdir.get", {"ticket": key, "list": True}))["data"]
    assert got["items"] == [{"name": "hello.txt", "bytes": 2}]
    for i in range(30):
        (folder / f"f{i:02}.txt").write_text("z")
    got = ok(c.call("workdir.get", {"ticket": key, "list": True}))["data"]
    assert len(got["items"]) == 20 and got["items_truncated"] is True
