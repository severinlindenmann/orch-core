"""Dark AI Factory end to end, in one process, with a scripted agent per session (tests/factory_e2e.py): a request,
the planner splitting it, two workers in their own clones, Ready, the release with its real executor and the example
release-step script into a bare remote on disk, dev, production and the close by the charter; then each failure the
live runs showed, scripted, with the safe outcome; and a seeded sweep over the order of session steps and runner
rounds. No network, no tmux, no claude."""
import json
import os
import random
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

import orch
from orch import clock
from orch.core import dark_profile, epics, factory_clones as fcl, factory_close, factory_release as fr, \
    factory_report, factory_runner, factory_sessions as fs, ledger, permits, store
from orch.core.ops import Ops
from factory_e2e import STOP, TRUST, World, deliverable, worked_commit, worker
from test_factory_release import _g

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
DOCS = Path(orch.__file__).parents[2] / "docs"
REAL_RESOLVE = factory_runner.resolve_bin
FILES = ["elephants.json", "elephants.html"]
ASK = "Build one page elephants.html that reads its data from elephants.json."
DONE_WHEN = "- [ ] elephants.html shows the elephants\n- [ ] elephants.json holds the data"
SIGNED = {"release": "prod", "rollback": True, "close": True}


@pytest.fixture(autouse=True)
def _not_stopping():
    fr._STOPPING.clear()
    yield
    fr._STOPPING.clear()


@pytest.fixture
def make_world(configure, human, ws_root, tmp_path, monkeypatch):
    """make_world(charter): a Dark factory workspace (a git checkout on main, pushed to a bare remote), Dark on with
    both baselines, the example recipe with its release-step script (the script's state folder beside it), and an
    epic the human started from New ticket with `charter` (default: release prod, rollback and close)."""
    def _make(charter=None):
        rs = tmp_path / "rs" / "bin"
        rs.mkdir(parents=True)
        shutil.copy(DOCS / "examples" / "release-step", rs / "release-step")
        (rs / "release-step").chmod(0o755)
        monkeypatch.setenv("PATH", f"{rs}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: REAL_RESOLVE(name) if os.path.basename(
            name) in ("git", "release-step") else f"/opt/test/{os.path.basename(name)}")
        monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: None)
        ws = configure(factory={"enabled": True}, git={"agent_may": {"commit": True}})
        _g(ws_root, "init", "-q", "-b", "main")
        _g(ws_root, "add", "orchestrator/config.json")
        _g(ws_root, "commit", "-q", "-m", "base")
        remote = tmp_path / "remote.git"
        _g(tmp_path, "clone", "-q", "--bare", str(ws_root), str(remote))
        Ops(ws, human).set_factory_dark(True)
        dark_profile.add_baseline(ws, human)
        dark_profile.add_baseline(ws, human, name="git-basic")
        rec = json.loads((DOCS / "examples" / "factory-release-live-test.json").read_text(encoding="utf-8"))
        fr.set_recipe(ws, human, {**rec, "remote": str(remote)})
        w = World(ws, human, monkeypatch, remote, rs.parent / "state", FILES)
        w.epic = start(w, SIGNED if charter is None else charter)
        return w
    return _make


@pytest.fixture
def world(make_world):
    return make_world()


def start(w, charter):
    """The human's New ticket in Dark mode: an epic whose Requirements are the ask and whose criteria are the done-when
    text, the signed Dark charter, and the runner armed (the dashboard's start)."""
    from conftest import human_ops
    e = Ops(w.ws, w.human).new("Elephants", type="epic", sections={"Requirements": ASK,
                                                                  "Acceptance criteria": DONE_WHEN})
    human_ops(w.ws, w.human).approve(e.id, "requirements", delegate={"factory": True, "dark": True, **charter})
    fs.arm(w.ws, w.human, epics.delegation(w.ws, store.load(w.ws, e.id)[1])["id"])
    return e.id


def edit_worker(w, name, edit):
    """The worker of the child whose criteria name `name` runs its script as `edit(script, agent)` changes it."""
    def make(a):
        s = worker(a)
        return edit(s, a) if deliverable(w.ws, a.child) == name else s
    w.scripts["worker"] = make


def _epic(w):
    return store.load(w.ws, w.epic)[1]


