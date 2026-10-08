"""Orch Remote end to end, the AI Factory from a phone (issue #94): a workspace `charlie` with the Factory on, a running
epic whose child asked for a command permission, and a second epic ready for the owner's verdict. Built with the repo's
own fakes (e2e_factory.py): no agent starts and no API credit is spent. The phone is the same virtual-authenticator
phone as in test_remote_unlock_e2e.py.

What this proves: Grant once needs a fresh confirmation showing the exact command; Deny and Pause need none; the epic's
verdict needs one showing what it closes; a cancelled sheet changes nothing. Not driven here (written down in the PR):
the Start charter sheet, Grant for the epic, and a Decide device with a valid assertion trying to close a child."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the end-to-end run needs Playwright (pip install pytest-playwright)")
pytest.importorskip("httpx")

from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "lib"))
import e2e_browser as B  # noqa: E402
import e2e_factory as F  # noqa: E402
import e2e_harness as H  # noqa: E402
from e2e_fixtures import _clean_env, _no_real_launch, _only_when_selected, stack  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.e2e_remote


@pytest.fixture(scope="module")
def charlie(stack):
    """A third workspace with the Factory on and its epics, started, and a phone paired with it at Type."""
    host = H.Host(stack.tix, stack.a.root.parent, "charlie", stack.a.state_dir, tmux_socket=None, tickets=[])
    host.prepare()
    F.enable_factory(host)
    host.ids = F.build(host)
    host.start()
    phone = B.new_phone(stack)
    phone.extra["did"] = B.pair(phone, host, "type", register=True)
    yield host, phone
    host.stop()


def frame(phone):
    return B.frame_of(phone.page)


def go(phone, path: str) -> None:
    frame(phone).evaluate(f"window.orchHost.navigate({__import__('json').dumps(path)})")


def notice(phone) -> str:
    return B.notice(phone.page)


def status_of(host, ref: str) -> str:
    from orch.core import store
    from orch.core.workspace import Workspace
    return store.load(Workspace.open(host.root, use_env=False), ref)[1].status


def open_today(phone, host) -> None:
    B.open_workspace(phone, host)
    B.wait_frame_text(phone.page, "Today")
    phone.page.evaluate("document.getElementById('remote-notice').textContent = ''")


def press(phone, selector: str) -> None:
    """A click on a form's button in the dashboard, then the dashboard's own inline 'Confirm' step if it shows one."""
    f = frame(phone)
    button = f.locator(selector).first
    button.click()
    phone.page.wait_for_timeout(700)
    try:
        asks = button.inner_text().strip().startswith("Confirm")      # the button turned into its own "Confirm · …" step
    except Exception:  # noqa: BLE001 - the page was replaced by the answer
        asks = False
    if asks and not phone.page.locator("#unlock-sheet").count():
        button.click()


def offered(host, rid: str) -> bool:
    """Today still offers a decision on this permission request (a decided one is gone from the forms, though its id
    may still be named in a note)."""
    import re
    return re.search(rf'action="/permits/{re.escape(rid)}/(?:grant|deny)"', host.dash.get("/").text) is not None


# -- Grant once: a fresh confirmation over the exact command --------------------------------------------------------

def test_grant_once_needs_a_confirmation_that_shows_the_command(charlie):
    host, phone = charlie
    rid = host.ids["permit"]
    open_today(phone, host)
    assert offered(host, rid)
    press(phone, f'form[action="/permits/{rid}/grant"] button[type="submit"]')
    text = B.sheet_text(phone)
    assert F.COMMAND in text, text
    assert B.sheets_shown(phone) == 1
    B.cancel_sheet(phone)                                          # not confirmed: nothing is granted
    phone.page.wait_for_timeout(2_000)
    assert offered(host, rid), "the permission was granted without a confirmation"
    press(phone, f'form[action="/permits/{rid}/grant"] button[type="submit"]')
    assert F.COMMAND in B.sheet_text(phone)
    B.confirm_sheet(phone)
    H.until(lambda: not offered(host, rid), 30, 0.5, what="the permission to be granted")
    assert B.sheets_shown(phone) == 2, "one sheet per attempt"


# -- Deny and Pause need no sheet ------------------------------------------------------------------------------------

def test_deny_and_pause_need_no_sheet(charlie):
    host, phone = charlie
    from orch.core import permits, store
    from orch.core.events import Actor
    from orch.core.workspace import Workspace
    with F.human_in_process(host.state_dir):
        ws = Workspace.open(host.root, use_env=False)
        second = permits.request(ws, Actor("agent", "claude-code", "cli", "7f3c9a21-0000"),
                                 store.load(ws, host.ids["child"])[1], "make test", reason="run the tests")
    open_today(phone, host)
    before = B.sheets_shown(phone)
    H.until(lambda: offered(host, second["id"]), 10, 0.5, what="the second permission on the page")
    go(phone, "/")
    B.wait_frame_text(phone.page, "Today")
    press(phone, f'form[action="/permits/{second["id"]}/deny"] button[type="submit"]')
    try:
        H.until(lambda: not offered(host, second["id"]), 30, 0.5, what="the permission to be denied")
    except AssertionError as e:
        import re
        raise AssertionError(f"{e}; second={second['id']}; forms now: "
                             f"{re.findall(r'action=.(/permits/[^ ]+).', host.dash.get('/').text)}") from None
    assert B.sheets_shown(phone) == before, "Deny asked for a confirmation"
    go(phone, f"/t/{host.ids['epic']}")
    B.wait_frame_text(phone.page, host.ids["epic"])
    press(phone, f'form[action="/t/{host.ids["epic"]}/epic/pause"] button[type="submit"]')
    phone.page.wait_for_timeout(3_000)
    assert B.sheets_shown(phone) == before, "Pause asked for a confirmation"
    assert "err=" not in (notice(phone) or ""), notice(phone)


# -- the epic's verdict: a fresh confirmation showing what it closes ------------------------------------------------

def test_the_epic_verdict_needs_a_confirmation_that_names_what_it_closes(charlie):
    host, phone = charlie
    eid, cid = host.ids["ready_epic"], host.ids["ready_child"]
    open_today(phone, host)
    B.wait_frame_text(phone.page, "Accept the epic")
    press(phone, f'form[action="/t/{eid}/verdict"] button[type="submit"]')
    text = B.sheet_text(phone)
    assert eid in text and cid in text, text
    B.cancel_sheet(phone)
    phone.page.wait_for_timeout(2_000)
    assert status_of(host, eid) != "done", "the epic was closed without a confirmation"
    press(phone, f'form[action="/t/{eid}/verdict"] button[type="submit"]')
    assert cid in B.sheet_text(phone)
    B.confirm_sheet(phone)
    H.until(lambda: status_of(host, eid) == "done" and status_of(host, cid) == "done", 30, 0.5,
            what="the epic and its child to be done")


@pytest.mark.skip(reason="not driven yet: the Start charter sheet (the epic's approve form with the Factory limits), "
                  "Grant for the epic, and a Decide device with a valid assertion trying to close a child; the "
                  "host side of each is covered by tests/test_factory_bridge.py")
def test_the_charter_start_and_the_rest():
    pass
