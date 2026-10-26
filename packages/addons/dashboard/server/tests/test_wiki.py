"""Wiki reads: real committed objects, fake host visibility, no working-tree reads."""

from types import SimpleNamespace

import pytest
from conftest import ORIGIN
from test_explorer import git

from orch_dashboard.errors import HostError, fail
from orch_dashboard.explorer import git as browse
from orch_dashboard.wiki import index, limits, reader, scope

ROUTES = [("", {}), ("/page", {"slug": "hello"}), ("/search", {"q": "hello"}), ("/backlinks", {"slug": "hello"})]


class Link:
    def __init__(self):
        self.person = "a"
        self.role = "viewer"
        self.member = True
        self.on = True
        self.dashboard = True
        self.tickets = {}
        self.fail_query = False
        self.calls = []

    @property
    def welcome(self):
        return {"peer": {"person": self.person}}

    def call(self, op, args=None):
        self.calls.append((op, args))
        if op == "addon.list":
            return {
                "addons": [
                    {"name": "dashboard", "enabled": self.dashboard},
                    {"name": "wiki", "state": "active" if self.on else "disabled"},
                ]
            }
        if op == "member.list":
            return {
                "members": [{"person": self.person, "name": "Reader", "role": self.role, "devices": []}]
                if self.member
                else []
            }
        if op == "ticket.query" and self.fail_query:
            raise fail("role.denied", exit=3)
        visible = {k: v for k, v in self.tickets.items() if self.person in v.get("people", ["a", "b"])}
        if op == "ticket.query":
            rows = [
                {"uid": key, "key": key, "title": t["title"], "updated_at": t.get("stamp", "1")}
                for key, t in visible.items()
            ]
            offset = int((args or {}).get("page", "0"))
            return {
                "rows": rows[offset : offset + 200],
                "next_page": str(offset + 200) if len(rows) > offset + 200 else None,
            }
        if op == "ticket.get":
            ticket = visible[args["ref"]]
            return {"addons": {"wiki": {"pages": ticket.get("pages")}}}
        raise AssertionError(op)


@pytest.fixture
def world(tmp_path, gateway, stub, monkeypatch):
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "main")
    (folder / "wiki").mkdir()
    (folder / "wiki/hello.md").write_text("# Hello world\n\nA content needle.\n## Rules\n### **Check**\n")
    git(folder, "add", ".")
    git(folder, "commit", "-qm", "first")
    link = Link()
    monkeypatch.setattr(gateway, "link", lambda ws: link)
    monkeypatch.setattr(gateway, "workspace_folder", lambda ws: (folder, folder / ".orch"))
    browse._confirmed.clear()
    index._cache.clear()
    return SimpleNamespace(folder=folder, link=link, ws=stub.ws, gateway=gateway, repo=browse.Repo(str(folder), (), ""))


@pytest.fixture
def get(world, client, token):
    def call(route="", **params):
        result = client.get(
            f"/api/w/{world.ws}/wiki{route}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN},
        )
        assert str(world.folder) not in result.text
        return result

    return call


def commit(world):
    git(world.folder, "add", "-f", ".")
    git(world.folder, "commit", "-qm", "pages")


@pytest.mark.parametrize(
    "slug,valid",
    [
        ("a", True),
        ("a-0", True),
        ("x" * 64, True),
        ("../x", False),
        ("Hello", False),
        ("a\0", False),
        ("а", False),
        ("a" * 65, False),
        ("", False),
    ],
)
def test_slug(slug, valid):
    if valid:
        assert reader.validate(slug) == slug
    else:
        with pytest.raises(HostError) as error:
            reader.validate(slug)
        assert error.value.error["code"] == "invalid.input"


