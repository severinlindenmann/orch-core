"""Orch Remote, end to end (issue #94): two workspaces on one computer, each running the dashboard command with the
remote flag through the real relay tool against a local TIX server, and one Chromium as the paired device.

Run it: see docs/remote-validation.md ("Running the automated run"). The tests share one expensive stack and run in
file order; a test that needs an earlier step pairs again by itself when it finds the device gone.

What is not here: typing into a terminal and starting the AI Factory need the unlock sheet (a fresh Face ID, Touch ID
or PIN answer) in the TIX app; those tests are written and skipped until it is in the orch-tix checkout."""
from __future__ import annotations

import json
import re
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="the end-to-end run needs Playwright (pip install pytest-playwright)")
pytest.importorskip("httpx")

from playwright.sync_api import expect  # noqa: E402

import browser as B  # noqa: E402
import harness as H  # noqa: E402

pytestmark = pytest.mark.e2e_remote

OP_TITLES = {"alpha": "Export the meter readings", "bravo": "Migrate the billing job"}


def ticket_ids(host) -> dict[str, str]:
    """title -> id of every ticket in the workspace, read from its folder."""
    from orch.core import store
    from orch.core.workspace import Workspace
    ws = Workspace.open(host.root, use_env=False)
    return {(e.meta or {}).get("title"): e.id for e in store.scan(ws)}


def count_titled(host, title: str) -> int:
    from orch.core import store
    from orch.core.workspace import Workspace
    ws = Workspace.open(host.root, use_env=False)
    return sum(1 for e in store.scan(ws) if (e.meta or {}).get("title") == title)


def beat(stack, host) -> dict:
    """What the host's own code says its heartbeat holds right now, to compare with what TIX received."""
    from orch.core.workspace import Workspace
    from orch.dashboard import terminals
    from orch.remote import presence
    old = terminals.SOCKET
    if stack.tmux:
        terminals.SOCKET = stack.tmux.socket
    try:
        return presence.heartbeat(Workspace.open(host.root, use_env=False))
    finally:
        terminals.SOCKET = old


def device_id(host) -> str:
    live = host.dash.devices()
    assert live, "no paired device on " + host.name
    return live[-1]


def ensure_paired(stack, host, scope="operate"):
    if host.dash.devices():
        return device_id(host)
    return B.pair(stack, host, scope)


def row(stack, host) -> dict:
    return stack.tix.presence()[host.space]


def go(stack, path: str) -> None:
    """Navigate the dashboard frame the way a click on a link does."""
    B.frame_of(stack.page).evaluate(f"window.orchHost.navigate({json.dumps(path)})")


def frame_has(stack, text: str, seconds: int = 60) -> None:
    B.wait_frame_text(stack.page, text, seconds)


def skip_unless_unlock_sheet(stack):
    js = stack.tix.dir / "fileshare" / "static" / "js"
    if not any("unlock" in p.name for p in js.glob("*.js")):
        pytest.skip("the orch-tix checkout has no unlock sheet yet (no unlock*.js in fileshare/static/js): "
                    "typing and the Factory start need a fresh Face ID, Touch ID or PIN answer from the app")


# -- pairing and switching ------------------------------------------------------------------------------------------

def test_pair_the_browser_with_the_first_workspace(stack):
    did = B.pair(stack, stack.a, "operate")
    tab = stack.a.dash.tab()
    assert did in tab and "operate" in tab


def test_the_pairing_link_works_once(stack):
    """A spent link pairs nothing: the browser says so and pins nothing."""
    link = stack.b.dash.offer("operate")
    B.pair(stack, stack.b, "operate", link=link)
    page = stack.page
    page.goto(f"{stack.tix.url}/remote")
    page.goto(link)
    page.locator("#pair-go").click()
    expect(page.locator("#pair-state")).to_contain_text("used by someone else", timeout=100_000)
    assert page.locator("#pair-fp").is_hidden()


