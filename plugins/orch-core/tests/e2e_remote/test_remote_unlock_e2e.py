"""Orch Remote end to end, the unlock sheet (issue #94): the REAL host and the REAL TIX app JS in Chromium, with a CDP
virtual platform authenticator standing in for Face ID.

A second browser context is "the phone": its own TIX session, its own device keys, and a virtual authenticator that
answers at once with user verification. It pairs with the `bravo` workspace at scope Type, registers its credential on
the Register button, then watches and types into a terminal (a private tmux server, never the person's own), starts
sessions, and is revoked. Nothing here starts a real agent: the only harness is a fake one that echoes its input.

Runs after test_remote_e2e.py (file order) on the same stack; the typing lease is shortened to 30 s by the launcher so
its end can be watched (lease expiry is the host's clock: the host's own tests cover 15 minutes)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the end-to-end run needs Playwright (pip install pytest-playwright)")
pytest.importorskip("httpx")

from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "lib"))
import e2e_browser as B  # noqa: E402
import e2e_harness as H  # noqa: E402
from e2e_fixtures import _clean_env, _no_real_launch, _only_when_selected, stack  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.e2e_remote

SESSION = "BRAVO-1"
LEASE_TEXT_START = f"Type into terminal {SESSION} for 15 minutes."


@pytest.fixture(scope="module")
def phone(stack):
    """The phone: paired with bravo at Type, with its platform credential registered."""
    if not stack.tmux:
        pytest.skip("tmux is not installed: there is no terminal to watch or type into")
    if H.unlock_problem(stack.tix.dir):
        pytest.skip(H.unlock_problem(stack.tix.dir))
    p = B.new_phone(stack)
    p.extra["did"] = B.pair(p, stack.b, "type", register=True)
    return p


def frame(phone):
    return B.frame_of(phone.page)


def go(phone, path: str) -> None:
    frame(phone).evaluate(f"window.orchHost.navigate({json.dumps(path)})")


def open_terminal(phone, stack) -> None:
    B.open_workspace(phone, stack.b)
    go(phone, f"/terminals/{SESSION}")
    B.wait_frame_text(phone.page, SESSION)
    # Typing before the live stream has answered is refused with "Typing needs the live screen" (and that banner is not
    # cleared when typing then works): give the stream its moment, as a person does, and start from a clean banner.
    phone.page.wait_for_timeout(4_000)
    phone.page.evaluate("document.getElementById('remote-notice').textContent = ''")


def type_line(phone, text: str) -> None:
    """The reply field of the terminal page, then Enter: what a person does on a phone."""
    f = frame(phone)
    f.locator('button[data-mode="type"]').click()
    f.locator('input[name="text"]').fill(text)
    f.locator('input[name="text"]').press("Enter")


def type_and_confirm(phone, stack, text: str) -> None:
    """Type a line; answer the sheet if one comes (it does not inside an open lease); wait until it is on the terminal."""
    type_line(phone, text)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if phone.page.locator("#unlock-sheet").count():
            B.confirm_sheet(phone)
        if text in screen(stack):
            return
        time.sleep(0.4)
    raise AssertionError(f"{text!r} never reached the terminal; notice: {notice(phone)!r}")


def screen(stack) -> str:
    return stack.tmux.screen(SESSION)


def wait_on_screen(stack, text: str, seconds: int = 45) -> None:
    H.until(lambda: text in screen(stack), seconds, 0.5, what=f"{text!r} on the terminal {SESSION}")


def wait_in_frame(phone, text: str, seconds: int = 60) -> None:
    """The text is in the frame's document (textContent: a short phone frame may clip what innerText would count)."""
    def seen():
        try:
            return text in frame(phone).evaluate("document.body ? document.body.textContent : ''")
        except Exception:  # noqa: BLE001 - the frame is being rebuilt
            return False
    H.until(seen, seconds, 0.3, what=f"the frame to hold {text!r}")


def notice(phone) -> str:
    return B.notice(phone.page)


# -- the credential -------------------------------------------------------------------------------------------------

def test_the_phone_registers_a_platform_credential_when_it_pairs(stack, phone):
    tab = stack.b.dash.tab()
    assert phone.extra["did"] in stack.b.dash.devices()
    assert "passkey on this device" in tab or "synced passkey" in tab, "the computer lists no credential for the phone"
    assert "type" in tab