def _kid(w, name):
    return next(c.id for c in epics.children(w.ws, w.epic) if deliverable(w.ws, c.id) == name)


def _status(w, tid):
    return store.load(w.ws, tid)[1].status


def _states(w):
    e = _epic(w)
    return {s["name"]: s["state"] for s in fr.status(w.ws, e, permits.factory_delegation(w.ws, e))["stages"]}


def _stopped(w):
    return [r["code"] for r in factory_report.stopped(w.ws, _epic(w))]


def _remote_main(w):
    return _g(w.remote, "rev-parse", "refs/heads/main")


def _on_remote(w, sha):
    return _g(w.remote, "merge-base", "--is-ancestor", sha, _remote_main(w)) == ""


def _tip(w, kid):
    return _g(fcl.clone_dir(w.ws, kid), "rev-parse", "HEAD")


def _findings(w):
    from orch.core.check import run_checks
    return {(f.level, f.code) for f in run_checks(w.ws, emit_events=False)}


def _view(w):
    from orch.dashboard.data import factory as data
    return data.run_view(w.ws, _epic(w))


def _page(w):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(w.ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c, " ".join(c.get(f"/factory/{w.epic}").text.split())


def _release_log(w):
    p = w.state / "release.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _merges(w):
    return [ln.split()[-1] for ln in _release_log(w) if " merge " in ln]  # the sha of each merge step that ran


# -- 1. the happy path ------------------------------------------------------------------------------------------------

def assert_released_and_closed(w):
    kids = [c.id for c in epics.children(w.ws, w.epic)]
    assert len(kids) == 2 and {deliverable(w.ws, k) for k in kids} == set(FILES)  # one child per named file
    assert _states(w) == {"merge": "proven", "dev": "proven", "production": "proven"}, w.lines
    e = _epic(w)
    assert e.status == "done" and factory_close.closed_by_charter(w.ws, e), w.lines
    for k in kids:
        assert _status(w, k) == "done" and _on_remote(w, _tip(w, k))
    assert set(FILES) <= set(_g(w.remote, "ls-tree", "--name-only", "refs/heads/main").split())
    dev = fr.unit_state(w.ws, w.epic, "dev", w.epic)["base_sha"]
    assert dev == _remote_main(w) == (w.state / "production.version").read_text().strip()
    assert sorted(_merges(w)) == sorted(_tip(w, k) for k in kids)  # each child merged exactly once


def test_the_whole_chain_runs_without_a_single_card_and_closes_by_itself(world):
    w = world
    w.settle()
    assert w.requests() == {}  # not one permission card in the whole run
    for s in w.launcher.sessions.values():
        assert s.agent.denied() == [], (s.agent.child, s.agent.denied())
    assert w.launcher.typed == []  # nobody waited for anything, so nothing was nudged
    assert_released_and_closed(w)
    assert not [f for f in _findings(w) if f[0] == "error"], _findings(w)
    # the human's Reopen: open again, the charter paused, sessions never come back and it never closes by itself again
    c, _ = _page(w)
    r = c.post(f"/factory/{w.epic}/reopen", data={"reason": "not done"}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert _epic(w).status == "open" and epics.delegation(w.ws, _epic(w))["paused"]
    n = len(w.launcher.sessions)
    w.settle()
    assert _epic(w).status == "open" and len(w.launcher.sessions) == n and not w.launcher.alive()


# -- 2. every failure the live runs showed, scripted ----------------------------------------------------------------

def test_cd_then_orch_is_denied_with_the_hint_and_the_plain_retry_works(world):
    w = world
    edit_worker(w, "elephants.json", lambda s, a: s.before("claim", ("cd", lambda: a.bash(
        f'cd "{w.ws.root}" && orch claim {a.child}'))))
    w.settle()
    kid = _kid(w, "elephants.json")
    (bad,) = w.session(kid).agent.denied()
    assert bad.by == "hook" and permits.CD_HINT in bad.message and "Run just the orch command" in bad.message
    (card,) = w.cards()  # the chain's card, and no other: the plain retry and everything after ran without one
    assert card["command"].startswith("cd ") and permits.CD_HINT in card["reason"]
    assert _status(w, kid) == "testing" and _epic(w).status == "open"  # an open card holds the close
    w.deny(card["id"])  # the human clears it
    w.settle()
    assert w.cards() == []
    assert_released_and_closed(w)


def test_git_add_and_commit_chained_is_denied_with_the_hint_and_two_commands_work(world):
    w = world
    edit_worker(w, "elephants.html", lambda s, a: s.before("add", ("chain", lambda: a.bash(
        f"git add elephants.html && {worked_commit(a.prompt)}"))))
    w.settle()
    kid = _kid(w, "elephants.html")
    (bad,) = w.session(kid).agent.denied()
    assert bad.by == "hook" and permits.GIT_HINT in bad.message
    assert [c["command"] for c in w.cards()] == [bad.text]
    assert _status(w, kid) == "testing" and _tip(w, kid) != _g(w.ws.root, "rev-parse", "main")


def test_a_commit_without_risk_is_refused_at_commit_time_with_the_worked_example(world):
    w = world
    edit_worker(w, "elephants.json", lambda s, a: s.before("commit", ("no risk", lambda: a.bash(
        f'git commit -m "{a.child} Add elephants data file" -m "What: elephants.json" -m "Why: the page reads it"'))))
    w.settle()
    kid = _kid(w, "elephants.json")
    (bad,) = w.session(kid).agent.denied()
    assert bad.by == "guard" and "body needs a 'Risk:' line" in bad.message
    assert f'Run: git commit -m "{kid} Add the requested file"' in bad.message
    assert w.requests() == {}  # the guard refused it: no card, the worked example then committed
    assert_released_and_closed(w)


def test_pipes_and_redirects_for_evidence_are_denied_and_the_file_tool_works(world):
    w = world

    def edit(s, a):
        return s.before("evidence",
                        ("pipe", lambda: a.bash(f"orch show {a.child} | head -30")),
                        ("redirect", lambda: a.bash(f"printf '%s' '- AC1: ok' > {a.child}-verification.md")))
    edit_worker(w, "elephants.json", edit)
    w.settle()
    kid = _kid(w, "elephants.json")
    pipe, redirect = w.session(kid).agent.denied()
    assert pipe.by == "hook" and "not in the Dark profile" in pipe.message and "Do not wait" in pipe.message
    assert redirect.by in ("guard", "hook")
    assert not (fcl.clone_dir(w.ws, kid) / f"{kid}-verification.md").exists()  # never run
    assert pipe.text in [c["command"] for c in w.cards()]
    assert _status(w, kid) == "testing"  # the Write tool and --file did the job
    assert "AC1: read elephants.json" in store.load(w.ws, kid)[1].section("Verification")


@pytest.mark.parametrize("when", ["before its move", "after its move"])
def test_two_children_adding_one_file_are_told_at_the_move_or_stopped_before_any_merge(world, when):
    w = world
    copy = [("copy", lambda a: a.write("elephants.json", "[1]\n")),  # the page child's own copy of the data file
            ("add copy", lambda a: a.ok("git add elephants.json")),
            ("commit copy", lambda a: a.ok(worked_commit(a.prompt)))]

    def edit(s, a):
        steps = [(n, lambda f=f: f(a)) for n, f in copy]
        return s.after("commit", *steps) if when == "before its move" else s.after("move", *steps)
    edit_worker(w, "elephants.html", edit)
    w.settle()
    data, page = _kid(w, "elephants.json"), _kid(w, "elephants.html")
    move = next(c for c in w.session(page).agent.calls if c.text == f"orch move {page} testing")
    assert w.requests() == {} and not _merges(w) and _epic(w).status == "open"
    assert _remote_main(w) == _g(w.ws.root, "rev-parse", "main")
    if when == "before its move":  # told while it can still fix it
        assert move.code != 0 and f"you add elephants.json, which {data} adds too" in move.out, move
        assert "say so with orch log" in move.out and "Delete it" not in move.out  # advice the session can follow
        assert _status(w, page) == "in-progress"
        return
    _, html = _page(w)  # it committed the copy after its move: Ready says it, the release stops before any merge
    assert f"{data} and {page} both add elephants.json: the release will conflict" in html
    assert "Release could not start" in html and _states(w)["merge"] != "proven"


def test_uncommitted_work_keeps_the_child_out_of_testing(world):
    w = world
    edit_worker(w, "elephants.json", lambda s, a: s.drop("add", "commit"))
    w.settle()
    kid = _kid(w, "elephants.json")
    move = w.session(kid).agent.calls[-1]
    assert move.code != 0 and "your clone has work that is not committed (elephants.json)" in move.out
    assert _status(w, kid) == "in-progress" and _epic(w).status == "open" and not _merges(w)


def test_a_misspelled_file_is_named_at_ready_and_blocks_the_merge_with_the_near_match(world):
    w = world
    w.scripts["worker"] = lambda a: worker(a, name="elepthants.json") if deliverable(
        w.ws, a.child) == "elephants.json" else worker(a)
    w.settle()
    _, html = _page(w)
    assert "Not in any child's commit: elephants.json (found similar: elepthants.json)" in html
    assert "Release could not start" in html
    assert not _merges(w) and _epic(w).status == "open"


def test_a_session_that_stops_after_a_denial_is_shown_stalled_and_the_nudge_names_the_answer(world):
    w = world
    seen = []

    def edit(s, a):
        def stubborn():  # the third live run: it waited "for approvals" and ignored the nudge too
            return STOP
        return s.before("claim", ("pipe", lambda: a.bash(f"orch show {a.child} | head -30")),
                        ("wait", lambda: STOP), ("read nudge", lambda: seen.append(a.nudges[-1])),
                        ("ignore it", stubborn))
    edit_worker(w, "elephants.json", edit)
    w.settle()
    kid = _kid(w, "elephants.json")
    (card,) = w.cards()
    assert _view(w)["state"] == "waiting"  # an open card: needs you
    w.grant(card["id"])  # (a denial would make it Stopped: a denied request holds the child back)
    w.runner()  # sees the pane idle after the answer
    w.at(factory_runner.IDLE_SECONDS + 1)
    w.runner()  # unchanged long enough: the nudge
    w.run_sessions()
    assert seen and seen[0].startswith(f"A person answered your permission request: {card['id']} granted.")
    w.runner()
    w.at(2 * factory_runner.IDLE_SECONDS + 2)
    w.runner()
    v = _view(w)
    assert v["state"] == "stalled" and f"{kid} is idle and still open" in v["headline"], v["headline"]
    assert len([n for n, _ in w.launcher.typed if n == w.session(kid).name]) == 1  # one answer, one nudge


def test_no_nudge_while_the_human_types_into_the_pane(world):
    from orch.dashboard import factory_runner as dfr
    w = world
    edit_worker(w, "elephants.json", lambda s, a: s.before("claim", ("pipe", lambda: a.bash(
        f"orch show {a.child} | head -30")), ("wait", lambda: STOP)))
    w.settle()
    kid = _kid(w, "elephants.json")
    name = w.session(kid).name
    w.deny(w.cards()[0]["id"])
    dfr.note_human_keys(name)
    mine = lambda: [t for n, t in w.launcher.typed if n == name]  # noqa: E731
    try:
        w.runner()
        w.at(factory_runner.IDLE_SECONDS + 1)
        w.runner()
        assert mine() == []
        dfr.HUMAN_KEYS[name] -= dfr.HUMAN_QUIET + 1  # a minute later: the human stopped typing
        w.runner()
        assert len(mine()) == 1 and " denied." in mine()[0]
    finally:
        dfr.HUMAN_KEYS.pop(name, None)
    w.settle()
    assert_released_and_closed(w)  # the nudge woke it, it finished, and the run went on to the end


def test_a_session_at_the_trust_question_is_said_and_never_typed_into(world):
    w = world
    w.runner()
    w.run_sessions()  # the planner splits the epic
    w.runner()  # the first child starts in its clone
    s = next(x for x in w.live() if not fs.is_planner(x.agent.b))
    s.screen = TRUST.format(path=s.cwd)
    lines = w.runner()
    assert any("waits at Claude's folder-trust question" in x for x in lines)
    assert _view(w)["state"] == "trust" and _view(w)["chip"] == "Trust question"
    w.at(factory_runner.IDLE_SECONDS * 10)
    w.runner()
    assert w.launcher.typed == [] and s.agent.calls == []


def test_accepting_before_the_release_needs_close_without_releasing(world):
    from conftest import human_ops
    from orch.errors import ValidationError
    w = world
    w.settle(release=False)
    assert factory_report.ready(w.ws, _epic(w)) is not None
    h = human_ops(w.ws, w.human)
    with pytest.raises(ValidationError, match="The release has not run; closing now skips it"):
        h.verdict(w.epic, "done")
    h.verdict(w.epic, "done", skip_release="the demo is today")
    entry = next(e for e in reversed(ledger.entries(w.ws)) if e.get("ticket") == w.epic and e.get("kind") == "verdict")
    assert entry["release_skipped"] == "the demo is today"
    assert entry["skipped_stages"] == ["merge", "dev", "production"]
    assert _view(w)["headline"] == "You gave the verdict: closed without release"
    w.settle()
    assert not _merges(w) and ("info", "closed-without-release") in _findings(w)


@pytest.mark.parametrize("signed_rollback", [True, False])
def test_a_failing_production_check_rolls_back_only_when_signed(make_world, signed_rollback):
    w = make_world(SIGNED if signed_rollback else {"release": "prod", "close": True})
    w.state.mkdir(parents=True, exist_ok=True)
    (w.state / "fail-production-check").write_text("", encoding="utf-8")
    w.settle()
    version = (w.state / "production.version").read_text().strip()
    if signed_rollback:
        assert version == "rolled-back" and _stopped(w) == ["rolled-back"]
    else:
        assert version != "rolled-back" and not any(" rollback " in x for x in _release_log(w))
        assert _stopped(w) == ["production-failed"]
    assert _states(w)["dev"] == "proven" and _epic(w).status == "open"


def test_production_waits_for_its_window_and_goes_once_it_opens(world):
    w = world
    fr._atomic(fr._window_path(w.ws), json.dumps({"at": clock.stamp_s(clock.now() - timedelta(minutes=30)),
                                                  "epic": "X-1"}))  # another production half an hour ago
    w.settle()
    assert _states(w) == {"merge": "proven", "dev": "proven", "production": "waiting"}
    assert _view(w)["state"] == "window" and _epic(w).status == "open"
    w.at(31 * 60)
    w.settle()
    assert_released_and_closed(w)


def test_a_crash_between_a_merge_and_its_record_never_merges_twice(world):
    w = world
    w.settle(release=False)
    crashed = []

    def run(argv, cwd, env, timeout, started=None, **kw):
        r = fr.run_command(argv, cwd, env, timeout, started=started, **kw)
        if len(argv) > 1 and argv[1] == "merge" and not crashed:
            crashed.append(argv[-1])
            raise KeyboardInterrupt  # the dashboard dies after the merge ran, before its outcome is recorded
        return r
    with pytest.raises(KeyboardInterrupt):
        w.release(run)
    restarted = World(w.ws, w.human, w.mp, w.remote, w.state, FILES)  # a new dashboard: new runner objects
    restarted.epic = w.epic
    restarted.settle()
    assert _merges(w) == crashed and _on_remote(w, crashed[0])  # merged once, never again
    assert "release-unknown" in _stopped(w) and _epic(w).status == "open"


# -- 3. the order of everything, swept --------------------------------------------------------------------------------

# what `orch check` reports for a run the charter closed: the delegated approvals and the charter's verdict, as info
EXPECTED_INFO = {"charter-verdict", "delegated-approval"}


@pytest.mark.parametrize("seed", range(20))
def test_any_order_of_session_steps_and_runner_rounds_keeps_the_invariants(world, seed):
    w = world
    rnd = random.Random(seed)
    for _ in range(400):
        live = [s for s in w.live() if not s.done]
        roll = rnd.random()
        if live and roll < 0.7:
            w.step(rnd.choice(live))
        elif roll < 0.9:
            w.runner()
        else:
            w.release()
        e = _epic(w)
        if e.status == "done":  # never closed without every stage proven
            assert set(_states(w).values()) == {"proven"}, (seed, w.lines)
        assert len(_merges(w)) == len(set(_merges(w))), (seed, w.lines)  # no double merge
        if e.status == "done" and not w.live():
            break
    w.settle()
    assert w.requests() == {}, seed
    assert_released_and_closed(w)
    assert ledger.head_ok()
    found = _findings(w)
    assert not [f for f in found if f[0] == "error"], (seed, found)
    assert {c for lvl, c in found if lvl != "error"} == EXPECTED_INFO and {lvl for lvl, _ in found} == {"info"}, \
        (seed, found)
