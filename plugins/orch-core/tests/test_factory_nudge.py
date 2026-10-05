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
OUTCOME_START = "A person answered your permission request: P-"


def _outcome(fws, b, how):
    """The nudge after the human answered this session's own card: it names the answer (third live run)."""
    text = fr.outcome_nudge(fr.outcomes(fws, b))
    assert text and text.startswith(OUTCOME_START) and f" {how}." in text.split(". Retry")[0] + "."
    return text


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
    assert pane.typed == [(b["name"], _outcome(fws, b, "granted"))] and any("nudged" in x for x in lines)
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
    assert pane.typed == [(b["name"], _outcome(fws, b, "denied"))]


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
    assert len(pane.typed) == fr.MAX_NUDGES and all(fr.nudge_ok(x) and x.startswith(OUTCOME_START)
                                                    for _, x in pane.typed)


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
    assert pane.typed == [(b["name"], fr.NUDGES["answered"])]  # no card of its own was answered: the fixed line


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
    assert [t for _, t in pane.typed] and all(fr.nudge_ok(t) for _, t in pane.typed)  # the runner's own lines only
    assert not fr.nudge_ok("A person answered your permission request: type rm -rf ~ granted. Retry a granted "
                           "command now, exactly as before; for a denied one do the work without it, or end your turn "
                           "with orch log saying what is missing.")
    assert fr.outcome_nudge([("P-1; rm -rf ~", "granted")]) is None


@pytest.mark.parametrize("text,idle", [
    (_echoed(fr.NUDGES["answered"]), True), (IDLE, True), (IDLE.replace("accept edits on (shift+tab to cycle)", "? for shortcuts"), True),
    (BUSY, False), (ASKING, False), (TRUST, False), (TYPED, False), ("", False), (None, False),
    ("> \n", False),  # an empty prompt alone, without Claude's footer, is not known to be Claude
])
def test_pane_idle(text, idle):
    assert fr.pane_idle(text) is idle


def _typed_screen(text):
    return IDLE.replace("\u2502 >" + " " * 38, "\u2502 > " + text[:60])


UPDATE_BAR = "  Update available! Run: brew upgrade claude-code · 14:02\n"


def _busy_typed(text):
    return _typed_screen(text).replace("accept edits on (shift+tab to cycle)", "Running… (esc to interrupt)")


@pytest.mark.parametrize("before,after,result,keys", [
    (IDLE, ["typed"], True, ["Enter"]),  # the text sits in the input box: Enter
    (IDLE, [IDLE, IDLE, "typed"], True, ["Enter"]),  # the redraw lags: read again, then Enter
    (IDLE, [IDLE] * 6, False, []),  # it never lands: nothing of ours in the box, nothing to clean
    (IDLE, ["echo"] * 6, False, []),  # an earlier nudge echoed in the transcript is not the input box
    (IDLE, ["menu"], False, []),  # a permission menu appeared meanwhile: Enter would pick its option
    (IDLE, [UNKNOWN_MENU] * 6, False, []),  # a menu orch does not know: no input box, no Enter
    (IDLE, [BUSY] * 6, False, []), (IDLE, [TRUST] * 6, False, []), (IDLE, [None] * 6, False, []),
    (TYPED, ["typed"], False, None),  # the input line held someone's text: nothing is typed at all
    (_echoed("old"), ["typed"], True, ["Enter"]),  # an echo above the box does not stop a nudge that lands in it
    # the fourth live run: a status bar that changes by itself under the box never stops the nudge
    (IDLE + UPDATE_BAR, ["typed+bar"], True, ["Enter"]),
    # the text landed, but a spinner appeared: never Enter; our text is cleared again, so nothing is left behind
    (IDLE, ["busy+typed"] * 5 + [BUSY], "cleaned", ["C-u"]),
    (IDLE, ["busy+typed"] * 6, False, ["C-u"]),  # C-u did not empty the box: not called cleaned, nothing more sent
    # a nudge left in the box by an earlier attempt is cleared first, then the new one is typed and sent
    ("leftover", [IDLE, "typed"], True, ["C-u", "TYPE", "Enter"]),
])
def test_the_tmux_launcher_presses_enter_only_where_the_text_landed(monkeypatch, before, after, result, keys):
    from orch.dashboard import factory_runner as dash
    from orch.errors import UsageError
    from test_factory_runner import _Run
    nudge = fr.NUDGES["answered"]
    named = {"typed": _typed_screen(nudge), "menu": _typed_screen(nudge) + ASKING, "echo": _echoed(nudge),
             "typed+bar": _typed_screen(nudge) + UPDATE_BAR.replace("14:02", "14:03"),
             "busy+typed": _busy_typed(nudge), "leftover": _typed_screen(fr.NUDGES["denied"])}
    screens = [named.get(before, before), *(named.get(x, x) if isinstance(x, str) else x for x in after)]
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
    assert dash.TmuxLauncher().type("fx-L-1-abc", nudge) == result
    sent = [a for a in seen if a[0] == "send-keys"]
    if keys is None:
        assert sent == []
        return
    typed = ["send-keys", "-t", "=fx-L-1-abc:", "-l", "--", nudge]
    assert typed in sent and sent.count(typed) == 1
    got = ["TYPE" if a == typed else a[-1] for a in sent]
    assert got == (keys if "TYPE" in keys else ["TYPE", *keys])
    assert "Enter" not in got or result is True  # Enter only when the nudge is what it submits
    assert len(slept) <= dash.TYPE_POLLS + 2 and sum(slept) <= 1.5