# -- watching needs no sheet ----------------------------------------------------------------------------------------

def test_watching_a_terminal_needs_no_sheet_and_shows_no_typing_banner(stack, phone):
    open_terminal(phone, stack)
    marker = f"watch-{int(time.time())}"
    stack.tmux.run("send-keys", "-t", SESSION, "-l", marker)
    wait_in_frame(phone, marker)
    phone.page.wait_for_timeout(4_000)                            # a watch also sizes its view: that must stay silent
    assert B.sheets_shown(phone) == 0, "a sheet was drawn for watching"
    assert "Typing needs the live screen" not in notice(phone), notice(phone)
    assert notice(phone) == "", notice(phone)


# -- typing: one sheet, in order, within the lease, then again ------------------------------------------------------

def test_typing_asks_once_types_in_order_and_a_second_burst_needs_no_sheet(stack, phone):
    open_terminal(phone, stack)
    before = B.sheets_shown(phone)
    type_line(phone, "first-burst")
    text = B.sheet_text(phone)
    assert text.startswith(LEASE_TEXT_START), text
    assert "every terminal on this computer for 15 minutes" in text
    B.confirm_sheet(phone)
    wait_on_screen(stack, "first-burst")
    type_line(phone, "second-burst")
    wait_on_screen(stack, "second-burst")
    out = screen(stack)
    assert out.index("first-burst") < out.index("second-burst"), "keys arrived out of order"
    assert B.sheets_shown(phone) == before + 1, "a second sheet was drawn inside the lease"
    assert "Typing needs the live screen" not in notice(phone)


def test_a_typed_line_arrives_once_and_the_page_goes_quiet(stack, phone):
    """After a confirmed burst the page must not keep posting keys: no more posts, and the line is typed exactly once
    (the terminal echoes it twice: the tty's echo and cat's output)."""
    open_terminal(phone, stack)
    type_and_confirm(phone, stack, "exactly-once")
    phone.page.wait_for_timeout(2_000)
    posts = [r for r in B.frame_requests(phone) if r[1] == "POST"]
    phone.page.wait_for_timeout(8_000)
    later = [r for r in B.frame_requests(phone) if r[1] == "POST"]
    assert len(later) == len(posts), f"the page kept posting after the keys arrived: {later[len(posts):]}"
    assert screen(stack).count("exactly-once") <= 2, screen(stack)


def test_the_lease_ends_and_the_next_burst_asks_again(stack, phone):
    open_terminal(phone, stack)
    type_and_confirm(phone, stack, "inside-lease")                # a sheet only if the lease has lapsed
    time.sleep(H.LEASE_MS / 1000 + 3)                             # the host's lease (shortened by the launcher) ends
    shown = B.sheets_shown(phone)
    type_line(phone, "after-expiry")
    assert B.sheet_text(phone).startswith(LEASE_TEXT_START)
    assert B.sheets_shown(phone) == shown + 1
    B.cancel_sheet(phone)                                         # cancel: nothing is typed, and the page says why
    phone.page.wait_for_timeout(3_000)
    assert "after-expiry" not in screen(stack), "keys were typed although the sheet was cancelled"
    H.until(lambda: "not confirmed" in notice(phone).lower() or "nothing was done" in notice(phone).lower(), 15, 0.5,
            what="a readable message after a cancelled sheet")


# -- starts: each its own sheet, never on the lease -----------------------------------------------------------------

def sessions(stack) -> list[str]:
    return sorted(stack.tmux.run("list-sessions", "-F", "#{session_name}").stdout.split())


def press_new_session(phone) -> None:
    go(phone, "/terminals")
    B.wait_frame_text(phone.page, "Terminals")
    frame(phone).locator('form[action="/terminals/new"] button[type="submit"]').first.click()


