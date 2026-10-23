"""H11e part 2: status and note through the tool, commit --push, auto mode and the timer (with H7's fences), the
person-side log, the run lock and budget, direct mode (D-P2-38) and the dashboard child contract. Local bare repos in
temp dirs only; no real service manager, no real HOME."""

from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from orch.records import RecordsError
from orch.records import cli as rcli
from orch.records import person as rperson
from orch.records import timer as rtimer
from orch.records import tool as rtool
from orch.records.direct import DirectLink
from tests.host.test_records_sync import WS_FILES, Env, git, home, w  # noqa: F401 - fixtures


@pytest.fixture
def r(w, tmp_path, home):  # noqa: F811 - the fixtures above
    return Env(w, tmp_path, home)


class FakeTimer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, state: Path) -> FakeTimer:
        self.calls.append(f"plan:{state}")
        return self

    def install(self, *, replace: bool = False) -> bool:
        self.calls.append("install")
        return True

    def start(self, *, changed: bool) -> None:
        self.calls.append(f"start:{changed}")

    def remove(self) -> str:
        self.calls.append("remove")
        return "removed"


def run_cli(r, *argv, confirm=True, timer=None, clock=None, link=None):
    out = io.StringIO()
    code = rcli.main(
        [*argv, "--json"], r.env, out, link=link or r.link, root=r.root, workspace_id=r.w.wid, tips=r.tips,
        protocols=WS_FILES, confirm=lambda text: confirm, timer=timer, state=r.tips.path.parent, clock=clock,
    )  # fmt: skip
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, lines
    return code, json.loads(lines[0])


# --- status, note, commit --push (the dashboard child) ---------------------------------------------------------------


def test_commit_push_is_one_child_run_and_status_follows(r):
    r.init()
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    code, out = run_cli(r, "commit")
    assert code == 0 and out["pushed"] is False
    assert r.push(initial=True)["pushed"] is True
    r.w.ticket("one")
    code, out = run_cli(r, "commit", "--push")
    jsonschema.validate(out, rcli.CHILD_SCHEMA)
    assert code == 0 and out["pushed"] is True and out["url"] == url and len(out["commit"]) == 40
    code, out = run_cli(r, "status")
    status = out["detail"]["status"]
    assert status["configured"] and status["last"]["commit"] == git(r.store, "rev-parse", "HEAD")
    assert status["last"]["pushed_at"] is not None and status["repo"] == {"state": "ok"}
    assert status["pending"]["events"] == 0
    assert status["remote"] == {"url": url, "private": False, "export": "ok", "current_url": url,
                                "current_private": False, "matches_last": True}  # fmt: skip
    # the push URL changes: the dashboard's confirmation sees it before any push
    other = r.bare("other")
    git(r.store, "remote", "set-url", "origin", other)
    remote = run_cli(r, "status")[1]["detail"]["status"]["remote"]
    assert remote["current_url"] == other and remote["matches_last"] is False and remote["url"] == url


def test_run8_status_text_says_not_set_up_in_plain_words_until_init(r):
    def text():
        out = io.StringIO()
        code = rcli.main(
            ["status"], r.env, out, link=r.link, root=r.root, workspace_id=r.w.wid, tips=r.tips,
            protocols=WS_FILES, confirm=lambda t: True, state=r.tips.path.parent,
        )  # fmt: skip
        assert code == 0
        return out.getvalue()

    assert text().startswith(rcli.NOT_SET_UP) and "orch records init" in rcli.NOT_SET_UP
    assert run_cli(r, "init")[0] == 0
    assert rcli.NOT_SET_UP not in text()


