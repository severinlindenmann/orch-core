"""Actions keep your place (app.js "Actions keep your place", driven by tests/js/keep_place.js): a POST form in the
page's main area posts in the background and the page is swapped in place with the scroll, the form's region and the
focus kept. That needs every region holding an action form to have an id, which the rendered pages are checked for
here; without JS the forms post and redirect exactly as before."""
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from orch.core import factory_sessions as fs, permits  # noqa: E402
from test_dark_dashboard import (_child, _client, _post, _started, _tick, _trusted_programs, Fake,  # noqa: E402,F401
                                 dws, fa, fh, fws)

ROOT = Path(__file__).resolve().parents[1]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_keep_place_js():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "keep_place.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "keep place ok" in r.stdout


class _Forms(HTMLParser):
    """Every POST form inside <main>, with the ids of the elements around it (main's own excluded), and every id."""
    def __init__(self):
        super().__init__()
        self.stack, self.forms, self.ids = [], [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if tag == "form" and (a.get("method") or "").lower() == "post" and any(t == "main" for t, _ in self.stack):
            around = [i for t, i in self.stack[[t for t, _ in self.stack].index("main") + 1:] if i]
            self.forms.append((a.get("action"), around))
        if tag not in VOID:
            self.stack.append((tag, a.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _forms(html):
    p = _Forms()
    p.feed(html)
    return p


def test_every_action_form_sits_in_a_region_with_an_id(dws, fa, human):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    cid = _child(fa, eid)
    assert _tick(dws, human, Fake())[0].startswith("started")
    (b,) = fs.bindings(dws)
    for cmd in ("make lint", "make build"):  # two cards side by side: each needs its own region
        permits.hook_decision(dws, {"session_id": b["session"], "tool_name": "Bash", "tool_input": {"command": cmd},
                                    "cwd": b["start"]})
    r, _ = permits.open_requests(dws)
    seen = set()
    for url in ("/", f"/factory/{eid}", f"/t/{eid}", f"/t/{cid}"):
        page = _forms(c.get(url).text)
        assert page.forms, url
        acts_on = {}  # innermost region id -> what its forms act on (the action without its verb)
        for action, around in page.forms:
            assert around, f"{url}: the form posting to {action} has no region with an id around it"
            acts_on.setdefault(around[-1], set()).add(action.rsplit("/", 1)[0])
            seen.add(action)
        shared = {k: v for k, v in acts_on.items() if len(v) > 1}
        assert not shared, f"{url}: one region holds forms of different cards (each card needs its own id): {shared}"
        dup = {i for i in page.ids if page.ids.count(i) > 1}
        assert not dup, f"{url}: ids used twice: {dup}"
    assert f"/permits/{r['id']}/grant" in seen and f"/t/{eid}/epic/pause" in seen


def test_without_js_a_form_still_posts_and_redirects(dws, fa, human):
    c = _client(dws)
    eid = _started(c, dws, "dark")
    _child(fa, eid)
    _tick(dws, human, Fake())
    (b,) = fs.bindings(dws)
    permits.hook_decision(dws, {"session_id": b["session"], "tool_name": "Bash", "tool_input": {"command": "make lint"},
                                "cwd": b["start"]})
    (r,) = permits.open_requests(dws)
    resp = _post(c, f"/permits/{r['id']}/deny", sha=r["sha"], next=f"/factory/{eid}")
    assert resp.status_code == 303 and resp.headers["location"].startswith(f"/factory/{eid}?msg=")
