"""Named reads of declarative add-on views (design D7): contract, limits, access, and the shipped view file."""

import json
from pathlib import Path

import pytest
from conftest import ORIGIN
from test_wiki import commit, world  # noqa: F401  (the fixture)

from orch_dashboard import addon_reads
from orch_dashboard.wiki import limits

REPO = Path(__file__).resolve().parents[5]


@pytest.fixture
def read(world, client, token):  # noqa: F811
    def call(name, **params):
        result = client.get(
            f"/api/w/{world.ws}/view/{name}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
        )
        assert str(world.folder) not in result.text
        return result

    return call


def test_the_shipped_view_is_the_package_file():
    shipped = (REPO / "packages/addons/wiki/views/wiki.json").read_bytes()
    assert addon_reads._view_file() == shipped
    doc = json.loads(shipped)
    assert doc["addon"] == "wiki"
    (nav,) = doc["contributions"]
    assert nav["slot"] == "nav" and nav["node"]["type"] == "stack"


def test_the_package_is_listed(client, token):
    res = client.get("/api/addons", headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN})
    (pkg,) = res.json()
    assert pkg["name"] == "wiki" and pkg["capabilities"] == [] and pkg["contributions"][0]["slot"] == "nav"


def test_every_read_is_declared_with_limits():
    assert sorted(addon_reads.READS) == ["wiki.page", "wiki.search", "wiki.tree"]
    for name, spec in addon_reads.READS.items():
        assert addon_reads.NAME.fullmatch(name) and spec.limits and spec.args.model_config["extra"] == "forbid"


def test_tree(world, read):  # noqa: F811
    body = read("wiki.tree").json()
    assert body == {
        "state": "ok",
        "message": None,
        "nodes": [{"id": "hello", "label": "Hello world", "hint": "hello"}],
        "total": 1,
        "cut": False,
    }
    world.link.tickets = {"A-1": {"title": "t", "pages": "hello"}}
    assert read("wiki.tree").json()["nodes"][0]["badge"] == "1 linked ticket"


def test_tree_children_are_lazy_and_the_wiki_is_flat(read):
    assert read("wiki.tree", parent="hello").json()["nodes"] == []


def test_page(read):
    body = read("wiki.page", id="hello").json()
    assert body["id"] == "hello" and body["title"] == "Hello world" and body["markdown"].startswith("# Hello world")
    assert set(body) == {"id", "title", "markdown", "updated", "by"}
    assert read("wiki.page", id="missing").status_code == 404
    assert read("wiki.page", id="../x").json()["error"]["code"] == "invalid.input"


def test_a_page_over_the_limit_has_no_text(world, read):  # noqa: F811
    (world.folder / "wiki/big.md").write_text("# Big\n" + "x" * limits.PAGE_MAX_BYTES)
    commit(world)
    assert read("wiki.page", id="big").json()["markdown"] is None


def test_search(read):
    body = read("wiki.search", q="needle").json()
    assert body["results"] == [{"id": "hello", "label": "Hello world", "snippet": "A content needle."}]
    assert body["total"] == 1 and body["cut"] is False
    assert read("wiki.search", q="nothing-matches-this").json()["results"] == []


def test_search_is_limited(world, read):  # noqa: F811
    for i in range(limits.SEARCH_HITS + 5):
        (world.folder / f"wiki/p{i:03d}.md").write_text(f"# Page {i}\nshared words\n")
    commit(world)
    body = read("wiki.search", q="shared").json()
    assert len(body["results"]) == limits.SEARCH_HITS


@pytest.mark.parametrize("q", ["", "x" * 201, "x\0", "x\n"])
def test_invalid_search(read, q):
    assert read("wiki.search", q=q).json()["error"]["code"] == "invalid.input"


def test_closed_arguments_and_names(read):
    assert read("wiki.tree", extra="no").json()["error"]["code"] == "invalid.input"
    assert read("wiki.page").json()["error"]["code"] == "invalid.input"
    for name in ("wiki", "wiki.nothing", "other.tree", "WIKI.tree", "wiki.tree.x"):
        assert read(name).status_code == 404


@pytest.mark.parametrize(
    "name,params", [("wiki.tree", {}), ("wiki.page", {"id": "hello"}), ("wiki.search", {"q": "hello"})]
)
def test_access_each_request(world, read, name, params):  # noqa: F811
    assert read(name, **params).status_code == 200
    world.link.on = False
    assert read(name, **params).json()["error"]["code"] == "addon.disabled"
    world.link.on = True
    world.link.member = False
    assert read(name, **params).json()["error"]["code"] == "role.denied"


def test_no_folder_offers_creation_to_owners_and_maintainers_only(world, read, tmp_path, monkeypatch):  # noqa: F811
    from test_wiki import _no_wiki_repo

    _no_wiki_repo(world, tmp_path, monkeypatch)
    world.link.role = "member"
    body = read("wiki.tree").json()
    assert body["state"] == "no_folder" and body["nodes"] == [] and "offer" not in body
    assert body["message"] == "No wiki folder in this project yet."
    world.link.role = "maintainer"
    assert read("wiki.tree").json()["offer"] == {"label": "Create the wiki folder", "action": "create"}


def test_reads_are_get_only(world, client, token):  # noqa: F811
    res = client.post(
        f"/api/w/{world.ws}/view/wiki.tree", json={}, headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN}
    )
    assert res.status_code in (404, 405)