def test_status_reports_a_missing_or_foreign_repo_and_a_differing_index(r):
    code, out = run_cli(r, "status")
    assert code == 0 and out["detail"]["status"]["local"]["repo"] == {"state": "missing"}
    assert out["detail"]["status"]["configured"] is False
    assert run_cli(r, "init")[0] == 0
    assert run_cli(r, "status")[1]["detail"]["status"]["configured"] is True
    assert r.link("records.status", {})["configured"] is True  # init reported the repo to the host
    r.commit()
    staged = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=r.store, input=b"x\n", capture_output=True, check=True,
        env={"HOME": str(r.home), "PATH": os.environ["PATH"]},
    ).stdout.decode().strip()  # fmt: skip
    git(r.store, "update-index", "--add", "--cacheinfo", f"100644,{staged},notes.txt")
    assert run_cli(r, "status")[1]["detail"]["status"]["local"]["repo"] == {"state": "index_differs"}
    code, out = run_cli(r, "sync-index")
    assert code == 0 and out["detail"]["index"] == "synced"
    assert run_cli(r, "status")[1]["detail"]["status"]["local"]["repo"] == {"state": "ok"}
    git(r.store, "config", "--local", "credential.helper", "!evil")
    state = run_cli(r, "status")[1]["detail"]["status"]["local"]["repo"]
    assert state["state"] == "unsafe" and "credential" in state["problem"]


def test_the_child_contract_on_refusal(r):
    code, out = run_cli(r, "commit")  # no repo
    jsonschema.validate(out, rcli.CHILD_SCHEMA)
    assert code == 3 and out["ok"] is False and out["code"] == "records.not_dedicated"
    assert set(out) <= {"ok", "code", "message", "hint"}


def test_orch_records_is_the_same_tool(tmp_path):
    from orch.cli.main import main

    out = io.StringIO()
    state = tmp_path / "state"
    code = main(["records", "auto", "--state", str(state), "--json"], env={"HOME": str(tmp_path)}, stdout=out)
    assert code == 0 and json.loads(out.getvalue()) == {"ok": True, "detail": {"runs": []}}


# --- auto mode -------------------------------------------------------------------------------------------------------


def test_auto_on_is_terminal_only_and_installs_the_timer(r):
    fake = FakeTimer()
    code, out = run_cli(r, "auto", "on", "--every", "10", confirm=False, timer=fake)
    assert code == 3 and out["code"] == "records.unconfirmed" and fake.calls == [] and r.tips.autos() == {}
    code, out = run_cli(r, "auto", "on", "--every", "3", timer=fake)
    assert code == 5 and out["code"] == "records.invalid" and fake.calls == [] and r.tips.autos() == {}
    code, out = run_cli(r, "auto", "on", "--every", "10", "--push", timer=fake)
    assert code == 0 and out["detail"]["auto"] == {"on": True, "every_min": 10, "push": True}
    assert fake.calls[1:] == ["install", "start:True"]
    assert r.tips.auto(r.w.wid) == {"on": True, "every_min": 10, "push": True}
    assert run_cli(r, "status")[1]["detail"]["status"]["auto"] == {"on": True, "every_min": 10, "push": True}
    code, out = run_cli(r, "auto", "off", timer=fake)
    assert code == 0 and out["detail"]["timer"] == "removed" and fake.calls[-1] == "remove"
    assert r.tips.autos() == {}


def test_a_failing_timer_leaves_auto_off(r):
    class Broken(FakeTimer):
        def start(self, *, changed: bool) -> None:
            raise RecordsError("records.timer_failed", "launchctl bootstrap failed (5)")

    code, out = run_cli(r, "auto", "on", timer=Broken())
    assert code == 1 and out["code"] == "records.timer_failed" and r.tips.autos() == {}


def _auto(r, now, link=None):
    out = io.StringIO()
    code = rcli.main(
        ["auto", "--json"], r.env, out, link=link or r.link, root=r.root, workspace_id=r.w.wid, tips=r.tips,
        protocols=WS_FILES, state=r.tips.path.parent, clock=lambda: now[0],
    )  # fmt: skip
    doc = json.loads(out.getvalue())
    return code, doc, doc["detail"]["runs"]


