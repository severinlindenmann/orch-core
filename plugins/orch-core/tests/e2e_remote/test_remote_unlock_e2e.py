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


def type_line(phone, text: str) -> None:
    """The reply field of the terminal page, then Enter: what a person does on a phone."""
    f = frame(phone)
    f.locator('button[data-mode="type"]').click()
    f.locator('input[name="text"]').fill(text)
    f.locator('input[name="text"]').press("Enter")


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


def test_the_lease_ends_and_the_next_burst_asks_again(stack, phone):
    open_terminal(phone, stack)
    type_line(phone, "inside-lease")                              # may or may not need a sheet, depending on the clock
    try:
        B.sheet_text(phone, timeout=8_000)
        B.confirm_sheet(phone)
    except Exception:  # noqa: BLE001 - still inside the lease
        pass
    wait_on_screen(stack, "inside-lease")
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