def test_switch_between_the_two_workspaces(stack):
    ensure_paired(stack, stack.a)
    ensure_paired(stack, stack.b)
    B.open_remote(stack)
    for host in (stack.a, stack.b):
        expect(B.card(stack, host).locator(".pill").first).to_have_text("Online", timeout=40_000)
    for host in (stack.a, stack.b, stack.a, stack.b):
        B.open_workspace(stack, host)
        B.wait_frame_text(stack.page, "Today")
        assert stack.page.locator("iframe.frame-dash").count() == 1
        other = stack.b if host is stack.a else stack.a
        go(stack, "/board")
        frame_has(stack, OP_TITLES[host.name])
        assert OP_TITLES[other.name] not in B.frame_text(stack.page), "the other workspace leaked into this frame"


def test_a_ticket_page_opens_in_the_frame_and_links_work(stack):
    B.open_workspace(stack, stack.a)
    go(stack, "/board")
    frame_has(stack, OP_TITLES["alpha"])
    B.frame_of(stack.page).get_by_text(OP_TITLES["alpha"]).first.click()      # a real click on a link in the frame
    tid = ticket_ids(stack.a)[OP_TITLES["alpha"]]
    frame_has(stack, tid)
    assert OP_TITLES["alpha"] in B.frame_text(stack.page)


# -- presence and the three counts ----------------------------------------------------------------------------------

def test_presence_and_counts_reach_the_status_page(stack):
    for host in (stack.a, stack.b):
        want = beat(stack, host)
        got = H.until(lambda: (lambda r: r if r["state"] == "online" and r.get("in_progress") == want["in_progress"]
                               and r.get("sessions") == want["sessions"] else None)(row(stack, host)),
                      60, what=f"{host.name} online with its counts")
        assert (got["sessions"], got["in_progress"], got["needs_you"]) == (want["sessions"], want["in_progress"],
                                                                           want["needs_you"])
    assert row(stack, stack.a)["in_progress"] == 2 and row(stack, stack.b)["in_progress"] == 1
    assert row(stack, stack.b)["needs_you"] >= 1, "a ticket waiting for the human's verdict counts as needs-you"
    assert row(stack, stack.a)["sessions"] == row(stack, stack.b)["sessions"] == (1 if stack.tmux else 0)
    stack.page.goto(f"{stack.tix.url}/workspaces")
    stack.page.locator("#ws-loading").wait_for(state="hidden")
    expect(stack.page.locator(f'.wrow[data-space="{stack.a.space}"]')).to_contain_text("2 in progress", timeout=30_000)


def test_the_counts_follow_the_workspace(stack):
    before = beat(stack, stack.a)
    ids = ticket_ids(stack.a)
    # the owner closes a ticket in progress (moving one forward needs the human's gates): it leaves the count
    r = stack.a.dash.post(f"/t/{ids['Fix the invoice rounding']}/close", {"as": "wont-do", "message": "closed by the e2e run"})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    if stack.tmux:
        stack.tmux.session("ALPHA-2", stack.a.root)
    want = beat(stack, stack.a)
    assert want["in_progress"] == before["in_progress"] - 1
    assert not stack.tmux or want["sessions"] == before["sessions"] + 1
    H.until(lambda: row(stack, stack.a)["in_progress"] == want["in_progress"] and row(stack, stack.a)["sessions"] == want["sessions"],
            60, what="the new counts on the status page")
    stack.page.goto(f"{stack.tix.url}/workspaces")
    expect(stack.page.locator(f'.wrow[data-space="{stack.a.space}"]')).to_contain_text(f"{want['in_progress']} in progress",
                                                                                      timeout=30_000)


# -- a retried POST runs once ---------------------------------------------------------------------------------------

FORM = {"content-type": "application/x-www-form-urlencoded"}


def test_a_retried_post_runs_once(stack):
    ensure_paired(stack, stack.a)
    B.open_remote(stack)
    title = "Created once over the bridge"
    assert count_titled(stack.a, title) == 0
    out = B.raw_request(stack, stack.a, "POST", "/new", f"title={title.replace(' ', '+')}&type=feature&size=m&priority=normal",
                        FORM, replay=True)
    first, second = out["first"], out["second"]
    assert first.get("status") in (200, 303), out
    assert second.get("status") == first["status"] and second.get("body") == first.get("body"), out
    assert count_titled(stack.a, title) == 1, "the retried POST ran again"


