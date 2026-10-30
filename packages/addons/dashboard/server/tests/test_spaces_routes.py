"""run-15c P03: the shared spaces of this workspace are relayed read-only in a closed shape."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orch_dashboard import spaces_routes
from orch_dashboard.errors import HostError, host_response

WS = "a" * 32
SID = "b" * 32
SPACE = {"space_id": SID, "epoch": 1, "members": ["c" * 32, "d" * 32], "shared": False, "key": True,
         "name": "Team notes", "kind": "wiki", "secret": "x"}  # fmt: skip


class Link:
    def __init__(self, answers, supported=True):
        self.answers, self.calls, self.supported = answers, [], supported

    def supports(self, op, cap="ops/1"):
        return self.supported

    def call(self, op, args=None, **kw):
        self.calls.append((op, args) if not kw else (op, args, kw))
        got = self.answers[op]
        if isinstance(got, Exception):
            raise got
        return got


class Gw:
    def __init__(self, link):
        self._link = link

    def enabled(self, ws):
        return self._link


def client(link):
    app = FastAPI()
    spaces_routes.register(app, Gw(link))

    @app.exception_handler(HostError)
    def _(request, exc):
        return host_response(exc)

    return TestClient(app)


def test_the_list_is_a_closed_shape_marked_from_the_hub():
    link = Link({"space.list": {"spaces": [SPACE, {**SPACE, "space_id": "bad"}, "x"]}})
    got = client(link).get(f"/api/w/{WS}/spaces").json()
    assert got == {"spaces": [{"id": SID, "name": "Team notes", "kind": "wiki", "members": 2}],
                   "note": "from the hub, not signed"}  # fmt: skip
    assert link.calls == [("space.list", None)]


def test_a_name_with_a_hidden_character_is_withheld_not_repaired():
    link = Link({"space.list": {"spaces": [{**SPACE, "name": "Team‮notes", "kind": 5}]}})
    (row,) = client(link).get(f"/api/w/{WS}/spaces").json()["spaces"]
    assert row["name"] == "" and row["kind"] == ""


@pytest.mark.parametrize("code", ["hub.not_joined", "hub.state_mismatch"])
def test_no_hub_is_an_empty_list_not_an_error(code):
    err = HostError({"code": code, "message": "x", "retryable": False}, 3)
    res = client(Link({"space.list": err})).get(f"/api/w/{WS}/spaces")
    assert res.status_code == 200 and res.json()["spaces"] == []


def test_a_host_without_the_op_shows_nothing():
    link = Link({}, supported=False)
    assert client(link).get(f"/api/w/{WS}/spaces").json()["spaces"] == [] and link.calls == []


def test_an_unreachable_hub_still_surfaces():
    err = HostError({"code": "hub.unreachable", "message": "x", "retryable": True}, 4)
    res = client(Link({"space.list": err})).get(f"/api/w/{WS}/spaces")
    assert res.status_code != 200 and res.json()["error"]["code"] == "hub.unreachable"


def test_items_are_names_and_sizes_only_never_content_or_authors():
    rows = [
        {"item_id": "1" * 32, "seq": 1, "epoch": 1, "revoked": False, "name": "plan.md", "author": "c" * 32, "size": 12,
         "data": "SECRET"},
        {"item_id": "2" * 32, "seq": 2, "epoch": 1, "revoked": True},
        {"item_id": "3" * 32, "seq": 3, "epoch": 1, "revoked": False, "name": "<img src=x onerror=1>", "size": "9"},
        {"item_id": "4" * 32, "seq": 4, "epoch": 1, "revoked": False, "name": "hid​den", "size": 1},
    ]
    link = Link({"space.list": {"items": rows, "refused": 2, "untrusted": True}})
    got = client(link).get(f"/api/w/{WS}/spaces/{SID}").json()
    assert got == {"id": SID, "items": [{"name": "plan.md", "size": 12},
                                        {"name": "<img src=x onerror=1>", "size": 0}],
                   "refused": 2, "note": "from the hub, not signed"}  # fmt: skip
    assert link.calls == [("space.list", {"space_id": SID})]


def test_a_bad_space_id_is_refused_before_the_host_is_asked():
    link = Link({})
    res = client(link).get(f"/api/w/{WS}/spaces/not-an-id")
    assert res.status_code != 200 and link.calls == []


def test_the_module_only_reads():
    from pathlib import Path

    src = Path(spaces_routes.__file__).read_text()
    assert "write=True" not in src and "idem" not in src and "app.post" not in src