def test_the_auto_run_keeps_its_own_clock_and_commits_and_pushes(r):
    r.init()
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    run_cli(r, "commit")
    r.push(initial=True)
    r.tips.set_auto(r.w.wid, {"on": True, "every_min": 5, "push": True})
    now = [1_000_000.0]
    code, doc, runs = _auto(r, now)  # never ran: due; nothing changed, so the commit is a no-op
    assert code == 0 and doc["ok"] and runs[0]["changed"] is False and runs[0]["pushed"] is False
    r.w.ticket("one")
    code, doc, runs = _auto(r, now)
    assert runs == [{"workspace": r.w.wid, "skipped": "not due"}]
    now[0] += 6 * 60
    code, doc, runs = _auto(r, now)
    assert code == 0 and runs[0]["changed"] is True and runs[0]["pushed"] is True and runs[0]["url"] == url
    assert git(r.tmp, "--git-dir", url[len("file://") :], "rev-parse", "refs/heads/main") == runs[0]["commit"]
    assert r.tips.auto(r.w.wid)["last_run"] == int(now[0])


def test_opus1_a_maintainers_note_never_steers_the_owners_timer_or_status(r, monkeypatch):
    """Opus's probe: a maintainer notes a fake commit and push to an evil URL. The owner's status keeps the owner's
    own record, and the owner's timer still commits and pushes."""
    import dataclasses

    from orch.host import records as host_records

    r.init()
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    code, out = run_cli(r, "commit")
    real_commit = out["commit"]
    r.push(initial=True)
    r.tips.set_auto(r.w.wid, {"on": True, "every_min": 5, "push": True})
    real = host_records.Viewer.of
    monkeypatch.setattr(
        host_records.Viewer, "of",
        lambda call: dataclasses.replace(real(call), person=r.w.quinn.ref, role="maintainer"),
    )  # fmt: skip
    snap = r.link("records.snapshot", {})
    r.link("records.note", {"manifest": snap["manifest"], "commit": "b" * 40, "pushed": True,
                            "url": "https://evil.example/x.git"})  # fmt: skip
    monkeypatch.setattr(host_records.Viewer, "of", real)
    r.w.ticket("one")
    status = run_cli(r, "status")[1]["detail"]["status"]
    assert status["last"]["commit"] == real_commit and status["pending"]["events"] > 0
    assert status["remote"]["url"] in (None, url) and "evil" not in json.dumps(status)
    assert status["last_noted"]["by"] == r.w.quinn.ref
    now = [2_000_000.0]
    code, doc, runs = _auto(r, now)
    assert code == 0 and runs[0]["changed"] is True and runs[0]["pushed"] is True
    assert git(r.tmp, "--git-dir", url[len("file://") :], "rev-parse", "refs/heads/main") == runs[0]["commit"]


def test_one_failing_workspace_never_stops_the_others(r):
    r.init()
    r.tips.set_auto(r.w.wid, {"on": True, "every_min": 5, "push": False})
    r.tips.set_auto("f" * 32, {"on": True, "every_min": 5, "push": False})  # not served by the seam: refused
    code, doc, runs = _auto(r, [3_000_000.0])
    assert code == 3 and doc["ok"] is False and doc["code"] == "records.unsupported"
    by = {i["workspace"]: i for i in runs}
    assert by[r.w.wid]["commit"] and by["f" * 32]["code"] == "records.unsupported"

    def boom(op, args):
        raise RuntimeError("unexpected")

    r.tips.set_auto("f" * 32, None)
    code, doc, runs = _auto(r, [4_000_000.0], link=boom)
    assert code == 1 and doc["code"] == "internal" and runs[0]["code"] == "internal"


# --- the timer units and their fences ------------------------------------------------------------------------------


