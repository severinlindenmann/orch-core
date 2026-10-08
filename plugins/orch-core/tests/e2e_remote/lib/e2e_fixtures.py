"""Fixtures of the Orch Remote end-to-end run (issue #94): one TIX server, two workspaces with their dashboards running
with the remote flag, and one Chromium that plays the paired device.

Not part of the default run: every test here carries the e2e_remote marker and is skipped unless it is selected with
`-m e2e_remote` (see docs/remote-validation.md for how to run it). Anything the run needs and cannot find skips it with
a sentence saying what is missing."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import e2e_harness as H

# No conftest.py in tests/e2e_remote on purpose: a second conftest.py replaces the suite's own `conftest` module in
# sys.modules, and every other test's `from conftest import ...` then fails. The test module imports these instead.
TIX_PASSPHRASE_WAIT = 30_000


@pytest.fixture(autouse=True)
def _only_when_selected(request):
    """Not part of the default run: skipped unless selected with -m e2e_remote."""
    if "e2e_remote" not in (request.config.getoption("markexpr") or ""):
        pytest.skip("Orch Remote end-to-end run: select it with -m e2e_remote (needs an orch-tix checkout)")


# The suite-wide autouse fixtures patch the process tree and the launcher for in-process tests. This run starts real
# subprocesses with their own environment (tests/e2e_remote/lib/e2e_harness.py), so the two that would get in the way
# are replaced by nothing; the git and tmux isolation of the suite's conftest stays.
@pytest.fixture(autouse=True)
def _clean_env(_only_when_selected):
    yield


@pytest.fixture(autouse=True)
def _no_real_launch(_only_when_selected):
    yield


def _missing_tools() -> str | None:
    for mod in ("cryptography", "fastapi", "uvicorn", "httpx", "playwright", "pywebpush"):
        if importlib.util.find_spec(mod) is None:
            return f"the Python package {mod} is not installed (orch-core[dashboard,dev], pytest-playwright, pywebpush)"
    if shutil.which("git") is None:
        return "git is not installed"
    return None


@dataclass
class Stack:
    tix: H.Tix
    a: H.Host
    b: H.Host
    tmux: H.Tmux | None
    page: object
    context: object
    cdp: object | None = None
    extra: dict = field(default_factory=dict)


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    why = H.tix_problem(H.find_tix()) or _missing_tools()
    if why:
        pytest.skip(why)
    from playwright.sync_api import sync_playwright
    work = tmp_path_factory.mktemp("e2e-remote")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch()
    except Exception as e:  # noqa: BLE001
        pw.stop()
        pytest.skip(f"Chromium cannot start for Playwright ({str(e).splitlines()[0]}); run: playwright install chromium")
    tix = H.Tix(H.find_tix(), work).start()
    tmux = H.Tmux() if shutil.which("tmux") else None
    state = work / "orch-state"
    state.mkdir()
    a = H.Host(tix, work, "alpha", state, tmux_socket=tmux.socket if tmux else None, tickets=[
        {"title": "Export the meter readings", "status": "in-progress"},
        {"title": "Fix the invoice rounding", "status": "in-progress"},
        {"title": "Write the onboarding mail", "status": "backlog"}])
    b = H.Host(tix, work, "bravo", state, tmux_socket=tmux.socket if tmux else None, tickets=[
        {"title": "Migrate the billing job", "status": "in-progress"},
        {"title": "Check the nightly export", "status": "testing"}])
    try:
        a.prepare()
        b.prepare()
        if tmux:
            tmux.session("ALPHA-1", a.root)
            tmux.session("BRAVO-1", b.root)
        a.start()
        b.start()
        context = browser.new_context(viewport={"width": 1000, "height": 800})
        page = context.new_page()
        import e2e_browser as B
        console = B.CONSOLE
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:300]))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"[:300]))
        page.on("requestfailed", lambda r: console.append(f"requestfailed: {r.url[:120]} {r.failure}"))
        page.goto(f"{tix.url}/login")
        page.locator("#passphrase").fill(tix.sim.passphrase)
        page.get_by_role("button", name="Log in").click()
        page.wait_for_url(f"{tix.url}/", timeout=TIX_PASSPHRASE_WAIT)
        yield Stack(tix, a, b, tmux, page, context, extra={"console": console, "browser": browser})
    finally:
        for h in (a, b):
            h.stop()
        if tmux:
            tmux.close()
        browser.close()
        pw.stop()
        tix.stop()
