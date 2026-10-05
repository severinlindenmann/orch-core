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