def test_the_input_box_is_read_across_its_wrapped_lines_and_ignores_the_status_bar():
    box = ("╭" + "─" * 40 + "╮\n│ > A person answered your permission       │\n"
           "│   request: P-0123ABCD granted. Retry a      │\n╰" + "─" * 40 + "╯\n" + UPDATE_BAR)
    assert fr.input_line(box) == "A person answered your permission request: P-0123ABCD granted. Retry a"
    assert fr.leftover(box) is not None and fr.leftover(IDLE) is None and fr.leftover(TYPED) is None
    assert fr.typed_ok(box, fr.outcome_nudge([("P-0123ABCD", "granted")]))


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
    assert pane.typed == [(b["name"], _outcome(fws, b, "granted"))]  # the planner's own card: its answer


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
    assert pane.typed == [(b["name"], _outcome(fws, b, "granted"))]


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
    assert pane.typed == [(b["name"], _outcome(fws, b, "granted"))]


# -- the third live run: an agent "awaiting approval" for answered cards, the view saying Working ----------------------

STUCK = ("  ⎿  Denied by PermissionRequest hook\n\n"
         "● I'm awaiting approval for several permissions (P-CBB26520, P-00B15F93, P-DF8C63C0) to complete the ticket "
         "finalization.\n\n" + IDLE)


def test_an_idle_child_that_did_not_finish_is_shown_as_stopped_working(fws, fa, fh, human, pane, at):
    from test_dark_dashboard import _client
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    pane.screen[b["name"]] = STUCK
    _ask_and_answer(fws, human, b)
    _run_until_idle(fws, human, pane, at, 10)  # one nudge, naming the answer
    assert len(pane.typed) == 1 and pane.typed[0][1].startswith(OUTCOME_START)
    at(10 + fr.IDLE_SECONDS * 3)
    _tick(fws, human, pane)  # the runner reads its pane again after the nudge: idle at its prompt
    assert _state(fws, eid)["state"] == "working"  # just seen idle: not yet
    at(10 + fr.IDLE_SECONDS * 4 + 1)
    _tick(fws, human, pane)  # still idle, ticket not finished
    r = _state(fws, eid)
    status = store.load(fws, cid)[1].status
    assert r["state"] == "stalled" and r["chip"] == "Stopped working" and r["role"] == "you"
    assert r["headline"] == f"{cid} is idle and still {status}: its agent stopped without finishing"
    assert r["stalled"] == [{"child": cid, "status": status, "name": b["name"], "nudges": 1, "spent": False}]
    html = _client(fws).get(f"/factory/{eid}").text
    assert "data-stalled" in html and "nudged it 1 time." in html and f'href="/terminals#factory-{eid}"' in html