def test_timer_units_for_both_platforms(tmp_path):
    env = {"HOME": str(tmp_path)}
    mac = rtimer.plan(env, tmp_path / "state", platform="darwin", python=Path("/usr/bin/python3"), uid=501, dev=True)
    (path, content), = mac.files  # fmt: skip
    assert path == tmp_path / "Library" / "LaunchAgents" / "io.orch.records.plist"
    doc = plistlib.loads(content)
    assert doc["StartInterval"] == 300 and doc["RunAtLoad"] is False and doc["Label"] == "io.orch.records"
    assert doc["ProgramArguments"][:5] == ["/usr/bin/python3", "-I", "-m", "orch.records", "auto"]
    lin = rtimer.plan(env, tmp_path / "st%te", platform="linux", python=Path("/usr/bin/python3"), uid=1, dev=True)
    names = [p.name for p, _ in lin.files]
    assert names == ["orch-records.service", "orch-records.timer"]
    service, timer = (c.decode() for _, c in lin.files)
    assert "Type=oneshot" in service and "st%%te" in service and "OnUnitActiveSec=5min" in timer
    with pytest.raises(RecordsError) as e:
        rtimer.plan(env, tmp_path, platform="win32", dev=True)
    assert e.value.code == "records.unsupported"


def test_a_source_checkout_is_never_the_timers_program(tmp_path):
    with pytest.raises(RecordsError) as e:  # this test runs from a checkout's .venv
        rtimer.plan({"HOME": str(tmp_path)}, tmp_path, platform="darwin", python=Path(sys.executable))
    assert e.value.code == "records.unsupported" and "checkout" in e.value.message


def test_the_timer_manager_runs_only_the_given_runner(tmp_path):
    ran = []

    def fake(argv):
        ran.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 1 if argv[1] == "print" else 0, "", "")

    unit = rtimer.plan({"HOME": str(tmp_path)}, tmp_path, platform="darwin", python=Path("/usr/bin/python3"), dev=True)
    m = rtimer.TimerManager(unit, run=fake)
    assert m.install() is True and m.install() is False
    m.start(changed=True)
    assert ran[-1][:2] == ["launchctl", "bootstrap"]
    m.remove()
    assert not unit.files[0][0].exists() and ran[-1][:2] == ["launchctl", "bootout"]


def test_the_default_runner_never_reaches_the_service_manager(tmp_path):
    unit = rtimer.plan({"HOME": str(tmp_path)}, tmp_path, platform="darwin", python=Path("/usr/bin/python3"), dev=True)
    with pytest.raises(RecordsError):
        rtimer.TimerManager(unit).start(changed=True)  # the suite's guard (and ORCH_NO_SERVICE_MANAGER) refuse
    assert os.environ.get("ORCH_NO_SERVICE_MANAGER") == "1"


def test_the_guard_refuses_a_unit_under_the_real_home():
    import pwd

    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    unit = rtimer.plan({"HOME": str(real)}, real / "x", platform="darwin", python=Path("/usr/bin/python3"), dev=True)
    before = unit.files[0][0].exists()
    with pytest.raises(AssertionError):
        rtimer.TimerManager(unit).install()
    assert unit.files[0][0].exists() == before


# --- the person-side log, the lock, the budget ---------------------------------------------------------------------


