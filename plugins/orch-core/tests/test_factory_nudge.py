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


def _echoed(text):
    """An idle screen whose transcript still shows an earlier nudge as an echoed prompt line above the input box."""
    return "> " + text + "\n\n" + IDLE


UNKNOWN_MENU = "Choose what to do next:\n  1. Keep going\n  2. Stop\n"


class Pane(Fake):
    """The fake launcher with a pane per session and a record of what was typed."""
    def __init__(self):
        super().__init__()
        self.screen, self.typed, self.dead = {}, [], {}

    def capture(self, name):
        return self.screen.get(name, IDLE)

    def type(self, name, text):
        self.typed.append((name, text))
        return not self.race  # True: Enter was pressed; False: the pane changed while typing

    race = False

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
    (_echoed(fr.NUDGES["answered"]), True), (IDLE, True), (IDLE.replace("accept edits on (shift+tab to cycle)", "? for shortcuts"), True),
    (BUSY, False), (ASKING, False), (TRUST, False), (TYPED, False), ("", False), (None, False),
    ("> \n", False),  # an empty prompt alone, without Claude's footer, is not known to be Claude
])
def test_pane_idle(text, idle):
    assert fr.pane_idle(text) is idle


def _typed_screen(text):
    return IDLE.replace("\u2502 >" + " " * 38, "\u2502 > " + text[:60])


@pytest.mark.parametrize("before,after,enter", [
    (IDLE, ["typed"], True),  # the text sits on the input line: Enter
    (IDLE, [IDLE, IDLE, "typed"], True),  # the redraw lags: read again, then Enter
    (IDLE, [IDLE] * 6, False),  # it never lands
    (IDLE, ["echo"] * 6, False),  # an earlier nudge echoed in the transcript is not the input line
    (IDLE, ["menu"], False),  # a permission menu appeared meanwhile: Enter would pick its option
    (IDLE, [UNKNOWN_MENU] * 6, False),  # a menu orch does not know: no input box, no Enter
    (IDLE, [BUSY] * 6, False), (IDLE, [TRUST] * 6, False), (IDLE, [None] * 6, False),
    (TYPED, ["typed"], False),  # the input line was not empty before: nothing is typed at all
    (_echoed("old"), ["typed"], True),  # an echo above the box does not stop a nudge that lands in it
])
def test_the_tmux_launcher_presses_enter_only_where_the_text_landed(monkeypatch, before, after, enter):
    from orch.dashboard import factory_runner as dash
    from orch.errors import UsageError
    from test_factory_runner import _Run
    nudge = fr.NUDGES["answered"]
    named = {"typed": _typed_screen(nudge), "menu": _typed_screen(nudge) + ASKING, "echo": _echoed(nudge)}
    screens = [before, *(named.get(x, x) if isinstance(x, str) else x for x in after)]
    seen, slept = [], []

    def tmux(args, timeout=10):
        seen.append(args)
        if args[0] == "capture-pane":
            screen = screens.pop(0) if len(screens) > 1 else screens[0]
            return _Run(0 if screen is not None else 1, screen or "")
        return _Run(0)
    monkeypatch.setattr(dash, "_tmux", tmux)
    monkeypatch.setattr(dash, "_sleep", slept.append)
    with pytest.raises(UsageError):
        dash.TmuxLauncher().type("fx-L-1-abc", "rm -rf ~")
    assert seen == []
    assert dash.TmuxLauncher().type("fx-L-1-abc", nudge) is enter
    keys = [a for a in seen if a[0] == "send-keys"]
    if before is TYPED:
        assert keys == []
        return
    assert keys[0] == ["send-keys", "-t", "=fx-L-1-abc:", "-l", "--", nudge]
    assert keys[1:] == ([["send-keys", "-t", "=fx-L-1-abc:", "Enter"]] if enter
                        else [["send-keys", "-t", "=fx-L-1-abc:", "C-u"]])
    assert len(slept) <= dash.TYPE_POLLS and sum(slept) <= 1.5