def test_head_only(world, get):
    (world.folder / "wiki/hello.md").write_text("# Staged\nOnly staged text\n")
    git(world.folder, "add", ".")
    (world.folder / "wiki/hello.md").write_text("# Uncommitted\n")
    listing = get().json()
    assert listing["state"] == "ok"
    assert listing["total"] == 1
    row = listing["pages"][0]
    assert row == {
        "slug": "hello",
        "title": "Hello world",
        "updated": "2026-01-01T10:00:00Z",
        "by": "Ann",
        "tickets": [],
        "links": 0,
        "too_large": False,
    }
    page = get("/page", slug="hello").json()
    assert page["markdown"].startswith("# Hello world")
    assert page["toc"] == [{"level": 2, "text": "Rules"}, {"level": 3, "text": "Check"}]
    assert get("/search", q="staged").json()["pages"] == []
    assert get("/search", q="Uncommitted").json()["pages"] == []


def test_regular_flat_pages_only(world, get):
    wiki = world.folder / "wiki"
    (wiki / "outside.md").symlink_to("/etc/passwd")
    (wiki / "secret.md").symlink_to("../.orch/secret")
    (wiki / ".env.md").write_text("secret")
    (wiki / ".orch.md").write_text("secret")
    (wiki / "sub").mkdir()
    (wiki / "sub/x.md").write_text("nested")
    (wiki / "binary.md").write_bytes(b"# Binary\n\0secret")
    commit(world)
    # a binary file is a name in the list (git cannot tell without reading it) but it is never served as a page
    assert [r["slug"] for r in get().json()["pages"]] == ["binary", "hello"]
    for slug in ("outside", "secret", "binary", "missing"):
        assert get("/page", slug=slug).status_code == 404
    assert get("/search", q="root:").json()["pages"] == []


@pytest.mark.parametrize("kind", ["link", "file"])
def test_non_tree_wiki(world, get, kind):
    (world.folder / "wiki/hello.md").unlink()
    (world.folder / "wiki").rmdir()
    wiki = world.folder / "wiki"
    if kind == "link":
        wiki.symlink_to("/tmp")
    else:
        wiki.write_text("not a folder")
    commit(world)
    data = get().json()
    assert data["state"] == "no_folder" and data["pages"] == []
    assert get("/page", slug="hello").status_code == 404


@pytest.mark.parametrize("kind", ["no_repo", "no_commits", "nested", "no_folder"])
def test_empty_states(world, get, tmp_path, monkeypatch, kind):
    folder = tmp_path / "empty"
    folder.mkdir()
    if kind == "nested":
        nested = folder / "wiki"
        nested.mkdir()
        git(nested, "init", "-q", "-b", "main")
        (nested / "x.md").write_text("# Nested")
        git(nested, "add", ".")
        git(nested, "commit", "-qm", "nested")
    if kind in ("no_commits", "no_folder"):
        git(folder, "init", "-q", "-b", "main")
    if kind == "no_folder":
        git(folder, "commit", "--allow-empty", "-qm", "empty")
    monkeypatch.setattr(world.gateway, "workspace_folder", lambda ws: (folder, folder / ".orch"))
    data = get().json()
    assert data["state"] == ("no_repo" if kind == "nested" else kind)
    assert data["pages"] == [] and data["total"] == 0 and data["message"]


def test_size_boundary(world, get):
    (world.folder / "wiki/exact.md").write_bytes(b"x" * limits.PAGE_MAX_BYTES)
    (world.folder / "wiki/over.md").write_bytes(b"x" * (limits.PAGE_MAX_BYTES + 1))
    commit(world)
    exact = get("/page", slug="exact").json()
    assert len(exact["markdown"]) == limits.PAGE_MAX_BYTES and not exact["too_large"]
    over = get("/page", slug="over").json()
    assert over["markdown"] is None and over["too_large"] and over["size"] == limits.PAGE_MAX_BYTES + 1
    assert next(r for r in get().json()["pages"] if r["slug"] == "over")["too_large"]


def test_list_cap(world, get):
    for n in range(500):
        (world.folder / f"wiki/p{n:03}.md").write_text("# Page")
    commit(world)
    data = get().json()
    assert len(data["pages"]) == 500 and data["total"] == 501 and data["cut"]
    assert data["pages"][-1]["slug"] == "p498"