def test_every_run_is_logged_without_contents(r):
    r.init()
    run_cli(r, "commit")
    run_cli(r, "push")  # no remote: refused
    log = (r.tips.path.parent / "person" / "records.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in log]
    assert [e["event"] for e in events] == ["start", "commit", "start", "refused"]
    assert events[-1]["code"] == "records.remote_missing" and events[1]["commit"]
    assert oct(os.stat(r.tips.path.parent / "person" / "records.jsonl").st_mode & 0o777) == "0o600"
    config = (r.store / "config.json").read_text()
    assert config[:40] not in "\n".join(log)


def test_the_log_rotates_at_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(rperson, "LOG_MAX", 300)
    log = rperson.RunLog(tmp_path / "person" / "records.jsonl", clock=lambda: 0)
    for i in range(40):
        log.write("w" * 32, "start", n=i)
    names = sorted(p.name for p in (tmp_path / "person").iterdir())
    assert names == ["records.jsonl", "records.jsonl.1", "records.jsonl.2", "records.jsonl.3"]
    assert all(p.stat().st_size <= 300 for p in (tmp_path / "person").iterdir())


def test_one_run_per_workspace(r):
    with rperson.run_lock(r.tips.path.parent, r.w.wid):
        code, out = run_cli(r, "commit")
    assert code == 9 and out["code"] == "records.busy"


def test_the_whole_run_has_one_budget(r, monkeypatch):
    r.init()
    monkeypatch.setattr(rcli, "RUN_BUDGET", 0.0)
    code, out = run_cli(r, "commit")
    assert code == 9 and out["code"] == "records.budget"


def test_a_replaced_file_stops_the_stream(r):
    r.init()
    repo = r.repo(None)
    manifest = rtool.fetch_manifest(r.link, r.w.wid)
    (r.store / "config.json").write_bytes(b"x" * (r.store / "config.json").stat().st_size)
    assert rtool.stream_blobs(repo, manifest) is None


# --- direct mode (D-P2-38) -----------------------------------------------------------------------------------------


@pytest.fixture
def direct(tmp_path, home):  # noqa: F811
    from tests.store.helpers import WS
    from tests.store.helpers import Env as StoreEnv

    (tmp_path / "d").mkdir()
    ws = StoreEnv(tmp_path / "d")
    ws.bootstrap()
    ws.store.close()
    link = DirectLink(ws.root, WS, ws.host_state)
    yield ws, link, WS
    link.close()


def test_direct_mode_commits_and_pushes_with_the_same_rules(direct, tmp_path, home):  # noqa: F811
    ws, link, wid = direct
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    rtool.init_repo(ws.store_dir, env=env)
    tips = rperson.PersonState(tmp_path / "pstate" / "records.json")

    def cli(*argv):
        out = io.StringIO()
        code = rcli.main([*argv, "--json"], env, out, link=link, root=ws.root, workspace_id=wid, tips=tips,
                         protocols=WS_FILES, confirm=lambda t: True)  # fmt: skip
        return code, json.loads(out.getvalue())

    code, out = cli("commit")
    assert code == 0 and len(out["commit"]) == 40
    tree = git(ws.store_dir, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "events/workspace.jsonl" in tree and not any(p.startswith(".state") for p in tree)
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(ws.store_dir, "remote", "add", "origin", f"file://{bare}")
    code, out = cli("push", "--initial")
    assert code == 0 and out["pushed"]
    status = cli("status")[1]["detail"]["status"]
    assert status["last"]["commit"] == out["commit"] and status["pending"]["events"] == 0
    # the local reader rules: convenience on, no mark -> refused
    from orch.host import convenience

    convenience.set_enabled(ws.host_state, wid, True, os.getuid())
    ws.store = ws.other()
    ws.new_ticket("more")
    ws.store.close()
    cli("commit")
    code, out = cli("push")
    assert code == 3 and out["code"] == "sign.convenience_readers"


def test_direct_mode_refuses_once_a_host_serves_the_workspace(w):  # noqa: F811
    link = DirectLink(Path(w.lab.root), w.wid, w.lab.state)
    try:
        with pytest.raises(RecordsError) as e:
            link("records.snapshot", {})
        assert e.value.code == "host.required"
    finally:
        link.close()


def test_direct_mode_is_posix_only(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(RecordsError) as e:
        DirectLink(tmp_path, "0" * 32, tmp_path)
    assert e.value.code == "records.unsupported"


# --- review round (#433) ---------------------------------------------------------------------------------------------


def _unit(tmp_path, state, python="/usr/bin/python3"):
    return rtimer.plan({"HOME": str(tmp_path)}, state, platform="darwin", python=Path(python), dev=True)


def _ok_runner(argv):
    return subprocess.CompletedProcess(list(argv), 0, "", "")


def test_opus2_a_differing_timer_is_never_replaced_silently(tmp_path):
    a = rtimer.TimerManager(_unit(tmp_path, tmp_path / "state-a"), run=_ok_runner)
    b = rtimer.TimerManager(_unit(tmp_path, tmp_path / "state-b"), run=_ok_runner)
    c = rtimer.TimerManager(_unit(tmp_path, tmp_path / "state-a", "/opt/other/python3"), run=_ok_runner)
    assert a.install() is True
    with pytest.raises(RecordsError) as e:
        b.install()
    assert e.value.code == "records.timer_conflict" and "state" in e.value.message
    with pytest.raises(RecordsError) as e:
        c.install()
    assert "interpreter" in e.value.message and "state" not in e.value.message
    path = a.plan.files[0][0]
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][6] == str(tmp_path / "state-a")
    assert b.install(replace=True) is True
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][6] == str(tmp_path / "state-b")


def test_opus2_remove_deletes_only_this_state_dirs_timer(tmp_path):
    a = rtimer.TimerManager(_unit(tmp_path, tmp_path / "state-a"), run=_ok_runner)
    b = rtimer.TimerManager(_unit(tmp_path, tmp_path / "state-b"), run=_ok_runner)
    b.install()
    assert a.remove() == "kept" and b.plan.files[0][0].exists()
    assert b.remove() == "removed" and not b.plan.files[0][0].exists()


def test_auto_on_replace_and_a_conflict_through_the_cli(r):
    class Conflict(FakeTimer):
        def install(self, *, replace: bool = False) -> bool:
            self.calls.append(f"install:{replace}")
            if not replace:
                raise RecordsError("records.timer_conflict", "a records timer is installed with another state")
            return True

    t = Conflict()
    code, out = run_cli(r, "auto", "on", timer=t)
    assert code == 3 and out["code"] == "records.timer_conflict" and r.tips.autos() == {}
    code, out = run_cli(r, "auto", "on", "--replace", timer=t)
    assert code == 0 and "install:True" in t.calls


def test_opus3_state_is_the_timers_own_option(r, tmp_path):
    out = io.StringIO()
    code = rcli.main(["commit", "--state", str(tmp_path / "empty"), "--json"], r.env, out)
    assert code == 5 and json.loads(out.getvalue())["code"] == "records.invalid"


def test_opus3_a_served_workspace_refuses_direct_mode_whatever_the_state_dir(w, tmp_path):  # noqa: F811
    from orch.host.service import SERVED_MARKER

    assert (Path(w.lab.root) / ".orch" / ".state" / SERVED_MARKER).is_file()
    other = tmp_path / "empty-state"
    other.mkdir()
    link = DirectLink(Path(w.lab.root), w.wid, other)
    try:
        with pytest.raises(RecordsError) as e:
            link("records.snapshot", {})
        assert e.value.code == "host.required"
    finally:
        link.close()


def test_sonnet_b_a_confirmation_restarts_the_budget(r, monkeypatch):
    import orch.records as records_pkg

    r.init()
    git(r.store, "remote", "add", "origin", r.bare())
    run_cli(r, "commit")
    clock = [100.0]
    monkeypatch.setattr(records_pkg, "MONOTONIC", lambda: clock[0])

    def slow_human(text):
        clock[0] += 3600  # an hour at the prompt
        return True

    out = io.StringIO()
    code = rcli.main(
        ["push", "--initial", "--json"], r.env, out, link=r.link, root=r.root, workspace_id=r.w.wid, tips=r.tips,
        protocols=WS_FILES, confirm=slow_human, state=r.tips.path.parent,
    )  # fmt: skip
    assert code == 0 and json.loads(out.getvalue())["pushed"] is True


def test_the_child_line_keeps_the_commit_when_the_push_fails(r):
    r.init()
    code, out = run_cli(r, "commit", "--push")  # no remote
    jsonschema.validate(out, rcli.CHILD_SCHEMA)
    assert code == 3 and out["code"] == "records.remote_missing" and out["commit"] == git(r.store, "rev-parse", "HEAD")


def test_auto_on_fails_when_the_note_fails(r):
    fake = FakeTimer()

    def link(op, args):
        if op == "records.note":
            raise RecordsError("workspace.unavailable", "the host is gone", exit_=1)
        return r.link(op, args)

    code, out = run_cli(r, "auto", "on", timer=fake, link=link)
    assert code == 1 and out["code"] == "workspace.unavailable" and r.tips.autos() == {}
    assert fake.calls[-1] == "remove"


def test_status_writes_nothing(r):
    r.init()
    run_cli(r, "commit")
    from orch.host import records as host_records

    path = host_records.record_path(r.w.lab.state, r.w.wid)
    before = path.read_bytes()
    run_cli(r, "status")
    assert path.read_bytes() == before


def test_redaction_strips_local_paths():
    from orch.records.git import redact

    text = redact("fatal: '/Users/me/secret/repo' does not appear to be a git repository (https://h/a/b)")
    assert "/Users" not in text and "<path>" in text and "https://h/a/b" in text


def test_the_log_tightens_an_existing_file(tmp_path):
    path = tmp_path / "person" / "records.jsonl"
    path.parent.mkdir(mode=0o700)
    path.write_text("")
    os.chmod(path, 0o644)
    rperson.RunLog(path).write("w" * 32, "start")
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_direct_mode_audit_names_who_acted(direct):
    ws, link, wid = direct
    link(
        "records.export_check",
        {"sha": "a" * 40, "url": "https://example.org/r.git", "ref": "refs/heads/main", "history_convenience": False},
    )
    from orch.host import records as host_records

    doc = host_records.read_record(ws.host_state, wid, os.getuid())
    assert doc["exports"][-1]["by"] == f"direct:{os.getuid()}"


def test_the_served_marker_holds_no_path_or_uid(w):  # noqa: F811
    from orch.host.service import SERVED_MARKER

    marker = Path(w.lab.root) / ".orch" / ".state" / SERVED_MARKER
    assert json.loads(marker.read_text()) == {"v": 1} and marker.stat().st_mode & 0o777 == 0o600
    assert not [p for p in marker.parent.iterdir() if p.name.startswith(f".{SERVED_MARKER}.")]


def test_a_failed_auto_on_restores_the_previous_entry(r):
    r.tips.set_auto(r.w.wid, {"on": True, "every_min": 30, "push": False, "last_run": 123})

    class Broken(FakeTimer):
        def start(self, *, changed: bool) -> None:
            raise RecordsError("records.timer_failed", "launchctl bootstrap failed (5)")

    code, out = run_cli(r, "auto", "on", "--every", "10", "--push", timer=Broken())
    assert code == 1 and out["code"] == "records.timer_failed"
    assert r.tips.auto(r.w.wid) == {"on": True, "every_min": 30, "push": False, "last_run": 123}


def test_status_is_strictly_read_only(r):
    """The dashboard's bounded child: no repo writes and no network. No object written into the records repo (the
    index is compared with diff-index, never write-tree), no records lock file, no run log, and the current push URL
    answered. (It may record the host's convenience mark and takes the store lock in direct mode, like any read.)"""
    r.init()
    r.commit()
    url = r.bare()
    git(r.store, "remote", "add", "origin", url)
    staged = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=r.store, input=b"y\n", capture_output=True, check=True,
        env={"HOME": str(r.home), "PATH": os.environ["PATH"]},
    ).stdout.decode().strip()  # fmt: skip
    git(r.store, "update-index", "--add", "--cacheinfo", f"100644,{staged},notes.txt")

    def snapshot():
        objects = sorted(str(p) for p in (r.store / ".git" / "objects").rglob("*") if p.is_file())
        person = sorted(str(p) for p in (r.tips.path.parent / "person").rglob("*")) if (
            r.tips.path.parent / "person").exists() else []  # fmt: skip
        return objects, person, (r.store / ".git" / "index").read_bytes()

    before = snapshot()
    code, out = run_cli(r, "status")
    assert code == 0, out
    status = out["detail"]["status"]
    assert status["local"]["repo"] == {"state": "index_differs"} and status["configured"] is True
    assert status["remote"]["current_url"] == url and status["remote"]["current_private"] is False
    assert snapshot() == before