# -- scopes: Look, Decide, Operate ----------------------------------------------------------------------------------

def test_a_look_device_hitting_a_form_gets_the_readable_refusal(stack):
    did = ensure_paired(stack, stack.b)
    assert stack.b.dash.set_scope(did, "look").status_code == 303
    tid = ticket_ids(stack.b)[OP_TITLES["bravo"]]
    B.open_workspace(stack, stack.b)
    go(stack, f"/t/{tid}")
    frame_has(stack, OP_TITLES["bravo"])
    # the comment form, submitted by a person in the frame
    frame = B.frame_of(stack.page)
    frame.evaluate("document.querySelectorAll('details').forEach((d) => { d.open = true; })")
    frame.locator(f'form[action$="/t/{tid}/comment"] textarea, form[action$="/t/{tid}/comment"] input[name="text"]').first.fill(
        "typed at look")
    frame.locator(f'form[action$="/t/{tid}/comment"] button[type="submit"]').first.click()
    expect(stack.page.locator("#remote-notice")).to_contain_text("not allowed to do that on that computer", timeout=60_000)
    raw = B.raw_request(stack, stack.b, "POST", f"/t/{tid}/comment", "text=typed+at+look", FORM)
    assert raw.get("refusal") == "forbidden_scope", raw
    from orch.core import store
    from orch.core.workspace import Workspace
    text = store.resolve(Workspace.open(stack.b.root, use_env=False), tid).path.read_text()
    assert "typed at look" not in text, "a Look device changed a ticket"


def test_a_decide_device_cannot_watch_or_type_a_terminal(stack):
    did = ensure_paired(stack, stack.b)
    assert stack.b.dash.set_scope(did, "decide").status_code == 303
    if not stack.tmux:
        pytest.skip("tmux is not installed: there is no terminal to watch")
    for path in ("/terminals", "/terminals/BRAVO-1", "/terminals/BRAVO-1/snapshot"):
        raw = B.raw_request(stack, stack.b, "GET", path, None, {"accept": "text/html"})
        assert raw.get("refusal") == "forbidden_scope", (path, raw)
    raw = B.raw_request(stack, stack.b, "POST", "/terminals/BRAVO-1/keys", json.dumps({"seq": [{"text": "x"}], "n": 1,
                                                                                    "page": "pageone"}),
                        {"content-type": "application/json"})
    assert raw.get("refusal") == "forbidden_scope", raw


def test_decide_can_answer_but_not_edit(stack):
    did = ensure_paired(stack, stack.b)
    assert stack.b.dash.set_scope(did, "decide").status_code == 303
    tid = ticket_ids(stack.b)[OP_TITLES["bravo"]]
    raw = B.raw_request(stack, stack.b, "POST", f"/t/{tid}/comment", "text=nope", FORM)
    assert raw.get("refusal") == "forbidden_scope", raw
    page = B.raw_request(stack, stack.b, "GET", f"/t/{tid}", None, {"accept": "text/html"})
    assert page.get("status") == 200, page


def test_an_operate_device_watches_a_terminal_but_cannot_type(stack):
    if not stack.tmux:
        pytest.skip("tmux is not installed: there is no terminal to watch")
    did = ensure_paired(stack, stack.b)
    assert stack.b.dash.set_scope(did, "operate").status_code == 303
    marker = f"watch-me-{int(time.time())}"
    stack.tmux.run("send-keys", "-t", "BRAVO-1", "-l", marker)
    snap = B.raw_request(stack, stack.b, "GET", "/terminals/BRAVO-1/snapshot", None, {"accept": "application/json"})
    assert snap.get("status") == 200 and marker in snap.get("body", ""), snap
    B.open_workspace(stack, stack.b)
    go(stack, "/terminals/BRAVO-1")
    frame_has(stack, marker)                                       # the live stream, drawn in the frame
    raw = B.raw_request(stack, stack.b, "POST", "/terminals/BRAVO-1/keys",
                        json.dumps({"seq": [{"text": "typed-at-operate"}], "n": 1, "page": "pageone"}),
                        {"content-type": "application/json"})
    assert raw.get("refusal") == "forbidden_scope", raw
    assert "typed-at-operate" not in stack.tmux.screen("BRAVO-1")


