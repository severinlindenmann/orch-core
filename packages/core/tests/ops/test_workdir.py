"""Design r3 part C: a ticket without a codebase has a work folder; its check runs there; results are artifacts."""

from __future__ import annotations

import stat

from orch.ops import plans
from tests.ops.test_task_ac import claimed, py


def test_claim_makes_the_work_folder_and_names_it_and_show_repeats_it(ws, cli):
    cli("new", "A ticket")
    claim = cli("claim", "1")
    folder = ws.root / ".orch-work" / "DEMO-0001"
    assert any(line.startswith(f"workdir: {folder} ") for line in claim.out.splitlines()), claim.out
    assert folder.is_dir() and stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert (ws.root / ".orch-work" / ".gitignore").read_text() == "*\n"
    assert not (ws.root / ".orch" / ".orch-work").exists()  # next to the store, never inside it
    r = cli("show", "--full")
    assert any(line.startswith("workdir: .orch-work/DEMO-0001 ") for line in r.out.splitlines()), r.out


def test_a_check_with_no_codebase_runs_in_the_work_folder_not_the_project_root(ws, cli):
    ws.plan_gate_off()  # r10-P02: no plan approval in this test; an agent builds only after one
    claimed(cli)
    cli("task", "add", "t1", "--verify", py("open('made-by-check.txt', 'w').write('x')"))
    r = cli("task", "done", "T1", "--run")
    assert r.code == 0, r.err
    assert (ws.root / ".orch-work" / "DEMO-0001" / "made-by-check.txt").exists()
    assert not (ws.root / "made-by-check.txt").exists()
    receipt = next(e for e in ws.events("1") if e["type"] == "task.done")["receipt"]
    assert receipt["repo"] is None  # no commit binds the files


def test_artifact_add_refuses_a_symbolic_link(ws, cli, tmp_path):
    claimed(cli)
    real = tmp_path / "real.txt"
    real.write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    r = cli.j("artifact", "add", str(link))
    assert (r.code, r.err_code) == (5, "invalid.input") and "symbolic link" in r.doc["error"]["message"]
    assert [e for e in ws.events("1") if e["type"] == "artifact.added"] == []
    # a result filed from the work folder is a plain copy with a digest
    out = ws.root / ".orch-work" / "DEMO-0001" / "report.txt"
    out.write_text("done")
    assert cli("artifact", "add", str(out), "--kind", "report").code == 0


def test_a_ticket_holds_at_most_its_file_budget(ws, cli, tmp_path, monkeypatch):
    claimed(cli)
    monkeypatch.setattr(plans, "TICKET_ARTIFACT_BUDGET", 20)
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"x" * 12)
    b.write_bytes(b"y" * 12)
    assert cli("artifact", "add", str(a)).code == 0
    r = cli.j("artifact", "add", str(b))
    assert (r.code, r.err_code) == (5, "invalid.input") and "limit" in r.doc["error"]["message"]
    b.write_bytes(b"y" * 8)
    assert cli("artifact", "add", str(b)).code == 0  # 12 + 8 fits exactly
    a.write_bytes(b"z" * 13)
    assert cli.j("artifact", "replace", "a.bin", str(a)).err_code == "invalid.input"  # 13 + 8 > 20
    a.write_bytes(b"z" * 12)
    assert cli("artifact", "replace", "a.bin", str(a)).code == 0  # a replaced file counts once


def test_codebase_rule_is_the_primary_repo_and_one_helper(ws):
    from orch.ops.workdir import has_codebase

    class S:
        pass

    def store(declared):
        s = S()
        s.layout = ws.root and __import__("orch.layout", fromlist=["x"]).Layout.new_at(ws.root)
        s.state = S()
        s.state.workspace = S()
        s.state.workspace.repos = {n: {"path": n} for n in declared}
        return s

    def view(repos):
        v = S()
        v.fields = {"links": {"repos": repos}}
        return v

    assert not has_codebase(store(["b"]), view([]))
    assert not has_codebase(store(["b"]), view(["a", "b"]))  # undeclared primary: a work folder, whatever follows
    assert has_codebase(store(["b"]), view(["b", "a"]))