@pytest.mark.parametrize("text,line", [
    (IDLE, ""), (TYPED, "please also delete"), (_echoed("x"), ""), ("> stale\n", None), (UNKNOWN_MENU, None),
    (None, None), ("\u2500" * 9 + "\n\u276f \n" + "\u2500" * 9 + "\n", ""),
    (IDLE + "Select a model\n  Opus\n\u203a Sonnet\n", None),  # a picker drawn below the box
])
def test_the_input_line_is_the_one_inside_the_box(text, line):
    assert fr.input_line(text) == line


def test_a_nudge_the_pane_changed_under_counts_as_an_attempt(fws, fa, fh, human, pane, at):
    _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    pane.race = True
    _ask_and_answer(fws, human, b)
    lines = _run_until_idle(fws, human, pane, at)
    assert any("nudge not sent" in x for x in lines) and fs.nudge_record(b["session"])["count"] == 1
    at(fr.IDLE_SECONDS * 4)
    _tick(fws, human, pane)
    assert len(pane.typed) == 1  # not retried until something new is answered


def test_revocations_and_profile_removals_are_not_answers(configure, agent, human, pane, at):
    from conftest import human_ops
    from orch.core import dark_profile
    from orch.core.ops import Ops
    dws = configure(factory={"enabled": True})
    Ops(dws, human).set_factory_dark(True)
    dark_profile.add(dws, human, "prefix", "make e2e")
    _started(dws, Ops(dws, agent), human_ops(dws, human), dark=True)
    _tick(dws, human, pane)
    (b,) = fs.bindings(dws)
    dark_profile.remove(dws, human, dark_profile.rules(dws)[0]["id"])
    _run_until_idle(dws, human, pane, at)
    assert pane.typed == []
    permits.hook_decision(dws, _payload(b["session"], "make other"))
    (r,) = permits.open_requests(dws)
    g = permits.permit_grant(dws, human, r["id"], "epic", expected_sha=r["sha"])
    _run_until_idle(dws, human, pane, at, fr.IDLE_SECONDS + 1)
    assert len(pane.typed) == 1  # the grant
    permits.permit_revoke(dws, human, g["grant"])
    _run_until_idle(dws, human, pane, at, 2 * fr.IDLE_SECONDS + fr.NUDGE_GAP)
    assert len(pane.typed) == 1  # the revocation answers nothing


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


# -- the run view tells sessions waiting at their prompt from working ones ----------------------------------------------

def _state(fws, eid):
    from orch.dashboard.data import factory as data
    return data.run_view(fws, store.load(fws, eid)[1])


def test_the_run_view_says_idle_when_every_session_waits_at_its_prompt(fws, fa, fh, human, pane, at):
    eid, kids, d = _started(fws, fa, fh, n=2)
    _tick(fws, human, pane)  # starts them
    _tick(fws, human, pane)  # reads their screens
    assert len(fs.bindings(fws)) == 2
    assert _state(fws, eid)["state"] == "working"  # seen idle just now: not long enough
    at(fr.IDLE_VIEW_SECONDS + 1)
    _tick(fws, human, pane)  # same screens: nothing changes in the records
    r = _state(fws, eid)
    assert r["state"] == "asleep" and r["headline"] == "Sessions are waiting at their prompt: nothing is running"
    assert r["chip"] == "Idle at prompt" and not r["live"]
    one = fs.bindings(fws)[0]["name"]
    pane.screen[one] = BUSY  # one starts working again
    _tick(fws, human, pane)
    assert _state(fws, eid)["state"] == "working"


@pytest.mark.parametrize("damage", ["no capture", "damaged record", "unreadable pane"])
def test_unknown_is_never_called_idle(fws, fa, fh, human, at, damage):
    p = Fake() if damage == "no capture" else Pane()
    eid, kids, d = _started(fws, fa, fh)
    _tick(fws, human, p)
    (b,) = fs.bindings(fws)
    if damage == "damaged record":
        (fs._root() / "nudges" / f"{b['session']}.json").write_text("{", encoding="utf-8")
    if damage == "unreadable pane":
        p.screen[b["name"]] = None
    at(fr.IDLE_VIEW_SECONDS * 3)
    _tick(fws, human, p)
    assert _state(fws, eid)["state"] == "working"


def test_the_run_view_page_says_it_and_the_nudge_count(fws, fa, fh, human, pane, at):
    from test_dark_dashboard import _client
    eid, kids, d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    _tick(fws, human, pane)
    at(fr.IDLE_VIEW_SECONDS + 1)
    _tick(fws, human, pane)
    html = _client(fws).get(f"/factory/{eid}").text
    assert "data-asleep" in html and "Sessions are waiting at their prompt: nothing is running" in html


