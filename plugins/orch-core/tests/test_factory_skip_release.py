"""Dark AI Factory: a human verdict on an epic whose charter signs a release that has not run. In the first live run
the human's Accept closed the epic while merge and dev were still waiting, and nothing was released. Now every
surface (the Ready card, the run view, the epic page, the CLI) offers "Close without releasing" with a reason the
ledger records as release_skipped, and the Finished summary says so. Without a signed release nothing changes."""
import re

import pytest

from orch.cli import run as cli_run
from orch.core import epics, factory_release as fr, ledger, store
from orch.errors import ValidationError
from test_factory_release import Fake, _states  # noqa: F401
from test_factory_release import (_not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote,  # noqa: F401
                                  switch)

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


def _seen(fws, eid):
    return epics.verdict_hash(epics.open_children(fws, store.load(fws, eid)[1]), fws)


def _verdict_entry(fws, eid):
    return next(e for e in reversed(ledger.entries(fws)) if e.get("ticket") == eid and e.get("kind") == "verdict")


def _client(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _run(fws, eid):
    from orch.dashboard.data import factory as data
    return data.run_view(fws, store.load(fws, eid)[1])


def test_a_plain_verdict_is_refused_while_the_signed_release_has_not_run(fws, ready, fh):
    eid, _, _ = ready(release="dev")
    assert fr.unreleased(fws, store.load(fws, eid)[1]) == ["merge", "dev"]
    with pytest.raises(ValidationError, match="The release has not run; closing now skips it"):
        fh.verdict(eid, "done", expected_hash=_seen(fws, eid))
    with pytest.raises(ValidationError, match="The release has not run"):
        fh.verdict(eid, "done", expected_hash=_seen(fws, eid), skip_release="   ")
    assert store.load(fws, eid)[1].status == "open"
    fh.verdict(eid, "done", expected_hash=_seen(fws, eid), skip_release="the customer needs it today")
    assert store.load(fws, eid)[1].status == "done"
    e = _verdict_entry(fws, eid)
    assert e["release_skipped"] == "the customer needs it today" and e["skipped_stages"] == ["merge", "dev"]
    r = _run(fws, eid)
    assert r["state"] == "finished" and r["headline"] == "You gave the verdict: closed without release"


def test_without_a_signed_release_or_once_it_is_proven_nothing_changes(fws, ready, fh, human):
    eid, _, _ = ready(release="none")
    assert fr.unreleased(fws, store.load(fws, eid)[1]) is None
    fh.verdict(eid, "done", expected_hash=_seen(fws, eid))
    assert "release_skipped" not in _verdict_entry(fws, eid) and _run(fws, eid)["headline"] == "You gave the verdict"
    eid2, _, _ = ready(release="dev")
    fr.tick(fws, human, Fake())
    assert _states(fws, eid2) == {"merge": "proven", "dev": "proven"} and fr.unreleased(fws, store.load(fws, eid2)[1]) == []
    fh.verdict(eid2, "done", expected_hash=_seen(fws, eid2))
    assert "release_skipped" not in _verdict_entry(fws, eid2)


def test_ready_card_run_view_and_epic_page_offer_close_without_releasing(fws, ready):
    pytest.importorskip("fastapi")
    eid, _, _ = ready(release="dev")
    c = _client(fws)
    for url in (f"/factory/{eid}", f"/t/{eid}"):  # the run view and the epic page, each with its Ready card
        html = c.get(url).text
        box = html[html.index(f'data-release-unrun="{eid}"'):]
        box = box[:box.index("</form>")]
        assert "The release has not run; closing now skips it (merge, dev not proven yet)" in box, url
        assert "Close without releasing" in box and 'name="skip_release"' in box and "required" in box, url
        assert "Accept the epic" not in html, url  # no plain button anywhere
    seen = re.search(r'name="seen" value="([^"]+)"', c.get(f"/factory/{eid}").text).group(1)
    r = c.post(f"/t/{eid}/verdict", data={"verdict": "done", "seen": seen}, follow_redirects=False)
    assert "err=" in r.headers.get("location", "") and store.load(fws, eid)[1].status == "open"
    r = c.post(f"/t/{eid}/verdict", data={"verdict": "done", "seen": seen, "skip_release": "ship it by hand"},
               follow_redirects=False)
    assert "err=" not in r.headers.get("location", "") and store.load(fws, eid)[1].status == "done"
    assert _verdict_entry(fws, eid)["release_skipped"] == "ship it by hand"
    html = c.get(f"/factory/{eid}").text
    assert "You gave the verdict: closed without release" in html


def test_the_cli_needs_skip_release_and_records_it(fws, ready, switch, capsys):
    eid, _, _ = ready(release="dev")
    switch.human(eid)
    capsys.readouterr()
    assert cli_run(["verdict", eid, "done"]) != 0
    err = capsys.readouterr().err
    assert "The release has not run; closing now skips it" in err and "--skip-release" in err
    assert store.load(fws, eid)[1].status == "open"
    assert cli_run(["verdict", eid, "done", "--skip-release", "released by hand"]) == 0
    assert "closing without releasing" in capsys.readouterr().out
    assert store.load(fws, eid)[1].status == "done" and _verdict_entry(fws, eid)["release_skipped"] == "released by hand"


# -- the review's gaps: partial and stale stages, a running release, the epic close, orch check, the child card --------

def test_partial_and_stale_stages_are_the_ones_skipped(fws, ready, fh, human):
    from test_factory_release import _branch
    eid, _, _ = ready(release="dev")
    fake = Fake()
    fake.results["dev-health"] = {"code": 1}  # merge proven, dev failed
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "failed"}
    assert fr.unreleased(fws, store.load(fws, eid)[1]) == ["dev"]
    fh.verdict(eid, "done", expected_hash=_seen(fws, eid), skip_release="dev is down today")
    assert _verdict_entry(fws, eid)["skipped_stages"] == ["dev"]
    eid2, (c,), _ = ready(release="dev")
    fr.tick(fws, human, Fake())
    assert fr.unreleased(fws, store.load(fws, eid2)[1]) == []
    _branch(fws.root, f"feat/{c.lower()}-work", {"src/more.py": "x\n"}, start=f"feat/{c.lower()}-work")
    fr.tick(fws, human, Fake())  # the child came back with more work: merge and dev are out of date
    assert fr.unreleased(fws, store.load(fws, eid2)[1]) == ["merge", "dev"]
    with pytest.raises(ValidationError, match="merge, dev not proven yet"):
        fh.verdict(eid2, "done", expected_hash=_seen(fws, eid2))


