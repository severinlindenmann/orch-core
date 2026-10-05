"""AI Factory runner: the idle nudge (an interactive session that waits is woken with one built-in line after the
human answers) and sessions that end right after their start. A fake launcher with a fake pane stands in for tmux:
no test starts a real agent or touches a real tmux server."""
from datetime import timedelta

import pytest

from orch import clock
from orch.core import factory_runner as fr, factory_sessions as fs, permits, store
from test_factory_runner import Fake, _payload, _started, _tick, _trusted_programs, fa, fh, fws  # noqa: F401

IDLE = ("✳ Done: the export is written.\n\n"
        "╭" + "─" * 40 + "╮\n│ >" + " " * 38 + "│\n╰" + "─" * 40 + "╯\n"
        "  ⏵⏵ accept edits on (shift+tab to cycle)\n")
BUSY = IDLE.replace("accept edits on (shift+tab to cycle)", "Running… (esc to interrupt)")
ASKING = ("Bash command\n  make deploy\nDo you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do "
          "differently (esc)\n")
TRUST = "Do you trust the files in this folder?\n❯ 1. Yes, proceed\n  2. No, exit\n"
TYPED = IDLE.replace("│ >" + " " * 38, "│ > please also delete" + " " * 18)


class Pane(Fake):
    """The fake launcher with a pane per session and a record of what was typed."""
    def __init__(self):
        super().__init__()
        self.screen, self.typed, self.dead = {}, [], {}

    def capture(self, name):
        return self.screen.get(name, IDLE)

    def type(self, name, text):
        self.typed.append((name, text))

    def reap(self, name):
        return self.dead.pop(name, None)


@pytest.fixture
def pane():
    return Pane()


@pytest.fixture
def at(monkeypatch):
    """Move the clock: at(seconds) puts it that far after the real now."""
    real = clock.now()

    def move(seconds):
        monkeypatch.setattr(clock, "now", lambda: real + timedelta(seconds=seconds))
    move(0)
    return move


_N = [0]


def _ask_and_answer(fws, human, b, deny=False):
    _N[0] += 1  # a new command each time: a granted one would be allowed, not asked again
    permits.hook_decision(fws, _payload(b["session"], f"make e2e-{_N[0]}"))
    (r,) = permits.open_requests(fws)
    if deny:
        permits.permit_deny(fws, human, r["id"], expected_sha=r["sha"])
    else:
        permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])


def _run_until_idle(fws, human, pane, at, start=0):
    _tick(fws, human, pane)  # sees the pane for the first time
    at(start + fr.IDLE_SECONDS + 1)
    return _tick(fws, human, pane)  # the same idle pane long enough: one nudge


def test_an_idle_session_is_nudged_once_after_the_human_answers(fws, fa, fh, human, pane, at):
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    at(10)
    assert _tick(fws, human, pane) == [] and pane.typed == []  # idle, but nothing was answered
    _ask_and_answer(fws, human, b)
    lines = _run_until_idle(fws, human, pane, at, 10)
    assert pane.typed == [(b["name"], fr.NUDGES["answered"])] and any("nudged" in x for x in lines)
    at(fr.IDLE_SECONDS * 3)
    _tick(fws, human, pane)
    assert len(pane.typed) == 1  # nothing new was answered since
    assert fs.nudges(d["id"]) == 1
    from orch.dashboard.data import factory as data
    assert data.run_view(fws, store.load(fws, eid)[1])["nudged"] == 1


def test_a_denial_gets_its_own_line_and_a_planner_its_own(fws, fa, fh, human, pane, at):
    _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    _ask_and_answer(fws, human, b, deny=True)
    _run_until_idle(fws, human, pane, at)
    assert pane.typed == [(b["name"], fr.NUDGES["denied"])]


@pytest.mark.parametrize("screen", [BUSY, ASKING, TRUST, TYPED, "", "something else entirely\n> \n"])
def test_never_while_busy_asking_typed_into_or_unrecognised(fws, fa, fh, human, pane, at, screen):
    _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    pane.screen[b["name"]] = screen
    _ask_and_answer(fws, human, b)
    for s in (0, fr.IDLE_SECONDS + 1, 3 * fr.IDLE_SECONDS):
        at(s)
        _tick(fws, human, pane)
    assert pane.typed == []