def test_a_new_session_needs_its_own_sheet_even_inside_a_lease_and_starts_once(stack, phone):
    open_terminal(phone, stack)
    type_and_confirm(phone, stack, "lease-first")                  # a lease is open now
    posts = [r for r in B.frame_requests(phone) if r[1] == "POST"]
    phone.page.wait_for_timeout(4_000)
    later = [r for r in B.frame_requests(phone) if r[1] == "POST"]
    assert later == posts, f"the page kept posting keys after they arrived: {later[len(posts):]}"
    assert "Typing needs the live" not in notice(phone), notice(phone)
    before = sessions(stack)
    press_new_session(phone)
    text = B.sheet_text(phone)
    assert "Typing needs the live" not in notice(phone), "leaving the terminal page raised the typing banner"
    assert text.startswith("Start a new terminal session "), text
    assert H.FAKE_AGENT in text and "Command: " in text, text
    B.cancel_sheet(phone)                                          # not confirmed: nothing starts
    phone.page.wait_for_timeout(3_000)
    assert sessions(stack) == before, "a session started without a confirmation"
    press_new_session(phone)
    assert B.sheet_text(phone).startswith("Start a new terminal session ")
    B.confirm_sheet(phone)
    H.until(lambda: len(sessions(stack)) == len(before) + 1, 30, 0.5, what="exactly one new session")
    phone.page.wait_for_timeout(3_000)
    assert len(sessions(stack)) == len(before) + 1, "the start ran more than once"
    new = (set(sessions(stack)) - set(before)).pop()
    H.until(lambda: "FAKE-AGENT-STARTED" in stack.tmux.screen(new), 20, 0.5, what="the fake agent's first line")


def ticket_id(host, title: str) -> str:
    from orch.core import store
    from orch.core.workspace import Workspace
    ws = Workspace.open(host.root, use_env=False)
    return next(e.id for e in store.scan(ws) if (e.meta or {}).get("title") == title)


def press_start_agent(phone, ref: str) -> None:
    go(phone, f"/t/{ref}")
    B.wait_frame_text(phone.page, ref)
    f = frame(phone)
    f.locator('form[data-agent-start] select[name="harness"]').first.select_option(H.FAKE_AGENT)   # Terminals runs only this one
    f.locator('form[data-agent-start] button[name="where"]:not([disabled])').first.click()


def test_an_agent_start_has_its_own_sheet_naming_the_ticket_and_starts_once(stack, phone):
    ref = ticket_id(stack.b, "Migrate the billing job")
    B.open_workspace(phone, stack.b)
    before = sessions(stack)
    press_start_agent(phone, ref)
    text = B.sheet_text(phone)
    assert text.startswith(f"Start an agent. Harness {H.FAKE_AGENT}, "), text
    assert f"as session {ref}" in text and f'Ticket {ref} titled: "Migrate the billing job"' in text, text
    B.cancel_sheet(phone)
    phone.page.wait_for_timeout(3_000)
    assert sessions(stack) == before, "an agent started without a confirmation"
    press_start_agent(phone, ref)
    assert B.sheet_text(phone).startswith("Start an agent. ")
    B.confirm_sheet(phone)
    H.until(lambda: ref in sessions(stack), 30, 0.5, what=f"the session {ref}")
    phone.page.wait_for_timeout(3_000)
    assert sessions(stack).count(ref) == 1 and len(sessions(stack)) == len(before) + 1, "the start ran more than once"
    H.until(lambda: "FAKE-AGENT-STARTED" in stack.tmux.screen(ref), 20, 0.5, what="the fake agent's first line")


def test_a_sheet_text_too_long_for_the_phone_is_refused_in_a_fixed_sentence_and_nothing_runs(stack, phone):
    """The phone refuses to show a text over 2000 characters it cannot check (and one with invisible or look-alike
    spacing: the host turns those in a title into plain spaces, so only the length can be reached from here; the
    spacing rule is covered by orch-tix's own tests). Nothing is asked of the authenticator and nothing starts."""
    stack.b.set_terminal_harness(H.LONG_AGENT)
    try:
        B.open_workspace(phone, stack.b)
        before = sessions(stack)
        phone.page.evaluate("document.getElementById('remote-notice').textContent = ''")
        press_new_session(phone)
        H.until(lambda: "too long to check on this phone" in notice(phone) or phone.page.locator("#unlock-sheet").count(),
                30, 0.5, what="the phone to answer")
        assert phone.page.locator("#unlock-sheet").count() == 0, "a sheet was drawn for text the phone cannot check"
        assert "The computer sent a request that is too long to check on this phone." in notice(phone), notice(phone)
        phone.page.wait_for_timeout(2_000)
        assert sessions(stack) == before, "something started"
    finally:
        stack.b.set_terminal_harness(H.FAKE_AGENT)