@pytest.mark.parametrize(
    "markdown,title",
    [
        ("intro\n# First\n# Second", "First"),
        ("```\n# Hidden\n## Hidden\n```\n# Real", "Hidden"),  # a line rule, as the list takes it from git grep
        ("no title", "fallback"),
        ("# " + "a" * 121, "a" * 120),
        ("# a\u202eb\x01", r"a\u202eb\u0001"),
    ],
)
def test_title(markdown, title):
    assert reader.title(markdown, "fallback") == title
    assert reader.toc("```\n## Hidden\n```\n## Visible") == [{"level": 2, "text": "Visible"}]


def test_history_window(world, get):
    for n in range(limits.LOG_COMMITS):
        (world.folder / "wiki/new.md").write_text(f"# New {n}\n")
        git(world.folder, "add", ".")
        git(world.folder, "commit", "-qm", "new")
    page = get("/page", slug="hello").json()
    assert page["updated"] is None and page["by"] is None
    assert get("/page", slug="new").json()["by"] == "Ann"


def test_search(world, get, monkeypatch):
    assert get("/search", q="HELLO").json()["pages"][0]["slug"] == "hello"
    data = get("/search", q="NEEDLE").json()
    assert data["pages"][0]["snippet"] == "A content needle."
    monkeypatch.setattr(limits, "SEARCH_SCAN_BYTES", 1)
    data = get("/search", q="needle").json()
    assert data == {"pages": [], "cut": True}
    assert get("/search", q="Hello").json()["pages"][0]["slug"] == "hello"


@pytest.mark.parametrize("q", ["", " ", "x" * 201, "x\0", "x\n", "x\x7f"])
def test_invalid_search(get, q):
    assert get("/search", q=q).json()["error"]["code"] == "invalid.input"


@pytest.mark.parametrize("route,params", ROUTES)
def test_access_each_request(world, get, route, params):
    assert get(route, **params).status_code == 200  # viewer
    world.link.on = False
    assert get(route, **params).json()["error"]["code"] == "addon.disabled"
    world.link.on = True
    world.link.member = False
    assert get(route, **params).json()["error"]["code"] == "role.denied"
    world.link.member = True
    world.link.dashboard = False
    assert get(route, **params).json()["error"]["code"] == "addon.disabled"


@pytest.mark.parametrize("route,params", ROUTES)
def test_extra_queries(get, route, params):
    assert get(route, **params, extra="no").status_code == 400


@pytest.mark.parametrize("route", ["/page", "/backlinks"])
@pytest.mark.parametrize("slug", ["../x", "Upper", "x\0", "а", "x" * 65, ""])
def test_invalid_route_slug(get, route, slug):
    assert get(route, slug=slug).json()["error"]["code"] == "invalid.input"


def test_links_isolated_by_person(world, get):
    world.link.tickets = {
        "DEMO-1": {"title": "Private A title", "pages": "hello future", "people": ["a"]},
        "DEMO-2": {"title": "Shared", "pages": "hello"},
    }
    assert get("/page", slug="hello").json()["tickets"] == ["DEMO-1", "DEMO-2"]
    assert get("/backlinks", slug="future").json()["tickets"][0]["ticket"] == "DEMO-1"
    world.link.person = "b"
    for route, params in ROUTES:
        response = get(route, **params)
        assert "DEMO-1" not in response.text and "Private A title" not in response.text
    assert get().json()["pages"][0]["tickets"] == ["DEMO-2"]
    assert get("/backlinks", slug="future").json()["tickets"] == []
    world.link.person = "a"
    assert get().json()["pages"][0]["links"] == 2


def test_parse_links():
    assert index.parse_pages(" ".join(f"p{n}" for n in range(21))) == [f"p{n}" for n in range(20)]
    assert index.parse_pages("a a ../x B а b") == ["a", "b"]
    for value in (None, {}, []):
        assert index.parse_pages(value) == []