def test_a_type_device_without_an_unlock_is_refused_and_types_nothing(stack):
    """Type alone is not enough: the host asks for a fresh assertion and the lease first (docs/remote.md)."""
    if not stack.tmux:
        pytest.skip("tmux is not installed: there is no terminal to watch")
    did = ensure_paired(stack, stack.b)
    assert stack.b.dash.set_scope(did, "type").status_code == 303
    raw = B.raw_request(stack, stack.b, "POST", "/terminals/BRAVO-1/keys",
                        json.dumps({"seq": [{"text": "typed-without-lease"}], "n": 1, "page": "pageone"}),
                        {"content-type": "application/json"})
    # this browser paired without a platform authenticator, so the host has nothing to ask it for: assertion_failed
    assert raw.get("refusal") in ("lease_required", "assertion_required", "assertion_failed"), raw
    assert "typed-without-lease" not in stack.tmux.screen("BRAVO-1")
    stack.b.dash.set_scope(did, "operate")


# -- the unlock sheet: typing and the AI Factory (waiting for the TIX app's side) ------------------------------------

def test_typing_after_an_unlock(stack):
    skip_unless_unlock_sheet(stack)
    pytest.skip("written when the unlock sheet lands: give the Type device a virtual authenticator (CDP WebAuthn), "
                "answer the sheet, type into BRAVO-1 and read it back from the private tmux server")


def test_the_factory_start_needs_a_fresh_confirmation(stack):
    skip_unless_unlock_sheet(stack)
    pytest.skip("written when the unlock sheet lands: the permission card and Start, each with a fresh assertion")


# -- revoking a device mid-stream -----------------------------------------------------------------------------------

def test_revoking_a_device_ends_its_stream_with_a_readable_refusal(stack):
    did = ensure_paired(stack, stack.b)
    stack.b.dash.set_scope(did, "operate")
    B.open_workspace(stack, stack.b)
    go(stack, "/board")
    frame_has(stack, OP_TITLES["bravo"])                          # the page holds its live-update stream open
    if stack.tmux:
        go(stack, "/terminals/BRAVO-1")                           # and a terminal stream, checked before every frame
        frame_has(stack, "BRAVO-1")
    stack.page.evaluate("document.getElementById('remote-notice').textContent = ''")   # only what the revoke causes
    r = stack.b.dash.revoke(did)
    assert r.status_code == 303 and "Device revoked" in r.headers["location"].replace("+", " ").replace("%20", " "), r.headers
    try:
        try:   # the open stream is ended by the host and the refusal reaches the page by itself ...
            expect(stack.page.locator("#remote-notice")).to_contain_text("removed from that computer", timeout=25_000)
        except AssertionError:   # ... or, if the page happened to be between two requests, at the person's next click
            go(stack, "/board")
            expect(stack.page.locator("#remote-notice")).to_contain_text("removed from that computer", timeout=60_000)
    except AssertionError as e:
        probe = B.raw_request(stack, stack.b, "GET", "/board", None, {"accept": "text/html"}, timeout_ms=20_000)
        raise AssertionError(f"{e}\nraw request after the revoke: {probe}\nconsole: {B.CONSOLE[-8:]}\n"
                             f"host log: {stack.b.text()[-500:]}") from None
    raw = B.raw_request(stack, stack.b, "GET", "/board", None, {"accept": "text/html"})
    assert raw.get("refusal") == "revoked", raw
    assert did not in stack.b.dash.devices(), "the device is still listed as live"
    audit = next((stack.b.state_dir / "permits" / "bridge" / stack.b.space).glob("audit.jsonl")).read_text()
    assert '"revoked"' in audit                                    # the host recorded it