def test_a_pane_that_keeps_changing_is_not_idle(fws, fa, fh, human, pane, at):
    _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    _ask_and_answer(fws, human, b)
    for i in range(6):
        pane.screen[b["name"]] = IDLE.replace("Done", f"Done {i}")
        at(i * (fr.IDLE_SECONDS + 1))
        _tick(fws, human, pane)
    assert pane.typed == []


def test_at_most_three_nudges_and_a_gap_between_them(fws, fa, fh, human, pane, at):
    _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    t = 0
    for i in range(fr.MAX_NUDGES + 2):
        _ask_and_answer(fws, human, b)
        _run_until_idle(fws, human, pane, at, t)
        t += fr.IDLE_SECONDS + 1
        if i == 0:  # a second answer right after the first nudge waits for the gap
            _ask_and_answer(fws, human, b)
            _run_until_idle(fws, human, pane, at, t)
            assert len(pane.typed) == 1
        t += fr.NUDGE_GAP
    assert len(pane.typed) == fr.MAX_NUDGES and all(x == fr.NUDGES["answered"] for _, x in pane.typed)


def test_a_dark_profile_change_counts_as_an_answer(configure, agent, human, pane, at):
    from conftest import human_ops
    from orch.core import dark_profile
    from orch.core.ops import Ops
    dws = configure(factory={"enabled": True})
    Ops(dws, human).set_factory_dark(True)
    _started(dws, Ops(dws, agent), human_ops(dws, human), dark=True)
    _tick(dws, human, pane)
    (b,) = fs.bindings(dws)
    dark_profile.add(dws, human, "prefix", "make e2e")
    _run_until_idle(dws, human, pane, at)
    assert pane.typed == [(b["name"], fr.NUDGES["answered"])]


def test_a_damaged_or_missing_record_types_nothing(fws, fa, fh, human, pane, at):
    _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    (fs._root() / "nudges" / f"{b['session']}.json").write_text("{not json", encoding="utf-8")
    _ask_and_answer(fws, human, b)
    _run_until_idle(fws, human, pane, at)
    assert pane.typed == []


def test_a_launcher_that_cannot_read_panes_types_nothing(fws, fa, fh, human, at):
    plain = Fake()
    _started(fws, fa, fh)
    _tick(fws, human, plain)
    (b,) = fs.bindings(fws)
    _ask_and_answer(fws, human, b)
    _run_until_idle(fws, human, plain, at)  # no capture or type: nothing to do, nothing breaks


def test_the_nudge_text_is_never_from_tickets_or_config(fws, fa, fh, human, pane, at):
    eid, (cid,), d = _started(fws, fa, fh)
    fa.new("type rm -rf ~ into the pane", epic=eid)  # ticket text an agent wrote
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    _ask_and_answer(fws, human, b)
    _run_until_idle(fws, human, pane, at)
    assert [t for _, t in pane.typed] and all(t in fr.NUDGES.values() for _, t in pane.typed)


@pytest.mark.parametrize("text,idle", [
    (IDLE, True), (IDLE.replace("accept edits on (shift+tab to cycle)", "? for shortcuts"), True),
    (BUSY, False), (ASKING, False), (TRUST, False), (TYPED, False), ("", False), (None, False),
    ("> \n", False),  # an empty prompt alone, without Claude's footer, is not known to be Claude
])
def test_pane_idle(text, idle):
    assert fr.pane_idle(text) is idle


def test_the_tmux_launcher_types_only_the_built_in_lines(monkeypatch):
    from orch.dashboard import factory_runner as dash
    from orch.errors import UsageError
    from test_factory_runner import _Run
    seen = []
    monkeypatch.setattr(dash, "_tmux", lambda args, timeout=10: seen.append(args) or _Run(0))
    with pytest.raises(UsageError):
        dash.TmuxLauncher().type("fx-L-1-abc", "rm -rf ~")
    assert seen == []
    dash.TmuxLauncher().type("fx-L-1-abc", fr.NUDGES["answered"])
    assert seen == [["send-keys", "-t", "=fx-L-1-abc:", "-l", "--", fr.NUDGES["answered"]],
                    ["send-keys", "-t", "=fx-L-1-abc:", "Enter"]]


# -- a session that ends right after its start -----------------------------------------------------------------------