def test_refresh_budget_and_changes(world, monkeypatch):
    world.link.tickets = {f"DEMO-{n}": {"title": "Title", "pages": "hello"} for n in range(3)}
    monkeypatch.setattr(limits, "PER_REFRESH", 1)
    for expected in (1, 2, 3):
        assert len(index.links_by_slug(world.link, world.ws, "a")["hello"]) == expected
    reads = [args for op, args in world.link.calls if op == "ticket.get"]
    assert len(reads) == 3
    world.link.tickets["DEMO-0"].update(pages="future", stamp="2", title="hidden\u202e")
    del world.link.tickets["DEMO-1"]
    result = index.links_by_slug(world.link, world.ws, "a")
    assert result["future"] == [{"ticket": "DEMO-0", "title": ""}]
    assert result["hello"] == [{"ticket": "DEMO-2", "title": "Title"}]


def test_unconfirmed_repo(world, monkeypatch):
    monkeypatch.setattr(browse, "confirmed", lambda folder: False)
    assert scope.open_wiki(world.gateway, world.ws).state == "no_repo"


@pytest.mark.parametrize("role", ["owner", "maintainer", "member", "viewer"])
def test_all_member_roles(world, get, role):
    world.link.role = role
    assert get().status_code == 200


def test_time_budget_retries(world, monkeypatch):
    world.link.tickets = {"DEMO-1": {"title": "Title", "pages": "hello"}}
    monkeypatch.setattr(limits, "REFRESH_SECONDS", 0)
    assert index.links_by_slug(world.link, world.ws, "a") == {}
    monkeypatch.setattr(limits, "REFRESH_SECONDS", 3)
    assert index.links_by_slug(world.link, world.ws, "a")["hello"][0]["ticket"] == "DEMO-1"


def test_ticket_cap(world):
    world.link.tickets = {f"DEMO-{n}": {"title": "Title", "pages": "hello"} for n in range(401)}
    for _ in range(4):
        result = index.links_by_slug(world.link, world.ws, "a")
    assert len(result["hello"]) == limits.MAX_TICKETS
    assert all(row["ticket"] != "DEMO-400" for row in result["hello"])


def test_search_limit_and_order(world, get, monkeypatch):
    for slug, body in [("a", "# Other\nneedle"), ("z", "# Needle title")]:
        (world.folder / f"wiki/{slug}.md").write_text(body)
    commit(world)
    monkeypatch.setattr(limits, "SEARCH_HITS", 1)
    result = get("/search", q="needle").json()
    assert result["cut"] and result["pages"][0]["slug"] == "z"


def test_cut_blob_has_no_content(world, monkeypatch):
    from orch.gitbrowse import Result

    run = browse._run

    def cut(repo, op, *operands, **kwargs):
        if op == "blob":
            return Result(-9, b"partial", True)
        return run(repo, op, *operands, **kwargs)

    monkeypatch.setattr(browse, "_run", cut)
    result = reader.get_page(world.repo, "hello")
    assert result["too_large"] and result["markdown"] is None


def test_one_history_read(world, get):
    # Assert the batching contract without executing an additional git log per page.
    from unittest.mock import patch

    with patch.object(browse, "_run", wraps=browse._run) as run:
        assert get().status_code == 200
    history = [call for call in run.call_args_list if call.args[1] == "dir_log"]
    assert len(history) == 1 and history[0].args[2] == str(limits.LOG_COMMITS)


def test_wiki_submodule_is_not_a_folder(world, get):
    # Even when its commit is locally available, a gitlink is not the Wiki tree.
    sha = git(world.folder, "rev-parse", "HEAD")
    git(world.folder, "rm", "-r", "wiki")
    git(world.folder, "update-index", "--add", "--cacheinfo", f"160000,{sha},wiki")
    git(world.folder, "commit", "-qm", "gitlink")
    assert get().json()["state"] == "no_folder"
    assert get("/page", slug="hello").status_code == 404


def test_regular_executable_and_gitlink_entries(world, get):
    sha = git(world.folder, "rev-parse", "HEAD")
    (world.folder / "wiki/executable.md").write_text("# Executable")
    git(world.folder, "add", ".")
    git(world.folder, "update-index", "--chmod=+x", "wiki/executable.md")
    git(world.folder, "update-index", "--add", "--cacheinfo", f"160000,{sha},wiki/submodule.md")
    git(world.folder, "commit", "-qm", "modes")
    assert [r["slug"] for r in get().json()["pages"]] == ["executable", "hello"]
    assert get("/page", slug="submodule").status_code == 404