@pytest.mark.xfail(strict=True, reason="a revoked browser cannot pair again: the host answers its pair request with a "
                   "plain `revoked` refusal (host_check._check) that carries no host_pub, which the browser drops, so "
                   "it waits 60 s and says the link was used, while the Remote tab says 'pair it again' (orch-core#256)")
def test_a_revoked_browser_can_pair_again(stack):
    if stack.b.dash.devices():
        pytest.skip("runs after the revoke test, which leaves bravo's device revoked")
    B.pair(stack, stack.b, "operate")


# -- the computer stops, restarts, vanishes -------------------------------------------------------------------------

def test_a_clean_stop_shows_stopped_and_a_restart_serves_the_same_device(stack):
    ensure_paired(stack, stack.a)
    B.open_workspace(stack, stack.a)
    go(stack, "/board")
    frame_has(stack, OP_TITLES["alpha"])
    assert stack.a.stop() is not None, stack.a.text()[-600:]
    H.until(lambda: row(stack, stack.a)["state"] == "stopped", 30, what="TIX to show the workspace as stopped")
    B.open_remote(stack)
    expect(B.card(stack, stack.a)).to_contain_text("Stopped", timeout=30_000)
    expect(B.card(stack, stack.a).get_by_role("button", name="Open Alpha")).to_be_disabled()
    stack.a.start()                                               # the lease was released: no take-over needed
    H.until(lambda: row(stack, stack.a)["state"] == "online", 60, what="the workspace online again")
    assert "1 paired device" in stack.a.text()
    B.open_remote(stack)
    B.open_workspace(stack, stack.a)
    go(stack, "/board")
    frame_has(stack, OP_TITLES["alpha"])


def test_a_computer_that_vanishes_holds_its_lease_until_it_is_taken_over(stack):
    ensure_paired(stack, stack.a)
    stack.a.kill()                                                # no goodbye, no release
    # a plain start finds the old lease: the local dashboard runs and the Remote tab says why the link does not
    stack.a.start()
    H.until(lambda: "Another dashboard already holds" in stack.a.dash.tab(), 90, 1,
            what="the Remote tab to say another host holds the workspace")
    H.until(lambda: row(stack, stack.a)["state"] != "online", 60, 1,      # the dead host's last beat ages out (30 s)
            what="TIX to stop calling the workspace online while the new dashboard cannot take it")
    stack.a.stop()
    stack.a.start(take_over=True)
    H.until(lambda: row(stack, stack.a)["state"] == "online", 60, what="the workspace online after a take-over")


@pytest.mark.parametrize("state,words", [("not_answering", "Not answering"), ("lost", "Lost")])
def test_a_host_that_stops_answering_while_open_is_said_in_words(stack, state, words):
    ensure_paired(stack, stack.a)
    B.open_workspace(stack, stack.a)
    go(stack, "/board")
    frame_has(stack, OP_TITLES["alpha"])
    mode = {"state": "online"}

    def presence(route):
        r = route.fetch()
        body = r.json()
        for s in body["spaces"]:
            if s["id"] == stack.a.space:
                s["state"] = mode["state"]
        route.fulfill(response=r, json=body)
    stack.page.route("**/api/presence", presence)
    try:
        mode["state"] = state                                     # the next refresh sees the host gone quiet
        stack.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(stack.page.locator("#remote-notice")).to_contain_text(words, timeout=40_000)
        expect(B.card(stack, stack.a).get_by_role("button", name="Open Alpha")).to_be_disabled()
    finally:
        stack.page.unroute("**/api/presence")


def test_a_host_that_really_goes_quiet_shows_not_answering(stack):
    """No rewriting of the answer: the computer is killed and TIX's own clock does the rest (30 s)."""
    ensure_paired(stack, stack.a)
    B.open_workspace(stack, stack.a)
    stack.a.kill()
    H.until(lambda: row(stack, stack.a)["state"] in ("not_answering", "lost"), 90, 1, what="TIX to see the host quiet")
    stack.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(stack.page.locator("#remote-notice")).to_contain_text(re.compile("Not answering|Lost"), timeout=40_000)
    stack.a.stop()
    stack.a.start(take_over=True)