def test_no_skip_while_a_release_holds_the_lock(fws, ready, fh):
    eid, _, _ = ready(release="dev")
    assert fr.acquire(fws, eid, 60)  # a release round is running
    try:
        with pytest.raises(ValidationError, match="a release is running"):
            fh.verdict(eid, "done", expected_hash=_seen(fws, eid), skip_release="now")
    finally:
        fr.release_lock(fws)
    assert store.load(fws, eid)[1].status == "open"
    fh.verdict(eid, "done", expected_hash=_seen(fws, eid), skip_release="now")
    assert fr.lock_holder(fws) is None  # the verdict let the lock go


def _all_done(fws, fh, eid):
    for k in epics.children(fws, eid):
        fh.verdict(k.id, "done", expected_hash=epics.verdict_hash([store.load(fws, k.id)[1]], fws))


def test_the_epic_close_needs_the_skip_reason_too(fws, ready, fh):
    eid, _, _ = ready(release="dev")
    _all_done(fws, fh, eid)
    with pytest.raises(ValidationError, match="The release has not run; closing now skips it"):
        fh.close(eid, "all children are done")
    fh.close(eid, "all children are done", skip_release="released by hand")
    last = [e for e in ledger.entries(fws) if e.get("ticket") == eid and e.get("kind") == "close"][-1]
    assert last["release_skipped"] == "released by hand" and last["skipped_stages"] == ["merge", "dev"]
    assert _run(fws, eid)["headline"] == "You gave the verdict: closed without release"


def test_the_run_views_close_form_asks_for_the_skip_reason(fws, ready, fh):
    eid, _, _ = ready(release="dev")
    _all_done(fws, fh, eid)
    c = _client(fws)
    html = c.get(f"/factory/{eid}").text
    assert f'data-release-unrun="{eid}"' in html and 'name="skip_release"' in html and "Close without releasing" in html
    r = c.post(f"/factory/{eid}/close", data={"reason": "done"}, follow_redirects=False)
    assert "err=" in r.headers.get("location", "") and store.load(fws, eid)[1].status == "open"
    c.post(f"/factory/{eid}/close", data={"reason": "done", "skip_release": "by hand"}, follow_redirects=False)
    assert store.load(fws, eid)[1].status == "done"


def test_orch_check_says_closed_without_release_and_warns_without_a_reason(fws, ready, fh, monkeypatch):
    from orch.core.check import run_checks
    eid, _, _ = ready(release="dev")
    fh.verdict(eid, "done", expected_hash=_seen(fws, eid), skip_release="by hand")
    eid2, _, _ = ready(release="dev")
    with monkeypatch.context() as m:  # an orch from before this check closed it plainly
        m.setattr(fr, "skip_fields", lambda *a, **k: {})
        fh.verdict(eid2, "done", expected_hash=_seen(fws, eid2))
    found = {(f.ticket, f.level, f.code) for f in run_checks(fws, emit_events=False)}
    assert (eid, "info", "closed-without-release") in found
    assert (eid2, "warning", "closed-unreleased") in found


def test_the_childs_own_verdict_card_says_accepting_it_releases_nothing(fws, ready):
    eid, (c,), _ = ready(release="dev")
    html = _client(fws).get(f"/t/{c}").text
    assert "data-child-release-unrun" in html and f"accepting {c} alone releases nothing" in html
