"""M4 S1 (#1452): ``GET /admin/published``: admin only, rows re-checked at the hub, selective workspaces hidden, text through
``render_text``, and the last good answer when orch-publish cannot be read."""

from __future__ import annotations

import json

import pytest
from webapi_helpers import Web

from orch_hub.publish import AdminReadError
from orch_hub.webapi import features

WS_FULL, WS_SEL = "11" * 16, "22" * 16
NOW = 1_800_000_000_000
ULID = "01HZX3N0ABCDEFGHJKMNPQRSTV"


def link(**kw):
    row = {
        "workspace_id": WS_FULL,
        "id": "ab" * 16,
        "type": "link",
        "kind": "secret",
        "state": "live",
        "created_ms": NOW - 1000,
        "expires_ms": NOW + 86_400_000,
        "bytes": 12,
        "label": "Q3 report",
        "ticket": ULID,
        "by_kind": "person",
        "by_name": "Ana",
        "views": 3,
        "has_password": False,
    }
    row.update(kw)
    return row


class FakeReader:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail, self.calls = list(rows), fail, 0

    def published(self, workspace=None):
        self.calls += 1
        if self.fail:
            raise AdminReadError("unavailable")
        return self.rows


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.reg.add(WS_FULL, "full", name="Product")
    x.reg.add(WS_SEL, "selective", name="Marketing")
    yield x
    x.close()


def get(w, tok=None):
    return w.call("GET", "/admin/published", token=tok or w.login(w.admin))


def test_the_feature_is_one_line_in_features_and_the_route_is_admin_only(w):
    assert any(f.mixin.__name__ == "PublishedHandlers" for f in features.FEATURES)
    w.api.d.published = FakeReader([link()])
    assert w.call("GET", "/admin/published")[0] == 401
    assert get(w, w.login(w.ann))[0] == 403
    assert w.call("GET", "/admin/published", token=w.login(w.admin, "app"))[0] in (401, 403)
    assert get(w)[0] == 200
    for method in ("POST", "PUT", "DELETE"):
        assert w.call(method, "/admin/published", {}, token=w.login(w.admin))[0] in (404, 405)


def test_a_link_row_carries_who_what_until_when_and_views_and_nothing_secret(w):
    w.api.d.published = FakeReader(
        [link(), link(id="cd" * 16, created_ms=NOW, label="Newer", by_kind="agent", by_name=None)]
    )
    s, o, _ = get(w)
    assert s == 200 and o["stale_ms"] is None and o["apps"] == []
    assert [x["label"] for x in o["links"]] == ["Newer", "Q3 report"]  # newest first
    first = o["links"][1]
    assert (first["workspace"], first["by_name"], first["views"], first["state"]) == ("Product", "Ana", 3, "live")
    assert first["expires_ms"] == NOW + 86_400_000 and first["has_password"] is False
    assert o["links"][0]["by_kind"] == "agent" and o["links"][0]["by_name"] is None
    blob = json.dumps(o).lower()
    for word in ("token", "hash", "pw", "path", "url", "content"):
        assert word not in blob.replace("has_password", ""), word


@pytest.mark.parametrize("field", ["label", "by_name", "ticket"])
@pytest.mark.parametrize("bad", ["a\u202eb", "a\u2066b", "a\x00b", "a\x1b[31mb", "a\u200bb", "a\nb"])
def test_control_and_bidi_text_is_never_emitted(w, field, bad):
    """C1 text is one clean line (text rule 8.5). A row that breaks it is refused at the hub as well; nothing reaches the page."""
    w.api.d.published = FakeReader([link(**{field: bad})])
    s, o, _ = get(w)
    assert s == 503 and "links" not in o
    assert bad not in json.dumps(o)


def test_a_refused_answer_is_not_kept_as_the_last_good_one(w):
    r = FakeReader([link()])
    w.api.d.published = r
    tok = w.login(w.admin)
    assert get(w, tok)[0] == 200
    r.rows = [link(label="a\u202eb")]
    s, o, _ = get(w, tok)
    assert s == 200 and o["links"][0]["label"] == "Q3 report" and o["stale_ms"] is not None  # the older good answer


def test_the_page_text_goes_through_render_text(w, monkeypatch):
    seen = []
    from orch_hub.webapi import published_api as PA

    real = PA.render_text
    monkeypatch.setattr(PA, "render_text", lambda x: (seen.append(x), real(x))[1])
    w.api.d.published = FakeReader([link()])
    get(w)
    assert {"Q3 report", "Ana", ULID, "Product"} <= set(seen)


def test_a_row_of_a_selective_workspace_is_never_shown_and_the_workspace_is_listed_as_hidden(w):
    w.api.d.published = FakeReader([link(), link(id="ee" * 16, workspace_id=WS_SEL, label="Secret plans")])
    _, o, _ = get(w)
    assert [x["label"] for x in o["links"]] == ["Q3 report"]
    assert "Secret plans" not in json.dumps(o)
    assert o["hidden"] == [{"workspace_id": WS_SEL, "name": "Marketing"}]


def test_an_unbound_workspace_is_neither_listed_nor_hidden(w):
    w.reg.add(WS_SEL, "full", status="unbound")
    w.api.d.published = FakeReader([link(workspace_id=WS_SEL)])
    _, o, _ = get(w)
    assert o["links"] == [] and o["hidden"] == []


def test_the_last_good_answer_is_kept_when_publish_is_down(w):
    r = FakeReader([link()])
    w.api.d.published = r
    tok = w.login(w.admin)
    assert get(w, tok)[1]["stale_ms"] is None
    r.fail = True
    s, o, _ = get(w, tok)
    assert s == 200 and o["stale_ms"] >= 0 and len(o["links"]) == 1


def test_with_no_good_answer_and_publish_down_the_route_says_unavailable(w):
    w.api.d.published = FakeReader(fail=True)
    assert get(w)[0:2] == (503, {"error": "unavailable"})


def test_no_publish_address_is_unavailable(w):
    assert get(w)[0] == 503