# -- Claude's folder-trust question: said once, never answered ----------------------------------------------------------

NEW_TRUST = ("Accessing workspace:\n\n /Users/x/.config/orch-clones/w/L-0002/repo\n\n Is this a project you trust?\n"
             " ❯ 1. Yes, I trust this folder\n   2. No, exit\n\n Enter to confirm · Esc to cancel\n")


@pytest.mark.parametrize("screen", [NEW_TRUST, "Some output above\n" + NEW_TRUST])
def test_a_session_at_the_trust_question_is_said_once_and_never_answered(fws, fa, fh, human, pane, at, screen):
    from test_dark_dashboard import _client
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    pane.screen[b["name"]] = screen
    lines = _tick(fws, human, pane)
    want = (f"{cid} waits at Claude's folder-trust question for {b['start']}; accept it once or trust the folder; "
            "the runner cannot answer it")
    assert lines.count(want) == 1
    _ask_and_answer(fws, human, b)  # even after an answer and a long wait, nothing is typed into it
    at(fr.IDLE_SECONDS * 10)
    assert want not in _tick(fws, human, pane) and pane.typed == []  # said once per session
    r = _state(fws, eid)
    assert r["state"] == "trust" and r["headline"] == want and r["chip"] == "Trust question" and not r["live"]
    html = _client(fws).get(f"/factory/{eid}").text
    assert "data-trust" in html and "never answers it for you" in html
    pane.screen[b["name"]] = BUSY  # the human accepted it in the pane: working again
    _tick(fws, human, pane)
    assert _state(fws, eid)["state"] == "working"


def test_trust_question_needs_the_dialogs_structure_not_its_words():
    assert fr.trust_question(NEW_TRUST)
    assert not fr.trust_question(IDLE) and not fr.trust_question(None) and not fr.trust_question(ASKING)
    # the phrases in ordinary output, above Claude's input box: a transcript, not the dialog
    said = "● Claude asks \"Is this a project you trust?\" and you pick 1. Yes, I trust this folder.\n\n" + IDLE
    assert not fr.trust_question(said)
    # docs text a session printed, with the options but no "Enter to confirm" and the input box below
    docs = ("Is this a project you trust?\n 1. Yes, I trust this folder\n 2. No, exit\n\n" + IDLE)
    assert not fr.trust_question(docs)
    # the dialog's words without its options
    assert not fr.trust_question("Is this a project you trust?\nEnter to confirm\n")
    assert not fr.trust_question(TRUST)  # an older dialog without "Enter to confirm" is not claimed either


def test_no_nudge_while_the_human_typed_into_the_session_from_the_browser(fws, fa, fh, human, pane, at):
    typed = {"now": True}
    pane.human_typed = lambda name: typed["now"]  # the dashboard's record of the human's last browser key
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    _ask_and_answer(fws, human, b)
    _run_until_idle(fws, human, pane, at, 10)
    at(fr.IDLE_SECONDS * 4)
    _tick(fws, human, pane)
    assert pane.typed == [] and fs.nudges(d["id"]) == 0  # not even counted: the human is at the keyboard
    typed["now"] = False  # a minute later
    at(fr.IDLE_SECONDS * 6)
    _tick(fws, human, pane)
    assert pane.typed == [(b["name"], fr.NUDGES["answered"])]


def test_a_real_tick_reads_the_dashboards_record_of_the_humans_keys(fws, fa, fh, human, pane, at):
    from orch.dashboard import factory_runner as dash
    dash.HUMAN_KEYS.clear()
    pane.human_typed = dash.human_typed  # what TmuxLauncher.human_typed reads
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    _ask_and_answer(fws, human, b)
    dash.note_human_keys(b["name"])  # the human typed into it on the Terminals page
    _run_until_idle(fws, human, pane, at, 10)
    assert pane.typed == []
    dash.HUMAN_KEYS.clear()  # a minute later
    at(fr.IDLE_SECONDS * 4)
    _tick(fws, human, pane)
    assert pane.typed == [(b["name"], fr.NUDGES["answered"])]