def test_a_stream_that_keeps_dying_does_not_hammer_the_host(stack, phone):
    """With the relay refusing every answer for 20 s, a watched terminal's stream dies again and again: the number of
    requests it posts to the relay must stay small (a person's phone on a bad network must not flood the host)."""
    open_terminal(phone, stack)
    posted: list[float] = []
    phone.page.on("request", lambda r: posted.append(time.monotonic())
                  if r.method == "POST" and f"/api/bridge/{stack.b.space}/requests" in r.url else None)
    phone.page.route(f"**/api/bridge/{stack.b.space}/responses*", lambda route: route.abort())
    try:
        phone.page.wait_for_timeout(20_000)
    finally:
        phone.page.unroute(f"**/api/bridge/{stack.b.space}/responses*")
    print(f"requests posted in 20 s with every answer refused: {len(posted)}")
    assert len(posted) <= 12, f"{len(posted)} requests in 20 s: the page hammers the host while its answers fail"
    phone.page.wait_for_timeout(5_000)
    B.wait_frame_text(phone.page, SESSION)                          # and the page is still there afterwards


@pytest.mark.xfail(strict=True, reason="found by this run: after a confirmed typing burst, leaving the terminal page "
                   "(any link in the dashboard) leaves its key batcher running in the frame: it posts the same batch "
                   "(same n) once a second for good, each refused locally with 'Typing needs the live screen', and "
                   "asks for a new confirmation when the lease lapses (orch-core#328: static/terminal.js and the "
                   "frame shim in orch-tix)")
def test_a_confirmed_burst_is_not_posted_again_after_the_page_is_left(stack, phone):
    open_terminal(phone, stack)
    type_and_confirm(phone, stack, "then-leave")
    go(phone, "/terminals")
    B.wait_frame_text(phone.page, "Terminals")
    before = [r for r in B.frame_requests(phone) if r[1] == "POST"]
    phone.page.wait_for_timeout(6_000)
    after = [r for r in B.frame_requests(phone) if r[1] == "POST"]
    assert after == before, f"keys were posted again from a page that was left: {after[len(before):][:3]}"
    assert "Typing needs the live" not in notice(phone), notice(phone)


def test_a_failed_face_id_starts_nothing_and_says_so(stack, phone):
    B.open_workspace(phone, stack.b)                               # a new frame: no page of an earlier test is still running
    before = sessions(stack)
    phone.page.evaluate("document.getElementById('remote-notice').textContent = ''")
    B.set_user_verified(phone, False)                              # the person fails Face ID
    try:
        press_new_session(phone)
        assert B.sheet_text(phone).startswith("Start a new terminal session ")
        phone.page.wait_for_timeout(700)
        phone.page.evaluate("() => { const t = document.getElementById('unlock-text'); t.scrollTop = t.scrollHeight; }")
        phone.page.locator("#unlock-go").click()
        H.until(lambda: "nothing was done" in notice(phone).lower() or "not confirmed" in notice(phone).lower()
                or phone.page.locator("#unlock-sheet").count() == 0, 20, 0.5, what="the sheet to end")
    finally:
        B.set_user_verified(phone, True)
    if phone.page.locator("#unlock-sheet").count():
        B.cancel_sheet(phone)
    phone.page.wait_for_timeout(2_000)
    assert sessions(stack) == before, "a session started although the confirmation failed"
    assert "nothing was done" in notice(phone).lower() or "not confirmed" in notice(phone).lower(), \
        f"{notice(phone)!r}; the frame asked for: {B.frame_requests(phone)[-12:]}"


# -- revoked while typing (last: the phone is gone after it) --------------------------------------------------------

def test_revoking_the_phone_while_it_types_refuses_the_next_keys_and_types_nothing_more(stack, phone):
    open_terminal(phone, stack)
    type_and_confirm(phone, stack, "before-revoke")                # a lease is open
    r = stack.b.dash.revoke(phone.extra["did"])
    assert r.status_code == 303 and "Device revoked" in r.headers["location"].replace("+", " ").replace("%20", " ")
    phone.page.evaluate("document.getElementById('remote-notice').textContent = ''")
    type_line(phone, "after-revoke")
    H.until(lambda: "removed from that computer" in notice(phone), 60, 0.5, what="the readable refusal after a revoke")
    phone.page.wait_for_timeout(3_000)
    assert "after-revoke" not in screen(stack), "keys were typed after the device was revoked"
    assert phone.extra["did"] not in stack.b.dash.devices()