@pytest.mark.parametrize("role", ["owner", "maintainer", "member", "viewer"])
def test_every_role_of_a_member_may_read(world, get, role):
    world.link.role = role
    assert get().status_code == 200 and get("/page", slug="hello").status_code == 200


def test_titles_come_from_committed_text_only(world, get):
    (world.folder / "wiki/hello.md").write_text("# Edited in the work tree\n")
    git(world.folder, "add", "wiki/hello.md")  # staged, not committed
    assert [r["title"] for r in get().json()["pages"]] == ["Hello world"]


def _spy(world, monkeypatch):
    """Every ``orch.gitbrowse.run`` of the wiki code: ``(op, operand, files)``."""
    from orch import gitbrowse

    seen = []
    real = gitbrowse.run

    def run(repo, op, own, **kw):
        seen.append((op, kw.get("operand", ()), kw.get("files", ())))
        return real(repo, op, own, **kw)

    monkeypatch.setattr(reader.gitbrowse, "run", run)
    monkeypatch.setattr(browse.gitbrowse, "run", run)
    return seen


def test_content_reads_name_exactly_the_files_the_scan_cap_counted(world, get, monkeypatch):
    """Regression (review): a big blob, a subfolder file, a link and a non-page name under wiki/ are never read."""
    wiki = world.folder / "wiki"
    (wiki / "big.md").write_text("# Big\n" + "needle\n" * (limits.PAGE_MAX_BYTES // 7 + 10))  # over the page cap
    (wiki / "sub").mkdir()
    (wiki / "sub/deep.md").write_text("# Deep\nneedle\n")
    (wiki / "Upper.md").write_text("# Upper\nneedle\n")
    (wiki / "link.md").symlink_to("hello.md")
    (wiki / "note.txt").write_text("needle\n")
    commit(world)
    seen = _spy(world, monkeypatch)
    pages = get("/search", q="needle").json()
    assert [p["slug"] for p in pages["pages"]] == ["hello"]
    greps = [files for op, _operand, files in seen if op == "tree_grep"]
    assert greps and all(files == ("wiki/hello.md",) for files in greps)
    listing = get().json()["pages"]
    assert {p["slug"]: p["too_large"] for p in listing} == {"big": True, "hello": False}


def test_the_scan_cap_leaves_pages_out_and_says_cut(world, get, monkeypatch):
    (world.folder / "wiki/second.md").write_text("# Second\nneedle\n")
    commit(world)
    seen = _spy(world, monkeypatch)
    monkeypatch.setattr(limits, "SEARCH_SCAN_BYTES", 60)  # room for the first page only
    out = get("/search", q="needle").json()
    assert out["cut"] is True
    assert [f for op, _o, f in seen if op == "tree_grep"][-1] == ("wiki/hello.md",)


def test_a_hostile_file_name_cannot_forge_a_search_record(world, get):
    wiki = world.folder / "wiki"
    evil = wiki / "zz\nHEAD^{commit}:wiki" / "hello.md"  # a folder whose name ends the record and starts a fake one
    evil.parent.mkdir()
    evil.write_text("# Forged title\nneedle\n")
    commit(world)
    assert [p["title"] for p in get().json()["pages"]] == ["Hello world"]
    assert get("/search", q="Forged").json()["pages"] == []


def test_the_table_of_contents_is_not_a_regex_bomb():
    import time

    t = time.monotonic()
    out = reader.toc("## a" + " " * 60000 + "x\n" + "## ok\n")
    assert time.monotonic() - t < 1 and out == [{"level": 2, "text": "ok"}]
    assert len(reader.toc("## h\n" * 5000)) == reader.TOC_MAX
    assert reader.toc("#### four\n##nospace\n## Real ##\n### `Code` _it_\n") == [
        {"level": 2, "text": "Real"},
        {"level": 3, "text": "Code it"},
    ]


def test_a_commit_date_beyond_the_year_9999_does_not_break_the_list(world, get, monkeypatch):
    far = ("a" * 40, "Ann", "9999-12-31T19:00:00-05:00", "s", ["wiki/hello.md"])
    monkeypatch.setattr(reader.gitbrowse, "parse_dir_log", lambda out: [far])
    page = get().json()["pages"][0]
    assert page["slug"] == "hello" and page["updated"] is None


def test_a_listing_stopped_by_a_cap_is_cut_not_empty(world, get, monkeypatch):
    from orch import gitbrowse

    real = browse._run
    oid = "b" * 40

    def run(repo, op, *operand, **kw):
        if op == "tree":
            return gitbrowse.Result(-9, f"100644 blob {oid} 5\tx.md\x00100644 blob {oid} 5\ty.md-ext".encode(), True)
        return real(repo, op, *operand, **kw)

    monkeypatch.setattr(browse, "_run", run)
    out = get().json()
    assert out["state"] == "ok" and out["cut"] is True and [p["slug"] for p in out["pages"]] == ["x"]


def test_the_author_is_not_rewritten_by_an_uncommitted_mailmap(world, get):
    (world.folder / ".mailmap").write_text("Mallory <a@example.invalid>\n")
    assert get().json()["pages"][0]["by"] == "Ann"


def test_a_ticket_that_left_the_persons_list_leaves_the_answer_at_once(world, get):
    world.link.tickets = {"DEMO-1": {"title": "Was visible", "pages": "hello"}}
    assert get("/backlinks", slug="hello").json()["tickets"][0]["ticket"] == "DEMO-1"
    world.link.tickets["DEMO-1"]["people"] = ["b"]  # access taken away
    out = get("/backlinks", slug="hello")
    assert out.json()["tickets"] == [] and "Was visible" not in out.text


def test_pages_stay_readable_when_the_host_cannot_list_tickets(world, get):
    world.link.fail_query = True
    assert get().status_code == 200 and get("/page", slug="hello").json()["tickets"] == []


@pytest.fixture
def post(world, client, token):
    def call():
        return client.post(
            f"/api/w/{world.ws}/wiki/create", json={}, headers={"Authorization": f"Bearer {token}", "Origin": ORIGIN}
        )

    return call


def _no_wiki_repo(world, tmp_path, monkeypatch):
    folder = tmp_path / "nowiki"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "main")
    (folder / "notes.txt").write_text("x\n")
    git(folder, "add", ".")
    git(folder, "commit", "-qm", "first")
    monkeypatch.setattr(world.gateway, "workspace_folder", lambda ws: (folder, folder / ".orch"))
    browse._confirmed.clear()
    return folder


def test_create_the_wiki_folder_commits_only_the_readme(world, get, post, tmp_path, monkeypatch):
    folder = _no_wiki_repo(world, tmp_path, monkeypatch)
    world.link.role = "owner"
    (folder / "staged.txt").write_text("staged\n")
    git(folder, "add", "staged.txt")  # the person's own staged work must stay out of the commit
    assert get().json()["state"] == "no_folder"
    res = post()
    assert res.status_code == 200, res.text
    assert res.json() == {"created": True, "path": "wiki/readme.md"}
    assert (folder / "wiki/readme.md").read_text() == "# Wiki\n"
    shown = git(folder, "show", "--stat", "--format=%an <%ae>", "HEAD")
    assert "orch <orch@localhost>" in shown and "wiki/readme.md" in shown and "staged.txt" not in shown
    assert "staged.txt" in git(folder, "diff", "--cached", "--name-only")
    listed = get().json()
    assert listed["state"] == "ok" and [p["slug"] for p in listed["pages"]] == ["readme"]
    assert post().status_code == 409  # a second time: already there


def test_create_is_for_owners_and_maintainers(world, post, tmp_path, monkeypatch):
    folder = _no_wiki_repo(world, tmp_path, monkeypatch)
    world.link.role = "member"
    assert post().status_code == 403
    assert not (folder / "wiki").exists()