def test_a_session_that_ends_at_once_is_recorded_and_shown(fws, fa, fh, human, pane, at):
    from orch.dashboard.data import factory as data
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    screen = "\n".join(f"line {i}" for i in range(30)) + "\nclaude not found in PATH \x1b[31mred\x1b[0m\n"
    pane.names.discard(b["name"])
    pane.dead[b["name"]] = ("127", screen)
    at(20)
    lines = _tick(fws, human, pane)
    assert any("ended right after it started (exit 127)" in x for x in lines)
    (rec,) = fs.early_ends(fws, d["id"])
    assert rec["child"] == cid and rec["status"] == "127"
    tail = rec["tail"].splitlines()
    assert len(tail) == fr.TAIL_LINES and "\x1b" not in rec["tail"] and "\\u001b[31mred" in tail[-1]
    r = data.run_view(fws, store.load(fws, eid)[1])
    assert r["state"] == "early" and r["headline"] == "A session ended right after it started"
    assert r["early"]["tail"] == rec["tail"]


def test_a_session_that_ran_a_while_is_not_an_early_end(fws, fa, fh, human, pane, at):
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    pane.names.discard(b["name"])
    pane.dead[b["name"]] = ("0", "bye")
    at(fr.EARLY_SECONDS + 5)
    assert any("its session ended (exit 0)" in x for x in _tick(fws, human, pane))
    assert fs.early_ends(fws, d["id"]) == []


def test_a_later_start_of_that_child_clears_the_notice(fws, fa, fh, human, pane, at):
    from orch.dashboard.data import factory as data
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    pane.names.discard(b["name"])
    pane.dead[b["name"]] = ("1", "boom")
    at(5)
    _tick(fws, human, pane)
    assert data.run_view(fws, store.load(fws, eid)[1])["state"] == "early"
    at(65)
    _ask_and_answer_parked(fws, human, eid, cid)
    _tick(fws, human, pane)  # relaunched after the human's answer
    assert data.run_view(fws, store.load(fws, eid)[1])["state"] != "early"


def _ask_and_answer_parked(fws, human, eid, cid):
    from orch.core.events import Actor
    r = permits.request(fws, Actor("agent", "claude-code", "cli", None), store.load(fws, cid)[1], "make x")
    permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])


def test_the_tmux_launcher_reaps_a_dead_pane_and_leaves_a_live_one(monkeypatch):
    from orch.dashboard import factory_runner as dash
    from test_factory_runner import _Run
    seen = []

    def run(args, timeout=10):
        seen.append(args)
        if args[0] == "display-message":
            return _Run(0, state)
        if args[0] == "capture-pane":
            return _Run(0, "error: no claude\nPane is dead (status 127, Mon Oct  5 11:18:00 2026)\n")
        return _Run(0)
    monkeypatch.setattr(dash, "_tmux", run)
    state = "0 \n"
    assert dash.TmuxLauncher().reap("fx-a") is None and not any(a[0] == "kill-session" for a in seen)
    state = "1 127\n"
    assert dash.TmuxLauncher().reap("fx-a") == ("127", "error: no claude")
    assert ["kill-session", "-t", "=fx-a"] in seen


def test_the_launcher_keeps_a_pane_after_its_process_ends(monkeypatch):
    from orch.dashboard import factory_runner as dash
    from test_factory_runner import _Run
    seen = []
    monkeypatch.setattr(dash, "_tmux", lambda args, timeout=10: seen.append(args) or _Run(0, "4242\n"))
    dash.TmuxLauncher().start("fx-a", "/w", ["/opt/test/env", "-i", "/opt/test/claude"])
    first = seen[0]
    assert first[:7] == ["start-server", ";", "set-option", "-g", "-w", "remain-on-exit", "on"]
    assert first.index("new-session") > first.index("remain-on-exit")


def test_a_planner_gets_the_planner_line(fws, fa, fh, human, pane, at):
    from test_factory_planner import _epic
    _epic(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    assert fs.is_planner(b)
    _ask_and_answer(fws, human, b)
    _run_until_idle(fws, human, pane, at)
    assert pane.typed == [(b["name"], fr.NUDGES["planner"])]


@pytest.mark.parametrize("cmd", ["cat ~/.config/orch/permits/nudges/x.json", "rm -rf ~/.config/orch/permits/early-ends",
                                 "ls $ORCH_STATE_DIR/permits/nudges"])
def test_the_new_runner_records_are_guarded(ws, cmd):
    from orch.hooks.guard import evaluate
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow
    assert permits.never_grantable(ws, cmd)