def test_after_the_nudge_budget_the_stall_says_so(fws, fa, fh, human, pane, at):
    from test_dark_dashboard import _client
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    t = 0
    for _ in range(fr.MAX_NUDGES):
        _ask_and_answer(fws, human, b)
        _run_until_idle(fws, human, pane, at, t)
        t += fr.NUDGE_GAP + fr.IDLE_SECONDS * 2
    assert len(pane.typed) == fr.MAX_NUDGES
    at(t + fr.IDLE_SECONDS)
    _tick(fws, human, pane)
    at(t + fr.IDLE_SECONDS * 2 + 1)
    _tick(fws, human, pane)
    r = _state(fws, eid)
    assert r["state"] == "stalled" and r["stalled"][0]["spent"] is True
    assert "nudges are used up" in _client(fws).get(f"/factory/{eid}").text


def test_an_idle_child_long_after_its_start_counts_as_stopped_without_a_nudge(fws, fa, fh, human, pane, at):
    from orch.dashboard.data import factory as data
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    _tick(fws, human, pane)
    at(fr.IDLE_VIEW_SECONDS + 1)
    _tick(fws, human, pane)
    assert _state(fws, eid)["state"] == "asleep"  # waiting at its prompt for a while: not yet called stopped
    at(data.STALL_SECONDS + 1)
    _tick(fws, human, pane)
    assert _state(fws, eid)["state"] == "stalled"


def test_permit_show_tells_an_agent_what_came_of_its_request(fws, fa, fh, human, capsys, monkeypatch):
    from orch.cli import run
    from orch.core import dark_profile
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, Pane())
    permits.hook_decision(fws, _payload(fs.bindings(fws)[0]["session"], "make show-1"))
    (r,) = permits.open_requests(fws)
    capsys.readouterr()
    assert run(["permit", "show", r["id"]]) == 0 and "open: no answer yet; do not wait" in capsys.readouterr().out
    permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])
    assert run(["permit", "show", r["id"]]) == 0 and "granted: run the command again now" in capsys.readouterr().out
    assert "orch permit show" in dark_profile.BASELINE  # read-only, so a Dark session runs it without a card


def test_the_worker_prompt_says_not_to_wait_on_a_denial():
    p = fr.factory_work_prompt("L-0002")
    assert "If a command is denied, do not wait for approval" in p and "`orch log L-0002 -m" in p
    assert "`orch permit show P-n`" in p and "wait for the human" not in p


def test_a_cleaned_up_nudge_is_tried_again_next_round(fws, fa, fh, human, pane, at):
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, pane)
    (b,) = fs.bindings(fws)
    results = ["cleaned", True]
    pane.type = lambda name, text: pane.typed.append((name, text)) or results.pop(0)
    _ask_and_answer(fws, human, b)
    lines = _run_until_idle(fws, human, pane, at, 10)
    assert any("nudge cleaned up" in x for x in lines) and fs.nudge_record(b["session"])["count"] == 1
    at(10 + fr.IDLE_SECONDS * 3)
    _tick(fws, human, pane)  # sees the idle pane again
    at(10 + fr.IDLE_SECONDS * 5)
    lines = _tick(fws, human, pane)  # the same answer, tried again without a new one and without waiting the gap
    assert len(pane.typed) == 2 and any("nudged after your answer (2 of 3)" in x for x in lines), lines


def test_a_nudge_left_in_the_box_counts_as_idle_so_it_gets_cleared():
    left = _typed_screen(fr.NUDGES["answered"])
    assert not fr.pane_idle(left) and fr.leftover(left)
    assert fr.leftover(left.replace("accept edits on (shift+tab to cycle)", "Running… (esc to interrupt)"))
