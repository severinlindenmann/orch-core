"""M: thumbnails beside a gated text or a criterion come from the renderer's own binding (markdown.pinned_images): only
an image the full text shows, bound and pinned by its hash (`?v=`), never a URL built by hand."""
import pytest

pytest.importorskip("fastapi")

import orch.core.artifacts as art  # noqa: E402
from orch.core import store  # noqa: E402
from orch.dashboard.markdown import artifact_scope, pinned_images, render_markdown  # noqa: E402


def _png(tmp_path, name="shot.png", data=b"\x89PNG-one"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_a_bound_inline_image_is_a_pinned_thumbnail(ws, aops, working, tmp_path):
    aops.artifact_add(working, _png(tmp_path))
    t = store.load(ws, working)[1]
    sha = t.meta["artifacts"][0]["sha256"]
    scope = artifact_scope(t, ws)
    text = "- export as CSV\n\n![Export dialog](artifact:shot.png)"
    pics = pinned_images(text, scope)
    assert pics == [{"href": f"/a/{working}/shot.png?v={sha[:16]}", "alt": "Export dialog", "name": "shot.png"}]
    assert pics[0]["href"] in render_markdown(text, scope)  # the very image the full text shows


@pytest.mark.parametrize("text", ["[the dialog](artifact:shot.png)",  # a link, not an image
                                  "`![x](artifact:shot.png)`",  # code: binds nothing, shows nothing
                                  "![x](artifact:loose.png)",  # not linked
                                  "![x](https://example.com/shot.png)",  # a web image is never loaded
                                  "![x](/a/L-0099/shot.png)"])  # another ticket's file
def test_nothing_else_becomes_a_thumbnail(ws, aops, working, tmp_path, text):
    aops.artifact_add(working, _png(tmp_path))
    assert pinned_images(text, artifact_scope(store.load(ws, working)[1], ws)) == []


def test_the_binding_function_decides(ws, aops, working, tmp_path, monkeypatch):
    aops.artifact_add(working, _png(tmp_path))
    scope = artifact_scope(store.load(ws, working)[1], ws)
    monkeypatch.setattr(art, "refs_in_tokens", lambda tokens, tid: [])
    assert pinned_images("![x](artifact:shot.png)", scope) == []


def test_no_ticket_no_thumbnails():
    assert pinned_images("![x](artifact:shot.png)", None) == []
