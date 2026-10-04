"""Issue 32: the one-off frame read, the ticket copy and the /wpf/ file route walk every path component below their
root without following links, like /a/: a directory swapped for a link below the root is refused."""
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from orch.widgets import pages  # noqa: E402

F = "```"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
HTML = b"<p id=r>replay</p>"
WIKI = "orchestrator/wiki"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fence(obj) -> str:
    return f"{F}orch\n{json.dumps(obj)}\n{F}"


def compare(ref):
    return fence({"type": "compare", "before": {"path": ref, "sha256": sha(PNG)}, "after": {"path": ref, "sha256": sha(PNG)}})


def art(ws, tid, name, blob):
    p = ws.artifacts_dir / tid / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)
    return f"artifacts/{tid}/{name}"


def link_sub_to_real(ws, tid):
    """sub -> real: a link to a directory inside the ticket folder, so a resolved path still looks fine."""
    d = ws.artifacts_dir / tid
    (d / "sub").symlink_to(d / "real")


def set_findings(ws, tid, text):
    from orch.core import store
    t = store.load(ws, tid)[1]
    t.set_section("Findings", text)
    store.save(ws, t)


@pytest.mark.usefixtures("html_on")
@pytest.mark.parametrize("name", ["r.html", "a/b/r.html"])
def test_one_off_frame_serves_flat_and_nested_files(dash, put, ws, wurl, name):
    tid = put("in-progress")
    ref = art(ws, tid, name, HTML)
    set_findings(ws, tid, fence({"html": ref, "sha256": sha(HTML), "id": "ok"}))
    assert "<p id=r>replay</p>" in dash.get(wurl(tid, "ok") + "?n=abcdefgh12").text


@pytest.mark.usefixtures("html_on")
def test_one_off_frame_refuses_a_linked_directory(dash, put, ws, wurl):
    tid = put("in-progress")
    ref = art(ws, tid, "real/r.html", HTML)
    link_sub_to_real(ws, tid)
    set_findings(ws, tid, fence({"html": ref.replace("real/", "sub/"), "sha256": sha(HTML), "id": "bad"}))
    doc = dash.get(wurl(tid, "bad") + "?n=abcdefgh12").text
    assert "<p id=r>replay</p>" not in doc and "window.orch" not in doc


def test_ticket_copy_refuses_a_linked_directory_and_copies_nested_files(ws, put):
    tid = put("testing", sections={"Summary": "s", "Verification": "v", "Findings": "x"})
    ok = art(ws, tid, "d/e/a.png", PNG)
    via = art(ws, tid, "real/b.png", PNG)
    link_sub_to_real(ws, tid)
    set_findings(ws, tid, compare(ok) + "\n\n" + compare(via.replace("real/", "sub/")))
    got = pages.ticket_copy(ws, tid, ("Findings",))
    assert got["files"] == {f"{tid}-d/e/a.png": PNG} and len(got["blocks"]) == 1 and len(got["skipped"]) == 1


def test_page_file_route_opens_below_the_resolved_files_root(dash, ws, monkeypatch):
    folder = ws.root / WIKI
    (folder / pages.FILES).mkdir(parents=True)
    (folder / pages.FILES / "a.png").write_bytes(PNG)
    obj = SimpleNamespace(page_source=lambda page: (compare("_files/a.png"), WIKI) if page == "p" else None)
    dash.app.state.addons = SimpleNamespace(registry=SimpleNamespace(get=lambda n: SimpleNamespace(obj=obj)))
    url = f"/wpf/wiki/{sha(PNG)}?page=p"
    assert dash.get(url).content == PNG
    import orch.core.artifacts as core
    real, seen = core.read_pinned, []

    def spy(path, digest, limit=None, root=None):
        seen.append((path, root))
        return real(path, digest, limit, root)

    monkeypatch.setattr(core, "read_pinned", spy)
    assert dash.get(url).content == PNG
    files = (folder / pages.FILES).resolve()  # the resolved wiki files folder, the file named below it as written
    assert seen == [(files / "a.png", files)]
