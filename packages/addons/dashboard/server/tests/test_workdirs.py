"""Ticket work folders (design r3 part C): the host link's ops, the typed routes, the projection."""

import pytest
from conftest import ORIGIN

from orch_dashboard import hostlink
from orch_dashboard.workdirs import view

IDEM = "a_01M4Y46B485AXD4CBEJZ15Z1TV"


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


class Host:
    welcome = {"peer": {"person": "p_me"}}

    def __init__(self):
        self.calls = []
        self.answers = {}

    def supports(self, op, cap):
        return op.startswith("workdir.") and cap == "workdir/1"

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        return self.answers[op]


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, path):
    return f"/api/w/{stub.ws}{path}"


def test_the_workdir_ops_are_ours_under_cap_workdir_1():
    assert "workdir/1" in hostlink.CAPS
    for op in ("workdir.get", "workdir.delete"):
        assert hostlink.OPS[op] == 1 and op in hostlink.WORKDIR_OPS


def test_get_shows_the_size_and_the_relative_folder_never_a_full_path(client, token, stub, host):
    host.answers["workdir.get"] = {
        "codebase": False,
        "exists": True,
        "folder": ".orch-work/DEMO-1",
        "files": 14,
        "bytes": 2_100_000,
        "truncated": False,
        "path": "/Users/x/proj/.orch-work/DEMO-1",
    }
    res = client.get(url(stub, "/tickets/DEMO-1/workdir"), headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls[0] == ("workdir.get", {"ticket": "DEMO-1", "list": True}, False, None)
    assert res.json() == {"available": True, "exists": True, "codebase": False, "folder": ".orch-work/DEMO-1",
                          "files": 14, "bytes": 2_100_000, "truncated": False, "items": [],
                          "items_truncated": False}  # fmt: skip
    assert "/Users" not in res.text


def test_get_offers_only_plain_named_files_with_their_sizes():
    got = view({
        "exists": True, "codebase": False, "folder": ".orch-work/DEMO-1", "files": 3, "bytes": 20,
        "items": [{"name": "hello.txt", "bytes": 16}, {"name": "../x", "bytes": 1}, {"name": ".hid", "bytes": 1},
                  {"name": "a.txt", "bytes": -1}, {"name": "b.txt", "bytes": True}, "junk",
                  {"name": "ok.md\n", "bytes": 1}],
    })  # fmt: skip
    assert got["items"] == [{"name": "hello.txt", "bytes": 16}]
    many = [{"name": f"f{i}.txt", "bytes": 1} for i in range(30)]
    assert len(view({"exists": True, "items": many})["items"]) == 20
    assert view({"exists": True, "items": "x"})["items"] == []
    assert view({"exists": True, "items_truncated": True})["items_truncated"] is True
    assert view({"exists": True, "items_truncated": "yes"})["items_truncated"] is False


def test_view_reads_an_odd_answer_as_no_folder():
    assert view(None) == {"exists": False, "codebase": False}
    assert view({"codebase": True, "exists": True}) == {"exists": False, "codebase": True}
    assert view({"exists": True, "files": -1, "bytes": "x", "folder": "a‮b"})["files"] is None


def test_delete_is_a_write_with_the_browsers_idem(client, token, stub, host):
    host.answers["workdir.delete"] = {"deleted": True, "folder": ".orch-work/DEMO-1"}
    res = client.post(url(stub, "/workdir/delete"), json={"ticket": "DEMO-1", "idem": IDEM}, headers=hdr(token))
    assert res.json() == {"deleted": True}
    assert host.calls[-1] == ("workdir.delete", {"ticket": "DEMO-1"}, True, IDEM)


@pytest.mark.parametrize(
    "body",
    [
        {"ticket": "DEMO-1"},
        {"ticket": "DEMO-1", "idem": "x"},
        {"ticket": "../x", "idem": IDEM},
        {"ticket": "DEMO-1", "idem": IDEM, "path": "/etc"},
    ],
)
def test_the_delete_body_is_closed(client, token, stub, host, body):
    assert client.post(url(stub, "/workdir/delete"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []
